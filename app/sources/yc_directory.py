import re
from datetime import UTC, datetime
from typing import Any

import requests

from app.models import STATUS_CONFIRMED_YC, Candidate
from app.sources.base import Source

# Only batches from this year or later are polled on routine runs.
# Older batches never gain members, so re-fetching them wastes bandwidth.
ACTIVE_BATCH_MIN_YEAR = datetime.now(UTC).year - 1

META_URL = "https://yc-oss.github.io/api/meta.json"
ALL_COMPANIES_URL = "https://yc-oss.github.io/api/companies/all.json"

REQUEST_TIMEOUT = 60



class YCDirectorySource(Source):
    name = "yc_directory"

    def collect(self) -> list[Candidate]:
        try:
            if self.store.official_count() == 0:
                return self._seed_full_directory()

            return self._poll_active_batches()

        except requests.RequestException as error:
            # Never raise. A dead source must not stop the other three.
            print(f"[{self.name}] network error: {error}")
            return []

        except Exception as error:
            print(f"[{self.name}] unexpected error: {error}")
            return []

    # ---------- first run ----------

    def _seed_full_directory(self) -> list[Candidate]:
        """
        Load every known YC company into yc_official without alerting.

        This baseline is what makes early detection meaningful: a founder
        post can only be 'ahead of YC' if we know what YC has published.
        """
        print(
            f"[{self.name}] first run detected, "
            "seeding full directory..."
        )

        response = requests.get(
            ALL_COMPANIES_URL,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        companies = response.json()

        for company in companies:
            parsed = self._parse(company)

            if parsed["name"]:
                self.store.record_official(
                    parsed["name"],
                    parsed["batch"],
                    parsed["profile_url"],
                )

        print(
            f"[{self.name}] seeded {len(companies)} companies. "
            "No alerts on first run."
        )

        return []

    # ---------- routine runs ----------

    def _poll_active_batches(self) -> list[Candidate]:
        """Check only recent batches for companies we have not recorded yet."""
        response = requests.get(
            META_URL,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        meta = response.json()

        batch_urls = self._active_batch_urls(meta)

        print(
            f"[{self.name}] polling "
            f"{len(batch_urls)} active batches."
        )

        candidates: list[Candidate] = []

        for batch_name, url in batch_urls:
            try:
                batch_response = requests.get(
                    url,
                    timeout=REQUEST_TIMEOUT,
                )
                batch_response.raise_for_status()

                companies = batch_response.json()

            except requests.RequestException as error:
                print(
                    f"[{self.name}] could not fetch batch "
                    f"{batch_name}: {error}"
                )
                continue

            for company in companies:
                parsed = self._parse(company)

                if not parsed["name"]:
                    continue

                # Name plus batch, so a 2026 company reusing an old name
                # is correctly treated as new.
                if self.store.is_officially_listed(
                    parsed["name"],
                    parsed["batch"],
                ):
                    continue

                self.store.record_official(
                    parsed["name"],
                    parsed["batch"],
                    parsed["profile_url"],
                )

                candidates.append(
                    Candidate(
                        company_name=parsed["name"],
                        source=self.name,
                        status=STATUS_CONFIRMED_YC,
                        url=parsed["profile_url"],
                        batch=parsed["batch"],
                        description=parsed["description"],
                        company_url=parsed["website"],
                        confidence=1.0,
                    )
                )

        return candidates

    # ---------- helpers ----------

    def _active_batch_urls(
        self,
        meta: dict[str, Any],
    ) -> list[tuple[str, str]]:
        """
        Pick batch endpoints worth polling.

        Batch names look like 'Summer 2026' or 'Winter 2027'. Anything
        older than ACTIVE_BATCH_MIN_YEAR is settled and skipped.
        """
        selected: list[tuple[str, str]] = []

        for key, info in meta.get("batches", {}).items():
            name = info.get("name", key)

            year_match = re.search(
                r"(20\d{2})",
                name,
            )

            if not year_match:
                continue

            if int(year_match.group(1)) < ACTIVE_BATCH_MIN_YEAR:
                continue

            api_url = info.get("api")

            if api_url:
                selected.append(
                    (name, api_url)
                )

        return selected

    def _parse(
        self,
        company: dict[str, Any],
    ) -> dict[str, str]:
        """
        Normalise one API record.

        Field names are read defensively with fallbacks, because this is an
        unofficial mirror and its schema is not contractually stable.
        """
        name = (
            company.get("name") or ""
        ).strip()

        slug = (
            company.get("slug") or ""
        ).strip()

        profile_url = (
            company.get("url") or ""
        ).strip()

        if not profile_url and slug:
            profile_url = (
                "https://www.ycombinator.com/"
                f"companies/{slug}"
            )

        description = (
            company.get("one_liner")
            or company.get("oneLiner")
            or company.get("long_description")
            or ""
        ).strip()

        return {
            "name": name,
            "batch": (
                company.get("batch") or ""
            ).strip(),
            "profile_url": profile_url,
            "website": (
                company.get("website") or ""
            ).strip(),
            "description": description[:300],
        }
