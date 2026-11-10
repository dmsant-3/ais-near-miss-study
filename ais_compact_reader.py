"""Read compact study data without changing the database."""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

from ais_compact_store import SCHEMA_VERSION, replay_tracks


def open_reader(path: Path) -> sqlite3.Connection:
    """Open an existing compact database using SQLite read-only mode."""
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    if connection.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
        connection.close()
        raise ValueError('Unsupported compact store schema version')
    return connection


def progress(connection: sqlite3.Connection) -> dict:
    rows = connection.execute(
        'SELECT ais_date, port, potential_events, likely_for_human_review '
        'FROM port_day ORDER BY ais_date DESC, port'
    )
    return {'ports': [dict(row) for row in rows]}


def encounters(connection: sqlite3.Connection, day: date, port: str,
               limit: int = 50, cursor: str | None = None) -> dict:
    if not 1 <= limit <= 100:
        raise ValueError('limit must be between 1 and 100')
    day_text = day.isoformat()
    if not connection.execute('SELECT 1 FROM port_day WHERE ais_date=? AND port=?',
                              (day_text, port)).fetchone():
        raise KeyError((day_text, port))
    position = ('', '')
    if cursor:
        try:
            position = json.loads(cursor)
        except json.JSONDecodeError as exc:
            raise ValueError('Invalid encounter cursor') from exc
        if not isinstance(position, list) or len(position) != 2 or not all(
                isinstance(part, str) for part in position):
            raise ValueError('Invalid encounter cursor')
    rows = connection.execute("""
        SELECT encounter_id, details_json,
               COALESCE(json_extract(details_json, '$.distance_time_utc'), '') AS closest
        FROM encounter
        WHERE ais_date = ? AND port = ?
          AND (CAST(json_extract(details_json, '$.likely_near_miss_score_7') AS INTEGER) = 7
               OR CAST(json_extract(details_json, '$.likely_near_miss_score_8') AS INTEGER) = 8)
          AND COALESCE(json_extract(details_json, '$._compact_replay'), 1) = 1
          AND (closest, encounter_id) > (?, ?)
        ORDER BY closest, encounter_id LIMIT ?
    """, (day_text, port, *position, limit + 1)).fetchall()
    result = []
    for row in rows[:limit]:
        details = json.loads(row['details_json'])
        details['encounter_id'] = row['encounter_id']
        result.append(details)
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = json.dumps([last['closest'], last['encounter_id']], separators=(',', ':'))
    return {'encounters': result, 'next_cursor': next_cursor}


def replay(connection: sqlite3.Connection, day: date, port: str, event_id: str) -> dict:
    row = connection.execute(
        'SELECT details_json, window_start, window_end FROM encounter '
        'WHERE ais_date=? AND port=? AND encounter_id=?',
        (day.isoformat(), port, event_id),
    ).fetchone()
    if row is None:
        raise KeyError(event_id)
    event = json.loads(row['details_json'])
    event['tracks'] = replay_tracks(connection, day, port, event_id)
    event['window_start_utc'] = datetime.fromtimestamp(
        row['window_start'], timezone.utc).isoformat()
    first_hour = row['window_start'] // 3600 * 3600
    last_hour = row['window_end'] // 3600 * 3600
    observations = connection.execute(
        'SELECT * FROM environment_hour WHERE ais_date=? AND port=? AND hour BETWEEN ? AND ? '
        'ORDER BY hour, measurement, station_key',
        (day.isoformat(), port, first_hour, last_hour),
    )
    wind, marine = {}, {}
    for observation in observations:
        record = dict(observation)
        hour = record.pop('hour')
        for field in ('ais_date', 'port', 'station_key'):
            record.pop(field)
        marine.setdefault(hour, []).append(record)
        if record['measurement'] == 'modeled_wind':
            wind[hour] = [record['speed_mps'], record['direction_deg']]
    return {'events': [event], 'wind': wind, 'marine': marine}


def dashboard_shell() -> str:
    """Adapt the shared replay renderer to load a single selected encounter."""
    from ais_event_dashboard import HTML

    script = """
let pageCursor = null;
let replayRequest = 0;
async function loadSelected() {
 pause();
 const selection = sel.value;
 const summary = d.events[selection];
 if (!summary) { draw(); return; }
 const request = ++replayRequest;
 details.textContent = 'Loading replay…';
 try {
  const response = await fetch('/api/days/' + encodeURIComponent(aisDay) + '/ports/' +
      encodeURIComponent(aisPort) + '/encounters/' + encodeURIComponent(summary.encounter_id));
  if (!response.ok) throw new Error('Replay could not be loaded');
  const data = await response.json();
  if (request !== replayRequest || sel.value !== selection) return;
  d.events[selection] = Object.assign(data.events[0], {encounter_id: summary.encounter_id});
  d.wind = data.wind;
  d.marine = data.marine;
  selectEvent();
  if (typeof updateShipLegend === 'function') updateShipLegend();
 } catch (error) {
  if (request === replayRequest) details.textContent = error.message;
 }
}
async function loadPage() {
 const url = '/api/days/' + encodeURIComponent(aisDay) + '/ports/' +
     encodeURIComponent(aisPort) + '/encounters?limit=50' +
     (pageCursor ? '&cursor=' + encodeURIComponent(pageCursor) : '');
 try {
  const response = await fetch(url);
  if (!response.ok) throw new Error('Encounter list could not be loaded');
  const page = await response.json();
  const wasEmpty = d.events.length === 0;
  for (const event of page.encounters) {
   const index = d.events.length;
   d.events.push(event);
   sel.add(new Option(event.distance_time_utc + ' | score ' + event.likely_near_miss_score_8 +
       ' | ' + event.mmsi_a + ' / ' + event.mmsi_b, index));
  }
  pageCursor = page.next_cursor;
  more.hidden = !pageCursor;
  if (wasEmpty && d.events.length) loadSelected();
  else if (!d.events.length) draw();
 } catch (error) { details.textContent = error.message; }
}
const [aisDay, aisPort] = location.pathname.split('/').filter(Boolean);
const more = document.createElement('button');
more.textContent = 'Load more encounters';
more.hidden = true;
more.onclick = loadPage;
sel.after(more);
loadPage();
"""
    shell = HTML.replace('DATA_PLACEHOLDER', '{"events":[],"wind":{},"marine":{}}')
    shell = shell.replace("if(!e){details.textContent=", "if(!e || !e.tracks){details.textContent=")
    shell = shell.replace('sel.onchange=()=>{pause();selectEvent();};', 'sel.onchange=loadSelected;')
    return shell.replace('selectEvent();\n</script>', script + '\n</script>')


def export_csv(connection: sqlite3.Connection, kind: str, day: date, port: str | None,
               event_id: str | None, output) -> None:
    """Write a filtered CSV directly to a text stream."""
    from run_historical_near_miss_study import REPORT_FIELDS

    if event_id and not port:
        raise ValueError('A port is required for an encounter export')
    day_text = day.isoformat()
    if not connection.execute('SELECT 1 FROM study_day WHERE ais_date=?', (day_text,)).fetchone():
        raise KeyError(day_text)
    if port and not connection.execute('SELECT 1 FROM port_day WHERE ais_date=? AND port=?',
                                       (day_text, port)).fetchone():
        raise KeyError(port)
    params = [day_text]
    where = 'ais_date=?'
    if port:
        where += ' AND port=?'
        params.append(port)
    if kind == 'daily':
        fields = ['date', 'port', 'status', 'potential_events', 'likely_for_human_review',
                  'elevated_priority_candidates', 'lower_priority_candidates', 'scored_file']
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in connection.execute('SELECT * FROM port_day WHERE ' + where + ' ORDER BY port', params):
            writer.writerow({'date': row['ais_date'], 'port': row['port'], 'status': 'complete',
                             'potential_events': row['potential_events'],
                             'likely_for_human_review': row['likely_for_human_review'],
                             'elevated_priority_candidates': row['elevated_priority_candidates'],
                             'lower_priority_candidates': row['lower_priority_candidates']})
        return
    if event_id:
        where += ' AND encounter_id=?'
        params.append(event_id)
    def encounter_rows():
        position = ('', '', '')
        while True:
            batch = connection.execute(
                'SELECT * FROM encounter WHERE ' + where +
                ' AND (port, first_flag_utc, encounter_id) > (?, ?, ?)'
                ' ORDER BY port, first_flag_utc, encounter_id LIMIT 200',
                [*params, *position],
            ).fetchall()
            if not batch:
                return
            for record in batch:
                yield record
            last = batch[-1]
            position = (last['port'], last['first_flag_utc'], last['encounter_id'])
    if kind in ('candidates', 'likely'):
        writer = csv.DictWriter(output, fieldnames=REPORT_FIELDS, extrasaction='ignore')
        writer.writeheader()
        for row in encounter_rows():
            detail = json.loads(row['details_json'])
            label = detail.get('likely_near_miss_classification', '')
            if (label in ('separate_tugboat_review', 'excluded_tugboat', 'excluded_barge_or_tug')
                    or detail.get('involves_tugboat', '').lower() == 'true'):
                continue
            if kind == 'likely' and label not in (
                    'likely_near_miss_for_human_review', 'elevated_priority_candidate'):
                continue
            writer.writerow({'ais_date': day_text, 'port': row['port'], **detail})
        return
    if kind != 'trajectories':
        raise ValueError('Unknown export kind')
    fields = ['event_id', 'closest_utc', 'score', 'mmsi', 'ship_name', 'time_utc',
              'longitude', 'latitude', 'role', 'wind_mps', 'wind_from_deg']
    writer = csv.writer(output)
    writer.writerow(fields)
    for row in encounter_rows():
        detail = json.loads(row['details_json'])
        if detail.get('_compact_replay') is False:
            continue
        tracks = replay_tracks(connection, day, row['port'], row['encounter_id'])
        for ship, points in tracks.items():
            for tick, lon, lat, name, _ in points:
                hour = round(tick / 3600) * 3600
                wind = connection.execute(
                    "SELECT speed_mps, direction_deg FROM environment_hour WHERE ais_date=? AND port=? "
                    "AND hour=? AND measurement='modeled_wind'",
                    (day_text, row['port'], hour)).fetchone()
                writer.writerow([row['encounter_id'], detail['distance_time_utc'],
                                 detail['likely_near_miss_score_8'], ship, name,
                                 datetime.fromtimestamp(tick, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                                 lon, lat, 'involved' if ship in (row['mmsi_a'], row['mmsi_b'])
                                 else 'anchored_neighbor', *(wind if wind else ('', ''))])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('action', choices=['progress', 'encounters', 'replay', 'shell', 'export'])
    parser.add_argument('--date', type=date.fromisoformat)
    parser.add_argument('--port')
    parser.add_argument('--limit', type=int, default=50)
    parser.add_argument('--cursor')
    parser.add_argument('--encounter')
    parser.add_argument('--kind', choices=['daily', 'candidates', 'likely', 'trajectories'])
    args = parser.parse_args()
    if args.action not in ('progress', 'shell', 'export') and (not args.date or not args.port):
        parser.error('date and port are required')
    if args.action == 'export' and (not args.date or not args.kind):
        parser.error('date and kind are required')
    if args.action == 'replay' and not args.encounter:
        parser.error('encounter is required')
    if args.action == 'shell':
        print(dashboard_shell())
        return
    connection = open_reader(args.database)
    try:
        if args.action == 'progress':
            result = progress(connection)
        elif args.action == 'encounters':
            result = encounters(connection, args.date, args.port, args.limit, args.cursor)
        elif args.action == 'export':
            export_csv(connection, args.kind, args.date, args.port, args.encounter, sys.stdout)
            return
        else:
            result = replay(connection, args.date, args.port, args.encounter)
        print(json.dumps(result, allow_nan=False, separators=(',', ':')))
    finally:
        connection.close()


if __name__ == '__main__':
    main()
