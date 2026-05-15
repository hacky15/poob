---
type: incident
status: resolved
date: 2026-05-14
tags: [voice, wake-word, docker, infra, disk]
related: [[docker-desktop-data-vhd-separate-from-engine]] [[wake-word-mass-augmentation-v3]] [[wake-word-retrain-v3]]
---

# C: drive filled to 1.69 GB during wake-word v3 trainer image builds

## Symptom

During the wake-word v3 training session, the user's C: drive crashed from ~30 GB free to **1.69 GB free** despite "Docker Desktop relocated to F:" being a checked-off task from earlier in the same session. The Docker `build` eventually died mid-export with:

```
ERROR: failed to solve: failed to create temp dir:
mkdir /var/lib/desktop-containerd/daemon/tmpmounts/containerd-mount...: input/output error
```

`docker run` immediately afterward failed on the same path with `blob ... input/output error` — the containerd content store was out of physical disk.

## Root cause

Docker Desktop on Windows 10 WSL2 backend uses **two separate VHD files**, and the earlier "relocate to F:" had only moved one of them:

1. **Engine distro** — `docker-desktop` WSL distribution, ~160 MB. This was the file moved earlier via `wsl --export` / `--unregister` / `--import --vhd` to `F:\wsl\docker-desktop\ext4.vhdx`. Engine works, GPU passthrough works, all good.
2. **Image store** — `docker_data.vhdx`, holds the containerd content blobs, image layers, container layers, and volume data. **Default path: `%LOCALAPPDATA%\Docker\wsl\disk\docker_data.vhdx`.** This file was never moved. It silently kept growing on C: as we built the 12 GB `poob-wake-trainer` image, downloaded ACAV (~2 GB cached inside the running container before being written to the bind-mount), and accumulated container-runtime overhead. By the time the user flagged it, this VHD was at **31.38 GB** on C:.

The Docker Desktop GUI's "Disk image location" setting (`Settings → Resources → Advanced`) is supposed to relocate this file, but on this Windows 10 WSL2 install the option is missing entirely (only visible on Hyper-V backend or newer Docker Desktop builds).

The smoke extraction itself wrote correctly to `F:\wake_word_v3\features\` via the bind mount — the contamination was purely inside Docker's own image store.

## Fix

Move the data VHD to F: and put a directory junction at the original path so Docker Desktop transparently keeps using its hard-coded default location:

```powershell
# 1. Stop Docker fully
taskkill /F /IM "Docker Desktop.exe" /T
taskkill /F /IM "com.docker.backend.exe" /T
wsl --shutdown

# 2. Move the 31 GB VHD (cross-drive copy + delete; ~3.5 min)
Move-Item `
  -Path "C:\Users\19203\AppData\Local\Docker\wsl\disk\docker_data.vhdx" `
  -Destination "F:\Docker\wsl\disk\docker_data.vhdx" `
  -Force

# 3. Remove the now-empty C: dir and create a directory junction
Remove-Item "C:\Users\19203\AppData\Local\Docker\wsl\disk" -Recurse -Force
cmd /c "mklink /J `"C:\Users\19203\AppData\Local\Docker\wsl\disk`" `"F:\Docker\wsl\disk`""

# 4. Relaunch Docker Desktop normally
Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
```

Docker Desktop has no idea the path is a junction — it reads/writes the default location and the I/O lands on F:.

## Validation

After the restart:

- `docker images` showed all 6 images preserved, `poob-wake-trainer:latest 12 GB` intact.
- `docker run --rm --gpus all poob-wake-trainer:latest "nvidia-smi -L"` → `GPU 0: NVIDIA GeForce RTX 2070 SUPER` (passthrough still works).
- `C:` free: **7.11 GB → 38.44 GB** (31 GB reclaimed).
- `F:` free: **611.51 GB → 580.10 GB** (took the 31 GB).
- `F:\wake_word_v3\` corpus + features were completely untouched — the smoke-extracted `positive_features.npy` (728 MB) and `negative_features.npy` (10 MB) loaded back cleanly with their original shapes `(118493, 16, 96)` and `(1600, 16, 96)`.

## Follow-ups

- The durable form of this lesson is filed at [[docker-desktop-data-vhd-separate-from-engine]] — any future "relocate Docker Desktop" task must move **both** VHDs, not just the engine distro.
- The runbook [[wake-word-retrain-v3]] pre-flight already warns about Docker's WSL VHD on C: by default, but the runbook's guidance assumed a single GUI setting would handle it. Update the runbook to call out the two-VHD reality and the junction recipe explicitly.
- ~30 minutes of debugging time was lost to this. Corpus generation and the smoke extraction itself ran fine because they all wrote via the F: bind mount — only Docker's own metadata + image store hit C:.
