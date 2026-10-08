# Free Hosting & Deployment Research (2026-10-08)

This document synthesizes primary official documentation for hosting the read-only public HTTP service of `agent-costbook` under zero added cost constraints, together with local environment audit findings.

---

## 1. Primary Platform Findings

### A. Vercel (Recommended Primary Target)

* **Native FastAPI / Python Serverless Functions (Default Contract)**:
  * **Hobby Plan**: Strictly personal, non-commercial use ([Vercel Hobby Plan](https://vercel.com/docs/plans/hobby)).
  * **Quotas**: Includes 4 Active CPU-hours and 360 GB-hours provisioned memory per billing cycle. Function execution limit is 300s (default 15s), memory up to 2,048 MB. Request body payload platform ceiling is 4.5 MB (application enforces strict 64 KiB ceiling before parsing).
  * **Architecture**: Stateless function execution. Bundling immutable data (source facts `src/agent_costbook/public_catalog/source.json`, compiled SQLite catalog `catalog.sqlite3`, and `manifest.json`) directly into the deployment bundle is officially supported ([Docker Compose Concepts on Vercel](https://vercel.com/kb/guide/docker-compose-concepts-on-vercel)). No external cloud database or write layer is required.
  * **Catalog Compilation Flags**: SQLite is pre-compiled with `journal_mode=DELETE`, `synchronous=FULL`, schema version 2, and zero WAL/SHM artifacts. Cryptographic hash `content_sha256` and version metadata are pinned in `manifest.json`.
  * **Official Deployment Contract**: Official FastAPI preset ([Vercel FastAPI Documentation](https://vercel.com/docs/frameworks/backend/fastapi)). Entrypoint is root `app.py` exporting `app = create_public_app()`. `vercel.json` explicitly declares `"framework": "fastapi"` matching the official schema enum and configures `functions["app.py"].excludeFiles` to omit non-runtime folders (`tests/**`, `docs/**`, `deploy/**`, `skills/**`, `examples/**`, `scripts/**`, `data/**`) without provisional rewrites hacks.

* **Container Images (Beta & Optional)**:
  * **Availability**: Beta on all plans ([Vercel Container Images](https://vercel.com/docs/functions/container-images)).
  * **Port Conventions**: Platform default ingress port is `80`. If container listens on `8080`, project setting `PORT=8080` must be explicitly configured.
  * **Lifecycle**: Scales down to zero after 5 minutes of inactivity; `SIGTERM` shutdown grace period is 30 seconds.
  * **VCR Storage Fee Boundary**: Vercel Container Registry (VCR) storage is billed at **$0.10 / GB** ([Container Registry Limits and Pricing](https://vercel.com/docs/container-registry/limits-and-pricing)). Because VCR is not confirmed as free ($0 unknown) in the Hobby quota table, a root `Dockerfile.vercel` must **not** be included; optional container templates are placed in `deploy/vercel-container/`.

* **Deployment Protection & Public Access**:
  * Standard Protection covers preview deployments while leaving production domains public. If "All Deployments" protection is enabled, production domain access requires SSO/login ([Vercel Deployment Protection](https://vercel.com/docs/security/deployment-protection)). Anonymous public reference HTTP access requires ensuring "All Deployments" is disabled for production.
  * CDN caching for Serverless Functions requires explicit `s-maxage` in `Cache-Control` headers ([Vercel CDN Cache](https://vercel.com/docs/caching/cdn-cache)). Current catalog views intentionally omit `s-maxage` to avoid pinning upcoming releases, while pinned `snap-N` snapshots may leverage immutable caching.

---

### B. Cloudflare (Containers & Workers)

* **Cloudflare Containers**: Free tier is not available (N/A); requires Workers Paid plan starting at **$5.00 / month** ([Cloudflare Containers Pricing](https://developers.cloudflare.com/containers/platform/pricing/)). Excluded from free tier deployment.
* **Cloudflare Workers + D1**: Requires rewriting Python ASGI and SQLite engine to JS/Wasm and D1 relational database. Not a first sufficient implementation; preserved as a future consideration.

---

### C. Render (Fallback Option)

* **Free Web Services**: Ephemeral filesystem only; disk storage is wiped on restart or new release ([Render Free Tier](https://render.com/docs/free)).
* **Persistence & Add-ons**: Free PostgreSQL instances expire after 30 days. No free persistent disks.
* **Limitations**: While immutable SQLite baked into container images is preserved across restarts, runtime writes cannot be durable. Free tier instances spin down on idle with cold start latency. Render serves only as an immutable read-only fallback.

---

## 2. Local Environment & Account Gap Audit

A local audit was conducted without inspecting sensitive files or reading auth tokens:

1. **Local CLI Tools**:
   * `docker`: Docker version 29.7.2 is available locally.
   * `uv`: Version 0.10.9 is installed and pinned in Dockerfile.
   * `vercel`: CLI is **not installed** on PATH.
   * `wrangler`: CLI is **not installed** on PATH.
2. **Authentication & Credentials**:
   * No Vercel authentication files (`~/.vercel`) or `VERCEL_TOKEN` environment variables are present.
   * No Cloudflare API tokens or configuration files are present.
3. **Cloud Resources & Packaging**:
   * Zero paid resources have been created.
   * Zero cloud deployments have been executed.
   * GHCR image packages push as private by default; public visibility cannot be claimed until receipt is verified by manual admin action.
4. **Deployed URL Status**:
   * Production deployment URL: **`null`** (unverified; pending human account provisioning and approval, no premature deployment claims made).
