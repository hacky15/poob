"""Generate synthetic 'Hey Poob' audio samples for OpenWakeWord training.

Uses Edge TTS with ALL available English voices at varied speeds to create
diverse positive training samples + adversarial negative samples.

Target: 15,000+ positive, 2,000+ adversarial negative samples.

Usage:
    python scripts/generate_wake_word_samples.py
    python scripts/generate_wake_word_samples.py --negatives-only
    python scripts/generate_wake_word_samples.py --count  (just count existing)

Output:
    data/wake_word_training/positive/   — "Hey Poob" variations
    data/wake_word_training/negative/   — phonetically similar non-wake-words
"""

import asyncio
import io
import os
import sys
import random

# ──────────────────────────────────────────────────────────────────────
# Positive phrases — "Hey Poob" is the primary wake word.
# Variations in phrasing, punctuation, and trailing context help the
# model learn the ONSET pattern regardless of what follows.
# ──────────────────────────────────────────────────────────────────────
POSITIVE_PHRASES = [
    # Core wake word — 5x weight (most important pattern)
    "Hey Poob", "Hey Poob", "Hey Poob", "Hey Poob", "Hey Poob",
    "Hey Poob!", "Hey Poob!", "Hey Poob!",
    "Hey, Poob", "Hey, Poob", "Hey, Poob",
    "Hey, Poob!", "Hey Poob.",
    # Slightly different intonation cues
    "Hey Poob?",
    "Hey, Poob?",
    "Hey Poob, hey",
    # With trailing context — model must detect the onset
    "Hey Poob, what's up",
    "Hey Poob, can you hear me",
    "Hey Poob, what do you think",
    "Hey Poob, are you there",
    "Hey Poob, say something",
    "Hey Poob, help me out",
    "Hey Poob, listen to this",
    "Hey Poob, come here",
    "Hey Poob, answer me",
    "Hey Poob, what time is it",
    "Hey Poob, tell me a joke",
    "Hey Poob, how are you",
    "Hey Poob, do you know",
    "Hey Poob, check this out",
    "Hey Poob, play a song",
    "Hey Poob, stop that",
    "Hey Poob, turn it off",
    "Hey Poob, what was that",
    "Hey Poob, I have a question",
    "Hey Poob, do me a favor",
    "Hey Poob, look at this",
    "Hey Poob, hold on",
    "Hey Poob, guess what",
    "Hey Poob, you listening",
    "Hey Poob, what should I do",
    "Hey Poob, is anyone there",
    "Hey Poob, wake up",
    "Hey Poob, pay attention",
    # Casual/fast delivery
    "Ay Poob",
    "Ey Poob",
    "Hey Poob hey Poob",
    "Yo hey Poob",
]

# ──────────────────────────────────────────────────────────────────────
# Adversarial negative phrases — phonetically similar to "Hey Poob"
# but should NOT trigger the wake word. Critical for <5% FP rate.
# ──────────────────────────────────────────────────────────────────────
NEGATIVE_PHRASES = [
    # Rhymes / near-misses for "Poob"
    "Hey tube", "the tube", "YouTube", "a tube",
    "Hey dude", "hey dude!", "the dude",
    "Hey boo", "hey boo!", "boo hoo",
    "Hey boob", "nice boob", "what a boob",
    "Hey poop", "oh poop", "dog poop",
    "Hey pool", "the pool", "swimming pool",
    "Hey cool", "that's cool", "so cool",
    "Hey fool", "you fool", "what a fool",
    "Hey mood", "bad mood", "good mood",
    "Hey food", "good food", "fast food",
    "Hey boot", "nice boot", "the boot",
    "Hey loop", "in the loop", "a loop",
    "Hey drool", "don't drool",
    "Hey boom", "big boom", "ka boom",
    "Hey room", "the room", "my room",
    "Hey zoom", "let's zoom",
    "Hey boop", "boop boop", "beep boop",
    "Hey scoop", "big scoop",
    "Hey stoop", "on the stoop",
    "Hey spoon", "a spoon",
    "Hey moon", "the moon", "full moon",
    "Hey soon", "coming soon", "pretty soon",
    "Hey noon", "at noon", "high noon",
    # "Hey [name]" patterns that shouldn't trigger
    "Hey Bruce", "Hey Brooke", "Hey Luke",
    "Hey Ruth", "Hey Booth", "Hey Drew",
    "Hey you", "hey you!", "hey, you there",
    "Hey Google", "Hey Siri", "Hey Alexa",
    # Common Discord chatter
    "that's pog", "let's go", "what the",
    "no way", "oh my god", "are you serious",
    "let me think", "hold on", "wait what",
    "good game", "nice one", "well played",
]

# ──────────────────────────────────────────────────────────────────────
# ALL 47 English Edge TTS voices for maximum speaker diversity
# ──────────────────────────────────────────────────────────────────────
VOICES = [
    # US voices (17)
    "en-US-AnaNeural", "en-US-AndrewNeural", "en-US-AndrewMultilingualNeural",
    "en-US-AriaNeural", "en-US-AvaNeural", "en-US-AvaMultilingualNeural",
    "en-US-BrianNeural", "en-US-BrianMultilingualNeural",
    "en-US-ChristopherNeural", "en-US-EmmaNeural", "en-US-EmmaMultilingualNeural",
    "en-US-EricNeural", "en-US-GuyNeural", "en-US-JennyNeural",
    "en-US-MichelleNeural", "en-US-RogerNeural", "en-US-SteffanNeural",
    # GB voices (5)
    "en-GB-LibbyNeural", "en-GB-MaisieNeural", "en-GB-RyanNeural",
    "en-GB-SoniaNeural", "en-GB-ThomasNeural",
    # AU voices (2)
    "en-AU-NatashaNeural", "en-AU-WilliamMultilingualNeural",
    # CA voices (2)
    "en-CA-ClaraNeural", "en-CA-LiamNeural",
    # IN voices (3)
    "en-IN-NeerjaNeural", "en-IN-NeerjaExpressiveNeural", "en-IN-PrabhatNeural",
    # IE voices (2)
    "en-IE-ConnorNeural", "en-IE-EmilyNeural",
    # Other regional (14)
    "en-HK-YanNeural", "en-HK-SamNeural",
    "en-KE-AsiliaNeural", "en-KE-ChilembaNeural",
    "en-NZ-MitchellNeural", "en-NZ-MollyNeural",
    "en-NG-AbeoNeural", "en-NG-EzinneNeural",
    "en-PH-JamesNeural", "en-PH-RosaNeural",
    "en-SG-LunaNeural", "en-SG-WayneNeural",
    "en-ZA-LeahNeural", "en-ZA-LukeNeural",
    "en-TZ-ElimuNeural", "en-TZ-ImaniNeural",
]

# Speed variations — 8 levels for more diversity
RATES = ["-30%", "-20%", "-10%", "-5%", "+0%", "+10%", "+20%", "+30%"]


def mp3_to_wav_16k_mono(mp3_bytes: bytes) -> bytes | None:
    """Convert MP3 bytes to 16kHz mono WAV using ffmpeg."""
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_in:
        tmp_in.write(mp3_bytes)
        tmp_in_path = tmp_in.name

    tmp_out_path = tmp_in_path.replace(".mp3", ".wav")

    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", tmp_in_path,
                "-ar", "16000", "-ac", "1", "-sample_fmt", "s16",
                tmp_out_path,
            ],
            capture_output=True,
            timeout=10,
        )
        if os.path.exists(tmp_out_path):
            with open(tmp_out_path, "rb") as f:
                return f.read()
    except Exception:
        pass
    finally:
        for p in [tmp_in_path, tmp_out_path]:
            try:
                os.unlink(p)
            except OSError:
                pass
    return None


async def generate_sample(voice: str, text: str, rate: str, retries: int = 2) -> bytes | None:
    """Generate a single TTS sample and convert to 16kHz mono WAV."""
    import edge_tts

    for attempt in range(retries + 1):
        try:
            communicate = edge_tts.Communicate(text, voice=voice, rate=rate)
            mp3_buf = io.BytesIO()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    mp3_buf.write(chunk["data"])

            mp3_bytes = mp3_buf.getvalue()
            if len(mp3_bytes) < 500:
                return None

            return mp3_to_wav_16k_mono(mp3_bytes)
        except Exception:
            if attempt < retries:
                await asyncio.sleep(1 + attempt * 2)  # Back off: 1s, 3s
            continue
    return None


async def generate_batch(
    phrases: list[str],
    out_dir: str,
    prefix: str,
    voices: list[str] | None = None,
    rates: list[str] | None = None,
) -> int:
    """Generate samples for a list of phrases, skipping existing files.

    Returns total count of files in out_dir after generation.
    """
    voices = voices or VOICES
    rates = rates or RATES
    os.makedirs(out_dir, exist_ok=True)

    # Build set of already-generated combos to skip
    existing_files = set(os.listdir(out_dir))
    existing_count = len([f for f in existing_files if f.endswith(".wav")])

    # Use a counter that continues from existing files
    count = existing_count
    total = len(phrases) * len(voices) * len(rates)
    generated = 0
    skipped = 0
    failed = 0

    print(f"Target: {total} {prefix} combos, {existing_count} already exist", flush=True)

    for phrase in phrases:
        for voice in voices:
            for rate in rates:
                # Build deterministic filename from combo (not counter)
                voice_tag = voice.replace("Neural", "").replace("Multilingual", "M")
                rate_tag = rate.replace("+", "p").replace("-", "m").replace("%", "")
                phrase_tag = phrase[:20].replace(" ", "_").replace(",", "").replace("'", "").replace("!", "").replace("?", "")
                fname = f"{prefix}_{voice_tag}_{rate_tag}_{phrase_tag}.wav"

                if fname in existing_files:
                    skipped += 1
                    continue

                wav_data = await generate_sample(voice, phrase, rate)
                if wav_data:
                    path = os.path.join(out_dir, fname)
                    with open(path, "wb") as f:
                        f.write(wav_data)
                    generated += 1
                else:
                    failed += 1

                if (generated + skipped) % 200 == 0 and generated > 0:
                    print(f"  {generated} new + {skipped} existing / {total} ({failed} failed)", flush=True)

    final = len([f for f in os.listdir(out_dir) if f.endswith(".wav")])
    print(f"  Done: {generated} new, {skipped} skipped, {failed} failed. Total: {final}", flush=True)
    return final


async def main():
    pos_dir = os.path.join("data", "wake_word_training", "positive")
    neg_dir = os.path.join("data", "wake_word_training", "negative")

    negatives_only = "--negatives-only" in sys.argv
    count_only = "--count" in sys.argv

    if count_only:
        pos_count = len([f for f in os.listdir(pos_dir) if f.endswith(".wav")]) if os.path.exists(pos_dir) else 0
        neg_count = len([f for f in os.listdir(neg_dir) if f.endswith(".wav")]) if os.path.exists(neg_dir) else 0
        print(f"Positive samples: {pos_count}")
        print(f"Negative samples: {neg_count}")
        return

    if not negatives_only:
        pos_total = await generate_batch(POSITIVE_PHRASES, pos_dir, "pos")
        print(f"\nPositive samples: {pos_total}", flush=True)

    neg_total = await generate_batch(NEGATIVE_PHRASES, neg_dir, "neg")
    print(f"\nNegative samples: {neg_total}", flush=True)

    print("\n=== Summary ===", flush=True)
    pos_final = len([f for f in os.listdir(pos_dir) if f.endswith(".wav")]) if os.path.exists(pos_dir) else 0
    neg_final = len([f for f in os.listdir(neg_dir) if f.endswith(".wav")]) if os.path.exists(neg_dir) else 0
    print(f"Positive: {pos_final}", flush=True)
    print(f"Negative: {neg_final}", flush=True)
    print(f"Target: 10,000+ positive, 2,000+ negative", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
