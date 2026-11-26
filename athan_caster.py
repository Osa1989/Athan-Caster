#!/usr/bin/env python3
"""Offline Islamic Athan caster for Google Home/Nest speakers."""

from __future__ import annotations

import argparse
import copy
import datetime
import json
import logging
import math
import os
import signal
import socket
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pychromecast
import schedule
from adhanpy.PrayerTimes import PrayerTimes
try:
    from adhanpy.calculation.CalculationMethod import CalculationMethod
except ImportError:
    from adhanpy.calculation import CalculationMethod

# ---------------------------------------------------------------------------
# Configuration Defaults (from environment variables)
# ---------------------------------------------------------------------------
DEFAULT_SPEAKER_NAME = os.environ.get("SPEAKER_NAME", "Living Room Speaker")
DEFAULT_LATITUDE = float(os.environ.get("LATITUDE", "50.1109"))
DEFAULT_LONGITUDE = float(os.environ.get("LONGITUDE", "8.6821"))
TZ_NAME = os.environ.get("TZ", "Europe/Berlin")
DEFAULT_CALCULATION_METHOD_NAME = (
    os.environ.get("CALCULATION_METHOD", "MUSLIM_WORLD_LEAGUE").strip().upper()
)
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8089"))
AUDIO_DIR = Path(os.environ.get("AUDIO_DIR", "/app/audio"))
VOLUMES_FILE = Path(os.environ.get("VOLUMES_FILE", "/app/volumes.json"))

PRAYERS = ("Fajr", "Dhuhr", "Asr", "Maghrib", "Isha")

CALCULATION_METHOD_LABELS: dict[str, str] = {
    "MUSLIM_WORLD_LEAGUE": "Muslim World League",
    "EGYPTIAN": "Egyptian General Authority of Survey",
    "KARACHI": "University of Islamic Sciences, Karachi",
    "UMM_AL_QURA": "Umm Al-Qura University, Makkah",
    "DUBAI": "Dubai",
    "MOON_SIGHTING_COMMITTEE": "Moonsighting Committee",
    "NORTH_AMERICA": "ISNA (North America)",
    "KUWAIT": "Kuwait",
    "QATAR": "Qatar",
    "SINGAPORE": "Singapore",
    "UOIF": "UOIF (France, 12°)",
}

DEFAULT_SETTINGS: dict[str, dict[str, Any]] = {
    "Fajr": {"volume": 15, "enabled": True},
    "Dhuhr": {"volume": 60, "enabled": True},
    "Asr": {"volume": 60, "enabled": True},
    "Maghrib": {"volume": 60, "enabled": True},
    "Isha": {"volume": 40, "enabled": True},
}

PLAYBACK_TIMEOUT_SECONDS = 360

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("athan_caster")

SETTINGS_LOCK = threading.Lock()
CAST_LOCK = threading.Lock()
STOP_EVENT = threading.Event()


def configure_timezone() -> ZoneInfo:
    """Configure process timezone and return the corresponding ZoneInfo."""
    os.environ["TZ"] = TZ_NAME
    if hasattr(time, "tzset"):
        time.tzset()
    return ZoneInfo(TZ_NAME)


LOCAL_TZ = configure_timezone()


def get_calculation_method(method_name: str | None = None) -> CalculationMethod:
    """Resolve a calculation method name into an adhanpy CalculationMethod."""
    name = (method_name or DEFAULT_CALCULATION_METHOD_NAME).strip().upper()
    try:
        return CalculationMethod[name]
    except KeyError:
        supported = ", ".join(m.name for m in CalculationMethod)
        logger.warning(
            "Unknown CALCULATION_METHOD '%s'; falling back to MUSLIM_WORLD_LEAGUE. Supported: %s",
            name,
            supported,
        )
        return CalculationMethod.MUSLIM_WORLD_LEAGUE


def get_local_ip() -> str:
    """Determine the host's primary LAN IP address without making external requests."""
    for target in (("8.8.8.8", 80), ("10.255.255.255", 1)):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(target)
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
        finally:
            sock.close()
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# Settings & Config Persistence (Thread-Safe)
# ---------------------------------------------------------------------------
def _default_config() -> dict[str, Any]:
    return {
        "speaker": DEFAULT_SPEAKER_NAME,
        "latitude": DEFAULT_LATITUDE,
        "longitude": DEFAULT_LONGITUDE,
        "method": get_calculation_method(DEFAULT_CALCULATION_METHOD_NAME).name,
    }


def _clamp_volume(value: Any, default: int) -> int:
    try:
        vol = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(0, min(100, vol))


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
    return default


def _parse_coordinate(value: Any, min_val: float, max_val: float, name: str) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a valid number") from exc
    if not math.isfinite(num) or num < min_val or num > max_val:
        raise ValueError(f"{name} must be between {min_val} and {max_val}")
    return round(num, 6)


def _normalize_prayer_settings(
    raw: Any, base: dict[str, dict[str, Any]] | None = None
) -> dict[str, dict[str, Any]]:
    result = copy.deepcopy(base if base is not None else DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        return result
    for prayer in PRAYERS:
        entry = raw.get(prayer)
        if not isinstance(entry, dict):
            continue
        if "volume" in entry:
            result[prayer]["volume"] = _clamp_volume(entry["volume"], result[prayer]["volume"])
        if "enabled" in entry:
            result[prayer]["enabled"] = _coerce_bool(entry["enabled"], result[prayer]["enabled"])
    return result


def _normalize_config(raw: Any, base: dict[str, Any] | None = None) -> dict[str, Any]:
    result = copy.deepcopy(base if base is not None else _default_config())
    if not isinstance(raw, dict):
        return result

    speaker = raw.get("speaker", raw.get("speaker_name"))
    if isinstance(speaker, str) and speaker.strip():
        result["speaker"] = speaker.strip()

    if "latitude" in raw:
        try:
            result["latitude"] = _parse_coordinate(raw["latitude"], -90.0, 90.0, "Latitude")
        except ValueError:
            pass

    if "longitude" in raw:
        try:
            result["longitude"] = _parse_coordinate(raw["longitude"], -180.0, 180.0, "Longitude")
        except ValueError:
            pass

    raw_method = raw.get("method", raw.get("calculation_method"))
    if isinstance(raw_method, str):
        candidate = raw_method.strip().upper()
        if candidate in CalculationMethod.__members__:
            result["method"] = candidate

    return result


def _read_store_unlocked() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Read raw store file; caller must hold SETTINGS_LOCK."""
    if not VOLUMES_FILE.exists():
        return copy.deepcopy(DEFAULT_SETTINGS), _default_config()
    if VOLUMES_FILE.is_dir():
        raise IsADirectoryError(
            f"{VOLUMES_FILE} is a directory (Docker creates a directory if volumes.json was missing on the host)."
        )
    try:
        with VOLUMES_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        logger.error("Failed to read %s (%s); using defaults.", VOLUMES_FILE, exc)
        return copy.deepcopy(DEFAULT_SETTINGS), _default_config()

    settings = _normalize_prayer_settings(data)
    raw_cfg = data.get("config") if isinstance(data, dict) else None
    config = _normalize_config(raw_cfg)
    return settings, config


def _write_store_unlocked(settings: dict[str, dict[str, Any]], config: dict[str, Any]) -> None:
    """Persist settings and optional config overrides in-place; caller must hold SETTINGS_LOCK."""
    if VOLUMES_FILE.is_dir():
        raise IsADirectoryError(
            f"{VOLUMES_FILE} is a directory. Remove it on the host and recreate volumes.json as a file."
        )
    payload: dict[str, Any] = copy.deepcopy(settings)
    if config != _default_config():
        payload["config"] = copy.deepcopy(config)
    VOLUMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Write in-place so Docker single-file bind mounts do not fail with EBUSY.
    with VOLUMES_FILE.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")


def load_store() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Return normalized (prayer_settings, config) from VOLUMES_FILE."""
    with SETTINGS_LOCK:
        try:
            return _read_store_unlocked()
        except Exception as exc:
            logger.error("Failed to load %s (%s); using defaults.", VOLUMES_FILE, exc)
            return copy.deepcopy(DEFAULT_SETTINGS), _default_config()


def load_settings() -> dict[str, dict[str, Any]]:
    """Read and normalize per-prayer settings from VOLUMES_FILE."""
    settings, _ = load_store()
    return settings


def load_config() -> dict[str, Any]:
    """Read active runtime config (speaker, latitude, longitude)."""
    _, config = load_store()
    return config


def save_settings(updates: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate/clamp incoming prayer settings and persist in-place to VOLUMES_FILE."""
    with SETTINGS_LOCK:
        current_settings, current_config = _read_store_unlocked()
        merged_settings = _normalize_prayer_settings(updates, base=current_settings)
        _write_store_unlocked(merged_settings, current_config)
        return merged_settings


def save_config(updates: dict[str, Any]) -> dict[str, Any]:
    """Validate speaker/latitude/longitude updates and persist in-place to VOLUMES_FILE."""
    with SETTINGS_LOCK:
        current_settings, current_config = _read_store_unlocked()
        new_config = copy.deepcopy(current_config)

        if "speaker" in updates or "speaker_name" in updates:
            raw_speaker = updates.get("speaker", updates.get("speaker_name"))
            if not isinstance(raw_speaker, str) or not raw_speaker.strip():
                raise ValueError("Speaker name cannot be empty")
            new_config["speaker"] = raw_speaker.strip()

        if "latitude" in updates:
            new_config["latitude"] = _parse_coordinate(updates["latitude"], -90.0, 90.0, "Latitude")

        if "longitude" in updates:
            new_config["longitude"] = _parse_coordinate(updates["longitude"], -180.0, 180.0, "Longitude")

        if "method" in updates or "calculation_method" in updates:
            raw_method = updates.get("method", updates.get("calculation_method"))
            if not isinstance(raw_method, str) or raw_method.strip().upper() not in CalculationMethod.__members__:
                supported = ", ".join(CALCULATION_METHOD_LABELS.keys())
                raise ValueError(f"Invalid calculation method. Supported: {supported}")
            new_config["method"] = raw_method.strip().upper()

        _write_store_unlocked(current_settings, new_config)
        return new_config


def check_volumes_file_writable() -> None:
    """Log actionable diagnostics on startup if VOLUMES_FILE is not writable."""
    if VOLUMES_FILE.is_dir():
        logger.error(
            "%s is a directory instead of a file! Stop the container, remove the directory, create volumes.json, and run: sudo chown 1000:1000 volumes.json",
            VOLUMES_FILE,
        )
        return
    if VOLUMES_FILE.exists() and not os.access(VOLUMES_FILE, os.W_OK):
        logger.warning(
            "%s is NOT writable by UID %d (saving settings from the web UI will fail). On the host, run: sudo chown 1000:1000 volumes.json",
            VOLUMES_FILE,
            os.getuid(),
        )


# ---------------------------------------------------------------------------
# Prayer Time Calculation & Scheduling
# ---------------------------------------------------------------------------
def calculate_prayer_times(
    target_date: datetime.date | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Calculate the 5 daily prayer times as HH:MM strings in the local timezone."""
    if target_date is None:
        target_date = datetime.datetime.now(LOCAL_TZ).date()
    if config is None:
        config = load_config()

    method = get_calculation_method(config.get("method"))
    pt = PrayerTimes(
        (float(config["latitude"]), float(config["longitude"])),
        target_date,
        method,
        time_zone=LOCAL_TZ,
    )
    return {
        "Fajr": pt.fajr.strftime("%H:%M"),
        "Dhuhr": pt.dhuhr.strftime("%H:%M"),
        "Asr": pt.asr.strftime("%H:%M"),
        "Maghrib": pt.maghrib.strftime("%H:%M"),
        "Isha": pt.isha.strftime("%H:%M"),
    }


def get_state_payload() -> dict[str, Any]:
    """Build the JSON payload returned by GET /api/state."""
    settings, config = load_store()
    active_method = get_calculation_method(config.get("method")).name
    return {
        "speaker": config["speaker"],
        "latitude": config["latitude"],
        "longitude": config["longitude"],
        "method": active_method,
        "methods": [
            {"value": key, "label": label}
            for key, label in CALCULATION_METHOD_LABELS.items()
            if key in CalculationMethod.__members__
        ],
        "tz": TZ_NAME,
        "times": calculate_prayer_times(config=config),
        "settings": settings,
    }


def _trigger_scheduled_cast(prayer: str) -> None:
    threading.Thread(
        target=cast_athan,
        args=(prayer,),
        kwargs={"force": False},
        name=f"cast-{prayer.lower()}",
        daemon=True,
    ).start()


def schedule_daily_prayers() -> dict[str, str]:
    """Clear previous prayer jobs and schedule today's 5 prayers."""
    schedule.clear("prayer")
    today = datetime.datetime.now(LOCAL_TZ).date()
    config = load_config()
    times = calculate_prayer_times(today, config=config)
    logger.info(
        "Calculated prayer times for %s (%s, speaker='%s', lat=%.4f, lon=%.4f, tz=%s):",
        today.isoformat(),
        get_calculation_method(config.get("method")).name,
        config["speaker"],
        config["latitude"],
        config["longitude"],
        TZ_NAME,
    )
    for prayer in PRAYERS:
        hhmm = times[prayer]
        schedule.every().day.at(hhmm).do(_trigger_scheduled_cast, prayer).tag("prayer")
        logger.info("  Scheduled %-7s at %s", prayer, hhmm)
    return times


# ---------------------------------------------------------------------------
# Chromecast Casting
# ---------------------------------------------------------------------------
def audio_filename_for_prayer(prayer: str) -> str:
    return "fajr.mp3" if prayer == "Fajr" else "athan.mp3"


def cast_athan(prayer: str, force: bool = False) -> bool:
    """Discover the configured Chromecast speaker and cast the prayer MP3."""
    if prayer not in PRAYERS:
        logger.error("Invalid prayer name: %s", prayer)
        return False

    settings, config = load_store()
    speaker_name = config["speaker"]
    prayer_cfg = settings.get(prayer, DEFAULT_SETTINGS[prayer])
    enabled = bool(prayer_cfg.get("enabled", True))
    volume = _clamp_volume(prayer_cfg.get("volume", 50), 50)

    if not force and (not enabled or volume <= 0):
        logger.info(
            "Skipping %s Athan (enabled=%s, volume=%d%%) without waking speaker.",
            prayer,
            enabled,
            volume,
        )
        return False

    filename = audio_filename_for_prayer(prayer)
    audio_path = AUDIO_DIR / filename
    if not audio_path.is_file():
        logger.error(
            "Audio file not found for %s: %s (place %s in your audio/ directory).",
            prayer,
            audio_path,
            filename,
        )
        return False

    with CAST_LOCK:
        browser = None
        cast = None
        prev_volume: float | None = None
        try:
            logger.info(
                "Discovering speaker '%s' for %s Athan (volume=%d%%, force=%s)...",
                speaker_name,
                prayer,
                volume,
                force,
            )
            chromecasts, browser = pychromecast.get_listed_chromecasts(
                friendly_names=[speaker_name]
            )
            if not chromecasts:
                logger.error("Speaker '%s' not found on the network via mDNS.", speaker_name)
                return False

            cast = chromecasts[0]
            cast.wait()

            if cast.status is not None and cast.status.volume_level is not None:
                prev_volume = cast.status.volume_level
                logger.info("Saved previous speaker volume: %.0f%%", prev_volume * 100)

            target_volume = max(0.0, min(1.0, volume / 100.0))
            cast.set_volume(target_volume)
            logger.info("Set speaker volume to %d%%", volume)

            local_ip = get_local_ip()
            media_url = f"http://{local_ip}:{HTTP_PORT}/{filename}"
            logger.info("Casting %s to '%s'...", media_url, speaker_name)

            mc = cast.media_controller
            mc.play_media(media_url, "audio/mp3")
            try:
                mc.block_until_active(timeout=15)
            except Exception as exc:
                logger.warning("Timed out waiting for media controller active state: %s", exc)

            # Give the speaker a brief window to transition out of IDLE into BUFFERING/PLAYING.
            start_wait = time.monotonic()
            while time.monotonic() - start_wait < 10:
                state = mc.status.player_state if mc.status else None
                if state not in ("IDLE", "UNKNOWN", None):
                    break
                time.sleep(0.5)

            # Poll until playback finishes or safety timeout elapses.
            playback_start = time.monotonic()
            while True:
                state = mc.status.player_state if mc.status else None
                if state in ("IDLE", "UNKNOWN", None):
                    logger.info("%s Athan playback finished (state=%s).", prayer, state)
                    break
                if time.monotonic() - playback_start >= PLAYBACK_TIMEOUT_SECONDS:
                    logger.warning(
                        "%s Athan playback reached %ds safety timeout; stopping.",
                        prayer,
                        PLAYBACK_TIMEOUT_SECONDS,
                    )
                    try:
                        mc.stop()
                    except Exception:
                        pass
                    break
                time.sleep(1.0)

            if prev_volume is not None:
                cast.set_volume(prev_volume)
                logger.info("Restored speaker volume to %.0f%%", prev_volume * 100)

            return True
        except Exception as exc:
            logger.exception("Error while casting %s Athan: %s", prayer, exc)
            if cast is not None and prev_volume is not None:
                try:
                    cast.set_volume(prev_volume)
                except Exception:
                    pass
            return False
        finally:
            if cast is not None:
                try:
                    cast.disconnect()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.stop_discovery()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Web UI HTML (Light Mode)
# ---------------------------------------------------------------------------
INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Athan Caster</title>
  <style>
    :root {
      --bg: #ffffff;
      --surface: #ffffff;
      --surface-alt: #f8fafc;
      --border: #e2e8f0;
      --border-strong: #cbd5e1;
      --text: #0f172a;
      --muted: #475569;
      --accent: #059669;
      --accent-hover: #047857;
      --accent-soft: #ecfdf5;
      --danger: #dc2626;
      --danger-soft: #fef2f2;
      --radius: 14px;
      --shadow: 0 1px 3px rgba(15, 23, 42, 0.06), 0 1px 2px rgba(15, 23, 42, 0.04);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      padding: 20px 14px 56px;
      line-height: 1.45;
    }
    .container {
      max-width: 640px;
      margin: 0 auto;
    }
    header {
      margin-bottom: 18px;
      padding: 20px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
    }
    h1 {
      font-size: 1.45rem;
      font-weight: 700;
      color: var(--text);
      letter-spacing: -0.01em;
    }
    .meta {
      margin-top: 8px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px 18px;
      color: var(--muted);
      font-size: 0.88rem;
    }
    .meta strong { color: var(--text); font-weight: 600; }

    .section-title {
      font-size: 0.82rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--muted);
      margin: 22px 0 10px 2px;
    }

    .config-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 18px 20px;
      box-shadow: var(--shadow);
    }
    .config-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }
    .field {
      display: flex;
      flex-direction: column;
      gap: 5px;
    }
    .field.full {
      grid-column: 1 / -1;
    }
    .field label {
      font-size: 0.82rem;
      font-weight: 600;
      color: var(--muted);
    }
    .field input,
    .field select {
      width: 100%;
      padding: 9px 12px;
      font-size: 0.94rem;
      color: var(--text);
      background: var(--bg);
      border: 1px solid var(--border-strong);
      border-radius: 9px;
      outline: none;
      transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }
    .field input:focus,
    .field select:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(5, 150, 105, 0.14);
    }
    .config-actions {
      margin-top: 14px;
      display: flex;
      justify-content: flex-end;
    }
    .btn-primary {
      background: var(--accent);
      color: #ffffff;
      border: 1px solid var(--accent);
      border-radius: 9px;
      padding: 9px 16px;
      font-size: 0.88rem;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.15s ease, transform 0.1s ease;
    }
    .btn-primary:hover { background: var(--accent-hover); }
    .btn-primary:active { transform: scale(0.98); }
    .btn-primary:disabled { opacity: 0.6; cursor: wait; }

    .cards {
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 16px 18px;
      box-shadow: var(--shadow);
      transition: opacity 0.2s ease, border-color 0.2s ease, background 0.2s ease;
    }
    .card.muted {
      background: var(--surface-alt);
      opacity: 0.72;
    }
    .card-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }
    .prayer-info {
      display: flex;
      align-items: baseline;
      gap: 12px;
    }
    .prayer-name {
      font-size: 1.12rem;
      font-weight: 600;
      color: var(--text);
    }
    .prayer-time {
      font-size: 1.05rem;
      font-variant-numeric: tabular-nums;
      color: var(--accent-hover);
      font-weight: 700;
    }
    .actions {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .btn-test {
      background: var(--surface-alt);
      color: var(--text);
      border: 1px solid var(--border-strong);
      border-radius: 8px;
      padding: 6px 12px;
      font-size: 0.84rem;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.15s ease, border-color 0.15s ease, color 0.15s ease;
    }
    .btn-test:hover {
      background: var(--accent-soft);
      border-color: var(--accent);
      color: var(--accent-hover);
    }
    .btn-test:active { transform: scale(0.97); }
    .btn-test:disabled { opacity: 0.5; cursor: wait; }

    /* Toggle switch */
    .switch {
      position: relative;
      display: inline-block;
      width: 44px;
      height: 24px;
      flex-shrink: 0;
    }
    .switch input { opacity: 0; width: 0; height: 0; }
    .slider-toggle {
      position: absolute;
      inset: 0;
      background: #cbd5e1;
      border-radius: 999px;
      cursor: pointer;
      transition: background 0.2s ease;
    }
    .slider-toggle::before {
      content: "";
      position: absolute;
      width: 18px;
      height: 18px;
      left: 3px;
      top: 3px;
      background: #ffffff;
      border-radius: 50%;
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.2);
      transition: transform 0.2s ease;
    }
    .switch input:checked + .slider-toggle {
      background: var(--accent);
    }
    .switch input:checked + .slider-toggle::before {
      transform: translateX(20px);
    }

    .volume-row {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .volume-label {
      font-size: 0.82rem;
      color: var(--muted);
      min-width: 52px;
    }
    input[type="range"] {
      -webkit-appearance: none;
      appearance: none;
      flex: 1;
      height: 6px;
      border-radius: 999px;
      background: var(--border-strong);
      outline: none;
    }
    input[type="range"]::-webkit-slider-thumb {
      -webkit-appearance: none;
      appearance: none;
      width: 20px;
      height: 20px;
      border-radius: 50%;
      background: var(--accent);
      cursor: pointer;
      border: 2px solid #ffffff;
      box-shadow: 0 1px 3px rgba(15, 23, 42, 0.25);
    }
    input[type="range"]::-moz-range-thumb {
      width: 20px;
      height: 20px;
      border-radius: 50%;
      background: var(--accent);
      cursor: pointer;
      border: 2px solid #ffffff;
      box-shadow: 0 1px 3px rgba(15, 23, 42, 0.25);
    }
    .volume-value {
      min-width: 42px;
      text-align: right;
      font-size: 0.9rem;
      font-variant-numeric: tabular-nums;
      font-weight: 600;
      color: var(--text);
    }
    .toast {
      position: fixed;
      left: 50%;
      bottom: 22px;
      transform: translateX(-50%) translateY(20px);
      background: #ffffff;
      color: var(--text);
      border: 1px solid var(--accent);
      border-left: 4px solid var(--accent);
      padding: 11px 18px;
      border-radius: 10px;
      font-size: 0.9rem;
      font-weight: 500;
      max-width: min(92vw, 520px);
      box-shadow: 0 10px 25px rgba(15, 23, 42, 0.14);
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.2s ease, transform 0.2s ease;
      z-index: 100;
    }
    .toast.show {
      opacity: 1;
      transform: translateX(-50%) translateY(0);
    }
    .toast.error {
      background: var(--danger-soft);
      color: #991b1b;
      border-color: var(--danger);
      border-left-color: var(--danger);
    }

    @media (max-width: 480px) {
      .config-grid {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>Athan Caster</h1>
      <div class="meta">
        <span>Speaker: <strong id="speaker-name">Loading...</strong></span>
        <span>Method: <strong id="calc-method">Loading...</strong></span>
        <span>Timezone: <strong id="tz-name">Loading...</strong></span>
      </div>
    </header>

    <div class="section-title">Prayer Schedule &amp; Volume</div>
    <main class="cards" id="prayer-cards"></main>

    <div class="section-title">Configuration</div>
    <form class="config-card" id="config-form">
      <div class="config-grid">
        <div class="field full">
          <label for="input-speaker">Speaker Name</label>
          <input id="input-speaker" type="text" required placeholder="Living Room Speaker" />
        </div>
        <div class="field">
          <label for="input-lat">Latitude (-90 to 90)</label>
          <input id="input-lat" type="number" step="any" min="-90" max="90" required />
        </div>
        <div class="field">
          <label for="input-lon">Longitude (-180 to 180)</label>
          <input id="input-lon" type="number" step="any" min="-180" max="180" required />
        </div>
        <div class="field full">
          <label for="input-method">Calculation Method</label>
          <select id="input-method" required></select>
        </div>
      </div>
      <div class="config-actions">
        <button type="submit" class="btn-primary" id="btn-save-config">Save Configuration</button>
      </div>
    </form>
  </div>
  <div class="toast" id="toast" role="status" aria-live="polite"></div>

  <script>
    const PRAYERS = ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"];
    let state = { speaker: "", latitude: 0, longitude: 0, method: "", tz: "", times: {}, settings: {} };
    let toastTimer = null;
    let saveTimer = null;

    function showToast(message, isError = false) {
      const el = document.getElementById("toast");
      el.textContent = message;
      el.classList.toggle("error", isError);
      el.classList.add("show");
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => el.classList.remove("show"), isError ? 4500 : 2400);
    }

    async function extractError(res, fallback) {
      try {
        const data = await res.json();
        if (data && data.error) return data.error;
      } catch (_) {}
      return fallback + " (HTTP " + res.status + ")";
    }

    async function persistSettings() {
      try {
        const res = await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(state.settings),
        });
        if (!res.ok) {
          throw new Error(await extractError(res, "Failed to save settings"));
        }
        showToast("Settings saved");
      } catch (err) {
        showToast(err.message || "Failed to save settings", true);
        console.error(err);
      }
    }

    function scheduleSave(immediate = false) {
      clearTimeout(saveTimer);
      if (immediate) {
        persistSettings();
      } else {
        saveTimer = setTimeout(persistSettings, 250);
      }
    }

    async function triggerTest(prayer, btn) {
      btn.disabled = true;
      try {
        const res = await fetch("/api/test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ prayer }),
        });
        if (!res.ok) {
          throw new Error(await extractError(res, "Failed to trigger test cast"));
        }
        showToast("Test cast started for " + prayer);
      } catch (err) {
        showToast(err.message || "Failed to trigger test cast", true);
        console.error(err);
      } finally {
        setTimeout(() => { btn.disabled = false; }, 1000);
      }
    }

    function renderConfigForm() {
      document.getElementById("input-speaker").value = state.speaker || "";
      if (state.latitude !== undefined) document.getElementById("input-lat").value = state.latitude;
      if (state.longitude !== undefined) document.getElementById("input-lon").value = state.longitude;

      const select = document.getElementById("input-method");
      if (Array.isArray(state.methods) && state.methods.length > 0) {
        select.innerHTML = "";
        state.methods.forEach((m) => {
          const opt = document.createElement("option");
          opt.value = m.value;
          opt.textContent = m.label;
          if (m.value === state.method) opt.selected = true;
          select.appendChild(opt);
        });
      }
      if (state.method) select.value = state.method;
    }

    function methodDisplayName(code) {
      if (Array.isArray(state.methods)) {
        const found = state.methods.find((m) => m.value === code);
        if (found) return found.label;
      }
      return code || "-";
    }

    function render() {
      document.getElementById("speaker-name").textContent = state.speaker || "-";
      document.getElementById("calc-method").textContent = methodDisplayName(state.method);
      document.getElementById("tz-name").textContent = state.tz || "-";
      renderConfigForm();

      const container = document.getElementById("prayer-cards");
      container.innerHTML = "";

      PRAYERS.forEach((prayer) => {
        const cfg = state.settings[prayer] || { volume: 50, enabled: true };
        const timeStr = (state.times && state.times[prayer]) || "--:--";

        const card = document.createElement("section");
        card.className = "card" + (!cfg.enabled || cfg.volume <= 0 ? " muted" : "");

        card.innerHTML = `
          <div class="card-top">
            <div class="prayer-info">
              <span class="prayer-name">${prayer}</span>
              <span class="prayer-time">${timeStr}</span>
            </div>
            <div class="actions">
              <button type="button" class="btn-test" data-prayer="${prayer}">Test</button>
              <label class="switch" title="Enable or mute ${prayer}">
                <input type="checkbox" ${cfg.enabled ? "checked" : ""} />
                <span class="slider-toggle"></span>
              </label>
            </div>
          </div>
          <div class="volume-row">
            <span class="volume-label">Volume</span>
            <input type="range" min="0" max="100" step="1" value="${cfg.volume}" aria-label="${prayer} volume" />
            <span class="volume-value">${cfg.volume}%</span>
          </div>
        `;

        const checkbox = card.querySelector('input[type="checkbox"]');
        const range = card.querySelector('input[type="range"]');
        const valueEl = card.querySelector(".volume-value");
        const testBtn = card.querySelector(".btn-test");

        const updateMutedClass = () => {
          const isMuted = !state.settings[prayer].enabled || state.settings[prayer].volume <= 0;
          card.classList.toggle("muted", isMuted);
        };

        checkbox.addEventListener("change", () => {
          state.settings[prayer].enabled = checkbox.checked;
          updateMutedClass();
          scheduleSave(true);
        });

        range.addEventListener("input", () => {
          const vol = Number(range.value);
          state.settings[prayer].volume = vol;
          valueEl.textContent = vol + "%";
          updateMutedClass();
          scheduleSave(false);
        });

        range.addEventListener("change", () => {
          scheduleSave(true);
        });

        testBtn.addEventListener("click", () => triggerTest(prayer, testBtn));
        container.appendChild(card);
      });
    }

    document.getElementById("config-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = document.getElementById("btn-save-config");
      const speaker = document.getElementById("input-speaker").value.trim();
      const latitude = Number(document.getElementById("input-lat").value);
      const longitude = Number(document.getElementById("input-lon").value);
      const method = document.getElementById("input-method").value;

      btn.disabled = true;
      try {
        const res = await fetch("/api/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ speaker, latitude, longitude, method }),
        });
        if (!res.ok) {
          throw new Error(await extractError(res, "Failed to save configuration"));
        }
        const data = await res.json();
        if (data && data.state) {
          state = data.state;
          render();
        }
        showToast("Configuration saved; prayer times updated");
      } catch (err) {
        showToast(err.message || "Failed to save configuration", true);
        console.error(err);
      } finally {
        btn.disabled = false;
      }
    });

    async function loadState() {
      try {
        const res = await fetch("/api/state");
        if (!res.ok) throw new Error(await extractError(res, "Failed to load prayer state"));
        state = await res.json();
        render();
      } catch (err) {
        showToast(err.message || "Failed to load prayer state", true);
        console.error(err);
      }
    }

    loadState();
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP Server (Web UI + API + Audio Streaming)
# ---------------------------------------------------------------------------
class AthanRequestHandler(BaseHTTPRequestHandler):
    """Serves the control panel UI, JSON API endpoints, and MP3 files."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("HTTP %s - %s", self.address_string(), format % args)

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        status: int = HTTPStatus.OK,
        extra_headers: dict[str, str] | None = None,
        head_only: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send_bytes(
            body,
            "application/json; charset=utf-8",
            status=status,
            extra_headers={"Cache-Control": "no-store"},
        )

    def _read_json_body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json({"ok": False, "error": "Invalid Content-Length"}, HTTPStatus.BAD_REQUEST)
            return None
        if length <= 0 or length > 1_048_576:
            self._send_json({"ok": False, "error": "Invalid request body size"}, HTTPStatus.BAD_REQUEST)
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"ok": False, "error": "Invalid JSON body"}, HTTPStatus.BAD_REQUEST)
            return None
        if not isinstance(data, dict):
            self._send_json({"ok": False, "error": "Expected JSON object"}, HTTPStatus.BAD_REQUEST)
            return None
        return data

    def _format_write_error(self, exc: OSError) -> str:
        if isinstance(exc, PermissionError):
            return (
                f"Permission denied writing {VOLUMES_FILE}. "
                "On the host, run: sudo chown 1000:1000 volumes.json"
            )
        if isinstance(exc, IsADirectoryError):
            return (
                f"{VOLUMES_FILE} is a directory instead of a file. "
                "Recreate volumes.json on the host and restart."
            )
        return f"Failed to write settings file: {exc}"

    def _serve_audio(self, filename: str, head_only: bool = False) -> None:
        audio_path = AUDIO_DIR / filename
        if not audio_path.is_file():
            self._send_json(
                {"ok": False, "error": f"{filename} not found"},
                status=HTTPStatus.NOT_FOUND,
            )
            return

        file_size = audio_path.stat().st_size
        range_header = self.headers.get("Range")

        start = 0
        end = file_size - 1
        status = HTTPStatus.OK

        if range_header and range_header.startswith("bytes="):
            try:
                byte_range = range_header.split("=", 1)[1].split(",", 1)[0].strip()
                start_str, end_str = byte_range.split("-", 1)
                if start_str:
                    start = int(start_str)
                    end = int(end_str) if end_str else file_size - 1
                else:
                    suffix = int(end_str)
                    start = max(0, file_size - suffix)
                    end = file_size - 1
                if start < 0 or end >= file_size or start > end:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT
            except ValueError:
                start = 0
                end = file_size - 1
                status = HTTPStatus.OK

        content_length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(content_length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()

        if head_only:
            return

        with audio_path.open("rb") as fh:
            fh.seek(start)
            remaining = content_length
            while remaining > 0:
                chunk = fh.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except BrokenPipeError:
                    break
                remaining -= len(chunk)

    def do_HEAD(self) -> None:
        path = urlparse(self.path).path
        if path in ("/athan.mp3", "/fajr.mp3"):
            self._serve_audio(path.lstrip("/"), head_only=True)
        elif path == "/":
            self._send_bytes(
                INDEX_HTML.encode("utf-8"),
                "text/html; charset=utf-8",
                head_only=True,
            )
        else:
            self.send_response(HTTPStatus.NOT_FOUND)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_bytes(
                INDEX_HTML.encode("utf-8"),
                "text/html; charset=utf-8",
                extra_headers={"Cache-Control": "no-store"},
            )
        elif path == "/api/state":
            self._send_json(get_state_payload())
        elif path in ("/athan.mp3", "/fajr.mp3"):
            self._serve_audio(path.lstrip("/"), head_only=False)
        else:
            self._send_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/settings":
            body = self._read_json_body()
            if body is None:
                return
            try:
                save_settings(body)
            except OSError as exc:
                logger.error("Failed to persist settings to %s: %s", VOLUMES_FILE, exc)
                self._send_json(
                    {"ok": False, "error": self._format_write_error(exc)},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            self._send_json({"ok": True})
        elif path == "/api/config":
            body = self._read_json_body()
            if body is None:
                return
            try:
                save_config(body)
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            except OSError as exc:
                logger.error("Failed to persist config to %s: %s", VOLUMES_FILE, exc)
                self._send_json(
                    {"ok": False, "error": self._format_write_error(exc)},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            schedule_daily_prayers()
            self._send_json({"ok": True, "state": get_state_payload()})
        elif path == "/api/test":
            body = self._read_json_body()
            if body is None:
                return
            prayer = body.get("prayer")
            if prayer not in PRAYERS:
                self._send_json(
                    {"ok": False, "error": f"Invalid prayer. Must be one of: {', '.join(PRAYERS)}"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            threading.Thread(
                target=cast_athan,
                args=(prayer,),
                kwargs={"force": True},
                name=f"test-cast-{prayer.lower()}",
                daemon=True,
            ).start()
            self._send_json({"ok": True, "prayer": prayer})
        else:
            self._send_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_http_server() -> ReusableThreadingHTTPServer:
    """Start the combined Web UI and audio file HTTP server in a background thread."""
    server = ReusableThreadingHTTPServer(("0.0.0.0", HTTP_PORT), AthanRequestHandler)
    thread = threading.Thread(target=server.serve_forever, name="http-server", daemon=True)
    thread.start()
    logger.info("HTTP server listening on http://0.0.0.0:%d (LAN IP: %s)", HTTP_PORT, get_local_ip())
    return server


# ---------------------------------------------------------------------------
# Main Entrypoint
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline Islamic Athan Caster for Google Home/Nest")
    parser.add_argument(
        "--test",
        choices=PRAYERS,
        help="Immediately trigger a forced test cast for the specified prayer and exit.",
    )
    return parser.parse_args(argv)


def _handle_shutdown(signum: int, _frame: Any) -> None:
    logger.info("Received signal %s; shutting down...", signum)
    STOP_EVENT.set()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.test:
        server = None
        try:
            server = start_http_server()
        except OSError as exc:
            logger.info(
                "Port %d already in use (%s); assuming running athan-caster instance is serving audio.",
                HTTP_PORT,
                exc,
            )
        try:
            ok = cast_athan(args.test, force=True)
            return 0 if ok else 1
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    check_volumes_file_writable()
    server = start_http_server()

    # Schedule today's prayers immediately and refresh daily at 00:05.
    schedule_daily_prayers()
    schedule.every().day.at("00:05").do(schedule_daily_prayers).tag("daily-refresh")
    logger.info("Daily schedule refresh registered at 00:05 (%s).", TZ_NAME)

    try:
        while not STOP_EVENT.is_set():
            schedule.run_pending()
            STOP_EVENT.wait(timeout=1.0)
    finally:
        logger.info("Stopping HTTP server...")
        server.shutdown()
        server.server_close()
        logger.info("Shutdown complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
