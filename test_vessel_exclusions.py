"""Verify tug and barge exclusions across screening and replay."""
import csv
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ais_event_dashboard import collect_replay_tracks
from confirm_near_misses import excluded_vessel, load_tracks
import rank_likely_near_misses as ranker


class VesselExclusionTests(unittest.TestCase):
    def test_type_and_specific_identity_rules(self):
        for vessel_type in ('31', '32.0', '52', '1003', '1014', '1015', '1023', '1025', 'Freight Barge'):
            self.assertTrue(excluded_vessel(vessel_type), vessel_type)
        self.assertTrue(excluded_vessel('53', '  captain   FAYRA '))
        self.assertTrue(excluded_vessel('', '', '338547847'))
        self.assertFalse(excluded_vessel('53', 'Captain Fayra II', '123'))
        self.assertFalse(excluded_vessel('70', 'Cargo Ship', '123'))

    def test_confirm_and_replay_exclude_identified_vessels(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ais = root / 'ais.csv'
            fields = ['MMSI', 'BaseDateTime', 'LON', 'LAT', 'SOG', 'COG',
                      'VesselName', 'VesselType', 'Status']
            with ais.open('w', encoding='utf-8', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for mmsi, name, kind, speed, status in [
                        ('338547847', 'CAPTAIN FAYRA', '53', '5', '0'),
                        ('2', 'Cargo', '70', '5', '0'),
                        ('3', 'Nearby Barge', '1003', '0', '1')]:
                    writer.writerow(dict(zip(fields, [mmsi, '2020-01-02T00:05:00Z', '0', '0',
                                                       speed, '90', name, kind, status])))
            excluded = set()
            load_tracks(ais, {'338547847', '2', '3'}, [-1, -1, 1, 1], 100, excluded)
            self.assertEqual(excluded, {'338547847', '3'})
            row = {'involves_excluded_vessel': 'True'}
            result = ranker.score_row(row, {ranker.key(k): k for k in row})
            self.assertEqual(result['likely_near_miss_classification'], 'excluded_barge_or_tug')
            event = {'mmsi_a': '338547847', 'mmsi_b': '2', 'first_flag_longitude': '0',
                     'first_flag_latitude': '0', 'first_flag_utc': '2020-01-02T00:05:00Z',
                     'distance_time_utc': '2020-01-02T00:06:00Z', 'likely_near_miss_score_8': '8',
                     'likely_near_miss_classification': 'likely_near_miss_for_human_review'}
            scored = root / 'scored.csv'
            with scored.open('w', encoding='utf-8', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(event))
                writer.writeheader()
                writer.writerow(event)
            traffic, eligible, tracks, _ = collect_replay_tracks(
                date(2020, 1, 2), (-1, -1, 1, 1), ais, scored, None)
            self.assertEqual(eligible, [])
            self.assertEqual(set(tracks), {'2'})
            self.assertEqual(traffic[0, 'moving'], {'2'})
            self.assertEqual(traffic[0, 'anchored'], set())


if __name__ == '__main__':
    unittest.main()
