"""
The orchestrator.

One run: collect from every enabled source, drop anything already seen,
classify the social candidates, deliver what survives, and record the
outcome. This is the only place that knows the order of operations.

Deduplication happens before classification on purpose. Classifying a
post costs an API call, and re-classifying something already alerted on
would be spending money to reach a conclusion already reached.
"""

from datetime import UTC, datetime
from typing import Any

from app.classifier import Classifier
from app.models import Candidate
from app.notifier import Notifier
from app.sources import build_sources
from app.state import Store


class Pipeline:
    """Runs one full collection cycle."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.store = Store(config.db_path)
        self.classifier = Classifier(config, self.store)
        self.notifier = Notifier(config)
        self.sources = build_sources(config, self.store)

    def run(self, send_summary: bool = False) -> dict[str, Any]:
        """
        Execute one cycle and return a summary of what happened.

        The returned dict feeds the health endpoint, so a monitoring
        system can tell a healthy quiet run from a degraded or broken one.
        """
        started = datetime.now(UTC)
        print(f"\n=== run started {started.isoformat()} ===")

        # Early-signal decisions must use the official state that existed
        # before this monitoring cycle began. Otherwise YC Directory can
        # discover a company first in _collect(), causing a founder post
        # found later in the same cycle to be incorrectly labelled confirmed.
        #
        # A completely fresh database has no baseline yet, so leave the
        # cutoff disabled while the first official directory seed is created.
        official_count_at_start = self.store.official_count()
        self.classifier.official_cutoff = (
            started.isoformat()
            if official_count_at_start > 0
            else None
        )

        collected, source_status = self._collect()
        fresh = self.store.filter_new(collected)
        print(f"[pipeline] {len(collected)} collected, {len(fresh)} new.")

        # Capture each candidate's key BEFORE classification. The
        # classifier fills in company_name, and dedup_key is derived from
        # that name, so the key changes mid-pipeline. Storing the new key
        # would leave the old one unrecorded, and the same post would look
        # new on every subsequent run.
        original_keys = {
            id(candidate): candidate.dedup_key
            for candidate in fresh
        }

        classified = self.classifier.classify_all(fresh)
        delivered = self.notifier.send_all(classified)

        # Identity comparison rather than key comparison, for the same
        # reason: keys are no longer stable across the classify step.
        delivered_ids = {id(candidate) for candidate in delivered}

        # Record everything new, including candidates the classifier
        # rejected, so they are never reconsidered on a later run.
        for candidate in fresh:
            self.store.record_with_key(
                original_keys[id(candidate)],
                candidate,
                alerted=id(candidate) in delivered_ids,
            )

        succeeded = [
            name
            for name, result in source_status.items()
            if result["status"] == "ok"
        ]
        degraded = [
            name
            for name, result in source_status.items()
            if result["status"] == "degraded"
        ]
        failed = [
            name
            for name, result in source_status.items()
            if result["status"] in {"failed", "skipped"}
        ]

        summary = {
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "examined": len(collected),
            "new": len(fresh),
            "classified": len(classified),
            "alerted": len(delivered),
            "early_signals": sum(
                1 for item in delivered if item.is_early_signal
            ),
            # Backward-compatible field: now means sources that completed
            # successfully, rather than merely configured sources.
            "sources_run": succeeded,
            "sources_attempted": [
                name
                for name, result in source_status.items()
                if result["status"] != "skipped"
            ],
            "sources_degraded": degraded,
            "sources_failed": failed,
            "source_status": source_status,
        }

        if send_summary and not delivered:
            self.notifier.send_run_summary(summary)

        print(
            f"[pipeline] done: {summary['alerted']} alerts sent; "
            f"{len(succeeded)} sources ok, "
            f"{len(degraded)} degraded, {len(failed)} failed/skipped."
        )
        return summary

    def _collect(
        self,
    ) -> tuple[list[Candidate], dict[str, dict[str, Any]]]:
        """
        Gather from every source while isolating failures.

        A source can complete successfully, complete in a degraded state
        with partial results, fail with an exception, or be skipped because
        it is not configured. One unhealthy platform never stops the others.
        """
        collected: list[Candidate] = []
        source_status: dict[str, dict[str, Any]] = {}

        for source in self.sources:
            availability = getattr(source, "is_available", None)

            if callable(availability) and not availability():
                error = "not_configured"
                print(f"[pipeline] source '{source.name}' skipped: {error}")
                self.store.mark_run(
                    source.name,
                    0,
                    error=error,
                )
                source_status[source.name] = {
                    "status": "skipped",
                    "items_found": 0,
                    "error": error,
                }
                continue

            try:
                found = source.collect()
                collected.extend(found)

                source_error = getattr(source, "last_error", None)

                if source_error:
                    self.store.mark_run(
                        source.name,
                        len(found),
                        error=str(source_error),
                    )
                    source_status[source.name] = {
                        "status": "degraded",
                        "items_found": len(found),
                        "error": str(source_error),
                    }
                else:
                    self.store.mark_run(source.name, len(found))
                    source_status[source.name] = {
                        "status": "ok",
                        "items_found": len(found),
                        "error": None,
                    }

            except Exception as error:
                message = str(error)
                print(f"[pipeline] source '{source.name}' failed: {message}")
                self.store.mark_run(
                    source.name,
                    0,
                    error=message,
                )
                source_status[source.name] = {
                    "status": "failed",
                    "items_found": 0,
                    "error": message,
                }

        return collected, source_status
