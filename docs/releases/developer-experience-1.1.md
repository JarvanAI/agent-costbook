# Developer Experience Release 1.1.0

> Status: [GitHub v1.1.0 released](https://github.com/JarvanAI/agent-costbook/releases/tag/v1.1.0) on 2026-10-06. PyPI publication is pending maintainer Trusted Publisher configuration.

Version 1.1.0 focuses on developer experience, interactive exploration, and external agent integration for `agent-costbook` (`ac`), while maintaining complete backward compatibility with v1.0 data schemas and API endpoints.

---

## Highlights

### 1. Synthetic demo (`ac demo`)
Run a synthetic estimation directly after installation without local configuration, a running service, creating a database, or contacting the network:
```sh
ac demo
```
Evaluates a bundled synthetic test fixture, outputting an M4 estimate of `0.0177 USD` with `synthetic: true` (result excerpt). Alternatively, run via `uvx --from git+https://github.com/JarvanAI/agent-costbook.git@v1.1.0 ac demo`.

### 2. Streamlined setup and diagnostics (`setup`, `serve`, `doctor`)
- **`ac setup`**: Generates a standard configuration file (Linux default: `~/.config/agent-costbook/config.json`; other platforms unverified) with random admin and read tokens, and initializes an empty SQLite database. Re-running `setup` is idempotent and preserves existing data; setup never rotates existing tokens.
- **`ac serve --setup`**: Single command to generate configuration and start the local loopback service on `http://127.0.0.1:8080` in the foreground. `ac serve` binds only loopback hosts (`127.0.0.1`, `localhost`, `::1`).
- **`ac doctor`**: Outputs machine-readable JSON diagnosing server connectivity, credentials, and catalog coverage without exposing secret tokens.
  - Distinguishes local SQLite record counts in `coverage` from verified HTTP counts in `service_coverage` (`service_coverage` is `null` when the service is unreachable or down, and partial when capabilities endpoints are unauthorized).
  - Verifies that both generated `admin_token` and `read_token` authenticate successfully against the running service for doctor success (`status: "ok"`). Status does not silently pass a wrong read token (reports `status: "auth"`).

### 3. Consumption query CLI (`ac query`)
Query prices, recorded Agent capabilities, model-effort tiers, and provenance evidence directly from the command line:
```sh
ac query prices --provider openai --model openai/gpt-4o-mini
ac query agents --agent-id agent-1
ac query model-efforts --provider openai --model openai/gpt-4o-mini
ac query evidence --id EVIDENCE_ID
ac query research --id RESEARCH_ID
```
Outputs structured JSON by default, with optional `--format table`. Falls back to `ACB_SERVER`, then `config.server`, then `http://127.0.0.1:8080` when `--server` is omitted.

### 4. Read-only token permissions
Introduces `ACB_READ_TOKEN` (configured in `config.json` or environment). Read tokens allow:
- `GET /v1/capabilities/*` (agents and model-effort tiers).
- Capability-enriched estimates in `POST /v1/estimates` (methods M0–M6).
- Rejects catalog mutations (`POST /v1/contributions`, `PUT /v1/capabilities/*`) and private task observations (M7).

### 5. Online estimation CLI (`ac estimate --server`)
`ac estimate` supports online estimation against a running service in addition to offline snapshot evaluation:
```sh
ac estimate --server http://127.0.0.1:8080 --request request.json
```
The `--server` and `--snapshot` options are mutually exclusive. Online estimation requires an explicit `--server` argument.

### 6. Command alias and version flag
- Long-form alias `agent-costbook` is registered alongside `ac`.
- `ac --version` reports the current package version.

### 7. Read-only query Skill (`skills/costbook-query/`)
A standard, self-contained Codex/Cursor skill for querying prices and capabilities:
```sh
npx skills add JarvanAI/agent-costbook --skill costbook-query --agent codex --global
```
Three Skills are discovered across the repository (`skills/costbook-query/`, `skills/costbook-contribute/`, `skills/costbook-initialize/`). Actual query Skill evaluation is running separately in background.

### 8. New documentation and issue templates
- [Getting started](../getting-started.md): Beginner onboarding from installation to query.
- [Agent integration guide](../guides/agent-integration.md): Integration patterns for `agent-router` and autonomous frameworks.
- [Catalog maintenance guide](../guides/catalog-maintenance.md): Complete batch update and backup workflow.
- Structured GitHub issue templates for first-run failures and data corrections.

---

## Backward compatibility

- **Storage & APIs**: Database schema 2 and API v1 contracts are unchanged.
- **Formulas**: `ac-formulas-v2` and `ac-formulas-v1` remain supported with identical calculation behavior.
- **Existing workflows**: Legacy uvicorn factory invocation, offline `--snapshot` estimates, and `ac data` batch workflows remain fully supported.

---

## Validation

The source tree has been verified:

- **Source test suite**: 223 passed with one existing Starlette deprecation warning (`uv run pytest -q`).
- **Package build & installation**: Wheel build (`uv build`) and installation outside the checkout directory succeeded.
- **Skill discovery**: Three Skills discovered (`skills/costbook-query/`, `skills/costbook-contribute/`, `skills/costbook-initialize/`). Actual query Skill evaluation is running separately in background (not claimed as complete yet).
- **Documentation links**: Validated with `python scripts/check-docs.py`.
- **Demo validation**: Tested with `python scripts/check-demo.py`.

---

## Release preparation & PyPI Trusted Publishing

GitHub source release and its wheel/sdist are published. PyPI is a separate channel and remains pending. Pinned Git installation is available:
```sh
uv tool install git+https://github.com/JarvanAI/agent-costbook.git@v1.1.0
```
Never claim `uv tool install agent-costbook` works before a publish receipt is available.

The GitHub repository environment `pypi` has been configured. To configure PyPI Trusted Publishing for the initial package release:

1. Open PyPI publishing management:
   https://pypi.org/manage/account/publishing/
   (Official guide: https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
2. Under **"Add a pending publisher"**, enter the following exact fields:
   - **PyPI Project Name**: `agent-costbook`
   - **Owner**: `JarvanAI`
   - **Repository name**: `agent-costbook`
   - **Workflow name**: `publish.yml`
   - **Environment name**: `pypi`
3. After saving the pending publisher on PyPI, trigger the `.github/workflows/publish.yml` workflow via GitHub Actions `workflow_dispatch` on the release tag with `publish_pypi=true`. Parent will update release receipts once publication completes.

## Publication receipts

- Release tag `v1.1.0` resolves to `18366457e02f51c9f66efde51ad0932ca216ff0f`.
- [Main CI](https://github.com/JarvanAI/agent-costbook/actions/runs/37411428031), [tag CI](https://github.com/JarvanAI/agent-costbook/actions/runs/37411427877), and [Publish workflow](https://github.com/JarvanAI/agent-costbook/actions/runs/37411427902) succeeded for that commit. These checks ran on Linux/Python 3.12.
- [GitHub Release](https://github.com/JarvanAI/agent-costbook/releases/tag/v1.1.0) is public, not a draft or prerelease, with `agent_costbook-1.1.0-py3-none-any.whl` and `agent_costbook-1.1.0.tar.gz`.
- The documented remote `uvx --from git+https://github.com/JarvanAI/agent-costbook.git@v1.1.0 ac demo` was executed successfully. Remote skills CLI discovery found all three Skills.
- [Local and Agent acceptance](../research/developer-experience-validation-20261006.md): 223 passing tests, installation outside the checkout, and read-only Skill consumption against synthetic data.
- PyPI job was skipped on tag push as designed. It needs the maintainer's pending publisher before an explicit tagged workflow dispatch with `publish_pypi=true`; no password or API token should be sent to an Agent.
