"""Replay track selection tests."""
import unittest
import csv
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ais_event_dashboard import (collect_replay_tracks, compact_replay_records,
                                 hourly_environment, select_replay_tracks)
from ais_compact_store import encounter_id, open_writer, publish_day, replay_tracks
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

    def test_source_aware_collection_round_trips_through_store(self):
        day = date(2020, 1, 2)
        event = {'mmsi_a': '1', 'mmsi_b': '2',
                 'first_flag_longitude': '0', 'first_flag_latitude': '0',
                 'first_flag_utc': '2020-01-02T00:05:00Z',
                 'distance_time_utc': '2020-01-02T00:10:00Z',
                 'likely_near_miss_score_8': '8',
                 'likely_near_miss_classification': 'likely_near_miss_for_human_review'}
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            paths = [folder / 'previous.csv', folder / 'main.csv']
            for path, tick in zip(paths, ['2020-01-01T23:55:00Z', '2020-01-02T00:06:00Z']):
                with path.open('w', encoding='utf-8', newline='') as stream:
                    writer = csv.DictWriter(stream, fieldnames=[
                        'MMSI', 'BaseDateTime', 'LON', 'LAT', 'SOG', 'VesselName', 'Status'])
                    writer.writeheader()
                    writer.writerow({'MMSI': '1', 'BaseDateTime': tick, 'LON': '0',
                                     'LAT': '0', 'SOG': '5', 'VesselName': 'Ship', 'Status': '0'})
            scored = folder / 'scores.csv'
            with scored.open('w', encoding='utf-8', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(event))
                writer.writeheader()
                writer.writerow(event)
            traffic, eligible, tracks, sources = collect_replay_tracks(
                day, (-1, -1, 1, 1), paths[1], scored, None,
                lambda adjacent_day: paths[0], include_sources=True)
            encounters, samples = compact_replay_records(eligible, tracks, sources)
            self.assertEqual({sample['source_date'] for sample in samples},
                             {'2020-01-01', '2020-01-02'})
            self.assertEqual(len(traffic[0, 'moving']), 1)
            result = {'port': 'port', 'potential_events': 1, 'likely_for_human_review': 1,
                      'elevated_priority_candidates': 0, 'lower_priority_candidates': 0,
                      'detector_quality': {}, 'encounters': encounters, 'track_samples': samples}
            source_metadata = {
                source_date: {'available': True, 'source_url': 'https://example.test/' + source_date,
                              'sha256': source_date, 'compressed_bytes': 1}
                for source_date in ('2020-01-01', '2020-01-02')
            }
            db = open_writer(folder / 'store.sqlite')
            try:
                publish_day(db, day, source_metadata, [result], {'port': (-1, -1, 1, 1)}, 'score-8')
                key = encounter_id(day, 'port', '1', '2', event['first_flag_utc'])
                stored = replay_tracks(db, day, 'port', key)
                self.assertEqual([row[0] for row in stored['1']],
                                 [timestamp('2020-01-01T23:55:00Z'),
                                  timestamp('2020-01-02T00:06:00Z')])
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
