# AIS and Weather Near-Miss Pipeline

The pipeline selects a daily file from NOAA's 2026 AIS index, downloads it, filters a buffered study area, detects distinct potential near-miss events, and joins historical atmospheric weather and observed visibility, waves, and currents to each event's first-flag location and time. It also saves the underlying environmental observations and a source-coverage report.

## Setup

Use Python 3.10 or newer. Keep `ais_weather_pipeline.py`, `ais_environment.py`, and `count_ais_near_misses.py` in the same folder. Install the decompressor:

```powershell
python -m pip install zstandard
```

A recent Node.js with built-in Zstandard support can be used instead through `--node PATH`. The remaining code uses Python's standard library.

HTTPS certificate verification stays enabled. If Python cannot validate the weather endpoint's certificate chain, the pipeline can use the system `curl` client with its normal certificate validation. Both clients must succeed under their own trust settings; the script never disables TLS verification. If neither works, repair the local certificate configuration before retrying.

## Run

The following example selects May 5, 2026 and an illustrative New York Harbor bounding box. Change the date and area to match your research scope. Bounding-box order is **west, south, east, north**, in decimal degrees.

```powershell
python .\ais_weather_pipeline.py --date 2026-05-05 --bbox -74.3 40.4 -73.7 40.9
```

The NOAA file covers a broad region, so the pipeline must download and read the full file even for a small study area. Downloads are cached, checked for completeness, and recorded with SHA-256 checksums. Use a disk with sufficient free space for the compressed download and temporary SQLite tables; working storage may be several times the compressed file size.

To reuse your existing AIS file and download only weather:

```powershell
python .\ais_weather_pipeline.py --date 2026-05-05 --bbox -74.3 40.4 -73.7 40.9 --ais-file "C:\Users\dmsan\OneDrive\SUNY Maritime\ais-2026-05-05.csv.zst" --output-dir .\local_ais_weather_results
```

To check the live sources and download environmental observations without downloading a full AIS file:

```powershell
python .\ais_weather_pipeline.py --date 2026-05-05 --bbox -74.3 40.4 -73.7 40.9 --check-sources
```

List available NOAA dates:

```powershell
python .\ais_weather_pipeline.py --list-dates
```

## Detection and weather matching

The defaults flag a pair when **DCPA is at most 926 meters and TCPA is greater than zero and at most six minutes**. Tracks are aligned every 30 seconds using interpolation over gaps no longer than two minutes. Successive flags for the same pair are merged when separated by no more than two minutes. At least one vessel must move at 1 knot or faster. These are adjustable research thresholds, not a confirmed near-miss classification.

The reporting area is the specified bounding box; AIS processing uses an additional buffer so nearby vessels outside the box can still participate in an encounter. Events are included when their first-flag midpoint lies inside the reporting box. Only one AIS day is processed, so encounters near midnight may be incomplete.

Weather comes from the [Open-Meteo Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api), explicitly selecting **ERA5 reanalysis** and nearest model cells rather than forcing coastal requests onto land. A configurable 0.25-degree request mesh covers the study box. Returned model-cell coordinates are retained, because they can differ from requested coordinates.

Each event is matched to the nearest returned grid location and nearest hourly timestamp. Defaults reject matches farther than 50 km or 30 minutes; match distances and signed time offsets are recorded. A positive offset means weather is later than the first flag. This is retrospective matching, not a forecast.

Downloaded variables are temperature, relative humidity, pressure, cloud cover, wind speed/direction/gusts, and precipitation. Wind units are meters per second. Precipitation is the accumulation over the preceding hour. Missing values remain missing; rejected matches contain no weather values.

**Scope:** ERA5 provides modeled atmospheric estimates, not observed conditions at the vessel. The additional NOAA observation sources below supply visibility, waves, and currents where available. Neither source type establishes that weather caused an encounter. Weather is attributed to Open-Meteo and Copernicus ERA5; retain this attribution in derived work and consult provider terms for your intended use.

## Visibility, waves, and currents

All three observation downloaders are enabled by default. They discover a limited number of nearby candidate stations, download the requested day plus one day on either side for matching, and retain missing or quality-rejected observations in an audit table. Discovery is a geographic starting point; review whether a station represents the waterway of interest.

| Source | Processing | Default event matching limits |
|---|---|---|
| NOAA NCEI GHCNh visibility | Download station/year PSV files; convert kilometers to meters; interpret source-specific legacy QC codes; preserve measurement codes | 25 km and 30 minutes |
| NOAA NDBC waves | Use annual or monthly standard meteorological archives; decode numeric missing values; retain significant height, dominant/average periods, and direction | 50 km and 60 minutes |
| NOAA CO-OPS currents | Retrieve measured current speed/direction for an explicit depth bin; convert cm/s to m/s; calculate east/north components | 10 km and 15 minutes |

These limits are configurable pilot settings, not guarantees of local representativeness. A wave buoy outside a sheltered harbor may be geographically close but physically unsuitable. Land-station visibility can differ from fog over the water. Match values are selected from usable observations inside **both** time and distance limits; a nearby station with missing or stale data does not prevent use of another eligible station. No temporal interpolation is applied to these observations.

The current downloader selects the shallowest QC-accepted metadata bin unless `--current-bin` is given. The output records the bin, depth in meters, and a warning that catalog depths are not historically verified. Deployment coordinates are selected for the observation date. Current measurements are explanatory variables; they are never added to AIS SOG/COG, which already describe motion over ground.

Where the provider supplies no per-value QC flags, the output says so rather than marking the value as QC-passed. Blank visibility quality codes are labeled unchecked, known suspect/error codes are excluded from matching, and unknown nonblank codes are rejected. Missing primary measurements are never filled with zero. Optional wave fields can remain blank even when wave height is usable.

To validate just the NOAA observation downloads, with explicit illustrative New York stations:

```powershell
python .\ais_weather_pipeline.py --date 2026-05-05 --bbox -74.3 40.4 -73.7 40.9 --check-environment --visibility-stations USW00094728 --wave-stations 44065 --current-stations n03020 --require-environment
```

The source check saves a coverage JSON and observation CSV in the cache directory. For a full run with the same stations:

```powershell
python .\ais_weather_pipeline.py --date 2026-05-05 --bbox -74.3 40.4 -73.7 40.9 --visibility-stations USW00094728 --wave-stations 44065 --current-stations n03020 --output-dir .\ny_environment_results
```

Automatic discovery considers stations with ICAO identifiers for visibility (including airport and other aviation-reporting sites), currently active NDBC meteorological stations for waves with priority given to buoys, and active/historical CO-OPS stations whose catalog deployment overlaps the analysis window. It tries two candidates per source by default. These catalogs do not guarantee that the selected stations have the requested variable or historical data. An explicit station must exist in the corresponding catalog; retired NDBC stations absent from the active catalog need a future historical-metadata extension. HF-radar gridded currents are not implemented.

Control the enabled observations with `--environment visibility waves currents`. Supply only desired names to select a subset, or pass `--environment` without values to disable all three. `--max-environment-stations` changes the automatic candidate limit. Explicit station lists override that limit.

Unavailable sources are recorded in `environment_sources.json` and the summary; by default, the pipeline continues with blank values and explicit match statuses. Add `--require-environment` to stop when an enabled source has no usable observations on the requested day. This requires source availability, not a successful match for every event. Even when source downloads succeed, individual events can be too far away or outside the matching time tolerance.

Source files are cached in UTC daily snapshots with checksums. Repeated runs on the same day reuse verified files; a later retrieval day obtains a new snapshot of changing catalogs and current-year observations.

References: [GHCNh documentation](https://www.ncei.noaa.gov/oa/global-historical-climatology-network/hourly/doc/ghcnh_DOCUMENTATION.pdf), [NDBC measurement definitions](https://www.ndbc.noaa.gov/measdes.shtml), [CO-OPS data API](https://api.tidesandcurrents.noaa.gov/api/prod/), [CO-OPS station metadata](https://api.tidesandcurrents.noaa.gov/mdapi/prod/).

## Outputs

Results are written to `ais_weather_results` by default:

| File | Contents |
|---|---|
| `events_with_weather.csv` | One row per consolidated event, first-flag coordinates, DCPA/TCPA measures, and weather-match metadata/values |
| `weather_hourly.csv` | Weather across all requested locations and hours, including next-day hours for boundary matching |
| `environment_observations.csv` | Observations from the three NOAA sources, normalized units, original quality information, usability flags, and provenance |
| `environment_sources.json` | Selected stations, per-source usable counts, download provenance, errors, and availability status |
| `summary.json` | Study-area count, match-status counts, source/checksum provenance, parameters, quality counts, and limitations |

`minimum_flagged_separation_m` is the smallest separation at qualifying grid timestamps; it is not exact hull clearance or the minimum over the complete surrounding encounter. `minimum_dcpa_m` is a prediction under constant motion. Review tracks and local operating context before describing an event as hazardous.

## Reviewing and classifying candidates

`confirm_near_misses.py` adds synchronized track evidence to each screened event. It calculates the closest observed antenna-position separation in a window around the event and looks for a course change or speed reduction in either vessel. The default rule uses a 0.1 nautical mile close-pass distance floor, a 15-degree course change, or a speed drop of at least 2 knots and 25 percent. These are configurable screening values, not a universal maritime safety definition.

Example:

```powershell
python confirm_near_misses.py `
  --ais-file "ais_weather_cache\ais\ais-2016-10-19.csv.zst" `
  --events "AIS_2016-10-19_NY_Results\events_with_weather.csv" `
  --bbox -74.3 40.4 -73.7 40.9 `
  --output "AIS_2016-10-19_NY_Results\near_miss_evidence_review.csv"
```

The output labels events `rule_supported_candidate`, `close_pass_no_maneuver_evidence`, `no_close_pass_in_review_window`, or `insufficient_track_coverage`. It includes the measurements behind the label and blank human-review fields. A `rule_supported_candidate` is not automatically a confirmed near miss: an analyst must inspect the synchronized tracks, local operating context, vessel roles, and possible data artifacts, then enter a review label and notes. AIS alone cannot provide ground-truth confirmation.

The hourly weather table includes periods with no flagged events, but the pipeline does not yet calculate vessel-hours or statistical weather-risk associations. Exposure calculations remain necessary before comparing rates under different weather conditions.

## Options and repeat runs

Run `python .\ais_weather_pipeline.py --help` for all settings. Useful options include:

- `--dcpa-meters`, `--tcpa-seconds`, `--step-seconds`, and `--merge-gap-seconds` for detection sensitivity.
- `--weather-grid-deg`, `--max-weather-distance-km`, and `--max-weather-time-seconds` for weather matching.
- `--max-visibility-distance-km`, `--max-waves-distance-km`, `--max-currents-distance-km`, and corresponding `--max-...-time-seconds` settings for independent observation matching.
- `--work-dir` for an existing directory on a disk with sufficient temporary space.
- `--cache-dir` for shared cached downloads; `--output-dir` for a new results folder.

An existing results folder is never overwritten. Choose a different `--output-dir` for each run. Failed or interrupted downloads use `.part` files and are downloaded again on retry rather than treated as complete.

Run offline tests with:

```powershell
python -m unittest test_count_ais_near_misses test_ais_weather_pipeline test_ais_environment -v
```

AIS source: [NOAA 2026 AIS index](https://noaaocm.blob.core.windows.net/ais/csv2/csv2026/index.html).
