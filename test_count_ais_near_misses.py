"""Run with: python -m unittest test_count_ais_near_misses -v"""
import csv
import gzip
import math
from pathlib import Path
import tempfile
import unittest

import count_ais_near_misses as ais


class DetectionTests(unittest.TestCase):
    def test_relative_motion(self):
        # B is 1000 m east; A and B close at 10 m/s each.
        lon = math.degrees(1000/ais.EARTH_R)
        distance,dcpa,tcpa = ais.relative_cpa((0,0,10,0),(lon,0,-10,0))
        self.assertAlmostEqual(distance,1000,places=5)
        self.assertAlmostEqual(dcpa,0,places=5)
        self.assertAlmostEqual(tcpa,50,places=5)
        self.assertLess(ais.relative_cpa((0,0,-10,0),(lon,0,10,0))[2],0)
        self.assertIsNone(ais.relative_cpa((0,0,10,0),(lon,0,10,0))[2])

    def test_crossing_at_different_times(self):
        # A reaches crossing in 100 s, B in 200 s; their CPA is not zero.
        lat = math.degrees(-2000/ais.EARTH_R)
        lon = math.degrees(-1000/ais.EARTH_R)
        _,dcpa,tcpa = ais.relative_cpa((lon,0,10,0),(0,lat,0,10))
        self.assertGreater(dcpa,700)
        self.assertAlmostEqual(tcpa,150,places=4)

    def test_dateline_grid(self):
        frame = [('111111111',179.999,0,1,0),('222222222',-179.999,0,-1,0)]
        self.assertEqual(len(list(ais.candidate_pairs(frame,1000))),1)

    def test_end_to_end_unsorted_gzip_and_event_merging(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root/'reports.csv.gz'
            rows = []
            # Two separated head-on encounters, with several flags per event.
            for start in (0,600):
                for dt in (0,30,60,90):
                    tick = start+dt
                    for m,meters,cog in [('111111111',dt*10,90),
                                         ('222222222',2000-dt*10,270)]:
                        rows.append([m,ais.utc(1777939200+tick),
                                     math.degrees(meters/ais.EARTH_R),0,10/ais.KNOT,cog])
            rows.append(rows[0])  # Duplicate must not inflate event counts.
            rows.append(['333333333','bad time',0,0,1,90])
            with gzip.open(source,'wt',newline='') as out:
                writer = csv.writer(out)
                writer.writerow(['MMSI','BaseDateTime','LON','LAT','SOG','COG'])
                writer.writerows(reversed(rows))
            summary = ais.main([str(source),'--output-dir',str(root/'results')])
            self.assertEqual(summary['counts']['events'],2)
            self.assertEqual(summary['counts']['flagged_pair_samples'],8)
            self.assertEqual(summary['quality']['invalid_rows'],1)
            self.assertEqual(summary['quality']['duplicate_or_conflicting_timestamp_rows'],1)
            with (root/'results/potential_near_miss_events.csv').open() as inp:
                events = list(csv.DictReader(inp))
            self.assertEqual([int(e['flag_count']) for e in events],[4,4])
            self.assertAlmostEqual(float(events[0]['tcpa_at_first_flag_s']),100,places=3)

    def test_long_gap_not_interpolated(self):
        import sqlite3
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE reports(m TEXT,t REAL,lon REAL,lat REAL,ve REAL,vn REAL)')
        db.executemany('INSERT INTO reports VALUES (?,?,?,?,?,?)',
                       [('111111111',1,0,0,1,0),('111111111',301,0.001,0,1,0)])
        args = ais.parser().parse_args(['unused.csv'])
        quality = ais.synchronize(db,args)
        self.assertEqual(quality['aligned_samples'],0)
        self.assertEqual(quality['rejected_or_long_gap_segments'],1)
        db.close()


if __name__ == '__main__':
    unittest.main()
