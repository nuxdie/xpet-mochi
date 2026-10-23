#!/usr/bin/env python3
"""The clocks: what they are shown, the token, a tap on an answer reaching the relay as the pig's own message, and
the relay taking an ask answered on a clock off the desktop pig. Scratch workspace, a real server on a free port."""

import importlib.machinery
import importlib.util
import json
import os
import queue
import socket
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="mochi-clocks-test-")
os.environ["XDG_DATA_HOME"] = TMP
os.environ["XDG_RUNTIME_DIR"] = TMP
os.environ["XDG_CONFIG_HOME"] = TMP

BRAIN = Path(__file__).resolve().parent.parent / "brain"


def load(name):
    spec = importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, str(BRAIN / f"{name}.py")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mc = load("mochi_clocks")
mb = load("mochi_brain")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        mc.CONFIG.parent.mkdir(parents=True, exist_ok=True)
        mc.CONFIG.write_text(json.dumps({"port": cls.port, "token": "sekrit"}))
        cls.sock_path = Path(TMP) / "brain.sock"
        cls.inbox = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        cls.inbox.bind(str(cls.sock_path))
        cls.inbox.settimeout(2)
        cls.asks = {
            "brief": {"text": "morning brief", "options": ["Show me", "Chat about it", "Dismiss"], "at": 100},
            "vat": {"text": "file the vat return?", "options": ["Yes, do it", "Not now", "Never", "Chat about it"],
                    "at": 200},
            "disk": {"text": "the nas disk is failing", "options": ["Ok", "Chat about it"], "urgent": True, "at": 50},
        }
        cls.clocks = mc.Clocks(lambda: cls.asks, lambda: "round", cls.sock_path, log=lambda *_: None)
        mc.SHEET_DIR.mkdir(parents=True, exist_ok=True)
        (mc.SHEET_DIR / "sheet.png").write_bytes(b"png")
        (mc.SHEET_DIR / "sheet.json").write_text("{}")
        cls.clocks.serve()
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.1).close()
                break
            except OSError:
                time.sleep(0.05)

    def call(self, path, body=None, token="sekrit"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=3) as r:
            data = r.read()
            return json.loads(data) if r.headers["Content-Type"] == "application/json" else data

    def test_no_token_no_feed(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.call("/clock?room=kitchen", token="wrong")
        self.assertEqual(e.exception.code, 401)

    def test_calendar_feed_token_in_query_plans_only(self):
        self.clocks.cal = (time.time(), mc.ics([
            {"calendar": "life", "summary": "dentist, upstairs", "location": "somewhere",
             "start": {"dateTime": "2026-10-22T15:00:00+02:00"}, "end": {"dateTime": "2026-10-22T16:00:00+02:00"}},
            {"calendar": "Family", "summary": "trip", "start": {"date": "2026-10-23"}, "end": {"date": "2026-10-25"}},
            {"calendar": "sleep", "summary": "slept", "start": {"date": "2026-10-22"}, "end": {"date": "2026-10-23"}},
            {"calendar": "artem@tsatsin.com", "summary": "skipped call", "attendees": [
                {"self": True, "responseStatus": "declined"}],
             "start": {"dateTime": "2026-10-22T10:00:00Z"}, "end": {"dateTime": "2026-10-22T11:00:00Z"}}]))
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/clock/calendar.ics?token=sekrit", timeout=3) as r:
            t = r.read().decode()
        self.assertIn("DTSTART:20261022T130000Z\r\nDTEND:20261022T140000Z\r\nSUMMARY:dentist\\, upstairs", t)
        self.assertIn("DTSTART;VALUE=DATE:20261023\r\nDTEND;VALUE=DATE:20261025", t)
        self.assertNotIn("slept", t)
        self.assertNotIn("skipped", t)
        self.assertNotIn("somewhere", t)
        with self.assertRaises(urllib.error.HTTPError) as e:  # the query token is for the feed alone
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/clock?room=kitchen&token=sekrit", timeout=3)
        self.assertEqual(e.exception.code, 401)

    def test_feed_urgent_first_and_no_desk_only_options(self):
        f = self.call("/clock?room=kitchen")
        self.assertEqual([a["id"] for a in f["asks"]], ["disk", "vat", "brief"])
        self.assertEqual(f["asks"][1]["options"], ["Yes, do it", "Not now", "Never"])
        self.assertEqual(f["asks"][2]["options"], ["Dismiss"])  # "Show me" opens a file on the desktop
        self.assertEqual(f["busy"], "round")
        self.assertTrue(f["sheet"])

    def test_answer_goes_to_the_relay_as_the_pig_would_send_it(self):
        self.assertTrue(self.call("/clock/answer", {"id": "vat", "label": "Not now", "room": "kitchen"})["ok"])
        m = json.loads(self.inbox.recv(4096))
        self.assertEqual(m, {"event": "answer", "id": "vat", "answer": "Not now", "via": "clock-kitchen"})

    def test_answer_must_be_one_of_the_options(self):
        self.assertFalse(self.call("/clock/answer", {"id": "vat", "label": "Chat about it", "room": "kitchen"})["ok"])
        self.assertFalse(self.call("/clock/answer", {"id": "gone", "label": "Ok", "room": "kitchen"})["ok"])

    def test_urgent_line_until_hushed(self):
        self.clocks.say("the nas disk is failing", "s1")
        self.assertEqual(self.call("/clock?room=corridor")["say"]["text"], "the nas disk is failing")
        self.call("/clock/hush", {"id": "s1", "room": "corridor"})
        self.assertIsNone(self.call("/clock?room=kitchen")["say"])

    def test_lights_and_touch_reach_mochis_context(self):
        self.call("/clock/seen", {"room": "kitchen", "what": "lights on", "lux": 240.0})
        self.call("/clock/seen", {"room": "kitchen", "what": "touch"})
        line = " ".join(self.clocks.status())
        self.assertIn("kitchen clock: lights on since", line)
        self.assertIn("last touched", line)

    def test_sheet_is_served(self):
        self.assertEqual(self.call("/clock/sheet.png"), b"png")


class Pig:
    def __init__(self):
        self.sent = []

    def send(self, **m):
        self.sent.append(m)

    def say(self, text, secs=6):
        self.sent.append({"event": "say", "text": text})

    def ask(self, aid, text, options, urgent=False):
        self.sent.append({"event": "ask", "id": aid})

    def withdraw(self, pid):
        self.sent.append({"event": "withdraw", "id": pid})


class Relay(unittest.TestCase):
    def test_answered_on_a_clock_leaves_the_desktop_menu(self):
        r = type("R", (), {})()
        r.state = {"asks": {"vat": {"text": "file the vat return?", "options": ["Yes, do it", "Not now"], "at": 1,
                                    "session": "", "path": "", "do": ""}}, "events": [], "closed": []}
        r.pet, r.tasks = Pig(), queue.Queue()
        r.tg = type("T", (), {"__getattr__": lambda s, n: lambda *a, **k: None})()
        r.save = lambda: None
        r.event = lambda text: r.state["events"].append(text)
        for name in ("remember_closed", "on_answer"):
            setattr(r, name, getattr(mb.Relay, name).__get__(r))
        r.on_answer("vat", "Not now", via="clock-kitchen")
        self.assertIn({"event": "withdraw", "id": "vat"}, r.pet.sent)
        self.assertNotIn("vat", r.state["asks"])


if __name__ == "__main__":
    unittest.main()
