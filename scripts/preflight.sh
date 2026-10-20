#!/bin/sh
# Read-only inventory. No installation, restart, config dump, tokens or process args.
set -eu
uptime
free -h
df -h /
ss -lnt
if command -v docker >/dev/null 2>&1; then
  docker --version
  docker compose version
  docker ps --format 'table {{.Names}}\t{{.Ports}}\t{{.Status}}'
fi
if ss -H -lnt | awk '{print $4}' | grep -Eq '(^|:)8100$'; then
  echo 'Port 8100 is occupied. Do not start messenger until its owner is identified.' >&2
  exit 1
fi
echo 'Port 8100 is free in this snapshot; this is not a resource/load approval.'
