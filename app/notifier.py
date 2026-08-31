"""
Slack delivery.

Alerts use Block Kit rather than plain text, because a GTM person acting
on these needs to scan a channel quickly and pick out the early signals.
The layout follows the structure given in the brief: company, founder,
batch, source, status, the original post, and links.

Two alert shapes:
  EARLY SIGNAL   a founder announced before the programme published them
  CONFIRMED      the programme's own directory listed a new company
"""

from datetime import UTC, datetime
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from app.models import (
    STATUS_CONFIRMED_SPEEDRUN,
    Candidate,
)

# Slack rejects section text over 3000 characters.
MAX_TEXT_LENGTH = 2900


class Notifier:
    """Posts candidates to Slack. Never raises on delivery failure."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self._client = WebClient(token=config.slack_bot_token)

    def send(self, candidate: Candidate) -> bool:
        """
        Post one alert. Returns True on success.

        A failed post must not stop the run, or one bad candidate would
        block every alert behind it. Failures are logged and skipped, and
        the candidate is left unmarked so the next run retries it.
        """
        channel = self.config.channel_for(candidate.status)

        try:
            self._client.chat_postMessage(
                channel=channel,
                text=self._fallback_text(candidate),
                blocks=self._build_blocks(candidate),
                unfurl_links=False,
                unfurl_media=False,
            )
            return True

        except SlackApiError as error:
            reason = error.response.get("error", "unknown")
            print(f"[notifier] Slack rejected the message: {reason}")
            return False

        except Exception as error:
            print(f"[notifier] delivery failed: {error}")
            return False

    def send_all(self, candidates: list[Candidate]) -> list[Candidate]:
        """
        Post every candidate, returning those that were delivered.

        Early signals are sent first. If something goes wrong partway
        through a large batch, the most valuable alerts have already
        landed.
        """
        ordered = sorted(
            candidates,
            key=lambda item: (
                not item.is_early_signal,
                -item.confidence,
            ),
        )

        delivered: list[Candidate] = []

        for candidate in ordered:
            if self.send(candidate):
                delivered.append(candidate)

        return delivered

    def send_run_summary(self, stats: dict[str, Any]) -> None:
        """
        Post a short digest when a run finds nothing.

        Silence is ambiguous: a bot that is working and a bot that has
        crashed look identical. A periodic heartbeat removes that doubt.
        """
        text = (
            f"YC Radar run complete. "
            f"{stats.get('examined', 0)} items examined, "
            f"{stats.get('new', 0)} new, "
            f"{stats.get('alerted', 0)} alerts sent."
        )

        try:
            self._client.chat_postMessage(
                channel=self.config.slack_channel_id,
                text=text,
            )

        except Exception as error:
            print(f"[notifier] summary failed: {error}")

    # ---------- message construction ----------

    def _build_blocks(self, candidate: Candidate) -> list[dict[str, Any]]:
        early = candidate.is_early_signal

        speedrun = (
            candidate.status == STATUS_CONFIRMED_SPEEDRUN
            or "speedrun" in (candidate.batch or "").lower()
        )

        if early:
            heading = (
                "EARLY SIGNAL - founder announced before official listing"
            )
        elif speedrun:
            heading = "NEW SPEEDRUN COMPANY"
        else:
            heading = "NEW YC COMPANY"

        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": heading,
                },
            }
        ]

        # Two-column field grid: compact and scannable.
        fields = [
            f"*Company*\n"
            f"{candidate.company_name or 'Not stated in post'}",
            f"*Batch*\n"
            f"{candidate.batch or 'Unknown'}",
        ]

        if candidate.founder_name or candidate.founder_handle:
            founder = candidate.founder_name or ""

            if candidate.founder_handle:
                founder = (
                    f"{founder} ({candidate.founder_handle})"
                    .strip()
                )

            fields.append(f"*Founder*\n{founder}")

        fields.append(
            f"*Source*\n{self._source_label(candidate.source)}"
        )

        if early and not candidate.extra.get("register_checked", True):
            # No company name was extracted, so the official register was
            # never queried. Say so rather than implying a verified scoop.
            status_text = (
                "Founder announced. Company not named in the post, "
                "so this was not checked against the official register"
            )
        elif early:
            status_text = "Founder announced, not yet listed officially"
        else:
            status_text = "Confirmed by the programme's directory"


        fields.append(f"*Status*\n{status_text}")

        if candidate.confidence < 1.0:
            fields.append(
                f"*Confidence*\n{candidate.confidence:.0%}"
            )

        blocks.append(
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": text,
                    }
                    for text in fields[:10]
                ],
            }
        )

        if candidate.description:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*What they do*\n"
                            f"{self._trim(candidate.description)}"
                        ),
                    },
                }
            )

        if candidate.post_text:
            quoted = "\n".join(
                f"> {line}"
                for line in self._trim(
                    candidate.post_text
                ).splitlines()
            )

            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*Original post*\n"
                            f"{quoted}"
                        ),
                    },
                }
            )

        links = []

        if candidate.url:
            label = (
                "Original post"
                if early
                else "Profile"
            )

            links.append(
                f"<{candidate.url}|{label}>"
            )

        if candidate.company_url:
            links.append(
                f"<{candidate.company_url}|Website>"
            )

        context_parts = []

        if links:
            context_parts.append(
                "  |  ".join(links)
            )

        context_parts.append(
            f"Detected {self._format_time(candidate.detected_at)}"
        )

        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "  |  ".join(context_parts),
                    }
                ],
            }
        )

        blocks.append(
            {
                "type": "divider",
            }
        )

        return blocks

    def _fallback_text(self, candidate: Candidate) -> str:
        """
        Plain-text version used in notifications and by screen readers.

        Slack requires this whenever blocks are supplied.
        """
        prefix = (
            "EARLY SIGNAL"
            if candidate.is_early_signal
            else "NEW COMPANY"
        )

        name = (
            candidate.company_name
            or candidate.founder_name
            or "Unknown"
        )

        return (
            f"{prefix}: "
            f"{name} "
            f"({candidate.batch or 'batch unknown'})"
        )

    # ---------- formatting helpers ----------

    def _source_label(self, source: str) -> str:
        return {
            "x_twitter": "X (Twitter)",
            "linkedin": "LinkedIn",
            "yc_directory": "YC Directory",
            "yc_speedrun": "Speedrun (a16z)",
        }.get(source, source)

    def _trim(self, text: str) -> str:
        if len(text) <= MAX_TEXT_LENGTH:
            return text

        return (
            text[:MAX_TEXT_LENGTH].rstrip()
            + "..."
        )

    def _format_time(self, iso_timestamp: str) -> str:
        """Render the detection time readably, falling back to the raw value."""
        try:
            moment = datetime.fromisoformat(iso_timestamp)

        except (ValueError, TypeError):
            return iso_timestamp

        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)

        return moment.strftime(
            "%d %b %Y, %H:%M UTC"
        )