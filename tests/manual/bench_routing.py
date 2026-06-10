"""Live head-to-head benchmark: FULL persona prompt vs SLIM routing prompt.

Gates the slim-routing-prompt change (docs/plans/slim-routing-prompt.md). For
each utterance it routes the SAME message through both system prompts on a live
model and compares (a) the tool decision and (b) the latency. The pass bar:

  * CORRECTNESS — zero decision flips between full and slim on all utterances.
    The crude request (#7) MUST NOT regress route -> no-tool (the one residual
    risk the audit flagged).
  * LATENCY — slim median <= full median per utterance (expected from ~490-535
    fewer input tokens).

NOT a unit test — it hits live providers and consumes free-tier quota. Run it
deliberately, not in CI.

Quota note: defaults to the GEMINI rung (request-capped ~1500 RPD, NOT
token-capped) so it does NOT burn Groq's 200K-token/day budget. Pass
``--provider groq`` only when you accept the Groq token cost (each call is
~1.5-1.8k input tokens; reps x utterances x 2 prompts adds up fast).

Usage (from repo root, venv active):
    python tests/manual/bench_routing.py                 # gemini, 3 reps
    python tests/manual/bench_routing.py --reps 5
    python tests/manual/bench_routing.py --provider groq --reps 1
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
from time import perf_counter

from poob.brain.poob import (
    DEAL_TOOL,
    MUSIC_TOOL,
    PoobBrain,
    _build_routing_prompt,
    _build_system_prompt,
)
from poob.config import AppConfig

# (utterance, music_playing) — music_playing injects the live track context
# block exactly as _build_messages would, since several cases depend on it.
MATRIX: list[tuple[str, bool]] = [
    ("slow it down and reverb", True),     # effect routing (cascade-outage 2nd wave)
    ("skip", True),                        # control gate
    ("play tiki tiki", False),             # nonsense-name song
    ("what's on my wishlist", False),      # deal-side tool-worthy
    ("normal volume", True),               # volume NOT effect (normal-volume regression)
    ("flip a coin", False),                # NOT music -> no tool
    ("play some fuckin nightcore", False), # crude -> MUST still route, not refuse
]

_TRACK = "Daft Punk - One More Time [3:58]"


def _decision(name: str | None, args: dict | None) -> str:
    """Compact, comparable summary of a routing decision."""
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


def _maybe_token_count(messages: list[dict], tools: list[dict]) -> int | None:
    try:
        import json

        import tiktoken
    except ImportError:
        return None
    enc = tiktoken.get_encoding("cl100k_base")
    blob = "\n".join(m["content"] for m in messages) + json.dumps(tools)
    return len(enc.encode(blob))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="gemini", choices=["gemini", "groq", "nvidia"])
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--delay", type=float, default=0.0,
                    help="seconds between calls — set ~4 on gemini to stay under "
                         "its ~20 RPM free-tier burst limit")
    args = ap.parse_args()

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

    model = {
        "gemini": brain.gemini_router_model,
        "groq": brain.groq_model,
        "nvidia": getattr(brain, "nvidia_model", ""),
    }[args.provider]

    full_prompt = _build_system_prompt(5, voice=True, with_tools=True)
    slim_prompt = _build_routing_prompt(with_music=True)

    ft = _maybe_token_count(_messages(full_prompt, "skip", True), tools)
    st = _maybe_token_count(_messages(slim_prompt, "skip", True), tools)
    print(f"provider={args.provider} model={model} reps={args.reps}")
    if ft and st:
        print(f"input tokens (incl. tool schemas): full={ft}  slim={st}  saved={ft - st}")
    print(f"estimated live calls: {len(MATRIX) * 2 * args.reps}\n")

    flips = 0
    slower = 0
    for utterance, playing in MATRIX:
        results: dict[str, dict] = {}
        for label, system in (("full", full_prompt), ("slim", slim_prompt)):
            msgs = _messages(system, utterance, playing)
            lats: list[float] = []
            decisions: list[str] = []
            for _ in range(args.reps):
                t0 = perf_counter()
                try:
                    _text, name, targs = await brain._call_provider_with_tools(
                        args.provider, model, msgs, tools, 256,
                    )
                    decisions.append(_decision(name, targs))
                except Exception as exc:  # noqa: BLE001 - bench reports, doesn't raise
                    decisions.append(f"ERROR:{str(exc)[:40]}")
                lats.append((perf_counter() - t0) * 1000)
                if args.delay:
                    await asyncio.sleep(args.delay)
            results[label] = {
                "median": statistics.median(lats),
                "p95": max(lats),
                "modal": statistics.mode(decisions),
                "all": decisions,
            }

        f, s = results["full"], results["slim"]
        flip = f["modal"] != s["modal"]
        lat_regress = s["median"] > f["median"] * 1.10
        flips += flip
        slower += lat_regress
        flag = "  <<< DECISION FLIP" if flip else ""
        lat_flag = "  <<< SLIM SLOWER >10%" if lat_regress else ""
        print(f"[{'PLAYING' if playing else 'idle   '}] {utterance!r}")
        print(f"    full: {f['modal']:<28} {f['median']:6.0f}ms (p95 {f['p95']:.0f})")
        print(f"    slim: {s['modal']:<28} {s['median']:6.0f}ms (p95 {s['p95']:.0f}){flag}{lat_flag}")
        if flip:
            print(f"    full all={f['all']}  slim all={s['all']}")

    print(f"\nSUMMARY: {flips} decision flip(s), {slower} latency regression(s) over "
          f"{len(MATRIX)} utterances.")
    if flips == 0 and slower == 0:
        print("PASS — slim prompt routes identically, no slower. Safe to ship.")
    else:
        print("INVESTIGATE — see flagged rows above before shipping "
              "(crude #7 route->no-tool flip = revert the crude-clause cut).")


if __name__ == "__main__":
    asyncio.run(main())
