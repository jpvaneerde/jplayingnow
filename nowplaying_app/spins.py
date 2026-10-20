"""JPlay Spins client: tell JPlay Records what this room hears and plays.

Events are queued in a local outbox (the same SQLite file as the history) and sent in batches, so nothing is
lost while the network or the server is down. The server decides what each event means (which room, physical
record or stream); this side only reports:

  mic      the microphone recognised a song (title, artist, album, cover, loudness)
  mic_end  that song is no longer heard
  room     this room's JPlay player changed state: play/pause/stop, the source (radio, plex, cd, spotify,
           bluetooth), the radio station and the song if known (for radio also songs recognised from the
           stream); repeated every few minutes as a heartbeat
"""
import json
import sqlite3
import threading
import time
import uuid

import requests

DB_PATH = None
HEARTBEAT_SECONDS = 300
KEEP_SECONDS = 7 * 86400     # unsent events older than this are dropped
MAX_QUEUED = 20000
_lock = threading.Lock()
_room = {"sig": None, "sent": 0.0}
status = {"queued": 0, "last_ok": None, "last_error": "", "last_attempt": None}


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init(path):
    global DB_PATH
    DB_PATH = path
    with _connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS spin_outbox (
                id TEXT PRIMARY KEY,
                body TEXT NOT NULL,
                created REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0
            )"""
        )
    _count()


def _count():
    with _connect() as conn:
        status["queued"] = conn.execute("SELECT COUNT(*) FROM spin_outbox").fetchone()[0]


def enabled(cfg):
    return bool(cfg.get("spins_enabled") and cfg.get("spins_url") and cfg.get("spins_token"))


def queue(event_type, ts, data):
    """Add an event to the outbox; returns its id."""
    if DB_PATH is None:
        return None
    eid = str(uuid.uuid4())
    body = json.dumps({"id": eid, "type": event_type, "ts": ts, "data": data})
    with _lock, _connect() as conn:
        conn.execute("INSERT INTO spin_outbox (id, body, created) VALUES (?, ?, ?)", (eid, body, time.time()))
    status["queued"] += 1
    return eid


def room_update(data):
    """Called by the player poller with this room's player state (players.spins_room_data);
    queues a 'room' event when it changes, and as a heartbeat."""
    sig = json.dumps(data, sort_keys=True)
    now = time.time()
    if sig != _room["sig"] or now - _room["sent"] >= HEARTBEAT_SECONDS:
        queue("room", now, data)
        _room.update(sig=sig, sent=now)


def send_pending(cfg, batch=100):
    """Send queued events. Returns the number sent; errors end up in status['last_error']."""
    if DB_PATH is None or not enabled(cfg):
        return 0
    with _connect() as conn:
        conn.execute("DELETE FROM spin_outbox WHERE created < ?", (time.time() - KEEP_SECONDS,))
        conn.execute(
            "DELETE FROM spin_outbox WHERE id NOT IN (SELECT id FROM spin_outbox ORDER BY created DESC LIMIT ?)",
            (MAX_QUEUED,),
        )
        rows = conn.execute("SELECT id, body FROM spin_outbox ORDER BY created LIMIT ?", (batch,)).fetchall()
    if not rows:
        status["queued"] = 0
        return 0
    status["last_attempt"] = time.time()
    url = cfg["spins_url"].rstrip("/") + "/api/spins/events"
    try:
        r = requests.post(
            url,
            json={"events": [json.loads(row["body"]) for row in rows]},
            headers={"Authorization": f"Bearer {cfg['spins_token']}"},
            timeout=10,
        )
    except Exception as e:
        status["last_error"] = f"{type(e).__name__}: {e}"
        return 0
    if r.status_code == 401:
        status["last_error"] = "The server doesn't know this token. Create a listener in JPlay Records → Settings → Listeners."
        return 0
    if not r.ok:
        status["last_error"] = f"HTTP {r.status_code}: {r.text[:200]}"
        with _connect() as conn:
            conn.executemany("UPDATE spin_outbox SET attempts = attempts + 1 WHERE id = ?", [(row["id"],) for row in rows])
        return 0
    with _lock, _connect() as conn:
        conn.executemany("DELETE FROM spin_outbox WHERE id = ?", [(row["id"],) for row in rows])
    status["last_ok"] = time.time()
    status["last_error"] = ""
    _count()
    return len(rows)
