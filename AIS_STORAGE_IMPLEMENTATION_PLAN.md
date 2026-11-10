# AIS storage reduction code changes

The compact path stores 30 AIS dates for the three configured ports. Use decimal byte limits of `10_000_000_000` for retained output and `2_000_000_000` for response bodies. Set scratch reserves from measurements on the report and work volumes. See the [AIS storage reduction plan](AIS_STORAGE_REDUCTION_PLAN.md) for the design goals.

## Implementation notes

- The historical runner records the availability, source URL, byte count, and checksum of adjacent AIS files in each new daily summary. An unavailable adjacent date is recorded once per attempt.
- `ais_compact_store.py` initializes a versioned SQLite database and atomically publishes daily source provenance and three-port summary counts. Its standalone tests cover rollback, retry, duplicate dates, and invalid port sets.
- Encounter and track storage, compact runner integration, dashboard endpoints, storage limits, cache policy, migration, and parity checks remain to be implemented. Existing reports lack the new adjacent-source records. The new store is not yet used by the runner.

## Batch entry point and publishing

| File or function | Change |
| --- | --- |
| [run_historical_near_miss_study.py](run_historical_near_miss_study.py) · `parser()` | Add `--output-format {legacy,compact}` with legacy as the default. Compact mode requires an explicit `--end-date` and scratch reserves for the report and work volumes. Add `--study-days` with a compact default of 30 total committed dates and `--retain-audit-candidates` as an opt-in. Keep `--max-days` as the per-invocation limit. |
| `main()` and `rebuild_reports()` | Persist the output format, dates, study limit, port bounds and audit setting in the report configuration. Reject a compact run against a legacy directory and reject changes to the stored compact configuration on resume. In compact mode, read completed dates from SQLite and cap pending work by both day limits. Keep the legacy report builder on the legacy branch. |
| `analyze_port()` | Keep the existing detector, confirmation and ranking commands. In compact mode, put detector, evidence and score CSVs in the per-attempt work directory. Read the score CSV once for counts and compact encounter rows. Call the shared replay collector and return per-port counts, provenance and replay data to the writer. |
| `main()` and `download()` | Run space checks before fetching the main AIS file and each adjacent file. Guard streamed downloads against exhausting the reserve when the size is unknown. Record adjacent files' checksums and unavailable dates alongside the main file's provenance. Hold an exclusive writer lock for the compact report directory for the whole invocation, including stale scratch recovery. |
| [run_cron.sh](run_cron.sh) | Expose compact format, date range and measured reserve settings through environment variables. Keep `flock`, the request gap and the one-date invocation limit. Use a separate report directory for compact runs. |

Stage each port's compact records in the attempt directory to limit memory to one port at a time. Keep one transaction per AIS date. Start it after all ports finish collecting results, insert a `study_day` row and stream in its child rows, and commit only after validating all three ports and the storage limit. A pre-commit error rolls back the day. On retry, a committed day is skipped by its primary key, including when a crash happens after commit but before scratch cleanup or logging. Scope crash cleanup to compact attempt directories belonging to the same report while the writer lock is held. Release scratch after the publish attempt and reconstruct progress from the database on each read.

## SQLite schema and write contract

Add `ais_compact_store.py` with `open_writer(path)`, `completed_dates(connection)`, and `publish_day(connection, day, sources, port_results, limits)`. Put versioned DDL in this module and use `PRAGMA user_version`. Enable foreign keys on each connection, parameterize queries, and use a bounded busy timeout. Use SQLite's rollback journal for the single writer, short read transactions, and full synchronization for durable commits.

| Table | Primary key and stored values |
| --- | --- |
| `study_day` | `(ais_date)`. Completion time and scoring version. |
| `ais_source` | `(ais_date, source_date)`. URL, SHA-256, bytes and availability for the main and requested adjacent files. |
| `port_day` | `(ais_date, port)`. Detector quality and counts by classification, potential-event count, review totals, replay count, and port bounds. Include rows for zero-event ports. |
| `encounter` | `(ais_date, port, encounter_id)`. Canonical MMSI pair, first and last flags, closest approach, window bounds, classification, score, replay visibility and typed evidence. Derive the ID from the date, port, sorted MMSI pair and first-flag UTC timestamp. These fields identify one detector event and also exist in legacy scored CSVs. |
| `vessel_name` | Integer ID with a unique `(mmsi, name)` constraint. Resolve names per sample so a vessel's label can change during a replay. |
| `track_sample` | `(port, source_file_id, mmsi, tick, longitude, latitude, name_id, anchored)`. Resolve the source file ID from a lookup keyed by UTC source date and SHA-256. Keep coordinates at source precision and preserve distinct samples at the same time. Use the primary-key prefix for per-vessel time-range queries. |
| `encounter_vessel` | `(ais_date, port, encounter_id, mmsi)`. Role is `involved` or `anchored_neighbor`. |
| `traffic_hour` | `(ais_date, port, hour)`. Unique moving and anchored vessel counts. |
| `environment_observation` / `replay_hour` | Key observations by a fingerprint of the normalized station or model location, measurement, timestamp, values and source. Map `(ais_date, port, hour, measurement, station_key)` to the selected observation. Keep explicit missing slots and direction conventions. The date-and-port mapping preserves the current station and hour selection when two days select different observations for the same hour. |

Use integer UTC seconds for timestamps, `REAL` for coordinates and other measurements, and nullable integer booleans where the source can be unknown. Preserve empty versus zero values. Add an index on `(ais_date, port, replay_visible, closest_tick, encounter_id)` for encounter listing. Store the report evidence columns using the names in `REPORT_FIELDS`. Map the ranker's current reason phrases to codes at ingest and reverse the mapping for CSV and detail responses. Store unrecognized imported phrases as text so older scores remain readable.

Compute classification totals before filtering. Retain rows classified as likely or elevated for review and any rows needed by the existing replay exporter. Preserve remaining candidate detail only when the audit flag is set. The default compact candidate export therefore covers retained rows. Full legacy candidate coverage requires the audit flag at ingestion. Apply the existing candidate CSV exclusions when exporting. Preserve scores and legacy classifications from the input without recalculating them.

## Replay collection and query

In [ais_event_dashboard.py](ais_event_dashboard.py), extract `collect_replay(day, bbox, ais, scored_rows, node, adjacent)` from `export()`. Return traffic, environmental slots and provenance, selected encounters, per-encounter vessel memberships, and unique samples. Keep `export()` as the legacy CSV and HTML serializer using that result. Split the observation selection in `hourly_environment()` from its CSV and JSON writes so compact runs can consume the same values in memory.

The collector applies the current Python replay filter, including its minimum score, closest-time requirement, barge and tug exclusions, and anchored-neighbor test. Record Node visibility separately using the current Node predicate, `score_7 == 7 or score_8 == 8`. Make this predicate explicit in the legacy adapter and compact ingest so presentation and stored counts can be checked independently. Use inclusive bounds from first flag minus 1,800 seconds to closest approach plus 600 seconds. Add each selected vessel's samples to the union only when they belong to a retained window. Reconstruct a replay by joining membership to samples on port and MMSI, restricting ticks to the window and source file IDs to the files recorded for that study date. This preserves missing adjacent coverage even if a later run obtains that file. Deduplicate identical points across eligible source files and sort them in the legacy order by time, coordinates, name and anchored status. Preserve the hourly lookup for drawing and the rounded-hour wind lookup used by the legacy trajectories CSV.

## Dashboard and export contracts

Keep [ais_dashboard_server.js](ais_dashboard_server.js) as the loopback HTTP server and keep `/<date>/<port>` as the replay URL. Detect compact mode from the selected report directory's configuration. Add `ais_compact_reader.py` as a read-only SQLite CLI so compact mode works with the project's Node.js 18 baseline. Node calls the reader with validated arguments, using the project Python interpreter. Launch it with an argument array and a fixed script path. Return bounded JSON for progress, lists and single replays. Stream CSV through Node with backpressure and terminate the reader if the client disconnects.

| Route | Response |
| --- | --- |
| `GET /` and `GET /progress.html` | Shared progress view with links to completed dates and their ports. |
| `GET /api/progress` | Requested range and study limit, completed dates and port counts, last run outcome, and missing source dates known from index lookup. No trajectory fields. |
| `GET /api/days/:date/ports/:port/encounters?limit=50&cursor=…` | Node-visible encounter summaries ordered by closest UTC time then ID. Cap the page at 100 and return `next_cursor` when more rows exist. Include stable IDs, score and display labels. A completed port with zero matches returns an empty list. |
| `GET /api/days/:date/ports/:port/encounters/:id` | Encounter fields and bounded tracks keyed by MMSI, plus hourly wind and marine data for the window. Retain the renderer's `[tick, longitude, latitude, name, anchored]` sample format and time fields. |
| `GET /api/export/:kind?date=…&port=…&encounter=…` | Support `daily`, `candidates`, `likely` and `trajectories`. Require a committed date for encounter data, with optional port scope. Require a port when an encounter ID is supplied. Reuse existing column order where applicable, stream the filtered CSV, and gzip when requested by the client. |

Validate route parameters and cursor contents, return `400` for malformed requests, `404` for missing dates, ports or encounters, and `503` for a database that remains busy after the timeout. Keep each read bounded. For CSV, read ordered batches and close each query before sending its batch so a slow client cannot hold a write-blocking transaction. Use committed dates as immutable inputs to those exports.

Separate the replay drawing and controls from the current embedded `d.events` input. In compact mode, load a page of summary options, then fetch one replay by ID when selected. Pause playback while loading and cancel or ignore a stale response after the user switches encounters. Keep the existing legacy embedded-data adapter, controls, labels, zoom, wind and current arrows, and wave-height display.

## Storage limits and cache behavior

- Add a compact preflight in the runner that sums retained file sizes and checks `shutil.disk_usage()` for the work and report paths. Include measured main and adjacent downloads, detector scratch, staging, and rollback journal headroom. Before the date transaction, compute the SQLite page allowance from the retained cap minus non-database retained files and reserved metadata or log growth. Set `max_page_count`, check the projected database size before commit, and roll back on a budget or disk-full error. Measure the transient journal separately and include it in report-volume headroom. Write a bounded `budget_blocked` outcome with the date and required bytes. Leave the date pending so the next invocation can retry.
- In [ais_environment.py](ais_environment.py), extend `EnvironmentalDownloader.fetch()` with an optional compact cache policy. Route the direct wind fetch in `collect_replay()` through the same policy. Reuse the verified-body manifest in [ais_weather_pipeline.py](ais_weather_pipeline.py), including URL and checksum. Key bodies by request URL and freshness class. Keep daily freshness for catalogs, mutable metadata and current-year data. Reuse a dated historical response only when its source and query are known to be stable. Keep standalone weather-pipeline callers on their existing cache policy.
- Store compact response bodies in a dedicated cache root scoped to one compact report and protected by its writer lock. Update last access on a verified hit. Evict least recently used unpinned bodies and their manifests before promotion of a download. Pin files while parsing and use bounded scratch for oversized or uncacheable responses. Clean orphaned parts on the next start. Keep the cooldown files in [ais_request_policy.py](ais_request_policy.py) outside that cache. Atomically replace a size-bounded `run_status.json` with the latest run outcome and missing dates from index lookup. Serve that file's fields together with the SQLite totals and use bounded log rotation for diagnostics.
- Record retained database and metadata bytes per committed date, peak scratch bytes per volume, and cache high-water bytes. Keep the 30-date projection in the pilot measurements. Add compressed track blocks only if the measured footprint exceeds the limit.

## Regression tests

| Test target | Required cases |
| --- | --- |
| `test_ais_compact_store.py` | Three ports committed together, zero-event port, forced failure before commit, restart after commit, stable IDs, duplicate windows sharing samples, distinct samples at the same tick, and an adjacent-day sample queried from both sides of midnight. |
| `test_run_historical_near_miss_study.py` | Stub downloads and port analysis. Cover legacy default, compact directory validation, invocation limit versus study limit, disk preflight before download, budget failure leaving the date pending, and restart cleanup scoped to the compact run. |
| Replay and cache tests | Compare compact and legacy counts, scores and drawn samples, including scores 6–8, barge and tug exclusions, anchored neighbors, changing names, missing adjacent AIS, missing station data, and valid zero readings. Cover verified cache hits, stale and oversized bodies, eviction, interrupted downloads, and preserved cooldown state. |
| Node route tests | Serve a fixture database and a legacy report. Check stable pagination, empty and missing dates, a single bounded replay response, rapid encounter switching, CSV filtering and gzip, and a client disconnect during streaming. Check that progress and list responses contain no samples. |

Use fixed local fixtures for automated tests. Run `python -m unittest discover` and `node --check ais_dashboard_server.js`. For browser parity, compare legacy and compact output on three representative dates that include busy Houston traffic, overlapping replay windows, and a UTC midnight crossing. Treat any legacy import as a separate command that reads scored rows and replay files, uses the same ID and transaction rules, and verifies each imported date against its source before any archive cleanup.
