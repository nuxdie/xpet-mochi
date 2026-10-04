# xpet

A small 3D Minecraft-style pig that lives on top of your windows (X11), software-rendered with Cairo (no GL). It walks along title bars and around inside windows (on their floor, just above the bottom edge), jumps between them, rides along when you drag a window, and falls off when you close or minimise the one it's standing on.

It is properly animated: a four-legged gait that matches its ground speed (a trot when it chases your cursor), breathing, a wagging tail nub, a head that follows your pointer when it moves, looks over at the window you are working in (focus and title changes count as activity), and looks out at you when it talks to you, when you pet it, and now and then when things are quiet (it turns to face you to talk), and a smooth turn through "facing you" instead of a flip. On its own it sniffs the ground, stretches, shakes, looks around, scratches, wiggles and hops. It nuzzles your hand when petted and shakes its head when it has had enough, squashes on landing and stretches on take-off, kicks when you hold it and tumbles when you throw it, turns to face into the screen and sits down at its laptop (or book, or globe) when it works, tapping the keys and following the lines, chews with a working snout as the bowl empties, and twitches a leg in its sleep. Poses ease into each other instead of snapping. It cheers (two jumps) when a Claude run finishes that touched five or more files, sulks when a run keeps failing, and now and then walks over to sit on the window you have settled into. It blinks at a natural rhythm, snuffles at your pointer when it comes to its nose, its skin has a faint per-texel tone, and the light goes warm in the evening and cool at night. `src/art3d.hpp` holds the model, the rig and the renderer.

## Build

Needs `libx11-dev libxext-dev libxrandr-dev libcairo2-dev`.

```sh
make
./xpet              # or: make install  (→ ~/.local/bin/xpet)
```

## Use

- **Left-click**: pet it (or, right after a wheel preview, open that question)
- **Middle-click**: one line about what it's up to
- **Wheel**: flip through its questions in a bubble without opening the panel
- **Drag**: pick it up; let go mid-swing to throw it. Set it down gently and it stays there for a while.
- **Right-click**: menu (Give me a task..., Open a chat, Shoo for 10 min, Quit; plus "N questions for you" when it has some)

It looks after itself: it eats when hungry, naps when tired, and now and then chases your cursor for fun; there is no feeding or stats to manage. It hides while a fullscreen app is focused.

State lives in `~/.config/xpet/state` and time passes (gently) while it's not running. It doesn't die.

Options: `--name NAME`, `--reset`, `--sheet out.png` (render every pose to an image; `XPET_SHEET_ZOOM=4` for a big one).

## Always on

`make install-autostart` puts `dist/xpet.service` and `dist/mochi-brain.service` in `~/.config/systemd/user/` and
`dist/xpet.desktop` in `~/.config/autostart/`. At login the autostart entry hands the X display to systemd and starts
both services; systemd restarts them if they crash. Menu → Quit stops the pig for real (until next login).
`systemctl --user start xpet` brings it back, and deleting the autostart file turns autostart off.

## The brain

Claude Code is the brain. `brain/mochi_brain.py` (installed as `mochi-brain`) is a thin relay with no judgement in
it. It needs the `claude` CLI, plus `xprop`, `zenity` and `notify-send`.

About every 30 minutes while you're at the computer (and when you ask, when you answer an offer, when you come
back from a break of two hours or more, and once first thing each day) the relay starts `claude -p` in Mochi's
workspace, `~/.local/share/mochi/`:

- `CLAUDE.md` is Mochi's standing brief (installed from `brain/CLAUDE.md`): who it is, why it exists, what it may
  look at, how a round goes, and the quiet rule.
- `memory/` is what it knows, written by itself: `life.md` (the map of your life and where things are tracked),
  `open-loops.md` (everything in flight), `preferences.md` (how you want it to behave, your "never"s),
  `last-round.md` (handoff to its next self).
- `journal/` has one file per day of what it noticed and did; `mochi-brain --seen` prints the last two.
- `reports/` holds the morning brief and longer write-ups.

Each round, Mochi reads its notes and the relay's context (idle time, the current window and the last hour of apps,
your Claude Code sessions, your answers and asks since last time, its pending offers), looks around proportionately
(mail, Drive, calendar if connected, repos, machine health), does what it is allowed to do by itself (drafts,
summaries, its own notes), and ends with a small JSON block. That block is the only way a run reaches you:

- **asks**: anything that would change the world outside its workspace, or any question it wants to put to you.
  Claude designs the small UI itself: the text and up to five menu options in its own words (a plain yes/no offer, a
  "which evening?" with choices, a check-in with a graceful exit). They wait silently behind a small yellow dot next
  to the pig's head; right-click → **N questions for you** opens a panel beside the pig with the question on top and
  the options below. With several waiting, "Next question" / "Previous question" (or the mouse wheel over the
  panel) flip between them, answering one moves on to the next, and "Later" keeps one waiting. "Not now", "Never", "Dismiss" just
  close it (Mochi hears about it next round); "Show me" opens a report; any other option, "Yes, do it" included,
  starts a second run with a broader tool set and the notes Mochi wrote for itself about that answer.
- **Chat about it** is on every ask: it opens a terminal with Mochi in it, resumed from the very run that raised
  the question, so it remembers why. There you talk normally, with normal permission prompts. **Open a chat** in
  the pig's menu (or `mochi-brain --chat`) opens a fresh chat any time.
- **report**: a file worth reading (the morning brief), also behind the dot.
- **say** with **urgent**: the pig speaks and hops. Reserved for what cannot wait an hour. If you ignore an urgent
  question (or a Claude session stuck on a permission) for half a minute, it walks right up to the screen, big head at
  the glass, stares at you for a few seconds, then walks back, and tries again a couple of minutes later. A non-urgent `say` is
  dropped by the relay, so quiet is enforced, not just requested.

The relay decides *when*, never *what*. It also enforces *which tools* a run may use, because safety should not live
in a prompt: a round gets read-only tools, mail drafts and sends through `mochi-mail` (msmtp; the brief says when it may send on
its own and when it must ask first, and every send is logged), and writes inside `memory/`, `journal/` and `reports/`;
a run you asked for or approved may also edit files and run commands; and a deny list no level can override (no
`sudo`, no deleting mail or sending through the Gmail connector, no force-push, no `rm -rf`, no sharing Drive files). Rounds use Sonnet; things
you ask for use the default model. Constants at the top of the script: cadence, daily cap, models.

You can also just ask it: menu → **Give me a task...**, or

```sh
xpet --ask 'find the biggest files in ~/Downloads'    # same thing from a shell
mochi-brain --round       # do a round now
mochi-brain --chat        # open a chat with Mochi in a terminal
mochi-brain --status      # the pig says what it's up to
mochi-brain --seen        # Mochi's journal for the last two days
mochi-brain --report      # open the latest report
mochi-brain --log         # the relay's recent log
```

Everything the relay does is appended to `~/.local/share/mochi/actions.log`; everything Mochi thought is in its
journal and in the Claude Code transcripts for that directory.

### Senses and the dossier

`brain/mochi_sense.py` (installed as `mochi-sense`) is how Mochi sees past this machine. Each subcommand reads
one source and prints a short digest: `browser` (Firefox/Chromium history: sites, searches, titles), `shell`,
`telegram` (your self-hosted tg-archive), `llm` (your LLM chat archive, live or its local sqlite copy), `nas`
(Synology home share over SMB), `photos` (Immich), `network` (hosts up/down, web services, Tailscale, KDE
Connect, mDNS), `home` (Home Assistant), `hosts` (ssh reports with Mochi's own key), `mail` (the local Maildir), `sessions` (Claude Code transcripts by project), `repos`, and `sources` (what is reachable).
Everything is read-only: sqlite files are copied before being opened, the NAS is only listed, web services
only GET. `~/.config/mochi/sources.json` says where things are and holds any passwords (never printed; start from
`sources.example.json`); where
none is set, a service is read with your own Firefox session cookie.

The relay runs `mochi-sense all` before every round and leaves `senses/digest.md` in the workspace; rounds may
also call any sense directly. Mochi keeps what it learns in `memory/dossier.md` (dated and sourced) and the
registry of sources in `memory/sources.md`; the "Knowing them" section of `brain/CLAUDE.md` sets the method
and the privacy rules (summarize, never quote private messages, never copy secrets, go no deeper than acting
for you requires).

```sh
mochi-sense --help                      # all senses
mochi-sense telegram search "center parcs"
mochi-sense browser --days 7 --grep onshape
mochi-sense nas ls Documents
mochi-brain --senses                    # the digest the last round saw
mochi-brain --discover                  # a long run that only works on the dossier
```

## Talking to it

The pig listens on a user-only Unix datagram socket (`$XDG_RUNTIME_DIR/xpet.sock`), one flat JSON object per message:

```sh
xpet --send 'build finished'                        # plain text = speech bubble
xpet --send '{"event":"say","text":"hi","secs":5}'
xpet --send '{"event":"emote","emote":"happy"}'     # happy | oof | sleep
xpet --send '{"session":"x","event":"tool_start","tool":"Bash","detail":"deploying"}'
```

Activity events (`prompt`, `tool_start`, `tool_end`, `notify`, `stop`, `session_end`, ...) make it drop what it's doing
and act them out; it goes back to normal life when a session stops or goes quiet for 10 minutes.

### As Claude's avatar

`xpet --send-hook` reads a Claude Code hook's JSON from stdin and forwards a summary (file name, a command's
description, a host name, the session's working directory; never prompts, file contents or full commands) to the
pig and to the brain. It prints nothing and returns in a few ms even if neither is running. Point every hook
event at it in `~/.claude/settings.json`:

```json
"PreToolUse": [{ "matcher": "*", "hooks": [{ "type": "command", "command": "~/.local/bin/xpet --send-hook", "timeout": 2 }] }]
```

(same for SessionStart, UserPromptSubmit, PostToolUse, PostToolUseFailure, Notification, Stop, SubagentStop,
SessionEnd, PreCompact). Run the pig with `--no-details` to keep file names and commands out of its bubbles.

## Privacy

Mochi is built to read a lot about you: browser history, chat and mail archives, your NAS, your shell history and
Claude Code transcripts, and it can send mail as you. Everything stays on your machine: its notes live in
`~/.local/share/mochi/`, its config in `~/.config/mochi/`, and neither is part of this repository. Rounds run through
Claude Code, so what a round reads is sent to Anthropic's API like any other Claude Code session. The relay enforces
tool allow and deny lists per run; `brain/CLAUDE.md` sets the rules for what it writes down and when it may send.
