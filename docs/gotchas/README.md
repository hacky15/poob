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

- [[empty-routing-response-is-not-failure]] — RLHF models (gpt-oss family) silently refuse by returning empty content + no tool; treat as soft refusal, route to a non-RLHF content model
- [[tool-hallucination-from-passive-context]] — LLM tool calls whose arguments come from history, not the current turn
- [[google-vlm-ipm-undocumented-limit]] — Google VLM has an undocumented Images-Per-Minute limit
- [[new-provider-integration-quirks]] — Google CSE 403, Together 402, Mistral Pixtral `extra_forbidden`

### Music

- [[ffmpeg-effect-toggle-creates-audio-gap]] — ~200-400ms silence on effect change / replay / previous is intentional; don't "fix" it without migrating to Lavalink

### Discord / Voice / Music

- [[dave-version-zero-rejected-by-e2ee-required-guilds]] — `VOICE_MAX_DAVE_PROTOCOL_VERSION=0` triggers WS 4017 reject loop on E2EE-required guilds
- [[dave-ready-flag-is-not-truth]] — `dave_session.ready` can stay False while audio works fine; never use it as a fatal precondition
- [[davey-session-needs-serialization-lock]] — a shared `davey.DaveSession` is touched by recv+player+loop threads; the `threading.Lock` dropped in the Pycord migration is REQUIRED — without it a 4014 reconnect wedges the whole event loop
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
- [[compose-container-name-collisions]] — `container_name:` overrides without a project prefix collide with other stacks; namespace them as `poob-*`
- [[docker-desktop-data-vhd-separate-from-engine]] — moving Docker Desktop's WSL distro does NOT move its image-store VHD; relocate both or C: silently fills (Windows)
- [[wake-word-model-path-conventions]] — wake-word ONNX files MUST live at `/app/*.onnx`, never under `/app/data/`; env var is `WAKE_WORD_MODEL_PATH` (legacy `PORCUPINE_KEYWORD_PATH` aliased for one rotation)
