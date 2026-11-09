# AIS Near-Miss Study

See the [pipeline guide](AIS_Weather_Pipeline_README.md) for analysis and dashboard usage.

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
17 */6 * * * /bin/sh "$HOME/ais-near-miss-study/run_cron.sh" >> "$HOME/ais-near-miss-study/cron.log" 2>&1
```

This attempts one day every six hours, in the server's timezone. Completed days are skipped. A failure stops the batch, and the next run retries the unfinished day from the beginning. Keep the same start date, report directory, and work directory across runs. The end date advances automatically. Run the same wrapper for manual retries to avoid overlap.

Cron mode spaces requests to each host by at least five seconds and caches NOAA indexes for one day for the current year and seven days for prior years. HTTP 429 pauses requests to the affected host for at least an hour. Temporary HTTP or connection failures pause for at least 15 minutes. A longer Retry-After value takes precedence, and cooldowns persist between runs. Missing observations can still appear as gaps. The schedule is intentionally slow for a multi-year backfill.
