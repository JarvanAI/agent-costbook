from pathlib import Path

from fastapi.testclient import TestClient

from agent_costbook.api import create_app
from agent_costbook.settings import Settings, load_settings
from support import synthetic_contribution

AS_OF = "2026-09-27T00:00:00+00:00"
SECRET = "read-scope-secret-7c2a"
ADMIN = "admin-token"
READ = "read-token"
METHODS = ("M0", "M1", "M2", "M3", "M4", "M5", "M6")


def _settings(tmp_path: Path, *, admin: str = ADMIN, read: str = READ) -> Settings:
    return Settings(db_path=tmp_path / "read-auth.sqlite3", admin_token=admin, read_token=read)


def _client(tmp_path: Path, *, admin: str = ADMIN, read: str = READ) -> TestClient:
    return TestClient(create_app(_settings(tmp_path, admin=admin, read=read)))


def _agent(**overrides) -> dict:
    body = {
        "agent_id": "agent-1",
        "expected_version": 0,
        "source": "user_observation",
        "source_ref": None,
        "as_of": AS_OF,
        "domain": None,
        "strengths": SECRET,
        "unsuitable": None,
        "can_edit_files": None,
        "can_use_tools": None,
        "input_output_shape": None,
    }
    body.update(overrides)
    return body


def _effort(effort: str | None, **overrides) -> dict:
    body = {
        "provider": "openai",
        "model": "openai/gpt-4o-mini",
        "effort": effort,
        "expected_version": 0,
        "source": "official",
        "source_ref": None,
        "as_of": AS_OF,
        "suitable": "named tier" if effort else "null tier",
        "unsuitable": None,
        "context_length": None,
        "benchmarks": None,
        "default_for_agents": None,
    }
    body.update(overrides)
    return body


def _estimate(method: str) -> dict:
    return {
        "method": method,
        "currency": "USD",
        "usage": {"uncached_input": "1000", "billed_output": "400"},
        "extra_cost": "0",
        "candidates": [
            {
                "candidate_id": "read-scope",
                "provider": "openai",
                "channel": "api",
                "model": "openai/gpt-4o-mini",
                "effort": "low",
                "plan": "payg",
                "feature_scope": "text",
                "agent_id": "agent-1",
            }
        ],
    }


def _observation() -> dict:
    return {
        "provider": "example",
        "channel": "api",
        "model": "synthetic-m4",
        "plan": "payg",
        "feature_scope": "text",
        "currency": "USD",
        "period_start": "2026-09-01T00:00:00+00:00",
        "period_end": "2026-10-01T00:00:00+00:00",
        "task_category": "coding",
        "acceptance": "tests_passed",
        "subscription_cash": "25",
        "tasks": [
            {
                "task_id": "private-task-9f3a",
                "attempts": [{"cash": "0", "api_equivalent": "100", "succeeded": True}],
            }
        ],
    }


def _m7() -> dict:
    return {
        "method": "M7",
        "currency": "USD",
        "task_category": "coding",
        "acceptance": "tests_passed",
        "candidates": [
            {
                "candidate_id": "measured",
                "provider": "example",
                "channel": "api",
                "model": "synthetic-m4",
                "plan": "payg",
                "feature_scope": "text",
                "window_start": "2026-09-01T00:00:00+00:00",
                "window_end": "2026-10-01T00:00:00+00:00",
            }
        ],
    }


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _assert_unauthorized(response) -> None:
    assert response.status_code == 401
    assert response.json() == {"detail": "admin token required"}
    assert SECRET not in response.text
    assert "private-task-9f3a" not in response.text
    assert READ not in response.text
    assert ADMIN not in response.text


def test_settings_constructor_defaults_read_token_empty(tmp_path):
    keyword = Settings(db_path=tmp_path / "keyword.sqlite3", admin_token="legacy-admin")
    positional = Settings(tmp_path / "positional.sqlite3", "legacy-admin")
    assert keyword.admin_token == "legacy-admin"
    assert positional.admin_token == "legacy-admin"
    assert keyword.read_token == ""
    assert positional.read_token == ""
    assert keyword.db_path == tmp_path / "keyword.sqlite3"


def test_load_settings_reads_read_token_from_env(monkeypatch, tmp_path):
    monkeypatch.delenv("ACB_DB", raising=False)
    monkeypatch.delenv("ACB_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("ACB_READ_TOKEN", raising=False)
    defaults = load_settings()
    assert defaults == Settings(Path("agent_costbook.sqlite3"), "")
    assert defaults.read_token == ""

    database = tmp_path / "env.sqlite3"
    monkeypatch.setenv("ACB_DB", str(database))
    monkeypatch.setenv("ACB_ADMIN_TOKEN", "env-admin")
    monkeypatch.setenv("ACB_READ_TOKEN", "env-read")
    loaded = load_settings()
    assert loaded == Settings(database, "env-admin", "env-read")

    client = TestClient(create_app())
    denied = client.get("/v1/capabilities")
    _assert_unauthorized(denied)
    listed = client.get("/v1/capabilities", headers=_auth("env-read"))
    assert listed.status_code == 200
    assert listed.json() == {"agents": [], "model_efforts": []}


def test_read_token_lists_gets_history_and_enriches_non_m7_estimates(tmp_path):
    client = _client(tmp_path)
    admin = _auth(ADMIN)
    reader = _auth(READ)
    assert client.put("/v1/capabilities/agents", headers=admin, json=_agent()).status_code == 200
    revised = client.put(
        "/v1/capabilities/agents",
        headers=admin,
        json=_agent(expected_version=1, source_ref="ref-kept"),
    )
    assert revised.status_code == 200, revised.text
    assert client.put(
        "/v1/capabilities/model-efforts", headers=admin, json=_effort(None)
    ).status_code == 200
    assert client.put(
        "/v1/capabilities/model-efforts", headers=admin, json=_effort("low")
    ).status_code == 200

    listed = client.get("/v1/capabilities", headers=reader)
    assert listed.status_code == 200
    assert [row["agent_id"] for row in listed.json()["agents"]] == ["agent-1"]
    assert listed.json()["agents"][0]["strengths"] == SECRET
    assert listed.json()["agents"][0]["source"] == "user_observation"
    assert listed.json()["agents"][0]["source_ref"] == "ref-kept"
    assert listed.json()["agents"][0]["as_of"] == AS_OF
    efforts = listed.json()["model_efforts"]
    assert [(row["effort"], row["suitable"], row["source"], row["as_of"]) for row in efforts] == [
        (None, "null tier", "official", AS_OF),
        ("low", "named tier", "official", AS_OF),
    ]

    agent = client.get("/v1/capabilities/agents", headers=reader, params={"agent_id": "agent-1"})
    assert agent.status_code == 200
    assert agent.json()["strengths"] == SECRET
    assert agent.json()["row_version"] == 2
    history = client.get(
        "/v1/capabilities/agents/history",
        headers=reader,
        params={"agent_id": "agent-1"},
    )
    assert history.status_code == 200
    assert [row["source_ref"] for row in history.json()] == [None, "ref-kept"]

    null_tier = client.get(
        "/v1/capabilities/model-efforts",
        headers=reader,
        params={"provider": "openai", "model": "openai/gpt-4o-mini"},
    )
    assert null_tier.status_code == 200
    assert null_tier.json()["effort"] is None
    assert null_tier.json()["suitable"] == "null tier"
    named = client.get(
        "/v1/capabilities/model-efforts",
        headers=reader,
        params={"provider": "openai", "model": "openai/gpt-4o-mini", "effort": "low"},
    )
    assert named.status_code == 200
    assert named.json()["effort"] == "low"
    effort_history = client.get(
        "/v1/capabilities/model-efforts/history",
        headers=reader,
        params={"provider": "openai", "model": "openai/gpt-4o-mini", "effort": "low"},
    )
    assert effort_history.status_code == 200
    assert [row["effort"] for row in effort_history.json()] == ["low"]

    for method in METHODS:
        estimate = client.post("/v1/estimates", headers=reader, json=_estimate(method))
        assert estimate.status_code == 200, estimate.text
        attached = estimate.json()["results"][0]["capabilities"]
        assert attached["status"] == "ok"
        assert attached["agent"]["agent_id"] == "agent-1"
        assert attached["agent"]["strengths"] == SECRET
        assert attached["agent"]["as_of"] == AS_OF
        assert attached["model_effort"]["effort"] == "low"
        assert attached["model_effort"]["source"] == "official"
        assert attached["model_effort"]["as_of"] == AS_OF

    hidden = client.post("/v1/estimates", json=_estimate("M4"))
    assert hidden.status_code == 200
    assert hidden.json()["results"][0]["capabilities"] == {
        "agent": None,
        "model_effort": None,
        "status": "not_authorized",
    }
    assert SECRET not in hidden.text


def test_read_token_cannot_write_or_call_m7(tmp_path):
    client = _client(tmp_path)
    admin = _auth(ADMIN)
    reader = _auth(READ)
    _assert_unauthorized(
        client.post("/v1/contributions", headers=reader, json=synthetic_contribution())
    )
    _assert_unauthorized(
        client.put("/v1/capabilities/agents", headers=reader, json=_agent())
    )
    _assert_unauthorized(
        client.put("/v1/capabilities/model-efforts", headers=reader, json=_effort("low"))
    )
    _assert_unauthorized(client.post("/v1/observations", headers=reader, json=_observation()))
    assert client.get("/v1/capabilities", headers=admin).json() == {
        "agents": [],
        "model_efforts": [],
    }
    assert client.get("/v1/catalog").json()["records"] == []

    created = client.post(
        "/v1/contributions",
        headers={**admin, "Idempotency-Key": "read-auth"},
        json=synthetic_contribution(),
    )
    assert created.status_code == 201, created.text
    contribution_id = created.json()["contribution_id"]
    _assert_unauthorized(
        client.post(f"/v1/contributions/{contribution_id}/publish", headers=reader)
    )
    assert client.get("/v1/catalog").json()["data_version"] is None

    published = client.post(f"/v1/contributions/{contribution_id}/publish", headers=admin)
    assert published.status_code == 200, published.text
    stored = client.put("/v1/capabilities/agents", headers=admin, json=_agent())
    assert stored.status_code == 200, stored.text
    observed = client.post("/v1/observations", headers=admin, json=_observation())
    assert observed.status_code == 201, observed.text
    observation_id = observed.json()["observation_id"]

    rejected = client.post("/v1/estimates", headers=reader, json=_m7())
    _assert_unauthorized(rejected)
    assert observation_id not in rejected.text
    assert "attributed_cash" not in rejected.text

    allowed = client.post("/v1/estimates", headers=admin, json=_m7())
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["results"][0]["observation_id"] == observation_id
    visible = client.get(
        "/v1/capabilities/agents",
        headers=reader,
        params={"agent_id": "agent-1"},
    )
    assert visible.status_code == 200
    assert visible.json()["agent_id"] == "agent-1"


def test_missing_token_rejects_capabilities_and_public_prices_stay_open(tmp_path):
    client = _client(tmp_path)
    admin = _auth(ADMIN)
    _assert_unauthorized(client.get("/v1/capabilities"))
    _assert_unauthorized(
        client.get("/v1/capabilities/agents", params={"agent_id": "agent-1"})
    )
    _assert_unauthorized(
        client.get("/v1/capabilities/agents/history", params={"agent_id": "agent-1"})
    )
    _assert_unauthorized(
        client.get(
            "/v1/capabilities/model-efforts",
            params={"provider": "openai", "model": "openai/gpt-4o-mini"},
        )
    )
    _assert_unauthorized(
        client.get(
            "/v1/capabilities/model-efforts/history",
            params={"provider": "openai", "model": "openai/gpt-4o-mini", "effort": "low"},
        )
    )
    _assert_unauthorized(client.get("/v1/capabilities", headers=_auth("someone-else")))
    _assert_unauthorized(client.get("/v1/capabilities", headers=_auth("")))

    created = client.post(
        "/v1/contributions",
        headers={**admin, "Idempotency-Key": "public-price"},
        json=synthetic_contribution(),
    )
    assert created.status_code == 201, created.text
    published = client.post(
        f"/v1/contributions/{created.json()['contribution_id']}/publish",
        headers=admin,
    )
    assert published.status_code == 200, published.text
    catalog = client.get("/v1/catalog")
    assert catalog.status_code == 200
    assert catalog.json()["records"][0]["provider"] == "example"
    assert catalog.json()["records"][0]["model"] == "synthetic-m4"
    evidence = client.get(f"/v1/evidence/{created.json()['evidence'][0]['id']}")
    research = client.get(f"/v1/research/{created.json()['research_id']}")
    assert evidence.status_code == 200
    assert evidence.json()["retrieved_at"] == "2026-09-25T00:00:00+00:00"
    assert research.status_code == 200
    assert research.json()["title"] == "Synthetic M4 card"
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}


def test_empty_read_token_does_not_authorize_and_admin_still_does(tmp_path):
    client = _client(tmp_path, read="")
    _assert_unauthorized(client.get("/v1/capabilities", headers=_auth("not-the-admin")))
    listed = client.get("/v1/capabilities", headers=_auth(ADMIN))
    assert listed.status_code == 200
    assert listed.json() == {"agents": [], "model_efforts": []}
    stored = client.put(
        "/v1/capabilities/agents",
        headers=_auth(ADMIN),
        json=_agent(),
    )
    assert stored.status_code == 200, stored.text
    again = client.get(
        "/v1/capabilities/agents",
        headers=_auth(ADMIN),
        params={"agent_id": "agent-1"},
    )
    assert again.status_code == 200
    assert again.json()["strengths"] == SECRET
