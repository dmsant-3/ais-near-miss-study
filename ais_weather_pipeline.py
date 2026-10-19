#!/usr/bin/env python3
"""Download NOAA AIS, detect potential near misses, and join historical weather.

Python 3.10+. Keep count_ais_near_misses.py in this script's folder.
For .zst install zstandard (`python -m pip install zstandard`) or use recent
Node.js as supported by the detector. Everything else uses the standard library.

Example (New York Harbor, explicit illustrative study boundary):
  python ais_weather_pipeline.py --date 2026-05-05 --bbox -74.3 40.4 -73.7 40.9

Weather: Open-Meteo Historical Weather API, ERA5 reanalysis, hourly UTC,
nearest grid-cell selection (including ocean). These are modeled estimates,
not local station observations. Additional NOAA downloaders retrieve observed
visibility (GHCNh), waves (NDBC) and depth-specific currents (CO-OPS).
Screen: DCPA <= 926 m and 0 < TCPA <= 360 s; not a confirmed near-miss verdict.
One source day is processed; encounters at midnight can be incomplete.

Sources:
https://noaaocm.blob.core.windows.net/ais/csv2/csv2026/index.html
https://open-meteo.com/en/docs/historical-weather-api
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import io
import json
import math
from pathlib import Path
import re
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

import count_ais_near_misses as detector
import ais_environment as environment

INDEX = 'https://noaaocm.blob.core.windows.net/ais/csv2/csv2026/index.html'
WEATHER_API = 'https://archive-api.open-meteo.com/v1/archive'
WEATHER_VARS = ['temperature_2m','relative_humidity_2m','precipitation',
                'pressure_msl','cloud_cover','wind_speed_10m',
                'wind_direction_10m','wind_gusts_10m']
WEATHER_FIELDS = ['temperature_2m_c','relative_humidity_2m_percent',
                  'precipitation_previous_hour_mm','pressure_msl_hpa',
                  'cloud_cover_percent','wind_speed_10m_mps',
                  'wind_direction_10m_deg','wind_gusts_10m_mps']
MATCH_FIELDS = ['weather_match_status','weather_point_id','weather_utc',
                'weather_time_offset_seconds','weather_distance_km',
                'weather_grid_longitude','weather_grid_latitude',
                'weather_source','weather_model',*WEATHER_FIELDS]
USER_AGENT = 'AISWeatherResearch/1.0 (academic research; Python urllib)'
_CURL_NOTICE_SHOWN = False


def verified_curl_request(url, method):
    """Use the system TLS client when Python's certificate chain is unusable.

    Restricted to small metadata/weather responses. Certificate verification
    remains enabled: no --insecure, custom clock, or TLS verification bypass.
    """
    global _CURL_NOTICE_SHOWN
    if urlparse(url).hostname != 'archive-api.open-meteo.com':
        raise RuntimeError('Python TLS verification failed; repair its certificate store')
    executable = shutil.which('curl.exe') if sys.platform == 'win32' else shutil.which('curl')
    if not executable:
        raise RuntimeError('Python TLS verification failed and verified system curl is unavailable')
    if not _CURL_NOTICE_SHOWN:
        detector.log('Python certificate validation failed for weather; using system curl '
                     'with certificate verification enabled.')
        _CURL_NOTICE_SHOWN = True
    command = [executable,'--fail','--silent','--show-error','--max-time','90',
               '--user-agent',USER_AGENT]
    if method == 'HEAD':
        command.append('--head')
    command.append(url)
    result = subprocess.run(command,capture_output=True,timeout=100,check=False)
    if result.returncode:
        raise RuntimeError('Verified system HTTPS request failed: '+
                           result.stderr.decode(errors='replace').strip())
    class Response(io.BytesIO):
        pass
    response = Response(result.stdout)
    response.headers = {'Content-Length':str(len(result.stdout))}
    return response


def request(url, method='GET'):
    """Retry temporary HTTP/network failures, keeping TLS verification enabled."""
    for attempt in range(3):
        try:
            return urlopen(Request(url,method=method,headers={'User-Agent':USER_AGENT}),timeout=90)
        except HTTPError as error:
            if error.code not in (408,429,500,502,503,504) or attempt == 2:
                raise
        except URLError as error:
            if isinstance(error.reason,ssl.SSLCertVerificationError):
                return verified_curl_request(url,method)
            if attempt == 2:
                raise
        except TimeoutError:
            if attempt == 2:
                raise
        time.sleep(2**attempt)


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    tmp = path.with_suffix(path.suffix+'.part')
    tmp.write_text(json.dumps(value,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    tmp.replace(path)


def download(url, target):
    """Cache only completed downloads verified against their local manifest."""
    target.parent.mkdir(parents=True,exist_ok=True)
    manifest = target.with_suffix(target.suffix+'.download.json')
    if target.exists():
        if not manifest.exists():
            raise FileExistsError(f'Unverified cache file: {target}; use --ais-file for local AIS')
        info = json.loads(manifest.read_text(encoding='utf-8'))
        if info['url'] != url or info['sha256'] != sha256(target):
            raise ValueError(f'Cache URL/checksum mismatch: {target}')
        detector.log(f'Reusing verified download: {target.name}')
        return info
    part = target.with_suffix(target.suffix+'.part')
    with request(url) as response, part.open('wb') as out:
        expected = response.headers.get('Content-Length')
        total = 0
        next_report = 50*1024*1024
        digest = hashlib.sha256()
        for chunk in iter(lambda:response.read(1024*1024),b''):
            out.write(chunk)
            digest.update(chunk)
            total += len(chunk)
            if total >= next_report:
                detector.log(f'Downloaded {total/1024/1024:.0f} MiB: {target.name}')
                next_report += 50*1024*1024
        if expected is not None and total != int(expected):
            raise OSError(f'Incomplete download: expected {expected} bytes, received {total}')
        if total == 0:
            raise OSError(f'Empty download: {url}')
        info = dict(url=url,bytes=total,sha256=digest.hexdigest(),
                    downloaded_utc=datetime.now(timezone.utc).isoformat(),
                    etag=response.headers.get('ETag'),
                    last_modified=response.headers.get('Last-Modified'))
    part.replace(target)
    write_json(manifest,info)
    return info


class IndexLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = {}

    def handle_starttag(self, tag, attrs):
        if tag != 'a':
            return
        href = dict(attrs).get('href','')
        absolute = urljoin(INDEX,href)
        parsed = urlparse(absolute)
        match = re.fullmatch(r'ais-(\d{4}-\d{2}-\d{2})\.csv\.zst',Path(parsed.path).name)
        # Download only data files actually linked inside the requested NOAA folder.
        if (match and parsed.scheme == 'https' and
            parsed.netloc == urlparse(INDEX).netloc and
            parsed.path.startswith('/ais/csv2/csv2026/') and not parsed.query):
            self.links[match[1]] = absolute


def fetch_index(cache):
    # Refresh the listing each run: newly published dates should be visible.
    with request(INDEX) as response:
        content = response.read().decode('utf-8')
    cache.mkdir(parents=True,exist_ok=True)
    (cache/'noaa_index.html').write_text(content,encoding='utf-8')
    parser = IndexLinks()
    parser.feed(content)
    if not parser.links:
        raise ValueError('NOAA index contains no recognized daily .csv.zst links')
    return parser.links


def weather_points(bbox, spacing, maximum):
    w,s,e,n = bbox
    nx = max(1,math.ceil((e-w)/spacing))
    ny = max(1,math.ceil((n-s)/spacing))
    if nx*ny > maximum:
        raise ValueError(f'Area needs {nx*ny} weather requests (limit {maximum}); '
                         'narrow --bbox or increase --weather-grid-deg / --max-weather-points')
    return [(f'w{iy:03d}_{ix:03d}',w+(ix+0.5)*(e-w)/nx,s+(iy+0.5)*(n-s)/ny)
            for iy in range(ny) for ix in range(nx)]


def weather_url(day, lon, lat):
    # Include next-day 00:00 for nearest-hour matching near the end of the AIS day.
    return WEATHER_API+'?'+urlencode(dict(latitude=f'{lat:.6f}',longitude=f'{lon:.6f}',
        start_date=day.isoformat(),end_date=(day+timedelta(days=1)).isoformat(),
        hourly=','.join(WEATHER_VARS),models='era5',timezone='GMT',
        timeformat='unixtime',wind_speed_unit='ms',cell_selection='nearest',elevation='nan'))


def validate_weather(data):
    if data.get('error'):
        raise ValueError(f"Weather API: {data.get('reason')}")
    hourly = data.get('hourly',{})
    times = hourly.get('time',[])
    if not times:
        raise ValueError('Weather response contains no hourly timestamps')
    for variable in WEATHER_VARS:
        if len(hourly.get(variable,[])) != len(times):
            raise ValueError(f'Missing/misaligned weather array: {variable}')
    expected_units = dict(zip(WEATHER_VARS,['°C','%','mm','hPa','%','m/s','°','m/s']))
    for key,unit in expected_units.items():
        if data.get('hourly_units',{}).get(key) != unit:
            raise ValueError(f'Unexpected weather unit for {key}: '
                             f"{data.get('hourly_units',{}).get(key)}")
    if data.get('utc_offset_seconds') != 0:
        raise ValueError('Weather response must be UTC')
    for key in ('latitude','longitude'):
        if not isinstance(data.get(key),(int,float)) or not math.isfinite(data[key]):
            raise ValueError(f'Invalid weather grid {key}')
    return data


def get_weather(day, points, cache):
    series = []
    rows = []
    for point_id,lon,lat in points:
        url = weather_url(day,lon,lat)
        key = hashlib.sha256(url.encode()).hexdigest()[:20]
        path = cache/f'era5_{key}.json'
        info = download(url,path)
        data = validate_weather(json.loads(path.read_text(encoding='utf-8')))
        values = data['hourly']
        series.append(dict(id=point_id,longitude=data['longitude'],latitude=data['latitude'],
                           requested_longitude=lon,requested_latitude=lat,
                           hourly=values,url=url,cache_sha256=info['sha256']))
        for i,tick in enumerate(values['time']):
            rows.append(dict(weather_point_id=point_id,requested_longitude=lon,
                requested_latitude=lat,weather_grid_longitude=data['longitude'],
                weather_grid_latitude=data['latitude'],weather_utc=detector.utc(tick),
                weather_source='Open-Meteo / Copernicus ERA5',weather_model='era5',
                **{field:values[var][i] for var,field in zip(WEATHER_VARS,WEATHER_FIELDS)}))
        time.sleep(0.15)  # Serial requests with a small courtesy interval.
    return series,rows


def distance_km(lon1,lat1,lon2,lat2):
    p1,p2 = math.radians(lat1),math.radians(lat2)
    h = math.sin((p2-p1)/2)**2+math.cos(p1)*math.cos(p2)*math.sin(math.radians(lon2-lon1)/2)**2
    return 2*detector.EARTH_R/1000*math.asin(math.sqrt(min(1,max(0,h))))


def match_weather(event, series, max_km, max_seconds):
    tick = detector.timestamp(event['first_flag_utc'])
    lon,lat = float(event['first_flag_longitude']),float(event['first_flag_latitude'])
    result = dict.fromkeys(MATCH_FIELDS,'')
    if not series:
        result['weather_match_status'] = 'no_weather'
        return result
    nearest = min(series,key=lambda p:distance_km(lon,lat,p['longitude'],p['latitude']))
    km = distance_km(lon,lat,nearest['longitude'],nearest['latitude'])
    times = nearest['hourly']['time']
    i = min(range(len(times)),key=lambda i:abs(times[i]-tick))
    offset = times[i]-tick
    result.update(weather_point_id=nearest['id'],weather_utc=detector.utc(times[i]),
        weather_time_offset_seconds=round(offset,3),weather_distance_km=round(km,3),
        weather_grid_longitude=nearest['longitude'],weather_grid_latitude=nearest['latitude'],
        weather_source='Open-Meteo / Copernicus ERA5',weather_model='era5')
    if km > max_km:
        result['weather_match_status'] = 'too_far'
    elif abs(offset) > max_seconds:
        result['weather_match_status'] = 'time_gap'
    else:
        vals = {field:nearest['hourly'][var][i] for var,field in zip(WEATHER_VARS,WEATHER_FIELDS)}
        result.update(vals)
        result['weather_match_status'] = ('missing_values' if all(v is None for v in vals.values())
            else 'matched' if all(v is not None for v in vals.values()) else 'partially_missing')
    return result


def write_csv(path, rows, fields):
    with path.open('w',encoding='utf-8',newline='') as out:
        writer = csv.DictWriter(out,fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def run_detection(source,args,folder):
    """Reuse validated detector; filter first-flag locations to reporting bounds."""
    start = datetime.combine(args.date,datetime.min.time(),tzinfo=timezone.utc).timestamp()
    end = start+86400
    core = detector.parser().parse_args([str(source)])
    for key in ('dcpa_meters','tcpa_seconds','step_seconds','max_gap_seconds',
                'merge_gap_seconds','min_speed_knots','max_speed_knots','node'):
        setattr(core,key,getattr(args,key))
    w,s,e,n = args.bbox
    # Conservative latitude/longitude buffer for the maximum CPA search radius.
    reach = args.dcpa_meters+2*args.max_speed_knots*detector.KNOT*args.tcpa_seconds
    lat_buffer = math.degrees(reach/detector.EARTH_R)
    lon_buffer = lat_buffer/max(0.01,math.cos(math.radians(min(89,max(abs(s),abs(n))+lat_buffer))))
    if w-lon_buffer < -180 or e+lon_buffer > 180:
        raise ValueError('Buffered bbox crosses the dateline; split the study region')
    core.bbox = [w-lon_buffer,max(-89.999,s-lat_buffer),e+lon_buffer,min(89.999,n+lat_buffer)]
    db = sqlite3.connect(str(folder/'working.sqlite'))
    try:
        db.execute('PRAGMA journal_mode=OFF')
        db.execute('PRAGMA synchronous=OFF')
        db.execute('PRAGMA temp_store=FILE')
        detector.log('Reading AIS and preparing synchronized tracks...')
        quality = detector.ingest(db,core)
        outside = db.execute('SELECT COUNT(*) FROM reports WHERE t < ? OR t >= ?',
                             (start,end)).fetchone()[0]
        db.execute('DELETE FROM reports WHERE t < ? OR t >= ?',(start,end))
        quality['reports_outside_requested_day'] = outside
        quality['reports_in_requested_day'] = db.execute('SELECT COUNT(*) FROM reports').fetchone()[0]
        if not quality['reports_in_requested_day']:
            raise ValueError('No valid AIS reports in the buffered area on the requested day')
        quality.update(detector.synchronize(db,core))
        raw = folder/'buffered_events.csv'
        with raw.open('w',encoding='utf-8',newline='') as out:
            writer = csv.DictWriter(out,fieldnames=detector.EVENT_FIELDS)
            writer.writeheader()
            counts = detector.detect(db,core,writer)
        events = []
        with raw.open(encoding='utf-8',newline='') as inp:
            for event in csv.DictReader(inp):
                lon,lat = float(event['first_flag_longitude']),float(event['first_flag_latitude'])
                if w <= lon <= e and s <= lat <= n:
                    events.append(dict(event_id=f"{args.date}_{len(events)+1:06d}",**event))
        return events,quality,counts,core.bbox
    finally:
        db.close()


def parser():
    p = argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--date',type=date.fromisoformat,default=date(2026,5,5))
    p.add_argument('--bbox',nargs=4,type=float,metavar=('WEST','SOUTH','EAST','NORTH'))
    p.add_argument('--list-dates',action='store_true',help='List dates available in the NOAA index')
    p.add_argument('--check-sources',action='store_true',help='Verify index, AIS HEAD and one weather request; no AIS download/detection')
    p.add_argument('--check-environment',action='store_true',help='Download/check only NOAA visibility, waves and currents; no AIS processing')
    p.add_argument('--ais-file',type=Path,help='Use an existing local AIS file instead of downloading')
    p.add_argument('--cache-dir',type=Path,default=Path('ais_weather_cache'))
    p.add_argument('--output-dir',type=Path,default=Path('ais_weather_results'))
    p.add_argument('--work-dir',type=Path,help='Existing folder with space for temporary SQLite data')
    p.add_argument('--weather-grid-deg',type=float,default=0.25)
    p.add_argument('--max-weather-points',type=int,default=100)
    p.add_argument('--max-weather-distance-km',type=float,default=50)
    p.add_argument('--max-weather-time-seconds',type=float,default=1800)
    p.add_argument('--environment',nargs='*',choices=list(environment.VALUES),
                   default=list(environment.VALUES),
                   help='Observation sources to include (default: all); pass no values to disable')
    p.add_argument('--visibility-stations',nargs='+',help='GHCNh IDs; otherwise discover nearby airport stations')
    p.add_argument('--wave-stations',nargs='+',help='NDBC IDs; otherwise discover nearby active met stations')
    p.add_argument('--current-stations',nargs='+',help='CO-OPS IDs; otherwise discover active and historical stations')
    p.add_argument('--max-environment-stations',type=int,default=2,help='Auto-discovery candidate limit per source')
    p.add_argument('--max-visibility-distance-km',type=float,default=25)
    p.add_argument('--max-waves-distance-km',type=float,default=50)
    p.add_argument('--max-currents-distance-km',type=float,default=10)
    p.add_argument('--max-visibility-time-seconds',type=float,default=1800)
    p.add_argument('--max-waves-time-seconds',type=float,default=3600)
    p.add_argument('--max-currents-time-seconds',type=float,default=900)
    p.add_argument('--current-bin',type=int,help='Explicit current-meter bin; default: shallowest QC-accepted metadata bin')
    p.add_argument('--require-environment',action='store_true',help='Stop if any enabled observation source has no usable records')
    p.add_argument('--dcpa-meters',type=float,default=926)
    p.add_argument('--tcpa-seconds',type=float,default=360)
    p.add_argument('--step-seconds',type=int,default=30)
    p.add_argument('--max-gap-seconds',type=float,default=120)
    p.add_argument('--merge-gap-seconds',type=float,default=120)
    p.add_argument('--min-speed-knots',type=float,default=1)
    p.add_argument('--max-speed-knots',type=float,default=99.9)
    p.add_argument('--node',help='Path to Node.js if zstandard is not installed')
    return p


def validate_args(args):
    if not args.bbox:
        raise ValueError('--bbox WEST SOUTH EAST NORTH is required to define the weather study area')
    w,s,e,n = args.bbox
    if not (-180 <= w < e <= 180 and -85 <= s < n <= 85):
        raise ValueError('Invalid bbox; use a local area between 85 S and 85 N without crossing the dateline')
    for key in ('weather_grid_deg','max_weather_points','max_weather_distance_km',
                'max_weather_time_seconds','dcpa_meters','tcpa_seconds',
                'step_seconds','max_gap_seconds','merge_gap_seconds','max_speed_knots',
                'max_environment_stations','max_visibility_distance_km','max_waves_distance_km',
                'max_currents_distance_km','max_visibility_time_seconds','max_waves_time_seconds',
                'max_currents_time_seconds'):
        if not math.isfinite(getattr(args,key)) or getattr(args,key) <= 0:
            raise ValueError(f'{key} must be positive and finite')
    if not math.isfinite(args.min_speed_knots) or not 0 <= args.min_speed_knots <= args.max_speed_knots:
        raise ValueError('Invalid min-speed-knots')
    if args.merge_gap_seconds < args.step_seconds:
        raise ValueError('merge-gap-seconds must be at least step-seconds')
    if args.current_bin is not None and args.current_bin <= 0:
        raise ValueError('current-bin must be a positive integer')
    for key in ('visibility_stations','wave_stations','current_stations'):
        if any(not re.fullmatch(r'[A-Za-z0-9]{3,12}',s) for s in (getattr(args,key) or [])):
            raise ValueError(f'Invalid station identifier in {key}')
    if args.date.year != 2026 and not args.ais_file and not args.check_environment:
        raise ValueError('This NOAA index contains 2026; use --ais-file for other years')


def main(argv=None):
    args = parser().parse_args(argv)
    if args.list_dates:
        print('\n'.join(sorted(fetch_index(args.cache_dir))))
        return
    validate_args(args)
    if args.check_environment:
        observations,report = environment.EnvironmentalDownloader(args,download).run()
        args.cache_dir.mkdir(parents=True,exist_ok=True)
        write_json(args.cache_dir/f'environment_check_{args.date}.json',report)
        write_csv(args.cache_dir/f'environment_observations_{args.date}.csv',observations,environment.OBS_FIELDS)
        print(json.dumps(report['sources'],indent=2))
        if args.require_environment and not report['coverage_complete']:
            raise ValueError('One or more enabled observation sources have no usable records; see source-check report')
        return report
    points = weather_points(args.bbox,args.weather_grid_deg,args.max_weather_points)
    url = None
    if not args.ais_file or args.check_sources:
        links = fetch_index(args.cache_dir)
        url = links.get(args.date.isoformat())
        if not url:
            raise ValueError(f'{args.date} is not in the NOAA index; use --list-dates')
    if args.check_sources:
        with request(url,method='HEAD') as response:
            print(f"AIS available: {url} ({response.headers.get('Content-Length','unknown')} bytes)")
        series,rows = get_weather(args.date,points[:1],args.cache_dir/'weather')
        print(f'Weather verified: {len(rows)} hourly rows; model ERA5; '
              f"grid {series[0]['latitude']}, {series[0]['longitude']}")
        observations,report = environment.EnvironmentalDownloader(args,download).run()
        write_json(args.cache_dir/f'environment_check_{args.date}.json',report)
        print('NOAA observations: '+json.dumps({k:v['status'] for k,v in report['sources'].items()}))
        if args.require_environment and not report['coverage_complete']:
            raise ValueError('One or more enabled observation sources have no usable records')
        return
    # A run folder is never overwritten; choose a different folder to rerun.
    if args.output_dir.exists():
        raise FileExistsError(f'Output directory exists: {args.output_dir}; select a new --output-dir')
    if args.ais_file:
        source = args.ais_file.resolve(strict=True)
        provenance = dict(local_path=str(source),sha256=sha256(source),bytes=source.stat().st_size)
    else:
        source = args.cache_dir/'ais'/Path(urlparse(url).path).name
        detector.log(f'Downloading NOAA AIS: {url}')
        provenance = download(url,source)
    detector.log(f'Downloading historical weather for {len(points)} locations...')
    series,weather_rows = get_weather(args.date,points,args.cache_dir/'weather')
    observations,environment_report = environment.EnvironmentalDownloader(args,download).run()
    if args.require_environment and not environment_report['coverage_complete']:
        args.cache_dir.mkdir(parents=True,exist_ok=True)
        write_json(args.cache_dir/f'environment_failed_{args.date}.json',environment_report)
        raise ValueError('Required environmental coverage missing; see environment_failed report in cache')
    with tempfile.TemporaryDirectory(prefix='ais_weather_',dir=args.work_dir) as temp:
        folder = Path(temp)
        events,quality,buffered_counts,buffered_bbox = run_detection(source,args,folder)
        statuses = {}
        environment_statuses = {kind:{} for kind in environment.VALUES}
        for event in events:
            match = match_weather(event,series,args.max_weather_distance_km,args.max_weather_time_seconds)
            event.update(match)
            statuses[match['weather_match_status']] = statuses.get(match['weather_match_status'],0)+1
            matched_environment = environment.match_observations(event,observations,args,environment_report)
            event.update(matched_environment)
            for kind in environment.VALUES:
                label = matched_environment[kind+'_match_status']
                environment_statuses[kind][label] = environment_statuses[kind].get(label,0)+1
        args.output_dir.mkdir(parents=True)
        write_csv(args.output_dir/'events_with_weather.csv',events,
                  ['event_id',*detector.EVENT_FIELDS,*MATCH_FIELDS,*environment.match_fields()])
        write_csv(args.output_dir/'environment_observations.csv',observations,environment.OBS_FIELDS)
        write_json(args.output_dir/'environment_sources.json',environment_report)
        weather_fields = ['weather_point_id','requested_longitude','requested_latitude',
                          'weather_grid_longitude','weather_grid_latitude','weather_utc',
                          'weather_source','weather_model',*WEATHER_FIELDS]
        write_csv(args.output_dir/'weather_hourly.csv',weather_rows,weather_fields)
        environmental_gaps = (not environment_report['coverage_complete'] or environment_report['errors'] or
            any(label != 'matched' and count for kind,counts in environment_statuses.items()
                if kind in args.environment for label,count in counts.items()))
        summary = dict(status='complete_with_environment_gaps' if environmental_gaps else 'complete',
            potential_near_miss_events=len(events),environment_match_counts=environment_statuses,
            environmental_source_statuses=environment_report['sources'],
            weather_match_counts=statuses,ais_source=provenance,noaa_index=INDEX,
            parameters={key:str(value) if isinstance(value,(Path,date)) else value
                        for key,value in vars(args).items()},
            buffered_bbox=buffered_bbox,ais_quality=quality,buffered_detection_counts=buffered_counts,
            weather_requests=[{key:value for key,value in p.items() if key != 'hourly'} for p in series],
            attribution='Weather: Open-Meteo and Copernicus Climate Change Service ERA5 (CC BY 4.0).',
            weather_documentation='https://open-meteo.com/en/docs/historical-weather-api',
            limitations=[
                'Counts are CPA/TCPA screened candidates, not confirmed hazardous encounters.',
                'ERA5 hourly reanalysis is modeled, coarsely resolved weather, not a local observation.',
                'Observed visibility, waves and currents are matched independently; '
                'missing or failed sources remain explicit gaps, never fabricated values.',
                'Station proximity does not establish local representativeness across channels or coastlines.',
                'Current-depth metadata is retained but not historically verified; '
                'the measured current is never added to AIS ground velocity.',
                'Precipitation is a preceding-hour accumulation; other variables follow API definitions.',
                'Nearest-hour retrospective matching may select weather after the flag time.',
                'One AIS day only: midnight encounters and data gaps may be incomplete.',
                'Event location is the midpoint at the first qualifying flag.',
                'Weather table includes all requested points/hours, but no vessel-exposure '
                'denominator or causal weather-risk model is computed.'
            ])
        write_json(args.output_dir/'summary.json',summary)
    print(f'Potential near-miss events in study area: {len(events):,}')
    print(f'Weather matches: {statuses}')
    print(f'Observed environment matches: {environment_statuses}')
    print(f'Results: {args.output_dir.resolve()}')
    return summary


if __name__ == '__main__':
    try:
        main()
    except (OSError,ValueError,RuntimeError,sqlite3.Error,subprocess.TimeoutExpired) as error:
        detector.log(f'Error: {error}')
        sys.exit(1)
