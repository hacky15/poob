---
type: gotcha
status: active
date: 2026-07-06
tags: [deploy, komodo, ghcr, docker, auth, infra, homelab]
related: [[komodo-mangled-container-name-wedges-deploy]] [[poob-deploy-frozen-by-dead-gha-runners]] [[komodo-deploy-trigger]]
---

# An expired GHCR token in komodo-periphery silently freezes every deploy

## Trigger

`git push` → GHA build **succeeds** (image pushed to GHCR) → but the homelab
container never updates, even after a **manual Komodo UI Redeploy**. The
running container stays on an old `GIT_SHA` indefinitely; `docker ps` shows it
healthy with long uptime (never recreated). This is the third distinct "built
but not deployed" failure mode — distinct from
[[komodo-mangled-container-name-wedges-deploy]] (bad container name) and
[[poob-deploy-frozen-by-dead-gha-runners]] (build never ran).

## Tell

```bash
# From the homelab. The smoking gun:
docker exec komodo-periphery docker pull ghcr.io/hacky15/poob:latest
#   -> Error response from daemon: error from registry: unauthorized
```

The local `:latest` tag is frozen at an old image (`docker inspect
ghcr.io/hacky15/poob:latest --format '{{.Created}}'` predates recent builds),
because `compose pull` can't authenticate to GHCR, so it silently keeps the
stale image and `up -d` sees "no change" → no recreate. Komodo may report the
redeploy as fine; the pull failure is buried.

## Why it happens

Komodo pulls images via the **`komodo-periphery` container**, which runs
`docker compose` against the host socket (bind-mounted) but authenticates using
the docker config **inside the periphery container**: `/root/.docker/config.json`
(`HOME=/root`, `DOCKER_CONFIG` unset). Periphery's own Komodo config has
`docker_registries: []` (empty) — so it relies entirely on that ambient docker
login, NOT on a Komodo-managed registry credential.

That `/root/.docker/config.json` lives in the periphery container's own
writable layer (no bind mount for it). It held a `ghcr.io` auth entry, but the
**token expired** (classic GitHub PATs expire; a short-lived token never
refreshes). Once expired, every pull 401s. The host user `ben` has **no**
ghcr.io entry at all, so a plain host-side `docker login` does nothing for
Komodo — the credential must land *inside periphery*.

Observed 2026-07-06: the token was valid just long enough for one deploy
(`c0647a0` at 03:30 UTC) then expired; the next commit (`060980a`) sat on GHCR
for ~13h through multiple redeploy attempts, none landing, until the periphery
login was refreshed.

## Fix (immediate)

Log a fresh token into GHCR **inside the periphery container** (not the host):

```bash
# On the homelab. Token = a GitHub classic PAT with read:packages scope.
echo '<FRESH_PAT>' | docker exec -i komodo-periphery docker login ghcr.io -u hacky15 --password-stdin
# verify:
docker exec komodo-periphery docker pull ghcr.io/hacky15/poob:latest   # -> Status: Downloaded newer image
```

Then Komodo UI → Redeploy. The container recreates on the new image. Verify per
[[komodo-deploy-trigger]]: `docker exec poob printenv GIT_SHA` matches HEAD.

## The durable fix (do this so it can't recur)

The periphery login is **ephemeral** — it lives in the periphery container's
writable layer and is lost if periphery is ever recreated (image update,
host reboot with `--rm`, stack redeploy of the Komodo stack itself). Two
durable options, either removes the "re-login every few months" toil:

1. **Komodo-managed registry credential** (preferred): add a GHCR provider
   account in the Komodo UI (Settings → Providers/Registries) or populate
   `docker_registries` in periphery's config with a long-lived credential.
   Komodo then injects auth into pulls itself, independent of the ambient
   docker config.
2. **A non-expiring token** with `read:packages` only, plus a bind-mount of a
   host `config.json` into periphery at `/root/.docker/` so the login survives
   container recreates.

Until one of those is in place, a token expiry silently re-freezes deploys.

## Prevent / detect

- After a push, verify the deploy by the RUNNING container's `GIT_SHA`, never
  by "the build went green" or "I clicked Redeploy" — both can succeed while
  the pull silently 401s. (Same discipline as
  [[poob-deploy-frozen-by-dead-gha-runners]], different layer.)
- If a redeploy doesn't recreate the container (uptime doesn't reset), run the
  `docker exec komodo-periphery docker pull ...` check FIRST — it's a one-line
  confirmation of this exact failure.
