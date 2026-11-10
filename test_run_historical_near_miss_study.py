"""Offline compact runner publication and restart tests."""
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from collections import namedtuple
import unittest
from unittest.mock import patch

import ais_compact_store
import run_historical_near_miss_study as runner


class CompactRunnerTests(unittest.TestCase):
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

            def analyze(day, port, bbox, ais, staging, node, adjacent, compact=False, cache_policy=None):
                self.assertTrue(compact)
                return {'port': port, 'potential_events': 0, 'likely_for_human_review': 0,
                        'elevated_priority_candidates': 0, 'lower_priority_candidates': 0,
                        'detector_quality': {}}

            with patch.object(runner, 'available_days', return_value={day: 'https://example.test/ais'}), \
                 patch.object(runner, 'download', side_effect=download) as downloader, \
                 patch.object(runner, 'analyze_port', side_effect=analyze):
                self.assertEqual(runner.main(args), 0)
                self.assertEqual(runner.main(args), 0)
                self.assertEqual(downloader.call_count, 1)
            connection = ais_compact_store.open_writer(report / 'study.sqlite')
            try:
                self.assertEqual(ais_compact_store.completed_dates(connection), {day})
                self.assertEqual(connection.execute('SELECT count(*) FROM port_day').fetchone()[0], 3)
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


if __name__ == '__main__':
    unittest.main()
