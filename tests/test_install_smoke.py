import hashlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import pytest

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
AS_OF = "2026-09-27T00:00:00+00:00"
PRICE_RETRIEVED_AT = "2026-09-25T00:00:00+00:00"
AGENT_ID = "installed-agent"
MODEL = "synthetic-m4"
PROVIDER = "example"
REPLACED_ADMIN = "install-smoke-not-admin"
_credentials: list[str] = []
_bearer = ""
_PROXY_KEYS = {
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
}

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="installed-wheel DX acceptance is verified on Linux only",
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _isolated_env(home: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ACB_") and key != "PYTHONPATH" and key not in _PROXY_KEYS
    }
    env.pop("VIRTUAL_ENV", None)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / "xdg-config")
    env["XDG_DATA_HOME"] = str(home / "xdg-data")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _reset_credentials() -> None:
    global _bearer
    _credentials.clear()
    _bearer = ""


def _remember(*values: str) -> None:
    for value in values:
        if value and value not in _credentials:
            _credentials.append(value)


def _redact(text: str) -> str:
    redacted = text
    for secret in _credentials:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


def _reject_credentials(text: str) -> None:
    if _redact(text) != text:
        raise AssertionError("command output contains a configured credential")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _bearer_scope:
    """Install one bearer for the nested HTTP calls without putting it in failure frames."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._previous = ""

    def __repr__(self) -> str:
        return "_bearer_scope(set)"

    def __enter__(self) -> None:
        global _bearer
        self._previous = _bearer
        _bearer = self._token

    def __exit__(self, exc_type, exc, tb) -> bool:
        global _bearer
        _bearer = self._previous
        return False


def _assert_bytes_unchanged(path: Path, original: bytes) -> None:
    current = path.read_bytes()
    if current != original:
        raise AssertionError(f"{path.name} changed ({len(original)} -> {len(current)} bytes)")


def _request(method: str, url: str, body: dict | None = None, idempotency_key: str | None = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if idempotency_key:
        request.add_header("Idempotency-Key", idempotency_key)
    if _bearer:
        request.add_header("Authorization", f"Bearer {_bearer}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _http_json(method: str, url: str, body: dict | None = None, idempotency_key: str | None = None):
    status, raw = _request(method, url, body, idempotency_key)
    if status < 200 or status >= 300:
        detail = _redact(raw.decode("utf-8", errors="replace"))
        raise AssertionError(f"{method} {url} returned {status}: {detail[:800]}")
    return json.loads(raw)


def _wait_health(base: str, proc: subprocess.Popen, log_path: Path) -> None:
    deadline = time.monotonic() + 20
    last = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            detail = _redact(_log_text(log_path))
            raise AssertionError(f"installed service exited {proc.returncode}: {detail[-1500:]}")
        try:
            status, payload = _request("GET", f"{base}/health")
            if status == 200 and json.loads(payload)["status"] == "ok":
                return
        except OSError as exc:
            last = exc
        time.sleep(0.1)
    detail = _redact(_log_text(log_path))
    raise AssertionError(f"installed health check failed: {last}; {detail[-1500:]}")


def _log_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _interrupt(proc: subprocess.Popen, *, required: bool) -> None:
    """Ctrl-C the owned serve process group. SIGKILL is only the stuck-process fallback."""

    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGINT)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)
        if required:
            raise AssertionError("installed serve ignored SIGINT")


def _run(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _json_ok(result: subprocess.CompletedProcess[str]) -> dict:
    combined = result.stdout + result.stderr
    if result.returncode != 0:
        detail = _redact(combined)
        raise AssertionError(f"command failed with {result.returncode}: {detail[-2000:]}")
    _reject_credentials(combined)
    return json.loads(result.stdout)


def _tree(root: Path) -> set[Path]:
    if not root.exists():
        return set()
    return {path.relative_to(root) for path in root.rglob("*")}


def _fingerprint(db: Path) -> tuple:
    rows = []
    for path in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
        if path.exists():
            rows.append((path.name, hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size))
        else:
            rows.append((path.name, None, None))
    return tuple(rows)


def test_clean_install_contributes_publishes_estimates_and_exports(tmp_path):
    _reset_credentials()
    wheel_dir = tmp_path / "dist"
    build = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    wheels = list(wheel_dir.glob("agent_costbook-1.1.0-*.whl"))
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

    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    assert not work.resolve().is_relative_to(ROOT.resolve())
    env = _isolated_env(home)
    located = _run(
        [str(python), "-c", "import agent_costbook; print(agent_costbook.__file__)"],
        cwd=work,
        env=env,
    )
    assert located.returncode == 0, located.stderr
    module_path = Path(located.stdout.strip()).resolve()
    assert module_path.is_relative_to((tmp_path / "venv").resolve())
    assert not module_path.is_relative_to(ROOT.resolve())
    version = _run(
        [str(python), "-c", "import agent_costbook; print(agent_costbook.__version__, end='')"],
        cwd=work,
        env=env,
    )
    assert version.returncode == 0, version.stderr
    assert version.stdout == "1.1.0"

    bindir = python.parent
    for command in (
        "ac",
        "agent-costbook",
        "agent-costbook-export",
        "agent-costbook-backup",
        "agent-costbook-migrate",
    ):
        assert (bindir / command).is_file()
    ac_script = (bindir / "ac").read_text(encoding="utf-8", errors="replace")
    assert str(ROOT) not in ac_script
    assert "agent_costbook.offline" in ac_script
    for binary in (bindir / "ac", bindir / "agent-costbook"):
        reported = _run([str(binary), "--version"], cwd=work, env=env)
        assert reported.returncode == 0, reported.stderr
        assert reported.stdout.strip() == "1.1.0"

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

    config = tmp_path / "isolated" / "config.json"
    before_demo = _tree(home) | _tree(work)
    demo_env = dict(env)
    demo_env["http_proxy"] = "http://127.0.0.1:9"
    demo_env["https_proxy"] = "http://127.0.0.1:9"
    demo = _json_ok(_run([str(bindir / "ac"), "demo"], cwd=work, env=demo_env))
    assert demo["synthetic"] is True
    assert demo["publisher_id"] == "pub_sample"
    assert [item["candidate_id"] for item in demo["results"]] == [MODEL]
    assert demo["results"][0]["status"] == "ok"
    assert demo["results"][0]["metrics"] == {"cost": "0.0177", "currency": "USD"}
    assert _tree(home) | _tree(work) == before_demo
    assert not config.exists()
    assert list(tmp_path.rglob("*.sqlite3")) == []

    setup_result = _run([str(bindir / "ac"), "setup", "--config", str(config)], cwd=work, env=env)
    if setup_result.returncode != 0:
        if config.is_file():
            try:
                written = json.loads(config.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                written = {}
            _remember(written.get("admin_token") or "", written.get("read_token") or "")
        detail = _redact(setup_result.stdout + setup_result.stderr)
        raise AssertionError(f"setup failed with {setup_result.returncode}: {detail[-2000:]}")
    setup = json.loads(setup_result.stdout)
    assert setup["status"] == "ok"
    assert setup["created"] is True
    assert setup["credentials"] == {"admin": "set", "read": "set"}
    assert setup["coverage"]["prices"] == 0
    assert setup["coverage"]["agents"] == 0
    assert setup["coverage"]["model_efforts"] == 0
    assert setup["coverage"]["empty"] is True
    stored = json.loads(config.read_text(encoding="utf-8"))
    admin = stored["admin_token"]
    read = stored["read_token"]
    admin_digest = _digest(admin)
    read_digest = _digest(read)
    _remember(admin, read, REPLACED_ADMIN)
    _reject_credentials(setup_result.stdout + setup_result.stderr)
    db = Path(setup["db"])
    assert db.is_file()
    assert db.resolve() != ROOT.resolve()
    original_config = config.read_bytes()
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE sentinel (value INTEGER)")
        connection.execute("INSERT INTO sentinel VALUES (7)")
    repeated = _json_ok(_run([str(bindir / "ac"), "setup", "--config", str(config)], cwd=work, env=env))
    assert repeated["created"] is False
    _assert_bytes_unchanged(config, original_config)
    reopened = json.loads(config.read_text(encoding="utf-8"))
    assert _digest(reopened["admin_token"]) == admin_digest
    assert _digest(reopened["read_token"]) == read_digest
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT value FROM sentinel").fetchone()[0] == 7

    query_config = config.with_name("query.json")
    query_document = dict(stored)
    query_document["admin_token"] = REPLACED_ADMIN
    assert _digest(query_document["read_token"]) == read_digest
    query_config.write_text(json.dumps(query_document), encoding="utf-8")
    os.chmod(query_config, 0o600)

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    log_path = tmp_path / "serve.log"
    proc = None
    with log_path.open("w", encoding="utf-8") as log_handle:
        proc = subprocess.Popen(
            [str(bindir / "ac"), "serve", "--config", str(config), "--port", str(port)],
            cwd=work,
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        _wait_health(base, proc, log_path)
        cmdline = Path(f"/proc/{proc.pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
        assert str(bindir / "ac") in cmdline
        assert str(ROOT) not in cmdline
        _reject_credentials(_log_text(log_path))
        with httpx.Client(base_url=base, timeout=5, trust_env=False) as client:
            schema = client.get("/openapi.json")
            assert schema.status_code == 200
            paths = set(schema.json()["paths"])
            assert FROZEN_PATHS <= paths
            assert all("research-job" not in path for path in paths)
            info = schema.json()["info"]
            assert info["version"] == "1.1.0"
        with _bearer_scope(admin):
            created = _http_json(
                "POST",
                f"{base}/v1/contributions",
                synthetic_contribution(),
                "install-smoke",
            )
            published = _http_json(
                "POST",
                f"{base}/v1/contributions/{created['contribution_id']}/publish",
            )
            priced = _http_json(
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
                            "provider": PROVIDER,
                            "channel": "api",
                            "model": MODEL,
                            "plan": "payg",
                            "feature_scope": "text",
                        }
                    ],
                },
            )
            _http_json(
                "PUT",
                f"{base}/v1/capabilities/agents",
                {
                    "agent_id": AGENT_ID,
                    "expected_version": 0,
                    "source": "user_observation",
                    "as_of": AS_OF,
                    "domain": None,
                    "strengths": "installed synthetic agent",
                    "unsuitable": None,
                    "can_edit_files": None,
                    "can_use_tools": None,
                    "input_output_shape": None,
                },
            )
            _http_json(
                "PUT",
                f"{base}/v1/capabilities/model-efforts",
                {
                    "provider": PROVIDER,
                    "model": MODEL,
                    "effort": None,
                    "expected_version": 0,
                    "source": "user_observation",
                    "as_of": AS_OF,
                    "suitable": "null tier",
                    "unsuitable": None,
                    "context_length": None,
                    "benchmarks": None,
                    "default_for_agents": [AGENT_ID],
                },
            )
            _http_json(
                "PUT",
                f"{base}/v1/capabilities/model-efforts",
                {
                    "provider": PROVIDER,
                    "model": MODEL,
                    "effort": "low",
                    "expected_version": 0,
                    "source": "user_observation",
                    "as_of": AS_OF,
                    "suitable": "low tier",
                    "unsuitable": None,
                    "context_length": None,
                    "benchmarks": None,
                    "default_for_agents": None,
                },
            )
        assert priced["results"][0]["status"] == "ok"
        assert priced["formula_version"] == "ac-formulas-v2"

        catalog = _http_json("GET", f"{base}/v1/catalog")
        prices = _json_ok(
            _run(
                [
                    str(bindir / "ac"),
                    "query",
                    "prices",
                    "--server",
                    base,
                    "--config",
                    str(query_config),
                    "--provider",
                    PROVIDER,
                    "--model",
                    MODEL,
                ],
                cwd=work,
                env=env,
            )
        )
        expected_records = [
            row for row in catalog["records"] if row.get("provider") == PROVIDER and row.get("model") == MODEL
        ]
        assert len(expected_records) == 1
        assert prices["records"] == expected_records
        assert prices["data_version"] == catalog["data_version"]
        assert prices["snapshot_id"] == catalog["snapshot_id"]
        assert prices["published_at"] == catalog["published_at"]
        assert prices["formula_version"] == catalog["formula_version"]
        assert prices["content_sha256"] == catalog["content_sha256"]
        assert prices["records"][0]["effort"] is None
        assert prices["records"][0]["sources"][0]["kind"] == "synthetic_fixture"
        assert prices["records"][0]["sources"][0]["retrieved_at"] == PRICE_RETRIEVED_AT

        with _bearer_scope(read):
            service_agent = _http_json("GET", f"{base}/v1/capabilities/agents?agent_id={AGENT_ID}")
        agents = _json_ok(
            _run(
                [
                    str(bindir / "ac"),
                    "query",
                    "agents",
                    "--server",
                    base,
                    "--config",
                    str(query_config),
                    "--agent-id",
                    AGENT_ID,
                ],
                cwd=work,
                env=env,
            )
        )
        assert agents == service_agent
        assert agents["agent_id"] == AGENT_ID
        assert agents["source"] == "user_observation"
        assert agents["as_of"] == AS_OF
        assert agents["domain"] is None
        assert agents["row_version"] == service_agent["row_version"]

        with _bearer_scope(read):
            service_efforts = _http_json("GET", f"{base}/v1/capabilities")
        listed_efforts = [
            row
            for row in service_efforts["model_efforts"]
            if row.get("provider") == PROVIDER and row.get("model") == MODEL
        ]
        efforts = _json_ok(
            _run(
                [
                    str(bindir / "ac"),
                    "query",
                    "model-efforts",
                    "--server",
                    base,
                    "--config",
                    str(query_config),
                    "--provider",
                    PROVIDER,
                    "--model",
                    MODEL,
                ],
                cwd=work,
                env=env,
            )
        )
        assert efforts == {"model_efforts": listed_efforts}
        assert {row["effort"] for row in efforts["model_efforts"]} == {None, "low"}
        null_tier = _json_ok(
            _run(
                [
                    str(bindir / "ac"),
                    "query",
                    "model-efforts",
                    "--server",
                    base,
                    "--config",
                    str(query_config),
                    "--provider",
                    PROVIDER,
                    "--model",
                    MODEL,
                    "--effort",
                    "",
                ],
                cwd=work,
                env=env,
            )
        )
        with _bearer_scope(read):
            service_null = _http_json(
                "GET",
                f"{base}/v1/capabilities/model-efforts?provider={PROVIDER}&model={MODEL}&effort=",
            )
        assert null_tier == service_null
        assert null_tier["effort"] is None
        assert null_tier["suitable"] == "null tier"
        assert null_tier["source"] == "user_observation"
        assert null_tier["as_of"] == AS_OF
        assert null_tier["row_version"] == service_null["row_version"]
        assert "model_efforts" not in null_tier

        online_request = {
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
                    "candidate_id": "installed",
                    "provider": PROVIDER,
                    "channel": "api",
                    "model": MODEL,
                    "effort": "",
                    "plan": "payg",
                    "feature_scope": "text",
                    "agent_id": AGENT_ID,
                }
            ],
        }
        request_path = work / "online-estimate.json"
        request_path.write_text(json.dumps(online_request), encoding="utf-8")
        online = _json_ok(
            _run(
                [
                    str(bindir / "ac"),
                    "estimate",
                    "--server",
                    base,
                    "--config",
                    str(query_config),
                    "--request",
                    str(request_path),
                ],
                cwd=work,
                env=env,
            )
        )
        online_result = online["results"][0]
        assert online_result["status"] == "ok"
        assert online_result["metrics"] == {"cost": "0.0177", "currency": "USD"}
        assert online["data_version"] == catalog["data_version"]
        assert online["formula_version"] == "ac-formulas-v2"
        attached = online_result["capabilities"]
        assert attached["status"] == "ok"
        assert attached["agent"]["agent_id"] == AGENT_ID
        assert attached["agent"]["source"] == "user_observation"
        assert attached["agent"]["as_of"] == AS_OF
        assert attached["agent"]["row_version"] == service_agent["row_version"]
        assert attached["model_effort"]["effort"] is None
        assert attached["model_effort"]["suitable"] == "null tier"
        assert attached["model_effort"]["source"] == "user_observation"
        assert attached["model_effort"]["as_of"] == AS_OF
        assert attached["model_effort"]["row_version"] == service_null["row_version"]

        doctor = _json_ok(
            _run(
                [
                    str(bindir / "ac"),
                    "doctor",
                    "--config",
                    str(config),
                    "--server",
                    base,
                ],
                cwd=work,
                env=env,
            )
        )
        assert doctor["status"] == "ok"
        assert doctor["problems"] == []
        assert doctor["checks"]["health"] == "ok"
        assert doctor["checks"]["schema"] == "ok"
        assert doctor["checks"]["auth"]["admin"] == "ok"
        assert doctor["checks"]["auth"]["read"] == "ok"
        assert doctor["coverage"] == {
            "prices": 1,
            "agents": 1,
            "model_efforts": 2,
            "empty": False,
        }
        _interrupt(proc, required=True)
        proc = None
    finally:
        if proc is not None and proc.poll() is None:
            _interrupt(proc, required=False)

    _reject_credentials(_log_text(log_path))
    stopped_before = _fingerprint(db)
    config_before_stopped = config.read_bytes()
    stopped = _run(
        [str(bindir / "ac"), "doctor", "--config", str(config), "--server", base],
        cwd=work,
        env=env,
    )
    assert stopped.returncode != 0
    _reject_credentials(stopped.stdout + stopped.stderr)
    assert "Traceback" not in stopped.stdout + stopped.stderr
    stopped_body = json.loads(stopped.stdout)
    assert stopped_body["status"] == "transport"
    assert _fingerprint(db) == stopped_before
    _assert_bytes_unchanged(config, config_before_stopped)
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT value FROM sentinel").fetchone()[0] == 7

    preserved = _json_ok(_run([str(bindir / "ac"), "setup", "--config", str(config)], cwd=work, env=env))
    assert preserved["created"] is False
    _assert_bytes_unchanged(config, original_config)
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT value FROM sentinel").fetchone()[0] == 7

    migrated = _run([str(bindir / "agent-costbook-migrate"), "--db", str(db)], cwd=work, env=env)
    assert migrated.returncode == 0, migrated.stderr
    exported = subprocess.run(
        [str(bindir / "agent-costbook-export"), "--db", str(db)],
        cwd=work,
        env=env,
        capture_output=True,
        check=False,
    )
    assert exported.returncode == 0, exported.stderr
    document = json.loads(exported.stdout)
    assert document["schema_version"] == 1
    assert document["formula_version"] == "ac-formulas-v2"
    assert document["data_version"] == 1
    assert any(row["model"] == MODEL and row["provider"] == PROVIDER for row in document["records"])
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
                        "provider": PROVIDER,
                        "channel": "api",
                        "model": MODEL,
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
        cwd=work,
        env=env,
        capture_output=True,
        check=False,
    )
    assert offline.returncode == 0, offline.stderr
    assert json.loads(offline.stdout)["results"][0]["status"] == "ok"
    saved = tmp_path / "saved.sqlite3"
    backed_up = _run(
        [
            str(bindir / "agent-costbook-backup"),
            "--source",
            str(db),
            "--destination",
            str(saved),
        ],
        cwd=work,
        env=env,
    )
    assert backed_up.returncode == 0, backed_up.stderr
    restored = tmp_path / "restored.sqlite3"
    restore = _run(
        [
            str(bindir / "agent-costbook-backup"),
            "--restore",
            "--source",
            str(saved),
            "--destination",
            str(restored),
        ],
        cwd=work,
        env=env,
    )
    assert restore.returncode == 0, restore.stderr
    restored_export = subprocess.run(
        [str(bindir / "agent-costbook-export"), "--db", str(restored)],
        cwd=work,
        env=env,
        capture_output=True,
        check=False,
    )
    assert restored_export.stdout == exported.stdout
