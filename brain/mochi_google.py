#!/usr/bin/env python3
"""mochi-google — Mochi's sign-in to their Google accounts (OAuth), shared by mochi-drive, mochi-calendar and
mochi-contacts.

    mochi-google auth ACCOUNT [--no-browser]   once per account: sign in as them in a browser, keeps a refresh token
    mochi-google accounts                      which accounts are signed in, as whom, and what each may reach

App passwords only open IMAP/SMTP/CalDAV; the Drive, Docs, Sheets, Calendar and People APIs need OAuth. This uses the
OAuth client of the "Mochi" project in Google Cloud (the one mochi-tasks uses, with those APIs enabled), and keeps one
token per account in ~/.config/mochi/google.ACCOUNT.token.json (mode 600) with every scope in SCOPES, so one consent
covers all three tools. Accounts are named as in mail.accounts in ~/.config/mochi/sources.json (artem, nuxdie), or
google.accounts if that is set.

The tools load this file as a module from beside themselves (brain/mochi_google.py in the repo, mochi-google once
installed); see load() at the bottom of each.
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home()
CONF_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "mochi"
CONFIG = CONF_DIR / "sources.json"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE = "https://www.googleapis.com/auth/drive"  # Drive, and the Docs and Sheets APIs too
CALENDAR = "https://www.googleapis.com/auth/calendar"
CONTACTS = "https://www.googleapis.com/auth/contacts.readonly"
OTHER_CONTACTS = "https://www.googleapis.com/auth/contacts.other.readonly"  # people they mailed, never saved
SCOPES = {DRIVE: "drive", CALENDAR: "calendar", CONTACTS: "contacts", OTHER_CONTACTS: "other contacts"}
PROG = "mochi-google"  # the tool's name in messages; each tool sets it


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"HTTP {code} {msg}")
        self.code = code


def die(msg):
    sys.exit(f"{PROG}: {msg}")


def conf():
    try:
        c = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        c = {}
    g = c.get("google", {})
    accounts = g.get("accounts") or c.get("mail", {}).get("accounts") or {}
    client = g.get("client_file") or c.get("tasks", {}).get("client_file") or CONF_DIR / "google-oauth-client.json"
    return accounts, Path(os.path.expanduser(client))


def token_path(account):
    return CONF_DIR / f"google.{account}.token.json"


def load_token(account):
    try:
        return json.loads(token_path(account).read_text())
    except (OSError, ValueError):
        return None


def signed_in(scope=None):
    """Accounts with a token, or only those whose token covers SCOPE."""
    out = []
    for a in conf()[0]:
        tok = load_token(a)
        if tok and (scope is None or scope in tok.get("scopes", [])):
            out.append(a)
    return out


def accounts_for(account, scope):
    """The accounts a command runs in: ACCOUNT if given, else every one signed in with SCOPE."""
    accounts = conf()[0]
    if account:
        if account not in accounts:
            die(f"no account {account!r} (have: {', '.join(accounts) or 'none in sources.json'})")
        return [account]
    have = signed_in(scope)
    if not have:
        die(f"no account is signed in for {SCOPES[scope]} (they run `mochi-google auth ACCOUNT` once; accounts: "
            + ", ".join(accounts) + ")")
    return have


def client():
    path = conf()[1]
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        die(f"no OAuth client at {path} (see `mochi-google --help`)")
    c = data.get("installed") or data.get("web") or data
    if not c.get("client_id"):
        die(f"{path} has no client_id")
    return c


def post_form(url, fields):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            err = json.loads(body)
        except ValueError:
            err = {"error": body[:200]}
        return {"_error": err.get("error"), "_detail": err.get("error_description", "")}


def save_token(account, tok):
    path = token_path(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tok, f)


def access_token(account, scope=None):
    tok = load_token(account)
    if not tok:
        die(f"{account} is not signed in (they run `mochi-google auth {account}` once)")
    if scope and scope not in tok.get("scopes", []):
        die(f"{account}'s sign-in doesn't cover {SCOPES[scope]}; they run `mochi-google auth {account}` again")
    if tok.get("access_token") and tok.get("expires_at", 0) > time.time() + 60:
        return tok["access_token"]
    c = client()
    r = post_form(TOKEN_URL, {"client_id": c["client_id"], "client_secret": c.get("client_secret", ""),
                              "refresh_token": tok.get("refresh_token", ""), "grant_type": "refresh_token"})
    if r.get("_error"):
        hint = f" — the sign-in expired or was revoked; they run `mochi-google auth {account}` again" if r["_error"] == "invalid_grant" else ""
        die(f"{account}: token refresh failed: {r['_error']} {r['_detail']}{hint}")
    tok.update(access_token=r["access_token"], expires_at=time.time() + int(r.get("expires_in", 3600)))
    save_token(account, tok)
    return tok["access_token"]


def request(account, method, url, body=None, params=None, raw=None, ctype="application/json", scope=None):
    """One API call as ACCOUNT. body is JSON; raw is bytes sent as is. Returns parsed JSON, or bytes for media."""
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data, headers={
        "Authorization": "Bearer " + access_token(account, scope), "Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = r.read()
            if "json" in r.headers.get("Content-Type", ""):
                return json.loads(out) if out else {}
            return out
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        raise ApiError(e.code, msg) from None
    except urllib.error.URLError as e:
        die(str(e.reason))


def call(account, method, url, **kw):
    try:
        return request(account, method, url, **kw)
    except ApiError as e:
        die(f"{account}: {method} {url.split('googleapis.com')[-1].split('?')[0]}: {e}")


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# ---- the commands -----------------------------------------------------------------------------------------

def cmd_auth(a):
    accounts = conf()[0]
    if a.account not in accounts:
        die(f"no account {a.account!r}; name one of: {', '.join(accounts) or '(none: add google.accounts to sources.json)'}")
    email = accounts[a.account]
    c = client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if q.get("state") != state:
                self.send_response(400)
                self.end_headers()
                return
            got.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"Mochi is signed in to {email}. You can close this tab.".encode() if "code" in q
                             else f"Not signed in: {q.get('error')}".encode())

        def log_message(self, *_):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{srv.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": " ".join(SCOPES),
        "access_type": "offline", "prompt": "consent", "state": state, "login_hint": email,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print(f"Open this in a browser where {email} is signed in to Google:\n\n{url}\n", flush=True)
    if not a.no_browser:
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    srv.timeout = 10
    deadline = time.time() + 600
    while not got and time.time() < deadline:
        srv.handle_request()  # stray requests (favicon, a wrong state) don't end the wait
    if "code" not in got:
        die(f"sign-in failed: {got.get('error', 'no answer')}")
    r = post_form(TOKEN_URL, {"client_id": c["client_id"], "client_secret": c.get("client_secret", ""),
                              "code": got["code"], "code_verifier": verifier, "redirect_uri": redirect,
                              "grant_type": "authorization_code"})
    if r.get("_error"):
        die(f"token exchange failed: {r['_error']} {r['_detail']}")
    if not r.get("refresh_token"):
        die("Google returned no refresh token; remove Mochi's access at myaccount.google.com/permissions and run auth again")
    granted = [s for s in r.get("scope", "").split() if s in SCOPES]
    old = token_path(a.account).read_bytes() if token_path(a.account).exists() else None
    save_token(a.account, {"refresh_token": r["refresh_token"], "access_token": r["access_token"], "scopes": granted,
                           "expires_at": time.time() + int(r.get("expires_in", 3600))})
    me = whoami(a.account)
    if me and me.lower() != email.lower():
        if old:
            token_path(a.account).write_bytes(old)
        else:
            token_path(a.account).unlink()
        die(f"signed in as {me}, not {email}; run it again and pick {email}")
    missing = [SCOPES[s] for s in SCOPES if s not in granted]
    print(f"signed in as {me or email}" + (f"; NOT granted: {', '.join(missing)} (tick those boxes next time)" if missing else ""))
    cmd_accounts(a)


def whoami(account):
    """The account's email, from whichever API the token can reach."""
    tok = load_token(account) or {}
    try:
        if DRIVE in tok.get("scopes", []):
            return request(account, "GET", "https://www.googleapis.com/drive/v3/about",
                           params={"fields": "user(emailAddress)"})["user"]["emailAddress"]
        if CALENDAR in tok.get("scopes", []):
            return request(account, "GET", "https://www.googleapis.com/calendar/v3/calendars/primary")["id"]
    except ApiError:
        return None
    return None


def cmd_accounts(a):
    for acc, email in conf()[0].items():
        tok = load_token(acc)
        if not tok:
            print(f"- {acc} ({email}): not signed in — `mochi-google auth {acc}`")
            continue
        try:
            me = whoami(acc)
        except SystemExit as e:
            print(f"- {acc} ({email}): {str(e).split(': ', 1)[-1]}")
            continue
        reach = ", ".join(SCOPES[s] for s in SCOPES if s in tok.get("scopes", [])) or "nothing"
        print(f"- {acc} ({me or email}): ok, {reach}")


def main():
    ap = argparse.ArgumentParser(prog=PROG, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("auth")
    s.add_argument("account")
    s.add_argument("--no-browser", action="store_true")
    sub.add_parser("accounts")
    a = ap.parse_args()
    {"auth": cmd_auth, "accounts": cmd_accounts}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
