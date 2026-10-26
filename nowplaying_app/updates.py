"""Versions and updates, from the app's side (the same module in player-jplay and jplayingnow).

The version comes from git tags (v1.6.0); `git describe` gives "1.6.0", or "1.6.0+3" for 3 commits after it.

The app never updates itself: it asks its separate updater (deploy/update.sh, run by systemd as root) through
two services it may start with sudo (see deploy/install-updater.sh):
  NAME-update-check.service   fetch from GitHub, note the newest release (also runs every 6 hours by itself)
  NAME-update.service         update to the version in update-request.json, restart, go back if it fails
The updater reports in STATE_DIR/update-status.json, which status() turns into what Settings shows.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime

TAG = re.compile(r"^v\d+\.\d+\.\d+$")
SYSTEMCTL = "/usr/bin/systemctl" if os.path.exists("/usr/bin/systemctl") else (shutil.which("systemctl") or "systemctl")
STALE_AFTER = 25 * 60          # an "updating" status older than this means the updater died
NIGHT = (3, 5)                 # automatic updates between 03:00 and 05:00 (local time)
AUTO_RETRY_AFTER = 20 * 3600   # at most one automatic attempt per night


class UpdateError(Exception):
    pass


def pretty(describe: str | None) -> str:
    """'v1.6.0' -> '1.6.0', 'v1.6.0-3-gab12cd3' -> '1.6.0+3', 'ab12cd3' (no release yet) -> 'dev-ab12cd3'."""
    d = (describe or "").strip()
    if not d:
        return "unknown"
    dirty = d.endswith("-dirty")
    d = d.removesuffix("-dirty")
    m = re.match(r"^v?(\d+\.\d+\.\d+)(?:-(\d+)-g[0-9a-f]+)?$", d)
    if m:
        out = m.group(1) + (f"+{m.group(2)}" if m.group(2) else "")
    else:
        out = f"dev-{d[:7]}"
    return out + (" (changed)" if dirty else "")


class Updates:
    def __init__(self, name: str, repo: str, state_dir: str):
        self.name = name                       # e.g. "jplayingnow": the updater's services are NAME-update*.service
        self.repo = repo
        self.state_dir = state_dir
        self._version: tuple[float, dict] | None = None

    # ------------------------------------------------------------ what's installed
    def version(self) -> dict:
        """{'version': '1.6.0+3', 'describe': 'v1.6.0-3-gab12cd3', 'commit': 'ab12cd3'} (cached half a minute)."""
        if self._version and time.time() - self._version[0] < 30:
            return self._version[1]
        git = ["git", "-c", f"safe.directory={self.repo}", "-C", self.repo]
        describe = commit = ""
        try:
            describe = subprocess.run(git + ["describe", "--tags", "--always"], capture_output=True, text=True,
                                      timeout=5).stdout.strip()
            commit = subprocess.run(git + ["rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                                    timeout=5).stdout.strip()
        except Exception:
            pass
        v = {"version": pretty(describe), "describe": describe, "commit": commit}
        self._version = (time.time(), v)
        return v

    def supported(self) -> bool:
        """Is the updater set up on this Pi (sudo bash deploy/install-updater.sh)?"""
        return os.path.exists(f"/etc/systemd/system/{self.name}-update.service")

    # ------------------------------------------------------------ what the updater says
    def _read(self, name: str) -> dict:
        try:
            with open(os.path.join(self.state_dir, name)) as f:
                return json.load(f)
        except Exception:
            return {}

    def _write(self, name: str, data: dict) -> None:
        path = os.path.join(self.state_dir, name)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)

    def status(self, channel: str = "releases") -> dict:
        """Everything Settings (and Home Assistant) shows about versions and updates."""
        s = self._read("update-status.json")
        v = self.version()
        now = time.time()
        state = s.get("state") or "idle"
        if state in ("updating", "checking") and now - (s.get("updated_at") or 0) > STALE_AFTER:
            state = "idle"
        main = s.get("main") or {}
        if channel == "main":
            available = main.get("included") is False
            latest = pretty(main.get("describe")) if main.get("describe") else None
            target = "main" if available else None
            notes = f"Newest change: {main.get('subject')}" if available and main.get("subject") else ""
        else:
            tag = s.get("latest_tag")
            available = bool(tag) and s.get("latest_tag_included") is False
            latest = pretty(tag) if tag else None
            target = tag if available else None
            notes = (s.get("notes") or "").strip() if available else ""
        if state == "updating" and s.get("target"):
            target = s.get("target")
        if self._version and s.get("finished_at") and s["finished_at"] > self._version[0]:
            self._version = None                       # just updated: read the version again
            v = self.version()
        return {
            "supported": self.supported(),
            "version": v["version"],
            "describe": v["describe"],
            "commit": v["commit"],
            "channel": channel,
            "latest": latest if available else v["version"],
            "available": available,
            "target": target,
            "notes": notes,
            "checked_at": s.get("checked_at"),
            "state": state,                            # idle | checking | updating | done | failed | rolled_back
            "in_progress": state in ("updating", "checking"),
            "step": s.get("step"),
            "message": s.get("message") or "",
            "finished_at": s.get("finished_at"),
            "log": (s.get("log") or [])[-12:],
        }

    # ------------------------------------------------------------ asking the updater
    def _start(self, unit: str) -> None:
        if not self.supported():
            raise UpdateError("The updater isn't set up on this Pi yet: run sudo bash deploy/install-updater.sh once.")
        res = subprocess.run(["sudo", "-n", SYSTEMCTL, "start", "--no-block", unit],
                             capture_output=True, text=True, timeout=15)
        if res.returncode != 0:
            raise UpdateError((res.stderr or res.stdout).strip()[-300:] or f"Couldn't start {unit}")

    def check(self) -> None:
        self._start(f"{self.name}-update-check.service")

    def install(self, target: str) -> None:
        if not (TAG.match(target or "") or target == "main"):
            raise UpdateError("Nothing to update to")
        self._write("update-request.json", {"target": target, "asked_at": time.time()})
        self._start(f"{self.name}-update.service")

    # ------------------------------------------------------------ automatic updates
    def auto_target(self, auto: str, channel: str, busy: bool, now: datetime | None = None) -> str | None:
        """The version to install now, if automatic updates are on, it is night, nothing plays and an update waits."""
        if auto != "night" or busy or not self.supported():
            return None
        now = now or datetime.now()
        if not (NIGHT[0] <= now.hour < NIGHT[1]):
            return None
        st = self.status(channel)
        if not st["available"] or st["in_progress"] or not st["target"]:
            return None
        last = self._read("update-auto.json").get("tried_at") or 0
        if time.time() - last < AUTO_RETRY_AFTER:
            return None
        self._write("update-auto.json", {"tried_at": time.time(), "target": st["target"]})
        return st["target"]
