"""NOAA visibility, wave and current observations for ais_weather_pipeline.

Standard library only. HTTP/download functions are injected by the pipeline.
Documentation:
https://www.ncei.noaa.gov/oa/global-historical-climatology-network/hourly/doc/ghcnh_DOCUMENTATION.pdf
https://www.ndbc.noaa.gov/measdes.shtml
https://api.tidesandcurrents.noaa.gov/api/prod/
https://api.tidesandcurrents.noaa.gov/mdapi/prod/
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
import math
import re
import xml.etree.ElementTree as ET
from urllib.error import HTTPError
from urllib.parse import urlencode

import count_ais_near_misses as detector

GHCN = 'https://www.ncei.noaa.gov/oa/global-historical-climatology-network/hourly'
NDBC = 'https://www.ndbc.noaa.gov'
MDAPI = 'https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi'
DATA_API = 'https://api.tidesandcurrents.noaa.gov/api/prod/datagetter'
VALUES = {
    'visibility':['visibility_m','visibility_measurement_code'],
    'waves':['significant_wave_height_m','wave_period_s','average_wave_period_s','wave_direction_deg'],
    'currents':['current_speed_mps','current_direction_deg','current_east_mps','current_north_mps',
                'current_bin','current_depth_m','current_depth_metadata_status'],
}
COMMON = ['kind','station_id','station_name','timestamp_utc','longitude','latitude',
          'source','source_url','source_sha256','quality','usable','position_metadata_status']
OBS_FIELDS = COMMON+list(dict.fromkeys(v for fields in VALUES.values() for v in fields))
MATCH_META = ['match_status','station_id','station_name','utc','distance_km','time_offset_seconds',
              'longitude','latitude','source','source_url','quality','position_metadata_status']


def match_fields():
    return [field for kind in VALUES for field in
            [*(f'{kind}_{name}' for name in MATCH_META),*VALUES[kind]]]


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError,ValueError):
        return None


def distance_km(lon1,lat1,lon2,lat2):
    a,b = math.radians(lat1),math.radians(lat2)
    h = math.sin((b-a)/2)**2+math.cos(a)*math.cos(b)*math.sin(math.radians(lon2-lon1)/2)**2
    return 2*detector.EARTH_R/1000*math.asin(math.sqrt(min(1,max(0,h))))


def observation(kind, station, tick, url, checksum, **values):
    return dict(kind=kind,station_id=station['id'],station_name=station.get('name',''),
        timestamp_utc=detector.utc(tick),longitude=station['longitude'],latitude=station['latitude'],
        source={'visibility':'NOAA NCEI GHCNh','waves':'NOAA NDBC','currents':'NOAA CO-OPS'}[kind],
        source_url=url,source_sha256=checksum,quality='',usable=False,
        position_metadata_status=station.get('position_metadata_status','observation_coordinates'),
        **values)


def visibility_quality(code, source):
    """Source-specific legacy codes, GHCNh manual v1.1.0 Table 3.

    Unknown nonblank codes are rejected rather than interpreted as good.
    Blank quality is retained explicitly as unchecked, not labeled QC-passed.
    """
    code,source = code.strip(),source.strip()
    if not code:
        return True,'quality_not_provided'
    if source in {'313','314','315','322','335','343','344','346'}:
        if code in {'0','1','4','5','9'}:
            return True,f'source_{source}_qc_{code}'
    elif source in {'220','221','222','223','347','348'}:
        if code in {'0','1','4'}:
            return True,f'source_{source}_qc_{code}'
    return False,f'rejected_source_{source}_qc_{code}'


def parse_visibility(stream, station, start, end, url='', checksum=''):
    reader = csv.DictReader(stream,delimiter='|')
    required = {'DATE','LATITUDE','LONGITUDE','visibility','visibility_Quality_Code',
                'visibility_Source_Code','visibility_Measurement_Code'}
    if not required.issubset(reader.fieldnames or []):
        raise ValueError('Unrecognized GHCNh PSV schema')
    rows = []
    for raw in reader:
        try:
            tick = detector.timestamp(raw['DATE'])
            if not start <= tick < end:
                continue
            km = number(raw['visibility'])
            lat,lon = number(raw['LATITUDE']),number(raw['LONGITUDE'])
            if lat is None or lon is None or not (-90 < lat < 90 and -180 <= lon <= 180):
                continue
            local = dict(station,latitude=lat,longitude=lon,
                         position_metadata_status='observation_coordinates')
            row = observation('visibility',local,tick,url,checksum,
                visibility_m=km*1000 if km is not None and km >= 0 else None,
                visibility_measurement_code=raw['visibility_Measurement_Code'].strip())
            ok,qc = visibility_quality(raw['visibility_Quality_Code'],raw['visibility_Source_Code'])
            row['usable'] = ok and row['visibility_m'] is not None
            row['quality'] = qc if row['visibility_m'] is not None else 'missing_visibility'
            rows.append(row)
        except (ValueError,TypeError,KeyError):
            continue
    return rows


def parse_waves(text,station,start,end,url='',checksum=''):
    header = None
    rows = []
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] in ('#YY','#YYYY'):
            header = [v.lstrip('#') for v in fields]
            continue
        if line.startswith('#'):
            continue
        if header is None or 'WVHT' not in header:
            raise ValueError('Unrecognized NDBC standard meteorological schema')
        raw = dict(zip(header,fields))
        try:
            year = int(raw.get('YY',raw.get('YYYY','')))
            if year < 100:
                year += 1900 if year >= 70 else 2000
            tick = datetime(year,int(raw['MM']),int(raw['DD']),int(raw['hh']),
                            int(raw.get('mm',0)),tzinfo=timezone.utc).timestamp()
            if not start <= tick < end:
                continue
            vals = {}
            for key,field in [('WVHT','significant_wave_height_m'),('DPD','wave_period_s'),
                              ('APD','average_wave_period_s'),('MWD','wave_direction_deg')]:
                value = number(raw.get(key))
                sentinel = 999 if key == 'MWD' else 99
                vals[field] = value if value is not None and 0 <= value < sentinel else None
            row = observation('waves',station,tick,url,checksum,**vals)
            row['usable'] = vals['significant_wave_height_m'] is not None
            row['quality'] = ('archive_no_per_value_qc_flags' if row['usable'] else 'missing_wave_height')
            rows.append(row)
        except (ValueError,TypeError,KeyError):
            continue
    if header is None:
        raise ValueError('NDBC response did not contain a data header')
    return rows


def choose_bin(metadata, requested=None):
    if metadata.get('units') != 'meters':
        raise ValueError('Current depth metadata must be in meters')
    bins = metadata.get('bins') or metadata.get('binList') or []
    valid = []
    for entry in bins:
        depth = number(entry.get('depth'))
        bin_id = entry.get('num',entry.get('bin'))
        if depth is not None and depth >= 0 and bin_id is not None and str(entry.get('qc_flag')) == '1':
            valid.append((int(bin_id),depth))
    if requested is not None:
        valid = [b for b in valid if b[0] == requested]
    if not valid:
        raise ValueError('No usable current bin/depth metadata for requested station/bin')
    # The shallowest QC-accepted bin is explicit in the data request and output.
    return min(valid,key=lambda b:b[1])


def parse_currents(data,station,start,end,bin_id,depth,url='',checksum=''):
    if data.get('error'):
        raise ValueError('CO-OPS: '+str(data['error']))
    rows = []
    for raw in data.get('data',[]):
        try:
            tick = detector.timestamp(raw['t'])
            if not start <= tick < end or int(raw.get('b',bin_id)) != bin_id:
                continue
            speed = number(raw.get('s'))
            direction = number(raw.get('d'))
            speed = speed/100 if speed is not None and speed >= 0 else None
            if speed == 0 and direction is None:
                direction = 0.0
            direction = direction%360 if direction is not None and 0 <= direction <= 360 else None
            flags = str(raw.get('f','')).strip()
            accepted_flags = not flags or all(v.strip() == '0' for v in flags.split(','))
            valid = speed is not None and direction is not None and accepted_flags
            row = observation('currents',station,tick,url,checksum,
                current_speed_mps=speed,current_direction_deg=direction,
                current_east_mps=speed*math.sin(math.radians(direction)) if valid else None,
                current_north_mps=speed*math.cos(math.radians(direction)) if valid else None,
                current_bin=bin_id,current_depth_m=depth,
                current_depth_metadata_status='catalog_depth_not_historically_verified')
            row['usable'] = valid
            row['quality'] = ('flags_'+flags if flags else 'no_per_value_qc_flags_in_response')
            if not valid:
                row['quality'] = 'rejected_or_missing_'+row['quality']
            rows.append(row)
        except (ValueError,TypeError,KeyError):
            continue
    return rows


class EnvironmentalDownloader:
    def __init__(self,args,download):
        self.args = args
        self.download = download
        self.requests = []
        self.errors = []
        self.selected = {}
        midnight = datetime.combine(args.date,datetime.min.time(),tzinfo=timezone.utc)
        self.start = (midnight-timedelta(days=1)).timestamp()
        self.end = (midnight+timedelta(days=2)).timestamp()

    def fetch(self,url):
        # Daily snapshots refresh changing catalogs/current-year observations.
        snapshot = datetime.now(timezone.utc).date().isoformat()
        key = hashlib.sha256(url.encode()).hexdigest()[:24]
        path = self.args.cache_dir/'environment'/snapshot/key
        info = self.download(url,path)
        self.requests.append(info)
        return path,info['sha256']

    def json(self,url):
        path,_ = self.fetch(url)
        return json.loads(path.read_text(encoding='utf-8'))

    def visibility_stations(self):
        url = GHCN+'/doc/ghcnh-station-list.csv'
        path,_ = self.fetch(url)
        with path.open(encoding='utf-8-sig',newline='') as stream:
            return [dict(id=r['GHCN_ID'],name=r['NAME'],latitude=float(r['LATITUDE']),
                         longitude=float(r['LONGITUDE']),airport=bool(r.get('ICAO','').strip()))
                    for r in csv.DictReader(stream)]

    def wave_stations(self):
        path,_ = self.fetch(NDBC+'/activestations.xml')
        return [dict(id=r.attrib['id'],name=r.attrib.get('name',''),
                     latitude=float(r.attrib['lat']),longitude=float(r.attrib['lon']),
                     station_type=r.attrib.get('type',''),
                     position_metadata_status='active_catalog_not_historically_verified')
                for r in ET.parse(path).getroot().findall('station') if r.attrib.get('met') == 'y']

    def current_stations(self):
        stations = {}
        for kind in ('historiccurrents','currents'):
            data = self.json(MDAPI+'/stations.json?'+urlencode(dict(type=kind,units='metric')))
            for r in data.get('stations',[]):
                deployed = detector.timestamp(r['deployed']) if r.get('deployed') else -math.inf
                retrieved = detector.timestamp(r['retrieved']) if r.get('retrieved') else math.inf
                # Avoid exhausting the candidate limit on stations not deployed
                # during the requested window. Explicit overrides remain possible.
                if not self.args.current_stations and (deployed >= self.end or retrieved < self.start):
                    continue
                stations[r['id']] = dict(id=r['id'],name=r.get('name',''),latitude=float(r['lat']),
                    longitude=float(r['lng']),position_metadata_status='deployment_metadata')
        return list(stations.values())

    def select(self,kind,catalog):
        explicit = getattr(self.args,{'visibility':'visibility_stations','waves':'wave_stations',
                                    'currents':'current_stations'}[kind])
        if explicit:
            by_id = {s['id'].lower():s for s in catalog}
            missing = [i for i in explicit if i.lower() not in by_id]
            if missing:
                raise ValueError(f'{kind}: station IDs absent from catalog: {missing}')
            return [by_id[i.lower()] for i in explicit]
        w,s,e,n = self.args.bbox
        def box_distance(station):
            lon,lat = station['longitude'],station['latitude']
            return distance_km(lon,lat,min(e,max(w,lon)),min(n,max(s,lat)))
        candidates = [r for r in catalog if box_distance(r) <= getattr(self.args,f'max_{kind}_distance_km')]
        if kind == 'visibility':
            candidates = [r for r in candidates if r['airport']]
        # Nearby tide/weather piers often have no wave sensor. Prioritize
        # meteorological buoys over fixed stations within the allowed radius.
        candidates.sort(key=lambda r:(kind == 'waves' and r.get('station_type') != 'buoy',
            box_distance(r),distance_km(r['longitude'],r['latitude'],(w+e)/2,(s+n)/2)))
        return candidates[:self.args.max_environment_stations]

    def visibility(self,station):
        rows = []
        for year in range(datetime.fromtimestamp(self.start,timezone.utc).year,
                          datetime.fromtimestamp(self.end-1,timezone.utc).year+1):
            url = f"{GHCN}/access/by-year/{year}/psv/GHCNh_{station['id']}_{year}.psv"
            try:
                path,checksum = self.fetch(url)
                with path.open(encoding='utf-8-sig',newline='') as stream:
                    rows.extend(parse_visibility(stream,station,self.start,self.end,url,checksum))
            except HTTPError as error:
                if error.code != 404:
                    raise
                self.errors.append(dict(kind='visibility',station_id=station['id'],
                                        error=f'No GHCNh station/year file for {year}'))
        return rows

    def waves(self,station):
        rows = []
        days = [datetime.fromtimestamp(self.start,timezone.utc)+timedelta(days=i) for i in range(3)]
        annual_years = set()
        for year in sorted({d.year for d in days}):
            url = f"{NDBC}/data/historical/stdmet/{station['id']}h{year}.txt.gz"
            try:
                path,checksum = self.fetch(url)
                text = gzip.decompress(path.read_bytes()).decode('utf-8')
                rows.extend(parse_waves(text,station,self.start,self.end,url,checksum))
                annual_years.add(year)
            except HTTPError as error:
                if error.code != 404:
                    raise
        for year,month in sorted({(d.year,d.month) for d in days if d.year not in annual_years}):
            mon = datetime(year,month,1).strftime('%b')
            # Monthly compressed archives, then current-month uncompressed file.
            urls = [f"{NDBC}/data/stdmet/{mon}/{station['id']}{month}{year}.txt.gz",
                    f"{NDBC}/data/stdmet/{mon}/{station['id']}.txt"]
            found = False
            for url in urls:
                try:
                    path,checksum = self.fetch(url)
                    data = path.read_bytes()
                    text = (gzip.decompress(data) if data[:2] == b'\x1f\x8b' else data).decode('utf-8')
                    rows.extend(parse_waves(text,station,self.start,self.end,url,checksum))
                    found = True
                    break
                except HTTPError as error:
                    if error.code != 404:
                        raise
            if not found:
                self.errors.append(dict(kind='waves',station_id=station['id'],
                    error=f'No monthly wave archive for {year}-{month:02d}'))
        return rows

    def currents(self,station):
        deploy = self.json(f"{MDAPI}/stations/{station['id']}/deployments.json?units=metric")
        periods = []
        for d in deploy.get('deployments',[]):
            begin = detector.timestamp(d['deployed']) if d.get('deployed') else -math.inf
            end = detector.timestamp(d['retrieved']) if d.get('retrieved') else math.inf
            if begin < self.end and end >= self.start:
                periods.append((begin,end,d))
        if not periods:
            return []
        metadata = self.json(f"{MDAPI}/stations/{station['id']}/bins.json?units=metric")
        bin_id,depth = choose_bin(metadata,self.args.current_bin)
        params = dict(begin_date=datetime.fromtimestamp(self.start,timezone.utc).strftime('%Y%m%d'),
            end_date=datetime.fromtimestamp(self.end-1,timezone.utc).strftime('%Y%m%d'),
            station=station['id'],product='currents',time_zone='gmt',units='metric',format='json',
            bin=bin_id,application='AISWeatherResearch')
        url = DATA_API+'?'+urlencode(params)
        path,checksum = self.fetch(url)
        data = json.loads(path.read_text(encoding='utf-8'))
        # Assign actual deployment coordinates at each observation time.
        rows = parse_currents(data,station,self.start,self.end,bin_id,depth,url,checksum)
        for row in rows:
            tick = detector.timestamp(row['timestamp_utc'])
            relevant = [p for p in periods if p[0] <= tick <= p[1]]
            if relevant:
                d = max(relevant,key=lambda p:p[0])[2]
                row.update(latitude=float(d['lat']),longitude=float(d['lng']))
            else:
                row['usable'] = False
                row['quality'] = 'outside_known_deployment'
        return rows

    def run(self):
        observations = []
        report = dict(sources={},errors=self.errors,requests=self.requests)
        for kind,catalog_fn,read_fn in [('visibility',self.visibility_stations,self.visibility),
                                      ('waves',self.wave_stations,self.waves),
                                      ('currents',self.current_stations,self.currents)]:
            if kind not in self.args.environment:
                report['sources'][kind] = dict(status='disabled',stations=[],observations=0,usable=0)
                continue
            rows = []
            selected = []
            try:
                selected = self.select(kind,catalog_fn())
                for station in selected:
                    try:
                        detector.log(f"Downloading {kind}: {station['id']} ({station['name']})")
                        rows.extend(read_fn(station))
                    except (OSError,ValueError,RuntimeError,ET.ParseError) as error:
                        self.errors.append(dict(kind=kind,station_id=station['id'],error=str(error)))
            except (OSError,ValueError,RuntimeError,ET.ParseError) as error:
                self.errors.append(dict(kind=kind,station_id='',error=str(error)))
            # Deterministic de-duplication, preferring the first usable record.
            unique = {}
            for row in rows:
                key = (row['station_id'],row['timestamp_utc'])
                if key not in unique or (row['usable'] and not unique[key]['usable']):
                    unique[key] = row
            rows = list(unique.values())
            usable_window = sum(bool(r['usable']) for r in rows)
            usable = sum(bool(r['usable']) and self.start+86400 <=
                         detector.timestamp(r['timestamp_utc']) < self.start+172800 for r in rows)
            errors = [e for e in self.errors if e['kind'] == kind]
            status = ('available_with_errors' if errors else 'available') if usable else (
                      'source_error' if errors else 'no_usable_observations')
            report['sources'][kind] = dict(status=status,stations=selected,
                                          observations=len(rows),usable=usable,usable_in_padded_window=usable_window)
            observations.extend(rows)
        report['coverage_complete'] = all(s['usable'] > 0 for k,s in report['sources'].items()
                                           if k in self.args.environment)
        for error in self.errors:
            detector.log(f"Environmental gap [{error['kind']} {error['station_id']}]: {error['error']}")
        return observations,report


def match_observations(event,observations,args,report):
    """Nearest usable station with a record inside BOTH matching tolerances.

    No time interpolation: avoid interpolating visibility/fog or smearing tidal
    reversals. Matching each source independently preserves its own provenance.
    """
    result = dict.fromkeys(match_fields(),'')
    tick = detector.timestamp(event['first_flag_utc'])
    lon,lat = float(event['first_flag_longitude']),float(event['first_flag_latitude'])
    for kind,fields in VALUES.items():
        prefix = kind+'_'
        source_status = report['sources'][kind]['status']
        rows = [r for r in observations if r['kind'] == kind and r['usable']]
        if not rows:
            result[prefix+'match_status'] = ('no_usable_observations'
                if source_status.startswith('available') else source_status)
            continue
        candidates = [(distance_km(lon,lat,r['longitude'],r['latitude']),
                       detector.timestamp(r['timestamp_utc'])-tick,r) for r in rows]
        max_km = getattr(args,f'max_{kind}_distance_km')
        max_seconds = getattr(args,f'max_{kind}_time_seconds')
        eligible = [c for c in candidates if c[0] <= max_km and abs(c[1]) <= max_seconds]
        km,offset,row = min(eligible or candidates,key=lambda c:(c[0],abs(c[1]),c[2]['station_id']))
        for key,value in dict(station_id=row['station_id'],station_name=row['station_name'],
            utc=row['timestamp_utc'],distance_km=round(km,3),time_offset_seconds=round(offset,3),
            longitude=row['longitude'],latitude=row['latitude'],source=row['source'],
            source_url=row['source_url'],quality=row['quality'],
            position_metadata_status=row['position_metadata_status']).items():
            result[prefix+key] = value
        if not eligible:
            result[prefix+'match_status'] = 'too_far' if km > max_km else 'time_gap'
            continue
        for field in fields:
            result[field] = row.get(field)
        result[prefix+'match_status'] = 'matched'
    return result
