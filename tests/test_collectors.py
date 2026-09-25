import json
from pathlib import Path

from agent_costbook.collectors import ParseError, parse_catalog, parse_endpoints
from agent_costbook.fetch_policy import FetchError
from agent_costbook.store import Store
from agent_costbook.worker import Worker

FIXTURES = Path(__file__).parent / "fixtures"
NOW = "2026-09-25T00:00:00+00:00"


def _body(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_recorded_catalog_keeps_missing_prices_unset_and_ignores_instructions():
    observation = parse_catalog(
        _body("openrouter-catalog-recorded.json"),
        url="https://openrouter.ai/api/v1/models",
        retrieved_at=NOW,
    )
    assert observation.kind == "rates"
    assert observation.rates == {
        "uncached_input_per_million": "0.15",
        "cache_read_per_million": "0.075",
        "billed_output_per_million": "0.6",
    }
    assert "cache_write_per_million" not in observation.rates
    assert "0" not in observation.rates.values()
    assert "ignore previous instructions" in observation.evidence_text
    assert observation.parser == "openrouter-catalog-v1"


def test_layout_change_does_not_invent_a_zero_price():
    try:
        parse_catalog(
            b'{"data":{"models":[]}}',
            url="https://openrouter.ai/api/v1/models",
            retrieved_at=NOW,
        )
    except ParseError as exc:
        assert exc.code == "layout"
    else:
        raise AssertionError("changed layout was parsed")


def test_endpoint_disagreement_is_a_conflict_not_a_picked_price():
    observation = parse_endpoints(
        _body("openrouter-endpoints-conflict.json"),
        url="https://openrouter.ai/api/v1/models/openai/gpt-4o-mini/endpoints",
        retrieved_at=NOW,
    )
    assert observation.kind == "conflict"
    amounts = {card["uncached_input_per_million"] for card in observation.variants}
    assert amounts == {"0.15", "0.165"}


def test_failed_fetch_keeps_the_previous_snapshot(tmp_path):
    db = tmp_path / "costbook.sqlite3"
    store = Store(db)
    worker = Worker(store, fetch=lambda url: _ok(url))
    first = worker.tick(NOW)
    assert first["status"] == "published"
    active = store.revision_of(None)

    def fail(url):
        if url.endswith("/endpoints"):
            raise FetchError("http_429")
        return _body("openrouter-catalog-recorded.json")

    worker.fetch = fail
    second = worker.tick("2026-09-25T01:00:00+00:00")
    assert second["status"] == "failed"
    assert second["error"] == "http_429"
    assert store.revision_of(None) == active
    catalog = store.catalog(None)
    rates = next(row["rates"] for row in catalog if row["model"] == "openai/gpt-4o-mini")
    assert rates["uncached_input_per_million"] == "0.15"
    assert "cache_write_per_million" not in rates
    store.close()


def _ok(url: str) -> bytes:
    if url.endswith("/endpoints"):
        return _body("openrouter-endpoints-agree.json")
    return _body("openrouter-catalog-recorded.json")
