---
type: decision
status: active
date: 2026-03-30
tags: [search, infra, deploy]
related: [[free-search-providers]]
---

# Search provider cascade — Tavily → Serper → SearXNG → empty

## Context

The scanner and brain both need web-search results (RetailLookupTool, research cascade). Paid APIs are off the table; free tiers run out fast under patrol load.

## Decision

Cascade through three providers, falling back to empty results rather than erroring out:

1. **Tavily** — 1,000/month free tier. Burned through quickly during backfill; now effectively exhausted.
2. **Serper.dev** — 2,500 one-time free credits. Exhausted.
3. **SearXNG** — self-hosted Docker container, unlimited. Current primary.
4. Empty fallback if all three fail — the caller handles "no search results" gracefully.

## SearXNG deployment

- Runs as a service in [deploy/compose.yml](../../deploy/compose.yml) alongside scraper + ollama, single Komodo stack.
- On homelab: scraper reaches it via service-name DNS — set `SEARXNG_BASE_URL=http://searxng:8080` in the Komodo stack env.
- Local dev: `docker compose -f deploy/compose.yml up -d searxng` exposes `http://localhost:8080`. Default `searxng_base_url` in [config.py](../../src/poob/config.py) already points there; no override needed.
- Gracefully skips if the container is not running (ConnectError → `unavailable`), caller falls through to the next provider.

## Alternatives considered

- **SerpAPI** — 250 searches/month free. Hit 429s within the first patrol cycle. See [[serpapi]].
- **Paid search** — deliberately out of scope for now.

## Consequences

- SearXNG is now load-bearing. If the container stops, search quality drops to empty results.
- Monitoring: the provider-cascade logs which provider answered each query; watch for SearXNG unavailability in production logs.
- If the upstream search engines SearXNG proxies change their anti-bot behavior, SearXNG queries can return 403/429. Self-hosted doesn't mean infinite — it means no *API* quota.
