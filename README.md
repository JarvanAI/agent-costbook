# agent-costbook

Local service for traceable AI model pricing and per-task API cost estimates.
Version 0.1 stores evidence, research Markdown, and structured rates in SQLite,
then calculates M0 display and M4 task cost. M1, M2, M3, M5, M6, and M7 respond
with `unsupported_method`.

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
An unknown multiplier is not filled with 1. Request field `private_rates` changes only that
estimate and is not written to the catalog.

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
