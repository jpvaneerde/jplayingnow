import asyncio
import io
import json
import os
import re
import ctypes

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

import shutil
import socket
import subprocess
import time
from datetime import datetime
from urllib.parse import quote, unquote
import wave
import requests
import sounddevice as sd
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from shazamio import Shazam
from mpd import MPDClient
import history

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
    tasks = [
        asyncio.create_task(background_mic_listener()),
        asyncio.create_task(background_moode_poller()),
        asyncio.create_task(background_screen_manager()),
    ]
    cfg = load_config()
    set_screen_rotation(cfg.get("screen_rotation", "normal"))
    proxy = await start_moode_proxy()

    yield

    for t in tasks:
        t.cancel()
    if proxy:
        await proxy()

app = FastAPI(lifespan=lifespan)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
# Local, untracked files: settings and history live on the device and are never overwritten by git
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
CONFIG_EXAMPLE_FILE = os.path.join(BASE_DIR, "config.example.json")
DATA_DIR = os.path.join(BASE_DIR, "data")
HISTORY_DB = os.path.join(DATA_DIR, "history.db")

os.makedirs(STATIC_DIR, exist_ok=True)
templates = Jinja2Templates(directory=STATIC_DIR)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

shazam = Shazam()

DEFAULT_CONFIG = {
    "theme": "dark",
    "display_mode": "grid",
    "screen_rotation": "normal",
    "mic_enabled": True,
    "mic_interval_seconds": 8,
    "input_device_index": 1,
    "sample_rate": 16000,
    "hide_inactive_moodes": False,
    "screen_off_minutes": 0,          # 0 = never turn the local screen off
    "screen_wake_on_song": True,      # turn the screen back on when the ambient listener hears a song
    "moodes": []
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
    default = {"art": "/static/default_cover.png", "year": "", "album": "", "genre": ""}
    if not title or title in ["Idle", "Unknown Track", "Offline"]:
        return default
    key = (artist, title)
    if key in _itunes_cache:
        return _itunes_cache[key]
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
            }
            if len(_itunes_cache) > 200:
                _itunes_cache.clear()
            _itunes_cache[key] = info
            return info
    except Exception:
        pass
    return default

_moode_debug_last = {}
_logo_cache = {}   # (host, station) -> (url, expiry timestamp)

def _moode_rel(url):
    """Normalise a moOde coverurl to a decoded path relative to the player's web root.
    moOde may send encoded slashes ('imagesw%2Fradio-logos%2FBBC%20Radio%201.jpg')."""
    if url.startswith(("http://", "https://")):
        return url
    return unquote(url).lstrip("/")

def _moode_abs(host, rel):
    if rel.startswith(("http://", "https://")):
        return rel
    return f"http://{host}/{quote(rel, safe='/')}"

def _moode_debug(host, message):
    """Print only when the message changes, so the log isn't flooded every poll."""
    if _moode_debug_last.get(host) != message:
        _moode_debug_last[host] = message
        print(f"[moOde {host}] {message}")

def find_station_logo(host, station):
    """Look for the radio station logo in moOde's radio-logos folder. Cached."""
    key = (host, station)
    hit = _logo_cache.get(key)
    if hit and hit[1] > time.time():
        return hit[0]
    found = ""
    name = quote(station)
    for path in (f"/imagesw/radio-logos/{name}.jpg", f"/imagesw/radio-logos/{name}.png",
                 f"/imagesw/radio-logos/thumbs/{name}.jpg"):
        url = f"http://{host}{path}"
        try:
            r = requests.head(url, timeout=2, allow_redirects=True)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                found = unquote(path).lstrip("/")
                break
        except Exception:
            break
    _logo_cache[key] = (found, time.time() + (3600 if found else 120))
    return found

def fetch_moode_art(host, station="", is_radio=False):
    """Ask moOde itself for the current cover; for radio, fall back to the station logo.

    Returns (path_or_url, note). The note says where the art came from or why none was found;
    it is exposed in /api/dashboard as "art_debug" to make problems easy to diagnose.
    """
    url = ""
    album = ""
    notes = []
    # moOde placeholders are all named default-*.jpg/png/svg (default-radio-cover.jpg, ...)
    def usable(u):
        return bool(u) and not unquote(u).rsplit("/", 1)[-1].lower().startswith("default")

    # get_currentsong is the endpoint that returns the real radio logo (it needs
    # moOde's "Metadata file" option ON); engine-mpd.php is only a fallback because
    # it can report the generic radio placeholder for the same stream.
    for endpoint in ("command/?cmd=get_currentsong", "engine-mpd.php?cmd=status"):
        try:
            data = requests.get(f"http://{host}/{endpoint}", timeout=6).json()
            found = (data.get("coverurl") or "").strip()
            album = album or str(data.get("album") or "")
            notes.append(
                f"{endpoint} @ {host}: coverurl={found!r} album={data.get('album')!r} "
                f"state={data.get('state')!r} file={data.get('file')!r}"
            )
            url = url or found
            if usable(found):
                url = found
                break
        except Exception as e:
            notes.append(f"{endpoint} @ {host} failed: {type(e).__name__}: {e}")
    note = " | ".join(notes)
    _moode_debug(host, note)
    if usable(url):
        return _moode_rel(url), note
    # No real cover: for radio, moOde stores logos as radio-logos/<station name>.jpg.
    # The station name is MPD's "name" tag or moOde's "album" field (unless "Unknown station").
    for name in (station, album) if is_radio else ():
        if name and name.lower() != "unknown station":
            logo = find_station_logo(host, name)
            if logo:
                return logo, note + f" | logo found via station name {name!r}"
    return "", note + " | no usable cover or station logo"

def check_moode(instance):
    try:
        client = MPDClient()
        client.timeout = 1
        client.connect(instance["host"], int(instance["port"]))
        status = client.status()
        song = client.currentsong()
        client.disconnect()

        is_playing = status.get("state") == "play"
        title = song.get("title", song.get("name", "Idle" if not is_playing else "Live Stream"))
        artist = song.get("artist", "")
        art_debug = ""
        if is_playing:
            info = fetch_online_info(artist, title)
            # moOde's own cover (local art, embedded art, radio logos) beats an iTunes guess
            is_radio = str(song.get("file", "")).startswith("http")
            station = song.get("name", "") if is_radio else ""
            rel, art_debug = fetch_moode_art(instance["host"], station, is_radio)
            art = _moode_abs(instance["host"], rel) if rel else info["art"]
        else:
            info = {"art": "/static/idle.png", "year": "", "album": "", "genre": ""}
            art = info["art"]
        # Prefer MPD tags (usually more accurate), fall back to iTunes
        year = extract_year(song.get("date") or song.get("originaldate")) or info["year"]
        album = song.get("album") or info["album"]
        genre = song.get("genre") or info["genre"]
        if isinstance(genre, list):
            genre = ", ".join(genre)
        if isinstance(album, list):
            album = album[0]

        return {
            "id": instance["id"],
            "name": instance["name"],
            "playing": is_playing,
            "title": title,
            "artist": artist,
            "year": year if is_playing else "",
            "album": album if is_playing else "",
            "genre": genre if is_playing else "",
            "coverart": art,
            "art_debug": art_debug
        }
    except Exception as e:
        print(f"[moOde {instance.get('host')}] check failed: {type(e).__name__}: {e}")
        return {
            "id": instance["id"],
            "name": instance["name"],
            "playing": False,
            "title": "Offline",
            "artist": "Connection Failed",
            "coverart": "/static/offline.png"
        }



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

moode_state = {}

async def background_moode_poller():
    """Poll enabled moOde players and cache their state for the display."""
    global moode_state
    loop = asyncio.get_running_loop()
    while True:
        try:
            cfg = load_config()
            instances = [i for i in cfg.get("moodes", []) if i.get("enabled", True)]
            results = await asyncio.gather(
                *(loop.run_in_executor(None, check_moode, i) for i in instances)
            )
            moode_state = {r["id"]: r for r in results}  # display only; not logged to history
        except Exception as e:
            print(f"moOde poller error: {e}")
        await asyncio.sleep(3)

MIN_PLAY_SECONDS = 20          # a song must be heard this long before it enters history
MIC_SOURCE_NAME = "Ambient Listener"
pending_play = {"key": None, "first": None, "last": None, "logged": False, "heard": None}
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
                        "coverart": cover
                    }
                    # Only log once the same song has been heard for MIN_PLAY_SECONDS
                    s = latest_ambient_state
                    now = time.time()
                    pending_play["heard"] = now
                    key = (s["title"].lower(), s["artist"].lower())
                    if pending_play["key"] != key:
                        pending_play.update(key=key, first=now - duration, last=now, logged=False)
                    else:
                        pending_play["last"] = now
                    if not pending_play["logged"] and now - pending_play["first"] >= MIN_PLAY_SECONDS:
                        pending_play["logged"] = True
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
                        pending_play.update(key=None, first=None, last=None, logged=False)
                    quiet_after = to_int(cfg.get("quiet_after_seconds"), QUIET_AFTER_SECONDS, minimum=10)
                    silent_for = now - (pending_play["heard"] or listener_started)
                    if silent_for > quiet_after:
                        # Nothing recognised for a while: stop showing the last song
                        latest_ambient_state = quiet_state()
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

def _screen_log(message):
    """Print a screen-manager status line only when it changes."""
    if _screen_log_last[0] != message:
        _screen_log_last[0] = message
        print(f"[screen] {message}")

async def background_screen_manager():
    """Turn the local screen off after N quiet minutes and back on when a song is heard."""
    loop = asyncio.get_running_loop()
    started = time.time()
    screen_on = True
    off_at = 0.0
    idle_since = started          # restarted whenever someone wakes the screen by hand
    kiosk_active, kiosk_checked = False, 0.0
    while True:
        await asyncio.sleep(5)
        try:
            cfg = load_config()
            now = time.time()
            if now - kiosk_checked > 30:
                kiosk_active = await loop.run_in_executor(None, is_service_active, "kiosk")
                kiosk_checked = now

            screen_status.update(
                manager="running", kiosk_active=kiosk_active, mic_enabled=cfg.get("mic_enabled", True),
                off_after_minutes=to_int(cfg.get("screen_off_minutes"), 0, minimum=0, maximum=1440),
                wake_on_song=cfg.get("screen_wake_on_song", True),
                seconds_since_last_song=round(now - (pending_play["heard"] or started)),
                screen_on_assumed=screen_on,
            )
            # Only manage the screen while the local display is in use and the mic is listening;
            # make sure we never leave it switched off otherwise.
            if not (kiosk_active and cfg.get("mic_enabled", True)):
                _screen_log(
                    f"idle: kiosk_active={kiosk_active} mic={cfg.get('mic_enabled', True)} "
                    "(screen control only runs while the kiosk service is active and the mic is on)"
                )
                if not screen_on and await loop.run_in_executor(None, set_screen_power, True):
                    screen_on, idle_since = True, now
                continue

            minutes = to_int(cfg.get("screen_off_minutes"), 0, minimum=0, maximum=1440)
            heard = pending_play["heard"] or started   # last time the ambient listener heard a song

            # Notice if the screen was switched by hand (touch/keyboard wakes it)
            actual = await loop.run_in_executor(None, get_screen_power)
            _screen_log(
                f"kiosk_active={kiosk_active} mic={cfg.get('mic_enabled', True)} off_after={minutes}min "
                f"screen_on={screen_on} reported_by_system={actual}"
            )
            screen_status.update(screen_reported_by_system=actual,
                                 seconds_idle=round(now - max(heard, idle_since)))
            if actual is not None and actual != screen_on:
                screen_on = actual
                if actual:
                    idle_since = now      # fresh countdown after a manual wake

            if screen_on:
                if minutes > 0 and now - max(heard, idle_since) > minutes * 60:
                    ok = await loop.run_in_executor(None, set_screen_power, False)
                    _screen_log(f"turning screen off after {minutes} min without a song: {'ok' if ok else 'FAILED'}")
                    if ok:
                        screen_on, off_at = False, now
            else:
                heard_new_song = heard > off_at
                if minutes == 0 or (cfg.get("screen_wake_on_song", True) and heard_new_song):
                    if await loop.run_in_executor(None, set_screen_power, True):
                        screen_on, idle_since = True, now
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
            "error": request.query_params.get("error", "")[:1800],
        }
    )

@app.post("/settings/save")
async def save_settings(request: Request):
    form = await request.form()
    cfg = load_config()
    
    cfg["theme"] = form.get("theme", "dark")
    cfg["display_mode"] = form.get("display_mode", "grid")
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

    names = form.getlist("moode_name")
    hosts = form.getlist("moode_host")
    ports = form.getlist("moode_port")
    enabled_rows = set(form.getlist("moode_enabled"))  # row indexes that are ticked
    cfg["hide_inactive_moodes"] = form.get("hide_inactive_moodes") == "on"
    cfg["screen_off_minutes"] = to_int(form.get("screen_off_minutes"), 0, minimum=0, maximum=1440)
    cfg["screen_wake_on_song"] = form.get("screen_wake_on_song") == "on"

    updated_moodes = []
    for i, host in enumerate(hosts):
        host = host.strip()
        if not host:
            continue
        name = names[i].strip() if i < len(names) else ""
        port = to_int(ports[i] if i < len(ports) else None, 6600, minimum=1, maximum=65535)
        updated_moodes.append({
            "id": f"moode_{len(updated_moodes)+1}",
            "name": name or f"moOde {len(updated_moodes)+1}",
            "host": host,
            "port": port,
            "enabled": str(i) in enabled_rows
        })

    cfg["moodes"] = updated_moodes
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

# --- moOde web UI relay -------------------------------------------------------------
# moOde keeps its state in a PHP session cookie. Embedded straight from the player's own
# address that cookie is "third-party" and the browser drops it, leaving moOde blank.
# So moOde is relayed through this Pi on its own port (same site as the dashboard).
# Which player it talks to is remembered in the "jpn_moode" cookie.
MOODE_PROXY_PORT = 5100
_HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
                "trailers", "transfer-encoding", "upgrade", "host", "content-length",
                "x-frame-options"}

def moode_proxy_port(cfg=None):
    return to_int((cfg or load_config()).get("moode_proxy_port"), MOODE_PROXY_PORT, minimum=1024, maximum=65535)

async def start_moode_proxy():
    """Start the relay. Returns an async cleanup function, or None if it could not start."""
    try:
        from aiohttp import web, ClientSession, ClientTimeout
    except ImportError:
        print("[moOde relay] aiohttp is not installed; moOde controls are unavailable")
        return None

    client = ClientSession(auto_decompress=False,
                           timeout=ClientTimeout(total=None, sock_connect=5, sock_read=120))

    async def handler(request):
        if request.path.startswith("/__jpn/select/"):
            resp = web.Response(status=302, headers={"Location": "/", "Cache-Control": "no-store"})
            resp.set_cookie("jpn_moode", request.path.rsplit("/", 1)[-1], path="/", samesite="Lax")
            return resp
        player_id = request.cookies.get("jpn_moode", "")
        player = next((m for m in load_config().get("moodes", []) if m.get("id") == player_id), None)
        if not player:
            return web.Response(status=404, content_type="text/html",
                                text="<p style='font-family:sans-serif'>No moOde player selected. "
                                     "Go back to JPlaying Now and tap a player.</p>")
        base = f"http://{player['host']}"
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}
        body = await request.read()
        try:
            async with client.request(request.method, base + str(request.rel_url), headers=headers,
                                      data=body or None, allow_redirects=False) as up:
                data = await up.read()
                resp = web.Response(status=up.status, reason=up.reason, body=data)
                for k, v in up.headers.items():
                    if k.lower() in _HOP_HEADERS:
                        continue
                    if k.lower() == "location":
                        v = v.replace(base, "", 1)        # keep redirects on the relay
                    resp.headers.add(k, v)
                return resp
        except Exception as e:
            return web.Response(status=502, text=f"moOde '{player.get('name')}' ({player['host']}) "
                                                 f"is not reachable: {type(e).__name__}: {e}")

    relay = web.Application(client_max_size=64 * 1024 * 1024)
    relay.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(relay, access_log=None)
    await runner.setup()
    port = moode_proxy_port()
    try:
        await web.TCPSite(runner, "0.0.0.0", port).start()
        print(f"[moOde relay] listening on port {port}")
    except OSError as e:
        print(f"[moOde relay] could not listen on port {port}: {e}")
        await runner.cleanup()
        await client.close()
        return None

    async def stop():
        await runner.cleanup()
        await client.close()
    return stop

@app.get("/moode/{moode_id}", response_class=HTMLResponse)
async def moode_page(request: Request, moode_id: str):
    """Full-screen moOde web UI for one player, with a bar to get back to the dashboard."""
    cfg = load_config()
    player = next((m for m in cfg.get("moodes", []) if m.get("id") == moode_id), None)
    if not player:
        return RedirectResponse(url="/", status_code=303)
    host = request.url.hostname or "localhost"
    if ":" in host:
        host = f"[{host}]"   # IPv6 literal
    moode_url = f"http://{host}:{moode_proxy_port(cfg)}/__jpn/select/{quote(moode_id, safe='')}"
    return templates.TemplateResponse(
        request=request,
        name="moode.html",
        context={"config": cfg, "player": player, "moode_url": moode_url},
    )

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

@app.get("/api/screen-status")
async def api_screen_status():
    """What the screen manager currently sees (for troubleshooting)."""
    return screen_status

@app.get("/api/dashboard")
async def get_dashboard():
    cfg = load_config()
    # Served from the background poller's cache, so the page never waits on MPD
    moode_data = [
        moode_state[i["id"]]
        for i in cfg.get("moodes", [])
        if i.get("enabled", True) and i["id"] in moode_state
    ]
    if cfg.get("hide_inactive_moodes"):
        # Show a player only while it is actually playing (hides paused/stopped/offline ones)
        moode_data = [m for m in moode_data if m.get("playing")]

    return {
        "config": cfg,
        "moodes": moode_data,
        "ambient": latest_ambient_state
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
