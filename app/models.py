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

# YC batch letter codes. Spring is X because S was already taken by Summer.
SEASON_TO_CODE = {
    "winter": "w",
    "spring": "x",
    "summer": "s",
    "fall": "f",
    "autumn": "f",
}

_SPEEDRUN_COHORT = re.compile(r"\bsr[\s\-_]?(\d{1,3})\b")

_SEASON_YEAR = re.compile(
    r"\b(winter|spring|summer|fall|autumn)\s*'?\s*(\d{2}|\d{4})\b"
)

_SHORT_CODE = re.compile(r"\b([wxsf])\s*'?\s*(\d{2})\b")


def canonical_batch(raw: str) -> str:
    """
    Reduce any batch label to one comparable token.

    The YC directory publishes 'Fall 2026'. Founders write 'YC F26'.
    a16z uses 'SR007', while its own pages sometimes say only 'Speedrun'.
    Comparing those strings directly always fails, which silently turns
    every already-listed company into a false early signal.

    Returns an empty string when no batch can be identified. Callers must
    treat that as 'unknown' rather than as a match.
    """
    if not raw:
        return ""

    text = raw.lower().strip()

    cohort = _SPEEDRUN_COHORT.search(text)
    if cohort:
        return f"sr{int(cohort.group(1)):03d}"

    if "speedrun" in text:
        return "speedrun"

    season = _SEASON_YEAR.search(text)
    if season:
        return f"yc{SEASON_TO_CODE[season.group(1)]}{season.group(2)[-2:]}"

    short = _SHORT_CODE.search(text)
    if short:
        return f"yc{short.group(1)}{short.group(2)}"

    return ""

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