---
name: poob-logs
description: Reads live Docker container logs from poob's homelab production over SSH+Tailscale. Pulls logs from poob, ollama, poob-searxng, or any other container. Supports tail size, since/until time bounds, ISO8601 timestamps, live follow, and grep filtering. Use when investigating runtime behavior, verifying a deploy landed, debugging a shipped exception, confirming the bot is online, or auditing what happened in a specific time window. Logs are read-only — no permission needed to query freely.
---

# Querying Production Logs on Homelab

Poob runs in Docker on `homelab` (Ubuntu Server, accessed via Tailscale). You have read access to live container logs through `scripts/logs.sh`, which wraps `ssh homelab "docker logs ..."`.

## When to invoke this skill

- User reports something happening "right now" or "earlier today" in production
- You just pushed code and want to confirm the new container started cleanly
- A bug appeared in production but not in local dev — need to see real runtime state
- Verifying a feature works end-to-end after deploy (wake word fired, music played, deal posted)
- Auditing what happened during a specific window (e.g. "what did poob do between 8 and 9 PM CDT?")
- Discord users mention odd bot behavior — pull logs from that timeframe

## Common invocations

All commands run from the repo root.

```bash
# Default: last 200 lines of poob with timestamps
scripts/logs.sh

# Specific container
scripts/logs.sh poob
scripts/logs.sh ollama
scripts/logs.sh poob-searxng
scripts/logs.sh komodo-core              # rarely needed; for orchestrator debugging

# Tail size
scripts/logs.sh poob --tail 500
scripts/logs.sh poob --tail 1000

# Time-bounded
scripts/logs.sh poob --since 10m         # last 10 minutes
scripts/logs.sh poob --since 1h          # last hour
scripts/logs.sh poob --since 24h         # last day
scripts/logs.sh poob --since 2026-04-21T01:40:00Z                                 # since absolute UTC time
scripts/logs.sh poob --since 2026-04-21T01:40:00Z --until 2026-04-21T02:00:00Z   # bounded window

# Live tail (only for active interactive debugging — don't leave running in background)
scripts/logs.sh poob -f

# What's actually running on homelab right now?
scripts/logs.sh --list
```

The wrapper forwards anything `docker logs` accepts. See `scripts/logs.sh --help` for the inline reference.

## Filtering log output

Pipe through grep/sed for targeted queries. Examples:

```bash
scripts/logs.sh poob --tail 1000 | grep -iE "error|traceback|warning"
scripts/logs.sh poob --since 1h | grep "wake word"
scripts/logs.sh poob --since 1h | grep "Bot is ready"
scripts/logs.sh poob --since 1h | grep -B 2 -A 10 "Traceback"      # tracebacks with context
```

## Time conversion (UTC → local)

Docker logs are **UTC** (Z suffix). User is in **CDT (UTC−5)** April–November, **CST (UTC−6)** otherwise.

- Log timestamp `2026-04-21T01:40:00Z` = `2026-04-20 8:40 PM CDT`
- User says "8 PM" → query `--since 2026-04-21T01:00:00Z` (bracket the hour)

## Containers reference

| Container | What's in it |
|---|---|
| `poob` | Main bot — Discord, scanner, voice, music, agent. **This is the one you usually want.** |
| `ollama` | Local LLM runtime. Logs show model load/unload + inference requests. |
| `poob-searxng` | Self-hosted search (3rd in cascade). Mostly quiet. |
| `komodo-core` | Orchestrator's own logs. Check when deploys behave weirdly. |
| `komodo-mongo` | Komodo's database. Almost never needed. |
| `komodo-periphery` | Komodo's docker-socket agent. Almost never needed. |

## Retention limits

Docker keeps **5 × 50MB rolling files per container** (~250MB) per `/etc/docker/daemon.json` on homelab.

- High-volume container (poob during voice session): may rotate within hours
- Quiet container (searxng, idle ollama): keeps weeks
- `--since` queries past rotation return nothing silently — that data is gone

For deploy event history beyond log rotation, use the **Komodo Updates panel** at `http://homelab:9120` → Stacks → poob → Updates list. That metadata persists in MongoDB indefinitely.

## When SSH alone is the right tool

For state checks that aren't log queries, SSH directly:

```bash
ssh homelab "docker ps"
ssh homelab "docker stats --no-stream"
ssh homelab "docker exec ollama ollama list"               # what models are loaded
```

For mutations (restart, redeploy, env-var change), use the Komodo UI — it tracks those as Update events for audit history. Don't `docker restart` from CLI; you lose the audit trail.

## If the script fails

- `Permission denied (publickey)` — your shell environment doesn't have the SSH key loaded. Verify with `ssh homelab "echo ok"`. The user has key auth set up at `C:\Users\19203\.ssh\id_ed25519` with a `Host homelab` SSH config entry. If your shell can't see those, the script can't either. **Known gotcha:** Claude Code's Bash-tool sandbox may not share the user's `~/.ssh/config` — from the Bash sandbox the hostname may resolve but the key won't authenticate. In that case, instruct the user to run `scripts/logs.sh` from their own PowerShell terminal and paste the output back. See [shell-ops](../shell-ops/SKILL.md) for the sandbox-vs-user-shell distinction.
- `Could not resolve hostname homelab` — Tailscale isn't running locally. Check tray icon on Windows, `tailscale status` from CLI.
- `No such container: poob` — the stack was renamed or torn down. Run `scripts/logs.sh --list` first.

## Cross-references

- [`shell-ops`](../shell-ops/SKILL.md) — if SSH fails from the Bash sandbox, or for safe invocation patterns when asking the user to run log queries
- [`update-docs`](../update-docs/SKILL.md) — if you discover something non-obvious from logs (latency spikes, provider failure patterns), capture it
