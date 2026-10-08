"""Read-only checks for one reviewed public SQLite publication."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from agent_costbook.migrations import SCHEMA_VERSION
from agent_costbook.store import Store, StoreError

PUBLICATION_KIND = "agent-costbook.publication"
REQUIRED_MANIFEST_KEYS = (
    "kind",
    "schema_version",
    "reviewed",
    "reviewed_at",
    "publisher_id",
    "database_sha256",
    "data_version",
    "coverage",
    "sources",
    "exclusions",
)
CAPABILITY_TABLES = ("capability_agent_history", "capability_model_effort_history")
_HEX = frozenset("0123456789abcdef")
_SECRET = re.compile(r"sk-[A-Za-z0-9]{8,}")
_ASSIGNED_SECRET = re.compile(r"(?i)\b(?:api[_-]?key|secret|password)\s*[:=]\s*\S")
_UNSAFE_TEXT = ("<html", "<!doctype", "<script", "all rights reserved", "-----begin ")


class PublicDataError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def default_public_paths() -> tuple[Path, Path]:
    root = Path(__file__).resolve().parent / "public_catalog"
    return root / "catalog.sqlite3", root / "manifest.json"


def validate_public_artifact(db_path, manifest_path) -> dict:
    database = Path(db_path)
    manifest = _manifest(manifest_path)
    _require_hash(database, manifest)
    _require_no_sidecars(database)
    store = _open_store(database)
    try:
        _require_schema(store)
        _require_capability_tables(store)
        _require_delete_journal(store)
        _require_publisher(store, manifest)
        _require_data_version(store, manifest)
        _require_empty_runtime_tables(store)
        _require_published_contributions(store)
        _require_coverage(store, manifest)
        _require_reviewed_sources(store)
    finally:
        store.close()
    return manifest


def _fail(code: str, message: str) -> None:
    raise PublicDataError(code, message) from None


def _manifest(path) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        _fail("manifest_invalid", "manifest is not readable JSON")
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError:
        _fail("manifest_invalid", "manifest is not readable JSON")
    if not isinstance(manifest, dict) or any(key not in manifest for key in REQUIRED_MANIFEST_KEYS):
        _fail("manifest_invalid", "manifest is missing required keys")
    if manifest["kind"] != PUBLICATION_KIND or type(manifest["schema_version"]) is not int:
        _fail("manifest_invalid", "manifest kind or schema is not a public publication")
    if manifest["schema_version"] != 1 or manifest["reviewed"] is not True:
        _fail("manifest_invalid", "manifest is not a reviewed schema 1 publication")
    if not _aware_timestamp(manifest["reviewed_at"]):
        _fail("manifest_invalid", "reviewed_at is not a timezone-aware timestamp")
    digest = manifest["database_sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in _HEX for character in digest)
    ):
        _fail("manifest_invalid", "database_sha256 is not a lowercase SHA-256")
    if not isinstance(manifest["publisher_id"], str) or not manifest["publisher_id"]:
        _fail("manifest_invalid", "publisher_id is missing")
    if type(manifest["data_version"]) is not int or manifest["data_version"] < 1:
        _fail("manifest_invalid", "data_version is not a positive integer")
    coverage = manifest["coverage"]
    if not isinstance(coverage, dict) or any(
        type(coverage.get(key)) is not int for key in ("prices", "agents", "model_efforts")
    ):
        _fail("manifest_invalid", "coverage counts are missing")
    if not _https_sources(manifest["sources"]) or not _exclusion_list(manifest["exclusions"]):
        _fail("manifest_invalid", "sources or exclusions are not review records")
    return manifest


def _https_sources(value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        url = item.get("url")
        source_id = item.get("id")
        if not isinstance(source_id, str) or not source_id:
            return False
        if not isinstance(url, str) or not url.startswith("https://"):
            return False
    return True


def _exclusion_list(value: object) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        if not isinstance(item.get("id"), str) or not isinstance(item.get("reason"), str):
            return False
        if _unsafe_text(item["reason"]):
            return False
    return True


def _aware_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.tzinfo.utcoffset(parsed) is not None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_hash(database: Path, manifest: dict) -> None:
    if not database.is_file():
        _fail("artifact_unreadable", "public database is not a single file")
    if _file_sha256(database) != manifest["database_sha256"]:
        _fail("hash_mismatch", "database sha256 does not match the manifest")


def _sidecar(database: Path, suffix: str) -> Path:
    return database.with_name(database.name + suffix)


def _require_no_sidecars(database: Path) -> None:
    if _sidecar(database, "-wal").exists() or _sidecar(database, "-shm").exists():
        _fail("sidecar_dependency", "public database has a WAL or SHM sidecar")


def _open_store(database: Path) -> Store:
    try:
        return Store(database, readonly=True)
    except StoreError as exc:
        if exc.code in {"future_schema", "migration_required"}:
            _fail("schema_invalid", "database schema is not version 2")
        _fail("artifact_unreadable", "public database could not be opened read-only")


def _require_schema(store: Store) -> None:
    version = int(store._conn.execute("PRAGMA user_version").fetchone()[0])
    if version != SCHEMA_VERSION:
        _fail("schema_invalid", "database schema is not version 2")


def _table_names(store: Store) -> set[str]:
    return {
        row[0]
        for row in store._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _require_capability_tables(store: Store) -> None:
    if not set(CAPABILITY_TABLES) <= _table_names(store):
        _fail("capability_tables_missing", "capability history tables are missing")


def _require_delete_journal(store: Store) -> None:
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    if str(mode).lower() != "delete":
        _fail("journal_invalid", "public database must use DELETE journaling")


def _require_publisher(store: Store, manifest: dict) -> None:
    if store.publisher_id() != manifest["publisher_id"]:
        _fail("publisher_mismatch", "publisher does not match the manifest")


def _require_data_version(store: Store, manifest: dict) -> None:
    revision = store.revision_of(None)
    if revision != manifest["data_version"]:
        _fail("version_mismatch", "data_version does not match the published snapshot")


def _require_empty_runtime_tables(store: Store) -> None:
    observations = store._conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    if observations:
        _fail("observations_present", "observations are not empty")
    jobs = store._conn.execute("SELECT COUNT(*) FROM collector_jobs").fetchone()[0]
    if jobs:
        _fail("collector_jobs_present", "collector jobs are not empty")


def _require_published_contributions(store: Store) -> None:
    rows = store._conn.execute("SELECT status FROM contributions").fetchall()
    if not rows or any(row["status"] != "published" for row in rows):
        _fail("unpublished_contribution", "a contribution is not published")


def _require_coverage(store: Store, manifest: dict) -> None:
    coverage = manifest["coverage"]
    prices = len(store.catalog(None))
    current = store.current_capabilities()
    if (
        coverage["prices"] != prices
        or coverage["agents"] != len(current["agents"])
        or coverage["model_efforts"] != len(current["model_efforts"])
    ):
        _fail("manifest_invalid", "coverage counts do not match the database")


def _require_reviewed_sources(store: Store) -> None:
    publisher = store.publisher_id()
    if publisher == "pub_sample" or not publisher.startswith("pub_"):
        _fail("source_rejected", "publisher is not a reviewed public identity")
    evidence = store._conn.execute(
        """
        SELECT source_kind, source_url, collector_kind, collector_name, content, retrieved_at
        FROM evidence
        """
    ).fetchall()
    if not evidence:
        _fail("source_rejected", "a source row is not an approved public review")
    for row in evidence:
        if not _reviewed_evidence(row):
            _fail("source_rejected", "a source row is not an approved public review")
    research = store._conn.execute("SELECT title, markdown FROM research").fetchall()
    if not research:
        _fail("source_rejected", "a source row is not an approved public review")
    for row in research:
        if _unsafe_text(row["title"]) or _unsafe_text(row["markdown"]) or len(row["markdown"]) > 8000:
            _fail("source_rejected", "a source row is not an approved public review")
    records = store._conn.execute(
        "SELECT provider, channel, model, plan, feature_scope FROM records"
    ).fetchall()
    for row in records:
        if any(_private_identity(row[key]) for key in row.keys()):
            _fail("source_rejected", "a source row is not an approved public review")
    for table in CAPABILITY_TABLES:
        bodies = store._conn.execute(f"SELECT body_json FROM {table}").fetchall()
        for body in bodies:
            if not _reviewed_capability(body[0]):
                _fail("source_rejected", "a source row is not an approved public review")


def _private_identity(value: object) -> bool:
    if not isinstance(value, str):
        return True
    lowered = value.lower()
    return "synthetic" in lowered or value == "pub_sample" or lowered.startswith("fixture:")


def _reviewed_evidence(row: sqlite3.Row) -> bool:
    url = row["source_url"] or ""
    if row["source_kind"] != "reviewed_public":
        return False
    if not url.startswith("https://") or "artificialanalysis.ai" in url.lower():
        return False
    if "synthetic" in row["source_kind"] or url.lower().startswith("fixture:"):
        return False
    if not row["collector_kind"] or not row["collector_name"]:
        return False
    if not _aware_timestamp(row["retrieved_at"]):
        return False
    content = row["content"]
    return isinstance(content, str) and len(content) <= 4000 and not _unsafe_text(content)


def _reviewed_capability(raw: object) -> bool:
    try:
        body = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(body, dict) or body.get("source") != "official":
        return False
    source_ref = body.get("source_ref")
    if not isinstance(source_ref, str) or not source_ref.startswith("https://"):
        return False
    if "artificialanalysis.ai" in source_ref.lower():
        return False
    if body.get("benchmarks") is not None:
        return False
    for value in body.values():
        if isinstance(value, str) and _unsafe_text(value):
            return False
    return True


def _unsafe_text(value: object) -> bool:
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    if any(marker in lowered for marker in _UNSAFE_TEXT):
        return True
    if _SECRET.search(value) or _ASSIGNED_SECRET.search(value):
        return True
    return "artificialanalysis.ai" in lowered
