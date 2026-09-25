import json
import os
import socket
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import httpx

from support import OPENROUTER_SHA256, openrouter_contribution, synthetic_contribution

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _client(base: str) -> httpx.Client:
    return httpx.Client(base_url=base, timeout=5, trust_env=False)


def _wait_health(client: httpx.Client, proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + 20
    last_error = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"service exited {proc.returncode}")
        try:
            response = client.get("/health")
            if response.status_code == 200 and response.json()["status"] == "ok":
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(0.1)
    raise AssertionError(f"health check failed: {last_error}")


def _start(db: Path, port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(
        {
            "ACB_DB": str(db),
            "ACB_ADMIN_TOKEN": "e2e-token",
        }
    )
    return subprocess.Popen(
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


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def test_real_http_contribution_survives_restart_and_exports_markdown(tmp_path):
    db = tmp_path / "service.sqlite3"
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    headers = {"Authorization": "Bearer e2e-token"}
    proc = _start(db, port)
    try:
        with _client(base) as client:
            _wait_health(client, proc)
            created = client.post(
                "/v1/contributions",
                headers={**headers, "Idempotency-Key": "openrouter-gpt-4o-mini"},
                json=openrouter_contribution(),
            )
            assert created.status_code == 201, created.text
            body = created.json()
            assert body["evidence"][0]["content_sha256"] == OPENROUTER_SHA256
            published = client.post(
                f"/v1/contributions/{body['contribution_id']}/publish",
                headers=headers,
            )
            assert published.status_code == 200, published.text
            snapshot_id = published.json()["snapshot_id"]
            _stop(proc)
            proc = _start(db, port)
            _wait_health(client, proc)
            catalog = client.get("/v1/catalog")
            assert catalog.status_code == 200
            payload = catalog.json()
            record = payload["records"][0]
            assert payload["snapshot_id"] == "snap-1"
            assert record["model"] == "openai/gpt-4o-mini"
            assert record["channel"] == "openrouter"
            assert record["rates"]["cache_write_per_million"] is None
            assert "snapshot_id" not in record
            estimate = client.post(
                "/v1/estimates",
                json={
                    "method": "M4",
                    "snapshot_id": snapshot_id,
                    "currency": "USD",
                    "usage": {"uncached_input": "1000", "billed_output": "400"},
                    "extra_cost": "0",
                    "candidates": [
                        {
                            "candidate_id": "openrouter-gpt-4o-mini",
                            "provider": "openai",
                            "channel": "openrouter",
                            "model": "openai/gpt-4o-mini",
                            "effort": "",
                            "plan": "payg",
                            "feature_scope": "text",
                        }
                    ],
                },
            )
            assert estimate.status_code == 200, estimate.text
            result = estimate.json()["results"][0]
            assert result["status"] == "ok"
            assert Decimal(result["metrics"]["cost"]) == Decimal("0.00039")
            assert result["sources"] == [body["evidence"][0]["id"]]
            exported = client.get(
                f"/v1/research/{body['research_id']}",
                params={"format": "markdown"},
            )
            assert exported.status_code == 200
            assert "0.00039 USD" in exported.text
            assert "68650e8ee5ddbdea6eeae28779820585ec6a13cb6f5ed31e37323b4657a8eade" in exported.text

            historical = synthetic_contribution()
            first = client.post("/v1/contributions", headers=headers, json=historical)
            first_snapshot = client.post(
                f"/v1/contributions/{first.json()['contribution_id']}/publish",
                headers=headers,
            ).json()["snapshot_id"]
            historical["records"][0]["base_snapshot_id"] = first_snapshot
            historical["records"][0]["rates"]["uncached_input_per_million"] = "1"
            second = client.post("/v1/contributions", headers=headers, json=historical)
            client.post(
                f"/v1/contributions/{second.json()['contribution_id']}/publish",
                headers=headers,
            )
            old = client.post(
                "/v1/estimates",
                json={
                    "method": "M4",
                    "snapshot_id": first_snapshot,
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
                            "candidate_id": "historical",
                            "provider": "example",
                            "channel": "api",
                            "model": "synthetic-m4",
                            "plan": "payg",
                            "feature_scope": "text",
                        }
                    ],
                },
            )
            assert Decimal(old.json()["results"][0]["metrics"]["cost"]) == Decimal("0.0177")
            carried = client.get("/v1/catalog", params={"snapshot_id": first_snapshot})
            carried_body = carried.json()
            carried_rows = {row["model"]: row for row in carried_body["records"]}
            assert carried_body["snapshot_id"] == "snap-2"
            assert set(carried_rows) == {"openai/gpt-4o-mini", "synthetic-m4"}
            assert carried_rows["synthetic-m4"]["rates"]["uncached_input_per_million"]["amount"] == "2"
            whole = client.get("/v1/catalog")
            assert {row["model"] for row in whole.json()["records"]} == {
                "openai/gpt-4o-mini",
                "synthetic-m4",
            }
    finally:
        _stop(proc)


def test_real_http_review_boundaries(tmp_path):
    db = tmp_path / "review.sqlite3"
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    headers = {"Authorization": "Bearer e2e-token"}
    proc = _start(db, port)
    try:
        with _client(base) as client:
            _wait_health(client, proc)
            cny = synthetic_contribution()
            cny["records"][0]["currency"] = "CNY"
            cny["records"][0]["model"] = "cny-card"
            created = client.post("/v1/contributions", headers=headers, json=cny)
            assert created.status_code == 201, created.text
            published = client.post(
                f"/v1/contributions/{created.json()['contribution_id']}/publish",
                headers=headers,
            )
            assert published.status_code == 200, published.text
            shown = client.post(
                "/v1/estimates",
                json={
                    "method": "M0",
                    "currency": "CNY",
                    "candidates": [
                        {
                            "candidate_id": "cny",
                            "provider": "example",
                            "channel": "api",
                            "model": "cny-card",
                            "plan": "payg",
                            "feature_scope": "text",
                        }
                    ],
                },
            )
            assert shown.status_code == 200, shown.text
            units = shown.json()["results"][0]["units"]
            assert set(units.values()) == {"CNY_per_million_tokens"}
            missing = client.post(
                "/v1/estimates",
                json={
                    "method": "M4",
                    "currency": "CNY",
                    "extra_cost": "0",
                    "usage": {},
                    "candidates": [
                        {
                            "candidate_id": "empty-usage",
                            "provider": "example",
                            "channel": "api",
                            "model": "cny-card",
                            "plan": "payg",
                            "feature_scope": "text",
                        }
                    ],
                },
            )
            assert missing.status_code == 200
            assert missing.json()["results"][0]["status"] == "missing_data"
            assert missing.json()["results"][0]["metrics"] is None
            invalid = client.post(
                "/v1/estimates",
                json={
                    "method": "M4",
                    "currency": "CNY",
                    "extra_cost": "0",
                    "usage": {"billed_output": "1", "output": "1"},
                    "candidates": [
                        {
                            "candidate_id": "overlap",
                            "provider": "example",
                            "channel": "api",
                            "model": "cny-card",
                            "plan": "payg",
                            "feature_scope": "text",
                        }
                    ],
                },
            )
            assert invalid.json()["results"][0]["status"] == "invalid_input"
            assert invalid.json()["results"][0]["error_code"] == "invalid_usage"
            secret = "4.3210987"
            priced = client.post(
                "/v1/estimates",
                json={
                    "method": "M4",
                    "currency": "CNY",
                    "extra_cost": "0",
                    "usage": {"uncached_input": "0", "billed_output": "0"},
                    "candidates": [
                        {
                            "candidate_id": "private",
                            "provider": "example",
                            "channel": "api",
                            "model": "cny-card",
                            "plan": "payg",
                            "feature_scope": "text",
                            "private_rates": {"uncached_input_per_million": secret},
                        }
                    ],
                },
            )
            provenance = priced.json()["results"][0]["rate_provenance"]
            assert provenance["uncached_input_per_million"] == "request_override"
            assert provenance["billed_output_per_million"] == "public"
            assert secret not in str(provenance)
            other = synthetic_contribution()
            other["records"][0]["model"] = "second-card"
            other_created = client.post("/v1/contributions", headers=headers, json=other)
            other_published = client.post(
                f"/v1/contributions/{other_created.json()['contribution_id']}/publish",
                headers=headers,
            )
            later_id = other_published.json()["snapshot_id"]
            later_catalog = client.get("/v1/catalog", params={"snapshot_id": later_id})
            assert {row["model"] for row in later_catalog.json()["records"]} == {
                "cny-card",
                "second-card",
            }
            stale = synthetic_contribution()
            stale["records"][0]["model"] = "second-card"
            stale["records"][0]["base_snapshot_id"] = published.json()["snapshot_id"]
            stale["records"][0]["rates"]["uncached_input_per_million"] = "8"
            stale_created = client.post("/v1/contributions", headers=headers, json=stale)
            rejected = client.post(
                f"/v1/contributions/{stale_created.json()['contribution_id']}/publish",
                headers=headers,
            )
            assert rejected.status_code == 409
            assert rejected.json()["status"] == "conflict"
            active = {
                row["model"]: row["rates"]["uncached_input_per_million"]["amount"]
                for row in client.get("/v1/catalog").json()["records"]
            }
            assert active["second-card"] == "2"
    finally:
        _stop(proc)


def _plan(model: str, multiplier: str, feature_scope: str = "code") -> dict:
    payload = synthetic_contribution()
    payload["records"][0].update(
        {
            "channel": "subscription",
            "model": model,
            "effort": "high",
            "plan": f"plan-{multiplier}",
            "feature_scope": feature_scope,
            "rates": {},
            "subscription": {
                "monthly_price": "20",
                "price_period": "month",
                "quota_multiplier": multiplier,
                "baseline_api_budget": "100",
                "utilization": "0.5",
                "cost_per_task": "0.5",
                "weight": "1",
                "task_profile": "coding",
            },
        }
    )
    return payload


def test_real_http_m6_ratio_then_stopped_export(tmp_path):
    db = tmp_path / "m6.sqlite3"
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    headers = {"Authorization": "Bearer e2e-token"}
    proc = _start(db, port)
    try:
        with _client(base) as client:
            _wait_health(client, proc)
            for key, payload in (
                ("base", _plan("example-model", "1")),
                ("higher", _plan("example-model", "2")),
                ("image", _plan("example-model", "9", "image")),
            ):
                created = client.post(
                    "/v1/contributions",
                    headers={**headers, "Idempotency-Key": key},
                    json=payload,
                )
                assert created.status_code == 201, created.text
                published = client.post(
                    f"/v1/contributions/{created.json()['contribution_id']}/publish",
                    headers=headers,
                )
                assert published.status_code == 200, published.text
            estimate = client.post(
                "/v1/estimates",
                json={
                    "method": "M6",
                    "currency": "USD",
                    "reference_candidate_id": "base",
                    "candidates": [
                        {
                            "candidate_id": "higher",
                            "provider": "example",
                            "channel": "subscription",
                            "model": "example-model",
                            "effort": "high",
                            "plan": "plan-2",
                            "feature_scope": "code",
                            "marginal_cash": "0",
                        },
                        {
                            "candidate_id": "base",
                            "provider": "example",
                            "channel": "subscription",
                            "model": "example-model",
                            "effort": "high",
                            "plan": "plan-1",
                            "feature_scope": "code",
                        },
                        {
                            "candidate_id": "image-as-code",
                            "provider": "example",
                            "channel": "subscription",
                            "model": "example-model",
                            "effort": "high",
                            "plan": "plan-9",
                            "feature_scope": "code",
                        },
                    ],
                },
            )
            assert estimate.status_code == 200, estimate.text
            by_id = {row["candidate_id"]: row for row in estimate.json()["results"]}
            assert Decimal(by_id["higher"]["metrics"]["N"]) == Decimal("200")
            assert Decimal(by_id["higher"]["metrics"]["K"]) == Decimal("0.1")
            assert Decimal(by_id["higher"]["metrics"]["cost_ratio"]) == Decimal("0.5")
            assert by_id["higher"]["metrics"]["cash_increment"] == "0"
            assert Decimal(by_id["higher"]["metrics"]["amortization"]) == Decimal("0.1")
            assert "total" not in by_id["higher"]["metrics"]
            assert by_id["image-as-code"]["status"] == "missing_data"
            catalog = client.get("/v1/catalog").json()["records"]
            assert all(row["plan"] != "plan-9" or row["scope"]["function"] == "image" for row in catalog)
    finally:
        _stop(proc)
    exported = subprocess.run(
        [sys.executable, "-m", "agent_costbook.export", "--db", str(db)],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    assert exported.returncode == 0, exported.stderr
    document = json.loads(exported.stdout)
    assert document["snapshot_id"] == "snap-3"
    assert {row["plan"] for row in document["records"]} == {"plan-1", "plan-2", "plan-9"}
    assert "e2e-token" not in exported.stdout.decode()


def test_real_http_m3_m5_and_m7_share_the_core_and_hide_task_text(tmp_path):
    db = tmp_path / "service.sqlite3"
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    headers = {"Authorization": "Bearer e2e-token"}
    payload = synthetic_contribution()
    record = payload["records"][0]
    record["window_start"] = "2026-09-01T00:00:00+00:00"
    record["window_end"] = "2026-10-01T00:00:00+00:00"
    record["subscription"] = {
        "monthly_price": "20",
        "price_period": "month",
        "quota_multiplier": "2",
        "baseline_tasks": "100",
        "utilization": "0.5",
        "weight": "1",
        "task_profile": "coding",
        "baseline_group": "code",
    }
    tasks = []
    for index in range(10):
        task_id = "private-task-9f3a" if index == 0 else f"task-{index}"
        attempts = [{"cash": "0", "api_equivalent": "10", "succeeded": True}]
        if index == 0:
            attempts.append({"cash": "0", "api_equivalent": "0", "succeeded": True})
        tasks.append({"task_id": task_id, "attempts": attempts})
    observation = {
        "provider": "example",
        "channel": "api",
        "model": "synthetic-m4",
        "effort": "",
        "plan": "payg",
        "feature_scope": "text",
        "currency": "USD",
        "period_start": "2026-09-01T00:00:00+00:00",
        "period_end": "2026-10-01T00:00:00+00:00",
        "task_category": "coding",
        "acceptance": "tests_passed",
        "subscription_cash": "25",
        "tasks": tasks,
    }
    candidate = {
        "candidate_id": "synthetic",
        "provider": "example",
        "channel": "api",
        "model": "synthetic-m4",
        "plan": "payg",
        "feature_scope": "text",
        "window_start": "2026-09-01T00:00:00+00:00",
        "window_end": "2026-10-01T00:00:00+00:00",
    }
    proc = _start(db, port)
    try:
        with _client(base) as client:
            _wait_health(client, proc)
            refused = client.post("/v1/observations", json=observation)
            assert refused.status_code == 401
            invalid = client.post(
                "/v1/observations",
                headers=headers,
                json={**observation, "subscription_cash": None},
            )
            assert invalid.status_code == 422
            created = client.post(
                "/v1/contributions",
                headers={**headers, "Idempotency-Key": "v04-card"},
                json=payload,
            )
            assert created.status_code == 201, created.text
            published = client.post(
                f"/v1/contributions/{created.json()['contribution_id']}/publish",
                headers=headers,
            )
            assert published.status_code == 200, published.text
            observed = client.post(
                "/v1/observations",
                headers={**headers, "Idempotency-Key": "v04-measure"},
                json=observation,
            )
            assert observed.status_code == 201, observed.text
            replay = client.post(
                "/v1/observations",
                headers={**headers, "Idempotency-Key": "v04-measure"},
                json=observation,
            )
            assert replay.status_code == 200
            assert replay.json()["observation_id"] == observed.json()["observation_id"]
            changed = dict(observation)
            changed["subscription_cash"] = "26"
            conflict = client.post(
                "/v1/observations",
                headers={**headers, "Idempotency-Key": "v04-measure"},
                json=changed,
            )
            assert conflict.status_code == 409

            def estimate(method, **extra):
                request_headers = headers if method == "M7" else None
                response = client.post(
                    "/v1/estimates",
                    headers=request_headers,
                    json={
                        "method": method,
                        "currency": "USD",
                        "usage": {
                            "uncached_input": "1000",
                            "cache_read": "2000",
                            "cache_write": "500",
                            "billed_output": "400",
                        },
                        "extra_cost": "0.01",
                        "candidates": [candidate],
                        **extra,
                    },
                )
                assert response.status_code == 200, response.text
                return response.json()["results"][0]

            m2 = estimate("M2")
            m3 = estimate("M3")
            m4 = estimate("M4")
            m5 = estimate("M5")
            assert Decimal(m3["metrics"]["K"]) == Decimal(m2["metrics"]["K"]) == Decimal("0.2")
            assert m3["quality_proxy"]["explains"] == "not_a_measured_success_rate"
            assert "do_not_reweight_for_routing" in m3["assumptions"]
            assert Decimal(m5["metrics"]["K"]) == Decimal(m4["metrics"]["cost"]) == Decimal("0.0177")
            assert m5["quality_proxy"]["explains"] == "not_a_measured_success_rate"
            hidden = client.post(
                "/v1/estimates",
                json={
                    "method": "M7",
                    "currency": "USD",
                    "task_category": "coding",
                    "acceptance": "tests_passed",
                    "candidates": [candidate],
                },
            )
            assert hidden.status_code == 401
            assert "attributed_cash" not in hidden.text
            assert "25" not in hidden.text
            measured = estimate(
                "M7",
                task_category="coding",
                acceptance="tests_passed",
            )
            assert measured["status"] == "ok"
            assert measured["observation_id"] == observed.json()["observation_id"]
            assert Decimal(measured["metrics"]["K"]) == Decimal("2.5")
            assert Decimal(measured["metrics"]["api_equivalent"]) == Decimal("100")
            assert measured["measurement"]["sample_size"] == "10"
            encoded = json.dumps(measured)
            assert "private-task-9f3a" not in encoded
            assert "Infinity" not in encoded and "NaN" not in encoded
            catalog = client.get("/v1/catalog")
            assert catalog.status_code == 200
            catalog_text = catalog.text
            assert "private-task-9f3a" not in catalog_text
            assert "2.5" not in catalog_text
    finally:
        _stop(proc)
    exported = subprocess.run(
        [sys.executable, "-m", "agent_costbook.export", "--db", str(db)],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    assert exported.returncode == 0, exported.stderr
    exported_text = exported.stdout.decode()
    assert "private-task-9f3a" not in exported_text
    assert "ac-formulas-v2" in exported_text
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_bytes(exported.stdout)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "method": "M3",
                "currency": "USD",
                "candidates": [candidate],
            }
        ),
        encoding="utf-8",
    )
    offline = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_costbook.offline",
            "estimate",
            "--snapshot",
            str(snapshot),
            "--request",
            str(request),
            "--publisher",
            json.loads(exported.stdout)["publisher_id"],
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    assert offline.returncode == 0, offline.stderr
    offline_result = json.loads(offline.stdout)["results"][0]
    assert Decimal(offline_result["metrics"]["K"]) == Decimal("0.2")
    request.write_text(
        json.dumps({"method": "M7", "currency": "USD", "task_category": "coding", "acceptance": "tests_passed", "candidates": [candidate]}),
        encoding="utf-8",
    )
    offline_m7 = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_costbook.offline",
            "estimate",
            "--snapshot",
            str(snapshot),
            "--request",
            str(request),
            "--publisher",
            json.loads(exported.stdout)["publisher_id"],
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    assert offline_m7.returncode == 0, offline_m7.stderr
    assert json.loads(offline_m7.stdout)["results"][0]["status"] == "missing_data"
    assert "2.5" not in offline_m7.stdout.decode()
