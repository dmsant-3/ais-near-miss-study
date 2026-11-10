"""Replay track selection tests."""
import unittest
import csv
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ais_event_dashboard import hourly_environment, select_replay_tracks
from count_ais_near_misses import timestamp


class ReplayTrackSelectionTests(unittest.TestCase):
    def test_overlapping_windows_and_anchored_neighbors(self):
        event = {'mmsi_a': '1', 'mmsi_b': '2',
                 'first_flag_longitude': '0', 'first_flag_latitude': '0'}
        tracks = {
            '1': [(10, 0.1, 0.1, 'Ship', False),
                  (20, 0.2, 0.2, 'Ship', False),
                  (20, 0.2, 0.2, 'Ship', False)],
            '2': [(30, 0.2, 0.2, 'Other', False)],
            '3': [(15, 0.0, 0.001, 'Anchor', True)],
            '4': [(15, 1.0, 1.0, 'Far', True)],
            '5': [(15, 0.0, 0.001, 'Moving', False)],
        }
        first, second = list(select_replay_tracks(
            [(event, (10, 20)), (dict(event), (20, 30))], tracks))
        self.assertEqual(set(first[2]), {'1', '3'})
        self.assertEqual([p[0] for p in first[2]['1']], [10, 20])
        self.assertEqual(set(second[2]), {'1', '2'})
        self.assertEqual([p[0] for p in second[2]['1']], [20])
        self.assertEqual([p[0] for p in second[2]['2']], [30])

    def test_empty_eligible_list(self):
        self.assertEqual(list(select_replay_tracks([], {})), [])

    def test_environment_export_keeps_zero_and_missing_values(self):
        day = date(2020, 1, 2)
        start = timestamp('2020-01-02T00:00:00Z')
        report = {'sources': {'currents': {'stations': []}}}
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch('ais_event_dashboard.EnvironmentalDownloader.run',
                       return_value=([], report)):
                marine = hourly_environment(day, (0, 0, 1, 1), start, folder,
                                            {start: (0, 0)})
            self.assertEqual(marine[start][0]['wave_height_m'], None)
            self.assertEqual(marine[start][1]['speed_mps'], 0)
            with (folder / 'environment_hourly.csv').open(newline='', encoding='utf-8') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 48)
            self.assertEqual(rows[0]['wave_height_m'], '')
            self.assertEqual(rows[1]['speed_mps'], '0')
            self.assertTrue((folder / 'environment_sources.json').exists())


if __name__ == '__main__':
    unittest.main()
