import asyncio
import io
import json
import os
import subprocess
import wave
import requests
import sounddevice as sd
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from shazamio import Shazam
from mpd import MPDClient

app = FastAPI()

templates = Jinja2Templates(directory="templates")
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

shazam = Shazam()
CONFIG_FILE = "config.json"

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
    if not os.path.exists(CONFIG_FILE):
        return {
            "theme": "dark",
            "display_mode": "grid",
            "screen_rotation": "normal",
            "mic_enabled": True,
            "mic_interval_seconds": 8,
            "moodes": []
        }
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(config_data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config_data, f, indent=2)

latest_ambient_state = {
    "name": "Ambient Listener (CD)",
    "title": "Listening...",
    "artist": "Waiting for audio",
    "coverart": "/static/idle.png"
}

def fetch_online_coverart(artist, title):
    if not title or title in ["Idle", "Unknown Track", "Offline"]:
        return "/static/default_cover.png"
    try:
        query = f"{artist} {title}"
        res = requests.get(f"https://itunes.apple.com/search?term={query}&limit=1", timeout=2).json()
        if res.get("resultCount", 0) > 0:
            return res["results"][0]["artworkUrl100"].replace("100x100bb", "600x600bb")
    except Exception:
        pass
    return "/static/default_cover.png"

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
        art = fetch_online_coverart(artist, title) if is_playing else "/static/idle.png"

        return {
            "id": instance["id"],
            "name": instance["name"],
            "playing": is_playing,
            "title": title,
            "artist": artist,
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
    recording = sd.rec(int(duration * 44100), samplerate=44100, channels=1, dtype='int16')
    sd.wait()
    wav_io = io.BytesIO()
    with wave.open(wav_io, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(44100)
        wf.writeframes(recording.tobytes())
    return wav_io.getvalue()

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
                    latest_ambient_state = {
                        "name": "Ambient Listener (CD)",
                        "title": track.get("title", "Unknown Title"),
                        "artist": track.get("subtitle", "Unknown Artist"),
                        "coverart": cover
                    }
                else:
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

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(background_mic_listener())
    cfg = load_config()
    set_screen_rotation(cfg.get("screen_rotation", "normal"))

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    cfg = load_config()
    return templates.TemplateResponse("index.html", {"request": request, "config": cfg})

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    cfg = load_config()
    system_status = {
        "kiosk_active": is_service_active("kiosk"),
        "ssh_active": is_service_active("ssh")
    }
    return templates.TemplateResponse("settings.html", {
        "request": request, 
        "config": cfg, 
        "system": system_status
    })

@app.post("/settings/save")
async def save_settings(request: Request):
    form = await request.form()
    cfg = load_config()
    
    cfg["theme"] = form.get("theme", "dark")
    cfg["display_mode"] = form.get("display_mode", "grid")
    cfg["screen_rotation"] = form.get("screen_rotation", "normal")
    cfg["mic_enabled"] = form.get("mic_enabled") == "on"
    cfg["mic_interval_seconds"] = int(form.get("mic_interval_seconds", 8))

    names = form.getlist("moode_name")
    hosts = form.getlist("moode_host")
    ports = form.getlist("moode_port")

    updated_moodes = []
    for i in range(len(hosts)):
        if hosts[i].strip():
            updated_moodes.append({
                "id": f"moode_{i+1}",
                "name": names[i].strip() or f"moOde {i+1}",
                "host": hosts[i].strip(),
                "port": int(ports[i]) if ports[i].isdigit() else 6600
            })

    cfg["moodes"] = updated_moodes
    save_config(cfg)

    enable_kiosk = form.get("local_kiosk") == "on"
    enable_ssh = form.get("ssh_enabled") == "on"

    set_service_state("kiosk", enable_kiosk)
    set_service_state("ssh", enable_ssh)
    set_screen_rotation(cfg["screen_rotation"])

    return RedirectResponse(url="/settings", status_code=303)

@app.post("/system/reboot")
async def reboot_system():
    try:
        subprocess.run(["sudo", "reboot"], check=True)
        return {"status": "Rebooting..."}
    except Exception as e:
        return {"error": str(e)}

@app.post("/system/shutdown")
async def shutdown_system():
    try:
        subprocess.run(["sudo", "shutdown", "-h", "now"], check=True)
        return {"status": "Shutting down..."}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/dashboard")
async def get_dashboard():
    cfg = load_config()
    moode_data = []
    for instance in cfg.get("moodes", []):
        moode_data.append(check_moode(instance))

    return {
        "config": cfg,
        "moodes": moode_data,
        "ambient": latest_ambient_state
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)