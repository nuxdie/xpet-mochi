#!/usr/bin/env python3
"""mochi-tasks — their Google Tasks, through the Tasks API (OAuth, scope tasks).

    mochi-tasks auth [--no-browser]          once: sign in as them in a browser, keeps a refresh token
    mochi-tasks lists                        their task lists
    mochi-tasks list [--list L] [--all] [--json]   open tasks (--all: completed too), every list unless --list
    mochi-tasks digest                       open tasks, overdue and due soon first (mochi-sense tasks)
    mochi-tasks add TITLE [--notes T] [--due YYYY-MM-DD] [--list L] [--parent ID]
    mochi-tasks edit ID [--title T] [--notes T] [--due YYYY-MM-DD|none] [--list L]
    mochi-tasks done ID [--list L]  |  undone ID [--list L]
    mochi-tasks log [N]                      what Mochi changed

Google Tasks has no CalDAV and takes no app passwords, so unlike the calendar this needs an OAuth client of their
own: a Google Cloud project with the Tasks API on and a "Desktop app" OAuth client, its JSON saved where
tasks.client_file in ~/.config/mochi/sources.json points (default ~/.config/mochi/google-oauth-client.json). Publish
the consent screen ("In production"): in "Testing" Google expires the refresh token after 7 days. `auth` runs the
loopback flow and keeps the token in tasks.token_file (mode 600). L is a list's title or id. There is no delete:
a task Mochi shouldn't have added gets marked done, and every change is logged to tasks.log in Mochi's workspace.
"""

import argparse
import base64
import datetime as dt
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
WORK = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "mochi"
LOG = WORK / "tasks.log"
CONF_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "mochi"
CONFIG = CONF_DIR / "sources.json"
API = "https://tasks.googleapis.com/tasks/v1"
SCOPE = "https://www.googleapis.com/auth/tasks"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SOON_DAYS = 7


def conf():
    try:
        c = json.loads(CONFIG.read_text()).get("tasks", {})
    except (OSError, ValueError):
        c = {}
    return (Path(os.path.expanduser(c.get("client_file") or CONF_DIR / "google-oauth-client.json")),
            Path(os.path.expanduser(c.get("token_file") or CONF_DIR / "google-tasks.token.json")))


def client():
    path, _ = conf()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        sys.exit(f"mochi-tasks: no OAuth client at {path} (see `mochi-tasks --help`)")
    c = data.get("installed") or data.get("web") or data
    if not c.get("client_id"):
        sys.exit(f"mochi-tasks: {path} has no client_id")
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


def save_token(tok):
    _, path = conf()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tok, f)


def access_token():
    _, path = conf()
    try:
        tok = json.loads(path.read_text())
    except (OSError, ValueError):
        sys.exit("mochi-tasks: not signed in (they run `mochi-tasks auth` once)")
    if tok.get("access_token") and tok.get("expires_at", 0) > time.time() + 60:
        return tok["access_token"]
    c = client()
    r = post_form(TOKEN_URL, {"client_id": c["client_id"], "client_secret": c.get("client_secret", ""),
                              "refresh_token": tok.get("refresh_token", ""), "grant_type": "refresh_token"})
    if r.get("_error"):
        hint = " — the sign-in expired or was revoked; they run `mochi-tasks auth` again" if r["_error"] == "invalid_grant" else ""
        sys.exit(f"mochi-tasks: token refresh failed: {r['_error']} {r['_detail']}{hint}")
    tok.update(access_token=r["access_token"], expires_at=time.time() + int(r.get("expires_in", 3600)))
    save_token(tok)
    return tok["access_token"]


def api(method, path, body=None, params=None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + access_token(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        sys.exit(f"mochi-tasks: {method} {path}: HTTP {e.code} {msg}")
    except urllib.error.URLError as e:
        sys.exit(f"mochi-tasks: {e.reason}")


def paged(path, params):
    params, items = dict(params), []
    while True:
        r = api("GET", path, params=params)
        items += r.get("items", [])
        if not r.get("nextPageToken"):
            return items
        params["pageToken"] = r["nextPageToken"]


# ---- auth ---------------------------------------------------------------------------------------------

def cmd_auth(a):
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
            self.wfile.write("Mochi can see your tasks now. You can close this tab.".encode() if "code" in q
                             else f"Not signed in: {q.get('error')}".encode())

        def log_message(self, *_):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{srv.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print(f"Open this in the browser where you're signed in to Google:\n\n{url}\n", flush=True)
    if not a.no_browser:
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    srv.timeout = 10
    deadline = time.time() + 600
    while not got and time.time() < deadline:
        srv.handle_request()  # stray requests (favicon, a wrong state) don't end the wait
    if "code" not in got:
        sys.exit(f"mochi-tasks: sign-in failed: {got.get('error', 'no answer')}")
    r = post_form(TOKEN_URL, {"client_id": c["client_id"], "client_secret": c.get("client_secret", ""),
                              "code": got["code"], "code_verifier": verifier, "redirect_uri": redirect,
                              "grant_type": "authorization_code"})
    if r.get("_error"):
        sys.exit(f"mochi-tasks: token exchange failed: {r['_error']} {r['_detail']}")
    if not r.get("refresh_token"):
        sys.exit("mochi-tasks: Google returned no refresh token; remove the app's access at "
                 "myaccount.google.com/permissions and run auth again")
    save_token({"refresh_token": r["refresh_token"], "access_token": r["access_token"],
                "expires_at": time.time() + int(r.get("expires_in", 3600))})
    lists = paged("/users/@me/lists", {"maxResults": 100})
    print(f"signed in; {len(lists)} task lists: " + ", ".join(l["title"] for l in lists))


# ---- reading ------------------------------------------------------------------------------------------

def all_lists():
    return paged("/users/@me/lists", {"maxResults": 100})


def pick_list(name, lists=None):
    lists = lists if lists is not None else all_lists()
    for l in lists:
        if name in (l["id"], l["title"]) or l["title"].lower() == name.lower():
            return l
    sys.exit(f"mochi-tasks: no list {name!r} (have: {', '.join(l['title'] for l in lists)})")


def tasks_of(list_id, completed=False):
    params = {"maxResults": 100, "showCompleted": "true" if completed else "false", "showHidden": "true" if completed else "false"}
    return paged(f"/lists/{list_id}/tasks", params)


def due_date(t):
    return dt.date.fromisoformat(t["due"][:10]) if t.get("due") else None


def due_text(d, today=None):
    if not d:
        return ""
    today = today or dt.date.today()
    days = (d - today).days
    rel = ("today" if days == 0 else "tomorrow" if days == 1 else "yesterday" if days == -1 else
           f"{-days} days overdue" if days < 0 else f"in {days} days")
    return f"{d:%a %Y-%m-%d} ({rel})"


def line(t, today=None):
    box = "[x]" if t.get("status") == "completed" else "[ ]"
    parts = [f"- {box} {t.get('title') or '(untitled)'}"]
    if t.get("due"):
        parts.append(f"due {due_text(due_date(t), today)}")
    if t.get("parent"):
        parts.append("subtask")
    parts.append(f"id={t['id']}")
    out = " · ".join(parts)
    if t.get("notes"):
        out += "\n    " + " ".join(t["notes"].split())[:200]
    return out


def snapshot(list_name=None, completed=False):
    lists = all_lists()
    if list_name:
        lists = [pick_list(list_name, lists)]
    return [{"id": l["id"], "title": l["title"], "tasks": tasks_of(l["id"], completed)} for l in lists]


def cmd_lists(a):
    for l in all_lists():
        print(f"{l['title']}  (id={l['id']}, updated {l.get('updated', '')[:16].replace('T', ' ')})")


def cmd_list(a):
    snap = snapshot(a.list, a.all)
    if a.json:
        print(json.dumps(snap, ensure_ascii=False))
        return
    for l in snap:
        print(f"## {l['title']} ({len(l['tasks'])})")
        for t in sorted(l["tasks"], key=lambda t: t.get("position", "")):
            print(line(t))


def digest(snap, today=None):
    today = today or dt.date.today()
    open_ = [(l["title"], t) for l in snap for t in l["tasks"] if t.get("status") != "completed"]
    dated = sorted(((due_date(t), n, t) for n, t in open_ if t.get("due")), key=lambda x: x[0])
    urgent = [(d, n, t) for d, n, t in dated if (d - today).days <= SOON_DAYS]
    out = [f"{len(open_)} open across {len(snap)} lists"
           f" ({', '.join(f'{l['title']} {sum(t.get('status') != 'completed' for t in l['tasks'])}' for l in snap)})"]
    if urgent:
        out.append(f"\n**Overdue and due within {SOON_DAYS} days:**")
        out += [f"{line(t, today)} · in {n}" for _, n, t in urgent]
    rest = [(n, t) for n, t in open_ if not t.get("due") or (due_date(t) - today).days > SOON_DAYS]
    rest.sort(key=lambda x: x[1].get("updated", ""), reverse=True)
    if rest:
        out.append("\n**Other open tasks** (most recently touched first):")
        out += [f"{line(t, today)} · in {n}" for n, t in rest[:25]]
        if len(rest) > 25:
            out.append(f"_…and {len(rest) - 25} more: `mochi-tasks list`_")
    return "\n".join(out)


def cmd_digest(a):
    print(digest(snapshot()))


# ---- changing -----------------------------------------------------------------------------------------

def log(what, list_title, t):
    WORK.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M} {what} list={list_title!r} title={t.get('title')!r} id={t.get('id')}\n")


def find_task(task_id, list_name=None):
    lists = [pick_list(list_name)] if list_name else all_lists()
    for l in lists:
        for t in tasks_of(l["id"], completed=True):
            if t["id"] == task_id:
                return l, t
    sys.exit(f"mochi-tasks: no task {task_id}")


def due_field(s):
    try:
        return dt.date.fromisoformat(s).isoformat() + "T00:00:00.000Z"  # Tasks keeps the date only
    except ValueError:
        sys.exit(f"mochi-tasks: --due wants YYYY-MM-DD, not {s!r}")


def cmd_add(a):
    if not a.title.strip():
        sys.exit("mochi-tasks: empty title")
    l = pick_list(a.list) if a.list else all_lists()[0]
    body = {"title": a.title.strip()}
    if a.notes:
        body["notes"] = a.notes
    if a.due:
        body["due"] = due_field(a.due)
    t = api("POST", f"/lists/{l['id']}/tasks", body, {"parent": a.parent} if a.parent else None)
    log("added", l["title"], t)
    print(f"added to {l['title']}: {line(t)}")


def cmd_edit(a):
    l, t = find_task(a.id, a.list)
    body = {}
    if a.title is not None:
        body["title"] = a.title
    if a.notes is not None:
        body["notes"] = a.notes
    if a.due is not None:
        body["due"] = None if a.due == "none" else due_field(a.due)
    if not body:
        sys.exit("mochi-tasks: nothing to change")
    t = api("PATCH", f"/lists/{l['id']}/tasks/{t['id']}", body)
    log("edited " + ",".join(body), l["title"], t)
    print(line(t))


def set_status(a, status):
    l, t = find_task(a.id, a.list)
    body = {"status": status}
    if status == "needsAction":
        body["completed"] = None
    t = api("PATCH", f"/lists/{l['id']}/tasks/{t['id']}", body)
    log("done" if status == "completed" else "reopened", l["title"], t)
    print(line(t))


def cmd_log(a):
    if LOG.exists():
        sys.stdout.write("".join(LOG.read_text().splitlines(True)[-a.n:]))
    else:
        print("nothing changed yet")


def main():
    ap = argparse.ArgumentParser(prog="mochi-tasks", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("auth")
    s.add_argument("--no-browser", action="store_true")
    sub.add_parser("lists")
    s = sub.add_parser("list")
    s.add_argument("--list")
    s.add_argument("--all", action="store_true")
    s.add_argument("--json", action="store_true")
    sub.add_parser("digest")
    s = sub.add_parser("add")
    s.add_argument("title")
    s.add_argument("--notes")
    s.add_argument("--due")
    s.add_argument("--list")
    s.add_argument("--parent")
    s = sub.add_parser("edit")
    s.add_argument("id")
    s.add_argument("--title")
    s.add_argument("--notes")
    s.add_argument("--due")
    s.add_argument("--list")
    for name in ("done", "undone"):
        s = sub.add_parser(name)
        s.add_argument("id")
        s.add_argument("--list")
    s = sub.add_parser("log")
    s.add_argument("n", nargs="?", type=int, default=20)
    a = ap.parse_args()
    {"auth": cmd_auth, "lists": cmd_lists, "list": cmd_list, "digest": cmd_digest, "add": cmd_add, "edit": cmd_edit,
     "done": lambda a: set_status(a, "completed"), "undone": lambda a: set_status(a, "needsAction"),
     "log": cmd_log}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
