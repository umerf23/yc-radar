"""
The single data structure that flows through the whole pipeline.

Every source, no matter how different its raw API response, converts its
findings into a Candidate. State, classification, and Slack formatting all
work on Candidate objects and never touch source-specific formats.

That separation is what lets a new platform be added without changing
anything downstream of the source module itself.
"""

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

# Alert statuses.
# EARLY_SIGNAL is the one this project exists for:
# a founder has announced but YC has not confirmed them yet.
STATUS_EARLY_SIGNAL = "EARLY_SIGNAL"
STATUS_CONFIRMED_YC = "CONFIRMED_YC"
STATUS_CONFIRMED_SPEEDRUN = "CONFIRMED_SPEEDRUN"


def _normalise(text: str) -> str:
    """
    Reduce a company name to a comparable form for deduplication.

    Only legal suffixes are stripped. An earlier version also removed
    tokens like 'ai', 'io' and 'app', which silently merged 118 distinct
    YC companies into shared keys during directory seeding.

    Those words are part of real company names now, so they stay.

    Punctuation removal alone already handles the important case:
    'Acme AI' and 'acme.ai' both reduce to 'acmeai'.
    """
    lowered = text.lower().strip()

    # Company-form suffixes only. These are never the distinguishing part
    # of a name, so removing them is safe.
    lowered = re.sub(
        r"\b(inc|llc|ltd|corp|co)\b",
        "",
        lowered,
    )

    # Keep letters and digits, drop everything else.
    return re.sub(
        r"[^a-z0-9]",
        "",
        lowered,
    )


@dataclass
class Candidate:
    """A possible new YC or Speedrun company, from any source."""

    company_name: str
    source: str
    # "yc_directory", "x_twitter", etc.

    status: str
    # One of the STATUS_ constants.

    url: str = ""
    # Link to the post or YC profile.

    batch: str = ""
    # e.g. "Summer 2026", "Speedrun"

    founder_name: str = ""

    founder_handle: str = ""

    description: str = ""

    company_url: str = ""

    post_text: str = ""
    # Original post, quoted in the alert.

    confidence: float = 1.0
    # 1.0 for official sources.

    detected_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )

    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        """
        Stable identity for this company across sources and runs.

        Keyed on the normalised company name rather than the URL, so the
        same company found first on X and later in the YC directory is
        recognised as one thing rather than two alerts.
        """
        basis = _normalise(self.company_name)

        if not basis:
            # Fall back to the URL when no usable name was extracted,
            # so a nameless candidate still dedupes against itself.
            basis = self.url

        return hashlib.sha256(
            basis.encode("utf-8")
        ).hexdigest()[:16]

    @property
    def is_early_signal(self) -> bool:
        return self.status == STATUS_EARLY_SIGNAL

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)