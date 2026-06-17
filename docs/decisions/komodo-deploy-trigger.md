---
type: decision
status: active
date: 2026-06-17
tags: [deploy, ci, komodo, gha-runner, infra]
related: [[poob-deploy-frozen-by-dead-gha-runners]] [[zombie-voice-connection-blocks-autojoin]] [[homelab-host-memory-observability]]
---

# Event-driven Komodo deploy trigger from build.yml (kill the poll lag)

## Context

`build.yml` built the image and pushed it to GHCR, then **stopped**. Nothing
told Komodo to deploy. Komodo only redeployed when its `auto_update` /
`poll_for_updates` loop happened to notice the new GHCR digest — a
non-deterministic poll up to ~hourly ([[homelab-host-memory-observability]]).

Consequence (2026-06-17): a fix sat built-and-pushed in GHCR for 10+ minutes
with no deploy. Earlier the same lag let a stale image keep running long after a
fix shipped, compounding the [[zombie-voice-connection-blocks-autojoin]] outage.
"`git push` succeeded" kept reading as "deployed" when it wasn't — the same trap
as [[poob-deploy-frozen-by-dead-gha-runners]], but on the *deploy* side rather
than the *build* side.

## Decision

Add a final **event-driven trigger** step to `build.yml` that fires the poob
stack's Komodo webhook right after the image is pushed:

```yaml
- name: Trigger Komodo redeploy
  continue-on-error: true
  env:
    KOMODO_DEPLOY_URL: ${{ secrets.KOMODO_DEPLOY_URL }}
    KOMODO_WEBHOOK_SECRET: ${{ secrets.KOMODO_WEBHOOK_SECRET }}
  run: |
    body='{"ref":"refs/heads/main"}'
    sig=$(printf '%s' "$body" | openssl dgst -sha256 -hmac "$KOMODO_WEBHOOK_SECRET" | awk '{print $NF}')
    code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$KOMODO_DEPLOY_URL" \
      -H 'Content-Type: application/json' -H 'X-GitHub-Event: push' \
      -H "X-Hub-Signature-256: sha256=$sig" -d "$body")
    echo "Komodo /deploy -> HTTP $code"
    test "$code" = "200"
```

### How it works

- Komodo stacks expose action-specific webhook listeners:
  `POST {host}/listener/github/stack/{id}/{deploy|refresh}`. `deploy` does
  `compose pull && up -d` → pulls the new `:latest` image and recreates. The
  poob stack (`id 69e59853d6d44e5cd1ea615e`, `webhook_enabled: true`) was
  already listener-ready; nothing was calling it.
- Auth is GitHub-style HMAC: `X-Hub-Signature-256: sha256=<hmac_sha256(secret,
  body)>`, secret = Komodo's `KOMODO_WEBHOOK_SECRET`. The listener also checks
  the body `ref` against the stack's branch (`main`).
- **Reachability:** the self-hosted runner is a container on the homelab. It
  can't resolve the Tailscale `KOMODO_HOST` (`*.ts.net`) or `komodo-core` (other
  network), but komodo-core publishes `0.0.0.0:9120`, so the runner reaches it
  via the docker host gateway **`172.17.0.1:9120`** (verified HTTP 200). The
  full URL lives in the `KOMODO_DEPLOY_URL` secret so the topology isn't baked
  into the workflow file and is one edit to change.

### Secrets (GitHub Actions, repo hacky15/poob)

- `KOMODO_DEPLOY_URL` = `http://172.17.0.1:9120/listener/github/stack/69e59853d6d44e5cd1ea615e/deploy`
- `KOMODO_WEBHOOK_SECRET` = Komodo's `KOMODO_WEBHOOK_SECRET` (from
  `~/apps/komodo/core.env`).

## Why not the alternatives

- **GitHub repo webhook → Komodo (on push):** fires at push time, *before* the
  image is built → Komodo would pull the stale image. Must trigger post-build,
  hence the build.yml step.
- **Faster Komodo poll:** still poll-based (best ~minutes), and a global change
  affecting every stack. The webhook is deterministic and poob-scoped.
- **Komodo API (`/execute DeployStack`):** needs an API key (UI-generated) or a
  minted JWT; the webhook reuses the already-present `KOMODO_WEBHOOK_SECRET`.

## Consequences

- Push → build → **deploy in ~1 min**, deterministically. No more "pushed but
  not live" on the deploy side.
- `continue-on-error: true` + the `auto_update` poll mean a trigger miss is
  non-fatal (poll still catches it); the step goes red in the Actions UI as a
  signal without failing the build.
- The runner reaching the host via `172.17.0.1` assumes the default docker
  bridge gateway; if docker networking is reconfigured, update
  `KOMODO_DEPLOY_URL`.

## Validation

- Manual `POST …/stack/<id>/deploy` returned HTTP 200 (route + HMAC confirmed);
  `/redeploy` 400 confirmed the valid actions are `refresh`/`deploy`.
- First push carrying this step is the live end-to-end test: the build's
  "Trigger Komodo redeploy" step logs `HTTP 200` and the new SHA deploys within
  ~1 min of build completion (verify via `docker exec poob printenv GIT_SHA`).
