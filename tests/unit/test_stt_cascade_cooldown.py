"""STT cascade must actually skip a rate-limited provider (and come back).

PR #13 ("fix(stt): cooldown quota-exhausted STT providers to avoid 429
storms") added `STTRateLimitError` and an empty `_stt_provider_cooldown`
dict but never wired a check/set anywhere — `_transcribe`/`_transcribe_16k`
already caught every exception generically and moved to the next provider,
so raising STTRateLimitError instead of returning "" produced BYTE-IDENTICAL
behavior to before the PR. Zero tests shipped with it, which is exactly why.

Confirmed against production logs (2026-09-07): Gemini STT 429
'RESOURCE_EXHAUSTED' fired every 30-40s for hours — a hard quota
exhaustion where every retry in that window is guaranteed to fail
identically, exactly the case a cooldown should short-circuit.

Fix: VoiceSession._transcribe/_transcribe_16k now share PoobBrain's
ProviderCooldownRegistry (docs/decisions/provider-cooldown-registry.md) —
the same server-advised-Retry-After mechanism already proven for the LLM
routing cascade, not a bespoke re-implementation.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from poob.brain.poob import PoobBrain
from poob.voice.session import VoiceSession
from poob.voice.stt import STTRateLimitError


class _FakeResp:
    def __init__(self, headers: dict) -> None:
        self.headers = headers


def _rate_limit_exc(retry_after: str = "5") -> STTRateLimitError:
    exc = STTRateLimitError("429 rate limit exceeded")
    exc.response = _FakeResp({"retry-after": retry_after})
    return exc


class _ScriptedProvider:
    """An STT provider whose transcribe() replays a scripted sequence of
    results (an exception instance is raised, anything else is returned)."""

    def __init__(self, name: str, script: list) -> None:
        self._name = name
        self._script = list(script)
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    async def transcribe(self, pcm_audio: bytes, sample_rate: int = 48000) -> str:
        self.calls += 1
        # Once the script is exhausted, keep replaying the last entry
        # (a "healthy" provider stays healthy across repeated calls).
        result = (
            self._script.pop(0)
            if len(self._script) > 1
            else (self._script[0] if self._script else "")
        )
        if isinstance(result, Exception):
            raise result
        return result

    def is_available(self) -> bool:
        return True


def _session(providers: list) -> VoiceSession:
    brain = PoobBrain(groq_api_key="", deal_agent=None)
    vc = MagicMock()
    vc.guild = None
    return VoiceSession(voice_client=vc, stt_providers=providers, tts_providers=[], brain=brain)


@pytest.mark.asyncio
async def test_rate_limited_provider_is_skipped_on_the_next_call() -> None:
    """The actual gap PR #13 left: after a 429, the SAME provider must be
    skipped on the IMMEDIATE next transcribe() call, not re-probed."""
    flaky = _ScriptedProvider("gemini_stt:gemini-2.5-flash-lite", [_rate_limit_exc("100")])
    healthy = _ScriptedProvider("groq_whisper:whisper-large-v3-turbo", ["hello there"])
    session = _session([flaky, healthy])

    # First call: flaky 429s, cascade falls through to healthy.
    text1 = await session._transcribe(b"\x00" * 4000)
    assert text1 == "hello there"
    assert flaky.calls == 1
    assert healthy.calls == 1

    # Second call, immediately after: flaky is in cooldown (100s advised) —
    # it must NOT be probed again this call.
    text2 = await session._transcribe(b"\x00" * 4000)
    assert text2 == "hello there"
    assert flaky.calls == 1, "rate-limited provider was re-probed instead of skipped"
    assert healthy.calls == 2


@pytest.mark.asyncio
async def test_provider_rejoins_cascade_after_cooldown_expires() -> None:
    """'When the models start relieving their rate limits, we go RIGHT BACK
    to the previous one' — once the advised window has passed, the
    previously-capped provider must be tried again, first."""
    flaky = _ScriptedProvider("gemini_stt:gemini-2.5-flash-lite", [_rate_limit_exc("100"), "back!"])
    healthy = _ScriptedProvider("groq_whisper:whisper-large-v3-turbo", ["fallback"])
    session = _session([flaky, healthy])

    text1 = await session._transcribe(b"\x00" * 4000)
    assert text1 == "fallback"

    # Simulate the advised window having passed, rather than sleeping a
    # real 100s in a unit test (the registry's own tests use the same
    # pattern: set the deadline into the past directly).
    import time as _time

    session.brain._provider_breaker._cooldown[flaky.name] = _time.monotonic() - 1

    text2 = await session._transcribe(b"\x00" * 4000)
    assert text2 == "back!", "provider did not rejoin the cascade once its cooldown expired"
    assert flaky.calls == 2
    assert healthy.calls == 1, "healthy provider was called even though the primary recovered"


@pytest.mark.asyncio
async def test_transcribe_16k_shares_the_same_cooldown_state_as_transcribe() -> None:
    """The salvage cascade (_transcribe_16k) and the primary cascade
    (_transcribe) must share cooldown state — a provider that 429'd on one
    path should be skipped on the other too, since it's the same account
    hitting the same quota regardless of which code path called it."""
    flaky = _ScriptedProvider(
        "gemini_stt:gemini-2.5-flash-lite", [_rate_limit_exc("100"), "should not be reached"]
    )
    healthy = _ScriptedProvider("groq_whisper:whisper-large-v3-turbo", ["a", "b"])
    session = _session([flaky, healthy])

    await session._transcribe(b"\x00" * 4000)  # trips the cooldown via the primary path
    assert flaky.calls == 1

    text = await session._transcribe_16k(b"\x00" * 4000)  # different method, same registry
    assert text == "b"
    assert flaky.calls == 1, (
        "salvage cascade re-probed a provider cooled down by the primary cascade"
    )


@pytest.mark.asyncio
async def test_non_rate_limit_failure_does_not_trigger_cooldown() -> None:
    """An ordinary failure (bad audio, transient error) must not cool the
    provider down — only 429/quota errors should."""
    flaky = _ScriptedProvider(
        "groq_whisper:whisper-large-v3-turbo", [ValueError("bad audio"), "recovered"]
    )
    session = _session([flaky])

    text1 = await session._transcribe(b"\x00" * 4000)
    assert text1 == ""  # only provider failed non-rate-limit; cascade returns empty

    text2 = await session._transcribe(b"\x00" * 4000)
    assert text2 == "recovered"
    assert flaky.calls == 2, "provider was skipped after a non-rate-limit failure"


@pytest.mark.asyncio
async def test_never_strands_when_every_provider_is_cooling() -> None:
    """If every STT provider is in cooldown, the cascade must still try the
    full list (least-bad) rather than transcribing nothing at all."""
    a = _ScriptedProvider("a", [_rate_limit_exc("100"), "a-result"])
    b = _ScriptedProvider("b", [_rate_limit_exc("100")])
    session = _session([a, b])

    await session._transcribe(b"\x00" * 4000)  # cools both a and b
    assert a.calls == 1 and b.calls == 1

    text = await session._transcribe(b"\x00" * 4000)
    assert text == "a-result", "cascade stranded instead of retrying the full (cooling) list"
