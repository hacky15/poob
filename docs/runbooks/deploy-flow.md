---
type: runbook
status: active
date: 2026-04-19
tags: [deploy, github-actions, komodo, docker]
related: [[production-log-access]]
---

# Deploy flow — `git push origin main` is the deploy

## When to run this

Any code change you want live on the homelab poob container. There is no separate deploy step.

## Procedure

1. **Commit + push to main.**
   ```powershell
   git add <paths>
   git commit -m "fix: ..."
   git push origin main
   ```

2. **Wait 3–5 minutes.** What happens downstream:
   - [GitHub Actions](https://github.com/hacky15/poob/actions) builds a Docker image from the pushed commit and pushes it to GHCR as `ghcr.io/hacky15/poob:latest`. Takes ~2–3 minutes.
   - Komodo watches `ghcr.io/hacky15/poob:latest` for a new digest. On detection it pulls the image and restarts the `poob` container. Takes another ~30–60 seconds.
   - The container sends a `Poob is alive — v{version} ({sha})` DM to the owner.

3. **Verify the deploy landed.** See below.

## Verification

```powershell
# Browser: GHA build status for the pushed commit
Start-Process "https://github.com/hacky15/poob/actions"

# Log line with running SHA (the startup DM writes this to stdout too)
ssh ben@homelab "docker logs --since 10m poob 2>&1" | Select-String -Pattern "Poob is alive"

# How long has the current container been up? Fresh deploy = seconds/minutes.
ssh ben@homelab "docker ps --format '{{.Names}}: {{.Status}}' | grep poob"
```

If container uptime is longer than "minutes since GHA finished," the deploy didn't land — first check GHA, then the Komodo Updates panel for webhook events.

## Rollback

- **Same-PR rollback**: push the revert commit. The deploy flow treats a revert like any other commit — GHA builds, Komodo redeploys.
- **Emergency manual redeploy**: Komodo UI (`http://homelab:9120` → Stacks → poob → Redeploy). Use this only when the pipeline is stuck; it still records a deploy event.

## Do NOT

- `ssh ben@homelab "docker pull ..."` — bypasses Komodo's audit trail.
- `docker restart poob` from CLI — loses the deploy-event record in MongoDB and desyncs Komodo's state.
- SSH-pulled images and CLI restarts make Komodo think the old image is current, which then makes future auto-deploys no-op until the state is reconciled.

Direct intervention is for when the pipeline itself is broken, not for "I want to redeploy faster." The right escalation is the Komodo UI.
