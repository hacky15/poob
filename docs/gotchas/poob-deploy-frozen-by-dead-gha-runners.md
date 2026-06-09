---
type: gotcha
status: active
date: 2026-06-08
tags: [deploy, ci, infra, gha-runner, komodo, gotcha]
related: [[groq-failfast-client]] [[provider-circuit-breaker]] [[groq-daily-cap-routing-storm]]
---

# "Pushed but not live": poob deploys ride a shared GHA-runner image that can die silently

## The hazard

Poob's prod image is built by **self-hosted GitHub Actions runners on the
homelab** (`.github/workflows/build.yml`, `runs-on: [self-hosted, linux, x64,
poob]`) → pushed to GHCR → Komodo redeploys. Those runners use an image owned by
the **other project**: `ghcr.io/hacky15/permitscope-gha-runner:latest` (built
from the permitscope repo). Poob only *consumes* it.

**2026-06-04 → 2026-06-08 that image's runners crash-looped** (all 6, both
projects, `exit=1`, RestartCount ~1360). Root cause (fixed in permitscope
`2bb2294`): a host OOM SIGKILL left a stale `.runner` file in the container's
writable layer; the entrypoint ran `config.sh` unconditionally on restart →
`config.sh` aborts *"already configured"* → `set -e` exits 1 → docker restart
policy loops forever. `--replace` does NOT help (it only resolves the *remote*
name collision, not the *local* `.runner`).

**Consequence:** for 4 days **no poob image built**. `git push` succeeded,
GitHub queued the build, and it sat forever waiting on dead runners. Prod stayed
frozen at `34e7e67` (2026-06-04) while `f49761d` (fail-fast), `78a2fd6`
(now-playing), and `18fa167` (circuit-breaker) all sat on `main` looking
"shipped". Komodo kept redeploying the *same stale image*, so even the container
restarts looked healthy.

## The trap

`git push` success ≠ deployed. The whole point of these fixes (e.g. the routing
circuit-breaker) is invisible until the image actually rebuilds.

## What to do instead

**Verify a deploy by inspecting the RUNNING container, not the push:**

```bash
# image age + the commit baked in
docker inspect ghcr.io/hacky15/poob:latest -f '{{.Created}}'
docker exec poob sh -c 'echo $GIT_SHA'
# or grep a code marker unique to your change
docker exec poob grep -c '_provider_cooldown' /app/src/poob/brain/poob.py
```

If the GHCR image `Created` time is older than your push, or `$GIT_SHA` doesn't
match HEAD, **it did not deploy** — check the runners:

```bash
docker ps -a --filter name=poob-gha-runner --format '{{.Names}} {{.Status}}'
# Restarting / high RestartCount = the build pipeline is dead.
```

Recovery when the runner image itself is broken is a **bootstrap rebuild**
(can't build the fix via the dead runners): rebuild `permitscope-gha-runner`
manually (`docker buildx build --push`) or via a one-off `ubuntu-latest` run,
then recreate both runner stacks. Stacks live on the host at
`/home/ben/apps/stacks/{poob,permitscope}-gha-runners/` (root-owned; recreate
via the `komodo-periphery` container's root context).

## See also

The 4-day freeze also masked the [[groq-daily-cap-routing-storm]] fixes — the
fail-fast client and circuit-breaker were written + merged during the freeze, so
their first real production exposure is whatever deploy *follows* this note.
