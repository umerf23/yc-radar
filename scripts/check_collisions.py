"""
Find which company names collapse to the same normalised key.
"""

import sys
from collections import defaultdict
from pathlib import Path

import requests

sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent),
)

from app.models import _normalise

ALL_COMPANIES_URL = (
    "https://yc-oss.github.io/api/companies/all.json"
)


print("Fetching full directory...")

response = requests.get(
    ALL_COMPANIES_URL,
    timeout=60,
)

response.raise_for_status()

companies = response.json()

print(
    f"Records: {len(companies)}\n"
)


groups: dict[str, list[str]] = defaultdict(list)

empty_names = 0


for company in companies:
    name = (company.get("name") or "").strip()

    if not name:
        empty_names += 1
        continue

    batch = company.get("batch") or "?"

    groups[_normalise(name)].append(
        f"{name} [{batch}]"
    )


collisions = {
    key: names
    for key, names in groups.items()
    if len(names) > 1
}

lost = sum(
    len(names) - 1
    for names in collisions.values()
)


print(
    f"Unique keys:        {len(groups)}"
)

print(
    f"Records with no name: {empty_names}"
)

print(
    f"Colliding key groups: {len(collisions)}"
)

print(
    f"Records absorbed:     {lost}\n"
)


print("First 25 collisions:")

for key, names in list(collisions.items())[:25]:
    print(
        f"  {key:<24} <- {names}"
    )