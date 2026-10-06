"""Initialize and update price and capability rows through the current HTTP API.

The batch directory is local operator data. Approval is the sha256 of the
canonical apply plan, bound to the normalized server URL and publisher id.
There is no contribution-status route. A price publish is confirmed from
``GET /v1/evidence`` when the journal already holds that contribution id, or by
calling publish again for that same id. ``Store.publish`` returns the existing
snapshot and does not create another one. An unknown contribution is not
replayed. Public catalog rows do not carry the internal snapshot id;
``POST /v1/estimates`` returns it as ``record_snapshot_id``.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import sqlite3
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from pydantic import ValidationError

from agent_costbook.backup import BackupError, copy_database
from agent_costbook.capabilities import AgentCapabilityIn, ModelEffortIn
from agent_costbook.export import ExportError, catalog_document
from agent_costbook.store import Store, StoreError
from agent_costbook.collectors import (
    CATALOG_URL,
    ENDPOINTS_URL,
    PINNED_MODEL,
    ParseError,
    parse_catalog,
    parse_endpoints,
)
from agent_costbook.estimates import parse_decimal
from agent_costbook.models import ContributionIn

CONTRACT_VERSION = 1
SUPPORTED_CATEGORIES = frozenset({"price", "agent", "model_effort"})
READY = frozenset({"candidate", "collected"})
REDIRECTS = frozenset({301, 302, 303, 307, 308})
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_PRICE_LABELS = {
    "P": "monthly_price",
    "m": "quota_multiplier",
    "N0": "baseline_tasks",
    "B0": "baseline_api_budget",
    "u": "utilization",
    "C": "cost_per_task",
    "w": "weight",
    "N": "measured_tasks",
}
_AMOUNT_SUBSCRIPTION = frozenset(_PRICE_LABELS.values())
_TEXT_SUBSCRIPTION = frozenset({"price_period", "task_profile", "baseline_group"})
_AGENT_FIELDS = (
    "agent_id",
    "domain",
    "strengths",
    "unsuitable",
    "can_edit_files",
    "can_use_tools",
    "input_output_shape",
    "source",
    "source_ref",
    "as_of",
)
_EFFORT_FIELDS = (
    "provider",
    "model",
    "effort",
    "suitable",
    "unsuitable",
    "context_length",
    "benchmarks",
    "default_for_agents",
    "source",
    "source_ref",
    "as_of",
)
_PRICE_IDENTITY = (
    "provider",
    "channel",
    "model",
    "effort",
    "plan",
    "feature_scope",
    "window_start",
    "window_end",
    "currency",
)


class DataError(Exception):
    def __init__(self, code: str, *, details: dict | None = None, exit_code: int = 2):
        super().__init__(code)
        self.code = code
        self.details = details
        self.exit_code = exit_code


class TransportError(Exception):
    def __init__(self, code: str = "transport"):
        super().__init__(code)
        self.code = code


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: object) -> str:
    return sha256_bytes(canonical_bytes(value))


def normalize_server(value: str) -> str:
    if not isinstance(value, str) or not value or any(character.isspace() for character in value):
        raise DataError("server")
    parts = urlsplit(value)
    if parts.username or parts.password or "@" in parts.netloc:
        raise DataError("credential_url")
    if parts.scheme not in {"http", "https"}:
        raise DataError("server")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise DataError("server")
    host = parts.hostname
    if host is None or host.lower() not in _LOOPBACK:
        raise DataError("server")
    host = host.lower()
    try:
        port = parts.port
    except ValueError as exc:
        raise DataError("server") from exc
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    netloc = f"[{host}]:{port}" if host == "::1" else f"{host}:{port}"
    return f"{parts.scheme}://{netloc}"


def _emit(document: dict) -> None:
    sys.stdout.buffer.write(canonical_bytes(document) + b"\n")


def _prepare_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def atomic_write(path: Path, data: bytes) -> None:
    _prepare_dir(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise DataError("target_exists")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        view = memoryview(data)
        while len(view):
            written = os.write(descriptor, view)
            if written <= 0:
                raise DataError("io")
            view = view[written:]
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    os.close(descriptor)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _load_object(path: Path, code: str) -> dict:
    try:
        document = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise DataError(code) from exc
    if not isinstance(document, dict):
        raise DataError(code)
    return document


def _aware(value: object, code: str) -> str:
    if not isinstance(value, str) or not value:
        raise DataError(code)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DataError(code) from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise DataError(code)
    return value


def _validation_problem(item_id: str, code: str) -> dict:
    return {"item_id": item_id, "code": code}


def _problems_from_validation(item_id: str, exc: ValidationError) -> list[dict]:
    return [
        {"item_id": item_id, "code": "invalid_candidate", "loc": [str(part) for part in error["loc"]]}
        for error in exc.errors()
    ]


class ServiceClient:
    def __init__(self, server: str, token: str, *, timeout: float = 30):
        self.server = normalize_server(server)
        self.token = token
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        *,
        auth: bool = False,
        idempotency_key: str | None = None,
    ) -> tuple[int, object]:
        if not path.startswith("/"):
            raise DataError("server")
        parts = urlsplit(self.server)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        body = None if payload is None else canonical_bytes(payload)
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            if not self.token:
                raise DataError("unauthorized")
            headers["Authorization"] = "Bearer " + self.token
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        connection = None
        try:
            if parts.scheme == "https":
                connection = http.client.HTTPSConnection(
                    host,
                    port,
                    timeout=self.timeout,
                    context=ssl.create_default_context(),
                )
            else:
                connection = http.client.HTTPConnection(host, port, timeout=self.timeout)
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            status = response.status
            raw = response.read()
        except DataError:
            raise
        except Exception:
            raise TransportError("transport")
        finally:
            if connection is not None:
                connection.close()
        if status in REDIRECTS:
            raise DataError("redirect")
        if not raw:
            return status, None
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            if status >= 500:
                return status, None
            raise DataError("response") from exc
        return status, decoded

    def json_map(self, method: str, path: str, payload: object | None = None, **kwargs) -> tuple[int, dict]:
        status, decoded = self.request(method, path, payload, **kwargs)
        if not isinstance(decoded, dict):
            if status >= 500:
                return status, {}
            raise DataError("response")
        return status, decoded


def _http_failure(status: int) -> str | None:
    if status == 401:
        return "unauthorized"
    if status == 409:
        return "conflict"
    if status == 404:
        return "not_found"
    if status >= 500:
        return "uncertain"
    if status >= 400:
        return "failed"
    return None


def _price_identity(record: dict) -> dict:
    return {
        "provider": record["provider"],
        "channel": record["channel"],
        "model": record["model"],
        "effort": record.get("effort") or "",
        "plan": record["plan"],
        "feature_scope": record["feature_scope"],
        "window_start": record.get("window_start") or "",
        "window_end": record.get("window_end") or "",
        "currency": record["currency"],
    }


def _identity_key(identity: dict) -> tuple:
    return tuple(identity[key] for key in _PRICE_IDENTITY)


def _business_from_catalog(record: dict) -> dict:
    scope = record.get("scope") if isinstance(record.get("scope"), dict) else {}
    window = scope.get("window") if isinstance(scope.get("window"), dict) else {}
    rates = {}
    raw_rates = record.get("rates") if isinstance(record.get("rates"), dict) else {}
    for key, value in raw_rates.items():
        if isinstance(value, dict) and isinstance(value.get("amount"), str):
            rates[key] = value["amount"]
    subscription: dict = {}
    raw_subscription = record.get("subscription") if isinstance(record.get("subscription"), dict) else {}
    for label, key in _PRICE_LABELS.items():
        value = raw_subscription.get(label)
        if not isinstance(value, dict) or not isinstance(value.get("amount"), str):
            continue
        subscription[key] = value["amount"]
        if label == "P" and value.get("period"):
            subscription["price_period"] = value["period"]
    if scope.get("period") and "price_period" not in subscription:
        subscription["price_period"] = scope["period"]
    if scope.get("task_profile"):
        subscription["task_profile"] = scope["task_profile"]
    if scope.get("baseline_group"):
        subscription["baseline_group"] = scope["baseline_group"]
    assumptions = record.get("assumptions") or []
    if assumptions:
        subscription["assumptions"] = list(assumptions)
    identity = {
        "provider": record.get("provider"),
        "channel": record.get("channel"),
        "model": record.get("model"),
        "effort": record.get("effort") or "",
        "plan": record.get("plan"),
        "feature_scope": scope.get("function"),
        "window_start": window.get("start") or "",
        "window_end": window.get("end") or "",
        "currency": scope.get("currency"),
    }
    return {"identity": identity, "rates": rates, "subscription": subscription}


def _business_from_request(body: dict) -> dict:
    record = body["records"][0]
    return {
        "identity": _price_identity(record),
        "rates": {
            key: value
            for key, value in dict(record.get("rates") or {}).items()
            if value is not None
        },
        "subscription": dict(record.get("subscription") or {}),
    }


def _decimals_equal(left: object, right: object) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        parsed_left = parse_decimal(left)
        parsed_right = parse_decimal(right)
        if parsed_left is not None and parsed_right is not None:
            return parsed_left == parsed_right
    return left == right


def _subscription_equal(left: dict, right: dict) -> bool:
    keys = set(left) | set(right)
    for key in keys:
        if key == "assumptions":
            if list(left.get(key) or []) != list(right.get(key) or []):
                return False
            continue
        if key in _AMOUNT_SUBSCRIPTION:
            if not _decimals_equal(left.get(key), right.get(key)):
                return False
            continue
        if left.get(key) != right.get(key):
            return False
    return True


def _rates_equal(left: dict, right: dict) -> bool:
    if set(left) != set(right):
        return False
    return all(_decimals_equal(left[key], right[key]) for key in left)


def _price_equal(left: dict | None, right: dict | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return (
        left["identity"] == right["identity"]
        and _rates_equal(left["rates"], right["rates"])
        and _subscription_equal(left["subscription"], right["subscription"])
    )


def _capability_business(body: dict, fields: tuple[str, ...]) -> dict:
    return {key: body.get(key) for key in fields}


def _benchmarks_equal(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
        return False
    for first, second in zip(left, right, strict=True):
        if not isinstance(first, dict) or not isinstance(second, dict):
            return False
        if set(first) != set(second):
            return False
        for key in first:
            if key == "score":
                if not _decimals_equal(first[key], second[key]):
                    return False
            elif first[key] != second[key]:
                return False
    return True


def _capability_equal(left: dict, right: dict) -> bool:
    keys = set(left) | set(right)
    for key in keys:
        if key == "benchmarks":
            if not _benchmarks_equal(left.get(key), right.get(key)):
                return False
        elif key == "expected_version":
            continue
        elif left.get(key) != right.get(key):
            return False
    return True


def _exact(left: dict, right: dict) -> bool:
    return canonical_bytes(left) == canonical_bytes(right)


def _overlay(base: dict, patch: dict | None, fields: tuple[str, ...]) -> dict:
    merged = dict(base)
    if not isinstance(patch, dict):
        return merged
    for key in fields:
        if key in patch:
            merged[key] = patch[key]
    return merged


def _merge_mapping(remote: dict, patch: dict | None) -> dict:
    if not isinstance(patch, dict):
        return dict(remote)
    merged = dict(remote)
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def _price_request(candidate: dict, business: dict, snapshot_id: str | None) -> dict:
    evidence = candidate["evidence"]
    identity = business["identity"]
    record = {
        "provider": identity["provider"],
        "channel": identity["channel"],
        "model": identity["model"],
        "effort": identity["effort"],
        "plan": identity["plan"],
        "feature_scope": identity["feature_scope"],
        "currency": identity["currency"],
        "rates": business["rates"],
        "evidence_indexes": list(range(len(evidence))),
    }
    if identity["window_start"]:
        record["window_start"] = identity["window_start"]
    if identity["window_end"]:
        record["window_end"] = identity["window_end"]
    if business["subscription"]:
        record["subscription"] = business["subscription"]
    if snapshot_id:
        record["base_snapshot_id"] = snapshot_id
    body = {
        "research": candidate["research"],
        "evidence": evidence,
        "records": [record],
    }
    model = ContributionIn.model_validate(body)
    return model.model_dump(mode="json", exclude_none=True)


def _agent_request(business: dict, version: int) -> dict:
    payload = _capability_business(business, _AGENT_FIELDS)
    payload["expected_version"] = version
    return AgentCapabilityIn.model_validate(payload).model_dump(mode="json")


def _effort_request(business: dict, version: int) -> dict:
    payload = _capability_business(business, _EFFORT_FIELDS)
    payload["expected_version"] = version
    return ModelEffortIn.model_validate(payload).model_dump(mode="json")


def _source_block(item: dict) -> dict:
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    retrieved = source.get("retrieved_at") or item.get("retrieved_at")
    return {
        "kind": source.get("kind") or item.get("collection"),
        "ref": source.get("ref") or source.get("source_url"),
        "retrieved_at": retrieved,
        "labels": list(item.get("source_labels") or []),
    }


def _batch_item(
    *,
    item_id: str,
    category: str,
    collection: str,
    status: str,
    source: dict,
    mapping: str | None,
    candidate: dict | None,
    issues: list[str],
) -> dict:
    evidence_sha = None
    research_sha = None
    if isinstance(candidate, dict):
        evidence = candidate.get("evidence")
        if isinstance(evidence, list) and evidence and isinstance(evidence[0], dict):
            content = evidence[0].get("content")
            if isinstance(content, str):
                evidence_sha = sha256_bytes(content.encode("utf-8"))
        research = candidate.get("research")
        if isinstance(research, dict) and isinstance(research.get("markdown"), str):
            research_sha = sha256_bytes(research["markdown"].encode("utf-8"))
    return {
        "item_id": item_id,
        "category": category,
        "collection": collection,
        "status": status,
        "source": source,
        "mapping": mapping,
        "evidence_sha256": evidence_sha,
        "research_sha256": research_sha,
        "candidate": candidate,
        "issues": issues,
        "remote_version": None,
    }


def _scope_items(scope: dict, scope_dir: Path) -> list[dict]:
    raw_items = scope.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise DataError("scope")
    items: list[dict] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise DataError("scope")
        item_id = raw.get("item_id")
        category = raw.get("category")
        collection = raw.get("collection") or "manual"
        if (
            not isinstance(item_id, str)
            or not item_id
            or len(item_id) > 80
            or not isinstance(category, str)
            or collection not in {"manual", "agent", "openrouter-json"}
        ):
            raise DataError("scope")
        mapping = raw.get("mapping") if isinstance(raw.get("mapping"), str) else None
        if category not in SUPPORTED_CATEGORIES:
            items.append(
                _batch_item(
                    item_id=item_id,
                    category=category,
                    collection=collection,
                    status="unsupported",
                    source=_source_block(raw),
                    mapping=mapping,
                    candidate=raw.get("candidate") if isinstance(raw.get("candidate"), dict) else None,
                    issues=["unsupported_category"],
                )
            )
            continue
        if collection == "openrouter-json":
            items.extend(_openrouter_items(raw, scope_dir, mapping))
            continue
        candidate = raw.get("candidate") if isinstance(raw.get("candidate"), dict) else None
        status = "candidate" if candidate is not None else "needs_research"
        issues = [] if candidate is not None else ["needs_research"]
        items.append(
            _batch_item(
                item_id=item_id,
                category=category,
                collection=collection,
                status=status,
                source=_source_block(raw),
                mapping=mapping,
                candidate=candidate,
                issues=issues,
            )
        )
    items.sort(key=lambda item: item["item_id"])
    seen: set[str] = set()
    for item in items:
        if item["item_id"] in seen:
            raise DataError("duplicate_item")
        seen.add(item["item_id"])
    return items


def _openrouter_items(raw: dict, scope_dir: Path, mapping: str | None) -> list[dict]:
    item_id = raw["item_id"]
    parser_name = raw.get("parser")
    fixture = raw.get("fixture")
    retrieved = raw.get("retrieved_at")
    base = _batch_item(
        item_id=item_id,
        category="price",
        collection="openrouter-json",
        status="needs_research",
        source={
            "kind": "openrouter-json",
            "ref": fixture if isinstance(fixture, str) else None,
            "retrieved_at": retrieved if isinstance(retrieved, str) else None,
            "labels": ["pinned-model"],
        },
        mapping=mapping,
        candidate=None,
        issues=[],
    )
    if parser_name not in {"catalog", "endpoints"} or not isinstance(fixture, str) or not fixture:
        base["issues"] = ["openrouter_fixture"]
        return [base]
    if not isinstance(retrieved, str) or not retrieved:
        base["issues"] = ["retrieved_at"]
        return [base]
    path = Path(fixture)
    if not path.is_absolute():
        path = scope_dir / path
    if not path.is_file():
        base["issues"] = ["fixture_unreadable"]
        return [base]
    try:
        body = path.read_bytes()
        if parser_name == "catalog":
            observation = parse_catalog(body, url=CATALOG_URL, retrieved_at=retrieved)
        else:
            observation = parse_endpoints(body, url=ENDPOINTS_URL, retrieved_at=retrieved)
    except (OSError, ParseError) as exc:
        base["issues"] = [exc.code if isinstance(exc, ParseError) else "fixture_unreadable"]
        return [base]
    if observation.conflicts or not observation.cards:
        base["issues"] = ["unresolved_prices"]
        return [base]
    produced = []
    for card in observation.cards:
        if card.model != PINNED_MODEL:
            base["issues"] = ["unresolved_prices"]
            return [base]
        candidate = {
            "research": {
                "title": f"OpenRouter {PINNED_MODEL} {card.channel}",
                "markdown": (
                    f"Local OpenRouter JSON fixture parsed by {observation.parser}. "
                    f"The parser reads only {PINNED_MODEL}. "
                    "This entry is not a live network fetch.\n"
                ),
            },
            "evidence": [
                {
                    "source_kind": "official_api",
                    "source_url": observation.url,
                    "collector_kind": "worker",
                    "collector_name": observation.parser,
                    "content": observation.evidence_text,
                    "retrieved_at": retrieved,
                }
            ],
            "record": {
                "provider": card.provider,
                "channel": card.channel,
                "model": card.model,
                "effort": "",
                "plan": "payg",
                "feature_scope": "text",
                "currency": "USD",
                "rates": dict(card.rates),
            },
        }
        produced.append(
            _batch_item(
                item_id=f"{item_id}:{card.channel}",
                category="price",
                collection="openrouter-json",
                status="collected",
                source={
                    "kind": "openrouter-json",
                    "ref": observation.url,
                    "retrieved_at": retrieved,
                    "labels": ["pinned-model", observation.parser],
                },
                mapping=mapping or f"Pinned {PINNED_MODEL} through {observation.parser}.",
                candidate=candidate,
                issues=[],
            )
        )
    produced.sort(key=lambda item: item["item_id"])
    return produced


def _require_scope(scope: dict) -> None:
    if scope.get("kind") != "agent-costbook.data-scope" or scope.get("contract_version") != 1:
        raise DataError("scope")
    visibility = scope.get("visibility") or "public"
    if visibility not in {"public", "private"}:
        raise DataError("scope")
    normalize_server(scope.get("server"))


def plan_batch(scope_path: Path, mode: str, out: Path) -> dict:
    if mode not in {"init", "update"}:
        raise DataError("mode")
    scope = _load_object(scope_path, "scope")
    _require_scope(scope)
    created_at = datetime.now(timezone.utc).isoformat()
    items = _scope_items(scope, scope_path.parent)
    batch_id = scope.get("id")
    if batch_id is None:
        batch_id = "batch-" + sha256_json(scope)[:16]
    if not isinstance(batch_id, str) or not batch_id or len(batch_id) > 160:
        raise DataError("scope")
    batch = {
        "kind": "agent-costbook.data-batch",
        "contract_version": CONTRACT_VERSION,
        "batch_id": batch_id,
        "mode": mode,
        "created_at": created_at,
        "server": normalize_server(scope["server"]),
        "visibility": scope.get("visibility") or "public",
        "scope_sha256": sha256_json(scope),
        "items": items,
    }
    _prepare_dir(out)
    atomic_write(out / "scope.json", canonical_bytes(scope) + b"\n")
    atomic_write(out / "batch.json", canonical_bytes(batch) + b"\n")
    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"batch_id": batch_id, "out": str(out), "counts": counts}


def _candidate_problems(item: dict) -> list[dict]:
    item_id = str(item.get("item_id") or "")
    status = item.get("status")
    category = item.get("category")
    candidate = item.get("candidate")
    problems = []
    if status == "unsupported":
        if candidate is not None:
            problems.append(_validation_problem(item_id, "unsupported_category"))
        return problems
    if status == "needs_research":
        if candidate is not None:
            problems.append(_validation_problem(item_id, "needs_research"))
        return problems
    if status not in READY or category not in SUPPORTED_CATEGORIES:
        problems.append(_validation_problem(item_id, "status"))
        return problems
    if not isinstance(candidate, dict):
        problems.append(_validation_problem(item_id, "candidate"))
        return problems
    try:
        if category == "price":
            if status == "collected" and item.get("collection") != "openrouter-json":
                problems.append(_validation_problem(item_id, "not_collected"))
            record = candidate.get("record") if isinstance(candidate.get("record"), dict) else {}
            if status == "collected" and record.get("model") != PINNED_MODEL:
                problems.append(_validation_problem(item_id, "not_collected"))
            _price_request(candidate, _candidate_business(candidate), None)
        elif category == "agent":
            problems.extend(_capability_candidate_problems(item_id, candidate, _AGENT_FIELDS))
        else:
            problems.extend(_capability_candidate_problems(item_id, candidate, _EFFORT_FIELDS))
    except ValidationError as exc:
        problems.extend(_problems_from_validation(item_id, exc))
    except (KeyError, TypeError):
        problems.append(_validation_problem(item_id, "candidate"))
    return problems


def _candidate_business(candidate: dict) -> dict:
    record = candidate["record"]
    return {
        "identity": _price_identity(record),
        "rates": {
            key: value
            for key, value in dict(record.get("rates") or {}).items()
            if value is not None
        },
        "subscription": {
            key: value
            for key, value in dict(record.get("subscription") or {}).items()
            if value is not None
        },
    }


def _capability_candidate_problems(item_id: str, candidate: dict, fields: tuple[str, ...]) -> list[dict]:
    problems = []
    if "expected_version" in candidate:
        problems.append(_validation_problem(item_id, "expected_version"))
    unknown = [key for key in candidate if key not in fields and key != "expected_version"]
    if unknown:
        problems.append(_validation_problem(item_id, "unknown_field"))
    identity_fields = ("agent_id",) if fields is _AGENT_FIELDS else ("provider", "model")
    if any(not isinstance(candidate.get(key), str) or not candidate.get(key) for key in identity_fields):
        problems.append(_validation_problem(item_id, "candidate"))
    if problems:
        return problems
    probe = {key: None for key in fields}
    probe["source"] = "user_observation"
    probe["as_of"] = "2026-09-28T00:00:00+00:00"
    if fields is _EFFORT_FIELDS:
        probe["provider"] = candidate["provider"]
        probe["model"] = candidate["model"]
    else:
        probe["agent_id"] = candidate["agent_id"]
    for key in fields:
        if key in candidate:
            probe[key] = candidate[key]
    try:
        if fields is _AGENT_FIELDS:
            _agent_request(probe, 0)
        else:
            _effort_request(probe, 0)
    except ValidationError as exc:
        problems.extend(_problems_from_validation(item_id, exc))
    return problems


def validate_batch(batch_dir: Path) -> dict:
    batch = _load_batch(batch_dir)
    problems = _batch_problems(batch)
    counts: dict[str, int] = {}
    for item in batch["items"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    if problems:
        raise DataError("invalid_batch", details={"problems": problems})
    return {"batch_id": batch["batch_id"], "counts": counts}


def _load_batch(batch_dir: Path) -> dict:
    batch = _load_object(batch_dir / "batch.json", "batch")
    if batch.get("kind") != "agent-costbook.data-batch" or batch.get("contract_version") != 1:
        raise DataError("batch")
    if batch.get("mode") not in {"init", "update"}:
        raise DataError("batch")
    if not isinstance(batch.get("items"), list):
        raise DataError("batch")
    normalize_server(batch.get("server"))
    return batch


def _batch_problems(batch: dict) -> list[dict]:
    problems = []
    seen: set[str] = set()
    for item in batch["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("item_id"), str):
            problems.append(_validation_problem("", "item"))
            continue
        if item["item_id"] in seen:
            problems.append(_validation_problem(item["item_id"], "duplicate_item"))
        seen.add(item["item_id"])
        problems.extend(_candidate_problems(item))
    identities: set[tuple] = set()
    for item in batch["items"]:
        if not isinstance(item, dict):
            continue
        identity = _normalized_identity(item)
        if identity is None:
            continue
        if identity in identities:
            problems.append(_validation_problem(str(item.get("item_id") or ""), "duplicate_identity"))
        identities.add(identity)
    return problems


def _normalized_identity(item: dict) -> tuple | None:
    if item.get("status") not in READY or not isinstance(item.get("candidate"), dict):
        return None
    candidate = item["candidate"]
    category = item.get("category")
    try:
        if category == "price":
            record = candidate.get("record")
            if not isinstance(record, dict):
                return None
            return ("price", _identity_key(_price_identity(record)))
        if category == "agent":
            agent_id = candidate.get("agent_id")
            if not isinstance(agent_id, str) or not agent_id:
                return None
            return ("agent", agent_id)
        if category == "model_effort":
            provider = candidate.get("provider")
            model = candidate.get("model")
            if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
                return None
            effort = candidate.get("effort") or None
            if effort == "":
                effort = None
            return ("model_effort", provider, model, effort)
    except (KeyError, TypeError):
        return None
    return None


def _catalog_index(catalog: dict) -> dict[tuple, dict]:
    index = {}
    for record in catalog.get("records") or []:
        if not isinstance(record, dict):
            continue
        business = _business_from_catalog(record)
        index[_identity_key(business["identity"])] = {
            "business": business,
            "conflict": record.get("status") == "conflict",
        }
    return index


def _read_price_versions(client: ServiceClient, items: list[dict], publisher: str) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = {}
    for item in items:
        currency = item["candidate"]["record"]["currency"]
        grouped.setdefault(currency, []).append(item)
    found = {}
    for currency, group in grouped.items():
        payload = {
            "method": "M0",
            "currency": currency,
            "candidates": [_estimate_candidate(item) for item in group],
        }
        status, body = client.json_map("POST", "/v1/estimates", payload)
        if status != 200:
            failure = _http_failure(status)
            raise DataError(failure or "response")
        if body.get("publisher_id") != publisher:
            raise DataError("server_identity")
        by_id = {item["item_id"]: item for item in group}
        for result in body.get("results") or []:
            if not isinstance(result, dict):
                raise DataError("response")
            item = by_id.get(result.get("candidate_id"))
            if item is None:
                raise DataError("response")
            found[item["item_id"]] = result
    return found


def _estimate_candidate(item: dict) -> dict:
    identity = _price_identity(item["candidate"]["record"])
    candidate = {
        "candidate_id": item["item_id"],
        "provider": identity["provider"],
        "channel": identity["channel"],
        "model": identity["model"],
        "effort": identity["effort"],
        "plan": identity["plan"],
        "feature_scope": identity["feature_scope"],
    }
    if identity["window_start"] or identity["window_end"]:
        candidate["window_start"] = identity["window_start"] or None
        candidate["window_end"] = identity["window_end"] or None
    return candidate


def _capability_indexes(document: dict) -> tuple[dict, dict]:
    agents = {}
    efforts = {}
    for row in document.get("agents") or []:
        if isinstance(row, dict) and isinstance(row.get("agent_id"), str):
            agents[row["agent_id"]] = row
    for row in document.get("model_efforts") or []:
        if not isinstance(row, dict):
            continue
        effort = row.get("effort") or None
        efforts[(row.get("provider"), row.get("model"), effort)] = row
    return agents, efforts


def _op(category: str, identity: dict, **fields) -> dict:
    operation = {
        "op_id": category + ":" + sha256_json(identity)[:20],
        "category": category,
        "identity": identity,
        "baseline": None,
        "target": None,
    }
    operation.update(fields)
    return operation


def _price_operation(item: dict, remote: dict | None, version: dict | None) -> dict:
    candidate = item["candidate"]
    identity = _price_identity(candidate["record"])
    if remote is not None and remote["conflict"]:
        return _op(
            "price",
            identity,
            item_id=item["item_id"],
            action="conflict",
            reason="remote_conflict",
            baseline={"snapshot_id": (version or {}).get("record_snapshot_id")},
        )
    if version is not None and version.get("status") == "conflict":
        return _op(
            "price",
            identity,
            item_id=item["item_id"],
            action="conflict",
            reason="remote_conflict",
            baseline={"snapshot_id": version.get("record_snapshot_id")},
        )
    remote_business = None if remote is None else remote["business"]
    snapshot_id = None if version is None else version.get("record_snapshot_id")
    record_patch = candidate.get("record") if isinstance(candidate.get("record"), dict) else {}
    rates = _merge_mapping(
        {} if remote_business is None else remote_business["rates"],
        record_patch.get("rates") if "rates" in record_patch else None,
    )
    subscription = _merge_mapping(
        {} if remote_business is None else remote_business["subscription"],
        record_patch.get("subscription") if "subscription" in record_patch else None,
    )
    business = {"identity": identity, "rates": rates, "subscription": subscription}
    baseline = {"snapshot_id": snapshot_id}
    if remote_business is not None and _price_equal(business, remote_business):
        return _op(
            "price",
            identity,
            item_id=item["item_id"],
            action="unchanged",
            reason="same_content",
            baseline=baseline,
            target=remote_business,
        )
    try:
        request = _price_request(candidate, business, snapshot_id if remote_business is not None else None)
    except ValidationError as exc:
        raise DataError(
            "invalid_batch",
            details={"problems": _problems_from_validation(item["item_id"], exc)},
        ) from exc
    return _op(
        "price",
        identity,
        item_id=item["item_id"],
        action="create" if remote_business is None else "update",
        reason="candidate",
        baseline=baseline,
        target=request,
    )


def _reject_unknown_fields(item: dict, fields: tuple[str, ...]) -> None:
    unknown = [key for key in item["candidate"] if key not in fields]
    if unknown:
        raise DataError(
            "invalid_batch",
            details={"problems": [_validation_problem(item["item_id"], "unknown_field")]},
        )


def _agent_operation(item: dict, remote: dict | None) -> dict:
    _reject_unknown_fields(item, _AGENT_FIELDS)
    candidate = item["candidate"]
    base = {key: None for key in _AGENT_FIELDS}
    if remote is not None:
        base = _capability_business(remote, _AGENT_FIELDS)
    merged = _overlay(base, candidate, _AGENT_FIELDS)
    identity = {"agent_id": merged["agent_id"]}
    version = 0 if remote is None else int(remote["row_version"])
    if remote is not None and _capability_equal(merged, base):
        return _op(
            "agent",
            identity,
            item_id=item["item_id"],
            action="unchanged",
            reason="same_content",
            baseline={"row_version": version},
            target=base,
        )
    try:
        request = _agent_request(merged, version)
    except ValidationError as exc:
        raise DataError(
            "invalid_batch",
            details={"problems": _problems_from_validation(item["item_id"], exc)},
        ) from exc
    return _op(
        "agent",
        identity,
        item_id=item["item_id"],
        action="create" if remote is None else "update",
        reason="candidate",
        baseline={"row_version": version},
        target=request,
    )


def _effort_operation(item: dict, remote: dict | None) -> dict:
    _reject_unknown_fields(item, _EFFORT_FIELDS)
    candidate = item["candidate"]
    base = {key: None for key in _EFFORT_FIELDS}
    if remote is not None:
        base = _capability_business(remote, _EFFORT_FIELDS)
    merged = _overlay(base, candidate, _EFFORT_FIELDS)
    if merged.get("effort") == "":
        merged["effort"] = None
    identity = {
        "provider": merged["provider"],
        "model": merged["model"],
        "effort": merged["effort"],
    }
    version = 0 if remote is None else int(remote["row_version"])
    if remote is not None and _capability_equal(merged, base):
        return _op(
            "model_effort",
            identity,
            item_id=item["item_id"],
            action="unchanged",
            reason="same_content",
            baseline={"row_version": version},
            target=base,
        )
    try:
        request = _effort_request(merged, version)
    except ValidationError as exc:
        raise DataError(
            "invalid_batch",
            details={"problems": _problems_from_validation(item["item_id"], exc)},
        ) from exc
    return _op(
        "model_effort",
        identity,
        item_id=item["item_id"],
        action="create" if remote is None else "update",
        reason="candidate",
        baseline={"row_version": version},
        target=request,
    )


def _gap_operation(item: dict) -> dict:
    action = "refused" if item["status"] == "unsupported" else "deferred"
    reason = "unsupported_category" if action == "refused" else "needs_research"
    return {
        "op_id": action + ":" + item["item_id"],
        "item_id": item["item_id"],
        "category": item["category"],
        "action": action,
        "reason": reason,
        "issues": list(item.get("issues") or []),
        "identity": None,
        "baseline": None,
        "target": None,
    }


def diff_batch(batch_dir: Path, server: str, token: str, *, client: ServiceClient | None = None) -> dict:
    batch = _load_batch(batch_dir)
    problems = _batch_problems(batch)
    if problems:
        raise DataError("invalid_batch", details={"problems": problems})
    normalized = normalize_server(server)
    if normalized != batch["server"]:
        raise DataError("server_identity")
    service = client or ServiceClient(normalized, token)
    catalog = _require_catalog(service)
    publisher = catalog["publisher_id"]
    price_items = [
        item
        for item in batch["items"]
        if item["status"] in READY and item["category"] == "price"
    ]
    versions = _read_price_versions(service, price_items, publisher) if price_items else {}
    catalog_rows = _catalog_index(catalog)
    need_capabilities = any(
        item["status"] in READY and item["category"] in {"agent", "model_effort"}
        for item in batch["items"]
    )
    agents: dict = {}
    efforts: dict = {}
    if need_capabilities:
        status, body = service.json_map("GET", "/v1/capabilities", auth=True)
        if status != 200:
            raise DataError(_http_failure(status) or "response")
        agents, efforts = _capability_indexes(body)
    operations = []
    for item in batch["items"]:
        if item["status"] not in READY:
            operations.append(_gap_operation(item))
            continue
        if item["category"] == "price":
            identity = _price_identity(item["candidate"]["record"])
            operations.append(
                _price_operation(
                    item,
                    catalog_rows.get(_identity_key(identity)),
                    versions.get(item["item_id"]),
                )
            )
        elif item["category"] == "agent":
            agent_id = item["candidate"].get("agent_id")
            operations.append(_agent_operation(item, agents.get(agent_id)))
        else:
            effort = item["candidate"].get("effort") or None
            key = (item["candidate"].get("provider"), item["candidate"].get("model"), effort)
            operations.append(_effort_operation(item, efforts.get(key)))
    operations.sort(key=lambda item: item["op_id"])
    if len({item["op_id"] for item in operations}) != len(operations):
        raise DataError("duplicate_item")
    plan = {
        "kind": "agent-costbook.apply-plan",
        "contract_version": CONTRACT_VERSION,
        "mode": batch["mode"],
        "visibility": batch["visibility"],
        "batch_id": batch["batch_id"],
        "batch_sha256": sha256_bytes((batch_dir / "batch.json").read_bytes()),
        "server": {
            "url": normalized,
            "publisher_id": publisher,
            "price_data_version": catalog.get("data_version"),
            "price_snapshot_id": catalog.get("snapshot_id"),
            "price_content_sha256": catalog.get("content_sha256"),
        },
        "operations": operations,
    }
    encoded = canonical_bytes(plan) + b"\n"
    digest = sha256_bytes(canonical_bytes(plan))
    atomic_write(batch_dir / "plan.json", encoded)
    atomic_write(batch_dir / "plan.md", _plan_markdown(plan, digest).encode("utf-8"))
    _retire_stale_journal(batch_dir, plan, digest)
    counts: dict[str, int] = {}
    for operation in operations:
        counts[operation["action"]] = counts.get(operation["action"], 0) + 1
    return {"plan_sha256": digest, "batch_id": batch["batch_id"], "counts": counts}


def _live_capabilities(client: ServiceClient) -> dict:
    status, body = client.json_map("GET", "/v1/capabilities", auth=True)
    if status != 200:
        raise DataError(_http_failure(status) or "response")
    return body


def _require_catalog(client: ServiceClient) -> dict:
    status, body = client.json_map("GET", "/v1/catalog")
    if status != 200:
        raise DataError(_http_failure(status) or "response")
    publisher = body.get("publisher_id")
    if not isinstance(publisher, str) or not publisher:
        raise DataError("server_identity")
    return body


def _plan_markdown(plan: dict, digest: str) -> str:
    lines = [
        "# Catalog apply plan",
        "",
        f"Plan sha256: `{digest}`",
        "",
        "The hash covers `plan.json` only. This file is the reading copy.",
        "",
        f"Server: `{plan['server']['url']}`",
        f"Publisher: `{plan['server']['publisher_id']}`",
        f"Batch: `{plan['batch_id']}`",
        f"Mode: `{plan['mode']}`",
        "",
        "| Action | Category | Item | Reason |",
        "| --- | --- | --- | --- |",
    ]
    for operation in plan["operations"]:
        lines.append(
            "| {action} | {category} | {item} | {reason} |".format(
                action=operation["action"],
                category=operation["category"],
                item=operation.get("item_id") or "",
                reason=operation.get("reason") or "",
            )
        )
    lines.append("")
    return "\n".join(lines)


def _load_plan(batch_dir: Path) -> dict:
    plan = _load_object(batch_dir / "plan.json", "plan")
    if plan.get("kind") != "agent-costbook.apply-plan" or plan.get("contract_version") != 1:
        raise DataError("plan")
    if not isinstance(plan.get("operations"), list) or not isinstance(plan.get("server"), dict):
        raise DataError("plan")
    return plan


def _approved_business(operation: dict) -> dict | None:
    target = operation.get("target")
    if not isinstance(target, dict):
        return None
    if operation["action"] == "unchanged":
        return target
    if operation["category"] == "price":
        return _business_from_request(target)
    fields = _AGENT_FIELDS if operation["category"] == "agent" else _EFFORT_FIELDS
    return _capability_business(target, fields)


def _same_target(operation: dict, remote_business: dict | None) -> bool:
    approved = _approved_business(operation)
    if approved is None or remote_business is None:
        return False
    if operation["category"] == "price":
        return _price_equal(approved, remote_business)
    return _capability_equal(approved, remote_business)


def _journal_path(batch_dir: Path) -> Path:
    return batch_dir / "journal.json"


def _fresh_journal(plan: dict, plan_sha256: str) -> dict:
    return {
        "kind": "agent-costbook.data-journal",
        "contract_version": CONTRACT_VERSION,
        "plan_sha256": plan_sha256,
        "server": {
            "url": plan["server"]["url"],
            "publisher_id": plan["server"]["publisher_id"],
        },
        "operations": {},
    }


def _journal_matches(document: dict, plan: dict, plan_sha256: str) -> bool:
    server = document.get("server") if isinstance(document.get("server"), dict) else {}
    return (
        document.get("kind") == "agent-costbook.data-journal"
        and document.get("contract_version") == CONTRACT_VERSION
        and document.get("plan_sha256") == plan_sha256
        and server.get("url") == plan["server"]["url"]
        and server.get("publisher_id") == plan["server"]["publisher_id"]
        and isinstance(document.get("operations"), dict)
    )


def _load_journal(batch_dir: Path, plan: dict, plan_sha256: str) -> dict:
    path = _journal_path(batch_dir)
    fresh = _fresh_journal(plan, plan_sha256)
    if not path.exists():
        return fresh
    document = _load_object(path, "journal")
    if not _journal_matches(document, plan, plan_sha256):
        return fresh
    return document


def _retire_stale_journal(batch_dir: Path, plan: dict, plan_sha256: str) -> None:
    path = _journal_path(batch_dir)
    if not path.exists():
        return
    document = _load_object(path, "journal")
    if _journal_matches(document, plan, plan_sha256):
        return
    _save_journal(batch_dir, _fresh_journal(plan, plan_sha256))


def _save_journal(batch_dir: Path, journal: dict) -> None:
    atomic_write(_journal_path(batch_dir), canonical_bytes(journal) + b"\n")


def _price_key(plan: dict, operation: dict) -> str:
    material = {
        "baseline_snapshot_id": (operation.get("baseline") or {}).get("snapshot_id") or "",
        "category": "price",
        "identity": operation["identity"],
        "publisher_id": plan["server"]["publisher_id"],
        "request": operation["target"],
        "server": plan["server"]["url"],
    }
    return "ac-data-" + sha256_json(material)


def _private_backup(path: Path) -> None:
    if path.stat().st_mode & 0o077 or path.parent.stat().st_mode & 0o077:
        raise DataError("backup_permissions")


def _capability_fingerprint(document: dict) -> bytes:
    agents = []
    for row in document.get("agents") or []:
        if not isinstance(row, dict):
            continue
        item = _capability_business(row, _AGENT_FIELDS)
        item["row_version"] = row.get("row_version")
        agents.append(item)
    efforts = []
    for row in document.get("model_efforts") or []:
        if not isinstance(row, dict):
            continue
        item = _capability_business(row, _EFFORT_FIELDS)
        item["row_version"] = row.get("row_version")
        efforts.append(item)
    agents.sort(key=lambda item: item.get("agent_id") or "")
    efforts.sort(
        key=lambda item: (
            item.get("provider") or "",
            item.get("model") or "",
            item.get("effort") or "",
        )
    )
    return canonical_bytes({"agents": agents, "model_efforts": efforts})


def _database_view(path: Path) -> tuple[str, dict, dict]:
    try:
        store = Store(path, readonly=True)
    except StoreError as exc:
        raise DataError("backup_unverified") from exc
    try:
        publisher = store.publisher_id()
        catalog = catalog_document(store, store.revision_of(None))
        capabilities = store.current_capabilities()
    except (StoreError, ExportError, sqlite3.Error) as exc:
        raise DataError("backup_unverified") from exc
    finally:
        store.close()
    return publisher, catalog, capabilities


def _backup_matches(
    path: Path,
    plan: dict,
    catalog: dict,
    capabilities: dict,
) -> None:
    _private_backup(path)
    publisher, copied_catalog, copied_capabilities = _database_view(path)
    if publisher != plan["server"]["publisher_id"]:
        raise DataError("backup_unverified")
    for key in ("publisher_id", "data_version", "snapshot_id", "content_sha256"):
        if copied_catalog.get(key) != catalog.get(key):
            raise DataError("backup_unverified")
    if _capability_fingerprint(copied_capabilities) != _capability_fingerprint(capabilities):
        raise DataError("backup_unverified")


def _backup_file(
    source: Path,
    destination: Path,
    plan: dict,
    catalog: dict,
    capabilities: dict,
) -> dict:
    parent = destination.parent
    if not parent.exists():
        parent.mkdir(parents=True, mode=0o700)
    if parent.stat().st_mode & 0o077:
        raise DataError("backup_permissions")
    try:
        copy_database(source, destination)
    except BackupError as exc:
        raise DataError("backup_" + exc.code) from exc
    os.chmod(destination, 0o600)
    _backup_matches(destination, plan, catalog, capabilities)
    return {
        "kind": "agent-costbook.backup-receipt",
        "contract_version": CONTRACT_VERSION,
        "method": "local-copy",
        "server": {
            "url": plan["server"]["url"],
            "publisher_id": plan["server"]["publisher_id"],
        },
        "data_version": catalog.get("data_version"),
        "content_sha256": catalog.get("content_sha256"),
        "source": str(source),
        "destination": str(destination),
        "sha256": sha256_bytes(destination.read_bytes()),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _validate_backup_receipt(
    path: Path,
    plan: dict,
    catalog: dict,
    capabilities: dict,
) -> dict:
    document = _load_object(path, "backup_receipt")
    if (
        document.get("kind") != "agent-costbook.backup-receipt"
        or document.get("contract_version") != 1
        or document.get("method") not in {"local-copy", "operator"}
    ):
        raise DataError("backup_receipt")
    server = document.get("server") if isinstance(document.get("server"), dict) else {}
    try:
        url = normalize_server(server.get("url"))
    except DataError as exc:
        raise DataError("backup_receipt") from exc
    if url != plan["server"]["url"] or server.get("publisher_id") != plan["server"]["publisher_id"]:
        raise DataError("backup_receipt")
    if document.get("data_version") != catalog.get("data_version"):
        raise DataError("backup_receipt")
    if document.get("content_sha256") != catalog.get("content_sha256"):
        raise DataError("backup_receipt")
    digest = document.get("sha256")
    destination = document.get("destination")
    if not isinstance(digest, str) or len(digest) != 64 or not isinstance(destination, str):
        raise DataError("backup_receipt")
    _aware(document.get("created_at"), "backup_receipt")
    file_path = Path(destination)
    if not file_path.is_file():
        raise DataError("backup_unverified")
    if sha256_bytes(file_path.read_bytes()) != digest:
        raise DataError("backup_unverified")
    _backup_matches(file_path, plan, catalog, capabilities)
    return document


def _live_price(client: ServiceClient, operation: dict, publisher: str) -> dict | None:
    identity = operation["identity"]
    payload = {
        "method": "M0",
        "currency": identity["currency"],
        "candidates": [
            {
                "candidate_id": "row",
                "provider": identity["provider"],
                "channel": identity["channel"],
                "model": identity["model"],
                "effort": identity["effort"],
                "plan": identity["plan"],
                "feature_scope": identity["feature_scope"],
                **(
                    {
                        "window_start": identity["window_start"] or None,
                        "window_end": identity["window_end"] or None,
                    }
                    if identity["window_start"] or identity["window_end"]
                    else {}
                ),
            }
        ],
    }
    status, estimate = client.json_map("POST", "/v1/estimates", payload)
    if status != 200:
        raise DataError(_http_failure(status) or "response")
    if estimate.get("publisher_id") != publisher:
        raise DataError("server_identity")
    status, catalog = client.json_map("GET", "/v1/catalog")
    if status != 200 or catalog.get("publisher_id") != publisher:
        raise DataError("server_identity")
    rows = _catalog_index(catalog)
    remote = rows.get(_identity_key(identity))
    result = (estimate.get("results") or [{}])[0]
    snapshot_id = result.get("record_snapshot_id") if isinstance(result, dict) else None
    if remote is None:
        return None
    remote["snapshot_id"] = snapshot_id
    if isinstance(result, dict) and result.get("status") == "conflict":
        remote["conflict"] = True
    return remote


def _live_capability(client: ServiceClient, operation: dict) -> dict | None:
    if operation["category"] == "agent":
        agent_id = operation["identity"]["agent_id"]
        path = "/v1/capabilities/agents?" + _query({"agent_id": agent_id})
    else:
        identity = operation["identity"]
        query = {"provider": identity["provider"], "model": identity["model"]}
        if identity.get("effort"):
            query["effort"] = identity["effort"]
        path = "/v1/capabilities/model-efforts?" + _query(query)
    status, body = client.json_map("GET", path, auth=True)
    if status == 404:
        return None
    if status != 200:
        raise DataError(_http_failure(status) or "response")
    return body


def _query(values: dict) -> str:
    return urlencode(values)


def _evidence_proves(client: ServiceClient, state: dict) -> bool:
    evidence = state.get("evidence") or []
    contribution_id = state.get("contribution_id")
    if not contribution_id or not evidence:
        return False
    for item in evidence:
        status, body = client.json_map("GET", f"/v1/evidence/{item['id']}")
        if status == 404:
            return False
        if status != 200:
            raise DataError(_http_failure(status) or "response")
        if body.get("contribution_id") != contribution_id or body.get("content_sha256") != item.get(
            "content_sha256"
        ):
            return False
    return True


def _result(operation: dict, outcome: str, detail: str, **extra) -> dict:
    row = {
        "op_id": operation["op_id"],
        "item_id": operation.get("item_id"),
        "category": operation["category"],
        "outcome": outcome,
        "detail": detail,
    }
    for key, value in extra.items():
        if value is not None:
            row[key] = value
    return row


def _finish_result(operation: dict, outcome: str, detail: str, state: dict | None = None) -> dict:
    state = state or {}
    return _result(
        operation,
        outcome,
        detail,
        contribution_id=state.get("contribution_id"),
        internal_snapshot_id=state.get("internal_snapshot_id"),
        row_version=state.get("row_version"),
    )


def apply_batch(
    batch_dir: Path,
    approved: str,
    server: str,
    token: str,
    *,
    backup_source: Path | None = None,
    backup_destination: Path | None = None,
    backup_receipt: Path | None = None,
    client: ServiceClient | None = None,
) -> dict:
    if not isinstance(approved, str) or len(approved) != 64:
        raise DataError("plan_hash")
    approved = approved.lower()
    plan = _load_plan(batch_dir)
    digest = sha256_bytes(canonical_bytes(plan))
    if digest != approved:
        raise DataError("plan_hash")
    batch_bytes = (batch_dir / "batch.json").read_bytes()
    if sha256_bytes(batch_bytes) != plan.get("batch_sha256"):
        raise DataError("batch_changed")
    normalized = normalize_server(server)
    if normalized != plan["server"]["url"]:
        raise DataError("server_identity")
    for operation in plan["operations"]:
        if operation["action"] in {"create", "update"} and operation["category"] not in SUPPORTED_CATEGORIES:
            raise DataError("unsupported_category")
    service = client or ServiceClient(normalized, token)
    catalog = _require_catalog(service)
    if catalog["publisher_id"] != plan["server"]["publisher_id"]:
        raise DataError("server_identity")
    journal = _load_journal(batch_dir, plan, digest)
    previews = [
        _preview(service, plan, operation, journal["operations"].get(operation["op_id"]) or {})
        for operation in plan["operations"]
    ]
    writes = [item for item in previews if item["will_write"]]
    backup = None
    if writes:
        supplied_copy = backup_source is not None or backup_destination is not None
        if supplied_copy and backup_receipt is not None:
            raise DataError("backup_required")
        if (backup_source is None) != (backup_destination is None):
            raise DataError("backup_required")
        if backup_source is not None and backup_destination is not None:
            capabilities = _live_capabilities(service)
            backup = _backup_file(backup_source, backup_destination, plan, catalog, capabilities)
            atomic_write(batch_dir / "backup-receipt.json", canonical_bytes(backup) + b"\n")
        elif backup_receipt is not None:
            capabilities = _live_capabilities(service)
            backup = _validate_backup_receipt(backup_receipt, plan, catalog, capabilities)
        else:
            raise DataError("backup_required")
    _save_journal(batch_dir, journal)
    results = []
    exit_code = 0
    for operation, preview in zip(plan["operations"], previews, strict=True):
        if preview["stop"]:
            raise DataError(preview["detail"])
        if not preview["will_write"]:
            results.append(preview["result"])
            if preview["result"]["outcome"] in {"conflict", "uncertain", "failed"}:
                exit_code = 3
            continue
        outcome = _mutate(service, batch_dir, plan, operation, journal)
        results.append(outcome)
        if outcome["outcome"] in {"conflict", "uncertain", "failed"}:
            exit_code = 3
        if outcome["detail"] == "unauthorized":
            exit_code = 2
            break
    receipt = _receipt(plan, digest, backup, results)
    atomic_write(batch_dir / "receipt.json", canonical_bytes(receipt) + b"\n")
    counts: dict[str, int] = {}
    for row in results:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    return {
        "plan_sha256": digest,
        "batch_id": plan["batch_id"],
        "counts": counts,
        "exit_code": exit_code,
        "receipt": str(batch_dir / "receipt.json"),
    }


def _preview(client: ServiceClient, plan: dict, operation: dict, state: dict) -> dict:
    action = operation["action"]
    if action == "deferred":
        return {
            "will_write": False,
            "stop": False,
            "detail": "needs_research",
            "result": _result(operation, "deferred", "needs_research"),
        }
    if action == "refused":
        return {
            "will_write": False,
            "stop": False,
            "detail": "unsupported_category",
            "result": _result(operation, "refused", "unsupported_category"),
        }
    if action == "conflict":
        return {
            "will_write": False,
            "stop": False,
            "detail": operation.get("reason") or "conflict",
            "result": _result(operation, "conflict", operation.get("reason") or "conflict"),
        }
    if state.get("state") == "confirmed" and _remote_matches(client, plan, operation):
        return {
            "will_write": False,
            "stop": False,
            "detail": "same_content",
            "result": _finish_result(operation, "unchanged", "same_content", state),
        }
    if operation["category"] == "price" and state.get("contribution_id"):
        if _evidence_proves(client, state) and _remote_matches(client, plan, operation):
            state["state"] = "confirmed"
            return {
                "will_write": False,
                "stop": False,
                "detail": "proved",
                "result": _finish_result(
                    operation,
                    "created" if operation["action"] == "create" else "updated",
                    "proved",
                    state,
                ),
            }
    if operation["category"] != "price" and state.get("state") in {"prepared", "uncertain"}:
        proved = _capability_proved(client, operation)
        if proved:
            state["state"] = "confirmed"
            state["row_version"] = proved
            return {
                "will_write": False,
                "stop": False,
                "detail": "proved",
                "result": _finish_result(
                    operation,
                    "created" if operation["action"] == "create" else "updated",
                    "proved",
                    state,
                ),
            }
        if state.get("state") == "uncertain":
            return {
                "will_write": False,
                "stop": False,
                "detail": "uncertain_capability",
                "result": _result(operation, "conflict", "uncertain_capability"),
            }
    remote_view = _remote_view(client, plan, operation)
    if remote_view["conflict"]:
        return {
            "will_write": False,
            "stop": False,
            "detail": "remote_conflict",
            "result": _result(operation, "conflict", "remote_conflict"),
        }
    if _same_target(operation, remote_view["business"]):
        return {
            "will_write": False,
            "stop": False,
            "detail": "same_content",
            "result": _result(operation, "unchanged", "same_content"),
        }
    if not _version_matches(operation, remote_view):
        return {
            "will_write": False,
            "stop": False,
            "detail": "version_conflict",
            "result": _result(operation, "conflict", "version_conflict"),
        }
    return {"will_write": True, "stop": False, "detail": "write", "result": None}


def _remote_matches(client: ServiceClient, plan: dict, operation: dict) -> bool:
    view = _remote_view(client, plan, operation)
    return _same_target(operation, view["business"])


def _remote_view(client: ServiceClient, plan: dict, operation: dict) -> dict:
    if operation["category"] == "price":
        remote = _live_price(client, operation, plan["server"]["publisher_id"])
        if remote is None:
            return {"business": None, "snapshot_id": None, "row_version": 0, "conflict": False}
        return {
            "business": remote["business"],
            "snapshot_id": remote.get("snapshot_id"),
            "row_version": 0,
            "conflict": bool(remote.get("conflict")),
        }
    remote = _live_capability(client, operation)
    if remote is None:
        return {"business": None, "snapshot_id": None, "row_version": 0, "conflict": False}
    fields = _AGENT_FIELDS if operation["category"] == "agent" else _EFFORT_FIELDS
    return {
        "business": _capability_business(remote, fields),
        "snapshot_id": None,
        "row_version": int(remote.get("row_version") or 0),
        "conflict": False,
    }


def _version_matches(operation: dict, view: dict) -> bool:
    baseline = operation.get("baseline") or {}
    if operation["category"] == "price":
        return view["snapshot_id"] == baseline.get("snapshot_id")
    return view["row_version"] == baseline.get("row_version")


def _mutate(
    client: ServiceClient,
    batch_dir: Path,
    plan: dict,
    operation: dict,
    journal: dict,
) -> dict:
    fresh = _preview(client, plan, operation, journal["operations"].get(operation["op_id"]) or {})
    if fresh["stop"]:
        raise DataError(fresh["detail"])
    if not fresh["will_write"]:
        _remember(batch_dir, journal, operation, journal["operations"].get(operation["op_id"]) or {})
        return fresh["result"]
    if operation["category"] == "price":
        return _mutate_price(client, batch_dir, plan, operation, journal)
    return _mutate_capability(client, batch_dir, plan, operation, journal)


def _remember(batch_dir: Path, journal: dict, operation: dict, state: dict) -> None:
    if not state:
        return
    journal["operations"][operation["op_id"]] = state
    _save_journal(batch_dir, journal)


def _mutate_price(
    client: ServiceClient,
    batch_dir: Path,
    plan: dict,
    operation: dict,
    journal: dict,
) -> dict:
    state = dict(journal["operations"].get(operation["op_id"]) or {})
    if not state.get("contribution_id"):
        state.update({"state": "prepared", "phase": "create", "idempotency_key": _price_key(plan, operation)})
        _remember(batch_dir, journal, operation, state)
        try:
            status, body = client.json_map(
                "POST",
                "/v1/contributions",
                operation["target"],
                auth=True,
                idempotency_key=state["idempotency_key"],
            )
        except TransportError:
            state["state"] = "uncertain"
            _remember(batch_dir, journal, operation, state)
            return _finish_result(operation, "uncertain", "response_lost", state)
        failure = _classify_http(status)
        if failure == "unauthorized":
            return _result(operation, "failed", "unauthorized")
        if failure == "uncertain":
            state["state"] = "uncertain"
            _remember(batch_dir, journal, operation, state)
            return _finish_result(operation, "uncertain", "response_lost", state)
        if failure == "conflict":
            state["state"] = "conflict"
            _remember(batch_dir, journal, operation, state)
            return _finish_result(operation, "conflict", "conflict", state)
        if failure or not body.get("contribution_id"):
            state["state"] = "failed"
            _remember(batch_dir, journal, operation, state)
            return _finish_result(operation, "failed", failure or "failed", state)
        state["contribution_id"] = body["contribution_id"]
        state["evidence"] = [
            {"id": item["id"], "content_sha256": item["content_sha256"]}
            for item in body.get("evidence") or []
            if isinstance(item, dict)
        ]
        state["phase"] = "publish"
        state["state"] = "prepared"
        _remember(batch_dir, journal, operation, state)
    if _evidence_proves(client, state) and _remote_matches(client, plan, operation):
        state["state"] = "confirmed"
        _remember(batch_dir, journal, operation, state)
        outcome = "created" if operation["action"] == "create" else "updated"
        return _finish_result(operation, outcome, "proved", state)
    state["state"] = "prepared"
    state["phase"] = "publish"
    _remember(batch_dir, journal, operation, state)
    try:
        status, body = client.json_map(
            "POST",
            f"/v1/contributions/{state['contribution_id']}/publish",
            auth=True,
        )
    except TransportError:
        state["state"] = "uncertain"
        _remember(batch_dir, journal, operation, state)
        return _finish_result(operation, "uncertain", "response_lost", state)
    failure = _classify_http(status)
    if failure == "unauthorized":
        return _finish_result(operation, "failed", "unauthorized", state)
    if failure == "uncertain":
        state["state"] = "uncertain"
        _remember(batch_dir, journal, operation, state)
        return _finish_result(operation, "uncertain", "response_lost", state)
    if failure == "conflict":
        state["state"] = "conflict"
        _remember(batch_dir, journal, operation, state)
        return _finish_result(operation, "conflict", "conflict", state)
    if failure or body.get("status") != "published" or body.get("contribution_id") != state["contribution_id"]:
        state["state"] = "failed"
        _remember(batch_dir, journal, operation, state)
        return _finish_result(operation, "failed", failure or "failed", state)
    state["state"] = "confirmed"
    state["internal_snapshot_id"] = body.get("snapshot_id")
    _remember(batch_dir, journal, operation, state)
    outcome = "created" if operation["action"] == "create" else "updated"
    return _finish_result(operation, outcome, "published", state)


def _capability_proved(client: ServiceClient, operation: dict) -> int | None:
    remote = _live_capability(client, operation)
    expected = int(operation["baseline"]["row_version"]) + 1
    business = _approved_business(operation)
    fields = _AGENT_FIELDS if operation["category"] == "agent" else _EFFORT_FIELDS
    if (
        remote is not None
        and int(remote.get("row_version") or 0) == expected
        and business is not None
        and _exact(_capability_business(remote, fields), business)
    ):
        return expected
    return None


def _mutate_capability(
    client: ServiceClient,
    batch_dir: Path,
    plan: dict,
    operation: dict,
    journal: dict,
) -> dict:
    state = dict(journal["operations"].get(operation["op_id"]) or {})
    proved = _capability_proved(client, operation)
    if state.get("state") == "uncertain":
        if proved:
            state.update({"state": "confirmed", "row_version": proved})
            _remember(batch_dir, journal, operation, state)
            outcome = "created" if operation["action"] == "create" else "updated"
            return _finish_result(operation, outcome, "proved", state)
        state["state"] = "conflict"
        _remember(batch_dir, journal, operation, state)
        return _finish_result(operation, "conflict", "uncertain_capability", state)
    view = _remote_view(client, plan, operation)
    expected = int(operation["baseline"]["row_version"]) + 1
    fields = _AGENT_FIELDS if operation["category"] == "agent" else _EFFORT_FIELDS
    business = _approved_business(operation)
    if proved:
        state.update({"state": "confirmed", "row_version": proved})
        _remember(batch_dir, journal, operation, state)
        outcome = "created" if operation["action"] == "create" else "updated"
        return _finish_result(operation, outcome, "proved", state)
    if view["row_version"] != operation["baseline"]["row_version"] or not _version_matches(operation, view):
        state["state"] = "conflict"
        _remember(batch_dir, journal, operation, state)
        return _finish_result(operation, "conflict", "version_conflict", state)
    if _same_target(operation, view["business"]):
        return _result(operation, "unchanged", "same_content")
    state.update({"state": "prepared", "phase": "put"})
    _remember(batch_dir, journal, operation, state)
    path = (
        "/v1/capabilities/agents"
        if operation["category"] == "agent"
        else "/v1/capabilities/model-efforts"
    )
    try:
        status, body = client.json_map("PUT", path, operation["target"], auth=True)
    except TransportError:
        state["state"] = "uncertain"
        _remember(batch_dir, journal, operation, state)
        return _finish_result(operation, "uncertain", "response_lost", state)
    failure = _classify_http(status)
    if failure == "unauthorized":
        return _finish_result(operation, "failed", "unauthorized", state)
    if failure == "uncertain":
        state["state"] = "uncertain"
        _remember(batch_dir, journal, operation, state)
        return _finish_result(operation, "uncertain", "response_lost", state)
    if failure == "conflict":
        state["state"] = "conflict"
        _remember(batch_dir, journal, operation, state)
        return _finish_result(operation, "conflict", "conflict", state)
    if failure or not isinstance(body, dict):
        state["state"] = "failed"
        _remember(batch_dir, journal, operation, state)
        return _finish_result(operation, "failed", failure or "failed", state)
    stored = _capability_business(body, fields)
    if int(body.get("row_version") or 0) != expected or not _exact(stored, business or {}):
        state["state"] = "conflict"
        _remember(batch_dir, journal, operation, state)
        return _finish_result(operation, "conflict", "response_mismatch", state)
    state.update({"state": "confirmed", "row_version": expected})
    _remember(batch_dir, journal, operation, state)
    outcome = "created" if operation["action"] == "create" else "updated"
    return _finish_result(operation, outcome, "written", state)


def _classify_http(status: int) -> str | None:
    if status in {200, 201}:
        return None
    return _http_failure(status) or "failed"


def _receipt(plan: dict, digest: str, backup: dict | None, results: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for row in results:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    present = counts.get("created", 0) + counts.get("updated", 0) + counts.get("unchanged", 0)
    return {
        "kind": "agent-costbook.data-receipt",
        "contract_version": CONTRACT_VERSION,
        "batch_id": plan["batch_id"],
        "plan_sha256": digest,
        "server": {
            "url": plan["server"]["url"],
            "publisher_id": plan["server"]["publisher_id"],
        },
        "backup": backup,
        "results": results,
        "counts": counts,
        "coverage": {
            "operations": len(results),
            "present": present,
            "deferred": counts.get("deferred", 0),
            "refused": counts.get("refused", 0),
            "not_applied": counts.get("conflict", 0)
            + counts.get("uncertain", 0)
            + counts.get("failed", 0),
        },
    }


_VERIFY_OUTCOMES = {
    "create": frozenset({"created", "unchanged"}),
    "update": frozenset({"updated", "unchanged"}),
    "unchanged": frozenset({"unchanged"}),
    "deferred": frozenset({"deferred"}),
    "refused": frozenset({"refused"}),
}


def _verified_version(client: ServiceClient, plan: dict, operation: dict, row: dict) -> bool:
    outcome = row.get("outcome")
    if operation["category"] == "price":
        view = _remote_view(client, plan, operation)
        recorded = row.get("internal_snapshot_id")
        if outcome == "unchanged" and recorded is None:
            return True
        return isinstance(recorded, str) and recorded != "" and view["snapshot_id"] == recorded
    view = _remote_view(client, plan, operation)
    if outcome == "unchanged":
        recorded = row.get("row_version")
        if recorded is None:
            return view["row_version"] == (operation.get("baseline") or {}).get("row_version")
        return recorded == view["row_version"]
    expected = int((operation.get("baseline") or {}).get("row_version") or 0) + 1
    return row.get("row_version") == expected and view["row_version"] == expected


def verify_receipt(receipt_path: Path, server: str, token: str, *, client: ServiceClient | None = None) -> dict:
    receipt = _load_object(receipt_path, "receipt")
    if receipt.get("kind") != "agent-costbook.data-receipt":
        raise DataError("receipt")
    batch_dir = receipt_path.parent
    plan = _load_plan(batch_dir)
    digest = sha256_bytes(canonical_bytes(plan))
    if digest != receipt.get("plan_sha256"):
        raise DataError("plan_hash")
    normalized = normalize_server(server)
    if normalized != plan["server"]["url"]:
        raise DataError("server_identity")
    service = client or ServiceClient(normalized, token)
    catalog = _require_catalog(service)
    if catalog["publisher_id"] != plan["server"]["publisher_id"]:
        raise DataError("server_identity")
    operations = plan["operations"]
    if not isinstance(operations, list) or not operations:
        return {"plan_sha256": digest, "matches": False, "mismatches": ["empty"], "exit_code": 3}
    results = receipt.get("results")
    if not isinstance(results, list) or len(results) != len(operations):
        return {"plan_sha256": digest, "matches": False, "mismatches": ["coverage"], "exit_code": 3}
    by_id = {}
    for operation in operations:
        op_id = operation.get("op_id")
        if not isinstance(op_id, str) or op_id in by_id:
            return {"plan_sha256": digest, "matches": False, "mismatches": [op_id], "exit_code": 3}
        by_id[op_id] = operation
    seen: set[str] = set()
    mismatches = []
    for row in results:
        if not isinstance(row, dict) or not isinstance(row.get("op_id"), str):
            mismatches.append(None)
            continue
        op_id = row["op_id"]
        if op_id in seen:
            mismatches.append(op_id)
            continue
        seen.add(op_id)
        operation = by_id.get(op_id)
        if operation is None:
            mismatches.append(op_id)
            continue
        allowed = _VERIFY_OUTCOMES.get(operation.get("action"), frozenset())
        if row.get("outcome") not in allowed:
            mismatches.append(op_id)
            continue
        if row["outcome"] in {"deferred", "refused"}:
            continue
        if not _remote_matches(service, plan, operation) or not _verified_version(
            service, plan, operation, row
        ):
            mismatches.append(op_id)
    if seen != set(by_id):
        mismatches.extend(sorted(set(by_id) - seen))
    return {
        "plan_sha256": digest,
        "matches": not mismatches,
        "mismatches": mismatches,
        "exit_code": 0 if not mismatches else 3,
    }


def _configured_admin_token(args: argparse.Namespace) -> str:
    from agent_costbook.local import LocalError, load_cli_config

    try:
        config = load_cli_config(getattr(args, "config", None))
    except LocalError as exc:
        raise DataError(exc.code, exit_code=exc.exit_code) from None
    return config.admin_token


def run_data(args: argparse.Namespace) -> int:
    try:
        if args.data_command == "plan":
            _emit(plan_batch(Path(args.scope), args.mode, Path(args.out)))
            return 0
        if args.data_command == "validate":
            _emit(validate_batch(Path(args.batch)))
            return 0
        if args.data_command == "diff":
            _emit(diff_batch(Path(args.batch), args.server, _configured_admin_token(args)))
            return 0
        if args.data_command == "apply":
            result = apply_batch(
                Path(args.batch),
                args.approved_diff_sha256,
                args.server,
                _configured_admin_token(args),
                backup_source=Path(args.backup_source) if args.backup_source else None,
                backup_destination=Path(args.backup_destination) if args.backup_destination else None,
                backup_receipt=Path(args.backup_receipt) if args.backup_receipt else None,
            )
            exit_code = result.pop("exit_code")
            _emit(result)
            return exit_code
        if args.data_command == "verify":
            result = verify_receipt(
                Path(args.receipt),
                args.server,
                _configured_admin_token(args),
            )
            exit_code = result.pop("exit_code")
            _emit(result)
            return exit_code
    except DataError as exc:
        print(exc.code, file=sys.stderr)
        if exc.details is not None:
            _emit(exc.details)
        return exc.exit_code
    except TransportError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    raise DataError("command")


def register(commands: argparse._SubParsersAction) -> None:
    data = commands.add_parser("data")
    nested = data.add_subparsers(dest="data_command", required=True)
    plan = nested.add_parser("plan")
    plan.add_argument("--mode", required=True, choices=("init", "update"))
    plan.add_argument("--scope", required=True)
    plan.add_argument("--out", required=True)
    validate = nested.add_parser("validate")
    validate.add_argument("--batch", required=True)
    diff = nested.add_parser("diff")
    diff.add_argument("--batch", required=True)
    diff.add_argument("--server", required=True)
    diff.add_argument("--config")
    apply = nested.add_parser("apply")
    apply.add_argument("--batch", required=True)
    apply.add_argument("--approved-diff-sha256", required=True)
    apply.add_argument("--server", required=True)
    apply.add_argument("--config")
    apply.add_argument("--backup-source")
    apply.add_argument("--backup-destination")
    apply.add_argument("--backup-receipt")
    verify = nested.add_parser("verify")
    verify.add_argument("--receipt", required=True)
    verify.add_argument("--server", required=True)
    verify.add_argument("--config")
