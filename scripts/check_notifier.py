import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent),
)

from app.config import load_config
from app.models import (
    STATUS_CONFIRMED_YC,
    STATUS_EARLY_SIGNAL,
    Candidate,
)
from app.notifier import Notifier


config = load_config()
notifier = Notifier(config)


early = Candidate(
    company_name="Redoubt Insurance",
    source="linkedin",
    status=STATUS_EARLY_SIGNAL,
    url="https://www.linkedin.com/posts/andre-beukers_example",
    batch="YC Fall 2026",
    founder_name="Andre Beukers",
    description=(
        "A commercial insurance company that automates underwriting."
    ),
    post_text=(
        "Thrilled to share that Redoubt Insurance is joining "
        "Y Combinator's Fall 2026 batch!"
    ),
    confidence=1.0,
)


confirmed = Candidate(
    company_name="Example Labs",
    source="yc_directory",
    status=STATUS_CONFIRMED_YC,
    url="https://www.ycombinator.com/companies/example",
    batch="Fall 2026",
    description="AI agents for logistics companies.",
    company_url="https://example.com",
    confidence=1.0,
)


for candidate in (early, confirmed):
    ok = notifier.send(candidate)

    print(
        f"{candidate.company_name}: "
        f"{'sent' if ok else 'FAILED'}"
    )