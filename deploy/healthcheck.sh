#!/usr/bin/env bash
# Every 5 minutes (deploy/liskodi-healthcheck.timer): check the live site from the server itself and send a
# phone alert (ntfy) when something breaks, and again when it recovers. Nothing is sent while all is well.
#   - the site over HTTPS: /healthz/ must say database, cache and scheduler are "ok"
#   - the HTTPS certificate: must have more than 14 days left (Caddy renews at 30)
#   - the disk: under 85% full
# Alerts go to ALERT_NTFY_URL in ~/.config/liskodi/alerts.env (see "Alerts" in README.md).
set -uo pipefail

APP_DIR=${APP_DIR:-/home/ubuntu/app}
STATE_DIR=${STATE_DIR:-$HOME/.local/state/liskodi}
CONFIG=${CONFIG:-$HOME/.config/liskodi/alerts.env}
mkdir -p "$STATE_DIR"
[ -f "$CONFIG" ] && . "$CONFIG"

alert() {  # alert <title> <message> [priority] [tags]
  [ -n "${ALERT_NTFY_URL:-}" ] || return 0
  curl -fsS -m 15 -H "Title: $1" -H "Priority: ${3:-default}" -H "Tags: ${4:-warning}" -d "$2" "$ALERT_NTFY_URL" > /dev/null || true
}

# Report a check only when its state changes: problem -> alert, fixed -> "back to normal".
report() {  # report <name> <ok|problem> <message>
  local file="$STATE_DIR/check-$1" before
  before=$(cat "$file" 2>/dev/null || echo ok)
  if [ "$2" != "$before" ]; then
    if [ "$2" = ok ]; then alert "Fixed: $1" "$3" default white_check_mark
    else alert "Problem: $1" "$3" high rotating_light; fi
    echo "$2" > "$file"
  fi
  echo "$1: $2 ($3)"
}

DOMAIN=$(grep -E '^DOMAIN=' "$APP_DIR/.env_prod" | cut -d= -f2-)

# 1. The site, through Caddy and HTTPS, exactly as visitors reach it.
body=$(curl -sS -m 20 --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/healthz/" 2>&1)
if echo "$body" | grep -q '"status": "ok"' && echo "$body" | grep -q '"scheduler": "ok"'; then
  report site ok "https://$DOMAIN is up."
else
  report site problem "https://$DOMAIN isn't healthy: ${body:0:300}"
fi

# 1b. Off-site backups (from the same health reply). "off" and "pending" aren't problems: not set up yet,
# or no nightly backup since it was.
backups=$(echo "$body" | grep -o '"backups": "[a-z]*"' | cut -d'"' -f4)
case "$backups" in
  failed) report backups problem "Last night's off-site backup copy failed. See: docker compose logs scheduler | grep backup" ;;
  stale) report backups problem "No off-site backup copy for over a day. Is the scheduler running? docker compose logs scheduler" ;;
  ok) report backups ok "Off-site backup copy is up to date." ;;
esac

# 2. The certificate.
end=$(echo | openssl s_client -connect 127.0.0.1:443 -servername "$DOMAIN" 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
if [ -n "$end" ]; then
  days=$(( ($(date -d "$end" +%s) - $(date +%s)) / 86400 ))
  if [ "$days" -gt 14 ]; then report certificate ok "HTTPS certificate valid for $days more days."
  else report certificate problem "HTTPS certificate expires in $days days and hasn't renewed. Check: docker compose logs caddy"; fi
fi

# 3. Disk space.
used=$(df --output=pcent / | tail -1 | tr -dc 0-9)
if [ "$used" -lt 85 ]; then report disk ok "Disk ${used}% full."
else report disk problem "Disk ${used}% full. Old backups, logs or Docker images may need clearing."; fi
