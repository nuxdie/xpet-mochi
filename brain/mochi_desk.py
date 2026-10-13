#!/usr/bin/env python3
"""mochi-desk: this machine's camera, microphone and screen, on request (the desk's twin of the phone's captures).

  mochi-desk photo [--device /dev/videoN]          a still from the webcam (warms up a few frames first)
  mochi-desk audio [--seconds N] [--source NAME]   a clip from the microphone, transcribed (faster-whisper)
  mochi-desk screen [--monitor N]                  what's on the screen: all monitors, or one (xrandr's order)
  mochi-desk media [--days N]                      the pictures, clips and screenshots that were taken, with transcripts
  mochi-desk status                                the camera, the microphones, the monitors

Captures land in ~/.local/share/mochi/desk/media/. Config (optional): ~/.config/mochi/desk.json
{"camera": "/dev/video0", "source": "pulse source name"}; otherwise the first camera and the default source.
"""
import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

CONFIG = Path(os.environ.get("MOCHI_DESK_CONFIG", Path.home() / ".config/mochi/desk.json"))
WORK = Path(os.environ.get("MOCHI_HOME", Path.home() / ".local/share/mochi"))
DATA = WORK / "desk"
MEDIA = DATA / "media"
INDEX = DATA / "captures.json"
MAX_SECONDS = 300


def cfg():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def run(cmd, timeout=60, env=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, "", str(e)


def stamp():
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def record(kind, path, **extra):
    """Remember what was taken (media lists it) and print where it landed."""
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        caps = json.loads(INDEX.read_text())
    except (OSError, ValueError):
        caps = []
    caps.append({"t": int(time.time()), "kind": kind, "path": str(path), **extra})
    INDEX.write_text(json.dumps(caps[-200:], indent=1, ensure_ascii=False))
    print(path)
    if extra.get("text"):
        print(extra["text"])


def camera():
    dev = cfg().get("camera") or (sorted(glob.glob("/dev/video*")) or [""])[0]
    return dev


def photo(args):
    dev = opt(args, "--device") or camera()
    if not dev or not Path(dev).exists():
        sys.exit("no camera (set camera in ~/.config/mochi/desk.json)")
    MEDIA.mkdir(parents=True, exist_ok=True)
    out = MEDIA / f"{stamp()}-photo.jpg"
    # the first frames of a webcam are dark while exposure settles: take a dozen, keep the last
    code, _, err = run(["ffmpeg", "-v", "error", "-y", "-f", "v4l2", "-input_format", "mjpeg", "-video_size", "1280x720",
                        "-i", dev, "-frames:v", "12", "-update", "1", "-q:v", "4", str(out)], timeout=40)
    if code != 0 or not out.exists():
        code, _, err = run(["ffmpeg", "-v", "error", "-y", "-f", "v4l2", "-i", dev, "-frames:v", "12", "-update", "1",
                            "-q:v", "4", str(out)], timeout=40)
    if code != 0 or not out.exists():
        sys.exit(f"camera failed: {err.strip()[-200:]} (in use by another app?)")
    record("photo", out, device=dev)


def source():
    s = cfg().get("source")
    if s:
        return s
    code, out, _ = run(["pactl", "get-default-source"], timeout=10)
    return out.strip() if code == 0 and out.strip() else "default"


def audio(args):
    secs = max(1, min(int(opt(args, "--seconds") or 10), MAX_SECONDS))
    src = opt(args, "--source") or source()
    MEDIA.mkdir(parents=True, exist_ok=True)
    out = MEDIA / f"{stamp()}-audio.m4a"
    code, _, err = run(["ffmpeg", "-v", "error", "-y", "-f", "pulse", "-i", src, "-t", str(secs), "-ac", "1", "-ar", "16000",
                        "-c:a", "aac", "-b:a", "32k", str(out)], timeout=secs + 30)
    if code != 0 or not out.exists():
        sys.exit(f"microphone failed: {err.strip()[-200:]}")
    text = None
    code, t_out, t_err = run(["mochi-phone", "transcribe", str(out)], timeout=900)
    if code == 0 and t_out.strip():
        text = t_out.strip()
        out.with_suffix(".txt").write_text(text)
    record("audio", out, seconds=secs, source=src, text=text)


def monitors():
    code, out, _ = run(["xrandr", "--listmonitors"], timeout=10)
    mons = []
    for line in out.splitlines():
        m = re.match(r"\s*(\d+):\s+\S+\s+(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)\s+(\S+)", line)
        if m:
            mons.append({"n": int(m.group(1)), "w": int(m.group(2)), "h": int(m.group(3)), "x": int(m.group(4)), "y": int(m.group(5)), "name": m.group(6)})
    return mons


def screen(args):
    MEDIA.mkdir(parents=True, exist_ok=True)
    which = opt(args, "--monitor")
    out = MEDIA / f"{stamp()}-screen{'-' + which if which else ''}.png"
    box = None
    if which is not None:
        mons = {str(m["n"]): m for m in monitors()}
        if which not in mons:
            sys.exit(f"no monitor {which}; have: " + ", ".join(f"{m['n']} ({m['name']}, {m['w']}x{m['h']})" for m in mons.values()))
        m = mons[which]
        box = (m["x"], m["y"], m["x"] + m["w"], m["y"] + m["h"])
    code, _, err = run(["import", "-window", "root", "-silent", str(out)], timeout=40,
                       env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")})
    if code != 0 or not out.exists():
        sys.exit(f"screenshot failed: {err.strip()[-200:]}")
    fit(out, box)
    record("screen", out, monitor=which)


def fit(path, box=None, max_side=2400):
    """One monitor out of the root window if asked (import's own -crop is unreliable), and a readable size."""
    from PIL import Image
    im = Image.open(path)
    if box:
        im = im.crop(box)
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side))
    im.save(path)


def media(args):
    days = int(opt(args, "--days") or 2)
    since = time.time() - days * 86400
    try:
        caps = [c for c in json.loads(INDEX.read_text()) if c.get("t", 0) >= since]
    except (OSError, ValueError):
        caps = []
    if not caps:
        return f"_nothing captured here in the last {days} day(s)_"
    out = []
    for c in caps:
        line = f"{dt.datetime.fromtimestamp(c['t']):%a %H:%M} {c['kind']}" + (f" {c['seconds']}s" if c.get("seconds") else "") + f": {c['path']}"
        if c.get("text"):
            line += f"\n    {c['text'][:300]}"
        out.append(line)
    return "\n".join(out)


def status(_args):
    print(f"camera   {camera() or 'none'}" + ("" if Path(camera() or "/nonexistent").exists() else " (missing)"))
    print(f"mic      {source()}")
    _, out, _ = run(["pactl", "list", "short", "sources"], timeout=10)
    for line in out.splitlines():
        if ".monitor" not in line:
            print("         " + line.split("\t")[1])
    print("monitors " + ", ".join(f"{m['n']}: {m['name']} {m['w']}x{m['h']}" for m in monitors()))
    print(f"whisper  {'ok' if (Path.home() / '.local/share/mochi-tools/bin/python').exists() else 'missing (make tools; pip install faster-whisper)'}")


def opt(args, name):
    if name in args:
        i = args.index(name)
        return args[i + 1] if i + 1 < len(args) else ""
    return None


def main(argv):
    cmds = {"photo": photo, "audio": audio, "screen": screen, "media": media, "status": status}
    if len(argv) < 2 or argv[1] not in cmds:
        print(__doc__.strip())
        return 2
    r = cmds[argv[1]](argv[2:])
    if isinstance(r, str):
        print(r)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
