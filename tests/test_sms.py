#!/usr/bin/env python3
"""Tests for mochi-sms's offline parts: Messages for Web's time stamps and aria-labels, the key that pairs a
conversation across the two sources, the merge of both lists, and the cache's stable handles. No phone, no Chrome,
no D-Bus. Run: python3 -m unittest discover -s tests   (or `make test`)."""

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

src = Path(__file__).resolve().parent.parent / "brain" / "mochi_sms.py"
spec = importlib.util.spec_from_loader("mochi_sms", importlib.machinery.SourceFileLoader("mochi_sms", str(src)))
ms = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ms)

NOW = dt.datetime(2026, 10, 10, 15, 40)


def ms_of(*args):
    return int(dt.datetime(*args).timestamp() * 1000)


class RelativeStamps(unittest.TestCase):
    def test_minutes_are_exact_enough(self):
        stamp, exact = ms.parse_rel("5 min", NOW)
        self.assertEqual(stamp, ms_of(2026, 10, 10, 15, 35))
        self.assertTrue(exact)

    def test_now(self):
        self.assertEqual(ms.parse_rel("now", NOW), (ms_of(2026, 10, 10, 15, 40), True))

    def test_hours_are_approximate(self):
        stamp, exact = ms.parse_rel("3 hr", NOW)
        self.assertEqual(stamp, ms_of(2026, 10, 10, 12, 40))
        self.assertFalse(exact)

    def test_yesterday_and_weekday(self):
        self.assertEqual(ms.parse_rel("Yesterday", NOW)[0], ms_of(2026, 10, 9, 12, 0))
        self.assertEqual(ms.parse_rel("Tue", NOW)[0], ms_of(2026, 10, 6, 12, 0))  # 10 Oct 2026 is a Saturday
        self.assertEqual(ms.parse_rel("Sat", NOW)[0], ms_of(2026, 10, 3, 12, 0))  # the same weekday means last week

    def test_day_and_month(self):
        self.assertEqual(ms.parse_rel("29 Sept", NOW)[0], ms_of(2026, 9, 29, 12, 0))
        self.assertEqual(ms.parse_rel("2 Aug 2025", NOW)[0], ms_of(2025, 8, 2, 12, 0))
        self.assertEqual(ms.parse_rel("Dec 24", NOW)[0], ms_of(2025, 12, 24, 12, 0))  # in the future → last year

    def test_numeric_date(self):
        self.assertEqual(ms.parse_rel("29/09/2026", NOW)[0], ms_of(2026, 9, 29, 12, 0))

    def test_garbage_is_now_and_not_exact(self):
        stamp, exact = ms.parse_rel("??", NOW)
        self.assertFalse(exact)
        self.assertEqual(stamp, ms_of(2026, 10, 10, 15, 40))


class AbsoluteStamps(unittest.TestCase):
    def test_british(self):
        self.assertEqual(ms.parse_abs("10 October 2026 at 14:34", NOW), ms_of(2026, 10, 10, 14, 34))

    def test_american(self):
        self.assertEqual(ms.parse_abs("October 10, 2026, 2:34 PM", NOW), ms_of(2026, 10, 10, 14, 34))
        self.assertEqual(ms.parse_abs("October 10, 2026 at 12:05 AM", NOW), ms_of(2026, 10, 10, 0, 5))

    def test_this_year_when_no_year(self):
        self.assertEqual(ms.parse_abs("29 Sept at 16:09", NOW), ms_of(2026, 9, 29, 16, 9))

    def test_time_only_is_today(self):
        self.assertEqual(ms.parse_abs("14:34", NOW), ms_of(2026, 10, 10, 14, 34))

    def test_no_time_is_none(self):
        self.assertIsNone(ms.parse_abs("10 October 2026", NOW))


class Labels(unittest.TestCase):
    def test_received(self):
        sender, body, stamp = ms.parse_label("Apple said: Your Apple Account Code is: 461910. Don't share it with anyone.. "
                                             "Received on 10 October 2026 at 15:36.")
        self.assertEqual(sender, "Apple")
        self.assertEqual(body, "Your Apple Account Code is: 461910. Don't share it with anyone.")
        self.assertEqual(dt.datetime.fromtimestamp(stamp / 1000), dt.datetime(2026, 10, 10, 15, 36))

    def test_sent(self):
        sender, body, stamp = ms.parse_label("You said: on my way. Sent on 9 October 2026 at 08:01.")
        self.assertEqual((sender, body), ("You", "on my way"))
        self.assertIsNotNone(stamp)

    def test_no_stamp(self):
        self.assertEqual(ms.parse_label("")[2], None)


class Keys(unittest.TestCase):
    def test_numbers_match_across_formats(self):
        self.assertEqual(ms.key_of("06 16004546"), ms.key_of("+31616004546"))
        self.assertEqual(ms.key_of("0970 102 41706"), ms.key_of("+3197010241706"))

    def test_names_lowered(self):
        self.assertEqual(ms.key_of("Apple"), ms.key_of("apple "))
        self.assertNotEqual(ms.key_of("Apple"), ms.key_of("Apple Store"))

    def test_a_name_is_not_a_number(self):
        self.assertTrue(ms.key_of("AUTHMSG").startswith("s:"))
        self.assertTrue(ms.key_of("1233").startswith("s:"))  # too short to be a number


def web(name, body, date, exact=True, read=True, conv="c1", handle=1):
    return {"thread": f"w{handle}", "uid": f"{conv}:s:x", "date": date, "exact": exact, "type": "in", "read": read, "body": body,
            "addresses": [name], "multi": False, "attachments": 0, "sub": 0, "source": "web", "conv": conv, "when": ""}


def kde(tid, addr, body, date, label=None, multi=False):
    return {"thread": tid, "uid": tid * 10, "date": date, "type": "in", "read": True, "body": body, "addresses": [addr],
            "multi": multi, "attachments": 0, "sub": 0, "source": "kde", "label": label or addr}


class Merge(unittest.TestCase):
    def test_same_text_folds_and_takes_the_exact_time(self):
        w = web("Uber", "Looks like you may be delayed.", ms_of(2026, 9, 22, 12, 0), exact=False)
        k = kde(11, "Uber", "Looks like you may be delayed.", ms_of(2026, 9, 22, 16, 46))
        out = ms.merge([w], [k])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kde"], 11)
        self.assertEqual(out[0]["vs_kde"], "same")
        self.assertEqual(out[0]["date"], ms_of(2026, 9, 22, 16, 46))
        self.assertTrue(out[0]["exact"])
        self.assertEqual(ms.tag(out[0]), "[w1|11]")

    def test_messages_newer_stays_one_line_pointing_at_both(self):
        w = web("Apple", "Your Apple Account Code is: 461910.", ms_of(2026, 10, 10, 15, 36))
        k = kde(67, "Apple", "Your Apple Account Code is: 437414.", ms_of(2025, 8, 2, 21, 27))
        out = ms.merge([w], [k])
        self.assertEqual([m["thread"] for m in out], ["w1"])
        self.assertEqual(out[0]["vs_kde"], "web_newer")

    def test_kde_newer_keeps_its_own_line(self):
        w = web("Uber", "old", ms_of(2026, 9, 1, 12, 0), exact=False)
        k = kde(11, "Uber", "new", ms_of(2026, 9, 22, 16, 46))
        out = ms.merge([w], [k])
        self.assertEqual([m["thread"] for m in out], [11, "w1"])
        self.assertEqual(out[1]["vs_kde"], "kde_newer")

    def test_same_day_without_a_minute_counts_as_caught_up(self):
        w = web("06 16004546", "1 missed call(s)", ms_of(2026, 9, 29, 12, 0), exact=False)
        k = kde(106, "+31616004546", "1 missed call(s) from +31616004546. Last call at 18:21", ms_of(2026, 9, 29, 18, 18))
        out = ms.merge([w], [k])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["vs_kde"], "same")

    def test_number_formats_pair_up_and_the_tail_survives(self):
        w = web("06 16004546", "missed call", ms_of(2026, 9, 29, 18, 18))
        k1 = kde(106, "+31616004546", "missed call", ms_of(2026, 9, 29, 18, 18))
        k2 = kde(5, "Lebara", "top up", ms_of(2026, 6, 30, 19, 0))
        out = ms.merge([w], [k1, k2])
        self.assertEqual([ms.tag(m) for m in out], ["[w1|106]", "[5]"])

    def test_groups_are_never_folded(self):
        w = web("Артем Цацин", "hi", ms_of(2026, 9, 1, 12, 0))
        k = kde(45, "+18339913448", "hi", ms_of(2026, 9, 1, 12, 0), label="+18339913448, Артем Цацин", multi=True)
        self.assertEqual(len(ms.merge([w], [k])), 2)

    def test_no_web_rows_is_just_kde(self):
        k = kde(5, "Lebara", "top up", ms_of(2026, 6, 30, 19, 0))
        self.assertEqual(ms.merge([], [k]), [k])


class Handles(unittest.TestCase):
    def test_stable_across_runs(self):
        db = ms.cache()
        self.assertEqual(ms.web_handle(db, "CghA", "Apple"), 1)
        self.assertEqual(ms.web_handle(db, "CghB", "Uber"), 2)
        self.assertEqual(ms.web_handle(db, "CghA", "Apple Inc"), 1)  # a renamed conversation keeps its handle
        self.assertEqual(ms.web_conv_of(db, 1), ("CghA", "Apple Inc"))
        self.assertEqual(ms.web_conv_of(db, 9), (None, None))


class Stamps(unittest.TestCase):
    def test_exact_and_approximate(self):
        m = web("Apple", "x", ms_of(2026, 10, 10, 15, 36))
        self.assertEqual(ms.stamp(m), "2026-10-10 15:36")
        m = web("Apple", "x", ms_of(2026, 10, 10, 12, 40), exact=False)
        m["when"] = "3 hr"
        self.assertEqual(ms.stamp(m), "2026-10-10 ~12:40")
        m["when"] = "29 Sept"
        self.assertEqual(ms.stamp(m), "2026-10-10      ")


if __name__ == "__main__":
    unittest.main()
