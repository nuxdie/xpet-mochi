#!/usr/bin/env python3
"""mochi-sms — their text messages: Google Messages for Web in Mochi's Chrome, and KDE Connect's SMS plugin (D-Bus).

    mochi-sms status                                both sources: the phone, the Messages tab, contacts, the cache
    mochi-sms threads [--limit N] [--json]          conversations, newest first: last message of each (both sources, merged)
    mochi-sms read WHO [--limit N] [--json] [--web] one conversation, oldest first (WHO: w-handle, thread id, contact name or number)
    mochi-sms recent [--hours H] [--limit N]        what came in or went out lately (the digest; mochi-sense sms)
    mochi-sms unread                                inbound messages still unread on the phone (stable lines, watch-friendly)
    mochi-sms search WORDS... [--limit N]           the local cache (everything a run has seen, plus `sync`)
    mochi-sms sync [--per-thread N]                 pull the last N messages of every conversation into the cache
    mochi-sms contacts [QUERY]                      names and numbers, from the KDE Connect contact sync
    mochi-sms send WHO TEXT [--dry-run]             a text message, as them, logged (WHO as for `read`; one person only)
    mochi-sms log [N]                               what Mochi sent

Two sources, merged. **Google Messages for Web** (messages.google.com, a tab in Mochi's Chrome on its debugging
port, paired once with the phone's Google account) shows what the phone's own Messages app shows: every text as
soon as it lands, RCS included. Its conversation list carries the last message of each conversation with a relative
time ("5 min", "Yesterday", "29 Sept"); reading the list touches nothing on the phone. Opening one conversation
(`read`) gives exact times but marks it read in Messages, so `read` uses it only when KDE Connect has not caught
up with that conversation, or with `--web`. **KDE Connect** (`org.kde.kdeconnect.device.conversations` on the
phone's D-Bus object) keeps every conversation with exact times and ids and never marks anything read, but it has
missed recent texts (2026-10-10: nothing after 27 Sep while the phone had newer). Conversations seen in Messages
get stable handles (`w1`, `w2`, …, kept in the cache); KDE Connect threads keep their integer ids; a line shows
both when both sources have the conversation (`[w3|67]`). Names come from the vCards the KDE Connect contacts
plugin syncs to ~/.local/share/kpeoplevcard/ and from Messages itself. Sending goes through
`kdeconnect-cli --send-sms` and is appended to sms.log in Mochi's workspace. The phone is picked by `sms.device`
(id or name) in ~/.config/mochi/sources.json, else the first paired device of type "phone" with the SMS plugin;
Chrome's port is `browser.port` there (9333). Each source says so when it is not there; one is enough to answer.
"""

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import quopri
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

HOME = Path.home()
WORK = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "mochi"
LOG = WORK / "sms.log"
CACHE = WORK / "sms.sqlite"
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "mochi" / "sources.json"
VCARDS = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "kpeoplevcard"

BUS = "org.kde.kdeconnect"
DAEMON_PATH = "/modules/kdeconnect"
DEVICE_IF = "org.kde.kdeconnect.device"
CONV_IF = "org.kde.kdeconnect.device.conversations"
SMS_PLUGIN = "kdeconnect_sms"

# Android's Telephony.TextBasedSmsColumns.MESSAGE_TYPE_*
TYPES = {1: "in", 2: "out", 3: "draft", 4: "outbox", 5: "failed", 6: "queued"}
ARROW = {"in": "←", "out": "→", "draft": "✎", "outbox": "→?", "failed": "✗", "queued": "→?"}
MAX_SMS = 1600


WEB_HOST = "messages.google.com"
WEB_URL = f"https://{WEB_HOST}/web/conversations"


def config(section="sms"):
    try:
        return json.loads(CONFIG_FILE.read_text()).get(section) or {}
    except (OSError, ValueError, AttributeError):
        return {}


# ---- D-Bus ---------------------------------------------------------------------------------------------

def bus_and_loop():
    try:
        import dbus
        import dbus.mainloop.glib
        from gi.repository import GLib
    except ImportError as e:
        sys.exit(f"mochi-sms: python3-dbus and python3-gi are needed ({e})")
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    try:
        bus = dbus.SessionBus()
        bus.get_object(BUS, DAEMON_PATH)  # raises if kdeconnectd is not on the bus
    except Exception as e:  # noqa: BLE001
        sys.exit(f"mochi-sms: KDE Connect is not running on the session bus ({e})")
    return dbus, GLib, bus


class Phone:
    """The device object of the phone and its conversations interface."""

    def __init__(self):
        self.dbus, self.GLib, self.bus = bus_and_loop()
        self.id, self.name, self.reachable = self.pick()
        self.path = f"{DAEMON_PATH}/devices/{self.id}"
        obj = self.bus.get_object(BUS, self.path)
        self.conv = self.dbus.Interface(obj, CONV_IF)

    def prop(self, dev_id, name):
        obj = self.bus.get_object(BUS, f"{DAEMON_PATH}/devices/{dev_id}")
        return self.dbus.Interface(obj, "org.freedesktop.DBus.Properties").Get(DEVICE_IF, name)

    def has_sms(self, dev_id):
        obj = self.bus.get_object(BUS, f"{DAEMON_PATH}/devices/{dev_id}")
        return bool(self.dbus.Interface(obj, DEVICE_IF).hasPlugin(SMS_PLUGIN))

    def pick(self):
        daemon = self.dbus.Interface(self.bus.get_object(BUS, DAEMON_PATH), "org.kde.kdeconnect.daemon")
        ids = [str(i) for i in daemon.devices(False, True)]  # paired, reachable or not
        want = str(config().get("device") or "").strip()
        rows = []
        for i in ids:
            try:
                rows.append((i, str(self.prop(i, "name")), str(self.prop(i, "type")), bool(self.prop(i, "isReachable"))))
            except Exception:  # noqa: BLE001
                continue
        if want:
            for i, name, _, reach in rows:
                if want in (i, name):
                    return i, name, reach
            sys.exit(f"mochi-sms: no paired device {want!r} (sms.device in {CONFIG_FILE}); paired: "
                     + ", ".join(f"{n} [{i}]" for i, n, _, _ in rows))
        phones = [r for r in rows if r[2] == "phone" and self.has_sms(r[0])]
        phones.sort(key=lambda r: not r[3])  # reachable first
        if not phones:
            sys.exit("mochi-sms: no paired phone with the KDE Connect SMS plugin; pair one (kdeconnect-cli -l) or set "
                     f"sms.device in {CONFIG_FILE}")
        i, name, _, reach = phones[0]
        return i, name, reach

    def listen(self, handlers):
        """handlers: {signal name: callable}. Returns a function that removes them."""
        matches = [self.bus.add_signal_receiver(fn, sig, CONV_IF, BUS, self.path) for sig, fn in handlers.items()]
        return lambda: [m.remove() for m in matches]

    def wait(self, done, quiet=0.8, limit=8.0):
        """Spin the main loop until `done()` and no signal for `quiet` s (the last signal time is in done's closure),
        or `limit` s have passed."""
        loop = self.GLib.MainLoop()
        t0 = time.time()

        def tick():
            if time.time() - t0 > limit or done(quiet):
                loop.quit()
                return False
            return True

        self.GLib.timeout_add(100, tick)
        loop.run()

    def active(self):
        """The daemon's cache: the latest message of every conversation."""
        return [parse_message(v) for v in self.conv.activeConversations()]


def parse_message(v):
    """ConversationMessage (isa(s)xiixixa(xsss)) → dict. D-Bus variants may wrap it once more."""
    while hasattr(v, "__len__") and len(v) == 1 and not isinstance(v, (str, bytes)):
        v = v[0]
    event, body, addresses, date, mtype, read, thread, uid, sub, attachments = v
    return {
        "thread": int(thread), "uid": int(uid), "date": int(date), "type": TYPES.get(int(mtype), str(int(mtype))),
        "read": bool(int(read)), "body": str(body), "addresses": [str(a[0]) for a in addresses],
        "multi": bool(int(event) & 2), "attachments": len(attachments), "sub": int(sub),
    }


# ---- contacts (vCards synced by KDE Connect) ----------------------------------------------------------------

def norm_number(s):
    s = re.sub(r"[^\d+]", "", s or "")
    return s if s.startswith("+") else s.lstrip("+")


def same_number(a, b):
    a, b = norm_number(a), norm_number(b)
    if not a or not b:
        return False
    if a == b:
        return True
    da, db = a.lstrip("+"), b.lstrip("+")
    return len(da) >= 9 and len(db) >= 9 and da[-9:] == db[-9:]


def contacts(dev_id):
    """[{name, numbers}] from ~/.local/share/kpeoplevcard/kdeconnect-<id>/*.vcf."""
    out = []
    d = VCARDS / f"kdeconnect-{dev_id}"
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.vcf")):
        try:
            text = f.read_bytes()
        except OSError:
            continue
        name, numbers = "", []
        for raw in text.replace(b"\r\n", b"\n").split(b"\n"):
            if b":" not in raw:
                continue
            head, _, val = raw.partition(b":")
            key, *params = head.decode("ascii", "replace").upper().split(";")
            if "ENCODING=QUOTED-PRINTABLE" in params:
                val = quopri.decodestring(val)
            val = val.decode("utf-8", "replace").strip()
            if key == "FN":
                name = val
            elif key == "TEL" and val:
                numbers.append(val)
        if name and numbers:
            out.append({"name": name, "numbers": numbers})
    return out


def name_of(address, book):
    for c in book:
        if any(same_number(address, n) for n in c["numbers"]):
            return c["name"]
    return ""


def who(addresses, book):
    """A label for a message's addresses: the contact's name where known, the raw address otherwise."""
    return ", ".join(name_of(a, book) or a for a in addresses) or "?"


# ---- Google Messages for Web, in Mochi's Chrome ---------------------------------------------------------

def chrome_port():
    try:
        return int(config("browser").get("port") or 9333)
    except (ValueError, TypeError):
        return 9333


class CDP:
    """Just enough of a websocket client to send Chrome DevTools Protocol commands to one page (as mochi-vault)."""

    def __init__(self, ws_url):
        u = urlsplit(ws_url)
        self.s = socket.create_connection((u.hostname, u.port), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.s.recv(4096)
            if not chunk:
                raise OSError("Chrome closed the connection")
            head += chunk
        head, self.buf = head.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise OSError("Chrome refused the connection: " + head.split(b"\r\n")[0].decode(errors="replace"))
        self.n = 0

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass

    def _read(self, k):
        while len(self.buf) < k:
            chunk = self.s.recv(65536)
            if not chunk:
                raise OSError("Chrome closed the connection")
            self.buf += chunk
        out, self.buf = self.buf[:k], self.buf[k:]
        return out

    def _send(self, data, op=0x1):
        n, mask = len(data), os.urandom(4)
        head = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else
                                     bytes([0x80 | 126]) + struct.pack("!H", n) if n < 65536 else
                                     bytes([0x80 | 127]) + struct.pack("!Q", n))
        self.s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv(self):
        msg = b""
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            op = b0 & 0x0F
            if op == 0x8:
                raise OSError("Chrome closed the connection")
            if op == 0x9:
                self._send(data, 0xA)
                continue
            if op in (0x0, 0x1, 0x2):
                msg += data
                if b0 & 0x80:
                    return msg

    def call(self, method, **params):
        self.n += 1
        self._send(json.dumps({"id": self.n, "method": method, "params": params}).encode())
        while True:
            m = json.loads(self._recv())
            if m.get("id") == self.n:
                if "error" in m:
                    raise OSError(f"{method}: {m['error'].get('message')}")
                return m.get("result") or {}

    def eval(self, js):
        r = self.call("Runtime.evaluate", expression=js, returnByValue=True, awaitPromise=True)
        if r.get("exceptionDetails"):
            raise OSError("in the page: " + str((r["exceptionDetails"].get("exception") or {}).get("description", ""))[:200])
        return (r.get("result") or {}).get("value")


# The conversation list as Messages renders it (the ~25 it keeps in the DOM; newest first).
LIST_JS = r"""(() => {
  const items = [...document.querySelectorAll('mws-conversation-list-item')].map(it => {
    const a = it.querySelector('a[href]');
    const name = it.querySelector('[data-e2e-conversation-name]');
    const snip = it.querySelector('[data-e2e-conversation-snippet]');
    const ts = it.querySelector('mws-relative-timestamp');
    return {id: a ? (a.getAttribute('href') || '').split('/').pop() : '', name: name ? name.innerText.trim() : '',
            snippet: snip ? snip.innerText.trim() : '', when: ts ? ts.innerText.trim() : '',
            unread: a ? a.getAttribute('data-e2e-is-unread') === 'true' : false};
  }).filter(i => i.id);
  return {url: location.href, items, text: items.length ? '' : (document.body.innerText || '').slice(0, 300)};
})()"""

# One open conversation: every message wrapper in the DOM, oldest first; the aria-label carries the exact time.
CONV_JS = r"""(() => [...document.querySelectorAll('mws-message-wrapper')].map(w => {
  const part = w.querySelector('mws-text-message-part[aria-label], [aria-label*=" on "]');
  const content = w.querySelector('[data-e2e-message-content]');
  return {id: w.getAttribute('msg-id') || '', out: w.getAttribute('is-outgoing') === 'true',
          unread: w.getAttribute('is-unread') === 'true', label: part ? (part.getAttribute('aria-label') || '') : '',
          text: content ? content.innerText.trim() : '',
          attachments: w.querySelectorAll('mws-image-message-part, mws-media-message-part, mws-file-message-part').length};
}))()"""

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9
WEEKDAYS = {d.lower(): i for i, d in enumerate(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])}
WEEKDAYS.update({k[:3]: v for k, v in list(WEEKDAYS.items())})


def parse_abs(s, now=None):
    """'10 October 2026 at 14:34' / 'October 10, 2026, 2:34 PM' / '10 Oct, 14:34' (this year) → ms, or None."""
    now = now or dt.datetime.now()
    t = re.sub(r"[,.]", " ", s or "").replace(" at ", " ").strip()
    m = re.search(r"\b(\d{1,2}):(\d{2})(?:\s*([AaPp])\.?[Mm]\.?)?", t)
    if not m:
        return None
    hh, mm, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if ap == "p" and hh < 12:
        hh += 12
    if ap == "a" and hh == 12:
        hh = 0
    rest = (t[: m.start()] + " " + t[m.end():]).lower()
    day = month = year = None
    for w in re.findall(r"[a-z]+|\d+", rest):
        if w in MONTHS and month is None:
            month = MONTHS[w]
        elif w.isdigit() and len(w) == 4:
            year = int(w)
        elif w.isdigit() and 1 <= int(w) <= 31 and day is None:
            day = int(w)
    if month is None and day is None:
        d = now.date()
    else:
        d = dt.date(year or now.year, month or now.month, day or 1)
        if year is None and d > now.date():
            d = dt.date(now.year - 1, d.month, d.day)
    try:
        return int(dt.datetime.combine(d, dt.time(hh, mm)).timestamp() * 1000)
    except (ValueError, OverflowError):
        return None


def parse_rel(s, now=None):
    """Messages' relative stamp → (ms, exact?). 'now', '5 min', '3 hr', 'Yesterday', 'Tue', '29 Sept', '2 Aug 2025'."""
    now = now or dt.datetime.now()
    t = (s or "").strip().lower().rstrip(".")
    if not t:
        return int(now.timestamp() * 1000), False
    if t in ("now", "just now"):
        return int(now.timestamp() * 1000), True
    m = re.fullmatch(r"(\d+)\s*(min|mins|minute|minutes|m)", t)
    if m:
        return int((now - dt.timedelta(minutes=int(m.group(1)))).timestamp() * 1000), True
    m = re.fullmatch(r"(\d+)\s*(h|hr|hrs|hour|hours)", t)
    if m:
        return int((now - dt.timedelta(hours=int(m.group(1)))).timestamp() * 1000), False
    noon = lambda d: int(dt.datetime.combine(d, dt.time(12, 0)).timestamp() * 1000)  # noqa: E731
    if t == "yesterday":
        return noon(now.date() - dt.timedelta(days=1)), False
    if t in WEEKDAYS:
        back = (now.weekday() - WEEKDAYS[t]) % 7 or 7
        return noon(now.date() - dt.timedelta(days=back)), False
    words = re.findall(r"[a-z]+|\d+", t)
    day = month = year = None
    for w in words:
        if w in MONTHS and month is None:
            month = MONTHS[w]
        elif w.isdigit() and len(w) == 4:
            year = int(w)
        elif w.isdigit() and 1 <= int(w) <= 31 and day is None:
            day = int(w)
    if month and day:
        try:
            d = dt.date(year or now.year, month, day)
        except ValueError:
            return int(now.timestamp() * 1000), False
        if year is None and d > now.date():
            d = dt.date(now.year - 1, month, day)
        return noon(d), False
    m = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})", t)  # 29/09/2026 in some locales
    if m:
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
        try:
            return noon(dt.date(y, int(m.group(2)), int(m.group(1)))), False
        except ValueError:
            pass
    return int(now.timestamp() * 1000), False


def parse_label(label):
    """'Apple said: BODY. Received on 10 October 2026 at 14:34.' → (sender, body, ms or None)."""
    m = re.search(r"\b(Received|Sent|Delivered|Read)\s+on\s+(.+?)\.?\s*$", label or "")
    stamp = parse_abs(m.group(2)) if m else None
    head = (label or "")[: m.start()] if m else (label or "")
    sender, _, body = head.partition(" said: ")
    body = body.strip()
    return sender.strip(), (body[:-1] if body.endswith(".") else body), stamp  # Messages adds one "." after the text


def key_of(label):
    """One key for the same conversation from either source: the number's last digits, or the name lowered."""
    digits = re.sub(r"\D", "", label or "")
    if len(digits) >= 6 and len(re.sub(r"[\d\s+()\-.]", "", label or "")) == 0:
        return "n:" + digits[-9:]
    return "s:" + re.sub(r"\s+", " ", (label or "").strip().lower())


class Web:
    """The Messages for Web tab in Mochi's Chrome. `why` says what is missing when it cannot be used."""

    def __init__(self, open_tab=True, wait=8.0, first_load=40.0):
        """`wait`: how long an existing tab gets to show its list; `first_load`: a tab opened here (a run's fresh
        headless Chrome) — Messages connects and loads the list in 10-25 s the first time."""
        self.why, self.cdp, self.tab, self.account = None, None, None, ""
        fresh = False
        port = chrome_port()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as r:
                pages = [t for t in json.load(r) if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        except (OSError, ValueError):
            self.why = f"Mochi's Chrome is not up on port {port} (mochi-browser open)"
            return
        tab = next((t for t in pages if WEB_HOST in (t.get("url") or "")), None)
        if tab is None:
            if not open_tab:
                self.why = "no Messages tab open"
                return
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?{WEB_URL}", method="PUT")
                with urllib.request.urlopen(req, timeout=5) as r:
                    tab = json.load(r)
                fresh = True
            except (OSError, ValueError) as e:
                self.why = f"could not open a Messages tab in Mochi's Chrome ({e})"
                return
        self.tab = tab
        try:
            self.cdp = CDP(tab["webSocketDebuggerUrl"])
        except OSError as e:
            self.why = f"Chrome would not talk ({e}); is another run holding the tab?"
            return
        t0, limit = time.time(), (first_load if fresh else wait)
        while True:
            try:
                st = self.cdp.eval(LIST_JS) or {}
            except OSError as e:
                self.why = f"the Messages tab did not answer ({e})"
                return
            url, text = st.get("url") or "", st.get("text") or ""
            if st.get("items"):
                break
            if any(x in url for x in ("/welcome", "/authentication", "accounts.google.com")):
                self.why = ("the Messages tab is not paired: open it in Mochi's Chrome, sign in with the phone's Google "
                            "account and accept the prompt on the phone")
                return
            if "loading" in text.lower() and limit < first_load:  # still connecting: give it the long allowance
                limit = first_load
            if time.time() - t0 > limit:
                self.why = f"the Messages tab shows no conversations after {limit:.0f}s ({url}; {one_line(text, 80)!r})"
                return
            time.sleep(0.5)
        m = re.search(r"/web/u/(\d+)/", url)
        self.account = f"account {m.group(1)}" if m else ""

    @property
    def ok(self):
        return self.why is None

    def close(self):
        if self.cdp:
            self.cdp.close()

    def threads(self, db):
        """The list as rendered → message dicts (newest first), each the last message of a conversation. Handles and
        the first-seen time of every snippet come from the cache, so lines stay stable from run to run."""
        st = self.cdp.eval(LIST_JS) or {}
        now = dt.datetime.now()
        out, seen = [], int(time.time())
        for it in st.get("items") or []:
            handle = web_handle(db, it["id"], it["name"])
            body, out_ = it["snippet"], False
            if body.lower().startswith("you: "):
                body, out_ = body[5:], True
            sid = f"{it['id']}:s:{hashlib.sha1(body.encode()).hexdigest()[:10]}"
            row = db.execute("SELECT date, exact FROM web_messages WHERE id=?", (sid,)).fetchone()
            if row:
                date, exact = row
            else:
                date, exact = parse_rel(it["when"], now)
            db.execute("INSERT OR REPLACE INTO web_messages VALUES (?,?,?,?,?,?,?,?,?)",
                       (sid, it["id"], handle, it["name"], date, int(exact), "out" if out_ else "in", int(not it["unread"]), body))
            out.append({"thread": f"w{handle}", "uid": sid, "date": date, "exact": bool(exact), "type": "out" if out_ else "in",
                        "read": not it["unread"], "body": body, "addresses": [it["name"]], "multi": False, "attachments": 0,
                        "sub": 0, "source": "web", "conv": it["id"], "when": it["when"]})
        db.commit()
        return out

    def read(self, conv, name, limit):
        """Open one conversation (this marks it read in Messages) and read what is rendered, oldest first."""
        self.cdp.call("Page.navigate", url=f"https://{WEB_HOST}/web/u/0/conversations/{conv}")
        t0, rows = time.time(), []
        while time.time() - t0 < 12:
            time.sleep(0.6)
            try:
                rows = self.cdp.eval(CONV_JS) or []
            except OSError:
                rows = []
            if rows and all(r.get("label") or r.get("text") for r in rows[-3:]):
                break
        out = []
        for r in rows:
            sender, body, stamp = parse_label(r.get("label"))
            text = r.get("text") or body
            out.append({"thread": "", "uid": f"{conv}:m:{r.get('id')}", "date": stamp or int(time.time() * 1000), "exact": bool(stamp),
                        "type": "out" if r.get("out") else "in", "read": not r.get("unread"), "body": text,
                        "addresses": [name if not r.get("out") else name], "multi": False, "attachments": int(r.get("attachments") or 0),
                        "sub": 0, "source": "web", "conv": conv})
        return out[-limit:]


def web_handle(db, conv_id, name):
    row = db.execute("SELECT n FROM web_convs WHERE id=?", (conv_id,)).fetchone()
    if row:
        db.execute("UPDATE web_convs SET name=?, seen=? WHERE id=?", (name, int(time.time()), conv_id))
        return row[0]
    n = (db.execute("SELECT max(n) FROM web_convs").fetchone()[0] or 0) + 1
    db.execute("INSERT INTO web_convs VALUES (?,?,?,?)", (conv_id, n, name, int(time.time())))
    db.commit()
    return n


def web_conv_of(db, handle):
    row = db.execute("SELECT id, name FROM web_convs WHERE n=?", (handle,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def phone_or_none():
    """KDE Connect's phone, or (None, why): one missing source must not blind the other."""
    try:
        return Phone(), ""
    except SystemExit as e:
        return None, str(e).replace("mochi-sms: ", "")


def same_text(a, b):
    return " ".join((a or "").split())[:120] == " ".join((b or "").split())[:120]


def compare(w, k):
    """Messages' snippet row `w` against KDE Connect's latest message `k` of the same conversation: "same" (one text;
    `w` takes `k`'s exact time), "kde_newer" (KDE Connect has something Messages' line does not show) or "web_newer"
    (KDE Connect has not caught up). Messages knows the minute, or only the day: compare accordingly."""
    if same_text(w["body"], k["body"]):
        w["date"], w["exact"] = k["date"], True
        return "same"
    if w.get("exact"):
        return "kde_newer" if k["date"] > w["date"] + 120_000 else "web_newer"
    kd, wd = dt.date.fromtimestamp(k["date"] / 1000), dt.date.fromtimestamp(w["date"] / 1000)
    return "kde_newer" if kd > wd else "web_newer" if kd < wd else "same"


def merge(web_rows, kde_rows):
    """One list, newest first: a KDE Connect thread that Messages also shows is folded into the Messages line (which
    then carries both ids); the rest of KDE Connect's threads follow, so the long tail is not lost. A KDE Connect
    thread with something newer than its Messages line stays as its own line; one whose Messages line is newer is
    what `read` opens in Messages."""
    by_key = {}
    for m in web_rows:
        by_key.setdefault(key_of(m["addresses"][0]), m)
    out = list(web_rows)
    for k in kde_rows:
        w = None
        if not k.get("multi"):
            for a in ([k["label"]] if k.get("label") else []) + list(k["addresses"]):
                w = by_key.get(key_of(a))
                if w is not None:
                    break
        if w is None:
            out.append(k)
            continue
        w["kde"] = k["thread"]
        w["vs_kde"] = compare(w, k)
        if w["vs_kde"] == "kde_newer":
            out.append(k)
    return sorted(out, key=lambda m: -m["date"])


def tag(m):
    """[w3|67], [w3], or [67]."""
    if m.get("source") == "web":
        return f"[{m['thread']}|{m['kde']}]" if m.get("kde") is not None else f"[{m['thread']}]"
    return f"[{m['thread']}]"


# ---- the cache ---------------------------------------------------------------------------------------

def cache():
    WORK.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(CACHE)
    db.execute("""CREATE TABLE IF NOT EXISTS messages (
        uid INTEGER PRIMARY KEY, thread INTEGER, date INTEGER, type TEXT, read INTEGER, body TEXT,
        addresses TEXT, multi INTEGER, attachments INTEGER, seen INTEGER)""")
    db.execute("CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread, date)")
    db.execute("CREATE TABLE IF NOT EXISTS web_convs (id TEXT PRIMARY KEY, n INTEGER, name TEXT, seen INTEGER)")
    db.execute("""CREATE TABLE IF NOT EXISTS web_messages (
        id TEXT PRIMARY KEY, conv TEXT, handle INTEGER, name TEXT, date INTEGER, exact INTEGER, type TEXT, read INTEGER, body TEXT)""")
    db.execute("CREATE INDEX IF NOT EXISTS web_messages_conv ON web_messages(conv, date)")
    return db


def remember(db, messages):
    now = int(time.time())
    db.executemany(
        "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(m["uid"], m["thread"], m["date"], m["type"], int(m["read"]), m["body"], json.dumps(m["addresses"]),
          int(m["multi"]), m["attachments"], now) for m in messages])
    db.commit()


# ---- formatting ----------------------------------------------------------------------------------------

def when(ms):
    return dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def one_line(body, n=110):
    s = " ".join((body or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def stamp(m):
    """Exact times as they are; Messages' relative ones to the day, with ~HH:MM when it said "N hr"."""
    if m.get("exact", True):
        return when(m["date"])
    d = dt.datetime.fromtimestamp(m["date"] / 1000)
    return f"{d:%Y-%m-%d} ~{d:%H:%M}" if re.fullmatch(r"\d+\s*(h|hr|hrs|hour|hours)", (m.get("when") or "").lower()) else f"{d:%Y-%m-%d}      "


def line(m, book, chars=110):
    extra = (f" [{m['attachments']} attachment{'s' if m['attachments'] > 1 else ''}]" if m["attachments"] else "")
    unread = " (unread)" if m["type"] == "in" and not m["read"] else ""
    return f"{stamp(m)}  {ARROW.get(m['type'], '·')} {who(m['addresses'], book)}: {one_line(m['body'], chars) or '(no text)'}{extra}{unread}"


def stale_note(phone):
    return "" if phone.reachable else f"\n_{phone.name} is not reachable right now: this is what the daemon remembers._"


# ---- reading --------------------------------------------------------------------------------------------

def fetch_threads(phone):
    """Ask the phone for every conversation's latest message, then read the daemon's cache."""
    last = [time.time()]

    def touched(*_):
        last[0] = time.time()

    stop = phone.listen({"conversationCreated": touched, "conversationUpdated": touched})
    try:
        phone.conv.requestAllConversationThreads()
    except Exception as e:  # noqa: BLE001
        stop()
        sys.exit(f"mochi-sms: {e}")
    phone.wait(lambda quiet: time.time() - last[0] > quiet, quiet=1.0, limit=6.0 if phone.reachable else 1.5)
    stop()
    rows = sorted(phone.active(), key=lambda m: -m["date"])
    remember(cache(), rows)
    return rows


def resolve(phone, target, book):
    """WHO → (thread id or None, label, addresses). A number or a contact name finds the matching conversation."""
    threads = sorted(phone.active(), key=lambda m: -m["date"])
    if re.fullmatch(r"\d{1,6}", target.strip()):
        tid = int(target)
        for m in threads:
            if m["thread"] == tid:
                return tid, who(m["addresses"], book), m["addresses"]
        return tid, f"thread {tid}", []
    hits = [m for m in threads if not m["multi"] and any(same_number(target, a) for a in m["addresses"])]
    if not hits:  # an alphanumeric sender (Uber, DHL) or a contact's name, as `threads` shows it
        t = target.strip().lower()
        hits = [m for m in threads if not m["multi"] and (t in who(m["addresses"], book).lower())]
    if not hits:
        names = [c for c in book if target.strip().lower() in c["name"].lower()]
        if len({c["name"] for c in names}) > 1:
            sys.exit("mochi-sms: more than one contact matches: " + "; ".join(sorted({c["name"] for c in names})[:8]))
        numbers = [n for c in names for n in c["numbers"]]
        hits = [m for m in threads if not m["multi"] and any(same_number(n, a) for n in numbers for a in m["addresses"])]
        if not hits and names:
            return None, names[0]["name"], numbers[:1]
    if not hits:
        sys.exit(f"mochi-sms: no conversation with {target!r} (try `mochi-sms threads` or `mochi-sms contacts {target}`)")
    m = hits[0]
    return m["thread"], who(m["addresses"], book), m["addresses"]


def fetch_conversation(phone, tid, limit):
    """The last `limit` messages of a thread, oldest first. The daemon sends what it has at once, asks the phone for
    the rest, says `conversationLoaded`, then sends the new ones."""
    got, state = {}, {"loaded": False, "last": time.time()}

    def on_update(v):
        m = parse_message(v)
        if m["thread"] == tid:
            got[m["uid"]] = m
        state["last"] = time.time()

    def on_loaded(t, _n):
        if int(t) == tid:
            state["loaded"] = True
        state["last"] = time.time()

    stop = phone.listen({"conversationUpdated": on_update, "conversationCreated": on_update, "conversationLoaded": on_loaded})
    try:
        phone.conv.requestConversation(phone.dbus.Int64(tid), phone.dbus.Int32(0), phone.dbus.Int32(limit))
    except Exception as e:  # noqa: BLE001
        stop()
        sys.exit(f"mochi-sms: {e}")
    phone.wait(lambda quiet: state["loaded"] and time.time() - state["last"] > quiet,
               quiet=0.8, limit=8.0 if phone.reachable else 1.5)
    stop()
    rows = sorted(got.values(), key=lambda m: m["date"])[-limit:]
    remember(cache(), rows)
    return rows


# ---- commands -------------------------------------------------------------------------------------------

class Sources:
    """Both sources opened once per command; each may be missing, and the digest says which."""

    def __init__(self, need_web=True):
        self.db = cache()
        self.phone, self.phone_why = phone_or_none()
        self.book = contacts(self.phone.id) if self.phone else []
        self.web = Web() if need_web else None

    def close(self):
        if self.web:
            self.web.close()

    def notes(self):
        out = []
        if self.web is not None and not self.web.ok:
            out.append(f"_Messages for Web: {self.web.why}. KDE Connect only, which has missed recent texts before._")
        if self.phone is None:
            out.append(f"_KDE Connect: {self.phone_why}. Messages for Web only._")
        elif not self.phone.reachable:
            out.append(f"_{self.phone.name} is not on the LAN: KDE Connect shows what the daemon remembers._")
        return ("\n" + "\n".join(out)) if out else ""

    def head(self):
        parts = []
        if self.web is not None and self.web.ok:
            parts.append("Messages for Web")
        if self.phone is not None:
            parts.append("KDE Connect")
        name = self.phone.name if self.phone else "the phone"
        return f"## SMS on {name} ({' + '.join(parts) or 'no source'})"

    def threads(self):
        """Merged, newest first. KDE Connect's rows carry `label` so merge() can fold them into the Messages rows."""
        web_rows = self.web.threads(self.db) if self.web is not None and self.web.ok else []
        kde_rows = fetch_threads(self.phone) if self.phone else []
        for k in kde_rows:
            k["label"] = who(k["addresses"], self.book)
            k["source"] = "kde"
        return merge(web_rows, kde_rows)

    def resolve(self, target, force_web=False):
        """WHO → a plan: ("web", conv id, name) or ("kde", thread id, label) or (None, None, label)."""
        t = target.strip()
        m = re.fullmatch(r"[wW](\d+)", t)
        rows = self.threads()
        if m:
            conv, name = web_conv_of(self.db, int(m.group(1)))
            if conv is None:
                sys.exit(f"mochi-sms: no conversation {t} yet (handles come from `mochi-sms threads`)")
            return self.plan(conv, name, next((r for r in rows if r.get("conv") == conv), None), force_web)
        if re.fullmatch(r"\d{1,6}", t):
            if not self.phone:
                sys.exit(f"mochi-sms: {t} is a KDE Connect thread id and KDE Connect is not there ({self.phone_why})")
            tid, label, _ = resolve(self.phone, t, self.book)
            return "kde", tid, label
        hit = None
        for r in rows:
            if r.get("source") != "web":
                continue
            name = r["addresses"][0]
            if same_number(t, name) or t.lower() in name.lower():
                hit = r
                break
        if hit is None:
            names = [c for c in self.book if t.lower() in c["name"].lower()]
            numbers = [n for c in names for n in c["numbers"]]
            hit = next((r for r in rows if r.get("source") == "web" and any(same_number(n, r["addresses"][0]) for n in numbers)), None)
        if hit is not None:
            return self.plan(hit["conv"], hit["addresses"][0], hit, force_web)
        if not self.phone:
            sys.exit(f"mochi-sms: no conversation with {target!r} in Messages (try `mochi-sms threads`)")
        tid, label, numbers = resolve(self.phone, t, self.book)
        return ("kde", tid, label) if tid is not None else (None, None, label)

    def plan(self, conv, name, web_row, force_web):
        """Messages shows this conversation. Read it through KDE Connect when that has caught up (exact times, nothing
        marked read); through Messages when it has not, or on --web."""
        kde_tid = web_row.get("kde") if web_row else None
        if kde_tid is None and self.phone:
            for k in self.phone.active():
                if not k["multi"] and key_of(who(k["addresses"], self.book)) == key_of(name):
                    kde_tid = k["thread"]
                    break
        if force_web or kde_tid is None or not self.phone:
            return "web", conv, name
        if web_row is None or web_row.get("vs_kde") in ("same", "kde_newer"):
            return "kde", kde_tid, name
        return "web", conv, name


def cmd_status(a):
    src = Sources()
    n = src.db.execute("SELECT count(*), count(DISTINCT thread) FROM messages").fetchone()
    w = src.db.execute("SELECT count(*), count(DISTINCT conv) FROM web_messages").fetchone()
    if src.web.ok:
        items = len((src.web.cdp.eval(LIST_JS) or {}).get("items") or [])
        print(f"messages for web: paired, {items} conversations in the list ({src.web.account or 'account ?'}; tab {src.web.tab.get('url', '')[:60]})")
    else:
        print(f"messages for web: NOT usable — {src.web.why}")
    if src.phone:
        phone = src.phone
        print(f"kde connect: {phone.name} [{phone.id}] {'reachable' if phone.reachable else 'NOT reachable'}, SMS plugin "
              f"{'on' if phone.has_sms(phone.id) else 'OFF'}; {len(phone.active())} conversations the daemon knows")
        print(f"contacts: {len(src.book)} with a number ({VCARDS / ('kdeconnect-' + phone.id)})")
    else:
        print(f"kde connect: NOT usable — {src.phone_why}")
    print(f"cache: {n[0]} KDE Connect messages in {n[1]} threads; {w[0]} Messages snippets/messages in {w[1]} conversations ({CACHE})")
    print(f"send: {'kdeconnect-cli found' if shutil.which('kdeconnect-cli') else 'kdeconnect-cli MISSING'}; log: {LOG}")
    src.close()
    return 0


def cmd_threads(a):
    src = Sources()
    rows = src.threads()[: a.limit]
    if a.json:
        print(json.dumps([dict(m, who=who(m["addresses"], src.book), tag=tag(m)) for m in rows], ensure_ascii=False, indent=1))
        src.close()
        return 0
    print(f"{src.head()}: {len(rows)} conversations, newest first{src.notes()}")
    for m in rows:
        print(f"- {tag(m)} {line(m, src.book, a.chars)}")
    src.close()
    return 0


def cmd_read(a):
    src = Sources()
    how, ref, label = src.resolve(a.who, force_web=a.web)
    if how is None:
        print(f"no conversation with {label} yet")
        src.close()
        return 0
    if how == "web":
        if not src.web.ok:
            sys.exit(f"mochi-sms: that conversation is only in Messages for Web, which is not usable: {src.web.why}")
        rows = src.web.read(ref, label, a.limit)
        note = " — read in Messages for Web (now marked read there)"
        src.db.executemany("INSERT OR REPLACE INTO web_messages VALUES (?,?,?,?,?,?,?,?,?)",
                           [(m["uid"], ref, 0, label, m["date"], int(m["exact"]), m["type"], int(m["read"]), m["body"]) for m in rows])
        src.db.commit()
        tid = f"w?"
        hrow = src.db.execute("SELECT n FROM web_convs WHERE id=?", (ref,)).fetchone()
        tid = f"w{hrow[0]}" if hrow else tid
    else:
        rows = fetch_conversation(src.phone, ref, a.limit)
        note = "" if src.phone.reachable else " — the phone is off the LAN; this is what the daemon remembers"
        tid = str(ref)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        src.close()
        return 0
    print(f"## SMS with {label} ({tid}, last {len(rows)}){note}")
    for m in rows:
        print(line(m, src.book, a.chars))
    src.close()
    return 0


def cmd_recent(a):
    src = Sources()
    since = (time.time() - a.hours * 3600) * 1000
    week = (time.time() - 7 * 86400) * 1000
    allrows = src.threads()
    rows = [m for m in allrows if m["date"] >= since]
    unread = [m for m in allrows if m["type"] == "in" and not m["read"]]
    fresh = [m for m in unread if m["date"] >= week]
    older = len(unread) - len(fresh)
    if not rows and not fresh:
        print(f"{src.head()}\nnothing in the last {a.hours:g} h" + (f"; {older} older unread" if older else "") + src.notes())
        src.close()
        return 0
    print(f"{src.head()}: {len(rows)} conversation{'s' if len(rows) != 1 else ''} with news in the last {a.hours:g} h, "
          f"{len(fresh)} unread this week" + (f" ({older} older unread)" if older else "") + src.notes())
    shown = rows[: a.limit]
    for m in shown + [m for m in fresh if m not in shown]:
        print(f"- {tag(m)} {line(m, src.book, a.chars)}")
    print("_Dig: `mochi-sms read WHO [--limit N]` · `mochi-sms search WORDS` · `mochi-sms threads`_")
    src.close()
    return 0


def cmd_unread(a):
    src = Sources()
    rows = sorted((m for m in src.threads() if m["type"] == "in" and not m["read"]), key=lambda m: m["date"])
    for m in rows:
        print(f"{tag(m)} {line(m, src.book, 80)}")
    if not rows:
        print("no unread SMS")
    src.close()
    return 0


def cmd_search(a):
    src = Sources(need_web=False)
    db, book = src.db, src.book
    if src.phone:
        remember(db, src.phone.active())
    words = [w for w in a.words if w.strip()]
    if not words:
        sys.exit("mochi-sms: search needs words")
    where = " AND ".join("(body LIKE ? OR addresses LIKE ?)" for _ in words)
    params = [f"%{w}%" for w in words for _ in (0, 1)]
    # a contact's name is not in the cache; add their numbers to the query
    names = {c["name"]: c["numbers"] for c in book if any(w.lower() in c["name"].lower() for w in words)}
    rows = db.execute(f"SELECT uid, thread, date, type, read, body, addresses, multi, attachments FROM messages "
                      f"WHERE {where} ORDER BY date DESC LIMIT ?", params + [a.limit]).fetchall()
    if names and len(rows) < a.limit:
        more = db.execute("SELECT uid, thread, date, type, read, body, addresses, multi, attachments FROM messages "
                          "ORDER BY date DESC").fetchall()
        rows += [r for r in more if any(same_number(n, x) for n in sum(names.values(), []) for x in json.loads(r[6]))
                 and r not in rows][: a.limit - len(rows)]
    found = [{"uid": uid, "thread": tid, "date": date, "type": mtype, "read": bool(read), "body": body,
              "addresses": json.loads(addresses), "multi": bool(multi), "attachments": att, "source": "kde"}
             for uid, tid, date, mtype, read, body, addresses, multi, att in rows]
    wwhere = " AND ".join("(body LIKE ? OR name LIKE ?)" for _ in words)
    wrows = db.execute(f"SELECT id, conv, handle, name, date, exact, type, read, body FROM web_messages WHERE {wwhere} "
                       f"ORDER BY date DESC LIMIT ?", params + [a.limit]).fetchall()
    for wid, conv, handle, name, date, exact, mtype, read, body in wrows:
        h = handle or (db.execute("SELECT n FROM web_convs WHERE id=?", (conv,)).fetchone() or [0])[0]
        found.append({"uid": wid, "thread": f"w{h}", "date": date, "exact": bool(exact), "type": mtype, "read": bool(read),
                      "body": body, "addresses": [name], "multi": False, "attachments": 0, "source": "web"})
    # the same text seen by both sources is one line
    out, seen = [], set()
    for m in sorted(found, key=lambda m: -m["date"]):
        k = (key_of(m["addresses"][0]), " ".join(m["body"].split())[:80])
        if k in seen:
            continue
        seen.add(k)
        out.append(m)
    out = out[: a.limit]
    n_total = db.execute("SELECT (SELECT count(*) FROM messages) + (SELECT count(*) FROM web_messages)").fetchone()[0]
    print(f"## SMS search {' '.join(words)!r}: {len(out)} of {n_total} cached messages (newest first; `mochi-sms sync` to cache more)")
    for m in out:
        print(f"- {tag(m)} {line(m, book, a.chars)}")
    return 0


def cmd_sync(a):
    src = Sources()
    if src.web.ok:
        n = len(src.web.threads(src.db))
        print(f"messages for web: {n} conversations' last messages noted", file=sys.stderr)
    if not src.phone:
        print(f"kde connect: not there ({src.phone_why}); nothing more to pull", file=sys.stderr)
    elif not src.phone.reachable:
        print(f"kde connect: {src.phone.name} is not reachable; the full pull needs the phone on the LAN", file=sys.stderr)
    else:
        threads = fetch_threads(src.phone)
        total = 0
        for i, t in enumerate(threads, 1):
            rows = fetch_conversation(src.phone, t["thread"], a.per_thread)
            total += len(rows)
            print(f"\r{i}/{len(threads)} threads, {total} messages", end="", file=sys.stderr, flush=True)
        print(file=sys.stderr)
    n = src.db.execute("SELECT count(*), count(DISTINCT thread) FROM messages").fetchone()
    w = src.db.execute("SELECT count(*), count(DISTINCT conv) FROM web_messages").fetchone()
    print(f"cache: {n[0]} KDE Connect messages in {n[1]} threads; {w[0]} Messages entries in {w[1]} conversations ({CACHE})")
    src.close()
    return 0


def cmd_contacts(a):
    phone, why = phone_or_none()
    if not phone:
        sys.exit(f"mochi-sms: contacts come from KDE Connect, which is not there ({why})")
    book = contacts(phone.id)
    q = (a.query or "").strip().lower()
    rows = [c for c in book if not q or q in c["name"].lower() or any(same_number(q, n) for n in c["numbers"])]
    for c in sorted(rows, key=lambda c: c["name"].lower())[: a.limit]:
        print(f"{c['name']}: {', '.join(c['numbers'])}")
    if not rows:
        print("no contact matches" if q else "no contacts synced (turn on the Contacts plugin in KDE Connect on the phone)")
    return 0


def send_target(src, target):
    """WHO → (label, number) for `send`: a KDE Connect thread or contact, else a Messages conversation whose name
    is a number."""
    t = target.strip()
    m = re.fullmatch(r"[wW](\d+)", t)
    if m:
        conv, name = web_conv_of(src.db, int(m.group(1)))
        if conv is None:
            sys.exit(f"mochi-sms: no conversation {t} yet")
        return name, (name if re.fullmatch(r"\+?[\d\s()\-]{6,}", name or "") else "")
    if src.phone:
        _, label, addresses = resolve(src.phone, t, src.book)
        if len(addresses) != 1:
            sys.exit("mochi-sms: send goes to one person; this conversation is a group" if addresses
                     else f"mochi-sms: no number for {label!r}")
        return label, addresses[0]
    sys.exit(f"mochi-sms: sending needs KDE Connect, which is not there ({src.phone_why})")


def cmd_send(a):
    text = a.text.strip()
    if not text:
        sys.exit("mochi-sms: nothing to send")
    if len(text) > MAX_SMS:
        sys.exit(f"mochi-sms: {len(text)} characters is too long for a text ({MAX_SMS} max)")
    src = Sources(need_web=False)
    label, number = send_target(src, a.who)
    if not re.fullmatch(r"\+?\d{6,15}", norm_number(number)):
        sys.exit(f"mochi-sms: {number or label!r} is not a phone number (a short code or an alphanumeric sender); not sending")
    phone = src.phone
    if a.dry_run:
        print(f"--- dry run: would send to {label} ({number}) via {phone.name if phone else 'KDE Connect'}:\n{text}")
        return 0
    if not phone:
        sys.exit(f"mochi-sms: sending needs KDE Connect, which is not there ({src.phone_why})")
    if not phone.reachable:
        sys.exit(f"mochi-sms: {phone.name} is not reachable; the text would not go out")
    cli = shutil.which("kdeconnect-cli")
    if not cli:
        sys.exit("mochi-sms: kdeconnect-cli is missing")
    p = subprocess.run([cli, "-d", phone.id, "--send-sms", text, "--destination", number], capture_output=True, text=True, timeout=30)
    ok = p.returncode == 0
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M} {'sent' if ok else 'FAILED'} to={label} ({number}) via={phone.name} "
                f"text={one_line(text, 200)!r}\n")
    if not ok:
        sys.exit(f"mochi-sms: kdeconnect-cli failed: {(p.stderr or p.stdout).strip()[-300:]}")
    print(f"sent to {label} ({number}) via {phone.name}")
    return 0


def cmd_log(a):
    if LOG.exists():
        sys.stdout.write("".join(LOG.read_text().splitlines(True)[-a.n:]))
    return 0


def main():
    ap = argparse.ArgumentParser(prog="mochi-sms", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, **kw):
        s = sub.add_parser(name, **kw)
        s.set_defaults(fn=fn)
        return s

    add("status", cmd_status)
    s = add("threads", cmd_threads)
    s.add_argument("--limit", type=int, default=40)
    s.add_argument("--chars", type=int, default=110)
    s.add_argument("--json", action="store_true")
    s = add("read", cmd_read)
    s.add_argument("who")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--chars", type=int, default=400)
    s.add_argument("--json", action="store_true")
    s.add_argument("--web", action="store_true", help="read it in Messages for Web even if KDE Connect has it (marks it read there)")
    s = add("recent", cmd_recent)
    s.add_argument("--hours", type=float, default=24)
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--chars", type=int, default=110)
    add("unread", cmd_unread)
    s = add("search", cmd_search)
    s.add_argument("words", nargs="+")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--chars", type=int, default=160)
    s = add("sync", cmd_sync)
    s.add_argument("--per-thread", type=int, default=30)
    s = add("contacts", cmd_contacts)
    s.add_argument("query", nargs="?")
    s.add_argument("--limit", type=int, default=50)
    s = add("send", cmd_send)
    s.add_argument("who")
    s.add_argument("text")
    s.add_argument("--dry-run", action="store_true")
    s = add("log", cmd_log)
    s.add_argument("n", nargs="?", type=int, default=20)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
