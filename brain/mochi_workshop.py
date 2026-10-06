#!/usr/bin/env python3
"""mochi-workshop — where Mochi works on its own looks and tricks at night.

Mochi's night workshop run (mochi-brain, kind "workshop") edits the pig's C++ in its own git worktree of the xpet
repo, on the branch mochi/workshop, inside Mochi's workspace (workshop/xpet). This tool is both sides of that:

  for Mochi (allowed in a workshop run):
    mochi-workshop build                   compile xpet in the worktree
    mochi-workshop test                    build and run the render test (every pose, action and costume)
    mochi-workshop preview [--date YYYY-MM-DD] [--costume NAME] [--strip ACTION]
                                           render a sheet (or one action, frame by frame) to reports/workshop/
                                           and print the path, so the run can Read the picture
    mochi-workshop diff                    what changed tonight, against the last commit

  for the relay (and you):
    mochi-workshop prepare                 worktree up to date with main, nothing left over from a crashed night
    mochi-workshop finish TITLE [BODY]     check what changed (src/ and tests/*.cpp only), build, test, commit
    mochi-workshop deploy                  install the worktree's xpet, restart the pig, roll back if it dies
    mochi-workshop undo COMMIT             revert a workshop commit and redeploy
    mochi-workshop keep                    fast-forward main to the workshop branch (only if main's checkout is clean)
    mochi-workshop guard                   the pig keeps crashing on a workshop build: put the last good one back
    mochi-workshop status                  where things stand

The relay gates all of it: nothing Mochi writes outside src/ and tests/*.cpp is committed, a build that doesn't
compile or fails the render test is thrown away, and a binary that doesn't stay up is replaced by the previous one.
"""

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
WORK = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "mochi"
SHOP = WORK / "workshop"
TREE = SHOP / "xpet"                 # Mochi's worktree
BRANCH = "mochi/workshop"
STATE = SHOP / "state.json"          # what's deployed, the last good binary
GOOD = SHOP / "xpet.good"            # the binary that ran before the last deploy
PREVIEWS = WORK / "reports" / "workshop"
REPO_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "mochi" / "xpet-repo"  # written by make install
BIN = Path(os.environ.get("XPET_BIN") or HOME / ".local/bin/xpet")
ALLOWED = ("src/",)                  # what a workshop night may change, besides tests/*.cpp
SETTLE = 8                           # seconds a freshly deployed pig must stay up


def repo():
    try:
        p = Path(REPO_FILE.read_text().strip())
        if (p / ".git").exists():
            return p
    except OSError:
        pass
    return HOME / "claude-exp-01" / "xpet"


def sh(cmd, cwd=None, timeout=600, env=None):
    try:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, **(env or {})))
        return p.returncode, (p.stdout + p.stderr).rstrip()  # not strip(): porcelain status starts with a space
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(cmd)}"
    except OSError as e:
        return 127, str(e)


def git(*args, cwd=None, timeout=120):
    return sh(["git", *args], cwd=cwd or TREE, timeout=timeout)


def load():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save(st):
    SHOP.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(STATE)


def out(ok, msg, **extra):
    return dict(ok=ok, msg=msg, **extra)


# ---- the relay's side ------------------------------------------------------------------------------

def prepare():
    """Make sure the worktree exists, is clean, and has main merged in. Returns notes for the run's prompt."""
    R = repo()
    notes = []
    SHOP.mkdir(parents=True, exist_ok=True)
    if not (TREE / ".git").exists():
        code, _ = git("rev-parse", "--verify", BRANCH, cwd=R)
        args = ["worktree", "add", str(TREE)] + ([BRANCH] if code == 0 else ["-b", BRANCH, "main"])
        code, msg = git(*args, cwd=R)
        if code:
            return out(False, f"couldn't create the worktree: {msg}")
        notes.append("set up a fresh worktree from main")
    code, dirty = git("status", "--porcelain")
    if dirty:  # a night that died half way: throw its leftovers away (it was never committed, so never deployed)
        discard()
        notes.append("threw away uncommitted leftovers from an unfinished night")
    code, ahead = git("rev-list", "--count", f"{BRANCH}..main")
    if code == 0 and ahead.strip() != "0":
        code, msg = git("merge", "--no-edit", "main")
        if code:
            git("merge", "--abort")
            return out(False, f"main ({ahead.strip()} new commits) doesn't merge into {BRANCH} cleanly; "
                              f"needs a human: {msg[-400:]}")
        notes.append(f"merged {ahead.strip()} new commits from main (things the user or Claude Code changed)")
    return out(True, "; ".join(notes) or "worktree ready, up to date with main")


def changed_paths():
    code, s = git("status", "--porcelain", "--untracked-files=all")
    paths = []
    for line in s.splitlines():
        p = line[3:].strip()
        if " -> " in p:
            p = p.split(" -> ")[1]
        paths.append(p.strip('"'))
    return paths


def allowed(path):
    return path.startswith(ALLOWED) or (path.startswith("tests/") and path.endswith(".cpp") and "/" not in path[6:])


def build():
    return sh(["make", "-s", "xpet"], cwd=TREE, timeout=600)


def render_test():
    """The C++ render test only (make test also runs the brain's Python tests, which a workshop never touches)."""
    code, msg = sh(["make", "-s", "xpet"], cwd=TREE, timeout=600)
    if code:
        return code, msg
    cflags = sh(["pkg-config", "--cflags", "--libs", "cairo"])[1].split()
    code, msg = sh(["g++", "-std=c++17", "-O2", "-Wall", "-Wno-missing-field-initializers", "tests/render_test.cpp",
                    "-o", "tests/render_test", *cflags], cwd=TREE, timeout=600)
    if code:
        return code, msg
    return sh(["./tests/render_test"], cwd=TREE, timeout=300)


def finish(title, body=""):
    """Gate what the night produced and commit it. Nothing outside src/ and tests/*.cpp; it must build and pass."""
    paths = changed_paths()
    if not paths:
        return out(False, "nothing changed", nothing=True)
    bad = [p for p in paths if not allowed(p)]
    if bad:
        for p in bad:  # put back only the forbidden files; the rest still has to pass on its own
            if git("ls-files", "--error-unmatch", p)[0] == 0:
                git("checkout", "--", p)
            else:
                (TREE / p).unlink(missing_ok=True)
    code, msg = render_test()
    if code:
        discard()
        return out(False, f"didn't pass, thrown away: {msg[-1500:]}", reverted=bad)
    git("add", "-A", "--", *[d for d in ("src", "tests") if (TREE / d).exists()])
    message = f"Mochi's workshop: {title.strip()[:70]}\n\n{body.strip()}\n".rstrip() + "\n"
    code, msg = sh(["git", "-c", "user.name=Mochi", "-c", "user.email=mochi@localhost", "commit", "-q", "-m", message],
                   cwd=TREE)
    if code:
        return out(False, f"commit failed: {msg}")
    sha = git("rev-parse", "HEAD")[1].strip()
    stat = git("show", "--stat", "--format=", "HEAD")[1].strip()
    return out(True, f"committed {sha[:8]}", sha=sha, stat=stat, reverted=bad)


def unit(*args):
    return sh(["systemctl", "--user", *args, "xpet.service"], timeout=30)


def prop(name):
    return sh(["systemctl", "--user", "show", "-p", name, "--value", "xpet.service"], timeout=10)[1].strip()


def restarts():
    try:
        return int(prop("NRestarts"))
    except ValueError:
        return 0


def discard():
    """Throw away whatever the worktree has that isn't committed."""
    git("checkout", "--", ".")
    git("clean", "-fdq", "--", *[d for d in ("src", "tests") if (TREE / d).exists()])


def deploy(sha=""):
    """Install the worktree's binary in place of the running one. If the pig doesn't stay up, put the old one back."""
    new = TREE / "xpet"
    if not new.exists():
        return out(False, "no binary in the worktree (build first)")
    was_up = unit("is-active")[0] == 0
    st = load()
    if BIN.exists():
        shutil.copy2(BIN, GOOD)
    tmp = BIN.with_suffix(".new")
    shutil.copy2(new, tmp)
    tmp.replace(BIN)  # atomic: the running pig keeps its old inode
    st.update(deployed=sha or git("rev-parse", "HEAD")[1].strip(), deployed_at=time.time(), verified=False)
    save(st)
    if not was_up:
        return out(True, "installed; the pig wasn't running, so it couldn't be tried (guard watches for crashes)")
    before = restarts()
    unit("restart")
    time.sleep(SETTLE)
    if unit("is-active")[0] == 0 and restarts() == before:
        st.update(verified=True, restarts=restarts())
        save(st)
        return out(True, "installed and running")
    rollback("it didn't stay up after the restart")
    return out(False, "the new pig didn't stay up; the previous one is back")


def rollback(why):
    st = load()
    if GOOD.exists():
        tmp = BIN.with_suffix(".new")
        shutil.copy2(GOOD, tmp)
        tmp.replace(BIN)
    bad = st.get("deployed")
    if bad and git("merge-base", "--is-ancestor", bad, "HEAD")[0] == 0:
        sh(["git", "-c", "user.name=Mochi", "-c", "user.email=mochi@localhost", "revert", "--no-edit", bad], cwd=TREE)
    st.setdefault("broken", []).append({"sha": bad, "why": why, "at": time.time()})
    st["broken"] = st["broken"][-20:]
    st.update(deployed=None, verified=False)
    save(st)
    unit("reset-failed")
    unit("restart")


def guard():
    """Called by the relay every few minutes: a pig that keeps dying on a fresh workshop build goes back."""
    st = load()
    if not st.get("deployed") or time.time() - st.get("deployed_at", 0) > 3 * 86400:
        return out(True, "nothing to guard")
    # Only a crash counts (a signal or a core dump): a pig that exits because the X session went away (logout)
    # fails with an exit code, and that's not the build's fault.
    crashed = prop("Result") in ("signal", "core-dump", "watchdog")
    if crashed and (unit("is-failed")[0] == 0 or restarts() - st.get("restarts", restarts()) >= 3):
        rollback("it kept crashing after the deploy")
        return out(False, f"rolled back {st['deployed'][:8]}: the pig kept crashing", rolled_back=st["deployed"])
    return out(True, "fine")


def undo(sha):
    """Revert one workshop commit, rebuild, redeploy."""
    if git("merge-base", "--is-ancestor", sha, "HEAD")[0] != 0:
        return out(False, f"{sha[:8]} isn't on {BRANCH}")
    code, msg = sh(["git", "-c", "user.name=Mochi", "-c", "user.email=mochi@localhost", "revert", "--no-edit", sha],
                   cwd=TREE)
    if code:
        sh(["git", "revert", "--abort"], cwd=TREE)
        return out(False, f"couldn't revert {sha[:8]} cleanly (later work depends on it): {msg[-300:]}")
    code, msg = build()
    if code:
        return out(False, f"reverted, but it doesn't build: {msg[-500:]}")
    return deploy()


def keep():
    """Bring the workshop's commits into main, if that's a fast-forward and main's checkout is untouched."""
    R = repo()
    if git("rev-parse", "--abbrev-ref", "HEAD", cwd=R)[1].strip() != "main":
        return out(False, "the repo isn't on main; left on the branch")
    if git("status", "--porcelain", "--untracked-files=no", cwd=R)[1].strip():
        return out(False, "main has uncommitted changes; left on the branch for now")
    if git("rev-list", "--count", f"main..{BRANCH}", cwd=R)[1].strip() == "0":
        return out(True, "main already has it")
    code, msg = git("merge", "--ff-only", BRANCH, cwd=R)
    if code:
        return out(False, f"not a fast-forward (main moved on); it merges at the next workshop night: {msg[-200:]}")
    return out(True, "main now has it")


# ---- Mochi's side ------------------------------------------------------------------------------------

def preview(argv):
    """Render what the worktree's pig looks like now, into reports/workshop/, and print where."""
    env, name, strip = {}, "sheet", None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--date" and i + 1 < len(argv):
            env["XPET_DATE"] = argv[i + 1]; name += "-" + argv[i + 1]; i += 2
        elif a == "--costume" and i + 1 < len(argv):
            env["XPET_COSTUME"] = argv[i + 1]; name += "-" + argv[i + 1]; i += 2
        elif a == "--strip" and i + 1 < len(argv):
            strip = argv[i + 1]; i += 2
        else:
            return out(False, f"unknown argument {a}")
    code, msg = build()
    if code:
        return out(False, f"doesn't build:\n{msg[-3000:]}")
    PREVIEWS.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    if strip:
        path = PREVIEWS / f"{stamp}-{strip}{name[5:]}.png"
        code, msg = sh([str(TREE / "xpet"), "--strip", strip, str(path)], env=env)
    else:
        path = PREVIEWS / f"{stamp}-{name}.png"
        code, msg = sh([str(TREE / "xpet"), "--sheet", str(path)], env=dict(env, XPET_SHEET_ZOOM="1.6"))
    if code:
        return out(False, msg)
    return out(True, str(path), path=str(path))


def status():
    st = load()
    lines = [f"worktree: {TREE} ({'present' if (TREE / '.git').exists() else 'not yet'})", f"repo: {repo()}"]
    if (TREE / ".git").exists():
        lines.append("recent: \n  " + git("log", "--oneline", "-8")[1].replace("\n", "\n  "))
        lines.append(f"ahead of main: {git('rev-list', '--count', 'main..HEAD')[1].strip()}")
    lines.append(f"deployed: {(st.get('deployed') or '-')[:8]} {'(verified)' if st.get('verified') else ''}")
    for b in st.get("broken", [])[-3:]:
        lines.append(f"rolled back {str(b['sha'])[:8]}: {b['why']}")
    return out(True, "\n".join(lines))


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "build":
        code, msg = build()
        print(msg or "built")
        return code
    if cmd == "test":
        code, msg = render_test()
        print(msg)
        return code
    if cmd == "diff":
        print(git("status", "--short")[1])
        print(git("diff", "--stat")[1])
        return 0
    if cmd == "preview":
        r = preview(argv[2:])
    elif cmd == "prepare":
        r = prepare()
    elif cmd == "finish" and len(argv) > 2:
        r = finish(argv[2], argv[3] if len(argv) > 3 else "")
    elif cmd == "deploy":
        r = deploy()
    elif cmd == "undo" and len(argv) > 2:
        r = undo(argv[2])
    elif cmd == "keep":
        r = keep()
    elif cmd == "guard":
        r = guard()
    elif cmd == "status":
        r = status()
    else:
        print(__doc__.strip())
        return 0 if cmd in ("-h", "--help") else 1
    print(r["msg"])
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
