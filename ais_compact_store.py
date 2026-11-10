"""SQLite storage primitives for completed compact AIS study dates.

The compact runner will use this module to publish each date in a single
transaction.  Only schema version 1 databases created by this module are
accepted.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3

SCHEMA_VERSION = 1

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
                'BEGIN IMMEDIATE;\n' + SCHEMA
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


def publish_day(connection: sqlite3.Connection, day: date, sources: dict,
                port_results: list[dict], port_bounds: dict,
                scoring_version: str) -> None:
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
        for result in port_results:
            port = result['port']
            connection.execute(
                'INSERT INTO port_day VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (day_text, port, result['potential_events'],
                 result['likely_for_human_review'], result['elevated_priority_candidates'],
                 result['lower_priority_candidates'],
                 json.dumps(result['detector_quality'], allow_nan=False, sort_keys=True),
                 json.dumps(port_bounds[port], allow_nan=False)),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
