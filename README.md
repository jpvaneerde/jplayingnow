# jplayingnow

JPlaying Now: a Raspberry Pi "now playing" display for the JPlay players
([player-jplay](https://github.com/jpvaneerde/player-jplay)), plus an ambient listener. Each unit shows one player
of your choice, full screen, laid out for its source (Radio, Plex, CD, Spotify or Bluetooth), each in its own colour:
the song and its cover, with the station's logo small in the corner for radio, or just the station's logo when no
song is known. Radio stations often name no song, or mix the show into it ("Magasinet i P3 med …, spelar nu Dua
Lipa"), so JPlaying Now listens to the stream itself and recognises the song with Shazam; that song wins over the
station's text. While the player is idle, the screen shows what the USB microphone hears (Shazam
recognises CDs and records). The web app (`nowplaying_app/server.py`, FastAPI) runs on port 5000; the Pi's own screen
shows it in a Chromium kiosk. The player's name next to **History** opens a menu: open the player's own page (also
when it's idle), or switch the display to another player. Tapping the screen while it plays opens the player too.

**Lyrics** (from LRCLIB, lrclib.net) appear under the title when a song has them. With timed lyrics, the line being
sung is highlighted and glides up with the music: exactly in time for Plex, CD and Spotify, and for radio and records
from where Shazam recognised the song (Settings → Lyrics → timing nudges them if they run early or late). Without
times, the text scrolls slowly through the song. With JPlay Spins set up, the lyrics come from JPlay Records, which
keeps them for the whole house (and a song's timing correction made there reaches every unit). Each unit also keeps
its own copy in `nowplaying_app/data/lyrics.db`, used when JPlay Records can't be reached, and otherwise asks LRCLIB
itself.

The screen switches off after the minutes set in Settings without music, and back on for a new song or a touch (the
touch that wakes it does nothing else). Home Assistant can show and control each unit (screen on/off, which player
it shows, what it hears): see the JPlay integration in
[player-jplay](https://github.com/jpvaneerde/player-jplay/tree/main/custom_components/jplay).

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
3. **JPlay Players:** add `Living Room`, `192.168.1.222`, `8080` and `Study`, `192.168.1.223`, `8080`, and pick
   the room's player with the round button: that's the one this display shows. Keep
   **Recognise radio songs from the stream** ticked (it needs `ffmpeg`, installed in step 3).
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

## Updates

**Settings → Version & updates** shows the version (from the git tags: `1.6.0`, or `1.6.0+3` for 3 commits after
it), whether a newer release is on GitHub with its notes, and **Update to …**. A green dot on the display's Settings
button means an update is waiting. Options: updates from **Releases** or the **newest commit** (for development),
and **install automatically at night** (03:00–05:00, only when nothing was heard or played for 10 minutes). Home
Assistant shows the same under **Settings → Updates** (the JPlay integration), with an Install button.

**One-time setup on each unit** (after the `git pull` that brings the updater):

```sh
ssh jp@192.168.1.225
cd ~/jplayingnow && git pull && sudo bash deploy/install-updater.sh
```

That installs the separate updater ([deploy/update.sh](deploy/update.sh)) as `jplayingnow-update.service`, a check
every 6 hours (`jplayingnow-update-check.timer`) and a sudoers rule that lets JPlaying Now start exactly these two
services. JPlaying Now never updates itself: it asks the updater, which, as root:

1. fetches from GitHub (git as `jp`, with the access `git pull` already has),
2. moves `main` forward to the release (fast-forward only: hand-made changes are never overwritten; it refuses
   instead),
3. installs the Python packages, only when `requirements.txt` changed,
4. restarts JPlaying Now and waits until it answers,
5. and when it doesn't, goes back to the previous version (packages too) and restarts that.

Settings shows each step and, if it failed, why. Settings (`config.json`) and history (`data/`) are never touched.
Changes to the files in `deploy/` (systemd units) are noted but not installed; re-run the setup steps for those.

**Releasing a version** (on the PC, in the repo, after committing): `.\tools\release.ps1`. It asks for the version
(suggesting the next one) and the release notes (the commit messages since the last release if you type none),
tags the commit `vX.Y.Z` and pushes. The units see it within 6 hours, or at once with **Check now**. If Windows
refuses to run scripts: `powershell -ExecutionPolicy Bypass -File tools\release.ps1`.

**By hand**, as before: `cd ~/jplayingnow && git pull && .venv/bin/pip install -r requirements.txt && sudo systemctl
restart jplayingnow`.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| `server.py` exits straight away | It prints the missing package or library; see step 3. |
| Service not running | `systemctl status jplayingnow --no-pager` and `journalctl -u jplayingnow -n 50` |
| Touching the screen doesn't wake it | Look at `touch` in http://&lt;pi&gt;:5000/api/screen-status: `devices` should be 1 or more. If `no_permission` isn't 0, run `sudo usermod -aG input jp` and reboot. |
| Screen stays black / kiosk error on the settings page | `systemctl status kiosk --no-pager`. Check the Chromium path (step 7) and that `id -u` is 1000. |
| No songs recognised | Settings → Ambient Listening shows which microphone is used; pick the USB one and check the volume near the speakers. |
| A player shows "Offline" | Check its address and port (8080) under Settings → JPlay Players, and that `http://<player>:8080` opens. |
| Radio songs aren't recognised | `http://<unit>:5000/api/players-status` shows each player's status and the radio listener's last result. It needs `ffmpeg` (step 3). |
| An update failed or hangs | Settings → Version & updates shows the steps and the error. More: `journalctl -u jplayingnow-update -n 50` and `journalctl -u jplayingnow-update-check -n 20`. "Updates not set up": run the one-time setup under Updates. |
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
