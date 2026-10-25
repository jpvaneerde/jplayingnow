"""Song lyrics. Many songs have *synced* lyrics (a time per line, the LRC format), others only the text.

Where they come from, in order:
1. JPlay Records (the JPlay Spins address and token from Settings): the lyrics store for the whole house. It
   looks songs up on LRCLIB itself and keeps them, with corrections (a song's timing) made there.
2. When JPlay Records isn't set up or can't be reached: this unit's own copy (data/lyrics.db), then LRCLIB
   (lrclib.net: free, no account) directly.

lookup() answers {"found", "instrumental", "synced": [[seconds, line], ...] | None, "plain": [line, ...] | None,
"offset"}: offset (seconds) is the song's timing correction, added to the position before picking the line.
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

NOT_FOUND = {"found": False, "instrumental": False, "synced": None, "plain": None, "offset": 0}

_db_path: str | None = None
_lock = threading.Lock()
_failed: dict[str, float] = {}     # song -> when LRCLIB last failed for it (memory only, never stored)
RETRY_FAILED_AFTER = 45            # seconds


# Raised whenever the matching gets better: songs not found with an older version are looked up again.
MATCHER_VERSION = 6


def init(path: str) -> None:
    global _db_path
    _db_path = path
    with _connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS lyrics (key TEXT PRIMARY KEY, answer TEXT, found INTEGER, fetched_at REAL)")
        if db.execute("PRAGMA user_version").fetchone()[0] < MATCHER_VERSION:
            # misses, and answers "instrumental" (stored as found by older versions)
            db.execute("""DELETE FROM lyrics WHERE found = 0 OR answer LIKE '%"found": false%'""")
            db.execute(f"PRAGMA user_version = {MATCHER_VERSION}")


def title_variants(title: str) -> list[str]:
    """How to search for a title: as it is, without '(Remastered)' and the like, and without punctuation
    (LRCLIB's search matches whole words, so 'CHANEL$' wouldn't find 'CHANEL')."""
    plain = re.sub(r"\s*[\(\[].*?[\)\]]", "", title).strip()
    bare = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", plain)).strip()
    return [t for t in dict.fromkeys([title, plain, bare]) if t]


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(_db_path, timeout=10)


# ---------------------------------------------------------------- matching helpers
def norm(s: str) -> str:
    """'Purple Rain (2015 Remaster)' -> 'purple rain', 'Björk' -> 'bjork', '分かってないよ' stays.

    Accents, brackets and punctuation are ignored; letters of every script (Japanese, Korean, Cyrillic...) are kept.
    Full-width characters become ordinary ones, so 'ＴＥＳＴ（Live）' and 'TEST (Live)' compare equal.
    """
    s = unicodedata.normalize("NFKD", str(s or ""))
    out, base = [], ""
    for c in s:
        if unicodedata.combining(c) and base and base < "\u0250":   # an accent on a Latin letter: drop it
            continue
        out.append(c)
        if not unicodedata.combining(c):
            base = c
    s = unicodedata.normalize("NFC", "".join(out)).lower()        # が, 방 etc. put back together
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"\s+-\s+.*$", "", s)                     # "Song - Radio Edit" / "- 2011 Remaster"
    s = re.sub(r"\b(feat|ft|featuring)\b.*$", "", s)
    s = re.sub(r"[\W_]+", " ", s)                         # \W: anything that isn't a letter or digit, in any script
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
            "synced": synced, "plain": plain, "offset": 0}


def _by_length(rec: dict, artist: str, album: str, duration: float | None) -> float | None:
    """For a song whose title is written differently (Japanese script vs romaji or English): the same artist
    (or album) and the same length to within 2 seconds. None = not this song."""
    if not duration or not rec.get("duration") or not (rec.get("syncedLyrics") or rec.get("plainLyrics")):
        return None
    diff = abs(float(rec["duration"]) - duration)
    if diff > 2:
        return None
    want, have = norm(main_artist(artist)), norm(rec.get("artistName"))
    same_artist = bool(want) and (want in have or have in want)
    same_album = bool(norm(album)) and norm(album) == norm(rec.get("albumName"))
    if not (same_artist or same_album):
        return None
    score = -diff * 3
    if rec.get("syncedLyrics"):
        score += 10
    elif rec.get("plainLyrics"):
        score += 3
    if rec.get("instrumental"):
        score -= 5
    return score


def _score(rec: dict, artist: str, title: str, duration: float | None) -> float | None:
    """How well an LRCLIB entry fits the song; None = not this song (or no lyrics in it)."""
    if not (rec.get("syncedLyrics") or rec.get("plainLyrics")):
        return None
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
class Unavailable(Exception):
    """LRCLIB didn't answer (overloaded, down): not the same as 'no lyrics', so nothing is stored."""


def _ask(session, endpoint: str, params: dict, problems: list):
    """One LRCLIB request, tried twice. A 404 answers None; a failure is noted in problems and answers None too."""
    for attempt in (1, 2):
        try:
            r = session.get(f"{API}/{endpoint}", headers=HEADERS, timeout=8, params=params)
            if r.status_code == 404:
                return None
            if r.status_code == 200:
                data = r.json()
                if endpoint == "search" and not isinstance(data, list):
                    raise ValueError(f"unexpected answer: {str(data)[:100]}")
                return data
            raise RuntimeError(f"HTTP {r.status_code}")
        except Exception as e:  # 503 when LRCLIB is busy, timeouts, odd answers
            if attempt == 2:
                problems.append(f"{endpoint} {params}: {type(e).__name__}: {e}")
                return None
            time.sleep(1.5)


def fetch(artist: str, title: str, album: str = "", duration: float | None = None, session=requests) -> dict:
    """Ask LRCLIB (no cache). Raises Unavailable when nothing was found and some requests failed."""
    instrumental = False
    problems: list[str] = []
    # 1. The exact recording, when the length is known.
    if duration and album:
        rec = _ask(session, "get", {"artist_name": artist, "track_name": title, "album_name": album,
                                    "duration": round(duration)}, problems)
        if rec:
            ans = _answer(rec)
            if ans["found"]:
                return ans
            # An entry without lyrics (marked instrumental, or empty): look further, another entry may have them.
            instrumental = ans["instrumental"]
    # 2. Search by title and artist, and pick the best fit.
    best, best_score = None, None
    for t in title_variants(title):
        for rec in _ask(session, "search", {"track_name": t, "artist_name": main_artist(artist)}, problems) or []:
            s = _score(rec, artist, title, duration)
            if s is not None and (best_score is None or s > best_score):
                best, best_score = rec, s
        if best is not None:
            break
    if best is None:
        # 3. A free search: artist and title, artist and album, the artist alone. Take a result with the same
        #    title, or (for a title written differently: Japanese script vs romaji or English) the same length.
        a = main_artist(artist)
        queries = [f"{a} {title_variants(title)[-1]}", f"{a} {album}".strip() if album else "", a]
        for q in dict.fromkeys(filter(None, queries)):
            for rec in _ask(session, "search", {"q": q}, problems) or []:
                s = _score(rec, artist, title, duration)
                if s is None:
                    s = _by_length(rec, artist, album, duration)
                if s is not None and (best_score is None or s > best_score):
                    best, best_score = rec, s
            if best is not None:
                break
    if best:
        return _answer(best)
    if problems:
        raise Unavailable("; ".join(problems[:3]))
    return {**NOT_FOUND, "instrumental": instrumental}


def from_records(url: str, token: str, artist: str, title: str, album: str = "", duration: float | None = None,
                 session=requests) -> dict:
    """Ask JPlay Records (raises when it can't answer)."""
    params = {"artist": artist, "title": title, "album": album or ""}
    if duration:
        params["duration"] = round(duration)
    r = session.get(url.rstrip("/") + "/api/lyrics", params=params, timeout=45,
                    headers={**HEADERS, "Authorization": f"Bearer {token}"})
    r.raise_for_status()
    ans = r.json()
    return {"found": bool(ans.get("found")), "instrumental": bool(ans.get("instrumental")),
            "synced": ans.get("synced") or None, "plain": ans.get("plain") or None,
            "offset": float(ans.get("offset") or 0)}


def _store(key: str, ans: dict) -> None:
    if _db_path:
        with _lock, _connect() as db:
            db.execute("INSERT OR REPLACE INTO lyrics VALUES (?, ?, ?, ?)",
                       (key, json.dumps(ans), int(ans["found"]), time.time()))


def lookup(artist: str, title: str, album: str = "", duration: float | None = None,
           records: tuple[str, str] | None = None) -> dict:
    """Lyrics for a song. records = (JPlay Records address, listener token) when JPlay Spins is set up.

    Never raises. When LRCLIB couldn't be asked, the answer has "unavailable": True (nothing is stored) and the
    display asks again a minute later.
    """
    if not artist or not title:
        return dict(NOT_FOUND)
    key = cache_key(artist, title)
    if records:
        # Always ask JPlay Records first, so a correction made there shows up; keep a copy for when it's away.
        try:
            ans = from_records(records[0], records[1], artist, title, album, duration)
            _store(key, ans)
            return ans
        except Exception as e:
            print(f"Lyrics: JPlay Records didn't answer ({type(e).__name__}: {e}); using this unit's copy or LRCLIB")
    with _lock:
        if _db_path:
            with _connect() as db:
                row = db.execute("SELECT answer, found, fetched_at FROM lyrics WHERE key = ?", (key,)).fetchone()
            if row and (row[1] or time.time() - row[2] < RETRY_MISS_AFTER):
                return json.loads(row[0])
    if time.time() - _failed.get(key, 0) < RETRY_FAILED_AFTER:
        return {**NOT_FOUND, "unavailable": True}
    try:
        ans = fetch(artist, title, album, duration)
    except Exception as e:  # LRCLIB busy or unreachable: not stored; the display asks again in a minute
        print(f"Lyrics lookup failed for {artist} – {title}: {type(e).__name__}: {e}")
        _failed[key] = time.time()
        if len(_failed) > 500:
            _failed.clear()
        return {**NOT_FOUND, "unavailable": True}
    _failed.pop(key, None)
    _store(key, ans)
    return ans
