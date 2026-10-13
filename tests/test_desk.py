"""mochi-desk: the capture commands build the right ffmpeg/import calls and keep an index; nothing real runs."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("mochi_desk", HERE.parent / "brain" / "mochi_desk.py")
md = importlib.util.module_from_spec(spec)
spec.loader.exec_module(md)

XRANDR = """Monitors: 2
 0: +*DP-0 2560/677x1080/290+1080+1080  DP-0
 1: +HDMI-0 1080/598x1920/336+0+240  HDMI-0
"""


class Desk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        md.DATA, md.MEDIA, md.INDEX, md.CONFIG = d, d / "media", d / "captures.json", d / "desk.json"
        self.calls = []

        def fake_run(cmd, timeout=60, env=None):
            self.calls.append(cmd)
            if cmd[0] == "xrandr":
                return 0, XRANDR, ""
            if cmd[0] == "pactl":
                return 0, "alsa_input.usb-mic\n", ""
            if cmd[0] == "mochi-phone":
                return 0, "[en] hello desk\n", ""
            for a in cmd:  # ffmpeg / import: produce the output file
                if a.startswith(str(d)):
                    Path(a).parent.mkdir(parents=True, exist_ok=True)
                    Path(a).write_bytes(b"x")
            return 0, "", ""
        self.p = mock.patch.object(md, "run", side_effect=fake_run)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def index(self):
        return json.loads(md.INDEX.read_text())

    def test_photo(self):
        with mock.patch.object(md, "camera", return_value="/dev/video0"), mock.patch.object(md.Path, "exists", return_value=True), \
             mock.patch("builtins.print"):
            md.photo([])
        self.assertEqual(self.calls[0][:4], ["ffmpeg", "-v", "error", "-y"])
        self.assertIn("/dev/video0", self.calls[0])
        self.assertEqual(self.index()[0]["kind"], "photo")

    def test_audio_is_transcribed(self):
        with mock.patch("builtins.print"):
            md.audio(["--seconds", "7"])
        ff = [c for c in self.calls if c[0] == "ffmpeg"][0]
        self.assertEqual(ff[ff.index("-t") + 1], "7")
        self.assertEqual(ff[ff.index("-i") + 1], "alsa_input.usb-mic")
        rec = self.index()[0]
        self.assertEqual((rec["kind"], rec["seconds"], rec["text"]), ("audio", 7, "[en] hello desk"))
        self.assertTrue(Path(rec["path"]).with_suffix(".txt").exists())
        self.assertIn("hello desk", md.media([]))

    def test_audio_is_capped(self):
        with mock.patch("builtins.print"):
            md.audio(["--seconds", "99999"])
        ff = [c for c in self.calls if c[0] == "ffmpeg"][0]
        self.assertEqual(ff[ff.index("-t") + 1], str(md.MAX_SECONDS))

    def test_screen_one_monitor_crops(self):
        with mock.patch("builtins.print"), mock.patch.object(md, "fit") as fit:
            md.screen(["--monitor", "1"])
        self.assertEqual([c for c in self.calls if c[0] == "import"][0][:3], ["import", "-window", "root"])
        self.assertEqual(fit.call_args[0][1], (0, 240, 1080, 2160))
        self.assertTrue(self.index()[0]["path"].endswith("-screen-1.png"))

    def test_screen_all_monitors(self):
        with mock.patch("builtins.print"), mock.patch.object(md, "fit") as fit:
            md.screen([])
        self.assertIsNone(fit.call_args[0][1])

    def test_screen_unknown_monitor(self):
        with self.assertRaises(SystemExit):
            md.screen(["--monitor", "7"])

    def test_monitors_parse(self):
        self.assertEqual([m["name"] for m in md.monitors()], ["DP-0", "HDMI-0"])
