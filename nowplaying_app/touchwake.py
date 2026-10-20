"""Notice touches (and keys, mouse) on the Pi itself, to wake the screen.

When JPlaying Now switches the screen off (wlopm / wlr-randr), the desktop doesn't switch it back on when the
screen is touched. So this reads the input devices directly (/dev/input/event*) and tells the screen manager.
The user running JPlaying Now must be in the `input` group (the Pi's first user is, by default).
"""
from __future__ import annotations

import glob
import os
import select
import struct
import threading
import time

# struct input_event: a timeval (two longs), then type, code, value
EVENT = struct.Struct("llHHi")
EV_KEY, EV_REL, EV_ABS = 0x01, 0x02, 0x03
ABS_MT_TRACKING_ID = 0x39
RESCAN_SECONDS = 15          # pick up a screen or mouse plugged in later


class InputWatcher:
    """Calls `on_input()` (from its own thread) whenever someone touches the screen, presses a key or moves the mouse."""

    def __init__(self, on_input):
        self.on_input = on_input
        self.fds: dict[str, int] = {}      # device path -> open file
        self.denied: set[str] = set()
        self.last_input = 0.0
        self._thread: threading.Thread | None = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="touch-wake", daemon=True)
            self._thread.start()

    def status(self) -> dict:
        return {
            "devices": len(self.fds),
            "no_permission": len(self.denied),
            "seconds_since_touch": round(time.time() - self.last_input) if self.last_input else None,
        }

    def _scan(self):
        for path in glob.glob("/dev/input/event*"):
            if path in self.fds:
                continue
            try:
                self.fds[path] = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                self.denied.discard(path)
            except PermissionError:
                if path not in self.denied:
                    self.denied.add(path)
                    print(f"[screen] can't read {path}: add this user to the 'input' group "
                          "(sudo usermod -aG input $USER, then reboot) so a touch wakes the screen")
            except OSError:
                pass

    def _close(self, path):
        fd = self.fds.pop(path, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    @staticmethod
    def _is_touch(data: bytes) -> bool:
        for i in range(0, len(data) - EVENT.size + 1, EVENT.size):
            _, _, etype, code, value = EVENT.unpack_from(data, i)
            if etype == EV_KEY and value == 1:                           # finger down, key or button press
                return True
            if etype == EV_ABS and code == ABS_MT_TRACKING_ID and value >= 0:   # a new finger (multi-touch)
                return True
            if etype == EV_REL:                                          # mouse moved
                return True
        return False

    def _run(self):
        next_scan = 0.0
        while True:
            try:
                now = time.monotonic()
                if now >= next_scan:
                    self._scan()
                    next_scan = now + RESCAN_SECONDS
                if not self.fds:
                    time.sleep(RESCAN_SECONDS)
                    continue
                by_fd = {fd: path for path, fd in self.fds.items()}
                ready, _, _ = select.select(list(by_fd), [], [], RESCAN_SECONDS)
                touched = False
                for fd in ready:
                    try:
                        data = os.read(fd, EVENT.size * 64)
                    except BlockingIOError:
                        continue
                    except OSError:                    # unplugged
                        self._close(by_fd[fd])
                        continue
                    if data and self._is_touch(data):
                        touched = True
                if touched:
                    self.last_input = time.time()
                    self.on_input()
            except Exception as e:  # never let the watcher die
                print(f"[screen] input watcher error: {type(e).__name__}: {e}")
                time.sleep(5)
