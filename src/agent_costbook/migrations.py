from __future__ import annotations

import argparse
import sqlite3
import sys
import uuid
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA_DDL = (
    """
    CREATE TABLE IF NOT EXISTS contributions (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        idempotency_key TEXT,
        payload_sha256 TEXT NOT NULL,
        response_json TEXT NOT NULL,
        research_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        published_snapshot_id TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS contributions_idempotency
        ON contributions(idempotency_key)
        WHERE idempotency_key IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS research (
        id TEXT PRIMARY KEY,
        contribution_id TEXT NOT NULL,
        title TEXT NOT NULL,
        markdown TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence (
        id TEXT PRIMARY KEY,
        contribution_id TEXT NOT NULL,
        source_kind TEXT NOT NULL,
        source_url TEXT,
        collector_kind TEXT NOT NULL,
        collector_name TEXT NOT NULL,
        content TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        retrieved_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        id TEXT PRIMARY KEY,
        contribution_id TEXT NOT NULL,
        published_at TEXT NOT NULL,
        revision INTEGER NOT NULL,
        formula_version TEXT,
        export_generation INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS collector_jobs (
        source_id TEXT PRIMARY KEY,
        interval_seconds INTEGER NOT NULL,
        next_run_at TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        max_attempts INTEGER NOT NULL,
        last_status TEXT,
        last_error TEXT,
        last_checked_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS records (
        id TEXT PRIMARY KEY,
        contribution_id TEXT NOT NULL,
        snapshot_id TEXT,
        provider TEXT NOT NULL,
        channel TEXT NOT NULL,
        model TEXT NOT NULL,
        effort TEXT NOT NULL,
        plan TEXT NOT NULL,
        feature_scope TEXT NOT NULL,
        window_start TEXT NOT NULL,
        window_end TEXT NOT NULL,
        currency TEXT NOT NULL,
        rates_json TEXT NOT NULL,
        evidence_ids_json TEXT NOT NULL,
        base_snapshot_id TEXT NOT NULL,
        subscription_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS publisher_identity (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        publisher_id TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS observations (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        idempotency_key TEXT,
        payload_sha256 TEXT NOT NULL,
        scope_key TEXT NOT NULL UNIQUE,
        provider TEXT NOT NULL,
        channel TEXT NOT NULL,
        model TEXT NOT NULL,
        effort TEXT NOT NULL,
        plan TEXT NOT NULL,
        feature_scope TEXT NOT NULL,
        currency TEXT NOT NULL,
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        task_category TEXT NOT NULL,
        acceptance TEXT NOT NULL,
        response_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS observations_idempotency
        ON observations(idempotency_key)
        WHERE idempotency_key IS NOT NULL
    """,
)

_ADDED_COLUMNS = (
    ("snapshots", "formula_version", "TEXT"),
    ("snapshots", "export_generation", "INTEGER"),
    ("records", "subscription_json", "TEXT"),
    ("records", "record_status", "TEXT"),
    ("records", "conflict_variants_json", "TEXT"),
)


class MigrationError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _add_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in present:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def apply_schema(connection: sqlite3.Connection) -> None:
    """Apply the frozen v1 schema. The caller owns the transaction."""
    from agent_costbook.store import LEGACY_FORMULA_SET

    for statement in _SCHEMA_DDL:
        connection.execute(statement)
    for table, column, definition in _ADDED_COLUMNS:
        _add_column(connection, table, column, definition)
    connection.execute(
        """
        UPDATE snapshots
        SET formula_version = ?
        WHERE formula_version IS NULL
        """,
        (LEGACY_FORMULA_SET,),
    )
    existing = connection.execute(
        "SELECT publisher_id FROM publisher_identity WHERE singleton = 1"
    ).fetchone()
    if existing is None:
        connection.execute(
            "INSERT INTO publisher_identity (singleton, publisher_id) VALUES (1, ?)",
            (f"pub_{uuid.uuid4().hex}",),
        )
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def upgrade_database(path: str | Path) -> None:
    """Upgrade the same database file. A failure rolls the transaction back."""
    database = Path(path)
    if not database.is_file():
        raise MigrationError("not_found")
    try:
        connection = sqlite3.connect(database, isolation_level=None)
    except sqlite3.Error as exc:
        raise MigrationError("failed") from exc
    try:
        try:
            row = connection.execute("PRAGMA user_version").fetchone()
        except sqlite3.Error as exc:
            raise MigrationError("failed") from exc
        if row is None:
            raise MigrationError("failed")
        version = int(row[0])
        if version > SCHEMA_VERSION:
            raise MigrationError("future_schema")
        if version == SCHEMA_VERSION:
            return
        try:
            connection.execute("BEGIN IMMEDIATE")
            apply_schema(connection)
            connection.execute("COMMIT")
        except Exception:
            _rollback(connection)
            raise
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-costbook-migrate")
    parser.add_argument("--db", required=True)
    args = parser.parse_args(argv)
    try:
        upgrade_database(args.db)
    except MigrationError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    except Exception:
        print("failed", file=sys.stderr)
        return 2
    print(f"schema_version={SCHEMA_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
