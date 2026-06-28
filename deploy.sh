#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/skachat"
ENV_FILE="$APP_DIR/.env"
IMAGE_NAME="skachat-bot"
CONTAINER_NAME="skachat-bot"

if ! [ -f "$ENV_FILE" ]; then
  echo ".env not found at $ENV_FILE"
  exit 1
fi

if grep -q '^BOT_TOKEN=PASTE_NEW_TOKEN_HERE$' "$ENV_FILE"; then
  echo "Set a real BOT_TOKEN in $ENV_FILE before starting the bot."
  exit 1
fi

cd "$APP_DIR"
docker build -t "$IMAGE_NAME" .
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
docker run -d \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  --env-file "$ENV_FILE" \
  "$IMAGE_NAME"

echo "Container started. Tail logs with:"
echo "docker logs -f $CONTAINER_NAME"
