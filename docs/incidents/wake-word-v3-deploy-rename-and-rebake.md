---
type: incident
status: resolved
date: 2026-05-20
tags: [voice, wake-word, docker, deploy, config]
related: [[wake-word-model-path-conventions]] [[wake-word-v3-shipped]] [[wake-word-retrain-v3]] [[docker-volume-shadows-baked-files]] [[c-drive-docker-data-vhd-trap-2026-05-14]]
---

# Wake-word v3 didn't go live after env-flip — three stacked bugs

## Symptom

After 8.5 h of training + a clean ONNX commit (`fa98619`) + a clean GHA build (run `26136775221`) + Komodo auto-redeploy + an operator env-flip of `WAKE_WORD_MODEL_PATH=/app/data/hey_poob_v3.onnx`, the live container's startup log still said:

```
Dual pipeline enabled (OpenWakeWord + Deepgram streaming)  wake_model=hey_poob.onnx
```

(the v1 model). Voice tests of the V2-failure patterns (`A Poob, skip` / `Pay Poob, what's up`) still missed.

## Root cause

Three independent bugs stacked behind a single "the env-flip didn't take" appearance:

1. **Wrong env-var name in the runbook.** [[wake-word-retrain-v3]] step 5 told the operator to set `WAKE_WORD_MODEL_PATH`. The bot's code read `config.porcupine_keyword_path`, which pydantic-settings binds to `PORCUPINE_KEYWORD_PATH` (legacy field name from Porcupine days). Flipping `WAKE_WORD_MODEL_PATH` set an env var nothing consumed.

2. **Wrong path even if the var had been right.** `/app/data/hey_poob_v3.onnx` lives under the `poob-data` named volume mounted at `/app/data` per `deploy/compose.yml:11`. The volume shadows any image-baked content at that path. This is the trap already documented in [[docker-volume-shadows-baked-files]]; the Dockerfile already worked around it correctly for v1 (`COPY data/hey_poob.onnx /app/hey_poob.onnx` outside the volume) — the v1 line had a comment explicitly calling out the trap.

3. **v3 ONNX wasn't in the image at all.** The Dockerfile only had the v1 COPY line. Nobody added a `COPY data/hey_poob_v3.onnx /app/...` line when shipping v3 in [[wake-word-v3-shipped]]. Even with bugs 1 and 2 fixed, the file `/app/hey_poob_v3.onnx` didn't exist anywhere in the running container.

A background verification agent surfaced (1) + (2) by SSH-probing the container's env + filesystem; reading the Dockerfile in this repo surfaced (3).

## Fix

Single commit, four changes:

1. **Dockerfile** — add `COPY data/hey_poob_v3.onnx /app/hey_poob_v3.onnx` next to the existing v1 COPY. Both models now baked at image root.
2. **`src/poob/config.py`** — rename field `porcupine_keyword_path` → `wake_word_model_path`. Use `pydantic.AliasChoices` so both `WAKE_WORD_MODEL_PATH` (new, authoritative) and `PORCUPINE_KEYWORD_PATH` (legacy) bind to the same field. Older Komodo stacks / .env files keep working through one rotation cycle.
3. **`src/poob/main.py` + `src/poob/voice/session.py` + `src/poob/voice/dual_pipeline.py`** — propagate the rename through the dict key and the `DualPipelineProcessor.__init__` kwarg. Dropped the vestigial `porcupine_access_key=""` arg (Porcupine was never actually called — OpenWakeWord is local).
4. **Tests + vault** — new `tests/unit/test_config.py::TestWakeWordModelPath` cases for the field default + new env-var + legacy alias + precedence; new gotcha [[wake-word-model-path-conventions]] codifying the `/app/*.onnx` (not `/app/data/`) rule; this incident note; runbook + ship decision note updated to use `WAKE_WORD_MODEL_PATH=/app/hey_poob_v3.onnx`.

## Validation

- ✅ Pydantic tests: all four new alias/path cases pass.
- ✅ Full unit suite: prior 1080 passing tests stay green (no regressions on the renamed field).
- ✅ Post-rebuild manual check (operator-side):
  - `docker exec poob ls /app/hey_poob_v3.onnx` returns the 857 KB file.
  - `docker exec poob env | grep WAKE_WORD_MODEL_PATH` returns `/app/hey_poob_v3.onnx`.
  - Bot startup log shows `wake_model=hey_poob_v3.onnx`.
  - Voice test of `A Poob, skip` and `Pay Poob, what's up` fires `wake_word=True`.

## Follow-ups

- Drop the `PORCUPINE_KEYWORD_PATH` alias from `config.py` once all live deployments have rotated to `WAKE_WORD_MODEL_PATH`. Target window: ~one month.
- Drop the now-vestigial `picovoice_access_key` config field in the same cleanup commit; OpenWakeWord is fully local and the key has been unused since the dual-pipeline migration.
- Future model versions (v4, v5, …) follow [[wake-word-model-path-conventions]]: bake at `/app/hey_poob_v<N>.onnx`, point env var at it, rollback by flipping the env var back to an earlier version.
