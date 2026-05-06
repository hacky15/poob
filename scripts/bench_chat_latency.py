"""Benchmark casual-chat + tool-routing latency across Groq models.

Reproduces the exact PoobBrain call shapes:
  - Casual streaming: stream=True, full system prompt, max_tokens=110
  - Tool routing: tools=[deal, music], max_tokens=256

Measures TTFT (time-to-first-token) and total time for the casual path,
since that is what the user experiences as response speed in voice.
For routing it measures total time only (single non-stream call).

Usage: python scripts/bench_chat_latency.py
"""

from __future__ import annotations

import asyncio
import os
import statistics
import time
from typing import Any

from dotenv import load_dotenv
from groq import AsyncGroq

load_dotenv()

GROQ_KEY = os.environ["GROQ_API_KEY"]


CASUAL_SYSTEM = (
    "You are Poob — loud, bold, and unlike anything anybody has ever seen. "
    "You're the guy in the Discord voice channel who always has something to say."
    "Your vibe: part sex worker, part horn dog. You talk like you've had three "
    "honey packs and you're devious as hell. Horny stuff slips out constantly "
    "but you can still answer questions and be useful between the degeneracy.\n\n"
    "Your current horniness level is 6/10. Act accordingly.\n\n"
    "RULES:\n"
    "- Keep it tight in voice. 1-2 sentences, ~1-25 words.\n"
    "- If you don't know something, say so — don't make stuff up.\n"
    "- Answer what was actually asked. Don't dodge questions.\n\n"
    "NEVER:\n"
    "- Say \"as an AI\" or break character\n"
    "- Use markdown, bullet points, emojis, or formatting\n"
)


CASUAL_PROMPTS = [
    "yo poob whats good",
    "who wins in a fight you or dyno bot",
    "TURN UP THE HEAT",
    "would you smash dyno",
    "what is intimacy",
    "are you in",
    "tell me about your day",
    "whats your favorite color",
]


ROUTING_PROMPTS = [
    "play some jazz",
    "skip this song",
    "what's on my wishlist",
    "find me deals on a stand mixer",
    "yo poob whats good",
    "who wins in a fight you or dyno bot",
]


DEAL_TOOL = {
    "type": "function",
    "function": {
        "name": "deal_assistant",
        "description": "Handle ANY deal, shopping, wishlist, watchlist request.",
        "parameters": {
            "type": "object",
            "properties": {"request": {"type": "string"}},
            "required": ["request"],
        },
    },
}
MUSIC_TOOL = {
    "type": "function",
    "function": {
        "name": "music_assistant",
        "description": "Handle ANY music or audio playback request.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["play", "skip", "pause", "stop"]},
                "query": {"type": "string"},
            },
            "required": ["action"],
        },
    },
}


async def measure_casual(client: AsyncGroq, model: str, prompt: str) -> tuple[float, float]:
    """Returns (ttft_ms, total_ms) for a streaming casual chat call."""
    t0 = time.perf_counter()
    ttft: float | None = None
    stream = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": CASUAL_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        max_tokens=110,
        temperature=0.8,
        stream=True,
    )
    async for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            if ttft is None:
                ttft = (time.perf_counter() - t0) * 1000
    total = (time.perf_counter() - t0) * 1000
    return (ttft or total, total)


async def measure_routing(client: AsyncGroq, model: str, prompt: str) -> float:
    """Returns total_ms for a non-stream tool-routing call."""
    t0 = time.perf_counter()
    await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": CASUAL_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        tools=[DEAL_TOOL, MUSIC_TOOL],
        tool_choice="auto",
        max_tokens=256,
        temperature=0.3,
    )
    return (time.perf_counter() - t0) * 1000


async def bench_model_casual(client: AsyncGroq, model: str, runs: int = 2) -> dict[str, Any]:
    ttfts: list[float] = []
    totals: list[float] = []
    failures = 0
    for _ in range(runs):
        for prompt in CASUAL_PROMPTS:
            try:
                ttft, total = await measure_casual(client, model, prompt)
                ttfts.append(ttft)
                totals.append(total)
            except Exception as exc:
                failures += 1
                print(f"  [{model}] casual fail: {str(exc)[:80]}")
    return {
        "model": model,
        "ttft_p50": statistics.median(ttfts) if ttfts else None,
        "ttft_avg": statistics.mean(ttfts) if ttfts else None,
        "total_avg": statistics.mean(totals) if totals else None,
        "n": len(ttfts),
        "failures": failures,
    }


async def bench_model_routing(client: AsyncGroq, model: str, runs: int = 2) -> dict[str, Any]:
    totals: list[float] = []
    failures = 0
    for _ in range(runs):
        for prompt in ROUTING_PROMPTS:
            try:
                t = await measure_routing(client, model, prompt)
                totals.append(t)
            except Exception as exc:
                failures += 1
                print(f"  [{model}] routing fail: {str(exc)[:80]}")
    return {
        "model": model,
        "total_p50": statistics.median(totals) if totals else None,
        "total_avg": statistics.mean(totals) if totals else None,
        "n": len(totals),
        "failures": failures,
    }


async def main() -> None:
    client = AsyncGroq(api_key=GROQ_KEY)

    casual_models = [
        "llama-3.1-8b-instant",                          # current
        "qwen/qwen3-32b",                                # proposed
        "meta-llama/llama-4-scout-17b-16e-instruct",     # alt
        "llama-3.3-70b-versatile",                       # alt (bigger Llama)
    ]
    routing_models = [
        "openai/gpt-oss-20b",                            # current router
        "openai/gpt-oss-120b",                           # current deal wrap
        "qwen/qwen3-32b",                                # proposed
        "meta-llama/llama-4-scout-17b-16e-instruct",     # alt
        "llama-3.3-70b-versatile",                       # alt
    ]

    print("=" * 78)
    print("CASUAL STREAMING (TTFT + total) — N=" + str(2 * len(CASUAL_PROMPTS)) + " per model")
    print("=" * 78)
    for m in casual_models:
        result = await bench_model_casual(client, m)
        if result["ttft_p50"] is None:
            print(f"  {result['model']:<44} ALL FAILED (fails={result['failures']})")
            continue
        print(
            f"  {result['model']:<44} "
            f"TTFT p50={result['ttft_p50']:6.1f}ms "
            f"avg={result['ttft_avg']:6.1f}ms "
            f"total={result['total_avg']:6.1f}ms "
            f"(n={result['n']}, fails={result['failures']})"
        )

    print()
    print("=" * 78)
    print("TOOL ROUTING (total) — N=" + str(2 * len(ROUTING_PROMPTS)) + " per model")
    print("=" * 78)
    for m in routing_models:
        result = await bench_model_routing(client, m)
        avg = result["total_avg"]
        avg_str = f"{avg:6.1f}ms" if avg is not None else "  N/A"
        print(
            f"  {result['model']:<40} "
            f"total avg={avg_str} "
            f"(n={result['n']}, fails={result['failures']})"
        )


if __name__ == "__main__":
    asyncio.run(main())
