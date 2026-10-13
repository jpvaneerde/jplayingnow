# jplayingnow

JPlaying Now: a Raspberry Pi "now playing" display for the moOde players, plus an ambient listener. A microphone
and Shazam recognise music from CDs and records. Runs `nowplaying_app/server.py` (FastAPI) on port 5000; the
local screen is a Chromium kiosk (`deploy/kiosk.service`).

Settings (`/settings`) and play history (`/history`) are stored on the device in `nowplaying_app/config.json` and
`nowplaying_app/data/history.db`. Git never overwrites them.

## Update a unit

```sh
cd ~/jplayingnow && git pull
sudo systemctl restart <the service that runs server.py>
```

## JPlay Spins

Each unit can report to **JPlay Spins** at records.jplay.cloud/spins. That's one history for all rooms: the
records you put on, radio and other streams, and what else was heard.

1. In JPlay Records go to **Settings → Listeners → Add listener**. Give it a name and a room (Lounge / Study),
   a priority (lounge higher: when both rooms hear the same song it counts in the lounge) and the formats that
   can play there (lounge: vinyl and CD; study: CD). Copy the token. It's shown once.
2. In this unit's **Settings → JPlay Spins**:
   - tick "Send plays to JPlay Spins"
   - leave the address on `https://records.jplay.cloud`
   - paste the token
   - pick **the moOde player in this room** (lounge: Living Room `192.168.1.222`; study: `192.168.1.223`)
3. Save. The status line shows how many events are waiting and when they were last sent. `/api/spins-status`
   shows the same.

The unit sends three kinds of events:
- **`mic`:** every song the microphone recognises and has heard for 20 seconds (title, artist, album, cover,
  loudness).
- **`mic_end`:** when that song stops being heard.
- **`room`:** this room's moOde state: play/pause/stop, radio station or file, and whatever metadata the stream
  has. It's sent on every change and every 5 minutes as a heartbeat.

Events wait in a local outbox (`spin_outbox` in `history.db`) until the server accepts them, so nothing is lost
while the network is down. Unsent events are dropped after 7 days. The server decides what each event means:
which room, record or stream.
