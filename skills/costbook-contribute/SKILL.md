---
name: costbook-contribute
description: Contribute a public pricing or subscription observation to a local agent-costbook service. Use when an existing agent has already retrieved a source and needs to submit evidence, research Markdown, and structured rates or subscription inputs through the HTTP API.
---

# Contribute to agent-costbook

The service does not research for you. Retrieve the public source yourself, then submit the observation through the API. Do not place the product record only in development `.docs`.

The service separately collects two fixed OpenRouter JSON URLs. That collector is not a research agent, there is no research-job endpoint, and it does not crawl HTML or forums. Different endpoint tags are different channels. Two prices for the same tag are published as `status: conflict` on a new snapshot. Do not fill an unknown price with 0 or an unknown multiplier with 1.

To recalculate a published file without the HTTP service:

```sh
ac estimate --snapshot snapshot.json --request estimate.json --publisher "$PUBLISHER_ID"
```

The request body matches `POST /v1/estimates`. If you set `snapshot_id`, it must be the file's `snap-N`. Old published files do not contain `task_profile` or `baseline_group`. Metrics are still calculated from the fields that are present. A reference comparison on those files returns `comparison: unavailable` and does not invent the missing context. New publications include both scope fields. A file whose `formula_version` is `ac-formulas-v1` or `ac-formulas-v2` can be recalculated. `ac-formulas-v1` keeps its original M0–M6 meaning. Any other formula set returns `unsupported_formula`. Record freshness follows source `retrieved_at`, not the snapshot's publish time.

## Before writing

1. Confirm the service is the local process on `127.0.0.1`.
2. Read the source URL yourself. Keep an excerpt, not a whole site.
3. Record `source_kind` separately from `collector_kind` and `collector_name`.
4. Convert the source units explicitly. OpenRouter's USD-per-token prices become USD per million tokens by multiplying by 1000000.
5. Omit any rate or subscription input the source does not state. Do not store 0 for an unknown price or 1 for an unknown multiplier, baseline budget, or utilization. A subscription record may include `subscription.monthly_price`, `price_period` (`month` for calculations), `quota_multiplier`, `baseline_tasks`, `measured_tasks`, `baseline_api_budget`, `utilization`, `cost_per_task`, `weight`, `task_profile`, and `baseline_group`. Omitting `weight` does not store 1.
6. Leave `effort` empty when the source does not name one, and do not reuse that record for another effort.
7. Keep evidence and research Markdown at or below 256 KiB each.

## Submit

Money and amounts are decimal strings. Writes use `Authorization: Bearer $ACB_ADMIN_TOKEN`.

```sh
curl -sS -X POST "http://127.0.0.1:8080/v1/contributions" \
  -H "Authorization: Bearer $ACB_ADMIN_TOKEN" \
  -H "Idempotency-Key: source-and-model-and-retrieval-time" \
  -H "Content-Type: application/json" \
  -d @contribution.json
```

`contribution.json` has `research` (`title`, `markdown`), `evidence` (`source_kind`, `source_url`, `collector_kind`, `collector_name`, `content`, `retrieved_at`), and `records` (`provider`, `channel`, `model`, `effort`, `plan`, `feature_scope`, `currency`, `rates`, `evidence_indexes`). Rate fields are `uncached_input_per_million`, `cache_read_per_million`, `cache_write_per_million`, and `billed_output_per_million`. To replace an identity that is already published, set `base_snapshot_id` to the record version you reviewed. A missing or stale base is a conflict and does not overwrite the active record. Omitting usage does not mean zero tokens.

Publish only after the draft ids look right:

```sh
curl -sS -X POST "http://127.0.0.1:8080/v1/contributions/$CONTRIBUTION_ID/publish" \
  -H "Authorization: Bearer $ACB_ADMIN_TOKEN"
```

The same idempotency key with the same body returns the original draft. The same key with different content returns 409. Two different rates for one identity in a single contribution also return 409 and do not publish.

## Check the loop

```sh
curl -sS "http://127.0.0.1:8080/v1/evidence/$EVIDENCE_ID"
curl -sS "http://127.0.0.1:8080/v1/research/$RESEARCH_ID?format=markdown"
curl -sS "http://127.0.0.1:8080/v1/catalog"
```

M4 sums uncached input, cache read, cache write, and billed output, each times its USD-per-million rate, divides by 1000000, then adds `extra_cost`. Send `"0"` when there is no extra cost; an omitted extra cost stays missing. Reasoning tokens already included in billed output must be sent as `reasoning` and are not added again. A second output bucket such as `output` is `invalid_input`.

```sh
curl -sS -X POST "http://127.0.0.1:8080/v1/estimates" \
  -H "Content-Type: application/json" \
  -d '{"method":"M4","currency":"USD","usage":{"uncached_input":"1000","billed_output":"400"},"extra_cost":"0","candidates":[{"candidate_id":"example","provider":"openai","channel":"openrouter","model":"openai/gpt-4o-mini","plan":"payg","feature_scope":"text"}]}'
```

`private_rates` on a candidate overrides that request only. Read the catalog afterward and confirm the stored rate did not change. M0 returns the stored rates. M1 is `monthly_price / quota_multiplier` inside one provider, feature scope, and `baseline_group`. The same group name does not compare another provider. `GET /v1/catalog` returns the canonical published records, the same body covered by `content_sha256`. M2 amortizes price over `quota_multiplier * baseline_tasks * utilization`, unless `measured_tasks` is present, in which case that count is not multiplied by utilization again. M6 amortizes price over the API-equivalent task count `quota_multiplier * baseline_api_budget * utilization / cost_per_task`, with `weight` applied only to the cost. M3 is the M2 cost divided by `subscription.weight` (`K = P / (N × w)`). M5 is the M4 cost divided by that same weight (`K = C / w`). When the weight is omitted, both use 1 and report `capability_proxy_disabled`. The weight is a capability proxy, not a measured success rate; do not multiply it again in routing. Pass `snapshot_id` to recalculate against an older published snapshot. `marginal_cash` is optional and is not inferred as 0.

Measured task results are a separate write, not a catalog contribution:

```sh
curl -sS -X POST "http://127.0.0.1:8080/v1/observations" \
  -H "Authorization: Bearer $ACB_ADMIN_TOKEN" \
  -H "Idempotency-Key: period-and-task-category" \
  -H "Content-Type: application/json" \
  -d @observation.json
```

`observation.json` names the same provider, channel, model, effort, plan, feature scope, and currency as an estimate candidate. `period_start` and `period_end` are ISO timestamps, `subscription_cash` is the period fee counted once, and each task has a `task_id` plus attempts of `cash`, optional `api_equivalent`, and `succeeded`. The same task id counts as one success even if a later attempt also succeeds. Cash from failed and retried attempts is kept. API-equivalent amounts are not cash. The same key with a different body returns 409, and a second different body for the same scope also returns 409. The service does not read agent credentials and does not copy task text into the catalog or export. Ask for M7 with `task_category`, `acceptance`, and the candidate's `window_start` / `window_end`. No imported sample returns `missing_data`. `S = 0` with cash above 0 returns `unbounded`; `S = 0` with cash 0 returns `insufficient_data`.
