from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_costbook.migrations import SCHEMA_VERSION, MigrationError, apply_schema, upgrade_database

FORMULA_SET = "ac-formulas-v2"
LEGACY_FORMULA_SET = "ac-formulas-v1"
SUPPORTED_FORMULA_SETS = frozenset({FORMULA_SET, LEGACY_FORMULA_SET})
EXPORT_GENERATION = 2
MAX_SAFE_INT = 9007199254740991
_PUBLIC_SNAPSHOT = re.compile(r"^snap-([1-9][0-9]*)$")
_OBSERVATION_SCOPE = (
    "provider",
    "channel",
    "model",
    "effort",
    "plan",
    "feature_scope",
    "currency",
    "period_start",
    "period_end",
    "task_category",
    "acceptance",
)
_SUBSCRIPTION_KEYS = (
    "monthly_price",
    "price_period",
    "quota_multiplier",
    "baseline_tasks",
    "measured_tasks",
    "baseline_api_budget",
    "utilization",
    "cost_per_task",
    "weight",
    "task_profile",
    "baseline_group",
    "assumptions",
)


def _locked(method):
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class StoreError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Selection:
    record: dict | None
    conflict: bool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _subscription_payload(item: dict) -> dict:
    raw = item.get("subscription") or {}
    kept = {}
    for key in _SUBSCRIPTION_KEYS:
        value = raw.get(key)
        if value is None or value == []:
            continue
        kept[key] = value
    return kept


def _identity(row: dict) -> tuple:
    return (
        row["provider"],
        row["channel"],
        row["model"],
        row["effort"],
        row["plan"],
        row["feature_scope"],
        row["window_start"],
        row["window_end"],
        row["currency"],
    )


class Store:
    def __init__(self, path: str | Path, *, readonly: bool = False):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._readonly = readonly
        if readonly:
            if not self.path.is_file():
                raise StoreError("not_found")
            self._conn = sqlite3.connect(
                f"{self.path.resolve().as_uri()}?mode=ro",
                uri=True,
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION or not self._schema_ready():
                self._conn.close()
                if version > SCHEMA_VERSION:
                    raise StoreError("future_schema")
                raise StoreError("migration_required")
            return
        if self.path.exists():
            try:
                upgrade_database(self.path)
            except MigrationError as exc:
                raise StoreError(exc.code) from exc
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def _schema_ready(self) -> bool:
        tables = {
            row[0]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "publisher_identity" not in tables or "snapshots" not in tables or "records" not in tables:
            return False
        snapshot_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(snapshots)")}
        record_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(records)")}
        if "formula_version" not in snapshot_columns or "subscription_json" not in record_columns:
            return False
        publisher = self._conn.execute(
            "SELECT 1 FROM publisher_identity WHERE singleton = 1"
        ).fetchone()
        return publisher is not None

    def _migrate(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise StoreError("future_schema")
        if version == SCHEMA_VERSION and self._schema_ready():
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            apply_schema(self._conn)
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def create_contribution(self, payload: dict, idempotency_key: str | None) -> tuple[dict, bool]:
        digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
        with self._lock:
            if idempotency_key:
                existing = self._conn.execute(
                    "SELECT payload_sha256, response_json FROM contributions WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if existing["payload_sha256"] != digest:
                        raise StoreError("idempotency_conflict")
                    return json.loads(existing["response_json"]), False

            contribution_id = f"co_{uuid.uuid4().hex}"
            research_id = f"rs_{uuid.uuid4().hex}"
            evidence_rows = []
            for item in payload["evidence"]:
                content = item["content"]
                evidence_rows.append(
                    {
                        "id": f"ev_{uuid.uuid4().hex}",
                        "source_kind": item["source_kind"],
                        "source_url": item.get("source_url"),
                        "collector_kind": item["collector_kind"],
                        "collector_name": item["collector_name"],
                        "content": content,
                        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                        "retrieved_at": item["retrieved_at"],
                    }
                )
            record_rows = []
            for item in payload["records"]:
                evidence_ids = [evidence_rows[index]["id"] for index in item["evidence_indexes"]]
                rates = {
                    key: value
                    for key, value in dict(item["rates"]).items()
                    if value is not None
                }
                record_rows.append(
                    {
                        "id": f"rec_{uuid.uuid4().hex}",
                        "provider": item["provider"],
                        "channel": item["channel"],
                        "model": item["model"],
                        "effort": item.get("effort") or "",
                        "plan": item["plan"],
                        "feature_scope": item["feature_scope"],
                        "window_start": item.get("window_start") or "",
                        "window_end": item.get("window_end") or "",
                        "currency": item["currency"],
                        "rates": rates,
                        "evidence_ids": evidence_ids,
                        "base_snapshot_id": item.get("base_snapshot_id") or "",
                        "subscription": _subscription_payload(item),
                        "record_status": "conflict" if item.get("status") == "conflict" else None,
                        "conflict_variants": item.get("conflict_variants")
                        if item.get("status") == "conflict"
                        else None,
                    }
                )
            response = {
                "contribution_id": contribution_id,
                "status": "draft",
                "research_id": research_id,
                "evidence": [
                    {"id": row["id"], "content_sha256": row["content_sha256"]}
                    for row in evidence_rows
                ],
                "records": [{"id": row["id"], "status": "draft"} for row in record_rows],
            }
            research = payload["research"]
            try:
                self._conn.execute("BEGIN")
                self._conn.execute(
                    """
                    INSERT INTO contributions (
                        id, status, idempotency_key, payload_sha256, response_json,
                        research_id, created_at
                    ) VALUES (?, 'draft', ?, ?, ?, ?, ?)
                    """,
                    (
                        contribution_id,
                        idempotency_key,
                        digest,
                        json.dumps(response, ensure_ascii=False),
                        research_id,
                        _now(),
                    ),
                )
                self._conn.execute(
                    "INSERT INTO research (id, contribution_id, title, markdown) VALUES (?, ?, ?, ?)",
                    (research_id, contribution_id, research["title"], research["markdown"]),
                )
                self._conn.executemany(
                    """
                    INSERT INTO evidence (
                        id, contribution_id, source_kind, source_url, collector_kind,
                        collector_name, content, content_sha256, retrieved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            row["id"],
                            contribution_id,
                            row["source_kind"],
                            row["source_url"],
                            row["collector_kind"],
                            row["collector_name"],
                            row["content"],
                            row["content_sha256"],
                            row["retrieved_at"],
                        )
                        for row in evidence_rows
                    ],
                )
                self._conn.executemany(
                    """
                    INSERT INTO records (
                        id, contribution_id, snapshot_id, provider, channel, model, effort,
                        plan, feature_scope, window_start, window_end, currency,
                        rates_json, evidence_ids_json, base_snapshot_id, subscription_json,
                        record_status, conflict_variants_json
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            row["id"],
                            contribution_id,
                            row["provider"],
                            row["channel"],
                            row["model"],
                            row["effort"],
                            row["plan"],
                            row["feature_scope"],
                            row["window_start"],
                            row["window_end"],
                            row["currency"],
                            json.dumps(row["rates"], ensure_ascii=False, sort_keys=True),
                            json.dumps(row["evidence_ids"]),
                            row["base_snapshot_id"],
                            json.dumps(row["subscription"], ensure_ascii=False, sort_keys=True),
                            row["record_status"],
                            None
                            if row["conflict_variants"] is None
                            else json.dumps(
                                row["conflict_variants"],
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                        )
                        for row in record_rows
                    ],
                )
                self._conn.commit()
            except BaseException as exc:
                self._conn.rollback()
                if isinstance(exc, sqlite3.IntegrityError):
                    raise StoreError("idempotency_conflict") from exc
                raise
            return response, True

    def publish(self, contribution_id: str) -> dict:
        with self._lock:
            contribution = self._conn.execute(
                "SELECT status, published_snapshot_id FROM contributions WHERE id = ?",
                (contribution_id,),
            ).fetchone()
            if contribution is None:
                raise StoreError("not_found")
            if contribution["status"] == "published":
                return {
                    "contribution_id": contribution_id,
                    "status": "published",
                    "snapshot_id": contribution["published_snapshot_id"],
                }
            if contribution["status"] == "conflict":
                raise StoreError("conflict")
            rows = [
                self._record_from_row(row)
                for row in self._conn.execute(
                    "SELECT * FROM records WHERE contribution_id = ?",
                    (contribution_id,),
                )
            ]
            seen: set[tuple] = set()
            for row in rows:
                identity = _identity(row)
                if identity in seen:
                    self._mark_conflict(contribution_id)
                    raise StoreError("conflict")
                seen.add(identity)
            current = {
                _identity(record): record for record in self._published_rows(None)
            }
            for row in rows:
                existing = current.get(_identity(row))
                base = row.get("base_snapshot_id") or ""
                if existing is None:
                    if base:
                        self._mark_conflict(contribution_id)
                        raise StoreError("conflict")
                    continue
                if base != existing["snapshot_id"]:
                    self._mark_conflict(contribution_id)
                    raise StoreError("conflict")
            snapshot_id = f"snap_{uuid.uuid4().hex}"
            published_at = _now()
            self._conn.execute("BEGIN")
            try:
                revision = self._conn.execute(
                    "SELECT COALESCE(MAX(revision), 0) + 1 FROM snapshots"
                ).fetchone()[0]
                self._conn.execute(
                    """
                    INSERT INTO snapshots (
                        id, contribution_id, published_at, revision, formula_version,
                        export_generation
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        contribution_id,
                        published_at,
                        revision,
                        FORMULA_SET,
                        EXPORT_GENERATION,
                    ),
                )
                self._conn.execute(
                    "UPDATE records SET snapshot_id = ? WHERE contribution_id = ?",
                    (snapshot_id, contribution_id),
                )
                self._conn.execute(
                    """
                    UPDATE contributions
                    SET status = 'published', published_snapshot_id = ?
                    WHERE id = ?
                    """,
                    (snapshot_id, contribution_id),
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            return {
                "contribution_id": contribution_id,
                "status": "published",
                "snapshot_id": snapshot_id,
            }

    def _mark_conflict(self, contribution_id: str) -> None:
        self._conn.execute(
            "UPDATE contributions SET status = 'conflict' WHERE id = ?",
            (contribution_id,),
        )
        self._conn.commit()

    def get_contribution(self, contribution_id: str) -> dict | None:
        with self._lock:
            return self._get_contribution(contribution_id)

    def _get_contribution(self, contribution_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT id, status, published_snapshot_id FROM contributions WHERE id = ?",
            (contribution_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "status": row["status"],
            "snapshot_id": row["published_snapshot_id"],
        }

    @_locked
    def get_evidence(self, evidence_id: str) -> dict | None:
        row = self._conn.execute(
            """
            SELECT evidence.*, contributions.status AS contribution_status
            FROM evidence
            JOIN contributions ON contributions.id = evidence.contribution_id
            WHERE evidence.id = ?
            """,
            (evidence_id,),
        ).fetchone()
        if row is None or row["contribution_status"] != "published":
            return None
        return {
            "id": row["id"],
            "contribution_id": row["contribution_id"],
            "source_kind": row["source_kind"],
            "source_url": row["source_url"],
            "collector_kind": row["collector_kind"],
            "collector_name": row["collector_name"],
            "content": row["content"],
            "content_sha256": row["content_sha256"],
            "retrieved_at": row["retrieved_at"],
        }

    @_locked
    def get_research(self, research_id: str) -> dict | None:
        row = self._conn.execute(
            """
            SELECT research.*, contributions.status AS contribution_status
            FROM research
            JOIN contributions ON contributions.id = research.contribution_id
            WHERE research.id = ?
            """,
            (research_id,),
        ).fetchone()
        if row is None or row["contribution_status"] != "published":
            return None
        return {
            "id": row["id"],
            "contribution_id": row["contribution_id"],
            "title": row["title"],
            "markdown": row["markdown"],
        }

    @_locked
    def latest_snapshot_id(self) -> str | None:
        row = self._conn.execute(
            "SELECT id FROM snapshots ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        return None if row is None else row["id"]

    def close(self) -> None:
        self._conn.close()

    @_locked
    def ensure_collector_job(
        self,
        source_id: str,
        *,
        interval_seconds: int,
        max_attempts: int,
        now: str,
    ) -> None:
        existing = self._conn.execute(
            "SELECT source_id FROM collector_jobs WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        if existing is not None:
            return
        self._conn.execute(
            """
            INSERT INTO collector_jobs (
                source_id, interval_seconds, next_run_at, attempt, max_attempts
            ) VALUES (?, ?, ?, 0, ?)
            """,
            (source_id, interval_seconds, now, max_attempts),
        )

    @_locked
    def collector_job(self, source_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM collector_jobs WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        if row is None:
            raise StoreError("not_found")
        return {key: row[key] for key in row.keys()}

    @_locked
    def schedule_collector_job(
        self,
        source_id: str,
        *,
        now: str,
        delay_seconds: int,
        attempt: int,
        status: str,
        error: str | None,
    ) -> None:
        next_run = (datetime.fromisoformat(now) + timedelta(seconds=delay_seconds)).isoformat()
        self._conn.execute(
            """
            UPDATE collector_jobs
            SET next_run_at = ?, attempt = ?, last_status = ?, last_error = ?,
                last_checked_at = ?
            WHERE source_id = ?
            """,
            (next_run, attempt, status, error, now, source_id),
        )

    @_locked
    def conflict_summaries(self) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT contributions.id AS contribution_id, records.provider, records.channel,
                   records.model, records.plan, records.rates_json
            FROM contributions
            JOIN records ON records.contribution_id = contributions.id
            WHERE contributions.status = 'conflict'
            ORDER BY contributions.id, records.id
            """
        ).fetchall()
        grouped: dict[str, dict] = {}
        for row in rows:
            item = grouped.setdefault(
                row["contribution_id"],
                {"contribution_id": row["contribution_id"], "rates": []},
            )
            item["rates"].append(
                {
                    "provider": row["provider"],
                    "channel": row["channel"],
                    "model": row["model"],
                    "plan": row["plan"],
                    "rates": json.loads(row["rates_json"]),
                }
            )
        return list(grouped.values())

    @_locked
    def publisher_id(self) -> str:
        row = self._conn.execute(
            "SELECT publisher_id FROM publisher_identity WHERE singleton = 1"
        ).fetchone()
        return row["publisher_id"]

    def _resolve_snapshot(self, snapshot_id: str) -> sqlite3.Row | None:
        match = _PUBLIC_SNAPSHOT.fullmatch(snapshot_id)
        if match:
            version = int(match.group(1))
            if version > MAX_SAFE_INT:
                return None
            return self._conn.execute(
                "SELECT * FROM snapshots WHERE revision = ?",
                (version,),
            ).fetchone()
        return self._conn.execute(
            "SELECT * FROM snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()

    @_locked
    def record_observation(
        self,
        payload: dict,
        measurement: dict,
        idempotency_key: str | None,
    ) -> tuple[dict, bool]:
        digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
        scope = {key: payload.get(key) or "" for key in _OBSERVATION_SCOPE}
        scope_key = _canonical(scope)
        with self._lock:
            if idempotency_key:
                existing = self._conn.execute(
                    "SELECT payload_sha256, response_json FROM observations WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if existing["payload_sha256"] != digest:
                        raise StoreError("idempotency_conflict")
                    return json.loads(existing["response_json"]), False
            current = self._conn.execute(
                "SELECT payload_sha256, response_json FROM observations WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
            if current is not None:
                if current["payload_sha256"] != digest:
                    raise StoreError("scope_conflict")
                return json.loads(current["response_json"]), False
            observation_id = f"ob_{uuid.uuid4().hex}"
            response = {
                "observation_id": observation_id,
                "status": "recorded",
                "measurement": measurement,
            }
            self._conn.execute(
                """
                INSERT INTO observations (
                    id, status, idempotency_key, payload_sha256, scope_key,
                    provider, channel, model, effort, plan, feature_scope, currency,
                    period_start, period_end, task_category, acceptance,
                    response_json, created_at
                ) VALUES (?, 'recorded', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_id,
                    idempotency_key,
                    digest,
                    scope_key,
                    scope["provider"],
                    scope["channel"],
                    scope["model"],
                    scope["effort"],
                    scope["plan"],
                    scope["feature_scope"],
                    scope["currency"],
                    scope["period_start"],
                    scope["period_end"],
                    scope["task_category"],
                    scope["acceptance"],
                    json.dumps(response, ensure_ascii=False),
                    _now(),
                ),
            )
            return response, True

    @_locked
    def find_measurement(
        self,
        *,
        provider: str,
        channel: str,
        model: str,
        effort: str,
        plan: str,
        feature_scope: str,
        currency: str | None,
        period_start: str | None,
        period_end: str | None,
        task_category: str | None,
        acceptance: str | None,
    ) -> dict | None:
        missing = []
        if not task_category:
            missing.append("task_category")
        if not acceptance:
            missing.append("acceptance")
        if not period_start or not period_end:
            missing.append("period")
        if not currency:
            missing.append("currency")
        if missing:
            return {"missing_fields": missing}
        identity = (
            provider,
            channel,
            model,
            effort or "",
            plan,
            feature_scope,
            period_start,
            period_end,
            task_category,
            acceptance,
        )
        rows = self._conn.execute(
            """
            SELECT currency, response_json FROM observations
            WHERE provider = ? AND channel = ? AND model = ? AND effort = ?
              AND plan = ? AND feature_scope = ? AND period_start = ? AND period_end = ?
              AND task_category = ? AND acceptance = ?
            """,
            identity,
        ).fetchall()
        matched = [row for row in rows if row["currency"] == currency]
        if len(matched) > 1:
            return {"status": "conflict"}
        if len(matched) == 1:
            stored = json.loads(matched[0]["response_json"])
            measurement = dict(stored["measurement"])
            measurement["observation_id"] = stored["observation_id"]
            return measurement
        if rows:
            return {"status": "invalid_input", "assumption": "currency_conversion_refused"}
        return None

    @_locked
    def snapshot_exists(self, snapshot_id: str) -> bool:
        return self._resolve_snapshot(snapshot_id) is not None

    @_locked
    def revision_of(self, snapshot_id: str | None) -> int | None:
        if snapshot_id is None:
            row = self._conn.execute(
                "SELECT revision FROM snapshots ORDER BY revision DESC LIMIT 1"
            ).fetchone()
            return None if row is None else row["revision"]
        resolved = self._resolve_snapshot(snapshot_id)
        return None if resolved is None else resolved["revision"]

    @_locked
    def snapshot_for_export(self, data_version: int | None) -> sqlite3.Row | None:
        if data_version is None:
            return self._conn.execute(
                "SELECT * FROM snapshots ORDER BY revision DESC LIMIT 1"
            ).fetchone()
        if data_version < 1 or data_version > MAX_SAFE_INT:
            return None
        return self._conn.execute(
            "SELECT * FROM snapshots WHERE revision = ?",
            (data_version,),
        ).fetchone()

    @_locked
    def evidence_metadata(self, evidence_ids: list[str]) -> dict[str, dict]:
        if not evidence_ids:
            return {}
        placeholders = ",".join("?" for _ in evidence_ids)
        rows = self._conn.execute(
            f"""
            SELECT id, source_kind, source_url, collector_kind, collector_name, retrieved_at
            FROM evidence
            WHERE id IN ({placeholders})
            """,
            evidence_ids,
        ).fetchall()
        return {
            row["id"]: {
                "kind": row["source_kind"],
                "id": row["id"],
                "source_url": row["source_url"],
                "collector_kind": row["collector_kind"],
                "collector_name": row["collector_name"],
                "retrieved_at": row["retrieved_at"],
            }
            for row in rows
        }

    @_locked
    def catalog(self, snapshot_id: str | None) -> list[dict]:
        if snapshot_id is not None and not self.snapshot_exists(snapshot_id):
            raise StoreError("not_found")
        return self._published_rows(snapshot_id)

    @_locked
    def select_record(
        self,
        *,
        provider: str,
        channel: str,
        model: str,
        effort: str,
        plan: str,
        feature_scope: str,
        window_start: str | None,
        window_end: str | None,
        currency: str | None,
        snapshot_id: str | None,
    ) -> Selection:
        rows = [
            row
            for row in self._published_rows(snapshot_id)
            if row["provider"] == provider
            and row["channel"] == channel
            and row["model"] == model
            and row["effort"] == effort
            and row["plan"] == plan
            and row["feature_scope"] == feature_scope
            and (currency is None or row["currency"] == currency)
        ]
        if window_start is not None or window_end is not None:
            rows = [
                row
                for row in rows
                if row["window_start"] == (window_start or "")
                and row["window_end"] == (window_end or "")
            ]
        grouped: dict[tuple, dict] = {}
        for row in rows:
            key = _identity(row)
            current = grouped.get(key)
            if current is None or row.get("revision", 0) > current.get("revision", 0):
                grouped[key] = row
        if len(grouped) > 1:
            return Selection(record=None, conflict=True)
        if not grouped:
            return Selection(record=None, conflict=False)
        return Selection(record=next(iter(grouped.values())), conflict=False)

    def _published_rows(self, snapshot_id: str | None) -> list[dict]:
        query = """
            SELECT records.*, snapshots.published_at, snapshots.revision,
                   research.id AS research_id
            FROM records
            JOIN snapshots ON snapshots.id = records.snapshot_id
            JOIN research ON research.contribution_id = records.contribution_id
        """
        params: tuple = ()
        if snapshot_id is not None:
            target = self._resolve_snapshot(snapshot_id)
            if target is None:
                return []
            query += " WHERE snapshots.revision <= ?"
            params = (target["revision"],)
        folded: dict[tuple, dict] = {}
        for row in self._conn.execute(query, params).fetchall():
            record = self._record_from_row(row)
            key = _identity(record)
            current = folded.get(key)
            if current is None or record["revision"] > current["revision"]:
                folded[key] = record
        return list(folded.values())

    def _record_from_row(self, row: sqlite3.Row) -> dict:
        keys = set(row.keys())
        published_at = row["published_at"] if "published_at" in keys else None
        research_id = row["research_id"] if "research_id" in keys else None
        revision = row["revision"] if "revision" in keys else 0
        base_snapshot_id = row["base_snapshot_id"] if "base_snapshot_id" in keys else ""
        subscription = {}
        if "subscription_json" in keys and row["subscription_json"]:
            subscription = json.loads(row["subscription_json"])
        record = {
            "id": row["id"],
            "contribution_id": row["contribution_id"],
            "snapshot_id": row["snapshot_id"],
            "provider": row["provider"],
            "channel": row["channel"],
            "model": row["model"],
            "effort": row["effort"],
            "plan": row["plan"],
            "feature_scope": row["feature_scope"],
            "window_start": row["window_start"],
            "window_end": row["window_end"],
            "currency": row["currency"],
            "rates": json.loads(row["rates_json"]),
            "evidence_ids": json.loads(row["evidence_ids_json"]),
            "research_id": research_id,
            "published_at": published_at,
            "revision": revision,
            "base_snapshot_id": base_snapshot_id,
        }
        if subscription:
            record["subscription"] = subscription
        if "record_status" in keys and row["record_status"]:
            record["record_status"] = row["record_status"]
        if "conflict_variants_json" in keys and row["conflict_variants_json"]:
            record["conflict_variants"] = json.loads(row["conflict_variants_json"])
        return record

    @_locked
    def earliest_retrieved_at(self, evidence_ids: list[str]) -> str | None:
        if not evidence_ids:
            return None
        placeholders = ",".join("?" for _ in evidence_ids)
        rows = self._conn.execute(
            f"SELECT id, retrieved_at FROM evidence WHERE id IN ({placeholders})",
            evidence_ids,
        ).fetchall()
        found = {row["id"]: row["retrieved_at"] for row in rows}
        if any(item not in found or not found[item] for item in evidence_ids):
            return None
        parsed = []
        for value in found.values():
            try:
                parsed.append((datetime.fromisoformat(value), value))
            except ValueError:
                return None
        parsed.sort()
        return parsed[0][1]
