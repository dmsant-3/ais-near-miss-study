"""Check official-report matching against small synthetic AIS files."""
import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import match_official_incidents as matcher


class IncidentMatchingTests(unittest.TestCase):
    def test_ntsb_time_location_identity_and_incremental_sources(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'ntsb.json'
            source.write_text(json.dumps([{'NTSBNumber': 'DCA26MM001',
                'EventDateTimeUTC': '2020-01-02T12:00:00Z', 'Latitude': 40.7,
                'Longitude': -74.0, 'Vehicles': [{'VehicleIds': [{'VesselName': 'TEST SHIP'}]}]}]))
            ais = root / 'ais-2020-01-02.csv'
            with ais.open('w', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(['MMSI', 'BaseDateTime', 'LAT', 'LON', 'VesselName', 'IMO'])
                writer.writerow(['123456789', '2020-01-02T12:30:00', 40.7, -74, 'Test Ship', ''])
                writer.writerow(['111111111', '2020-01-02T16:00:00', 40.7, -74, 'Test Ship', ''])
                writer.writerow(['222222222', '2020-01-02T12:00:00', 41.0, -74, 'Test Ship', ''])
                writer.writerow(['333333333', '2020-01-02T12:00:00', 40.7, -74, 'Other', ''])
            report = root / 'report'
            report.mkdir()
            target = report / 'official_incident_matches.json'
            target.write_text(json.dumps({'version': 1, 'processed_dates': [], 'matches': [
                {'agency': 'USCG', 'date': '2020-01-02', 'case_id': 'old'}]}))
            self.assertEqual(matcher.main(['--ntsb-json', str(source), '--ais-file', str(ais),
                                           '--report-dir', str(report)]), 0)
            data = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(len(data['matches']), 2)
            found = next(item for item in data['matches'] if item['agency'] == 'NTSB')
            self.assertEqual([item['mmsi'] for item in found['matches']], ['123456789'])
            self.assertEqual(found['matches'][0]['time_difference_seconds'], 1800)
            self.assertEqual(found['source']['sha256'], matcher.sha256(source))
            self.assertEqual(found['ais_source']['sha256'], matcher.sha256(ais))

    def test_uscg_export_keeps_unspecified_time_zone(self):
        try:
            import openpyxl
        except ImportError:
            self.skipTest('openpyxl is optional')
        from datetime import datetime
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'source.xlsx'
            workbook = openpyxl.Workbook()
            workbook.active.append(['Activity Id', 'Incident Date', 'Latitude', 'Longitude', 'Involved Vessel(s)'])
            workbook.active.append([123, datetime(2020, 1, 2, 12), 40.7, -74,
                                    'Vessel Name: TEST SHIP\nVIN: 1234567'])
            workbook.save(path)
            case = list(matcher.read_uscg(path))[0]
            self.assertEqual(case['vessels'][0]['reported_id'], '1234567')
            self.assertIn('timezone not specified', case['time_basis'])


if __name__ == '__main__':
    unittest.main()
