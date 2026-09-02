"""
Early-signal classifier.

This is where noisy search results become defensible alerts. The module
answers one question about each post: is this person announcing that
their own company was accepted into YC or Speedrun?

Note what it does NOT decide. Whether a signal is 'early' is not a
judgement call, it is a database lookup: the LLM extracts the company
name, and the official register decides whether YC has published it yet.
Keeping the fallible part narrow and the verifiable part deterministic
is the whole point of the split.

Providers are pluggable. Gemini is the default because its free tier is
generous, but the interface is small enough that adding another provider
means writing one class.
"""

import json
import re
import time
from typing import Any, Protocol

from app.models import (
    STATUS_CONFIRMED_SPEEDRUN,
    STATUS_CONFIRMED_YC,
    STATUS_EARLY_SIGNAL,
    Candidate,
)

# Gemini's free tier permits a limited number of requests per minute, so
# calls are paced rather than fired in a loop. Lower this on a paid tier.
MAX_RETRIES = 3
RATE_LIMIT_BACKOFF = 15


# Kept deliberately terse. Long prompts invite the model to editorialise,
# and every extra token is paid for on each of several dozen posts a day.
SYSTEM_PROMPT = """You analyse social media posts about startup accelerators.

Decide whether the AUTHOR is announcing, for the first time, that THEIR
OWN company was accepted into Y Combinator or a16z Speedrun.

Answer with JSON only. No markdown, no code fences, no commentary.

{
  "is_announcement": true or false,
  "company_name": "the company being announced, or empty string",
  "batch": "e.g. 'YC S26', 'Speedrun SR007', or empty string",
  "founder_name": "the author's name if stated, else empty string",
  "description": "one short sentence on what the company does, else empty",
  "confidence": 0.0 to 1.0,
  "reason": "under 12 words"
}

Set is_announcement to FALSE when:
- Someone congratulates or reports on a different company
- A newsletter, investor or accelerator writes about other founders
- The author was rejected, is applying, or is still waiting
- An already-known company posts a product update, launch, rebrand or
  partnership
- The post is satire, a joke, or obviously not credible
- The author's own name or headline already contains a batch label such
  as "(YC S26)" or "(a16z SR007)". That means the company is already
  publicly known, so the post is not new news.
- The post is a retrospective: how they got in, lessons learned, or
  progress made during a batch that is already underway. Phrases such as
  "months into the batch" or "back when we applied" indicate this.

Set is_announcement to TRUE only when the author is clearly stating that
they, or their company, have just been accepted or backed.

Valid YC batch codes are W, X, S or F followed by two digits, for
example "YC S26" or "YC F26". X means Spring. If a post names a
batch that does not fit this pattern, return an empty string for
batch rather than repeating what the post said.

Never invent a company name. If the post does not state one, return an
empty string for company_name.

For founder_name, use the person who wrote the post, not other people
mentioned in it.

Lower confidence when the company name is unclear, when the post is
truncated, or when the claim is vague."""


class LLMProvider(Protocol):
    """Minimal interface a provider must satisfy."""

    def complete(self, system: str, user: str) -> str:
        """Return the model's raw text response."""
        ...


class GeminiProvider:
    """
    Google Gemini via the current google-genai SDK.

    A lite-tier model is the default: this is a short classification
    task, and the lighter model has a higher free-tier quota.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.5-flash-lite",
    ) -> None:
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model

    def complete(self, system: str, user: str) -> str:
        from google.genai import types

        response = self._client.models.generate_content(
            model=self._model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                # Deterministic output: the same post should always get
                # the same verdict, which matters for reproducible runs.
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )

        return response.text or ""


class Classifier:
    """Wraps a provider and applies the early-signal decision rules."""

    def __init__(self, config: Any, store: Any) -> None:
        self.config = config
        self.store = store
        self._min_seconds = float(
            config.classifier.get("seconds_between_calls", 2.0)
        )

        self.min_confidence = float(
            config.classifier.get("min_confidence", 0.7)
        )

        self._last_call_at = 0.0
        self._provider = self._build_provider()

    def _build_provider(self) -> LLMProvider | None:
        """
        Construct the configured provider.

        Returns None when no key is set, which puts the pipeline into
        keyword-only mode rather than failing. The bot stays useful with
        no LLM credentials, just less precise.
        """

        if not self.config.classifier_enabled:
            print(
                "[classifier] no LLM configured, "
                "running keyword-only."
            )
            return None

        provider_name = self.config.llm_provider

        try:
            if provider_name == "gemini":
                model = self.config.classifier.get(
                    "model",
                    "gemini-3.5-flash-lite",
                )

                return GeminiProvider(
                    self.config.llm_api_key,
                    model=model,
                )

        except Exception as error:
            print(
                f"[classifier] could not initialise "
                f"'{provider_name}': {error}"
            )
            return None

        print(
            f"[classifier] unknown provider "
            f"'{provider_name}', running keyword-only."
        )

        return None

    # ---------- public entry point ----------

    def classify_all(
        self,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        """
        Classify social candidates and return those worth alerting on.

        Candidates from official sources pass through untouched: the YC
        directory does not need an LLM to confirm what it published.
        """

        kept: list[Candidate] = []
        dropped = 0
        low_confidence = 0
        missing_company = 0

        for candidate in candidates:
            # Official sources are already authoritative.
            if candidate.status != STATUS_EARLY_SIGNAL:
                kept.append(candidate)
                continue

            verdict = self._classify_one(candidate)

            if not verdict.get("is_announcement"):
                dropped += 1
                continue

            confidence = float(
                verdict.get("confidence", 0.0)
            )

            if confidence < self.min_confidence:
                low_confidence += 1
                continue

            classified = self._apply_verdict(
                candidate,
                verdict,
                confidence,
            )

            # The bounty requires an actionable company-level alert.
            # Without a company name there is nothing deterministic to
            # check against the official register, so do not promote the
            # post to Slack as an early signal. The pipeline still records
            # the candidate as seen, which prevents repeated classification.
            if not classified.company_name:
                missing_company += 1
                continue

            kept.append(classified)

        print(
            f"[classifier] dropped {dropped} non-announcements, "
            f"{low_confidence} below confidence, "
            f"{missing_company} missing company, "
            f"kept {len(kept)}."
        )

        return kept

    # ---------- per-candidate work ----------

    def _classify_one(
        self,
        candidate: Candidate,
    ) -> dict[str, Any]:
        """
        Ask the model about one post, with pacing, retry and fallback.

        A 429 is transient, so it is retried. A 404 means the configured
        model does not exist for this key, which no amount of waiting
        fixes, so it fails fast instead.
        """

        if self._provider is None:
            return self._keyword_verdict(candidate)

        prompt = (
            f"Author: {candidate.founder_name or 'unknown'}\n"
            f"Source: {candidate.source}\n\n"
            f"Post:\n{candidate.post_text}"
        )

        for attempt in range(MAX_RETRIES):
            self._wait_for_slot()

            try:
                raw = self._provider.complete(
                    SYSTEM_PROMPT,
                    prompt,
                )

                return self._parse_json(raw)

            except Exception as error:
                message = str(error)

                # Permanent failure. Retrying cannot help.
                if "404" in message or "NOT_FOUND" in message:
                    print(
                        f"[classifier] model not available: "
                        f"{message[:160]}"
                    )
                    break

                # Transient. Back off and try again.
                if (
                    "429" in message
                    or "RESOURCE_EXHAUSTED" in message
                ):
                    wait = RATE_LIMIT_BACKOFF * (attempt + 1)

                    print(
                        f"[classifier] rate limited, "
                        f"waiting {wait}s..."
                    )

                    time.sleep(wait)
                    continue

                print(
                    f"[classifier] call failed: "
                    f"{message[:120]}"
                )
                break

        return self._keyword_verdict(candidate)

    def _wait_for_slot(self) -> None:
        """Space calls to stay inside the provider's per-minute quota."""

        elapsed = time.monotonic() - self._last_call_at

        if elapsed < self._min_seconds:
            time.sleep(
                self._min_seconds - elapsed
            )

        self._last_call_at = time.monotonic()

    def _apply_verdict(
        self,
        candidate: Candidate,
        verdict: dict[str, Any],
        confidence: float,
    ) -> Candidate:
        """
        Fill in the extracted fields and set the final status.

        The early-versus-confirmed decision is made here, by lookup, not
        by the model. If the register already holds this company then the
        founder did not beat YC to it, and the alert says so.
        """

        candidate.company_name = (
            verdict.get("company_name")
            or candidate.company_name
            or ""
        ).strip()

        candidate.batch = (
            verdict.get("batch") or candidate.batch
        ).strip()

        candidate.description = (
            verdict.get("description") or ""
        ).strip()[:300]

        candidate.confidence = confidence

        candidate.extra["classifier_reason"] = (
            verdict.get("reason", "")
        )

        if not candidate.founder_name:
            candidate.founder_name = (
                verdict.get("founder_name") or ""
            ).strip()

        batch_lower = candidate.batch.lower()

        is_speedrun = (
            "speedrun" in batch_lower
            or bool(re.search(r"\bsr\d{1,3}\b", batch_lower))
        )

        if (
            candidate.company_name
            and self.store.is_officially_listed(
                candidate.company_name,
                candidate.batch,
            )
        ):
            # Already published, so this is confirmation rather than
            # a scoop.
            candidate.status = (
                STATUS_CONFIRMED_SPEEDRUN
                if is_speedrun
                else STATUS_CONFIRMED_YC
            )

            candidate.extra["already_listed"] = True

        else:
            candidate.status = STATUS_EARLY_SIGNAL
            candidate.extra["already_listed"] = False

        # A company-less social post is filtered from the alert stream in
        # classify_all. This flag remains useful for diagnostics and tests.
        candidate.extra["register_checked"] = bool(candidate.company_name)

        return candidate

    # ---------- helpers ----------

    def _parse_json(
        self,
        raw: str,
    ) -> dict[str, Any]:
        """
        Parse the model's response.

        response_mime_type should guarantee clean JSON, but models
        occasionally wrap output in code fences anyway, so strip those
        before parsing rather than failing the whole candidate.
        """

        text = raw.strip()

        text = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            text,
        )

        try:
            parsed = json.loads(text)

        except json.JSONDecodeError:
            print(
                f"[classifier] unparseable response: "
                f"{text[:120]}"
            )

            return {
                "is_announcement": False,
                "confidence": 0.0,
            }

        return (
            parsed
            if isinstance(parsed, dict)
            else {"is_announcement": False}
        )

    def _keyword_verdict(
        self,
        candidate: Candidate,
    ) -> dict[str, Any]:
        """
        Fallback when no LLM is available or every retry failed.

        Conservative by design: it cannot extract a company name, so it
        reports low confidence and most candidates will fall below the
        threshold. The bot degrades to near-silence rather than to noise.
        """

        return {
            "is_announcement": True,
            "company_name": candidate.company_name,
            "batch": candidate.batch,
            "description": "",
            "confidence": 0.5,
            "reason": "keyword fallback, no LLM verdict available",
        }
