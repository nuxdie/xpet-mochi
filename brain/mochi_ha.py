#!/usr/bin/env python3
"""mochi-ha: which of Mochi's machines is Mochi right now.

Mochi's brain (mochi-brain and the services around it) runs on one machine at a time, the *leader*; the others stand
by with the same workspace, kept in step by Syncthing (folders mochi-work, mochi-config, mochi-claude). This daemon
runs on every node and decides, every few seconds, whether its own node should lead:

  - every node publishes a *beacon* (who it is, whether it's healthy, whether it leads, its term) to the relay on the
    Hetzner box (PUT /v1/beacon/NODE) and serves it on the LAN (port 8378), and reads everyone else's both ways;
  - the healthy node with the best (lowest) priority should lead: desk, then sff, then the cloud;
  - a better node that comes back *claims*; the leader finishes what it's running, saves, steps down and publishes
    the state revision it left; the claimant waits until Syncthing has brought it that revision, then leads;
  - a leader that goes silent (off, in Windows, a power cut) is replaced once it has been unseen for LEADER_LOST;
  - a leader that can't reach the internet steps down after SICK_FOR; one that sees a newer term elsewhere (it was
    asleep while another took over) steps down at once.

Being leader means: $XDG_RUNTIME_DIR/mochi-ha.json says so (mochi-brain reads it and runs as the relay, or as a
quiet follower that forwards what it hears), mochi-leader.target is started (the phone's mailbox, the timers), and on
the LAN the name mochi.local points at this node (the wall clocks talk to it).

  mochi-ha serve            the daemon (user service mochi-ha)
  mochi-ha status           every node as this one sees it, and the sync state
  mochi-ha hold [REASON]    this node never leads (gaming, maintenance); hands over if it leads now
  mochi-ha release          undo hold
  mochi-ha prefer NODE      ask NODE to lead now if it is healthy (a hold on every better node, for an hour)

Config (synced): ~/.config/mochi/ha.json  {"relay", "token", "nodes": {NAME: {"prio", "lan"}}}
This node's name: ~/.config/mochi-ha/node (not synced; written by ha/deploy.sh)
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
CONF = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
CLUSTER = CONF / "mochi" / "ha.json"
LOCAL = CONF / "mochi-ha"  # this node's own settings and state, never synced
WORK = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "mochi"
STATE_FILE = WORK / "relay.json"  # the brain's state; its "rev" says how far Syncthing has brought it
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
ROLE_FILE = RUNTIME / "mochi-ha.json"  # what this daemon decided (mochi-brain reads it)
ACK_FILE = RUNTIME / "mochi-brain.role"  # what the brain did about it (written by mochi-brain)
AUTH_FILE = RUNTIME / "mochi-brain.auth"  # the brain's last Claude login failure (a run refused: logged out, expired)
PORT = 8378

TICK = 5
FRESH = 45            # a beacon older than this is a node we can't see
LEADER_LOST = 60      # no leader seen this long: the best healthy standby takes over
SICK_FOR = 120        # a leader unhealthy this long steps down
HEALTHY_FOR = 60      # healthy this long before claiming from a leader that is still alive (no flapping)
MIN_TENURE = 300      # a leader isn't asked to hand over sooner than this after it took over
YIELD_MAX = 25 * 60   # a leader finishing its run before handing over; past this the claimant takes over anyway
SYNC_MAX = 180        # the claimant waits this long for the leader's last revision to arrive
STARTUP = 20          # after the daemon starts: listen this long before deciding anything
AUTH_SICK = 15 * 60   # a Claude login failure this recent makes the node sick (another one's login may work)
ALARM_AFTER = 15 * 60  # nobody has led this long: the owner hears it on Telegram (once, then every ALARM_AGAIN)
ALARM_AGAIN = 6 * 3600
NET_PROBES = (("api.anthropic.com", 443), ("api.telegram.org", 443))


def now():
    return time.time()


def log(text):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + text, flush=True)


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def node_name():
    try:
        return (LOCAL / "node").read_text().strip()
    except OSError:
        return socket.gethostname().split(".")[0]


def state_rev():
    """The brain state's revision on this disk (mochi-brain bumps it on every save)."""
    s = load_json(STATE_FILE, {}) or {}
    return int(s.get("rev") or 0), str(s.get("rev_node") or "")


def configured():
    return CLUSTER.exists()


def role():
    """For mochi-brain: {"leader": bool, "node", "leader_node", "caps", ...}, or None when HA isn't set up here
    (then the brain runs as it always did). A stale file (the daemon died) reads as follower: two leaders are worse
    than a minute without one, and systemd restarts the daemon."""
    if not configured():
        return None
    r = load_json(ROLE_FILE)
    if not isinstance(r, dict) or now() - float(r.get("at") or 0) > 3 * FRESH or not pid_alive(r.get("pid")):
        return {"leader": False, "node": node_name(), "leader_node": "", "why": "mochi-ha isn't running",
                "caps": [], "nodes": {}}
    return r


def auth_failed(text):
    """For mochi-brain: a run failed on the Claude login itself."""
    write_json(AUTH_FILE, {"at": now(), "error": str(text)[:200]})


AUTH_ERRORS = ("invalid api key", "please run /login", "oauth token has expired", "authentication_error",
               "invalid bearer token", "not logged in", "credit balance is too low")


def is_auth_error(text):
    t = str(text).lower()
    return any(e in t for e in AUTH_ERRORS)


def pid_alive(pid):
    """The daemon that wrote the role file is still running (a file left by a killed one says nothing)."""
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (TypeError, ValueError, OSError):
        return False


def ack(role_, rev):
    write_json(ACK_FILE, {"role": role_, "rev": rev, "at": now(), "pid": os.getpid()})


# ---- what this node can do ---------------------------------------------------------------------------------------

def tcp_ok(host, port, timeout=4):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def unit_active(unit):
    try:
        return subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit], timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def claude_auth():
    if (HOME / ".claude/.credentials.json").exists():
        return "login"
    tok = CONF / "mochi" / "claude-oauth.token"
    try:
        if tok.read_text().strip():
            return "token"
    except OSError:
        pass
    return ""


def capabilities(seat=False):
    """What a run on this node can reach, for the brain's context and allow-lists. Cheap checks only. `seat`: the
    machine he sits at (ha.json); another node's screen (sff has one) isn't him."""
    caps = []
    if (RUNTIME / "xpet.sock").exists():
        caps.append("pig")
    if (RUNTIME / "mochi-vault.sock").exists():
        caps.append("vault")
    if seat and os.environ.get("DISPLAY"):
        caps.append("screen")
    if Path("/dev/video0").exists():
        caps.append("webcam")
    if (WORK / "chrome").is_dir() and any(shutil.which(c) for c in ("google-chrome", "google-chrome-stable", "chromium")):
        caps.append("chrome")
    if tcp_ok("127.0.0.1", 9334, 1):
        caps.append("peppy-chrome")
    if tcp_ok("storage.local", 445, 2):
        caps.append("nas")
    if tcp_ok("sff.local", 3000, 2):
        caps.append("tg-archive")
    if tcp_ok("peppy.local", 22, 2):
        caps.append("box")
    if tcp_ok("kitchen-clock.local", 22, 2) or tcp_ok("corridor-clock.local", 22, 2):
        caps.append("clocks")
    return caps


class Syncthing:
    """The local Syncthing's REST API, to ask whether a folder is in step."""

    def __init__(self):
        self.key = self.addr = None
        for p in (HOME / ".local/state/syncthing/config.xml", CONF / "syncthing/config.xml"):
            try:
                x = p.read_text()
            except OSError:
                continue
            k = re.search(r"<apikey>([^<]+)</apikey>", x)
            a = re.search(r"<gui[^>]*>.*?<address>([^<]+)</address>", x, re.S)
            if k and a:
                self.key, self.addr = k.group(1), a.group(1)
                break

    def get(self, path):
        if not self.key:
            return None
        req = urllib.request.Request(f"http://{self.addr}{path}", headers={"X-API-Key": self.key})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())
        except (OSError, ValueError):
            return None

    def folder(self, fid):
        """{"state", "need"} of a folder, or None when Syncthing can't be asked."""
        s = self.get(f"/rest/db/status?folder={fid}")
        if not s:
            return None
        return {"state": s.get("state"), "need": int(s.get("needFiles") or 0) + int(s.get("needDeletes") or 0),
                "errors": int(s.get("errors") or 0) + int(s.get("pullErrors") or 0)}

    def peers(self):
        c = self.get("/rest/system/connections")
        return sum(1 for v in ((c or {}).get("connections") or {}).values() if v.get("connected")) if c else 0

    def conflicts(self):
        """Files Syncthing kept twice because two nodes changed them at once (name.sync-conflict-DATE-ID.ext)."""
        out = []
        for root in (WORK / "memory", WORK / "journal", WORK):
            try:
                out += [str(p.relative_to(WORK)) for p in root.glob("*.sync-conflict-*")]
            except OSError:
                pass
        return sorted(set(out))


# ---- the daemon --------------------------------------------------------------------------------------------------

class HA:
    def __init__(self):
        self.cfg = load_json(CLUSTER) or {}
        self.me = node_name()
        nodes = self.cfg.get("nodes") or {}
        if self.me not in nodes:
            sys.exit(f"mochi-ha: this node ({self.me}) isn't in {CLUSTER}")
        self.prio = int(nodes[self.me]["prio"])
        self.seat = bool(nodes[self.me].get("seat"))  # he sits at this one: its screen and input say whether he's here
        self.local = load_json(LOCAL / "state.json", {}) or {}
        self.term = int(self.local.get("term") or 0)
        self.leader = False
        self.lead_since = 0
        self.claiming = 0       # when this node started claiming (0: not)
        self.yielding = 0       # when this leader started handing over (0: not)
        self.yielded = None     # {"term", "rev", "at"}: what the last handover left behind
        self.healthy = False
        self.why = "starting"
        self.healthy_since = 0
        self.sick_since = 0
        self.net_fail = 0
        self.started = now()
        self.last_tick = now()
        self.last_leader_seen = now()  # pretend one was just seen: give the others STARTUP+LEADER_LOST to show up
        self.view = {}          # node -> {"b": beacon, "age": s, "via": "lan"|"relay"}
        self.caps = []
        self.caps_at = 0
        self.st = Syncthing()
        self.avahi = None
        self.relay_trouble = ""
        self.lock = threading.Lock()

    # -- the beacon

    def beacon(self):
        rev, _ = state_rev()
        hold = load_json(LOCAL / "hold.json")
        return {"node": self.me, "prio": self.prio, "healthy": self.healthy, "why": self.why, "leader": self.leader,
                "term": self.term, "claim": bool(self.claiming), "yielding": bool(self.yielding),
                "yielded": self.yielded, "rev": rev, "since": self.lead_since, "hold": bool(hold),
                "caps": self.caps, "t": now(), "v": 1}

    def publish_and_gather(self):
        mine = self.beacon()
        seen = {}
        relay, token = self.cfg.get("relay"), self.cfg.get("token")
        if relay and token:
            try:
                body = json.dumps(mine).encode()
                req = urllib.request.Request(f"{relay}/v1/beacon/{self.me}", data=body, method="PUT",
                                             headers={"Authorization": f"Bearer {token}",
                                                      "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    rnow = float(json.loads(r.read()).get("now") or now())
                req = urllib.request.Request(f"{relay}/v1/beacons", headers={"Authorization": f"Bearer {token}"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    for n, e in json.loads(r.read()).items():
                        if n != self.me and isinstance(e, dict) and isinstance(e.get("beacon"), dict):
                            seen[n] = {"b": e["beacon"], "age": max(0.0, rnow - float(e["at"])), "via": "relay"}
            except (OSError, ValueError) as e:
                self.relay_trouble = str(e)[:80]
            else:
                self.relay_trouble = ""
        for n, spec in (self.cfg.get("nodes") or {}).items():
            host = spec.get("lan")
            if n == self.me or not host:
                continue
            try:
                req = urllib.request.Request(f"http://{host}:{PORT}/beacon", headers={"Authorization": f"Bearer {token}"})
                with urllib.request.urlopen(req, timeout=3) as r:
                    b = json.loads(r.read())
                if isinstance(b, dict) and b.get("node") == n:
                    seen[n] = {"b": b, "age": 0.0, "via": "lan"}
            except (OSError, ValueError):
                pass
        with self.lock:
            self.view = {n: v for n, v in seen.items() if v["age"] <= FRESH}

    def serve_lan(self):
        ha = self
        token = self.cfg.get("token") or ""

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path != "/beacon" or self.headers.get("Authorization", "") != f"Bearer {token}":
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps(ha.beacon()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        if not (self.cfg.get("nodes") or {}).get(self.me, {}).get("lan"):
            return  # the cloud node: nothing on the LAN to talk to
        try:
            srv = ThreadingHTTPServer(("", PORT), H)
        except OSError as e:
            log(f"lan beacon: can't listen on {PORT}: {e}")
            return
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    # -- health

    def check_health(self):
        why = []
        if load_json(LOCAL / "hold.json"):
            h = load_json(LOCAL / "hold.json") or {}
            if h.get("until") and now() > h["until"]:
                (LOCAL / "hold.json").unlink(missing_ok=True)
            else:
                why.append("on hold: " + str(h.get("why") or "by hand"))
        if all(tcp_ok(h, p) for h, p in NET_PROBES):
            self.net_fail = 0
        else:
            self.net_fail += 1
            if self.net_fail >= 2:  # one lost probe is a blip
                why.append("no internet (Claude or Telegram unreachable)")
        if not claude_auth():
            why.append("no Claude login here (~/.config/mochi/claude-oauth.token)")
        a = load_json(AUTH_FILE) or {}
        if now() - float(a.get("at") or 0) < AUTH_SICK:
            why.append("Claude login failing: " + str(a.get("error") or "")[:80])
        if not Path(HOME / ".local/bin/mochi-brain").exists():
            why.append("mochi-brain isn't installed")
        for path in (WORK, CONF / "mochi", HOME / ".claude/projects/-home-n--local-share-mochi"):
            marker = path / ".stfolder"
            if path.is_dir() and not marker.exists():  # Claude Code tidies empty folders away; Syncthing then stops
                marker.mkdir()
                (marker / "keep.txt").write_text("Syncthing folder marker: keep (an empty dir here gets tidied away)\n")
                log(f"put back the Syncthing marker in {path}")
        f = self.st.folder("mochi-work")
        if f is None:
            why.append("Syncthing isn't answering")
        elif f["state"] in ("error", "unknown") or f["errors"] > 20:
            why.append(f"workspace sync is broken ({f['state']}, {f['errors']} errors)")
        healthy = not why
        if healthy and not self.healthy:
            self.healthy_since = now()
            self.sick_since = 0
        if not healthy and self.healthy:
            self.sick_since = now()
        if not healthy and not self.sick_since:
            self.sick_since = now()
        self.healthy, self.why = healthy, "; ".join(why) or "ok"
        if now() - self.caps_at > 60:
            self.caps, self.caps_at = capabilities(self.seat), now()

    # -- deciding

    def best(self):
        """The healthy node that should lead, by priority (then name, so every node agrees)."""
        cands = [(self.prio, self.me)] if self.healthy else []
        cands += [(int(v["b"].get("prio") or 99), n) for n, v in self.view.items()
                  if v["b"].get("healthy") and not v["b"].get("hold")]
        return min(cands)[1] if cands else None

    def leaders(self):
        return {n: v["b"] for n, v in self.view.items() if v["b"].get("leader")}

    def max_term(self):
        return max([self.term] + [int(v["b"].get("term") or 0) for v in self.view.values()])

    def tick(self):
        gap = now() - self.last_tick
        self.last_tick = now()
        self.check_health()
        self.publish_and_gather()
        others = self.leaders()
        if others:
            self.last_leader_seen = now()
        if self.leader:
            self.as_leader(others, gap)
        else:
            self.as_standby(others)
        self.write_role()
        self.alarm(bool(others) or self.leader)

    def alarm(self, someone_leads):
        """Nobody leads for ALARM_AFTER: the alive node that comes first tells the owner on Telegram, through the bot
        (a send, not a poll: it can't disturb a leader that is still polling somewhere)."""
        if someone_leads:
            if self.local.get("alarmed"):
                self.telegram("I'm back, running on " + (self.me if self.leader else
                                                           next(iter(self.leaders()), "another machine")) + ".")
                self.local.pop("alarmed")
                write_json(LOCAL / "state.json", self.local)
            self.no_leader_since = 0
            return
        self.no_leader_since = getattr(self, "no_leader_since", 0) or now()
        alive = [(self.prio, self.me)] + [(int(v["b"].get("prio") or 99), n) for n, v in self.view.items()]
        if min(alive)[1] != self.me or now() - self.no_leader_since < ALARM_AFTER:
            return
        if self.relay_trouble:  # without the relay this node sees only itself (and the LAN): not enough to cry wolf
            return
        if now() - float(self.local.get("alarmed") or 0) < ALARM_AGAIN:
            return
        lines = [f"{self.me}: {self.why}"] + [f"{n}: {v['b'].get('why')}" for n, v in sorted(self.view.items())]
        missing = [n for n in (self.cfg.get("nodes") or {}) if n != self.me and n not in self.view]
        lines += [f"{n}: can't be seen (off?)" for n in missing]
        if self.telegram("I can't think anywhere right now, none of my machines is fit to lead:\n" + "\n".join(lines)):
            self.local["alarmed"] = now()
            write_json(LOCAL / "state.json", self.local)

    def telegram(self, text):
        try:
            token = (CONF / "mochi" / "telegram.token").read_text().strip()
            owner = (load_json(CONF / "mochi" / "sources.json", {}) or {}).get("telegram", {}).get("owner_id")
            if not token or not owner:
                return False
            body = json.dumps({"chat_id": owner, "text": text}).encode()
            req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                ok = json.loads(r.read()).get("ok")
            log(f"told the owner on Telegram: {text.splitlines()[0][:80]}")
            return bool(ok)
        except (OSError, ValueError) as e:
            log(f"telegram alarm failed: {e}")
            return False

    def as_leader(self, others, gap):
        if gap > LEADER_LOST:  # this machine slept; someone may have taken over meanwhile
            return self.step_down(f"this node was asleep {gap:.0f}s", now_=True)
        newer = {n: b for n, b in others.items() if int(b.get("term") or 0) > self.term}
        if newer:
            return self.step_down(f"{', '.join(newer)} leads with a newer term", now_=True)
        same = {n: b for n, b in others.items() if int(b.get("term") or 0) == self.term}
        for n, b in same.items():  # two leaders on one term (a partition healed): the better one stays
            if (int(b.get("prio") or 99), n) < (self.prio, self.me):
                return self.step_down(f"{n} leads too and comes first", now_=True)
        if load_json(LOCAL / "hold.json"):
            return self.step_down("on hold")
        if not self.healthy and now() - self.sick_since > SICK_FOR:
            return self.step_down(self.why)
        claim = [n for n, v in self.view.items() if v["b"].get("claim") and v["b"].get("healthy")
                 and (int(v["b"].get("prio") or 99), n) < (self.prio, self.me)]
        if self.yielding and not claim and self.healthy and not self.brain_acked():
            log("handover called off: nobody is claiming any more")
            self.yielding = 0
            return
        if claim and (now() - self.lead_since > MIN_TENURE or not self.healthy) or self.yielding:
            self.step_down(f"{claim[0] if claim else 'a better node'} is back")

    def brain_acked(self):
        a = load_json(ACK_FILE, {}) or {}
        return a.get("role") == "follower" and float(a.get("at") or 0) >= self.yielding

    def step_down(self, why, now_=False):
        """Hand over: the brain finishes its run and saves (it acks with the revision it left), then the leader's
        services stop and the beacon says what was left behind. `now_`: another node already leads; just stop."""
        if not self.yielding:
            self.yielding = now()
            log(f"stepping down: {why}")
            self.write_role()
        a = load_json(ACK_FILE, {}) or {}
        brain_done = self.brain_acked() or not unit_active("mochi-brain")
        if not (brain_done or now_ or now() - self.yielding > YIELD_MAX):
            return
        rev = int(a.get("rev") or 0) if brain_done and a.get("rev") else state_rev()[0]
        self.leader = False
        self.yielded = {"term": self.term, "rev": rev, "at": now()}
        self.yielding = 0
        self.lead_since = 0
        self.local["leading"] = False
        write_json(LOCAL / "state.json", self.local)
        self.leader_services(False)
        log(f"follower now (left revision {rev})")

    def as_standby(self, others):
        best = self.best()
        hold = load_json(LOCAL / "hold.json")
        if best != self.me or hold or now() - self.started < STARTUP:
            if self.claiming:
                log("no longer claiming")
            self.claiming = 0
            return
        if self.local.get("leading") and not others:  # this daemon restarted while leading, and nobody took over
            return self.take_over("resumed after a restart", None)
        if others:
            # a worse node leads: claim, once healthy long enough; take over when it has handed over
            if now() - self.healthy_since < HEALTHY_FOR:
                return
            if not self.claiming:
                self.claiming = now()
                log(f"claiming from {', '.join(others)}")
            left = [b.get("yielded") for b in others.values() if b.get("yielded")]
            if now() - self.claiming > YIELD_MAX + 60:
                return self.take_over(f"{', '.join(others)} didn't hand over in {YIELD_MAX // 60} min", None)
            return  # still leading there; its beacon flips to leader=false with "yielded" when it's done
        recent = [v["b"]["yielded"] for v in self.view.values()
                  if v["b"].get("yielded") and now() - float(v["b"]["yielded"].get("at") or 0) < SYNC_MAX + 120]
        if recent:  # the leader just handed over (to us): wait for its last state to arrive
            want = max(int(y.get("rev") or 0) for y in recent)
            have, _ = state_rev()
            waited = now() - max(float(y.get("at") or 0) for y in recent)
            if have < want and waited < SYNC_MAX:
                return
            return self.take_over("handed over", want)
        if now() - self.last_leader_seen < LEADER_LOST:
            return
        f = self.st.folder("mochi-work")
        if f and f["need"] and self.st.peers() and now() - self.last_leader_seen < LEADER_LOST + SYNC_MAX:
            return  # Syncthing is still bringing something in from a peer: let it land first
        self.take_over(f"no leader seen for {now() - self.last_leader_seen:.0f}s", None)

    def take_over(self, why, rev):
        self.term = self.max_term() + 1
        self.leader = True
        self.lead_since = now()
        self.claiming = 0
        self.yielded = None
        self.local["term"] = self.term
        self.local["leading"] = True
        write_json(LOCAL / "state.json", self.local)
        have, by = state_rev()
        log(f"leading now, term {self.term}: {why} (state revision {have} from {by or '?'}"
            + (f", the handover left {rev})" if rev is not None else ")"))
        self.write_role()
        self.leader_services(True)

    # -- acting on it

    def write_role(self):
        nodes = {n: {"healthy": v["b"].get("healthy"), "leader": v["b"].get("leader"), "why": v["b"].get("why"),
                     "via": v["via"], "age": round(v["age"]), "caps": v["b"].get("caps")} for n, v in self.view.items()}
        lead = self.me if self.leader else next((n for n, v in self.view.items() if v["b"].get("leader")), "")
        term = self.term if self.leader else next((int(v["b"].get("term") or 0) for n, v in self.view.items()
                                                   if v["b"].get("leader")), self.term)
        write_json(ROLE_FILE, {"leader": self.leader and not self.yielding, "draining": bool(self.yielding),
                               "node": self.me, "leader_node": lead, "term": term, "since": self.lead_since,
                               "healthy": self.healthy, "why": self.why, "caps": self.caps, "nodes": nodes,
                               "seat": self.seat, "pid": os.getpid(),
                               "at": now()})

    def leader_services(self, on):
        try:
            subprocess.run(["systemctl", "--user", "start" if on else "stop", "mochi-leader.target"], timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"mochi-leader.target: {e}")
        self.mdns(on)

    def mdns(self, on):
        """mochi.local → this node, on the LAN, while it leads (the wall clocks find Mochi by that name)."""
        if self.avahi:
            self.avahi.terminate()
            self.avahi = None
        lan = (self.cfg.get("nodes") or {}).get(self.me, {}).get("lan")
        if not on or not lan:
            return
        try:
            ip = socket.gethostbyname(lan)
            if ip.startswith("127."):
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("192.168.68.1", 9))
                ip = s.getsockname()[0]
                s.close()
            self.avahi = subprocess.Popen(["avahi-publish", "-a", "-R", "mochi.local", ip],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, socket.error) as e:
            log(f"mochi.local not published: {e}")

    def run(self):
        log(f"mochi-ha on {self.me} (priority {self.prio}), term {self.term}"
            + (", led before this start" if self.local.get("leading") else ""))
        self.serve_lan()
        self.leader_services(False)  # whatever was running before a restart: leading is decided afresh
        while True:
            t0 = now()
            try:
                self.tick()
            except Exception as e:
                import traceback
                log("tick failed:\n" + traceback.format_exc())
            time.sleep(max(1.0, TICK - (now() - t0)))


# ---- the command line --------------------------------------------------------------------------------------------

def status():
    r = load_json(ROLE_FILE) or {}
    me = node_name()
    if not configured():
        print(f"HA isn't set up here (no {CLUSTER})")
        return 1
    age = now() - float(r.get("at") or 0)
    print(f"this node: {me}" + (" — LEADER" if r.get("leader") else "")
          + (" (handing over)" if r.get("draining") else "")
          + f"; healthy: {r.get('why', '?')}" + (f"; daemon silent {age:.0f}s!" if age > 30 else ""))
    print(f"leader: {r.get('leader_node') or 'none seen'}, term {r.get('term')}")
    for n, v in sorted((r.get("nodes") or {}).items()):
        print(f"  {n:8} {'LEADER ' if v.get('leader') else '       '}{'ok' if v.get('healthy') else 'sick'} "
              f"({v.get('why')}) via {v.get('via')} {v.get('age')}s ago; can: {', '.join(v.get('caps') or [])}")
    print(f"  here can: {', '.join(r.get('caps') or [])}")
    st = Syncthing()
    for fid in ("mochi-work", "mochi-config", "mochi-claude"):
        f = st.folder(fid)
        print(f"sync {fid}: " + (f"{f['state']}, {f['need']} to fetch, {f['errors']} errors" if f else "Syncthing not answering"))
    c = st.conflicts()
    if c:
        print("sync conflicts (two nodes wrote at once; merge by hand or let Mochi): " + ", ".join(c[:10]))
    rev, by = state_rev()
    print(f"brain state revision {rev} (saved by {by or '?'})")
    hold = load_json(LOCAL / "hold.json")
    if hold:
        print(f"ON HOLD: {hold.get('why')}" + (f" until {time.strftime('%H:%M', time.localtime(hold['until']))}"
                                               if hold.get("until") else ""))
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "serve":
        HA().run()
        return 0
    if cmd == "status":
        return status()
    if cmd == "hold":
        write_json(LOCAL / "hold.json", {"why": " ".join(argv[2:]) or "by hand", "at": now()})
        print("this node won't lead (mochi-ha release to undo); if it leads now it hands over")
        return 0
    if cmd == "release":
        (LOCAL / "hold.json").unlink(missing_ok=True)
        print("released")
        return 0
    if cmd == "prefer":
        if len(argv) < 3:
            print("usage: mochi-ha prefer NODE", file=sys.stderr)
            return 1
        cfg = load_json(CLUSTER) or {}
        nodes = cfg.get("nodes") or {}
        if argv[2] not in nodes:
            print(f"no node {argv[2]} (nodes: {', '.join(nodes)})", file=sys.stderr)
            return 1
        me = node_name()
        if nodes[me]["prio"] < nodes[argv[2]]["prio"]:
            write_json(LOCAL / "hold.json", {"why": f"preferring {argv[2]}", "at": now(), "until": now() + 3600})
            print(f"{me} holds for an hour; run `mochi-ha prefer {argv[2]}` on the other better nodes too, if any")
        else:
            print(f"{me} doesn't come before {argv[2]}; nothing to do here")
        return 0
    print(__doc__)
    return 0 if cmd in ("-h", "--help", "help") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
