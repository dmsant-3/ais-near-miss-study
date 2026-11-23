#!/usr/bin/env python3
"""Match official marine investigation exports to raw daily AIS observations.

Matches are leads for review, not proof that an AIS observation is the incident.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys

from count_ais_near_misses import EARTH_R, open_csv, timestamp, utc
from run_historical_near_miss_study import PORTS

DISTANCE_LIMIT_M = 2 * 1852
NTSB_TIME_LIMIT_SECONDS = 2 * 3600


def canonical(value):
    return ''.join(c for c in str(value or '').casefold() if c.isalnum())


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def location(raw_lat, raw_lon):
    lat, lon = number(raw_lat), number(raw_lon)
    if lat is None or lon is None or not (-90 < lat < 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def ports_at(lat, lon):
    return [port for port, (west, south, east, north) in PORTS.items()
            if west <= lon <= east and south <= lat <= north]


def read_uscg(path):
    try:
        import openpyxl
    except ImportError as exc:
        raise RuntimeError('Install openpyxl to read the Coast Guard export') from exc
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        rows = iter(workbook.active.values)
        headings = next(rows)
        required = {'Activity Id', 'Incident Date', 'Latitude', 'Longitude', 'Involved Vessel(s)'}
        if not required.issubset(headings):
            raise ValueError('Unrecognized Coast Guard IIR export columns')
        for row in rows:
            data = dict(zip(headings, row))
            when, point = data['Incident Date'], location(data['Latitude'], data['Longitude'])
            if not isinstance(when, datetime) or point is None:
                continue
            lat, lon = point
            vessels = []
            for section in re.split(r'(?=Vessel Name:)', str(data['Involved Vessel(s)'] or ''), flags=re.I):
                name = re.search(r'^Vessel Name:\s*([^\r\n]+)', section, re.I | re.M)
                ident = re.search(r'^VIN:\s*([^\r\n]+)', section, re.I | re.M)
                if name:
                    vessels.append({'name': name.group(1).strip(),
                                    'reported_id': ident.group(1).strip() if ident else ''})
            case_id = str(data['Activity Id'] or '').strip()
            if not case_id or not vessels:
                continue
            yield {'agency': 'USCG', 'case_id': case_id, 'date': when.date().isoformat(),
                   'reported_time': when.isoformat(), 'time_basis': 'USCG export; timezone not specified',
                   'lat': lat, 'lon': lon, 'ports': ports_at(lat, lon), 'vessels': vessels,
                   'summary': str(data.get('Incident Brief') or ''),
                   'report_url': 'https://cgmix.uscg.mil/iir/Default.aspx'}
    finally:
        workbook.close()


def read_ntsb(path):
    cases = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(cases, list):
        raise ValueError('Unrecognized NTSB CAROL JSON export')
    for data in cases:
        try:
            when = datetime.fromisoformat(data['EventDateTimeUTC'].replace('Z', '+00:00'))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            when = when.astimezone(timezone.utc)
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        point = location(data.get('Latitude'), data.get('Longitude'))
        if point is None:
            continue
        lat, lon = point
        case_id = str(data.get('NTSBNumber') or '')
        if not re.fullmatch(r'[A-Za-z0-9-]+', case_id):
            continue
        vessels = []
        for vehicle in data.get('Vehicles') or []:
            for identity in vehicle.get('VehicleIds') or []:
                if identity.get('VesselName'):
                    vessels.append({'name': str(identity['VesselName']).strip(), 'reported_id': ''})
        if not vessels:
            continue
        yield {'agency': 'NTSB', 'case_id': case_id, 'date': when.date().isoformat(),
               'reported_time': when.isoformat(), 'time_basis': 'UTC', 'lat': lat, 'lon': lon,
               'ports': ports_at(lat, lon), 'vessels': vessels,
               'summary': str(data.get('CompletionStatus') or ''),
               'report_url': 'https://www.ntsb.gov/investigations/Pages/' + case_id + '.aspx'}


def distance_m(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 2 * EARTH_R * math.asin(min(1, math.sqrt(a)))


def match_day(path, day, cases):
    """Return the nearest same-name/identifier AIS report for each case and vessel."""
    cases = [case for case in cases if case['date'] == day and case['ports']]
    names, identifiers = {}, {}
    for index, case in enumerate(cases):
        for vessel in case['vessels']:
            name = canonical(vessel['name'])
            if name:
                names.setdefault(name, set()).add(index)
            ident = canonical(vessel['reported_id'])
            if re.fullmatch(r'[0-9]{7}', ident):
                identifiers.setdefault(ident, set()).add(index)
    if not cases:
        return []
    best = {}
    with open_csv(path) as stream:
        reader = csv.DictReader(stream)
        mapping = {canonical(key): key for key in (reader.fieldnames or [])}
        if not {'mmsi', 'basedatetime', 'vesselname'}.issubset(mapping):
            raise ValueError('AIS file lacks MMSI, timestamp or vessel name')
        lon_key = mapping.get('longitude') or mapping.get('lon')
        lat_key = mapping.get('latitude') or mapping.get('lat')
        if not lon_key or not lat_key:
            raise ValueError('AIS file lacks coordinates')
        for raw in reader:
            name = canonical(raw.get(mapping['vesselname']))
            imo = canonical(raw.get(mapping.get('imo', ''))).removeprefix('imo')
            indices = names.get(name, set()) | identifiers.get(imo, set())
            if not indices:
                continue
            mmsi = (raw.get(mapping['mmsi']) or '').strip()
            point = location(raw.get(lat_key), raw.get(lon_key))
            try:
                tick = timestamp(raw[mapping['basedatetime']])
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                continue
            if len(mmsi) != 9 or not mmsi.isdigit() or point is None or utc(tick)[:10] != day:
                continue
            for index in indices:
                case = cases[index]
                lat, lon = point
                distance = distance_m(lat, lon, case['lat'], case['lon'])
                if distance > DISTANCE_LIMIT_M:
                    continue
                time_delta = None
                if case['agency'] == 'NTSB':
                    time_delta = abs(tick - timestamp(case['reported_time']))
                    if time_delta > NTSB_TIME_LIMIT_SECONDS:
                        continue
                identity = 'reported identifier matches AIS IMO' if index in identifiers.get(imo, ()) else 'exact normalized vessel name'
                key = (index, mmsi)
                if key not in best or distance < best[key]['distance_m']:
                    best[key] = {'mmsi': mmsi, 'ais_vessel_name': raw.get(mapping['vesselname'], ''),
                                 'ais_time_utc': utc(tick), 'ais_latitude': lat, 'ais_longitude': lon,
                                 'distance_m': round(distance, 2), 'time_difference_seconds': time_delta,
                                 'identity_evidence': identity}
    result = []
    for index, case in enumerate(cases):
        matches = [value for (case_index, _), value in best.items() if case_index == index]
        if matches:
            result.append({**case, 'matches': sorted(matches, key=lambda x: (x['distance_m'], x['mmsi']))})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uscg-xlsx', type=Path)
    parser.add_argument('--ntsb-json', type=Path)
    parser.add_argument('--ais-file', type=Path, action='append', required=True,
                        help='Raw daily AIS file named ais-YYYY-MM-DD.csv[.zst/.gz]; repeat for each date')
    parser.add_argument('--report-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.uscg_xlsx and not args.ntsb_json:
        parser.error('Supply at least one official export')
    source_paths = {agency: path for agency, path in
                    [('USCG', args.uscg_xlsx), ('NTSB', args.ntsb_json)] if path}
    sources = {agency: {'file': path.name, 'sha256': sha256(path)}
               for agency, path in source_paths.items()}
    cases = list(read_uscg(args.uscg_xlsx)) if args.uscg_xlsx else []
    cases += list(read_ntsb(args.ntsb_json)) if args.ntsb_json else []
    for case in cases:
        case['source'] = sources[case['agency']]
    target = args.report_dir / 'official_incident_matches.json'
    existing = json.loads(target.read_text(encoding='utf-8')) if target.exists() else {'matches': [], 'processed_dates': []}
    matches = existing['matches']
    processed = set(existing.get('processed_dates', []))
    provenance = existing.get('provenance', {})
    for path in args.ais_file:
        date_match = re.fullmatch(r'ais-(\d{4}-\d{2}-\d{2})\.csv(?:\.zst|\.gz)?', path.name, re.I)
        if not date_match:
            raise ValueError(f'Cannot infer AIS date from {path.name}')
        day = date_match.group(1)
        datetime.fromisoformat(day)
        new = match_day(path, day, cases)
        ais_source = {'file': path.name, 'sha256': sha256(path)}
        for item in new:
            item['ais_source'] = ais_source
        matches = [item for item in matches if item['date'] != day or item['agency'] not in sources] + new
        provenance.setdefault(day, {}).update({agency: {'official_source': source, 'ais_source': ais_source}
                                               for agency, source in sources.items()})
        processed.add(day)
        print(f'{day}: {len(new)} possible official-incident matches', flush=True)
    payload = {'version': 1, 'matches': matches, 'processed_dates': sorted(processed),
               'provenance': provenance,
               'criteria': {'max_distance_m': DISTANCE_LIMIT_M, 'ntsb_max_time_seconds': NTSB_TIME_LIMIT_SECONDS,
                            'uscg_time_basis': 'reported timezone unspecified; same date only',
                            'identity': 'normalized exact vessel name or reported identifier matching AIS IMO'}}
    args.report_dir.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix('.json.part')
    temp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(target)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(f'Error: {error}', file=sys.stderr)
        sys.exit(1)
