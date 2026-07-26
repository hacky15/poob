---
type: decision
status: active
date: 2026-07-26
tags: [ci, github-actions, testing, observability, process]
related: [[voice-session-never-reestablished-mid-flight]] [[referential-play-query-searched-literally]] [[per-subsystem-component-logging]] [[poob-deploy-frozen-by-dead-gha-runners]]
---

# CI now runs the tests — it never did before

## Context

Operator, 2026-07-26, after the third consecutive round of "you fixed it and
something else broke":

> *"we are always running in circles — you claim to fix the issues, something
> gets fixed but more things either aren't fixed or pop up as new. just fix
> this all once and for all or at the very least set us up for next time to
> be able to surely know what the issue is."*

The 07-26 production audit found the mechanical reason this kept happening.
`.github/workflows/` contained exactly one workflow: `build.yml` — build the
image, push to GHCR, ping Komodo. **Nothing ever ran `pytest`.** The repo has
~1,950 unit tests and a TDD-first convention in CLAUDE.md, and not one of
them had ever gated a merge.

Two concrete things the audit found sitting in that blind spot:

1. **`test_music_tool_stays_under_budget` was RED on `main`** — since
   `b7d78cf` took `MUSIC_TOOL` from 1216 to 1352 tokens against a 1350
   budget. That budget exists to protect Groq's 8000 tokens/minute ceiling
   ([[slim-tool-schemas]], [[groq-daily-token-cap-degrades-routing]]), so it
   was quietly costing headroom on **every routing call** for weeks.
2. **A complete, tested per-subsystem logging feature was never committed** —
   390 lines in `utils/logging.py`, 280 lines of passing tests, four vault
   notes, and a `poob-logs` skill update that instructs every future agent to
   read `/app/data/logs/poob.jsonl`. That file **did not exist in
   production**, because the code was never shipped. Every session read the
   doc, ran the documented command, got nothing, and fell back to grepping a
   7 MB interleaved log. See [[per-subsystem-component-logging]].

3. **`test_boob_probability_in_expected_range` was ALSO red on `main`** —
   found while staging this very change. `BOOB_PROBABILITY` was retuned
   from `0.05` to `1/75 ≈ 0.0133` and the constant shipped, but the test
   still asserted `0.03 <= p <= 0.07`, and the note+test update was left
   uncommitted. Verified by experiment: stash the working-tree fix, run the
   committed test, watch it fail `assert 0.03 <= 0.013333`.

That second one is the circle, literally: the tool built to make debugging
possible was itself invisible, so each session re-learned the same lesson
from scratch. The third is the same shape — a finished fix left on the
floor because nothing was watching.

Three red tests on `main`, none of them noticed by anyone, is not a
discipline problem. It is a missing gate.

Six more tests were also failing locally on Python 3.12+ (`asyncio.
get_event_loop()` raises rather than warns), which meant the suite was
**un-runnable off the container's Python** — a quiet disincentive to run it
at all. Fixed in the same change at the source (`get_running_loop()` with a
test-path fallback in `music/player.py`), rather than pinning developers to
3.11.

## Decision

Add `.github/workflows/test.yml`: run the unit suite on every pull request
and every push to `main`.

- **Unit tier only.** Integration and e2e need live browsers, Discord, and
  provider credentials; gating on them would make the gate flaky, and a
  flaky gate gets ignored — which is how we got here.
- **Self-hosted runners**, same labels as `build.yml`
  (`[self-hosted, linux, x64, poob]`) — the hosted-runner budget cap is why
  `build.yml` moved there. If this job stops picking up work, see
  [[poob-deploy-frozen-by-dead-gha-runners]].
- **Python 3.11**, matching the container (openwakeword → tflite-runtime has
  no 3.12 wheels).
- **Ruff runs but does not block** (`|| true`). The repo carries pre-existing
  findings that predate this gate; making it blocking on day one would mean
  either a huge unrelated cleanup commit or an immediately-disabled gate.
  It reports so new code is visible. Flip to blocking once the backlog is
  cleared — that is a deliberate follow-up, not an oversight.
- **`--maxfail=5`** so a broad breakage reports fast instead of grinding
  through 1,900 tests.

## Alternatives considered

- **Gate on the full suite (unit + integration + e2e).** Rejected: needs
  secrets and live services in CI; flakiness would erode trust in the gate.
- **Run tests inside `build.yml`.** Rejected: a test failure would block the
  image build, coupling "is this correct" to "can we deploy". Separate
  workflows fail independently and report separately.
- **Pre-commit hooks instead.** Rejected as the *primary* gate — trivially
  bypassed with `--no-verify` and does nothing for work pushed from another
  machine. Complementary, not a substitute.
- **Make ruff blocking immediately.** Rejected for now, see above.

## Consequences

- A red test can no longer reach `main` unnoticed. The two regressions above
  are exactly the class this stops.
- The suite must stay green and fast. It runs in ~7 minutes locally;
  `timeout-minutes: 20` is the backstop.
- New tests are now load-bearing infrastructure, not documentation. This
  raises the cost of writing a flaky test — correctly.
- Being on self-hosted runners means CI availability is coupled to homelab
  health. Accepted: the same is already true of deploys.

## Validation

Full unit suite green locally before this shipped, including the six tests
that the `get_event_loop()` fix un-broke. The first PR carrying this
workflow is its own proof — if the gate does not run, or runs and fails,
that is visible immediately rather than in three weeks.
