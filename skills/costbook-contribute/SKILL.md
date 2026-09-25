---
name: costbook-contribute
description: Contribute a public pricing or subscription observation to a local agent-costbook service. Use when an existing agent has already retrieved a source and needs to submit evidence, research Markdown, and structured rates or subscription inputs through the HTTP API.
---

# Contribute to agent-costbook

The service does not research for you. Retrieve the public source yourself, then submit the observation through the API. Do not place the product record only in development `.docs`.

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

`private_rates` on a candidate overrides that request only. Read the catalog afterward and confirm the stored rate did not change. M0 returns the stored rates. M1 is `monthly_price / quota_multiplier` inside one provider, feature scope, and `baseline_group`. The same group name does not compare another provider. `GET /v1/catalog` returns the canonical published records, the same body covered by `content_sha256`. M2 amortizes price over `quota_multiplier * baseline_tasks * utilization`, unless `measured_tasks` is present, in which case that count is not multiplied by utilization again. M6 amortizes price over the API-equivalent task count `quota_multiplier * baseline_api_budget * utilization / cost_per_task`, with `weight` applied only to the cost. Set `reference_candidate_id` to compare against an explicit candidate. M3, M5, and M7 return `unsupported_method`. Pass `snapshot_id` to recalculate against an older published snapshot. `marginal_cash` is optional and is not inferred as 0.
