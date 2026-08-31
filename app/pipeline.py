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
        system can tell a healthy quiet run from a broken one.
        """
        started = datetime.now(UTC)
        print(f"\n=== run started {started.isoformat()} ===")

        collected = self._collect()
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
            "sources_run": [source.name for source in self.sources],
        }

        if send_summary and not delivered:
            self.notifier.send_run_summary(summary)

        print(f"[pipeline] done: {summary['alerted']} alerts sent.")
        return summary

    def _collect(self) -> list[Candidate]:
        """
        Gather from every source, isolating failures.

        A source that raises is logged against its own name and the run
        continues. Losing one platform for one cycle is acceptable;
        losing the whole run is not.
        """
        collected: list[Candidate] = []

        for source in self.sources:
            try:
                found = source.collect()
                collected.extend(found)
                self.store.mark_run(source.name, len(found))
            except Exception as error:
                print(f"[pipeline] source '{source.name}' failed: {error}")
                self.store.mark_run(
                    source.name,
                    0,
                    error=str(error),
                )

        return collected
