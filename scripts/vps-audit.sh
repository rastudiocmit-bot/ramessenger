#!/bin/sh
# Read-only inspection. Never reveal secrets or modify running services.
set -eu
echo "=== machine ==="
uname -s -m
uptime
df -h /
free -h
echo "=== listeners ==="
ss -lnt | grep -E '(:80|:443|:8100|:3000)([[:space:]]|$)' || true
echo "=== docker ==="
if command -v docker >/dev/null 2>&1; then
  docker --version
  docker compose version
  docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' || true
fi
echo "=== messenger deployment unit states ==="
if command -v systemctl >/dev/null 2>&1; then
  systemctl is-active center-messenger-deploy.timer || true
  systemctl is-active center-messenger-deploy.service || true
  systemctl is-active nginx || true
fi
echo "=== local readiness ==="
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8100/health/ready || true
echo
echo "=== public website status ==="
curl --silent --show-error --location --output /dev/null --write-out 'HTTP %{http_code}\n' --max-time 10 https://rastudio.org || true
