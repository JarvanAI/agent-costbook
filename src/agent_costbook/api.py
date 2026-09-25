from __future__ import annotations

import hmac

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from agent_costbook.estimates import evaluate_candidate
from agent_costbook.models import ContributionIn, EstimateIn
from agent_costbook.settings import Settings, load_settings
from agent_costbook.store import Store, StoreError


def _authorized(settings: Settings, authorization: str | None) -> bool:
    if not settings.admin_token or not authorization or not authorization.startswith("Bearer "):
        return False
    provided = authorization.removeprefix("Bearer ")
    return hmac.compare_digest(provided, settings.admin_token)


def _require_admin(settings: Settings, authorization: str | None) -> None:
    if not _authorized(settings, authorization):
        raise HTTPException(status_code=401, detail="admin token required")


def _public_record(record: dict) -> dict:
    shown = dict(record)
    shown["window_start"] = shown["window_start"] or None
    shown["window_end"] = shown["window_end"] or None
    return shown


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or load_settings()
    store = Store(resolved.db_path)
    app = FastAPI(title="agent-costbook", version="0.1.0")
    app.state.store = store
    app.state.settings = resolved

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/v1/catalog")
    def catalog(snapshot_id: str | None = None) -> dict:
        try:
            records = store.catalog(snapshot_id)
        except StoreError as exc:
            if exc.code == "not_found":
                raise HTTPException(status_code=404, detail="snapshot not found") from exc
            raise
        return {"records": [_public_record(record) for record in records]}

    @app.get("/v1/evidence/{evidence_id}")
    def evidence(evidence_id: str) -> dict:
        row = store.get_evidence(evidence_id)
        if row is None:
            raise HTTPException(status_code=404, detail="evidence not found")
        return row

    @app.get("/v1/research/{research_id}")
    def research(research_id: str, format: str = "json"):
        row = store.get_research(research_id)
        if row is None:
            raise HTTPException(status_code=404, detail="research not found")
        if format == "markdown":
            return PlainTextResponse(row["markdown"], media_type="text/markdown; charset=utf-8")
        if format != "json":
            raise HTTPException(status_code=422, detail="format must be json or markdown")
        return row

    @app.post("/v1/contributions", status_code=201)
    def contribute(
        body: ContributionIn,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        _require_admin(resolved, authorization)
        try:
            payload, created = store.create_contribution(
                body.model_dump(mode="json", exclude_none=True),
                idempotency_key,
            )
        except StoreError as exc:
            if exc.code == "idempotency_conflict":
                return JSONResponse(
                    status_code=409,
                    content={
                        "status": "conflict",
                        "detail": "idempotency key reused with different content",
                    },
                )
            raise
        return JSONResponse(status_code=201 if created else 200, content=payload)

    @app.post("/v1/contributions/{contribution_id}/publish")
    def publish(
        contribution_id: str,
        authorization: str | None = Header(default=None),
    ):
        _require_admin(resolved, authorization)
        try:
            return store.publish(contribution_id)
        except StoreError as exc:
            if exc.code == "not_found":
                raise HTTPException(status_code=404, detail="contribution not found") from exc
            if exc.code == "conflict":
                return JSONResponse(
                    status_code=409,
                    content={"contribution_id": contribution_id, "status": "conflict"},
                )
            raise

    @app.post("/v1/estimates")
    def estimates(body: EstimateIn) -> dict:
        if body.snapshot_id is not None and not store.snapshot_exists(body.snapshot_id):
            raise HTTPException(status_code=404, detail="snapshot not found")
        results = []
        for candidate in body.candidates:
            selection = store.select_record(
                provider=candidate.provider,
                channel=candidate.channel,
                model=candidate.model,
                effort=candidate.effort,
                plan=candidate.plan,
                feature_scope=candidate.feature_scope,
                window_start=candidate.window_start,
                window_end=candidate.window_end,
                currency=body.currency,
                snapshot_id=body.snapshot_id,
            )
            private = None
            if candidate.private_rates is not None:
                private = candidate.private_rates.model_dump(exclude_none=True)
            results.append(
                evaluate_candidate(
                    candidate_id=candidate.candidate_id,
                    method=body.method,
                    usage=body.usage,
                    extra_cost=body.extra_cost,
                    currency=body.currency,
                    private_rates=private or None,
                    record=selection.record,
                    conflict=selection.conflict,
                )
            )
        return {"results": results}

    return app


