---
type: research
status: active
date: 2026-05-11
tags: [music, voice, ffmpeg, discord, roadmap]
related: []
---

# Music Bot Feature Roadmap — what the field ships in 2025-2026

## Question

What features do top Discord music bots actually ship today, what are the proven FFmpeg parameters for the common effects (nightcore, slowed+reverb, bass boost, karaoke, 8D, pitch shift), how do autoplay engines actually work, and what is the right ordered punch-list for one dev to ship over the next month on top of Poob's existing voice + brain stack?

Source corpus is dated 2025-2026 and emphasises concrete numbers — filter strings, free-tier limits, API endpoints — over generic advice.

## Findings

### 1. Feature inventory of top music bots, 2025-2026

The space has bifurcated into "simple/fast" bots and "AI-augmented/Spotify-connected" bots. Most bots converge on the same baseline (play, queue, skip, loop, shuffle, volume, lyrics, autoplay, basic filters) and differentiate at the long tail.

**FredBoat** — free, no premium. Multi-source (YouTube, SoundCloud, Bandcamp, Twitch, Vimeo, Dailymotion, Deezer). Strong playlist import/export. No effects. Conservative, reliability-first.

**Hydra (the surviving Hydrogen-class bot)** — free + $3.99/mo premium. Web dashboard for queue/settings. In-Discord lyrics. Audio filters. 24/7 in premium. Multi-source including Spotify.

**Vexera** — free + premium. Web dashboard. Autoplay and volume gated to premium. Song history.

**Aiode** — free, open-source ([robinfriedli/aiode](https://github.com/robinfriedli/aiode)). Cross-platform playlists (Spotify + YouTube + SoundCloud + Twitch in one playlist). Custom command presets / shortcuts. Per-server prefix and bot name. Role-based command access. Login-your-Spotify for personal playlist playback. Real-time lyrics.

**Jockie Music** — free + premium. Up to four parallel bot instances in one server (lets one server run multiple queues). 8D, bass boost, distortion, echo effects. Spotify + Apple Music. Song-guessing minigame.

**Uzox** — built-in nightcore, vaporwave, bass-boost filters; live lyrics in chat; on-message playback buttons. Spotify connect.

**Mee6 Premium** — music gated to premium tier. Up to 1000-track queue, web dashboard, music quizzes, voice-channel recording. Music is bolted onto a moderation bot, not the focus.

**Chip Bot** — free bass-boost/nightcore filters; lyrics; YouTube + Twitch + SoundCloud (no Spotify).

**Tempo** — donation-supported; differentiation is low-latency playback at scale.

**LoFi Girl** — 24/7 ambient with seasonal/themed playlists.

**Lavalink-backed bots (lavamusic, Lunox, PrimeMusic, etc.)** — Lavalink's `FilterBuilder` exposes timescale (speed+pitch+rate), tremolo, vibrato, karaoke, rotation, distortion, channel-mix, low-pass, equalizer (15-band). Most "12+ filters" claims come from this set ([lavalink-client FilterBuilder](https://client.lavalink.dev/lavalink-client/dev.arbjerg.lavalink.client.player/-filter-builder/index.html)). erela.js-filters adds nightcore/vaporwave as presets on top.

**The long-tail features that show up across the list:**

- Autoplay when queue empties (YouTube related / genre-curated / Spotify recs)
- Vote-skip with DJ role override (~50-60% threshold; JMusicBot ~60%, Rythm 50%)
- Cross-platform playlist import (Spotify URL -> resolve tracks -> queue from YouTube)
- Lyrics, plain and synced — Genius (plain), LRCLIB and Musixmatch (synced LRC)
- Seek to timestamp with multiple time formats (`2m45s`, `2:45`, `165`)
- Now-playing embed with progress bar + cover art + interactive buttons (pause/skip/loop/volume/shuffle/previous/stop)
- Queue manipulation: `move N to M`, `remove N`, `shuffle`, `loop track`, `loop queue`, `clear`, `previous` (history-replay), `replay current`
- Per-server defaults: volume, autoplay on/off, DJ role, vote-skip threshold, default search source
- Audio quality presets (bitrate/source selection)
- Save / restore named playlists (`saveplaylist chill`, `loadplaylist chill`)
- "What was that song?" — last-N history lookup
- 24/7 stay-in-channel
- Multiple parallel bot instances per server (Jockie's niche)

### 2. Audio effects — concrete FFmpeg parameters

All numbers below are validated from FFmpeg's own filter docs ([ffmpeg-filters](https://ffmpeg.org/ffmpeg-filters.html)) plus published recipes.

**Nightcore.** The standard trick is `asetrate=48000*1.25,aresample=48000,atempo=1.0` — speeds up sample rate (raising pitch and tempo by 25%) then resamples back to 48k so downstream stages stay aligned. Most bots pick **1.2x-1.3x** asetrate multiplier; 1.25 is the sweet spot, 1.3 starts sounding chipmunked. Source rate must match the input (44100 for most YouTube extracts, 48000 for Discord-ready streams). To slightly decouple pitch from speed, chain an `atempo` after — e.g. `asetrate=44100*1.3,aresample=44100,atempo=0.95` lifts pitch ~30% but only speeds up ~23%. ([hhsprings atempo/asetrate/aresample reference](https://hhsprings.bitbucket.io/docs/programming/examples/ffmpeg/manipulating_audio/atempo_asetrate_aresample.html))

**Slowed.** Inverse of nightcore: `asetrate=44100*0.85,aresample=44100` for the canonical "slowed" feel — pitches down ~15%, slows tempo ~15%. **0.85** is the genre standard for "slowed + reverb" TikTok-style edits. **0.75** is "super slowed" and starts losing intelligibility on vocals. Below **0.65** is unlistenable except for ambient drones.

**Slowed + reverb (the genre).** Standard chain: `asetrate=44100*0.85,aresample=44100,atempo=1.0,aecho=0.8:0.88:60|90|120:0.4|0.3|0.2`. The aecho params decode as `in_gain : out_gain : delays(ms) : decays`. The triple-delay (60/90/120 ms) with decays 0.4/0.3/0.2 produces a "room" feel without smearing the beat. ([williamyaps echo recipe](https://williamyaps.blogspot.com/2017/04/soundeffects.html)) For a wetter chorus-y reverb, lengthen delays to 500|750|1000 ms and lower decays to 0.3|0.25|0.2.

**Reverb alone.** `aecho` is the cheap option and what most bots use. For real convolution reverb use `afir` with an impulse response file (`afir=dry=10:wet=10`) — quality is studio-grade but you ship an IR alongside. Skip this unless quality matters; `aecho` is good enough for Discord-bitrate playback.

**Bass boost.** `bass=g=N` where N is dB. Useful range -20 to +20; **+5 to +8 dB is the sweet spot** for music, +10 is the "loud" preset, +15 distorts on most consumer DACs. The `f` (cutoff freq) parameter defaults to 100 Hz; widen to `f=150` for fuller bass on bass-light tracks. Add `dynaudnorm` after `bass=` to recover headroom: `bass=g=8,dynaudnorm=f=200`. ([FFmpeg bass filter docs](https://ayosec.github.io/ffmpeg-filters-docs/8.0/Filters/Audio/bass.html))

**8D / spatial.** `apulsator=hz=0.125` is the one-liner everyone uses — 0.125 Hz is one full pan every 8 seconds, the "8D audio" effect TikTok popularized. Bump to `hz=0.2` for faster spin, drop to `hz=0.08` for slower. Modes: `sine` (default, smooth), `triangle` (sharper), `square` (hard left/right pingpong — usually bad). Worth it: yes, it's a 5-character config and users love it. ([FFmpeg apulsator source](https://github.com/FFmpeg/FFmpeg/blob/master/libavfilter/af_apulsator.c))

**Karaoke / vocal removal.** Cheap version: `pan=stereo|c0=c0-c1|c1=c1-c0` cancels the L=R center where lead vocals usually sit. Quality is mediocre — anything else that's center-panned (kick, bass, snare often) gets cancelled too, and any vocal with stereo widening / reverb leaves a faint ghost. Don't promise "remove vocals" — promise "karaoke mode" and let users discover the limits. For high-quality vocal removal you need spectral separation (Spleeter/Demucs); that's heavyweight and probably not worth shipping inside a Discord bot's hot path.

**Pitch shift independent of speed.** Only `rubberband` does this cleanly. `rubberband=tempo=1.0:pitch=1.5` raises pitch a perfect fifth without speeding up. **Important caveat: FFmpeg's `rubberband` filter is only present in builds compiled with `--enable-librubberband`**, which is _not_ the default in many distros. Verify with `ffmpeg -filters | findstr rubberband` on Windows before promising the feature. If rubberband is missing, the only fallback is the `asetrate` trick, which couples pitch and speed.

**Crossfade between tracks.** `afade=t=out:st=Xs:d=3` on the outgoing source and `afade=t=in:st=0:d=3` on the incoming, then `amix=inputs=2`. Hard to do cleanly across two FFmpeg subprocesses; needs a small mixer process or Lavalink which does it natively.

**On-the-fly toggling.** Hard truth: `FFmpegPCMAudio` filters are baked in at process spawn via `-af` in `before_options`/`options`. You **cannot** change a filter mid-stream without respawning the process. The standard pattern is:

1. Track current playback position
2. Stop current source (`voice_client.stop()`)
3. Spawn new `FFmpegPCMAudio` with updated `-af` chain and `-ss <position>` to resume
4. `voice_client.play(new_source)`

This produces a ~200-400 ms gap and may glitch on stream URLs that don't support seek. `PCMVolumeTransformer` is the only thing that adjusts live without respawn — it's a Python wrapper that scales samples in-process. ([discord.py player.py source](https://github.com/Rapptz/discord.py/blob/master/discord/player.py)) For real live filter toggling you need Lavalink, which exposes a `filters` op that updates the running player.

### 3. Autoplay implementations

Compared by reliability, latency, quality, cost:

| Approach | Cost | Latency | Quality | Notes |
|---|---|---|---|---|
| **yt-dlp scrape of YouTube watch-page "Up next"** | Free | 1-3 s | Mediocre; YT bias pushes you toward popular tracks regardless of taste | This is what most bots actually do. Hit the watch URL, parse the related items, pick one not in recent history. |
| **yt-dlp on a YT Mix playlist (`RD<videoId>`)** | Free | 1-3 s | Better — YouTube's own "make a radio station from this song" algorithm | Construct the URL as `https://www.youtube.com/watch?v=<id>&list=RD<id>`; yt-dlp resolves the playlist. Best free option. |
| **ytmusicapi `get_song_related` / `get_watch_playlist`** | Free | 500 ms-2 s | Best of the free options — uses YT Music's recommender, which is genuinely good | Unofficial API; uses YT Music's `RDAMVM<videoId>` mix prefix internally. Best quality / latency ratio if you can tolerate unofficial-API maintenance risk. ([ytmusicapi docs](https://ytmusicapi.readthedocs.io/)) |
| **Last.fm `track.getSimilar`** | Free, no auth | 200-500 ms | Good for mainstream Western music; sparse on niche / non-Western / new releases | No published rate limit but "be reasonable, don't pin several req/sec." Returns track+artist pairs; you still have to resolve to a playable source. ([Last.fm track.getSimilar](https://www.last.fm/api/show/track.getSimilar)) |
| **Spotify `/recommendations`** | OAuth + Spotify Premium for dev accounts starting Feb 2026 | 200 ms | Very good | **Effectively closed to new bots in 2025-2026.** Spotify deprecated several recommendation endpoints in late 2024, new Development Mode client IDs need Premium and are capped at 5 users + a reduced endpoint set, extended quota requires 250k MAU and a registered business. Not viable for a hobby bot. ([Spotify dev access update Feb 2026](https://developer.spotify.com/blog/2026-02-06-update-on-developer-access-and-platform-security), [TechCrunch Nov 2024](https://techcrunch.com/2024/11/27/spotify-cuts-developer-access-to-several-of-its-recommendation-features/)) |
| **LLM-as-DJ — feed last 5 tracks, ask for next** | Free (Groq/Cerebras tier) | 800 ms-2 s | Genuinely good — LLM understands genre, mood, vibe in a way classifiers don't | Then you still need to resolve title->source. Best as a "vibe-aware" mode rather than the default — slower than ytmusicapi and you pay an LLM call per autoplay tick. |
| **Local-history-weighted random** | Free | Instant | Bad — just shuffles what's already been played | Useful as a fallback but never a primary. |
| **Genre/tag matching from MusicBrainz / Last.fm tags** | Free | 300 ms | OK | Adds complexity (two lookups: tags then similar-by-tag) for marginal gain over `track.getSimilar` directly. Skip unless you're explicitly building taste profiles. |

**Recommendation.** Build a cascade like the VLM cascade already in Poob: `ytmusicapi.get_watch_playlist` -> `Last.fm track.getSimilar` -> yt-dlp `RD<id>` mix scrape -> LLM-as-DJ (only when user has invoked a "vibe" mode). Keep Spotify out of the path; the licensing wall is real.

### 4. Multi-song queue from a single utterance

Four patterns observed; ranked by quality:

**(a) LLM emits an array argument in a single tool call** — `queue_tracks(tracks=["Bohemian Rhapsody", "Don't Stop Believin'"])`. Cleanest, single round-trip. Requires the tool schema to accept `list[str]`. **This is the right move for an LLM-augmented bot** like Aiode and what Poob's brain already does well.

**(b) LLM emits multiple parallel tool calls** — `play("...")` + `play("...")` in one assistant turn. Works on Claude, OpenAI, Groq (which all support `parallel_tool_calls`). Latency-equivalent to (a) if the harness runs them concurrently, but messier ordering semantics — which song lands first in the queue? Risk of race condition unless your queue accepts an explicit position arg.

**(c) Server-side parsing of "and" / commas / "then"** — regex over the raw user utterance. Brittle: fails on "play 'salt and pepper' and 'come together'", on commas inside song titles, on conjoined artist names. Don't.

**(d) Two-stage: LLM extracts list, separate stage resolves** — extract structured `{tracks: [...]}` then per-track call. Same network cost as (a) but more code. Useful if you want per-track error handling / partial success reporting.

Aiode and the Lavalink-stack bots don't actually do this well — they accept one query at a time. **Poob's already-multi-modal brain is positioned to leapfrog them on this** by using pattern (a) with the array tool. Single tool, `list[str]` arg, append-to-queue semantics, return list of resolved-or-failed per track for the brain to narrate.

### 5. The gauntlet — 20 candidate features ranked by user value

Difficulty: S (hours), M (1-2 days), L (a week+). Value: low/med/high. Paid-API flag where relevant.

| # | Feature | Diff | Value | Paid? |
|---|---|---|---|---|
| 1 | `/play <query>` with YouTube source (already shipped in voice flow) | — | — | No |
| 2 | Queue + show queue + remove + move + clear | S | high | No |
| 3 | Skip + previous + replay current | S | high | No |
| 4 | Loop (track / queue / off) | S | high | No |
| 5 | Shuffle | S | med | No |
| 6 | Seek to timestamp (`/seek 2:30`, `+10s`, `-5s`) | S | high | No |
| 7 | Volume + persistent per-server default | S | high | No |
| 8 | Autoplay when queue empties (ytmusicapi cascade) | M | high | No |
| 9 | Filter presets: nightcore / slowed / slowed+reverb / bassboost / 8D / vaporwave | M | high | No |
| 10 | On-the-fly filter toggle (respawn FFmpeg with `-ss` seek) | M | high | No |
| 11 | Now-playing embed with progress bar + buttons (pause/skip/loop/shuffle/volume) | M | high | No |
| 12 | Multi-song queue from one utterance (`list[str]` tool arg) | S | high | No |
| 13 | Lyrics — plain (Genius scrape or `lyricsgenius`) | S | med | No |
| 14 | Lyrics — synced LRC (LRCLIB free, no auth) | M | med | No |
| 15 | Vote-skip with DJ role override (50% threshold) | M | med | No |
| 16 | Spotify playlist URL import (Spotipy `ClientCredentials` -> resolve titles -> queue) | M | high | Free tier of Spotify (still works for read-only public playlist titles) |
| 17 | Save / restore named playlists per server (SQLite) | M | med | No |
| 18 | "What was that song?" — last-N history with re-add | S | med | No |
| 19 | 24/7 stay-in-channel toggle | S | low | No |
| 20 | Karaoke / vocal-remove preset | S | low | No |
| 21 | Crossfade between tracks (mixer process or Lavalink) | L | med | No |
| 22 | Pitch-shift independent of speed (requires rubberband-enabled FFmpeg build) | M | low | No (but verify build) |
| 23 | Genre-tagged radio mode (Beatra-style 20 genres) | M | med | No |
| 24 | Song-guessing minigame (Jockie-style) | M | low | No |
| 25 | Web dashboard for queue / settings | L | med | No |

### Evidence

**Bot inventories**

- [BotPenguin best music bots 2026](https://botpenguin.com/blogs/best-music-bot-for-discord)
- [alvarotrigo.com 2025 list](https://alvarotrigo.com/blog/best-discord-music-bots/) — covers Jockie 4-instance feature, Uzox filters, Chip free filters, Aiode Spotify
- [aiode source](https://github.com/robinfriedli/aiode) — open-source reference for cross-platform playlists, custom presets, per-server config
- [JMusicBot wiki](https://jmusicbot.com/commands/) — vote-skip mechanics, ~60% threshold
- [Rythm docs](https://docs.rythm.fm/commands/) — 50% vote-skip, seek time formats
- [erela.js-filters](https://www.npmjs.com/package/erela.js-filters) — reference Lavalink-style filter preset names

**FFmpeg parameters**

- [ffmpeg-filters official](https://ffmpeg.org/ffmpeg-filters.html)
- [bass filter range -20..+20](https://ayosec.github.io/ffmpeg-filters-docs/8.0/Filters/Audio/bass.html)
- [atempo/asetrate/aresample patterns](https://hhsprings.bitbucket.io/docs/programming/examples/ffmpeg/manipulating_audio/atempo_asetrate_aresample.html)
- [apulsator source for 8D](https://github.com/FFmpeg/FFmpeg/blob/master/libavfilter/af_apulsator.c)
- [aecho example chain](https://williamyaps.blogspot.com/2017/04/soundeffects.html)
- [pan filter karaoke discussion](https://ffmpeg-user.ffmpeg.narkive.com/rOFDyQy1/pan-filter-confusion)
- [rubberband enable flag warning](https://bbs.archlinux.org/viewtopic.php?id=284082)

**Recommendation engines**

- [ytmusicapi docs](https://ytmusicapi.readthedocs.io/)
- [yt-dlp recommended-feed issue #9767](https://github.com/yt-dlp/yt-dlp/issues/9767)
- [Last.fm track.getSimilar](https://www.last.fm/api/show/track.getSimilar) + [API ToS](https://www.last.fm/api/tos)
- [Spotify Feb 2026 platform access update](https://developer.spotify.com/blog/2026-02-06-update-on-developer-access-and-platform-security)
- [Spotify deprecation TechCrunch Nov 2024](https://techcrunch.com/2024/11/27/spotify-cuts-developer-access-to-several-of-its-recommendation-features/)

**discord.py / Pycord audio internals**

- [Pycord player.py source](https://docs.pycord.dev/en/v2.7.0/_modules/discord/player.html)
- [discord.py v1 migration — VoiceClient.source swap](https://discordpy.readthedocs.io/en/v1.6.0/migrating.html)

**Lyrics / synced lyrics**

- [syncedlyrics on PyPI](https://pypi.org/project/syncedlyrics/) — LRCLIB / Musixmatch / Netease aggregator, no auth needed for LRCLIB

## Recommendation

### Top-10 ship order for one dev over the next month

The ranking optimizes for **value-per-hour-of-work** and assumes Poob's voice + brain stack is already in place (so all of this can be exposed through the brain's tool-calling, not just slash commands).

**1. Queue primitives (queue / show / remove / move / clear / skip / previous / replay / loop / shuffle).** S each, high value, table stakes. Without these the bot is one-shot-only. Ship as a coherent set of brain tools so the LLM can invoke them naturally ("skip the next two and shuffle the rest"). Half a day total.

**2. Multi-song queue from one utterance.** S, high value, and Poob's brain is already wired for tool-calling. Define `queue_tracks(tracks: list[str])`. Returns per-track resolution status. Two hours.

**3. Now-playing embed with buttons.** M, high value. Pycord has native button support. Buttons: pause/resume, skip, loop-cycle, shuffle, queue-view, leave. This is the visible UX that signals "real music bot" vs "voice toy."

**4. Filter presets (nightcore / slowed / slowed+reverb / bassboost / 8D / vaporwave / karaoke / chipmunk / deep).** M, high value. One module mapping preset name -> FFmpeg `-af` string. Validated parameters from this doc:

- `nightcore`: `asetrate=44100*1.25,aresample=44100`
- `slowed`: `asetrate=44100*0.85,aresample=44100`
- `slowed+reverb`: `asetrate=44100*0.85,aresample=44100,aecho=0.8:0.88:60|90|120:0.4|0.3|0.2`
- `bassboost`: `bass=g=8,dynaudnorm=f=200`
- `8d`: `apulsator=hz=0.125`
- `vaporwave`: `asetrate=44100*0.8,aresample=44100,aecho=0.8:0.9:1000:0.3`
- `karaoke`: `pan=stereo|c0=c0-c1|c1=c1-c0`
- `chipmunk`: `asetrate=44100*1.5,aresample=44100`
- `deep`: `asetrate=44100*0.75,aresample=44100`

**5. On-the-fly filter toggle via FFmpeg respawn-with-seek.** M, high value, and a hard requirement to make #4 usable from voice ("Poob, slow it down"). Track playback position via `voice_client.timestamp` or a wall-clock since `voice_client.play()`. Stop, respawn with new `-af` + `-ss <pos>`. Document the ~300 ms gap as a known cost in `docs/gotchas/`. Half a day.

**6. Autoplay cascade.** M, high value. Order: `ytmusicapi.get_watch_playlist(track_id)` -> `Last.fm track.getSimilar` -> yt-dlp scrape of `RD<id>` mix URL -> shuffle of recent history (last-resort). Brain owns the toggle: `/autoplay on|off|vibe`. "vibe" mode uses LLM-as-DJ on top of the cascade. One day for the cascade, half a day for the LLM-DJ mode.

**7. Seek with multi-format input.** S, high value. Accept `2:30`, `2m30s`, `150`, `+10`, `-5`. Implemented as a respawn with `-ss`, same machinery as #5. Two hours.

**8. Spotify playlist URL import (read-only, no OAuth).** M, high value. `spotipy.SpotifyClientCredentials` still works for public-playlist `playlist_items` reads (the deprecation hit recommendations, not basic metadata). Parse playlist URL -> list track titles -> queue them via #2. Add to brain as `queue_spotify_playlist(url)`. Half a day if Spotify dev creds are already provisioned, full day otherwise.

**9. Save / restore named per-server playlists.** M, med value, but compounds with #8 and #2 to become "the personal jukebox" feature. Persist track-URL lists to the existing SQLite store keyed on `(guild_id, name)`. `/playlist save chill`, `/playlist load chill`, `/playlist list`. Half a day.

**10. Synced lyrics overlay.** M, med value. `syncedlyrics` package -> LRCLIB free no-auth -> render the current line in a periodically-edited embed every 1-2 seconds, throttled to Discord's rate limit. Falls back to plain Genius lyrics in a paginated embed if no LRC is found. One day.

### Rationale for the ranking

1-5 is the "play parity with a real music bot in two weeks" track — without queue primitives + buttons + filter presets + live filter toggle Poob can't compete with Hydra or Jockie at the basic level. 6 (autoplay) is the killer free-tier feature that bots like Vexera gate behind premium, so shipping it free is differentiating. 7 (seek) is cheap once #5's respawn machinery exists. 8-9 are the Aiode-style cross-platform-playlist features that lift Poob into the AI-augmented tier. 10 (synced lyrics) is the visible "wow" feature for the next demo without paid APIs.

Deliberately deferred:

- **Crossfade** (#21 in the gauntlet) — requires either a custom mixer or Lavalink; high-effort, medium-value, and most bots don't actually do it well anyway.
- **Pitch-shift-independent-of-speed** (#22) — depends on a rubberband-enabled FFmpeg, which Poob's Windows-dev Docker-prod environment may not have. Verify before promising.
- **Web dashboard** (#25) — large scope, and the bot's interaction model is already voice-first via PoobBrain. A dashboard would duplicate brain functionality.
- **Vote-skip with DJ role** (#15) — useful in big servers, but Poob's actual deployment is small servers / personal-buddy use, where vote-skip is friction. Add if a larger server adopts the bot.

The bigger architectural call this research surfaces: **expose every music capability as a brain tool first**, then mirror slash commands as a secondary surface. Poob's differentiation is not "another music bot" — it's "the buddy who happens to also DJ." Keeping the brain as the primary interface means voice commands ("Poob, slow this down and queue Bohemian Rhapsody after") work without any extra plumbing.
