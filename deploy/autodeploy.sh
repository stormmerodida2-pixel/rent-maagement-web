#!/usr/bin/env bash
# Automatic deploys: keeps the live server on the latest main that passed CI.
#
# Run every 2 minutes by deploy/liskodi-autodeploy.timer (installed on the server; see "Automatic deploys" in README.md).
# The server pulls from GitHub, so nothing needs to log in to it: SSH can stay locked to your own address.
#   1. Fetch main. If it hasn't moved, stop.
#   2. Ask GitHub whether CI passed for that commit. Still running: try again next time. Failed: skip it.
#   3. Check it out and rebuild (migrations run as the web container starts). Restart Caddy if its config changed.
#   4. If the new version doesn't come up healthy, go back to the previous one and don't retry that commit.
set -euo pipefail

APP_DIR=${APP_DIR:-/home/ubuntu/app}
REPO=${REPO:-stormmerodida2-pixel/rent-maagement-web}
WORKFLOW=${WORKFLOW:-CI/CD}
ENV_FILE=${ENV_FILE:-.env_prod}
STATE_DIR=${STATE_DIR:-$HOME/.local/state/liskodi}
CONFIG=${CONFIG:-$HOME/.config/liskodi/alerts.env}
mkdir -p "$STATE_DIR"
[ -f "$CONFIG" ] && . "$CONFIG"

alert() {  # alert <title> <message> [priority] [tags]: phone notification via ntfy, if set up
  [ -n "${ALERT_NTFY_URL:-}" ] || return 0
  curl -fsS -m 15 -H "Title: $1" -H "Priority: ${3:-default}" -H "Tags: ${4:-warning}" -d "$2" "$ALERT_NTFY_URL" > /dev/null || true
}

exec 9>"$STATE_DIR/lock"
flock -n 9 || exit 0  # a deploy is already running

cd "$APP_DIR"
git fetch --quiet origin main
target=$(git rev-parse origin/main)
current=$(git rev-parse HEAD)
[ "$target" = "$current" ] && exit 0
[ "$(cat "$STATE_DIR/failed" 2>/dev/null)" = "$target" ] && exit 0  # already failed once: wait for a fix

ci=$(curl -fsS -m 20 "https://api.github.com/repos/$REPO/actions/runs?head_sha=$target&event=push&per_page=20" \
  | python3 -c '
import json, sys
runs = [r for r in json.load(sys.stdin).get("workflow_runs", []) if r["name"] == sys.argv[1]]
if not runs or runs[0]["status"] != "completed":
    print("pending")
else:
    print("success" if runs[0]["conclusion"] == "success" else "failed")
' "$WORKFLOW") || ci=pending

case "$ci" in
  pending) exit 0 ;;
  failed)
    echo "Not deploying ${target:0:7}: CI did not pass."
    echo "$target" > "$STATE_DIR/failed"
    alert "Not deployed: tests failed" "${target:0:7} ($(git log -1 --format=%s "$target")) failed its checks on GitHub, so the live site stays on ${current:0:7}." high x
    exit 0 ;;
esac

compose() { docker compose --env-file "$ENV_FILE" "$@"; }

echo "Deploying ${target:0:7} (live: ${current:0:7})"
caddy_changed=$(git diff --quiet "$current" "$target" -- Caddyfile && echo no || echo yes)
git checkout --quiet --detach "$target"
if compose up -d --build --remove-orphans --wait --wait-timeout 600; then
  [ "$caddy_changed" = yes ] && compose restart caddy > /dev/null
  docker image prune -f > /dev/null
  echo "Deployed ${target:0:7}: $(git log -1 --format=%s)"
  alert "Deployed" "$(git log -1 --format=%s) (${target:0:7}) is live." low rocket
else
  echo "Deploy of ${target:0:7} failed; going back to ${current:0:7}." >&2
  alert "Deploy failed" "${target:0:7} didn't come up healthy, so the site went back to ${current:0:7}. See: journalctl -u liskodi-autodeploy" urgent rotating_light
  echo "$target" > "$STATE_DIR/failed"
  git checkout --quiet --detach "$current"
  compose up -d --build --remove-orphans --wait --wait-timeout 600 || true
  exit 1
fi
