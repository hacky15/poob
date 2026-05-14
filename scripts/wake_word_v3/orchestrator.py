"""Wake-word v3 sample-generation orchestrator.

Builds a massive positive + negative training corpus for "Hey Poob" by
combining:

  - 449 phrase variants (canonical + prefix mishears + phonetic neighbors
    + trailing-context + bare-stem + slurred forms) — see
    ``scripts/wake_word_v3/phrases.py``.
  - 47 Edge TTS English voices × 8 speaking rates — ``tts_edge.py``.
  - Per-sample clip augmentation (5 head + 5 tail + 15 mid-dropout +
    2 mid-cut = 27 augmented variants per base WAV) — ``clip_augment.py``.

Math: 449 × 47 × 8 = **168,824 base positive WAVs**. Augmentation
multiplies by ~27 → **~4.5M positive WAVs**, capped operationally by
disk + the orchestrator's runtime budget. Operator can tune the
``--max-base-samples`` and ``--augment-fraction`` flags to dial total
output.

Negative side: 101 adversarial phrases × 47 voices × 8 rates =
**~38k adversarial negative WAVs**, no augmentation (we want clean
"this is NOT a wake" signal).

Resumable: deterministic filenames keyed by ``(phrase, voice, rate)``.
Re-running skips existing files. Augmented variants live in a
sibling directory so the orchestrator can stop and restart at the
augmentation phase without re-running TTS.

This module is the engine. The CLI entry point is
``scripts/generate_wake_word_v3.py``.

See docs/decisions/wake-word-mass-augmentation-v3.md for design,
docs/runbooks/wake-word-retrain-v3.md for the operator procedure.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field

from . import clip_augment
from . import pitch_augment
from .audio_utils import list_wavs
from .phrases import (
    POSITIVE_PHRASES,
    NEGATIVE_PHRASES,
    stats as phrase_stats,
)
from .tts_edge import (
    ALL_RATES,
    ALL_VOICES,
    rate_tag,
    sanitize_filename_component,
    synth_one,
    voice_tag,
)


@dataclass
class GenConfig:
    """Tunable run parameters. The CLI sets these from argv."""

    out_root: str = "data/wake_word_training_v3"
    concurrency: int = 8          # Concurrent edge_tts requests
    progress_every: int = 250     # Log progress every N files generated
    skip_existing: bool = True
    max_base_samples: int | None = None  # Cap base-TTS samples for dry-runs
    augment_fraction: float = 1.0        # 0..1 — fraction of base WAVs to clip-augment
    # Fraction of base positives to additionally render with pitch/speed
    # perturbations. 0.25 is a sensible default — pitched samples should
    # be a "small but loud" minority per the user's spec, not the bulk.
    pitch_fraction: float = 0.25
    seed: int = 1729                     # Deterministic augmentation sampling
    voices: tuple[str, ...] = field(default_factory=lambda: ALL_VOICES)
    rates: tuple[str, ...] = field(default_factory=lambda: ALL_RATES)
    # Which pitch presets to apply when the pitch phase runs.
    pitch_presets: tuple[str, ...] = field(
        default_factory=lambda: pitch_augment.DEFAULT_PRESETS,
    )


def _filename_for(prefix: str, phrase: str, voice: str, rate: str) -> str:
    return (
        f"{prefix}_"
        f"{voice_tag(voice)}_"
        f"{rate_tag(rate)}_"
        f"{sanitize_filename_component(phrase)}.wav"
    )


def _build_jobs(
    phrases: list[str], voices: tuple[str, ...], rates: tuple[str, ...],
    prefix: str, out_dir: str, skip_existing: bool,
    cap: int | None = None,
) -> list[tuple[str, str, str, str]]:
    """Enumerate (phrase, voice, rate, out_path) jobs.

    Skips existing files when ``skip_existing`` is True. Stops at ``cap``
    if given (operator dry-runs).
    """
    existing = set(list_wavs(out_dir)) if skip_existing else set()
    jobs: list[tuple[str, str, str, str]] = []
    for phrase in phrases:
        for voice in voices:
            for rate in rates:
                fname = _filename_for(prefix, phrase, voice, rate)
                if fname in existing:
                    continue
                jobs.append((phrase, voice, rate, os.path.join(out_dir, fname)))
                if cap is not None and len(jobs) >= cap:
                    return jobs
    return jobs


async def _run_one_job(
    sem: asyncio.Semaphore,
    job: tuple[str, str, str, str],
) -> tuple[bool, str]:
    """Synthesize one sample under a concurrency-limiting semaphore.

    Returns ``(success, out_path)``.
    """
    phrase, voice, rate, out_path = job
    async with sem:
        wav = await synth_one(voice, phrase, rate)
        if wav is None:
            return (False, out_path)
        try:
            with open(out_path, "wb") as f:
                f.write(wav)
            return (True, out_path)
        except OSError:
            return (False, out_path)


async def generate_tts_phase(
    phrases: list[str],
    prefix: str,
    config: GenConfig,
) -> tuple[int, int, int]:
    """Run the multi-voice multi-rate TTS phase for one phrase set.

    Returns ``(generated, skipped, failed)`` counts. Files land at
    ``{out_root}/{prefix}/<filename>.wav``.
    """
    out_dir = os.path.join(config.out_root, prefix)
    os.makedirs(out_dir, exist_ok=True)
    jobs = _build_jobs(
        phrases, config.voices, config.rates, prefix, out_dir,
        config.skip_existing, cap=config.max_base_samples,
    )
    if not jobs:
        existing = len(list_wavs(out_dir))
        print(f"[{prefix}] nothing to do — {existing} files already on disk")
        return (0, existing, 0)

    print(
        f"[{prefix}] {len(jobs)} jobs queued "
        f"(phrases={len(phrases)} × voices={len(config.voices)} "
        f"× rates={len(config.rates)})"
    )

    sem = asyncio.Semaphore(config.concurrency)
    generated = 0
    failed = 0
    start = time.monotonic()
    last_log = start

    coros = [_run_one_job(sem, j) for j in jobs]
    for fut in asyncio.as_completed(coros):
        ok, _path = await fut
        if ok:
            generated += 1
        else:
            failed += 1

        # Throttle progress logging to once per progress_every results
        # (avoid drowning the operator's terminal).
        if (generated + failed) % config.progress_every == 0:
            elapsed = time.monotonic() - last_log
            rate_per_s = config.progress_every / elapsed if elapsed > 0 else 0
            print(
                f"[{prefix}] {generated + failed}/{len(jobs)} "
                f"(ok={generated} fail={failed}, {rate_per_s:.1f}/s)"
            )
            last_log = time.monotonic()

    total_elapsed = time.monotonic() - start
    print(
        f"[{prefix}] phase done: gen={generated} fail={failed} "
        f"in {total_elapsed:.1f}s"
    )
    skipped = len(list_wavs(out_dir)) - generated
    return (generated, skipped, failed)


def run_clip_augmentation(
    source_dir: str, out_dir: str, augment_fraction: float = 1.0,
    seed: int = 1729,
) -> tuple[int, int]:
    """Apply head/tail/mid clipping to every base WAV in ``source_dir``.

    Writes augmented variants to ``out_dir`` with deterministic
    ``<base>_<variant_tag>.wav`` filenames so re-runs skip existing.

    ``augment_fraction`` lets the operator dry-run with a subset
    (0.1 = augment 10% of base samples).

    Returns ``(generated, skipped)``.
    """
    import random

    os.makedirs(out_dir, exist_ok=True)
    base_files = list_wavs(source_dir)
    if not base_files:
        return (0, 0)

    rng = random.Random(seed)
    if augment_fraction < 1.0:
        sample_n = max(1, int(len(base_files) * augment_fraction))
        base_files = rng.sample(base_files, sample_n)

    existing = set(list_wavs(out_dir))
    generated = 0
    skipped = 0

    print(
        f"[augment] {len(base_files)} base WAVs → "
        f"applying clip augmentation (~27 variants each)"
    )

    for i, base_name in enumerate(base_files):
        base_path = os.path.join(source_dir, base_name)
        try:
            with open(base_path, "rb") as f:
                wav_bytes = f.read()
        except OSError:
            continue

        stem = base_name[:-4]  # strip .wav
        for tag, variant_bytes in clip_augment.generate_variants(wav_bytes):
            out_name = f"{stem}_{tag}.wav"
            if out_name in existing:
                skipped += 1
                continue
            out_path = os.path.join(out_dir, out_name)
            try:
                with open(out_path, "wb") as f:
                    f.write(variant_bytes)
                generated += 1
            except OSError:
                continue

        if (i + 1) % 500 == 0:
            print(f"[augment] processed {i + 1}/{len(base_files)} base files; "
                  f"gen={generated} skip={skipped}")

    print(f"[augment] done: gen={generated} skipped={skipped}")
    return (generated, skipped)


def run_pitch_augmentation(
    source_dir: str,
    out_dir: str,
    pitch_fraction: float = 0.25,
    seed: int = 1729,
    presets: tuple[str, ...] = pitch_augment.DEFAULT_PRESETS,
) -> tuple[int, int]:
    """Render pitched / speed-perturbed variants of a fraction of base WAVs.

    Per the user's directive — different frequencies + high vs low pitch
    + sped-up combinations as a SMALL-BUT-LOUD minority of the corpus.
    Default fraction is 0.25 (25% of base WAVs get pitch-augmented across
    every preset in ``presets``).

    Writes to ``out_dir`` with filenames ``<base-stem>_<pitch-preset>.wav``.
    Deterministic on ``seed`` for reproducibility. Resumable: skips
    existing.

    Returns ``(generated, skipped)``.
    """
    import random

    os.makedirs(out_dir, exist_ok=True)
    base_files = list_wavs(source_dir)
    if not base_files:
        return (0, 0)

    rng = random.Random(seed ^ 0xA110CA7)  # different seed namespace from clip
    if pitch_fraction < 1.0:
        sample_n = max(1, int(len(base_files) * pitch_fraction))
        base_files = rng.sample(base_files, sample_n)

    existing = set(list_wavs(out_dir))
    generated = 0
    skipped = 0

    print(
        f"[pitch] {len(base_files)} base WAVs × {len(presets)} pitch presets "
        f"= {len(base_files) * len(presets)} potential pitched WAVs"
    )

    for i, base_name in enumerate(base_files):
        base_path = os.path.join(source_dir, base_name)
        try:
            with open(base_path, "rb") as f:
                wav_bytes = f.read()
        except OSError:
            continue

        stem = base_name[:-4]  # strip .wav
        for tag, variant_bytes in pitch_augment.generate_variants(
            wav_bytes, presets=presets,
        ):
            out_name = f"{stem}_{tag}.wav"
            if out_name in existing:
                skipped += 1
                continue
            out_path = os.path.join(out_dir, out_name)
            try:
                with open(out_path, "wb") as f:
                    f.write(variant_bytes)
                generated += 1
            except OSError:
                continue

        if (i + 1) % 250 == 0:
            print(
                f"[pitch] processed {i + 1}/{len(base_files)} base files; "
                f"gen={generated} skip={skipped}"
            )

    print(f"[pitch] done: gen={generated} skipped={skipped}")
    return (generated, skipped)


async def run_full_pipeline(config: GenConfig) -> None:
    """End-to-end: positive TTS → negative TTS → pitch augment → clip augment.

    Negative samples are NOT augmented at all (clip or pitch) — we want
    clean "not a wake" signal, no augmented noise inflating the negative
    class.
    """
    pos_stats = phrase_stats()
    print("=" * 70)
    print("Wake-word v3 corpus generation")
    print("=" * 70)
    print(f"Phrase corpus: {pos_stats}")
    print(f"Output root:   {config.out_root}")
    print(f"Concurrency:   {config.concurrency}")
    print(
        f"Voices × Rates: {len(config.voices)} × {len(config.rates)} = "
        f"{len(config.voices) * len(config.rates)} acoustic identities per phrase"
    )
    print(f"Pitch fraction: {config.pitch_fraction} × {len(config.pitch_presets)} presets")
    print(f"Clip-augment fraction: {config.augment_fraction}")
    print()

    # Phase 1 — positive TTS
    print("PHASE 1: positive TTS synthesis")
    await generate_tts_phase(POSITIVE_PHRASES, "pos", config)
    print()

    # Phase 2 — negative TTS (no augmentation)
    print("PHASE 2: negative TTS synthesis")
    await generate_tts_phase(NEGATIVE_PHRASES, "neg", config)
    print()

    # Phase 3 — pitch / speed perturbation of a fraction of positives.
    # Runs BEFORE clip augmentation so clip-augment then operates on
    # both base + pitched WAVs in one pass.
    pos_src = os.path.join(config.out_root, "pos")
    pos_pitched = os.path.join(config.out_root, "pos_pitched")
    pos_aug = os.path.join(config.out_root, "pos_augmented")
    if config.pitch_fraction > 0 and config.pitch_presets:
        print("PHASE 3: positive pitch / speed perturbation")
        run_pitch_augmentation(
            pos_src, pos_pitched,
            pitch_fraction=config.pitch_fraction,
            seed=config.seed,
            presets=config.pitch_presets,
        )
        print()

    # Phase 4 — clip augmentation of positives (base + pitched).
    if config.augment_fraction > 0:
        print("PHASE 4: positive clip augmentation (base WAVs)")
        run_clip_augmentation(
            pos_src, pos_aug,
            augment_fraction=config.augment_fraction,
            seed=config.seed,
        )
        if config.pitch_fraction > 0 and config.pitch_presets:
            print()
            print("PHASE 4b: positive clip augmentation (pitched WAVs)")
            run_clip_augmentation(
                pos_pitched, pos_aug,
                augment_fraction=config.augment_fraction,
                seed=config.seed ^ 0x5EED,
            )
        print()

    # Phase 5 — summary
    pos_total = len(list_wavs(pos_src))
    pos_pitched_total = len(list_wavs(pos_pitched))
    pos_aug_total = len(list_wavs(pos_aug))
    neg_total = len(list_wavs(os.path.join(config.out_root, "neg")))
    print("=" * 70)
    print(f"FINAL CORPUS:")
    print(f"  positive base:      {pos_total:>10,}")
    print(f"  positive pitched:   {pos_pitched_total:>10,}")
    print(f"  positive augmented: {pos_aug_total:>10,}")
    print(f"  positive total:     {pos_total + pos_pitched_total + pos_aug_total:>10,}")
    print(f"  negative:           {neg_total:>10,}")
    print(
        "  GRAND TOTAL:        "
        f"{pos_total + pos_pitched_total + pos_aug_total + neg_total:>10,}"
    )
    print("=" * 70)
