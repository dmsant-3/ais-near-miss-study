"""Read compact study data without changing the database."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3

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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('action', choices=['progress', 'encounters', 'replay'])
    parser.add_argument('--date', type=date.fromisoformat)
    parser.add_argument('--port')
    parser.add_argument('--limit', type=int, default=50)
    parser.add_argument('--cursor')
    parser.add_argument('--encounter')
    args = parser.parse_args()
    if args.action != 'progress' and (not args.date or not args.port):
        parser.error('date and port are required')
    if args.action == 'replay' and not args.encounter:
        parser.error('encounter is required')
    connection = open_reader(args.database)
    try:
        if args.action == 'progress':
            result = progress(connection)
        elif args.action == 'encounters':
            result = encounters(connection, args.date, args.port, args.limit, args.cursor)
        else:
            result = replay(connection, args.date, args.port, args.encounter)
        print(json.dumps(result, allow_nan=False, separators=(',', ':')))
    finally:
        connection.close()


if __name__ == '__main__':
    main()
