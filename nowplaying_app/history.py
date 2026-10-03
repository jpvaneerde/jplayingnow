"""Play history, stored indefinitely in a local SQLite database."""
import csv
import io
import os
import sqlite3
import threading
from datetime import datetime, timedelta

DB_PATH = None
_lock = threading.Lock()
_last_logged = {}  # source -> (title, artist) of the most recently logged play

SKIP_TITLES = {"idle", "offline", "unknown track", "unknown title", "no song identified", "listening..."}


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path):
    global DB_PATH
    DB_PATH = path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS plays (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,            -- local time, 'YYYY-MM-DD HH:MM:SS'
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                artist TEXT NOT NULL DEFAULT '',
                album TEXT NOT NULL DEFAULT '',
                genre TEXT NOT NULL DEFAULT '',
                year TEXT NOT NULL DEFAULT '',
                coverart TEXT NOT NULL DEFAULT ''
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_plays_ts ON plays(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_plays_artist ON plays(artist)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_plays_genre ON plays(genre)")


def purge_other_sources(keep):
    """Delete plays from any source other than `keep` (e.g. old moOde entries)."""
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM plays WHERE source != ?", (keep,))
    _last_logged.clear()


def log_play(source, title, artist="", album="", genre="", year="", coverart="", played_at=None):
    """Record a play if it differs from the last one logged for this source."""
    title, artist = (title or "").strip(), (artist or "").strip()
    if not title or title.lower() in SKIP_TITLES:
        return False
    if title.lower() == "live stream" and not artist:
        return False
    key = (title.lower(), artist.lower())
    with _lock:
        if source not in _last_logged:
            with _connect() as conn:
                row = conn.execute(
                    "SELECT title, artist FROM plays WHERE source=? ORDER BY id DESC LIMIT 1", (source,)
                ).fetchone()
            _last_logged[source] = (row["title"].lower(), row["artist"].lower()) if row else None
        if _last_logged[source] == key:
            return False
        with _connect() as conn:
            conn.execute(
                "INSERT INTO plays (ts, source, title, artist, album, genre, year, coverart) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ((played_at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"), source, title, artist,
                 album or "", genre or "", year or "", coverart or ""),
            )
        _last_logged[source] = key
    return True


def _where(f):
    clauses, params = [], []
    if f.get("start"):
        clauses.append("ts >= ?")
        params.append(f["start"] + " 00:00:00")
    if f.get("end"):
        try:
            nxt = (datetime.strptime(f["end"], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            clauses.append("ts < ?")
            params.append(nxt + " 00:00:00")
        except ValueError:
            pass
    if f.get("genre"):
        clauses.append("genre = ?")
        params.append(f["genre"])
    if f.get("artist"):
        clauses.append("artist LIKE ?")
        params.append(f"%{f['artist']}%")
    if f.get("source"):
        clauses.append("source = ?")
        params.append(f["source"])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def query_plays(f, limit=100, offset=0):
    where, params = _where(f)
    with _connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM plays{where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM plays{where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return {"total": total, "plays": [dict(r) for r in rows]}


def stats(f, limit=20):
    where, params = _where(f)
    extra = lambda col: (where + (" AND " if where else " WHERE ") + f"{col} != ''")
    with _connect() as conn:
        totals = conn.execute(
            f"SELECT COUNT(*) AS plays, COUNT(DISTINCT lower(artist) || '|' || lower(title)) AS tracks, "
            f"COUNT(DISTINCT lower(artist)) AS artists FROM plays{where}",
            params,
        ).fetchone()
        top_tracks = conn.execute(
            f"SELECT MAX(artist) AS artist, MAX(title) AS title, MAX(album) AS album, "
            f"MAX(coverart) AS coverart, COUNT(*) AS plays, MAX(ts) AS last_played "
            f"FROM plays{where} GROUP BY lower(artist), lower(title) "
            f"ORDER BY plays DESC, last_played DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        top_artists = conn.execute(
            f"SELECT MAX(artist) AS artist, COUNT(*) AS plays, MAX(ts) AS last_played "
            f"FROM plays{extra('artist')} GROUP BY lower(artist) "
            f"ORDER BY plays DESC, last_played DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        top_albums = conn.execute(
            f"SELECT MAX(album) AS album, MAX(artist) AS artist, MAX(coverart) AS coverart, "
            f"COUNT(*) AS plays FROM plays{extra('album')} GROUP BY lower(album), lower(artist) "
            f"ORDER BY plays DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        top_genres = conn.execute(
            f"SELECT genre, COUNT(*) AS plays FROM plays{extra('genre')} "
            f"GROUP BY genre ORDER BY plays DESC LIMIT ?",
            params + [limit],
        ).fetchall()
    return {
        "totals": dict(totals),
        "top_tracks": [dict(r) for r in top_tracks],
        "top_artists": [dict(r) for r in top_artists],
        "top_albums": [dict(r) for r in top_albums],
        "top_genres": [dict(r) for r in top_genres],
    }


def filter_options():
    with _connect() as conn:
        genres = [r[0] for r in conn.execute(
            "SELECT DISTINCT genre FROM plays WHERE genre != '' ORDER BY genre COLLATE NOCASE")]
        artists = [r[0] for r in conn.execute(
            "SELECT artist FROM plays WHERE artist != '' GROUP BY lower(artist) "
            "ORDER BY COUNT(*) DESC LIMIT 500")]
        sources = [r[0] for r in conn.execute("SELECT DISTINCT source FROM plays ORDER BY source")]
    return {"genres": genres, "artists": artists, "sources": sources}


def export_csv(f):
    where, params = _where(f)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["timestamp", "source", "title", "artist", "album", "genre", "year"])
    with _connect() as conn:
        for r in conn.execute(
            f"SELECT ts, source, title, artist, album, genre, year FROM plays{where} ORDER BY ts DESC", params
        ):
            w.writerow(list(r))
    return out.getvalue()
