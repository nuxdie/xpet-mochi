#!/bin/sh
# Joins this machine to Mochi's Syncthing mesh: the other nodes as devices, Mochi's three folders shared with them.
# Idempotent; run on every node (ha/deploy.sh does). Needs a running Syncthing of this user.
set -eu
ME=$(hostname -s)
ST=$(command -v syncthing || echo "$HOME/.local/bin/syncthing")
cli() { "$ST" cli "$@"; }
CONF=$(ls "$HOME/.local/state/syncthing/config.xml" "$HOME/.config/syncthing/config.xml" 2>/dev/null | head -1)
KEY=$(sed -n 's:.*<apikey>\(.*\)</apikey>.*:\1:p' "$CONF")
GUI=$(sed -n '/<gui /,/<\/gui>/s:.*<address>\(.*\)</address>.*:\1:p' "$CONF")
api() { curl -fsS -X "$1" -H "X-API-Key: $KEY" -H 'Content-Type: application/json' "http://$GUI$2" ${3:+-d "$3"}; }
# name id addresses
NODES="desk C3DXPKJ-UKSVLI6-OHAFQ6C-ANRMOW5-N64EUM2-4SF2P7A-RPXJAHX-OK7IUQ5 dynamic
sff XWEZ2ER-ZMS5K4B-7MCUTYO-QUYVW4I-EGDC6HJ-EBPYK2H-5ANTVBP-3WMVPQB tcp://sff.local:22000,dynamic
ubuntu-4gb-nbg1-1 32V5QZN-R5IGSFQ-ERUCRR6-SWB4QIN-2DYIXRB-OJQPTE5-BD5WC35-4HA4HAP tcp://mc.tsatsin.com:22000,quic://mc.tsatsin.com:22000,dynamic"
# id path
FOLDERS="mochi-work $HOME/.local/share/mochi
mochi-config $HOME/.config/mochi
mochi-claude $HOME/.claude/projects/-home-n--local-share-mochi"

have_dev=$(cli config devices list)
echo "$NODES" | while read -r name id addrs; do
  [ "$name" = "$ME" ] && continue
  if ! echo "$have_dev" | grep -q "$id"; then
    cli config devices add --device-id "$id" --name "$name"
    cli config devices "$id" addresses delete 0 2>/dev/null || true
    echo "$addrs" | tr , '\n' | while read -r a; do cli config devices "$id" addresses add "$a"; done
  fi
done

have_fold=$(cli config folders list)
echo "$FOLDERS" | while read -r fid path; do
  mkdir -p "$path"
  case $fid in
    mochi-work) printf '%s\n' '// per machine, never synced (see ha/README.md)' chrome workshop browser.lock '*.tmp' '*.lock' \
                  'mcp-*.json' browser.json box/viewer.pid senses/cache '.ha-local' > "$path/.stignore" ;;
    mochi-config) printf '%s\n' xpet-repo '*.tmp' > "$path/.stignore" ;;
  esac
  if ! echo "$have_fold" | grep -qx "$fid"; then
    cli config folders add --id "$fid" --label "$fid" --path "$path"
  fi
  if [ "$ME" = sff ]; then  # the box that is always on keeps old versions: a bad edit by any node can be undone
    ver='{"type":"staggered","params":{"maxAge":"2592000","cleanInterval":"3600"}}'
  else
    ver='{"type":"trashcan","params":{"cleanoutDays":"14"}}'
  fi
  api PATCH "/rest/config/folders/$fid" "{\"fsWatcherDelayS\":2,\"versioning\":$ver}"
  echo "$NODES" | while read -r name id addrs; do
    [ "$name" = "$ME" ] && continue
    cli config folders "$fid" devices list | grep -q "$id" || cli config folders "$fid" devices add --device-id "$id"
  done
done
echo "syncthing: $ME joined"
