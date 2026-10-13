#!/usr/bin/env python3
"""
Zolta Pinezka RMF FM - listener.

Listens to the RMF FM live stream, transcribes Polish speech with faster-whisper
(CPU), and pushes ntfy.sh notifications when:
  * the start signal is heard ("Zolta Pinezka RMF - 3,2,1 start!")  -> you have 30 min
  * the word "haslo" (password) is heard                            -> transcript + audio clip

Per the contest rules (regulamin): start signal 14:00-14:30, password aired
15:00-17:00, phone calls 15:00-18:00 (all Polish time). So the listener runs
roughly 13:50-17:05 Polish time.

Usage:
  python pinezka.py              # normal run (waits for the window, then listens)
  python pinezka.py --test 5     # listen right now for 5 minutes, send sample transcript
  python pinezka.py --selftest   # offline check of keyword detection (no network/model)

Config via environment variables (see CONFIG below). NTFY_TOPIC is required.
"""

from __future__ import annotations

import argparse
import base64
import collections
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.request
from zoneinfo import ZoneInfo

PL = ZoneInfo("Europe/Warsaw")
UK = ZoneInfo("Europe/London")
SR = 16000  # sample rate for Whisper


def env(name: str, default: str) -> str:
    v = os.environ.get(name, "").strip()
    return v if v else default


# ----------------------------------------------------------------- CONFIG
NTFY_SERVER = env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = env("NTFY_TOPIC", "")

# Official RMF FM streams (from rmfon.pl station list). MP3 first, AAC as fallback.
STREAM_URLS = [u.strip() for u in env(
    "STREAM_URLS",
    "https://rs201-krk.rmfstream.pl/rmf_fm,"
    "https://rs203-krk.rmfstream.pl/rmf_fm,"
    "https://rs103-krk.rmfstream.pl/rmf_fm,"
    "https://rs202-krk-cyfronet.rmfstream.pl/rmf_fm,"
    "https://rs101-krk.rmfstream.pl/RMFFM48,"
    "https://rs102-krk.rmfstream.pl/RMFFM48",
).split(",") if u.strip()]

MODEL = env("WHISPER_MODEL", "small")          # small = good Polish, ~real-time x3 on 4 vCPU
COMPUTE_TYPE = env("WHISPER_COMPUTE", "int8")
CPU_THREADS = int(env("WHISPER_THREADS", str(os.cpu_count() or 4)))

START_PL = env("START_PL", "13:50")            # start listening (Polish time)
END_PL = env("END_PL", "17:05")                # stop listening (password window ends 17:00)
CONTEST_END = env("CONTEST_END", "2026-10-23")  # last contest day (regulamin)

CHUNK_SEC = float(env("CHUNK_SEC", "15"))      # seconds of new audio per transcription
OVERLAP_SEC = float(env("OVERLAP_SEC", "3"))   # context carried from the previous chunk
MAX_LAG_SEC = float(env("MAX_LAG_SEC", "90"))  # if we fall further behind, skip ahead
RING_SEC = 240                                  # audio kept in memory (for clips)
FOLLOW_SEC = float(env("FOLLOW_SEC", "120"))   # keep sending transcript after a hit
CLIP_BEFORE = 20
CLIP_AFTER = 50
HEARTBEAT_MIN = float(env("HEARTBEAT_MIN", "30"))
META_ON = env("META", "1") == "1"                    # use ICY metadata (song / ad labels)
META_WINDOW_SEC = float(env("META_WINDOW_SEC", "60"))  # +/- window: metadata vs audio offset

PAGE_WATCH = env("PAGE_WATCH", "1") == "1"
PAGE_URL = env("PAGE_URL", "https://www.rmf.fm/r/zolta-pinezka.html")

OUT_DIR = env("OUT_DIR", "out")
HOTWORDS = "Żółta Pinezka RMF FM, hasło, trzy dwa jeden start"


# ----------------------------------------------------------------- helpers
def now_pl() -> dt.datetime:
    return dt.datetime.now(PL)


def fmt_both(t: dt.datetime) -> str:
    return f"{t.astimezone(PL):%H:%M} PL / {t.astimezone(UK):%H:%M} UK"


def log(msg: str) -> None:
    print(f"[{now_pl():%H:%M:%S} PL] {msg}", flush=True)


def at_pl(hhmm: str, day: dt.datetime) -> dt.datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


_PL_MAP = str.maketrans({"ł": "l", "Ł": "l"})


def normalize(text: str) -> str:
    """lowercase, strip Polish diacritics and punctuation, words -> single spaces."""
    t = text.translate(_PL_MAP).lower()
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# ----------------------------------------------------------------- ntfy
def _rfc2047(s: str) -> str:
    try:
        s.encode("ascii")
        return s
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(s.encode()).decode() + "?="


def notify(message: str, title: str = "Pinezka", priority: int = 3,
           tags: list[str] | None = None) -> None:
    log(f"NTFY p{priority} [{title}] {message[:200]!r}")
    if not NTFY_TOPIC:
        return
    body = json.dumps({
        "topic": NTFY_TOPIC, "title": title, "message": message[:3900],
        "priority": priority, "tags": tags or [],
    }).encode()
    req = urllib.request.Request(NTFY_SERVER, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            urllib.request.urlopen(req, timeout=15).read()
            return
        except Exception as e:  # noqa: BLE001
            log(f"ntfy error ({attempt + 1}/3): {e}")
            time.sleep(2)


def notify_file(data: bytes, filename: str, title: str, message: str,
                priority: int = 4) -> None:
    log(f"NTFY attachment {filename} ({len(data)} B)")
    if not NTFY_TOPIC:
        return
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}", data=data, method="PUT",
        headers={"Filename": filename, "Title": _rfc2047(title),
                 "Message": _rfc2047(message), "Priority": str(priority),
                 "Tags": "headphones"})
    try:
        urllib.request.urlopen(req, timeout=60).read()
    except Exception as e:  # noqa: BLE001
        log(f"ntfy attachment error: {e}")
        notify(message + "\n(nie udało się wysłać nagrania)", title, priority)


# ----------------------------------------------------------------- audio
class StreamReader(threading.Thread):
    """Runs ffmpeg on the stream, keeps the last RING_SEC of 16 kHz mono PCM."""

    def __init__(self, urls: list[str]):
        super().__init__(daemon=True)
        self.urls = urls
        self.lock = threading.Lock()
        self.buf = bytearray()
        self.buf_start = 0          # absolute sample index of buf[0]
        self.total = 0              # absolute samples received
        self.last_data = time.time()
        self.stop = False
        self.current_url = ""
        self.proc: subprocess.Popen | None = None

    def run(self) -> None:
        i = 0
        while not self.stop:
            url = self.urls[i % len(self.urls)]
            i += 1
            self.current_url = url
            log(f"stream: connecting {url}")
            cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                   "-user_agent", "Mozilla/5.0",
                   "-reconnect", "1", "-reconnect_streamed", "1",
                   "-reconnect_delay_max", "10", "-rw_timeout", "20000000",
                   "-i", url, "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
            except FileNotFoundError:
                log("ffmpeg not found - install ffmpeg")
                sys.exit(2)
            leftover = b""
            got_any = False
            while not self.stop:
                data = self.proc.stdout.read(3200)  # 0.1 s
                if not data:
                    break
                data = leftover + data
                cut = len(data) - (len(data) % 2)
                data, leftover = data[:cut], data[cut:]
                got_any = True
                with self.lock:
                    self.buf.extend(data)
                    self.total += len(data) // 2
                    excess = len(self.buf) - RING_SEC * SR * 2
                    if excess > 0:
                        del self.buf[:excess]
                        self.buf_start += excess // 2
                    self.last_data = time.time()
            if self.proc.poll() is None:
                self.proc.kill()
            log(f"stream: ended ({url}), got_data={got_any}; retrying")
            time.sleep(2 if got_any else 5)

    def get(self, start: int, end: int):
        import numpy as np
        with self.lock:
            start = max(start, self.buf_start)
            end = min(end, self.total)
            if end <= start:
                return np.zeros(0, dtype=np.float32)
            a = (start - self.buf_start) * 2
            b = (end - self.buf_start) * 2
            raw = bytes(self.buf[a:b])
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    def get_raw(self, start: int, end: int) -> bytes:
        with self.lock:
            start = max(start, self.buf_start)
            end = min(end, self.total)
            if end <= start:
                return b""
            return bytes(self.buf[(start - self.buf_start) * 2:(end - self.buf_start) * 2])


class MetaWatcher(threading.Thread):
    """Reads ICY metadata (StreamTitle) on a separate connection.

    Verified on 13 Oct 2026: RMF FM sends song titles ('Artist - Title'), 'Reklama',
    'STOP_AD_BREAK' and 'RMF FM - FAKTY', updated live. Metadata and audio can be offset
    by tens of seconds, so callers look at a window, not a single moment.
    """

    def __init__(self, urls: list[str]):
        super().__init__(daemon=True)
        self.urls = urls
        self.lock = threading.Lock()
        self.changes: list[tuple[dt.datetime, str]] = []   # (time, title)
        self.stop = False

    def run(self) -> None:
        i = 0
        while not self.stop:
            url = self.urls[i % len(self.urls)]
            i += 1
            try:
                req = urllib.request.Request(url, headers={"Icy-MetaData": "1",
                                                           "User-Agent": "Mozilla/5.0"})
                r = urllib.request.urlopen(req, timeout=20)
                metaint = int(r.headers.get("icy-metaint", "0") or 0)
                if not metaint:
                    log(f"meta: no icy-metaint on {url}")
                    time.sleep(30)
                    continue
                while not self.stop:
                    r.read(metaint)
                    n = r.read(1)
                    if not n:
                        break
                    if n[0]:
                        meta = r.read(n[0] * 16).rstrip(b"\0").decode("utf-8", "replace")
                        m = re.search(r"StreamTitle='(.*?)';", meta)
                        if m:
                            self._add(m.group(1).strip())
            except Exception as e:  # noqa: BLE001
                log(f"meta: {e}")
            time.sleep(5)

    def _add(self, title: str) -> None:
        with self.lock:
            if not self.changes or self.changes[-1][1] != title:
                self.changes.append((now_pl(), title))
                log(f"meta: {title!r}")

    def current(self) -> str:
        with self.lock:
            return self.changes[-1][1] if self.changes else ""

    def titles_between(self, a: dt.datetime, b: dt.datetime) -> list[str]:
        """All titles that were active at any point in [a, b]."""
        with self.lock:
            ch = list(self.changes)
        out = [title for (t, title), nxt in zip(ch, ch[1:] + [(None, None)])
               if t <= b and (nxt[0] is None or nxt[0] >= a)]
        return out

    def context(self, t: dt.datetime) -> tuple[str, bool]:
        titles = self.titles_between(t - dt.timedelta(seconds=META_WINDOW_SEC),
                                     t + dt.timedelta(seconds=META_WINDOW_SEC))
        if not titles:
            return "", False
        label = " → ".join(titles[-3:])
        return label, all(meta_kind(x) in ("ad", "song") for x in titles)


def meta_kind(title: str) -> str:
    low = normalize(title)
    if not low:
        return "unknown"
    if low.startswith("reklama"):
        return "ad"
    if low.startswith("rmf fm") or "stop ad" in low:
        return "station"          # FAKTY, idents, end of ad break -> hosts may talk
    if " - " in title:
        return "song"
    return "unknown"


def pcm_to_mp3(raw: bytes) -> tuple[bytes, str]:
    try:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                            "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "-",
                            "-b:a", "48k", "-f", "mp3", "-"],
                           input=raw, capture_output=True, timeout=60)
        if p.returncode == 0 and p.stdout:
            return p.stdout, "mp3"
    except Exception as e:  # noqa: BLE001
        log(f"mp3 encode failed: {e}")
    import io
    import wave
    bio = io.BytesIO()
    with wave.open(bio, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(raw)
    return bio.getvalue(), "wav"


# ----------------------------------------------------------------- detection
SIGNAL_STRONG = [
    re.compile(r"pinez\w*.{0,40}\bstart"),
    re.compile(r"\b(3|trzy) (2|dwa) (1|jeden) start"),
    re.compile(r"\b(1|jeden|raz) (2|dwa) (3|trzy) pinez"),   # wording used in the regulamin
]
SIGNAL_WEAK = re.compile(r"pinez")
PASSWORD = re.compile(r"\bhas(l|el)\w*")   # haslo, hasla, hasle (hasło/hasła/haśle)


class Detector:
    """Turns transcript chunks into notification decisions."""

    def __init__(self, sender=notify, context=None):
        self.send = sender
        # context(t) -> (label, quiet): what the stream metadata says was on air around t.
        # quiet=True only when the WHOLE window around t was an ad or a song.
        self.context = context or (lambda t: ("", False))
        self.history: collections.deque[tuple[dt.datetime, str]] = collections.deque(maxlen=4)
        self.signal_sent_at: dt.datetime | None = None
        self.weak_sent_at: dt.datetime | None = None
        self.pass_sent_at: dt.datetime | None = None
        self.follow_until: dt.datetime | None = None
        self.follow_kind = ""
        self.events: list[str] = []

    def process(self, t: dt.datetime, text: str) -> list[str]:
        """Returns list of event kinds triggered (for tests / clip scheduling)."""
        kinds: list[str] = []
        prev = " ".join(x for _, x in self.history)
        norm_now = normalize(text)
        norm_ctx = normalize(prev[-200:] + " " + text)
        self.history.append((t, text))
        if not norm_now:
            return kinds

        # 1) follow-up snippets after a hit
        if self.follow_until and t <= self.follow_until:
            self.send(text, f"Ciąg dalszy ({self.follow_kind}) {t:%H:%M:%S}", 3, ["memo"])

        # 2) start signal
        strong = any(p.search(norm_ctx) for p in SIGNAL_STRONG) and (
            SIGNAL_WEAK.search(norm_now) or re.search(r"\bstart", norm_now))
        if strong and (self.signal_sent_at is None or (t - self.signal_sent_at).total_seconds() > 600):
            self.signal_sent_at = t
            deadline = t + dt.timedelta(minutes=30)
            self.send(
                f"SYGNAŁ ok. {fmt_both(t)}. Wyślij zdjęcie do {fmt_both(deadline)}!\n"
                f"Apka RMF FM lub rmf.fm/r/zolta-pinezka.html\n\n„{(prev[-300:] + ' ' + text).strip()}”",
                "ŻÓŁTA PINEZKA - START!", 5, ["rotating_light", "pushpin"])
            self.follow_until = t + dt.timedelta(seconds=FOLLOW_SEC)
            self.follow_kind = "sygnał"
            kinds.append("signal")
        elif SIGNAL_WEAK.search(norm_now) and not strong and (
                self.weak_sent_at is None or (t - self.weak_sent_at).total_seconds() > 300) and (
                self.signal_sent_at is None or (t - self.signal_sent_at).total_seconds() > 600):
            self.weak_sent_at = t
            self.send(f"{fmt_both(t)}: „{text}”", "Wspomniano Pinezkę (może zaraz start)", 4,
                      ["pushpin"])
            kinds.append("weak")

        # 3) password
        if PASSWORD.search(norm_now):
            label, quiet = self.context(t)
            note = f"\n(na antenie: {label})" if label else ""
            if quiet:
                # ad or song for the whole window: low-priority note, no clip, no follow-ups
                self.send(f"{fmt_both(t)}:\n„{text}”{note}",
                          "hasło w reklamie/piosence (raczej nie to)", 2, ["speech_balloon"])
                kinds.append("password_low")
            else:
                if self.pass_sent_at is None or (t - self.pass_sent_at).total_seconds() > 45:
                    self.send(f"{fmt_both(t)}:\n„{(prev[-300:] + ' ' + text).strip()}”{note}",
                              "HASŁO? - sprawdź!", 5, ["key"])
                    kinds.append("password")
                self.pass_sent_at = t
                self.follow_until = t + dt.timedelta(seconds=FOLLOW_SEC)
                self.follow_kind = "hasło"
        self.events.extend(kinds)
        return kinds


# ----------------------------------------------------------------- page watch (experimental)
def page_watch(stop_at: dt.datetime) -> None:
    """Cheap backup detector: watches the contest page between 13:55 and 14:40 PL and
    notifies when its open/closed state seems to change. Experimental: the page may be
    rendered by JavaScript, in which case this simply never fires."""
    day = now_pl()
    begin, end = at_pl("13:55", day), min(at_pl("14:40", day), stop_at)
    last_state = None
    while now_pl() < end:
        if now_pl() < begin:
            time.sleep(20)
            continue
        try:
            req = urllib.request.Request(PAGE_URL, headers={"User-Agent": "Mozilla/5.0"})
            html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
            low = normalize(html)
            state = ("zakonczon" in low, "<form" in html.lower(), "wyslij" in low)
            if last_state is not None and state != last_state:
                notify(f"Stan strony konkursu zmienił się {fmt_both(now_pl())} "
                       f"(zakończona={state[0]}, formularz={state[1]}). Sprawdź rmf.fm!",
                       "Strona Pinezki się zmieniła", 4, ["globe_with_meridians"])
            last_state = state
        except Exception as e:  # noqa: BLE001
            log(f"page watch: {e}")
        time.sleep(20)


# ----------------------------------------------------------------- main loop
def listen(until: dt.datetime, test: bool = False) -> None:
    from faster_whisper import WhisperModel

    os.makedirs(OUT_DIR, exist_ok=True)
    tr_path = os.path.join(OUT_DIR, f"transcript-{now_pl():%Y-%m-%d}.txt")
    log(f"loading model {MODEL} ({COMPUTE_TYPE}, {CPU_THREADS} threads)")
    model = WhisperModel(MODEL, device="cpu", compute_type=COMPUTE_TYPE, cpu_threads=CPU_THREADS)

    reader = StreamReader(STREAM_URLS)
    reader.start()
    meta = MetaWatcher(STREAM_URLS) if META_ON else None
    if meta:
        meta.start()
    if PAGE_WATCH and not test:
        threading.Thread(target=page_watch, args=(until,), daemon=True).start()

    # wait for audio
    t0 = time.time()
    while reader.total < SR * 2:
        if time.time() - t0 > 120:
            notify("Brak dźwięku ze streamu RMF FM przez 2 min. Sprawdź logi Actions.",
                   "Pinezka: BŁĄD streamu", 5, ["warning"])
            t0 = time.time()
        time.sleep(0.5)

    time.sleep(3)
    now_on = f" Teraz na antenie: {meta.current()}." if meta and meta.current() else ""
    notify(f"Słucham RMF FM ({reader.current_url}) do {fmt_both(until)}. Model: {MODEL}.{now_on}",
           "Pinezka: nasłuch start" + (" (TEST)" if test else ""), 2, ["ear"])

    det = Detector(context=meta.context if meta else None)
    last_meta = ""
    pos = reader.total
    chunk = int(CHUNK_SEC * SR)
    overlap = int(OVERLAP_SEC * SR)
    pending_clips: list[tuple[int, int, dt.datetime]] = []
    last_beat = time.time()
    stall_warned = False
    rtfs: list[float] = []
    samples_sent = 0

    with open(tr_path, "a", encoding="utf-8") as tr:
        while now_pl() < until:
            # stream health
            if time.time() - reader.last_data > 90 and not stall_warned:
                notify("Stream milczy >90 s, próbuję ponownie...", "Pinezka: przerwa streamu",
                       4, ["warning"])
                stall_warned = True
            if time.time() - reader.last_data < 5:
                stall_warned = False

            if reader.total < pos + chunk:
                time.sleep(0.3)
                continue
            lag = reader.total - (pos + chunk)
            if lag > MAX_LAG_SEC * SR:
                log(f"lagging {lag / SR:.0f}s behind - skipping ahead")
                pos = reader.total - chunk
            end = pos + chunk
            # wall-clock time of the chunk start (approx.)
            chunk_time = now_pl() - dt.timedelta(seconds=(reader.total - pos) / SR)
            audio = reader.get(pos - overlap, end)
            t1 = time.time()
            segs, _ = model.transcribe(
                audio, language="pl", beam_size=1, vad_filter=True,
                condition_on_previous_text=False, without_timestamps=True,
                hotwords=HOTWORDS)
            text = " ".join(s.text.strip() for s in segs if s.no_speech_prob < 0.8).strip()
            took = time.time() - t1
            rtfs.append(took / (len(audio) / SR))
            cur = meta.current() if meta else ""
            if cur and cur != last_meta:
                tr.write(f"[{now_pl():%H:%M:%S}] ### na antenie: {cur}\n")
                last_meta = cur
            tr.write(f"[{chunk_time:%H:%M:%S}] {text}\n")
            tr.flush()
            log(f"({took:4.1f}s, lag {lag / SR:4.0f}s) {text}")

            kinds = det.process(chunk_time, text)
            if "password" in kinds or "signal" in kinds:
                pending_clips.append((end - CLIP_BEFORE * SR - chunk, end + CLIP_AFTER * SR,
                                      chunk_time))
            for c in list(pending_clips):
                if reader.total >= c[1]:
                    raw = reader.get_raw(c[0], c[1])
                    data, ext = pcm_to_mp3(raw)
                    notify_file(data, f"rmf-{c[2]:%H%M%S}.{ext}",
                                f"Nagranie {c[2]:%H:%M:%S}",
                                "Posluchaj dokladnej formy hasla (odmiana sie liczy!)", 4)
                    pending_clips.remove(c)

            if test and text and samples_sent < 2:
                notify(f"„{text}”", "Pinezka TEST: transkrypcja działa", 3, ["white_check_mark"])
                samples_sent += 1
            if not test and time.time() - last_beat > HEARTBEAT_MIN * 60:
                notify(f"Nadal słucham. Ostatnio: „{text[-200:]}”", "Pinezka: OK", 1)
                last_beat = time.time()
            pos = end

    reader.stop = True
    if meta:
        meta.stop = True
    if reader.proc and reader.proc.poll() is None:
        reader.proc.kill()
    avg = sum(rtfs) / len(rtfs) if rtfs else 0
    notify(f"Koniec nasłuchu. Zdarzenia: {', '.join(det.events) or 'brak'}. "
           f"Średni czas przetwarzania: {avg:.2f}x czasu audio (musi być < 1).",
           "Pinezka: koniec" + (" (TEST)" if test else ""), 2, ["checkered_flag"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", type=float, default=0, help="listen now for N minutes")
    ap.add_argument("--selftest", action="store_true", help="offline keyword test")
    ap.add_argument("--force", action="store_true", help="ignore weekday/contest-end checks")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not NTFY_TOPIC:
        log("WARNING: NTFY_TOPIC not set - notifications only printed to log")

    if args.test > 0:
        listen(now_pl() + dt.timedelta(minutes=args.test), test=True)
        return

    n = now_pl()
    if not args.force:
        if n.date() > dt.date.fromisoformat(CONTEST_END):
            log(f"contest ended ({CONTEST_END}) - nothing to do")
            return
        if n.weekday() >= 5:
            log("weekend - nothing to do")
            return
    start, end = at_pl(START_PL, n), at_pl(END_PL, n)
    if n >= end:
        log("listening window already over today - exiting (probably a backup run)")
        return
    if n < start:
        wait = (start - n).total_seconds()
        max_early = float(env("MAX_EARLY_MIN", "75"))
        if wait > max_early * 60 and not args.force:
            # In winter (CET) the earlier UTC cron fires ~1.5 h early; the later cron covers it.
            log(f"{wait / 60:.0f} min too early (> {max_early:.0f}) - later scheduled run will listen")
            return
        log(f"sleeping {wait / 60:.0f} min until {START_PL} PL")
        time.sleep(wait)
    listen(end)


def selftest() -> None:
    sent: list[tuple[str, int]] = []
    d = Detector(sender=lambda m, ti, p, tags=None: sent.append((ti, p)))
    base = dt.datetime(2026, 10, 14, 14, 10, tzinfo=PL)
    cases = [
        ("Za chwilę gramy dalej, zostańcie z nami.", []),
        ("Żółta Pinezka RMF, trzy, dwa, jeden, start!", ["signal"]),
        ("Macie 30 minut na zdjęcie.", []),
        ("A teraz uwaga, dzisiejsze hasło to: zielony parasol.", ["password"]),
        ("Hasła nie zdradzimy wcześniej.", []),   # within 45 s cooldown
    ]
    ok = True
    for i, (txt, exp) in enumerate(cases):
        got = d.process(base + dt.timedelta(seconds=15 * i), txt)
        flag = "OK " if got == exp else "BAD"
        ok &= got == exp
        print(f"{flag} {txt!r} -> {got} (expected {exp})")
    d2 = Detector(sender=lambda *a, **k: None)
    got = d2.process(base, "Słuchajcie, 3 2 1 start, Żółta Pinezka!")
    print(("OK " if got == ["signal"] else "BAD"), "digits variant ->", got)
    ok &= got == ["signal"]
    d3 = Detector(sender=lambda *a, **k: None)
    got = d3.process(base, "O 14 startuje Żółta Pinezka, słuchajcie uważnie")
    print(("OK " if got == ["weak"] else "BAD"), "promo mention ->", got)
    ok &= got == ["weak"]
    # metadata context: ad for the whole window -> quiet; hosts nearby -> loud
    mw = MetaWatcher([])
    t0 = base
    mw.changes = [(t0, "Shanguy - La Louze"), (t0 + dt.timedelta(seconds=35), "Reklama"),
                  (t0 + dt.timedelta(seconds=300), "STOP_AD_BREAK"),
                  (t0 + dt.timedelta(seconds=400), "RMF FM - FAKTY")]
    checks = [
        (t0 + dt.timedelta(seconds=150), True),    # deep inside the ad break
        (t0 + dt.timedelta(seconds=280), False),   # ad break ends within the window
        (t0 + dt.timedelta(seconds=500), False),   # news / hosts
        (t0 + dt.timedelta(seconds=10), True),     # song -> ad
    ]
    for when, exp_quiet in checks:
        label, quiet = mw.context(when)
        flag = "OK " if quiet == exp_quiet else "BAD"
        ok &= quiet == exp_quiet
        print(f"{flag} meta at +{(when - t0).seconds}s -> quiet={quiet} ({label})")
    d4 = Detector(sender=lambda *a, **k: None, context=mw.context)
    got = d4.process(t0 + dt.timedelta(seconds=150), "Hasło dnia: taniej w naszym sklepie!")
    print(("OK " if got == ["password_low"] else "BAD"), "password in ad ->", got)
    ok &= got == ["password_low"]
    got = d4.process(t0 + dt.timedelta(seconds=500), "Dzisiejsze hasło brzmi: zielony parasol")
    print(("OK " if got == ["password"] else "BAD"), "password from hosts ->", got)
    ok &= got == ["password"]
    print("notifications:", sent)
    print("SELFTEST", "PASSED" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
