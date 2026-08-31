import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from app.state import Store
from app.sources.x_twitter import XTwitterSource

config = load_config()
store = Store(config.db_path)
source = XTwitterSource(config, store)

candidates = source.collect()

print(f"\n{'=' * 70}")
for candidate in candidates[:15]:
    print(f"\n{candidate.founder_name} ({candidate.founder_handle})")
    if candidate.batch:
        print(f"  batch guess: {candidate.batch}")
    print(f"  {candidate.post_text[:220]}")
    print(f"  {candidate.url}")
"""
Manual test for the X collector.

Passing --hours widens the search window regardless of stored state,
which matters because repeated test runs push last_run forward and leave
almost nothing inside the incremental window.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from app.state import Store
from app.sources.x_twitter import XTwitterSource


# Default to a week so a test run has something to find.
hours = 168

if len(sys.argv) > 1:
    hours = int(sys.argv[1])


config = load_config()
config.lookback_hours = hours

store = Store(config.db_path)


class TestStore:
    """
    Wraps the real store but forces a wide search window.

    Everything else, including dedup behaviour, is left untouched so the
    test exercises the real code path.
    """

    def __init__(self, inner: Store, hours: int) -> None:
        self._inner = inner
        self._hours = hours

    def since_timestamp(self, source: str, lookback_hours: int) -> int:
        from datetime import datetime, timedelta, timezone

        start = datetime.now(timezone.utc) - timedelta(hours=self._hours)

        return int(start.timestamp())

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


source = XTwitterSource(
    config,
    TestStore(store, hours),
)


print(f"Searching the last {hours} hours.\n")

candidates = source.collect()


print(f"\n{'=' * 70}")

for candidate in candidates[:15]:
    print(
        f"\n{candidate.founder_name} "
        f"({candidate.founder_handle})"
    )

    if candidate.batch:
        print(f"  batch guess: {candidate.batch}")

    print(f"  {candidate.post_text[:220]}")
    print(f"  {candidate.url}")
