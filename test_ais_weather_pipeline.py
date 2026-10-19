"""Offline tests: python -m unittest test_ais_weather_pipeline -v"""
import csv
from datetime import date
import io
import math
from pathlib import Path
import ssl
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import URLError

import ais_weather_pipeline as pipeline
import count_ais_near_misses as detector


def weather_fixture():
    start = detector.timestamp('2026-05-05T00:00:00Z')
    times = [start+i*3600 for i in range(48)]
    return [dict(id='w000_000',longitude=0,latitude=0,
                 hourly=dict(time=times,**{v:[10.0]*48 for v in pipeline.WEATHER_VARS}),
                 url='https://example.test/weather')]


class PipelineTests(unittest.TestCase):
    def test_system_https_fallback_keeps_verification_enabled(self):
        error = URLError(ssl.SSLCertVerificationError(1,'expired certificate chain'))
        with patch.object(pipeline,'urlopen',side_effect=error), \
             patch.object(pipeline.shutil,'which',return_value='system-curl'), \
             patch.object(pipeline.subprocess,'run',return_value=SimpleNamespace(
                 returncode=0,stdout=b'{"ok":true}',stderr=b'')) as run:
            with pipeline.request('https://archive-api.open-meteo.com/v1/archive') as response:
                self.assertEqual(response.read(),b'{"ok":true}')
            command = run.call_args.args[0]
            self.assertNotIn('--insecure',command)
            self.assertNotIn('-k',command)

    def test_index_uses_only_matching_noaa_links(self):
        parser = pipeline.IndexLinks()
        parser.feed('<a href="ais-2026-05-05.csv.zst">yes</a>'
                    '<a href="https://other.test/ais-2026-05-06.csv.zst">no</a>'
                    '<a href="../ais-2026-05-07.csv.zst">no</a>')
        self.assertEqual(list(parser.links),['2026-05-05'])

    def test_area_grid_and_request_limit(self):
        self.assertEqual(len(pipeline.weather_points([-74.3,40.4,-73.7,40.9],0.25,100)),6)
        with self.assertRaises(ValueError):
            pipeline.weather_points([-180,-80,180,80],0.25,100)

    def test_hour_match_and_distance_rejection(self):
        event = dict(first_flag_utc='2026-05-05T00:45:00Z',first_flag_longitude='0',
                     first_flag_latitude='0')
        match = pipeline.match_weather(event,weather_fixture(),50,1800)
        self.assertEqual(match['weather_match_status'],'matched')
        self.assertEqual(match['weather_time_offset_seconds'],900)
        self.assertEqual(match['wind_speed_10m_mps'],10)
        event['first_flag_longitude'] = '3'
        match = pipeline.match_weather(event,weather_fixture(),50,1800)
        self.assertEqual(match['weather_match_status'],'too_far')
        self.assertEqual(match['wind_speed_10m_mps'],'')

    def test_missing_weather_and_time_gap(self):
        series = weather_fixture()
        series[0]['hourly']['precipitation'][0] = None
        event = dict(first_flag_utc='2026-05-05T00:10:00Z',first_flag_longitude='0',
                     first_flag_latitude='0')
        self.assertEqual(pipeline.match_weather(event,series,50,1800)['weather_match_status'],
                         'partially_missing')
        event['first_flag_utc'] = '2026-05-04T00:00:00Z'
        self.assertEqual(pipeline.match_weather(event,series,50,1800)['weather_match_status'],
                         'time_gap')

    def test_download_integrity_and_reuse(self):
        class Response(io.BytesIO):
            headers = {'Content-Length':'4','ETag':'fixture'}
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)/'data'
            with patch.object(pipeline,'request',return_value=Response(b'data')) as req:
                pipeline.download('https://example.test/a',target)
                pipeline.download('https://example.test/a',target)
                self.assertEqual(req.call_count,1)
            target.write_bytes(b'changed')
            with self.assertRaises(ValueError):
                pipeline.download('https://example.test/a',target)

    def test_incomplete_download_not_published(self):
        class Response(io.BytesIO):
            headers = {'Content-Length':'100'}
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)/'data'
            with patch.object(pipeline,'request',return_value=Response(b'data')):
                with self.assertRaises(OSError):
                    pipeline.download('https://example.test/a',target)
            self.assertFalse(target.exists())

    def test_local_pipeline_counts_events_and_enriches(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root/'ais.csv'
            with source.open('w',newline='') as out:
                writer = csv.writer(out)
                writer.writerow(['mmsi','base_date_time','longitude','latitude','sog','cog'])
                for second in (0,30,60,90):
                    for m,meters,cog in [('111111111',second*10,90),
                                         ('222222222',2000-second*10,270)]:
                        writer.writerow([m,detector.utc(1777939200+second),
                                         math.degrees(meters/detector.EARTH_R),0,
                                         10/detector.KNOT,cog])
            # Weather network is mocked; AIS filtering, synchronization, relative
            # motion, consolidation, area reporting and output writing are real.
            with patch.object(pipeline,'get_weather',return_value=(weather_fixture(),[])):
                summary = pipeline.main(['--ais-file',str(source),'--date','2026-05-05',
                    '--bbox','-0.1','-0.1','0.1','0.1','--output-dir',str(root/'results'),'--environment'])
            self.assertEqual(summary['potential_near_miss_events'],1)
            self.assertEqual(summary['weather_match_counts'],{'matched':1})
            with (root/'results/events_with_weather.csv').open() as inp:
                events = list(csv.DictReader(inp))
            self.assertEqual(len(events),1)
            self.assertAlmostEqual(float(events[0]['first_flag_longitude']),
                                   math.degrees(1000/detector.EARTH_R),places=5)
            self.assertEqual(events[0]['weather_model'],'era5')
            self.assertTrue((root/'results/summary.json').exists())

    def test_parameter_validation_before_download(self):
        with self.assertRaises(ValueError):
            pipeline.validate_args(pipeline.parser().parse_args([]))
        args = pipeline.parser().parse_args(['--bbox','-74','40','-73','41',
                                             '--step-seconds','180'])
        with self.assertRaises(ValueError):
            pipeline.validate_args(args)


if __name__ == '__main__':
    unittest.main()
