#!/usr/bin/env bash
# One-time setup of the safe updater (run again after changing deploy/updater.conf):
#   sudo bash deploy/install-updater.sh
# Installs, for the app named in deploy/updater.conf (NAME):
#   NAME-update.service         updates to the version the app asked for, restarts it, goes back if it doesn't start
#   NAME-update-check.service   fetches from GitHub and notes the newest release
#   NAME-update-check.timer     runs the check 5 minutes after boot and every 6 hours
#   /etc/sudoers.d/NAME-update  lets the app start exactly these two services, nothing else
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run this with sudo: sudo $0"; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=/dev/null
. "$REPO/deploy/updater.conf"
[[ "$STATE_DIR" = /* ]] || STATE_DIR="$REPO/$STATE_DIR"
OWNER="$(stat -c %U "$REPO")"
APP_USER="${APP_USER:-$OWNER}"
SYSTEMCTL=/usr/bin/systemctl   # the apps call exactly this path
[[ -x $SYSTEMCTL ]] || SYSTEMCTL="$(command -v systemctl)"

mkdir -p "$STATE_DIR"
[[ "$(stat -c %U "$STATE_DIR")" == root ]] && chown "$APP_USER" "$STATE_DIR"

cat > "/etc/$NAME-update.env" <<CONF
APP=$APP
REPO=$REPO
STATE_DIR=$STATE_DIR
SERVICE=$SERVICE
APP_USER=$APP_USER
HEALTH_URL=$HEALTH_URL
DEPS_FILES=$DEPS_FILES
DEPS_CMD=$DEPS_CMD
CONF

for mode in "" "-check"; do
  arg=install; desc="Update $APP"
  [[ -n "$mode" ]] && { arg=check; desc="Check for $APP updates"; }
  cat > "/etc/systemd/system/$NAME-update$mode.service" <<UNIT
[Unit]
Description=$desc
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/etc/$NAME-update.env
ExecStart=/bin/bash $REPO/deploy/update.sh $arg
TimeoutStartSec=20min
UNIT
done

cat > "/etc/systemd/system/$NAME-update-check.timer" <<UNIT
[Unit]
Description=Check for $APP updates a few times a day

[Timer]
OnBootSec=5min
OnUnitActiveSec=6h
RandomizedDelaySec=20min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

tmp="$(mktemp)"
cat > "$tmp" <<SUDO
# $APP may start its own updater (and nothing else) without a password.
$APP_USER ALL=(root) NOPASSWD: $SYSTEMCTL start --no-block $NAME-update.service, $SYSTEMCTL start --no-block $NAME-update-check.service
SUDO
visudo -cf "$tmp" >/dev/null
install -m 440 "$tmp" "/etc/sudoers.d/$NAME-update"
rm -f "$tmp"

systemctl daemon-reload
systemctl enable --now "$NAME-update-check.timer" >/dev/null
systemctl start --no-block "$NAME-update-check.service"
echo "Updater for $APP installed. The app's settings show its version and updates in a minute."
