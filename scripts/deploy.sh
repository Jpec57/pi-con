#!/usr/bin/env bash
# Deploy pi-con to the Raspberry Pi (4 or 5, 64-bit OS) over SSH. Run: ./scripts/deploy.sh
# Copies the code, installs it in a venv on the Pi and (re)starts it as a systemd service.
# Connection settings come from .env (see .env.example) or from the environment.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

: "${PI_HOST:?Set PI_HOST in .env (see .env.example)}"
PI_USER="${PI_USER:-pi}"
PI_PORT="${PI_PORT:-22}"
PI_TARGET_DIR="${PI_TARGET_DIR:-/home/$PI_USER/pi-con}"
SERVICE="pi-con"

SSH_OPTS=(-p "$PI_PORT" -o BatchMode=yes -o StrictHostKeyChecking=accept-new)
if [ -n "${PI_SSH_KEY_PATH:-}" ]; then
  KEY="${PI_SSH_KEY_PATH/#\~/$HOME}"
  SSH_OPTS+=(-i "$KEY" -o IdentitiesOnly=yes)
fi

pi() { ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" "$@"; }

echo "==> Checking SSH connection to $PI_USER@$PI_HOST:$PI_PORT"
pi "uname -m && python3 --version"

echo "==> Sending code to $PI_TARGET_DIR"
pi "mkdir -p '$PI_TARGET_DIR'"
COPYFILE_DISABLE=1 tar --no-xattrs -czf - pyproject.toml README.md requirements-pi.txt src config \
  | pi "tar xzf - -C '$PI_TARGET_DIR'"

echo "==> Installing and restarting the service"
pi "bash -s" <<REMOTE
set -euo pipefail
cd '$PI_TARGET_DIR'

if ! dpkg -s libportaudio2 >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  sudo -E apt-get update -qq
  sudo -E apt-get install -y --no-install-recommends libportaudio2 python3-venv
fi

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet --upgrade -r requirements-pi.txt
.venv/bin/pip install --quiet --upgrade --no-deps 'openwakeword>=0.6,<0.7' .
.venv/bin/pi-con --download-models

sudo tee /etc/systemd/system/$SERVICE.service >/dev/null <<UNIT
[Unit]
Description=Pi-Con voice assistant
After=network-online.target sound.target

[Service]
User=$PI_USER
SupplementaryGroups=audio
WorkingDirectory=$PI_TARGET_DIR
ExecStart=$PI_TARGET_DIR/.venv/bin/pi-con
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable $SERVICE >/dev/null 2>&1
sudo systemctl restart $SERVICE
REMOTE

echo "==> Verifying the service stays up"
sleep 8
if [ "$(pi "systemctl is-active $SERVICE" || true)" != "active" ]; then
  echo "Service is not healthy. Last logs:" >&2
  pi "journalctl -u $SERVICE -n 50 --no-pager" >&2 || true
  exit 1
fi
echo "==> UI: http://$PI_HOST:8080"
echo "==> Deployed. Follow logs with: ssh -p $PI_PORT $PI_USER@$PI_HOST journalctl -fu $SERVICE"
