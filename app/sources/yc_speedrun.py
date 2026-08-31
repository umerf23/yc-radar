"""
Speedrun watcher.

Important context, also documented in the README: Speedrun is a16z's
accelerator, not a YC sub-program. It launched in 2023, runs cohorts
labelled SR001 onward, and lives at speedrun.a16z.com. The task brief
describes it as a YC program; this module monitors the real one.

Unlike YC, Speedrun publishes no open API and no machine-readable
directory. Its Sanity CMS holds only marketing pages, and its company
listing is rendered client-side from a source that is not publicly
queryable. So this module works from search indexing instead.

That is not a downgrade. Speedrun's site listing lags founder
announcements badly, which means social and search are where the early
signal actually lives, exactly the signal this bot exists to catch.
"""

import re
from typing import Any

import requests

from app.models import STATUS_CONFIRMED_SPEEDRUN, Candidate
from app.sources.base import Source

SERPER_URL = "https://google.serper.dev/search"
REQUEST_TIMEOUT = 30

# Only results on this host are treated as directory listings.
# Cohort searches also surface X profiles and news coverage, which belong
# to the early-detection path rather than the confirmed-company path.
OFFICIAL_HOST = "speedrun.a16z.com"

# Cohort labels follow SR001, SR002 and so on.
COHORT_PATTERN = re.compile(r"\bSR(\d{3})\b", re.IGNORECASE)

# Titles on speedrun.a16z.com company pages tend to read:
# "CompanyName | Speedrun" or "Speedrun - CompanyName".
TITLE_CLEAN_PATTERN = re.compile(
    r"\s*[|\-]\s*speedrun.*$|^\s*speedrun\s*[|\-]\s*",
    re.IGNORECASE,
)

# Search results titled "Someone's Post" are social content, not company
# pages. They are handled by the early-detection path instead.
POST_TITLE_PATTERN = re.compile(
    r"'s Post\b|\bPost\s*$",
    re.IGNORECASE,
)


class SpeedrunSource(Source):
    name = "yc_speedrun"

    def is_available(self) -> bool:
        """Serper is the only dependency. Without it this source is skipped."""
        return bool(self.config.serper_key)

    def collect(self) -> list[Candidate]:
        if not self.is_available():
            print(f"[{self.name}] no SERPER_KEY set, skipping.")
            return []

        candidates: list[Candidate] = []

        try:
            candidates.extend(self._find_company_pages())
            candidates.extend(self._find_cohort_announcements())

        except Exception as error:
            # Never raise. One dead source must not stop the run.
            print(f"[{self.name}] unexpected error: {error}")

        # A first run has no baseline, so every indexed page looks new and
        # the entire existing portfolio would be alerted at once. Seed the
        # register silently instead, matching the YC directory's behaviour.
        if self.store is not None and self.store.official_count() == 0:
            for candidate in candidates:
                if candidate.company_name:
                    self.store.record_official(
                        candidate.company_name,
                        candidate.batch,
                        candidate.url,
                    )

            print(
                f"[{self.name}] first run, seeded {len(candidates)} companies. "
                "No alerts."
            )

            return []

        print(f"[{self.name}] found {len(candidates)} candidates.")

        return candidates

    # ---------- strategy one: indexed company pages ----------

    def _find_company_pages(self) -> list[Candidate]:
        """
        Look for company pages on the Speedrun site itself.

        Restricting to the past month keeps the result set to pages that
        have appeared or been refreshed recently, which approximates a
        directory diff without a directory API.
        """

        results = self._search(
            f"site:{OFFICIAL_HOST}/companies",
            time_filter="qdr:m",
            limit=self.settings.get("max_results_per_query", 10),
        )

        candidates: list[Candidate] = []

        for item in results:
            link = item.get("link", "")

            # Skip the index page itself; we only want individual companies.
            if not link or link.rstrip("/").endswith("/companies"):
                continue

            name = self._company_name_from(item)

            if not name:
                continue

            candidates.append(
                Candidate(
                    company_name=name,
                    source=self.name,
                    status=STATUS_CONFIRMED_SPEEDRUN,
                    url=link,
                    batch="Speedrun",
                    description=item.get("snippet", "")[:300],
                    confidence=0.9,
                    extra={"program": "a16z Speedrun"},
                )
            )

        return candidates

    # ---------- strategy two: cohort announcements ----------

    def _find_cohort_announcements(self) -> list[Candidate]:
        """
        Catch cohort roster pages naming a specific SR cohort.

        Results are restricted to the official host. Cohort searches also
        return founder posts and press coverage, but those arrive through
        the X and LinkedIn collectors as early signals, where they belong.
        Treating them as confirmed listings here would mislabel them.
        """

        queries = [
            f'site:{OFFICIAL_HOST} ("SR007" OR "SR008")',
            f"site:{OFFICIAL_HOST}/companies cohort",
        ]

        candidates: list[Candidate] = []

        for query in queries:
            for item in self._search(
                query,
                time_filter="qdr:w",
                limit=10,
            ):
                link = item.get("link", "")

                # Belt and braces: the site: operator is not always honoured.
                if OFFICIAL_HOST not in link:
                    continue

                name = self._company_name_from(item)

                if not name:
                    continue

                snippet = item.get("snippet", "")

                cohort_match = COHORT_PATTERN.search(
                    f"{item.get('title', '')} {snippet}"
                )

                cohort = (
                    f"Speedrun SR{cohort_match.group(1)}"
                    if cohort_match
                    else "Speedrun"
                )

                candidates.append(
                    Candidate(
                        company_name=name,
                        source=self.name,
                        status=STATUS_CONFIRMED_SPEEDRUN,
                        url=link,
                        batch=cohort,
                        description=snippet[:300],
                        confidence=0.8,
                        extra={"program": "a16z Speedrun"},
                    )
                )

        return candidates

    # ---------- helpers ----------

    def _search(
        self,
        query: str,
        time_filter: str = "",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Run one Serper query. Returns an empty list on any failure."""

        payload: dict[str, Any] = {
            "q": query,
            "num": limit,
        }

        if time_filter:
            payload["tbs"] = time_filter

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

    def _company_name_from(self, item: dict[str, Any]) -> str:
        """
        Extract a company name from a search result.

        Speedrun company URLs look like /companies/<company>/<founder>,
        so the company is the first path segment after /companies/, never
        the last.

        Falls back to cleaning the page title, and returns an empty string
        when neither yields something plausible so that junk never reaches
        Slack.
        """

        link = item.get("link", "")

        if "/companies/" in link:
            tail = link.rstrip("/").split("/companies/")[-1]

            if tail:
                # First segment is the company; anything after is a founder.
                slug = tail.split("/")[0]

                if slug:
                    return slug.replace("-", " ").title()

        title = TITLE_CLEAN_PATTERN.sub(
            "",
            item.get("title", ""),
        ).strip()

        # Reject social post titles outright. These are real signals, but
        # the company name lives in the post body, not the title, so the
        # classifier extracts it rather than this function guessing.
        if POST_TITLE_PATTERN.search(title):
            return ""

        # Reject anything reading like an article headline.
        if (
            not title
            or len(title) > 40
            or len(title.split()) > 5
        ):
            return ""

        return title