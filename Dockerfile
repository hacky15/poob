FROM python:3.12-slim

WORKDIR /app

# System dependencies:
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

# Copy package metadata + source together — setuptools package discovery
# reads `[tool.setuptools.packages.find] where = ["src"]` at install time,
# so src/ must exist before `pip install -e .`.
COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir -e . && \
    playwright install --with-deps chromium

COPY scripts ./scripts

RUN mkdir -p /app/data /app/browser_profiles

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "poob.main"]
