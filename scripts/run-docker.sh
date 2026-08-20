#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="claude-usage"
CONTAINER="claude-usage"
NETWORK="claude-usage-net"
PORT=9898

echo "▶  Checking for running container..."
if docker ps -q --filter "name=^${CONTAINER}$" | grep -q .; then
  echo "⏹  Stopping ${CONTAINER}..."
  docker stop "$CONTAINER"
fi

echo "🔗  Ensuring isolated network..."
if ! docker network inspect "$NETWORK" &>/dev/null; then
  docker network create \
    --opt com.docker.network.bridge.enable_ip_masquerade=false \
    "$NETWORK"
fi

echo "⬇  Pulling latest..."
cd "$REPO_DIR"
git pull

echo "🔨  Building image..."
docker build -t "$IMAGE" .

echo "🚀  Starting container..."
docker run --rm -d \
  --name "$CONTAINER" \
  --network "$NETWORK" \
  -p "$PORT:8080" \
  -v "$HOME/.claude:/root/.claude:ro" \
  -v "${CONTAINER}-data:/data" \
  -e HOST=0.0.0.0 \
  "$IMAGE"

echo "✅  Running at http://localhost:${PORT}"
