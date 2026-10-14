#!/usr/bin/env python3
"""mochi-tg — Telegram in your name: Mochi sends a message from your own account, once you allow it on your phone.

    mochi-tg auth                     you: sign in once (phone number, the code Telegram sends you, your 2FA password)
    mochi-tg logout                   you: sign that session out again
    mochi-tg status                   signed in as whom, is the relay there to ask you, what's allowed
    mochi-tg dialogs [--limit N] [QUERY]   your chats with their ids and names (what Mochi may address)
    mochi-tg send WHO TEXT [--why TEXT] [--reply-to MSG_ID] [--dry-run]
                                      Mochi: send TEXT to WHO (a dialog id, @username, or a name), after your tap
    mochi-tg log [N]                  what went out, newest last

Why this exists. Mochi reads your Telegram through the archive (mochi-sense telegram) but could only hand you lines to
paste: "ask Nastya where the contract is" ended with a paste-ready message three times in one week (2026-10-07..13).
This is the other half: it writes in your voice, you approve each message with one tap, and it goes out from your
account, in the thread, so the other person sees you.

How it holds together. The session is a Telethon user session in ~/.config/mochi/telegram-user.session, created by
`auth` (your hands: Telegram sends the code to your phone). The API id and hash are your own app's, read from
~/.config/mochi/telegram-api.json. `send` resolves WHO, then asks the relay (mochi-brain) to put the message on your
phone with Send it / Deny; it waits for the relay to answer over a socket of its own and trusts only an answer from
the relay's own process (peer pid against mochi-brain.service), like mochi-vault. Then it sends, and logs to
actions.log and ~/.local/share/mochi/tg-sent.log. No tap, no message. The relay rations the questions
(TG_SEND_ASKS_PER_DAY) and the brief (CLAUDE.md, "Telegram as them") says when Mochi may ask at all: only for what
they asked for or said yes to, in their voice, never money or commitments.

The same trade as the vault's "remember": the session file is readable by any program running as you, Mochi's approved
runs included, so the tap guards against mistakes, not against a run set on getting around it. `mochi-tg auth` and
`logout` are on the relay's never-list; sending from a round is not allowed at all (approved runs only).

Telethon lives in the mochi-tools venv (`make tools`); this script re-runs itself with that Python when needed.
"""

import argparse
import asyncio
import datetime as dt
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "mochi"
API_FILE = CONFIG_DIR / "telegram-api.json"
SESSION = CONFIG_DIR / "telegram-user"  # Telethon adds .session
WORK = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "mochi"
LOG_FILE = WORK / "actions.log"
SENT_LOG = WORK / "tg-sent.log"
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
BRAIN_SOCK = RUNTIME / "mochi-brain.sock"
RELAY_UNIT = "mochi-brain.service"
TOOLS_PY = HOME / ".local/share/mochi-tools/bin/python3"
ASK_TIMEOUT = 8 * 60   # how long a send waits for the tap
MAX_TEXT = 4000


def log(text):
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M} tg: {text}\n")
    except OSError:
        pass


def api_creds():
    try:
        d = json.loads(API_FILE.read_text())
        return int(d["api_id"]), str(d["api_hash"])
    except (OSError, ValueError, KeyError, TypeError):
        return None, None


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


def tell_relay(**msg):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.sendto(json.dumps(msg).encode(), str(BRAIN_SOCK))
        return True
    except OSError:
        return False
    finally:
        s.close()


def ask_on_phone(to, text, why, timeout=ASK_TIMEOUT, relay_pid=relay_pid, notify=tell_relay):
    """Put "send this as you?" on their phone through the relay and wait for the tap. Returns (allowed, reason, rid).
    The answer arrives on a socket of our own; only the relay's process may answer."""
    rid = secrets.token_hex(8)
    path = RUNTIME / f"mochi-tg-{rid}.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(path))
        os.chmod(path, 0o600)
        srv.listen(1)
        srv.settimeout(timeout)
        if not notify(event="tg_send_ask", id=rid, to=to[:80], text=text[:1500], why=(why or "")[:300], sock=str(path)):
            return False, "mochi-brain isn't running, so there's no way to ask them", rid
        log(f"asking on the phone to send a message to {to[:60]}" + (f" ({why[:80]})" if why else ""))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                break
            with conn:
                conn.settimeout(10)
                rp = relay_pid()
                pp = peer_pid(conn)
                try:
                    data = b""
                    while not data.endswith(b"\n") and len(data) < 65536:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    m = json.loads(data or b"{}")
                except (OSError, ValueError):
                    m = {}
                if not rp or pp != rp or m.get("id") != rid:
                    log(f"ignored an answer from pid {pp} (the relay is {rp or 'not running'})")
                    try:
                        conn.sendall(b'{"ok": false, "text": "only the relay decides"}\n')
                    except OSError:
                        pass
                    continue
                try:
                    conn.sendall(b'{"ok": true, "text": "noted"}\n')
                except OSError:
                    pass
                return bool(m.get("allow")), str(m.get("reason") or ""), rid
        notify(event="tg_send_done", id=rid, label="⌛ no answer in time, not sent")
        return False, "they didn't answer in time", rid
    finally:
        srv.close()
        try:
            path.unlink()
        except OSError:
            pass


# -- Telethon (loaded lazily, from the mochi-tools venv when the system Python lacks it)

def need_telethon():
    try:
        import telethon  # noqa: F401
        return True
    except ImportError:
        if TOOLS_PY.exists() and not os.environ.get("MOCHI_TG_REEXEC"):  # once: the venv's python is a symlink to the system one
            os.environ["MOCHI_TG_REEXEC"] = "1"
            os.execv(str(TOOLS_PY), [str(TOOLS_PY), *sys.argv])
        print(f"telethon is missing: run `make tools` in the xpet repo (or {TOOLS_PY.parent}/pip install telethon)",
              file=sys.stderr)
        return False


def client():
    from telethon import TelegramClient
    api_id, api_hash = api_creds()
    if not api_id:
        raise SystemExit(f"no API credentials: put {{\"api_id\": ..., \"api_hash\": \"...\"}} in {API_FILE} "
                         "(my.telegram.org → API development tools; the Telegram archive's own pair works)")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(SESSION), api_id, api_hash, device_model="Mochi (desk)", app_version="mochi-tg 1")


def name_of(entity):
    parts = [getattr(entity, "first_name", "") or "", getattr(entity, "last_name", "") or ""]
    name = " ".join(p for p in parts if p).strip() or getattr(entity, "title", "") or ""
    user = getattr(entity, "username", "") or ""
    return name + (f" (@{user})" if user and user.lower() not in name.lower() else "")


def kind_of(entity):
    from telethon.tl.types import Channel, Chat, User
    if isinstance(entity, User):
        return "bot" if getattr(entity, "bot", False) else "user"
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, Channel):
        return "group" if getattr(entity, "megagroup", False) else "channel"
    return "?"


async def resolve(c, who):
    """WHO as a dialog id (the archive's ids: 1169722994, -100xxxx), @username, or a name: the entity and its name.
    Ids need the dialog list once so Telethon knows the access hash; names match the dialog list only."""
    from telethon.errors import RPCError
    from telethon.utils import get_peer_id
    w = who.strip()
    try:
        wid = int(w)
    except ValueError:
        wid = None
    if wid is None and w.startswith("@"):
        ent = await c.get_entity(w)
        return ent, name_of(ent)
    dialogs = await c.get_dialogs(limit=400)
    if wid is not None:
        for d in dialogs:
            if d.entity is not None and (d.id == wid or get_peer_id(d.entity) == wid or getattr(d.entity, "id", None) == abs(wid)):
                return d.entity, name_of(d.entity)
        try:
            ent = await c.get_entity(wid)
            return ent, name_of(ent)
        except (RPCError, ValueError):
            raise SystemExit(f"no dialog with id {wid} among your {len(dialogs)} chats")
    hits = [d for d in dialogs if d.entity is not None and w.lower() in (d.name or name_of(d.entity)).lower()]
    if len(hits) == 1:
        return hits[0].entity, name_of(hits[0].entity)
    if not hits:
        raise SystemExit(f"nobody called '{w}' among your chats (mochi-tg dialogs {w!r} to look)")
    raise SystemExit("ambiguous: " + "; ".join(f"{d.id} {d.name}" for d in hits[:8]) + " — use the id")


async def do_status(c):
    await c.connect()
    if not await c.is_user_authorized():
        print(f"not signed in: run `mochi-tg auth` (session file {SESSION}.session)")
        return 1
    me = await c.get_me()
    print(f"signed in as {name_of(me)} (id {me.id}, +{me.phone})")
    print(f"relay:    {'running (pid %d)' % relay_pid() if relay_pid() else 'NOT RUNNING: nothing can be asked, so nothing can be sent'}")
    print(f"sent log: {SENT_LOG} ({sum(1 for _ in open(SENT_LOG)) if SENT_LOG.exists() else 0} messages)")
    return 0


async def do_dialogs(c, query, limit):
    await c.connect()
    if not await c.is_user_authorized():
        raise SystemExit("not signed in: mochi-tg auth")
    rows = []
    for d in await c.get_dialogs(limit=limit):
        if d.entity is None:
            continue
        k = kind_of(d.entity)
        if k in ("bot", "channel"):
            continue
        name = d.name or name_of(d.entity)
        if query and query.lower() not in name.lower():
            continue
        when = d.date.astimezone().strftime("%Y-%m-%d") if d.date else ""
        rows.append((d.id, k, name, when))
    for did, k, name, when in rows:
        print(f"{did:>15}  {k:5}  {name[:50]:50}  last {when}")
    if not rows:
        print("no matching dialogs")
    return 0


async def do_send(c, who, text, why, reply_to, dry_run, ask=ask_on_phone):
    await c.connect()
    if not await c.is_user_authorized():
        raise SystemExit("not signed in: mochi-tg auth (their hands)")
    if not text.strip():
        raise SystemExit("empty message")
    if len(text) > MAX_TEXT:
        raise SystemExit(f"too long ({len(text)} chars; Telegram takes {MAX_TEXT})")
    ent, name = await resolve(c, who)
    k = kind_of(ent)
    if k in ("bot", "channel"):
        raise SystemExit(f"{name} is a {k}: Mochi writes to people and groups only")
    print(f"to: {name} ({k}, id {ent.id})\n---\n{text}\n---")
    if dry_run:
        print("(dry run: nothing asked, nothing sent)")
        return 0
    allowed, reason, rid = ask(name, text, why)
    if not allowed:
        log(f"NOT sent to {name}: {reason}")
        print(f"not sent: {reason}")
        return 2
    try:
        msg = await c.send_message(ent, text, reply_to=int(reply_to) if reply_to else None)
    except Exception as e:  # the tap was given; say plainly that it still didn't go
        tell_relay(event="tg_send_done", id=rid, label=f"✗ allowed, but Telegram refused: {str(e)[:60]}")
        log(f"allowed but NOT sent to {name}: {str(e)[:160]}")
        raise SystemExit(f"allowed, but sending failed: {e}")
    stamp = dt.datetime.now()
    try:
        with open(SENT_LOG, "a") as f:
            f.write(f"{stamp:%Y-%m-%d %H:%M} → {name} (id {ent.id}, msg {msg.id}): {text[:200].replace(chr(10), ' ')}\n")
    except OSError:
        pass
    log(f"SENT to {name} (msg {msg.id}, {len(text)} chars)" + (f": {why[:80]}" if why else ""))
    tell_relay(event="tg_send_done", id=rid, label=f"✓ sent to {name}")
    print(f"sent (message {msg.id})")
    return 0


async def do_auth(c):
    await c.connect()
    if await c.is_user_authorized():
        me = await c.get_me()
        print(f"already signed in as {name_of(me)}; `mochi-tg logout` first to change")
        return 0
    phone = input("your phone number (with country code, e.g. +31...): ").strip()
    await c.send_code_request(phone)
    code = input("the code Telegram just sent you: ").strip().replace(" ", "")
    from telethon.errors import SessionPasswordNeededError
    try:
        await c.sign_in(phone, code)
    except SessionPasswordNeededError:
        import getpass
        await c.sign_in(password=getpass.getpass("your two-step verification password: "))
    me = await c.get_me()
    await c.get_dialogs(limit=400)  # cache the entities, so ids from the archive resolve at once
    try:
        os.chmod(f"{SESSION}.session", 0o600)
    except OSError:
        pass
    print(f"signed in as {name_of(me)}. Mochi can now ask you, on your phone, to send messages as you.")
    log(f"signed in as {name_of(me)} (their hands)")
    return 0


async def do_logout(c):
    await c.connect()
    if await c.is_user_authorized():
        await c.log_out()
    for p in (Path(f"{SESSION}.session"), Path(f"{SESSION}.session-journal")):
        p.unlink(missing_ok=True)
    print("signed out; the session file is gone")
    log("signed out (their hands)")
    return 0


def cmd_log(n):
    if not SENT_LOG.exists():
        print("nothing sent yet")
        return 0
    lines = SENT_LOG.read_text().splitlines()
    print("\n".join(lines[-n:]))
    return 0


def main():
    ap = argparse.ArgumentParser(prog="mochi-tg", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth")
    sub.add_parser("logout")
    sub.add_parser("status")
    d = sub.add_parser("dialogs")
    d.add_argument("query", nargs="?", default="")
    d.add_argument("--limit", type=int, default=300)
    s = sub.add_parser("send")
    s.add_argument("who")
    s.add_argument("text")
    s.add_argument("--why", default="")
    s.add_argument("--reply-to", default="")
    s.add_argument("--dry-run", action="store_true")
    lg = sub.add_parser("log")
    lg.add_argument("n", nargs="?", type=int, default=20)
    a = ap.parse_args()
    if a.cmd == "log":
        return cmd_log(a.n)
    if not need_telethon():
        return 1
    c = client()  # not `with c:` — that would start an interactive sign-in whenever the session is missing
    try:
        if a.cmd == "auth":
            return c.loop.run_until_complete(do_auth(c))
        if a.cmd == "logout":
            return c.loop.run_until_complete(do_logout(c))
        if a.cmd == "status":
            return c.loop.run_until_complete(do_status(c))
        if a.cmd == "dialogs":
            return c.loop.run_until_complete(do_dialogs(c, a.query, a.limit))
        if a.cmd == "send":
            return c.loop.run_until_complete(do_send(c, a.who, a.text, a.why, a.reply_to, a.dry_run))
    finally:
        if c.is_connected():
            c.disconnect()  # Telethon runs it on its loop when called outside one
    return 1


if __name__ == "__main__":
    sys.exit(main())
