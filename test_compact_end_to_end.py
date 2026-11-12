"""Run both report modes through their real processing entry points."""
from contextlib import closing
from collections import Counter
import csv
from datetime import date
import json
import math
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import ais_compact_store as store
import count_ais_near_misses as detector
import run_historical_near_miss_study as runner


def node_supports_zstd():
    node = shutil.which('node')
    if not node:
        return False
    return subprocess.run([node, '-e', "process.exit(typeof require('zlib').createZstdCompress==='function'?0:1)"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


@unittest.skipUnless(node_supports_zstd(), 'Node.js with Zstandard support is required')
class CompactEndToEndTests(unittest.TestCase):
    def test_actual_three_port_processing_matches_legacy_summaries_and_replays(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            day = date(2020, 1, 2)
            start = int(detector.timestamp('2020-01-02T12:00:00Z'))
            csv_path = root / 'source.csv'
            compressed = root / 'source.csv.zst'
            fields = ['MMSI', 'BaseDateTime', 'LON', 'LAT', 'SOG', 'COG', 'VesselName',
                      'VesselType', 'Status', 'Length']
            with csv_path.open('w', encoding='utf-8', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for index, (port, bbox) in enumerate(runner.PORTS.items()):
                    lon = (bbox[0] + bbox[2]) / 2
                    lat = (bbox[1] + bbox[3]) / 2
                    lon_scale = math.cos(math.radians(lat)) * math.pi * detector.EARTH_R / 180
                    for seconds in range(0, 931, 30):
                        for side, direction in ((0, 1), (1, -1)):
                            offset = direction * (-8100 + 10 * seconds)
                            writer.writerow({'MMSI': str(100000000 + index * 10 + side),
                                             'BaseDateTime': detector.utc(start + seconds),
                                             'LON': lon + offset / lon_scale, 'LAT': lat,
                                             'SOG': 10 / detector.KNOT, 'COG': 90 if side == 0 else 270,
                                             'VesselName': port + str(side), 'VesselType': 70,
                                             'Status': 0, 'Length': 100})
            subprocess.run([shutil.which('node'), '-e',
                            "const f=require('fs'),z=require('zlib');"
                            "f.createReadStream(process.argv[1]).pipe(z.createZstdCompress())."
                            "pipe(f.createWriteStream(process.argv[2]));",
                            str(csv_path), str(compressed)], check=True)

            def download(url, target, reserve_bytes=0):
                shutil.copyfile(compressed, target)
                return {'source_url': url, 'sha256': 'fixture', 'compressed_bytes': target.stat().st_size}

            common = ['--start-date', str(day), '--end-date', str(day), '--pause-seconds', '0',
                      '--node', shutil.which('node')]
            legacy = root / 'legacy'
            compact = root / 'compact'
            with patch.object(runner, 'available_days', return_value={day: 'https://example.test/ais'}), \
                 patch.object(runner, 'download', side_effect=download), \
                 patch('ais_event_dashboard.collect_wind', return_value={}), \
                 patch('ais_event_dashboard.hourly_environment', return_value={}), \
                 patch.object(runner, 'collect_wind', return_value={}), \
                 patch.object(runner, 'collect_hourly_environment', return_value=({}, {})):
                self.assertEqual(runner.main(common + ['--report-dir', str(legacy),
                                                       '--work-dir', str(root / 'legacy_work')]), 0)
                self.assertEqual(runner.main(common + ['--output-format', 'compact',
                    '--report-dir', str(compact), '--work-dir', str(root / 'compact_work'),
                    '--work-reserve-bytes', '1', '--report-reserve-bytes', '1']), 0)

            summary = json.loads((legacy / 'daily' / str(day) / 'summary.json').read_text(encoding='utf-8'))
            with closing(store.open_writer(compact / 'study.sqlite')) as db:
                db.row_factory = sqlite3.Row
                for port_result in summary['ports']:
                    port = port_result['port']
                    row = db.execute('SELECT * FROM port_day WHERE ais_date=? AND port=?',
                                     (str(day), port)).fetchone()
                    self.assertGreater(port_result['potential_events'], 0)
                    self.assertEqual(row['potential_events'], port_result['potential_events'])
                    self.assertEqual(row['likely_for_human_review'], port_result['likely_for_human_review'])
                    html = (legacy / 'daily' / str(day) / port / 'event_dashboard.html').read_text(encoding='utf-8')
                    events = json.loads(re.search(
                        r'<script type="application/json" id="data">(.*?)</script>', html, re.S).group(1))['events']
                    with (legacy / 'daily' / str(day) / port / 'scored_candidates.csv').open(
                            encoding='utf-8-sig', newline='') as stream:
                        scored_rows = list(csv.DictReader(stream))
                    expected_counts = Counter(item['likely_near_miss_classification'] for item in scored_rows)
                    stored_counts = {item['classification']: item['count'] for item in db.execute(
                        'SELECT classification, count FROM classification_count WHERE ais_date=? AND port=?',
                        (str(day), port))}
                    self.assertEqual(stored_counts, expected_counts)
                    self.assertGreater(len(events), 0, str(scored_rows))
                    with (legacy / 'daily' / str(day) / port / 'traffic_hourly.csv').open(
                            encoding='utf-8', newline='') as stream:
                        legacy_traffic = [(int(detector.timestamp(item['window_start_utc'])),
                                           int(item['unique_moving']), int(item['unique_anchored']))
                                          for item in csv.DictReader(stream)]
                    stored_traffic = [tuple(item) for item in db.execute(
                        'SELECT hour, unique_moving, unique_anchored FROM traffic_hour '
                        'WHERE ais_date=? AND port=? ORDER BY hour', (str(day), port))]
                    self.assertEqual(stored_traffic, legacy_traffic)
                    for event in events:
                        key = store.encounter_id(day, port, event['mmsi_a'], event['mmsi_b'],
                                                 event['first_flag_utc'])
                        self.assertEqual(store.replay_tracks(db, day, port, key), event['tracks'])
                        saved = db.execute('SELECT * FROM encounter WHERE ais_date=? AND port=? AND encounter_id=?',
                                           (str(day), port, key)).fetchone()
                        details = store.encounter_details(db, saved)
                        self.assertEqual(details['likely_near_miss_score_8'],
                                         event['likely_near_miss_score_8'])
                        self.assertEqual(details['likely_near_miss_classification'],
                                         event['likely_near_miss_classification'])
                        self.assertEqual(details['likely_near_miss_evidence'],
                                         event['likely_near_miss_evidence'])
