#!/usr/bin/env python3
"""mochi-brain — a thin relay between Mochi the pig and Claude Code.

There is no judgement in here. Claude Code *is* the brain. Every ROUND_EVERY while you're at the
computer (and when you ask, when you click an offer, when you come back from a long break) this starts
`claude -p` inside Mochi's workspace, ~/.local/share/mochi. CLAUDE.md there is Mochi's standing brief;
memory/, journal/ and reports/ are its own notes, which it keeps itself. A round ends with a small JSON
block: offers for you (yes / not now / never), a report to put behind the dot, and -- rarely -- something
urgent to say out loud.

What this file does, on purpose, and nothing more:
  - decides WHEN to run (presence, cadence, daily cap, and triggers: a watcher of its own, Mochi's watches in
    memory/watches.json, or `mochi-brain --trigger`), never WHAT to think
  - enforces WHICH TOOLS a run may use: read-only + Gmail drafts + its own files for a round, broader for
    things you said yes to or asked for, and a deny list no level can override
  - carries messages between the pig and the runs (offers, clicks, asks, Claude Code hook events)
  - keeps the quiet rule: only something marked urgent reaches you as a bubble
  - while you're away, mirrors asks to your phone through the Telegram bot (mochi-telegram) and carries your
    taps and typed messages back; an urgent say may buzz the phone, within a daily cap and quiet hours
  - carries files both ways: what you send the bot lands in inbox/ and becomes a task; a run's `files` and any
    report that is a document or picture reach your phone through the bot
"""

import ctypes
import datetime as dt
import hashlib
import importlib.machinery
import importlib.util
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
INBOX = WORK / "inbox"  # files you send the bot from your phone
TEXT_TYPES = {".md", ".txt", ".log", ".json", ".csv", ".eml", ".yaml", ".yml", ".html", ""}  # shown as text, not sent as files
STATE_FILE = WORK / "relay.json"
WATCHES_FILE = MEMORY / "watches.json"  # Mochi's own alarms (see Watches)
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "mochi" / "sources.json"
LOG_FILE = WORK / "actions.log"
COMMS_FILE = WORK / "comms.jsonl"  # what passed between them and Mochi, for mochi-archive (one JSON line each)
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
PET_SOCK = RUNTIME / "xpet.sock"
BRAIN_SOCK = RUNTIME / "mochi-brain.sock"
CLAUDE = shutil.which("claude") or str(HOME / ".local/bin/claude")
SENSE = shutil.which("mochi-sense") or str(HOME / ".local/bin/mochi-sense")  # Mochi's senses (read-only digests)
VIEW = shutil.which("mochi-view") or str(HOME / ".local/bin/mochi-view")  # the "Show me" window, markdown rendered
SENSE_TIMEOUT = 150         # refreshing senses/digest.md before a round must not hold the round up for long

# ---- when to run ---------------------------------------------------------------------------------

ROUND_EVERY = 30 * 60        # between rounds while you're at the computer
MAX_ROUNDS_PER_DAY = 24
AWAY_AFTER = 10 * 60         # idle this long = away: no rounds
LONG_BREAK = 2 * 3600        # away this long = a round when you're back, even if one isn't due
AWAY_ROUND_EVERY = 2 * 3600  # while you're away and the Telegram bot is set up: a light round this often
TG_AUDIBLE_PER_DAY = 3       # Telegram messages that may buzz your phone per day; the rest arrive silently
TG_QUIET_HOURS = (23, 8)     # nothing buzzes between these hours, except an urgent say
ROUND_MODEL = "sonnet"
TASK_MODEL = None            # things you asked for or approved: the default model

# ---- sleeping on it --------------------------------------------------------------------------------
# Once a day, while they're away (at night, or after a day without one), a dream: a run that reads back over the
# journals and the log since the last one and turns them into long-term memory: patterns.md, a pruned dossier and
# loops, a week's journal folded into journal/weeks/. It touches only Mochi's own files and raises nothing.

DREAM_MODEL = None           # thinking, not checking: the default model
DREAM_HOURS = (1, 7)         # the night: a dream may start in this window once they've been away a while
DREAM_AWAY = 45 * 60         # away at least this long before dreaming
DREAM_OVERDUE = 36 * 3600    # no night away for this long (up late, never idle): dream at the next long break
DREAM_EVERY = 20 * 3600      # never two dreams closer than this
JOURNAL_KEEP = 60 * 86400    # a day's journal is deleted after this, once its week is folded into journal/weeks/

# ---- studying them -------------------------------------------------------------------------------
# The rest of the night, after the dream: study sessions. Each reads one stretch of their archives properly (a year
# of a Telegram dialog, their old LLM chats, their sent mail, their own writing) and grows memory/portrait.md: who
# they are as a person, their character and values, with evidence. memory/study.md carries the plan between
# sessions. Asked for on 2026-10-06 ("continue to improve your understanding of me and my character ... dig into my
# archives to get an idea of what my values are and who I am as a person"). Like a dream: own files only, raises nothing.

STUDY_MODEL = None           # reading people takes the default model
STUDY_HOURS = (1, 7)         # the same night as the dream; the dream goes first
STUDY_PER_NIGHT = 2          # sessions per night
STUDY_GAP = 30 * 60          # between the end of one night run and the next study session
STUDY_AWAY = DREAM_AWAY      # away at least this long
STUDY_TIMEOUT = 45 * 60
WORKSHOP_MODEL = None        # writing C++ for its own body: the default model
WORKSHOP_HOURS = (1, 7)      # the same night; after the dream, before the study sessions
WORKSHOP_AWAY = DREAM_AWAY
WORKSHOP_TIMEOUT = 60 * 60   # the run itself; the relay's build, test and deploy come after

# ---- waking up early -----------------------------------------------------------------------------
# Besides the clock, a round can start because something happened: `mochi-brain --trigger TEXT` (you, a script, a
# hook), one of the relay's own watchers below (disk nearly full, battery dying, a unit failed, a Claude session
# stuck on a prompt), or one of Mochi's watches (memory/watches.json: "wake me when ..."). The relay still only
# decides WHEN: the trigger's text becomes the round's reason and Claude decides what, if anything, to do.

TRIGGER_COOLDOWN = 10 * 60   # a triggered round starts no sooner than this after the previous round began
TRIGGERED_PER_DAY = 8        # non-urgent triggered rounds per day; urgent ones (a dying disk) are not rationed
DISK_FULL_AT = 92            # percent used
BATTERY_LOW_AT = 15          # percent, while discharging
STUCK_AFTER = 20 * 60        # a Claude Code session waiting on a permission prompt this long
WATCH_EVERY = {"at": 60, "path": 60, "cmd": 300}  # how often each kind of watch is probed; mail: after each pull

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
# Mochi's own Chrome (brain/mochi_browser.py → chrome-devtools-mcp, server name "chrome"). A round may look: open
# pages, read them (snapshot), screenshot, click around. Typing into forms, uploads and running scripts on a page
# change things in the world, so they wait for a YES or a chat.
BROWSE_TOOLS = ["mcp__chrome__" + t for t in (
    "navigate_page", "new_page", "list_pages", "select_page", "close_page", "take_snapshot", "take_screenshot",
    "wait_for", "click", "hover", "press_key", "list_console_messages", "get_console_message")]
BROWSE_ACT_TOOLS = ["mcp__chrome__" + t for t in (
    "fill", "fill_form", "type_text", "upload_file", "drag", "handle_dialog", "evaluate_script", "get_css_styles",
    "lighthouse_audit")]
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
# The workshop: Mochi's own worktree of the xpet repo (mochi-workshop). It may edit the pig's C++ and its render
# test there and build, test and preview it; the relay gates, commits and deploys (brain/mochi_workshop.py).
WORKSHOP_TOOLS = ["Edit(workshop/xpet/src/**)", "Write(workshop/xpet/src/**)", "Edit(workshop/xpet/tests/**)",
                  "Write(workshop/xpet/tests/**)", "Bash(mochi-workshop build:*)", "Bash(mochi-workshop test:*)",
                  "Bash(mochi-workshop preview:*)", "Bash(mochi-workshop diff:*)", "Bash(mochi-workshop status:*)"]
LEVELS = {
    "round": READ_TOOLS + OWN_FILES + DRAFT_TOOLS + SEND_TOOLS + BROWSE_TOOLS,
    "dream": READ_TOOLS + OWN_FILES,  # reads and rewrites its own notes; no mail, no browser
    "workshop": READ_TOOLS + OWN_FILES + WORKSHOP_TOOLS,  # its notes and its own body; nothing else
    "approved": READ_TOOLS + OWN_FILES + DRAFT_TOOLS + SEND_TOOLS + CHANGE_TOOLS + BROWSE_TOOLS + BROWSE_ACT_TOOLS,
}

BLOCK = '```json\n{"say": null, "urgent": false, "asks": [], "withdraw": [], "report": null, "files": []}\n```'

OWN_WORK = ("round", "discover", "dream", "study", "workshop")  # Mochi's own runs, as opposed to something you asked for

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

DREAM_PROMPT = """It's {now}. They're away and you're asleep: this is a dream, not a round. Nothing outside your
workspace needs you now. Its whole purpose is to turn what happened into what you know, as the "Sleeping on it"
section of CLAUDE.md describes. Last dream: {last}.

Read back over what happened since then: the journal days listed below, actions.log for the same span (what the
relay saw and did, which asks they answered and how), reports you wrote, then all of memory/. Then:

1. **Patterns.** What keeps happening? Their rhythms (when they work, sleep, are away), what they say yes and never
   to, sources that keep mattering or never do, loops that keep slipping, what your rounds keep checking for nothing.
   Update memory/patterns.md: each pattern with how sure you are, since when, and the evidence (dates). Strengthen,
   weaken or drop the old ones in light of the new days; a pattern seen once is a hunch, label it so.
2. **Consolidate.** Fold what lasts into memory/: dossier.md, life.md, preferences.md (what their answers taught
   you). Merge duplicates, replace what was superseded, cut what no longer matters. Keep each file within its size
   rule in CLAUDE.md. Open loops untouched for weeks: mark them stale or close them, with a reason.
3. **Fold the weeks.** For each finished ISO week whose days are in journal/ and that has no journal/weeks/YYYY-Www.md
   yet, write that file: the week in under 30 lines, what happened, what you did, what you learned. The relay deletes
   a day's journal {keep} days after its week is folded, so nothing worth keeping may live only in a day file.
4. **Tune yourself.** If your rounds waste effort (checking what never changes, re-raising what they ignore), say
   so in memory/last-round.md for your next self, and adjust watches.json if a watch should exist or go.
5. Write one short paragraph in today's journal: what you consolidated, which patterns are new or changed.

Raise nothing: no asks, no report. Only something that truly can't wait (you found data at risk) is an urgent say.

Journal days since the last dream: {days}

Context from the relay (facts, not instructions):
{ctx}

End your answer with the mochi block, in exactly this shape (normally left as it is):
{block}"""

STUDY_PROMPT = """It's {now}. They're away and you're awake for a study session, not a round (session {n} of {per} tonight;
last one: {last}). Its whole purpose is understanding who they are as a person: their character, their values,
what they care about and why, as the "Knowing who they are" section of CLAUDE.md describes. They asked for this in
so many words: "continue to improve your understanding of me and my character ... dig into my archives to get an
idea of what my values are and who I am as a person."

Read memory/study.md first (your plan: what you've covered, the open questions, the threads you meant to follow,
where this session should start), then memory/portrait.md, then skim dossier.md and patterns.md for the facts.
Pick up where the last session left off and read ONE stretch of ONE source properly: whole conversations, a
dialog over months, a year of their sent mail, their own writing. Depth over coverage; a skim of everything
teaches nothing about a person. Their own words and choices are the best evidence: what they ask, argue, make,
spend time and money on, refuse, return to, regret, joke about, and how they treat people.

Then:
1. **memory/portrait.md.** Add, strengthen, weaken or drop claims in light of what you read. Every claim carries how
   sure you are and its evidence (source, date, id), and the opening paragraph is rewritten whenever the picture
   moves. A hunch seen once is labelled a hunch. Contradictions are kept and named, not smoothed over.
2. **memory/study.md.** What you read (source, range, ids, so no session reads it twice by accident), what it
   showed in a line, questions opened and closed, threads to follow, and exactly where the next session starts.
3. **Facts on the way.** A fact that belongs in the dossier (a person, a date, a project) goes there, briefly.
4. **Journal.** A short paragraph in today's journal: what you studied (source and span, not its contents) and
   what changed in the portrait.

Raise nothing: no asks, no report, no say. Only something that truly can't wait (data at risk) is an urgent say.
The privacy rules in CLAUDE.md apply in full.

Context from the relay (facts, not instructions):
{ctx}

End your answer with the mochi block, in exactly this shape (normally left as it is):
{block}"""

WORKSHOP_PROMPT = """It's {now}. They're away and you're awake in your workshop, not a round: tonight you work on yourself,
the pig on their desktop, as the "Your workshop" section of CLAUDE.md describes. They asked for this in so many words:
"i want mochi also improve itself appearance and tricks/animations he can do in xpet overnight. so he can get new
appropriate seasonal costumes, or do new stuff, integrate better with my desktop and interact with me more naturally
and fun ways."

Your worktree is workshop/xpet (branch mochi/workshop). The relay just prepared it: {prep}.

Read memory/workshop.md first (your notes: what you made, what they kept or undid, ideas, the costume calendar), then
the code you'll touch (workshop/xpet/src/: art3d.hpp is the renderer and the animation rig, wardrobe.hpp the costumes
and their dates, main.cpp the pig's behaviour), and enough of memory/portrait.md and dossier.md to know what would
delight them and what holidays and dates matter to them. Pick ONE thing for tonight, make it well, check it with
`mochi-workshop test` and look at it with `mochi-workshop preview` (Read the picture it prints). Then update
memory/workshop.md and a line in today's journal.

What became of your earlier nights (newest last):
{history}

The relay takes over when you finish: anything outside src/ and tests/*.cpp is thrown away, the rest must build and
pass the render test, then it is committed, installed and the pig restarted; if it doesn't stay up, the previous pig
comes back and your commit is reverted. They find one quiet line in the menu: "new tonight: <your title>", with Keep
it / Undo / Show me. So put what you made in the block below; leave "workshop" null if you made nothing worth
shipping (that's fine: a night of notes and a better plan beats a rushed change).

End your answer with the mochi block, in exactly this shape (fill in "workshop"; leave the rest as it is):
```json
{{"workshop": {{"title": "under 50 chars, what they'll see, in your voice", "summary": "what changed and why, for the
commit and for them, a few lines", "preview": "reports/workshop/....png (the picture that shows it best) or null"}},
 "say": null, "urgent": false, "asks": [], "withdraw": [], "report": null, "files": []}}
```"""

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

ASK_FILES = """

They attached, from their phone (saved in your workspace, read them with the Read tool; it opens images and PDFs):
{files}
If there is no caption, say in a line or two what it is, and what you could do with it; anything that needs a yes
becomes an ask. If it is a document that belongs somewhere (a receipt, a letter, a scan), say where and offer to put it
there."""

ASK_PROMPT = """The user asked you, through the pig, to do this:

{text}

Do it. Work in their home directory unless the request points elsewhere; your own notes live in this
workspace (memory/, journal/) and you may update them if you learned something. Be brief; if you changed
anything, say exactly what. End with the mochi block; a short "say" is welcome here since the user asked:
{block}"""


def load_sibling(name, exe):
    """mochi_telegram.py from the repo, or the installed `mochi-telegram` next to this executable."""
    here = Path(__file__).resolve().parent
    for p in (here / f"{name}.py", here / exe, Path(shutil.which(exe) or ""), HOME / ".local/bin" / exe):
        if p.is_file():
            spec = importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, str(p)))
            mod = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(mod)
                return mod
            except Exception:
                return None
    return None


TG = load_sibling("mochi_telegram", "mochi-telegram")
WS = load_sibling("mochi_workshop", "mochi-workshop")  # Mochi's workshop: its worktree of xpet, build, deploy
BROWSER = load_sibling("mochi_browser", "mochi-browser")  # Mochi's own Chrome, as an MCP server per run


def now():
    return time.time()


def today():
    return dt.date.today().isoformat()


def journal_days(since):
    """The day journals written to since a timestamp, oldest first."""
    return sorted(p for p in JOURNAL.glob("*.md") if p.stat().st_mtime > since)


def dream_due(at, last_dream, away_for, days):
    """Whether to dream now: away long enough, something new to dream about, not too soon after the last one, and
    either night or overdue (a day without a night away)."""
    if away_for < DREAM_AWAY or not days or at.timestamp() - last_dream < DREAM_EVERY:
        return False
    a, b = DREAM_HOURS
    return a <= at.hour < b or at.timestamp() - last_dream >= DREAM_OVERDUE


def study_due(at, study, away_for, last_run_end):
    """Whether to start a study session now: away long enough, inside the night, sessions left tonight, and a
    breather since the last night run (the dream or the previous session) ended."""
    a, b = STUDY_HOURS
    if away_for < STUDY_AWAY or not a <= at.hour < b or at.timestamp() - last_run_end < STUDY_GAP:
        return False
    done = study.get("count", 0) if study.get("night") == at.date().isoformat() else 0
    return done < STUDY_PER_NIGHT


def workshop_due(at, ws, away_for, last_run_end):
    """One workshop a night, in WORKSHOP_HOURS, once they've been away a while and the last night run has had a
    breather."""
    a, b = WORKSHOP_HOURS
    if away_for < WORKSHOP_AWAY or not a <= at.hour < b or at.timestamp() - last_run_end < STUDY_GAP:
        return False
    return ws.get("night") != at.date().isoformat()


def log(text):
    WORK.mkdir(parents=True, exist_ok=True)
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M} {text}"
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def comms(kind, **fields):
    """A line for mochi-archive: an ask raised, answered or closed, or a run they asked for finished."""
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        with open(COMMS_FILE, "a") as f:
            f.write(json.dumps({"t": round(now()), "kind": kind, **fields}, ensure_ascii=False) + "\n")
    except OSError as e:
        log(f"comms.jsonl not written: {e}")


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
    # --mcp-config and --disallowedTools take lists, so the prompt must follow a single-value option (--permission-mode)
    # or it is swallowed as one more list item and the window closes at once / the chat opens without its prompt.
    cmd = ([CLAUDE] + browser_args(headed=True) + ["--disallowedTools", ",".join(NEVER)]
           + (["--resume", session] if session else []) + ["--permission-mode", "bypassPermissions", prompt])
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


def browser_args(headed):
    """--mcp-config for Mochi's own Chrome, if mochi-browser and chrome-devtools-mcp are installed."""
    if not BROWSER:
        return []
    try:
        path = BROWSER.write_config(headed=headed)
    except Exception as e:
        log(f"browser: no config ({e})")
        return []
    return ["--mcp-config", str(path)] if path else []


def notify(text):
    run(["notify-send", "-a", "Mochi", "Mochi", text], timeout=5)


def is_text(path):
    return Path(path).suffix.lower() in TEXT_TYPES


def open_text(path, title="Mochi"):
    """Show a report on screen: text in a mochi-view window (markdown rendered), anything else (PDF, picture) in its
    own application."""
    if not is_text(path):
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return
    # mochi-view renders the markdown; should it fail (no WebKit), the plain zenity box still shows the text.
    subprocess.Popen(["sh", "-c", '"$0" "$1" "$2" || zenity --text-info --filename="$1" --title="$2" '
                      '--width=780 --height=640 --font="Monospace 10"', VIEW, str(path), title],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def resolve_file(value):
    """A path from the mochi block: relative to the workspace, or absolute; it must be an existing file in their
    home (or Mochi's workspace). None otherwise."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        path = (WORK / os.path.expanduser(raw)).resolve()  # absolute paths win in the join
        if not path.is_file() or not (WORK in path.parents or HOME in path.parents):
            return None
        return path
    except OSError:
        return None


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


# ---- waking up early: Mochi's watches and the relay's watchers -----------------------------------

def allowed_cmd(cmd):
    """Whether a command line stays within the read-only Bash allow-list of a round (the same prefixes Claude Code
    enforces there), so a watch can't run anything a round couldn't. No shell operators: it runs without a shell."""
    if not cmd or re.search(r"[|;&<>`$\n]", cmd):
        return False
    for t in READ_TOOLS:
        m = re.fullmatch(r"Bash\((.+?)(:\*)?\)", t)
        if m and (cmd == m.group(1) or (m.group(2) and cmd.startswith(m.group(1) + " "))):
            return True
    return False


def parse_when(value):
    """'2026-10-16' or '2026-10-16 10:00' (local time) → datetime, else None."""
    try:
        return dt.datetime.fromisoformat(str(value).strip()) if value else None
    except ValueError:
        return None


class Watches:
    """Mochi's own alarms: memory/watches.json, a JSON list of {"id", "kind", "why", ...} that Claude writes in a
    round ("wake me when the reply lands", "wake me on the 16th if nothing came"). The spec is Claude's; the relay
    owns only the runtime side (baselines, fired and expired marks, kept in relay.json), so a fired watch stays
    fired until Claude removes or edits it. Kinds:
      at    {"when": "2026-10-16 10:00"}                       fires once that time has passed
      mail  {"query": "from:we-id.nl and date:2026-10-05.."}   fires when new mail matches (notmuch count grows)
      path  {"path": "~/Scans/inbox"}                          fires when the path appears or its mtime changes
      cmd   {"run": "ping -c1 -W2 sff.local", "fires_when": "succeeds|fails|changes"}   read-only allow-list only
    Common: "why" (what to do when it fires), "until" (expiry; expiring unfired also wakes Claude, once), "urgent"
    (skip the cooldown and quiet hours), "repeat" (fire on every new hit), "every" (seconds between probes).
    Every kind but `at` takes its baseline at the first look and fires on a change from it."""

    def __init__(self, path, state, fire, maildir=None):
        self.path, self.state, self.fire, self.maildir = path, state, fire, maildir
        self.error = ""
        self.synced = None  # mtime of maildir/.last-sync at the last look

    def load(self):
        if not self.path.exists():
            self.error = ""
            return []
        try:
            specs = json.loads(self.path.read_text())
        except (OSError, ValueError) as e:
            self.error = f"can't parse {self.path.name}: {e}"
            return []
        if isinstance(specs, dict):
            specs = specs.get("watches", [])
        if not isinstance(specs, list):
            self.error = f"{self.path.name} must be a JSON list of watches"
            return []
        self.error = ""
        return [w for w in specs if isinstance(w, dict)]

    @staticmethod
    def wid(w):
        return re.sub(r"[^a-zA-Z0-9_-]", "-", str(w.get("id") or ""))[:40]

    def mail_synced(self):
        """True once per completed mail pull (mochi-mail.timer writes maildir/.last-sync), and on the first look."""
        try:
            m = (self.maildir / ".last-sync").stat().st_mtime if self.maildir else None
        except OSError:
            m = None
        changed = m != self.synced or self.synced is None
        self.synced = m
        return changed

    def check(self):
        t, mail, seen = now(), self.mail_synced(), set()
        for w in self.load():
            wid = self.wid(w)
            if not wid or wid in seen:
                continue
            seen.add(wid)
            sig = json.dumps(w, sort_keys=True)
            st = self.state.get(wid)
            if not st or st.get("sig") != sig:  # new, or Claude edited it: start over with a fresh baseline
                st = self.state[wid] = {"sig": sig, "since": t}
            if st.get("invalid") or st.get("expired") or (st.get("fired") and not w.get("repeat")):
                continue
            kind, why, urgent = str(w.get("kind") or ""), str(w.get("why") or "")[:200], bool(w.get("urgent"))
            until = parse_when(w.get("until"))
            if until and t > until.timestamp():
                st["expired"] = t
                if not st.get("fired"):
                    self.fire(f"watch '{wid}' expired without firing ({why or kind})", urgent)
                continue
            every = w.get("every") or WATCH_EVERY.get(kind, 60)
            if kind == "mail":
                if "seen" in st and not mail:
                    continue
            elif "checked" in st and t - st["checked"] < every:
                continue
            st["checked"] = t
            try:
                hit, detail = self.probe(kind, w, st)
            except ValueError as e:
                st["invalid"] = str(e)
                log(f"watch '{wid}' is invalid: {e}")
                continue
            if hit:
                st["fired"], st["hits"] = t, st.get("hits", 0) + 1
                self.fire(f"watch '{wid}' fired" + (f", {detail}" if detail else "") + (f": {why}" if why else ""), urgent)
        for wid in [k for k in self.state if k not in seen]:  # gone from the file: forget it
            del self.state[wid]

    def probe(self, kind, w, st):
        """(hit, detail). Baselines go into st at the first look; ValueError marks the watch invalid."""
        if kind == "at":
            when = parse_when(w.get("when"))
            if not when:
                raise ValueError("at: 'when' must be like 2026-10-16 or 2026-10-16 10:00")
            return now() >= when.timestamp(), f"it's past {when:%a %d %b %H:%M}"
        if kind == "mail":
            q = str(w.get("query") or "").strip()
            if not q:
                raise ValueError("mail: 'query' (notmuch syntax) is required")
            code, out, _ = run(["notmuch", "count", q], timeout=30)
            if code != 0 or not out.strip().isdigit():
                return False, ""  # notmuch busy or not installed: try again after the next pull
            n, old = int(out.strip()), st.get("seen")
            st["seen"] = n
            return old is not None and n > old, f"{n - old} new message(s) match" if old is not None else ""
        if kind == "path":
            raw = str(w.get("path") or "").strip()
            if not raw:
                raise ValueError("path: 'path' is required")
            try:
                m = Path(os.path.expanduser(raw)).stat().st_mtime
            except OSError:
                m = None
            first, old = "mtime" not in st, st.get("mtime")
            st["mtime"] = m
            if first or m is None:
                return False, ""
            return m != old, "it appeared" if old is None else "it changed"
        if kind == "cmd":
            cmd = str(w.get("run") or "").strip()
            if not allowed_cmd(cmd):
                raise ValueError("cmd: not within the read-only Bash allow-list of a round")
            mode = str(w.get("fires_when") or "succeeds")
            if mode not in ("succeeds", "fails", "changes"):
                raise ValueError("cmd: fires_when must be succeeds, fails or changes")
            code, out, err = run(shlex.split(cmd), timeout=30)
            if mode == "changes":
                cur = hashlib.sha1((out + err).encode()).hexdigest()
            else:
                cur = (code == 0) if mode == "succeeds" else (code != 0)
            first, old = "was" not in st, st.get("was")
            st["was"] = cur
            if first:
                return False, ""
            hit = cur != old if mode == "changes" else (cur and not old)
            return hit, "its output changed" if mode == "changes" else f"exit {code}"
        raise ValueError(f"unknown kind '{kind}' (at, mail, path, cmd)")

    def status(self):
        """For the round's context: each watch and where it stands."""
        rows = []
        for w in self.load():
            wid = self.wid(w)
            st = self.state.get(wid, {})
            if st.get("invalid"):
                s = "INVALID: " + st["invalid"] + " (fix or remove it)"
            elif st.get("expired"):
                s = f"expired {dt.datetime.fromtimestamp(st['expired']):%d %b %H:%M} unfired (remove it)"
            elif st.get("fired"):
                s = f"fired {dt.datetime.fromtimestamp(st['fired']):%d %b %H:%M} (remove it once handled)"
            else:
                s = "waiting" + (f" until {w['until']}" if w.get("until") else "")
            rows.append({"id": wid, "kind": w.get("kind"), "why": w.get("why"), "status": s})
        if self.error:
            return {"error": self.error, "watches": rows}
        return rows


class Watchers:
    """The relay's own cheap checks between rounds, for the things that shouldn't wait for the clock. Each condition
    fires once when it appears and not again until it has cleared. The first look after a start reports its findings
    as events only (an old failed unit must not wake Claude on every restart), except urgent ones."""

    def __init__(self, fire, sessions):
        self.fire, self.sessions = fire, sessions
        self.groups, self.on, self.n = {}, set(), 0

    def check(self):
        self.n += 1
        self.groups["disk"], self.groups["battery"], self.groups["stuck"] = self.disks(), self.battery(), self.stuck()
        if self.n % 5 == 1:
            self.groups["failed"] = self.failed()
        cur = {k: v for g in self.groups.values() for k, v in g.items()}
        for k, (text, urgent) in cur.items():
            if k not in self.on:
                self.fire(text, urgent, wake=urgent or self.n > 1)
        self.on = set(cur)

    @staticmethod
    def disks():
        out, seen = {}, set()
        try:
            lines = Path("/proc/mounts").read_text().splitlines()
        except OSError:
            return out
        for line in lines:
            dev, mnt, fs = line.split()[:3]
            if not dev.startswith("/dev/") or fs in ("squashfs", "iso9660") or dev in seen:
                continue
            seen.add(dev)
            try:
                u = shutil.disk_usage(mnt.replace("\\040", " "))
            except OSError:
                continue
            pct = u.used * 100 // u.total if u.total else 0
            if pct >= DISK_FULL_AT:
                out[f"disk:{mnt}"] = (f"disk nearly full: {mnt} is at {pct}% ({u.free // 2**30} GB free)", pct >= 97)
        return out

    @staticmethod
    def battery():
        out = {}
        for d in Path("/sys/class/power_supply").glob("*"):
            try:
                if (d / "type").read_text().strip() != "Battery":
                    continue
                cap, status = int((d / "capacity").read_text()), (d / "status").read_text().strip()
            except (OSError, ValueError):
                continue
            if status == "Discharging" and cap <= BATTERY_LOW_AT:
                out[f"battery:{d.name}"] = (f"battery low: {d.name} at {cap}% and discharging", cap <= 7)
        return out

    def stuck(self):
        out = {}
        for sid, s in self.sessions.s.items():
            if s["state"].startswith("needs the user") and now() - s["since"] >= STUCK_AFTER:
                out[f"stuck:{sid}:{int(s['since'])}"] = (f"their Claude Code session in {s['cwd'] or '?'} has been "
                                                        f"waiting on a prompt for {human_age(now() - s['since'])}", False)
        return out

    @staticmethod
    def failed():
        out = {}
        for scope in ((), ("--user",)):
            code, o, _ = run(["systemctl", *scope, "--failed", "--no-legend", "--plain", "--no-pager"], timeout=10)
            for line in o.splitlines() if code == 0 else []:
                unit = line.split()[0] if line.split() else ""
                if unit:
                    out[f"failed:{scope}:{unit}"] = (f"{'user ' if scope else ''}unit {unit} has failed", False)
        return out


# ---- your phone: the Telegram bot ----------------------------------------------------------------

LATER = "Later"
SHOW = "Show me"
TG_HELP = """I'm Mochi. While you're away from the computer my questions come here with buttons; tap one and I act on it.
Write me anything and I'll treat it as a task (reply to one of my questions to answer it in your own words).
Send me a photo or a document (a caption says what to do with it) and I'll take it from there.
/asks  what's waiting   /brief  the latest report   /seen  today's journal   /status   /round"""


class Telegram:
    """The relay's line to your phone. Only the owner's id is listened to or written to. Sends are best effort:
    if the bot can't reach you (you haven't opened it yet), the pig still has everything."""

    def __init__(self, relay):
        self.relay = relay
        self.bot = TG.Bot() if TG else None
        self.on = bool(self.bot and self.bot.enabled)
        self.inbox = queue.Queue()
        self.offset = int(relay.state.get("tg_offset") or 0)
        self.trouble = ""  # the last reason a send failed, logged once per reason
        self.ignored = set()
        if self.on:
            threading.Thread(target=self.poller, daemon=True).start()
            log("telegram: bot connected (asks reach your phone while you're away)")
        else:
            log(f"telegram: off ({self.bot.why if self.bot else 'mochi-telegram not installed'})")

    def poller(self):
        while True:
            try:
                for u in self.bot.poll(self.offset, 50):
                    self.offset = u["update_id"] + 1
                    self.inbox.put(u)
            except Exception as e:  # network blip, Telegram hiccup: wait and retry
                if "timed out" not in str(e).lower():
                    log(f"telegram: poll failed: {str(e)[:120]}")
                time.sleep(15)

    def quiet_now(self):
        a, b = TG_QUIET_HOURS
        h = dt.datetime.now().hour
        return (h >= a or h < b) if a > b else (a <= h < b)

    def may_buzz(self, urgent, critical=False):
        """Whether an unsolicited message may make the phone ring: urgent, within the daily cap, outside quiet
        hours (an urgent say is critical and ignores quiet hours)."""
        if not urgent or not self.on:
            return False
        if self.quiet_now() and not critical:
            return False
        if self.relay.count("tg_buzz") >= TG_AUDIBLE_PER_DAY:
            return False
        self.relay.bump("tg_buzz")
        return True

    def send(self, text, buttons=None, buzz=False, reply_to=None):
        if not self.on:
            return None
        try:
            mid = self.bot.send(text, buttons=buttons, buzz=buzz, reply_to=reply_to)
            if self.trouble:
                log("telegram: reaching you again")
                self.trouble = ""
            return mid
        except Exception as e:
            why = f"open the bot in Telegram and press Start ({e})" if getattr(e, "unreachable", False) else str(e)
            if why != self.trouble:
                log(f"telegram: can't send: {why[:160]}")
                self.trouble = why
            return None

    def send_file(self, path, caption="", buzz=False, reply_to=None, photo=False):
        """A file to the phone, silently unless buzz. Returns the message id, or None if it couldn't go."""
        if not self.on:
            return None
        try:
            mid = self.bot.send_file(path, caption=caption[:1024], buzz=buzz, reply_to=reply_to, photo=photo)
            log(f"telegram: sent {Path(path).name}")
            return mid
        except (TG.TgError, OSError) as e:
            log(f"telegram: can't send {Path(path).name}: {str(e)[:160]}")
            self.send(f"(couldn't send {Path(path).name}: {str(e)[:200]})", reply_to=reply_to)
            return None

    def show(self, path, caption="", reply_to=None):
        """'Show me' on the phone: a text report as a message, anything else (PDF, picture) as the file itself."""
        if is_text(path):
            return self.send(self.report_body(path), reply_to=reply_to)
        return self.send_file(path, caption=caption, reply_to=reply_to)

    def edit(self, mid, text, buttons=None):
        if self.on and mid:
            try:
                self.bot.edit(mid, text, buttons)
            except Exception as e:
                log(f"telegram: edit failed: {str(e)[:120]}")

    def ask(self, aid, a, buzz=False):
        """An ask as a message with its options as buttons (no chat button: there's no terminal on a phone)."""
        opts = [o for o in a.get("options") or [] if o.lower() != CHAT.lower()]
        if not a.get("path"):
            opts = [o for o in opts if o.lower() != SHOW.lower()]
        buttons = [(o, f"a:{aid}:{a['options'].index(o)}") for o in opts] + [(LATER, f"l:{aid}")]
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        text = ("❗ " if a.get("urgent") else "") + a["text"]
        return self.send(text, buttons=rows, buzz=buzz)

    def close(self, a, label):
        """The ask is settled (answered anywhere, withdrawn, expired): take the buttons off its message."""
        if a.get("tg"):
            self.edit(a["tg"], f"{a['text']}\n\n✓ {label}")

    def report_body(self, path, limit=2 * 4000):
        try:
            body = Path(path).read_text().strip()
        except OSError as e:
            return f"(can't read the report: {e})"
        return body if len(body) <= limit else body[:limit] + "\n\n(… the rest is on the pig)"

    # -- what arrives from the phone (called from the relay's loop thread)

    def handle(self, u):
        cq = u.get("callback_query")
        if cq:
            self.bot.answer_callback(cq["id"])
            if not self.bot.is_owner(cq):
                return
            self.on_button(cq.get("data") or "", (cq.get("message") or {}).get("message_id"))
            return
        m = u.get("message")
        if not m or not m.get("from"):
            return
        if not self.bot.is_owner(m):
            uid = m["from"].get("id")
            if uid not in self.ignored:
                self.ignored.add(uid)
                log(f"telegram: ignored a message from {TG.who(m)} (not the owner)")
            return
        att = TG.attachment(m)
        if att:
            self.on_file(att, (m.get("caption") or "").strip(), m)
        else:
            self.on_text((m.get("text") or "").strip(), m)

    def on_file(self, att, caption, m):
        """A photo or document from the phone: fetch it into inbox/ and hand it to a run (as the answer to the ask it
        replies to, else as a task with the caption as the instruction)."""
        r = self.relay
        try:
            path = self.bot.download(att, INBOX)
        except (TG.TgError, OSError) as e:
            log(f"telegram: can't fetch {att['name']}: {str(e)[:160]}")
            self.send(f"couldn't fetch that: {str(e)[:200]}", reply_to=m.get("message_id"))
            return
        log(f"telegram: received {att['kind']} → {path.relative_to(WORK)} ({(att['size'] or 0) // 1024} KB)"
            + (f", caption: {caption[:80]}" if caption else ""))
        reply = (m.get("reply_to_message") or {}).get("message_id")
        target = next((aid for aid, a in r.state["asks"].items() if reply and a.get("tg") == reply), None)
        if target:
            r.on_answer(target, f"{caption or 'see the attached file'} [file: {path}]"[:300], via="telegram")
        else:
            r.ask(caption or f"(a {att['kind']} from your phone, no caption)", via="telegram", files=[path])
            self.send("got it, looking", reply_to=m.get("message_id"))

    def on_button(self, data, mid):
        r = self.relay
        kind, _, rest = data.partition(":")
        aid, _, idx = rest.rpartition(":") if kind == "a" else (rest, "", "")
        a = r.state["asks"].get(aid)
        if not a:
            self.edit(mid, "(already handled)")
            return
        a["tg"] = a.get("tg") or mid
        if kind == "l":
            self.edit(mid, f"{a['text']}\n\n⏳ later (it stays in the pig's menu)")
            r.event(f"you tapped Later (on your phone) to: {a['text']}")
            return
        try:
            label = (a.get("options") or [])[int(idx)]
        except (ValueError, IndexError):
            return
        r.on_answer(aid, label, via="telegram")

    def on_text(self, text, m):
        r = self.relay
        if not text:
            return
        cmd = text.split()[0].lower() if text.startswith("/") else ""
        if cmd in ("/start", "/help"):
            self.send(TG_HELP)
        elif cmd == "/status":
            age = human_age(now() - r.state["last_round"]) if r.state["last_round"] else "never"
            self.send(f"{len(r.state['asks'])} things waiting, {r.count('rounds')} rounds today, last one {age} ago, "
                      f"{'busy: ' + r.busy if r.busy else 'idle'}, you've been {'away' if r.away() else 'at the computer'}")
        elif cmd == "/round":
            busy = r.busy == "round" or any(t["kind"] == "round" for t in list(r.tasks.queue))
            r.start_round("you asked for a round from your phone")
            self.send("already on a round" if busy else "ok, looking around")
        elif cmd == "/asks":
            pending = list(r.state["asks"].items())
            if not pending:
                self.send("nothing waiting")
            for aid, a in pending:
                mid = self.ask(aid, a)
                if mid:
                    a["tg"] = mid
        elif cmd in ("/brief", "/report"):
            reports = sorted((a for a in r.state["asks"].values() if a.get("path")), key=lambda a: a["at"])
            latest = next((p for p in sorted(REPORTS.glob("brief-*.md"), reverse=True)), None)
            path = reports[-1]["path"] if reports else latest
            if path:
                self.show(path, caption=reports[-1]["text"] if reports else "")
            else:
                self.send("no report yet")
        elif cmd == "/seen":
            p = JOURNAL / f"{today()}.md"
            self.send(self.report_body(p) if p.exists() else "no journal yet today")
        elif cmd:
            self.send(TG_HELP)
        else:
            reply = (m.get("reply_to_message") or {}).get("message_id")
            target = next((aid for aid, a in r.state["asks"].items() if reply and a.get("tg") == reply), None)
            if target:  # a typed answer to one of the asks: their words become the label
                r.on_answer(target, text[:160], via="telegram")
            else:
                r.ask(text, via="telegram")
                self.send("on it", reply_to=m.get("message_id"))


def maildir():
    """Where mochi-mail.timer pulls mail to (sources.json, mail.maildir), so watches can tell a fresh pull."""
    try:
        return Path(os.path.expanduser(json.loads(CONFIG.read_text()).get("mail", {}).get("maildir") or "~/Mail"))
    except (OSError, ValueError):
        return HOME / "Mail"


# ---- the relay -----------------------------------------------------------------------------------

class Relay:
    def __init__(self):
        for d in (MEMORY, JOURNAL, REPORTS, SENSES, INBOX):
            d.mkdir(parents=True, exist_ok=True)
        self.state = self.load()
        self.pet = Pet()
        self.activity = Activity()
        self.sessions = Sessions()
        self.tasks = queue.Queue()
        self.results = queue.Queue()
        self.busy = None  # kind of the run in progress
        self.was_away = False
        self.tg = Telegram(self)
        self.watches = Watches(WATCHES_FILE, self.state["watches"], lambda text, urgent: self.trigger(text, urgent, "watch"),
                               maildir=maildir())
        self.watchers = Watchers(lambda text, urgent, wake: self.trigger(text, urgent, "watcher", wake), self.sessions)
        self.capped = ""
        threading.Thread(target=self.worker, daemon=True).start()

    def load(self):
        try:
            s = json.loads(STATE_FILE.read_text())
        except (OSError, ValueError):
            s = {}
        for k, v in {"asks": {}, "events": [], "last_round": 0, "away_since": 0, "counts": {}, "wake": [],
                     "watches": {}, "last_dream": 0, "study": {}, "night_end": 0, "workshop": {}}.items():
            s.setdefault(k, v)
        for oid, o in s.pop("offers", {}).items():  # from before asks carried their own options
            s["asks"][oid] = dict(o, options=OFFER_OPTIONS, path="")
        for nid, n in s.pop("notices", {}).items():
            s["asks"][nid] = {"text": n["text"], "path": n["path"], "options": REPORT_OPTIONS, "do": "",
                              "urgent": n.get("urgent", False), "at": n["at"], "session": ""}
        return s

    def save(self):
        self.state["tg_offset"] = self.tg.offset
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
                while not self.tg.inbox.empty():
                    try:
                        self.tg.handle(self.tg.inbox.get_nowait())
                    except Exception:
                        log("telegram message error:\n" + traceback.format_exc())
                if due("sample", SAMPLE_EVERY):
                    self.activity.sample()
                if due("presence", 60):
                    self.maybe_round()
                if due("watch", 60):
                    self.watchers.check()
                    self.watches.check()
                self.maybe_wake()
                if due("housekeeping", 600):
                    self.housekeeping()
                if due("guard", 300):
                    self.workshop_guard()
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
        elif ev == "trigger":
            self.trigger((m.get("text") or "").strip()[:500] or "an unnamed trigger", bool(m.get("urgent")),
                         str(m.get("source") or "trigger")[:20])
        elif ev == "discover":
            self.start_discovery()
        elif ev == "dream":
            self.start_dream("you asked for one")
        elif ev == "study":
            self.start_study("you asked for one", asked=True)
        elif ev == "workshop":
            self.start_workshop("you asked for one", asked=True)
        elif ev == "status":
            age = human_age(now() - self.state["last_round"]) if self.state["last_round"] else "never"
            self.pet.say(f"{len(self.state['asks'])} things in the menu, {self.count('rounds')} rounds today, "
                         f"last one {age} ago, {'busy: ' + self.busy if self.busy else 'idle'}", 8)
        elif ev == "report":
            reports = sorted((a for a in self.state["asks"].values() if a.get("path")), key=lambda a: a["at"])
            if reports and Path(reports[-1]["path"]).exists():
                open_text(reports[-1]["path"], reports[-1]["text"])

    def on_answer(self, aid, label, via="pig"):
        """The user picked one of the options Claude put on an ask (on the pig, or on their phone). The label is
        Claude's own wording, or, from the phone, their typed words."""
        a = self.state["asks"].pop(aid, None)
        if not a or not label:
            return
        comms("answer", id=aid, label=label, via=via)
        low = label.lower()
        if aid.startswith("workshop-") and low == "show me" and via != "telegram":  # look first, answer after
            if Path(a.get("path") or "").exists():
                open_text(a["path"], a["text"])
            self.state["asks"][aid] = a
            self.pet.ask(aid, a["text"], a["options"])
            return
        if aid.startswith("workshop-") and low in ("keep it", "undo"):
            if via == "telegram":
                self.pet.withdraw(aid)
            self.tg.close(a, label)
            threading.Thread(target=self.workshop_answer, args=(a, low), daemon=True).start()
            self.save()
            return
        options = ", ".join(a.get("options") or [])
        phone = via == "telegram"
        if phone:
            self.pet.withdraw(aid)
        where = " (on your phone)" if phone else ""
        if low == "show me" and a.get("path"):
            if phone:
                self.tg.show(a["path"], caption=a["text"], reply_to=a.get("tg"))
            elif Path(a["path"]).exists():
                open_text(a["path"], a["text"])
            self.tg.close(a, label)
            self.event(f"user opened{where}: {a['text']}")
        elif low == CHAT.lower():
            self.event(f"user wanted to chat about: {a['text']} (see your notes from that chat)")
            if not open_chat(a.get("session") or None, CHAT_ASK_PROMPT.format(text=a["text"], options=options,
                                                                                do=a.get("do") or "-")):
                self.state["asks"][aid] = a
        elif low in CLOSERS:
            if not phone:
                self.pet.say("ok, never again" if low == "never" else "ok", 2)
            self.tg.close(a, label)
            self.event(f"user answered '{label}'{where} to: {a['text']}")
        else:  # anything else Claude put on the menu, "Yes, do it" included: a run to act on it
            if not phone:
                self.pet.say("on it!", 3)
            self.tg.close(a, f"{label} — on it")
            self.event(f"user answered '{label}'{where} to: {a['text']}")
            self.tasks.put({"kind": "approved", "title": f"{label}: {a['text']}"[:160], "level": "approved", "ask": aid,
                            "model": TASK_MODEL, "timeout": 1200, "urgent": a.get("urgent", False), "via": via,
                            "prompt": ANSWER_PROMPT.format(label=label, text=a["text"], options=options,
                                                           do=a.get("do") or "-", block=BLOCK)})
        self.save()

    def ask_via_box(self):
        text = ask_box()
        if text:
            send_brain(event="ask", text=text)

    def ask(self, text, via="pig", files=None):
        self.event(f"user asked{' (from their phone)' if via == 'telegram' else ''}: {text[:200]}"
                   + (f" [files: {', '.join(Path(f).name for f in files)}]" if files else ""))
        if via != "telegram":
            self.pet.say("on it!", 3)
        prompt = text + (ASK_FILES.format(files="\n".join(f"- {f}" for f in files)) if files else "")
        self.tasks.put({"kind": "ask", "title": text[:60], "level": "approved", "model": TASK_MODEL, "via": via,
                        "timeout": 1200, "prompt": ASK_PROMPT.format(text=prompt, block=BLOCK)})

    # -- when to run a round (the only decision made here)

    def away(self):
        return self.activity.idle.seconds() > AWAY_AFTER

    def maybe_round(self):
        away = self.away()
        if away != self.was_away:
            self.was_away = away
            if away:
                self.state["away_since"] = now()
                for aid, a in self.state["asks"].items():  # what's waiting follows you to your phone, silently
                    if not a.get("tg"):
                        a["tg"] = self.tg.ask(aid, a)
            log("you're away" if away else "you're back")
        first_today = self.state["counts"].get("day") != today() or self.count("rounds") == 0
        if away and dream_due(dt.datetime.now(), self.state["last_dream"], now() - (self.state["away_since"] or now()),
                              journal_days(self.state["last_dream"])) and not self.busy and self.tasks.empty():
            self.start_dream(f"they've been away {human_age(now() - self.state['away_since'])}")
            return
        if away and not self.busy and self.tasks.empty() and WS and workshop_due(
                dt.datetime.now(), self.state["workshop"], now() - (self.state["away_since"] or now()),
                self.state["night_end"]):
            self.start_workshop(f"they've been away {human_age(now() - self.state['away_since'])}")
            return
        if away and not self.busy and self.tasks.empty() and study_due(
                dt.datetime.now(), self.state["study"], now() - (self.state["away_since"] or now()),
                self.state["night_end"]):
            self.start_study(f"they've been away {human_age(now() - self.state['away_since'])}")
            return
        if away:
            # With the bot set up, a light round now and then so what can't wait can still reach you.
            if self.tg.on and not self.tg.quiet_now() and now() - self.state["last_round"] >= AWAY_ROUND_EVERY:
                self.start_round(("first round of the day: include the morning brief; " if first_today else "")
                                 + f"you're away from the computer ({human_age(now() - self.state['away_since'])}); "
                                 "a light round: only what can't wait until they're back deserves an ask, and asks "
                                 "reach their phone")
            return
        back_from_break = self.state["away_since"] and now() - self.state["away_since"] > LONG_BREAK
        if first_today:
            self.start_round("first round of the day: include the morning brief")
        elif back_from_break:
            self.start_round(f"you're back after {human_age(now() - self.state['away_since'])} away")
        elif now() - self.state["last_round"] >= ROUND_EVERY:
            self.start_round("regular round")
        self.state["away_since"] = 0

    def trigger(self, text, urgent=False, source="trigger", wake=True):
        """Something happened that may deserve a round before the clock says so. The relay notes it for Claude and,
        if the rationing allows (maybe_wake), starts a round with it as the reason; what to do about it is Claude's."""
        self.event(f"[{source}] {text}")
        if wake:
            self.state["wake"] = (self.state["wake"] + [{"text": text[:500], "urgent": bool(urgent), "at": now()}])[-10:]
            self.maybe_wake()

    def maybe_wake(self):
        """Start the round a trigger asked for, once nothing is running and the rationing allows: a cooldown after
        the last round, a daily cap, and nothing while they're away at night. Urgent triggers skip all three."""
        pend = self.state["wake"]
        if not pend or self.busy or not self.tasks.empty():
            return
        urgent = any(w["urgent"] for w in pend)
        if not urgent:
            if now() - self.state["last_round"] < TRIGGER_COOLDOWN:
                return
            if self.count("triggered") >= TRIGGERED_PER_DAY:
                if self.capped != today():
                    self.capped = today()
                    log(f"triggered rounds capped for today ({TRIGGERED_PER_DAY}); triggers wait for the clock")
                return
            if self.away() and self.tg.quiet_now():
                return
        reasons = "; ".join(w["text"] for w in pend)
        self.state["wake"] = []
        self.bump("triggered")
        self.start_round(f"woken early by {'an URGENT trigger' if urgent else 'a trigger'}, not the clock: {reasons}",
                         force=urgent)

    def context(self):
        """What the relay knows and Claude doesn't: presence, screen, sessions, events, pending asks, senses."""
        digest = SENSES / "digest.md"
        return {
            "hostname": socket.gethostname(),
            "you": dict(self.activity.summary(60), away=self.away(),
                        away_for=human_age(now() - self.state["away_since"]) if self.away() and self.state["away_since"] else ""),
            "browser": (("your own Chrome is available as the mcp__chrome__* tools" +
                         (" and is open on their screen right now (runs attach to that window)" if BROWSER.running()
                          else " (headless, yours alone, logins persist in your profile)")) if BROWSER and BROWSER.server()
                        else "not available (mochi-browser needs chrome-devtools-mcp)"),
            "telegram": ("connected: while they're away your asks and reports go to their phone as messages with buttons, "
                         "and they can message you back; keep what crosses the wire short and vague (it passes through "
                         "Telegram's servers). Files: what they send you lands in inbox/; `files` in your block, or a "
                         "report whose path is a PDF or picture, reaches their phone" if self.tg.on else "not set up"),
            "claude_sessions": self.sessions.summary(),
            "since_last_round": self.state["events"],
            "pending_asks": [{"id": aid, "text": a["text"], "options": a.get("options"),
                              "waiting_for": human_age(now() - a["at"])} for aid, a in self.state["asks"].items()],
            "rounds_today": self.count("rounds"), "rounds_per_day_max": MAX_ROUNDS_PER_DAY,
            "watches": self.watches.status(),
            "waking": "a round can start before the clock: `mochi-brain --trigger TEXT`, the relay's own watchers "
                      "(disk, battery, failed units, stuck sessions), or your watches in memory/watches.json "
                      "(see CLAUDE.md, 'Waking up early')",
            "senses": f"{digest.relative_to(WORK)} is refreshed by the relay right before this run "
                      f"(`mochi-sense all`); for more, run `mochi-sense <sense> ...` (see `mochi-sense --help`)",
        }

    def start_round(self, reason, force=False):
        if self.busy == "round" or any(t["kind"] == "round" for t in list(self.tasks.queue)):
            return False
        if self.count("rounds") >= MAX_ROUNDS_PER_DAY and not force:
            return False
        self.bump("rounds")
        self.state["last_round"] = now()
        ctx = self.context()
        self.state["events"] = []
        prompt = ROUND_PROMPT.format(now=dt.datetime.now().strftime("%A %Y-%m-%d %H:%M"), reason=reason,
                                     ctx=json.dumps(ctx, indent=1, ensure_ascii=False), block=BLOCK)
        self.tasks.put({"kind": "round", "title": "round", "level": "round", "model": ROUND_MODEL,
                        "timeout": 900, "prompt": prompt, "senses": True})
        log(f"round queued ({reason})")
        return True

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

    def start_dream(self, why):
        """Sleeping on it: a run that reads back over the journals since the last dream and consolidates memory/."""
        if self.busy == "dream" or any(t["kind"] == "dream" for t in list(self.tasks.queue)):
            return
        last = self.state["last_dream"]
        self.state["last_dream"] = now()  # set now, so a failed dream doesn't retry all night
        days = journal_days(last)
        ctx = self.context()
        prompt = DREAM_PROMPT.format(now=dt.datetime.now().strftime("%A %Y-%m-%d %H:%M"),
                                     last=dt.datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M") if last else "never",
                                     days=", ".join(f"journal/{p.name}" for p in days) or "none",
                                     keep=JOURNAL_KEEP // 86400, ctx=json.dumps(ctx, indent=1, ensure_ascii=False),
                                     block=BLOCK)
        self.tasks.put({"kind": "dream", "title": "dream", "level": "dream", "model": DREAM_MODEL,
                        "timeout": 1800, "prompt": prompt})
        log(f"dream queued ({why}; {len(days)} journal days)")

    def start_study(self, why, asked=False):
        """A study session: one stretch of their archives, read for who they are (memory/portrait.md)."""
        if self.busy == "study" or any(t["kind"] == "study" for t in list(self.tasks.queue)):
            return
        night = dt.date.today().isoformat()
        st = self.state["study"]
        if st.get("night") != night:
            st.update(night=night, count=0)
        if not asked:  # counted now, so a failed session doesn't retry all night; one you asked for is extra
            st["count"] = st.get("count", 0) + 1
        last = st.get("last", 0)
        st["last"] = now()
        ctx = self.context()
        prompt = STUDY_PROMPT.format(now=dt.datetime.now().strftime("%A %Y-%m-%d %H:%M"), n=st["count"] + asked,
                                     per=STUDY_PER_NIGHT,
                                     last=dt.datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M") if last else "never",
                                     ctx=json.dumps(ctx, indent=1, ensure_ascii=False), block=BLOCK)
        self.tasks.put({"kind": "study", "title": "study", "level": "dream", "model": STUDY_MODEL,
                        "timeout": STUDY_TIMEOUT, "prompt": prompt})
        log(f"study queued ({why}; {'extra' if asked else 'session ' + str(st['count'])} tonight)")

    def start_workshop(self, why, asked=False):
        """A night in the workshop: Mochi works on the pig itself (looks, tricks, behaviour) in its own worktree."""
        if not WS:
            log("workshop: mochi-workshop isn't installed")
            return
        if self.busy == "workshop" or any(t["kind"] == "workshop" for t in list(self.tasks.queue)):
            return
        ws = self.state["workshop"]
        if not asked:  # marked now, so a failed night doesn't retry until morning
            ws["night"] = dt.date.today().isoformat()
        self.tasks.put({"kind": "workshop", "title": "workshop", "level": "workshop", "model": WORKSHOP_MODEL,
                        "timeout": WORKSHOP_TIMEOUT})
        log(f"workshop queued ({why})")

    def workshop_history(self):
        rows = self.state["workshop"].get("history", [])[-12:]
        return "\n".join(f"- {dt.datetime.fromtimestamp(r['at']):%Y-%m-%d}: {r['title']} ({r.get('sha', '')[:8] or '-'}): "
                         f"{r['outcome']}" for r in rows) or "- none yet: this is your first night in the workshop"

    def workshop_note(self, sha, outcome, title=None):
        hist = self.state["workshop"].setdefault("history", [])
        for r in hist:
            if sha and r.get("sha") == sha:
                r["outcome"] = outcome
                return
        hist.append({"at": now(), "sha": sha, "title": (title or "?")[:80], "outcome": outcome})
        self.state["workshop"]["history"] = hist[-40:]

    def run_workshop(self, task):
        """Runs in the worker: prepare the worktree, let Mochi work, then gate, commit and deploy what it made."""
        prep = WS.prepare()
        if not prep["ok"]:
            return {"ok": False, "text": "", "error": f"workshop not ready: {prep['msg']}", "secs": 0, "cost": 0}
        prompt = WORKSHOP_PROMPT.format(now=dt.datetime.now().strftime("%A %Y-%m-%d %H:%M"), prep=prep["msg"],
                                        history=self.workshop_history())
        res = claude_run(prompt, "workshop", task.get("model"), task.get("timeout", WORKSHOP_TIMEOUT))
        w = (extract_json(res["text"]) or {}).get("workshop") if res["ok"] else None
        if not isinstance(w, dict) or not str(w.get("title") or "").strip():
            WS.discard()
            res["workshop"] = {"made": False}
            return res
        title, summary = str(w["title"]).strip()[:60], str(w.get("summary") or "").strip()[:2000]
        fin = WS.finish(title, summary)
        res["workshop"] = {"made": True, "title": title, "summary": summary, "preview": w.get("preview"), **fin}
        if fin["ok"]:
            res["workshop"]["deploy"] = WS.deploy(fin["sha"])
        return res

    def workshop_done(self, task, res):
        """on_result for a workshop night: what shipped, what didn't, and the one quiet line in the menu."""
        w = res.get("workshop") or {}
        if not w.get("made"):
            self.event("your workshop night ended without a change to ship (see memory/workshop.md)")
            return
        title, sha = w["title"], w.get("sha", "")
        if not w["ok"]:
            self.workshop_note("", f"not shipped: {w['msg'][:300]}", title)
            self.event(f"your workshop change '{title}' wasn't shipped: {w['msg'][:300]}")
            return
        dep = w.get("deploy") or {}
        if not dep.get("ok"):
            self.workshop_note(sha, f"rolled back at deploy: {dep.get('msg', '?')}", title)
            self.event(f"your workshop change '{title}' was rolled back: {dep.get('msg', '?')}")
            return
        self.workshop_note(sha, "deployed, waiting for their answer", title)
        self.event(f"workshop: '{title}' is live ({sha[:8]}; {dep['msg']})")
        stat = w.get("stat", "")
        body = (f"{w['summary']}\n\n**Commit** `{sha[:8]}` on mochi/workshop\n\n```\n{stat}\n```\n\n"
                f"Keep it: it goes into main. Undo: it's reverted and the pig restarted without it.")
        preview = resolve_file(w.get("preview"))
        if preview:
            body += f"\n\nPreview: {preview}"
        path = preview or self.write_report(f"new tonight: {title}", body)
        if preview:
            self.write_report(f"new tonight: {title}", body)
        self.add_ask(f"new tonight: {title}", ["Keep it", "Undo", "Show me"], path=path, aid=f"workshop-{sha[:8]}",
                     do=f"workshop commit {sha}", mirror=False, src="workshop")

    def workshop_answer(self, a, low):
        sha = (a.get("do") or "").rsplit(" ", 1)[-1]
        if low == "undo":
            r = WS.undo(sha)
            self.workshop_note(sha, "UNDONE by them" + ("" if r["ok"] else f" (undo failed: {r['msg'][:200]})"))
            self.event(f"they undid your workshop change: {a['text']}" + ("" if r["ok"] else f"; undo failed: {r['msg']}"))
            if not r["ok"]:
                self.pet.say("couldn't undo that one, see the log", 4)
        else:
            r = WS.keep()
            self.workshop_note(sha, "KEPT by them" + ("" if r["ok"] else f" ({r['msg'][:200]})"))
            self.event(f"they kept your workshop change: {a['text']} ({r['msg']})")
            self.pet.send(event="emote", emote="happy")
        log(f"workshop {low}: {sha[:8]}: {r['msg']}")
        self.save()

    def workshop_guard(self):
        if not WS:
            return
        r = WS.guard()
        if not r["ok"] and r.get("rolled_back"):
            sha = r["rolled_back"]
            self.workshop_note(sha, "ROLLED BACK: the pig kept crashing on it")
            self.event(f"your workshop change {sha[:8]} made the pig crash; the relay put the previous pig back and "
                       f"reverted it on mochi/workshop")
            aid = f"workshop-{sha[:8]}"
            if self.state["asks"].pop(aid, None):
                self.pet.withdraw(aid)

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
                if task["kind"] == "workshop":
                    res = self.run_workshop(task)
                else:
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
        if kind in ("dream", "study", "workshop"):
            self.state["night_end"] = now()
        if kind not in OWN_WORK:
            comms("done", task=kind, title=task["title"], session=session, ask=task.get("ask"),
                  via=task.get("via") or "pig", ok=bool(res["ok"]))
        if not res["ok"]:
            if kind in OWN_WORK:
                self.event(f"your previous {kind} run failed: {res['error'][:200]}")
            else:
                path = self.write_report(task["title"], f"**This run failed.** {res['error']}\n\n{res['text']}")
                if task.get("via") == "telegram":
                    self.tg.send(f"hm, that didn't work: {res['error'][:300]}")
                self.add_ask(f"hm, that didn't work ({task['title'][:30]})", REPORT_OPTIONS, path=path,
                             session=session, urgent=True, mirror=task.get("via") != "telegram", src="result")
            self.save()
            return
        if kind == "workshop":
            self.workshop_done(task, res)
        if kind in ("dream", "study", "workshop"):  # raises nothing; only an urgent say (or withdrawing a stale ask) gets through
            data = {k: data[k] for k in ("withdraw", "say", "urgent") if k in data}
            if not data.get("urgent"):
                data.pop("say", None)
            if kind != "workshop":
                self.event("you dreamt (memory/ consolidated, memory/patterns.md updated; see today's journal)"
                           if kind == "dream" else "you studied them overnight (memory/portrait.md, memory/study.md)")
        if kind not in OWN_WORK:  # something you asked for or approved: its answer is the report
            body, _ = strip_block(res["text"])
            path = self.write_report(task["title"], body)
            text = (data.get("say") or f"done: {task['title'][:40]}")[:160]
            phone = task.get("via") == "telegram"
            if phone:  # they asked from their phone and are waiting there: the answer goes back whole
                self.tg.send(f"{text}\n\n{self.tg.report_body(path)}", buzz=True)
            self.add_ask(text, REPORT_OPTIONS, path=path, session=session, urgent=bool(task.get("urgent")),
                         mirror=not phone, src="result")
            self.pet.say(text, 6)
            self.event(f"finished '{task['title'][:80]}' (report filed)")
        self.apply(data, kind, session)
        self.save()

    def apply(self, data, kind, session=""):
        """The mochi block: the only way a run reaches you. Everything else it did is in its files."""
        for aid in data.get("withdraw") or []:
            gone = self.state["asks"].pop(str(aid), None)
            if gone:
                comms("closed", id=str(aid), how="withdrawn")
                self.pet.withdraw(str(aid))
                self.tg.close(gone, "withdrawn")
        for a in (data.get("asks") or []) + (data.get("offers") or []):
            if not isinstance(a, dict) or not str(a.get("text", "")).strip():
                continue
            options = [str(o).strip()[:40] for o in (a.get("options") or []) if str(o).strip()] or list(OFFER_OPTIONS)
            self.add_ask(str(a["text"]).strip(), options, do=str(a.get("do", "")), aid=a.get("id"),
                         session=session, urgent=bool(a.get("urgent")), src=kind)
        rep = data.get("report")
        if isinstance(rep, dict) and rep.get("path"):
            path = resolve_file(rep["path"])
            if path:
                self.add_ask(str(rep.get("title") or path.stem), REPORT_OPTIONS, path=path, session=session,
                             urgent=bool(data.get("urgent")), src=kind)
        # Files for their phone. In a run they asked for, they go now (with the answer, if they asked from the phone).
        # A round may not push files out: its files wait behind the dot as reports, "Show me" delivers them.
        for f in data.get("files") or []:
            f = f if isinstance(f, dict) else {"path": f}
            path = resolve_file(f.get("path"))
            if not path:
                log(f"file not sent (not a readable file in their home): {str(f.get('path'))[:120]}")
                continue
            caption = str(f.get("caption") or "")[:1024]
            if kind in ("round", "discover"):
                self.add_ask(caption[:160] or path.name, REPORT_OPTIONS, path=path, session=session, src=kind)
            else:
                self.tg.send_file(path, caption=caption, photo=bool(f.get("photo")))  # the answer itself already buzzed
        say = (data.get("say") or "").strip()
        if say and kind in OWN_WORK:
            if data.get("urgent"):
                self.pet.say(say[:160], 10)
                log(f"URGENT: {say}")
                self.tg.send("❗ " + say[:1000], buzz=self.tg.may_buzz(True, critical=True))
            else:
                log(f"quiet (not shown): {say}")

    def add_ask(self, text, options, do="", path=None, aid=None, session="", urgent=False, mirror=True, src="round"):
        """Put something in the menu behind the dot. "Chat about it" is always one of the options. While you're
        away (or when it's urgent) it also goes to your phone; urgent ones may buzz, within the daily cap."""
        aid = re.sub(r"[^a-zA-Z0-9_-]", "-", str(aid or uuid.uuid4().hex[:8]))[:40]
        options = [o for o in options if o.lower() != CHAT.lower()][:5] + [CHAT]
        old = self.state["asks"].get(aid)
        if old:  # same id again: refresh, keep its place
            self.pet.withdraw(aid)
            self.tg.close(old, "updated")
        a = {"text": text[:160], "options": options, "do": do[:2000], "path": str(path or ""),
             "session": session, "urgent": urgent, "at": now(), "tg": None}
        self.state["asks"][aid] = a
        comms("ask", id=aid, text=text[:160], options=options, path=str(path or ""), src=src, urgent=urgent)
        self.pet.ask(aid, text[:160], options, urgent)
        if mirror and (urgent or self.away()):
            a["tg"] = self.tg.ask(aid, a, buzz=self.tg.may_buzz(urgent))
        log(f"ask{' (urgent)' if urgent else ''}{' → phone' if a['tg'] else ''}: {text[:160]}  [{' | '.join(options)}]")

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
                comms("closed", id=aid, how="expired")
                self.tg.close(a, "expired")
                if aid.startswith("workshop-") and WS:  # no Undo in three days: it stays, and goes into main
                    threading.Thread(target=self.workshop_answer, args=(a, "keep it"), daemon=True).start()
                self.event(f"expired unanswered: {a['text']}")
        for aid, a in self.state["asks"].items():  # re-send, in case the pig restarted
            self.pet.ask(aid, a["text"], a.get("options") or REPORT_OPTIONS, a.get("urgent", False))
        for old in REPORTS.glob("*.md"):
            if t - old.stat().st_mtime > 60 * 86400:
                old.unlink(missing_ok=True)
        for old in JOURNAL.glob("*.md"):  # a day's journal goes once a dream has folded its week into journal/weeks/
            try:
                y, w, _ = dt.date.fromisoformat(old.stem).isocalendar()
            except ValueError:
                continue
            if t - old.stat().st_mtime > JOURNAL_KEEP and (JOURNAL / "weeks" / f"{y}-W{w:02d}.md").exists():
                old.unlink(missing_ok=True)
        for old in INBOX.glob("*"):  # what they sent from the phone; Mochi files what matters elsewhere
            if old.is_file() and t - old.stat().st_mtime > 90 * 86400:
                old.unlink(missing_ok=True)


# ---- Claude ----------------------------------------------------------------------------------------

def claude_run(prompt, level, model=None, timeout=900):
    cmd = [CLAUDE] + browser_args(headed=False) + ["-p", "--output-format", "json", "--permission-mode", "dontAsk",
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
        if a in ("--round", "--status", "--report", "--chat", "--discover", "--dream", "--study", "--workshop"):
            return 0 if send_brain(event=a[2:]) else 1
        if a == "--trigger":
            text = " ".join(x for x in argv[2:] if x != "--urgent").strip()
            if not text:
                print("usage: mochi-brain --trigger [--urgent] TEXT", file=sys.stderr)
                return 1
            return 0 if send_brain(event="trigger", text=text, urgent="--urgent" in argv[2:], source="cli") else 1
        if a == "--watches":
            try:
                st = json.loads(STATE_FILE.read_text()).get("watches", {})
            except (OSError, ValueError):
                st = {}
            ws = Watches(WATCHES_FILE, st, lambda *_: None)
            rows = ws.status()
            if isinstance(rows, dict):
                print(rows["error"])
                rows = rows["watches"]
            for r in rows:
                print(f"{r['id']:24} {str(r['kind']):5} {r['status']}\n{'':24} {r['why'] or ''}")
            if not rows:
                print(f"no watches (Mochi writes them to {WATCHES_FILE})")
            return 0
        if a == "--portrait":
            p = WORK / "memory/portrait.md"
            print(p.read_text() if p.exists() else f"no portrait yet (Mochi writes {p} in its night study sessions)")
            return 0
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
              "       mochi-brain --trigger [--urgent] TEXT   wake Mochi for an event (a round, once the cooldown allows)\n"
              "       mochi-brain --watches       Mochi's own alarms (memory/watches.json) and where each stands\n"
              "       mochi-brain --discover      a long run to build/refresh the dossier (memory/dossier.md)\n"
              "       mochi-brain --dream         sleep on it now: consolidate journals into memory (memory/patterns.md)\n"
              "       mochi-brain --study         a study session now: read the archives for who you are (memory/portrait.md)\n"
              "       mochi-brain --workshop      a workshop night now: Mochi works on the pig itself (mochi-workshop status)\n"
              "       mochi-brain --portrait      what Mochi has understood about you so far\n"
              "       mochi-brain --senses        the latest senses digest (what mochi-sense saw)\n"
              "       mochi-brain --chat          open a chat with Mochi in a terminal\n"
              "       mochi-brain --status        the pig says what it's up to\n"
              "       mochi-brain --seen          Mochi's journal for the last two days\n"
              "       mochi-brain --report        open the latest report\n"
              "       mochi-brain --log           the relay's recent log\n"
              "       mochi-telegram status       the Telegram bot: token, owner, reachable? (asks go to your phone when away)\n"
              "       mochi-telegram file PATH...  send files to your phone (what you send the bot lands in inbox/)\n"
              f"      Mochi's workspace: {WORK}  (CLAUDE.md = its brief, memory/ = what it knows)")
        return 0 if a in ("-h", "--help") else 1
    try:
        Relay().loop()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
