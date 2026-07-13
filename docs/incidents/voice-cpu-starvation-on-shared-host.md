---
type: incident
status: resolved
date: 2026-06-20
tags: [voice, latency, cpu, homelab, infra, scanner, cpuset]
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

## 2026-07-07 recurrence — escalation shipped

**Symptom:** repeated `WARNING [discord.gateway] Shard ID None has stopped
responding to the gateway` + voice WS `code=1006` closures + forced voice
reconnects, 3 occurrences within one hour window. Confirmed NOT the CPU-limit
regression from above — `docker inspect poob` showed `CpuShares=4096` (the
2026-06-20 fix) correctly live throughout.

**Root cause:** a **previously-unknown, unrelated `ollama` stack**
(`ollama/ollama:latest`, compose project `ollama`, working dir
`/host-apps/stacks/ollama/docker` — a THIRD, standalone stack, not
`poob-ollama` and not permitscope) with **zero CPU limit** burst to **311%
CPU / 5.6 GiB** during a single long-running local-LLM prompt (~183s
processing time, per its own logs). `poob-ollama` itself measured 0% CPU the
entire time — it was correctly capped and idle; the damage came from a
sibling stack nobody had capped, because nobody knew it existed until this
investigation.

This confirms the layered design in this note was sound (weight + capping
*known* contenders), but exposed its blind spot: **weight-based priority only
protects against contenders you've already identified and capped.** A new,
uncapped heavy neighbor — on ANY project sharing this host — can reproduce
the exact same starvation regardless of poob's own configuration. This is a
whack-a-mole risk, not a one-time fluke: today it was a standalone `ollama`
stack; tomorrow it could be anything else spun up on the box.

**Fix shipped (two parts):**

1. **Immediate mitigation (live, this stack):** capped the newly-found
   `ollama` container to match the already-proven `poob-ollama` pattern —
   `cpu_shares: 256` + `cpuset: "2,3"` (kept off poob's reserved cores; see
   below). Applied live via `docker update --cpus 2.0 --cpu-shares 256
   --cpuset-cpus 2,3 ollama`. This is a live cgroup change on infra outside
   this repo (a different project's stack) — durable persistence across a
   future recreate of THAT container needs the equivalent change made in its
   own compose file, which lives outside this repo.

2. **Durable escalation (this repo, `deploy/compose.yml`) — corrected design:**
   the first version of this fix `cpuset`-capped poob ITSELF to cores 0,1 —
   a symmetric hard partition (poob confined to 0,1; `poob-ollama` confined
   to 2,3). Operator correctly flagged this before it ever deployed: poob
   "barely ever runs hard," so permanently halving its ceiling trades away
   real burst capacity for a guarantee achievable without that cost.

   **Corrected to an asymmetric fence:** poob is left with **no cpuset
   restriction at all** — free to use all 4 cores whenever nothing else
   needs them, which is nearly always. Only the KNOWN heavy background
   contenders (`poob-ollama`, and the newly-found rogue `ollama` stack, live
   via `docker update --cpuset-cpus 2,3`) are cpuset-fenced OUT of cores 0,1.
   This is a one-way fence, not a shared partition: those containers can
   never touch 0,1 no matter how hard they burst, but poob may still use
   2,3 when they're free. Net effect: poob is unconditionally guaranteed at
   least 2 fully-clear cores under the worst-case burst from either known
   heavy neighbor, with **zero capacity lost** in the overwhelmingly common
   uncontended case — strictly better than the symmetric-partition version,
   for the same worst-case protection.

   Residual gap (accepted, not solved tonight): this protects against the
   two *known* heavy contenders, not a hypothetical brand-new one that
   might appear later uncapped and unfenced — that would need the same
   treatment (cpuset-fence it out of 0,1) applied when/if it's discovered,
   the same way this incident found and fenced today's contender. A
   stronger, still-uncapped-for-poob answer exists (cgroup v2 `cpu.idle` /
   `SCHED_IDLE` on all non-poob containers, which never delays a normal
   task even fractionally) but isn't exposed by plain Docker Compose keys
   and would need custom tooling — noted here as a future option, not
   implemented.

**Validation:** live cgroup checks confirmed the immediate mitigation
(`cpu.max = 200000 100000` on the rogue container, i.e. exactly 2.0 cores;
`cpuset.cpus.effective = 2-3`). Post-deploy, confirm poob's own
`cpuset.cpus.effective` is unset/all-cores (NOT restricted) and `poob-ollama`'s
is `2-3`. Re-verify no further gateway-stall lines appear across a subsequent
multi-hour voice-active window. Only 3 stalls occurred in the 24h window (all
clustered in the single burst hour), consistent with one contained event
rather than constant contention — still zero-tolerance given voice is the
product's core loop.
