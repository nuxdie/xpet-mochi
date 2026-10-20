#!/usr/bin/env python3
"""Location errands: opening hours, places near a fix, the calendar's free time, and the relay's gates and answers.
Uses a scratch workspace, never ~/.local/share/mochi; no network (Overpass and the calendar are faked)."""

import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="mochi-errands-test-")
os.environ["XDG_DATA_HOME"] = TMP
os.environ["XDG_RUNTIME_DIR"] = TMP
os.environ["XDG_CONFIG_HOME"] = TMP
os.environ["MOCHI_PHONE_CONFIG"] = str(Path(TMP) / "phone.json")

BRAIN = Path(__file__).resolve().parent.parent / "brain"


def load(name):
    spec = importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, str(BRAIN / f"{name}.py")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


me = load("mochi_errands")
mb = load("mochi_brain")

HOME = (52.36703, 4.89007)
SHOP = {"name": "Kruidvat", "addr": "Utrechtsestraat 1", "lat": 52.36734, "lon": 4.89259,
        "hours": "Mo-Sa 10:00-21:00; Su 11:00-21:00"}
NEAR_SHOP = (52.36745, 4.89300)  # ~30 m from it
TUE_NOON = dt.datetime(2026, 10, 20, 12, 0)


def fetch_shop(filters, s, w, n, e):
    return [dict(SHOP)]


class Hours(unittest.TestCase):
    def test_days_and_spans(self):
        spec = "Mo-Sa 08:00-20:00; Su 12:00-18:00"
        self.assertTrue(me.open_at(spec, dt.datetime(2026, 10, 20, 9, 0)))
        self.assertFalse(me.open_at(spec, dt.datetime(2026, 10, 25, 11, 0)))
        self.assertFalse(me.open_at(spec, dt.datetime(2026, 10, 20, 19, 55), for_minutes=10))  # closes before it's done

    def test_past_midnight_and_off(self):
        self.assertTrue(me.open_at("Mo-Fr 18:00-02:00", dt.datetime(2026, 10, 24, 1, 0)))  # Friday's night, on Sat
        self.assertFalse(me.open_at("Mo-Fr 18:00-02:00", dt.datetime(2026, 10, 25, 1, 0)))
        self.assertFalse(me.open_at("Mo-Su 09:00-17:00; Su off", dt.datetime(2026, 10, 25, 12, 0)))

    def test_unknown_is_not_a_no(self):
        self.assertIsNone(me.open_at("sunrise-sunset", TUE_NOON))
        self.assertIsNone(me.open_at("", TUE_NOON))
        self.assertTrue(me.open_at("24/7", TUE_NOON))


class Nearest(unittest.TestCase):
    def test_kind_of_place_within_reach_and_open(self):
        e = {"id": "batteries", "do": "buy AA batteries", "osm": "brand=Kruidvat"}
        p = me.nearest(e, *NEAR_SHOP, cache={}, fetch=fetch_shop, when=TUE_NOON)
        self.assertEqual(p["name"], "Kruidvat")
        self.assertLess(p["d"], 60)
        self.assertIsNone(me.nearest(e, *NEAR_SHOP, cache={}, fetch=fetch_shop, when=dt.datetime(2026, 10, 20, 8, 0)))
        self.assertIsNone(me.nearest(e, 52.3800, 4.9000, cache={}, fetch=fetch_shop, when=TUE_NOON))  # 1.5 km off

    def test_lookups_are_cached_per_tile(self):
        calls = []
        cache = {}
        e = {"id": "x", "do": "x", "osm": ["brand=Kruidvat"]}
        for _ in range(3):
            me.nearest(e, *NEAR_SHOP, cache=cache, fetch=lambda *a: calls.append(a) or [dict(SHOP)], when=TUE_NOON)
        self.assertEqual(len(calls), 1)

    def test_a_failed_lookup_is_not_cached(self):
        def down(*a):
            raise OSError("overpass down")
        cache = {}
        self.assertIsNone(me.places_of_kind(["shop=chemist"], *NEAR_SHOP, cache, fetch=down))
        self.assertEqual(cache, {})

    def test_concrete_place_and_named_zone(self):
        Path(os.environ["MOCHI_PHONE_CONFIG"]).write_text(json.dumps({"places": {"gym": [52.36, 4.88, 100]}}))
        e = {"id": "locker", "do": "empty the locker", "at": ["gym", {"name": "post", "lat": 1, "lon": 1}]}
        self.assertEqual(me.nearest(e, 52.3601, 4.8801, when=TUE_NOON)["name"], "gym")
        self.assertIsNone(me.nearest(e, 52.37, 4.88, when=TUE_NOON))

    def test_the_link_for_telegram_carries_no_coordinates(self):
        self.assertNotIn("52.", me.maps_link(SHOP))
        self.assertIn("Kruidvat", me.maps_link(SHOP))


class Calendar(unittest.TestCase):
    def rows(self):
        tz = "+02:00"
        return [{"calendar": "life", "summary": "Pennylane", "start": {"dateTime": f"2026-10-20T17:00:00{tz}"},
                 "end": {"dateTime": f"2026-10-20T17:30:00{tz}"}},
                {"calendar": "sleep", "summary": "Sleep", "start": {"dateTime": f"2026-10-20T11:00:00{tz}"},
                 "end": {"dateTime": f"2026-10-20T13:00:00{tz}"}},
                {"calendar": "life", "summary": "holiday", "start": {"date": "2026-10-20"}, "end": {"date": "2026-10-21"}},
                {"calendar": "artem@tsatsin.com", "summary": "declined", "attendees": [{"self": True, "responseStatus": "declined"}],
                 "start": {"dateTime": f"2026-10-20T12:30:00{tz}"}, "end": {"dateTime": f"2026-10-20T13:00:00{tz}"}}]

    def test_logs_all_day_and_declined_dont_hold_them(self):
        spans = me.busy_spans(self.rows())
        self.assertEqual([t for _, _, t in spans], ["Pennylane"])
        noon = dt.datetime.fromisoformat("2026-10-20T12:00:00+02:00")
        self.assertEqual(me.free_until(spans, noon).hour, 17)
        self.assertEqual(me.free_until(spans, noon.replace(hour=17, minute=10)), noon.replace(hour=17, minute=10))
        self.assertIsNone(me.free_until(spans, noon.replace(hour=18)))


class RelayErrands(unittest.TestCase):
    def setUp(self):
        me_path = Path(TMP) / "mochi" / "memory" / "errands.json"
        me_path.parent.mkdir(parents=True, exist_ok=True)
        me_path.write_text(json.dumps([{"id": "batteries", "do": "buy AA batteries", "osm": "brand=Kruidvat", "minutes": 5}]))
        self.quiet = False
        self.er = mb.Errands({}, lambda: self.quiet)
        self.er.busy = lambda: []
        self.er.fetch = fetch_shop

    def latest(self, lat=NEAR_SHOP[0], lon=NEAR_SHOP[1], zone="away", t=None, **kw):
        t = t or time.time()
        loc = {"lat": lat, "lon": lon, "acc": 10, "t": t, "zone": zone, "speed": kw.pop("speed", 1.0)}
        return dict({"loc": loc, "state": {"dnd": False}, "notifs": [], "calls": []}, **kw)

    def run_check(self, latest, when=None):
        when = when or dt.datetime.now().replace(hour=12, minute=0)
        self.er.check(latest, t=when.timestamp())
        out = []
        while not self.er.ready.empty():
            out.append(self.er.ready.get_nowait())
        return out

    def at_noon(self):
        return dt.datetime.combine(dt.date.today(), dt.time(12, 0))

    def test_near_open_free_out_gets_one_poke(self):
        when = self.at_noon()
        got = self.run_check(self.latest(t=when.timestamp()), when)
        self.assertEqual([k for k, _ in got], ["poke"])
        text, tg, app = self.er.poke_text(got[0][1]["e"], got[0][1]["place"], "17:00")
        self.assertIn("buy AA batteries", text)
        self.assertIn("free till 17:00", text)
        self.assertNotIn("Utrechtsestraat", tg)  # the street never crosses Telegram...
        self.assertIn("Utrechtsestraat", app)    # ...but the sealed line to the app may carry it

    def test_gates(self):
        when = self.at_noon()
        t = when.timestamp()
        self.assertEqual(self.run_check(self.latest(zone="home", t=t), when), [])
        self.assertEqual(self.run_check(self.latest(speed=15, t=t), when), [])
        self.assertEqual(self.run_check(self.latest(t=t - 3600), when), [])
        self.assertEqual(self.run_check(self.latest(t=t, state={"dnd": True}), when), [])
        self.assertEqual(self.run_check(self.latest(t=t, notifs=[{"app": "com.ubercab.driver", "t": t - 600}]), when), [])
        self.quiet = True
        self.assertEqual(self.run_check(self.latest(t=t), when), [])

    def test_busy_soon_waits(self):
        when = self.at_noon().astimezone()
        self.er.busy = lambda: [(when + dt.timedelta(minutes=10), when + dt.timedelta(hours=1), "call")]
        self.assertEqual(self.run_check(self.latest(t=when.timestamp()), when.replace(tzinfo=None)), [])

    def test_answers_and_cooldown(self):
        when = self.at_noon()
        t = when.timestamp()
        got = self.run_check(self.latest(t=t), when)
        self.er.poked(got[0][1], "errand-batteries")
        self.assertEqual(self.run_check(self.latest(t=t), when), [])  # one at a time while it's pending
        self.assertIn("put off", self.er.answer("batteries", "Not now"))
        self.assertEqual(self.run_check(self.latest(t=t), when), [])  # cooling down
        self.assertIn("done", self.er.answer("batteries", "Done"))
        later = when + dt.timedelta(days=2)
        self.assertEqual(self.run_check(self.latest(t=later.timestamp()), later), [])  # done for good

    def test_walking_on_takes_it_back(self):
        when = self.at_noon()
        got = self.run_check(self.latest(t=when.timestamp()), when)
        self.er.poked(got[0][1], "errand-batteries")
        got = self.run_check(self.latest(lat=52.3800, lon=4.9000, t=when.timestamp()), when)
        self.assertEqual([k for k, _ in got], ["passed"])

    def test_daily_cap(self):
        when = self.at_noon()
        self.er.state["counts_day"], self.er.state["today"] = dt.date.today().isoformat(), mb.ERRAND_PER_DAY
        self.assertEqual(self.run_check(self.latest(t=when.timestamp()), when), [])


class FakeTG:
    on = True

    def __init__(self):
        self.asks, self.closed = [], []

    def ask(self, aid, a, buzz=False):
        self.asks.append((aid, a.get("tg_text") or a["text"], buzz))
        return 42

    def close(self, a, label):
        self.closed.append(label)

    def may_buzz(self, urgent, critical=False):
        return False

    def __getattr__(self, name):
        return lambda *a, **k: None


class RelayPoke(unittest.TestCase):
    def relay(self):
        import queue
        r = type("R", (), {})()
        r.state = {"asks": {}, "events": [], "closed": []}
        r.pet = type("P", (), {"__getattr__": lambda s, n: lambda *a, **k: None})()
        r.tg, r.tasks = FakeTG(), queue.Queue()
        r.away, r.save = (lambda: False), (lambda: None)
        r.event = lambda text: r.state["events"].append(text)
        r.errands = mb.Errands({}, lambda: False)
        for name in ("add_ask", "remember_closed", "on_answer", "drain_errands"):
            setattr(r, name, getattr(mb.Relay, name).__get__(r))
        return r

    def test_poke_buzzes_the_phone_and_done_closes_it(self):
        r = self.relay()
        e = {"id": "batteries", "do": "buy AA batteries", "minutes": 5}
        r.errands.ready.put(("poke", {"e": e, "place": dict(SHOP, d=32.0), "free": "17:00", "calendar": True}))
        r.drain_errands()
        aid, text, buzz = r.tg.asks[0]
        self.assertEqual(aid, "errand-batteries")
        self.assertTrue(buzz)  # at the desk or not: they're out, and this is what they asked to be poked for
        self.assertIn("maps", text)
        r.on_answer("errand-batteries", "Done", via="telegram")
        self.assertEqual(r.tg.closed, ["Done"])
        self.assertTrue(r.tasks.empty())  # nothing to run: Mochi hears it as an event
        self.assertIn("errand done", r.state["events"][-1])
        self.assertIsNone(r.errands.state["pending"])
        self.assertTrue(r.errands.state["items"]["batteries"]["done"])

    def test_poke_goes_through_the_app_when_it_is_alive(self):
        r = self.relay()
        sent = []
        r.errands.app_fresh = lambda: True
        orig = mb.phone_send
        mb.phone_send = lambda *a: sent.append(a) or True
        try:
            e = {"id": "parcel", "do": "pick up the parcel", "minutes": 5}
            r.errands.ready.put(("poke", {"e": e, "place": dict(SHOP, d=30.0), "free": None, "calendar": True}))
            r.drain_errands()
            self.assertEqual(r.tg.asks, [])  # not twice: the app's buzz is the poke
            self.assertEqual(sent[0][0], "ask")
            self.assertIn("--buzz", sent[0])
            self.assertTrue(any(str(x).startswith("geo:") for x in sent[0]))
            r.on_answer("errand-parcel", "Not now", via="pig")
            self.assertEqual(sent[-1], ("close", "--id", "errand-parcel"))  # off the phone's shade too
        finally:
            mb.phone_send = orig


if __name__ == "__main__":
    unittest.main()
