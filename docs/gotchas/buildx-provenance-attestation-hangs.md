---
type: gotcha
status: active
date: 2026-07-03
tags: [ci, deploy, docker, buildx, github-actions]
related: [[komodo-deploy-trigger]] [[self-hosted-runners-migration]]
---

# `docker/build-push-action` provenance attestation hangs on this self-hosted runner

## Symptom

The `Build and push image` GHA run gets stuck `in_progress` indefinitely — no
failure, just never completes. Deploys silently stop landing (the image never
reaches GHCR, so Komodo has nothing new to pull).

`docker logs <buildx_buildkit container>` shows the build reaching:

```
level=warning msg="forcibly turning on oci-mediatype mode for attestations" span="exporting to image"
```

...and then **nothing else, ever** — no further log lines, no network I/O on
the runner (checked via `/proc/net/dev` deltas), the `docker buildx build`
process still alive in `ps aux` but making no progress. Reproduced **twice
back-to-back**, byte-identical stall point both times.

## Root cause

`docker/build-push-action@v5` defaults `provenance: true`, which adds an
`--attest type=provenance,mode=min,inline-only=true,...` flag to the
underlying `docker buildx build` invocation — an extra export step that writes
SLSA provenance metadata alongside the image manifest.

Combined with this repo's `cache-to: type=registry,...,mode=max` (registry
cache, not just GHA cache), the provenance export step hangs indefinitely on
this self-hosted-runner + buildkit + GHCR combination. This is a known class
of friction between buildx attestations and registry-mode cache exports —
not something specific to poob's Dockerfile or build content (the main image
layers build and push fine; it's specifically the attestation manifest step
that stalls).

## Fix

Set `provenance: false` explicitly on the `docker/build-push-action` step in
[.github/workflows/build.yml](../../.github/workflows/build.yml). Poob's
deploy pipeline doesn't consume or need provenance metadata (no supply-chain
policy checks it) — this is optional metadata, not a functional requirement,
so disabling it is the correct fix, not a workaround.

## If a build ever hangs again

Don't wait indefinitely — cancel and check the buildkit container's log for
the exact stall point first:

```bash
gh run cancel <run-id>
gh run rerun <run-id>
# on the runner host, if it recurs:
docker logs $(docker ps --format '{{.Names}}' | grep buildx_buildkit) --tail 20
```

If it's the SAME "exporting to image" / attestation stall despite
`provenance: false`, check for an `sbom: true` default too (same action,
same class of issue) and disable that explicitly as well.

## Validation

Two consecutive builds (for commits `6a1e74a` and `ff9dcf7`) hung at the
identical log line before this fix; the build for the commit that ships this
fix must complete in the normal ~3-6 min window (see prior successful runs in
`gh run list`) with no attestation-export stall.
