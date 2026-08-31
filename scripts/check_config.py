import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config

config = load_config()

print("Config loaded successfully.\n")
print(f"Poll interval:    {config.poll_interval_hours} hours")
print(f"Lookback window:  {config.lookback_hours} hours")
print(f"Database path:    {config.db_path}")
print(f"Classifier:       {'enabled' if config.classifier_enabled else 'disabled'}")
print(f"Apify:            {'enabled' if config.apify_enabled else 'disabled (using Serper)'}")
print("\nSources:")
for name in config.sources:
    state = "on " if config.is_source_enabled(name) else "off"
    print(f"  [{state}] {name}")