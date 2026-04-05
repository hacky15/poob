# Training "Hey Poob" Wake Word Model

## Step 1: Install WSL2 (Windows Terminal as Admin)

```powershell
# Run in PowerShell as Administrator
wsl --install
```

Reboot when prompted. After reboot, open "Ubuntu" from Start menu and create a user.

## Step 2: Verify GPU in WSL2

```bash
# Inside WSL2 Ubuntu terminal
nvidia-smi
```

You should see your RTX 2070 Super. If not, update your NVIDIA Windows driver to the latest version.

## Step 3: Set up training environment in WSL2

```bash
# Inside WSL2
sudo apt update && sudo apt install -y python3.10 python3.10-venv python3.10-dev git ffmpeg

# Clone OpenWakeWord
cd ~
git clone https://github.com/dscripka/openWakeWord.git
cd openWakeWord

# Create venv with Python 3.10 (required by the training pipeline)
python3.10 -m venv train_env
source train_env/bin/activate

# Install training dependencies
pip install -e ".[full]"
pip install torch==1.13.1+cu117 --extra-index-url https://download.pytorch.org/whl/cu117
pip install tensorflow==2.8.1
pip install piper-tts  # For synthetic data generation
```

## Step 4: Copy training data from Windows to WSL2

```bash
# Inside WSL2 — Windows drives are at /mnt/c/
mkdir -p ~/openwakeword_training/hey_poob
cp -r /mnt/c/Users/19203/Downloads/AgenticWebScraper/data/wake_word_training/positive ~/openwakeword_training/hey_poob/
cp -r /mnt/c/Users/19203/Downloads/AgenticWebScraper/data/wake_word_training/negative ~/openwakeword_training/hey_poob/
```

## Step 5: Download pre-computed negative features

```bash
# These are ~2000 hours of diverse audio pre-computed as embeddings
# Required by the training pipeline for background negative data
cd ~/openWakeWord
python -c "
from openwakeword.utils import download_models
download_models()
"

# Download ACAV100M features from HuggingFace (the big one)
pip install huggingface_hub
python -c "
from huggingface_hub import hf_hub_download
# Download pre-computed negative features
hf_hub_download(
    repo_id='davidscripka/openwakeword_features',
    filename='ACAV100M_sample_clips_features.npy',
    local_dir='./openwakeword/resources/models/',
)
print('Downloaded ACAV100M features')
"
```

## Step 6: Create training config YAML

```bash
cat > ~/openwakeword_training/hey_poob/config.yaml << 'EOF'
# Hey Poob — OpenWakeWord training config
target_phrase: "hey poob"

# TTS generation (supplements our Edge TTS samples)
n_samples: 15000
n_samples_val: 2000

# Architecture
n_blocks: 1
layer_dim: 128

# Training
steps: 50000
learning_rate: 0.0001
max_negative_weight: 2000
target_false_positives_per_hour: 0.1
target_recall: 0.5
batch_n_per_class:
  ACAV100M_sample: 1024
  adversarial_negative: 75
  positive: 75

# Adversarial negatives — phonetically similar words
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
  - "hey noon"
  - "hey Bruce"
  - "hey Luke"
  - "hey you"
  - "hey Google"
  - "hey Siri"

# Pre-existing positive samples (our Edge TTS data)
custom_positive_audio_dir: ~/openwakeword_training/hey_poob/positive

# Output
output_dir: ~/openwakeword_training/hey_poob/output
EOF
```

## Step 7: Run training

```bash
cd ~/openWakeWord

# Activate env
source train_env/bin/activate

# Run the automated training pipeline
python -m openwakeword.train \
    --config ~/openwakeword_training/hey_poob/config.yaml \
    --output_dir ~/openwakeword_training/hey_poob/output

# If the above doesn't work, use the notebook approach:
# jupyter notebook notebooks/automatic_model_training.ipynb
```

Training takes ~4-6 hours on the 2070 Super.

## Step 8: Copy trained model back to Windows

```bash
# Find the output .onnx file
ls ~/openwakeword_training/hey_poob/output/*.onnx

# Copy to Windows project
cp ~/openwakeword_training/hey_poob/output/hey_poob.onnx \
   /mnt/c/Users/19203/Downloads/AgenticWebScraper/data/hey_poob.onnx
```

## Step 9: Deploy in the bot

Update `.env`:
```
PORCUPINE_KEYWORD_PATH=data/hey_poob.onnx
```

The dual pipeline in `dual_pipeline.py` will automatically load the custom model
instead of the pre-trained "hey_jarvis".
