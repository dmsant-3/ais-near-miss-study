#!/bin/sh
set -eu
cd "$(dirname "$0")"
format=${AIS_OUTPUT_FORMAT:-compact}
work_dir=${AIS_WORK_DIR:-ais_cron_work}
if [ "$format" = compact ]; then
  start_date=${AIS_START_DATE:-2016-01-01}
  end_date=${AIS_END_DATE:-2026-10-31}
  work_reserve_bytes=${AIS_WORK_RESERVE_BYTES:-20000000000}
  report_reserve_bytes=${AIS_REPORT_RESERVE_BYTES:-5000000000}
  report_dir=${AIS_REPORT_DIR:-historical_compact_reverse_report}
  set -- --output-format compact --end-date "$end_date" \
    --work-reserve-bytes "$work_reserve_bytes" \
    --report-reserve-bytes "$report_reserve_bytes" \
    --study-days "${AIS_STUDY_DAYS:-30}"
elif [ "$format" = legacy ]; then
  start_date=${AIS_START_DATE:-2026-01-01}
  report_dir=${AIS_REPORT_DIR:-historical_cron_report}
  set --
else
  echo "Invalid AIS_OUTPUT_FORMAT: $format" >&2
  exit 2
fi
exec /usr/bin/flock -n .ais-cron.lock .venv/bin/python -u run_historical_near_miss_study.py \
  --cron --start-date "$start_date" --max-days 1 --request-gap-seconds 5 \
  --report-dir "$report_dir" --work-dir "$work_dir" "$@"
