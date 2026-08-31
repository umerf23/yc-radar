"""
Persistent state for the bot.

Three responsibilities:
  1. Remember every candidate ever alerted on, so nothing repeats.
  2. Maintain a register of what each programme has officially published,
     which is what makes early detection possible.
  3. Remember when each source last ran, so each poll fetches only what
     is new rather than re-scanning the same window forever.

SQLite is used deliberately over a JSON file: it survives concurrent
writes, gives us indexed lookups as the table grows, and needs no server
for a non-technical user to install.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.models import Candidate, _normalise

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_candidates (
    dedup_key       TEXT PRIMARY KEY,
    company_name    TEXT NOT NULL,
    source          TEXT NOT NULL,
    status          TEXT NOT NULL,
    batch           TEXT,
    url             TEXT,
    founder_handle  TEXT,
    confidence      REAL,
    alerted         INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_status ON seen_candidates(status);
CREATE INDEX IF NOT EXISTS idx_company ON seen_candidates(company_name);

CREATE TABLE IF NOT EXISTS source_runs (
    source        TEXT PRIMARY KEY,
    last_run_at   TEXT NOT NULL,
    items_found   INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT
);

CREATE TABLE IF NOT EXISTS yc_official (
    normalised_name TEXT PRIMARY KEY,
    company_name    TEXT NOT NULL,
    batch           TEXT,
    profile_url     TEXT,
    recorded_at     TEXT NOT NULL
);
"""


class Store:
    """All database access goes through this class."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a connection, commit on success, always close."""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row

        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------- candidate deduplication ----------

    def has_seen(self, candidate: Candidate) -> bool:
        """True if this company has been recorded before, from any source."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM seen_candidates WHERE dedup_key = ?",
                (candidate.dedup_key,),
            ).fetchone()

        return row is not None

    def record(self, candidate: Candidate, alerted: bool = False) -> None:
        """
        Insert a candidate under its own derived key.

        Use record_with_key instead when the candidate may have been
        mutated since it was filtered.
        """
        self.record_with_key(
            candidate.dedup_key,
            candidate,
            alerted,
        )

    def record_with_key(
        self,
        dedup_key: str,
        candidate: Candidate,
        alerted: bool = False,
    ) -> None:
        """
        Record a candidate under an explicit key.

        Needed because classification fills in company_name, and
        dedup_key is derived from that name. The key used when filtering
        must be the key stored, or the same item is treated as new on
        every run.

        The ON CONFLICT clause never clears the alerted flag, so a
        company rediscovered later cannot trigger a second notification.
        """
        now = datetime.now(UTC).isoformat()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO seen_candidates (
                    dedup_key,
                    company_name,
                    source,
                    status,
                    batch,
                    url,
                    founder_handle,
                    confidence,
                    alerted,
                    first_seen_at,
                    last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dedup_key) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    company_name = excluded.company_name,
                    alerted = MAX(
                        seen_candidates.alerted,
                        excluded.alerted
                    )
                """,
                (
                    dedup_key,
                    candidate.company_name,
                    candidate.source,
                    candidate.status,
                    candidate.batch,
                    candidate.url,
                    candidate.founder_handle,
                    candidate.confidence,
                    1 if alerted else 0,
                    now,
                    now,
                ),
            )

    def filter_new(self, candidates: list[Candidate]) -> list[Candidate]:
        """
        Return only candidates never seen before.

        Deduplicates within the batch too, since the same company often
        appears in several search queries during a single run.
        """
        fresh: list[Candidate] = []
        seen_this_run: set[str] = set()

        for candidate in candidates:
            key = candidate.dedup_key

            if key in seen_this_run or self.has_seen(candidate):
                continue

            seen_this_run.add(key)
            fresh.append(candidate)

        return fresh

    # ---------- official register ----------

    # This is what makes early detection possible: we can only claim a
    # founder announced "before YC" if we know what YC has already listed.

    def record_official(
        self,
        name: str,
        batch: str,
        profile_url: str,
    ) -> None:
        """
        Record a company as officially listed by its programme.

        Keyed on name plus batch, because YC reuses short names across
        years. 'Remix' from 2015 and 'Remix' from 2026 are different
        companies, and collapsing them would suppress alerts for the
        newer one.
        """
        key = f"{_normalise(name)}|{_normalise(batch)}"

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO yc_official (
                    normalised_name,
                    company_name,
                    batch,
                    profile_url,
                    recorded_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(normalised_name) DO UPDATE SET
                    batch = excluded.batch,
                    profile_url = excluded.profile_url
                """,
                (
                    key,
                    name,
                    batch,
                    profile_url,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def is_officially_listed(
        self,
        company_name: str,
        batch: str = "",
    ) -> bool:
        """
        True if a programme directory lists this company.

        With a batch supplied, the check is exact. Without one, it matches
        the name against any batch. Social posts often name a batch, so
        pass it when available for a precise answer.
        """
        name_key = _normalise(company_name)

        if not name_key:
            return False

        with self._connect() as conn:
            if batch:
                row = conn.execute(
                    """
                    SELECT 1
                    FROM yc_official
                    WHERE normalised_name = ?
                    """,
                    (f"{name_key}|{_normalise(batch)}",),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT 1
                    FROM yc_official
                    WHERE normalised_name LIKE ?
                    """,
                    (f"{name_key}|%",),
                ).fetchone()

        return row is not None

    def official_count(self) -> int:
        """Return the number of officially known companies."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM yc_official"
            ).fetchone()[0]

    # ---------- run bookkeeping ----------

    def mark_run(
        self,
        source: str,
        items_found: int,
        error: str | None = None,
    ) -> None:
        """Record the latest execution of a source."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO source_runs (
                    source,
                    last_run_at,
                    items_found,
                    last_error
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    last_run_at = excluded.last_run_at,
                    items_found = excluded.items_found,
                    last_error = excluded.last_error
                """,
                (
                    source,
                    datetime.now(UTC).isoformat(),
                    items_found,
                    error,
                ),
            )

    def last_run(self, source: str) -> datetime | None:
        """When this source last ran, or None if it never has."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT last_run_at
                FROM source_runs
                WHERE source = ?
                """,
                (source,),
            ).fetchone()

        return (
            datetime.fromisoformat(row["last_run_at"])
            if row
            else None
        )

    def since_timestamp(
        self,
        source: str,
        lookback_hours: int,
    ) -> int:
        """
        Unix timestamp marking the start of this run's search window.

        Falls back to the full lookback window on a first run or after a
        gap, which is why lookback_hours is set wider than the poll
        interval: a delayed or failed run must not create a blind spot.
        """
        previous = self.last_run(source)
        floor = datetime.now(UTC) - timedelta(hours=lookback_hours)

        start = max(previous, floor) if previous else floor

        return int(start.timestamp())

    def stats(self) -> dict[str, int]:
        """Summary counts, used by the health endpoint."""
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM seen_candidates"
            ).fetchone()[0]

            alerted = conn.execute(
                """
                SELECT COUNT(*)
                FROM seen_candidates
                WHERE alerted = 1
                """
            ).fetchone()[0]

            early = conn.execute(
                """
                SELECT COUNT(*)
                FROM seen_candidates
                WHERE status = 'EARLY_SIGNAL'
                """
            ).fetchone()[0]

        return {
            "total_candidates": total,
            "alerted": alerted,
            "early_signals": early,
            "official_known": self.official_count(),
        }
