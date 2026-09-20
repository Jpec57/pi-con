#!/usr/bin/env sh
set -eu

IMAGE="${IMAGE:-pi-con:latest}"
PLATFORM="${PLATFORM:-linux/arm64}"
docker buildx build --platform "$PLATFORM" --load -t "$IMAGE" .
