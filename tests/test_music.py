#!/usr/bin/env python3
"""Tests for mochi-music's offline parts: folder names, cue sheets, the conversion plan and the arrival check.
No ffmpeg run, no Deluge. Run: python3 -m unittest discover -s tests   (or `make test`)."""

import importlib.machinery
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="mochi-test-")
os.environ["XDG_DATA_HOME"] = TMP
os.environ["XDG_CONFIG_HOME"] = TMP

src = Path(__file__).resolve().parent.parent / "brain" / "mochi_music.py"
spec = importlib.util.spec_from_loader("mochi_music", importlib.machinery.SourceFileLoader("mochi_music", str(src)))
mm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mm)

CUE = """REM GENRE Jazz
REM DATE 1970
PERFORMER "Miles Davis"
TITLE "Bitches Brew"
FILE "Bitches Brew.wav" WAVE
  TRACK 01 AUDIO
    TITLE "Pharaoh's Dance"
    PERFORMER "Miles Davis"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Bitches Brew"
    INDEX 00 20:04:50
    INDEX 01 20:05:00
"""


class Names(unittest.TestCase):
    def test_clean_name(self):
        self.assertEqual(mm.clean_name("Charli XCX - 2024 - BRAT (FLAC)"), "Charli XCX - 2024 - BRAT")
        self.assertEqual(mm.clean_name("Depeche Mode - 1990 - Violator [Deluxe] (16bit-44.1kHz)"),
                         "Depeche Mode - 1990 - Violator [Deluxe]")
        self.assertEqual(mm.clean_name("Swans - To Be Kind {FLAC 24bit WEB}"), "Swans - To Be Kind")
        self.assertEqual(mm.clean_name("Kate Bush - Hounds Of Love 1985(2018) [24-44.1]"), "Kate Bush - Hounds Of Love 1985(2018)")
        self.assertEqual(mm.clean_name("LINGUA IGNOTA - SINNER GET READY (2021) [FLAC]"), "LINGUA IGNOTA - SINNER GET READY (2021)")
        self.assertEqual(mm.clean_name("Miles Davis - Bitches Brew - 1970 (1999 Columbia.Legacy C2k 65774) [CD FLAC]"),
                         "Miles Davis - Bitches Brew - 1970 (1999 Columbia.Legacy C2k 65774)")
        self.assertEqual(mm.clean_name("2001 - Vespertine"), "2001 - Vespertine")

    def test_safe(self):
        self.assertEqual(mm.safe('What/Is: "This"?'), 'What-Is- -This-')


class Cue(unittest.TestCase):
    def test_parse(self):
        p = Path(TMP) / "a.cue"
        p.write_text(CUE)
        tracks, album = mm.parse_cue(p)
        self.assertEqual(album, {"genre": "Jazz", "date": "1970", "albumartist": "Miles Davis", "album": "Bitches Brew"})
        self.assertEqual([t["n"] for t in tracks], [1, 2])
        self.assertEqual(tracks[0]["file"], "Bitches Brew.wav")
        self.assertEqual(tracks[0]["start"], 0)
        self.assertAlmostEqual(tracks[1]["start"], 20 * 60 + 5)  # INDEX 01, not INDEX 00
        self.assertEqual(tracks[1]["title"], "Bitches Brew")

    def test_cp1251(self):
        p = Path(TMP) / "b.cue"
        p.write_bytes('TITLE "Альбом"\nFILE "x.flac" WAVE\n  TRACK 01 AUDIO\n    TITLE "Песня"\n    INDEX 01 00:00:00\n'.encode("cp1251"))
        tracks, album = mm.parse_cue(p)
        self.assertEqual(album["album"], "Альбом")
        self.assertEqual(tracks[0]["title"], "Песня")


NONCOMPLIANT = """PERFORMER "Bob Dylan"
TITLE "Highway 61 Revisited"
FILE "01 - Like a Rolling Stone.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Like a Rolling Stone"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Tombstone Blues"
    INDEX 00 06:08:70
FILE "02 - Tombstone Blues.flac" WAVE
    INDEX 01 00:00:00
"""


class NoncompliantCue(unittest.TestCase):
    def test_file_between_index_00_and_01_belongs_to_the_track(self):
        p = Path(TMP) / "nc.cue"
        p.write_text(NONCOMPLIANT)
        tracks, _ = mm.parse_cue(p)
        self.assertEqual([(t["n"], t["file"], t["start"]) for t in tracks],
                         [(1, "01 - Like a Rolling Stone.flac", 0), (2, "02 - Tombstone Blues.flac", 0)])

    def test_tracks_plus_cue_is_not_split(self):
        src = Path(TMP) / "src" / "Dylan"
        src.mkdir(parents=True, exist_ok=True)
        for n in ("01 - Like a Rolling Stone.flac", "02 - Tombstone Blues.flac"):
            (src / n).write_bytes(b"x")
        (src / "album.cue").write_text(NONCOMPLIANT)
        jobs, _ = mm.plan(src, Path(TMP) / "Music" / "Dylan")
        self.assertEqual(sorted(d.name for _c, d in jobs), ["01 - Like a Rolling Stone.mp3", "02 - Tombstone Blues.mp3"])
        self.assertTrue(all("-ss" not in c for c, _d in jobs))


class FolderName(unittest.TestCase):
    def test_from_tags_else_clean_name(self):
        src = Path(TMP) / "src" / "Kate Bush - Hounds Of Love 1985(2018) [24-44.1]"
        src.mkdir(parents=True, exist_ok=True)
        (src / "01.flac").write_bytes(b"x")
        mm.tags_of = lambda p: ("Kate Bush", "", "Hounds of Love", "1985")
        self.assertEqual(mm.folder_name(src), "Kate Bush - Hounds of Love (1985)")
        mm.tags_of = lambda p: ("Kate Bush", "Various Artists", "Hounds of Love", "")
        self.assertEqual(mm.folder_name(src), "Hounds of Love")
        mm.tags_of = lambda p: ("", "", "", "")
        self.assertEqual(mm.folder_name(src), "Kate Bush - Hounds Of Love 1985(2018)")
        mm.tags_of = lambda p: ("AC/DC", "", "Back in Black", "1980")
        self.assertEqual(mm.folder_name(src), "AC-DC - Back in Black (1980)")


class Plan(unittest.TestCase):
    def setUp(self):
        mm.tags_of = lambda p: ("Artist", "", "Album", "")
        self.src = Path(TMP) / "src" / "Artist - Album (FLAC)"
        (self.src / "CD2").mkdir(parents=True, exist_ok=True)
        for n in ("01 One.flac", "02 Two.flac", "cover.jpg", "rip.log"):
            (self.src / n).write_bytes(b"x")
        (self.src / "CD2" / "Album CD2.flac").write_bytes(b"x")
        (self.src / "CD2" / "Album CD2.cue").write_text(CUE.replace("Bitches Brew.wav", "Album CD2.wav"))
        mm.duration = lambda p: 3000.0
        self.out = Path(TMP) / "Music" / "Artist - Album"  # from the (faked) tags

    def test_tracks_images_and_cue_split(self):
        jobs, copies = mm.plan(self.src, self.out)
        dsts = sorted(str(d.relative_to(self.out)) for _c, d in jobs)
        self.assertEqual(dsts, ["01 One.mp3", "02 Two.mp3", "CD2/01 Pharaoh's Dance.mp3", "CD2/02 Bitches Brew.mp3"])
        self.assertEqual([(f.name, str(t.relative_to(self.out))) for f, t in copies], [("cover.jpg", "cover.jpg")])
        split = [c for c, d in jobs if d.name == "02 Bitches Brew.mp3"][0]
        self.assertIn("-ss", split)
        self.assertEqual(split[split.index("-ss") + 1], f"{20 * 60 + 5:.3f}")
        self.assertEqual(split[split.index("-to") + 1], "3000.000")
        self.assertIn("track=2/2", split)
        self.assertIn("album=Bitches Brew", split)
        plain = [c for c, d in jobs if d.name == "01 One.mp3"][0]
        self.assertNotIn("-ss", plain)
        self.assertEqual(plain[plain.index("-b:a") + 1], "320k")
        self.assertEqual(plain[plain.index("-codec:a") + 1], "libmp3lame")

    def test_convert_dry_run_and_no_audio(self):
        out, n, bad, note = mm.convert(self.src, str(Path(TMP) / "Music"), dry_run=True)
        self.assertEqual(out, self.out)
        self.assertEqual((n, bad, note), (4, 0, "dry run"))
        empty = Path(TMP) / "src" / "Nothing"
        empty.mkdir()
        (empty / "readme.txt").write_text("hi")
        self.assertEqual(mm.convert(empty, str(Path(TMP) / "Music"))[3], "no lossless audio in it")


class Arrival(unittest.TestCase):
    def setUp(self):
        conf = Path(TMP) / "mochi"
        conf.mkdir(exist_ok=True)
        (conf / "sources.json").write_text('{"music": {"nas_root": "%s/nas"}}' % TMP)
        self.dl = Path(TMP) / "nas" / "Downloads" / "Album"
        self.dl.mkdir(parents=True, exist_ok=True)
        (self.dl / "a.flac").write_bytes(b"x" * 1000)

    def test_states(self):
        item = {"name": "Album"}
        self.assertEqual(mm.arrived(item, None), (False, "gone from Deluge"))
        self.assertEqual(mm.arrived(item, {"name": "Album", "progress": 40, "is_finished": False})[1], "downloading, 40%")
        self.assertEqual(mm.arrived({"name": "Other"}, {"name": "Other", "is_finished": True, "total_size": 10}),
                         (False, "finished; not on the NAS yet"))
        ok, why = mm.arrived(item, {"name": "Album", "is_finished": True, "total_size": 2000})
        self.assertFalse(ok)
        self.assertIn("arriving", why)
        self.assertEqual(mm.arrived(item, {"name": "Album", "is_finished": True, "total_size": 1000}), (True, ""))


if __name__ == "__main__":
    unittest.main()
