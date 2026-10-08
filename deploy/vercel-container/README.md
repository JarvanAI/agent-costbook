# Vercel Container Images (Optional Template)

This directory contains an optional container deployment template for Vercel Container Images (Beta).

## Why This File Is Kept in `deploy/vercel-container/`

Vercel automatically detects a root-level `Dockerfile.vercel` and routes builds through Vercel Container Registry (VCR). According to official Vercel documentation ([VCR Limits and Pricing](https://vercel.com/docs/container-registry/limits-and-pricing)), VCR storage is billed at **$0.10 / GB**. Because VCR is not listed in the Hobby plan's free included usage quotas, having a root `Dockerfile.vercel` risks incurring unexpected fees or requiring a paid plan.

The default deployment method for `agent-costbook` is **native FastAPI / Python Serverless Functions** via root `app.py` and `vercel.json`, which runs completely within Hobby personal/non-commercial free limits.

## How to Use This Template (If Container Deployment is Desired)

1. Verify that your Vercel account and billing settings allow container images without unexpected charges.
2. Copy `deploy/vercel-container/Dockerfile.vercel` to the repository root as `Dockerfile.vercel` (or configure Vercel Project Settings to point to this path).
3. **Port Configuration**:
   - Vercel Container Images expect the container to listen on **port 80** by default.
   - If you want the container to listen on another port (such as `8080`), you **must** configure the `PORT` environment variable to `8080` in your Vercel Project Settings (`Environment Variables`).
   - The provided `Dockerfile.vercel` defaults `PORT=80` and dynamically uses `${PORT:-80}`.
4. Scale-down behavior: Vercel containers spin down after 5 minutes of idle time. Cold-start initialization reads the pre-packaged immutable SQLite database directly from the image without external cloud database dependencies.
