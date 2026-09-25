# agent-costbook

Local service for traceable AI model pricing, subscription amortization, and
per-task API cost estimates. Version 0.4 stores evidence, research Markdown,
API rates, subscription inputs, and explicit task-result imports in SQLite.
It calculates M0 display, M1 quota price, M2 task amortization, M3
capability-adjusted amortization, M4 task cost, M5 capability-adjusted task
cost, M6 budget amortization, and M7 measured cost per successful task.
New publications use formula set `ac-formulas-v2`, which calculates M0–M7.
A file or stored snapshot whose formula set is `ac-formulas-v1` still
calculates only M0, M1, M2, M4, and M6, and its bytes are not rewritten.
M3, M5, and M7 on that old set return `unsupported_method`. Any other formula
set is refused as `unsupported_formula`. M0–M6 estimates stay public. M7
reads stored measurements and requires the same admin token as a write.

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

M3 is M2 divided by an optional capability weight `w`. M5 is M4's task cost
divided by the same `w`. Omitting `w` uses 1 and is labeled
`capability_proxy_disabled`. A supplied `w` is a linear index proxy, not a
measured success rate, and the result says `do_not_reweight_for_routing` so a
router does not apply that weight again. M7 is imported attributed cash
divided by the number of distinct tasks that finally succeeded. Failed and
retried attempts stay in the cash total and do not add another success. A
period subscription amount is added once. API-equivalent amounts are reported
separately and are not added to cash. Zero successes with positive cash is
`unbounded`; zero successes with zero cash is `insufficient_data`. Neither
status emits NaN or Infinity. `POST /v1/observations` uses the same admin
token as contributions. Those rows are not part of `GET /v1/catalog` or the
export file. An estimate names `task_category`, `acceptance`, and the
candidate window; a missing sample stays `missing_data`. `POST /v1/estimates`
with method M7 requires `Authorization: Bearer $ACB_ADMIN_TOKEN` and returns
the import's `observation_id`. The price snapshot id remains the catalog
publication, not the measurement version. `period_start` and `period_end`
must both include a timezone or both omit it; a mix is HTTP 422.

`ac estimate` reads one published snapshot file, checks its kind, schema,
publisher, `snap-N` version, record hash, and formula set, then uses the same
calculation core as `POST /v1/estimates`. It does not rebuild a database.
M7 measurements are not in the public snapshot, so offline M7 stays
`missing_data` unless the service that holds the import answers the request.
Publications written by this version include `scope.task_profile` and
`scope.baseline_group` so a reference comparison can be repeated offline.
Older snapshot files keep their original bytes and do not gain those keys on
upgrade. For those files, candidate metrics are still calculated, and a
reference comparison returns `comparison: unavailable` rather than a rank.
`read_view` freshness is per record and uses each source `retrieved_at`.
`stale` stays null unless both that timestamp and `--now` are supplied, and it
is not written back into the snapshot. Republishing an old source under a new
`published_at` does not make the price fresh. A request `snapshot_id` must
equal the file's `snap-N`; omit it only when the file you passed is the
version you mean. A record whose `status` is `conflict`, or whose amount
currency or unit disagrees with its scope, is not a usable price.
`record_snapshot_id` is the service's internal id; the file carries
`snapshot_id` (`snap-N`) and offline results leave the internal id empty.

`ac collect --once` runs one due tick and then exits. Cron can call it every
minute; SQLite keeps `next_run_at` and the retry count, so an early invocation
does not fetch again. `ac collect --max-runtime 3600` is a bounded loop that
stops at the deadline or on SIGTERM. It is not a general crawler and it does
not fetch HTML or forums. The only scheduled sources are the OpenRouter model
catalog and the fixed `openai/gpt-4o-mini` endpoints JSON. Each endpoint tag,
including a different region, is its own channel. Two prices for the same tag
are published as a new snapshot whose record `status` is `conflict` and whose
`conflicts` summary lists the variants; older snapshot bytes stay as they were.
Unknown prices are omitted, not stored as 0. The service does not run a
research agent.

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
