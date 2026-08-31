import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from app.models import STATUS_EARLY_SIGNAL, Candidate
from app.state import Store

config = load_config()
store = Store(config.db_path)


first = Candidate(
    company_name="Acme AI",
    source="x_twitter",
    status=STATUS_EARLY_SIGNAL,
    url="https://x.com/example/status/123",
    batch="S26",
)


# Same company written differently. Should collapse to the same key.
variant = Candidate(
    company_name="acme.ai",
    source="linkedin",
    status=STATUS_EARLY_SIGNAL,
    url="https://linkedin.com/posts/example",
)


print(
    f"Dedup keys match across name variants: "
    f"{first.dedup_key == variant.dedup_key}"
)


new_items = store.filter_new([first, variant])

print(
    f"New on first pass (expect 1): "
    f"{len(new_items)}"
)


for item in new_items:
    store.record(item, alerted=True)


new_items = store.filter_new([first, variant])

print(
    f"New on second pass (expect 0): "
    f"{len(new_items)}"
)


store.record_official(
    "Example Labs",
    "S26",
    "https://ycombinator.com/companies/example",
)


print(
    f"Example Labs listed by YC: "
    f"{store.is_officially_listed('Example Labs')}"
)

print(
    f"Acme AI listed by YC:      "
    f"{store.is_officially_listed('Acme AI')}"
)


store.mark_run(
    "x_twitter",
    items_found=2,
)


print(
    f"Since timestamp for x_twitter: "
    f"{store.since_timestamp('x_twitter', 10)}"
)


print(f"\nStats: {store.stats()}")