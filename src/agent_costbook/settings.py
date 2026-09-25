from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: Path
    admin_token: str


def load_settings() -> Settings:
    return Settings(
        db_path=Path(os.environ.get("ACB_DB", "agent_costbook.sqlite3")),
        admin_token=os.environ.get("ACB_ADMIN_TOKEN", ""),
    )
