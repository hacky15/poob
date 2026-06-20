---
type: incident
status: resolved
date: 2026-06-20
tags: [voice, latency, cpu, homelab, infra, scanner]
related: [[patrol-backoff-during-voice]] [[voice-synth-ahead-pipeline]] [[voice-pipeline-cold-start-drops-requests]] [[homelab-cpu-only]]
---

# Voice delay / wake-word misses = CPU starvation on a shared 4-core host

## Symptom

Recurring across sessions: voice responses arrive late, the wake word seems
"inaccurate," and Poob "doesn't listen." Measured during a bad window:
host load **11.7**, poob at **229% CPU**, speech→wake **~4200 ms**.

## Root cause (layered)

The homelab is **4 cores, CPU-only, heavily multi-tenant**: poob (voice pipeline
**and** marketplace scanner in one process), two ollama containers, 6 GitHub
Actions runners (2 poob + 4 permitscope), Komodo (core/mongo/periphery), and the
permitscope stack (worker/dashboard/pipeline/postgres). Voice is a **real-time**
workload sharing the box with **batch** workloads.

Two distinct contention layers:

1. **In-process (dominant, measured):** poob's scanner patrol cycle (browser
   automation + VLM) spikes CPU and competed with poob's own voice pipeline.
   This was the bulk of the 229% and the wake-word misses — under starvation the
   STT *mishears*, which looked like OpenWakeWord degrading. It is not: the dual
   gate requires a text match, so OWW can't miss alone. No retrain needed.

2. **Cross-container (structural, latent):** `docker inspect` showed **poob has
   `NanoCpus=0, CpuShares=0` — zero CPU limit, zero reservation, zero priority.**
   It competes on equal footing with everything. So even with the scanner
   quiet, a **non-cooperative** co-tenant can starve voice:
   - a **deploy build** on `poob-gha-runner` (capped 2 of 4 cores) — every push
     builds an image; a VC at that moment stutters,
   - an **ollama fallback** (CPU-only, 50–120 s, can demand 3.5 of 4 cores),
   - a permitscope / mongod / dockerd spike.

## Fix (layered defense — cooperative + kernel-enforced)

| Layer | Mechanism | Commit / file |
|---|---|---|
| In-process | **patrol-backoff** — scanner skips cycles when voice was active in the last 120 s (voice-activity beacon) | `5b81fea` ([[patrol-backoff-during-voice]]) |
| In-process | synth-ahead pipeline, cold-start prewarm, Deepgram-leak fix, routing-schema trim (TPM headroom) | `96d2f23` / `d98075f` / `aa690c0` / `47c4eb8` |
| **Cross-container** | **`cpu_shares: 4096` on poob** — 4× default CFS weight; kernel hands contested cores to voice first, no idle waste. poob left uncapped (voice must burst). | `deploy/compose.yml` |
| Blast-radius | **`cpus: "2.0"` + `cpu_shares: 256` on poob-ollama** — a stray local-LLM call can't monopolize the box | `deploy/compose.yml` |

`cpu_shares` is the QoS-class pattern (cf. Kubernetes Guaranteed/Burstable,
systemd `CPUWeight`): priority **only under contention**, so idle capacity is not
wasted. The GHA runners' existing 2-core cap bounds the worst external
contender; poob's weight wins the rest.

## Why this is root-cause, not a bandaid

The patrol-backoff alone is *cooperative* — it only helps when the contender is
poob's own scanner. The `cpu_shares` weight is **kernel-enforced** and covers the
non-cooperative vectors (build, ollama, co-tenant) the backoff cannot. The two
layers together mean: when 4 cores are contested, the scheduler favors voice
regardless of *who* the other consumer is.

## Validation

- Patrol-backoff result (live): speech→wake **4200 ms → ~700–1500 ms**, load
  **11.7 → 0.87**.
- cgroup audit (2026-06-20): confirmed poob had no CPU priority; co-tenant caps
  documented above.
- After the compose change deploys, re-verify under a real load window
  (build + VC concurrently) that `docker stats` shows poob winning CPU and
  speech→wake stays sub-1.5 s.

## If it ever recurs (escalation tier)

If `cpu_shares` proves insufficient under a pathological load, escalate to
**`cpuset` core-pinning** (reserve cores 0–1 for poob, pin runners/ollama to
2–3) — a hard guarantee at the cost of idle capacity. Deferred unless needed;
the weight-based approach is the lower-risk first move on a 4-core box.
