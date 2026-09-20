#!/usr/bin/env bash
# Build the ARM64 image and deploy it to the Raspberry Pi over SSH.
# Connection settings come from .env (see .env.example) or from the environment (CI).
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
PI_TARGET_DIR="${PI_TARGET_DIR:-/opt/pi-con}"
PI_PLATFORM="${PI_PLATFORM:-linux/arm64}"
IMAGE="${IMAGE:-pi-con:latest}"
CONTAINER="pi-con"

SSH_OPTS=(-p "$PI_PORT" -o BatchMode=yes -o StrictHostKeyChecking=accept-new)
if [ -n "${PI_SSH_KEY_PATH:-}" ]; then
  KEY="${PI_SSH_KEY_PATH/#\~/$HOME}"
  SSH_OPTS+=(-i "$KEY" -o IdentitiesOnly=yes)
fi

pi() { ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" "$@"; }

echo "==> Checking SSH connection to $PI_USER@$PI_HOST:$PI_PORT"
pi "docker --version"

echo "==> Building $IMAGE for $PI_PLATFORM"
docker buildx build --platform "$PI_PLATFORM" --load -t "$IMAGE" .

echo "==> Sending image"
docker save "$IMAGE" | gzip | pi "gunzip | docker load"

echo "==> Sending config to $PI_TARGET_DIR/config"
pi "mkdir -p '$PI_TARGET_DIR/config'"
pi "cat > '$PI_TARGET_DIR/config/settings.json'" < config/settings.json

echo "==> Restarting container"
pi "docker rm -f $CONTAINER >/dev/null 2>&1 || true
docker run -d --name $CONTAINER --restart unless-stopped \
  --device /dev/snd --group-add audio -p 8080:8080 \
  -v '$PI_TARGET_DIR/config:/app/config:ro' $IMAGE"

echo "==> Verifying the container stays up"
sleep 8
if [ "$(pi "docker inspect -f '{{.State.Running}} {{.RestartCount}}' $CONTAINER")" != "true 0" ]; then
  echo "Container is not healthy. Last logs:" >&2
  pi "docker logs --tail 50 $CONTAINER" >&2 || true
  exit 1
fi
echo "==> UI: http://$PI_HOST:8080"
echo "==> Deployed. Follow logs with: ssh -p $PI_PORT $PI_USER@$PI_HOST docker logs -f $CONTAINER"
