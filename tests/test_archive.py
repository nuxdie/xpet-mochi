#!/usr/bin/env python3
"""Tests for mochi-archive (transcripts and comms.jsonl into llm-chats-archive's native format) and the relay's
comms.jsonl lines. Scratch directories only; nothing is sent anywhere."""

import importlib.machinery
import importlib.util
import json
import os
import queue
import tempfile
import time
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="mochi-archive-test-"))
for var in ("XDG_DATA_HOME", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME"):
    os.environ.setdefault(var, str(TMP))


def load(name, file):
    src = Path(__file__).resolve().parent.parent / "brain" / file
    spec = importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, str(src)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ma = load("mochi_archive", "mochi_archive.py")
mb = load("mochi_brain_for_archive", "mochi_brain.py")

ma.PROJECTS = TMP / "projects"
ma.COMMS = mb.COMMS_FILE
MOCHI_DIR = ma.PROJECTS / ma.MOCHI_PROJECT


def transcript(folder, sid, first, entrypoint="cli", extra=(), reply="Done.\n\n```json\n{\"say\": \"ok\"}\n```"):
    folder.mkdir(parents=True, exist_ok=True)
    base = {"sessionId": sid, "cwd": "/home/n/proj", "gitBranch": "main", "version": "2.1", "entrypoint": entrypoint}
    rows = [
        {**base, "type": "user", "uuid": f"{sid}-u1", "timestamp": "2026-10-05T10:00:00Z",
         "message": {"role": "user", "content": first}},
        {**base, "type": "assistant", "uuid": f"{sid}-a1", "timestamp": "2026-10-05T10:00:05Z",
         "message": {"model": "claude-opus-5-5", "content": [
             {"type": "text", "text": "Looking."},
             {"type": "tool_use", "name": "Bash", "input": {"command": "ls", "description": "List files"}}]}},
        {**base, "type": "user", "uuid": f"{sid}-r1", "timestamp": "2026-10-05T10:00:06Z",
         "message": {"content": [{"type": "tool_result", "content": "a b c"}]}},
        {**base, "type": "assistant", "uuid": f"{sid}-a2", "timestamp": "2026-10-05T10:00:09Z",
         "message": {"content": [{"type": "text", "text": reply}]}},
        *extra,
    ]
    path = folder / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


class UserText(unittest.TestCase):
    def t(self, content, **kw):
        return ma.user_text({"type": "user", "message": {"content": content}, **kw})

    def test_what_counts_as_typed(self):
        self.assertEqual(self.t("hello <system-reminder>x</system-reminder>"), "hello")
        self.assertEqual(self.t("<command-name>/clear</command-name>\n<command-message>clear</command-message>\n"
                                "<command-args></command-args>"), "/clear")
        self.assertEqual(self.t("<bash-input> ls -la</bash-input>"), "! ls -la")
        self.assertEqual(self.t([{"type": "text", "text": "look"}, {"type": "image"}]), "look\n[image]")
        for skipped in ("<local-command-stdout>x</local-command-stdout>", "<bash-stdout>x</bash-stdout>",
                        "<task-notification>x</task-notification>", "[Request interrupted by user]", ""):
            self.assertIsNone(self.t(skipped), skipped)
        self.assertIsNone(self.t([{"type": "tool_result", "content": "x"}]))
        self.assertIsNone(self.t("hi", isMeta=True))
        self.assertIsNone(self.t("hi", isSidechain=True))


class ClaudeCode(unittest.TestCase):
    def test_session_becomes_one_conversation(self):
        title = {"type": "ai-title", "aiTitle": "Fix the build", "sessionId": "cc1"}
        c = ma.claudecode_conversation(transcript(ma.PROJECTS / "-home-n-proj", "cc1", "fix the build",
                                                  extra=[title], reply="Fixed it."))
        self.assertEqual((c["id"], c["title"], c["metadata"]["project"]), ("cc1", "Fix the build", "proj"))
        self.assertEqual([m["role"] for m in c["messages"]], ["user", "tool", "assistant"])
        self.assertEqual(c["messages"][2]["text"], "Looking.\n\nFixed it.")
        self.assertIn("Bash: List files", c["messages"][1]["text"])
        self.assertNotIn("a b c", json.dumps(c))  # tool output never goes along
        self.assertEqual(c["messages"][2]["model"], "claude-opus-5-5")

    def test_scripted_runs_and_mochi_stay_out(self):
        self.assertIsNone(ma.claudecode_conversation(
            transcript(ma.PROJECTS / "-home-n-proj", "cc2", "say hi", entrypoint="sdk-cli")))
        transcript(MOCHI_DIR, "m0", "It's Monday. Do a round, as CLAUDE.md describes.")
        self.assertNotIn(MOCHI_DIR, {f.parent for f in ma.claudecode_files()})


class Mochi(unittest.TestCase):
    def setUp(self):
        for f in MOCHI_DIR.glob("*.jsonl"):
            f.unlink()
        ma.COMMS.unlink(missing_ok=True)

    def comms(self, *lines):
        ma.COMMS.parent.mkdir(parents=True, exist_ok=True)
        with open(ma.COMMS, "a") as f:
            for i, line in enumerate(lines):
                f.write(json.dumps({"t": 1_790_000_000 + i, **line}) + "\n")

    def by_id(self):
        return {c["id"]: c for c in ma.mochi_conversations()}

    def test_kinds_of_session(self):
        transcript(MOCHI_DIR, "round", "It's Monday 2026-10-05 12:00. Do a round, as CLAUDE.md describes (regular).")
        transcript(MOCHI_DIR, "req", "The user asked you, through the pig, to do this:\n\nwater the plants\n\nDo it. Work")
        transcript(MOCHI_DIR, "ans", 'The user answered "Yes, prefill it" to this, which you put on the pig:\n'
                                     '  BeFrank needs the form\nThe options you gave them: Yes, prefill it, Never\n'
                                     'Your notes to yourself about it: -\n\nAct on their answer now.')
        transcript(MOCHI_DIR, "chat", "The user opened a chat with you from the pig's menu. You're in your workspace",
                   extra=[{"type": "user", "uuid": "chat-u2", "sessionId": "chat", "timestamp": "2026-10-05T10:01:00Z",
                           "message": {"content": "what's new?"}}])
        cs = self.by_id()
        self.assertEqual(set(cs), {"session:req", "session:ans", "session:chat"})
        req = cs["session:req"]["messages"]
        self.assertEqual((req[0]["role"], req[0]["text"]), ("user", "water the plants"))
        self.assertEqual(req[-1]["text"], "Done.")  # the mochi block is stripped
        ans = cs["session:ans"]["messages"]
        self.assertEqual([m["role"] for m in ans], ["assistant", "user", "tool", "assistant"])
        self.assertIn("_Options: Yes, prefill it, Never_", ans[0]["text"])
        self.assertEqual(ans[1]["text"], "Yes, prefill it")
        self.assertEqual(cs["session:chat"]["title"], "what's new?")
        self.assertEqual(cs["session:chat"]["messages"][0]["role"], "system")

    def test_asks_from_comms_become_threads(self):
        transcript(MOCHI_DIR, "ans2", 'The user answered "Patch" to this, which you put on the pig:\n  patch it?\n'
                                      'The options you gave them: Patch, Never\n')
        self.comms(
            {"kind": "ask", "id": "p1", "text": "patch it?", "options": ["Patch", "Never"], "src": "round"},
            {"kind": "answer", "id": "p1", "label": "Patch", "via": "telegram"},
            {"kind": "done", "task": "approved", "session": "ans2", "ask": "p1", "via": "telegram", "ok": True},
            {"kind": "ask", "id": "r1", "text": "done: patched", "src": "result"},
            {"kind": "ask", "id": "x", "text": "move the podcast?", "src": "round"},
            {"kind": "closed", "id": "x", "how": "expired"},
            {"kind": "ask", "id": "x", "text": "move the podcast now?", "src": "round"},
        )
        cs = self.by_id()
        self.assertNotIn("session:ans2", cs)     # merged into its ask's thread
        self.assertNotIn("ask:r1", cs)           # a result note is the request's own answer
        p1 = cs["ask:p1"]["messages"]
        self.assertEqual([m["role"] for m in p1], ["assistant", "user", "tool", "assistant"])
        self.assertEqual(p1[1]["metadata"], {"via": "telegram"})
        self.assertEqual(p1[-1]["id"], "ans2:reply")
        self.assertEqual(cs["ask:x"]["messages"][-1]["text"], "Closed: expired.")
        again = [c for i, c in cs.items() if i.startswith("ask:x~")]
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["title"], "move the podcast now?")


class Anything:
    """Swallows any call: the pig, the phone."""

    def __getattr__(self, name):
        return lambda *a, **k: None


class RelayComms(unittest.TestCase):
    def test_relay_lines_feed_the_archive(self):
        mb.COMMS_FILE.unlink(missing_ok=True)
        r = type("R", (), {})()
        r.state, r.pet, r.tg, r.tasks = {"asks": {}, "events": []}, Anything(), Anything(), queue.Queue()
        r.away, r.event, r.save = (lambda: False), (lambda text: None), (lambda: None)
        mb.Relay.add_ask(r, "renew the domain?", ["Yes, do it", "Not now"], aid="dom", src="round")
        mb.Relay.on_answer(r, "dom", "Yes, do it", via="pig")
        task = r.tasks.get_nowait()
        self.assertEqual(task["ask"], "dom")
        lines = [json.loads(line) for line in mb.COMMS_FILE.read_text().splitlines()]
        self.assertEqual([x["kind"] for x in lines], ["ask", "answer"])
        self.assertEqual(lines[0]["options"], ["Yes, do it", "Not now", "Chat about it"])
        self.assertLessEqual(abs(lines[1]["t"] - time.time()), 5)


if __name__ == "__main__":
    unittest.main()
