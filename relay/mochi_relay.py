#!/usr/bin/env python3
"""mochi-relay: a blind mailbox between Mochi's desk and Mochi on the phone.

Two boxes, "desk" and "phone", each a directory of opaque blobs. The phone's token may push into "desk" and pull
from "phone"; the desk's token the other way round. The relay never sees plaintext (the ends encrypt to each other,
see brain/mochi_phone.py and app/), keeps nothing after delivery is acknowledged, and throws away anything older
than KEEP_DAYS. Stdlib only, one process, ~20 MB, meant to sit next to a Minecraft server on a small box.

  POST /v1/push/{box}                 body = blob (<= MAX_BLOB bytes)        -> {"id": "..."}
  GET  /v1/pull/{box}?wait=25&ack=a,b  long-polls up to `wait` s for items    -> {"items": [{"id", "t", "b64"}]}
  GET  /v1/health                                                            -> "ok"
  PUT  /v1/beacon/{node}              body = JSON (<= 4 KB), the node's heartbeat -> {"ok": true}
  GET  /v1/beacons                    every node's last heartbeat              -> {"node": {"at", "beacon"}}

Beacons are the brain nodes' heartbeats for choosing which one leads (see ha/mochi_ha.py); they take the "ha"
token (the Windows seat agent's own "seat" token: its one beacon only), are held in memory only, and are the one thing here that isn't opaque (no secrets in them, by design).

Auth: `Authorization: Bearer TOKEN`, tokens in /etc/mochi-relay/tokens.json ({"desk": "...", "phone": "..."}).
TLS: /etc/mochi-relay/tls/fullchain.pem + privkey.pem (copied there by certbot's deploy hook); without them it
serves plain HTTP (for a reverse proxy or a test).
"""
import base64
import hmac
import json
import os
import secrets
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

STATE = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/mochi-relay"))
CONF = Path(os.environ.get("CONFIGURATION_DIRECTORY", "/etc/mochi-relay"))
PORT = int(os.environ.get("PORT", "443"))
BOXES = ("desk", "phone")
MAX_BLOB = 8 * 1024 * 1024   # a picture or a clip from the phone
MAX_WAIT = 50
BATCH = 50
KEEP_DAYS = 7

MAX_BEACON = 4096
BEACONS = {}  # node -> {"at": epoch, "beacon": {...}}; in memory: a restart forgets them within one heartbeat

TOKENS = {}
COND = {b: threading.Condition() for b in BOXES}


def load_tokens():
    TOKENS.update(json.loads((CONF / "tokens.json").read_text()))
    for b in BOXES:
        if not TOKENS.get(b):
            sys.exit(f"tokens.json needs a token for {b}")
    # "ha" (the brain nodes' heartbeats) is optional: without it the beacon routes answer 401


def box_dir(box):
    d = STATE / "boxes" / box
    d.mkdir(parents=True, exist_ok=True)
    return d


def prune():
    """Anything older than KEEP_DAYS was never collected; it is not ours to keep."""
    cutoff = time.time() - KEEP_DAYS * 86400
    for b in BOXES:
        for f in box_dir(b).glob("*.bin"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass


def pruner():
    while True:
        prune()
        time.sleep(3600)


class Handler(BaseHTTPRequestHandler):
    server_version = "mochi-relay/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # one line per request, no bodies, no tokens
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def send(self, code, body=b"", ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def who(self):
        """Which side is talking, from the bearer token (compared in constant time)."""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        tok = auth[7:].strip()
        for side, t in TOKENS.items():
            if hmac.compare_digest(tok, t):
                return side
        return None

    def route(self):
        u = urlsplit(self.path)
        parts = u.path.strip("/").split("/")
        q = parse_qs(u.query)
        if parts == ["v1", "health"]:
            return self.send(200, "ok", "text/plain")
        if parts[:2] in (["v1", "beacon"], ["v1", "beacons"]):
            return self.beacon(parts)
        if len(parts) != 3 or parts[0] != "v1" or parts[1] not in ("push", "pull") or parts[2] not in BOXES:
            return self.send(404, {"error": "no such route"})
        side = self.who()
        if side not in BOXES:  # no token, or the heartbeat token: the mailboxes are only the desk's and the phone's
            return self.send(401, {"error": "bad token"})
        op, box = parts[1], parts[2]
        # the phone pushes into the desk's box and pulls its own; the desk the other way round
        allowed = (op == "pull" and box == side) or (op == "push" and box != side)
        if not allowed:
            return self.send(403, {"error": f"{side} may not {op} {box}"})
        return self.push(box) if op == "push" else self.pull(box, q)

    def do_GET(self):
        if self.path.startswith("/v1/push/"):
            return self.send(405, {"error": "push is POST"})
        self.route()

    def do_PUT(self):
        self.route()

    def do_POST(self):
        if self.path.startswith("/v1/pull/"):
            return self.send(405, {"error": "pull is GET"})
        self.route()

    def beacon(self, parts):
        who = self.who()
        # "seat": the little agent on his Windows install may only say he's at the PC (PUT /v1/beacon/windows)
        seat_ok = who == "seat" and self.command == "PUT" and parts == ["v1", "beacon", "windows"]
        if who != "ha" and not seat_ok:
            return self.send(401, {"error": "bad token"})
        if parts == ["v1", "beacons"] and self.command == "GET":
            return self.send(200, BEACONS)
        if len(parts) != 3 or self.command != "PUT" or not parts[2].isidentifier() or len(parts[2]) > 32:
            return self.send(404, {"error": "no such route"})
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BEACON:
            return self.send(413, {"error": f"beacon must be 1..{MAX_BEACON} bytes"})
        try:
            b = json.loads(self.rfile.read(n))
        except ValueError:
            return self.send(400, {"error": "not json"})
        if len(BEACONS) >= 16 and parts[2] not in BEACONS:
            return self.send(429, {"error": "too many nodes"})
        BEACONS[parts[2]] = {"at": time.time(), "beacon": b}
        self.send(200, {"ok": True, "now": time.time()})

    def push(self, box):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BLOB:
            return self.send(413, {"error": f"blob must be 1..{MAX_BLOB} bytes"})
        blob = self.rfile.read(n)
        mid = f"{int(time.time() * 1000):013d}-{secrets.token_hex(4)}"
        tmp = box_dir(box) / (mid + ".tmp")
        tmp.write_bytes(blob)
        tmp.rename(tmp.with_suffix(".bin"))
        with COND[box]:
            COND[box].notify_all()
        self.send(200, {"id": mid})

    def pull(self, box, q):
        d = box_dir(box)
        for mid in (q.get("ack", [""])[0]).split(","):
            if mid and "/" not in mid and ".." not in mid:
                (d / (mid + ".bin")).unlink(missing_ok=True)
        wait = min(max(int(q.get("wait", ["0"])[0] or 0), 0), MAX_WAIT)
        deadline = time.time() + wait
        while True:
            files = sorted(d.glob("*.bin"))[:BATCH]
            if files or time.time() >= deadline:
                break
            with COND[box]:
                COND[box].wait(timeout=max(0.1, deadline - time.time()))
        items = []
        for f in files:
            try:
                items.append({"id": f.stem, "t": int(f.stat().st_mtime), "b64": base64.b64encode(f.read_bytes()).decode()})
            except OSError:
                pass
        self.send(200, {"items": items})


def main():
    load_tokens()
    threading.Thread(target=pruner, daemon=True).start()
    srv = ThreadingHTTPServer(("", PORT), Handler)
    srv.daemon_threads = True
    cert, key = CONF / "tls" / "fullchain.pem", CONF / "tls" / "privkey.pem"
    if cert.exists() and key.exists():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        print(f"mochi-relay: https on :{PORT}", file=sys.stderr)
    else:
        print(f"mochi-relay: plain http on :{PORT} (no tls files in {CONF / 'tls'})", file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
