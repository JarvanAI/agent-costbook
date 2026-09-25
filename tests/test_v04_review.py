import json
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from agent_costbook.api import create_app
from agent_costbook.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


def _app(path: Path) -> TestClient:
    app = create_app(Settings(db_path=path, admin_token="review-token"))
    return TestClient(app, raise_server_exceptions=False)


def _observation(**overrides) -> dict:
    body = {
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
                "attempts": [
                    {"cash": "0", "api_equivalent": "100", "succeeded": True},
                ],
            }
        ],
    }
    body.update(overrides)
    return body


def _estimate(method: str) -> dict:
    return {
        "method": method,
        "currency": "USD",
        "task_category": "coding",
        "acceptance": "tests_passed",
        "candidates": [
            {
                "candidate_id": "synthetic",
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


def test_mixed_timezone_is_validation_422(tmp_path):
    client = _app(tmp_path / "review.sqlite3")
    response = client.post(
        "/v1/observations",
        headers={"Authorization": "Bearer review-token"},
        json=_observation(period_start="2026-09-01T00:00:00Z", period_end="2026-10-01"),
    )
    assert response.status_code == 422
    assert "TypeError" not in response.text
    paired = client.post(
        "/v1/observations",
        headers={"Authorization": "Bearer review-token"},
        json=_observation(
            period_start="2026-09-01T00:00:00Z",
            period_end="2026-10-01T00:00:00Z",
        ),
    )
    assert paired.status_code == 201, paired.text


def test_m7_read_requires_admin_and_keeps_observation_id(tmp_path):
    client = _app(tmp_path / "review.sqlite3")
    headers = {"Authorization": "Bearer review-token"}
    created = client.post("/v1/observations", headers=headers, json=_observation())
    assert created.status_code == 201, created.text
    observation_id = created.json()["observation_id"]
    hidden = client.post("/v1/estimates", json=_estimate("M7"))
    assert hidden.status_code == 401
    assert "attributed_cash" not in hidden.text
    assert observation_id not in hidden.text
    public = client.post(
        "/v1/estimates",
        json={
            "method": "M4",
            "currency": "USD",
            "usage": {"uncached_input": "1", "billed_output": "1"},
            "extra_cost": "0",
            "candidates": _estimate("M4")["candidates"],
        },
    )
    assert public.status_code == 200
    shown = client.post("/v1/estimates", headers=headers, json=_estimate("M7"))
    assert shown.status_code == 200, shown.text
    result = shown.json()["results"][0]
    assert result["observation_id"] == observation_id
    assert result["measurement"]["observation_id"] == observation_id
    assert Decimal(result["metrics"]["K"]) == Decimal("25")
    assert "private-task-9f3a" not in shown.text


def test_old_snapshot_http_rejects_new_methods_and_keeps_bytes(tmp_path):
    db = tmp_path / "old.sqlite3"
    shutil.copy(FIXTURES / "ac-v0.2-sample.sqlite3", db)
    client = _app(db)
    request = json.loads((ROOT / "examples" / "estimate-request.json").read_text())
    request["snapshot_id"] = "snap-1"
    request["method"] = "M4"
    current = client.post("/v1/estimates", json=request)
    assert current.status_code == 200, current.text
    assert Decimal(current.json()["results"][0]["metrics"]["cost"]) == Decimal("0.0177")
    assert current.json()["formula_version"] == "ac-formulas-v1"
    for method in ("M3", "M5", "M7"):
        request["method"] = method
        headers = {"Authorization": "Bearer review-token"} if method == "M7" else None
        rejected = client.post("/v1/estimates", headers=headers, json=request)
        assert rejected.status_code == 200, rejected.text
        body = rejected.json()
        assert body["formula_version"] == "ac-formulas-v1"
        assert body["results"][0]["status"] == "unsupported_method"
        assert body["results"][0]["metrics"] is None
        assert "0.0177" not in json.dumps(body["results"][0])
    exported = subprocess.run(
        [sys.executable, "-m", "agent_costbook.export", "--db", str(db), "--data-version", "1"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    assert exported.returncode == 0, exported.stderr
    assert exported.stdout == (FIXTURES / "ac-v0.2-published-1.json").read_bytes()
