---
type: gotcha
status: active
date: 2026-05-20
tags: [voice, wake-word, docker, deploy, infra]
related: [[docker-volume-shadows-baked-files]] [[wake-word-v3-shipped]] [[wake-word-retrain-v3]] [[wake-word-v3-deploy-rename-and-rebake]]
---

# Wake-word ONNX models live at `/app/*.onnx`, NEVER under `/app/data/`

## Trigger

Adding a new wake-word model file to the image (e.g. shipping `data/hey_poob_v4.onnx` for the next retrain) and pointing the env var at `/app/data/<name>.onnx`. The model file is in the repo, the Dockerfile copies the whole repo, and on dev-laptop testing it works — then production silently keeps loading the old model and you can't figure out why.

Also triggers when renaming the env var: the runbook says `WAKE_WORD_MODEL_PATH`, somebody types `PORCUPINE_KEYWORD_PATH` from muscle memory (or vice-versa), and the env-flip has no observable effect.

## Why it happens

Two stacked traps:

**1. Volume shadow.** `deploy/compose.yml` mounts the named volume `poob-data` onto the container's `/app/data` path so the bot's runtime state (`scraper.db`, `quota_state.db`, `cache/`, `logs/`, `voice_fillers/`) survives container restarts. Docker's mount semantics: when a volume mounts onto a directory that already exists in the image, the volume **replaces** the directory contents. Any file baked at `/app/data/*` becomes invisible at runtime — the volume wins. This is the same trap documented in [[docker-volume-shadows-baked-files]] for other image-vs-volume content collisions. The model file disappears even though `docker exec poob ls /app/data` looks fine — you're just seeing the volume's contents, not the image's.

**2. Env-var name confusion.** Until 2026-05-20, the config field was named `porcupine_keyword_path` (legacy from when we were on Picovoice Porcupine), which pydantic-settings maps to `PORCUPINE_KEYWORD_PATH`. The wake-word v3 runbook called it `WAKE_WORD_MODEL_PATH` because that's what the field *should* be called now that we're on OpenWakeWord. Result: an operator flipping `WAKE_WORD_MODEL_PATH` on Komodo achieved literally nothing — the bot kept reading the old `PORCUPINE_KEYWORD_PATH` value (or its default empty string). Fixed in [[wake-word-v3-deploy-rename-and-rebake]] by renaming the field and adding an `AliasChoices` so both names work.

## Don't

- Don't `COPY data/hey_poob_v4.onnx /app/data/hey_poob_v4.onnx` in the Dockerfile. The volume swallows it.
- Don't set `WAKE_WORD_MODEL_PATH=/app/data/<anything>` in any environment. The path is shadowed.
- Don't `docker cp` the model into the running container's `/app/data` as a "fix" — it works for the current container but vanishes the next time the volume is created from scratch (compose `down -v`, fresh deploy on a new host, etc.). Bandaid.
- Don't add a new env-var name for the model path. Use `WAKE_WORD_MODEL_PATH` and keep extending the `AliasChoices` if you must.

## Do

Three rules:

**1. Bake the model at the image root.** Two coupled edits:

  a. In the `Dockerfile`, alongside the existing v1 line, add:

  ```dockerfile
  COPY data/hey_poob_<version>.onnx /app/hey_poob_<version>.onnx
  ```

  b. In `.dockerignore`, add the matching allowlist entry so the build context actually contains the file:

  ```
  data/*
  !data/hey_poob.onnx
  !data/hey_poob_v3.onnx
  !data/hey_poob_<version>.onnx
  ```

  Path `/app/hey_poob_*.onnx` is outside any volume mount, so the file is present at runtime exactly as baked. Skipping the `.dockerignore` entry causes `docker/build-push-action` to fail with `"failed to calculate checksum of ref ...: '/data/hey_poob_<version>.onnx': not found"` even though the file is in the repo. The exclusion comes from line 14 of `.dockerignore` (`data/*`); the new model needs a matching `!` line.

**2. Point the env var at the image-root path.** In Komodo's Environment panel for the poob stack:

```
WAKE_WORD_MODEL_PATH=/app/hey_poob_v3.onnx
```

(swap the version when bumping). Save → auto-redeploy. The bot's startup log line `Dual pipeline enabled ... wake_model=hey_poob_v3.onnx` is the ground-truth confirmation.

**3. Rollback is a single Komodo env-var flip.** Change `WAKE_WORD_MODEL_PATH` back to `/app/hey_poob.onnx` (or any older version that's still baked in the image). No code change, no container rebuild. Old models stay in the image specifically so this is one click.

## Reference

- Incident that surfaced the rename half of this: [[wake-word-v3-deploy-rename-and-rebake]] (2026-05-20).
- The underlying mount mechanism: [[docker-volume-shadows-baked-files]].
- Dockerfile is [Dockerfile](../../Dockerfile); the `COPY data/hey_poob*.onnx /app/...` lines live in step 6.
- Volume definition is in [deploy/compose.yml](../../deploy/compose.yml) (`poob-data:/app/data`).
- Env-var binding is in [src/poob/config.py](../../src/poob/config.py) on the `wake_word_model_path` field; the `AliasChoices` keeps the legacy `PORCUPINE_KEYWORD_PATH` working for one rotation cycle.
