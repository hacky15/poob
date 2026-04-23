---
type: gotcha
status: active
date: 2026-03-15
tags: [deploy, docker, komodo]
related: [[deploy-flow]]
---

# Docker named-volume mount shadows baked image files

## Trigger

Adding a file to a Dockerfile via `COPY` at a path that also has a named-volume mount in `deploy/compose.yml`. The file is present when the image is built, but at runtime the volume mount overlays the directory with an empty volume.

## Why it happens

`/app/data` is a named volume in `deploy/compose.yml`. Named volumes shadow whatever was at the mount point in the image — so files baked into `/app/data/...` at build time are invisible at runtime. The first bite was `hey_poob.onnx` at `/app/data/hey_poob.onnx`: openwakeword's `Model()` failed with `Could not find pretrained model for 'data/hey_poob.onnx'` because the volume had overlaid the baked file with nothing.

## Don't

- `COPY hey_poob.onnx /app/data/` — gets shadowed.
- Assume a volume path also contains baked files; it never does at runtime.

## Do

- `COPY hey_poob.onnx /app/hey_poob.onnx` — outside the volume mount.
- Set the absolute path in env: `PORCUPINE_KEYWORD_PATH=/app/hey_poob.onnx` in the Komodo stack env.
- Local dev keeps `PORCUPINE_KEYWORD_PATH=data/hey_poob.onnx` because no volume mount is in play.

## Reference

- openwakeword's preprocessor models (`melspectrogram.onnx`, `embedding_model.onnx`) don't ship with the pip package — they're downloaded on first use. The Dockerfile pre-fetches at build via `python -c "import openwakeword.utils; openwakeword.utils.download_models([])"` so the container can start offline. Passing `[]` skips the pretrained hot-word models we don't use.
- Any future file we want shipped in the image must live OUTSIDE `/app/data`, `/app/browser_profiles`, or any other path that gets a volume in compose.
