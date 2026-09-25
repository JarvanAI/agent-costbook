from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import uuid
from pathlib import Path

from agent_costbook.backup import _sqlite_backup

SCHEMA_VERSION = 1


class MigrationError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _read_user_version(path: Path) -> int:
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise MigrationError("failed") from exc
    try:
        try:
            row = connection.execute("PRAGMA user_version").fetchone()
        except sqlite3.Error as exc:
            raise MigrationError("failed") from exc
        if row is None:
            raise MigrationError("failed")
        return int(row[0])
    finally:
        connection.close()


def _add_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in present:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def apply_schema(connection: sqlite3.Connection) -> None:
    """Bring one connection to the frozen v1 schema without rewriting published bytes."""
    from agent_costbook.store import LEGACY_FORMULA_SET

    connection.executescript(
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
        );
        CREATE UNIQUE INDEX IF NOT EXISTS contributions_idempotency
            ON contributions(idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        CREATE TABLE IF NOT EXISTS research (
            id TEXT PRIMARY KEY,
            contribution_id TEXT NOT NULL,
            title TEXT NOT NULL,
            markdown TEXT NOT NULL
        );
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
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id TEXT PRIMARY KEY,
            contribution_id TEXT NOT NULL,
            published_at TEXT NOT NULL,
            revision INTEGER NOT NULL,
            formula_version TEXT,
            export_generation INTEGER
        );
        CREATE TABLE IF NOT EXISTS collector_jobs (
            source_id TEXT PRIMARY KEY,
            interval_seconds INTEGER NOT NULL,
            next_run_at TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            max_attempts INTEGER NOT NULL,
            last_status TEXT,
            last_error TEXT,
            last_checked_at TEXT
        );
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
        );
        CREATE TABLE IF NOT EXISTS publisher_identity (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            publisher_id TEXT NOT NULL
        );
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
        );
        CREATE UNIQUE INDEX IF NOT EXISTS observations_idempotency
            ON observations(idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        """
    )
    _add_column(connection, "snapshots", "formula_version", "TEXT")
    _add_column(connection, "snapshots", "export_generation", "INTEGER")
    _add_column(connection, "records", "subscription_json", "TEXT")
    _add_column(connection, "records", "record_status", "TEXT")
    _add_column(connection, "records", "conflict_variants_json", "TEXT")
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


def _replace_database(target: Path, replacement: Path) -> None:
    sidecars = [target, Path(str(target) + "-wal"), Path(str(target) + "-shm")]
    moved: list[tuple[Path, Path]] = []
    try:
        for src in sidecars:
            if not src.exists() and not src.is_symlink():
                continue
            aside = src.with_name(src.name + ".migrate-aside")
            if aside.exists() or aside.is_symlink():
                raise MigrationError("failed")
            os.replace(src, aside)
            moved.append((src, aside))
        os.replace(replacement, target)
    except Exception:
        for src, aside in reversed(moved):
            if aside.exists() and not src.exists():
                os.replace(aside, src)
        raise
    for _src, aside in moved:
        aside.unlink(missing_ok=True)


def upgrade_database(path: str | Path) -> None:
    """Upgrade an on-disk database. A failed attempt leaves the original file in place."""
    database = Path(path)
    if not database.is_file():
        raise MigrationError("not_found")
    version = _read_user_version(database)
    if version > SCHEMA_VERSION:
        raise MigrationError("future_schema")
    if version == SCHEMA_VERSION:
        return
    partial = database.with_name(f".{database.name}.{os.getpid()}.migrate-partial")
    if partial.exists() or partial.is_symlink():
        raise MigrationError("target_exists")
    try:
        _sqlite_backup(database, partial)
        connection = sqlite3.connect(partial, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            apply_schema(connection)
        finally:
            connection.close()
        _replace_database(database, partial)
    finally:
        if partial.exists():
            partial.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-costbook-migrate")
    parser.add_argument("--db", required=True)
    args = parser.parse_args(argv)
    try:
        upgrade_database(args.db)
    except MigrationError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    print(f"schema_version={SCHEMA_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
