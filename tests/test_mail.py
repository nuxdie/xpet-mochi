#!/usr/bin/env python3
"""Tests for mochi-mail archive: the IMAP side against a fake Gmail. No network, no passwords.
Run: python3 -m unittest discover -s tests   (or `make test`)."""

import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TMP = tempfile.mkdtemp(prefix="mochi-test-")
os.environ["XDG_DATA_HOME"] = TMP
os.environ["XDG_CONFIG_HOME"] = TMP

src = Path(__file__).resolve().parent.parent / "brain" / "mochi_mail.py"
spec = importlib.util.spec_from_loader("mochi_mail", importlib.machinery.SourceFileLoader("mochi_mail", str(src)))
mm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mm)

(Path(TMP) / "mochi" / "mail").mkdir(parents=True, exist_ok=True)
(Path(TMP) / "mochi" / "mail" / "artem.pass").write_text("app-password\n")
(Path(TMP) / "mochi" / "sources.json").write_text(json.dumps({"mail": {
    "accounts": {"artem": "a@example.com"}, "password_dir": str(Path(TMP) / "mochi" / "mail")}}))


class FakeGmail:
    """Two threads in All Mail: thread 0x10 (uids 1, 2; both in the inbox) and 0x20 (uid 3, archived)."""

    def __init__(self, *a, **kw):
        self.msgs = {"1": {"thr": "16", "mid": "one@x", "subj": "Hello", "labels": {"\\Inbox", "\\Important"}},
                     "2": {"thr": "16", "mid": "two@x", "subj": "=?UTF-8?B?UmU6IEjDqWxsbw==?=", "labels": {"\\Inbox"}},
                     "3": {"thr": "32", "mid": "three@x", "subj": "Old", "labels": set()}}
        self.calls = []
        FakeGmail.last = self

    def login(self, user, pw):
        self.calls.append(("login", user, pw))

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"']

    def select(self, box, readonly=False):
        self.calls.append(("select", box, readonly))
        return "OK", [b"3"]

    def uid(self, cmd, *args):
        self.calls.append((cmd,) + args)
        if cmd == "SEARCH" and args[0] == "X-GM-RAW":
            mid = args[1].strip('"').split(":", 1)[1]
            return "OK", [" ".join(u for u, m in self.msgs.items() if m["mid"] == mid).encode()]
        if cmd == "SEARCH" and args[0] == "X-GM-THRID":
            return "OK", [" ".join(u for u, m in self.msgs.items() if m["thr"] == args[1]).encode()]
        if cmd == "FETCH" and args[1] == "(X-GM-THRID)":
            return "OK", [f"1 (X-GM-THRID {self.msgs[args[0]]['thr']} UID {args[0]})".encode()]
        if cmd == "FETCH":
            out = []
            for u in args[0].split(","):
                m = self.msgs[u]
                labels = " ".join(f'"\\\\{l[1:]}"' for l in sorted(m["labels"]))
                out += [(f"{u} (X-GM-LABELS ({labels}) UID {u} BODY[HEADER.FIELDS (MESSAGE-ID SUBJECT)] {{9}}".encode(),
                         f"Message-ID: <{m['mid']}>\r\nSubject: {m['subj']}\r\n\r\n".encode()), b")"]
            return "OK", out
        if cmd == "STORE":
            for u in args[0].split(","):
                (self.msgs[u]["labels"].discard if args[1] == "-X-GM-LABELS" else self.msgs[u]["labels"].add)("\\Inbox")
            return "OK", [b""]
        raise AssertionError(cmd)

    def logout(self):
        pass


class Archive(unittest.TestCase):
    def run_cmd(self, cmd, query, dry_run=False, notmuch=("one@x",)):
        a = SimpleNamespace(cmd=cmd, query=query, dry_run=dry_run, max=mm.MAX_ARCHIVE)
        with mock.patch.object(mm.imaplib, "IMAP4_SSL", FakeGmail), \
                mock.patch.object(mm, "notmuch_ids", return_value=list(notmuch)), \
                mock.patch.object(mm.subprocess, "run") as run:
            mm.cmd_archive(a, cmd == "archive")
        return FakeGmail.last, run

    def test_archive_takes_whole_thread_and_only_the_inbox_label(self):
        g, run = self.run_cmd("archive", ["id:one@x"])
        self.assertEqual(g.msgs["1"]["labels"], {"\\Important"})
        self.assertEqual(g.msgs["2"]["labels"], set())
        stores = [c for c in g.calls if c[0] == "STORE"]
        self.assertEqual(stores, [("STORE", "1,2", "-X-GM-LABELS", "(\\Inbox)")])
        self.assertIn(("login", "a@example.com", "app-password"), g.calls)
        self.assertIn(("select", '"[Gmail]/All Mail"', False), g.calls)
        tag = run.call_args[0][0]
        self.assertEqual(tag[:3], ["notmuch", "tag", "-inbox"])
        self.assertIn('id:"two@x"', tag[-1])
        self.assertIn("Re: Héllo", mm.LOG.read_text())

    def test_dry_run_changes_nothing(self):
        g, run = self.run_cmd("archive", ["id:one@x"], dry_run=True)
        self.assertFalse([c for c in g.calls if c[0] == "STORE"])
        self.assertIn(("select", '"[Gmail]/All Mail"', True), g.calls)
        run.assert_not_called()

    def test_gmail_thread_id(self):
        g, _ = self.run_cmd("unarchive", ["gmail:20"], notmuch=())
        self.assertEqual(g.msgs["3"]["labels"], {"\\Inbox"})
        self.assertEqual([c for c in g.calls if c[0] == "STORE"], [("STORE", "3", "+X-GM-LABELS", "(\\Inbox)")])

    def test_too_many(self):
        with self.assertRaises(SystemExit):
            self.run_cmd("archive", ["from:x"], notmuch=[f"{i}@x" for i in range(mm.MAX_ARCHIVE + 1)])


if __name__ == "__main__":
    unittest.main()
