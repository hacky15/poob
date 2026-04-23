---
name: update-docs
description: Updates the docs/ Obsidian vault as the project's working memory after every meaningful change. Picks the right note type (decision / incident / gotcha / runbook / reference / research / architecture / plan) and either updates an existing note or hands off to docs-organizer for a new note. Captures architectural decisions, bug root causes, dependency rationale, deploy config tweaks, env var additions, integration quirks, and non-obvious discoveries. Trigger before declaring ANY task complete — code changes, fixes, refactors, infra tweaks, dep adds, deploy config edits. State that isn't written down decays; future agents (and future you) will re-introduce bugs that were already fixed.
---

# Documentation Sync — Working Memory of the System

The `docs/` tree is the **single source of truth** for how Poob actually works right now. CLAUDE.md tells you to read it before changing anything; this skill enforces the other half: writing back what you learned/changed.

If you finish work without touching docs, you've created drift. Drift compounds. Future agents (you, in three weeks) will re-introduce bugs that were already fixed. **Updating docs is not optional cleanup — it's part of the change.**

## When to invoke

This skill should fire on ALL of these:

- **After a code change that touches behavior** — even small fixes. Note what changed and why.
- **After adding/changing a dependency** in `pyproject.toml`. Note why and what it enables.
- **After a deploy-config change** (Dockerfile, deploy/compose.yml, .github/workflows/build.yml). Note the operational implication.
- **After adding/renaming an env var** — both `config.py` and the env-var contract changed.
- **After integrating with a new external service** (API key added, new endpoint hit). Note quotas, gotchas.
- **After fixing a bug** — write the root cause + fix as an `incidents/` note, plus a `gotchas/` note if the hazard is durable.
- **After learning something non-obvious** while debugging — even if you didn't change code.
- **After a homelab / infra change** (Komodo config, Docker daemon, Tailscale, env var in Komodo UI).
- **After any architectural decision** — even one you decided to defer. Future you needs the reasoning.

Before you say "I'm done": did the vault change? If no, this skill probably needs to run.

## Decision: existing note or new note

**Update an existing note** when:

- The note is about the same subsystem and your change is a refinement (new provider observed, threshold tuned, latency number updated).
- The note's `status: active` still holds — the fact / decision is still true after your change.

**Write a new note** (hand off to [docs-organizer](../docs-organizer/SKILL.md)) when:

- The change introduces a new concept, bug, hazard, or decision.
- An existing note is being *superseded*, not refined — the old fact is now wrong. Mark the old one `status: superseded`, write the new note, link both ways.
- The content doesn't fit the scope of any existing note cleanly.

If unsure, err toward a new note. Atomic notes with backlinks are easier to navigate than one note that grew eleven sections.

## Where things go

Vault layout (see [docs/README.md](../../../docs/README.md) for the full contract):

| Folder | What goes there |
|---|---|
| `docs/decisions/` | Architectural choices with context, alternatives, consequences |
| `docs/incidents/` | Post-incident reviews: symptom, root cause, fix, validation |
| `docs/gotchas/` | Durable hazards — "tried X, didn't work, do Z instead" |
| `docs/runbooks/` | Copy-pasteable operational procedures |
| `docs/references/` | What we care about from third-party APIs / libraries / services |
| `docs/research/` | Open-ended investigations with findings and recommendations |
| `docs/architecture/` | Subsystem-level shape, invariants, key files |
| `docs/plans/` | Per-feature implementation plans written before coding |

If you're writing a NEW note, invoke [docs-organizer](../docs-organizer/SKILL.md) — it handles type selection, template application, foldering, and MOC updates.

If you're UPDATING an existing note, edit in place. Keep the frontmatter's `date` field unchanged (it's creation date, not last-modified).

`docs/technical_notes.md` is retained as a historical pointer only. Do not write new content into it.

## What a good update looks like

Bad (vague, no value to future agent):
> Fixed wake word bug.

Good (root cause + how the fix landed + caveats for future):
> ### Wake-word ONNX path on homelab
>
> openwakeword's `Model()` failed with `Could not find pretrained model for 'data/hey_poob.onnx'`. Root cause: `/app/data` is a named volume in `deploy/compose.yml`, which shadows anything `COPY`'d into that path at build time.
>
> Fix: COPY `hey_poob.onnx` to `/app/hey_poob.onnx` (outside the volume mount), set `PORCUPINE_KEYWORD_PATH=/app/hey_poob.onnx` in Komodo env.
>
> Future caveat: any other file we want shipped in the image must live OUTSIDE volume-mounted paths.

Good entries answer: *what was wrong, why, what fixed it, what to watch for next time.*

## Workflow — updating an existing note

1. Find the right note with grep: `grep -rn "<keyword>" docs/`.
2. Open it. Read the existing content — avoid stating something the note already says.
3. Edit in place. Add a new section or extend an existing one.
4. If the note is now superseded by your change, invoke [docs-organizer](../docs-organizer/SKILL.md) to handle the supersede properly.
5. Keep the frontmatter's `date` unchanged; update `tags` if the scope genuinely changed.

## Workflow — writing a new note

Hand off to [docs-organizer](../docs-organizer/SKILL.md) with the raw content + the subsystem(s) involved. It will pick the type, apply the template, wire up wikilinks, and update the folder MOC.

## Auditing for drift

Periodically (or when in doubt about state), do a "total state audit":

1. Read `.claude/CLAUDE.md` end-to-end. Anything stale (versions, paths, conventions)?
2. Skim every folder README in `docs/` — `decisions/README.md` etc. Any entries pointing to deleted code, deprecated patterns, or resolved-then-forgotten incidents?
3. Check `pyproject.toml` deps against what's actually imported in `src/poob/` — orphan deps or undeclared imports?
4. Check `.env.example` against `config.py` fields — match?
5. Verify `deploy/compose.yml` services match what `scripts/logs.sh --list` shows running on homelab.

Fix drift you find. Supersede notes that reference removed code. Delete notes that are truly dead (deprecated patterns that nobody would re-reach for).

## Don't over-document

- Don't write tutorials for things the code already explains via good naming.
- Don't paste long stack traces — summarize the failure mode.
- Don't write a doc entry for a typo fix or formatting change.
- Don't preserve outdated content "for history" — git log keeps history. The vault is present-tense.

The bar is: **would a future agent reading this save time vs. having to re-derive it?** If yes, write it. If no, skip.

## Comment-style rule applies to vault writing

Keep vault prose tight. Incident narratives, user quotes, specific dates belong in incident notes — not everywhere those incidents are mentioned. Comments and prose both follow the same rule: say what the thing is; link out for the story.

## Verification discipline — before declaring done

Every "I updated the docs" needs a verification step:

1. **`git status`** — verify the doc file(s) you touched are modified / untracked as expected. If nothing shows modified, your edit didn't land.
2. **`git diff <doc-file>`** — scan the diff. Does it say what you think? Any accidental Markdown breakage?
3. If you committed: `git log -1 --stat` — confirm the commit contains the doc file.
4. If the doc references code you also changed, re-read the code one more time to catch docs-say-X / code-says-Y drift.
5. If the change is infra / deploy related, invoke [poob-logs](../poob-logs/SKILL.md) to confirm production matches the documented claim.

## Cross-references

- [docs-organizer](../docs-organizer/SKILL.md) — writing a new note from scratch
- [shell-ops](../shell-ops/SKILL.md) — safe git / commit patterns on PowerShell
- [poob-logs](../poob-logs/SKILL.md) — verify deployed state matches claims
- [run-tests](../run-tests/SKILL.md) — when docs describe test behavior, confirm tests pass
