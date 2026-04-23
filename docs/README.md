# Poob Docs — Obsidian Vault

This `docs/` directory is an Obsidian vault. Open it in Obsidian by pointing "Open folder as vault" at `docs/`. The `.obsidian/` config is committed, so graph view, backlinks, templates, and tag filters work out of the box.

It is also the project's working memory — CLAUDE.md tells the agent to read here before making changes and write here after.

## Structure

Notes are organized by **document type**, not topic. Topic emerges from tags and backlinks.

| Folder | What goes there |
|---|---|
| `decisions/` | Architectural choices with context, alternatives, consequences |
| `incidents/` | Post-incident reviews: symptom, root cause, fix, validation |
| `gotchas/` | Durable hazards — "tried X, didn't work, do Z instead" |
| `runbooks/` | Copy-pasteable operational procedures |
| `references/` | What we care about from third-party APIs/libraries/services |
| `research/` | Open-ended investigation notes with findings and recommendations |
| `architecture/` | Subsystem-level shape, invariants, key files |
| `plans/` | Per-feature implementation plans written before coding |
| `_templates/` | Templates for each note type |
| `_inbox/` | New notes land here before being filed |
| `_attachments/` | Images / binaries pasted into notes |

## Frontmatter contract

Every note has YAML frontmatter. This is what lets an agent filter the vault without reading files.

```yaml
---
type: decision | incident | gotcha | runbook | reference | research | architecture | plan | moc
status: active | resolved | superseded | proposed
date: 2026-04-22
tags: [voice, brain, music]
supersedes: [[old-note]]        # optional
superseded_by: [[new-note]]     # optional
related: [[note-a]] [[note-b]]  # optional
---
```

`type` is required. `status` defaults to `active`. `date` is the creation date; don't change it when editing.

## Writing rules

- **One concept per note.** If a note covers two things, split it. Small atomic notes are the point.
- **Use `[[wikilinks]]`**, not file paths. Obsidian's backlinks panel surfaces the reverse direction automatically.
- **Don't edit history in place.** When a decision is reversed, mark the old one `status: superseded` and write a new one. Link both directions.
- **No dates or incident narratives in code comments** — those belong here. Inline comments describe what the code does, not the story of how it got there.

## Entry points

- [[INDEX]] — top-level map of content
- Open any folder's `README.md` for its MOC
- Search by tag: click a tag in a frontmatter block
- Graph view: `Ctrl+G`

## Writing a new note

1. Pick a type; open `_templates/<type>.md`.
2. Fill in the frontmatter and body.
3. Save into the matching folder.
4. If it has a connection to an existing note, add a `[[wikilink]]` — backlinks handle the reverse.

The `docs-organizer` skill (see `.claude/skills/`) can also categorize an inbox note and move it into the right folder automatically.
