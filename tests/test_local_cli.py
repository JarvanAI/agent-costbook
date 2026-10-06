"""Behavior tests for the local setup, serve, doctor, and demo commands."""

import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tests" / "fixtures" / "ac-v0.2-published-snapshot.json"
REQUEST = ROOT / "examples" / "estimate-request.json"
SECRET_ADMIN = "sekret-admin-value"
SECRET_READ = "sekret-read-value"


def _env(home: Path, extra: dict | None = None) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("ACB_") or key.startswith("XDG_"):
            env.pop(key)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / "xdg-config")
    env["XDG_DATA_HOME"] = str(home / "xdg-data")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra:
        env.update(extra)
    return env


def _paths(home: Path) -> tuple[Path, Path, Path]:
    work = home / "work"
    work.mkdir(parents=True, exist_ok=True)
    config = home / "xdg-config" / "agent-costbook" / "config.json"
    database = home / "xdg-data" / "agent-costbook" / "costbook.sqlite3"
    return work, config, database


def _run(
    home: Path,
    args: list[str],
    *,
    cwd: Path | None = None,
    extra: dict | None = None,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    work, _, _ = _paths(home)
    return subprocess.run(
        [sys.executable, "-m", "agent_costbook.offline", *args],
        cwd=cwd or work,
        env=_env(home, extra),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _json(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.stdout, result.stderr
    return json.loads(result.stdout)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _free_port() -> int:
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_health(port: int, proc: subprocess.Popen[str], output: list[str]) -> None:
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(0.05)
    joined = "".join(output)
    raise AssertionError(f"service did not become healthy\n{joined}")


def _serve(home: Path, port: int, args: list[str] | None = None, extra: dict | None = None):
    work, _, _ = _paths(home)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agent_costbook.offline",
            "serve",
            "--port",
            str(port),
            *(args or []),
        ],
        cwd=work,
        env=_env(home, extra),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    output: list[str] = []

    def _drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            output.append(line)

    import threading

    thread = threading.Thread(target=_drain, daemon=True)
    thread.start()
    try:
        _wait_health(port, proc, output)
        yield proc, output
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
        thread.join(timeout=2)


class _serving:
    def __init__(self, home: Path, port: int, args: list[str] | None = None, extra: dict | None = None):
        self._iterator = _serve(home, port, args, extra)

    def __enter__(self):
        return next(self._iterator)

    def __exit__(self, exc_type, exc, tb):
        try:
            next(self._iterator)
        except StopIteration:
            return False
        return False


def test_version_flag_and_command(tmp_path):
    flagged = _run(tmp_path / "home", ["--version"])
    named = _run(tmp_path / "home", ["version"])
    assert flagged.returncode == 0, flagged.stderr
    assert named.returncode == 0, named.stderr
    assert flagged.stdout.strip() == "1.1.0"
    assert named.stdout.strip() == "1.1.0"
    import agent_costbook

    assert agent_costbook.__version__ == "1.1.0"


def test_setup_repeat_keeps_original_config_bytes_and_database_rows(tmp_path):
    home = tmp_path / "home"
    _, config, database = _paths(home)
    first = _run(home, ["setup"])
    assert first.returncode == 0, first.stderr
    body = _json(first)
    assert body["status"] == "ok"
    assert body["created"] is True
    assert body["coverage"]["empty"] is True
    assert body["coverage"]["prices"] == 0
    assert body["credentials"] == {"admin": "set", "read": "set"}
    stored = json.loads(config.read_text(encoding="utf-8"))
    assert stored["version"] == 1
    assert stored["server"] == "http://127.0.0.1:8080"
    assert Path(stored["db_path"]).is_absolute()
    assert stored["db_path"] == str(database.resolve())
    assert stored["admin_token"] != stored["read_token"]
    assert stored["admin_token"] not in first.stdout + first.stderr
    assert stored["read_token"] not in first.stdout + first.stderr
    original = config.read_bytes()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sentinel (value INTEGER)")
        connection.execute("INSERT INTO sentinel VALUES (7)")
    second = _run(home, ["setup"])
    assert second.returncode == 0, second.stderr
    assert config.read_bytes() == original
    again = _json(second)
    assert again["created"] is False
    assert again["coverage"]["empty"] is True
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM sentinel").fetchone()[0] == 7


def test_setup_config_permissions_are_private(tmp_path):
    home = tmp_path / "home"
    _, config, database = _paths(home)
    result = _run(home, ["setup"])
    assert result.returncode == 0, result.stderr
    assert _mode(config) == 0o600
    assert _mode(config.parent) == 0o700
    assert _mode(database) == 0o600
    assert _mode(database.parent) == 0o700
    assert _mode(config) & 0o077 == 0


def test_setup_preserves_existing_parent_directory_modes(tmp_path):
    home = tmp_path / "home"
    parent = home / "shared"
    database_dir = home / "db-shared"
    parent.mkdir(parents=True)
    database_dir.mkdir()
    os.chmod(parent, 0o775)
    os.chmod(database_dir, 0o775)
    config = parent / "ac.json"
    database = database_dir / "costbook.sqlite3"
    result = _run(home, ["setup", "--config", str(config), "--db", str(database)])
    assert result.returncode == 0, result.stderr
    assert _mode(parent) == 0o775
    assert _mode(database_dir) == 0o775
    assert _mode(config) == 0o600
    assert _mode(database) == 0o600
    assert _run(home, ["setup", "--config", str(config)]).returncode == 0
    assert _mode(parent) == 0o775
    assert _mode(database_dir) == 0o775

    app = home / "xdg-config" / "agent-costbook"
    data = home / "xdg-data" / "agent-costbook"
    app.mkdir(parents=True)
    data.mkdir(parents=True)
    os.chmod(app, 0o755)
    os.chmod(data, 0o755)
    default = _run(home, ["setup"])
    assert default.returncode == 0, default.stderr
    assert _mode(app) == 0o755
    assert _mode(data) == 0o755
    assert _mode(app / "config.json") == 0o600
    assert _mode(data / "costbook.sqlite3") == 0o600


def test_setup_refuses_to_overwrite_bogus_config(tmp_path):
    home = tmp_path / "home"
    _, config, database = _paths(home)
    config.parent.mkdir(parents=True)
    payload = {
        "version": 2,
        "db_path": "/tmp/not-used.sqlite3",
        "admin_token": SECRET_ADMIN,
        "read_token": SECRET_READ,
        "server": "http://127.0.0.1:8080",
    }
    config.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(config, 0o644)
    original = config.read_bytes()
    result = _run(home, ["setup"])
    assert result.returncode == 3
    assert config.read_bytes() == original
    assert _mode(config) == 0o644
    assert not database.exists()
    combined = result.stdout + result.stderr
    assert SECRET_ADMIN not in combined
    assert SECRET_READ not in combined
    assert "Traceback" not in combined
    body = _json(result)
    assert body["status"] == "config_invalid"
    assert body["problems"][0]["repair"]


def test_setup_refuses_malformed_json_without_rewriting(tmp_path):
    home = tmp_path / "home"
    _, config, database = _paths(home)
    config.parent.mkdir(parents=True)
    config.write_bytes(b"{not-json " + SECRET_ADMIN.encode() + b"\n")
    original = config.read_bytes()
    result = _run(home, ["setup"])
    assert result.returncode == 3
    assert config.read_bytes() == original
    assert not database.exists()
    assert SECRET_ADMIN not in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr


def test_setup_rejects_invalid_config_shapes_without_traceback(tmp_path):
    home = tmp_path / "home"
    _, config, database = _paths(home)
    config.parent.mkdir(parents=True)
    database_path = str((home / "db.sqlite3").resolve())
    same = {
        "version": 1,
        "db_path": database_path,
        "admin_token": SECRET_ADMIN,
        "read_token": SECRET_ADMIN,
        "server": "http://127.0.0.1:8080",
    }
    shapes = [
        b"\xff\xfe" + SECRET_ADMIN.encode(),
        json.dumps({**same, "read_token": SECRET_READ, "server": 8080}).encode(),
        json.dumps({**same, "read_token": SECRET_READ, "server": {"url": "http://127.0.0.1:8080"}}).encode(),
        json.dumps(same).encode(),
        json.dumps(["raw", SECRET_ADMIN]).encode(),
    ]
    for payload in shapes:
        config.write_bytes(payload)
        os.chmod(config, 0o644)
        original = config.read_bytes()
        result = _run(home, ["setup"])
        combined = result.stdout + result.stderr
        assert result.returncode == 3, combined
        assert config.read_bytes() == original
        assert _mode(config) == 0o644
        assert not database.exists()
        assert "Traceback" not in combined
        assert SECRET_ADMIN not in combined
        assert _json(result)["status"] == "config_invalid"


def test_setup_io_and_store_errors_are_coded(tmp_path):
    home = tmp_path / "home"
    blocked = home / "not-a-directory"
    blocked.parent.mkdir(parents=True)
    blocked.write_text("keep")
    blocked_mode = _mode(blocked)
    result = _run(home, ["setup", "--config", str(blocked / "ac.json")])
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Traceback" not in combined
    assert _json(result)["status"] == "io"
    assert blocked.read_text() == "keep"
    assert _mode(blocked) == blocked_mode

    home_ready = tmp_path / "ready"
    assert _run(home_ready, ["setup"]).returncode == 0
    _, config, database = _paths(home_ready)
    config_bytes = config.read_bytes()
    database_bytes = database.read_bytes()
    os.chmod(database, 0)
    try:
        failed = _run(home_ready, ["setup"])
    finally:
        os.chmod(database, 0o600)
    failed_text = failed.stdout + failed.stderr
    assert failed.returncode != 0
    assert "Traceback" not in failed_text
    assert _json(failed)["status"] in {"io", "db_invalid"}
    assert config.read_bytes() == config_bytes
    assert database.read_bytes() == database_bytes
    assert json.loads(config_bytes)["admin_token"] not in failed_text


def test_setup_db_override_does_not_rewrite_existing_config(tmp_path):
    home = tmp_path / "home"
    _, config, database = _paths(home)
    assert _run(home, ["setup"]).returncode == 0
    original = config.read_bytes()
    database_bytes = database.read_bytes()
    other = home / "other.sqlite3"
    result = _run(home, ["setup", "--db", str(other)])
    assert result.returncode != 0
    body = _json(result)
    assert body["status"] == "db_mismatch"
    assert body["problems"][0]["repair"]
    assert config.read_bytes() == original
    assert database.read_bytes() == database_bytes
    assert not other.exists()
    stored = json.loads(original)
    assert stored["admin_token"] not in result.stdout + result.stderr


def test_setup_paths_are_independent_of_cwd(tmp_path):
    home = tmp_path / "home"
    first = home / "dir-a"
    second = home / "dir-b"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    created = _run(home, ["setup"], cwd=first)
    assert created.returncode == 0, created.stderr
    _, config, database = _paths(home)
    assert config.is_file()
    assert database.is_file()
    assert str(first) not in str(database)
    assert str(second) not in str(config)
    original = config.read_bytes()
    repeated = _run(home, ["setup"], cwd=second)
    assert repeated.returncode == 0, repeated.stderr
    assert config.read_bytes() == original
    assert not any(first.iterdir())
    assert not any(second.iterdir())


def test_explicit_config_uses_sibling_database(tmp_path):
    home = tmp_path / "home"
    custom = home / "custom" / "ac.json"
    result = _run(home, ["setup", "--config", str(custom)])
    assert result.returncode == 0, result.stderr
    body = _json(result)
    stored = json.loads(custom.read_text(encoding="utf-8"))
    expected = custom.parent / "costbook.sqlite3"
    assert Path(stored["db_path"]) == expected.resolve()
    assert Path(body["db"]) == expected.resolve()
    assert expected.is_file()
    assert not (home / "xdg-data").exists()
    assert _mode(custom) == 0o600
    assert _mode(custom.parent) == 0o700


def test_setup_race_publishes_one_complete_config(tmp_path):
    home = tmp_path / "home"
    work, config, _ = _paths(home)
    processes = [
        subprocess.Popen(
            [sys.executable, "-m", "agent_costbook.offline", "setup"],
            cwd=work,
            env=_env(home),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(5)
    ]
    codes = [process.wait(timeout=30) for process in processes]
    assert codes == [0, 0, 0, 0, 0]
    raw = config.read_bytes()
    stored = json.loads(raw)
    assert stored["version"] == 1
    assert Path(stored["db_path"]).is_absolute()
    assert len(stored["admin_token"]) >= 16
    assert len(stored["read_token"]) >= 16
    assert _mode(config) == 0o600


def test_demo_is_synthetic_and_does_not_touch_files_or_config(tmp_path):
    home = tmp_path / "home"
    work, config, database = _paths(home)
    before = {path.relative_to(home) for path in home.rglob("*")}
    result = _run(
        home,
        ["demo"],
        cwd=work,
        extra={
            "ACB_CONFIG": str(home / "missing-config.json"),
            "ACB_ADMIN_TOKEN": SECRET_ADMIN,
            "http_proxy": "http://127.0.0.1:9",
            "https_proxy": "http://127.0.0.1:9",
        },
    )
    assert result.returncode == 0, result.stderr
    body = _json(result)
    from importlib.resources import files

    from agent_costbook.models import EstimateIn
    from agent_costbook.offline import estimate_snapshot, load_snapshot

    package = files("agent_costbook.demo")
    snapshot = package.joinpath("snapshot.json").read_bytes()
    request = package.joinpath("request.json").read_bytes()
    expected = estimate_snapshot(
        load_snapshot(snapshot, expected_publisher="pub_sample"),
        EstimateIn.model_validate_json(request),
    )
    expected["synthetic"] = True
    assert body["synthetic"] is True
    assert body["publisher_id"] == "pub_sample"
    assert body["content_sha256"] == expected["content_sha256"]
    assert body["data_version"] == expected["data_version"]
    assert body["formula_version"] == expected["formula_version"]
    assert [item["candidate_id"] for item in body["results"]] == ["synthetic-m4"]
    assert body["results"][0]["metrics"] == {"cost": "0.0177", "currency": "USD"}
    assert "result" not in body
    assert body == expected
    assert SECRET_ADMIN not in result.stdout + result.stderr
    after = {path.relative_to(home) for path in home.rglob("*")}
    assert after == before
    assert not config.exists()
    assert not database.exists()


def test_doctor_missing_config_does_not_create_database(tmp_path):
    home = tmp_path / "home"
    _, _, database = _paths(home)
    result = _run(home, ["doctor"])
    assert result.returncode == 2
    body = _json(result)
    assert body["status"] == "config_missing"
    assert "setup" in body["problems"][0]["repair"]
    assert not database.exists()
    assert not (home / "xdg-data").exists()
    assert "Traceback" not in result.stdout + result.stderr


def test_doctor_missing_database_does_not_create_one(tmp_path):
    home = tmp_path / "home"
    _, config, database = _paths(home)
    assert _run(home, ["setup"]).returncode == 0
    original = config.read_bytes()
    database.unlink()
    result = _run(home, ["doctor"])
    assert result.returncode == 4
    body = _json(result)
    assert body["status"] == "db_missing"
    assert "setup" in body["problems"][0]["repair"]
    assert not database.exists()
    assert config.read_bytes() == original


def test_doctor_does_not_write_the_database(tmp_path):
    home = tmp_path / "home"
    _, config, database = _paths(home)
    assert _run(home, ["setup"]).returncode == 0
    port = _free_port()
    original_config = config.read_bytes()
    original_db = database.read_bytes()
    result = _run(home, ["doctor", "--server", f"http://127.0.0.1:{port}"])
    assert result.returncode == 6
    body = _json(result)
    assert body["status"] == "transport"
    assert body["coverage"]["empty"] is True
    assert body["coverage"]["prices"] == 0
    assert body["service_coverage"] is None
    assert "ConnectionRefusedError" not in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    assert config.read_bytes() == original_config
    assert database.read_bytes() == original_db
    assert not database.with_name(database.name + "-wal").exists()


def test_doctor_invalid_schema_is_distinct_and_does_not_migrate(tmp_path):
    home = tmp_path / "home"
    _, _, database = _paths(home)
    assert _run(home, ["setup"]).returncode == 0
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 99")
    original = database.read_bytes()
    result = _run(home, ["doctor", "--server", "http://127.0.0.1:9"])
    assert result.returncode == 5
    body = _json(result)
    assert body["status"] == "db_invalid"
    assert body["problems"][0]["code"] == "future_schema"
    assert database.read_bytes() == original
    assert "Traceback" not in result.stdout + result.stderr


def test_doctor_empty_catalog_is_not_a_failure(tmp_path):
    home = tmp_path / "home"
    _, config, database = _paths(home)
    assert _run(home, ["setup"]).returncode == 0
    stored = json.loads(config.read_text(encoding="utf-8"))
    port = _free_port()
    with _serving(home, port):
        original = database.read_bytes()
        result = _run(home, ["doctor", "--server", f"http://127.0.0.1:{port}"])
        assert database.read_bytes() == original
    assert result.returncode == 0, result.stderr
    body = _json(result)
    assert body["status"] == "ok"
    assert body["problems"] == []
    assert body["coverage"] == {
        "prices": 0,
        "agents": 0,
        "model_efforts": 0,
        "empty": True,
    }
    assert body["notes"] == ["The catalog is empty."]
    assert body["service_coverage"] == {
        "prices": 0,
        "agents": 0,
        "model_efforts": 0,
    }
    assert body["checks"]["health"] == "ok"
    assert body["checks"]["schema"] == "ok"
    assert body["checks"]["auth"]["admin"] == "ok"
    assert body["checks"]["auth"]["read"] == "ok"
    combined = result.stdout + result.stderr
    assert stored["admin_token"] not in combined
    assert stored["read_token"] not in combined


def test_doctor_rejects_bad_admin_credential(tmp_path):
    home = tmp_path / "home"
    _, config, _ = _paths(home)
    assert _run(home, ["setup"]).returncode == 0
    stored = json.loads(config.read_text(encoding="utf-8"))
    port = _free_port()
    with _serving(home, port):
        result = _run(
            home,
            ["doctor", "--server", f"http://127.0.0.1:{port}"],
            extra={"ACB_ADMIN_TOKEN": "wrong-admin-value"},
        )
    assert result.returncode == 7
    body = _json(result)
    assert body["status"] == "auth"
    assert body["problems"]
    assert body["checks"]["auth"]["admin"] == "rejected"
    assert body["checks"]["auth"]["read"] == "ok"
    assert body["coverage"] == {
        "prices": 0,
        "agents": 0,
        "model_efforts": 0,
        "empty": True,
    }
    assert body["service_coverage"] == {
        "prices": 0,
        "agents": 0,
        "model_efforts": 0,
    }
    repair = body["problems"][0]["repair"]
    assert "admin" in repair
    assert "ACB_ADMIN_TOKEN" in repair
    assert "setup" not in repair.lower()
    combined = result.stdout + result.stderr
    assert "wrong-admin-value" not in combined
    assert stored["admin_token"] not in combined
    assert stored["read_token"] not in combined


def test_doctor_rejects_wrong_read_token_on_a_live_service(tmp_path):
    home = tmp_path / "home"
    _, config, database = _paths(home)
    assert _run(home, ["setup"]).returncode == 0
    stored = json.loads(config.read_text(encoding="utf-8"))
    original_config = config.read_bytes()
    bad = tmp_path / "bad-read-config.json"
    bad.write_text(
        json.dumps({**stored, "read_token": "wrong-read-value"}),
        encoding="utf-8",
    )
    bad_bytes = bad.read_bytes()
    port = _free_port()
    with _serving(home, port):
        original_db = database.read_bytes()
        result = _run(
            home,
            ["doctor", "--config", str(bad), "--server", f"http://127.0.0.1:{port}"],
        )
        assert database.read_bytes() == original_db
    assert config.read_bytes() == original_config
    assert bad.read_bytes() == bad_bytes
    assert result.returncode == 7, result.stdout + result.stderr
    body = _json(result)
    assert body["status"] == "auth"
    assert body["problems"]
    assert body["checks"]["auth"]["admin"] == "ok"
    assert body["checks"]["auth"]["read"] == "rejected"
    assert body["coverage"]["empty"] is True
    assert body["coverage"]["prices"] == 0
    assert body["service_coverage"] == {
        "prices": 0,
        "agents": 0,
        "model_efforts": 0,
    }
    repair = body["problems"][0]["repair"]
    assert "read" in repair
    assert "ACB_READ_TOKEN" in repair
    assert "setup" not in repair.lower()
    combined = result.stdout + result.stderr
    assert "wrong-read-value" not in combined
    assert stored["admin_token"] not in combined
    assert stored["read_token"] not in combined
    assert "Traceback" not in combined


def test_doctor_rejects_missing_read_credential_on_a_live_service(tmp_path):
    home = tmp_path / "home"
    _, config, database = _paths(home)
    assert _run(home, ["setup"]).returncode == 0
    stored = json.loads(config.read_text(encoding="utf-8"))
    original_config = config.read_bytes()
    port = _free_port()
    with _serving(home, port):
        original_db = database.read_bytes()
        result = _run(
            home,
            ["doctor", "--server", f"http://127.0.0.1:{port}"],
            extra={"ACB_READ_TOKEN": ""},
        )
        assert database.read_bytes() == original_db
    assert config.read_bytes() == original_config
    assert result.returncode == 7, result.stdout + result.stderr
    body = _json(result)
    assert body["status"] == "auth"
    assert body["problems"]
    assert body["checks"]["auth"]["admin"] == "ok"
    assert body["checks"]["auth"]["read"] == "missing"
    assert body["coverage"]["empty"] is True
    assert body["coverage"]["agents"] == 0
    assert body["service_coverage"] == {
        "prices": 0,
        "agents": 0,
        "model_efforts": 0,
    }
    repair = body["problems"][0]["repair"]
    assert "read" in repair
    assert "missing" in repair
    assert "ACB_READ_TOKEN" in repair
    assert "setup" not in repair.lower()
    combined = result.stdout + result.stderr
    assert stored["admin_token"] not in combined
    assert stored["read_token"] not in combined
    assert "Traceback" not in combined


def test_doctor_transport_when_read_probe_cannot_be_verified(tmp_path):
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    home = tmp_path / "home"
    _, config, database = _paths(home)
    assert _run(home, ["setup"]).returncode == 0
    stored = json.loads(config.read_text(encoding="utf-8"))
    original_config = config.read_bytes()
    original_db = database.read_bytes()
    port = _free_port()

    def _http_json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                _http_json(self, 200, {"status": "ok"})
                return
            if self.path == "/v1/catalog":
                _http_json(self, 200, {"records": []})
                return
            if self.path == "/v1/capabilities":
                authorization = self.headers.get("Authorization", "")
                if authorization == f"Bearer {stored['admin_token']}":
                    _http_json(self, 200, {"agents": [], "model_efforts": []})
                    return
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return
            _http_json(self, 404, {})

        def log_message(self, format: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = _run(home, ["doctor", "--server", f"http://127.0.0.1:{port}"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert config.read_bytes() == original_config
    assert database.read_bytes() == original_db
    assert result.returncode == 6, result.stdout + result.stderr
    body = _json(result)
    assert body["status"] == "transport"
    assert body["problems"]
    assert body["checks"]["health"] == "ok"
    assert body["checks"]["auth"]["admin"] == "ok"
    assert body["checks"]["auth"]["read"] == "unchecked"
    assert body["coverage"] == {
        "prices": 0,
        "agents": 0,
        "model_efforts": 0,
        "empty": True,
    }
    assert body["service_coverage"] == {
        "prices": 0,
        "agents": 0,
        "model_efforts": 0,
    }
    repair = body["problems"][0]["repair"]
    assert "read" in repair
    assert "not a rejected" in repair
    assert "setup" not in repair.lower()
    combined = result.stdout + result.stderr
    assert stored["admin_token"] not in combined
    assert stored["read_token"] not in combined
    assert "Traceback" not in combined


def _seed_distinct_service(database: Path) -> None:
    from agent_costbook.store import Store

    from support import synthetic_contribution

    store = Store(database)
    try:
        created, inserted = store.create_contribution(synthetic_contribution(), None)
        assert inserted is True
        store.publish(created["contribution_id"])
        store.save_agent_capability(
            {
                "agent_id": "synthetic-agent",
                "expected_version": 0,
                "source": "user_observation",
                "as_of": "2026-09-25T00:00:00+00:00",
            }
        )
        store.save_model_effort(
            {
                "provider": "example",
                "model": "synthetic-m4",
                "effort": None,
                "expected_version": 0,
                "source": "user_observation",
                "as_of": "2026-09-25T00:00:00+00:00",
            }
        )
    finally:
        store.close()


def _http_json(url: str, token: str | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            body = json.load(response)
            return response.status, body if isinstance(body, dict) else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            body = json.loads(raw.decode()) if raw else {}
        except json.JSONDecodeError:
            body = {}
        return exc.code, body if isinstance(body, dict) else {}


def test_doctor_keeps_local_coverage_apart_from_another_service(tmp_path):
    home_a = tmp_path / "home-a"
    home_b = tmp_path / "home-b"
    _, config_a, database_a = _paths(home_a)
    _, config_b, database_b = _paths(home_b)
    assert _run(home_a, ["setup"]).returncode == 0
    assert _run(home_b, ["setup"]).returncode == 0
    _seed_distinct_service(database_b)
    stored_a = json.loads(config_a.read_text(encoding="utf-8"))
    stored_b = json.loads(config_b.read_text(encoding="utf-8"))
    original_config_a = config_a.read_bytes()
    original_config_b = config_b.read_bytes()
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    local_empty = {"prices": 0, "agents": 0, "model_efforts": 0, "empty": True}
    with _serving(home_b, port):
        original_db_a = database_a.read_bytes()
        original_db_b = database_b.read_bytes()
        catalog_status, catalog = _http_json(base + "/v1/catalog")
        cap_status, capabilities = _http_json(base + "/v1/capabilities", stored_b["admin_token"])
        rejected_status, _ = _http_json(base + "/v1/capabilities", stored_a["admin_token"])
        assert catalog_status == 200
        assert isinstance(catalog.get("records"), list)
        assert len(catalog["records"]) == 1
        assert catalog["records"][0]["model"] == "synthetic-m4"
        assert cap_status == 200
        assert len(capabilities["agents"]) == 1
        assert len(capabilities["model_efforts"]) == 1
        assert rejected_status == 401
        verified = {
            "prices": len(catalog["records"]),
            "agents": len(capabilities["agents"]),
            "model_efforts": len(capabilities["model_efforts"]),
        }

        mismatched = _run(home_a, ["doctor", "--config", str(config_a), "--server", base])
        read_only = _run(
            home_a,
            ["doctor", "--config", str(config_a), "--server", base],
            extra={"ACB_READ_TOKEN": stored_b["read_token"]},
        )
        matched = _run(
            home_a,
            ["doctor", "--config", str(config_a), "--server", base],
            extra={
                "ACB_ADMIN_TOKEN": stored_b["admin_token"],
                "ACB_READ_TOKEN": stored_b["read_token"],
            },
        )
        assert database_a.read_bytes() == original_db_a
        assert database_b.read_bytes() == original_db_b
    assert config_a.read_bytes() == original_config_a
    assert config_b.read_bytes() == original_config_b
    assert not database_a.with_name(database_a.name + "-wal").exists()

    mismatched_body = _json(mismatched)
    assert mismatched.returncode == 7
    assert mismatched_body["status"] == "auth"
    assert mismatched_body["paths"]["db"] == str(database_a)
    assert mismatched_body["paths"]["config"] == str(config_a)
    assert mismatched_body["coverage"] == local_empty
    assert mismatched_body["service_coverage"] == {"prices": verified["prices"]}
    assert "agents" not in mismatched_body["service_coverage"]
    assert "model_efforts" not in mismatched_body["service_coverage"]
    assert mismatched_body["checks"]["auth"]["admin"] == "rejected"
    assert mismatched_body["checks"]["auth"]["read"] == "rejected"
    assert "setup" not in mismatched_body["problems"][0]["repair"].lower()

    read_body = _json(read_only)
    assert read_only.returncode == 7
    assert read_body["status"] == "auth"
    assert read_body["coverage"] == local_empty
    assert read_body["service_coverage"] == verified
    assert read_body["checks"]["auth"]["admin"] == "rejected"
    assert read_body["checks"]["auth"]["read"] == "ok"
    assert "admin" in read_body["problems"][0]["repair"]
    assert "ACB_ADMIN_TOKEN" in read_body["problems"][0]["repair"]
    assert "setup" not in read_body["problems"][0]["repair"].lower()

    matched_body = _json(matched)
    assert matched.returncode == 0, matched.stderr
    assert matched_body["status"] == "ok"
    assert matched_body["problems"] == []
    assert matched_body["paths"]["db"] == str(database_a)
    assert matched_body["coverage"] == local_empty
    assert matched_body["notes"] == ["The catalog is empty."]
    assert matched_body["service_coverage"] == verified
    assert matched_body["checks"]["auth"]["admin"] == "ok"
    assert matched_body["checks"]["auth"]["read"] == "ok"

    combined = mismatched.stdout + mismatched.stderr + read_only.stdout + read_only.stderr
    combined += matched.stdout + matched.stderr
    assert stored_a["admin_token"] not in combined
    assert stored_a["read_token"] not in combined
    assert stored_b["admin_token"] not in combined
    assert stored_b["read_token"] not in combined
    assert "Traceback" not in combined


def test_serve_foreground_health_and_termination(tmp_path):
    home = tmp_path / "home"
    _, config, _ = _paths(home)
    assert _run(home, ["setup"]).returncode == 0
    stored = json.loads(config.read_text(encoding="utf-8"))
    port = _free_port()
    work, _, _ = _paths(home)
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_costbook.offline", "serve", "--port", str(port)],
        cwd=work,
        env=_env(home),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    output: list[str] = []

    def _drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            output.append(line)

    import threading

    thread = threading.Thread(target=_drain, daemon=True)
    thread.start()
    try:
        _wait_health(port, proc, output)
        os.killpg(proc.pid, signal.SIGTERM)
        assert proc.wait(timeout=10) is not None
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        thread.join(timeout=2)
    text = "".join(output)
    assert str(port) in text
    assert stored["admin_token"] not in text
    assert stored["read_token"] not in text


def test_serve_port_conflict_does_not_create_files(tmp_path):
    import socket

    home = tmp_path / "home"
    _, config, database = _paths(home)
    port = _free_port()
    held = socket.socket()
    held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    held.bind(("127.0.0.1", port))
    held.listen(1)
    try:
        result = _run(home, ["serve", "--setup", "--port", str(port)], timeout=20)
    finally:
        held.close()
    assert result.returncode == 8
    body = _json(result)
    assert body["status"] == "port_conflict"
    assert "--port" in body["problems"][0]["repair"] or "port" in body["problems"][0]["repair"]
    assert not config.exists()
    assert not database.exists()


def test_serve_missing_config_does_not_open_or_create_database(tmp_path):
    home = tmp_path / "home"
    _, config, database = _paths(home)
    result = _run(home, ["serve", "--port", str(_free_port())], timeout=20)
    assert result.returncode == 2
    body = _json(result)
    assert body["status"] == "config_missing"
    assert "setup" in body["problems"][0]["repair"]
    assert not config.exists()
    assert not database.exists()


def test_serve_missing_database_is_not_created(tmp_path):
    home = tmp_path / "home"
    _, _, database = _paths(home)
    assert _run(home, ["setup"]).returncode == 0
    database.unlink()
    result = _run(home, ["serve", "--port", str(_free_port())], timeout=20)
    assert result.returncode == 4
    body = _json(result)
    assert body["status"] == "db_missing"
    assert not database.exists()


def test_serve_rejects_non_loopback_before_creating_files(tmp_path):
    home = tmp_path / "home"
    _, config, database = _paths(home)
    result = _run(home, ["serve", "--setup", "--host", "0.0.0.0", "--port", str(_free_port())])
    assert result.returncode != 0
    assert not config.exists()
    assert not database.exists()
    assert "Traceback" not in result.stdout + result.stderr


def test_serve_explicit_db_and_admin_env_do_not_need_a_config_file(tmp_path):
    home = tmp_path / "home"
    work, config, _ = _paths(home)
    database = home / "explicit.sqlite3"
    from agent_costbook.store import Store

    Store(database).close()
    os.chmod(database, 0o600)
    port = _free_port()
    with _serving(
        home,
        port,
        ["--db", str(database)],
        {"ACB_ADMIN_TOKEN": "env-admin-token", "ACB_DB": str(database)},
    ):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
            assert response.status == 200
    assert not config.exists()


def test_existing_offline_commands_still_dispatch(tmp_path):
    home = tmp_path / "home"
    estimate = _run(
        home,
        ["estimate", "--snapshot", str(SNAPSHOT), "--request", str(REQUEST)],
        cwd=home / "work",
    )
    assert estimate.returncode == 0, estimate.stderr
    assert "0.0177" in estimate.stdout
    assert _run(home, ["estimate"]).returncode == 2
    assert _run(home, ["collect"]).returncode == 2
    assert _run(home, ["data"]).returncode == 2


def test_load_cli_config_precedence_and_secret_errors(tmp_path, monkeypatch):
    from agent_costbook.local import LocalError, default_config_path, default_data_path, load_cli_config

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ACB_CONFIG", raising=False)
    monkeypatch.delenv("ACB_DB", raising=False)
    monkeypatch.delenv("ACB_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("ACB_READ_TOKEN", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "xdg-data"))
    monkeypatch.setattr("agent_costbook.local.sys.platform", "linux")

    absent = load_cli_config()
    assert absent.exists is False
    assert absent.path == default_config_path()
    assert absent.db_path == default_data_path().resolve()
    assert absent.source_db == "default"
    assert not absent.path.exists()
    assert not absent.db_path.exists()

    custom = home / "pair" / "ac.json"
    explicit = load_cli_config(config_path=str(custom))
    assert explicit.explicit_config is True
    assert explicit.db_path == (custom.parent / "costbook.sqlite3").resolve()
    assert explicit.source_db == "config_dir"
    assert not custom.exists()

    custom.parent.mkdir(parents=True)
    database = (home / "configured.sqlite3").resolve()
    document = {
        "version": 1,
        "db_path": str(database),
        "admin_token": SECRET_ADMIN,
        "read_token": SECRET_READ,
        "server": "http://127.0.0.1:8080",
    }
    custom.write_text(json.dumps(document), encoding="utf-8")
    original = custom.read_bytes()
    monkeypatch.setenv("ACB_DB", str(home / "env.sqlite3"))
    monkeypatch.setenv("ACB_ADMIN_TOKEN", "env-admin")
    monkeypatch.setenv("ACB_READ_TOKEN", "env-read")
    overridden = load_cli_config(config_path=str(custom), db=str(home / "cli.sqlite3"))
    assert overridden.db_path == (home / "cli.sqlite3").resolve()
    assert overridden.configured_db_path == database
    assert overridden.source_db == "cli"
    assert overridden.admin_token == "env-admin"
    assert overridden.read_token == "env-read"
    assert overridden.server == "http://127.0.0.1:8080"
    assert custom.read_bytes() == original
    from_env = load_cli_config(config_path=str(custom))
    assert from_env.db_path == (home / "env.sqlite3").resolve()
    assert from_env.source_db == "env"

    monkeypatch.delenv("ACB_DB")
    monkeypatch.delenv("ACB_ADMIN_TOKEN")
    monkeypatch.delenv("ACB_READ_TOKEN")
    stored = load_cli_config(config_path=str(custom))
    assert stored.db_path == database
    assert stored.admin_token == SECRET_ADMIN
    assert stored.read_token == SECRET_READ
    assert stored.source_db == "config"

    custom.write_text("{bad " + SECRET_ADMIN, encoding="utf-8")
    with pytest.raises(LocalError) as caught:
        load_cli_config(config_path=str(custom))
    assert caught.value.code == "config_invalid"
    assert SECRET_ADMIN not in str(caught.value)
    assert SECRET_ADMIN not in caught.value.repair

    invalid = home / "invalid-shapes"
    invalid.mkdir()
    database = (home / "shape.sqlite3").resolve()
    base = {
        "version": 1,
        "db_path": str(database),
        "admin_token": SECRET_ADMIN,
        "read_token": SECRET_READ,
        "server": "http://127.0.0.1:8080",
    }
    samples = {
        "utf8": b"\xff" + SECRET_ADMIN.encode(),
        "server-number": json.dumps({**base, "server": 8080}).encode(),
        "server-object": json.dumps({**base, "server": {"url": "http://127.0.0.1:8080"}}).encode(),
        "same-token": json.dumps({**base, "read_token": SECRET_ADMIN}).encode(),
        "raw-list": json.dumps(["raw", SECRET_ADMIN]).encode(),
    }
    for name, payload in samples.items():
        path = invalid / f"{name}.json"
        path.write_bytes(payload)
        with pytest.raises(LocalError) as caught:
            load_cli_config(config_path=str(path))
        assert caught.value.code == "config_invalid"
        assert not isinstance(caught.value.__cause__, UnicodeDecodeError)
        assert SECRET_ADMIN not in str(caught.value)
        assert SECRET_ADMIN not in caught.value.repair
        assert path.read_bytes() == payload


def test_default_paths_follow_platform(tmp_path, monkeypatch):
    import agent_costbook.local as local

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(local.sys, "platform", "linux")
    assert local.default_config_path() == tmp_path / ".config" / "agent-costbook" / "config.json"
    assert local.default_data_path() == tmp_path / ".local" / "share" / "agent-costbook" / "costbook.sqlite3"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert local.default_config_path() == tmp_path / "cfg" / "agent-costbook" / "config.json"
    assert local.default_data_path() == tmp_path / "data" / "agent-costbook" / "costbook.sqlite3"

    monkeypatch.setattr(local.sys, "platform", "darwin")
    assert local.default_config_path() == (
        tmp_path / "Library" / "Application Support" / "agent-costbook" / "config.json"
    )
    assert local.default_data_path() == (
        tmp_path / "Library" / "Application Support" / "agent-costbook" / "costbook.sqlite3"
    )

    monkeypatch.setattr(local.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert local.default_config_path() == tmp_path / "Roaming" / "agent-costbook" / "config.json"
    assert local.default_data_path() == tmp_path / "Local" / "agent-costbook" / "costbook.sqlite3"


def test_query_credential_prefers_read_token():
    from agent_costbook.local import CliConfig, query_credential

    config = CliConfig(
        path=Path("/tmp/config.json"),
        exists=True,
        version=1,
        db_path=Path("/tmp/costbook.sqlite3"),
        configured_db_path=Path("/tmp/costbook.sqlite3"),
        admin_token="admin-secret",
        read_token="read-secret",
        server="http://127.0.0.1:8080",
        source_db="config",
        explicit_config=False,
    )
    assert query_credential(config) == "read-secret"
    assert query_credential(
        CliConfig(
            path=config.path,
            exists=True,
            version=1,
            db_path=config.db_path,
            configured_db_path=config.configured_db_path,
            admin_token="admin-secret",
            read_token="",
            server=config.server,
            source_db="config",
            explicit_config=False,
        )
    ) == "admin-secret"
