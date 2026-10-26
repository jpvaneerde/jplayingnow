#!/usr/bin/env bash
# Safe updater for a JPlay app on a Pi (the same script in player-jplay and jplayingnow).
#
# Runs as root from its own systemd units, never from the app itself (an app that pulls and restarts itself
# dies halfway):
#   update.sh check     fetch from GitHub and note the newest release (the timer runs this a few times a day)
#   update.sh install   update to the version the app asked for (STATE_DIR/update-request.json), then restart
#                       the app and check it answers; if it doesn't, go back to the previous version.
#
# Git runs as the user who owns the repo, with that user's access, so private repos need nothing extra.
# Only fast-forwards: local edits are never overwritten, the branch stays on main (a manual `git pull` still works).
# Progress and results go to STATE_DIR/update-status.json, which the app shows.
#
# Settings (from the systemd unit):
#   APP          name shown in messages, e.g. "JPlaying Now"
#   REPO         the git checkout, e.g. /home/jp/jplayingnow
#   STATE_DIR    where the app and this script exchange update-request.json / update-status.json
#   SERVICE      the app's systemd unit, restarted after an update
#   HEALTH_URL   answers 200 once the app runs, e.g. http://127.0.0.1:5000/api/version
#   DEPS_FILES   files that, when changed, need DEPS_CMD, e.g. "requirements.txt"
#   DEPS_CMD     installs the Python packages (run in REPO as the repo's owner)
#   RESTART_CMD  optional; default: systemctl restart "$SERVICE"
#   APP_USER     optional; who owns the status file (default: the repo's owner)
set -uo pipefail

MODE="${1:-check}"
: "${APP:=JPlay}" "${REPO:?REPO not set}" "${STATE_DIR:?STATE_DIR not set}" "${HEALTH_URL:=}" "${DEPS_FILES:=}" "${DEPS_CMD:=}"
: "${HEALTH_TIMEOUT:=90}" "${RESTART_CMD:=systemctl restart ${SERVICE:-}}"
STATUS="$STATE_DIR/update-status.json"
REQUEST="$STATE_DIR/update-request.json"
LOCK="${LOCK_FILE:-/run/lock/$(basename "$REPO")-update.lock}"

# Bash reads a script while it runs; the update may change this very file. Run from a private copy.
if [[ -z "${JPLAY_UPDATE_COPY:-}" ]]; then
  copy="$(mktemp /tmp/jplay-update.XXXXXX)"
  cp "$0" "$copy"
  JPLAY_UPDATE_COPY="$copy" exec bash "$copy" "$@"
fi
trap 'rm -f "$JPLAY_UPDATE_COPY"' EXIT

OWNER="$(stat -c %U "$REPO")"
APP_USER="${APP_USER:-$OWNER}"
LOG=()

as_owner() {
  # Git and pip as the repo's owner (root for /opt/player-jplay). safe.directory: root may work in a user's repo.
  if [[ "$(id -un)" == "$OWNER" ]]; then
    (cd "$REPO" && "$@")
  else
    runuser -u "$OWNER" -- bash -c 'cd "$0" && exec "$@"' "$REPO" "$@"
  fi
}
git_() { as_owner git -c safe.directory="$REPO" "$@"; }

say() { LOG+=("$(date '+%H:%M:%S') $*"); echo "$*"; }

json_str() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"; }

# Write the status file atomically: state, step, message, and everything known about the versions.
write_status() {
  local state="$1" step="$2" message="$3"
  local tmp; tmp="$(mktemp "$STATE_DIR/.update-status.XXXXXX")"
  local log_json; log_json="$(printf '%s\n' "${LOG[@]:-}" | tail -n 30 | python3 -c 'import json,sys; print(json.dumps([l for l in sys.stdin.read().splitlines() if l]))')"
  STATE="$state" STEP="$step" MESSAGE="$message" LOG_JSON="$log_json" OLD_STATUS="$STATUS" \
  CURRENT="$(git_ describe --tags --always --dirty 2>/dev/null)" COMMIT="$(git_ rev-parse --short HEAD 2>/dev/null)" \
  LATEST_TAG="${LATEST_TAG:-}" LATEST_TAG_INCLUDED="${LATEST_TAG_INCLUDED:-}" NOTES="${NOTES:-}" \
  MAIN_COMMIT="${MAIN_COMMIT:-}" MAIN_DESCRIBE="${MAIN_DESCRIBE:-}" MAIN_SUBJECT="${MAIN_SUBJECT:-}" MAIN_INCLUDED="${MAIN_INCLUDED:-}" \
  CHECKED="${CHECKED:-}" PREVIOUS="${PREVIOUS:-}" TARGET="${TARGET:-}" python3 - "$tmp" <<'PY'
import json, os, sys, time
e = os.environ
try:
    old = json.load(open(e["OLD_STATUS"]))
except Exception:
    old = {}
def b(v):
    return {"1": True, "0": False}.get(v)
s = dict(old)
s.update(state=e["STATE"], step=e["STEP"], message=e["MESSAGE"], log=json.loads(e["LOG_JSON"]), updated_at=time.time(),
         current=e["CURRENT"], commit=e["COMMIT"])
if e["CHECKED"]:
    s.update(checked_at=time.time(),
             latest_tag=e["LATEST_TAG"] or None, latest_tag_included=b(e["LATEST_TAG_INCLUDED"]), notes=e["NOTES"],
             main={"commit": e["MAIN_COMMIT"], "describe": e["MAIN_DESCRIBE"], "subject": e["MAIN_SUBJECT"],
                   "included": b(e["MAIN_INCLUDED"])})
if e["STATE"] == "updating" and e["STEP"] == "starting":
    s.update(started_at=time.time(), target=e["TARGET"], previous=e["PREVIOUS"], finished_at=None)
if e["STATE"] in ("done", "failed", "rolled_back"):
    s["finished_at"] = time.time()
json.dump(s, open(sys.argv[1], "w"), indent=1)
PY
  chmod 644 "$tmp"
  chown "$APP_USER" "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$STATUS"
}

# What's on GitHub: the newest release tag (vX.Y.Z), its notes (and those of releases in between), and main.
look() {
  LATEST_TAG="$(git_ tag -l 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname | head -n1)"
  LATEST_TAG_INCLUDED=""
  NOTES=""
  if [[ -n "$LATEST_TAG" ]]; then
    if git_ merge-base --is-ancestor "$LATEST_TAG" HEAD; then LATEST_TAG_INCLUDED=1; else LATEST_TAG_INCLUDED=0; fi
    # Notes of every release this Pi doesn't have yet, newest first
    local t
    while read -r t; do
      [[ -z "$t" ]] && continue
      git_ merge-base --is-ancestor "$t" HEAD && continue
      NOTES+="## ${t#v}"$'\n'"$(git_ tag -l --format='%(contents)' "$t" | sed '/^-----BEGIN PGP/,$d')"$'\n\n'
    done < <(git_ tag -l 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname | head -n 10)
  fi
  MAIN_COMMIT="$(git_ rev-parse --short origin/main 2>/dev/null)"
  MAIN_DESCRIBE="$(git_ describe --tags --always origin/main 2>/dev/null)"
  MAIN_SUBJECT="$(git_ log -1 --format=%s origin/main 2>/dev/null)"
  if git_ merge-base --is-ancestor origin/main HEAD 2>/dev/null; then MAIN_INCLUDED=1; else MAIN_INCLUDED=0; fi
  CHECKED=1
}

fetch() {
  local out
  if ! out="$(git_ fetch --tags --prune --force origin 2>&1)"; then
    say "Fetching from GitHub failed: ${out##*$'\n'}"
    return 1
  fi
}

healthy() {
  [[ -z "$HEALTH_URL" ]] && { sleep 3; return 0; }
  local i
  for ((i = 0; i < HEALTH_TIMEOUT; i += 2)); do
    if python3 -c 'import sys,urllib.request; urllib.request.urlopen(sys.argv[1], timeout=3)' "$HEALTH_URL" 2>/dev/null; then
      return 0
    fi
    sleep 2
  done
  return 1
}

deps_changed() {
  local from="$1" to="$2" f
  [[ -z "$DEPS_CMD" ]] && return 1
  for f in $DEPS_FILES; do
    if ! git_ diff --quiet "$from" "$to" -- "$f"; then return 0; fi
  done
  return 1
}

install_deps() {
  say "Installing Python packages"
  as_owner bash -c "$DEPS_CMD" >/tmp/jplay-update-deps.log 2>&1 9>&-
}

restart() {
  say "Restarting $APP"
  bash -c "$RESTART_CMD" 9>&-   # nothing we start may hold on to the update lock
}

do_check() {
  write_status checking fetching "Checking for updates"
  if ! fetch; then
    write_status idle failed_check "Couldn't reach GitHub"
    return 1
  fi
  look
  if [[ -n "$LATEST_TAG" && "$LATEST_TAG_INCLUDED" == 0 ]]; then
    say "New release: ${LATEST_TAG#v}"
  else
    say "Up to date"
  fi
  write_status idle checked "Checked for updates"
}

do_install() {
  # The app asked for: {"target": "v1.6.0"} (a release) or {"target": "main"} (the newest commit)
  TARGET="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("target",""))' "$REQUEST" 2>/dev/null)"
  if [[ ! "$TARGET" =~ ^(v[0-9]+\.[0-9]+\.[0-9]+|main)$ ]]; then
    say "Unknown update target '$TARGET'"
    write_status failed request "Nothing to update to"
    return 1
  fi
  PREVIOUS="$(git_ rev-parse HEAD)"
  write_status updating starting "Updating to ${TARGET#v}"
  say "Updating $APP to ${TARGET#v} (now on $(git_ describe --tags --always))"

  write_status updating fetching "Fetching ${TARGET#v}"
  if ! fetch; then
    write_status failed fetching "Couldn't reach GitHub; nothing changed"
    return 1
  fi
  look
  local ref="$TARGET"
  [[ "$TARGET" == main ]] && ref="origin/main"
  if ! git_ rev-parse --verify --quiet "$ref^{commit}" >/dev/null; then
    write_status failed fetching "${TARGET#v} doesn't exist on GitHub; nothing changed"
    return 1
  fi
  if git_ merge-base --is-ancestor "$ref" HEAD; then
    say "Already on ${TARGET#v} or newer"
    write_status done unchanged "Already up to date"
    return 0
  fi
  if [[ -n "$(git_ status --porcelain --untracked-files=no)" ]]; then
    say "Files in $REPO were changed by hand: $(git_ status --porcelain --untracked-files=no | head -n 3 | tr '\n' ' ')"
    write_status failed local_changes "Files on the Pi were changed by hand; nothing changed (undo them, or update with git)"
    return 1
  fi
  local branch; branch="$(git_ rev-parse --abbrev-ref HEAD)"
  if [[ "$branch" != main ]]; then
    write_status failed branch "The Pi isn't on the main branch ($branch); nothing changed"
    return 1
  fi

  write_status updating applying "Installing ${TARGET#v}"
  if ! git_ merge --ff-only --quiet "$ref" 2>/tmp/jplay-update-merge.log; then
    say "Can't fast-forward: $(tail -n 1 /tmp/jplay-update-merge.log)"
    write_status failed applying "This Pi has commits GitHub doesn't; nothing changed"
    return 1
  fi
  local deps=0
  if deps_changed "$PREVIOUS" HEAD; then
    deps=1
    write_status updating packages "Installing Python packages"
    if ! install_deps; then
      say "Package install failed: $(tail -n 2 /tmp/jplay-update-deps.log | tr '\n' ' ')"
      rollback "$deps" "installing the Python packages failed"
      return 1
    fi
  fi
  local f
  while read -r f; do
    [[ -n "$f" ]] && say "Note: $f changed; systemd files are only installed by the setup steps"
  done < <(git_ diff --name-only "$PREVIOUS" HEAD -- 'deploy/*.service' 'deploy/*.timer')

  write_status updating restarting "Restarting $APP"
  restart
  write_status updating verifying "Waiting for $APP to start"
  if healthy; then
    say "$APP runs version $(git_ describe --tags --always)"
    look
    write_status done done "Updated to $(git_ describe --tags --always | sed 's/^v//')"
    return 0
  fi
  rollback "$deps" "$APP didn't start"
  return 1
}

rollback() {
  local deps="$1" why="$2"
  say "$why; going back to $(git_ describe --tags --always "$PREVIOUS")"
  write_status updating rolling_back "$why; going back"
  git_ reset --hard --quiet "$PREVIOUS"
  [[ "$deps" == 1 ]] && install_deps
  restart
  if healthy; then
    look
    write_status rolled_back rolled_back "Update failed ($why); back on $(git_ describe --tags --always | sed 's/^v//')"
  else
    write_status failed rolled_back "Update failed ($why) and $APP doesn't start on the previous version either; see journalctl"
  fi
}

mkdir -p "$STATE_DIR"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "Another update or check is running"
  exit 0
fi
case "$MODE" in
  check) do_check ;;
  install) do_install ;;
  *) echo "usage: $0 check|install"; exit 2 ;;
esac
