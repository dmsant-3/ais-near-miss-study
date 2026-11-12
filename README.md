# AIS Near-Miss Study

This project uses historical Automatic Identification System (AIS) vessel tracks to screen for possible near collisions in New York/New Jersey, Los Angeles, and Houston. It compares vessels' projected closest approach with their reconstructed separation and maneuver evidence. An eight-point score prioritizes encounters for human review. Confirming an incident requires independent evidence.

The Python batch runner downloads and processes one AIS day at a time. It produces encounter reports, hourly counts of unique moving and anchored vessels, hourly environmental records, and vessel trajectories for replay. The local Node.js dashboard serves the saved replays with playback controls, vessel labels, and wind and current arrows where data is available.

## Dependencies

| Component | Requirements |
|---|---|
| Python batch runner | Python 3.10 or newer. Install `zstandard` to read NOAA's compressed `.csv.zst` files. The remaining Python imports use the standard library. |
| Node.js dashboard | Node.js 18 or newer. The server uses built-in modules, so no `npm install` is needed. Node.js is optional for the Python batch if `zstandard` is installed. |
| Ubuntu scheduling | `cron` runs the schedule and `flock` from `util-linux` prevents overlapping runs. |

Install the Python dependency in a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install zstandard
```

To serve a report after a completed run:

```bash
node ais_dashboard_server.js historical_cron_report
```

Open [the local dashboard](http://127.0.0.1:8765/) on the machine running Node.js.

See the [pipeline guide](AIS_Weather_Pipeline_README.md) for analysis and dashboard usage.

## Compact study output

The compact output mode stores completed dates in SQLite and serves replay data on demand. Choose an explicit study date range and measure scratch requirements on both the work and report volumes before running. The 10 GB retained-output and 2 GB response-cache limits are engineering targets until representative dates have been measured.

```bash
.venv/bin/python run_historical_near_miss_study.py \
  --output-format compact --start-date 2016-01-01 --end-date 2016-01-30 \
  --report-dir historical_compact_report --work-dir ais_cron_work \
  --work-reserve-bytes "$WORK_RESERVE_BYTES" --report-reserve-bytes "$REPORT_RESERVE_BYTES"
node ais_dashboard_server.js historical_compact_report
```

The study stops after 30 available dates by default. Use `--study-days` to set the study limit and `--max-days` to limit one invocation. The compact candidate export includes review-classified candidates and retained replay encounters. Add `--retain-audit-candidates` on the first run to retain other scored rows for audit exports. Use the same settings and report directory when resuming. Set `AIS_PYTHON` to the Python interpreter if the dashboard cannot find `python3`.

For compact cron runs, set `AIS_OUTPUT_FORMAT=compact`, `AIS_END_DATE`, `AIS_WORK_RESERVE_BYTES` and `AIS_REPORT_RESERVE_BYTES` in the cron environment. The wrapper accepts `AIS_START_DATE`, `AIS_STUDY_DAYS`, `AIS_REPORT_DIR` and `AIS_WORK_DIR`. It uses a separate compact report directory by default.

To import a completed legacy date, initialize the compact report with the same compact runner options and `--dry-run`, then run `.venv/bin/python migrate_legacy_ais_day.py historical_cron_report historical_compact_report 2016-01-01 --report-reserve-bytes "$REPORT_RESERVE_BYTES"`. The importer checks recorded source metadata, all three port summaries, hourly traffic and replay tracks before committing. It assigns replay samples to recorded source dates by their UTC timestamps. The importer does not re-download or hash the original AIS files. Dates without the required source checksums or hourly files need the original AIS sources or a separately verified archive. The legacy files remain in place.

Pass `--retain-audit-candidates` to the importer when that option was enabled for the compact report.

## Ubuntu cron

Install as a regular user. Push the updated source to GitHub before cloning it on the server. Use Python 3.10 or newer.

```bash
sudo apt update
sudo apt install git python3-venv cron util-linux
sudo systemctl enable --now cron
git clone https://github.com/dmsant-3/ais-near-miss-study.git ~/ais-near-miss-study
cd ~/ais-near-miss-study
python3 -m venv .venv
.venv/bin/python -m pip install zstandard
```

Run `/bin/sh ./run_cron.sh` once to verify installation. Then open your user crontab with `crontab -e` and add:

```cron
*/15 * * * * /bin/sh "$HOME/ais-near-miss-study/run_cron.sh" >> "$HOME/ais-near-miss-study/cron.log" 2>&1
```

This attempts up to one day every 15 minutes, in the server's timezone. Completed days are skipped. A failure stops the batch, and the next run retries the unfinished day from the beginning. Keep the same start date, report directory, and work directory across runs. The end date advances automatically. Run the same wrapper for manual retries to avoid overlap.

Cron mode spaces requests to each host by at least five seconds and caches NOAA indexes for one day for the current year and seven days for prior years. HTTP 429 pauses requests to the affected host for at least an hour. Temporary HTTP or connection failures pause for at least 15 minutes. A longer Retry-After value takes precedence, and cooldowns persist between runs. Missing observations can still appear as gaps. The schedule is intentionally slow for a multi-year backfill.
