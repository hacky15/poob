#!/usr/bin/env bash
# Host-side wrapper for running the wake-word v3 training in Docker.
#
# Builds the trainer image (cached after first build), mounts the
# corpus drive at /work in the container, and runs train_inside_container.py.
# Logs stream to host stdout so the operator can watch progress.
#
# Usage (from repo root, in Git Bash on Windows):
#
#   # Build the image (one-time, ~5 min for first build):
#   bash scripts/wake_word_v3/train_in_docker.sh build
#
#   # Run the full training (extracts features + trains + exports ONNX):
#   bash scripts/wake_word_v3/train_in_docker.sh train
#
#   # Run a feature-extraction smoke test (cheap, fails early if any
#   # corpus dir has a broken WAV):
#   bash scripts/wake_word_v3/train_in_docker.sh smoke
#
#   # Drop into a bash shell inside the container for debugging:
#   bash scripts/wake_word_v3/train_in_docker.sh shell
#
# Corpus drive is hard-coded to E:\wake_word_v3\corpus per the
# wake-word-retrain-v3 runbook. Override via CORPUS_HOST env var if
# the operator runs from a different machine.

set -euo pipefail

IMAGE_TAG="${IMAGE_TAG:-poob-wake-trainer:latest}"
CORPUS_HOST="${CORPUS_HOST:-F:/wake_word_v3}"
DOCKERFILE="scripts/wake_word_v3/Dockerfile.training"

# Docker Desktop on Windows accepts Windows-style absolute paths
# (e.g. ``F:/wake_word_v3``) directly in -v. The previous ``/f/...``
# unix-style form does NOT work — Docker silently mounts an empty
# directory instead of the intended host path. Normalize: backslashes
# to forward slashes, leave the drive letter alone.
mount_path="${CORPUS_HOST//\\//}"  # backslashes -> forward slashes only

# Project root for the read-only /scripts mount. Use ``cygpath -m``
# (Git Bash) to convert ``/c/Users/.../AgenticWebScraper`` to
# ``C:/Users/.../AgenticWebScraper`` so Docker recognizes it.
if command -v cygpath >/dev/null 2>&1; then
    repo_host="$(cygpath -m "$(pwd)")"
else
    repo_host="$(pwd)"
fi
scripts_host="${repo_host}/scripts/wake_word_v3"

action="${1:-train}"

cmd_build() {
    echo "==> Building $IMAGE_TAG from $DOCKERFILE"
    docker build -t "$IMAGE_TAG" -f "$DOCKERFILE" .
    echo "==> Build complete."
}

cmd_run() {
    local container_cmd="$1"
    echo "==> Mounting host '$CORPUS_HOST' -> container '/work'"
    echo "==> Mounting host '$scripts_host' -> container '/scripts' (ro)"
    docker run --rm -t \
        --name poob-wake-train \
        --gpus all \
        -v "${mount_path}:/work" \
        -v "${scripts_host}:/scripts:ro" \
        -e PYTHONPATH=/scripts \
        "$IMAGE_TAG" \
        "$container_cmd"
}

case "$action" in
    build)
        cmd_build
        ;;
    train)
        cmd_run "python /scripts/train_inside_container.py"
        ;;
    smoke)
        # Cheap smoke test — feature extraction only, no training.
        # Bails out before run_training so we can verify the corpus
        # loads cleanly without committing to the multi-hour run.
        cmd_run "python -c 'import sys; sys.path.insert(0, \"/scripts\"); from train_inside_container import extract_features, POS_DIRS, NEG_DIRS; extract_features(\"positive\", POS_DIRS); extract_features(\"negative\", NEG_DIRS); print(\"smoke OK\")'"
        ;;
    shell)
        cmd_run "/bin/bash"
        ;;
    *)
        echo "usage: $0 {build|train|smoke|shell}" >&2
        exit 2
        ;;
esac
