# agent-costbook

Local service for traceable AI model pricing, subscription amortization, and
per-task API cost estimates. Version 0.3 stores evidence, research Markdown,
API rates, and subscription inputs in SQLite. It calculates M0 display, M1
quota price, M2 task amortization, M4 task cost, and M6 budget amortization.
M3, M5, and M7 respond with `unsupported_method`. A snapshot whose
`formula_version` is not `ac-formulas-v1` is refused as `unsupported_formula`
instead of being recalculated with the current formulas.

Design notes live in `.docs/`, which is outside this product Git history.

## Run

```sh
uv sync --extra dev
export ACB_DB=agent_costbook.sqlite3
export ACB_ADMIN_TOKEN=choose-a-local-token
uv run uvicorn agent_costbook.api:create_app --factory --host 127.0.0.1 --port 8080
```

Keep the bind address on `127.0.0.1`. Writes require `Authorization: Bearer $ACB_ADMIN_TOKEN`.
Money and token amounts are JSON decimal strings. An unknown rate is omitted from storage, not stored as 0.
The public catalog returns that missing rate as null.
An unknown multiplier, baseline budget, or utilization is not filled with 1. Omitted M6
weight uses 1 and is labeled `default_weight_one`; that default is not written back to the catalog.
Request fields `private_rates`, `marginal_cash`, and `private_subscription.monthly_price` change
only that estimate and are not written to the catalog or the export file. A price period other
than `month` is rejected instead of being treated as a monthly fee. M1 ranks only inside an
explicit `baseline_group` of the same provider and feature. The same group name does not
compare plans from different providers. M2 and M6 can compare different models when the task profile,
currency, period, scope, and each candidate's own capacity basis match.
Cash, amortization, API-equivalent cost, and quota are separate metrics and are not added together.

With the service stopped, export reads an already migrated database and does not change it.
If the file still has an older schema, export stops and tells you to open the service once so
the schema can migrate. Repeat exports of the same database are byte-identical.
`snap-<data_version>` reads that published catalog. A request may still pass the original
`snap_<uuid>`, and the public response uses `snap-<data_version>`. `record_snapshot_id` on an
estimate keeps the stored uuid. An unknown version is HTTP 404 or a non-zero export with no
catalog. With nothing published, `data_version` is null. `GET /v1/catalog` returns the same
canonical records as export, and `content_sha256` is the hash of those records. Every candidate
in one estimate echoes that publication's publisher, data version, and `snap-N`, including a
candidate with missing data. The envelope `formula_version` is the formula set. Each candidate's
`formula_version` remains its own method.

```sh
uv run agent-costbook-export --db agent_costbook.sqlite3
uv run agent-costbook-export --db agent_costbook.sqlite3 --data-version 1
uv run ac estimate --snapshot snapshot.json --request examples/estimate-request.json --publisher "$PUBLISHER_ID"
uv run ac collect --once --db agent_costbook.sqlite3
```

`ac estimate` reads one published snapshot file, checks its kind, schema,
publisher, `snap-N` version, record hash, and formula set, then uses the same
calculation core as `POST /v1/estimates`. It does not rebuild a database.
Publications written by this version include `scope.task_profile` and
`scope.baseline_group` so a reference comparison can be repeated offline.
Older snapshot files keep their original bytes and do not gain those keys on
upgrade. For those files, candidate metrics are still calculated, and a
reference comparison returns `comparison: unavailable` rather than a rank.
`read_view.freshness.stale` stays null unless both a timestamp and `--now`
are supplied. `record_snapshot_id` is the service's internal id; the file
carries `snapshot_id` (`snap-N`) and offline results leave the internal id empty.

`ac collect --once` is one scheduled tick for two public OpenRouter sources:
the model catalog and the fixed `openai/gpt-4o-mini` endpoints page. Put it
on a timer. A failed fetch, a changed page layout, or disagreeing prices keep
the previous snapshot active. Unknown prices are omitted, not stored as 0.
The service does not run a research agent.

## HTTP

- `GET /health`
- `GET /v1/catalog`
- `POST /v1/estimates`
- `GET /v1/evidence/{id}`
- `GET /v1/research/{id}` and `GET /v1/research/{id}?format=markdown`
- `POST /v1/contributions`
- `POST /v1/contributions/{id}/publish`

A snapshot is the whole catalog as of that publication. Replacing an existing identity
requires `base_snapshot_id` of the reviewed record version. Estimates pin one catalog
revision for every candidate. Omitting usage does not mean the tokens were zero.
There is no research-job endpoint.

## Tests

```sh
uv sync --extra dev
uv run pytest -q
```
