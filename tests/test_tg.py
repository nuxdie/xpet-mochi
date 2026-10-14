#!/usr/bin/env python3
"""Tests for mochi-tg's consent protocol: the question goes to the relay, only the relay's own process may answer,
and no answer means no send. Telethon is not needed (nothing here talks to Telegram).
Run: python3 -m unittest discover -s tests   (or `make test`)."""

import importlib.machinery
import importlib.util
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="mochi-tg-test-")
os.environ["XDG_DATA_HOME"] = TMP
os.environ["XDG_RUNTIME_DIR"] = TMP
os.environ["XDG_CONFIG_HOME"] = TMP

src = Path(__file__).resolve().parent.parent / "brain" / "mochi_tg.py"
spec = importlib.util.spec_from_loader("mochi_tg", importlib.machinery.SourceFileLoader("mochi_tg", str(src)))
tg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tg)


def answer(sock_path, msg, delay=0.1):
    """Play the relay: connect to mochi-tg's reply socket and send a decision."""
    def go():
        time.sleep(delay)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(sock_path)
        s.sendall(json.dumps(msg).encode() + b"\n")
        s.settimeout(2)
        try:
            s.recv(1024)
        except OSError:
            pass
        s.close()
    threading.Thread(target=go, daemon=True).start()


class Consent(unittest.TestCase):
    def ask(self, decide, relay_pid, timeout=3):
        sent = {}

        def notify(**msg):
            if msg["event"] != "tg_send_ask":  # tg_send_done, when the wait ran out
                sent["done"] = msg
                return True
            sent.update(msg)
            if decide is not None:
                answer(msg["sock"], dict(decide, id=msg["id"]) if decide.get("id") is None else decide)
            return True
        res = tg.ask_on_phone("Nastya", "where is the envelope?", "the We-ID contract", timeout=timeout,
                              relay_pid=relay_pid, notify=notify)
        return res, sent

    def test_allowed_by_the_relay(self):
        (ok, reason, rid), sent = self.ask({"allow": True, "id": None}, relay_pid=os.getpid)
        self.assertTrue(ok)
        self.assertEqual(sent["event"], "tg_send_ask")
        self.assertEqual((sent["to"], sent["text"], sent["why"]), ("Nastya", "where is the envelope?", "the We-ID contract"))
        self.assertEqual(sent["id"], rid)
        self.assertFalse(Path(sent["sock"]).exists())                   # cleaned up

    def test_denied(self):
        (ok, reason, _), _ = self.ask({"allow": False, "reason": "they tapped Deny", "id": None}, relay_pid=os.getpid)
        self.assertFalse(ok)
        self.assertEqual(reason, "they tapped Deny")

    def test_only_the_relay_may_answer(self):
        (ok, reason, _), _ = self.ask({"allow": True, "id": None}, relay_pid=lambda: 1, timeout=1)
        self.assertFalse(ok)                                            # an answer from the wrong pid is ignored...
        self.assertEqual(reason, "they didn't answer in time")          # ...and the wait runs out

    def test_wrong_request_id_is_ignored(self):
        (ok, _, _), _ = self.ask({"allow": True, "id": "0000000000000000"}, relay_pid=os.getpid, timeout=1)
        self.assertFalse(ok)

    def test_no_relay_no_send(self):
        res = tg.ask_on_phone("x", "y", "", timeout=1, relay_pid=os.getpid, notify=lambda **m: False)
        self.assertFalse(res[0])
        self.assertIn("mochi-brain isn't running", res[1])

    def test_api_creds(self):
        self.assertEqual(tg.api_creds(), (None, None))
        tg.API_FILE.parent.mkdir(parents=True, exist_ok=True)
        tg.API_FILE.write_text(json.dumps({"api_id": 123, "api_hash": "abc"}))
        self.assertEqual(tg.api_creds(), (123, "abc"))


if __name__ == "__main__":
    unittest.main()
