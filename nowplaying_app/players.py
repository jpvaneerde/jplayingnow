"""JPlay players: what each one is playing, ready for the display and for JPlay Spins.

A JPlay player (github.com/jpvaneerde/player-jplay) answers GET /api/v1/status with the active source
(radio, plex, cd, spotify, bluetooth) and a `now_playing` block that looks the same for every source.
This module turns that into one card per player.

Radio stations often send no song, only their own name, or text that mixes the show into the song
("Magasinet i P3 med …, spelar nu Dua Lipa - Training Season"). So this module listens to the stream itself
(ffmpeg records a few seconds, Shazam recognises it) and prefers that song over the stream's text.
"""
from __future__ import annotations

import re

import subprocess
import time
from urllib.parse import urljoin

import requests

DEFAULT_PORT = 8080

SOURCE_NAMES = {
    "radio": "Radio",
    "plex": "Plex",
    "cd": "CD",
    "spotify": "Spotify",
    "bluetooth": "Bluetooth",
}

# Radio recognition: how often to listen, and how long a recognised song stays on screen.
LISTEN_SECONDS = 10          # length of each recording from the stream
LISTEN_EVERY = 30            # between attempts while nothing is recognised
LISTEN_EVERY_AFTER_HIT = 45  # after a song was recognised (songs last a few minutes)
KEEP_RECOGNISED = 150        # a recognised song stays shown this long without being heard again


def base_url(p: dict) -> str:
    return f"http://{p['host']}:{int(p.get('port') or DEFAULT_PORT)}"


def fetch_status(p: dict) -> dict | None:
    """The player's /api/v1/status, or None when it can't be reached."""
    try:
        r = requests.get(base_url(p) + "/api/v1/status", timeout=2.5)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


# Show text in front of the artist: "<show>, spelar nu <artist>", "<show> - now playing: <artist>", ...
_NOW_PLAYING = re.compile(
    r"^.*?\b(?:spelar nu|nu spelas|just nu|now playing|playing now|speelt nu|nu op|nu te horen|läuft gerade|jetzt läuft)\s*:?\s+",
    re.I,
)


def clean_artist(artist: str) -> str:
    """'Magasinet i P3 med Siri Fagerudd, spelar nu Dua Lipa' -> 'Dua Lipa'."""
    a = (artist or "").strip()
    cleaned = _NOW_PLAYING.sub("", a).strip(" ,-:")
    return cleaned or a


def has_song(source: str, np: dict) -> bool:
    """Does the player itself know which song is playing?"""
    title = (np.get("title") or "").strip()
    if not title:
        return False
    if source == "radio":
        # The player shows the station name when the stream names no song, and puts the artist in
        # `artist` only when the stream sends "Artist - Title". A title alone is a show or a slogan.
        station = (np.get("extra") or {}).get("station") or ""
        return bool((np.get("artist") or "").strip()) and title.lower() != station.lower()
    return True


def quality(source: str, np: dict) -> str:
    x = np.get("extra") or {}
    parts = []
    codec = str(x.get("codec") or "").upper()
    if source == "cd":
        parts.append("16-bit · 44.1 kHz")
    if codec:
        parts.append(codec)
    rate = x.get("samplerate")
    if rate and source != "cd":
        parts.append(f"{rate / 1000:g} kHz")
    if x.get("bitrate") and codec not in ("FLAC", "ALAC", "WAV"):
        parts.append(f"{x['bitrate']} kbps")
    return " · ".join(parts)


def _abs(p: dict, url: str) -> str:
    """Art from the player is often a path on the player (/api/v1/plex/art?...): make it a full address."""
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("/"):
        return urljoin(base_url(p) + "/", url.lstrip("/"))
    return ""


def make_card(p: dict, status: dict | None, recognised: dict | None, online_info) -> dict:
    """One display card. `online_info(artist, title)` adds year, genre and a cover (iTunes, cached)."""
    card = {
        "id": p["id"],
        "name": p.get("name") or p["host"],
        "host": p["host"],
        "port": int(p.get("port") or DEFAULT_PORT),
        "online": status is not None,
        "state": "offline",
        "playing": False,
        "source": "",
        "source_name": "",
        "station": "",
        "station_logo": "",
        "title": "",
        "artist": "",
        "album": "",
        "year": "",
        "genre": "",
        "coverart": "/static/offline.png",
        "duration": None,
        "position": None,
        "quality": "",
        "detail": "",
        "recognised": False,
        "song_known": False,
    }
    if status is None:
        card.update(title="Offline", artist="Can't reach the player")
        return card

    np = status.get("now_playing") or {}
    source = status.get("active") or ""
    state = np.get("state") or "stopped"
    output = status.get("output") or {}
    card.update(
        name=p.get("name") or status.get("player") or p["host"],
        state=state if source else "idle",
        playing=bool(source) and state in ("playing", "loading"),
        source=source,
        source_name=SOURCE_NAMES.get(source, source.title()),
    )
    if not source:
        card.update(title="Standby", artist="Nothing playing", coverart="/static/idle.png?v=2")
        if output and output.get("present") is False:
            card["detail"] = "DAC is off"
        return card

    x = np.get("extra") or {}
    title, artist, album = (np.get(k) or "" for k in ("title", "artist", "album"))
    known = has_song(source, np)
    cover = _abs(p, np.get("art_url") or "")
    card.update(duration=np.get("duration"), position=np.get("position"), quality=quality(source, np))

    if source == "radio":
        station = x.get("station") or album or title
        card.update(station=station, station_logo=cover, album="")
        artist = clean_artist(artist)
        # The song recognised from the stream wins, unless the stream named a new song since then.
        use_recognised = bool(recognised and recognised.get("station") == station
                              and (not known or recognised.get("icy") == icy_signature(np)))
        if known and not use_recognised:
            info = online_info(artist, title)
            card.update(title=title, artist=artist, album=info.get("album", ""), year=info.get("year", ""),
                        genre=info.get("genre", "") or x.get("genre", ""), coverart=_real_art(info) or cover,
                        song_duration=info.get("duration"))
        elif use_recognised:
            card.update(title=recognised["title"], artist=recognised["artist"], album=recognised.get("album", ""),
                        year=recognised.get("year", ""), genre=recognised.get("genre", ""),
                        coverart=recognised.get("cover") or cover, recognised=True,
                        # where in the song the recording started, and when (for the lyrics)
                        heard_offset=recognised.get("offset"), heard_at=recognised.get("rec_start"),
                        song_duration=recognised.get("duration"))
            known = True
        else:
            # No song yet: the station itself. A show name or slogan from the stream goes underneath.
            card.update(title=station, artist=title if title.lower() != station.lower() else "Live radio",
                        coverart=cover or "/static/default_cover.png")
        card["detail"] = station
    else:
        info = online_info(artist, title) if title else {}
        year = str(x.get("year") or "") or info.get("year", "")
        card.update(title=title or card["source_name"], artist=artist, album=album or info.get("album", ""),
                    year=year, genre=info.get("genre", ""),
                    coverart=cover or _real_art(info) or "/static/default_cover.png")
        if source == "bluetooth":
            card["detail"] = x.get("device") or ""
        elif source == "spotify":
            card["detail"] = "Spotify Connect"
        elif source == "cd" and np.get("track_number"):
            n, total = np.get("track_number"), np.get("track_count")
            card["detail"] = f"Track {n}" + (f" of {total}" if total else "")
    if output and output.get("present") is False:
        card["detail"] = "DAC is off"
    card["song_known"] = known
    return card


def _real_art(info: dict) -> str:
    art = (info or {}).get("art") or ""
    return art if art.startswith("http") else ""


def icy_signature(np: dict) -> str:
    """What the stream itself said at a moment (to notice when it names a new song)."""
    return f"{np.get('artist') or ''}|{np.get('title') or ''}"


def spins_room_data(card: dict) -> dict:
    """This room's player for JPlay Spins: state, source, station and the song (if known)."""
    if not card.get("online"):
        return {"player": "jplay", "state": "offline", "mode": "", "station": "", "title": "", "artist": "", "album": ""}
    state = {"playing": "play", "loading": "play", "paused": "pause"}.get(card.get("state"), "stop")
    known = state == "play" and card.get("song_known")
    return {
        "player": "jplay",
        "state": state,
        "mode": card.get("source") or "",                  # radio | plex | cd | spotify | bluetooth
        "station": card.get("station", "") if state == "play" else "",
        "title": card.get("title", "") if known else "",
        "artist": card.get("artist", "") if known else "",
        "album": card.get("album", "") if known else "",
        "recognised": bool(known and card.get("recognised")),
    }


def needs_listening(card: dict, status: dict | None) -> str:
    """The stream address to listen to while this player plays radio.

    Always, not only when the station names no song: what stations send is often wrong or mixed with
    the show's name, and the recognised song is preferred.
    """
    if not status or status.get("active") != "radio" or not card.get("playing"):
        return ""
    np = status.get("now_playing") or {}
    return str((np.get("extra") or {}).get("stream_url") or "")


def record_stream(url: str, seconds: int = LISTEN_SECONDS) -> bytes:
    """A few seconds of a radio stream as 16 kHz mono WAV (what Shazam wants)."""
    res = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-rw_timeout", "10000000", "-t", str(seconds), "-i", url,
         "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1"],
        capture_output=True, timeout=seconds + 25,
    )
    if res.returncode != 0 or len(res.stdout) < 16000:
        raise RuntimeError((res.stderr.decode(errors="replace").strip() or "no audio from the stream")[-300:])
    return res.stdout


class RadioListener:
    """Per player: when to listen next and the last song recognised from its stream."""

    def __init__(self):
        self.songs: dict[str, dict] = {}       # player id -> recognised song
        self.next_try: dict[str, float] = {}
        self.status: dict[str, str] = {}       # player id -> last result, for troubleshooting
        self.last_icy: dict[str, str] = {}     # player id -> what the stream last said

    def due(self, player_id: str, now: float) -> bool:
        return now >= self.next_try.get(player_id, 0)

    def current(self, player_id: str, station: str, now: float) -> dict | None:
        s = self.songs.get(player_id)
        if not s:
            return None
        if s["station"] != station or now - s["heard"] > KEEP_RECOGNISED:
            self.songs.pop(player_id, None)
            return None
        return s

    def heard(self, player_id: str, station: str, song: dict, now: float, icy: str = ""):
        prev = self.songs.get(player_id)
        same = prev and prev["station"] == station and prev["title"].lower() == song["title"].lower()
        self.songs[player_id] = {**song, "station": station, "icy": icy, "heard": now, "since": prev["since"] if same else now}
        self.next_try[player_id] = now + LISTEN_EVERY_AFTER_HIT
        self.status[player_id] = f"recognised {song['artist']} – {song['title']}"

    def missed(self, player_id: str, now: float, why: str):
        self.next_try[player_id] = now + LISTEN_EVERY
        self.status[player_id] = why

    def stream_changed(self, player_id: str, icy: str, now: float):
        """The stream named something new (a new song, probably): listen again soon."""
        if self.last_icy.get(player_id) != icy:
            self.last_icy[player_id] = icy
            self.next_try[player_id] = min(self.next_try.get(player_id, 0), now + 10)

    def forget(self, player_id: str):
        self.songs.pop(player_id, None)
        self.next_try.pop(player_id, None)
