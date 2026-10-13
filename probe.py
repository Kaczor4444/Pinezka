#!/usr/bin/env python3
"""Probe: can we tell music from speech on the RMF FM stream?

1) ICY metadata (StreamTitle) - does the stream say which song is playing?
2) inaSpeechSegmenter - speech / music / noise segments on a recorded sample.
Results go to the log and (summary) to ntfy.
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.request

URLS = [
    "https://rs201-krk.rmfstream.pl/rmf_fm",
    "https://rs101-krk.rmfstream.pl/RMFFM48",
]
SECONDS = int(os.environ.get("PROBE_SECONDS", "240"))
TOPIC = os.environ.get("NTFY_TOPIC", "")
T0 = time.time()


def ts():
    return f"{time.time() - T0:6.1f}s"


def notify(title, msg):
    print(f"\n=== {title} ===\n{msg}\n", flush=True)
    if not TOPIC:
        return
    body = json.dumps({"topic": TOPIC, "title": title, "message": msg[:3900]}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            "https://ntfy.sh", data=body, method="POST",
            headers={"Content-Type": "application/json"}), timeout=15).read()
    except Exception as e:  # noqa: BLE001
        print("ntfy error", e)


# ------------------------------------------------------------------ 1) ICY metadata
icy_report = {}


def icy_probe(url):
    rep = {"titles": [], "headers": {}}
    icy_report[url] = rep
    try:
        req = urllib.request.Request(url, headers={"Icy-MetaData": "1", "User-Agent": "Mozilla/5.0"})
        r = urllib.request.urlopen(req, timeout=20)
        rep["headers"] = {k: v for k, v in r.headers.items() if k.lower().startswith(("icy", "content-type", "ice"))}
        metaint = int(r.headers.get("icy-metaint", "0") or 0)
        rep["metaint"] = metaint
        if not metaint:
            return
        end = time.time() + SECONDS
        last = None
        while time.time() < end:
            r.read(metaint)
            n = r.read(1)
            if not n:
                break
            ln = n[0] * 16
            if ln:
                meta = r.read(ln).rstrip(b"\0").decode("utf-8", "replace")
                m = re.search(r"StreamTitle='(.*?)';", meta)
                title = m.group(1) if m else meta
                if title != last:
                    rep["titles"].append((ts(), title))
                    print(f"[{ts()}] ICY {url.split('/')[-1]}: {title!r}", flush=True)
                    last = title
    except Exception as e:  # noqa: BLE001
        rep["error"] = repr(e)


threads = [threading.Thread(target=icy_probe, args=(u,), daemon=True) for u in URLS]
for t in threads:
    t.start()

# ------------------------------------------------------------------ 2) record + segment
wav = "sample.wav"
print(f"[{ts()}] recording {SECONDS}s", flush=True)
subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", URLS[0],
                "-t", str(SECONDS), "-ac", "1", "-ar", "16000", wav], check=False)
for t in threads:
    t.join(timeout=30)

seg_lines = []
seg_err = ""
try:
    from inaSpeechSegmenter import Segmenter
    t1 = time.time()
    segs = Segmenter(vad_engine="smn", detect_gender=False)(wav)
    took = time.time() - t1
    for lab, a, b in segs:
        if b - a >= 1.0:
            seg_lines.append(f"{a:6.1f}-{b:6.1f}s {lab}")
    totals = {}
    for lab, a, b in segs:
        totals[lab] = totals.get(lab, 0) + (b - a)
    seg_summary = ", ".join(f"{k}: {v:.0f}s" for k, v in sorted(totals.items())) + f" | czas analizy {took:.1f}s"
except Exception as e:  # noqa: BLE001
    seg_err = repr(e)
    seg_summary = "BLAD: " + seg_err

# whisper text per speech segment (short check that 'speech' really is talk)
whisper_lines = []
try:
    from faster_whisper import WhisperModel
    import numpy as np
    import wave
    with wave.open(wav) as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    model = WhisperModel("small", device="cpu", compute_type="int8")
    for line in seg_lines:
        a, b = (float(x) for x in re.findall(r"[\d.]+", line)[:2])
        if b - a < 2:
            continue
        segs_w, _ = model.transcribe(audio[int(a * 16000):int(b * 16000)], language="pl",
                                     beam_size=1, without_timestamps=True)
        txt = " ".join(s.text.strip() for s in segs_w)[:120]
        whisper_lines.append(f"{line} | {txt}")
except Exception as e:  # noqa: BLE001
    whisper_lines.append("whisper error " + repr(e))

# ------------------------------------------------------------------ report
icy_txt = []
for url, rep in icy_report.items():
    name = url.split("/")[-1]
    if "error" in rep:
        icy_txt.append(f"{name}: blad {rep['error']}")
    elif not rep.get("metaint"):
        icy_txt.append(f"{name}: brak metadanych (naglowki: {rep['headers']})")
    else:
        t = "; ".join(f"{a} {b!r}" for a, b in rep["titles"]) or "metaint jest, ale brak tytulow"
        icy_txt.append(f"{name}: {t}")

print("\n".join(["", "#### SEGMENTY + WHISPER ####", *whisper_lines]), flush=True)
notify("Probe 1: metadane strumienia", "\n".join(icy_txt))
notify("Probe 2: mowa/muzyka", seg_summary + "\n\n" + "\n".join(whisper_lines)[:3500])
