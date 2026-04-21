---
name: run-tests
description: Runs pytest with proper marker/scope filters for poob's three-tier test layout (unit/integration/e2e). Knows pytest-asyncio auto mode, in-memory SQLite fixtures, and the default-fast convention (run cheapest scope first). Use when verifying a code change doesn't break the suite, reproducing a specific failing test the user named, scoping to a single subsystem, before declaring any non-trivial code change complete, or running coverage. Also documents related lint/typecheck commands (ruff, mypy) since those are part of "is this change ready".
---

# Running the Test Suite

Poob uses pytest with `pytest-asyncio` (auto mode) and three test tiers under `tests/`. The conventions here match `pyproject.toml` — don't fight them.

## Layout

```
tests/
  conftest.py               # shared fixtures (in-memory SQLite, mock providers)
  unit/                     # fast, isolated, mock at protocol boundary
  integration/              # multi-component, may hit real local services (Ollama, SearXNG)
  e2e/                      # full pipeline, slow, may make external network calls
```

Markers (declared in `pyproject.toml`):
- `slow` — opt-in for long-running tests
- `integration` — applied to `tests/integration/`
- `e2e` — applied to `tests/e2e/`

## When to invoke

- After ANY edit under `src/poob/` — run at minimum the unit suite before saying "done"
- When the user references a specific failing test by name — reproduce it first, then debug
- When refactoring across modules — full unit suite + relevant integration
- Before pushing a commit that changes core logic (LLM cascade, scanner, voice pipeline, deal radar)
- When investigating a pre-existing test failure the user wants triaged

## Default-fast invocations

Always start with the cheapest scope that covers the change. Climb the ladder only if needed.

```bash
# Tier 1 (default): unit suite only — sub-second, no network, no Ollama
pytest tests/unit/

# Tier 2: unit + integration — pulls in local Ollama/SearXNG paths
pytest tests/unit/ tests/integration/

# Tier 3: full suite including e2e — slow, may need Facebook creds, real LLM keys
pytest

# Targeted: single file
pytest tests/unit/test_listing_filter.py

# Targeted: single test function
pytest tests/unit/test_listing_filter.py::test_rejects_bulk_listings

# Targeted: by name pattern (matches function name substrings)
pytest -k "watchlist and not_negation"

# Skip slow tests (default convention for fast iteration)
pytest -m "not slow"

# Only run slow tests (when explicitly verifying perf-sensitive paths)
pytest -m slow
```

## Useful flags

```bash
pytest -x                  # stop on first failure (faster feedback while debugging)
pytest -v                  # verbose: show each test name + status
pytest -vv                 # very verbose: full assertion diffs
pytest --tb=short          # compact tracebacks (good for scanning many failures)
pytest --tb=long           # full tracebacks (default; good for one failure)
pytest -s                  # don't capture stdout — see print() / log output
pytest --pdb               # drop into debugger on first failure (interactive only)
pytest --lf                # rerun only last failed
pytest --ff                # run failed first, then rest
```

## Coverage

```bash
pytest --cov=poob tests/unit/                      # quick unit coverage
pytest --cov=poob --cov-report=term-missing        # show uncovered lines inline
pytest --cov=poob --cov-report=html                # HTML report at htmlcov/index.html
```

`pytest-cov` is in the `[dev]` extras (`pip install -e ".[dev]"`).

## Async tests

Auto-mode is on (`asyncio_mode = "auto"` in pyproject.toml), so you write:

```python
async def test_something():
    result = await some_async_call()
    assert result == expected
```

Don't decorate with `@pytest.mark.asyncio` — it's redundant and the convention here is bare `async def`.

## Mocking conventions

Per CLAUDE.md: **mock at protocol boundaries, not internal implementation.** That means:

- Mock `LLMProvider`, `SiteAdapter`, repository classes
- Don't mock private methods, don't patch deep internals
- Use `pytest-mock`'s `mocker` fixture for clean teardown

Database tests use in-memory SQLite via `tests/conftest.py` fixtures — never spin up the real `data/scraper.db`.

## Linting + type-checking (related, not pytest)

These are part of "is this change ready" but they aren't pytest. Run them too before saying done:

```bash
ruff check src/ tests/         # lint
ruff format src/ tests/        # auto-format (do this FIRST, then re-lint)
mypy src/                      # type check (strict mode per pyproject.toml)
```

If ruff/mypy fail on code you didn't touch, note it but don't fix it as part of an unrelated PR — open a follow-up.

## Interpreting failures

| Failure mode | Likely cause |
|---|---|
| `ModuleNotFoundError` on a poob submodule | Forgot `pip install -e .` after pulling, OR you broke an import path |
| `AttributeError: ... has no attribute` mid-test | Mocked at the wrong layer; check protocol boundary |
| `RuntimeError: This event loop is already running` | Mixing sync/async incorrectly; check for missing `await` |
| `aiosqlite.OperationalError: no such table` | conftest fixture didn't run; ensure test file pulls the right fixture |
| Real network call hangs | Test reached real LLM/Discord/Facebook — should be mocked or marked `slow`/`e2e` |

## Don't

- Don't run `pytest` (no scope) when iterating on a single fix — too slow
- Don't commit with failing tests (other than ones you've explicitly noted as pre-existing)
- Don't add `@pytest.mark.skip` to make a failure go away — fix it or document why it's skipped in the test docstring
- Don't write tests AFTER the fix unless TDD wasn't possible — CLAUDE.md says TDD; honor it where you can

## Running pytest from the Claude Bash sandbox vs user's PowerShell

`pytest` can be invoked from either. The Bash sandbox uses the repo's `venv/` Python (since we're already `cd`'d into the repo root). Long runs (full suite) may exceed the Bash tool's timeout — use `run_in_background: true` and poll, or run in the user's PS terminal instead.

**Known gotcha:** `tests/unit/test_voice.py` currently fails at collection time with `ModuleNotFoundError: No module named 'poob.voice.conversation'` — that module was removed but the test wasn't updated. Use `--ignore=tests/unit/test_voice.py` for clean unit runs until the test is repaired. Capture this in [update-docs](../update-docs/SKILL.md) if you fix it.

## Cross-references

- [`shell-ops`](../shell-ops/SKILL.md) — for running pytest across multiple arguments safely on PowerShell
- [`update-docs`](../update-docs/SKILL.md) — if tests surface a bug with a non-obvious root cause, document the fix
- [`poob-logs`](../poob-logs/SKILL.md) — when a test passes locally but production behaves differently, logs bridge the gap
