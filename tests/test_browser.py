#!/usr/bin/env python3
"""Tests for mochi-browser's offline parts: config parsing, the nested-server state file, and stop()'s rules
(what it closes after a run and what it leaves). No Chrome, no X server."""

import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

src = Path(__file__).resolve().parent.parent / "brain" / "mochi_browser.py"
tmp = tempfile.mkdtemp()
os.environ["XDG_DATA_HOME"] = tmp
os.environ["XDG_CONFIG_HOME"] = tmp
spec = importlib.util.spec_from_loader("mochi_browser", importlib.machinery.SourceFileLoader("mochi_browser", str(src)))
mb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mb)


def config(**browser):
    mb.CONFIG.parent.mkdir(parents=True, exist_ok=True)
    mb.CONFIG.write_text(json.dumps({"browser": browser}))


class Config(unittest.TestCase):
    def test_defaults(self):
        config()
        self.assertEqual(mb.port(), 9333)
        self.assertEqual(mb.size(), (1280, 1000))
        self.assertEqual(mb.scale(), 1.0)

    def test_values_and_junk(self):
        config(port=9444, size="1440x900", scale=2, display=81)
        self.assertEqual((mb.port(), mb.size(), mb.scale()), (9444, (1440, 900), 2.0))
        self.assertEqual(mb.free_display(), ":81")
        config(port="x", size="wide", scale="big")
        self.assertEqual((mb.port(), mb.size(), mb.scale()), (9333, (1280, 1000), 1.0))

    def test_free_display_skips_taken(self):
        config()
        with mock.patch.object(mb.Path, "exists", lambda self: str(self) in ("/tmp/.X77-lock", "/tmp/.X11-unix/X78")):
            self.assertEqual(mb.free_display(), ":79")


class Nested(unittest.TestCase):
    def test_state_only_counts_a_live_xephyr(self):
        mb.STATE.parent.mkdir(parents=True, exist_ok=True)
        mb.STATE.write_text(json.dumps({"display": ":77", "xephyr": os.getpid()}))
        self.assertEqual(mb.nested(), {})  # alive, but this process isn't Xephyr
        mb.STATE.write_text(json.dumps({"display": ":77", "xephyr": 2 ** 22 + 12345}))
        self.assertEqual(mb.nested(), {})  # no such pid
        mb.STATE.write_text("not json")
        self.assertEqual(mb.nested(), {})
        mb.STATE.unlink()
        self.assertEqual(mb.window(), "")


class Stop(unittest.TestCase):
    """stop() after a run: kills a headless Chrome; kills a hidden nested one unless a chat has it; leaves a shown
    one and a plain window alone."""

    def run_stop(self, browser, nested, shown, chat):
        killed = []
        with mock.patch.object(mb, "running", lambda: {"Browser": browser, "User-Agent": ""}), \
             mock.patch.object(mb, "nested", lambda: nested), mock.patch.object(mb, "shown", lambda: shown), \
             mock.patch.object(mb, "chat_alive", lambda: chat), \
             mock.patch.object(mb, "kill_chrome", lambda: killed.append("chrome")), \
             mock.patch.object(mb, "stop_nested", lambda: killed.append("xephyr")), \
             mock.patch.object(mb.time, "sleep", lambda s: None):
            mb.stop()
        return killed

    def test_rules(self):
        self.assertEqual(self.run_stop("HeadlessChrome/1", {}, False, False), ["chrome"])
        self.assertEqual(self.run_stop("Chrome/1", {"display": ":77", "xephyr": 1}, False, False), ["chrome", "xephyr"])
        self.assertEqual(self.run_stop("Chrome/1", {"display": ":77", "xephyr": 1}, True, False), [])
        self.assertEqual(self.run_stop("Chrome/1", {"display": ":77", "xephyr": 1}, False, True), [])
        self.assertEqual(self.run_stop("Chrome/1", {}, True, False), [])

    def test_nothing_running(self):
        with mock.patch.object(mb, "running", lambda: None), \
             mock.patch.object(mb, "kill_chrome", lambda: self.fail("killed with nothing running")):
            mb.stop()


if __name__ == "__main__":
    unittest.main()
