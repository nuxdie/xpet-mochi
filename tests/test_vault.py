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


class Identities(unittest.TestCase):
    def test_username_and_email_field(self):
        self.assertEqual(mv.identities({"username": "artem"}, [{"name": "Email", "value": "a@x.com", "type": 0}]),
                         ("a@x.com", "artem"))

    def test_email_and_username_field(self):
        self.assertEqual(mv.identities({"username": "a@x.com"}, [{"name": "Username", "value": "artem", "type": 0}]),
                         ("a@x.com", "artem"))
        self.assertEqual(mv.identities({"username": "a@x.com"}, [{"name": "Логин", "value": "artem", "type": 1}]),
                         ("a@x.com", "artem"))

    def test_only_custom_fields(self):
        self.assertEqual(mv.identities({}, [{"name": "PIN", "value": "1234", "type": 1},
                                            {"name": "e-mail", "value": "a@x.com", "type": 0}]), ("a@x.com", ""))

    def test_nothing_else(self):
        self.assertEqual(mv.identities({"username": "artem"}, None), ("", "artem"))
        self.assertEqual(mv.identities({"username": "artem"}, [{"name": "Email", "value": "not one", "type": 0},
                                                               {"name": "Remember", "value": "true", "type": 2}]),
                         ("", "artem"))


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
        mv.frames = lambda t: [(t["url"], t["webSocketDebuggerUrl"], None)]
        mv.probe = lambda f: True
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


class FillFrom(unittest.TestCase):
    """fill --from: the login saved for one site, on a sign-in page elsewhere; both named; the site added on Allow."""

    def setUp(self):
        import base64, json, subprocess
        self.asked, self.filled, self.edited = [], [], []
        self.v = mv.Vault(bw="/bin/false", relay_pid=lambda: RELAY, notify=self.notify)
        self.v.items = [item("j", "jobs", "https://apply.jobs.example"), item("a", "GitHub", "github.com")]
        self.tabs = [{"url": "https://login.jobs-id.example/signin?x=1", "webSocketDebuggerUrl": "ws://x/1"}]
        mv.pages = lambda: self.tabs
        mv.frames = lambda t: [(t["url"], t["webSocketDebuggerUrl"], None)]
        mv.probe = lambda f, signup=False: True
        mv.keyring_get = lambda: "master"
        mv.fill_page = lambda f, host, it, submit=True, signup=False: (self.filled.append((host, it["id"])) or
                                                                      {"ok": True, "filled": ["password"], "how": "button"})
        self.v.open_bw = lambda pw: ("SESSION", None)

        def run_bw(args, session=None, pass_fds=(), timeout=120, input=None):
            if args[:2] == ["get", "item"]:
                return subprocess.CompletedProcess(args, 0, json.dumps(
                    {"id": "j", "login": {"uris": [{"uri": "https://apply.jobs.example", "match": None}]}}), "")
            if args[:2] == ["edit", "item"]:
                self.edited.append(json.loads(base64.b64decode(input)))
            return subprocess.CompletedProcess(args, 0, "{}", "")
        self.v.run_bw = run_bw
        self.answer = None

    def notify(self, **m):
        if m["event"] == "vault_ask":
            self.asked.append(m)
            if self.answer:
                choice, pid = self.answer
                threading.Timer(0.05, lambda: self.v.decide(m["id"], choice, pid=pid)).start()
        return True

    def test_plain_fill_finds_nothing(self):
        ok, msg = self.v.fill("x", timeout=1)
        self.assertFalse(ok)
        self.assertEqual(self.asked, [])

    def test_allow_fills_and_adds_the_site(self):
        self.answer = (0, RELAY)
        ok, msg = self.v.fill("finish the application", from_site="apply.jobs.example", timeout=2)
        self.assertTrue(ok, msg)
        self.assertEqual(self.asked[0]["kind"], "elsewhere")
        self.assertEqual(self.asked[0]["source"], "apply.jobs.example")
        self.assertEqual(self.asked[0]["host"], "login.jobs-id.example")
        self.assertEqual(self.filled, [("login.jobs-id.example", "j")])
        self.assertEqual(self.edited[0]["login"]["uris"][-1], {"uri": "https://login.jobs-id.example", "match": None})
        self.assertIn("Added", msg)
        # from now on an ordinary fill matches there
        self.assertEqual(self.v.has("https://login.jobs-id.example/signin"), "1 login for login.jobs-id.example")

    def test_deny_and_unknown_source(self):
        self.answer = (None, RELAY)
        ok, _ = self.v.fill("x", from_site="apply.jobs.example", timeout=2)
        self.assertFalse(ok)
        self.assertEqual((self.filled, self.edited), ([], []))
        ok, msg = self.v.fill("x", from_site="nowhere.example", timeout=1)
        self.assertFalse(ok)
        self.assertIn("no login", msg)


class Export(unittest.TestCase):
    """export: a login copied into a file under the config dir, after the tap; nowhere else; nothing printed."""

    def setUp(self):
        self.asked = []
        self.v = mv.Vault(bw="/bin/false", relay_pid=lambda: RELAY, notify=self.notify)
        self.v.items = [item("d", "seedbox", "https://box.example"), item("a", "GitHub", "github.com")]
        self.answer = None

    def notify(self, **m):
        if m["event"] == "vault_ask":
            self.asked.append(m)
            if self.answer:
                choice, pid = self.answer
                threading.Timer(0.05, lambda: self.v.decide(m["id"], choice, pid=pid)).start()
        return True

    def test_allow_writes_the_file_600(self):
        self.answer = (0, RELAY)
        to = Path(TMP) / "mochi" / "deluge.env"
        ok, msg = self.v.export("https://box.example/deluge/", str(to), "Deluge's HTTP login", timeout=2)
        self.assertTrue(ok, msg)
        self.assertEqual(self.asked[0]["kind"], "copy")
        self.assertEqual(self.asked[0]["host"], "box.example")
        self.assertEqual(oct(to.stat().st_mode & 0o777), "0o600")
        text = to.read_text()
        self.assertIn("USERNAME=me@example.com\n", text)
        self.assertIn("PASSWORD=pw-d\n", text)
        self.assertNotIn("pw-", msg + repr(self.asked))  # the secret goes into the file only

    def test_only_under_the_config_dir(self):
        ok, msg = self.v.export("https://box.example/", str(Path(TMP) / "elsewhere.env"), "x", timeout=1)
        self.assertFalse(ok)
        self.assertIn("under", msg)
        self.assertEqual(self.asked, [])

    def test_deny_and_no_login(self):
        self.answer = (None, RELAY)
        ok, _ = self.v.export("https://box.example/", str(Path(TMP) / "mochi" / "x.env"), "x", timeout=2)
        self.assertFalse(ok)
        self.assertFalse((Path(TMP) / "mochi" / "x.env").exists())
        ok, msg = self.v.export("https://nobody.example/", str(Path(TMP) / "mochi" / "x.env"), "x", timeout=1)
        self.assertFalse(ok)
        self.assertIn("no login", msg)
        ok, msg = self.v.export("https://box.example/", str(Path(TMP) / "mochi" / "x.env"), "  ", timeout=1)
        self.assertFalse(ok)


class Signup(unittest.TestCase):
    """signup: asked on the phone, saved in Bitwarden before anything is typed, then filled; never without the save."""

    def setUp(self):
        self.asked, self.filled, self.created, self.bw_calls = [], [], [], []
        self.v = mv.Vault(bw="/bin/false", relay_pid=lambda: RELAY, notify=self.notify)
        self.v.items = [item("a", "GitHub", "github.com")]
        self.tabs = [{"url": "https://jobs.example/apply/new", "webSocketDebuggerUrl": "ws://x/1"}]
        mv.pages = lambda: self.tabs
        mv.frames = lambda t: [(t["url"], t["webSocketDebuggerUrl"], None)]
        mv.probe = lambda f, signup=False: True
        mv.keyring_get = lambda: "master"
        mv.fill_page = lambda f, host, it, submit=True, signup=False: (
            self.filled.append((host, it["username"], it["password"], submit, signup)) or
            {"ok": True, "filled": ["email", "password (in 2 boxes)"], "how": "not submitted"})
        self.v.open_bw = lambda pw: ("SESSION", None)
        self.v.run_bw = self.run_bw
        self.save_fails = False
        self.answer = None

    def run_bw(self, args, session=None, pass_fds=(), timeout=120, input=None):
        import base64, json, subprocess
        self.bw_calls.append(args)
        if args[:2] == ["create", "item"]:
            if self.save_fails:
                return subprocess.CompletedProcess(args, 1, "", "Not found.")
            self.created.append(json.loads(base64.b64decode(input)))
            return subprocess.CompletedProcess(args, 0, json.dumps({"id": "new-1"}), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def notify(self, **m):
        if m["event"] == "vault_ask":
            self.asked.append(m)
            if self.answer:
                choice, pid = self.answer
                threading.Timer(0.05, lambda: self.v.decide(m["id"], choice, pid=pid)).start()
        return True

    def test_allow_saves_then_fills(self):
        self.answer = (0, RELAY)
        ok, msg = self.v.signup("apply for the job they picked", "me@example.com", timeout=2)
        self.assertTrue(ok, msg)
        self.assertEqual(self.asked[0]["kind"], "create")
        self.assertEqual(self.asked[0]["host"], "jobs.example")
        made = self.created[0]
        pw = made["login"]["password"]
        self.assertEqual(made["login"]["username"], "me@example.com")
        self.assertEqual(made["login"]["uris"], [{"uri": "https://jobs.example", "match": None}])
        self.assertGreaterEqual(len(pw), 20)
        self.assertEqual(self.filled, [("jobs.example", "me@example.com", pw, False, True)])
        self.assertEqual(self.bw_calls[-1], ["lock"])  # Bitwarden locked again after the write
        self.assertNotIn(pw, msg + repr(self.asked))
        # it's a login like any other now: a fill on that site finds it
        self.assertEqual(self.v.has("https://jobs.example/login"), "1 login for jobs.example")

    def test_save_fails_nothing_filled(self):
        self.answer = (0, RELAY)
        self.save_fails = True
        ok, msg = self.v.signup("x", "me@example.com", timeout=2)
        self.assertFalse(ok)
        self.assertIn("nothing was filled", msg)
        self.assertEqual(self.filled, [])
        self.assertEqual(self.bw_calls[-1], ["lock"])

    def test_not_remembered(self):
        self.answer = (0, RELAY)
        mv.keyring_get = lambda: None
        ok, msg = self.v.signup("x", "me@example.com", timeout=2)
        self.assertFalse(ok)
        self.assertIn("remember", msg)
        self.assertEqual(self.filled, [])

    def test_deny(self):
        self.answer = (None, RELAY)
        ok, _ = self.v.signup("x", "me@example.com", timeout=2)
        self.assertFalse(ok)
        self.assertEqual((self.created, self.filled), ([], []))

    def test_existing_login_and_unsafe_page(self):
        self.tabs = [{"url": "https://github.com/signup", "webSocketDebuggerUrl": "ws://x/1"}]
        ok, msg = self.v.signup("x", "me@example.com", timeout=1)
        self.assertFalse(ok)
        self.assertIn("already", msg)
        self.tabs = [{"url": "http://jobs.example/apply", "webSocketDebuggerUrl": "ws://x/1"}]
        ok, msg = self.v.signup("x", "me@example.com", timeout=1)
        self.assertFalse(ok)
        self.assertIn("https", msg)
        self.assertEqual(self.asked, [])

    def test_password_shape(self):
        for _ in range(50):
            pw = mv.make_password()
            self.assertEqual(len(pw), 20)
            self.assertTrue(any(c.islower() for c in pw) and any(c.isupper() for c in pw) and
                            any(c.isdigit() for c in pw) and any(c in mv.SYMBOLS for c in pw))
        self.assertTrue(mv.make_password(16, symbols=False).isalnum())


if __name__ == "__main__":
    unittest.main()
