"""
X (Twitter) collector.

This is the primary early-detection source. Founders announce their
acceptance on X days or weeks before the company appears in any official
directory, which is the entire premise of this bot.

Three design points worth understanding:

1. Search operators. X disabled the classic 'since:' and 'until:'
   operators, so incremental windowing uses 'since_time:' with a Unix
   timestamp instead. The window start comes from the state layer, which
   now advances only after a successful source run.

2. The display-name signal. Founders who are already publicly announced
   put their batch in their handle, as in "Conifer (YC S26)". Their posts
   are product updates, not acceptance news. Filtering on this one
   pattern removes the large majority of the noise.

3. Division of labour. This module does not decide whether a post is a
   real announcement. It narrows the field cheaply, then hands survivors
   to the classifier. Provider errors are exposed through last_error so
   the pipeline can report a degraded source instead of pretending a
   failed API request was a healthy zero-result run.
"""

import re
import time
from typing import Any

import requests

from app.models import STATUS_EARLY_SIGNAL, Candidate
from app.sources.base import Source

API_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"
REQUEST_TIMEOUT = 30

# Pages return up to 20 tweets, so this caps pagination per query.
MAX_PAGES = 3

# TwitterAPI.io enforces a per-second request cap. Requests are spaced
# rather than fired in a tight loop, and 429 responses back off and retry.
MIN_SECONDS_BETWEEN_REQUESTS = 8

MAX_RETRIES = 3
BACKOFF_SECONDS = 5

# Valid YC batch letters are W, S, X and F. "P" is deliberately excluded.
BATCH_PATTERN = re.compile(
    r"\bYC\s*([WSXF]\d{2})\b",
    re.IGNORECASE,
)

COHORT_PATTERN = re.compile(
    r"\bSR(\d{3})\b",
    re.IGNORECASE,
)

# A batch label in the author's display name means the company is already
# publicly associated with the programme, so this is not an early signal.
ANNOUNCED_IN_NAME = re.compile(
    r"\((?:YC|Y Combinator)\s*[WSXF]?\d{2}\)|\bSR\d{3}\b",
    re.IGNORECASE,
)

# Phrases that mean the post is not a first-time acceptance announcement.
NOISE_MARKERS = (
    "congrat",
    "rejected",
    "rejection",
    "didn't get in",
    "did not get in",
    "didn't make it",
    "did not make it",
    "wasn't accepted",
    "no luck",
    "third time",
    "applying to",
    "application is",
    "apply to yc",
    "applications open",
    "how to get into",
    "tips for",
    "hiring",
    "questions for anyone",
    "what was the interview",
)

# Language that marks a post as an acceptance announcement rather than a
# product update from a company announced long ago.
ACCEPTANCE_MARKERS = (
    "got into",
    "accepted into",
    "accepted to",
    "joining yc",
    "joining y combinator",
    "we're in yc",
    "we are in yc",
    "part of yc",
    "backed by y combinator",
    "backed by yc",
    "backed by a16z speedrun",
    "joining speedrun",
    "part of a16z speedrun",
    "excited to announce that we",
    "thrilled to share that we",
)


class XTwitterSource(Source):
    name = "x_twitter"

    def __init__(self, config: Any, store: Any = None) -> None:
        super().__init__(config, store)

        # Monotonic clock, so pacing is unaffected by system time changes.
        self._last_request_at = 0.0

        # Public diagnostic consumed by Pipeline._collect().
        self.last_error: str | None = None

    def is_available(self) -> bool:
        """Without an API key this source is skipped, not failed."""
        return bool(self.config.twitterapi_key)

    def collect(self) -> list[Candidate]:
        self.last_error = None

        if not self.is_available():
            print(f"[{self.name}] no TWITTERAPI_KEY set, skipping.")
            return []

        since = self.store.since_timestamp(
            self.name,
            self.config.lookback_hours,
        )

        queries = self.settings.get("queries", [])
        limit = self.settings.get("max_results_per_query", 40)

        print(
            f"[{self.name}] searching {len(queries)} "
            f"queries since {since}."
        )

        candidates: list[Candidate] = []
        seen_ids: set[str] = set()

        examined = 0
        filtered = 0

        for query in queries:
            tweets = self._search(query, since, limit)

            for tweet in tweets:
                tweet_id = str(tweet.get("id", ""))

                if not tweet_id or tweet_id in seen_ids:
                    continue

                seen_ids.add(tweet_id)
                examined += 1

                text = tweet.get("text", "")
                author_name = (
                    tweet.get("author", {}) or {}
                ).get("name", "")

                if not self._looks_like_announcement(
                    text,
                    author_name,
                ):
                    filtered += 1
                    continue

                candidate = self._to_candidate(tweet, text)

                if candidate:
                    candidates.append(candidate)

            # Auth, credit, provider or exhausted-rate-limit failures are
            # source-level problems. Keep any partial results already found,
            # but do not waste calls on the remaining queries this cycle.
            if self.last_error:
                break

        print(
            f"[{self.name}] examined {examined} posts, "
            f"prefilter dropped {filtered}, "
            f"kept {len(candidates)}"
            + (
                f", degraded: {self.last_error}."
                if self.last_error
                else "."
            )
        )

        return candidates

    # ---------- fetching ----------

    def _search(
        self,
        query: str,
        since: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        """
        Run one paginated search, respecting the provider's rate limit.

        Requests are spaced by MIN_SECONDS_BETWEEN_REQUESTS. Transient
        timeouts, connection failures and 429 responses are retried. If
        retries are exhausted or the provider rejects
        auth/credits, last_error is set so the pipeline can mark this source
        degraded without stopping YC, Speedrun or LinkedIn.
        """
        collected: list[dict[str, Any]] = []
        cursor = ""

        headers = {
            "X-API-Key": self.config.twitterapi_key,
        }

        # since_time replaces the deprecated 'since:' operator.
        full_query = f"{query} since_time:{since}"

        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {
                "query": full_query,
                "queryType": "Latest",
            }

            if cursor:
                params["cursor"] = cursor

            payload = self._request_with_retry(
                headers,
                params,
                query,
            )

            if payload is None:
                break

            tweets = payload.get("tweets", [])

            if not tweets:
                break

            collected.extend(tweets)

            if len(collected) >= limit:
                break

            cursor = payload.get("next_cursor", "")

            if not cursor:
                break

        return collected[:limit]

    def _request_with_retry(
        self,
        headers: dict[str, str],
        params: dict[str, Any],
        query_label: str,
    ) -> dict[str, Any] | None:
        """
        Make one provider call with pacing and bounded retries.

        Errors are converted into stable diagnostic codes rather than being
        silently treated as legitimate zero-result searches.
        """
        for attempt in range(MAX_RETRIES):
            self._wait_for_slot()

            try:
                response = requests.get(
                    API_URL,
                    headers=headers,
                    params=params,
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.Timeout as error:
                wait = BACKOFF_SECONDS * (attempt + 1)
                print(
                    f"[{self.name}] request timed out on "
                    f"'{query_label[:40]}', "
                    f"retrying in {wait}s..."
                )

                if attempt < MAX_RETRIES - 1:
                    time.sleep(wait)
                    continue

                self.last_error = "provider_timeout"
                print(
                    f"[{self.name}] timeout persisted after "
                    f"{MAX_RETRIES} attempts: {error}"
                )
                break

            except requests.ConnectionError as error:
                wait = BACKOFF_SECONDS * (attempt + 1)
                print(
                    f"[{self.name}] connection error on "
                    f"'{query_label[:40]}', "
                    f"retrying in {wait}s..."
                )

                if attempt < MAX_RETRIES - 1:
                    time.sleep(wait)
                    continue

                self.last_error = "provider_connection_error"
                print(
                    f"[{self.name}] connection failed after "
                    f"{MAX_RETRIES} attempts: {error}"
                )
                break

            except requests.RequestException as error:
                self.last_error = "provider_request_error"
                print(
                    f"[{self.name}] request error on "
                    f"'{query_label[:40]}': {error}"
                )
                return None

            status = response.status_code

            if status == 200:
                try:
                    return response.json()
                except ValueError:
                    self.last_error = "provider_invalid_response"
                    print(
                        f"[{self.name}] could not parse response body."
                    )
                    return None

            if status == 429:
                wait = BACKOFF_SECONDS * (attempt + 1)
                print(
                    f"[{self.name}] rate limited, "
                    f"waiting {wait}s..."
                )

                if attempt < MAX_RETRIES - 1:
                    time.sleep(wait)
                    continue

                self.last_error = "provider_rate_limited"
                break

            if status in {500, 502, 503, 504}:
                wait = BACKOFF_SECONDS * (attempt + 1)
                print(
                    f"[{self.name}] provider HTTP {status}, "
                    f"retrying in {wait}s..."
                )

                if attempt < MAX_RETRIES - 1:
                    time.sleep(wait)
                    continue

                self.last_error = f"provider_server_error_{status}"
                break

            if status in {401, 403}:
                self.last_error = "provider_auth_failed"
            elif status == 402:
                self.last_error = "provider_credit_exhausted"
            else:
                self.last_error = f"provider_http_{status}"

            print(
                f"[{self.name}] HTTP {status} "
                f"on '{query_label[:40]}' "
                f"({self.last_error})"
            )
            return None

        print(
            f"[{self.name}] gave up after {MAX_RETRIES} attempts "
            f"on '{query_label[:40]}'"
        )

        return None

    def _wait_for_slot(self) -> None:
        """Space consecutive requests to stay under the provider's limit."""
        elapsed = time.monotonic() - self._last_request_at

        if elapsed < MIN_SECONDS_BETWEEN_REQUESTS:
            time.sleep(
                MIN_SECONDS_BETWEEN_REQUESTS - elapsed
            )

        self._last_request_at = time.monotonic()

    # ---------- filtering ----------

    def _looks_like_announcement(
        self,
        text: str,
        author_name: str,
    ) -> bool:
        """
        Cheap prefilter run before any LLM call.

        Three gates, cheapest first:

          1. Authors with a batch label in their display name are already
             publicly announced, so their posts are product updates.

          2. Rejection, advice and hiring language is dropped outright.

          3. What remains must contain explicit acceptance language.

        Gate three is deliberately strict. Requiring it costs some recall on
        creatively worded posts, but sharply reduces false-positive LLM calls.
        """
        if not text:
            return False

        if ANNOUNCED_IN_NAME.search(author_name or ""):
            return False

        lowered = text.lower()

        for marker in NOISE_MARKERS:
            if marker in lowered:
                return False

        return any(
            marker in lowered
            for marker in ACCEPTANCE_MARKERS
        )

    # ---------- conversion ----------

    def _to_candidate(
        self,
        tweet: dict[str, Any],
        text: str,
    ) -> Candidate | None:
        """
        Build a Candidate from a raw tweet.

        company_name is left empty on purpose. The classifier extracts it
        from the post text, and Candidate.dedup_key falls back to the URL
        when no name is present, so deduplication still works meanwhile.
        """
        author = tweet.get("author", {}) or {}
        handle = author.get("userName", "")

        url = tweet.get("url", "")

        if not url and handle and tweet.get("id"):
            url = (
                f"https://x.com/{handle}/status/"
                f"{tweet['id']}"
            )

        if not url:
            return None

        return Candidate(
            company_name="",
            source=self.name,
            status=STATUS_EARLY_SIGNAL,
            url=url,
            batch=self._extract_batch(text),
            founder_name=author.get("name", ""),
            founder_handle=f"@{handle}" if handle else "",
            post_text=text[:600],

            # Provisional. The classifier overwrites this with a real score.
            confidence=0.5,

            extra={
                "tweet_id": str(tweet.get("id", "")),
                "created_at": tweet.get("createdAt", ""),
                "author_followers": author.get("followers", 0),
            },
        )

    def _extract_batch(self, text: str) -> str:
        """Pull a batch or cohort label from the post, if one is stated."""
        batch_match = BATCH_PATTERN.search(text)

        if batch_match:
            return f"YC {batch_match.group(1).upper()}"

        cohort_match = COHORT_PATTERN.search(text)

        if cohort_match:
            return f"Speedrun SR{cohort_match.group(1)}"

        return ""
