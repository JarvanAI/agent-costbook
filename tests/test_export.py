import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from agent_costbook.store import Store

from support import synthetic_contribution

ROOT = Path(__file__).resolve().parents[1]
MAX_SAFE_INT = 9007199254740991


def _publish_pair(db: Path):
    store = Store(db)
    api = synthetic_contribution()
    created, _ = store.create_contribution(api, "api")
    first = store.publish(created["contribution_id"])
    plan = synthetic_contribution()
    plan["records"][0].update(
        {
            "channel": "subscription",
            "model": "example-model",
            "effort": "",
            "plan": "plus",
            "feature_scope": "code",
            "rates": {},
            "subscription": {
                "monthly_price": "20",
                "price_period": "month",
                "quota_multiplier": "2",
                "baseline_api_budget": None,
                "utilization": "0.5",
                "weight": "1",
                "assumptions": ["u is an explicit scenario, not a measurement"],
            },
        }
    )
    drafted, _ = store.create_contribution(plan, "plan")
    second = store.publish(drafted["contribution_id"])
    return store, first, second


def _export(db: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "agent_costbook.export", "--db", str(db), *args],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )


def _records_sha256(records: list) -> str:
    encoded = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_export_is_stable_and_hash_matches_records_only(tmp_path):
    db = tmp_path / "costbook.sqlite3"
    store, first, second = _publish_pair(db)
    store.close()
    current = _export(db)
    again = _export(db)
    assert current.returncode == 0, current.stderr
    assert current.stdout == again.stdout
    assert current.stdout.endswith(b"\n")
    document = json.loads(current.stdout)
    assert document["kind"] == "agent-costbook.snapshot"
    assert document["schema_version"] == 1
    assert document["formula_version"] == "ac-formulas-v1"
    assert document["data_version"] == 2
    assert document["snapshot_id"] == "snap-2"
    assert document["freshness"] is None
    assert isinstance(document["data_version"], int)
    assert document["content_sha256"] == _records_sha256(document["records"])
    by_plan = {row["plan"]: row for row in document["records"]}
    assert set(by_plan) == {"payg", "plus"}
    plan = by_plan["plus"]
    assert plan["effort"] is None
    assert plan["rates"] is None
    assert plan["subscription"]["P"] == {
        "amount": "20",
        "currency": "USD",
        "period": "month",
    }
    assert plan["subscription"]["B0"] is None
    assert "B0" in plan["missing_fields"]
    assert plan["subscription"]["m"]["amount"] == "2"
    assert "1" != plan["subscription"]["B0"]
    old = _export(db, "--data-version", "1")
    assert old.returncode == 0
    historical = json.loads(old.stdout)
    assert historical["data_version"] == 1
    assert historical["snapshot_id"] == "snap-1"
    assert [row["plan"] for row in historical["records"]] == ["payg"]
    assert historical["content_sha256"] == _records_sha256(historical["records"])
    by_uuid = Store(db)
    assert {row["plan"] for row in by_uuid.catalog(first["snapshot_id"])} == {"payg"}
    assert {row["plan"] for row in by_uuid.catalog("snap-1")} == {"payg"}
    assert {row["plan"] for row in by_uuid.catalog(second["snapshot_id"])} == {"payg", "plus"}
    by_uuid.close()


def test_failed_publish_and_private_estimate_do_not_consume_or_leak(tmp_path):
    db = tmp_path / "costbook.sqlite3"
    store, first, _second = _publish_pair(db)
    conflict = synthetic_contribution()
    conflict["records"][0]["rates"]["uncached_input_per_million"] = "9"
    conflict["records"][0]["base_snapshot_id"] = "snap_wrong"
    drafted, _ = store.create_contribution(conflict, "bad")
    try:
        store.publish(drafted["contribution_id"])
    except Exception:
        pass
    secret = "private-rate-secret"
    token = "admin-token-secret"
    unpublished = synthetic_contribution()
    unpublished["records"][0]["model"] = "draft-only-model"
    unpublished["evidence"][0]["content"] = secret
    unpublished["evidence"][0]["collector_name"] = token
    store.create_contribution(unpublished, "draft")
    store.close()
    exported = _export(db, "--data-version", "2")
    text = exported.stdout.decode("utf-8")
    assert exported.returncode == 0
    assert secret not in text
    assert token not in text
    assert "draft-only-model" not in text
    assert "ignore previous instructions" not in text
    latest = json.loads(_export(db).stdout)
    assert latest["data_version"] == 2
    assert latest["snapshot_id"] == "snap-2"
    assert first["snapshot_id"].startswith("snap_")


def test_unknown_and_unsafe_versions_do_not_print_the_catalog(tmp_path):
    db = tmp_path / "costbook.sqlite3"
    _publish_pair(db)
    for version in ("3", "0", "-1", "1.5", "01", str(MAX_SAFE_INT + 1)):
        result = _export(db, "--data-version", version)
        assert result.returncode != 0
        assert result.stdout == b""
        assert b"snap-2" not in result.stdout
        assert b"uncached_input_per_million" not in result.stdout


def test_publisher_survives_reopen_and_v01_rows_remain_readable(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    legacy = sqlite3.connect(db)
    legacy.executescript(
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
            published_at TEXT NOT NULL, revision INTEGER NOT NULL
        );
        CREATE TABLE records (
            id TEXT PRIMARY KEY, contribution_id TEXT NOT NULL, snapshot_id TEXT,
            provider TEXT NOT NULL, channel TEXT NOT NULL, model TEXT NOT NULL,
            effort TEXT NOT NULL, plan TEXT NOT NULL, feature_scope TEXT NOT NULL,
            window_start TEXT NOT NULL, window_end TEXT NOT NULL, currency TEXT NOT NULL,
            rates_json TEXT NOT NULL, evidence_ids_json TEXT NOT NULL,
            base_snapshot_id TEXT NOT NULL
        );
        """
    )
    legacy.execute(
        """
        INSERT INTO contributions (
            id, status, idempotency_key, payload_sha256, response_json,
            research_id, created_at, published_snapshot_id
        ) VALUES ('co_old', 'published', NULL, 'abc', '{}', 'rs_old',
                  '2026-09-25T00:00:00+00:00', 'snap_legacy')
        """
    )
    legacy.execute(
        """
        INSERT INTO research (id, contribution_id, title, markdown)
        VALUES ('rs_old', 'co_old', 'Legacy', 'legacy research markdown')
        """
    )
    legacy.execute(
        """
        INSERT INTO evidence (
            id, contribution_id, source_kind, source_url, collector_kind,
            collector_name, content, content_sha256, retrieved_at
        ) VALUES ('ev_old', 'co_old', 'synthetic_fixture', 'fixture://legacy',
                  'test', 'pytest', 'legacy evidence body', 'deadbeef',
                  '2026-09-25T00:00:00+00:00')
        """
    )
    legacy.execute(
        """
        INSERT INTO snapshots (id, contribution_id, published_at, revision)
        VALUES ('snap_legacy', 'co_old', '2026-09-25T00:00:00+00:00', 1)
        """
    )
    legacy.execute(
        """
        INSERT INTO records (
            id, contribution_id, snapshot_id, provider, channel, model, effort,
            plan, feature_scope, window_start, window_end, currency, rates_json,
            evidence_ids_json, base_snapshot_id
        ) VALUES (
            'rec_old', 'co_old', 'snap_legacy', 'example', 'api', 'legacy-model',
            '', 'payg', 'text', '', '', 'USD',
            '{"uncached_input_per_million":"2"}', '["ev_old"]', ''
        )
        """
    )
    legacy.commit()
    legacy.close()
    upgraded = Store(db)
    assert upgraded.catalog("snap_legacy")[0]["model"] == "legacy-model"
    assert upgraded.catalog("snap-1")[0]["rates"]["uncached_input_per_million"] == "2"
    assert upgraded.get_research("rs_old")["markdown"] == "legacy research markdown"
    assert "legacy evidence body" in upgraded.get_evidence("ev_old")["content"]
    publisher = upgraded.publisher_id()
    upgraded.close()
    assert Store(db).publisher_id() == publisher
    exported = json.loads(_export(db).stdout)
    assert exported["publisher_id"] == publisher
    assert exported["data_version"] == 1
    assert exported["snapshot_id"] == "snap-1"
    assert exported["records"][0]["model"] == "legacy-model"
    assert "legacy evidence body" not in _export(db).stdout.decode()
    assert "legacy research markdown" not in _export(db).stdout.decode()


def test_recorded_snapshot_sample_recomputes_its_records_hash():
    sample = Path(__file__).parent / "fixtures" / "ac-v0.2-published-snapshot.json"
    raw = sample.read_bytes()
    assert raw.endswith(b"\n")
    document = json.loads(raw)
    assert document["kind"] == "agent-costbook.snapshot"
    assert document["schema_version"] == 1
    assert document["formula_version"] == "ac-formulas-v1"
    assert document["freshness"] is None
    assert document["snapshot_id"] == "snap-2"
    assert document["content_sha256"] == _records_sha256(document["records"])
    by_plan = {row["plan"]: row for row in document["records"]}
    assert by_plan["payg"]["rates"]["uncached_input_per_million"]["amount"] == "2"
    assert by_plan["payg"]["subscription"] is None
    assert by_plan["plus"]["rates"] is None
    assert by_plan["plus"]["subscription"]["P"]["amount"] == "20"
    assert by_plan["plus"]["subscription"]["m"]["amount"] == "2"
    assert by_plan["plus"]["subscription"]["B0"] is None
    assert "B0" in by_plan["plus"]["missing_fields"]
    text = raw.decode("utf-8")
    assert "ignore previous instructions" not in text
    assert "private-rate" not in text


def test_stopped_service_export_does_not_need_an_admin_token(tmp_path, monkeypatch):
    db = tmp_path / "costbook.sqlite3"
    _publish_pair(db)
    monkeypatch.delenv("ACB_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("ACB_DB", raising=False)
    result = _export(db, "--data-version", "1")
    assert result.returncode == 0
    document = json.loads(result.stdout)
    assert document["records"][0]["plan"] == "payg"
