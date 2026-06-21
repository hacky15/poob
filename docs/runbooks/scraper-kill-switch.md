---
type: runbook
status: active
date: 2026-06-20
tags: [scanner, patrol, deploy, infra, voice]
related: [[voice-cpu-starvation-on-shared-host]] [[patrol-backoff-during-voice]]
---

# Scraper kill-switch — turn the marketplace patrol off / on

The marketplace patrol (scanner) and the voice/music pipeline run in **one
poob process** on a shared 4-core host. When the host is dedicated to voice,
the patrol can be turned off entirely with one switch — no code change.

## The switch

`patrol_scheduler_auto_start` (env `PATROL_SCHEDULER_AUTO_START`) gates
`scheduler.start()` in [main.py:778](../../src/poob/main.py#L778). When `false`,
the patrol scheduler is never started — zero scanner CPU, zero browser, zero
GraphQL — but the Discord bot, voice, and music run normally.

It is set in [deploy/compose.yml](../../deploy/compose.yml) under the `poob`
service `environment:` block (which overrides `.env`), so it is **git-tracked,
visible, and survives redeploys**.

## Turn the scraper OFF

```yaml
# deploy/compose.yml → services.poob.environment
PATROL_SCHEDULER_AUTO_START: "false"
```

Commit + push → Komodo redeploys (git-backed stack at
`/host-apps/stacks/poob/deploy/compose.yml`) and recreates the container with
the patrol disabled.

## Turn the scraper BACK ON

Set the value to `"true"` (or delete the line — the code default is `True`),
then commit + push. Komodo recreates and the patrol auto-starts again.

```yaml
PATROL_SCHEDULER_AUTO_START: "true"
```

## Verify

```bash
# OFF: this line is ABSENT from the logs after boot
docker logs poob --since 2m | grep "Patrol scheduler auto-started"
# OFF confirmation: this line IS present
docker logs poob --since 2m | grep "Patrol scheduler NOT auto-started"
# Bot still healthy either way:
docker logs poob --since 2m | grep "Bot is ready"
```

## Notes

- This is the clean, full off-switch. `patrol_skip_during_voice` (the
  [[patrol-backoff-during-voice]] cooperative backoff) only *pauses* the patrol
  during active VC; the kill-switch stops it outright.
- `config.patrol_enabled` (config.py:322) exists but is **not wired to
  anything** — do not use it expecting an effect. `patrol_scheduler_auto_start`
  is the switch that actually gates the scanner.
- For an immediate local toggle without a full git redeploy, edit the box's
  `/host-apps/stacks/poob/deploy/compose.yml` + `docker compose up -d poob` —
  but push the same change to git so it isn't lost on the next deploy.
