#!/usr/bin/env python3
"""Tests for mochi-calendar and mochi-contacts' offline parts: times, event lines, links, contact matching and
birthdays. No network, no token. Run: python3 -m unittest discover -s tests   (or `make test`)."""

import base64
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


def load(name):
    src = Path(__file__).resolve().parent.parent / "brain" / f"{name}.py"
    spec = importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, str(src)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mc = load("mochi_calendar")
mp = load("mochi_contacts")
TODAY = dt.date(2026, 10, 13)


class Times(unittest.TestCase):
    def test_parse_when(self):
        self.assertEqual(mc.parse_when("2026-10-20 15:00"), (dt.date(2026, 10, 20), dt.time(15, 0)))
        self.assertEqual(mc.parse_when("2026-10-20T09:30"), (dt.date(2026, 10, 20), dt.time(9, 30)))
        self.assertEqual(mc.parse_when("2026-10-20"), (dt.date(2026, 10, 20), None))
        self.assertEqual(mc.parse_when("tomorrow 9:05", TODAY), (dt.date(2026, 10, 14), dt.time(9, 5)))
        self.assertEqual(mc.parse_when("Today", TODAY), (TODAY, None))
        for bad in ("next friday", "2026-10-20 25:00", "2026-10-20 3pm", ""):
            with self.assertRaises(SystemExit, msg=bad):
                mc.parse_when(bad)

    def test_duration(self):
        self.assertEqual(mc.parse_duration("1h30m"), dt.timedelta(minutes=90))
        self.assertEqual(mc.parse_duration("45m"), dt.timedelta(minutes=45))
        self.assertEqual(mc.parse_duration("2d"), dt.timedelta(days=2))
        with self.assertRaises(SystemExit):
            mc.parse_duration("an hour")

    def test_span(self):
        tz = "Europe/Amsterdam"
        self.assertEqual(mc.span("2026-10-20 15:00", tz=tz),
                         ({"dateTime": "2026-10-20T15:00:00", "timeZone": tz}, {"dateTime": "2026-10-20T16:00:00", "timeZone": tz}))
        self.assertEqual(mc.span("2026-10-20 23:30", dur=dt.timedelta(hours=1), tz=tz)[1]["dateTime"], "2026-10-21T00:30:00")
        self.assertEqual(mc.span("2026-10-20 15:00", "2026-10-20 15:45", tz=tz)[1]["dateTime"], "2026-10-20T15:45:00")
        # all day: the end is exclusive
        self.assertEqual(mc.span("2026-10-20", tz=tz), ({"date": "2026-10-20"}, {"date": "2026-10-21"}))
        self.assertEqual(mc.span("2026-10-20", "2026-10-22", tz=tz)[1], {"date": "2026-10-23"})
        self.assertEqual(mc.span("2026-10-20", dur=dt.timedelta(days=3), tz=tz)[1], {"date": "2026-10-23"})
        for args in (("2026-10-20 15:00", "2026-10-20 14:00"), ("2026-10-20 15:00", "2026-10-21"), ("2026-10-20", "2026-10-21 10:00")):
            with self.assertRaises(SystemExit, msg=args):
                mc.span(*args, tz=tz)


class Events(unittest.TestCase):
    def test_line_all_day(self):
        ev = {"id": "e1", "summary": "Trip", "start": {"date": "2026-10-20"}, "end": {"date": "2026-10-23"}}
        self.assertEqual(mc.line(ev, "life", "nuxdie"), "- all day, to Thu 22 Oct Trip · life · nuxdie · id=e1")
        ev["end"] = {"date": "2026-10-21"}
        self.assertTrue(mc.line(ev).startswith("- all day Trip"))

    def test_line_timed(self):
        start = dt.datetime(2026, 10, 20, 13, 0, tzinfo=dt.timezone.utc)
        ev = {"id": "e2", "summary": "Call", "location": "Zoom\n room",
              "start": {"dateTime": start.isoformat()}, "end": {"dateTime": (start + dt.timedelta(minutes=30)).isoformat()},
              "attendees": [{"email": "x@y", "responseStatus": "accepted"}, {"self": True, "responseStatus": "needsAction"}]}
        local = start.astimezone()
        self.assertEqual(mc.line(ev), f"- {local:%H:%M}–{local + dt.timedelta(minutes=30):%H:%M} Call · Zoom room · NOT ANSWERED · id=e2")

    def test_event_link(self):
        eid = base64.urlsafe_b64encode(b"abc123 artem@tsatsin.com").decode().rstrip("=")
        self.assertEqual(mc.event_id(f"https://www.google.com/calendar/event?eid={eid}"), "abc123")
        self.assertEqual(mc.event_id(" abc123 "), "abc123")


def person(name, account="artem", birthday=None, **kw):
    p = {"id": "people/c1", "account": account, "other": False, "name": name, "nicknames": [], "emails": [],
         "phones": [], "orgs": [], "relations": [], "addresses": [], "urls": [], "notes": "", "events": [],
         "labels": [], "birthday": birthday}
    p.update(kw)
    return p


class Contacts(unittest.TestCase):
    def test_simplify(self):
        p = mp.simplify({"resourceName": "people/c9", "names": [{"displayName": "Anna K"}],
                         "phoneNumbers": [{"value": "06 1234 5678", "canonicalForm": "+31612345678"}],
                         "birthdays": [{"date": {"month": 3, "day": 4}}, {"text": "4 March"}],
                         "memberships": [{"contactGroupMembership": {"contactGroupResourceName": "contactGroups/abc"}}]},
                        "nuxdie")
        self.assertEqual((p["name"], p["phones"], p["birthday"], p["labels"]),
                         ("Anna K", ["+31612345678"], {"month": 3, "day": 4}, ["contactGroups/abc"]))

    def test_matches(self):
        p = person("Tsatsin Artem", emails=["a@x.com"], phones=["+31 6 1234 5678"], nicknames=["Тёма"])
        for q in ("artem", "artem tsatsin", "A@X", "0612345678", "тёма"):
            self.assertTrue(mp.matches(p, q), q)
        for q in ("bob", "", "1234"):
            self.assertFalse(mp.matches(p, q), q)

    def test_merge(self):
        a = person("Anna", "artem", emails=["a@x"])
        b = person("Anna", "nuxdie", emails=["a@x", "anna@y"], birthday={"month": 1, "day": 2})
        (m,) = mp.merge([a, b])
        self.assertEqual((m["account"], m["emails"], m["birthday"]), ("artem+nuxdie", ["a@x", "anna@y"], {"month": 1, "day": 2}))
        self.assertEqual(a["emails"], ["a@x"])  # the cache's own entries aren't changed

    def test_birthdays(self):
        people = [person("Soon", birthday={"year": 1990, "month": 10, "day": 25}),
                  person("Today", birthday={"month": 10, "day": 13}),
                  person("Past", birthday={"month": 10, "day": 1}),
                  person("Leap", birthday={"month": 2, "day": 29}),
                  person("None")]
        rows = mp.upcoming(people, 20, TODAY)
        self.assertEqual([(d, p["name"], age) for d, p, age in rows],
                         [(TODAY, "Today", None), (dt.date(2026, 10, 25), "Soon", 36)])
        self.assertEqual(mp.upcoming(people, 365, TODAY)[-1][0], dt.date(2027, 10, 1))
        leap = [r for r in mp.upcoming(people, 365, TODAY) if r[1]["name"] == "Leap"]
        self.assertEqual(leap[0][0], dt.date(2027, 3, 1))

    def test_date_text(self):
        self.assertEqual(mp.date_text({"month": 3, "day": 4}), "04 Mar")
        self.assertEqual(mp.date_text({"year": 1990, "month": 12, "day": 31}), "31 Dec 1990")


if __name__ == "__main__":
    unittest.main()
