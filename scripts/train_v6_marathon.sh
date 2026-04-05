#!/bin/bash
# ============================================================================
# Hey Poob — Marathon Training (v6)
#
# Designed to run ALL DAY. Uses controlled negative weights (no auto-ramping)
# and 500K+ training steps across multiple sequences with decreasing LR.
#
# Key difference from v5: DOES NOT use auto_train's aggressive negative
# weight doubling (1500 → 3000 → 6000) which destroys recall for made-up words.
# Instead uses a fixed moderate weight with long, stable training.
#
# Runtime: ~4-6 hours on RTX 2070 Super
# ============================================================================
set -e

cd ~/openWakeWord
source train_env/bin/activate

V5_DIR=~/wake_word_training/hey_poob_v5
V6_DIR=~/wake_word_training/hey_poob_v6
ACAV_PATH=~/wake_word_training/acav100m/acav_sub_50000.npy

echo "============================================"
echo "  Hey Poob Marathon Training — v6"
echo "============================================"

# ------------------------------------------------------------------
# Check that v5 features exist (reuse them — no need to regenerate)
# ------------------------------------------------------------------
echo ""
echo "=== Checking v5 features ==="
for f in positive_features_train.npy negative_features_train.npy positive_features_test.npy negative_features_test.npy; do
    if [ ! -f "$V5_DIR/hey_poob/$f" ]; then
        echo "ERROR: $V5_DIR/hey_poob/$f not found. Run v5 training first."
        exit 1
    fi
    SIZE=$(ls -lh "$V5_DIR/hey_poob/$f" | awk '{print $5}')
    echo "  $f: $SIZE"
done

if [ ! -f "$V5_DIR/validation_set_features.npy" ]; then
    echo "ERROR: validation features not found"
    exit 1
fi
echo "  validation_set_features.npy: OK"
echo "  All features present — skipping generation + augmentation"

# ------------------------------------------------------------------
# Create v6 output directory
# ------------------------------------------------------------------
mkdir -p $V6_DIR

# ------------------------------------------------------------------
# Run marathon training with controlled weights
# ------------------------------------------------------------------
echo ""
echo "=== Starting marathon training ==="
echo "  This will run for several hours. Don't touch the window."
echo ""

python3 -c "
import sys, os, copy, logging, time
import numpy as np
import torch
from torch.utils.data import DataLoader, TensorDataset

sys.path.insert(0, 'piper-sample-generator')
# Patch imports
import types
mock_data = types.ModuleType('openwakeword.data')
mock_data.mmap_batch_generator = None
mock_data.generate_adversarial_texts = lambda *a, **k: []
mock_data.augment_clips = lambda *a, **k: None
sys.modules['openwakeword.data'] = mock_data
from openwakeword.train import Model

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

# Load features from v5
logging.info('Loading features from v5...')
pos_train = np.load('$V5_DIR/hey_poob/positive_features_train.npy')
neg_train = np.load('$V5_DIR/hey_poob/negative_features_train.npy')
pos_test = np.load('$V5_DIR/hey_poob/positive_features_test.npy')
neg_test = np.load('$V5_DIR/hey_poob/negative_features_test.npy')
val_fp = np.load('$V5_DIR/validation_set_features.npy')

# Also load ACAV
acav = np.load('$ACAV_PATH', mmap_mode='r')
logging.info(f'Positive train: {pos_train.shape}')
logging.info(f'Negative train: {neg_train.shape}')
logging.info(f'Positive test: {pos_test.shape}')
logging.info(f'Negative test: {neg_test.shape}')
logging.info(f'ACAV background: {acav.shape}')
logging.info(f'Validation FP: {val_fp.shape}')

input_shape = pos_train.shape[1:]  # (16, 96)

# Build validation data
val_x = np.concatenate([pos_test, neg_test])
val_y = np.concatenate([np.ones(len(pos_test)), np.zeros(len(neg_test))]).astype(np.float32)
X_val = DataLoader(TensorDataset(torch.from_numpy(val_x), torch.from_numpy(val_y)), batch_size=len(val_y))

# Build FP validation data
val_fp_reshaped = np.array([val_fp[i:i+input_shape[0]] for i in range(0, val_fp.shape[0]-input_shape[0], 1)])
val_fp_labels = np.zeros(val_fp_reshaped.shape[0]).astype(np.float32)
X_val_fp = DataLoader(TensorDataset(torch.from_numpy(val_fp_reshaped), torch.from_numpy(val_fp_labels)), batch_size=len(val_fp_labels))

# Build training batch generator
# Mix: positive + adversarial negative + ACAV background
acav_np = np.array(acav[:200000])  # Use 200K ACAV samples
logging.info(f'Using {len(acav_np)} ACAV samples for training')

def make_batch_gen(pos, neg, acav_data, pos_per_batch=64, neg_per_batch=64, acav_per_batch=512):
    pos_idx = 0
    neg_idx = 0
    acav_idx = 0
    while True:
        # Sample positives
        if pos_idx + pos_per_batch > len(pos):
            pos_idx = 0
        p = pos[pos_idx:pos_idx+pos_per_batch]
        pos_idx += pos_per_batch

        # Sample adversarial negatives
        if neg_idx + neg_per_batch > len(neg):
            neg_idx = 0
        n = neg[neg_idx:neg_idx+neg_per_batch]
        neg_idx += neg_per_batch

        # Sample ACAV background
        if acav_idx + acav_per_batch > len(acav_data):
            acav_idx = 0
        a = acav_data[acav_idx:acav_idx+acav_per_batch]
        acav_idx += acav_per_batch

        x = np.concatenate([p, n, a])
        y = np.concatenate([
            np.ones(len(p)),
            np.zeros(len(n)),
            np.zeros(len(a)),
        ]).astype(np.float32)

        yield torch.from_numpy(x.copy()).float(), torch.from_numpy(y)

batch_gen = make_batch_gen(pos_train, neg_train, acav_np)

# Create model
model = Model(n_classes=1, input_shape=input_shape, model_type='dnn', layer_dim=32, n_blocks=1)
model.summary()

# ------------------------------------------------------------------
# Marathon training: 3 sequences with FIXED moderate negative weights
# No auto-doubling. Gradual weight increase with decreasing LR.
# ------------------------------------------------------------------
t0 = time.time()
val_set_hrs = 11.3

sequences = [
    # Sequence 1: Learn the basics (500K steps, low neg weight, high LR)
    {'steps': 500000, 'max_neg_weight': 100, 'lr': 0.0001, 'name': 'Foundation'},
    # Sequence 2: Refine (100K steps, moderate neg weight, lower LR)
    {'steps': 100000, 'max_neg_weight': 300, 'lr': 0.00001, 'name': 'Refinement'},
    # Sequence 3: Polish (50K steps, higher neg weight for FP reduction, lowest LR)
    {'steps': 50000, 'max_neg_weight': 500, 'lr': 0.000001, 'name': 'Polish'},
]

for seq_idx, seq in enumerate(sequences):
    logging.info(f'')
    logging.info(f'===== Sequence {seq_idx+1}/3: {seq[\"name\"]} =====')
    logging.info(f'Steps: {seq[\"steps\"]}, Max neg weight: {seq[\"max_neg_weight\"]}, LR: {seq[\"lr\"]}')

    steps = seq['steps']
    weights = np.linspace(1, seq['max_neg_weight'], steps).tolist()
    val_steps = np.linspace(max(1, steps - int(steps*0.25)), steps, 20).astype(np.int64)

    model.train_model(
        X=batch_gen,
        X_val=X_val,
        false_positive_val_data=X_val_fp,
        max_steps=steps,
        negative_weight_schedule=weights,
        val_steps=val_steps,
        warmup_steps=steps//5,
        hold_steps=steps//3,
        lr=seq['lr'],
        val_set_hrs=val_set_hrs,
    )

    elapsed = time.time() - t0
    logging.info(f'Sequence {seq_idx+1} done ({elapsed/60:.0f} min total)')
    logging.info(f'  Best recall: {model.best_val_recall:.4f}')
    logging.info(f'  Best accuracy: {model.best_val_accuracy:.4f}')
    logging.info(f'  Best FP: {model.best_val_fp}')

# Merge best models
logging.info('Merging best checkpoints...')
if model.best_models:
    accuracy_pct = np.percentile(model.history['val_accuracy'], 90)
    recall_pct = np.percentile(model.history['val_recall'], 90)
    fp_pct = np.percentile(model.history.get('val_fp_per_hr', [1000]), 10)

    models = []
    for m, score in zip(model.best_models, model.best_model_scores):
        if score['val_accuracy'] >= accuracy_pct and score['val_recall'] >= recall_pct:
            models.append(m)

    if models:
        combined = model.average_models(models=models)
        logging.info(f'Averaged {len(models)} top checkpoints')
    else:
        combined = model.model
        logging.info('No models met percentile threshold, using last best')
else:
    combined = model.model
    logging.info('No checkpoints saved, using final model')

# Evaluate combined model
with torch.no_grad():
    for batch in X_val:
        x, y = batch[0].to(model.device), batch[1].to(model.device)
        val_ps = combined(x)

    final_recall = model.recall(val_ps, y[..., None]).detach().cpu().numpy()
    final_accuracy = model.accuracy(val_ps, y[..., None].to(torch.int64)).detach().cpu().numpy()

    final_fp = 0
    for batch in X_val_fp:
        x_val, y_val = batch[0].to(model.device), batch[1].to(model.device)
        val_ps = combined(x_val)
        final_fp += model.fp(val_ps, y_val[..., None])
    final_fp_per_hr = (final_fp / val_set_hrs).detach().cpu().numpy()

logging.info(f'')
logging.info(f'################')
logging.info(f'Final Model Accuracy: {final_accuracy}')
logging.info(f'Final Model Recall: {final_recall}')
logging.info(f'Final Model FP/hr: {final_fp_per_hr}')
logging.info(f'################')

# Export
os.makedirs('$V6_DIR', exist_ok=True)
onnx_path = '$V6_DIR/hey_poob.onnx'
model.model = combined
model.export_to_onnx(onnx_path, class_mapping='hey_poob')
logging.info(f'Model saved to {onnx_path}')

# Copy to Windows
try:
    import shutil
    shutil.copy(onnx_path, '/mnt/c/Users/19203/Downloads/AgenticWebScraper/data/hey_poob.onnx')
    logging.info('Copied to Windows: data/hey_poob.onnx')
except:
    logging.info(f'Copy failed — run: cp {onnx_path} /mnt/c/Users/19203/Downloads/AgenticWebScraper/data/hey_poob.onnx')

elapsed = time.time() - t0
logging.info(f'Total training time: {elapsed/60:.0f} min ({elapsed/3600:.1f} hr)')
"

echo ""
echo "============================================"
echo "  Marathon training complete!"
echo "============================================"
echo "Press Enter to close..."
read
