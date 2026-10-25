"""mochi-ha's choice of leader, with three simulated nodes on a fake clock and a fake network (no Syncthing, no
relay, no systemd)."""
import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_loader("mochi_ha", importlib.machinery.SourceFileLoader(
    "mochi_ha", str(HERE / "brain/mochi_ha.py")))
HA = importlib.util.module_from_spec(spec)
spec.loader.exec_module(HA)

CFG = {"relay": "", "token": "t", "nodes": {"desk": {"prio": 1, "lan": "desk.local", "seat": True},
                                            "sff": {"prio": 2, "lan": "sff.local"}, "cloud": {"prio": 3}}}


class Clock:
    t = 1_000_000.0


class FakeSt:
    def __init__(self, node):
        self.node = node

    def folder(self, fid):
        return {"state": "idle", "need": 0, "errors": 0}

    def peers(self):
        return 1

    def conflicts(self):
        return []


class Node:
    """One machine: its own directories, health, brain (acks a handover after `busy` seconds) and state file."""

    def __init__(self, sim, name):
        self.sim, self.name = sim, name
        self.dir = Path(sim.tmp.name) / name
        for d in ("local", "run", "work"):
            (self.dir / d).mkdir(parents=True, exist_ok=True)
        self.up = True        # powered and running
        self.net = True       # can reach the internet and the relay
        self.lan = True       # can reach the home LAN (desk/sff)
        self.login = True     # has a working Claude login
        self.busy_until = 0   # the brain is finishing a run until then
        self.rev = 0
        self.services = None
        self.ha = None
        self.boot()

    def paths(self):
        HA.LOCAL = self.dir / "local"
        HA.ROLE_FILE = self.dir / "run/mochi-ha.json"
        HA.ACK_FILE = self.dir / "run/mochi-brain.role"
        HA.STATE_FILE = self.dir / "work/relay.json"

    def boot(self):
        self.paths()
        (self.dir / "local/node").write_text(self.name)
        old = HA.load_json
        HA.load_json = lambda p, d=None: CFG if Path(p) == HA.CLUSTER else old(p, d)
        try:
            self.ha = HA.HA()
        finally:
            HA.load_json = old
        self.ha.cfg = CFG
        self.ha.st = FakeSt(self.name)
        node = self

        def health():
            self_ = node.ha
            healthy = node.net and node.login and not HA.load_json(HA.LOCAL / "hold.json")
            if healthy and not self_.healthy:
                self_.healthy_since = HA.now()
                self_.sick_since = 0
            if not healthy and (self_.healthy or not self_.sick_since):
                self_.sick_since = HA.now()
            self_.healthy, self_.why = healthy, "ok" if healthy else ("no internet" if not node.net else "no login")

        self.ha.check_health = health
        self.ha.publish_and_gather = lambda: self.sim.gather(self)
        self.ha.leader_services = lambda on: setattr(self, "services", on)
        self.ha.mdns = lambda on: None

    def write_state(self, rev):
        self.paths()
        HA.STATE_FILE.write_text(json.dumps({"rev": rev, "rev_node": self.name}))

    def brain(self):
        """The brain's side of a handover: when the role says draining and the run is done, save and ack."""
        self.paths()
        r = HA.load_json(HA.ROLE_FILE, {}) or {}
        if r.get("draining") and HA.now() >= self.busy_until:
            a = HA.load_json(HA.ACK_FILE, {}) or {}
            if a.get("role") != "follower" or a.get("at", 0) < self.ha.yielding:
                self.rev += 1
                self.write_state(self.rev)
                HA.write_json(HA.ACK_FILE, {"role": "follower", "rev": self.rev, "at": HA.now()})
                self.sim.sync(self)  # Syncthing carries the last save
        if r.get("leader"):
            self.rev += 1  # the leader saves now and then
            self.write_state(self.rev)
            self.sim.sync(self)

    def tick(self):
        if not self.up:
            return
        self.paths()
        HA.unit_active = lambda unit: True
        self.ha.tick()
        self.brain()


class Sim:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_now = HA.now
        HA.now = lambda: Clock.t
        self.relay = {}  # node -> (at, beacon)
        self.relay_up = True
        self.nodes = {n: Node(self, n) for n in ("desk", "sff", "cloud")}

    def close(self):
        HA.now = self.saved_now
        self.tmp.cleanup()

    def gather(self, node):
        mine = node.ha.beacon()
        seen = {}
        node.ha.relay_trouble = "" if node.net and self.relay_up else "unreachable"
        if node.net and self.relay_up:
            self.relay[node.name] = (Clock.t, mine)
            for n, (at, b) in self.relay.items():
                if n != node.name:
                    seen[n] = {"b": b, "age": Clock.t - at, "via": "relay"}
        if node.lan and CFG["nodes"][node.name].get("lan"):
            for n, other in self.nodes.items():
                if n != node.name and other.up and other.lan and CFG["nodes"][n].get("lan"):
                    seen[n] = {"b": other.ha.beacon(), "age": 0.0, "via": "lan"}
        node.ha.set_view(seen)

    def sync(self, src):
        """Syncthing: the leader's state reaches every node it can reach, at once."""
        for n, other in self.nodes.items():
            if other is src or not other.up:
                continue
            both_lan = src.lan and other.lan and CFG["nodes"][n].get("lan") and CFG["nodes"][src.name].get("lan")
            if both_lan or (src.net and other.net):
                other.write_state(src.rev)
                other.rev = max(other.rev, src.rev)

    def run(self, seconds, step=HA.TICK):
        end = Clock.t + seconds
        while Clock.t < end:
            Clock.t += step
            for n in self.nodes.values():
                n.tick()

    def leaders(self):
        return sorted(n for n, node in self.nodes.items() if node.up and node.ha.leader and not node.ha.yielding)

    def power(self, name, on):
        node = self.nodes[name]
        node.up = on
        if on:  # a fresh boot: a new daemon, the same disk
            node.boot()
        else:
            self.relay.pop(name, None) if False else None  # the relay keeps the old beacon until it goes stale


class Election(unittest.TestCase):
    def setUp(self):
        Clock.t = 1_000_000.0
        self.sim = Sim()

    def tearDown(self):
        self.sim.close()

    def test_cold_start_desk_leads_alone(self):
        self.sim.run(200)
        self.assertEqual(self.sim.leaders(), ["desk"])
        self.assertTrue(self.sim.nodes["desk"].services)

    def test_desk_off_sff_takes_over_not_the_cloud(self):
        self.sim.run(200)
        self.sim.power("desk", False)
        self.sim.run(30)
        self.assertEqual(self.sim.leaders(), [], "nobody leads within the grace time")
        self.sim.run(120)
        self.assertEqual(self.sim.leaders(), ["sff"])
        self.sim.run(600)
        self.assertEqual(self.sim.leaders(), ["sff"])

    def test_desk_back_claims_and_sff_hands_over_after_its_run(self):
        self.sim.run(200)
        self.sim.power("desk", False)
        self.sim.run(500)
        self.assertEqual(self.sim.leaders(), ["sff"])
        sff = self.sim.nodes["sff"]
        sff.busy_until = Clock.t + 400  # a run in progress on sff
        self.sim.power("desk", True)
        self.sim.run(300)
        self.assertEqual(self.sim.leaders(), [], "sff is finishing its run; desk waits")
        self.assertTrue(sff.ha.yielding)
        self.sim.run(300)
        self.assertEqual(self.sim.leaders(), ["desk"])
        self.assertFalse(sff.services)
        self.assertGreaterEqual(self.sim.nodes["desk"].rev, sff.rev, "desk took over with sff's last state")

    def test_home_blackout_cloud_leads_then_hands_back(self):
        self.sim.run(200)
        self.sim.power("desk", False)
        self.sim.power("sff", False)
        self.sim.run(200)
        self.assertEqual(self.sim.leaders(), ["cloud"])
        self.sim.power("sff", True)
        self.sim.run(300)
        self.assertEqual(self.sim.leaders(), ["sff"])
        self.sim.power("desk", True)
        self.sim.run(300)
        self.assertEqual(self.sim.leaders(), ["desk"])

    def test_home_internet_down_cloud_leads_desk_steps_down(self):
        self.sim.run(200)
        for n in ("desk", "sff"):
            self.sim.nodes[n].net = False
        self.sim.run(400)
        self.assertEqual(self.sim.leaders(), ["cloud"])
        for n in ("desk", "sff"):
            self.sim.nodes[n].net = True
        self.sim.run(400)
        self.assertEqual(self.sim.leaders(), ["desk"])

    def test_two_leaders_on_one_term_the_better_stays(self):
        self.sim.run(200)
        sff = self.sim.nodes["sff"]
        sff.ha.term = self.sim.nodes["desk"].ha.term
        sff.ha.leader = True
        sff.ha.lead_since = Clock.t
        self.sim.run(20)
        self.assertEqual(self.sim.leaders(), ["desk"])

    def test_leader_that_slept_steps_down(self):
        self.sim.run(200)
        desk = self.sim.nodes["desk"]
        desk.up = False  # suspended: no ticks, the beacon goes stale
        self.sim.run(200)
        self.assertEqual(self.sim.leaders(), ["sff"])
        desk.up = True   # wakes with leader=True in memory
        Clock.t += HA.TICK
        desk.tick()
        self.assertFalse(desk.ha.leader, "a leader back from sleep stops at once")
        self.sim.run(400)
        self.assertEqual(self.sim.leaders(), ["desk"])

    def test_daemon_restart_while_leading_resumes(self):
        self.sim.run(200)
        term = self.sim.nodes["desk"].ha.term
        self.sim.nodes["desk"].boot()
        self.sim.run(HA.STARTUP + 10)
        self.assertEqual(self.sim.leaders(), ["desk"])
        self.assertEqual(self.sim.nodes["desk"].ha.term, term + 1)

    def test_hold_hands_over(self):
        self.sim.run(200)
        desk = self.sim.nodes["desk"]
        desk.paths()
        HA.write_json(HA.LOCAL / "hold.json", {"why": "gaming"})
        self.sim.run(400)
        self.assertEqual(self.sim.leaders(), ["sff"])
        desk.paths()
        (HA.LOCAL / "hold.json").unlink()
        self.sim.run(400)
        self.assertEqual(self.sim.leaders(), ["desk"])

    def test_nobody_fit_tells_the_owner_once(self):
        told = []
        for n in self.sim.nodes.values():
            n.ha.telegram = lambda text, name=n.name: told.append((name, text)) or True
        self.sim.run(200)
        for n in self.sim.nodes.values():
            n.login = False  # every node sick (the token expired), the network fine
        self.sim.run(HA.ALARM_AFTER + 300)
        self.assertEqual(self.sim.leaders(), [])
        self.assertEqual([n for n, _ in told], ["desk"], "the first alive node says it, once")
        self.assertIn("sff: no login", told[0][1])
        for n in self.sim.nodes.values():
            n.login = True
        self.sim.run(300)
        self.assertEqual(self.sim.leaders(), ["desk"])
        self.assertEqual(told[-1][0], "desk")
        self.assertIn("back", told[-1][1])

    def test_never_two_leaders(self):
        """Through a messy afternoon, at no tick do two nodes lead."""
        events = [(200, "desk", False), (300, "sff", False), (300, "sff", True), (200, "desk", True),
                  (100, "sff", False), (100, "desk", False), (300, "desk", True), (300, "sff", True)]
        self.sim.run(200)
        for secs, name, on in events:
            self.sim.power(name, on)
            end = Clock.t + secs
            while Clock.t < end:
                self.sim.run(HA.TICK)
                self.assertLessEqual(len(self.sim.leaders()), 1, f"at {Clock.t}: {self.sim.leaders()}")
        self.sim.run(400)
        self.assertEqual(self.sim.leaders(), ["desk"])


class WindowsSeat(unittest.TestCase):
    def test_presence_not_a_node(self):
        Clock.t = 1_000_000.0
        sim = Sim()
        try:
            sim.run(200)
            sim.power("desk", False)  # booted into Windows
            sim.run(150)
            sim.relay["windows"] = (Clock.t, {"node": "windows", "seat_only": True, "healthy": False, "prio": 99,
                                              "idle": 4, "app": "ELDEN RING", "fullscreen": True})
            sim.run(10)
            self.assertEqual(sim.leaders(), ["sff"])
            sff = sim.nodes["sff"]
            sff.paths()
            role = HA.load_json(HA.ROLE_FILE)
            self.assertNotIn("windows", role["nodes"])
            self.assertEqual(role["windows"]["app"], "ELDEN RING")
            self.assertLess(role["windows"]["idle"], 30)
            sim.run(200)  # the agent stopped (Windows shut down): stale
            sff.paths()
            self.assertIsNone(HA.load_json(HA.ROLE_FILE)["windows"])
        finally:
            sim.close()


class TokenReminder(unittest.TestCase):
    def test_a_month_ahead_then_weekly_until_replaced(self):
        import os
        Clock.t = 1_000_000.0
        sim = Sim()
        try:
            desk = sim.nodes["desk"]
            told = []
            desk.ha.telegram = lambda text: told.append(text) or True
            with tempfile.TemporaryDirectory() as d:
                old = HA.CONF
                HA.CONF = Path(d)
                (Path(d) / "mochi").mkdir()
                tok = Path(d) / "mochi/claude-oauth.token"
                tok.write_text("x")
                os.utime(tok, (Clock.t - 300 * 86400, Clock.t - 300 * 86400))
                try:
                    desk.paths()
                    desk.ha.token_reminder()
                    self.assertEqual(told, [], "65 days left: quiet")
                    Clock.t += 40 * 86400
                    desk.ha.token_reminder()
                    desk.ha.token_reminder()
                    self.assertEqual(len(told), 1)
                    self.assertIn("25 days", told[0])
                    Clock.t += 8 * 86400
                    desk.ha.token_reminder()
                    self.assertEqual(len(told), 2, "again a week later")
                    os.utime(tok, (Clock.t, Clock.t))  # replaced
                    Clock.t += 86400
                    desk.ha.token_reminder()
                    self.assertEqual(len(told), 2)
                finally:
                    HA.CONF = old
        finally:
            sim.close()


class Watchdog(unittest.TestCase):
    def test_only_the_leader_pings(self):
        Clock.t = 1_000_000.0
        sim = Sim()
        pinged = []
        try:
            for n in sim.nodes.values():
                n.ha.ping_watchdog = lambda name=n.name: pinged.append(name)
            old = HA.threading.Thread
            HA.threading.Thread = lambda target, daemon: type("T", (), {"start": lambda s: target()})()
            try:
                sim.run(300)
            finally:
                HA.threading.Thread = old
        finally:
            sim.close()
        self.assertEqual(set(pinged), {"desk"})
        self.assertLessEqual(len(pinged), 300 // HA.PING_EVERY + 1)


class RoleFile(unittest.TestCase):
    def test_a_killed_daemons_leader_file_is_not_believed(self):
        with tempfile.TemporaryDirectory() as d:
            old = HA.ROLE_FILE, HA.CLUSTER
            HA.ROLE_FILE, HA.CLUSTER = Path(d) / "role.json", Path(d) / "ha.json"
            HA.CLUSTER.write_text("{}")
            try:
                HA.write_json(HA.ROLE_FILE, {"leader": True, "at": HA.now(), "pid": 2 ** 22 + 12345})
                self.assertFalse(HA.role()["leader"])
                HA.write_json(HA.ROLE_FILE, {"leader": True, "at": HA.now(), "pid": __import__("os").getpid()})
                self.assertTrue(HA.role()["leader"])
            finally:
                HA.ROLE_FILE, HA.CLUSTER = old


if __name__ == "__main__":
    unittest.main()
