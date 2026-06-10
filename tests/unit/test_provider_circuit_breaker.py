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


# ---------------------------------------------------------------------------
# 2026-06-09 cascade outage additions (see
# docs/incidents/cascade-outage-nvidia-hang-gemini-rpm.md):
# - Gemini puts "Please retry in 46.4s" in the 429 response BODY (str(exc)
#   doesn't contain it) and uses "retry in", not "try again in".
# - A provider that consistently TIMES OUT (NVIDIA NIM outage) is functionally
#   down: consecutive timeouts must arm a short cooldown, or every voice turn
#   pays the full REST timeout re-probing a hung rung.
# ---------------------------------------------------------------------------


def test_gemini_retry_in_message_format_parses() -> None:
    got = PoobBrain._retry_after_seconds(
        _FakeExc("RESOURCE_EXHAUSTED ... Please retry in 46.438775173s.")
    )
    assert got is not None and abs(got - 46.438775173) < 0.01


def test_retry_after_read_from_response_body() -> None:
    """httpx raise_for_status puts the quota text in resp.text, NOT str(exc)."""
    exc = _FakeExc("Client error '429 Too Many Requests' for url 'https://x'")
    exc.response = _FakeResp({})
    exc.response.text = (
        '{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", '
        '"message": "Quota exceeded ... Please retry in 46.438775173s."}}'
    )
    got = PoobBrain._retry_after_seconds(exc)
    assert got is not None and abs(got - 46.438775173) < 0.01


def test_timeout_classification() -> None:
    import httpx

    assert PoobBrain._is_timeout_error(httpx.ReadTimeout("x")) is True
    assert PoobBrain._is_timeout_error(httpx.ConnectTimeout("x")) is True
    assert PoobBrain._is_timeout_error(TimeoutError("timed out")) is True
    assert PoobBrain._is_timeout_error(_FakeExc(_GROQ_429)) is False
    assert PoobBrain._is_timeout_error(ValueError("boom")) is False


def test_consecutive_timeouts_arm_cooldown() -> None:
    import httpx

    b = _brain()
    b._note_model_timeout("m", httpx.ReadTimeout("t1"))
    assert not b._model_in_cooldown("m")          # one timeout = transient
    b._note_model_timeout("m", httpx.ReadTimeout("t2"))
    assert b._model_in_cooldown("m")              # two in a row = hung -> eject
    left = b._provider_cooldown["m"] - time.monotonic()
    assert 115 <= left <= 121                     # short fixed window (120s)


def test_success_clears_timeout_streak() -> None:
    import httpx

    b = _brain()
    b._note_model_timeout("m", httpx.ReadTimeout("t1"))
    # The cascade loop pops both maps on a successful call.
    b._provider_cooldown.pop("m", None)
    b._provider_timeouts.pop("m", None)
    b._note_model_timeout("m", httpx.ReadTimeout("t2"))
    assert not b._model_in_cooldown("m")          # streak restarted at 1


def test_non_timeout_error_resets_timeout_streak() -> None:
    import httpx

    b = _brain()
    b._note_model_timeout("m", httpx.ReadTimeout("t1"))
    b._note_model_timeout("m", _FakeExc(_GROQ_429))   # responded -> not hung
    b._note_model_timeout("m", httpx.ReadTimeout("t2"))
    assert not b._model_in_cooldown("m")          # 429 broke the streak


def test_second_gemini_rung_configured() -> None:
    """A second Gemini MODEL = a separate per-model free-tier RPM bucket;
    busy-VC bursts past one bucket while Groq's daily cap is spent."""
    import inspect

    import poob.brain.poob as brain_mod

    b = _brain()
    # Primary = 3.1-flash-lite (fastest free, ~587ms w/ thinking off); alt =
    # 2.5-flash-lite (separate per-model RPM bucket + GA fallback).
    assert b.gemini_router_model == "gemini-3.1-flash-lite-preview"
    assert b.gemini_router_model_alt == "gemini-2.5-flash-lite"
    assert b.gemini_router_model != b.gemini_router_model_alt
    src = inspect.getsource(brain_mod)
    assert 'providers.append(("gemini", self.gemini_router_model_alt))' in src
