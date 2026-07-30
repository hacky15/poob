---
type: incident
status: resolved
date: 2026-07-29
tags: [ci, deploy, github-actions, ghcr, komodo, version-skew]
related: [[no-ci-test-gate]] [[deploy-flow]] [[poob-deploy-frozen-by-dead-gha-runners]] [[ghcr-credential-expiry-freezes-komodo-deploy]]
---

# Two merges 3 seconds apart made `:latest` point at the OLDER commit

## Symptom

PRs #6 and #7 were merged within 3 seconds. Both pushes to `main` triggered
`Build and push image`, both ran **concurrently**, and both tag
`ghcr.io/…/poob:latest`. The winner was decided by finish order, not commit
order — and the older commit won:

```
17:30:46.92  Komodo redeploy  sha=2b655b9   <- PR #7, NEWEST commit
17:30:49.97  Komodo redeploy  sha=96e3b22   <- PR #6, older commit, 3s LATER
```

`:latest` ended up pointing at an image built from `96e3b22`, which does not
contain PR #7. `main` said both were merged; production was about to run one
of them. Nothing failed — both builds reported success.

Caught during post-merge verification, not by any alarm. There is no alarm
for this.

## Root cause

`.github/workflows/build.yml` had **no `concurrency` group**. Nothing
serialized builds on the same ref, and the workflow's last two steps are
"push `:latest`" then "fire the Komodo deploy webhook" — so two overlapping
runs race to define what production pulls, with the loser silently winning.

The asymmetry is self-inflicted and visible in the same run list: the
`Tests` workflow added on 2026-07-26 ([[no-ci-test-gate]]) *does* declare a
concurrency group, so PR #6's test run was cleanly cancelled when #7 landed.
Only `build.yml` was left unguarded — the guard was added to the new workflow
and never backfilled to the old one.

## Fix

A concurrency group on `build.yml`:

```yaml
concurrency:
  group: build-${{ github.ref }}
  cancel-in-progress: false
```

**`cancel-in-progress` is deliberately `false`.** Cancelling would also fix
the ordering — the older run dies, the newer wins — but this job pushes an
image and fires a **production deploy webhook**. Interrupting it mid-push or
mid-webhook trades a version skew for a half-applied deploy, which is worse
and harder to notice. Queueing costs one extra build on rapid merges and
guarantees the newest ref finishes last.

Contrast with `test.yml`, which correctly uses `cancel-in-progress: true`:
cancelling a test run wastes nothing and touches nothing outside CI. The two
workflows want opposite settings for the same reason — one has side effects,
one does not.

## Recovery

The fix is self-healing: this change is itself a commit on `main`, so its
build re-pushes `:latest` from a tree containing **both** merges. No manual
registry surgery, no forced redeploy — the next build corrects the tag as a
side effect of shipping the guard.

Verify after it lands:

```bash
ssh homelab "docker exec poob printenv GIT_SHA"   # expect this commit, not 96e3b22
```

## Known property, not a defect

`test.yml`'s `cancel-in-progress: true` means that when merges land in quick
succession, **intermediate commits do not get a full test run** — only the
newest does. That is acceptable because the newest tree contains all the
merged changes, and it is what keeps the gate fast. Recorded so it is a known
property rather than a later surprise: a green check on `main` attests to the
newest commit, not to every commit individually.

## Why this class keeps appearing

Third instance in four days of "the code is merged but production is running
something else":

- the per-subsystem logging feature that was built, tested, and never
  committed ([[no-ci-test-gate]]);
- the searxng config the repo edits but the container never reads;
- this.

Each was invisible because *every individual step succeeded*. Merges
succeeded, builds succeeded, deploys succeeded. The failure only exists in
the relationship between them, which nothing was checking. That is the
argument for verifying `GIT_SHA` after a deploy rather than trusting a green
workflow — a habit worth keeping in the deploy runbook.
