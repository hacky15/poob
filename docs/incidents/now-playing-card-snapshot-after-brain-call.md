---
type: incident
status: resolved
date: 2026-07-12
tags: [music, discord, agent-handler, ui]
related: [[now-playing-card-download-race]] [[music-now-playing-embed-buttons]] [[music-text-replies-are-toob]]
---

# Now-playing card never posts — track snapshot taken AFTER the brain call

## Symptom

Text play requests ("@Poob play backwoods 808 fishing", 2026-07-13 01:08 UTC)
queued and played the song, but the now-playing card (embed + control
buttons) never appeared in the channel. Operator: *"where's the discord
message activity that shows the current music playing and stuff?"*

## Root cause

Comment–code drift in `AgentMessageHandler.on_message`. The comment said:

> "We snapshot the current track *before* the brain call (which may
> queue/play as a side effect) and compare after."

…but the code took the `track_before` snapshot **after** `brain.respond()`
returned. The brain call is precisely what starts the track, and the wrap
LLM call inside it adds 1–2 s of latency — enough for the track's download
to finish and the player loop to set `current` **during** the brain call.
The "before" snapshot then already held the NEW track, so
`_post_now_playing_when_ready` (the [[now-playing-card-download-race]] fix,
which polls for `current` to *change* from the snapshot) waited 15 s for a
change that had already happened and timed out silently. The card only ever
posted when the download happened to outlast the brain call — a coin flip
that recent wrap-latency additions tilted toward "never".

## Fix

Moved the `music_cog` lookup + `track_before` snapshot **above** the brain
call, making the code match its own comment. The poll helper is unchanged
(its tests all still pass): early-start races are now caught because the
baseline predates the side effect, and late-start races were already
handled by the polling.

## Validation

- `tests/unit/test_agent_music_reply_flow.py::test_now_playing_snapshot_taken_before_brain_call`
  — the exact failure shape: brain side-effect sets `current` mid-call; the
  poster must receive the PRE-call snapshot (`None`), not the new track.
- `tests/unit/test_now_playing_post.py` — the poll helper's contract,
  unchanged and green.
- Post-deploy: text play requests should be followed by
  `agent.now_playing_posted` within a few seconds; the card appears in the
  channel with the control buttons.

## Follow-ups

- None. The lesson is general: a comment asserting ordering is not
  ordering — the flow test now pins it.
