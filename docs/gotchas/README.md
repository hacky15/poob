---
type: moc
status: active
tags: [index]
---

# Gotchas — Map of Content

Things that bit us once and will bite us again if forgotten. Library quirks, platform surprises, deploy-config shadows, tempting-but-wrong fixes.

Every "tried X, it didn't work because Y, do Z instead" lives here as its own atomic note. The point of this folder: before trying something novel, search here first. If the gotcha is captured, we don't re-learn it at 2 AM.

Gotchas differ from incidents: an incident is a specific event in the past, a gotcha is a durable hazard in the codebase or toolchain. One incident can spawn one or more gotchas.

## Entries

### Brain / LLM

- [[tool-hallucination-from-passive-context]] — LLM tool calls whose arguments come from history, not the current turn
- [[google-vlm-ipm-undocumented-limit]] — Google VLM has an undocumented Images-Per-Minute limit
- [[new-provider-integration-quirks]] — Google CSE 403, Together 402, Mistral Pixtral `extra_forbidden`

### Discord / Voice / Music

- [[pycord-auto-sync-commands-fires-before-cogs]] — auto-sync runs in `on_connect`, before `on_ready` cog loading
- [[pycord-is-playing-is-a-property]] — `player.is_playing()` crashes; it's a property, not a method
- [[voice-music-common-pitfalls]] — asetrate inversion, yield-from-async, grace periods, temp-file cleanup

### Scanner / Marketplace

- [[price-none-is-not-zero]] — `listing.price = None` is not `$0`; caused 29 false deals
- [[facebook-og-jsonld-are-dead]] — OG tags + JSON-LD do NOT work on Facebook Marketplace
- [[graphql-amount-units-cents-vs-dollars]] — `amount_with_offset*` fields are cents, not dollars
- [[common-scanner-pitfalls]] — the ~17-item scanner pipeline hazard list

### Deploy / Infra

- [[docker-volume-shadows-baked-files]] — named-volume mount shadows files baked into the image
