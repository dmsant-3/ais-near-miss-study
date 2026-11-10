"""Tests for durable, all-port publication in the compact store."""
from datetime import date
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

import ais_compact_store as store


class CompactStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'study.sqlite'
        self.db = store.open_writer(self.path)
        self.addCleanup(self.db.close)
        self.day = date(2020, 1, 2)
        self.bounds = {'NY': [0, 0, 1, 1], 'LA': [1, 1, 2, 2], 'Houston': [2, 2, 3, 3]}
        self.results = [
            {'port': port, 'potential_events': 0, 'likely_for_human_review': 0,
             'elevated_priority_candidates': 0, 'lower_priority_candidates': 0,
             'detector_quality': {}}
            for port in self.bounds
        ]
        self.sources = {
            '2020-01-02': {'available': True, 'source_url': 'https://example.test/main',
                           'sha256': 'abc', 'compressed_bytes': 123},
            '2020-01-01': {'available': False},
        }

    def publish(self):
        store.publish_day(self.db, self.day, self.sources, self.results,
                          self.bounds, 'score-8')

    def test_completed_date_survives_reopen_and_duplicate_is_rejected(self):
        self.publish()
        with closing(store.open_writer(self.path)) as reader:
            self.assertEqual(store.completed_dates(reader), {self.day})
            self.assertEqual(reader.execute('SELECT count(*) FROM port_day').fetchone()[0], 3)
            self.assertEqual(reader.execute('SELECT count(*) FROM ais_source').fetchone()[0], 2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.publish()
        self.assertEqual(self.db.execute('SELECT count(*) FROM port_day').fetchone()[0], 3)

    def test_failed_port_insert_rolls_back_day_and_sources(self):
        self.results[-1]['potential_events'] = -1
        with self.assertRaises(sqlite3.IntegrityError):
            self.publish()
        self.assertEqual(store.completed_dates(self.db), set())
        self.assertEqual(self.db.execute('SELECT count(*) FROM ais_source').fetchone()[0], 0)
        self.results[-1]['potential_events'] = 0
        self.publish()
        self.assertEqual(store.completed_dates(self.db), {self.day})

    def test_missing_or_duplicate_port_rejected(self):
        self.results[-1]['port'] = 'NY'
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(store.completed_dates(self.db), set())

    def test_unversioned_nonempty_database_rejected(self):
        other = Path(self.directory.name) / 'other.sqlite'
        with closing(sqlite3.connect(other)) as connection:
            connection.execute('CREATE TABLE existing (value TEXT)')
        with self.assertRaises(ValueError):
            store.open_writer(other)

    def test_replays_share_samples_and_cross_midnight(self):
        self.sources['2020-01-01'] = {
            'available': True, 'source_url': 'https://example.test/previous',
            'sha256': 'previous', 'compressed_bytes': 20,
        }
        samples = [
            {'source_date': '2020-01-01', 'mmsi': '111', 'tick': 99,
             'longitude': 1.0, 'latitude': 2.0, 'name': 'Ship', 'anchored': False},
            {'source_date': '2020-01-02', 'mmsi': '111', 'tick': 100,
             'longitude': 1.1, 'latitude': 2.0, 'name': 'Ship', 'anchored': False},
        ]
        event = {'mmsi_a': '111', 'mmsi_b': '222', 'first_flag_utc': '2020-01-02T00:00:00Z',
                 'window_start': 99, 'window_end': 100, 'details': {'score': 8},
                 'vessels': [{'mmsi': '111', 'role': 'involved'}]}
        second = dict(event, first_flag_utc='2020-01-02T00:01:00Z')
        self.results[0]['track_samples'] = samples + samples
        self.results[0]['encounters'] = [event, second]
        self.publish()
        self.assertEqual(self.db.execute('SELECT count(*) FROM track_sample').fetchone()[0], 2)
        for entry in (event, second):
            event_id = store.encounter_id(self.day, 'NY', '222', '111', entry['first_flag_utc'])
            tracks = store.replay_tracks(self.db, self.day, 'NY', event_id)
            self.assertEqual([point[0] for point in tracks['111']], [99, 100])

    def test_invalid_sample_source_rolls_back_publication(self):
        self.results[0]['track_samples'] = [
            {'source_date': '2020-01-01', 'mmsi': '111', 'tick': 1,
             'longitude': 1.0, 'latitude': 2.0, 'name': 'Ship', 'anchored': False}
        ]
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(store.completed_dates(self.db), set())

    def test_later_available_source_does_not_fill_earlier_missing_coverage(self):
        event = {'mmsi_a': '111', 'mmsi_b': '222', 'first_flag_utc': '2020-01-02T00:00:00Z',
                 'window_start': 99, 'window_end': 100, 'details': {},
                 'vessels': [{'mmsi': '111', 'role': 'involved'}]}
        self.results[0]['encounters'] = [event]
        self.publish()
        next_day = date(2020, 1, 3)
        next_sources = {
            '2020-01-03': {'available': True, 'source_url': 'https://example.test/next',
                           'sha256': 'next', 'compressed_bytes': 1},
            '2020-01-01': {'available': True, 'source_url': 'https://example.test/previous',
                           'sha256': 'previous', 'compressed_bytes': 1},
        }
        self.results[0]['encounters'] = []
        self.results[0]['track_samples'] = [
            {'source_date': '2020-01-01', 'mmsi': '111', 'tick': 99,
             'longitude': 1.0, 'latitude': 2.0, 'name': 'Ship', 'anchored': False}
        ]
        store.publish_day(self.db, next_day, next_sources, self.results, self.bounds, 'score-8')
        event_id = store.encounter_id(self.day, 'NY', '111', '222', event['first_flag_utc'])
        self.assertEqual(store.replay_tracks(self.db, self.day, 'NY', event_id), {})

    def test_v1_database_upgrades_without_losing_dates(self):
        old = Path(self.directory.name) / 'old.sqlite'
        with closing(sqlite3.connect(old)) as connection:
            connection.executescript(store.SCHEMA + '''
                INSERT INTO study_day VALUES ('2020-01-01', 'now', 'old');
                PRAGMA user_version = 1;
            ''')
        with closing(store.open_writer(old)) as upgraded:
            self.assertEqual(store.completed_dates(upgraded), {date(2020, 1, 1)})
            self.assertEqual(upgraded.execute('PRAGMA user_version').fetchone()[0], 3)

    def test_hourly_values_preserve_zero_missing_and_rollback(self):
        self.results[0]['traffic_hourly'] = [{'hour': 100, 'unique_moving': 0, 'unique_anchored': 2}]
        self.results[0]['environment_hourly'] = {
            100: [{'measurement': 'modeled_wind', 'speed_mps': 0,
                   'direction_deg': None, 'direction_convention': 'from'}]}
        self.results[0]['environment_sources'] = {'missing': ['waves']}
        self.publish()
        row = self.db.execute(
            'SELECT speed_mps, direction_deg FROM environment_hour').fetchone()
        self.assertEqual(row, (0.0, None))
        self.assertEqual(self.db.execute(
            'SELECT unique_moving, unique_anchored FROM traffic_hour').fetchone(), (0, 2))

    def test_invalid_hourly_record_rolls_back_entire_day(self):
        self.results[0]['traffic_hourly'] = [{'hour': 100, 'unique_moving': -1, 'unique_anchored': 0}]
        with self.assertRaises(sqlite3.IntegrityError):
            self.publish()
        self.assertEqual(store.completed_dates(self.db), set())


if __name__ == '__main__':
    unittest.main()
