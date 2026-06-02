---
type: gotcha
status: active
date: 2026-06-02
tags: [deploy, komodo, docker, ops, homelab]
related: [[voice-auto-rejoin-on-restart]]
---

# A hash-prefixed `poob` container name silently wedges Komodo auto-deploy

## Trigger

`git push origin main` → GHA build **Succeeds** (image pushed to GHCR) → but the
homelab container never updates. The running container stays on an old image
for hours, `RestartCount=0`, healthy, but new commits don't go live. `gh`/CI
look fine; the bot looks fine; the deploy just silently doesn't happen.

## Tell

`docker ps -a --filter ancestor=ghcr.io/hacky15/poob:latest` shows the app
container named with a **hash prefix** — e.g. `a920baaa8bce_poob` instead of
`poob`. The local `ghcr.io/hacky15/poob:latest` image and the running
container share the same (stale) image ID, even though a newer build
succeeded. Observed 2026-06-02: container froze at the 20:28 image under
`a920baaa8bce_poob`; the 20:53 build (efc2d97) succeeded on GHCR but was never
pulled/recreated for ~3 hours.

## Why it happens

Docker assigns a `<id>_<name>` prefix when `docker run --name poob` hits a name
collision (an old `poob` container wasn't fully removed during a prior
recreate). Komodo then reconciles against a container that doesn't carry the
clean `poob` name, and its auto-deploy step stops recreating — so every
subsequent build lands on GHCR but never deploys. The build pipeline is fine;
the **deploy/recreate** step is wedged.

Side effect: every `docker ... poob` command (and the `poob-logs` skill) fails
with `No such container: poob` until the name is restored — resolve the
container dynamically meanwhile:
`C=$(docker ps --filter ancestor=ghcr.io/hacky15/poob:latest --format '{{.Names}}' | head -1)`.

## Fix

**Trigger a Redeploy in the Komodo UI** (`http://homelab:9120` → Stacks → poob
→ Redeploy). That removes the mangled container, pulls the latest GHCR image,
and recreates it with the clean `poob` name — clearing the wedge and deploying
all the pending commits at once. This is the sanctioned path (audited as a
Komodo Update event).

Do **not** fix it by CLI (`docker rename` / manual `docker run`): renaming
doesn't pull the new image, and a hand-rolled `docker run` loses Komodo's
managed env/volumes/network. Per [[poob-logs]] skill guidance, mutations go
through Komodo, not the CLI.

## Prevent / detect

- After a push, if the deploy-verify poll reports "no redeploy" for >5 min,
  check the container name immediately — don't assume the build failed.
- A clean redeploy normalizes the name; the wedge only recurs if a future
  recreate again races the old container's removal.
