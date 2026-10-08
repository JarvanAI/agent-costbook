"""Anonymous public-reference HTTP API.

Artifact checks belong to ``public_data.validate_public_artifact`` and packaged
defaults to ``public_data.default_public_paths``. Until that module exists, an
explicit database and manifest still open readonly; the manifest is only parsed
as a JSON object.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Path as ApiPath, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agent_costbook.api import build_estimate_response
from agent_costbook.estimates import ALLOWED_USAGE, parse_decimal
from agent_costbook.export import ExportError, build_document
from agent_costbook.models import EstimateIn, ScenarioIn
from agent_costbook.store import Store, StoreError

SERVICE_VERSION = "1.2.0"
BODY_BYTES = 65536
MAX_CANDIDATES = 64
REQUESTS_PER_WINDOW = 60
WINDOW_SECONDS = 60
CURRENT_CACHE = "public, max-age=300"
PINNED_CACHE = "public, max-age=31536000, immutable"
NO_STORE = "no-store"
_PUBLIC_SNAPSHOT = re.compile(r"^snap-[1-9][0-9]*$")
_ETAG = re.compile(r'(?:W/)?"([^"\\]*)"')


class PublicArtifactError(Exception):
    pass


class PublicError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: list[dict] | None = None,
    ):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class InstanceRateLimiter:
    def __init__(self) -> None:
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def decide(self) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            boundary = now - WINDOW_SECONDS
            while self._hits and self._hits[0] <= boundary:
                self._hits.popleft()
            if len(self._hits) >= REQUESTS_PER_WINDOW:
                remaining = self._hits[0] + WINDOW_SECONDS - now
                seconds = math.ceil(remaining - 1e-6)
                return False, max(1, seconds)
            self._hits.append(now)
            return True, 0


class PublicCandidateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str = Field(min_length=1, max_length=160)
    provider: str = Field(min_length=1, max_length=120)
    channel: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=200)
    effort: str = Field(default="", max_length=80)
    plan: str = Field(min_length=1, max_length=80)
    feature_scope: str = Field(min_length=1, max_length=80)
    window_start: str | None = Field(default=None, max_length=64)
    window_end: str | None = Field(default=None, max_length=64)
    agent_id: str | None = Field(default=None, min_length=1, max_length=160)


class PublicEstimateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: str = Field(min_length=1, max_length=16)
    snapshot_id: str | None = None
    currency: str = Field(min_length=1, max_length=12)
    usage: dict[str, str] | None = None
    extra_cost: str | None = None
    reference_candidate_id: str | None = Field(default=None, max_length=160)
    scenario: ScenarioIn | None = None
    candidates: list[PublicCandidateIn] = Field(min_length=1, max_length=MAX_CANDIDATES)

    @field_validator("usage")
    @classmethod
    def usage_is_public(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        if any(not isinstance(item, str) for item in value.values()):
            raise ValueError("usage values must be decimal strings")
        for key, item in value.items():
            if key not in ALLOWED_USAGE or parse_decimal(item) is None:
                raise ValueError("usage must contain public decimal buckets")
        return value

    @field_validator("extra_cost")
    @classmethod
    def extra_cost_is_explicit_zero(cls, value: str | None) -> str | None:
        if value is None:
            return None
        number = parse_decimal(value)
        if number is None or number != 0:
            raise ValueError("extra_cost must be an explicit zero")
        return value


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _error_payload(code: str, message: str, details: list[dict] | None = None) -> dict:
    error = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"error": error}


def _json_response(
    payload: object,
    status: int = 200,
    cache_control: str = NO_STORE,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    headers = {"cache-control": cache_control}
    if extra_headers:
        headers.update(extra_headers)
    return Response(
        content=_canonical(payload),
        status_code=status,
        media_type="application/json",
        headers=headers,
    )


def _if_none_match(header: str | None, etag: str) -> bool:
    if header is None:
        return False
    text = header.strip()
    if text == "*":
        return True
    for match in _ETAG.finditer(text):
        if f'"{match.group(1)}"' == etag:
            return True
    return False


def _cached_json(request: Request, payload: object, cache_control: str) -> Response:
    body = _canonical(payload)
    etag = f'"{hashlib.sha256(body).hexdigest()}"'
    headers = {"etag": etag, "cache-control": cache_control}
    if _if_none_match(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, content=b"", headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


def _validation_details(errors: list) -> list[dict]:
    details = []
    for err in errors:
        loc = []
        for item in err.get("loc", ()):
            if isinstance(item, str | int):
                loc.append(item)
            else:
                loc.append(str(item))
        details.append({"type": str(err.get("type", "value_error")), "loc": loc})
    return details


def _resolve_paths(
    db_path: str | Path | None,
    manifest_path: str | Path | None,
) -> tuple[Path, Path]:
    if db_path is None and manifest_path is None:
        try:
            from agent_costbook.public_data import default_public_paths
        except ImportError as exc:
            raise PublicArtifactError("packaged public data is not available") from exc
        packaged_db, packaged_manifest = default_public_paths()
        return Path(packaged_db), Path(packaged_manifest)
    if db_path is None or manifest_path is None:
        raise PublicArtifactError("db_path and manifest_path are both required")
    return Path(db_path), Path(manifest_path)


def _load_manifest(db_path: Path, manifest_path: Path) -> dict:
    try:
        from agent_costbook import public_data
    except ImportError:
        return _read_manifest_file(manifest_path)
    validate_public_artifact = getattr(public_data, "validate_public_artifact", None)
    if validate_public_artifact is None:
        raise PublicArtifactError("public artifact validator is not available")
    manifest = validate_public_artifact(db_path, manifest_path)
    if not isinstance(manifest, dict):
        raise PublicArtifactError("public artifact validator returned an invalid manifest")
    return manifest


def _read_manifest_file(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublicArtifactError("public manifest is not available") from exc
    if not isinstance(payload, Mapping):
        raise PublicArtifactError("public manifest is not available")
    return dict(payload)


def _open_store(db_path: Path) -> Store:
    try:
        store = Store(db_path, readonly=True)
    except StoreError as exc:
        raise PublicArtifactError("public database is not available") from exc
    # Read-only startup checks open a deferred transaction. Release it so a
    # request does not keep a stale snapshot or a shared lock.
    store._conn.commit()
    store._conn.isolation_level = None
    return store


def _public_revision(store: Store, snapshot_id: str | None) -> tuple[int, str]:
    if snapshot_id is None:
        revision = store.revision_of(None)
        if revision is None:
            raise PublicError(503, "not_ready", "The public catalog is not ready.")
        return int(revision), CURRENT_CACHE
    if _PUBLIC_SNAPSHOT.fullmatch(snapshot_id) is None:
        raise PublicError(404, "snapshot_not_found", "Snapshot was not found.")
    revision = store.revision_of(snapshot_id)
    if revision is None:
        raise PublicError(404, "snapshot_not_found", "Snapshot was not found.")
    return int(revision), PINNED_CACHE


def _linked_source_ids(store: Store) -> tuple[set[str], set[str]]:
    evidence: set[str] = set()
    research: set[str] = set()
    latest = store.revision_of(None)
    if not latest:
        return evidence, research
    for version in range(1, int(latest) + 1):
        for row in store.catalog(f"snap-{version}"):
            evidence.update(row.get("evidence_ids") or [])
            research_id = row.get("research_id")
            if isinstance(research_id, str) and research_id:
                research.add(research_id)
    return evidence, research


def _capability_document(store: Store) -> dict:
    current = store.current_capabilities()
    agents = sorted(current["agents"], key=lambda row: row["agent_id"])
    efforts = sorted(
        current["model_efforts"],
        key=lambda row: (row["provider"], row["model"], row.get("effort") or ""),
    )
    digest = hashlib.sha256(
        _canonical({"agents": agents, "model_efforts": efforts})
    ).hexdigest()
    return {
        "kind": "agent-costbook.capabilities",
        "schema_version": 1,
        "publisher_id": store.publisher_id(),
        "content_sha256": digest,
        "agents": agents,
        "model_efforts": efforts,
    }


def _info(store: Store) -> dict:
    revision = store.revision_of(None)
    if revision is None:
        catalog = {"snapshot_id": None, "data_version": None, "content_sha256": None}
    else:
        document = build_document(store, int(revision))
        catalog = {
            "snapshot_id": document["snapshot_id"],
            "data_version": document["data_version"],
            "content_sha256": document["content_sha256"],
        }
    capabilities = _capability_document(store)
    return {
        "kind": "agent-costbook.service",
        "schema_version": 1,
        "api_version": 1,
        "service_version": SERVICE_VERSION,
        "profile": "public-reference",
        "publisher_id": store.publisher_id(),
        "catalog": catalog,
        "capabilities": {
            "content_sha256": capabilities["content_sha256"],
            "agent_count": len(capabilities["agents"]),
            "model_effort_count": len(capabilities["model_efforts"]),
        },
        "access": {
            "anonymous_read": True,
            "remote_write": False,
            "admin_publication": "reviewed-artifact",
        },
        "limits": {
            "body_bytes": BODY_BYTES,
            "candidates": MAX_CANDIDATES,
            "requests": REQUESTS_PER_WINDOW,
            "window_seconds": WINDOW_SECONDS,
            "scope": "instance",
        },
        "usage_assumptions": {"supported": False},
    }


def _effort_name(effort: str | None) -> str | None:
    if effort is None or effort == "":
        return None
    if not effort.strip():
        raise PublicError(
            422,
            "invalid_public_request",
            "The request is invalid.",
            details=[{"type": "value_error", "loc": ["query", "effort"]}],
        )
    return effort


def _public_estimate(payload: dict) -> dict:
    results = []
    for result in payload["results"]:
        item = dict(result)
        item.pop("record_snapshot_id", None)
        item["cost_basis"] = "public-reference"
        results.append(item)
    body = dict(payload)
    body["results"] = results
    body["kind"] = "agent-costbook.estimate"
    body["schema_version"] = 1
    body["cost_basis"] = "public-reference"
    return body


def _headers(scope: dict) -> dict[str, str]:
    found = {}
    for key, value in scope.get("headers") or []:
        found[key.decode("latin1").lower()] = value.decode("latin1")
    return found


async def _read_limited(receive, limit: int) -> bytes | None:
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return b""
        if message["type"] != "http.request":
            continue
        piece = message.get("body", b"")
        total += len(piece)
        if total > limit:
            return None
        chunks.append(piece)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _replay(body: bytes):
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


class PublicGuard:
    def __init__(self, app, limiter: InstanceRateLimiter):
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")
        if method == "GET" and path in {"/health", "/ready"}:
            await self.app(scope, receive, send)
            return
        allowed, retry_after = self.limiter.decide()
        if not allowed:
            response = _json_response(
                _error_payload("rate_limited", "The instance request limit was reached."),
                status=429,
                extra_headers={"retry-after": str(retry_after)},
            )
            await response(scope, receive, send)
            return
        if method not in {"GET", "HEAD"} and not (method == "POST" and path == "/v1/estimates"):
            response = _json_response(
                _error_payload("public_read_only", "The public service is read only."),
                status=403,
            )
            await response(scope, receive, send)
            return
        if method == "POST":
            length = _headers(scope).get("content-length")
            if length is not None:
                try:
                    announced = int(length)
                except ValueError:
                    announced = None
                if announced is not None and announced > BODY_BYTES:
                    response = _json_response(
                        _error_payload(
                            "request_too_large",
                            "The request body is larger than the public limit.",
                        ),
                        status=413,
                    )
                    await response(scope, receive, send)
                    return
            body = await _read_limited(receive, BODY_BYTES)
            if body is None:
                response = _json_response(
                    _error_payload(
                        "request_too_large",
                        "The request body is larger than the public limit.",
                    ),
                    status=413,
                )
                await response(scope, receive, send)
                return
            receive = _replay(body)
        await self.app(scope, receive, send)


def create_public_app(
    db_path: str | Path | None = None,
    manifest_path: str | Path | None = None,
) -> FastAPI:
    database, manifest_path = _resolve_paths(db_path, manifest_path)
    manifest = _load_manifest(database, manifest_path)
    store = _open_store(database)
    limiter = InstanceRateLimiter()
    app = FastAPI(
        title="agent-costbook",
        version=SERVICE_VERSION,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.state.store = store
    app.state.manifest = manifest
    app.state.limiter = limiter
    app.add_middleware(PublicGuard, limiter=limiter)

    @app.exception_handler(PublicError)
    async def public_error(_request: Request, exc: PublicError) -> Response:
        return _json_response(
            _error_payload(exc.code, exc.message, exc.details),
            status=exc.status,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, exc: RequestValidationError) -> Response:
        return _json_response(
            _error_payload(
                "invalid_public_request",
                "The request is invalid.",
                _validation_details(exc.errors()),
            ),
            status=422,
        )

    @app.get("/health")
    def health(request: Request) -> Response:
        return _cached_json(request, {"status": "ok"}, NO_STORE)

    @app.get("/ready")
    def ready(request: Request) -> Response:
        try:
            revision = store.revision_of(None)
            store.publisher_id()
        except StoreError as exc:
            raise PublicError(503, "not_ready", "The public catalog is not ready.") from exc
        if revision is None:
            raise PublicError(503, "not_ready", "The public catalog is not ready.")
        return _cached_json(request, {"status": "ready"}, NO_STORE)

    @app.get("/v1/info")
    def info(request: Request) -> Response:
        try:
            payload = _info(store)
        except (StoreError, ExportError) as exc:
            raise PublicError(503, "not_ready", "The public catalog is not ready.") from exc
        return _cached_json(request, payload, CURRENT_CACHE)

    @app.get("/v1/catalog")
    def catalog(request: Request, snapshot_id: str | None = None) -> Response:
        revision, cache_control = _public_revision(store, snapshot_id)
        try:
            document = build_document(store, revision)
        except ExportError as exc:
            raise PublicError(503, "not_ready", "The public catalog is not ready.") from exc
        return _cached_json(request, document, cache_control)

    @app.get("/v1/capabilities")
    def capabilities(request: Request) -> Response:
        return _cached_json(request, _capability_document(store), CURRENT_CACHE)

    @app.get("/v1/capabilities/agents")
    def agent(
        request: Request,
        agent_id: Annotated[str, Query(min_length=1, max_length=160)],
    ) -> Response:
        row = store.agent_capability(agent_id)
        if row is None:
            raise PublicError(404, "capability_not_found", "Capability was not found.")
        return _cached_json(request, row, CURRENT_CACHE)

    @app.get("/v1/capabilities/model-efforts")
    def model_effort(
        request: Request,
        provider: Annotated[str, Query(min_length=1, max_length=120)],
        model: Annotated[str, Query(min_length=1, max_length=200)],
        effort: Annotated[str | None, Query(max_length=80)] = None,
    ) -> Response:
        row = store.model_effort(provider, model, _effort_name(effort))
        if row is None:
            raise PublicError(404, "capability_not_found", "Capability was not found.")
        return _cached_json(request, row, CURRENT_CACHE)

    @app.get("/v1/evidence/{evidence_id}")
    def evidence(
        request: Request,
        evidence_id: Annotated[str, ApiPath(min_length=1, max_length=200)],
    ) -> Response:
        linked, _research = _linked_source_ids(store)
        row = store.get_evidence(evidence_id)
        if row is None or evidence_id not in linked:
            raise PublicError(404, "evidence_not_found", "Evidence was not found.")
        return _cached_json(request, row, CURRENT_CACHE)

    @app.get("/v1/research/{research_id}")
    def research(
        request: Request,
        research_id: Annotated[str, ApiPath(min_length=1, max_length=200)],
    ) -> Response:
        _evidence, linked = _linked_source_ids(store)
        row = store.get_research(research_id)
        if row is None or research_id not in linked:
            raise PublicError(404, "research_not_found", "Research was not found.")
        return _cached_json(request, row, CURRENT_CACHE)

    @app.post("/v1/estimates")
    def estimates(body: PublicEstimateIn) -> Response:
        if body.method == "M7":
            raise PublicError(
                403,
                "private_measurement_unavailable",
                "Private measured estimates are not available.",
            )
        _public_revision(store, body.snapshot_id)
        try:
            estimate = EstimateIn.model_validate(body.model_dump())
        except ValidationError as exc:
            raise PublicError(
                422,
                "invalid_public_request",
                "The request is invalid.",
                _validation_details(exc.errors()),
            ) from None
        payload = build_estimate_response(store, estimate, authorized=True)
        return _json_response(_public_estimate(payload))

    @app.get("/openapi.json")
    def openapi(request: Request) -> Response:
        if app.openapi_schema is None:
            schema = get_openapi(
                title=app.title,
                version=app.version,
                routes=app.routes,
            )
            components = schema.get("components")
            if isinstance(components, dict):
                components.pop("securitySchemes", None)
            app.openapi_schema = schema
        return _cached_json(request, app.openapi_schema, CURRENT_CACHE)

    @app.get("/{path:path}", include_in_schema=False)
    def unknown(path: str) -> Response:
        # The path is matched only so unknown GETs use the public error shape.
        del path
        raise PublicError(404, "not_found", "Resource was not found.")

    return app
