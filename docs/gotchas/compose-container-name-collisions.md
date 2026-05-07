---
type: gotcha
status: active
date: 2026-05-06
tags: [deploy, docker, compose, komodo, infra]
related: [[deploy-flow]]
---

# `container_name:` overrides in compose cause name collisions during recreate

## Trigger

Komodo deploy fails at the "Compose Up" stage with:

```
Container <name>  Error response from daemon: Conflict.
The container name "/<name>" is already in use by container "<old-id>".
You have to remove (or rename) that container to be able to reuse that name.
```

The destroy phase succeeded, the new image was pulled successfully, but `docker compose up -d` cannot create the new container because the old one (or some unrelated container with the same name) is still claiming the name.

## Why it happens

When a compose service has `container_name: <fixed-name>` set, Docker uses that exact string instead of compose's default `<project>-<service>-<index>` pattern. Two consequences:

1. **The name is global on the docker host**, not scoped to a compose project. Two compose projects (or one compose project + a manual `docker run`) can both try to claim it. The second one to run loses.
2. **`docker compose down` only removes containers labeled with the current project**. If the conflicting container was created by a different project / different Komodo path / manual `docker run` / a previous Komodo version with a different `-p` argument, the down phase doesn't touch it. Then up phase finds the leftover and bails.

This bit poob's deploy on 2026-05-06 (post-1ccc224 rollout). The `ollama` service had `container_name: ollama` — a name shared with another project on the same host. Komodo's destroy stage removed `poob` and `poob-searxng` (which had project labels) but skipped `ollama`. Then up stage failed with the conflict, leaving the stack in an unhealthy state until manual intervention removed the orphan.

## Don't

- **Don't use unprefixed `container_name:` overrides for shared-runtime images** (databases, ollama, redis, postgres). Anything popular enough that a teammate or another stack might also use it. The collision isn't hypothetical.
- **Don't "fix" a deploy collision with `docker rm -f <name>`** as a permanent solution. That's a one-shot bypass — the next push hits the same wall. Fix the compose contract.
- **Don't remove `container_name:` overrides from the main service** (e.g., `poob`) just because of the rule above. The ergonomics of `docker logs poob` is worth keeping for the one container you reach for daily, and there's no second project competing for that name.

## Do

**Namespace every `container_name:` override with the project prefix** (`poob-ollama`, `poob-searxng`, `poob-postgres`, etc.). Compose's default project-prefix-and-index does this automatically; if you want a stable name without the index suffix, set the override explicitly with the prefix.

Example — what got fixed:

```yaml
# Before
ollama:
  image: ollama/ollama:latest
  container_name: ollama        # collides with anyone else's ollama

# After
ollama:
  image: ollama/ollama:latest
  container_name: poob-ollama   # collision-proof, still ergonomic
```

The compose **service name** (`ollama` — the YAML key) stays unchanged, so other services in the same compose still resolve it via DNS. Only the docker container name changes.

After a rename, update every place that addresses the container by name:

- `docker logs <name>`, `docker exec <name>` calls in runbooks
- `scripts/logs.sh <name>` invocations
- The `poob-logs` skill's container table
- Any monitoring / alerting that targets the container by name

DNS-based references inside the compose network do NOT need updating — they use the service name, not the container name.

## Reference

- Compose YAML that triggered the regression: `deploy/compose.yml` (commit history).
- Komodo deploy flow: [[deploy-flow]] — see the orphan-container note in "When the deploy gets stuck".
- The actual incident's full Komodo Update log was at `http://homelab:9120` → Stacks → poob → Updates → "Deploy Stack" entry timestamped 2026-05-06 21:07 (Compose Up stage stderr).
