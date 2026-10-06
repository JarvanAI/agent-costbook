"""Local config, setup, foreground serve, doctor, and the synthetic demo.

Query and online estimate commands should keep using this module instead of
reading environment variables or the config file themselves.

Helper contract for query commands
-----------------------------------
``load_cli_config(config_path=None, db=None)`` resolves one :class:`CliConfig`
and does not create or rewrite files.

* Config path: ``config_path`` argument, then ``ACB_CONFIG``, then
  :func:`default_config_path`. Linux uses ``$XDG_CONFIG_HOME/agent-costbook/config.json``
  (or ``~/.config/...``). macOS uses ``~/Library/Application Support/agent-costbook/config.json``.
  Windows uses ``%APPDATA%/agent-costbook/config.json``.
* Database: ``db`` argument, then ``ACB_DB``, then ``db_path`` in the config
  file, then ``<config parent>/costbook.sqlite3`` when the config path was
  explicit, otherwise :func:`default_data_path`.
* Tokens: ``ACB_ADMIN_TOKEN`` and ``ACB_READ_TOKEN`` override the file in
  memory. Overrides are not written back. There is no token CLI flag.
* ``query_credential(config)`` returns the read token when it is non-empty,
  otherwise the admin token. Never log either value.
* :func:`service_settings` builds ``Settings(db_path, admin_token, read_token)``
  for ``serve``. ``load_settings()`` stays cwd-based and is not used here.

A config file is JSON object version ``1`` with absolute ``db_path``,
distinct ``admin_token`` and ``read_token``, and loopback ``server`` (created
as ``http://127.0.0.1:8080``). Malformed JSON, invalid text, a non-string
server, equal tokens, and any other version raise :class:`LocalError` with
code ``config_invalid``. The repair text does not include file contents or tokens.

Setup creates only missing directories and marks those new directories ``0700``.
An existing parent keeps its mode. The config file and database file are ``0600``.

Exit codes: 1 input/output, 2 config missing, 3 config invalid, 4 database
missing, 5 database invalid, 6 transport, 7 auth, 8 port conflict,
9 database mismatch.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sys
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from agent_costbook import __version__
from agent_costbook.data_batch import DataError, ServiceClient, TransportError, normalize_server
from agent_costbook.settings import Settings
from agent_costbook.store import Store, StoreError

DEFAULT_SERVER = "http://127.0.0.1:8080"
_CONFIG_KEYS = frozenset({"version", "db_path", "admin_token", "read_token", "server"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_HTTP_TIMEOUT = 3.0
_IO_REPAIR = (
    "Setup could not create or open a path. Existing files were left unchanged. "
    "Check that each parent is a writable directory."
)
EXIT_IO = 1
EXIT_CONFIG_MISSING = 2
EXIT_CONFIG_INVALID = 3
EXIT_DB_MISSING = 4
EXIT_DB_INVALID = 5
EXIT_TRANSPORT = 6
EXIT_AUTH = 7
EXIT_PORT = 8
EXIT_DB_MISMATCH = 9


class LocalError(Exception):
    """Sanitized CLI failure. ``code`` and ``repair`` are safe to print."""

    def __init__(self, code: str, repair: str, *, exit_code: int):
        super().__init__(code)
        self.code = code
        self.repair = repair
        self.exit_code = exit_code


@dataclass(frozen=True)
class CliConfig:
    """Resolved local configuration. Token fields are secrets."""

    path: Path
    exists: bool
    version: int | None
    db_path: Path
    configured_db_path: Path | None
    admin_token: str
    read_token: str
    server: str
    source_db: str
    explicit_config: bool


def default_config_path() -> Path:
    """Platform config file. Independent of the current working directory."""

    if sys.platform == "win32":
        root = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(root) / "agent-costbook" / "config.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "agent-costbook" / "config.json"
    root = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(root) / "agent-costbook" / "config.json"


def default_data_path() -> Path:
    """Platform database file. Independent of the current working directory."""

    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(root) / "agent-costbook" / "costbook.sqlite3"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "agent-costbook" / "costbook.sqlite3"
    root = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(root) / "agent-costbook" / "costbook.sqlite3"


def query_credential(config: CliConfig) -> str:
    """Bearer token for query calls. Prefer the read token, then the admin token."""

    return config.read_token or config.admin_token


def service_settings(config: CliConfig) -> Settings:
    """Settings for this process. Does not call the cwd-based factory."""

    return Settings(
        db_path=config.db_path,
        admin_token=config.admin_token,
        read_token=config.read_token,
    )


def load_cli_config(config_path: str | None = None, db: str | None = None) -> CliConfig:
    """Resolve config, database, and tokens without creating files."""

    path, explicit = _config_location(config_path)
    document = None
    exists = path.is_file()
    if exists:
        document = _parse_config_file(path)
    elif path.exists():
        raise _invalid_config(path)
    db_path, source, configured = _resolve_db(path, explicit, document, db)
    return CliConfig(
        path=path,
        exists=exists,
        version=None if document is None else document["version"],
        db_path=db_path,
        configured_db_path=configured,
        admin_token=_token_override("ACB_ADMIN_TOKEN", document, "admin_token"),
        read_token=_token_override("ACB_READ_TOKEN", document, "read_token"),
        server=document["server"] if document is not None else DEFAULT_SERVER,
        source_db=source,
        explicit_config=explicit,
    )


def register_local(commands: argparse._SubParsersAction) -> None:
    """Register setup, serve, doctor, demo, and version on an existing parser."""

    setup = commands.add_parser("setup", help="create a private config and empty database")
    setup.add_argument("--config", help="config file path")
    setup.add_argument("--db", help="database path")
    serve = commands.add_parser("serve", help="run the loopback service in the foreground")
    serve.add_argument("--setup", action="store_true", help="create the config and empty database first")
    serve.add_argument("--config", help="config file path")
    serve.add_argument("--db", help="database path")
    serve.add_argument("--host", default="127.0.0.1", help="loopback host (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8080, help="port (default 8080)")
    doctor = commands.add_parser("doctor", help="check config, database, and the local service")
    doctor.add_argument("--config", help="config file path")
    doctor.add_argument("--db", help="database path")
    doctor.add_argument("--server", help="loopback server URL to probe")
    commands.add_parser("demo", help="estimate the bundled synthetic request")
    commands.add_parser("version", help="print the package version")


def run_local(args: argparse.Namespace) -> int:
    """Dispatch a command registered by :func:`register_local`."""

    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "demo":
        return run_demo()
    if args.command == "setup":
        return run_setup(args)
    if args.command == "serve":
        return run_serve(args)
    if args.command == "doctor":
        return run_doctor(args)
    print("command", file=sys.stderr)
    return EXIT_CONFIG_MISSING


def run_setup(args: argparse.Namespace) -> int:
    """Create or reopen the local config and database without printing secrets."""

    try:
        preview = load_cli_config(args.config, args.db)
    except LocalError as exc:
        return _fail(exc.code, exc.repair, exc.exit_code)
    if _db_conflict(preview):
        return _db_mismatch(preview)
    try:
        return _setup_locked(preview)
    except LocalError as exc:
        return _fail(exc.code, exc.repair, exc.exit_code, config=str(preview.path))
    except StoreError as exc:
        return _fail(
            "db_invalid",
            "The database could not be opened. Setup did not replace an existing config or database.",
            EXIT_DB_INVALID,
            problem_code=exc.code,
            config=str(preview.path),
        )
    except Exception:
        return _io_failure(config=str(preview.path))


def run_serve(args: argparse.Namespace) -> int:
    """Run uvicorn on a loopback host in this thread so signals stop the process."""

    if args.host.lower() not in _LOOPBACK_HOSTS:
        return _fail(
            "host",
            "ac serve accepts only a loopback host: 127.0.0.1, localhost, or ::1.",
            EXIT_CONFIG_INVALID,
        )
    if not isinstance(args.port, int) or isinstance(args.port, bool) or args.port < 1 or args.port > 65535:
        return _fail("port", "Choose a TCP port from 1 to 65535.", EXIT_CONFIG_INVALID)
    if not args.setup:
        blocked = _serve_block(args)
        if blocked is not None:
            return blocked
    if not _port_available(args.host, args.port):
        return _fail(
            "port_conflict",
            "That port is already in use. Choose another --port, or connect to the instance that is already running.",
            EXIT_PORT,
        )
    if args.setup:
        created = run_setup(args)
        if created != 0:
            return created
        blocked = _serve_block(args)
        if blocked is not None:
            return blocked
    try:
        config = load_cli_config(args.config, args.db)
    except LocalError as exc:
        return _fail(exc.code, exc.repair, exc.exit_code)
    try:
        from agent_costbook.api import create_app

        app = create_app(service_settings(config))
    except StoreError as exc:
        return _fail(
            "db_invalid",
            "The database could not be opened. Serve did not replace it.",
            EXIT_DB_INVALID,
            problem_code=exc.code,
        )
    _emit(
        {
            "status": "starting",
            "host": args.host,
            "port": args.port,
            "db": str(config.db_path),
            "config": str(config.path) if config.exists else None,
            "version": __version__,
        }
    )
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def run_doctor(args: argparse.Namespace) -> int:
    """Report config, schema, coverage, health, and auth without writing."""

    try:
        config = load_cli_config(args.config, args.db)
    except LocalError as exc:
        return _fail(exc.code, exc.repair, exc.exit_code, config=str(_config_location(args.config)[0]))
    if not config.exists:
        return _doctor_document(
            status="config_missing",
            exit_code=EXIT_CONFIG_MISSING,
            repair="Run ac setup to create a private config and an empty database.",
            config=config,
            coverage=None,
            checks=_checks(config="missing", database="unchecked", schema="unchecked"),
        )
    if not config.db_path.is_file():
        return _doctor_document(
            status="db_missing",
            exit_code=EXIT_DB_MISSING,
            repair="The configured database is missing. Run ac setup to create an empty one. Doctor does not create it.",
            config=config,
            coverage=None,
            checks=_checks(config="ok", database="missing", schema="unchecked"),
            problem_code="db_missing",
        )
    try:
        coverage = _readonly_coverage(config.db_path)
    except StoreError as exc:
        if exc.code == "not_found":
            return _doctor_document(
                status="db_missing",
                exit_code=EXIT_DB_MISSING,
                repair="The configured database is missing. Run ac setup. Doctor does not create it.",
                config=config,
                coverage=None,
                checks=_checks(config="ok", database="missing", schema="unchecked"),
            )
        return _doctor_document(
            status="db_invalid",
            exit_code=EXIT_DB_INVALID,
            repair=_schema_repair(exc.code),
            config=config,
            coverage=None,
            checks=_checks(config="ok", database="invalid", schema="invalid"),
            problem_code=exc.code,
        )
    except Exception:
        return _doctor_document(
            status="db_invalid",
            exit_code=EXIT_DB_INVALID,
            repair="The database could not be read. Doctor did not modify it.",
            config=config,
            coverage=None,
            checks=_checks(config="ok", database="invalid", schema="invalid"),
        )
    try:
        server = normalize_server(args.server or config.server)
    except DataError:
        return _fail(
            "config_invalid",
            "Pass a loopback server URL such as http://127.0.0.1:8080.",
            EXIT_CONFIG_INVALID,
            config=str(config.path),
            db=str(config.db_path),
        )
    health, auth, live, outcome = _probe(server, config)
    if live:
        coverage.update(live)
        coverage["empty"] = _empty(coverage)
    checks = _checks(config="ok", database="ok", schema="ok", health=health, auth=auth)
    if outcome == "transport":
        if health == "ok":
            repair = _unverified_credential_repair(auth)
        else:
            repair = (
                "Nothing is accepting connections at the server. Start it with ac serve, "
                "or pass --server for an instance that is already running."
            )
        return _doctor_document(
            status="transport",
            exit_code=EXIT_TRANSPORT,
            repair=repair,
            config=config,
            coverage=coverage,
            checks=checks,
            server=server,
        )
    if outcome == "auth":
        return _doctor_document(
            status="auth",
            exit_code=EXIT_AUTH,
            repair=_auth_repair(auth),
            config=config,
            coverage=coverage,
            checks=checks,
            server=server,
        )
    notes = ["The catalog is empty."] if coverage["empty"] else []
    return _doctor_document(
        status="ok",
        exit_code=0,
        repair="",
        config=config,
        coverage=coverage,
        checks=checks,
        server=server,
        notes=notes,
        problems=[],
    )


def run_demo() -> int:
    """Estimate the bundled synthetic snapshot. No config, network, or writes."""

    from agent_costbook.models import EstimateIn
    from agent_costbook.offline import estimate_snapshot, load_snapshot

    package = resources.files("agent_costbook.demo")
    document = load_snapshot(package.joinpath("snapshot.json").read_bytes(), expected_publisher="pub_sample")
    body = EstimateIn.model_validate_json(package.joinpath("request.json").read_bytes())
    payload = estimate_snapshot(document, body)
    payload["synthetic"] = True
    _emit(payload)
    return 0


def _config_location(config_path: str | None) -> tuple[Path, bool]:
    if config_path:
        return Path(config_path).expanduser().resolve(), True
    env = os.environ.get("ACB_CONFIG")
    if env:
        return Path(env).expanduser().resolve(), True
    return default_config_path(), False


def _resolve_db(
    config_path: Path,
    explicit: bool,
    document: dict | None,
    db_argument: str | None,
) -> tuple[Path, str, Path | None]:
    configured = None if document is None else Path(document["db_path"]).expanduser().resolve()
    if db_argument:
        return Path(db_argument).expanduser().resolve(), "cli", configured
    if "ACB_DB" in os.environ:
        return Path(os.environ["ACB_DB"]).expanduser().resolve(), "env", configured
    if configured is not None:
        return configured, "config", configured
    if explicit:
        return (config_path.parent / "costbook.sqlite3").resolve(), "config_dir", None
    return default_data_path().expanduser().resolve(), "default", None


def _token_override(env_name: str, document: dict | None, field: str) -> str:
    if env_name in os.environ:
        return os.environ[env_name]
    if document is None:
        return ""
    return document[field]


def _invalid_config(path: Path) -> LocalError:
    return LocalError(
        "config_invalid",
        f"Config at {path} is malformed or uses an unsupported version. "
        "Repair or remove it manually; ac setup will not overwrite it.",
        exit_code=EXIT_CONFIG_INVALID,
    )


def _parse_config_file(path: Path) -> dict:
    try:
        raw = path.read_bytes()
    except OSError:
        raise _invalid_config(path) from None
    try:
        return _config_document(path, raw)
    except LocalError:
        raise
    except Exception:
        raise _invalid_config(path) from None


def _config_document(path: Path, raw: bytes) -> dict:
    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict) or set(document) != _CONFIG_KEYS:
        raise _invalid_config(path)
    version = document.get("version")
    if type(version) is not int or version != 1:
        raise _invalid_config(path)
    db_path = document.get("db_path")
    if not isinstance(db_path, str) or not Path(db_path).is_absolute():
        raise _invalid_config(path)
    admin_token = document.get("admin_token")
    read_token = document.get("read_token")
    for value in (admin_token, read_token):
        if not isinstance(value, str) or not value or len(value) > 512:
            raise _invalid_config(path)
        if any(ord(character) < 32 for character in value):
            raise _invalid_config(path)
    if admin_token == read_token:
        raise LocalError(
            "config_invalid",
            f"Config at {path} is invalid. The admin and read tokens must be different. "
            "Repair or remove the file manually; ac setup will not overwrite it.",
            exit_code=EXIT_CONFIG_INVALID,
        )
    server = document.get("server")
    if not isinstance(server, str):
        raise _invalid_config(path)
    try:
        document["server"] = normalize_server(server)
    except DataError:
        raise _invalid_config(path) from None
    return document


def _db_conflict(config: CliConfig) -> bool:
    return (
        config.exists
        and config.configured_db_path is not None
        and config.db_path != config.configured_db_path
    )


def _db_mismatch(config: CliConfig) -> int:
    return _fail(
        "db_mismatch",
        "The requested database differs from the existing config. "
        "The original config and database were left unchanged. "
        "Reuse the configured database or replace the files deliberately.",
        EXIT_DB_MISMATCH,
        config=str(config.path),
        config_db=str(config.configured_db_path),
        requested_db=str(config.db_path),
    )


def _setup_locked(preview: CliConfig) -> int:
    if not preview.exists:
        _ensure_private_dir(preview.path.parent)
    with _SetupLock(preview.path.with_name(f".{preview.path.name}.lock")):
        if preview.path.is_file():
            document = _parse_config_file(preview.path)
            configured = Path(document["db_path"]).expanduser().resolve()
            if preview.source_db in {"cli", "env"} and preview.db_path != configured:
                return _db_mismatch(preview)
            created = False
            db_path = configured
            server = document["server"]
            credentials = _credential_flags(document)
        elif preview.path.exists():
            raise _invalid_config(preview.path)
        else:
            _ensure_private_dir(preview.path.parent)
            db_path = preview.db_path
            document = _new_document(db_path)
            _publish(preview.path, document)
            created = True
            server = document["server"]
            credentials = _credential_flags(document)
        coverage = _open_database(db_path)
        _private_file(preview.path)
    return _success(preview.path, db_path, server, coverage, credentials, created)


def _new_document(db_path: Path) -> dict:
    admin_token = secrets.token_urlsafe(32)
    read_token = secrets.token_urlsafe(32)
    while read_token == admin_token:
        read_token = secrets.token_urlsafe(32)
    return {
        "version": 1,
        "db_path": str(db_path),
        "admin_token": admin_token,
        "read_token": read_token,
        "server": DEFAULT_SERVER,
    }


def _publish(path: Path, document: dict) -> None:
    data = _bytes(document)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    if temporary.exists():
        raise LocalError("config_invalid", "A temporary config file is already present.", exit_code=EXIT_CONFIG_INVALID)
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        view = memoryview(data)
        while len(view):
            written = os.write(descriptor, view)
            if written <= 0:
                raise LocalError("config_invalid", "The config file could not be written.", exit_code=EXIT_CONFIG_INVALID)
            view = view[written:]
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    os.close(descriptor)
    os.chmod(temporary, 0o600)
    try:
        os.link(temporary, path)
    except FileExistsError:
        temporary.unlink(missing_ok=True)
        raise LocalError(
            "config_invalid",
            "The config file appeared while setup was writing. The existing file was left unchanged.",
            exit_code=EXIT_CONFIG_INVALID,
        ) from None
    temporary.unlink(missing_ok=True)
    _private_file(path)
    _fsync_dir(path.parent)


def _open_database(db_path: Path) -> dict:
    _ensure_private_dir(db_path.parent)
    try:
        store = Store(db_path)
    except StoreError:
        raise
    except Exception:
        raise LocalError("io", _IO_REPAIR, exit_code=EXIT_IO) from None
    try:
        coverage = _coverage(store)
    finally:
        store.close()
    try:
        _private_file(db_path)
        for suffix in ("-wal", "-shm"):
            sidecar = db_path.with_name(db_path.name + suffix)
            if sidecar.is_file():
                _private_file(sidecar)
    except OSError:
        raise LocalError("io", _IO_REPAIR, exit_code=EXIT_IO) from None
    return coverage


def _coverage(store: Store) -> dict:
    prices = len(store.catalog(None))
    current = store.current_capabilities()
    coverage = {
        "prices": prices,
        "agents": len(current["agents"]),
        "model_efforts": len(current["model_efforts"]),
    }
    coverage["empty"] = _empty(coverage)
    return coverage


def _readonly_coverage(db_path: Path) -> dict:
    store = Store(db_path, readonly=True)
    try:
        return _coverage(store)
    finally:
        store.close()


def _empty(coverage: dict) -> bool:
    return coverage["prices"] == 0 and coverage["agents"] == 0 and coverage["model_efforts"] == 0


def _credential_flags(document: dict) -> dict:
    return {
        "admin": "set" if document.get("admin_token") else "missing",
        "read": "set" if document.get("read_token") else "missing",
    }


def _success(path: Path, db_path: Path, server: str, coverage: dict, credentials: dict, created: bool) -> int:
    _emit(
        {
            "status": "ok",
            "created": created,
            "config": str(path),
            "db": str(db_path),
            "server": server,
            "coverage": coverage,
            "credentials": credentials,
        }
    )
    return 0


def _serve_block(args: argparse.Namespace) -> int | None:
    try:
        config = load_cli_config(args.config, args.db)
    except LocalError as exc:
        return _fail(exc.code, exc.repair, exc.exit_code)
    if not config.exists and not _explicit_runtime(args):
        return _fail(
            "config_missing",
            "No config file is available. Run ac setup, or pass --db with ACB_DB and ACB_ADMIN_TOKEN already set.",
            EXIT_CONFIG_MISSING,
            config=str(config.path),
        )
    if not config.admin_token:
        return _fail(
            "config_missing",
            "No admin credential is available. Run ac setup, or set ACB_ADMIN_TOKEN for this process. The value is not accepted as a CLI argument.",
            EXIT_CONFIG_MISSING,
            config=str(config.path),
        )
    if not config.db_path.is_file():
        return _fail(
            "db_missing",
            "The database is missing. Run ac setup to create an empty database. ac serve does not create one unless you pass --setup.",
            EXIT_DB_MISSING,
            config=str(config.path) if config.exists else None,
            db=str(config.db_path),
        )
    return None


def _explicit_runtime(args: argparse.Namespace) -> bool:
    explicit_db = bool(args.db) or "ACB_DB" in os.environ
    return explicit_db and bool(os.environ.get("ACB_ADMIN_TOKEN"))


def _port_available(host: str, port: int) -> bool:
    family = socket.AF_INET6 if host == "::1" else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


def _probe(server: str, config: CliConfig) -> tuple[str, dict, dict, str]:
    try:
        status, body = ServiceClient(server, "", timeout=_HTTP_TIMEOUT).json_map("GET", "/health")
    except (TransportError, DataError):
        return "down", {"admin": "unchecked", "read": "unchecked"}, {}, "transport"
    if status != 200 or body.get("status") != "ok":
        return "down", {"admin": "unchecked", "read": "unchecked"}, {}, "transport"
    live: dict = {}
    try:
        catalog_status, catalog = ServiceClient(server, "", timeout=_HTTP_TIMEOUT).json_map("GET", "/v1/catalog")
        if catalog_status == 200 and isinstance(catalog.get("records"), list):
            live["prices"] = len(catalog["records"])
    except (TransportError, DataError):
        return "down", {"admin": "unchecked", "read": "unchecked"}, live, "transport"
    admin_state, admin_body = _auth_probe(server, config.admin_token)
    read_state, _ = _auth_probe(server, config.read_token)
    auth = {"admin": admin_state, "read": read_state}
    if admin_state == "ok":
        if isinstance(admin_body.get("agents"), list):
            live["agents"] = len(admin_body["agents"])
        if isinstance(admin_body.get("model_efforts"), list):
            live["model_efforts"] = len(admin_body["model_efforts"])
    return "ok", auth, live, _credential_outcome(admin_state, read_state)


def _credential_outcome(admin_state: str, read_state: str) -> str:
    """Both generated credentials must pass. A 401 or a missing token is auth.

    An exception or unexpected status leaves the probe unchecked. That is a
    transport failure even when the other credential was accepted, and it is
    distinct from a rejected credential.
    """

    if admin_state == "ok" and read_state == "ok":
        return "ok"
    if admin_state in {"rejected", "missing"} or read_state in {"rejected", "missing"}:
        return "auth"
    return "transport"


def _auth_repair(auth: dict) -> str:
    sentences: list[str] = []
    envs: list[str] = []
    for name, env_name in (("admin", "ACB_ADMIN_TOKEN"), ("read", "ACB_READ_TOKEN")):
        state = auth.get(name)
        if state == "ok":
            continue
        if state == "missing":
            sentences.append(f"The {name} credential is missing.")
        elif state == "rejected":
            sentences.append(f"The {name} credential was rejected.")
        else:
            continue
        envs.append(env_name)
    joined = " and ".join(envs) if envs else "ACB_ADMIN_TOKEN and ACB_READ_TOKEN"
    sentences.append(
        "Restore the correct configuration or environment override, or verify the running service uses "
        + joined
        + "."
    )
    return " ".join(sentences)


def _unverified_credential_repair(auth: dict) -> str:
    names = [name for name in ("admin", "read") if auth.get(name) == "unchecked"]
    if len(names) == 2:
        named = "the admin and read credentials"
    elif len(names) == 1:
        named = f"the {names[0]} credential"
    else:
        named = "a credential"
    return (
        f"The server answered, but {named} could not be verified. "
        "This is not a rejected credential. Check that the service is still accepting connections."
    )


def _auth_probe(server: str, token: str) -> tuple[str, dict]:
    if not token:
        return "missing", {}
    try:
        status, body = ServiceClient(server, token, timeout=_HTTP_TIMEOUT).json_map(
            "GET",
            "/v1/capabilities",
            auth=True,
        )
    except (TransportError, DataError):
        return "unchecked", {}
    if status == 200:
        return "ok", body
    if status == 401:
        return "rejected", {}
    return "unchecked", {}


def _checks(
    *,
    config: str,
    database: str,
    schema: str,
    health: str = "unchecked",
    auth: dict | None = None,
) -> dict:
    return {
        "config": config,
        "database": database,
        "schema": schema,
        "health": health,
        "auth": auth or {"admin": "unchecked", "read": "unchecked"},
    }


def _schema_repair(code: str) -> str:
    if code == "future_schema":
        return "The database schema is newer than this ac build. Doctor did not modify it. Use the ac release that wrote it."
    if code == "migration_required":
        return "The database schema is older than this ac build. Doctor did not migrate it. Use the migrate entrypoint or a new database from ac setup."
    return "The database could not be checked. Doctor did not modify it."


def _doctor_document(
    *,
    status: str,
    exit_code: int,
    repair: str,
    config: CliConfig,
    coverage: dict | None,
    checks: dict,
    server: str | None = None,
    notes: list[str] | None = None,
    problems: list[dict] | None = None,
    problem_code: str | None = None,
) -> int:
    document = {
        "status": status,
        "problems": problems if problems is not None else [{"code": problem_code or status, "repair": repair}],
        "notes": notes or [],
        "coverage": coverage,
        "checks": checks,
        "paths": {"config": str(config.path), "db": str(config.db_path)},
        "server": server or config.server,
    }
    _emit(document)
    if status != "ok":
        print(status, file=sys.stderr)
    return exit_code


def _fail(status: str, repair: str, exit_code: int, *, problem_code: str | None = None, **extra) -> int:
    _emit(
        {
            "status": status,
            "problems": [{"code": problem_code or status, "repair": repair}],
            **extra,
        }
    )
    print(status, file=sys.stderr)
    return exit_code


def _emit(document: dict) -> None:
    sys.stdout.buffer.write(_bytes(document))


def _bytes(document: dict) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _ensure_private_dir(path: Path) -> None:
    """Create missing directories as ``0700``. Leave an existing directory unchanged."""

    if path.is_dir():
        return
    if path.exists():
        raise LocalError("io", _IO_REPAIR, exit_code=EXIT_IO)
    parent = path.parent
    if parent != path:
        _ensure_private_dir(parent)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        if path.is_dir():
            return
        raise LocalError("io", _IO_REPAIR, exit_code=EXIT_IO) from None
    except OSError:
        raise LocalError("io", _IO_REPAIR, exit_code=EXIT_IO) from None
    try:
        os.chmod(path, 0o700)
    except OSError:
        raise LocalError("io", _IO_REPAIR, exit_code=EXIT_IO) from None


def _private_file(path: Path) -> None:
    os.chmod(path, 0o600)


def _io_failure(**extra) -> int:
    return _fail("io", _IO_REPAIR, EXIT_IO, **extra)


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _SetupLock:
    """Exclusive lock so two setup processes cannot truncate the same secret."""

    def __init__(self, path: Path):
        self.path = path
        self._descriptor: int | None = None

    def __enter__(self) -> _SetupLock:
        deadline = time.monotonic() + 15
        while True:
            try:
                self._descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise LocalError(
                        "setup_busy",
                        "Another ac setup is still creating this config. Retry ac setup.",
                        exit_code=EXIT_CONFIG_MISSING,
                    ) from None
                try:
                    stale = time.time() - self.path.stat().st_mtime > 30
                except FileNotFoundError:
                    continue
                if stale:
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                time.sleep(0.05)
                continue
            os.write(self._descriptor, str(os.getpid()).encode("ascii"))
            os.fsync(self._descriptor)
            os.chmod(self.path, 0o600)
            return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
