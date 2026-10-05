#!/usr/bin/env python3
"""mochi-browser — Mochi's own Chrome.

    mochi-browser open [URL]     open Mochi's Chrome on your screen (its own profile), e.g. to log it into something
    mochi-browser close          close it
    mochi-browser status         where the profile is, is the browser open, is the MCP server installed
    mochi-browser mcp-config [--headed]   the MCP server config the relay hands to Claude Code (JSON)

Mochi browses through Google's chrome-devtools-mcp (an MCP server Claude Code talks to), always with its own Chrome
profile in ~/.local/share/mochi/chrome: its own cookies, logins, history and bookmarks, nothing of yours. The relay
writes the server config before every run and passes it with `--mcp-config`. Background runs get a headless Chrome
that lives only as long as the run; a chat gets a visible window. If Mochi's Chrome is already open on your screen
(`mochi-browser open`), every run attaches to that window instead, so you can watch, and log it into things first.
The debugging port is bound to localhost only; anything on this machine could drive that window while it's open.

Config (optional, `browser` in ~/.config/mochi/sources.json): `chrome` (executable, default google-chrome),
`port` (default 9333), `server` (path to chrome-devtools-mcp if it isn't found on its own).
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

HOME = Path.home()
WORK = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "mochi"
PROFILE = WORK / "chrome"
REPORTS = WORK / "reports"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "mochi" / "sources.json"
CONFIG_FILES = {False: WORK / "mcp-chrome.json", True: WORK / "mcp-chrome-headed.json"}


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
    """The browser Mochi (or you) opened with `mochi-browser open`, if it's up: Chrome's /json/version."""
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
    args = ["--no-usage-statistics", "--no-category-performance", "--no-category-emulation", "--no-category-memory",
            "--no-category-network", "--screenshot-format", "jpeg", "--screenshot-max-width", "1280",
            "--viewport", "1280x900", "--workspace", str(REPORTS)]
    if running():
        args = ["--browser-url", f"http://127.0.0.1:{port()}"] + args
    else:
        args = ["--user-data-dir", str(PROFILE)] + args + ([] if headed else ["--headless"])
        if chrome():
            args += ["--executable-path", chrome()]
    env = {"PATH": os.pathsep.join(bindirs + [os.environ.get("PATH", "/usr/bin:/bin")]),
           "CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS": "1"}
    for k in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "HOME"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return {"mcpServers": {"chrome": {"command": srv, "args": args, "env": env}}}


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


def open_browser(url=None):
    exe = chrome()
    if not exe:
        sys.exit("mochi-browser: no Chrome found (set browser.chrome in sources.json)")
    if running():
        print(f"already open (debugging port {port()})")
        if url:
            subprocess.Popen([exe, f"--user-data-dir={PROFILE}", url], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return 0
    PROFILE.mkdir(parents=True, exist_ok=True)
    cmd = [exe, f"--user-data-dir={PROFILE}", f"--remote-debugging-port={port()}", "--remote-debugging-address=127.0.0.1",
           "--no-first-run", "--no-default-browser-check", "--window-size=1280,900", url or "about:blank"]
    subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"opened Mochi's Chrome (profile {PROFILE}, debugging port {port()}, localhost only). "
          "Runs attach to this window while it's open; close it when you're done.")
    return 0


def close_browser():
    v = running()
    if not v:
        print("not open")
        return 0
    subprocess.run(["pkill", "-f", "--", f"user-data-dir={PROFILE} --remote-debugging-port={port()}"], check=False)
    print("closed")
    return 0


def status():
    srv = server()
    print(f"profile:  {PROFILE} ({'exists' if PROFILE.exists() else 'not created yet'})")
    print(f"chrome:   {chrome() or 'NOT FOUND'}")
    print(f"server:   {srv or 'NOT FOUND (npm install -g chrome-devtools-mcp)'}")
    v = running()
    print(f"window:   {'open: ' + v.get('Browser', '') + ' (runs attach to it)' if v else 'not open (runs use a headless Chrome of their own)'}")
    print(f"configs:  {CONFIG_FILES[False]} / {CONFIG_FILES[True]} (rewritten before each run)")
    return 0 if srv and chrome() else 1


def main():
    ap = argparse.ArgumentParser(prog="mochi-browser", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("open")
    o.add_argument("url", nargs="?")
    sub.add_parser("close")
    sub.add_parser("status")
    m = sub.add_parser("mcp-config")
    m.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    if a.cmd == "open":
        return open_browser(a.url)
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
