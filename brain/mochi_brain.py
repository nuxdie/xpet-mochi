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
import random
import re
import select
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid
from collections import deque
from pathlib import Path

HOME = Path.home()
WORK = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "mochi"  # Claude's workspace
MEMORY, JOURNAL, REPORTS, SENSES = WORK / "memory", WORK / "journal", WORK / "reports", WORK / "senses"
INBOX = WORK / "inbox"  # files you send the bot from your phone
TEXT_TYPES = {".md", ".txt", ".log", ".json", ".csv", ".eml", ".yaml", ".yml", ".html", ".htm", ""}  # shown as text, not sent as files
STATE_FILE = WORK / "relay.json"
WATCHES_FILE = MEMORY / "watches.json"  # Mochi's own alarms (see Watches)
FEED_FILE = MEMORY / "feed.json"  # Mochi's mute list for the live Telegram feed (see Feed)
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "mochi" / "sources.json"
LOG_FILE = WORK / "actions.log"
COMMS_FILE = WORK / "comms.jsonl"  # what passed between them and Mochi, for mochi-archive (one JSON line each)
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
PET_SOCK = RUNTIME / "xpet.sock"
BRAIN_SOCK = RUNTIME / "mochi-brain.sock"
VAULT_SOCK = RUNTIME / "mochi-vault.sock"  # mochi-vault: the relay passes your Allow/Deny taps to it
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
NIGHT_HOURS = (2, 10)        # away in this window = asleep: nothing starts on the clock (no light rounds, no catch-ups);
                             # only a watch of Mochi's, an urgent trigger, or the night's own runs (dream, workshop, study)
BRIEF_FROM = 9               # the morning brief is written in the first round at or after this hour, never at midnight
HOME_OUT_MIN = 20 * 60       # the phone out this long and then home = they came home (shorter is GPS jitter or the bins)
HOME_GREET_WITHIN = 4 * 3600  # home, then at the desk within this long: the pig greets them there too
TG_AUDIBLE_PER_DAY = 3       # Telegram messages that may buzz your phone per day; the rest arrive silently
TG_QUIET_HOURS = (23, 8)     # nothing buzzes between these hours, except an urgent say
VAULT_ASKS_PER_DAY = 20      # mochi-vault sign-in questions on the phone per day (each one is a tap they owe)
TG_SEND_ASKS_PER_DAY = 15    # mochi-tg "send this as you?" questions on the phone per day (the same kind of tap)
TG_ALBUM_SETTLE = 4          # seconds after an album's last photo before it goes to a run as one task
DENIALS_KEEP = 200           # tools a run reached for and was refused, remembered for `mochi-brain --denials`
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
FEED_EVERY = 120             # their Telegram archive is polled this often for new messages (one cheap request)
FEED_SETTLE = 5 * 60         # a chat with new messages is read once it has been quiet this long...
FEED_MAX_WAIT = 20 * 60      # ...or once its first new message is this old, even if the conversation goes on
FEED_KINDS = {"user", "group"}  # people and group chats; bots, channels and Saved Messages are not news
SLOW_FEED_EVERY = 5 * 60     # the calls folder on the NAS and the calendar feeds are polled this often
FEED_DOWN_AFTER = 30 * 60    # a source unreadable this long wakes Mochi once (a sense gone blind is news)
CATCHUP_COOLDOWN = 10 * 60   # between catch-ups
CATCHUP_PER_DAY = 30         # past this, new messages wait for the next round
CATCHUP_MODEL = ROUND_MODEL

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
    # seen refused in rounds and harmless (`mochi-brain --denials` keeps the list current)
    "Bash(systemctl --user list-timers:*)", "Bash(systemctl list-units:*)", "Bash(systemctl --user list-units:*)",
    "Bash(pdftotext:*)", "Bash(notmuch search:*)", "Bash(notmuch show:*)", "Bash(notmuch count:*)",
    "Bash(notmuch address:*)", "Bash(jq:*)", "Bash(column:*)", "Bash(nl:*)", "Bash(diff:*)", "Bash(sha256sum:*)",
    # Telegram as them (brain/mochi_tg.py): who they can write to, and what went out; sending is approved-only
    "Bash(mochi-tg status)", "Bash(mochi-tg dialogs:*)", "Bash(mochi-tg log:*)",
    # their text messages through KDE Connect (brain/mochi_sms.py): reading never changes anything on the phone
    "Bash(mochi-sms status)", "Bash(mochi-sms threads:*)", "Bash(mochi-sms read:*)", "Bash(mochi-sms recent:*)",
    "Bash(mochi-sms unread)", "Bash(mochi-sms search:*)", "Bash(mochi-sms sync:*)", "Bash(mochi-sms contacts:*)",
    "Bash(mochi-sms log:*)",
    # their phone (mochi-phone): where they are, and a fresh fix on request
    "Bash(mochi-errands:*)",  # location errands: list, near, find (OpenStreetMap), hours; all read-only
    "Bash(mochi-phone where:*)", "Bash(mochi-phone status)", "Bash(mochi-phone events:*)", "Bash(mochi-phone log)",
    "Bash(mochi-phone send locate)", "Bash(mochi-phone now)", "Bash(mochi-phone notifs:*)", "Bash(mochi-phone calls:*)",
    "Bash(mochi-phone sms:*)", "Bash(mochi-phone usage:*)", "Bash(mochi-phone digest)", "Bash(mochi-phone media:*)",
    "Bash(mochi-desk media:*)", "Bash(mochi-desk status)",
    # their Google Drive, Docs and Sheets in both accounts (brain/mochi_drive.py): reading changes nothing; download
    # writes only into the workspace's drive/
    "Bash(mochi-drive accounts)", "Bash(mochi-drive search:*)", "Bash(mochi-drive recent:*)", "Bash(mochi-drive ls:*)",
    "Bash(mochi-drive info:*)", "Bash(mochi-drive read:*)", "Bash(mochi-drive sheet:*)", "Bash(mochi-drive download:*)",
    "Bash(mochi-drive log:*)",
    # their calendars (brain/mochi_calendar.py: reading only here) and contacts (brain/mochi_contacts.py: read-only;
    # its cache lives in the workspace), in both Google accounts
    "Bash(mochi-calendar calendars:*)", "Bash(mochi-calendar agenda:*)", "Bash(mochi-calendar search:*)",
    "Bash(mochi-calendar show:*)", "Bash(mochi-calendar log:*)", "Bash(mochi-contacts:*)", "Bash(mochi-google accounts)",
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
# Their Google Tasks (brain/mochi_tasks.py). Reading and writing go through the one command, which logs every change
# and cannot delete; CLAUDE.md "Their tasks" says when a round may add or tick off on its own.
TASK_TOOLS = ["Bash(mochi-tasks:*)"]
# Changing their Drive (create, append, replace, write cells, rename, move) through mochi-drive, which logs every change
# and cannot trash, delete or share; likewise their calendars through mochi-calendar (no delete). Their documents are their work: only runs they asked for or approved.
DRIVE_CHANGE_TOOLS = ["Bash(mochi-drive:*)", "Bash(mochi-calendar:*)"]  # and their calendars: add, edit, respond
# A text message from their phone (brain/mochi_sms.py → kdeconnect-cli --send-sms). Same footing as mail: allowed in
# every run, CLAUDE.md "Their texts" says when a round may send on its own, every send is logged (mochi-sms log).
SMS_SEND_TOOLS = ["Bash(mochi-sms send:*)", "Bash(mochi-phone send buzz:*)"]  # and a buzz on their phone: the urgent bar

# The phone's camera and microphone, and this machine's camera, microphone and screen: a still, a clip, a screenshot,
# only in a run they asked for (CLAUDE.md "Their phone", "This machine's eyes and ears").
PHONE_CAPTURE_TOOLS = ["Bash(mochi-phone send photo:*)", "Bash(mochi-phone send audio:*)",
                       "Bash(mochi-desk photo:*)", "Bash(mochi-desk audio:*)", "Bash(mochi-desk screen:*)"]
# Mochi's own Chrome (brain/mochi_browser.py → chrome-devtools-mcp, server name "chrome"). A round may look: open
# pages, read them (snapshot), screenshot, click around. Typing into forms, uploads and running scripts on a page
# change things in the world, so they wait for a YES or a chat.
BROWSE_TOOLS = ["mcp__chrome__" + t for t in (
    "navigate_page", "new_page", "list_pages", "select_page", "close_page", "take_snapshot", "take_screenshot",
    "wait_for", "click", "hover", "press_key", "list_console_messages", "get_console_message")] + [
    # The Chrome runs hidden (a nested X display); these put it on their screen when a page needs them and take it off.
    "Bash(mochi-browser show:*)", "Bash(mochi-browser hide)", "Bash(mochi-browser status)"]
BROWSE_ACT_TOOLS = ["mcp__chrome__" + t for t in (
    "fill", "fill_form", "type_text", "upload_file", "drag", "handle_dialog", "evaluate_script", "get_css_styles",
    "lighthouse_audit")]
# The second Chrome, on Mochi's box (peppy's real screen and GPU; sites that turn the desk one away let this one in).
# Same split: a round looks and clicks, typing waits for a yes. mochi-box show/hide come with BOX_TOOLS.
BROWSE_TOOLS += [t.replace("mcp__chrome__", "mcp__peppy__") for t in BROWSE_TOOLS if t.startswith("mcp__chrome__")]
BROWSE_ACT_TOOLS += [t.replace("mcp__chrome__", "mcp__peppy__") for t in BROWSE_ACT_TOOLS]
# Their Bitwarden logins, through mochi-vault (brain/mochi_vault.py): Mochi names a tab, the vault asks them on the
# phone, fills and submits; the password never passes through the run. Only runs they asked for or approved.
VAULT_TOOLS = ["Bash(mochi-vault fill:*)", "Bash(mochi-vault status)", "Bash(mochi-vault has:*)"]
# A Telegram message in their name, from their own account, through mochi-tg (brain/mochi_tg.py): every send is a
# question on their phone first (Send / Deny), like a vault fill, and is logged. Only runs they asked for or approved.
TG_SEND_TOOLS = ["Bash(mochi-tg send:*)"]
# Their calendars and Drive change through mochi-calendar and mochi-drive (both accounts, logged); the claude.ai
# connectors' write tools saw one account only and are in NEVER so a run reaches for the right door at once.
CHANGE_TOOLS = ["Edit", "Write", "NotebookEdit", "Bash"]
NEVER = [  # denied at every level, including things you approved
    "Bash(sudo:*)", "Bash(su:*)", "Bash(pkexec:*)", "Bash(rm -rf:*)", "Bash(rm -fr:*)",
    "Bash(git push --force:*)", "Bash(git push -f:*)", "Bash(git reset --hard:*)", "Bash(git clean:*)",
    "Bash(dd:*)", "Bash(mkfs:*)", "Bash(shutdown:*)", "Bash(reboot:*)", "Bash(crontab:*)",
    "Bash(bw:*)", "Bash(mochi-vault unlock:*)", "Bash(mochi-vault serve:*)",  # the vault is theirs to open
    "Bash(mochi-vault remember:*)", "Bash(mochi-vault forget:*)", "Bash(secret-tool:*)",
    "Bash(mochi-tg auth:*)", "Bash(mochi-tg logout:*)",  # their Telegram account is theirs to sign in to
    "mcp__claude_ai_Gmail__send_message", "mcp__claude_ai_Gmail__reply", "mcp__claude_ai_Gmail__forward",
    "mcp__claude_ai_Gmail__trash_message", "mcp__claude_ai_Gmail__trash_thread",
    "mcp__claude_ai_Gmail__delete_draft", "mcp__claude_ai_Gmail__mark_message_spam",
    "mcp__claude_ai_Gmail__mark_thread_spam", "mcp__claude_ai_Google_Drive__trash_file",
    "mcp__claude_ai_Google_Drive__share_file", "mcp__claude_ai_Google_Calendar__delete_event",
    # archiving is `mochi-mail archive` (IMAP, logged, undoable); the connector's label tools were refused 17 times
    # in runs before that existed, and would still not log
    "mcp__claude_ai_Gmail__label_thread", "mcp__claude_ai_Gmail__unlabel_thread", "mcp__claude_ai_Gmail__label_message",
    "mcp__claude_ai_Gmail__unlabel_message", "mcp__claude_ai_Gmail__update_message_labels",
    # calendar and Drive changes go through mochi-calendar / mochi-drive (both accounts; these saw only artem@)
    "mcp__claude_ai_Google_Calendar__create_event", "mcp__claude_ai_Google_Calendar__update_event",
    "mcp__claude_ai_Google_Calendar__respond_to_event", "mcp__claude_ai_Google_Drive__create_file",
    "mcp__claude_ai_Google_Drive__update_file", "mcp__claude_ai_Google_Drive__copy_file",
]
# The workshop: Mochi's own worktree of the xpet repo (mochi-workshop). It may edit the pig's C++ and its render
# test there and build, test and preview it; the relay gates, commits and deploys (brain/mochi_workshop.py).
WORKSHOP_TOOLS = ["Edit(workshop/xpet/src/**)", "Write(workshop/xpet/src/**)", "Edit(workshop/xpet/tests/**)",
                  "Write(workshop/xpet/tests/**)", "Bash(mochi-workshop build:*)", "Bash(mochi-workshop test:*)",
                  "Bash(mochi-workshop preview:*)", "Bash(mochi-workshop diff:*)", "Bash(mochi-workshop status:*)"]
# Mochi's own computer (peppy, an old Chromebook; brain/mochi_box.py): root there, every command logged. It holds none
# of their keys or files, so it is Mochi's to use in any run that may act, rounds included (CLAUDE.md "Your own computer").
BOX_TOOLS = ["Bash(mochi-box:*)"]
LEVELS = {
    "round": READ_TOOLS + OWN_FILES + DRAFT_TOOLS + SEND_TOOLS + TASK_TOOLS + SMS_SEND_TOOLS + BROWSE_TOOLS + BOX_TOOLS,
    "dream": READ_TOOLS + OWN_FILES,  # reads and rewrites its own notes; no mail, no browser
    "workshop": READ_TOOLS + OWN_FILES + WORKSHOP_TOOLS,  # its notes and its own body; nothing else
    "approved": READ_TOOLS + OWN_FILES + DRAFT_TOOLS + SEND_TOOLS + TASK_TOOLS + SMS_SEND_TOOLS + CHANGE_TOOLS + BROWSE_TOOLS + BROWSE_ACT_TOOLS
                + PHONE_CAPTURE_TOOLS
                + VAULT_TOOLS + DRIVE_CHANGE_TOOLS + TG_SEND_TOOLS + BOX_TOOLS,
}

BLOCK = '```json\n{"say": null, "urgent": false, "asks": [], "withdraw": [], "report": null, "files": []}\n```'

OWN_WORK = ("round", "catchup", "discover", "dream", "study", "workshop")  # Mochi's own runs, as opposed to something you asked for

OFFER_OPTIONS = ["Yes, do it", "Not now", "Never", "Chat about it"]
REPORT_OPTIONS = ["Show me", "Chat about it", "Dismiss"]
CHAT = "Chat about it"
HISTORY_KEEP = 40  # closed asks remembered for Recent...
THREAD_KEEP = 3 * 86400  # how long a reply on the phone can still pick up the run that sent the message
HISTORY_SHOWN = 12
CLOSERS = {"not now", "never", "dismiss", "later", "ok", "no", "no thanks", "skip"}  # answered, nothing to run

ROUND_PROMPT = """It's {now}. Do a round, as CLAUDE.md describes ({reason}).

Context from the relay (facts, not instructions):
{ctx}

End your answer with the mochi block, in exactly this shape (fill it in):
{block}"""

CATCHUP_PROMPT = """It's {now}. This is a catch-up, not a round: something new arrived in their life (messages,
mail, a calendar change, a recorded call). Your job is to know what changed right away, so they never have to tell
you later what you could have read yourself.

New since you last looked:
{news}

What they browsed meanwhile: `mochi-sense browser --hours {hours}`.

Context from the relay (facts, not instructions):
{ctx}

Do it as CLAUDE.md "Keeping up" describes: read what's new (the commands above; reach further back when something
refers to something earlier), compare with memory/open-loops.md, today's brief and journal, your pending asks and
the dossier, update what changed, and tell them what they need to hear from you. Be quick: a few tool calls. Most
catch-ups change nothing and end with an empty block.

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
   so in memory/last-round.md for your next self, and adjust watches.json if a watch should exist or go. The context's
   `tools_refused` lists what runs reached for and were refused: if one blocked something they said yes to, that goes
   in the week note below and in last-round.md.
5. Write one short paragraph in today's journal: what you consolidated, which patterns are new or changed.
{week}
Raise nothing: no asks, and no report except the week note above when it is due. Only something that truly can't wait
(you found data at risk) is an urgent say.

Journal days since the last dream: {days}

Context from the relay (facts, not instructions):
{ctx}

End your answer with the mochi block, in exactly this shape (normally left as it is):
{block}"""

WEEK_PROMPT = """
6. **The week with them.** Week {week} is over and has no note yet, so write `reports/week-{week}.html` (a page; see
   "Markdown or a page"): a short letter to them, in your voice, one screen or so, that lets them judge whether you are making their life lighter. Sections:
   *what I took off your plate* (the tasks you did for them, the asks they said yes to and what came of them, mail
   sent or archived, calendar and task changes, drafts: concrete, dated, one line each); *what I got wrong* (asks they
   answered "Already did it", results they dismissed unread, things you re-asked, corrections they made, a run that
   failed or was refused a tool when they had said yes); *what I'm carrying for you* (the open loops with dates, in one
   line each, and the watches set for them); *what I'd need to do more* (sources you can't reach, tools you were refused,
   things that still take their hands). Then the relay's numbers for the week, as a small `stats` row or table, and one honest
   sentence on how the week went. Return it as the dream's `report` with the title "Your week with Mochi ({week})";
   this is the one report a dream may raise. The relay's numbers (facts): {stats}
"""

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
what you changed. End with the mochi block. Your "say" is the one line they see in the pig's bubble (under 160
characters, the result itself: "moved it to sat 19:00", not "done"); the rest of your answer is filed as a report
they can open from Recent... Don't add an ask about the result; an ask is only for a real next step that needs them:
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

FOLLOWUP_PROMPT = """They replied on their phone to what you sent them from this conversation:

{text}

Carry on from where you left off, as in a chat: you have everything above, don't redo what's done. Same rules as
before: be brief, say exactly what you changed, end with the mochi block. Your "say" is the first line of what goes
back to their phone (the rest of your reply follows it whole):
{block}"""

ASK_PROMPT = """The user asked you, through the pig, to do this:

{text}

Do it. Work in their home directory unless the request points elsewhere; your own notes live in this
workspace (memory/, journal/) and you may update them if you learned something. Be brief; if you changed
anything, say exactly what. End with the mochi block. Your "say" is the one line they see in the pig's bubble (under
160 characters, the answer itself, in your voice); the rest of your reply is filed as a report they can open from
Recent... Don't add an ask about the result; an ask is only for a real next step that needs them:
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
PAGE = load_sibling("mochi_view", "mochi-view")  # an HTML report: its plain text, and the page for the phone
WS = load_sibling("mochi_workshop", "mochi-workshop")  # Mochi's workshop: its worktree of xpet, build, deploy
BROWSER = load_sibling("mochi_browser", "mochi-browser")  # Mochi's own Chrome, as an MCP server per run
BOX = load_sibling("mochi_box", "mochi-box")  # Mochi's own computer; its Chrome is a second MCP server
CLOCKS = load_sibling("mochi_clocks", "mochi-clocks")  # the pig on the kitchen and corridor clocks


def now():
    return time.time()


def today():
    return dt.date.today().isoformat()


def journal_days(since):
    """The day journals written to since a timestamp, oldest first."""
    return sorted(p for p in JOURNAL.glob("*.md") if p.stat().st_mtime > since)


def night_now(at=None):
    """Inside NIGHT_HOURS: away then means asleep, and nothing starts on the clock."""
    a, b = NIGHT_HOURS
    h = (at or dt.datetime.now()).hour
    return (h >= a or h < b) if a > b else (a <= h < b)


def brief_due(state, at=None):
    """Whether this round should carry the morning brief: none written for today yet, and it is late enough in the
    morning (they wake ~9:30; a brief at midnight was read eight hours stale, four times in the first week)."""
    at = at or dt.datetime.now()
    return state.get("brief_day") != at.date().isoformat() and at.hour >= BRIEF_FROM


def week_due(state, today_=None):
    """The ISO week before the current one, as 'YYYY-Www', if no week note has been written for it yet; else None."""
    d = today_ or dt.date.today()
    y, w, _ = (d - dt.timedelta(days=7)).isocalendar()
    week = f"{y}-W{w:02d}"
    return week if state.get("week_report") != week else None


def week_span(week):
    """Monday 00:00 to the next Monday 00:00 of an ISO week ('2026-W41'), as timestamps."""
    y, w = int(week[:4]), int(week[-2:])
    monday = dt.datetime.combine(dt.date.fromisocalendar(y, w, 1), dt.time())
    return monday.timestamp(), (monday + dt.timedelta(days=7)).timestamp()


def week_stats(week, comms_file=None, log_file=None):
    """The relay's numbers for a week, from comms.jsonl (asks, answers, results, tasks) and actions.log (runs and
    their cost): facts for the week note, so Mochi's letter has the count, not an impression."""
    t0, t1 = week_span(week)
    rows = []
    try:
        for line in (comms_file or COMMS_FILE).read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and t0 <= float(r.get("t") or 0) < t1:
                rows.append(r)
    except OSError:
        pass
    asks = {r["id"]: r for r in rows if r.get("kind") == "ask" and r.get("id")}
    by_src, answers, done = {}, {}, {"pig": 0, "telegram": 0, "phone": 0}
    for r in rows:
        if r.get("kind") == "ask":
            by_src[r.get("src") or "?"] = by_src.get(r.get("src") or "?", 0) + 1
        elif r.get("kind") == "answer":
            lab = str(r.get("label") or "")
            answers[lab] = answers.get(lab, 0) + 1
        elif r.get("kind") == "done":
            done[r.get("via") or "pig"] = done.get(r.get("via") or "pig", 0) + 1
    runs, cost = {}, 0.0
    stamp = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) (\w+) '[^']*': (ok|FAILED)\b.*?\((\d+)s, \$([\d.]+)\)")
    try:
        for line in (log_file or LOG_FILE).read_text().splitlines():
            m = stamp.match(line)
            if not m:
                continue
            t = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").timestamp()
            if t0 <= t < t1:
                runs[m.group(2)] = runs.get(m.group(2), 0) + 1
                cost += float(m.group(5))
    except OSError:
        pass
    proactive = {k: v for k, v in by_src.items() if k in ("round", "catchup")}
    return {"week": week, "runs": runs, "cost_usd": round(cost, 2),
            "tasks_done": {k: v for k, v in done.items() if v},
            "asks_raised_by": by_src, "asks_unprompted": sum(proactive.values()),
            "answers": dict(sorted(answers.items(), key=lambda kv: -kv[1])),
            "results_delivered": sum(1 for r in rows if r.get("kind") == "result"),
            "misses": {k: v for k, v in answers.items()
                       if k.lower().startswith(("already", "dismiss", "not now", "never"))},
            "asks_in_week": len(asks)}


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


def closed_from_comms(waiting):
    """Asks answered or expired, rebuilt from comms.jsonl, with the session that raised each one where its
    transcript can still be found (so Chat about it resumes it)."""
    asks, closed = {}, {}
    try:
        lines = COMMS_FILE.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            x = json.loads(line)
        except ValueError:
            continue
        aid = x.get("id", "")
        if x.get("kind") == "ask":
            asks[aid] = x
        elif x.get("kind") in ("answer", "closed") and aid in asks and not aid.startswith("workshop-"):
            a = asks[aid]
            closed.pop(aid, None)
            closed[aid] = {"id": aid, "text": a.get("text", ""), "options": a.get("options") or REPORT_OPTIONS,
                           "path": a.get("path", ""), "do": "", "session": "", "urgent": bool(a.get("urgent")),
                           "at": a["t"], "how": x.get("label") or x.get("how") or "", "closed_at": x["t"], "tg": None}
    out = [c for c in closed.values() if c["id"] not in waiting][-HISTORY_KEEP:]
    projects = Path.home() / ".claude" / "projects" / ("-" + str(WORK).strip("/").replace("/", "-").replace(".", "-"))
    transcripts = sorted(projects.glob("*.jsonl"), key=lambda f: f.stat().st_mtime) if projects.is_dir() else []
    for c in out:
        needle = json.dumps(c["text"][:60], ensure_ascii=False)[1:-1]
        for f in transcripts:  # the run ended just before the ask was raised
            if abs(f.stat().st_mtime - c["at"]) > 3600:
                continue
            try:
                if needle in f.read_text(errors="replace"):
                    c["session"] = f.stem
                    break
            except OSError:
                pass
    return out


def history_label(c):
    """One line in Recent...: when, how it ended, what it was. '|' separates menu items, so none inside."""
    when = dt.datetime.fromtimestamp(c.get("closed_at") or c["at"])
    day = "" if when.date() == dt.date.today() else f"{when:%a} "
    how = (c.get("how") or "").replace("|", "/")[:10]
    text = c["text"].replace("|", "/")
    text = text if len(text) <= 30 else text[:29].rstrip() + "…"
    return f"{day}{when:%H:%M}  {text}" + (f"  ({how.lower()})" if how else "")


def browser_args(headed):
    """--mcp-config for Mochi's own Chrome, if mochi-browser and chrome-devtools-mcp are installed. Starts the Chrome
    (hidden on a nested display, with the debugging port mochi-vault signs in through) unless it's up already.
    `headed` only matters when there is no nested display to hide it on: a chat then gets a plain window."""
    if not BROWSER:
        return []
    try:
        BROWSER.ensure(headed=headed)
        path = BROWSER.write_config(headed=headed)
    except Exception as e:
        log(f"browser: no config ({e})")
        return []
    paths = [str(path)] if path else []
    try:  # the box's Chrome (peppy's real screen and GPU), when the box is up: mcp__peppy__*
        box = BOX and BOX.write_mcp_config((BROWSER.mcp_config(headed) or {}).get("mcpServers", {}).get("chrome"))
        if box:
            paths.append(str(box))
    except Exception as e:
        log(f"browser: no box config ({e})")
    return ["--mcp-config"] + paths if paths else []


def notify(text):
    run(["notify-send", "-a", "Mochi", "Mochi", text], timeout=5)


def is_text(path):
    return Path(path).suffix.lower() in TEXT_TYPES


def is_page(path):
    return Path(path).suffix.lower() in (".html", ".htm")


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

class InputProbe:
    """Real input, straight from the devices: XInput2 raw key, button and motion events on the root window, read on a
    thread with its own X connection. XScreenSaver's idle counter (below) is what everything used to rely on, and any
    program may reset it with XResetScreenSaver (players and games do, to keep the screen on); on 2026-10-12/13 that
    kept the relay believing they were at the desk all night, 15 rounds for nothing. Raw events only come from input
    devices (XTest's fake ones included), so this is the idle time that means "nobody touched anything"."""

    XI_RawKeyPress, XI_RawButtonPress, XI_RawMotion, XI_LASTEVENT = 13, 15, 17, 26
    XIAllMasterDevices, GenericEvent = 1, 35

    class EventMask(ctypes.Structure):
        _fields_ = [("deviceid", ctypes.c_int), ("mask_len", ctypes.c_int), ("mask", ctypes.POINTER(ctypes.c_ubyte))]

    class Cookie(ctypes.Structure):
        _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                    ("display", ctypes.c_void_p), ("extension", ctypes.c_int), ("evtype", ctypes.c_int),
                    ("cookie", ctypes.c_uint), ("data", ctypes.c_void_p)]

    class Event(ctypes.Union):
        pass  # fields set below (they refer to Cookie)

    Event._fields_ = [("type", ctypes.c_int), ("cookie", Cookie), ("pad", ctypes.c_long * 24)]

    def __init__(self):
        self.last = 0.0   # when the last raw input event arrived (0: none seen yet)
        self.events = 0
        self.ok = False
        try:
            x = ctypes.cdll.LoadLibrary("libX11.so.6")
            xi = ctypes.cdll.LoadLibrary("libXi.so.6")
            x.XOpenDisplay.restype, x.XOpenDisplay.argtypes = ctypes.c_void_p, [ctypes.c_char_p]
            x.XDefaultRootWindow.restype, x.XDefaultRootWindow.argtypes = ctypes.c_ulong, [ctypes.c_void_p]
            x.XQueryExtension.argtypes = [ctypes.c_void_p, ctypes.c_char_p] + [ctypes.POINTER(ctypes.c_int)] * 3
            x.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            x.XPending.argtypes = x.XFlush.argtypes = x.XConnectionNumber.argtypes = [ctypes.c_void_p]
            xi.XISelectEvents.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(self.EventMask), ctypes.c_int]
            xi.XIQueryVersion.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
            dpy = x.XOpenDisplay(None)
            if not dpy:
                return
            op, ev, er = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
            if not x.XQueryExtension(dpy, b"XInputExtension", op, ev, er):
                return
            major, minor = ctypes.c_int(2), ctypes.c_int(0)
            if xi.XIQueryVersion(dpy, major, minor) != 0:  # Success
                return
            n = (self.XI_LASTEVENT >> 3) + 1
            buf = (ctypes.c_ubyte * n)()
            for e in (self.XI_RawKeyPress, self.XI_RawButtonPress, self.XI_RawMotion):
                buf[e >> 3] |= 1 << (e & 7)
            mask = self.EventMask(self.XIAllMasterDevices, n, ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
            xi.XISelectEvents(dpy, x.XDefaultRootWindow(dpy), ctypes.byref(mask), 1)
            x.XFlush(dpy)
            self.x, self.dpy, self.opcode, self.buf = x, dpy, op.value, buf
            self.ok = True
            threading.Thread(target=self.listen, daemon=True).start()
        except (OSError, AttributeError):
            pass

    def listen(self):
        fd = self.x.XConnectionNumber(self.dpy)
        e = self.Event()
        while True:
            try:
                select.select([fd], [], [], 5)
                while self.x.XPending(self.dpy):
                    self.x.XNextEvent(self.dpy, ctypes.byref(e))
                    if e.type == self.GenericEvent and e.cookie.extension == self.opcode:
                        self.last = time.time()
                        self.events += 1
            except (OSError, ValueError):
                self.ok = False
                return

    def seconds(self):
        """Seconds since the last real input, or None until the first event (then nothing is known yet)."""
        return time.time() - self.last if self.ok and self.last else None


class IdleProbe:
    """How long since they last touched the computer. Raw input (InputProbe) once it has seen anything; until then,
    and without XInput2, XScreenSaverQueryInfo. The two normally agree; when the screensaver counter sits near zero
    while no input has arrived for a long time, something is calling XResetScreenSaver, and that is logged once an
    hour so the culprit can be found (`mochi-brain --log`)."""

    class Info(ctypes.Structure):
        _fields_ = [("window", ctypes.c_ulong), ("state", ctypes.c_int), ("kind", ctypes.c_int),
                    ("til_or_since", ctypes.c_ulong), ("idle", ctypes.c_ulong), ("event_mask", ctypes.c_ulong)]

    def __init__(self, raw=True):
        self.ok = False
        self.input = InputProbe() if raw else None
        self.noted = 0
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

    def screensaver_seconds(self):
        if not self.ok:
            return 0.0
        self.xss.XScreenSaverQueryInfo(self.dpy, self.root, self.info)
        return self.info.contents.idle / 1000.0

    def seconds(self):
        xss = self.screensaver_seconds()
        raw = self.input.seconds() if self.input else None
        if raw is None:
            return xss
        if xss < 60 < AWAY_AFTER < raw and time.time() - self.noted > 3600:
            self.noted = time.time()
            log(f"presence: X's idle counter is {xss:.0f}s but no key, button or pointer event for {raw / 60:.0f} min: "
                "something keeps calling XResetScreenSaver (a player or game?); going by real input")
        return raw


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


class Feed:
    """What's new in their life, live, from four places. The relay polls each with a cheap `mochi-sense ... feed`
    (or `telegram counts`) in a thread and keeps what's new here until a catch-up run reads it:
      telegram  every FEED_EVERY: chats (people and groups, minus memory/feed.json's mute list) that grew. A chat is
                ready once it has been quiet FEED_SETTLE, or its first new message is FEED_MAX_WAIT old.
      mail      after every mail pull: new messages that aren't bulk (List-Unsubscribe and the like).
      calls     every SLOW_FEED_EVERY: a recorded call on the NAS that got its transcript.
      calendar  every SLOW_FEED_EVERY: events added, moved, changed or cancelled (Google CalDAV, sources.json).
                A calendar marked "log" (a time log, holidays) only leaves an event for the next run.
    Everything but Telegram is ready at once. The first look at each source is only a baseline. A source that stays
    unreadable for FEED_DOWN_AFTER wakes Mochi once (and again when it's back). state is relay.json's "feed"."""

    LABELS = {"telegram": "their Telegram archive", "mail": "their local mail index",
              "calls": "the recorded-calls folder on the NAS", "calendar": "their Google calendars (CalDAV)",
              "phone": "their phone (mochi-phone)"}

    def __init__(self, state, fire):
        self.state, self.fire = state, fire
        for k, v in {"counts": {}, "new": {}, "items": {}, "down": {}, "last_poll": 0, "last_catchup": 0,
                     "mail_seen": None, "calls": None, "cal": None, "phone_seen": None}.items():
            state.setdefault(k, v)
        for k in ("down_since", "told_down"):  # from before the feed had more than Telegram
            state.pop(k, None)
        self.results = queue.Queue()
        self.polling = set()

    def mute(self):
        try:
            m = json.loads(FEED_FILE.read_text()).get("mute") or []
            return {str(x) for x in m}
        except (OSError, ValueError, AttributeError):
            return set()

    COMMANDS = {"telegram": ["telegram", "counts"], "mail": ["mail", "feed"], "calls": ["calls", "feed"],
                "calendar": ["calendar", "feed"], "phone": ["phone", "feed"]}

    def poll_async(self, src):
        if src in self.polling:
            return
        self.polling.add(src)

        def go():
            try:
                code, out, err = run([SENSE, *self.COMMANDS[src]], timeout=150)
                try:
                    data = json.loads(out)
                except ValueError:
                    data = {"ok": False, "error": (out or err or f"exit {code}").strip()[-200:]}
                self.results.put((src, data))
            finally:
                self.polling.discard(src)
        threading.Thread(target=go, daemon=True).start()

    def drain(self):
        while not self.results.empty():
            src, data = self.results.get_nowait()
            self.take(data, src=src)

    def take(self, data, t=None, src="telegram"):
        t = now() if t is None else t
        if not self.health(src, data, t):
            return
        getattr(self, "take_" + src)(data, t)

    def health(self, src, data, t):
        """True if the source answered. Tracks how long it hasn't, and tells Mochi once when that's too long."""
        d = self.state["down"].setdefault(src, {"since": 0, "told": False})
        if data.get("ok"):
            if d["told"]:
                self.fire(f"{self.LABELS[src]} is readable again", False, False)
            d["since"], d["told"] = 0, False
            return True
        why = re.sub(r"\s+", " ", str(data.get("error") or "no answer")).replace("## Telegram archive", "")[:160]
        if src == "calendar" and "no calendars configured" in why:
            return False  # not set up: nothing to miss
        d["since"] = d["since"] or t
        if t - d["since"] >= FEED_DOWN_AFTER and not d["told"]:
            d["told"] = True
            self.fire(f"{self.LABELS[src]} has been unreadable for {human_age(t - d['since'])} ({why.strip(' _:')}); "
                      f"you can't keep up with it until it's fixed", False, True)
        return False

    def item(self, key, line, t):
        self.state["items"].setdefault(key, {"line": line, "at": t})

    def take_telegram(self, data, t):
        st = self.state
        mute, counts, first = self.mute(), st["counts"], not st["counts"]
        since = st["last_poll"] or t
        for did, d in (data.get("dialogs") or {}).items():
            n = int(d.get("n") or 0)
            old = counts.get(did)
            counts[did] = n
            if old is None and not first and n <= 50:  # a chat that just appeared: someone new wrote (not a backfill)
                old = 0
            if first or old is None or n <= old:
                continue
            if d.get("kind") not in FEED_KINDS or d.get("archived") or did in mute:
                continue
            e = st["new"].setdefault(did, {"name": d.get("name") or did, "kind": d.get("kind"), "n": 0,
                                           "first": t, "since": since - 300})  # the archive may lag a little
            e["n"] += n - old
            e["last"] = t
        st["last_poll"] = t

    def take_mail(self, data, t):
        msgs = data.get("messages") or {}
        seen = self.state["mail_seen"]
        if seen is not None:
            seen = set(seen)
            for mid, m in msgs.items():
                if mid in seen or m.get("bulk"):
                    continue
                who = "they sent" if m.get("sent") else f"from {m.get('from', '?')}"
                self.item(f"mail:{mid}", f"mail ({m.get('account', '?')}), {who}: “{m.get('subject', '')}”; read: "
                                         f"`mochi-sense mail show id:{mid}`", t)
        self.state["mail_seen"] = list(msgs)

    def take_phone(self, data, t):
        """Missed calls, texts in, messages from people in messaging apps, as mochi-phone keys them (a message group
        per sender per hour keeps a chatty evening to one item). Telegram messages arrive here too, as a backup: they
        count only while the archive is unreadable; otherwise the archive's own feed raises the chat."""
        items, seen = data.get("items") or {}, self.state["phone_seen"]
        tg_down = bool(self.state["down"].get("telegram", {}).get("since"))
        if seen is not None:
            seen = set(seen)
            for key, it in items.items():
                if key in seen:
                    continue
                if key.startswith("phone:notif:Telegram") and not tg_down:
                    continue
                self.item(key, it["line"], t)
        self.state["phone_seen"] = list(items)

    def take_calls(self, data, t):
        calls, old = data.get("calls") or {}, self.state["calls"]
        has = lambda exts: bool(set(exts) & {".dialog", ".txt", ".json"})
        if old is not None:
            for key, exts in calls.items():
                if has(exts) and not has(old.get(key) or []):
                    how = "calls summary" if ".summary" in exts else "calls read"
                    self.item(f"call:{key}", f"a recorded call got its transcript on the NAS: {key}; read: "
                                             f"`mochi-sense {how} \"{key}\"`", t)
        self.state["calls"] = calls

    def take_calendar(self, data, t):
        evs, old = data.get("events") or {}, self.state["cal"]
        cur = {uid: {"s": e.get("summary", ""), "t": e.get("start") or 0, "a": bool(e.get("all_day")),
                     "w": e.get("where", ""), "st": e.get("status", ""), "c": e.get("calendar", ""),
                     "r": bool(e.get("rrule")), "l": bool(e.get("log"))} for uid, e in evs.items()}
        when = lambda e: dt.datetime.fromtimestamp(e["t"]).strftime("%a %d %b" + ("" if e["a"] else " %H:%M"))
        if old is not None:
            item = self.item

            def note(key, line, t, log):  # a time log's entries ride along with the next run instead of starting one
                if log:
                    self.fire(line, False, False)
                else:
                    item(key, line, t)
            for uid, e in cur.items():
                o = old.get(uid)
                what = f"calendar '{e['c']}': “{e['s']}”"
                if o is None:
                    note(f"cal:{uid}:{e['t']}", f"{what} added, {when(e)}" + (f" at {e['w']}" if e["w"] else ""), t, e["l"])
                elif e["st"] == "CANCELLED" and o["st"] != "CANCELLED":
                    note(f"cal:{uid}:x", f"{what} {when(e)} cancelled", t, e["l"])
                elif (o["t"], o["a"]) != (e["t"], e["a"]):
                    note(f"cal:{uid}:{e['t']}", f"{what} moved from {when(o)} to {when(e)}", t, e["l"])
                elif (o["s"], o["w"]) != (e["s"], e["w"]):
                    note(f"cal:{uid}:{e['s']}{e['w']}", f"{what} {when(e)} changed (was “{o['s']}”"
                                                         f"{', at ' + o['w'] if o['w'] else ''})", t, e["l"])
            for uid, o in old.items():
                if uid not in cur and not o["r"] and t + 3600 < o["t"] < t + 80 * 86400:
                    note(f"cal:{uid}:gone", f"calendar '{o['c']}': “{o['s']}” {when(o)} removed", t, o.get("l"))
        self.state["cal"] = cur

    def ready(self, t=None):
        """(chats, items): Telegram chats whose conversation has settled (or waited long enough), and everything
        else that's new."""
        t = now() if t is None else t
        chats = [(did, e) for did, e in sorted(self.state["new"].items(), key=lambda x: x[1]["first"])
                 if t - e.get("last", e["first"]) >= FEED_SETTLE or t - e["first"] >= FEED_MAX_WAIT]
        items = sorted(self.state["items"].items(), key=lambda x: x[1]["at"])
        return chats, items

    def pop(self, chats, items=()):
        for did in chats:
            self.state["new"].pop(did, None)
        for key in items:
            self.state["items"].pop(key, None)


ERRAND = load_sibling("mochi_errands", "mochi-errands")  # what needs them somewhere, and where they are
ERRAND_PER_DAY = 4           # location pokes per day; each one buzzes (they asked to be poked: 2026-10-20)
ERRAND_COOLDOWN = 4 * 3600   # the same errand is not raised again sooner than this, answered "Not now" or ignored
ERRAND_FIX_AGE = 10 * 60     # a fix older than this is not "where they are"
ERRAND_FAST = 8.0            # m/s: faster than a bike is a car or a train; not the moment to stop
ERRAND_MARGIN = 15 * 60      # free for the errand's minutes plus this, or it waits
ERRAND_SHIFT = 60 * 60       # an Uber Driver notification this recent: they're on a courier shift
ERRAND_OPTIONS = ["Done", "Not now", "Drop it"]
ERRAND_APP_FRESH = 20 * 60   # the phone app heard from this recently: pokes go through it (sealed), else Telegram
PHONE = shutil.which("mochi-phone") or str(HOME / ".local/bin/mochi-phone")


def phone_send(*args):
    """mochi-phone send ...: True if it went to the relay."""
    try:
        return subprocess.run([PHONE, "send", *args], capture_output=True, text=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class Errands:
    """Mochi's location errands (memory/errands.json, see mochi_errands.py): each time the phone reports a new fix,
    whether one of them can be done right here, right now: out of the house, not driving, not on a shift or a call
    or do-not-disturb, free in the calendar for long enough, the place open, outside quiet hours. Then one poke as
    an ask that goes to their phone and buzzes (ERRAND_PER_DAY). Done / Not now / Drop it come back here; Mochi hears
    each as an event and keeps the file (ticks the task, removes what's done). An unanswered poke is withdrawn once
    they've walked on. The check runs in a thread (Overpass, the calendar); pokes come out through `ready`."""

    def __init__(self, state, quiet):
        self.state, self.quiet = state, quiet
        for k, v in {"items": {}, "pending": None, "last_fix": 0, "error": ""}.items():
            state.setdefault(k, v)
        self.ready = queue.Queue()
        self.running = False
        self.cal = (0, None)  # (when read, busy spans)
        self.fetch = ERRAND.osm_fetch if ERRAND else None  # Overpass (tests put a fake here)

    def item(self, eid):
        return self.state["items"].setdefault(eid, {})

    def latest(self):
        try:
            return json.loads(ERRAND.LATEST.read_text())
        except (OSError, ValueError, AttributeError):
            return {}

    def poll(self):
        """From the main loop: a new fix → a check in the background."""
        if not ERRAND or self.running:
            return
        latest = self.latest()
        loc = latest.get("loc") or {}
        if not loc.get("t") or loc["t"] == self.state["last_fix"]:
            return
        self.state["last_fix"] = loc["t"]
        self.running = True
        threading.Thread(target=self._check, args=(latest,), daemon=True).start()

    def _check(self, latest):
        try:
            self.check(latest)
        except Exception:
            log("errands: check failed:\n" + traceback.format_exc())
        finally:
            self.running = False

    def why_not(self, latest, t=None):
        """Why nothing may be raised at all right now, or "" if it may."""
        t = t or now()
        loc, st = latest.get("loc") or {}, latest.get("state") or {}
        if self.quiet():
            return "quiet hours"
        if t - (loc.get("t") or 0) > ERRAND_FIX_AGE:
            return "old fix"
        if (loc.get("speed") or 0) > ERRAND_FAST:
            return "moving fast"
        if st.get("dnd"):
            return "do not disturb"
        if any(c.get("state") in ("ringing", "offhook", "active") and t - (c.get("t") or 0) < 1800
               for c in (latest.get("calls") or [])[-2:]):
            return "on a call"
        if any(n.get("app") == "com.ubercab.driver" and t - (n.get("t") or 0) < ERRAND_SHIFT
               for n in latest.get("notifs") or []):
            return "on a shift"
        return ""

    def busy(self):
        if now() - self.cal[0] > 600:
            rows = ERRAND.agenda()
            self.cal = (now(), ERRAND.busy_spans(rows) if rows is not None else None)
        return self.cal[1]

    def check(self, latest, t=None):
        t = t or now()
        loc = latest.get("loc") or {}
        errands, self.state["error"] = ERRAND.load()
        pend = self.state.get("pending")
        if pend:  # one poke at a time; withdrawn once they've walked away from the place
            p = pend.get("place") or {}
            if loc.get("lat") is not None and ERRAND.distance_m(loc["lat"], loc["lon"], p["lat"], p["lon"]) > pend.get("r", 250) * 2 + 100:
                self.ready.put(("passed", pend))
            return
        if self.state.get("counts_day") != today():
            self.state["counts_day"], self.state["today"] = today(), 0
        if self.state.get("today", 0) >= ERRAND_PER_DAY or self.why_not(latest, t):
            return
        home = loc.get("zone") == "home"
        when = dt.datetime.fromtimestamp(t)
        cache = ERRAND.jload(ERRAND.CACHE, {})
        best = None
        for e in errands:
            eid = ERRAND.eid(e)
            s = self.state["items"].get(eid) or {}
            every = float(e.get("every") or 0) * 86400
            if s.get("dropped") or (s.get("done") and not (every and t - s["done"] > every)):
                continue
            if t - max(s.get("poked", 0), s.get("snoozed", 0)) < ERRAND_COOLDOWN or (home and not e.get("from_home")):
                continue
            if not ERRAND.doable_now(e, when):
                continue
            p = ERRAND.nearest(e, loc["lat"], loc["lon"], loc.get("acc") or 0, cache, self.fetch, when=when,
                              skip=s.get("not_here") or ())
            if not p:
                continue
            due = ERRAND.day(e.get("until")) or dt.date.max
            if best is None or (due, p["d"]) < (best[2], best[1]["d"]):
                best = (e, p, due)
        ERRAND.jsave(ERRAND.CACHE, cache)
        if not best:
            return
        e, p, _ = best
        spans = self.busy()
        minutes = int(e.get("minutes") or 10)
        free = ERRAND.free_until(spans, when.astimezone()) if spans is not None else None
        if free is not None and (free - when.astimezone()).total_seconds() < (minutes * 60 + ERRAND_MARGIN):
            return  # it comes back with the next fix, once the meeting is over
        self.ready.put(("poke", {"e": e, "place": p, "free": free.strftime("%H:%M") if free else None,
                                 "calendar": spans is not None}))

    def poke_text(self, e, p, free):
        """The pig's line, Telegram's (the place by its name, never the street) and the phone app's (sealed end to
        end, so the street too)."""
        minutes = int(e.get("minutes") or 10)
        head = f"📍 {e['do']}"
        tail = f"{round(p['d'] / 10) * 10:.0f} m · ~{minutes} min" + (f" · free till {free}" if free else "")
        app = f"{head}\n{p['name']}" + (f", {p['addr']}" if p.get("addr") else "") + f" · {tail}"
        return f"{head} — {p['name']}, {tail}"[:160], f"{head}\n{p['name']}, {tail}\n{ERRAND.maps_link(p)}", app

    def app_fresh(self):
        return now() - (self.latest().get("last_seen") or 0) < ERRAND_APP_FRESH

    def poked(self, info, aid):
        e = info["e"]
        s = self.item(ERRAND.eid(e))
        s["poked"] = now()
        self.state["today"] = self.state.get("today", 0) + 1
        p = info["place"]
        r = float(e.get("near") or ERRAND.NEAR_RADIUS)
        self.state["pending"] = {"aid": aid, "id": ERRAND.eid(e), "do": e["do"], "at": now(),
                                 "place": {"name": p["name"], "lat": p["lat"], "lon": p["lon"]}, "r": r}

    def answer(self, eid, label):
        """Their tap on a poke. Returns the line for Mochi's events."""
        s = self.item(eid)
        pend = self.state.get("pending") or {}
        if pend.get("id") == eid:
            self.state["pending"] = None
        low = label.lower()
        where = (pend.get("place") or {}).get("name", "")
        if low == "done":
            s["done"] = now()
            return f"errand done (they tapped Done near {where}): {pend.get('do') or eid}"
        if low == "drop it":
            s["dropped"] = now()
            return f"errand dropped by them: {pend.get('do') or eid} (take it out of errands.json; ask nothing)"
        if low in ("not now", "later"):
            s["snoozed"] = now()
            return f"errand put off ('{label}') near {where}: {pend.get('do') or eid}"
        s["snoozed"] = now() + 20 * 3600
        return f"errand: they answered '{label}' near {where} to: {pend.get('do') or eid}"

    def status(self):
        if not ERRAND:
            return "mochi-errands not installed"
        errands, err = ERRAND.load()
        rows = [f"! {err}"] if err else []
        for e in errands:
            s = self.state["items"].get(ERRAND.eid(e)) or {}
            marks = [f"{k} {human_age(now() - s[k])} ago" for k in ("done", "dropped", "poked", "snoozed")
                     if s.get(k) and s[k] <= now()]
            rows.append(f"{ERRAND.eid(e)}: {e['do'][:60]} — {', '.join(marks) or 'waiting'}")
        pend = self.state.get("pending")
        if pend:
            rows.append(f"poked {human_age(now() - pend['at'])} ago, unanswered: {pend['do'][:60]}")
        return rows or ["none (memory/errands.json)"]


# ---- your phone: the Telegram bot ----------------------------------------------------------------

LATER = "Later"
SHOW = "Show me"
TG_HELP = """I'm Mochi. While you're away from the computer my questions come here with buttons; tap one and I act on it.
Write me anything and I'll treat it as a task (reply to one of my questions to answer it in your own words).
Reply to one of my answers to carry on that conversation; a message that isn't a reply starts a fresh one.
Send me a photo or a document (a caption says what to do with it) and I'll take it from there.
/asks  what's waiting   /brief  the latest report   /seen  today's journal   /status   /round"""


def tgsend_decide(sock, rid, allow, reason=""):
    """Your Send/Deny to a waiting `mochi-tg send`, over the reply socket it named; mochi-tg checks the peer is this
    process (the relay's pid) before it trusts the answer."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    try:
        s.connect(sock)
        s.sendall(json.dumps({"id": rid, "allow": bool(allow), "reason": reason}).encode() + b"\n")
        r = json.loads(s.recv(65536) or b"{}")
        return bool(r.get("ok")), str(r.get("text") or "")
    except (OSError, ValueError) as e:
        return False, f"mochi-tg isn't waiting any more ({e})"
    finally:
        s.close()


def vault_decide(rid, choice, reason=""):
    """Your Allow (the account's index) or Deny (None) to mochi-vault, which checks it came from this process."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    try:
        s.connect(str(VAULT_SOCK))
        s.sendall(json.dumps({"cmd": "decide", "id": rid, "choice": choice, "reason": reason}).encode() + b"\n")
        r = json.loads(s.recv(65536) or b"{}")
        return bool(r.get("ok")), str(r.get("text") or "")
    except (OSError, ValueError) as e:
        return False, f"mochi-vault unreachable ({e})"
    finally:
        s.close()


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
        self.vault = {}  # request id -> (message id, text, accounts) for the sign-in questions on the phone
        self.tgsend = {}  # request id -> (message id, text, reply socket) for mochi-tg's "send this as you?"
        self.albums = {}  # media_group_id -> {"atts", "caption", "m", "last"}: an album still arriving
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
            self.last_ids = list(getattr(self.bot, "sent_ids", None) or [mid])
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
        """'Show me' on the phone: a text report as a message, anything else (PDF, picture) as the file itself. An
        HTML report is both: its text as a message, then the page as one file that opens in the phone's browser."""
        if not is_text(path):
            return self.send_file(path, caption=caption, reply_to=reply_to)
        mid = self.send(self.report_body(path), reply_to=reply_to)
        if is_page(path) and PAGE:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / f"{Path(path).stem}.html"
                try:
                    out.write_text(PAGE.phone_page(path))
                except OSError as e:
                    log(f"telegram: no page for {Path(path).name}: {e}")
                    return mid
                self.send_file(out, caption="the page: open it to see it properly")
        return mid

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
        text = ("❗ " if a.get("urgent") else "") + (a.get("tg_text") or a["text"])
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
        if is_page(path):
            body = PAGE.text_of(body) if PAGE else re.sub(r"<[^>]+>", " ", body)
        return body if len(body) <= limit else body[:limit] + "\n\n(… the rest is on the pig)"

    # -- mochi-vault: a sign-in Mochi wants to make with one of their logins, asked on the phone every time

    def vault_ask(self, m):
        rid = str(m.get("id") or "")
        if not re.fullmatch(r"[0-9a-f]{16}", rid):
            return
        if not self.on:
            vault_decide(rid, None, "the Telegram bot is off, so there was no one to ask")
            return
        if self.relay.count("vault_asks") >= VAULT_ASKS_PER_DAY:
            vault_decide(rid, None, f"over today's {VAULT_ASKS_PER_DAY} sign-in questions; ask again tomorrow")
            return
        self.relay.bump("vault_asks")
        accounts = [str(a)[:60] for a in (m.get("accounts") or [])][:8]
        text = f"🔑 Mochi wants to sign in to {str(m.get('host'))[:80]}\n{str(m.get('why') or '')[:300]}".rstrip()
        rows = [[(a, f"v:{rid}:{i}")] for i, a in enumerate(accounts)] + [[("Deny", f"v:{rid}:x")]]
        mid = self.send(text, buttons=rows, buzz=not self.quiet_now())
        if not mid:
            vault_decide(rid, None, "couldn't reach their phone")
            return
        self.vault[rid] = (mid, text, accounts)
        log(f"vault: asked on the phone to sign in to {m.get('host')}")

    def vault_answer(self, rid, idx, mid):
        mid0, text, accounts = self.vault.pop(rid, (mid, "", []))
        choice = None if idx == "x" else (int(idx) if idx.isdigit() else None)
        ok, msg = vault_decide(rid, choice, "" if choice is not None else "they tapped Deny")
        if not ok and choice is not None:
            self.edit(mid0, f"{text}\n\n(couldn't pass that on: {msg})")
        else:
            self.edit(mid0, f"{text}\n\n" + (f"✓ allowed: {msg}" if choice is not None else "✗ denied"))
        log(f"vault: {'allowed' if choice is not None else 'denied'} on the phone" + ("" if ok else f" ({msg})"))

    def vault_done(self, m):
        got = self.vault.pop(str(m.get("id") or ""), None)
        if got:
            self.edit(got[0], f"{got[1]}\n\n{str(m.get('label') or 'closed')[:60]}")

    # -- mochi-tg: a Telegram message Mochi wants to send in their name, asked on the phone every time

    def tgsend_ask(self, m):
        rid, sock = str(m.get("id") or ""), str(m.get("sock") or "")
        if not re.fullmatch(r"[0-9a-f]{16}", rid) or not sock:
            return
        if not self.on:
            tgsend_decide(sock, rid, False, "the Telegram bot is off, so there was no one to ask")
            return
        if self.relay.count("tgsend_asks") >= TG_SEND_ASKS_PER_DAY:
            tgsend_decide(sock, rid, False, f"over today's {TG_SEND_ASKS_PER_DAY} send questions; ask again tomorrow")
            return
        self.relay.bump("tgsend_asks")
        body = str(m.get("text") or "")[:1500]
        text = (f"✉️ Mochi wants to send this to {str(m.get('to'))[:80]}, as you, from your Telegram account:\n\n"
                f"{body}\n\n" + (f"Why: {str(m.get('why'))[:300]}\n\n" if m.get("why") else "")
                + "Send it, or Deny. Nothing goes out without your tap.")
        rows = [[("Send it", f"t:{rid}:y"), ("Deny", f"t:{rid}:x")]]
        mid = self.send(text, buttons=rows, buzz=not self.quiet_now())
        if not mid:
            tgsend_decide(sock, rid, False, "couldn't reach their phone")
            return
        self.tgsend[rid] = (mid, text, sock)
        log(f"tg: asked on the phone to send a message to {str(m.get('to'))[:60]} as them")

    def tgsend_answer(self, rid, choice, mid):
        mid0, text, sock = self.tgsend.get(rid, (mid, "", ""))
        allow = choice == "y"
        ok, msg = tgsend_decide(sock, rid, allow, "" if allow else "they tapped Deny") if sock else (False, "no such request")
        if not ok and allow:
            self.edit(mid0, f"{text}\n\n(couldn't pass that on: {msg})")
        else:
            self.edit(mid0, f"{text}\n\n" + ("✓ sending…" if allow else "✗ denied"))
        if not (ok and allow):  # allowed: mochi-tg reports the outcome with tg_send_done, which edits the message again
            self.tgsend.pop(rid, None)
        log(f"tg: {'allowed' if allow else 'denied'} on the phone" + ("" if ok else f" ({msg})"))

    def tgsend_done(self, m):
        got = self.tgsend.pop(str(m.get("id") or ""), None)
        if got:
            self.edit(got[0], f"{got[1]}\n\n{str(m.get('label') or 'closed')[:80]}")

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
        caption = (m.get("caption") or "").strip()
        if att and m.get("media_group_id"):
            # An album arrives as one message per photo, the caption on just one of them: hold the pieces until
            # it has settled, so the pages of one letter reach one run together (flush_albums)
            al = self.albums.setdefault(m["media_group_id"], {"atts": [], "caption": "", "m": m, "last": 0})
            al["atts"].append(att)
            al["caption"] = al["caption"] or caption
            if not (al["m"].get("reply_to_message")) and m.get("reply_to_message"):
                al["m"] = m
            al["last"] = time.time()
        elif att:
            self.on_files([att], caption, m)
        else:
            self.on_text((m.get("text") or "").strip(), m)

    def flush_albums(self, settle=TG_ALBUM_SETTLE):
        """Hand on the albums whose last photo came more than `settle` seconds ago (called from the relay's loop)."""
        for gid in [g for g, al in self.albums.items() if time.time() - al["last"] >= settle]:
            al = self.albums.pop(gid)
            self.on_files(al["atts"], al["caption"], al["m"])

    def on_files(self, atts, caption, m):
        """Photos or documents from the phone (one, or an album): fetch them into inbox/ and hand them to one run (as
        the answer to the ask they reply to, else as a task with the caption as the instruction)."""
        r = self.relay
        paths = []
        for att in atts:
            try:
                path = self.bot.download(att, INBOX)
            except (TG.TgError, OSError) as e:
                log(f"telegram: can't fetch {att['name']}: {str(e)[:160]}")
                self.send(f"couldn't fetch {'one of those' if len(atts) > 1 else 'that'}: {str(e)[:200]}",
                          reply_to=m.get("message_id"))
                continue
            log(f"telegram: received {att['kind']} → {path.relative_to(WORK)} ({(att['size'] or 0) // 1024} KB)"
                + (f", caption: {caption[:80]}" if caption else ""))
            paths.append(path)
        if not paths:
            return
        reply = (m.get("reply_to_message") or {}).get("message_id")
        target = next((aid for aid, a in r.state["asks"].items() if reply and a.get("tg") == reply), None)
        if target:
            files = " ".join(f"[file: {p}]" for p in paths)
            r.on_answer(target, f"{(caption or 'see the attached file' + ('s' if len(paths) > 1 else ''))[:200]} {files}",
                        via="telegram")
        else:
            what = f"an album of {len(paths)}" if len(paths) > 1 else f"a {atts[0]['kind']}"
            r.ask(caption or f"({what} from your phone, no caption)", via="telegram", files=paths,
                  session=r.thread_of(reply))
            self.send("got it, looking", reply_to=m.get("message_id"))

    def on_button(self, data, mid):
        r = self.relay
        kind, _, rest = data.partition(":")
        if kind == "v":
            rid, _, idx = rest.partition(":")
            self.vault_answer(rid, idx, mid)
            return
        if kind == "t":
            rid, _, choice = rest.partition(":")
            self.tgsend_answer(rid, choice, mid)
            return
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
            briefs = [*REPORTS.glob("brief-*.md"), *REPORTS.glob("brief-*.html")]
            latest = max(briefs, key=lambda p: p.name, default=None)
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
            else:  # a reply to one of Mochi's answers carries on that run's conversation; anything else starts fresh
                r.ask(text, via="telegram", session=r.thread_of(reply))
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
        self.feed = Feed(self.state["feed"], lambda text, urgent, wake: self.trigger(text, urgent, "feed", wake))
        self.errands = Errands(self.state["errands"], self.tg.quiet_now)
        self.clocks = CLOCKS.Clocks(lambda: self.state["asks"], lambda: self.busy, BRAIN_SOCK, log) if CLOCKS else None
        self.capped = ""
        self.catchup_capped = ""
        self.mail_mtime = None
        threading.Thread(target=self.worker, daemon=True).start()

    def load(self):
        try:
            s = json.loads(STATE_FILE.read_text())
        except (OSError, ValueError):
            s = {}
        for k, v in {"asks": {}, "events": [], "last_round": 0, "away_since": 0, "counts": {}, "wake": [],
                     "watches": {}, "last_dream": 0, "study": {}, "night_end": 0, "workshop": {}, "feed": {},
                     "closed": [], "denials": [], "brief_day": "", "week_report": "", "threads": {}, "errands": {}}.items():
            s.setdefault(k, v)
        for oid, o in s.pop("offers", {}).items():  # from before asks carried their own options
            s["asks"][oid] = dict(o, options=OFFER_OPTIONS, path="")
        for nid, n in s.pop("notices", {}).items():
            s["asks"][nid] = {"text": n["text"], "path": n["path"], "options": REPORT_OPTIONS, "do": "",
                              "urgent": n.get("urgent", False), "at": n["at"], "session": ""}
        if not s.get("closed_seeded"):  # from before Recent...: what comms.jsonl remembers
            s["closed"] = closed_from_comms(s["asks"]) + s["closed"]
            s["closed_seeded"] = True
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
        if self.clocks:
            self.clocks.serve()
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
                if self.tg.albums:
                    self.tg.flush_albums()
                if due("sample", SAMPLE_EVERY):
                    self.activity.sample()
                if due("presence", 60):
                    self.maybe_round()
                if due("watch", 60):
                    self.watchers.check()
                    self.watches.check()
                if due("feed", FEED_EVERY):
                    self.feed.poll_async("telegram")
                if due("feed-slow", SLOW_FEED_EVERY):
                    self.feed.poll_async("calls")
                    self.feed.poll_async("calendar")
                    self.feed.poll_async("phone")
                if due("feed-mail", 60) and self.mail_pulled():
                    self.feed.poll_async("mail")
                self.feed.drain()
                pend = self.errands.state.get("pending")
                if pend and (pend["aid"] not in self.state["asks"] or now() - pend["at"] > 6 * 3600):
                    self.errands.state["pending"] = None  # withdrawn by Mochi, expired, or long gone stale
                self.errands.poll()
                self.drain_errands()
                self.maybe_wake()
                self.maybe_catchup()
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
            self.on_answer(m.get("id", ""), (m.get("answer") or "").strip(), via=m.get("via") or "pig")
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
        elif ev == "vault_ask":
            self.tg.vault_ask(m)
        elif ev == "vault_done":
            self.tg.vault_done(m)
        elif ev == "tg_send_ask":
            self.tg.tgsend_ask(m)
        elif ev == "tg_send_done":
            self.tg.tgsend_done(m)
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
        elif ev == "history":  # menu → Recent...: what was answered or expired lately, newest first
            items = [c for c in reversed(self.state["closed"]) if c["id"] not in self.state["asks"]][:HISTORY_SHOWN]
            self.pet.send(event="history", ids="|".join(c["id"] for c in items),
                          labels="|".join(history_label(c) for c in items))
        elif ev == "reopen":
            self.reopen(m.get("id", ""))
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
        self.remember_closed(aid, a, label)
        if aid.startswith("errand-"):  # a location poke: the relay keeps its state, Mochi hears it as an event
            if via != "phone" and (self.errands.state.get("pending") or {}).get("app"):
                phone_send("close", "--id", aid)  # answered on the pig or in Telegram: off the phone's shade too
            self.event(self.errands.answer(aid[len("errand-"):], label))
            if label.lower() in CLOSERS | {"done", "drop it"}:
                if via != "pig":
                    self.pet.withdraw(aid)
                self.tg.close(a, label)
                self.save()
                return
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
        if via != "pig":  # answered on the phone or on a clock: off the desktop pig's menu too
            self.pet.withdraw(aid)
        where = " (on your phone)" if phone else ""
        if low == "show me" and a.get("path"):
            if phone:
                self.link(self.tg.show(a["path"], caption=a["text"], reply_to=a.get("tg")), a.get("session"))
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
                            "resume": a.get("session") or "",  # the run that asked knows why it asked
                            "prompt": ANSWER_PROMPT.format(label=label, text=a["text"], options=options,
                                                           do=a.get("do") or "-", block=BLOCK)})
        self.save()

    def remember_closed(self, aid, a, how):
        """Keep what left the menu for a while, so Recent... can bring it back."""
        if aid.startswith("workshop-"):  # Keep it / Undo has happened (or will): nothing to come back to
            return
        closed = [c for c in self.state["closed"] if c["id"] != aid]
        self.state["closed"] = (closed + [dict(a, id=aid, how=how, closed_at=now(), tg=None)])[-HISTORY_KEEP:]

    def reopen(self, aid):
        """Recent... → one of them: back in the menu as it was, with the panel open on it."""
        c = next((c for c in self.state["closed"] if c["id"] == aid), None)
        if not c:
            return
        self.state["closed"].remove(c)
        self.event(f"user reopened (was '{c.get('how')}'): {c['text']}")
        self.add_ask(c["text"], c.get("options") or REPORT_OPTIONS, do=c.get("do") or "", path=c.get("path") or None,
                     aid=aid, session=c.get("session") or "", mirror=False, src="reopen")
        self.pet.send(event="show", id=aid)
        self.save()

    def ask_via_box(self):
        text = ask_box()
        if text:
            send_brain(event="ask", text=text)

    def link(self, mid, session):
        """A message on the phone came out of this run: a reply to it resumes the run (FOLLOWUP_PROMPT)."""
        if not (mid and session):
            return
        ids = getattr(self.tg, "last_ids", None) or []
        for i in set(ids if mid in ids else []) | {mid}:
            self.state["threads"][str(i)] = {"session": session, "at": now()}

    def thread_of(self, mid):
        t = self.state["threads"].get(str(mid)) if mid else None
        return t["session"] if t and now() - t["at"] < THREAD_KEEP else ""

    def ask(self, text, via="pig", files=None, session=""):
        self.event(f"user asked{' (from their phone)' if via == 'telegram' else ''}: {text[:200]}"
                   + (f" [files: {', '.join(Path(f).name for f in files)}]" if files else ""))
        if via != "telegram":
            self.pet.say("on it!", 3)
        prompt = text + (ASK_FILES.format(files="\n".join(f"- {f}" for f in files)) if files else "")
        if files and len(files) > 1:
            prompt += ("\nThey sent these together, as one album: read them all before you answer, and treat them as one "
                       "thing (often the pages of one letter), with one answer.")
        fresh = ASK_PROMPT.format(text=prompt, block=BLOCK)
        self.tasks.put({"kind": "ask", "title": text[:60], "level": "approved", "model": TASK_MODEL, "via": via,
                        "timeout": 1200, "resume": session, "fresh": fresh,
                        "prompt": FOLLOWUP_PROMPT.format(text=prompt, block=BLOCK) if session else fresh})

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
            if not away:
                self.greet_at_desk()
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
            # With the bot set up, a light round now and then so what can't wait can still reach you. Not at night:
            # away then means asleep, and 15 empty rounds on 2026-10-13 said so.
            if (self.tg.on and not self.tg.quiet_now() and not night_now()
                    and now() - self.state["last_round"] >= AWAY_ROUND_EVERY):
                self.start_round(self.brief_label() + f"you're away from the computer "
                                 f"({human_age(now() - self.state['away_since'])}); a light round: only what can't "
                                 "wait until they're back deserves an ask, and asks reach their phone")
            return
        back_from_break = self.state["away_since"] and now() - self.state["away_since"] > LONG_BREAK
        if first_today or brief_due(self.state):
            self.start_round(self.brief_label() + ("first round of the day" if first_today else "late-morning round"))
        elif back_from_break:
            self.start_round(f"you're back after {human_age(now() - self.state['away_since'])} away")
        elif now() - self.state["last_round"] >= ROUND_EVERY:
            self.start_round("regular round")
        self.state["away_since"] = 0

    def brief_label(self):
        """The round reason's prefix when the morning brief is due now (and marks it as written for today, so a
        failed run doesn't retry every half hour; the ~09:30 rule in CLAUDE.md still applies to the content)."""
        if not brief_due(self.state):
            return ""
        self.state["brief_day"] = today()
        return "the morning brief is due now (first round at or after 09:00 today): write it; "

    # -- coming home ("when I'm back home I want mochi to greet me", 2026-10-14). mochi-phone reports the zone changes
    # as triggers; a real return (out a while, not GPS jitter) gets one quiet line on the phone at the door, and the
    # pig is glad when they first sit down at the desk afterwards. Both are answers to them arriving, never news.

    HOME_LINES = {"morning": ["welcome home! 🐷", "you're back! oink"],
                  "day": ["welcome home! 🐷", "you're back! oink", "home again 🐷"],
                  "evening": ["welcome home 🐷 the evening's yours", "home! oink. kettle on?", "welcome back home 🐷"],
                  "night": ["home safe 🐷", "welcome home. sleep well 🐷"]}
    DESK_LINES = ["welcome home!", "you're home! oink", "there you are!"]

    def on_phone_zone(self, text):
        if text != "their phone: home":  # left home, or went from home straight to a named place
            if not self.state.get("left_home"):
                self.state["left_home"] = now()
            self.state["home_greet"] = 0
        else:
            left = self.state.get("left_home") or 0
            self.state["left_home"] = 0
            if not left or now() - left < HOME_OUT_MIN:
                return
            self.state["home_greet"] = now()
            h = dt.datetime.now().hour
            part = "night" if h < 5 else "morning" if h < 12 else "day" if h < 18 else "evening"
            if self.away():  # at the desk already (a phone that lagged): the pig says it instead
                self.tg.send(random.choice(self.HOME_LINES[part]))
                log("came home: greeted on the phone")
            else:
                self.greet_at_desk()

    def greet_at_desk(self):
        at = self.state.get("home_greet") or 0
        if not at:
            return
        self.state["home_greet"] = 0
        if now() - at > HOME_GREET_WITHIN:
            return
        self.pet.send(event="emote", emote="happy")
        self.pet.say(random.choice(self.DESK_LINES), 4)
        log("came home: the pig greeted them at the desk")

    def trigger(self, text, urgent=False, source="trigger", wake=True):
        """Something happened that may deserve a round before the clock says so. The relay notes it for Claude and,
        if the rationing allows (maybe_wake), starts a round with it as the reason; what to do about it is Claude's."""
        if text.startswith(("their phone: ", "their phone left ")):  # mochi-phone's zone changes
            self.on_phone_zone(text)
        self.event(f"[{source}] {text}")
        if wake:
            self.state["wake"] = (self.state["wake"] + [{"text": text[:500], "urgent": bool(urgent), "at": now(),
                                                         "source": source}])[-10:]
            self.maybe_wake()

    def maybe_wake(self):
        """Start the round a trigger asked for, once nothing is running and the rationing allows: a cooldown after
        the last round, a daily cap, and nothing while they're away at night, except for one of Mochi's own watches
        (an alarm it set on purpose; "unless a watch fires", 2026-10-14). Urgent triggers skip all three."""
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
            if self.away() and (self.tg.quiet_now() or night_now()) and not any(w.get("source") == "watch" for w in pend):
                return
        reasons = "; ".join(w["text"] for w in pend)
        self.state["wake"] = []
        self.bump("triggered")
        self.start_round(f"woken early by {'an URGENT trigger' if urgent else 'a trigger'}, not the clock: {reasons}",
                         force=urgent)

    def mail_pulled(self):
        """True once after every completed mail pull (mochi-mail.timer touches maildir/.last-sync)."""
        try:
            m = (maildir() / ".last-sync").stat().st_mtime
        except (OSError, TypeError):
            return False
        changed = m != self.mail_mtime
        self.mail_mtime = m
        return changed

    def maybe_catchup(self):
        """Read what's new as soon as it's ready: after any run in progress, a cooldown apart, within a daily cap.
        Past the cap, the news goes to the next round as events instead."""
        if self.busy or not self.tasks.empty():
            return
        chats, items = self.feed.ready()
        if not (chats or items) or now() - self.feed.state["last_catchup"] < CATCHUP_COOLDOWN:
            return
        asleep = self.away() and night_now()  # the news keeps until morning: it goes to the first round as events
        if asleep or self.count("catchups") >= CATCHUP_PER_DAY:
            if not asleep and self.catchup_capped != today():
                self.catchup_capped = today()
                log(f"catch-ups capped for today ({CATCHUP_PER_DAY}); what's new goes to the next round")
            for did, e in chats:
                self.event(f"[feed] {e['n']} new Telegram message(s) in {e['name']} (id {did}) since "
                           f"{dt.datetime.fromtimestamp(e['since']):%H:%M}")
            for _, it in items:
                self.event(f"[feed] {it['line']}")
            self.feed.pop([d for d, _ in chats], [k for k, _ in items])
            return
        self.start_catchup(chats, items)

    def start_catchup(self, chats, items=()):
        self.bump("catchups")
        last_look = max(self.feed.state["last_catchup"], self.state["last_round"]) or now() - 3600
        self.feed.state["last_catchup"] = now()
        hours = round(min(6.0, max(0.25, (now() - last_look) / 3600)), 2)
        lines = [f"- Telegram, {e['name']} ({e['kind']}, id {did}): {e['n']} new message(s); read them with "
                 f"`mochi-sense telegram dialog {did} --since {dt.datetime.fromtimestamp(e['since']):%Y-%m-%dT%H:%M} --chars 400`"
                 for did, e in chats]
        lines += [f"- {it['line']}" for _, it in items[:40]]
        if len(items) > 40:
            lines.append(f"- … and {len(items) - 40} more of the same kind (`mochi-sense mail recent` etc.)")
        ctx = {"you": {"away": self.away(), "idle": human_age(self.activity.idle.seconds())},
               "pending_asks": [{"id": aid, "text": a["text"], "options": a.get("options")}
                                for aid, a in self.state["asks"].items()],
               "telegram_bot": "connected: while they're away your asks reach their phone" if self.tg.on else "not set up"}
        prompt = CATCHUP_PROMPT.format(now=dt.datetime.now().strftime("%A %Y-%m-%d %H:%M"), news="\n".join(lines),
                                       hours=hours, ctx=json.dumps(ctx, indent=1, ensure_ascii=False), block=BLOCK)
        self.feed.pop([d for d, _ in chats], [k for k, _ in items])
        self.tasks.put({"kind": "catchup", "title": "catch-up", "level": "round", "model": CATCHUP_MODEL,
                        "timeout": 600, "prompt": prompt})
        log("catch-up queued: " + ", ".join([f"{e['name']} ({e['n']})" for _, e in chats] +
                                            [it["line"].split(";")[0][:60] for _, it in items[:5]]))

    def context(self):
        """What the relay knows and Claude doesn't: presence, screen, sessions, events, pending asks, senses."""
        digest = SENSES / "digest.md"
        return {
            "hostname": socket.gethostname(),
            "you": dict(self.activity.summary(60), away=self.away(),
                        away_for=human_age(now() - self.state["away_since"]) if self.away() and self.state["away_since"] else ""),
            "browser": (("your own Chrome is available as the mcp__chrome__* tools" +
                         (" and is on their screen right now (they see what you do; `mochi-browser hide` takes it off)"
                          if BROWSER.shown() else
                          " (hidden, yours alone, logins persist in your profile; `mochi-browser show` puts it on "
                          "their screen when a page needs them)")) if BROWSER and BROWSER.server()
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
            "tools_refused": (self.denials_lately() or ["none this week"]) + [
                "(what runs reached for and the relay's allow-list refused, last 7 days. In a round, Bash is read-only "
                "and a chain (`a && b`, `a; b`) is refused if any part is: write your own files with Write/Edit, not "
                "`echo >>`, `sed -i` or a python heredoc. If something they said yes to was refused, tell them so.)"],
            "watches": self.watches.status(),
            "clocks": (self.clocks.status() if self.clocks else ["not running"]) + [
                "(the clocks in the kitchen and the corridor: you live in their landscape and your asks wait behind a "
                "dot over you there too; see CLAUDE.md 'On the clocks'. A touch or the kitchen lights mean they're home)"],
            "errands": self.errands.status() + [
                "(memory/errands.json: what needs them somewhere. The relay pokes them on the phone when they're out, "
                "near a place for it, free and the place open; see CLAUDE.md 'Errands on the way')"],
            "waking": "a round can start before the clock: `mochi-brain --trigger TEXT`, the relay's own watchers "
                      "(disk, battery, failed units, stuck sessions), or your watches in memory/watches.json "
                      "(see CLAUDE.md, 'Waking up early'); new messages, mail, calendar changes and recorded calls "
                      "start catch-ups of their own ('Keeping up')",
            "browser_since_last_look": f"`mochi-sense browser --hours "
                                       f"{round(min(12.0, max(0.25, (now() - (self.state['last_round'] or now() - 3600)) / 3600)), 2)}`",
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
        week = week_due(self.state)
        week_part = WEEK_PROMPT.format(week=week, stats=json.dumps(week_stats(week), ensure_ascii=False)) if week else ""
        prompt = DREAM_PROMPT.format(now=dt.datetime.now().strftime("%A %Y-%m-%d %H:%M"),
                                     last=dt.datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M") if last else "never",
                                     days=", ".join(f"journal/{p.name}" for p in days) or "none",
                                     keep=JOURNAL_KEEP // 86400, ctx=json.dumps(ctx, indent=1, ensure_ascii=False),
                                     week=week_part, block=BLOCK)
        self.tasks.put({"kind": "dream", "title": "dream", "level": "dream", "model": DREAM_MODEL,
                        "timeout": 1800, "prompt": prompt, "week": week})
        log(f"dream queued ({why}; {len(days)} journal days" + (f"; week note for {week}" if week else "") + ")")

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
                    res = claude_run(task["prompt"], task["level"], task.get("model"), task.get("timeout", 900),
                                     resume=task.get("resume") or None)
                    if task.get("resume") and not res["ok"] and "no conversation found" in res["error"].lower():
                        log(f"session {task['resume'][:8]} is gone; starting '{task['title'][:40]}' fresh")
                        res = claude_run(task.get("fresh") or task["prompt"], task["level"], task.get("model"),
                                         task.get("timeout", 900))
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
        self.note_denials(task, res)
        if kind in ("dream", "study", "workshop"):
            self.state["night_end"] = now()
        if kind == "dream" and res["ok"] and task.get("week"):
            self.state["week_report"] = task["week"]
        if kind not in OWN_WORK:
            comms("done", task=kind, title=task["title"], session=session, ask=task.get("ask"),
                  via=task.get("via") or "pig", ok=bool(res["ok"]))
        if not res["ok"]:
            if kind in OWN_WORK:
                self.event(f"your previous {kind} run failed: {res['error'][:200]}")
            else:
                path = self.write_report(task["title"], f"**This run failed.** {res['error']}\n\n{res['text']}")
                if task.get("via") == "telegram":
                    self.link(self.tg.send(f"hm, that didn't work: {res['error'][:300]}"), session or task.get("resume"))
                self.add_ask(f"hm, that didn't work ({task['title'][:30]})", REPORT_OPTIONS, path=path,
                             session=session, urgent=True, mirror=task.get("via") != "telegram", src="result")
            self.save()
            return
        if kind == "workshop":
            self.workshop_done(task, res)
        if kind in ("dream", "study", "workshop"):  # raises nothing; only an urgent say (or withdrawing a stale ask) gets through
            keep = ("withdraw", "say", "urgent") + (("report",) if kind == "dream" and task.get("week") else ())
            data = {k: data[k] for k in keep if k in data}
            if not data.get("urgent"):
                data.pop("say", None)
            if kind != "workshop":
                self.event("you dreamt (memory/ consolidated, memory/patterns.md updated; see today's journal)"
                           if kind == "dream" else "you studied them overnight (memory/portrait.md, memory/study.md)")
        if kind not in OWN_WORK:  # something you asked for or approved: the answer is one line in the bubble
            self.deliver_result(task, res, data, session)
        self.apply(data, kind, session)
        self.save()

    def deliver_result(self, task, res, data, session):
        """A finished task: its `say` is the line they see (the pig's bubble, or a message on the phone when they're
        away or asked from there); the full answer is filed as a report and listed under Recent..., where "Show me"
        and "Chat about it" still work. No "done" ask: they dismissed 12 of 27 of those unread (2026-10-04..13).
        Anything that needs them next is an ask the run itself puts in its block."""
        body, _ = strip_block(res["text"])
        path = self.write_report(task["title"], body)
        text = (data.get("say") or f"done: {task['title'][:40]}")[:160]
        rid = uuid.uuid4().hex[:8]
        comms("result", id=rid, text=text, path=str(path), via=task.get("via") or "pig")
        phone = task.get("via") == "telegram"
        if phone:  # they asked from their phone and are waiting there: the answer goes back whole
            self.link(self.tg.send(f"{text}\n\n{self.tg.report_body(path)}", buzz=True), session)
        elif self.away() or task.get("urgent"):  # they'll see the bubble too late: the line goes to the phone, silently
            self.link(self.tg.send(text, buzz=self.tg.may_buzz(bool(task.get("urgent")))), session)
        self.pet.say(text, 12)
        if self.clocks and str(task.get("via") or "").startswith("clock-"):  # asked at a clock: the line shows there
            self.clocks.say(text, rid)
        self.remember_closed(rid, {"text": text, "options": REPORT_OPTIONS, "do": "", "path": str(path),
                                   "session": session, "urgent": bool(task.get("urgent")), "at": now()}, "done")
        self.event(f"finished '{task['title'][:80]}': {text} (report filed: {path.relative_to(WORK)})")

    def note_denials(self, task, res):
        """What a run reached for and was refused: kept for `mochi-brain --denials`, shown to Mochi in its context,
        and said out loud when it happened in something they asked for (a yes that didn't execute is the worst
        failure: better to hear it than to find 'done' with nothing done)."""
        den = res.get("denials") or []
        if not den:
            return
        t = now()
        self.state["denials"] = (self.state["denials"] + [{"at": t, "kind": task["kind"], "title": task["title"][:60],
                                                           "what": d} for d in den])[-DENIALS_KEEP:]
        log(f"{task['kind']} '{task['title'][:40]}' was refused {len(den)} tool call(s): " + "; ".join(den)[:300])
        if task["kind"] not in OWN_WORK:
            self.event(f"your run for '{task['title'][:60]}' was refused: " + "; ".join(den)[:300]
                       + ". If that blocked what they asked for, they need to hear it, not 'done'.")

    def denials_lately(self, days=7, limit=12):
        """The last week's refusals, most frequent first, as lines for the context and the week note."""
        cutoff = now() - days * 86400
        seen = {}
        for d in self.state["denials"]:
            if d["at"] < cutoff:
                continue
            key = re.sub(r"\s+", " ", d["what"])[:90]
            seen.setdefault(key, {"n": 0, "kinds": set()})
            seen[key]["n"] += 1
            seen[key]["kinds"].add(d["kind"])
        rows = sorted(seen.items(), key=lambda kv: -kv[1]["n"])[:limit]
        return [f"{v['n']}× in {'/'.join(sorted(v['kinds']))}: {k}" for k, v in rows]

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
            if kind in ("round", "catchup", "discover"):
                self.add_ask(caption[:160] or path.name, REPORT_OPTIONS, path=path, session=session, src=kind)
            else:
                self.tg.send_file(path, caption=caption, photo=bool(f.get("photo")))  # the answer itself already buzzed
        say = (data.get("say") or "").strip()
        if say and kind in OWN_WORK:
            if data.get("urgent"):
                self.pet.say(say[:160], 10)
                log(f"URGENT: {say}")
                if self.clocks:
                    self.clocks.say(say[:160])
                self.tg.send("❗ " + say[:1000], buzz=self.tg.may_buzz(True, critical=True))
            else:
                log(f"quiet (not shown): {say}")

    def add_ask(self, text, options, do="", path=None, aid=None, session="", urgent=False, mirror=True, src="round",
                tg_text="", buzz=None):
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
        if tg_text:
            a["tg_text"] = tg_text[:600]
        self.state["asks"][aid] = a
        comms("ask", id=aid, text=text[:160], options=options, path=str(path or ""), src=src, urgent=urgent)
        self.pet.ask(aid, text[:160], options, urgent)
        if mirror and (urgent or self.away() or buzz):
            a["tg"] = self.tg.ask(aid, a, buzz=self.tg.may_buzz(urgent) if buzz is None else buzz)
        log(f"ask{' (urgent)' if urgent else ''}{' → phone' if a['tg'] else ''}: {text[:160]}  [{' | '.join(options)}]")

    def drain_errands(self):
        """What the errand check found: a poke to raise, or one they walked past to take back."""
        while not self.errands.ready.empty():
            kind, info = self.errands.ready.get_nowait()
            if kind == "poke":
                e, p = info["e"], info["place"]
                aid = f"errand-{ERRAND.eid(e)}"
                text, tg_text, app_text = self.errands.poke_text(e, p, info["free"])
                self.errands.poked(info, aid)
                geo = f"geo:{p['lat']:.6f},{p['lon']:.6f}?q={p['lat']:.6f},{p['lon']:.6f}({urllib.parse.quote(p['name'][:40])})"
                by_app = self.errands.app_fresh() and phone_send(
                    "ask", app_text, "--options", ",".join(ERRAND_OPTIONS), "--id", aid, "--link", geo, "--buzz")
                self.errands.state["pending"]["app"] = by_app
                self.add_ask(text, ERRAND_OPTIONS, aid=aid, src="errand", buzz=None if by_app else True,
                             mirror=not by_app, tg_text=tg_text,
                             do=f"a location errand ({ERRAND.eid(e)} in memory/errands.json): {e['do']}. "
                                f"Their reply is about doing it at {p['name']}; act on what they said "
                                f"(tick the linked task if it's done, change or remove the errand).")
                if not by_app and not self.tg.on:
                    self.pet.say(text[:160], 8)  # no phone line: the pig is the only way to reach them
                log(f"errand poke: {e['do']} at {p['name']} ({p['d']:.0f} m)")
            elif kind == "passed":
                aid = info["aid"]
                a = self.state["asks"].pop(aid, None)
                self.errands.state["pending"] = None
                if info.get("app"):
                    phone_send("close", "--id", aid)
                if a:
                    self.pet.withdraw(aid)
                    self.tg.close(a, "walked on")
                    comms("closed", id=aid, how="walked on")
                    self.event(f"errand poke unanswered, they walked on from {info['place']['name']} after "
                               f"{human_age(now() - info['at'])}: {info['do']}")
            self.save()

    def write_report(self, title, body):
        stamp = dt.datetime.now()
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-")[:40] or "report"
        path = REPORTS / f"{stamp:%Y%m%d-%H%M%S}-{slug}.md"
        path.write_text(f"# {title}\n\n_{stamp:%Y-%m-%d %H:%M}_\n\n{body.strip()}\n")
        return path

    def housekeeping(self):
        t = now()
        self.state["threads"] = {k: v for k, v in self.state["threads"].items() if t - v["at"] < THREAD_KEEP}
        for aid, a in list(self.state["asks"].items()):
            if t - a["at"] > (2 * 3600 if a.get("urgent") else 3 * 86400):
                self.pet.withdraw(aid)
                self.state["asks"].pop(aid)
                comms("closed", id=aid, how="expired")
                self.remember_closed(aid, a, "expired")
                self.tg.close(a, "expired")
                if aid.startswith("workshop-") and WS:  # no Undo in three days: it stays, and goes into main
                    threading.Thread(target=self.workshop_answer, args=(a, "keep it"), daemon=True).start()
                self.event(f"expired unanswered: {a['text']}")
        for aid, a in self.state["asks"].items():  # re-send, in case the pig restarted
            self.pet.ask(aid, a["text"], a.get("options") or REPORT_OPTIONS, a.get("urgent", False))
        for old in [*REPORTS.glob("*.md"), *REPORTS.glob("*.html")]:
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

def claude_run(prompt, level, model=None, timeout=900, resume=None):
    cmd = [CLAUDE] + (browser_args(headed=False) if level in ("round", "approved") else []) + ["-p", "--output-format", "json", "--permission-mode", "dontAsk",
                                                     "--allowedTools", ",".join(LEVELS[level]), "--disallowedTools", ",".join(NEVER)]
    if model:
        cmd += ["--model", model]
    if resume:
        cmd += ["--resume", resume]
    env = dict(os.environ, MOCHI_BRAIN="1")  # the pig's hook relay marks these as Mochi's own runs
    env.pop("CLAUDECODE", None)
    t0 = now()
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=str(WORK), env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": "", "error": f"timed out after {timeout}s", "secs": now() - t0, "cost": 0}
    except OSError as e:
        return {"ok": False, "text": "", "error": str(e), "secs": now() - t0, "cost": 0}
    finally:
        if BROWSER:
            BROWSER.stop()  # the run's hidden Chrome; one on their screen, or one a chat is using, stays
    try:
        j = json.loads(p.stdout)
    except ValueError:
        j = {}
    text = (j.get("result") if isinstance(j, dict) else None) or p.stdout.strip()
    ok = p.returncode == 0 and not (isinstance(j, dict) and j.get("is_error"))
    err = "" if ok else (p.stderr.strip()[-600:] or text[-600:] or f"exit {p.returncode}")
    return {"ok": ok, "text": text, "error": err, "secs": now() - t0,
            "cost": float(j.get("total_cost_usd") or 0) if isinstance(j, dict) else 0,
            "session": str(j.get("session_id") or "") if isinstance(j, dict) else "",
            "denials": denials_of(j)}


def denials_of(j):
    """The tools a run reached for and was refused (Claude Code's `permission_denials` in its JSON result), each as
    one short line: the tool, and for Bash the command's first words."""
    out = []
    for d in (j.get("permission_denials") or []) if isinstance(j, dict) else []:
        if not isinstance(d, dict):
            continue
        tool = str(d.get("tool_name") or "?")
        inp = d.get("tool_input") or {}
        if tool == "Bash":
            what = str(inp.get("command") or "").strip().replace("\n", " ")
        else:
            what = " ".join(f"{k}={str(v)[:40]}" for k, v in list(inp.items())[:3]) if isinstance(inp, dict) else str(inp)
        out.append(f"{tool}: {what[:160]}")
    return out


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
        if a == "--answer":  # an answer to an ask from somewhere else (mochi-phone: a button on their phone's notification)
            if len(argv) < 4:
                print("usage: mochi-brain --answer ASK_ID LABEL", file=sys.stderr)
                return 1
            return 0 if send_brain(event="answer", id=argv[2], answer=" ".join(argv[3:]), via="phone") else 1
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
        if a == "--feed":
            try:
                rs = json.loads(STATE_FILE.read_text())
            except (OSError, ValueError):
                rs = {}
            f = rs.get("feed") or {}
            at = lambda ts: dt.datetime.fromtimestamp(ts).strftime("%a %H:%M") if ts else "never"
            print(f"Feed: Telegram last polled {at(f.get('last_poll'))}, {len(f.get('counts') or {})} chats tracked; "
                  f"mail {'tracked' if f.get('mail_seen') is not None else 'not yet seen'}; "
                  f"calls {'tracked' if f.get('calls') is not None else 'not yet seen'}; "
                  f"calendar {'tracked' if f.get('cal') is not None else 'not set up (calendar in sources.json)'}")
            print(f"Last catch-up {at(f.get('last_catchup'))}, {(rs.get('counts') or {}).get('catchups', 0)} today "
                  f"(max {CATCHUP_PER_DAY})")
            for src, d in (f.get("down") or {}).items():
                if d.get("since"):
                    print(f"{Feed.LABELS.get(src, src)} UNREADABLE since {at(d['since'])}")
            for did, e in (f.get("new") or {}).items():
                print(f"  waiting: Telegram {e['name']} ({did}): {e['n']} new since {at(e['since'])}")
            for key, it in (f.get("items") or {}).items():
                print(f"  waiting: {it['line'].split(';')[0]}")
            try:
                print("muted: " + ", ".join(json.loads(FEED_FILE.read_text()).get("mute") or []))
            except (OSError, ValueError, AttributeError):
                pass
            return 0
        if a == "--denials":  # what runs reached for and were refused: the allow-list audit, kept current by the relay
            try:
                den = json.loads(STATE_FILE.read_text()).get("denials") or []
            except (OSError, ValueError):
                den = []
            days = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else 7
            fake = type("R", (), {"state": {"denials": den}})()
            rows = Relay.denials_lately(fake, days=days, limit=40)
            print(f"Tools refused in the last {days} days ({sum(1 for d in den if d['at'] > now() - days * 86400)} calls):")
            for r in rows:
                print("  " + r)
            if not rows:
                print("  none recorded")
            print("A round's Bash is read-only (prefixes in READ_TOOLS); an approved run has Bash but only the MCP tools listed;\n"
                  "NEVER holds what no run may do. Edit brain/mochi_brain.py, then `make install` and restart mochi-brain.")
            return 0
        if a == "--week":  # the relay's numbers for a week (last week unless given: 2026-W41), as the week note sees them
            week = argv[2] if len(argv) > 2 else week_due({}) or ""
            print(json.dumps(week_stats(week), indent=1, ensure_ascii=False))
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
              "       mochi-brain --feed          the live feed (Telegram, mail, calls, calendar): what's waiting for a catch-up\n"
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
              "       mochi-brain --denials [DAYS]  tools Mochi's runs reached for and were refused (the allow-list audit)\n"
              "       mochi-brain --week [YYYY-Www] the relay's numbers for a week (what the Monday-night week note is built on)\n"
              "       mochi-telegram status       the Telegram bot: token, owner, reachable? (asks go to your phone when away)\n"
              "       mochi-tg status             Telegram as you (mochi-tg send asks on your phone first; `mochi-tg auth` once)\n"
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
