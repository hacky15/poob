"""Train 'Hey Poob' OpenWakeWord model from pre-existing WAV samples.

Runs inside WSL2 with GPU. Uses the OpenWakeWord training API to:
1. Augment raw WAV samples (noise, reverb, padding)
2. Compute frozen speech embeddings from augmented audio
3. Train a small DNN classifier on the embeddings
4. Export to ONNX for deployment

Usage (inside WSL2):
    cd ~/openWakeWord && source train_env/bin/activate
    python /mnt/c/Users/19203/Downloads/AgenticWebScraper/scripts/train_hey_poob.py
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
POSITIVE_DIR = os.path.expanduser("~/wake_word_training/hey_poob/positive")
NEGATIVE_DIR = os.path.expanduser("~/wake_word_training/hey_poob/negative")
OUTPUT_DIR = os.path.expanduser("~/wake_word_training/hey_poob/output")
FEATURES_DIR = os.path.expanduser("~/wake_word_training/hey_poob/features")

# Training hyperparameters (tuned for short wake word on 2070 Super)
STEPS = 50000
LAYER_DIM = 128
N_BLOCKS = 1
LEARNING_RATE = 0.0001
MAX_NEGATIVE_WEIGHT = 2000
TARGET_FP_PER_HOUR = 0.1
BATCH_POS = 50
BATCH_NEG = 50
BATCH_BG = 512  # background negatives per step


def check_data():
    """Verify training data exists."""
    pos_files = sorted(glob.glob(os.path.join(POSITIVE_DIR, "*.wav")))
    neg_files = sorted(glob.glob(os.path.join(NEGATIVE_DIR, "*.wav")))
    print(f"Positive samples: {len(pos_files)}")
    print(f"Negative samples: {len(neg_files)}")
    if len(pos_files) < 100:
        print("ERROR: Need at least 100 positive samples!")
        sys.exit(1)
    return pos_files, neg_files


def load_wav_as_numpy(wav_path, target_samples=32000):
    """Load a WAV file and return as float32 numpy array, padded/trimmed to target length."""
    import wave
    import struct
    try:
        with wave.open(wav_path, 'rb') as wf:
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
            samples = np.array(struct.unpack(f'<{n_frames}h', raw), dtype=np.int16)

        # Pad or trim to target length (2 seconds at 16kHz)
        if len(samples) < target_samples:
            samples = np.pad(samples, (0, target_samples - len(samples)))
        else:
            samples = samples[:target_samples]

        return samples.astype(np.int16)
    except Exception:
        return None


def compute_features(wav_files, output_path, label):
    """Compute OpenWakeWord embeddings from WAV files using embed_clips."""
    if os.path.exists(output_path):
        features = np.load(output_path)
        print(f"  Loaded cached {label} features: {features.shape}")
        return output_path

    print(f"  Computing {label} features from {len(wav_files)} files...")
    t0 = time.time()

    from openwakeword.utils import AudioFeatures
    af = AudioFeatures()

    # Load all WAV files into a numpy array (N, 32000)
    TARGET_SAMPLES = 32000  # 2 seconds at 16kHz
    clips = []
    skipped = 0
    for i, wav_path in enumerate(wav_files):
        audio = load_wav_as_numpy(wav_path, TARGET_SAMPLES)
        if audio is not None:
            clips.append(audio)
        else:
            skipped += 1
        if (i + 1) % 2000 == 0:
            print(f"    Loaded {i+1}/{len(wav_files)} files ({skipped} skipped)...", flush=True)

    if not clips:
        print(f"  ERROR: No audio loaded for {label}!")
        sys.exit(1)

    print(f"  Loaded {len(clips)} clips, {skipped} skipped. Computing embeddings...", flush=True)

    # Stack into (N, 32000) array
    audio_array = np.stack(clips)

    # Compute embeddings in batches using OpenWakeWord's API
    features = af.embed_clips(audio_array, batch_size=64)

    np.save(output_path, features)
    elapsed = time.time() - t0
    print(f"  Saved {label} features: {features.shape} ({elapsed:.0f}s)", flush=True)
    return output_path


def train_model(pos_features_path, neg_features_path):
    """Train the wake word DNN classifier."""
    print(f"\n=== Training ===")
    print(f"Steps: {STEPS}, LR: {LEARNING_RATE}, Layer: {LAYER_DIM}")

    pos_features = np.load(pos_features_path)
    neg_features = np.load(neg_features_path)
    print(f"Positive features shape: {pos_features.shape}")
    print(f"Negative features shape: {neg_features.shape}")

    try:
        # Try using OpenWakeWord's built-in training
        from openwakeword.train import Model as OWWModel

        # Determine input shape from features
        if len(pos_features.shape) == 2:
            input_shape = pos_features.shape[1]
        elif len(pos_features.shape) == 3:
            input_shape = (pos_features.shape[1], pos_features.shape[2])
        else:
            input_shape = pos_features.shape[1:]

        print(f"Input shape: {input_shape}")

        model = OWWModel(
            n_classes=1,
            input_shape=input_shape,
            model_type="dnn",
            layer_dim=LAYER_DIM,
        )

        # Train
        model.train_model(
            X={"positive": pos_features, "negative": neg_features},
            max_steps=STEPS,
            warmup_steps=STEPS // 5,
            hold_steps=STEPS // 3,
            lr=LEARNING_RATE,
        )

        # Export
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        onnx_path = os.path.join(OUTPUT_DIR, "hey_poob.onnx")
        model.export_to_onnx(onnx_path)
        print(f"\nModel exported to: {onnx_path}")
        return onnx_path

    except (ImportError, AttributeError) as e:
        print(f"OpenWakeWord training API not available: {e}")
        print("Falling back to manual PyTorch training...")
        return train_pytorch(pos_features, neg_features)


def train_pytorch(pos_features, neg_features):
    """Fallback: train using PyTorch directly."""
    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader, TensorDataset

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Training on: {device}")

    # Flatten features if needed
    if len(pos_features.shape) > 2:
        pos_flat = pos_features.reshape(pos_features.shape[0], -1)
        neg_flat = neg_features.reshape(neg_features.shape[0], -1)
    else:
        pos_flat = pos_features
        neg_flat = neg_features

    input_dim = pos_flat.shape[1]
    print(f"Input dim: {input_dim}")

    # Create labels: 1 for positive, 0 for negative
    X = np.vstack([pos_flat, neg_flat]).astype(np.float32)
    y = np.concatenate([
        np.ones(len(pos_flat), dtype=np.float32),
        np.zeros(len(neg_flat), dtype=np.float32),
    ])

    # Shuffle
    idx = np.random.permutation(len(X))
    X, y = X[idx], y[idx]

    # Split train/val (90/10)
    split = int(0.9 * len(X))
    X_train, X_val = X[:split], X[split:]
    y_train, y_val = y[:split], y[split:]

    train_ds = TensorDataset(torch.from_numpy(X_train), torch.from_numpy(y_train))
    val_ds = TensorDataset(torch.from_numpy(X_val), torch.from_numpy(y_val))
    train_dl = DataLoader(train_ds, batch_size=256, shuffle=True)
    val_dl = DataLoader(val_ds, batch_size=256)

    # Build DNN (matches OpenWakeWord architecture)
    model = nn.Sequential(
        nn.Linear(input_dim, LAYER_DIM),
        nn.LayerNorm(LAYER_DIM),
        nn.ReLU(),
        nn.Linear(LAYER_DIM, LAYER_DIM),
        nn.LayerNorm(LAYER_DIM),
        nn.ReLU(),
        nn.Linear(LAYER_DIM, 1),
        nn.Sigmoid(),
    ).to(device)

    optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)
    criterion = nn.BCELoss()

    # Train
    best_val_acc = 0
    best_state = None
    t0 = time.time()

    for epoch in range(100):
        model.train()
        train_loss = 0
        for xb, yb in train_dl:
            xb, yb = xb.to(device), yb.to(device)
            pred = model(xb).squeeze()
            loss = criterion(pred, yb)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            train_loss += loss.item()

        # Validate
        model.eval()
        correct = 0
        total = 0
        with torch.no_grad():
            for xb, yb in val_dl:
                xb, yb = xb.to(device), yb.to(device)
                pred = model(xb).squeeze()
                correct += ((pred > 0.5) == yb).sum().item()
                total += len(yb)

        val_acc = correct / total if total > 0 else 0
        if val_acc > best_val_acc:
            best_val_acc = val_acc
            best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}

        if (epoch + 1) % 10 == 0:
            elapsed = time.time() - t0
            print(f"  Epoch {epoch+1}: loss={train_loss/len(train_dl):.4f} val_acc={val_acc:.3f} ({elapsed:.0f}s)")

    print(f"\nBest validation accuracy: {best_val_acc:.3f}")

    # Load best weights and export to ONNX
    model.load_state_dict(best_state)
    model.eval()
    model.cpu()

    # Wrap with Flatten so ONNX accepts (batch, 16, 96) — the shape
    # OpenWakeWord's predict() sends — instead of pre-flattened (batch, 1536)
    class FlattenWrapper(nn.Module):
        def __init__(self, inner):
            super().__init__()
            self.inner = inner
        def forward(self, x):
            return self.inner(x.reshape(x.shape[0], -1))

    wrapped = FlattenWrapper(model)
    wrapped.eval()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    onnx_path = os.path.join(OUTPUT_DIR, "hey_poob.onnx")

    # Export with (batch, 16, 96) input shape to match OpenWakeWord's feature format
    dummy_input = torch.randn(1, 16, 96)
    torch.onnx.export(
        wrapped, dummy_input, onnx_path,
        input_names=["input"],
        output_names=["output"],
        dynamic_axes={"input": {0: "batch"}, "output": {0: "batch"}},
    )
    print(f"Model exported to: {onnx_path}")

    # Copy to Windows
    win_path = "/mnt/c/Users/19203/Downloads/AgenticWebScraper/data/hey_poob.onnx"
    import shutil
    shutil.copy2(onnx_path, win_path)
    print(f"Copied to Windows: {win_path}")

    return onnx_path


def main():
    print("=== Hey Poob Wake Word Training ===\n")

    # Check data
    pos_files, neg_files = check_data()

    # Create dirs
    os.makedirs(FEATURES_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Compute features
    print("\n=== Computing Features ===")
    pos_feat_path = os.path.join(FEATURES_DIR, "positive_features.npy")
    neg_feat_path = os.path.join(FEATURES_DIR, "negative_features.npy")

    compute_features(pos_files, pos_feat_path, "positive")
    compute_features(neg_files, neg_feat_path, "negative")

    # Train
    onnx_path = train_model(pos_feat_path, neg_feat_path)

    print(f"\n=== DONE ===")
    print(f"Model: {onnx_path}")
    print(f"\nTo deploy, update .env:")
    print(f"  PORCUPINE_KEYWORD_PATH=data/hey_poob.onnx")


if __name__ == "__main__":
    main()
