---
type: runbook
status: active
date: 2026-05-13
tags: [voice, wake-word, training, openwakeword, operations]
related: [[wake-word-mass-augmentation-v3]] [[wake-word-augmentation-2026]] [[wake-word-dual-gate]] [[wake-fp-pcm-capture]] [[docker-desktop-data-vhd-separate-from-engine]] [[c-drive-docker-data-vhd-trap-2026-05-14]]
---

# Retraining the Hey-Poob wake word — v3 pipeline (operator runbook)

End-to-end procedure for generating the v3 corpus and producing a new `data/hey_poob_v3.onnx` model. See [[wake-word-mass-augmentation-v3]] for the design rationale and tradeoffs.

## Pre-flight

Disk: plan for **400-500 GB free** on a dedicated drive — the dev machine for the 2026-05-13 retrain used `F:\wake_word_v3\` (637 GB free) for the corpus and `F:\Docker\` for the Docker Desktop disk image, leaving C:\ untouched. The full corpus at default settings produces ~11.85M WAVs ≈ ~380 GB raw. Tunable down via `--pitch-fraction` and `--augment-fraction`.

**Critical**: Docker Desktop on Windows uses **two** VHDs and you must relocate **both** before the first `docker build`. See [[docker-desktop-data-vhd-separate-from-engine]] for the full recipe.

1. Engine distro (`docker-desktop` WSL distro, ~160 MB): move via `wsl --export | --unregister | --import --vhd` to `F:\wsl\docker-desktop\`.
2. Data VHD (`%LOCALAPPDATA%\Docker\wsl\disk\docker_data.vhdx`, grows to many GB): the Docker Desktop GUI's "Disk image location" setting (`Settings → Resources → Advanced`) is supposed to handle this, but **the option is missing on Windows 10 WSL2 backend**. Use the directory-junction trick — `Move-Item` the VHD to `F:\Docker\wsl\disk\` and `mklink /J` the original path. Skipping this step is what caused [[c-drive-docker-data-vhd-trap-2026-05-14]].

Dependencies:
- `pip install edge-tts` (Windows-side; the orchestrator runs on the dev machine over the network to Edge TTS).
- `ffmpeg` on PATH (already required for the bot itself).
- For training only: WSL2 + the existing `openwakeword/train_env/` virtualenv per `scripts/train_hey_poob_v2.py`.

## Step 1 — Generate the corpus (Windows dev machine, ~12-18h)

From repo root:

```powershell
# Show the plan first — no generation yet
python -m scripts.wake_word_v3 --plan

# Dry-run smoke test (~3 minutes, ~5,000 samples). Verifies edge_tts
# auth + ffmpeg + filesystem permissions.
python -m scripts.wake_word_v3 --max-base 1000 --pitch-fraction 0.1 --augment-fraction 0.1

# Full run. Resumable — Ctrl-C and re-run anytime; deterministic
# filenames mean it picks up where it left off.
python -m scripts.wake_word_v3 --concurrency 8

# Lighter run if disk is tight (~3M samples, ~100 GB).
python -m scripts.wake_word_v3 --pitch-fraction 0.15 --augment-fraction 0.4

# Tune concurrency UP if Edge TTS isn't rate-limiting you (try 12 / 16).
python -m scripts.wake_word_v3 --concurrency 16

# Disable a phase entirely if you only want partial regeneration.
python -m scripts.wake_word_v3 --no-pitch
python -m scripts.wake_word_v3 --no-augment
```

Phases run in order:

1. **Positive TTS** (~169k WAVs at full / ~8-12 hours at 8x concurrency) — 449 phrases × 47 voices × 8 rates.
2. **Negative TTS** (~38k WAVs / ~2-3 hours) — 101 adversarial phrases × 47 × 8.
3. **Pitch / speed perturbation** (~250k WAVs / ~30-60 min) — 25% of base positives × 6 presets.
4. **Clip augmentation** (~11M+ WAVs / ~2-3 hours) — 27 variants per base/pitched WAV.

Progress lines print every 250 generated samples. Phase summaries print on completion.

Output lands under `F:\wake_word_v3\/`:

```
pos/             base positive WAVs                ~169k
neg/             adversarial negative WAVs         ~38k
pos_pitched/     pitch/speed-perturbed positives   ~250k
pos_augmented/   clip-augmented (base + pitched)   ~11M
```

## Step 2 — Move corpus into the WSL2 training env

```powershell
# Robocopy to WSL2 home (much faster than cp for millions of small files).
$wsl = wsl wslpath -a -u "$($PWD.Path)\data\wake_word_training_v3"
wsl bash -c "mkdir -p ~/wake_word_training/hey_poob_v3 && \
  rsync -a /mnt/c/Users/19203/Downloads/AgenticWebScraper/F:\wake_word_v3\/ \
  ~/wake_word_training/hey_poob_v3/"
```

If the dataset is huge enough that WSL2 round-trip is painful, skip the copy entirely and have openWakeWord's feature extractor read from `/mnt/c/...` directly. Slower disk I/O but no double-storage.

## Step 3 — Extract features (WSL2, ~8-12h on CPU)

```bash
cd ~/openWakeWord
source train_env/bin/activate

# Replace v2 paths with v3 paths in compute_features_from_clips invocations.
# The v3 dir layout matches the v2 layout (pos/, neg/) so most of
# scripts/train_hey_poob_v2.py works as-is by pointing the feature
# extractor at:
#   ~/wake_word_training/hey_poob_v3/pos/
#   ~/wake_word_training/hey_poob_v3/pos_pitched/
#   ~/wake_word_training/hey_poob_v3/pos_augmented/
#   ~/wake_word_training/hey_poob_v3/neg/

# Combine pos + pos_pitched + pos_augmented into ONE positive_features.npy
# (openWakeWord's training expects a single .npy for positives + one for
# negatives + uses ACAV100M from a separate directory).

python -c "
from openwakeword.data import compute_features_from_clips
import numpy as np
import os

root = os.path.expanduser('~/wake_word_training/hey_poob_v3')
all_pos = []
for sub in ('pos', 'pos_pitched', 'pos_augmented'):
    d = os.path.join(root, sub)
    if not os.path.isdir(d):
        continue
    feats = compute_features_from_clips(d, batch_size=64)
    all_pos.append(feats)
    print(f'  {sub}: {feats.shape}')
positive_features = np.concatenate(all_pos, axis=0)
print('total positives:', positive_features.shape)
np.save(os.path.join(root, 'features', 'positive_features.npy'), positive_features)

negative_features = compute_features_from_clips(
    os.path.join(root, 'neg'), batch_size=64,
)
print('total adversarial negatives:', negative_features.shape)
np.save(os.path.join(root, 'features', 'negative_features.npy'), negative_features)
"
```

## Step 4 — Train (WSL2, ~24-72h on CPU; 6-10h on a single GPU)

```bash
cd /mnt/c/Users/19203/Downloads/AgenticWebScraper
# Edit scripts/train_hey_poob_v2.py POSITIVE_FEATURES / NEGATIVE_FEATURES
# paths to point at the v3 .npy files, then:
python scripts/train_hey_poob_v2.py
```

The existing training script (`train_hey_poob_v2.py`) uses ACAV100M (~2000 hrs) as the bulk negative pool, ramped negative-weight scheduling, hard example mining, and cosine LR with warmup. The v3 corpus replaces only the positive + adversarial-negative .npy paths; the rest of the training recipe is untouched.

Output: `~/wake_word_training/hey_poob/output_v2/*.onnx`. Pick the highest-recall variant from the validation log; copy to `data/hey_poob_v3.onnx` in the repo.

## Step 5 — Deploy (Komodo, ~5 min)

The bot looks up the wake model via env var. Drop the new onnx in `data/hey_poob_v3.onnx`, commit it, push. Then in the Komodo `poob` stack Environment panel:

```
WAKE_WORD_MODEL_PATH=/app/hey_poob_v3.onnx
```

Save → auto-redeploy. The bot reboots with the new model. The startup DM will show the new sha.

## Step 6 — Validate live

In voice channel:

```
Hey Poob, skip            → expect: addressed (was already working)
A Poob, skip              → expect: addressed (NEW — was failing 2026-05-13)
Pay Poob, skip            → expect: addressed (NEW)
Poob, skip                → expect: addressed (NEW)
Hey Noob, skip            → expect: addressed (NEW — phonetic-neighbor as positive)
Yo Poob, what's up        → expect: addressed (NEW)
```

Log signals to watch:

```bash
# Old failure mode should be gone:
ssh ben@homelab "docker logs poob --since 30m 2>&1 | grep 'Audio wake word overridden by text'"

# New true-fires:
ssh ben@homelab "docker logs poob --since 30m 2>&1 | grep 'Wake word detected'"

# Look for any FP regressions:
ssh ben@homelab "docker logs poob --since 24h 2>&1 | grep 'wake_word=True' | grep -ivE 'poob|noob|tube|poop|boob'"
```

If FP rate looks unhealthy after a session, fork to the two-model ensemble approach (see [[wake-word-mass-augmentation-v3]] § "Why phonetic neighbors as POSITIVES").

## Rollback

In Komodo: change `WAKE_WORD_MODEL_PATH` back to `/app/hey_poob.onnx`. Save → redeploy. v2 model is back live. No code change needed. Older models stay baked into the image for exactly this purpose; see [[wake-word-model-path-conventions]] for why all wake-word ONNX files live at the image root rather than `/app/data/`.

## Resumability

The generation pipeline is resumable end-to-end. Killing the process mid-run (Ctrl-C, machine reboot, lost SSH) leaves on-disk WAVs intact; re-running the orchestrator skips every file that already exists. Use `--count` to inspect on-disk sizes at any point:

```powershell
python -m scripts.wake_word_v3 --count
```

If a phase failed mid-way, look for stale 0-byte files (rare; ffmpeg cleanup usually catches them):

```powershell
Get-ChildItem F:\wake_word_v3\ -Recurse -Include *.wav |
  Where-Object { $_.Length -eq 0 } |
  Remove-Item
```

## Tuning the generation footprint

- **Disk-tight** (< 100 GB free): `--pitch-fraction 0.1 --augment-fraction 0.3`. ~1.7M samples, ~55 GB. Still 10× the v2 corpus.
- **Time-tight** (smoke test): `--max-base 5000 --pitch-fraction 0.2 --augment-fraction 0.2`. ~30k samples, runs in ~15 min.
- **All-in** (default): ~11.85M samples, ~380 GB. The user's "days" intent.

## Related

- [[wake-word-mass-augmentation-v3]] — design + decision record
- [[wake-word-augmentation-2026]] — research foundation
- [[wake-word-dual-gate]] — gate that complements the model in production
- [[wake-fp-pcm-capture]] — captured FP audio for the held-out test set
