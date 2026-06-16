"""Live provider head-to-head for the VC tool-router FRONT slot.

Answers the operator question: should Gemini 3.1-flash-lite-preview REPLACE Groq
gpt-oss-20b as the primary VC router (not just sit behind it as a fallback)?

For each representative utterance it routes the SAME production SLIM routing
prompt through each provider, `--reps` times, and reports per provider:
  * ACCURACY — decisions vs the expected routing oracle (per utterance).
  * LATENCY — median + p95 ms.

This is the data the "benchmark before swapping any routing model" rule wants:
a clean Gemini-vs-Groq comparison on identical inputs.

NOT a unit test — hits live providers and consumes free-tier quota. Groq calls
share the prod bot's 200K-token/DAY budget (each ~1.5-1.8k input tokens); a
429 here means Groq's daily cap is already spent (itself a finding). Gemini is
RPD-limited, not token-capped.

Usage (repo root, venv active):
    python tests/manual/bench_router_providers.py                 # gemini+groq, 3 reps
    python tests/manual/bench_router_providers.py --reps 5 --delay 4
    python tests/manual/bench_router_providers.py --providers gemini,groq,nvidia
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
from collections import Counter
from time import perf_counter

from poob.brain.poob import (
    DEAL_TOOL,
    MUSIC_TOOL,
    PoobBrain,
    _build_routing_prompt,
)
from poob.config import AppConfig

# (utterance, music_playing, expected_decision_prefix) — expected is the
# routing oracle: _decision(...) must start with this. "music:" alone is
# lenient (any music action ok); the action-specific ones lock the known
# regression risks (volume!=effect, crude-still-routes, nonsense-name-no-halluc).
MATRIX: list[tuple[str, bool, str]] = [
    ("slow it down and reverb", True, "music:apply_effect"),  # effect routing
    ("skip", True, "music:skip"),                             # control gate
    ("play tiki tiki", False, "music:play"),                  # nonsense-name song
    ("what's on my wishlist", False, "deal"),                 # deal-side tool-worthy
    ("normal volume", True, "music:volume"),                  # volume NOT effect
    ("flip a coin", False, "NO_TOOL"),                         # NOT music -> no tool
    ("play some fuckin nightcore", False, "music:play"),      # crude -> MUST route
]

_TRACK = "Daft Punk - One More Time [3:58]"


def _decision(name: str | None, args: dict | None) -> str:
    if not name:
        return "NO_TOOL"
    if name == "music_assistant":
        a = (args or {}).get("action")
        extra = (args or {}).get("query") or (args or {}).get("effect") or ""
        return f"music:{a}:{extra}".rstrip(":")
    if name == "deal_assistant":
        return "deal"
    return name


def _messages(system: str, utterance: str, playing: bool) -> list[dict]:
    if playing:
        system = system + PoobBrain._music_context_block(_TRACK)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": utterance},
    ]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--providers", default="gemini,groq",
                    help="comma list from gemini,groq,nvidia")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--delay", type=float, default=0.0,
                    help="seconds between calls — ~4 on gemini to respect ~20 RPM burst")
    args = ap.parse_args()
    providers = [p.strip() for p in args.providers.split(",") if p.strip()]

    cfg = AppConfig()
    brain = PoobBrain(
        deal_agent=None,
        groq_api_key=cfg.groq_api_key,
        google_api_key=cfg.google_api_key,
        nvidia_api_key=cfg.nvidia_api_key,
        gemini_router_model=cfg.agent_google_model,
    )
    brain._music_handler = object()  # non-None -> MUSIC_TOOL present, with_music=True
    tools = [DEAL_TOOL, MUSIC_TOOL]
    slim_prompt = _build_routing_prompt(with_music=True)

    model_for = {
        "gemini": brain.gemini_router_model,
        "groq": brain.groq_model,
        "nvidia": getattr(brain, "nvidia_model", ""),
    }
    print(f"providers={providers} reps={args.reps}")
    for p in providers:
        print(f"  {p}: {model_for.get(p)}")
    print(f"estimated live calls: {len(MATRIX) * len(providers) * args.reps}\n")

    # provider -> {correct, total, latencies[]}
    agg: dict[str, dict] = {p: {"correct": 0, "total": 0, "lats": []} for p in providers}

    for utterance, playing, expected in MATRIX:
        print(f"[{'PLAYING' if playing else 'idle   '}] {utterance!r}  (expect {expected})")
        msgs = _messages(slim_prompt, utterance, playing)
        for p in providers:
            lats: list[float] = []
            decisions: list[str] = []
            for _ in range(args.reps):
                t0 = perf_counter()
                try:
                    _t, name, targs = await brain._call_provider_with_tools(
                        p, model_for[p], msgs, tools, 256,
                    )
                    decisions.append(_decision(name, targs))
                except Exception as exc:  # noqa: BLE001 - bench reports, doesn't raise
                    decisions.append(f"ERROR:{str(exc)[:50]}")
                lats.append((perf_counter() - t0) * 1000)
                if args.delay:
                    await asyncio.sleep(args.delay)
            modal = Counter(decisions).most_common(1)[0][0]
            correct = modal.startswith(expected)
            agg[p]["correct"] += int(correct)
            agg[p]["total"] += 1
            agg[p]["lats"].extend(lats)
            mark = "OK " if correct else "MISS"
            med = statistics.median(lats)
            print(f"    {p:<7} {mark} {modal:<28} {med:6.0f}ms (p95 {max(lats):.0f})  all={decisions}")
        print()

    print("=" * 64)
    print("SUMMARY (front-slot candidates)")
    for p in providers:
        a = agg[p]
        lats = a["lats"]
        med = statistics.median(lats) if lats else 0
        p95 = (sorted(lats)[max(0, int(len(lats) * 0.95) - 1)]) if lats else 0
        print(f"  {p:<7} accuracy {a['correct']}/{a['total']}  "
              f"latency median {med:.0f}ms  p95 {p95:.0f}ms  (n={len(lats)})")
    print("\nDecide front slot on: accuracy first, then median latency. "
          "Groq ERROR rows = daily token cap already spent.")


if __name__ == "__main__":
    asyncio.run(main())
