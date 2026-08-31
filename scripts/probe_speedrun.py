"""
Probe the a16z Speedrun site's backend to find the cleanest data path.

The companies page is client-rendered from Sanity CMS (project tzetulnq,
dataset production). Sanity datasets are often publicly readable, which
would give us structured JSON instead of a brittle HTML parser.

This script answers three questions:
  1. Is the dataset public?
  2. What document types exist?
  3. What fields does a company record have?
"""

import json

import requests


SANITY_BASE = (
    "https://tzetulnq.api.sanity.io/"
    "v2021-10-21/data/query/production"
)
TIMEOUT = 30


def query(groq: str) -> dict | None:
    """Run one GROQ query. Returns None on any failure, with the reason printed."""
    try:
        response = requests.get(
            SANITY_BASE,
            params={"query": groq},
            timeout=TIMEOUT,
        )
    except requests.RequestException as error:
        print(f"  request failed: {error}")
        return None

    if response.status_code != 200:
        print(f"  HTTP {response.status_code}: {response.text[:200]}")
        return None

    return response.json()


print("1. Checking whether the dataset is publicly readable...")

result = query("count(*)")

if result is None:
    print(
        "\nDataset is not public. "
        "We will use the HTML or search fallback instead."
    )
    raise SystemExit(0)

print(f"  Public. Total documents: {result.get('result')}\n")


print("2. Listing document types...")

result = query("array::unique(*[]._type)")
types = result.get("result", []) if result else []

print(f"  {types}\n")


print("3. Sampling a likely company type...")

# Try the most plausible type names until one returns records.
for candidate_type in [
    "company",
    "portfolioCompany",
    "companies",
    "startup",
    "founder",
]:
    result = query(
        f'*[_type == "{candidate_type}"][0...2]'
    )

    records = result.get("result", []) if result else []

    if records:
        print(
            f"  Type '{candidate_type}' returned "
            f"{len(records)} sample records."
        )

        print(
            f"  Fields: {sorted(records[0].keys())}\n"
        )

        print("  First record:")
        print(
            json.dumps(
                records[0],
                indent=2,
            )[:1500]
        )

        break

else:
    print(
        "  None of the guessed type names matched. "
        "Use the type list above."
    )