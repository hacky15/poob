#!/bin/bash
# Run this INSIDE WSL2 Ubuntu (open Ubuntu from Start menu)
# Training takes ~10 minutes on RTX 2070 Super
cd ~/openWakeWord
source train_env/bin/activate
echo "Starting training..."
python /mnt/c/Users/19203/Downloads/AgenticWebScraper/scripts/train_hey_poob_v2.py
echo ""
echo "=== Training finished! ==="
echo "Press Enter to close..."
read
