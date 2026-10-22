#!/usr/bin/env python3
"""mochi-music — lossless albums from the seedbox turned into MP3 320 in their Music folder on the NAS.

    mochi-music queue TORRENT [--into DIR]   put a Deluge torrent (id prefix or part of its name) on the conversion list
    mochi-music sync [--dry-run]             convert what has finished downloading and has fully arrived on the NAS
                                             (mochi-music.timer runs this every 15 minutes)
    mochi-music convert SRC [--into DIR] [--dry-run]   convert one folder now (SRC under the NAS root, or absolute)
    mochi-music status                       the list: waiting, converting, done, failed
    mochi-music log [N]

The seedbox mirrors its finished downloads into Downloads/ on the NAS by itself; the NAS home share is mounted on this
machine (`music.nas_root` in ~/.config/mochi/sources.json, default ~/share). `sync` takes each queued torrent, asks
Deluge whether it has finished, checks the folder on the NAS is there and as big as the torrent says, then converts it:
every FLAC / APE / ALAC / WavPack / WAV file to MP3 320 kbps CBR with its tags (ffmpeg, libmp3lame), a single image
with a .cue sheet split into tracks, cover pictures copied along, the folder structure kept, into
Music/Artist - Album (Year) as their library is named (from the tags; the release folder's name minus its format
tags when the tags don't say; `music.music`, default Music). The lossless source stays where it is
(it keeps seeding). Nothing is deleted, ever. Each conversion gets a line in music.log and, when something finished,
one `mochi-brain --trigger` so Mochi can tell them.
"""

import argparse
import concurrent.futures
import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROG = "mochi-music"
HOME = Path.home()
WORK = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "mochi"
LOG = WORK / "music.log"
QUEUE = WORK / "music-queue.json"
CONF_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "mochi"
CONFIG = CONF_DIR / "sources.json"
LOSSLESS = {".flac", ".ape", ".wv", ".wav", ".aiff", ".aif", ".m4a", ".alac", ".tta", ".tak"}
PICTURES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
JOBS = 3                     # ffmpeg processes at once
COMPLETE_RATIO = 0.985       # the NAS folder's size vs the torrent's: the mirror is still copying below this
# Format tags release groups hang on folder names; the Music folder gets the plain name.
TAGS = re.compile(r"\s*[\[\({]\s*(flac|ape|alac|wavpack|wv|lossless|cd ?flac|web flac|mqa(-cd)?|24[ -]?bit|16[ -]?bit|"
                  r"\d{2}bit[- ]\d+(\.\d+)?k?hz|\d{2}[-/]\d{2,3}(\.\d)?|hi-?res|web|cd|image\+?\.?cue|tracks\+?\.?cue|"
                  r"tracks|vinyl rip|rip|remaster(ed)?|fla|\s|,|\+|&)*\s*[\]\)}]", re.I)


def die(msg, code=1):
    print(f"{PROG}: {msg}", file=sys.stderr)
    sys.exit(code)


def conf():
    try:
        return json.loads(CONFIG.read_text()).get("music", {}) or {}
    except (OSError, ValueError):
        return {}


def nas_root():
    return Path(os.path.expanduser(conf().get("nas_root") or "~/share"))


def downloads_dir():
    return nas_root() / (conf().get("downloads") or "Downloads")


def music_dir():
    return nas_root() / (conf().get("music") or "Music")


def log(text):
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M} {text}\n")
    except OSError:
        pass


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" or n >= 100 else f"{n:.1f} {unit}"
        n /= 1024


def _sibling(name, installed):
    here = Path(__file__).resolve().parent
    path = next((here / n for n in (name + ".py", installed) if (here / n).exists()), None)
    if not path:
        die(f"{installed} is not installed beside {here}")
    spec = importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, str(path)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def clean_name(name):
    """The folder name without the format tags: 'Charli XCX - 2024 - BRAT (FLAC)' → 'Charli XCX - 2024 - BRAT'."""
    out = name
    for _ in range(4):
        new = TAGS.sub("", out).strip(" -_.")
        if new == out:
            break
        out = new
    return out or name


def tags_of(path):
    """artist, album artist, album, year of one audio file, from its tags (ffprobe); empty strings when missing."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(path)],
                             capture_output=True, text=True, timeout=60).stdout
        t = {k.lower(): str(v).strip() for k, v in (json.loads(out or "{}").get("format") or {}).get("tags", {}).items()}
    except (OSError, ValueError, subprocess.SubprocessError):
        t = {}
    year = re.search(r"\d{4}", t.get("originaldate") or t.get("original_year") or t.get("date") or t.get("year") or "")
    return t.get("artist", ""), t.get("album_artist") or t.get("albumartist") or "", t.get("album", ""), year.group(0) if year else ""


def folder_name(src):
    """The Music folder's name, the way their library is named: 'Artist - Album (Year)' from the tags of the first
    audio file; the release folder's name minus its format tags when the tags don't say."""
    first = None
    for root, _dirs, files in os.walk(src):
        audio = sorted(f for f in files if Path(f).suffix.lower() in LOSSLESS)
        if audio:
            first = Path(root) / audio[0]
            break
    if first is None:
        return clean_name(src.name)
    artist, album_artist, album, year = tags_of(first)
    who = album_artist or artist
    if re.search(r"various|^va$|сборник", who, re.I):
        who = ""
    if not album:
        return clean_name(src.name)
    name = (f"{who} - {album}" if who else album) + (f" ({year})" if year else "")
    return safe(name)


# ---- the queue -----------------------------------------------------------------------------------------

def load_queue():
    try:
        return json.loads(QUEUE.read_text())
    except (OSError, ValueError):
        return []


def save_queue(q):
    WORK.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE.with_suffix(".tmp")
    tmp.write_text(json.dumps(q, ensure_ascii=False, indent=1))
    tmp.replace(QUEUE)


def deluge():
    mt = _sibling("mochi_torrent", "mochi-torrent")
    try:
        return mt, mt.Deluge().login()
    except mt.DelugeError as e:
        die(str(e))


def torrents(d):
    return d.rpc("core.get_torrents_status", {}, ["name", "state", "progress", "total_size", "is_finished", "save_path",
                                                  "time_added"]) or {}


def queue(what, into=None):
    mt, d = deluge()
    ts = torrents(d)
    hits = {h: t for h, t in ts.items() if h.startswith(what.lower()) or what.lower() in t["name"].lower()}
    if len(hits) != 1:
        die(f"{len(hits)} torrents match {what!r}" + ("; be more specific" if hits else ""))
    h, t = next(iter(hits.items()))
    q = load_queue()
    for item in q:
        if item["id"] == h:
            print(f"already on the list ({item['state']}): {t['name']}")
            return
    q.append({"id": h, "name": t["name"], "into": into or "", "state": "waiting", "queued": time.time()})
    save_queue(q)
    log(f"queued {t['name']} ({human(t['total_size'])})")
    print(f"on the list: {t['name']} ({human(t['total_size'])}); converted into {into or music_dir()} once it has arrived")


def folder_size(p):
    n = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                n += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return n


def arrived(item, t):
    """(ready, why): the torrent finished in Deluge and its folder on the NAS is complete."""
    if not t:
        return False, "gone from Deluge"
    if not (t.get("is_finished") or (t.get("progress") or 0) >= 100):
        return False, f"downloading, {t.get('progress') or 0:.0f}%"
    src = downloads_dir() / t["name"]
    if not src.exists():
        return False, "finished; not on the NAS yet"
    if src.is_file():
        return True, ""
    have, want = folder_size(src), int(t.get("total_size") or 0)
    if want and have < want * COMPLETE_RATIO:
        return False, f"arriving on the NAS: {human(have)} of {human(want)}"
    return True, ""


# ---- conversion -----------------------------------------------------------------------------------------

def parse_cue(path):
    """Tracks of a cue sheet: [{file, n, title, performer, start}] with start in seconds (INDEX 01)."""
    try:
        text = path.read_bytes()
    except OSError:
        return [], {}
    for enc in ("utf-8-sig", "cp1251", "latin-1"):
        try:
            text = text.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return [], {}
    tracks, album, cur, file = [], {}, None, None
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r'FILE\s+"?(.*?)"?\s+\w+$', line)
        if m:
            file = m.group(1)
            if cur is not None and cur["start"] is None:
                cur["file"] = file  # EAC's "noncompliant" layout: the track's FILE comes between its INDEX 00 and 01
            continue
        m = re.match(r"TRACK\s+(\d+)\s+AUDIO", line, re.I)
        if m:
            cur = {"file": file, "n": int(m.group(1)), "title": "", "performer": "", "start": None}
            tracks.append(cur)
            continue
        m = re.match(r'(TITLE|PERFORMER|REM DATE|REM GENRE)\s+"?(.*?)"?$', line, re.I)
        if m:
            key, val = m.group(1).upper(), m.group(2)
            if cur is None:
                album[{"TITLE": "album", "PERFORMER": "albumartist", "REM DATE": "date", "REM GENRE": "genre"}[key]] = val
            elif key == "TITLE":
                cur["title"] = val
            elif key == "PERFORMER":
                cur["performer"] = val
            continue
        m = re.match(r"INDEX\s+01\s+(\d+):(\d\d):(\d\d)", line)
        if m and cur is not None:
            mm, ss, ff = map(int, m.groups())
            cur["start"] = mm * 60 + ss + ff / 75.0
    return [t for t in tracks if t["start"] is not None], album


def duration(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=120).stdout.strip()
        return float(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def safe(name):
    return re.sub(r'[\\/:*?"<>|]+', "-", name).strip() or "track"


def ffmpeg_cmd(src, dst, start=None, end=None, meta=None):
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    if end is not None:
        cmd += ["-to", f"{end:.3f}"]
    cmd += ["-i", str(src), "-map", "0:a:0", "-map_metadata", "0", "-id3v2_version", "3",
            "-codec:a", "libmp3lame", "-b:a", "320k"]
    for k, v in (meta or {}).items():
        if v:
            cmd += ["-metadata", f"{k}={v}"]
    return cmd + [str(dst)]


def plan(src, out):
    """What to run: [(cmd, dst)] for the audio, and [(from, to)] copies for pictures. Cue images become tracks."""
    jobs, copies = [], []
    for root, _dirs, files in os.walk(src):
        root = Path(root)
        rel = root.relative_to(src)
        audio = [f for f in files if Path(f).suffix.lower() in LOSSLESS]
        cues = [f for f in files if f.lower().endswith(".cue")]
        split = set()
        for cue in cues:
            tracks, album = parse_cue(root / cue)
            if len(audio) >= len(tracks) > 0:
                continue  # one file per track already: the cue is a playlist, not something to split
            by_file = {}
            for t in tracks:
                by_file.setdefault(t["file"], []).append(t)
            for fname, ts in by_file.items():
                f = root / fname
                if not f.exists():  # the cue names a .wav the group converted to .flac, or the like
                    alts = [a for a in audio if Path(a).stem == Path(fname).stem]
                    if not alts:
                        continue
                    f = root / alts[0]
                if len(ts) < 2:
                    continue
                total = duration(f)
                split.add(f.name)
                for i, t in enumerate(ts):
                    end = ts[i + 1]["start"] if i + 1 < len(ts) else total
                    dst = out / rel / f"{t['n']:02d} {safe(t['title'] or f'Track {t['n']}')}.mp3"
                    meta = {"title": t["title"], "artist": t["performer"] or album.get("albumartist", ""),
                            "album": album.get("album", ""), "album_artist": album.get("albumartist", ""),
                            "date": album.get("date", ""), "genre": album.get("genre", ""),
                            "track": f"{t['n']}/{len(ts)}"}
                    jobs.append((ffmpeg_cmd(f, dst, t["start"], end, meta), dst))
        for a in audio:
            if a in split:
                continue
            dst = out / rel / (Path(a).stem + ".mp3")
            jobs.append((ffmpeg_cmd(root / a, dst), dst))
        for f in files:
            if Path(f).suffix.lower() in PICTURES:
                copies.append((root / f, out / rel / f))
    return jobs, copies


def convert(src, into=None, dry_run=False):
    src = Path(src)
    if not src.is_absolute():
        src = nas_root() / src
    if not src.exists():
        die(f"no such folder: {src}")
    into = Path(os.path.expanduser(into)) if into else music_dir()
    if not into.is_absolute():
        into = nas_root() / into
    out = into / folder_name(src) if src.is_dir() else into / clean_name(src.stem)
    if src.is_file():
        jobs, copies = [(ffmpeg_cmd(src, out / (src.stem + ".mp3")), out / (src.stem + ".mp3"))], []
    else:
        jobs, copies = plan(src, out)
    todo = [(c, d) for c, d in jobs if not d.exists()]
    if not jobs:
        return out, 0, 0, "no lossless audio in it"
    if dry_run:
        print(f"{src.name}\n  → {out}\n  {len(jobs)} tracks ({len(jobs) - len(todo)} already there), {len(copies)} pictures")
        for c, d in todo[:5]:
            print(f"    {d.relative_to(out)}")
        if len(todo) > 5:
            print(f"    ... {len(todo) - 5} more")
        return out, len(jobs), 0, "dry run"
    out.mkdir(parents=True, exist_ok=True)
    failed = []

    def run(job):
        cmd, dst = job
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".part.mp3")
        cmd = cmd[:-1] + [str(tmp)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                raise OSError(r.stderr.strip()[-300:] or f"ffmpeg exit {r.returncode}")
            tmp.replace(dst)
            return None
        except (OSError, subprocess.SubprocessError) as e:
            try:
                tmp.unlink()
            except OSError:
                pass
            return (dst, str(e))

    with concurrent.futures.ThreadPoolExecutor(max_workers=JOBS) as ex:
        for res in ex.map(run, todo):
            if res:
                failed.append(res)
    for f, t in copies:
        if not t.exists():
            try:
                t.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, t)
            except OSError:
                pass
    for dst, err in failed:
        log(f"failed {dst.relative_to(out)}: {err[:200]}")
    return out, len(jobs), len(failed), ""


def sync(dry_run=False):
    q = load_queue()
    if not any(i["state"] in ("waiting", "failed") for i in q):
        print("nothing waiting")
        return
    mt, d = deluge()
    ts = torrents(d)
    done_now = []
    for item in q:
        if item["state"] not in ("waiting", "failed"):
            continue
        ready, why = arrived(item, ts.get(item["id"]))
        if not ready:
            item["why"] = why
            print(f"  waiting: {item['name']}: {why}")
            continue
        src = downloads_dir() / item["name"]
        if dry_run:
            convert(src, item.get("into") or None, dry_run=True)
            continue
        item["state"], item["why"] = "converting", ""
        save_queue(q)
        t0 = time.time()
        try:
            out, n, bad, note = convert(src, item.get("into") or None)
        except SystemExit as e:
            item["state"], item["why"] = "failed", str(e)
            log(f"failed {item['name']}: {e}")
            save_queue(q)
            continue
        if note == "no lossless audio in it":
            item["state"], item["why"] = "done", note
            log(f"skipped {item['name']}: {note}")
        elif bad:
            item["state"], item["why"] = "failed", f"{bad} of {n} tracks failed"
            log(f"converted {item['name']} → {out}: {bad} of {n} tracks failed ({time.time() - t0:.0f}s)")
        else:
            item["state"], item["out"], item["done"] = "done", str(out), time.time()
            log(f"converted {item['name']} → {out}: {n} tracks, MP3 320 ({time.time() - t0:.0f}s)")
            done_now.append(out.name)
        save_queue(q)
        print(f"  {item['state']}: {item['name']} → {out}" + (f" ({item['why']})" if item.get("why") else ""))
    save_queue(q)
    if done_now:
        left = sum(1 for i in q if i["state"] == "waiting")
        text = (f"mochi-music: converted to MP3 320 in {music_dir().name}/: " + "; ".join(done_now)[:300]
                + (f". {left} still on the way." if left else ". The list is done."))
        try:
            subprocess.run(["mochi-brain", "--trigger", text], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass


def status():
    q = load_queue()
    if not q:
        print("nothing on the conversion list")
        return
    for st in ("converting", "waiting", "failed", "done"):
        items = [i for i in q if i["state"] == st]
        if not items:
            continue
        print(f"{st} ({len(items)}):")
        for i in items:
            extra = i.get("why") or (Path(i["out"]).name if i.get("out") else "")
            print(f"  {i['name'][:70]}" + (f"  [{extra}]" if extra else ""))


def show_log(n):
    try:
        lines = LOG.read_text().splitlines()
    except OSError:
        lines = []
    print("\n".join(lines[-n:]) if lines else "nothing yet")


def main():
    ap = argparse.ArgumentParser(prog=PROG, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("queue", help="convert this Deluge torrent once it has arrived on the NAS")
    q.add_argument("torrent", help="Deluge id prefix, or part of the torrent's name")
    q.add_argument("--into", help="folder for the MP3s (default: Music on the NAS)")
    s = sub.add_parser("sync", help="convert what has arrived (the timer's job)")
    s.add_argument("--dry-run", action="store_true")
    c = sub.add_parser("convert", help="convert one folder now")
    c.add_argument("src")
    c.add_argument("--into")
    c.add_argument("--dry-run", action="store_true")
    sub.add_parser("status")
    lg = sub.add_parser("log")
    lg.add_argument("n", nargs="?", type=int, default=30)
    x = ap.parse_args()
    if x.cmd == "queue":
        queue(x.torrent, x.into)
    elif x.cmd == "sync":
        sync(x.dry_run)
    elif x.cmd == "convert":
        out, n, bad, note = convert(x.src, x.into, x.dry_run)
        if not x.dry_run:
            print(f"{note or f'{n - bad} of {n} tracks'} → {out}")
            if bad:
                sys.exit(1)
    elif x.cmd == "status":
        status()
    elif x.cmd == "log":
        show_log(x.n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
