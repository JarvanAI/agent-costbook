from __future__ import annotations

import hmac

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from agent_costbook import __version__
from agent_costbook.estimates import run_estimate
from agent_costbook.export import catalog_document
from agent_costbook.models import CandidateIn, ContributionIn, EstimateIn, ObservationIn
from agent_costbook.observations import aggregate_observation
from agent_costbook.settings import Settings, load_settings
from agent_costbook.store import Store, StoreError

# None inside the store means "read the live catalog". An estimate that starts
# with no published snapshot must keep that empty revision for every candidate.
_FROZEN_EMPTY_SNAPSHOT = ""


def _candidate_dict(candidate: CandidateIn) -> dict:
    private = None
    if candidate.private_rates is not None:
        private = candidate.private_rates.model_dump(exclude_none=True)
    private_subscription = None
    if candidate.private_subscription is not None:
        private_subscription = candidate.private_subscription.model_dump(exclude_none=True)
    return {
        "candidate_id": candidate.candidate_id,
        "provider": candidate.provider,
        "channel": candidate.channel,
        "model": candidate.model,
        "effort": candidate.effort,
        "plan": candidate.plan,
        "feature_scope": candidate.feature_scope,
        "window_start": candidate.window_start,
        "window_end": candidate.window_end,
        "private_rates": private or None,
        "private_subscription": private_subscription,
        "marginal_cash": candidate.marginal_cash,
    }


def _authorized(settings: Settings, authorization: str | None) -> bool:
    if not settings.admin_token or not authorization or not authorization.startswith("Bearer "):
        return False
    provided = authorization.removeprefix("Bearer ")
    return hmac.compare_digest(provided, settings.admin_token)


def _require_admin(settings: Settings, authorization: str | None) -> None:
    if not _authorized(settings, authorization):
        raise HTTPException(status_code=401, detail="admin token required")


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or load_settings()
    store = Store(resolved.db_path)
    app = FastAPI(title="agent-costbook", version=__version__)
    app.state.store = store
    app.state.settings = resolved

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/v1/catalog")
    def catalog(snapshot_id: str | None = None) -> dict:
        revision = store.revision_of(snapshot_id)
        if snapshot_id is not None and revision is None:
            raise HTTPException(status_code=404, detail="snapshot not found")
        return catalog_document(store, revision)

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

    @app.post("/v1/observations", status_code=201)
    def observe(
        body: ObservationIn,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        _require_admin(resolved, authorization)
        payload = body.model_dump(mode="json", exclude_none=True)
        try:
            recorded, created = store.record_observation(
                payload,
                aggregate_observation(payload),
                idempotency_key,
            )
        except StoreError as exc:
            if exc.code in {"idempotency_conflict", "scope_conflict"}:
                return JSONResponse(
                    status_code=409,
                    content={"status": "conflict", "detail": exc.code},
                )
            raise
        return JSONResponse(status_code=201 if created else 200, content=recorded)

    @app.post("/v1/estimates")
    def estimates(
        body: EstimateIn,
        authorization: str | None = Header(default=None),
    ) -> dict:
        if body.method == "M7":
            _require_admin(resolved, authorization)
        revision = store.revision_of(body.snapshot_id)
        if body.snapshot_id is not None and revision is None:
            raise HTTPException(status_code=404, detail="snapshot not found")
        frozen_snapshot = f"snap-{revision}" if revision is not None else _FROZEN_EMPTY_SNAPSHOT
        selections = [
            store.select_record(
                provider=candidate.provider,
                channel=candidate.channel,
                model=candidate.model,
                effort=candidate.effort,
                plan=candidate.plan,
                feature_scope=candidate.feature_scope,
                window_start=candidate.window_start,
                window_end=candidate.window_end,
                currency=body.currency,
                snapshot_id=frozen_snapshot,
            )
            for candidate in body.candidates
        ]
        for selection in selections:
            if selection.record is not None:
                selection.record["source_retrieved_at"] = store.earliest_retrieved_at(
                    selection.record.get("evidence_ids") or []
                )
        scenario = body.scenario.model_dump() if body.scenario is not None else None
        published = catalog_document(store, revision)
        measurements = None
        if body.method == "M7":
            measurements = [
                store.find_measurement(
                    provider=candidate.provider,
                    channel=candidate.channel,
                    model=candidate.model,
                    effort=candidate.effort,
                    plan=candidate.plan,
                    feature_scope=candidate.feature_scope,
                    currency=body.currency,
                    period_start=candidate.window_start,
                    period_end=candidate.window_end,
                    task_category=body.task_category,
                    acceptance=body.acceptance,
                )
                for candidate in body.candidates
            ]
        results = run_estimate(
            method=body.method,
            usage=body.usage,
            extra_cost=body.extra_cost,
            currency=body.currency,
            scenario=scenario,
            reference_candidate_id=body.reference_candidate_id,
            candidates=[_candidate_dict(candidate) for candidate in body.candidates],
            selections=selections,
            measurements=measurements,
            formula_set=published.get("formula_version"),
        )
        published.pop("records")
        for result in results:
            result["publisher_id"] = published["publisher_id"]
            result["data_version"] = published["data_version"]
            result["snapshot_id"] = published["snapshot_id"]
        return {**published, "results": results}

    return app


