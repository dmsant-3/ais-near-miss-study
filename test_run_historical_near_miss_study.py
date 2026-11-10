"""Offline compact runner publication and restart tests."""
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from collections import defaultdict, namedtuple
import json
import csv
import unittest
from unittest.mock import patch

import ais_compact_store
import run_historical_near_miss_study as runner


class CompactRunnerTests(unittest.TestCase):
    def test_compact_retains_review_candidates_without_replays(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            day = date(2020, 1, 2)
            event = {'mmsi_a': '1', 'mmsi_b': '2', 'first_flag_utc': '2020-01-02T00:05:00Z',
                     'distance_time_utc': '', 'likely_near_miss_score_8': '5',
                     'likely_near_miss_classification': 'elevated_priority_candidate'}
            def run_command(command):
                if command[1].endswith('count_ais_near_misses.py'):
                    detected = Path(command[command.index('--output-dir') + 1])
                    (detected / 'summary.json').write_text('{"quality": {}}', encoding='utf-8')
                    (detected / 'potential_near_miss_events.csv').write_text('id\n1\n', encoding='utf-8')
                elif command[1].endswith('rank_likely_near_misses.py'):
                    target = Path(command[command.index('--output') + 1])
                    with target.open('w', encoding='utf-8', newline='') as stream:
                        writer = csv.DictWriter(stream, fieldnames=list(event))
                        writer.writeheader()
                        writer.writerow(event)
            with patch.object(runner, 'run_command', side_effect=run_command), \
                 patch.object(runner, 'collect_replay_tracks', return_value=(defaultdict(set), [], {}, {})), \
                 patch.object(runner, 'collect_wind', return_value={}), \
                 patch.object(runner, 'collect_hourly_environment', return_value=({}, {})):
                result = runner.analyze_port(day, 'port', (0, 0, 1, 1), root / 'ais',
                                             root, None, compact=True)
            self.assertEqual(result['likely_for_human_review'], 1)
            self.assertEqual(len(result['encounters']), 1)
            self.assertFalse(result['encounters'][0]['details']['_compact_replay'])

    def test_publishes_all_ports_and_skips_committed_date_on_restart(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report, work = root / 'report', root / 'work'
            day = date(2020, 1, 2)
            args = ['--output-format', 'compact', '--start-date', str(day),
                    '--end-date', str(day), '--report-dir', str(report), '--work-dir', str(work),
                    '--work-reserve-bytes', '1', '--report-reserve-bytes', '1', '--pause-seconds', '0']

            def download(url, target, reserve):
                target.write_bytes(b'ais')
                return {'source_url': url, 'sha256': 'fixture', 'compressed_bytes': 3}

            def analyze(day, port, bbox, ais, staging, node, adjacent, compact=False,
                        cache_policy=None, retain_audit_candidates=False, work_reserve_bytes=0):
                self.assertTrue(compact)
                return {'port': port, 'potential_events': 0, 'likely_for_human_review': 0,
                        'elevated_priority_candidates': 0, 'lower_priority_candidates': 0,
                        'detector_quality': {}}

            with patch.object(runner, 'available_days', return_value={day: 'https://example.test/ais'}), \
                 patch.object(runner, 'download', side_effect=download) as downloader, \
                 patch.object(runner, 'analyze_port', side_effect=analyze):
                self.assertEqual(runner.main(args), 0)
                config_file = report / 'run_config.json'
                old_config = json.loads(config_file.read_text(encoding='utf-8'))
                old_config.pop('scoring_version')
                old_config.pop('retain_audit_candidates')
                config_file.write_text(json.dumps(old_config), encoding='utf-8')
                self.assertEqual(runner.main(args), 0)
                self.assertEqual(downloader.call_count, 1)
                self.assertIn('scoring_version', json.loads(config_file.read_text(encoding='utf-8')))
            connection = ais_compact_store.open_writer(report / 'study.sqlite')
            try:
                self.assertEqual(ais_compact_store.completed_dates(connection), {day})
                self.assertEqual(connection.execute('SELECT count(*) FROM port_day').fetchone()[0], 3)
                measurement = connection.execute(
                    'SELECT retained_before, retained_after, work_scratch_sampled_peak '
                    'FROM storage_measurement').fetchone()
                self.assertGreaterEqual(measurement[1], measurement[0])
                self.assertGreater(measurement[2], 0)
            finally:
                connection.close()

    def test_requires_explicit_date_and_reserves(self):
        with self.assertRaises(ValueError):
            runner.main(['--output-format', 'compact'])

    def test_space_preflight_stops_before_download(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            day = date(2020, 1, 2)
            args = ['--output-format', 'compact', '--start-date', str(day),
                    '--end-date', str(day), '--report-dir', str(root / 'report'),
                    '--work-dir', str(root / 'work'), '--work-reserve-bytes', '1000',
                    '--report-reserve-bytes', '1000']
            usage = namedtuple('Usage', 'total used free')(100, 99, 1)
            with patch.object(runner, 'available_days', return_value={day: 'https://example.test/ais'}), \
                 patch.object(runner.shutil, 'disk_usage', return_value=usage), \
                 patch.object(runner, 'download') as download:
                with self.assertRaisesRegex(OSError, 'Insufficient free space'):
                    runner.main(args)
                download.assert_not_called()
            status = json.loads((root / 'report' / 'run_status.json').read_text(encoding='utf-8'))
            self.assertEqual(status['state'], 'failed')
            self.assertIn('Insufficient free space', status['message'])


if __name__ == '__main__':
    unittest.main()
