---
name: shell-ops
description: Executes git and shell operations reliably on poob's Windows dev environment. The user runs Windows PowerShell 5.1 (`PS C:\Users\19203\Downloads\AgenticWebScraper>` prompt). Bash line-continuation syntax (`\` at end of line) FAILS in PowerShell — splits the command, leaves files unstaged, produces "fatal: is outside repository" errors. This skill documents cross-shell safe patterns for git add/commit/push, multi-line commands, commit messages with special chars, CRLF warnings, and the mandatory verification step (`git status` after every commit). Use whenever running git, staging multiple files, authoring commit messages, or any shell command that might span multiple lines. Also covers Claude Code's Bash sandbox quirks (SSH config not loaded, HOME differs from Windows %USERPROFILE%).
---

# Shell Operations — Windows PowerShell Reality Check

This skill exists because a prior agent wrote bash-style line-continued `git add` commands that PowerShell interpreted as separate commands — half the files weren't staged, commits went out with wrong contents, and the user had to manually repair. Don't do that.

## The user's shell environment (April 2026)

- **Primary shell:** Windows PowerShell 5.1 — prompt looks like `(venv) PS C:\Users\19203\Downloads\AgenticWebScraper>`
- **OS:** Windows 10 Home 10.0.19045
- **Virtualenv active:** `venv/` under the repo root (`pip install -e ".[dev]"`)
- **Available to agents:** `Bash` tool (Git Bash / WSL-style bash sandbox) + `PowerShell` tool (native PS 5.1)
- **Local git:** Windows-native git with `core.autocrlf=true` (the `LF will be replaced by CRLF` warnings are EXPECTED, not errors)

When writing commands for the user to paste into their terminal, assume PowerShell. When using the `Bash` tool yourself, remember it's a sandbox with a different HOME and may not have the user's SSH config.

## The rule that matters most: no `\` line continuation in PowerShell

**WRONG** (what the prior agent did):
```bash
git add src/poob/discord_bot/cogs/voice_cog.py \
        src/poob/discord_bot/cogs/music_cog.py \
        tests/unit/test_music_ui.py
```

PowerShell parses the first line as a complete command (treating `\` as a literal arg), then runs line 2 and 3 as separate invalid commands. Result: only the first file staged, `fatal: \: '\' is outside repository at 'C:/Users/19203/...'`.

**RIGHT — three options, pick one:**

### Option A: one-liner (simplest)

```powershell
git add src/poob/discord_bot/cogs/voice_cog.py src/poob/discord_bot/cogs/music_cog.py tests/unit/test_music_ui.py
```

### Option B: backtick continuation (PowerShell-native)

```powershell
git add `
    src/poob/discord_bot/cogs/voice_cog.py `
    src/poob/discord_bot/cogs/music_cog.py `
    tests/unit/test_music_ui.py
```

Backtick (`` ` ``) is PowerShell's line continuation. Trailing space before the backtick is tolerated but not required.

### Option C: separate calls (most explicit)

```powershell
git add src/poob/discord_bot/cogs/voice_cog.py
git add src/poob/discord_bot/cogs/music_cog.py
git add tests/unit/test_music_ui.py
```

Each `git add` is a full command. `git add` is idempotent — staging an already-staged file is a no-op.

**When in doubt: use Option A.** One-liners are the most portable across PowerShell, bash, and Git Bash.

## Commit message escaping

PowerShell has its own escape rules. Observed pitfalls:

| Character | In PowerShell | Safe pattern |
|---|---|---|
| `"` inside double-quoted message | breaks quoting | use single quotes: `-m 'feat: add "thing"'` |
| `'` inside single-quoted message | breaks quoting | use double quotes: `-m "fix: don't crash"` |
| `` ` `` backtick | parsed as continuation | single-quote the message |
| `$` | PowerShell var expansion | single-quote: `-m 'fix: $foo escaped'` |
| `—` em-dash | works fine (unicode pass-through) | safe |
| newlines in message | complicated | use here-string (see below) |

**Multi-line commit message — use here-string:**

```powershell
git commit -m @'
feat: summary line

Body paragraph with full sentence.

- bullet one
- bullet two
'@
```

Single-quoted here-string (`@'...'@`) preserves literal text. Double-quoted (`@"..."@`) interpolates variables. Use single-quoted unless you need expansion. Closing `'@` MUST be at column 0 (no leading whitespace).

## Verification is mandatory

**After every git operation, run `git status` and read the output.** Not optional. The prior agent's workflow failed because it assumed `git add ... \` succeeded and moved on to commit — half the files were untracked, half were staged, the commit went out wrong.

```powershell
git add <files>
git status                          # VERIFY what's staged matches what you intended
git commit -m "..."
git status                          # VERIFY clean or expected remaining state
git log -1 --stat                   # VERIFY the commit contains the right files
git push origin main
```

For push confirmation beyond the `To https://...` line, agents should invoke the **poob-logs skill** (`scripts/logs.sh poob --since 5m` after 5 min) to confirm the new image landed and the container restarted cleanly. Or wait for the startup DM from Poob to Ben (automated — runs `_notify_owner_alive` in bot.py on_ready).

## Staging patterns for different situations

### All modified tracked files + specific new files

```powershell
git add -u                                      # stages all modifications to tracked files (NOT new files)
git add path/to/new/file.py                     # stage new files individually
git status                                      # verify
```

### Everything including new files (careful — catches ignored files if your ignore is wrong)

```powershell
git add -A
git status                                      # MANDATORY — verify no unwanted files
```

If `git add -A` sneaks in a file it shouldn't (like `data/scraper.db`), unstage with `git restore --staged <file>`.

### Interactive staging (when unsure what's in a diff)

```powershell
git diff <file>                                 # see unstaged changes to one file
git diff --cached <file>                        # see staged changes
git add -p <file>                               # stage hunks selectively (interactive, works in PS)
```

## Logical commit splitting

When a task touched many files across different concerns, split into logical commits. Pattern:

```powershell
# Commit 1: core feature
git add src/poob/feature_a.py tests/unit/test_feature_a.py
git commit -m "feat: add feature A"

# Commit 2: related refactor
git add src/poob/related.py
git commit -m "refactor: update related module to use feature A"

# Commit 3: docs
git add docs/technical_notes.md
git commit -m "docs: document feature A decision rationale"

# Push all at once
git push origin main
```

Run `git status` between each commit. Logical commits make git log readable and make bisect-based debugging possible.

## Common error recovery

| Error | What it means | Fix |
|---|---|---|
| `fatal: \: '\' is outside repository` | You used bash `\` continuation in PowerShell | Retry as one-liner; run `git status` to see what actually got staged |
| `warning: LF will be replaced by CRLF` | Normal on Windows with `core.autocrlf=true` | Ignore — not an error, just informational |
| `pathspec '<file>' did not match any files` | Typo in path, or file isn't tracked and not created | Check with `git status`, `ls <file>` |
| `nothing to commit, working tree clean` | You already committed, or git add was a no-op | Run `git log -1` to confirm last commit |
| `Your branch is ahead of 'origin/main' by N commits` | Local has commits that aren't pushed | `git push origin main` |
| `! [rejected] main -> main (fetch first)` | Remote has commits you don't | `git pull --rebase origin main`, resolve, push |

## Claude Bash sandbox limitations

When using the `Bash` tool (not `PowerShell` tool), be aware:

- The sandbox has a different `$HOME` than the user's Windows `%USERPROFILE%`. The user's `~/.ssh/config` (`Host homelab`) may NOT be visible inside the Bash sandbox. This means `ssh homelab "..."` from the Bash tool may fail with `Permission denied (publickey)` even when it works fine from the user's PS terminal.
- For production log queries, **instruct the user to run `scripts/logs.sh`** in their own PS terminal rather than trying to run it from the Bash sandbox — unless SSH works in the sandbox (test with `ssh homelab "echo ok"`).
- File paths: Bash sandbox can read/write `c:/Users/.../` but case sensitivity and path separators differ. Use forward slashes universally; both shells accept them for file ops.

## Never do these

- Never chain with `&&` or `||` when using the PowerShell tool — PS 5.1 doesn't support pipeline chain operators. Use `; if ($?) { next }` or separate calls.
- Never assume a command succeeded because it didn't print an error — some commands are silent on success AND silent on partial failure.
- Never use `git push --force` on `main` unless explicitly asked (overwrites shared history).
- Never write commits that bundle unrelated changes — the "logical commit splitting" pattern exists for a reason.
- Never skip `git status` after staging operations. That's the single highest-value 1-second habit.

## Cross-references

- [`poob-logs`](../poob-logs/SKILL.md) — for reading production after a push
- [`update-docs`](../update-docs/SKILL.md) — document the operational discoveries this skill captures
- [`run-tests`](../run-tests/SKILL.md) — run BEFORE pushing; failed pushes that auto-deploy cost an Actions build cycle
