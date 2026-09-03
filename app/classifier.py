"""
Early-signal classifier.

This is where noisy search results become defensible alerts. The model has
one narrow job: decide whether the author is announcing that their own
company was accepted into YC or a16z Speedrun, and extract the stated facts.

The model does NOT decide whether a signal is early. That decision is made
deterministically from programme evidence plus the persisted official
register. This keeps the fallible extraction step separate from the claim
that a company beat an official announcement.
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
    canonical_batch,
    programme_from_batch,
)

# Gemini's free tier permits a limited number of requests per minute, so
# calls are paced rather than fired in a loop. Lower this on a paid tier.
MAX_RETRIES = 3
RATE_LIMIT_BACKOFF = 15

# Deterministic evidence checks performed after the LLM verdict.
YC_PROGRAMME = re.compile(
    r"\b(?:y\s*combinator|ycombinator|yc)\b",
    re.IGNORECASE,
)

SPEEDRUN_PROGRAMME = re.compile(
    r"\b(?:a16z\s+speedrun|speedrun|sr[\s\-_]?\d{1,3})\b",
    re.IGNORECASE,
)

YC_BATCH_MENTION = re.compile(
    r"\bYC\s*([A-Z])\s*'?\s*(\d{2})\b",
    re.IGNORECASE,
)

VALID_YC_BATCH_LETTERS = {"W", "X", "S", "F"}

GENERIC_COMPANY_NAMES = {
    "company",
    "ourcompany",
    "mystartup",
    "ourstartup",
    "startup",
    "stealth",
    "stealthstartup",
    "unknown",
    "unnamed",
    "na",
    "none",
    "notstated",
    "notstatedinpost",
}


# Kept deliberately terse. Long prompts invite the model to editorialise,
# and every extra token is paid for on each of several dozen posts a day.
SYSTEM_PROMPT = """You analyse social media posts about startup accelerators.

Decide whether the AUTHOR is announcing, for the first time, that THEIR
OWN company was accepted into Y Combinator or a16z Speedrun.

Answer with JSON only. No markdown, no code fences, no commentary.

{
  "is_announcement": true or false,
  "company_name": "the company explicitly named in the post, or empty string",
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
example "YC S26" or "YC F26". X means Spring. If a post names a batch
that does not fit this pattern, return an empty string for batch rather
than repeating what the post said.

Never invent a company name. company_name must be stated in the supplied
post text. Do not infer it from outside knowledge, the author's profile,
or a likely employer. If the post does not state one, return an empty
string for company_name.

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

        Candidates from official sources pass through untouched: the
        official directory does not need an LLM to confirm what it published.
        """
        kept: list[Candidate] = []
        dropped = 0
        low_confidence = 0
        missing_company = 0
        invalid_evidence = 0

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
            if not classified.company_name:
                missing_company += 1
                continue

            # The LLM verdict is necessary but not sufficient. Programme
            # evidence and batch validity are checked deterministically.
            if classified.extra.get("validation_error"):
                invalid_evidence += 1
                continue

            kept.append(classified)

        print(
            f"[classifier] dropped {dropped} non-announcements, "
            f"{low_confidence} below confidence, "
            f"{missing_company} missing company, "
            f"{invalid_evidence} invalid evidence, "
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
        Fill extracted fields, validate evidence, and set the final status.

        EARLY_SIGNAL is assigned only after:
          1. a usable company name exists,
          2. the post contains deterministic YC/Speedrun evidence,
          3. any YC batch code in the post is valid, and
          4. the programme-specific official register has been checked.
        """
        source_batch = candidate.batch
        verdict_batch = str(verdict.get("batch") or "").strip()

        candidate.company_name = (
            verdict.get("company_name")
            or candidate.company_name
            or ""
        ).strip()

        # Never let an invalid model-produced batch overwrite a valid batch
        # already extracted by the source.
        if canonical_batch(verdict_batch):
            candidate.batch = verdict_batch
        elif canonical_batch(source_batch):
            candidate.batch = source_batch
        else:
            candidate.batch = ""

        candidate.description = (
            verdict.get("description") or ""
        ).strip()[:300]

        candidate.confidence = confidence

        candidate.extra["classifier_reason"] = (
            verdict.get("reason", "")
        )
        candidate.extra["canonical_batch"] = canonical_batch(
            candidate.batch
        )

        if not candidate.founder_name:
            candidate.founder_name = (
                verdict.get("founder_name") or ""
            ).strip()

        candidate.extra.pop("validation_error", None)

        if not candidate.company_name:
            candidate.extra["register_checked"] = False
            return candidate

        validation_error, programme = self._validate_evidence(candidate)

        if validation_error:
            candidate.extra["validation_error"] = validation_error
            candidate.extra["register_checked"] = False
            candidate.extra["programme"] = programme
            return candidate

        candidate.extra["programme"] = programme

        official_match = self._official_match(
            candidate.company_name,
            candidate.batch,
            programme,
        )

        candidate.extra["register_checked"] = True

        if official_match:
            official_programme = (
                official_match.get("programme")
                or programme
            )

            candidate.status = (
                STATUS_CONFIRMED_SPEEDRUN
                if official_programme == "speedrun"
                else STATUS_CONFIRMED_YC
            )

            candidate.extra["already_listed"] = True
            candidate.extra["official_match"] = {
                "company_name": official_match.get("company_name", ""),
                "batch": official_match.get("batch", ""),
                "profile_url": official_match.get("profile_url", ""),
                "programme": official_programme,
                "match_type": official_match.get("match_type", "exact"),
            }

        else:
            candidate.status = STATUS_EARLY_SIGNAL
            candidate.extra["already_listed"] = False
            candidate.extra.pop("official_match", None)

        return candidate

    # ---------- deterministic validation ----------

    def _validate_evidence(
        self,
        candidate: Candidate,
    ) -> tuple[str, str]:
        """
        Return (error_code, programme).

        An empty error code means the social post has enough deterministic
        evidence to support an early/confirmed classification.
        """
        if not self._usable_company_name(candidate.company_name):
            return "invalid_company_name", ""

        text = candidate.post_text or ""

        invalid_code = YC_BATCH_MENTION.search(text)
        if (
            invalid_code
            and invalid_code.group(1).upper()
            not in VALID_YC_BATCH_LETTERS
        ):
            return "invalid_yc_batch", "yc"

        programmes: set[str] = set()

        batch_programme = programme_from_batch(candidate.batch)
        if batch_programme:
            programmes.add(batch_programme)

        if YC_PROGRAMME.search(text):
            programmes.add("yc")

        if SPEEDRUN_PROGRAMME.search(text):
            programmes.add("speedrun")

        if not programmes:
            return "missing_programme_evidence", ""

        if len(programmes) > 1:
            return "programme_conflict", ""

        return "", next(iter(programmes))

    def _usable_company_name(self, company_name: str) -> bool:
        """Reject placeholders that cannot support a company-level alert."""
        compact = re.sub(
            r"[^a-z0-9]",
            "",
            (company_name or "").lower(),
        )

        return (
            len(compact) >= 2
            and compact not in GENERIC_COMPANY_NAMES
        )

    def _official_match(
        self,
        company_name: str,
        batch: str,
        programme: str,
    ) -> dict[str, Any] | None:
        """
        Query the richer register API when available.

        The compatibility fallback keeps Classifier usable with older stores
        and simple test doubles.
        """
        match_method = getattr(self.store, "official_match", None)

        if callable(match_method):
            return match_method(
                company_name,
                batch,
                programme=programme,
            )

        if self.store.is_officially_listed(
            company_name,
            batch,
        ):
            return {
                "company_name": company_name,
                "batch": batch,
                "profile_url": "",
                "programme": programme,
                "match_type": "legacy_boolean",
            }

        return None

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
