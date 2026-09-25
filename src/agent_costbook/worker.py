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
        return self._publish_observed(observations, now)

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

    def _publish_observed(self, observations: list[Observation], now: str) -> dict:
        active = {
            (
                row["provider"],
                row["channel"],
                row["model"],
                row["effort"],
                row["plan"],
                row["feature_scope"],
            ): row
            for row in self.store.catalog(None)
        }
        records = []
        saw_conflict = False
        for observation in observations:
            for card in observation.cards:
                current = active.get(_card_key(card))
                if (
                    current is not None
                    and current.get("record_status") != "conflict"
                    and same_amounts(current.get("rates") or {}, card.rates)
                ):
                    continue
                records.append(_record(card, current, conflict=False))
            for group in observation.conflicts:
                saw_conflict = True
                current = active.get(_card_key(group[0]))
                records.append(
                    _record(
                        group[0],
                        current,
                        conflict=True,
                        variants=[{"tag": item.tag, "rates": item.rates} for item in group],
                    )
                )
        if not records:
            self._schedule(now, delay=self.interval_seconds, attempt=0, status="unchanged", error=None)
            return {"status": "unchanged", "error": None}
        payload = _payload(observations, records, now)
        created, _ = self.store.create_contribution(payload, _idempotency(payload))
        self.store.publish(created["contribution_id"])
        status = "conflict" if saw_conflict else "published"
        self._schedule(now, delay=self.interval_seconds, attempt=0, status=status, error=None)
        return {"status": status, "error": None}


def _card_key(card) -> tuple:
    return (card.provider, card.channel, card.model, "", "payg", "text")


def _record(card, current: dict | None, *, conflict: bool, variants: list | None = None) -> dict:
    record = {
        "provider": card.provider,
        "channel": card.channel,
        "model": card.model,
        "effort": "",
        "plan": "payg",
        "feature_scope": "text",
        "currency": "USD",
        "rates": {} if conflict else dict(card.rates),
    }
    if current is not None:
        record["base_snapshot_id"] = current["snapshot_id"]
    if conflict:
        record["status"] = "conflict"
        record["conflict_variants"] = variants or []
    return record


def _payload(observations, records, now: str) -> dict:
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
    for record in records:
        record["evidence_indexes"] = list(range(len(evidence)))
    lines = [f"Collected {PINNED_MODEL} at {now}.", "Retrieved pages are stored as data."]
    for record in records:
        if record.get("status") == "conflict":
            lines.append(f"{record['channel']}: conflict")
            continue
        for key in sorted(record["rates"]):
            lines.append(f"{record['channel']} {key}: {record['rates'][key]}")
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


def collect_loop(
    store,
    fetch,
    *,
    clock,
    max_runtime: float,
    interval_seconds: int = 3600,
    max_attempts: int = 3,
    retry_delay_seconds: int = 60,
    stop=None,
) -> list[dict]:
    worker = Worker(
        store,
        fetch=fetch,
        interval_seconds=interval_seconds,
        max_attempts=max_attempts,
        retry_delay_seconds=retry_delay_seconds,
    )
    started = clock.monotonic()
    results = []
    while clock.monotonic() - started < max_runtime:
        if stop is not None and stop():
            break
        results.append(worker.tick(clock.iso()))
        job = store.collector_job(JOB_ID)
        wait = (_clock(job["next_run_at"]) - _clock(clock.iso())).total_seconds()
        remaining = max_runtime - (clock.monotonic() - started)
        if remaining <= 0:
            break
        clock.sleep(min(max(wait, 0.0), remaining))
    return results


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
