"""Regression guard: voice_llm_model's token budgets must stay above the
measured full-starvation point for a REASONING model on a REAL question.

Prod, 2026-09-09: Ben asked Poob a real question in VC twice ("Hey Poob,
which attacker should I start with?") and got "got nothing to say right
now" both times. Root cause, reproduced live and deterministically (8/8):
qwen3.8-27b (voice_llm_model since PR #12) spent its ENTIRE token budget
on hidden chain-of-thought reasoning and had nothing left for the visible
answer -- a real question costs 200-300 reasoning tokens; casual banter
costs far less, which is why the PR #12 eval (roast-style prompts only)
never caught this.

Measured at the exact configured production values:
    max_tokens=200  -> 200/200 reasoning tokens, 0 content (8/8 reproduced)
    max_tokens=300  -> 300/300 reasoning tokens, 0 content
    max_tokens=500  ->  ~182-225 reasoning, some content -- but stochastic,
                        not a safe margin
    max_tokens=1000 -> reliable content with room to spare

Fix: max_tokens_voice / voice_llm_max_tokens raised 110/200 -> 1000, and
every toob_max_tokens ceiling (the personality-wrap sites, same reasoning
model, same starvation risk) raised 100 -> 400. This is a ceiling, not a
target -- the persona prompt's own "default tight, breathe when it earns
it" instruction is what keeps normal replies short.

See docs/incidents/voice-reasoning-model-token-starvation-on-real-questions.md.
"""

from __future__ import annotations

from poob.brain.poob import PoobBrain
from poob.config import AppConfig

# The exact ceiling a real question reproduced 0/8 visible output at,
# measured live against the current voice_llm_model. Any configured budget
# at or below this is provably unsafe, not just "lower than we'd like".
_MEASURED_FULL_STARVATION_TOKENS = 300


def test_poobbrain_max_tokens_voice_above_measured_starvation_point() -> None:
    brain = PoobBrain(groq_api_key="", deal_agent=None)
    assert brain.max_tokens_voice > _MEASURED_FULL_STARVATION_TOKENS, (
        f"max_tokens_voice={brain.max_tokens_voice} is at or below the "
        "measured full-starvation point for a real question on the "
        "current voice_llm_model -- see docs/incidents/"
        "voice-reasoning-model-token-starvation-on-real-questions.md"
    )


def test_appconfig_voice_llm_max_tokens_above_measured_starvation_point() -> None:
    cfg = AppConfig(_env_file=None, discord_bot_token="test-token", discord_deals_channel_id=123)
    assert cfg.voice_llm_max_tokens > _MEASURED_FULL_STARVATION_TOKENS, (
        f"voice_llm_max_tokens={cfg.voice_llm_max_tokens} is at or below "
        "the measured full-starvation point -- see docs/incidents/"
        "voice-reasoning-model-token-starvation-on-real-questions.md"
    )


def test_config_and_brain_defaults_stay_consistent() -> None:
    """main.py wires config.voice_llm_max_tokens into PoobBrain's
    max_tokens_voice -- the two bare defaults drifting apart (as they did
    before this incident: 110 vs 200) means whichever number a test reads
    directly off PoobBrain doesn't reflect what actually runs in
    production. Not required to be equal, but a future silent divergence
    of "one got fixed, the other didn't" should fail loudly."""
    brain = PoobBrain(groq_api_key="", deal_agent=None)
    cfg = AppConfig(_env_file=None, discord_bot_token="test-token", discord_deals_channel_id=123)
    assert brain.max_tokens_voice == cfg.voice_llm_max_tokens, (
        "PoobBrain's bare default and AppConfig's default have drifted apart "
        f"({brain.max_tokens_voice} vs {cfg.voice_llm_max_tokens}) -- main.py "
        "wires config's value into the brain in production, so a fix to only "
        "one of these two defaults silently doesn't ship"
    )
