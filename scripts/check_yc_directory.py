import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from app.state import Store
from app.sources.yc_directory import YCDirectorySource


config = load_config()
store = Store(config.db_path)
source = YCDirectorySource(config, store)


print(
    f"Known official companies before run: "
    f"{store.official_count()}\n"
)


candidates = source.collect()


print(
    f"\nKnown official companies after run: "
    f"{store.official_count()}"
)

print(
    f"New candidates returned: "
    f"{len(candidates)}"
)


for candidate in candidates[:5]:
    print(
        f"\n  {candidate.company_name} "
        f"({candidate.batch})"
    )

    print(
        f"  {candidate.description[:100]}"
    )

    print(
        f"  {candidate.url}"
    )