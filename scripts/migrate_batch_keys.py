"""
One-time migration: rewrite yc_official keys to canonical batch tokens.

Only the key column changes. company_name and batch are preserved, so
the migration is re-runnable and reversible from the same source columns.
"""

import sqlite3
import sys
from pathlib import Path
from app.config import load_config 
DB_PATH = load_config().db_path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import _normalise, canonical_batch  # noqa: E402

DB_PATH = Path("data/seen.db")


def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit(
            f"No database at {DB_PATH}. Nothing to migrate."
        )

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT normalised_name, company_name, batch, profile_url,"
        " recorded_at FROM yc_official"
    ).fetchall()

    print(f"read {len(rows)} rows")

    rewritten = []
    unrecognised = 0

    for row in rows:
        batch_key = canonical_batch(row["batch"])

        if not batch_key:
            unrecognised += 1

        key = f"{_normalise(row['company_name'])}|{batch_key}"

        rewritten.append(
            (
                key,
                row["company_name"],
                row["batch"],
                row["profile_url"],
                row["recorded_at"],
            )
        )

    distinct = len({item[0] for item in rewritten})

    print(f"{distinct} distinct keys after migration")
    print(f"{len(rows) - distinct} rows collapsed into shared keys")
    print(f"{unrecognised} rows had an unrecognisable batch label")

    conn.execute("DELETE FROM yc_official")

    conn.executemany(
        "INSERT OR REPLACE INTO yc_official (normalised_name,"
        " company_name, batch, profile_url, recorded_at)"
        " VALUES (?, ?, ?, ?, ?)",
        rewritten,
    )

    conn.commit()
    conn.close()

    print("migration complete")


if __name__ == "__main__":
    main()