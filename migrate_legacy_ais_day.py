"""Import one verified legacy AIS report date into a compact study database.

Legacy reports must include a completed daily summary, source checksums,
per-port hourly traffic and embedded replay data. Files remain in place.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re

import ais_compact_store as store
from count_ais_near_misses import timestamp
from run_historical_near_miss_study import PORTS, compact_lock, compact_size


def load_day(legacy_root: Path, day: date, retain_audit: bool):
    folder = legacy_root / 'daily' / day.isoformat()
    summary = json.loads((folder / 'summary.json').read_text(encoding='utf-8'))
    if summary['date'] != day.isoformat():
        raise ValueError('Legacy summary date does not match the requested date')
    sources = {day.isoformat(): {'available': True, **summary['source']}}
    sources.update(summary.get('adjacent_sources', {}))
    for source_date, source in sources.items():
        date.fromisoformat(source_date)
        if source['available']:
            digest = source.get('sha256')
            size = source.get('compressed_bytes')
            if (not source.get('source_url') or not isinstance(digest, str)
                    or re.fullmatch(r'[0-9a-fA-F]{64}', digest) is None
                    or not isinstance(size, int) or isinstance(size, bool) or size <= 0):
                raise ValueError(f'Legacy source provenance is incomplete for {source_date}')
    results = []
    expected = []
    for original in summary['ports']:
        port = original['port']
        if port not in PORTS:
            raise ValueError(f'Unknown port: {port}')
        port_dir = folder / port
        html = (port_dir / 'event_dashboard.html').read_text(encoding='utf-8')
        embedded = re.search(r'<script type="application/json" id="data">(.*?)</script>',
                             html, flags=re.DOTALL)
        if not embedded:
            raise ValueError(f'Missing embedded replay data for {port}')
        data = json.loads(embedded.group(1))
        samples = {}
        events = []
        seen = set()
        for legacy in data['events']:
            event = dict(legacy)
            tracks = event.pop('tracks')
            start = int(timestamp(event['window_start_utc']))
            end = int(timestamp(event['distance_time_utc'])) + 600
            vessels = []
            normalized = {}
            for mmsi, points in tracks.items():
                vessels.append({'mmsi': mmsi, 'role': 'involved' if mmsi in
                                (event['mmsi_a'], event['mmsi_b']) else 'anchored_neighbor'})
                normalized[mmsi] = []
                for tick, lon, lat, name, anchored in points:
                    source_date = datetime.fromtimestamp(tick, timezone.utc).date().isoformat()
                    if source_date not in sources or not sources[source_date]['available']:
                        raise ValueError(f'No verified legacy source for replay sample on {source_date}')
                    point = (tick, lon, lat, name, bool(anchored))
                    normalized[mmsi].append(list(point))
                    samples[(source_date, mmsi, *point)] = {
                        'source_date': source_date, 'mmsi': mmsi, 'tick': point[0],
                        'longitude': lon, 'latitude': lat, 'name': name, 'anchored': bool(anchored)}
                normalized[mmsi].sort()
            event_id = store.encounter_id(day, port, event['mmsi_a'], event['mmsi_b'],
                                          event['first_flag_utc'])
            expected.append((port, event_id, normalized))
            seen.add(event_id)
            events.append({'mmsi_a': event['mmsi_a'], 'mmsi_b': event['mmsi_b'],
                           'first_flag_utc': event['first_flag_utc'], 'window_start': start,
                           'window_end': end, 'details': event, 'vessels': vessels})
        scored = port_dir / 'scored_candidates.csv'
        classification_counts = Counter()
        scored_count = 0
        if scored.exists():
            with scored.open(encoding='utf-8-sig', newline='') as stream:
                for row in csv.DictReader(stream):
                    label = row['likely_near_miss_classification']
                    classification_counts[label] += 1
                    scored_count += 1
                    if not retain_audit and label not in (
                            'likely_near_miss_for_human_review', 'elevated_priority_candidate'):
                        continue
                    event_id = store.encounter_id(day, port, row['mmsi_a'], row['mmsi_b'],
                                                  row['first_flag_utc'])
                    if event_id in seen:
                        continue
                    begin = int(timestamp(row['first_flag_utc'])) - 1800
                    end = int(timestamp(row['distance_time_utc'])) + 600 if row['distance_time_utc'] else begin
                    events.append({'mmsi_a': row['mmsi_a'], 'mmsi_b': row['mmsi_b'],
                                   'first_flag_utc': row['first_flag_utc'], 'window_start': begin,
                                   'window_end': max(begin, end),
                                   'details': {**row, '_compact_replay': False}, 'vessels': []})
        if scored_count != int(original['potential_events']):
            raise ValueError(f'Legacy scored count does not match the summary for {port}')
        with (port_dir / 'traffic_hourly.csv').open(encoding='utf-8-sig', newline='') as stream:
            traffic = [{'hour': int(timestamp(row['window_start_utc'])),
                        'unique_moving': int(row['unique_moving']),
                        'unique_anchored': int(row['unique_anchored'])}
                       for row in csv.DictReader(stream)]
        day_start = int(timestamp(day.isoformat() + 'T00:00:00Z'))
        expected_hours = {day_start + 3600 * index for index in range(24)}
        if len(traffic) != 24 or {row['hour'] for row in traffic} != expected_hours:
            raise ValueError(f'Expected 24 hourly traffic rows for {port}')
        marine = {int(hour): rows for hour, rows in data.get('marine', {}).items()}
        for hour, values in data.get('wind', {}).items():
            bucket = marine.setdefault(int(hour), [])
            if not any(row['measurement'] == 'modeled_wind' for row in bucket):
                bucket.append({'measurement': 'modeled_wind', 'speed_mps': values[0],
                               'direction_deg': values[1], 'direction_convention': 'from'})
        source_file = port_dir / 'environment_sources.json'
        environment_sources = json.loads(source_file.read_text(encoding='utf-8')) if source_file.exists() else {}
        environment_sources['legacy_import'] = {
            'sample_source_assignment': 'UTC date of sample, verified against recorded source dates'}
        results.append({**original, 'encounters': events, 'track_samples': list(samples.values()),
                        'traffic_hourly': traffic, 'environment_hourly': marine,
                        'environment_sources': environment_sources,
                        'classification_counts': dict(classification_counts)})
    if {item['port'] for item in results} != set(PORTS):
        raise ValueError('Legacy day must contain all configured ports')
    return sources, results, expected


def import_day(legacy_root: Path, compact_root: Path, day: date,
               retain_audit: bool = False, limit_bytes: int = 10_000_000_000):
    """Verify and import one date. The caller must initialize the compact study."""
    sources, results, expected = load_day(legacy_root, day, retain_audit)
    with compact_lock(compact_root):
        config = json.loads((compact_root / 'run_config.json').read_text(encoding='utf-8'))
        if config.get('output_format') != 'compact' or config.get('retain_audit_candidates', False) != retain_audit:
            raise ValueError('Import settings must match the compact report configuration')
        if not date.fromisoformat(config['start_date']) <= day <= date.fromisoformat(config['end_date']):
            raise ValueError('Legacy date is outside the compact study range')
        db_path = compact_root / 'study.sqlite'
        if not db_path.exists():
            raise FileNotFoundError('Initialize the compact study before importing')
        non_database = compact_size(compact_root) - db_path.stat().st_size
        db = store.open_writer(db_path, max_bytes=limit_bytes - non_database - 8192)
        try:
            if len(store.completed_dates(db)) >= config['study_days']:
                raise ValueError('Compact study date limit reached')
            page_size = db.execute('PRAGMA page_size').fetchone()[0]
            max_pages = (limit_bytes - (compact_size(compact_root) - db_path.stat().st_size) - 8192) // page_size
            if max_pages < db.execute('PRAGMA page_count').fetchone()[0]:
                raise ValueError('Compact retained-output limit reached')
            db.execute(f'PRAGMA max_page_count = {max_pages}')
            def validate(connection):
                for port, event_id, tracks in expected:
                    if store.replay_tracks(connection, day, port, event_id) != tracks:
                        raise ValueError(f'Legacy replay mismatch for {port} {event_id}')
            store.publish_day(db, day, sources, results, PORTS, 'legacy-import', validate=validate)
        finally:
            db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('legacy_root', type=Path)
    parser.add_argument('compact_root', type=Path)
    parser.add_argument('date', type=date.fromisoformat)
    parser.add_argument('--retain-audit-candidates', action='store_true')
    parser.add_argument('--retained-limit-bytes', type=int, default=10_000_000_000)
    args = parser.parse_args()
    import_day(args.legacy_root, args.compact_root, args.date,
               args.retain_audit_candidates, args.retained_limit_bytes)
    print(f'Imported and verified {args.date}; legacy files remain in place.')


if __name__ == '__main__':
    main()
