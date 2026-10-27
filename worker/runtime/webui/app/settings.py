"""Encoding-only durable settings for the headless worker.

Controller job payloads carry the effective thread and output-size safety
policy. These local defaults support older controllers and worker restarts.
"""

from __future__ import annotations

import json
import os
import shutil
import threading

from .config import DATA_DIR


SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
SETTINGS_LOCK = threading.RLock()
DEFAULT_SETTINGS = {
    "hb_threads": 0,
    "hardware_transcode_concurrency": 1,
    "worker_controller_managed_capacity": False,
    "auto_stop_large_output_enabled": False,
    "auto_stop_large_output_percent": 90.0,
    "output_container": "mkv",
    "web_optimized": False,
    "audio_policy_default": "preserve",
    "audio_optimize_codec": "aac",
    "audio_optimize_bitrate_kbps": 1024,
    "audio_deduplicate_languages": True,
    "audio_preferred_languages": ["eng", "spa"],
    "audio_allow_downmix": False,
    "audio_allow_object_metadata_loss": False,
    "remote_transfer_temp_dir": "/work/jobs",
}
_settings_cache: dict | None = None


def normalize_output_container(value) -> str:
    """Return the same public container id accepted by the controller."""
    normalized = str(value or "").strip().lower()
    aliases = {
        "auto": "auto",
        "smart": "auto",
        "mp4": "mp4",
        "m4v": "mp4",
        "av_mp4": "mp4",
        "mkv": "mkv",
        "matroska": "mkv",
        "av_mkv": "mkv",
    }
    return aliases.get(normalized, DEFAULT_SETTINGS["output_container"])


def _boolean_value(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    if isinstance(value, (int, float)):
        return value != 0
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    return bool(default)


def _normalize_audio_languages(value) -> list[str]:
    aliases = {
        "en": "eng", "eng": "eng", "english": "eng",
        "es": "spa", "spa": "spa", "spanish": "spa", "español": "spa",
    }
    rows = value if isinstance(value, list) else str(value or "").replace(";", ",").split(",")
    result = []
    for row in rows:
        language = aliases.get(str(row).strip().lower(), str(row).strip().lower())
        if language and language not in result:
            result.append(language[:24])
    return result[:16]


def _normalized(values: dict | None) -> dict:
    values = values if isinstance(values, dict) else {}
    try:
        hb_threads = max(0, min(256, int(values.get("hb_threads") or 0)))
    except (TypeError, ValueError):
        hb_threads = 0
    try:
        stop_percent = float(values.get("auto_stop_large_output_percent") or 90.0)
    except (TypeError, ValueError):
        stop_percent = 90.0
    try:
        hardware_concurrency = max(
            1,
            min(8, int(values.get("hardware_transcode_concurrency") or 1)),
        )
    except (TypeError, ValueError):
        hardware_concurrency = 1
    try:
        audio_bitrate = max(
            640,
            min(2048, int(values.get("audio_optimize_bitrate_kbps") or 1024)),
        )
    except (TypeError, ValueError):
        audio_bitrate = 1024
    output_container = normalize_output_container(values.get("output_container"))
    audio_policy = str(values.get("audio_policy_default") or "preserve").strip().lower()
    if audio_policy not in {"preserve", "optimize_lossless", "custom"}:
        audio_policy = "preserve"
    audio_codec = str(values.get("audio_optimize_codec") or "aac").strip().lower()
    if audio_codec not in {"aac", "eac3", "ac3", "opus", "flac"}:
        audio_codec = "aac"
    return {
        "hb_threads": hb_threads,
        "hardware_transcode_concurrency": hardware_concurrency,
        "worker_controller_managed_capacity": bool(
            values.get("worker_controller_managed_capacity", False)
        ),
        "auto_stop_large_output_enabled": bool(values.get("auto_stop_large_output_enabled", False)),
        "auto_stop_large_output_percent": round(max(1.0, min(500.0, stop_percent)), 1),
        "output_container": output_container,
        "web_optimized": bool(
            output_container == "mp4"
            and _boolean_value(values.get("web_optimized"), False)
        ),
        "audio_policy_default": audio_policy,
        "audio_optimize_codec": audio_codec,
        "audio_optimize_bitrate_kbps": audio_bitrate,
        "audio_deduplicate_languages": _boolean_value(
            values.get("audio_deduplicate_languages"), True
        ),
        "audio_preferred_languages": (
            _normalize_audio_languages(values.get("audio_preferred_languages"))
            or ["eng", "spa"]
        ),
        "audio_allow_downmix": _boolean_value(values.get("audio_allow_downmix"), False),
        "audio_allow_object_metadata_loss": _boolean_value(
            values.get("audio_allow_object_metadata_loss"), False
        ),
        "remote_transfer_temp_dir": str(values.get("remote_transfer_temp_dir") or "/work/jobs").strip()[:500],
    }


def load_settings() -> dict:
    global _settings_cache
    with SETTINGS_LOCK:
        if _settings_cache is not None:
            return dict(_settings_cache)
        loaded = {}
        for path in (SETTINGS_FILE, SETTINGS_FILE + ".bak"):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    candidate = json.load(handle)
                if isinstance(candidate, dict):
                    loaded = candidate
                    break
            except FileNotFoundError:
                continue
            except Exception as exc:
                print(f"[WARN] Failed to load worker settings: {exc}", flush=True)
        _settings_cache = _normalized({**DEFAULT_SETTINGS, **loaded})
        return dict(_settings_cache)


def save_settings(new_values: dict) -> dict:
    global _settings_cache
    with SETTINGS_LOCK:
        merged = {**load_settings(), **(new_values if isinstance(new_values, dict) else {})}
        _settings_cache = _normalized(merged)
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = f"{SETTINGS_FILE}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(_settings_cache, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, SETTINGS_FILE)
        try:
            shutil.copy2(SETTINGS_FILE, SETTINGS_FILE + ".bak")
        except OSError:
            pass
        return dict(_settings_cache)
