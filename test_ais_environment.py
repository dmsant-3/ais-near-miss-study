"""Offline provider-format, quality, vector, matching and integration tests."""
import csv
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ais_environment as env
import ais_weather_pipeline as pipeline
import count_ais_near_misses as detector

START = detector.timestamp('2026-05-05T00:00:00Z')
STATION = dict(id='test',name='Test station',longitude=0,latitude=0)


def args():
    return pipeline.parser().parse_args(['--bbox','-0.1','-0.1','0.1','0.1'])


def report():
    return dict(sources={k:dict(status='available') for k in env.VALUES})


class EnvironmentalTests(unittest.TestCase):
    def test_wave_discovery_prioritizes_buoys_over_weather_piers(self):
        downloader = env.EnvironmentalDownloader(args(),lambda *_:None)
        downloader.args.max_environment_stations = 1
        candidates = [dict(STATION,id='pier',station_type='fixed'),
                      dict(STATION,id='buoy',station_type='buoy',longitude=0.2)]
        self.assertEqual(downloader.select('waves',candidates)[0]['id'],'buoy')

    def test_visibility_km_to_meters_and_source_specific_qc(self):
        header = ['DATE','LATITUDE','LONGITUDE','visibility','visibility_Quality_Code',
                  'visibility_Source_Code','visibility_Measurement_Code']
        stream = io.StringIO()
        w = csv.writer(stream,delimiter='|')
        w.writerow(header)
        for minute,value,qc,source in [(0,'0.8','1','223'),(5,'2.0','2','223'),
                                       (10,'-9999','1','223'),(15,'3.0','5','223'),
                                       (20,'3.0','5','343')]:
            w.writerow([f'2026-05-05T00:{minute:02d}:00',0,0,value,qc,source,'P'])
        stream.seek(0)
        rows = env.parse_visibility(stream,STATION,START,START+86400)
        self.assertEqual(rows[0]['visibility_m'],800)
        self.assertEqual([r['usable'] for r in rows],[True,False,False,False,True])
        self.assertIsNone(rows[2]['visibility_m'])
        self.assertEqual(rows[0]['visibility_measurement_code'],'P')

    def test_visibility_unknown_quality_is_not_silently_accepted(self):
        self.assertFalse(env.visibility_quality('Q','999')[0])
        self.assertTrue(env.visibility_quality('','999')[0])
        self.assertEqual(env.visibility_quality('','999')[1],'quality_not_provided')

    def test_wave_sentinels_and_units(self):
        text = ('#YY MM DD hh mm WVHT DPD APD MWD\n'
                '#yr mo dy hr mn m sec sec deg\n'
                '2026 05 05 00 00 1.5 10.0 6.5 359\n'
                '2026 05 05 00 10 99.00 99.00 MM 999\n')
        rows = env.parse_waves(text,STATION,START,START+86400)
        self.assertEqual(rows[0]['significant_wave_height_m'],1.5)
        self.assertEqual(rows[0]['wave_direction_deg'],359)
        self.assertFalse(rows[1]['usable'])
        self.assertTrue(all(rows[1][k] is None for k in env.VALUES['waves']))

    def test_current_bins_conversion_vector_and_bad_flags(self):
        metadata = dict(units='meters',bins=[dict(num=2,depth=4,qc_flag=1),
            dict(num=3,depth=2,qc_flag=1),dict(num=4,depth=1,qc_flag=0)])
        self.assertEqual(env.choose_bin(metadata),(3,2))
        self.assertEqual(env.choose_bin(metadata,2),(2,4))
        with self.assertRaises(ValueError):
            env.choose_bin(metadata,4)
        data = dict(data=[dict(t='2026-05-05 00:00',s='100',d='90',b='3'),
                          dict(t='2026-05-05 00:06',s='50',d='359',b='3'),
                          dict(t='2026-05-05 00:12',s='50',d='0',b='3',f='1,0')])
        rows = env.parse_currents(data,STATION,START,START+86400,3,2)
        self.assertEqual(rows[0]['current_speed_mps'],1)
        self.assertAlmostEqual(rows[0]['current_east_mps'],1)
        self.assertAlmostEqual(rows[0]['current_north_mps'],0)
        self.assertLess(rows[1]['current_east_mps'],0)
        self.assertGreater(rows[1]['current_north_mps'],0)
        self.assertFalse(rows[2]['usable'])
        self.assertEqual(rows[0]['current_depth_m'],2)

    def test_matching_uses_other_station_when_nearest_is_missing_or_stale(self):
        records = []
        for station_id,lon,offset,usable in [('near',0,0,False),('stale',0.001,-7200,True),
                                            ('good',0.01,300,True)]:
            row = env.observation('visibility',dict(STATION,id=station_id,longitude=lon),
                                  START+offset,'test','hash',visibility_m=900,
                                  visibility_measurement_code='P')
            row['usable'] = usable
            records.append(row)
        event = dict(first_flag_utc=detector.utc(START),first_flag_longitude='0',first_flag_latitude='0')
        result = env.match_observations(event,records,args(),report())
        self.assertEqual(result['visibility_station_id'],'good')
        self.assertEqual(result['visibility_match_status'],'matched')
        self.assertEqual(result['visibility_m'],900)
        self.assertEqual(result['visibility_time_offset_seconds'],300)

    def test_rejected_match_has_no_measurement_values(self):
        row = env.observation('waves',dict(STATION,longitude=10),START,'test','hash',
                              significant_wave_height_m=2)
        row['usable'] = True
        event = dict(first_flag_utc=detector.utc(START),first_flag_longitude='0',first_flag_latitude='0')
        result = env.match_observations(event,[row],args(),report())
        self.assertEqual(result['waves_match_status'],'too_far')
        self.assertEqual(result['significant_wave_height_m'],'')

    def test_source_failure_report_and_disabled_sources(self):
        settings = args()
        settings.environment = ['visibility']
        downloader = env.EnvironmentalDownloader(settings,lambda *_:None)
        with patch.object(downloader,'visibility_stations',side_effect=OSError('service unavailable')):
            rows,status = downloader.run()
        self.assertEqual(rows,[])
        self.assertFalse(status['coverage_complete'])
        self.assertEqual(status['sources']['visibility']['status'],'source_error')
        self.assertEqual(status['sources']['currents']['status'],'disabled')

    def test_end_to_end_pipeline_writes_environment_and_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root/'ais.csv'
            source.write_text('placeholder',encoding='utf-8')
            event = dict.fromkeys(detector.EVENT_FIELDS,'')
            event.update(event_id='one',first_flag_utc=detector.utc(START),
                         first_flag_longitude='0',first_flag_latitude='0')
            observation = env.observation('visibility',STATION,START,'test','hash',
                                          visibility_m=500,visibility_measurement_code='P')
            observation['usable'] = True
            source_report = dict(coverage_complete=True,requests=[],errors=[],sources={
                k:dict(status='available',usable=1) for k in env.VALUES})
            with patch.object(pipeline,'get_weather',return_value=([],[])), \
                 patch.object(pipeline,'run_detection',return_value=([event],{}, {},[-1,-1,1,1])), \
                 patch.object(env.EnvironmentalDownloader,'run',return_value=([observation],source_report)):
                result = pipeline.main(['--ais-file',str(source),'--bbox','-1','-1','1','1',
                                        '--output-dir',str(root/'out')])
            self.assertEqual(result['environment_match_counts']['visibility'],{'matched':1})
            self.assertTrue((root/'out/environment_sources.json').exists())
            with (root/'out/events_with_weather.csv').open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]['visibility_m'],'500')
            self.assertEqual(rows[0]['visibility_station_id'],'test')


if __name__ == '__main__':
    unittest.main()
