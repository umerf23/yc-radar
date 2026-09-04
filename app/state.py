"""
Persistent state for the bot.

Three responsibilities:
  1. Remember every candidate ever alerted on, so nothing repeats.
  2. Maintain a register of what each programme has officially published,
     which is what makes early detection possible.
  3. Remember each source's last successful poll separately from failed
     attempts, so provider outages never create an incremental-search gap.

SQLite is used deliberately over a JSON file: it survives concurrent
writes, gives us indexed lookups as the table grows, and needs no server
for a non-technical user to install.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.models import (
    Candidate,
    _normalise,
    canonical_batch,
    programme_from_batch,
)

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
    source           TEXT PRIMARY KEY,
    last_run_at      TEXT NOT NULL,
    last_success_at  TEXT,
    items_found      INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT
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
            self._migrate_schema(conn)

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

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        """
        Upgrade older YC Radar databases in place.

        Existing deployments created source_runs before last_success_at
        existed. Successful legacy rows can be safely backfilled from
        last_run_at. Failed legacy rows are left with no success timestamp,
        which intentionally causes a full lookback on the next healthy run.
        """
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(source_runs)")
        }

        if "last_success_at" not in columns:
            conn.execute(
                "ALTER TABLE source_runs ADD COLUMN last_success_at TEXT"
            )
            conn.execute(
                """
                UPDATE source_runs
                SET last_success_at = last_run_at
                WHERE last_error IS NULL
                """
            )

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
        key = f"{_normalise(name)}|{canonical_batch(batch)}"

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

    def official_match(
        self,
        company_name: str,
        batch: str = "",
        programme: str = "",
    ) -> dict[str, str] | None:
        """
        Return the matching official-register row, or None.

        Matching is deliberately conservative:

        * A specific YC batch (e.g. YC F26) must match that exact batch.
          This protects reused names such as companies called "Remix" in
          different years.
        * A specific Speedrun cohort (e.g. SR007) must also match exactly.
        * A generic programme-only claim ("YC" or "Speedrun") may match the
          same normalised company name anywhere inside that programme.
        * With no programme evidence at all, name-only lookup is retained
          for backward compatibility.

        No fuzzy/edit-distance matching is used. A false "already listed"
        decision is just as damaging as a false early signal, so aliases are
        not guessed.
        """
        name_key = _normalise(company_name)

        if not name_key:
            return None

        batch_key = canonical_batch(batch)
        programme_key = (
            programme
            if programme in {"yc", "speedrun"}
            else programme_from_batch(batch)
        )

        with self._connect() as conn:
            # Specific cohort/batch: exact only.
            if batch_key and batch_key != "speedrun":
                row = conn.execute(
                    """
                    SELECT company_name, batch, profile_url, recorded_at
                    FROM yc_official
                    WHERE normalised_name = ?
                    """,
                    (f"{name_key}|{batch_key}",),
                ).fetchone()

                if row is None:
                    return None

                matched_programme = (
                    programme_from_batch(row["batch"])
                    or programme_key
                )

                return {
                    "company_name": row["company_name"],
                    "batch": row["batch"] or "",
                    "profile_url": row["profile_url"] or "",
                    "recorded_at": row["recorded_at"] or "",
                    "programme": matched_programme,
                    "match_type": "exact_batch",
                }

            # Generic Speedrun claim: match the same company in any
            # Speedrun cohort, including older rows stored as "speedrun".
            if programme_key == "speedrun" or batch_key == "speedrun":
                row = conn.execute(
                    """
                    SELECT company_name, batch, profile_url, recorded_at
                    FROM yc_official
                    WHERE normalised_name LIKE ?
                       OR normalised_name = ?
                    ORDER BY recorded_at DESC
                    LIMIT 1
                    """,
                    (
                        f"{name_key}|sr%",
                        f"{name_key}|speedrun",
                    ),
                ).fetchone()

                if row is None:
                    return None

                return {
                    "company_name": row["company_name"],
                    "batch": row["batch"] or "",
                    "profile_url": row["profile_url"] or "",
                    "recorded_at": row["recorded_at"] or "",
                    "programme": "speedrun",
                    "match_type": "programme_name",
                }

            # Generic YC claim: same company in any YC batch.
            if programme_key == "yc":
                row = conn.execute(
                    """
                    SELECT company_name, batch, profile_url, recorded_at
                    FROM yc_official
                    WHERE normalised_name LIKE ?
                    ORDER BY recorded_at DESC
                    LIMIT 1
                    """,
                    (f"{name_key}|yc%",),
                ).fetchone()

                if row is None:
                    return None

                return {
                    "company_name": row["company_name"],
                    "batch": row["batch"] or "",
                    "profile_url": row["profile_url"] or "",
                    "recorded_at": row["recorded_at"] or "",
                    "programme": "yc",
                    "match_type": "programme_name",
                }

            # No batch/programme was supplied. Preserve the old name-only
            # behavior, but return the row so the classifier can report
            # what actually matched.
            row = conn.execute(
                """
                SELECT company_name, batch, profile_url, recorded_at
                FROM yc_official
                WHERE normalised_name LIKE ?
                ORDER BY recorded_at DESC
                LIMIT 1
                """,
                (f"{name_key}|%",),
            ).fetchone()

        if row is None:
            return None

        return {
            "company_name": row["company_name"],
            "batch": row["batch"] or "",
            "profile_url": row["profile_url"] or "",
            "recorded_at": row["recorded_at"] or "",
            "programme": programme_from_batch(row["batch"]),
            "match_type": "name_only",
        }

    def is_officially_listed(
        self,
        company_name: str,
        batch: str = "",
    ) -> bool:
        """Backward-compatible boolean wrapper around official_match()."""
        return self.official_match(
            company_name,
            batch,
        ) is not None

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
        """
        Record the latest source attempt.

        last_run_at always records the attempt for observability.
        last_success_at advances only on a healthy source run. This prevents
        failed API calls from moving the next incremental search window
        forward and silently skipping posts.
        """
        now = datetime.now(UTC).isoformat()
        success_at = now if error is None else None

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO source_runs (
                    source,
                    last_run_at,
                    last_success_at,
                    items_found,
                    last_error
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    last_run_at = excluded.last_run_at,
                    last_success_at = CASE
                        WHEN excluded.last_error IS NULL
                        THEN excluded.last_run_at
                        ELSE source_runs.last_success_at
                    END,
                    items_found = excluded.items_found,
                    last_error = excluded.last_error
                """,
                (
                    source,
                    now,
                    success_at,
                    items_found,
                    error,
                ),
            )

    def last_run(self, source: str) -> datetime | None:
        """
        Return the last successful run time for incremental collection.

        The public method name is retained for compatibility with the source
        collectors, but failed attempts intentionally do not advance it.
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT last_success_at
                FROM source_runs
                WHERE source = ?
                """,
                (source,),
            ).fetchone()

        if not row or not row["last_success_at"]:
            return None

        return datetime.fromisoformat(row["last_success_at"])

    def since_timestamp(
        self,
        source: str,
        lookback_hours: int,
    ) -> int:
        """
        Unix timestamp marking the start of this run's search window.

        Uses the last successful run, never merely the last attempt.
        Falls back to the configured lookback window when the source has
        never completed successfully, so outages do not create blind spots.
        """
        previous = self.last_run(source)
        floor = datetime.now(UTC) - timedelta(hours=lookback_hours)

        start = max(previous, floor) if previous else floor

        return int(start.timestamp())

    def source_health(self) -> dict[str, dict[str, object]]:
        """Return the persisted latest health state for every source."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    source,
                    last_run_at,
                    last_success_at,
                    items_found,
                    last_error
                FROM source_runs
                ORDER BY source
                """
            ).fetchall()

        return {
            row["source"]: {
                "status": "ok" if row["last_error"] is None else "degraded",
                "last_run_at": row["last_run_at"],
                "last_success_at": row["last_success_at"],
                "items_found": row["items_found"],
                "error": row["last_error"],
            }
            for row in rows
        }

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
