# Service and CLI reference

[README](../README.md) · [中文首页](../README.zh-CN.md) · [Getting Started](getting-started.md) · [Agent Integration](guides/agent-integration.md) · [Catalog Maintenance](guides/catalog-maintenance.md)

Local service for traceable AI model pricing, subscription amortization, and
per-task API cost estimates. Version 1.0 freezes the price API and snapshot
`schema_version` 1. Database schema 2 adds a separate capability catalog.
It stores evidence, research Markdown,
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

Public design and research notes live in `docs/design/` and `docs/research/`.

---

## Public reference profile

The independent public factory is `agent_costbook.public_api:create_public_app`. It anonymously serves a reviewed immutable catalog, capability facts, and M0–M6 estimates. Its contract and private-field exclusions differ from the local administrator API below. See the [public specification](design/public-service-1.2/spec.md) and [deployment guide](deployment.md). No public URL has been deployed yet.

## Local commands and service lifecycle

### `ac demo`
Run an instant synthetic task cost estimate without local configuration, a running service, network access, tokens, or disk writes.
```sh
ac demo
```
Evaluates the bundled synthetic snapshot (`snapshot.json`, publisher `pub_sample`) and request (`request.json`, candidate `synthetic-m4`). Outputs the full estimation results envelope matching the standard evaluation schema (`publisher_id`, `data_version`, `formula_version`, `results: [...]`), with `synthetic: true` added to the top level, rather than a custom or trimmed result object. Returns an M4 estimate of `0.0177 USD`.

### `ac setup`
Generate standard configuration and initialize an empty SQLite database.
```sh
ac setup [--config PATH] [--db PATH]
```
- Creates a JSON configuration file (Linux default: `~/.config/agent-costbook/config.json`; other operating systems are unverified, specify explicit `--config` or `ACB_CONFIG`).
- **Permissions**: Missing directories are created with mode `0700` (`rwx------`); existing parent directories preserve their original mode. The configuration file and database file are created with mode `0600` (`rw-------`).
- Generates random, distinct `admin_token` and `read_token`.
- Creates an empty database at `db_path`.
- **Idempotent**: Re-running `ac setup` retains existing tokens, configuration, and database records. Setup never rotates existing tokens.
- Plaintext tokens are never printed to stdout.

### `ac serve`
Run the local loopback HTTP service in the foreground.
```sh
ac serve [--setup] [--config PATH] [--db PATH] [--port PORT]
```
- Listens on loopback hosts only (`127.0.0.1`, `localhost`, `::1`; default `127.0.0.1`, port 8080). `ac serve` binds only loopback even if client query configuration points to a remote HTTPS target.
- `--setup`: Convenience flag to create configuration and empty database if missing, then start the server immediately.
- Runs in the foreground; stop with `Ctrl-C`. No background daemon or automatic web crawling.
- When port conflict occurs, reports port occupancy and advises connecting to the existing instance or specifying `--port`.

### `ac doctor`
Inspect local configuration, database status, connection to the server, credential validity, and catalog coverage.
```sh
ac doctor [--config PATH] [--db PATH] [--server URL]
```
Outputs machine-readable JSON diagnosing service health. Never outputs credentials or collects account data. Doctor is strictly read-only: it does not modify the configuration, database, or schema. If the database schema is older than the current binary, doctor does not run migrations automatically; it reports the status and advises running migration or creating a new database via `ac setup`.
- **Local coverage vs. service coverage**: Local SQLite catalog counts are reported in `coverage` (`prices`, `agents`, `model_efforts`, `empty`) read directly from SQLite without network requests. Verified HTTP counts probed against the running server are reported separately in `service_coverage`; `service_coverage` is `null` when the service is unreachable or down, and partial (e.g. only `prices`) when capabilities endpoints are unauthorized.
- **Credential verification**: Generated `admin_token` and `read_token` must both verify against the service for doctor success (`status: "ok"`). Doctor status does not silently pass if the read token is wrong (it reports `status: "auth"` with exit code 7).

### Aliases and version
- `agent-costbook` is available as a long-form command alias for `ac`.
- `ac --version` reports the current package version.

---

## Query commands

All query commands default to JSON format. Table format is available via `--format table`.

```sh
# Query prices (supports provider and model filtering)
ac query prices [--server URL] [--config PATH] [--provider PROVIDER] [--model MODEL] [--format json|table]

# Query recorded Agent capabilities
ac query agents [--server URL] [--config PATH] [--agent-id AGENT_ID] [--format json|table]

# Query model-effort capabilities and benchmarks
# Omitting --effort returns all effort tiers for that model
# Passing --effort "" queries the unquantified / default tier (effort is null)
ac query model-efforts [--server URL] [--config PATH] [--provider PROVIDER] [--model MODEL] [--effort EFFORT] [--format json|table]

# Query provenance evidence by ID
ac query evidence --id EVIDENCE_ID [--server URL] [--config PATH] [--format json|table]

# Query research Markdown by ID
ac query research --id RESEARCH_ID [--server URL] [--config PATH] [--format json|table]
```

Query commands prioritize `read_token` (`ACB_READ_TOKEN`) and fall back to `admin_token`. Tokens must not be passed as CLI arguments.

---

## Cost estimation (`ac estimate`)

`ac estimate` calculates task cost estimates for candidate models. Output is always formatted as JSON; `--format` (`table`) is not supported on `ac estimate` and is exclusive to `ac query` subcommands.

```sh
# Online mode against a running service
ac estimate --server http://127.0.0.1:8080 --request request.json [--config PATH]

# Offline mode against a published snapshot file
ac estimate --snapshot snapshot.json --request request.json [--publisher "$PUBLISHER_ID"] [--now NOW] [--max-age-seconds SECONDS]
```

The `--server` and `--snapshot` options are mutually exclusive:
- `--request REQUEST`: Path to JSON request file (required).
- Online mode (`--server URL`): Evaluates against the running service database. Requires an explicit `--server URL` (online estimation does not fall back to `ACB_SERVER`). Accepts `--config` to resolve credentials and configuration. Offline parameters (`--publisher`, `--now`, `--max-age-seconds`) are rejected with an `offline_only` error.
- Offline mode (`--snapshot SNAPSHOT`): Evaluates against a frozen snapshot JSON file. `--publisher` is an optional verification pin; `--now` and `--max-age-seconds` are optional freshness controls. Does not read configuration or communicate over the network.

---

## Configuration and runtime contract

### Configuration schema and validation (`CliConfig`)

The local configuration file (`config.json`) is a version 1 JSON document validated by `CliConfig`:

```json
{
  "version": 1,
  "db_path": "/home/user/.local/share/agent-costbook/costbook.sqlite3",
  "admin_token": "<random_admin_token>",
  "read_token": "<random_read_token>",
  "server": "http://127.0.0.1:8080"
}
```

Validation invariants enforced by `CliConfig`:
- `version`: Must be integer `1`.
- `db_path`: Must be an absolute filesystem path string.
- `admin_token` and `read_token`: Must be non-empty strings (maximum 512 characters, no ASCII control characters `< 32`).
- **Distinct tokens**: `admin_token` and `read_token` MUST be distinct (`admin_token != read_token`).
- `server`: Must be a valid normalized URL string (HTTP is restricted to loopback hosts; HTTPS targets can be remote).
- Violations raise `config_invalid` (exit code 3) without echoing secret tokens in error messages.

### Permissions and paths

- **Directories**: Setup creates only missing directories with permissions `0700` (`rwx------`). Existing parent directories preserve their original mode.
- **Files**: Configuration file and database file are created or set to permissions `0600` (`rw-------`).
- **Paths**: Defaults follow Linux XDG conventions (`$XDG_CONFIG_HOME/agent-costbook/config.json` and `$XDG_DATA_HOME/agent-costbook/costbook.sqlite3`). Non-Linux platforms are unverified in this release; specify explicit `--config` / `ACB_CONFIG` or `--db` / `ACB_DB`.

### Precedence and overrides

- `--config PATH` or environment `ACB_CONFIG`: Specifies configuration file location.
- `--db PATH` or environment `ACB_DB`: Overrides `db_path` in configuration.
- `ACB_ADMIN_TOKEN` and `ACB_READ_TOKEN`: Override credentials in configuration.
- `ACB_SERVER`: Sets the fallback query target for `ac query` commands when `--server` is omitted. Does not change `ac serve` (which binds loopback only via `--host` and `--port`), and online `ac estimate` requires an explicit `--server` argument.

### Token permissions

- **Public access** (no token): `/health`, `/v1/catalog`, public estimates (capabilities return `not_authorized`), `/v1/evidence/{id}`, `/v1/research/{id}`.
- **Read token (`ACB_READ_TOKEN`)**: Allows reading capability records (`GET /v1/capabilities/*`) and requesting capability enrichment in estimates (methods M0–M6). Cannot modify data or read M7 private measurements.
- **Admin token (`ACB_ADMIN_TOKEN`)**: Full access, including catalog batch writes, capability updates (`PUT /v1/capabilities/*`), observations import, and M7 measurement estimates.

### Local CLI exit codes and error codes

Accurate codes extracted from `local.py`:

| Exit Code | Constant | Error Codes | Description |
| --- | --- | --- | --- |
| 1 | `EXIT_IO` | `io` | I/O failure creating or opening directories or files. Existing files left unchanged. |
| 2 | `EXIT_CONFIG_MISSING` | `config_missing`, `setup_busy` | Configuration file not found, or another `ac setup` process holds the lock. |
| 3 | `EXIT_CONFIG_INVALID` | `config_invalid`, `host`, `port` | Configuration is malformed, has invalid/equal tokens or relative paths, or invalid host/port. |
| 4 | `EXIT_DB_MISSING` | `db_missing` | Database file does not exist at configured path. |
| 5 | `EXIT_DB_INVALID` | `db_invalid` | Database could not be opened or is corrupted. |
| 6 | `EXIT_TRANSPORT` | `transport` | HTTP connection failure to local service. |
| 7 | `EXIT_AUTH` | `auth` | Token authentication failed against local service. |
| 8 | `EXIT_PORT` | `port_conflict` | Requested TCP port is already in use. |
| 9 | `EXIT_DB_MISMATCH` | `db_mismatch` | Specified `--db` conflicts with database path in existing `config.json`. |

---

## Legacy server factory and operator commands

To run the service directly via uvicorn factory:

```sh
uv sync --extra dev
cp .env.example .env
export ACB_DB=agent_costbook.sqlite3
export ACB_ADMIN_TOKEN=choose-a-local-token
uv run uvicorn agent_costbook.api:create_app --factory --host 127.0.0.1 --port 8080
```

Migration changes the open database in one transaction and rolls back on
failure. It refuses a schema newer than this release. An existing connection
keeps the same file and can continue writing after a successful migration.
Backup and restore refuse an existing destination and refuse to use the source
path as the destination. A failed copy leaves the source file unchanged.

```sh
uv run agent-costbook-migrate --db agent_costbook.sqlite3
uv run agent-costbook-backup --source agent_costbook.sqlite3 --destination agent_costbook.sqlite3.bak
uv run agent-costbook-backup --restore --source agent_costbook.sqlite3.bak --destination agent_costbook-restored.sqlite3
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

`ac data` plans, checks, diffs, applies, and verifies a local batch of price
and capability rows against the HTTP API above. It does not start the service,
does not delete rows, and does not add a benchmark or private-quota store.
`skills/costbook-initialize/SKILL.md` is the workflow. Contribution and
capability fields stay in `skills/costbook-contribute/SKILL.md`. An example
scope is `examples/data-scope.json`; `examples/data-batch/` is the batch that
`plan` writes from it.

```sh
uv run ac data plan --mode init --scope examples/data-scope.json --out examples/data-batch
uv run ac data validate --batch examples/data-batch
uv run ac data diff --batch examples/data-batch --server http://127.0.0.1:8080
uv run ac data apply --batch examples/data-batch --approved-diff-sha256 PLAN_SHA256 \
  --server http://127.0.0.1:8080 \
  --backup-source "/path/to/agent-costbook/costbook.sqlite3" --backup-destination examples/data-batch/backup.sqlite3
uv run ac data verify --receipt examples/data-batch/receipt.json --server http://127.0.0.1:8080
```

The token is resolved from the setup configuration or `ACB_ADMIN_TOKEN` in the environment. The server URL must be a normalized URL (HTTP is allowed only on loopback hosts; HTTPS targets can be remote), with no userinfo, and redirects are refused. `diff` prints the
sha256 of canonical `plan.json`; that file binds the server, publisher, batch
bytes, record identity, baseline version, and complete target. `plan.md` is
only for reading. Apply refuses a different hash, server, or publisher before
it writes. A row whose version moved and whose content differs is left as
`conflict`. The same business content writes nothing on a second apply.

A write needs a backup. `--backup-source` must be the actual database path initialized by `ac setup` or `ac doctor` (or the value of `$ACB_DB` if set). `--backup-source` and `--backup-destination` copy the
database with the existing backup helper and then set the new file to mode
`0600`; the directory must be private (`0700`). `agent-costbook-backup` itself is
unchanged and still creates mode `0644`. `--backup-receipt` accepts an
operator backup whose server, publisher, catalog version, content hash, and
file sha256 match the current service. The backup file must be private, and
its publisher, price snapshot, and capability rows must match that service. A
copy of a different or older database does not authorize writes. The fixed OpenRouter parser, when
given a local fixture, reads only `openai/gpt-4o-mini`. It does not fetch the
network. Coding-agent combinations, private quotas, and other unsupported
categories are refused instead of being stored as price rows. A lost publish
is settled from `GET /v1/evidence` for the contribution id saved in the
journal, or by publishing that same id again. There is no contribution-status
endpoint, and an unknown contribution is not replayed.

## Support

| Surface | Status |
| --- | --- |
| Platform / OS | Linux validated (XDG paths: `~/.config/agent-costbook/config.json`, `~/.local/share/agent-costbook/costbook.sqlite3`). Non-Linux platforms (macOS, Windows) have unverified path defaults; specify explicit `--config` / `ACB_CONFIG` and `--db` / `ACB_DB`. Full cross-platform support is not claimed. |
| Formula set `ac-formulas-v2` | M0, M1, M2, M3, M4, M5, M6, M7 |
| Formula set `ac-formulas-v1` | M0, M1, M2, M4, M6; M3, M5, and M7 return `unsupported_method` |
| Scheduled collectors | OpenRouter model catalog and the fixed `openai/gpt-4o-mini` endpoints JSON |
| Other providers | Any provider submitted through `POST /v1/contributions` |
| Storage | One local SQLite file |
| Snapshot document | `schema_version` 1, decimal strings, no automatic currency conversion |

## Security

Keep the process on `127.0.0.1`. Set `ACB_ADMIN_TOKEN` before any contribution,
publication, observation import, or M7 read. Request fields `private_rates`,
`marginal_cash`, and `private_subscription.monthly_price` apply only to that
estimate. They are not written to the catalog, the export, or a log.
Evidence and research Markdown are stored data, not instructions. Backup and
restore refuse to overwrite a file, including the database the service has open.

## Known limitations

Project code is licensed under MIT; third-party data retains its own terms.
See [Data sources and rights](data-sources.md). A local wheel is a build artifact,
not a package-registry release. agent-router can import a published snapshot
and call this service; a successful recommendation and automatic dispatch are
not verified in this repository. Live OpenRouter collection depends on the
network, and the offline tests use recorded fixtures. Freshness follows each
source `retrieved_at`. M7 reports sample size, successful tasks, and
`insufficient_data` or `unbounded`. This release does not add a savings-rate
or quality-score gate. There is no multi-tenant control plane and no
research-job endpoint. The capability catalog is not an AA crawler and is not
synced into offline snapshots.

## HTTP

- `GET /health`
- `GET /v1/catalog`
- `POST /v1/estimates`
- `GET /v1/evidence/{id}`
- `GET /v1/research/{id}` and `GET /v1/research/{id}?format=markdown`
- `POST /v1/contributions`
- `POST /v1/contributions/{id}/publish`
- `GET /v1/capabilities`
- `PUT /v1/capabilities/agents` and `GET /v1/capabilities/agents?agent_id=`
- `GET /v1/capabilities/agents/history?agent_id=`
- `PUT /v1/capabilities/model-efforts`
- `GET /v1/capabilities/model-efforts?provider=&model=&effort=`
- `GET /v1/capabilities/model-efforts/history?provider=&model=&effort=`

## Capability catalog

Capability rows are local observations about recorded Agent capabilities or one
model-effort tier. They are not price rows. Writing one does not change
`data_version`, `content_sha256`, formula version, or export bytes. Every
capability read and write uses `Authorization: Bearer $ACB_ADMIN_TOKEN` or `Authorization: Bearer $ACB_READ_TOKEN` (read-only endpoints).
A public estimate still prices the candidate, and its `capabilities` object
is `status: not_authorized` with null `agent` and `model_effort`. An
authorized estimate adds the current rows for the optional `agent_id` and the
exact provider, model, and effort. Missing data stays null. An empty effort
selects the real row whose effort is null; it does not fall back to `low`.
Offline `ac estimate --snapshot` reads only the price snapshot, so it returns
`status: unavailable` and cannot show this catalog. In contrast, online
`ac estimate --server` returns the same HTTP `capabilities` object as
`POST /v1/estimates`: a read token or admin token authorizes capability enrichment
for methods M0–M6 (`status: ok`), while method M7 requires the admin token.
Requests without a valid read or admin token return `capabilities.status: not_authorized`.
There is no offline sync and no AA crawler. `aa`, `official`, `community`, `unofficial`, and
`user_observation` are source labels. User observations must use
`user_observation` and may omit a URL. `source_ref` is an optional URL or
document citation stored as text; the service does not fetch or run it.
Unknown citations stay null. Do not invent a score for an unknown benchmark.

Model names may contain `/`. Pass `provider`, `model`, and `effort` as JSON
fields or query parameters, not as path segments. A write replaces the whole
row. `expected_version` is `0` for the first write and the current
`row_version` after that. A stale version or a second current sweet spot for
the same Agent, provider, and model returns 409 and changes nothing.
`default_for_agents` lists Agents that treat this tier as the default for
that model. It does not list every Agent that can call the tier. Remove the
id from the old row before adding it to another effort. Two Agents may share
one tier. Different models are independent. Omitted text, context length, and
benchmarks stay null. Text fields are at most 2000 characters, the default
list at most 32 ids, and benchmarks at most 16 complete `{name, score, unit,
source}` objects. A benchmark may also carry `source_ref`. `score` is a finite decimal string. `context_length` is a
positive integer when it is known. `as_of` must include a timezone. Stored
text is data, not a command.

`GET /v1/capabilities` lists the current Agent and model-effort rows for an
authorized token. It does not include history or price records. An empty catalog returns
two empty arrays.

> [!NOTE]
> Direct `curl` examples below retain token placeholders `<ACB_READ_TOKEN>` and `<ACB_ADMIN_TOKEN>`. In multi-user or shared environments, passing tokens in shell command-line arguments is not argv-safe (tokens can appear in process listings `ps`); programmatic integrations should load credentials directly from `config.json` without exposing them in argv. `ac` CLI does not provide token argument flags for capability mutations.

```sh
curl -sS "http://127.0.0.1:8080/v1/capabilities" \
  -H "Authorization: Bearer <ACB_READ_TOKEN>"
curl -sS -X PUT "http://127.0.0.1:8080/v1/capabilities/agents" \
  -H "Authorization: Bearer <ACB_ADMIN_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"agent-1","expected_version":0,"source":"user_observation","as_of":"2026-09-27T00:00:00+00:00","strengths":"edits files in this workspace","can_edit_files":true}'
curl -sS "http://127.0.0.1:8080/v1/capabilities/agents?agent_id=agent-1" \
  -H "Authorization: Bearer <ACB_READ_TOKEN>"
curl -sS -X PUT "http://127.0.0.1:8080/v1/capabilities/model-efforts" \
  -H "Authorization: Bearer <ACB_ADMIN_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"provider":"example","model":"synthetic-m4","effort":null,"expected_version":0,"source":"user_observation","as_of":"2026-09-27T00:00:00+00:00","benchmarks":null,"default_for_agents":["agent-1"]}'
curl -sS "http://127.0.0.1:8080/v1/capabilities/model-efforts?provider=example&model=synthetic-m4" \
  -H "Authorization: Bearer <ACB_READ_TOKEN>"
curl -sS "http://127.0.0.1:8080/v1/capabilities/model-efforts/history?provider=example&model=synthetic-m4&effort=low" \
  -H "Authorization: Bearer <ACB_READ_TOKEN>"
```

A snapshot is the whole price catalog as of that publication. Replacing an existing identity
requires `base_snapshot_id` of the reviewed record version. Estimates pin one catalog
revision for every candidate. Omitting usage does not mean the tokens were zero.
There is no research-job endpoint.

## Tests

```sh
uv sync --extra dev
uv run pytest -q
```

## Documentation

- [AGENTS.md](../AGENTS.md)
- [Getting started](getting-started.md)
- [Agent integration](guides/agent-integration.md)
- [Catalog maintenance](guides/catalog-maintenance.md)
- [Data sources and rights](data-sources.md)
- [Data acquisition research](research/aa-data-acquisition.md)
- [Storage findings research](research/aa-storage-findings.md)
