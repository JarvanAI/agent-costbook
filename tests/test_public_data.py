import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from agent_costbook.public_data import (
    PublicDataError,
    default_public_paths,
    validate_public_artifact,
)
from agent_costbook.store import Store

ROOT = Path(__file__).resolve().parents[1]
OPENROUTER = "https://openrouter.ai/api/v1/models"
RETRIEVED = "2026-10-08T13:58:41+08:00"
PLANTED_SECRET = "sk-testsecretvalue"
PLANTED_HTML = "<html><body>copyrighted page</body></html>"


def _builder():
    path = ROOT / "scripts" / "build-public-catalog.py"
    spec = importlib.util.spec_from_file_location("build_public_catalog", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_google_cache_write_is_not_a_flat_token_rate(tmp_path):
    source = _mini_source()
    source["prices"][0] = {
        "provider": "google",
        "model": "google/gemini-3.8-flash",
        "raw_pricing": {
            "prompt": "0.00000075",
            "completion": "0.00000375",
            "input_cache_read": "0.000000075",
            "input_cache_write": "0.0000000416666666666667",
        },
    }
    db, _manifest_path = _compile(source, tmp_path)
    store = Store(db, readonly=True)
    try:
        row = next(row for row in store.catalog(None) if row["provider"] == "google")
        assert row["rates"].get("cache_write_per_million") is None
        assert row["rates"]["uncached_input_per_million"] == "0.75"
    finally:
        store.close()


def _mini_source():
    """Two real OpenRouter text rates and one official agent. Not a synthetic card."""
    return {
        "kind": "agent-costbook.public-source",
        "schema_version": 1,
        "reviewed_at": "2026-10-08T14:31:00+08:00",
        "research": {
            "title": "Reviewed OpenRouter text rates",
            "markdown": (
                "Channel openrouter payg text USD. Rates are Decimal(raw)*1000000 "
                "from the 2026-10-08T13:58:41+08:00 models payload. "
                "This review is not a license grant.\n"
            ),
        },
        "evidence": [
            {
                "source_kind": "reviewed_public",
                "source_url": OPENROUTER,
                "collector_kind": "manual_review",
                "collector_name": "ac-public-catalog",
                "content": (
                    "OpenRouter public models metadata retrieved "
                    "2026-10-08T13:58:41+08:00. Text token rates only."
                ),
                "retrieved_at": RETRIEVED,
            }
        ],
        "prices": [
            {
                "provider": "openai",
                "model": "openai/gpt-4o-mini",
                "raw_pricing": {
                    "prompt": "0.00000015",
                    "completion": "0.0000006",
                    "input_cache_read": "0.000000075",
                },
            },
            {
                "provider": "openai",
                "model": "openai/o4-mini",
                "raw_pricing": {
                    "prompt": "0.0000011",
                    "completion": "0.0000044",
                    "input_cache_read": "0.000000275",
                },
            },
        ],
        "agents": [
            {
                "agent_id": "codex",
                "source": "official",
                "source_ref": "https://developers.openai.com/codex/cli",
                "as_of": "2026-10-08T14:06:45+08:00",
                "domain": "terminal coding agent",
                "strengths": "The Codex CLI page says it edits files and runs local tools.",
                "unsuitable": None,
                "can_edit_files": True,
                "can_use_tools": True,
                "input_output_shape": "Terminal session in a repository.",
            }
        ],
        "model_efforts": [
            {
                "provider": "openai",
                "model": "openai/gpt-4o-mini",
                "effort": None,
                "source": "official",
                "source_ref": OPENROUTER,
                "as_of": RETRIEVED,
                "context_length": 128000,
                "benchmarks": None,
                "default_for_agents": None,
            }
        ],
        "sources": [
            {"id": "openrouter_models_api", "url": OPENROUTER, "retrieved_at": RETRIEVED}
        ],
        "exclusions": [
            {
                "id": "license",
                "reason": "Publication review is not a license grant.",
            }
        ],
    }


def _compile(source, directory, name="catalog.sqlite3", update_from=None):
    db = directory / name
    manifest = directory / f"{name}.manifest.json"
    _builder().compile_public_catalog(source, db, manifest, update_from=update_from)
    return db, manifest


def _resign(db, manifest_path, **overrides):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(overrides)
    manifest["database_sha256"] = hashlib.sha256(db.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _edit(db, manifest_path, statement, params=()):
    connection = sqlite3.connect(db)
    connection.execute(statement, params)
    connection.commit()
    connection.close()
    _resign(db, manifest_path)


def test_default_public_paths_point_at_the_package():
    database, manifest = default_public_paths()
    package = Path(default_public_paths.__code__.co_filename).resolve().parent
    assert database == package / "public_catalog" / "catalog.sqlite3"
    assert manifest == package / "public_catalog" / "manifest.json"


def test_builder_keeps_decimal_rates_retrieval_time_and_delete_journal(tmp_path):
    database, manifest_path = _compile(_mini_source(), tmp_path)
    manifest = validate_public_artifact(database, manifest_path)
    assert manifest["kind"] == "agent-costbook.publication"
    assert manifest["schema_version"] == 1
    assert manifest["reviewed"] is True
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    assert validate_public_artifact(database, manifest_path)["database_sha256"] == before
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    connection.close()
    assert not Path(str(database) + "-wal").exists()
    assert not Path(str(database) + "-shm").exists()
    store = Store(database, readonly=True)
    try:
        rows = {row["model"]: row for row in store.catalog(None)}
        assert rows["openai/gpt-4o-mini"]["channel"] == "openrouter"
        assert rows["openai/gpt-4o-mini"]["effort"] == ""
        assert rows["openai/gpt-4o-mini"]["rates"] == {
            "billed_output_per_million": "0.6",
            "cache_read_per_million": "0.075",
            "uncached_input_per_million": "0.15",
        }
        assert rows["openai/o4-mini"]["rates"]["uncached_input_per_million"] == "1.1"
        evidence = store.get_evidence(rows["openai/gpt-4o-mini"]["evidence_ids"][0])
        assert evidence["retrieved_at"] == RETRIEVED
        assert store.agent_capability("codex")["can_edit_files"] is True
        effort = store.model_effort("openai", "openai/gpt-4o-mini", None)
        assert effort["context_length"] == 128000
        assert effort["benchmarks"] is None
        assert effort["default_for_agents"] is None
        assert store.model_effort("openai", "openai/gpt-4o-mini", "low") is None
    finally:
        store.close()


def test_scratch_rebuilds_change_ids_and_updates_keep_publisher_history(tmp_path):
    source = _mini_source()
    first_db, _first_manifest = _compile(source, tmp_path, "first.sqlite3")
    second_db, _second_manifest = _compile(source, tmp_path, "second.sqlite3")
    first = Store(first_db, readonly=True)
    second = Store(second_db, readonly=True)
    try:
        assert first.publisher_id() != second.publisher_id()
        first_mini = next(row for row in first.catalog(None) if row["model"] == "openai/gpt-4o-mini")
        other_id = next(row["id"] for row in first.catalog(None) if row["model"] == "openai/o4-mini")
        publisher = first.publisher_id()
    finally:
        first.close()
        second.close()

    changed = _mini_source()
    changed["prices"][0]["raw_pricing"]["prompt"] = "0.0000003"
    updated_db, updated_manifest = _compile(
        changed, tmp_path, "updated.sqlite3", update_from=first_db
    )
    manifest = json.loads(updated_manifest.read_text(encoding="utf-8"))
    assert manifest["publisher_id"] == publisher
    assert manifest["data_version"] == 2
    store = Store(updated_db, readonly=True)
    try:
        current = {row["model"]: row for row in store.catalog(None)}
        previous = {row["model"]: row for row in store.catalog("snap-1")}
        assert current["openai/gpt-4o-mini"]["rates"]["uncached_input_per_million"] == "0.3"
        assert current["openai/gpt-4o-mini"]["id"] != first_mini["id"]
        assert current["openai/o4-mini"]["id"] == other_id
        assert previous["openai/gpt-4o-mini"]["id"] == first_mini["id"]
        assert previous["openai/gpt-4o-mini"]["rates"]["uncached_input_per_million"] == "0.15"
    finally:
        store.close()

    same_db, same_manifest = _compile(source, tmp_path, "same.sqlite3", update_from=first_db)
    same = json.loads(same_manifest.read_text(encoding="utf-8"))
    assert same["publisher_id"] == publisher
    assert same["data_version"] == 1
    with pytest.raises(PublicDataError) as refused:
        _compile(source, tmp_path, "updated.sqlite3", update_from=first_db)
    assert refused.value.code == "output_exists"


def test_builder_rejects_unrepresentable_price_conditions(tmp_path):
    source = _mini_source()
    source["prices"][0]["raw_pricing"]["overrides"] = [
        {"min_prompt_tokens": 272000, "prompt": "0.0000003"}
    ]
    database = tmp_path / "catalog.sqlite3"
    manifest = tmp_path / "catalog.sqlite3.manifest.json"
    with pytest.raises(PublicDataError) as refused:
        _builder().compile_public_catalog(source, database, manifest)
    assert refused.value.code == "conditional_price"
    assert not database.exists()
    assert not manifest.exists()


def _hash_mismatch(database, manifest_path):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["database_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _publisher_mismatch(database, manifest_path):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["publisher_id"] = "pub_other"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _version_mismatch(database, manifest_path):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["data_version"] = 9
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _schema_invalid(database, manifest_path):
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()
    _resign(database, manifest_path)


def _drop_capability(database, manifest_path):
    _edit(database, manifest_path, "DROP TABLE capability_agent_history")


def _add_observation(database, manifest_path):
    _edit(
        database,
        manifest_path,
        """
        INSERT INTO observations (
            id, status, idempotency_key, payload_sha256, scope_key,
            provider, channel, model, effort, plan, feature_scope, currency,
            period_start, period_end, task_category, acceptance,
            response_json, created_at
        ) VALUES (
            'ob_private', 'recorded', NULL, 'abc', 'scope',
            'openai', 'openrouter', 'openai/gpt-4o-mini', '', 'payg', 'text', 'USD',
            '2026-10-01T00:00:00+00:00', '2026-10-02T00:00:00+00:00', 'coding', 'tests',
            '{}', '2026-10-08T00:00:00+00:00'
        )
        """,
    )


def _add_job(database, manifest_path):
    _edit(
        database,
        manifest_path,
        """
        INSERT INTO collector_jobs (
            source_id, interval_seconds, next_run_at, attempt, max_attempts
        ) VALUES ('job', 60, '2026-10-08T00:00:00+00:00', 0, 1)
        """,
    )


def _draft(database, manifest_path):
    _edit(database, manifest_path, "UPDATE contributions SET status = 'draft'")


def _wal_without_sidecar(database, manifest_path):
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.commit()
    connection.close()
    for suffix in ("-wal", "-shm"):
        Path(str(database) + suffix).unlink(missing_ok=True)
    _resign(database, manifest_path)


def _plant_sidecar(database, manifest_path):
    Path(str(database) + "-wal").write_bytes(b"")


def _synthetic_source(database, manifest_path):
    _edit(
        database,
        manifest_path,
        "UPDATE evidence SET source_kind = 'synthetic_fixture', source_url = 'fixture://synthetic'",
    )


def _aa_source(database, manifest_path):
    connection = sqlite3.connect(database)
    row = connection.execute(
        "SELECT provider, model, effort_key, row_version, body_json FROM capability_model_effort_history"
    ).fetchone()
    body = json.loads(row[4])
    body["source"] = "aa"
    body["benchmarks"] = [
        {"name": "index", "score": "1", "unit": "point", "source": "aa", "source_ref": None}
    ]
    connection.execute(
        """
        UPDATE capability_model_effort_history
        SET body_json = ?
        WHERE provider = ? AND model = ? AND effort_key = ? AND row_version = ?
        """,
        (json.dumps(body), row[0], row[1], row[2], row[3]),
    )
    connection.commit()
    connection.close()
    _resign(database, manifest_path)


def _html_source(database, manifest_path):
    _edit(database, manifest_path, "UPDATE evidence SET content = ?", (PLANTED_HTML,))


def _secret_source(database, manifest_path):
    _edit(
        database,
        manifest_path,
        "UPDATE evidence SET content = ?",
        (f"account token {PLANTED_SECRET}",),
    )


def _sample_publisher(database, manifest_path):
    connection = sqlite3.connect(database)
    connection.execute("UPDATE publisher_identity SET publisher_id = 'pub_sample'")
    connection.commit()
    connection.close()
    _resign(database, manifest_path, publisher_id="pub_sample")


def _user_source(database, manifest_path):
    connection = sqlite3.connect(database)
    row = connection.execute(
        "SELECT agent_id, row_version, body_json FROM capability_agent_history"
    ).fetchone()
    body = json.loads(row[2])
    body["source"] = "user_observation"
    connection.execute(
        "UPDATE capability_agent_history SET body_json = ? WHERE agent_id = ? AND row_version = ?",
        (json.dumps(body), row[0], row[1]),
    )
    connection.commit()
    connection.close()
    _resign(database, manifest_path)


def _not_reviewed(database, manifest_path):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["reviewed"] = False
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _naive_time(database, manifest_path):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["reviewed_at"] = "2026-10-08T14:31:00"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize(
    ("name", "mutate", "code"),
    [
        ("hash", _hash_mismatch, "hash_mismatch"),
        ("publisher", _publisher_mismatch, "publisher_mismatch"),
        ("version", _version_mismatch, "version_mismatch"),
        ("schema", _schema_invalid, "schema_invalid"),
        ("capability table", _drop_capability, "capability_tables_missing"),
        ("observation", _add_observation, "observations_present"),
        ("collector job", _add_job, "collector_jobs_present"),
        ("draft", _draft, "unpublished_contribution"),
        ("wal", _wal_without_sidecar, "journal_invalid"),
        ("sidecar", _plant_sidecar, "sidecar_dependency"),
        ("synthetic", _synthetic_source, "source_rejected"),
        ("aa", _aa_source, "source_rejected"),
        ("html", _html_source, "source_rejected"),
        ("secret", _secret_source, "source_rejected"),
        ("sample publisher", _sample_publisher, "source_rejected"),
        ("user observation", _user_source, "source_rejected"),
        ("unreviewed", _not_reviewed, "manifest_invalid"),
        ("naive timestamp", _naive_time, "manifest_invalid"),
    ],
)
def test_validate_rejects_unsafe_artifacts(tmp_path, name, mutate, code):
    database, manifest_path = _compile(_mini_source(), tmp_path)
    mutate(database, manifest_path)
    with pytest.raises(PublicDataError) as refused:
        validate_public_artifact(database, manifest_path)
    assert refused.value.code == code
    message = refused.value.message
    assert PLANTED_SECRET not in message
    assert "<html" not in message.lower()
    assert "copyrighted page" not in message


def test_packaged_artifact_exposes_real_prices_and_independent_facts():
    database, manifest_path = default_public_paths()
    manifest = validate_public_artifact(database, manifest_path)
    assert manifest["kind"] == "agent-costbook.publication"
    assert manifest["reviewed"] is True
    assert manifest["schema_version"] == 1
    assert "license grant" in json.dumps(manifest["exclusions"]).lower() or any(
        "license" in str(item).lower() for item in manifest["exclusions"]
    )
    store = Store(database, readonly=True)
    try:
        assert store.publisher_id() == manifest["publisher_id"]
        assert store.publisher_id() != "pub_sample"
        rows = list(store.catalog(None))
        by_model = {row["model"]: row for row in rows}
        quote = by_model["openai/gpt-4o-mini"]
        assert quote["provider"] == "openai"
        assert quote["channel"] == "openrouter"
        assert quote["plan"] == "payg"
        assert quote["feature_scope"] == "text"
        assert quote["currency"] == "USD"
        assert quote["effort"] == ""
        assert quote["rates"]["uncached_input_per_million"] == "0.15"
        assert quote["rates"]["billed_output_per_million"] == "0.6"
        assert quote["rates"]["cache_read_per_million"] == "0.075"
        assert "cache_write_per_million" not in quote["rates"]
        assert all(row["effort"] == "" for row in rows)
        assert "openai/gpt-6.1-sol" not in by_model
        assert "openai/gpt-6-astra" not in by_model
        assert "x-ai/grok-4.7" not in by_model
        assert "google/gemini-4" not in by_model
        assert not any(model.startswith("google/gemma-") for model in by_model)
        opus = by_model["anthropic/claude-opus-5.5"]
        assert opus["rates"]["uncached_input_per_million"] == "4"
        assert "cache_write_per_million" not in opus["rates"]
        gemini = by_model["google/gemini-3.8-flash"]
        assert gemini["rates"].get("cache_write_per_million") is None
        assert gemini["model"] != "google/gemini-4"
        effort = store.model_effort("openai", "openai/gpt-4o-mini", None)
        assert effort["context_length"] == 128000
        assert effort["benchmarks"] is None
        assert effort["default_for_agents"] is None
        assert store.model_effort("x-ai", "x-ai/grok-4.7", None)["context_length"] == 500000
        assert store.model_effort("openai", "openai/gpt-6-astra", "low") is None
        for name in ("low", "medium", "high", "xhigh", "max"):
            tier = store.model_effort("openai", "openai/gpt-6.1-sol", name)
            assert tier["effort"] == name
            assert tier["benchmarks"] is None
            assert tier["default_for_agents"] is None
            assert tier["context_length"] == 1050000
            assert tier["source"] == "official"
        assert store.model_effort("openai", "openai/gpt-6.1-sol", None) is None
        assert store.agent_capability("codex")["can_edit_files"] is True
        assert store.agent_capability("claude-code")["can_use_tools"] is True
        assert store.agent_capability("cursor")["can_edit_files"] is True
        assert store.agent_capability("gemini-cli")["can_edit_files"] is True
        assert store.agent_capability("grok-build") is None
        evidence = store.get_evidence(quote["evidence_ids"][0])
        assert RETRIEVED in evidence["content"] or evidence["retrieved_at"] == RETRIEVED
        assert "<html" not in evidence["content"].lower()
        counts = store.current_capabilities()
        assert manifest["coverage"]["prices"] == len(rows)
        assert manifest["coverage"]["agents"] == len(counts["agents"])
        assert manifest["coverage"]["model_efforts"] == len(counts["model_efforts"])
        assert manifest["data_version"] == 1
    finally:
        store.close()
