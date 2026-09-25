from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


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
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def _migrate(self) -> None:
        self._conn.executescript(
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
                published_at TEXT NOT NULL
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
                evidence_ids_json TEXT NOT NULL
            );
            """
        )
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
                        rates_json, evidence_ids_json
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        )
                        for row in record_rows
                    ],
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise StoreError("idempotency_conflict") from exc
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
                    self._conn.execute(
                        "UPDATE contributions SET status = 'conflict' WHERE id = ?",
                        (contribution_id,),
                    )
                    self._conn.commit()
                    raise StoreError("conflict")
                seen.add(identity)
            snapshot_id = f"snap_{uuid.uuid4().hex}"
            published_at = _now()
            self._conn.execute("BEGIN")
            self._conn.execute(
                "INSERT INTO snapshots (id, contribution_id, published_at) VALUES (?, ?, ?)",
                (snapshot_id, contribution_id, published_at),
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
            return {
                "contribution_id": contribution_id,
                "status": "published",
                "snapshot_id": snapshot_id,
            }

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
    def snapshot_exists(self, snapshot_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
        return row is not None

    @_locked
    def catalog(self, snapshot_id: str | None) -> list[dict]:
        if snapshot_id is not None and not self.snapshot_exists(snapshot_id):
            raise StoreError("not_found")
        rows = self._published_rows(snapshot_id)
        if snapshot_id is not None:
            return rows
        latest: dict[tuple, dict] = {}
        for row in rows:
            key = _identity(row)
            current = latest.get(key)
            if current is None or (row["published_at"], row["snapshot_id"]) > (
                current["published_at"],
                current["snapshot_id"],
            ):
                latest[key] = row
        return list(latest.values())

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
            if current is None or (row["published_at"], row["snapshot_id"]) > (
                current["published_at"],
                current["snapshot_id"],
            ):
                grouped[key] = row
        if len(grouped) > 1:
            return Selection(record=None, conflict=True)
        if not grouped:
            return Selection(record=None, conflict=False)
        return Selection(record=next(iter(grouped.values())), conflict=False)

    def _published_rows(self, snapshot_id: str | None) -> list[dict]:
        query = """
            SELECT records.*, snapshots.published_at, research.id AS research_id
            FROM records
            JOIN snapshots ON snapshots.id = records.snapshot_id
            JOIN research ON research.contribution_id = records.contribution_id
        """
        params: tuple = ()
        if snapshot_id is not None:
            query += " WHERE records.snapshot_id = ?"
            params = (snapshot_id,)
        rows = self._conn.execute(query, params).fetchall()
        return [self._record_from_row(row) for row in rows]

    def _record_from_row(self, row: sqlite3.Row) -> dict:
        published_at = row["published_at"] if "published_at" in row.keys() else None
        research_id = row["research_id"] if "research_id" in row.keys() else None
        return {
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
        }
