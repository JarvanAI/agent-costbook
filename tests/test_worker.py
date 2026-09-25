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
    record = next(row for row in store.catalog(None) if row["model"] == "openai/gpt-4o-mini")
    assert record["rates"]["uncached_input_per_million"] == "0.15"
    assert "cache_write_per_million" not in record["rates"]
    evidence = store.get_evidence(record["evidence_ids"][0])
    assert "ignore previous instructions" in evidence["content"]
    store.close()


def test_source_conflict_is_summarized_without_replacing_the_active_snapshot(tmp_path):
    db = tmp_path / "costbook.sqlite3"
    store = Store(db)

    def agree(url):
        if url.endswith("/endpoints"):
            return _body("openrouter-endpoints-agree.json")
        return _body("openrouter-catalog-recorded.json")

    assert _worker(store, agree).tick(NOW)["status"] == "published"
    active = store.revision_of(None)

    def disagree(url):
        if url.endswith("/endpoints"):
            return _body("openrouter-endpoints-conflict.json")
        return _body("openrouter-catalog-recorded.json")

    result = _worker(store, disagree).tick(LATER)
    assert result["status"] == "conflict"
    assert store.revision_of(None) == active
    summary = store.conflict_summaries()
    assert summary
    observed = {
        item["rates"].get("uncached_input_per_million")
        for entry in summary
        for item in entry["rates"]
    }
    assert "0.165" in observed
    assert "0.15" in observed
    store.close()


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


def test_there_is_no_built_in_research_provider(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "empty.sqlite3", admin_token="token"))
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/v1/research-jobs" not in paths
    assert [source["id"] for source in SOURCES] == [
        "openrouter-models",
        "openrouter-gpt-4o-mini-endpoints",
    ]
    assert json.dumps([source["url"] for source in SOURCES]).startswith('["https://')
