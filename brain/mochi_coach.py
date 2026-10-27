#!/usr/bin/env python3
"""mochi-coach: Mochi sits in on your call and coaches you on the screen, silently.

  mochi-coach start [--engine mochi|live] [--stt assemblyai|gemini] [--monitor N] [--brief TEXT] [--model M]
                    [--every SECS] [--transcript] [--debug]
                                   listen to the call (your microphone = "me", what the speakers play = "them"),
                                   watch the monitor the call is on (the active window's; --monitor N pins one, in
                                   xrandr's order) and put short notes, with highlighted areas, on the screen
                                   through the pig. Never a sound. Ctrl-C or `mochi-coach stop` ends it.
  mochi-coach stop                 end the session
  mochi-coach status               is one running, which keys and sources it would use, where the last log is
  mochi-coach show TEXT [--box X,Y,W,H]... [--secs N]
                                   try the overlay by hand (no models involved)
  mochi-coach clear                take the overlay down

Two engines:
  mochi (the default)  Live speech-to-text (AssemblyAI's streaming models, or Gemini's transcribe-live when there is
                       no AssemblyAI key) turns both sides of the call into text, and the text goes, in chunks every
                       --every seconds (15), into one running Claude Code session in Mochi's own workspace: the normal
                       Mochi, with its memory, senses and read-only tools, told that it is hearing a call. Each chunk
                       comes back as a note for the screen (or nothing), a highlight if it looked at the screen, and
                       lines to remember. A note lands five to ten seconds after a chunk goes.
  live                 Google's Gemini Live API hears the audio and sees the screen itself and answers through a
                       `show` tool within a second or two, with Mochi's notes read once at the start, live lookups
                       through Mochi's senses, Google Search, and `ask_mochi` for a question to Mochi's brain
                       (--context full|light|none, --no-search). Faster, but not Mochi.

The notes land in ~/.local/share/mochi/desk/coach/<stamp>.md: every note it showed and what it was told to remember;
the call's words too only with --transcript (the Claude session keeps its own record in ~/.claude, as any chat).
Keys: ASSEMBLYAI_API_KEY in ~/.config/mochi/assemblyai.env, GEMINI_API_KEY in ~/.config/mochi/gemini.env.
Config (optional), ~/.config/mochi/coach.json: {"mic": "<pulse source>", "speakers": "<pulse sink>.monitor",
"model": "<claude model>", "stt_model": "universal-3-6-pro", "stt_language": "", "budget": 15, "gate": 500,
"every": 15, "live_model": "gemini-3.8-live", "resolution": "low", "window_tokens": 12000, "fps": 1, "name": "Artem"}.
"""
import argparse
import asyncio
import datetime as dt
import importlib.machinery
import importlib.util
import io
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from array import array
from pathlib import Path

CONFIG = Path(os.environ.get("MOCHI_COACH_CONFIG", Path.home() / ".config/mochi/coach.json"))
GEMINI_ENV = Path(os.environ.get("MOCHI_GEMINI_ENV", Path.home() / ".config/mochi/gemini.env"))
ASSEMBLY_ENV = Path(os.environ.get("MOCHI_ASSEMBLYAI_ENV", Path.home() / ".config/mochi/assemblyai.env"))
WORK = Path(os.environ.get("MOCHI_HOME", Path.home() / ".local/share/mochi"))
LOGS = WORK / "desk" / "coach"
FRAME = LOGS / "frame.jpg"   # the latest look at the screen, for Mochi to Read when the call refers to it
MEMORY = WORK / "memory"
TOOLS = Path(os.environ.get("MOCHI_TOOLS", Path.home() / ".local/share/mochi-tools"))
RUN = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
PID_FILE = RUN / "mochi-coach.pid"
PET_SOCK = RUN / "xpet.sock"
BRAIN_SOCK = RUN / "mochi-brain.sock"   # the relay: the live engine's questions for Mochi's brain go there
COACH_SOCK = RUN / "mochi-coach.sock"   # and the answers come back here

DEFAULT_LIVE_MODEL = "gemini-3.8-live"
DEFAULT_STT_MODEL = "universal-3-6-pro"   # AssemblyAI's streaming model (18 languages, code-switching)
GEMINI_STT_MODEL = "gemini-3.5-transcribe-live"
RATE = 16000
CHUNK_MS = 40
CHUNK = RATE * 2 * CHUNK_MS // 1000  # bytes of 16-bit mono per chunk
PRE_ROLL = 8          # chunks sent from before the gate opened (so the first syllable isn't lost)
HANGOVER = 18         # quiet chunks (~0.7 s) that close an utterance
MAX_UTTERANCE = 20.0  # seconds: a monologue gets cut into turns, so notes can come mid-flow
FRAME_MAX_W = 1280
FRAME_EVERY = 5.0     # seconds (live engine): a frame goes even when nothing changed
NOTE_SECS = 12
CHUNK_CHARS = 500     # mochi engine: this much transcript goes at once even before --every seconds are up
MIN_CHUNK_CHARS = 40  # and less than this waits for more, up to three times --every
CONTEXT_FILES = {"full": ["life.md", "open-loops.md", "dossier.md", "preferences.md"],
                 "light": ["life.md", "open-loops.md"], "none": []}
CONTEXT_CAP = 60000
LOOKUP_CAP = 3500
ASK_TIMEOUT = 240
# What the live model says aloud instead of using the tool still reaches the screen, except when it is only saying "nothing"
NOTHING = re.compile(r"nothing (to|new|yet|for)|no (note|comment|advice)|stay(ing)? (silent|quiet)|all good|carry on", re.I)

# ---- the mochi engine's prompt: the normal Mochi, told what this session is -------------------------------------

CALL_PROMPT = """You are sitting in on a call with {name} right now (mochi-coach). Every message from now on is a chunk of a
live transcript, a few seconds to a minute of the call: lines marked `me:` are {name}, `them:` is the other side, in
the order they were said, with the call's clock. Machine transcription: a name or a number may be off; prefer what
your notes say over a garbled word. Each chunk may name a fresh picture of the screen the call is on
({frame}); Read it only when what is being said refers to something on the screen (a document, a figure, a page),
not every time.

You never speak. Your whole reply to a chunk is ONE JSON object on one line, nothing before or after it, no mochi
block, no markdown:
  {{"note": "...", "box": [x, y, w, h], "remember": "..."}}
- note: a short note that appears on their screen for a few seconds, or null. At most 140 characters, lower-case,
  plain words, the useful part first ("the q3 figure on screen is 42k, not 40k"), in the language the call is in.
  Show one only when it genuinely helps right now: a fact, a number, a name or a date they are reaching for (from
  your notes, your senses, the screen, or the web); a question from the other side they have not answered; a point
  they missed; a good question to ask next; a commitment being made, so they can confirm it out loud; a wrong figure
  or an old version on screen; when they have talked a long time. Most chunks need nothing: then "note": null.
  Never narrate or summarise the call, never greet, never comment on yourself.
- box: only with a note about something on the screen picture you Read, [x, y, w, h] in that picture's pixels,
  else null. Never the whole screen.
- remember: a line for the notes after the call (a commitment, a date agreed, a number, a to-do), else null. Use it
  freely; it shows nothing. Write it into memory/open-loops.md yourself as well when it is a real loop.
Use your tools between chunks as you would in a chat (your memory, mochi-sense, mochi-calendar, mochi-contacts,
mochi-drive, the web) when a chunk calls for it, but quickly: a note that comes a minute late is noise. Never anything
that acts in the world during the call; what needs a yes becomes a remember line, and an ask after the call.
{brief}The call starts now; the first message is the first chunk. Reply to this message with {{"note": null,
"box": null, "remember": null}} once you have read what you need to be ready."""

SYSTEM = """You are Mochi, a small pig who sits beside {name} while they are on a call, as a silent coach.
You hear both sides of the call (their microphone, and what their speakers play) and you see the screen the call
is on, about once a second. You never speak and never produce audio. The only ways you communicate:

- `show`: a short note that appears on their screen for a few seconds. Optionally with boxes that highlight
  areas of the LATEST frame: [ymin, xmin, ymax, xmax], each 0-1000, relative to that frame. Point only when it
  helps (a number, a name, a line in a document, a button they're looking for). Never box the whole screen.
- `remember`: a line for the notes after the call (a commitment made, a date agreed, a number, a to-do),
  without showing anything. Use it freely: it costs them nothing.

To know more, before you show something:
- `lookup`: Mochi's senses, live and read-only: a Telegram chat with someone, their mail, their calendar, a
  contact's details, a document in their Drive, their tasks. A few seconds. Use it when the call touches something
  that is written down somewhere ("the contract Nastya sent", "when did we agree to meet", "his number").
- `ask_mochi`: a question for Mochi's own brain (Claude, in its workspace, with every sense and the web). It
  takes a minute or two and comes back as a tool result; use it for what needs reading and thinking (what was
  agreed in a long thread, the state of a project, the facts behind a figure on screen), not for anything quick.
- Google Search, for public facts (a company, a price, a date, a word).
Never read a tool result out: turn what matters into one note with `show`, or into `remember`.
When the call turns to something in {name}'s life that the notes below don't settle (what someone said, when a
thing was agreed, where a document is, a figure) and {name} is reaching for it, don't stay silent: `lookup` first,
and if that doesn't settle it, `ask_mochi`. Tell them it's being looked up only if the answer is slow.

{notes}
Show a note only when it genuinely helps right now, for example:
- a fact, number, name or date they are reaching for, especially one visible on screen (point at it);
- a question from the other side that they have not answered, or a point they missed;
- a good question to ask next, or a thing they meant to raise and haven't;
- a commitment being made, so they can confirm it out loud;
- when they have been talking a long time, or the other side sounds confused;
- a mistake or a risk in what is being said or shown (a wrong figure, an old version of a document).
Most moments need nothing: then do nothing at all, no words and no tool call (anything you say aloud is thrown
away unheard, and "nothing to add" is never worth a note). Never narrate the call, never summarise what was just
said, never greet, never comment on the pig or on yourself. At most one note every ~15 seconds. Notes are at most
140 characters, lower-case, plain words, no exclamation marks, the useful part first ("the q3 figure on screen is
42k, not 40k"). Write notes in the language the call is in. {name} is the person at this computer; the other voices
are the other side of the call. If something on screen is private and unrelated to the call, ignore it.
{brief}Today is {today}."""


# ---- small helpers -----------------------------------------------------------------------------------------------

def cfg():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def env_key(name, path):
    key = os.environ.get(name)
    if key:
        return key
    try:
        for line in path.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == name and v.strip():
                return v.strip().strip('"')
    except OSError:
        pass
    return ""


def load_key():
    return env_key("GEMINI_API_KEY", GEMINI_ENV)


def assembly_key():
    return env_key("ASSEMBLYAI_API_KEY", ASSEMBLY_ENV)


def run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, "", str(e)


def monitors(xrandr_out=None):
    """[(x, y, w, h), ...] in xrandr's order."""
    if xrandr_out is None:
        _, xrandr_out, _ = run(["xrandr", "--listmonitors"])
    mons = []
    for m in re.finditer(r"^\s*\d+:\s+\S+\s+(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)", xrandr_out, re.M):
        w, h, x, y = map(int, m.groups())
        mons.append((x, y, w, h))
    return mons


def active_window_rect():
    """(x, y, w, h) of the focused window, or None."""
    _, out, _ = run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
    m = re.search(r"0x[0-9a-f]+", out)
    if not m or m.group(0) == "0x0":
        return None
    _, info, _ = run(["xwininfo", "-id", m.group(0)])
    vals = {}
    for k, key in (("Absolute upper-left X", "x"), ("Absolute upper-left Y", "y"), ("Width", "w"), ("Height", "h")):
        mm = re.search(re.escape(k) + r":\s+(-?\d+)", info)
        if not mm:
            return None
        vals[key] = int(mm.group(1))
    return vals["x"], vals["y"], vals["w"], vals["h"]


def monitor_of(rect, mons):
    """The monitor holding most of a window (by its centre), else the first."""
    if not mons:
        return None
    if rect:
        cx, cy = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2
        for mon in mons:
            if mon[0] <= cx < mon[0] + mon[2] and mon[1] <= cy < mon[1] + mon[3]:
                return mon
    return mons[0]


def boxes_to_root(boxes, frame):
    """Gemini's [ymin, xmin, ymax, xmax] (0-1000 of the frame) -> [x, y, w, h] in root pixels."""
    fx, fy, fw, fh = frame
    out = []
    for b in boxes or []:
        if not isinstance(b, (list, tuple)) or len(b) != 4:
            continue
        try:
            ymin, xmin, ymax, xmax = [min(1000, max(0, float(v))) for v in b]
        except (TypeError, ValueError):
            continue
        if ymax <= ymin or xmax <= xmin:
            continue
        x, y = fx + xmin / 1000 * fw, fy + ymin / 1000 * fh
        w, h = (xmax - xmin) / 1000 * fw, (ymax - ymin) / 1000 * fh
        if w * h > 0.6 * fw * fh:  # the whole screen is not a highlight
            continue
        out.append([int(round(x)), int(round(y)), int(round(w)), int(round(h))])
    return out[:4]


def pixel_box_to_root(box, frame, picture):
    """Mochi's [x, y, w, h] in the saved picture's pixels -> [x, y, w, h] in root pixels (the picture is a scaled
    copy of the frame), or None when it isn't a box worth drawing."""
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    try:
        x, y, w, h = [float(v) for v in box]
    except (TypeError, ValueError):
        return None
    pw, ph = picture
    if pw <= 0 or ph <= 0 or w <= 0 or h <= 0:
        return None
    sx, sy = frame[2] / pw, frame[3] / ph
    x, y = max(0.0, x), max(0.0, y)
    w, h = min(w, pw - x), min(h, ph - y)
    if w <= 0 or h <= 0 or w * h > 0.6 * pw * ph:
        return None
    return [int(round(frame[0] + x * sx)), int(round(frame[1] + y * sy)), int(round(w * sx)), int(round(h * sy))]


def pet_send(**msg):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.sendto(json.dumps(msg, ensure_ascii=False).encode(), str(PET_SOCK))
        s.close()
        return True
    except OSError:
        return False


def brain_send(msg):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.sendto(json.dumps(msg, ensure_ascii=False).encode(), str(BRAIN_SOCK))
        s.close()
        return True
    except OSError:
        return False


def overlay_show(text, boxes=None, secs=NOTE_SECS):
    return pet_send(event="coach", text=text[:400], secs=int(secs), boxes=boxes or [])


def overlay_clear():
    return pet_send(event="coach", clear="1")


def rms(chunk):
    a = array("h", chunk)
    return math.sqrt(sum(x * x for x in a) / len(a)) if len(a) else 0.0


def ffmpeg_source_cmd(src):
    """One pulse source as 16 kHz mono PCM on stdout."""
    return ["ffmpeg", "-v", "error", "-nostdin", "-f", "pulse", "-i", src,
            "-af", "aformat=sample_fmts=s16:channel_layouts=mono,aresample=16000", "-f", "s16le", "-"]


def ffmpeg_mix_cmd(mic, speakers):
    """Both sides of the call as one 16 kHz mono PCM stream on stdout (the live engine hears one stream)."""
    inputs = []
    n = 0
    for src in (mic, speakers):
        if src:
            inputs += ["-f", "pulse", "-i", src]
            n += 1
    if n == 0:
        raise SystemExit("no audio source: set mic/speakers in ~/.config/mochi/coach.json")
    if n == 1:
        filt = "aformat=sample_fmts=s16:channel_layouts=mono,aresample=16000"
    else:
        filt = "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0,aformat=sample_fmts=s16:channel_layouts=mono,aresample=16000"
    return ["ffmpeg", "-v", "error", "-nostdin", *inputs, "-filter_complex" if n > 1 else "-af", filt,
            "-f", "s16le", "-"]


def default_sources():
    c = cfg()
    mic, spk = c.get("mic"), c.get("speakers")
    if not mic:
        _, out, _ = run(["pactl", "get-default-source"])
        mic = out.strip() or None
    if not spk:
        _, out, _ = run(["pactl", "get-default-sink"])
        spk = (out.strip() + ".monitor") if out.strip() else None
    return mic, spk


def context_pack(level, name):
    """Mochi's notes, for the live model to read once at the start: what it knows about them and what's in flight."""
    if level == "none":
        return ""
    parts = []
    for fn in CONTEXT_FILES.get(level, []):
        try:
            text = (MEMORY / fn).read_text()
        except OSError:
            continue
        if len(text) > CONTEXT_CAP:
            text = text[:CONTEXT_CAP] + "\n[... cut]"
        parts.append(f"### memory/{fn}\n{text.strip()}")
    code, out, _ = run(["mochi-calendar", "agenda", "--days", "2"], timeout=25)
    if code == 0 and out.strip():
        parts.append("### their calendar, the next two days\n" + out.strip()[:6000])
    if not parts:
        return ""
    return (f"What Mochi knows about {name} (its own notes, private: for your judgement only, never for the other "
            f"side; what you show lands on {name}'s screen alone):\n\n" + "\n\n".join(parts) + "\n")


LOOKUPS = {
    "telegram": lambda q: ["mochi-sense", "telegram", "search", *q.split()],
    "mail": lambda q: ["mochi-sense", "mail", "search", q],
    "calendar": lambda q: ["mochi-calendar", "search", *q.split(), "--back", "60", "--days", "120"],
    "agenda": lambda q: ["mochi-calendar", "agenda", "--days", "7"],
    "contacts": lambda q: ["mochi-contacts", "show", q],
    "drive": lambda q: ["mochi-drive", "search", *q.split(), "--limit", "8"],
    "tasks": lambda q: ["mochi-sense", "tasks"],
}


def run_lookup(kind, query):
    """One of Mochi's read-only senses, its output cut to what a tool result can carry."""
    build = LOOKUPS.get(kind)
    if not build:
        return f"unknown kind {kind!r}; use one of {', '.join(LOOKUPS)}"
    if not query.strip() and kind not in ("agenda", "tasks"):
        return "empty query"
    code, out, err = run(build(query.strip()), timeout=40)
    text = (out or "").strip() or (err or "").strip()[-400:] or "(nothing found)"
    if code != 0 and not out.strip():
        text = f"lookup failed: {text}"
    return text[:LOOKUP_CAP]


def parse_reply(text):
    """Mochi's answer to a chunk: the one JSON object it was asked for, found wherever it put it."""
    # the object may sit in a code fence, and a mochi block (say/asks) it added anyway has no note, so it is passed over
    for m in re.finditer(r"\{.*?\}", text, flags=re.S):
        try:
            d = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(d, dict) and ("note" in d or "remember" in d):
            return {"note": d.get("note") or None, "box": d.get("box") or None, "remember": d.get("remember") or None}
    return {"note": None, "box": None, "remember": None}


def call_clock(t0):
    s = int(time.time() - t0)
    return f"{s // 60:02d}:{s % 60:02d}"


def brain_module():
    """mochi_brain.py beside this file, or the installed mochi-brain: for the tool allow-list Mochi's own runs use."""
    here = Path(__file__).resolve().parent
    for p in (here / "mochi_brain.py", Path(shutil.which("mochi-brain") or ""), Path.home() / ".local/bin/mochi-brain"):
        if p.is_file():
            spec = importlib.util.spec_from_loader("mochi_brain", importlib.machinery.SourceFileLoader("mochi_brain", str(p)))
            mod = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(mod)
                return mod
            except Exception:
                return None
    return None


# ---- the screen and the sound: shared by both engines -----------------------------------------------------------

class Screen:
    """The monitor the call is on: the active window's, re-checked every two seconds, or one pinned by number."""

    def __init__(self, pinned=None):
        self.mons = monitors()
        self.pinned = None
        if pinned is not None:
            if not 0 <= pinned < len(self.mons):
                raise SystemExit(f"no monitor {pinned} (there are {len(self.mons)})")
            self.pinned = self.mons[pinned]
        self.rect = self.pinned or monitor_of(active_window_rect(), self.mons) or (0, 0, 1920, 1080)
        self.check_at = 0.0

    def follow(self):
        if not self.pinned and time.time() > self.check_at:
            self.rect = monitor_of(active_window_rect(), self.mons) or self.rect
            self.check_at = time.time() + 2

    def grab(self):
        """(jpeg bytes, (w, h) of the picture, a 64x36 grey thumbnail for change detection)."""
        from PIL import ImageGrab
        x, y, w, h = self.rect
        im = ImageGrab.grab(bbox=(x, y, x + w, y + h), xdisplay=os.environ.get("DISPLAY", ":0"))
        im.thumbnail((FRAME_MAX_W, FRAME_MAX_W))
        small = im.convert("L").resize((64, 36))
        b = io.BytesIO()
        im.save(b, "JPEG", quality=70)
        return b.getvalue(), im.size, small

    @staticmethod
    def delta(a, b):
        pa, pb = a.tobytes(), b.tobytes()
        return sum(abs(x - y) for x, y in zip(pa, pb)) / len(pa)


class Gate:
    """An energy gate on one PCM stream: opens on sound, closes after HANGOVER quiet chunks, cuts long monologues.
    feed() returns ("start", [pre-roll chunks]), ("audio", chunk), ("end", chunk), ("restart", chunk) or None."""

    def __init__(self, threshold):
        self.threshold = threshold
        self.pre, self.quiet, self.talking, self.since = [], 0, False, 0.0

    def feed(self, chunk):
        level = rms(chunk)
        if level > self.threshold:
            self.quiet = 0
            if not self.talking:
                self.talking, self.since = True, time.time()
                pre, self.pre = self.pre, []
                return ("start", pre + [chunk])
        else:
            self.quiet += 1
        if self.talking:
            if self.quiet >= HANGOVER:
                self.talking = False
                return ("end", chunk)
            if time.time() - self.since > MAX_UTTERANCE:
                self.since = time.time()
                return ("restart", chunk)
            return ("audio", chunk)
        self.pre = (self.pre + [chunk])[-PRE_ROLL:]
        return None


async def pcm_stream(cmd, on_chunk, stop):
    """Runs an ffmpeg capture and hands each 40 ms chunk to on_chunk (a coroutine) until stop() says so."""
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        while not stop():
            try:
                chunk = await proc.stdout.readexactly(CHUNK)
            except asyncio.IncompleteReadError:
                return
            await on_chunk(chunk)
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()


# ---- speech to text: one session per side of the call ----------------------------------------------------------

class GeminiSTT:
    """Gemini's transcribe-live model, one session per source, fed inside activity marks from an energy gate (its
    own voice detection never fires on this account; see memory). on_turn(label, text) for each utterance."""

    def __init__(self, label, key, gate, on_turn, debug):
        self.label, self.key, self.gate, self.on_turn, self.debug = label, key, gate, on_turn, debug
        self.session = None
        self.running = True

    async def run(self):
        from google import genai
        from google.genai import types
        self.types = types
        client = genai.Client(api_key=self.key, http_options=types.HttpOptions(api_version="v1beta"))
        cfg_ = types.LiveConnectConfig(
            response_modalities=[types.Modality.TEXT], input_audio_transcription=types.AudioTranscriptionConfig(),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)))
        backoff = 1
        while self.running:
            try:
                async with client.aio.live.connect(model=GEMINI_STT_MODEL, config=cfg_) as s:
                    self.session = s
                    backoff = 1
                    self.debug(f"stt {self.label}: gemini connected")
                    while self.running:
                        async for m in s.receive():
                            sc = m.server_content
                            if sc and sc.input_transcription and sc.input_transcription.text:
                                self.on_turn(self.label, sc.input_transcription.text.strip())
                            if not self.running:
                                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self.running:
                    break
                self.debug(f"stt {self.label} dropped:", repr(e)[:200])
                self.session = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15)
            self.session = None

    async def feed(self, chunk):
        ev = self.gate.feed(chunk)
        s = self.session
        if not ev or not s:
            return
        kind, data = ev
        t = self.types
        try:
            if kind == "start":
                await s.send_realtime_input(activity_start=t.ActivityStart())
                for c in data:
                    await s.send_realtime_input(audio=t.Blob(data=c, mime_type=f"audio/pcm;rate={RATE}"))
            elif kind == "audio":
                await s.send_realtime_input(audio=t.Blob(data=data, mime_type=f"audio/pcm;rate={RATE}"))
            elif kind == "end":
                await s.send_realtime_input(audio=t.Blob(data=data, mime_type=f"audio/pcm;rate={RATE}"))
                await s.send_realtime_input(activity_end=t.ActivityEnd())
            elif kind == "restart":
                await s.send_realtime_input(activity_end=t.ActivityEnd())
                await s.send_realtime_input(activity_start=t.ActivityStart())
                await s.send_realtime_input(audio=t.Blob(data=data, mime_type=f"audio/pcm;rate={RATE}"))
        except Exception as e:
            self.debug(f"stt {self.label} send failed:", repr(e)[:120])

    async def stop(self):
        self.running = False


class AssemblySTT:
    """AssemblyAI's streaming API, one session per source: it finds the turns itself. on_turn(label, text) for each
    formatted, finished turn."""

    def __init__(self, label, key, model, language, on_turn, debug):
        self.label, self.key, self.model, self.language, self.on_turn, self.debug = label, key, model, language, on_turn, debug
        self.client = None
        self.running = True

    async def run(self):
        from assemblyai.streaming import v3 as s3
        loop = asyncio.get_running_loop()

        def on_begin(_c, ev):
            loop.call_soon_threadsafe(self.debug, f"stt {self.label}: assemblyai session {ev.id}")

        def on_turn(_c, ev):
            if ev.end_of_turn and ev.transcript and ev.turn_is_formatted:
                loop.call_soon_threadsafe(self.on_turn, self.label, ev.transcript.strip())

        def on_error(_c, err):
            loop.call_soon_threadsafe(self.debug, f"stt {self.label} error:", str(err)[:200])

        params = dict(speech_model=self.model, encoding=s3.Encoding.pcm_s16le, sample_rate=RATE, format_turns=True)
        if self.language:
            params["language_code"] = self.language
        client = s3.AsyncRealTimeTranscriber(s3.RealTimeTranscriberOptions(api_key=self.key))
        client.on(s3.RealTimeEvents.Begin, on_begin)
        client.on(s3.RealTimeEvents.Turn, on_turn)
        client.on(s3.RealTimeEvents.Error, on_error)
        await client.connect(s3.RealTimeParameters(**params))
        self.client = client
        try:
            while self.running:
                await asyncio.sleep(0.5)
        finally:
            try:
                await client.disconnect(terminate=True)
            except Exception:
                pass

    async def feed(self, chunk):
        if self.client and self.running:
            try:
                await self.client.stream(chunk)
            except Exception as e:
                self.debug(f"stt {self.label} send failed:", repr(e)[:120])

    async def stop(self):
        self.running = False


# ---- the mochi engine: a running Claude Code session in Mochi's workspace, fed the transcript ------------------

class MochiSession:
    """One Claude Code session, the normal Mochi (its workspace, memory and read-only tools), that gets the call in
    chunks and answers each with one JSON line."""

    def __init__(self, name, brief, model, budget, debug):
        self.name, self.brief, self.model, self.budget, self.debug = name, brief, model, budget, debug
        self.client = None
        self.lock = asyncio.Lock()
        self.cost = 0.0
        self.turns = 0
        self.session_id = ""

    def options(self):
        from claude_agent_sdk import ClaudeAgentOptions
        mb = brain_module()
        allowed = list(mb.LEVELS["coach"]) if mb and "coach" in getattr(mb, "LEVELS", {}) else [
            "Read", "Grep", "Glob", "WebSearch", "WebFetch", "Bash(mochi-sense:*)", "Bash(mochi-calendar agenda:*)",
            "Bash(mochi-calendar search:*)", "Bash(mochi-contacts:*)", "Bash(mochi-drive search:*)",
            "Bash(mochi-drive read:*)", "Write(memory/**)", "Edit(memory/**)", "Write(journal/**)", "Edit(journal/**)"]
        never = list(getattr(mb, "NEVER", []) or [])
        env = {"MOCHI_BRAIN": "1"}
        if mb and hasattr(mb, "claude_auth_env"):
            try:
                env.update(mb.claude_auth_env())
            except Exception:
                pass
        brief = f"About this call: {self.brief.strip()}\n" if self.brief else ""
        return ClaudeAgentOptions(
            cwd=str(WORK), permission_mode="dontAsk", allowed_tools=allowed, disallowed_tools=never, env=env,
            setting_sources=["project"], model=self.model or None, max_budget_usd=self.budget or None,
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": CALL_PROMPT.format(name=self.name, brief=brief, frame=FRAME)})

    async def open(self):
        from claude_agent_sdk import ClaudeSDKClient
        os.environ.pop("CLAUDECODE", None)  # the SDK's CLI must not think it is nested in another session
        self.client = ClaudeSDKClient(options=self.options())
        await self.client.connect()
        t0 = time.time()
        reply = await self.ask("(the call is about to start; get ready)")
        self.debug(f"mochi ready in {time.time() - t0:.0f}s (${self.cost:.2f})", reply[:120])

    async def ask(self, text):
        """One chunk in, Mochi's reply out (the raw text; parse_reply reads it)."""
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
        async with self.lock:
            await self.client.query(text)
            out = []
            async for m in self.client.receive_response():
                if isinstance(m, AssistantMessage):
                    for b in m.content:
                        if isinstance(b, TextBlock) and b.text:
                            out.append(b.text)
                elif isinstance(m, ResultMessage):
                    self.turns += 1
                    self.cost += float(m.total_cost_usd or 0)
                    self.session_id = m.session_id or self.session_id
                    if m.is_error:
                        self.debug("mochi run error:", m.subtype, (m.errors or [""])[0][:200] if m.errors else "")
            return out[-1] if out else ""

    async def close(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass


class MochiCoach:
    def __init__(self, args):
        self.args = args
        c = cfg()
        self.name = c.get("name") or "Artem"
        self.gate_level = float(c.get("gate", 500))
        self.every = float(args.every or c.get("every", 15))
        self.screen = Screen(args.monitor)
        self.running = True
        self.t0 = time.time()
        self.buffer = []          # (clock, label, text) since the last chunk went
        self.last_sent = 0.0
        self.picture = (1, 1)     # size of the last picture Mochi was told about
        self.notes = 0
        self.stts = {}
        LOGS.mkdir(parents=True, exist_ok=True)
        self.log_path = LOGS / f"{dt.datetime.now():%Y%m%d-%H%M%S}.md"
        self.stt_name = args.stt or ("assemblyai" if assembly_key() else "gemini")
        self.log_path.write_text(f"# coach session {dt.datetime.now():%Y-%m-%d %H:%M}\n\nengine: mochi, speech to text: "
                                 f"{self.stt_name}, model: {args.model or c.get('model') or 'default'}\n\n")
        self.mochi = MochiSession(self.name, args.brief, args.model or c.get("model"), float(c.get("budget", 15)), self.debug)

    def log(self, line):
        with self.log_path.open("a") as f:
            f.write(f"- {dt.datetime.now():%H:%M:%S} {line}\n")

    def debug(self, *a):
        if self.args.debug:
            print(f"[{time.strftime('%H:%M:%S')}]", *a, file=sys.stderr, flush=True)

    def on_turn(self, label, text):
        if not text:
            return
        self.buffer.append((call_clock(self.t0), label, text))
        self.debug(f"{label}: {text}")
        if self.args.transcript:
            self.log(f"{label}: {text}")

    def make_stt(self, label):
        if self.stt_name == "assemblyai":
            key = assembly_key()
            if not key:
                raise SystemExit("no AssemblyAI key: put ASSEMBLYAI_API_KEY=... in ~/.config/mochi/assemblyai.env, or --stt gemini")
            c = cfg()
            return AssemblySTT(label, key, c.get("stt_model") or DEFAULT_STT_MODEL, c.get("stt_language") or "", self.on_turn, self.debug)
        key = load_key()
        if not key:
            raise SystemExit("no Gemini key: put GEMINI_API_KEY=... in ~/.config/mochi/gemini.env (aistudio.google.com)")
        return GeminiSTT(label, key, Gate(self.gate_level), self.on_turn, self.debug)

    def chunk_text(self, lines, frame_line):
        return f"[call {lines[0][0]}-{lines[-1][0]}]\n" + "\n".join(f"{lb}: {t}" for _, lb, t in lines) + frame_line

    async def sender(self):
        """Every second: if there is transcript waiting and it is time, one chunk goes to Mochi."""
        while self.running:
            await asyncio.sleep(1)
            if not self.buffer:
                continue
            chars = sum(len(t) for _, _, t in self.buffer)
            waited = time.time() - self.last_sent
            if waited < self.every and chars < CHUNK_CHARS:
                continue
            if chars < MIN_CHUNK_CHARS and waited < 3 * self.every:  # an "okay" alone isn't worth a turn (each costs)
                continue
            lines, self.buffer = self.buffer, []
            self.last_sent = time.time()
            frame_line = ""
            try:
                self.screen.follow()
                data, size, _ = await asyncio.get_running_loop().run_in_executor(None, self.screen.grab)
                FRAME.write_bytes(data)
                frame_line = f"\n(screen now: {FRAME}, {size[0]}x{size[1]} px)"
                self.picture = size
            except Exception as e:
                self.debug("grab failed:", e)
            t1 = time.time()
            try:
                reply = await self.mochi.ask(self.chunk_text(lines, frame_line))
            except Exception as e:
                self.debug("mochi failed:", repr(e)[:200])
                continue
            r = parse_reply(reply)
            self.debug(f"mochi in {time.time() - t1:.1f}s (${self.mochi.cost:.2f} so far):", r)
            if r["remember"]:
                self.log(f"remember: {r['remember']}")
            if r["note"]:
                note = " ".join(str(r["note"]).split())[:200]
                box = pixel_box_to_root(r["box"], self.screen.rect, self.picture)
                overlay_show(note, [box] if box else None, min(25, max(8, 4 + len(note) / 12)))
                self.notes += 1
                self.log(f"show: {note}" + (f"  box {box}" if box else ""))

    async def main(self):
        mic, spk = default_sources()
        self.debug("audio:", mic, "+", spk, "| stt:", self.stt_name)
        pet_send(event="coach", state="on")
        tasks = []
        try:
            await self.mochi.open()
            for label, src in (("me", mic), ("them", spk)):
                if not src:
                    continue
                stt = self.make_stt(label)
                self.stts[label] = stt
                tasks.append(asyncio.create_task(stt.run()))
                tasks.append(asyncio.create_task(pcm_stream(ffmpeg_source_cmd(src), stt.feed, lambda: not self.running)))
            if not self.stts:
                raise SystemExit("no audio source: set mic/speakers in ~/.config/mochi/coach.json")
            await self.sender()
        finally:
            self.running = False
            for stt in self.stts.values():
                await stt.stop()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.mochi.close()
            pet_send(event="coach", state="off")
            overlay_clear()
            self.log(f"ended: {self.notes} notes, {self.mochi.turns} chunks, ${self.mochi.cost:.2f}, "
                     f"claude session {self.mochi.session_id}")
            print(self.log_path)


# ---- the live engine: Gemini Live hears and sees for itself -----------------------------------------------------

class LiveCoach:
    def __init__(self, args):
        self.args = args
        c = cfg()
        self.model = args.model or c.get("live_model") or DEFAULT_LIVE_MODEL
        self.gate = Gate(float(c.get("gate", 500)))
        self.window_tokens = int(c.get("window_tokens", 12000))
        self.fps = float(c.get("fps", 1))
        self.resolution = (c.get("resolution") or "low").lower()
        self.screen = Screen(args.monitor)
        self.session = None
        self.handle = None
        self.running = True
        self.reconnect = False
        self.turn_tool_calls = 0
        self.spoken = ""
        self.notes = 0
        self.tokens = 0
        self.pending = {}  # ask_mochi calls waiting for the relay's answer: id -> (fc, started)
        self.search = not args.no_search
        self.notes_text = ""
        LOGS.mkdir(parents=True, exist_ok=True)
        self.log_path = LOGS / f"{dt.datetime.now():%Y%m%d-%H%M%S}.md"
        self.log_path.write_text(f"# coach session {dt.datetime.now():%Y-%m-%d %H:%M}\n\nengine: live, model: {self.model}\n\n")
        self.name = c.get("name") or "Artem"

    def log(self, line):
        with self.log_path.open("a") as f:
            f.write(f"- {dt.datetime.now():%H:%M:%S} {line}\n")

    def debug(self, *a):
        if self.args.debug:
            print(f"[{time.strftime('%H:%M:%S')}]", *a, file=sys.stderr, flush=True)

    def config(self, types):
        brief = f"About this call: {self.args.brief.strip()}\n" if self.args.brief else ""
        if not self.notes_text:
            self.notes_text = context_pack(self.args.context, self.name)
        system = SYSTEM.format(name=self.name, brief=brief, today=dt.date.today().strftime("%A %d %B %Y"), notes=self.notes_text)
        show = types.FunctionDeclaration(
            name="show", behavior=types.Behavior.NON_BLOCKING,
            description="Put a short note on the screen for a few seconds, optionally highlighting areas of the latest frame.",
            parameters_json_schema={"type": "object", "properties": {
                "text": {"type": "string", "description": "the note, at most 140 characters, lower-case"},
                "boxes": {"type": "array", "description": "areas to highlight on the latest frame: [ymin, xmin, ymax, xmax], each 0-1000",
                          "items": {"type": "array", "items": {"type": "integer"}}}},
                "required": ["text"]})
        remember = types.FunctionDeclaration(
            name="remember", behavior=types.Behavior.NON_BLOCKING,
            description="Write a line into the notes for after the call (a commitment, a date, a number, a to-do). Shows nothing.",
            parameters_json_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
        lookup = types.FunctionDeclaration(
            name="lookup", behavior=types.Behavior.NON_BLOCKING,
            description="Look something up in Mochi's senses (read-only, a few seconds): kind is one of telegram "
                        "(search their chats), mail (search their mail; notmuch syntax works: from:, subject:), calendar "
                        "(search events), agenda (the next days), contacts (a person's details), drive (search their "
                        "documents), tasks (their open tasks). query is the words to search.",
            parameters_json_schema={"type": "object", "properties": {
                "kind": {"type": "string", "enum": ["telegram", "mail", "calendar", "agenda", "contacts", "drive", "tasks"]},
                "query": {"type": "string"}}, "required": ["kind", "query"]})
        ask_mochi = types.FunctionDeclaration(
            name="ask_mochi", behavior=types.Behavior.NON_BLOCKING,
            description="Ask Mochi's brain (Claude, in its workspace, with all its senses and the web) a question that "
                        "needs reading or thinking. Slow: a minute or two. The answer comes back as the tool result.",
            parameters_json_schema={"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]})
        tools = [types.Tool(function_declarations=[show, remember, lookup, ask_mochi])]
        if self.search:
            tools.append(types.Tool(google_search=types.GoogleSearch()))
        res = {"low": types.MediaResolution.MEDIA_RESOLUTION_LOW, "medium": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
               "high": types.MediaResolution.MEDIA_RESOLUTION_HIGH}.get(self.resolution, types.MediaResolution.MEDIA_RESOLUTION_LOW)
        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],  # the only one these models offer; the audio is dropped
            system_instruction=types.Content(parts=[types.Part(text=system)]),
            tools=tools,
            media_resolution=res,
            output_audio_transcription=types.AudioTranscriptionConfig(),
            input_audio_transcription=types.AudioTranscriptionConfig() if self.args.transcript else None,
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)),
            context_window_compression=types.ContextWindowCompressionConfig(
                trigger_tokens=self.window_tokens * 2, sliding_window=types.SlidingWindow(target_tokens=self.window_tokens)),
            session_resumption=types.SessionResumptionConfig(handle=self.handle),
        )

    async def on_chunk(self, chunk, types):
        ev = self.gate.feed(chunk)
        s = self.session
        if not ev or not s:
            return
        kind, data = ev
        try:
            if kind == "start":
                await s.send_realtime_input(activity_start=types.ActivityStart())
                for c in data:
                    await s.send_realtime_input(audio=types.Blob(data=c, mime_type=f"audio/pcm;rate={RATE}"))
                self.debug("speech starts")
            elif kind == "audio":
                await s.send_realtime_input(audio=types.Blob(data=data, mime_type=f"audio/pcm;rate={RATE}"))
            elif kind == "end":
                await s.send_realtime_input(audio=types.Blob(data=data, mime_type=f"audio/pcm;rate={RATE}"))
                await s.send_realtime_input(activity_end=types.ActivityEnd())
                self.debug("speech ends")
            elif kind == "restart":
                await s.send_realtime_input(activity_end=types.ActivityEnd())
                await s.send_realtime_input(activity_start=types.ActivityStart())
                await s.send_realtime_input(audio=types.Blob(data=data, mime_type=f"audio/pcm;rate={RATE}"))
                self.debug("long utterance: a turn")
        except Exception as e:
            self.debug("audio not sent:", repr(e)[:120])

    async def video_task(self, types):
        loop = asyncio.get_running_loop()
        last_small, last_sent = None, 0.0
        while self.running:
            t = time.time()
            self.screen.follow()
            try:
                data, _, small = await loop.run_in_executor(None, self.screen.grab)
            except Exception as e:
                self.debug("grab failed:", e)
                await asyncio.sleep(1 / self.fps)
                continue
            changed = last_small is None or Screen.delta(last_small, small) > 2.0
            if (changed or t - last_sent > FRAME_EVERY) and self.session:
                try:
                    await self.session.send_realtime_input(video=types.Blob(data=data, mime_type="image/jpeg"))
                    last_sent, last_small = t, small
                except Exception as e:
                    self.debug("frame not sent:", e)
            await asyncio.sleep(max(0.05, 1 / self.fps - (time.time() - t)))

    async def on_message(self, m, types):
        s = self.session
        if m.session_resumption_update and m.session_resumption_update.resumable and m.session_resumption_update.new_handle:
            self.handle = m.session_resumption_update.new_handle
        if m.go_away:
            self.debug("server says go away in", m.go_away.time_left)
            self.reconnect = True
        if m.usage_metadata and m.usage_metadata.total_token_count:
            self.tokens = m.usage_metadata.total_token_count
        if m.tool_call:
            responses = []
            for fc in m.tool_call.function_calls:
                args = fc.args or {}
                self.turn_tool_calls += 1
                if fc.name == "show":
                    self.show(str(args.get("text") or ""), args.get("boxes"))
                elif fc.name == "remember":
                    self.log(f"remember: {args.get('text')}")
                    self.debug("remember:", args.get("text"))
                elif fc.name == "lookup":
                    asyncio.create_task(self.lookup(fc, types))
                    continue
                elif fc.name == "ask_mochi":
                    self.ask_mochi(fc, types)
                    continue
                responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response={"result": "ok"},
                                                        scheduling=types.FunctionResponseScheduling.SILENT))
            if responses and s:
                await s.send_tool_response(function_responses=responses)
        sc = m.server_content
        if not sc:
            return
        if sc.input_transcription and sc.input_transcription.text and self.args.transcript:
            self.log(f"heard: {sc.input_transcription.text.strip()}")
        if sc.output_transcription and sc.output_transcription.text:
            self.spoken += sc.output_transcription.text
        if sc.turn_complete:
            spoken = self.spoken.strip()
            # It spoke instead of using the tool: the words still reach the screen, unless it's a stray word or "nothing"
            if spoken and self.turn_tool_calls == 0 and len(spoken.split()) >= 3 and not NOTHING.search(spoken):
                self.show(spoken.lower()[:200], None, spoke=True)
            elif spoken:
                self.log(f"(spoke, dropped) {spoken}")
            self.spoken, self.turn_tool_calls = "", 0

    async def lookup(self, fc, types):
        kind, query = str((fc.args or {}).get("kind") or ""), str((fc.args or {}).get("query") or "")
        self.debug("lookup:", kind, query)
        self.log(f"lookup: {kind} {query}")
        text = await asyncio.get_running_loop().run_in_executor(None, run_lookup, kind, query)
        await self.answer_tool(fc, types, text)

    def ask_mochi(self, fc, types):
        q = str((fc.args or {}).get("question") or "").strip()
        qid = f"coach-{int(time.time() * 1000) % 100000000}"
        self.debug("ask mochi:", q)
        self.log(f"asked mochi: {q}")
        ok = q and brain_send({"event": "coach_ask", "id": qid, "text": q[:1500], "reply_to": str(COACH_SOCK)})
        if not ok:
            asyncio.create_task(self.answer_tool(fc, types, "mochi's brain isn't running; say so only if it matters"))
            return
        self.pending[qid] = (fc, time.time())
        overlay_show("asking my brain: " + q[:120], None, 6)

    def on_coach_sock(self, sock, types):
        try:
            m = json.loads(sock.recv(65536).decode())
        except (OSError, ValueError):
            return
        if m.get("event") != "coach_answer":
            return
        hit = self.pending.pop(str(m.get("id") or ""), None)
        if not hit:
            return
        fc, started = hit
        text = " ".join(str(m.get("text") or "").split())[:LOOKUP_CAP] or "(no answer)"
        self.debug(f"mochi answered in {time.time() - started:.0f}s:", text[:200])
        self.log(f"mochi answered: {text[:500]}")
        asyncio.create_task(self.answer_tool(fc, types, text))

    async def answer_tool(self, fc, types, text):
        s = self.session
        if not s:
            return
        try:
            await s.send_tool_response(function_responses=[types.FunctionResponse(
                id=fc.id, name=fc.name, response={"result": text[:LOOKUP_CAP]},
                scheduling=types.FunctionResponseScheduling.INTERRUPT)])
        except Exception as e:
            self.debug("tool answer not sent:", e)

    def expire_pending(self):
        for qid, (fc, started) in list(self.pending.items()):
            if time.time() - started > ASK_TIMEOUT:
                self.pending.pop(qid)
                self.debug("mochi didn't answer in time:", qid)

    def show(self, text, boxes, spoke=False):
        text = " ".join(text.split())
        if not text:
            return
        root = boxes_to_root(boxes, self.screen.rect)
        overlay_show(text, root, min(25, max(8, 4 + len(text) / 12)))
        self.notes += 1
        self.log(f"{'spoke' if spoke else 'show'}: {text}" + (f"  boxes {root}" if root else ""))
        self.debug("SHOW:", text, root)

    async def main(self):
        from google import genai
        from google.genai import types
        key = load_key()
        if not key:
            raise SystemExit("no Gemini key: put GEMINI_API_KEY=... in ~/.config/mochi/gemini.env (aistudio.google.com)")
        client = genai.Client(api_key=key, http_options=types.HttpOptions(api_version="v1beta"))
        loop = asyncio.get_running_loop()
        try:
            COACH_SOCK.unlink()
        except OSError:
            pass
        csock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        csock.bind(str(COACH_SOCK))
        os.chmod(COACH_SOCK, 0o600)
        csock.setblocking(False)
        loop.add_reader(csock.fileno(), self.on_coach_sock, csock, types)
        mic, spk = default_sources()
        self.debug("audio:", mic, "+", spk)
        pet_send(event="coach", state="on")
        tasks = [asyncio.create_task(pcm_stream(ffmpeg_mix_cmd(mic, spk), lambda c: self.on_chunk(c, types), lambda: not self.running)),
                 asyncio.create_task(self.video_task(types))]
        backoff = 1
        try:
            while self.running:
                self.reconnect = False
                try:
                    async with client.aio.live.connect(model=self.model, config=self.config(types)) as session:
                        self.session = session
                        backoff = 1
                        self.debug("connected", self.model, "resumed" if self.handle else "fresh", "frame", self.screen.rect)
                        while self.running and not self.reconnect:
                            async for m in session.receive():
                                await self.on_message(m, types)
                                self.expire_pending()
                                if not self.running or self.reconnect:
                                    break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    if not self.running:
                        break
                    self.debug("session dropped:", repr(e)[:300])
                    if self.search and "search" in repr(e).lower():
                        self.search = False
                        self.debug("retrying without Google Search")
                    self.session = None
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 15)
                self.session = None
        finally:
            self.running = False
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            loop.remove_reader(csock.fileno())
            csock.close()
            try:
                COACH_SOCK.unlink()
            except OSError:
                pass
            pet_send(event="coach", state="off")
            overlay_clear()
            self.log(f"ended: {self.notes} notes, last context {self.tokens} tokens")
            print(self.log_path)


# ---- commands ------------------------------------------------------------------------------------------------------

def start(args):
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text())
            os.kill(pid, 0)
            sys.exit(f"already running (pid {pid}); mochi-coach stop first")
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    PID_FILE.write_text(str(os.getpid()))
    coach = LiveCoach(args) if args.engine == "live" else MochiCoach(args)
    loop = asyncio.new_event_loop()

    def stop(*_):
        coach.running = False
        for t in asyncio.all_tasks(loop):
            t.cancel()
    loop.add_signal_handler(signal.SIGTERM, stop)
    loop.add_signal_handler(signal.SIGINT, stop)
    try:
        loop.run_until_complete(coach.main())
    except asyncio.CancelledError:
        pass
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        try:
            PID_FILE.unlink()
        except OSError:
            pass


def stop(_args):
    try:
        pid = int(PID_FILE.read_text())
        os.kill(pid, signal.SIGTERM)
        print(f"stopping {pid}")
    except (OSError, ValueError):
        sys.exit("not running")


def status(_args):
    try:
        pid = int(PID_FILE.read_text())
        os.kill(pid, 0)
        print(f"running (pid {pid})")
    except (OSError, ValueError):
        print("not running")
    logs = sorted(LOGS.glob("*.md"))
    if logs:
        print(f"last session: {logs[-1]}")
    mic, spk = default_sources()
    print(f"sources: {mic} + {spk}")
    print("assemblyai key:", "set" if assembly_key() else "missing (ASSEMBLYAI_API_KEY in ~/.config/mochi/assemblyai.env; "
                                                          "speech to text falls back to gemini)")
    print("gemini key:", "set" if load_key() else "missing (GEMINI_API_KEY in ~/.config/mochi/gemini.env)")


def show_cmd(args):
    boxes = []
    for b in args.box or []:
        try:
            boxes.append([int(v) for v in b.split(",")][:4])
        except ValueError:
            sys.exit(f"bad --box {b}: want X,Y,W,H")
    if not overlay_show(args.text, boxes, args.secs):
        sys.exit("the pig isn't running")


def clear_cmd(_args):
    if not overlay_clear():
        sys.exit("the pig isn't running")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mochi-coach", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--engine", choices=["mochi", "live"], default="mochi")
    s.add_argument("--stt", choices=["assemblyai", "gemini"])
    s.add_argument("--monitor", type=int)
    s.add_argument("--brief", default="")
    s.add_argument("--model")
    s.add_argument("--every", type=float)
    s.add_argument("--context", choices=["full", "light", "none"], default="full")
    s.add_argument("--no-search", action="store_true")
    s.add_argument("--transcript", action="store_true")
    s.add_argument("--debug", action="store_true")
    s.set_defaults(fn=start)
    sub.add_parser("stop").set_defaults(fn=stop)
    sub.add_parser("status").set_defaults(fn=status)
    sh = sub.add_parser("show")
    sh.add_argument("text")
    sh.add_argument("--box", action="append")
    sh.add_argument("--secs", type=int, default=NOTE_SECS)
    sh.set_defaults(fn=show_cmd)
    sub.add_parser("clear").set_defaults(fn=clear_cmd)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    # The SDKs live in Mochi's tools venv (make tools); hop over to it when this python lacks them.
    if len(sys.argv) > 1 and sys.argv[1] == "start":
        try:
            import google.genai  # noqa: F401
            import claude_agent_sdk  # noqa: F401
        except ImportError:
            py = TOOLS / "bin" / "python"
            if py.exists() and os.environ.get("MOCHI_COACH_VENV") != "1":  # once: the venv's python is a symlink to this one
                os.environ["MOCHI_COACH_VENV"] = "1"
                os.execv(str(py), [str(py), *sys.argv])
            sys.exit("google-genai / claude-agent-sdk missing: run `make tools` in the xpet repo")
    main()
