"""Generate negative samples one phrase at a time via subprocess isolation.

Each phrase gets its own Python process + fresh Edge TTS connection.
This avoids the WebSocket connection death that kills long-running async loops.

Usage: python scripts/gen_neg_batch.py
"""
import os
import subprocess
import sys
import time

NEGATIVE_PHRASES = [
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
    "Hey Bruce", "Hey Brooke", "Hey Luke",
    "Hey Ruth", "Hey Booth", "Hey Drew",
    "Hey you", "hey you!", "hey, you there",
    "Hey Google", "Hey Siri", "Hey Alexa",
    "that's pog", "let's go", "what the",
    "no way", "oh my god", "are you serious",
    "let me think", "hold on", "wait what",
    "good game", "nice one", "well played",
]

NEG_DIR = os.path.join("data", "wake_word_training", "negative")

# Worker script that generates all voice/rate combos for ONE phrase
WORKER = '''
import asyncio, io, os, sys, edge_tts

VOICES = [
    "en-US-AnaNeural", "en-US-AndrewNeural", "en-US-AriaNeural",
    "en-US-AvaNeural", "en-US-BrianNeural", "en-US-ChristopherNeural",
    "en-US-EmmaNeural", "en-US-EricNeural", "en-US-GuyNeural",
    "en-US-JennyNeural", "en-US-MichelleNeural", "en-US-RogerNeural",
    "en-US-SteffanNeural",
    "en-GB-LibbyNeural", "en-GB-RyanNeural", "en-GB-SoniaNeural", "en-GB-ThomasNeural",
    "en-AU-NatashaNeural",
    "en-CA-ClaraNeural", "en-CA-LiamNeural",
    "en-IN-NeerjaNeural", "en-IN-PrabhatNeural",
    "en-IE-ConnorNeural", "en-IE-EmilyNeural",
    "en-NZ-MitchellNeural", "en-NZ-MollyNeural",
    "en-SG-LunaNeural", "en-SG-WayneNeural",
    "en-ZA-LeahNeural", "en-ZA-LukeNeural",
]
RATES = ["-20%", "-10%", "+0%", "+10%", "+20%", "+30%"]

def to_wav(mp3_bytes):
    import subprocess, tempfile
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(mp3_bytes); inp = f.name
    out = inp.replace(".mp3", ".wav")
    try:
        subprocess.run(["ffmpeg","-y","-i",inp,"-ar","16000","-ac","1","-sample_fmt","s16",out],
                       capture_output=True, timeout=10)
        if os.path.exists(out):
            with open(out,"rb") as f: return f.read()
    except: pass
    finally:
        for p in [inp,out]:
            try: os.unlink(p)
            except: pass
    return None

async def gen(voice, phrase, rate):
    try:
        c = edge_tts.Communicate(phrase, voice=voice, rate=rate)
        buf = io.BytesIO()
        async for chunk in c.stream():
            if chunk["type"]=="audio": buf.write(chunk["data"])
        d = buf.getvalue()
        return to_wav(d) if len(d)>=500 else None
    except: return None

async def main():
    phrase = sys.argv[1]
    out_dir = sys.argv[2]
    existing = set(os.listdir(out_dir))
    ok = 0; fail = 0
    for voice in VOICES:
        for rate in RATES:
            vt = voice.replace("Neural","")
            rt = rate.replace("+","p").replace("-","m").replace("%","")
            pt = phrase[:20].replace(" ","_").replace(",","").replace("'","").replace("!","").replace("?","")
            fn = f"neg_{vt}_{rt}_{pt}.wav"
            if fn in existing: continue
            wav = await gen(voice, phrase, rate)
            if wav:
                with open(os.path.join(out_dir,fn),"wb") as f: f.write(wav)
                ok += 1
            else: fail += 1
    print(f"{ok} ok {fail} fail")

asyncio.run(main())
'''


def main():
    os.makedirs(NEG_DIR, exist_ok=True)
    existing_count = len([f for f in os.listdir(NEG_DIR) if f.endswith(".wav")])
    print(f"Existing negatives: {existing_count}", flush=True)

    total_new = 0
    t0 = time.time()

    for i, phrase in enumerate(NEGATIVE_PHRASES):
        try:
            result = subprocess.run(
                [sys.executable, "-c", WORKER, phrase, NEG_DIR],
                capture_output=True, text=True, timeout=300,
            )
            out = result.stdout.strip()
            elapsed = time.time() - t0
            current = len([f for f in os.listdir(NEG_DIR) if f.endswith(".wav")])
            print(f"[{i+1}/{len(NEGATIVE_PHRASES)}] \"{phrase[:25]:25s}\" {out:20s} | total={current} ({elapsed:.0f}s)", flush=True)
        except subprocess.TimeoutExpired:
            print(f"[{i+1}/{len(NEGATIVE_PHRASES)}] \"{phrase[:25]:25s}\" TIMEOUT", flush=True)
        except Exception as e:
            print(f"[{i+1}/{len(NEGATIVE_PHRASES)}] \"{phrase[:25]:25s}\" ERROR: {e}", flush=True)

    final = len([f for f in os.listdir(NEG_DIR) if f.endswith(".wav")])
    print(f"\nDONE: {final} total negative samples", flush=True)


if __name__ == "__main__":
    main()
