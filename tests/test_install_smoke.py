import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx

from support import synthetic_contribution

ROOT = Path(__file__).resolve().parents[1]
FROZEN_PATHS = {
    "/health",
    "/v1/catalog",
    "/v1/estimates",
    "/v1/evidence/{evidence_id}",
    "/v1/research/{research_id}",
    "/v1/contributions",
    "/v1/contributions/{contribution_id}/publish",
    "/v1/observations",
}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(method: str, url: str, body: dict | None = None, headers: dict | None = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _wait_health(base: str, proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + 20
    last = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"installed service exited {proc.returncode}")
        try:
            status, payload = _request("GET", f"{base}/health")
            if status == 200 and json.loads(payload)["status"] == "ok":
                return
        except OSError as exc:
            last = exc
        time.sleep(0.1)
    raise AssertionError(f"installed health check failed: {last}")


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def test_clean_install_contributes_publishes_estimates_and_exports(tmp_path):
    wheel_dir = tmp_path / "dist"
    build = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    wheels = list(wheel_dir.glob("agent_costbook-1.0.0-*.whl"))
    assert len(wheels) == 1
    python = tmp_path / "venv" / "bin" / "python"
    venv = subprocess.run(["uv", "venv", str(tmp_path / "venv")], capture_output=True, text=True)
    assert venv.returncode == 0, venv.stderr
    installed = subprocess.run(
        ["uv", "pip", "install", "--python", str(python), str(wheels[0])],
        capture_output=True,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr
    version = subprocess.run(
        [str(python), "-c", "import agent_costbook; print(agent_costbook.__version__, end='')"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": ""},
    )
    assert version.returncode == 0, version.stderr
    assert version.stdout == "1.0.0"
    bindir = python.parent
    for command in (
        "ac",
        "agent-costbook-export",
        "agent-costbook-backup",
        "agent-costbook-migrate",
    ):
        assert (bindir / command).is_file()

    skill = (ROOT / "skills" / "costbook-contribute" / "SKILL.md").read_text(encoding="utf-8")
    for endpoint in (
        "/v1/contributions",
        "/v1/estimates",
        "/v1/catalog",
        "/v1/evidence/",
        "/v1/research/",
        "no research-job endpoint",
    ):
        assert endpoint in skill
    assert "The service does not research for you." in skill

    db = tmp_path / "installed.sqlite3"
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update({"ACB_DB": str(db), "ACB_ADMIN_TOKEN": "install-token", "PYTHONPATH": ""})
    proc = subprocess.Popen(
        [
            str(python),
            "-m",
            "uvicorn",
            "agent_costbook.api:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=tmp_path,
        env=env,
        start_new_session=True,
    )
    try:
        _wait_health(base, proc)
        with httpx.Client(base_url=base, timeout=5, trust_env=False) as client:
            schema = client.get("/openapi.json")
            assert schema.status_code == 200
            paths = set(schema.json()["paths"])
            assert FROZEN_PATHS <= paths
            assert all("research-job" not in path for path in paths)
            info = schema.json()["info"]
            assert info["version"] == "1.0.0"
        status, raw = _request(
            "POST",
            f"{base}/v1/contributions",
            synthetic_contribution(),
            {"Authorization": "Bearer install-token", "Idempotency-Key": "install-smoke"},
        )
        assert status == 201, raw
        created = json.loads(raw)
        status, raw = _request(
            "POST",
            f"{base}/v1/contributions/{created['contribution_id']}/publish",
            headers={"Authorization": "Bearer install-token"},
        )
        assert status == 200, raw
        published = json.loads(raw)
        status, raw = _request(
            "POST",
            f"{base}/v1/estimates",
            {
                "method": "M4",
                "snapshot_id": published["snapshot_id"],
                "currency": "USD",
                "usage": {
                    "uncached_input": "1000",
                    "cache_read": "2000",
                    "cache_write": "500",
                    "billed_output": "400",
                },
                "extra_cost": "0",
                "candidates": [
                    {
                        "candidate_id": "installed",
                        "provider": "example",
                        "channel": "api",
                        "model": "synthetic-m4",
                        "plan": "payg",
                        "feature_scope": "text",
                    }
                ],
            },
        )
        assert status == 200, raw
        estimate = json.loads(raw)
        assert estimate["results"][0]["status"] == "ok"
        assert estimate["formula_version"] == "ac-formulas-v2"
    finally:
        _stop(proc)

    migrated = subprocess.run(
        [str(bindir / "agent-costbook-migrate"), "--db", str(db)],
        capture_output=True,
        text=True,
    )
    assert migrated.returncode == 0, migrated.stderr
    exported = subprocess.run(
        [str(bindir / "agent-costbook-export"), "--db", str(db)],
        capture_output=True,
        check=False,
    )
    assert exported.returncode == 0, exported.stderr
    document = json.loads(exported.stdout)
    assert document["schema_version"] == 1
    assert document["formula_version"] == "ac-formulas-v2"
    assert document["data_version"] == 1
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_bytes(exported.stdout)
    request = tmp_path / "estimate.json"
    request.write_text(
        json.dumps(
            {
                "method": "M4",
                "snapshot_id": "snap-1",
                "currency": "USD",
                "usage": {
                    "uncached_input": "1000",
                    "billed_output": "400",
                },
                "extra_cost": "0",
                "candidates": [
                    {
                        "candidate_id": "installed",
                        "provider": "example",
                        "channel": "api",
                        "model": "synthetic-m4",
                        "plan": "payg",
                        "feature_scope": "text",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    offline = subprocess.run(
        [
            str(bindir / "ac"),
            "estimate",
            "--snapshot",
            str(snapshot),
            "--request",
            str(request),
            "--publisher",
            document["publisher_id"],
        ],
        capture_output=True,
        check=False,
    )
    assert offline.returncode == 0, offline.stderr
    assert json.loads(offline.stdout)["results"][0]["status"] == "ok"
    saved = tmp_path / "saved.sqlite3"
    backed_up = subprocess.run(
        [
            str(bindir / "agent-costbook-backup"),
            "--source",
            str(db),
            "--destination",
            str(saved),
        ],
        capture_output=True,
        text=True,
    )
    assert backed_up.returncode == 0, backed_up.stderr
    restored = tmp_path / "restored.sqlite3"
    restore = subprocess.run(
        [
            str(bindir / "agent-costbook-backup"),
            "--restore",
            "--source",
            str(saved),
            "--destination",
            str(restored),
        ],
        capture_output=True,
        text=True,
    )
    assert restore.returncode == 0, restore.stderr
    restored_export = subprocess.run(
        [str(bindir / "agent-costbook-export"), "--db", str(restored)],
        capture_output=True,
        check=False,
    )
    assert restored_export.stdout == exported.stdout
