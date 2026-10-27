#!/usr/bin/env python3
"""mochi-vault — Mochi signs in with your Bitwarden logins without ever seeing a password.

    mochi-vault unlock                you: type your master password; the vault loads your logins into memory
    mochi-vault remember              you: the same, and keep the master password in your login keyring, so the vault
                                      unlocks itself whenever you log in and re-reads Bitwarden every few hours
    mochi-vault forget                take it out of the keyring again (back to unlocking by hand)
    mochi-vault lock                  forget the logins until the next unlock (or the next start, if remembered)
    mochi-vault status                locked or not, how many logins, what's allowed right now
    mochi-vault has URL               how many of your logins match a site (no names, no secrets)
    mochi-vault fill --why TEXT [--page URL-PART] [--account TEXT] [--from SITE]
                                      Mochi: sign in on the page open in its Chrome, once you allow it on your phone;
                                      --from: with the login saved for another site (a sign-in page on its own
                                      address), and on Allow that site is added to the login in Bitwarden
    mochi-vault signup --why TEXT --username NAME [--page URL-PART] [--submit]
                                      Mochi: a new account on the sign-up page open in its Chrome, once you allow it
                                      on your phone: the vault makes a strong password, saves it in your Bitwarden as
                                      a new login, and types it into the form (both boxes); needs `remember`
    mochi-vault export URL --to FILE --why TEXT [--account TEXT]
                                      setup: copy one login (USERNAME=, PASSWORD= lines) into a file under
                                      ~/.config/mochi, for a tool that must hold it itself (Deluge's HTTP auth);
                                      asked on your phone the same way; never allowed in Mochi's runs
    mochi-vault serve                 the service itself (mochi-vault.service)

How it holds together. `serve` runs as its own user service and is the only thing that ever has your logins. On
`unlock` it runs `bw unlock` with the password you typed, reads the vault (`bw sync`, `bw list items`) and runs
`bw lock` at once, so no usable Bitwarden session outlives the unlock; the logins stay in this process's memory
only, never on disk. Mochi's runs are not its children and cannot read that memory.

`fill` names a page in Mochi's Chrome (the debugging port mochi-browser opens). The vault reads the page's address
from Chrome itself, not from Mochi, matches it against your logins' URIs the way Bitwarden does, and asks the relay
to put the question on your phone: one button per matching account, and Deny. Only a decision that comes from the
relay's own process (checked by the socket's peer pid against mochi-brain.service) counts. On Allow it fills the
username, password (or the one-time code, for a TOTP step) into the page and submits it, all inside one script in
the page, which first checks the page is still on that host. Mochi gets back what was filled, never the values. An
Allow holds for that account on that site for LEASE_MINUTES, so a two-step login doesn't ask twice.

`fill --from SITE` is for a sign-in page that lives somewhere else than the site the login is saved for (Booking's
applications are on external-workingatbooking.icims.com, its sign-in on login.icims.eu). The question names both
sites; on Allow the vault fills there and adds the page's address to that login in Bitwarden (with the remembered
master password, like `signup`), so the next time it matches by itself.

`signup` is the same question for an account that doesn't exist yet ("make a new login for SITE"). On Allow the vault
makes the password itself, saves it in Bitwarden first (it opens Bitwarden with the remembered master password for
that one write and locks it again; without `remember` there is nothing to open it with, so it says so and stops),
then fills it into every password box on the form and the username or email into an empty box that asks for it.
It doesn't submit unless told to: sign-up forms tend to have a captcha or a terms box. If saving fails, nothing is
filled, so a password never ends up on a site without being in Bitwarden.

Setup once: `bw login` (and `bw config server https://vault.bitwarden.eu` first if your account is on the EU
server), then `systemctl --user enable --now mochi-vault` and either `mochi-vault unlock` after each boot or
`mochi-vault remember` once. Remembering was their choice (2026-10-10), knowing the trade: the login keyring hands its
secrets to any program running as them, Mochi's approved runs included, so a run that went wrong could read the
master password itself and skip the question on the phone. The phone question then guards against mistakes, not
against a run set on getting around it.
"""

import argparse
import base64
import datetime as dt
import getpass
import glob
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import string
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HOME = Path.home()
WORK = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "mochi"
LOG_FILE = WORK / "actions.log"
CONF_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "mochi"  # the only place `export` writes
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
SOCK = RUNTIME / "mochi-vault.sock"
BRAIN_SOCK = RUNTIME / "mochi-brain.sock"
RELAY_UNIT = "mochi-brain.service"
ASK_TIMEOUT = 8 * 60       # how long a fill waits for your tap
LEASE_MINUTES = 10         # an Allow covers that account on that site this long
PASSWORD_LENGTH = 20       # what `signup` makes: letters, digits and symbols, at least one of each
SYMBOLS = "!@#$%^&*-_=+?"
REFRESH_EVERY = 6 * 3600   # with the password remembered, re-read Bitwarden this often (new and changed logins)
LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localhost")
# Second-level public suffixes common enough to matter for "same site" (Bitwarden uses the full public suffix list).
SECOND_LEVEL = {"co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "com.au", "net.au", "org.au", "co.nz", "co.jp",
                "ne.jp", "or.jp", "com.br", "com.tr", "co.il", "co.za", "com.cn", "com.ua", "co.in", "com.mx",
                "com.ar", "com.sg", "com.hk", "co.kr", "com.tw", "com.pl", "com.ru", "com.es", "co.id", "com.my"}


def log(text):
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M} vault: {text}\n")
    except OSError:
        pass
    print(f"vault: {text}", flush=True)


# ---- matching a page to logins -----------------------------------------------------------------------

def parse_uri(uri):
    uri = (uri or "").strip()
    if not uri:
        return None
    return urlsplit(uri if "://" in uri else "http://" + uri)


def is_local(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return host == "localhost" or host.endswith(LOCAL_SUFFIXES)


def base_domain(host):
    """The registrable domain (github.com for gist.github.com), approximately; an IP or a one-label host as is."""
    host = (host or "").lower().rstrip(".")
    if is_local(host):  # the router, the NAS: the exact host is the site
        return host
    parts = host.split(".")
    n = 3 if len(parts) >= 3 and ".".join(parts[-2:]) in SECOND_LEVEL else 2
    return ".".join(parts[-n:])


def uri_matches(uri, match, page):
    """Bitwarden's URI match detection: None/0 base domain, 1 host, 2 starts with, 3 exact, 4 regex, 5 never."""
    u, p = parse_uri(uri), urlsplit(page)
    if not u or not p.hostname or match == 5:
        return False
    if match in (None, 0):
        return bool(u.hostname) and base_domain(u.hostname) == base_domain(p.hostname)
    if match == 1:
        return (u.hostname or "").lower() == p.hostname.lower() and (u.port or None) == (p.port or None)
    if match == 2:
        return page.startswith(uri)
    if match == 3:
        return page == uri
    if match == 4:
        try:
            return re.search(uri, page, re.I) is not None
        except re.error:
            return False
    return False


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMAIL_NAME = re.compile(r"e-?mail|почт", re.I)
USER_NAME = re.compile(r"user|login|логин|nick|account|customer|client", re.I)


def identities(login, fields):
    """The email and the plain username of a login, wherever the item keeps them: Bitwarden's Username holds one, and
    often a custom field ("Email", "Username", "Login") holds the other. Either may be empty."""
    user = (login.get("username") or "").strip()
    email = user if EMAIL_RE.match(user) else ""
    name = "" if email else user
    texts = [(f.get("name") or "", (f.get("value") or "").strip()) for f in fields or []
             if f.get("type") in (0, 1, None) and (f.get("value") or "").strip()]
    if not email:
        email = next((v for n, v in texts if EMAIL_NAME.search(n) and EMAIL_RE.match(v)), "") or \
            next((v for _n, v in texts if EMAIL_RE.match(v)), "")
    if not name:
        name = next((v for n, v in texts if USER_NAME.search(n) and not EMAIL_NAME.search(n) and not EMAIL_RE.match(v)), "")
    return email, name


def make_password(length=PASSWORD_LENGTH, symbols=True):
    sets = [string.ascii_lowercase, string.ascii_uppercase, string.digits] + ([SYMBOLS] if symbols else [])
    alphabet = "".join(sets)
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(max(length, 12)))
        if all(any(c in s for c in pw) for s in sets):
            return pw


def matching(items, page):
    return [it for it in items if any(uri_matches(u.get("uri"), u.get("match"), page) for u in it["uris"])]


def safe_page(url):
    """Only https, or plain http on this machine or the local network (a router, the NAS)."""
    p = urlsplit(url)
    if p.scheme == "https":
        return True
    return p.scheme == "http" and bool(p.hostname) and is_local(p.hostname)


# ---- one-time codes ------------------------------------------------------------------------------------

def totp(secret, at=None):
    """The current code for an authenticator key (base32, or an otpauth:// URI). None if it can't be made."""
    if not secret:
        return None
    digits, period, algo, key = 6, 30, "sha1", secret
    if secret.lower().startswith("otpauth://"):
        q = parse_qs(urlsplit(secret).query)
        key = (q.get("secret") or [""])[0]
        digits = int((q.get("digits") or ["6"])[0])
        period = int((q.get("period") or ["30"])[0])
        algo = (q.get("algorithm") or ["SHA1"])[0].lower()
    elif "://" in secret:  # steam:// and friends
        return None
    try:
        raw = base64.b32decode(re.sub(r"[\s-]", "", key).upper() + "=" * (-len(re.sub(r"[\s-]", "", key)) % 8))
        h = hmac.new(raw, struct.pack(">Q", int((at if at is not None else time.time()) // period)), getattr(hashlib, algo)).digest()
    except (ValueError, AttributeError, TypeError):
        return None
    o = h[-1] & 15
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


# ---- Chrome, over its debugging port ----------------------------------------------------------------

def chrome_port():
    try:
        cfg = json.loads((Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "mochi/sources.json").read_text())
        return int((cfg.get("browser") or {}).get("port") or 9333)
    except (OSError, ValueError, TypeError):
        return 9333


def targets():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{chrome_port()}/json/list", timeout=3) as r:
            return [t for t in json.load(r) if t.get("webSocketDebuggerUrl")]
    except (OSError, ValueError):
        return None


def pages():
    ts = targets()
    return None if ts is None else [t for t in ts if t.get("type") == "page"]


def frames(target):
    """Everywhere on a tab a login form could be: (url, websocket, frame id) for the page itself (frame id None),
    each frame in the page's own process (by frame id), and each frame Chrome runs as a target of its own (a frame
    from another site: its own websocket). Logins often sit in one (Apple's sign-in is a frame on idmsa)."""
    out = [(target.get("url", ""), target["webSocketDebuggerUrl"], None)]
    own = {t["id"]: t for t in targets() or [] if t.get("type") == "iframe"}  # a frame target's id is its frame id
    c = CDP(target["webSocketDebuggerUrl"])
    try:
        tree = c.call("Page.getFrameTree").get("frameTree") or {}
    finally:
        c.close()

    def walk(node):
        for ch in node.get("childFrames") or []:
            f = ch.get("frame") or {}
            t = own.pop(f.get("id"), None)
            out.append((t["url"], t["webSocketDebuggerUrl"], None) if t else (f.get("url", ""), target["webSocketDebuggerUrl"], f.get("id")))
            walk(ch)
    walk(tree)
    out += [(t["url"], t["webSocketDebuggerUrl"], None) for t in own.values() if t.get("parentId") == target.get("id")]
    return out


class CDP:
    """Just enough of a websocket client to send Chrome DevTools Protocol commands to one page."""

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


# Runs in the page in one go: checks it is still on the host you allowed, finds the fields, fills, submits.
FILL_JS = r"""(function (a) {
  if (location.hostname !== a.host) return {ok: false, error: "the page is on " + location.hostname + " now"};
  const shown = e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== "hidden";
  const usable = e => shown(e) && !e.disabled && !e.readOnly && e.type !== "hidden";
  const deep = (root, sel, out = []) => { root.querySelectorAll(sel).forEach(e => out.push(e));
    root.querySelectorAll("*").forEach(el => el.shadowRoot && deep(el.shadowRoot, sel, out)); return out; };
  const inputs = deep(document, "input").filter(usable);
  const label = e => [e.name, e.id, e.placeholder, e.getAttribute("aria-label"), e.autocomplete].join(" ");
  const texty = e => ["text", "email", "tel", "number", ""].includes(e.type);
  const pw = inputs.find(e => e.type === "password") || null;
  const otp = inputs.find(e => texty(e) && (e.autocomplete === "one-time-code" ||
    /otp|totp|2fa|mfa|one.?time|verification|security.?code|auth.?code|\bcode\b|\btoken\b/i.test(label(e)))) || null;
  let user = inputs.find(e => texty(e) && e !== otp && /username|email/.test(e.autocomplete || "")) || null;
  if (!user && pw) user = inputs.slice(0, inputs.indexOf(pw)).filter(e => texty(e) && e !== otp).pop() || null;
  if (!user && !pw) user = inputs.find(e => texty(e) && e !== otp &&
    (e.type === "email" || /user|login|email|e-?mail|account|identifier/i.test(label(e)))) || null;
  if (a.probe) return {ok: true, has: a.signup ? !!pw : !!(pw || otp || user)};
  // The login keeps an email and/or a plain username (one in Username, the other in a custom field): a field that
  // plainly asks for one gets that one, any other field gets Bitwarden's Username, else whichever there is.
  const asksEmail = e => e.type === "email" || e.autocomplete === "email" ||
    (/e-?mail|почт/i.test(label(e)) && !/user|login|логин/i.test(label(e)));
  const asksName = e => e.type !== "email" && !/mail|почт/i.test(label(e)) &&
    /user.?name|nick|логин|login.?(name|id)|customer|client/i.test(label(e));
  const idFor = e => (asksEmail(e) && a.email) || (asksName(e) && a.login) || a.username || a.email || a.login;
  const kind = (e, v) => v === a.email && v !== a.login ? "email" : "username";
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  const set = (e, v) => { e.focus(); setter.call(e, v);
    e.dispatchEvent(new Event("input", {bubbles: true})); e.dispatchEvent(new Event("change", {bubbles: true})); };
  const filled = []; let last = null;
  const fillId = e => { const v = idFor(e); if (v && e.value !== v) { set(e, v); filled.push(kind(e, v)); } return !!v; };
  if (a.signup) {
    if (!pw || !a.password) return {ok: false, error: "no password box on this page"};
    // a new account: the email or username only into an empty box before the password that plainly asks for it (a
    // sign-up form has names and phones there too), then the new password into every password box (and its repeat)
    inputs.slice(0, inputs.indexOf(pw)).filter(e => texty(e) && e !== otp && !e.value &&
      (asksEmail(e) || asksName(e) || /username|email/.test(e.autocomplete || ""))).forEach(fillId);
    const pws = inputs.filter(e => e.type === "password");
    pws.forEach(e => set(e, a.password));
    filled.push(pws.length > 1 ? `password (in ${pws.length} boxes)` : "password"); last = pws[pws.length - 1];
  } else if (pw && a.password) {
    if (user) {
      fillId(user);
      // a form that asks for both, the email and the username, each in its own empty field before the password
      const other = inputs.slice(0, inputs.indexOf(pw)).find(e => e !== user && e !== otp && texty(e) && !e.value &&
        (asksEmail(user) ? asksName(e) : asksEmail(e)));
      if (other && idFor(other) !== user.value) fillId(other);
    }
    set(pw, a.password); filled.push("password"); last = pw;
    // a form that wants it twice (an application re-opened after a reload): the repeat box too, but not on a
    // change-password form, where the second box is the new password
    const said = e => [e.name, e.id, e.placeholder, e.getAttribute("aria-label"),
      ...[...(e.labels || [])].map(l => l.textContent)].join(" ");
    const rest = inputs.filter(e => e.type === "password" && e !== pw);
    const again = /confirm|repeat|re-?enter|re-?type|again|verif|повтор|подтвер|wiederhol|bestätig/i;
    const twin = rest.length === 1 && pw.autocomplete !== "current-password" &&
      !/current|old|текущ|стар|aktuell|alt/i.test(said(pw)) &&
      (again.test(said(rest[0])) || !/new|neu|нов/i.test(said(rest[0]))) ? rest[0] : null;
    if (twin) { set(twin, a.password); filled[filled.length - 1] = "password (and its repeat box)"; last = twin; }
  } else if (otp && a.totp) {
    set(otp, a.totp); filled.push("one-time code"); last = otp;
  } else if (otp) {
    return {ok: false, error: "the page wants a one-time code and this login has no authenticator key"};
  } else if (user && fillId(user)) {
    if (!filled.length) filled.push("username (already there)");
    last = user;
  } else {
    return {ok: false, error: "no login fields on this page"};
  }
  if (!a.submit) return {ok: true, filled, how: "not submitted"};
  const go = /sign.?in|log.?in|next|continue|submit|verify|weiter|anmelden|войти|далее|продолжить/i;
  const text = b => [b.textContent, b.value, b.getAttribute("aria-label")].join(" ");
  const btn = (last.form && [...last.form.querySelectorAll("button[type=submit],input[type=submit]")].find(shown)) ||
    (last.form ? [...last.form.querySelectorAll("button,input[type=submit],[role=button]")] :
                 deep(document, "button,input[type=submit],[role=button]")).filter(shown).find(b => go.test(text(b)));
  if (btn) { btn.click(); return {ok: true, filled, how: "button"}; }
  if (last.form && last.form.requestSubmit) { last.form.requestSubmit(); return {ok: true, filled, how: "form"}; }
  return {ok: true, filled, how: "enter"};
})"""


def run_in(c, frame, args):
    """FILL_JS with these args in one frame: the page's own world for the top or a frame target, an isolated world
    (the frame's DOM, none of its scripts) for a frame inside the page's process."""
    extra = {}
    if frame[2]:
        extra["contextId"] = c.call("Page.createIsolatedWorld", frameId=frame[2], worldName="mochi-vault")["executionContextId"]
    r = c.call("Runtime.evaluate", expression=f"{FILL_JS}({json.dumps(args)})", returnByValue=True, userGesture=True,
               awaitPromise=False, **extra)
    return (r.get("result") or {}).get("value") or {"ok": False, "error": "the page's script failed"}


def probe(frame, signup=False):
    """Whether a frame shows login fields, or for a sign-up a password box (nothing is filled)."""
    host = urlsplit(frame[0]).hostname or ""
    try:
        c = CDP(frame[1])
    except OSError:
        return False
    try:
        return bool(run_in(c, frame, {"host": host, "probe": True, "signup": signup}).get("has"))
    except OSError:
        return False
    finally:
        c.close()


def fill_page(frame, host, item, submit=True, signup=False):
    args = {"host": host, "username": item.get("username") or "", "email": item.get("email") or "",
            "login": item.get("login") or "", "password": item.get("password") or "",
            "totp": totp(item.get("totp")) or "", "submit": submit, "signup": signup}
    c = CDP(frame[1])
    try:
        res = run_in(c, frame, args)
        if res.get("ok") and res.get("how") == "enter":
            for t in ("keyDown", "char", "keyUp"):
                c.call("Input.dispatchKeyEvent", type=t, key="Enter", code="Enter", windowsVirtualKeyCode=13,
                       **({"text": "\r"} if t != "keyUp" else {}))
        return res
    finally:
        c.close()
        args.clear()


# ---- the login keyring (gnome-keyring, over libsecret) -------------------------------------------------

def _secret():
    import gi
    gi.require_version("Secret", "1")
    from gi.repository import Secret
    schema = Secret.Schema.new("com.mochi.vault", Secret.SchemaFlags.NONE, {"app": Secret.SchemaAttributeType.STRING})
    return Secret, schema, {"app": "mochi-vault"}


def keyring_get():
    """The remembered master password, or None (not remembered, no keyring, keyring still locked)."""
    try:
        Secret, schema, attrs = _secret()
        return Secret.password_lookup_sync(schema, attrs, None)
    except Exception:
        return None


def keyring_set(password):
    Secret, schema, attrs = _secret()
    return Secret.password_store_sync(schema, attrs, Secret.COLLECTION_DEFAULT, "Bitwarden master password (mochi-vault)",
                                      password, None)


def keyring_clear():
    Secret, schema, attrs = _secret()
    return Secret.password_clear_sync(schema, attrs, None)


# ---- the service --------------------------------------------------------------------------------------

def bw_path():
    p = shutil.which("bw")
    if p:
        return p
    pats = [str(HOME / ".local/share/fnm/node-versions/*/installation/bin/bw"), str(HOME / ".nvm/versions/node/*/bin/bw"),
            str(HOME / ".npm-global/bin/bw"), "/usr/local/bin/bw", "/snap/bin/bw"]
    hits = sorted(h for pat in pats for h in glob.glob(pat))
    return hits[-1] if hits else ""


def relay_pid():
    try:
        out = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", RELAY_UNIT],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return int(out or 0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0


def peer_pid(conn):
    pid, _uid, _gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    return pid


def label(item):
    user = item.get("username") or item.get("email") or item.get("login") or ""
    return f"{item['name']} · {user}" if user and user.lower() not in item["name"].lower() else item["name"]


class Vault:
    def __init__(self, bw=None, relay_pid=relay_pid, notify=None):
        self.bw = bw or bw_path()
        self.relay_pid = relay_pid
        self.notify = notify or self.tell_relay
        self.items = None          # None = locked
        self.unlocked_at = 0
        self.leases = {}           # (host, item id) -> until
        self.pending = {}          # request id -> {"event", "choice", "items"}
        self.lock = threading.Lock()

    # -- unlocking

    def bw_env(self, session=None):
        env = {"PATH": os.pathsep.join([str(Path(self.bw).parent), os.environ.get("PATH", "/usr/bin:/bin")]),
               "HOME": str(HOME), "BW_NOINTERACTION": "true"}
        for k in ("BITWARDENCLI_APPDATA_DIR", "XDG_CONFIG_HOME", "NODE_EXTRA_CA_CERTS"):
            if os.environ.get(k):
                env[k] = os.environ[k]
        if session:
            env["BW_SESSION"] = session
        return env

    def run_bw(self, args, session=None, pass_fds=(), timeout=120, input=None):
        return subprocess.run([self.bw] + args, capture_output=True, text=True, timeout=timeout,
                              env=self.bw_env(session), pass_fds=pass_fds, input=input)

    def open_bw(self, password):
        """A Bitwarden session key for this password: (session, None) or (None, why not). The caller locks it."""
        if not self.bw:
            return None, "the Bitwarden CLI isn't installed (npm install -g @bitwarden/cli)"
        try:
            st = json.loads(self.run_bw(["status"]).stdout or "{}").get("status")
        except (ValueError, OSError, subprocess.TimeoutExpired):
            st = None
        if st == "unauthenticated":
            return None, "Bitwarden isn't logged in on this machine yet: run `bw login` first"
        r, w = os.pipe()
        try:
            os.write(w, password.encode() + b"\n")
            os.close(w)
            p = self.run_bw(["unlock", "--raw", "--passwordfile", f"/dev/fd/{r}"], pass_fds=(r,))
        finally:
            os.close(r)
            password = None
        session = p.stdout.strip()
        if p.returncode != 0 or not session:
            return None, (p.stderr.strip() or p.stdout.strip() or "unlock failed")[:200]
        return session, None

    def unlock(self, password):
        session, err = self.open_bw(password)
        password = None
        if not session:
            return False, err
        try:
            self.run_bw(["sync"], session)
            out = self.run_bw(["list", "items"], session, timeout=300)
            raw = json.loads(out.stdout or "[]")
        except (ValueError, OSError, subprocess.TimeoutExpired) as e:
            return False, f"couldn't read the vault: {e}"
        finally:
            self.run_bw(["lock"])  # the session key is dead from here on, wherever it might have been seen
            session = None
        items = []
        for it in raw:
            lg = it.get("login") or {}
            if it.get("type") != 1 or it.get("deletedDate") or not (lg.get("password") or lg.get("totp")):
                continue
            email, name = identities(lg, it.get("fields"))  # other custom fields (PINs, answers) are not kept
            items.append({"id": it["id"], "name": it.get("name") or "(unnamed)", "username": lg.get("username") or "",
                          "email": email, "login": name,
                          "password": lg.get("password") or "", "totp": lg.get("totp") or "",
                          "uris": [{"uri": u.get("uri"), "match": u.get("match")} for u in lg.get("uris") or []]})
        with self.lock:
            if self.items is None:
                self.leases = {}
            self.items, self.unlocked_at = items, time.time()
        log(f"unlocked: {len(items)} logins in memory")
        return True, f"unlocked: {len(items)} logins in memory (Bitwarden itself is locked again)"

    def forget(self):
        with self.lock:
            self.items, self.leases = None, {}
            for req in self.pending.values():
                req["choice"] = None
                req["event"].set()
        log("locked")
        return "locked"

    # -- what Mochi can ask

    def status(self):
        if self.items is None:
            return "locked (they unlock it with `mochi-vault unlock` in a terminal)"
        since = dt.datetime.fromtimestamp(self.unlocked_at).strftime("%a %H:%M")
        leases = [f"{h} (until {dt.datetime.fromtimestamp(t):%H:%M})" for (h, _), t in self.leases.items() if t > time.time()]
        return (f"unlocked since {since}: {len(self.items)} logins"
                + (f"; allowed right now on: {', '.join(sorted(set(leases)))}" if leases else ""))

    def has(self, url):
        if self.items is None:
            return "locked"
        n = len(matching(self.items, url if "://" in url else "https://" + url))
        return f"{n} login{'s' if n != 1 else ''} for {urlsplit(url if '://' in url else 'https://' + url).hostname}"

    def tell_relay(self, **msg):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            s.sendto(json.dumps(msg).encode(), str(BRAIN_SOCK))
            return True
        except OSError:
            return False
        finally:
            s.close()

    def fill(self, why, page="", account="", submit=True, timeout=ASK_TIMEOUT, from_site=""):
        if self.items is None:
            return False, "the vault is locked; ask them to run `mochi-vault unlock` in a terminal"
        why = (why or "").strip()
        if not why:
            return False, "say what it's for (--why): it goes on their phone with the question"
        ps = pages()
        if ps is None:
            return False, "Mochi's Chrome isn't reachable on its debugging port"
        src = ""
        if from_site:  # the login saved for another site, on this page
            src = from_site if "://" in from_site else "https://" + from_site
            if not matching(self.items, src):
                return False, f"no login of theirs is saved for {urlsplit(src).hostname}"
        found = lambda url: matching(self.items, src) if src else matching(self.items, url)
        cands = [t for t in ps if page and page in t.get("url", "")] if page else \
            [t for t in ps if found(t.get("url", ""))] if not src else [t for t in ps if safe_page(t.get("url", ""))]
        if len(cands) != 1:
            return False, (f"{len(cands)} tabs match; name the tab with --page (part of its address)" if cands else
                           "no open tab " + (f"matching {page!r}" if page else "is on a site they have a login for"))
        target = cands[0]
        try:
            places = [f for f in frames(target) if safe_page(f[0]) and found(f[0])]
        except OSError as e:
            return False, f"couldn't reach the page: {e}"
        if not places:
            url = target.get("url", "")
            if not safe_page(url):
                return False, f"not signing in on {url.split('?')[0]}: only https (or plain http on the local network)"
            return False, f"no login of theirs is saved for {urlsplit(url).hostname}"
        # the page itself first, then its frames: the first that shows login fields (else the page, to say so)
        frame = next((f for f in places if probe(f)), places[0])
        url = frame[0]
        host = urlsplit(url).hostname or ""
        items = found(url)
        if src and matching(self.items, url):
            src, items = "", matching(self.items, url)  # it's saved for this page already: an ordinary fill
        if account:
            items = [it for it in items if account.lower() in " ".join(
                [it["name"], it["username"], it.get("email", ""), it.get("login", "")]).lower()]
        if not items:
            return False, f"no login of theirs is saved for {host}" + (f" matching {account!r}" if account else "")
        leased = [it for it in items if self.leases.get((host, it["id"]), 0) > time.time()]
        if len(leased) == 1:
            item = leased[0]
        else:
            ok, item = self.ask(host, why, items, timeout, **({"kind": "elsewhere", "source": urlsplit(src).hostname}
                                                              if src else {}))
            if not ok:
                return False, item
        with self.lock:
            self.leases[(host, item["id"])] = time.time() + LEASE_MINUTES * 60
        try:
            res = fill_page(frame, host, item, submit)
        except OSError as e:
            return False, f"couldn't reach the page: {e}"
        if not res.get("ok"):
            log(f"fill on {host} ({label(item)}) failed: {res.get('error')}")
            return False, res.get("error") or "fill failed"
        how = {"button": "pressed the sign-in button", "form": "submitted the form", "enter": "pressed Enter",
               "not submitted": "not submitted"}.get(res.get("how"), res.get("how"))
        log(f"filled {', '.join(res['filled'])} for {label(item)} on {host}, {how} ({why[:80]})")
        added = ""
        if src:
            ok, msg = self.add_site(item, url)
            added = f" Added {host} to that login in Bitwarden." if ok else f" Couldn't add {host} to the login: {msg}."
        return True, (f"filled {', '.join(res['filled'])} for {item['name']} on {host} and {how}.{added} Take a snapshot to "
                      f"see where it landed; a next step on this site (a code, a second page) can fill again "
                      f"without asking for {LEASE_MINUTES} minutes.")

    def add_site(self, item, url):
        """The page's address added to a login in Bitwarden (open with the remembered master password, edit, lock),
        and to the copy in memory. (ok, message)."""
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        master = keyring_get()
        if master is None:
            return False, "their master password isn't remembered (`mochi-vault remember`)"
        session, err = self.open_bw(master)
        master = None
        if not session:
            return False, f"couldn't open Bitwarden: {err}"
        try:
            got = self.run_bw(["get", "item", item["id"]], session)
            full = json.loads(got.stdout or "{}") if got.returncode == 0 else {}
            if not full.get("login"):
                return False, "Bitwarden didn't give the login back: " + (got.stderr.strip() or got.stdout.strip())[:200]
            uris = full["login"].get("uris") or []
            if not any(uri_matches(u.get("uri"), u.get("match"), url) for u in uris):
                full["login"]["uris"] = uris + [{"uri": origin, "match": None}]
                out = self.run_bw(["edit", "item", item["id"]], session,
                                  input=base64.b64encode(json.dumps(full).encode()).decode())
                if out.returncode != 0:
                    return False, "Bitwarden didn't save it: " + (out.stderr.strip() or out.stdout.strip())[:200]
        except (ValueError, OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)
        finally:
            self.run_bw(["lock"])
            session = None
            full = None
        with self.lock:
            item["uris"].append({"uri": origin, "match": None})
        log(f"added {p.hostname} to the login {label(item)}")
        return True, "added"

    def save_login(self, url, username, password, why):
        """A new login in their Bitwarden, written at once (open with the remembered master password, create, lock).
        Returns (item for self.items, None) or (None, why not)."""
        master = keyring_get()
        if master is None:
            return None, ("their master password isn't remembered, so the vault can't write to Bitwarden; they can "
                          "run `mochi-vault remember` once, or make the login in Bitwarden themselves")
        session, err = self.open_bw(master)
        master = None
        if not session:
            return None, f"couldn't open Bitwarden: {err}"
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        new = {"type": 1, "name": p.hostname, "favorite": False, "reprompt": 0, "folderId": None, "organizationId": None,
               "notes": f"Made by mochi-vault {dt.datetime.now():%Y-%m-%d} for: {why[:200]}", "fields": [],
               "login": {"username": username, "password": password, "totp": None,
                         "uris": [{"uri": origin, "match": None}]}}
        try:  # on stdin, never on a command line other processes could read
            out = self.run_bw(["create", "item"], session, input=base64.b64encode(json.dumps(new).encode()).decode())
            made = json.loads(out.stdout or "{}") if out.returncode == 0 else {}
        except (ValueError, OSError, subprocess.TimeoutExpired) as e:
            made, out = {}, None
            err = str(e)
        finally:
            self.run_bw(["lock"])
            session = None
            new.clear()
        if not made.get("id"):
            return None, "Bitwarden didn't save it: " + (err if out is None else (out.stderr.strip() or out.stdout.strip())[:200])
        email, name = identities({"username": username}, [])
        return {"id": made["id"], "name": p.hostname, "username": username, "email": email, "login": name,
                "password": password, "totp": "", "uris": [{"uri": origin, "match": None}]}, None

    def signup(self, why, username, page="", submit=False, length=PASSWORD_LENGTH, symbols=True, timeout=ASK_TIMEOUT):
        """A new account: asked on their phone, then a made-up password saved in Bitwarden and typed into the form."""
        if self.items is None:
            return False, "the vault is locked; ask them to run `mochi-vault unlock` in a terminal"
        why, username = (why or "").strip(), (username or "").strip()
        if not why:
            return False, "say what it's for (--why): it goes on their phone with the question"
        if not username:
            return False, "say which username or email the account is for (--username)"
        ps = pages()
        if ps is None:
            return False, "Mochi's Chrome isn't reachable on its debugging port"
        cands = [t for t in ps if page in t.get("url", "")] if page else ps
        if len(cands) != 1:
            return False, (f"{len(cands)} tabs are open; name the sign-up tab with --page (part of its address)" if cands
                           else "no open tab " + (f"matching {page!r}" if page else "in Mochi's Chrome"))
        target = cands[0]
        try:
            places = [f for f in frames(target) if safe_page(f[0])]
        except OSError as e:
            return False, f"couldn't reach the page: {e}"
        frame = next((f for f in places if probe(f, signup=True)), None)
        if not frame:
            url = target.get("url", "")
            if not safe_page(url):
                return False, f"not making an account on {url.split('?')[0]}: only https (or plain http on the local network)"
            return False, f"no password box on {urlsplit(url).hostname}; open the sign-up form first"
        url = frame[0]
        host = urlsplit(url).hostname or ""
        same = [it for it in matching(self.items, url) if username.lower() in
                (it["username"].lower(), it.get("email", "").lower(), it.get("login", "").lower())]
        if same:
            return False, (f"they already have a login for {host} as {username} ({same[0]['name']}); "
                           f"use `mochi-vault fill` to sign in with it")
        ok, msg = self.ask(host, why, [{"name": "Create", "username": username}], timeout, kind="create")
        if not ok:
            return False, msg
        item, err = self.save_login(url, username, make_password(length, symbols), why)
        if not item:
            log(f"signup on {host} as {username}: not saved ({err})")
            return False, err + "; nothing was filled"
        with self.lock:
            if self.items is not None:
                self.items.append(item)
            self.leases[(host, item["id"])] = time.time() + LEASE_MINUTES * 60
        log(f"saved a new login for {host} as {username} in Bitwarden ({why[:80]})")
        try:
            res = fill_page(frame, host, item, submit, signup=True)
        except OSError as e:
            res = {"ok": False, "error": f"couldn't reach the page: {e}"}
        if not res.get("ok"):
            log(f"signup fill on {host} failed: {res.get('error')}")
            return False, (f"saved the new login for {host} in their Bitwarden, but couldn't fill the form: "
                           f"{res.get('error')}. `mochi-vault fill --no-submit` can try again without asking")
        how = {"button": "pressed the submit button", "form": "submitted the form", "enter": "pressed Enter",
               "not submitted": "didn't submit it"}.get(res.get("how"), res.get("how"))
        log(f"signup: filled {', '.join(res['filled'])} on {host}, {how}")
        return True, (f"saved a new login for {host} ({username}) in their Bitwarden, filled "
                      f"{', '.join(res['filled'])} and {how}. Take a snapshot: the rest of the form (a captcha, a "
                      f"terms box) is still yours or theirs to do. `mochi-vault fill` signs in with it from now on.")

    def export(self, url, to, why, account="", timeout=ASK_TIMEOUT):
        """Copy one login into a file under ~/.config/mochi, for a tool that has to hold it itself (Deluge behind
        HTTP auth: no page to fill). Asked on their phone like a fill; written 0600; the values are never printed.
        A setup step for them, not for Mochi's runs (the relay never allows it there)."""
        if self.items is None:
            return False, "the vault is locked; ask them to run `mochi-vault unlock` in a terminal"
        why = (why or "").strip()
        if not why:
            return False, "say what it's for (--why): it goes on their phone with the question"
        path = Path(os.path.expanduser(to or ""))
        try:
            path.resolve().relative_to(CONF_DIR.resolve())
        except ValueError:
            return False, f"a login is copied only into a file under {CONF_DIR}"
        if path.is_dir() or not path.name:
            return False, "--to names a file"
        if not safe_page(url):
            return False, f"not for {url.split('?')[0]}: only https (or plain http on the local network)"
        host = urlsplit(url).hostname or ""
        items = matching(self.items, url)
        if account:
            items = [it for it in items if account.lower() in " ".join(
                [it["name"], it["username"], it.get("email", ""), it.get("login", "")]).lower()]
        if not items:
            return False, f"no login of theirs is saved for {host}" + (f" matching {account!r}" if account else "")
        ok, item = self.ask(host, why, items, timeout, kind="copy")
        if not ok:
            return False, item
        keys = [("USERNAME", item.get("username") or ""), ("EMAIL", item.get("email") or ""),
                ("LOGIN", item.get("login") or ""), ("PASSWORD", item.get("password") or "")]
        keys = [(k, v) for k, v in keys if v and "\n" not in v]
        text = (f"# {label(item)} for {host}: copied by `mochi-vault export` {dt.datetime.now():%Y-%m-%d %H:%M} ({why[:80]})\n"
                + "".join(f"{k}={v}\n" for k, v in keys))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.chmod(tmp, 0o600)
            tmp.replace(path)
        except OSError as e:
            return False, f"couldn't write {path}: {e}"
        finally:
            text = None
        log(f"copied the login {label(item)} for {host} into {path} ({why[:80]})")
        return True, f"copied the login {item['name']} for {host} into {path} ({', '.join(k for k, _ in keys)}; mode 600)"

    def ask(self, host, why, items, timeout, kind="fill", source=""):
        rid = secrets.token_hex(8)
        req = {"event": threading.Event(), "choice": None, "items": items, "reason": ""}
        with self.lock:
            self.pending[rid] = req
        try:
            if not self.notify(event="vault_ask", id=rid, host=host, why=why[:300], accounts=[label(i) for i in items],
                               kind=kind, **({"source": source} if source else {})):
                return False, "mochi-brain isn't running, so there's no way to ask them"
            log(f"asking to {dict(copy='copy the login for', create='make a new login for').get(kind, 'sign in on')} {host} ({len(items)} matching) for: {why[:80]}")
            if not req["event"].wait(timeout):
                self.notify(event="vault_done", id=rid, label="⌛ no answer in time")
                return False, "they didn't answer in time"
            c = req["choice"]
            if c is None:
                return False, req["reason"] or "they said no"
            return True, items[c]
        finally:
            with self.lock:
                self.pending.pop(rid, None)

    def decide(self, rid, choice, reason="", pid=None):
        """A tap on the phone, as the relay passes it on. Only the relay's own process may decide."""
        rp = self.relay_pid()
        if not rp or pid != rp:
            log(f"ignored a decision from pid {pid} (the relay is {rp or 'not running'})")
            return False, "only the relay decides"
        with self.lock:
            req = self.pending.get(rid)
            if not req:
                return False, "no such request (expired?)"
            try:
                req["choice"] = None if choice is None else int(choice)
                if req["choice"] is not None and not 0 <= req["choice"] < len(req["items"]):
                    raise ValueError
            except (TypeError, ValueError):
                req["choice"] = None
            req["reason"] = reason
            req["event"].set()
            return True, label(req["items"][req["choice"]]) if req["choice"] is not None else "denied"

    # -- the socket

    def handle(self, conn):
        try:
            conn.settimeout(30)
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            m = json.loads(data or b"{}")
            cmd = m.get("cmd")
            conn.settimeout(None)
            if cmd == "unlock":
                ok, msg = self.unlock(m.get("password") or "")
                m.clear()
            elif cmd == "lock":
                ok, msg = True, self.forget()
            elif cmd == "status":
                ok, msg = True, self.status()
            elif cmd == "has":
                ok, msg = True, self.has(str(m.get("url") or ""))
            elif cmd == "fill":
                ok, msg = self.fill(str(m.get("why") or ""), str(m.get("page") or ""), str(m.get("account") or ""),
                                    bool(m.get("submit", True)), from_site=str(m.get("from") or ""))
            elif cmd == "signup":
                ok, msg = self.signup(str(m.get("why") or ""), str(m.get("username") or ""), str(m.get("page") or ""),
                                      bool(m.get("submit")), int(m.get("length") or PASSWORD_LENGTH),
                                      bool(m.get("symbols", True)))
            elif cmd == "export":
                ok, msg = self.export(str(m.get("url") or ""), str(m.get("to") or ""), str(m.get("why") or ""),
                                      str(m.get("account") or ""))
            elif cmd == "decide":
                ok, msg = self.decide(str(m.get("id") or ""), m.get("choice"), str(m.get("reason") or ""), peer_pid(conn))
            else:
                ok, msg = False, f"unknown command {cmd!r}"
            conn.sendall(json.dumps({"ok": ok, "text": msg}).encode() + b"\n")
        except (OSError, ValueError) as e:
            try:
                conn.sendall(json.dumps({"ok": False, "text": str(e)}).encode() + b"\n")
            except OSError:
                pass
        finally:
            conn.close()

    def remembered(self):
        """Unlock from the keyring once it's there (it opens when they log in, maybe a little after this service
        starts), then re-read Bitwarden every REFRESH_EVERY while unlocked. Silent if nothing is remembered."""
        tries = 0
        while True:
            pw = keyring_get()
            if pw is None:
                tries += 1
                if tries == 20:  # ten minutes after the start: nothing remembered (or no keyring); stop looking
                    return
                time.sleep(30)
                continue
            tries = 21  # found once: from here on only refreshes
            if self.items is None and self.unlocked_at:  # they locked it by hand: stay locked until the next start
                pw = None
                time.sleep(REFRESH_EVERY)
                continue
            ok, msg = self.unlock(pw)
            pw = None
            if not ok:
                log(f"couldn't unlock from the keyring: {msg}")
            time.sleep(REFRESH_EVERY if ok else 300)

    def serve(self):
        threading.Thread(target=self.remembered, daemon=True).start()
        try:
            SOCK.unlink()
        except FileNotFoundError:
            pass
        old = os.umask(0o077)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(SOCK))
        os.umask(old)
        srv.listen(8)
        log(f"up, locked (bw: {self.bw or 'NOT FOUND'})")
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()


# ---- command line ------------------------------------------------------------------------------------

def request(msg, timeout=60):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(SOCK))
    except OSError:
        return {"ok": False, "text": "mochi-vault isn't running (systemctl --user start mochi-vault)"}
    try:
        s.sendall(json.dumps(msg).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        return json.loads(data or b'{"ok": false, "text": "no answer"}')
    except (OSError, ValueError) as e:
        return {"ok": False, "text": str(e)}
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(prog="mochi-vault", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("unlock")
    sub.add_parser("remember")
    sub.add_parser("forget")
    sub.add_parser("lock")
    sub.add_parser("status")
    h = sub.add_parser("has")
    h.add_argument("url")
    f = sub.add_parser("fill")
    f.add_argument("--why", required=True, help="what it's for; shown on their phone")
    f.add_argument("--page", default="", help="part of the tab's address, when more than one tab could be meant")
    f.add_argument("--account", default="", help="part of the login's name or username, when they have several")
    f.add_argument("--no-submit", action="store_true", help="fill but don't submit")
    f.add_argument("--from", dest="from_site", default="", help="the site the login is saved for, when the sign-in page "
                   "is on another address (on Allow, this page's site is added to the login)")
    s = sub.add_parser("signup", help="a new account: password made, saved in Bitwarden, typed into the form")
    s.add_argument("--why", required=True, help="what it's for; shown on their phone")
    s.add_argument("--username", required=True, help="the email or username the new account uses")
    s.add_argument("--page", default="", help="part of the sign-up tab's address, when more than one tab is open")
    s.add_argument("--submit", action="store_true", help="submit the form too (default: leave it for a captcha or terms)")
    s.add_argument("--length", type=int, default=PASSWORD_LENGTH, help="password length (at least 12)")
    s.add_argument("--no-symbols", action="store_true", help="letters and digits only, for sites that refuse symbols")
    e = sub.add_parser("export", help="copy one login into a file under ~/.config/mochi (asked on your phone)")
    e.add_argument("url", help="the site, as a URL: which of your logins")
    e.add_argument("--to", required=True, help="the file to write (USERNAME=, PASSWORD= lines, mode 600)")
    e.add_argument("--why", required=True, help="what it's for; shown on your phone")
    e.add_argument("--account", default="", help="part of the login's name or username, when you have several")
    a = ap.parse_args()
    if a.cmd == "serve":
        Vault().serve()
        return 0
    if a.cmd in ("unlock", "remember"):
        if not sys.stdin.isatty():
            sys.exit(f"mochi-vault {a.cmd}: run it yourself in a terminal (it asks for your master password)")
        pw = getpass.getpass("Bitwarden master password: ")
        r = request({"cmd": "unlock", "password": pw}, timeout=400)
        if a.cmd == "remember" and r.get("ok"):
            try:
                keyring_set(pw)
                r["text"] += "; remembered in your login keyring: it unlocks itself when you log in (`mochi-vault forget` undoes it)"
            except Exception as e:
                r = {"ok": False, "text": r["text"] + f"; but couldn't save it to the keyring: {e}"}
        pw = None
    elif a.cmd == "forget":
        try:
            gone = keyring_clear()
            r = {"ok": True, "text": "taken out of the keyring: unlock by hand from now on" if gone else "nothing was remembered"}
        except Exception as e:
            r = {"ok": False, "text": f"couldn't reach the keyring: {e}"}
    elif a.cmd == "fill":
        r = request({"cmd": "fill", "why": a.why, "page": a.page, "account": a.account, "submit": not a.no_submit,
                     "from": a.from_site}, timeout=ASK_TIMEOUT + 180)
    elif a.cmd == "signup":
        r = request({"cmd": "signup", "why": a.why, "username": a.username, "page": a.page, "submit": a.submit,
                     "length": a.length, "symbols": not a.no_symbols}, timeout=ASK_TIMEOUT + 180)
    elif a.cmd == "export":
        r = request({"cmd": "export", "url": a.url, "to": a.to, "why": a.why, "account": a.account}, timeout=ASK_TIMEOUT + 90)
    elif a.cmd == "has":
        r = request({"cmd": "has", "url": a.url})
    else:
        r = request({"cmd": a.cmd})
    print(r.get("text", ""))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
