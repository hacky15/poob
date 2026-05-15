"""CLI entry point for the v3 wake-word corpus generator.

Usage (from repo root):

  # Full run — everything (~hours-to-days depending on disk + concurrency).
  python -m scripts.wake_word_v3

  # Dry-run with caps for testing the pipeline before committing to a
  # multi-hour run.
  python -m scripts.wake_word_v3 --max-base 1000 --augment-fraction 0.1

  # Just count what's already on disk.
  python -m scripts.wake_word_v3 --count

  # Tune concurrency for your edge_tts rate-limit tolerance (default 8).
  python -m scripts.wake_word_v3 --concurrency 16

Output landscape (canonical):

  data/wake_word_training_v3/
    pos/             — base positive TTS WAVs (~169k)
    neg/             — adversarial negative TTS WAVs (~38k)
    pos_augmented/   — clip-augmented positives (~4M+ at full run)

The pipeline is **resumable**. Filenames are deterministic on
``(prefix, voice, rate, phrase)`` so re-running skips existing.
Augmented variants are tagged with the clip mode + size so they
also skip cleanly.

After generation completes, feed the corpus into
``scripts/train_hey_poob_v2.py`` via openWakeWord's feature
extractor (operator-side, in the WSL2 training env). See
``docs/runbooks/wake-word-retrain-v3.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Force UTF-8 on stdout/stderr — Windows PowerShell defaults to cp1252,
# which crashes on the unicode arrows / multiplication signs / em dashes
# this script prints in progress and plan lines.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

from .audio_utils import list_wavs
from .orchestrator import GenConfig, run_full_pipeline
from .phrases import stats as phrase_stats
from .tts_edge import ALL_VOICES, ALL_RATES


def _print_count(out_root: str) -> None:
    """Print on-disk corpus sizes; non-destructive."""
    pos = len(list_wavs(os.path.join(out_root, "pos")))
    neg = len(list_wavs(os.path.join(out_root, "neg")))
    aug = len(list_wavs(os.path.join(out_root, "pos_augmented")))
    print(f"out_root: {out_root}")
    print(f"  positive base:      {pos:>10,}")
    print(f"  positive augmented: {aug:>10,}")
    print(f"  positive total:     {pos + aug:>10,}")
    print(f"  negative:           {neg:>10,}")
    print(f"  GRAND TOTAL:        {pos + aug + neg:>10,}")


def _print_plan(config: GenConfig) -> None:
    """Show the operator what's about to happen — phrase counts ×
    voices × rates × augmentation — before they commit a multi-hour run."""
    s = phrase_stats()
    n_voices = len(config.voices)
    n_rates = len(config.rates)
    base_pos = s["positive_total"] * n_voices * n_rates
    base_neg = s["negative_total"] * n_voices * n_rates
    if config.max_base_samples is not None:
        base_pos = min(base_pos, config.max_base_samples)
        base_neg = min(base_neg, config.max_base_samples)
    # Pitch phase: a fraction of base positives × number of presets.
    n_pitch_presets = len(config.pitch_presets)
    pitched = int(base_pos * config.pitch_fraction * n_pitch_presets)
    # Clip-augment phase runs on BOTH base and pitched WAVs.
    aug_per_wav = 27  # head 5 + tail 5 + mid_dropout 15 + mid_cut 2
    clip_source_total = base_pos + pitched
    clip_aug_total = int(clip_source_total * config.augment_fraction * aug_per_wav)
    grand_total = base_pos + pitched + clip_aug_total + base_neg
    print("=" * 70)
    print("Wake-word v3 plan")
    print("=" * 70)
    print(f"  phrase corpus: {s}")
    print(f"  voices × rates: {n_voices} × {n_rates}")
    print(f"  base positive WAVs:      ~{base_pos:>12,}")
    print(f"  base negative WAVs:      ~{base_neg:>12,}")
    print(
        f"  pitched positives:       ~{pitched:>12,} "
        f"(fraction={config.pitch_fraction} × {n_pitch_presets} presets)"
    )
    print(
        f"  clip-augmented positives:~{clip_aug_total:>12,} "
        f"(fraction={config.augment_fraction} × ~{aug_per_wav}/source)"
    )
    print(f"  GRAND TOTAL (estimate):  ~{grand_total:>12,}")
    print(f"  out_root: {config.out_root}")
    print(f"  concurrency: {config.concurrency}")
    print(f"  skip_existing: {config.skip_existing}")
    print("=" * 70)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m scripts.wake_word_v3",
        description=__doc__.splitlines()[0] if __doc__ else "",
    )
    ap.add_argument(
        "--out-root", default="data/wake_word_training_v3",
        help="Output directory root (default: data/wake_word_training_v3)",
    )
    ap.add_argument(
        "--concurrency", type=int, default=8,
        help="Concurrent edge_tts requests (default: 8)",
    )
    ap.add_argument(
        "--max-base", type=int, default=None, metavar="N",
        help="Cap base-TTS sample count per phase (dry-run helper)",
    )
    ap.add_argument(
        "--augment-fraction", type=float, default=1.0,
        help="Fraction of base positives to clip-augment (0..1, default 1.0)",
    )
    ap.add_argument(
        "--no-augment", action="store_true",
        help="Skip the clip-augmentation phase entirely",
    )
    ap.add_argument(
        "--pitch-fraction", type=float, default=0.25,
        help=(
            "Fraction of base positives to render with pitch / speed "
            "perturbation (0..1, default 0.25 — a small-but-loud minority "
            "per the corpus design)"
        ),
    )
    ap.add_argument(
        "--no-pitch", action="store_true",
        help="Skip the pitch / speed perturbation phase entirely",
    )
    ap.add_argument(
        "--skip-existing", action=argparse.BooleanOptionalAction, default=True,
        help="Skip jobs whose output file already exists (default: True)",
    )
    ap.add_argument(
        "--seed", type=int, default=1729,
        help="RNG seed for deterministic augmentation sampling",
    )
    ap.add_argument(
        "--count", action="store_true",
        help="Print on-disk corpus sizes and exit",
    )
    ap.add_argument(
        "--plan", action="store_true",
        help="Print the planned job counts and exit (no generation)",
    )
    args = ap.parse_args(argv)

    if args.count:
        _print_count(args.out_root)
        return 0

    config = GenConfig(
        out_root=args.out_root,
        concurrency=args.concurrency,
        max_base_samples=args.max_base,
        augment_fraction=0.0 if args.no_augment else args.augment_fraction,
        pitch_fraction=0.0 if args.no_pitch else args.pitch_fraction,
        skip_existing=args.skip_existing,
        seed=args.seed,
        voices=ALL_VOICES,
        rates=ALL_RATES,
    )

    _print_plan(config)
    if args.plan:
        return 0

    asyncio.run(run_full_pipeline(config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
