---
type: reference
status: superseded
date: 2026-03-10
tags: [search, rate-limits]
source: "serpapi.com"
superseded_by: [[search-provider-cascade]]
related: [[search-provider-cascade]]
---

# SerpAPI

## Summary

Paid Google-search-as-a-service. Offered a free tier (250/month) that was burned through within a single patrol cycle. No longer in the cascade — see [[search-provider-cascade]].

## Key facts

- Free tier: 250 searches / month.
- Gets rate-limited quickly during patrol cycles with many listings.
- 429 on first overage; we disable the provider for the session on first 429.

## Status

Effectively dead for our use case. Keep the adapter only for the case where someone puts a key in and explicitly enables it; don't route to it by default.

## Source

[serpapi.com](https://serpapi.com)
