import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from app.state import Store
from app.sources.yc_speedrun import SpeedrunSource


config = load_config()
store = Store(config.db_path)
source = SpeedrunSource(config, store)

candidates = source.collect()

print(f"\nCandidates: {len(candidates)}\n")

for candidate in candidates[:10]:
    print(
        f"  {candidate.company_name} "
        f"[{candidate.batch}] "
        f"conf={candidate.confidence}"
    )
    print(f"    {candidate.url}")
    print(f"    {candidate.description[:90]}\n")