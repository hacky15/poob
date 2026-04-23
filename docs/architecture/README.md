---
type: moc
status: active
tags: [index]
---

# Architecture — Map of Content

Subsystem-level descriptions: shape, invariants, key files. One note per subsystem. These notes are what someone new to the code reads to build a mental model before digging into `src/`.

Update in place when the architecture changes. Link to decision notes that drove the change.

## Entries

### Voice / Brain / Music

- [[poobbrain-architecture]] — unified personality + deal sub-agent, tool routing
- [[voice-architecture]] — dual pipeline, Toob, deferred playback, cascade
- [[music-player-architecture]] — yt-dlp + FFmpeg + audioop PCM mixer, four-layer anti-stutter

### Scanner / Marketplace

- [[facebook-scrolling-and-listing-volume]] — GraphQL + DOM paths, scroll, depersonalization, rate limits
- [[vlm-triage-pipeline]] — triage + VLM + savings enforcement, watchlist bypass, price display
- [[listing-freshness-verification]] — timestamp sources, distributions, FB-filter violations
- [[unified-filter-pipeline]] — `FilterChain` predicate composition, geo, tag exemptions
- [[garbage-listing-filter]] — UI artifacts + unenrichable placeholders
- [[deal-provenance-trail]] — evaluation details in notification embeds
