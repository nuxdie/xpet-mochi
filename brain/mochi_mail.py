#!/usr/bin/env python3
"""mochi-mail — Mochi's outbound mail, through msmtp (~/.msmtprc, Gmail SMTP).

    mochi-mail draft  --account NAME --to a@b.c [--cc ...] --subject "..." [--in-reply-to '<id>'] < body.txt
    mochi-mail send   --draft FILE [--dry-run]
    mochi-mail send   --account NAME --to a@b.c --subject "..." [--in-reply-to '<id>'] [--dry-run] < body.txt
    mochi-mail archive   QUERY [--dry-run] [--max N]
    mochi-mail unarchive QUERY [--dry-run] [--max N]
    mochi-mail log    [N]

A draft is a small file: header lines (From, To, Cc, Subject, In-Reply-To), a blank line, the body. `draft` writes one
into reports/drafts/ in Mochi's workspace and prints its path, so a run can show it to the human behind "Show me" and
send it later with `send --draft`. `send` builds a proper message (Date, Message-ID, UTF-8), hands it to
`msmtp -a ACCOUNT -t`, and appends a line to mail-sent.log. Gmail files its own copy in Sent Mail, which the next
mbsync pull brings back into the local Maildir. Accounts come from the "mail" section of ~/.config/mochi/sources.json.

`archive` is Gmail's Archive: QUERY is notmuch syntax (`id:...`, `thread:...`, `from:greenhouse subject:received`),
matched against what is in the inbox (unarchive: against what is not), and/or Gmail thread ids as `gmail:HEX` (the
ids the Gmail web UI and API use). Each match takes its whole thread with it, as in Gmail. Over IMAP with the same app passwords mbsync uses (~/.config/mochi/mail/NAME.pass), it only removes
the \\Inbox label (`STORE -X-GM-LABELS`): nothing is flagged deleted, moved to Trash or expunged, so `unarchive`
puts back exactly what was taken. The local notmuch "inbox" tag follows at once; every change goes into the log.
"""

import argparse
import datetime as dt
import imaplib
import json
import os
import re
import subprocess
import sys
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from pathlib import Path

HOME = Path.home()
WORK = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "mochi"
DRAFTS = WORK / "reports" / "drafts"
LOG = WORK / "mail-sent.log"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "mochi" / "sources.json"
MAX_RECIPIENTS = 10
MAX_ARCHIVE = 50  # more than this in one go needs --max: a query that broad is probably a mistake


def accounts():
    try:
        return json.loads(CONFIG.read_text()).get("mail", {}).get("accounts", {})
    except (OSError, ValueError):
        return {}


def sender_name():
    try:
        return json.loads(CONFIG.read_text()).get("mail", {}).get("name", "")
    except (OSError, ValueError):
        return ""


def account_for(from_addr):
    _, addr = parseaddr(from_addr)
    for name, email in accounts().items():
        if email.lower() == addr.lower():
            return name
    return None


def split_addrs(value):
    return [a.strip() for a in re.split(r"[,;]", value or "") if a.strip()]


def read_draft(path):
    text = Path(path).read_text()
    head, _, body = text.partition("\n\n")
    headers = {}
    for line in head.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return headers, body.strip() + "\n"


def build(account, to, cc, subject, body, in_reply_to=None):
    acc = accounts()
    if account not in acc:
        sys.exit(f"mochi-mail: unknown account {account!r} (known: {', '.join(acc) or 'none'}; see {CONFIG})")
    tos, ccs = split_addrs(to), split_addrs(cc)
    if not tos:
        sys.exit("mochi-mail: no recipient")
    if len(tos) + len(ccs) > MAX_RECIPIENTS:
        sys.exit(f"mochi-mail: more than {MAX_RECIPIENTS} recipients; that's a mailing, not a mail")
    for a in tos + ccs:
        if not re.match(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$", parseaddr(a)[1]):
            sys.exit(f"mochi-mail: not an address: {a!r}")
    if not subject.strip():
        sys.exit("mochi-mail: empty subject")
    if not body.strip():
        sys.exit("mochi-mail: empty body")
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name(), acc[account])) if sender_name() else acc[account]
    msg["To"] = ", ".join(tos)
    if ccs:
        msg["Cc"] = ", ".join(ccs)
    msg["Subject"] = subject.strip()
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=acc[account].split("@")[1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg["User-Agent"] = "Mochi (mochi-mail)"
    msg.set_content(body)
    return msg


def send(msg, account, dry_run):
    raw = msg.as_bytes()
    if dry_run:
        sys.stdout.write(raw.decode("utf-8", "replace"))
        print(f"\n--- dry run: not sent (would use msmtp -a {account})")
        return 0
    p = subprocess.run(["msmtp", "-a", account, "-t"], input=raw, capture_output=True, timeout=60)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M} {'sent' if p.returncode == 0 else 'FAILED'} account={account} "
                f"to={msg['To']}{(' cc=' + msg['Cc']) if msg.get('Cc') else ''} subject={msg['Subject']!r} id={msg['Message-ID']}\n")
    if p.returncode != 0:
        sys.exit(f"mochi-mail: msmtp failed: {p.stderr.decode(errors='replace').strip()[-400:]}")
    print(f"sent to {msg['To']} from {account} ({msg['Message-ID']})")
    return 0


def mail_cfg():
    try:
        return json.loads(CONFIG.read_text()).get("mail", {})
    except (OSError, ValueError):
        return {}


def log_line(text):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M} {text}\n")


def notmuch_ids(query):
    p = subprocess.run(["notmuch", "search", "--format=json", "--output=messages", query],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        sys.exit(f"mochi-mail: notmuch search failed: {p.stderr.strip()[-300:]}")
    return json.loads(p.stdout or "[]")


def imap_quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def all_mail(conn):
    """The mailbox flagged \\All ("[Gmail]/All Mail", or its localized name)."""
    _, rows = conn.list()
    for row in rows or []:
        line = row.decode(errors="replace") if isinstance(row, bytes) else str(row)
        if "\\All" in line:
            m = re.search(r'"([^"]*)"\s*$', line) or re.search(r"(\S+)\s*$", line)
            return m.group(1)
    return "[Gmail]/All Mail"


def decoded(header):
    try:
        return str(make_header(decode_header(header)))
    except Exception:
        return header


def fetch_meta(conn, uids):
    """{uid: (has_inbox, message_id, subject)} for UIDs in the selected mailbox."""
    out = {}
    if not uids:
        return out
    _, data = conn.uid("FETCH", ",".join(uids), "(X-GM-LABELS BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT)])")
    for item in data or []:
        if not isinstance(item, tuple):
            continue
        head = item[0].decode(errors="replace")
        uid = re.search(r"UID (\d+)", head)
        if not uid:
            continue
        labels = re.search(r"X-GM-LABELS \((.*?)\)", head)
        hdrs = item[1].decode(errors="replace")
        mid = re.search(r"^Message-ID:\s*<?([^>\s]+)>?", hdrs, re.I | re.M)
        subj = re.search(r"^Subject:\s*(.*(?:\n[ \t].*)*)", hdrs, re.I | re.M)
        out[uid.group(1)] = ("\\Inbox" in (labels.group(1) if labels else ""), mid.group(1) if mid else "",
                             re.sub(r"\s+", " ", decoded(subj.group(1))).strip() if subj else "")
    return out


def relabel(account, address, msgids, thread_ids, archive, dry_run):
    """Take the \\Inbox label off (or put it back on) the threads of these Message-IDs and Gmail thread ids (hex).
    Returns [(msgid, subject)] of the messages changed."""
    pw_file = Path(mail_cfg().get("password_dir") or CONFIG.parent / "mail") / f"{account}.pass"
    conn = imaplib.IMAP4_SSL("imap.gmail.com", timeout=60)
    try:
        conn.login(address, pw_file.read_text().strip())
        conn.select(imap_quote(all_mail(conn)), readonly=dry_run)
        threads = {str(int(t, 16)) for t in thread_ids}
        for mid in msgids:
            _, data = conn.uid("SEARCH", "X-GM-RAW", imap_quote(f"rfc822msgid:{mid}"))
            for uid in (data[0] or b"").split():
                _, t = conn.uid("FETCH", uid.decode(), "(X-GM-THRID)")
                m = re.search(rb"X-GM-THRID (\d+)", t[0] if t and isinstance(t[0], bytes) else b"")
                if m:
                    threads.add(m.group(1).decode())
        uids = []
        for tid in threads:
            _, data = conn.uid("SEARCH", "X-GM-THRID", tid)
            uids += [u.decode() for u in (data[0] or b"").split()]
        meta = fetch_meta(conn, uids)
        todo = [u for u, (inbox, _, _) in meta.items() if inbox == archive]
        if todo and not dry_run:
            typ, resp = conn.uid("STORE", ",".join(todo), "-X-GM-LABELS" if archive else "+X-GM-LABELS", "(\\Inbox)")
            if typ != "OK":
                sys.exit(f"mochi-mail: Gmail refused the change: {resp}")
        return [(meta[u][1], meta[u][2]) for u in todo]
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def cmd_archive(a, archive):
    cfg = mail_cfg()
    accts = cfg.get("accounts", {})
    if not accts:
        sys.exit(f"mochi-mail: no mail accounts in {CONFIG}")
    gmail = [t.split(":", 1)[1] for t in a.query if re.fullmatch(r"gmail:[0-9a-fA-F]{1,16}", t)]
    query = " ".join(t for t in a.query if not re.fullmatch(r"gmail:[0-9a-fA-F]{1,16}", t))
    side = "tag:inbox" if archive else "not tag:inbox"
    per = {name: notmuch_ids(f"tag:{name} and {side} and ({query})") if query else [] for name in accts}
    n = sum(len(v) for v in per.values()) + len(gmail)
    if not n:
        print(f"nothing {'in' if archive else 'archived outside'} the inbox matches: {query}")
        return 0
    if n > a.max:
        sys.exit(f"mochi-mail: {n} messages/threads match, more than --max {a.max}; narrow the query or raise --max")
    verb = "archived" if archive else "unarchived"
    for name, ids in per.items():
        if not ids and not gmail:  # a Gmail thread id names no account: each one is looked for in every account
            continue
        done = relabel(name, accts[name], ids, gmail, archive, a.dry_run)
        for mid, subj in done:
            print(f"{'would be ' if a.dry_run else ''}{verb}: [{name}] {subj or '(no subject)'}  <{mid}>")
        if a.dry_run or not done:
            continue
        found = [m for m, _ in done if m]
        if found:
            subprocess.run(["notmuch", "tag", "-inbox" if archive else "+inbox", "--",
                            " or ".join('id:"' + m.replace('"', '""') + '"' for m in found)], capture_output=True, timeout=60)
        log_line(f"{verb} account={name} n={len(done)} query={' '.join(a.query)!r} "
                 + "; ".join(f"{s[:60]!r} <{m}>" for m, s in done))
    if a.dry_run:
        print("--- dry run: nothing changed")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="mochi-mail", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("draft", "send"):
        s = sub.add_parser(name)
        s.add_argument("--draft", help="(send) a draft file written by `mochi-mail draft`")
        s.add_argument("--account", help="an account name from mail.accounts in sources.json (the From address)")
        s.add_argument("--to")
        s.add_argument("--cc", default="")
        s.add_argument("--subject", default="")
        s.add_argument("--in-reply-to", default="")
        s.add_argument("--body-file", help="body text (default: stdin)")
        s.add_argument("--dry-run", action="store_true")
    for name in ("archive", "unarchive"):
        s = sub.add_parser(name)
        s.add_argument("query", nargs="+", help="notmuch query ('id:abc@x', 'from:greenhouse') and/or gmail:HEX thread ids")
        s.add_argument("--dry-run", action="store_true")
        s.add_argument("--max", type=int, default=MAX_ARCHIVE)
    lg = sub.add_parser("log")
    lg.add_argument("n", nargs="?", type=int, default=20)
    a = ap.parse_args()

    if a.cmd in ("archive", "unarchive"):
        return cmd_archive(a, a.cmd == "archive")

    if a.cmd == "log":
        if LOG.exists():
            sys.stdout.write("".join(LOG.read_text().splitlines(True)[-a.n:]))
        else:
            print("nothing sent or archived yet")
        return 0

    if a.draft:
        headers, body = read_draft(a.draft)
        account = a.account or account_for(headers.get("from", ""))
        if not account:
            sys.exit("mochi-mail: the draft's From address matches no account; pass --account")
        msg = build(account, headers.get("to", ""), headers.get("cc", ""), headers.get("subject", ""), body, headers.get("in-reply-to") or None)
    else:
        if not a.account or not a.to:
            sys.exit("mochi-mail: --account and --to are required (or --draft FILE)")
        body = Path(a.body_file).read_text() if a.body_file else sys.stdin.read()
        msg = build(a.account, a.to, a.cc, a.subject, body, a.in_reply_to or None)
        account = a.account

    if a.cmd == "draft":
        DRAFTS.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", msg["Subject"]).strip("-")[:40] or "mail"
        path = DRAFTS / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{slug}.eml"
        lines = [f"From: {msg['From']}", f"To: {msg['To']}"]
        if msg.get("Cc"):
            lines.append(f"Cc: {msg['Cc']}")
        lines.append(f"Subject: {msg['Subject']}")
        if msg.get("In-Reply-To"):
            lines.append(f"In-Reply-To: {msg['In-Reply-To']}")
        path.write_text("\n".join(lines) + "\n\n" + msg.get_content())
        print(path)
        return 0
    return send(msg, account, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
