"""Read-only catalog queries and online estimates over the local HTTP service."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote, urlencode

from pydantic import ValidationError

from agent_costbook.data_batch import DataError, ServiceClient, TransportError
from agent_costbook.models import EstimateIn

_DEFAULT_SERVER = "http://127.0.0.1:8080"
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_PRICE_COLUMNS = ("provider", "channel", "model", "effort", "plan", "currency")
_AGENT_FIELDS = (
    "agent_id",
    "domain",
    "source",
    "as_of",
    "row_version",
    "can_edit_files",
    "can_use_tools",
)
_EFFORT_FIELDS = ("provider", "model", "effort", "source", "as_of", "benchmarks")
_EVIDENCE_FIELDS = (
    "id",
    "source_kind",
    "source_url",
    "collector_kind",
    "collector_name",
    "retrieved_at",
    "content_sha256",
)
_RESEARCH_FIELDS = ("id", "title")


def _load_cli_config(config_path: str | None = None) -> dict:
    from agent_costbook.local import load_cli_config

    return load_cli_config(config_path=config_path)


def _failure(status: int) -> str | None:
    if status == 401:
        return "unauthorized"
    if status == 404:
        return "not_found"
    if status == 409:
        return "conflict"
    if status >= 500:
        return "uncertain"
    if status >= 400:
        return "failed"
    return None


def _emit(document: dict) -> None:
    payload = (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(payload)
    else:
        buffer.write(payload.encode("utf-8"))
        buffer.flush()


def _config_view(loaded: object) -> dict:
    if loaded is None:
        return {}
    if isinstance(loaded, dict):
        return loaded
    read_token = getattr(loaded, "read_token", "")
    admin_token = getattr(loaded, "admin_token", "")
    server = getattr(loaded, "server", "")
    if not all(isinstance(item, str) for item in (read_token, admin_token, server)):
        raise DataError("config")
    return {
        "db_path": getattr(loaded, "db_path", None),
        "admin_token": admin_token,
        "read_token": read_token,
        "server": server,
    }


def _config(args: argparse.Namespace) -> dict:
    path = getattr(args, "config", None)
    try:
        loaded = _load_cli_config(path)
    except ImportError:
        loaded = {}
    except FileNotFoundError:
        if path:
            raise DataError("config") from None
        loaded = {}
    except Exception as exc:
        if type(exc).__name__ != "LocalError":
            raise
        code = getattr(exc, "code", None)
        exit_code = getattr(exc, "exit_code", None)
        if not isinstance(code, str) or not isinstance(exit_code, int):
            raise DataError("config") from None
        raise DataError(code, exit_code=exit_code) from None
    return _config_view(loaded)


def _text(value: object) -> str:
    if isinstance(value, str) and value:
        return value
    return ""


def _server(args: argparse.Namespace, config: dict) -> str:
    explicit = _text(getattr(args, "server", None))
    if explicit:
        return explicit
    environ = _text(os.environ.get("ACB_SERVER", ""))
    if environ:
        return environ
    configured = _text(config.get("server"))
    if configured:
        return configured
    return _DEFAULT_SERVER


def _configured_secret(env_name: str, config: dict, key: str) -> str:
    environ = os.environ.get(env_name, "")
    if environ:
        return environ
    return _text(config.get(key))


def _query_token(config: dict) -> str:
    read = _configured_secret("ACB_READ_TOKEN", config, "read_token")
    if read:
        return read
    return _configured_secret("ACB_ADMIN_TOKEN", config, "admin_token")


def _admin_token(config: dict) -> str:
    return _configured_secret("ACB_ADMIN_TOKEN", config, "admin_token")


def _accepted(status: int, body: dict) -> dict:
    failure = _failure(status)
    if failure:
        raise DataError(failure)
    return body


def _get(server: str, token: str, path: str, *, auth: bool) -> dict:
    status, body = ServiceClient(server, token).json_map("GET", path, auth=auth)
    return _accepted(status, body)


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str):
        return ""
    text = _CSI.sub("", value)
    return "".join(character if character.isprintable() and character != "\t" else " " for character in text)


def _write_table(headers: list[str], rows: list[list[str]]) -> None:
    sys.stdout.write("\t".join(headers) + "\n")
    for row in rows:
        sys.stdout.write("\t".join(row) + "\n")


def _object_table(rows: list[dict], fields: tuple[str, ...]) -> tuple[list[str], list[list[str]]]:
    return list(fields), [[_cell(row.get(field)) for field in fields] for row in rows if isinstance(row, dict)]


def _benchmark_cell(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    parts = []
    for key in sorted(value):
        score = value[key]
        if score is None:
            continue
        parts.append(f"{_cell(key)}={_cell(score)}")
    return ",".join(parts)


def _price_table(records: list) -> tuple[list[str], list[list[str]]]:
    rate_keys = sorted(
        {
            key
            for record in records
            if isinstance(record, dict) and isinstance(record.get("rates"), dict)
            for key in record["rates"]
        }
    )
    headers = [*_PRICE_COLUMNS, *rate_keys]
    rows = []
    for record in records:
        if not isinstance(record, dict):
            continue
        scope = record.get("scope") if isinstance(record.get("scope"), dict) else {}
        rates = record.get("rates") if isinstance(record.get("rates"), dict) else {}
        cells = [_cell(record.get(name)) for name in ("provider", "channel", "model", "effort", "plan")]
        cells.append(_cell(scope.get("currency")))
        for key in rate_keys:
            rate = rates.get(key)
            amount = rate.get("amount") if isinstance(rate, dict) else None
            cells.append(_cell(amount))
        rows.append(cells)
    return headers, rows


def _effort_table(rows: list) -> tuple[list[str], list[list[str]]]:
    rendered = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rendered.append(
            [
                _cell(row.get("provider")),
                _cell(row.get("model")),
                _cell(row.get("effort")),
                _cell(row.get("source")),
                _cell(row.get("as_of")),
                _benchmark_cell(row.get("benchmarks")),
            ]
        )
    return list(_EFFORT_FIELDS), rendered


def _present(payload: dict, fmt: str, kind: str) -> None:
    if fmt == "json":
        _emit(payload)
        return
    if fmt != "table":
        raise DataError("invalid")
    if kind == "prices":
        headers, rows = _price_table(payload.get("records") or [])
    elif kind == "agents":
        headers, rows = _object_table(payload.get("agents") or [], _AGENT_FIELDS)
    elif kind == "agent":
        headers, rows = _object_table([payload], _AGENT_FIELDS)
    elif kind == "model_efforts":
        headers, rows = _effort_table(payload.get("model_efforts") or [])
    elif kind == "model_effort":
        headers, rows = _effort_table([payload])
    elif kind == "evidence":
        headers, rows = _object_table([payload], _EVIDENCE_FIELDS)
    elif kind == "research":
        headers, rows = _object_table([payload], _RESEARCH_FIELDS)
    else:
        raise DataError("invalid")
    _write_table(headers, rows)


def _prices(args: argparse.Namespace, server: str, fmt: str) -> int:
    document = _get(server, "", "/v1/catalog", auth=False)
    records = document.get("records")
    if not isinstance(records, list):
        raise DataError("response")
    provider = getattr(args, "provider", None)
    model = getattr(args, "model", None)
    if provider is None and model is None:
        matched = records
    else:
        matched = []
        for record in records:
            if not isinstance(record, dict):
                continue
            if provider is not None and record.get("provider") != provider:
                continue
            if model is not None and record.get("model") != model:
                continue
            matched.append(record)
        if not matched:
            raise DataError("not_found")
    filtered = dict(document)
    filtered["records"] = matched
    _present(filtered, fmt, "prices")
    return 0


def _agents(args: argparse.Namespace, server: str, config: dict, fmt: str) -> int:
    token = _query_token(config)
    agent_id = getattr(args, "agent_id", None)
    if agent_id is not None:
        path = "/v1/capabilities/agents?" + urlencode([("agent_id", agent_id)])
        _present(_get(server, token, path, auth=True), fmt, "agent")
        return 0
    body = _get(server, token, "/v1/capabilities", auth=True)
    agents = body.get("agents")
    if not isinstance(agents, list):
        raise DataError("response")
    _present({"agents": agents}, fmt, "agents")
    return 0


def _effort_matches(row: dict, effort: str) -> bool:
    value = row.get("effort")
    if effort == "":
        return value is None or value == ""
    return value == effort


def _model_efforts(args: argparse.Namespace, server: str, config: dict, fmt: str) -> int:
    token = _query_token(config)
    provider = getattr(args, "provider", None)
    model = getattr(args, "model", None)
    effort = getattr(args, "effort", None)
    if effort is not None and provider is not None and model is not None:
        path = "/v1/capabilities/model-efforts?" + urlencode(
            [("provider", provider), ("model", model), ("effort", effort)]
        )
        _present(_get(server, token, path, auth=True), fmt, "model_effort")
        return 0
    body = _get(server, token, "/v1/capabilities", auth=True)
    rows = body.get("model_efforts")
    if not isinstance(rows, list):
        raise DataError("response")
    matched = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if provider is not None and row.get("provider") != provider:
            continue
        if model is not None and row.get("model") != model:
            continue
        if effort is not None and not _effort_matches(row, effort):
            continue
        matched.append(row)
    if (provider is not None or model is not None or effort is not None) and not matched:
        raise DataError("not_found")
    _present({"model_efforts": matched}, fmt, "model_efforts")
    return 0


def _resource(args: argparse.Namespace, server: str, fmt: str, kind: str) -> int:
    identifier = getattr(args, "id", None)
    if not isinstance(identifier, str) or not identifier:
        raise DataError("invalid")
    path = f"/v1/{kind}/{quote(identifier, safe='')}"
    _present(_get(server, "", path, auth=False), fmt, kind)
    return 0


def _run_query(args: argparse.Namespace) -> int:
    command = getattr(args, "query_command", None)
    config = _config(args)
    server = _server(args, config)
    fmt = getattr(args, "format", None) or "json"
    if command == "prices":
        return _prices(args, server, fmt)
    if command == "agents":
        return _agents(args, server, config, fmt)
    if command == "model-efforts":
        return _model_efforts(args, server, config, fmt)
    if command == "evidence":
        return _resource(args, server, fmt, "evidence")
    if command == "research":
        return _resource(args, server, fmt, "research")
    raise DataError("command")


def run_query(args: argparse.Namespace) -> int:
    try:
        return _run_query(args)
    except DataError as exc:
        print(exc.code, file=sys.stderr)
        return exc.exit_code
    except TransportError as exc:
        print(exc.code, file=sys.stderr)
        return 2


def estimate_online(args: argparse.Namespace) -> int:
    try:
        return _estimate_online(args)
    except DataError as exc:
        print(exc.code, file=sys.stderr)
        return exc.exit_code
    except TransportError as exc:
        print(exc.code, file=sys.stderr)
        return 2


def _estimate_online(args: argparse.Namespace) -> int:
    if any(getattr(args, name, None) is not None for name in ("publisher", "now", "max_age_seconds")):
        raise DataError("offline_only")
    request = getattr(args, "request", None)
    if not isinstance(request, str) or not request:
        raise DataError("invalid")
    try:
        document = json.loads(Path(request).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise DataError("invalid") from None
    try:
        body = EstimateIn.model_validate(document)
    except ValidationError:
        raise DataError("invalid") from None
    config = _config(args)
    token = _admin_token(config) if body.method == "M7" else _query_token(config)
    status, response = ServiceClient(_server(args, config), token).json_map(
        "POST",
        "/v1/estimates",
        document,
        auth=bool(token),
    )
    failure = _failure(status)
    if failure:
        raise DataError(failure)
    _emit(response)
    return 0


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--server")
    parser.add_argument("--config")
    parser.add_argument("--format", choices=("json", "table"), default="json")


def register_query(commands: argparse._SubParsersAction) -> None:
    query = commands.add_parser("query")
    nested = query.add_subparsers(dest="query_command", required=True)
    prices = nested.add_parser("prices")
    _common(prices)
    prices.add_argument("--provider")
    prices.add_argument("--model")
    agents = nested.add_parser("agents")
    _common(agents)
    agents.add_argument("--agent-id")
    efforts = nested.add_parser("model-efforts")
    _common(efforts)
    efforts.add_argument("--provider")
    efforts.add_argument("--model")
    efforts.add_argument("--effort", default=None)
    evidence = nested.add_parser("evidence")
    _common(evidence)
    evidence.add_argument("--id", required=True)
    research = nested.add_parser("research")
    _common(research)
    research.add_argument("--id", required=True)
