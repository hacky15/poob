#!/bin/bash
# Run this INSIDE WSL2 Ubuntu terminal
# Sets up OpenWakeWord training environment with GPU support

set -e

echo "=== Step 1: Install system dependencies ==="
sudo apt update
sudo apt install -y python3.10 python3.10-venv python3.10-dev git ffmpeg espeak-ng

echo "=== Step 2: Clone OpenWakeWord ==="
cd ~
if [ ! -d "openWakeWord" ]; then
    git clone https://github.com/dscripka/openWakeWord.git
fi
cd openWakeWord

echo "=== Step 3: Create Python 3.10 venv ==="
python3.10 -m venv train_env
source train_env/bin/activate

echo "=== Step 4: Install PyTorch with CUDA ==="
pip install --upgrade pip
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121

echo "=== Step 5: Install OpenWakeWord training deps ==="
pip install -e .
pip install tensorflow
pip install mutagen scipy tqdm pyyaml datasets huggingface_hub
pip install pronouncing  # for phonetic similarity

echo "=== Step 6: Verify GPU access from Python ==="
python -c "
import torch
print(f'PyTorch: {torch.__version__}')
print(f'CUDA available: {torch.cuda.is_available()}')
if torch.cuda.is_available():
    print(f'GPU: {torch.cuda.get_device_name(0)}')
    print(f'VRAM: {torch.cuda.get_device_properties(0).total_mem / 1e9:.1f} GB')
"

echo "=== Step 7: Copy training data ==="
mkdir -p ~/wake_word_training/hey_poob
cp -r /mnt/c/Users/19203/Downloads/AgenticWebScraper/data/wake_word_training/positive ~/wake_word_training/hey_poob/
cp -r /mnt/c/Users/19203/Downloads/AgenticWebScraper/data/wake_word_training/negative ~/wake_word_training/hey_poob/

POS_COUNT=$(ls ~/wake_word_training/hey_poob/positive/*.wav 2>/dev/null | wc -l)
NEG_COUNT=$(ls ~/wake_word_training/hey_poob/negative/*.wav 2>/dev/null | wc -l)
echo "Copied $POS_COUNT positive, $NEG_COUNT negative samples"

echo ""
echo "=== SETUP COMPLETE ==="
echo "Next: run the training script"
echo "  cd ~/openWakeWord && source train_env/bin/activate"
echo "  python /mnt/c/Users/19203/Downloads/AgenticWebScraper/scripts/train_hey_poob.py"
