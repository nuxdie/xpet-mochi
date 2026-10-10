#!/usr/bin/env python3
"""Tests for mochi-tasks' offline parts: due dates, task lines and the digest. No network, no token.
Run: python3 -m unittest discover -s tests   (or `make test`)."""

import datetime as dt
import importlib.machinery
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="mochi-test-")
os.environ["XDG_DATA_HOME"] = TMP
os.environ["XDG_CONFIG_HOME"] = TMP

src = Path(__file__).resolve().parent.parent / "brain" / "mochi_tasks.py"
spec = importlib.util.spec_from_loader("mochi_tasks", importlib.machinery.SourceFileLoader("mochi_tasks", str(src)))
mt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mt)

TODAY = dt.date(2026, 10, 10)


def task(id, title, due=None, status="needsAction", updated="2026-10-01T00:00:00Z", **kw):
    t = {"id": id, "title": title, "status": status, "updated": updated, **kw}
    if due:
        t["due"] = due + "T00:00:00.000Z"
    return t


class Dates(unittest.TestCase):
    def test_due_text(self):
        self.assertEqual(mt.due_text(dt.date(2026, 10, 10), TODAY), "Sat 2026-10-10 (today)")
        self.assertIn("tomorrow", mt.due_text(dt.date(2026, 10, 11), TODAY))
        self.assertIn("3 days overdue", mt.due_text(dt.date(2026, 10, 7), TODAY))
        self.assertIn("in 5 days", mt.due_text(dt.date(2026, 10, 15), TODAY))
        self.assertEqual(mt.due_text(None, TODAY), "")

    def test_due_field(self):
        self.assertEqual(mt.due_field("2026-10-12"), "2026-10-12T00:00:00.000Z")
        with self.assertRaises(SystemExit):
            mt.due_field("next friday")


class Digest(unittest.TestCase):
    def test_overdue_and_soon_first(self):
        snap = [{"id": "a", "title": "My Tasks", "tasks": [
                    task("1", "renew passport", "2026-11-30", updated="2026-10-09T00:00:00Z"),
                    task("2", "pay VAT", "2026-10-08"),
                    task("3", "call dentist", notes="ask about\nthe crown"),
                    task("4", "done thing", status="completed")]},
                {"id": "b", "title": "Home", "tasks": [task("5", "fix tap", "2026-10-12")]}]
        out = mt.digest(snap, TODAY)
        self.assertTrue(out.startswith("4 open across 2 lists (My Tasks 3, Home 1)"))
        soon, other = out.split("**Other open tasks**")
        self.assertLess(soon.index("pay VAT"), soon.index("fix tap"))
        self.assertIn("2 days overdue", soon)
        self.assertIn("in Home", soon)
        self.assertLess(other.index("renew passport"), other.index("call dentist"))  # most recently touched first
        self.assertIn("ask about the crown", other)
        self.assertNotIn("done thing", out)

    def test_empty(self):
        self.assertEqual(mt.digest([{"id": "a", "title": "My Tasks", "tasks": []}], TODAY), "0 open across 1 lists (My Tasks 0)")


class Config(unittest.TestCase):
    def test_defaults_without_sources(self):
        client, token = mt.conf()
        self.assertEqual(client.name, "google-oauth-client.json")
        self.assertEqual(token.name, "google-tasks.token.json")

    def test_not_signed_in(self):
        with self.assertRaises(SystemExit) as e:
            mt.access_token()
        self.assertIn("mochi-tasks auth", str(e.exception))


if __name__ == "__main__":
    unittest.main()
