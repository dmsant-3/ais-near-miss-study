# Research Plan: Weather, Environmental Conditions, and Vessel Near Misses

**Status:** Draft study design  
**Prepared:** October 19, 2026  
**Pilot input:** `ais-2026-05-05.csv.zst`  
**Primary method:** AIS encounter detection followed by environmental matching and exposure-adjusted statistical analysis.

## 1. Purpose and research questions

This study will examine whether visibility, wind, waves, and currents are associated with the frequency and characteristics of potential vessel near misses. Historical Automatic Identification System (AIS) data will be used to reconstruct vessel movements and identify encounters. Historical weather and oceanographic data will then be matched to both encounters and the traffic exposed to those conditions.

The central research question is:

> Are potential near-miss rates associated with adverse weather and environmental conditions after accounting for traffic exposure, vessel mix, location, and time?

Four supporting questions guide the analysis:

1. Where and when do potential near misses occur, and what encounter geometries are involved?
2. How do associations with environmental conditions differ by vessel type and size?
3. Do selected encounters show course or speed changes consistent with avoidance?
4. How robust are the findings to detection thresholds, reporting gaps, and environmental matching choices?

The design is observational. It can estimate associations but cannot establish that weather caused an encounter or reveal bridge-team intent from AIS alone.

## 2. Outcome definition and study scope

### What will be counted

The primary outcome is a **screened encounter event**: a distinct episode in which a vessel pair meets the study's distance-at-closest-point-of-approach (DCPA) and time-to-closest-point-of-approach (TCPA) criteria. Repeated qualifying reports from the same episode count as one event.

Screened events are candidates for **potential near misses**. A contextual review will label a subset as likely potential near misses, routine close encounters, data artifacts, or uncertain. These labels will be kept separate from the reproducible screening result. A small passing distance or a low predicted DCPA alone does not establish a hazardous encounter.

The main statistical analysis will use screened event counts. Review results will indicate how often that screen captures encounters of navigational concern. If a secondary analysis uses reviewed labels, it must account for how events were selected for review.

### Area, period, and population

Begin with one port approach, channel, or coastal area and one day of AIS data to validate processing. The existing May 5, 2026 file is the pilot input. The weather analysis must then expand to multiple adverse and favorable periods; one day is insufficient to characterize a general weather association.

Select the final area and period based on overlapping AIS and environmental coverage, traffic volume, and a feasible review workload. Record the geographic boundary, vessel population, and treatment of tugs, fishing vessels, pleasure craft, and anchored vessels before final counting.

Process a spatial buffer around the reporting area and time buffers around the reporting period. Assign each event to an area and time interval using its first qualifying flag. This allows tracks to be reconstructed across boundaries while counting each event once. Mark incomplete boundary encounters when adjacent data are unavailable.

## 3. Data requirements and source selection

### AIS inputs

Preserve original downloads and record source URL, download date, covered dates, geographic extent, file size, checksum, and provider documentation. Inspect the actual schema and map it to the fields below; provider names, timestamp conventions, units, and sampling intervals may differ.

| Fields | Purpose | Requirement |
|---|---|---|
| `mmsi` | Group and identify vessel tracks | Required; preserve as an identifier |
| `timestamp_utc` | Order observations and synchronize vessels | Required; verify the source timezone |
| `longitude`, `latitude` | Reconstruct position | Required; verify coordinate reference system |
| `sog`, `cog` | Calculate motion over ground | Required by the current detection script |
| `heading` | Describe bow orientation | Optional; distinguish from course over ground |
| `length`, `width`, `vessel_type` | Interpret encounters and compare vessel groups | Needed for the corresponding secondary analyses |
| `status` | Support operational interpretation | Optional; may be stale or missing |
| `vessel_name`, `imo`, `call_sign` | Support encounter review | Optional |

For NOAA's 2025–present files, `base_date_time` supplies the timestamp. Other downloaded data can be used after schema and unit normalization. Deriving velocity from position-only data would require a separately validated preprocessing step.

### Environmental inputs

Choose products only after checking historical coverage, available variables, temporal and spatial resolution, quality controls, and licensing.

| Data need | Candidate source | Selection check |
|---|---|---|
| U.S. AIS tracks | NOAA / Marine Cadastre | Dates, coverage, schema, and reporting cadence |
| Alternative European AIS tracks | Danish Maritime Authority or EMODnet Human Activities | Historical access, licensing, and geographic suitability |
| Weather observations | NOAA National Centers for Environmental Information (NCEI) or suitable coastal stations | Visibility, wind, precipitation, and observation timing |
| Marine wind and waves | NOAA National Data Buoy Center (NDBC) archives | Station location and variables actually measured |
| Gridded waves and currents | Suitable Copernicus Marine Service products | Product-specific variables, resolution, and modeled versus observed values |

Not all buoys measure visibility, and a marine product may lack atmospheric variables. Use an appropriate atmospheric source where necessary and retain the distinction between observations and modeled estimates.

Standardize visibility to meters, wind and current speeds to meters per second, wave height to meters, and wave period to seconds. Document direction conventions and precipitation accumulation periods. Use continuous environmental variables as the primary predictors where feasible. Define any adverse-weather categories before examining their relationship with event counts.

## 4. AIS preparation and track reconstruction

Read compressed inputs as streams or chunks, filter to the buffered study area, and use disk-backed working storage for large downloads.

1. Parse timestamps and coordinates, normalize units, and decode unavailable-value codes.
2. Remove exact duplicates and flag conflicting reports for the same MMSI and timestamp. Document the resolution rule.
3. Identify implausible positions, speeds, and track jumps. Split tracks at discontinuities and long gaps.
4. Measure reporting cadence and missingness by vessel and period before choosing an analysis interval.
5. Sort each vessel's track and align tracks to common UTC timestamps. Interpolate only between sufficiently close valid observations; do not extrapolate beyond track endpoints.
6. Preserve interpolation flags, source times, gap lengths, and exclusion reasons for quality assessment.

The pilot uses a 30-second analysis interval and a maximum two-minute interpolation gap. These are provisional settings that must be checked against actual coverage. Interpolate velocity components or treat angles circularly to avoid errors at the 359-to-1-degree boundary.

Produce a quality report showing original and retained row counts, unique vessels, duplicates, exclusions, reporting intervals, and gaps. Preserve missing vessel dimensions and classifications as unknown.

## 5. Encounter detection and event counting

### Relative-motion calculation

For each aligned time step, use a spatial index or grid to find nearby pairs of distinct vessels. The search radius must cover the DCPA threshold plus the maximum distance a pair could close during the prediction horizon. Store each pair consistently as `(lower_mmsi, higher_mmsi)`.

Use coordinates and velocity components expressed in a consistent local metric frame. Convert speed with `1 knot = 1852 / 3600 meters per second`. For course over ground measured clockwise from true north:

```text
east_velocity  = speed_mps * sin(COG_radians)
north_velocity = speed_mps * cos(COG_radians)

r = position_B - position_A
v = velocity_B - velocity_A

TCPA_seconds = -dot(r, v) / dot(v, v)
DCPA_meters = norm(r + v * TCPA_seconds)
```

Handle near-zero relative velocity separately. Positive TCPA indicates a future closest approach; negative TCPA indicates it has passed. These predictions assume constant courses and speeds at the calculation time and must be recomputed as motion changes.

### Pilot settings

| Parameter | Initial value | Interpretation |
|---|---|---|
| DCPA threshold | At most 926 meters (0.5 nautical miles) | Predicted close passing distance |
| TCPA window | Greater than zero and at most 360 seconds | Closest approach within six minutes |
| Analysis interval | 30 seconds | Common track sampling interval |
| Maximum interpolation gap | 120 seconds | Longest interval bridged between valid reports |
| Event merge gap | 120 seconds | Maximum separation between successive flags in one episode |
| Minimum motion | At least one vessel moving at 1 knot or more | Suppress pairs of nearly stationary vessels |
| Speed quality limit | 99.9 knots | Upper report and segment-speed filter in the pilot script |

The DCPA/TCPA screen is a research starting point, not a universal safety standard. Keep moving-versus-anchored encounters eligible. Interpret tug assistance, berth movements, and routine channel passing during review.

### From flags to events

Merge successive qualifying flags for the same pair when the gap is within the configured limit. Start a new event after a longer interruption. Review whether track gaps justify splitting an episode even when the time-based rule would merge it.

Count each consolidated event once at its first flag. Preserve first and last flag times, flag count, minimum predicted DCPA and its timestamp, and TCPA at that same timestamp. Do not combine minima from different times into an artificial vessel state.

Separate the following distance measures in outputs:

- **Minimum predicted DCPA:** Smallest constant-motion passing-distance prediction during the flagged episode.
- **Minimum flagged separation:** Smallest separation at qualifying analysis timestamps; available from the current script.
- **Reconstructed encounter minimum:** Closest approach over the surrounding track segments; a planned enhancement for contextual review.

AIS positions describe antenna locations. These distances do not establish exact hull clearance.

## 6. Environmental matching and traffic exposure

### Common spatial and temporal framework

Define analysis cells and time intervals that are supported by environmental resolution and event frequency. An hourly coastal-area analysis is a possible starting point, subject to the selected products. Use the same boundaries, quality rules, and environmental matching framework for event counts and traffic exposure.

For an event, use the vessel-pair midpoint at the first flag as its primary location and the first-flag timestamp as its reference time. Retain later closest-approach locations and times for review and sensitivity checks.

For exposure, accumulate valid reconstructed vessel time within each cell and interval. Allocate time across boundaries rather than counting entire tracks in each cell they touch. Divide supported vessel-seconds by 3,600 to obtain vessel-hours. Retain intervals with traffic and zero events; distinguish them from intervals lacking usable AIS coverage.

### Matching procedure

1. Standardize environmental timestamps to UTC and identify whether values are instantaneous, averaged, or accumulated.
2. Match events and exposure to suitable stations or grid cells using prespecified distance and time tolerances.
3. Assess local representativeness, including shoreline barriers, station exposure, and spatial variability. The nearest station may not be suitable.
4. Interpolate only when supported by the product. Retain source identifiers, observation times, spatial distances, time offsets, and quality flags for each variable.
5. Keep unmatched conditions as missing and report unmatched events and vessel-hours. Missing weather must not be classified as favorable weather.

Match conditions to eligible traffic even when no event occurs. An event-only weather table cannot establish a higher rate under adverse conditions. Use closest-approach timing and preceding weather windows as secondary matching choices.

## 7. Validation and sensitivity analysis

### Algorithm and encounter validation

Test relative-motion calculations and event consolidation with controlled head-on, crossing, overtaking, parallel, diverging, moving-versus-anchored, and reporting-gap examples. Check units, angle conventions, TCPA signs, geographic boundaries, and duplicate handling.

Review all highest-ranked candidates plus a random sample of other flagged events and unflagged encounters. Plot both tracks with synchronized timestamps, speed/course histories, DCPA/TCPA histories, and matched environmental conditions. Record the review selection method; where feasible, use a second reviewer for a subset.

Use the labels `likely potential near miss`, `routine close encounter`, `data artifact`, and `uncertain`. Report agreement and the proportion of reviewed flags judged relevant. Without an independent reference set, do not claim detection of every near miss or measured recall.

### Robustness checks

| Choice | Alternatives to evaluate |
|---|---|
| DCPA threshold | 0.1, 0.25, and 0.5 nautical miles; consider 1 nautical mile only with an area-specific justification |
| TCPA horizon | 3, 6, and 10 minutes |
| Analysis interval | 15, 30, and 60 seconds where reporting cadence supports them |
| Maximum interpolation gap | 60 and 120 seconds where coverage supports them |
| Event merge gap | 60, 120, and 180 seconds |
| Spatial risk criterion | Fixed threshold versus a documented vessel-size-dependent ship domain |
| Environmental matching | Alternative justified time/distance tolerances, reference times, and products |
| Exposure denominator | Vessel-hours versus independently defined eligible pair encounters |

Vary individual choices first, then selected combinations. Report changes in counts, reviewed relevance, hotspot locations, and estimated weather associations. A chosen ship domain is a modeling assumption, not a proven safe boundary.

## 8. Statistical analysis and interpretation

The primary analysis will model distinct screened event counts in each area–time interval using eligible vessel-hours as exposure. Report descriptive rates per 1,000 vessel-hours together with event counts, exposure, and uncertainty intervals. A secondary rate per eligible pair encounter addresses a different question and requires an eligibility definition independent of the near-miss screen.

Follow this sequence:

1. Summarize AIS and weather coverage, vessel mix, traffic exposure, and event distributions.
2. Compare rates across prespecified weather categories and examine continuous relationships with visibility, wind, waves, and currents.
3. If the sample supports modeling, fit a Poisson count model with a log-exposure offset. Check overdispersion and consider a negative binomial model when justified. Exclude zero-exposure intervals from this rate model while retaining them in coverage reporting.
4. Account for available location, season, time-of-day, traffic, and vessel-mix factors. Check correlated environmental predictors and nonlinear relationships.
5. Assess dependence within days, weather episodes, and repeated vessel pairs. Use suitable clustered uncertainty estimates or block resampling rather than treating each report or storm hour as independent.
6. Examine a limited set of prespecified vessel-type or size differences, then repeat key estimates under the sensitivity choices in Section 7.

Use DCPA, reconstructed separation, relative speed, and maneuver evidence to describe encounters. Do not equate DCPA alone with accident severity. Report uncertain and null findings alongside positive associations.

Interpret hotspots against traffic exposure and coverage. Weather may change route choice, traffic volume, and the vessels that remain underway. Discuss these selection effects and distinguish overall associations from estimates conditional on traffic characteristics. Proposed mechanisms involving lookout, workload, ship handling, or sensor performance require supporting literature and encounter evidence.

## 9. Outputs and reproducibility

Maintain three linked analytical datasets:

| Dataset | Contents |
|---|---|
| Event catalog | Event and pair identifiers; flag times; reference location; DCPA/TCPA; separation measures; vessel attributes; review labels; configuration ID |
| Environmental match table | Event or exposure reference; variable values and units; source/station/grid IDs; match distances and time offsets; quality and missingness flags |
| Area–time exposure table | Event counts, valid vessel-hours, traffic/vessel composition, environmental conditions, and coverage indicators, including zero-event intervals |

Retain pairwise time series and source observations so calculations can be traced. Record configuration, software versions, checksums, processing logs, and exclusion rules for each run. Preserve original downloads; use SQLite or Parquet working datasets as appropriate to scale.

The final deliverables are:

- An AIS and environmental data-quality report.
- A reproducible event catalog and linked exposure and weather tables.
- Maps of traffic exposure and event rates, weather-stratified rate plots, and selected encounter reconstructions.
- A sensitivity table and statistical results with uncertainty intervals.
- A research paper explaining methods, findings, and limitations.

Use Python for processing and analysis. The existing script uses streaming decompression and SQLite. Additional tools may include pandas or Polars for tables, pyproj and spatial tools for coordinates, xarray for gridded environmental products, and plotting and statistical libraries as needed.

## 10. Work plan and current status

| Phase | Work | Completion criterion |
|---|---|---|
| 1. Scope and source selection | Choose area, period, population, and overlapping environmental products | Configuration and coverage documented |
| 2. Pilot preparation | Clean and synchronize the one-day AIS input | Quality report and representative tracks reviewed |
| 3. Detection and review | Compute flags, consolidate events, and inspect candidates | Calculation checks pass and review labels are recorded |
| 4. Environmental integration | Match conditions to events and eligible exposure | Matching diagnostics and zero-event intervals verified |
| 5. Expansion and analysis | Include multiple weather episodes; estimate rates and associations | Dependence, confounding, and sensitivity checks documented |
| 6. Reporting | Produce final tables, figures, and paper | Results reproducible and conclusions supported by the design |

**Implemented:** `count_ais_near_misses.py` performs the initial DCPA/TCPA screen and time-based event consolidation and records first-flag locations. `ais_weather_pipeline.py` adds NOAA downloads, area filtering, historical ERA5 weather downloads, and event-to-weather matching. Its `ais_environment.py` module downloads observed GHCNh visibility, NDBC waves, and CO-OPS currents with station discovery, quality handling, unit conversions, independent matching, and gap reporting. All 23 detector and pipeline tests pass. The detector has processed a 10,000-record sample; live checks have verified NOAA AIS, ERA5 weather, and all three observational sources for the May 5, 2026 New York example.

**Remaining:** Run and review the full pilot, add exposure calculations, preserve additional vessel metadata, and perform the statistical analysis. Review environmental station representativeness and historical depth metadata; add historical NDBC station discovery or gridded HF-radar currents where the study requires them. Full encounter-minimum reconstruction and geometric classification are also planned. The full-day count has not yet been run.

The study is complete when events and exposure are traceable, environmental coverage supports the stated comparisons, validation and sensitivity results are reported, and the conclusions reflect the limits of an observational design.

## 11. Paper structure

1. **Introduction:** Maritime safety context, environmental hypotheses, research gap, and questions.
2. **Data and methods:** Study scope, source quality, track reconstruction, event definition, weather matching, exposure, validation, and statistical model.
3. **Results:** Coverage and descriptive findings, exposure-adjusted comparisons, vessel-group differences, and robustness checks.
4. **Discussion:** Navigational interpretation, alternative explanations, study limitations, and proportionate operational implications.
5. **Conclusion:** Findings supported by the evidence and priorities for further validation or expansion.

## 12. Methodological references

1. [NOAA AIS Broadcast Points Data Dictionaries](https://coast.noaa.gov/data/marinecadastre/ais/data-dictionary.pdf). Field definitions and units; page 2 covers the 2025–present schema.
2. [NOAA AIS Data and Tools FAQ](https://coast.noaa.gov/data/marinecadastre/ais/faq.pdf). Data characteristics and code guidance; check applicability to the source version.
3. [U.S. Coast Guard AIS overview](https://www.navcen.uscg.gov/automatic-identification-system-overview). AIS information and closest-point-of-approach concepts.
4. [Do maritime safety actions deliver lasting risk reduction? Evidence from AIS-based near-miss analysis using difference-in-differences](https://www.frontiersin.org/journals/marine-science/articles/10.3389/fmars.2026.1823394/full). Example use of the 0.5-nautical-mile DCPA and six-minute TCPA screen.
5. [A ship domain-based model of collision risk for near-miss detection and Collision Alert Systems](https://www.sciencedirect.com/science/article/pii/S0951832021002945). Domain-based encounter-risk methods.
6. [PNNL: AIS-Based Characterization of Navigation Conflicts along the US Atlantic Coast prior to Development of Wind Energy](https://www.pnnl.gov/publications/ais-based-characterization-navigation-conflicts-along-us-atlantic-coast-prior). Pairwise encounter analysis and actual versus potential conflicts.

Add the selected environmental products' documentation and mechanism-specific literature when source selection is complete.
