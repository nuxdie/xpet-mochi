"""mochi-coach: the pieces around the Gemini session (geometry, the audio command, the pig's messages); no Gemini runs."""
import importlib.util
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("mochi_coach", HERE.parent / "brain" / "mochi_coach.py")
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)

XRANDR = """Monitors: 4
 0: +DP-2 1080/598x1920/336+0+240  DP-2
 1: +HDMI-1 2560/798x1080/334+1080+0  HDMI-1
 2: +DP-0 2560/677x1080/290+1080+1080  DP-0
 3: +HDMI-0 1080/523x1920/293+3640+240  HDMI-0
"""


class Geometry(unittest.TestCase):
    def test_monitors_in_xrandr_order(self):
        self.assertEqual(mc.monitors(XRANDR), [(0, 240, 1080, 1920), (1080, 0, 2560, 1080), (1080, 1080, 2560, 1080),
                                               (3640, 240, 1080, 1920)])

    def test_monitor_of_window_by_centre(self):
        mons = mc.monitors(XRANDR)
        self.assertEqual(mc.monitor_of((1660, 57, 942, 1010), mons), (1080, 0, 2560, 1080))
        self.assertEqual(mc.monitor_of((2000, 1500, 400, 300), mons), (1080, 1080, 2560, 1080))
        self.assertEqual(mc.monitor_of(None, mons), mons[0])  # no active window: the first
        self.assertIsNone(mc.monitor_of(None, []))

    def test_boxes_to_root(self):
        frame = (1080, 0, 2560, 1080)
        # Gemini's [ymin, xmin, ymax, xmax] in thousandths of the frame -> [x, y, w, h] in root pixels
        self.assertEqual(mc.boxes_to_root([[141, 32, 219, 481]], frame), [[1162, 152, 1149, 84]])
        # junk is dropped: a flat list, a degenerate box, values off the scale, the whole screen
        self.assertEqual(mc.boxes_to_root([620, 50, 720, 950], frame), [])
        self.assertEqual(mc.boxes_to_root([[500, 500, 500, 600], [0, 0, 1000, 1000], [0, 0, 900, 800]], frame), [])
        self.assertEqual(mc.boxes_to_root([[-10, 0, 100, 1200]], frame), [[1080, 0, 2560, 108]])
        self.assertEqual(mc.boxes_to_root(None, frame), [])
        self.assertEqual(len(mc.boxes_to_root([[0, 0, 10, 10]] * 9, frame)), 4)  # at most four


class Audio(unittest.TestCase):
    def test_mix_command_two_sources(self):
        cmd = mc.ffmpeg_mix_cmd("mic", "sink.monitor")
        self.assertEqual(cmd[:2], ["ffmpeg", "-v"])
        self.assertEqual(cmd.count("pulse"), 2)
        self.assertIn("amix=inputs=2", " ".join(cmd))
        self.assertIn("aresample=16000", " ".join(cmd))
        self.assertEqual(cmd[-3:], ["-f", "s16le", "-"])

    def test_mix_command_one_source(self):
        cmd = mc.ffmpeg_mix_cmd(None, "sink.monitor")
        self.assertEqual(cmd.count("pulse"), 1)
        self.assertNotIn("amix", " ".join(cmd))
        self.assertIn("-af", cmd)

    def test_no_source_refused(self):
        with self.assertRaises(SystemExit):
            mc.ffmpeg_mix_cmd(None, None)

    def test_rms_and_chunk(self):
        self.assertEqual(mc.CHUNK, 1280)  # 40 ms of 16 kHz 16-bit mono
        self.assertEqual(mc.rms(b"\x00" * mc.CHUNK), 0.0)
        self.assertGreater(mc.rms(b"\x00\x40" * (mc.CHUNK // 2)), 16000)


class PigMessages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        mc.PET_SOCK = Path(self.tmp.name) / "xpet.sock"
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(str(mc.PET_SOCK))
        self.sock.settimeout(1)

    def tearDown(self):
        self.sock.close()
        self.tmp.cleanup()

    def recv(self):
        return json.loads(self.sock.recv(8192).decode())

    def test_show_and_clear(self):
        self.assertTrue(mc.overlay_show("the q3 figure is 42k", [[1162, 152, 1149, 84]], 9))
        m = self.recv()
        self.assertEqual(m["event"], "coach")
        self.assertEqual(m["text"], "the q3 figure is 42k")
        self.assertEqual(m["boxes"], [[1162, 152, 1149, 84]])  # nested: the pig flattens it to boxes.0.0 ...
        self.assertEqual(m["secs"], 9)
        self.assertTrue(mc.overlay_clear())
        self.assertEqual(self.recv(), {"event": "coach", "clear": "1"})

    def test_no_pig(self):
        self.sock.close()
        os.unlink(mc.PET_SOCK)
        self.assertFalse(mc.overlay_show("hi"))


class Knowledge(unittest.TestCase):
    def test_context_pack_levels(self):
        with tempfile.TemporaryDirectory() as d:
            mc.MEMORY = Path(d)
            (Path(d) / "life.md").write_text("# life\nprojects: the pig")
            (Path(d) / "open-loops.md").write_text("- VAT return by 31 Oct")
            (Path(d) / "dossier.md").write_text("x" * 100)
            with mock.patch.object(mc, "run", return_value=(0, "Tue 27 Oct 18:00 AI Tinkerers", "")):
                full = mc.context_pack("full", "Artem")
                light = mc.context_pack("light", "Artem")
            self.assertIn("memory/dossier.md", full)
            self.assertNotIn("memory/dossier.md", light)
            self.assertIn("VAT return", light)
            self.assertIn("AI Tinkerers", light)  # the agenda rides along
            self.assertIn("private", full)
            self.assertEqual(mc.context_pack("none", "Artem"), "")

    def test_context_pack_caps_long_files(self):
        with tempfile.TemporaryDirectory() as d:
            mc.MEMORY = Path(d)
            (Path(d) / "life.md").write_text("y" * (mc.CONTEXT_CAP + 500))
            with mock.patch.object(mc, "run", return_value=(1, "", "")):
                pack = mc.context_pack("light", "Artem")
            self.assertIn("[... cut]", pack)
            self.assertLess(len(pack), mc.CONTEXT_CAP + 400)

    def test_lookup_commands(self):
        calls = []

        def fake_run(cmd, timeout=20):
            calls.append(cmd)
            return 0, "found: " + " ".join(cmd), ""
        with mock.patch.object(mc, "run", side_effect=fake_run):
            self.assertIn("found", mc.run_lookup("telegram", "nastya contract"))
            self.assertEqual(calls[-1][:3], ["mochi-sense", "telegram", "search"])
            mc.run_lookup("mail", "from:nastya subject:contract")
            self.assertEqual(calls[-1], ["mochi-sense", "mail", "search", "from:nastya subject:contract"])
            mc.run_lookup("contacts", "Denis")
            self.assertEqual(calls[-1][:2], ["mochi-contacts", "show"])
            mc.run_lookup("agenda", "")
            self.assertEqual(calls[-1][:2], ["mochi-calendar", "agenda"])
            self.assertEqual(mc.run_lookup("drive", "   "), "empty query")
            self.assertIn("unknown kind", mc.run_lookup("nas", "x"))
        with mock.patch.object(mc, "run", return_value=(0, "z" * 9000, "")):
            self.assertEqual(len(mc.run_lookup("tasks", "")), mc.LOOKUP_CAP)
        with mock.patch.object(mc, "run", return_value=(1, "", "no such sense")):
            self.assertIn("lookup failed", mc.run_lookup("drive", "contract"))

    def test_brain_send(self):
        with tempfile.TemporaryDirectory() as d:
            mc.BRAIN_SOCK = Path(d) / "mochi-brain.sock"
            self.assertFalse(mc.brain_send({"event": "coach_ask"}))
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            srv.bind(str(mc.BRAIN_SOCK))
            srv.settimeout(1)
            self.assertTrue(mc.brain_send({"event": "coach_ask", "id": "coach-1", "text": "when is the VAT due?",
                                           "reply_to": "/run/x/mochi-coach.sock"}))
            m = json.loads(srv.recv(8192).decode())
            self.assertEqual((m["event"], m["id"], m["reply_to"]), ("coach_ask", "coach-1", "/run/x/mochi-coach.sock"))
            srv.close()


class MochiEngine(unittest.TestCase):
    def test_parse_reply_finds_the_object(self):
        self.assertEqual(mc.parse_reply('{"note": "vat is due 31 oct", "box": null, "remember": null}'),
                         {"note": "vat is due 31 oct", "box": None, "remember": None})
        wrapped = 'Sure.\n```json\n{"note": null, "box": [10, 20, 300, 40], "remember": "invoice tomorrow"}\n```'
        self.assertEqual(mc.parse_reply(wrapped), {"note": None, "box": [10, 20, 300, 40], "remember": "invoice tomorrow"})
        # a mochi block tacked on the end is ignored; prose with no object means nothing
        tail = '{"note": "x", "box": null, "remember": null}\n```json\n{"say": null, "asks": []}\n```'
        self.assertEqual(mc.parse_reply(tail)["note"], "x")
        self.assertEqual(mc.parse_reply("I have nothing to add."), {"note": None, "box": None, "remember": None})
        self.assertEqual(mc.parse_reply(""), {"note": None, "box": None, "remember": None})

    def test_pixel_box_to_root(self):
        frame, picture = (1080, 0, 2560, 1080), (1280, 540)  # the picture is the frame at half size
        self.assertEqual(mc.pixel_box_to_root([100, 50, 200, 20], frame, picture), [1280, 100, 400, 40])
        self.assertIsNone(mc.pixel_box_to_root([0, 0, 1280, 540], frame, picture))  # the whole screen
        self.assertIsNone(mc.pixel_box_to_root([10, 10, 0, 5], frame, picture))
        self.assertIsNone(mc.pixel_box_to_root("nope", frame, picture))
        self.assertIsNone(mc.pixel_box_to_root(None, frame, picture))
        # clipped to the picture
        self.assertEqual(mc.pixel_box_to_root([1200, 500, 300, 100], frame, picture), [3480, 1000, 160, 80])

    def test_gate_opens_and_closes(self):
        g = mc.Gate(500)
        quiet, loud = b"\x00" * mc.CHUNK, b"\x00\x20" * (mc.CHUNK // 2)
        self.assertIsNone(g.feed(quiet))
        self.assertIsNone(g.feed(quiet))
        kind, pre = g.feed(loud)
        self.assertEqual(kind, "start")
        self.assertEqual(len(pre), 3)  # two chunks of pre-roll and the loud one
        self.assertEqual(g.feed(loud), ("audio", loud))
        for _ in range(mc.HANGOVER - 1):
            self.assertEqual(g.feed(quiet), ("audio", quiet))
        self.assertEqual(g.feed(quiet), ("end", quiet))
        self.assertIsNone(g.feed(quiet))

    def test_chunk_text(self):
        with mock.patch.object(mc, "monitors", return_value=[(0, 0, 1920, 1080)]), \
                mock.patch.object(mc, "active_window_rect", return_value=None), \
                mock.patch.object(mc, "assembly_key", return_value=""), \
                mock.patch.object(mc, "cfg", return_value={}):
            with tempfile.TemporaryDirectory() as d:
                mc.LOGS = Path(d)
                args = mock.Mock(monitor=None, every=None, stt=None, model=None, brief="", debug=False, transcript=False)
                coach = mc.MochiCoach(args)
        text = coach.chunk_text([("00:10", "them", "when is it due?"), ("00:12", "me", "friday")], "\n(screen now: f.jpg, 10x10 px)")
        self.assertEqual(text, "[call 00:10-00:12]\nthem: when is it due?\nme: friday\n(screen now: f.jpg, 10x10 px)")
        self.assertEqual(coach.stt_name, "gemini")

    def test_source_command(self):
        cmd = mc.ffmpeg_source_cmd("mic")
        self.assertEqual(cmd[:2], ["ffmpeg", "-v"])
        self.assertIn("aresample=16000", " ".join(cmd))
        self.assertEqual(cmd[-3:], ["-f", "s16le", "-"])


class Key(unittest.TestCase):
    def test_key_from_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            mc.GEMINI_ENV = Path(d) / "gemini.env"
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("GEMINI_API_KEY", None)
                self.assertEqual(mc.load_key(), "")
                mc.GEMINI_ENV.write_text("# the key\nGEMINI_API_KEY=\"abc\"\n")
                self.assertEqual(mc.load_key(), "abc")
                os.environ["GEMINI_API_KEY"] = "env-wins"
                self.assertEqual(mc.load_key(), "env-wins")


if __name__ == "__main__":
    unittest.main()
