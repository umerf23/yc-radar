import os

import requests
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("SERPER_KEY")
if not api_key:
    raise SystemExit("SERPER_KEY is missing. Check your .env file.")

# Site-restricted search finds LinkedIn posts without needing LinkedIn credentials.
# "qdr:w" limits results to the past week, which suits incremental polling.
payload = {
    "q": 'site:linkedin.com/posts ("accepted into Y Combinator" OR "joining Y Combinator")',
    "tbs": "qdr:w",
    "num": 10,
}
headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}

response = requests.post(
    "https://google.serper.dev/search",
    json=payload,
    headers=headers,
    timeout=30,
)
if response.status_code != 200:
    raise SystemExit(f"Request failed with status {response.status_code}: {response.text[:300]}")

results = response.json().get("organic", [])
print(f"Returned {len(results)} results.\n")

for item in results[:5]:
    print(item.get("title", "No title"))
    print(f"  {item.get('snippet', '')[:160]}")
    print(f"  {item.get('link', '')}\n")