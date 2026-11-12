"""Offline legacy report migration checks."""
import csv
from contextlib import closing
from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from collections import namedtuple

import ais_compact_store as store
from count_ais_near_misses import timestamp, utc
from migrate_legacy_ais_day import import_day
from run_historical_near_miss_study import PORTS


class MigrationTests(unittest.TestCase):
    def test_import_verifies_replay_and_refuses_missing_source_provenance(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            legacy, compact = root / 'legacy', root / 'compact'
            day = date(2020, 1, 2)
            daily = legacy / 'daily' / str(day)
            compact.mkdir()
            config = {'output_format': 'compact', 'start_date': str(day), 'end_date': str(day),
                      'study_days': 30, 'retain_audit_candidates': False}
            (compact / 'run_config.json').write_text(json.dumps(config), encoding='utf-8')
            with closing(store.open_writer(compact / 'study.sqlite')):
                pass
            results = []
            tick = int(timestamp('2020-01-02T00:05:00Z'))
            event = {'mmsi_a': '111', 'mmsi_b': '222', 'first_flag_utc': '2020-01-02T00:05:00Z',
                     'distance_time_utc': '2020-01-02T00:10:00Z', 'window_start_utc': utc(tick - 1800),
                     'likely_near_miss_score_8': '8', 'first_flag_longitude': '0',
                     'first_flag_latitude': '0', 'likely_near_miss_classification': 'elevated_priority_candidate',
                     'tracks': {'111': [[tick, 0, 0, 'Ship', False]]}}
            for port in PORTS:
                port_dir = daily / port
                port_dir.mkdir(parents=True)
                data = {'events': [event], 'wind': {}, 'marine': {}}
                (port_dir / 'event_dashboard.html').write_text(
                    '<script type="application/json" id="data">' + json.dumps(data) + '</script>',
                    encoding='utf-8')
                scored = {key: value for key, value in event.items() if key != 'tracks'}
                with (port_dir / 'scored_candidates.csv').open('w', encoding='utf-8', newline='') as stream:
                    writer = csv.DictWriter(stream, fieldnames=list(scored))
                    writer.writeheader()
                    writer.writerow(scored)
                with (port_dir / 'traffic_hourly.csv').open('w', encoding='utf-8', newline='') as stream:
                    writer = csv.writer(stream)
                    writer.writerow(['window_start_utc', 'unique_moving', 'unique_anchored'])
                    for hour in range(24):
                        writer.writerow([utc(timestamp(str(day) + 'T00:00:00') + hour * 3600), 0, 0])
                results.append({'port': port, 'potential_events': 1, 'likely_for_human_review': 1,
                                'elevated_priority_candidates': 0, 'lower_priority_candidates': 0,
                                'detector_quality': {}})
            summary = {'date': str(day), 'source': {'source_url': 'https://example.test/ais',
                       'sha256': 'a' * 64, 'compressed_bytes': 123}, 'ports': results}
            summary_path = daily / 'summary.json'
            summary_path.write_text(json.dumps(summary), encoding='utf-8')
            broken = dict(summary, source={'source_url': 'https://example.test/ais'})
            summary_path.write_text(json.dumps(broken), encoding='utf-8')
            with self.assertRaises(ValueError):
                import_day(legacy, compact, day)
            with closing(store.open_writer(compact / 'study.sqlite')) as db:
                self.assertEqual(store.completed_dates(db), set())
            summary_path.write_text(json.dumps(summary), encoding='utf-8')
            usage = namedtuple('Usage', 'total used free')(100, 99, 1)
            with patch('migrate_legacy_ais_day.shutil.disk_usage', return_value=usage):
                with self.assertRaisesRegex(OSError, 'migration report reserve'):
                    import_day(legacy, compact, day, report_reserve_bytes=2)
            import_day(legacy, compact, day)
            with closing(store.open_writer(compact / 'study.sqlite')) as db:
                self.assertEqual(store.completed_dates(db), {day})
            self.assertTrue(summary_path.exists())
