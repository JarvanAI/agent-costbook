import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import httpx

from agent_costbook.estimates import freshness_view
from agent_costbook.export import build_document, render
from agent_costbook.store import Store

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
REQUEST = ROOT / "examples" / "estimate-request.json"
BUSINESS = (
    "candidate_id",
    "status",
    "metrics",
    "units",
    "method",
    "formula_version",
    "snapshot_id",
    "sources",
    "missing_fields",
    "error_code",
    "rate_provenance",
)


def _export(db: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "agent_costbook.export", "--db", str(db), *args],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )


def _estimate(snapshot: Path, request: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_costbook.offline",
            "estimate",
            "--snapshot",
            str(snapshot),
            "--request",
            str(request),
            *args,
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )


def _business(result: dict) -> dict:
    return {key: result.get(key) for key in BUSINESS}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_old_export_bytes_survive_opening_the_database(tmp_path):
    db = tmp_path / "sample.sqlite3"
    shutil.copy(FIXTURES / "ac-v0.2-sample.sqlite3", db)
    first = _export(db, "--data-version", "1")
    second = _export(db, "--data-version", "2")
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first.stdout == (FIXTURES / "ac-v0.2-published-1.json").read_bytes()
    assert second.stdout == (FIXTURES / "ac-v0.2-published-2.json").read_bytes()
    opened = Store(db)
    publisher = opened.publisher_id()
    opened.close()
    assert publisher == "pub_d740ade90dc84618a0732b0b6708cffe"
    assert _export(db, "--data-version", "1").stdout == first.stdout
    upgraded = json.loads(_export(db, "--data-version", "2").stdout)
    assert upgraded["formula_version"] == "ac-formulas-v1"
    assert upgraded["content_sha256"] == json.loads(second.stdout)["content_sha256"]
    assert _export(db, "--data-version", "2").stdout == second.stdout


def test_old_file_refuses_bad_identity_and_limits_comparison(tmp_path):
    snapshot = FIXTURES / "ac-v0.2-published-1.json"
    document = json.loads(snapshot.read_text())
    accepted = _estimate(snapshot, REQUEST, "--publisher", document["publisher_id"])
    assert accepted.returncode == 0, accepted.stderr
    accepted_body = json.loads(accepted.stdout)
    assert accepted_body["results"][0]["metrics"]["cost"] == "0.0177"
    assert accepted_body["comparison"] == "not_requested"
    assert accepted_body["read_view"]["freshness"]["stale"] is None

    wrong_publisher = _estimate(snapshot, REQUEST, "--publisher", "pub_other")
    assert wrong_publisher.returncode == 2
    assert wrong_publisher.stdout == b""
    assert b"0.0177" not in wrong_publisher.stderr

    tampered = tmp_path / "tampered.json"
    document = json.loads(snapshot.read_text())
    document["records"][0]["rates"]["uncached_input_per_million"]["amount"] = "9"
    tampered.write_text(json.dumps(document), encoding="utf-8")
    rejected = _estimate(tampered, REQUEST, "--publisher", document["publisher_id"])
    assert rejected.returncode == 2
    assert b"0.0177" not in rejected.stdout
    assert b"content_sha256" in rejected.stderr

    old_formula = tmp_path / "old-formula.json"
    document = json.loads(snapshot.read_text())
    document["formula_version"] = "ac-formulas-v0"
    old_formula.write_text(json.dumps(document), encoding="utf-8")
    unsupported = _estimate(old_formula, REQUEST, "--publisher", document["publisher_id"])
    assert unsupported.returncode == 3
    body = json.loads(unsupported.stdout)
    assert body["status"] == "unsupported_formula"
    assert body["formula_version"] == "ac-formulas-v0"
    assert "results" not in body or body["results"] == []
    assert "0.0177" not in unsupported.stdout.decode()

    paired = json.loads((FIXTURES / "ac-v0.2-published-2.json").read_text())
    reference = {
        "method": "M1",
        "currency": "USD",
        "reference_candidate_id": "plus",
        "candidates": [
            {
                "candidate_id": "payg",
                "provider": "example",
                "channel": "api",
                "model": "synthetic-m4",
                "plan": "payg",
                "feature_scope": "text",
            },
            {
                "candidate_id": "plus",
                "provider": "example",
                "channel": "subscription",
                "model": "example-model",
                "plan": "plus",
                "feature_scope": "code",
            },
        ],
    }
    request = tmp_path / "reference.json"
    request.write_text(json.dumps(reference), encoding="utf-8")
    limited = _estimate(
        FIXTURES / "ac-v0.2-published-2.json",
        request,
        "--publisher",
        paired["publisher_id"],
    )
    assert limited.returncode == 0, limited.stderr
    payload = json.loads(limited.stdout)
    assert payload["comparison"] == "unavailable"
    assert all("rank" not in item for item in payload["results"])
    assert "task_profile" not in limited.stdout.decode()
    by_id = {item["candidate_id"]: item for item in payload["results"]}
    assert by_id["plus"]["status"] == "ok"
    assert by_id["plus"]["metrics"]["K"] == "10"
    assert by_id["payg"]["status"] == "missing_data"
    assert payload["read_view"]["freshness"]["stale"] is None


def test_freshness_unknown_is_not_false():
    unknown = freshness_view(None, now="2026-09-25T00:00:00+00:00", max_age_seconds=60)
    assert unknown["stale"] is None
    undated = freshness_view("2026-09-25T00:00:00+00:00", now=None, max_age_seconds=60)
    assert undated["stale"] is None
    stale = freshness_view(
        "2026-09-25T00:00:00+00:00",
        now="2026-09-25T00:02:00+00:00",
        max_age_seconds=60,
    )
    assert stale["stale"] is True


def test_real_http_and_offline_share_a_new_publication(tmp_path):
    db = tmp_path / "service.sqlite3"
    store = Store(db)
    created, _ = store.create_contribution(
        {
            "research": {"title": "Comparable plans", "markdown": "M1 pair\n"},
            "evidence": [
                {
                    "source_kind": "synthetic_fixture",
                    "source_url": "fixture://plans",
                    "collector_kind": "test",
                    "collector_name": "pytest",
                    "content": "recorded plan prices",
                    "retrieved_at": "2026-09-25T00:00:00+00:00",
                }
            ],
            "records": [
                _plan("alpha", "20"),
                _plan("beta", "10"),
            ],
        },
        "plans",
    )
    store.publish(created["contribution_id"])
    store.close()
    exported = _export(db)
    assert exported.returncode == 0, exported.stderr
    document = json.loads(exported.stdout)
    scopes = [row["scope"] for row in document["records"]]
    assert all("task_profile" in scope and "baseline_group" in scope for scope in scopes)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_bytes(exported.stdout)
    request = {
        "method": "M1",
        "currency": "USD",
        "reference_candidate_id": "alpha",
        "candidates": [
            {
                "candidate_id": "alpha",
                "provider": "example",
                "channel": "subscription",
                "model": "alpha",
                "plan": "plus",
                "feature_scope": "code",
            },
            {
                "candidate_id": "beta",
                "provider": "example",
                "channel": "subscription",
                "model": "beta",
                "plan": "plus",
                "feature_scope": "code",
            },
        ],
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    offline = _estimate(snapshot, request_path, "--publisher", document["publisher_id"])
    assert offline.returncode == 0, offline.stderr
    offline_body = json.loads(offline.stdout)
    assert offline_body["comparison"] == "available"
    port = _free_port()
    env = {
        **dict(**{key: value for key, value in __import__("os").environ.items()}),
        "ACB_DB": str(db),
        "ACB_ADMIN_TOKEN": "e2e-token",
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "agent_costbook.api:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env=env,
        start_new_session=True,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False) as client:
            deadline = __import__("time").monotonic() + 20
            while True:
                if proc.poll() is not None:
                    raise AssertionError(f"service exited {proc.returncode}")
                try:
                    health = client.get("/health")
                    if health.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if __import__("time").monotonic() > deadline:
                    raise AssertionError("health check failed")
                __import__("time").sleep(0.1)
            response = client.post("/v1/estimates", json=request)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
    assert response.status_code == 200, response.text
    http_body = response.json()
    assert http_body["publisher_id"] == offline_body["publisher_id"]
    assert http_body["data_version"] == offline_body["data_version"]
    assert http_body["snapshot_id"] == offline_body["snapshot_id"]
    assert http_body["content_sha256"] == offline_body["content_sha256"]
    http_results = {item["candidate_id"]: _business(item) for item in http_body["results"]}
    offline_results = {item["candidate_id"]: _business(item) for item in offline_body["results"]}
    assert offline_results == http_results
    assert offline_results["beta"]["metrics"]["K"] == "5"
    assert offline_results["beta"]["metrics"]["cost_ratio"] == "0.5"
    assert {item["rank"] for item in offline_body["results"]} == {1, 2}
    opened = Store(db)
    try:
        rendered = render(build_document(opened, 1))
    finally:
        opened.close()
    assert rendered == exported.stdout


def _plan(model: str, price: str) -> dict:
    return {
        "provider": "example",
        "channel": "subscription",
        "model": model,
        "plan": "plus",
        "feature_scope": "code",
        "currency": "USD",
        "rates": {},
        "subscription": {
            "monthly_price": price,
            "price_period": "month",
            "quota_multiplier": "2",
            "task_profile": "coding",
            "baseline_group": "coding",
        },
        "evidence_indexes": [0],
    }
