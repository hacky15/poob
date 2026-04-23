# Poob - Project Conventions

## CRITICAL: The docs/ Vault is the Knowledge Base

The `docs/` directory is an Obsidian vault and the single source of truth for how this system works. It is organized by **document type**, not topic. Before any non-trivial change, read the vault; after any non-trivial change, write to it.

### Vault layout

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
| `docs/_templates/` | Note templates — copy when writing a new one |
| `docs/_inbox/` | New notes land here before being categorized |

Entry points: [docs/INDEX.md](../docs/INDEX.md) and [docs/README.md](../docs/README.md). Every folder has its own `README.md` that describes what goes there and serves as that folder's map of content.

### Frontmatter contract

Every note has YAML frontmatter. This is what lets an agent filter the vault cheaply.

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

### Read-before / write-after workflow

Before any non-trivial work:

1. Start at [docs/INDEX.md](../docs/INDEX.md).
2. Check [docs/gotchas/](../docs/gotchas/) first — if someone already learned what you're about to try, don't re-learn it.
3. Read the relevant `architecture/` note for mental model, and any `decisions/` notes tagged with the subsystem you're touching.
4. If a past `incidents/` note is adjacent, read it — the symptom might match again.

After any non-trivial work:

1. Pick the right note type (decision, incident, gotcha, runbook, reference, research, architecture).
2. Copy the matching template from `docs/_templates/` and fill it in.
3. File into the right folder.
4. Link with `[[wikilinks]]` — backlinks surface the reverse direction automatically.
5. If the change supersedes an existing note, mark the old one `status: superseded` and link forward/back.

This is not optional. Undocumented changes rot, and the next agent (or future you) will re-introduce bugs that were already fixed.

### Note writing rules

- **One concept per note.** Split if it covers two things.
- **Use wikilinks**, not file paths, for vault-internal references.
- **Don't rewrite history in place.** When a decision is reversed, the old note becomes `superseded` and a new note is written. Both link to each other.
- **Keep code comments tight and professional.** Incident narratives, dates, user quotes, and multi-paragraph justifications do NOT belong in inline comments — that context goes in the vault. Comments describe what the code does, pointing out to `docs/` when there's more backstory.

## CRITICAL: No Bandaid Fixes

Every fix must be **robust, modular, and integrated**. Never stuff edge-case patches into code to "make it work for now." If a fix requires understanding why the root cause exists, do the research first.

- **Use proper DOM readiness detection**, not longer sleep timers
- **Use existing libraries and services** to solve problems instead of hand-rolling fragile solutions
- **Fix at the right architectural layer** — if data is missing, fix the extraction, don't work around it downstream
- **If you're adding `if x is None: default_value` more than once for the same field**, the extraction is broken — fix the extraction
- **If a value flows through 5 stages and gets lost**, trace the data flow end-to-end and fix where it drops — don't add fallbacks at every stage
- **Every fix must be documented** in the vault with root cause and rationale — as an `incidents/` note if it was a bug, plus a `gotchas/` note if the hazard will recur.

## CRITICAL: Shell Environment — Read Before Running ANY Command

The user runs **Windows PowerShell 5.1** (prompt: `(venv) PS C:\Users\19203\Downloads\AgenticWebScraper>`). Bash syntax does not translate cleanly. Common lethal mistakes:

- **`\` at end of line is NOT line continuation in PowerShell** — it's a literal character, splits your command, leaves files unstaged, produces `fatal: \: is outside repository`. Use one-liners, or backtick `` ` `` continuation, or separate commands.
- **`&&` and `||` pipeline chains don't work in PS 5.1** — use `;` or separate calls.
- **`warning: LF will be replaced by CRLF`** is normal on Windows, not an error. Ignore it.
- **After every `git add` and `git commit`, run `git status`** to verify what actually happened. Silent partial failure is the default on Windows/PS.

For the full cross-shell safety rules, invoke the **`shell-ops`** skill.

## CRITICAL: Project Skills — Use Them Without Asking

This repo ships project-scoped Claude skills under `.claude/skills/`. Each is a single SKILL.md with full instructions; descriptions below are pointers, not the spec.

| Skill | Trigger | Where |
|---|---|---|
| **`shell-ops`** | Running git, shell commands, multi-line operations, or staging multiple files. Knows PowerShell 5.1 quirks, safe commit patterns, verification discipline (`git status` after every stage/commit), commit-message escaping, Claude Bash sandbox limitations. | [.claude/skills/shell-ops/SKILL.md](skills/shell-ops/SKILL.md) |
| **`poob-logs`** | Reading live container logs from homelab production. Investigating runtime behavior, verifying a deploy landed, debugging a shipped exception, auditing what poob did at a specific moment. Read logs freely — don't ask permission. | [.claude/skills/poob-logs/SKILL.md](skills/poob-logs/SKILL.md) |
| **`update-docs`** | After every meaningful change — code edits, dependency updates, deploy config tweaks, env var additions, bug fixes, architectural decisions, infra changes. Documentation is part of the change, not cleanup. Trigger before declaring any task complete. | [.claude/skills/update-docs/SKILL.md](skills/update-docs/SKILL.md) |
| **`run-tests`** | Verifying a code change doesn't break the suite, reproducing a specific failing test, scoping to a single tier (unit/integration/e2e). Always run at minimum the unit suite before saying "done" on src/ edits. | [.claude/skills/run-tests/SKILL.md](skills/run-tests/SKILL.md) |
| **`docs-organizer`** | Filing a new or unsorted note into the vault: inspects frontmatter + content, picks the right folder (decisions/incidents/gotchas/etc.), wires up wikilinks. Also handles converting prose into the correct template shape. | [.claude/skills/docs-organizer/SKILL.md](skills/docs-organizer/SKILL.md) |

These skills are auto-discovered from `.claude/skills/`. Their descriptions load into every session's context — if your task matches one of them, invoke it. Don't reinvent log-querying, doc-writing, test-running, or shell-command flows ad-hoc.

## Overview
Autonomous deal-hunting bot that monitors Facebook Marketplace, evaluates listings through a multi-stage VLM pipeline, and delivers deal alerts via Discord. Uses Playwright for browser automation, a cascade of cloud and local LLMs for evaluation, and SQLite for storage.

## Architecture

See [docs/architecture/](../docs/architecture/) for subsystem-level notes. Load-bearing invariants:

- **src layout:** All source code under `src/poob/`
- **Protocol-based interfaces** (not ABC) for all extension points (SiteAdapter, LLMProvider)
- **Async-first:** Every I/O operation is async
- **Dependency injection:** Components receive dependencies via constructor
- **Plugin system:** Site adapters auto-discovered from `src/poob/sites/` subdirectories
- **LLM provider cascade:** Agent brain uses Groq → Cerebras → NVIDIA NIM → Gemini → Ollama (fastest-first)
- **VLM evaluation pipeline:** Text triage → visual enrichment → VLM deep evaluation (Gemini Flash → Groq Vision → Gemini Pro → OpenRouter → Ollama)
- **Dual interaction model:** Patrol engine runs autonomously; Discord bot (Pycord) provides conversational agent with tool-calling

## Code Style
- Python 3.11+ features (type unions with `|`, match statements where appropriate). Container runs 3.11 because openwakeword pulls tflite-runtime which has no 3.12 wheels yet; local dev on 3.12 still works since 3.11 is a minimum.
- `ruff` for linting and formatting (line-length 100)
- `mypy` strict mode
- Docstrings on all public classes and methods (Google style)
- No wildcard imports
- **Comments stay professional.** Describe what the code does; keep it to a few lines. Incident narratives, dates, specific user quotes, multi-paragraph "why" prose do NOT belong inline — they belong in a vault note, and the comment can point there.

## Testing
- Tests live in `tests/` with `unit/`, `integration/`, `e2e/` subdirectories
- **Write tests BEFORE implementation** (TDD)
- `pytest` + `pytest-asyncio` (auto mode)
- Mock at protocol boundaries, not internal implementation
- In-memory SQLite (`:memory:`) for all database tests
- Shared fixtures in `tests/conftest.py`

## Git Conventions
- Conventional commits: `feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `chore:`
- One logical change per commit
- Never commit `.env`, `browser_profiles/`, or `data/`

## Adding a New Site Adapter
1. Create `src/poob/sites/<site_name>/` directory
2. Implement `adapter.py` with a class satisfying `SiteAdapter` protocol
3. Implement `prompts.py` with LLM task prompts for navigation
4. Implement `parser.py` for listing extraction from agent output
5. Export `Adapter = YourAdapter` in `__init__.py`
6. The `SiteRegistry` will auto-discover it on startup
7. **Write a `docs/references/<site>.md` note** with any site-specific quirks, rate limits, or extraction gotchas. If any hazard is durable, also write a `docs/gotchas/` note.

## Configuration
- All config via environment variables (12-factor app)
- `pydantic-settings` loads from `.env` file
- No hardcoded values — everything in `AppConfig` class
- Secrets (tokens, passwords) only in `.env`, never in code

## Key Dependencies
- `playwright`: Browser automation (with `browser-use` for agent-mode navigation)
- `py-cord` (Pycord 2.7+): Discord bot with native voice recording
- `aiosqlite`: Async SQLite storage
- `pydantic-settings`: Typed configuration from environment
- `structlog`: Structured logging
- `httpx`: HTTP client (eBay/retail lookups, API calls)
- `langchain-ollama`: Local LLM via Ollama
- `google-genai`: Gemini Flash/Pro for VLM evaluation
- `groq`: Groq Cloud for fast agent responses and vision
- `cerebras-cloud-sdk`: Cerebras for text triage and fallback tool-calling
- `openai` (SDK): NVIDIA NIM and OpenRouter access via OpenAI-compatible API
- `openwakeword`: Local wake-word detection (`hey_poob.onnx`)
- `deepgram-sdk`: Streaming STT with keyterm boosting
- `ffmpeg`: Audio processing (speechnorm, Toob filter chain, mixing)

## Running
- `pip install -e ".[dev]"` to install in dev mode
- `playwright install` to install browser binaries
- Copy `.env.example` to `.env` and fill in values
- `python -m poob.main` to start
- `pytest` to run tests

## First-session checklist

New agent starting a task: do these before touching code.

1. Open [docs/INDEX.md](../docs/INDEX.md).
2. Read [docs/architecture/README.md](../docs/architecture/README.md) if the task touches src/.
3. Skim [docs/gotchas/README.md](../docs/gotchas/README.md) — it's short, and it's the "here be dragons" list.
4. Grep `docs/` for keywords related to the task. `grep -ri "<keyword>" docs/` is a good first move.

Skip this and you'll re-introduce something that was already fixed.
