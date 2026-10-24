# Mochi on several machines

Mochi's brain runs on one machine at a time, the **leader**, chosen by `mochi-ha` (brain/mochi_ha.py) on every node:

| node  | where                          | priority | has                                           |
|-------|--------------------------------|----------|-----------------------------------------------|
| desk  | the desk (Linux; off in Windows)| 1        | the pig, screen, webcam, Mochi's Chrome, Bitwarden |
| sff   | home server, always on         | 2        | NAS, tg-archive, peppy, the clocks            |
| cloud | Hetzner (mc.tsatsin.com)       | 3        | survives power and internet cuts at home      |

State is shared by Syncthing (all three nodes, every folder on every node):

- `mochi-work` = `~/.local/share/mochi` (Mochi's memory, journal, reports, relay.json; `.stignore` keeps `chrome/`,
  `workshop/`, `mcp-*.json` per machine)
- `mochi-config` = `~/.config/mochi` (secrets, `ha.json`, `claude-oauth.token`)
- `mochi-claude` = `~/.claude/projects/-home-n--local-share-mochi` (run transcripts, so `--resume` works anywhere)

sff keeps 30 days of staggered versions of everything (Syncthing's `.stversions/`), the others a 14-day trash can.

Mail is not synced: each node pulls Gmail itself (mbsync, its own Maildir, first copied from the desk by rsync).

## How the leader is chosen

Every 5 s each node publishes a beacon to the relay (`PUT https://mc.tsatsin.com/v1/beacon/NODE`, token in
`ha.json`) and on the LAN (`:8378/beacon`), and reads everyone's. The healthy node with the lowest priority should
lead. Healthy = internet (Anthropic and Telegram reachable), a Claude login, Syncthing working, not on hold.

- A better node that comes back **claims** (after 60 s healthy). The leader stops polling Telegram, lets the run in
  progress finish (up to 25 min), saves, and publishes the state revision it left; the claimant waits until that
  revision has arrived by Syncthing, then leads with a new term.
- A leader not seen for 60 s is replaced by the best healthy standby.
- A leader that sees a newer term elsewhere (it slept while another took over) steps down at once.
- Tasks they gave (asks, approvals) are saved in relay.json while queued or running and run on whichever node leads
  next (twice at most).

The leader runs `mochi-brain` as the relay and `mochi-leader.target` (mochi-phone, mochi-mc, bedtime, fuel, tracks,
music, NAS index), and on the LAN publishes `mochi.local` for the wall clocks. Elsewhere `mochi-brain` is a follower:
what the pig or the command line says there is appended to `handoff/NODE.jsonl` and the leader acts on it.

## Commands

    mochi-ha status            who leads, every node's health, sync state, conflicts
    mochi-ha hold [why]        this node won't lead (hands over if it does); `mochi-ha release` undoes it
    mochi-ha prefer sff        the desk holds for an hour so sff leads (try a handover)
    journalctl --user -u mochi-ha -f

## Setting up a node

    ha/deploy.sh NODE SSH_TARGET    e.g. ha/deploy.sh sff n@sff.local; ha/deploy.sh desk local

Standbys need `~/.config/mochi/claude-oauth.token` (from `claude setup-token` on any machine; it syncs).
