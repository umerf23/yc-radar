"""One-off helper to rewrite .env with clean UTF-8 and no BOM."""

from pathlib import Path
from dotenv import dotenv_values

path = Path(__file__).resolve().parent / ".env"

# Read existing .env, automatically removing a UTF-8 BOM if present.
values = dotenv_values(path)

# Rewrite with clean UTF-8 and no BOM.
lines = []
for key, value in values.items():
    if value is None:
        lines.append(f"{key}=")
    else:
        lines.append(f"{key}={value}")

path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

print(f"Wrote {path}")

# Verify keys without exposing secrets.
values = dotenv_values(path)

for key, value in values.items():
    shown = f"{value[:8]}..." if value else "(empty)"
    print(f"  {key} = {shown}")