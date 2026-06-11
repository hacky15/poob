"""Filler-noise workshop generator.

Synthesizes a batch of candidate Poob "thinking-noise" fillers (Google Fenrir)
for the operator to listen to and pick favorites — the same listen-and-rate loop
behind docs/decisions/poob-noise-fillers.md, but fixing two root causes found
2026-06-10:

  1. SPELL-OUT HACK: 6 of the live fillers were forced to rate 1.25 (fast,
     un-moany) only because Fenrir spells repeated consonants out at slow rates.
     Here every candidate is in the slow 0.7-0.85 "drawn-out moan" zone, using
     vowel-dominant spellings chosen to vocalize without spelling out.
  2. LOUDNESS: live fillers play through speech-tuned `speechnorm=e=12.5` + 3x
     volume, which over-expands soft moans. Candidates here are normalized with
     a gentle `loudnorm` (the proposed new filler path) so what you approve is
     what will play. The first three clips are a loudness A/B/C of the SAME moan
     (raw / new-gentle / old-harsh) so the fix is audible.

NOT in the test suite — hits live Google TTS (a few hundred characters total,
negligible quota). Output: data/filler_candidates/ + a zip + manifest.txt.

Usage (repo root, venv): python tests/manual/gen_filler_candidates.py
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import zipfile
from pathlib import Path

from poob.config import AppConfig
from poob.voice.tts import GoogleCloudTTS

OUT = Path("data/filler_candidates")
VOICE = "en-US-Chirp3-HD-Fenrir"

# The proposed NEW filler loudness: EBU-R128 normalize to a broadcast-ish target
# — consistent, audible, character-preserving. Replaces speechnorm=e=12.5+3x.
NEW_LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
# The CURRENT (harsh) deployed treatment, for the A/B/C reference only.
OLD_HARSH = "speechnorm=e=12.5:r=0.0001:l=1,volume=3.0"

# (label, phrase, rate). All slow-zone. Grouped for variety, vowel-dominant to
# dodge the spell-out problem. The operator picks which vocalize as real moans.
CANDIDATES: list[tuple[str, str, float]] = [
    # --- Augh family (the hero sound — vary length/openness) ---
    ("augh_a", "Aughhh.", 0.80),
    ("augh_b", "Auughhh.", 0.80),
    ("augh_c", "Aaughhh.", 0.78),
    ("augh_d", "Auuughh.", 0.76),
    ("augh_e", "Aaaughhh.", 0.78),
    ("augh_slow", "Aughhhh.", 0.72),
    # --- Oh / sigh family ---
    ("oh_a", "Ohhh.", 0.80),
    ("oh_b", "Ohhhhh.", 0.76),
    ("oh_c", "Oohhh.", 0.80),
    ("oh_slow", "Ohhhh.", 0.72),
    # --- Groan / grunt family (mm/un/ng + vowel, NOT pure mmm) ---
    ("groan_mmgh", "Mmghh.", 0.80),
    ("groan_mngh", "Mnghh.", 0.80),
    ("groan_ungh", "Unghh.", 0.80),
    ("groan_hmgh", "Hmghh.", 0.80),
    ("groan_aohh", "Aohh.", 0.80),
    # --- Short / quick utility noises (snappier, still slow-rate) ---
    ("short_auh", "Auh.", 0.85),
    ("short_ungh", "Ungh.", 0.82),
    ("short_uhmm", "Uhmm.", 0.82),
    # --- Spell-out re-tests at SLOW rate (do these still spell out?) ---
    ("retest_ughhh", "Ughhh.", 0.80),
    ("retest_uuughhh", "Uuughhh.", 0.78),
    # --- Experimental middle-ground: tiny quip + moan tail (operator floated) ---
    ("quip_augh_yeah", "Augh, yeah.", 0.82),
    ("quip_mm_augh", "Mm, aughh.", 0.80),
    ("quip_ohman", "Oh man, aughh.", 0.82),
]


def _ffmpeg(src: Path, dst: Path, af: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-af", af, str(dst)],
        check=True,
    )


async def main() -> None:
    cfg = AppConfig()
    key = cfg.google_cloud_vision_api_key
    if not key:
        raise SystemExit("No google_cloud_vision_api_key in .env")

    # Clean slate — rmtree handles both files and the _raw subdir (a bare
    # glob+unlink fails on the directory with PermissionError on Windows).
    if OUT.exists():
        shutil.rmtree(OUT)
    raw_dir = OUT / "_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    manifest: list[str] = []
    idx = 0

    async def synth(phrase: str, rate: float) -> Path:
        tts = GoogleCloudTTS(api_key=key, voice=VOICE, speaking_rate=rate)
        audio = await tts.synthesize(phrase)
        p = raw_dir / f"raw_{rate}_{abs(hash(phrase)) % 10000}.mp3"
        p.write_bytes(audio)
        return p

    # --- Loudness A/B/C reference on the hero moan ---
    hero = await synth("Aughhhh.", 0.80)
    for tag, af in (("A_raw", "anull"), ("B_new_gentle", NEW_LOUDNORM), ("C_old_harsh", OLD_HARSH)):
        dst = OUT / f"00_LOUDNESS_{tag}.mp3"
        _ffmpeg(hero, dst, af)
        manifest.append(f"00_LOUDNESS_{tag}  ->  hero 'Aughhhh.' @0.80, treatment={tag}")
    manifest.append("    (compare: B = proposed new filler loudness, C = current harsh one)")
    manifest.append("")

    # --- Candidates (new gentle loudness) ---
    for label, phrase, rate in CANDIDATES:
        idx += 1
        raw = await synth(phrase, rate)
        dst = OUT / f"{idx:02d}_{label}_r{int(rate*100)}.mp3"
        _ffmpeg(raw, dst, NEW_LOUDNORM)
        manifest.append(f"{idx:02d}_{label}_r{int(rate*100)}  ->  {phrase!r} @ rate {rate}")

    (OUT / "manifest.txt").write_text("\n".join(manifest), encoding="utf-8")

    # zip everything (minus _raw)
    zip_path = OUT / "filler_candidates.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(OUT.glob("*.mp3")):
            z.write(f, f.name)
        z.write(OUT / "manifest.txt", "manifest.txt")

    print(f"Generated {idx} candidates + 3 loudness refs -> {OUT}")
    print(f"Zip: {zip_path}")
    print("Manifest:\n" + "\n".join(manifest))


if __name__ == "__main__":
    asyncio.run(main())
