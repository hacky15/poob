---
type: decision
status: active
date: 2026-05-13
tags: [ci, infra, github-actions, runners, komodo, homelab, cost]
related: [[deploy-flow]] [[compose-container-name-collisions]]
---

# CI runs on self-hosted homelab runners — `runs-on: [self-hosted, linux, x64, poob]`

## Context

The monthly $2 GitHub-hosted Actions budget on the hacky15 user account was exhausted by a permitscope CI burst on 2026-05-11. Private-repo Actions billing is $0.008 per Linux minute; that's ~250 minutes/month at the cap. With poob CI runs averaging 2-7 min each plus permitscope's 2-3 min builds, the combined load on one shared cap was unsustainable.

Permitscope migrated to a self-hosted runner stack on 2026-05-11 (see `hacky15/permitscope:vault/10-Decisions/2026-05-11-containerized-runners-komodo-stack.md`) — four containerized replicas managed by Komodo on the homelab, registered against `[self-hosted, linux, x64, permitscope]`. Cost dropped to zero compute. The runner image (`ghcr.io/hacky15/permitscope-gha-runner:latest`) is **fully repo-agnostic at runtime** via env vars, so the same image works for poob with different labels.

Poob's CI was still on `ubuntu-latest`, paying the cap while sitting next to a perfectly capable homelab. This decision migrates poob to its own stack of two replicas on the same image.

## Decision

Three pieces, shipped across the commits `5de551a` (embed feature, unrelated but on the same push), `f34b815` (compose file), and this one (`runs-on:` flip + vault note):

### 1. `deploy/gha-runner.compose.yml` lives in poob's repo

Mirrors `hacky15/permitscope:docker/gha-runner.compose.yml`, adapted for poob:

- Image: `ghcr.io/hacky15/permitscope-gha-runner:latest` (unchanged — repo-agnostic via env)
- Env: `RUNNER_REPO=hacky15/poob`, `RUNNER_LABELS=self-hosted,linux,x64,poob`, `RUNNER_NAME_PREFIX=poob-runner`
- `deploy.replicas: 2` (vs permitscope's 4 — poob CI runs less frequently)
- `memory: 2G` per replica (defensive ceiling — onnxruntime + playwright install can spike)
- Healthcheck: `.runner` file present AND `pgrep -f Runner.Listener` finds the process
- Mounts `/var/run/docker.sock` so `docker buildx` inside the runner hits the host daemon (= the same buildkitd + registry cache permitscope uses)
- No `network` block — runner only needs egress to github.com, default bridge is fine

### 2. Komodo stack `poob-gha-runners`

Created via the Komodo API on 2026-05-13:

- Repo source: `hacky15/poob` main, file path `deploy/gha-runner.compose.yml`
- `auto_pull`, `poll_for_updates`, `auto_update`, `webhook_enabled` all on
- `GH_PAT` set in the per-stack Environment panel — same `gho_` user token permitscope uses (covers the whole hacky15 account)
- Deploys automatically on registration

Two containers come up — `poob-gha-runners-poob-gha-runner-1` and `-2` — each registering with GitHub as `poob-runner-<container-id-prefix>` with labels `[self-hosted, Linux, X64, poob]`.

### 3. `.github/workflows/build.yml` flips `runs-on:`

```yaml
# Before
runs-on: ubuntu-latest

# After
runs-on: [self-hosted, linux, x64, poob]
```

Workflow body is otherwise untouched. The build still pushes to `ghcr.io/hacky15/poob:{latest,<sha>}`; the registry-cache strategy stays identical; `GITHUB_TOKEN` permissions unchanged.

Sequencing matters: this flip lands ONLY after the runners are confirmed online. Verification before push:

```bash
curl -s -H "Authorization: token <PAT>" \
  https://api.github.com/repos/hacky15/poob/actions/runners | jq .total_count
# 2
```

The compose file landed in commit `f34b815` first, then Komodo pulled + deployed, then this commit ships the flip. Three-commit sequence prevents the "runs-on requires self-hosted but no runners exist" deadlock.

## Why poob owns the compose file (not permitscope)

The handoff prompt from the permitscope agent assumed `docker/poob-gha-runner.compose.yml` would land in permitscope's repo. That file was never shipped on the permitscope side — verified by inspecting the permitscope repo-cache on the homelab (`gha-runner.compose.yml` exists, the poob-prefixed one does not). Rather than block on cross-repo coordination, poob owns its own runner compose under `deploy/` — the same dir that holds the main `compose.yml`. The pattern: **each repo owns its runner stack config; the runner IMAGE is shared cross-repo.**

This is a clean separation:
- Image (built / pushed) → lives in permitscope where the original CI infra was built
- Per-repo compose + Komodo stack → lives in the consumer repo
- GH_PAT → set once per stack in Komodo's Environment panel; same value works across stacks because it's a user-level token

If a third repo joins later (poob-plugins, etc.), it follows the same pattern: drop a `deploy/gha-runner.compose.yml` in the new repo, register a third Komodo stack pointing at it, set `GH_PAT` in the Environment panel.

## Alternatives considered

- **Wait for permitscope to ship `docker/poob-gha-runner.compose.yml` per the handoff.** Slower and creates an awkward cross-repo dependency where poob's CI infra config lives in someone else's tree.
- **Convert `hacky15` user account → GitHub Org and share runners at org level (Path B in the handoff).** Cleaner long-term but requires reconfiguring every API token, every gh CLI auth, and every GHA workflow ref. Not worth the migration cost for two repos. Revisit if/when a fifth repo joins.
- **Self-host on Komodo with the `gha-runner.compose.yml` from permitscope's repo.** Komodo would have to point a poob stack at permitscope's repo source, which couples poob's infra to permitscope's repo state in a confusing way. Reject.
- **Use the existing systemd `actions-runner` install on the homelab.** Permitscope already migrated off that path on 2026-05-11; reusing it would be a regression to single-replica + manual ops.

## Consequences

- Every poob CI run executes on the homelab at $0 compute. Caps the cost story permanently.
- Cold-build time roughly equivalent to GitHub-hosted runners — the i5-7500T is slower per-core than ubuntu-latest but the registry cache (which now lives next to buildkitd, not behind GitHub's cache eviction) more than makes up for it on warm rebuilds.
- Replica failures are now an operational concern (Komodo handles restart-on-crash via `restart: unless-stopped` + the healthcheck). If both replicas die, CI sits queued; Komodo's auto-update polling resurrects them within a minute.
- The `GH_PAT` lives in Komodo's Environment panel, not in the repo. Rotation: rotate at gh CLI, update the value in BOTH the permitscope and poob stacks' Environment panels via the Komodo UI.
- Workflow YAML edits in poob now need to remember the four-element label list. CI lint won't catch this; the runner just refuses dispatch.

## Validation

- Pre-flip: `curl -H "Authorization: token <PAT>" https://api.github.com/repos/hacky15/poob/actions/runners` returned `total_count: 2` with both `status: online` and the `poob` label set. (Confirmed 2026-05-13 ~02:30 UTC.)
- Post-flip: the next push to main fires the workflow on the homelab. Verify via:
  ```bash
  gh run view <id> --log | grep "Runner name"
  # Runner name: 'poob-runner-<container-id>'  (NOT 'GitHub Actions Runner …')
  ```
- The startup-DM `sha=` value in poob's container should advance to the post-flip SHA after Komodo's auto-update pulls + recreates poob (which depends on the homelab-built image, not on GitHub-hosted CI).

## Rollback

Single-commit revert on `.github/workflows/build.yml` restores `runs-on: ubuntu-latest`. Hosted-runner billing resumes immediately. The Komodo stack stays online (idle); no infrastructure teardown required. Reverting via Komodo UI's "Destroy Stack" cleanly removes the runners from GitHub's registered-runners list via the entrypoint's de-registration handler.

## Cross-references

- `hacky15/permitscope:vault/10-Decisions/2026-05-11-containerized-runners-komodo-stack.md` — original infra decision (image build, healthcheck design, replica-naming via hostname).
- `hacky15/permitscope:vault/10-Decisions/2026-05-11-poob-runner-stack-parallel.md` — multi-repo Path A choice.
- `hacky15/permitscope:vault/80-Operations/ci-bootstrap.md` — operational runbook (rotation, scaling, image rebuild).
- [[deploy-flow]] — poob's general `git push origin main` → Komodo auto-deploy flow. Still applies; only the CI build step substrate changed.
- [[compose-container-name-collisions]] — adjacent gotcha. Doesn't apply here (Komodo uses default `<stack>-<service>-<replica>` naming pattern, not `container_name:` overrides) but worth knowing about when extending the stack.
