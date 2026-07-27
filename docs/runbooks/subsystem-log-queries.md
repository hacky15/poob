---
type: runbook
status: active
date: 2026-06-15
tags: [logging, observability, voice, music, scanner, deploy, docker]
related: [[per-subsystem-component-logging]] [[persistent-voice-log]] [[production-log-access]]
---

# Pull one subsystem's logs (component-filtered, lossless)

## When to run this

Auditing a single subsystem in isolation — VC session, music, scanner, brain routing, discord — with zero cross-contamination and zero missed lines. Use this instead of `grep -ivE 'graphql|patrol|...'` on `docker logs`, which is ANSI-cluttered and can silently drop lines.

Two sources, both on the `poob-data` volume so they survive redeploys (see [[per-subsystem-component-logging]]):

- **`/app/data/logs/poob.jsonl`** — every event, one JSON object per line, tagged with `component`. The machine path: filter losslessly with `jq`.
- **`/app/data/logs/voice.log`** — the curated human-readable interactive stream (voice + brain + music). The fast path for a live voice incident: `tail -f`.

`component` is the first dotted segment of the logger name — non-exhaustive, since any new subsystem appears automatically: `voice`, `brain`, `music`, `scanner`, `discord`, `llm`, `skills`, `agent`, `browser`, `sites`, `cogs`, `cache`, `quota`, `resilience`, `login_server`, `utils`, `main`. The stdlib voice loggers (voice_compat DAVE, discord WS 4014) are tagged `voice`. Only events at or above the running `log_level` (INFO in prod) reach the file.

## Procedure

All commands assume `ssh homelab` works (Tailscale up, key auth — see [[production-log-access]]). `jq` runs locally on your machine; pipe the file contents out over SSH.

### One subsystem (lossless, no ANSI, no scanner noise)

```bash
# All music events
ssh homelab "docker exec poob cat /app/data/logs/poob.jsonl" | jq -c 'select(.component=="music")'

# Just voice
ssh homelab "docker exec poob cat /app/data/logs/poob.jsonl" | jq -c 'select(.component=="voice")'

# Full interactive surface (voice + brain + music) in one pull
ssh homelab "docker exec poob cat /app/data/logs/poob.jsonl" \
  | jq -c 'select(.component=="voice" or .component=="brain" or .component=="music")'
```

The interactive surface is also available pre-curated and human-readable — no jq needed:

```bash
ssh homelab "docker exec poob tail -200 /app/data/logs/voice.log"
```

### Scanner only (served by the firehose, not a curated file)

```bash
ssh homelab "docker exec poob cat /app/data/logs/poob.jsonl" | jq -c 'select(.component=="scanner")'
```

### Compact, readable projection (timestamp · level · event)

```bash
ssh homelab "docker exec poob cat /app/data/logs/poob.jsonl" \
  | jq -r 'select(.component=="voice") | "\(.timestamp) [\(.level)] \(.event)"'
```

### Time window (ISO-UTC string compare — lexical sort works on ISO timestamps)

```bash
ssh homelab "docker exec poob cat /app/data/logs/poob.jsonl" \
  | jq -c 'select(.component=="voice" and .timestamp >= "2026-06-15T20:00:00" and .timestamp < "2026-06-15T21:00:00")'
```

User is CDT (UTC−5) Apr–Nov; "8 PM CDT" → `2026-06-16T01:00:00`. See [[production-log-access]] for the conversion table.

### Errors / exceptions across all subsystems

```bash
ssh homelab "docker exec poob cat /app/data/logs/poob.jsonl" \
  | jq -c 'select(.level=="error" or .level=="critical")'

# With the rendered traceback for one subsystem
ssh homelab "docker exec poob cat /app/data/logs/poob.jsonl" \
  | jq -r 'select(.component=="scanner" and .exception) | .exception'
```

### Grep a specific event string within a subsystem

```bash
ssh homelab "docker exec poob cat /app/data/logs/poob.jsonl" \
  | jq -c 'select(.component=="voice" and (.event | test("4014|websocket")))'
```

### Rotated files (older history)

JSONL rotates at 50 MB × 5 (`poob.jsonl.1` … `poob.jsonl.5`); curated streams at 25 MB × 4. To search across rotations, concatenate in order (newest is the un-suffixed file):

```bash
ssh homelab "docker exec poob sh -c 'cat /app/data/logs/poob.jsonl.5 /app/data/logs/poob.jsonl.4 /app/data/logs/poob.jsonl.3 /app/data/logs/poob.jsonl.2 /app/data/logs/poob.jsonl.1 /app/data/logs/poob.jsonl'" \
  | jq -c 'select(.component=="music")'
```

## Verification

```bash
# Whole file is valid JSONL (no output = every line parsed)
ssh homelab "docker exec poob cat /app/data/logs/poob.jsonl" | jq -e . >/dev/null && echo "valid JSONL"

# The music gap is closed: a voice audit pull contains music events
ssh homelab "docker exec poob cat /app/data/logs/poob.jsonl" \
  | jq -c 'select(.component=="music")' | head -1
```

If `poob.jsonl` is absent, the container predates [[per-subsystem-component-logging]] — redeploy. If empty, the bot has been idle (no events at INFO+).

## Rollback

N/A — read-only operation. The JSONL sink and `voice.log` are written by the app; this runbook only reads them.
