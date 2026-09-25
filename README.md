# agent-costbook

Local service for traceable AI model pricing, subscription amortization, and
per-task API cost estimates. Version 0.2 stores evidence, research Markdown,
API rates, and subscription inputs in SQLite. It calculates M0 display, M1
quota price, M2 task amortization, M4 task cost, and M6 budget amortization.
M3, M5, and M7 respond with `unsupported_method`.

Design notes live in `.docs/`, which is outside this product Git history.

## Run

```sh
uv sync --extra dev
export ACB_DB=agent_costbook.sqlite3
export ACB_ADMIN_TOKEN=choose-a-local-token
uv run uvicorn agent_costbook.api:create_app --factory --host 127.0.0.1 --port 8080
```

Keep the bind address on `127.0.0.1`. Writes require `Authorization: Bearer $ACB_ADMIN_TOKEN`.
Money and token amounts are JSON decimal strings. An unknown rate is omitted, not stored as 0.
An unknown multiplier, baseline budget, or utilization is not filled with 1. Omitted M6
weight uses 1 and is labeled `default_weight_one`; that default is not written back to the catalog.
Request fields `private_rates`, `marginal_cash`, and `private_subscription.monthly_price` change
only that estimate and are not written to the catalog or the export file. A price period other
than `month` is rejected instead of being treated as a monthly fee. M1 ranks only inside an
explicit `baseline_group`. M2 and M6 can compare different models when the task profile,
currency, period, scope, and each candidate's own capacity basis match.
Cash, amortization, API-equivalent cost, and quota are separate metrics and are not added together.

With the service stopped, export reads an already migrated database and does not change it.
If the file still has an older schema, export stops and tells you to open the service once so
the schema can migrate. Repeat exports of the same database are byte-identical.
`snap-<data_version>` reads that published catalog; the original `snap_<uuid>` id still works.
An unknown or unsafe version exits non-zero and prints no catalog. Catalog and estimate
responses carry the same publisher, data version, public snapshot id, formula set, and
records hash as the export. Each candidate still reports its own method formula.

```sh
uv run agent-costbook-export --db agent_costbook.sqlite3
uv run agent-costbook-export --db agent_costbook.sqlite3 --data-version 1
```

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
