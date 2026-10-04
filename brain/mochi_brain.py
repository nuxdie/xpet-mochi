#!/usr/bin/env python3
"""mochi-brain — a thin relay between Mochi the pig and Claude Code.

There is no judgement in here. Claude Code *is* the brain. Every ROUND_EVERY while you're at the
computer (and when you ask, when you click an offer, when you come back from a long break) this starts
`claude -p` inside Mochi's workspace, ~/.local/share/mochi. CLAUDE.md there is Mochi's standing brief;
memory/, journal/ and reports/ are its own notes, which it keeps itself. A round ends with a small JSON
block: offers for you (yes / not now / never), a report to put behind the dot, and -- rarely -- something
urgent to say out loud.

What this file does, on purpose, and nothing more:
  - decides WHEN to run (presence, cadence, daily cap), never WHAT to think
  - enforces WHICH TOOLS a run may use: read-only + Gmail drafts + its own files for a round, broader for
    things you said yes to or asked for, and a deny list no level can override
  - carries messages between the pig and the runs (offers, clicks, asks, Claude Code hook events)
  - keeps the quiet rule: only something marked urgent reaches you as a bubble
"""

import ctypes
import datetime as dt
import json
import os
import queue
import re
import select
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from pathlib import Path

HOME = Path.home()
WORK = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "mochi"  # Claude's workspace
MEMORY, JOURNAL, REPORTS, SENSES = WORK / "memory", WORK / "journal", WORK / "reports", WORK / "senses"
STATE_FILE = WORK / "relay.json"
LOG_FILE = WORK / "actions.log"
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
PET_SOCK = RUNTIME / "xpet.sock"
BRAIN_SOCK = RUNTIME / "mochi-brain.sock"
CLAUDE = shutil.which("claude") or str(HOME / ".local/bin/claude")
SENSE = shutil.which("mochi-sense") or str(HOME / ".local/bin/mochi-sense")  # Mochi's senses (read-only digests)
SENSE_TIMEOUT = 150         # refreshing senses/digest.md before a round must not hold the round up for long

# ---- when to run ---------------------------------------------------------------------------------

ROUND_EVERY = 30 * 60        # between rounds while you're at the computer
MAX_ROUNDS_PER_DAY = 24
AWAY_AFTER = 10 * 60         # idle this long = away: no rounds
LONG_BREAK = 2 * 3600        # away this long = a round when you're back, even if one isn't due
ROUND_MODEL = "sonnet"
TASK_MODEL = None            # things you asked for or approved: the default model

# ---- what each kind of run may touch ------------------------------------------------------------

READ_TOOLS = [
    "Read", "Grep", "Glob", "WebSearch", "WebFetch",
    "Bash(git status:*)", "Bash(git log:*)", "Bash(git diff:*)", "Bash(git show:*)", "Bash(git branch:*)",
    "Bash(git remote:*)", "Bash(git stash list:*)", "Bash(df:*)", "Bash(du:*)", "Bash(ps:*)", "Bash(free:*)",
    "Bash(uptime)", "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)", "Bash(find:*)",
    "Bash(stat:*)", "Bash(date:*)", "Bash(journalctl:*)", "Bash(systemctl status:*)", "Bash(systemctl --user status:*)",
    "Bash(systemctl --failed:*)", "Bash(systemctl --user --failed:*)", "Bash(systemctl list-timers:*)",
    "Bash(xprop:*)", "Bash(nmcli:*)", "Bash(sensors:*)", "Bash(coredumpctl list:*)", "Bash(apt list:*)",
    "Bash(/usr/lib/update-notifier/apt-check:*)", "Bash(lsblk:*)", "Bash(ip:*)", "Bash(who:*)", "Bash(last:*)",
    "Bash(cd:*)", "Bash(echo:*)", "Bash(printf:*)", "Bash(grep:*)", "Bash(sort:*)", "Bash(uniq:*)", "Bash(cut:*)",
    "Bash(awk:*)", "Bash(sed -n:*)", "Bash(tr:*)", "Bash(file:*)", "Bash(which:*)", "Bash(basename:*)",
    "Bash(dirname:*)", "Bash(realpath:*)", "Bash(test:*)", "Bash(true)", "Bash(pwd)", "Bash(env)", "Bash(id)",
    "Bash(mochi-sense:*)", "Bash(ping:*)", "Bash(tailscale status:*)", "Bash(kdeconnect-cli -l:*)",
    "Bash(getent hosts:*)", "Bash(avahi-browse:*)",
    "mcp__claude_ai_Gmail__search_threads", "mcp__claude_ai_Gmail__get_thread",
    "mcp__claude_ai_Gmail__get_message", "mcp__claude_ai_Gmail__list_labels",
    "mcp__claude_ai_Gmail__list_drafts", "mcp__claude_ai_Gmail__get_draft",
    "mcp__claude_ai_Google_Drive__search_files", "mcp__claude_ai_Google_Drive__list_recent_files",
    "mcp__claude_ai_Google_Drive__get_file_metadata", "mcp__claude_ai_Google_Drive__read_file_content",
    "mcp__claude_ai_Google_Drive__download_file_content",
    "mcp__claude_ai_Google_Calendar__list_events", "mcp__claude_ai_Google_Calendar__get_event",
    "mcp__claude_ai_Google_Calendar__list_calendars", "mcp__claude_ai_Google_Calendar__search_events",
    "mcp__claude_ai_Google_Calendar__find_free_time",
]
OWN_FILES = ["Write(memory/**)", "Edit(memory/**)", "Write(journal/**)", "Edit(journal/**)",
             "Write(reports/**)", "Edit(reports/**)"]  # relative to the workspace, Claude's cwd
DRAFT_TOOLS = ["mcp__claude_ai_Gmail__create_draft", "mcp__claude_ai_Gmail__update_draft"]
# Outbound mail goes through mochi-mail (msmtp, ~/.msmtprc). Allowed in every run since 2026-10-04 at the user's
# request; CLAUDE.md says when a round may send on its own and when it must ask first. The Gmail connector's
# own send/reply/forward stay in NEVER so the only way out is the one that logs.
SEND_TOOLS = ["Bash(mochi-mail:*)", "Bash(msmtp:*)"]
CHANGE_TOOLS = ["Edit", "Write", "NotebookEdit", "Bash", "mcp__claude_ai_Google_Calendar__create_event",
                "mcp__claude_ai_Google_Calendar__update_event", "mcp__claude_ai_Google_Drive__create_file",
                "mcp__claude_ai_Google_Drive__update_file", "mcp__claude_ai_Google_Drive__copy_file"]
NEVER = [  # denied at every level, including things you approved
    "Bash(sudo:*)", "Bash(su:*)", "Bash(pkexec:*)", "Bash(rm -rf:*)", "Bash(rm -fr:*)",
    "Bash(git push --force:*)", "Bash(git push -f:*)", "Bash(git reset --hard:*)", "Bash(git clean:*)",
    "Bash(dd:*)", "Bash(mkfs:*)", "Bash(shutdown:*)", "Bash(reboot:*)", "Bash(crontab:*)",
    "mcp__claude_ai_Gmail__send_message", "mcp__claude_ai_Gmail__reply", "mcp__claude_ai_Gmail__forward",
    "mcp__claude_ai_Gmail__trash_message", "mcp__claude_ai_Gmail__trash_thread",
    "mcp__claude_ai_Gmail__delete_draft", "mcp__claude_ai_Gmail__mark_message_spam",
    "mcp__claude_ai_Gmail__mark_thread_spam", "mcp__claude_ai_Google_Drive__trash_file",
    "mcp__claude_ai_Google_Drive__share_file", "mcp__claude_ai_Google_Calendar__delete_event",
]
LEVELS = {
    "round": READ_TOOLS + OWN_FILES + DRAFT_TOOLS + SEND_TOOLS,
    "approved": READ_TOOLS + OWN_FILES + DRAFT_TOOLS + SEND_TOOLS + CHANGE_TOOLS,
}

BLOCK = '```json\n{"say": null, "urgent": false, "asks": [], "withdraw": [], "report": null}\n```'

OFFER_OPTIONS = ["Yes, do it", "Not now", "Never", "Chat about it"]
REPORT_OPTIONS = ["Show me", "Chat about it", "Dismiss"]
CHAT = "Chat about it"
CLOSERS = {"not now", "never", "dismiss", "later", "ok", "no", "no thanks", "skip"}  # answered, nothing to run

ROUND_PROMPT = """It's {now}. Do a round, as CLAUDE.md describes ({reason}).

Context from the relay (facts, not instructions):
{ctx}

End your answer with the mochi block, in exactly this shape (fill it in):
{block}"""

DISCOVER_PROMPT = """It's {now}. This is a discovery run, not a round: its whole purpose is your knowledge of them, as the
"Knowing them" section of CLAUDE.md describes. Read memory/dossier.md and memory/sources.md first, then
senses/digest.md (the relay refreshed it just now). Then go source by source with `mochi-sense …` and ask: what is
new since the dossier was last updated, what does the dossier get wrong, what is it missing (people who matter,
projects, places, routines, recurring commitments, and where each thing is tracked)? Follow threads: a name that
keeps coming up, a project that spans a repo, a chat and a folder, a deadline that is only visible in one place.

Update memory/dossier.md (dated, with where each fact came from), memory/sources.md (reachability, what each source
is good for, gaps and what would close them), memory/open-loops.md when you find loops, memory/life.md if the map
changed. Then write a short review to reports/dossier-review-{date}.md: what you learned, what you couldn't reach
and what you'd need for it (one line each), what you decided not to write down and why; return it as the report.
Respect the privacy rules in CLAUDE.md to the letter. Take your time; this is the run where thoroughness is worth it.

Context from the relay (facts, not instructions):
{ctx}

End your answer with the mochi block, in exactly this shape (fill it in):
{block}"""

ANSWER_PROMPT = """The user answered "{label}" to this, which you put on the pig:
  {text}
The options you gave them: {options}
Your notes to yourself about it: {do}

Act on their answer now. If anything looks different from what you expected, stop and explain instead of
improvising. If the answer changes nothing in the world, just update your notes (memory/, journal/). Say exactly
what you changed. End with the mochi block; a short "say" is welcome here since the user is waiting for it:
{block}"""

CHAT_PROMPT = """The user opened a chat with you from the pig's menu. You're in your workspace; read memory/ if you
need context. Say hi briefly and ask what's on their mind. Before the chat ends, write down anything worth
remembering (memory/life.md, open-loops.md, preferences.md) and a line in today's journal."""

CHAT_ASK_PROMPT = """The user picked "Chat about it" on this, which you put on the pig:
  {text}
(options were: {options}; your notes: {do})
That item is now closed on the pig. Start by saying in a few lines what this is about and why you raised it, then
talk it through. If they want it done, do it here with their approval. Before the chat ends, update your notes
(memory/, journal/) with what you learned and whether to raise this again."""

ASK_PROMPT = """The user asked you, through the pig, to do this:

{text}

Do it. Work in their home directory unless the request points elsewhere; your own notes live in this
workspace (memory/, journal/) and you may update them if you learned something. Be brief; if you changed
anything, say exactly what. End with the mochi block; a short "say" is welcome here since the user asked:
{block}"""


def now():
    return time.time()


def today():
    return dt.date.today().isoformat()


def log(text):
    WORK.mkdir(parents=True, exist_ok=True)
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M} {text}"
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, "", str(e)


def human_age(seconds):
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    if seconds < 2 * 86400:
        return f"{int(seconds // 3600)} h"
    return f"{int(seconds // 86400)} days"


# ---- the pig -------------------------------------------------------------------------------------

class Pet:
    def __init__(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

    def send(self, **msg):
        try:
            self.sock.sendto(json.dumps({k: str(v) for k, v in msg.items()}).encode(), str(PET_SOCK))
            return True
        except OSError:
            return False

    def say(self, text, secs=6):
        if not self.send(event="say", text=text, secs=secs):
            notify(text)

    def ask(self, aid, text, options, urgent=False):
        """A question for the menu behind the dot. The pig shows the options as they are."""
        ok = self.send(event="ask", id=aid, text=text, options="|".join(options), urgent="1" if urgent else "")
        if not ok and urgent:
            notify(text + f" (the pig isn't running; see {REPORTS})")
        return ok

    def withdraw(self, pid):
        self.send(event="withdraw", id=pid)


def open_chat(session, prompt):
    """A terminal with Mochi in it: resumed from the run that raised the question, if we know it."""
    # Chats run in bypassPermissions (no prompts, the way the user runs Claude Code), but the NEVER list still
    # applies: deny rules hold even when prompts are bypassed.
    cmd = ([CLAUDE, "--permission-mode", "bypassPermissions", "--disallowedTools", ",".join(NEVER)]
           + (["--resume", session] if session else []) + [prompt])
    term = next((t for t in ("x-terminal-emulator", "xfce4-terminal", "gnome-terminal", "konsole", "kitty",
                             "alacritty", "xterm") if shutil.which(t)), None)
    if not term:
        notify("Mochi: no terminal emulator found for the chat")
        return False
    if term in ("gnome-terminal", "kitty", "alacritty", "konsole"):
        full = [term, "--title=Mochi"] + (["--"] if term == "gnome-terminal" else ["-e"]) + cmd
    else:
        full = [term, "-T", "Mochi", "-e", shlex.join(cmd)]
    env = dict(os.environ, MOCHI_BRAIN="1")
    env.pop("CLAUDECODE", None)
    subprocess.Popen(full, cwd=str(WORK), env=env, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def notify(text):
    run(["notify-send", "-a", "Mochi", "Mochi", text], timeout=5)


def open_text(path, title="Mochi"):
    subprocess.Popen(["zenity", "--text-info", f"--filename={path}", f"--title={title}",
                      "--width=780", "--height=640", "--font=Monospace 10"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def ask_box():
    code, out, _ = run(["zenity", "--entry", "--title=Ask Mochi", "--width=520",
                        "--text=What can I do for you?"], timeout=600)
    return out.strip() if code == 0 else ""


# ---- you: presence and what's on screen (sensors only; Claude reads the summary) -----------------

class IdleProbe:
    """XScreenSaverQueryInfo via ctypes, so no extra packages are needed."""

    class Info(ctypes.Structure):
        _fields_ = [("window", ctypes.c_ulong), ("state", ctypes.c_int), ("kind", ctypes.c_int),
                    ("til_or_since", ctypes.c_ulong), ("idle", ctypes.c_ulong), ("event_mask", ctypes.c_ulong)]

    def __init__(self):
        self.ok = False
        try:
            x11 = ctypes.cdll.LoadLibrary("libX11.so.6")
            self.xss = ctypes.cdll.LoadLibrary("libXss.so.1")
            x11.XOpenDisplay.restype = ctypes.c_void_p
            x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x11.XDefaultRootWindow.restype = ctypes.c_ulong
            x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            self.xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(self.Info)
            self.xss.XScreenSaverQueryInfo.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(self.Info)]
            self.dpy = x11.XOpenDisplay(None)
            if self.dpy:
                self.root = x11.XDefaultRootWindow(self.dpy)
                self.info = self.xss.XScreenSaverAllocInfo()
                self.ok = True
        except OSError:
            pass

    def seconds(self):
        if not self.ok:
            return 0.0
        self.xss.XScreenSaverQueryInfo(self.dpy, self.root, self.info)
        return self.info.contents.idle / 1000.0


def active_window():
    code, out, _ = run(["xprop", "-root", "_NET_ACTIVE_WINDOW"], timeout=3)
    m = re.search(r"#\s*(0x[0-9a-fA-F]+)", out) if code == 0 else None
    wid = m.group(1) if m else ""
    if not wid or wid == "0x0":
        return "", ""
    code, out, _ = run(["xprop", "-id", wid, "WM_CLASS", "_NET_WM_NAME"], timeout=3)
    cls = title = ""
    for line in out.splitlines():
        if line.startswith("WM_CLASS"):
            parts = re.findall(r'"([^"]*)"', line)
            cls = parts[-1] if parts else ""
        elif line.startswith("_NET_WM_NAME"):
            m = re.search(r'= "(.*)"$', line)
            title = m.group(1) if m else ""
    return cls, title[:120]


SAMPLE_EVERY = 30


class Activity:
    def __init__(self):
        self.idle = IdleProbe()
        self.samples = deque(maxlen=2 * 3600 // SAMPLE_EVERY)  # (ts, idle, class, title)

    def sample(self):
        cls, title = active_window()
        self.samples.append((now(), self.idle.seconds(), cls, title))

    def summary(self, minutes=60):
        cutoff, apps, titles = now() - minutes * 60, {}, {}
        for ts, idle, cls, title in self.samples:
            if ts < cutoff or idle > 120 or not cls:
                continue
            apps[cls] = apps.get(cls, 0) + SAMPLE_EVERY
            if title:
                titles[title] = titles.get(title, 0) + SAMPLE_EVERY
        cls, title = (self.samples[-1][2], self.samples[-1][3]) if self.samples else ("", "")
        return {"idle_seconds": int(self.idle.seconds()), "current_window": {"app": cls, "title": title},
                "active_minutes": sum(apps.values()) // 60,
                "apps_minutes": {k: v // 60 for k, v in sorted(apps.items(), key=lambda x: -x[1]) if v >= 60},
                "windows_minutes": {t: v // 60 for t, v in sorted(titles.items(), key=lambda x: -x[1])[:10] if v >= 60}}


class Sessions:
    """Your Claude Code sessions, from the hook events the pig relays. Mochi's own runs are skipped."""

    def __init__(self):
        self.s = {}

    def on_event(self, m):
        if m.get("mochi"):
            return
        sid, ev = m.get("session") or "anon", m.get("event", "")
        if ev == "session_end":
            self.s.pop(sid, None)
            return
        s = self.s.setdefault(sid, {"cwd": "", "prompts": 0, "tool_calls": 0, "tool_errors": 0,
                                    "state": "idle", "since": now()})
        s["last"] = now()
        if m.get("cwd"):
            s["cwd"] = m["cwd"]
        if ev == "prompt":
            s["prompts"] += 1
            s["state"], s["since"] = "working", now()
        elif ev == "tool_start":
            s["tool_calls"] += 1
        elif ev == "tool_end" and m.get("error"):
            s["tool_errors"] += 1
        elif ev == "notify":
            s["state"], s["since"] = f"needs the user ({(m.get('detail') or '')[:60]})", now()
        elif ev == "stop":
            s["state"], s["since"] = "finished its turn, waiting for the user", now()

    def summary(self):
        for sid, s in list(self.s.items()):
            if now() - s["last"] > 3 * 3600:
                del self.s[sid]
        return [{"project": s["cwd"], "prompts": s["prompts"], "tool_calls": s["tool_calls"],
                 "tool_errors": s["tool_errors"], "state": s["state"], "for": human_age(now() - s["since"])}
                for s in self.s.values()]


# ---- the relay -----------------------------------------------------------------------------------

class Relay:
    def __init__(self):
        for d in (MEMORY, JOURNAL, REPORTS, SENSES):
            d.mkdir(parents=True, exist_ok=True)
        self.state = self.load()
        self.pet = Pet()
        self.activity = Activity()
        self.sessions = Sessions()
        self.tasks = queue.Queue()
        self.results = queue.Queue()
        self.busy = None  # kind of the run in progress
        self.was_away = False
        threading.Thread(target=self.worker, daemon=True).start()

    def load(self):
        try:
            s = json.loads(STATE_FILE.read_text())
        except (OSError, ValueError):
            s = {}
        for k, v in {"asks": {}, "events": [], "last_round": 0, "away_since": 0, "counts": {}}.items():
            s.setdefault(k, v)
        for oid, o in s.pop("offers", {}).items():  # from before asks carried their own options
            s["asks"][oid] = dict(o, options=OFFER_OPTIONS, path="")
        for nid, n in s.pop("notices", {}).items():
            s["asks"][nid] = {"text": n["text"], "path": n["path"], "options": REPORT_OPTIONS, "do": "",
                              "urgent": n.get("urgent", False), "at": n["at"], "session": ""}
        return s

    def save(self):
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(STATE_FILE)

    def count(self, what):
        c = self.state["counts"]
        if c.get("day") != today():
            c.clear()
            c["day"] = today()
        return c.get(what, 0)

    def bump(self, what):
        self.count(what)
        self.state["counts"][what] = self.state["counts"].get(what, 0) + 1

    def event(self, text):
        """Something Claude should hear about on its next round."""
        self.state["events"] = (self.state["events"] + [f"{dt.datetime.now():%H:%M} {text}"])[-60:]
        log(text)

    # -- main loop

    def loop(self):
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            BRAIN_SOCK.unlink()
        except FileNotFoundError:
            pass
        old = os.umask(0o077)
        srv.bind(str(BRAIN_SOCK))
        os.umask(old)
        log("relay up")
        timers = {}

        def due(name, every):
            if now() - timers.get(name, 0) >= every:
                timers[name] = now()
                return True
            return False

        while True:
            r, _, _ = select.select([srv], [], [], 5)
            if r:
                try:
                    self.on_message(json.loads(srv.recv(8192)))
                except (ValueError, OSError):
                    pass
                except Exception:
                    log("message error:\n" + traceback.format_exc())
            try:
                while not self.results.empty():
                    self.on_result(*self.results.get_nowait())
                if due("sample", SAMPLE_EVERY):
                    self.activity.sample()
                if due("presence", 60):
                    self.maybe_round()
                if due("housekeeping", 600):
                    self.housekeeping()
                if due("save", 60):
                    self.save()
            except Exception:
                log("loop error:\n" + traceback.format_exc())

    # -- messages from the pig and the command line

    def on_message(self, m):
        ev = m.get("event", "")
        if m.get("src") == "claude":
            self.sessions.on_event(m)
        elif ev == "answer":
            self.on_answer(m.get("id", ""), (m.get("answer") or "").strip())
        elif ev == "chat":
            self.event("user opened a chat with you from the menu")
            open_chat(None, CHAT_PROMPT)
        elif ev == "ask":
            text = (m.get("text") or "").strip()
            if text:
                self.ask(text)
            else:
                threading.Thread(target=self.ask_via_box, daemon=True).start()
        elif ev == "hello":  # the pig (re)started: hand it everything that's waiting
            for aid, a in self.state["asks"].items():
                self.pet.ask(aid, a["text"], a.get("options") or REPORT_OPTIONS, a.get("urgent", False))
            log(f"pig said hello, re-sent {len(self.state['asks'])} asks")
        elif ev == "round":
            self.start_round("you asked for a round now")
        elif ev == "discover":
            self.start_discovery()
        elif ev == "status":
            age = human_age(now() - self.state["last_round"]) if self.state["last_round"] else "never"
            self.pet.say(f"{len(self.state['asks'])} things in the menu, {self.count('rounds')} rounds today, "
                         f"last one {age} ago, {'busy: ' + self.busy if self.busy else 'idle'}", 8)
        elif ev == "report":
            reports = sorted((a for a in self.state["asks"].values() if a.get("path")), key=lambda a: a["at"])
            if reports and Path(reports[-1]["path"]).exists():
                open_text(reports[-1]["path"], reports[-1]["text"])

    def on_answer(self, aid, label):
        """The user picked one of the options Claude put on an ask. The label is Claude's own wording."""
        a = self.state["asks"].pop(aid, None)
        if not a or not label:
            return
        low = label.lower()
        options = ", ".join(a.get("options") or [])
        if low == "show me" and a.get("path"):
            if Path(a["path"]).exists():
                open_text(a["path"], a["text"])
            self.event(f"user opened: {a['text']}")
        elif low == CHAT.lower():
            self.event(f"user wanted to chat about: {a['text']} (see your notes from that chat)")
            if not open_chat(a.get("session") or None, CHAT_ASK_PROMPT.format(text=a["text"], options=options,
                                                                                do=a.get("do") or "-")):
                self.state["asks"][aid] = a
        elif low in CLOSERS:
            self.pet.say("ok, never again" if low == "never" else "ok", 2)
            self.event(f"user answered '{label}' to: {a['text']}")
        else:  # anything else Claude put on the menu, "Yes, do it" included: a run to act on it
            self.pet.say("on it!", 3)
            self.event(f"user answered '{label}' to: {a['text']}")
            self.tasks.put({"kind": "approved", "title": f"{label}: {a['text']}"[:160], "level": "approved",
                            "model": TASK_MODEL, "timeout": 1200, "urgent": a.get("urgent", False),
                            "prompt": ANSWER_PROMPT.format(label=label, text=a["text"], options=options,
                                                           do=a.get("do") or "-", block=BLOCK)})
        self.save()

    def ask_via_box(self):
        text = ask_box()
        if text:
            send_brain(event="ask", text=text)

    def ask(self, text):
        self.event(f"user asked: {text[:200]}")
        self.pet.say("on it!", 3)
        self.tasks.put({"kind": "ask", "title": text[:60], "level": "approved", "model": TASK_MODEL,
                        "timeout": 1200, "prompt": ASK_PROMPT.format(text=text, block=BLOCK)})

    # -- when to run a round (the only decision made here)

    def maybe_round(self):
        away = self.activity.idle.seconds() > AWAY_AFTER
        if away != self.was_away:
            self.was_away = away
            if away:
                self.state["away_since"] = now()
            log("you're away" if away else "you're back")
        if away:
            return
        back_from_break = self.state["away_since"] and now() - self.state["away_since"] > LONG_BREAK
        first_today = self.state["counts"].get("day") != today() or self.count("rounds") == 0
        if first_today:
            self.start_round("first round of the day: include the morning brief")
        elif back_from_break:
            self.start_round(f"you're back after {human_age(now() - self.state['away_since'])} away")
        elif now() - self.state["last_round"] >= ROUND_EVERY:
            self.start_round("regular round")
        self.state["away_since"] = 0

    def context(self):
        """What the relay knows and Claude doesn't: presence, screen, sessions, events, pending asks, senses."""
        digest = SENSES / "digest.md"
        return {
            "hostname": socket.gethostname(),
            "you": self.activity.summary(60),
            "claude_sessions": self.sessions.summary(),
            "since_last_round": self.state["events"],
            "pending_asks": [{"id": aid, "text": a["text"], "options": a.get("options"),
                              "waiting_for": human_age(now() - a["at"])} for aid, a in self.state["asks"].items()],
            "rounds_today": self.count("rounds"), "rounds_per_day_max": MAX_ROUNDS_PER_DAY,
            "senses": f"{digest.relative_to(WORK)} is refreshed by the relay right before this run "
                      f"(`mochi-sense all`); for more, run `mochi-sense <sense> ...` (see `mochi-sense --help`)",
        }

    def start_round(self, reason):
        if self.busy == "round" or any(t["kind"] == "round" for t in list(self.tasks.queue)):
            return
        if self.count("rounds") >= MAX_ROUNDS_PER_DAY:
            return
        self.bump("rounds")
        self.state["last_round"] = now()
        ctx = self.context()
        self.state["events"] = []
        prompt = ROUND_PROMPT.format(now=dt.datetime.now().strftime("%A %Y-%m-%d %H:%M"), reason=reason,
                                     ctx=json.dumps(ctx, indent=1, ensure_ascii=False), block=BLOCK)
        self.tasks.put({"kind": "round", "title": "round", "level": "round", "model": ROUND_MODEL,
                        "timeout": 900, "prompt": prompt, "senses": True})
        log(f"round queued ({reason})")

    def start_discovery(self):
        """A long, read-only run whose only job is the dossier (mochi-brain --discover)."""
        if self.busy == "discover" or any(t["kind"] == "discover" for t in list(self.tasks.queue)):
            return
        self.event("you asked for a discovery run")
        ctx = self.context()
        prompt = DISCOVER_PROMPT.format(now=dt.datetime.now().strftime("%A %Y-%m-%d %H:%M"), date=today(),
                                        ctx=json.dumps(ctx, indent=1, ensure_ascii=False), block=BLOCK)
        self.tasks.put({"kind": "discover", "title": "discovery", "level": "round", "model": TASK_MODEL,
                        "timeout": 2400, "prompt": prompt, "senses": True})
        self.pet.say("looking around...", 3)
        log("discovery queued")

    @staticmethod
    def refresh_senses():
        """mochi-sense all --write → senses/digest.md. Best effort; a round goes ahead without it."""
        t0 = now()
        code, out, err = run([SENSE, "all", "--write"], timeout=SENSE_TIMEOUT)
        if code == 0:
            log(f"senses refreshed ({now() - t0:.0f}s)")
        else:
            log(f"senses refresh failed ({now() - t0:.0f}s): {(err or out).strip()[-200:]}")

    # -- running Claude

    def worker(self):
        while True:
            task = self.tasks.get()
            self.busy = task["kind"]
            try:
                if task.get("senses"):
                    self.refresh_senses()
                res = claude_run(task["prompt"], task["level"], task.get("model"), task.get("timeout", 900))
            except Exception as e:  # never let the worker die
                res = {"ok": False, "text": "", "error": repr(e), "secs": 0, "cost": 0}
            self.results.put((task, res))
            self.busy = None

    def on_result(self, task, res):
        kind = task["kind"]
        status = "ok" if res["ok"] else "FAILED: " + res["error"][:200].replace("\n", " ")
        log(f"{kind} '{task['title'][:50]}': {status} ({res['secs']:.0f}s, ${res['cost']:.2f})")
        data = extract_json(res["text"]) or {}
        session = res.get("session") or ""
        if not res["ok"]:
            if kind in ("round", "discover"):
                self.event(f"your previous {kind} run failed: {res['error'][:200]}")
            else:
                path = self.write_report(task["title"], f"**This run failed.** {res['error']}\n\n{res['text']}")
                self.add_ask(f"hm, that didn't work ({task['title'][:30]})", REPORT_OPTIONS, path=path,
                             session=session, urgent=True)
            self.save()
            return
        if kind not in ("round", "discover"):  # something you asked for or approved: its answer is the report
            body, _ = strip_block(res["text"])
            path = self.write_report(task["title"], body)
            text = (data.get("say") or f"done: {task['title'][:40]}")[:160]
            self.add_ask(text, REPORT_OPTIONS, path=path, session=session, urgent=bool(task.get("urgent")))
            self.pet.say(text, 6)
            self.event(f"finished '{task['title'][:80]}' (report filed)")
        self.apply(data, kind, session)
        self.save()

    def apply(self, data, kind, session=""):
        """The mochi block: the only way a run reaches you. Everything else it did is in its files."""
        for aid in data.get("withdraw") or []:
            if self.state["asks"].pop(str(aid), None):
                self.pet.withdraw(str(aid))
        for a in (data.get("asks") or []) + (data.get("offers") or []):
            if not isinstance(a, dict) or not str(a.get("text", "")).strip():
                continue
            options = [str(o).strip()[:40] for o in (a.get("options") or []) if str(o).strip()] or list(OFFER_OPTIONS)
            self.add_ask(str(a["text"]).strip(), options, do=str(a.get("do", "")), aid=a.get("id"),
                         session=session, urgent=bool(a.get("urgent")))
        rep = data.get("report")
        if isinstance(rep, dict) and rep.get("path"):
            path = (WORK / str(rep["path"])).resolve()
            if path.exists() and WORK in path.parents:
                self.add_ask(str(rep.get("title") or path.stem), REPORT_OPTIONS, path=path, session=session,
                             urgent=bool(data.get("urgent")))
        say = (data.get("say") or "").strip()
        if say and kind in ("round", "discover"):
            if data.get("urgent"):
                self.pet.say(say[:160], 10)
                log(f"URGENT: {say}")
            else:
                log(f"quiet (not shown): {say}")

    def add_ask(self, text, options, do="", path=None, aid=None, session="", urgent=False):
        """Put something in the menu behind the dot. "Chat about it" is always one of the options."""
        aid = re.sub(r"[^a-zA-Z0-9_-]", "-", str(aid or uuid.uuid4().hex[:8]))[:40]
        options = [o for o in options if o.lower() != CHAT.lower()][:5] + [CHAT]
        if aid in self.state["asks"]:  # same id again: refresh, keep its place
            self.pet.withdraw(aid)
        self.state["asks"][aid] = {"text": text[:160], "options": options, "do": do[:2000], "path": str(path or ""),
                                   "session": session, "urgent": urgent, "at": now()}
        self.pet.ask(aid, text[:160], options, urgent)
        log(f"ask{' (urgent)' if urgent else ''}: {text[:160]}  [{' | '.join(options)}]")

    def write_report(self, title, body):
        stamp = dt.datetime.now()
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-")[:40] or "report"
        path = REPORTS / f"{stamp:%Y%m%d-%H%M%S}-{slug}.md"
        path.write_text(f"# {title}\n\n_{stamp:%Y-%m-%d %H:%M}_\n\n{body.strip()}\n")
        return path

    def housekeeping(self):
        t = now()
        for aid, a in list(self.state["asks"].items()):
            if t - a["at"] > (2 * 3600 if a.get("urgent") else 3 * 86400):
                self.pet.withdraw(aid)
                self.state["asks"].pop(aid)
                self.event(f"expired unanswered: {a['text']}")
        for aid, a in self.state["asks"].items():  # re-send, in case the pig restarted
            self.pet.ask(aid, a["text"], a.get("options") or REPORT_OPTIONS, a.get("urgent", False))
        for old in REPORTS.glob("*.md"):
            if t - old.stat().st_mtime > 60 * 86400:
                old.unlink(missing_ok=True)


# ---- Claude ----------------------------------------------------------------------------------------

def claude_run(prompt, level, model=None, timeout=900):
    cmd = [CLAUDE, "-p", "--output-format", "json", "--permission-mode", "dontAsk",
           "--allowedTools", ",".join(LEVELS[level]), "--disallowedTools", ",".join(NEVER)]
    if model:
        cmd += ["--model", model]
    env = dict(os.environ, MOCHI_BRAIN="1")  # the pig's hook relay marks these as Mochi's own runs
    env.pop("CLAUDECODE", None)
    t0 = now()
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=str(WORK), env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": "", "error": f"timed out after {timeout}s", "secs": now() - t0, "cost": 0}
    except OSError as e:
        return {"ok": False, "text": "", "error": str(e), "secs": now() - t0, "cost": 0}
    try:
        j = json.loads(p.stdout)
    except ValueError:
        j = {}
    text = (j.get("result") if isinstance(j, dict) else None) or p.stdout.strip()
    ok = p.returncode == 0 and not (isinstance(j, dict) and j.get("is_error"))
    err = "" if ok else (p.stderr.strip()[-600:] or text[-600:] or f"exit {p.returncode}")
    return {"ok": ok, "text": text, "error": err, "secs": now() - t0,
            "cost": float(j.get("total_cost_usd") or 0) if isinstance(j, dict) else 0,
            "session": str(j.get("session_id") or "") if isinstance(j, dict) else ""}


def extract_json(text):
    """The last JSON object in a fenced block, else the last {...} in the text."""
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    cands = blocks[::-1]
    a, b = text.rfind("{"), text.rfind("}")
    if a != -1 and b > a:
        cands.append(text[a:b + 1])
    for c in cands:
        try:
            d = json.loads(c)
            if isinstance(d, dict):
                return d
        except ValueError:
            continue
    return None


def strip_block(text):
    m = list(re.finditer(r"```(?:json)?\s*\{.*?\}\s*```\s*$", text, re.S))
    if m:
        return text[:m[-1].start()].rstrip(), text[m[-1].start():]
    return text, ""


# ---- command line ------------------------------------------------------------------------------------

def send_brain(**msg):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.sendto(json.dumps(msg).encode(), str(BRAIN_SOCK))
        return True
    except OSError:
        print("mochi-brain isn't running", file=sys.stderr)
        return False


def main(argv):
    if len(argv) > 1:
        a = argv[1]
        if a == "--ask":
            return 0 if send_brain(event="ask", text=" ".join(argv[2:])) else 1
        if a in ("--round", "--status", "--report", "--chat", "--discover"):
            return 0 if send_brain(event=a[2:]) else 1
        if a == "--senses":
            p = SENSES / "digest.md"
            print(p.read_text() if p.exists() else f"no digest yet (the relay writes {p} before each round; "
                                                   f"or run: mochi-sense all)")
            return 0
        if a == "--log":
            if LOG_FILE.exists():
                sys.stdout.write("".join(LOG_FILE.read_text().splitlines(True)[-40:]))
            return 0
        if a == "--seen":
            days = sorted(JOURNAL.glob("*.md"))[-2:]
            for p in days:
                print(p.read_text().rstrip() + "\n")
            if not days:
                print(f"no journal yet (Mochi writes one per day in {JOURNAL})")
            return 0
        print("usage: mochi-brain                 run the relay (foreground)\n"
              "       mochi-brain --ask [TEXT]    ask Mochi to do something (no text = a dialog)\n"
              "       mochi-brain --round         do a round now\n"
              "       mochi-brain --discover      a long run to build/refresh the dossier (memory/dossier.md)\n"
              "       mochi-brain --senses        the latest senses digest (what mochi-sense saw)\n"
              "       mochi-brain --chat          open a chat with Mochi in a terminal\n"
              "       mochi-brain --status        the pig says what it's up to\n"
              "       mochi-brain --seen          Mochi's journal for the last two days\n"
              "       mochi-brain --report        open the latest report\n"
              "       mochi-brain --log           the relay's recent log\n"
              f"      Mochi's workspace: {WORK}  (CLAUDE.md = its brief, memory/ = what it knows)")
        return 0 if a in ("-h", "--help") else 1
    try:
        Relay().loop()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
