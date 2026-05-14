"""Wake-word v3 sample-generation CLI.

Generates the full positive + negative training corpus for "Hey Poob"
v3. The architecture and rationale live in:

  - docs/decisions/wake-word-mass-augmentation-v3.md (design + tradeoffs)
  - docs/runbooks/wake-word-retrain-v3.md (operator procedure end-to-end)
  - docs/research/wake-word-augmentation-2026.md (SOTA survey backing the
    design choices)

Usage:

  # Full run (takes hours; resumable — re-run picks up where it left off)
  python scripts/generate_wake_word_v3.py

  # Stats only
  python scripts/generate_wake_word_v3.py --stats

  # Dry run — cap base TTS samples + augment only 10% of base WAVs
  python scripts/generate_wake_word_v3.py --max-base 2000 --augment-fraction 0.1

  # Crank concurrency on a fast machine
  python scripts/generate_wake_word_v3.py --concurrency 16

  # Custom output dir
  python scripts/generate_wake_word_v3.py --out-root /mnt/big-disk/wake_v3
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Make sure ``scripts`` is on sys.path so the wake_word_v3 sub-package
# imports cleanly when this file is invoked directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.wake_word_v3.audio_utils import list_wavs
from scripts.wake_word_v3.orchestrator import GenConfig, run_full_pipeline
from scripts.wake_word_v3.phrases import stats as phrase_stats


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--out-root", default="data/wake_word_training_v3",
        help="Output directory root (default: %(default)s)",
    )
    p.add_argument(
        "--concurrency", type=int, default=8,
        help="Concurrent Edge TTS requests (default: %(default)d)",
    )
    p.add_argument(
        "--max-base", type=int, default=None,
        help="Cap total base-TTS samples (dry-run / partial generation)",
    )
    p.add_argument(
        "--augment-fraction", type=float, default=1.0,
        help="Fraction of base WAVs to clip-augment (0..1, default 1.0)",
    )
    p.add_argument(
        "--seed", type=int, default=1729,
        help="Deterministic seed for augmentation sampling",
    )
    p.add_argument(
        "--no-resume", action="store_true",
        help="Disable skip-existing — regenerate every sample from scratch",
    )
    p.add_argument(
        "--stats", action="store_true",
        help="Show phrase counts + on-disk WAV counts, then exit",
    )
    return p.parse_args()


def _print_stats(out_root: str) -> None:
    print("=" * 70)
    print("Phrase corpus (in-memory):")
    for k, v in phrase_stats().items():
        print(f"  {k:<20} {v:>8,}")
    print()
    print(f"On-disk counts under {out_root}:")
    for sub in ("pos", "neg", "pos_augmented"):
        d = os.path.join(out_root, sub)
        n = len(list_wavs(d)) if os.path.isdir(d) else 0
        print(f"  {sub:<20} {n:>8,}")
    print("=" * 70)


async def _main_async() -> None:
    args = _parse_args()

    if args.stats:
        _print_stats(args.out_root)
        return

    config = GenConfig(
        out_root=args.out_root,
        concurrency=args.concurrency,
        max_base_samples=args.max_base,
        augment_fraction=max(0.0, min(1.0, args.augment_fraction)),
        seed=args.seed,
        skip_existing=not args.no_resume,
    )
    await run_full_pipeline(config)


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
