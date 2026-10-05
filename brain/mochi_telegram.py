#!/usr/bin/env python3
"""mochi-telegram — Mochi's line to your phone: a Telegram bot that talks to you and only you.

    mochi-telegram status            is the token there, who is the owner, can the bot reach them
    mochi-telegram pair [--write]    wait for you to message the bot, print your id (--write saves it as owner_id)
    mochi-telegram send TEXT [--buzz]   a test message (silent unless --buzz)
    mochi-telegram file PATH... [--caption TEXT] [--photo] [--buzz]   send files (documents; --photo = as pictures)
    mochi-telegram updates           pending updates, for debugging

The relay (mochi-brain) imports this file and does the real work: while you're away from the computer it mirrors
Mochi's asks to the phone as messages with buttons, carries your taps and typed messages back, and lets an urgent
`say` buzz your pocket. Files go both ways: what you send the bot (photos, documents) lands in Mochi's workspace
inbox/ and becomes a task; Mochi's reports and files reach you with `file`. This file only knows the Bot API.

Config: the token lives in ~/.config/mochi/telegram.token (or `telegram.token_file` / `telegram.token` in
~/.config/mochi/sources.json); your own Telegram user id is `telegram.owner_id` there. Messages from anyone else
are ignored. Nothing here prints the token.
"""

import argparse
import datetime as dt
import json
import mimetypes
import os
import sys
import uuid
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "mochi"
CONFIG = CONFIG_DIR / "sources.json"
TOKEN_FILE = CONFIG_DIR / "telegram.token"
MAX_TEXT = 4000  # Telegram's limit is 4096; leave room for the answer line an edit appends
MAX_UPLOAD = 50 * 2**20    # Bot API: 50 MB per sent file
MAX_DOWNLOAD = 20 * 2**20  # Bot API: 20 MB per received file
PHOTO_TYPES = {".jpg", ".jpeg", ".png", ".webp"}


class TgError(Exception):
    def __init__(self, code, description):
        super().__init__(f"{code}: {description}")
        self.code, self.description = code, description

    @property
    def unreachable(self):
        """You haven't opened the bot yet (or blocked it): the bot can't start the conversation."""
        d = self.description.lower()
        return "chat not found" in d or "blocked" in d or "can't initiate" in d or "deactivated" in d


def load_cfg():
    try:
        return json.loads(CONFIG.read_text()).get("telegram", {}) or {}
    except (OSError, ValueError):
        return {}


class Bot:
    def __init__(self):
        cfg = load_cfg()
        self.token, self.owner, self.why = "", 0, ""
        tf = Path(os.path.expanduser(cfg.get("token_file") or TOKEN_FILE))
        if cfg.get("token"):
            self.token = str(cfg["token"]).strip()
        elif tf.is_file():
            self.token = tf.read_text().strip()
        try:
            self.owner = int(cfg.get("owner_id") or 0)
        except (TypeError, ValueError):
            self.owner = 0
        if not self.token:
            self.why = f"no bot token (create one with @BotFather and put it in {tf})"
        elif not self.owner:
            self.why = f"no owner: run `mochi-telegram pair --write` and message the bot (telegram.owner_id in {CONFIG})"
        self.enabled = not self.why

    # -- the Bot API

    def api(self, method, http_timeout=25, **params):
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{method}",
                                     data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as r:
                j = json.load(r)
        except urllib.error.HTTPError as e:
            try:
                j = json.load(e)
            except ValueError:
                j = {"ok": False, "error_code": e.code, "description": str(e)}
        if not j.get("ok"):
            raise TgError(j.get("error_code", 0), j.get("description", "unknown error"))
        return j.get("result")

    def me(self):
        return self.api("getMe")

    def send(self, text, buttons=None, buzz=False, reply_to=None):
        """Send text to the owner, in chunks if long; buttons (rows of (label, data)) go on the last chunk.
        Returns the id of the message that carries the buttons (the last one)."""
        chunks = split_text(text) or ["(empty)"]
        mid = None
        for i, chunk in enumerate(chunks):
            params = {"chat_id": self.owner, "text": chunk, "disable_notification": not buzz}
            if reply_to and i == 0:
                params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            if buttons and i == len(chunks) - 1:
                params["reply_markup"] = keyboard(buttons)
            mid = self.api("sendMessage", **params)["message_id"]
        return mid

    def send_file(self, path, caption="", buzz=False, reply_to=None, photo=False):
        """Send one file to the owner: as a document (unchanged bytes, the default) or as a photo (Telegram
        recompresses it; only for pictures). Returns the message id."""
        path = Path(path)
        size = path.stat().st_size
        if size > MAX_UPLOAD:
            raise TgError(413, f"{path.name} is {size // 2**20} MB; the Bot API takes up to 50 MB")
        photo = photo and path.suffix.lower() in PHOTO_TYPES and size <= 10 * 2**20
        fields = {"chat_id": str(self.owner), "disable_notification": "false" if buzz else "true"}
        if caption:
            fields["caption"] = caption[:1024]
        if reply_to:
            fields["reply_parameters"] = json.dumps({"message_id": reply_to, "allow_sending_without_reply": True})
        body, ctype = multipart(fields, "photo" if photo else "document", path.name, path.read_bytes())
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{'sendPhoto' if photo else 'sendDocument'}",
                                     data=body, headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                j = json.load(r)
        except urllib.error.HTTPError as e:
            try:
                j = json.load(e)
            except ValueError:
                j = {"ok": False, "error_code": e.code, "description": str(e)}
        if not j.get("ok"):
            raise TgError(j.get("error_code", 0), j.get("description", "unknown error"))
        return j["result"]["message_id"]

    def download(self, att, dest_dir):
        """Fetch a received attachment (see `attachment`) into dest_dir. Returns the saved path."""
        if att["size"] and att["size"] > MAX_DOWNLOAD:
            raise TgError(413, f"{att['name']} is {att['size'] // 2**20} MB; the Bot API lets me fetch up to 20 MB")
        info = self.api("getFile", file_id=att["file_id"])
        url = f"https://api.telegram.org/file/bot{self.token}/{info['file_path']}"
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        name = safe_name(att["name"]) or f"file{Path(info['file_path']).suffix}"
        dest = dest_dir / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{name}"
        with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
        return dest

    def edit(self, message_id, text, buttons=None):
        params = {"chat_id": self.owner, "message_id": message_id, "text": text[:4096]}
        if buttons:
            params["reply_markup"] = keyboard(buttons)
        try:
            self.api("editMessageText", **params)
        except TgError as e:
            if "not modified" not in e.description:
                raise

    def typing(self):
        self.api("sendChatAction", chat_id=self.owner, action="typing")

    def answer_callback(self, callback_id, text=""):
        try:
            self.api("answerCallbackQuery", callback_query_id=callback_id, text=text[:200])
        except TgError:
            pass

    def poll(self, offset, timeout=50):
        """Long-poll for updates; `timeout` is Telegram's wait, the HTTP timeout is a little longer."""
        return self.api("getUpdates", http_timeout=timeout + 10, offset=offset, timeout=timeout,
                        allowed_updates=["message", "callback_query"])

    def is_owner(self, update_part):
        return int(((update_part or {}).get("from") or {}).get("id") or 0) == self.owner


def multipart(fields, file_field, filename, data):
    """A multipart/form-data body: text fields plus one file. Returns (body, content_type)."""
    boundary = "mochi" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{safe_name(filename)}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n").encode()
    out += data + f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def safe_name(name):
    """A file name with nothing that could escape a directory or confuse a shell."""
    name = os.path.basename(str(name or "")).replace('"', "")
    return "".join(c if c.isalnum() or c in "._- ()" else "_" for c in name).strip(" .")[:120]


def attachment(m):
    """What a message carries besides text: {"kind", "file_id", "name", "size", "mime"} or None. A photo comes
    as several sizes; the largest is taken."""
    m = m or {}
    if m.get("photo"):
        best = max(m["photo"], key=lambda p: p.get("file_size") or 0)
        return {"kind": "photo", "file_id": best["file_id"], "name": f"photo.jpg", "size": best.get("file_size") or 0,
                "mime": "image/jpeg"}
    for kind in ("document", "video", "audio", "voice", "video_note", "animation"):
        d = m.get(kind)
        if d:
            ext = {"voice": ".ogg", "video_note": ".mp4", "audio": ".mp3", "video": ".mp4", "animation": ".mp4"}.get(kind, "")
            name = d.get("file_name") or (kind + ext)
            return {"kind": kind, "file_id": d["file_id"], "name": name, "size": d.get("file_size") or 0,
                    "mime": d.get("mime_type") or mimetypes.guess_type(name)[0] or ""}
    return None


def keyboard(rows):
    """rows: list of rows, each a list of (label, callback_data)."""
    return {"inline_keyboard": [[{"text": str(l)[:40], "callback_data": str(d)[:64]} for l, d in row] for row in rows]}


def split_text(text, limit=MAX_TEXT):
    text = (text or "").strip()
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        out.append(text)
    return out


def who(m):
    f = (m or {}).get("from") or {}
    name = " ".join(x for x in (f.get("first_name"), f.get("last_name")) if x)
    return f"{f.get('id')} ({name}{' @' + f['username'] if f.get('username') else ''})"


# ---- command line ------------------------------------------------------------------------------------

def cmd_status(bot):
    print(f"token: {'present' if bot.token else 'MISSING'}  owner_id: {bot.owner or 'not set'}  config: {CONFIG}")
    if not bot.token:
        return 1
    try:
        me = bot.me()
        print(f"bot: @{me.get('username')} ({me.get('first_name')})")
    except (TgError, OSError) as e:
        print(f"bot: cannot reach the Bot API: {e}")
        return 1
    if not bot.owner:
        print(bot.why)
        return 1
    try:
        bot.typing()
        print("owner: reachable (you have opened the bot)")
    except TgError as e:
        print(f"owner: NOT reachable yet: {e.description}" +
              ("\n       open the bot in Telegram and press Start, then try again" if e.unreachable else ""))
        return 1
    return 0


def cmd_pair(bot, write):
    if not bot.token:
        print(bot.why)
        return 1
    me = bot.me()
    print(f"message @{me.get('username')} from your phone (anything, /start is fine). waiting up to 5 min...")
    offset, deadline = 0, time.time() + 300
    while time.time() < deadline:
        for u in bot.poll(offset, 30):
            offset = u["update_id"] + 1
            m = u.get("message") or {}
            if m.get("from"):
                uid = m["from"]["id"]
                print(f"got a message from {who(m)}: {(m.get('text') or '')[:60]!r}")
                if write:
                    try:
                        cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
                    except ValueError:
                        cfg = {}
                    cfg.setdefault("telegram", {})["owner_id"] = uid
                    CONFIG.parent.mkdir(parents=True, exist_ok=True)
                    CONFIG.write_text(json.dumps(cfg, indent=1, ensure_ascii=False) + "\n")
                    print(f"saved telegram.owner_id = {uid} in {CONFIG}; restart the relay: systemctl --user restart mochi-brain")
                else:
                    print(f"to make them the owner: set telegram.owner_id = {uid} in {CONFIG} (or rerun with --write)")
                return 0
    print("nobody wrote. try again.")
    return 1


def main():
    ap = argparse.ArgumentParser(prog="mochi-telegram", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("pair")
    p.add_argument("--write", action="store_true", help="save the sender as telegram.owner_id in sources.json")
    s = sub.add_parser("send")
    s.add_argument("text", nargs="+")
    s.add_argument("--buzz", action="store_true", help="with a notification (default: silent)")
    f = sub.add_parser("file")
    f.add_argument("paths", nargs="+")
    f.add_argument("--caption", default="", help="text under the (first) file")
    f.add_argument("--photo", action="store_true", help="send pictures as photos (Telegram recompresses them) instead of files")
    f.add_argument("--buzz", action="store_true", help="with a notification (default: silent)")
    sub.add_parser("updates")
    a = ap.parse_args()
    bot = Bot()
    try:
        if a.cmd == "status":
            return cmd_status(bot)
        if a.cmd == "pair":
            return cmd_pair(bot, a.write)
        if not bot.enabled:
            sys.exit(f"mochi-telegram: {bot.why}")
        if a.cmd == "send":
            mid = bot.send(" ".join(a.text), buzz=a.buzz)
            print(f"sent (message {mid}, {'buzz' if a.buzz else 'silent'}) at {dt.datetime.now():%H:%M}")
            return 0
        if a.cmd == "file":
            for i, path in enumerate(a.paths):
                if not Path(path).is_file():
                    sys.exit(f"mochi-telegram: not a file: {path}")
                mid = bot.send_file(path, caption=a.caption if i == 0 else "", buzz=a.buzz and i == 0, photo=a.photo)
                print(f"sent {path} (message {mid}) at {dt.datetime.now():%H:%M}")
            return 0
        if a.cmd == "updates":
            for u in bot.poll(0, 0):
                m = u.get("message") or (u.get("callback_query") or {}).get("message") or {}
                src = u.get("message") or u.get("callback_query") or {}
                print(f"{u['update_id']}: from {who(src)}: {(src.get('text') or src.get('data') or '')[:80]!r}"
                      f"{'  (message ' + str(m.get('message_id')) + ')' if m else ''}")
            return 0
    except TgError as e:
        sys.exit(f"mochi-telegram: Telegram said {e}")
    except OSError as e:
        sys.exit(f"mochi-telegram: network: {e}")


if __name__ == "__main__":
    sys.exit(main())
