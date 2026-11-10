"""SQLite storage primitives for completed compact AIS study dates.

The compact runner will use this module to publish each date in a single
transaction.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

SCHEMA_VERSION = 4

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


def open_writer(path: Path) -> sqlite3.Connection:
    """Open or create a versioned database with durable single-writer settings."""
    connection = sqlite3.connect(path, timeout=5)
    try:
        connection.execute('PRAGMA foreign_keys = ON')
        connection.execute('PRAGMA journal_mode = DELETE')
        connection.execute('PRAGMA synchronous = FULL')
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version == 0:
            existing = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' LIMIT 1"
            ).fetchone()
            if existing:
                raise ValueError('Unversioned database is not an empty compact store')
            connection.executescript(
                'BEGIN IMMEDIATE;\n' + SCHEMA + REPLAY_SCHEMA + HOURLY_SCHEMA + MEASUREMENT_SCHEMA
                + f'\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;'
            )
        elif version == 1:
            connection.executescript(
                'BEGIN IMMEDIATE;\n' + REPLAY_SCHEMA + HOURLY_SCHEMA + MEASUREMENT_SCHEMA
                + f'\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;'
            )
        elif version == 2:
            connection.executescript(
                'BEGIN IMMEDIATE;\n' + HOURLY_SCHEMA + MEASUREMENT_SCHEMA
                + f'\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;'
            )
        elif version == 3:
            connection.executescript(
                'BEGIN IMMEDIATE;\n' + MEASUREMENT_SCHEMA
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
                    connection.execute(
                        'INSERT INTO environment_hour VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        (day_text, port, int(hour), row['measurement'], row.get('station_id') or '',
                         row.get('station_id'), row.get('latitude'), row.get('longitude'),
                         row.get('observation_utc'), row.get('speed_mps'), row.get('direction_deg'),
                         row.get('direction_convention'), row.get('wave_height_m'),
                         row.get('current_depth_m')),
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
                connection.execute(
                    'INSERT OR IGNORE INTO track_sample VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (port, source_date, source['sha256'], sample['mmsi'], sample['tick'],
                     sample['longitude'], sample['latitude'], sample['name'],
                     int(sample['anchored'])),
                )
            for event in result.get('encounters', ()):
                event_id = encounter_id(day, port, event['mmsi_a'], event['mmsi_b'],
                                        event['first_flag_utc'])
                connection.execute(
                    'INSERT INTO encounter VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (day_text, port, event_id, event['mmsi_a'], event['mmsi_b'],
                     event['first_flag_utc'], event['window_start'], event['window_end'],
                     json.dumps(event['details'], allow_nan=False, sort_keys=True)),
                )
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
        SELECT DISTINCT t.mmsi, t.tick, t.longitude, t.latitude, t.name, t.anchored
        FROM encounter_vessel AS v
        JOIN track_sample AS t ON t.port = v.port AND t.mmsi = v.mmsi
        JOIN ais_source AS s ON s.ais_date = v.ais_date
             AND s.source_date = t.source_date AND s.sha256 = t.sha256
             AND s.available = 1
        WHERE v.ais_date = ? AND v.port = ? AND v.encounter_id = ?
          AND t.tick BETWEEN ? AND ?
        ORDER BY t.mmsi, t.tick, t.longitude, t.latitude, t.name, t.anchored
    """, (day_text, port, event_id, *event))
    tracks = {}
    for mmsi, tick, longitude, latitude, name, anchored in rows:
        tracks.setdefault(mmsi, []).append([tick, longitude, latitude, name, bool(anchored)])
    return tracks
