#!/usr/bin/env python3
"""mochi-torrent — rutracker.org with their account, through Mochi's Chrome; downloads on their Deluge.

    mochi-torrent search WORDS [--sort seeds|date|size] [--limit N] [--json]
                                           the tracker's search, first page: topic id, size, seeds, date, forum, title
    mochi-torrent show TOPIC               one release: title, size, seeds, forum, the magnet, the description's start
    mochi-torrent status [--all]           their Deluge: what it's downloading and seeding, where downloads go, labels
    mochi-torrent add TOPIC|MAGNET|URL [--to DIR] [--label L] [--paused] [--why TEXT] [--dry-run]
                                           fetch the .torrent (with their account) and hand it to Deluge; logged
    mochi-torrent setup [--again]          once, you: Deluge's login into ~/.config/mochi/deluge.env, via mochi-vault
    mochi-torrent log [N]                  what was added, when, why

rutracker sits behind Cloudflare and shows its search only to members, so this doesn't fetch pages itself: it opens
a tab in Mochi's own Chrome (mochi-browser; started hidden if it isn't up) and reads the page there, signed in as
them (the profile keeps rutracker's year-long session; when it lapses, `mochi-vault fill` on
https://rutracker.org/forum/login.php signs it in again, with their tap). `add` downloads the .torrent inside that
tab (it carries their passkey) and sends it to Deluge's web JSON-RPC, base64, so nothing is written on this
machine but a copy under torrents/ in Mochi's workspace and a line in torrent.log; a magnet is sent as it is.

Deluge: `torrent.deluge.url` in ~/.config/mochi/sources.json (the web UI's address), its HTTP auth and web password
in `torrent.deluge.auth_file` (default ~/.config/mochi/deluge.env: USERNAME=, PASSWORD= as `mochi-vault export`
writes them, WEB_PASSWORD= for Deluge's own login when it differs). `setup` fills that file: it asks on your phone
to copy the Bitwarden login for the site, then tries Deluge's web login with that password and the default.
Nothing here removes a torrent or its files; that stays yours.
"""

import argparse
import base64
import datetime as dt
import http.cookiejar
import importlib.machinery
import importlib.util
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

PROG = "mochi-torrent"
HOME = Path.home()
WORK = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "mochi"
LOG = WORK / "torrent.log"
TORRENTS = WORK / "torrents"
CONF_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "mochi"
CONFIG = CONF_DIR / "sources.json"
DEFAULT_TRACKER = "https://rutracker.org"
DEFAULT_AUTH_FILE = CONF_DIR / "deluge.env"
PAGE_WAIT = 45          # seconds for a page to load (Cloudflare's "Just a moment" included)
SORT = {"date": 1, "size": 7, "seeds": 10}  # tracker.php's `o`
APPROVED = {"tor-approved": "ok", "tor-not-approved": "unchecked", "tor-dup": "duplicate", "tor-consumed": "absorbed",
            "tor-need-edit": "needs edit", "tor-closed": "closed", "tor-temp": "temporary"}


def die(msg, code=1):
    print(f"{PROG}: {msg}", file=sys.stderr)
    sys.exit(code)


def conf():
    try:
        return json.loads(CONFIG.read_text()).get("torrent", {}) or {}
    except (OSError, ValueError):
        return {}


def tracker():
    return (conf().get("tracker") or DEFAULT_TRACKER).rstrip("/")


def log(text):
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M} {text}\n")
    except OSError:
        pass


def human(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):  # 1024-based, as the tracker and Deluge show them
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" or n >= 100 else f"{n:.2f} {unit}" if n < 10 else f"{n:.1f} {unit}"
        n /= 1024
    return "?"


def when(ts):
    if not ts:
        return "?"
    try:
        return dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return "?"


def _sibling(name, installed):
    """A brain module installed beside this one (brain/NAME.py in the repo, ~/.local/bin/INSTALLED once installed)."""
    here = Path(__file__).resolve().parent
    path = next((here / n for n in (name + ".py", installed) if (here / n).exists()), None)
    if not path:
        die(f"{installed} is not installed beside {here}")
    spec = importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, str(path)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- Mochi's Chrome, over its debugging port -------------------------------------------------------------

class CDP:
    """Just enough of a websocket client to send Chrome DevTools Protocol commands to one page (as mochi-vault)."""

    def __init__(self, ws_url):
        u = urlsplit(ws_url)
        self.s = socket.create_connection((u.hostname, u.port), timeout=20)
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

    def eval(self, js, await_promise=False):
        r = self.call("Runtime.evaluate", expression=js, returnByValue=True, awaitPromise=await_promise)
        if (r.get("exceptionDetails") or {}).get("text"):
            raise OSError("the page's script failed: " + str(r["exceptionDetails"].get("text")))
        return (r.get("result") or {}).get("value")


class Browser:
    """Mochi's Chrome for one command: the one that's up, else started hidden and closed again at the end."""

    def __init__(self):
        self.mod = _sibling("mochi_browser", "mochi-browser")
        self.started = False
        self.port = self.mod.port()

    def __enter__(self):
        if not self.mod.running():
            if self.mod.in_use():
                die("Mochi's Chrome is held by another process without its debugging port (mochi-browser status)")
            if not self.mod.launch(headless=True):
                die("Mochi's Chrome didn't come up (mochi-browser status)")
            self.started = True
        return self

    def __exit__(self, *exc):
        if self.started:
            try:
                self.mod.stop()
            except Exception:
                pass

    def open(self, url):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/json/new?{url}", method="PUT")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return Tab(self, json.load(r))
        except (OSError, ValueError) as e:
            die(f"couldn't open a tab in Mochi's Chrome ({e})")


class Tab:
    def __init__(self, browser, target):
        self.browser, self.target = browser, target
        self.cdp = CDP(target["webSocketDebuggerUrl"])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.cdp.close()
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{self.browser.port}/json/close/{self.target['id']}", timeout=5).read()
        except (OSError, ValueError):
            pass

    def eval(self, js, await_promise=False):
        return self.cdp.eval(js, await_promise)

    def wait(self, limit=PAGE_WAIT):
        """Until the page has loaded (a new tab is about:blank for a moment first) and Cloudflare's interstitial, if
        any, has let it through."""
        t0, host, st = time.time(), urlsplit(self.target.get("url") or "").hostname or "", ""
        while time.time() - t0 < limit:
            try:
                st = self.eval("document.readyState + '|' + location.hostname + '|' + document.title") or ""
            except OSError:
                st = ""
            ready, here, title = (st.split("|", 2) + ["", ""])[:3]
            if ready == "complete" and here == host and "just a moment" not in title.lower() \
                    and not title.lower().startswith("attention"):
                return title
            time.sleep(0.3)
        die("the page didn't finish loading in time" + (" (Cloudflare's check didn't let the browser through)"
                                                           if "moment" in st.lower() else ""))


# ---- rutracker, in the page -------------------------------------------------------------------------------

STATE_JS = r"""(() => ({
  user: (document.querySelector('#logged-in-username') || {}).textContent || '',
  loginForm: !!document.querySelector('#login-form-full, input[name=login_username]'),
  url: location.href}))()"""

SEARCH_JS = r"""JSON.stringify((() => {
  const rows = [...document.querySelectorAll('tr.hl-tr')].map(tr => {
    const q = s => tr.querySelector(s);
    const t = s => (q(s) || {}).textContent || '';
    const ico = q('td.t-ico .tor-icon');
    return {id: tr.dataset.topic_id, title: t('a.tLink').trim(), forum: t('.f-name a').trim(),
      size: +((q('.tor-size') || {}).dataset || {}).ts_text || 0,
      seeds: parseInt(t('.seedmed')) || 0, leech: parseInt(t('.leechmed')) || 0,
      date: +((q('td:last-child') || {}).dataset || {}).ts_text || 0,
      status: ico ? [...ico.classList].find(c => c.startsWith('tor-') && c !== 'tor-icon') || '' : '',
      tags: [...tr.querySelectorAll('.t-tags .tg')].map(e => e.textContent.trim()),
      author: t('.u-name a').trim()};
  });
  const total = (document.querySelector('#main_content_wrap .bottom_info, #tor-tbl + table') || {}).textContent || '';
  const note = (document.querySelector('#tor-tbl .row1 td[colspan], .tCenter.med') || {}).textContent || '';
  return {rows, total: total.trim().replace(/\s+/g, ' ').slice(0, 200), note: note.trim().slice(0, 200)};
})())"""

TOPIC_JS = r"""JSON.stringify((() => {
  const q = s => document.querySelector(s);
  const t = s => ((q(s) || {}).textContent || '').trim();
  const magnet = (q('a.magnet-link') || {}).href || '';
  const body = q('.post_body');
  let text = body ? body.innerText : '';
  text = text.replace(/\n{3,}/g, '\n\n').trim();
  const crumbs = [...document.querySelectorAll('td.nav a[href*="viewforum"], .nav a[href*="viewforum"]')].map(a => a.textContent.trim());
  return {title: document.title.replace(/\s*::\s*RuTracker\.org\s*$/, ''), magnet,
    size: +((q('#tor-size-humn') || {}).title || 0), sizeText: t('#tor-size-humn'),
    seeds: parseInt(t('span.seed b')) || 0, leech: parseInt(t('span.leech b')) || 0,
    registered: t('#tor-reged'), status: t('#tor-status-resp, .tor-status-resp'),
    dl: !!q('a.dl-link'), forum: crumbs, text: text.slice(0, 2500), hash: t('#tor-hash')};
})())"""

# Downloads the .torrent in the page (their session and passkey ride along) and hands it back as base64.
DL_JS = r"""(async () => {
  const r = await fetch(%s, {credentials: 'include'});
  const type = r.headers.get('content-type') || '';
  if (!r.ok) return {error: 'HTTP ' + r.status};
  const buf = new Uint8Array(await r.arrayBuffer());
  if (buf.length === 0 || buf[0] !== 0x64) return {error: 'not a torrent file (' + type + ')', type};
  let s = '';
  for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  return {b64: btoa(s), type, size: buf.length};
})()"""


def not_signed_in():
    die("Mochi's Chrome is not signed in to rutracker (their session lapsed). Fix: open "
        f"{tracker()}/forum/login.php in it and sign in with `mochi-vault fill --why ...` in a run they asked for.")


def check_state(tab):
    st = tab.eval(STATE_JS) or {}
    if st.get("loginForm") and not st.get("user"):
        not_signed_in()
    return st


def topic_id(s):
    s = (s or "").strip()
    if s.isdigit():
        return s
    m = re.search(r"[?&]t=(\d+)", s)
    if m and ("viewtopic" in s or "dl.php" in s or "rutracker" in s):
        return m.group(1)
    return None


def search(words, sort=None, limit=50, as_json=False):
    url = f"{tracker()}/forum/tracker.php?nm={urllib.parse.quote(words)}"
    if sort:
        url += f"&o={SORT[sort]}&s=2"
    with Browser() as b, b.open(url) as tab:
        tab.wait()
        check_state(tab)
        res = json.loads(tab.eval(SEARCH_JS) or "{}")
    rows = (res.get("rows") or [])[:limit]
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    if not rows:
        print(f"nothing on rutracker for {words!r}" + (f" ({res['note']})" if res.get("note") else ""))
        return
    print(f"rutracker: {len(rows)} result{'s' if len(rows) != 1 else ''} for {words!r}"
          + (f", by {sort}" if sort else ", newest first") + " (id, size, seeds/leech, date, forum, title):")
    for r in rows:
        flag = APPROVED.get(r.get("status") or "", "")
        flag = f" [{flag}]" if flag and flag != "ok" else ""
        tags = f" {{{', '.join(r['tags'])}}}" if r.get("tags") else ""
        print(f"  {r['id']:>8}  {human(r['size']):>9}  S{r['seeds']}/L{r['leech']:<4} {when(r['date'])}  "
              f"[{r['forum']}]  {r['title']}{tags}{flag}")


def show(topic):
    tid = topic_id(topic)
    if not tid:
        die(f"not a topic id or rutracker link: {topic!r}")
    url = f"{tracker()}/forum/viewtopic.php?t={tid}"
    with Browser() as b, b.open(url) as tab:
        tab.wait()
        check_state(tab)
        res = json.loads(tab.eval(TOPIC_JS) or "{}")
    if not res.get("title"):
        die(f"topic {tid}: nothing readable there (gone, or not a release)")
    print(f"{res['title']}")
    print(f"  topic {tid}  {url}")
    if res.get("forum"):
        print(f"  forum: {' › '.join(res['forum'][-3:])}")
    print(f"  size: {res.get('sizeText') or human(res.get('size'))}   seeds {res.get('seeds')} / leech {res.get('leech')}"
          + (f"   registered {res['registered']}" if res.get("registered") else "")
          + (f"   status: {res['status']}" if res.get("status") else ""))
    if res.get("magnet"):
        print(f"  magnet: {res['magnet']}")
    elif not res.get("dl"):
        print("  no torrent on this topic (not a release, or taken down)")
    if res.get("text"):
        print("  ---")
        for line in res["text"].splitlines():
            print(f"  {line}")
        if len(res["text"]) >= 2500:
            print("  [...]")


# ---- the .torrent itself ----------------------------------------------------------------------------------

def bdecode(data):
    """The one bencode reader this needs: enough to name a torrent and sum its size."""
    def parse(i):
        c = data[i:i + 1]
        if c == b"i":
            j = data.index(b"e", i)
            return int(data[i + 1:j]), j + 1
        if c == b"l":
            out, i = [], i + 1
            while data[i:i + 1] != b"e":
                v, i = parse(i)
                out.append(v)
            return out, i + 1
        if c == b"d":
            out, i = {}, i + 1
            while data[i:i + 1] != b"e":
                k, i = parse(i)
                v, i = parse(i)
                out[k.decode("utf-8", "replace") if isinstance(k, bytes) else k] = v
            return out, i + 1
        j = data.index(b":", i)
        n = int(data[i:j])
        return data[j + 1:j + 1 + n], j + 1 + n
    v, _ = parse(0)
    return v


def describe(raw):
    """(name, total bytes, files) of a .torrent, or (None, 0, 0) when it isn't one."""
    try:
        info = bdecode(raw).get("info") or {}
        name = info.get("name.utf-8") or info.get("name") or b""
        name = name.decode("utf-8", "replace") if isinstance(name, bytes) else str(name)
        if "files" in info:
            return name, sum(int(f.get("length") or 0) for f in info["files"]), len(info["files"])
        return name, int(info.get("length") or 0), 1
    except (ValueError, AttributeError, TypeError, IndexError):
        return None, 0, 0


def fetch_torrent(tid):
    """The .torrent of a topic from Mochi's Chrome: (bytes, title, magnet). Falls back to the magnet alone."""
    url = f"{tracker()}/forum/viewtopic.php?t={tid}"
    with Browser() as b, b.open(url) as tab:
        tab.wait()
        check_state(tab)
        res = json.loads(tab.eval(TOPIC_JS) or "{}")
        if not res.get("dl") and not res.get("magnet"):
            die(f"topic {tid} ({res.get('title') or 'no title'}) has no torrent to download")
        got = {}
        if res.get("dl"):
            try:
                got = tab.eval(DL_JS % json.dumps(f"{tracker()}/forum/dl.php?t={tid}"), await_promise=True) or {}
            except OSError as e:
                got = {"error": str(e)}
    raw = base64.b64decode(got["b64"]) if got.get("b64") else b""
    if not raw and not res.get("magnet"):
        die(f"couldn't download the .torrent for topic {tid}: {got.get('error') or 'no data'}")
    return raw, res.get("title") or "", res.get("magnet") or "", got.get("error")


# ---- Deluge, over its web JSON-RPC --------------------------------------------------------------------------

class DelugeError(Exception):
    pass


def auth_file():
    return Path(os.path.expanduser((conf().get("deluge") or {}).get("auth_file") or DEFAULT_AUTH_FILE))


def read_env(path):
    out = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


class Deluge:
    STATUS_KEYS = ["name", "state", "progress", "total_size", "total_done", "download_payload_rate", "upload_payload_rate",
                   "eta", "save_path", "time_added", "ratio", "label", "num_seeds", "num_peers", "is_finished"]

    def __init__(self, web_password=None):
        d = conf().get("deluge") or {}
        self.url = (d.get("url") or "").strip()
        if not self.url:
            die('no Deluge configured: set "torrent": {"deluge": {"url": "https://host/deluge/"}} in '
                f"{CONFIG} and run `{PROG} setup`")
        if not self.url.endswith("/"):
            self.url += "/"
        self.env = read_env(auth_file())
        self.web_password = web_password if web_password is not None else (self.env.get("WEB_PASSWORD") or self.env.get("PASSWORD") or "deluge")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.n = 0
        self.connected = None

    def headers(self):
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.env.get("USERNAME") and self.env.get("PASSWORD"):
            h["Authorization"] = "Basic " + base64.b64encode(f"{self.env['USERNAME']}:{self.env['PASSWORD']}".encode()).decode()
        return h

    def rpc(self, method, *params):
        self.n += 1
        body = json.dumps({"method": method, "params": list(params), "id": self.n}).encode()
        req = urllib.request.Request(self.url + "json", data=body, headers=self.headers(), method="POST")
        try:
            with self.opener.open(req, timeout=60) as r:
                res = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise DelugeError(f"{urlsplit(self.url).hostname} wants an HTTP login the auth file doesn't have "
                                  f"({auth_file()}; run `{PROG} setup`)")
            raise DelugeError(f"HTTP {e.code} from Deluge at {self.url}")
        except (OSError, ValueError) as e:
            raise DelugeError(f"can't reach Deluge at {self.url}: {e}")
        if res.get("error"):
            raise DelugeError(str(res["error"].get("message") or res["error"]))
        return res.get("result")

    def login(self):
        if not self.rpc("auth.login", self.web_password):
            raise DelugeError(f"Deluge's web login refused the password (WEB_PASSWORD in {auth_file()}; `{PROG} setup`)")
        if not self.rpc("web.connected"):
            hosts = self.rpc("web.get_hosts") or []
            if not hosts:
                raise DelugeError("the Deluge web UI knows no daemon to connect to")
            self.rpc("web.connect", hosts[0][0])
            if not self.rpc("web.connected"):
                raise DelugeError("the Deluge web UI couldn't connect to its daemon")
        self.connected = True
        return self

    def torrents(self):
        return self.rpc("core.get_torrents_status", {}, self.STATUS_KEYS) or {}

    def config(self):
        return self.rpc("core.get_config_values", ["download_location", "move_completed", "move_completed_path",
                                                   "max_download_speed", "max_upload_speed"]) or {}

    def labels(self):
        try:
            if "Label" in (self.rpc("core.get_enabled_plugins") or []):
                return sorted(self.rpc("label.get_labels") or [])
        except DelugeError:
            pass
        return None

    def free(self, path=None):
        try:
            return self.rpc("core.get_free_space", path) if path else self.rpc("core.get_free_space")
        except DelugeError:
            return None

    def add_file(self, filename, raw, options):
        return self.rpc("core.add_torrent_file", filename, base64.b64encode(raw).decode(), options)

    def add_magnet(self, uri, options):
        return self.rpc("core.add_torrent_magnet", uri, options)

    def set_label(self, tid, label):
        self.rpc("label.set_torrent", tid, label)


def status(show_all=False):
    d = Deluge()
    try:
        d.login()
        ts, cfg = d.torrents(), d.config()
        labels = d.labels()
        free = d.free(cfg.get("download_location"))
    except DelugeError as e:
        die(str(e))
    host = urlsplit(d.url).hostname
    states = {}
    for t in ts.values():
        states[t.get("state")] = states.get(t.get("state"), 0) + 1
    summary = ", ".join(f"{n} {s.lower()}" for s, n in sorted(states.items(), key=lambda kv: -kv[1]))
    print(f"Deluge at {host}: {len(ts)} torrent{'s' if len(ts) != 1 else ''}" + (f" ({summary})" if summary else ""))
    print(f"  downloads go to {cfg.get('download_location')}" + (f" ({human(free)} free)" if free is not None else "")
          + (f", finished ones move to {cfg['move_completed_path']}" if cfg.get("move_completed") else ""))
    if labels is not None:
        print("  labels: " + (", ".join(labels) if labels else "(none yet; the Label plugin is on)"))
    week = time.time() - 7 * 86400
    rows = sorted(ts.items(), key=lambda kv: -(kv[1].get("time_added") or 0))
    if not show_all:
        rows = [(k, t) for k, t in rows if t.get("state") not in ("Seeding", "Paused") or not t.get("is_finished")
                or (t.get("time_added") or 0) > week]
    for k, t in rows:
        st = t.get("state") or "?"
        prog = t.get("progress") or 0
        rate = t.get("download_payload_rate") or 0
        eta = t.get("eta") or 0
        bits = [f"{st:<11}", f"{prog:5.1f}%", f"{human(t.get('total_done')):>8}/{human(t.get('total_size')):<8}"]
        if rate:
            bits.append(f"↓{human(rate)}/s")
        if st == "Downloading" and eta and eta < 10 * 86400:
            bits.append(f"eta {int(eta // 3600)}h{int(eta % 3600 // 60):02d}m" if eta >= 3600 else f"eta {int(eta // 60)}m")
        if st == "Seeding":
            bits.append(f"ratio {t.get('ratio') or 0:.2f}")
        line = f"  {' '.join(bits)}  {t.get('name')}"
        if t.get("label"):
            line += f"  [{t['label']}]"
        if t.get("save_path") and t["save_path"] != cfg.get("download_location"):
            line += f"  → {t['save_path']}"
        line += f"  (added {when(t.get('time_added'))}, {k[:8]})"
        print(line)
    if not rows:
        print("  nothing active" + ("" if show_all else "; --all lists what's seeding"))


def add(what, to=None, label=None, paused=False, why="", dry_run=False, convert=False):
    what = (what or "").strip()
    options = {}
    if to:
        if not to.startswith("/"):
            die("--to is a full path on the Deluge machine")
        options["download_location"] = to
    if paused:
        options["add_paused"] = True
    d = Deluge()
    if label is not None or not dry_run:
        try:
            d.login()
        except DelugeError as e:
            die(str(e))
    if label is not None:
        labels = d.labels()
        if labels is None:
            die("Deluge has no Label plugin on; add without --label")
        if label not in labels:
            die(f"no label {label!r} in Deluge (there are: {', '.join(labels) or 'none'})")
    tid = topic_id(what)
    raw, title, magnet, err = b"", "", "", None
    if tid:
        raw, title, magnet, err = fetch_torrent(tid)
    elif what.startswith("magnet:"):
        magnet = what
        title = urllib.parse.unquote(urllib.parse.parse_qs(urlsplit(what).query).get("dn", [""])[0])
    else:
        die(f"give a rutracker topic id or link, or a magnet: {what!r}")
    name, size, files = describe(raw) if raw else (None, 0, 0)
    if raw and name is None:
        die("the downloaded file isn't a torrent (their session may have lapsed)")
    what_text = f"{name or title}" + (f" ({human(size)}, {files} file{'s' if files != 1 else ''})" if size else "")
    src = f"rutracker topic {tid}" if tid else "a magnet"
    if dry_run:
        print(f"would add {what_text} from {src}" + (" via its .torrent" if raw else f" via the magnet ({err})" if err else " via the magnet")
              + (f" into {to}" if to else "") + (f" with label {label}" if label else "") + (", paused" if paused else ""))
        return
    try:
        if raw:
            fname = f"{tid or re.sub(r'[^A-Za-z0-9._-]+', '_', name or 'torrent')[:60]}.torrent"
            hid = d.add_file(fname, raw, options)
            try:
                TORRENTS.mkdir(parents=True, exist_ok=True)
                (TORRENTS / fname).write_bytes(raw)
            except OSError:
                pass
        else:
            hid = d.add_magnet(magnet, options)
    except DelugeError as e:
        msg = str(e)
        if "already" in msg.lower():
            log(f"not added: {what_text} from {src}: {msg}")
            print(f"Deluge already has {what_text}")
            return
        die(f"Deluge refused it: {msg}")
    if label:
        try:
            d.set_label(hid, label)
        except DelugeError as e:
            print(f"added, but the label failed: {e}")
    log(f"added {what_text} from {src} → {to or 'the default location'}" + (f" [{label}]" if label else "")
        + (" paused" if paused else "") + (f" ({hid[:8]})" if hid else "") + (f": {why}" if why else ""))
    print(f"added {what_text} to Deluge" + (f" into {to}" if to else "") + (f" with label {label}" if label else "")
          + (", paused" if paused else "") + (f" (id {hid[:8]})" if hid else ""))
    if convert and hid:
        here = Path(__file__).resolve().parent
        music = next((here / n for n in ("mochi-music", "mochi_music.py") if (here / n).exists()), None)
        if music:
            r = subprocess.run([sys.executable, str(music), "queue", hid], capture_output=True, text=True)
            print((r.stdout or r.stderr).strip())


def setup(again=False):
    d = conf().get("deluge") or {}
    url = (d.get("url") or "").strip()
    if not url:
        die('first put the web UI\'s address in sources.json: "torrent": {"deluge": {"url": "https://host/deluge/"}}')
    path = auth_file()
    env = read_env(path)
    if again or not (env.get("USERNAME") and env.get("PASSWORD")):
        here = Path(__file__).resolve().parent
        vault = next((here / n for n in ("mochi-vault", "mochi_vault.py") if (here / n).exists()), None)
        if not vault:
            die("mochi-vault is not installed beside this")
        print(f"asking on your phone to copy your login for {urlsplit(url).hostname} into {path} ...")
        r = subprocess.run([sys.executable, str(vault), "export", url, "--to", str(path), "--why",
                            "Deluge's HTTP login, for mochi-torrent (adding downloads)"], capture_output=True, text=True)
        print((r.stdout or r.stderr).strip())
        if r.returncode != 0:
            sys.exit(1)
        env = read_env(path)
    # the HTTP door
    dl = Deluge(web_password="")
    try:
        dl.rpc("web.connected")  # before auth.login Deluge answers "Not authenticated": the HTTP door itself is open then
    except DelugeError as e:
        if "HTTP login" in str(e):
            die(f"{e}; the login copied there doesn't open the HTTP door")
        if str(e).startswith("HTTP ") or "can't reach" in str(e):
            die(str(e))
    print(f"HTTP login at {urlsplit(url).hostname}: ok")
    # Deluge's own password: what the file says, else the same password, else Deluge's default
    tried = []
    for cand in [env.get("WEB_PASSWORD"), env.get("PASSWORD"), "deluge"]:
        if not cand or cand in tried:
            continue
        tried.append(cand)
        try:
            if Deluge(web_password=cand).rpc("auth.login", cand):
                break
        except DelugeError as e:
            die(str(e))
    else:
        die(f"Deluge's web login took none of the passwords tried; add WEB_PASSWORD=... to {path}")
    if env.get("WEB_PASSWORD") != cand:
        with open(path, "a") as f:
            f.write(f"WEB_PASSWORD={cand}\n")
        os.chmod(path, 0o600)
    try:
        dz = Deluge(web_password=cand).login()
        cfg, labels = dz.config(), dz.labels()
        n = len(dz.torrents())
    except DelugeError as e:
        die(str(e))
    print(f"Deluge web login: ok; connected to the daemon; {n} torrents; downloads go to {cfg.get('download_location')}"
          + (f"; labels: {', '.join(labels) or 'none'}" if labels is not None else ""))
    log(f"setup: Deluge at {urlsplit(url).hostname} reachable; auth in {path}")


def show_log(n):
    try:
        lines = LOG.read_text().splitlines()
    except OSError:
        lines = []
    print("\n".join(lines[-n:]) if lines else "nothing added yet")


def main():
    ap = argparse.ArgumentParser(prog=PROG, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search", help="search rutracker (first page)")
    s.add_argument("words", nargs="+")
    s.add_argument("--sort", choices=sorted(SORT), help="seeds, date or size, most first (default: the tracker's, newest)")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    t = sub.add_parser("show", help="one topic: size, seeds, magnet, description")
    t.add_argument("topic", help="topic id or viewtopic link")
    st = sub.add_parser("status", help="what Deluge is doing")
    st.add_argument("--all", action="store_true", help="seeding and finished ones too")
    a = sub.add_parser("add", help="send a topic's torrent (or a magnet) to Deluge")
    a.add_argument("what", help="rutracker topic id or link, or a magnet link")
    a.add_argument("--to", help="download directory on the Deluge machine (default: Deluge's)")
    a.add_argument("--label", help="a Deluge label (Label plugin)")
    a.add_argument("--paused", action="store_true")
    a.add_argument("--why", default="", help="for the log: who asked, what for")
    a.add_argument("--dry-run", action="store_true", help="fetch and describe, add nothing")
    a.add_argument("--convert", action="store_true", help="lossless: put it on mochi-music's list (MP3 320 in Music/ once it's on the NAS)")
    su = sub.add_parser("setup", help="you: Deluge's login into the auth file, via mochi-vault, and a first connection")
    su.add_argument("--again", action="store_true", help="copy the login again even if the file has one")
    lg = sub.add_parser("log")
    lg.add_argument("n", nargs="?", type=int, default=30)
    x = ap.parse_args()
    if x.cmd == "search":
        search(" ".join(x.words), x.sort, x.limit, x.json)
    elif x.cmd == "show":
        show(x.topic)
    elif x.cmd == "status":
        status(x.all)
    elif x.cmd == "add":
        add(x.what, x.to, x.label, x.paused, x.why, x.dry_run, x.convert)
    elif x.cmd == "setup":
        setup(x.again)
    elif x.cmd == "log":
        show_log(x.n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
