"""Private capability catalog. Price snapshots do not read or store these rows."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from agent_costbook.estimates import parse_decimal

SOURCES = frozenset({"user_observation", "aa", "official", "community", "unofficial"})
MAX_PROSE = 2000
MAX_SOURCE_REF = 2000
MAX_AGENTS = 32
MAX_BENCHMARKS = 16
MAX_CONTEXT = 10_000_000


class CapabilityError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _source(value: object) -> str:
    if not isinstance(value, str) or value not in SOURCES:
        raise ValueError(
            "source must be user_observation, aa, official, community, or unofficial"
        )
    return value


def _optional_ref(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_SOURCE_REF:
        raise ValueError("source_ref must be a short reference or null")
    return value


def _aware_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("as_of must be a timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("as_of must be a timezone-aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError("as_of must include a timezone")
    return value


def _version(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("expected_version must be a non-negative integer")
    return value


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if type(value) is not bool:
        raise ValueError("must be true, false, or null")
    return value


def _optional_prose(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_PROSE:
        raise ValueError("text must be a short non-empty string or null")
    return value


def _optional_context(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 1 or value > MAX_CONTEXT:
        raise ValueError("context_length must be a positive integer")
    return value


def _score(value: object) -> str:
    if parse_decimal(value) is None:
        raise ValueError("benchmark score must be a finite decimal string")
    return value


def _agent_ids(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > MAX_AGENTS:
        raise ValueError("default_for_agents must be a short array or null")
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 160 or item in seen:
            raise ValueError("default_for_agents entries must be unique short strings")
        seen.add(item)
    return value


def _benchmark_list(value: object) -> object:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > MAX_BENCHMARKS:
        raise ValueError("benchmarks must be a short array or null")
    return value


class BenchmarkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    score: Annotated[str, BeforeValidator(_score)]
    unit: str = Field(min_length=1, max_length=40)
    source: Annotated[str, BeforeValidator(_source)]
    source_ref: Annotated[str | None, BeforeValidator(_optional_ref)] = None


class AgentCapabilityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(min_length=1, max_length=160)
    expected_version: Annotated[int, BeforeValidator(_version)]
    source: Annotated[str, BeforeValidator(_source)]
    source_ref: Annotated[str | None, BeforeValidator(_optional_ref)] = None
    as_of: Annotated[str, BeforeValidator(_aware_timestamp)]
    domain: Annotated[str | None, BeforeValidator(_optional_prose)] = None
    strengths: Annotated[str | None, BeforeValidator(_optional_prose)] = None
    unsuitable: Annotated[str | None, BeforeValidator(_optional_prose)] = None
    can_edit_files: Annotated[bool | None, BeforeValidator(_optional_bool)] = None
    can_use_tools: Annotated[bool | None, BeforeValidator(_optional_bool)] = None
    input_output_shape: Annotated[str | None, BeforeValidator(_optional_prose)] = None


class ModelEffortIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=200)
    effort: str | None = Field(default=None, max_length=80)
    expected_version: Annotated[int, BeforeValidator(_version)]
    source: Annotated[str, BeforeValidator(_source)]
    source_ref: Annotated[str | None, BeforeValidator(_optional_ref)] = None
    as_of: Annotated[str, BeforeValidator(_aware_timestamp)]
    suitable: Annotated[str | None, BeforeValidator(_optional_prose)] = None
    unsuitable: Annotated[str | None, BeforeValidator(_optional_prose)] = None
    context_length: Annotated[int | None, BeforeValidator(_optional_context)] = None
    benchmarks: Annotated[list[BenchmarkIn] | None, BeforeValidator(_benchmark_list)] = None
    default_for_agents: Annotated[list[str] | None, BeforeValidator(_agent_ids)] = None

    @model_validator(mode="after")
    def effort_is_null_or_named(self) -> ModelEffortIn:
        if self.effort is None or self.effort == "":
            self.effort = None
            return self
        if not self.effort.strip():
            raise ValueError("effort must be null or a named tier")
        return self


def effort_key(effort: str | None) -> str:
    if effort is None or effort == "":
        return ""
    return effort


def capability_view(*, authorized: bool, agent: dict | None, model_effort: dict | None) -> dict:
    if not authorized:
        return {"agent": None, "model_effort": None, "status": "not_authorized"}
    return {"agent": agent, "model_effort": model_effort, "status": "ok"}


def unavailable_capability() -> dict:
    return {"agent": None, "model_effort": None, "status": "unavailable"}


def _canonical(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_version(current: sqlite3.Row | None, expected: int) -> int:
    if current is None:
        if expected != 0:
            raise CapabilityError("version_conflict")
        return 1
    if int(current["row_version"]) != expected:
        raise CapabilityError("version_conflict")
    return int(current["row_version"]) + 1


def _agent_row(payload: dict, *, row_version: int, recorded_at: str) -> dict:
    return {
        "agent_id": payload["agent_id"],
        "domain": payload.get("domain"),
        "strengths": payload.get("strengths"),
        "unsuitable": payload.get("unsuitable"),
        "can_edit_files": payload.get("can_edit_files"),
        "can_use_tools": payload.get("can_use_tools"),
        "input_output_shape": payload.get("input_output_shape"),
        "source": payload["source"],
        "source_ref": payload.get("source_ref"),
        "as_of": payload["as_of"],
        "row_version": row_version,
        "recorded_at": recorded_at,
    }


def _effort_row(payload: dict, *, effort: str | None, row_version: int, recorded_at: str) -> dict:
    return {
        "provider": payload["provider"],
        "model": payload["model"],
        "effort": effort,
        "suitable": payload.get("suitable"),
        "unsuitable": payload.get("unsuitable"),
        "context_length": payload.get("context_length"),
        "benchmarks": payload.get("benchmarks"),
        "default_for_agents": payload.get("default_for_agents"),
        "source": payload["source"],
        "source_ref": payload.get("source_ref"),
        "as_of": payload["as_of"],
        "row_version": row_version,
        "recorded_at": recorded_at,
    }


def _latest_agent(connection: sqlite3.Connection, agent_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT row_version, body_json
        FROM capability_agent_history
        WHERE agent_id = ?
        ORDER BY row_version DESC
        LIMIT 1
        """,
        (agent_id,),
    ).fetchone()


def _latest_effort(
    connection: sqlite3.Connection,
    provider: str,
    model: str,
    key: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT row_version, body_json
        FROM capability_model_effort_history
        WHERE provider = ? AND model = ? AND effort_key = ?
        ORDER BY row_version DESC
        LIMIT 1
        """,
        (provider, model, key),
    ).fetchone()


def _assert_defaults(
    connection: sqlite3.Connection,
    provider: str,
    model: str,
    key: str,
    agents: list[str] | None,
) -> None:
    if not agents:
        return
    wanted = set(agents)
    rows = connection.execute(
        """
        SELECT effort_key, body_json
        FROM capability_model_effort_history AS history
        WHERE provider = ? AND model = ?
          AND row_version = (
              SELECT MAX(row_version)
              FROM capability_model_effort_history
              WHERE provider = history.provider
                AND model = history.model
                AND effort_key = history.effort_key
          )
        """,
        (provider, model),
    ).fetchall()
    for row in rows:
        if row["effort_key"] == key:
            continue
        claimed = json.loads(row["body_json"]).get("default_for_agents") or []
        if wanted.intersection(claimed):
            raise CapabilityError("default_conflict")


def save_agent(connection: sqlite3.Connection, payload: dict) -> dict:
    current = _latest_agent(connection, payload["agent_id"])
    version = _next_version(current, int(payload["expected_version"]))
    row = _agent_row(payload, row_version=version, recorded_at=_now())
    connection.execute(
        """
        INSERT INTO capability_agent_history (
            agent_id, row_version, recorded_at, body_json
        ) VALUES (?, ?, ?, ?)
        """,
        (row["agent_id"], row["row_version"], row["recorded_at"], _canonical(row)),
    )
    return row


def read_agent(connection: sqlite3.Connection, agent_id: str) -> dict | None:
    current = _latest_agent(connection, agent_id)
    if current is None:
        return None
    return json.loads(current["body_json"])


def read_agent_history(connection: sqlite3.Connection, agent_id: str) -> list[dict] | None:
    rows = connection.execute(
        """
        SELECT body_json
        FROM capability_agent_history
        WHERE agent_id = ?
        ORDER BY row_version
        """,
        (agent_id,),
    ).fetchall()
    if not rows:
        return None
    return [json.loads(row["body_json"]) for row in rows]


def save_model_effort(connection: sqlite3.Connection, payload: dict) -> dict:
    effort = payload.get("effort") or None
    if effort == "":
        effort = None
    key = effort_key(effort)
    current = _latest_effort(connection, payload["provider"], payload["model"], key)
    version = _next_version(current, int(payload["expected_version"]))
    _assert_defaults(
        connection,
        payload["provider"],
        payload["model"],
        key,
        payload.get("default_for_agents"),
    )
    row = _effort_row(payload, effort=effort, row_version=version, recorded_at=_now())
    connection.execute(
        """
        INSERT INTO capability_model_effort_history (
            provider, model, effort_key, row_version, recorded_at, body_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            row["provider"],
            row["model"],
            key,
            row["row_version"],
            row["recorded_at"],
            _canonical(row),
        ),
    )
    return row


def read_model_effort(
    connection: sqlite3.Connection,
    provider: str,
    model: str,
    effort: str | None,
) -> dict | None:
    current = _latest_effort(connection, provider, model, effort_key(effort))
    if current is None:
        return None
    return json.loads(current["body_json"])


def current_catalog(connection: sqlite3.Connection) -> dict:
    agents = connection.execute(
        """
        SELECT body_json
        FROM capability_agent_history AS history
        WHERE row_version = (
            SELECT MAX(row_version)
            FROM capability_agent_history
            WHERE agent_id = history.agent_id
        )
        ORDER BY agent_id
        """
    ).fetchall()
    efforts = connection.execute(
        """
        SELECT body_json
        FROM capability_model_effort_history AS history
        WHERE row_version = (
            SELECT MAX(row_version)
            FROM capability_model_effort_history
            WHERE provider = history.provider
              AND model = history.model
              AND effort_key = history.effort_key
        )
        ORDER BY provider, model, effort_key
        """
    ).fetchall()
    return {
        "agents": [json.loads(row["body_json"]) for row in agents],
        "model_efforts": [json.loads(row["body_json"]) for row in efforts],
    }


def read_model_effort_history(
    connection: sqlite3.Connection,
    provider: str,
    model: str,
    effort: str | None,
) -> list[dict] | None:
    rows = connection.execute(
        """
        SELECT body_json
        FROM capability_model_effort_history
        WHERE provider = ? AND model = ? AND effort_key = ?
        ORDER BY row_version
        """,
        (provider, model, effort_key(effort)),
    ).fetchall()
    if not rows:
        return None
    return [json.loads(row["body_json"]) for row in rows]
