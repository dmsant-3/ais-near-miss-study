#!/usr/bin/env python3
"""Sequentially screen NOAA daily AIS files for likely near misses at three ports.

Each source day is downloaded once, screened separately for each port, then
removed from a temporary working directory. Only compact event-review reports
and daily summaries are retained. AIS-derived labels are research candidates,
not confirmed incidents.

Example:
  python run_historical_near_miss_study.py --start-date 2016-01-01 \
      --end-date 2016-01-03 --pause-seconds 0
"""
from __future__ import annotations

import argparse
from html import escape
import ais_request_policy
import csv
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re 
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request
from ais_request_policy import open_request as urlopen

import count_ais_near_misses as detector
from ais_event_dashboard import export

BASE = 'https://noaaocm.blob.core.windows.net/ais/csv2/'
USER_AGENT = 'AISNearMissResearch/1.0 (sequential NOAA AIS analysis)'
PORTS = {
    # Local reporting boxes cover port waters and nearby approach areas.
    'Port_of_NY_NJ': (-74.30, 40.30, -73.65, 40.95),
    'Port_of_LA': (-118.55, 33.55, -117.85, 34.00),
    'Port_of_Houston': (-95.45, 29.25, -94.65, 29.90),
}
RANK_FIELDS = ['likely_near_miss_score_7', 'likely_near_miss_score_8', 'likely_near_miss_classification',
               'likely_near_miss_evidence', 'likely_near_miss_human_review']
EVIDENCE_FIELDS = [
    "involves_tugboat", "tug_track_pattern",
    "baseline_course_difference_deg",
    'rule_classification', 'human_review_label', 'reviewer_notes',
    'observed_min_separation_in_window_m', 'distance_time_utc',
    'distance_threshold_used_m', 'ship_length_a_near_pass_m',
    'ship_length_b_near_pass_m', 'synchronized_baseline_samples',
    'synchronized_encounter_samples', 'max_course_change_a_deg',
    'max_course_change_b_deg', 'max_speed_drop_a_knots',
    'max_speed_drop_b_knots', 'maneuver_evidence_a', 'maneuver_evidence_b',
]
REPORT_FIELDS = ['ais_date', 'port'] + detector.EVENT_FIELDS + EVIDENCE_FIELDS + RANK_FIELDS


class IndexParser(HTMLParser):
    """Collect only dated daily Zstandard CSV files from the requested year."""
    def __init__(self, year: int):
        super().__init__()
        self.year = year
        self.links: dict[date, str] = {}
        self.index_url = f'{BASE}csv{year}/index.html'

    def handle_starttag(self, tag, attrs):
        if tag != 'a':
            return
        href = dict(attrs).get('href', '')
        absolute = urljoin(self.index_url, href)
        parsed = urlparse(absolute)
        match = re.fullmatch(rf'ais-({self.year}-\d{{2}}-\d{{2}})\.csv\.zst',
                             Path(parsed.path).name)
        if (match and parsed.scheme == 'https' and
                parsed.netloc == urlparse(BASE).netloc and
                parsed.path.startswith(f'/ais/csv2/csv{self.year}/') and
                not parsed.query):
            try:
                self.links[date.fromisoformat(match.group(1))] = absolute
            except ValueError:
                pass


def get(url: str) -> bytes:
    """Fetch with normal TLS verification and bounded retries."""
    for attempt in range(4):
        try:
            with urlopen(Request(url, headers={'User-Agent': USER_AGENT}), timeout=120) as response:
                return response.read()
        except HTTPError as exc:
            if exc.code == 404:
                raise
            if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == 3:
                raise
        except (URLError, TimeoutError, OSError):
            if attempt == 3:
                raise
        time.sleep(min(2 ** attempt, 15))
    raise RuntimeError('NOAA request retries exhausted')


def available_days(start: date, end: date, index_cache: Path | None = None) -> dict[date, str]:
    links: dict[date, str] = {}
    for year in range(start.year, end.year + 1):
        parser = IndexParser(year)
        try:
            cached = index_cache / f'{year}.html' if index_cache else None
            ttl = 86400 if year == date.today().year else 7 * 86400
            if cached and cached.exists() and time.time() - cached.stat().st_mtime < ttl:
                parser.feed(cached.read_text(encoding='utf-8'))
            else:
                content = get(parser.index_url).decode('utf-8')
                parser.feed(content)
                if cached and parser.links:
                    temp = cached.with_suffix('.part')
                    temp.write_text(content, encoding='utf-8')
                    temp.replace(cached)

        except HTTPError as exc:
            if exc.code == 404:
                print(f'No NOAA CSV archive for {year}; skipping.', flush=True)
                continue
            raise
        links.update({day: url for day, url in parser.links.items() if start <= day <= end})
    return dict(sorted(links.items()))


def download(url: str, target: Path) -> dict:
    """Write one verified daily download, without keeping a second copy."""
    digest = __import__('hashlib').sha256()
    total = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + '.part')
    try:
        with urlopen(Request(url, headers={'User-Agent': USER_AGENT}), timeout=180) as response, part.open('wb') as out:
            expected = response.headers.get('Content-Length')
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                out.write(block)
                digest.update(block)
                total += len(block)
            if not total or (expected is not None and total != int(expected)):
                raise OSError(f'Incomplete AIS download: expected {expected}, received {total} bytes')
        part.replace(target)
        return {'source_url': url, 'compressed_bytes': total, 'sha256': digest.hexdigest()}
    finally:
        part.unlink(missing_ok=True)


def row_count(path: Path) -> int:
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return max(0, sum(1 for _ in csv.DictReader(stream)))


def run_command(args: list[str]) -> None:
    print('  $ ' + subprocess.list2cmdline(args), flush=True)
    subprocess.run(args, check=True)


def analyze_port(day: date, port: str, bbox: tuple[float, ...], ais: Path,
                 staging: Path, node: str | None, adjacent=None) -> dict:
    port_dir = staging / port
    detected_dir = port_dir / 'detected'
    detected_dir.mkdir(parents=True)
    events = detected_dir / 'potential_near_miss_events.csv'
    summary = detected_dir / 'summary.json'
    common = [sys.executable, str(Path(__file__).with_name('count_ais_near_misses.py')),
              str(ais), '--bbox', *(str(v) for v in bbox), '--output-dir', str(detected_dir)]
    if node:
        common += ['--node', node]
    run_command(common)
    detection_summary = json.loads(summary.read_text(encoding='utf-8'))
    # The downloaded path is intentionally temporary; preserve truthful provenance.
    detection_summary['source'] = 'Temporary NOAA daily AIS file; deleted after processing.'
    detection_summary.setdefault('parameters', {})['input'] = ais.name
    summary.write_text(json.dumps(detection_summary, indent=2) + '\n', encoding='utf-8')
    count = row_count(events)
    scored = port_dir / 'scored_candidates.csv'
    if count:
        evidence = port_dir / 'evidence_review.csv'
        confirm = [sys.executable, str(Path(__file__).with_name('confirm_near_misses.py')),
                   '--ais-file', str(ais), '--events', str(events), '--bbox',
                   *(str(v) for v in bbox), '--output', str(evidence)]
        run_command(confirm)
        rank = [sys.executable, str(Path(__file__).with_name('rank_likely_near_misses.py')),
                '--input', str(evidence), '--output', str(scored)]
        run_command(rank)
        classifications: dict[str, int] = {}
        with scored.open(encoding='utf-8-sig', newline='') as stream:
            for row in csv.DictReader(stream):
                label = row.get('likely_near_miss_classification', '')
                classifications[label] = classifications.get(label, 0) + 1
    else:
        classifications = {}
    export(day, bbox, ais, port_dir, scored, node, adjacent)
    return {'date': day.isoformat(), 'port': port, 'status': 'complete',
            'potential_events': count,
            'likely_for_human_review': classifications.get('likely_near_miss_for_human_review', 0) + classifications.get('elevated_priority_candidate', 0),
            'elevated_priority_candidates': classifications.get('elevated_priority_candidate', 0),
            'lower_priority_candidates': classifications.get('lower_priority_candidate', 0),
            'detector_quality': detection_summary.get('quality', {}),
            'scored_file': scored.name if scored.exists() else None}


def publish_report(temp: Path, target: Path) -> None:
    for delay in (0.25, 0.5, 1, 2, None):
        try:
            temp.replace(target)
            return
        except PermissionError as exc:
            if delay is None:
                raise PermissionError(
                    f'Cannot replace {target}. Close the report in Excel or its preview '
                    f'and retry. The rebuilt report is saved at {temp}.'
                ) from exc
            time.sleep(delay)


def write_csv_atomic(path: Path, fields: list[str], rows: list[dict]) -> None:
    temp = path.with_suffix(path.suffix + '.part')
    with temp.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    publish_report(temp, path)


def log_progress(report_dir: Path, message: str) -> None:
    with (report_dir / 'progress.log').open('a', encoding='utf-8') as stream:
        stream.write(f'{datetime.now(timezone.utc).isoformat()} {message}\n')


def rebuild_reports(report_dir: Path) -> None:
    """Regenerate aggregate reports from committed per-day results."""
    all_candidates: list[dict] = []
    likely: list[dict] = []
    tug_review: list[dict] = []
    daily: list[dict] = []
    for summary_path in sorted((report_dir / 'daily').glob('*/summary.json')):
        summary = json.loads(summary_path.read_text(encoding='utf-8'))
        for result in summary['ports']:
            daily.append({k: v for k, v in result.items() if k != 'detector_quality'})
            filename = result.get('scored_file')
            if not filename:
                continue
            scored = summary_path.parent / result['port'] / filename
            if not scored.exists():
                continue
            with scored.open(encoding='utf-8-sig', newline='') as stream:
                for row in csv.DictReader(stream):
                    record = {'ais_date': summary['date'], 'port': result['port'], **row}
                    label = row['likely_near_miss_classification']
                    if label in ('separate_tugboat_review', 'excluded_tugboat'):
                        continue
                    all_candidates.append(record)
                    if row.get('involves_tugboat', '').lower() == 'true' and row.get('tug_track_pattern') == 'possible_conflict':
                        tug_review.append(record)
                    if row['likely_near_miss_classification'] in ('likely_near_miss_for_human_review', 'elevated_priority_candidate'):
                        likely.append(record)
    report_dir.mkdir(parents=True, exist_ok=True)
    write_csv_atomic(report_dir / 'candidate_review_report.csv', REPORT_FIELDS, all_candidates)
    write_csv_atomic(report_dir / 'likely_near_miss_report.csv', REPORT_FIELDS, likely)
    write_csv_atomic(report_dir / 'tugboat_review_report.csv', REPORT_FIELDS, tug_review)
    daily_fields = ['date', 'port', 'status', 'potential_events',
                    'likely_for_human_review', 'elevated_priority_candidates',
                    'lower_priority_candidates', 'scored_file']
    write_csv_atomic(report_dir / 'daily_summary.csv', daily_fields, daily)
    totals = {
        'generated_utc': datetime.now(timezone.utc).isoformat(),
        'data_source': BASE,
        'date_span_requested': None,
        'ports': {name: {'bbox_wgs84': bbox} for name, bbox in PORTS.items()},
        'screening': {'dcpa_max_m': 926, 'tcpa_range_seconds': [0, 360],
                      'sample_step_seconds': 30,
                      'likely_label': 'Transparent evidence score 8/8 with close-pass, imminent CPA, maneuver and coverage gates; human review still required.'},
        'limitations': [
            'AIS evidence cannot confirm that a near miss occurred; labels prioritize human review.',
            'One daily AIS file is analyzed at a time; midnight encounters may be incomplete.',
            'The three boxes are fixed geographic study boundaries and can be changed in the script.',
            'Only files linked by NOAA daily CSV year indexes are included; missing days are not inferred.',
            'A full 2016-to-present run takes weeks with the default five-minute inter-day pause and uses substantial download bandwidth.'
        ],
        'completed_day_count': len({r['date'] for r in daily}),
        'ports_day_rows': len(daily),
        'total_potential_events': sum(int(r.get('potential_events') or 0) for r in daily),
        'total_likely_for_human_review': sum(int(r.get('likely_for_human_review') or 0) for r in daily),
        'candidate_review_rows': len(all_candidates),
    }
    config = report_dir / 'run_config.json'
    if config.exists():
        totals['date_span_requested'] = json.loads(config.read_text(encoding='utf-8'))['date_span_requested']
    temp = report_dir / 'report_summary.json.part'
    temp.write_text(json.dumps(totals, indent=2) + '\n', encoding='utf-8')
    publish_report(temp, report_dir / 'report_summary.json')
    latest = max((row['date'] for row in daily), default=None)
    table_rows = []
    for row in daily:
        if row['date'] != latest:
            continue
        port = escape(row['port'])
        replay = f"daily/{escape(latest)}/{port}/event_dashboard.html"
        table_rows.append(
            f'<tr><td><a href="{replay}">{port.replace("_", " ")}</a></td>'
            f'<td>{int(row["potential_events"]):,}</td>'
            f'<td>{int(row["likely_for_human_review"]):,}</td></tr>'
        )
    progress = report_dir / 'progress.html'
    temp = progress.with_suffix('.html.part')
    temp.write_text(f"""<!doctype html>
<html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>AIS processing progress</title>
<style>
body{{font:16px/1.6 system-ui;margin:40px auto;padding:0 20px;max-width:900px;
background:#102437;color:#eef7ff}} a{{color:#8de9db}}
table{{border-collapse:collapse;width:100%}}th,td{{padding:10px;text-align:left;
border-bottom:1px solid #476278}} small{{color:#b9cfdf}}
</style>
<h1>AIS processing progress</h1>
<p><strong>{totals['completed_day_count']:,}</strong> completed days ·
<strong>{totals['total_potential_events']:,}</strong> screened encounters ·
<strong>{totals['total_likely_for_human_review']:,}</strong> review candidates</p>
<h2>Latest completed AIS date</h2><p>{escape(latest or 'No completed days yet')}</p>
<table><thead><tr><th>Port replay</th><th>Screened encounters</th>
<th>Review candidates</th></tr></thead><tbody>{''.join(table_rows)}</tbody></table>
<p><a href="daily_summary.csv">Full daily summary</a></p>
<h2>Run log</h2>
<p><a href="progress.log">Open full progress log</a></p>
<iframe title="AIS run log" src="progress.log"
style="width:100%;height:320px;background:white;border:0"></iframe>
<small>Generated {escape(totals['generated_utc'])}. Refreshes every minute.
Counts come from completed daily reports.</small></html>
""", encoding='utf-8')
    publish_report(temp, progress)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--start-date', type=date.fromisoformat, default=date(2016, 1, 1))
    p.add_argument('--end-date', type=date.fromisoformat, default=date.today(),
                   help='Last date to look for; defaults to today (only published files are processed)')
    p.add_argument('--report-dir', type=Path, default=Path('historical_near_miss_report'))
    p.add_argument('--work-dir', type=Path, default=Path('ais_batch_work'),
                   help='Temporary working directory; each day is removed after analysis')
    p.add_argument('--pause-seconds', type=int, default=300,
                   help='Delay between processed days; default 300 seconds (5 minutes)')
    p.add_argument('--max-days', type=int,
                   help='Optional cap for a pilot run; omit to process every available day in range')
    p.add_argument('--dry-run', action='store_true', help='List available dates without downloading AIS files')
    p.add_argument('--cron', action='store_true', help='Pace API calls and stop on the first failed day')
    p.add_argument('--request-gap-seconds', type=float, default=5)
    p.add_argument('--node', help='Optional Node.js path when Python zstandard is unavailable')
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.start_date > args.end_date:
        raise ValueError('--start-date must be on or before --end-date')
    if args.pause_seconds < 0 or (args.max_days is not None and args.max_days <= 0):
        raise ValueError('--pause-seconds must be nonnegative and --max-days positive')
    args.report_dir = args.report_dir.resolve()
    args.work_dir = args.work_dir.resolve()
    args.report_dir.mkdir(parents=True, exist_ok=True)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    if args.cron:
        if not 1 <= args.request_gap_seconds <= 3600:
            raise ValueError("Request gap must be between 1 and 3600 seconds")
        ais_request_policy.configure(args.work_dir / "api_state", args.request_gap_seconds)
        args.max_days = args.max_days or 1
    config_path = args.report_dir / 'run_config.json'
    config = {'start_date': args.start_date.isoformat(), 'end_date': args.end_date.isoformat(),
              'date_span_requested': [args.start_date.isoformat(), args.end_date.isoformat()],
              'pause_seconds': args.pause_seconds, 'ports': PORTS,
              'screening': {'dcpa_max_m': 926, 'tcpa_max_seconds': 360}}
    if config_path.exists():
        prior = json.loads(config_path.read_text(encoding='utf-8'))
        if prior.get('start_date') != config['start_date'] or (prior.get('end_date') != config['end_date'] and
                (not args.cron or config['end_date'] < prior.get('end_date', ''))):
            raise ValueError(f'{config_path} belongs to a different date range; choose another --report-dir')
    if not config_path.exists() or args.cron:
        config_temp = config_path.with_suffix('.json.part')
        config_temp.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
        publish_report(config_temp, config_path)

    index_cache = args.work_dir / "api_state" if args.cron else None
    log_progress(args.report_dir, f'Run started for {args.start_date} through {args.end_date}')
    try:
        links = available_days(args.start_date, args.end_date, index_cache)
    except (ais_request_policy.DeferredRequest, OSError, ValueError) as exc:
        log_progress(args.report_dir, f'FAILED during NOAA index lookup: {type(exc).__name__}: {exc}')
        rebuild_reports(args.report_dir)
        print(exc, file=sys.stderr, flush=True)
        return 1
    daily_root = args.report_dir / 'daily'
    daily_root.mkdir(parents=True, exist_ok=True)
    completed = {date.fromisoformat(p.parent.name) for p in daily_root.glob('*/summary.json')
                 if re.fullmatch(r'\d{4}-\d{2}-\d{2}', p.parent.name)}
    pending = [(d, url) for d, url in links.items() if d not in completed]
    if args.max_days:
        pending = pending[:args.max_days]
    print(f'NOAA files available: {len(links):,}; already complete: {len(completed):,}; '
          f'to process this run: {len(pending):,}.', flush=True)
    if args.dry_run:
        for day, url in pending:
            print(f'{day.isoformat()}  {url}')
        return 0

    failures = 0
    for index, (day, url) in enumerate(pending, 1):
        day_target = daily_root / day.isoformat()
        log_progress(args.report_dir, f'{day} processing started')
        print(f'[{index}/{len(pending)}] Processing {day.isoformat()}', flush=True)
        try:
            with tempfile.TemporaryDirectory(prefix=f'ais_{day.isoformat()}_', dir=args.work_dir) as temp_name:
                temp = Path(temp_name)
                ais = temp / f'ais-{day.isoformat()}.csv.zst'
                def adjacent(other_day):
                    other_url = links.get(other_day)
                    if not other_url:
                        log_progress(args.report_dir, f'{day} warning: adjacent AIS unavailable for {other_day}')
                        other_url = available_days(other_day, other_day, index_cache).get(other_day)
                    if not other_url:
                        print(f'Adjacent AIS unavailable for {other_day}', flush=True)
                        return None
                    target = temp / f'ais-{other_day}.csv.zst'
                    if not target.exists():
                        download(other_url, target)
                        log_progress(args.report_dir, f'{day} downloaded adjacent AIS for {other_day}')
                    return target

                provenance = download(url, ais)
                log_progress(args.report_dir, f'{day} downloaded AIS ({provenance["compressed_bytes"]:,} bytes)')
                print(f"  Downloaded {provenance['compressed_bytes']:,} bytes; screening three ports.", flush=True)
                with tempfile.TemporaryDirectory(prefix=f'.{day.isoformat()}_staging_', dir=daily_root) as stage_name:
                    stage = Path(stage_name)
                    port_results = []
                    for port, bbox in PORTS.items():
                        print(f'  {port}:', flush=True)
                        result = analyze_port(day, port, bbox, ais, stage, args.node, adjacent)
                        port_results.append(result)
                        source_report = stage / port / 'environment_sources.json'
                        if source_report.exists():
                            environment = json.loads(source_report.read_text(encoding='utf-8'))
                            for error in environment.get('errors', []):
                                log_progress(args.report_dir, f'{day} {port} environmental warning: {error}')
                        log_progress(args.report_dir, f'{day} {port} processing completed')
                    (stage / 'summary.json').write_text(json.dumps({
                        'date': day.isoformat(), 'source': provenance,
                        'file_deleted_after_processing': True, 'ports': port_results,
                    }, indent=2) + '\n', encoding='utf-8')
                    if day_target.exists():
                        raise FileExistsError(f'Result directory already exists: {day_target}')
                    # OneDrive and Windows security software can deny directory
                    # renames even when creating files is allowed. Copy the
                    # compact results, and use summary.json as the completion marker.
                    if args.cron:
                        stage.rename(day_target)
                    else:
                        shutil.copytree(stage, day_target)
            # The TemporaryDirectory scope above removes the compressed AIS file.
            rebuild_reports(args.report_dir)
            log_progress(args.report_dir, f'{day} SUCCESS: reports published and temporary AIS cleaned up')
            likely = sum(r['likely_for_human_review'] for r in port_results)
            events = sum(r['potential_events'] for r in port_results)
            print(f'  Done: {events} potential events; {likely} likely-labels for review. '
                  'Daily AIS file deleted.', flush=True)
        except KeyboardInterrupt:
            log_progress(args.report_dir, f'{day} interrupted')
            print('\nInterrupted. Current day will be retried on the next run.', file=sys.stderr)
            rebuild_reports(args.report_dir)
            return 130
        except Exception as exc:
            failures += 1
            log_progress(args.report_dir, f'{day} FAILED: {type(exc).__name__}: {exc}')
            print(f'  FAILED {day.isoformat()}: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
            if args.cron:
                break
        if index < len(pending) and args.pause_seconds:
            print(f'Waiting {args.pause_seconds} seconds before the next day.', flush=True)
            time.sleep(args.pause_seconds)
    rebuild_reports(args.report_dir)
    status = 'FAILED' if failures else 'SUCCESS' if pending else 'NO WORK'
    log_progress(args.report_dir, f'Run finished: {status}; failed days: {failures}')
    print(f"Finished this run. Failures: {failures}. Reports: {args.report_dir}", flush=True)
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())


