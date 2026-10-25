#!/bin/sh
# Installs the Windows seat agent (mochi-seat.ps1) onto the Windows partition of this desk, from Linux:
#   %LOCALAPPDATA%\Mochi\mochi-seat.ps1 (with the relay's "seat" token filled in) and a Startup launcher that runs it
#   hidden at login. Needs sudo (ntfs-3g mount) and Windows shut down cleanly (no hibernation / Fast Startup).
#   ha/windows/install.sh [DEVICE] [WINUSER]
set -eu
DEV=${1:-/dev/nvme0n1p3}
WUSER=${2:-n}
HERE=$(cd "$(dirname "$0")" && pwd)
MNT=$(mktemp -d)
TOKEN=$(ssh -i "$HOME/.ssh/mochi_ed25519" root@mc.tsatsin.com \
  'python3 -c "import json;print(json.load(open(\"/etc/mochi-relay/tokens.json\"))[\"seat\"])"' 2>/dev/null)
[ -n "$TOKEN" ] || { echo "no seat token on the relay (run relay/deploy.sh there)" >&2; exit 1; }
sudo umount "$DEV" 2>/dev/null || true
sudo mount -t ntfs-3g -o rw,uid="$(id -u)",gid="$(id -g)" "$DEV" "$MNT"
trap 'sudo umount "$MNT"; rmdir "$MNT"' EXIT
U="$MNT/Users/$WUSER"
[ -d "$U" ] || { echo "no Windows user $WUSER" >&2; exit 1; }
mkdir -p "$U/AppData/Local/Mochi"
sed -e "s#__RELAY__#https://mc.tsatsin.com#" -e "s#__TOKEN__#$TOKEN#" -e 's/$/\r/' "$HERE/mochi-seat.ps1" \
  > "$U/AppData/Local/Mochi/mochi-seat.ps1"
START="$U/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"
printf '%s\r\n' "' Mochi seat agent: tells Mochi you're at the PC (see %LOCALAPPDATA%\\Mochi\\mochi-seat.ps1)" \
  'CreateObject("Wscript.Shell").Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & CreateObject("Wscript.Shell").ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Mochi\mochi-seat.ps1""", 0, False' \
  > "$START/mochi-seat.vbs"
ls -la "$U/AppData/Local/Mochi/mochi-seat.ps1" "$START/mochi-seat.vbs" | awk '{print $5, $NF}'
echo "installed for Windows user $WUSER: starts at the next Windows login (tray: a pink dot)"
