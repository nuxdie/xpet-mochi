#!/usr/bin/env python3
"""mochi-browser — Mochi's own Chrome.

    mochi-browser open [URL]     put Mochi's Chrome on your screen (its own profile), e.g. to log it into something
    mochi-browser show [URL]     the same, for Mochi: when a page needs the human (a captcha, a code, a login)
    mochi-browser hide           take it off the screen again (it keeps running)
    mochi-browser close          close it
    mochi-browser status         where the profile is, is the browser up, hidden or shown, is the MCP server installed
    mochi-browser mcp-config [--headed]   the MCP server config the relay hands to Claude Code (JSON)

Mochi browses through Google's chrome-devtools-mcp (an MCP server Claude Code talks to), always with its own Chrome
profile in ~/.local/share/mochi/chrome: its own cookies, logins, history and bookmarks, nothing of yours. The relay
writes the server config before every run and passes it with `--mcp-config`. Chrome is always started by this module
with a debugging port (so mochi-vault can sign in on a page without the password passing through Mochi) and the MCP
server attaches to it.

The Chrome is an ordinary headed one, but it runs on a nested X server (Xephyr) whose one window the relay keeps
hidden: off the screen and off the panel (wmctrl), nowhere to be seen until `mochi-browser show` brings it to the
front, so Mochi can hand a page over (a captcha, a one-time code, a login it may not do itself) and `hide` takes it
back. Nothing a page does (popups, new tabs, focus) can reach your desktop, because Chrome's desktop is the nested
one. A headless Chrome can't be shown later, and sites treat it with suspicion; this one is a real browser that just
isn't on screen. It lives as long as it's needed: the relay closes it after a run unless it's on the screen or a chat
is still using it. Without Xephyr, wmctrl or a DISPLAY, runs get a headless Chrome and `open` a plain window, as
before. The debugging port is bound to localhost only; anything on this machine could drive that browser while it's up.

Config (optional, `browser` in ~/.config/mochi/sources.json): `chrome` (executable, default google-chrome),
`port` (default 9333), `server` (path to chrome-devtools-mcp if it isn't found on its own), `display` (the nested
X display number, default the first free one from 77), `size` ("WIDTHxHEIGHT" of the nested screen, default
1280x1000), `scale` (device scale factor in the nested screen, default 1).
"""

import argparse
import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
WORK = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "mochi"
PROFILE = WORK / "chrome"
REPORTS = WORK / "reports"
STATE = WORK / "browser.json"  # the nested X server a Chrome is running on: {"display": ":77", "xephyr": pid}
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "mochi" / "sources.json"
CONFIG_FILES = {False: WORK / "mcp-chrome.json", True: WORK / "mcp-chrome-headed.json"}
TITLE = "Mochi's browser"  # the nested server's window on your screen; how it's found again
CHAT_MARK = "mcp-chrome-headed.json"  # on a chat's command line (and only there): a chat is alive while it's seen


def cfg():
    try:
        return json.loads(CONFIG.read_text()).get("browser", {}) or {}
    except (OSError, ValueError):
        return {}


def chrome():
    c = cfg().get("chrome") or ""
    for cand in ([c] if c else []) + ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]:
        p = shutil.which(cand)
        if p:
            return p
    return ""


def port():
    try:
        return int(cfg().get("port") or 9333)
    except (TypeError, ValueError):
        return 9333


def size():
    try:
        w, h = str(cfg().get("size") or "1280x1000").lower().split("x")
        return int(w), int(h)
    except ValueError:
        return 1280, 1000


def scale():
    try:
        return float(cfg().get("scale") or 1)
    except (TypeError, ValueError):
        return 1.0


def server():
    """chrome-devtools-mcp: on PATH, or in an fnm/nvm/npm-global Node install (the relay runs without a shell PATH)."""
    c = cfg().get("server") or ""
    if c and Path(os.path.expanduser(c)).is_file():
        return str(Path(os.path.expanduser(c)))
    p = shutil.which("chrome-devtools-mcp")
    if p:
        return p
    pats = [str(HOME / ".local/share/fnm/node-versions/*/installation/bin/chrome-devtools-mcp"),
            str(HOME / ".nvm/versions/node/*/bin/chrome-devtools-mcp"),
            str(HOME / ".npm-global/bin/chrome-devtools-mcp"), "/usr/local/bin/chrome-devtools-mcp"]
    hits = sorted(h for pat in pats for h in glob.glob(pat))
    return hits[-1] if hits else ""


def running():
    """Mochi's Chrome, if it's up (hidden, shown, or a run's headless one): /json/version."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port()}/json/version", timeout=2) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def mcp_config(headed=False):
    """The MCP server definition for a run. None if the server isn't installed."""
    srv = server()
    if not srv:
        return None
    # The server's shebang wants `node` on PATH; it lives next to the (unresolved) symlink, not next to its target.
    bindirs = [str(Path(srv).parent)]
    node = shutil.which("node")
    if node and str(Path(node).parent) not in bindirs:
        bindirs.append(str(Path(node).parent))
    w, h = size()
    args = ["--no-usage-statistics", "--no-category-performance", "--no-category-emulation", "--no-category-memory",
            "--no-category-network", "--screenshot-format", "jpeg", "--screenshot-max-width", "1280",
            "--viewport", f"{w}x{min(h, 900)}", "--workspace", str(REPORTS)]
    if running() or not chrome():
        args = ["--browser-url", f"http://127.0.0.1:{port()}"] + args
    else:  # not started (ensure() failed): let the server launch its own over a pipe, as before
        args = ["--user-data-dir", str(PROFILE), "--executable-path", chrome()] + args + ([] if headed else ["--headless"])
    env = {"PATH": os.pathsep.join(bindirs + [os.environ.get("PATH", "/usr/bin:/bin")]),
           "CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS": "1"}
    for k in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "HOME"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return {"mcpServers": {"chrome": {"command": srv, "args": args, "env": env}}}


# ---- the nested X server --------------------------------------------------------------------------------

def nested_possible():
    return bool(shutil.which("Xephyr") and shutil.which("wmctrl") and shutil.which("xprop") and os.environ.get("DISPLAY"))


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def nested():
    """The nested server we started, if it's still running: {"display", "xephyr"}; else {}."""
    try:
        s = json.loads(STATE.read_text())
        pid = int(s["xephyr"])
        if alive(pid) and Path(f"/proc/{pid}/comm").read_text().strip() == "Xephyr":
            return s
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {}


def free_display():
    d = cfg().get("display")
    cands = [int(d)] if str(d or "").isdigit() else range(77, 100)
    for n in cands:
        if not Path(f"/tmp/.X{n}-lock").exists() and not Path(f"/tmp/.X11-unix/X{n}").exists():
            return f":{n}"
    return ""


def window():
    """The nested server's window on your screen (wmctrl id), or ""."""
    if not nested():
        return ""
    try:
        out = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[3] == TITLE:
            return parts[0]
    return ""


def shown():
    """Is Mochi's browser on the screen right now? (A plain, non-nested window counts as shown.)"""
    v = running()
    if not v:
        return False
    if "Headless" in v.get("Browser", "") + v.get("User-Agent", ""):
        return False
    w = window()
    if not w:
        return not nested()  # a plain window (no Xephyr): visible by definition
    try:
        out = subprocess.run(["xprop", "-id", w, "_NET_WM_STATE"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "_NET_WM_STATE_HIDDEN" not in out


def _wm(w, op):
    subprocess.run(["wmctrl", "-i", "-r", w, "-b", f"{op},hidden,skip_taskbar"], check=False, timeout=5,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _settled(want_shown):
    """The window manager applies a state change a moment after wmctrl asks; wait for it (up to a second)."""
    for _ in range(20):
        if shown() == want_shown:
            return True
        time.sleep(0.05)
    return False


def hide():
    """Take the window off the screen and the panel. True if there was one."""
    w = window()
    if w:
        _wm(w, "add")
        _settled(False)
    return bool(w)


def show():
    """Bring the window to the front. True if there was one."""
    w = window()
    if w:
        _wm(w, "remove")
        subprocess.run(["wmctrl", "-i", "-a", w], check=False, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _settled(True)
    return bool(w)


def start_nested():
    """Start Xephyr, hide its window the moment it appears, give it the host's keyboard layout. The display, or ""."""
    disp = free_display()
    if not disp:
        return ""
    w, h = size()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    try:
        p = subprocess.Popen(["Xephyr", disp, "-screen", f"{w}x{h}", "-title", TITLE, "-nolisten", "tcp", "-br",
                              "-noreset", "-no-host-grab"], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return ""
    STATE.write_text(json.dumps({"display": disp, "xephyr": p.pid}))
    for _ in range(250):  # its window maps within a few tens of ms; hide it before anyone notices
        if hide():
            break
        if p.poll() is not None:
            break
        time.sleep(0.02)
    else:
        p.terminate()
    if p.poll() is not None or not Path(f"/tmp/.X11-unix/X{disp[1:]}").exists():
        STATE.unlink(missing_ok=True)
        return ""
    try:  # the nested server starts with a US keymap; take the host's layout so what they type comes out right
        q = dict(line.split(":", 1) for line in subprocess.run(["setxkbmap", "-query"], capture_output=True,
                                                                text=True, timeout=5).stdout.splitlines() if ":" in line)
        layout, variant = q.get("layout", "").strip(), q.get("variant", "").strip()
        if layout:
            subprocess.run(["setxkbmap", "-display", disp, "-layout", layout] + (["-variant", variant] if variant else []),
                           check=False, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return disp


def stop_nested():
    s = nested()
    if s:
        pid = int(s["xephyr"])
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(40):  # Xephyr takes a moment to go; it isn't our child, so poll
                if not alive(pid) or Path(f"/proc/{pid}/comm").read_text().strip() != "Xephyr":
                    break
                time.sleep(0.05)
            else:
                os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    STATE.unlink(missing_ok=True)


# ---- Chrome ---------------------------------------------------------------------------------------------

def launch(headless=False):
    """Start Mochi's Chrome with the debugging port (localhost only) and wait for it. True once it answers.
    On the hidden nested display when that's possible (`headless` is then moot); else headless or a plain window."""
    exe = chrome()
    if not exe:
        return False
    PROFILE.mkdir(parents=True, exist_ok=True)
    w, h = size()
    cmd = [exe, f"--user-data-dir={PROFILE}", f"--remote-debugging-port={port()}", "--remote-debugging-address=127.0.0.1",
           "--no-first-run", "--no-default-browser-check", f"--window-size={w},{h}", "--mute-audio"]
    env = dict(os.environ)
    disp = start_nested() if nested_possible() else ""
    if disp:
        env["DISPLAY"] = disp
        env.pop("WAYLAND_DISPLAY", None)
        cmd += ["--window-position=0,0", f"--force-device-scale-factor={scale():g}"]
    elif headless:
        cmd += ["--headless", "--hide-scrollbars"]
    try:
        subprocess.Popen(cmd + ["about:blank"], env=env, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        stop_nested()
        return False
    for _ in range(40):
        if running():
            return True
        time.sleep(0.25)
    kill_chrome()
    stop_nested()
    return False


def ensure(headed=False):
    """Mochi's Chrome up for a run: the one that's up if there is one, else a new one (hidden; `headed` only matters
    without a nested display). Returns True when this call started it."""
    if not server() or running() or in_use():
        return False
    return launch(headless=not headed)


def in_use():
    """The profile is held by a Chrome without our port (one the MCP server launched itself, as before): leave it be,
    a second Chrome on the same profile would only open a tab in that one."""
    lock = PROFILE / "SingletonLock"
    try:
        pid = int(os.readlink(lock).rpartition("-")[2])
        # a Chrome killed outright leaves its lock behind, and the pid may be live again as something else (or a
        # zombie): only a process that is a Chrome on this profile counts
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        return f"user-data-dir={PROFILE}" in cmd
    except (OSError, ValueError):
        return False


def kill_chrome():
    subprocess.run(["pkill", "-f", "--", f"user-data-dir={PROFILE} --remote-debugging-port={port()}"], check=False)


def chat_alive():
    """Is a chat (an interactive Claude with the headed config on its command line) still running?"""
    try:
        return subprocess.run(["pgrep", "-f", CHAT_MARK], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def stop():
    """After a run: close the browser, unless it's on their screen or a chat is still using it."""
    v = running()
    if not v:
        return
    if "Headless" in v.get("Browser", "") + v.get("User-Agent", ""):
        kill_chrome()
        return
    if not nested():
        return  # a plain visible window (theirs): stays
    if shown() or chat_alive():
        return
    kill_chrome()
    time.sleep(0.5)
    stop_nested()


def write_config(headed=False):
    """Write the config for a run and return its path (None when browsing isn't available)."""
    c = mcp_config(headed)
    if not c:
        return None
    PROFILE.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = CONFIG_FILES[headed]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, indent=1))
    tmp.replace(path)
    return path


def open_tab(url):
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port()}/json/new?{url}", method="PUT")
        with urllib.request.urlopen(req, timeout=5):
            return True
    except (OSError, ValueError):
        return False


# ---- commands -------------------------------------------------------------------------------------------

def open_browser(url=None):
    if not chrome():
        sys.exit("mochi-browser: no Chrome found (set browser.chrome in sources.json)")
    if not running() and not launch(headless=False):
        sys.exit("mochi-browser: Chrome didn't come up")
    if url:
        open_tab(url)
    if nested():
        if not show():
            sys.exit("mochi-browser: the browser is up but its window wasn't found")
        print(f"Mochi's Chrome is on the screen (profile {PROFILE}, debugging port {port()}, localhost only). "
              "Runs and chats keep using it; `mochi-browser hide` takes it off the screen, `close` closes it.")
    else:
        print(f"opened Mochi's Chrome (profile {PROFILE}, debugging port {port()}, localhost only). "
              "Runs attach to this window while it's open; close it when you're done.")
    return 0


def hide_browser():
    if not running():
        print("not open")
    elif hide():
        print("hidden (still running; `mochi-browser show` brings it back)")
    else:
        print("not a nested window; nothing to hide (close it instead)")
    return 0


def close_browser():
    if not running() and not nested():
        print("not open")
        return 0
    kill_chrome()
    time.sleep(0.5)
    stop_nested()
    print("closed")
    return 0


def status():
    srv = server()
    print(f"profile:  {PROFILE} ({'exists' if PROFILE.exists() else 'not created yet'})")
    print(f"chrome:   {chrome() or 'NOT FOUND'}")
    print(f"server:   {srv or 'NOT FOUND (npm install -g chrome-devtools-mcp)'}")
    print(f"nested:   {'Xephyr + wmctrl, display ' + (nested().get('display') or free_display() or '?') if nested_possible() else 'NOT POSSIBLE (needs Xephyr, wmctrl, xprop and a DISPLAY): runs get a headless Chrome'}")
    v = running()
    if v:
        where = "on the screen" if shown() else ("hidden" if nested() else "headless")
        print(f"browser:  up, {where}: {v.get('Browser', '')}" + (" (a chat is using it)" if chat_alive() else ""))
    else:
        print("browser:  not running (a run starts one, hidden, and closes it after)")
    print(f"configs:  {CONFIG_FILES[False]} / {CONFIG_FILES[True]} (rewritten before each run)")
    return 0 if srv and chrome() else 1


def main():
    ap = argparse.ArgumentParser(prog="mochi-browser", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("open", "show"):
        sub.add_parser(name).add_argument("url", nargs="?")
    sub.add_parser("hide")
    sub.add_parser("close")
    sub.add_parser("status")
    m = sub.add_parser("mcp-config")
    m.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    if a.cmd in ("open", "show"):
        return open_browser(a.url)
    if a.cmd == "hide":
        return hide_browser()
    if a.cmd == "close":
        return close_browser()
    if a.cmd == "status":
        return status()
    c = mcp_config(a.headed)
    if not c:
        sys.exit("mochi-browser: chrome-devtools-mcp not found (npm install -g chrome-devtools-mcp)")
    print(json.dumps(c, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
