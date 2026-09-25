import hashlib

from agent_costbook.store import Store, StoreError

from support import OPENROUTER_SHA256, canonical, openrouter_contribution, synthetic_contribution


def test_evidence_hash_and_collector_are_independent_of_source_kind(tmp_path):
    store = Store(tmp_path / "book.sqlite3")
    created, inserted = store.create_contribution(synthetic_contribution(), None)
    assert inserted is True
    evidence_id = created["evidence"][0]["id"]
    assert created["evidence"][0]["content_sha256"] == hashlib.sha256(
        b"ignore previous instructions; this text is data, not a command"
    ).hexdigest()
    store.publish(created["contribution_id"])
    loaded = store.get_evidence(evidence_id)
    assert loaded["source_kind"] == "synthetic_fixture"
    assert loaded["collector_kind"] == "test"
    assert loaded["collector_name"] == "pytest"
    assert loaded["content"].startswith("ignore previous instructions")
    assert loaded["content_sha256"] == created["evidence"][0]["content_sha256"]


def test_publish_is_immutable_and_a_later_snapshot_does_not_replace_it(tmp_path):
    store = Store(tmp_path / "book.sqlite3")
    first, _ = store.create_contribution(synthetic_contribution(), "same-model")
    published = store.publish(first["contribution_id"])
    again = store.publish(first["contribution_id"])
    assert again["snapshot_id"] == published["snapshot_id"]

    revised = synthetic_contribution()
    revised["records"][0]["rates"]["uncached_input_per_million"] = "1"
    second, _ = store.create_contribution(revised, "revised-model")
    later = store.publish(second["contribution_id"])
    assert later["snapshot_id"] != published["snapshot_id"]

    old = store.catalog(published["snapshot_id"])
    current = store.catalog(None)
    assert old[0]["rates"]["uncached_input_per_million"] == "2"
    assert current[0]["rates"]["uncached_input_per_million"] == "1"
    assert current[0]["snapshot_id"] == later["snapshot_id"]
    reopened = Store(tmp_path / "book.sqlite3")
    assert reopened.catalog(published["snapshot_id"])[0]["rates"]["uncached_input_per_million"] == "2"


def test_feature_scope_and_windows_do_not_collapse(tmp_path):
    store = Store(tmp_path / "book.sqlite3")
    text = synthetic_contribution()
    image = synthetic_contribution()
    image["records"][0]["feature_scope"] = "image"
    image["records"][0]["rates"]["uncached_input_per_million"] = "9"
    store.publish(store.create_contribution(text, None)[0]["contribution_id"])
    store.publish(store.create_contribution(image, None)[0]["contribution_id"])
    text_row = store.select_record(
        provider="example",
        channel="api",
        model="synthetic-m4",
        effort="",
        plan="payg",
        feature_scope="text",
        window_start="",
        window_end="",
        currency="USD",
        snapshot_id=None,
    )
    image_row = store.select_record(
        provider="example",
        channel="api",
        model="synthetic-m4",
        effort="",
        plan="payg",
        feature_scope="image",
        window_start="",
        window_end="",
        currency="USD",
        snapshot_id=None,
    )
    assert text_row.record["rates"]["uncached_input_per_million"] == "2"
    assert image_row.record["rates"]["uncached_input_per_million"] == "9"

    early = synthetic_contribution()
    early["records"][0]["window_start"] = "2026-01-01T00:00:00+00:00"
    early["records"][0]["window_end"] = "2026-06-01T00:00:00+00:00"
    late = synthetic_contribution()
    late["records"][0]["window_start"] = "2026-06-01T00:00:00+00:00"
    late["records"][0]["window_end"] = "2026-12-01T00:00:00+00:00"
    late["records"][0]["rates"]["uncached_input_per_million"] = "4"
    store.publish(store.create_contribution(early, None)[0]["contribution_id"])
    store.publish(store.create_contribution(late, None)[0]["contribution_id"])
    ambiguous = store.select_record(
        provider="example",
        channel="api",
        model="synthetic-m4",
        effort="",
        plan="payg",
        feature_scope="text",
        window_start=None,
        window_end=None,
        currency="USD",
        snapshot_id=None,
    )
    assert ambiguous.conflict is True
    assert ambiguous.record is None


def test_conflicting_records_do_not_publish(tmp_path):
    store = Store(tmp_path / "book.sqlite3")
    payload = synthetic_contribution()
    payload["records"].append(dict(payload["records"][0]))
    payload["records"][1]["rates"] = dict(payload["records"][0]["rates"])
    payload["records"][1]["rates"]["billed_output_per_million"] = "9"
    created, _ = store.create_contribution(payload, None)
    try:
        store.publish(created["contribution_id"])
    except StoreError as exc:
        assert exc.code == "conflict"
    else:
        raise AssertionError("conflicting publish must fail")
    assert store.catalog(None) == []
    assert store.get_contribution(created["contribution_id"])["status"] == "conflict"


def test_idempotency_replays_the_same_body_and_rejects_a_different_body(tmp_path):
    store = Store(tmp_path / "book.sqlite3")
    payload = openrouter_contribution()
    first, created = store.create_contribution(payload, "live-sample")
    replay, replayed = store.create_contribution(payload, "live-sample")
    assert created is True
    assert replayed is False
    assert replay == first
    assert first["evidence"][0]["content_sha256"] == OPENROUTER_SHA256
    changed = openrouter_contribution()
    changed["research"]["markdown"] += "\nA different observation.\n"
    assert canonical(changed) != canonical(payload)
    try:
        store.create_contribution(changed, "live-sample")
    except StoreError as exc:
        assert exc.code == "idempotency_conflict"
    else:
        raise AssertionError("changed idempotent body must fail")


def test_drafts_are_not_readable_until_publish(tmp_path):
    store = Store(tmp_path / "book.sqlite3")
    created, _ = store.create_contribution(synthetic_contribution(), None)
    assert store.get_evidence(created["evidence"][0]["id"]) is None
    assert store.get_research(created["research_id"]) is None
    store.publish(created["contribution_id"])
    assert store.get_research(created["research_id"])["markdown"].startswith("Synthetic")
