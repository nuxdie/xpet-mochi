#!/bin/sh
# Installs or updates mochi-relay on the server (run there as root). Idempotent.
#   scp relay/* root@mc.tsatsin.com:/tmp/relay/ && ssh root@mc.tsatsin.com 'sh /tmp/relay/deploy.sh'
# First run: opens 80/443 in ufw, gets a Let's Encrypt cert for the host name, generates the two tokens
# (printed once; the desk keeps its own in ~/.config/mochi/phone.json, the phone gets its own at pairing).
set -eu
HOST=${RELAY_HOST:-$(hostname -f 2>/dev/null || hostname)}
case "$HOST" in *.*) ;; *) HOST=mc.tsatsin.com ;; esac
SRC=$(dirname "$0")
install -Dm644 "$SRC/mochi_relay.py" /usr/local/lib/mochi-relay/mochi_relay.py
install -Dm644 "$SRC/mochi-relay.service" /etc/systemd/system/mochi-relay.service
id mochi-relay >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -d /var/lib/mochi-relay mochi-relay
install -d -m 750 -g mochi-relay /etc/mochi-relay /etc/mochi-relay/tls
if [ ! -f /etc/mochi-relay/tokens.json ]; then
    python3 -c 'import json,secrets;print(json.dumps({"desk":secrets.token_urlsafe(32),"phone":secrets.token_urlsafe(32)}))' > /etc/mochi-relay/tokens.json
    chgrp mochi-relay /etc/mochi-relay/tokens.json && chmod 640 /etc/mochi-relay/tokens.json
fi
# the brain nodes' heartbeat token (mochi-ha), added to an older tokens.json; it goes into ~/.config/mochi/ha.json
python3 - <<'PY'
import json, secrets
p = "/etc/mochi-relay/tokens.json"
t = json.load(open(p))
if not t.get("ha"):
    t["ha"] = secrets.token_urlsafe(32)
if not t.get("seat"):  # the Windows seat agent (ha/windows/): may only PUT /v1/beacon/windows
    t["seat"] = secrets.token_urlsafe(32)
    json.dump(t, open(p, "w"))
PY
ufw allow 443/tcp comment 'mochi relay' >/dev/null
ufw allow 80/tcp comment 'letsencrypt http-01' >/dev/null
if [ ! -d /etc/letsencrypt/live/"$HOST" ]; then
    command -v certbot >/dev/null || { apt-get -qq update && DEBIAN_FRONTEND=noninteractive apt-get -qq install -y certbot; }
    certbot certonly --standalone -n --agree-tos --register-unsafely-without-email -d "$HOST"
fi
# certbot renews from a systemd timer; the hook hands the fresh cert to the (unprivileged) relay and restarts it.
cat > /etc/letsencrypt/renewal-hooks/deploy/mochi-relay.sh <<EOF
#!/bin/sh
install -m 640 -g mochi-relay /etc/letsencrypt/live/$HOST/fullchain.pem /etc/mochi-relay/tls/fullchain.pem
install -m 640 -g mochi-relay /etc/letsencrypt/live/$HOST/privkey.pem /etc/mochi-relay/tls/privkey.pem
systemctl restart mochi-relay
EOF
chmod 755 /etc/letsencrypt/renewal-hooks/deploy/mochi-relay.sh
install -m 640 -g mochi-relay /etc/letsencrypt/live/"$HOST"/fullchain.pem /etc/mochi-relay/tls/fullchain.pem
install -m 640 -g mochi-relay /etc/letsencrypt/live/"$HOST"/privkey.pem /etc/mochi-relay/tls/privkey.pem
systemctl daemon-reload
systemctl enable --now mochi-relay >/dev/null
systemctl restart mochi-relay
sleep 1
systemctl is-active mochi-relay
curl -fsS "https://$HOST/v1/health" && echo
echo "tokens in /etc/mochi-relay/tokens.json"
