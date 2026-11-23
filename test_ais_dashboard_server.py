"""Offline HTTP checks for compact dashboard routes."""
from contextlib import closing
from datetime import date
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
from tempfile import TemporaryDirectory
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import ais_compact_store as store


@unittest.skipUnless(shutil.which('node'), 'Node.js is required')
class CompactDashboardTests(unittest.TestCase):
    def test_progress_pagination_replay_shell_and_gzip_export(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'historical_compact_no_tugs_report'
            root.mkdir()
            (root / 'run_config.json').write_text('{"output_format":"compact"}', encoding='utf-8')
            day = date(2020, 1, 2)
            port = 'Port_of_NY_NJ'
            event = {'mmsi_a': '111', 'mmsi_b': '222', 'first_flag_utc': '2020-01-02T00:00:00Z',
                     'window_start': 90, 'window_end': 120, 'vessels': [],
                     'details': {'mmsi_a': '111', 'mmsi_b': '222', 'first_flag_longitude': '0',
                                 'first_flag_latitude': '0', 'distance_time_utc': '2020-01-02T00:01:00Z',
                                 'likely_near_miss_score_8': '8',
                                 'likely_near_miss_classification': 'likely_near_miss_for_human_review'}}
            result = {'port': port, 'potential_events': 1, 'likely_for_human_review': 1,
                      'elevated_priority_candidates': 0, 'lower_priority_candidates': 0,
                      'detector_quality': {}, 'encounters': [event]}
            sources = {str(day): {'available': True, 'source_url': 'https://example.test/ais',
                                  'sha256': 'fixture', 'compressed_bytes': 1}}
            with closing(store.open_writer(root / 'study.sqlite')) as db:
                store.publish_day(db, day, sources, [result], {port: [0, 0, 1, 1]}, 'score-8')
            with closing(socket.socket()) as sock:
                sock.bind(('127.0.0.1', 0))
                port_number = sock.getsockname()[1]
            env = dict(os.environ, AIS_DASHBOARD_PORT=str(port_number))
            env.pop('AIS_PYTHON', None)
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            server = Path(__file__).resolve().with_name('ais_dashboard_server.js')
            process = subprocess.Popen([shutil.which('node'), str(server)], cwd=directory,
                                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                       creationflags=flags)
            try:
                base = f'http://127.0.0.1:{port_number}'
                for attempt in range(50):
                    try:
                        with urlopen(base + '/api/progress', timeout=1) as response:
                            progress = json.load(response)
                        break
                    except HTTPError as exc:
                        self.fail(f'Dashboard returned {exc.code}: {exc.read().decode()}')
                    except OSError as exc:
                        if getattr(getattr(exc, 'reason', None), 'winerror', None) == 10013:
                            self.skipTest('Loopback connections are restricted by the sandbox')
                        last_error = exc
                        if process.poll() is not None:
                            self.fail(process.stderr.read().decode('utf-8', errors='replace'))
                        time.sleep(0.1)
                else:
                    self.fail(f'Dashboard did not start: {last_error}')
                self.assertEqual(progress['ports'][0]['port'], port)
                with urlopen(base + '/progress.html', timeout=10) as response:
                    self.assertIn('1 completed AIS dates</p>', response.read().decode('utf-8'))
                with urlopen(base + f'/api/days/{day}/ports/{port}/provenance', timeout=10) as response:
                    provenance = json.load(response)
                self.assertEqual(provenance['ais_sources'][0]['sha256'], 'fixture')
                prefix = f'/api/days/{day}/ports/{port}/encounters'
                with urlopen(base + prefix, timeout=10) as response:
                    events = json.load(response)['encounters']
                with urlopen(base + prefix + '/' + events[0]['encounter_id'], timeout=10) as response:
                    self.assertEqual(len(json.load(response)['events']), 1)
                with urlopen(base + f'/{day}/{port}', timeout=10) as response:
                    shell = response.read().decode('utf-8')
                    self.assertIn('loadSelected', shell)
                    self.assertIn('Review scores · UTC', shell)
                    self.assertNotIn('All listed encounters scored 7 out of 7', shell)
                    self.assertIn('First flag', shell)
                    self.assertIn('Closest approach', shell)
                request = Request(base + f'/api/export/candidates?date={day}&port={port}',
                                  headers={'Accept-Encoding': 'gzip'})
                with urlopen(request, timeout=10) as response:
                    self.assertEqual(response.headers['Content-Encoding'], 'gzip')
                    self.assertIn('111', gzip.decompress(response.read()).decode('utf-8'))
                request = Request(base + f'/api/export/candidates?date={day}&port={port}',
                                  headers={'Accept-Encoding': 'gzip;q=0'})
                with urlopen(request, timeout=10) as response:
                    self.assertIsNone(response.headers.get('Content-Encoding'))
                    self.assertIn('111', response.read().decode('utf-8'))
                with closing(store.open_writer(root / 'study.sqlite')) as db:
                    page_ports = (port, 'Port_of_LA', 'Port_of_Houston')
                    page_results = [{'port': name, 'potential_events': 0,
                                     'likely_for_human_review': 0, 'elevated_priority_candidates': 0,
                                     'lower_priority_candidates': 0, 'detector_quality': {}}
                                    for name in page_ports]
                    bounds = {name: [0, 0, 1, 1] for name in page_ports}
                    for day_number in range(1, 32):
                        extra_day = date(2021, 10, day_number)
                        store.publish_day(db, extra_day, {str(extra_day): sources[str(day)]},
                                          page_results, bounds, 'score-8')
                    store.publish_day(db, date(2022, 1, 1), {'2022-01-01': sources[str(day)]},
                                      [{'port': port, 'potential_events': 0,
                                        'likely_for_human_review': 0, 'elevated_priority_candidates': 0,
                                        'lower_priority_candidates': 0, 'detector_quality': {}}],
                                      {port: [0, 0, 1, 1]}, 'score-8')
                with urlopen(base + '/progress.html', timeout=10) as response:
                    listing = response.read().decode('utf-8')
                listed = re.findall(r'href="/(\d{4}-\d{2}-\d{2})/Port_of_NY_NJ"', listing)
                self.assertEqual(len(listed), 30)
                self.assertEqual(listed[0], '2022-01-01')
                self.assertIn('/2021-10-31/Port_of_LA', listing)
                self.assertIn('/2021-10-31/Port_of_Houston', listing)
                self.assertIn('name="year"', listing)
                self.assertIn('name="month"', listing)
                self.assertIn('rel="next"', listing)
                with urlopen(base + '/progress.html?year=2021&month=10&page=2', timeout=10) as response:
                    listing = response.read().decode('utf-8')
                self.assertEqual(re.findall(r'href="/(\d{4}-\d{2}-\d{2})/Port_of_NY_NJ"', listing),
                                 ['2021-10-01'])
                self.assertIn('Page 2 of 2 · 31 matching dates', listing)
                self.assertIn('year=2021&amp;month=10&amp;page=1', listing)
                with urlopen(base + '/progress.html?year=2020&month=02', timeout=10) as response:
                    self.assertIn('No completed dates match', response.read().decode('utf-8'))
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + '/progress.html?month=13', timeout=10)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
            finally:
                process.terminate()
                process.wait(timeout=10)
                process.stderr.close()
