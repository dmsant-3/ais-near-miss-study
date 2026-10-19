#!/usr/bin/env python3
"""Add track and maneuver evidence to CPA/TCPA event candidates.

This script applies an explicit research rule; it cannot establish ground truth.
Rule-supported events still require human review before being reported as
confirmed near misses.

Example:
  python confirm_near_misses.py --ais-file ais.csv.zst \
    --events results/events_with_weather.csv --bbox -74.3 40.4 -73.7 40.9 \
    --output results/near_miss_evidence_review.csv
"""
from __future__ import annotations

import argparse
import bisect
import csv
import math
from pathlib import Path
import statistics
import sys

import count_ais_near_misses as detector

EARTH_R = detector.EARTH_R
KNOT = detector.KNOT


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def circular_difference(a: float, b: float) -> float:
    return abs((a-b+180.0) % 360.0-180.0)


def circular_median(values: list[float]) -> float:
    """Choose the observed bearing minimizing total angular deviation."""
    return min(values, key=lambda candidate: sum(circular_difference(candidate, x)
                                                 for x in values))


def position_distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, a)
    lon2, lat2 = map(math.radians, b)
    dlat = lat2-lat1
    dlon = (lon2-lon1+math.pi) % (2*math.pi)-math.pi
    hav = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return 2*EARTH_R*math.asin(min(1.0, math.sqrt(hav)))


def interpolate(track: list[tuple], times: list[float], tick: float,
                max_gap: float) -> tuple[float, float, float, float, float] | None:
    """Return lon, lat, speed knots, course degrees, and vessel length meters."""
    i = bisect.bisect_left(times, tick)
    if i < len(times) and times[i] == tick:
        _, lon, lat, sog, cog, length = track[i]
        return lon, lat, sog, cog, length
    if i == 0 or i == len(times):
        return None
    left, right = track[i-1], track[i]
    gap = right[0]-left[0]
    if gap <= 0 or gap > max_gap:
        return None
    f = (tick-left[0])/gap
    delta_lon = (right[1]-left[1]+180.0) % 360.0-180.0
    lon = (left[1]+f*delta_lon+180.0) % 360.0-180.0
    lat = left[2]+f*(right[2]-left[2])
    ve1, vn1 = detector.velocity(left[3], left[4])
    ve2, vn2 = detector.velocity(right[3], right[4])
    ve, vn = ve1+f*(ve2-ve1), vn1+f*(vn2-vn1)
    sog = math.hypot(ve, vn)/KNOT
    cog = math.degrees(math.atan2(ve, vn)) % 360.0 if sog > 0.05 else left[4]
    lengths = [x for x in (left[5], right[5]) if x is not None]
    length = (sum(lengths)/len(lengths)) if lengths else None
    return lon, lat, sog, cog, length


def parse_num(value: str | None, low: float, high: float) -> float | None:
    try:
        number = float(value) if value and value.strip() else None
    except (ValueError, TypeError):
        return None
    return number if number is not None and math.isfinite(number) and low <= number <= high else None


def load_events(path: Path) -> tuple[list[dict], set[str]]:
    events = []
    vessels = set()
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        fields = {''.join(c for c in x.lower() if c.isalnum()): x
                  for x in (reader.fieldnames or [])}
        required = ('mmsia', 'mmsib', 'firstflagutc', 'lastflagutc')
        if any(key not in fields for key in required):
            raise ValueError('Events CSV must contain MMSI A/B and first/last flag UTC columns')
        for row in reader:
            a, b = row[fields['mmsia']].strip(), row[fields['mmsib']].strip()
            if not (a.isdigit() and b.isdigit()):
                continue
            start = detector.timestamp(row[fields['firstflagutc']])
            end = detector.timestamp(row[fields['lastflagutc']])
            if end < start:
                raise ValueError('Event last flag precedes first flag')
            row['_a'], row['_b'] = a, b
            row['_start'], row['_end'] = start, end
            row['_id'] = row.get('event_id') or row.get('eventid') or ''
            events.append(row)
            vessels.update((a, b))
    if not events:
        raise ValueError('No usable events found in the events CSV')
    return events, vessels


def load_tracks(path: Path, vessels: set[str], bbox: list[float], max_speed: float) -> dict:
    west, south, east, north = bbox
    aliases = {'mmsi':'mmsi', 'basedatetime':'time', 'longitude':'lon', 'lon':'lon',
               'latitude':'lat', 'lat':'lat', 'sog':'sog', 'cog':'cog', 'length':'length'}
    tracks: dict[str, list[tuple]] = {m: [] for m in vessels}
    bad = 0
    seen = 0
    with detector.open_csv(path) as stream:
        reader = csv.DictReader(stream)
        mapping = {aliases[key]: name for name in (reader.fieldnames or [])
                   if (key := ''.join(c for c in name.lower() if c.isalnum())) in aliases}
        missing = {'mmsi','time','lon','lat','sog','cog'}-mapping.keys()
        if missing:
            raise ValueError('AIS file lacks required fields: '+', '.join(sorted(missing)))
        for row in reader:
            seen += 1
            m = (row.get(mapping['mmsi']) or '').strip()
            if m not in tracks:
                continue
            try:
                t = detector.timestamp(row[mapping['time']])
                lon, lat = float(row[mapping['lon']]), float(row[mapping['lat']])
                sog = float(row[mapping['sog']])
                raw_cog = (row.get(mapping['cog']) or '').strip()
                cog = 0.0 if sog == 0 else float(raw_cog)
                if not (all(map(math.isfinite,(t,lon,lat,sog,cog))) and
                        -180 <= lon <= 180 and -90 < lat < 90 and
                        0 <= sog <= max_speed and 0 <= cog < 360):
                    raise ValueError
                if not (west <= lon <= east and south <= lat <= north):
                    continue
                length = parse_num(row.get(mapping.get('length','')), 0.0, 500.0)
                tracks[m].append((t,lon,lat,sog,cog,length))
            except (ValueError, TypeError, KeyError, OverflowError):
                bad += 1
            if seen % 1_000_000 == 0:
                log(f'Read {seen:,} AIS rows')
    for m in tracks:
        # Keep one deterministic valid record for each vessel/timestamp.
        tracks[m].sort(key=lambda item: item[0])
        deduped = []
        for item in tracks[m]:
            if not deduped or item[0] != deduped[-1][0]:
                deduped.append(item)
        tracks[m] = deduped
    log(f'AIS rows read: {seen:,}; invalid candidate-vessel rows: {bad:,}')
    log(f'Candidate vessels with reports in area: {sum(bool(x) for x in tracks.values()):,}/{len(tracks):,}')
    return tracks


def evidence_for(event: dict, tracks: dict, args) -> dict:
    a, b = tracks[event['_a']], tracks[event['_b']]
    ta, tb = [r[0] for r in a], [r[0] for r in b]
    start, end = event['_start'], event['_end']
    before_start = start-args.context_seconds
    sample_end = end+args.context_seconds
    tick = math.ceil(before_start/args.step_seconds)*args.step_seconds
    baseline_a, baseline_b = [], []
    encounter_a, encounter_b = [], []
    distances = []
    n_before = n_encounter = 0
    while tick <= sample_end:
        pa = interpolate(a,ta,tick,args.max_gap_seconds) if a else None
        pb = interpolate(b,tb,tick,args.max_gap_seconds) if b else None
        if pa and pb:
            dist = position_distance(pa[:2],pb[:2])
            distances.append((dist,tick,pa,pb))
            if start-args.baseline_end_seconds <= tick < start-args.baseline_start_seconds:
                baseline_a.append(pa); baseline_b.append(pb); n_before += 1
            if start-args.maneuver_before_seconds <= tick <= end+args.maneuver_after_seconds:
                encounter_a.append(pa); encounter_b.append(pb); n_encounter += 1
        tick += args.step_seconds

    base_a = statistics.median([p[2] for p in baseline_a]) if baseline_a else None
    base_b = statistics.median([p[2] for p in baseline_b]) if baseline_b else None
    course_a = circular_median([p[3] for p in baseline_a]) if baseline_a else None
    course_b = circular_median([p[3] for p in baseline_b]) if baseline_b else None
    change_a = max((circular_difference(p[3],course_a) for p in encounter_a),default=None) if course_a is not None else None
    change_b = max((circular_difference(p[3],course_b) for p in encounter_b),default=None) if course_b is not None else None
    drop_a = max((base_a-p[2] for p in encounter_a),default=None) if base_a is not None else None
    drop_b = max((base_b-p[2] for p in encounter_b),default=None) if base_b is not None else None
    speed_evasion_a = drop_a is not None and drop_a >= args.speed_drop_knots and base_a and drop_a/base_a >= args.speed_drop_fraction
    speed_evasion_b = drop_b is not None and drop_b >= args.speed_drop_knots and base_b and drop_b/base_b >= args.speed_drop_fraction
    course_evasion_a = change_a is not None and change_a >= args.course_change_degrees
    course_evasion_b = change_b is not None and change_b >= args.course_change_degrees
    adequate = n_before >= args.min_samples and n_encounter >= args.min_samples
    closest = min(distances,key=lambda x:x[0]) if distances else None
    min_distance = closest[0] if closest else None
    sizes = [p[4] for p in closest[2:] if p[4] is not None] if closest else []
    size_screen = max(args.min_close_distance_m, sum(sizes)/2) if sizes else args.min_close_distance_m
    maneuver_a = bool(course_evasion_a or speed_evasion_a)
    maneuver_b = bool(course_evasion_b or speed_evasion_b)
    if not adequate or min_distance is None:
        label = 'insufficient_track_coverage'
    elif min_distance <= size_screen and (maneuver_a or maneuver_b):
        label = 'rule_supported_candidate'
    elif min_distance <= size_screen:
        label = 'close_pass_no_maneuver_evidence'
    else:
        label = 'no_close_pass_in_review_window'
    return {
        'rule_classification':label,
        'human_review_label':'pending',
        'reviewer_notes':'',
        'observed_min_separation_in_window_m':round(min_distance,2) if min_distance is not None else '',
        'distance_time_utc':detector.utc(closest[1]) if closest else '',
        'distance_threshold_used_m':round(size_screen,2),
        'ship_length_a_near_pass_m':round(closest[2][4],1) if closest and closest[2][4] is not None else '',
        'ship_length_b_near_pass_m':round(closest[3][4],1) if closest and closest[3][4] is not None else '',
        'synchronized_baseline_samples':n_before,
        'synchronized_encounter_samples':n_encounter,
        'max_course_change_a_deg':round(change_a,1) if change_a is not None else '',
        'max_course_change_b_deg':round(change_b,1) if change_b is not None else '',
        'max_speed_drop_a_knots':round(drop_a,2) if drop_a is not None else '',
        'max_speed_drop_b_knots':round(drop_b,2) if drop_b is not None else '',
        'maneuver_evidence_a':bool(course_evasion_a or speed_evasion_a),
        'maneuver_evidence_b':bool(course_evasion_b or speed_evasion_b),
    }


def parser() -> argparse.ArgumentParser:
    p=argparse.ArgumentParser(description='Review CPA/TCPA events using observed close-pass and AIS maneuver evidence.')
    p.add_argument('--ais-file',type=Path,required=True)
    p.add_argument('--events',type=Path,required=True,help='Pipeline events_with_weather.csv or detector event CSV')
    p.add_argument('--bbox',type=float,nargs=4,required=True,metavar=('WEST','SOUTH','EAST','NORTH'))
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--context-seconds',type=int,default=600,help='AIS context before/after candidate window (default 600)')
    p.add_argument('--max-gap-seconds',type=int,default=120)
    p.add_argument('--step-seconds',type=int,default=30)
    p.add_argument('--min-close-distance-m',type=float,default=185.2,help='Close-pass distance floor (default 0.1 nautical mile)')
    p.add_argument('--course-change-degrees',type=float,default=15.0)
    p.add_argument('--speed-drop-knots',type=float,default=2.0)
    p.add_argument('--speed-drop-fraction',type=float,default=0.25)
    p.add_argument('--baseline-start-seconds',type=int,default=120,help='End of pre-event maneuver baseline before first flag')
    p.add_argument('--baseline-end-seconds',type=int,default=300,help='Start of pre-event maneuver baseline before first flag')
    p.add_argument('--maneuver-before-seconds',type=int,default=60)
    p.add_argument('--maneuver-after-seconds',type=int,default=180)
    p.add_argument('--min-samples',type=int,default=3)
    p.add_argument('--max-speed-knots',type=float,default=100.0)
    return p


def main(argv=None) -> int:
    args=parser().parse_args(argv)
    if not (-180 <= args.bbox[0] < args.bbox[2] <= 180 and -90 < args.bbox[1] < args.bbox[3] < 90):
        raise ValueError('Invalid bbox; dateline-spanning boxes must be split')
    if args.output.exists():
        raise FileExistsError(f'Refusing to overwrite {args.output}')
    events,vessels=load_events(args.events)
    tracks=load_tracks(args.ais_file.resolve(strict=True),vessels,args.bbox,args.max_speed_knots)
    fields=list(events[0].keys())
    fields=[x for x in fields if not x.startswith('_')]
    evidence_fields=['rule_classification','human_review_label','reviewer_notes',
        'observed_min_separation_in_window_m','distance_time_utc','distance_threshold_used_m',
        'ship_length_a_near_pass_m','ship_length_b_near_pass_m','synchronized_baseline_samples',
        'synchronized_encounter_samples','max_course_change_a_deg','max_course_change_b_deg',
        'max_speed_drop_a_knots','max_speed_drop_b_knots','maneuver_evidence_a','maneuver_evidence_b']
    args.output.parent.mkdir(parents=True,exist_ok=True)
    counts={}
    with args.output.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields+evidence_fields,extrasaction='ignore')
        writer.writeheader()
        for i,event in enumerate(events,1):
            evidence=evidence_for(event,tracks,args)
            counts[evidence['rule_classification']]=counts.get(evidence['rule_classification'],0)+1
            writer.writerow({**event,**evidence})
            if i % 1000 == 0:
                log(f'Classified {i:,}/{len(events):,} candidates')
    print(f'Wrote {len(events):,} reviewed candidates to {args.output.resolve()}')
    print('Rule classifications: '+', '.join(f'{k}={v:,}' for k,v in sorted(counts.items())))
    print('Rule-supported candidates are not confirmed without human review.')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
