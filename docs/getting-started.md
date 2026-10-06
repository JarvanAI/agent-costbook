# Getting Started with agent-costbook

This guide introduces `agent-costbook` (`ac`), from a zero-configuration demo to running a local service, querying facts, and calculating task cost estimates.

`agent-costbook` is an independent, traceable ledger of AI model pricing, subscription terms, and capability facts. It was created to provide verifiable inputs for routers like `agent-router` (`ar`), but can be used standalone by any developer or AI agent framework.

---

## 1. Installation

`agent-costbook` requires Python 3.12 or newer. We recommend using [uv](https://docs.astral.sh/uv/) for installation and environment management.

### Tool installation (recommended)

Install `ac` as a standalone CLI tool directly from Git:

```sh
uv tool install git+https://github.com/JarvanAI/agent-costbook.git@v1.1.0
```

> [!NOTE]
> Git installation is pinned to the verified release tag `@v1.1.0`. PyPI distribution is pending official publication; `uv tool install agent-costbook` will become available after publish receipts are updated.

Both `ac` and the long-form alias `agent-costbook` are installed on PATH. Verify installation:

```sh
ac --version
```

### Development installation

To contribute code, documentation, or catalog batches from a source checkout:

```sh
git clone https://github.com/JarvanAI/agent-costbook.git
cd agent-costbook
uv sync --locked --extra dev
```

---

## 2. Quick demo (no configuration or service required)

Try a synthetic estimate directly from the command line. After installation, `ac demo` requires no local configuration, no running service, no database setup, and no API keys:

```sh
ac demo
```

*(You can also run the demo directly without installing `ac` via `uvx --from git+https://github.com/JarvanAI/agent-costbook.git@v1.1.0 ac demo`).*

The demo runs completely offline:
- Uses bundled synthetic resources inside the package.
- Makes no network requests.
- Writes nothing to disk or database.
- Requires no tokens or configuration.

Expected output (result excerpt):

```json
{
  "publisher_id": "pub_sample",
  "synthetic": true,
  "results": [
    {
      "candidate_id": "synthetic-m4",
      "status": "ok",
      "method": "M4",
      "metrics": {
        "cost": "0.0177",
        "currency": "USD"
      }
    }
  ]
}
```

---

## 3. Setting up a local service

To query live capabilities and run online estimates, start a local HTTP service on loopback (`127.0.0.1:8080`).

### Quick start (single command)

Run `serve` with `--setup` to create configuration and start the server in one step:

```sh
ac serve --setup
```

This generates random authentication tokens, initializes an empty SQLite database, and starts the server in the foreground.

### Step-by-step setup

Alternatively, run the setup, start, and diagnosis steps individually:

1. **Initialize configuration and database**:
   ```sh
   ac setup
   ```
   - Creates a local configuration file (Linux default: `~/.config/agent-costbook/config.json`; other operating systems are unverified, specify explicit `ACB_CONFIG` or platform directory) with restricted permissions (`0600`). Missing directories are created with mode `0700` (`rwx------`); existing parent directories preserve their original mode.
   - Generates random `admin_token` and `read_token`.
   - Initializes an empty SQLite database (file permissions `0600`).
   - **Idempotent**: Re-running `ac setup` retains existing configuration, tokens, and database records. It never rotates existing tokens.

2. **Start the service**:
   ```sh
   ac serve
   ```
   Runs the loopback HTTP service in the foreground. Press `Ctrl-C` to stop.

3. **Check status and connectivity**:
   ```sh
   ac doctor
   ```
   Outputs a machine-readable JSON health report inspecting configuration existence, database status, connection to the server, credential validity, and catalog coverage. Doctor is strictly read-only and does not modify configuration, databases, or schemas. Tokens are never exposed in output.
   - **Local coverage vs. service coverage**: Local SQLite catalog counts are reported in `coverage` (`prices`, `agents`, `model_efforts`, `empty`). Verified HTTP counts probed against the running server are reported separately in `service_coverage`; `service_coverage` is `null` when the service is unreachable or down, and partial (e.g. only `prices`) when capabilities endpoints are unauthorized.
   - **Credential verification**: Generated `admin_token` and `read_token` must both verify against the service for doctor success (`status: "ok"`). Doctor status does not silently pass if the read token is wrong (it reports `status: "auth"` with exit code 7).

---

## 4. Querying facts and running estimates

Once the service is running, query facts using the CLI:

### Query prices

```sh
# Query all prices
ac query prices

# Filter by provider and model
ac query prices --provider openai --model openai/gpt-4o-mini

# Display as a table
ac query prices --format table
```

### Query capabilities

```sh
# Query recorded Agent capabilities
ac query agents --agent-id agent-1

# Query model-effort tiers and benchmark scores
ac query model-efforts --provider openai --model openai/gpt-4o-mini
```

### Online cost estimation

Run task cost estimation against the running service. Create a local `request.json` file:

```json
{
  "method": "M4",
  "currency": "USD",
  "usage": {
    "uncached_input": "1000",
    "cache_read": "2000",
    "cache_write": "500",
    "billed_output": "400"
  },
  "extra_cost": "0.01",
  "candidates": [
    {
      "candidate_id": "synthetic-m4",
      "provider": "example",
      "channel": "api",
      "model": "synthetic-m4",
      "plan": "payg",
      "feature_scope": "text"
    }
  ]
}
```

Then evaluate the estimate (`ac estimate` always outputs JSON; `--format` is not supported):

```sh
ac estimate --server http://127.0.0.1:8080 --request request.json
```

> [!NOTE]
> **Empty database vs. standalone demo**: While `ac demo` is completely independent and evaluates bundled in-memory synthetic fixtures without needing a database or server, online `ac estimate` queries the local service's SQLite catalog. On a newly initialized database (immediately after `ac setup` or `ac serve --setup` before any catalog import), the database is empty. Running this request against an empty database honestly returns `missing_data` (with `missing_fields: ["record"]` and no calculated rates):
>
> ```json
> {
>   "publisher_id": "pub_<generated>",
>   "data_version": null,
>   "snapshot_id": null,
>   "published_at": null,
>   "formula_version": null,
>   "freshness": null,
>   "content_sha256": null,
>   "results": [
>     {
>       "candidate_id": "synthetic-m4",
>       "status": "missing_data",
>       "metrics": null,
>       "units": null,
>       "method": "M4",
>       "formula_version": null,
>       "snapshot_id": null,
>       "sources": [],
>       "assumptions": [],
>       "missing_fields": [
>         "record"
>       ],
>       "freshness": null,
>       "record_snapshot_id": null,
>       "publisher_id": "pub_<generated>",
>       "data_version": null,
>       "capabilities": {
>         "agent": null,
>         "model_effort": null,
>         "status": "ok"
>       }
>     }
>   ]
> }
> ```
>
> In this empty-database response:
> - `publisher_id` is not `null`: `ac setup` generates and inserts a persistent identifier (`pub_<generated>`) upon initial database creation.
> - `capabilities.status` is `"ok"` when evaluated with the configured read token (which `ac estimate` automatically resolves from config). This indicates successful read authentication; `agent` and `model_effort` remain `null` because no capability records exist yet. If called without any valid read or admin token, `capabilities.status` returns `"not_authorized"`.
>
> To obtain a calculated cost (`status: "ok"`), the service must have matching records in its database—either by importing synthetic fixtures (see [Catalog Maintenance](guides/catalog-maintenance.md)) or by populating real provider pricing and capability facts.

---

## 5. Configuration and authentication

The configuration file structure (`version=1`):

```json
{
  "version": 1,
  "db_path": "<database_file_path>",
  "admin_token": "<random_admin_token>",
  "read_token": "<random_read_token>",
  "server": "http://127.0.0.1:8080"
}
```

- **Configuration path**: Overridden via `--config PATH` or environment variable `ACB_CONFIG` (Linux default: `~/.config/agent-costbook/config.json`; other platforms are unverified and should set explicit paths).
- **Database path**: Overridden via `--db PATH` or environment variable `ACB_DB`. The exact default path depends on platform application directories; use a generic placeholder or explicit `--db` rather than assuming an unverified filename.
- **Server URL**: Configured in `server` (defaults to `http://127.0.0.1:8080`). HTTP targets are restricted to loopback hosts (`127.0.0.1`, `localhost`, `::1`); HTTPS targets can be remote. Note that `ac serve` binds only loopback hosts regardless of the query target.
- **Environment variable `ACB_SERVER`**: Sets the fallback query target for `ac query` commands when `--server` is omitted. `ac serve` uses `--host` and `--port` CLI options, and online `ac estimate` requires an explicit `--server` argument.
- **Credentials**: Can be overridden via `ACB_ADMIN_TOKEN` and `ACB_READ_TOKEN`.
- **Read token permissions**: `read_token` allows read-only access to capabilities and M0–M6 estimates. It cannot modify data or read M7 private measurements.
- Tokens should never be passed as CLI arguments or logged.

---

## Next steps

- Integrate `agent-costbook` into your agent or router: see the [Agent integration guide](guides/agent-integration.md).
- Initialize and populate the catalog with real pricing and capability facts: see the [Catalog maintenance guide](guides/catalog-maintenance.md).
- Full command and API documentation: see the [Service and CLI reference](reference.md).
