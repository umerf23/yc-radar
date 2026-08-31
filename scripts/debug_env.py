import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values, load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
env_path = PROJECT_ROOT / ".env"

print(f"PROJECT_ROOT : {PROJECT_ROOT}")
print(f"env_path     : {env_path}")
print(f"exists       : {env_path.exists()}")
print(f"size         : {env_path.stat().st_size if env_path.exists() else 'n/a'} bytes")

print("\nKeys dotenv can see in the file:")
for key in dotenv_values(env_path, encoding="utf-8-sig"):
    print(f"  {repr(key)}")

loaded = load_dotenv(
    env_path,
    encoding="utf-8-sig",
    override=True
)

print(f"\nload_dotenv returned: {loaded}")
print(
    f"os.getenv('SLACK_BOT_TOKEN') -> "
    f"{repr(os.getenv('SLACK_BOT_TOKEN'))[:40]}"
)