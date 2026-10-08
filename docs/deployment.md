# Deployment Guide: agent-costbook Public Reference Service

This guide covers building, running, deploying, updating, backing up, and rolling back the public reference service for `agent-costbook`.

---

## 1. Architectural Principles & Boundaries

The public reference service is designed for external agents (such as `agent-router`) to consume public reference model costs, capabilities, and deterministic estimations over standard HTTP:

* **Anonymous & Read-Only**: All public endpoints (`/health`, `/ready`, `/v1/info`, `/v1/catalog`, `/v1/capabilities`, `POST /v1/estimates`) are unauthenticated. Write endpoints (`/v1/observations`, `/v1/contributions`), private pricing overrides, and measurement method `M7` are strictly disabled (`HTTP 403 Forbidden`).
* **Pre-Packaged Immutable Data**: Source facts are maintained in `src/agent_costbook/public_catalog/source.json`. Catalog pricing and capability data are compiled into an immutable SQLite database (`src/agent_costbook/public_catalog/catalog.sqlite3`) using strict compilation flags (`journal_mode=DELETE`, schema version 2, zero WAL/SHM artifacts) and validated against `src/agent_costbook/public_catalog/manifest.json` (`content_sha256` hash and publisher metadata). Data is **never generated or mutated at runtime or container startup**.
* **Zero Model Keys & No AI Execution**: The service contains no AI agent execution loops, requires no LLM API keys, and never persists request bodies or credentials.
* **Instance-Scoped Rate Limiting & Protections**: Enforces a strict 64 KiB raw request body limit, a maximum of 64 candidates per estimation batch, and an in-memory limit of 60 requests per 60 seconds per instance.
* **Log Privacy**: Access logging of query strings, request bodies, and Authorization headers is disabled at the server level (`--no-access-log`).

---

## 2. Local Docker Deployment (Self-Hosted OCI)

### Prerequisites
* Docker 20.10+ (tested on Docker Engine 29.7.2).
* Pinned dependency tool: `uv 0.10.9` (packaged via multi-stage binary copy).
* Read-only container root support.

### Building the Container
From the repository root:
```bash
docker build -t agent-costbook:latest .
```
The Docker build context excludes all internal docs (`.docs`), worktrees (`.worktree`), harness state (`hgit`), generic/user SQLite files, and private keys via `.dockerignore`, allowing only the exact public database `src/agent_costbook/public_catalog/catalog.sqlite3`.

### Running the Container
The container runs as an unprivileged user (`costbook`, UID 10001) with a read-only root filesystem and an in-memory temporary scratch space:
```bash
docker run -d \
  --name agent-costbook-public \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  -p 8080:8080 \
  -e PORT=8080 \
  agent-costbook:latest
```

### Health and Readiness Checks
* **Process Liveness (`/health`)**: Confirms the uvicorn process and FastAPI application are running:
  ```bash
  curl -s -f http://localhost:8080/health
  # {"status": "ok"}
  ```
* **Database & Catalog Readiness (`/ready`)**: Verifies that the packaged catalog SQLite database and manifest are intact, schema version is valid, and published records exist:
  ```bash
  curl -s -f http://localhost:8080/ready
  # {"status": "ready"}
  ```
  *(Invalid or unreviewed artifacts fail startup. A running service returns HTTP 503 `not_ready` if it has no current published price snapshot.)*
* **Service Descriptor (`/v1/info`)**:
  ```bash
  curl -s http://localhost:8080/v1/info
  ```

### Running with Docker Compose
A self-hosting `compose.yaml` is provided:
```bash
# Start the service
docker compose up -d

# Check status and health
docker compose ps

# View logs (sanitized, no request body or secret leaks)
docker compose logs

# Stop the service
docker compose down
```

*Note: The public container binds to `0.0.0.0:8080`, leaving the separate CLI tool's `ac serve` local loopback constraint (`127.0.0.1`) untouched.*

---

## 3. Native Vercel Deployment (Default Free Adapter)

The primary zero-cost public hosting option is **Vercel Native Python Serverless Functions** using the official FastAPI framework contract.

### Primary Source Citations
* [Vercel FastAPI Documentation](https://vercel.com/docs/frameworks/backend/fastapi) (Native ASGI entrypoint and framework detection).
* [Vercel Python Runtime Documentation](https://vercel.com/docs/functions/runtimes/python) (Runtime execution, `excludeFiles` bundling control).
* [Vercel Hobby Plan Limits](https://vercel.com/docs/plans/hobby) (Personal / non-commercial tier: 4 CPU-hours active, 360 GB-hours memory, 300s timeout).
* [Vercel Deployment Protection](https://vercel.com/docs/security/deployment-protection) (Public anonymous access settings).

### Configuration Details
1. **Entrypoint (`app.py`)**: Directly imports and instantiates the application:
   ```python
   from agent_costbook.public_api import create_public_app

   app = create_public_app()
   ```
2. **Framework Declaration (`vercel.json`)**:
   ```json
   {
     "$schema": "https://openapi.vercel.sh/vercel.json",
     "framework": "fastapi",
     "functions": {
       "app.py": {
         "excludeFiles": "{tests/**,docs/**,deploy/**,skills/**,examples/**,scripts/**,data/**}"
       }
     }
   }
   ```
   *Note: Sets `"framework": "fastapi"` matching the official schema enum and bundles the application as a single serverless function, excluding non-runtime folders while preserving `src/agent_costbook/public_catalog/`.*

### Step-by-Step Deployment Steps
1. Push the repository to GitHub.
2. Link the repository in the Vercel Dashboard (**Add New Project**).
3. **Framework Preset**: Auto-detected as **FastAPI** (declared in `vercel.json`).
4. **Environment Variables**: Leave blank (zero secret tokens or cloud database dependencies).
5. **Deployment Protection Check**:
   * Navigate to **Settings -> Deployment Protection**.
   * Ensure that **All Deployments** is **disabled** so the production domain allows anonymous unauthenticated requests.
6. Click **Deploy**.

---

## 4. Optional Vercel Container Deployment

If containerized execution is explicitly desired on Vercel:

* An optional template is provided in `deploy/vercel-container/Dockerfile.vercel`.
* **Cost Caution**: Vercel Container Registry (VCR) bills storage at **$0.10 / GB** ([VCR Limits and Pricing](https://vercel.com/docs/container-registry/limits-and-pricing)). VCR is not confirmed as free ($0 unknown) in the Hobby plan included quotas. Do not place `Dockerfile.vercel` at the repository root unless container storage fees are acceptable for your account.
* **Port Ingress**: Vercel Container Images default to port **80**. If configuring the container to run on port 8080, set the `PORT=8080` environment variable in Vercel Project Settings.

---

## 5. Catalog Lifecycle: Maintenance, Update, Backup & Rollback

### Data Packaging Architecture
The public database is compiled into:
* `src/agent_costbook/public_catalog/source.json`: Raw reviewed source facts, evidence URLs, and retrieval timestamps.
* `src/agent_costbook/public_catalog/catalog.sqlite3`: Compiled SQLite database with `journal_mode=DELETE`, schema version 2, no WAL/SHM files.
* `src/agent_costbook/public_catalog/manifest.json`: File SHA256 (`database_sha256`), publisher ID, price data version, and coverage metadata.

### Updating the Catalog
1. Edit or append verified public source facts in `src/agent_costbook/public_catalog/source.json`.
2. Compile and validate the artifact using the catalog build script:
   ```bash
   uv run --locked python scripts/build-public-catalog.py \
     --source src/agent_costbook/public_catalog/source.json \
     --update-from src/agent_costbook/public_catalog/catalog.sqlite3 \
     --output /tmp/ac-public-next/catalog.sqlite3 \
     --manifest /tmp/ac-public-next/manifest.json
   uv run --locked python -c 'from agent_costbook.public_data import validate_public_artifact; validate_public_artifact("/tmp/ac-public-next/catalog.sqlite3", "/tmp/ac-public-next/manifest.json")' 
   ```
3. Review the source diff, coverage, exclusions and resulting publisher/versions. The output directory must be new: the builder refuses to overwrite published files. After approval, replace the package artifact with the checked output and commit the updated `catalog.sqlite3`, `manifest.json`, and `source.json`.
4. Run repository checks and the real HTTP smoke, then publish a new image/deployment (for example `v1.2.1`). Keep the old image/deployment and artifact for rollback. Updating prices preserves publisher and history; capability-only changes preserve price version and change the capability hash.

### Backup
* All source facts JSON files and compiled SQLite release artifacts are version-controlled in Git.
* Published Docker images in GHCR are identified by version tags and immutable image digests (e.g. `ghcr.io/jarvanai/agent-costbook:1.2.0`).

### Rollback
* **Docker / Self-Hosted**: Re-run the container pointing to the previous release tag:
  ```bash
  docker run -d --read-only --tmpfs /tmp -p 8080:8080 ghcr.io/jarvanai/agent-costbook:1.2.0
  ```
  The example above applies after a later update: 1.2.0 is the first Docker release, and there is no 1.1.0 container to roll back to. Prefer a verified digest when pinning an image.
* **Vercel**: In the Vercel Dashboard, go to the **Deployments** tab, find the previous successful deployment, and click **Instant Rollback**.

---

## 6. Hosting Alternatives Comparison

| Platform | Tier / Pricing | Architecture | Suitability |
| :--- | :--- | :--- | :--- |
| **Vercel (Native Python)** | Free (Hobby: personal/non-commercial) | Serverless Function (stateless) | **Primary Target**: zero-cost, native Python 3.12, bundled read-only SQLite. |
| **Vercel (Container Images)** | Beta; VCR $0.10/GB ($0 unknown on Hobby) | OCI Container | **Optional**: requires verifying VCR storage cost boundary. |
| **Cloudflare Containers** | Paid only ($5.00/mo min) | Container instance | **Excluded**: does not qualify for free public hosting. |
| **Cloudflare Workers (D1)** | Free | JS/Wasm + D1 SQLite | **Out of Scope**: requires rewriting Python ASGI engine and formulas. |
| **Render** | Free | Web Service (ephemeral disk) | **Fallback Only**: cold start latency; free databases expire in 30 days. |

---

## 7. Known Gaps & Unresolved Cloud Steps

* **Cloud Credentials**: No Vercel or Cloudflare tokens are configured in the current environment. Automated cloud deployment cannot execute without human account provisioning.
* **GHCR Package Visibility**: By default, GitHub Container Registry packages pushed from workflows are private. Public visibility cannot be claimed until receipt is verified by human administration: navigate to **GitHub -> Packages -> agent-costbook -> Package settings** and set visibility to **Public** to enable anonymous `docker pull`.
* **Public Service URL**: Currently **`null`** (no deployment claims made; unverified until an actual cloud deployment is provisioned by the account owner).

## Verification

Run `python3 scripts/check-public-service.py BASE_URL` against a fresh instance. The checker uses only standard-library HTTP and validates actual hashes, known cost, capability-with-price-gap, missing usage, ETag/304, private-input rejection, forbidden writes/M7 and request limits. [Recorded local fixtures](../examples/public-service/README.md) are real local responses, not cloud deployment evidence.
