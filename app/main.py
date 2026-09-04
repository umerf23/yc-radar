"""
Entry point.

Runs the scheduler and a small HTTP server side by side. The server
provides health monitoring plus Pond Protocol V1 discovery/execution.

Usage:
  python -m app.main            run continuously on the configured schedule
  python -m app.main --once     run a single cycle and exit
"""

import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
from datetime import UTC, datetime
from typing import Any

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import load_config
from app.pipeline import Pipeline
from app.state import Store

POND_PROTOCOL_VERSION = "1.0"
POND_AGENT_VERSION = "1.1.1"

# Shared state between the scheduler thread and the HTTP handlers.
_last_run: dict[str, Any] = {}
_started_at = datetime.now(UTC).isoformat()
_pipeline_lock = threading.Lock()
_pond_run_lock = threading.Lock()

app = FastAPI(
    title="YC Radar",
    version=POND_AGENT_VERSION,
)


class RunRequest(BaseModel):
    """Pond Protocol V1 prepared execution request."""

    run_id: str
    agent_id: str
    conversation_id: str
    history_truncated: bool
    action_id: str | None = None
    user: dict[str, Any]
    messages: list[dict[str, Any]]
    parameters: dict[str, Any]
    execution: dict[str, Any]


def fail(status_code: int, code: str, message: str) -> None:
    """Return a safe Pond-compatible error response."""
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


@app.exception_handler(HTTPException)
async def pond_error(_request: Request, error: HTTPException) -> JSONResponse:
    """Keep runtime errors machine-readable for Pond."""
    return JSONResponse(
        status_code=error.status_code,
        content={"error": error.detail},
    )


@app.exception_handler(RequestValidationError)
async def invalid_request(
    _request: Request,
    _error: RequestValidationError,
) -> JSONResponse:
    """Pond expects malformed V1 requests to return HTTP 400."""
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": "invalid_request",
                "message": "The request does not match Pond Protocol V1.",
            }
        },
    )


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness, freshness, source and persistent-state health check."""
    config = load_config()
    pipeline_store = Store(config.db_path)

    return {
        "status": "ok" if _last_run else "starting",
        "service": "yc-radar",
        "started_at": _started_at,
        "last_run": _last_run,
        "totals": pipeline_store.stats(),
    }


@app.get("/manifest")
def manifest() -> dict[str, Any]:
    """Public Pond Protocol V1 discovery document."""
    empty_parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    return {
        "protocol": "marketplace-agent",
        "protocol_version": POND_PROTOCOL_VERSION,
        "agent_version": POND_AGENT_VERSION,
        "metadata": {
            "name": "YC Radar",
            "short_description": (
                "Monitors YC, a16z Speedrun, X, and LinkedIn for startup signals."
            ),
            "description": (
                "Persistent startup-monitoring agent that detects new official "
                "listings and founder social announcements, deduplicates them, "
                "and delivers qualified alerts to Slack."
            ),
            "key_features": (
                "Four-source monitoring, early-signal verification, persistent "
                "deduplication, Slack alerts, and health reporting."
            ),
            "use_cases": (
                "GTM prospecting, founder outreach, accelerator launch tracking, "
                "and early startup discovery."
            ),
        },
        "actions": [
            {
                "id": "run_monitoring_cycle",
                "name": "Run monitoring cycle",
                "description": (
                    "Run YC Radar now across YC Directory, a16z Speedrun, X, and "
                    "LinkedIn, send any new qualified alerts to Slack, and return "
                    "a summary."
                ),
                "input_schema": empty_parameters,
            },
            {
                "id": "get_monitoring_status",
                "name": "Get monitoring status",
                "description": (
                    "Return YC Radar health, the most recent monitoring result, "
                    "and persistent database totals without starting a new scan."
                ),
                "input_schema": empty_parameters,
            },
        ],
        "capabilities": {
            "sync": True,
            "streaming": False,
            "async_tasks": False,
            "cancellation": False,
            "attachments": False,
            "feedback": False,
        },
        "input_modes": ["text/plain"],
        "output_modes": ["text/markdown"],
        "limits": {
            "max_request_bytes": 262144,
            "max_attachment_bytes": 1048576,
            "max_run_seconds": 300,
        },
    }


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "yc-radar",
        "health": "/health",
        "manifest": "/manifest",
        "runs": "/runs",
        "tasks": "/tasks/{task_id}",
    }


def authenticate_pond(
    authorization: str | None = Header(default=None),
    pond_version: str | None = Header(
        default=None,
        alias="X-Agent-Protocol-Version",
    ),
) -> None:
    """Authenticate Pond runtime calls and enforce Protocol V1."""
    access_key = os.getenv("POND_ACCESS_KEY", "").strip()

    if not access_key:
        fail(
            503,
            "agent_unavailable",
            "Pond runtime access is not configured on this deployment.",
        )

    expected = f"Bearer {access_key}"
    if authorization is None or not secrets.compare_digest(authorization, expected):
        fail(401, "unauthorized", "The Access Key is missing or invalid.")

    raw_version = (pond_version or "").strip()

    # Some HTTP intermediaries may fold repeated identical headers into
    # a comma-separated value, e.g. "1.0, 1.0".
    versions = [
        part.strip()
        for part in raw_version.split(",")
        if part.strip()
    ]

    if not versions or any(
        re.fullmatch(r"\d+\.\d+", version) is None
        for version in versions
    ):
        fail(
            400,
            "invalid_request",
            f"The protocol version must be Major.Minor. Received: {raw_version!r}",
        )

    if any(
        version != POND_PROTOCOL_VERSION
        for version in versions
    ):
        fail(
            400,
            "unsupported_protocol_version",
            f"Protocol version {raw_version} is not supported.",
        )


@app.get(
    "/tasks/{task_id}",
    dependencies=[Depends(authenticate_pond)],
)
def get_task(task_id: str) -> dict[str, Any]:
    """Compatibility endpoint for Pond task validation.

    YC Radar executes Pond runs synchronously and never creates
    asynchronous tasks. The route exists so Pond can discover the
    standard V1 task endpoint without advertising async task support.
    """
    fail(
        404,
        "task_not_found",
        f"Task {task_id} does not exist because YC Radar executes synchronously.",
    )


def _request_payload(run: RunRequest) -> dict[str, Any]:
    """Return a JSON-serializable request body on Pydantic v1 or v2."""
    model_dump = getattr(run, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return run.dict()


def _request_hash(run: RunRequest) -> str:
    """Stable hash used to detect conflicting idempotency-key reuse."""
    encoded = json.dumps(
        _request_payload(run),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _pond_connection() -> sqlite3.Connection:
    """Open the same persistent SQLite database used by YC Radar."""
    config = load_config()
    connection = sqlite3.connect(config.db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS pond_runs (
            run_id TEXT PRIMARY KEY,
            request_hash TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def _load_pond_result(run_id: str) -> tuple[str, dict[str, Any]] | None:
    """Load an earlier terminal result for Pond idempotency."""
    with _pond_connection() as connection:
        row = connection.execute(
            """
            SELECT request_hash, response_json
            FROM pond_runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()

    if row is None:
        return None

    return row["request_hash"], json.loads(row["response_json"])


def _save_pond_result(
    run_id: str,
    request_hash: str,
    response: dict[str, Any],
) -> None:
    """Persist a Pond terminal response on the Railway volume."""
    with _pond_connection() as connection:
        connection.execute(
            """
            INSERT INTO pond_runs (
                run_id,
                request_hash,
                response_json,
                created_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                request_hash = excluded.request_hash,
                response_json = excluded.response_json
            """,
            (
                run_id,
                request_hash,
                json.dumps(response, separators=(",", ":")),
                datetime.now(UTC).isoformat(),
            ),
        )


def _status_text() -> str:
    """Build a concise human-readable monitoring status for Pond."""
    config = load_config()
    totals = Store(config.db_path).stats()

    if _last_run:
        last_result = json.dumps(_last_run, sort_keys=True)
    else:
        last_result = "No cycle has completed in this process yet."

    return (
        "YC Radar is online.\n\n"
        f"Last run: {last_result}\n\n"
        f"Persistent totals: {json.dumps(totals, sort_keys=True)}"
    )


def run_cycle(
    config: Any,
    send_summary: bool = True,
) -> dict[str, Any]:
    """Run one pipeline cycle and publish the result to the health endpoint."""
    global _last_run

    with _pipeline_lock:
        try:
            _last_run = Pipeline(config).run(send_summary=send_summary)
        except Exception as error:
            print(f"[main] run failed: {error}")
            _last_run = {
                "error": str(error),
                "finished_at": datetime.now(UTC).isoformat(),
            }

    return _last_run


@app.post("/runs", dependencies=[Depends(authenticate_pond)])
def create_run(
    run: RunRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
) -> dict[str, Any]:
    """Execute one authenticated Pond Protocol V1 action."""
    if idempotency_key != run.run_id:
        fail(400, "invalid_request", "Idempotency-Key must match run_id.")

    supported_actions = {
        "run_monitoring_cycle",
        "get_monitoring_status",
    }
    if run.action_id not in supported_actions:
        fail(400, "unsupported_operation", "The requested action is not supported.")

    if run.parameters:
        fail(400, "invalid_input", "This action does not accept parameters.")

    request_hash = _request_hash(run)

    # Serializing Pond runs makes the idempotency check and save atomic from
    # this process's perspective. The response itself is also persisted in
    # SQLite, so a duplicate after a redeploy can return the same result.
    with _pond_run_lock:
        saved = _load_pond_result(run.run_id)
        if saved is not None:
            saved_hash, saved_response = saved
            if not secrets.compare_digest(saved_hash, request_hash):
                fail(
                    409,
                    "idempotency_conflict",
                    "This run_id was already used for a different request.",
                )
            return saved_response

        if run.action_id == "get_monitoring_status":
            response: dict[str, Any] = {
                "run_id": run.run_id,
                "status": "completed",
                "output": [{"type": "text", "text": _status_text()}],
                "usage": {
                    "unit_of_measurement": "result",
                    "quantity": 1,
                },
            }
        else:
            summary = run_cycle(load_config(), send_summary=False)

            if "error" in summary:
                response = {
                    "run_id": run.run_id,
                    "status": "failed",
                    "error": {
                        "code": "monitoring_failed",
                        "message": "YC Radar could not complete the monitoring cycle.",
                    },
                    "usage": {
                        "unit_of_measurement": "result",
                        "quantity": 0,
                    },
                }
            else:
                text = (
                    "YC Radar monitoring cycle completed.\n\n"
                    f"- Examined: {summary.get('examined', 0)}\n"
                    f"- New candidates: {summary.get('new', 0)}\n"
                    f"- Classified: {summary.get('classified', 0)}\n"
                    f"- Slack alerts: {summary.get('alerted', 0)}\n"
                    f"- Early signals: {summary.get('early_signals', 0)}\n"
                    "- Sources: "
                    + ", ".join(summary.get("sources_run", []))
                )
                response = {
                    "run_id": run.run_id,
                    "status": "completed",
                    "output": [{"type": "text", "text": text}],
                    "usage": {
                        "unit_of_measurement": "result",
                        "quantity": 1,
                    },
                }

        _save_pond_result(run.run_id, request_hash, response)
        return response


def main() -> None:
    config = load_config()

    if "--once" in sys.argv:
        Pipeline(config).run()
        return

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        run_cycle,
        "interval",
        hours=config.poll_interval_hours,
        args=[config],
        id="collection_cycle",
        # A missed run catches up rather than being skipped, and
        # overlapping scheduled runs are collapsed into one.
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.start()

    print(f"[main] scheduled every {config.poll_interval_hours} hours.")

    # First cycle immediately, in a thread so the server starts at once.
    threading.Thread(
        target=run_cycle,
        args=(config,),
        daemon=True,
    ).start()

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
