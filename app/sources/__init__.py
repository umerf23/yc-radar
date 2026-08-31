"""
Source registry.

To add a platform: write the module, import it here, add it to REGISTRY,
and add a matching block to config.yaml. Nothing else changes.
"""
from app.sources.base import Source
from app.sources.linkedin import LinkedInSource
from app.sources.x_twitter import XTwitterSource
from app.sources.yc_directory import YCDirectorySource
from app.sources.yc_speedrun import SpeedrunSource

REGISTRY: dict[str, type[Source]] = {
    YCDirectorySource.name: YCDirectorySource,
    SpeedrunSource.name: SpeedrunSource,
    XTwitterSource.name: XTwitterSource,
    LinkedInSource.name: LinkedInSource,

}


def build_sources(config, store) -> list[Source]:
    """Instantiate every source that is enabled in config and available."""
    sources: list[Source] = []

    for name, source_class in REGISTRY.items():
        if not config.is_source_enabled(name):
            continue

        source = source_class(config, store)

        if not source.is_available():
            print(
                f"[registry] {name} is enabled but unavailable, skipping."
            )
            continue

        sources.append(source)

    return sources
