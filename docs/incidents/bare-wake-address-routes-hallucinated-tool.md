---
type: incident
status: resolved
date: 2026-07-11
tags: [brain, routing, voice, stt, tool-calling]
related: [[tool-hallucination-from-passive-context]] [[control-command-misroute-by-weak-rung]] [[deepgram-zombie-stream-no-transcript]]
---

# A bare "Hey, Poob." silently enabled autoplay — tool hallucinated from history on a content-free address

## Symptom

Production `voice.log`, 2026-07-11 04:07:

```
04:07:01  Utterance complete user=Ben duration_ms=9712 wake_word=True transcript=Hey, Poob.
04:07:01  poob.tool_route  args={'action': 'autoplay', 'mode': 'on'}   provider=groq
04:07:01  music.response   "[SILENT]Autoplay enabled."
04:07:01  Response complete response= total_ms=536
```

Ben spoke for **9.7 seconds** but Deepgram transcribed only "Hey, Poob." The
router was handed a content-free address — and returned `{autoplay, on}`,
echoing Ben's *"Turn auto play on"* from 17 minutes earlier (still in his
conversation history). The ack was `[SILENT]`, so Ben heard **nothing** while
a state toggle silently fired. Part of the "it does something completely
different" complaint cluster.

## Root cause

A message with no content gives the router nothing to classify, so it
back-fills an action from conversation history — the same failure family as
[[tool-hallucination-from-passive-context]], but triggered by STT dropping
the utterance body rather than by crosstalk. Any tool call produced from a
content-free message is by definition hallucinated; there is no valid
routing for "Hey, Poob." other than a casual reply.

## Fix

`PoobBrain._is_content_free(clean_message)`: strips the speaker-attribution
head ("Ben: …") and the wake/address token ("Hey, Poob.", including the
documented phonetic mis-hears) — if **any** head-stripped variant reduces to
fewer than 2 characters, the message is content-free. Both `respond()` and
`respond_streaming()` check it **before** tool detection: content-free →
skip `_groq_with_tools` entirely (also saving the routing call) and answer
casually ("what's up"-style), which is the correct response to being called
with nothing to say.

Narrow by design: any real remainder ("Hey, Poob. Hmm.") routes normally.
Log signal: `poob.content_free_skip_tools`.

## Validation

- `tests/unit/test_routing_rules.py`: bare-address variants detected
  (including the attribution shape); content-bearing messages untouched;
  respond() bypasses even a router that WOULD return a tool (the prod
  hallucination, mocked) and answers via the casual path; a normal "play X"
  still routes through the router once.
- Full brain/music suites green.
- Post-deploy: a lone "Hey, Poob." should produce a spoken casual reply and
  a `poob.content_free_skip_tools` log line — never a `poob.tool_route`.

## Follow-ups

- The 9.7s-of-audio → 2-word transcript is a separate STT quality problem
  (Deepgram dropped the utterance body). If it recurs, that's its own
  incident — this fix only removes the downstream hallucination hazard.
