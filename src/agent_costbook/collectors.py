from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from agent_costbook.estimates import MILLION, money_text, parse_decimal

PINNED_MODEL = "openai/gpt-4o-mini"
CATALOG_URL = "https://openrouter.ai/api/v1/models"
ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/openai/gpt-4o-mini/endpoints"
CATALOG_PARSER = "openrouter-catalog-v1"
ENDPOINTS_PARSER = "openrouter-endpoints-v1"
MAX_EVIDENCE_CHARS = 256 * 1024
_PRICE_FIELDS = {
    "prompt": "uncached_input_per_million",
    "completion": "billed_output_per_million",
    "input_cache_read": "cache_read_per_million",
    "input_cache_write": "cache_write_per_million",
}


class ParseError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PriceCard:
    provider: str
    channel: str
    model: str
    tag: str
    rates: dict


@dataclass(frozen=True)
class Observation:
    kind: str
    parser: str
    url: str
    retrieved_at: str
    evidence_text: str
    cards: tuple = ()
    conflicts: tuple = ()


def parse_catalog(body: bytes, *, url: str, retrieved_at: str) -> Observation:
    document = _json_object(body)
    rows = document.get("data")
    if not isinstance(rows, list):
        raise ParseError("layout")
    hits = [row for row in rows if isinstance(row, dict) and row.get("id") == PINNED_MODEL]
    if len(hits) != 1 or not isinstance(hits[0].get("pricing"), dict):
        raise ParseError("layout")
    card = PriceCard(
        provider="openai",
        channel="openrouter",
        model=PINNED_MODEL,
        tag="",
        rates=_rates(hits[0]["pricing"]),
    )
    return Observation(
        kind="rates",
        parser=CATALOG_PARSER,
        url=url,
        retrieved_at=retrieved_at,
        evidence_text=_evidence(body, hits[0]),
        cards=(card,),
    )


def parse_endpoints(body: bytes, *, url: str, retrieved_at: str) -> Observation:
    document = _json_object(body)
    data = document.get("data")
    if not isinstance(data, dict) or data.get("id") != PINNED_MODEL:
        raise ParseError("layout")
    endpoints = data.get("endpoints")
    if not isinstance(endpoints, list) or not endpoints:
        raise ParseError("layout")
    grouped: dict[str, list[PriceCard]] = {}
    for endpoint in endpoints:
        if not isinstance(endpoint, dict) or not isinstance(endpoint.get("pricing"), dict):
            raise ParseError("layout")
        tag = endpoint.get("tag") or endpoint.get("provider_name")
        if not isinstance(tag, str) or not tag:
            raise ParseError("layout")
        card = PriceCard(
            provider="openai",
            channel=f"openrouter:{tag}",
            model=PINNED_MODEL,
            tag=tag,
            rates=_rates(endpoint["pricing"]),
        )
        grouped.setdefault(tag, []).append(card)
    cards = []
    conflicts = []
    for group in grouped.values():
        if any(not same_amounts(group[0].rates, item.rates) for item in group[1:]):
            conflicts.append(tuple(group))
        else:
            cards.append(group[0])
    return Observation(
        kind="conflict" if conflicts else "rates",
        parser=ENDPOINTS_PARSER,
        url=url,
        retrieved_at=retrieved_at,
        evidence_text=_evidence(body, data),
        cards=tuple(cards),
        conflicts=tuple(conflicts),
    )


def _json_object(body: bytes) -> dict:
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParseError("layout") from exc
    if not isinstance(document, dict):
        raise ParseError("layout")
    return document


def _rates(pricing: dict) -> dict:
    rates = {}
    for source, target in _PRICE_FIELDS.items():
        if source not in pricing or pricing[source] is None:
            continue
        number = parse_decimal(pricing[source])
        if number is None or number < 0:
            raise ParseError("invalid_price")
        rates[target] = money_text(number * MILLION)
    if "uncached_input_per_million" not in rates and "billed_output_per_million" not in rates:
        raise ParseError("layout")
    return rates


def _evidence(body: bytes, excerpt: dict) -> str:
    if len(body) <= MAX_EVIDENCE_CHARS:
        return body.decode("utf-8")
    payload = {
        "excerpt": excerpt,
        "response_sha256": hashlib.sha256(body).hexdigest(),
        "truncated": True,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def same_amounts(left: dict, right: dict) -> bool:
    if set(left) != set(right):
        return False
    return all(Decimal(left[key]) == Decimal(right[key]) for key in left)
