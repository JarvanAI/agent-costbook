import json
import threading

from agent_costbook.api import create_app
from agent_costbook.fetch_policy import FetchError
from agent_costbook.settings import Settings
from agent_costbook.store import Store
from agent_costbook.worker import SOURCES, Worker
from test_collectors import _body

NOW = "2026-09-25T00:00:00+00:00"
RETRY = "2026-09-25T00:01:00+00:00"
LATER = "2026-09-25T02:00:00+00:00"


def _worker(store, fetch, **kwargs):
    return Worker(
        store,
        fetch=fetch,
        interval_seconds=3600,
        max_attempts=2,
        retry_delay_seconds=60,
        **kwargs,
    )


def test_schedule_survives_reopen_and_stops_after_the_retry_budget(tmp_path):
    db = tmp_path / "costbook.sqlite3"
    calls = {"count": 0}

    def fail(url):
        calls["count"] += 1
        raise FetchError("timeout")

    store = Store(db)
    first = _worker(store, fail).tick(NOW)
    assert first["status"] == "failed"
    assert store.revision_of(None) is None
    assert calls["count"] == 1
    store.close()

    store = Store(db)
    waiting = _worker(store, fail).tick("2026-09-25T00:00:30+00:00")
    assert waiting["status"] == "waiting"
    assert calls["count"] == 1
    second = _worker(store, fail).tick(RETRY)
    assert second["status"] == "failed"
    assert store.revision_of(None) is None
    assert calls["count"] == 2
    held = _worker(store, fail).tick("2026-09-25T00:30:00+00:00")
    assert held["status"] == "waiting"
    assert calls["count"] == 2
    store.close()


def test_agreeing_sources_publish_once_and_a_repeat_does_not_churn(tmp_path):
    db = tmp_path / "costbook.sqlite3"
    store = Store(db)

    def fetch(url):
        if url.endswith("/endpoints"):
            return _body("openrouter-endpoints-agree.json")
        return _body("openrouter-catalog-recorded.json")

    published = _worker(store, fetch).tick(NOW)
    assert published["status"] == "published"
    revision = store.revision_of(None)
    again = _worker(store, fetch).tick(LATER)
    assert again["status"] == "unchanged"
    assert store.revision_of(None) == revision
    record = next(row for row in store.catalog(None) if row["channel"] == "openrouter")
    assert record["rates"]["uncached_input_per_million"] == "0.15"
    assert "cache_write_per_million" not in record["rates"]
    evidence = store.get_evidence(record["evidence_ids"][0])
    assert "ignore previous instructions" in evidence["content"]
    store.close()


def test_region_prices_publish_as_separate_channels(tmp_path):
    db = tmp_path / "costbook.sqlite3"
    store = Store(db)

    def fetch(url):
        if url.endswith("/endpoints"):
            return _body("openrouter-endpoints-conflict.json")
        return _body("openrouter-catalog-recorded.json")

    assert _worker(store, fetch).tick(NOW)["status"] == "published"
    channels = {row["channel"]: row["rates"]["uncached_input_per_million"] for row in store.catalog(None)}
    assert channels["openrouter"] == "0.15"
    assert channels["openrouter:openai"] == "0.15"
    assert channels["openrouter:azure/swedencentral"] == "0.165"
    assert store.conflict_summaries() == []
    store.close()


def test_same_channel_conflict_publishes_a_new_version_and_keeps_the_old_bytes(tmp_path):
    db = tmp_path / "costbook.sqlite3"
    store = Store(db)

    def agree(url):
        if url.endswith("/endpoints"):
            return _body("openrouter-endpoints-agree.json")
        return _body("openrouter-catalog-recorded.json")

    assert _worker(store, agree).tick(NOW)["status"] == "published"
    before = _export(db, "1")
    assert json.loads(before)["conflicts"] == []

    def clash(url):
        if url.endswith("/endpoints"):
            return _body("openrouter-endpoints-same-tag.json")
        return _body("openrouter-catalog-recorded.json")

    result = _worker(store, clash).tick(LATER)
    assert result["status"] == "conflict"
    assert store.revision_of(None) == 2
    assert _export(db, "1") == before
    latest = json.loads(_export(db, "2"))
    assert latest["conflicts"]
    assert latest["conflicts"][0]["channel"] == "openrouter:openai"
    amounts = {
        variant["rates"]["uncached_input_per_million"] for variant in latest["conflicts"][0]["variants"]
    }
    assert amounts == {"0.15", "0.2"}
    conflict_row = next(row for row in latest["records"] if row["channel"] == "openrouter:openai")
    assert conflict_row["status"] == "conflict"
    assert conflict_row["rates"] is None
    store.close()


def _export(db, version: str) -> bytes:
    import subprocess
    import sys
    from pathlib import Path

    result = subprocess.run(
        [sys.executable, "-m", "agent_costbook.export", "--db", str(db), "--data-version", version],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_concurrent_publish_does_not_leave_half_a_snapshot(tmp_path):
    db = tmp_path / "costbook.sqlite3"
    store = Store(db)
    pairs = []
    for index in (1, 2):
        created, _ = store.create_contribution(
            {
                "research": {"title": f"pair {index}", "markdown": "two rows\n"},
                "evidence": [
                    {
                        "source_kind": "synthetic_fixture",
                        "source_url": "fixture://pair",
                        "collector_kind": "test",
                        "collector_name": "pytest",
                        "content": f"row {index}",
                        "retrieved_at": NOW,
                    }
                ],
                "records": [
                    {
                        "provider": "example",
                        "channel": "api",
                        "model": f"left-{index}",
                        "plan": "payg",
                        "feature_scope": "text",
                        "currency": "USD",
                        "rates": {"uncached_input_per_million": "1"},
                        "evidence_indexes": [0],
                    },
                    {
                        "provider": "example",
                        "channel": "api",
                        "model": f"right-{index}",
                        "plan": "payg",
                        "feature_scope": "text",
                        "currency": "USD",
                        "rates": {"uncached_input_per_million": "2"},
                        "evidence_indexes": [0],
                    },
                ],
            },
            f"pair-{index}",
        )
        pairs.append(created["contribution_id"])

    errors = []

    def publish(contribution_id):
        try:
            store.publish(contribution_id)
        except Exception as exc:  # pragma: no cover - the assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=publish, args=(contribution_id,)) for contribution_id in pairs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    for contribution_id in pairs:
        rows = [
            row
            for row in store.catalog(None)
            if row["contribution_id"] == contribution_id
        ]
        assert len(rows) == 2
        assert len({row["snapshot_id"] for row in rows}) == 1
    store.close()


def test_dns_failure_keeps_the_snapshot_and_schedules_a_retry(tmp_path):
    import socket

    from agent_costbook.fetch_policy import fetch_public

    db = tmp_path / "dns.sqlite3"
    store = Store(db)

    def agree(url):
        if url.endswith("/endpoints"):
            return _body("openrouter-endpoints-agree.json")
        return _body("openrouter-catalog-recorded.json")

    assert _worker(store, agree).tick(NOW)["status"] == "published"
    revision = store.revision_of(None)

    def fail_dns(url):
        def resolve(host):
            raise socket.gaierror(socket.EAI_AGAIN, "temporary")

        return fetch_public(url, resolve=resolve, opener=lambda *args: None).body

    failed = _worker(store, fail_dns).tick(LATER)
    assert failed["status"] == "failed"
    assert failed["error"] == "dns"
    assert store.revision_of(None) == revision
    job = store.collector_job("openrouter-public")
    assert job["attempt"] == 1
    assert job["next_run_at"] > LATER
    store.close()


def test_bounded_loop_retries_from_the_saved_schedule_and_stops(tmp_path):
    from datetime import datetime, timedelta

    from agent_costbook.worker import collect_loop

    class Clock:
        def __init__(self):
            self.now = datetime.fromisoformat(NOW)
            self.mono = 0.0

        def iso(self):
            return self.now.isoformat()

        def monotonic(self):
            return self.mono

        def sleep(self, seconds):
            self.now += timedelta(seconds=seconds)
            self.mono += seconds

    calls = {"count": 0}

    def fail(url):
        calls["count"] += 1
        raise FetchError("timeout")

    store = Store(tmp_path / "loop.sqlite3")
    results = collect_loop(
        store,
        fail,
        clock=Clock(),
        max_runtime=150,
        interval_seconds=3600,
        max_attempts=2,
        retry_delay_seconds=60,
    )
    assert [item["status"] for item in results] == ["failed", "failed"]
    assert calls["count"] == 2
    job = store.collector_job("openrouter-public")
    assert job["last_status"] == "failed"
    assert job["next_run_at"] > "2026-09-25T00:02:00+00:00"
    store.close()


def test_bounded_loop_checks_stop_during_a_long_wait(tmp_path):
    from datetime import datetime, timedelta

    from agent_costbook.worker import collect_loop

    class Clock:
        def __init__(self):
            self.now = datetime.fromisoformat("2026-09-25T00:00:00+00:00")
            self.mono = 0.0
            self.slices = []

        def iso(self):
            return self.now.isoformat()

        def monotonic(self):
            return self.mono

        def sleep(self, seconds):
            self.slices.append(seconds)
            self.now += timedelta(seconds=seconds)
            self.mono += seconds

    store = Store(tmp_path / "stop.sqlite3")
    store.ensure_collector_job(
        "openrouter-public",
        interval_seconds=3600,
        max_attempts=3,
        now="2026-09-25T00:00:00+00:00",
    )
    store.schedule_collector_job(
        "openrouter-public",
        now="2099-01-01T00:00:00+00:00",
        delay_seconds=0,
        attempt=0,
        status="waiting",
        error=None,
    )
    clock = Clock()
    results = collect_loop(
        store,
        lambda url: b"",
        clock=clock,
        max_runtime=60,
        stop=lambda: clock.mono >= 1,
    )
    assert [item["status"] for item in results] == ["waiting"]
    assert clock.slices
    assert max(clock.slices) <= 0.5
    assert clock.mono <= 1.5
    store.close()


def test_sigterm_stops_a_long_collect_wait(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    import time
    from pathlib import Path

    db = tmp_path / "sigterm.sqlite3"
    store = Store(db)
    store.ensure_collector_job(
        "openrouter-public",
        interval_seconds=3600,
        max_attempts=3,
        now="2026-09-25T00:00:00+00:00",
    )
    store.schedule_collector_job(
        "openrouter-public",
        now="2099-01-01T00:00:00+00:00",
        delay_seconds=0,
        attempt=0,
        status="waiting",
        error=None,
    )
    store.close()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agent_costbook.offline",
            "collect",
            "--db",
            str(db),
            "--max-runtime",
            "60",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        time.sleep(0.5)
        assert proc.poll() is None
        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=1.5)
    except Exception:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        raise
    assert proc.returncode == 0, stderr
    assert stdout.decode().strip() == "waiting"


def test_collect_once_does_not_fetch_before_the_saved_next_run(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    db = tmp_path / "scheduled.sqlite3"
    store = Store(db)
    store.ensure_collector_job(
        "openrouter-public",
        interval_seconds=3600,
        max_attempts=3,
        now=NOW,
    )
    store.schedule_collector_job(
        "openrouter-public",
        now=NOW,
        delay_seconds=3600,
        attempt=1,
        status="failed",
        error="timeout",
    )
    store.close()
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_costbook.offline",
            "collect",
            "--once",
            "--db",
            str(db),
            "--now",
            "2026-09-25T00:10:00+00:00",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
        env=os.environ.copy(),
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.decode().strip() == "waiting"


def test_there_is_no_built_in_research_provider(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "empty.sqlite3", admin_token="token"))
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/v1/research-jobs" not in paths
    assert [source["id"] for source in SOURCES] == [
        "openrouter-models",
        "openrouter-gpt-4o-mini-endpoints",
    ]
    assert json.dumps([source["url"] for source in SOURCES]).startswith('["https://')
