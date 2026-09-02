"""
LinkedIn collector.

LinkedIn publishes no public search API, and scraping it directly gets
accounts restricted. So this module works through the search index instead,
querying Google via Serper for public posts on linkedin.com.

That approach has a real cost, stated plainly: results are limited to what
Google has indexed, so very recent posts may be missed, and the snippet
returned is often truncated mid-sentence. In exchange it needs no LinkedIn
credentials, costs nothing on Serper's free tier, and cannot get anyone's
account banned.

An optional Apify branch is included for higher-volume direct scraping.
It activates only when APIFY_TOKEN is set and degrades silently to
search-only when it is absent, so the bot runs fully without it.
"""

import re
from typing import Any
from urllib.parse import urlparse

import requests

from app.models import STATUS_EARLY_SIGNAL, Candidate
from app.sources.base import Source

SERPER_URL = "https://google.serper.dev/search"
APIFY_BASE = "https://api.apify.com/v2/acts"
REQUEST_TIMEOUT = 30
APIFY_TIMEOUT = 180

# Batch codes such as "S26", and Speedrun cohorts such as "SR007".
BATCH_PATTERN = re.compile(r"\bYC\s*([WSXF]\d{2})\b", re.IGNORECASE)
COHORT_PATTERN = re.compile(r"\bSR(\d{3})\b", re.IGNORECASE)

# LinkedIn post URLs embed the author's public identifier:
# linkedin.com/posts/<vanity-name>_<slugified-post-text>-activity-<id>
AUTHOR_FROM_URL = re.compile(
    r"linkedin\.com/posts/([^_/?]+)",
    re.IGNORECASE,
)

# Search result titles read "Firstname Lastname's Post" or
# "Firstname Lastname on LinkedIn: ...".
NAME_FROM_TITLE = re.compile(
    r"^(.*?)(?:'s Post\b|\s+on LinkedIn\b)",
    re.IGNORECASE,
)

# Language that means the post is not a first-person acceptance.
NOISE_MARKERS = (
    "congrat",
    "rejected",
    "rejection",
    "didn't get in",
    "did not get in",
    "didn't make it",
    "applications open",
    "apply to yc",
    "how to get into",
    "tips for",
    "we're hiring",
    "we are hiring",
    "now hiring",
    "open roles",
)

# At least one must appear. Kept broader than the X equivalent because
# Serper snippets are truncated, so the acceptance phrase is sometimes
# cut off even when the post itself contains it.
# An acceptance phrase alone is not enough. The post must also name
# YC or Speedrun so unrelated accelerator announcements are filtered out.
PROGRAMME_MARKERS = (
    "y combinator",
    "ycombinator",
    "yc s2",
    "yc w2",
    "yc f2",
    "yc x2",
    " yc ",
    "(yc",
    "speedrun",
    "a16z",
)

ACCEPTANCE_MARKERS = (
    "accepted into",
    "accepted to",
    "got into",
    "joining y combinator",
    "joining yc",
    "part of yc",
    "part of y combinator",
    "backed by y combinator",
    "backed by yc",
    "a16z speedrun",
    "joining speedrun",
    "backed by a16z",
)


class LinkedInSource(Source):
    name = "linkedin"

    def is_available(self) -> bool:
        """Serper is the minimum requirement. Apify is optional on top."""
        return bool(self.config.serper_key)

    def collect(self) -> list[Candidate]:
        if not self.is_available():
            print(f"[{self.name}] no SERPER_KEY set, skipping.")
            return []

        candidates = self._collect_via_search()

        # Optional enrichment layer. Absent credentials are not an error.
        if self.config.apify_enabled:
            candidates.extend(self._collect_via_apify())
        else:
            print(f"[{self.name}] Apify not configured, using search only.")

        return candidates

    # ---------- primary path: search index ----------

    def _collect_via_search(self) -> list[Candidate]:
        queries = self.settings.get("queries", [])
        limit = self.settings.get("max_results_per_query", 10)

        print(f"[{self.name}] running {len(queries)} search queries.")

        candidates: list[Candidate] = []
        seen_links: set[str] = set()
        examined = 0
        filtered = 0
        invalid_links = 0

        for query in queries:
            for item in self._search(query, limit):
                link = item.get("link", "")

                if not link or link in seen_links:
                    continue

                seen_links.add(link)
                examined += 1

                # Serper occasionally exposes an opaque Google result token
                # instead of the destination URL. Such a token cannot produce
                # the bounty-required original LinkedIn post link, so skip it
                # rather than sending a broken Slack alert.
                if not self._is_linkedin_url(link):
                    invalid_links += 1
                    continue

                snippet = item.get("snippet", "")
                title = item.get("title", "")

                if not self._looks_like_announcement(snippet, title):
                    filtered += 1
                    continue

                candidates.append(
                    self._to_candidate(
                        item,
                        link,
                        title,
                        snippet,
                    )
                )

        print(
            f"[{self.name}] examined {examined} results, "
            f"prefilter dropped {filtered}, "
            f"invalid links {invalid_links}, "
            f"kept {len(candidates)}."
        )

        return candidates

    def _search(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Run one Serper query. Returns an empty list on any failure."""
        payload = {
            "q": query,
            "num": limit,
            "tbs": "qdr:w",
        }

        headers = {
            "X-API-KEY": self.config.serper_key,
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(
                SERPER_URL,
                json=payload,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            print(
                f"[{self.name}] search failed for "
                f"'{query[:40]}': {error}"
            )
            return []

        return response.json().get("organic", [])

    # ---------- optional path: Apify ----------

    def _collect_via_apify(self) -> list[Candidate]:
        """
        Direct post scraping through a rented Apify actor.

        Actor input schemas vary between providers, so the payload is
        read from config rather than hardcoded. Failures here never
        affect the search path, which has already returned its results.
        """
        actor = self.config.apify_post_actor
        url = f"{APIFY_BASE}/{actor}/run-sync-get-dataset-items"
        params = {"token": self.config.apify_token}

        payload = self.settings.get(
            "apify_input",
            {
                "searchQuery": "Y Combinator accepted",
                "maxItems": 25,
            },
        )

        try:
            response = requests.post(
                url,
                params=params,
                json=payload,
                timeout=APIFY_TIMEOUT,
            )
            response.raise_for_status()
            items = response.json()

        except requests.RequestException as error:
            print(f"[{self.name}] Apify run failed: {error}")
            return []

        except ValueError:
            print(
                f"[{self.name}] Apify returned an unreadable response."
            )
            return []

        candidates: list[Candidate] = []

        for item in items:
            text = item.get("text") or item.get("content") or ""
            link = item.get("url") or item.get("postUrl") or ""

            if not text or not self._is_linkedin_url(link):
                continue

            if not self._looks_like_announcement(text, ""):
                continue

            candidates.append(
                Candidate(
                    company_name="",
                    source=self.name,
                    status=STATUS_EARLY_SIGNAL,
                    url=link,
                    batch=self._extract_batch(text),
                    founder_name=item.get("authorName", ""),
                    post_text=text[:600],
                    confidence=0.5,
                    extra={"via": "apify"},
                )
            )

        print(
            f"[{self.name}] Apify contributed "
            f"{len(candidates)} candidates."
        )

        return candidates

    # ---------- filtering ----------

    def _looks_like_announcement(
        self,
        snippet: str,
        title: str,
    ) -> bool:
        """
        Cheap prefilter, same principle as the X module.

        Deliberately more permissive here, because Serper snippets are
        truncated and the decisive phrase is often cut off. Precision is
        recovered by the classifier, which reads the full text.
        """
        combined = f"{title} {snippet}".lower()

        if not combined.strip():
            return False

        for marker in NOISE_MARKERS:
            if marker in combined:
                return False

        has_acceptance = any(
            marker in combined
            for marker in ACCEPTANCE_MARKERS
        )
        has_programme = any(
            marker in combined
            for marker in PROGRAMME_MARKERS
        )

        return has_acceptance and has_programme

    # ---------- conversion ----------

    def _to_candidate(
        self,
        item: dict[str, Any],
        link: str,
        title: str,
        snippet: str,
    ) -> Candidate:
        """
        Build a Candidate from one search result.

        company_name stays empty for the classifier to fill. Founder name
        is taken from the result title where possible, since Serper gives
        no structured author field.
        """
        return Candidate(
            company_name="",
            source=self.name,
            status=STATUS_EARLY_SIGNAL,
            url=link,
            batch=self._extract_batch(
                f"{title} {snippet}"
            ),
            founder_name=self._author_name(title),
            founder_handle=self._author_handle(link),
            post_text=snippet[:600],
            # Lower than X, reflecting the thinner evidence: a truncated
            # snippet rather than the full post text.
            confidence=0.4,
            extra={
                "via": "serper",
                "title": title,
            },
        )

    def _is_linkedin_url(self, link: str) -> bool:
        """Return True only for absolute HTTP(S) LinkedIn URLs."""
        try:
            parsed = urlparse(link)
        except ValueError:
            return False

        if parsed.scheme not in {"http", "https"}:
            return False

        host = (parsed.hostname or "").lower()
        return host == "linkedin.com" or host.endswith(".linkedin.com")

    def _author_name(self, title: str) -> str:
        """Pull a person's name from the search result title."""
        match = NAME_FROM_TITLE.match(title or "")

        if not match:
            return ""

        name = match.group(1).strip()

        # Reject anything too long to be a personal name.
        return name if 0 < len(name) <= 60 else ""

    def _author_handle(self, link: str) -> str:
        """Derive the LinkedIn vanity identifier from the post URL."""
        match = AUTHOR_FROM_URL.search(link or "")

        return match.group(1) if match else ""

    def _extract_batch(self, text: str) -> str:
        """Pull a batch or cohort label from the text, if one is stated."""
        batch_match = BATCH_PATTERN.search(text)

        if batch_match:
            return f"YC {batch_match.group(1).upper()}"

        cohort_match = COHORT_PATTERN.search(text)

        if cohort_match:
            return f"Speedrun SR{cohort_match.group(1)}"

        return ""