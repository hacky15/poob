"""Provider-agnostic rate-limit cooldown registry.

Extracted from PoobBrain's tool-routing circuit breaker
(docs/decisions/provider-circuit-breaker.md) so any cascade — LLM routing,
STT, TTS, VLM — can skip a rate-limited model for its server-advised window
and automatically resume once that window passes, without re-implementing
the retry-after parsing / clamping / never-strand logic per subsystem.

These tests mirror test_provider_circuit_breaker.py's cases against the
class directly (rather than through PoobBrain), since the brain now
delegates to this registry instead of owning the logic.
"""

from __future__ import annotations

import time

import httpx

from poob.resilience.provider_cooldown import ProviderCooldownRegistry


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


def _reg() -> ProviderCooldownRegistry:
    return ProviderCooldownRegistry()


# --- retry-after parsing ---


def test_parses_minutes_and_seconds_from_message() -> None:
    assert (
        abs(ProviderCooldownRegistry.retry_after_seconds(_FakeExc(_GROQ_429)) - (25 * 60 + 7.68))
        < 0.01
    )


def test_parses_seconds_only_message() -> None:
    got = ProviderCooldownRegistry.retry_after_seconds(_FakeExc("Please try again in 9.552s"))
    assert abs(got - 9.552) < 0.01


def test_prefers_retry_after_header() -> None:
    assert (
        ProviderCooldownRegistry.retry_after_seconds(_FakeExc("rate limited", retry_after="42"))
        == 42.0
    )


def test_no_signal_returns_none() -> None:
    assert ProviderCooldownRegistry.retry_after_seconds(_FakeExc("some other error")) is None


def test_gemini_retry_in_message_format_parses() -> None:
    got = ProviderCooldownRegistry.retry_after_seconds(
        _FakeExc("RESOURCE_EXHAUSTED ... Please retry in 46.438775173s.")
    )
    assert got is not None and abs(got - 46.438775173) < 0.01


def test_retry_after_read_from_response_body() -> None:
    exc = _FakeExc("Client error '429 Too Many Requests' for url 'https://x'")
    exc.response = _FakeResp({})
    exc.response.text = (
        '{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", '
        '"message": "Quota exceeded ... Please retry in 46.438775173s."}}'
    )
    got = ProviderCooldownRegistry.retry_after_seconds(exc)
    assert got is not None and abs(got - 46.438775173) < 0.01


# --- classification ---


def test_429_is_rate_limit() -> None:
    assert ProviderCooldownRegistry.is_rate_limit_error(_FakeExc(_GROQ_429)) is True
    assert ProviderCooldownRegistry.is_rate_limit_error(_FakeExc("RESOURCE_EXHAUSTED")) is True


def test_timeout_is_not_rate_limit() -> None:
    assert ProviderCooldownRegistry.is_rate_limit_error(TimeoutError("timed out")) is False
    assert ProviderCooldownRegistry.is_rate_limit_error(_FakeExc("connection reset")) is False


def test_timeout_classification() -> None:
    assert ProviderCooldownRegistry.is_timeout_error(httpx.ReadTimeout("x")) is True
    assert ProviderCooldownRegistry.is_timeout_error(httpx.ConnectTimeout("x")) is True
    assert ProviderCooldownRegistry.is_timeout_error(TimeoutError("timed out")) is True
    assert ProviderCooldownRegistry.is_timeout_error(_FakeExc(_GROQ_429)) is False
    assert ProviderCooldownRegistry.is_timeout_error(ValueError("boom")) is False


# --- cooldown set / expiry / clamp ---


def test_note_rate_limited_sets_cooldown_from_message() -> None:
    r = _reg()
    r.note_rate_limited("openai/gpt-oss-20b", _FakeExc(_GROQ_429))
    assert r.in_cooldown("openai/gpt-oss-20b") is True


def test_timeout_does_not_set_cooldown_via_note_rate_limited() -> None:
    r = _reg()
    r.note_rate_limited("m", TimeoutError("timed out"))
    assert r.in_cooldown("m") is False


def test_cooldown_clamped_to_max() -> None:
    r = _reg()
    r.note_rate_limited("m", _FakeExc("429 rate_limit_exceeded. try again in 9999.0s"))
    left = r._cooldown["m"] - time.monotonic()
    assert 1795 <= left <= 1801  # 30-min cap


def test_cooldown_clamped_to_min() -> None:
    r = _reg()
    r.note_rate_limited("m", _FakeExc("429 rate limit. try again in 1.0s"))
    left = r._cooldown["m"] - time.monotonic()
    assert 4 <= left <= 6  # 5s floor


def test_expired_cooldown_is_not_active() -> None:
    r = _reg()
    r._cooldown["m"] = time.monotonic() - 1
    assert r.in_cooldown("m") is False


# --- timeout arming ---


def test_consecutive_timeouts_arm_cooldown() -> None:
    r = _reg()
    r.note_timeout("m", httpx.ReadTimeout("t1"))
    assert not r.in_cooldown("m")  # one timeout = transient
    r.note_timeout("m", httpx.ReadTimeout("t2"))
    assert r.in_cooldown("m")  # two in a row = hung
    left = r._cooldown["m"] - time.monotonic()
    assert 115 <= left <= 121


def test_non_timeout_error_resets_timeout_streak() -> None:
    r = _reg()
    r.note_timeout("m", httpx.ReadTimeout("t1"))
    r.note_timeout("m", _FakeExc(_GROQ_429))  # responded -> not hung
    r.note_timeout("m", httpx.ReadTimeout("t2"))
    assert not r.in_cooldown("m")  # streak restarted at 1


def test_note_success_clears_both_cooldown_and_timeout_streak() -> None:
    r = _reg()
    r.note_rate_limited("m", _FakeExc(_GROQ_429))
    r.note_timeout("m", httpx.ReadTimeout("t1"))
    r.note_success("m")
    assert not r.in_cooldown("m")
    # streak restarted from zero, so a single subsequent timeout is transient
    r.note_timeout("m", httpx.ReadTimeout("t2"))
    assert not r.in_cooldown("m")


# --- permanent-failure classification (404/410/403/402, not 429) ---


def test_404_410_403_402_are_permanent_failures() -> None:
    for status in (402, 403, 404, 410):
        assert (
            ProviderCooldownRegistry.is_permanent_failure_error(_FakeExc("x", status_code=status))
            is True
        )


def test_429_is_not_a_permanent_failure() -> None:
    assert ProviderCooldownRegistry.is_permanent_failure_error(_FakeExc(_GROQ_429)) is False


def test_timeout_is_not_a_permanent_failure() -> None:
    assert ProviderCooldownRegistry.is_permanent_failure_error(httpx.ReadTimeout("x")) is False


def test_gone_message_text_detected_without_status_code() -> None:
    exc = _FakeExc(
        "Client error '410 Gone' for url 'https://x' The model has reached its end of life"
    )
    assert ProviderCooldownRegistry.is_permanent_failure_error(exc) is True


def test_note_permanent_failure_sets_long_cooldown() -> None:
    r = _reg()
    r.note_permanent_failure("m", _FakeExc("x", status_code=410))
    left = r._cooldown["m"] - time.monotonic()
    assert 1795 <= left <= 1801  # permanent_failure_cooldown_s default (30 min)


def test_note_permanent_failure_noop_for_rate_limit() -> None:
    r = _reg()
    r.note_permanent_failure("m", _FakeExc(_GROQ_429))
    assert not r.in_cooldown("m")


# --- generic cascade filtering (works over ANY item type via key_fn) ---

_PROVIDERS = [
    ("groq", "openai/gpt-oss-20b"),
    ("gemini", "gemini-2.5-flash-lite"),
    ("nvidia", "qwen/qwen3-next-80b-a3b-instruct"),
    ("groq", "meta-llama/llama-4-scout-17b-16e-instruct"),
]


def test_filter_active_skips_cooled_model_only() -> None:
    r = _reg()
    r._cooldown["openai/gpt-oss-20b"] = time.monotonic() + 100
    active = r.filter_active(_PROVIDERS, key=lambda pm: pm[1])
    assert ("groq", "openai/gpt-oss-20b") not in active
    assert ("gemini", "gemini-2.5-flash-lite") in active
    assert ("groq", "meta-llama/llama-4-scout-17b-16e-instruct") in active


def test_filter_active_never_strands_when_all_cooling() -> None:
    r = _reg()
    now = time.monotonic()
    for _p, m in _PROVIDERS:
        r._cooldown[m] = now + 100
    assert r.filter_active(_PROVIDERS, key=lambda pm: pm[1]) == _PROVIDERS


def test_filter_active_unchanged_when_none_cooling() -> None:
    assert _reg().filter_active(_PROVIDERS, key=lambda pm: pm[1]) == _PROVIDERS


def test_filter_active_default_key_works_on_plain_strings() -> None:
    """A cascade of bare strings (e.g. STT provider names) needs no key_fn."""
    r = _reg()
    names = ["groq_whisper:whisper-large-v3-turbo", "gemini_stt:gemini-2.5-flash-lite"]
    r._cooldown[names[1]] = time.monotonic() + 100
    active = r.filter_active(names)
    assert active == [names[0]]


# --- configurable thresholds (the "any provider, any config" requirement) ---


def test_thresholds_are_constructor_configurable() -> None:
    r = ProviderCooldownRegistry(
        cooldown_min_s=1.0,
        cooldown_max_s=10.0,
        cooldown_default_s=2.0,
        timeout_arm_count=1,
        timeout_cooldown_s=3.0,
    )
    r.note_rate_limited("m", _FakeExc("429 rate limit, no advised time"))
    left = r._cooldown["m"] - time.monotonic()
    assert 1.5 <= left <= 2.5  # uses the configured default, clamped to [1,10]

    r2 = ProviderCooldownRegistry(timeout_arm_count=1, timeout_cooldown_s=3.0)
    r2.note_timeout("x", httpx.ReadTimeout("t1"))
    assert r2.in_cooldown("x")  # arms on the FIRST timeout with arm_count=1
