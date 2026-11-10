"""Tests for durable, all-port publication in the compact store."""
from datetime import date, datetime, timezone
from contextlib import closing
from pathlib import Path
import csv
import io
import sqlite3
import tempfile
import unittest

import ais_compact_store as store
import ais_compact_reader as reader


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

    def test_existing_store_cannot_open_beyond_retained_limit(self):
        self.publish()
        with self.assertRaises(OSError):
            store.open_writer(self.path, max_bytes=1)
        self.assertEqual(store.completed_dates(self.db), {self.day})

    def test_score_seven_replay_is_listed_and_exported(self):
        event = {'mmsi_a': '111', 'mmsi_b': '222', 'first_flag_utc': '2020-01-02T00:00:00Z',
                 'window_start': 90, 'window_end': 120,
                 'details': {'likely_near_miss_score_7': '7', 'likely_near_miss_score_8': '0',
                             'distance_time_utc': '2020-01-02T00:01:00Z'},
                 'vessels': [{'mmsi': '111', 'role': 'involved'}]}
        self.results[0]['encounters'] = [event]
        self.results[0]['track_samples'] = [
            {'source_date': str(self.day), 'mmsi': '111', 'tick': 100, 'longitude': 1,
             'latitude': 2, 'name': 'Ship', 'anchored': False}]
        self.publish()
        with closing(reader.open_reader(self.path)) as db:
            page = reader.encounters(db, self.day, 'NY')
            self.assertEqual(len(page['encounters']), 1)
            output = io.StringIO(newline='')
            reader.export_csv(db, 'trajectories', self.day, 'NY',
                              page['encounters'][0]['encounter_id'], output)
            rows = list(csv.DictReader(io.StringIO(output.getvalue())))
            self.assertEqual(rows[0]['score'], '7')

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
            self.assertEqual(upgraded.execute('PRAGMA user_version').fetchone()[0], 8)

    def test_v3_database_adds_measurements(self):
        old = Path(self.directory.name) / 'v3.sqlite'
        with closing(sqlite3.connect(old)) as connection:
            connection.executescript(store.SCHEMA + store.REPLAY_SCHEMA + store.HOURLY_SCHEMA
                                     + 'PRAGMA user_version = 3;')
        with closing(store.open_writer(old)) as upgraded:
            self.assertEqual(upgraded.execute('PRAGMA user_version').fetchone()[0], 8)
            self.assertEqual(upgraded.execute('SELECT count(*) FROM storage_measurement').fetchone()[0], 0)

    def test_v4_database_keeps_legacy_evidence_during_upgrade(self):
        old = Path(self.directory.name) / 'v4.sqlite'
        with closing(sqlite3.connect(old)) as connection:
            connection.executescript(store.SCHEMA + store.REPLAY_SCHEMA + store.HOURLY_SCHEMA
                                     + store.MEASUREMENT_SCHEMA + """
                INSERT INTO study_day VALUES ('2020-01-02', 'now', 'old');
                INSERT INTO port_day VALUES ('2020-01-02', 'NY', 1, 1, 0, 0, '{}', '[]');
                INSERT INTO encounter VALUES ('2020-01-02', 'NY', 'id', '1', '2', '2020-01-02T00:00:00Z',
                    0, 1, '{"likely_near_miss_score_8":"8","likely_near_miss_evidence":"old reason","minimum_dcpa_m":"12.5","maneuver_evidence_a":"true"}');
                PRAGMA user_version = 4;
            """)
        with closing(store.open_writer(old)) as upgraded:
            upgraded.row_factory = sqlite3.Row
            row = upgraded.execute('SELECT * FROM encounter').fetchone()
            self.assertEqual(row['score_8'], 8)
            self.assertEqual(row['minimum_dcpa_m'], 12.5)
            self.assertEqual(row['maneuver_evidence_a'], 1)
            self.assertEqual(store.encounter_details(upgraded, row)['likely_near_miss_evidence'], 'old reason')

    def test_v6_environment_values_survive_normalization(self):
        old = Path(self.directory.name) / 'v6.sqlite'
        with closing(sqlite3.connect(old)) as connection:
            connection.create_function('compact_number', 1, store._number)
            connection.create_function('compact_flag', 1, store._flag)
            connection.executescript(
                store.SCHEMA + store.REPLAY_SCHEMA + store.HOURLY_SCHEMA +
                store.MEASUREMENT_SCHEMA + store.EVIDENCE_SCHEMA + store.NUMERIC_SCHEMA + """
                INSERT INTO study_day VALUES ('2020-01-02', 'now', 'old');
                INSERT INTO port_day VALUES ('2020-01-02', 'NY', 0, 0, 0, 0, '{}', '[]');
                INSERT INTO environment_hour VALUES ('2020-01-02', 'NY', 100, 'currents', 'A',
                    'A', 1, 2, NULL, 0, NULL, 'toward', NULL, NULL);
                PRAGMA user_version = 6;
            """)
        with closing(store.open_writer(old)) as upgraded:
            row = upgraded.execute(
                'SELECT speed_mps, direction_deg, direction_convention FROM environment_hour').fetchone()
            self.assertEqual(row, (0, None, 'toward'))
            self.assertEqual(upgraded.execute('PRAGMA user_version').fetchone()[0], 8)

    def test_v7_vessel_names_survive_normalization(self):
        old = Path(self.directory.name) / 'v7.sqlite'
        with closing(sqlite3.connect(old)) as connection:
            connection.create_function('compact_number', 1, store._number)
            connection.create_function('compact_flag', 1, store._flag)
            connection.create_function('compact_environment_hash', 12, store._environment_hash)
            connection.executescript(
                store.SCHEMA + store.REPLAY_SCHEMA + store.HOURLY_SCHEMA + store.MEASUREMENT_SCHEMA +
                store.EVIDENCE_SCHEMA + store.NUMERIC_SCHEMA + store.ENVIRONMENT_SCHEMA + """
                INSERT INTO source_file VALUES ('2020-01-02', 'source');
                INSERT INTO track_sample VALUES ('NY', '2020-01-02', 'source',
                    '111', 100, 1, 2, 'Ship', 0);
                PRAGMA user_version = 7;
            """)
        with closing(store.open_writer(old)) as upgraded:
            self.assertEqual(upgraded.execute(
                'SELECT n.mmsi, n.name FROM track_sample t JOIN vessel_name n ON n.name_id=t.name_id'
            ).fetchone(), ('111', 'Ship'))
            self.assertEqual(upgraded.execute('PRAGMA user_version').fetchone()[0], 8)

    def test_hourly_values_preserve_zero_missing_and_rollback(self):
        hour = int(datetime(2020, 1, 2, tzinfo=timezone.utc).timestamp())
        self.results[0]['traffic_hourly'] = [{'hour': hour, 'unique_moving': 0, 'unique_anchored': 2}]
        self.results[0]['environment_hourly'] = {
            hour: [{'measurement': 'modeled_wind', 'speed_mps': 0,
                   'direction_deg': None, 'direction_convention': 'from'}]}
        self.results[0]['environment_sources'] = {'missing': ['waves']}
        self.publish()
        row = self.db.execute(
            'SELECT speed_mps, direction_deg FROM environment_hour').fetchone()
        self.assertEqual(row, (0.0, None))
        self.assertEqual(self.db.execute(
            'SELECT unique_moving, unique_anchored FROM traffic_hour').fetchone(), (0, 2))
        with closing(reader.open_reader(self.path)) as db:
            for kind, field in (('traffic', 'unique_moving'), ('environment', 'speed_mps')):
                output = io.StringIO(newline='')
                reader.export_csv(db, kind, self.day, 'NY', None, output)
                rows = list(csv.DictReader(io.StringIO(output.getvalue())))
                self.assertEqual(float(rows[0][field]), 0)

    def test_hourly_observations_are_shared_across_dates(self):
        self.results[0]['environment_hourly'] = {
            100: [{'measurement': 'currents', 'station_id': 'A', 'speed_mps': 0,
                   'direction_deg': None, 'direction_convention': 'toward'}]}
        self.publish()
        another = date(2020, 1, 3)
        other_sources = {'2020-01-03': {'available': True, 'source_url': 'https://example.test/next',
                                        'sha256': 'next', 'compressed_bytes': 1}}
        store.publish_day(self.db, another, other_sources, self.results, self.bounds, 'score-8')
        self.assertEqual(self.db.execute('SELECT count(*) FROM environment_observation').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM replay_hour').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM environment_hour').fetchone()[0], 2)

    def test_invalid_hourly_record_rolls_back_entire_day(self):
        self.results[0]['traffic_hourly'] = [{'hour': 100, 'unique_moving': -1, 'unique_anchored': 0}]
        with self.assertRaises(sqlite3.IntegrityError):
            self.publish()
        self.assertEqual(store.completed_dates(self.db), set())

    def test_read_only_pagination_and_replay(self):
        base = {'mmsi_a': '111', 'mmsi_b': '222', 'window_start': 90, 'window_end': 120,
                'vessels': [{'mmsi': '111', 'role': 'involved'}]}
        self.results[0]['encounters'] = [
            dict(base, first_flag_utc='2020-01-02T00:00:00Z',
                 details={'likely_near_miss_score_8': '8', 'distance_time_utc': '2020-01-02T00:01:00Z',
                          'likely_near_miss_evidence': 'close pass; maneuver',
                          'observed_min_separation_in_window_m': '0',
                          'maneuver_evidence_a': '', 'maneuver_evidence_b': 'false'}),
            dict(base, first_flag_utc='2020-01-02T00:02:00Z',
                 details={'likely_near_miss_score_8': '8', 'distance_time_utc': '2020-01-02T00:03:00Z',
                          'likely_near_miss_evidence': 'close pass; maneuver'}),
            dict(base, first_flag_utc='2020-01-02T00:04:00Z',
                 details={'likely_near_miss_score_8': '6', 'distance_time_utc': '2020-01-02T00:05:00Z',
                          'likely_near_miss_evidence': None}),
        ]
        self.results[0]['environment_hourly'] = {
            0: [{'measurement': 'modeled_wind', 'speed_mps': 0, 'direction_deg': None}]}
        self.results[0]['traffic_hourly'] = [{'hour': 0, 'unique_moving': 3, 'unique_anchored': 1}]
        self.publish()
        with closing(reader.open_reader(self.path)) as db:
            first = reader.encounters(db, self.day, 'NY', 1)
            second = reader.encounters(db, self.day, 'NY', 1, first['next_cursor'])
            self.assertEqual(len(first['encounters']), 1)
            self.assertEqual(first['encounters'][0]['likely_near_miss_evidence'], 'close pass; maneuver')
            self.assertEqual(db.execute('SELECT count(*) FROM score_reason').fetchone()[0], 2)
            no_reason = db.execute(
                'SELECT * FROM encounter WHERE first_flag_utc=?', ('2020-01-02T00:04:00Z',)).fetchone()
            self.assertIsNone(store.encounter_details(db, no_reason)['likely_near_miss_evidence'])
            typed = db.execute(
                'SELECT observed_min_separation_in_window_m, maneuver_evidence_a, maneuver_evidence_b '
                'FROM encounter WHERE encounter_id=?', (first['encounters'][0]['encounter_id'],)).fetchone()
            self.assertEqual(tuple(typed), (0, None, 0))
            self.assertIsNone(second['next_cursor'])
            self.assertNotEqual(first['encounters'][0]['encounter_id'],
                                second['encounters'][0]['encounter_id'])
            data = reader.replay(db, self.day, 'NY', first['encounters'][0]['encounter_id'])
            self.assertEqual(data['wind'][0], [0, None])
            self.assertEqual(data['traffic'][0], {'moving': 3, 'anchored': 1})
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM study_day')


if __name__ == '__main__':
    unittest.main()
