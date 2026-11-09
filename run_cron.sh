#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec /usr/bin/flock -n .ais-cron.lock .venv/bin/python -u run_historical_near_miss_study.py \
  --cron --start-date 2016-01-01 --max-days 1 --request-gap-seconds 5 \
  --report-dir historical_cron_report --work-dir ais_cron_work
