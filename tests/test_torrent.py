#!/usr/bin/env python3
"""Tests for mochi-torrent's offline parts: topic ids, sizes, the bencode reader, the auth file, and the Deluge
client against a fake web JSON-RPC (HTTP auth, login, connect, add). No Chrome, no tracker.
Run: python3 -m unittest discover -s tests   (or `make test`)."""

import base64
import http.server
import importlib.machinery
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="mochi-test-")
os.environ["XDG_DATA_HOME"] = TMP
os.environ["XDG_CONFIG_HOME"] = TMP

src = Path(__file__).resolve().parent.parent / "brain" / "mochi_torrent.py"
spec = importlib.util.spec_from_loader("mochi_torrent", importlib.machinery.SourceFileLoader("mochi_torrent", str(src)))
mt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mt)


def bencode(v):
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, bytes):
        return b"%d:" % len(v) + v
    if isinstance(v, str):
        return bencode(v.encode())
    if isinstance(v, list):
        return b"l" + b"".join(bencode(x) for x in v) + b"e"
    return b"d" + b"".join(bencode(k) + bencode(v[k]) for k in sorted(v)) + b"e"


class Small(unittest.TestCase):
    def test_topic_id(self):
        self.assertEqual(mt.topic_id("6925198"), "6925198")
        self.assertEqual(mt.topic_id("https://rutracker.org/forum/viewtopic.php?t=6925198"), "6925198")
        self.assertEqual(mt.topic_id("https://rutracker.org/forum/dl.php?t=42"), "42")
        self.assertIsNone(mt.topic_id("magnet:?xt=urn:btih:abc"))
        self.assertIsNone(mt.topic_id("https://example.com/?t=5"))
        self.assertIsNone(mt.topic_id(""))

    def test_human(self):
        self.assertEqual(mt.human(0), "0 B")
        self.assertEqual(mt.human(9151033393), "8.52 GB")
        self.assertEqual(mt.human(4.7 * 1024 ** 3), "4.70 GB")
        self.assertEqual(mt.human(150 * 1024 ** 2), "150 MB")
        self.assertEqual(mt.human(None), "?")

    def test_when(self):
        self.assertEqual(mt.when(0), "?")
        self.assertRegex(mt.when(1792462292), r"2026-10-2\d")

    def test_bdecode_and_describe(self):
        one = bencode({"info": {"name": "Film.mkv", "length": 700, "piece length": 16384, "pieces": "x" * 20}})
        self.assertEqual(mt.describe(one), ("Film.mkv", 700, 1))
        many = bencode({"announce": "http://bt.t-ru.org/ann", "info": {
            "name": "Season 1", "files": [{"length": 10, "path": ["a"]}, {"length": 32, "path": ["b", "c"]}]}})
        self.assertEqual(mt.describe(many), ("Season 1", 42, 2))
        self.assertEqual(mt.describe(b"<html>login</html>"), (None, 0, 0))
        self.assertEqual(mt.describe(b"d4:infod4:name3:abcee")[0], "abc")

    def test_read_env(self):
        p = Path(TMP) / "x.env"
        p.write_text("# a comment\nUSERNAME=me\nPASSWORD=p=w\n\nWEB_PASSWORD = deluge \n")
        self.assertEqual(mt.read_env(p), {"USERNAME": "me", "PASSWORD": "p=w", "WEB_PASSWORD": "deluge"})
        self.assertEqual(mt.read_env(Path(TMP) / "missing"), {})


class FakeDeluge(http.server.BaseHTTPRequestHandler):
    """Deluge's web JSON-RPC behind HTTP basic auth, as nginx + deluge-web present it."""
    basic = base64.b64encode(b"door:key").decode()
    web_password = "web-pw"
    calls = []
    connected = False
    torrents = {}

    def log_message(self, *a):
        pass

    def do_POST(self):
        if self.headers.get("Authorization") != "Basic " + self.basic:
            self.send_response(401)
            self.end_headers()
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        m, p = body["method"], body["params"]
        FakeDeluge.calls.append((m, p))
        authed = "_session_id=ok" in (self.headers.get("Cookie") or "")
        res, err = None, None
        if m == "auth.login":
            res = p[0] == self.web_password
        elif not authed:
            err = {"message": "Not authenticated", "code": 1}
        elif m == "web.connected":
            res = FakeDeluge.connected
        elif m == "web.get_hosts":
            res = [["h1", "127.0.0.1", 58846, "Online"]]
        elif m == "web.connect":
            FakeDeluge.connected = True
        elif m == "core.get_torrents_status":
            res = FakeDeluge.torrents
        elif m == "core.get_config_values":
            res = {"download_location": "/downloads", "move_completed": False}
        elif m == "core.get_enabled_plugins":
            res = ["Label"]
        elif m == "label.get_labels":
            res = ["tv", "movies"]
        elif m == "core.add_torrent_file":
            if p[0] in FakeDeluge.torrents:
                err = {"message": "Torrent already in session (abc).", "code": 4}
            else:
                FakeDeluge.torrents[p[0]] = {"name": p[0], "dump": p[1], "options": p[2]}
                res = "a" * 40
        elif m == "core.add_torrent_magnet":
            res = "b" * 40
        elif m == "label.set_torrent":
            FakeDeluge.torrents.setdefault("label", []).append(p)
        out = {"id": body["id"], "result": res, "error": err}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if m == "auth.login" and res:
            self.send_header("Set-Cookie", "_session_id=ok; Path=/")
        self.end_headers()
        self.wfile.write(json.dumps(out).encode())


class DelugeClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), FakeDeluge)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        conf = Path(TMP) / "mochi"
        conf.mkdir(exist_ok=True)
        cls.auth = conf / "deluge.env"
        (conf / "sources.json").write_text(json.dumps({"torrent": {"deluge": {
            "url": f"http://127.0.0.1:{cls.srv.server_port}/deluge", "auth_file": str(cls.auth)}}}))

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.auth.write_text("USERNAME=door\nPASSWORD=key\nWEB_PASSWORD=web-pw\n")
        FakeDeluge.calls, FakeDeluge.connected, FakeDeluge.torrents = [], False, {}

    def test_http_door_closed(self):
        self.auth.write_text("USERNAME=door\nPASSWORD=wrong\n")
        with self.assertRaises(mt.DelugeError) as cm:
            mt.Deluge().login()
        self.assertIn("HTTP login", str(cm.exception))

    def test_wrong_web_password(self):
        with self.assertRaises(mt.DelugeError) as cm:
            mt.Deluge(web_password="nope").login()
        self.assertIn("refused the password", str(cm.exception))

    def test_login_connects_the_daemon_and_adds(self):
        d = mt.Deluge().login()
        self.assertIn(("web.connect", ["h1"]), FakeDeluge.calls)
        self.assertEqual(d.config()["download_location"], "/downloads")
        self.assertEqual(d.labels(), ["movies", "tv"])
        hid = d.add_file("42.torrent", b"d4:infod4:name1:ae", {"download_location": "/x", "add_paused": True})
        self.assertEqual(len(hid), 40)
        self.assertEqual(FakeDeluge.torrents["42.torrent"]["options"], {"download_location": "/x", "add_paused": True})
        self.assertEqual(base64.b64decode(FakeDeluge.torrents["42.torrent"]["dump"]), b"d4:infod4:name1:ae")
        d.set_label(hid, "tv")
        self.assertEqual(FakeDeluge.torrents["label"], [[hid, "tv"]])
        with self.assertRaises(mt.DelugeError) as cm:
            d.add_file("42.torrent", b"d4:infod4:name1:ae", {})
        self.assertIn("already", str(cm.exception))
        self.assertEqual(len(d.add_magnet("magnet:?xt=urn:btih:abc", {})), 40)

    def test_web_password_falls_back_to_the_http_one(self):
        self.auth.write_text("USERNAME=door\nPASSWORD=key\n")
        FakeDeluge.web_password = "key"
        try:
            mt.Deluge().login()  # no WEB_PASSWORD line: the HTTP password is tried
        finally:
            FakeDeluge.web_password = "web-pw"
        self.assertEqual(FakeDeluge.calls[0], ("auth.login", ["key"]))


if __name__ == "__main__":
    unittest.main()
