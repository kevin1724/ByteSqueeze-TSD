"""
Settings handling for HandBrake TSD Helper.

This module manages user-tunable settings such as:
- HandBrake thread (CPU core) count
- CPU profile selection for ETA estimation
- (future) UI theme, etc.

Settings are stored as JSON on disk in settings.json.
"""

import json
import os
import re
import shutil
import threading

from .config import DATA_DIR, ROOTS
from .cpu_profiles import CPU_PROFILES  # NEW

# Where settings are stored on disk (inside /app/data by default)
SETTINGS_FILE = (DATA_DIR.rstrip("/") + "/settings.json")

# Default values for all settings
DEFAULT_SETTINGS = {
    # Interface generation. V3 is the beta default, while V2 remains a fully
    # supported fallback that can be selected from Settings or with ?ui=v2.
    "ui_version": "v3",
    "ui_density": "comfortable",

    # HandBrake threads (0 = auto / HandBrake default)
    "hb_threads": 0,

    # Global output muxer. MKV remains the migration-safe default for existing
    # installations; users may explicitly opt into per-file Auto selection.
    # The requested setting is snapshotted onto a job when it is queued.
    "output_container": "mkv",
    "web_optimized": False,
    # Safe default for every normal video job.  Optimization is opt-in and
    # track-specific; channel downmixing and object-audio loss are never
    # enabled implicitly.
    "audio_policy_default": "preserve",
    "audio_optimize_codec": "aac",
    "audio_allow_downmix": False,
    "audio_allow_object_metadata_loss": False,

    # Size Wizard / ETA estimation
    "cpu_profile": "i5-9500t",      # baseline CPU
    "cpu_speed_override": 1.0,       # multiplier (1.0 = no adjustment)
    "qsv_device_available": False,   # True only when /dev/dri is mounted into the container.
    # Hardware encoders can safely share a worker when the GPU supports more
    # than one session. Software encoders always remain exclusive regardless
    # of this value. One preserves the previous behavior after an upgrade.
    "hardware_transcode_concurrency": 1,
    # Used only when this code is running as a headless worker. It lets the
    # paired controller change GPU capacity without a worker-side website.
    "worker_controller_managed_capacity": False,

    # Stop a running encode when checkpoint-based projected output size is at
    # or above this percentage of the original source. Disabled by default.
    "auto_stop_large_output_enabled": False,
    "auto_stop_large_output_percent": 90,

    # Queue UI behavior on the Jobs page
    # buttons = classic button controls
    # drag_drop = grab rows and reorder visually
    "queue_ui_mode": "buttons",

    # Keyless catalog metadata is enabled by default. When TMDb credentials are
    # configured its artwork wins; local/TVmaze/Apple artwork remains the
    # automatic fallback and TVmaze continues to supply show schedules.
    "metadata_no_key_enabled": True,
    "metadata_country": "US",
    "episode_release_monitor_enabled": True,
    "episode_release_refresh_hours": 12,

    # Optional preferred TMDb artwork provider.
    "tmdb_api_key": "",
    "tmdb_bearer_token": "",

    # Beta page folder mapping. These narrow Beta scans to the folders users care about.
    "beta_media_folders": {
        "movies": [],
        "shows": [],
    },

    # Beta incremental auto scan. The scan engine reuses the Beta cache and
    # only reparses files whose path, size, or modified time changed.
    "beta_auto_scan_enabled": False,
    "beta_auto_scan_interval_minutes": 30,
    "beta_auto_scan_skip_while_encoding": True,
    "beta_auto_scan_auto_queue_tracked": True,
    "beta_auto_scan_file_stability_enabled": True,
    "beta_auto_scan_file_stability_minutes": 10,

    # Autopilot is intentionally bounded and defaults to observe-only. In
    # manage mode it can queue stable, eligible files up to the configured
    # per-run and active-job limits.
    "autopilot_enabled": False,
    "autopilot_mode": "observe",
    "autopilot_include_movies": True,
    "autopilot_include_shows": False,
    "autopilot_min_size_gb": 2.0,
    "autopilot_min_savings_percent": 10.0,
    "autopilot_batch_limit": 3,
    "autopilot_max_active_jobs": 5,
    "autopilot_schedule_start": "00:00",
    "autopilot_schedule_end": "23:59",
    "autopilot_continuous_learning_enabled": True,
    "autopilot_tour_completed": False,

    # Optional cloud advisor for Size Wizard. Keys remain server-side and are
    # never needed by the deterministic planning/encoding path.
    "wizard_ai_provider": "local",
    "gemini_api_key": "",
    "gemini_model": "gemini-3.6-flash",
    "openai_api_key": "",
    "openai_model": "gpt-5.6-luna",

    # Worker-side folder used for remote-transfer source downloads and
    # temporary encodes. Blank means DATA_DIR/node_transfer_work.
    "remote_transfer_temp_dir": "",
}

_settings_cache: dict | None = None
SETTINGS_LOCK = threading.RLock()


def _ensure_dict(obj) -> dict:
    if isinstance(obj, dict):
        return obj
    return {}


def _path_is_under_allowed_root(path: str) -> bool:
    if not path:
        return False
    try:
        real = os.path.realpath(path)
        for root_path, _label in ROOTS:
            root_real = os.path.realpath(root_path)
            try:
                if os.path.commonpath([real, root_real]) == root_real:
                    return True
            except ValueError:
                continue
    except Exception:
        return False
    return False


def _normalize_beta_folder_rows(rows, default_label: str) -> list[dict]:
    out = []
    seen = set()
    if not isinstance(rows, list):
        return out

    for row in rows:
        if isinstance(row, str):
            path = row
            label = ""
        elif isinstance(row, dict):
            path = row.get("path") or ""
            label = row.get("label") or ""
        else:
            continue

        path = str(path or "").strip()
        if not path or not _path_is_under_allowed_root(path):
            continue

        real_key = os.path.normcase(os.path.realpath(path))
        if real_key in seen:
            continue
        seen.add(real_key)

        label = str(label or "").strip()
        if not label:
            label = os.path.basename(path.rstrip("/\\")) or default_label

        out.append({"path": path, "label": label[:80]})
        if len(out) >= 50:
            break

    return out


def _normalize_beta_media_folders(value) -> dict:
    value = _ensure_dict(value)
    return {
        "movies": _normalize_beta_folder_rows(value.get("movies"), "Movies"),
        "shows": _normalize_beta_folder_rows(value.get("shows"), "Shows"),
    }


def normalize_output_container(value) -> str:
    """Return the supported public container id for settings and job policy."""
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


def load_settings() -> dict:
    """Load settings from disk (with in-memory caching)."""
    global _settings_cache
    with SETTINGS_LOCK:
        if _settings_cache is not None:
            return _settings_cache

        data = {}
        for path in (SETTINGS_FILE, SETTINGS_FILE + ".bak"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    candidate = json.load(f)
                if isinstance(candidate, dict):
                    data = candidate
                    break
            except FileNotFoundError:
                continue
            except Exception as e:  # just logging
                print(f"[WARN] Failed to load {os.path.basename(path)}: {e}", flush=True)

        data = _ensure_dict(data)
        merged = DEFAULT_SETTINGS.copy()
        merged.update(data)
        merged["ui_version"] = str(merged.get("ui_version") or "v3").strip().lower()
        if merged["ui_version"] not in {"v2", "v3"}:
            merged["ui_version"] = "v3"
        merged["ui_density"] = str(merged.get("ui_density") or "comfortable").strip().lower()
        if merged["ui_density"] not in {"comfortable", "compact"}:
            merged["ui_density"] = "comfortable"
        merged["output_container"] = normalize_output_container(
            merged.get("output_container")
        )
        merged["web_optimized"] = bool(
            merged["output_container"] == "mp4"
            and _boolean_value(merged.get("web_optimized"), False)
        )
        merged["audio_policy_default"] = str(
            merged.get("audio_policy_default") or "preserve"
        ).strip().lower()
        if merged["audio_policy_default"] not in {"preserve", "optimize_lossless", "custom"}:
            merged["audio_policy_default"] = "preserve"
        merged["audio_optimize_codec"] = str(
            merged.get("audio_optimize_codec") or "aac"
        ).strip().lower()
        if merged["audio_optimize_codec"] not in {"aac", "eac3", "ac3", "opus", "flac"}:
            merged["audio_optimize_codec"] = "aac"
        merged["audio_allow_downmix"] = _boolean_value(merged.get("audio_allow_downmix"), False)
        merged["audio_allow_object_metadata_loss"] = _boolean_value(
            merged.get("audio_allow_object_metadata_loss"), False
        )
        merged["hardware_transcode_concurrency"] = _bounded_number(
            merged.get("hardware_transcode_concurrency", 1),
            1,
            1,
            8,
            integer=True,
        )
        merged["worker_controller_managed_capacity"] = bool(
            merged.get("worker_controller_managed_capacity", False)
        )
        merged["beta_media_folders"] = _normalize_beta_media_folders(merged.get("beta_media_folders"))
        _settings_cache = merged
        return merged


def _bounded_number(value, default, minimum, maximum, *, integer: bool = False):
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError):
        parsed = default
    parsed = max(minimum, min(maximum, parsed))
    return int(parsed) if integer else round(float(parsed), 2)


def _clock_value(value, default: str) -> str:
    text = str(value or "").strip()
    try:
        hour_text, minute_text = text.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    except (TypeError, ValueError):
        pass
    return default


def _save_settings_unlocked(new_values: dict) -> dict:
    """Merge and persist settings to disk, returning the updated dict."""
    global _settings_cache
    with SETTINGS_LOCK:
        base = load_settings().copy()
        new_values = _ensure_dict(new_values)

    # ------------------------------------------------------------------
    # HandBrake threads
    # ------------------------------------------------------------------
    hb_threads = new_values.get("hb_threads", base.get("hb_threads", 0))
    try:
        hb_threads_int = int(hb_threads)
    except (TypeError, ValueError):
        hb_threads_int = 0
    if hb_threads_int < 0:
        hb_threads_int = 0

    base["hb_threads"] = hb_threads_int

    # ------------------------------------------------------------------
    # Global output muxer / MP4 fast start
    # ------------------------------------------------------------------
    output_container = normalize_output_container(
        new_values.get("output_container", base.get("output_container"))
    )
    base["output_container"] = output_container
    base["web_optimized"] = bool(
        output_container == "mp4"
        and _boolean_value(
            new_values.get("web_optimized", base.get("web_optimized", False)),
            False,
        )
    )
    audio_policy = str(
        new_values.get("audio_policy_default", base.get("audio_policy_default", "preserve"))
        or "preserve"
    ).strip().lower()
    base["audio_policy_default"] = audio_policy if audio_policy in {
        "preserve", "optimize_lossless", "custom"
    } else "preserve"
    audio_codec = str(
        new_values.get("audio_optimize_codec", base.get("audio_optimize_codec", "aac"))
        or "aac"
    ).strip().lower()
    base["audio_optimize_codec"] = audio_codec if audio_codec in {
        "aac", "eac3", "ac3", "opus", "flac"
    } else "aac"
    base["audio_allow_downmix"] = _boolean_value(
        new_values.get("audio_allow_downmix", base.get("audio_allow_downmix", False)), False
    )
    base["audio_allow_object_metadata_loss"] = _boolean_value(
        new_values.get(
            "audio_allow_object_metadata_loss",
            base.get("audio_allow_object_metadata_loss", False),
        ),
        False,
    )

    # ------------------------------------------------------------------
    # CPU profile (Size Wizard ETA)
    # ------------------------------------------------------------------
    cpu_profile = new_values.get("cpu_profile", base.get("cpu_profile"))
    if isinstance(cpu_profile, str):
        cpu_profile = cpu_profile.strip().lower()
    else:
        cpu_profile = None

    if cpu_profile not in CPU_PROFILES:
        cpu_profile = DEFAULT_SETTINGS["cpu_profile"]

    base["cpu_profile"] = cpu_profile

    # ------------------------------------------------------------------
    # CPU speed override multiplier
    # ------------------------------------------------------------------
    cpu_speed_override = new_values.get(
        "cpu_speed_override",
        base.get("cpu_speed_override", 1.0),
    )
    try:
        cpu_speed_override = float(cpu_speed_override)
    except (TypeError, ValueError):
        cpu_speed_override = 1.0

    # sanity clamp
    if cpu_speed_override <= 0:
        cpu_speed_override = 1.0

    base["cpu_speed_override"] = cpu_speed_override

    # ------------------------------------------------------------------
    # Intel QSV render device availability
    # ------------------------------------------------------------------
    base["qsv_device_available"] = bool(
        new_values.get("qsv_device_available", base.get("qsv_device_available", False))
    )
    base["hardware_transcode_concurrency"] = _bounded_number(
        new_values.get(
            "hardware_transcode_concurrency",
            base.get("hardware_transcode_concurrency", 1),
        ),
        1,
        1,
        8,
        integer=True,
    )
    base["worker_controller_managed_capacity"] = bool(
        new_values.get(
            "worker_controller_managed_capacity",
            base.get("worker_controller_managed_capacity", False),
        )
    )

    base["auto_stop_large_output_enabled"] = bool(
        new_values.get(
            "auto_stop_large_output_enabled",
            base.get("auto_stop_large_output_enabled", False),
        )
    )
    try:
        stop_percent = float(
            new_values.get(
                "auto_stop_large_output_percent",
                base.get("auto_stop_large_output_percent", 90),
            )
        )
    except (TypeError, ValueError):
        stop_percent = 90.0
    base["auto_stop_large_output_percent"] = round(max(1.0, min(500.0, stop_percent)), 1)

    # ------------------------------------------------------------------
    # Queue UI mode
    # ------------------------------------------------------------------
    queue_ui_mode = new_values.get("queue_ui_mode", base.get("queue_ui_mode", "buttons"))
    if isinstance(queue_ui_mode, str):
        queue_ui_mode = queue_ui_mode.strip().lower()
    else:
        queue_ui_mode = "buttons"
    if queue_ui_mode not in {"buttons", "drag_drop"}:
        queue_ui_mode = "buttons"
    base["queue_ui_mode"] = queue_ui_mode

    # ------------------------------------------------------------------
    # Interface generation and density
    # ------------------------------------------------------------------
    ui_version = str(new_values.get("ui_version", base.get("ui_version", "v3")) or "v3").strip().lower()
    base["ui_version"] = ui_version if ui_version in {"v2", "v3"} else "v3"
    ui_density = str(
        new_values.get("ui_density", base.get("ui_density", "comfortable")) or "comfortable"
    ).strip().lower()
    base["ui_density"] = ui_density if ui_density in {"comfortable", "compact"} else "comfortable"

    # ------------------------------------------------------------------
    # Poster metadata credentials
    # ------------------------------------------------------------------
    for key in ("tmdb_api_key", "tmdb_bearer_token"):
        value = new_values.get(key, base.get(key, ""))
        base[key] = str(value or "").strip()

    base["metadata_no_key_enabled"] = bool(
        new_values.get("metadata_no_key_enabled", base.get("metadata_no_key_enabled", True))
    )
    base["episode_release_monitor_enabled"] = bool(
        new_values.get(
            "episode_release_monitor_enabled",
            base.get("episode_release_monitor_enabled", True),
        )
    )
    country = str(new_values.get("metadata_country", base.get("metadata_country", "US")) or "US").strip().upper()
    base["metadata_country"] = country[:2] if len(country) >= 2 else "US"
    base["episode_release_refresh_hours"] = _bounded_number(
        new_values.get(
            "episode_release_refresh_hours",
            base.get("episode_release_refresh_hours", 12),
        ),
        12,
        1,
        168,
        integer=True,
    )

    # ------------------------------------------------------------------
    # Beta media folder mapping
    # ------------------------------------------------------------------
    beta_media_folders = new_values.get(
        "beta_media_folders",
        base.get("beta_media_folders", DEFAULT_SETTINGS["beta_media_folders"]),
    )
    base["beta_media_folders"] = _normalize_beta_media_folders(beta_media_folders)

    # ------------------------------------------------------------------
    # Beta auto scan settings
    # ------------------------------------------------------------------
    base["beta_auto_scan_enabled"] = bool(
        new_values.get("beta_auto_scan_enabled", base.get("beta_auto_scan_enabled", False))
    )
    base["beta_auto_scan_skip_while_encoding"] = bool(
        new_values.get(
            "beta_auto_scan_skip_while_encoding",
            base.get("beta_auto_scan_skip_while_encoding", True),
        )
    )
    base["beta_auto_scan_auto_queue_tracked"] = bool(
        new_values.get(
            "beta_auto_scan_auto_queue_tracked",
            base.get("beta_auto_scan_auto_queue_tracked", True),
        )
    )
    base["beta_auto_scan_file_stability_enabled"] = bool(
        new_values.get(
            "beta_auto_scan_file_stability_enabled",
            base.get("beta_auto_scan_file_stability_enabled", True),
        )
    )

    try:
        interval = int(
            new_values.get(
                "beta_auto_scan_interval_minutes",
                base.get("beta_auto_scan_interval_minutes", 30),
            )
        )
    except (TypeError, ValueError):
        interval = 30
    base["beta_auto_scan_interval_minutes"] = max(5, min(1440, interval))

    try:
        stability_minutes = int(
            new_values.get(
                "beta_auto_scan_file_stability_minutes",
                base.get("beta_auto_scan_file_stability_minutes", 10),
            )
        )
    except (TypeError, ValueError):
        stability_minutes = 10
    base["beta_auto_scan_file_stability_minutes"] = max(1, min(240, stability_minutes))

    # ------------------------------------------------------------------
    # Bounded Autopilot policy
    # ------------------------------------------------------------------
    base["autopilot_enabled"] = bool(new_values.get("autopilot_enabled", base.get("autopilot_enabled", False)))
    mode = str(new_values.get("autopilot_mode", base.get("autopilot_mode", "observe"))).strip().lower()
    base["autopilot_mode"] = mode if mode in {"observe", "manage"} else "observe"
    base["autopilot_include_movies"] = bool(new_values.get("autopilot_include_movies", base.get("autopilot_include_movies", True)))
    base["autopilot_include_shows"] = bool(new_values.get("autopilot_include_shows", base.get("autopilot_include_shows", False)))
    base["autopilot_min_size_gb"] = _bounded_number(
        new_values.get("autopilot_min_size_gb", base.get("autopilot_min_size_gb", 2.0)), 2.0, 0.1, 1000.0
    )
    base["autopilot_min_savings_percent"] = _bounded_number(
        new_values.get("autopilot_min_savings_percent", base.get("autopilot_min_savings_percent", 10.0)), 10.0, 0.0, 95.0
    )
    base["autopilot_batch_limit"] = _bounded_number(
        new_values.get("autopilot_batch_limit", base.get("autopilot_batch_limit", 3)), 3, 1, 50, integer=True
    )
    base["autopilot_max_active_jobs"] = _bounded_number(
        new_values.get("autopilot_max_active_jobs", base.get("autopilot_max_active_jobs", 5)), 5, 1, 100, integer=True
    )
    base["autopilot_schedule_start"] = _clock_value(
        new_values.get("autopilot_schedule_start", base.get("autopilot_schedule_start", "00:00")), "00:00"
    )
    base["autopilot_schedule_end"] = _clock_value(
        new_values.get("autopilot_schedule_end", base.get("autopilot_schedule_end", "23:59")), "23:59"
    )
    base["autopilot_continuous_learning_enabled"] = bool(
        new_values.get(
            "autopilot_continuous_learning_enabled",
            base.get("autopilot_continuous_learning_enabled", True),
        )
    )
    base["autopilot_tour_completed"] = bool(
        new_values.get("autopilot_tour_completed", base.get("autopilot_tour_completed", False))
    )

    # ------------------------------------------------------------------
    # Optional Size Wizard cloud advisor
    # ------------------------------------------------------------------
    provider = str(new_values.get("wizard_ai_provider", base.get("wizard_ai_provider", "local"))).strip().lower()
    base["wizard_ai_provider"] = provider if provider in {"off", "local", "gemini", "openai"} else "local"
    for key in ("gemini_api_key", "openai_api_key"):
        if key in new_values:
            base[key] = str(new_values.get(key) or "").strip()[:500]
    model_rules = {
        "gemini_model": ("gemini-3.6-flash", r"[A-Za-z0-9._-]{3,100}"),
        "openai_model": ("gpt-5.6-luna", r"[A-Za-z0-9._-]{3,100}"),
    }
    for key, (default, pattern) in model_rules.items():
        value = str(new_values.get(key, base.get(key, default)) or default).strip()
        base[key] = value if re.fullmatch(pattern, value) else default

    # ------------------------------------------------------------------
    # Remote transfer worker temp folder
    # ------------------------------------------------------------------
    remote_temp = new_values.get("remote_transfer_temp_dir", base.get("remote_transfer_temp_dir", ""))
    base["remote_transfer_temp_dir"] = str(remote_temp or "").strip()[:500]

    # ------------------------------------------------------------------
    # Persist
    # ------------------------------------------------------------------
    _settings_cache = base
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE) or DATA_DIR, exist_ok=True)
        tmp = f"{SETTINGS_FILE}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(base, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SETTINGS_FILE)
        try:
            shutil.copy2(SETTINGS_FILE, SETTINGS_FILE + ".bak")
        except OSError:
            pass
    except Exception as e:  # just logging
        print(f"[WARN] Failed to save settings.json: {e}", flush=True)

    return base


def save_settings(new_values: dict) -> dict:
    """Serialize settings updates so concurrent requests cannot lose fields."""
    with SETTINGS_LOCK:
        return _save_settings_unlocked(new_values)
