#!/usr/bin/env python3
"""mochi-mail — Mochi's outbound mail, through msmtp (~/.msmtprc, Gmail SMTP).

    mochi-mail draft  --account NAME --to a@b.c [--cc ...] --subject "..." [--in-reply-to '<id>'] < body.txt
    mochi-mail send   --draft FILE [--dry-run]
    mochi-mail send   --account NAME --to a@b.c --subject "..." [--in-reply-to '<id>'] [--dry-run] < body.txt
    mochi-mail log    [N]

A draft is a small file: header lines (From, To, Cc, Subject, In-Reply-To), a blank line, the body. `draft` writes one
into reports/drafts/ in Mochi's workspace and prints its path, so a run can show it to the human behind "Show me" and
send it later with `send --draft`. `send` builds a proper message (Date, Message-ID, UTF-8), hands it to
`msmtp -a ACCOUNT -t`, and appends a line to mail-sent.log. Gmail files its own copy in Sent Mail, which the next
mbsync pull brings back into the local Maildir. Accounts come from the "mail" section of ~/.config/mochi/sources.json.
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from pathlib import Path

HOME = Path.home()
WORK = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "mochi"
DRAFTS = WORK / "reports" / "drafts"
LOG = WORK / "mail-sent.log"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "mochi" / "sources.json"
MAX_RECIPIENTS = 10


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
    lg = sub.add_parser("log")
    lg.add_argument("n", nargs="?", type=int, default=20)
    a = ap.parse_args()

    if a.cmd == "log":
        if LOG.exists():
            sys.stdout.write("".join(LOG.read_text().splitlines(True)[-a.n:]))
        else:
            print("nothing sent yet")
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
