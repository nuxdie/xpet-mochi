#!/usr/bin/env python3
"""Tests for mochi-google's offline parts: accounts, tokens and scopes. No network.
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

src = Path(__file__).resolve().parent.parent / "brain" / "mochi_google.py"
spec = importlib.util.spec_from_loader("mochi_google", importlib.machinery.SourceFileLoader("mochi_google", str(src)))
mg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mg)


class Accounts(unittest.TestCase):
    def setUp(self):
        mg.CONFIG.parent.mkdir(parents=True, exist_ok=True)
        for p in mg.CONF_DIR.glob("google.*.token.json"):
            p.unlink()

    def test_from_mail_accounts(self):
        mg.CONFIG.write_text(json.dumps({"mail": {"accounts": {"artem": "a@x", "nuxdie": "n@y"}}}))
        accounts, client = mg.conf()
        self.assertEqual(accounts, {"artem": "a@x", "nuxdie": "n@y"})
        self.assertEqual(client, mg.CONF_DIR / "google-oauth-client.json")
        self.assertEqual(mg.signed_in(), [])

    def test_google_overrides(self):
        mg.CONFIG.write_text(json.dumps({"mail": {"accounts": {"artem": "a@x"}},
                                         "google": {"accounts": {"work": "w@z"}, "client_file": "/tmp/c.json"}}))
        self.assertEqual(mg.conf(), ({"work": "w@z"}, Path("/tmp/c.json")))

    def test_scopes(self):
        mg.CONFIG.write_text(json.dumps({"mail": {"accounts": {"artem": "a@x", "nuxdie": "n@y"}}}))
        mg.save_token("artem", {"refresh_token": "r", "scopes": [mg.DRIVE]})
        mg.save_token("nuxdie", {"refresh_token": "r", "scopes": [mg.DRIVE, mg.CALENDAR]})
        self.assertEqual(oct(mg.token_path("artem").stat().st_mode & 0o777), "0o600")
        self.assertEqual(mg.signed_in(), ["artem", "nuxdie"])
        self.assertEqual(mg.signed_in(mg.CALENDAR), ["nuxdie"])
        self.assertEqual(mg.accounts_for(None, mg.DRIVE), ["artem", "nuxdie"])
        self.assertEqual(mg.accounts_for("artem", mg.CALENDAR), ["artem"])  # named: the call itself says what's missing
        with self.assertRaises(SystemExit):
            mg.accounts_for(None, mg.CONTACTS)
        with self.assertRaises(SystemExit):
            mg.accounts_for("bob", mg.DRIVE)
        with self.assertRaises(SystemExit) as e:
            mg.access_token("artem", mg.CALENDAR)
        self.assertIn("doesn't cover calendar", str(e.exception))


if __name__ == "__main__":
    unittest.main()
