from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from agent_costbook.collectors import (
    CATALOG_URL,
    ENDPOINTS_URL,
    PINNED_MODEL,
    Observation,
    ParseError,
    parse_catalog,
    parse_endpoints,
    same_amounts,
)
from agent_costbook.fetch_policy import FetchError, default_opener, default_resolve, fetch_public
from agent_costbook.store import StoreError

JOB_ID = "openrouter-public"
SOURCES = (
    {"id": "openrouter-models", "url": CATALOG_URL, "parse": parse_catalog},
    {
        "id": "openrouter-gpt-4o-mini-endpoints",
        "url": ENDPOINTS_URL,
        "parse": parse_endpoints,
    },
)


def _clock(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Worker:
    def __init__(
        self,
        store,
        *,
        fetch,
        interval_seconds: int = 3600,
        max_attempts: int = 3,
        retry_delay_seconds: int = 60,
    ):
        self.store = store
        self.fetch = fetch
        self.interval_seconds = interval_seconds
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds

    def tick(self, now: str) -> dict:
        self.store.ensure_collector_job(
            JOB_ID,
            interval_seconds=self.interval_seconds,
            max_attempts=self.max_attempts,
            now=now,
        )
        job = self.store.collector_job(JOB_ID)
        if _clock(job["next_run_at"]) > _clock(now):
            return {"status": "waiting", "error": job["last_error"]}
        try:
            observations = [
                source["parse"](self.fetch(source["url"]), url=source["url"], retrieved_at=now)
                for source in SOURCES
            ]
        except (FetchError, TimeoutError) as exc:
            return self._fail(now, getattr(exc, "code", "timeout"))
        except ParseError as exc:
            return self._fail(now, exc.code)
        if self._conflicting(observations):
            self._hold_conflict(observations, now)
            self._schedule(now, delay=self.interval_seconds, attempt=0, status="conflict", error=None)
            return {"status": "conflict", "error": None}
        rates = observations[0].rates or {}
        active = self._active()
        if active is not None and same_amounts(active["rates"], rates):
            self._schedule(now, delay=self.interval_seconds, attempt=0, status="unchanged", error=None)
            return {"status": "unchanged", "error": None}
        self._publish(observations, rates, active, now)
        self._schedule(now, delay=self.interval_seconds, attempt=0, status="published", error=None)
        return {"status": "published", "error": None}

    def _fail(self, now: str, code: str) -> dict:
        job = self.store.collector_job(JOB_ID)
        attempt = int(job["attempt"]) + 1
        if attempt >= self.max_attempts:
            self._schedule(now, delay=self.interval_seconds, attempt=0, status="failed", error=code)
        else:
            self._schedule(
                now,
                delay=self.retry_delay_seconds,
                attempt=attempt,
                status="failed",
                error=code,
            )
        return {"status": "failed", "error": code}

    def _schedule(self, now: str, *, delay: int, attempt: int, status: str, error: str | None) -> None:
        self.store.schedule_collector_job(
            JOB_ID,
            now=now,
            delay_seconds=delay,
            attempt=attempt,
            status=status,
            error=error,
        )

    def _conflicting(self, observations: list[Observation]) -> bool:
        if any(item.kind != "rates" or not item.rates for item in observations):
            return True
        first = observations[0].rates
        return any(not same_amounts(first, item.rates or {}) for item in observations[1:])

    def _active(self) -> dict | None:
        selection = self.store.select_record(
            provider="openai",
            channel="openrouter",
            model=PINNED_MODEL,
            effort="",
            plan="payg",
            feature_scope="text",
            window_start=None,
            window_end=None,
            currency="USD",
            snapshot_id=None,
        )
        if selection.conflict or selection.record is None:
            return None
        return selection.record

    def _publish(self, observations, rates: dict, active: dict | None, now: str) -> None:
        payload = _payload(observations, [rates], now, active)
        created, _ = self.store.create_contribution(payload, _idempotency(payload))
        self.store.publish(created["contribution_id"])

    def _hold_conflict(self, observations, now: str) -> None:
        cards = []
        for observation in observations:
            if observation.variants:
                cards.extend(observation.variants)
            elif observation.rates:
                cards.append(observation.rates)
        if len(cards) < 2:
            return
        payload = _payload(observations, cards, now, None)
        created, _ = self.store.create_contribution(payload, _idempotency(payload))
        try:
            self.store.publish(created["contribution_id"])
        except StoreError as exc:
            if exc.code != "conflict":
                raise


def _payload(observations, cards, now: str, active: dict | None) -> dict:
    evidence = [
        {
            "source_kind": "official_api",
            "source_url": item.url,
            "collector_kind": "worker",
            "collector_name": item.parser,
            "content": item.evidence_text,
            "retrieved_at": now,
        }
        for item in observations
    ]
    records = []
    for card in cards:
        record = {
            "provider": "openai",
            "channel": "openrouter",
            "model": PINNED_MODEL,
            "effort": "",
            "plan": "payg",
            "feature_scope": "text",
            "currency": "USD",
            "rates": dict(card),
            "evidence_indexes": list(range(len(evidence))),
        }
        if active is not None:
            record["base_snapshot_id"] = active["snapshot_id"]
        records.append(record)
    lines = [f"Collected {PINNED_MODEL} at {now}.", "Retrieved pages are stored as data."]
    if cards:
        for key in sorted(cards[0]):
            lines.append(f"{key}: {cards[0][key]}")
    return {
        "research": {
            "title": f"OpenRouter {PINNED_MODEL} public collection",
            "markdown": "\n".join(lines) + "\n",
        },
        "evidence": evidence,
        "records": records,
    }


def _idempotency(payload: dict) -> str:
    encoded = json_key(payload)
    return "collector-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def json_key(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def production_fetch(url: str) -> bytes:
    return fetch_public(url, resolve=default_resolve, opener=default_opener).body


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    from pathlib import Path

    from agent_costbook.store import Store, StoreError

    parser = argparse.ArgumentParser(prog="ac collect")
    parser.add_argument("--db", required=True)
    parser.add_argument("--now")
    args = parser.parse_args(argv)
    now = args.now or datetime.now(timezone.utc).isoformat()
    path = Path(args.db)
    try:
        store = Store(path)
    except StoreError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    try:
        result = Worker(store, fetch=production_fetch).tick(now)
    finally:
        store.close()
    print(result["status"])
    return 0 if result["status"] != "failed" else 1
