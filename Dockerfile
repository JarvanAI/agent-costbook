# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Pin uv 0.10.9 (matching host version) for deterministic dependency resolution from uv.lock
COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /uvx /bin/

# Create unprivileged non-root user and group
RUN groupadd -r -g 10001 costbook && \
    useradd -r -u 10001 -g costbook -s /sbin/nologin -d /app costbook

WORKDIR /app

# Explicitly copy packaging definition, lockfile, and source tree.
# Do not copy whole worktree, .git, .env, or private data.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ ./src/

# Install locked dependencies into virtual environment without upgrades or editable links
RUN uv sync --locked --no-dev --no-editable

# Ensure virtual environment and public data files (SQLite 644/444, searchable dirs)
# are fully readable by unprivileged user (UID 10001)
RUN chmod -R a+rX /app

# Switch to unprivileged non-root user
USER costbook

# Expose default port
EXPOSE 8080

# Environment variables: place virtual environment on PATH
ENV PATH="/app/.venv/bin:$PATH" \
    PORT=8080 \
    PYTHONUNBUFFERED=1

# Healthcheck testing the public /health endpoint for process liveness
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c 'import os, urllib.request; p = os.environ.get("PORT", "8080"); urllib.request.urlopen(f"http://127.0.0.1:{p}/health", timeout=3)' || exit 1

# Shell entrypoint expands $PORT and execs uvicorn with the exact public factory
# to receive SIGTERM directly. Access logging is disabled to prevent leaking data.
ENTRYPOINT ["sh", "-c", "exec uvicorn --factory agent_costbook.public_api:create_public_app --host 0.0.0.0 --port ${PORT:-8080} --no-access-log"]
