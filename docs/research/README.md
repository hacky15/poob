---
type: moc
status: active
tags: [index]
---

# Research — Map of Content

Open-ended investigations. A research note captures the question, the findings, the evidence, and the recommendation.

When research leads to a commitment, open a matching decision note and link to it. The research note records the investigation; the decision note records the call.

## Entries

### Voice / Brain

- [[music-failure-census-2026-07-17]] — full-log census (9 days, 225 addressed interactions, pre/post-deploy split) behind the "it's so on and off" complaint: deploy fixes verified working; 8 root causes ranked by post-deploy frequency (Deepgram stream death = 38% of attempts); 8 fixes shipped, 4 deferred with triggers
- [[dave-handshake-failure-april2026]] — DAVE never completes; bot deaf in voice; Option A workaround (env flag) recommended
- [[voice-bot-latency-april2026-findings]] — 2026 research dump (response to [[voice-pipeline-optimization-prompt]]); shipped vs deferred mapping included
- [[voice-pipeline-optimization-prompt]] — 2026 research prompt; resolved by [[voice-bot-latency-april2026-findings]]
- [[conversation-classifier]] — intelligent address detection for multi-user voice
- [[wake-word-latency]] — custom wake-word detection + response latency
- [[wake-word-augmentation-2026]] — 2024-2026 SOTA survey: hard-negative mining, TTS diversity, augmentation sweet spots, top-5 lift recipes for Hey Poob
- [[music-bot-feature-roadmap]] — 2025-2026 Discord music bot field survey + concrete FFmpeg effect params + autoplay cascade design + top-10 ship order

### Scanner / Marketplace

- [[fb-anti-bot-defenses]] — Facebook anti-bot defenses & scraping landscape (mutable)
- [[fb-detail-page-extraction]] — detail-page data extraction approaches
- [[fb-graphql-schema]] — Marketplace GraphQL API schema + anonymous access
- [[free-search-providers]] — survey of free web-search + price-comparison APIs (mutable)
- [[free-vlm-providers]] — survey of free-tier VLM/LLM providers (mutable)

### Historical audits

- [[handoff-audit-april-2026]] — spring 2026 pipeline audit; most items landed in [[unified-filter-pipeline]]
