#!/bin/bash
# ============================================================================
# Hey Poob — Official OpenWakeWord Training Pipeline (v5)
#
# This script is IDEMPOTENT — safe to re-run. It skips completed steps.
# Total runtime: ~2-3 hours on RTX 2070 Super
#
# Steps:
#   1. Copy Edge TTS samples into OWW directory structure (once)
#   2. Generate Piper TTS samples (positive + adversarial negative)
#   3. Augment all clips (reverb + noise + pitch shift)
#   4. Compute OpenWakeWord embeddings
#   5. Train model with ACAV100M + augmented features
#   6. Export ONNX and copy to Windows
# ============================================================================
set -e

cd ~/openWakeWord
source train_env/bin/activate

MODEL_DIR=~/wake_word_training/hey_poob_v5
EDGE_POS=~/wake_word_training/hey_poob/positive
EDGE_NEG=~/wake_word_training/hey_poob/negative
ACAV_PATH=~/wake_word_training/acav100m/acav_sub_50000.npy

echo "============================================"
echo "  Hey Poob Wake Word Training — v5"
echo "============================================"
echo ""

# ------------------------------------------------------------------
# Step 0: Verify prerequisites
# ------------------------------------------------------------------
echo "=== Checking prerequisites ==="

if [ ! -f "$ACAV_PATH" ]; then
    echo "ERROR: ACAV subsample not found at $ACAV_PATH"
    echo "Run train_hey_poob_v2.py first to create it."
    exit 1
fi
echo "  ACAV features: OK"

if [ ! -d "$MODEL_DIR/mit_rirs" ] || [ $(find $MODEL_DIR/mit_rirs -name "*.wav" -maxdepth 1 2>/dev/null | wc -l) -lt 100 ]; then
    echo "  RIRs: downloading..."
    python3 -c "
import os
from huggingface_hub import hf_hub_download, list_repo_files
output_dir = '$MODEL_DIR/mit_rirs'
os.makedirs(output_dir, exist_ok=True)
files = list_repo_files('davidscripka/MIT_environmental_impulse_responses', repo_type='dataset')
wav_files = [f for f in files if f.endswith('.wav')]
print(f'  Downloading {len(wav_files)} RIR files...')
for wf in wav_files:
    hf_hub_download('davidscripka/MIT_environmental_impulse_responses', wf, repo_type='dataset', local_dir=output_dir)
import shutil, glob
subdir = os.path.join(output_dir, '16khz')
if os.path.isdir(subdir):
    for f in glob.glob(os.path.join(subdir, '*.wav')):
        dest = os.path.join(output_dir, os.path.basename(f))
        if not os.path.exists(dest): shutil.move(f, dest)
    shutil.rmtree(subdir, ignore_errors=True)
for d in [os.path.join(output_dir, '.cache'), os.path.join(output_dir, '.huggingface')]:
    if os.path.isdir(d): shutil.rmtree(d, ignore_errors=True)
print(f'  Done: {len([f for f in os.listdir(output_dir) if f.endswith(\".wav\")])} RIR files')
"
else
    echo "  RIRs: $(find $MODEL_DIR/mit_rirs -name '*.wav' -maxdepth 1 | wc -l) files OK"
fi

if [ ! -f "$MODEL_DIR/validation_set_features.npy" ]; then
    echo "  Validation features: downloading..."
    python3 -c "
from huggingface_hub import hf_hub_download
hf_hub_download(repo_id='davidscripka/openwakeword_features', filename='validation_set_features.npy', repo_type='dataset', local_dir='$MODEL_DIR')
print('  Done')
"
else
    echo "  Validation features: OK"
fi

# ------------------------------------------------------------------
# Step 1: Copy Edge TTS samples (once only)
# ------------------------------------------------------------------
echo ""
echo "=== Copying Edge TTS samples ==="

EDGE_MARKER="$MODEL_DIR/.edge_tts_copied"
if [ ! -f "$EDGE_MARKER" ]; then
    mkdir -p $MODEL_DIR/hey_poob/positive_train
    mkdir -p $MODEL_DIR/hey_poob/positive_test
    mkdir -p $MODEL_DIR/hey_poob/negative_train
    mkdir -p $MODEL_DIR/hey_poob/negative_test

    # Copy Edge TTS positives (90% train, 10% test)
    if [ -d "$EDGE_POS" ]; then
        EDGE_COUNT=$(find $EDGE_POS -name "*.wav" | wc -l)
        echo "  Copying $EDGE_COUNT Edge TTS positive samples..."
        find $EDGE_POS -name "*.wav" | sort | head -n $((EDGE_COUNT * 9 / 10)) | while read f; do
            cp "$f" $MODEL_DIR/hey_poob/positive_train/
        done
        find $EDGE_POS -name "*.wav" | sort | tail -n $((EDGE_COUNT / 10)) | while read f; do
            cp "$f" $MODEL_DIR/hey_poob/positive_test/
        done
    fi

    # Copy Edge TTS negatives (90% train, 10% test)
    if [ -d "$EDGE_NEG" ]; then
        NEG_COUNT=$(find $EDGE_NEG -name "*.wav" | wc -l)
        echo "  Copying $NEG_COUNT Edge TTS negative samples..."
        find $EDGE_NEG -name "*.wav" | sort | head -n $((NEG_COUNT * 9 / 10)) | while read f; do
            cp "$f" $MODEL_DIR/hey_poob/negative_train/
        done
        find $EDGE_NEG -name "*.wav" | sort | tail -n $((NEG_COUNT / 10)) | while read f; do
            cp "$f" $MODEL_DIR/hey_poob/negative_test/
        done
    fi

    touch "$EDGE_MARKER"
    echo "  Done"
else
    echo "  Already copied (skipping)"
fi

echo "  positive_train: $(find $MODEL_DIR/hey_poob/positive_train -name '*.wav' 2>/dev/null | wc -l)"
echo "  positive_test:  $(find $MODEL_DIR/hey_poob/positive_test -name '*.wav' 2>/dev/null | wc -l)"
echo "  negative_train: $(find $MODEL_DIR/hey_poob/negative_train -name '*.wav' 2>/dev/null | wc -l)"
echo "  negative_test:  $(find $MODEL_DIR/hey_poob/negative_test -name '*.wav' 2>/dev/null | wc -l)"

# ------------------------------------------------------------------
# Step 2: Create YAML config
# ------------------------------------------------------------------
echo ""
echo "=== Creating training config ==="
cat > $MODEL_DIR/hey_poob.yaml << 'YAMLEOF'
model_name: "hey_poob"

target_phrase:
  - "hey poob"
  - "heypoob"
  - "ey poob"
  - "poob"

custom_negative_phrases:
  - "hey tube"
  - "hey dude"
  - "hey boo"
  - "hey boob"
  - "hey poop"
  - "hey pool"
  - "hey cool"
  - "hey fool"
  - "hey boot"
  - "hey loop"
  - "hey boom"
  - "hey boop"
  - "hey food"
  - "hey mood"
  - "hey room"
  - "hey zoom"
  - "hey spoon"
  - "hey moon"
  - "hey you"
  - "hey Google"
  - "hey Siri"
  - "hey Alexa"
  - "pooch"
  - "poof"
  - "proof"
  - "drool"
  - "scoop"

n_samples: 50000
n_samples_val: 5000
tts_batch_size: 50
augmentation_batch_size: 16
YAMLEOF

# Append paths (need variable expansion, can't use heredoc with single quotes)
cat >> $MODEL_DIR/hey_poob.yaml << EOF
piper_sample_generator_path: "./piper-sample-generator"
output_dir: "$MODEL_DIR"

rir_paths:
  - "$MODEL_DIR/mit_rirs"

background_paths: []
background_paths_duplication_rate:
  - 1

false_positive_validation_data_path: "$MODEL_DIR/validation_set_features.npy"
augmentation_rounds: 2

feature_data_files:
  "ACAV100M_sample": "$ACAV_PATH"

batch_n_per_class:
  "ACAV100M_sample": 1024
  "adversarial_negative": 50
  "positive": 50

model_type: "dnn"
layer_size: 32

steps: 100000
max_negative_weight: 1500
target_false_positives_per_hour: 0.2
EOF

echo "  Config written to $MODEL_DIR/hey_poob.yaml"

# ------------------------------------------------------------------
# Step 3: Generate + Augment + Train (official OWW pipeline)
# ------------------------------------------------------------------
echo ""
echo "=== Step 3a: Generating Piper TTS clips ==="
echo "  (50K positive + 50K adversarial negative, ~30 min on GPU)"
python openwakeword/train.py --training_config $MODEL_DIR/hey_poob.yaml --generate_clips

echo ""
echo "=== Step 3b: Augmenting clips + computing features ==="
echo "  (Reverb + noise + pitch shift, then embeddings, ~30 min)"
python openwakeword/train.py --training_config $MODEL_DIR/hey_poob.yaml --augment_clips --overwrite

echo ""
echo "=== Step 3c: Training model ==="
echo "  (100K steps with ACAV100M negatives, ~60 min)"
python openwakeword/train.py --training_config $MODEL_DIR/hey_poob.yaml --train_model

# ------------------------------------------------------------------
# Step 4: Deploy
# ------------------------------------------------------------------
echo ""
echo "=== Deploying model ==="
ONNX_FILE="$MODEL_DIR/hey_poob.onnx"
if [ -f "$ONNX_FILE" ]; then
    cp "$ONNX_FILE" /mnt/c/Users/19203/Downloads/AgenticWebScraper/data/hey_poob.onnx
    echo "  Copied to Windows: data/hey_poob.onnx"
    echo "  Size: $(ls -lh $ONNX_FILE | awk '{print $5}')"
else
    echo "  ERROR: No .onnx model found at $ONNX_FILE"
fi

echo ""
echo "============================================"
echo "  Training complete!"
echo "============================================"
echo "Press Enter to close..."
read
