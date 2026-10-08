import asyncio
import hashlib
import json
import sys
import types
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_costbook.api import create_app
from agent_costbook.export import build_document
from agent_costbook.public_api import PublicArtifactError, create_public_app
from agent_costbook.settings import Settings
from agent_costbook.store import Store
from support import synthetic_contribution

AS_OF = "2026-09-27T00:00:00+00:00"
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
        "model": "synthetic-m4",
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


def _agent(agent_id: str, **overrides) -> dict:
    body = {
        "agent_id": agent_id,
        "expected_version": 0,
        "source": "official",
        "source_ref": f"https://example.test/{agent_id}",
        "as_of": AS_OF,
        "domain": None,
        "strengths": f"strength-{agent_id}",
        "unsuitable": None,
        "can_edit_files": True,
        "can_use_tools": False,
        "input_output_shape": None,
    }
    body.update(overrides)
    return body


def _effort(effort: str | None, **overrides) -> dict:
    body = {
        "provider": "example",
        "model": "synthetic-m4",
        "effort": effort,
        "expected_version": 0,
        "source": "official",
        "source_ref": "https://example.test/effort",
        "as_of": AS_OF,
        "suitable": "null tier" if not effort else "named tier",
        "unsuitable": None,
        "context_length": None,
        "benchmarks": None,
        "default_for_agents": None,
    }
    body.update(overrides)
    return body


def _contribution(model: str = "synthetic-m4", *, unlinked: bool = False) -> dict:
    payload = synthetic_contribution()
    payload["records"][0]["model"] = model
    payload["research"]["title"] = f"Synthetic card {model}"
    payload["evidence"][0]["source_url"] = f"fixture://{model}"
    if unlinked:
        payload["evidence"].append(
            {
                "source_kind": "synthetic_fixture",
                "source_url": "fixture://unlinked",
                "collector_kind": "test",
                "collector_name": "pytest",
                "content": "unlinked-evidence-secret-7f3c",
                "retrieved_at": "2026-09-25T00:00:00+00:00",
            }
        )
    return payload


def _manifest(path: Path) -> None:
    path.write_text(
        json.dumps({"publisher_id": "pub_test", "role": "explicit-fixture"}),
        encoding="utf-8",
    )


def _prepare(directory: Path, *, versions: int = 1, capabilities: bool = True) -> dict:
    database = directory / "public.sqlite3"
    manifest = directory / "manifest.json"
    store = Store(database)
    created = []
    published = []
    for index, model in enumerate(("synthetic-m4", "synthetic-next")[:versions]):
        body, _ = store.create_contribution(
            _contribution(model, unlinked=index == 0),
            None,
        )
        created.append(body)
        published.append(store.publish(body["contribution_id"]))
    if capabilities:
        store.save_agent_capability(_agent("agent-b"))
        store.save_agent_capability(_agent("agent-a"))
        store.save_model_effort(
            _effort(
                "low",
                provider="zeta",
                model="other",
                suitable="other tier",
            )
        )
        store.save_model_effort(
            _effort(
                None,
                benchmarks=[
                    {"name": "zeta", "score": "2", "unit": "point", "source": "official"},
                    {"name": "alpha", "score": "1", "unit": "point", "source": "official"},
                ],
            )
        )
        store.save_model_effort(_effort("low"))
    publisher = store.publisher_id()
    store.close()
    _manifest(manifest)
    return {
        "db": database,
        "manifest": manifest,
        "created": created,
        "published": published,
        "publisher_id": publisher,
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
    with pytest.raises(PublicArtifactError):
        create_public_app()


def test_explicit_fixture_opens_when_validator_module_is_absent(published, monkeypatch):
    monkeypatch.setitem(sys.modules, "agent_costbook.public_data", None)
    before = hashlib.sha256(published["db"].read_bytes()).hexdigest()
    app = create_public_app(published["db"], published["manifest"])
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/v1/info").json()["publisher_id"] == published["publisher_id"]
    assert hashlib.sha256(published["db"].read_bytes()).hexdigest() == before
    assert not Path(str(published["db"]) + "-wal").exists()
    assert not Path(str(published["db"]) + "-shm").exists()


def test_validator_rejection_stops_startup(published, monkeypatch):
    module = types.ModuleType("agent_costbook.public_data")

    def validate_public_artifact(db_path, manifest_path):
        raise RuntimeError("mixed publisher")

    module.validate_public_artifact = validate_public_artifact
    monkeypatch.setitem(sys.modules, "agent_costbook.public_data", module)
    with pytest.raises(RuntimeError, match="mixed publisher"):
        create_public_app(published["db"], published["manifest"])


def test_paths_must_be_supplied_together(published):
    with pytest.raises(PublicArtifactError):
        create_public_app(published["db"])


def test_health_is_alive_when_catalog_is_missing(tmp_path):
    database = tmp_path / "empty.sqlite3"
    Store(database).close()
    manifest = tmp_path / "manifest.json"
    _manifest(manifest)
    client = TestClient(create_public_app(database, manifest))
    health = client.get("/health")
    ready = client.get("/ready")
    info = client.get("/v1/info")
    catalog = client.get("/v1/catalog")
    capabilities = client.get("/v1/capabilities")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    _assert_error(ready, 503, "not_ready")
    _assert_error(catalog, 503, "not_ready")
    _assert_error(
        client.post("/v1/estimates", json=_estimate()),
        503,
        "not_ready",
    )
    assert info.status_code == 200
    payload = info.json()
    assert set(payload) == INFO_KEYS
    assert payload["catalog"] == {
        "snapshot_id": None,
        "data_version": None,
        "content_sha256": None,
    }
    assert payload["capabilities"]["agent_count"] == 0
    assert payload["capabilities"]["model_effort_count"] == 0
    assert payload["usage_assumptions"] == {"supported": False}
    assert capabilities.status_code == 200
    listed = capabilities.json()
    assert set(listed) == {
        "kind",
        "schema_version",
        "publisher_id",
        "content_sha256",
        "agents",
        "model_efforts",
    }
    assert listed["agents"] == []
    assert listed["model_efforts"] == []
    assert listed["content_sha256"] == _sha256({"agents": [], "model_efforts": []})
    assert client.get("/v1/catalog", params={"snapshot_id": "snap-1"}).status_code == 404


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
    ["internal", "snap-0", "snap-2", "snap-01", "latest"],
)
def test_unknown_or_internal_snapshot_is_not_found(client, published, snapshot_id):
    if snapshot_id == "internal":
        snapshot_id = published["published"][0]["snapshot_id"]
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
    assert {row["model"] for row in old.json()["records"]} == {"synthetic-m4"}
    assert {row["model"] for row in current.json()["records"]} == {
        "synthetic-m4",
        "synthetic-next",
    }
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
        ("example", "synthetic-m4", None),
        ("example", "synthetic-m4", "low"),
        ("zeta", "other", "low"),
    ]
    null_row = body["model_efforts"][0]
    assert null_row["row_version"] == 1
    assert null_row["as_of"] == AS_OF
    assert null_row["source"] == "official"
    assert [item["name"] for item in null_row["benchmarks"]] == ["zeta", "alpha"]
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
    assert info.json()["service_version"] == "1.2.0"
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
        params={"provider": "example", "model": "synthetic-m4"},
    )
    blank = client.get(
        "/v1/capabilities/model-efforts",
        params={"provider": "example", "model": "synthetic-m4", "effort": ""},
    )
    named = client.get(
        "/v1/capabilities/model-efforts",
        params={"provider": "example", "model": "synthetic-m4", "effort": "low"},
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


def test_single_capability_etag_ignores_other_rows(client, published):
    first = client.get("/v1/capabilities/agents", params={"agent_id": "agent-a"})
    collection = client.get("/v1/capabilities")
    writer = Store(published["db"])
    writer.save_agent_capability(_agent("agent-c"))
    writer.close()
    second = client.get("/v1/capabilities/agents", params={"agent_id": "agent-a"})
    again = client.get("/v1/capabilities")
    assert second.content == first.content
    assert second.headers["etag"] == first.headers["etag"]
    assert again.headers["etag"] != collection.headers["etag"]
    assert [row["agent_id"] for row in again.json()["agents"]] == [
        "agent-a",
        "agent-b",
        "agent-c",
    ]


def test_sources_require_published_record_linkage(client, published):
    created = published["created"][0]
    linked = created["evidence"][0]["id"]
    unlinked = created["evidence"][1]["id"]
    research = created["research_id"]
    assert client.get(f"/v1/evidence/{linked}").status_code == 200
    assert client.get(f"/v1/research/{research}").status_code == 200
    hidden_evidence = client.get(f"/v1/evidence/{unlinked}")
    _assert_error(hidden_evidence, 404, "evidence_not_found", "unlinked-evidence-secret-7f3c")
    writer = Store(published["db"])
    draft, _ = writer.create_contribution(_contribution("draft-only"), None)
    writer.close()
    draft_evidence = client.get(f"/v1/evidence/{draft['evidence'][0]['id']}")
    draft_research = client.get(f"/v1/research/{draft['research_id']}")
    _assert_error(draft_evidence, 404, "evidence_not_found", draft["evidence"][0]["id"])
    _assert_error(draft_research, 404, "research_not_found", draft["research_id"])
    assert "draft-only" not in draft_research.text


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
    assert published["published"][0]["snapshot_id"] not in public.text
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
    assert allowed.json()["results"][0]["record_snapshot_id"] == published["published"][0][
        "snapshot_id"
    ]
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
    estimate = document["paths"]["/v1/estimates"]["post"]["requestBody"]
    schema = estimate["content"]["application/json"]["schema"]
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = document["components"]["schemas"][name]
    assert "currency" in schema["required"]


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
