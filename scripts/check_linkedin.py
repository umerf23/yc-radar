import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from app.sources.linkedin import LinkedInSource
from app.state import Store


config = load_config()
store = Store(config.db_path)
source = LinkedInSource(config, store)

candidates = source.collect()

print(f"\n{'=' * 70}")

for candidate in candidates[:15]:
    print(
        f"\n{candidate.founder_name or '(unknown)'} "
        f"[{candidate.founder_handle}]"
    )

    if candidate.batch:
        print(f"  batch guess: {candidate.batch}")

    print(f"  {candidate.post_text[:220]}")
    print(f"  {candidate.url}")
