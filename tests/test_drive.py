#!/usr/bin/env python3
"""Tests for mochi-drive's offline parts: ids from links, Drive queries, file lines, accounts. No network, no token.
Run: python3 -m unittest discover -s tests   (or `make test`)."""

import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="mochi-test-")
os.environ["XDG_DATA_HOME"] = TMP
os.environ["XDG_CONFIG_HOME"] = TMP

src = Path(__file__).resolve().parent.parent / "brain" / "mochi_drive.py"
spec = importlib.util.spec_from_loader("mochi_drive", importlib.machinery.SourceFileLoader("mochi_drive", str(src)))
md = importlib.util.module_from_spec(spec)
spec.loader.exec_module(md)

ID = "1-2MgrKZLQuY11QN0HEHWfU3lH0ReUa9d5hgwqeGtNvc"


class Ids(unittest.TestCase):
    def test_links(self):
        for s in (ID, f"https://docs.google.com/spreadsheets/d/{ID}/edit#gid=0",
                  f"https://docs.google.com/document/d/{ID}/edit?usp=drivesdk",
                  f"https://drive.google.com/drive/folders/{ID}", f"https://drive.google.com/open?id={ID}",
                  f"https://drive.google.com/file/d/{ID}/view", f"  {ID}\n"):
            self.assertEqual(md.file_id(s), ID, s)
        self.assertEqual(md.file_id("root"), "root")


class Queries(unittest.TestCase):
    def test_quote(self):
        self.assertEqual(md.quote("it's a\\b"), "'it\\'s a\\\\b'")

    def test_query(self):
        self.assertEqual(md.query(), "trashed = false")
        self.assertEqual(md.query("vat 2026"), "trashed = false and fullText contains 'vat 2026'")
        self.assertIn("name contains 'vat'", md.query("vat", name_only=True))
        q = md.query(type_="sheet", folder=f"https://drive.google.com/drive/folders/{ID}", since="2026-10-01T00:00:00Z")
        self.assertIn("mimeType = 'application/vnd.google-apps.spreadsheet'", q)
        self.assertIn(f"'{ID}' in parents", q)
        self.assertIn("modifiedTime > '2026-10-01T00:00:00Z'", q)
        with self.assertRaises(SystemExit):
            md.query(type_="spreadsheet")


class Lines(unittest.TestCase):
    def test_line(self):
        f = {"id": "abc", "name": "Accounting", "mimeType": md.TYPES["sheet"], "modifiedTime": "2026-10-13T19:28:00.000Z",
             "lastModifyingUser": {"displayName": "Artem", "me": True}}
        self.assertEqual(md.line(f, "artem"), "- Accounting · sheet · modified 2026-10-13 19:28 by them · artem · id=abc")
        g = {"id": "x", "name": "a.pdf", "mimeType": "application/pdf", "modifiedTime": "2026-10-01T08:00:00Z",
             "size": "51300", "lastModifyingUser": {"displayName": "Joshua"}}
        self.assertEqual(md.line(g), "- a.pdf · pdf · modified 2026-10-01 08:00 by Joshua · 50.1 KB · id=x")

    def test_human(self):
        self.assertEqual(md.human(512), "512 B")
        self.assertEqual(md.human(1536), "1.5 KB")
        self.assertEqual(md.human(15 * 1024 ** 3), "15.0 GB")

    def test_tsv(self):
        self.assertEqual(md.tsv([["a", "b\tc"], ["1"]]), "a\tb c\n1")
        self.assertEqual(md.tsv([[1], [2], [3]], 2), "1\n2\n…1 more rows")

    def test_utf16(self):
        self.assertEqual(md.utf16_len("привет 🐷"), 9)  # the pig is a surrogate pair

    def test_tab_range(self):
        self.assertEqual(md.tab_range("Bob's tab"), "'Bob''s tab'")


class Accounts(unittest.TestCase):
    def setUp(self):
        md.CONFIG.parent.mkdir(parents=True, exist_ok=True)

    def test_from_mail_accounts(self):
        md.CONFIG.write_text(json.dumps({"mail": {"accounts": {"artem": "a@x", "nuxdie": "n@y"}}}))
        accounts, client = md.conf()
        self.assertEqual(accounts, {"artem": "a@x", "nuxdie": "n@y"})
        self.assertEqual(client, md.CONF_DIR / "google-oauth-client.json")
        self.assertEqual(md.signed_in(), [])
        md.token_path("nuxdie").write_text("{}")
        self.assertEqual(md.signed_in(), ["nuxdie"])
        md.token_path("nuxdie").unlink()

    def test_drive_overrides(self):
        md.CONFIG.write_text(json.dumps({"mail": {"accounts": {"artem": "a@x"}},
                                         "drive": {"accounts": {"work": "w@z"}, "client_file": "/tmp/c.json"}}))
        self.assertEqual(md.conf(), ({"work": "w@z"}, Path("/tmp/c.json")))

    def test_rows_arg(self):
        class A:
            csv = None
            values = '[["a", 1], ["=SUM(B1:B2)"]]'
        self.assertEqual(md.rows_arg(A), [["a", 1], ["=SUM(B1:B2)"]])
        A.values = '{"a": 1}'
        with self.assertRaises(SystemExit):
            md.rows_arg(A)


if __name__ == "__main__":
    unittest.main()
