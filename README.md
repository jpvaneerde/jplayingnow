# jplayingnow

JPlaying Now: a Raspberry Pi "now playing" display for the JPlay players
([player-jplay](https://github.com/jpvaneerde/player-jplay)), plus an ambient listener. Each player gets a card with
the song, the cover and the source (Radio, Plex, CD, Spotify or Bluetooth) in its own colour. When a radio station
doesn't say which song it plays, JPlaying Now listens to the stream itself and recognises the song with Shazam. A USB
microphone and Shazam recognise music from CDs and records. The web app (`nowplaying_app/server.py`, FastAPI) runs on
port 5000; the Pi's own screen shows it in a Chromium kiosk. Tap a player's card to open that player's own page.

Settings (`/settings`) and play history (`/history`) are stored on the device in `nowplaying_app/config.json` and
`nowplaying_app/data/history.db`. Git never overwrites them.

## The units

| Unit | Address | Its room's JPlay player | JPlay Spins listener |
| --- | --- | --- | --- |
| Study | 192.168.1.224 | Study, 192.168.1.223 | room **Study**, priority **1**, formats **CD** |
| Lounge | 192.168.1.225 | Living Room, 192.168.1.222 | room **Lounge**, priority **2**, formats **Vinyl + CD** |

Every unit shows both players; the "room's player" is only used by JPlay Spins. (The table assumes the JPlay players
run on the Pis that used to run moOde; the player's web page is on port 8080.)

## Setup of a new unit

You need a Raspberry Pi 4 or 5, a screen (HDMI or the official touch display), a USB microphone and a power supply.
The commands below use user `jp` and the lounge address `192.168.1.225`; use `192.168.1.224` for the study.

### 1. Flash the SD card

In **Raspberry Pi Imager**:
1. Choose **Raspberry Pi OS (64-bit)**, the version *with desktop*. The kiosk needs its Wayland desktop.
2. Under **Edit settings**:
   - hostname, e.g. `jplayingnow-lounge`
   - user **`jp`** with a password
   - Wi-Fi (or use Ethernet)
   - time zone Europe/Stockholm
   - on the Services tab, **Enable SSH**

Plug in the screen and the USB microphone, then boot. In the router, give the Pi a fixed address (DHCP reservation).

### 2. Log in and update

From PowerShell on the PC:

```sh
ssh jp@192.168.1.225
sudo apt update && sudo apt full-upgrade -y && sudo reboot
```

Log in again after the reboot. Check the user: `whoami` should say `jp` and `id -u` should say `1000`. The service
files assume both. If yours differ, change `User=`, the paths and `/run/user/1000` in the two files under `deploy/`
after copying them.

### 3. Install

```sh
sudo apt install -y git python3-venv python3-dev libportaudio2 ffmpeg wlr-randr wlopm curl
git clone https://github.com/jpvaneerde/jplayingnow.git ~/jplayingnow
cd ~/jplayingnow
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Check that everything loads. It must print `all OK`:

```sh
.venv/bin/python -c "import fastapi, uvicorn, jinja2, multipart, shazamio, requests, sounddevice, numpy; print('all OK')"
```

- `No module named 'audioop'` / `'pyaudioop'`: Python 3.13+ without `audioop-lts`. Run
  `.venv/bin/pip install audioop-lts`.
- `PortAudio library not found`: run `sudo apt install -y libportaudio2`.

### 4. Test run

```sh
cd ~/jplayingnow/nowplaying_app
../.venv/bin/python server.py
```

Wait for `Uvicorn running on http://0.0.0.0:5000` and open **http://192.168.1.225:5000** on the PC. Stop it with
**Ctrl+C**; from now on the service runs it.

### 5. Run it as a service

```sh
sudo cp ~/jplayingnow/deploy/jplayingnow.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jplayingnow
systemctl status jplayingnow --no-pager
```

It must say **`active (running)`**. Follow the log live with `journalctl -u jplayingnow -f` (stop with Ctrl+C).

### 6. Let the settings page control the screen, SSH and power

```sh
sudo visudo -f /etc/sudoers.d/nowplaying
```

Paste this as **one line**, then save with **Ctrl+O**, **Enter**, **Ctrl+X**:

```
jp ALL=(root) NOPASSWD: /usr/bin/systemctl enable --now kiosk, /usr/bin/systemctl disable --now kiosk, /usr/bin/systemctl enable --now ssh, /usr/bin/systemctl disable --now ssh, /usr/sbin/reboot, /usr/sbin/shutdown
```

### 7. Kiosk service (the Pi's own screen)

```sh
which chromium chromium-browser
sudo cp ~/jplayingnow/deploy/kiosk.service /etc/systemd/system/kiosk.service
sudo systemctl daemon-reload
```

The unit uses `/usr/bin/chromium` (Raspberry Pi OS Bookworm and newer). If `which` only finds
`/usr/bin/chromium-browser` (older OS), run:
`sudo sed -i 's#/usr/bin/chromium #/usr/bin/chromium-browser #' /etc/systemd/system/kiosk.service && sudo systemctl daemon-reload`.

Don't enable it yourself. The settings page does that in the next step.

### 8. Configure the unit

Open **http://192.168.1.225:5000/settings** on the PC:

1. Tick **Display locally on HDMI / Ribbon Display (Kiosk Mode)**. The Pi's screen switches to JPlaying Now
   within a few seconds. If a red box appears, it says why (usually the Chromium path from step 7).
2. **Ambient Listening:** select the USB microphone and keep the listener enabled. Put the microphone where it hears
   the speakers clearly, but not right against a speaker or the turntable.
3. **JPlay Players:** add `Living Room`, `192.168.1.222`, `8080` and `Study`, `192.168.1.223`, `8080`. Keep
   **Recognise songs on radio stations that don't name them** ticked (it needs `ffmpeg`, installed in step 3).
4. Set rotation, theme and screen-off time to taste, then press **Save & Apply Settings**.

### 9. Connect to JPlay Spins

1. On **records.jplay.cloud → Settings → Listeners**, add a listener with the room, priority and formats from the
   table above. Copy the token. It's shown only once.
2. In the unit's **Settings → JPlay Spins**:
   - tick **Send plays to JPlay Spins**
   - address `https://records.jplay.cloud`
   - paste the token
   - set **JPlay player in this room** to the room's player from the table
3. Save.

### 10. Reboot test

```sh
sudo reboot
```

After about a minute the screen shows JPlaying Now by itself, and http://192.168.1.225:5000 works from the PC.
Play the radio on the room's JPlay player: within a minute, records.jplay.cloud/spins shows it on that room's card.

## Updating a unit

The first update after moOde: the moOde players in the settings become JPlay players on the same addresses, port
8080. Check them under **Settings → JPlay Players**.

```sh
ssh jp@192.168.1.225
cd ~/jplayingnow && git pull && .venv/bin/pip install -r requirements.txt && sudo systemctl restart jplayingnow
```

## Troubleshooting

| Problem | What to do |
| --- | --- |
| `server.py` exits straight away | It prints the missing package or library; see step 3. |
| Service not running | `systemctl status jplayingnow --no-pager` and `journalctl -u jplayingnow -n 50` |
| Screen stays black / kiosk error on the settings page | `systemctl status kiosk --no-pager`. Check the Chromium path (step 7) and that `id -u` is 1000. |
| No songs recognised | Settings → Ambient Listening shows which microphone is used; pick the USB one and check the volume near the speakers. |
| A player shows "Offline" | Check its address and port (8080) under Settings → JPlay Players, and that `http://<player>:8080` opens. |
| Radio songs aren't recognised | `http://<unit>:5000/api/players-status` shows each player's status and the radio listener's last result. It needs `ffmpeg` (step 3). |
| Nothing arrives in JPlay Spins | The JPlay Spins card shows events waiting and the last error; so does `http://<unit>:5000/api/spins-status`. A "token" error means the listener was removed or the token was mistyped. |

## JPlay Spins

Each unit can report to **JPlay Spins** at records.jplay.cloud/spins. That's one history for all rooms: the
records you put on, radio and other streams, and what else was heard. Setting it up is step 9 above.

The unit sends three kinds of events:
- **`mic`:** every song the microphone recognises and has heard for 20 seconds (title, artist, album, cover,
  loudness).
- **`mic_end`:** when that song stops being heard.
- **`room`:** this room's JPlay player: play/pause/stop, the source (radio, plex, cd, spotify, bluetooth), the
  station and the song if known, including radio songs recognised from the stream. It's sent on every change and
  every 5 minutes as a heartbeat.

Events wait in a local outbox (`spin_outbox` in `history.db`) until the server accepts them, so nothing is lost
while the network is down. Unsent events are dropped after 7 days. The server decides what each event means:
- **heard in both rooms:** it counts in the room with the higher priority (the lounge)
- **this room's player was playing:** a CD in the player is your CD; Radio, Plex, Spotify and Bluetooth are
  streamed, labelled with that source
- **otherwise:** it's matched against your records and CDs
