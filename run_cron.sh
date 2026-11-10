#!/bin/sh
set -eu
cd "$(dirname "$0")"
format=${AIS_OUTPUT_FORMAT:-legacy}
start_date=${AIS_START_DATE:-2016-01-01}
work_dir=${AIS_WORK_DIR:-ais_cron_work}
if [ "$format" = compact ]; then
  : "${AIS_END_DATE:?Set AIS_END_DATE for a compact study}"
  : "${AIS_WORK_RESERVE_BYTES:?Set the measured work-volume scratch reserve}"
  : "${AIS_REPORT_RESERVE_BYTES:?Set the measured report-volume scratch reserve}"
  report_dir=${AIS_REPORT_DIR:-historical_compact_report}
  set -- --output-format compact --end-date "$AIS_END_DATE" \
    --work-reserve-bytes "$AIS_WORK_RESERVE_BYTES" \
    --report-reserve-bytes "$AIS_REPORT_RESERVE_BYTES" \
    --study-days "${AIS_STUDY_DAYS:-30}"
elif [ "$format" = legacy ]; then
  report_dir=${AIS_REPORT_DIR:-historical_cron_report}
  set --
else
  echo "Invalid AIS_OUTPUT_FORMAT: $format" >&2
  exit 2
fi
exec /usr/bin/flock -n .ais-cron.lock .venv/bin/python -u run_historical_near_miss_study.py \
  --cron --start-date "$start_date" --max-days 1 --request-gap-seconds 5 \
  --report-dir "$report_dir" --work-dir "$work_dir" "$@"
