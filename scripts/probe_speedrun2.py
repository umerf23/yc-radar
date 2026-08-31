"""
Second probe.

The company list is not a top-level Sanity type, so check whether it hides
inside 'person' records or a page's content blocks.
"""

import json

import requests


SANITY_BASE = (
    "https://tzetulnq.api.sanity.io/"
    "v2021-10-21/data/query/production"
)

TIMEOUT = 30


def query(groq: str):
    """Run a GROQ query and return its result."""
    try:
        response = requests.get(
            SANITY_BASE,
            params={"query": groq},
            timeout=TIMEOUT,
        )

        if response.status_code != 200:
            print(f"  HTTP {response.status_code}")
            return None

        return response.json().get("result")

    except requests.RequestException as error:
        print(f"  request failed: {error}")
        return None


print("1. How many of each type?")

for doc_type in [
    "person",
    "page",
    "demoDay",
    "settings",
    "faq",
]:
    count = query(
        f'count(*[_type == "{doc_type}"])'
    )

    print(f"  {doc_type:<12} {count}")


print("\n2. Sample 'person' record (founders may carry company data):")

people = query(
    '*[_type == "person"][0...2]'
)

if people:
    print(
        f"  Fields: {sorted(people[0].keys())}"
    )

    print(
        json.dumps(
            people[0],
            indent=2,
        )[:1200]
    )


print("\n3. Page slugs (looking for a companies page):")

pages = query(
    '*[_type == "page"]{ "slug": slug.current, title }'
)

print(f"  {pages}")


print("\n4. demoDay records (cohorts may be listed here):")

demo = query(
    '*[_type == "demoDay"][0...1]'
)

if demo:
    print(
        f"  Fields: {sorted(demo[0].keys())}"
    )

    print(
        json.dumps(
            demo[0],
            indent=2,
        )[:1500]
    )