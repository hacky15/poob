"""Tests for the deal sub-agent system prompt formatting contract.

The deal sub-agent runs gpt-oss-120b with personality=False. Its output
is returned to the user RAW (no personality wrap) when long/multiline —
a documented intentional invariant (docs/architecture/poobbrain-architecture.md:
"data-heavy responses bypass personality wrap"). Because the wrap is
bypassed, the sub-agent prompt itself MUST forbid markdown/emoji/boilerplate
so the plain data it emits reads as Poob, not as a ChatGPT helpdesk form.
See docs/incidents/text-mode-rlhf-refusal-leak-2026-05-29.md (Fix 2).
"""

from __future__ import annotations

from poob.agent.prompts import build_system_prompt


def test_sub_agent_preamble_forbids_decorative_formatting() -> None:
    prompt = build_system_prompt(personality=False).lower()
    # Must explicitly forbid the artifacts seen in prod (markdown tables,
    # bold headers, emoji) and mandate plain text.
    assert "markdown" in prompt, "sub-agent prompt must forbid markdown"
    assert "emoji" in prompt, "sub-agent prompt must forbid emoji"
    assert "plain text" in prompt or "plain-text" in prompt, (
        "sub-agent prompt must mandate plain text"
    )


def test_sub_agent_preamble_bans_helpdesk_boilerplate() -> None:
    prompt = build_system_prompt(personality=False).lower()
    # The prod replies ended with "let me know if you'd like..." filler.
    assert "let me know" in prompt or "boilerplate" in prompt, (
        "sub-agent prompt must address the trailing-boilerplate habit"
    )


def test_sub_agent_preamble_still_requires_showing_data() -> None:
    # Forbidding formatting must NOT remove the 'always show the data'
    # rule — the data-bypass invariant depends on the sub-agent emitting
    # the actual items.
    prompt = build_system_prompt(personality=False).lower()
    assert "show the data" in prompt or "include the actual data" in prompt \
        or "include all relevant data" in prompt
