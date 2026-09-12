---
type: gotcha
status: active
date: 2026-09-11
tags: [voice, dual_pipeline, latency, vad]
related: []
---

# The 2000ms addressed-silence threshold looks like free latency to cut — it isn't

## Trigger

`dual_pipeline.py`'s `_SILENCE_THRESHOLD_ADDRESSED = 100` (2000ms) is the
single biggest latency cost on every wake-word turn: measured live, a
short "Hey, Poob. Who—" logs an utterance duration of ~2.7s, almost
entirely the mandatory silence tail, not the speech itself. Before adding
LLM think-time on top, that's a consistent ~2s of dead air on every
addressed request, in a perfectly quiet room with zero contention. It
looks like an obvious, free win to shorten.

## Why it happens

Once the wake word fires, the pipeline waits for this much silence before
deciding the person is done talking — deliberately more patient than the
passive-listening threshold (`_SILENCE_THRESHOLD_PASSIVE`, 1000ms), per
the code's own comment: "otherwise we cut off the addressed utterance
halfway through the request and the actual song / question arrives 1-2
seconds later as a passive continuation."

## Don't

Lower `_SILENCE_THRESHOLD_ADDRESSED` to cut latency. This was raised to
its current value specifically to stop a real, previously-observed failure
mode: Poob responding to only the first half of a request because someone
paused mid-sentence, with the rest of what they said falling through as
unaddressed passive chatter.

## Do

If this comes up again, treat it as an explicit, already-litigated
tradeoff — the operator was asked directly whether to lower it (2026-09-11)
and said no: people need to be able to finish their sentences. Any future
latency work here should look at everything *else* in the addressed-turn
path first (LLM first-token time, the response-serialization lock behavior
during multi-speaker bursts), not this threshold.

## Reference

`src/poob/voice/dual_pipeline.py:853-857` (threshold definitions),
`:1107-1117` (where it's applied). No incident note — this was a
proactive latency audit, not a bug report.
