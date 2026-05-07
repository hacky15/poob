---
type: runbook
status: active
date: 2026-04-19
tags: [deploy, logs, tailscale, docker]
related: [[deploy-flow]]
---

# Reading production container logs on homelab

## When to run this

Debugging shipped behavior, verifying a deploy landed, auditing a specific time window, or investigating a live incident.

## Procedure

```bash
# Tail live logs
scripts/logs.sh -f poob

# Last 10 minutes
scripts/logs.sh --since 10m poob

# Specific container (poob-ollama, poob-searxng)
scripts/logs.sh --since 1h poob-ollama

# Filter for a pattern
scripts/logs.sh --since 30m poob | grep "Response complete"
```

The wrapper is thin — it just shells out to `ssh homelab "docker logs <args> <container>"`. Any flag `docker logs` accepts is forwarded.

Also used heavily via the [`poob-logs`](../../.claude/skills/poob-logs/SKILL.md) skill — preferred path for agents because it handles the SSH boilerplate.

## How the path works

1. **Tailscale MagicDNS** resolves `homelab` to its tailnet IP. Works from any device with Tailscale running and authed to the same tailnet.
2. **SSH config** on the dev machine has a `Host homelab` entry pointing at user `ben` with key auth via `~/.ssh/id_ed25519`. Password auth is disabled on the homelab side (`/etc/ssh/sshd_config.d/99-hardening.conf`), so the key is the only way in.
3. **Docker socket on homelab** is owned by the `docker` group; user `ben` is in that group, so `docker logs` runs without sudo.
4. **`scripts/logs.sh`** forwards any flag `docker logs` accepts. Not magic.

## Log retention

Global rotation in `/etc/docker/daemon.json` on homelab: `max-size: 50m`, `max-file: 5` — ~250 MB of rolling logs per container.

- High-volume containers (poob during active patrol) may rotate within hours.
- Quiet containers (poob-searxng, poob-ollama between requests) keep weeks.

- `--since` queries beyond the rotation horizon return nothing without warning. If an agent gets an empty response, widening the window won't help — that data is gone.

For deploy-event history beyond rotation (when an image landed, who triggered the deploy, env-var change history), use the **Komodo Updates panel** at `http://homelab:9120` → Stacks → poob → Updates. MongoDB-backed, persists indefinitely.

## Debugging the log path itself

If the wrapper fails, bisect manually:

```bash
ssh homelab "docker ps"                 # does docker itself respond?
ssh homelab "docker logs --tail 50 poob"  # does docker logs work?
ssh homelab echo up                     # does SSH work?
```

If `ssh homelab` itself fails, the issue is upstream (Tailscale not running, SSH key not loaded, key revoked).

## When SSH directly is the right tool

`scripts/logs.sh` is just for log queries. Other production operations use SSH directly:

```bash
ssh homelab "docker ps"                                                 # what's running
ssh homelab "docker stats --no-stream"                                  # current resource usage
ssh homelab "docker exec poob-ollama ollama list"                       # what models are loaded
ssh homelab "docker compose -f ~/apps/komodo/docker-compose.yml ps"     # komodo health
```

## Verification

The runbook works when `scripts/logs.sh --since 5m poob` returns recent lines. If it returns nothing and the container has been up for more than 5 minutes, the container is genuinely quiet — not an access problem.

## Rollback

N/A — read-only operation.
