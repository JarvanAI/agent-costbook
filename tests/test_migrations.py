import sqlite3
import threading
from pathlib import Path

import pytest

from agent_costbook.export import build_document, render
from agent_costbook import migrations
from agent_costbook.migrations import SCHEMA_VERSION, MigrationError, main, upgrade_database
from agent_costbook.observations import aggregate_observation
from agent_costbook.store import Store, StoreError
from support import synthetic_contribution


def _stamp(path: Path, version: int) -> None:
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {version}")
    connection.commit()
    connection.close()


def _version(path: Path) -> int:
    connection = sqlite3.connect(path)
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    connection.close()
    return version


def _identities(path: Path) -> dict:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    publisher = connection.execute(
        "SELECT publisher_id FROM publisher_identity WHERE singleton = 1"
    ).fetchone()["publisher_id"]
    snapshots = connection.execute(
        "SELECT id, revision, formula_version FROM snapshots ORDER BY revision"
    ).fetchall()
    evidence = connection.execute(
        "SELECT id, content_sha256, content FROM evidence ORDER BY id"
    ).fetchall()
    observations = connection.execute(
        "SELECT id, payload_sha256 FROM observations ORDER BY id"
    ).fetchall()
    connection.close()
    return {
        "publisher_id": publisher,
        "snapshots": [tuple(row) for row in snapshots],
        "evidence": [tuple(row) for row in evidence],
        "observations": [tuple(row) for row in observations],
    }


def test_upgrade_keeps_publisher_revision_evidence_and_observation(tmp_path):
    db = tmp_path / "current.sqlite3"
    store = Store(db)
    created, _ = store.create_contribution(synthetic_contribution(), "kept")
    store.publish(created["contribution_id"])
    payload = {
        "provider": "example",
        "channel": "api",
        "model": "synthetic-m4",
        "effort": "",
        "plan": "payg",
        "feature_scope": "text",
        "currency": "USD",
        "period_start": "2026-09-01T00:00:00+00:00",
        "period_end": "2026-10-01T00:00:00+00:00",
        "task_category": "pricing",
        "acceptance": "tests_passed",
        "subscription_cash": "0",
        "tasks": [
            {
                "task_id": "task-1",
                "attempts": [{"cash": "1.25", "api_equivalent": "4", "succeeded": True}],
            }
        ],
    }
    recorded, _ = store.record_observation(payload, aggregate_observation(payload), "obs-1")
    store.close()
    before_export = render(build_document(Store(db, readonly=True), None))
    Store(db, readonly=True).close()
    identities = _identities(db)
    _stamp(db, 0)
    upgrade_database(db)
    assert _version(db) == SCHEMA_VERSION
    assert _identities(db) == identities
    assert identities["observations"][0][0] == recorded["observation_id"]
    assert identities["snapshots"][0][2] == "ac-formulas-v2"
    reread = Store(db, readonly=True)
    assert render(build_document(reread, None)) == before_export
    reread.close()
    unchanged = db.read_bytes()
    upgrade_database(db)
    assert db.read_bytes() == unchanged


def test_legacy_formula_v1_is_not_rewritten_to_v2(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(db)
    connection.executescript(
        """
        CREATE TABLE contributions (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, idempotency_key TEXT,
            payload_sha256 TEXT NOT NULL, response_json TEXT NOT NULL,
            research_id TEXT NOT NULL, created_at TEXT NOT NULL,
            published_snapshot_id TEXT
        );
        CREATE TABLE research (
            id TEXT PRIMARY KEY, contribution_id TEXT NOT NULL,
            title TEXT NOT NULL, markdown TEXT NOT NULL
        );
        CREATE TABLE evidence (
            id TEXT PRIMARY KEY, contribution_id TEXT NOT NULL,
            source_kind TEXT NOT NULL, source_url TEXT, collector_kind TEXT NOT NULL,
            collector_name TEXT NOT NULL, content TEXT NOT NULL,
            content_sha256 TEXT NOT NULL, retrieved_at TEXT NOT NULL
        );
        CREATE TABLE snapshots (
            id TEXT PRIMARY KEY, contribution_id TEXT NOT NULL,
            published_at TEXT NOT NULL, revision INTEGER NOT NULL,
            formula_version TEXT
        );
        CREATE TABLE records (
            id TEXT PRIMARY KEY, contribution_id TEXT NOT NULL, snapshot_id TEXT,
            provider TEXT NOT NULL, channel TEXT NOT NULL, model TEXT NOT NULL,
            effort TEXT NOT NULL, plan TEXT NOT NULL, feature_scope TEXT NOT NULL,
            window_start TEXT NOT NULL, window_end TEXT NOT NULL, currency TEXT NOT NULL,
            rates_json TEXT NOT NULL, evidence_ids_json TEXT NOT NULL,
            base_snapshot_id TEXT NOT NULL
        );
        INSERT INTO contributions (
            id, status, idempotency_key, payload_sha256, response_json,
            research_id, created_at, published_snapshot_id
        ) VALUES ('co_old', 'published', NULL, 'abc', '{}', 'rs_old',
                  '2026-09-25T00:00:00+00:00', 'snap_legacy');
        INSERT INTO research (id, contribution_id, title, markdown)
        VALUES ('rs_old', 'co_old', 'Legacy', 'legacy research markdown');
        INSERT INTO evidence (
            id, contribution_id, source_kind, source_url, collector_kind,
            collector_name, content, content_sha256, retrieved_at
        ) VALUES ('ev_old', 'co_old', 'synthetic_fixture', 'fixture://legacy',
                  'test', 'pytest', 'legacy evidence body', 'deadbeef',
                  '2026-09-25T00:00:00+00:00');
        INSERT INTO snapshots (
            id, contribution_id, published_at, revision, formula_version
        ) VALUES ('snap_legacy', 'co_old', '2026-09-25T00:00:00+00:00', 1,
                  'ac-formulas-v1');
        INSERT INTO records (
            id, contribution_id, snapshot_id, provider, channel, model, effort,
            plan, feature_scope, window_start, window_end, currency, rates_json,
            evidence_ids_json, base_snapshot_id
        ) VALUES (
            'rec_old', 'co_old', 'snap_legacy', 'example', 'api', 'legacy-model',
            '', 'payg', 'text', '', '', 'USD',
            '{"uncached_input_per_million":"2"}', '["ev_old"]', ''
        );
        """
    )
    connection.commit()
    connection.close()
    upgrade_database(db)
    store = Store(db, readonly=True)
    document = build_document(store, 1)
    store.close()
    assert document["formula_version"] == "ac-formulas-v1"
    assert document["data_version"] == 1
    assert document["publisher_id"]
    assert document["records"][0]["sources"][0]["id"] == "ev_old"
    assert "legacy evidence body" not in render(document).decode()
    connection = sqlite3.connect(db)
    formula = connection.execute(
        "SELECT formula_version FROM snapshots WHERE id = 'snap_legacy'"
    ).fetchone()[0]
    body = connection.execute("SELECT content FROM evidence WHERE id = 'ev_old'").fetchone()[0]
    connection.close()
    assert formula == "ac-formulas-v1"
    assert body == "legacy evidence body"


def test_future_schema_is_rejected_without_rewriting(tmp_path):
    db = tmp_path / "future.sqlite3"
    Store(db).close()
    _stamp(db, SCHEMA_VERSION + 8)
    before = db.read_bytes()
    with pytest.raises(MigrationError) as failure:
        upgrade_database(db)
    assert failure.value.code == "future_schema"
    assert db.read_bytes() == before
    assert _version(db) == SCHEMA_VERSION + 8
    with pytest.raises(Exception) as opened:
        Store(db)
    assert opened.value.code == "future_schema"
    assert db.read_bytes() == before
    assert main(["--db", str(db)]) == 2


def test_failed_upgrade_leaves_the_original_bytes(tmp_path, monkeypatch):
    db = tmp_path / "live.sqlite3"
    Store(db).close()
    _stamp(db, 0)
    before = db.read_bytes()

    def boom(_connection):
        raise RuntimeError("migration exploded")

    monkeypatch.setattr("agent_costbook.migrations.apply_schema", boom)
    with pytest.raises(RuntimeError, match="migration exploded"):
        upgrade_database(db)
    assert db.read_bytes() == before
    assert _version(db) == 0


def test_open_connection_keeps_writing_after_store_migration(tmp_path):
    db = tmp_path / "shared.sqlite3"
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("CREATE TABLE kept (id INTEGER PRIMARY KEY)")
    holder.execute("INSERT INTO kept VALUES (1)")
    holder.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    holder.execute("INSERT INTO kept VALUES (2)")
    holder.execute("PRAGMA user_version = 0")
    inode = db.stat().st_ino
    wal = Path(str(db) + "-wal")
    assert wal.stat().st_size > 0
    store = Store(db)
    assert db.stat().st_ino == inode
    holder.execute("INSERT INTO kept VALUES (3)")
    assert store.publisher_id()
    reader = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    assert [row[0] for row in reader.execute("SELECT id FROM kept ORDER BY id")] == [1, 2, 3]
    reader.close()
    assert _version(db) == SCHEMA_VERSION
    store.close()
    holder.close()


def test_partial_ddl_failure_rolls_back_schema_version_and_data(tmp_path, monkeypatch):
    db = tmp_path / "partial.sqlite3"
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("CREATE TABLE kept (id INTEGER PRIMARY KEY)")
    holder.execute("INSERT INTO kept VALUES (4)")
    holder.execute(
        """
        CREATE TABLE snapshots (
            id TEXT PRIMARY KEY,
            contribution_id TEXT NOT NULL,
            published_at TEXT NOT NULL,
            revision INTEGER NOT NULL
        )
        """
    )
    holder.execute(
        """
        INSERT INTO snapshots (id, contribution_id, published_at, revision)
        VALUES ('snap_old', 'co', '2026-09-25T00:00:00+00:00', 1)
        """
    )
    holder.execute("PRAGMA user_version = 0")
    inode = db.stat().st_ino
    real_add_column = migrations._add_column

    def fail_after_earlier_ddl(connection, table, column, definition):
        if table == "records" and column == "record_status":
            raise sqlite3.OperationalError("partial ddl")
        return real_add_column(connection, table, column, definition)

    monkeypatch.setattr(migrations, "_add_column", fail_after_earlier_ddl)
    with pytest.raises(sqlite3.OperationalError, match="partial ddl"):
        upgrade_database(db)
    assert db.stat().st_ino == inode
    assert holder.execute("PRAGMA user_version").fetchone()[0] == 0
    assert holder.execute("SELECT id FROM kept").fetchone()[0] == 4
    names = {
        row[0]
        for row in holder.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "contributions" not in names
    assert "observations" not in names
    columns = [row[1] for row in holder.execute("PRAGMA table_info(snapshots)")]
    assert "formula_version" not in columns
    holder.execute("INSERT INTO kept VALUES (5)")
    assert [row[0] for row in holder.execute("SELECT id FROM kept ORDER BY id")] == [4, 5]
    holder.close()


def test_cli_prints_a_stable_failure_code(tmp_path, monkeypatch, capsys):
    db = tmp_path / "cli.sqlite3"
    Store(db).close()
    _stamp(db, 0)
    inode = db.stat().st_ino

    def boom(_connection):
        raise RuntimeError("migration exploded")

    monkeypatch.setattr("agent_costbook.migrations.apply_schema", boom)
    assert main(["--db", str(db)]) == 2
    captured = capsys.readouterr()
    assert captured.err.strip() == "failed"
    assert captured.out == ""
    assert _version(db) == 0
    assert db.stat().st_ino == inode


def _wal_database(path: Path) -> None:
    Store(path).close()
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA user_version = 0")
    connection.execute(
        """
        INSERT INTO snapshots (
            id, contribution_id, published_at, revision, formula_version
        ) VALUES ('sentinel', 'co', '2026-09-25T00:00:00+00:00', 99, NULL)
        """
    )
    connection.close()


def _race_connect(monkeypatch, reached_begin: threading.Event):
    original = sqlite3.connect

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)

        def trace(statement: str) -> None:
            if statement.lstrip().upper().startswith("BEGIN"):
                reached_begin.set()

        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    return original


def _run_race(db: Path, original_connect, reached_begin: threading.Event, target, committed_version: int):
    writer = original_connect(db, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(f"PRAGMA user_version = {committed_version}")
    outcome: dict = {}

    def run() -> None:
        try:
            target()
        except (MigrationError, StoreError) as exc:
            outcome["code"] = exc.code
        else:
            outcome["code"] = None

    thread = threading.Thread(target=run)
    thread.start()
    assert reached_begin.wait(5)
    writer.execute("COMMIT")
    thread.join(5)
    writer.close()
    assert not thread.is_alive()
    return outcome


def test_upgrade_rechecks_future_version_after_the_write_lock(tmp_path, monkeypatch):
    db = tmp_path / "future-race.sqlite3"
    _wal_database(db)
    reached_begin = threading.Event()
    original = _race_connect(monkeypatch, reached_begin)
    outcome = _run_race(
        db,
        original,
        reached_begin,
        lambda: upgrade_database(db),
        99,
    )
    assert outcome["code"] == "future_schema"
    connection = original(db)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
    formula = connection.execute(
        "SELECT formula_version FROM snapshots WHERE id = 'sentinel'"
    ).fetchone()[0]
    connection.close()
    assert formula is None


def test_upgrade_rechecks_completed_version_after_the_write_lock(tmp_path, monkeypatch):
    db = tmp_path / "current-race.sqlite3"
    _wal_database(db)
    reached_begin = threading.Event()
    original = _race_connect(monkeypatch, reached_begin)
    outcome = _run_race(
        db,
        original,
        reached_begin,
        lambda: upgrade_database(db),
        SCHEMA_VERSION,
    )
    assert outcome["code"] is None
    connection = original(db)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    formula = connection.execute(
        "SELECT formula_version FROM snapshots WHERE id = 'sentinel'"
    ).fetchone()[0]
    connection.close()
    assert formula is None


def test_store_migrate_rechecks_version_after_the_write_lock(tmp_path, monkeypatch):
    db = tmp_path / "store-race.sqlite3"
    _wal_database(db)
    monkeypatch.setattr("agent_costbook.store.upgrade_database", lambda path: None)
    reached_begin = threading.Event()
    original = _race_connect(monkeypatch, reached_begin)
    outcome = _run_race(db, original, reached_begin, lambda: Store(db), 99)
    assert outcome["code"] == "future_schema"
    connection = original(db)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
    formula = connection.execute(
        "SELECT formula_version FROM snapshots WHERE id = 'sentinel'"
    ).fetchone()[0]
    connection.close()
    assert formula is None


def test_store_migrate_keeps_a_completed_version(tmp_path, monkeypatch):
    db = tmp_path / "store-current-race.sqlite3"
    _wal_database(db)
    monkeypatch.setattr("agent_costbook.store.upgrade_database", lambda path: None)
    reached_begin = threading.Event()
    original = _race_connect(monkeypatch, reached_begin)
    outcome = _run_race(db, original, reached_begin, lambda: Store(db), SCHEMA_VERSION)
    assert outcome["code"] is None
    connection = original(db)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    formula = connection.execute(
        "SELECT formula_version FROM snapshots WHERE id = 'sentinel'"
    ).fetchone()[0]
    connection.close()
    assert formula is None


def test_corrupt_database_is_left_unchanged(tmp_path):
    db = tmp_path / "corrupt.sqlite3"
    db.write_bytes(b"not a sqlite database")
    before = db.read_bytes()
    with pytest.raises(MigrationError) as failure:
        upgrade_database(db)
    assert failure.value.code == "failed"
    assert db.read_bytes() == before
