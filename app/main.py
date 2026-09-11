"""
Entry point.

Runs the scheduler and a small HTTP server side by side. The server
provides health monitoring plus Pond Protocol V1 discovery/execution.

Usage:
  python -m app.main            run continuously on the configured schedule
  python -m app.main --once     run a single cycle and exit
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import load_config
from app.pipeline import Pipeline
from app.state import Store

POND_PROTOCOL_VERSION = "1.0"
POND_AGENT_VERSION = "1.2.0"

# Shared state between the scheduler thread and the HTTP handlers.
_last_run: dict[str, Any] = {}
_started_at = datetime.now(UTC).isoformat()
_pipeline_lock = threading.Lock()
_pond_run_lock = threading.Lock()

SLACK_SESSION_COOKIE = "yc_radar_workspace"
SLACK_STATE_COOKIE = "yc_radar_oauth_state"
SLACK_OAUTH_SCOPES = "chat:write,chat:write.public,channels:read,groups:read"

app = FastAPI(
    title="YC Radar",
    version=POND_AGENT_VERSION,
)

WEB_DIR = Path(__file__).resolve().parent / "web"
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


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


class SlackChannelSelection(BaseModel):
    """Destination selected by an authenticated Slack workspace."""

    channel_id: str


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
                    "the qualified lead details directly in the Pond chat."
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


@app.get("/", include_in_schema=False)
def root() -> FileResponse:
    """Serve the YC Radar operator dashboard."""
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api", include_in_schema=False)
def api_index() -> dict[str, str]:
    """Retain a small machine-readable endpoint index."""
    return {
        "service": "yc-radar",
        "dashboard": "/",
        "health": "/health",
        "manifest": "/manifest",
        "runs": "/runs",
        "tasks": "/tasks/{task_id}",
    }


def authenticate_pond(
    request: Request,
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

    # Prefer FastAPI's parsed header value, then read directly from the
    # ASGI request headers as a compatibility fallback.
    raw_version = (
        pond_version
        or request.headers.get("x-agent-protocol-version")
        or ""
    ).strip()

    # On the current Railway deployment this custom header is removed before
    # it reaches the app even though the client sends it. The Bearer access
    # key remains mandatory, so authenticated requests safely default to the
    # only protocol version this agent advertises and supports.
    if not raw_version:
        raw_version = POND_PROTOCOL_VERSION

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
            "The protocol version must be Major.Minor.",
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


@app.get("/api/dashboard")
def dashboard_data() -> dict[str, Any]:
    """Return one compact dashboard snapshot with no secret values."""
    config = load_config()
    dashboard_store = Store(config.db_path)

    return {
        "status": "ok" if _last_run else "starting",
        "service": "yc-radar",
        "started_at": _started_at,
        "poll_interval_hours": config.poll_interval_hours,
        "last_run": _last_run,
        "totals": dashboard_store.stats(),
        "sources": dashboard_store.source_health(),
    }


def _slack_oauth_settings() -> dict[str, str] | None:
    """Return complete Slack OAuth settings or None when not configured."""
    settings = {
        "client_id": os.getenv("SLACK_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("SLACK_CLIENT_SECRET", "").strip(),
        "redirect_uri": os.getenv("SLACK_REDIRECT_URI", "").strip(),
        "encryption_key": os.getenv("SLACK_TOKEN_ENCRYPTION_KEY", "").strip(),
        "session_secret": os.getenv("SLACK_SESSION_SECRET", "").strip(),
    }
    return settings if all(settings.values()) else None


def _session_value(team_id: str, secret: str) -> str:
    timestamp = str(int(time.time()))
    payload = f"{team_id}.{timestamp}"
    signature = hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{signature}"


def _session_team(request: Request, secret: str) -> str | None:
    value = request.cookies.get(SLACK_SESSION_COOKIE, "")
    try:
        team_id, timestamp, signature = value.split(".", 2)
        payload = f"{team_id}.{timestamp}"
        expected = hmac.new(
            secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if not secrets.compare_digest(signature, expected):
            return None
        if int(time.time()) - int(timestamp) > 30 * 24 * 60 * 60:
            return None
        return team_id
    except (TypeError, ValueError):
        return None


def _oauth_store() -> Store:
    return Store(load_config().db_path)


def _decrypt_slack_secret(value: object, settings: dict[str, str]) -> str:
    """Decrypt a stored workspace credential or reject a stale installation."""
    try:
        return Fernet(settings["encryption_key"].encode()).decrypt(
            str(value or "").encode()
        ).decode()
    except (InvalidToken, ValueError, TypeError):
        fail(503, "slack_connection_invalid", "Reconnect this Slack workspace.")


def _slack_api(
    method: str,
    token: str,
    *,
    data: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call Slack with a workspace token and normalize transport/API errors."""
    try:
        response = requests.post(
            f"https://slack.com/api/{method}",
            headers={"Authorization": f"Bearer {token}"},
            data=data,
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError):
        fail(502, "slack_unavailable", "Slack could not be reached. Try again.")
    if not result.get("ok"):
        reason = result.get("error", "unknown_error")
        if reason in {"invalid_auth", "account_inactive", "token_revoked"}:
            fail(401, "slack_reconnect_required", "Reconnect this Slack workspace.")
        fail(400, "slack_api_error", f"Slack rejected the request: {reason}.")
    return result


@app.get("/slack/install", include_in_schema=False)
def slack_install() -> RedirectResponse:
    """Begin OAuth; channel selection happens in YC Radar after approval."""
    settings = _slack_oauth_settings()
    if settings is None:
        fail(503, "slack_oauth_unavailable", "Slack installation is not configured yet.")

    state = secrets.token_urlsafe(32)
    query = urlencode(
        {
            "client_id": settings["client_id"],
            "scope": SLACK_OAUTH_SCOPES,
            "redirect_uri": settings["redirect_uri"],
            "state": state,
        }
    )
    response = RedirectResponse(f"https://slack.com/oauth/v2/authorize?{query}")
    response.set_cookie(
        SLACK_STATE_COOKIE,
        state,
        max_age=600,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@app.get("/slack/oauth/callback", include_in_schema=False)
def slack_oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
) -> RedirectResponse:
    """Exchange Slack's short-lived code and retain an encrypted bot token."""
    settings = _slack_oauth_settings()
    expected_state = request.cookies.get(SLACK_STATE_COOKIE, "")
    if settings is None:
        return RedirectResponse("/?slack=not_configured#slack")
    if error:
        return RedirectResponse("/?slack=cancelled#slack")
    if not code or not state or not expected_state or not secrets.compare_digest(
        state, expected_state
    ):
        return RedirectResponse("/?slack=invalid_state#slack")

    try:
        oauth_response = requests.post(
            "https://slack.com/api/oauth.v2.access",
            data={
                "client_id": settings["client_id"],
                "client_secret": settings["client_secret"],
                "code": code,
                "redirect_uri": settings["redirect_uri"],
            },
            timeout=20,
        )
        oauth_response.raise_for_status()
        result = oauth_response.json()
    except (requests.RequestException, ValueError):
        return RedirectResponse("/?slack=oauth_failed#slack")
    team = result.get("team") or {}
    token = result.get("access_token") or ""
    if not result.get("ok") or not token or not team.get("id"):
        return RedirectResponse("/?slack=oauth_failed#slack")

    try:
        encrypted = Fernet(settings["encryption_key"].encode()).encrypt(
            token.encode()
        ).decode()
    except (ValueError, TypeError):
        return RedirectResponse("/?slack=server_config#slack")

    _oauth_store().save_slack_installation(
        team_id=team["id"],
        team_name=team.get("name") or "Slack workspace",
        bot_token_encrypted=encrypted,
        bot_user_id=result.get("bot_user_id") or "",
    )
    response = RedirectResponse("/?slack=choose_channel#slack")
    response.delete_cookie(SLACK_STATE_COOKIE)
    response.set_cookie(
        SLACK_SESSION_COOKIE,
        _session_value(team["id"], settings["session_secret"]),
        max_age=30 * 24 * 60 * 60,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@app.get("/api/slack/status")
def slack_status(request: Request) -> dict[str, Any]:
    """Report OAuth availability and this browser's connected workspace."""
    settings = _slack_oauth_settings()
    if settings is None:
        return {"available": False, "connected": False}
    team_id = _session_team(request, settings["session_secret"])
    installation = _oauth_store().slack_installation(team_id) if team_id else None
    if installation is None:
        return {"available": True, "connected": False}
    return {
        "available": True,
        "connected": True,
        "workspace": installation["team_name"],
        "channel": installation["channel_name"],
        "channel_id": installation["channel_id"],
        "channel_configured": bool(installation["channel_id"]),
        "last_manual_run_at": installation["last_manual_run_at"],
    }


@app.get("/api/slack/channels")
def slack_channels(request: Request) -> dict[str, Any]:
    """List public channels and private channels the installed bot can access."""
    settings = _slack_oauth_settings()
    if settings is None:
        fail(503, "slack_oauth_unavailable", "Slack installation is not configured.")
    team_id = _session_team(request, settings["session_secret"])
    installation = _oauth_store().slack_installation(team_id) if team_id else None
    if installation is None:
        fail(401, "slack_not_connected", "Connect a Slack workspace first.")
    token = _decrypt_slack_secret(installation["bot_token_encrypted"], settings)

    channels: list[dict[str, str]] = []
    cursor = ""
    for _ in range(10):
        result = _slack_api(
            "conversations.list",
            token,
            params={
                "types": "public_channel,private_channel",
                "exclude_archived": "true",
                "limit": 200,
                "cursor": cursor,
            },
        )
        channels.extend(
            {"id": channel["id"], "name": channel.get("name") or channel["id"]}
            for channel in result.get("channels", [])
            if channel.get("id") and not channel.get("is_archived")
        )
        cursor = (result.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    channels.sort(key=lambda channel: channel["name"].lower())
    return {"workspace": installation["team_name"], "channels": channels}


@app.post("/api/slack/channel")
def select_slack_channel(
    selection: SlackChannelSelection,
    request: Request,
    action_header: str | None = Header(default=None, alias="X-YC-Radar-Action"),
) -> dict[str, Any]:
    """Validate and save a channel for the browser's authenticated workspace."""
    settings = _slack_oauth_settings()
    if settings is None or action_header != "select-channel":
        fail(403, "forbidden", "Slack workspace authorization is required.")
    if not re.fullmatch(r"[CG][A-Z0-9]+", selection.channel_id):
        fail(400, "invalid_channel", "Select a valid Slack channel.")
    team_id = _session_team(request, settings["session_secret"])
    store = _oauth_store()
    installation = store.slack_installation(team_id) if team_id else None
    if installation is None:
        fail(401, "slack_not_connected", "Connect a Slack workspace first.")
    token = _decrypt_slack_secret(installation["bot_token_encrypted"], settings)
    result = _slack_api(
        "conversations.info",
        token,
        params={"channel": selection.channel_id},
    )
    channel = result.get("channel") or {}
    if channel.get("is_archived") or not channel.get("name"):
        fail(400, "invalid_channel", "Choose an active Slack channel.")
    store.update_slack_channel(team_id, selection.channel_id, channel["name"])
    return {
        "status": "saved",
        "workspace": installation["team_name"],
        "channel": channel["name"],
    }


def _workspace_blocks(candidate: dict[str, object]) -> list[dict[str, Any]]:
    """Build a compact Slack alert from the persisted safe candidate fields."""
    heading = (
        "EARLY SIGNAL — founder announced before official listing"
        if candidate["status"] == "EARLY_SIGNAL"
        else "NEW ACCELERATOR COMPANY"
    )
    fields = [
        f"*Company*\n{candidate['company_name'] or 'Not stated'}",
        f"*Batch*\n{candidate['batch'] or 'Unknown'}",
        f"*Source*\n{candidate['source']}",
        f"*Confidence*\n{float(candidate['confidence'] or 0):.0%}",
    ]
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": heading}},
        {
            "type": "section",
            "fields": [{"type": "mrkdwn", "text": field} for field in fields],
        },
    ]
    if candidate.get("url"):
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"<{candidate['url']}|Open original source>",
                },
            }
        )
    return blocks


def _send_workspace_message(
    installation: dict[str, object],
    settings: dict[str, str],
    text: str,
    blocks: list[dict[str, Any]] | None = None,
) -> None:
    """Deliver through a bot token, retaining legacy webhook compatibility."""
    encrypted_token = installation.get("bot_token_encrypted")
    if encrypted_token:
        token = _decrypt_slack_secret(encrypted_token, settings)
        data: dict[str, Any] = {
            "channel": installation["channel_id"],
            "text": text,
            "unfurl_links": "false",
            "unfurl_media": "false",
        }
        if blocks:
            data["blocks"] = json.dumps(blocks)
        _slack_api("chat.postMessage", token, data=data)
        return

    webhook_url = _decrypt_slack_secret(
        installation.get("webhook_encrypted"), settings
    )
    response = requests.post(
        webhook_url,
        json={"text": text, **({"blocks": blocks} if blocks else {})},
        timeout=20,
    )
    response.raise_for_status()


@app.post("/api/slack/run")
def run_for_slack_workspace(
    request: Request,
    action_header: str | None = Header(default=None, alias="X-YC-Radar-Action"),
) -> dict[str, Any]:
    """Run the monitor and deliver every lead pending for this workspace."""
    settings = _slack_oauth_settings()
    if settings is None or action_header != "run":
        fail(403, "forbidden", "Slack workspace authorization is required.")
    team_id = _session_team(request, settings["session_secret"])
    store = _oauth_store()
    installation = store.slack_installation(team_id) if team_id else None
    if installation is None:
        fail(401, "slack_not_connected", "Connect a Slack workspace first.")
    if not installation["channel_id"]:
        fail(409, "slack_channel_required", "Choose a Slack channel first.")
    if not store.claim_slack_run(team_id):
        fail(429, "run_cooldown", "Please wait five minutes before running again.")

    summary = run_cycle(load_config(), send_summary=False)
    if "error" in summary:
        fail(502, "monitoring_failed", "YC Radar could not complete the scan.")

    # Candidate discovery is global, but delivery deduplication is scoped to
    # team_id. An alert sent to one workspace therefore remains pending for
    # every other workspace until Slack accepts it there too.
    pending = store.pending_slack_candidates(team_id, limit=100)
    delivered = 0
    for candidate in pending:
        _send_workspace_message(
            installation,
            settings,
            f"YC Radar: {candidate['company_name']}",
            _workspace_blocks(candidate),
        )
        store.record_slack_delivery(
            team_id,
            str(candidate["dedup_key"]),
            str(installation["channel_id"]),
        )
        delivered += 1

    # Intentionally send no Slack summary when nothing qualifies. The API
    # response still tells the dashboard that the scan completed quietly.
    return {
        "status": "completed",
        "delivered": delivered,
        "remaining": store.pending_slack_count(team_id),
        "summary": summary,
    }


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


def _pond_safe_text(value: object, fallback: str = "Unknown") -> str:
    """Keep one untrusted candidate value readable inside Pond markdown."""
    text = " ".join(str(value or "").split()).strip()
    return text or fallback


def _pond_run_leads(
    config: Any,
    started_at: str,
    limit: int = 20,
) -> tuple[list[dict[str, object]], bool]:
    """Return leads delivered during this Pond-triggered monitoring cycle."""
    candidates = Store(config.db_path).recent_candidates(limit=250)
    matching = [
        candidate
        for candidate in candidates
        if str(candidate["first_seen_at"]) >= started_at
    ]
    return matching[:limit], len(matching) > limit


def _pond_leads_text(
    summary: dict[str, Any],
    leads: list[dict[str, object]],
    truncated: bool = False,
) -> str:
    """Build the Pond chat response with summary and actionable lead details."""
    lines = [
        "YC Radar monitoring cycle completed.",
        "",
        f"- Examined: {summary.get('examined', 0)}",
        f"- New candidates: {summary.get('new', 0)}",
        f"- Qualified leads: {summary.get('alerted', len(leads))}",
        f"- Slack alerts: {summary.get('alerted', 0)}",
        f"- Early signals: {summary.get('early_signals', 0)}",
        "- Sources: " + ", ".join(summary.get("sources_run", [])),
        "",
    ]

    if not leads:
        lines.append("No new qualified leads were found in this run.")
        return "\n".join(lines)

    lines.append("## New qualified leads")
    for index, lead in enumerate(leads, start=1):
        confidence = float(lead.get("confidence") or 0)
        lines.extend(
            [
                "",
                f"### {index}. {_pond_safe_text(lead.get('company_name'), 'Company not stated')}",
                f"- Status: {_pond_safe_text(lead.get('status'))}",
                f"- Batch: {_pond_safe_text(lead.get('batch'))}",
                f"- Source: {_pond_safe_text(lead.get('source'))}",
                f"- Confidence: {confidence:.0%}",
            ]
        )
        if lead.get("founder_handle"):
            lines.append(
                f"- Founder: {_pond_safe_text(lead.get('founder_handle'))}"
            )
        if lead.get("url"):
            lines.append(f"- Original source: {_pond_safe_text(lead.get('url'))}")

    if truncated:
        lines.extend(
            [
                "",
                "More than 20 leads qualified. The remaining alerts are available in Slack.",
            ]
        )
    return "\n".join(lines)


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
            config = load_config()
            summary = run_cycle(config, send_summary=False)

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
                leads, truncated = _pond_run_leads(
                    config,
                    str(summary.get("started_at", "")),
                )
                text = _pond_leads_text(summary, leads, truncated)
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
