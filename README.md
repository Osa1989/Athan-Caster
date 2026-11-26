# Athan Caster

A self-contained, offline Islamic Athan (Adhan) caster for Google Home and Nest speakers, packaged as a lightweight Docker Compose stack with a built-in web dashboard.

No external prayer-time APIs, cloud services, or heavy frameworks are required—prayer times are calculated locally using astronomical algorithms, and your local MP3 files are streamed directly over your LAN to your Chromecast-enabled speaker.

---

## Features

- **Offline Prayer-Time Calculation:** Calculates Fajr, Dhuhr, Asr, Maghrib, and Isha locally using [`adhanpy`](https://pypi.org/project/adhanpy/) and Python's `zoneinfo` (with automatic DST handling) and refreshes the schedule daily at `00:05`.
- **11 Supported Calculation Methods:** Switch between all standard conventions (`MUSLIM_WORLD_LEAGUE`, `EGYPTIAN`, `KARACHI`, `UMM_AL_QURA`, `DUBAI`, `MOON_SIGHTING_COMMITTEE`, `NORTH_AMERICA`, `KUWAIT`, `QATAR`, `SINGAPORE`, `UOIF`).
- **Google Home / Nest Casting:** Discovers your target speaker over mDNS via [`pychromecast`](https://pypi.org/project/pychromecast/) and plays `fajr.mp3` for Fajr and `athan.mp3` for Dhuhr, Asr, Maghrib, and Isha.
- **Automatic Volume Save & Restore:** Saves the speaker's current volume before casting, applies your per-prayer volume, waits for playback to finish (with a 360-second safety timeout), and restores the previous volume.
- **Silent Skip When Muted:** If a prayer is disabled or its volume is set to `0%`, casting is skipped completely so the speaker never wakes up or plays its connection chime.
- **Web Control Panel:** Mobile-friendly light-mode dashboard built on Python's standard-library `ThreadingHTTPServer` to:
  - View today's calculated prayer times
  - Adjust per-prayer volumes (`0–100%`) and mute/unmute toggles
  - Trigger immediate test casts for any prayer
  - Edit **Speaker Name**, **Latitude**, **Longitude**, and **Calculation Method** live (immediately recalculates and reschedules prayer times)
- **Persistent Settings:** All volume, mute, speaker, location, and calculation method changes are persisted in `volumes.json`.
- **Raspberry Pi Ready:** Uses `python:3.12-slim` (Debian, multi-arch `amd64` / `arm64`) with prebuilt wheels for `pychromecast`, `zeroconf`, and `protobuf`, running as non-root user `athan` (UID `1000`).

---

## Project Structure

```text
athan-caster/
├── Dockerfile           # python:3.12-slim + tzdata, non-root user (UID 1000)
├── docker-compose.yml   # Host networking + volume mounts + default env vars
├── requirements.txt     # pychromecast, adhanpy, schedule
├── athan_caster.py      # Scheduler, Chromecast caster, HTTP audio server & Web UI
├── volumes.json         # Persisted per-prayer volumes, mute flags & live config
├── README.md
└── audio/
    ├── .gitkeep
    ├── athan.mp3        # (You add this) Played for Dhuhr, Asr, Maghrib, Isha
    └── fajr.mp3         # (You add this) Played for Fajr
```

---

## Prerequisites

- **Docker** and **Docker Compose** (`docker compose` plugin)
- A **Google Home / Nest** (or Chromecast-compatible) speaker on the **same local network / VLAN** as the host
- Two MP3 files: `athan.mp3` and `fajr.mp3`

---

## Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/Osa1989/Athan-Caster.git
cd athan-caster
```

### 2. Add Your Athan MP3 Files

Drop your MP3 files into the `audio/` directory:

- `audio/athan.mp3` — played for **Dhuhr**, **Asr**, **Maghrib**, and **Isha**
- `audio/fajr.mp3` — played for **Fajr**

> **Tip:** If you do not have a separate Fajr recording, copy `athan.mp3` to `fajr.mp3`.

### 3. Ensure `volumes.json` Permissions

The container runs as non-root user `athan` (`UID 1000`, `GID 1000`). Ensure `volumes.json` is writable by UID `1000` so settings saved in the web dashboard persist across restarts:

```bash
sudo chown 1000:1000 volumes.json
sudo chmod 664 volumes.json
```

### 4. Configure Environment Variables (Optional)

Edit `docker-compose.yml` to set your initial defaults (Speaker Name, Latitude, Longitude, and Calculation Method can also be changed anytime from the web UI):

```yaml
environment:
  SPEAKER_NAME: "Living Room Speaker"
  LATITUDE: "50.1109"
  LONGITUDE: "8.6821"
  TZ: "Europe/Berlin"
  CALCULATION_METHOD: "MUSLIM_WORLD_LEAGUE"
  HTTP_PORT: "8089"
  AUDIO_DIR: "/app/audio"
  VOLUMES_FILE: "/app/volumes.json"
```

### 5. Build and Start

```bash
docker compose up -d --build
```

Verify that all 5 prayer times are calculated and scheduled with no errors:

```bash
docker compose logs -f
```

Expected log output:

```text
2026-11-25 21:35:54 [INFO] HTTP server listening on http://0.0.0.0:8089 (LAN IP: 192.168.1.50)
2026-11-25 21:35:54 [INFO] Calculated prayer times for 2026-11-25 (MUSLIM_WORLD_LEAGUE, speaker='Living Room Speaker', lat=50.1109, lon=8.6821, tz=Europe/Berlin):
2026-11-25 21:35:54 [INFO]   Scheduled Fajr    at 05:58
2026-11-25 21:35:54 [INFO]   Scheduled Dhuhr   at 12:13
2026-11-25 21:35:54 [INFO]   Scheduled Asr     at 14:10
2026-11-25 21:35:54 [INFO]   Scheduled Maghrib at 16:30
2026-11-25 21:35:54 [INFO]   Scheduled Isha    at 18:20
2026-11-25 21:35:54 [INFO] Daily schedule refresh registered at 00:05 (Europe/Berlin).
```

---

## Usage

### Web Dashboard

Open the control panel in your browser from any device on your LAN:

```text
http://<host-ip>:8089
```

From the dashboard you can:

- **Monitor today's prayer times** at a glance
- **Adjust per-prayer volume** (`0–100%`) using the sliders (auto-saves)
- **Mute or unmute** any individual prayer (muted prayers will not wake/chime the speaker)
- **Trigger a test cast** immediately using the **Test** button next to any prayer
- **Edit Configuration** (Speaker Name, Latitude, Longitude, and Calculation Method) and click **Save Configuration** to immediately recalculate and reschedule prayer times

### CLI Test Cast

You can also trigger a forced test cast directly from the command line (`Fajr`, `Dhuhr`, `Asr`, `Maghrib`, or `Isha`):

```bash
docker compose run --rm athan-caster --test Maghrib
```

*(A forced test cast bypasses mute/zero-volume skip checks and exits once playback finishes.)*

---

## Configuration Reference

### Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `SPEAKER_NAME` | `"Living Room Speaker"` | Exact friendly name of your Google Home / Nest speaker (as shown in the Google Home app). |
| `LATITUDE` | `50.1109` | Geographic latitude (`-90` to `90`). |
| `LONGITUDE` | `8.6821` | Geographic longitude (`-180` to `180`). |
| `TZ` | `"Europe/Berlin"` | IANA timezone name (e.g., `America/New_York`, `Europe/London`, `Asia/Riyadh`). |
| `CALCULATION_METHOD` | `"MUSLIM_WORLD_LEAGUE"` | Default prayer calculation convention (see table below). |
| `HTTP_PORT` | `8089` | Port for both the web UI and local MP3 streaming server. |
| `AUDIO_DIR` | `"/app/audio"` | Directory inside the container containing `athan.mp3` and `fajr.mp3`. |
| `VOLUMES_FILE` | `"/app/volumes.json"` | Path inside the container for persisting volumes and web UI config overrides. |

### Supported Calculation Methods

| Value | Organization / Standard |
| :--- | :--- |
| `MUSLIM_WORLD_LEAGUE` | Muslim World League |
| `EGYPTIAN` | Egyptian General Authority of Survey |
| `KARACHI` | University of Islamic Sciences, Karachi |
| `UMM_AL_QURA` | Umm Al-Qura University, Makkah |
| `DUBAI` | Dubai |
| `MOON_SIGHTING_COMMITTEE` | Moonsighting Committee |
| `NORTH_AMERICA` | ISNA (North America) |
| `KUWAIT` | Kuwait |
| `QATAR` | Qatar |
| `SINGAPORE` | Singapore |
| `UOIF` | UOIF (France, 12°) |

### `volumes.json` Format

Default contents:

```json
{
  "Fajr": { "volume": 15, "enabled": true },
  "Dhuhr": { "volume": 60, "enabled": true },
  "Asr": { "volume": 60, "enabled": true },
  "Maghrib": { "volume": 60, "enabled": true },
  "Isha": { "volume": 40, "enabled": true }
}
```

When you modify **Speaker Name**, **Latitude**, **Longitude**, or **Calculation Method** from the web dashboard, those overrides are automatically stored in the same file under a `"config"` key and take precedence over the environment variable defaults on subsequent restarts.

---

## HTTP API Reference

The built-in HTTP server exposes both the audio files and a small JSON API:

| Method & Path | Description |
| :--- | :--- |
| `GET /` | Light-mode web control panel |
| `GET /api/state` | Returns active `speaker`, `latitude`, `longitude`, `method`, supported `methods`, `tz`, today's `times`, and `settings` |
| `POST /api/settings` | Updates per-prayer `volume` (clamped `0–100`) and `enabled` flags in `volumes.json`; returns `{"ok": true}` |
| `POST /api/config` | Updates `speaker`, `latitude`, `longitude`, and/or `method` in `volumes.json`, immediately reschedules prayers, and returns `{"ok": true, "state": ...}` |
| `POST /api/test` | Accepts `{"prayer": "Maghrib"}` and triggers a forced test cast in a background thread |
| `GET /athan.mp3` | Streams `audio/athan.mp3` (`audio/mpeg`, supports HTTP `Range` & `HEAD`) |
| `GET /fajr.mp3` | Streams `audio/fajr.mp3` (`audio/mpeg`, supports HTTP `Range` & `HEAD`) |

---

## Deploying to a Raspberry Pi 4

64-bit Raspberry Pi OS (`arm64` / `aarch64`) is recommended so `pip` installs prebuilt wheels without compiling from source.

1. **Install Docker & Docker Compose on the Pi:**
   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker "$USER"
   sudo systemctl enable --now docker
   ```
   Log out and back in so the `docker` group takes effect.

2. **Copy the project and MP3 files to the Pi:**
   ```bash
   scp -r athan-caster pi@<pi-ip>:~/athan-caster
   scp athan.mp3 fajr.mp3 pi@<pi-ip>:~/athan-caster/audio/
   ```

3. **Set permissions and start the stack on the Pi:**
   ```bash
   cd ~/athan-caster
   sudo chown 1000:1000 volumes.json
   sudo chmod 664 volumes.json
   docker compose up -d --build
   ```

4. **Firewall (if using `ufw`):**
   ```bash
   sudo ufw allow 8089/tcp   # Web dashboard & MP3 stream to speaker
   sudo ufw allow 5353/udp   # mDNS Chromecast discovery
   ```

Because `docker-compose.yml` uses `restart: unless-stopped`, Athan Caster starts automatically whenever the Pi reboots.

---

## Networking Notes

`docker-compose.yml` uses `network_mode: host` (and `build.network: host`) for two reasons:

1. **mDNS Discovery (UDP 5353):** Chromecast discovery relies on multicast DNS broadcasts on your LAN, which do not cross Docker's default bridge network.
2. **LAN IP Advertisement:** The speaker fetches `http://<host-lan-ip>:8089/athan.mp3` directly from the container, so the container must see and advertise the host's real LAN IP.

---

## Troubleshooting

### `"Permission denied writing /app/volumes.json"` / `"Failed to save settings"`
The container runs as UID `1000`. Fix file ownership on the host:
```bash
sudo chown 1000:1000 volumes.json
sudo chmod 664 volumes.json
```
If `volumes.json` was accidentally created as a directory by Docker (happens if the file was missing before the first `docker compose up`), remove it and recreate it as a regular file:
```bash
docker compose down
sudo rm -rf volumes.json
git checkout -- volumes.json   # or recreate the JSON file
sudo chown 1000:1000 volumes.json
docker compose up -d
```

### Speaker Not Found via mDNS
- Confirm `SPEAKER_NAME` matches the exact name shown in the Google Home app (case-sensitive).
- Confirm the host and speaker are on the same subnet/VLAN (mDNS does not cross subnets without an mDNS repeater).
- Confirm UDP port `5353` and TCP port `8089` are not blocked by a host firewall (`ufw` / `iptables`).

### DNS Errors During `docker compose build` on Raspberry Pi
`docker-compose.yml` already sets `build.network: host` to avoid bridge-network DNS issues during `pip install`. If builds still fail with `Temporary failure in name resolution`, configure explicit DNS servers in `/etc/docker/daemon.json`:
```bash
sudo mkdir -p /etc/docker
echo '{"dns": ["1.1.1.1", "8.8.8.8"]}' | sudo tee /etc/docker/daemon.json
sudo systemctl restart docker
```

### Speaker Connects and Chimes, but No Audio Plays
- Verify both `audio/athan.mp3` and `audio/fajr.mp3` exist and are valid MP3 files:
  ```bash
  curl -I http://<host-ip>:8089/athan.mp3
  curl -I http://<host-ip>:8089/fajr.mp3
  ```
- Ensure TCP port `8089` is reachable from other devices on your LAN (not just `127.0.0.1`).

---

## License

MIT
