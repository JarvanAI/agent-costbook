"""Vercel / ASGI serverless entrypoint for agent-costbook public service.

Exports the read-only public ASGI application using the native FastAPI
runtime without requiring external databases or container registries.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure 'src' layout is resolvable in serverless runtime environments
_src_dir = Path(__file__).resolve().parent / "src"
if _src_dir.is_dir() and str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

from agent_costbook.public_api import create_public_app

# Top-level ASGI application instance for Vercel Python runtime
app = create_public_app()
