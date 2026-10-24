#!/bin/sh
# Sets up (or updates) one of Mochi's nodes from the desk. Idempotent.
#   ha/deploy.sh desk local            this machine
#   ha/deploy.sh sff n@sff.local
#   ha/deploy.sh cloud n@mc.tsatsin.com
# The desk's installed copies are the source (~/.local/bin/mochi-*, the user units, claude itself): what runs here
# runs there. Syncthing must already be set up (ha/syncthing-setup.sh) so the workspace and config are there.
set -eu
NODE=$1
TARGET=${2:-local}
REPO=$(cd "$(dirname "$0")/.." && pwd)
KEY="$HOME/.ssh/mochi_ed25519"
UNITS="$HOME/.config/systemd/user"
# What runs only on the leader. The NAS ones need the LAN, so not on the cloud.
LEADER_UNITS="mochi-phone.service mochi-mc.service mochi-bedtime.timer mochi-fuel.timer mochi-tracks.timer"
[ "$NODE" = cloud ] || LEADER_UNITS="$LEADER_UNITS mochi-music.timer mochi-nas-index.timer"

# The desk first: brain and ha from the repo (the rest of brain/ is installed by `make install`).
install -Dm755 "$REPO/brain/mochi_brain.py" "$HOME/.local/bin/mochi-brain"
install -Dm755 "$REPO/brain/mochi_ha.py" "$HOME/.local/bin/mochi-ha"
install -Dm644 "$REPO/dist/mochi-ha.service" "$UNITS/mochi-ha.service"

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/bin" "$STAGE/units" "$STAGE/home"
cp -a "$HOME"/.local/bin/mochi-* "$STAGE/bin/"
CLAUDE_REAL=$(readlink -f "$HOME/.local/bin/claude")
cp "$REPO/dist/mochi-ha.service" "$REPO/dist/mochi-leader.target" "$REPO/dist/mochi-mail.service" \
   "$REPO/dist/mochi-mail.timer" "$STAGE/units/"
for u in $LEADER_UNITS; do
  cp "$UNITS/$u" "$STAGE/units/"
  case $u in *.timer) cp "$UNITS/${u%.timer}.service" "$STAGE/units/" ;; esac
done
{ printf '[Unit]\nDescription=What runs only where Mochi leads (mochi-ha starts and stops this)\n'
  for u in $LEADER_UNITS; do printf 'Wants=%s\n' "$u"; done; } > "$STAGE/units/mochi-leader.target"
# the brain as a plain service where there is no desktop session to start it
{ sed '/^\[Install\]/,$d' "$REPO/dist/mochi-brain.service" | sed 's/^Restart=on-failure/Restart=always\nUnsetEnvironment=DISPLAY WAYLAND_DISPLAY XAUTHORITY/'
  printf '\n[Install]\nWantedBy=default.target\n'; } > "$STAGE/units/mochi-brain.node.service"
cp "$HOME/.mbsyncrc" "$HOME/.msmtprc" "$HOME/.notmuch-config" "$STAGE/home/"
if [ "$NODE" = sff ]; then  # Debian 11's isync 1.3 still says Master/Slave for Far/Near
  sed -E 's/^Far /Master /; s/^Near /Slave /; s/^(Create|Expunge|Remove) Near/\1 Slave/; s/^(Create|Expunge|Remove) Far/\1 Master/' \
    "$HOME/.mbsyncrc" > "$STAGE/home/.mbsyncrc"
fi
if [ "$NODE" = cloud ]; then  # the cloud's disk sits next to Minecraft: inbox and sent only, no "All Mail" (7 GB)
  awk 'BEGIN{RS="";ORS="\n\n"} !/^Channel [a-z]+-all/' "$HOME/.mbsyncrc" | sed -E 's/ [a-z]+-all( |$)/\1/' > "$STAGE/home/.mbsyncrc"
fi
[ "$NODE" = sff ] && cp "$HOME/.ssh/samba_credentials" "$STAGE/home/" 2>/dev/null || true

cat > "$STAGE/remote.sh" <<EOF
set -eu
NODE=$NODE
TZ_DESK=$(timedatectl show -p Timezone --value 2>/dev/null || echo Europe/Amsterdam)
LEADER_UNITS="$LEADER_UNITS"
EOF
cat >> "$STAGE/remote.sh" <<'EOF'
cd "$HOME"
S=$HOME/.mochi-stage
mkdir -p .local/bin .config/systemd/user .config/mochi-ha .local/state .ssh
printf '%s\n' "$NODE" > .config/mochi-ha/node
# Mochi's day (quiet hours, the brief, nights) is his day: the desk's timezone, whatever the box runs on
mkdir -p .config/environment.d
printf 'TZ=%s\n' "$TZ_DESK" > .config/environment.d/mochi-tz.conf
systemctl --user set-environment TZ="$TZ_DESK"
if [ "$NODE" != desk ]; then  # ~/.local/bin first: Mochi's tools, claude, and on sff a current python3 (uv, 3.12)
  printf 'PATH=%s\n' "$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" > .config/environment.d/mochi-path.conf
  systemctl --user set-environment PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
fi
U=.config/systemd/user
if [ "$NODE" != desk ]; then
  cp -a "$S"/bin/mochi-* .local/bin/
  mkdir -p .local/share/claude/versions
  cp "$S/claude" ".local/share/claude/versions/$(cat "$S/claude.version")"
  chmod 755 ".local/share/claude/versions/$(cat "$S/claude.version")"
  ln -sfn "$HOME/.local/share/claude/versions/$(cat "$S/claude.version")" .local/bin/claude
  for f in .mbsyncrc .msmtprc .notmuch-config; do install -m600 "$S/home/$f" "$f"; done
  [ -f "$S/home/samba_credentials" ] && install -m600 "$S/home/samba_credentials" .ssh/samba_credentials
  if [ "$NODE" = sff ] && [ -d Mail ]; then  # a Maildir copied from the desk: isync 1.4's state headers, in 1.3's words
    find Mail -name .mbsyncstate -exec sed -i '1,6{s/^FarUidValidity/MasterUidValidity/;s/^NearUidValidity/SlaveUidValidity/;s/^MaxExpiredFarUid/MaxExpiredMasterUid/}' {} +
  fi
  cp "$S"/units/* "$U/"
  mv "$U/mochi-brain.node.service" "$U/mochi-brain.service"
  # Mochi's own key for this node (mochi-mc, mochi-box); authorized on the other machines from the desk
  [ -f .ssh/mochi_ed25519 ] || ssh-keygen -q -t ed25519 -N '' -C "mochi@$NODE" -f .ssh/mochi_ed25519
  # Telegram as him and yt-dlp (no whisper here: transcribing stays on the desk)
  if [ -x .local/bin/uv ]; then  # sff: Debian 11's python 3.9 is too old for some tools; python3 is this venv's
    [ -x .local/share/mochi-tools/bin/python3 ] || .local/bin/uv venv -q -p 3.12 .local/share/mochi-tools
    VIRTUAL_ENV=$HOME/.local/share/mochi-tools .local/bin/uv pip install -q -U cryptography markdown pillow telethon yt-dlp
    rm -f .local/bin/python3  # a wrapper, not a symlink: through a symlink python doesn't find its venv
    printf '#!/bin/sh\nexec "$HOME/.local/share/mochi-tools/bin/python3" "$@"\n' > .local/bin/python3
    chmod 755 .local/bin/python3
  else
    [ -x .local/share/mochi-tools/bin/pip ] || python3 -m venv .local/share/mochi-tools
    .local/share/mochi-tools/bin/pip install -q -U telethon yt-dlp >/dev/null 2>&1 || echo "mochi-tools: pip failed"
  fi
else
  cp "$S/units/mochi-leader.target" "$S/units/mochi-ha.service" "$U/"
fi
# what the per-minute scripts remember follows the leader: their state lives in the synced workspace
for d in mochi-bedtime mochi-fuel; do
  w=.local/share/mochi/node-state/${d#mochi-}
  mkdir -p "$w"
  if [ -d ".local/state/$d" ] && [ ! -L ".local/state/$d" ]; then
    cp -an ".local/state/$d/." "$w/" && rm -rf ".local/state/$d"
  fi
  ln -sfn "$HOME/$w" ".local/state/$d"
done
systemctl --user daemon-reload
for u in $LEADER_UNITS; do  # started only through mochi-leader.target now
  systemctl --user disable "$u" >/dev/null 2>&1 || true
  mkdir -p "$U/$u.d"
  printf '[Unit]\nPartOf=mochi-leader.target\n' > "$U/$u.d/ha.conf"
done
systemctl --user daemon-reload
if [ "$NODE" != desk ]; then
  systemctl --user enable mochi-brain.service mochi-mail.timer >/dev/null 2>&1
  systemctl --user start mochi-mail.timer
  systemctl --user restart mochi-brain.service
fi
systemctl --user enable mochi-ha.service >/dev/null 2>&1
systemctl --user restart mochi-ha.service
rm -rf "$S"
echo "$NODE: deployed; mochi key: $(cut -d' ' -f1,3 .ssh/mochi_ed25519.pub 2>/dev/null)"
EOF

if [ "$TARGET" = local ]; then
  rm -rf "$HOME/.mochi-stage" && cp -a "$STAGE" "$HOME/.mochi-stage"
  sh "$HOME/.mochi-stage/remote.sh"
else
  cp "$CLAUDE_REAL" "$STAGE/claude"
  basename "$CLAUDE_REAL" > "$STAGE/claude.version"
  rsync -a --delete -e "ssh -i $KEY" "$STAGE/" "$TARGET:.mochi-stage/"
  ssh -i "$KEY" "$TARGET" 'sh .mochi-stage/remote.sh'
fi
