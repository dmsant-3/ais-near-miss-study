#!/usr/bin/env python3
"""Count distinct AIS potential-conflict events, not confirmed near misses.

Default screen: DCPA <= 926 m and 0 < TCPA <= 360 s, sampled every 30 s.
Positions and velocity components are linearly interpolated between reports
at most 120 s apart. No track extrapolation. Events for the same MMSI pair
are merged if successive flags are at most 120 s apart.

Usage (Python 3.10+):
  python count_ais_near_misses.py "path/to/ais.csv.zst"
  python count_ais_near_misses.py "path/to/ais.csv.zst" --bbox -75 39 -72 42

CSV, CSV.GZ and CSV.ZST supported. Zstandard needs `pip install zstandard`
or Node.js 22.15+/23.8+ with built-in Zstandard support (use --node PATH).
Otherwise standard library only. Temporary SQLite storage can require several
times the compressed input size. The entire source is read even with --bbox.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
from datetime import datetime, timezone
import gzip
import io
import itertools
import json
import math
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

EARTH_R = 6371008.8
KNOT = 1852 / 3600
NODE_READER = """const fs=require('node:fs'),z=require('node:zlib');
const f=fs.createReadStream(process.argv[1]),d=z.createZstdDecompress();
for(const s of [f,d])s.on('error',e=>{console.error(e.message);process.exit(1)});
// A caller may intentionally stop after a sample. Treat that closed pipe as
// normal, while still surfacing source-file and decompressor errors.
process.stdout.on('error',e=>process.exit(e.code==='EPIPE'?0:1));
f.pipe(d).pipe(process.stdout);"""


def log(message):
    print(message, file=sys.stderr, flush=True)


@contextlib.contextmanager
def open_csv(path, node=None):
    """Stream decompression without writing an uncompressed copy."""
    suffix = path.suffix.lower()
    if suffix == '.zst':
        try:
            import zstandard
        except ImportError:
            bundled = (Path.home() / '.cache/codex-runtimes/'
                       'codex-primary-runtime/dependencies/node/bin/node.exe')
            executable = node or shutil.which('node')
            if not executable and bundled.is_file():
                executable = str(bundled)
            if not executable:
                raise RuntimeError('ZST input requires: pip install zstandard; '
                                   'or supply --node PATH to a recent Node.js.')
            # Capture errors on disk to avoid pipe deadlocks; no shell execution.
            with tempfile.TemporaryFile() as errors:
                proc = subprocess.Popen([executable, '-e', NODE_READER, str(path)],
                                        stdout=subprocess.PIPE, stderr=errors)
                stream = io.TextIOWrapper(proc.stdout, encoding='utf-8-sig', newline='')
                completed = False
                try:
                    yield stream
                    completed = True
                finally:
                    stream.close()
                    if not completed:
                        proc.terminate()
                    code = proc.wait()
                if code:
                    errors.seek(0)
                    raise RuntimeError('Zstandard decompression failed: ' +
                                       errors.read().decode(errors='replace'))
        else:
            with path.open('rb') as raw:
                with zstandard.ZstdDecompressor().stream_reader(raw) as reader:
                    with io.TextIOWrapper(reader, encoding='utf-8-sig', newline='') as stream:
                        yield stream
    elif suffix == '.gz':
        with gzip.open(path, 'rt', encoding='utf-8-sig', newline='') as stream:
            yield stream
    else:
        with path.open(encoding='utf-8-sig', newline='') as stream:
            yield stream


def timestamp(value):
    dt = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    # NOAA naive timestamps are UTC. Other sources must be normalized first.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def velocity(sog, cog):
    angle = math.radians(cog)
    return sog * KNOT * math.sin(angle), sog * KNOT * math.cos(angle)


def ecef(lon, lat):
    lo, la = math.radians(lon), math.radians(lat)
    return (EARTH_R * math.cos(la) * math.cos(lo),
            EARTH_R * math.cos(la) * math.sin(lo), EARTH_R * math.sin(la))


def relative_cpa(a, b):
    """Inputs (lon,lat,east_mps,north_mps); output current range,DCPA,TCPA.

    Use a local east/north frame centered at the pair's mean latitude, avoiding
    one projection across a nationwide dataset. Intended for short encounters.
    """
    dlon = (b[0] - a[0] + 180) % 360 - 180
    x = EARTH_R * math.radians(dlon) * math.cos(math.radians((a[1]+b[1])/2))
    y = EARTH_R * math.radians(b[1]-a[1])
    vx, vy = b[2]-a[2], b[3]-a[3]
    vv = vx*vx + vy*vy
    distance = math.hypot(x, y)
    if vv < 1e-8:
        return distance, distance, None
    tcpa = -(x*vx + y*vy) / vv
    return distance, math.hypot(x+vx*tcpa, y+vy*tcpa), tcpa


def ingest(db, args):
    db.execute('CREATE TABLE reports (m TEXT,t REAL,lon REAL,lat REAL,ve REAL,vn REAL, '
               'PRIMARY KEY(m,t)) WITHOUT ROWID')
    stats = dict(rows_read=0, invalid_rows=0, outside_bbox=0,
                 duplicate_or_conflicting_timestamp_rows=0, retained_reports=0)
    aliases = {'mmsi': 'm', 'basedatetime': 't', 'longitude': 'lon', 'lon': 'lon',
               'latitude': 'lat', 'lat': 'lat', 'sog': 'sog', 'cog': 'cog'}
    batch = []

    def flush():
        before = db.total_changes
        db.executemany('INSERT OR IGNORE INTO reports VALUES (?,?,?,?,?,?)', batch)
        added = db.total_changes-before
        stats['retained_reports'] += added
        stats['duplicate_or_conflicting_timestamp_rows'] += len(batch)-added
        batch.clear()
        db.commit()

    with open_csv(args.input, args.node) as stream:
        reader = csv.DictReader(stream)
        mapping = {aliases[key]: name for name in (reader.fieldnames or [])
                   if (key := ''.join(c for c in name.lower() if c.isalnum())) in aliases}
        missing = {'m','t','lon','lat','sog','cog'} - mapping.keys()
        if missing:
            raise ValueError('Missing required columns: '+', '.join(sorted(missing)))
        for row in reader:
            stats['rows_read'] += 1
            try:
                m = row[mapping['m']].strip()
                t = timestamp(row[mapping['t']])
                lon, lat = float(row[mapping['lon']]), float(row[mapping['lat']])
                sog = float(row[mapping['sog']])
                raw_cog = row[mapping['cog']].strip()
                # At exactly zero speed the velocity is zero regardless of COG.
                cog = 0.0 if sog == 0 else (float(raw_cog) if raw_cog else math.nan)
                if not (m.isdigit() and len(m) == 9 and
                        all(map(math.isfinite, (t,lon,lat,sog,cog))) and
                        -180 <= lon <= 180 and -90 < lat < 90 and
                        0 <= sog <= args.max_speed_knots and 0 <= cog < 360):
                    raise ValueError('invalid report')
                if args.bbox:
                    west,south,east,north = args.bbox
                    if not (west <= lon <= east and south <= lat <= north):
                        stats['outside_bbox'] += 1
                        continue
                ve,vn = velocity(sog,cog)
                batch.append((m,t,lon,lat,ve,vn))
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                stats['invalid_rows'] += 1
            if len(batch) >= 50000:
                flush()
            if stats['rows_read'] % 1000000 == 0:
                log(f"Read {stats['rows_read']:,} rows")
        flush()
    return stats


def synchronize(db, args):
    db.execute('CREATE TABLE samples (t INTEGER,m TEXT,lon REAL,lat REAL,ve REAL,vn REAL)')
    batch = []
    previous = None
    count = rejected_segments = 0
    for row in db.execute('SELECT m,t,lon,lat,ve,vn FROM reports ORDER BY m,t'):
        m,t,lon,lat,ve,vn = row
        if previous and previous[0] == m:
            _,pt,plon,plat,pve,pvn = previous
            gap = t-pt
            if 0 < gap <= args.max_gap_seconds:
                delta_lon = (lon-plon+180) % 360-180
                dx = EARTH_R*math.radians(delta_lon)*math.cos(math.radians((lat+plat)/2))
                dy = EARTH_R*math.radians(lat-plat)
                if math.hypot(dx,dy)/gap <= args.max_speed_knots*KNOT:
                    # Half-open intervals prevent duplicate samples at shared endpoints.
                    tick = math.ceil(pt/args.step_seconds)*args.step_seconds
                    while tick < t:
                        f = (tick-pt)/gap
                        batch.append((tick,m,(plon+f*delta_lon+180)%360-180,
                                      plat+f*(lat-plat),pve+f*(ve-pve),pvn+f*(vn-pvn)))
                        tick += args.step_seconds
                else:
                    rejected_segments += 1
            elif gap > args.max_gap_seconds:
                rejected_segments += 1
        # Include a report exactly on the grid if it is the final report of a
        # track: handled below by inserting exact reports absent from samples.
        previous = row
        if len(batch) >= 50000:
            db.executemany('INSERT INTO samples VALUES (?,?,?,?,?,?)',batch)
            count += len(batch)
            batch.clear()
    db.executemany('INSERT INTO samples VALUES (?,?,?,?,?,?)',batch)
    count += len(batch)
    db.execute('CREATE UNIQUE INDEX sample_time_vessel ON samples(t,m)')
    before = db.total_changes
    db.execute('INSERT OR IGNORE INTO samples SELECT CAST(t AS INTEGER),m,lon,lat,ve,vn '
               'FROM reports WHERE t = CAST(t AS INTEGER) AND CAST(t AS INTEGER) % ? = 0',
               (args.step_seconds,))
    count += db.total_changes-before
    db.commit()
    return dict(aligned_samples=count, rejected_or_long_gap_segments=rejected_segments)


def candidate_pairs(frame, radius):
    """ECEF grid: nearby surface points have nearby chord coordinates.

    Radius conservatively covers DCPA + maximum relative speed * horizon.
    Exact relative-motion screening follows; dateline crossings work naturally.
    """
    grid = {}
    offsets = tuple(itertools.product((-1,0,1),repeat=3))
    for point in frame:
        cell = tuple(math.floor(v/radius) for v in ecef(point[1],point[2]))
        for offset in offsets:
            neighbor = tuple(cell[k]+offset[k] for k in range(3))
            for other in grid.get(neighbor, ()):
                yield other,point
        grid.setdefault(cell,[]).append(point)


EVENT_FIELDS = ['mmsi_a','mmsi_b','first_flag_utc','last_flag_utc','flag_count',
                'minimum_flagged_separation_m','minimum_dcpa_m','minimum_dcpa_utc',
                'tcpa_at_minimum_dcpa_s','tcpa_at_first_flag_s',
                'first_flag_longitude','first_flag_latitude']


def detect(db, args, writer):
    active = {}
    stats = dict(events=0, flagged_pair_samples=0, time_steps=0)

    def finish(pair):
        event = active.pop(pair)
        writer.writerow(dict(zip(EVENT_FIELDS,[*pair,utc(event['first']),utc(event['last']),
            event['n'],round(event['distance'],3),round(event['dcpa'],3),
            utc(event['dcpa_time']),round(event['tcpa_min'],3),round(event['tcpa_first'],3),
            round(event['lon'],6),round(event['lat'],6)])))
        stats['events'] += 1

    rows = db.execute('SELECT t,m,lon,lat,ve,vn FROM samples ORDER BY t,m')
    for tick, group in itertools.groupby(rows, key=lambda row: row[0]):
        frame = [row[1:] for row in group]
        for pair in list(active):
            if tick-active[pair]['last'] > args.merge_gap_seconds:
                finish(pair)
        max_speed = max((math.hypot(p[3],p[4]) for p in frame), default=0)
        radius = args.dcpa_meters+2*max_speed*args.tcpa_seconds
        for a,b in candidate_pairs(frame,radius):
            # Suppress stationary/near-stationary pairs, but keep anchored vs moving.
            if max(math.hypot(a[3],a[4]),math.hypot(b[3],b[4])) < args.min_speed_knots*KNOT:
                continue
            distance,dcpa,tcpa = relative_cpa(a[1:],b[1:])
            if tcpa is None or not (0 < tcpa <= args.tcpa_seconds and dcpa <= args.dcpa_meters):
                continue
            pair = tuple(sorted((a[0],b[0])))
            event = active.get(pair)
            if event is None:
                event = dict(first=tick,last=tick,n=0,distance=distance,dcpa=dcpa,
                             dcpa_time=tick,tcpa_min=tcpa,tcpa_first=tcpa,
                             lon=(a[1]+((b[1]-a[1]+180)%360-180)/2+180)%360-180,
                             lat=(a[2]+b[2])/2)
                active[pair] = event
            event['last'] = tick
            event['n'] += 1
            event['distance'] = min(event['distance'],distance)
            if dcpa < event['dcpa']:
                event.update(dcpa=dcpa,dcpa_time=tick,tcpa_min=tcpa)
            stats['flagged_pair_samples'] += 1
        stats['time_steps'] += 1
        if stats['time_steps'] % 120 == 0:
            log(f"Analyzed through {utc(tick)}; {stats['events']:,} completed events")
    for pair in list(active):
        finish(pair)
    return stats


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('input', type=Path)
    p.add_argument('--output-dir', type=Path, default=Path('near_miss_results'))
    p.add_argument('--work-dir', type=Path, help='Existing folder for temporary SQLite file')
    p.add_argument('--node', help='Node.js executable, used only if zstandard is unavailable')
    p.add_argument('--bbox', nargs=4, type=float, metavar=('WEST','SOUTH','EAST','NORTH'),
                   help='Filter reports to this box; use a buffer around the area of interest')
    p.add_argument('--dcpa-meters', type=float, default=926)
    p.add_argument('--tcpa-seconds', type=float, default=360)
    p.add_argument('--step-seconds', type=int, default=30)
    p.add_argument('--max-gap-seconds', type=float, default=120)
    p.add_argument('--merge-gap-seconds', type=float, default=120)
    p.add_argument('--max-speed-knots', type=float, default=99.9)
    p.add_argument('--min-speed-knots', type=float, default=1.0,
                   help='At least one vessel must meet this speed (default: 1 knot)')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    for key in ('dcpa_meters','tcpa_seconds','step_seconds','max_gap_seconds',
                'merge_gap_seconds','max_speed_knots'):
        if not math.isfinite(getattr(args,key)) or getattr(args,key) <= 0:
            raise ValueError(f'{key} must be finite and positive')
    if not math.isfinite(args.min_speed_knots) or not 0 <= args.min_speed_knots <= args.max_speed_knots:
        raise ValueError('min-speed-knots must be between zero and max-speed-knots')
    if args.merge_gap_seconds < args.step_seconds:
        raise ValueError('merge-gap-seconds must be at least step-seconds')
    if args.bbox:
        w,s,e,n = args.bbox
        if not (-180 <= w < e <= 180 and -90 < s < n < 90):
            raise ValueError('Invalid bbox; dateline-spanning boxes must be split')
    args.input = args.input.resolve(strict=True)
    args.output_dir.mkdir(parents=True,exist_ok=True)
    events_path = args.output_dir/'potential_near_miss_events.csv'
    summary_path = args.output_dir/'summary.json'
    # Avoid destroying an earlier result or an input accidentally named as output.
    if events_path.exists() or summary_path.exists():
        raise FileExistsError('Output files already exist; choose a new --output-dir')
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='ais_near_miss_',dir=args.work_dir) as folder:
        db = sqlite3.connect(str(Path(folder)/'working.sqlite'))
        try:
            db.execute('PRAGMA journal_mode=OFF')
            db.execute('PRAGMA synchronous=OFF')
            db.execute('PRAGMA temp_store=FILE')
            log('Reading and sorting source reports on disk...')
            quality = ingest(db,args)
            if not quality['retained_reports']:
                raise ValueError('No valid reports remain after filtering')
            log(f"Synchronizing {quality['retained_reports']:,} reports...")
            quality.update(synchronize(db,args))
            log('Detecting and consolidating pair encounters...')
            # Publish complete outputs only after detection succeeds.
            temporary_events = Path(folder)/'events.csv'
            with temporary_events.open('w',newline='',encoding='utf-8') as out:
                writer = csv.DictWriter(out,fieldnames=EVENT_FIELDS)
                writer.writeheader()
                counts = detect(db,args,writer)
            summary = dict(label='AIS-detected potential near misses (CPA/TCPA screen)',
                source=str(args.input),parameters={k:str(v) if isinstance(v,Path) else v
                    for k,v in vars(args).items()},quality=quality,counts=counts,
                elapsed_seconds=round(time.monotonic()-started,2),
                limitations=[
                    'Screened conflicts are not confirmed hazardous near misses.',
                    'Fixed thresholds are not a universal safety standard.',
                    'Naive source timestamps are interpreted as UTC.',
                    'First valid report wins for duplicate/conflicting MMSI timestamps.',
                    'Interpolation uses short straight segments and velocity components.',
                    'Separation is antenna-position separation at flagged samples only; '
                    'not hull clearance or the full encounter minimum.',
                    'Routine tug, berth and parallel operations need contextual review.',
                    'Missing reports, boundary coverage and grid interval affect counts.'
                ])
            shutil.copyfile(temporary_events,events_path)
            summary_path.write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
        finally:
            db.close()
    print(f"Potential near-miss events: {counts['events']:,}")
    print(f'Events: {events_path.resolve()}')
    print(f'Summary: {summary_path.resolve()}')
    return summary


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as error:
        log(f'Error: {error}')
        sys.exit(1)
