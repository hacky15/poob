---
name: update-docs
description: Updates docs/technical_notes.md as the project's working memory after every meaningful change. Captures architectural decisions, bug root causes, dependency rationale, deploy config tweaks, env var additions, integration quirks, and non-obvious discoveries. Trigger before declaring ANY task complete — code changes, fixes, refactors, infra tweaks, dep adds, deploy config edits. State that isn't written down decays; future agents (and future you) will re-introduce bugs that were already fixed. Includes a "total state audit" workflow for periodic drift checks.
---

# Documentation Sync — Working Memory of the System

The `docs/` tree is the **single source of truth** for how Poob actually works right now. CLAUDE.md tells you to read it before changing anything; this skill enforces the other half: writing back what you learned/changed.

If you finish work without touching docs, you've created drift. Drift compounds. Future agents (you, in three weeks) will re-introduce bugs that were already fixed. **Updating docs is not optional cleanup — it's part of the change.**

## When to invoke

This skill should fire on ALL of these:

- **After a code change that touches behavior** — even small fixes. Note what changed and why.
- **After adding/changing a dependency** in `pyproject.toml`. Note why and what it enables.
- **After a deploy-config change** (Dockerfile, deploy/compose.yml, .github/workflows/build.yml). Note the operational implication.
- **After adding/renaming an env var** — both `config.py` and the env var contract changed.
- **After integrating with a new external service** (API key added, new endpoint hit). Note quotas, gotchas.
- **After fixing a bug** — write the root cause + fix in "Common Pitfalls" so it can't recur silently.
- **After learning something non-obvious** while debugging — even if you didn't change code, the discovery itself is valuable.
- **After a homelab/infra change** (Komodo config, Docker daemon, Tailscale, env var in Komodo UI).
- **After any architectural decision** — even one you decided to defer. Future you needs the reasoning.

If you're about to say "I'm done" or "this is complete" — first ask: did the docs change? If no, this skill probably needs to run.

## Where things go

The docs tree has specific homes. Choose deliberately.

| File | What goes there |
|---|---|
| `docs/technical_notes.md` | **The primary log.** Architectural decisions, root-cause analyses, pitfalls, quirks of external services, infra mechanics. **MOST updates land here.** Add a new `##` section at the bottom or extend an existing topical section. |
| `docs/architecture.md` | High-level pipeline + component diagram. Update only when adding/removing major components or changing data flow. |
| `docs/plans/` | Implementation plans written *before* coding a feature. Don't update post-hoc — write a new plan if you're starting a new feature. |
| `docs/references/` | 3rd-party tool references (Komodo, openwakeword, browser-use, etc.). Add when you've researched a non-trivial library detail worth saving. |
| `.claude/CLAUDE.md` | Project conventions for agents — code style, workflow rules, where things live. Update only when conventions themselves change. |
| `.claude/skills/*/SKILL.md` | When the *invocation pattern* of a recurring task changes. |
| `README.md` | User-facing only — install, run, basic configuration. Update when external setup changes (new required env var, changed install command). |

When in doubt → `docs/technical_notes.md`.

## What a good update looks like

Bad (vague, no value to future agent):
> Fixed wake word bug.

Good (root cause + how the fix landed + caveats for future):
> ### Wake-word ONNX path on homelab (April 2026)
>
> openwakeword's `Model()` failed with `Could not find pretrained model for 'data/hey_poob.onnx'` after auto-deploy worked. Root cause: `/app/data` is a Docker named-volume mount in `deploy/compose.yml`, which **shadows** anything `COPY`'d into that path at build time. The empty volume overlays the baked-in onnx file at runtime.
>
> Fix: COPY `hey_poob.onnx` to `/app/hey_poob.onnx` (outside the volume mount), set `PORCUPINE_KEYWORD_PATH=/app/hey_poob.onnx` in Komodo env. Local dev keeps the relative `data/hey_poob.onnx` path because no volume mount is in play.
>
> Future caveat: any other model file we want shipped in the image must also live OUTSIDE `/app/data`, `/app/browser_profiles`, or any other path that gets a volume in compose.

Good entries answer: *what was wrong, why, what fixed it, what to watch for next time.*

## Workflow

1. Skim the relevant existing section in `docs/technical_notes.md` (use grep — `grep -n "<topic>" docs/technical_notes.md`). Don't duplicate; update in place.
2. If the change introduces a new topic, add a new `## Section Title` at the bottom with date in the header (`## My Topic (April 2026)`).
3. Use code blocks for commands, file paths in backticks.
4. Cross-link related entries when relevant (`see "Search Provider Cascade" above`).
5. If you discovered something while debugging that wasn't your assigned task, capture it anyway under "Common Pitfalls" or its own section.

## Auditing for drift

Periodically (or when in doubt about state), do a "total state audit":

1. Read `.claude/CLAUDE.md` end-to-end. Anything stale (versions, paths, conventions)?
2. Skim `docs/technical_notes.md` section headers. Any sections that reference removed code or deprecated patterns?
3. Check `pyproject.toml` deps against what's actually imported in `src/poob/` — are there orphan deps or undeclared imports?
4. Check `.env.example` against `config.py` fields — match?
5. Verify `deploy/compose.yml` services match what `scripts/logs.sh --list` shows running on homelab.

Fix drift you find. That's also part of this skill.

## Don't over-document

- Don't write tutorials for things the code already explains via good naming.
- Don't paste long stack traces — summarize the failure mode.
- Don't write a doc entry for a typo fix or formatting change.
- Don't preserve outdated content "for history" — git log keeps history. The docs are present-tense.

The bar is: **would a future agent reading this save time vs. having to re-derive it?** If yes, write it. If no, skip.
