#!/usr/bin/env bash
# Install (or update) Home Assistant Core natively on the Pi, no Docker, and register the cameras from .env.
# Run from the Mac after the first ./scripts/deploy.sh: ./scripts/install-ha.sh
# Needs in .env: PI_HOST, HA_ADMIN_USER, HA_ADMIN_PASSWORD and one CAMERA_<NAME>_RTSP per camera.
# The first install builds a venv of ~700 MB and takes several minutes on a Pi.
set -euo pipefail

cd "$(dirname "$0")/.."
set -a
# shellcheck disable=SC1091
. ./.env
set +a

: "${PI_HOST:?Set PI_HOST in .env}"
: "${HA_ADMIN_USER:?Set HA_ADMIN_USER in .env}"
: "${HA_ADMIN_PASSWORD:?Set HA_ADMIN_PASSWORD in .env}"
PI_USER="${PI_USER:-pi}"
PI_PORT="${PI_PORT:-22}"
PI_TARGET_DIR="${PI_TARGET_DIR:-/home/$PI_USER/pi-con}"
HA_DIR="/home/$PI_USER/homeassistant"

SSH_OPTS=(-p "$PI_PORT" -o BatchMode=yes -o StrictHostKeyChecking=accept-new)
if [ -n "${PI_SSH_KEY_PATH:-}" ]; then
  KEY="${PI_SSH_KEY_PATH/#\~/$HOME}"
  [ -f "$KEY" ] && SSH_OPTS+=(-i "$KEY" -o IdentitiesOnly=yes)
fi
pi() { ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" "$@"; }

echo "==> Installing Home Assistant Core in $HA_DIR (this is slow the first time)"
pi "bash -s" <<REMOTE
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
sudo -E apt-get update -qq
sudo -E apt-get install -y --no-install-recommends python3-venv ffmpeg libturbojpeg0 libpcap0.8
mkdir -p '$HA_DIR/config'
[ -d '$HA_DIR/venv' ] || python3 -m venv '$HA_DIR/venv'
'$HA_DIR/venv/bin/pip' install --quiet --upgrade pip
'$HA_DIR/venv/bin/pip' install --quiet --upgrade homeassistant
[ -f '$HA_DIR/config/configuration.yaml' ] || printf 'default_config:\n' > '$HA_DIR/config/configuration.yaml'

sudo tee /etc/systemd/system/homeassistant.service >/dev/null <<UNIT
[Unit]
Description=Home Assistant
After=network-online.target

[Service]
User=$PI_USER
WorkingDirectory=$HA_DIR/config
ExecStart=$HA_DIR/venv/bin/hass -c $HA_DIR/config
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable homeassistant >/dev/null 2>&1
sudo systemctl restart homeassistant
REMOTE

echo "==> Waiting for Home Assistant to answer (first start takes a few minutes)"
pi "for i in \$(seq 1 120); do curl -sf -o /dev/null http://127.0.0.1:8123/api/onboarding && exit 0; sleep 5; done; echo 'Home Assistant did not come up:' >&2; journalctl -u homeassistant -n 30 --no-pager >&2; exit 1"

echo "==> Sending credentials to $PI_TARGET_DIR/.env (private to $PI_USER) and configuring"
pi "mkdir -p '$PI_TARGET_DIR' && touch '$PI_TARGET_DIR/.env' && chmod 600 '$PI_TARGET_DIR/.env'"
# Keep the token already generated on the Pi; refresh the admin login and camera URLs.
{ pi "grep '^HA_TOKEN=' '$PI_TARGET_DIR/.env' || true"; grep -E '^(HA_ADMIN_USER|HA_ADMIN_PASSWORD|CAMERA_[A-Z0-9_]+_RTSP)=' .env; } \
  | pi "umask 077; cat > '$PI_TARGET_DIR/.env.new' && mv '$PI_TARGET_DIR/.env.new' '$PI_TARGET_DIR/.env'"
cat scripts/ha_setup.py | pi "cat > '$PI_TARGET_DIR/ha_setup.py'"
pi "'$HA_DIR/venv/bin/python' '$PI_TARGET_DIR/ha_setup.py' --env '$PI_TARGET_DIR/.env' && rm '$PI_TARGET_DIR/ha_setup.py'"

echo "==> Restarting pi-con so it picks up HA_TOKEN"
pi "sudo systemctl restart pi-con"
echo "==> Done. Home Assistant: http://$PI_HOST:8123  (user $HA_ADMIN_USER)"
echo "==> Say \"Hey Jarvis\" then \"Montre la caméra rabbit\", or test: curl -s http://$PI_HOST:8080/camera/rabbit/stream"
