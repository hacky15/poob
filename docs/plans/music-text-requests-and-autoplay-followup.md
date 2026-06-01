---
type: plan
status: active
date: 2026-06-01
tags: [music, autoplay, voice, brain, backlog]
related: [[music-autoplay-cascade]] [[music-player-architecture]] [[voice-architecture]] [[auto-join-missed-listening-setup]]
---

# Music follow-ups: request-when-not-in-VC + autoplay userflow

Two operator-requested items (2026-06-01), captured with current-state findings so the next session starts from facts, not a cold read.

## 1. Request music for Poob to play when you're NOT in a voice channel

### Current behavior (verified)

`handle_music_request` ([music_cog.py:316-319](../../src/poob/discord_bot/cogs/music_cog.py)) → if the guild has no voice client, calls `_auto_join_requester_vc(guild, user_id)` ([:209-256](../../src/poob/discord_bot/cogs/music_cog.py)):
- **Requester IS in a VC** → Poob auto-joins *their* channel, wires the wake/STT pipeline, and plays. Works from **text or voice** today.
- **Requester is NOT in any VC** → returns `None`; caller replies "join first." This is the gap.

### The honest constraint

Discord music must play *into* a voice channel. If the requester isn't in one, there is **no channel to play into** — so this is genuinely close to "hardstuck" unless we define a target. Options (need an operator product call, not an assumption):
- **(a) Queue-for-later:** accept the request, stash it, and play when the requester next joins a VC. Best UX, needs a per-user pending-request store + a voice-state-update hook.
- **(b) Default channel:** Poob joins a configured/last-used channel and plays there. Simple but plays to an empty/wrong room.
- **(c) Leave as-is** ("join first") — current behavior.

Recommendation: **(a)**, but it's a real feature (state + join-hook), not a quick patch. Deferred pending operator pick of (a)/(b)/(c). The operator noted this may already be hardstuck — confirmed it's constraint-bound, not a bug.

## 2. Autoplay — fully built; "stopped in its tracks" investigation

### Current state (verified)

Autoplay IS implemented and the mechanism is sound:
- `AutoplayEngine` 3-tier cascade ([music/autoplay.py](../../src/poob/music/autoplay.py)): ytmusic `get_watch_playlist` → yt-dlp `RD<id>` mix → random-from-history. Per [[music-autoplay-cascade]].
- Player loop ([player.py:856-857](../../src/poob/music/player.py)): when the queue empties, `_try_autoplay_inject()` fires off the **`_last_played_track` seed** ([:867](../../src/poob/music/player.py)) iff `autoplay_enabled`.
- Toggle via `music_assistant(action=autoplay, mode=on/off/status)`.

No code regression found. **No autoplay events at all in the current container logs** — so it simply hasn't been exercised since the recent boots, OR the enable request isn't being routed. Can't confirm a regression from logs; needs a live test.

### Userflow answers (operator's questions)

- **"hey poob autoplay" while a song is playing** → `action=autoplay, mode=on` sets `autoplay_enabled=True`. The player drains the existing queue first (`queue.get_next()`), and only when it's empty does `_try_autoplay_inject` seed off the last-played track. So autoplay continues **from the current song's mix, AFTER anything already queued** — exactly the desired flow. ✅ Mechanism supports it; verify routing live.
  - **Caveat:** if the queue has *already* emptied and the player loop has **ended** ("Queue empty, player loop ending", [player.py:859](../../src/poob/music/player.py)), enabling autoplay afterward won't restart a dead loop. Enabling *while a song is still playing* is fine. Worth confirming whether a post-stop "autoplay" should restart playback from the last seed.
- **"play xyz and autoplay" (compound)** → the brain emits **one** tool call per turn, so today this likely routes to `action=play(query=xyz)` and **drops the autoplay** (or vice-versa). This is the real gap. Robust fix: give the `play` action an optional `autoplay: bool` arg so a compound request sets both in one call (`play(query=xyz, autoplay=true)` → enqueue + flip `autoplay_enabled`). Cleaner than relying on the LLM to emit two calls.

### Proposed work (when prioritized)

1. **Confirm routing live:** operator says "hey poob autoplay" mid-song; check the voice log (once deployed) for `tool_route action=autoplay` + `autoplay enqueued`. If the route doesn't fire, the gap is brain routing (the `music_assistant` autoplay action description / examples), not the engine.
2. **Compound `play+autoplay`:** add `autoplay` arg to the play path so "play X and autoplay" works in one turn.
3. **(maybe) post-stop resume:** decide if "autoplay" after the queue already ended should restart from the last seed.

Out of scope until the voice log + DAVE-wedge fixes are deployed (so we can actually observe autoplay routing in the clean log).
