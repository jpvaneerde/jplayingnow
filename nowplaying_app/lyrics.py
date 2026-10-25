"""Song lyrics from LRCLIB (lrclib.net): free, no account. Many songs have *synced* lyrics there (a time per
line, the LRC format), others only the text.

lookup() answers {"found", "instrumental", "synced": [[seconds, line], ...] | None, "plain": [line, ...] | None}.
Answers are kept in data/lyrics.db, misses too (asked again after a few days), so each song is fetched once.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import unicodedata

import requests

API = "https://lrclib.net/api"
HEADERS = {"User-Agent": "JPlayingNow (https://github.com/jpvaneerde/jplayingnow)"}
RETRY_MISS_AFTER = 3 * 86400       # a song without lyrics is asked again after 3 days
DURATION_SLACK = 4                 # seconds: same recording if the lengths differ by at most this

NOT_FOUND = {"found": False, "instrumental": False, "synced": None, "plain": None}

_db_path: str | None = None
_lock = threading.Lock()


def init(path: str) -> None:
    global _db_path
    _db_path = path
    with _connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS lyrics (key TEXT PRIMARY KEY, answer TEXT, found INTEGER, fetched_at REAL)")


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(_db_path, timeout=10)


# ---------------------------------------------------------------- matching helpers
def norm(s: str) -> str:
    """'Purple Rain (2015 Remaster)' -> 'purple rain'; accents, brackets and punctuation ignored."""
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"\s+-\s+.*$", "", s)                     # "Song - Radio Edit" / "- 2011 Remaster"
    s = re.sub(r"\b(feat|ft|featuring)\b.*$", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def main_artist(artist: str) -> str:
    """'Dua Lipa & Elton John' / 'X feat. Y' / 'A, B' -> 'Dua Lipa'."""
    return re.split(r"\s*(?:,|&|\bx\b|\band\b|\bfeat\.?|\bft\.?|\bfeaturing\b|\bmed\b|\bwith\b)\s*", str(artist or ""), 1,
                    flags=re.I)[0].strip() or str(artist or "")


def cache_key(artist: str, title: str) -> str:
    return f"{norm(main_artist(artist))}|{norm(title)}"


# ---------------------------------------------------------------- LRC
_TIME = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def parse_lrc(text: str) -> list[list]:
    """'[01:02.50] Line' (a line may carry several times) -> [[62.5, 'Line'], ...], in time order."""
    out = []
    for raw in (text or "").splitlines():
        times = _TIME.findall(raw)
        if not times:
            continue                                     # [ar:...], [length:...] and other tags
        line = _TIME.sub("", raw).strip()
        for m, s in times:
            out.append([round(int(m) * 60 + float(s), 2), line])
    out.sort(key=lambda x: x[0])
    return out


def _answer(rec: dict) -> dict:
    synced = parse_lrc(rec.get("syncedLyrics") or "") or None
    plain = [line.rstrip() for line in (rec.get("plainLyrics") or "").strip().splitlines()] or None
    if plain is None and synced:
        plain = [line for _, line in synced]
    return {"found": bool(synced or plain), "instrumental": bool(rec.get("instrumental")),
            "synced": synced, "plain": plain}


def _score(rec: dict, artist: str, title: str, duration: float | None) -> float | None:
    """How well an LRCLIB entry fits the song; None = not this song."""
    if norm(rec.get("trackName")) != norm(title):
        return None
    want, have = norm(main_artist(artist)), norm(rec.get("artistName"))
    if not want or not (want in have or have in want):
        return None
    score = 0.0
    if duration and rec.get("duration"):
        diff = abs(float(rec["duration"]) - duration)
        if diff > DURATION_SLACK * 3:
            return None                                  # another version (live, extended...)
        score -= diff
    if rec.get("syncedLyrics"):
        score += 10
    elif rec.get("plainLyrics"):
        score += 3
    if rec.get("instrumental"):
        score -= 5
    return score


# ---------------------------------------------------------------- lookup
def fetch(artist: str, title: str, album: str = "", duration: float | None = None, session=requests) -> dict:
    """Ask LRCLIB (no cache)."""
    # 1. The exact recording, when the length is known.
    if duration and album:
        r = session.get(f"{API}/get", headers=HEADERS, timeout=8, params={
            "artist_name": artist, "track_name": title, "album_name": album, "duration": round(duration)})
        if r.status_code == 200:
            ans = _answer(r.json())
            if ans["found"] or ans["instrumental"]:
                return ans
        elif r.status_code != 404:
            r.raise_for_status()
    # 2. Search by title and artist, and pick the best fit.
    best, best_score = None, None
    for t in dict.fromkeys([title, re.sub(r"\s*[\(\[].*?[\)\]]", "", title).strip()]):   # also without "(Remastered)"
        r = session.get(f"{API}/search", headers=HEADERS, timeout=8,
                        params={"track_name": t, "artist_name": main_artist(artist)})
        if r.status_code == 404:
            continue
        r.raise_for_status()
        for rec in r.json() or []:
            s = _score(rec, artist, title, duration)
            if s is not None and (best_score is None or s > best_score):
                best, best_score = rec, s
        if best is not None:
            break
    return _answer(best) if best else dict(NOT_FOUND)


def lookup(artist: str, title: str, album: str = "", duration: float | None = None) -> dict:
    """Lyrics for a song, from the cache or LRCLIB. Never raises: a failure is 'not found' (asked again later)."""
    if not artist or not title:
        return dict(NOT_FOUND)
    key = cache_key(artist, title)
    with _lock:
        if _db_path:
            with _connect() as db:
                row = db.execute("SELECT answer, found, fetched_at FROM lyrics WHERE key = ?", (key,)).fetchone()
            if row and (row[1] or time.time() - row[2] < RETRY_MISS_AFTER):
                return json.loads(row[0])
    try:
        ans = fetch(artist, title, album, duration)
    except Exception as e:  # LRCLIB unreachable: try again in an hour, not in 3 days
        print(f"Lyrics lookup failed for {artist} – {title}: {type(e).__name__}: {e}")
        if _db_path:
            with _lock, _connect() as db:
                db.execute("INSERT OR REPLACE INTO lyrics VALUES (?, ?, 0, ?)",
                           (key, json.dumps(NOT_FOUND), time.time() - RETRY_MISS_AFTER + 3600))
        return dict(NOT_FOUND)
    if _db_path:
        with _lock, _connect() as db:
            db.execute("INSERT OR REPLACE INTO lyrics VALUES (?, ?, ?, ?)",
                       (key, json.dumps(ans), int(ans["found"] or ans["instrumental"]), time.time()))
    return ans
