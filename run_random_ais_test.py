"""Run and verify the complete pipeline for one randomly selected NOAA file.

Run from the study folder: python run_random_ais_test.py
The study boundary is the documented New York example. Date selection occurs
once from the live listing; a failed run is recorded, not silently reselected.
"""
import collections
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import time

import ais_environment as environment
import ais_weather_pipeline as pipeline


def main():
    started = time.monotonic()
    cache = Path('ais_weather_cache')
    available = sorted(pipeline.fetch_index(cache))
    selected = secrets.choice(available)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder = Path(f'random_ais_test_{selected}_{stamp}')
    folder.mkdir()
    selection = dict(selected_date=selected,index_url=pipeline.INDEX,
        selection_method='secrets.choice over all daily links in the live NOAA index',
        candidate_count=len(available),available_dates=available,
        selected_file_url=pipeline.INDEX.rsplit('/',1)[0]+f'/ais-{selected}.csv.zst',
        bbox=[-74.3,40.4,-73.7,40.9],started_utc=datetime.now(timezone.utc).isoformat())
    pipeline.write_json(folder/'selection.json',selection)
    print(f'Randomly selected: {selected} (from {len(available)} available dates)',flush=True)
    print(f'Test folder: {folder.resolve()}',flush=True)
    arguments = ['--date',selected,'--bbox','-74.3','40.4','-73.7','40.9',
                 '--cache-dir',str(cache),'--output-dir',str(folder/'results')]
    try:
        summary = pipeline.main(arguments)
        with (folder/'results/events_with_weather.csv').open(encoding='utf-8',newline='') as stream:
            events = list(csv.DictReader(stream))
        assert len(events) == summary['potential_near_miss_events'], 'Event count differs from CSV'
        assert len({e['event_id'] for e in events}) == len(events), 'Duplicate event identifiers'
        pair_events = collections.defaultdict(list)
        match_counts = {kind:collections.Counter() for kind in environment.VALUES}
        for event in events:
            assert event['mmsi_a'] < event['mmsi_b'], 'Invalid or reversed vessel pair'
            assert -74.3 <= float(event['first_flag_longitude']) <= -73.7
            assert 40.4 <= float(event['first_flag_latitude']) <= 40.9
            assert event['first_flag_utc'].startswith(selected), 'Event outside selected day'
            first = pipeline.detector.timestamp(event['first_flag_utc'])
            last = pipeline.detector.timestamp(event['last_flag_utc'])
            assert first <= last
            assert int(event['flag_count']) >= 1
            assert 0 <= float(event['minimum_dcpa_m']) <= 926.001
            assert 0 < float(event['tcpa_at_first_flag_s']) <= 360.001
            pair_events[(event['mmsi_a'],event['mmsi_b'])].append((first,last))
            for kind,fields in environment.VALUES.items():
                status = event[kind+'_match_status']
                match_counts[kind][status] += 1
                if status == 'matched':
                    assert float(event[kind+'_distance_km']) <= {'visibility':25,'waves':50,'currents':10}[kind]+0.001
                    assert abs(float(event[kind+'_time_offset_seconds'])) <= {'visibility':1800,'waves':3600,'currents':900}[kind]+0.001
                else:
                    assert all(event[field] == '' for field in fields), 'Rejected match contains measurement values'
        for episodes in pair_events.values():
            episodes.sort()
            assert all(b[0]-a[1] > 120 for a,b in zip(episodes,episodes[1:])), 'Pair events should have merged'
        for kind,count in match_counts.items():
            assert dict(count) == summary['environment_match_counts'][kind]
        verification = dict(status='passed',selected_date=selected,event_rows=len(events),
            checks=['CSV/summary count reconciliation','unique events and vessel pairs',
                    'event locations and dates','DCPA/TCPA thresholds','event consolidation',
                    'environmental match tolerances','blank values for rejected matches',
                    'environmental match-count reconciliation'],
            elapsed_seconds=round(time.monotonic()-started,2),
            pipeline_status=summary['status'],environment_match_counts=summary['environment_match_counts'])
        pipeline.write_json(folder/'verification.json',verification)
        print(json.dumps(verification,indent=2),flush=True)
    except Exception as error:
        pipeline.write_json(folder/'verification.json',dict(status='failed',selected_date=selected,
            error_type=type(error).__name__,error=str(error),
            elapsed_seconds=round(time.monotonic()-started,2)))
        raise


if __name__ == '__main__':
    main()
