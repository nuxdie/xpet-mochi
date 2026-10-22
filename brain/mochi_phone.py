#!/usr/bin/env python3
"""mochi-phone: Mochi's end of the line to Mochi on their phone (app/ in the repo).

The phone app reports where they are and how the phone is doing; Mochi can send it a few things back. Both ends
talk through a blind relay (relay/mochi_relay.py on mc.tsatsin.com) that stores only ciphertext: every message is
sealed end to end with X25519 + HKDF-SHA256 + AES-256-GCM, with an ephemeral key per message (forward secrecy) and the
sender's static key mixed in (the receiver knows who sealed it). The relay token only lets you fetch blobs.

  mochi-phone pair [--json]      make the keys if needed and print the phone's config (app/install.sh ships it)
  mochi-phone serve              the daemon: pulls, decrypts, writes phone/latest.json + phone/events.jsonl, triggers
  mochi-phone where [--days N]   where they are, for people and for mochi-sense
  mochi-phone now                what the phone is doing right now: playing, audio route, ringer, battery
  mochi-phone notifs [--hours N] [--app WORD]   the shade: notifications lately (messages, mail, deliveries, alerts)
  mochi-phone calls [--days N]   calls in and out, missed
  mochi-phone sms [--hours N]    texts in and out
  mochi-phone usage [--days N]   which apps they used, minutes per day
  mochi-phone digest             all of the above in one page, for the senses digest
  mochi-phone feed               JSON for the relay's live feed: what's new that may deserve a catch-up
  mochi-phone status             the line, the keys, the last word from the phone
  mochi-phone events [--hours N] the raw events lately
  mochi-phone send locate | lock | buzz "TEXT" | ask "TEXT" --options "A,B,C" [--id ID] [--link URL] [--buzz] | close --id ID
  mochi-phone send photo [--front] [--wait S] | send audio [--seconds N] [--wait S]   a still, a clip (then transcribed)
  mochi-phone media [--days N]   the pictures and clips that came back, with transcripts
  mochi-phone transcribe FILE    (what `serve` does for clips: faster-whisper in Mochi's tools venv)
  mochi-phone log                what was sent
  mochi-phone tracks [--dry-run] each finished outing as a GPX into wanderer on sff: walk/bike/transit by speed,
                                 Uber Eats shifts as Biking, named after the calendar event it went to
  mochi-phone place NAME [--here | LAT LON] [--radius M] | place --list   named places ("home" first): the zones

Config: ~/.config/mochi/phone.json (relay, tokens, keys, home). Data: ~/.local/share/mochi/phone/.
"""
import base64
import datetime as dt
import email
import email.policy
import json
import math
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

CONFIG = Path(os.environ.get("MOCHI_PHONE_CONFIG", Path.home() / ".config/mochi/phone.json"))
WORK = Path(os.environ.get("MOCHI_HOME", Path.home() / ".local/share/mochi"))
DATA = WORK / "phone"
MEDIA = DATA / "media"
WHISPER = Path.home() / ".local/share/mochi-tools/bin/python"
LATEST, EVENTS, SENT, GEOCACHE, STATE = DATA / "latest.json", DATA / "events.jsonl", DATA / "sent.log", DATA / "geocache.json", DATA / "state.json"
MAGIC = b"M1"
INFO = b"mochi-phone-v1"
MAX_AGE = 2 * 86400          # a message older than this is replayed or lost in the relay for too long: drop it
HOME_RADIUS = 150            # metres; "home" is being within this of the home point
GEOCODE_MIN_MOVE = 120       # metres before asking Nominatim for a new address
USER_AGENT = "mochi-phone/1 (a desktop companion; one lookup per move)"


def b64(b):
    return base64.b64encode(b).decode()


def unb64(s):
    return base64.b64decode(s)


def load_config():
    if not CONFIG.exists():
        sys.exit(f"no {CONFIG}: run the relay's deploy.sh, then `mochi-phone pair`")
    return json.loads(CONFIG.read_text())


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, indent=1))
    CONFIG.chmod(0o600)


def jload(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def jsave(path, obj):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False))
    tmp.rename(path)


# ---- crypto --------------------------------------------------------------------------------------------

def seal(plain: bytes, my_priv: X25519PrivateKey, peer_pub: X25519PublicKey) -> bytes:
    """MAGIC | eph_pub | nonce | AES-GCM(ct+tag). Key from dh(eph, peer) || dh(me, peer); AAD = MAGIC | eph_pub."""
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes_raw()
    ikm = eph.exchange(peer_pub) + my_priv.exchange(peer_pub)
    salt = eph_pub + my_priv.public_key().public_bytes_raw() + peer_pub.public_bytes_raw()
    key = HKDF(hashes.SHA256(), 32, salt, INFO).derive(ikm)
    nonce = secrets.token_bytes(12)
    return MAGIC + eph_pub + nonce + AESGCM(key).encrypt(nonce, plain, MAGIC + eph_pub)


def open_sealed(blob: bytes, my_priv: X25519PrivateKey, peer_pub: X25519PublicKey) -> bytes:
    if len(blob) < 2 + 32 + 12 + 16 or blob[:2] != MAGIC:
        raise ValueError("not a mochi blob")
    eph_pub_raw, nonce, ct = blob[2:34], blob[34:46], blob[46:]
    eph_pub = X25519PublicKey.from_public_bytes(eph_pub_raw)
    ikm = my_priv.exchange(eph_pub) + my_priv.exchange(peer_pub)
    salt = eph_pub_raw + peer_pub.public_bytes_raw() + my_priv.public_key().public_bytes_raw()
    key = HKDF(hashes.SHA256(), 32, salt, INFO).derive(ikm)
    return AESGCM(key).decrypt(nonce, ct, MAGIC + eph_pub_raw)


def keys(cfg):
    return X25519PrivateKey.from_private_bytes(unb64(cfg["desk_priv"])), X25519PublicKey.from_public_bytes(unb64(cfg["phone_pub"]))


# ---- relay ---------------------------------------------------------------------------------------------

def relay(cfg, method, path, body=None, timeout=60):
    req = urllib.request.Request(cfg["relay"].rstrip("/") + path, data=body, method=method,
                                 headers={"Authorization": "Bearer " + cfg["desk_token"], "Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def push(cfg, msg):
    """Seal a message for the phone and leave it in the relay. Returns the relay id."""
    priv, phone_pub = keys(cfg)
    msg = {"id": msg.get("id") or secrets.token_hex(6), "t": int(time.time()), **msg}
    rid = relay(cfg, "POST", "/v1/push/phone", seal(json.dumps(msg).encode(), priv, phone_pub), timeout=20)["id"]
    DATA.mkdir(parents=True, exist_ok=True)
    with SENT.open("a") as f:
        f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M} {msg['type']} {json.dumps({k: v for k, v in msg.items() if k not in ('type', 't')}, ensure_ascii=False)[:300]}\n")
    return rid


# ---- geography -----------------------------------------------------------------------------------------

def distance_m(a_lat, a_lon, b_lat, b_lon):
    p = math.pi / 180
    h = 0.5 - math.cos((b_lat - a_lat) * p) / 2 + math.cos(a_lat * p) * math.cos(b_lat * p) * (1 - math.cos((b_lon - a_lon) * p)) / 2
    return 12742000 * math.asin(math.sqrt(h))


def zone_of(cfg, lat, lon, acc):
    """'home', a named place from config, or 'away'. Places: cfg["places"] = {"home": [lat, lon, radius?], ...}."""
    best = None
    for name, spec in (cfg.get("places") or {}).items():
        r = spec[2] if len(spec) > 2 else HOME_RADIUS
        d = distance_m(lat, lon, spec[0], spec[1])
        if d <= r + min(acc or 0, 100) and (best is None or d < best[1]):
            best = (name, d)
    return best[0] if best else "away"


def geocode(lat, lon):
    """Street-level address from OpenStreetMap's Nominatim, cached by ~10 m cell; never more than one call per move."""
    cache = jload(GEOCACHE, {})
    key = f"{lat:.4f},{lon:.4f}"
    if key in cache:
        return cache[key]
    for k, v in cache.items():  # anything cached within GEOCODE_MIN_MOVE is close enough
        klat, klon = map(float, k.split(","))
        if distance_m(lat, lon, klat, klon) < GEOCODE_MIN_MOVE:
            return v
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=17&lat={lat}&lon={lon}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.loads(r.read().decode())
        a = j.get("address") or {}
        parts = [a.get("amenity") or a.get("shop") or a.get("building") or "",
                 " ".join(x for x in (a.get("road"), a.get("house_number")) if x),
                 a.get("neighbourhood") or a.get("suburb") or "",
                 a.get("city") or a.get("town") or a.get("village") or a.get("municipality") or ""]
        text = ", ".join(p for p in parts if p) or j.get("display_name", "")[:80]
    except (urllib.error.URLError, OSError, ValueError):
        return ""
    cache[key] = text
    if len(cache) > 2000:
        cache = dict(list(cache.items())[-1500:])
    jsave(GEOCACHE, cache)
    return text


# ---- the daemon ----------------------------------------------------------------------------------------

def log(msg):
    print(f"{dt.datetime.now():%H:%M:%S} {msg}", file=sys.stderr, flush=True)


def trigger(text):
    try:
        subprocess.run(["mochi-brain", "--trigger", text], timeout=20, capture_output=True)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"trigger failed: {e}")


def apply_events(cfg, events, latest, state):
    """Fold a batch of phone events into latest.json; append them to events.jsonl; notice what changed."""
    DATA.mkdir(parents=True, exist_ok=True)
    news = []
    with EVENTS.open("a") as f:
        for ev in events:
            ev.setdefault("rx", int(time.time()))
            f.write(json.dumps({k: v for k, v in ev.items() if k != "b64"}, ensure_ascii=False) + "\n")
            kind = ev.get("type")
            if kind == "loc":
                lat, lon, acc = ev.get("lat"), ev.get("lon"), ev.get("acc")
                if lat is None or lon is None:
                    continue
                zone = zone_of(cfg, lat, lon, acc)
                prev = latest.get("loc") or {}
                moved = distance_m(lat, lon, prev["lat"], prev["lon"]) if prev.get("lat") is not None else 1e9
                addr = prev.get("address", "") if moved < GEOCODE_MIN_MOVE and prev.get("address") else geocode(lat, lon)
                if zone != prev.get("zone"):
                    latest["zone_since"] = ev.get("t", int(time.time()))
                    if prev.get("zone"):
                        news.append(f"their phone: {'home' if zone == 'home' else zone if zone != 'away' else 'left home'}"
                                    if zone != "away" else f"their phone left {prev.get('zone')}")
                latest["loc"] = {"lat": lat, "lon": lon, "acc": acc, "alt": ev.get("alt"), "speed": ev.get("speed"),
                                 "t": ev.get("t"), "zone": zone, "address": addr, "provider": ev.get("provider")}
            elif kind == "state":
                st = latest.setdefault("state", {})
                st.update({k: v for k, v in ev.items() if k not in ("type", "rx")})
            elif kind == "hello":
                latest["app"] = {k: v for k, v in ev.items() if k not in ("type", "rx")}
            elif kind in ("notif", "call", "sms"):
                ring = latest.setdefault(kind + "s", [])
                ring.append({k: v for k, v in ev.items() if k not in ("type", "rx")})
                del ring[:-(200 if kind == "notif" else 60)]
            elif kind == "media":
                latest["media"] = {k: v for k, v in ev.items() if k not in ("type", "rx")}
            elif kind in ("photo", "audio"):
                cap = save_capture(ev)
                if cap:
                    caps = latest.setdefault("captures", [])
                    caps.append(cap)
                    del caps[:-100]
            elif kind == "usage":
                latest.setdefault("usage", {})[ev.get("day", "")] = ev.get("apps") or {}
                for day in sorted(latest["usage"])[:-14]:
                    del latest["usage"][day]
            elif kind == "reply":
                replies = state.setdefault("replies", [])
                replies.append(ev)
                del replies[:-50]
                trigger(f"from their phone, to ask {ev.get('ask_id')}: {ev.get('option')}")
                try:  # hand the answer to the relay as if they'd clicked the pig's panel
                    subprocess.run(["mochi-brain", "--answer", str(ev.get("ask_id")), str(ev.get("option"))], timeout=20, capture_output=True)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            elif kind == "note":
                trigger(f"from their phone: {str(ev.get('text'))[:300]}")
            latest["last_event"] = ev.get("t", int(time.time()))
    latest["last_seen"] = int(time.time())
    jsave(LATEST, latest)
    for n in news:
        log(n)
        trigger(n)


def save_capture(ev):
    """A picture or a clip from the phone: to phone/media/, a clip transcribed. Returns the record for latest.json."""
    MEDIA.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.fromtimestamp(ev.get("t", time.time())).strftime("%Y%m%d-%H%M%S")
    rec = {"t": ev.get("t"), "kind": ev["type"], "req": ev.get("req"), "error": ev.get("error")}
    if ev.get("error") or not ev.get("b64"):
        log(f"{ev['type']} from the phone failed: {ev.get('error')}")
        return rec
    ext = ".jpg" if ev["type"] == "photo" else ".m4a"
    path = MEDIA / f"{stamp}-{ev['type']}{'-' + ev['cam'] if ev.get('cam') else ''}{ext}"
    path.write_bytes(unb64(ev["b64"]))
    rec["path"] = str(path)
    if ev.get("cam"):
        rec["cam"] = ev["cam"]
    if ev["type"] == "audio":
        rec["seconds"] = ev.get("seconds")
        text = transcribe_file(path)
        if text is not None:
            path.with_suffix(".txt").write_text(text)
            rec["text"] = text[:2000]
    log(f"{ev['type']} from the phone: {path.name} ({len(ev['b64']) * 3 // 4} bytes)")
    return rec


def transcribe_file(path):
    if not WHISPER.exists():
        return None
    # ffmpeg decodes (faster-whisper's own decoder, PyAV, breaks across versions); whisper gets raw 16 kHz samples
    code = ("import sys, subprocess, numpy as np\nfrom faster_whisper import WhisperModel\n"
            "raw = subprocess.run(['ffmpeg', '-v', 'quiet', '-i', sys.argv[1], '-f', 'f32le', '-ac', '1', '-ar', '16000', '-'],"
            " capture_output=True, check=True).stdout\n"
            "audio = np.frombuffer(raw, dtype=np.float32)\n"
            "m = WhisperModel('small', device='cpu', compute_type='int8')\n"
            "segs, info = m.transcribe(audio, vad_filter=True)\n"
            "print(f'[{info.language}] ' + ' '.join(s.text.strip() for s in segs))")
    try:
        p = subprocess.run([str(WHISPER), "-c", code, str(path)], capture_output=True, text=True, timeout=600)
        return p.stdout.strip() if p.returncode == 0 else f"(transcription failed: {p.stderr.strip()[-200:]})"
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"(transcription failed: {e})"


def transcribe(args):
    pos = positional(args)
    if not pos:
        sys.exit("transcribe FILE")
    print(transcribe_file(Path(pos[0])))


def media(args):
    days = int(opt(args, "--days") or 2)
    since = time.time() - days * 86400
    caps = [c for c in (jload(LATEST, {}).get("captures") or []) if (c.get("t") or 0) >= since]
    if not caps:
        return f"_nothing captured in the last {days} day(s)_"
    out = []
    for c in caps:
        line = f"{dt.datetime.fromtimestamp(c['t']):%a %H:%M} {c['kind']}" + (f" ({c['cam']})" if c.get("cam") else "") + \
               (f" {c['seconds']}s" if c.get("seconds") else "")
        line += f": {c['path']}" if c.get("path") else f": failed ({c.get('error')})"
        if c.get("text"):
            line += f"\n    {c['text'][:300]}"
        out.append(line)
    return "\n".join(out)


def serve(_args):
    cfg = load_config()
    priv, phone_pub = keys(cfg)
    state = jload(STATE, {"seen": []})
    latest = jload(LATEST, {})
    backoff = 5
    last_mail = 0
    log("mochi-phone: serving")
    while True:
        if time.time() - last_mail > 300:
            last_mail = time.time()
            mail_ingest(load_config(), priv, phone_pub, state, latest)
            jsave(STATE, state)
        try:
            ack = ",".join(state.get("ack", []))
            r = relay(cfg, "GET", f"/v1/pull/desk?wait=50&ack={urllib.parse.quote(ack)}", timeout=70)
            backoff = 5
        except (urllib.error.URLError, OSError, ValueError) as e:
            log(f"relay: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
            continue
        state["ack"] = []
        batch = []
        for it in r.get("items", []):
            state["ack"].append(it["id"])  # acked next pull, after we wrote it down
            try:
                msg = json.loads(open_sealed(unb64(it["b64"]), priv, phone_pub).decode())
            except Exception as e:  # a blob we can't open is noise (wrong key, corrupted); don't let it stop the line
                log(f"unreadable blob {it['id']}: {e!r}")
                continue
            mid, t = msg.get("id"), msg.get("t", 0)
            if mid in state["seen"] or abs(time.time() - t) > MAX_AGE:
                log(f"dropping {mid}: replay or too old")
                continue
            state["seen"] = (state["seen"] + [mid])[-2000:]
            batch.extend(msg.get("events") or [])
        if batch:
            cfg = load_config()  # places may have been named since we started
            apply_events(cfg, batch, latest, state)
        jsave(STATE, state)


# ---- reading it back -----------------------------------------------------------------------------------

def ago(ts):
    if not ts:
        return "?"
    s = int(time.time() - ts)
    return f"{s // 60}m ago" if s < 3600 else f"{s // 3600}h{(s % 3600) // 60:02d}m ago" if s < 86400 else f"{s // 86400}d ago"


def hm(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M")


def read_events(since_ts):
    out = []
    try:
        with EVENTS.open() as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("t", 0) >= since_ts:
                    out.append(ev)
    except OSError:
        pass
    return out


def stops(cfg, events, stay_min=8, radius=120):
    """Group the day's fixes into stays: places they were for at least stay_min minutes."""
    locs = [e for e in events if e.get("type") == "loc" and e.get("lat") is not None]
    locs.sort(key=lambda e: e.get("t", 0))
    out = []
    cur = None
    for e in locs:
        if cur and distance_m(e["lat"], e["lon"], cur["lat"], cur["lon"]) <= radius:
            cur["end"] = e["t"]
            continue
        if cur and cur["end"] - cur["start"] >= stay_min * 60:
            out.append(cur)
        cur = {"lat": e["lat"], "lon": e["lon"], "start": e["t"], "end": e["t"]}
    if cur and cur["end"] - cur["start"] >= stay_min * 60:
        out.append(cur)
    for s in out:
        s["zone"] = zone_of(cfg, s["lat"], s["lon"], 0)
        s["where"] = s["zone"] if s["zone"] != "away" else (geocode(s["lat"], s["lon"]) or f"{s['lat']:.4f},{s['lon']:.4f}")
    return out


def where(args):
    cfg = load_config()
    latest = jload(LATEST, {})
    out = ["## Where they are (their phone)"]
    loc, st = latest.get("loc"), latest.get("state") or {}
    if not latest:
        return "\n".join(out + ["_nothing from the phone yet (is the app installed and running? `mochi-phone status`)_"])
    if loc:
        place = loc["zone"] if loc["zone"] != "away" else "out"
        line = f"**{place}**"
        if loc.get("address") and loc["zone"] == "away":
            line += f" — {loc['address']}"
        if latest.get("zone_since"):
            line += f" since {hm(latest['zone_since'])}"
        line += f"; fix {ago(loc.get('t'))} (±{int(loc.get('acc') or 0)} m"
        if loc.get("speed") and loc["speed"] > 1:
            line += f", moving {loc['speed'] * 3.6:.0f} km/h"
        line += ")"
        out.append(line)
    else:
        out.append("_no location yet_")
    bits = []
    if st.get("battery") is not None:
        bits.append(f"battery {st['battery']}%" + (" charging" if st.get("charging") else ""))
    if st.get("wifi"):
        bits.append(f"Wi-Fi {st['wifi']}")
    elif st.get("net"):
        bits.append(st["net"])
    if st.get("screen") is not None:
        bits.append("screen on" if st["screen"] else "screen off")
    if st.get("audio"):
        bits.append("sound to " + ", ".join(st["audio"]))
    if st.get("ringer") in ("silent", "vibrate"):
        bits.append(st["ringer"])
    if st.get("dnd"):
        bits.append("do not disturb")
    m = latest.get("media") or {}
    if m.get("playing") and time.time() - (m.get("t") or 0) < 6 * 3600:
        bits.append(f"playing {m.get('title')}" + (f" — {m['artist']}" if m.get("artist") else "") + f" ({m.get('label')})")
    if bits:
        out.append("phone: " + ", ".join(bits) + f" (heard {ago(latest.get('last_seen'))})")
    stale = time.time() - (latest.get("last_seen") or 0)
    if stale > 3 * 3600:
        out.append(f"_the phone hasn't reported for {ago(latest.get('last_seen'))}: off, no network, or the app was stopped_")
    days = int(opt(args, "--days") or 1)
    start = (dt.datetime.now() - dt.timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    ss = stops(cfg, read_events(start.timestamp()))
    if ss:
        label = "Today" if days == 1 else f"Last {days} days"
        out.append(f"**{label}**: " + " → ".join(
            f"{hm(s['start'])}–{hm(s['end'])} {s['where']}" + ("" if days == 1 else f" ({dt.datetime.fromtimestamp(s['start']):%a})") for s in ss[-30:]))
    return "\n".join(out)


MESSAGING = ("whatsapp", "signal", "telegram", "messaging", "messenger", "instagram", "slack", "discord", "linkedin",
             "viber", "threema", "snapchat", "teams")


def notifs(args):
    hours = float(opt(args, "--hours") or 6)
    app = (opt(args, "--app") or "").lower()
    out = []
    for ev in read_events(time.time() - hours * 3600):
        if ev.get("type") != "notif":
            continue
        if app and app not in (ev.get("app", "") + " " + ev.get("label", "")).lower():
            continue
        who = ev.get("sender") or ev.get("title") or ""
        conv = f" [{ev['conv']}]" if ev.get("conv") and ev["conv"] != who else ""
        out.append(f"{hm(ev['t'])} {ev.get('label', ev.get('app'))}: {who}{conv}: {ev.get('text', '')}".rstrip(": "))
    return "\n".join(out) or f"_no notifications in the last {hours:g} h_"


def calls(args):
    days = int(opt(args, "--days") or 2)
    start = (dt.datetime.now() - dt.timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for ev in read_events(start.timestamp()):
        if ev.get("type") != "call":
            continue
        who = ev.get("name") or ev.get("number") or "unknown"
        dur = f", {int(ev.get('duration') or 0) // 60}m{int(ev.get('duration') or 0) % 60:02d}s" if ev.get("dir") in ("in", "out") else ""
        out.append(f"{dt.datetime.fromtimestamp(ev['t']):%a %H:%M} {ev.get('dir')}: {who}{dur}")
    return "\n".join(out) or f"_no calls in the last {days} day(s)_"


def sms(args):
    hours = float(opt(args, "--hours") or 24)
    out = []
    for ev in read_events(time.time() - hours * 3600):
        if ev.get("type") != "sms":
            continue
        who = ev.get("name") or ev.get("address") or "?"
        out.append(f"{hm(ev['t'])} {'from' if ev.get('dir') == 'in' else 'to'} {who}: {ev.get('text', '')}")
    return "\n".join(out) or f"_no texts in the last {hours:g} h_"


def usage(args):
    days = int(opt(args, "--days") or 1)
    u = jload(LATEST, {}).get("usage") or {}
    out = []
    for day in sorted(u)[-days:]:
        apps = sorted(u[day].items(), key=lambda x: -x[1])
        total = sum(u[day].values())
        out.append(f"{day}: {total // 3600}h{(total % 3600) // 60:02d}m on the phone; " +
                   ", ".join(f"{a} {sec // 60}m" for a, sec in apps[:10]))
    return "\n".join(out) or "_no usage data yet_"


def now(_args):
    latest = jload(LATEST, {})
    st, m = latest.get("state") or {}, latest.get("media") or {}
    out = []
    if m:
        out.append(("playing " if m.get("playing") else "paused: ") + f"{m.get('title')}" + (f" — {m['artist']}" if m.get("artist") else "") +
                   f" ({m.get('label')}, {ago(m.get('t'))})")
    out.append(f"sound to {', '.join(st['audio'])}" if st.get("audio") else "sound to the phone itself")
    out.append(f"ringer {st.get('ringer', '?')}" + (", do not disturb" if st.get("dnd") else ""))
    out.append(f"battery {st.get('battery', '?')}%{' charging' if st.get('charging') else ''}, " +
               (f"Wi-Fi {st['wifi']}" if st.get("wifi") else st.get("net", "?")) + f", screen {'on' if st.get('screen') else 'off'}")
    out.append(f"heard {ago(latest.get('last_seen'))}")
    return "\n".join(out)


def digest(_args):
    """The phone section of the senses digest: where, what it's doing, what came in."""
    latest = jload(LATEST, {})
    out = [where([]).replace("## Where they are (their phone)", "## Their phone")]
    if not latest:
        return out[0]
    since = time.time() - 3 * 3600
    evs = read_events(since)
    cs = [e for e in evs if e.get("type") == "call"]
    if cs:
        out.append("**Calls (3 h)**: " + "; ".join(f"{hm(e['t'])} {e.get('dir')} {e.get('name') or e.get('number')}" for e in cs[-8:]))
    ss = [e for e in evs if e.get("type") == "sms"]
    if ss:
        out.append("**Texts (3 h)**: " + "; ".join(f"{hm(e['t'])} {'from' if e.get('dir') == 'in' else 'to'} {e.get('name') or e.get('address')}: "
                                                  f"{e.get('text', '')[:60]}" for e in ss[-8:]))
    ns = [e for e in evs if e.get("type") == "notif"]
    if ns:
        by = {}
        for e in ns:
            by.setdefault(e.get("label") or e.get("app"), []).append(e)
        parts = []
        for label, es in sorted(by.items(), key=lambda x: -len(x[1]))[:10]:
            senders = sorted({(e.get("sender") or e.get("title") or "")[:30] for e in es} - {""})
            parts.append(f"{label} {len(es)}" + (f" ({', '.join(senders[:4])}{', …' if len(senders) > 4 else ''})" if senders else ""))
        out.append(f"**Notifications (3 h, {len(ns)})**: " + "; ".join(parts) + "  _(`mochi-phone notifs --hours 3 --app X`)_")
    u = (latest.get("usage") or {}).get(dt.date.today().isoformat())
    if u:
        apps = sorted(u.items(), key=lambda x: -x[1])[:6]
        out.append(f"**On the phone today**: {sum(u.values()) // 3600}h{(sum(u.values()) % 3600) // 60:02d}m; " +
                   ", ".join(f"{a} {sec // 60}m" for a, sec in apps))
    out.append("_Dig: `mochi-phone notifs|calls|sms|usage|now`, `mochi-phone where --days 3`_")
    return "\n".join(out)


def feed(_args):
    """For the relay's Feed (mochi-sense phone feed → Feed.take_phone): the last day's items that may be news, keyed
    stably so the relay can tell new from seen. Missed calls, texts in, and messages from people in messaging apps
    (grouped per app and sender per hour). Telegram is in too: the relay drops those while the archive is readable
    and uses them when it isn't (a backup channel, as they asked)."""
    items = {}
    for ev in read_events(time.time() - 86400):
        t, kind = ev.get("t", 0), ev.get("type")
        if kind == "call" and ev.get("dir") in ("missed", "in", "rejected"):
            who = ev.get("name") or ev.get("number") or "unknown"
            items[f"phone:call:{ev.get('id', t)}"] = {"line": f"phone: {ev['dir']} call from {who} at {hm(t)}; see: `mochi-phone calls`", "at": t}
        elif kind == "sms" and ev.get("dir") == "in":
            who = ev.get("name") or ev.get("address") or "?"
            items[f"phone:sms:{ev.get('id', t)}"] = {"line": f"phone: a text from {who} at {hm(t)}; read: `mochi-phone sms --hours 6`", "at": t}
        elif kind == "notif":
            app = (ev.get("app", "") + " " + ev.get("label", "")).lower()
            if not any(m in app for m in MESSAGING):
                continue
            who = ev.get("sender") or ev.get("title") or ""
            if not who or ev.get("cat") in ("service", "progress", "promo", "recommendation"):
                continue
            key = f"phone:notif:{ev.get('label')}:{who}:{int(t // 3600)}"
            it = items.setdefault(key, {"line": "", "at": t, "n": 0})
            it["n"] += 1
            it["line"] = (f"phone: {it['n']} message(s) from {who} in {ev.get('label')} around {hm(t)}; read: "
                          f"`mochi-phone notifs --hours 3 --app \"{ev.get('label')}\"`")
    return json.dumps({"ok": True, "items": {k: {"line": v["line"], "at": v["at"]} for k, v in items.items()}})


def status(_args):
    cfg = load_config()
    latest = jload(LATEST, {})
    try:
        ok = urllib.request.urlopen(cfg["relay"] + "/v1/health", timeout=10).read().decode().strip()
    except (urllib.error.URLError, OSError) as e:
        ok = f"unreachable ({e})"
    print(f"relay   {cfg['relay']}: {ok}")
    print(f"keys    desk {'ok' if cfg.get('desk_priv') else 'MISSING'}, phone pub {'ok' if cfg.get('phone_pub') else 'MISSING (pair)'}")
    print(f"places  {', '.join((cfg.get('places') or {}).keys()) or 'none set (places in ' + str(CONFIG) + ')'}")
    app = latest.get("app") or {}
    print(f"phone   last heard {ago(latest.get('last_seen'))}; app {app.get('version', '?')} said hello {ago(app.get('t'))}")
    p = subprocess.run(["systemctl", "--user", "is-active", "mochi-phone"], capture_output=True, text=True)
    print(f"daemon  {p.stdout.strip() or 'not installed'}")


def events(args):
    hours = float(opt(args, "--hours") or 6)
    for ev in read_events(time.time() - hours * 3600):
        print(hm(ev.get("t", 0)), json.dumps({k: v for k, v in ev.items() if k not in ("t", "rx")}, ensure_ascii=False))


def send(args):
    cfg = load_config()
    if not cfg.get("phone_pub"):
        sys.exit("not paired: mochi-phone pair, then app/install.sh")
    pos = positional(args)
    if not pos:
        sys.exit("send locate | buzz TEXT | ask TEXT --options A,B,C [--id ID]")
    kind = pos[0]
    if kind == "locate":
        msg = {"type": "locate"}
    elif kind == "buzz":
        msg = {"type": "buzz", "text": " ".join(pos[1:])[:300]}
    elif kind == "ask":
        opts = [o.strip() for o in (opt(args, "--options") or "Yes,Not now").split(",") if o.strip()][:3]
        msg = {"type": "ask", "ask_id": opt(args, "--id") or secrets.token_hex(3), "text": " ".join(pos[1:])[:300], "options": opts}
        if opt(args, "--link"):  # a tap on the notification opens it (a Maps link, a geo: URI)
            msg["link"] = opt(args, "--link")[:500]
        if "--buzz" in args:
            msg["buzz"] = True
    elif kind == "close":  # take an ask's notification back (answered elsewhere, or no longer true)
        msg = {"type": "close", "ask_id": opt(args, "--id") or sys.exit("send close --id ASK_ID")}
    elif kind == "lock":  # the sleep wall (mochi-bedtime): lock the screen, like the desk's
        msg = {"type": "lock"}
    elif kind == "photo":
        msg = {"type": "photo", "cam": "front" if "--front" in args else "back"}
    elif kind == "audio":
        msg = {"type": "audio", "seconds": int(opt(args, "--seconds") or 10)}
    else:
        sys.exit(f"unknown: {kind}")
    msg["id"] = secrets.token_hex(6)
    push(cfg, msg)
    wait = int(opt(args, "--wait") or 0)
    if not wait:
        print(msg["id"])
        return
    # wait for the capture to come back through serve (which writes latest.json), then say where it landed
    deadline = time.time() + wait
    while time.time() < deadline:
        for c in (jload(LATEST, {}).get("captures") or [])[::-1]:
            if c.get("req") == msg["id"]:
                if c.get("path"):
                    print(c["path"])
                    if c.get("text"):
                        print(c["text"])
                    return
                sys.exit(f"the phone couldn't: {c.get('error')}")
        time.sleep(2)
    sys.exit(f"no {kind} back within {wait}s (is the phone online? `mochi-phone status`); it may still arrive: `mochi-phone media`")


def pair(args):
    """Make both keypairs (the phone's private key travels once, over adb, with install.sh) and print the phone's config."""
    cfg = load_config()
    if not cfg.get("desk_priv"):
        k = X25519PrivateKey.generate()
        cfg["desk_priv"], cfg["desk_pub"] = b64(k.private_bytes_raw()), b64(k.public_key().public_bytes_raw())
    if not cfg.get("phone_priv") or "--new" in args:
        k = X25519PrivateKey.generate()
        cfg["phone_priv"], cfg["phone_pub"] = b64(k.private_bytes_raw()), b64(k.public_key().public_bytes_raw())
    save_config(cfg)
    phone = {"relay": cfg["relay"], "token": cfg["phone_token"], "phone_priv": cfg["phone_priv"], "phone_pub": cfg["phone_pub"],
             "desk_pub": cfg["desk_pub"]}
    smtp = mail_fallback(cfg)
    if smtp:
        phone["smtp"] = smtp
    print(json.dumps(phone))


def place(args):
    cfg = load_config()
    places = cfg.setdefault("places", {})
    if "--list" in args or not positional(args):
        for n, p in places.items():
            print(f"{n}: {p[0]:.5f},{p[1]:.5f} r={p[2] if len(p) > 2 else HOME_RADIUS}m")
        return
    pos = positional(args)
    name = pos[0]
    r = int(opt(args, "--radius") or HOME_RADIUS)
    if "--here" in args:
        loc = (jload(LATEST, {}).get("loc") or {})
        if loc.get("lat") is None:
            sys.exit("no fix from the phone yet")
        places[name] = [loc["lat"], loc["lon"], r]
    elif len(pos) >= 3:
        places[name] = [float(pos[1]), float(pos[2]), r]
    elif name in places and "--remove" in args:
        del places[name]
    else:
        sys.exit("place NAME --here | place NAME LAT LON [--radius M] | place NAME --remove")
    save_config(cfg)
    print(f"{name}: {places.get(name)}")


MAIL_TO = "nuxdie+mochi@gmail.com"


def mail_fallback(cfg):
    """The phone's worst-case path: Gmail SMTP with the nuxdie app password mochi-mail already uses (they said to
    reuse it, 2026-10-13); the desk's mbsync pulls MAIL_TO back in. Returns the smtp block for the phone, or None."""
    try:
        sources = json.loads((Path.home() / ".config/mochi/sources.json").read_text())
        pw = (Path(sources["mail"]["password_dir"]) / "nuxdie.pass").read_text().strip()
        user = sources["mail"]["accounts"]["nuxdie"]
    except (OSError, KeyError, ValueError):
        return None
    return {"host": "smtp.gmail.com", "port": 465, "user": user, "pass": pw, "to": cfg.get("mail_to", MAIL_TO)}


def mail_ingest(cfg, priv, phone_pub, state, latest):
    """Blobs the phone mailed when the relay was out of reach: find them in the local mail index, open them the same
    way, then take them out of the inbox (they are ours, not theirs)."""
    done = state.setdefault("mail_done", [])
    try:
        p = subprocess.run(["notmuch", "search", "--output=files", "--format=json", f"to:{cfg.get('mail_to', MAIL_TO)} subject:\"mochi blob\""],
                           capture_output=True, text=True, timeout=30)
        files = json.loads(p.stdout or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return
    batch = []
    for f in files:
        if f in done:
            continue
        try:
            msg = email.message_from_bytes(Path(f).read_bytes(), policy=email.policy.default)
            blob = next((part.get_payload(decode=True) for part in msg.walk() if part.get_filename() == "mochi.bin"), None)
            if blob:
                m = json.loads(open_sealed(blob, priv, phone_pub).decode())
                if m.get("id") not in state["seen"]:
                    state["seen"] = (state["seen"] + [m.get("id")])[-2000:]
                    batch.extend(m.get("events") or [])
                    log(f"mail fallback: {len(m.get('events') or [])} events from {Path(f).name}")
            mid = msg.get("Message-ID", "").strip("<>")
            if mid:
                subprocess.run(["mochi-mail", "archive", f"id:{mid}", "--max", "1"], capture_output=True, timeout=60)
        except Exception as e:
            log(f"mail fallback: {Path(f).name}: {e!r}")
        done.append(f)
    del done[:-500]
    if batch:
        apply_events(cfg, batch, latest, state)


def opt(args, name):
    if name in args:
        i = args.index(name)
        return args[i + 1] if i + 1 < len(args) else ""
    return None


def positional(args):
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith("--"):
            skip = a in ("--options", "--id", "--days", "--hours", "--radius", "--app", "--seconds", "--wait", "--link")
            continue
        out.append(a)
    return out


# ---- tracks: every outing as a GPX in their wanderer on sff ("fill in all my movements", 2026-10-14) -----------

TRACKS = DATA / "tracks"                  # the GPX files as written, kept here too
TRACKS_STATE = DATA / "tracks.json"       # {"sent": [outing ids]}
TRACKS_DROP = "n@sff.local:wanderer/phone-drop/"  # wanderer's watched upload folder for their account (token-named)
TRACKS_KEY = Path.home() / ".ssh/mochi_ed25519"
TRACK_MAX_ACC = 150                       # metres; a rougher fix is noise on a map
TRACK_MIN_LEN = 300                       # metres; shorter is the bins, or GPS drifting at the door


def outings(cfg):
    """Each trip out of home and back, as a list of fixes from the last one at home to the first one home again.
    Only finished trips: one still under way waits for the next run."""
    fixes = []
    for line in EVENTS.open() if EVENTS.exists() else []:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "loc" and ev.get("lat") is not None and (ev.get("acc") or 0) <= TRACK_MAX_ACC:
            fixes.append(ev)
    fixes.sort(key=lambda e: e.get("t", 0))
    trips, cur, last_home = [], None, None
    for ev in fixes:
        home = zone_of(cfg, ev["lat"], ev["lon"], ev.get("acc")) == "home"
        if cur is None:
            if home:
                last_home = ev
            else:
                cur = ([last_home] if last_home else []) + [ev]
        elif home:
            cur.append(ev)
            trips.append(cur)
            cur, last_home = None, ev
        else:
            cur.append(ev)
    return [t for t in trips if sum(distance_m(a["lat"], a["lon"], b["lat"], b["lon"]) for a, b in zip(t, t[1:]))
            >= TRACK_MIN_LEN]


def gpx(trip, name, kind="walking"):
    def iso(t):
        return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pts = "\n".join(f'      <trkpt lat="{e["lat"]:.6f}" lon="{e["lon"]:.6f}">'
                    + (f'<ele>{e["alt"]:.0f}</ele>' if e.get("alt") is not None else "")
                    + f'<time>{iso(e["t"])}</time></trkpt>' for e in trip)
    name = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" creator="mochi-phone" '
            f'xmlns="http://www.topografix.com/GPX/1/1">\n  <metadata><name>{name}</name><time>{iso(trip[0]["t"])}'
            f'</time></metadata>\n  <trk><name>{name}</name><type>{kind}</type>\n    <trkseg>\n{pts}\n    </trkseg>\n'
            f'  </trk>\n</gpx>\n')


# How they travelled, from the speed between fixes (the phone seldom sends its own): mode -> (phrase for the name,
# wanderer category, GPX type). Wanderer has no transit category, so a tram or train ride goes under Other.
TRAVEL = {"walk": ("on foot", "Walking", "walking"), "bike": ("by bike", "Biking", "cycling"),
          "transit": ("by transit", "Other", "transit")}


def travel_mode(trip):
    """walk / bike / transit for an outing. A leg faster than 30 km/h for 200 m in all is a tram, train or car; most of
    the moving distance at 7–30 km/h is a bike; the rest is on foot. A slow tram still reads as a bike."""
    legs = []
    for a, b in zip(trip, trip[1:]):
        d, s = distance_m(a["lat"], a["lon"], b["lat"], b["lon"]), b["t"] - a["t"]
        if s <= 0 or d < max(a.get("acc") or 0, b.get("acc") or 0):
            continue  # a jump inside the fix's own accuracy is jitter, not movement
        legs.append((d, d / s * 3.6))
    moving = sum(d for d, _ in legs)
    if sum(d for d, kmh in legs if kmh > 30) >= 200:
        return "transit"
    if moving and sum(d for d, kmh in legs if 7 <= kmh <= 30) > moving / 2:
        return "bike"
    return "walk"


# ---- the plan behind an outing: a calendar event they went to names the trail ("match those with calendar invites",
# 2026-10-20). Only their plans count, never the log calendars (how the time went) or the background ones.
TRACK_LOG_CALS = {"sleep", "building", "spending", "sidetracking"}
TRACK_SKIP_CALS = ("Holidays", "F1 Calendar")
TRACK_LEAVE_BEFORE = 2 * 3600   # an outing that starts this long before an event is them going to it


def plans_between(d0, d1):
    """Their timed plans from day d0 to day d1 inclusive, as (start ts, end ts, title); [] when the calendar is out of
    reach (the outing is then just 'Out'). Video calls and events they declined don't count: nobody went anywhere."""
    try:
        p = subprocess.run(["mochi-calendar", "agenda", "--json", "--from", d0.isoformat(), "--days",
                            str((d1 - d0).days + 1)], capture_output=True, text=True, timeout=90)
        evs = json.loads(p.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    out = []
    for ev in evs:
        cal = ev.get("calendar", "")
        s, e = ev.get("start", {}).get("dateTime"), ev.get("end", {}).get("dateTime")
        loc = (ev.get("location") or "").lower()
        if (cal in TRACK_LOG_CALS or cal.startswith(TRACK_SKIP_CALS) or ev.get("status") == "cancelled"
                or not s or not e  # all day: not a time they went somewhere
                or ev.get("hangoutLink") or ev.get("conferenceData")  # a video call (a Maps link as location is a place)
                or any(h in loc for h in ("meet.google", "zoom.us", "teams.microsoft", "whereby", "meet.jit.si"))
                or any(x.get("self") and x.get("responseStatus") == "declined" for x in ev.get("attendees") or [])):
            continue
        title = " ".join((ev.get("summary") or "").split())
        if title:
            out.append((dt.datetime.fromisoformat(s).timestamp(), dt.datetime.fromisoformat(e).timestamp(), title))
    return sorted(out)


def plan_for(trip, plans):
    """The event(s) this outing was for: they set out at most two hours before it (or a quarter of an hour into it,
    GPS at the door) and were still out when it began. A whole day out with a meeting in the middle, or something
    that was already running at home when they left (a visit), keeps its plain name."""
    t0, t1 = trip[0]["t"], trip[-1]["t"]
    hits = []
    for s, e, title in plans:
        if s - TRACK_LEAVE_BEFORE <= t0 <= s + 900 and t1 >= s and e - s <= 12 * 3600 and title not in hits:
            hits.append(title)
    return " + ".join(short(h, 48) for h in hits[:2]) or None


def short(s, n):
    """s cut to n characters at a word, with an ellipsis."""
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0] if " " in s[:n] else s[:n]
    return cut.rstrip(" -–,:") + "…"


def trail_name(trip, plan, mode, shift):
    start, end = dt.datetime.fromtimestamp(trip[0]["t"]), dt.datetime.fromtimestamp(trip[-1]["t"])
    head = "Uber Eats shift" if shift else f"{plan} ({TRAVEL[mode][0]})" if plan else f"Out {TRAVEL[mode][0]}"
    return f"{head} {start:%a %-d %b %H:%M}–{end:%H:%M}"


UBER_APP = "com.ubercab.driver"          # a notification from it during an outing: the outing was a courier shift
TRACKS_FIX = ["ssh", "-i", str(TRACKS_KEY), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=10",
              "n@sff.local", "wanderer/fix-trails.sh"]  # names and categories after import (the upload can't set them)


def uber_times():
    out = []
    for line in EVENTS.open() if EVENTS.exists() else []:
        if UBER_APP in line:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "notif" and ev.get("app") == UBER_APP:
                out.append(ev.get("t", 0))
    return out


def tracks(args):
    """mochi-phone tracks [--dry-run]: write each finished outing not sent yet as a GPX and copy it into wanderer's
    upload folder on sff, which imports it as a trail of theirs; then name and categorise what was imported: an Uber
    Eats shift is Biking, the rest Walking / Biking / Other (transit) by the speed between fixes, and an outing that
    went to something in their calendar is named after it ("dental cleaning (by bike) Wed 14 Oct 13:19–14:04").
    Run by mochi-tracks.timer."""
    dry = "--dry-run" in args
    st = jload(TRACKS_STATE, {"sent": []})
    st.setdefault("fix", [])
    uber = uber_times()
    todo = [t for t in outings(load_config()) if str(t[0]["t"]) not in st["sent"]]
    plans = []
    if todo:  # one calendar call for all of them
        days = [dt.date.fromtimestamp(t[0]["t"]) for t in todo] + [dt.date.fromtimestamp(t[-1]["t"]) for t in todo]
        plans = plans_between(min(days), max(days))
    new = []
    for trip in todo:
        oid = str(trip[0]["t"])
        t0, t1 = trip[0]["t"], trip[-1]["t"]
        shift = any(t0 - 3600 <= u <= t1 for u in uber)  # the check-in reminder comes an hour before the shift
        mode = "bike" if shift else travel_mode(trip)
        name = trail_name(trip, None if shift else plan_for(trip, plans), mode, shift)
        TRACKS.mkdir(parents=True, exist_ok=True)
        path = TRACKS / f"{dt.datetime.fromtimestamp(t0):%Y%m%d-%H%M}.gpx"
        path.write_text(gpx(trip, name, TRAVEL[mode][2]))
        new.append((oid, path, name, len(trip), TRAVEL[mode][1]))
    if dry:
        return "\n".join(f"would send {p.name}: {n} ({k} fixes, {c})" for _, p, n, k, c in new) or "no new outings"
    lines = []
    if new:
        p = subprocess.run(["scp", "-q", "-i", str(TRACKS_KEY), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                            "-o", "ConnectTimeout=10"] + [str(x[1]) for x in new] + [TRACKS_DROP],
                           capture_output=True, text=True, timeout=120)
        if p.returncode:
            return f"couldn't reach sff ({p.stderr.strip()[:200]}); {len(new)} outings wait for the next run"
        st["sent"] = (st["sent"] + [x[0] for x in new])[-5000:]
        st["fix"] += [[n, n, c, int(time.time())] for _, _, n, _, c in new]
        jsave(TRACKS_STATE, st)
        lines += [f"sent {pth.name}: {n} ({k} fixes, {c})" for _, pth, n, k, c in new]
        time.sleep(15)  # the watcher waits for the file to settle, then imports
    if st["fix"]:
        try:
            p = subprocess.run(TRACKS_FIX, input=json.dumps([f[:3] for f in st["fix"]]), capture_output=True, text=True,
                               timeout=120)
            r = json.loads(p.stdout.strip().splitlines()[-1])
            # not imported yet: try again next run, for a day
            st["fix"] = [f for f in st["fix"] if f[0] in r.get("missing", []) and time.time() - f[3] < 86400]
            jsave(TRACKS_STATE, st)
            lines.append(f"named {r.get('done', 0)}" + (f", {len(st['fix'])} not imported yet" if st["fix"] else ""))
        except (OSError, subprocess.TimeoutExpired, ValueError, IndexError) as e:
            lines.append(f"naming waits for the next run ({str(e)[:120]})")
    return "\n".join(lines) or "no new outings"


def main(argv):
    cmds = {"pair": pair, "serve": serve, "where": where, "status": status, "events": events, "send": send, "place": place,
            "now": now, "notifs": notifs, "calls": calls, "sms": sms, "usage": usage, "digest": digest, "feed": feed,
            "media": media, "transcribe": transcribe, "tracks": tracks,
            "log": lambda a: print(SENT.read_text() if SENT.exists() else "nothing sent yet")}
    if len(argv) < 2 or argv[1] not in cmds:
        print(__doc__.strip())
        return 2
    r = cmds[argv[1]](argv[2:])
    if isinstance(r, str):
        print(r)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
