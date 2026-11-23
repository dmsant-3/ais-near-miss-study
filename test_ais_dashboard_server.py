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
            root = Path(directory) / 'historical_compact_no_tugs_no_docks_report'
            root.mkdir()
            (root / 'run_config.json').write_text('{"output_format":"compact"}', encoding='utf-8')
            official = {'version': 1, 'matches': [{'agency': 'USCG', 'case_id': '12345',
                'date': '2020-01-02', 'ports': ['Port_of_NY_NJ'], 'reported_time': '2020-01-02T01:00:00',
                'time_basis': 'timezone unspecified', 'lat': 40, 'lon': -74, 'summary': '<unsafe>',
                'matches': [{'ais_vessel_name': 'VESSEL', 'mmsi': '123456789', 'identity_evidence': 'name',
                             'ais_time_utc': '2020-01-02T01:00:00Z', 'distance_m': 50,
                             'time_difference_seconds': None}]}]}
            (root / 'official_incident_matches.json').write_text(json.dumps(official), encoding='utf-8')
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
                    page = response.read().decode('utf-8')
                    self.assertIn('1 completed AIS dates</p>', page)
                    self.assertIn('produced before moored-location exclusion', page)
                    self.assertIn('OFFICIAL INCIDENT REPORTED', page)
                    self.assertIn('/official-incidents/USCG/12345', page)
                with urlopen(base + '/official-incidents/USCG/12345', timeout=10) as response:
                    detail = response.read().decode('utf-8')
                    self.assertIn('possible AIS match, requires review', detail)
                    self.assertIn('&lt;unsafe&gt;', detail)
                    self.assertNotIn('<unsafe>', detail)
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
                    check_legend = r'''
const fs = require('fs');
const shell = fs.readFileSync(0, 'utf8');
const start = shell.indexOf('function updateShipLegend(){');
const end = shell.indexOf("\nsel.addEventListener('change', updateShipLegend)", start);
if (start < 0 || end < 0) throw Error('Legend function missing');
function element(tag) { return {tag, style:{}, children:[], append(...items){this.children.push(...items)},
  replaceChildren(){this.children=[]}}; }
const document = {createElement: element};
const legend = {hidden:false}, legendItems = element('ul'), sel = {value:'0'};
const d = {events:[{mmsi_a:'123456789',mmsi_b:'987654321',tracks:{
  '123456789':[[0,0,0,'First ship']], '987654321':[[0,0,0,'Second ship']],
  '111111111':[[0,0,0,'Anchored']] }},
  {mmsi_a:'555555555',mmsi_b:'bad',tracks:{'555555555':[[0,0,0,'New ship']]}}]};
eval(shell.slice(start, end) + '\nupdateShipLegend();');
function links() {return legendItems.children.flatMap(item => item.children.filter(x => x && x.tag === 'a'));}
let found = links();
if(found.length !== 2 || found[0].href !== 'https://www.vesselfinder.com/vessels/details/123456789' ||
   found[1].href !== 'https://www.vesselfinder.com/vessels/details/987654321' ||
   found.some(link => link.rel !== 'noopener noreferrer' || link.target !== '_blank')) throw Error('Incorrect links');
sel.value = '1'; updateShipLegend(); found = links();
if(found.length !== 1 || !found[0].href.endsWith('/555555555')) throw Error('Links did not update');
'''
                    subprocess.run([shutil.which('node'), '-e', check_legend], input=shell,
                                   text=True, check=True, capture_output=True)
                    check_map = r'''
const fs = require('fs');
const shell = fs.readFileSync(0, 'utf8');
const start = shell.indexOf(' const mapZoomScale =');
const end = shell.indexOf(' for(const [id,pts,involved] of visible){', start);
if(start < 0 || end < 0) throw Error('Map code missing');
const document = {querySelector: () => ({value:'1'})};
const window = {aisPanX:20,aisPanY:-10};
const tiles = [];
class Image { constructor(){this.complete=true;this.naturalWidth=256;} set src(value){tiles.push(value);} }
const c = {drawImage(){}};
const x=-74, y=40, scale=Math.cos(y*Math.PI/180), radius=0.05;
function draw(){}
const result = eval(shell.slice(start,end) + '\n[xy([0,x,y]),xy([0,x+0.01,y+0.01])];');
if(Math.abs(result[0][0]-520)>0.0001 || Math.abs(result[0][1]-315)>0.0001 ||
   result[1][0]<=result[0][0] || result[1][1]>=result[0][1]) throw Error('Map and vessel coordinates are misaligned');
if(!tiles.length || tiles.length > 40 || tiles.some(url => !url.startsWith('https://tile.openstreetmap.org/'))) throw Error('Invalid visible tiles');
'''
                    subprocess.run([shutil.which('node'), '-e', check_map], input=shell,
                                   text=True, check=True, capture_output=True)
                    self.assertIn('OpenStreetMap contributors', shell)
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
