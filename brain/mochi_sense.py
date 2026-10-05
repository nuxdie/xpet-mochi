#!/usr/bin/env python3
"""mochi-sense — Mochi's senses: read-only digests of the places your life is written down.

Each subcommand looks at one source and prints a short markdown digest for Claude to read. Nothing here
decides anything; it only turns raw stores (sqlite history files, a Telegram archive, an LLM chat
archive, a NAS, the LAN, shell history, Claude Code transcripts, git repos) into a few lines a round can
afford to read. `mochi-sense all` runs every sensor and writes senses/digest.md in Mochi's workspace.

Sources and credentials are described in ~/.config/mochi/sources.json (see DEFAULT_CONFIG). Secrets are
read at run time from the files named there and never printed. Where a service needs a login and none is
configured, the sensor falls back to the session cookie of your own browser (Firefox), read from a copy of
cookies.sqlite -- it is your archive, on your LAN, and Mochi only reads.

Everything is read-only: sqlite files are copied before being opened, the NAS is listed with smbclient,
the web services are only GET.
"""

import datetime as dt
import glob
import http.cookiejar
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

HOME = Path.home()
WORK = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "mochi"
SENSES = WORK / "senses"
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "mochi" / "sources.json"

DEFAULT_CONFIG = {
    # Everything specific to your machines lives in ~/.config/mochi/sources.json; these are only safe, empty
    # defaults. See sources.example.json in the repo for the shape.
    "hosts": {},          # name: hostname. `network` pings these and notes which are up.
    "web_services": {},   # name: url. Shown in `network` and `sources`.
    "tg_archive": {"url": "", "password": "", "password_file": "", "password_key": "ADMIN_PASSWORD",
                   "cookie_name": "tg_archive_admin_session"},
    "llm_archive": {"url": "", "password": "", "local_db": ""},
    "immich": {"url": "", "api_key": "", "cookie_name": "immich_access_token"},
    "home_assistant": {"url": "", "token": "", "token_file": ""},
    "ssh": {  # hosts Mochi may look at over ssh with its own key (read-only commands), once the key is installed there
        "key": str(HOME / ".ssh/mochi_ed25519"),
        "hosts": {},      # name: user@host
    },
    "nas": {"share": "", "credentials": "", "watch": ["Documents", "Scans", "Backup", "Downloads", "Photos"]},
    "browsers": {  # name: history sqlite (Firefox places.sqlite or Chromium History). Globs allowed.
        "firefox": str(HOME / ".mozilla/firefox/*.default-release/places.sqlite"),
        "chromium": str(HOME / ".config/chromium/Default/History"),
        "chrome": str(HOME / ".config/google-chrome/Default/History"),
    },
    "firefox_profile": str(HOME / ".mozilla/firefox/*.default-release"),
    "shell_history": [str(HOME / ".bash_history"), str(HOME / ".zsh_history")],
    "claude_projects": str(HOME / ".claude/projects"),
    "repo_roots": [str(HOME)], "repo_depth": 3,
    "ignore_domains": ["accounts.google.com", "localhost", "127.0.0.1"],
    "mail": {  # local Maildir pulled by mbsync and indexed by notmuch
        "maildir": str(HOME / "Mail"),
        "accounts": {},   # name: address, e.g. {"work": "me@example.com"}
        "name": "",       # display name on outgoing mail
        "password_dir": str(HOME / ".config/mochi/mail"),  # read by mbsync only; never by this script
    },
    # Their YouTube channel: listed and read (auto-captions) with yt-dlp. The distro's yt-dlp goes stale fast;
    # `make tools` puts a current one in ~/.local/share/mochi-tools.
    "youtube": {"channel": "", "ytdlp": "~/.local/share/mochi-tools/bin/yt-dlp"},
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        user = json.loads(CONFIG_FILE.read_text())
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except (OSError, ValueError):
        pass
    return cfg


CFG = load_config()
TMP = Path(tempfile.mkdtemp(prefix="mochi-sense-"))


def cleanup():
    shutil.rmtree(TMP, ignore_errors=True)


def run(cmd, timeout=30, **kw):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, "", str(e)


def snapshot_sqlite(path):
    """Copy a (possibly locked, WAL-backed) sqlite file and open the copy read-only."""
    src = Path(path)
    dst = TMP / (src.name + f".{abs(hash(str(src)))}")
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-journal"):
        if (s := Path(str(src) + suffix)).exists():
            shutil.copy2(s, Path(str(dst) + suffix))
    con = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def first_glob(pattern):
    hits = sorted(glob.glob(pattern))
    return hits[0] if hits else None


def ago(ts):
    if not ts:
        return "?"
    s = time.time() - ts
    if s < 3600:
        return f"{int(s // 60)}m ago"
    if s < 2 * 86400:
        return f"{int(s // 3600)}h ago"
    return f"{int(s // 86400)}d ago"


def host_of(url):
    try:
        h = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def secret_from_file(path, key):
    try:
        m = re.search(rf"^{re.escape(key)}=(.*)$", Path(path).read_text(), re.M)
        return m.group(1).strip().strip('"').strip("'") if m else ""
    except OSError:
        return ""


def firefox_cookie(host, name):
    prof = first_glob(CFG["firefox_profile"])
    if not prof or not (Path(prof) / "cookies.sqlite").exists():
        return ""
    try:
        con = snapshot_sqlite(Path(prof) / "cookies.sqlite")
        row = con.execute("select value from moz_cookies where host=? and name=? order by lastAccessed desc limit 1",
                          (host, name)).fetchone()
        return row[0] if row else ""
    except sqlite3.Error:
        return ""


def http_json(url, headers=None, timeout=20, data=None):
    req = urllib.request.Request(url, headers=headers or {}, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def md_table(rows, head):
    if not rows:
        return "_nothing_"
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "/").replace("\n", " ")[:80] for c in r) + " |")
    return "\n".join(out)


VALUE_OPTS = {"--days", "-d", "--limit", "--lines", "--grep", "--page", "--date", "--chars", "--provider", "--part", "--depth"}


def split_args(args):
    """(positionals, {option: value}) — every option in VALUE_OPTS takes one value, the rest are flags."""
    pos, opts, i = [], {}, 0
    while i < len(args):
        a = args[i]
        if a in VALUE_OPTS and i + 1 < len(args):
            opts[a] = args[i + 1]
            i += 2
            continue
        if a.startswith("-"):
            opts[a] = True
        else:
            pos.append(a)
        i += 1
    return pos, opts


def days_arg(args, default):
    v = split_args(args)[1].get("--days") or split_args(args)[1].get("-d")
    return max(1, int(v)) if v else default


def opt(args, name):
    v = split_args(args)[1].get(name)
    return v if isinstance(v, str) else None


def positional(args):
    return split_args(args)[0]


# ---- browser history --------------------------------------------------------------------------------

def browser_visits(days):
    """Yield (ts, url, title, browser) for every visit in the window, across the configured browsers."""
    since = time.time() - days * 86400
    for name, pattern in CFG["browsers"].items():
        path = first_glob(pattern)
        if not path:
            continue
        try:
            con = snapshot_sqlite(path)
            if name == "firefox" or path.endswith("places.sqlite"):
                q = ("select v.visit_date/1000000 ts, p.url, p.title from moz_historyvisits v "
                     "join moz_places p on p.id=v.place_id where v.visit_date > ? order by v.visit_date desc")
                rows = con.execute(q, (int(since * 1e6),))
            else:  # chromium family: microseconds since 1601
                q = ("select v.visit_time/1000000-11644473600 ts, u.url, u.title from visits v "
                     "join urls u on u.id=v.url where v.visit_time > ? order by v.visit_time desc")
                rows = con.execute(q, (int((since + 11644473600) * 1e6),))
            for ts, url, title in rows:
                yield ts, url or "", title or "", name
        except sqlite3.Error as e:
            print(f"_({name}: {e})_", file=sys.stderr)


SEARCH_PARAMS = {"duckduckgo.com": "q", "google.com": "q", "youtube.com": "search_query", "bing.com": "q",
                 "perplexity.ai": "q", "github.com": "q", "marktplaats.nl": "query", "bol.com": "searchtext",
                 "amazon.nl": "k", "aliexpress.com": "SearchText"}


def sense_browser(args):
    days = days_arg(args, 2)
    pat = opt(args, "--grep")
    visits = list(browser_visits(days))
    if not visits:
        return f"## Browser (last {days}d)\n_no history files found or nothing in the window_"
    domains, titles, searches, per_day, browsers = Counter(), {}, [], Counter(), Counter()
    seen_titles = set()
    for ts, url, title, br in visits:
        h = host_of(url)
        if not h or any(h == d or h.endswith("." + d) for d in CFG["ignore_domains"]):
            continue
        browsers[br] += 1
        domains[h] += 1
        per_day[dt.date.fromtimestamp(ts).isoformat()] += 1
        sp = urllib.parse.urlsplit(url)
        for dom, param in SEARCH_PARAMS.items():
            if h == dom or h.endswith("." + dom):
                q = urllib.parse.parse_qs(sp.query).get(param)
                if q and q[0].strip() and not q[0].startswith(("http", "www.")) and q[0] not in [s[1] for s in searches]:
                    searches.append((ts, q[0].strip()[:80]))
        key = (h, title[:60])
        if title and key not in seen_titles and len(titles) < 400:
            seen_titles.add(key)
            titles.setdefault(h, []).append((ts, title[:90]))
    out = [f"## Browser (last {days}d, {sum(browsers.values())} visits; " +
           ", ".join(f"{k} {v}" for k, v in browsers.most_common()) + ")"]
    out.append("Visits per day: " + ", ".join(f"{d} {n}" for d, n in sorted(per_day.items())))
    out.append("\n**Top sites**: " + ", ".join(f"{d} ({n})" for d, n in domains.most_common(25)))
    if searches:
        out.append("\n**Searches**: " + "; ".join(q for _, q in searches[:40]))
    if pat:
        rx = re.compile(pat, re.I)
        hits = [(ts, url, t) for ts, url, t, _ in visits if rx.search(url) or rx.search(t)]
        out.append(f"\n**Matching `{pat}`** ({len(hits)}):")
        for ts, url, t in hits[:60]:
            out.append(f"- {dt.datetime.fromtimestamp(ts):%m-%d %H:%M} {t[:80]} — {url[:100]}")
    else:
        out.append("\n**Pages by site** (newest first, titles only):")
        for h, _ in domains.most_common(18):
            ts_titles = sorted(titles.get(h, []), reverse=True)[:6]
            if ts_titles:
                out.append(f"- **{h}**: " + " · ".join(t for _, t in ts_titles))
    return "\n".join(out)


# ---- shell history ----------------------------------------------------------------------------------

def sense_shell(args):
    n = int(opt(args, "--lines") or 400)
    lines = []
    for pattern in CFG["shell_history"]:
        for p in glob.glob(pattern):
            try:
                lines.extend(Path(p).read_text(errors="replace").splitlines())
            except OSError:
                pass
    lines = [re.sub(r"^: \d+:\d;", "", l).strip() for l in lines if l.strip()]
    recent = lines[-n:]
    cmds = Counter(l.split()[0] for l in recent if l.split())
    hosts = Counter()
    dirs = Counter()
    for l in recent:
        for m in re.finditer(r"\b(?:[\w.-]+@)?([\w-]+\.local|\d+\.\d+\.\d+\.\d+)\b", l):
            hosts[m.group(1)] += 1
        if l.startswith("cd ") and len(l) > 3:
            dirs[l[3:].strip()] += 1
    scrub = re.compile(r"(?i)(password|passwd|token|secret|key)=\S+")
    out = [f"## Shell history (last {len(recent)} commands of {len(lines)} on file; no timestamps)",
           "**Commands**: " + ", ".join(f"{c} ({k})" for c, k in cmds.most_common(20)),
           "**Hosts touched**: " + (", ".join(f"{h} ({k})" for h, k in hosts.most_common(15)) or "-"),
           "**Directories**: " + (", ".join(f"{d} ({k})" for d, k in dirs.most_common(15)) or "-"),
           "**Last 25**:"]
    out += [f"- `{scrub.sub(r'\1=…', l)[:110]}`" for l in recent[-25:]]
    return "\n".join(out)


# ---- Telegram archive (a self-hosted tg-archive) ------------------------------------------------------

class TgArchive:
    def __init__(self):
        c = CFG["tg_archive"]
        self.base = c["url"].rstrip("/")
        self.cookie = ""
        self.why = ""
        pw = c.get("password") or (secret_from_file(c["password_file"], c["password_key"]) if c.get("password_file") else "")
        if pw:
            self.cookie = self.login(pw)
            if not self.cookie:
                self.why = "configured password rejected"
        if not self.cookie:
            v = firefox_cookie(host_of(self.base), c["cookie_name"])
            if v:
                self.cookie = f"{c['cookie_name']}={v}"
                self.why = self.why or "using your Firefox session"
        if not self.cookie:
            self.why = "no password configured and no browser session found"

    def login(self, pw):
        cj = http.cookiejar.CookieJar()

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NoRedirect)
        try:
            op.open(self.base + "/login", data=urllib.parse.urlencode({"password": pw, "next": "/"}).encode(), timeout=10)
        except urllib.error.HTTPError as e:
            if e.code not in (301, 302, 303):
                return ""
        except (urllib.error.URLError, OSError):
            return ""
        return "; ".join(f"{c.name}={c.value}" for c in cj) if len(cj) else ""

    def get(self, path, **params):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        return http_json(url, headers={"Cookie": self.cookie})


def tg_text(m):
    c = m.get("content") or {}
    t = c.get("text")
    if isinstance(t, list):
        t = "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in t)
    t = (t or "").strip()
    if not t and c.get("media"):
        t = f"[{(c['media'] or {}).get('type', 'media')}]"
    return re.sub(r"\s+", " ", t)


def tg_who(m):
    s = m.get("sender") or {}
    return s.get("name") or s.get("id") or "?"


def tg_self_id(tg):
    """Their own Telegram user id: the Saved Messages dialog, or the owner id the bot was paired with."""
    owner = str((CFG.get("telegram") or {}).get("owner_id") or "")
    if owner:
        return owner
    try:
        ds = tg.get("/api/dialogs")
        ds = ds if isinstance(ds, list) else ds.get("dialogs") or []
        return next((str(d.get("tgDialogId")) for d in ds if (d.get("entity") or {}).get("self")), "")
    except (urllib.error.URLError, OSError, ValueError):
        return ""


def dialog_name(d):
    e = d.get("entity") or {}
    if e.get("self"):
        return "Saved Messages (me)"
    n = e.get("title") or " ".join(x for x in (e.get("firstName"), e.get("lastName")) if x) or e.get("username")
    return n or d.get("name") or str(d.get("tgDialogId"))


def sense_telegram(args):
    tg = TgArchive()
    head = f"## Telegram archive ({CFG['tg_archive']['url']})"
    if not tg.cookie:
        return f"{head}\n_unreachable: {tg.why}. Set tg_archive.password (or password_file) in {CONFIG_FILE}._"
    pos = positional(args)
    sub = pos[0] if pos else "recent"
    try:
        chars = int(opt(args, "--chars") or 0)  # set: one message per line, text up to this long (deep reading)
        if sub == "search":
            q = " ".join(pos[1:])
            page = int(opt(args, "--page") or 1)
            d = tg.get("/api/messages/search", q=q, limit=int(opt(args, "--limit") or 25), page=page)
            pg = d.get("pagination", {})
            title = f"{head}\n**Search `{q}`**: {pg.get('totalCount', '?')} hits, page {page} of {pg.get('total', '?')}\n"
            if chars:
                return title + "\n".join(f"- {m.get('metadata', {}).get('originalDate', '')[:16]} [{m.get('chatName', '')[:30]}] "
                                          f"{tg_who(m)[:20]}: {tg_text(m)[:chars]}" for m in d.get("messages", []))
            rows = [(m.get("metadata", {}).get("originalDate", "")[:16], m.get("chatName", "")[:30], tg_who(m)[:20], tg_text(m)[:90])
                    for m in d.get("messages", [])]
            return title + md_table(rows, ["when", "chat", "from", "text"])
        if sub == "range":
            d = tg.get(f"/api/dialog/{pos[1]}/date-range")
            return f"{head}\n**Dialog {pos[1]}** spans: {json.dumps(d)[:300]}"
        if sub == "dialog":
            did = pos[1]
            limit = min(int(opt(args, "--limit") or 40), 100)
            date = opt(args, "--date")
            me = tg_self_id(tg)
            if date:  # history: the messages around the first one on or after this date, oldest first
                d = tg.get(f"/api/dialog/{did}/messages/cursor", date=date, limit=limit)
                msgs = d.get("messages", [])
                order = f"around {date}, oldest first" + ("; older exist" if d.get("hasOlder") else "") + \
                        ("; newer exist" if d.get("hasNewer") else "")
            else:
                page = int(opt(args, "--page") or 1)
                d = tg.get(f"/api/dialog/{did}/messages", limit=limit, page=page)
                msgs = d.get("messages", [])
                order = f"newest first, page {page} of {d.get('pagination', {}).get('total', '?')}"
            who = lambda m: "me" if me and str((m.get("sender") or {}).get("id")) == me else tg_who(m)[:20]
            if chars:
                return f"{head}\n**Dialog {did}** ({order}):\n" + "\n".join(
                    f"- {m.get('metadata', {}).get('originalDate', '')[:16]} {who(m)}: {tg_text(m)[:chars]}" for m in msgs)
            rows = [(m.get("metadata", {}).get("originalDate", "")[:16], who(m), tg_text(m)[:100]) for m in msgs]
            return f"{head}\n**Dialog {did}** ({order}):\n" + md_table(rows, ["when", "from", "text"])
        # recent / dialogs
        status = tg.get("/api/agent/status")
        dialogs = tg.get("/api/dialogs")
        dialogs = dialogs if isinstance(dialogs, list) else dialogs.get("dialogs") or []
        dialogs.sort(key=lambda d: (bool(d.get("pinned")), d.get("date") or 0), reverse=True)
        self_id = next((str(d.get("tgDialogId")) for d in dialogs if (d.get("entity") or {}).get("self")), "")
        days = days_arg(args, 3)
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")
        out = [head, f"Sync agent: {status.get('state')} (updated {status.get('updatedAt', '')[:16]}); "
                     f"{len(dialogs)} dialogs archived" + (f"; {tg.why}" if tg.why else "")]
        if sub == "dialogs":
            rows = [(dialog_name(d)[:35], (d.get('entity') or {}).get('className', ''), d.get("tgDialogId"),
                     d.get("messageCount", ""), "pinned" if d.get("pinned") else ("archived" if d.get("archived") else ""))
                    for d in dialogs[:int(opt(args, "--limit") or 60)]]
            out.append(md_table(rows, ["dialog", "type", "id", "msgs", ""]))
            return "\n".join(out)
        # The dialog list carries no "last message" date, so look at the newest message of the likeliest dialogs.
        probe = [d for d in dialogs if not d.get("archived")][:int(opt(args, "--limit") or 80)]
        from concurrent.futures import ThreadPoolExecutor

        def newest(d):
            try:
                return d, tg.get(f"/api/dialog/{d.get('tgDialogId')}/messages", limit=3).get("messages", [])
            except (urllib.error.URLError, OSError, ValueError):
                return d, []
        with ThreadPoolExecutor(8) as ex:
            pairs = list(ex.map(newest, probe))
        active = []
        for d, msgs in pairs:
            when = (msgs[0].get("metadata", {}).get("originalDate", "") if msgs else "")[:16]
            if when and when >= since:
                active.append((when, d, msgs))
        active.sort(key=lambda x: x[0], reverse=True)
        out.append(f"\n**Dialogs with messages in the last {days}d** ({len(active)} of {len(probe)} checked), newest first:")
        for when, d, msgs in active:
            kind = (d.get("entity") or {}).get("className", "")
            line = f"- **{dialog_name(d)[:40]}** ({kind}, id {d.get('tgDialogId')}, {d.get('messageCount', '?')} msgs)"
            for m in msgs[:2]:
                me = "me" if str((m.get("sender") or {}).get("id")) == self_id else tg_who(m)[:18]
                line += f"\n    - {m.get('metadata', {}).get('originalDate', '')[5:16]} {me}: {tg_text(m)[:110]}"
            out.append(line)
        return "\n".join(out)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return f"{head}\n_error: {e}_"


# ---- LLM chat archive (llm-chats-archive, live or its local sqlite copy) ---------------------------------

class LlmArchive:
    def __init__(self):
        c = CFG["llm_archive"]
        self.base = c["url"].rstrip("/")
        self.cookie = ""
        self.local = c.get("local_db") if c.get("local_db") and Path(c["local_db"]).exists() else ""
        self.why = ""
        if c.get("password"):
            try:
                cj = http.cookiejar.CookieJar()
                op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
                op.open(urllib.request.Request(self.base + "/api/auth/login", data=json.dumps({"password": c["password"]}).encode(),
                                               headers={"Content-Type": "application/json"}), timeout=10)
                self.cookie = "; ".join(f"{k.name}={k.value}" for k in cj)
            except (urllib.error.URLError, OSError):
                self.why = "live archive login failed"
        if not self.cookie:
            v = firefox_cookie(host_of(self.base), "session")
            if v and self.session_ok(f"session={v}"):
                self.cookie = f"session={v}"
        if not self.cookie:
            self.why = (self.why + "; " if self.why else "") + "no live login (set llm_archive.password); " + \
                       (f"using local copy {self.local}" if self.local else "no local copy either")

    def session_ok(self, cookie):
        try:
            return bool(http_json(self.base + "/api/auth/session", headers={"Cookie": cookie}, timeout=8).get("authenticated"))
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def get(self, path, **params):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        return http_json(url, headers={"Cookie": self.cookie})


def sense_llm(args):
    a = LlmArchive()
    head = f"## LLM chat archive ({CFG['llm_archive']['url']})"
    pos = positional(args)
    sub = pos[0] if pos else "recent"
    q = " ".join(pos[1:]) if sub == "search" else ""
    limit = int(opt(args, "--limit") or 25)
    chars = int(opt(args, "--chars") or 600)
    page = max(1, int(opt(args, "--page") or 1))
    provider = opt(args, "--provider")
    if a.cookie:
        try:
            if sub == "list":
                params = {"limit": min(limit, 100), "offset": (page - 1) * min(limit, 100)}
                if provider:
                    params["provider"] = provider
                d = a.get("/api/conversations", **params)
                return f"{head} (live)\n**Conversations, newest first, page {page}**:\n" + llm_list_rows(
                    [(c["id"], c.get("provider", ""), c.get("title") or "", (c.get("created_at") or "")[:10],
                      c.get("message_count", "")) for c in d.get("items", [])])
            if sub == "show":
                c = a.get(f"/api/conversations/{int(pos[1])}")
                return f"{head} (live)\n" + llm_show(c.get("provider", ""), c.get("title") or "", c.get("created_at") or "",
                                                       [(m.get("role"), m.get("created_at"), m.get("text") or "") for m in c.get("messages", [])],
                                                       chars)
            if sub == "search":
                d = a.get("/api/search", q=q, limit=min(limit, 50))
                rows = [(r.get("provider", ""), (r.get("title") or "")[:50], (r.get("created_at") or "")[:10], (r.get("snippet") or r.get("text") or "")[:90])
                        for r in d.get("results", d.get("items", []))]
                return f"{head} (live)\n**Search `{q}`**:\n" + md_table(rows, ["provider", "conversation", "when", "snippet"])
            d = a.get("/api/conversations", limit=min(limit, 100))
            rows = [(c.get("provider", ""), (c.get("title") or "")[:60], (c.get("updated_at") or c.get("created_at") or "")[:10])
                    for c in d.get("items", d.get("conversations", []))]
            dash = a.get("/api/dashboard")
            return f"{head} (live)\n{json.dumps(dash)[:300]}\n**Recent conversations**:\n" + md_table(rows, ["provider", "title", "updated"])
        except (urllib.error.URLError, OSError, ValueError) as e:
            a.why = f"live error {e}; falling back to local copy"
    if not a.local:
        return f"{head}\n_unreachable: {a.why}_"
    con = snapshot_sqlite(a.local)
    snap = dt.date.fromtimestamp(Path(a.local).stat().st_mtime).isoformat()
    out = [f"{head}\n_{a.why}; local copy last updated {snap}_"]
    if sub == "list":
        rows = con.execute("select c.id, c.provider, c.title, substr(c.created_at,1,10), count(m.id) from conversations c "
                           "left join messages m on m.conversation_id=c.id where (? is null or c.provider=?) group by c.id "
                           "order by coalesce(c.updated_at, c.created_at) desc limit ? offset ?",
                           (provider, provider, min(limit, 100), (page - 1) * min(limit, 100))).fetchall()
        out.append(f"**Conversations, newest first, page {page}**:\n" + llm_list_rows(rows))
        return "\n".join(out)
    if sub == "show":
        c = con.execute("select provider, title, created_at from conversations where id=?", (int(pos[1]),)).fetchone()
        if not c:
            return "\n".join(out + [f"_no conversation {pos[1]}_"])
        msgs = con.execute("select role, created_at, text from messages where conversation_id=? order by sequence, id",
                           (int(pos[1]),)).fetchall()
        out.append(llm_show(c[0], c[1], c[2] or "", msgs, chars))
        return "\n".join(out)
    if sub == "search":
        rows = con.execute("select f.provider, f.title, substr(f.text,1,120) t, c.created_at from messages_fts f "
                           "join conversations c on c.id=f.conversation_id where messages_fts match ? "
                           "order by rank limit ?", (q, min(limit, 50))).fetchall()
        out.append(f"**Search `{q}`**:\n" + md_table([(r[0], (r[1] or '')[:50], (r[3] or '')[:10], r[2]) for r in rows],
                                                   ["provider", "conversation", "when", "snippet"]))
    else:
        stats = con.execute("select provider, count(*), min(substr(created_at,1,10)), max(substr(created_at,1,10)) "
                            "from conversations group by provider").fetchall()
        out.append("**By provider**: " + "; ".join(f"{p} {n} ({a}..{b})" for p, n, a, b in stats))
        rows = con.execute("select provider, title, substr(coalesce(updated_at, created_at),1,10) from conversations "
                           "order by coalesce(updated_at, created_at) desc limit ?", (limit,)).fetchall()
        out.append("**Most recent conversations**:\n" + md_table(rows, ["provider", "title", "updated"]))
        topics = Counter()
        for (title,) in con.execute("select title from conversations where coalesce(updated_at,created_at) > date('now','-365 day')"):
            for w in re.findall(r"[A-Za-zА-Яа-я][\w-]{3,}", title or ""):
                if w.lower() not in STOP:
                    topics[w.lower()] += 1
        out.append("**Frequent words in titles (last year of the copy)**: " + ", ".join(f"{w} ({n})" for w, n in topics.most_common(30)))
    return "\n".join(out)


def llm_list_rows(rows):
    return md_table([(i, p, (t or "")[:60], d, n) for i, p, t, d, n in rows], ["id", "provider", "title", "created", "msgs"])


def llm_show(provider, title, created, msgs, chars):
    """A whole conversation for reading: their messages in full (up to 4x chars), the model's cut to chars."""
    out = [f"**{title}** ({provider}, {created[:10]}, {len(msgs)} messages)"]
    for role, at, text in msgs:
        text = (text or "").strip()
        if not text:
            continue
        cap = chars * 4 if role == "user" else chars
        out.append(f"\n[{role} {(at or '')[:16]}]\n{text[:cap]}" + (" […]" if len(text) > cap else ""))
    return "\n".join(out)


STOP = set("with from that this your what have into using about make help need want does when which there their "
           "would could should were they them then than some more over only also just like into onto code file files "
           "error issue question chat conversation untitled new".split())


# ---- NAS (Synology, over SMB) -------------------------------------------------------------------------

def smb(cmd, timeout=40):
    c = CFG["nas"]
    code, out, err = run(["smbclient", c["share"], "-A", c["credentials"], "-c", cmd], timeout=timeout)
    return code, out, err


def parse_smb_ls(out):
    rows = []
    for line in out.splitlines():
        m = re.match(r"\s{2}(.+?)\s+([DAHSRN]+)\s+(\d+)\s+(\w{3} \w{3}\s+\d+ \d\d:\d\d:\d\d \d{4})$", line)
        if not m:
            continue
        name, attrs, size, when = m.groups()
        if name in (".", ".."):
            continue
        try:
            ts = dt.datetime.strptime(when, "%a %b %d %H:%M:%S %Y").timestamp()
        except ValueError:
            ts = 0
        rows.append((name, "D" in attrs, int(size), ts))
    return rows


def nas_read(path, chars):
    """The text of one document on the NAS (txt/md/csv/html/pdf/docx/odt), fetched into a temp file and deleted."""
    path = path.strip().strip("/")
    ext = Path(path).suffix.lower()
    if not path or ext not in {".txt", ".md", ".csv", ".html", ".htm", ".json", ".pdf", ".docx", ".odt", ".rtf"}:
        return f"_can't read `{path}`: only text, pdf, docx and odt documents_"
    TMP.mkdir(parents=True, exist_ok=True)
    local = TMP / ("nas-read" + ext)
    try:
        d, name = (path.rsplit("/", 1) if "/" in path else ("", path))
        code, out, err = smb((f'cd "{d}"; ' if d else "") + f'get "{name}" "{local}"', timeout=90)
        if code != 0 or not local.exists():
            return f"_error: {(err or out).strip()[:200]}_"
        if ext == ".pdf":
            code, text, err = run(["pdftotext", "-layout", str(local), "-"], timeout=60)
        elif ext in (".docx", ".odt"):
            import zipfile
            with zipfile.ZipFile(local) as z:
                xml = z.read("word/document.xml" if ext == ".docx" else "content.xml").decode("utf-8", "replace")
            text = re.sub(r"<[^>]+>", "", re.sub(r"</(w:p|text:p|text:h)>", "\n", xml))
        else:
            text = local.read_text(errors="replace")
        text = text.strip()
        return f"**{path}** ({len(text)} chars{', cut' if len(text) > chars else ''}):\n\n{text[:chars]}"
    except (OSError, ValueError, KeyError) as e:
        return f"_error reading `{path}`: {e}_"
    finally:
        local.unlink(missing_ok=True)


NAS_INDEX = Path.home() / ".cache/mochi-sense/nas-index.tsv.gz"  # every file and folder on the share, crawled nightly


def nas_index_build():
    """Crawl the whole share (one recursive smbclient ls, many minutes) into NAS_INDEX: kind, size, mtime, path."""
    import gzip
    t0 = time.time()
    code, out, err = smb("recurse ON; ls", timeout=3 * 3600)
    if code != 0 and not out:
        return f"_crawl failed: {(err or out).strip()[:200]}_"
    rows, cur = [], ""
    for line in out.splitlines():
        if line.startswith("\\"):
            cur = line.strip().replace("\\", "/").strip("/")
            continue
        for name, is_dir, size, ts in parse_smb_ls(line + "\n"):
            rows.append(f"{'d' if is_dir else 'f'}\t{size}\t{int(ts)}\t{cur + '/' if cur else ''}{name}")
    NAS_INDEX.parent.mkdir(parents=True, exist_ok=True)
    tmp = NAS_INDEX.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        f.write("\n".join(rows) + "\n")
    tmp.replace(NAS_INDEX)
    return f"indexed {len(rows)} entries in {time.time() - t0:.0f}s → {NAS_INDEX}" + \
           (f" (smbclient exit {code}: {err.strip()[-150:]})" if code else "")


def nas_index_rows():
    import gzip
    with gzip.open(NAS_INDEX, "rt", encoding="utf-8") as f:
        for line in f:
            k, size, ts, path = line.rstrip("\n").split("\t", 3)
            yield k, int(size), int(ts), path


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


def nas_tree(root, depth, limit):
    """Folders under root down to depth, each with files, size, newest change and its commonest file types."""
    root = root.strip("/")
    pre = root + "/" if root else ""
    agg = {}
    for k, size, ts, path in nas_index_rows():
        if not path.startswith(pre) or k == "d":
            continue
        parts = path[len(pre):].split("/")
        ext = Path(parts[-1]).suffix.lower()[:6]
        for d in range(1, min(depth, len(parts) - 1) + 1):
            a = agg.setdefault("/".join(parts[:d]), [0, 0, 0, Counter()])
            a[0] += 1
            a[1] += size
            a[2] = max(a[2], ts)
            a[3][ext] += 1
        if len(parts) == 1:
            a = agg.setdefault(".", [0, 0, 0, Counter()])
            a[0] += 1
            a[1] += size
            a[2] = max(a[2], ts)
            a[3][ext] += 1
    rows = [(f"{pre}{k}/" if k != "." else f"{pre or '/'} (files here)", a[0], human_size(a[1]),
             dt.date.fromtimestamp(a[2]).isoformat() if a[2] else "", " ".join(e or "-" for e, _ in a[3].most_common(4)))
            for k, a in sorted(agg.items())]
    return md_table(rows[:limit], ["folder", "files", "size", "newest", "types"]) + \
        (f"\n_{len(rows) - limit} more folders; narrow the PATH or lower --depth_" if len(rows) > limit else "")


def sense_nas(args):
    c = CFG["nas"]
    head = f"## NAS ({c['share']})"
    pos = positional(args)
    sub = pos[0] if pos else "recent"
    if sub == "index":
        return f"{head}\n" + nas_index_build()
    if sub in ("tree", "find"):
        if not NAS_INDEX.exists():
            return f"{head}\n_no index yet: `mochi-sense nas index` (the mochi-nas-index timer runs it nightly)_"
        age = dt.date.fromtimestamp(NAS_INDEX.stat().st_mtime).isoformat()
        limit = int(opt(args, "--limit") or (150 if sub == "tree" else 60))
        if sub == "tree":
            root = " ".join(pos[1:])
            return f"{head}\n**{root or '/'}** (index of {age}; `nas ls` for live):\n" + \
                nas_tree(root, int(opt(args, "--depth") or 2), limit)
        rx = re.compile(" ".join(pos[1:]), re.I)
        hits = [(p + ("/" if k == "d" else ""), human_size(n) if k == "f" else "", dt.date.fromtimestamp(t).isoformat())
                for k, n, t, p in nas_index_rows() if rx.search(p)]
        hits.sort(key=lambda h: h[2], reverse=True)
        return f"{head}\n**find `{rx.pattern}`** (index of {age}): {len(hits)} hits, newest first\n" + \
            md_table(hits[:limit], ["path", "size", "modified"])
    if sub == "read":
        return f"{head}\n" + nas_read(" ".join(pos[1:]), int(opt(args, "--chars") or 20000))
    if sub == "ls":
        path = pos[1] if len(pos) > 1 else ""
        code, out, err = smb(f'ls "{path}/*"' if path else "ls")
        if code != 0:
            return f"{head}\n_error: {(err or out).strip()[:200]}_"
        rows = sorted(parse_smb_ls(out), key=lambda r: -r[3])
        return f"{head}\n**{path or '/'}** ({len(rows)} entries, newest first):\n" + \
            md_table([(n + ("/" if d else ""), f"{s / 1e6:.1f} MB" if not d else "", dt.date.fromtimestamp(t).isoformat()) for n, d, s, t in rows[:80]],
                     ["name", "size", "modified"])
    code, out, err = smb("ls")
    if code != 0:
        return f"{head}\n_unreachable: {(err or out).strip()[:200]}_"
    m = re.search(r"(\d+) blocks of size (\d+)\. (\d+) blocks available", out)
    space = ""
    if m:
        total, bs, avail = (int(x) for x in m.groups())
        space = f"{avail * bs / 1e12:.2f} TB free of {total * bs / 1e12:.2f} TB ({100 - avail * 100 // total}% used)"
    top = parse_smb_ls(out)
    days = days_arg(args, 14)
    since = time.time() - days * 86400
    out_lines = [head, f"Reachable. {space}", "**Top level**: " + ", ".join(n + ("/" if d else "") for n, d, _, _ in sorted(top, key=lambda r: -r[3])[:40])]
    changed = []
    for folder in c["watch"]:
        code, o, _ = smb(f'ls "{folder}/*"', timeout=30)
        if code != 0:
            continue
        for n, d, s, t in parse_smb_ls(o):
            if t > since:
                changed.append((t, f"{folder}/{n}" + ("/" if d else ""), s))
    changed.sort(reverse=True)
    out_lines.append(f"\n**Changed in the last {days}d under {', '.join(c['watch'])}** ({len(changed)}):")
    out_lines += [f"- {dt.date.fromtimestamp(t).isoformat()} {p}" + (f" ({s / 1e6:.0f} MB)" if s else "") for t, p, s in changed[:40]]
    return "\n".join(out_lines)


# ---- recorded calls (NAS Videos/Zoom: recordings with AssemblyAI transcripts) --------------------------

CALL_EXTS = {".dialog", ".summary", ".json", ".txt"}
MEDIA_EXTS = {".mkv", ".mp4", ".mp3", ".webm", ".m4a", ".wav"}


def calls_root():
    return (CFG["nas"].get("calls") or "Videos/Zoom").strip("/")


def call_index():
    """{"folder/stem": {"exts": set, "size": transcript bytes, "when": ts}} for every recording under the calls root."""
    root = calls_root()
    code, out, err = smb(f'cd "{root}"; recurse ON; ls', timeout=120)
    if code != 0:
        raise OSError((err or out).strip()[:200])
    calls, cur = {}, None
    for line in out.splitlines():
        if line.startswith("\\"):
            cur = line.strip().replace("\\", "/").strip("/")
            cur = cur[len(root):].strip("/") if cur.startswith(root) else cur
            continue
        for name, is_dir, size, ts in parse_smb_ls(line + "\n") if cur is not None else []:
            ext = Path(name).suffix.lower()
            if is_dir or ext not in CALL_EXTS | MEDIA_EXTS:
                continue
            stem = Path(name).stem.replace("_trimmed_audio", "")
            c = calls.setdefault(f"{cur}/{stem}".strip("/"), {"exts": set(), "size": 0, "when": ts, "files": {}})
            c["exts"].add(ext)
            c["files"][ext] = name
            if ext in (".txt", ".dialog"):
                c["size"] = max(c["size"], size)
    return calls


def call_text(key, c):
    """The call as who-said-what: the .dialog file if there is one, else the transcript JSON's utterances, else
    the flat .txt (no speakers). Speakers are A/B as the transcriber labelled them; which one is them is for the
    reader to work out (and note in study.md)."""
    folder, _, _ = key.rpartition("/")
    TMP.mkdir(parents=True, exist_ok=True)
    for ext in (".dialog", ".json", ".txt"):
        if ext not in c["files"]:
            continue
        local = TMP / ("call" + ext)
        try:
            code, out, err = smb(f'cd "{calls_root()}/{folder}"; get "{c["files"][ext]}" "{local}"', timeout=120)
            if code != 0 or not local.exists():
                continue
            if ext == ".json":
                u = json.loads(local.read_text(errors="replace")).get("utterances") or []
                if not u:
                    continue
                return "\n".join(f"[{x.get('start', 0) // 60000}:{x.get('start', 0) // 1000 % 60:02d}] "
                                 f"{x.get('speaker', '?')}: {x.get('text', '')}" for x in u), "speakers from the transcript"
            return local.read_text(errors="replace"), ("speakers" if ext == ".dialog" else "no speaker labels")
        finally:
            local.unlink(missing_ok=True)
    return "", "no transcript"


def tidy_transcript(text):
    """Transcriber noise out: a character stuck on repeat, the same short phrase over and over."""
    text = re.sub(r"(\S)\1{5,}", r"\1\1\1…", text)
    return re.sub(r"((?:[^.!?\n]{1,40}[.!?]\s*))(?:\1){2,}", r"\1(×) ", text)


def sense_calls(args):
    head = f"## Recorded calls (NAS {calls_root()})"
    pos = positional(args)
    sub = pos[0] if pos else "list"
    try:
        calls = call_index()
    except OSError as e:
        return f"{head}\n_unreachable: {e}_"
    if sub in ("read", "summary"):
        key = " ".join(pos[1:]).strip().strip("/")
        for e in CALL_EXTS | MEDIA_EXTS:
            key = key[:-len(e)] if key.lower().endswith(e) else key
        c = calls.get(key)
        if not c:
            near = [k for k in calls if key.lower() in k.lower()][:10]
            return f"{head}\n_no call `{key}`_" + (". Did you mean: " + "; ".join(near) if near else "")
        if sub == "summary":
            if ".summary" not in c["exts"]:
                return f"{head}\n_no summary for `{key}`; read it instead_"
            local = TMP / "call.summary"
            folder = key.rpartition("/")[0]
            smb(f'cd "{calls_root()}/{folder}"; get "{c["files"][".summary"]}" "{local}"')
            try:
                return f"{head}\n**{key}** (summary):\n\n" + local.read_text(errors="replace")
            finally:
                local.unlink(missing_ok=True)
        text, how = call_text(key, c)
        text = tidy_transcript(text)
        if not text:
            return f"{head}\n_`{key}` has no transcript (recording only)_"
        size = int(opt(args, "--chars") or 40000)
        parts = max(1, -(-len(text) // size))
        part = min(max(1, int(opt(args, "--part") or 1)), parts)
        return (f"{head}\n**{key}** ({how}; {len(text)} chars; part {part} of {parts}"
                f"{f', next: --part {part + 1}' if part < parts else ''}):\n\n" + text[(part - 1) * size:part * size])
    # list: every folder, its calls with dates and what each has
    folder = " ".join(pos[1:]).strip("/") if sub == "list" and len(pos) > 1 else ""
    rows, by = [], Counter()
    for k, c in sorted(calls.items()):
        f = k.rpartition("/")[0] or "/"
        has = c["exts"] & CALL_EXTS
        by[(f, "transcribed" if has else "recording only")] += 1
        if folder and not k.startswith(folder + "/"):
            continue
        if folder:
            rows.append((k.rpartition("/")[2], f"{c['size'] / 1000:.0f}k" if c["size"] else "",
                         ", ".join(sorted(e[1:] for e in has)) or "recording only"))
    if folder:
        return f"{head}\n**{folder}** ({len(rows)} calls):\n" + md_table(rows, ["call", "transcript", "has"])
    folders = sorted({f for f, _ in by})
    out = [head, f"{len(calls)} recordings, {sum(1 for c in calls.values() if c['exts'] & CALL_EXTS)} with transcripts. "
                 "`calls list FOLDER` for its calls, `calls read FOLDER/CALL [--part N]`, `calls summary FOLDER/CALL`."]
    out.append(md_table([(f, by[(f, "transcribed")], by[(f, "recording only")]) for f in folders],
                        ["folder", "transcribed", "recording only"]))
    return "\n".join(out)


# ---- YouTube (their channel: what they publish, in their own voice) ------------------------------------

YT_CACHE = Path.home() / ".cache/mochi-sense/youtube"  # public captions, cached: YouTube rate-limits subtitle fetches


def ytdlp():
    p = Path(os.path.expanduser(CFG["youtube"].get("ytdlp") or ""))
    return str(p) if p.is_file() else (shutil.which("yt-dlp") or "yt-dlp")


def vtt_text(vtt):
    """Auto-captions roll: each line appears two or three times. Keep each spoken line once, with a minute mark."""
    out, last, minute = [], "", -1
    for block in vtt.split("\n\n"):
        lines = block.strip().splitlines()
        if not lines or "-->" not in lines[0]:
            continue
        m = re.match(r"(\d+):(\d+):(\d+)", lines[0])
        for line in lines[1:]:
            line = re.sub(r"<[^>]+>", "", line).strip()
            if not line or line == last:
                continue
            last = line
            mins = int(m.group(1)) * 60 + int(m.group(2)) if m else minute
            if mins != minute and mins % 5 == 0:
                out.append(f"\n[{mins} min]")
                minute = mins
            out.append(line)
    return re.sub(r"(?<!\])\n(?!\[)", " ", "\n".join(out)).strip()


def yt_list():
    """[(id, duration s, title, tab)] for the channel's videos and shorts; cached for a day."""
    cache = YT_CACHE / "list.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 86400:
        return json.loads(cache.read_text())
    ch = CFG["youtube"]["channel"]
    base = f"https://www.youtube.com/channel/{ch}" if ch.startswith("UC") else f"https://www.youtube.com/{ch}"
    rows = []
    for tab in ("videos", "shorts", "streams"):
        code, out, err = run([ytdlp(), "--flat-playlist", "--print", "%(id)s|%(duration)s|%(title)s", f"{base}/{tab}"],
                             timeout=240)
        for line in out.splitlines():
            vid, dur, title = (line.split("|", 2) + ["", ""])[:3]
            if re.fullmatch(r"[\w-]{11}", vid):
                rows.append([vid, int(float(dur)) if dur not in ("NA", "None", "") else 0, title, tab])
    if rows:
        YT_CACHE.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(rows, ensure_ascii=False))
    return rows


def yt_read(vid):
    """(header, text) for one video: title, date, description and the captions in its own language."""
    cache = YT_CACHE / f"{vid}.json"
    if cache.exists():
        d = json.loads(cache.read_text())
        return d["head"], d["text"]
    YT_CACHE.mkdir(parents=True, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={vid}"
    code, out, err = run([ytdlp(), "--skip-download", "--no-simulate", "--print",
                          "%(title)s\u241f%(upload_date)s\u241f%(duration)s\u241f%(description)s", url], timeout=120)
    title, date, dur, desc = (out.split("\u241f", 3) + ["", "", "", ""])[:4]
    head = f"**{title.strip()}** ({date[:4]}-{date[4:6]}-{date[6:8]}, {int(float(dur or 0)) // 60} min, {url})" + \
           (f"\n_Description:_ {desc.strip()[:1500]}" if desc.strip() else "")
    text = ""
    for lang in ("ru-orig", "en-orig", "ru", "en", "nl-orig"):  # one at a time: each fetch counts against a rate limit
        stem = TMP / f"yt-{vid}"
        run([ytdlp(), "--skip-download", "--write-auto-subs", "--write-subs", "--sub-langs", lang, "--sub-format", "vtt",
             "-o", str(stem), url], timeout=120)
        got = sorted(TMP.glob(f"yt-{vid}*.vtt"))
        if got:
            text = vtt_text(got[0].read_text(errors="replace"))
            for g in got:
                g.unlink()
            break
    if text:
        cache.write_text(json.dumps({"head": head, "text": text}, ensure_ascii=False))
    return head, text


def sense_youtube(args):
    head = "## YouTube channel"
    if not CFG["youtube"].get("channel"):
        return f"{head}\n_not set up: youtube.channel in {CONFIG_FILE}_"
    pos = positional(args)
    sub = pos[0] if pos else "list"
    if sub == "read" and len(pos) > 1:
        vid = re.sub(r".*(?:v=|youtu\.be/|shorts/)", "", pos[1])[:11]
        h, text = yt_read(vid)
        if not text:
            return f"{head}\n{h}\n\n_no captions (or rate-limited; try later)_"
        size = int(opt(args, "--chars") or 40000)
        parts = max(1, -(-len(text) // size))
        part = min(max(1, int(opt(args, "--part") or 1)), parts)
        return (f"{head}\n{h}\n\n_Auto-captions, {len(text)} chars, part {part} of {parts}"
                f"{f', next: --part {part + 1}' if part < parts else ''}:_\n\n" + text[(part - 1) * size:part * size])
    rows = yt_list()
    if not rows:
        return f"{head}\n_couldn't list the channel (yt-dlp: {ytdlp()}; `make tools` installs a current one)_"
    have = {p.stem for p in YT_CACHE.glob("*.json")}
    return (f"{head} ({CFG['youtube']['channel']}): {len(rows)} uploads, newest first. "
            "`youtube read ID [--part N]` for one with its captions (✓ = already fetched).\n" +
            md_table([(v, f"{d // 60} min" if d else "", t[:70], tab, "✓" if v in have else "")
                      for v, d, t, tab in rows], ["id", "length", "title", "tab", ""]))


# ---- Immich (photos) ----------------------------------------------------------------------------------

def sense_photos(args):
    c = CFG["immich"]
    head = f"## Photos (Immich, {c['url']})"
    hdr = {}
    if c.get("api_key"):
        hdr["x-api-key"] = c["api_key"]
    else:
        v = firefox_cookie(host_of(c["url"]), c["cookie_name"])
        if v:
            hdr["Cookie"] = f"{c['cookie_name']}={v}"
    if not hdr:
        return f"{head}\n_no api_key configured and no browser session_"
    try:
        s = http_json(c["url"] + "/api/server/statistics", headers=hdr, timeout=10)
        out = [head, f"{s.get('photos')} photos, {s.get('videos')} videos, {s.get('usage', 0) / 1e9:.1f} GB"]
        try:
            body = json.dumps({"size": 12, "order": "desc", "withExif": False}).encode()
            d = http_json(c["url"] + "/api/search/metadata", headers=dict(hdr, **{"Content-Type": "application/json"}), data=body, timeout=15)
            items = (d.get("assets") or {}).get("items") or []
            out.append("**Newest uploads**: " + ", ".join(f"{(a.get('fileCreatedAt') or '')[:10]} {a.get('originalFileName', '')[:30]}" for a in items[:12]))
        except (urllib.error.URLError, OSError, ValueError):
            pass
        return "\n".join(out)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return f"{head}\n_error: {e}_"


# ---- network: what is around --------------------------------------------------------------------------

def sense_network(args):
    out = ["## Network"]
    rows = []
    for name, host in CFG["hosts"].items():
        code, o, _ = run(["ping", "-c", "1", "-W", "1", host], timeout=4)
        ip = re.search(r"\((\d+\.\d+\.\d+\.\d+)\)", o)
        rows.append((name, host, "up" if code == 0 else "down", ip.group(1) if ip else ""))
    out.append(md_table(rows, ["name", "host", "state", "ip"]))
    svc = []
    for name, url in CFG["web_services"].items():
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=4):
                svc.append(f"{name} ({url}) up")
        except urllib.error.HTTPError:
            svc.append(f"{name} ({url}) up")  # any HTTP answer means the service is there
        except (urllib.error.URLError, OSError):
            svc.append(f"{name} ({url}) DOWN")
    out.append("**Web services**: " + "; ".join(svc))
    code, o, _ = run(["tailscale", "status"], timeout=6)
    if code == 0:
        out.append("**Tailscale**:\n" + "\n".join("- " + re.sub(r"\s+", " ", l.strip()) for l in o.splitlines()[:12] if l.strip()))
    code, o, _ = run(["kdeconnect-cli", "-l"], timeout=6)
    if code == 0 and o.strip():
        out.append("**KDE Connect**: " + "; ".join(l.strip("- ").strip() for l in o.splitlines() if l.startswith("-")))
    code, o, _ = run(["avahi-browse", "-atrp"], timeout=8)
    if code in (0, -1) and o:
        seen = {}
        for l in o.splitlines():
            p = l.split(";")
            if len(p) > 8 and p[0] == "=" and p[2] == "IPv4":
                seen.setdefault(p[6], set()).add(p[4].replace("_", "").replace(".tcp", "").replace(".udp", ""))
        out.append("**mDNS**: " + "; ".join(f"{h} [{', '.join(sorted(s))}]" for h, s in sorted(seen.items())[:20]))
    return "\n".join(out)


# ---- Claude Code sessions -----------------------------------------------------------------------------

def sense_sessions(args):
    days = days_arg(args, 7)
    root = Path(CFG["claude_projects"])
    since = time.time() - days * 86400
    per_project = defaultdict(lambda: {"n": 0, "last": 0, "prompts": []})
    for f in root.glob("*/*.jsonl"):
        try:
            mt = f.stat().st_mtime
        except OSError:
            continue
        if mt < since:
            continue
        proj = f.parent.name
        if "local-share-mochi" in proj or "scratchpad" in proj:
            continue
        cwd, prompt = "", ""
        try:
            with open(f, errors="replace") as fh:
                for line in fh:
                    if not cwd:
                        m = re.search(r'"cwd":\s*"([^"]+)"', line)
                        cwd = m.group(1) if m else ""
                    if '"type":"user"' not in line and '"type": "user"' not in line:
                        continue
                    d = json.loads(line)
                    c = d.get("message", {}).get("content")
                    if isinstance(c, list):
                        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
                    if isinstance(c, str) and c.strip() and not c.lstrip().startswith("<") and len(c) > 12:
                        prompt = re.sub(r"\s+", " ", c.strip())[:120]
                        break
        except (OSError, ValueError):
            pass
        if cwd.startswith(("/tmp", str(WORK))):
            continue
        rec = per_project[cwd or proj]
        rec["n"] += 1
        rec["last"] = max(rec["last"], mt)
        if prompt:
            rec["prompts"].append((mt, prompt))
    out = [f"## Claude Code sessions (last {days}d, from transcripts; Mochi's own excluded)"]
    for proj, r in sorted(per_project.items(), key=lambda kv: -kv[1]["last"]):
        path = proj.replace(str(HOME), "~")
        out.append(f"- **{path}**: {r['n']} sessions, last {ago(r['last'])}")
        for _, p in sorted(r["prompts"], reverse=True)[:3]:
            out.append(f"    - “{p}”")
    return "\n".join(out)


# ---- git repos ---------------------------------------------------------------------------------------

def sense_repos(args):
    days = days_arg(args, 30)
    rows = []
    for root in CFG["repo_roots"]:
        code, o, _ = run(["find", root, "-maxdepth", str(CFG["repo_depth"]), "-name", ".git", "-not", "-path", "*/node_modules/*"], timeout=30)
        for g in o.splitlines():
            repo = str(Path(g).parent)
            c2, last, _ = run(["git", "-C", repo, "log", "-1", "--format=%ct|%an|%s"], timeout=10)
            if c2 != 0 or not last.strip():
                continue
            ts, author, subj = last.strip().split("|", 2)
            ts = int(ts)
            if ts < time.time() - days * 86400:
                continue
            _, st, _ = run(["git", "-C", repo, "status", "--porcelain", "-b"], timeout=10)
            lines = st.splitlines()
            branch = lines[0][3:] if lines else ""
            dirty = len(lines) - 1
            rows.append((ts, repo.replace(str(HOME), "~"), branch[:40], dirty, author[:14], subj[:50]))
    rows.sort(reverse=True)
    out = [f"## Git repos touched in the last {days}d ({len(rows)})",
           md_table([(dt.date.fromtimestamp(t).isoformat(), r, b, d or "", a, s) for t, r, b, d, a, s in rows],
                    ["last commit", "repo", "branch [ahead/behind]", "dirty", "author", "subject"])]
    return "\n".join(out)


# ---- mail (local Maildir via mbsync + notmuch) ------------------------------------------------------

def notmuch(*argv, timeout=60):
    """Run notmuch read-only (search/show/count/address only) and return (ok, stdout)."""
    if argv and argv[0] not in ("search", "show", "count", "address", "config"):
        raise ValueError("mochi-sense only reads mail")
    code, out, err = run(["notmuch", *argv], timeout=timeout)
    return code == 0, out if code == 0 else err.strip()


def mail_status():
    """(ready, why). Ready means notmuch exists and the database has been built at least once."""
    md = Path(CFG["mail"]["maildir"])
    if not shutil.which("notmuch") or not shutil.which("mbsync"):
        return False, "isync/notmuch not installed (`sudo apt install isync notmuch`)"
    if not (md / ".notmuch" / "xapian").exists():
        return False, f"no index yet under {md} (passwords in {CFG['mail']['password_dir']}? `systemctl --user start mochi-mail`)"
    return True, ""


def mail_sync_age():
    try:
        return ago(float((Path(CFG["mail"]["maildir"]) / ".last-sync").read_text().strip()))
    except (OSError, ValueError):
        return "never"


def nm_search(query, limit=25, output="summary"):
    ok, out = notmuch("search", "--format=json", f"--output={output}", "--sort=newest-first", f"--limit={int(limit)}", query)
    if not ok:
        return []
    try:
        return json.loads(out or "[]")
    except ValueError:
        return []


def nm_count(query):
    ok, out = notmuch("count", query)
    return int(out.strip()) if ok and out.strip().isdigit() else 0


def mail_acct(tags):
    return next((a for a in CFG["mail"]["accounts"] if a in tags), "?")


def thread_rows(threads):
    rows = []
    for t in threads:
        tags = t.get("tags", [])
        flags = ("U" if "unread" in tags else "") + ("A" if "attachment" in tags else "") + ("S" if "sent" in tags else "")
        rows.append((t.get("date_relative", ""), mail_acct(tags), (t.get("authors") or "")[:28], (t.get("subject") or "(no subject)")[:60],
                     f"{t.get('matched')}/{t.get('total')}", flags, t.get("thread", "")))
    return rows


MAIL_HEAD = ["when", "acct", "from", "subject", "msgs", "", "thread"]


def _walk_parts(node):
    if isinstance(node, dict):
        yield node
        for c in node.get("content") if isinstance(node.get("content"), list) else []:
            yield from _walk_parts(c)
    elif isinstance(node, list):
        for n in node:
            yield from _walk_parts(n)


def mail_body(part, want_html=False):
    """Walk a notmuch show JSON part tree; return text/plain, or de-tagged html if that's all there is."""
    if isinstance(part, list):
        return "\n".join(filter(None, (mail_body(p, want_html) for p in part)))
    if not isinstance(part, dict):
        return ""
    ct = (part.get("content-type") or "").lower()
    content = part.get("content")
    if ct == "text/plain" and isinstance(content, str):
        return content
    if ct == "text/html" and isinstance(content, str) and want_html:
        import html as _html
        txt = re.sub(r"(?is)<(script|style).*?</\1>", "", content)
        txt = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h[1-6]>", "\n", txt)
        txt = re.sub(r"<[^>]+>", "", txt)
        return re.sub(r"\n{3,}", "\n\n", _html.unescape(txt)).strip()
    if isinstance(content, list):
        if ct == "multipart/alternative":
            return "\n".join(filter(None, (mail_body(p, False) for p in content))) or mail_body(content, True)
        return "\n".join(filter(None, (mail_body(p, want_html) for p in content)))
    return ""


def mail_show(query, chars):
    ok, out = notmuch("show", "--format=json", "--include-html", "--entire-thread=false", query)
    if not ok:
        return f"_notmuch show failed: {out[:200]}_"
    try:
        data = json.loads(out)
    except ValueError:
        return "_unparseable notmuch output_"

    def msgs(node):  # notmuch show nests [thread][message, replies]; flatten to message dicts
        if isinstance(node, dict) and "headers" in node:
            yield node
        elif isinstance(node, list):
            for n in node:
                yield from msgs(n)
    parts = []
    for m in msgs(data):
        h = m.get("headers", {})
        body = mail_body(m.get("body", []), False) or mail_body(m.get("body", []), True)
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        if len(body) > chars:
            body = body[:chars] + f"\n… [truncated at {chars} chars; --chars N for more]"
        atts = [p.get("filename") for p in _walk_parts(m.get("body", [])) if p.get("filename")]
        parts.append("\n".join([f"### {h.get('Subject', '(no subject)')}",
                                f"**From:** {h.get('From', '')}  \n**To:** {h.get('To', '')}  \n**Date:** {h.get('Date', '')}  \n"
                                f"**id:** `{m.get('id', '')}`  **tags:** {', '.join(m.get('tags', []))}"
                                + (f"  \n**attachments:** {', '.join(atts)}" if atts else ""),
                                "", body or "_(empty body)_"]))
    return "\n\n".join(parts) if parts else "_no message matches_"


def sense_mail(args):
    """mail: recent | unread | search QUERY | show QUERY | folders  [--days N] [--limit N] [--chars N]
    QUERY is notmuch syntax: from:, to:, subject:, tag:unread, tag:ACCOUNT, folder:, date:2w.., attachment:, plain words."""
    head = "## Mail (local Maildir, mbsync + notmuch)"
    ready, why = mail_status()
    if not ready:
        return f"{head}\n_not available: {why}_"
    pos = positional(args)
    sub = pos[0] if pos else "recent"
    limit = int(opt(args, "--limit") or 25)
    accts = CFG["mail"]["accounts"]
    if sub == "search":
        q = " ".join(pos[1:])
        rows = thread_rows(nm_search(q, limit))
        return f"{head}\n**Search `{q}`**: {nm_count(q)} messages; newest {len(rows)} threads (U unread, A attachment, S sent)\n" + md_table(rows, MAIL_HEAD)
    if sub == "show":
        return f"{head}\n" + mail_show(" ".join(pos[1:]), int(opt(args, "--chars") or 6000))
    if sub == "folders":
        rows = []
        root = Path(CFG["mail"]["maildir"])
        for a in accts:
            for d in sorted(p for p in (root / a).rglob("cur") if p.is_dir()):
                folder = str(d.parent.relative_to(root))
                rows.append((folder, nm_count(f'folder:"{folder}"'), nm_count(f'folder:"{folder}" and tag:unread')))
        return f"{head}\n" + md_table(rows, ["folder", "msgs", "unread"])
    days = days_arg(args, 2)
    out = [head, f"Last sync: {mail_sync_age()}. " + "; ".join(
        f"**{a}** ({addr}): {nm_count(f'tag:{a} and tag:inbox')} in inbox, {nm_count(f'tag:{a} and tag:inbox and tag:unread')} unread, "
        f"{nm_count(f'tag:{a} and tag:unread and date:{days}d..')} unread in {days}d anywhere, {nm_count(f'tag:{a} and tag:draft')} drafts"
        for a, addr in accts.items())]
    if sub == "unread":
        out.append(f"**Unread, last {days}d, all folders** (newest {limit}):")
        out.append(md_table(thread_rows(nm_search(f"tag:unread and date:{days}d..", limit)), MAIL_HEAD))
        return "\n".join(out)
    # recent: inbox threads, then unread that skipped the inbox (filters/labels)
    out.append(f"**Inbox** (newest {limit}; U unread, A attachment, S sent):")
    out.append(md_table(thread_rows(nm_search("tag:inbox", limit)), MAIL_HEAD))
    skipped = nm_search(f"tag:unread and not tag:inbox and not tag:sent and date:{days}d..", limit)
    if skipped:
        out.append(f"**Unread outside the inbox, last {days}d** (filtered/labelled; newest {len(skipped)}):")
        out.append(md_table(thread_rows(skipped), MAIL_HEAD))
    out.append("_Dig: `mochi-sense mail search 'from:belastingdienst date:30d..'` · `mochi-sense mail show thread:ID` · "
               "`mochi-sense mail show id:MSGID` · `mochi-sense mail unread --days 7`_")
    return "\n".join(out)


# ---- Home Assistant -----------------------------------------------------------------------------------

def ha_token():
    c = CFG["home_assistant"]
    return c.get("token") or (Path(c["token_file"]).read_text().strip() if c.get("token_file") and Path(c["token_file"]).exists() else "")


def sense_home(args):
    """What the house is doing, from Home Assistant's REST API (read-only: GET /api/states)."""
    c = CFG["home_assistant"]
    head = f"## Home (Home Assistant, {c['url']})"
    tok = ha_token()
    if not tok:
        return f"{head}\n_no token: create a long-lived access token in HA (profile → security) and put it in " \
               f"home_assistant.token (or token_file) in {CONFIG_FILE}_"
    try:
        states = http_json(c["url"].rstrip("/") + "/api/states", headers={"Authorization": f"Bearer {tok}"}, timeout=10)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return f"{head}\n_error: {e}_"
    pos = positional(args)
    pat = re.compile(opt(args, "--grep") or (pos[0] if pos else ""), re.I) if (opt(args, "--grep") or pos) else None
    now_ts = time.time()
    rows, people, on, vac, climate, binary, stale, recent = [], [], [], [], [], [], [], []
    for st in states:
        eid, state, attrs = st.get("entity_id", ""), st.get("state", ""), st.get("attributes") or {}
        name = attrs.get("friendly_name") or eid
        dom = eid.split(".")[0]
        try:
            changed = dt.datetime.fromisoformat(st.get("last_changed", "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            changed = 0
        if pat and not (pat.search(eid) or pat.search(name)):
            continue
        if pat:
            rows.append((eid, state + (" " + str(attrs.get("unit_of_measurement")) if attrs.get("unit_of_measurement") else ""), name[:40], ago(changed)))
            continue
        if dom in ("person", "device_tracker"):
            people.append(f"{name}: {state} ({ago(changed)})")
        elif dom in ("light", "switch", "fan", "media_player") and state in ("on", "playing"):
            on.append(f"{name}" + (f" [{attrs.get('media_title')}]" if attrs.get("media_title") else ""))
        elif dom == "vacuum":
            vac.append(f"{name}: {state}" + (f", battery {attrs.get('battery_level')}%" if attrs.get("battery_level") is not None else ""))
        elif dom == "climate" or (dom == "sensor" and attrs.get("device_class") in ("temperature", "humidity")):
            climate.append(f"{name} {state}{attrs.get('unit_of_measurement', '')}")
        elif dom == "binary_sensor" and state == "on":
            binary.append(f"{name} ({attrs.get('device_class', 'on')}, {ago(changed)})")
        if state in ("unavailable", "unknown") and dom not in ("sensor", "button", "update", "event"):
            stale.append(name)
        if changed and now_ts - changed < 1800 and dom not in ("sensor", "sun", "update"):
            recent.append(f"{ago(changed)} {name} → {state}")
    if pat:
        return f"{head}\n**Matching `{pat.pattern}`** ({len(rows)}):\n" + md_table(rows[:60], ["entity", "state", "name", "changed"])
    out = [head, f"{len(states)} entities."]
    if people: out.append("**People**: " + "; ".join(people))
    if on: out.append("**On**: " + ", ".join(on[:25]))
    if vac: out.append("**Vacuums**: " + "; ".join(vac))
    if climate: out.append("**Climate**: " + ", ".join(climate[:12]))
    if binary: out.append("**Active sensors**: " + ", ".join(binary[:15]))
    if recent: out.append("**Changed in the last 30 min**: " + "; ".join(sorted(recent)[:15]))
    if stale: out.append("**Unavailable**: " + ", ".join(stale[:15]))
    out.append("_Dig: `mochi-sense home vacuum`, `mochi-sense home --grep temperature`_")
    return "\n".join(out)


# ---- hosts over ssh ------------------------------------------------------------------------------------

SSH_REPORT = ("echo HOST $(hostname); uptime; echo DISK; df -h -x tmpfs -x devtmpfs -x overlay 2>/dev/null | tail -n +2; "
              "echo MEM; free -h 2>/dev/null | sed -n 2p; echo FAILED; systemctl --failed --no-legend 2>/dev/null | head -5; "
              "echo DOCKER; docker ps --format '{{.Names}}\t{{.Status}}' 2>/dev/null | head -30; "
              "echo UPDATES; ls /var/run/reboot-required 2>/dev/null; echo END")


def ssh_run(target, cmd, timeout=25):
    key = CFG["ssh"]["key"]
    return run(["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "IdentitiesOnly=yes", target, cmd], timeout=timeout)


def sense_hosts(args):
    """Each ssh host: uptime, disk, memory, failed units, containers. Needs Mochi's key in authorized_keys there."""
    pos = positional(args)
    only = set(pos)
    key = CFG["ssh"]["key"]
    out = [f"## Hosts (ssh, key {key})"]
    if not Path(key).exists():
        return out[0] + "\n_no key: `ssh-keygen -t ed25519 -N '' -f " + key + "`_"
    for name, target in CFG["ssh"]["hosts"].items():
        if only and name not in only:
            continue
        if pos and len(pos) > 1 and pos[0] == name:  # mochi-sense hosts NAME <command...>: a read-only command of choice
            code, o, err = ssh_run(target, " ".join(pos[1:]), timeout=40)
            out.append(f"**{name}** (`{' '.join(pos[1:])}`):\n```\n{(o or err).strip()[:4000]}\n```")
            return "\n".join(out)
        code, o, err = ssh_run(target, SSH_REPORT)
        if code != 0:
            why = err.strip().splitlines()[-1][:120] if err.strip() else f"exit {code}"
            out.append(f"- **{name}** ({target}): NO ACCESS: {why}. Install the key: `ssh-copy-id -i {key}.pub {target}`")
            continue
        sec = {}
        cur = None
        for line in o.splitlines():
            if line in ("DISK", "MEM", "FAILED", "DOCKER", "UPDATES", "END"):
                cur = line
                continue
            if line.startswith("HOST "):
                sec["host"] = line[5:]
                continue
            if cur is None:
                sec["uptime"] = re.sub(r"\s+", " ", line.strip())
            else:
                sec.setdefault(cur, []).append(line.rstrip())
        disks = []
        for d in sec.get("DISK", []):
            parts = d.split()
            if len(parts) >= 6 and parts[4].endswith("%"):
                pct = int(parts[4][:-1])
                disks.append(f"{parts[5]} {parts[4]} ({parts[3]} free)" + (" ⚠" if pct >= 90 else ""))
        line = f"- **{name}** ({sec.get('host', target)}): {sec.get('uptime', '')}"
        if disks: line += f"\n    - disk: " + ", ".join(disks[:6])
        if sec.get("MEM"): line += f"\n    - mem: {re.sub(r'\s+', ' ', sec['MEM'][0])}"
        if sec.get("FAILED"): line += f"\n    - failed units: " + "; ".join(x.split()[0] for x in sec["FAILED"] if x.strip())
        if sec.get("DOCKER"): line += f"\n    - containers: " + "; ".join(x.replace("\t", " ") for x in sec["DOCKER"][:20])
        if sec.get("UPDATES"): line += "\n    - reboot required"
        out.append(line)
    return "\n".join(out)


# ---- the registry ------------------------------------------------------------------------------------

def sense_sources(args):
    out = ["## Sources (what mochi-sense can look at)",
           f"Config: {CONFIG_FILE} ({'exists' if CONFIG_FILE.exists() else 'defaults'})"]
    rows = []
    for name, pattern in CFG["browsers"].items():
        p = first_glob(pattern)
        rows.append((f"browser: {name}", p or pattern, "ok, last write " + ago(Path(p).stat().st_mtime) if p else "missing"))
    rows.append(("shell history", ", ".join(CFG["shell_history"]), f"{sum(len(glob.glob(p)) for p in CFG['shell_history'])} files"))
    tg = TgArchive()
    rows.append(("telegram archive", CFG["tg_archive"]["url"], "ok" + (f" ({tg.why})" if tg.why else "") if tg.cookie else f"NO ACCESS: {tg.why}"))
    llm = LlmArchive()
    rows.append(("llm chat archive", CFG["llm_archive"]["url"], "live" if llm.cookie else (llm.why or "?")))
    code, o, err = smb("ls", timeout=20)
    rows.append(("nas", CFG["nas"]["share"], "ok" if code == 0 else f"NO ACCESS: {(err or o).strip()[:80]}"))
    rows.append(("photos (immich)", CFG["immich"]["url"], "api key" if CFG["immich"].get("api_key") else ("browser session" if firefox_cookie(host_of(CFG["immich"]["url"]), CFG["immich"]["cookie_name"]) else "no access")))
    rows.append(("home assistant", CFG["home_assistant"]["url"], "token set" if ha_token() else "no token (home_assistant.token in sources.json)"))
    for name, target in CFG["ssh"]["hosts"].items():
        code, _, err = ssh_run(target, "true", timeout=10)
        rows.append((f"ssh {name}", target, "ok" if code == 0 else "NO ACCESS (install Mochi's key: ssh-copy-id -i ~/.ssh/mochi_ed25519.pub " + target + ")"))
    rows.append(("claude code transcripts", CFG["claude_projects"], f"{len(list(Path(CFG['claude_projects']).glob('*/*.jsonl')))} files"))
    rows.append(("git repos", f"{CFG['repo_roots']} depth {CFG['repo_depth']}", "ok"))
    ready, why = mail_status()
    rows.append(("mail (mbsync+notmuch)", CFG["mail"]["maildir"], f"ok, synced {mail_sync_age()}" if ready else f"NO ACCESS: {why}"))
    rows.append(("gmail drafts / drive / calendar", "claude.ai connectors", "see tools available in the run"))
    out.append(md_table(rows, ["source", "where", "state"]))
    return "\n".join(out)


# ---- all -----------------------------------------------------------------------------------------------

SENSORS = {
    "browser": (sense_browser, "browser history: sites, searches, pages  [--days N] [--grep REGEX]"),
    "shell": (sense_shell, "shell history: commands, hosts, dirs  [--lines N]"),
    "telegram": (sense_telegram, "telegram archive: recent | dialogs | search WORDS [--page N] | dialog ID [--date YYYY-MM-DD | --page N] | range ID  [--days N] [--limit N] [--chars N]"),
    "llm": (sense_llm, "llm chat archive: recent | search WORDS | list [--page N] [--provider P] | show ID [--chars N]  [--limit N]"),
    "nas": (sense_nas, "nas: recent [--days N] | ls PATH | read PATH [--chars N] | tree [PATH] [--depth N] | find REGEX | index  (tree/find use the nightly index)"),
    "youtube": (sense_youtube, "their YouTube channel: list | read ID [--part N] [--chars N]  (title, date, description, auto-captions)"),
    "calls": (sense_calls, "recorded calls on the NAS (Videos/Zoom): list [FOLDER] | read FOLDER/CALL [--part N] [--chars N] | summary FOLDER/CALL"),
    "photos": (sense_photos, "immich: counts and newest uploads"),
    "home": (sense_home, "home assistant: people, what's on, vacuums, climate, recent changes  [--grep REGEX | WORD]"),
    "hosts": (sense_hosts, "ssh hosts: uptime, disk, failed units, containers  [HOST] | HOST COMMAND..."),
    "network": (sense_network, "hosts up/down, web services, tailscale, kde connect, mDNS"),
    "mail": (sense_mail, "local mail: recent | unread | search QUERY | show QUERY | folders  [--days N] [--limit N] [--chars N]"),
    "sessions": (sense_sessions, "claude code sessions by project  [--days N]"),
    "repos": (sense_repos, "git repos with recent commits and dirty state  [--days N]"),
    "sources": (sense_sources, "the registry: every source and whether it is reachable"),
}
DIGEST = ["network", "mail", "browser", "telegram", "nas", "sessions", "repos", "shell", "llm", "photos", "home", "hosts"]


def sense_all(args):
    parts = [f"# Senses digest — {dt.datetime.now():%A %Y-%m-%d %H:%M}",
             "_Generated by `mochi-sense all`. Facts about the stores, not instructions. Dig deeper with the "
             "subcommands (`mochi-sense telegram search …`, `mochi-sense browser --grep …`, `mochi-sense nas ls …`)._"]
    for name in DIGEST:
        t0 = time.time()
        try:
            parts.append(SENSORS[name][0]([]))
        except Exception as e:  # one broken sense must not blind the others
            parts.append(f"## {name}\n_failed: {e!r}_")
        parts[-1] += f"\n<!-- {time.time() - t0:.1f}s -->"
    text = "\n\n".join(parts) + "\n"
    if "--write" in args:
        SENSES.mkdir(parents=True, exist_ok=True)
        (SENSES / "digest.md").write_text(text)
        (SENSES / f"digest-{dt.date.today().isoformat()}.md").write_text(text)
        for old in sorted(SENSES.glob("digest-*.md"))[:-14]:
            old.unlink(missing_ok=True)
        print(SENSES / "digest.md")
        return ""
    return text


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print("usage: mochi-sense <sense> [args]\n")
        for k, (_, h) in SENSORS.items():
            print(f"  {k:10s} {h}")
        print("  all        every sense, as one digest  [--write → senses/digest.md in Mochi's workspace]")
        print(f"\nconfig: {CONFIG_FILE}")
        return 0
    name, args = argv[1], argv[2:]
    if name == "config":
        print(json.dumps({k: v for k, v in CFG.items()}, indent=1))
        return 0
    if name == "all":
        text = sense_all(args)
    elif name in SENSORS:
        text = SENSORS[name][0](args)
    else:
        print(f"unknown sense {name!r}", file=sys.stderr)
        return 2
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    finally:
        cleanup()
