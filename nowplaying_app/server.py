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

import socket
import subprocess
import time
from datetime import datetime
from urllib.parse import quote
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
    history.purge_other_sources(MIC_SOURCE_NAME)   # history holds mic-detected songs only
    tasks = [
        asyncio.create_task(background_mic_listener()),
        asyncio.create_task(background_moode_poller()),
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

def set_service_state(service_name, enable: bool):
    try:
        if enable:
            subprocess.run(["sudo", "systemctl", "enable", "--now", service_name], check=True)
        else:
            subprocess.run(["sudo", "systemctl", "disable", "--now", service_name], check=True)
    except Exception as e:
        print(f"Failed to set state for {service_name}: {e}")

def set_screen_rotation(rotation: str):
    try:
        env = os.environ.copy()
        env["DISPLAY"] = ":0"
        subprocess.run(["xrandr", "-o", rotation], env=env, check=False)
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
    "name": "Ambient Listener (CD)",
    "title": "Listening...",
    "artist": "Waiting for audio",
    "coverart": "/static/idle.png"
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

def _moode_abs(host, url):
    if url.startswith(("http://", "https://")):
        return url
    return f"http://{host}/{url.lstrip('/')}"

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
                found = url
                break
        except Exception:
            break
    _logo_cache[key] = (found, time.time() + (3600 if found else 120))
    return found

def fetch_moode_art(host, station=""):
    """Ask moOde itself for the current cover; for radio, fall back to the station logo."""
    url = ""
    # engine-mpd.php?cmd=status is confirmed to return coverurl on this setup
    for endpoint in ("engine-mpd.php?cmd=status", "command/?cmd=get_currentsong"):
        try:
            data = requests.get(f"http://{host}/{endpoint}", timeout=2).json()
            url = (data.get("coverurl") or "").strip()
            _moode_debug(host, f"{endpoint}: coverurl={url!r} station={station!r}")
            break
        except Exception as e:
            _moode_debug(host, f"{endpoint} failed: {e}")
    # moOde placeholders are all named default-*.jpg/png/svg (default-radio-cover.jpg, ...)
    if url and not url.rsplit("/", 1)[-1].lower().startswith("default"):
        return _moode_abs(host, url)
    if station:
        return find_station_logo(host, station)
    return ""

SR_STREAM_RE = re.compile(r"sverigesradio\.se/topsy/direkt/(\d+)", re.I)
_sr_cache = {}   # key -> (data, expiry)

def _sr_get(url, ttl):
    hit = _sr_cache.get(url)
    if hit and hit[1] > time.time():
        return hit[0]
    data = {}
    try:
        data = requests.get(url, timeout=3).json()
    except Exception as e:
        print(f"Sveriges Radio API error: {e}")
    _sr_cache[url] = (data, time.time() + ttl)
    return data

def fetch_sr_info(channel_id):
    """Station name, logo and current song for a Sveriges Radio channel (open API, no key)."""
    ch = _sr_get(f"https://api.sr.se/api/v2/channels/{channel_id}?format=json", 3600).get("channel", {})
    now = _sr_get(f"https://api.sr.se/api/v2/playlists/rightnow?channelid={channel_id}&format=json", 20)
    song = now.get("playlist", {}).get("song", {}) or {}
    return {
        "station": ch.get("name", ""),
        "image": ch.get("image") or ch.get("imagetemplate") or "",
        "title": song.get("title", ""),
        "artist": song.get("artist") or song.get("composer") or "",
        "album": song.get("albumname", ""),
    }

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
        sr_match = SR_STREAM_RE.search(str(song.get("file", ""))) if is_playing else None
        if sr_match:
            # Sveriges Radio stream: use the SR open API for station logo and current song
            sr = fetch_sr_info(sr_match.group(1))
            if not song.get("title"):               # stream sends no metadata of its own
                title = sr["title"] or sr["station"] or title
                artist = sr["artist"] or (sr["station"] if sr["title"] else "")
            info = {"art": sr["image"] or "/static/default_cover.png", "year": "",
                    "album": sr["album"], "genre": ""}
            art = info["art"]
        elif is_playing:
            info = fetch_online_info(artist, title)
            # moOde's own cover (local art, embedded art, radio logos) beats an iTunes guess
            is_radio = str(song.get("file", "")).startswith("http")
            station = song.get("name", "") if is_radio else ""
            art = fetch_moode_art(instance["host"], station) or info["art"]
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
            "coverart": art
        }
    except Exception:
        return {
            "id": instance["id"],
            "name": instance["name"],
            "playing": False,
            "title": "Offline",
            "artist": "Connection Failed",
            "coverart": "/static/offline.png"
        }



def record_audio(duration):
    cfg = load_config()
    MIC_DEVICE = to_int(cfg.get("input_device_index"), 1)

    # Configured rate first, then standard webcam rates as fallbacks
    rates = [to_int(cfg.get("sample_rate"), 16000)]
    rates += [r for r in (16000, 48000, 44100) if r not in rates]
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
MIC_SOURCE_NAME = "Ambient Listener (CD)"
pending_play = {"key": None, "first": None, "last": None, "logged": False}

async def background_mic_listener():
    global latest_ambient_state
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
                        "name": "Ambient Listener (CD)",
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
                    # Forget the candidate if the song has been gone for a while
                    if pending_play["last"] and time.time() - pending_play["last"] > 40:
                        pending_play.update(key=None, first=None, last=None, logged=False)
                    # Keep showing the last identified song if a later sample finds nothing
                    if not latest_ambient_state.get("identified"):
                        latest_ambient_state["title"] = "No Song Identified"
                        latest_ambient_state["artist"] = "Listening..."
            except Exception as e:
                latest_ambient_state["title"] = "Mic Error"
                latest_ambient_state["artist"] = str(e)
                await asyncio.sleep(3)
        else:
            latest_ambient_state["title"] = "Mic Disabled"
            latest_ambient_state["artist"] = "Disabled in settings"
            await asyncio.sleep(5)

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
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={"config": cfg, "system": system_status, "is_local": is_local_request(request)}
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

    names = form.getlist("moode_name")
    hosts = form.getlist("moode_host")
    ports = form.getlist("moode_port")
    enabled_rows = set(form.getlist("moode_enabled"))  # row indexes that are ticked

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

    set_service_state("kiosk", enable_kiosk)
    # SSH can only be changed from the Pi itself; remote requests leave it untouched
    if is_local_request(request):
        set_service_state("ssh", enable_ssh)
    set_screen_rotation(cfg["screen_rotation"])

    return RedirectResponse(url="/settings", status_code=303)

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

@app.get("/api/dashboard")
async def get_dashboard():
    cfg = load_config()
    # Served from the background poller's cache, so the page never waits on MPD
    moode_data = [
        moode_state[i["id"]]
        for i in cfg.get("moodes", [])
        if i.get("enabled", True) and i["id"] in moode_state
    ]

    return {
        "config": cfg,
        "moodes": moode_data,
        "ambient": latest_ambient_state
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
