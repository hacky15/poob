#!/bin/bash
# Official OpenWakeWord training pipeline for "Hey Poob"
# Run inside WSL2 Ubuntu
set -e

cd ~/openWakeWord
source train_env/bin/activate

MODEL_DIR=~/wake_word_training/hey_poob_v3
POSITIVE_SRC=~/wake_word_training/hey_poob/positive
NEGATIVE_SRC=~/wake_word_training/hey_poob/negative

echo "=== Setting up directory structure ==="
mkdir -p $MODEL_DIR/hey_poob/positive_train
mkdir -p $MODEL_DIR/hey_poob/positive_test
mkdir -p $MODEL_DIR/hey_poob/negative_train
mkdir -p $MODEL_DIR/hey_poob/negative_test

# Split positives: 90% train, 10% test
echo "Splitting positive samples..."
POS_FILES=($(ls $POSITIVE_SRC/*.wav | shuf))
TOTAL=${#POS_FILES[@]}
TRAIN_COUNT=$((TOTAL * 9 / 10))
for i in "${!POS_FILES[@]}"; do
    if [ $i -lt $TRAIN_COUNT ]; then
        cp "${POS_FILES[$i]}" $MODEL_DIR/hey_poob/positive_train/
    else
        cp "${POS_FILES[$i]}" $MODEL_DIR/hey_poob/positive_test/
    fi
done
echo "  Train: $(ls $MODEL_DIR/hey_poob/positive_train/*.wav | wc -l)"
echo "  Test: $(ls $MODEL_DIR/hey_poob/positive_test/*.wav | wc -l)"

# Split negatives: 90% train, 10% test
echo "Splitting negative samples..."
NEG_FILES=($(ls $NEGATIVE_SRC/*.wav | shuf))
TOTAL_NEG=${#NEG_FILES[@]}
TRAIN_NEG=$((TOTAL_NEG * 9 / 10))
for i in "${!NEG_FILES[@]}"; do
    if [ $i -lt $TRAIN_NEG ]; then
        cp "${NEG_FILES[$i]}" $MODEL_DIR/hey_poob/negative_train/
    else
        cp "${NEG_FILES[$i]}" $MODEL_DIR/hey_poob/negative_test/
    fi
done
echo "  Train: $(ls $MODEL_DIR/hey_poob/negative_train/*.wav | wc -l)"
echo "  Test: $(ls $MODEL_DIR/hey_poob/negative_test/*.wav | wc -l)"

# Download Room Impulse Responses (if not already downloaded)
echo ""
echo "=== Downloading Room Impulse Responses ==="
if [ ! -d "$MODEL_DIR/mit_rirs" ] || [ $(ls $MODEL_DIR/mit_rirs/*.wav 2>/dev/null | wc -l) -lt 10 ]; then
    python3 -c "
import os
from huggingface_hub import hf_hub_download, list_repo_files

output_dir = '$MODEL_DIR/mit_rirs'
os.makedirs(output_dir, exist_ok=True)

# RIRs are stored as individual WAV files under 16khz/ in the dataset repo
files = list_repo_files('davidscripka/MIT_environmental_impulse_responses', repo_type='dataset')
wav_files = [f for f in files if f.endswith('.wav')]
print(f'Found {len(wav_files)} WAV files to download')

for i, wf in enumerate(wav_files):
    local = hf_hub_download(
        'davidscripka/MIT_environmental_impulse_responses', wf,
        repo_type='dataset', local_dir=output_dir,
    )
    if (i+1) % 50 == 0:
        print(f'  Downloaded {i+1}/{len(wav_files)}...')

# HuggingFace downloads to output_dir/16khz/ — move to flat structure
# and remove the subdirectory so torchaudio doesn't try to open it as a file
import shutil, glob
subdir = os.path.join(output_dir, '16khz')
if os.path.isdir(subdir):
    for f in glob.glob(os.path.join(subdir, '*.wav')):
        dest = os.path.join(output_dir, os.path.basename(f))
        if not os.path.exists(dest):
            shutil.move(f, dest)
    # Remove empty subdirectory and .huggingface cache
    shutil.rmtree(subdir, ignore_errors=True)
    hf_cache = os.path.join(output_dir, '.huggingface')
    if os.path.isdir(hf_cache):
        shutil.rmtree(hf_cache, ignore_errors=True)

count = len([f for f in os.listdir(output_dir) if f.endswith('.wav')])
print(f'Done: {count} RIR files')
"
else
    echo "  Already downloaded: $(ls $MODEL_DIR/mit_rirs/*.wav | wc -l) RIR files"
fi

# Download validation features
echo ""
echo "=== Downloading validation features ==="
if [ ! -f "$MODEL_DIR/validation_set_features.npy" ]; then
    python3 -c "
from huggingface_hub import hf_hub_download
hf_hub_download(
    repo_id='davidscripka/openwakeword_features',
    filename='validation_set_features.npy',
    repo_type='dataset',
    local_dir='$MODEL_DIR',
)
print('Downloaded validation features')
"
else
    echo "  Already downloaded"
fi

# Use existing ACAV subsample
ACAV_PATH=~/wake_word_training/acav100m/acav_sub_50000.npy
if [ ! -f "$ACAV_PATH" ]; then
    echo "ERROR: ACAV subsample not found. Run train_hey_poob_v2.py first to create it."
    exit 1
fi
echo "ACAV features: $ACAV_PATH"

# Create YAML config
echo ""
echo "=== Creating training config ==="
cat > $MODEL_DIR/hey_poob.yaml << EOF
model_name: "hey_poob"
target_phrase:
  - "hey poob"

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
  - "hey you"
  - "hey Google"
  - "hey Siri"

n_samples: 50000
n_samples_val: 5000
tts_batch_size: 50
augmentation_batch_size: 16
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

echo "Config written to $MODEL_DIR/hey_poob.yaml"

# Install piper-sample-generator (required by train.py even if we skip --generate_clips)
echo ""
echo "=== Installing piper-sample-generator ==="
if [ ! -f "./piper-sample-generator/generate_samples.py" ]; then
    rm -rf ./piper-sample-generator
    git clone https://github.com/dscripka/piper-sample-generator
    wget -O piper-sample-generator/models/en_US-libritts_r-medium.pt 'https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt'
    pip install piper-phonemize webrtcvad 2>/dev/null || true
fi

# Step 0: Generate additional synthetic clips via Piper TTS
# Uses LibriTTS with 100+ speaker voices — far more diverse than Edge TTS
# Generates both positive ("hey poob") and adversarial negative clips
echo ""
echo "=== Step 0: Generating synthetic clips via Piper TTS ==="
python openwakeword/train.py --training_config $MODEL_DIR/hey_poob.yaml --generate_clips

# Step 1: Augment ALL clips and compute features
# Mixes raw WAV files with room impulse responses + background noise
echo ""
echo "=== Step 1: Augmenting clips + computing features ==="
python openwakeword/train.py --training_config $MODEL_DIR/hey_poob.yaml --augment_clips --overwrite

# Step 2: Train model
echo ""
echo "=== Step 2: Training model ==="
python openwakeword/train.py --training_config $MODEL_DIR/hey_poob.yaml --train_model

# Copy model to Windows
echo ""
echo "=== Copying model ==="
ONNX=$(find $MODEL_DIR -name "*.onnx" | head -1)
if [ -n "$ONNX" ]; then
    cp "$ONNX" /mnt/c/Users/19203/Downloads/AgenticWebScraper/data/hey_poob.onnx
    echo "Model copied to Windows: data/hey_poob.onnx"
    echo "Size: $(ls -lh /mnt/c/Users/19203/Downloads/AgenticWebScraper/data/hey_poob.onnx | awk '{print $5}')"
else
    echo "ERROR: No .onnx model found"
fi

echo ""
echo "=== DONE ==="
echo "Press Enter to close..."
read
