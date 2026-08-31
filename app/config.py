"""
Loads configuration from .env and config.yaml, validates it, and exposes
a single Config object to the rest of the app.

Design note: secrets live in .env, behaviour lives in config.yaml.
That split means a user can retune the bot without touching secrets,
and can share config.yaml publicly without leaking anything.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Project root is one level above this file's directory (app/).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load .env explicitly by path so the app works no matter which
# directory it is launched from. This is a common source of confusion.
load_dotenv(PROJECT_ROOT / ".env", encoding="utf-8-sig", override=True)


@dataclass
class Config:
    """Everything the app needs to run, resolved and validated."""

    # Secrets from .env
    slack_bot_token: str
    slack_channel_id: str
    twitterapi_key: str
    serper_key: str
    llm_provider: str
    llm_api_key: str
    apify_token: str
    apify_post_actor: str
    apify_company_actor: str

    # Behaviour from config.yaml
    poll_interval_hours: int
    lookback_hours: int
    sources: dict[str, Any] = field(default_factory=dict)
    slack_routing: dict[str, str] = field(default_factory=dict)
    classifier: dict[str, Any] = field(default_factory=dict)

    # Paths
    db_path: Path = PROJECT_ROOT / "data" / "seen.db"

    def is_source_enabled(self, name: str) -> bool:
        """True only if the source exists in config and is switched on."""
        return bool(self.sources.get(name, {}).get("enabled", False))

    def source_config(self, name: str) -> dict[str, Any]:
        """Settings for one source. Returns an empty dict if absent."""
        return self.sources.get(name, {})

    def channel_for(self, status: str) -> str:
        """
        Pick the Slack channel for a given alert status, falling back to
        the default channel when no override is configured.
        """
        key = "early_signal_channel" if status == "EARLY_SIGNAL" else "confirmed_channel"
        return self.slack_routing.get(key) or self.slack_channel_id

    @property
    def apify_enabled(self) -> bool:
        """Apify is optional. Absence disables it rather than breaking the run."""
        return bool(self.apify_token and self.apify_post_actor)

    @property
    def classifier_enabled(self) -> bool:
        """Without an LLM key the app falls back to keyword-only filtering."""
        return self.llm_provider != "none" and bool(self.llm_api_key)


def _require(name: str) -> str:
    """Read a mandatory environment variable or fail with a clear message."""
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(
            f"Missing required setting '{name}'.\n"
            f"Copy .env.example to .env and fill it in, then try again."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    """Read an optional environment variable, stripped of stray whitespace."""
    return os.getenv(name, default).strip()


def load_config(yaml_path: Path | None = None) -> Config:
    """Build the Config object. Raises SystemExit with a readable message on error."""
    path = yaml_path or (PROJECT_ROOT / "config.yaml")

    if not path.exists():
        raise SystemExit(f"Config file not found at {path}.")

    with open(path, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    config = Config(
        slack_bot_token=_require("SLACK_BOT_TOKEN"),
        slack_channel_id=_require("SLACK_CHANNEL_ID"),
        twitterapi_key=_optional("TWITTERAPI_KEY"),
        serper_key=_optional("SERPER_KEY"),
        llm_provider=_optional("LLM_PROVIDER", "none").lower(),
        llm_api_key=_optional("LLM_API_KEY"),
        apify_token=_optional("APIFY_TOKEN"),
        apify_post_actor=_optional("APIFY_POST_ACTOR"),
        apify_company_actor=_optional("APIFY_COMPANY_ACTOR"),
        poll_interval_hours=int(raw.get("poll_interval_hours", 8)),
        lookback_hours=int(raw.get("lookback_hours", 10)),
        sources=raw.get("sources", {}),
        slack_routing=raw.get("slack", {}),
        classifier=raw.get("classifier", {}),
    )

    # Warn rather than crash when an enabled source has no credentials.
    # A partial run is more useful than no run at all.
    if config.is_source_enabled("x_twitter") and not config.twitterapi_key:
        print("Warning: x_twitter is enabled but TWITTERAPI_KEY is empty. It will be skipped.")
    if config.is_source_enabled("linkedin") and not config.serper_key:
        print("Warning: linkedin is enabled but SERPER_KEY is empty. It will be skipped.")

    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    return config