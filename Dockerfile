FROM python:3.11-slim

WORKDIR /app

# Layer ordering is tuned for build-cache stability:
#   1. apt deps          — changes rarely, only on system-dep additions
#   2. python deps       — changes only when pyproject.toml deps change
#   3. playwright chrome — stable binary, only changes on playwright version bump
#   4. openwakeword      — stable models, ~140 MB download
#   5. local src         — volatile, changes every commit
#   6. scripts + onnx    — small tail
#
# The previous single-RUN layer pulled src before pip install, which meant
# every source-code edit invalidated the whole install layer and forced
# re-download of Chrome + openwakeword models on every build (~6 min of
# avoidable work per commit).

# 1. System deps (rarely change):
# - ffmpeg: yt-dlp music playback + audio processing
# - libsodium23: PyNaCl runtime (Discord voice encryption)
# - build-essential + libffi-dev: compile native wheels (PyNaCl, onnxruntime, etc.)
# - curl/wget/gnupg/ca-certificates: general scraping + Playwright installer
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    gnupg \
    ca-certificates \
    ffmpeg \
    libsodium23 \
    build-essential \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# 2. Python deps from pyproject.toml ONLY. Writing deps to a requirements
# file first is more robust than `pip install $(...)` — it handles version
# specifiers with shell metacharacters and environment markers cleanly.
COPY pyproject.toml ./
RUN python -c "import tomllib; \
deps = tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']; \
open('/tmp/requirements.txt', 'w').write('\n'.join(deps))" \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# 3. Playwright Chromium (stable between commits; invalidates only on
# playwright version bump in pyproject.toml, which busts layer 2 anyway).
RUN playwright install --with-deps chromium

# 4. openwakeword preprocessor models. Passing `[]` skips the pretrained
# hot-word models we don't use (alexa/hey_jarvis/etc.) — saves ~50 MB vs
# the default download, and the layer caches so subsequent builds skip.
RUN python -c "import openwakeword.utils; openwakeword.utils.download_models([])"

# 4b. Kokoro-82M ONNX (~330 MB model + ~24 MB voices). Pre-download so
# first runtime invocation doesn't block on a 350 MB fetch. Soft-fails
# if kokoro_onnx isn't importable (e.g. dep dropped mid-rebuild) — the
# image still ships; KokoroTTS.is_available() will just return False
# and the cascade falls through to the cloud providers. See
# docs/decisions/voice-latency-phase3-kokoro.md.
RUN python -c "from kokoro_onnx import Kokoro; Kokoro()" \
    || echo "warn: kokoro_onnx pre-download skipped; cascade will use cloud TTS"

# 4c. Model2Vec static embedding (potion-base-8M, ~30 MB). Pre-download so
# the MultiSignalAddressDetector's semantic-relevance signal loads from
# cache on first voice utterance instead of fetching at runtime. Soft-fails
# so the image still ships if the dep is absent — the detector degrades to
# its other signals (wake-word, conversation-state) gracefully. See
# docs/incidents/text-mode-rlhf-refusal-leak-2026-05-29.md.
RUN python -c "from model2vec import StaticModel; StaticModel.from_pretrained('minishlab/potion-base-8M')" \
    || echo "warn: model2vec pre-download skipped; semantic addressee scoring disabled"

# 5. Local package (volatile — every commit invalidates from here down).
# setuptools' package-find reads `[tool.setuptools.packages.find] where=["src"]`
# at install time, so src/ must exist before `pip install -e .`. The
# `--no-deps` flag is critical: deps are already installed in layer 2, and
# re-resolving them here would slow the hot path to no benefit.
COPY src ./src
RUN pip install --no-cache-dir -e . --no-deps

# 6. Scripts + runtime dirs + wake-word models (tiny, fast).
COPY scripts ./scripts
RUN mkdir -p /app/data /app/browser_profiles
# Wake-word ONNX models live at /app/<name>.onnx — OUTSIDE /app/data —
# because /app/data is a volume mount at runtime; any file baked into
# that path is hidden by the empty volume on first start.
# See docs/gotchas/wake-word-model-path-conventions.md.
COPY data/hey_poob.onnx /app/hey_poob.onnx
COPY data/hey_poob_v3.onnx /app/hey_poob_v3.onnx

ENV PYTHONUNBUFFERED=1

# Injected by GitHub Actions at build time; visible to runtime via config.git_sha.
ARG GIT_SHA=dev
ENV GIT_SHA=$GIT_SHA

CMD ["python", "-m", "poob.main"]
