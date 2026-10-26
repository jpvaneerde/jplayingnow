import asyncio
import io
import json
import os
import re
import ctypes
import sys

devnull = os.open(os.devnull, os.O_WRONLY)
old_stderr = os.dup(2)
os.dup2(devnull, 2)

ERROR_HANDLER_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p)
def py_error_handler(filename, line, function, err, fmt):
    pass
c_error_handler = ERROR_HANDLER_FUNC(py_error_handler)

try:
    asound = ctypes.cdll.LoadLibrary('libasound.so.2')
    asound.snd_lib_error_set_handler(c_error_handler)
except Exception:
    pass

try:
    import shutil
    import socket
    import subprocess
    import time
    from datetime import datetime
    from urllib.parse import quote, unquote
    import wave
    import requests
    import sounddevice as sd
    import numpy as np
    from contextlib import asynccontextmanager
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates
    from shazamio import Shazam
    import history
    import players
    import lyrics
    import spins
    import touchwake
    import updates
except Exception:
    # Imports run with stderr silenced (to hide ALSA noise); show why startup failed instead of exiting silently.
    os.dup2(old_stderr, 2)
    import traceback
    traceback.print_exc()
    print("\nJPlaying Now could not start: a package or library is missing (see above).", file=sys.stderr)
    sys.exit(1)
os.dup2(old_stderr, 2)
os.close(old_stderr)
os.close(devnull)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup Logic ---
    history.init_db(HISTORY_DB)
    # Keep existing history under the renamed source, then drop anything that isn't the mic
    history.rename_source("Ambient Listener (CD)", MIC_SOURCE_NAME)
    history.purge_other_sources(MIC_SOURCE_NAME)   # history holds mic-detected songs only
    spins.init(HISTORY_DB)                          # JPlay Spins outbox lives next to the history
    os.makedirs(DATA_DIR, exist_ok=True)
    lyrics.init(os.path.join(DATA_DIR, "lyrics.db"))  # lyrics already looked up
    tasks = [
        asyncio.create_task(background_mic_listener()),
        asyncio.create_task(background_player_poller()),
        asyncio.create_task(background_radio_listener()),
        asyncio.create_task(background_screen_manager()),
        asyncio.create_task(background_spins_sender()),
        asyncio.create_task(background_auto_update()),
    ]
    cfg = load_config()
    set_screen_rotation(cfg.get("screen_rotation", "normal"))

    yield

    for t in tasks:
        t.cancel()

app = FastAPI(lifespan=lifespan)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
# Local, untracked files: settings and history live on the device and are never overwritten by git
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
CONFIG_EXAMPLE_FILE = os.path.join(BASE_DIR, "config.example.json")
DATA_DIR = os.path.join(BASE_DIR, "data")
HISTORY_DB = os.path.join(DATA_DIR, "history.db")
# Versions and updates: the separate updater (deploy/update.sh) does the work; see updates.py
UPDATES = updates.Updates("jplayingnow", os.path.dirname(BASE_DIR), DATA_DIR)

os.makedirs(STATIC_DIR, exist_ok=True)
templates = Jinja2Templates(directory=STATIC_DIR)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

shazam = Shazam()

DEFAULT_CONFIG = {
    "theme": "dark",
    "screen_rotation": "normal",
    "mic_enabled": True,
    "mic_interval_seconds": 8,
    "input_device_index": 1,
    "sample_rate": 16000,
    "view_player": "",                # id of the player this display shows ("" = the first one)
    "screen_off_minutes": 0,          # 0 = never turn the local screen off
    "screen_wake_on_song": True,      # turn the screen back on when the ambient listener hears a song
    "spins_enabled": False,           # report plays to JPlay Spins (records.jplay.cloud)
    "spins_url": "https://records.jplay.cloud",
    "spins_token": "",                # listener token from JPlay Records → Settings → Listeners
    "spins_room_player": "",          # host of the JPlay player in this room ("" = none)
    "radio_listen": True,             # recognise songs on radio stations that don't name them (listens to the stream)
    "lyrics_enabled": True,           # show the song's lyrics (LRCLIB), in time with the music when possible
    "lyrics_offset_seconds": 0,       # move the lyrics earlier (-) or later (+) if they run ahead or behind
    "update_channel": "releases",     # releases (tagged versions) | main (the newest commit, for development)
    "update_auto": "off",             # off | night (install updates between 03:00 and 05:00 when nothing plays)
    "players": []                     # JPlay players: {id, name, host, port, enabled}
}

def _local_addresses():
    """Loopback plus this machine's own addresses (a kiosk browser may use the LAN IP)."""
    addrs = {"127.0.0.1", "::1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addrs.add(info[4][0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))   # no packet is sent; just picks the LAN interface
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return addrs

def is_local_request(request: Request) -> bool:
    """True only for requests coming from the Pi itself (e.g. the kiosk browser)."""
    host = request.client.host if request.client else ""
    if host.startswith("::ffff:"):
        host = host[7:]
    return host in _local_addresses()

def is_service_active(service_name):
    try:
        res = subprocess.run(["systemctl", "is-active", service_name], capture_output=True, text=True)
        return res.stdout.strip() == "active"
    except Exception:
        return False

def service_status_text(service_name):
    """Last lines of `systemctl status` (state, exit code, recent log) for error messages."""
    try:
        res = subprocess.run(["systemctl", "status", service_name, "--no-pager", "-n", "12"],
                             capture_output=True, text=True, timeout=10)
        return (res.stdout or res.stderr).strip()[-1200:] or "no status available"
    except Exception as e:
        return f"could not read status: {e}"

def set_service_state(service_name, enable: bool):
    """Enable/disable a systemd service. Returns '' on success or an error message."""
    action = ["enable", "--now"] if enable else ["disable", "--now"]
    try:
        # -n: never wait for a sudo password (the server has no terminal to type it in)
        res = subprocess.run(["sudo", "-n", "systemctl", *action, service_name],
                             capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            detail = (res.stderr or res.stdout).strip() or f"exit code {res.returncode}"
            msg = f"Could not {action[0]} '{service_name}': {detail}"
            print(msg)
            return msg
        return ""
    except Exception as e:
        msg = f"Could not {action[0]} '{service_name}': {e}"
        print(msg)
        return msg

def _session_env():
    """Environment for talking to the desktop. Returns (env, 'wayland' | 'x11').

    Wayland (labwc/wayfire, the Raspberry Pi OS default) is detected by its socket in
    /run/user/<uid>; otherwise X11 on display :0 is assumed.
    """
    env = os.environ.copy()
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    try:
        sockets = sorted(n for n in os.listdir(runtime) if re.fullmatch(r"wayland-\d+", n))
    except OSError:
        sockets = []
    if sockets:
        env["XDG_RUNTIME_DIR"] = runtime
        env.setdefault("WAYLAND_DISPLAY", sockets[0])
        return env, "wayland"
    env["DISPLAY"] = ":0"
    return env, "x11"

def _run_display_cmd(args, env):
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=8)

def _wayland_outputs(env):
    """Names of the connected outputs, from `wlr-randr`."""
    out = _run_display_cmd(["wlr-randr"], env).stdout
    return re.findall(r"^(\S+)", out, re.M)

# Same labels as the settings page (xrandr names) -> wlr-randr transforms.
# xrandr "right" turns the picture 90° clockwise, which wlr-randr calls 270.
_WAYLAND_TRANSFORM = {"normal": "normal", "right": "270", "inverted": "180", "left": "90"}

def set_screen_rotation(rotation: str):
    try:
        env, kind = _session_env()
        if kind == "wayland":
            transform = _WAYLAND_TRANSFORM.get(rotation, "normal")
            for output in _wayland_outputs(env):
                _run_display_cmd(["wlr-randr", "--output", output, "--transform", transform], env)
        else:
            _run_display_cmd(["xrandr", "-o", rotation], env)
    except Exception as e:
        print(f"Failed to set screen rotation: {e}")

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    # Local config.json wins; on a fresh install fall back to the shipped example
    for path in (CONFIG_FILE, CONFIG_EXAMPLE_FILE):
        try:
            with open(path, "r") as f:
                cfg.update(json.load(f))
            break
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return migrate_config(cfg)

def migrate_config(cfg):
    """Settings from the moOde days: the JPlay players replaced the moOde players on the same Pis."""
    if "players" not in cfg or (not cfg["players"] and cfg.get("moodes")):
        cfg["players"] = [
            {"id": f"player_{i + 1}", "name": m.get("name") or f"Player {i + 1}", "host": m.get("host", ""),
             "port": players.DEFAULT_PORT, "enabled": m.get("enabled", True)}
            for i, m in enumerate(cfg.get("moodes") or []) if m.get("host")
        ]
    if not cfg.get("spins_room_player") and cfg.get("spins_room_moode"):
        cfg["spins_room_player"] = cfg["spins_room_moode"]
    for k in ("moodes", "spins_room_moode", "hide_inactive_moodes", "moode_proxy_port",
              "display_mode", "hide_inactive_players"):
        cfg.pop(k, None)
    return cfg

def save_config(config_data):
    # Atomic write: temp file in same dir, then replace
    tmp_path = CONFIG_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(config_data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, CONFIG_FILE)

def to_int(value, default, minimum=None, maximum=None):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        n = max(minimum, n)
    if maximum is not None:
        n = min(maximum, n)
    return n

latest_ambient_state = {
    "name": "Ambient Listener",
    "title": "Listening...",
    "artist": "Waiting for audio",
    "coverart": "/static/idle.png?v=2"
}

_itunes_cache = {}
_itunes_misses = {}   # (artist, title) -> retry after this time

def extract_year(value):
    """Return a 4-digit year string from values like '1977', '1977-05-01', or ISO dates."""
    m = re.search(r"\b(1[89]\d{2}|20\d{2})\b", str(value or ""))
    return m.group(1) if m else ""

_COMPILATION_WORDS = re.compile(
    r"\b(greatest hits|best of|compilation|karaoke|tribute|essentials?|collection|"
    r"now that|anthology|hits|playlist|remixes|live)\b",
    re.I,
)

def _norm(s):
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", str(s or "").lower())  # drop (feat. x), [Remastered]
    s = re.sub(r"\s+-\s+.*$", "", s)                            # drop "- 2011 Remaster"
    return re.sub(r"[^a-z0-9]+", " ", s).strip()

def pick_best_match(results, artist, title):
    """Choose the iTunes result that best matches artist+title, preferring original albums."""
    t, a = _norm(title), _norm(artist)
    best, best_score = None, 0
    for r in results:
        rt, ra = _norm(r.get("trackName")), _norm(r.get("artistName"))
        score = 0
        if rt == t:
            score += 4
        elif t and (t in rt or rt in t):
            score += 1
        if a and (ra == a):
            score += 4
        elif a and (a in ra or ra in a):
            score += 2
        if score < 5:               # need title AND artist to be reasonably close
            continue
        album = r.get("collectionName", "")
        if r.get("collectionArtistName", "").lower() == "various artists":
            score -= 3
        if _COMPILATION_WORDS.search(album):
            score -= 2
        if r.get("collectionType") == "Album" and r.get("trackCount", 0) > 30:
            score -= 1
        if score > best_score:
            best, best_score = r, score
    return best

def fetch_online_info(artist, title):
    """Return dict(art, year, album, genre) from iTunes. Cached per track."""
    default = {"art": "/static/default_cover.png", "year": "", "album": "", "genre": "", "duration": None}
    if not title or title in ["Idle", "Unknown Track", "Offline"]:
        return default
    key = (artist, title)
    if key in _itunes_cache:
        return _itunes_cache[key]
    if _itunes_misses.get(key, 0) > time.time():
        return default
    try:
        res = requests.get(
            "https://itunes.apple.com/search",
            params={"term": f"{artist} {title}".strip(), "entity": "song", "limit": 10},
            timeout=3,
        ).json()
        r = pick_best_match(res.get("results", []), artist, title)
        if r:
            info = {
                "art": r["artworkUrl100"].replace("100x100bb", "600x600bb"),
                "year": extract_year(r.get("releaseDate")),
                "album": r.get("collectionName", ""),
                "genre": r.get("primaryGenreName", ""),
                "duration": round(r["trackTimeMillis"] / 1000) if r.get("trackTimeMillis") else None,
            }
            if len(_itunes_cache) > 200:
                _itunes_cache.clear()
            _itunes_cache[key] = info
            return info
    except Exception:
        pass
    # Not found (or iTunes unreachable): don't ask again for a while; the players are polled every 2 s.
    _itunes_misses[key] = time.time() + 600
    return default

MIC_DEFAULT = "__default__"   # settings value meaning "the system's default input"

def mic_label(name):
    """Device name without the ALSA card numbers, e.g. 'USB PnP Sound Device: Audio (hw:2,0)'
    -> 'USB PnP Sound Device: Audio'. The numbers change when USB devices are re-ordered."""
    return re.sub(r"\s*\((hw|plughw):\d+,\d+\)\s*$", "", str(name or "")).strip()

def list_input_devices():
    """Microphones PortAudio can see right now: [{index, name, label, channels, rate}]."""
    try:
        return [
            {"index": i, "name": d["name"], "label": mic_label(d["name"]),
             "channels": d["max_input_channels"], "rate": int(d.get("default_samplerate") or 0)}
            for i, d in enumerate(sd.query_devices()) if d.get("max_input_channels", 0) > 0
        ]
    except Exception as e:
        print(f"Could not list audio devices: {e}")
        return []

def resolve_mic(cfg):
    """Return (device index or None for the system default, device info or None, note).

    The microphone is stored by name, so it is found again when the device order changes.
    Older configs only have input_device_index, which is used as-is.
    """
    devices = list_input_devices()
    wanted = str(cfg.get("input_device_name") or "").strip()
    if wanted == MIC_DEFAULT:
        return None, None, "system default input"
    if wanted:
        for d in devices:
            if d["name"] == wanted or d["label"] == mic_label(wanted):
                return d["index"], d, f"'{d['label']}' (device {d['index']})"
        return None, None, f"'{wanted}' is not connected; using the system default input"
    idx = to_int(cfg.get("input_device_index"), 1)
    dev = next((d for d in devices if d["index"] == idx), None)
    if dev:
        return idx, dev, f"'{dev['label']}' (device {idx})"
    return None, None, f"device {idx} is not an input; using the system default input"

def record_audio(duration):
    cfg = load_config()
    MIC_DEVICE, dev, _ = resolve_mic(cfg)

    # Configured rate first, then the mic's own rate and standard webcam rates as fallbacks
    rates = [to_int(cfg.get("sample_rate"), 16000)]
    rates += [r for r in ((dev or {}).get("rate"), 16000, 48000, 44100) if r and r not in rates]
    for sample_rate in rates:
        devnull = os.open(os.devnull, os.O_WRONLY)
        old_stderr = os.dup(2)
        os.dup2(devnull, 2)

        try:
            recording = sd.rec(
                int(duration * sample_rate),
                samplerate=sample_rate,
                channels=1,
                dtype="int16",
                device=MIC_DEVICE,
            )
            sd.wait()
            global LAST_LEVEL_DB
            rms = float(np.sqrt(np.mean(np.square(recording.astype(np.float32))))) if recording.size else 0.0
            LAST_LEVEL_DB = round(20 * np.log10(rms / 32768 + 1e-9), 1)

            # If recording succeeded, compile WAV buffer and break out
            wav_io = io.BytesIO()
            with wave.open(wav_io, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(recording.tobytes())

            return wav_io.getvalue()

        except Exception:
            continue
        finally:
            os.dup2(old_stderr, 2)
            os.close(old_stderr)
            os.close(devnull)

    raise RuntimeError(
        "Failed to capture audio at supported sample rates (16kHz, 48kHz, 44.1kHz)."
    )

LAST_LEVEL_DB = None   # loudness of the last microphone clip (dBFS)

# --- JPlay players ------------------------------------------------------------------
player_cards = {}        # player id -> display card (see players.make_card)
player_status = {}       # player id -> raw /api/v1/status (None = unreachable)
last_player_activity = [0.0]   # last time this display's player was playing (keeps the screen on)
paused_since = {}        # player id -> when it was paused (the display goes back to the listener after a while)
SHOW_PAUSED_SECONDS = 300
radio = players.RadioListener()

def configured_players(cfg, include_room=True):
    """Enabled players, plus this room's player for JPlay Spins even when it isn't shown."""
    shown = [p for p in cfg.get("players", []) if p.get("enabled", True) and p.get("host")]
    room = str(cfg.get("spins_room_player") or "").strip()
    if include_room and room and not any(p["host"] == room for p in shown):
        port = next((p.get("port") for p in cfg.get("players", []) if p.get("host") == room), players.DEFAULT_PORT)
        shown.append({"id": "room_player", "name": "This room", "host": room, "port": port, "hidden": True})
    return shown

def _build_card(p, status):
    np = (status or {}).get("now_playing") or {}
    station = (np.get("extra") or {}).get("station", "")
    song = radio.current(p["id"], station, time.time()) if status and status.get("active") == "radio" else None
    return players.make_card(p, status, song, fetch_online_info)

async def background_player_poller():
    """Ask every JPlay player what it's doing (every 2 seconds) and keep the cards for the display."""
    global player_cards, player_status
    loop = asyncio.get_running_loop()
    while True:
        try:
            cfg = load_config()
            ps = configured_players(cfg)
            statuses = await asyncio.gather(*(loop.run_in_executor(None, players.fetch_status, p) for p in ps))
            cards = await asyncio.gather(*(loop.run_in_executor(None, _build_card, p, st) for p, st in zip(ps, statuses)))
            player_status = {p["id"]: st for p, st in zip(ps, statuses)}
            now = time.time()
            for c in cards:
                if c["state"] == "paused":
                    paused_since.setdefault(c["id"], now)
                else:
                    paused_since.pop(c["id"], None)
            player_cards = {
                c["id"]: {**c, "hidden": bool(p.get("hidden")), "polled_at": now,
                          # Worth showing: playing, or paused only a little while. Otherwise the display shows the listener.
                          "show": c["playing"] or (c["state"] == "paused" and now - paused_since[c["id"]] < SHOW_PAUSED_SECONDS)}
                for p, c in zip(ps, cards)
            }
            # Only the player this display shows keeps its screen on (not the one in the other room).
            shown = view_player(cfg)
            if shown and player_cards.get(shown["id"], {}).get("playing"):
                last_player_activity[0] = time.time()
            room = str(cfg.get("spins_room_player") or "").strip()
            if spins.enabled(cfg) and room:
                own = next((c for c in cards if c["host"] == room), None)
                if own:
                    spins.room_update(players.spins_room_data(own))
        except Exception as e:
            print(f"Player poller error: {type(e).__name__}: {e}")
        await asyncio.sleep(2)

async def background_radio_listener():
    """Radio without song names: record a few seconds of the stream and let Shazam name the song."""
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(3)
        try:
            cfg = load_config()
            if not cfg.get("radio_listen", True) or not shutil.which("ffmpeg"):
                continue
            now = time.time()
            for pid, card in list(player_cards.items()):
                url = players.needs_listening(card, player_status.get(pid))
                if not url:
                    if card.get("source") != "radio":
                        radio.forget(pid)
                    continue
                np = (player_status.get(pid) or {}).get("now_playing") or {}
                icy = players.icy_signature(np)
                radio.stream_changed(pid, icy, now)
                if not radio.due(pid, now):
                    continue
                station = card.get("station", "")
                try:
                    rec_start = time.time()
                    audio = await loop.run_in_executor(None, players.record_stream, url)
                    song = parse_shazam(await shazam.recognize(audio))
                except Exception as e:
                    radio.missed(pid, time.time(), f"listening failed: {type(e).__name__}: {e}")
                    continue
                if song:
                    info = await loop.run_in_executor(None, fetch_online_info, song["artist"], song["title"])
                    song["year"] = song["year"] or info["year"]
                    song["album"] = song["album"] or info["album"]
                    song["genre"] = song["genre"] or info["genre"]
                    song["duration"] = info.get("duration")
                    song["rec_start"] = rec_start        # with song["offset"]: where in the song we are (lyrics)
                    radio.heard(pid, station, song, time.time(), icy)   # the next poll (2 s) shows it
                else:
                    radio.missed(pid, time.time(), "no song recognised (talk, ads or unknown music)")
        except Exception as e:
            print(f"Radio listener error: {type(e).__name__}: {e}")

def parse_shazam(out):
    """Shazam's answer -> {title, artist, album, year, genre, cover, key, isrc}, or None."""
    track = (out or {}).get("track") or {}
    if not track:
        return None
    images = track.get("images", {})
    year = album = ""
    for section in track.get("sections", []):
        for item in section.get("metadata", []):
            label = str(item.get("title", "")).lower()
            if label == "released":
                year = extract_year(item.get("text"))
            elif label == "album":
                album = item.get("text", "")
    return {
        "title": track.get("title", "Unknown Title"),
        "artist": track.get("subtitle", "Unknown Artist"),
        "album": album,
        "year": year,
        "genre": (track.get("genres") or {}).get("primary", ""),
        "cover": images.get("coverarthq") or images.get("coverart") or "",
        "key": track.get("key", ""),
        "isrc": track.get("isrc", ""),
        "offset": shazam_offset(out),
    }

def shazam_offset(out):
    """How far into the song (seconds) the recording started, as Shazam matched it; None if unknown."""
    try:
        return float(((out or {}).get("matches") or [{}])[0]["offset"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None

MIN_PLAY_SECONDS = 20          # a song must be heard this long before it enters history
MIC_SOURCE_NAME = "Ambient Listener"
pending_play = {"key": None, "first": None, "last": None, "logged": False, "heard": None, "spin_id": None}

def _finish_pending():
    """The current song stopped being heard: tell JPlay Spins when it ended."""
    if pending_play.get("spin_id"):
        spins.queue("mic_end", pending_play["last"] or time.time(), {"mic_id": pending_play["spin_id"]})
    pending_play["spin_id"] = None
QUIET_AFTER_SECONDS = 45       # no song recognised for this long -> show the "quiet" card

def quiet_state():
    return {
        "identified": False,
        "name": MIC_SOURCE_NAME,
        "title": "No music detected",
        "artist": "Waiting for something to play",
        "year": "", "album": "", "genre": "",
        "coverart": "/static/idle.png?v=2",
    }

async def background_mic_listener():
    global latest_ambient_state
    listener_started = time.time()
    while True:
        cfg = load_config()
        if cfg.get("mic_enabled", True):
            try:
                loop = asyncio.get_event_loop()
                duration = cfg.get("mic_interval_seconds", 8)
                rec_start = time.time()
                audio_bytes = await loop.run_in_executor(None, record_audio, duration)

                out = await shazam.recognize(audio_bytes)
                track = out.get('track', {})

                if track:
                    images = track.get("images", {})
                    cover = images.get("coverarthq", images.get("coverart", "/static/default_cover.png"))
                    year = ""
                    shazam_album = ""
                    for section in track.get("sections", []):
                        for item in section.get("metadata", []):
                            label = str(item.get("title", "")).lower()
                            if label == "released":
                                year = extract_year(item.get("text"))
                            elif label == "album":
                                shazam_album = item.get("text", "")
                    info = await loop.run_in_executor(
                        None, fetch_online_info,
                        track.get("subtitle", ""), track.get("title", "")
                    )
                    year = year or info["year"]
                    latest_ambient_state = {
                        "identified": True,
                        "name": MIC_SOURCE_NAME,
                        "title": track.get("title", "Unknown Title"),
                        "artist": track.get("subtitle", "Unknown Artist"),
                        "year": year,
                        "album": shazam_album or info["album"],
                        "genre": track.get("genres", {}).get("primary") or info["genre"],
                        "coverart": cover,
                        # Where in the song we are (for the lyrics): offset at the moment the recording started
                        "offset": shazam_offset(out),
                        "heard_at": rec_start,
                        "duration": info.get("duration"),
                    }
                    # Only log once the same song has been heard for MIN_PLAY_SECONDS
                    s = latest_ambient_state
                    now = time.time()
                    pending_play["heard"] = now
                    key = (s["title"].lower(), s["artist"].lower())
                    if pending_play["key"] != key:
                        _finish_pending()
                        pending_play.update(key=key, first=now - duration, last=now, logged=False)
                    else:
                        pending_play["last"] = now
                    if not pending_play["logged"] and now - pending_play["first"] >= MIN_PLAY_SECONDS:
                        pending_play["logged"] = True
                        if spins.enabled(cfg):
                            pending_play["spin_id"] = spins.queue("mic", pending_play["first"], {
                                "title": s["title"], "artist": s["artist"], "album": s["album"],
                                "genre": s["genre"], "year": s["year"],
                                "cover": s["coverart"] if str(s["coverart"]).startswith("http") else "",
                                "shazam_key": track.get("key", ""), "isrc": track.get("isrc", ""),
                                "level_db": LAST_LEVEL_DB,
                            })
                        await loop.run_in_executor(
                            None, lambda: history.log_play(
                                s["name"], s["title"], s["artist"], s["album"], s["genre"],
                                s["year"], s["coverart"],
                                played_at=datetime.fromtimestamp(pending_play["first"]),
                            )
                        )
                else:
                    now = time.time()
                    # Forget the history candidate if the song has been gone for a while
                    if pending_play["last"] and now - pending_play["last"] > 40:
                        _finish_pending()
                        pending_play.update(key=None, first=None, last=None, logged=False)
                    quiet_after = to_int(cfg.get("quiet_after_seconds"), QUIET_AFTER_SECONDS, minimum=10)
                    silent_for = now - (pending_play["heard"] or listener_started)
                    if silent_for > quiet_after:
                        # Nothing recognised for a while: stop showing the last song
                        latest_ambient_state = quiet_state()
                        _finish_pending()
                        pending_play.update(key=None, first=None, last=None, logged=False)
                    elif not latest_ambient_state.get("identified"):
                        latest_ambient_state["title"] = "Listening..."
                        latest_ambient_state["artist"] = "Waiting for audio"
            except Exception as e:
                latest_ambient_state["title"] = "Mic Error"
                latest_ambient_state["artist"] = str(e)
                await asyncio.sleep(3)
        else:
            latest_ambient_state["title"] = "Mic Disabled"
            latest_ambient_state["artist"] = "Disabled in settings"
            await asyncio.sleep(5)

async def background_spins_sender():
    """Send queued Spins events every few seconds (slower while the server can't be reached)."""
    wait = 5
    while True:
        await asyncio.sleep(wait)
        cfg = load_config()
        if not spins.enabled(cfg):
            wait = 10
            continue
        try:
            sent = await asyncio.get_running_loop().run_in_executor(None, spins.send_pending, cfg)
            wait = 5 if sent or not spins.status["last_error"] else min(wait * 2, 120)
        except Exception as e:
            spins.status["last_error"] = str(e)
            wait = 60

def get_screen_power():
    """True if the monitor is on, False if off, None if it can't be determined."""
    try:
        env, kind = _session_env()
        if kind == "wayland":
            # `wlopm` (installed on Raspberry Pi OS) prints "<output> on|off" per output
            if shutil.which("wlopm"):
                states = re.findall(r"^\S+\s+(on|off)\s*$", _run_display_cmd(["wlopm"], env).stdout, re.M)
                if states:
                    return "on" in states
            # Fallback: outputs reported as "Enabled: yes/no" by wlr-randr
            states = re.findall(r"Enabled:\s*(yes|no)", _run_display_cmd(["wlr-randr"], env).stdout)
            return ("yes" in states) if states else None
        m = re.search(r"Monitor is (On|Off|Standby|Suspend)", _run_display_cmd(["xset", "q"], env).stdout)
        if m:
            return m.group(1) == "On"
    except Exception:
        pass
    return None

def set_screen_power(on):
    """Turn the local display on or off. Returns True if the commands ran."""
    try:
        env, kind = _session_env()
        if kind == "wayland":
            if shutil.which("wlopm"):
                res = _run_display_cmd(["wlopm", "--on" if on else "--off", "*"], env)
                if res.returncode != 0:
                    print(f"wlopm failed: {res.stderr.strip()}")
                return res.returncode == 0
            for output in _wayland_outputs(env):
                _run_display_cmd(["wlr-randr", "--output", output, "--on" if on else "--off"], env)
            return True
        if on:
            _run_display_cmd(["xset", "dpms", "force", "on"], env)
            _run_display_cmd(["xset", "s", "reset"], env)
        else:
            _run_display_cmd(["xset", "+dpms"], env)
            _run_display_cmd(["xset", "dpms", "force", "off"], env)
        return True
    except Exception as e:
        print(f"Failed to switch screen {'on' if on else 'off'}: {e}")
        return False

screen_status = {"manager": "starting"}   # live view of the screen manager, see /api/screen-status
_screen_log_last = [None]

# The screen as the manager sees it. Shared with /api/screen (Home Assistant) and the display page.
screen = {
    "on": True,
    "changed": time.time(),   # when it last went on or off
    "off_at": 0.0,
    "idle_since": time.time(), # restarted by a touch or a manual wake
    "hold_off": False,         # switched off by hand (Home Assistant): stays off until a touch, a song or "on"
}
screen_wake = None             # asyncio.Event: a touch or a request wants the manager now
screen_request = [None]        # "on" / "off" from /api/screen
touch_watcher = None

def _screen_log(message):
    """Print a screen-manager status line only when it changes."""
    if _screen_log_last[0] != message:
        _screen_log_last[0] = message
        print(f"[screen] {message}")

def last_activity(started):
    """Last time something played: a song the microphone heard, or a JPlay player playing."""
    return max(pending_play["heard"] or started, last_player_activity[0])

def _screen_set(on, now):
    screen["on"] = on
    screen["changed"] = now
    if on:
        screen["idle_since"] = now
        screen["hold_off"] = False
    else:
        screen["off_at"] = now

async def background_screen_manager():
    """Turn the local screen off after N quiet minutes, and back on for a song, a touch or Home Assistant."""
    global screen_wake, touch_watcher
    loop = asyncio.get_running_loop()
    started = time.time()
    screen_wake = asyncio.Event()
    touched = [0.0]

    def on_touch():                       # runs in the watcher's thread
        touched[0] = time.time()
        loop.call_soon_threadsafe(screen_wake.set)

    touch_watcher = touchwake.InputWatcher(on_touch)
    touch_watcher.start()
    handled_touch = 0.0
    kiosk_active, kiosk_checked = False, 0.0
    while True:
        try:
            await asyncio.wait_for(screen_wake.wait(), 5)
        except asyncio.TimeoutError:
            pass
        screen_wake.clear()
        try:
            cfg = load_config()
            now = time.time()
            if now - kiosk_checked > 30:
                kiosk_active = await loop.run_in_executor(None, is_service_active, "kiosk")
                kiosk_checked = now
            minutes = to_int(cfg.get("screen_off_minutes"), 0, minimum=0, maximum=1440)

            # A touch on the screen: wake it, or (when on) count as activity
            if touched[0] > handled_touch:
                handled_touch = touched[0]
                if not screen["on"]:
                    if await loop.run_in_executor(None, set_screen_power, True):
                        _screen_set(True, now)
                        _screen_log("screen woken by a touch")
                else:
                    screen["idle_since"] = now

            # Home Assistant (or anything else) asked for on/off
            wanted, screen_request[0] = screen_request[0], None
            if wanted == "on" and await loop.run_in_executor(None, set_screen_power, True):
                _screen_set(True, now)
            elif wanted == "off" and await loop.run_in_executor(None, set_screen_power, False):
                _screen_set(False, now)
                screen["hold_off"] = True

            screen_status.update(
                manager="running", kiosk_active=kiosk_active, mic_enabled=cfg.get("mic_enabled", True),
                off_after_minutes=minutes,
                wake_on_song=cfg.get("screen_wake_on_song", True),
                seconds_since_last_song=round(now - last_activity(started)),
                # Which one keeps the screen on: the microphone hearing music, or this display's player playing
                seconds_since_mic_heard_song=round(now - (pending_play["heard"] or started)),
                seconds_since_player_played=round(now - last_player_activity[0]) if last_player_activity[0] else None,
                screen_on_assumed=screen["on"],
                switched_off_by_hand=screen["hold_off"],
                touch=touch_watcher.status(),
            )
            # Only manage the screen while the local display is in use and something is watched;
            # make sure we never leave it switched off otherwise.
            watching = cfg.get("mic_enabled", True) or bool(cfg.get("players"))
            if not (kiosk_active and watching):
                _screen_log(
                    f"idle: kiosk_active={kiosk_active} mic={cfg.get('mic_enabled', True)} players={len(cfg.get('players', []))} "
                    "(screen control only runs while the kiosk service is active and something is watched)"
                )
                if not screen["on"] and await loop.run_in_executor(None, set_screen_power, True):
                    _screen_set(True, now)
                continue

            heard = last_activity(started)   # last song heard or player playing

            # Notice if the screen was switched by other means (keyboard, another program)
            actual = await loop.run_in_executor(None, get_screen_power)
            _screen_log(
                f"kiosk_active={kiosk_active} mic={cfg.get('mic_enabled', True)} off_after={minutes}min "
                f"screen_on={screen['on']} reported_by_system={actual}"
            )
            screen_status.update(screen_reported_by_system=actual,
                                 seconds_idle=round(now - max(heard, screen["idle_since"])))
            if actual is not None and actual != screen["on"]:
                _screen_set(actual, now)

            if screen["on"]:
                if minutes > 0 and now - max(heard, screen["idle_since"]) > minutes * 60:
                    ok = await loop.run_in_executor(None, set_screen_power, False)
                    _screen_log(f"turning screen off after {minutes} min without a song: {'ok' if ok else 'FAILED'}")
                    if ok:
                        _screen_set(False, now)
            else:
                heard_new_song = heard > screen["off_at"]
                if (minutes == 0 and not screen["hold_off"]) or (cfg.get("screen_wake_on_song", True) and heard_new_song):
                    if await loop.run_in_executor(None, set_screen_power, True):
                        _screen_set(True, now)
        except Exception as e:
            screen_status["manager"] = f"error: {type(e).__name__}: {e}"
            print(f"Screen manager error: {e}")

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    cfg = load_config()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"config": cfg}
    )

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    cfg = load_config()
    system_status = {
        "kiosk_active": is_service_active("kiosk"),
        "ssh_active": is_service_active("ssh")
    }
    mic_index, mic_dev, mic_note = await asyncio.to_thread(resolve_mic, cfg)
    if str(cfg.get("input_device_name") or "") == MIC_DEFAULT:
        mic_selected = MIC_DEFAULT
    elif mic_dev:
        mic_selected = mic_dev["label"]
    else:
        mic_selected = mic_label(cfg.get("input_device_name")) or MIC_DEFAULT
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "config": cfg, "system": system_status, "is_local": is_local_request(request),
            "mics": await asyncio.to_thread(list_input_devices), "mic_selected": mic_selected,
            "mic_note": mic_note, "mic_default": MIC_DEFAULT,
            "spins": spins.status, "spins_seen": {
                k: (datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M:%S") if v else "never")
                for k, v in (("last_ok", spins.status.get("last_ok")), ("last_attempt", spins.status.get("last_attempt")))
            },
            "error": request.query_params.get("error", "")[:1800],
        }
    )

@app.post("/settings/save")
async def save_settings(request: Request):
    form = await request.form()
    cfg = load_config()
    
    cfg["theme"] = form.get("theme", "dark")
    cfg["screen_rotation"] = form.get("screen_rotation", "normal")
    cfg["mic_enabled"] = form.get("mic_enabled") == "on"
    cfg["mic_interval_seconds"] = to_int(
        form.get("mic_interval_seconds"), 8, minimum=3, maximum=60
    )
    mic_choice = str(form.get("mic_device") or "").strip()
    if mic_choice:
        cfg["input_device_name"] = mic_choice
        if mic_choice != MIC_DEFAULT:
            # Keep the current index too, for anything still reading the old setting
            match = next((d for d in list_input_devices() if d["label"] == mic_choice), None)
            if match:
                cfg["input_device_index"] = match["index"]

    names = form.getlist("player_name")
    hosts = form.getlist("player_host")
    ports = form.getlist("player_port")
    enabled_rows = set(form.getlist("player_enabled"))  # row indexes that are ticked
    cfg["screen_off_minutes"] = to_int(form.get("screen_off_minutes"), 0, minimum=0, maximum=1440)
    cfg["screen_wake_on_song"] = form.get("screen_wake_on_song") == "on"
    cfg["radio_listen"] = form.get("radio_listen") == "on"
    cfg["lyrics_enabled"] = form.get("lyrics_enabled") == "on"
    cfg["update_channel"] = "main" if form.get("update_channel") == "main" else "releases"
    cfg["update_auto"] = "night" if form.get("update_auto") == "night" else "off"
    try:
        cfg["lyrics_offset_seconds"] = max(-30.0, min(30.0, float(form.get("lyrics_offset_seconds") or 0)))
    except ValueError:
        cfg["lyrics_offset_seconds"] = 0

    updated = []
    for i, host in enumerate(hosts):
        host = host.strip()
        if not host:
            continue
        name = names[i].strip() if i < len(names) else ""
        port = to_int(ports[i] if i < len(ports) else None, players.DEFAULT_PORT, minimum=1, maximum=65535)
        updated.append({
            "id": f"player_{len(updated) + 1}",
            "name": name or f"Player {len(updated) + 1}",
            "host": host,
            "port": port,
            "enabled": str(i) in enabled_rows,
        })
    cfg["players"] = updated
    # Which player this display shows: the form sends its row number (ids are renumbered above).
    view_row = to_int(form.get("view_player_row"), -1)
    kept = [i for i, h in enumerate(hosts) if h.strip()]
    cfg["view_player"] = updated[kept.index(view_row)]["id"] if view_row in kept else (updated[0]["id"] if updated else "")

    cfg["spins_enabled"] = form.get("spins_enabled") == "on"
    cfg["spins_url"] = str(form.get("spins_url") or "").strip().rstrip("/") or DEFAULT_CONFIG["spins_url"]
    new_token = str(form.get("spins_token") or "").strip()
    if new_token:                      # empty field = keep the saved token
        cfg["spins_token"] = new_token
    cfg["spins_room_player"] = str(form.get("spins_room_player") or "").strip()
    save_config(cfg)

    enable_kiosk = form.get("local_kiosk") == "on"
    enable_ssh = form.get("ssh_enabled") == "on"

    errors = []
    # Only touch a service when its wanted state differs from the current one
    if enable_kiosk != is_service_active("kiosk"):
        err = await asyncio.to_thread(set_service_state, "kiosk", enable_kiosk)
        if not err and enable_kiosk:
            # The command succeeded, but the service may have crashed right after starting
            await asyncio.sleep(3)
            if not is_service_active("kiosk"):
                err = "'kiosk' was enabled but is not running:\n" + await asyncio.to_thread(service_status_text, "kiosk")
        errors.append(err)
    # SSH can only be changed from the Pi itself; remote requests leave it untouched
    if is_local_request(request) and enable_ssh != is_service_active("ssh"):
        errors.append(await asyncio.to_thread(set_service_state, "ssh", enable_ssh))
    set_screen_rotation(cfg["screen_rotation"])

    error = "\n".join(e for e in errors if e)
    url = "/settings" + (f"?error={quote(error)}" if error else "")
    return RedirectResponse(url=url, status_code=303)

@app.post("/system/reboot")
async def reboot_system(request: Request):
    if not is_local_request(request):
        return JSONResponse({"error": "Only allowed from the Pi itself"}, status_code=403)
    try:
        subprocess.run(["sudo", "reboot"], check=True)
        return {"status": "Rebooting..."}
    except Exception as e:
        return {"error": str(e)}

@app.post("/system/shutdown")
async def shutdown_system(request: Request):
    if not is_local_request(request):
        return JSONResponse({"error": "Only allowed from the Pi itself"}, status_code=403)
    try:
        subprocess.run(["sudo", "shutdown", "-h", "now"], check=True)
        return {"status": "Shutting down..."}
    except Exception as e:
        return {"error": str(e)}

@app.get("/player/{player_id}", response_class=HTMLResponse)
async def player_page(request: Request, player_id: str):
    """A JPlay player's own web page (sources, browsing, controls), with a bar back to the display."""
    cfg = load_config()
    player = next((p for p in cfg.get("players", []) if p.get("id") == player_id), None)
    if not player:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="player.html",
        context={"config": cfg, "player": player, "player_url": players.base_url(player) + "/"},
    )

@app.get("/manifest.webmanifest")
async def web_manifest():
    """Lets phones install JPlaying Now as an app (Add to Home Screen / Install app)."""
    return JSONResponse({
        "name": "JPlaying Now",
        "short_name": "JPlaying Now",
        "description": "What's playing on the JPlay players and the ambient listener",
        "id": "/",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#0f111a",
        "theme_color": "#0f111a",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }, media_type="application/manifest+json")

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return RedirectResponse(url="/static/icons/favicon.ico", status_code=301)

@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    return templates.TemplateResponse(request=request, name="history.html", context={"config": load_config()})

def _history_filters(request: Request):
    q = request.query_params
    return {k: q.get(k, "").strip() for k in ("start", "end", "genre", "artist", "source")}

@app.get("/api/history")
async def api_history(request: Request):
    q = request.query_params
    limit = to_int(q.get("limit"), 100, minimum=1, maximum=500)
    offset = to_int(q.get("offset"), 0, minimum=0)
    return await asyncio.to_thread(history.query_plays, _history_filters(request), limit, offset)

@app.get("/api/history/stats")
async def api_history_stats(request: Request):
    limit = to_int(request.query_params.get("limit"), 20, minimum=1, maximum=100)
    return await asyncio.to_thread(history.stats, _history_filters(request), limit)

@app.get("/api/history/options")
async def api_history_options():
    return await asyncio.to_thread(history.filter_options)

@app.get("/api/history/export.csv")
async def api_history_export(request: Request):
    data = await asyncio.to_thread(history.export_csv, _history_filters(request))
    return Response(
        content=data, media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="nowplaying_history.csv"'},
    )

@app.get("/api/spins-status")
async def api_spins_status():
    """JPlay Spins outbox state (for troubleshooting)."""
    return spins.status

@app.get("/api/screen-status")
async def api_screen_status():
    """What the screen manager currently sees (for troubleshooting)."""
    return screen_status

@app.get("/api/players-status")
async def api_players_status():
    """Raw player status and the radio listener's last result per player (for troubleshooting)."""
    return {"players": player_status, "radio_listener": radio.status, "ffmpeg": bool(shutil.which("ffmpeg"))}

def view_player(cfg):
    """The player this display shows: the chosen one, else the first enabled one."""
    enabled = [p for p in cfg.get("players", []) if p.get("enabled", True) and p.get("host")]
    return next((p for p in enabled if p["id"] == cfg.get("view_player")), enabled[0] if enabled else None)

@app.get("/api/dashboard")
async def get_dashboard():
    cfg = load_config()
    # Served from the background poller's cache, so the page never waits on a player
    p = view_player(cfg)
    return {
        "config": cfg,
        "player": player_cards.get(p["id"]) if p else None,
        "players": [{"id": x["id"], "name": x["name"]} for x in cfg.get("players", []) if x.get("enabled", True)],
        "view": p["id"] if p else "",
        "radio_listen": bool(cfg.get("radio_listen", True) and shutil.which("ffmpeg")),
        "ambient": latest_ambient_state,
        "screen": {"on": screen["on"], "changed": screen["changed"]},
        "lyrics": lyrics_track(cfg, player_cards.get(p["id"]) if p else None, time.time()),
        "update_available": update_available[0],
    }

def lyrics_track(cfg, card, now):
    """The song on the screen, for its lyrics: who, what, and where in the song we are right now.

    position is None when it isn't known (a radio station naming the song, before it is recognised);
    the page then scrolls the lyrics slowly instead of following them line by line.
    """
    if not cfg.get("lyrics_enabled", True):
        return None
    try:
        shift = float(cfg.get("lyrics_offset_seconds") or 0)
    except (TypeError, ValueError):
        shift = 0.0
    pos = None
    if card and card.get("online") and card.get("source") and card.get("show"):
        if not card.get("song_known") or not card.get("title"):
            return None
        if card["source"] == "radio":
            if card.get("recognised") and card.get("heard_offset") is not None and card.get("heard_at"):
                pos = card["heard_offset"] + (now - card["heard_at"])
        elif card.get("position") is not None:
            pos = card["position"] + ((now - card.get("polled_at", now)) if card.get("playing") else 0)
        song = {"artist": card.get("artist", ""), "title": card["title"], "album": card.get("album", ""),
                "duration": card.get("duration") or card.get("song_duration"), "playing": bool(card.get("playing"))}
    else:
        a = latest_ambient_state
        if not a.get("identified"):
            return None
        if a.get("offset") is not None and a.get("heard_at"):
            pos = a["offset"] + (now - a["heard_at"])
        song = {"artist": a.get("artist", ""), "title": a.get("title", ""), "album": a.get("album", ""),
                "duration": a.get("duration"), "playing": True}
    song["key"] = f"{song['artist']}\n{song['title']}".lower()
    song["position"] = round(pos + shift, 2) if pos is not None else None
    return song

# ---------------------------------------------------------------- versions and updates
update_available = [False]     # for the dot on the display's Settings button

def update_status():
    st = UPDATES.status(load_config().get("update_channel", "releases"))
    update_available[0] = bool(st["available"])
    return st

@app.get("/api/version")
async def api_version():
    """This app's version (also what the updater asks to see that the app started)."""
    return await asyncio.get_running_loop().run_in_executor(None, UPDATES.version)

@app.get("/api/update")
async def api_update():
    """Version, the newest release, its notes, and how an update is going."""
    return await asyncio.get_running_loop().run_in_executor(None, update_status)

@app.post("/api/update/check")
async def api_update_check():
    try:
        await asyncio.get_running_loop().run_in_executor(None, UPDATES.check)
    except updates.UpdateError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    return {"ok": True}

@app.post("/api/update/install")
async def api_update_install(request: Request):
    """Update to the newest version (or {"target": "v1.6.0"}). The updater restarts this app when done."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    st = await asyncio.get_running_loop().run_in_executor(None, update_status)
    target = body.get("target") or st["target"]
    if not target:
        return JSONResponse({"error": "Already up to date"}, status_code=409)
    try:
        await asyncio.get_running_loop().run_in_executor(None, UPDATES.install, target)
    except updates.UpdateError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    return {"ok": True, "target": target}

async def background_auto_update():
    """Every minute: refresh the 'update available' dot, and install at night when that is switched on."""
    loop = asyncio.get_running_loop()
    await asyncio.sleep(5)
    while True:
        try:
            cfg = load_config()
            await loop.run_in_executor(None, update_status)
            # Busy: a song was heard or this display's player played in the last 10 minutes
            busy = time.time() - max(pending_play["heard"] or 0, last_player_activity[0]) < 600
            target = await loop.run_in_executor(None, UPDATES.auto_target, cfg.get("update_auto", "off"),
                                                cfg.get("update_channel", "releases"), busy)
            if target:
                print(f"[update] installing {target} automatically")
                await loop.run_in_executor(None, UPDATES.install, target)
        except Exception as e:
            print(f"[update] {type(e).__name__}: {e}")
        await asyncio.sleep(60)

@app.get("/api/lyrics")
async def get_lyrics(artist: str = "", title: str = "", album: str = "", duration: float | None = None):
    """The lyrics of a song (LRCLIB, cached on the Pi): synced lines with times, or the plain text."""
    cfg = load_config()
    # JPlay Records keeps the lyrics for the whole house (same address and token as JPlay Spins)
    records = (cfg["spins_url"], cfg["spins_token"]) if spins.enabled(cfg) else None
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: lyrics.lookup(artist, title, album, duration, records))

@app.get("/api/screen")
async def get_screen():
    """Is the Pi's own screen on? (for Home Assistant)"""
    return {"on": screen["on"], "changed": screen["changed"], "switched_off_by_hand": screen["hold_off"]}

@app.post("/api/screen")
async def set_screen(request: Request):
    """Switch the Pi's own screen on or off: {"on": true|false}. Off stays off until a touch, a song or on."""
    body = await request.json()
    screen_request[0] = "on" if body.get("on") else "off"
    if screen_wake is not None:
        screen_wake.set()
    for _ in range(40):                   # wait (up to 4 s) for the manager to do it
        await asyncio.sleep(0.1)
        if screen_request[0] is None:
            break
    return await get_screen()

@app.post("/api/view")
async def set_view(request: Request):
    """Switch the display to another player (from the menu on the display)."""
    body = await request.json()
    cfg = load_config()
    wanted = str(body.get("player") or "")
    if not any(p["id"] == wanted for p in cfg.get("players", [])):
        return JSONResponse({"error": "Unknown player"}, status_code=404)
    cfg["view_player"] = wanted
    save_config(cfg)
    return {"view": wanted}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
