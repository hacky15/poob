---
type: runbook
status: active
date: 2026-06-08
tags: [homelab, infrastructure, memory, observability, wedge, multi-tenant]
related: [[anon-browser-cdp-death-no-recovery]] [[in-process-browser-recycle]]
---

# Runbook: diagnose & fix the recurring homelab host wedge (memory thrash)

## Symptom

Host (`homelab` / 100.125.74.35) periodically goes unreachable: ICMP 100% loss,
and `ssh` reaches port 22 but **"banner exchange … timed out"** — `sshd` is too
memory-starved to complete a login. The box is up but thrashing. Every container
(Poob included) is unreachable. Recovers only on reboot. Recurs ~hourly.

**This is NOT Poob.** Poob is hard-capped at 4 GB (cgroup) and cannot OOM a 24 GB
host. The host runs ~19 containers across multiple tenants — notably the
*permitscope* project (GHA runners, **2 buildkit builders, mongod**) plus
Komodo's deploy stack. The wedge is host-level resource exhaustion.

## Most likely cause (hypothesis — CONFIRM with the sampler below)

1. **mongod / WiredTiger cache (the baseline hog).** WiredTiger defaults its
   cache to `(RAM − 1 GB) / 2` ≈ **~11 GB on a 24 GB host** when uncapped. That
   alone keeps the box near-full at baseline, so *any* spike tips it into swap.
2. **buildkit (×2) + GHA build jobs (the tipping spike).** Image builds spike
   multi-GB transiently; two concurrent builds on an already-full box = the
   freeze. Komodo retrying a build that's sitting on GHCR fits the ~hourly cadence.
3. **No memory limits + no OS headroom reserve** → a spike starves the kernel /
   page cache / `sshd` instead of being bounded.

The fix is two-phase: **(A) get visibility that survives the wedge**, then
**(B) bound the culprits**.

## Phase A — wedge-surviving observability (deploy the moment the host is back)

The key property: sample to **disk** every ~20s, and make the sampler
**OOM-immune** so it keeps logging *into* the freeze. After the next wedge, the
log shows exactly which container/process ballooned in the 2–3 min before.

### 1. `/usr/local/bin/host-mem-sampler.sh`

```bash
#!/usr/bin/env bash
set -u
LOG=${LOG:-/var/log/host-mem-sampler.log}
INTERVAL=${INTERVAL:-20}
maxbytes=$((50*1024*1024))   # rotate at 50 MB
sample() {
  echo "[$(date -u +%FT%TZ)] PSI{ $(tr '\n' ' ' < /proc/pressure/memory 2>/dev/null)}"
  free -m | awk '/Mem:/{printf "    mem used=%s/%sMB avail=%sMB\n",$3,$2,$7}
                 /Swap:/{printf "    swap used=%s/%sMB\n",$3,$2}'
  vmstat 1 2 2>/dev/null | tail -1 | awk '{printf "    vm si=%s so=%s wa=%s (so>0 = paging out = THRASH)\n",$7,$8,$16}'
  ps -eo rss,comm --sort=-rss 2>/dev/null | awk 'NR>1 && NR<=8 {printf "    proc %-22s %6.0fMB\n",$2,$1/1024}'
  timeout 8 docker stats --no-stream --format '    ctr {{.Name}} {{.MemUsage}} cpu={{.CPUPerc}}' 2>/dev/null | head -25
}
while true; do
  [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt "$maxbytes" ] && mv -f "$LOG" "${LOG}.1"
  sample >> "$LOG" 2>&1
  sleep "$INTERVAL"
done
```

### 2. `/etc/systemd/system/host-mem-sampler.service`

```ini
[Unit]
Description=Host memory observability sampler (wedge-surviving)
After=docker.service
[Service]
ExecStart=/usr/local/bin/host-mem-sampler.sh
Restart=always
RestartSec=5
Nice=10
IOSchedulingClass=idle
OOMScoreAdjust=-900        # do NOT let the OOM killer take the sampler
MemoryMax=64M             # but cap it so it can never be the problem
[Install]
WantedBy=multi-user.target
```

```bash
sudo install -m755 host-mem-sampler.sh /usr/local/bin/host-mem-sampler.sh
sudo systemctl daemon-reload && sudo systemctl enable --now host-mem-sampler
```

`PSI` (`/proc/pressure/memory`) is the single best "is the box thrashing" signal,
and `vmstat so>0` confirms active swap-out. Both are near-zero on a healthy box.

## Phase B — read it after the next wedge

```bash
# What did the kernel OOM-kill (if anything)?
journalctl -k --since "90 min ago" | grep -iE "oom|out of memory|killed process"
# The run-up to the freeze — last samples before the time gap:
tail -120 /var/log/host-mem-sampler.log
# Persistent baseline hog right now (sorted):
docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}' | sort -h -k2
# mongod cache size (the #1 suspect):
docker exec <mongo-container> mongosh --quiet --eval \
  'print((db.serverStatus().wiredTiger.cache["bytes currently in the cache"]/1073741824).toFixed(1)+" GB in WT cache")'
```

## Phase C — bound the culprits (needs operator coordination — these are the
## *permitscope* tenant's containers, not Poob's)

- **Cap mongod:** `--wiredTigerCacheSizeGB 2` (compose `command:`), or a
  `mem_limit: 4g` on the container. Usually the single biggest win.
- **Bound + serialize builds:** `mem_limit` on the buildkit builders; run builds
  with concurrency 1 (GHA runner count, or BuildKit `--oom-score-adj` + a single
  builder) so two builds can't spike together.
- **Reserve OS headroom:** the sum of all container `mem_limit`s should be
  < ~18 GB so ~6 GB stays for the kernel/page-cache/`sshd`. A spike then hits a
  container's limit, not the host.
- **Swap as a shock absorber:** ensure a few GB of swap + `vm.swappiness=10`, so
  a transient spike pages slowly instead of OOM-thrashing — but PSI/`so` tells
  you if you're relying on it too much.
- **Protect sshd:** `sudo systemctl set-property ssh.service OOMScoreAdjust=-800`
  so you can always get a shell to investigate, even mid-spike.

## Invariants

- The sampler is `Nice`/`idle`/64 MB-capped — it does not add meaningful load.
- Poob is not the cause and pausing it does not help; do not chase it here.
- Mitigations on *permitscope* containers require the operator's sign-off — this
  runbook's Phase A/B (observe) is safe and tenant-neutral; Phase C (limit) is not.
