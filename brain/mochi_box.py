#!/usr/bin/env python3
"""mochi-box: Mochi's own computer (peppy, an old Chromebook running Linux Mint), over ssh.

  mochi-box run [--timeout S] CMD...    run a shell command there (bash -l; sudo needs no password); prints its
                                        output and exits with its code. Default timeout 540 s.
  mochi-box spawn NAME CMD...           start a long job in the background there (a systemd --user unit box-NAME);
                                        it outlives the run and survives logouts
  mochi-box jobs                        the background jobs, running or finished
  mochi-box output NAME [--lines N]     a job's output so far (journal of box-NAME)
  mochi-box stop NAME                   stop a job
  mochi-box put PATH [REMOTE]           copy a file or folder from the workspace there (default ~/inbox/)
  mochi-box get REMOTE [PATH]           copy a file or folder from there into the workspace (default box/files/)
  mochi-box status                      up or not, uptime, load, memory, disk, battery
  mochi-box log [N]                     the last N commands sent (default 20)

Everything sent is logged in ~/.local/share/mochi/box/log.jsonl. Config (optional): ~/.config/mochi/box.json
{"host": "mochi@peppy.local", "key": "~/.ssh/mochi_ed25519"}. The box holds none of their keys or passwords:
put copies only from Mochi's workspace.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

CONFIG = Path(os.environ.get("MOCHI_BOX_CONFIG", Path.home() / ".config/mochi/box.json"))
WORK = Path(os.environ.get("MOCHI_HOME", Path.home() / ".local/share/mochi"))
DATA = WORK / "box"
LOG = DATA / "log.jsonl"
FILES = DATA / "files"
DEFAULT_TIMEOUT = 540  # under the Bash tool's 10-minute ceiling; longer work is a spawn


def cfg():
    try:
        c = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        c = {}
    return c.get("host", "mochi@peppy.local"), os.path.expanduser(c.get("key", "~/.ssh/mochi_ed25519"))


def ssh_args():
    host, key = cfg()
    return ["-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=30",
            "-o", "StrictHostKeyChecking=accept-new"], host


def log(kind, what, code, secs):
    DATA.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps({"t": int(time.time()), "kind": kind, "cmd": what, "rc": code, "s": round(secs, 1)},
                           ensure_ascii=False) + "\n")


def remote(cmd, timeout=DEFAULT_TIMEOUT, capture=False, kind="run"):
    """Run cmd (a shell string) on the box; stream or capture its output."""
    opts, host = ssh_args()
    t0 = time.time()
    try:
        p = subprocess.run(["ssh"] + opts + [host, "bash -lc " + shlex.quote(cmd)], timeout=timeout,
                           capture_output=capture, text=True, stdin=subprocess.DEVNULL)
        code, out = p.returncode, (p.stdout + p.stderr if capture else "")
    except subprocess.TimeoutExpired:
        code, out = 124, ""
        print(f"mochi-box: timed out after {timeout}s (a long job is `mochi-box spawn`)", file=sys.stderr)
    if code == 255:
        print(f"mochi-box: can't reach {host} (asleep, off, or off the network?)", file=sys.stderr)
    if kind:
        log(kind, cmd, code, time.time() - t0)
    return code, out


def opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        val = args[i + 1] if i + 1 < len(args) else default
        del args[i:i + 2]
        return val
    return default


def unit(name):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", name or ""):
        sys.exit("a job name is letters, digits, - _ . (up to 60)")
    return "box-" + name


def inside(path, root):
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    verb, args = args[0], args[1:]

    if verb == "run":
        timeout = int(opt(args, "--timeout", DEFAULT_TIMEOUT))
        if not args:
            sys.exit("mochi-box run CMD...")
        return remote(" ".join(args), timeout)[0]

    if verb == "spawn":
        if len(args) < 2:
            sys.exit("mochi-box spawn NAME CMD...")
        u, cmd = unit(args[0]), " ".join(args[1:])
        # a finished unit with the same name would block a new one
        code = remote(f"systemctl --user stop {u} 2>/dev/null; systemctl --user reset-failed {u} 2>/dev/null; "
                      f"systemd-run --user --quiet --unit={u} --property=RemainAfterExit=yes "
                      f"--working-directory=$HOME bash -lc {shlex.quote(cmd)}", 60, kind=None)[0]
        log("spawn", f"{args[0]}: {cmd}", code, 0)
        return code

    if verb == "jobs":
        return remote("systemctl --user list-units --all --no-legend --plain 'box-*' "
                      "| awk '{printf \"%-30s %s/%s\\n\", $1, $3, $4}'", 60, kind=None)[0]

    if verb == "output":
        if not args:
            sys.exit("mochi-box output NAME [--lines N]")
        n = int(opt(args, "--lines", 80))
        return remote(f"journalctl --user -u {unit(args[0])} -n {n} --no-pager -o cat", 60, kind=None)[0]

    if verb == "stop":
        if not args:
            sys.exit("mochi-box stop NAME")
        return remote(f"systemctl --user stop {unit(args[0])}", 60, kind="stop")[0]

    if verb == "put":
        if not args:
            sys.exit("mochi-box put PATH [REMOTE]")
        src = Path(args[0]).expanduser()
        if not src.is_absolute():
            src = Path.cwd() / src
        if not inside(src, WORK):
            sys.exit(f"put copies only from your workspace ({WORK}); their files stay on their machine")
        dest = args[1] if len(args) > 1 else "inbox/"
        opts, host = ssh_args()
        remote("mkdir -p ~/inbox", 30, kind=None)
        t0 = time.time()
        code = subprocess.run(["rsync", "-a", "-e", "ssh " + " ".join(shlex.quote(o) for o in opts),
                               str(src), f"{host}:{dest}"]).returncode
        log("put", f"{src} -> {dest}", code, time.time() - t0)
        return code

    if verb == "get":
        if not args:
            sys.exit("mochi-box get REMOTE [PATH]")
        dest = Path(args[1]).expanduser() if len(args) > 1 else FILES
        if not dest.is_absolute():
            dest = Path.cwd() / dest
        if not inside(dest, WORK):
            sys.exit(f"get copies only into your workspace ({WORK})")
        dest.mkdir(parents=True, exist_ok=True) if dest == FILES else None
        opts, host = ssh_args()
        t0 = time.time()
        code = subprocess.run(["rsync", "-a", "-e", "ssh " + " ".join(shlex.quote(o) for o in opts),
                               f"{host}:{args[0]}", str(dest)]).returncode
        log("get", f"{args[0]} -> {dest}", code, time.time() - t0)
        if code == 0:
            print(dest)
        return code

    if verb == "status":
        code, out = remote("echo \"$(hostname): up $(uptime -p | sed 's/^up //'), load $(cut -d' ' -f1-3 /proc/loadavg)\"; "
                           "free -h | awk '/Mem:/{print \"memory: \" $3 \" used of \" $2}'; "
                           "df -h / | awk 'NR==2{print \"disk: \" $3 \" used of \" $2 \" (\" $5 \")\"}'; "
                           "b=/sys/class/power_supply; echo \"battery: $(cat $b/BAT0/capacity 2>/dev/null)% "
                           "$(cat $b/BAT0/status 2>/dev/null), mains $( [ \"$(cat $b/AC/online 2>/dev/null)\" = 1 ] "
                           "&& echo on || echo off)\"; "
                           "j=$(systemctl --user list-units --state=running --no-legend --plain 'box-*' | wc -l); "
                           "echo \"jobs running: $j\"; "
                           "[ -f /var/run/reboot-required ] && echo 'wants a reboot (updates)'; true",
                           30, capture=True, kind=None)
        print(out.strip() if code == 0 else "down: " + (cfg()[0]))
        return 0 if code == 0 else 1

    if verb == "log":
        n = int(args[0]) if args else 20
        try:
            lines = LOG.read_text().splitlines()[-n:]
        except OSError:
            lines = []
        for ln in lines:
            e = json.loads(ln)
            print(time.strftime("%m-%d %H:%M", time.localtime(e["t"])), e["kind"], f"rc={e['rc']}",
                  f"{e['s']}s", e["cmd"][:200])
        return 0

    sys.exit(f"unknown: {verb} (mochi-box help)")


if __name__ == "__main__":
    sys.exit(main())
