from __future__ import annotations

import hmac

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from agent_costbook.estimates import (
    apply_reference_comparison,
    apply_scenario,
    evaluate_candidate,
)
from agent_costbook.export import catalog_document
from agent_costbook.models import ContributionIn, EstimateIn
from agent_costbook.settings import Settings, load_settings
from agent_costbook.store import Store, StoreError

# None inside the store means "read the live catalog". An estimate that starts
# with no published snapshot must keep that empty revision for every candidate.
_FROZEN_EMPTY_SNAPSHOT = ""


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
    app = FastAPI(title="agent-costbook", version="0.2.0")
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

    @app.post("/v1/estimates")
    def estimates(body: EstimateIn) -> dict:
        revision = store.revision_of(body.snapshot_id)
        if body.snapshot_id is not None and revision is None:
            raise HTTPException(status_code=404, detail="snapshot not found")
        frozen_snapshot = f"snap-{revision}" if revision is not None else _FROZEN_EMPTY_SNAPSHOT
        selected = []
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
                snapshot_id=frozen_snapshot,
            )
            selected.append((candidate, selection))
        reference_record = None
        if body.reference_candidate_id:
            for candidate, selection in selected:
                if candidate.candidate_id == body.reference_candidate_id:
                    reference_record = selection.record
        scenario = body.scenario.model_dump() if body.scenario is not None else None
        results = []
        contexts = []
        for candidate, selection in selected:
            subscription, notes = apply_scenario(
                selection.record,
                reference_record,
                scenario,
            )
            private = None
            if candidate.private_rates is not None:
                private = candidate.private_rates.model_dump(exclude_none=True)
            private_subscription = None
            if candidate.private_subscription is not None:
                private_subscription = candidate.private_subscription.model_dump(exclude_none=True)
            result = evaluate_candidate(
                candidate_id=candidate.candidate_id,
                method=body.method,
                usage=body.usage,
                extra_cost=body.extra_cost,
                currency=body.currency,
                private_rates=private or None,
                record=selection.record,
                conflict=selection.conflict,
                marginal_cash=candidate.marginal_cash,
                subscription_override=subscription,
                scenario_notes=notes,
                private_subscription=private_subscription,
            )
            result["record_snapshot_id"] = (
                selection.record.get("snapshot_id") if selection.record else None
            )
            record = selection.record or {}
            stored = record.get("subscription") or {}
            contexts.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "provider": record.get("provider", candidate.provider),
                    "feature_scope": record.get("feature_scope", candidate.feature_scope),
                    "window_start": record.get("window_start") or "",
                    "window_end": record.get("window_end") or "",
                    "currency": record.get("currency"),
                    "model": record.get("model", candidate.model),
                    "effort": record.get("effort", candidate.effort),
                    "task_profile": stored.get("task_profile") or "",
                    "price_period": stored.get("price_period") or "",
                    "baseline_group": stored.get("baseline_group") or "",
                    "has_own_budget": bool(stored.get("baseline_api_budget")),
                    "has_own_tasks": bool(
                        stored.get("baseline_tasks") or stored.get("measured_tasks")
                    ),
                }
            )
            results.append(result)
        if body.reference_candidate_id:
            allow_cross_provider = bool(
                scenario
                and (
                    scenario.get("equal_baseline_budget")
                    or scenario.get("equal_baseline_tasks")
                )
            )
            results = apply_reference_comparison(
                body.method,
                results,
                contexts,
                reference_candidate_id=body.reference_candidate_id,
                allow_cross_provider=allow_cross_provider,
            )
        published = catalog_document(store, revision)
        published.pop("records")
        for result in results:
            result["publisher_id"] = published["publisher_id"]
            result["data_version"] = published["data_version"]
            result["snapshot_id"] = published["snapshot_id"]
        return {**published, "results": results}

    return app


