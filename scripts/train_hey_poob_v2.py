"""Train 'Hey Poob' using OpenWakeWord's official training pipeline.

Uses:
- Official OWW Model class with proper DNN architecture
- mmap_batch_generator for efficient data loading
- ACAV100M pre-computed features as background negatives (real-world audio)
- Custom positive + adversarial negative features
- Cosine LR decay with warmup
- Negative weight scheduling (ramps importance of negatives over training)
- Hard example mining (only backprop on high-loss samples)

This is the PROPER training approach — not a simple PyTorch BCELoss loop.

Usage (inside WSL2):
    cd ~/openWakeWord && source train_env/bin/activate
    python /mnt/c/Users/19203/Downloads/AgenticWebScraper/scripts/train_hey_poob_v2.py
"""

import os
import sys
import glob
import time
import numpy as np
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────
POSITIVE_FEATURES = os.path.expanduser("~/wake_word_training/hey_poob/features/positive_features.npy")
NEGATIVE_FEATURES = os.path.expanduser("~/wake_word_training/hey_poob/features/negative_features.npy")
OUTPUT_DIR = os.path.expanduser("~/wake_word_training/hey_poob/output_v2")
ACAV_FEATURES_DIR = os.path.expanduser("~/wake_word_training/acav100m")

# Training hyperparameters
STEPS = 50000
WARMUP_STEPS = 10000
HOLD_STEPS = 16667
LAYER_DIM = 128
N_BLOCKS = 1
LR = 0.0001


def download_acav_features():
    """Download ACAV100M pre-computed features from HuggingFace."""
    os.makedirs(ACAV_FEATURES_DIR, exist_ok=True)
    acav_path = os.path.join(ACAV_FEATURES_DIR, "openwakeword_features_ACAV100M_2000_hrs_16bit.npy")

    if os.path.exists(acav_path):
        arr = np.load(acav_path, mmap_mode='r')
        print(f"ACAV100M features already downloaded: {arr.shape}", flush=True)
        return acav_path

    print("Downloading ACAV100M pre-computed features from HuggingFace...", flush=True)
    print("(This is ~2GB of real-world audio embeddings — the critical negative data)", flush=True)

    from huggingface_hub import hf_hub_download
    downloaded = hf_hub_download(
        repo_id="davidscripka/openwakeword_features",
        filename="openwakeword_features_ACAV100M_2000_hrs_16bit.npy",
        repo_type="dataset",
        local_dir=ACAV_FEATURES_DIR,
    )
    # Move to expected location if needed
    if downloaded != acav_path and os.path.exists(downloaded):
        import shutil
        shutil.move(downloaded, acav_path)

    arr = np.load(acav_path, mmap_mode='r')
    print(f"ACAV100M features downloaded: {arr.shape}", flush=True)
    return acav_path


def build_negative_weight_schedule(max_steps, max_weight=50):
    """Build a linearly increasing negative weight schedule.

    Starts at 1 (equal weight) and ramps to max_weight over training.
    This forces the model to increasingly focus on reducing false positives.
    """
    schedule = np.linspace(1, max_weight, max_steps).tolist()
    return schedule


def main():
    print("=== Hey Poob Wake Word Training v2 (Official OWW Pipeline) ===\n", flush=True)

    # Verify features exist
    if not os.path.exists(POSITIVE_FEATURES):
        print(f"ERROR: Positive features not found at {POSITIVE_FEATURES}")
        print("Run train_hey_poob.py first to compute features from WAV files.")
        sys.exit(1)

    pos = np.load(POSITIVE_FEATURES, mmap_mode='r')
    print(f"Positive features: {pos.shape}", flush=True)

    if os.path.exists(NEGATIVE_FEATURES):
        neg = np.load(NEGATIVE_FEATURES, mmap_mode='r')
        print(f"Adversarial negative features: {neg.shape}", flush=True)
    else:
        print("WARNING: No adversarial negative features found. Using only ACAV100M.", flush=True)

    # Download ACAV100M real-world negative features
    acav_path = download_acav_features()
    acav = np.load(acav_path, mmap_mode='r')
    print(f"ACAV100M background features: {acav.shape}", flush=True)

    # Subsample ACAV to avoid OOM — 500K samples is plenty (vs 5.6M)
    # The full 17GB file causes OOM in WSL2 with 8GB+ GPU VRAM contention
    MAX_ACAV = 50_000
    acav_sub_path = os.path.join(ACAV_FEATURES_DIR, f"acav_sub_{MAX_ACAV}.npy")
    if not os.path.exists(acav_sub_path):
        print(f"Subsampling ACAV: {acav.shape[0]} → {MAX_ACAV} (sequential slice)...", flush=True)
        # Take first MAX_ACAV samples — sequential access is mmap-friendly (no random IO)
        sub = np.array(acav[:MAX_ACAV])
        np.save(acav_sub_path, sub)
        del sub  # Free RAM immediately
        print(f"Saved subsampled ACAV", flush=True)
    else:
        sub = np.load(acav_sub_path, mmap_mode='r')
        print(f"Loaded subsampled ACAV: {sub.shape}", flush=True)
    acav_path = acav_sub_path

    # Verify feature shapes match (all should be (N, 16, 96))
    assert pos.shape[1:] == (16, 96), f"Unexpected positive shape: {pos.shape}"

    # Build data files dict for mmap_batch_generator.
    # Keys become labels: "1" = positive, "0" = ACAV negative, "2" = adversarial negative.
    # label_transform_funcs maps "2" → 0 so the trainer sees binary (0 vs 1).
    # mmap_batch_generator uses mmap_mode='r' so the 17GB ACAV file stays on disk.
    data_files = {
        "1": POSITIVE_FEATURES,
        "0": acav_path,
    }
    n_per_class = {
        "1": 64,
        "0": 128,
    }
    label_transform_funcs = {}

    if os.path.exists(NEGATIVE_FEATURES):
        data_files["2"] = NEGATIVE_FEATURES
        n_per_class["2"] = 75
        # Map label "2" → 0 (negative) so trainer sees binary classification
        label_transform_funcs["2"] = lambda y: [0] * len(y)

    print(f"\nData files:", flush=True)
    for key, path in data_files.items():
        arr = np.load(path, mmap_mode='r')
        label_name = {
            "1": "positive",
            "0": "ACAV background",
            "2": "adversarial negative"
        }.get(key, key)
        print(f"  [{key}] {label_name}: {arr.shape} ({os.path.basename(path)})", flush=True)

    # Import OpenWakeWord training components
    # Bypass openwakeword.data (has broken deps: acoustics/scipy compat, speechbrain, etc.)
    # Import Model directly and use mmap_batch_generator from the source file
    import importlib
    import types

    # Patch: load train.py without triggering data.py's broken imports
    # We only need Model (from train.py) and mmap_batch_generator (from data.py)
    # Load mmap_batch_generator by reading just that class from data.py
    import sys
    _data_path = os.path.join(os.path.dirname(os.path.dirname(
        importlib.util.find_spec("openwakeword").origin)), "openwakeword", "data.py")

    # Read only mmap_batch_generator class from source to avoid top-level imports
    import ast
    with open(_data_path) as f:
        source = f.read()
    tree = ast.parse(source)

    # Extract just the mmap_batch_generator class source
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "mmap_batch_generator":
            class_source = ast.get_source_segment(source, node)
            break

    # Execute it in a clean namespace with numpy
    _ns = {"np": np, "numpy": np}
    exec(class_source, _ns)
    mmap_batch_generator = _ns["mmap_batch_generator"]

    # Now load train.py with a mock openwakeword.data module
    mock_data = types.ModuleType("openwakeword.data")
    mock_data.mmap_batch_generator = mmap_batch_generator
    mock_data.generate_adversarial_texts = lambda *a, **k: []
    mock_data.augment_clips = lambda *a, **k: None
    sys.modules["openwakeword.data"] = mock_data

    from openwakeword.train import Model as OWWModel

    # Create batch generator, wrapped to convert numpy → torch tensors
    import torch

    raw_gen = mmap_batch_generator(
        data_files=data_files,
        n_per_class=n_per_class,
        label_transform_funcs=label_transform_funcs,
    )

    def tensor_gen():
        for x, y in raw_gen:
            # Labels come as strings ("0","1","2") from dict keys — convert to float
            # Map: "1" → 1.0 (positive), everything else → 0.0 (negative)
            y_float = np.array([1.0 if str(lbl) == "1" else 0.0 for lbl in y], dtype=np.float32)
            yield torch.from_numpy(x.copy()).float(), torch.from_numpy(y_float)

    batch_gen = tensor_gen()

    # Build negative weight schedule
    neg_weight_schedule = build_negative_weight_schedule(STEPS, max_weight=2000)

    # Create model
    model = OWWModel(
        n_classes=1,
        input_shape=(16, 96),
        model_type="dnn",
        layer_dim=LAYER_DIM,
        n_blocks=N_BLOCKS,
    )
    print(f"\nModel architecture:", flush=True)
    model.summary()

    # Build validation set: last 5% of positives + equal negatives (small to save RAM)
    pos_full = np.load(POSITIVE_FEATURES)
    val_split = int(0.95 * len(pos_full))
    val_pos = pos_full[val_split:]
    del pos_full  # Free RAM
    # Sample equal number of negatives from ACAV subsample
    acav_sub = np.load(acav_path, mmap_mode='r')
    val_neg = np.array(acav_sub[:len(val_pos)])

    val_x = np.concatenate([val_pos, val_neg], axis=0)
    val_y = np.concatenate([np.ones(len(val_pos)), np.zeros(len(val_neg))]).astype(np.float32)

    # Wrap as a single-batch iterator that yields torch tensors
    import torch as _torch
    val_data = [(_torch.from_numpy(val_x).float(), _torch.from_numpy(val_y).float())]
    print(f"\nValidation: {len(val_pos)} positive + {len(val_neg)} negative clips", flush=True)

    # Train
    print(f"\n=== Training ===", flush=True)
    print(f"Steps: {STEPS}, LR: {LR}, Layer: {LAYER_DIM}", flush=True)
    print(f"Negative weight: 1 → 50 over {STEPS} steps", flush=True)
    print(f"Batch: 64 positive + 128 negative per step", flush=True)
    print(flush=True)

    t0 = time.time()
    model.train_model(
        X=batch_gen,
        X_val=val_data,
        max_steps=STEPS,
        warmup_steps=WARMUP_STEPS,
        hold_steps=HOLD_STEPS,
        negative_weight_schedule=neg_weight_schedule,
        lr=LR,
    )
    elapsed = time.time() - t0
    print(f"\nTraining complete in {elapsed:.0f}s ({elapsed/60:.1f} min)", flush=True)

    # Export best model to ONNX
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    onnx_path = os.path.join(OUTPUT_DIR, "hey_poob.onnx")

    # Average the best models for more robust final model
    if model.best_models:
        print(f"\nAveraging {len(model.best_models)} best checkpoints...", flush=True)
        model.average_models(model.best_models)

    model.export_to_onnx(onnx_path, class_mapping="hey_poob")
    print(f"Model exported to: {onnx_path}", flush=True)

    # Copy to Windows
    win_path = "/mnt/c/Users/19203/Downloads/AgenticWebScraper/data/hey_poob.onnx"
    try:
        import shutil
        shutil.copy(onnx_path, win_path)
        print(f"Copied to Windows: {win_path}", flush=True)
    except Exception:
        print(f"Copy to Windows failed — run manually:", flush=True)
        print(f"  cp {onnx_path} {win_path}", flush=True)

    # Print training history summary
    if model.history:
        for key in ["val_accuracy", "val_recall", "val_fp"]:
            if key in model.history:
                vals = model.history[key]
                print(f"  {key}: best={max(vals) if 'fp' not in key else min(vals):.4f}", flush=True)

    print(f"\n=== DONE ===", flush=True)


if __name__ == "__main__":
    main()
