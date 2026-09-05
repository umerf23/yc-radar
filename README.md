# YC Radar

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/Deployment-Docker-2496ED)](https://www.docker.com/)
[![Railway](https://img.shields.io/badge/Hosted%20on-Railway-0B0D0E)](https://railway.com/)
[![Tests](https://img.shields.io/badge/tests-58%20passing-brightgreen)](#testing)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Persistent startup monitoring for GTM teams.** YC Radar watches YC company listings, a16z Speedrun, X/Twitter, and public LinkedIn founder signals, verifies whether a startup is already officially listed, deduplicates results, and sends qualified alerts to Slack.

The system is designed for the moment when timing matters most: a founder publicly announces accelerator acceptance, but the company has not yet been known to YC Radar's official-register layer before the current monitoring cycle.

---

## Live deployment

| Resource | URL |
|---|---|
| GitHub repository | https://github.com/umerf23/yc-radar |
| Production service | https://yc-radar-production.up.railway.app |
| Health endpoint | https://yc-radar-production.up.railway.app/health |
| Pond manifest | https://yc-radar-production.up.railway.app/manifest |

The production deployment runs continuously on Railway with a persistent SQLite volume.

---

## Submission overview

YC Radar was built as a **single-workspace personal Slack monitoring agent** for GTM prospecting. It monitors four signal sources, identifies new accelerator companies and founder acceptance announcements, verifies them against a persistent register, and sends actionable results to Slack.

### Requirement coverage

| Submission requirement | Implementation |
|---|---|
| Monitor YC company listings | `YCDirectorySource` polls structured YC directory data and records official companies |
| Monitor Speedrun | `SpeedrunSource` monitors the real **a16z Speedrun** site and cohort/company pages |
| Monitor X/Twitter | `XTwitterSource` uses TwitterAPI.io advanced search with incremental windows, prefiltering, pacing, and retries |
| Monitor LinkedIn | `LinkedInSource` searches publicly indexed LinkedIn posts through Serper, with optional Apify enrichment |
| Highlight founder acceptance before official listing | Social posts are classified and compared with the persistent official register; same-cycle first-seen protection prevents newly discovered directory entries from suppressing an earlier social signal |
| Persistent/incremental monitoring | SQLite stores seen candidates, official companies, source run state, first-seen timestamps, and Pond run results |
| Avoid duplicate alerts | Stable dedup keys plus persistent `alerted` state prevent re-alerting |
| Slack delivery | Qualified candidates are routed to a configured Slack channel, with optional early/confirmed channel overrides |
| Extensible architecture | Every source implements the same `Source` interface and produces the same `Candidate` model |
| Health/operational visibility | `/health` exposes run summary, source status, and persistent totals |
| Pond integration | Pond Protocol V1 `/manifest`, authenticated `/runs`, idempotency, and compatibility `/tasks/{task_id}` route |
| Deployment | Dockerized and deployed on Railway with persistent storage |
| Tests | **51 passing tests** on the final verified local baseline |

---

## Important clarification: “YC Speedrun”

The task wording refers to **“YC Speedrun.”** The `SR00x` cohorts are actually part of **a16z Speedrun**, not Y Combinator.

YC Radar therefore monitors the real programme:

```text
https://speedrun.a16z.com/companies
```

The code keeps YC and a16z Speedrun as separate programmes during matching and classification.

The internal source key remains:

```text
yc_speedrun
```

for backward compatibility with the existing configuration and persisted state, but user-facing descriptions correctly refer to **a16z Speedrun**.

---

# How YC Radar works

## High-level architecture

```text
                          +----------------------+
                          |     YC Directory     |
                          +----------+-----------+
                                     |
                          +----------v-----------+
                          |   Official register  |
                          +----------+-----------+
                                     |
                                     |
+-------------------+     +----------v-----------+     +-------------------+
|     X/Twitter     +---->|                      |<----+     LinkedIn      |
+-------------------+     |   Candidate pipeline |     +-------------------+
                          |                      |
+-------------------+     |  classify + verify   |
|  a16z Speedrun    +---->|                      |
+-------------------+     +----------+-----------+
                                     |
                            +--------v---------+
                            | Persistent dedup |
                            |     SQLite       |
                            +--------+---------+
                                     |
                            +--------v---------+
                            | Slack notifier   |
                            +------------------+

                  FastAPI /health + Pond Protocol V1
```

A single pipeline orchestrates every source. All sources return the same `Candidate` structure, so downstream classification, deduplication, Slack formatting, and persistence remain source-independent.

---

## Monitoring cycle

One cycle performs the following stages:

```text
1. Poll every enabled source
2. Record official YC / Speedrun listings
3. Collect social candidates from X and LinkedIn
4. Drop candidates already seen in SQLite
5. Classify new social candidates
6. Validate company, programme, and batch evidence
7. Compare against the official register
8. Assign EARLY_SIGNAL or confirmed status
9. Send qualified alerts to Slack
10. Persist all processed candidates and run status
```

Deduplication happens **before paid classification calls**, which avoids spending API quota on candidates the system has already processed.

---

# Early-signal detection

The core feature is not simply finding posts that mention YC. The system attempts to distinguish genuine founder acceptance announcements from already-known companies, hiring posts, advice threads, rejection posts, congratulatory posts, and other noise.

## Classification decision

```text
Founder/social post
        |
        v
Does it contain credible programme evidence?
        |
        +-- No --> reject
        |
        v
Can a usable company be identified?
        |
        +-- No --> reject
        |
        v
Does the batch / cohort evidence make sense?
        |
        +-- No --> reject
        |
        v
Was this company already known in the official
register before the current monitoring cycle?
        |
        +-- Yes --> CONFIRMED_YC / CONFIRMED_SPEEDRUN
        |
        +-- No  --> EARLY_SIGNAL
```

### Same-cycle protection

A subtle race exists because the official directory collector runs in the same monitoring cycle as X and LinkedIn.

Example:

```text
10:00  Founder publishes acceptance announcement
14:00  Company appears in directory
15:00  YC Radar monitoring cycle runs
```

Without special handling, the directory collector could write the company into the official register before the social post reaches the classifier, incorrectly turning the founder signal into a confirmed result.

YC Radar protects against this by comparing official first-seen state with the monitoring-cycle cutoff. A company first observed officially **during the current cycle** does not suppress a qualifying founder signal discovered in that same cycle.

This is intentionally conservative. `EARLY_SIGNAL` means the company was not already known to YC Radar's official-register state before the current monitoring cycle. It does **not** claim that YC Radar knows the exact publication timestamp of every official accelerator announcement.

---

# Source implementation

## 1. YC company directory

Module:

```text
app/sources/yc_directory.py
```

The collector uses structured public YC company data through the `yc-oss` mirror:

```text
https://yc-oss.github.io/api/meta.json
https://yc-oss.github.io/api/companies/all.json
```

This is a public mirror, **not an official YC API**.

### First run

On an empty database, YC Radar seeds the full YC company register silently. Existing historical companies are not sent as thousands of new Slack alerts.

### Routine runs

After the baseline exists, the collector polls recent/active batch endpoints and records newly observed companies.

Stored fields include:

- company name
- batch
- YC profile URL
- description
- website
- first-observed official timestamp

Batch labels are canonicalized so formats such as:

```text
Fall 2026
F26
YC F26
```

can be compared consistently.

---

## 2. a16z Speedrun

Module:

```text
app/sources/yc_speedrun.py
```

Speedrun's public site is monitored separately from YC.

The collector uses Serper to discover indexed pages restricted to:

```text
speedrun.a16z.com
```

It looks for:

- individual company pages
- cohort pages
- cohort markers such as `SR007` / `SR008`

Only results on the official Speedrun domain are treated as official Speedrun confirmation. Founder/social content belongs to the social-signal path instead.

Because this implementation depends on search indexing for official-domain discovery, Speedrun freshness is subject to indexing latency.

---

## 3. X / Twitter

Module:

```text
app/sources/x_twitter.py
```

X is the primary high-freshness founder-signal source.

Provider:

```text
TwitterAPI.io advanced search
```

The collector uses multiple query families for:

- first-person YC acceptance language
- current YC batch codes
- a16z Speedrun cohort terms
- broader announcement language near YC references

### Incremental search

The state layer stores the timestamp of the last successful source run. X searches use an incremental `since_time` window instead of rescanning the full history.

If a previous run failed, the next healthy run can use a wider lookback rather than advancing the success timestamp and creating a silent blind spot.

### Cheap prefiltering

Before an LLM call, obvious noise is removed, including:

- rejection posts
- application/advice posts
- hiring posts
- congratulations to somebody else
- profiles whose display name already exposes a public YC/Speedrun batch
- posts without acceptance/joining language

### Provider reliability

The X collector includes:

- request pacing
- transient retry handling
- rate-limit backoff
- server-error retry logic
- explicit auth/credit/error classification
- partial-result retention

A provider issue degrades the X source without taking down the other sources.

---

## 4. LinkedIn

Module:

```text
app/sources/linkedin.py
```

The default LinkedIn path uses **Serper** to search publicly indexed LinkedIn content through two deliberately separate signal paths.

### Founder / launch posts

The configured post searches cover:

- accepted into YC
- joining YC / Y Combinator
- active YC batch codes
- a16z Speedrun / SR cohorts
- backed-by announcements

Founder-post collection:

- validates that result URLs are actually LinkedIn URLs
- deduplicates repeated result links
- filters rejection/hiring/advice noise
- requires both acceptance and programme evidence
- extracts founder name/handle where available
- passes qualifying social candidates to the common classifier

### LinkedIn company-page signals

YC Radar also searches public `linkedin.com/company/...` results for newly discovered company pages that carry YC or a16z Speedrun evidence.

This path is intentionally separate from founder announcements:

```text
Founder acceptance post      -> EARLY_SIGNAL candidate
LinkedIn company page        -> LINKEDIN_COMPANY_SIGNAL
```

A company-page result is never described as a founder announcement merely because the page mentions an accelerator.

The company-page detector:

- accepts only canonical LinkedIn `/company/<slug>/` URLs
- normalizes subpages and query strings to one stable company URL
- strips accelerator suffixes such as `(YC F26)` from the company name for cleaner register matching and deduplication
- rejects invalid YC batch labels such as `YC P26`
- requires programme evidence tied to the company itself rather than an unrelated company, partner, embedded post, or “Similar pages” result
- treats generic a16z mentions as insufficient evidence for Speedrun
- uses the dedicated `LINKEDIN_COMPANY_SIGNAL` Slack alert shape
- relies on persistent deduplication so the same discovered page is not repeatedly alerted

A controlled local validation of the stricter filter examined 13 indexed company-page results, rejected 8 noisy/ambiguous results, and retained 5 higher-confidence signals.

### Optional Apify path

If configured, Apify can provide an additional direct collection path:

```env
APIFY_TOKEN=...
APIFY_POST_ACTOR=...
```

If Apify is absent, LinkedIn continues in Serper-only mode.

### LinkedIn limitation

Serper depends on Google indexing. Very new LinkedIn posts or company pages may not be visible immediately.

For company pages, search indexing also does **not** expose a trustworthy exact LinkedIn page-creation timestamp. YC Radar therefore describes `LINKEDIN_COMPANY_SIGNAL` as a **newly discovered / first-observed indexed company page**, not as proof of the exact moment the page was created.

Accordingly, the default LinkedIn implementation is **public indexed-content monitoring**, not guaranteed real-time LinkedIn ingestion.

---

# Classification and validation

Module:

```text
app/classifier.py
```

The current configuration uses:

```yaml
classifier:
  min_confidence: 0.7
  model: "gemini-3.5-flash-lite"
  seconds_between_calls: 2.0
```

The LLM is used to determine whether a social candidate appears to be a genuine founder/company announcement and to extract structured fields.

The model **does not get the final say** on whether something is early or confirmed. That decision is deterministic and based on the official register.

## Defensive validation

Before an alert is promoted, the classifier rejects or downgrades cases such as:

- missing company name
- generic placeholder names such as “our company”
- invalid YC batch codes
- missing YC/Speedrun programme evidence
- conflicting programme evidence
- wrong specific Speedrun cohort
- low model confidence

The implementation recognizes valid YC seasonal batch codes and Speedrun cohorts while avoiding invented batch values.

---

# Batch and programme normalization

Module:

```text
app/models.py
```

Canonical batch normalization prevents false early signals caused by different naming formats.

Examples:

```text
Fall 2026        -> ycf26
F26              -> ycf26
YC F26           -> ycf26

SR007            -> sr007
Speedrun SR007   -> sr007
Speedrun         -> speedrun
```

This matters because the official register and founder posts often express the same cohort differently.

The matching logic also preserves programme identity: YC and a16z Speedrun are never treated as interchangeable.

---

# Persistent state and deduplication

Module:

```text
app/state.py
```

YC Radar uses SQLite for durable state.

The database stores:

- previously seen candidates
- official company register
- source run state
- last successful run timestamp
- source errors
- official first-seen timestamps
- alert state
- Pond run/idempotency results

## Why persistence matters

Without persistence, every restart would rediscover the same companies and resend the same alerts.

YC Radar creates a stable candidate identity from normalized company information, with URL fallback when the social source has not yet yielded a company name.

Within a single run it also deduplicates candidates returned by overlapping search queries.

Once a candidate has been alerted, the stored alert flag is not cleared by later rediscovery.

---

# Slack delivery

YC Radar is designed for a **single Slack workspace**, not as a public Slack Marketplace application.

Environment variables:

```env
SLACK_BOT_TOKEN=xoxb-...
SLACK_CHANNEL_ID=C...
```

Optional routing in `config.yaml`:

```yaml
slack:
  early_signal_channel: ""
  confirmed_channel: ""
```

When those overrides are empty, both alert types use `SLACK_CHANNEL_ID`.

Qualified candidates carry the information needed for actionable outreach, including the company, status, source URL, programme/batch context, founder information when available, and classification context.

---

# Health and source reliability

Endpoint:

```text
GET /health
```

The health response includes:

- service status
- process start time
- most recent monitoring cycle
- number examined
- number new
- number classified
- number alerted
- number of early signals delivered in the most recent cycle
- successfully completed sources
- attempted sources
- degraded sources
- failed/skipped sources
- per-source item counts and errors
- persistent database totals

Each source can finish as:

```text
ok
degraded
failed
skipped
```

A single provider failure does not abort the entire run.

---

# Pond Protocol V1 integration

YC Radar exposes a synchronous Pond-compatible agent interface.

## Public discovery

```text
GET /manifest
```

Production:

```text
https://yc-radar-production.up.railway.app/manifest
```

Current protocol metadata:

```text
Protocol:      marketplace-agent
Version:       1.0
Agent version: 1.1.1
Sync:          true
Async tasks:   false
```

Supported actions:

```text
run_monitoring_cycle
get_monitoring_status
```

## Authenticated execution

```text
POST /runs
```

Authentication uses:

```text
Authorization: Bearer <POND_ACCESS_KEY>
```

The runtime also supports the Pond protocol-version header and idempotency key:

```text
X-Agent-Protocol-Version: 1.0
Idempotency-Key: <run_id>
```

`Idempotency-Key` must correspond to the request's `run_id`.

YC Radar stores completed Pond run responses in the same persistent SQLite database. Repeating the same run can return the saved terminal response, while trying to reuse a `run_id` for a different request produces an idempotency conflict.

## Task compatibility route

Because YC Radar executes synchronously, it advertises:

```text
async_tasks: false
```

A compatibility route still exists:

```text
GET /tasks/{task_id}
```

and returns a structured `404 task_not_found` rather than pretending an asynchronous task exists.

---

# Production deployment

YC Radar is containerized and deployed on Railway.

Production base URL:

```text
https://yc-radar-production.up.railway.app
```

The application starts:

1. the FastAPI HTTP service
2. an immediate monitoring cycle
3. the APScheduler recurring monitoring job

Default configured schedule:

```text
every 8 hours
```

This is continuous scheduled monitoring, **not second-by-second streaming**.

## Persistent Railway storage

SQLite must be on a persistent volume.

The verified Railway setup uses:

```text
Volume mount: /app/data
YC_RADAR_DB_PATH=/app/data/seen.db
```

This allows state to survive container restarts and redeployments.

---

# Verified production snapshot

Final production verification was performed on **2026-09-04** after the LinkedIn company-page detection and early-signal semantics changes were deployed.

The verified post-deploy health run reported:

```text
yc_directory   ok
yc_speedrun    ok
x_twitter      ok
linkedin       ok

4 sources ok
0 degraded
0 failed/skipped
34 candidates collected
0 new candidates
0 alerts
```

The production service restarted at `2026-09-04T17:53:56.107503+00:00`, confirming a fresh Railway deployment rather than the earlier container instance.

`0 new` was expected because the persistent database had already seen all returned candidates. This is evidence that deduplication survived the redeploy rather than evidence that the collectors were inactive.

The health endpoint reported:

```text
status:           ok
total_candidates: 58
alerted:          14
official_known:   6200
```

It also reported `early_signals: 47` in persistent totals. That value is the count of historical database records whose stored status is `EARLY_SIGNAL`; it should **not** be interpreted as “47 Slack early-signal alerts.” The persisted `alerted` total is the actual historical alert count.

---

# Configuration

Primary behavioral configuration lives in:

```text
config.yaml
```

Current important defaults:

```yaml
poll_interval_hours: 8
lookback_hours: 10

sources:
  yc_directory:
    enabled: true

  yc_speedrun:
    enabled: true
    url: "https://speedrun.a16z.com/companies"
    max_results_per_query: 10

  x_twitter:
    enabled: true

  linkedin:
    enabled: true
    max_results_per_query: 10

classifier:
  min_confidence: 0.7
  model: "gemini-3.5-flash-lite"
  seconds_between_calls: 2.0
```

Search queries and active batch/cohort terms can be changed in YAML without modifying Python source.

Secrets remain in environment variables.

---

# Environment variables

Copy the example file:

### Windows PowerShell

```powershell
Copy-Item .env.example .env
```

### Linux/macOS

```bash
cp .env.example .env
```

Then provide your own credentials.

| Variable | Required | Purpose |
|---|---:|---|
| `SLACK_BOT_TOKEN` | Yes | Slack bot authentication |
| `SLACK_CHANNEL_ID` | Yes | Default destination channel |
| `TWITTERAPI_KEY` | Required for X | TwitterAPI.io access |
| `SERPER_KEY` | Required for Speedrun + default LinkedIn path | Serper search access |
| `LLM_PROVIDER` | Required for configured LLM classification | Provider selection |
| `LLM_API_KEY` | Required for configured LLM classification | LLM API key |
| `APIFY_TOKEN` | No | Optional LinkedIn enrichment |
| `APIFY_POST_ACTOR` | No | Optional Apify post actor |
| `APIFY_COMPANY_ACTOR` | No | Optional/reserved company actor config |
| `POND_ACCESS_KEY` | Required for authenticated Pond `/runs` | Bearer access key |
| `YC_RADAR_DB_PATH` | Recommended in hosted deployment | Persistent SQLite file location |
| `PORT` | Hosting-platform dependent | HTTP port; defaults to `8000` locally |

Do **not** commit `.env` or real credentials.

---

# Local installation

## 1. Clone

```bash
git clone https://github.com/umerf23/yc-radar.git
cd yc-radar
```

## 2. Create a virtual environment

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### Linux/macOS

```bash
python -m venv .venv
source .venv/bin/activate
```

## 3. Install dependencies

```bash
pip install -r requirements.txt
```

## 4. Configure environment

Windows:

```powershell
Copy-Item .env.example .env
```

Linux/macOS:

```bash
cp .env.example .env
```

Edit `.env` and provide your own API keys/tokens.

## 5. Run a single monitoring cycle

```bash
python -m app.main --once
```

## 6. Run continuously

```bash
python -m app.main
```

Expected startup behavior:

```text
[main] scheduled every 8 hours.
Uvicorn running ...
=== run started ...
```

The first monitoring cycle begins immediately; the scheduler then continues at the configured interval.

---

# Docker

Build and start:

```bash
docker compose up --build
```

Run in the background:

```bash
docker compose up -d --build
```

View logs:

```bash
docker compose logs -f
```

Trigger a one-off cycle:

```bash
docker compose run --rm yc-radar python -m app.main --once
```

The container entry point must run:

```text
python -m app.main
```

rather than importing the FastAPI app directly, because the scheduler is started by `main()`.

---

# API endpoints

## `GET /`

Service discovery:

```json
{
  "service": "yc-radar",
  "health": "/health",
  "manifest": "/manifest",
  "runs": "/runs",
  "tasks": "/tasks/{task_id}"
}
```

## `GET /health`

Public operational status.

Example:

```bash
curl https://yc-radar-production.up.railway.app/health
```

## `GET /manifest`

Public Pond agent discovery document.

```bash
curl https://yc-radar-production.up.railway.app/manifest
```

## `POST /runs`

Authenticated Pond action execution.

Do not put a real access key in source control. Example request headers:

```text
Authorization: Bearer <POND_ACCESS_KEY>
X-Agent-Protocol-Version: 1.0
Idempotency-Key: <RUN_ID>
Content-Type: application/json
```

---

# Testing

Final verified local baseline:

```text
58 passed
```

Run:

```bash
ruff check app scripts tests
pytest -q
git diff --check
```

The test suite covers behavior including:

- company normalization
- canonical YC batch matching
- Speedrun cohort matching
- wrong-cohort rejection
- deduplication
- official-register matching
- placeholder company rejection
- invalid YC batch rejection
- missing programme evidence
- social-to-official classification
- same-cycle early-signal behavior
- LinkedIn company-page URL normalization
- LinkedIn related-company/noise rejection
- invalid LinkedIn company-page YC batch rejection
- separation of company-page signals from founder early signals
- source-state reliability
- Pond behavior
- core pipeline behavior

A clean submission baseline should show:

```text
All checks passed!
58 passed
```

with no `git diff --check` errors.

---

# Project structure

```text
yc-radar/
├── app/
│   ├── __init__.py
│   ├── classifier.py
│   ├── config.py
│   ├── main.py
│   ├── models.py
│   ├── notifier.py
│   ├── pipeline.py
│   ├── state.py
│   └── sources/
│       ├── __init__.py
│       ├── base.py
│       ├── linkedin.py
│       ├── x_twitter.py
│       ├── yc_directory.py
│       └── yc_speedrun.py
├── scripts/
├── tests/
│   └── test_core.py
├── config.yaml
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── ruff.toml
├── .env.example
├── .gitignore
├── LICENSE
└── README.md
```

---

# Failure isolation and recovery

YC Radar is deliberately designed to degrade by source rather than fail as one monolithic scraper.

Examples:

```text
YC works, X rate-limited
-> YC/Speedrun/LinkedIn results still complete
-> X is marked degraded
-> run remains observable

LinkedIn provider unavailable
-> other sources continue
-> source status records the error

X authentication failure
-> X stops wasting calls for that cycle
-> persistent source error is visible in /health
```

A failed run does not falsely advance the successful incremental timestamp for a source.

---

# Security notes

- Secrets belong in `.env` locally and hosting-provider environment variables in production.
- `.env` should never be committed.
- Pond `/runs` is protected with a bearer access key.
- Pond request reuse is guarded by persisted request hashing/idempotency.
- Slack and provider credentials should be rotated immediately if exposed in logs, screenshots, commits, or shell history.
- The public `/health` and `/manifest` endpoints do not require provider secrets.
- Avoid printing environment variables or `docker compose config` output when recording a public demo, because those commands can expose credentials.

---

# Known limitations

A professional monitoring system should state its limits explicitly.

### 1. Polling is not streaming

The current production schedule runs every 8 hours. YC Radar is therefore a continuously scheduled monitor, not a millisecond or second-by-second event stream.

### 2. LinkedIn has indexing latency

The default LinkedIn source relies on publicly indexed search results. New posts may not appear until the search engine indexes them.

### 3. Speedrun discovery has indexing latency

a16z Speedrun official-site monitoring uses indexed official-domain pages rather than a dedicated structured API.

### 4. YC data source is a mirror

The YC collector uses the public `yc-oss` mirror of YC company data. It should not be described as an official YC API.

### 5. “Early” is based on observed register state

`EARLY_SIGNAL` means the company was not already in YC Radar's official register before the current monitoring cycle. It is not a claim that the system independently knows the exact moment every accelerator made a public announcement.

### 6. External APIs can degrade coverage

TwitterAPI.io, Serper, Gemini, Slack, and optional Apify are external dependencies. Provider outages, exhausted credits, authentication errors, or rate limits can temporarily reduce coverage. Source health makes those failures visible instead of silently presenting incomplete results as fully healthy.

---

# Extending YC Radar

Adding another source follows the existing source contract:

1. Create a module under `app/sources/`.
2. Subclass `Source`.
3. Implement `collect() -> list[Candidate]`.
4. Add the class to the source registry in `app/sources/__init__.py`.
5. Add a matching configuration block in `config.yaml`.
6. Add tests.

The classifier, notifier, persistence layer, and pipeline do not need source-specific rewrites when the new collector produces valid `Candidate` objects.

---

# Why this is useful for GTM

The differentiator is not another static startup database. It is **timing plus filtering**.

A founder who has just announced accelerator acceptance may be entering a period of:

- company formation and banking decisions
- infrastructure purchases
- recruiting
- tooling selection
- vendor evaluation
- fundraising preparation
- increased outbound activity

YC Radar turns scattered public signals into a persistent, deduplicated Slack feed so a GTM operator can find those companies without manually refreshing multiple platforms.

---

# Final submission status

The final verified implementation has:

```text
Four monitored sources:       yes
YC directory monitoring:      healthy
a16z Speedrun monitoring:     healthy
X/Twitter monitoring:         healthy
LinkedIn post monitoring:     healthy
LinkedIn company pages:       validated
Persistent SQLite state:      working
Duplicate suppression:        working
Same-cycle early detection:   tested
Slack integration:            tested
Pond Protocol V1:             live
Railway production deploy:    live
Source health reporting:      live
Ruff:                         pass
Tests:                        58 passed
```

Production health:

```text
https://yc-radar-production.up.railway.app/health
```

Pond manifest:

```text
https://yc-radar-production.up.railway.app/manifest
```

---

## License

MIT. See [LICENSE](LICENSE).
