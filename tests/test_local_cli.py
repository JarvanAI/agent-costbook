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
    assert body["synthetic"] is True
    assert body["currency"] == "USD"
    assert body["publisher_id"] == "pub_sample"
    assert body["result"]["candidate_id"] == "synthetic-m4"
    assert body["result"]["method"] == "M4"
    assert body["result"]["metrics"]["cost"] == "0.0177"
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
    assert body["checks"]["health"] == "ok"
    assert body["checks"]["schema"] == "ok"
    assert body["checks"]["auth"]["admin"] == "ok"
    assert body["checks"]["auth"]["read"] in {"ok", "rejected", "missing", "unchecked"}
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
    assert body["problems"][0]["repair"]
    combined = result.stdout + result.stderr
    assert "wrong-admin-value" not in combined
    assert stored["admin_token"] not in combined
    assert stored["read_token"] not in combined


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
