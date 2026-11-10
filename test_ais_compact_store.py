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


if __name__ == '__main__':
    unittest.main()
