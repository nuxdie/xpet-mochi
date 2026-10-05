#!/usr/bin/env python3
"""mochi-archive — your conversations into your LLM chat archive (llm-chats-archive on sff), as two providers.

    mochi-archive sync [--all] [--dry-run]   send what's new (--all: everything again; --dry-run: just count)
    mochi-archive dump claudecode|mochi      print the payload instead of sending it

"claudecode": your interactive Claude Code sessions (~/.claude/projects), one archive chat per session: your
prompts, Claude's replies, and a one-line trail of the tools each turn used (never their output). `claude -p`
runs (scripts, tests, Mochi's own work) are left out, and so is a session still being written to.

"mochi": what passed between you and Mochi: chats opened from the pig, things you asked it to do (from the pig or
the phone) with its answer, and the questions it put to you with what you picked and what came of it. Its rounds,
discovery runs and dreams are its own work and stay out. Sources: Mochi's transcripts, plus comms.jsonl, which the
relay writes as asks are raised, answered and closed.

The archive takes a re-sent conversation and adds only its new messages, so a session that grows is simply sent
again. Delivery is `python -m backend.app.cli sync NAME` inside the archive's container, over ssh with Mochi's key
(config "llm_archive" -> "sync" in ~/.config/mochi/sources.json); each provider shows as one "sync: NAME" import.
"""

import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
WORK = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "mochi"
PROJECTS = HOME / ".claude" / "projects"
MOCHI_PROJECT = "-" + str(WORK).replace("/", "-").replace(".", "-").lstrip("-")
COMMS = WORK / "comms.jsonl"
STATE = WORK / "archive-sync.json"
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "mochi" / "sources.json"
FORMAT = "llm-chats-archive/conversations-v1"
SETTLE = 20 * 60  # a transcript untouched this long is a finished piece of work (a turn in flight would be cut short)
DEFAULT_SYNC = {"ssh": "n@sff.local", "key": "~/.ssh/mochi_ed25519", "container": "llm-chats-archive-archive-1"}


def config():
    try:
        cfg = json.loads(CONFIG_FILE.read_text()).get("llm_archive", {}).get("sync") or {}
    except (OSError, ValueError):
        cfg = {}
    return {**DEFAULT_SYNC, **cfg}


# ---- reading a Claude Code transcript ----------------------------------------------------------------------------

def records(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                yield r


def user_text(r):
    """What the person typed, or None for everything else that travels as a user record (tool results, command
    output, notices, compaction summaries)."""
    if r.get("isMeta") or r.get("isCompactSummary") or r.get("isSidechain"):
        return None
    c = (r.get("message") or {}).get("content")
    if isinstance(c, str):
        parts = [c]
    elif isinstance(c, list):
        parts = []
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                parts.append(b.get("text") or "")
            elif b.get("type") == "image":
                parts.append("[image]")
            elif b.get("type") == "tool_result":
                return None
    else:
        return None
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", "\n".join(parts), flags=re.S).strip()
    m = re.match(r"<command-name>(.*?)</command-name>.*?(?:<command-args>(.*?)</command-args>)?", text, re.S)
    if m:
        return f"{m.group(1).strip()} {(m.group(2) or '').strip()}".strip()
    m = re.match(r"<bash-input>(.*?)</bash-input>", text, re.S)
    if m:
        return "! " + m.group(1).strip()
    if not text or text.startswith(("<local-command", "<bash-stdout", "<bash-stderr", "<task-notification",
                                    "[Request interrupted", "<command-message>")):
        return None
    return text


def tool_line(b):
    name, inp = b.get("name") or "tool", b.get("input") or {}
    detail = (inp.get("description") or inp.get("file_path") or inp.get("path") or inp.get("pattern")
              or inp.get("url") or inp.get("query") or inp.get("command") or inp.get("prompt") or "")
    detail = " ".join(str(detail).split())
    return f"{name}: {detail[:100]}" if detail else name


def turns(path):
    """The session as (user record, user text, assistant records after it) turns, plus its title and facts."""
    meta, title, out, cur = {}, "", [], None
    for r in records(path):
        t = r.get("type")
        if t == "ai-title" and r.get("aiTitle"):
            title = r["aiTitle"]
        elif t == "agent-name" and r.get("agentName"):
            title = title or r["agentName"]
        elif t == "user":
            if not meta:
                meta = {k: r.get(k) for k in ("sessionId", "cwd", "gitBranch", "version", "entrypoint")}
            text = user_text(r)
            if text is not None:
                cur = (r, text, [])
                out.append(cur)
        elif t == "assistant" and not r.get("isSidechain") and cur is not None:
            cur[2].append(r)
    return out, title, meta


def reply(recs, last_only=False):
    """An assistant turn as (text, model, tool lines, time of the first record)."""
    texts, tools, model = [], [], None
    for r in recs:
        m = r.get("message") or {}
        model = model or m.get("model")
        for b in m.get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and (b.get("text") or "").strip():
                texts.append(b["text"].strip())
            elif b.get("type") == "tool_use":
                tools.append(tool_line(b))
    if last_only:
        texts = texts[-1:]
    return "\n\n".join(texts), model, tools, recs[0].get("timestamp") if recs else None


def tool_summary(tools):
    counts = {}
    for t in tools:
        counts[t.split(":")[0]] = counts.get(t.split(":")[0], 0) + 1
    head = " · ".join(f"{n} ×{c}" if c > 1 else n for n, c in counts.items())
    lines = tools[:15] + ([f"… and {len(tools) - 15} more"] if len(tools) > 15 else [])
    return head + "\n" + "\n".join("- " + line for line in lines)


def msg(mid, role, text, at, **extra):
    return {"id": mid, "role": role, "text": text, "created_at": at, **{k: v for k, v in extra.items() if v}}


def assistant_msgs(recs, author, last_only=False):
    text, model, tools, at = reply(recs, last_only)
    base = recs[0].get("uuid") if recs else None
    out = []
    if tools:
        out.append(msg(f"{base}:tools", "tool", tool_summary(tools), at, author="tools"))
    if text:
        out.append(msg(f"{base}:reply", "assistant", text, at, author=author, model=model))
    return out


def session_times(msgs):
    stamps = [m["created_at"] for m in msgs if m.get("created_at")]
    return (min(stamps), max(stamps)) if stamps else (None, None)


# ---- claudecode ------------------------------------------------------------------------------------------------

def claudecode_conversation(path):
    ts, title, meta = turns(path)
    if not ts or meta.get("entrypoint") == "sdk-cli":
        return None
    msgs = []
    for r, text, recs in ts:
        msgs.append(msg(r.get("uuid"), "user", text, r.get("timestamp"), author="you"))
        msgs += assistant_msgs(recs, "Claude")
    first = next((m["text"] for m in msgs if m["role"] == "user"), "")
    start, end = session_times(msgs)
    project = meta.get("cwd") or ""
    return {"id": meta.get("sessionId") or path.stem,
            "title": title or " ".join(first.split())[:80] or "Claude Code session",
            "created_at": start, "updated_at": end, "messages": msgs,
            "metadata": {"cwd": project, "project": Path(project).name if project else "",
                         "git_branch": meta.get("gitBranch"), "claude_code_version": meta.get("version"),
                         "entrypoint": meta.get("entrypoint"), "transcript": str(path)}}


def claudecode_files():
    for d in sorted(PROJECTS.glob("*")):
        if d.is_dir() and d.name != MOCHI_PROJECT:
            yield from sorted(d.glob("*.jsonl"))


# ---- mochi -----------------------------------------------------------------------------------------------------

CHAT_RE = re.compile(r"^The user opened a chat with you from the (?:pig|cat)'s menu")
CHAT_ASK_RE = re.compile(r'^The user picked "Chat about it" on this, which you put on the (?:pig|cat):\n  (.*?)\n', re.S)
ASK_RE = re.compile(r"^The user asked you, through the (?:pig|cat), to do this:\n\n(.*?)\n\nDo it\.", re.S)
ANSWER_RE = re.compile(r'^The user answered "(.*?)" to this, which you put on the (?:pig|cat):\n  (.*?)\n'
                       r'The options you gave them: (.*?)(?:\n|$)', re.S)
FILES_RE = re.compile(r"\n\nThey attached, from their phone.*?:\n(.*?)\nIf there is no caption", re.S)


def strip_block(text):
    m = list(re.finditer(r"```(?:json)?\s*\{.*?\}\s*```\s*$", text, re.S))
    return text[:m[-1].start()].rstrip() if m else text


def mochi_reply(recs, sid, last_only=True):
    out = assistant_msgs(recs, "Mochi", last_only)
    for m in out:
        if m["role"] == "assistant":
            m["text"] = strip_block(m["text"]) or "(done, nothing to say)"
            m["id"] = f"{sid}:reply"
        else:
            m["id"] = f"{sid}:tools"
    return out


def read_comms():
    """Asks as threads (an id Mochi raises again after it was settled starts a new thread), and which thread each
    finished run belongs to."""
    asks, current, done = {}, {}, {}
    try:
        lines = COMMS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return asks, done
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        kind, aid = e.get("kind"), str(e.get("id") or "")
        key = current.get(aid)
        if kind == "ask" and aid:
            if key is None or asks[key]["events"]:
                key = current[aid] = aid if key is None else f"{aid}~{e.get('t')}"
                asks[key] = {"events": [], "at": e.get("t")}
            asks[key].update({k: e.get(k) for k in ("text", "options", "path", "src", "urgent")})
        elif kind in ("answer", "closed") and key:
            asks[key]["events"].append(e)
        elif kind == "done" and e.get("session"):
            done[e["session"]] = {**e, "thread": current.get(str(e.get("ask") or ""))}
    return asks, done


def iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat() if t else None


def ask_question(aid, text, options, at, path=None):
    body = text.strip()
    if path and Path(path).is_file():  # a report: what it said, not only its title
        try:
            body += "\n\n" + Path(path).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            pass
    if options:
        body += "\n\n_Options: " + (options if isinstance(options, str) else " · ".join(options)) + "_"
    return msg(f"ask:{aid}:q", "assistant", body, at, author="Mochi", metadata={"kind": "ask"})


def mochi_conversations():
    asks, done = read_comms()
    convs, by_ask = [], {}
    for path in sorted((PROJECTS / MOCHI_PROJECT).glob("*.jsonl")):
        ts, title, meta = turns(path)
        if not ts:
            continue
        sid = meta.get("sessionId") or path.stem
        r0, first, recs0 = ts[0]
        at = r0.get("timestamp")
        link = done.get(sid) or {}
        via = link.get("via") or "pig"
        msgs, kind = [], None
        if CHAT_RE.match(first) or CHAT_ASK_RE.match(first):
            kind = "chat"
            m = CHAT_ASK_RE.match(first)
            note = f'You picked "Chat about it" on: {m.group(1).strip()}' if m else "You opened a chat from the pig's menu."
            msgs.append(msg(f"{sid}:opened", "system", note, at))
            msgs += mochi_reply(recs0, f"{sid}:0", last_only=False)
            for r, text, recs in ts[1:]:
                msgs.append(msg(r.get("uuid"), "user", text, r.get("timestamp"), author="you"))
                msgs += mochi_reply(recs, r.get("uuid"), last_only=False)
            said = next((m["text"] for m in msgs if m["role"] == "user"), "")
            title = " ".join(said.split())[:80] or title or "Chat with Mochi"
        elif ASK_RE.match(first):
            kind = "request"
            text = ASK_RE.match(first).group(1).strip()
            files = FILES_RE.search(first)
            if files:
                text = FILES_RE.sub("", text).strip()
                text += "\n\n[attached: " + ", ".join(Path(f.lstrip("- ").strip()).name
                                                     for f in files.group(1).splitlines() if f.strip()) + "]"
            msgs.append(msg(f"{sid}:ask", "user", text, at, author="you", metadata={"via": via}))
            msgs += mochi_reply(recs0, sid)
            title = " ".join(text.split())[:80]
        elif ANSWER_RE.match(first):
            kind = "answer"
            label, question, options = (g.strip() for g in ANSWER_RE.match(first).groups())
            aid = link.get("thread")
            if aid and aid in asks:  # this run belongs to an ask the relay logged: it continues that conversation
                by_ask[aid] = mochi_reply(recs0, sid)
                continue
            msgs.append(ask_question(sid, question, options, at))
            msgs.append(msg(f"{sid}:answer", "user", label, at, author="you", metadata={"via": via}))
            msgs += mochi_reply(recs0, sid)
            title = " ".join(question.split())[:80]
        else:
            continue  # a round, a discovery run, a dream, a test: Mochi's own work
        if not any(m["role"] in ("user", "assistant") for m in msgs):
            continue
        start, end = session_times(msgs)
        convs.append({"id": f"session:{sid}", "title": title or "Mochi", "created_at": start, "updated_at": end,
                      "messages": msgs, "metadata": {"kind": kind, "via": via, "session": sid,
                                                     "transcript": str(path)}})
    for aid, a in asks.items():
        if a.get("src") == "result" or not a.get("text"):
            continue  # "done: …" notes are the answer to a request, already in that request's conversation
        msgs = [ask_question(aid, a["text"], a.get("options") or [], iso(a.get("at")), a.get("path"))]
        for e in a["events"]:
            if e["kind"] == "answer":
                msgs.append(msg(f"ask:{aid}:a", "user", e.get("label") or "", iso(e.get("t")), author="you",
                                metadata={"via": e.get("via") or "pig"}))
            else:
                msgs.append(msg(f"ask:{aid}:closed", "system", f"Closed: {e.get('how') or 'gone'}.", iso(e.get("t"))))
        msgs += by_ask.get(aid, [])
        start, end = session_times(msgs)
        convs.append({"id": f"ask:{aid}", "title": " ".join(a["text"].split())[:80], "created_at": start,
                      "updated_at": end, "messages": msgs,
                      "metadata": {"kind": "question", "raised_by": a.get("src"), "urgent": bool(a.get("urgent"))}})
    return convs


# ---- sending ---------------------------------------------------------------------------------------------------

def load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def payload(provider, convs):
    return {"format": FORMAT, "provider": provider, "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "conversations": convs}


def send(provider, convs):
    cfg = config()
    cmd = ["ssh", "-i", os.path.expanduser(cfg["key"]), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
           "-o", "ConnectTimeout=10", cfg["ssh"],
           f"docker exec -i {cfg['container']} python -m backend.app.cli sync {provider}"]
    p = subprocess.run(cmd, input=json.dumps(payload(provider, convs)).encode(), capture_output=True, timeout=1800)
    out = p.stdout.decode(errors="replace").strip().splitlines()
    try:
        result = json.loads(out[-1]) if out else {}
    except ValueError:
        result = {}
    if p.returncode != 0 or result.get("status") != "completed":
        err = result.get("error") or p.stderr.decode(errors="replace").strip()[-500:] or f"exit {p.returncode}"
        raise RuntimeError(f"{provider}: {err}")
    return result.get("summary") or {}


def changed_claudecode(state, everything):
    now, seen, out = dt.datetime.now().timestamp(), state.get("claudecode", {}), []
    for f in claudecode_files():
        st = f.stat()
        if now - st.st_mtime < SETTLE:
            continue
        key = f"{st.st_mtime:.0f}:{st.st_size}"
        if everything or seen.get(str(f)) != key:
            out.append((f, key))
    return out


def sync(everything=False, dry=False):
    state = load_state()
    files = changed_claudecode(state, everything)
    convs = [c for c in (claudecode_conversation(f) for f, _ in files) if c]
    mochi = mochi_conversations()
    lines = []
    for provider, cs in (("claudecode", convs), ("mochi", mochi)):
        n = sum(len(c["messages"]) for c in cs)
        if dry or not cs:
            lines.append(f"{provider}: {len(cs)} conversations, {n} messages{' (dry run)' if dry and cs else ''}")
            continue
        s = send(provider, cs)
        lines.append(f"{provider}: {len(cs)} conversations sent, {s.get('inserted_messages', 0)} new messages")
    if not dry:
        state.setdefault("claudecode", {}).update({str(f): key for f, key in files})
        state["last_sync"] = dt.datetime.now().isoformat(timespec="seconds")
        STATE.write_text(json.dumps(state, indent=1))
    print("\n".join(lines))


def main(argv):
    if argv[:1] == ["sync"]:
        sync("--all" in argv, "--dry-run" in argv)
    elif argv[:1] == ["dump"] and argv[1:2] in (["claudecode"], ["mochi"]):
        convs = ([c for c in map(claudecode_conversation, claudecode_files()) if c] if argv[1] == "claudecode"
                 else mochi_conversations())
        json.dump(payload(argv[1], convs), sys.stdout, ensure_ascii=False, indent=1)
    else:
        print(__doc__.strip())
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
        print(f"mochi-archive: {e}", file=sys.stderr)
        raise SystemExit(1)
