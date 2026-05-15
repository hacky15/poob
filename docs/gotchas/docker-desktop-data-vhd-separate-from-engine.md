---
type: gotcha
status: active
date: 2026-05-14
tags: [docker, infra, windows, wsl2, disk]
related: [[c-drive-docker-data-vhd-trap-2026-05-14]] [[wake-word-retrain-v3]]
---

# Relocating Docker Desktop's WSL distro does NOT move its image store

## Trigger

Setting up a Windows dev box where C: has limited space and you want Docker Desktop to live on a different drive (D:, E:, F:). You move the `docker-desktop` WSL distro to the target drive via `wsl --export` / `--unregister` / `--import --vhd`, mark the relocation complete, then start building images — C: silently fills up anyway.

## Why it happens

Docker Desktop on Windows 10/11 WSL2 backend uses **two separate VHD files**, only one of which a distro move touches:

1. **`<wsl-root>\docker-desktop\ext4.vhdx`** — the engine distro. Tiny (~160 MB) and barely grows. This is what `wsl --import` moves.
2. **`%LOCALAPPDATA%\Docker\wsl\disk\docker_data.vhdx`** — the **image store**: containerd content blobs, image layers, container layers, volume data, build cache. **Grows unbounded** with every `docker build` / `docker pull`. Default location is hard-coded and a `wsl --import` of the engine distro does not touch it.

The Docker Desktop GUI is supposed to handle the second file via `Settings → Resources → Advanced → "Disk image location"`. On Hyper-V backend and newer Docker Desktop builds it works. On Windows 10 with the WSL2 backend that the user runs here, **the setting is missing from the GUI** — there's nowhere to change it. So users who "moved Docker" expect the second VHD to follow, and it doesn't.

The incident that surfaced this: [[c-drive-docker-data-vhd-trap-2026-05-14]]. We built a 12 GB trainer image after the "relocation," confidently watched C: drain to 1.69 GB free, then hit a containerd I/O error mid-build.

## Don't

Don't trust that moving the `docker-desktop` distro is enough. Don't assume the GUI's "Disk image location" setting is present — on Windows 10 WSL2 backend it usually isn't. Don't go hunting for the right `dataFolder` key in `%APPDATA%\Docker\settings-store.json` either — the schema varies between Docker Desktop versions, may be unset/empty by default, and Docker doesn't always honor it for the WSL2 disk path anyway.

## Do

After moving the engine distro, find and relocate the data VHD too, then put a directory junction at the original path so Docker Desktop transparently uses its hard-coded default:

```powershell
# Audit — find every VHD on the system
Get-ChildItem -Path C:\,D:\,E:\,F:\ -Filter "*.vhdx" -Recurse `
    -ErrorAction SilentlyContinue |
  Select-Object FullName, @{N='SizeGB';E={[math]::Round($_.Length/1GB,2)}}

# The big one (multi-GB) is the data VHD. Relocate it:
taskkill /F /IM "Docker Desktop.exe" /T
taskkill /F /IM "com.docker.backend.exe" /T
wsl --shutdown

Move-Item `
  -Path "C:\Users\19203\AppData\Local\Docker\wsl\disk\docker_data.vhdx" `
  -Destination "<target-drive>:\Docker\wsl\disk\docker_data.vhdx" -Force

Remove-Item "C:\Users\19203\AppData\Local\Docker\wsl\disk" -Recurse -Force
cmd /c "mklink /J `"C:\Users\19203\AppData\Local\Docker\wsl\disk`" `
  `"<target-drive>:\Docker\wsl\disk`""

Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
```

Docker Desktop reads `C:\...\Docker\wsl\disk\docker_data.vhdx` the same way it always did — but the junction transparently resolves to the target drive. All images survive the move. GPU passthrough survives.

When validating: `docker images` should show every image you had before; the underlying VHD on the new drive should match the size of the moved file; C: free space should jump by the moved amount.

## Reference

- Incident that surfaced this: [[c-drive-docker-data-vhd-trap-2026-05-14]]
- The wake-word retrain runbook that needs to call this out: [[wake-word-retrain-v3]]
- The directory junction is a Windows NTFS feature distinct from `mklink /D` (symlink) — Docker Desktop's WSL2 backend works with junctions because the underlying NTFS resolves the path before the WSL hypervisor sees it.
