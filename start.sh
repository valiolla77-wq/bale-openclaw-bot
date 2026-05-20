#!/bin/bash

# Create log file
touch /app/openclaw.log

echo "--- Step 1: Running OpenClaw Gateway ---"

# Detect OpenClaw path or use command name
OPENCLAW_PATH=$(which openclaw)
if [ -z "$OPENCLAW_PATH" ]; then
    if [ -f "/usr/local/bin/openclaw" ]; then
        OPENCLAW_PATH="/usr/local/bin/openclaw"
    elif [ -f "/usr/bin/openclaw" ]; then
        OPENCLAW_PATH="/usr/bin/openclaw"
    else
        echo "Error: openclaw command not found in common paths."
        exit 1
    fi
fi

echo "Using OpenClaw from: $OPENCLAW_PATH"
$OPENCLAW_PATH gateway --port 18789 >> /app/openclaw.log 2>&1 &

echo "--- Step 2: Checking health of port 18789 ---"
MAX_RETRIES=30
COUNT=0

while ! nc -z 127.0.0.1 18789; do
  COUNT=$((COUNT + 1))
  if [ $COUNT -ge $MAX_RETRIES ]; then
    echo "Error: OpenClaw failed to start within 60 seconds."
    cat /app/openclaw.log
    exit 1
  fi
  echo "Waiting for OpenClaw to be ready... ($COUNT/$MAX_RETRIES)"
  sleep 2
done

echo "OpenClaw is ready."

echo "--- Step 3: Running Python Bot ---"
exec python3 /app/bot.py
