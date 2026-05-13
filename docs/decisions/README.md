---
type: moc
status: active
tags: [index]
---

# Decisions — Map of Content

Architectural choices and why we made them. Each note captures context, the call we made, alternatives we rejected, and consequences we accepted.

Write a decision note when a choice locks in non-trivial downstream behavior — cascade order, thresholds, framework choice, filter chains, persistence layer. If someone could plausibly ask "why did we do it this way," the answer lives here.

Write a new note (don't edit in place) when a decision is reversed or materially changed. Mark the old one `status: superseded` and link forward with `supersedes` / `superseded_by`.

## Entries

- [[music-queue-primitives]] — move / remove / clear / previous / replay as ``music_assistant`` actions; respawn-based playback for replay & previous (2026-05-12, active)
- [[music-queue-many-tool]] — ``queue_many`` action takes ``tracks: list[str]`` for multi-song queue from one utterance; per-track resolution status (2026-05-12, active)
- [[music-filter-presets]] — nightcore / slowed / slowed_reverb / bassboost / 8d / vaporwave / karaoke / chipmunk / deep / super_slowed as named presets, dispatched via ``apply_effect`` (2026-05-12, active)
- [[music-on-the-fly-filter-respawn]] — change effects mid-track via FFmpeg respawn with ``-ss`` seek; shared mechanism for replay / previous / set_effect (2026-05-12, active)
- [[music-seek]] — ``seek`` action with multi-format parser (``2:30``, ``2m30s``, ``150``, ``+10``, ``-1m``); reuses respawn machinery (2026-05-12, active)
- [[music-now-playing-embed-buttons]] — 3 new persistent-view buttons (previous / replay / leave) + active-effect embed indicator; cross-cog leave reaches VoiceCog (2026-05-12, active)
- [[boob-music-wrap-variant]] — rare ~5% music-wrap variant: Boob, Toob's sweet side piece, three-sentence compliment, higher pitch (2026-05-06, active)
- [[text-casual-fallback-bypass-deal-agent]] — text-mode mirrors voice: empty router response → llama-3.1-8b-instant casual call, not deal sub-agent (2026-05-06, active)
- [[ytdl-search-best-guess-fallback]] — two-pass ytsearch1 → ytsearch5 instead of dead-ending misheard queries on "couldn't find" (2026-05-01, active)
- [[music-tool-call-robustness]] — tool-call token floor + prompt isolation + duplicate-play suppression with failure-recovery (2026-04-27, proposed)
- [[drop-cerebras-from-cascade]] — Cerebras chronically 429-rate-limited; removed from tool-call cascade (2026-04-23, proposed)
- [[wake-fp-pcm-capture]] — save 2s PCM windows of wake-word false positives for offline retraining (2026-04-23, proposed)
- [[deepgram-flux-optional]] — Deepgram model now configurable via env; opt-in Flux path available (2026-04-23, proposed)
- [[speculative-music-wrap]] — run ytdl search and Toob wrap in parallel; stream wrap so Toob speaks within 500ms (2026-04-23, proposed)
- [[groq-gpt-oss-20b-swap]] — swap Groq primary to gpt-oss-20b + client-side `<function=...>` recovery (2026-04-23, proposed)
- [[brain-routing-audit]] — LLM cascade for tool routing; keyword-intent bandaid removed (2026-04-22)
- [[just-listed-rework]] — split freshness knobs, auto-start patrol, observability + canary (2026-04-22)
- [[one-handler-discord]] — one `on_message` listener, one voice owner, one music owner (2026-04-21)
- [[one-handler-music-contract]] — all music flows through `handle_music_request(tool_args=...)` (2026-04-21)
- [[search-provider-cascade]] — Tavily → Serper → SearXNG → empty (2026-03-30)
- [[toob-voice-filter-chain]] — FFmpeg warlord chain; retuned for intelligibility + speed (2026-04-22)
- [[tts-loudness-speechnorm]] — FFmpeg speechnorm at decode step for RMS normalization (2026-04-22)
- [[wake-word-dual-gate]] — context-aware acoustic + text, bot-TTS-only loopback gate (2026-04-21 → 2026-04-22)
