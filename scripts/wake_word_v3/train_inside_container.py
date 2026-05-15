"""In-container training driver — runs feature extraction + training.

This script ONLY runs inside the wake-word-trainer Docker image (built
from ``Dockerfile.training``). Host invocation lives in
``train_in_docker.sh``.

Pipeline:

  1. Resolve corpus directories under /work (mounted from host
     F:\\wake_word_v3\\, which has pos/, neg/, pos_pitched/, pos_augmented/
     as direct children).
  2. Extract features for positives (pos + pos_pitched + pos_augmented),
     concatenate into a single numpy array, save to /work/features/.
  3. Extract features for adversarial negatives (neg), save.
  4. Download ACAV100M precomputed features once into /work/acav/
     (cached on host disk via the volume mount).
  5. Train via the openWakeWord Model class with the standard recipe
     (cosine LR + warmup + curriculum negative weighting + hard example
     mining). 50,000 steps; checkpoints to /work/models/.
  6. Export the final ONNX to /work/models/hey_poob_v3.onnx.

Everything is resumable: feature files are skipped if they already
exist on disk; ACAV is skipped if already downloaded; training
checkpoints would survive between invocations if we wanted (we don't
here — one-shot run).

The script is intentionally light on abstractions. Operator runs it
once via the wrapper, watches the logs, and at the end the ONNX is
sitting on the host filesystem.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np


# ---------------------------------------------------------------------------
# Paths — all relative to /work which is the host's corpus drive mount.
# ---------------------------------------------------------------------------

WORK = Path("/work")
CORPUS = WORK
FEATURES = WORK / "features"
ACAV_DIR = WORK / "acav"
MODELS = WORK / "models"

POS_DIRS = ["pos", "pos_pitched", "pos_augmented"]
NEG_DIRS = ["neg"]

for d in (FEATURES, ACAV_DIR, MODELS):
    d.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def count_wavs(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    return sum(1 for _ in directory.glob("*.wav"))


# ---------------------------------------------------------------------------
# Step 1: Feature extraction
# ---------------------------------------------------------------------------

def _load_wav_int16(wav_path: str, target_samples: int = 32000):
    """Load a 16-bit mono 16 kHz WAV, pad/trim to ``target_samples``.

    Returns int16 numpy array, or None on read error. 2s @ 16 kHz = 32000
    samples is openwakeword's canonical clip size for AudioFeatures.
    """
    import struct
    import wave
    try:
        with wave.open(wav_path, "rb") as wf:
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
            samples = np.array(struct.unpack(f"<{n_frames}h", raw), dtype=np.int16)
        if len(samples) < target_samples:
            samples = np.pad(samples, (0, target_samples - len(samples)))
        else:
            samples = samples[:target_samples]
        return samples.astype(np.int16)
    except Exception:
        return None


def extract_features(
    class_label: str,
    source_dirs: list[str],
    load_chunk: int = 4096,
    embed_batch: int = 64,
    device: str = "gpu",
) -> Path:
    """Stream WAVs through openWakeWord's AudioFeatures embedder and
    concatenate the per-clip embeddings into a single ``.npy``.

    Streaming-first design: we load WAVs in chunks of ``load_chunk``,
    embed them, write the chunk's feature block to the output memmap,
    and free the audio array. This keeps peak RAM at ~``load_chunk *
    32000 * 2 bytes`` (~256 MB for chunk=4096) regardless of corpus
    size, so it survives the 11M-WAV target without OOM.

    Resumable at the file level: if the output .npy already exists we
    skip the whole pass.
    """
    out_path = FEATURES / f"{class_label}_features.npy"
    if out_path.exists():
        arr = np.load(out_path, mmap_mode="r")
        log(f"features already extracted for {class_label}: {arr.shape}")
        return out_path

    from openwakeword.utils import AudioFeatures
    from numpy.lib.format import open_memmap

    wav_files: list[str] = []
    for sub in source_dirs:
        d = CORPUS / sub
        if not d.is_dir():
            log(f"  {sub}: dir missing, skipping")
            continue
        files = sorted(str(p) for p in d.glob("*.wav"))
        log(f"  {sub}: {len(files):,} WAVs queued")
        wav_files.extend(files)

    if not wav_files:
        raise RuntimeError(f"no WAVs found for class {class_label}")

    log(
        f"building AudioFeatures embedder for {class_label} "
        f"({len(wav_files):,} WAVs, device={device})"
    )
    af = AudioFeatures(device=device)
    log(f"  ONNX execution provider: {getattr(af, 'onnx_execution_provider', '?')}")

    feat_shape = None
    out_mmap = None
    written = 0
    skipped = 0
    t_start = time.monotonic()

    for chunk_start in range(0, len(wav_files), load_chunk):
        chunk = wav_files[chunk_start : chunk_start + load_chunk]
        clips: list[np.ndarray] = []
        for p in chunk:
            audio = _load_wav_int16(p, 32000)
            if audio is None:
                skipped += 1
                continue
            clips.append(audio)
        if not clips:
            continue
        audio_array = np.stack(clips)
        feats = af.embed_clips(audio_array, batch_size=embed_batch)

        if feat_shape is None:
            feat_shape = feats.shape[1:]
            total = len(wav_files)
            out_mmap = open_memmap(
                str(out_path),
                mode="w+",
                dtype=feats.dtype,
                shape=(total, *feat_shape),
            )
            log(
                f"  output memmap created: shape=({total}, {feat_shape}), "
                f"dtype={feats.dtype}, path={out_path}"
            )

        end = written + feats.shape[0]
        out_mmap[written:end] = feats
        written = end

        dt = time.monotonic() - t_start
        rate = (chunk_start + len(chunk)) / dt if dt > 0 else 0.0
        log(
            f"  [{class_label}] processed {chunk_start + len(chunk):,}/"
            f"{len(wav_files):,} WAVs ({rate:.0f} wav/s, "
            f"written={written:,} skipped={skipped:,})"
        )

    if written < len(wav_files):
        # Trim the trailing zero rows (skipped reads) so the saved npy is dense.
        out_mmap.flush()
        del out_mmap
        full = np.load(out_path, mmap_mode="r")
        trimmed = np.array(full[:written])
        del full
        np.save(out_path, trimmed)
        log(f"  trimmed memmap to {trimmed.shape} after {skipped:,} skipped WAVs")
    else:
        out_mmap.flush()
        del out_mmap

    final = np.load(out_path, mmap_mode="r")
    log(
        f"  -> {class_label} features ready: shape={final.shape} "
        f"dtype={final.dtype} ({(time.monotonic() - t_start):.1f}s)"
    )
    return out_path


# ---------------------------------------------------------------------------
# Step 2: ACAV100M background features
# ---------------------------------------------------------------------------

def ensure_acav() -> Path:
    """Download ACAV100M precomputed features from HuggingFace.

    Cached under /work/acav so the (~2 GB) download happens once.
    """
    acav = ACAV_DIR / "openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
    if acav.exists():
        arr = np.load(acav, mmap_mode="r")
        log(f"ACAV already on disk: {arr.shape}")
        return acav

    log("downloading ACAV100M precomputed features (~2 GB)")
    from huggingface_hub import hf_hub_download

    # The features live in the dataset repo, not the model repo. The
    # original train_hey_poob_v2.py recipe pulls from
    # davidscripka/openwakeword_features (dataset).
    hf_hub_download(
        repo_id="davidscripka/openwakeword_features",
        filename="openwakeword_features_ACAV100M_2000_hrs_16bit.npy",
        repo_type="dataset",
        local_dir=str(ACAV_DIR),
    )
    log(f"ACAV downloaded -> {acav}")
    return acav


# ---------------------------------------------------------------------------
# Step 3: Training (mirrors scripts/train_hey_poob_v2.py recipe)
# ---------------------------------------------------------------------------

def build_negative_weight_schedule(max_steps: int, max_weight: float = 2000.0):
    """Linear ramp from weight=1 to weight=max_weight over training.

    Standard openWakeWord recipe — start with negatives roughly equal
    importance to positives, ramp up so adversarial negatives dominate
    the late-stage gradient (curriculum).
    """
    return [1.0 + (max_weight - 1.0) * (i / max_steps) for i in range(max_steps + 1)]


def run_training(
    pos_features_path: Path,
    neg_features_path: Path,
    acav_path: Path,
    steps: int = 50000,
    warmup_steps: int = 10000,
    hold_steps: int = 16667,
    layer_dim: int = 128,
    n_blocks: int = 1,
    lr: float = 1e-4,
    max_negative_weight: float = 2000.0,
    pos_per_batch: int = 64,
    neg_per_batch: int = 75,
    acav_per_batch: int = 128,
    max_acav_samples: int = 50_000,
) -> Path:
    """Run openWakeWord training via ``Model.train_model`` and emit ONNX.

    Mirrors the canonical recipe from
    ``scripts/train_hey_poob_v2.py``:

      * 3-source mmap_batch_generator: positive (label=1) +
        adversarial-negative (label=2 -> remapped to 0) +
        ACAV100M-background (label=0).
      * Validation set = last 5% of positives + equal-sized ACAV slice.
      * Negative-weight curriculum 1 -> max_negative_weight linearly.
      * Cosine LR with warmup + hold (warmup_steps + hold_steps total
        before decay; openwakeword handles the math).
      * Best-checkpoint averaging at the end before ONNX export.

    Memory: ACAV is subsampled to ``max_acav_samples`` before training
    because the full 5.6M-row file (~17 GB) blows RAM on commodity
    hardware. 50k is plenty given how the curriculum schedule weighs
    it relative to the hard adversarial negatives.
    """
    import torch
    from openwakeword.train import Model as OWWModel
    from openwakeword.data import mmap_batch_generator

    log(f"loading positive features from {pos_features_path}")
    pos = np.load(pos_features_path, mmap_mode="r")
    log(f"  positives: {pos.shape}")

    log(f"loading negative features from {neg_features_path}")
    neg = np.load(neg_features_path, mmap_mode="r")
    log(f"  negatives: {neg.shape}")

    log(f"loading ACAV background features from {acav_path}")
    acav = np.load(acav_path, mmap_mode="r")
    log(f"  ACAV: {acav.shape}")

    # Subsample ACAV — full 5.6M rows is too big for a commodity GPU
    # box's working memory.
    acav_sub_path = ACAV_DIR / f"acav_sub_{max_acav_samples}.npy"
    if not acav_sub_path.exists():
        log(f"subsampling ACAV to {max_acav_samples:,} rows -> {acav_sub_path}")
        np.save(acav_sub_path, np.array(acav[:max_acav_samples]))
    else:
        log(f"reusing ACAV subsample at {acav_sub_path}")

    # Validation split: last 5% of positives + equal-sized ACAV slice.
    n_val = max(64, int(pos.shape[0] * 0.05))
    val_pos = np.array(pos[-n_val:])
    acav_sub = np.load(acav_sub_path, mmap_mode="r")
    val_neg = np.array(acav_sub[:n_val])
    val_x = np.concatenate([val_pos, val_neg], axis=0).astype(np.float32)
    val_y = np.concatenate(
        [np.ones(len(val_pos)), np.zeros(len(val_neg))]
    ).astype(np.float32)
    val_data = [(torch.from_numpy(val_x), torch.from_numpy(val_y))]
    log(f"validation: {len(val_pos)} pos + {len(val_neg)} neg")

    # Build 3-source batch generator. Labels: "1"=positive,
    # "2"=adversarial-negative (remapped to 0), "0"=ACAV background.
    data_files = {
        "1": str(pos_features_path),
        "2": str(neg_features_path),
        "0": str(acav_sub_path),
    }
    n_per_class = {
        "1": pos_per_batch,
        "2": neg_per_batch,
        "0": acav_per_batch,
    }
    label_transform_funcs = {
        "2": lambda y: [0] * len(y),
    }
    log(
        f"batch composition per step: pos={pos_per_batch} "
        f"adversarial={neg_per_batch} acav={acav_per_batch}"
    )

    raw_gen = mmap_batch_generator(
        data_files=data_files,
        n_per_class=n_per_class,
        label_transform_funcs=label_transform_funcs,
    )

    def tensor_gen():
        for x, y in raw_gen:
            y_float = np.array(
                [1.0 if str(lbl) == "1" else 0.0 for lbl in y],
                dtype=np.float32,
            )
            yield (
                torch.from_numpy(x.copy()).float(),
                torch.from_numpy(y_float),
            )

    log("building Model")
    model = OWWModel(
        n_classes=1,
        input_shape=tuple(pos.shape[1:]),
        model_type="dnn",
        layer_dim=layer_dim,
        n_blocks=n_blocks,
    )
    log("Model summary:")
    model.summary()

    neg_weight_schedule = build_negative_weight_schedule(
        steps, max_weight=max_negative_weight,
    )

    log(
        f"starting training: steps={steps:,} warmup={warmup_steps:,} "
        f"hold={hold_steps:,} lr={lr}"
    )
    t0 = time.monotonic()
    model.train_model(
        X=tensor_gen(),
        X_val=val_data,
        max_steps=steps,
        warmup_steps=warmup_steps,
        hold_steps=hold_steps,
        negative_weight_schedule=neg_weight_schedule,
        lr=lr,
    )
    dt = time.monotonic() - t0
    log(f"training complete in {dt / 3600:.2f} hours")

    # Average best checkpoints before ONNX export.
    if getattr(model, "best_models", None):
        log(f"averaging {len(model.best_models)} best checkpoints")
        model.average_models(model.best_models)

    onnx_out = MODELS / "hey_poob_v3.onnx"
    model.export_to_onnx(str(onnx_out), class_mapping="hey_poob")
    log(f"ONNX exported -> {onnx_out}")

    if getattr(model, "history", None):
        for key in ("val_accuracy", "val_recall", "val_fp"):
            vals = model.history.get(key)
            if not vals:
                continue
            best = max(vals) if "fp" not in key else min(vals)
            log(f"  {key}: best={best:.4f}")
    return onnx_out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    log("Wake-word v3 training driver — inside container")
    log(f"CORPUS={CORPUS} (positives: {sum(count_wavs(CORPUS / d) for d in POS_DIRS):,}, "
        f"negatives: {sum(count_wavs(CORPUS / d) for d in NEG_DIRS):,})")
    log(f"FEATURES={FEATURES} MODELS={MODELS} ACAV_DIR={ACAV_DIR}")

    # Verify GPU is visible.
    try:
        import torch
        log(f"torch={torch.__version__} cuda_available={torch.cuda.is_available()} "
            f"device_count={torch.cuda.device_count()}")
        if torch.cuda.is_available():
            log(f"  device 0: {torch.cuda.get_device_name(0)}")
    except Exception as exc:
        log(f"torch import failed: {exc}")

    pos_path = extract_features("positive", POS_DIRS)
    neg_path = extract_features("negative", NEG_DIRS)
    acav_path = ensure_acav()
    onnx_path = run_training(pos_path, neg_path, acav_path)
    log(f"DONE. Trained model at host path: {onnx_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
