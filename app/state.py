"""
Persistent state for the bot.

Four responsibilities:
  1. Remember every candidate ever classified, so collection work is not
     repeated.
  2. Maintain a register of what each programme has officially published,
     which is what makes early detection possible.
  3. Remember each source's last successful poll separately from failed
     attempts, so provider outages never create an incremental-search gap.
  4. Track Slack delivery per workspace, so every workspace receives each
     qualified lead once without suppressing delivery to other workspaces.

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

CREATE TABLE IF NOT EXISTS slack_installations (
    team_id             TEXT PRIMARY KEY,
    team_name           TEXT NOT NULL,
    channel_id          TEXT NOT NULL DEFAULT '',
    channel_name        TEXT NOT NULL DEFAULT '',
    webhook_encrypted   TEXT NOT NULL DEFAULT '',
    bot_token_encrypted TEXT NOT NULL DEFAULT '',
    bot_user_id         TEXT NOT NULL DEFAULT '',
    installed_at        TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    last_manual_run_at  TEXT
);

CREATE TABLE IF NOT EXISTS slack_deliveries (
    team_id       TEXT NOT NULL,
    dedup_key     TEXT NOT NULL,
    channel_id    TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    PRIMARY KEY (team_id, dedup_key)
);

CREATE INDEX IF NOT EXISTS idx_slack_deliveries_team
ON slack_deliveries(team_id);
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

        slack_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(slack_installations)")
        }
        if "bot_token_encrypted" not in slack_columns:
            conn.execute(
                "ALTER TABLE slack_installations "
                "ADD COLUMN bot_token_encrypted TEXT NOT NULL DEFAULT ''"
            )
        if "bot_user_id" not in slack_columns:
            conn.execute(
                "ALTER TABLE slack_installations "
                "ADD COLUMN bot_user_id TEXT NOT NULL DEFAULT ''"
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

    def recent_candidates(
        self,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        """Return the newest candidate signals for the operator dashboard."""
        safe_limit = max(1, min(limit, 250))

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
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
                FROM seen_candidates
                WHERE alerted = 1
                ORDER BY first_seen_at DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()

        return [
            {
                **dict(row),
                "alerted": bool(row["alerted"]),
            }
            for row in rows
        ]

    def pending_slack_candidates(
        self,
        team_id: str,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        """Return qualified leads not yet delivered to one Slack workspace."""
        safe_limit = max(1, min(limit, 500))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    candidate.dedup_key,
                    candidate.company_name,
                    candidate.source,
                    candidate.status,
                    candidate.batch,
                    candidate.url,
                    candidate.founder_handle,
                    candidate.confidence,
                    candidate.alerted,
                    candidate.first_seen_at,
                    candidate.last_seen_at
                FROM seen_candidates AS candidate
                LEFT JOIN slack_deliveries AS delivery
                  ON delivery.team_id = ?
                 AND delivery.dedup_key = candidate.dedup_key
                WHERE candidate.alerted = 1
                  AND delivery.dedup_key IS NULL
                ORDER BY candidate.first_seen_at ASC
                LIMIT ?
                """,
                (team_id, safe_limit),
            ).fetchall()
        return [{**dict(row), "alerted": True} for row in rows]

    def record_slack_delivery(
        self,
        team_id: str,
        dedup_key: str,
        channel_id: str,
    ) -> None:
        """Mark one lead delivered only after Slack accepts the message."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO slack_deliveries (
                    team_id, dedup_key, channel_id, delivered_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (team_id, dedup_key, channel_id, datetime.now(UTC).isoformat()),
            )

    def pending_slack_count(self, team_id: str) -> int:
        """Count qualified leads still waiting for one Slack workspace."""
        with self._connect() as conn:
            return int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM seen_candidates AS candidate
                    LEFT JOIN slack_deliveries AS delivery
                      ON delivery.team_id = ?
                     AND delivery.dedup_key = candidate.dedup_key
                    WHERE candidate.alerted = 1
                      AND delivery.dedup_key IS NULL
                    """,
                    (team_id,),
                ).fetchone()[0]
            )

    # ---------- Slack OAuth installations ----------

    def save_slack_installation(
        self,
        team_id: str,
        team_name: str,
        channel_id: str = "",
        channel_name: str = "",
        webhook_encrypted: str = "",
        bot_token_encrypted: str = "",
        bot_user_id: str = "",
    ) -> None:
        """Persist or refresh one workspace's Slack OAuth installation."""
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO slack_installations (
                    team_id, team_name, channel_id, channel_name,
                    webhook_encrypted, bot_token_encrypted, bot_user_id,
                    installed_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id) DO UPDATE SET
                    team_name = excluded.team_name,
                    channel_id = CASE
                        WHEN excluded.channel_id <> '' THEN excluded.channel_id
                        ELSE slack_installations.channel_id
                    END,
                    channel_name = CASE
                        WHEN excluded.channel_name <> '' THEN excluded.channel_name
                        ELSE slack_installations.channel_name
                    END,
                    webhook_encrypted = CASE
                        WHEN excluded.webhook_encrypted <> ''
                        THEN excluded.webhook_encrypted
                        ELSE slack_installations.webhook_encrypted
                    END,
                    bot_token_encrypted = CASE
                        WHEN excluded.bot_token_encrypted <> ''
                        THEN excluded.bot_token_encrypted
                        ELSE slack_installations.bot_token_encrypted
                    END,
                    bot_user_id = CASE
                        WHEN excluded.bot_user_id <> '' THEN excluded.bot_user_id
                        ELSE slack_installations.bot_user_id
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    team_id,
                    team_name,
                    channel_id,
                    channel_name,
                    webhook_encrypted,
                    bot_token_encrypted,
                    bot_user_id,
                    now,
                    now,
                ),
            )

    def update_slack_channel(
        self,
        team_id: str,
        channel_id: str,
        channel_name: str,
    ) -> bool:
        """Set the destination chosen after OAuth for one workspace."""
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE slack_installations
                SET channel_id = ?, channel_name = ?, updated_at = ?
                WHERE team_id = ?
                """,
                (channel_id, channel_name, now, team_id),
            )
        return cursor.rowcount == 1

    def slack_installations(self) -> list[dict[str, object]]:
        """Return installed workspaces for scheduled multi-tenant delivery."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT team_id, team_name, channel_id, channel_name,
                       webhook_encrypted, bot_token_encrypted, bot_user_id,
                       installed_at, updated_at, last_manual_run_at
                FROM slack_installations
                ORDER BY installed_at
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def slack_installation(self, team_id: str) -> dict[str, object] | None:
        """Return one installed workspace without decrypting its webhook."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT team_id, team_name, channel_id, channel_name,
                       webhook_encrypted, bot_token_encrypted, bot_user_id,
                       installed_at, updated_at,
                       last_manual_run_at
                FROM slack_installations
                WHERE team_id = ?
                """,
                (team_id,),
            ).fetchone()
        return dict(row) if row else None

    def claim_slack_run(self, team_id: str, cooldown_seconds: int = 300) -> bool:
        """Atomically enforce a short cooldown on manual provider scans."""
        now = datetime.now(UTC)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT last_manual_run_at
                FROM slack_installations
                WHERE team_id = ?
                """,
                (team_id,),
            ).fetchone()
            if row is None:
                return False
            if row["last_manual_run_at"]:
                previous = datetime.fromisoformat(row["last_manual_run_at"])
                if (now - previous).total_seconds() < cooldown_seconds:
                    return False
            conn.execute(
                """
                UPDATE slack_installations
                SET last_manual_run_at = ?, updated_at = ?
                WHERE team_id = ?
                """,
                (now.isoformat(), now.isoformat(), team_id),
            )
        return True

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
                  AND alerted = 1
                """
            ).fetchone()[0]

        return {
            "total_candidates": total,
            "alerted": alerted,
            "early_signals": early,
            "official_known": self.official_count(),
        }
