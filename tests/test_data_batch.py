import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from agent_costbook.backup import copy_database
from agent_costbook.data_batch import (
    DataError,
    ServiceClient,
    TransportError,
    apply_batch,
    canonical_bytes,
    diff_batch,
    normalize_server,
    plan_batch,
    sha256_bytes,
)
from agent_costbook.store import Store

from support import synthetic_contribution

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "token-do-not-log-7f3a"
AS_OF = "2026-09-28T00:00:00+00:00"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_health(base: str, proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + 20
    last = None
    with httpx.Client(base_url=base, timeout=5, trust_env=False) as client:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(f"service exited {proc.returncode}")
            try:
                response = client.get("/health")
                if response.status_code == 200 and response.json()["status"] == "ok":
                    return
            except httpx.HTTPError as exc:
                last = exc
            time.sleep(0.05)
    raise AssertionError(f"health check failed: {last}")


def _start(db: Path, port: int, token: str = TOKEN) -> subprocess.Popen:
    env = os.environ.copy()
    env["ACB_DB"] = str(db)
    env["ACB_ADMIN_TOKEN"] = token
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
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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


def _ac(args: list[str], *, token: str = TOKEN) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ACB_ADMIN_TOKEN"] = token
    env.pop("ACB_DB", None)
    return subprocess.run(
        [sys.executable, "-m", "agent_costbook.offline", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _scope(server: str, *, agent: bool = True, strengths: str = "edits the batch") -> dict:
    items = [
        {
            "item_id": "synthetic-price",
            "category": "price",
            "collection": "manual",
            "mapping": "Synthetic price kept as text.",
            "candidate": {
                "research": {"title": "Synthetic price", "markdown": "A reviewed example rate.\n"},
                "evidence": [
                    {
                        "source_kind": "synthetic_fixture",
                        "source_url": "fixture://synthetic-price",
                        "collector_kind": "agent",
                        "collector_name": "example",
                        "content": "uncached input is 1 USD per million tokens",
                        "retrieved_at": AS_OF,
                    }
                ],
                "record": {
                    "provider": "example",
                    "channel": "api",
                    "model": "example-small",
                    "effort": "",
                    "plan": "payg",
                    "feature_scope": "text",
                    "currency": "USD",
                    "rates": {
                        "uncached_input_per_million": "1",
                        "billed_output_per_million": "2",
                    },
                },
            },
        },
        {
            "item_id": "needs-a-source",
            "category": "price",
            "collection": "agent",
            "mapping": "No source was supplied.",
        },
        {
            "item_id": "coding-agent-gap",
            "category": "coding_agent",
            "collection": "agent",
        },
    ]
    if agent:
        items.append(
            {
                "item_id": "local-agent",
                "category": "agent",
                "collection": "manual",
                "mapping": "A person wrote this observation.",
                "candidate": {
                    "agent_id": "local-agent",
                    "source": "user_observation",
                    "as_of": AS_OF,
                    "strengths": strengths,
                },
            }
        )
    return {
        "kind": "agent-costbook.data-scope",
        "contract_version": 1,
        "id": "example-run",
        "server": server,
        "visibility": "public",
        "items": items,
    }


def _write_scope(path: Path, server: str, **kwargs) -> None:
    path.write_text(json.dumps(_scope(server, **kwargs)), encoding="utf-8")


def _counts(db: Path) -> tuple[int, int, int, int]:
    connection = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        def count(table: str) -> int:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

        return (
            count("contributions"),
            count("snapshots"),
            count("capability_agent_history"),
            count("capability_model_effort_history"),
        )
    finally:
        connection.close()


def _service(tmp_path: Path):
    db = tmp_path / "service.sqlite3"
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = _start(db, port)
    _wait_health(base, proc)
    return db, base, proc


def _flow(tmp_path: Path, base: str, name: str = "batch"):
    scope = tmp_path / f"{name}-scope.json"
    batch = tmp_path / name
    _write_scope(scope, base)
    planned = _ac(["data", "plan", "--mode", "init", "--scope", str(scope), "--out", str(batch)])
    assert planned.returncode == 0, planned.stderr
    validated = _ac(["data", "validate", "--batch", str(batch)])
    assert validated.returncode == 0, validated.stderr
    diffed = _ac(["data", "diff", "--batch", str(batch), "--server", base])
    assert diffed.returncode == 0, diffed.stderr
    summary = json.loads(diffed.stdout)
    return batch, summary["plan_sha256"]


def test_atomic_write_completes_short_writes_and_keeps_the_previous_file(tmp_path, monkeypatch):
    from agent_costbook.data_batch import atomic_write

    directory = tmp_path / "batch"
    directory.mkdir()
    os.chmod(directory, 0o700)
    target = directory / "journal.json"
    original_bytes = b'{"kind":"agent-costbook.data-journal","operations":{}}'
    target.write_bytes(original_bytes)
    os.chmod(target, 0o600)
    real_write = os.write

    def short_write(descriptor, data):
        chunk = bytes(data[:4])
        return real_write(descriptor, chunk)

    monkeypatch.setattr(os, "write", short_write)
    payload = b'{"operations":{"price":"0123456789abcdefghijklmnopqrstuvwxyz"}}'
    atomic_write(target, payload)
    assert target.read_bytes() == payload

    def fail_write(descriptor, data):
        if bytes(data):
            raise OSError("short device")
        return real_write(descriptor, data)

    monkeypatch.setattr(os, "write", fail_write)
    try:
        atomic_write(target, b"truncated-should-not-replace")
    except OSError:
        pass
    else:
        raise AssertionError("failed write replaced the journal")
    assert target.read_bytes() == payload
    assert not list(directory.glob(".journal.json.*.tmp"))


def test_normalize_server_rejects_credentials_and_non_loopback():
    assert normalize_server("http://127.0.0.1:8080/") == "http://127.0.0.1:8080"
    for value in (
        "http://user:secret@127.0.0.1:8080",
        "http://example.com:8080",
        "http://127.0.0.1:8080/v1",
        "http://127.0.0.1:8080/;x",
    ):
        try:
            normalize_server(value)
        except DataError as exc:
            assert exc.code in {"credential_url", "server"}
        else:
            raise AssertionError(value)


def test_redirect_is_not_followed(tmp_path):
    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://example.com/private")
            self.end_headers()

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        client = ServiceClient(f"http://127.0.0.1:{port}", "unused-token")
        try:
            client.request("GET", "/health")
        except DataError as exc:
            assert exc.code == "redirect"
        else:
            raise AssertionError("redirect was followed")
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert not tmp_path.joinpath("unused").exists()


def test_operator_backup_helper_keeps_its_file_mode(tmp_path):
    source = tmp_path / "source.sqlite3"
    Store(source).close()
    destination = tmp_path / "operator.sqlite3"
    copy_database(source, destination)
    assert destination.stat().st_mode & 0o777 == 0o644


def test_openrouter_fixture_is_collected_and_a_gap_is_not(tmp_path):
    scope = {
        "kind": "agent-costbook.data-scope",
        "contract_version": 1,
        "server": "http://127.0.0.1:9",
        "visibility": "public",
        "items": [
            {
                "item_id": "pinned",
                "category": "price",
                "collection": "openrouter-json",
                "parser": "catalog",
                "fixture": str(ROOT / "tests" / "fixtures" / "openrouter-catalog-recorded.json"),
                "retrieved_at": AS_OF,
            },
            {
                "item_id": "broken",
                "category": "price",
                "collection": "openrouter-json",
                "parser": "catalog",
                "fixture": str(tmp_path / "missing.json"),
                "retrieved_at": AS_OF,
            },
            {
                "item_id": "quota",
                "category": "private_quota",
                "collection": "agent",
                "candidate": {"monthly_price": "20"},
            },
        ],
    }
    path = tmp_path / "scope.json"
    path.write_text(json.dumps(scope), encoding="utf-8")
    out = tmp_path / "batch"
    summary = plan_batch(path, "update", out)
    document = json.loads((out / "batch.json").read_text(encoding="utf-8"))
    collected = [item for item in document["items"] if item["status"] == "collected"]
    assert len(collected) == 1
    assert collected[0]["candidate"]["record"]["model"] == "openai/gpt-4o-mini"
    assert collected[0]["collection"] == "openrouter-json"
    broken = next(item for item in document["items"] if item["item_id"] == "broken")
    assert broken["status"] == "needs_research"
    assert broken["candidate"] is None
    refused = _ac(["data", "validate", "--batch", str(out)])
    assert refused.returncode == 2
    assert refused.stderr.strip() == "invalid_batch"
    assert "unsupported_category" in refused.stdout
    assert summary["counts"]["collected"] == 1


def test_http_plan_apply_verify_and_second_apply_writes_nothing(tmp_path):
    db, base, proc = _service(tmp_path)
    try:
        with httpx.Client(base_url=base, timeout=5, trust_env=False) as client:
            other = synthetic_contribution()
            other["records"][0]["model"] = "already-there"
            created = client.post(
                "/v1/contributions",
                headers={"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "preexisting"},
                json=other,
            )
            assert created.status_code == 201
            published = client.post(
                f"/v1/contributions/{created.json()['contribution_id']}/publish",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert published.status_code == 200
        batch, digest = _flow(tmp_path, base)
        refused = _ac(
            [
                "data",
                "apply",
                "--batch",
                str(batch),
                "--approved-diff-sha256",
                digest,
                "--server",
                base,
            ]
        )
        assert refused.returncode == 2
        assert refused.stderr.strip() == "backup_required"
        assert TOKEN not in refused.stdout
        assert TOKEN not in refused.stderr
        assert _counts(db)[1] == 1

        backup = batch / "backup.sqlite3"
        applied = _ac(
            [
                "data",
                "apply",
                "--batch",
                str(batch),
                "--approved-diff-sha256",
                digest,
                "--server",
                base,
                "--backup-source",
                str(db),
                "--backup-destination",
                str(backup),
            ]
        )
        assert applied.returncode == 0, applied.stderr
        assert TOKEN not in applied.stdout
        assert TOKEN not in applied.stderr
        for path in batch.iterdir():
            if path.suffix == ".json" or path.suffix == ".md":
                assert TOKEN not in path.read_text(encoding="utf-8")
        assert backup.stat().st_mode & 0o777 == 0o600
        assert batch.stat().st_mode & 0o777 == 0o700
        after = _counts(db)
        assert after[1] == 2
        assert after[2] == 1
        with httpx.Client(base_url=base, timeout=5, trust_env=False) as client:
            catalog = client.get("/v1/catalog").json()
            models = {row["model"] for row in catalog["records"]}
            assert models == {"already-there", "example-small"}
            kept = next(row for row in catalog["records"] if row["model"] == "already-there")
            assert kept["rates"]["uncached_input_per_million"]["amount"] == "2"
            agent = client.get(
                "/v1/capabilities/agents",
                params={"agent_id": "local-agent"},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert agent.status_code == 200
            assert agent.json()["strengths"] == "edits the batch"
            assert agent.json()["row_version"] == 1
        verified = _ac(["data", "verify", "--receipt", str(batch / "receipt.json"), "--server", base])
        assert verified.returncode == 0, verified.stderr
        again = _ac(
            [
                "data",
                "apply",
                "--batch",
                str(batch),
                "--approved-diff-sha256",
                digest,
                "--server",
                base,
            ]
        )
        assert again.returncode == 0, again.stderr
        assert _counts(db) == after
        tampered = json.loads((batch / "plan.json").read_text(encoding="utf-8"))
        tampered["operations"][0]["target"] = {"tampered": True}
        (batch / "plan.json").write_text(json.dumps(tampered), encoding="utf-8")
        rejected = _ac(
            [
                "data",
                "apply",
                "--batch",
                str(batch),
                "--approved-diff-sha256",
                digest,
                "--server",
                base,
                "--backup-source",
                str(db),
                "--backup-destination",
                str(batch / "second-backup.sqlite3"),
            ]
        )
        assert rejected.returncode == 2
        assert rejected.stderr.strip() == "plan_hash"
        assert _counts(db) == after
    finally:
        _stop(proc)


def test_preserved_fields_conflict_and_unknown_token_are_not_logged(tmp_path):
    db, base, proc = _service(tmp_path)
    try:
        batch, digest = _flow(tmp_path, base, "first")
        applied = _ac(
            [
                "data",
                "apply",
                "--batch",
                str(batch),
                "--approved-diff-sha256",
                digest,
                "--server",
                base,
                "--backup-source",
                str(db),
                "--backup-destination",
                str(batch / "backup.sqlite3"),
            ]
        )
        assert applied.returncode == 0, applied.stderr
        first_digest = digest
        scope = tmp_path / "update-scope.json"
        _write_scope(scope, base, strengths="edits the batch")
        document = json.loads(scope.read_text(encoding="utf-8"))
        agent = next(item for item in document["items"] if item["item_id"] == "local-agent")
        agent["candidate"] = {
            "agent_id": "local-agent",
            "source": "user_observation",
            "as_of": AS_OF,
            "unsuitable": "does not browse",
        }
        scope.write_text(json.dumps(document), encoding="utf-8")
        updated = tmp_path / "updated"
        planned = _ac(["data", "plan", "--mode", "update", "--scope", str(scope), "--out", str(updated)])
        assert planned.returncode == 0, planned.stderr
        diffed = _ac(["data", "diff", "--batch", str(updated), "--server", base])
        assert diffed.returncode == 0, diffed.stderr
        plan = json.loads((updated / "plan.json").read_text(encoding="utf-8"))
        operation = next(item for item in plan["operations"] if item["category"] == "agent")
        assert operation["action"] == "update"
        assert operation["target"]["strengths"] == "edits the batch"
        assert operation["target"]["unsuitable"] == "does not browse"
        digest = json.loads(diffed.stdout)["plan_sha256"]
        with httpx.Client(base_url=base, timeout=5, trust_env=False) as client:
            conflicted = client.put(
                "/v1/capabilities/agents",
                headers={"Authorization": f"Bearer {TOKEN}"},
                json={
                    "agent_id": "local-agent",
                    "expected_version": 1,
                    "source": "user_observation",
                    "as_of": AS_OF,
                    "strengths": "changed elsewhere",
                },
            )
            assert conflicted.status_code == 200
        before = _counts(db)
        blocked = _ac(
            [
                "data",
                "apply",
                "--batch",
                str(updated),
                "--approved-diff-sha256",
                digest,
                "--server",
                base,
                "--backup-source",
                str(db),
                "--backup-destination",
                str(updated / "backup.sqlite3"),
            ]
        )
        assert blocked.returncode == 3, blocked.stderr
        assert "conflict" in blocked.stdout
        with httpx.Client(base_url=base, timeout=5, trust_env=False) as client:
            agent = client.get(
                "/v1/capabilities/agents",
                params={"agent_id": "local-agent"},
                headers={"Authorization": f"Bearer {TOKEN}"},
            ).json()
        assert agent["strengths"] == "changed elsewhere"
        assert agent["row_version"] == 2
        assert agent.get("unsuitable") in {None, ""}
        assert _counts(db) == before
        wrong = _ac(
            [
                "data",
                "apply",
                "--batch",
                str(batch),
                "--approved-diff-sha256",
                first_digest,
                "--server",
                base,
            ],
            token="another-secret-token",
        )
        combined = wrong.stdout + wrong.stderr + "".join(
            path.read_text(encoding="utf-8")
            for path in batch.rglob("*")
            if path.is_file() and path.suffix in {".json", ".md"}
        )
        assert "another-secret-token" not in combined
        assert wrong.returncode == 2
    finally:
        _stop(proc)


class _Drop(ServiceClient):
    def __init__(self, inner: ServiceClient, predicate):
        self.inner = inner
        self.predicate = predicate
        self.server = inner.server
        self.token = inner.token
        self.timeout = inner.timeout
        self.calls: list[tuple[str, str]] = []

    def request(self, method, path, payload=None, *, auth=False, idempotency_key=None):
        self.calls.append((method, path))
        status, decoded = self.inner.request(
            method,
            path,
            payload,
            auth=auth,
            idempotency_key=idempotency_key,
        )
        if self.predicate(method, path):
            raise TransportError("response_lost")
        return status, decoded


def test_lost_publish_and_capability_responses_do_not_write_twice(tmp_path):
    db, base, proc = _service(tmp_path)
    try:
        scope = tmp_path / "scope.json"
        _write_scope(scope, base, strengths="kept")
        batch = tmp_path / "batch"
        assert plan_batch(scope, "init", batch)["batch_id"]
        inner = ServiceClient(base, TOKEN)
        diff_summary = diff_batch(batch, base, TOKEN, client=inner)
        digest = diff_summary["plan_sha256"]
        dropped_publish = _Drop(
            inner,
            lambda method, path: method == "POST" and path.endswith("/publish"),
        )
        first = apply_batch(
            batch,
            digest,
            base,
            TOKEN,
            backup_source=db,
            backup_destination=batch / "backup.sqlite3",
            client=dropped_publish,
        )
        assert first["exit_code"] == 3
        assert first["counts"].get("uncertain") == 1
        assert _counts(db)[1] == 1
        publish_calls = [
            path for method, path in dropped_publish.calls if method == "POST" and path.endswith("/publish")
        ]
        assert publish_calls == [publish_calls[0]]
        second = apply_batch(batch, digest, base, TOKEN, client=ServiceClient(base, TOKEN))
        assert second["exit_code"] == 0, second
        assert _counts(db)[1] == 1
        resumed = ServiceClient(base, TOKEN)
        third_client = _Drop(resumed, lambda method, path: False)
        third = apply_batch(batch, digest, base, TOKEN, client=third_client)
        assert third["exit_code"] == 0, third
        assert not any(method == "POST" and path.endswith("/publish") for method, path in third_client.calls)
        assert _counts(db) == (
            _counts(db)[0],
            1,
            1,
            0,
        )
        # The capability put was not dropped above because the price publish stopped the run
        # before a later success was required. Repeat on a fresh service for the capability loss.
    finally:
        _stop(proc)

    db, base, proc = _service(tmp_path / "capability")
    try:
        scope = tmp_path / "capability-scope.json"
        _write_scope(scope, base, strengths="kept")
        batch = tmp_path / "capability-batch"
        plan_batch(scope, "init", batch)
        # Remove the price write so this run only loses the capability response.
        document = json.loads((batch / "batch.json").read_text(encoding="utf-8"))
        document["items"] = [item for item in document["items"] if item["category"] == "agent"]
        (batch / "batch.json").write_bytes(canonical_bytes(document) + b"\n")
        inner = ServiceClient(base, TOKEN)
        digest = diff_batch(batch, base, TOKEN, client=inner)["plan_sha256"]
        dropped = _Drop(inner, lambda method, path: method == "PUT")
        first = apply_batch(
            batch,
            digest,
            base,
            TOKEN,
            backup_source=db,
            backup_destination=batch / "backup.sqlite3",
            client=dropped,
        )
        assert first["exit_code"] == 3
        assert _counts(db)[2] == 1
        second = apply_batch(batch, digest, base, TOKEN, client=ServiceClient(base, TOKEN))
        assert second["exit_code"] == 0, second
        assert _counts(db)[2] == 1
        with httpx.Client(base_url=base, timeout=5, trust_env=False) as client:
            agent = client.get(
                "/v1/capabilities/agents",
                params={"agent_id": "local-agent"},
                headers={"Authorization": f"Bearer {TOKEN}"},
            ).json()
        assert agent["row_version"] == 1
        assert agent["strengths"] == "kept"
    finally:
        _stop(proc)


def test_exact_contribution_publish_is_idempotent_when_the_response_never_arrived(tmp_path):
    db, base, proc = _service(tmp_path)
    try:
        scope = tmp_path / "scope.json"
        document = _scope(base, agent=False)
        scope.write_text(json.dumps(document), encoding="utf-8")
        batch = tmp_path / "batch"
        plan_batch(scope, "init", batch)
        inner = ServiceClient(base, TOKEN)
        digest = diff_batch(batch, base, TOKEN, client=inner)["plan_sha256"]

        class LoseBeforePublish(ServiceClient):
            def __init__(self, wrapped):
                self.inner = wrapped
                self.server = wrapped.server
                self.token = wrapped.token
                self.timeout = wrapped.timeout
                self.lost = False

            def request(self, method, path, payload=None, *, auth=False, idempotency_key=None):
                if method == "POST" and path.endswith("/publish") and not self.lost:
                    self.lost = True
                    raise TransportError("response_lost")
                return self.inner.request(
                    method,
                    path,
                    payload,
                    auth=auth,
                    idempotency_key=idempotency_key,
                )

        first = apply_batch(
            batch,
            digest,
            base,
            TOKEN,
            backup_source=db,
            backup_destination=batch / "backup.sqlite3",
            client=LoseBeforePublish(inner),
        )
        assert first["exit_code"] == 3
        assert _counts(db)[1] == 0
        second = apply_batch(
            batch,
            digest,
            base,
            TOKEN,
            backup_source=db,
            backup_destination=batch / "backup-retry.sqlite3",
            client=ServiceClient(base, TOKEN),
        )
        assert second["exit_code"] == 0, second
        assert _counts(db)[1] == 1
        third = apply_batch(batch, digest, base, TOKEN, client=ServiceClient(base, TOKEN))
        assert third["exit_code"] == 0, third
        assert _counts(db)[1] == 1
    finally:
        _stop(proc)


def test_duplicate_normalized_identities_and_unknown_capability_fields(tmp_path):
    scope = _scope("http://127.0.0.1:9", agent=False)
    scope["items"].append(
        {
            "item_id": "same-price-again",
            "category": "price",
            "collection": "manual",
            "candidate": scope["items"][0]["candidate"],
        }
    )
    scope["items"].extend(
        [
            {
                "item_id": "effort-blank",
                "category": "model_effort",
                "collection": "manual",
                "candidate": {
                    "provider": "openai",
                    "model": "openai/gpt-4o-mini",
                    "effort": "",
                    "source": "user_observation",
                    "as_of": AS_OF,
                },
            },
            {
                "item_id": "effort-null",
                "category": "model_effort",
                "collection": "manual",
                "candidate": {
                    "provider": "openai",
                    "model": "openai/gpt-4o-mini",
                    "source": "user_observation",
                    "as_of": AS_OF,
                },
            },
        ]
    )
    path = tmp_path / "scope.json"
    path.write_text(json.dumps(scope), encoding="utf-8")
    out = tmp_path / "batch"
    plan_batch(path, "init", out)
    refused = _ac(["data", "validate", "--batch", str(out)])
    assert refused.returncode == 2
    assert refused.stderr.strip() == "invalid_batch"
    assert refused.stdout.count("duplicate_identity") >= 2
    unknown = _scope("http://127.0.0.1:9", agent=True)
    unknown["items"][-1]["candidate"]["notes"] = "drop me"
    unknown_path = tmp_path / "unknown.json"
    unknown_path.write_text(json.dumps(unknown), encoding="utf-8")
    unknown_out = tmp_path / "unknown-batch"
    plan_batch(unknown_path, "init", unknown_out)
    rejected = _ac(["data", "validate", "--batch", str(unknown_out)])
    assert rejected.returncode == 2
    assert "unknown_field" in rejected.stdout
    stored = json.loads((unknown_out / "batch.json").read_text(encoding="utf-8"))
    agent = next(item for item in stored["items"] if item["item_id"] == "local-agent")
    assert "notes" in agent["candidate"]


def test_rediff_drops_the_previous_contribution_id(tmp_path):
    db, base, proc = _service(tmp_path)
    try:
        scope = tmp_path / "scope.json"
        scope.write_text(json.dumps(_scope(base, agent=False)), encoding="utf-8")
        batch = tmp_path / "batch"
        plan_batch(scope, "init", batch)
        inner = ServiceClient(base, TOKEN)
        digest = diff_batch(batch, base, TOKEN, client=inner)["plan_sha256"]

        class LoseBeforePublish(ServiceClient):
            def __init__(self, wrapped):
                self.inner = wrapped
                self.server = wrapped.server
                self.token = wrapped.token
                self.timeout = wrapped.timeout
                self.lost = False

            def request(self, method, path, payload=None, *, auth=False, idempotency_key=None):
                if method == "POST" and path.endswith("/publish") and not self.lost:
                    self.lost = True
                    raise TransportError("response_lost")
                return self.inner.request(
                    method, path, payload, auth=auth, idempotency_key=idempotency_key
                )

        lost = apply_batch(
            batch,
            digest,
            base,
            TOKEN,
            backup_source=db,
            backup_destination=batch / "backup.sqlite3",
            client=LoseBeforePublish(inner),
        )
        assert lost["exit_code"] == 3
        journal = json.loads((batch / "journal.json").read_text(encoding="utf-8"))
        old_id = next(item["contribution_id"] for item in journal["operations"].values())
        document = json.loads((batch / "batch.json").read_text(encoding="utf-8"))
        price = next(item for item in document["items"] if item["item_id"] == "synthetic-price")
        price["candidate"]["record"]["rates"]["uncached_input_per_million"] = "9"
        (batch / "batch.json").write_bytes(canonical_bytes(document) + b"\n")
        revised = diff_batch(batch, base, TOKEN, client=ServiceClient(base, TOKEN))
        assert revised["plan_sha256"] != digest
        rebound = json.loads((batch / "journal.json").read_text(encoding="utf-8"))
        assert rebound["plan_sha256"] == revised["plan_sha256"]
        assert rebound["server"]["publisher_id"]
        assert rebound["operations"] == {}
        applied = apply_batch(
            batch,
            revised["plan_sha256"],
            base,
            TOKEN,
            backup_source=db,
            backup_destination=batch / "backup-revised.sqlite3",
            client=ServiceClient(base, TOKEN),
        )
        assert applied["exit_code"] == 0, applied
        fresh = json.loads((batch / "journal.json").read_text(encoding="utf-8"))
        new_ids = [item.get("contribution_id") for item in fresh["operations"].values()]
        assert old_id not in new_ids
        with httpx.Client(base_url=base, timeout=5, trust_env=False) as client:
            catalog = client.get("/v1/catalog").json()
        row = next(item for item in catalog["records"] if item["model"] == "example-small")
        assert row["rates"]["uncached_input_per_million"]["amount"] == "9"
    finally:
        _stop(proc)


def test_uncertain_capability_conflicts_unless_the_next_version_matches(tmp_path):
    db, base, proc = _service(tmp_path)
    try:
        scope = tmp_path / "scope.json"
        scope.write_text(json.dumps(_scope(base, agent=True)), encoding="utf-8")
        batch = tmp_path / "batch"
        plan_batch(scope, "init", batch)
        document = json.loads((batch / "batch.json").read_text(encoding="utf-8"))
        document["items"] = [item for item in document["items"] if item["category"] == "agent"]
        (batch / "batch.json").write_bytes(canonical_bytes(document) + b"\n")
        digest = diff_batch(batch, base, TOKEN, client=ServiceClient(base, TOKEN))["plan_sha256"]
        plan = json.loads((batch / "plan.json").read_text(encoding="utf-8"))
        operation = next(item for item in plan["operations"] if item["category"] == "agent")
        journal = {
            "kind": "agent-costbook.data-journal",
            "contract_version": 1,
            "plan_sha256": digest,
            "server": plan["server"],
            "operations": {operation["op_id"]: {"state": "uncertain", "phase": "put"}},
        }
        (batch / "journal.json").write_text(json.dumps(journal), encoding="utf-8")
        blocked = apply_batch(batch, digest, base, TOKEN, client=ServiceClient(base, TOKEN))
        assert blocked["exit_code"] == 3, blocked
        assert blocked["counts"].get("conflict") == 1
        assert _counts(db)[2] == 0
        (batch / "journal.json").unlink()

        created = apply_batch(
            batch,
            digest,
            base,
            TOKEN,
            backup_source=db,
            backup_destination=batch / "backup.sqlite3",
            client=ServiceClient(base, TOKEN),
        )
        assert created["exit_code"] == 0, created
        plan["operations"][0]["action"] = "update"
        plan["operations"][0]["baseline"] = {"row_version": 1}
        plan["operations"][0]["reason"] = "candidate"
        encoded = canonical_bytes(plan)
        digest = sha256_bytes(encoded)
        (batch / "plan.json").write_bytes(encoded + b"\n")
        journal["plan_sha256"] = digest
        journal["operations"] = {operation["op_id"]: {"state": "uncertain", "phase": "put"}}
        (batch / "journal.json").write_text(json.dumps(journal), encoding="utf-8")
        same_body = apply_batch(batch, digest, base, TOKEN, client=ServiceClient(base, TOKEN))
        assert same_body["exit_code"] == 3, same_body
        assert same_body["counts"].get("conflict") == 1
        with httpx.Client(base_url=base, timeout=5, trust_env=False) as client:
            agent = client.get(
                "/v1/capabilities/agents",
                params={"agent_id": "local-agent"},
                headers={"Authorization": f"Bearer {TOKEN}"},
            ).json()
        assert agent["row_version"] == 1
        assert agent["strengths"] == "edits the batch"
    finally:
        _stop(proc)


def test_partial_capability_update_keeps_required_metadata(tmp_path):
    db, base, proc = _service(tmp_path)
    try:
        scope = tmp_path / "scope.json"
        scope.write_text(json.dumps(_scope(base)), encoding="utf-8")
        batch = tmp_path / "batch"
        plan_batch(scope, "init", batch)
        document = json.loads((batch / "batch.json").read_text(encoding="utf-8"))
        document["items"] = [item for item in document["items"] if item["category"] == "agent"]
        (batch / "batch.json").write_bytes(canonical_bytes(document) + b"\n")
        digest = diff_batch(batch, base, TOKEN, client=ServiceClient(base, TOKEN))["plan_sha256"]
        applied = apply_batch(
            batch,
            digest,
            base,
            TOKEN,
            backup_source=db,
            backup_destination=batch / "backup.sqlite3",
            client=ServiceClient(base, TOKEN),
        )
        assert applied["exit_code"] == 0, applied
        partial = _scope(base, agent=True)
        partial["items"] = [
            {
                "item_id": "local-agent",
                "category": "agent",
                "collection": "manual",
                "candidate": {"agent_id": "local-agent", "unsuitable": "does not browse"},
            }
        ]
        partial_path = tmp_path / "partial.json"
        partial_path.write_text(json.dumps(partial), encoding="utf-8")
        updated = tmp_path / "updated"
        plan_batch(partial_path, "update", updated)
        assert _ac(["data", "validate", "--batch", str(updated)]).returncode == 0
        diffed = diff_batch(updated, base, TOKEN, client=ServiceClient(base, TOKEN))
        operation = json.loads((updated / "plan.json").read_text(encoding="utf-8"))["operations"][0]
        assert operation["action"] == "update"
        assert operation["target"]["source"] == "user_observation"
        assert operation["target"]["as_of"] == AS_OF
        assert operation["target"]["strengths"] == "edits the batch"
        assert operation["target"]["unsuitable"] == "does not browse"
        written = apply_batch(
            updated,
            diffed["plan_sha256"],
            base,
            TOKEN,
            backup_source=db,
            backup_destination=updated / "backup.sqlite3",
            client=ServiceClient(base, TOKEN),
        )
        assert written["exit_code"] == 0, written
    finally:
        _stop(proc)


def test_backup_must_match_publisher_snapshot_and_capabilities(tmp_path):
    db, base, proc = _service(tmp_path)
    try:
        scope = tmp_path / "scope.json"
        scope.write_text(json.dumps(_scope(base, agent=False)), encoding="utf-8")
        batch = tmp_path / "batch"
        plan_batch(scope, "init", batch)
        digest = diff_batch(batch, base, TOKEN, client=ServiceClient(base, TOKEN))["plan_sha256"]
        other = tmp_path / "other.sqlite3"
        Store(other).close()
        try:
            apply_batch(
                batch,
                digest,
                base,
                TOKEN,
                backup_source=other,
                backup_destination=batch / "wrong.sqlite3",
                client=ServiceClient(base, TOKEN),
            )
        except DataError as exc:
            assert exc.code == "backup_unverified"
        else:
            raise AssertionError("wrong database authorized a write")
        assert _counts(db)[1] == 0

        agent_scope = tmp_path / "agent-scope.json"
        agent_scope.write_text(json.dumps(_scope(base, agent=True)), encoding="utf-8")
        agent_batch = tmp_path / "agent-batch"
        plan_batch(agent_scope, "init", agent_batch)
        agent_doc = json.loads((agent_batch / "batch.json").read_text(encoding="utf-8"))
        agent_doc["items"] = [item for item in agent_doc["items"] if item["category"] == "agent"]
        (agent_batch / "batch.json").write_bytes(canonical_bytes(agent_doc) + b"\n")
        agent_digest = diff_batch(agent_batch, base, TOKEN, client=ServiceClient(base, TOKEN))[
            "plan_sha256"
        ]
        created = apply_batch(
            agent_batch,
            agent_digest,
            base,
            TOKEN,
            backup_source=db,
            backup_destination=agent_batch / "backup.sqlite3",
            client=ServiceClient(base, TOKEN),
        )
        assert created["exit_code"] == 0, created
        stale = tmp_path / "stale.sqlite3"
        copy_database(db, stale)
        with httpx.Client(base_url=base, timeout=5, trust_env=False) as client:
            changed = client.put(
                "/v1/capabilities/agents",
                headers={"Authorization": f"Bearer {TOKEN}"},
                json={
                    "agent_id": "local-agent",
                    "expected_version": 1,
                    "source": "user_observation",
                    "as_of": AS_OF,
                    "strengths": "changed elsewhere",
                },
            )
            assert changed.status_code == 200
            catalog = client.get("/v1/catalog").json()
        before = _counts(db)
        partial = {
            "kind": "agent-costbook.data-scope",
            "contract_version": 1,
            "server": base,
            "visibility": "public",
            "items": [
                {
                    "item_id": "local-agent",
                    "category": "agent",
                    "collection": "manual",
                    "candidate": {"agent_id": "local-agent", "unsuitable": "later"},
                }
            ],
        }
        partial_path = tmp_path / "partial.json"
        partial_path.write_text(json.dumps(partial), encoding="utf-8")
        partial_batch = tmp_path / "partial-batch"
        plan_batch(partial_path, "update", partial_batch)
        partial_digest = diff_batch(partial_batch, base, TOKEN, client=ServiceClient(base, TOKEN))[
            "plan_sha256"
        ]
        try:
            apply_batch(
                partial_batch,
                partial_digest,
                base,
                TOKEN,
                backup_source=stale,
                backup_destination=partial_batch / "stale-copy.sqlite3",
                client=ServiceClient(base, TOKEN),
            )
        except DataError as exc:
            assert exc.code == "backup_unverified"
        else:
            raise AssertionError("stale capability copy authorized a write")
        assert _counts(db) == before

        loose = tmp_path / "loose"
        loose.mkdir()
        os.chmod(loose, 0o755)
        exposed = loose / "copy.sqlite3"
        copy_database(db, exposed)
        os.chmod(exposed, 0o644)
        receipt = {
            "kind": "agent-costbook.backup-receipt",
            "contract_version": 1,
            "method": "operator",
            "server": {
                "url": base if base.count(":") == 2 else base,
                "publisher_id": catalog["publisher_id"],
            },
            "data_version": catalog["data_version"],
            "content_sha256": catalog["content_sha256"],
            "destination": str(exposed),
            "sha256": sha256_bytes(exposed.read_bytes()),
            "created_at": AS_OF,
        }
        # The plan server is normalized. Use the plan's server block.
        plan_server = json.loads((batch / "plan.json").read_text(encoding="utf-8"))["server"]
        receipt["server"] = {"url": plan_server["url"], "publisher_id": plan_server["publisher_id"]}
        receipt_path = tmp_path / "receipt.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        try:
            apply_batch(
                batch,
                digest,
                base,
                TOKEN,
                backup_receipt=receipt_path,
                client=ServiceClient(base, TOKEN),
            )
        except DataError as exc:
            assert exc.code == "backup_permissions"
        else:
            raise AssertionError("loose backup authorized a write")

        private = tmp_path / "private"
        private.mkdir()
        os.chmod(private, 0o700)
        wrong = private / "wrong.sqlite3"
        copy_database(other, wrong)
        os.chmod(wrong, 0o600)
        receipt["destination"] = str(wrong)
        receipt["sha256"] = sha256_bytes(wrong.read_bytes())
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        try:
            apply_batch(
                batch,
                digest,
                base,
                TOKEN,
                backup_receipt=receipt_path,
                client=ServiceClient(base, TOKEN),
            )
        except DataError as exc:
            assert exc.code == "backup_unverified"
        else:
            raise AssertionError("wrong private database authorized a write")
        assert _counts(db) == before
    finally:
        _stop(proc)


def test_verify_rejects_incomplete_duplicate_and_bad_versions(tmp_path):
    db, base, proc = _service(tmp_path)
    try:
        batch, digest = _flow(tmp_path, base, "verify")
        applied = _ac(
            [
                "data",
                "apply",
                "--batch",
                str(batch),
                "--approved-diff-sha256",
                digest,
                "--server",
                base,
                "--backup-source",
                str(db),
                "--backup-destination",
                str(batch / "backup.sqlite3"),
            ]
        )
        assert applied.returncode == 0, applied.stderr
        verified = _ac(["data", "verify", "--receipt", str(batch / "receipt.json"), "--server", base])
        assert verified.returncode == 0, verified.stderr
        receipt_path = batch / "receipt.json"
        original = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert len(original["results"]) > 1

        def check(mutated: dict) -> subprocess.CompletedProcess[str]:
            receipt_path.write_bytes(canonical_bytes(mutated) + b"\n")
            return _ac(["data", "verify", "--receipt", str(receipt_path), "--server", base])

        empty = json.loads(json.dumps(original))
        empty["results"] = []
        rejected = check(empty)
        assert rejected.returncode == 3
        assert '"matches":false' in rejected.stdout.replace(" ", "")
        truncated = json.loads(json.dumps(original))
        truncated["results"] = original["results"][:-1]
        assert check(truncated).returncode == 3
        duplicated = json.loads(json.dumps(original))
        duplicated["results"] = [*original["results"], original["results"][0]]
        assert check(duplicated).returncode == 3
        wrong_version = json.loads(json.dumps(original))
        for row in wrong_version["results"]:
            if row["outcome"] == "created" and row["category"] == "agent":
                row["row_version"] = 99
        assert check(wrong_version).returncode == 3
        receipt_path.write_bytes(canonical_bytes(original) + b"\n")
        assert _ac(["data", "verify", "--receipt", str(receipt_path), "--server", base]).returncode == 0
    finally:
        _stop(proc)
