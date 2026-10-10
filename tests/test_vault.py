#!/usr/bin/env python3
"""Tests for mochi-vault's offline parts: URI matching, one-time codes, and the ask/decide/lease flow with Chrome and
the relay faked. No Bitwarden, no browser. Run: python3 -m unittest discover -s tests   (or `make test`)."""

import importlib.machinery
import importlib.util
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="mochi-test-")
os.environ["XDG_DATA_HOME"] = TMP
os.environ["XDG_CONFIG_HOME"] = TMP
os.environ["XDG_RUNTIME_DIR"] = TMP

src = Path(__file__).resolve().parent.parent / "brain" / "mochi_vault.py"
spec = importlib.util.spec_from_loader("mochi_vault", importlib.machinery.SourceFileLoader("mochi_vault", str(src)))
mv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mv)

RELAY = 4242


def item(id, name, uri, match=None, user="me@example.com", totp=""):
    return {"id": id, "name": name, "username": user, "password": "pw-" + id, "totp": totp,
            "uris": [{"uri": uri, "match": match}]}


class Matching(unittest.TestCase):
    def test_base_domain(self):
        self.assertEqual(mv.base_domain("gist.github.com"), "github.com")
        self.assertEqual(mv.base_domain("login.bbc.co.uk"), "bbc.co.uk")
        self.assertEqual(mv.base_domain("192.168.1.10"), "192.168.1.10")
        self.assertEqual(mv.base_domain("nas.local"), "nas.local")

    def test_default_is_same_site(self):
        self.assertTrue(mv.uri_matches("https://github.com", None, "https://github.com/login"))
        self.assertTrue(mv.uri_matches("github.com", None, "https://gist.github.com/x"))
        self.assertFalse(mv.uri_matches("github.com", None, "https://github.com.evil.io/login"))
        self.assertFalse(mv.uri_matches("github.com", None, "https://notgithub.com/"))

    def test_other_match_kinds(self):
        self.assertTrue(mv.uri_matches("https://a.example.com", 1, "https://a.example.com/x"))
        self.assertFalse(mv.uri_matches("https://a.example.com", 1, "https://b.example.com/x"))
        self.assertTrue(mv.uri_matches("https://x.com/app", 2, "https://x.com/app/login"))
        self.assertFalse(mv.uri_matches("https://x.com/app", 3, "https://x.com/app/login"))
        self.assertTrue(mv.uri_matches(r"^https://(www\.)?x\.com/", 4, "https://www.x.com/"))
        self.assertFalse(mv.uri_matches("https://x.com", 5, "https://x.com/"))

    def test_safe_page(self):
        self.assertTrue(mv.safe_page("https://github.com/login"))
        self.assertTrue(mv.safe_page("http://192.168.1.1/"))
        self.assertTrue(mv.safe_page("http://nas.local:5000/"))
        self.assertFalse(mv.safe_page("http://github.com/login"))
        self.assertFalse(mv.safe_page("file:///etc/passwd"))


class Codes(unittest.TestCase):
    def test_rfc6238(self):
        key = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # "12345678901234567890"
        self.assertEqual(mv.totp(f"otpauth://totp/x?secret={key}&digits=8", at=59), "94287082")
        self.assertEqual(mv.totp(key, at=1111111109), "081804")
        self.assertEqual(mv.totp(key.lower(), at=1111111109), "081804")

    def test_unusable(self):
        self.assertIsNone(mv.totp(""))
        self.assertIsNone(mv.totp("steam://ABC"))
        self.assertIsNone(mv.totp("not base32 !!"))


class Flow(unittest.TestCase):
    """fill → ask (relay faked) → decide → fill_page (faked); leases; who may decide."""

    def setUp(self):
        self.asked, self.filled = [], []
        self.v = mv.Vault(bw="/bin/false", relay_pid=lambda: RELAY, notify=self.notify)
        self.v.items = [item("a", "GitHub", "github.com"), item("b", "GitHub work", "github.com", user="work@x.com"),
                        item("c", "Bank", "https://bank.example")]
        self.tabs = [{"url": "https://github.com/login", "webSocketDebuggerUrl": "ws://x/1"},
                     {"url": "https://news.example/", "webSocketDebuggerUrl": "ws://x/2"}]
        mv.pages = lambda: self.tabs
        mv.fill_page = lambda t, host, it, submit=True: (self.filled.append((host, it["id"])) or
                                                          {"ok": True, "filled": ["username", "password"], "how": "button"})
        self.answer = None  # (choice, pid) the fake phone answers with

    def notify(self, **m):
        if m["event"] == "vault_ask":
            self.asked.append(m)
            if self.answer:
                choice, pid = self.answer
                threading.Timer(0.05, lambda: self.v.decide(m["id"], choice, pid=pid)).start()
        return True

    def test_locked(self):
        self.v.items = None
        ok, msg = self.v.fill("why")
        self.assertFalse(ok)
        self.assertIn("locked", msg)

    def test_allow_fills_the_chosen_account_and_leases_it(self):
        self.answer = (1, RELAY)
        ok, msg = self.v.fill("check the PR they asked about", timeout=2)
        self.assertTrue(ok, msg)
        self.assertEqual(self.filled, [("github.com", "b")])
        self.assertEqual(self.asked[0]["host"], "github.com")
        self.assertEqual(len(self.asked[0]["accounts"]), 2)
        self.assertNotIn("pw-", repr(self.asked) + msg)  # no secret leaves in the question or the answer
        # the second step on the same site, same account: no new question
        ok, _ = self.v.fill("same", account="work", timeout=2)
        self.assertTrue(ok)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(self.filled[-1], ("github.com", "b"))

    def test_deny(self):
        self.answer = (None, RELAY)
        ok, msg = self.v.fill("x", timeout=2)
        self.assertFalse(ok)
        self.assertEqual(self.filled, [])

    def test_only_the_relay_may_decide(self):
        self.answer = (0, RELAY + 1)  # someone else's process taps Allow
        ok, msg = self.v.fill("x", timeout=0.5)
        self.assertFalse(ok)
        self.assertIn("in time", msg)
        self.assertEqual(self.filled, [])

    def test_no_answer(self):
        ok, msg = self.v.fill("x", timeout=0.2)
        self.assertFalse(ok)
        self.assertIn("in time", msg)

    def test_page_choice_and_unsafe_pages(self):
        ok, msg = self.v.fill("x", page="news.example")
        self.assertFalse(ok)
        self.assertIn("no login", msg)
        self.tabs = [{"url": "http://bank.example/login", "webSocketDebuggerUrl": "ws://x/3"}]
        ok, msg = self.v.fill("x", page="bank")
        self.assertFalse(ok)
        self.assertIn("https", msg)
        self.assertEqual(self.asked, [])

    def test_why_is_required(self):
        ok, msg = self.v.fill("  ")
        self.assertFalse(ok)

    def test_lock_releases_a_waiting_fill(self):
        t = threading.Timer(0.1, self.v.forget)
        t.start()
        t0 = time.time()
        ok, _ = self.v.fill("x", timeout=5)
        self.assertFalse(ok)
        self.assertLess(time.time() - t0, 2)


if __name__ == "__main__":
    unittest.main()
