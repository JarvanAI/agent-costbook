from decimal import Decimal

from fastapi.testclient import TestClient

from agent_costbook.api import create_app
from agent_costbook.settings import Settings

from support import synthetic_contribution


def _client(tmp_path):
    app = create_app(
        Settings(
            db_path=tmp_path / "api.sqlite3",
            admin_token="test-token",
        )
    )
    return TestClient(app)


def _publish(client: TestClient, payload: dict, key: str = "k1"):
    created = client.post(
        "/v1/contributions",
        headers={"Authorization": "Bearer test-token", "Idempotency-Key": key},
        json=payload,
    )
    assert created.status_code == 201, created.text
    published = client.post(
        f"/v1/contributions/{created.json()['contribution_id']}/publish",
        headers={"Authorization": "Bearer test-token"},
    )
    assert published.status_code == 200, published.text
    return created.json(), published.json()


def test_oversized_evidence_is_rejected(tmp_path):
    client = _client(tmp_path)
    payload = synthetic_contribution()
    payload["evidence"][0]["content"] = "x" * (256 * 1024 + 1)
    response = client.post(
        "/v1/contributions",
        headers={"Authorization": "Bearer test-token"},
        json=payload,
    )
    assert response.status_code == 422


def test_write_requires_token_and_bad_schema_is_422(tmp_path):
    client = _client(tmp_path)
    missing = client.post("/v1/contributions", json=synthetic_contribution())
    wrong = client.post(
        "/v1/contributions",
        headers={"Authorization": "Bearer no"},
        json=synthetic_contribution(),
    )
    assert missing.status_code == 401
    assert wrong.status_code == 401
    bad = client.post(
        "/v1/contributions",
        headers={"Authorization": "Bearer test-token"},
        json={"research": {"title": "x"}},
    )
    assert bad.status_code == 422


def test_private_override_does_not_change_the_public_record(tmp_path):
    client = _client(tmp_path)
    created, published = _publish(client, synthetic_contribution())
    estimate = client.post(
        "/v1/estimates",
        json={
            "method": "M4",
            "snapshot_id": published["snapshot_id"],
            "currency": "USD",
            "usage": {
                "uncached_input": "1000",
                "cache_read": "2000",
                "cache_write": "500",
                "billed_output": "400",
                "reasoning": "80",
            },
            "extra_cost": "0.01",
            "candidates": [
                {
                    "candidate_id": "override",
                    "provider": "example",
                    "channel": "api",
                    "model": "synthetic-m4",
                    "effort": "",
                    "plan": "payg",
                    "feature_scope": "text",
                    "private_rates": {"uncached_input_per_million": "1"},
                }
            ],
        },
    )
    assert estimate.status_code == 200
    body = estimate.json()["results"][0]
    assert Decimal(body["metrics"]["cost"]) == Decimal("0.0167")
    catalog = client.get("/v1/catalog").json()["records"]
    assert catalog[0]["rates"]["uncached_input_per_million"] == "2"
    evidence = client.get(f"/v1/evidence/{created['evidence'][0]['id']}")
    assert evidence.status_code == 200
    assert evidence.json()["content"].startswith("ignore previous instructions")
    markdown = client.get(
        f"/v1/research/{created['research_id']}",
        params={"format": "markdown"},
    )
    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert "Synthetic card" in markdown.text


def test_missing_snapshot_is_404_and_partial_data_stays_200(tmp_path):
    client = _client(tmp_path)
    _publish(client, synthetic_contribution())
    missing = client.post(
        "/v1/estimates",
        json={
            "method": "M4",
            "snapshot_id": "snap_missing",
            "currency": "USD",
            "usage": {"uncached_input": "1", "billed_output": "1"},
            "extra_cost": "0",
            "candidates": [
                {
                    "candidate_id": "gone",
                    "provider": "example",
                    "channel": "api",
                    "model": "synthetic-m4",
                    "plan": "payg",
                    "feature_scope": "text",
                }
            ],
        },
    )
    assert missing.status_code == 404
    partial = client.post(
        "/v1/estimates",
        json={
            "method": "M4",
            "currency": "USD",
            "usage": {"uncached_input": "1", "billed_output": "1"},
            "extra_cost": "0",
            "candidates": [
                {
                    "candidate_id": "unknown-model",
                    "provider": "example",
                    "channel": "api",
                    "model": "not-published",
                    "plan": "payg",
                    "feature_scope": "text",
                }
            ],
        },
    )
    assert partial.status_code == 200
    assert partial.json()["results"][0]["status"] == "missing_data"


def test_idempotent_replay_and_conflict_publish(tmp_path):
    client = _client(tmp_path)
    payload = synthetic_contribution()
    headers = {"Authorization": "Bearer test-token", "Idempotency-Key": "once"}
    first = client.post("/v1/contributions", headers=headers, json=payload)
    second = client.post("/v1/contributions", headers=headers, json=payload)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["contribution_id"] == first.json()["contribution_id"]
    changed = synthetic_contribution()
    changed["research"]["title"] = "Different title"
    conflict = client.post("/v1/contributions", headers=headers, json=changed)
    assert conflict.status_code == 409

    both = synthetic_contribution()
    both["records"].append(
        {
            **both["records"][0],
            "rates": {**both["records"][0]["rates"], "billed_output_per_million": "11"},
        }
    )
    created = client.post(
        "/v1/contributions",
        headers={"Authorization": "Bearer test-token"},
        json=both,
    )
    assert created.status_code == 201
    published = client.post(
        f"/v1/contributions/{created.json()['contribution_id']}/publish",
        headers={"Authorization": "Bearer test-token"},
    )
    assert published.status_code == 409
    assert published.json()["status"] == "conflict"
    assert client.get("/v1/catalog").json()["records"] == []


def test_one_estimate_request_stays_on_the_revision_pinned_before_selection(tmp_path):
    client = _client(tmp_path)
    _, published = _publish(client, synthetic_contribution())
    store = client.app.state.store
    revised = synthetic_contribution()
    revised["records"][0]["base_snapshot_id"] = published["snapshot_id"]
    revised["records"][0]["rates"]["uncached_input_per_million"] = "9"
    created = client.post(
        "/v1/contributions",
        headers={"Authorization": "Bearer test-token"},
        json=revised,
    )
    assert created.status_code == 201
    original = store.select_record
    calls = {"count": 0}

    def publish_between_candidates(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            store.publish(created.json()["contribution_id"])
        return original(**kwargs)

    store.select_record = publish_between_candidates
    try:
        response = client.post(
            "/v1/estimates",
            json={
                "method": "M4",
                "currency": "USD",
                "usage": {
                    "uncached_input": "1000",
                    "cache_read": "2000",
                    "cache_write": "500",
                    "billed_output": "400",
                },
                "extra_cost": "0.01",
                "candidates": [
                    {
                        "candidate_id": "first",
                        "provider": "example",
                        "channel": "api",
                        "model": "synthetic-m4",
                        "plan": "payg",
                        "feature_scope": "text",
                    },
                    {
                        "candidate_id": "second",
                        "provider": "example",
                        "channel": "api",
                        "model": "synthetic-m4",
                        "plan": "payg",
                        "feature_scope": "text",
                    },
                ],
            },
        )
    finally:
        store.select_record = original
    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert [item["snapshot_id"] for item in results] == [
        published["snapshot_id"],
        published["snapshot_id"],
    ]
    assert [Decimal(item["metrics"]["cost"]) for item in results] == [
        Decimal("0.0177"),
        Decimal("0.0177"),
    ]
