"""mochi-phone: the sealed box, zones, stays, and folding phone events into latest.json."""
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("mochi_phone", HERE.parent / "brain" / "mochi_phone.py")
mp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mp)


class Box(unittest.TestCase):
    def setUp(self):
        self.desk, self.phone = X25519PrivateKey.generate(), X25519PrivateKey.generate()

    def test_roundtrip_both_ways(self):
        for a, b in ((self.desk, self.phone), (self.phone, self.desk)):
            blob = mp.seal(b'{"x": 1}', a, b.public_key())
            self.assertEqual(blob[:2], b"M1")
            self.assertEqual(mp.open_sealed(blob, b, a.public_key()), b'{"x": 1}')

    def test_wrong_keys_fail(self):
        blob = mp.seal(b"hi", self.phone, self.desk.public_key())
        other = X25519PrivateKey.generate()
        with self.assertRaises(Exception):
            mp.open_sealed(blob, self.desk, other.public_key())  # a forged sender
        with self.assertRaises(Exception):
            mp.open_sealed(blob, other, self.phone.public_key())  # not the recipient

    def test_tamper_fails(self):
        blob = bytearray(mp.seal(b"hi", self.phone, self.desk.public_key()))
        blob[-1] ^= 1
        with self.assertRaises(Exception):
            mp.open_sealed(bytes(blob), self.desk, self.phone.public_key())

    def test_fresh_ephemeral_each_time(self):
        a = mp.seal(b"hi", self.phone, self.desk.public_key())
        b = mp.seal(b"hi", self.phone, self.desk.public_key())
        self.assertNotEqual(a[2:34], b[2:34])


class Zones(unittest.TestCase):
    cfg = {"places": {"home": [52.0, 4.0, 150], "gym": [52.01, 4.0]}}

    def test_home_and_away(self):
        self.assertEqual(mp.zone_of(self.cfg, 52.0, 4.0, 10), "home")
        self.assertEqual(mp.zone_of(self.cfg, 52.0005, 4.0, 10), "home")   # ~55 m
        self.assertEqual(mp.zone_of(self.cfg, 52.005, 4.0, 10), "away")    # ~550 m
        self.assertEqual(mp.zone_of(self.cfg, 52.01, 4.0, 10), "gym")

    def test_poor_fix_widens_a_little(self):
        self.assertEqual(mp.zone_of(self.cfg, 52.002, 4.0, 100), "home")   # 220 m with ±100 m
        self.assertEqual(mp.zone_of(self.cfg, 52.002, 4.0, 10), "away")

    def test_distance(self):
        self.assertAlmostEqual(mp.distance_m(52.0, 4.0, 52.0, 4.0), 0)
        self.assertAlmostEqual(mp.distance_m(52.0, 4.0, 52.001, 4.0), 111, delta=2)


class Stays(unittest.TestCase):
    def test_groups_fixes_into_stays(self):
        t0 = 1_700_000_000
        evs = [{"type": "loc", "lat": 52.0, "lon": 4.0, "t": t0 + i * 60} for i in range(20)]            # 19 min at home
        evs += [{"type": "loc", "lat": 52.0 + i * 0.002, "lon": 4.0, "t": t0 + 1200 + i * 60} for i in range(5)]  # moving
        evs += [{"type": "loc", "lat": 52.05, "lon": 4.0, "t": t0 + 1500 + i * 60} for i in range(12)]   # 11 min somewhere
        cfg = {"places": {"home": [52.0, 4.0, 150]}}
        with mock.patch.object(mp, "geocode", return_value="Some Street"):
            ss = mp.stops(cfg, evs)
        self.assertEqual([s["where"] for s in ss], ["home", "Some Street"])
        self.assertEqual(ss[0]["end"] - ss[0]["start"], 20 * 60)  # the first "moving" fix is still within home


class Folding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        for name in ("LATEST", "EVENTS", "SENT", "GEOCACHE", "STATE"):
            setattr(mp, name, d / f"{name.lower()}.json")
        mp.DATA = d
        self.cfg = {"places": {"home": [52.0, 4.0, 150]}}
        self.triggers = []
        self.p1 = mock.patch.object(mp, "trigger", side_effect=self.triggers.append)
        self.p2 = mock.patch.object(mp, "geocode", return_value="Some Street 1, Town")
        self.p1.start(); self.p2.start()

    def tearDown(self):
        self.p1.stop(); self.p2.stop()
        self.tmp.cleanup()

    def test_latest_and_transitions(self):
        latest, state = {}, {}
        now = int(time.time())
        mp.apply_events(self.cfg, [{"type": "loc", "lat": 52.0, "lon": 4.0, "acc": 20, "t": now - 600},
                                   {"type": "state", "battery": 80, "wifi": "casa", "t": now - 600}], latest, state)
        self.assertEqual(latest["loc"]["zone"], "home")
        self.assertEqual(latest["state"]["battery"], 80)
        self.assertEqual(self.triggers, [])  # the first zone is not news
        mp.apply_events(self.cfg, [{"type": "loc", "lat": 52.05, "lon": 4.0, "acc": 20, "t": now}], latest, state)
        self.assertEqual(latest["loc"]["zone"], "away")
        self.assertEqual(latest["loc"]["address"], "Some Street 1, Town")
        self.assertEqual(self.triggers, ["their phone left home"])
        mp.apply_events(self.cfg, [{"type": "loc", "lat": 52.0001, "lon": 4.0, "acc": 20, "t": now + 60}], latest, state)
        self.assertEqual(self.triggers[-1], "their phone: home")
        self.assertEqual(len(mp.EVENTS.read_text().splitlines()), 4)
        self.assertIn("home", mp.where([]))

    def test_reply_is_handed_to_the_brain(self):
        with mock.patch.object(mp.subprocess, "run") as run:
            mp.apply_events(self.cfg, [{"type": "reply", "ask_id": "x1", "option": "Yes", "t": int(time.time())}], {}, {})
        self.assertEqual(run.call_args[0][0][:3], ["mochi-brain", "--answer", "x1"])
        self.assertTrue(any("x1" in t for t in self.triggers))

    def test_where_without_data(self):
        self.assertIn("nothing from the phone yet", mp.where([]))


if __name__ == "__main__":
    unittest.main()


class PhoneSenses(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        for name in ("LATEST", "EVENTS", "SENT", "GEOCACHE", "STATE"):
            setattr(mp, name, d / f"{name.lower()}.json")
        mp.DATA = d
        self.p1 = mock.patch.object(mp, "trigger")
        self.p2 = mock.patch.object(mp, "geocode", return_value="Some Street 1, Town")
        self.p1.start(); self.p2.start()
        now = int(time.time())
        self.cfg = {"places": {"home": [52.0, 4.0, 150]}}
        self.latest, self.state = {}, {}
        mp.apply_events(self.cfg, [
            {"type": "call", "id": 1, "dir": "missed", "number": "+31600000000", "name": "Mom", "duration": 0, "t": now - 3000},
            {"type": "call", "id": 2, "dir": "out", "number": "+31600000001", "name": "", "duration": 95, "t": now - 2000},
            {"type": "sms", "id": 3, "dir": "in", "address": "DHL", "name": "", "text": "Your parcel comes tomorrow", "t": now - 1000},
            {"type": "notif", "app": "com.whatsapp", "label": "WhatsApp", "title": "Nastya", "sender": "Nastya", "text": "see you at 7?", "t": now - 900},
            {"type": "notif", "app": "com.whatsapp", "label": "WhatsApp", "title": "Nastya", "sender": "Nastya", "text": "or 8", "t": now - 890},
            {"type": "notif", "app": "org.telegram.messenger", "label": "Telegram", "title": "Denis", "text": "hey", "t": now - 800},
            {"type": "notif", "app": "com.android.vending", "label": "Play Store", "title": "3 updates", "text": "...", "t": now - 700},
            {"type": "media", "app": "com.spotify.music", "label": "Spotify", "title": "Song", "artist": "Band", "playing": True, "t": now - 600},
            {"type": "usage", "day": time.strftime("%Y-%m-%d"), "apps": {"Chrome": 3600, "WhatsApp": 600}, "t": now - 500},
            {"type": "state", "battery": 60, "charging": False, "wifi": "", "net": "mobile", "screen": False,
             "ringer": "vibrate", "dnd": True, "audio": ["Pixel Buds"], "t": now - 400},
        ], self.latest, self.state)

    def tearDown(self):
        self.p1.stop(); self.p2.stop()
        self.tmp.cleanup()

    def test_rings_and_latest(self):
        self.assertEqual(len(self.latest["calls"]), 2)
        self.assertEqual(len(self.latest["notifs"]), 4)
        self.assertEqual(self.latest["media"]["title"], "Song")
        self.assertIn("Chrome", list(self.latest["usage"].values())[0])

    def test_readers(self):
        self.assertIn("missed: Mom", mp.calls([]))
        self.assertIn("1m35s", mp.calls([]))
        self.assertIn("from DHL: Your parcel", mp.sms([]))
        self.assertIn("WhatsApp: Nastya: see you at 7?", mp.notifs([]))
        self.assertNotIn("Play Store", mp.notifs(["--app", "whatsapp"]))
        self.assertIn("1h10m on the phone", mp.usage([]))
        self.assertIn("playing Song — Band (Spotify", mp.now([]))
        self.assertIn("Pixel Buds", mp.now([]))
        self.assertIn("do not disturb", mp.now([]))

    def test_feed_keys_the_news(self):
        items = json.loads(mp.feed([]))["items"]
        kinds = sorted(k.rsplit(":", 1)[0] if k.startswith("phone:notif") else k for k in items)
        self.assertIn("phone:call:1", kinds)                 # missed call
        self.assertNotIn("phone:call:2", kinds)              # their own outgoing call is not news
        self.assertIn("phone:sms:3", kinds)
        whats = [k for k in items if k.startswith("phone:notif:WhatsApp:Nastya")]
        self.assertEqual(len(whats), 1)                      # two messages, one item
        self.assertIn("2 message(s) from Nastya", items[whats[0]]["line"])
        self.assertTrue([k for k in items if k.startswith("phone:notif:Telegram:Denis")])  # a backup for the archive
        self.assertFalse([k for k in items if "Play Store" in k])

    def test_digest_mentions_everything(self):
        d = mp.digest([])
        for part in ("## Their phone", "missed Mom", "from DHL", "WhatsApp 2 (Nastya)", "On the phone today", "Chrome 60m"):
            self.assertIn(part, d)
