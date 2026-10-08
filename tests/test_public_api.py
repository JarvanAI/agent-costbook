import asyncio
import hashlib
import importlib.util
import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_costbook import __version__
from agent_costbook.api import create_app
from agent_costbook.export import build_document
from agent_costbook.public_api import PUBLIC_METHODS, PublicArtifactError, create_public_app
from agent_costbook.settings import Settings
from agent_costbook.store import Store
from support import SYNTHETIC_RATES

AS_OF = "2026-09-27T00:00:00+00:00"
RETRIEVED = "2026-09-25T00:00:00+00:00"
MODEL = "m4-card"
NEXT_MODEL = "m4-next"
OFFICIAL_PRICE = "https://official.test/prices/m4-card"
UNLINKED_MARKER = "unlinked-review-marker-7f3c"
SECRET = "priv-echo-7f3c9a"
USAGE = {
    "uncached_input": "1000",
    "cache_read": "2000",
    "cache_write": "500",
    "billed_output": "400",
    "reasoning": "80",
}
INFO_KEYS = {
    "kind",
    "schema_version",
    "api_version",
    "service_version",
    "profile",
    "publisher_id",
    "catalog",
    "capabilities",
    "access",
    "limits",
    "usage_assumptions",
}


def _canonical(payload) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(payload) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _candidate(candidate_id="known", **extra) -> dict:
    body = {
        "candidate_id": candidate_id,
        "provider": "example",
        "channel": "api",
        "model": MODEL,
        "effort": "",
        "plan": "payg",
        "feature_scope": "text",
    }
    body.update(extra)
    return body


def _estimate(**overrides) -> dict:
    body = {
        "method": "M4",
        "currency": "USD",
        "usage": dict(USAGE),
        "extra_cost": "0",
        "candidates": [_candidate(agent_id="agent-a")],
    }
    body.update(overrides)
    return body


def _compiler():
    path = Path(__file__).resolve().parents[1] / "scripts" / "build-public-catalog.py"
    spec = importlib.util.spec_from_file_location("build_public_catalog", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.compile_public_catalog


def _source(models: tuple[str, ...]) -> dict:
    """Small reviewed fixture. Price numbers are the original synthetic card."""
    return {
        "kind": "agent-costbook.public-source",
        "schema_version": 1,
        "reviewed_at": AS_OF,
        "research": {
            "title": "Reviewed public rate card",
            "markdown": (
                "Original fixture price facts for the public API tests. "
                "Not a provider quote.\n"
            ),
        },
        "evidence": [
            {
                "source_kind": "reviewed_public",
                "source_url": OFFICIAL_PRICE,
                "collector_kind": "manual_review",
                "collector_name": "ac-public-fixture",
                "content": "Reviewed public rate card for the owned API fixture.",
                "retrieved_at": RETRIEVED,
            },
            {
                "source_kind": "reviewed_public",
                "source_url": "https://official.test/prices/unlinked",
                "collector_kind": "manual_review",
                "collector_name": "ac-public-fixture",
                "content": UNLINKED_MARKER,
                "retrieved_at": RETRIEVED,
            },
        ],
        "prices": [
            {
                "provider": "example",
                "channel": "api",
                "model": model,
                "plan": "payg",
                "feature_scope": "text",
                "currency": "USD",
                "evidence_index": 0,
                "raw_pricing": {
                    "prompt": "0.000002",
                    "completion": "0.000008",
                    "input_cache_read": "0.0000005",
                    "input_cache_write": "0.000003",
                },
            }
            for model in models
        ],
        "agents": [
            {
                "agent_id": "agent-b",
                "source": "official",
                "source_ref": "https://official.test/agents/agent-b",
                "as_of": AS_OF,
                "strengths": "strength-agent-b",
                "can_edit_files": True,
                "can_use_tools": False,
            },
            {
                "agent_id": "agent-a",
                "source": "official",
                "source_ref": "https://official.test/agents/agent-a",
                "as_of": AS_OF,
                "strengths": "strength-agent-a",
                "can_edit_files": True,
                "can_use_tools": False,
            },
        ],
        "model_efforts": [
            {
                "provider": "zeta",
                "model": "other",
                "effort": "low",
                "source": "official",
                "source_ref": "https://official.test/efforts/other",
                "as_of": AS_OF,
                "suitable": "other tier",
                "benchmarks": None,
                "default_for_agents": None,
            },
            {
                "provider": "example",
                "model": MODEL,
                "effort": None,
                "source": "official",
                "source_ref": "https://official.test/efforts/m4-card",
                "as_of": AS_OF,
                "suitable": "null tier",
                "benchmarks": None,
                "default_for_agents": None,
            },
            {
                "provider": "example",
                "model": MODEL,
                "effort": "low",
                "source": "official",
                "source_ref": "https://official.test/efforts/m4-card",
                "as_of": AS_OF,
                "suitable": "named tier",
                "benchmarks": None,
                "default_for_agents": None,
            },
        ],
        "sources": [
            {
                "id": "official_test_rate_card",
                "url": OFFICIAL_PRICE,
                "retrieved_at": RETRIEVED,
            }
        ],
        "exclusions": [
            {
                "id": "private_guard",
                "reason": "Private observations and credentials stay out of this reviewed fixture.",
            }
        ],
    }


def _unlinked_evidence_id(database: Path, linked: set[str]) -> str:
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT id, source_url FROM evidence").fetchall()
    finally:
        connection.close()
    for row in rows:
        if row["id"] not in linked and str(row["source_url"]).endswith("/unlinked"):
            return row["id"]
    raise AssertionError("reviewed fixture is missing unlinked evidence")


def _prepare(directory: Path, *, versions: int = 1) -> dict:
    compile_public_catalog = _compiler()
    database = directory / "public.sqlite3"
    manifest_path = directory / "manifest.json"
    if versions == 1:
        compile_public_catalog(_source((MODEL,)), database, manifest_path)
    else:
        base = directory / "base.sqlite3"
        compile_public_catalog(
            _source((MODEL,)),
            base,
            directory / "base.manifest.json",
        )
        compile_public_catalog(
            _source((MODEL, NEXT_MODEL)),
            database,
            manifest_path,
            update_from=base,
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["kind"] == "agent-costbook.publication"
    assert manifest["schema_version"] == 1
    assert manifest["reviewed"] is True
    assert manifest["sources"][0]["url"] == OFFICIAL_PRICE
    assert manifest["exclusions"][0]["id"] == "private_guard"
    store = Store(database, readonly=True)
    try:
        current = store.catalog(None)
        linked: set[str] = set()
        for row in current:
            linked.update(row["evidence_ids"])
        primary = next(row for row in current if row["model"] == MODEL)
        assert primary["rates"] == SYNTHETIC_RATES
        publisher = store.publisher_id()
    finally:
        store.close()
    return {
        "db": database,
        "manifest": manifest_path,
        "publisher_id": publisher,
        "linked_evidence_id": primary["evidence_ids"][0],
        "unlinked_evidence_id": _unlinked_evidence_id(database, linked),
        "research_id": primary["research_id"],
        "record_snapshot_id": primary["snapshot_id"],
    }


def _assert_error(response, status: int, code: str, secret: str | None = None) -> dict:
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error"}
    error = body["error"]
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    if status == 422:
        assert error["details"]
        for item in error["details"]:
            assert set(item) == {"type", "loc"}
        raw = response.text
        assert '"input"' not in raw
        assert '"ctx"' not in raw
        assert '"context"' not in raw
    else:
        assert "details" not in error
    if secret is not None:
        assert secret not in response.text
    assert response.headers["cache-control"] == "no-store"
    return error


@pytest.fixture
def published(tmp_path):
    return _prepare(tmp_path)


@pytest.fixture
def public_app(published):
    return create_public_app(published["db"], published["manifest"])


@pytest.fixture
def client(public_app):
    return TestClient(public_app)


def test_absent_packaged_data_does_not_invent_a_database(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent_costbook.public_data", None)
    with pytest.raises(PublicArtifactError, match="public artifact is not available"):
        create_public_app()


def test_missing_validator_does_not_parse_an_explicit_manifest(published, monkeypatch):
    monkeypatch.setitem(sys.modules, "agent_costbook.public_data", None)
    published["manifest"].write_text("{not-json private-body-marker}", encoding="utf-8")
    before = hashlib.sha256(published["db"].read_bytes()).hexdigest()
    with pytest.raises(PublicArtifactError, match="public artifact is not available") as caught:
        create_public_app(published["db"], published["manifest"])
    assert "private-body-marker" not in str(caught.value)
    assert hashlib.sha256(published["db"].read_bytes()).hexdigest() == before
    assert not Path(str(published["db"]) + "-wal").exists()
    assert not Path(str(published["db"]) + "-shm").exists()


def test_validator_rejection_is_sanitized(published, monkeypatch):
    from agent_costbook.public_data import PublicDataError

    def validate_public_artifact(db_path, manifest_path):
        del db_path
        leaked = Path(manifest_path).read_text(encoding="utf-8")
        raise PublicDataError("source_rejected", leaked)

    monkeypatch.setattr(
        "agent_costbook.public_data.validate_public_artifact",
        validate_public_artifact,
    )
    with pytest.raises(PublicArtifactError, match="public artifact is not available") as caught:
        create_public_app(published["db"], published["manifest"])
    assert "official.test" not in str(caught.value)
    assert "database_sha256" not in str(caught.value)


def test_rejected_artifact_does_not_start(published):
    manifest = json.loads(published["manifest"].read_text(encoding="utf-8"))
    manifest["reviewed"] = False
    published["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(PublicArtifactError, match="public artifact is not available") as caught:
        create_public_app(published["db"], published["manifest"])
    assert published["db"].name not in str(caught.value)


def test_paths_must_be_supplied_together(published):
    with pytest.raises(PublicArtifactError):
        create_public_app(published["db"])


def test_empty_database_does_not_start(tmp_path):
    database = tmp_path / "empty.sqlite3"
    Store(database).close()
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(PublicArtifactError, match="public artifact is not available"):
        create_public_app(database, manifest)


def test_catalog_uses_exporter_document_and_cache_policy(client, published):
    store = Store(published["db"], readonly=True)
    try:
        expected = build_document(store, 1)
    finally:
        store.close()
    current = client.get("/v1/catalog")
    pinned = client.get("/v1/catalog", params={"snapshot_id": "snap-1"})
    assert current.status_code == 200
    assert current.content == _canonical(expected)
    assert current.json()["kind"] == "agent-costbook.snapshot"
    assert current.json()["schema_version"] == 1
    assert current.json()["freshness"] is None
    assert current.json()["content_sha256"] == _sha256(current.json()["records"])
    assert current.headers["etag"] == f'"{hashlib.sha256(current.content).hexdigest()}"'
    changed = dict(current.json())
    changed["published_at"] = "1999-01-01T00:00:00+00:00"
    assert hashlib.sha256(_canonical(changed)).hexdigest() != current.headers["etag"].strip('"')
    assert _sha256(changed["records"]) == current.json()["content_sha256"]
    assert current.headers["cache-control"] == "public, max-age=300"
    assert "immutable" not in current.headers["cache-control"]
    assert "s-maxage" not in current.headers["cache-control"]
    assert pinned.content == current.content
    assert pinned.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "s-maxage" not in pinned.headers["cache-control"]


def test_get_etag_matches_weak_list_and_star(client):
    first = client.get("/v1/info")
    etag = first.headers["etag"]
    assert etag.startswith('"') and not etag.startswith('W/"')
    for header in (etag, f"W/{etag}", f'W/"deadbeef", {etag}', "*"):
        cached = client.get("/v1/info", headers={"If-None-Match": header})
        assert cached.status_code == 304
        assert cached.content == b""
        assert cached.headers["etag"] == etag
        assert cached.headers["cache-control"] == first.headers["cache-control"]
    missed = client.get("/v1/info", headers={"If-None-Match": '"deadbeef"'})
    assert missed.status_code == 200
    assert missed.content == first.content
    posted = client.post(
        "/v1/estimates",
        json=_estimate(),
        headers={"If-None-Match": "*"},
    )
    assert posted.status_code == 200
    assert posted.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "snapshot_id",
    ["internal", "snap-0", "snap-2", "snap-01", "latest", pytest.param("snap-" + "9" * 4301, id="oversized-number")],
)
def test_unknown_or_internal_snapshot_is_not_found(client, published, snapshot_id):
    if snapshot_id == "internal":
        snapshot_id = published["record_snapshot_id"]
        assert snapshot_id.startswith("snap_")
    for response in (
        client.get("/v1/catalog", params={"snapshot_id": snapshot_id}),
        client.post("/v1/estimates", json=_estimate(snapshot_id=snapshot_id)),
    ):
        _assert_error(response, 404, "snapshot_not_found", snapshot_id)


def test_older_snapshot_does_not_fall_back_to_current(tmp_path):
    prepared = _prepare(tmp_path, versions=2)
    client = TestClient(create_public_app(prepared["db"], prepared["manifest"]))
    old = client.get("/v1/catalog", params={"snapshot_id": "snap-1"})
    current = client.get("/v1/catalog")
    explicit_current = client.get("/v1/catalog", params={"snapshot_id": "snap-2"})
    assert old.status_code == 200
    assert {row["model"] for row in old.json()["records"]} == {MODEL}
    assert {row["model"] for row in current.json()["records"]} == {MODEL, NEXT_MODEL}
    assert current.json()["snapshot_id"] == "snap-2"
    assert explicit_current.json()["snapshot_id"] == "snap-2"
    assert old.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert explicit_current.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert current.headers["cache-control"] == "public, max-age=300"
    frozen = client.post(
        "/v1/estimates",
        json=_estimate(
            snapshot_id="snap-1",
            candidates=[_candidate("known"), _candidate("missing", model="not-published")],
        ),
    )
    assert frozen.status_code == 200
    body = frozen.json()
    assert body["data_version"] == 1
    assert body["snapshot_id"] == "snap-1"
    assert {item["snapshot_id"] for item in body["results"]} == {"snap-1"}
    assert {item["data_version"] for item in body["results"]} == {1}


def test_capabilities_keep_real_rows_sort_and_independent_hash(client, published):
    listed = client.get("/v1/capabilities")
    assert listed.status_code == 200
    body = listed.json()
    assert body["kind"] == "agent-costbook.capabilities"
    assert body["schema_version"] == 1
    assert body["publisher_id"] == published["publisher_id"]
    assert [row["agent_id"] for row in body["agents"]] == ["agent-a", "agent-b"]
    assert [
        (row["provider"], row["model"], row["effort"]) for row in body["model_efforts"]
    ] == [
        ("example", MODEL, None),
        ("example", MODEL, "low"),
        ("zeta", "other", "low"),
    ]
    null_row = body["model_efforts"][0]
    assert null_row["row_version"] == 1
    assert null_row["as_of"] == AS_OF
    assert null_row["source"] == "official"
    assert null_row["benchmarks"] is None
    assert body["content_sha256"] == _sha256(
        {"agents": body["agents"], "model_efforts": body["model_efforts"]}
    )
    assert _sha256({"publisher_id": body["publisher_id"], **{
        "agents": body["agents"],
        "model_efforts": body["model_efforts"],
    }}) != body["content_sha256"]
    assert listed.headers["cache-control"] == "public, max-age=300"
    info = client.get("/v1/info")
    assert info.json()["capabilities"]["content_sha256"] == body["content_sha256"]
    assert info.json()["capabilities"]["agent_count"] == 2
    assert info.json()["capabilities"]["model_effort_count"] == 3
    assert set(info.json()) == INFO_KEYS
    assert info.json()["service_version"] == __version__
    assert info.json()["limits"] == {
        "body_bytes": 65536,
        "candidates": 64,
        "requests": 60,
        "window_seconds": 60,
        "scope": "instance",
    }
    assert info.json()["access"] == {
        "anonymous_read": True,
        "remote_write": False,
        "admin_publication": "reviewed-artifact",
    }
    assert info.json()["usage_assumptions"] == {"supported": False}
    assert info.json()["catalog"]["content_sha256"] == client.get("/v1/catalog").json()["content_sha256"]

    omitted = client.get(
        "/v1/capabilities/model-efforts",
        params={"provider": "example", "model": MODEL},
    )
    blank = client.get(
        "/v1/capabilities/model-efforts",
        params={"provider": "example", "model": MODEL, "effort": ""},
    )
    named = client.get(
        "/v1/capabilities/model-efforts",
        params={"provider": "example", "model": MODEL, "effort": "low"},
    )
    assert omitted.json()["effort"] is None
    assert omitted.json()["suitable"] == "null tier"
    assert blank.json() == omitted.json()
    assert named.json()["effort"] == "low"
    assert named.headers["etag"] != omitted.headers["etag"]
    assert "immutable" not in named.headers["cache-control"]
    assert named.headers["cache-control"] == "public, max-age=300"
    missing = client.get("/v1/capabilities/agents", params={"agent_id": "missing-agent"})
    _assert_error(missing, 404, "capability_not_found")
    history = client.get(
        "/v1/capabilities/agents/history",
        params={"agent_id": "agent-a"},
    )
    assert history.status_code == 404
    assert "strength-agent-a" not in history.text


def test_single_capability_etag_is_independent_of_the_collection(client):
    single = client.get("/v1/capabilities/agents", params={"agent_id": "agent-a"})
    collection = client.get("/v1/capabilities")
    assert single.status_code == 200
    assert single.headers["etag"] != collection.headers["etag"]
    assert single.headers["cache-control"] == "public, max-age=300"
    assert "immutable" not in single.headers["cache-control"]


def test_sources_require_published_record_linkage(client, published):
    linked = published["linked_evidence_id"]
    unlinked = published["unlinked_evidence_id"]
    research = published["research_id"]
    assert client.get(f"/v1/evidence/{linked}").status_code == 200
    assert client.get(f"/v1/evidence/{linked}").json()["source_url"] == OFFICIAL_PRICE
    assert client.get(f"/v1/research/{research}").status_code == 200
    hidden_evidence = client.get(f"/v1/evidence/{unlinked}")
    _assert_error(hidden_evidence, 404, "evidence_not_found", UNLINKED_MARKER)


def test_public_estimate_reuses_engine_and_strips_internal_snapshot(client, published):
    private = TestClient(
        create_app(Settings(db_path=published["db"], admin_token="admin-token"))
    )
    payload = _estimate(
        candidates=[
            _candidate("known", agent_id="agent-a"),
            _candidate("missing", model="not-published"),
        ]
    )
    public = client.post("/v1/estimates", json=payload)
    hidden = private.post("/v1/estimates", json=payload)
    assert public.status_code == 200
    assert hidden.status_code == 200
    body = public.json()
    assert body["kind"] == "agent-costbook.estimate"
    assert body["schema_version"] == 1
    assert body["cost_basis"] == "public-reference"
    assert "records" not in body
    assert body["content_sha256"] == client.get("/v1/catalog").json()["content_sha256"]
    assert body["content_sha256"] != hashlib.sha256(public.content).hexdigest()
    assert body["freshness"] is None
    known, missing = body["results"]
    assert known["status"] == "ok"
    assert Decimal(known["metrics"]["cost"]) == Decimal("0.0077")
    assert Decimal(known["metrics"]["cost"]) == Decimal(
        hidden.json()["results"][0]["metrics"]["cost"]
    )
    assert known["cost_basis"] == "public-reference"
    assert missing["cost_basis"] == "public-reference"
    assert known["formula_version"] == "m4-v1"
    assert known["capabilities"]["status"] == "ok"
    assert known["capabilities"]["agent"]["agent_id"] == "agent-a"
    assert known["capabilities"]["agent"]["as_of"] == AS_OF
    assert known["capabilities"]["model_effort"]["effort"] is None
    assert missing["status"] == "missing_data"
    assert missing["missing_fields"] == ["record"]
    assert missing["metrics"] is None
    assert {item["snapshot_id"] for item in body["results"]} == {body["snapshot_id"]}
    assert {item["data_version"] for item in body["results"]} == {body["data_version"]}
    assert "record_snapshot_id" not in public.text
    assert published["record_snapshot_id"] not in public.text
    assert "record_snapshot_id" in hidden.text
    assert hidden.json()["results"][0]["capabilities"]["status"] == "not_authorized"
    assert "public-reference" not in hidden.text
    assert public.headers["cache-control"] == "no-store"


def test_omitted_usage_and_extra_cost_stay_missing(client):
    no_usage = _estimate()
    no_usage.pop("usage")
    usage = client.post("/v1/estimates", json=no_usage)
    assert usage.status_code == 200
    assert usage.json()["results"][0]["status"] == "missing_data"
    assert usage.json()["results"][0]["missing_fields"] == ["usage"]
    assert usage.json()["results"][0]["metrics"] is None
    no_extra = _estimate()
    no_extra.pop("extra_cost")
    extra = client.post("/v1/estimates", json=no_extra)
    assert extra.status_code == 200
    assert extra.json()["results"][0]["missing_fields"] == ["extra_cost"]
    assert extra.json()["results"][0]["metrics"] is None
    explicit_null = client.post("/v1/estimates", json=_estimate(extra_cost=None))
    assert explicit_null.status_code == 200
    assert explicit_null.json()["results"][0]["missing_fields"] == ["extra_cost"]


@pytest.mark.parametrize(
    "extra,secret,loc_tail",
    [
        ({"private_rates": {"uncached_input_per_million": SECRET}}, SECRET, "private_rates"),
        (
            {"private_subscription": {"monthly_price": SECRET}},
            SECRET,
            "private_subscription",
        ),
        ({"marginal_cash": SECRET}, SECRET, "marginal_cash"),
        ({"account_id": SECRET}, SECRET, "account_id"),
    ],
)
def test_private_estimate_fields_are_rejected(client, extra, secret, loc_tail):
    candidate = _candidate()
    candidate.update(extra)
    response = client.post("/v1/estimates", json=_estimate(candidates=[candidate]))
    error = _assert_error(response, 422, "invalid_public_request", secret)
    assert error["details"][0]["loc"][-1] == loc_tail


@pytest.mark.parametrize(
    "usage",
    [
        {"uncached_input": "10", "task_text": SECRET},
        {"reasoning": SECRET},
        {"uncached_input": "1e2"},
    ],
)
def test_usage_only_accepts_known_decimal_buckets(client, usage):
    response = client.post("/v1/estimates", json=_estimate(usage=usage))
    _assert_error(response, 422, "invalid_public_request", SECRET)


def test_numeric_usage_does_not_echo_the_value(client):
    response = client.post(
        "/v1/estimates",
        json=_estimate(usage={"uncached_input": 987654321}),
    )
    _assert_error(response, 422, "invalid_public_request", "987654321")


@pytest.mark.parametrize("extra_cost", ["0.50", "1", "-1"])
def test_nonzero_extra_cost_is_rejected(client, extra_cost):
    response = client.post("/v1/estimates", json=_estimate(extra_cost=extra_cost))
    _assert_error(response, 422, "invalid_public_request", extra_cost)


def test_currency_is_required_on_public_and_optional_on_private(client, published):
    payload = _estimate()
    payload.pop("currency")
    _assert_error(client.post("/v1/estimates", json=payload), 422, "invalid_public_request")
    private = TestClient(
        create_app(Settings(db_path=published["db"], admin_token="admin-token"))
    )
    allowed = private.post("/v1/estimates", json=payload)
    assert allowed.status_code == 200
    assert allowed.json()["results"][0]["record_snapshot_id"] == published["record_snapshot_id"]
    rejected = private.post("/v1/estimates", json=_estimate(method="M7"))
    assert rejected.status_code == 401
    allowed_m7 = private.post(
        "/v1/estimates",
        headers={"Authorization": "Bearer admin-token"},
        json=_estimate(method="M7"),
    )
    assert allowed_m7.status_code == 200


def test_m7_and_writes_are_forbidden(client):
    secret = "measured-task-secret-88e1"
    measured = client.post(
        "/v1/estimates",
        json=_estimate(method="M7", candidates=[_candidate(secret)]),
    )
    _assert_error(measured, 403, "private_measurement_unavailable", secret)


@pytest.mark.parametrize("method", ["M8", "M9", "nope", "m4"])
def test_unknown_method_is_rejected_before_the_engine(client, method):
    response = client.post("/v1/estimates", json=_estimate(method=method))
    error = _assert_error(response, 422, "invalid_public_request", method)
    assert error["details"][0]["loc"] == ["body", "method"]
    assert "unsupported_method" not in response.text


def test_public_methods_reach_the_engine(client):
    response = client.post("/v1/estimates", json=_estimate(method="M0"))
    assert response.status_code == 200
    assert response.json()["results"][0]["method"] == "M0"
    assert response.json()["results"][0]["status"] == "ok"


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/v1/contributions"),
        ("post", "/v1/observations"),
        ("post", "/v1/contributions/co_secret/publish"),
        ("put", "/v1/capabilities/agents"),
        ("put", "/v1/capabilities/model-efforts"),
        ("patch", "/v1/info"),
        ("delete", "/v1/catalog"),
    ],
)
def test_public_writes_are_read_only(client, method, path):
    response = client.request(method, path, json={"account_key": SECRET, "tasks": [SECRET]})
    _assert_error(response, 403, "public_read_only", SECRET)


def test_unknown_get_does_not_echo_the_resource_id(client):
    secret = "ob_private_measurement_77aa"
    response = client.get(f"/v1/observations/{secret}")
    _assert_error(response, 404, "not_found", secret)
    assumptions = client.get("/v1/usage-assumptions")
    _assert_error(assumptions, 404, "not_found")


def test_openapi_lists_only_the_public_request_shape(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert response.headers["etag"] == f'"{hashlib.sha256(response.content).hexdigest()}"'
    document = response.json()
    paths = set(document["paths"])
    assert paths == {
        "/health",
        "/ready",
        "/openapi.json",
        "/v1/info",
        "/v1/catalog",
        "/v1/capabilities",
        "/v1/capabilities/agents",
        "/v1/capabilities/model-efforts",
        "/v1/evidence/{evidence_id}",
        "/v1/research/{research_id}",
        "/v1/estimates",
    }
    text = response.text
    for forbidden in (
        "private_rates",
        "private_subscription",
        "marginal_cash",
        "admin_token",
        "read_token",
        "task_category",
        "/v1/contributions",
        "/v1/observations",
    ):
        assert forbidden not in text
    assert "securitySchemes" not in document.get("components", {})
    assert "security" not in document
    assert document["info"]["version"] == __version__
    assert "HTTPValidationError" not in text
    assert "ValidationError" not in text
    assert '"input"' not in text
    assert '"msg"' not in text
    assert '"ctx"' not in text
    assert "M7" not in text
    estimate = document["paths"]["/v1/estimates"]["post"]["requestBody"]
    schema = estimate["content"]["application/json"]["schema"]
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = document["components"]["schemas"][name]
    assert "currency" in schema["required"]
    assert schema["properties"]["method"]["enum"] == list(PUBLIC_METHODS)
    invalid = document["paths"]["/v1/estimates"]["post"]["responses"]["422"]
    assert invalid["content"]["application/json"]["schema"]["$ref"].endswith("PublicErrorResponse")
    error_body = document["components"]["schemas"]["PublicErrorBody"]
    assert set(error_body["properties"]) == {"code", "message", "details"}
    assert set(error_body["properties"]["details"]["items"]["properties"]) == {"type", "loc"}


def _resolved_schema(document, schema, seen=0):
    while isinstance(schema, dict) and "$ref" in schema:
        if seen > 8:
            break
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = document["components"]["schemas"][name]
        seen += 1
    return schema


def _schema_matches(document, schema, payload) -> bool:
    schema = _resolved_schema(document, schema)
    if "anyOf" in schema:
        return any(_schema_matches(document, branch, payload) for branch in schema["anyOf"])
    if "oneOf" in schema:
        return any(_schema_matches(document, branch, payload) for branch in schema["oneOf"])
    if schema.get("type") == "null":
        return payload is None
    if payload is None:
        return False
    if "enum" in schema and payload not in schema["enum"]:
        return False
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(payload, dict):
            return False
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        if not required <= set(payload):
            return False
        extra = schema.get("additionalProperties", True)
        if extra is False and not set(payload) <= set(properties):
            return False
        for key, value in payload.items():
            if key in properties:
                if not _schema_matches(document, properties[key], value):
                    return False
            elif isinstance(extra, dict) and not _schema_matches(document, extra, value):
                return False
        return True
    if kind == "array":
        if not isinstance(payload, list):
            return False
        item = schema.get("items")
        return item is None or all(_schema_matches(document, item, value) for value in payload)
    if kind == "string":
        return isinstance(payload, str)
    if kind == "integer":
        return type(payload) is int
    if kind == "boolean":
        return type(payload) is bool
    if kind == "number":
        return type(payload) in {int, float} and type(payload) is not bool
    return False


def _property_names(schema) -> set[str]:
    if not isinstance(schema, dict):
        return set()
    names = set(schema.get("properties") or {})
    for key in ("anyOf", "oneOf"):
        for branch in schema.get(key) or []:
            names |= _property_names(branch)
    return names


def _success_schema(document, path, method):
    response = document["paths"][path][method]["responses"]["200"]
    return response["content"]["application/json"]["schema"]


def test_openapi_documents_absent_catalog_and_safe_internal_errors(client, public_app, monkeypatch):
    document = client.get("/openapi.json").json()
    monkeypatch.setattr(public_app.state.store, "revision_of", lambda _snapshot: None)
    info = client.get("/v1/info")
    assert info.status_code == 200
    assert info.json()["catalog"] == {"snapshot_id": None, "data_version": None, "content_sha256": None}
    catalog_schema = document["components"]["schemas"]["PublicServiceInfo"]["properties"]["catalog"]
    for field in info.json()["catalog"]:
        assert {"type": "null"} in catalog_schema["properties"][field].get("anyOf", [])
    for operation in document["paths"].values():
        for spec in operation.values():
            assert spec["responses"]["500"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/PublicErrorResponse"}


def test_openapi_success_schemas_match_live_bodies(client, published):
    document = client.get("/openapi.json").json()
    info = client.get("/v1/info").json()
    catalog = client.get("/v1/catalog").json()
    capabilities = client.get("/v1/capabilities").json()
    agent = client.get("/v1/capabilities/agents", params={"agent_id": "agent-a"}).json()
    effort = client.get(
        "/v1/capabilities/model-efforts",
        params={"provider": "example", "model": MODEL},
    ).json()
    evidence = client.get(f"/v1/evidence/{published['linked_evidence_id']}").json()
    research = client.get(f"/v1/research/{published['research_id']}").json()
    health = client.get("/health").json()
    ready = client.get("/ready").json()
    estimated = client.post(
        "/v1/estimates",
        json=_estimate(
            candidates=[
                _candidate("known", agent_id="agent-a"),
                _candidate("missing", model="not-published"),
            ]
        ),
    ).json()
    priced = client.post("/v1/estimates", json=_estimate(method="M0")).json()
    missing_usage = client.post(
        "/v1/estimates",
        json=_estimate(usage=None),
    ).json()
    samples = {
        ("/health", "get"): [health],
        ("/ready", "get"): [ready],
        ("/v1/info", "get"): [info],
        ("/v1/catalog", "get"): [catalog],
        ("/v1/capabilities", "get"): [capabilities],
        ("/v1/capabilities/agents", "get"): [agent],
        ("/v1/capabilities/model-efforts", "get"): [effort],
        ("/v1/evidence/{evidence_id}", "get"): [evidence],
        ("/v1/research/{research_id}", "get"): [research],
        ("/v1/estimates", "post"): [estimated, priced, missing_usage],
        ("/openapi.json", "get"): [document],
    }
    for (path, method), payloads in samples.items():
        schema = _success_schema(document, path, method)
        resolved = _resolved_schema(document, schema)
        assert resolved.get("type") == "object" and resolved.get("properties"), path
        for payload in payloads:
            assert _schema_matches(document, schema, payload), path
    info_schema = _resolved_schema(document, _success_schema(document, "/v1/info", "get"))
    assert set(info_schema["required"]) == INFO_KEYS
    catalog_schema = _resolved_schema(
        document, _success_schema(document, "/v1/catalog", "get")
    )
    assert {
        "kind",
        "schema_version",
        "publisher_id",
        "data_version",
        "snapshot_id",
        "published_at",
        "formula_version",
        "freshness",
        "content_sha256",
        "records",
    } <= set(catalog_schema["required"])
    estimate_schema = _resolved_schema(
        document, _success_schema(document, "/v1/estimates", "post")
    )
    assert {"kind", "schema_version", "cost_basis", "results"} <= set(estimate_schema["required"])
    assert "records" not in estimate_schema["properties"]
    assert "record_snapshot_id" not in estimate_schema["properties"]
    result_schema = _resolved_schema(document, estimate_schema["properties"]["results"]["items"])
    assert {
        "status",
        "metrics",
        "units",
        "missing_fields",
        "capabilities",
        "cost_basis",
    } <= set(result_schema["required"])
    assert "record_snapshot_id" not in result_schema["properties"]
    assert _schema_matches(document, result_schema["properties"]["metrics"], None)
    assert "cost" not in _property_names(result_schema["properties"]["metrics"])
    agent_schema = _resolved_schema(
        document, _success_schema(document, "/v1/capabilities/agents", "get")
    )
    assert {"row_version", "recorded_at", "agent_id", "as_of", "source"} <= set(
        agent_schema["required"]
    )
    assert "expected_version" not in agent_schema["properties"]
    text = json.dumps(document)
    for forbidden in (
        "expected_version",
        "record_snapshot_id",
        "private_rates",
        "private_subscription",
        "marginal_cash",
        "observation_id",
        "securitySchemes",
    ):
        assert forbidden not in text
    cached = (
        "/health",
        "/ready",
        "/v1/info",
        "/v1/catalog",
        "/v1/capabilities",
        "/v1/capabilities/agents",
        "/v1/capabilities/model-efforts",
        "/v1/evidence/{evidence_id}",
        "/v1/research/{research_id}",
        "/openapi.json",
    )
    for path in cached:
        responses = document["paths"][path]["get"]["responses"]
        assert "ETag" in responses["200"]["headers"]
        assert "Cache-Control" in responses["200"]["headers"]
        assert "content" not in responses["304"]
        assert "ETag" in responses["304"]["headers"]
        assert "Cache-Control" in responses["304"]["headers"]
    estimates = document["paths"]["/v1/estimates"]["post"]["responses"]
    assert "304" not in estimates
    assert "Cache-Control" in estimates["200"]["headers"]
    assert "ETag" not in estimates["200"].get("headers", {})
    for code in ("403", "404", "413", "422", "429", "503"):
        ref = estimates[code]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("PublicErrorResponse")
    assert "Retry-After" in estimates["429"]["headers"]
    assert "404" in document["paths"]["/v1/catalog"]["get"]["responses"]
    assert "503" in document["paths"]["/ready"]["get"]["responses"]
    assert "429" not in document["paths"]["/health"]["get"]["responses"]


def test_body_limit_covers_content_length_and_chunked_streams(public_app):
    client = TestClient(public_app)
    exact = client.post(
        "/v1/estimates",
        content=b"x" * 65536,
        headers={"content-type": "application/json"},
    )
    assert exact.status_code == 422
    oversized = client.post(
        "/v1/estimates",
        content=b"x" * 65537,
        headers={"content-type": "application/json"},
    )
    _assert_error(oversized, 413, "request_too_large")

    status, payload = asyncio.run(
        _post_chunks(
            public_app,
            [(b"{" * 30000, True), (b"x" * 40000, False)],
        )
    )
    assert status == 413
    assert json.loads(payload)["error"]["code"] == "request_too_large"


async def _post_chunks(app, chunks: list[tuple[bytes, bool]]) -> tuple[int, bytes]:
    """POST without Content-Length so the body arrives as separate ASGI chunks."""
    sent: list[dict] = []
    remaining = list(chunks)

    async def receive():
        if remaining:
            body, more = remaining.pop(0)
            return {"type": "http.request", "body": body, "more_body": more}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/estimates",
            "raw_path": b"/v1/estimates",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 50000),
            "server": ("public", 80),
        },
        receive,
        send,
    )
    status = next(item["status"] for item in sent if item["type"] == "http.response.start")
    body = b"".join(
        item.get("body", b"") for item in sent if item["type"] == "http.response.body"
    )
    return status, body


def test_unexpected_backend_failure_is_a_generic_500(public_app, monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("secret-backend-trace /tmp/private.sqlite3")

    monkeypatch.setattr("agent_costbook.public_api.build_document", explode)
    client = TestClient(public_app, raise_server_exceptions=False)
    response = client.get("/v1/catalog")
    _assert_error(response, 500, "internal_error", "secret-backend-trace")
    assert "private.sqlite3" not in response.text
    assert "Traceback" not in response.text
    assert "RuntimeError" not in response.text


def test_candidate_limit_is_422(client):
    accepted = client.post(
        "/v1/estimates",
        json=_estimate(candidates=[_candidate(f"c{index}") for index in range(64)]),
    )
    assert accepted.status_code == 200
    assert len(accepted.json()["results"]) == 64
    rejected = client.post(
        "/v1/estimates",
        json=_estimate(candidates=[_candidate(f"c{index}") for index in range(65)]),
    )
    _assert_error(rejected, 422, "invalid_public_request")


def test_instance_rate_limit_is_not_an_ip_quota(client, monkeypatch):
    clock = {"now": 1_000.0}
    monkeypatch.setattr("agent_costbook.public_api.time.monotonic", lambda: clock["now"])
    assert all(client.get("/health").status_code == 200 for _ in range(61))
    assert all(client.get("/ready").status_code == 200 for _ in range(61))
    for index in range(60):
        response = client.get(
            "/v1/info",
            headers={"X-Forwarded-For": f"203.0.113.{index}"},
        )
        assert response.status_code == 200
    blocked = client.get("/v1/info", headers={"X-Forwarded-For": "198.51.100.8"})
    error = _assert_error(blocked, 429, "rate_limited")
    assert error["message"]
    assert blocked.headers["retry-after"] == "60"
    clock["now"] = 1_059.2
    retry = client.get("/v1/info")
    assert retry.status_code == 429
    assert retry.headers["retry-after"] == "1"
    clock["now"] = 1_060.0
    assert client.get("/v1/info").status_code == 200


def test_rate_limit_is_per_app_instance(published, monkeypatch):
    clock = {"now": 500.0}
    monkeypatch.setattr("agent_costbook.public_api.time.monotonic", lambda: clock["now"])
    first = TestClient(create_public_app(published["db"], published["manifest"]))
    second = TestClient(create_public_app(published["db"], published["manifest"]))
    for _ in range(60):
        assert first.get("/v1/info").status_code == 200
    assert first.get("/v1/info").status_code == 429
    assert second.get("/v1/info").status_code == 200
