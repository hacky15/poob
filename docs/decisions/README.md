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
- [[voice-latency-phase2-filler-dispatch]] — wake up `FillerPlayer`: dispatch filler clip via `create_task(self._maybe_play_filler())` at the start of `_process_single_response`; routes through existing `_play_audio` + mutex for natural queue-behind semantics (2026-05-24, active)
- [[voice-latency-phase3-kokoro]] — Kokoro-82M as opt-in TTS provider via `VOICE_TTS_PROVIDER=kokoro`; eliminates cloud round-trip when active; cloud cascade is the fallback (2026-05-23, active)
- [[music-synced-lyrics]] — `lyrics` brain action; LRCLIB synced → plain cascade via `syncedlyrics`; inline `[SILENT]` reply (live overlay-tick loop deferred to v2 — needs channel-ref plumbing) (2026-05-23, active)
- [[music-spotify-playlist-import]] — `queue_spotify_playlist(url)` brain action; Spotipy `ClientCredentials` for read-only public playlists → YT search resolution → enqueue (2026-05-23, active)
- [[music-named-playlists]] — per-guild `guild_playlists(guild_id, name, tracks_json)` SQLite store; 4 brain actions (save/load/list/delete) with case-insensitive name uniqueness (2026-05-23, active)
- [[music-autoplay-cascade]] — when the queue empties and `autoplay=on`, cascade `ytmusicapi.get_watch_playlist` → yt-dlp on `RD<id>` mix URL → random-from-history; brain action `music_assistant(action=autoplay, mode=on/off/status)` (2026-05-20, active)
- [[wake-word-v3-shipped]] — ship v3 ONNX (816k WAV corpus, 50k steps, V2's known failures fixed at 0.9997, FP=0/12); known phonetic-neighbor + bare-stem gaps queued for v4 (2026-05-16, active)
- [[self-hosted-runners-migration]] — CI runs on homelab containerized runners; ``runs-on: [self-hosted, linux, x64, poob]``; eliminates GHA billing (2026-05-13, active)
- [[wake-word-mass-augmentation-v3]] — recall-biased corpus: 449 phrase variants × 47 voices × 8 rates + phonetic-neighbor positives + clip/pitch augmentation = ~11.85M training samples (2026-05-13, active)
- [[boob-music-wrap-variant]] — rare ~5% music-wrap variant: Boob, Toob's sweet side piece, three-sentence compliment, higher pitch (2026-05-06, active)
- [[text-casual-fallback-bypass-deal-agent]] — text-mode mirrors voice: empty router response → llama-3.1-8b-instant casual call, not deal sub-agent (2026-05-06, active)
- [[ytdl-search-best-guess-fallback]] — two-pass ytsearch1 → ytsearch5 instead of dead-ending misheard queries on "couldn't find" (2026-05-01, active)
- [[music-tool-call-robustness]] — tool-call token floor + prompt isolation + duplicate-play suppression with failure-recovery (2026-04-27, active)
- [[drop-cerebras-from-cascade]] — Cerebras chronically 429-rate-limited; removed from tool-call cascade (2026-04-23, active)
- [[wake-fp-pcm-capture]] — save 2s PCM windows of wake-word false positives for offline retraining (2026-04-23, active)
- [[deepgram-flux-optional]] — Deepgram model now configurable via env; opt-in Flux path available (2026-04-23, active)
- [[speculative-music-wrap]] — run ytdl search and Toob wrap in parallel; stream wrap so Toob speaks within 500ms (2026-04-23, active)
- [[groq-gpt-oss-20b-swap]] — swap Groq primary to gpt-oss-20b + client-side `<function=...>` recovery (2026-04-23, active)
- [[brain-routing-audit]] — LLM cascade for tool routing; keyword-intent bandaid removed (2026-04-22)
- [[just-listed-rework]] — split freshness knobs, auto-start patrol, observability + canary (2026-04-22)
- [[one-handler-discord]] — one `on_message` listener, one voice owner, one music owner (2026-04-21)
- [[one-handler-music-contract]] — all music flows through `handle_music_request(tool_args=...)` (2026-04-21)
- [[search-provider-cascade]] — Tavily → Serper → SearXNG → empty (2026-03-30)
- [[toob-voice-filter-chain]] — FFmpeg warlord chain; retuned for intelligibility + speed (2026-04-22)
- [[tts-loudness-speechnorm]] — FFmpeg speechnorm at decode step for RMS normalization (2026-04-22)
- [[wake-word-dual-gate]] — context-aware acoustic + text, bot-TTS-only loopback gate (2026-04-21 → 2026-04-22)
