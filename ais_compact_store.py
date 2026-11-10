"""SQLite storage primitives for completed compact AIS study dates.

The compact runner will use this module to publish each date in a single
transaction.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

SCHEMA_VERSION = 8

NUMERIC_FIELDS = (
    'flag_count', 'minimum_flagged_separation_m', 'minimum_dcpa_m',
    'tcpa_at_minimum_dcpa_s', 'tcpa_at_first_flag_s', 'first_flag_longitude',
    'first_flag_latitude', 'baseline_course_difference_deg',
    'observed_min_separation_in_window_m', 'distance_threshold_used_m',
    'ship_length_a_near_pass_m', 'ship_length_b_near_pass_m',
    'synchronized_baseline_samples', 'synchronized_encounter_samples',
    'max_course_change_a_deg', 'max_course_change_b_deg',
    'max_speed_drop_a_knots', 'max_speed_drop_b_knots',
)
FLAG_FIELDS = ('involves_excluded_vessel', 'involves_tugboat',
               'maneuver_evidence_a', 'maneuver_evidence_b')
INTEGER_FIELDS = ('flag_count', 'synchronized_baseline_samples', 'synchronized_encounter_samples')

SCHEMA = """
CREATE TABLE study_day (
    ais_date TEXT PRIMARY KEY,
    completed_utc TEXT NOT NULL,
    scoring_version TEXT NOT NULL
);
CREATE TABLE ais_source (
    ais_date TEXT NOT NULL REFERENCES study_day(ais_date),
    source_date TEXT NOT NULL,
    available INTEGER NOT NULL CHECK (available IN (0, 1)),
    source_url TEXT,
    sha256 TEXT,
    compressed_bytes INTEGER CHECK (compressed_bytes >= 0),
    PRIMARY KEY (ais_date, source_date),
    CHECK ((available = 0 AND source_url IS NULL AND sha256 IS NULL
            AND compressed_bytes IS NULL)
        OR (available = 1 AND source_url IS NOT NULL AND sha256 IS NOT NULL
            AND compressed_bytes IS NOT NULL))
);
CREATE TABLE port_day (
    ais_date TEXT NOT NULL REFERENCES study_day(ais_date),
    port TEXT NOT NULL,
    potential_events INTEGER NOT NULL CHECK (potential_events >= 0),
    likely_for_human_review INTEGER NOT NULL CHECK (likely_for_human_review >= 0),
    elevated_priority_candidates INTEGER NOT NULL CHECK (elevated_priority_candidates >= 0),
    lower_priority_candidates INTEGER NOT NULL CHECK (lower_priority_candidates >= 0),
    detector_quality TEXT NOT NULL,
    bbox_json TEXT NOT NULL,
    PRIMARY KEY (ais_date, port)
);
"""

REPLAY_SCHEMA = """
CREATE TABLE source_file (
    source_date TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (source_date, sha256)
);
CREATE TABLE track_sample (
    port TEXT NOT NULL,
    source_date TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    mmsi TEXT NOT NULL,
    tick INTEGER NOT NULL,
    longitude REAL NOT NULL,
    latitude REAL NOT NULL,
    name TEXT NOT NULL,
    anchored INTEGER NOT NULL CHECK (anchored IN (0, 1)),
    PRIMARY KEY (port, source_date, sha256, mmsi, tick, longitude, latitude, name, anchored),
    FOREIGN KEY (source_date, sha256) REFERENCES source_file(source_date, sha256)
) WITHOUT ROWID;
CREATE TABLE encounter (
    ais_date TEXT NOT NULL,
    port TEXT NOT NULL,
    encounter_id TEXT NOT NULL,
    mmsi_a TEXT NOT NULL,
    mmsi_b TEXT NOT NULL,
    first_flag_utc TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    window_end INTEGER NOT NULL CHECK (window_end >= window_start),
    details_json TEXT NOT NULL,
    PRIMARY KEY (ais_date, port, encounter_id),
    FOREIGN KEY (ais_date, port) REFERENCES port_day(ais_date, port)
) WITHOUT ROWID;
CREATE TABLE encounter_vessel (
    ais_date TEXT NOT NULL,
    port TEXT NOT NULL,
    encounter_id TEXT NOT NULL,
    mmsi TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('involved', 'anchored_neighbor')),
    PRIMARY KEY (ais_date, port, encounter_id, mmsi),
    FOREIGN KEY (ais_date, port, encounter_id)
        REFERENCES encounter(ais_date, port, encounter_id)
) WITHOUT ROWID;
"""

HOURLY_SCHEMA = """
CREATE TABLE traffic_hour (
    ais_date TEXT NOT NULL,
    port TEXT NOT NULL,
    hour INTEGER NOT NULL,
    unique_moving INTEGER NOT NULL CHECK (unique_moving >= 0),
    unique_anchored INTEGER NOT NULL CHECK (unique_anchored >= 0),
    PRIMARY KEY (ais_date, port, hour),
    FOREIGN KEY (ais_date, port) REFERENCES port_day(ais_date, port)
) WITHOUT ROWID;
CREATE TABLE environment_hour (
    ais_date TEXT NOT NULL,
    port TEXT NOT NULL,
    hour INTEGER NOT NULL,
    measurement TEXT NOT NULL,
    station_key TEXT NOT NULL,
    station_id TEXT,
    latitude REAL,
    longitude REAL,
    observation_utc TEXT,
    speed_mps REAL,
    direction_deg REAL,
    direction_convention TEXT,
    wave_height_m REAL,
    current_depth_m REAL,
    PRIMARY KEY (ais_date, port, hour, measurement, station_key),
    FOREIGN KEY (ais_date, port) REFERENCES port_day(ais_date, port)
) WITHOUT ROWID;
CREATE TABLE environment_source (
    ais_date TEXT NOT NULL,
    port TEXT NOT NULL,
    report_json TEXT NOT NULL,
    PRIMARY KEY (ais_date, port),
    FOREIGN KEY (ais_date, port) REFERENCES port_day(ais_date, port)
) WITHOUT ROWID;
"""

MEASUREMENT_SCHEMA = """
CREATE TABLE storage_measurement (
    ais_date TEXT PRIMARY KEY REFERENCES study_day(ais_date),
    retained_before INTEGER NOT NULL,
    retained_after INTEGER NOT NULL,
    work_scratch_sampled_peak INTEGER NOT NULL,
    report_scratch_sampled_peak INTEGER NOT NULL,
    cache_body_peak INTEGER NOT NULL,
    sample_interval_seconds REAL NOT NULL
) WITHOUT ROWID;
"""

EVIDENCE_SCHEMA = """
ALTER TABLE encounter ADD COLUMN score_7 INTEGER;
ALTER TABLE encounter ADD COLUMN score_8 INTEGER;
ALTER TABLE encounter ADD COLUMN classification TEXT;
ALTER TABLE encounter ADD COLUMN closest_tick REAL;
ALTER TABLE encounter ADD COLUMN reasons_present INTEGER NOT NULL DEFAULT 0;
UPDATE encounter SET
    score_7 = CAST(NULLIF(json_extract(details_json, '$.likely_near_miss_score_7'), '') AS INTEGER),
    score_8 = CAST(NULLIF(json_extract(details_json, '$.likely_near_miss_score_8'), '') AS INTEGER),
    classification = json_extract(details_json, '$.likely_near_miss_classification'),
    closest_tick = CAST(strftime('%s', json_extract(details_json, '$.distance_time_utc')) AS REAL);
CREATE INDEX encounter_listing ON encounter(ais_date, port, closest_tick, encounter_id);
CREATE TABLE score_reason (
    reason_id INTEGER PRIMARY KEY,
    description TEXT NOT NULL UNIQUE
);
CREATE TABLE encounter_reason (
    ais_date TEXT NOT NULL,
    port TEXT NOT NULL,
    encounter_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    reason_id INTEGER NOT NULL REFERENCES score_reason(reason_id),
    PRIMARY KEY (ais_date, port, encounter_id, position),
    FOREIGN KEY (ais_date, port, encounter_id)
        REFERENCES encounter(ais_date, port, encounter_id)
) WITHOUT ROWID;
"""

NUMERIC_SCHEMA = (
    ''.join(f'ALTER TABLE encounter ADD COLUMN {field} '
            f'{"INTEGER" if field in INTEGER_FIELDS else "REAL"};\n' for field in NUMERIC_FIELDS)
    + ''.join(f'ALTER TABLE encounter ADD COLUMN {field} INTEGER;\n' for field in FLAG_FIELDS)
    + 'UPDATE encounter SET '
    + ', '.join(f"{field} = compact_number(json_extract(details_json, '$.{field}'))"
                for field in NUMERIC_FIELDS)
    + ', '
    + ', '.join(f"{field} = compact_flag(json_extract(details_json, '$.{field}'))"
                for field in FLAG_FIELDS)
    + ';\n'
)

ENVIRONMENT_SCHEMA = """
CREATE TABLE environment_observation (
    observation_id INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    port TEXT NOT NULL,
    hour INTEGER NOT NULL,
    measurement TEXT NOT NULL,
    station_id TEXT,
    latitude REAL,
    longitude REAL,
    observation_utc TEXT,
    speed_mps REAL,
    direction_deg REAL,
    direction_convention TEXT,
    wave_height_m REAL,
    current_depth_m REAL
);
CREATE TABLE replay_hour (
    ais_date TEXT NOT NULL,
    port TEXT NOT NULL,
    hour INTEGER NOT NULL,
    measurement TEXT NOT NULL,
    station_key TEXT NOT NULL,
    observation_id INTEGER NOT NULL REFERENCES environment_observation(observation_id),
    PRIMARY KEY (ais_date, port, hour, measurement, station_key),
    FOREIGN KEY (ais_date, port) REFERENCES port_day(ais_date, port)
) WITHOUT ROWID;
INSERT OR IGNORE INTO environment_observation
    (fingerprint, port, hour, measurement, station_id, latitude, longitude, observation_utc,
     speed_mps, direction_deg, direction_convention, wave_height_m, current_depth_m)
    SELECT compact_environment_hash(port, hour, measurement, station_id, latitude, longitude,
        observation_utc, speed_mps, direction_deg, direction_convention, wave_height_m, current_depth_m),
        port, hour, measurement, station_id, latitude, longitude, observation_utc,
        speed_mps, direction_deg, direction_convention, wave_height_m, current_depth_m
    FROM environment_hour;
INSERT INTO replay_hour
    SELECT e.ais_date, e.port, e.hour, e.measurement, e.station_key, o.observation_id
    FROM environment_hour e JOIN environment_observation o ON o.fingerprint =
        compact_environment_hash(e.port, e.hour, e.measurement, e.station_id, e.latitude, e.longitude,
            e.observation_utc, e.speed_mps, e.direction_deg, e.direction_convention, e.wave_height_m, e.current_depth_m);
DROP TABLE environment_hour;
CREATE VIEW environment_hour AS
    SELECT r.ais_date, r.port, r.hour, r.measurement, r.station_key, o.station_id,
           o.latitude, o.longitude, o.observation_utc, o.speed_mps, o.direction_deg,
           o.direction_convention, o.wave_height_m, o.current_depth_m
    FROM replay_hour r JOIN environment_observation o ON o.observation_id = r.observation_id;
"""

VESSEL_SCHEMA = """
CREATE TABLE vessel_name (
    name_id INTEGER PRIMARY KEY,
    mmsi TEXT NOT NULL,
    name TEXT NOT NULL,
    UNIQUE (mmsi, name)
);
INSERT INTO vessel_name (mmsi, name)
    SELECT DISTINCT mmsi, name FROM track_sample;
CREATE TABLE track_sample_new (
    port TEXT NOT NULL,
    source_date TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    mmsi TEXT NOT NULL,
    tick INTEGER NOT NULL,
    longitude REAL NOT NULL,
    latitude REAL NOT NULL,
    name_id INTEGER NOT NULL REFERENCES vessel_name(name_id),
    anchored INTEGER NOT NULL CHECK (anchored IN (0, 1)),
    PRIMARY KEY (port, source_date, sha256, mmsi, tick, longitude, latitude, name_id, anchored),
    FOREIGN KEY (source_date, sha256) REFERENCES source_file(source_date, sha256)
) WITHOUT ROWID;
INSERT INTO track_sample_new
    SELECT t.port, t.source_date, t.sha256, t.mmsi, t.tick, t.longitude, t.latitude, n.name_id, t.anchored
    FROM track_sample t JOIN vessel_name n ON n.mmsi=t.mmsi AND n.name=t.name;
DROP TABLE track_sample;
ALTER TABLE track_sample_new RENAME TO track_sample;
"""


def _number(value):
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def _flag(value):
    text = str(value).strip().lower()
    if text in ('true', '1', '1.0', 'yes'):
        return 1
    if text in ('false', '0', '0.0', 'no'):
        return 0
    return None


def _environment_hash(*values):
    normalized = list(values)
    for index in (4, 5, 7, 8, 10, 11):
        if normalized[index] is not None:
            normalized[index] = float(normalized[index])
    payload = json.dumps(normalized, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def open_writer(path: Path, max_bytes: int | None = None) -> sqlite3.Connection:
    """Open or create a versioned database with durable single-writer settings."""
    connection = sqlite3.connect(path, timeout=5)
    try:
        connection.create_function('compact_number', 1, _number)
        connection.create_function('compact_flag', 1, _flag)
        connection.create_function('compact_environment_hash', 12, _environment_hash)
        connection.execute('PRAGMA foreign_keys = ON')
        connection.execute('PRAGMA journal_mode = DELETE')
        connection.execute('PRAGMA synchronous = FULL')
        if max_bytes is not None:
            page_size = connection.execute('PRAGMA page_size').fetchone()[0]
            allowed_pages = max_bytes // page_size
            current_pages = connection.execute('PRAGMA page_count').fetchone()[0]
            if allowed_pages < max(1, current_pages):
                raise OSError('Compact retained-output limit reached before database initialization')
            connection.execute(f'PRAGMA max_page_count = {allowed_pages}')
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version == 0:
            existing = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' LIMIT 1"
            ).fetchone()
            if existing:
                raise ValueError('Unversioned database is not an empty compact store')
            connection.executescript(
                'BEGIN IMMEDIATE;\n' + SCHEMA + REPLAY_SCHEMA + HOURLY_SCHEMA + MEASUREMENT_SCHEMA + EVIDENCE_SCHEMA + NUMERIC_SCHEMA + ENVIRONMENT_SCHEMA + VESSEL_SCHEMA
                + f'\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;'
            )
        elif version == 1:
            connection.executescript(
                'BEGIN IMMEDIATE;\n' + REPLAY_SCHEMA + HOURLY_SCHEMA + MEASUREMENT_SCHEMA + EVIDENCE_SCHEMA + NUMERIC_SCHEMA + ENVIRONMENT_SCHEMA + VESSEL_SCHEMA
                + f'\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;'
            )
        elif version == 2:
            connection.executescript(
                'BEGIN IMMEDIATE;\n' + HOURLY_SCHEMA + MEASUREMENT_SCHEMA + EVIDENCE_SCHEMA + NUMERIC_SCHEMA + ENVIRONMENT_SCHEMA + VESSEL_SCHEMA
                + f'\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;'
            )
        elif version == 3:
            connection.executescript(
                'BEGIN IMMEDIATE;\n' + MEASUREMENT_SCHEMA + EVIDENCE_SCHEMA + NUMERIC_SCHEMA + ENVIRONMENT_SCHEMA + VESSEL_SCHEMA
                + f'\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;'
            )
        elif version == 4:
            connection.executescript(
                'BEGIN IMMEDIATE;\n' + EVIDENCE_SCHEMA + NUMERIC_SCHEMA + ENVIRONMENT_SCHEMA + VESSEL_SCHEMA
                + f'\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;'
            )
        elif version == 5:
            connection.executescript(
                'BEGIN IMMEDIATE;\n' + NUMERIC_SCHEMA + ENVIRONMENT_SCHEMA + VESSEL_SCHEMA
                + f'\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;'
            )
        elif version == 6:
            connection.executescript(
                'BEGIN IMMEDIATE;\n' + ENVIRONMENT_SCHEMA + VESSEL_SCHEMA
                + f'\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;'
            )
        elif version == 7:
            connection.executescript(
                'BEGIN IMMEDIATE;\n' + VESSEL_SCHEMA
                + f'\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;'
            )
        elif version != SCHEMA_VERSION:
            raise ValueError(f'Unsupported compact store schema version: {version}')
        return connection
    except Exception:
        connection.close()
        raise


def completed_dates(connection: sqlite3.Connection) -> set[date]:
    """Return only dates committed to the store."""
    return {date.fromisoformat(row[0]) for row in
            connection.execute('SELECT ais_date FROM study_day')}


def record_measurement(connection: sqlite3.Connection, day: date, retained_before: int,
                       non_database_bytes: int, work_peak: int, report_peak: int,
                       cache_peak: int, sample_interval: float) -> None:
    """Record post-publication storage and sampled scratch high-water marks."""
    with connection:
        connection.execute(
            'INSERT INTO storage_measurement VALUES (?, ?, 0, ?, ?, ?, ?)',
            (day.isoformat(), retained_before, work_peak, report_peak, cache_peak, sample_interval),
        )
        pages = connection.execute('PRAGMA page_count').fetchone()[0]
        page_size = connection.execute('PRAGMA page_size').fetchone()[0]
        connection.execute('UPDATE storage_measurement SET retained_after=? WHERE ais_date=?',
                           (pages * page_size + non_database_bytes, day.isoformat()))


def encounter_id(day: date, port: str, mmsi_a: str, mmsi_b: str,
                 first_flag_utc: str) -> str:
    """Generate a stable identifier from the detector's event identity."""
    identity = [day.isoformat(), port, *sorted((mmsi_a, mmsi_b)), first_flag_utc]
    return hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode('utf-8')).hexdigest()


def publish_day(connection: sqlite3.Connection, day: date, sources: dict,
                port_results: list[dict], port_bounds: dict,
                scoring_version: str, validate=None) -> None:
    """Atomically publish source provenance and one result for each configured port.

    `sources` maps ISO dates to download metadata and an availability flag.
    A duplicate date raises IntegrityError so callers cannot replace a committed
    result accidentally.
    """
    names = [result['port'] for result in port_results]
    if len(names) != len(set(names)) or set(names) != set(port_bounds):
        raise ValueError('Exactly one result is required for each configured port')
    if not scoring_version:
        raise ValueError('A scoring version is required')
    day_text = day.isoformat()
    if day_text not in sources or not sources[day_text].get('available'):
        raise ValueError('The primary AIS source must be available')
    if connection.in_transaction:
        raise ValueError('publish_day requires a connection without an active transaction')

    try:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute(
            'INSERT INTO study_day VALUES (?, ?, ?)',
            (day_text, datetime.now(timezone.utc).isoformat(), scoring_version),
        )
        for source_date, source in sources.items():
            date.fromisoformat(source_date)
            available = source['available']
            if not isinstance(available, bool):
                raise ValueError('Source availability must be a boolean')
            connection.execute(
                'INSERT INTO ais_source VALUES (?, ?, ?, ?, ?, ?)',
                (day_text, source_date, int(available), source.get('source_url'),
                 source.get('sha256'), source.get('compressed_bytes')),
            )
            if available:
                connection.execute('INSERT OR IGNORE INTO source_file VALUES (?, ?)',
                                   (source_date, source['sha256']))
        vessel_names = {}
        for result in port_results:
            port = result['port']
            if '_staged_path' in result:
                with Path(result['_staged_path']).open(encoding='utf-8') as stream:
                    result = json.load(stream)
                if result['port'] != port:
                    raise ValueError('Staged port result does not match its declared port')
            connection.execute(
                'INSERT INTO port_day VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (day_text, port, result['potential_events'],
                 result['likely_for_human_review'], result['elevated_priority_candidates'],
                 result['lower_priority_candidates'],
                 json.dumps(result['detector_quality'], allow_nan=False, sort_keys=True),
                 json.dumps(port_bounds[port], allow_nan=False)),
            )
            for row in result.get('traffic_hourly', ()):
                connection.execute(
                    'INSERT INTO traffic_hour VALUES (?, ?, ?, ?, ?)',
                    (day_text, port, row['hour'], row['unique_moving'], row['unique_anchored']),
                )
            for hour, rows in result.get('environment_hourly', {}).items():
                for row in rows:
                    values = (port, int(hour), row['measurement'], row.get('station_id'),
                              row.get('latitude'), row.get('longitude'), row.get('observation_utc'),
                              row.get('speed_mps'), row.get('direction_deg'),
                              row.get('direction_convention'), row.get('wave_height_m'),
                              row.get('current_depth_m'))
                    fingerprint = _environment_hash(*values)
                    connection.execute(
                        'INSERT OR IGNORE INTO environment_observation '
                        '(fingerprint, port, hour, measurement, station_id, latitude, longitude, '
                        'observation_utc, speed_mps, direction_deg, direction_convention, wave_height_m, current_depth_m) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        (fingerprint, *values),
                    )
                    observation_id = connection.execute(
                        'SELECT observation_id FROM environment_observation WHERE fingerprint=?',
                        (fingerprint,),
                    ).fetchone()[0]
                    connection.execute(
                        'INSERT INTO replay_hour VALUES (?, ?, ?, ?, ?, ?)',
                        (day_text, port, int(hour), row['measurement'],
                         row.get('station_id') or '', observation_id),
                    )
            if 'environment_sources' in result:
                connection.execute(
                    'INSERT INTO environment_source VALUES (?, ?, ?)',
                    (day_text, port, json.dumps(result['environment_sources'],
                                               allow_nan=False, sort_keys=True)),
                )
            for sample in result.get('track_samples', ()):
                source_date = sample['source_date']
                source = sources.get(source_date)
                if not source or not source['available']:
                    raise ValueError(f'Track sample refers to unavailable source {source_date}')
                name_key = (sample['mmsi'], sample['name'])
                if name_key not in vessel_names:
                    connection.execute('INSERT OR IGNORE INTO vessel_name (mmsi, name) VALUES (?, ?)',
                                       name_key)
                    vessel_names[name_key] = connection.execute(
                        'SELECT name_id FROM vessel_name WHERE mmsi=? AND name=?', name_key,
                    ).fetchone()[0]
                connection.execute(
                    'INSERT OR IGNORE INTO track_sample VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (port, source_date, source['sha256'], sample['mmsi'], sample['tick'],
                     sample['longitude'], sample['latitude'], vessel_names[name_key],
                     int(sample['anchored'])),
                )
            for event in result.get('encounters', ()):
                event_id = encounter_id(day, port, event['mmsi_a'], event['mmsi_b'],
                                        event['first_flag_utc'])
                details = dict(event['details'])
                reasons_present = ('likely_near_miss_evidence' in details
                                   and details['likely_near_miss_evidence'] is not None)
                evidence = details.pop('likely_near_miss_evidence') if reasons_present else ''
                score_7 = details.get('likely_near_miss_score_7')
                score_8 = details.get('likely_near_miss_score_8')
                closest = details.get('distance_time_utc')
                closest_tick = None
                if closest:
                    closest_time = datetime.fromisoformat(closest.replace('Z', '+00:00'))
                    if closest_time.tzinfo is None:
                        closest_time = closest_time.replace(tzinfo=timezone.utc)
                    closest_tick = closest_time.timestamp()
                connection.execute(
                    'INSERT INTO encounter (ais_date, port, encounter_id, mmsi_a, mmsi_b, '
                    'first_flag_utc, window_start, window_end, details_json, score_7, score_8, '
                    'classification, closest_tick, reasons_present) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (day_text, port, event_id, event['mmsi_a'], event['mmsi_b'],
                     event['first_flag_utc'], event['window_start'], event['window_end'],
                     json.dumps(details, allow_nan=False, sort_keys=True),
                     int(float(score_7)) if score_7 not in (None, '') else None,
                     int(float(score_8)) if score_8 not in (None, '') else None,
                     details.get('likely_near_miss_classification'), closest_tick, int(reasons_present)),
                )
                fields = NUMERIC_FIELDS + FLAG_FIELDS
                values = ([_number(details.get(field)) for field in NUMERIC_FIELDS]
                          + [_flag(details.get(field)) for field in FLAG_FIELDS])
                connection.execute(
                    'UPDATE encounter SET ' + ', '.join(field + '=?' for field in fields)
                    + ' WHERE ais_date=? AND port=? AND encounter_id=?',
                    (*values, day_text, port, event_id),
                )
                for position, reason in enumerate(evidence.split('; ') if evidence else ()):
                    connection.execute('INSERT OR IGNORE INTO score_reason (description) VALUES (?)',
                                       (reason,))
                    reason_id = connection.execute(
                        'SELECT reason_id FROM score_reason WHERE description=?', (reason,)).fetchone()[0]
                    connection.execute('INSERT INTO encounter_reason VALUES (?, ?, ?, ?, ?)',
                                       (day_text, port, event_id, position, reason_id))
                for vessel in event['vessels']:
                    connection.execute(
                        'INSERT INTO encounter_vessel VALUES (?, ?, ?, ?, ?)',
                        (day_text, port, event_id, vessel['mmsi'], vessel['role']),
                    )
        if validate is not None:
            validate(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def encounter_details(connection: sqlite3.Connection, row) -> dict:
    """Expand deduplicated score explanations for display and CSV export."""
    details = json.loads(row['details_json'])
    if row['reasons_present']:
        reasons = connection.execute(
            'SELECT r.description FROM encounter_reason AS e JOIN score_reason AS r '
            'ON r.reason_id=e.reason_id WHERE e.ais_date=? AND e.port=? AND e.encounter_id=? '
            'ORDER BY e.position',
            (row['ais_date'], row['port'], row['encounter_id']),
        )
        details['likely_near_miss_evidence'] = '; '.join(reason[0] for reason in reasons)
    return details


def replay_tracks(connection: sqlite3.Connection, day: date, port: str,
                  event_id: str) -> dict[str, list[list]]:
    """Return selected vessel samples from the sources available on this date."""
    day_text = day.isoformat()
    event = connection.execute(
        'SELECT window_start, window_end FROM encounter '
        'WHERE ais_date = ? AND port = ? AND encounter_id = ?',
        (day_text, port, event_id),
    ).fetchone()
    if event is None:
        raise KeyError(event_id)
    rows = connection.execute("""
        SELECT DISTINCT t.mmsi, t.tick, t.longitude, t.latitude, n.name, t.anchored
        FROM encounter_vessel AS v
        JOIN track_sample AS t ON t.port = v.port AND t.mmsi = v.mmsi
        JOIN vessel_name AS n ON n.name_id = t.name_id
        JOIN ais_source AS s ON s.ais_date = v.ais_date
             AND s.source_date = t.source_date AND s.sha256 = t.sha256
             AND s.available = 1
        WHERE v.ais_date = ? AND v.port = ? AND v.encounter_id = ?
          AND t.tick BETWEEN ? AND ?
        ORDER BY t.mmsi, t.tick, t.longitude, t.latitude, n.name, t.anchored
    """, (day_text, port, event_id, *event))
    tracks = {}
    for mmsi, tick, longitude, latitude, name, anchored in rows:
        tracks.setdefault(mmsi, []).append([tick, longitude, latitude, name, bool(anchored)])
    return tracks
