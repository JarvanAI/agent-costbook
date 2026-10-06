import argparse
import ast
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_costbook.local import CliConfig, load_cli_config, query_credential
from agent_costbook.query import estimate_online, register_query, run_query

READ = "synthetic-read-token"
ADMIN = "synthetic-admin-token"


@pytest.fixture(autouse=True)
def _clear_query_env(monkeypatch):
    for name in ("ACB_READ_TOKEN", "ACB_ADMIN_TOKEN", "ACB_SERVER", "ACB_CONFIG", "ACB_DB"):
        monkeypatch.delenv(name, raising=False)


class _Handler(BaseHTTPRequestHandler):
    def _dispatch(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        receipt = {
            "method": self.command,
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": raw,
        }
        self.server.receipts.append(receipt)
        status, payload, extra = self.server.respond(receipt)
        if isinstance(payload, bytes):
            data = payload
        else:
            data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def log_message(self, fmt, *args):
        return


@contextmanager
def served(respond):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.respond = respond
    server.receipts = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server.receipts
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _config(server="", read_token="", admin_token=""):
    return CliConfig(
        path=Path("config.json"),
        exists=True,
        version=1,
        db_path=Path("costbook.sqlite3"),
        configured_db_path=None,
        admin_token=admin_token,
        read_token=read_token,
        server=server,
        source_db="config",
        explicit_config=True,
    )


def _install_config(monkeypatch, server="", read_token="", admin_token="", calls=None):
    def load_cli_config(config_path=None, db=None):
        if calls is not None:
            calls.append(config_path)
        return _config(server, read_token, admin_token)

    monkeypatch.setattr("agent_costbook.query.load_cli_config", load_cli_config)


def _parser():
    parser = argparse.ArgumentParser(prog="ac")
    register_query(parser.add_subparsers(dest="command", required=True))
    return parser


def _query(command, **kwargs):
    values = {
        "query_command": command,
        "server": None,
        "config": None,
        "format": "json",
        "provider": None,
        "model": None,
        "agent_id": None,
        "effort": None,
        "id": None,
    }
    values.update(kwargs)
    return argparse.Namespace(**values)


def _estimate_args(**kwargs):
    values = {
        "request": None,
        "server": None,
        "config": None,
        "publisher": None,
        "now": None,
        "max_age_seconds": None,
    }
    values.update(kwargs)
    return argparse.Namespace(**values)


def _record(provider, model, amount="2.50"):
    return {
        "record_id": f"rec-{provider}",
        "status": "ok",
        "provider": provider,
        "channel": "api",
        "model": model,
        "effort": None,
        "plan": "payg",
        "scope": {"function": "text", "currency": "USD"},
        "rates": {
            "uncached_input": {"amount": amount, "currency": "USD"},
            "cache_read": None,
            "billed_output": {"amount": "0.00", "currency": "USD"},
        },
        "subscription": None,
        "missing_fields": ["cache_read"],
        "sources": [
            {
                "id": "ev_1",
                "kind": "url",
                "retrieved_at": "2026-10-01T00:00:00Z",
                "note": "quoted from the provider price page",
            }
        ],
        "assumptions": [],
        "research_id": None,
    }


def _catalog(records):
    return {
        "publisher_id": "pub_local",
        "data_version": 3,
        "snapshot_id": "snap-3",
        "published_at": "2026-10-02T00:00:00Z",
        "formula_version": "ac-formulas-v1",
        "freshness": None,
        "content_sha256": "hash-of-the-full-catalog",
        "conflicts": [{"record_id": "rec-keep", "model": "other"}],
        "records": records,
    }


def _empty_catalog():
    return {
        "publisher_id": "pub_local",
        "data_version": None,
        "snapshot_id": None,
        "published_at": None,
        "formula_version": None,
        "freshness": None,
        "content_sha256": None,
        "records": [],
    }


def _benchmark(name, score, unit, source="catalog page", source_ref=None):
    return {
        "name": name,
        "score": score,
        "unit": unit,
        "source": source,
        "source_ref": source_ref,
    }


def _efforts():
    return [
        {
            "provider": "openai",
            "model": "openai/gpt-4o-mini",
            "effort": "low",
            "source": "catalog page",
            "benchmarks": [
                _benchmark("intelligence", "41", "point"),
                _benchmark("coding", None, "point"),
            ],
            "as_of": "2026-10-01T00:00:00Z",
            "row_version": 1,
        },
        {
            "provider": "openai",
            "model": "openai/gpt-4o-mini",
            "effort": None,
            "source": "catalog page\n\x1b[31mred",
            "benchmarks": None,
            "as_of": "2026-10-01T00:00:00Z",
            "row_version": 2,
        },
        {
            "provider": "openai",
            "model": "openai/gpt-4o-mini",
            "effort": "high",
            "source": "catalog page",
            "benchmarks": [_benchmark("intelligence", "55", "point")],
            "as_of": "2026-10-01T00:00:00Z",
            "row_version": 3,
        },
    ]


def _json_out(capsys):
    captured = capsys.readouterr()
    return json.loads(captured.out), captured.err


def _query_parts(path):
    parts = urlsplit(path)
    return parts.path, parse_qs(parts.query, keep_blank_values=True)


def test_register_query_defaults_json_and_has_no_token_argument():
    parser = _parser()
    args = parser.parse_args(["query", "prices", "--provider", "openai"])
    assert args.command == "query"
    assert args.query_command == "prices"
    assert args.format == "json"
    assert args.provider == "openai"
    assert args.model is None
    assert args.server is None
    omitted = parser.parse_args(["query", "model-efforts", "--provider", "openai"])
    assert omitted.effort is None
    explicit_null = parser.parse_args(
        ["query", "model-efforts", "--provider", "openai", "--model", "m", "--effort", ""]
    )
    assert explicit_null.effort == ""
    with pytest.raises(SystemExit):
        parser.parse_args(["query", "prices", "--token", READ])
    with pytest.raises(SystemExit):
        parser.parse_args(["query", "evidence"])
    with pytest.raises(SystemExit):
        parser.parse_args(["query", "research"])


def test_query_module_uses_the_cli_config_helper():
    import agent_costbook.query as query

    source = Path(query.__file__).read_text(encoding="utf-8")
    imported = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.ImportFrom) and node.module == "agent_costbook.local":
            imported.extend(alias.name for alias in node.names)
    assert {"load_cli_config", "query_credential", "LocalError"} <= set(imported)
    assert "except ImportError" not in source
    assert "type(exc).__name__" not in source


def test_prices_filter_preserves_catalog_envelope_and_source(monkeypatch, capsys):
    records = [
        _record("openai", "openai/gpt-4o-mini", "2.50"),
        _record("anthropic", "anthropic/claude", "9.00"),
    ]
    catalog = _catalog(records)

    def respond(receipt):
        assert receipt["authorization"] is None
        assert receipt["path"] == "/v1/catalog"
        return 200, catalog, []

    calls = []
    with served(respond) as (base, receipts):
        _install_config(monkeypatch, server="http://127.0.0.1:9", calls=calls)
        code = run_query(
            _query(
                "prices",
                server=base,
                config="/tmp/ac-query.json",
                provider="openai",
                model="openai/gpt-4o-mini",
            )
        )
        assert code == 0
        assert calls == ["/tmp/ac-query.json"]
        assert len(receipts) == 1
    document, err = _json_out(capsys)
    assert err == ""
    assert document["publisher_id"] == "pub_local"
    assert document["data_version"] == 3
    assert document["snapshot_id"] == "snap-3"
    assert document["content_sha256"] == "hash-of-the-full-catalog"
    assert document["formula_version"] == "ac-formulas-v1"
    assert document["freshness"] is None
    assert document["conflicts"] == catalog["conflicts"]
    assert document["records"] == [records[0]]
    assert document["records"][0]["effort"] is None
    assert document["records"][0]["research_id"] is None
    assert document["records"][0]["rates"]["cache_read"] is None
    assert document["records"][0]["sources"][0]["note"] == "quoted from the provider price page"


def test_unfiltered_empty_prices_succeed_and_missing_model_is_not_found(monkeypatch, capsys):
    state = {"records": []}

    def respond(receipt):
        assert receipt["authorization"] is None
        return 200, _catalog(state["records"]) if state["records"] else _empty_catalog(), []

    with served(respond) as (base, _receipts):
        _install_config(monkeypatch, server=base)
        assert run_query(_query("prices", server=base)) == 0
        empty, err = _json_out(capsys)
        assert err == ""
        assert empty["records"] == []
        assert empty["data_version"] is None
        assert empty["content_sha256"] is None
        assert run_query(_query("prices", server=base, model="missing")) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "not_found\n"
        state["records"] = [
            _record("openai", "openai/gpt-4o-mini"),
            _record("anthropic", "anthropic/claude"),
        ]
        assert run_query(_query("prices", server=base)) == 0
        listed, err = _json_out(capsys)
        assert err == ""
        assert [row["provider"] for row in listed["records"]] == ["openai", "anthropic"]
        assert run_query(_query("prices", server=base, provider="missing")) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "not_found\n"


def test_agents_empty_list_succeeds_and_missing_agent_is_not_found(monkeypatch, capsys):
    agents = [
        {
            "agent_id": "agent-1",
            "domain": None,
            "source": "operator note",
            "as_of": "2026-10-01T00:00:00Z",
            "row_version": 1,
            "can_edit_files": False,
        }
    ]

    def respond(receipt):
        assert receipt["authorization"] == f"Bearer {READ}"
        path, query = _query_parts(receipt["path"])
        if path == "/v1/capabilities":
            return 200, {"agents": agents, "model_efforts": _efforts()}, []
        if path == "/v1/capabilities/agents" and query.get("agent_id") == ["agent-1"]:
            return 200, agents[0], []
        return 404, {"detail": f"secret {READ}"}, []

    with served(respond) as (base, receipts):
        _install_config(monkeypatch, server=base, read_token=READ)
        monkeypatch.setenv("ACB_READ_TOKEN", READ)
        assert run_query(_query("agents", server=base)) == 0
        listed, err = _json_out(capsys)
        assert err == ""
        assert listed == {"agents": agents}
        assert "model_efforts" not in listed
        assert run_query(_query("agents", server=base, agent_id="agent-1")) == 0
        one, err = _json_out(capsys)
        assert err == ""
        assert one == agents[0]
        assert one["domain"] is None
        assert one["can_edit_files"] is False
        assert run_query(_query("agents", server=base, agent_id="missing")) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "not_found\n"
        assert READ not in captured.err
        assert receipts[-1]["path"].startswith("/v1/capabilities/agents?")


def test_omitted_effort_lists_tiers_and_explicit_empty_selects_null_tier(monkeypatch, capsys):
    rows = _efforts()

    def respond(receipt):
        path, query = _query_parts(receipt["path"])
        if path == "/v1/capabilities":
            return 200, {"agents": [], "model_efforts": rows}, []
        if path != "/v1/capabilities/model-efforts" or "effort" not in query:
            return 404, {"detail": "missing explicit effort"}, []
        effort = query["effort"][0]
        matched = [
            row
            for row in rows
            if row["provider"] == query.get("provider", [""])[0]
            and row["model"] == query.get("model", [""])[0]
            and ((effort == "" and row["effort"] is None) or row["effort"] == effort)
        ]
        if len(matched) != 1:
            return 404, {"detail": "capability not found"}, []
        return 200, matched[0], []

    with served(respond) as (base, receipts):
        _install_config(monkeypatch, server=base, read_token=READ)
        assert (
            run_query(
                _query(
                    "model-efforts",
                    server=base,
                    provider="openai",
                    model="openai/gpt-4o-mini",
                )
            )
            == 0
        )
        listed, err = _json_out(capsys)
        assert err == ""
        assert listed == {"model_efforts": rows}
        assert receipts[-1]["path"] == "/v1/capabilities"
        assert (
            run_query(
                _query(
                    "model-efforts",
                    server=base,
                    provider="openai",
                    model="openai/gpt-4o-mini",
                    effort="",
                )
            )
            == 0
        )
        selected, err = _json_out(capsys)
        assert err == ""
        assert selected["effort"] is None
        assert selected["benchmarks"] is None
        assert selected["source"] == "catalog page\n\x1b[31mred"
        assert "model_efforts" not in selected
        path, query = _query_parts(receipts[-1]["path"])
        assert path == "/v1/capabilities/model-efforts"
        assert query["effort"] == [""]
        assert query["provider"] == ["openai"]
        assert query["model"] == ["openai/gpt-4o-mini"]
        assert run_query(_query("model-efforts", server=base, effort="absent")) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "not_found\n"
        assert run_query(_query("model-efforts", server=base)) == 0
        empty_filter, err = _json_out(capsys)
        assert err == ""
        assert empty_filter == {"model_efforts": rows}


def test_capabilities_without_token_or_rejected_token_are_unauthorized(monkeypatch, capsys):
    def respond(receipt):
        return 401, {"detail": f"Bearer {READ}"}, []

    with served(respond) as (base, receipts):
        _install_config(monkeypatch, server=base)
        assert run_query(_query("agents", server=base)) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "unauthorized\n"
        assert receipts == []
        _install_config(monkeypatch, server=base, read_token=READ)
        assert run_query(_query("model-efforts", server=base, provider="openai")) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "unauthorized\n"
        assert READ not in captured.out
        assert READ not in captured.err
        assert receipts[-1]["authorization"] == f"Bearer {READ}"


def test_resolved_config_prefers_read_token_then_admin(monkeypatch, capsys):
    def respond(receipt):
        return 200, {"agents": [], "model_efforts": []}, []

    with served(respond) as (base, receipts):
        _install_config(monkeypatch, server=base, read_token="config-read", admin_token="config-admin")
        assert run_query(_query("agents", server=base)) == 0
        assert receipts[-1]["authorization"] == "Bearer config-read"
        _install_config(monkeypatch, server=base, read_token="", admin_token="config-admin")
        assert run_query(_query("agents", server=base)) == 0
        assert receipts[-1]["authorization"] == "Bearer config-admin"
        capsys.readouterr()


def test_public_prices_do_not_send_a_token(monkeypatch, capsys):
    def respond(receipt):
        assert receipt["authorization"] is None
        return 200, _empty_catalog(), []

    with served(respond) as (base, receipts):
        _install_config(monkeypatch, server=base, read_token=READ, admin_token=ADMIN)
        monkeypatch.setenv("ACB_READ_TOKEN", READ)
        monkeypatch.setenv("ACB_ADMIN_TOKEN", ADMIN)
        assert run_query(_query("prices", server=base)) == 0
        assert len(receipts) == 1
    _json_out(capsys)


def test_evidence_and_research_preserve_api_bodies(monkeypatch, capsys):
    evidence = {
        "id": "ev_1",
        "contribution_id": "con_1",
        "source_kind": "url",
        "source_url": "https://example.test/prices",
        "collector_kind": None,
        "collector_name": None,
        "content": "quoted from the provider price page",
        "content_sha256": "abc",
        "retrieved_at": "2026-10-01T00:00:00Z",
    }
    research = {
        "id": "rs_1",
        "contribution_id": "con_1",
        "title": "Price note",
        "markdown": "source line\nwith a quote",
    }

    def respond(receipt):
        assert receipt["authorization"] is None
        if receipt["path"] == "/v1/evidence/ev_1":
            return 200, evidence, []
        if receipt["path"] == "/v1/research/rs_1":
            return 200, research, []
        return 404, {"detail": f"hidden {READ}"}, []

    with served(respond) as (base, _receipts):
        _install_config(monkeypatch, server=base, read_token=READ)
        monkeypatch.setenv("ACB_READ_TOKEN", READ)
        assert run_query(_query("evidence", server=base, id="ev_1")) == 0
        body, err = _json_out(capsys)
        assert err == ""
        assert body == evidence
        assert run_query(_query("research", server=base, id="rs_1")) == 0
        body, err = _json_out(capsys)
        assert err == ""
        assert body == research
        assert run_query(_query("evidence", server=base, id="missing")) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "not_found\n"
        assert READ not in captured.err
        assert run_query(_query("research", server=base, id=None)) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "invalid\n"


def test_table_prints_recorded_values_and_leaves_missing_cells_blank(monkeypatch, capsys):
    record = _record("open\x1b[31mai", "gpt\n4o", "2.50")

    def respond(receipt):
        path, _query = _query_parts(receipt["path"])
        if path == "/v1/catalog":
            return 200, _catalog([record]), []
        return 200, {"agents": [], "model_efforts": _efforts()}, []

    with served(respond) as (base, _receipts):
        _install_config(monkeypatch, server=base, read_token=READ)
        assert run_query(_query("prices", server=base, format="table")) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        assert "\x1b" not in captured.out
        assert "null" not in captured.out.lower()
        assert "None" not in captured.out
        lines = captured.out.splitlines()
        assert len(lines) == 2
        header = lines[0].split("\t")
        row = lines[1].split("\t")
        assert header[:6] == ["provider", "channel", "model", "effort", "plan", "currency"]
        assert header[6:] == ["billed_output", "cache_read", "uncached_input"]
        cells = dict(zip(header, row, strict=True))
        assert cells["effort"] == ""
        assert cells["cache_read"] == ""
        assert cells["uncached_input"] == "2.50"
        assert cells["billed_output"] == "0.00"
        assert cells["currency"] == "USD"
        assert cells["model"] == "gpt 4o"
        assert "gpt\n4o" not in captured.out
        assert (
            run_query(
                _query(
                    "model-efforts",
                    server=base,
                    format="table",
                    provider="openai",
                    model="openai/gpt-4o-mini",
                )
            )
            == 0
        )
        captured = capsys.readouterr()
        assert captured.err == ""
        assert "intelligence=41 point" in captured.out
        assert "intelligence=55 point" in captured.out
        assert "coding=0" not in captured.out
        assert "null" not in captured.out.lower()
        assert "\x1b" not in captured.out
        assert "catalog page red" in captured.out or "catalog page  red" in captured.out


def test_benchmark_list_renders_name_score_unit_and_json_keeps_fields(monkeypatch, capsys):
    benchmarks = [
        _benchmark("coding\n\x1b[31mx", "0.00", "po\nint", source="bench-source-kept", source_ref="bench-ref-kept"),
        _benchmark("intelligence", "90", "index", source="later-source"),
        _benchmark("coding", None, "point", source="skipped-null"),
    ]
    row = {
        "provider": "openai",
        "model": "openai/gpt-4o-mini",
        "effort": "low",
        "source": "catalog page",
        "as_of": "2026-10-01T00:00:00Z",
        "benchmarks": benchmarks,
    }

    def respond(receipt):
        return 200, row, []

    with served(respond) as (base, _receipts):
        _install_config(monkeypatch, server=base, read_token=READ)
        assert (
            run_query(
                _query(
                    "model-efforts",
                    server=base,
                    provider="openai",
                    model="openai/gpt-4o-mini",
                    effort="low",
                    format="json",
                )
            )
            == 0
        )
        document, err = _json_out(capsys)
        assert err == ""
        assert document["benchmarks"] == benchmarks
        assert document["benchmarks"][0]["source"] == "bench-source-kept"
        assert document["benchmarks"][0]["source_ref"] == "bench-ref-kept"
        assert document["benchmarks"][1]["source_ref"] is None
        assert (
            run_query(
                _query(
                    "model-efforts",
                    server=base,
                    provider="openai",
                    model="openai/gpt-4o-mini",
                    effort="low",
                    format="table",
                )
            )
            == 0
        )
        captured = capsys.readouterr()
        assert captured.err == ""
        lines = captured.out.splitlines()
        assert len(lines) == 2
        header = lines[0].split("\t")
        cells = dict(zip(header, lines[1].split("\t"), strict=True))
        assert cells["benchmarks"] == "coding x=0.00 po int,intelligence=90 index"
        assert cells["benchmarks"].index("coding x=0.00") < cells["benchmarks"].index("intelligence=90")
        assert "coding=0" not in captured.out
        assert "bench-source-kept" not in captured.out
        assert "bench-ref-kept" not in captured.out
        assert "later-source" not in captured.out
        assert "\x1b" not in captured.out


def test_server_priority_uses_argument_then_env_then_config(monkeypatch, capsys):
    def respond(receipt):
        return 200, _empty_catalog(), []

    with served(respond) as (explicit, explicit_receipts):
        with served(respond) as (configured, configured_receipts):
            _install_config(monkeypatch, server=configured)
            monkeypatch.setenv("ACB_SERVER", "http://127.0.0.1:9")
            assert run_query(_query("prices", server=explicit)) == 0
            assert len(explicit_receipts) == 1
            assert configured_receipts == []
            assert run_query(_query("prices", server=None)) == 2
            captured = capsys.readouterr()
            assert captured.err == "transport\n"
            monkeypatch.setenv("ACB_SERVER", configured)
            assert run_query(_query("prices")) == 0
            assert len(configured_receipts) == 1
            monkeypatch.delenv("ACB_SERVER")
            assert run_query(_query("prices")) == 0
            assert len(configured_receipts) == 2
    capsys.readouterr()


def test_default_server_is_loopback_8080(monkeypatch, capsys):
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 8080), _Handler)
    except OSError:
        pytest.skip("port 8080 is already in use")
    server.receipts = []
    server.respond = lambda _receipt: (200, _empty_catalog(), [])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _install_config(monkeypatch, server="")
        assert run_query(_query("prices")) == 0
        assert len(server.receipts) == 1
        assert server.receipts[0]["path"] == "/v1/catalog"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    captured = capsys.readouterr()
    assert captured.err == ""


def test_cli_config_dataclass_supplies_server_and_read_token(monkeypatch, capsys):
    def respond(receipt):
        return 200, {"agents": [], "model_efforts": []}, []

    with served(respond) as (base, receipts):
        config = _config(server=base, read_token="config-read", admin_token="config-admin")
        assert isinstance(config, CliConfig)
        monkeypatch.setattr(
            "agent_costbook.query.load_cli_config",
            lambda config_path=None, db=None: config,
        )
        assert run_query(_query("agents")) == 0
        assert receipts[-1]["authorization"] == "Bearer config-read"
    captured = capsys.readouterr()
    assert "config-read" not in captured.err
    assert "config-admin" not in captured.err


def test_real_cli_config_env_overrides_file_tokens(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    config_path = tmp_path / "config.json"
    _write_real_config(
        config_path,
        server="http://127.0.0.1:9",
        admin=FILE_ADMIN,
        read=FILE_READ,
        db=tmp_path / "costbook.sqlite3",
    )
    loaded = load_cli_config(str(config_path))
    assert isinstance(loaded, CliConfig)
    assert loaded.read_token == FILE_READ
    assert loaded.admin_token == FILE_ADMIN
    assert query_credential(loaded) == FILE_READ
    monkeypatch.setenv("ACB_READ_TOKEN", "")
    monkeypatch.setenv("ACB_ADMIN_TOKEN", ENV_ADMIN)
    loaded = load_cli_config(str(config_path))
    assert loaded.read_token == ""
    assert loaded.admin_token == ENV_ADMIN
    assert loaded.server == "http://127.0.0.1:9"
    assert query_credential(loaded) == ENV_ADMIN


def test_local_config_error_prints_only_its_code(monkeypatch, capsys):
    from agent_costbook.local import LocalError

    def load_cli_config(config_path=None, db=None):
        raise LocalError("config_invalid", "repair mentions " + READ, exit_code=3)

    monkeypatch.setattr("agent_costbook.query.load_cli_config", load_cli_config)
    assert run_query(_query("prices", server="http://127.0.0.1:9")) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "config_invalid\n"
    assert READ not in captured.err


def test_stopped_service_redirect_and_error_body_do_not_reveal_tokens(monkeypatch, capsys):
    closed = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    closed_port = closed.server_address[1]
    closed.server_close()

    def respond(receipt):
        if receipt["path"] == "/v1/capabilities":
            return 302, {"detail": f"Bearer {READ}", "location": "http://127.0.0.1/private"}, [
                ("Location", "http://127.0.0.1/private")
            ]
        return 500, {"detail": f"Bearer {ADMIN}"}, []

    with served(respond) as (base, receipts):
        _install_config(monkeypatch, server=base, read_token=READ, admin_token=ADMIN)
        monkeypatch.setenv("ACB_READ_TOKEN", READ)
        monkeypatch.setenv("ACB_ADMIN_TOKEN", ADMIN)
        assert run_query(_query("prices", server=f"http://127.0.0.1:{closed_port}")) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "transport\n"
        assert READ not in captured.err and ADMIN not in captured.err
        assert run_query(_query("agents", server=base)) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "redirect\n"
        assert READ not in captured.out and READ not in captured.err
        assert len(receipts) == 1
        assert run_query(_query("evidence", server=base, id="ev_1")) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "uncertain\n"
        assert ADMIN not in captured.out and ADMIN not in captured.err


def _request(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _valid_request(method="M4"):
    return {
        "method": method,
        "currency": "USD",
        "usage": {"uncached_input": "1000", "billed_output": "400"},
        "candidates": [
            {
                "candidate_id": "synthetic-m4",
                "provider": "example",
                "channel": "api",
                "model": "synthetic-m4",
                "plan": "payg",
                "feature_scope": "text",
            }
        ],
    }


def test_online_estimate_posts_request_and_preserves_server_status(monkeypatch, capsys, tmp_path):
    def respond(receipt):
        assert receipt["path"] == "/v1/estimates"
        assert receipt["method"] == "POST"
        assert receipt["authorization"] is None
        sent = json.loads(receipt["body"])
        assert sent == _valid_request()
        assert "effort" not in sent["candidates"][0]
        return 200, {
            "publisher_id": "pub_local",
            "data_version": 3,
            "snapshot_id": "snap-3",
            "results": [
                {
                    "candidate_id": "synthetic-m4",
                    "status": "missing_data",
                    "capabilities": {"agent": None, "model_effort": None, "status": "not_authorized"},
                }
            ],
        }, []

    with served(respond) as (base, receipts):
        _install_config(monkeypatch, server=base)
        path = _request(tmp_path, _valid_request())
        assert estimate_online(_estimate_args(request=str(path), server=base)) == 0
        assert len(receipts) == 1
    document, err = _json_out(capsys)
    assert err == ""
    assert document["results"][0]["status"] == "missing_data"
    assert document["results"][0]["capabilities"]["status"] == "not_authorized"
    assert document["publisher_id"] == "pub_local"
    assert document["data_version"] == 3


def test_read_token_is_attached_for_non_m7_estimates(monkeypatch, capsys, tmp_path):
    def respond(receipt):
        assert receipt["authorization"] == f"Bearer {READ}"
        return 200, {
            "publisher_id": "pub_local",
            "data_version": 3,
            "snapshot_id": "snap-3",
            "results": [
                {
                    "candidate_id": "synthetic-m4",
                    "status": "ok",
                    "capabilities": {
                        "agent": None,
                        "model_effort": {"effort": None, "source": "catalog page"},
                        "status": "ok",
                    },
                }
            ],
        }, []

    with served(respond) as (base, _receipts):
        _install_config(monkeypatch, server=base, read_token=READ, admin_token=ADMIN)
        monkeypatch.setenv("ACB_ADMIN_TOKEN", ADMIN)
        path = _request(tmp_path, _valid_request("M6"))
        assert estimate_online(_estimate_args(request=str(path), server=base)) == 0
    document, err = _json_out(capsys)
    assert err == ""
    assert document["results"][0]["capabilities"]["status"] == "ok"
    assert document["results"][0]["capabilities"]["model_effort"]["effort"] is None


def test_m7_uses_admin_token_and_never_falls_back_to_read(monkeypatch, capsys, tmp_path):
    def respond(receipt):
        if receipt["authorization"] == f"Bearer {ADMIN}":
            return 200, {
                "publisher_id": "pub_local",
                "data_version": 4,
                "snapshot_id": "snap-4",
                "results": [{"candidate_id": "synthetic-m4", "status": "ok"}],
            }, []
        return 401, {"detail": f"Bearer {READ}"}, []

    with served(respond) as (base, receipts):
        _install_config(monkeypatch, server=base, read_token=READ, admin_token=ADMIN)
        monkeypatch.setenv("ACB_READ_TOKEN", READ)
        monkeypatch.setenv("ACB_ADMIN_TOKEN", ADMIN)
        path = _request(tmp_path, _valid_request("M7"))
        assert estimate_online(_estimate_args(request=str(path), server=base)) == 0
        assert receipts[-1]["authorization"] == f"Bearer {ADMIN}"
        document, err = _json_out(capsys)
        assert err == ""
        assert document["results"][0]["status"] == "ok"
        assert document["snapshot_id"] == "snap-4"
        monkeypatch.delenv("ACB_ADMIN_TOKEN")
        monkeypatch.setattr(
            "agent_costbook.query.load_cli_config",
            lambda config_path=None: _config(base, READ, ""),
        )
        assert estimate_online(_estimate_args(request=str(path), server=base)) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "unauthorized\n"
        assert READ not in captured.out and READ not in captured.err
        assert receipts[-1]["authorization"] in {None, ""}


def test_invalid_estimate_request_exits_without_calling_the_server(monkeypatch, capsys, tmp_path):
    def respond(_receipt):
        raise AssertionError("invalid request reached the server")

    with served(respond) as (base, receipts):
        _install_config(monkeypatch, server=base)
        invalid = tmp_path / "invalid.json"
        invalid.write_text('{"method":"M4"}', encoding="utf-8")
        assert estimate_online(_estimate_args(request=str(invalid), server=base)) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "invalid\n"
        broken = tmp_path / "broken.json"
        broken.write_text("{", encoding="utf-8")
        assert estimate_online(_estimate_args(request=str(broken), server=base)) == 2
        captured = capsys.readouterr()
        assert captured.err == "invalid\n"
        raw = tmp_path / "bad-utf8.json"
        raw.write_bytes(b"\xff\xfe{")
        assert estimate_online(_estimate_args(request=str(raw), server=base)) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "invalid\n"
        assert receipts == []


def test_online_estimate_rejects_offline_snapshot_flags(monkeypatch, capsys, tmp_path):
    def respond(_receipt):
        raise AssertionError("offline flag reached the server")

    with served(respond) as (base, receipts):
        _install_config(monkeypatch, server=base)
        path = _request(tmp_path, _valid_request())
        for extra in (
            {"publisher": "pub_local"},
            {"now": "2026-10-05T00:00:00Z"},
            {"max_age_seconds": 0},
        ):
            assert estimate_online(_estimate_args(request=str(path), server=base, **extra)) == 2
            captured = capsys.readouterr()
            assert captured.out == ""
            assert captured.err == "offline_only\n"
        assert receipts == []


ROOT = Path(__file__).resolve().parents[1]
FILE_READ = "file-read-token"
FILE_ADMIN = "file-admin-token"
ENV_READ = "env-read-token"
ENV_ADMIN = "env-admin-token"


def _invoke(argv: list[str]) -> int:
    from agent_costbook.offline import main

    try:
        return main(argv)
    except SystemExit as exc:
        return int(exc.code)


def _isolate_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("ACB_CONFIG", raising=False)


def _write_real_config(path: Path, *, server: str, admin: str, read: str, db: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "db_path": str(db.resolve()),
                "admin_token": admin,
                "read_token": read,
                "server": server,
            }
        ),
        encoding="utf-8",
    )


def _agent_scope(server: str) -> dict:
    return {
        "kind": "agent-costbook.data-scope",
        "contract_version": 1,
        "id": "token-run",
        "server": server,
        "visibility": "public",
        "items": [
            {
                "item_id": "local-agent",
                "category": "agent",
                "collection": "manual",
                "mapping": "A person wrote this observation.",
                "candidate": {
                    "agent_id": "local-agent",
                    "source": "user_observation",
                    "as_of": "2026-09-28T00:00:00+00:00",
                    "strengths": "edits the batch",
                },
            }
        ],
    }


def test_version_flag_still_prints_the_package_version(capsys):
    from agent_costbook import __version__

    assert _invoke(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_registered_query_prices_without_setup_uses_explicit_server(monkeypatch, capsys, tmp_path):
    _isolate_config(monkeypatch, tmp_path)

    def respond(receipt):
        assert receipt["authorization"] is None
        return 200, _empty_catalog(), []

    with served(respond) as (base, receipts):
        assert _invoke(["query", "prices", "--server", base]) == 0
        assert len(receipts) == 1
        assert receipts[0]["path"] == "/v1/catalog"
    document, err = _json_out(capsys)
    assert err == ""
    assert document["records"] == []


def test_real_config_env_precedence_and_explicit_server(monkeypatch, capsys, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    config_path = tmp_path / "config.json"
    _write_real_config(
        config_path,
        server="http://127.0.0.1:9",
        admin=FILE_ADMIN,
        read=FILE_READ,
        db=tmp_path / "costbook.sqlite3",
    )

    def respond(receipt):
        path, _query = _query_parts(receipt["path"])
        if path == "/v1/catalog":
            return 200, _empty_catalog(), []
        if path == "/v1/capabilities":
            return 200, {"agents": [], "model_efforts": []}, []
        if path == "/v1/estimates":
            sent = json.loads(receipt["body"])
            return 200, {
                "publisher_id": "pub_local",
                "data_version": 1,
                "snapshot_id": "snap-1",
                "results": [{"candidate_id": sent["candidates"][0]["candidate_id"], "status": "ok"}],
            }, []
        return 404, {"detail": "missing"}, []

    with served(respond) as (base, receipts):
        assert _invoke(["query", "prices", "--server", base, "--config", str(config_path)]) == 0
        assert receipts[-1]["authorization"] is None
        assert _invoke(["query", "agents", "--config", str(config_path), "--server", base]) == 0
        assert receipts[-1]["authorization"] == f"Bearer {FILE_READ}"
        monkeypatch.setenv("ACB_READ_TOKEN", ENV_READ)
        monkeypatch.setenv("ACB_ADMIN_TOKEN", ENV_ADMIN)
        assert _invoke(["query", "agents", "--config", str(config_path), "--server", base]) == 0
        assert receipts[-1]["authorization"] == f"Bearer {ENV_READ}"
        monkeypatch.setenv("ACB_READ_TOKEN", "")
        assert _invoke(["query", "agents", "--config", str(config_path), "--server", base]) == 0
        assert receipts[-1]["authorization"] == f"Bearer {ENV_ADMIN}"
        request = _request(tmp_path, _valid_request("M7"))
        assert _invoke(["estimate", "--server", base, "--request", str(request), "--config", str(config_path)]) == 0
        assert receipts[-1]["path"] == "/v1/estimates"
        assert receipts[-1]["authorization"] == f"Bearer {ENV_ADMIN}"
        monkeypatch.delenv("ACB_ADMIN_TOKEN")
        assert _invoke(["estimate", "--server", base, "--request", str(request), "--config", str(config_path)]) == 0
        assert receipts[-1]["authorization"] == f"Bearer {FILE_ADMIN}"
    captured = capsys.readouterr()
    assert FILE_READ not in captured.err
    assert FILE_ADMIN not in captured.err
    assert ENV_READ not in captured.err
    assert ENV_ADMIN not in captured.err


def test_invalid_config_code_hides_the_file_text(monkeypatch, capsys, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    config_path = tmp_path / "config.json"
    config_path.write_text('{"admin_token":"' + FILE_ADMIN + '"}', encoding="utf-8")
    assert _invoke(["query", "prices", "--server", "http://127.0.0.1:9", "--config", str(config_path)]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "config_invalid\n"
    assert FILE_ADMIN not in captured.err


def test_estimate_source_is_required_and_exclusive(capsys):
    assert _invoke(["estimate", "--request", "request.json"]) == 2
    assert (
        _invoke(
            [
                "estimate",
                "--snapshot", "snapshot.json",
                "--server", "http://127.0.0.1:9",
                "--request", "request.json",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "snapshot.json" not in captured.out


def test_registered_offline_estimate_keeps_the_snapshot_result(capsys):
    code = _invoke(
        [
            "estimate",
            "--snapshot",
            str(ROOT / "tests/fixtures/ac-v0.2-published-snapshot.json"),
            "--request",
            str(ROOT / "examples/estimate-request.json"),
            "--publisher",
            "pub_sample",
        ]
    )
    assert code == 0
    document = json.loads(capsys.readouterr().out)
    assert document["publisher_id"] == "pub_sample"
    assert document["results"][0]["candidate_id"] == "synthetic-m4"
    assert document["results"][0]["status"] == "ok"
    assert document["results"][0]["metrics"]["cost"] == "0.0177"


def test_data_remote_commands_use_admin_config_and_plan_stays_config_free(monkeypatch, capsys, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    broken = tmp_path / "broken.json"
    broken.write_text('{"admin_token":"' + FILE_ADMIN + '"}', encoding="utf-8")
    monkeypatch.setenv("ACB_CONFIG", str(broken))
    scope = tmp_path / "scope.json"
    batch = tmp_path / "batch"

    def respond(receipt):
        if receipt["path"] == "/v1/catalog":
            return 200, _empty_catalog(), []
        if receipt["path"] == "/v1/capabilities":
            return 200, {"agents": [], "model_efforts": []}, []
        return 500, {"detail": FILE_ADMIN}, []

    with served(respond) as (base, receipts):
        scope.write_text(json.dumps(_agent_scope(base)), encoding="utf-8")
        assert _invoke(["data", "plan", "--mode", "init", "--scope", str(scope), "--out", str(batch)]) == 0
        assert _invoke(["data", "validate", "--batch", str(batch)]) == 0
        assert _invoke(["data", "diff", "--batch", str(batch), "--server", base]) == 3
        captured = capsys.readouterr()
        assert "config_invalid" in captured.err
        assert FILE_ADMIN not in captured.out
        assert FILE_ADMIN not in captured.err
        monkeypatch.delenv("ACB_CONFIG")
        config_path = tmp_path / "good.json"
        _write_real_config(
            config_path,
            server=base,
            admin=FILE_ADMIN,
            read=FILE_READ,
            db=tmp_path / "costbook.sqlite3",
        )
        assert (
            _invoke(["data", "diff", "--batch", str(batch), "--server", base, "--config", str(config_path)])
            == 0
        )
        authorized = [item for item in receipts if item["path"] == "/v1/capabilities"]
        assert authorized
        assert authorized[-1]["authorization"] == f"Bearer {FILE_ADMIN}"
        monkeypatch.setenv("ACB_READ_TOKEN", ENV_READ)
        monkeypatch.setenv("ACB_ADMIN_TOKEN", ENV_ADMIN)
        assert _invoke(["data", "diff", "--batch", str(batch), "--server", base, "--config", str(config_path)]) == 0
        authorized = [item for item in receipts if item["path"] == "/v1/capabilities"]
        assert authorized[-1]["authorization"] == f"Bearer {ENV_ADMIN}"
        assert _invoke(["data", "diff", "--batch", str(batch), "--server", base]) == 0
        authorized = [item for item in receipts if item["path"] == "/v1/capabilities"]
        assert authorized[-1]["authorization"] == f"Bearer {ENV_ADMIN}"
        monkeypatch.delenv("ACB_ADMIN_TOKEN")
        monkeypatch.delenv("ACB_READ_TOKEN")
        assert _invoke(["data", "diff", "--batch", str(batch), "--server", base]) == 2
        captured = capsys.readouterr()
        assert captured.err.splitlines()[-1] == "unauthorized"
        assert FILE_ADMIN not in captured.err
        assert ENV_ADMIN not in captured.err
        assert FILE_READ not in captured.out and FILE_READ not in captured.err
        assert ENV_READ not in captured.out and ENV_READ not in captured.err
