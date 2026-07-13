---
type: incident
status: resolved
date: 2026-06-02
tags: [music, discord, now-playing, race, ux]
related: [[music-now-playing-embed-buttons]] [[one-handler-music-contract]] [[now-playing-card-snapshot-after-brain-call]]
---

# Now-playing card only appears for some tracks (download race)

## Symptom

Operator, with a screenshot: the now-playing widget (embed + control buttons)
appeared for `TIKI TIKI (Slowed)` but **not** for the track played just before
it (`A Hood Classic…`). Sequence was:

1. "play hood classic" → reply "Playing A Hood Classic… [29:29]" — **no card**.
2. "play tiki tiki" → "Queued TIKI TIKI at position 1" — no card (correct; queued).
3. "skip" → tiki tiki starts → **card appears**.

## Root cause

`agent_handler` posted the card by comparing `player.current_track` **before**
the brain call to **immediately after** the reply was sent
([agent_handler.py](../../src/poob/discord_bot/agent_handler.py)), posting only
if it changed. That synchronous check loses a race against the track
**download**:

- A fresh "play X" enqueues the track and the player loop starts downloading.
  `queue.current` (= `current_track`) is set only **after** the download
  finishes and `vc.play` begins. The post-reply check ran while the 29-min
  track was still downloading → `current_track` still `None` → no card.
- "skip" advanced to tiki tiki, which had already been **pre-fetched** (the
  player pre-downloads the next queued track), so it became `current`
  *instantly* → the check caught the change → card posted.

So the card only showed for instant transitions (skip/queue-advance to a
pre-downloaded track) and silently missed normal fresh plays.

## Fix

Replaced the synchronous snapshot with a **bounded, non-blocking wait**:
`agent_handler` spawns `_post_now_playing_when_ready(...)` as a background task
that polls `current_track` (up to 15 s, 0.25 s interval) and posts the card the
moment it changes from the pre-call snapshot. By the time it fires,
`current_track` is set, so `build_now_playing_message` builds a correct embed.
A queue-only request never changes `current`, so the task times out silently —
no card, which is correct. Non-blocking, so it never delays the reply.

**Why not an event-driven player callback (considered):** firing the card from
the player's track-start event is the "purest" design, but it would post a card
on *every* track start — including each autoplay/queue-advance track (spammy) —
and would require plumbing the originating text channel down through the brain →
handler → cog → player. The bounded wait fixes exactly the reported bug
(inconsistent card on user-triggered plays) without that behavior change or
cross-layer plumbing. If a true now-playing event surface is wanted later
(e.g. to also drive autoplay cards or edit a single persistent message), that's
the follow-up.

## Validation

- `tests/unit/test_now_playing_post.py`: posts after a download delay (current
  flips None→track); no post for queue-only (current unchanged → timeout); no
  post when nothing ever plays; no send when the embed builder returns None.
- Full unit suite green.
- Post-deploy: a fresh "play X" should show the card a beat after the track
  actually starts; `agent.now_playing_posted` should log for fresh plays, not
  just skips.
