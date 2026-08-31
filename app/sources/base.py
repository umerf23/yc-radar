"""
The contract every source must satisfy.

Adding a new platform means writing one class here that inherits Source,
implementing collect(), and registering it in app/sources/__init__.py.

No other file needs to change. This is what makes the bot upgradable:
supporting Reddit or Product Hunt later is one new file plus one config
block, not a rewrite.

Sources receive the Store because some of them need to diff against what
is already known. The YC directory watcher, for example, can only tell a
newly listed company from an old one by consulting the official register.
"""

from abc import ABC, abstractmethod
from typing import Any

from app.models import Candidate


class Source(ABC):
    """Base class for every monitored platform."""

    # Must match the key used in config.yaml under 'sources'.
    name: str = "unnamed"

    def __init__(
        self,
        config: Any,
        store: Any = None,
    ) -> None:
        """
        Args:
            config: the loaded Config object.
            store: the Store instance, or None for sources that are
                   purely stateless collectors.
        """
        self.config = config
        self.store = store
        self.settings = config.source_config(self.name)

    @abstractmethod
    def collect(self) -> list[Candidate]:
        """
        Fetch from this platform and return Candidate objects.

        Implementations must not raise on network errors. Log the problem,
        return an empty list, and let the other sources finish their run.

        One dead API should never take down the whole bot.
        """
        raise NotImplementedError

    def is_available(self) -> bool:
        """
        Whether this source can actually run right now.

        Override when a source depends on a credential that may be absent,
        so the orchestrator can skip it cleanly instead of failing mid-run.
        """
        return True

    def __repr__(self) -> str:
        return f"<Source {self.name}>"