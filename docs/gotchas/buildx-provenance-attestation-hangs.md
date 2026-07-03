---
type: gotcha
status: active
date: 2026-07-03
tags: [ci, deploy, docker, buildx, github-actions]
related: [[komodo-deploy-trigger]] [[self-hosted-runners-migration]]
---

# GHA build hangs indefinitely — two distinct buildx causes found back-to-back

## Symptom

The `Build and push image` GHA run gets stuck `in_progress` indefinitely — no
failure, just never completes. Deploys silently stop landing (the image never
reaches GHCR, so Komodo has nothing new to pull). `ps aux` on the runner shows
the `docker buildx build` process still alive but making no further progress;
`/proc/net/dev` deltas show zero network I/O — it's not slow, it's stuck.

Three consecutive hangs on 2026-07-03 while trying to ship a voice fix exposed
**two separate root causes** in sequence — fixing the first was necessary but
not sufficient.

## Cause 1 — provenance attestation export (hangs 1 & 2)

`docker logs <buildx_buildkit container>` showed the build reaching:

```
level=warning msg="forcibly turning on oci-mediatype mode for attestations" span="exporting to image"
```

...then nothing else, ever. Byte-identical stall point on two back-to-back
builds (commits `6a1e74a`, `ff9dcf7`).

**Root cause:** `docker/build-push-action@v5` defaults `provenance: true`,
adding `--attest type=provenance,mode=min,inline-only=true,...` to the
underlying `buildx build` — an extra export step writing SLSA provenance
metadata. Combined with `cache-to: type=registry,...,mode=max` on this
self-hosted runner, that export step hangs indefinitely — a known class of
friction between buildx attestations and registry-mode cache exports.

**Fix:** `provenance: false` explicitly on the `docker/build-push-action` step
in [.github/workflows/build.yml](../../.github/workflows/build.yml). Poob's
deploy doesn't consume provenance metadata — disabling it is the correct fix,
not a workaround. **Confirmed effective** (verified the actual buildx command
line switched to `--attest type=provenance,disabled=true`) but the very next
build hit Cause 2 instead — a different bug, not a symptom of this one.

## Cause 2 — corrupted registry buildcache manifest (hang 3)

With provenance disabled, the next build (commit `a3667c3`) hung at a
different, later point:

```
level=warning msg="reference for unknown type: application/vnd.buildkit.cacheconfig.v0"
```

**Root cause:** `cache-from: type=registry,ref=ghcr.io/hacky15/poob:buildcache`
was reading a cache manifest that buildkit couldn't parse — almost certainly
corrupted by one of the two prior builds' `cache-to: type=registry,...,mode=max`
push getting interrupted mid-write when that build was cancelled (Cause 1's
hangs were cancelled via `gh run cancel`, which kills the runner process
mid-push). A registry cache manifest is not atomic across a `mode=max`
multi-layer push, so a cancelled build can leave the shared `buildcache` tag
in a state a later build's `cache-from` chokes on instead of gracefully
falling back to no-cache.

**Fix:** dropped `type=registry,...` from both `cache-from` and `cache-to`,
keeping only `type=gha` (GHA's own cache — scoped per-run, not a
long-lived shared registry tag, so it can't accumulate this class of
corruption across cancelled builds).

## Lesson

Cancelling a stuck build mid-push isn't fully free — if it was writing a
non-atomic multi-part cache manifest (`mode=max` to a registry), the
cancellation can corrupt shared state that poisons the *next* build. When
troubleshooting a hung build, don't assume a cancel-and-retry cycle is
side-effect-free; check whether the stuck step was a shared-mutable write.

## If a build ever hangs again

Cancel and check the buildkit container's log for the exact stall point
*before* assuming it's either of the causes above — the log message tells you
which step is stuck, don't guess:

```bash
gh run cancel <run-id>
# on the runner host:
docker logs $(docker ps --format '{{.Names}}' | grep buildx_buildkit) --tail 20
ps aux | grep "buildx build"   # confirm the exact --attest / --cache-from flags actually in effect
```

## Validation

Post-fix build (commit carrying both fixes) must complete in the normal
~3-6 min window with a fresh GHA-cache-only cold build (first build after
this change is disabled-cache-warm, so may run closer to the ~6 min high end)
and no stall at either the attestation-export or cache-read step.
