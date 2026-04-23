---
name: docs-organizer
description: File a new or unsorted note into the Obsidian vault under docs/. Classifies content by note type (decision / incident / gotcha / runbook / reference / research / architecture / plan), chooses the correct folder, applies the matching template frontmatter, wires up wikilinks to related notes, and updates the folder's MOC (README.md). Use this skill when you have raw prose to save, an inbox note to triage, or an existing note that's in the wrong folder. Also handles batch operations (converting multiple loose docs) and "does this belong as one note or two" decisions.
---

# docs-organizer — file notes into the vault correctly

The vault at `docs/` is organized by **document type**, not topic. Topic emerges from tags and wikilinks. This skill handles the act of filing: picking the right type, the right folder, the right frontmatter, and the right links.

## When to invoke

- User dropped raw prose into `_inbox/` or a chat and said "file this."
- An incident / fix / decision just landed and the agent needs to write it up.
- An existing note turns out to be in the wrong folder.
- Multiple loose notes need to be atomized (one concept per note) and filed in bulk.
- You're about to declare a task complete and haven't yet written the corresponding vault entry.

Skip this skill when: the change is trivially one-note ("rename function X"), or you already know exactly where it goes and just need to write it.

## Decision tree — which note type

Use this priority order. Stop at the first match.

1. **incident** — Did something break or misbehave, then get fixed? Symptom + root cause + fix. Goes in `docs/incidents/`.
2. **gotcha** — Did we learn a durable hazard that will bite again if forgotten? Library quirk, platform surprise, tempting-but-wrong fix. Goes in `docs/gotchas/`.
3. **decision** — Did we make an architectural choice with non-trivial downstream consequences? Cascade order, thresholds, framework pick. Goes in `docs/decisions/`.
4. **runbook** — Is this copy-pasteable steps for a recurring operational task? Goes in `docs/runbooks/`.
5. **reference** — Is this what we care about from an external API / library / service? Goes in `docs/references/`.
6. **research** — Is this an open-ended investigation with findings and a recommendation? Goes in `docs/research/`.
7. **architecture** — Is this a subsystem-level description with shape, invariants, key files? Goes in `docs/architecture/`.
8. **plan** — Is this a per-feature implementation plan written before coding? Goes in `docs/plans/`.

One concept per note. If the content covers two things, split first, file second.

Incidents vs gotchas: an incident is a specific past event; a gotcha is the durable hazard extracted from it. A single incident can spawn one or more gotchas. Write both when the hazard will recur.

## Procedure

### 1. Choose the type

Run the decision tree against the content. If two types both fit, pick the one that will answer the most future queries (usually "gotcha" over "incident" if the hazard is durable, or "decision" over "architecture" if the content is primarily *why*).

### 2. Pick a filename

Kebab-case, descriptive, specific. Examples:

- `wake-gate-over-rejection-during-music.md`
- `docker-volume-shadows-baked-files.md`
- NOT: `bugfix.md`, `new-stuff.md`, `april-22-incident.md`

Filenames don't carry dates — frontmatter does.

### 3. Copy the template

Templates live in `docs/_templates/<type>.md`. Read, copy the frontmatter block and body scaffolding, fill in:

- `type` — matches the folder
- `status` — `active` | `resolved` | `superseded` | `proposed`
- `date` — creation date, YYYY-MM-DD; never updated after first write
- `tags` — 2-5 tags, lowercase, singular where sensible
- `supersedes` / `superseded_by` — only if applicable
- `related` — wikilinks to adjacent notes; backlinks surface the reverse automatically

### 4. Write the body

Follow the template's sections. Keep prose tight — atomic notes are typically 30-80 lines. If a note hits 150+ lines, consider splitting.

Comment-style rule applies to prose too: no multi-paragraph narratives about past incidents inside a different note. Link out instead.

### 5. Wire up wikilinks

Every note should link to at least one other note unless it's truly standalone. Use `[[slug-of-target-note]]` or `[[slug|Display text]]`. Don't use file paths.

Backlinks panel in Obsidian surfaces the reverse direction automatically — you don't need to edit the target note to add a reverse link.

### 6. Update the folder MOC

Every folder has a `README.md` that lists entries. Add a one-line entry:

```
- [[slug]] — one-sentence hook (date if relevant)
```

Keep entries grouped logically — by subsystem for architecture/gotchas, chronologically for incidents, by importance for decisions.

### 7. Verify

- `grep -n "^type:" docs/<folder>/<slug>.md` — confirms frontmatter landed
- Open the vault in Obsidian and click the link from the folder's README.md → note should open cleanly
- Graph view (`Ctrl+G`) should show the note connected to its `related` targets

## When a note is the wrong type

If you find a note that's filed wrong (e.g. a decision sitting in `incidents/`):

1. `git mv` to the correct folder.
2. Update the `type` field in frontmatter.
3. Update both the old and new folder's `README.md`.
4. Update any notes with `related: [[old-path]]` — Obsidian will flag these as unresolved in graph view. Fix them.

## Superseding vs editing

Editing in place: the fact or decision is refined but fundamentally unchanged. Example: updating `vlm-cascade-operational-findings.md` after observing a new provider's latency.

Superseding: the fact or decision is replaced. Example: wake-word gate v1 (hard both-gates) superseded by v2 (context-aware). The old note stays as `status: superseded`, the new note links back with `supersedes: [[old-slug]]`, the old note gets `superseded_by: [[new-slug]]`.

Rule of thumb: if a future reader landing on the old note could be misled into thinking it's current, supersede. If they'd just see mildly out-of-date phrasing, edit.

## Don't

- Write new content into `docs/technical_notes.md`. It's a superseded historical pointer — see the INDEX.
- Put dates in filenames — use the `date` frontmatter field.
- Merge two unrelated concepts into one note "to save a file."
- Leave `related:` empty on a note that obviously connects to others. Graph view without edges is a smell.
- Use Markdown links `[text](path)` for vault-internal references. Use `[[wikilinks]]`.

## Quick reference

| Trigger | Type | Folder |
|---|---|---|
| Something broke, we fixed it | incident | `docs/incidents/` |
| Don't do X, do Y (durable hazard) | gotcha | `docs/gotchas/` |
| We chose A over B because C | decision | `docs/decisions/` |
| Here's how to do operation Z | runbook | `docs/runbooks/` |
| Here's what API W actually does for us | reference | `docs/references/` |
| We asked: <question>. Findings: ... | research | `docs/research/` |
| Subsystem V: shape, invariants, key files | architecture | `docs/architecture/` |
| Plan to ship feature F | plan | `docs/plans/` |

## Cross-references

- [update-docs](../update-docs/SKILL.md) — when to write vs when to skip; invoked before declaring tasks done
- [docs/README.md](../../../docs/README.md) — vault contract
- [docs/INDEX.md](../../../docs/INDEX.md) — top-level map of content
