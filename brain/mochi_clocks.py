#!/usr/bin/env python3
"""mochi-clocks — Mochi on the wall clocks in the kitchen and the corridor.

The clocks are Raspberry Pi 2s with 1024×600 touch screens and speakers, running clock_v3_cpp (a landscape that
follows the sun and the weather, the time, and the next few hours). Mochi lives on the near shore of its lake: it
wanders, sniffs, naps at night, and sits at its laptop while one of its runs is going. What it has for them is quiet,
as on the desktop: a dot over the pig when there is something in the menu, and the asks themselves when they tap it.
Only something urgent walks the pig to the middle with a bubble and a sound.

This is a module of the relay (mochi-brain loads it and runs `Clocks.serve`): a small HTTP server on the LAN, every
request carrying the token from ~/.config/mochi/clocks.json (made on first start):

  GET  /clock?room=kitchen         what to show: asks, an urgent line, whether a run is going, the sheet's version
  GET  /clock/sheet.png|json       the pig, drawn by `xpet --clock-sheet` in today's costume (redrawn each day)
  POST /clock/answer               {"id", "label", "room"}: a tap on an ask's option, handed to the relay as an
                                   answer from "clock-<room>", exactly like a click on the desktop pig
  POST /clock/seen                 {"room", "what", "lux"?}: a touch, or the kitchen lights going on or off
  POST /clock/hush                 {"id"}: the urgent line was tapped away, on either clock

`mochi-clocks status` prints what the clocks last told the relay; `mochi-clocks sheet` redraws the pig now.
"""

import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOME = Path.home()
WORK = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "mochi"
SHEET_DIR = WORK / "clock"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "mochi" / "clocks.json"
SEEN_FILE = SHEET_DIR / "seen.json"
PORT = 8377
ROOMS = ("kitchen", "corridor")
SAY_FOR = 15 * 60          # an urgent line stays on the clocks this long, unless tapped away
ASKS_SHOWN = 4
DESK_ONLY = {"chat about it", "show me"}  # options that open something on the desktop: not offered on a wall
MAX_BODY = 4096


def config():
    """Port and token; the token is made once and kept (the clocks have it in their service environment)."""
    try:
        c = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        c = {}
    if not c.get("token"):
        c = {"port": c.get("port") or PORT, "token": secrets.token_urlsafe(24)}
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(c, f, indent=1)
    return c


def xpet():
    return shutil.which("xpet") or str(HOME / ".local/bin/xpet")


def clock_options(options):
    return [o for o in options or [] if o.lower() not in DESK_ONLY]


class Clocks:
    """The relay's side of the clocks. `asks` returns the relay's pending asks ({id: ask}), `busy` the kind of the run
    going (or None); a tap on an answer goes to the relay's own socket `sock`, as the pig would send it."""

    def __init__(self, asks, busy, sock=None, log=print):
        self.asks, self.busy, self.sock, self.log = asks, busy, sock, log
        self.lock = threading.Lock()
        self.said = None  # {"id", "text", "at"}: the last urgent line
        self.hushed = set()
        self.seen = self.load_seen()
        self.drawing = False
        self.cfg = config()

    # -- what the relay tells the clocks

    def say(self, text, sid=None):
        with self.lock:
            self.said = {"id": sid or secrets.token_hex(4), "text": text[:160], "at": time.time()}

    def feed(self):
        try:
            pending = list(self.asks().items())
        except RuntimeError:  # the relay changed the dict under us; next poll gets it
            pending = []
        shown = []
        for aid, a in sorted(pending, key=lambda kv: (not kv[1].get("urgent"), -kv[1].get("at", 0)))[:ASKS_SHOWN]:
            opts = clock_options(a.get("options"))
            if opts:
                shown.append({"id": aid, "text": a.get("text", ""), "options": opts, "urgent": bool(a.get("urgent")),
                              "at": round(a.get("at", 0))})
        with self.lock:
            s = self.said
            say = s if s and time.time() - s["at"] < SAY_FOR and s["id"] not in self.hushed else None
        return {"t": round(time.time()), "asks": shown, "say": say, "busy": self.busy() or "",
                "sheet": self.sheet_version()}

    # -- what the clocks tell the relay

    def answer(self, body, room):
        aid, label = str(body.get("id", "")), str(body.get("label", ""))
        a = self.asks().get(aid)
        if not a or label not in clock_options(a.get("options")):
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.sendto(json.dumps({"event": "answer", "id": aid, "answer": label, "via": f"clock-{room}"}).encode(),
                         str(self.sock))
        except (OSError, TypeError) as e:
            self.log(f"clocks: answer not handed to the relay: {e}")
            return False
        self.note(room, f"answered '{label}'")
        return True

    def note(self, room, what, lux=None):
        with self.lock:
            r = self.seen.setdefault(room, {})
            r["last"] = {"what": what[:60], "at": round(time.time())}
            if what == "touch":
                r["touched"] = round(time.time())
            if what in ("lights on", "lights off"):
                r["lights"] = {"on": what == "lights on", "at": round(time.time())}
            if lux is not None:
                r["lux"] = round(float(lux), 1)
            self.save_seen()

    def hush(self, sid):
        with self.lock:
            self.hushed = (self.hushed | {str(sid)[:20]}) if len(self.hushed) < 200 else {str(sid)[:20]}

    def status(self):
        """For Mochi's context: when they were last at a clock, and whether the kitchen light is on."""
        out = []
        with self.lock:
            seen = json.loads(json.dumps(self.seen))
        for room in ROOMS:
            r = seen.get(room)
            if not r:
                continue
            bits = []
            if r.get("lights"):
                bits.append(f"lights {'on' if r['lights']['on'] else 'off'} since {stamp(r['lights']['at'])}")
            if r.get("touched"):
                bits.append(f"last touched {stamp(r['touched'])}")
            if r.get("polled"):
                bits.append("online" if time.time() - r["polled"] < 180 else f"offline since {stamp(r['polled'])}")
            out.append(f"{room} clock: " + ", ".join(bits))
        return out or ["no clock has called in yet"]

    def load_seen(self):
        try:
            return json.loads(SEEN_FILE.read_text())
        except (OSError, ValueError):
            return {}

    def save_seen(self):
        try:
            SHEET_DIR.mkdir(parents=True, exist_ok=True)
            tmp = SEEN_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.seen))
            tmp.replace(SEEN_FILE)
        except OSError:
            pass

    # -- the pig's sprite sheet

    def sheet_version(self):
        png = SHEET_DIR / "sheet.png"
        try:
            st = png.stat()
        except OSError:
            self.redraw()
            return ""
        stale = dt.date.fromtimestamp(st.st_mtime) != dt.date.today()
        try:
            stale = stale or Path(xpet()).stat().st_mtime > st.st_mtime
        except OSError:
            pass
        if stale:
            self.redraw()
        return f"{int(st.st_mtime)}-{st.st_size}"

    def redraw(self, wait=False):
        """Draw the sheet with the installed xpet, in the background; the old one is served until the new is in."""
        with self.lock:
            if self.drawing:
                return
            self.drawing = True

        def draw():
            try:
                SHEET_DIR.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=SHEET_DIR) as tmp:
                    p = subprocess.run([xpet(), "--clock-sheet", tmp], capture_output=True, text=True, timeout=120)
                    if p.returncode == 0 and (Path(tmp) / "sheet.png").exists():
                        os.replace(Path(tmp) / "sheet.json", SHEET_DIR / "sheet.json")
                        os.replace(Path(tmp) / "sheet.png", SHEET_DIR / "sheet.png")  # last: it is the version
                        self.log("clocks: drew the pig's sheet")
                    else:
                        self.log(f"clocks: xpet --clock-sheet failed: {(p.stderr or p.stdout)[:200]}")
            except (OSError, subprocess.TimeoutExpired) as e:
                self.log(f"clocks: sheet not drawn: {e}")
            finally:
                with self.lock:
                    self.drawing = False

        if wait:
            draw()
        else:
            threading.Thread(target=draw, daemon=True).start()

    # -- the server

    def serve(self):
        clocks, token = self, self.cfg["token"].encode()

        class Handler(BaseHTTPRequestHandler):
            server_version = "mochi-clocks"

            def log_message(self, *a):
                pass

            def authorized(self):
                got = self.headers.get("Authorization", "").removeprefix("Bearer ").encode()
                return hmac.compare_digest(got, token)

            def reply(self, code, body=b"", ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def json(self, obj, code=200):
                self.reply(code, json.dumps(obj, ensure_ascii=False).encode())

            def room(self, value):
                return value if value in ROOMS else "clock"

            def do_GET(self):
                if not self.authorized():
                    return self.reply(401)
                u = urlparse(self.path)
                if u.path == "/clock":
                    q = parse_qs(u.query)
                    room = self.room((q.get("room") or [""])[0])
                    with clocks.lock:
                        clocks.seen.setdefault(room, {})["polled"] = round(time.time())
                    return self.json(clocks.feed())
                if u.path in ("/clock/sheet.png", "/clock/sheet.json"):
                    f = SHEET_DIR / u.path.rsplit("/", 1)[1]
                    try:
                        data = f.read_bytes()
                    except OSError:
                        return self.reply(404)
                    return self.reply(200, data, "image/png" if f.suffix == ".png" else "application/json")
                self.reply(404)

            def do_POST(self):
                if not self.authorized():
                    return self.reply(401)
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(min(n, MAX_BODY)) or b"{}") if n <= MAX_BODY else None
                except ValueError:
                    body = None
                if not isinstance(body, dict):
                    return self.reply(400)
                room = self.room(str(body.get("room", "")))
                path = urlparse(self.path).path
                if path == "/clock/answer":
                    return self.json({"ok": clocks.answer(body, room)})
                if path == "/clock/seen":
                    what = str(body.get("what", ""))
                    if what not in ("touch", "lights on", "lights off"):
                        return self.reply(400)
                    lux = body.get("lux")
                    clocks.note(room, what, lux if isinstance(lux, (int, float)) else None)
                    return self.json({"ok": True})
                if path == "/clock/hush":
                    clocks.hush(body.get("id", ""))
                    return self.json({"ok": True})
                self.reply(404)

        def run():
            try:
                srv = ThreadingHTTPServer(("0.0.0.0", int(self.cfg.get("port") or PORT)), Handler)
            except OSError as e:
                self.log(f"clocks: can't listen on {self.cfg.get('port') or PORT}: {e}")
                return
            srv.daemon_threads = True
            self.log(f"clocks: listening on {srv.server_address[1]}")
            srv.serve_forever()

        threading.Thread(target=run, daemon=True).start()


def stamp(t):
    d = dt.datetime.fromtimestamp(t)
    return f"{d:%H:%M}" if d.date() == dt.date.today() else f"{d:%a %H:%M}"


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "sheet":
        c = Clocks(dict, lambda: None)
        c.redraw(wait=True)
        print(SHEET_DIR / "sheet.png")
    elif cmd == "status":
        c = Clocks(dict, lambda: None)
        print("\n".join(c.status()))
    elif cmd == "token":  # for setting up a clock: MOCHI_TOKEN in its service
        print(config()["token"])
    else:
        print(__doc__)
        print("usage: mochi-clocks [status | sheet | token]")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
