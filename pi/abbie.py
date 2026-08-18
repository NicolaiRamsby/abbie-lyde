#!/usr/bin/env python3
"""Zone motion detection for Abbie's room, with a live monitor page.

One ffmpeg per camera decodes the stream down to a small grayscale frame a few
times per second. Consecutive frames are diffed inside each zone; enough changed
pixels for enough frames in a row counts as a detection, and a detection that
clears the cooldown fires that zone's webhook.

Everything it sees is pushed to a browser page over SSE, so the run can be
watched and judged before any sound is actually played.

  see where the zones sit:   ./abbie.py config.json --preview
  watch live numbers:        ./abbie.py config.json --tune
  observe, never POST:       ./abbie.py config.json --dry-run
  run for real:              ./abbie.py config.json
"""

import argparse
import collections
import getpass
import hashlib
import hmac
import json
import os
import queue
import random
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

GRID_W, GRID_H = 320, 180
FRAME_BYTES = GRID_W * GRID_H
HERE = os.path.dirname(os.path.abspath(__file__))


class EventBus:
    """Fan-out of detection events to every connected browser, plus a backlog
    so a page opened later still shows what happened earlier."""

    def __init__(self, backlog=500, log_dir=None, keep_days=90):
        self.lock = threading.Lock()
        self.subscribers = []
        self.history = []
        self.backlog = backlog
        self.stats = Stats()
        self.config = {}
        self.guard = None
        self.zones = []
        self.log_dir = log_dir
        self.keep_days = keep_days
        self.snapshots = None
        self.last_pruned = None
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

    def _day_path(self, day):
        return os.path.join(self.log_dir, f"{day}.jsonl")

    def _persist(self, event):
        """One JSON object per line, one file per day. Small enough that a
        day is a few hundred kilobytes, and trivial to read back or grep."""
        if not self.log_dir:
            return
        day = event["at"][:10]
        try:
            with open(self._day_path(day), "a") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError as exc:
            print(f"kunne ikke skrive log: {exc}", flush=True)
            return
        if self.last_pruned != day:
            self.last_pruned = day
            self._prune()

    def _prune(self):
        try:
            days = sorted(f[:-6] for f in os.listdir(self.log_dir)
                          if f.endswith(".jsonl"))
        except OSError:
            return
        for old in days[:-self.keep_days] if len(days) > self.keep_days else []:
            try:
                os.remove(self._day_path(old))
            except OSError:
                pass

    def days(self):
        try:
            return sorted((f[:-6] for f in os.listdir(self.log_dir)
                           if f.endswith(".jsonl")), reverse=True)
        except (OSError, TypeError):
            return []

    def read_day(self, day, limit=3000, only=None):
        """Newest last, capped so a very busy day cannot blow up the page.

        A day runs to thousands of detections and only a few dozen sounds, so
        the cap used to cut the morning's played sounds off the top: the page
        said nothing had happened while the counter said thirty. Asking for
        sounds only skips past that, and limit=None reads the whole day, which
        is what rebuilding the counters needs.
        """
        if not self.log_dir or not day.replace("-", "").isdigit():
            return []
        try:
            with open(self._day_path(day)) as fh:
                lines = fh.readlines()
        except OSError:
            return []
        out = []
        for line in lines if limit is None or only else lines[-limit:]:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if only == "sounds" and event.get("type") not in ("trigger",
                                                              "manual"):
                continue
            out.append(event)
        return out if limit is None else out[-limit:]

    def publish(self, event):
        event = dict(event, at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                     ts=time.time())
        self._persist(event)
        with self.lock:
            self.history.append(event)
            del self.history[:-self.backlog]
            targets = list(self.subscribers)
        for q in targets:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass
        return event

    def subscribe(self):
        q = queue.Queue(maxsize=200)
        with self.lock:
            self.subscribers.append(q)
            past = list(self.history)
        return q, past

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)


class Auth:
    """A single shared code, exchanged for a signed cookie.

    HTTP Basic auth cannot be used here: go2rtc's player is loaded as an ES
    module, and module fetches do not carry Basic credentials, so the player
    silently 401s and never registers. Safari on iOS is worst about it. A
    cookie is sent with module fetches, so this works everywhere.

    The code is stored only as a salted hash. Losing it means running
    --set-code again, which is fine for a device on a shelf at home.
    """

    COOKIE = "abbie_auth"
    MAX_AGE = 365 * 24 * 3600

    def __init__(self, path):
        self.path = path
        self.data = None
        self.mtime = None
        self.load()

    def load(self):
        try:
            with open(self.path) as fh:
                self.data = json.load(fh)
            self.mtime = os.path.getmtime(self.path)
        except (OSError, ValueError):
            self.data = None
            self.mtime = None
        return self.data

    def _refresh(self):
        """Pick up a code set while the service was already running, so
        --set-code takes effect without a restart."""
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            if self.data is not None:
                self.data, self.mtime = None, None
            return
        if mtime != self.mtime:
            self.load()

    @property
    def configured(self):
        self._refresh()
        return bool(self.data and self.data.get("hash"))

    def set_code(self, code):
        salt = secrets.token_hex(16)
        self.data = {
            "salt": salt,
            "hash": hashlib.sha256((salt + code).encode()).hexdigest(),
            # Rotating the secret invalidates every existing cookie, which is
            # what you want when the code is changed.
            "secret": secrets.token_hex(32),
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.data, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def check_code(self, code):
        if not self.configured:
            return False
        got = hashlib.sha256((self.data["salt"] + code).encode()).hexdigest()
        return hmac.compare_digest(got, self.data["hash"])

    def issue(self):
        exp = str(int(time.time()) + self.MAX_AGE)
        return f"{exp}.{self._sign(exp)}"

    def valid(self, token):
        if not token or not self.configured:
            return False
        exp, _, sig = token.partition(".")
        if not sig or not exp.isdigit() or int(exp) < time.time():
            return False
        return hmac.compare_digest(sig, self._sign(exp))

    def _sign(self, exp):
        return hmac.new(self.data["secret"].encode(), exp.encode(),
                        hashlib.sha256).hexdigest()

    @staticmethod
    def cookie_from(header):
        for part in (header or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == Auth.COOKIE:
                return v
        return None


LOGIN_PAGE = """<!doctype html>
<html lang="da"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Abbie</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; min-height:100dvh; display:grid; place-items:center;
    background:#0f1115; color:#e6e9ef;
    font:15px/1.5 ui-sans-serif,-apple-system,"Segoe UI",sans-serif; }
  form { background:#171a21; border:1px solid #262b36; border-radius:12px;
    padding:28px; width:min(340px,90vw); }
  h1 { margin:0 0 4px; font-size:19px; }
  p { margin:0 0 20px; color:#8b93a3; font-size:13px; }
  input { width:100%; box-sizing:border-box; font:inherit; font-size:17px;
    padding:12px 14px; border-radius:8px; border:1px solid #2f3541;
    background:#0f1115; color:#e6e9ef; letter-spacing:.06em; }
  input:focus { outline:none; border-color:#4ade80; }
  button { width:100%; margin-top:12px; font:inherit; font-weight:600;
    font-size:15px; padding:12px; border:0; border-radius:8px;
    background:#4ade80; color:#06240f; cursor:pointer; }
  .err { margin:14px 0 0; color:#ff6b5e; font-size:13px; }
</style></head>
<body>
<form method="POST" action="/login">
  <h1>Abbie</h1>
  <p>Indtast koden for at se kameraerne.</p>
  <input type="password" name="code" inputmode="text" autocomplete="current-password"
         autofocus required placeholder="Kode">
  <button type="submit">Log ind</button>
  __ERROR__
</form>
</body></html>
"""


class Stats:
    """Counts that survive a page reload, kept per calendar day.

    The counters reset at midnight, but "last heard" deliberately does not:
    knowing she was last praised yesterday evening is still useful.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.day = time.strftime("%Y-%m-%d")
        self.counts = {}
        self.last = {}
        self.detections = 0

    def _roll(self):
        today = time.strftime("%Y-%m-%d")
        if today != self.day:
            self.day = today
            self.counts.clear()
            self.detections = 0

    def detection(self):
        with self.lock:
            self._roll()
            self.detections += 1

    def trigger(self, sound):
        with self.lock:
            self._roll()
            self.counts[sound] = self.counts.get(sound, 0) + 1
            self.last[sound] = time.strftime("%Y-%m-%dT%H:%M:%S")

    def rebuild(self, events):
        """Recount today from the log on disk.

        The counters used to live only in memory, so any restart, including a
        deploy, silently reset them to zero while the log kept every entry.
        """
        with self.lock:
            self._roll()
            for e in events:
                if e.get("at", "")[:10] != self.day:
                    continue
                if e.get("type") == "detect" and not e.get("suppressed"):
                    self.detections += 1
                elif e.get("type") in ("trigger", "manual") and e.get("sound"):
                    snd = e["sound"]
                    self.counts[snd] = self.counts.get(snd, 0) + 1
                    self.last[snd] = e["at"]

    def snapshot(self):
        with self.lock:
            self._roll()
            return {
                "day": self.day,
                "counts": dict(self.counts),
                "last": dict(self.last),
                "detections": self.detections,
            }


class Guard:
    """Decides whether a trigger is allowed to make a sound.

    Two brakes: the manual pause from the web interface, and the sequence rule
    that keeps a dygtig from arriving without a nej before it. There is
    deliberately no automatic safety valve; there was one, it shut the system
    down for an hour without warning, and it was taken out again.

    The state is written to disk on purpose. Pausing means something is wrong,
    so a restart must never quietly start playing sounds again.
    """

    def __init__(self, path, bus, cfg):
        self.path = path
        self.bus = bus
        self.lock = threading.Lock()
        self.until = None        # None = running, else epoch seconds
        self.reason = None
        self.monitoring = True   # master switch: off means nothing is watched

        # "dygtig": "nej" means a dygtig only counts once the gate has fired
        # since the last one. Praise for settling should follow being told off
        # for standing at the door, not repeat on its own.
        self.sequence = (cfg or {}).get("sequence") or {}
        # Being armed is not enough: the movement has to arrive a while after
        # the telling-off. On 18 August two dygtig fired four seconds after the
        # nej that armed them, with the basket movement starting in the same
        # second as the movement at the gate. One object crossing two zones on
        # the same camera looks exactly like a dog changing her mind, and time
        # is the only thing that separates them. The eleven real ones that day
        # took between eleven and twenty seconds.
        self.sequence_min = (cfg or {}).get("sequence_min_seconds", 8)
        self.armed = {}
        self.armed_at = {}

        self._load()

    def _load(self):
        try:
            with open(self.path) as fh:
                d = json.load(fh)
            self.until = d.get("until")
            self.reason = d.get("reason")
            self.armed = d.get("armed") or {}
            self.armed_at = d.get("armed_at") or {}
            self.monitoring = d.get("monitoring", True)
            if self.until is not None and self.until != "forever" \
                    and self.until < time.time():
                self.until, self.reason = None, None
        except Exception:
            self.until, self.reason = None, None

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"until": self.until, "reason": self.reason,
                           "armed": self.armed, "armed_at": self.armed_at,
                           "monitoring": self.monitoring}, fh)
            os.replace(tmp, self.path)
        except OSError as exc:
            print(f"kunne ikke gemme pause-tilstand: {exc}", flush=True)

    def is_paused(self):
        with self.lock:
            return self._paused_locked()

    def _paused_locked(self):
        if self.until is None:
            return False
        if self.until == "forever":
            return True
        if self.until <= time.time():
            self.until, self.reason = None, None
            self._save()
            return False
        return True

    def pause(self, seconds=None, reason="manuel"):
        with self.lock:
            self.until = "forever" if seconds is None else time.time() + seconds
            self.reason = reason
            self._save()
            snap = self._snapshot_locked()
        self.bus.publish({"type": "pause", "note": self._describe(snap),
                          "paused": True})
        return snap

    def resume(self):
        with self.lock:
            self.until, self.reason = None, None
            self._save()
            snap = self._snapshot_locked()
        self.bus.publish({"type": "pause", "note": "genoptaget", "paused": False})
        return snap

    def set_monitoring(self, active):
        """Master switch. Off means zones are not evaluated at all: no
        detections, no log entries, no sounds. The cameras keep streaming so
        the page still works as a plain viewer."""
        with self.lock:
            self.monitoring = bool(active)
            if self.monitoring:
                # A new session starts unarmed, so coming home and leaving
                # again cannot carry yesterday's nej over into a free dygtig.
                self.armed = {}
                self.armed_at = {}
            self._save()
            snap = self._snapshot_locked()
        self.bus.publish({
            "type": "monitoring", "active": self.monitoring,
            "note": "overvågning aktiveret" if self.monitoring
                    else "overvågning deaktiveret",
        })
        return snap

    def note_sound(self, sound):
        """Record that a sound played, so sequence rules can react to it."""
        with self.lock:
            for dependent, required in self.sequence.items():
                if sound == required:
                    self.armed[dependent] = True
                    self.armed_at[dependent] = time.time()
                elif sound == dependent:
                    self.armed[dependent] = False
            self._save()

    def _sequence_note_locked(self, sound, in_seconds=0):
        """Why this sound may not play yet, or None if the rule is satisfied.

        Not armed until the required sound has actually played, so a fresh
        start, or a restart, cannot open with a dygtig.

        The gap is measured to the moment the sound would land, which is why
        the zone's delay is passed in: checked against the movement alone, an
        eight second minimum would also have blocked the shortest real praise
        of 18 August, where the basket movement came seven and a half seconds
        after the nej and the sound landed at eleven.
        """
        required = self.sequence.get(sound)
        if not required:
            return None
        if not self.armed.get(sound):
            return f"venter på {required} først"
        lands = time.time() + in_seconds - (self.armed_at.get(sound) or 0)
        if lands < self.sequence_min:
            return (f"kun {lands:.0f}s efter {required}, "
                    f"kræver {self.sequence_min:.0f}s")
        return None

    def would_allow(self, sound, in_seconds=0):
        """Same check as allow, without side effects.

        Used the moment motion is seen, not when the sound would play. Without
        it, movement in a basket from before she was told off sat waiting, and
        cashed in the instant a nej armed the rule, which looked exactly like
        praise for nothing. The minimum gap is checked here too, counted
        forward to where the delayed sound would land.
        """
        with self.lock:
            if self._paused_locked():
                return False, "pauset, ingen lyd"
            note = self._sequence_note_locked(sound, in_seconds)
            return (False, note) if note else (True, None)

    def allow(self, sound=None, in_seconds=0):
        """Called for every trigger. Returns (allowed, note)."""
        with self.lock:
            if self._paused_locked():
                return False, "pauset, ingen lyd"
            note = self._sequence_note_locked(sound, in_seconds)
            if note:
                return False, note
        return True, None

    def _describe(self, snap):
        if not snap["paused"]:
            return "genoptaget"
        if snap["until"] is None:
            return "pauset indtil du slår den til igen"
        mins = max(1, int(snap["seconds_left"] / 60))
        return f"pauset i {mins} minutter ({snap['reason']})"

    def _snapshot_locked(self):
        paused = self.until is not None and (
            self.until == "forever" or self.until > time.time())
        left = None
        if paused and self.until != "forever":
            left = max(0, self.until - time.time())
        return {
            "monitoring": self.monitoring,
            "paused": paused,
            "reason": self.reason if paused else None,
            "until": None if (not paused or self.until == "forever")
                     else int(self.until),
            "seconds_left": int(left) if left is not None else None,
        }

    def snapshot(self):
        with self.lock:
            return self._snapshot_locked()


def slug(text):
    """File-name-safe version of a zone or sound name."""
    plain = "".join(c if c.isalnum() else "-" for c in (text or "").lower())
    return "-".join(part for part in plain.split("-") if part) or "x"


def stream_name(url):
    """The go2rtc stream a detector URL points at, e.g. mi360_det."""
    path = urllib.parse.urlparse(url or "").path
    return path.strip("/").split("/")[-1] or None


def snapshot_stream(cam):
    """The full resolution stream sitting next to the detector's substream.

    Built by swapping the last path segment, so
    rtsp://127.0.0.1:8554/mi360_det becomes rtsp://127.0.0.1:8554/mi360 and
    nothing in the config on the Pi has to be touched.
    """
    if cam.get("snapshot_stream"):
        return cam["snapshot_stream"]
    src = cam.get("snapshot_src") or cam.get("src")
    if not src or not cam.get("stream"):
        return cam.get("stream")
    parts = urllib.parse.urlsplit(cam["stream"])
    base = parts.path.rsplit("/", 1)[0]
    return urllib.parse.urlunsplit(parts._replace(path=f"{base}/{src}"))


class FrameGrabber:
    """Keeps the last few seconds of one camera as JPEGs in memory.

    go2rtc can produce a JPEG on request, but it waits for the next keyframe of
    an H265 stream, and measured on the Pi that put the picture two to three
    seconds after the sound: long enough for her to have left the gate again.
    Decoding continuously costs about a quarter of one core per camera, and the
    picture is then already in hand the moment a sound fires.
    """

    SOI, EOI = b"\xff\xd8", b"\xff\xd9"
    MAX_BUFFER = 8_000_000

    def __init__(self, name, stream, fps=2, seconds=10, transport="tcp",
                 quality=4):
        self.name = name
        self.stream = stream
        self.fps = fps
        self.transport = transport
        self.quality = quality
        self.frames = collections.deque(maxlen=max(2, int(fps * seconds)))
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True,
                         name=f"grab:{self.name}").start()
        return self

    def _run(self):
        while not self.stop.is_set():
            try:
                self._read()
            except Exception as exc:
                if self.stop.is_set():
                    return
                print(f"[{self.name}] billedstream tabt: {exc} - nyt forsøg "
                      f"om 5s", flush=True)
            if not self.stop.is_set():
                time.sleep(5)

    def _read(self):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
               "-rtsp_transport", self.transport, "-i", self.stream, "-an",
               "-vf", f"fps={self.fps}", "-f", "image2pipe",
               "-c:v", "mjpeg", "-q:v", str(self.quality), "-"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        buf = b""
        try:
            while not self.stop.is_set():
                chunk = proc.stdout.read(65536)
                if not chunk:
                    err = proc.stderr.read().decode(errors="replace").strip()
                    raise RuntimeError(err or "ffmpeg closed the stream")
                buf += chunk
                if len(buf) > self.MAX_BUFFER:
                    raise RuntimeError("ingen billedgrænse i "
                                       f"{len(buf)} bytes")
                # One JPEG per frame down the pipe. Split on the end marker and
                # keep the tail for the next read.
                while True:
                    end = buf.find(self.EOI)
                    if end < 0:
                        break
                    frame, buf = buf[:end + 2], buf[end + 2:]
                    start = frame.find(self.SOI)
                    if start >= 0:
                        with self.lock:
                            self.frames.append((time.time(), frame[start:]))
        finally:
            proc.kill()
            proc.wait()

    def nearest(self, ts, max_age=None):
        """The frame closest in time to ts, or None.

        Nothing is returned if the closest frame is further away than one frame
        interval plus a second. A stalled grabber would otherwise hand out a
        picture from minutes ago as if it were the moment the sound fired, and
        the whole point is that the picture can be trusted. The caller falls
        back to asking go2rtc, which is late but honest about being current.
        """
        if max_age is None:
            max_age = 1.0 / max(self.fps, 0.1) + 1.0
        with self.lock:
            if not self.frames:
                return None
            when, frame = min(self.frames, key=lambda f: abs(f[0] - ts))
        return frame if abs(when - ts) <= max_age else None


def snapshot_sources(cam):
    """Streams to try for a picture, best quality first.

    Full resolution is worth the try: 2304x1296 against the substream's
    640x360, for about half a second more and four times the bytes. The
    substream stays as the fallback, because it is the one ffmpeg already holds
    open and it answers even if the camera refuses another session.
    """
    wanted = [cam.get("snapshot_src"), cam.get("src"),
              stream_name(cam.get("stream"))]
    out = []
    for src in wanted:
        if src and src not in out:
            out.append(src)
    return out


class Snapshots:
    """A JPEG of what the camera saw, every time a sound actually played.

    The percentages in the log cannot answer whether she was really at the
    gate, and that is the question a "nej" that looks wrong raises.
    """

    SAFE = re.compile(r"^\d{4}-\d{2}-\d{2}/[A-Za-z0-9_.-]+\.jpg$")
    MAX_BYTES = 2_000_000

    def __init__(self, root, base_url="http://127.0.0.1:1984", keep_days=90,
                 enabled=True):
        self.root = root
        self.base = (base_url or "").rstrip("/")
        self.keep_days = keep_days
        self.enabled = bool(enabled and root and self.base)
        self.last_pruned = None
        if self.enabled:
            try:
                os.makedirs(root, exist_ok=True)
            except OSError as exc:
                print(f"kan ikke oprette billedmappen: {exc}", flush=True)
                self.enabled = False

    def capture(self, grabber, srcs, zone, sound, ts):
        """Name the picture now, write it in the background.

        The caller is the camera loop, where disk or network work stalls the
        decode and drops frames. Handing back the name up front lets the event
        carry it, and the browser asks for the file after it has landed.
        """
        if not self.enabled or not (grabber or srcs):
            return None
        when = time.localtime(ts)
        stamp = time.strftime("%H%M%S", when) + f"{int(ts % 1 * 1000):03d}"
        rel = (f"{time.strftime('%Y-%m-%d', when)}/"
               f"{stamp}-{slug(zone)}-{slug(sound)}.jpg")
        threading.Thread(target=self._save, args=(grabber, srcs, rel, ts),
                         daemon=True, name="snapshot").start()
        return rel

    def _save(self, grabber, srcs, rel, ts):
        """The buffered frame from the moment it fired. Only if the grabber has
        nothing yet does go2rtc get asked to produce one."""
        data = grabber.nearest(ts) if grabber else None
        if data:
            self._store(rel, data)
            return
        self._fetch(srcs, rel)

    def _fetch(self, srcs, rel):
        data = None
        for src in srcs:
            query = urllib.parse.urlencode({"src": src})
            try:
                with urllib.request.urlopen(
                        f"{self.base}/api/frame.jpeg?{query}",
                        timeout=8) as resp:
                    data = resp.read(self.MAX_BYTES)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                print(f"billede {rel} fra {src} fejlede: {exc}", flush=True)
                continue
            if data:
                break
            print(f"billede {rel} fra {src} kom tomt tilbage", flush=True)
        if data:
            self._store(rel, data)

    def _store(self, rel, data):
        path = os.path.join(self.root, rel)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except OSError as exc:
            print(f"kunne ikke gemme {rel}: {exc}", flush=True)
            return
        day = rel[:10]
        if self.last_pruned != day:
            self.last_pruned = day
            self.prune()

    def prune(self):
        """One directory per day, so pictures expire together with their day."""
        try:
            days = sorted(d for d in os.listdir(self.root)
                          if len(d) == 10 and d.replace("-", "").isdigit())
        except OSError:
            return
        for old in days[:-self.keep_days] if len(days) > self.keep_days else []:
            shutil.rmtree(os.path.join(self.root, old), ignore_errors=True)

    def path(self, rel):
        """Absolute path for a reference out of the log, or None.

        The pattern is the whole defence against a request walking out of the
        directory, so it has to stay strict.
        """
        if not self.enabled or not rel or not self.SAFE.match(rel):
            return None
        full = os.path.join(self.root, rel)
        return full if os.path.isfile(full) else None


class Zone:
    def __init__(self, cfg, camera, bus, dry_run, snap_srcs=(),
                 grabber=None):
        self.name = cfg["name"]
        self.camera = camera
        self.bus = bus
        self.dry_run = dry_run
        self.snap_srcs = list(snap_srcs)
        self.grabber = grabber

        self.set_rect(cfg["rect"])

        self.threshold_pct = cfg.get("threshold_pct", 3.0)
        self.pixel_delta = cfg.get("pixel_delta", 22)
        self.consecutive = cfg.get("consecutive", 2)
        self.cooldown = cfg.get("cooldown", 60)
        # A dog changes one part of the picture. The camera's auto-exposure
        # changes all of it, and that used to read as motion in every zone at
        # once. A real detection has to stand out from the rest of the frame.
        self.local_ratio = cfg.get("local_ratio", 2.0)
        self.delay = cfg.get("delay", 0)
        self.sound = cfg.get("sound", self.name)
        # A zone may name its own webhook; otherwise it uses the one for its
        # sound, which is where the Pushover endpoints live.
        self.webhook = cfg.get("webhook") or webhook_for(bus.config, self.sound)

        self.hits = 0
        self.last_fired = 0.0
        self.pending = False

    def set_rect(self, rect):
        x, y, w, h = rect  # normalised 0-1
        x0, y0 = max(0, int(x * GRID_W)), max(0, int(y * GRID_H))
        x1 = min(GRID_W, int((x + w) * GRID_W))
        y1 = min(GRID_H, int((y + h) * GRID_H))
        if x1 <= x0 or y1 <= y0:
            raise ValueError(f"zone {self.name!r} is empty")
        self.rect = list(rect)
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1

    @property
    def pixels(self):
        return (self.x1 - self.x0) * (self.y1 - self.y0)

    def measure(self, diff):
        window = diff[self.y0:self.y1, self.x0:self.x1]
        return np.count_nonzero(window > self.pixel_delta) / self.pixels * 100.0

    def background(self, diff):
        """How much of the whole frame changed, by this zone's own standard."""
        return np.count_nonzero(diff > self.pixel_delta) / diff.size * 100.0

    def feed(self, pct, now, bg=0.0):
        if pct < self.threshold_pct:
            self.hits = 0
            return

        if bg > 0 and pct < bg * self.local_ratio:
            # The whole frame moved about as much as this zone did, so this is
            # the camera adjusting, not something in the zone.
            self.hits = 0
            self.bus.publish({
                "type": "detect", "zone": self.name, "camera": self.camera,
                "pct": round(pct, 2), "bg": round(bg, 2), "sound": self.sound,
                "suppressed": True, "note": "global lysændring, ikke bevægelse",
            })
            return
        self.hits += 1
        if self.hits < self.consecutive:
            return
        self.hits = 0

        cooling = now - self.last_fired < self.cooldown
        if not cooling and not self.pending:
            self.bus.stats.detection()
        self.bus.publish({
            "type": "detect", "zone": self.name, "camera": self.camera,
            "pct": round(pct, 2), "bg": round(bg, 2), "sound": self.sound,
            "suppressed": cooling,
            "note": "i cooldown, ingen lyd" if cooling else None,
        })
        if cooling or self.pending:
            return

        # The delay is part of the question: the rule is about when the sound
        # lands, not when the movement was seen.
        ok, why = self.bus.guard.would_allow(self.sound, self.delay)
        if not ok:
            self.bus.publish({
                "type": "blocked", "zone": self.name, "camera": self.camera,
                "pct": round(pct, 2), "sound": self.sound, "note": why,
            })
            print(f"{time.strftime('%H:%M:%S')}  {self.name}  {pct:.1f}%  "
                  f"-> {self.sound}  [{why}]", flush=True)
            return
        # The cooldown is claimed in _fire, and only when a sound actually
        # plays. Claiming it here meant a trigger blocked by the sequence rule
        # still locked the zone for two minutes, so the praise she had earned
        # a moment later was swallowed.
        self.pending = True

        # The delay is what makes "dygtig" land after she has settled rather
        # than the instant she steps into the basket.
        if self.delay:
            self.bus.publish({
                "type": "pending", "zone": self.name, "camera": self.camera,
                "pct": round(pct, 2), "sound": self.sound,
                "note": f"afventer {self.delay}s",
            })
            threading.Timer(self.delay, self._fire, args=(pct,)).start()
        else:
            self._fire(pct)

    def _fire(self, pct):
        self.pending = False
        allowed, why = self.bus.guard.allow(self.sound)
        if not allowed:
            # Still logged, so the interface shows what would have happened
            # while paused. That is the record needed to judge whether it is
            # safe to switch the sounds back on.
            self.bus.publish({
                "type": "blocked", "zone": self.name, "camera": self.camera,
                "pct": round(pct, 2), "sound": self.sound, "note": why,
            })
            print(f"{time.strftime('%H:%M:%S')}  {self.name}  {pct:.1f}%  "
                  f"-> {self.sound}  [{why}]", flush=True)
            return

        self.last_fired = time.time()
        # Ask for the picture before the Pushover call. That request can sit
        # for seconds, and by then she has moved on.
        snap = (self.bus.snapshots.capture(self.grabber, self.snap_srcs,
                                           self.name, self.sound,
                                           self.last_fired)
                if self.bus.snapshots else None)
        self.bus.stats.trigger(self.sound)
        self.bus.guard.note_sound(self.sound)
        note = send_sound(self.bus.config, self.sound, "auto", self.dry_run)
        self.bus.publish({
            "type": "trigger", "zone": self.name, "camera": self.camera,
            "pct": round(pct, 2), "sound": self.sound,
            "dry_run": self.dry_run, "note": note, "snap": snap,
        })
        print(f"{time.strftime('%H:%M:%S')}  {self.name}  {pct:.1f}%  "
              f"-> {self.sound}  {note}", flush=True)


PUSHOVER_API = "https://api.pushover.net/1/messages.json"


def save_zones(cfg, bus, config_path, wanted):
    """Apply edited rectangles to the running zones and to config.json.

    Updating the live Zone objects rather than restarting means an edit does
    not drop the camera connections, which take seconds to re-establish.
    """
    if not isinstance(wanted, list) or not wanted:
        raise ValueError("ingen zoner angivet")

    by_key = {(z.camera, z.name): z for z in bus.zones}
    updates = []
    for item in wanted:
        key = (item["camera"], item["name"])
        if key not in by_key:
            raise KeyError(f"ukendt zone {key}")
        rect = [float(v) for v in item["rect"]]
        if len(rect) != 4 or rect[2] <= 0 or rect[3] <= 0:
            raise ValueError(f"ugyldigt rektangel for {key}")
        x, y, w, h = rect
        # Each value in range is not enough: the box also has to end inside
        # the frame, or the zone silently gets clamped to something else.
        if not (0 <= x and 0 <= y and x + w <= 1.0001 and y + h <= 1.0001):
            raise ValueError(f"rektangel uden for billedet for {key}")
        updates.append((by_key[key], rect))

    for zone, rect in updates:
        zone.set_rect(rect)

    for cam in cfg["cameras"]:
        for z in cam["zones"]:
            for zone, rect in updates:
                if zone.camera == cam.get("name") and zone.name == z["name"]:
                    z["rect"] = [round(v, 4) for v in rect]

    stored = {k: v for k, v in cfg.items() if not k.startswith("_")}
    tmp = config_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(stored, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, config_path)
    return len(updates)


def webhook_for(cfg, sound):
    """Per-sound webhook, used as the fallback for any zone that does not name
    its own. Only relevant when Pushover is not configured."""
    return ((cfg.get("sounds") or {}).get(sound) or {}).get("webhook")


def send_sound(cfg, sound, source, dry_run):
    """Play one sound on the iPad via Pushover. Returns a note for the log.

    A "nej" picks at random from several recordings so it does not become one
    sound she stops reacting to.
    """
    spec = (cfg.get("sounds") or {}).get(sound) or {}
    if dry_run:
        return "ville have afspillet lyd"

    po = cfg.get("pushover") or {}
    if po.get("token") and po.get("user"):
        picked = random.choice(spec.get("pushover_sounds") or [sound])
        fields = {
            "token": po["token"],
            "user": po["user"],
            "message": spec.get("message", sound),
            "sound": picked,
            "priority": spec.get("priority", 0),
        }
        # Without a device the sound plays on every device on the account,
        # including the phone in Nicolai's pocket. The speaker is the iPad.
        device = spec.get("device") or po.get("device")
        if device:
            fields["device"] = device
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(po.get("api", PUSHOVER_API), data=data,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return f"Pushover {resp.status}, lyd {picked}"
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:120].decode(errors="replace")
            return f"Pushover fejl {exc.code}: {detail}"
        except urllib.error.URLError as exc:
            return f"Pushover fejlede: {exc}"

    url = spec.get("webhook")
    if not url:
        return "ingen modtager konfigureret"
    req = urllib.request.Request(
        url,
        data=json.dumps({"sound": sound, "source": source,
                         "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return f"POST {resp.status}"
    except urllib.error.URLError as exc:
        return f"POST fejlede: {exc}"


def ffmpeg_frames(stream, fps, transport):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-rtsp_transport", transport, "-i", stream, "-an",
           "-vf", f"fps={fps},scale={GRID_W}:{GRID_H},format=gray",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        while True:
            buf = proc.stdout.read(FRAME_BYTES)
            if len(buf) < FRAME_BYTES:
                err = proc.stderr.read().decode(errors="replace").strip()
                raise RuntimeError(err or "ffmpeg closed the stream")
            yield np.frombuffer(buf, dtype=np.uint8).reshape(GRID_H, GRID_W)
    finally:
        proc.kill()
        proc.wait()


def watch(cam, fps, transport, tune, dry_run, bus, stop, grabber=None):
    """Run one camera. Reconnects on failure so a blip does not end the run."""
    name = cam.get("name", "cam")
    snap_srcs = snapshot_sources(cam)
    zones = [Zone(z, name, bus, dry_run, snap_srcs, grabber)
             for z in cam["zones"]]
    bus.zones.extend(zones)
    while not stop.is_set():
        prev = None
        try:
            for frame in ffmpeg_frames(cam["stream"], fps, transport):
                if stop.is_set():
                    return
                cur = frame.astype(np.int16)
                if prev is None:
                    prev = cur
                    bus.publish({"type": "status", "camera": name,
                                 "note": "stream forbundet"})
                    continue
                diff = np.abs(cur - prev)
                prev = cur
                now = time.time()

                if not bus.guard.monitoring:
                    continue

                readings = []
                for z in zones:
                    pct = z.measure(diff)
                    bg = z.background(diff)
                    readings.append(f"{z.name}={pct:5.2f}% (bg {bg:4.1f}%)")
                    z.feed(pct, now, bg)
                if tune:
                    print(f"[{name}] " + "  ".join(readings), flush=True)
        except Exception as exc:
            if stop.is_set():
                return
            bus.publish({"type": "status", "camera": name,
                         "note": f"stream tabt: {exc}"})
            print(f"[{name}] stream error: {exc} - reconnecting in 5s",
                  flush=True)
            time.sleep(5)


def make_handler(bus, cfg, auth, args_config):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass  # the detection log is the interesting one

        def _send(self, body, ctype, status=200, headers=()):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, to, headers=()):
            self.send_response(302)
            self.send_header("Location", to)
            self.send_header("Content-Length", "0")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()

        def _authed(self):
            return auth.valid(Auth.cookie_from(self.headers.get("Cookie")))

        def _login_page(self, error=None):
            msg = f'<p class="err">{error}</p>' if error else ""
            body = LOGIN_PAGE.replace("__ERROR__", msg).encode()
            self._send(body, "text/html; charset=utf-8",
                       status=401 if error else 200)

        def do_GET(self):
            path = self.path.split("?")[0]

            # Caddy asks here before letting any request through. A 2xx lets it
            # pass; anything else is copied to the browser, so the redirect
            # below is what sends a stranger to the login page.
            if path == "/auth":
                if not auth.configured or self._authed():
                    self._send(b"", "text/plain", status=200)
                else:
                    self._redirect("/login")
                return

            if path == "/login":
                if self._authed():
                    self._redirect("/")
                else:
                    self._login_page()
                return

            if path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as fh:
                    self._send(fh.read(), "text/html; charset=utf-8")
            elif path == "/config":
                view = {
                    "go2rtc": cfg.get("go2rtc", "http://127.0.0.1:1984"),
                    "dry_run": cfg.get("_dry_run", False),
                    "cameras": [
                        {"name": c.get("name"), "src": c.get("src"),
                         "zones": [{"name": z["name"], "rect": z["rect"],
                                    "sound": z.get("sound", z["name"])}
                                   for z in c["zones"]]}
                        for c in cfg["cameras"]],
                }
                self._send(json.dumps(view).encode(),
                           "application/json; charset=utf-8")
            elif path == "/state":
                state = bus.stats.snapshot()
                state["pause"] = bus.guard.snapshot()
                self._send(json.dumps(state).encode(),
                           "application/json; charset=utf-8")
            elif path in ("/icon.png", "/icon-512.png", "/manifest.json"):
                # Home screen icon and manifest, so iOS treats it as an app.
                fname = os.path.join(HERE, os.path.basename(path))
                ctype = ("application/manifest+json" if path.endswith(".json")
                         else "image/png")
                try:
                    with open(fname, "rb") as fh:
                        self._send(fh.read(), ctype,
                                   headers=[("Cache-Control", "max-age=86400")])
                except OSError:
                    self.send_error(404)
            elif path == "/days":
                self._send(json.dumps(bus.days()).encode(),
                           "application/json; charset=utf-8")
            elif path == "/history":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                day = (q.get("day") or [time.strftime("%Y-%m-%d")])[0]
                only = (q.get("only") or [""])[0] or None
                self._send(
                    json.dumps({"day": day, "only": only,
                                "events": bus.read_day(day, only=only)},
                               ensure_ascii=False).encode(),
                    "application/json; charset=utf-8")
            elif path == "/snap":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                full = (bus.snapshots.path((q.get("f") or [""])[0])
                        if bus.snapshots else None)
                if not full:
                    self.send_error(404)
                    return
                try:
                    with open(full, "rb") as fh:
                        body = fh.read()
                except OSError:
                    self.send_error(404)
                    return
                # The file never changes once written, so let the browser keep
                # it: scrolling back through a day is otherwise a fresh
                # download per picture.
                self._send(body, "image/jpeg",
                           headers=[("Cache-Control",
                                     "max-age=31536000, immutable")])
            elif path == "/events":
                self.stream_events()
            else:
                self.send_error(404)

        def do_POST(self):
            path = self.path.split("?")[0]

            if path == "/login":
                n = int(self.headers.get("Content-Length") or 0)
                form = urllib.parse.parse_qs(self.rfile.read(n).decode())
                code = (form.get("code") or [""])[0]
                if auth.check_code(code):
                    cookie = (f"{Auth.COOKIE}={auth.issue()}; Path=/; "
                              f"Max-Age={Auth.MAX_AGE}; HttpOnly; "
                              f"SameSite=Lax; Secure")
                    self._redirect("/", headers=[("Set-Cookie", cookie)])
                else:
                    time.sleep(1)  # take the edge off guessing
                    self._login_page("Forkert kode.")
                return

            if path == "/zones":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}")
                except (ValueError, json.JSONDecodeError):
                    self.send_error(400, "ugyldig JSON")
                    return
                try:
                    saved = save_zones(cfg, bus, args_config, body.get("zones"))
                except (ValueError, KeyError, OSError) as exc:
                    self.send_error(400, f"kunne ikke gemme zoner: {exc}")
                    return
                bus.publish({"type": "zones", "note": f"{saved} zoner gemt"})
                print(f"{time.strftime('%H:%M:%S')}  zoner gemt ({saved})",
                      flush=True)
                self._send(json.dumps({"saved": saved}).encode(),
                           "application/json; charset=utf-8")
                return

            if path == "/monitoring":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}")
                except (ValueError, json.JSONDecodeError):
                    self.send_error(400, "ugyldig JSON")
                    return
                snap = bus.guard.set_monitoring(body.get("active", True))
                print(f"{time.strftime('%H:%M:%S')}  overvågning "
                      f"{'aktiveret' if snap['monitoring'] else 'deaktiveret'}",
                      flush=True)
                self._send(json.dumps(snap).encode(),
                           "application/json; charset=utf-8")
                return

            if path == "/trigger":
                if not self._authed() and auth.configured:
                    self.send_error(401)
                    return
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}")
                except (ValueError, json.JSONDecodeError):
                    self.send_error(400, "ugyldig JSON")
                    return

                sound = body.get("sound")
                if sound not in (cfg.get("sounds") or {}):
                    self.send_error(400, "ukendt lyd")
                    return

                # Deliberate, so it ignores the pause and does not feed the
                # safety valve. Pausing guards against the detector running
                # away, not against Nicolai pressing a button.
                # Manual presses send for real even in dry-run: dry-run is
                # about not trusting the detector yet, not about muting him.
                note = send_sound(cfg, sound, "manuel", dry_run=False)
                bus.stats.trigger(sound)
                bus.publish({"type": "manual", "sound": sound, "note": note})
                print(f"{time.strftime('%H:%M:%S')}  manuel  -> {sound}  "
                      f"{note}", flush=True)
                self._send(json.dumps({"sound": sound, "note": note}).encode(),
                           "application/json; charset=utf-8")
                return

            if path != "/pause":
                self.send_error(404)
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self.send_error(400, "ugyldig JSON")
                return

            if body.get("resume"):
                snap = bus.guard.resume()
            else:
                mins = body.get("minutes")
                secs = None if mins in (None, 0) else float(mins) * 60
                snap = bus.guard.pause(secs, reason="manuel")
            self._send(json.dumps(snap).encode(),
                       "application/json; charset=utf-8")

        def stream_events(self):
            q, _ = bus.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            # Tells proxies not to sit on the stream waiting for a full buffer.
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            # A quiet room produces no events, and the browser shows
            # "forbinder" until it sees bytes. Send one immediately so the
            # indicator reflects the connection, not the dog's activity.
            try:
                self.wfile.write(b": forbundet\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            try:
                # No replay: the page loads the day from /history, which is
                # the durable record. Replaying here would duplicate it.
                while True:
                    try:
                        self.write_event(q.get(timeout=15))
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                bus.unsubscribe(q)

        def write_event(self, event):
            self.wfile.write(
                f"data: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()

    return Handler


def preview(cfg, outdir):
    os.makedirs(outdir, exist_ok=True)
    for cam in cfg["cameras"]:
        # drawtext needs a freetype-enabled ffmpeg, which is not a given, so the
        # zones are colour-coded by sound instead of labelled.
        boxes = []
        for z in cam["zones"]:
            x, y, w, h = z["rect"]
            colour = "red" if z.get("sound") == "nej" else "lime"
            boxes.append(
                f"drawbox=x=iw*{x}:y=ih*{y}:w=iw*{w}:h=ih*{h}:"
                f"color={colour}@0.9:t=6")
        out = os.path.join(outdir, f"{cam.get('name', 'cam')}.png")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-rtsp_transport", cfg.get("rtsp_transport", "tcp"),
               "-i", cam["stream"], "-vf", ",".join(boxes),
               "-frames:v", "1", out]
        res = subprocess.run(cmd, capture_output=True)
        if res.returncode != 0:
            sys.exit(res.stderr.decode(errors="replace").strip())
        print(f"wrote {out}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--tune", action="store_true",
                    help="print change percentages every frame")
    ap.add_argument("--dry-run", action="store_true",
                    help="log triggers without POSTing")
    ap.add_argument("--preview", nargs="?", const="zones", metavar="DIR",
                    help="write one frame per camera with zones drawn")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--bind", default="127.0.0.1",
                    help="address to serve the monitor on (0.0.0.0 for LAN)")
    ap.add_argument("--set-code", action="store_true",
                    help="set the access code, then exit")
    args = ap.parse_args()

    auth = Auth(os.path.join(HERE, "auth.json"))
    if args.set_code:
        code = getpass.getpass("Ny kode: ")
        if not code or code != getpass.getpass("Gentag koden: "):
            sys.exit("koderne er ikke ens, intet ændret")
        if len(code) < 4:
            sys.exit("koden skal være mindst 4 tegn")
        auth.set_code(code)
        print(f"koden er gemt i {auth.path}. Alle eksisterende logins er "
              f"logget ud.")
        return

    with open(args.config) as fh:
        cfg = json.load(fh)

    if args.preview:
        preview(cfg, args.preview)
        return

    cfg["_dry_run"] = args.dry_run
    bus = EventBus(log_dir=os.path.join(HERE, "log"),
                   keep_days=cfg.get("keep_days", 90))
    # The whole day, not the capped page view: a restart on a busy day used to
    # rebuild the counters from the last few thousand detections alone and lose
    # every sound played before that.
    bus.stats.rebuild(bus.read_day(time.strftime("%Y-%m-%d"), limit=None))
    bus.config = cfg
    bus.guard = Guard(os.path.join(HERE, "pause.json"), bus, cfg)
    snaps = cfg.get("snapshots") or {}
    bus.snapshots = Snapshots(
        root=snaps.get("dir") or os.path.join(HERE, "snapshots"),
        base_url=snaps.get("go2rtc_api") or "http://127.0.0.1:1984",
        keep_days=snaps.get("keep_days", cfg.get("keep_days", 90)),
        enabled=snaps.get("enabled", True))
    fps = cfg.get("fps", 5)

    if bus.guard.is_paused():
        print("OBS: starter PAUSET", flush=True)
    transport = cfg.get("rtsp_transport", "tcp")
    stop = threading.Event()

    for dependent, required in (bus.guard.sequence or {}).items():
        print(f"{dependent} kræver {required} først, og mindst "
              f"{bus.guard.sequence_min}s efter den", flush=True)

    live_snaps = bus.snapshots.enabled and snaps.get("live", True)
    for cam in cfg["cameras"]:
        for z in cam["zones"]:
            print(f"{cam.get('name')}/{z['name']}: >="
                  f"{z.get('threshold_pct', 3.0)}% i "
                  f"{z.get('consecutive', 2)} frames, "
                  f"delay {z.get('delay', 0)}s, "
                  f"cooldown {z.get('cooldown', 60)}s", flush=True)
        grabber = None
        if live_snaps:
            grabber = FrameGrabber(
                cam.get("name", "cam"), snapshot_stream(cam),
                fps=snaps.get("fps", 2),
                seconds=snaps.get("buffer_seconds", 10),
                transport=transport, quality=snaps.get("quality", 4)).start()
            print(f"{cam.get('name')}: billeder fra {grabber.stream} "
                  f"({grabber.fps} fps i hukommelsen)", flush=True)
        threading.Thread(
            target=watch,
            args=(cam, fps, transport, args.tune, args.dry_run, bus, stop,
                  grabber),
            daemon=True).start()

    server = ThreadingHTTPServer((args.bind, args.port),
                                 make_handler(bus, cfg, auth,
                                              args.config))
    server.daemon_threads = True
    mode = "DRY-RUN, ingen lyde afspilles" if args.dry_run else "LIVE"
    if not auth.configured:
        print("ADVARSEL: ingen kode sat, alt er åbent. "
              "Kør med --set-code.", flush=True)
    print(f"\nmonitor: http://{args.bind}:{args.port}   ({mode})\n", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        stop.set()
        server.shutdown()


if __name__ == "__main__":
    main()
