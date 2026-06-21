---
type: gotcha
status: active
date: 2026-06-21
tags: [voice, wake-word, openwakeword, concurrency, stt]
related: [[voice-pipeline-cold-start-drops-requests]] [[wake-word-dual-gate]] [[voice-architecture]]
---

# OpenWakeWord lazy init is not thread-safe — serialize it

## Hazard

Audio-frame callbacks run **one thread per user** (`realtime_sink.write` →
`voice_cog.on_audio_frame` → `dual_pipeline.process_audio_frame`). The wake
detector loads its model lazily on the first frame (`_ensure_model` →
`from openwakeword.model import Model` + `Model(...)`).

If two users' first frames hit `_ensure_model` **concurrently** before the model
is loaded, both threads enter `import openwakeword` at once. The package is then
"partially initialized" in one thread while the other dereferences it:

```
AttributeError: partially initialized module 'openwakeword' has no attribute
'get_pretrained_model_paths' (most likely due to a circular import)
```

`process_frame` calls `_ensure_model` **first**, so the throw aborts the frame
**before the Deepgram/STT feed** — the user goes unheard (wake-miss + lost
transcript). It is caught at `realtime_sink` (bot stays up) but recurs on every
audio frame until one thread wins the load. Surfaces on **every (re)start while
multiple users are in VC** — i.e. right after a deploy.

## Why prewarm alone doesn't fix it

The cold-start prewarm ([[voice-pipeline-cold-start-drops-requests]]) calls
`_ensure_model` off-thread on join, but it doesn't *serialize* — concurrent
joins/frames still race the import.

## Do this

Guard the lazy init with a **module-level lock + double-checked locking**, and
publish the fully-built model to `self._model` LAST so the fast path never sees
a half-constructed instance:

```python
_OWW_INIT_LOCK = threading.Lock()

def _ensure_model(self):
    if self._model is not None:        # fast path, no lock
        return
    with _OWW_INIT_LOCK:
        if self._model is not None:    # re-check after acquiring
            return
        from openwakeword.model import Model
        model = Model(...)             # build into a local first
        self._model = model            # publish last
```

The import happening inside the lock means only one thread ever imports
`openwakeword` — eliminating the partial-init race. Module-level (not instance)
lock so it holds across detector instances.

Regression test: `tests/unit/test_voice_prewarm.py::
test_ensure_model_is_thread_safe_single_construction` (8 threads, asserts the
model is constructed exactly once, no error).
