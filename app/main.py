"""
Entry point.

Runs the scheduler and a small HTTP server side by side. The server
exists so Pond, or any monitoring system, can poll the agent's health
and confirm it is alive and collecting.

Usage:
  python -m app.main            run continuously on the configured schedule
  python -m app.main --once     run a single cycle and exit
"""

import sys
import threading
from datetime import UTC, datetime
from typing import Any

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI

from app.config import load_config
from app.pipeline import Pipeline

# Shared state between the scheduler thread and the HTTP handlers.
_last_run: dict[str, Any] = {}
_started_at = datetime.now(UTC).isoformat()

app = FastAPI(
    title="YC Radar",
    version="1.0.0",
)


@app.get("/health")
def health() -> dict[str, Any]:
    """
    Liveness and freshness check.

    Reports 'starting' before the first cycle completes, so a monitor
    does not read an empty result as a failure during boot.
    """
    config = load_config()
    pipeline_store = Pipeline(config).store

    return {
        "status": "ok" if _last_run else "starting",
        "service": "yc-radar",
        "started_at": _started_at,
        "last_run": _last_run,
        "totals": pipeline_store.stats(),
    }


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "yc-radar",
        "health": "/health",
    }


def run_cycle(config: Any) -> None:
    """Run one pipeline cycle and publish the result to the health endpoint."""
    global _last_run

    try:
        _last_run = Pipeline(config).run(
            send_summary=True
        )

    except Exception as error:
        print(f"[main] run failed: {error}")

        _last_run = {
            "error": str(error),
            "finished_at": datetime.now(UTC).isoformat(),
        }


def main() -> None:
    config = load_config()

    if "--once" in sys.argv:
        Pipeline(config).run()
        return

    scheduler = BackgroundScheduler(
        timezone="UTC"
    )

    scheduler.add_job(
        run_cycle,
        "interval",
        hours=config.poll_interval_hours,
        args=[config],
        id="collection_cycle",
        # A missed run catches up rather than being skipped, and
        # overlapping runs are collapsed into one.
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )

    scheduler.start()

    print(
        f"[main] scheduled every "
        f"{config.poll_interval_hours} hours."
    )

    # First cycle immediately, in a thread so the server starts at once.
    threading.Thread(
        target=run_cycle,
        args=(config,),
        daemon=True,
    ).start()

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )


if __name__ == "__main__":
    main()