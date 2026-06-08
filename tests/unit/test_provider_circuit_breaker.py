"""Provider circuit breaker for the tool-routing cascade.

The 2026-06-08 429 storm (Groq's 200k-TPD cap exhausted mid voice-session)
made the cascade re-probe a known-capped Groq on every turn — 130 fall-throughs,
23s latency spikes. The breaker reads the 429's own "try again in <N>" and
skips that model for exactly that window, so Gemini becomes rung 1 until Groq
recovers. Driven by the server's signal, never a guessed TTL. See
docs/decisions/provider-circuit-breaker.md.
"""

from __future__ import annotations

import time

from poob.brain.poob import PoobBrain


def _brain() -> PoobBrain:
    return PoobBrain(groq_api_key="", deal_agent=None)


class _FakeResp:
    def __init__(self, headers: dict) -> None:
        self.headers = headers


class _FakeExc(Exception):
    def __init__(self, msg: str, status_code=None, retry_after=None) -> None:
        super().__init__(msg)
        self.status_code = status_code
        if retry_after is not None:
            self.response = _FakeResp({"retry-after": retry_after})


_GROQ_429 = (
    "Error code: 429 - {'error': {'message': 'Rate limit reached for model "
    "`openai/gpt-oss-20b` ... on tokens per day (TPD): Limit 200000, Used "
    "199956, Requested 3534. Please try again in 25m7.68s.', 'code': "
    "'rate_limit_exceeded'}}"
)


# --- retry-after parsing (the heart of "driven by the server, not a guess") ---

def test_parses_minutes_and_seconds_from_message() -> None:
    assert abs(PoobBrain._retry_after_seconds(_FakeExc(_GROQ_429)) - (25 * 60 + 7.68)) < 0.01


def test_parses_seconds_only_message() -> None:
    assert abs(PoobBrain._retry_after_seconds(_FakeExc("Please try again in 9.552s")) - 9.552) < 0.01


def test_parses_minutes_seconds_fractional() -> None:
    got = PoobBrain._retry_after_seconds(_FakeExc("try again in 2m5.279999999s"))
    assert abs(got - 125.28) < 0.01


def test_prefers_retry_after_header() -> None:
    assert PoobBrain._retry_after_seconds(_FakeExc("rate limited", retry_after="42")) == 42.0


def test_no_signal_returns_none() -> None:
    assert PoobBrain._retry_after_seconds(_FakeExc("some other error")) is None


# --- rate-limit classification (timeouts must NOT cool down) ---

def test_429_is_rate_limit() -> None:
    assert PoobBrain._is_rate_limit_error(_FakeExc(_GROQ_429)) is True
    assert PoobBrain._is_rate_limit_error(_FakeExc("RESOURCE_EXHAUSTED")) is True


def test_timeout_is_not_rate_limit() -> None:
    assert PoobBrain._is_rate_limit_error(TimeoutError("timed out")) is False
    assert PoobBrain._is_rate_limit_error(_FakeExc("connection reset")) is False


# --- cooldown set / expiry / clamp ---

def test_note_rate_limit_sets_cooldown_from_message() -> None:
    b = _brain()
    b._note_model_rate_limited("openai/gpt-oss-20b", _FakeExc(_GROQ_429))
    assert b._model_in_cooldown("openai/gpt-oss-20b") is True


def test_timeout_does_not_set_cooldown() -> None:
    b = _brain()
    b._note_model_rate_limited("openai/gpt-oss-20b", TimeoutError("timed out"))
    assert "openai/gpt-oss-20b" not in b._provider_cooldown


def test_cooldown_clamped_to_max() -> None:
    b = _brain()
    b._note_model_rate_limited("m", _FakeExc("429 rate_limit_exceeded. try again in 9999.0s"))
    assert 1795 <= (b._provider_cooldown["m"] - time.monotonic()) <= 1801  # 30-min cap


def test_cooldown_clamped_to_min() -> None:
    b = _brain()
    b._note_model_rate_limited("m", _FakeExc("429 rate limit. try again in 1.0s"))
    assert 4 <= (b._provider_cooldown["m"] - time.monotonic()) <= 6  # 5s floor


def test_expired_cooldown_is_not_active() -> None:
    b = _brain()
    b._provider_cooldown["m"] = time.monotonic() - 1
    assert b._model_in_cooldown("m") is False


# --- cascade filtering ---

_PROVIDERS = [
    ("groq", "openai/gpt-oss-20b"),
    ("gemini", "gemini-2.5-flash-lite"),
    ("nvidia", "qwen/qwen3-next-80b-a3b-instruct"),
    ("groq", "meta-llama/llama-4-scout-17b-16e-instruct"),
]


def test_active_providers_skips_cooled_model_only() -> None:
    b = _brain()
    b._provider_cooldown["openai/gpt-oss-20b"] = time.monotonic() + 100
    active = b._active_providers(_PROVIDERS)
    assert ("groq", "openai/gpt-oss-20b") not in active           # capped model skipped
    assert ("gemini", "gemini-2.5-flash-lite") in active          # → Gemini is now rung 1
    assert ("groq", "meta-llama/llama-4-scout-17b-16e-instruct") in active  # separate TPD budget — survives


def test_active_providers_never_strands_when_all_cooling() -> None:
    b = _brain()
    now = time.monotonic()
    for _p, m in _PROVIDERS:
        b._provider_cooldown[m] = now + 100
    assert b._active_providers(_PROVIDERS) == _PROVIDERS  # all cooling → least-bad full list


def test_active_providers_unchanged_when_none_cooling() -> None:
    assert _brain()._active_providers(_PROVIDERS) == _PROVIDERS
