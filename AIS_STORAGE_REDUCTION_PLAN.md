# AIS storage reduction plan

## Goal and scope

Run the three-port study for 30 AIS dates with a provisional target of **10 GB of retained output**, plus **2 GB of cache** and a separate measured allowance for temporary processing. Treat these as engineering targets until a representative pilot establishes what fits. If “30 days” means 30 calendar days of unattended operation, the 15-minute cron schedule can process substantially more than 30 AIS dates. Set an explicit end date for a 30-date study and enforce the storage budget independently of the schedule.

Keep the progress page, review counts, vessel names, replay controls, wind and current arrows, wave height, and provenance available. This plan changes storage and delivery. Existing scoring thresholds and vessel exclusions carry forward.

## First measure what is growing

Run these commands from the Ubuntu checkout to separate retained reports, cached responses, logs, and scratch space:

```bash
du -h -d 1 . | sort -h
du -h -d 2 historical_cron_report | sort -h | tail -25
du -h -d 3 ais_weather_cache | sort -h | tail -25
df -h .
```

Record bytes by file type for a completed day at each port and measure peak disk use during a run. In the current code, a track point can appear in multiple overlapping encounter windows, then appear again in both `event_trajectories.csv` and JSON embedded in `event_dashboard.html`. Per-port candidate rows also appear at multiple processing stages and again in aggregate CSVs that the runner rewrites. The environmental cache creates dated snapshots, so the same source response can occupy multiple files. These are code-level sources of duplication. Use the disk inventory to determine how much each contributed to the observed 50 GB.

## Recommended layout

| Output | Store | Read from the dashboard |
|---|---|---|
| Encounter data | One typed SQLite row per retained encounter, keyed by date, port and stable encounter ID. Keep scores, numeric evidence, reason codes, and coverage fields. Keep low-score or excluded rows only if needed for an explicitly enabled audit export. Keep screening and exclusion totals for every day. | Page the encounter list by date and port. Fetch individual encounter details when selected. |
| Trajectories | Store each vessel’s samples once per port and day, for the union of retained replay windows. Store encounter-to-vessel membership and window bounds separately, including nearby anchored vessels. | Fetch only the selected encounter’s vessels and time range. Preserve the first-flag and closest-approach times, and resolve cross-midnight windows against both dates. |
| Traffic and environment | Store each hourly record once per port and station. Keep observation time, direction convention, location, depth where applicable, and missing-data status. | Join by replay hour. Reuse the same wind and current records across encounters. |
| HTML and CSV | Keep one small dashboard shell and one small progress page. Generate CSV extracts for a date, port, or encounter on demand and stream them. Offer gzip for larger exports. | Keep the normal browser replay. Use the existing Node server to serve the shell and JSON responses from the store. |

Use a vessel lookup for names and IDs. Use integer UTC timestamps, integer coordinate microdegrees if validated against source precision, and integer flags such as `0` and `1` with null for unknown values. Store scoring explanations as reason codes and expand them to readable labels at display time. A one-byte flag saves space, but removing repeated rows and embedded trajectories should be tested first because it addresses multiple copies of the same data.

Start with SQLite through Python’s standard library. For the web layer, choose a SQLite reader supported by the deployed Node runtime or use a small Python reader behind the local server. Keep the current HTML renderer during migration and change its data source to a per-encounter request. Add compressed binary track blocks only if the measured SQLite footprint exceeds the budget. Test packing and compression before considering lower trajectory sampling rates. Any later change in precision or sampling requires checking that closest-approach highlights and vessel motion still match the original replay.

## Retention and restart

1. **Publish once per day.** Write each day’s retained records and completion marker in one transaction. Retry unfinished days by their date and port keys so a restart cannot duplicate rows. Build the progress page from completed-day counts. Keep the existing cron lock and API cooldown state.
2. **Remove intermediates after verification.** Keep detector and evidence CSVs, decompression scratch files, and downloaded AIS only while processing. Delete them after the final records and completion marker are durable. Keep source URLs, file checksums, scoring version, port bounds, and compact run outcomes for reproducibility.
3. **Bound the cache and logs.** Key cached responses by source request and reuse historical responses across days where safe. Cap cached bodies at 2 GB, evicting the oldest unused entries. Keep cooldowns and completion state outside the eviction policy. Rotate verbose logs and retain compact download, error, and success records for the study.
4. **Protect the disk before work starts.** Estimate the next day’s scratch use from measured peaks, including adjacent AIS dates and temporary database files. Stop before downloading when free space or the retained-output cap would be exceeded, record the reason in the progress log, and resume once space is available. Avoid keeping a complete set of expanded exports alongside the database.

## Rollout and acceptance

1. **Quick savings first.** Add a compact-output mode that skips persistent intermediate and aggregate CSV copies and writes temporary files outside the report directory. Keep existing output readable until the replacement has been verified.
2. **Move replay data into the shared store.** Compare old and new results on at least three representative dates, including busy Houston traffic, overlapping replays, and a midnight crossing. Verify totals and scores, vessel names and positions, hourly traffic and environmental values, replay timing, and missing-data behavior. Test an interruption and a restart.
3. **Measure against the budget.** The 10 GB retained-output target allows roughly **330 MB per AIS date across all three ports**, including indexes and metadata. Report actual retained bytes per date, peak scratch bytes, and the projected 30-date total. Verify that opening the latest progress page and starting a replay do not require loading all 30 days of data.
4. **Migrate existing output separately.** Convert and verify a day at a time against the existing files. If the disk is already near capacity, use a separate volume for migration or an archive. Remove the old expanded copies only after checking the migrated output and agreeing on whether to keep an audit archive.

No storage format or existing output changes are part of this plan.
