"""
Job management & dispatcher logic for HandBrake TSD Helper.

This module is responsible for:
- Keeping track of all jobs (in-memory + persisted to disk)
- Running CPU jobs exclusively and hardware jobs at the configured concurrency
- Parsing HandBrake progress output to update job progress
- Parsing ETA from HandBrake output to estimate remaining time
- Canceling jobs, removing from queue, clearing finished jobs
- Pause / resume queue state

It does NOT know about Flask or HTTP. The web layer should call into
these functions to:
    - create jobs
    - list jobs
    - cancel/remove jobs
    - query status
    - pause/resume queue
"""

import os
import ntpath
import re
import json
import uuid
import shlex
import time
import threading
import subprocess
import http.client
import shutil
import sys
import traceback
from copy import deepcopy
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from .config import (
    DATA_DIR,
    LOG_DIR,
    JOBS_FILE,
    VIDEO_EXTS,
    ALLOWED_PREFIXES,
)
from .presets import load_preset_definition, resolve_preset_file_and_name
from .settings import load_settings, normalize_output_container
from .events import log_event
from .process_utils import background_process_options, terminate_process_tree
from .storage_stats import get_summary as get_storage_summary, list_encodes, record_encode
from .audio_optimization import (
    ffmpeg_audio_only_command,
    handbrake_audio_args,
    normalize_operations,
    scan_media,
    storage_breakdown,
    validate_audio_only,
)


def _process_group_options() -> dict:
    """Start encoders in their own group on both POSIX and Windows."""
    return background_process_options(process_group=True)


def _encoder_launch_command() -> tuple[list[str], str]:
    """Return the platform launcher while keeping one dispatcher contract."""
    if os.name != "nt":
        return ["/bin/sh", "/worker/encode-one.sh"], "/worker/encode-one.sh"
    if getattr(sys, "frozen", False):
        return [sys.executable, "--encode-one"], "ByteSqueezeWorker.exe --encode-one"
    return [sys.executable, "-m", "worker.windows_app", "--encode-one"], "python -m worker.windows_app --encode-one"

# -------------------------------------------------------------------
# Global in-memory job state
# -------------------------------------------------------------------
# jobs: main dictionary with all job metadata
#   key: job_id (str UUID)
#   value: {
#       "status": "queued" | "running" | "done" | "error" | "canceled",
#       "src": "/path/to/video.mkv",
#       "preset": "1080" | "4k",
#       "log": "last few KB of HandBrake output",
#       "returncode": int | None,
#       "pid": int | None,
#       "progress": float (0.0 - 100.0),
#       "eta_seconds": float | None   # NEW: remaining time in seconds (if known)
#   }
#
# job_queue: list of job_ids representing run order for "queued" jobs
# queue_paused: if True, dispatcher will NOT start new jobs
# dispatcher_started: ensures we only start one dispatcher thread
# -------------------------------------------------------------------

jobs: dict[str, dict] = {}
job_queue: list[str] = []
queue_paused: bool = False
dispatcher_started: bool = False
transfer_retry_started: bool = False
dashboard_totals: dict[str, float | int] = {}
# Completed rows at or before this timestamp remain part of lifetime storage
# totals, but are hidden from the operational queue/history table after the
# user chooses "Clear finished queue records".
history_cleared_before: float = 0.0
JOBS_SAVE_LOCK = threading.RLock()
DISPATCH_LOCK = threading.RLock()
DISPATCH_WAKE_EVENT = threading.Event()
RUNNING_JOB_THREADS: dict[str, threading.Thread] = {}
TRANSFER_WORK_DIR = os.path.join(DATA_DIR, "node_transfer_work")
PRESET_WORK_DIR = os.path.join(DATA_DIR, "node_job_presets")
OUTPUT_ESTIMATE_CHECKPOINTS = (2, 10, 25, 60, 90)
TRANSFER_RETRY_MIN_SECONDS = 15
TRANSFER_RETRY_MAX_SECONDS = 30 * 60
JOB_LOG_TAIL_CHARS = 12_000
JOB_LOG_API_MAX_BYTES = 2 * 1024 * 1024
HARDWARE_ENCODER_FAMILIES = frozenset({
    "qsv",
    "nvenc",
    "vce",
    "amf",
    "videotoolbox",
    "vaapi",
})
QSV_ENCODERS = frozenset({"qsv_h264", "qsv_h265", "qsv_h265_10bit", "qsv_av1", "qsv_av1_10bit"})
QSV_DECODE_SOURCE_CODECS = frozenset({"h264", "avc", "avc1", "hevc", "h265"})
WINDOWS_GPU_ENCODERS = {
    "qsv": {
        ("h264", "8"): "qsv_h264",
        ("h265", "8"): "qsv_h265",
        ("h265", "10"): "qsv_h265_10bit",
        ("av1", "8"): "qsv_av1",
        ("av1", "10"): "qsv_av1_10bit",
    },
    "nvenc": {
        ("h264", "8"): "nvenc_h264",
        ("h265", "8"): "nvenc_h265",
        ("h265", "10"): "nvenc_h265_10bit",
        ("av1", "8"): "nvenc_av1",
        ("av1", "10"): "nvenc_av1_10bit",
    },
    "vce": {
        ("h264", "8"): "vce_h264",
        ("h265", "8"): "vce_h265",
        ("h265", "10"): "vce_h265_10bit",
        ("av1", "8"): "vce_av1",
        ("av1", "10"): "vce_av1_10bit",
    },
}
QSV_DECODE_POSITIVE_RE = re.compile(
    r'(?:"(?:HWDecode|HardwareDecode)"\s*:\s*(?!0\b)-?\d+|"Decode"\s*:\s*true|using full QSV|QSV hardware decode and QSV hardware encode|decoder:\s*(?:qsv\s+)?(?:h264|hevc)(?:_qsv)?|(?:h264|hevc)_qsv-decoder)',
    re.IGNORECASE,
)
QSV_DECODE_FALLBACK_RE = re.compile(
    r'(?:"Decode"\s*:\s*false|(?:qsv[_ -])?decoder[^\n]*(?:failed|error)|Hardware decode:\s*software fallback)',
    re.IGNORECASE,
)


def _now_ts() -> float:
    return float(time.time())


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _transfer_download_timeout() -> int:
    return _bounded_env_int(
        "TSD_WORKER_TRANSFER_TIMEOUT_SECONDS",
        300,
        30,
        1800,
    )


def _transfer_download_attempts() -> int:
    return _bounded_env_int("TSD_WORKER_TRANSFER_ATTEMPTS", 3, 1, 8)


def _job_log_path(job_id: str) -> str:
    safe_id = str(job_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", safe_id):
        raise ValueError("invalid job id")
    return os.path.join(LOG_DIR, f"{safe_id}.log")


def _append_job_log(job_id: str, message: str) -> None:
    """Persist a UTF-8 diagnostic and keep the same tail in job state."""
    text = str(message or "")
    if not text:
        return
    if not text.endswith("\n"):
        text += "\n"
    os.makedirs(LOG_DIR, exist_ok=True)
    try:
        with open(
            _job_log_path(job_id),
            "a",
            encoding="utf-8",
            errors="replace",
        ) as handle:
            handle.write(text)
            handle.flush()
    except OSError as exc:
        print(f"[JOB {job_id}] could not persist log: {exc}", flush=True)
    job = jobs.get(job_id)
    if isinstance(job, dict):
        job["log"] = (str(job.get("log") or "") + text)[-JOB_LOG_TAIL_CHARS:]


def read_job_log(job_id: str, *, max_bytes: int = JOB_LOG_API_MAX_BYTES) -> tuple[str, bool]:
    """Return a decoded tail of a job log and whether earlier bytes were omitted."""
    path = _job_log_path(job_id)
    limit = max(1024, min(16 * 1024 * 1024, int(max_bytes or JOB_LOG_API_MAX_BYTES)))
    try:
        size = int(os.path.getsize(path))
        with open(path, "rb") as handle:
            truncated = size > limit
            if truncated:
                handle.seek(max(0, size - limit))
            payload = handle.read(limit)
    except FileNotFoundError:
        job = jobs.get(str(job_id or "")) or {}
        return str(job.get("log") or ""), False
    return payload.decode("utf-8", errors="replace"), truncated


def _job_error_excerpt(job: dict, fallback: str = "") -> str:
    lines = [line.strip() for line in str(job.get("log") or "").splitlines() if line.strip()]
    preferred = [
        line
        for line in lines
        if any(
            marker in line.lower()
            for marker in ("error", "failed", "invalid", "incompatible", "unsupported", "not available")
        )
    ]
    meaningful = [
        line
        for line in preferred
        if "encode failed, output file was not created" not in line.lower()
        and not line.lower().startswith("[bytesqueeze] error: handbrake exited")
    ]
    value = (meaningful or preferred or lines or [str(fallback or "encode failed")])[-1]
    return value[:500]


def _valid_http_base_url(value: str) -> str:
    candidate = str(value or "").strip().rstrip("/")
    try:
        parsed = urlparse(candidate)
    except Exception:
        return ""
    return candidate if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _rebase_transfer_url(value: str, base_url: str) -> str:
    original = str(value or "").strip()
    if not original:
        return ""
    base = _valid_http_base_url(base_url)
    if not base:
        return original
    parsed_value = urlparse(original)
    if not parsed_value.path:
        return original
    parsed_base = urlparse(base)
    return parsed_value._replace(
        scheme=parsed_base.scheme,
        netloc=parsed_base.netloc,
    ).geturl()


def _apply_observed_controller_route(transfer: dict) -> dict:
    """Prefer the controller address observed by the worker over a browser-only URL."""
    clean = transfer.copy() if isinstance(transfer, dict) else {}
    try:
        from .node_linking import trusted_controller, trusted_controller_by_url

        controller_id = str(clean.get("controller_id") or "").strip()
        controller = trusted_controller(controller_id) if controller_id else None
        if not controller:
            controller = trusted_controller_by_url(clean.get("controller_url") or "")
    except Exception:
        controller = None
    if not isinstance(controller, dict):
        return clean
    route = _valid_http_base_url(
        controller.get("observed_url") or controller.get("url") or ""
    )
    if not route:
        return clean
    clean["controller_url"] = route
    clean["source_url"] = _rebase_transfer_url(clean.get("source_url") or "", route)
    clean["upload_url"] = _rebase_transfer_url(clean.get("upload_url") or "", route)
    return clean


def _remote_transfer_temp_root(transfer: dict | None = None) -> str:
    transfer = transfer if isinstance(transfer, dict) else {}
    value = str(transfer.get("remote_temp_dir") or "").strip()
    if not value:
        try:
            value = str(load_settings().get("remote_transfer_temp_dir") or "").strip()
        except Exception:
            value = ""
    if not value:
        value = TRANSFER_WORK_DIR
    return os.path.abspath(os.path.expanduser(value))


def _empty_dashboard_totals() -> dict[str, float | int]:
    return {
        "done": 0,
        "error": 0,
        "canceled": 0,
        "saved_bytes": 0,
        "runtime_seconds": 0.0,
        "done_runtime_seconds": 0.0,
        "error_runtime_seconds": 0.0,
        "canceled_runtime_seconds": 0.0,
    }


def _normalize_dashboard_totals(value) -> dict[str, float | int]:
    raw = value if isinstance(value, dict) else {}
    totals = _empty_dashboard_totals()
    for key in ("done", "error", "canceled", "saved_bytes"):
        try:
            totals[key] = max(0, int(raw.get(key) or 0))
        except Exception:
            totals[key] = 0
    try:
        totals["runtime_seconds"] = max(0.0, float(raw.get("runtime_seconds") or 0.0))
    except Exception:
        totals["runtime_seconds"] = 0.0
    for key in ("done_runtime_seconds", "error_runtime_seconds", "canceled_runtime_seconds"):
        try:
            totals[key] = max(0.0, float(raw.get(key) or 0.0))
        except Exception:
            totals[key] = 0.0
    return totals

# Regex to parse HandBrakeCLI progress lines:
# e.g. "Encoding: task 1 of 1, 42.34 % (118.19 fps, avg 118.40 fps, ETA 00h02m34s)"
PROGRESS_RE = re.compile(
    r"Encoding:\s+task\s+\d+\s+of\s+\d+,\s*([\d\.]+)\s*%"
)

# Regex to extract the ETA substring from a line.
# It is intentionally loose: it just grabs the token after "ETA",
# e.g. "00h02m34s", "03m21s", "00:03:21"
ETA_RE = re.compile(r"ETA\s+([0-9hms:]+)")
HDR_PATH_RE = re.compile(
    r"(?:^|[ ._\-\[\(])(?:"
    r"hdr(?:10(?:[ ._\-]*(?:plus|\+))?)?|hdr10plus|hdr10\+|hlg|"
    r"dolby[ ._\-]*vision|dovi|dvhe|dvh1|dv|"
    r"bt[ ._\-]?2020|rec[ ._\-]?2020"
    r")(?=$|[ ._\-\]\)\+])",
    re.IGNORECASE,
)
HDR_SIZE_HINT_RE = re.compile(r"(?:^|[ ._\-\[\(])(?:2160p|4320p|4k|8k|uhd)(?=$|[ ._\-\]\)])", re.IGNORECASE)
HDR_REMUX_HINT_RE = re.compile(r"(?:^|[ ._\-\[\(])(?:remux|uhd[ ._\-]*blu[ ._\-]*ray|uhd[ ._\-]*bd)(?=$|[ ._\-\]\)])", re.IGNORECASE)
HDR_VIDEO_HINT_RE = re.compile(
    r"(?:^|[ ._\-\[\(])(?:hevc|x265|h[ ._\-]*265|main[ ._\-]*10|10[ ._\-]*bit)(?=$|[ ._\-\]\)])",
    re.IGNORECASE,
)
HDR_TENBIT_HINT_RE = re.compile(
    r"(?:^|[ ._\-\[\(])(?:main[ ._\-]*10|10[ ._\-]*bit)(?=$|[ ._\-\]\)])",
    re.IGNORECASE,
)
HDR_AUDIO_HINT_RE = re.compile(
    r"(?:^|[ ._\-\[\(])(?:ddp(?:[ ._\-]*[257]\.?1)?|dd\+|e[ ._\-]*ac3|eac3|atmos|truehd)(?=$|[ ._\-\]\)])",
    re.IGNORECASE,
)


# -------------------------------------------------------------------
# Path helper
# -------------------------------------------------------------------

def is_allowed_path(path: str) -> bool:
    """
    Make sure a given path is under one of the allowed root directories.

    This is a safety check to prevent the UI from browsing / encoding
    files outside of the configured media roots.

    Args:
        path (str): Absolute path to check.

    Returns:
        bool: True if the real path starts with any ALLOWED_PREFIXES.
    """
    real = os.path.realpath(path)
    for prefix in ALLOWED_PREFIXES:
        if real.startswith(os.path.realpath(prefix)):
            return True
    return False


# -------------------------------------------------------------------
# ETA parsing helper
# -------------------------------------------------------------------

def _parse_eta_to_seconds(eta_str: str) -> float | None:
    """
    Convert an ETA string from HandBrake into seconds.

    Supports formats like:
      - "00h02m34s"
      - "03m21s"
      - "00:03:21"
      - "03:21"

    Returns:
        float | None: total seconds, or None if we can't parse.
    """
    if not eta_str:
        return None

    s = eta_str.strip()

    # Case 1: colon-separated formats "HH:MM:SS" or "MM:SS"
    if ":" in s:
        parts = s.split(":")
        try:
            if len(parts) == 3:
                h, m, sec = [int(p) for p in parts]
            elif len(parts) == 2:
                h = 0
                m, sec = [int(p) for p in parts]
            else:
                return None
        except ValueError:
            return None
        return float(h * 3600 + m * 60 + sec)

    # Case 2: letter-based formats like "01h23m45s" or "03m21s"
    # Grab all "<number><unit>" pairs.
    matches = re.findall(r"(\d+)([hms])", s)
    if not matches:
        return None

    total = 0
    for value, unit in matches:
        try:
            v = int(value)
        except ValueError:
            continue
        if unit == "h":
            total += v * 3600
        elif unit == "m":
            total += v * 60
        elif unit == "s":
            total += v

    return float(total) if total > 0 else None


def _looks_like_hdr_path(path: str) -> bool:
    """Fast metadata hint used for job history and predictions."""
    text = f"{os.path.basename(path or '')} {path or ''}"
    if HDR_PATH_RE.search(text):
        return True

    has_size = bool(HDR_SIZE_HINT_RE.search(text))
    has_remux = bool(HDR_REMUX_HINT_RE.search(text))
    has_video = bool(HDR_VIDEO_HINT_RE.search(text))
    has_tenbit = bool(HDR_TENBIT_HINT_RE.search(text))
    has_audio = bool(HDR_AUDIO_HINT_RE.search(text))

    if has_size and (has_remux or has_video or has_audio):
        return True
    return bool(has_remux and has_tenbit)


def _encoded_output_is_valid(path: str) -> tuple[bool, str]:
    """Return True only when the output exists, has bytes, and ffprobe can read it."""
    if not path or not os.path.isfile(path):
        return False, "output file missing"

    try:
        if int(os.path.getsize(path)) <= 0:
            return False, "output file is empty"
    except Exception as e:
        return False, f"could not read output size: {e}"

    try:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
            **background_process_options(),
        )
    except FileNotFoundError:
        return True, "ffprobe unavailable; size check passed"
    except subprocess.TimeoutExpired:
        return False, "ffprobe timed out"
    except Exception as e:
        return False, f"ffprobe failed: {e}"

    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout or "").strip()
        return False, detail[:240] or "ffprobe could not read output"

    try:
        duration = float((probe.stdout or "").strip() or 0.0)
    except Exception:
        duration = 0.0
    if duration <= 0:
        return False, "output duration is missing"
    return True, "ok"


def _safe_transfer_filename(name: str, fallback: str = "source.mkv") -> str:
    value = os.path.basename(str(name or "").replace("\\", "/")).strip()
    value = re.sub(r"[^A-Za-z0-9._ -]+", "_", value)
    value = value.strip(" .")
    return value or fallback


def _human_rate(bytes_per_second: float) -> str:
    try:
        value = float(bytes_per_second or 0.0)
    except Exception:
        value = 0.0
    if value <= 0:
        return ""
    mb = value / (1024 ** 2)
    if mb >= 1:
        return f"{mb:.2f} MB/s"
    return f"{max(1, round(value / 1024))} KB/s"


def _transfer_progress_payload(phase: str, transferred: int, total: int, started_at: float) -> dict:
    now = _now_ts()
    elapsed = max(0.001, now - float(started_at or now))
    transferred_i = max(0, int(transferred or 0))
    total_i = max(0, int(total or 0))
    speed = transferred_i / elapsed if transferred_i > 0 else 0.0
    remaining = max(0, total_i - transferred_i) if total_i else 0
    percent = (transferred_i / total_i * 100.0) if total_i else 0.0
    eta = int(round(remaining / speed)) if speed > 0 and remaining > 0 else None
    return {
        "phase": phase,
        "bytes": transferred_i,
        "total_bytes": total_i,
        "remaining_bytes": remaining,
        "percent": round(max(0.0, min(100.0, percent)), 2),
        "speed_bps": round(speed, 2),
        "speed_label": _human_rate(speed),
        "eta_seconds": eta,
        "updated_at": now,
    }


def _normalize_preset_bundle(bundle) -> dict | None:
    if not isinstance(bundle, dict):
        return None
    contents = bundle.get("contents")
    if isinstance(contents, dict):
        contents = json.dumps(contents, indent=2)
    contents = str(contents or "")
    if not contents.strip():
        return None
    try:
        json.loads(contents)
    except Exception:
        return None
    file_name = _safe_transfer_filename(bundle.get("file_name") or bundle.get("filename") or "controller-preset.json", "controller-preset.json")
    if not file_name.lower().endswith(".json"):
        file_name += ".json"
    return {
        "key": str(bundle.get("key") or "").strip()[:32],
        "file_name": file_name,
        "name": str(bundle.get("name") or "").strip()[:160],
        "contents": contents,
        "source": "controller",
    }


def snapshot_preset_bundle(preset_key: str) -> dict | None:
    """Capture the exact configured preset so queued work cannot drift later."""
    key = str(preset_key or "1080").strip().lower()
    if key not in {"1080", "4k"}:
        key = "1080"
    try:
        file_path, preset_name = resolve_preset_file_and_name(key)
        with open(file_path, "r", encoding="utf-8") as handle:
            contents = handle.read()
        return _normalize_preset_bundle({
            "key": key,
            "file_name": os.path.basename(file_path),
            "name": preset_name,
            "contents": contents,
        })
    except Exception as exc:
        print(f"[WARN] Could not snapshot queued preset {key}: {exc}", flush=True)
        return None


def _materialize_job_preset(job_id: str, bundle) -> tuple[str, str, str] | None:
    clean = _normalize_preset_bundle(bundle)
    if not clean:
        return None
    work_dir = os.path.join(PRESET_WORK_DIR, str(job_id))
    os.makedirs(work_dir, exist_ok=True)
    path = os.path.join(work_dir, clean["file_name"])
    with open(path, "w", encoding="utf-8") as f:
        f.write(clean["contents"])
    return path, clean.get("name") or "", work_dir


def _cleanup_job_preset_dir(path: str) -> None:
    if not path:
        return
    try:
        real = os.path.realpath(path)
        root = os.path.realpath(PRESET_WORK_DIR)
        if os.path.commonpath([real, root]) == root and os.path.isdir(real):
            shutil.rmtree(real, ignore_errors=True)
    except Exception:
        pass


def _maybe_update_output_estimate(job: dict, out_path: str, progress: float) -> bool:
    try:
        pct = float(progress or 0.0)
    except Exception:
        return False
    if pct <= 0 or pct >= 100 or not out_path:
        return False

    seen = job.get("estimate_checkpoints_seen")
    if not isinstance(seen, list):
        seen = []
    eligible = [point for point in OUTPUT_ESTIMATE_CHECKPOINTS if pct >= point and point not in seen]
    next_checkpoint = max(eligible) if eligible else None
    if next_checkpoint is None:
        return False

    try:
        current_bytes = int(os.path.getsize(out_path))
    except Exception:
        # HandBrake may not have created/opened the output yet. Do not consume
        # the checkpoint: the next progress update should retry it.
        return False
    if current_bytes <= 0:
        return False

    seen.append(next_checkpoint)
    job["estimate_checkpoints_seen"] = seen
    estimated = int(round(current_bytes / max(0.01, pct / 100.0)))
    job["estimated_out_bytes"] = estimated
    job["estimated_out_current_bytes"] = current_bytes
    job["estimated_out_checked_progress"] = round(pct, 2)
    job["estimated_out_updated_at"] = _now_ts()
    job["estimated_out_source"] = "live"
    return True


def _normalize_encoding_policy(policy: dict | None = None) -> dict:
    """Return only the worker-safe settings that can affect an encode."""
    source = policy if isinstance(policy, dict) else {}
    if not source:
        try:
            source = load_settings()
        except Exception:
            source = {}

    try:
        hb_threads = max(0, min(256, int(source.get("hb_threads") or 0)))
    except (TypeError, ValueError):
        hb_threads = 0
    try:
        stop_percent = float(source.get("auto_stop_large_output_percent") or 90.0)
    except (TypeError, ValueError):
        stop_percent = 90.0
    try:
        hardware_concurrency = int(source.get("hardware_transcode_concurrency") or 1)
    except (TypeError, ValueError):
        hardware_concurrency = 1
    output_container = normalize_output_container(source.get("output_container"))
    web_optimized_value = source.get("web_optimized", False)
    if isinstance(web_optimized_value, str):
        web_optimized_value = web_optimized_value.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    try:
        audio_target_bitrate = max(
            640, min(2048, int(source.get("audio_optimize_bitrate_kbps") or 1024))
        )
    except (TypeError, ValueError):
        audio_target_bitrate = 1024

    return {
        "hb_threads": hb_threads,
        "hardware_transcode_concurrency": max(1, min(8, hardware_concurrency)),
        "auto_stop_large_output_enabled": bool(source.get("auto_stop_large_output_enabled", False)),
        "auto_stop_large_output_percent": round(max(1.0, min(500.0, stop_percent)), 1),
        "output_container": output_container,
        "web_optimized": bool(output_container == "mp4" and web_optimized_value),
        "audio_policy_default": str(source.get("audio_policy_default") or "preserve"),
        "audio_optimize_codec": str(source.get("audio_optimize_codec") or "aac"),
        "audio_optimize_bitrate_kbps": audio_target_bitrate,
        "audio_deduplicate_languages": bool(source.get("audio_deduplicate_languages", True)),
        "audio_preferred_languages": list(source.get("audio_preferred_languages") or ["eng", "spa"]),
        "audio_allow_downmix": bool(source.get("audio_allow_downmix", False)),
        "audio_allow_object_metadata_loss": bool(source.get("audio_allow_object_metadata_loss", False)),
    }


def _job_encoding_policy(job: dict | None) -> dict:
    job = job if isinstance(job, dict) else {}
    stored = job.get("encoding_policy")
    return _normalize_encoding_policy(stored if isinstance(stored, dict) else None)


def _normalized_copy_codec(value: str) -> str:
    codec = str(value or "").strip().lower().replace("copy:", "")
    aliases = {
        "dca": "dts",
        "dts-hd": "dtshd",
        "dts_hd": "dtshd",
        "ac-3": "ac3",
        "e-ac-3": "eac3",
        "mp4a": "aac",
    }
    return aliases.get(codec, codec)


def _preset_audio_encoders(job: dict | None, preset_definition: dict | None) -> list[str]:
    """Return the effective HandBrake audio encoders for container planning."""
    operations = (job or {}).get("operations") if isinstance((job or {}).get("operations"), dict) else {}
    actions = operations.get("audio_actions") if isinstance(operations.get("audio_actions"), list) else []
    if actions:
        planned = []
        for row in actions:
            if not isinstance(row, dict) or row.get("action") == "remove":
                continue
            if row.get("action") == "copy":
                planned.append(f"copy:{str(row.get('source_codec') or '').strip().lower()}")
            else:
                planned.append(str(row.get("target_codec") or "aac").strip().lower())
        return planned
    args = _split_extra_args((job or {}).get("extra_args") or "")
    override = _argument_value(args, "--aencoder", "-E")
    if override:
        return [part.strip().lower() for part in override.split(",") if part.strip()]
    preset = preset_definition if isinstance(preset_definition, dict) else {}
    if str(preset.get("AudioTrackSelectionBehavior") or "").strip().lower() == "none":
        return []
    rows = preset.get("AudioList") if isinstance(preset.get("AudioList"), list) else []
    return [
        str(row.get("AudioEncoder") or row.get("PresetEncoder") or "").strip().lower()
        for row in rows
        if isinstance(row, dict)
        and str(row.get("AudioEncoder") or row.get("PresetEncoder") or "").strip()
    ]


def _audio_passthrough_codecs(job: dict | None, preset_definition: dict | None) -> set[str]:
    """Return source codecs which the effective audio plan may pass through."""
    encoders = _preset_audio_encoders(job, preset_definition)
    generic_copy = any(encoder == "copy" for encoder in encoders)
    direct = {
        _normalized_copy_codec(encoder)
        for encoder in encoders
        if encoder.startswith("copy:")
    }
    if not generic_copy:
        return {codec for codec in direct if codec}

    args = _split_extra_args((job or {}).get("extra_args") or "")
    mask_value = _argument_value(args, "--audio-copy-mask")
    if mask_value:
        mask = mask_value.split(",")
    else:
        preset = preset_definition if isinstance(preset_definition, dict) else {}
        configured = preset.get("AudioCopyMask")
        mask = configured if isinstance(configured, list) else []
    normalized = {
        _normalized_copy_codec(codec)
        for codec in mask
        if _normalized_copy_codec(codec)
    }
    # A generic copy encoder without a mask asks HandBrake to copy any source
    # it can and use its fallback otherwise. Treat that as unknown passthrough
    # so Auto chooses the compatibility-first container.
    return direct | (normalized or {"*"})


def _selected_audio_streams(
    source: dict | None,
    job: dict | None,
    preset_definition: dict | None,
) -> list[dict]:
    streams = [
        row for row in (source or {}).get("streams", [])
        if isinstance(row, dict) and row.get("type") == "audio"
    ]
    if not streams:
        return []
    operations = (job or {}).get("operations") if isinstance((job or {}).get("operations"), dict) else {}
    actions = operations.get("audio_actions") if isinstance(operations.get("audio_actions"), list) else []
    if actions:
        selected_indexes = {
            int(row.get("stream_index"))
            for row in actions
            if isinstance(row, dict)
            and row.get("action") != "remove"
            and str(row.get("stream_index", "")).lstrip("-").isdigit()
        }
        return [row for row in streams if int(row.get("index") or 0) in selected_indexes]
    args = _split_extra_args((job or {}).get("extra_args") or "")
    lowered = [str(arg or "").strip().lower() for arg in args]
    audio_value = _argument_value(args, "--audio", "-a").strip().lower()
    if audio_value in {"none", "0"}:
        return []
    if "--all-audio" in lowered or audio_value:
        return streams

    preset = preset_definition if isinstance(preset_definition, dict) else {}
    behavior = str(preset.get("AudioTrackSelectionBehavior") or "none").strip().lower()
    if behavior == "none":
        return []
    languages = {
        str(value or "").strip().lower()
        for value in (preset.get("AudioLanguageList") or [])
        if str(value or "").strip()
    }
    if not languages or languages & {"any", "*", "und"}:
        candidates = streams
    else:
        candidates = [
            row for row in streams
            if str(row.get("language") or "und").strip().lower() in languages
        ]
    return candidates[:1] if behavior == "first" else candidates


def _encoded_audio_codec(encoder: str) -> str:
    value = str(encoder or "").strip().lower()
    if not value or value == "none" or value.startswith("copy"):
        return ""
    for marker, codec in (
        ("eac3", "eac3"),
        ("ac3", "ac3"),
        ("aac", "aac"),
        ("mp3", "mp3"),
        ("lame", "mp3"),
        ("truehd", "truehd"),
        ("opus", "opus"),
        ("flac", "flac"),
        ("alac", "alac"),
        ("vorbis", "vorbis"),
    ):
        if marker in value:
            return codec
    return value


def _selected_subtitle_streams(
    source: dict | None,
    job: dict | None,
    preset_definition: dict | None,
) -> list[dict]:
    streams = [
        row for row in (source or {}).get("streams", [])
        if isinstance(row, dict) and row.get("type") == "subtitle"
    ]
    if not streams:
        return []
    args = _split_extra_args((job or {}).get("extra_args") or "")
    lowered = [str(arg or "").strip().lower() for arg in args]
    subtitle_value = _argument_value(args, "--subtitle", "-s").strip().lower()
    if subtitle_value in {"none", "0"}:
        return []
    if "--all-subtitles" in lowered:
        return streams

    preset = preset_definition if isinstance(preset_definition, dict) else {}
    behavior = str(preset.get("SubtitleTrackSelectionBehavior") or "none").strip().lower()
    if behavior == "none" and not subtitle_value:
        return []
    if behavior in {"all", "first"} or subtitle_value:
        # An explicit track-number expression is difficult to map reliably to
        # ffprobe's stream indexes. Checking all subtitle tracks is the safe
        # choice and avoids silently selecting MP4 for a bitmap track.
        return streams

    languages = {
        str(value or "").strip().lower()
        for value in (preset.get("SubtitleLanguageList") or [])
        if str(value or "").strip()
    }
    if not languages:
        return []
    if languages & {"any", "*", "und"}:
        return streams
    return [
        row for row in streams
        if str(row.get("language") or "und").strip().lower() in languages
    ]


def _auto_output_container(
    source: dict | None,
    job: dict | None = None,
    preset_definition: dict | None = None,
    selected_encoder: str = "",
) -> tuple[str, str]:
    """Choose the safest container for the tracks this encode can preserve."""
    source = source if isinstance(source, dict) else {}
    if source.get("error"):
        return "mkv", f"source tracks could not be inspected: {source.get('error')}"
    streams = [row for row in source.get("streams", []) if isinstance(row, dict)]
    if not streams:
        return "mkv", "source track inventory is unavailable"

    encoder = str(selected_encoder or "").strip().lower()
    mp4_video_markers = ("h264", "x264", "h265", "hevc", "x265", "av1", "mpeg2", "mpeg4", "vp9")
    if not encoder:
        return "mkv", "the selected video encoder could not be identified"
    if not any(marker in encoder for marker in mp4_video_markers):
        return "mkv", f"video encoder {encoder} is not in the MP4-safe set"

    mp4_audio = {"aac", "mp3", "ac3", "eac3", "truehd", "opus", "flac", "alac"}
    for encoder_name in _preset_audio_encoders(job, preset_definition):
        encoded_codec = _encoded_audio_codec(encoder_name)
        if encoded_codec and encoded_codec not in mp4_audio:
            return "mkv", f"audio encoder {encoder_name} produces {encoded_codec.upper()}, which is not MP4-safe"

    passthrough = _audio_passthrough_codecs(job, preset_definition)
    for stream in _selected_audio_streams(source, job, preset_definition):
        codec = _normalized_copy_codec(stream.get("codec") or "")
        profile = str(stream.get("profile") or "").strip().lower()
        aliases = {codec}
        if codec == "dts" and any(marker in profile for marker in ("dts-hd", "dts hd", "master audio", "ma")):
            aliases.add("dtshd")
        copied = "*" in passthrough or bool(aliases & passthrough)
        if not copied or codec in mp4_audio:
            continue
        if "dtshd" in aliases:
            label = "DTS-HD"
        elif codec == "dts":
            label = "DTS"
        else:
            label = (codec or "unknown").upper()
        return "mkv", f"{label} audio is configured for passthrough"

    subtitle_labels = {
        "hdmv_pgs_subtitle": "PGS bitmap",
        "dvd_subtitle": "VobSub bitmap",
        "dvb_subtitle": "DVB bitmap",
        "xsub": "XSUB bitmap",
        "ass": "styled ASS",
        "ssa": "styled SSA",
    }
    mp4_text_subtitles = {
        "subrip", "srt", "mov_text", "text", "webvtt", "eia_608",
        "eia_708", "cea_608", "cea_708", "closed_caption", "ttml",
    }
    for stream in _selected_subtitle_streams(source, job, preset_definition):
        codec = str(stream.get("codec") or "").strip().lower()
        if codec in mp4_text_subtitles:
            continue
        label = subtitle_labels.get(codec, (codec or "unknown").upper())
        return "mkv", f"selected {label} subtitle is safer to preserve in MKV"

    return "mp4", "all selected video, audio, and subtitle tracks are MP4-compatible"


def _output_container_plan(
    policy: dict | None = None,
    source: dict | None = None,
    *,
    job: dict | None = None,
    preset_definition: dict | None = None,
    selected_encoder: str = "",
) -> dict:
    """Build the controlled HandBrake muxer options and filename extension."""
    normalized = _normalize_encoding_policy(policy)
    requested_container = normalized["output_container"]
    if requested_container == "auto":
        container, reason = _auto_output_container(
            source,
            job,
            preset_definition,
            selected_encoder,
        )
    else:
        container = requested_container
        reason = f"{container.upper()} selected in Settings"
    web_optimized = bool(container == "mp4" and normalized.get("web_optimized"))
    cli_args = ["--format", "av_mp4" if container == "mp4" else "av_mkv"]
    if web_optimized:
        cli_args.append("--optimize")
    return {
        "requested_container": requested_container,
        "container": container,
        "extension": ".mp4" if container == "mp4" else ".mkv",
        "web_optimized": web_optimized,
        "reason": reason,
        "cli_args": cli_args,
    }


def _output_path_for_source(
    src: str,
    suffix: str,
    policy: dict | None = None,
    *,
    plan: dict | None = None,
) -> str:
    plan = plan if isinstance(plan, dict) else _output_container_plan(policy)
    folder = os.path.dirname(src)
    name = os.path.splitext(os.path.basename(src))[0]
    return os.path.join(folder, f"{name}-{suffix}{plan['extension']}")


def _estimated_output_stop_guard(job: dict) -> dict | None:
    """Return stop details when a reliable checkpoint crosses the configured limit."""
    if job.get("auto_stop_triggered"):
        return None
    policy = _job_encoding_policy(job)
    if not policy.get("auto_stop_large_output_enabled", False):
        return None
    try:
        checked_progress = float(job.get("estimated_out_checked_progress") or 0.0)
        estimated = int(job.get("estimated_out_bytes") or 0)
        source = int(job.get("src_bytes") or 0)
        threshold = float(policy.get("auto_stop_large_output_percent") or 90.0)
    except (TypeError, ValueError):
        return None
    # The 2% checkpoint is useful for display, but too noisy for termination.
    if checked_progress < 10.0 or estimated <= 0 or source <= 0:
        return None
    ratio = estimated / float(source) * 100.0
    if ratio < threshold:
        return None
    return {
        "ratio_percent": round(ratio, 1),
        "threshold_percent": round(threshold, 1),
        "estimated_bytes": estimated,
        "source_bytes": source,
        "checked_progress": round(checked_progress, 1),
    }


def _download_transfer_source(
    url: str,
    token: str,
    worker_node_id: str,
    destination: str,
    expected_size: int = 0,
    progress_callback=None,
    attempt_callback=None,
) -> int:
    if not url or not token:
        raise RuntimeError("transfer download is missing URL or token")
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    part_path = destination + ".part"
    attempts = _transfer_download_attempts()
    timeout = _transfer_download_timeout()
    last_error = ""

    for attempt in range(1, attempts + 1):
        req = Request(
            url,
            method="GET",
            headers={
                "X-Transfer-Token": token,
                "X-Worker-Node-Id": str(worker_node_id or ""),
            },
        )
        if attempt_callback:
            attempt_callback(
                f"Source download attempt {attempt}/{attempts} "
                f"(socket timeout {timeout}s)."
            )
        try:
            with urlopen(req, timeout=timeout) as res, open(part_path, "wb") as f:
                transferred = 0
                header_size = 0
                try:
                    header_size = int(res.headers.get("Content-Length") or 0)
                except Exception:
                    header_size = 0
                total = int(expected_size or header_size or 0)
                if progress_callback:
                    progress_callback("downloading", 0, total, force=True)
                while True:
                    chunk = res.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    transferred += len(chunk)
                    if progress_callback:
                        progress_callback("downloading", transferred, total)
            downloaded_size = int(os.path.getsize(part_path))
            if expected_size and downloaded_size != int(expected_size):
                last_error = (
                    "downloaded source size mismatch "
                    f"({downloaded_size} != {expected_size})"
                )
                try:
                    os.remove(part_path)
                except FileNotFoundError:
                    pass
                if attempt >= attempts:
                    raise RuntimeError(
                        f"source download failed after {attempts} attempts: {last_error}"
                    )
                if attempt_callback:
                    attempt_callback(f"Download interrupted: {last_error}. Retrying...")
                time.sleep(min(5, attempt))
                continue
            break
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                detail = str(exc)
            last_error = detail[:500] or str(exc)
            # Authentication and validation failures will not improve with a
            # socket retry. Surface them immediately with their response text.
            if int(getattr(exc, "code", 500) or 500) < 500:
                raise RuntimeError(last_error)
        except (URLError, TimeoutError, OSError) as exc:
            last_error = str(getattr(exc, "reason", exc) or exc)

        if attempt >= attempts:
            raise RuntimeError(
                f"source download failed after {attempts} attempts: {last_error}"
            )
        if attempt_callback:
            attempt_callback(f"Download interrupted: {last_error}. Retrying...")
        time.sleep(min(5, attempt))

    size = int(os.path.getsize(part_path))
    if progress_callback:
        progress_callback("downloaded", size, int(expected_size or size), force=True)
    os.replace(part_path, destination)
    return size


def _upload_transfer_output(url: str, token: str, worker_node_id: str, out_path: str, *, job_id: str, duration_seconds: float | None, progress_callback=None) -> dict:
    if not url or not token:
        raise RuntimeError("transfer upload is missing URL or token")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("invalid transfer upload URL")
    size = int(os.path.getsize(out_path))
    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    conn = conn_cls(parsed.netloc, timeout=60)
    try:
        conn.putrequest("POST", target)
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(size))
        conn.putheader("X-Transfer-Token", token)
        conn.putheader("X-Worker-Node-Id", str(worker_node_id or ""))
        conn.putheader("X-Worker-Job-Id", str(job_id or ""))
        conn.putheader("X-Output-Filename", os.path.basename(out_path))
        job = jobs.get(job_id) or {}
        selected_container = str(job.get("output_container") or "").strip().lower()
        if selected_container in {"mp4", "mkv"}:
            conn.putheader("X-Output-Container", selected_container)
        container_reason = str(job.get("output_container_reason") or "").strip()
        if container_reason:
            safe_reason = container_reason.encode("ascii", errors="replace").decode("ascii")
            conn.putheader("X-Output-Container-Reason", safe_reason[:240])
        for header, key in (
            ("X-Encode-Method", "encode_method"),
            ("X-Encode-Encoder", "encoder"),
            ("X-Encode-Video-Codec", "video_codec"),
            ("X-Encode-Encoder-Family", "encoder_family"),
            ("X-Encode-Bit-Depth", "bit_depth"),
        ):
            value = str(job.get(key) or "").strip()
            if value:
                conn.putheader(header, value[:120])
        if duration_seconds is not None:
            conn.putheader("X-Encode-Duration-Seconds", str(round(float(duration_seconds), 3)))
        conn.endheaders()
        with open(out_path, "rb") as f:
            sent = 0
            if progress_callback:
                progress_callback("uploading", 0, size, force=True)
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                conn.send(chunk)
                sent += len(chunk)
                if progress_callback:
                    progress_callback("uploading", sent, size)
        if progress_callback:
            progress_callback("uploaded", size, size, force=True)
        res = conn.getresponse()
        body = res.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body or "{}")
        except Exception:
            payload = {"error": body[:240]}
        if res.status >= 400 or not payload.get("ok"):
            raise RuntimeError(payload.get("error") or f"transfer upload failed ({res.status})")
        return payload if isinstance(payload, dict) else {}
    finally:
        conn.close()


def _cleanup_transfer_work_dir(path: str, transfer: dict | None = None) -> None:
    if not path:
        return
    try:
        real = os.path.realpath(path)
        root = os.path.realpath(_remote_transfer_temp_root(transfer))
        if os.path.commonpath([real, root]) == root and os.path.isdir(real):
            shutil.rmtree(real, ignore_errors=True)
    except Exception:
        pass


def _remote_transfer_public(transfer: dict | None) -> dict:
    transfer = transfer if isinstance(transfer, dict) else {}
    return {
        "id": transfer.get("id") or transfer.get("transfer_id") or "",
        "controller_url": transfer.get("controller_url") or "",
        "original_path": transfer.get("original_path") or "",
        "source_basename": transfer.get("source_basename") or "",
        "source_size": transfer.get("source_size") or 0,
        "status": transfer.get("status") or "",
        "retry_count": int(transfer.get("retry_count") or 0),
        "next_retry_at": float(transfer.get("next_retry_at") or 0),
        "last_error": transfer.get("last_error") or transfer.get("error") or "",
        "remote_temp_dir": transfer.get("remote_temp_dir") or "",
        "progress": transfer.get("progress") if isinstance(transfer.get("progress"), dict) else {},
    }


def _transfer_retry_delay(retry_count: int) -> int:
    count = max(1, int(retry_count or 1))
    return min(TRANSFER_RETRY_MAX_SECONDS, TRANSFER_RETRY_MIN_SECONDS * (2 ** min(count - 1, 7)))


def _renew_transfer_upload_grant(job_id: str, transfer: dict) -> dict:
    controller_id = str(transfer.get("controller_id") or "").strip()
    transfer_id = str(transfer.get("id") or "").strip()
    if not transfer_id:
        raise RuntimeError("transfer is missing its durable transfer identity")
    from .node_linking import signed_json_request, trusted_controller, trusted_controller_by_url

    controller = trusted_controller(controller_id) if controller_id else trusted_controller_by_url(transfer.get("controller_url") or "")
    if not controller:
        raise RuntimeError("paired controller record is unavailable")
    if not controller_id:
        controller_id = str(controller.get("id") or "")
        transfer["controller_id"] = controller_id
    api_path = f"/api/node/transfers/{transfer_id}/renew-upload"
    result = signed_json_request(
        controller,
        api_path,
        method="POST",
        body={"job_id": job_id},
        timeout=15,
    )
    if result.get("complete"):
        return result
    token = str(result.get("upload_token") or "").strip()
    if not token:
        raise RuntimeError("controller did not return a renewed upload token")
    transfer["upload_token"] = token
    transfer["upload_url"] = str(result.get("upload_url") or transfer.get("upload_url") or "").strip()
    transfer["expires_at"] = float(result.get("expires_at") or transfer.get("expires_at") or 0)
    return result


def _mark_transfer_waiting(job_id: str, job: dict, error: Exception | str) -> None:
    transfer = job.get("transfer") if isinstance(job.get("transfer"), dict) else {}
    retry_count = int(transfer.get("retry_count") or 0) + 1
    delay = _transfer_retry_delay(retry_count)
    message = str(error or "controller unavailable")[:300]
    transfer.update({
        "status": "waiting_to_upload",
        "retry_count": retry_count,
        "next_retry_at": _now_ts() + delay,
        "last_attempt_at": _now_ts(),
        "last_error": message,
        "error": "",
        "progress": {
            "phase": "waiting_to_upload",
            "percent": 100.0,
            "bytes": int(job.get("out_bytes") or 0),
            "total_bytes": int(job.get("out_bytes") or 0),
            "remaining_bytes": 0,
            "speed_label": "",
            "updated_at": _now_ts(),
        },
    })
    job["status"] = "waiting_to_upload"
    job["phase"] = "waiting_to_upload"
    job["error_message"] = f"Upload waiting: {message}"[:500]
    job["transfer"] = transfer
    _append_job_log(
        job_id,
        (
            "[ByteSqueeze] Controller unavailable; completed output is safe "
            f"locally. Retrying upload in {delay}s. Error: {message}"
        ),
    )
    print(
        f"[JOB {job_id}] upload deferred for {delay}s: {message}",
        flush=True,
    )
    save_jobs()
    log_event(
        "node_transfer_waiting",
        f"Encode complete; waiting to return output to controller: {os.path.basename(job.get('src') or job_id)}",
        level="warn",
        job_id=job_id,
        src=job.get("src"),
        extra={"retry_in_seconds": delay, "error": message},
    )


def _apply_remote_upload_success(job_id: str, job: dict, result: dict, out_path: str) -> None:
    transfer = job.get("transfer") if isinstance(job.get("transfer"), dict) else {}
    local_bytes = int(os.path.getsize(out_path)) if out_path and os.path.isfile(out_path) else int(job.get("out_bytes") or 0)
    controller_out = result.get("out_path") or job.get("out_path") or out_path
    controller_out_bytes = int(result.get("out_bytes") or local_bytes)
    controller_saved = int(result.get("saved_bytes") or job.get("saved_bytes") or 0)
    job.update({
        "status": "done",
        "phase": "done",
        "error_message": "",
        "progress": 100.0,
        "out_path": controller_out,
        "out_bytes": controller_out_bytes,
        "saved_bytes": controller_saved,
        "estimated_out_bytes": controller_out_bytes,
        "estimated_out_source": "actual",
    })
    if isinstance(result.get("storage_breakdown"), dict):
        job["storage_breakdown"] = result["storage_breakdown"]
    if isinstance(result.get("output_inventory"), dict):
        job["output_inventory"] = result["output_inventory"]
    transfer.update({
        "status": "complete",
        "controller_out_path": controller_out,
        "source_deleted": bool(result.get("source_deleted")),
        "next_retry_at": 0,
        "last_error": "",
        "completed_at": _now_ts(),
    })
    transfer["progress"] = _transfer_progress_payload("complete", controller_out_bytes, controller_out_bytes, _now_ts())
    job["transfer"] = transfer
    log_event(
        "node_transfer_finished",
        f"Remote transfer finished: {os.path.basename(job.get('src') or job_id)} - saved {round(controller_saved/(1024**3), 3)} GB",
        job_id=job_id,
        src=job.get("src"),
        extra={"saved_bytes": controller_saved, "out_path": controller_out, "source_deleted": bool(result.get("source_deleted"))},
    )


def _encoder_method_from_encoder(encoder: str, preset: str = "") -> dict:
    value = str(encoder or "").strip().lower()
    preset_key = str(preset or "auto").strip().lower() or "auto"
    if not value:
        return {
            "encode_method": f"preset:{preset_key}",
            "encoder": "",
            "video_codec": "",
            "encoder_family": "preset",
            "bit_depth": "",
        }

    family = "software"
    if value.startswith("qsv_"):
        family = "qsv"
    elif value.startswith("nvenc_"):
        family = "nvenc"
    elif value.startswith("vce_"):
        family = "vce"
    elif value.startswith("amf_"):
        family = "amf"
    elif value.startswith("vt_") or "videotoolbox" in value:
        family = "videotoolbox"
    elif value.startswith("vaapi_"):
        family = "vaapi"

    codec = ""
    if "av1" in value:
        codec = "av1"
    elif "265" in value or "h265" in value or "hevc" in value:
        codec = "h265"
    elif "264" in value or "h264" in value:
        codec = "h264"
    elif "vp9" in value:
        codec = "vp9"
    elif "vp8" in value:
        codec = "vp8"

    bit_depth = "10" if "10bit" in value or "_10" in value else ("12" if "12bit" in value or "_12" in value else "8")
    return {
        "encode_method": value,
        "encoder": value,
        "video_codec": codec,
        "encoder_family": family,
        "bit_depth": bit_depth,
    }


def _encode_metadata_from_extra_args(extra_args: str = "", preset: str = "", metadata: dict | None = None) -> dict:
    out = _encoder_method_from_encoder("", preset)
    if isinstance(metadata, dict):
        out.update({
            key: str(metadata.get(key) or out.get(key) or "")
            for key in ("encode_method", "encoder", "video_codec", "encoder_family", "bit_depth")
        })

    encoder = str(out.get("encoder") or "").strip()
    if not encoder:
        try:
            parts = shlex.split(str(extra_args or ""))
        except Exception:
            parts = str(extra_args or "").split()
        for idx, part in enumerate(parts):
            if part in {"--encoder", "-e"} and idx + 1 < len(parts):
                encoder = parts[idx + 1]
                break
            if part.startswith("--encoder="):
                encoder = part.split("=", 1)[1]
                break
            if part.startswith("-e="):
                encoder = part.split("=", 1)[1]
                break
    if encoder:
        return _encoder_method_from_encoder(encoder, preset)
    return out


def _queued_preset_display_name(
    preset: str,
    preset_bundle: dict | None = None,
    *,
    extra_args: str = "",
    encode_metadata: dict | None = None,
    preset_selection: str = "",
    preset_adaptive: bool = False,
) -> str:
    """Return the immutable, truthful preset label shown by every client.

    Smart jobs start from a configured HandBrake preset bundle, but their
    episode plan can select a different video encoder, bit depth, and target
    resolution.  The bundle name therefore is not a useful Smart preset name
    and previously made QSV H.265 jobs appear as AV1 jobs in the queue.
    """
    metadata = encode_metadata if isinstance(encode_metadata, dict) else {}
    selection = str(
        preset_selection
        or metadata.get("preset_selection")
        or preset
        or "1080"
    ).strip().lower()
    smart = bool(
        selection == "smart"
        or preset_adaptive
        or metadata.get("preset_adaptive")
        or metadata.get("smart_preset")
    )
    bundle_name = str((preset_bundle or {}).get("name") or "").strip()
    explicit_name = str(metadata.get("queued_preset_name") or "").strip()
    if not smart:
        return (explicit_name or bundle_name or str(preset or "Preset"))[:160]

    method = _encode_metadata_from_extra_args(extra_args, preset, metadata)
    codec = str(method.get("video_codec") or "").strip().lower()
    depth = str(method.get("bit_depth") or "").strip()
    family = str(method.get("encoder_family") or "").strip().lower()

    candidate_id = str(metadata.get("smart_candidate_id") or "").strip().lower()
    candidate_labels = {
        "balanced": "Balanced",
        "compact": "Space Saver",
        "detail": "Detail First",
        "fast": "Fast",
        "archive": "Archive",
        "manual": "Custom",
    }
    candidate = str(metadata.get("smart_candidate_name") or "").strip()
    if not candidate:
        candidate = candidate_labels.get(
            candidate_id,
            candidate_id.replace("_", " ").replace("-", " ").title(),
        )

    codec_label = {
        "h264": "H.264",
        "h265": "H.265",
        "av1": "AV1",
        "vp9": "VP9",
        "vp8": "VP8",
    }.get(codec, codec.upper() if codec else "Video")
    if depth and depth not in {"", "8"}:
        codec_label = f"{codec_label} {depth}-bit"
    family_label = {
        "qsv": "Intel QSV",
        "nvenc": "NVIDIA NVENC",
        "vce": "AMD VCE",
        "amf": "AMD AMF",
        "software": "Software",
        "videotoolbox": "VideoToolbox",
        "vaapi": "VAAPI",
    }.get(family, family.upper() if family and family != "preset" else "")

    episode_plan = metadata.get("smart_episode_plan")
    episode_plan = episode_plan if isinstance(episode_plan, dict) else {}
    target = episode_plan.get("target")
    target = target if isinstance(target, dict) else {}
    try:
        target_height = int(target.get("height") or 0)
    except (TypeError, ValueError):
        target_height = 0
    try:
        target_width = int(target.get("width") or 0)
    except (TypeError, ValueError):
        target_width = 0
    if target_width >= 3000 or target_height >= 2000:
        resolution_label = "4K"
    elif target_height >= 1000:
        resolution_label = "1080p"
    elif target_height >= 700:
        resolution_label = "720p"
    elif target_height:
        resolution_label = f"{target_height}p"
    else:
        resolution_label = "4K" if str(preset or "").strip().lower() == "4k" else "1080p"

    smart_label = f"Smart {candidate}" if candidate else "Smart"
    return " · ".join(
        part for part in (smart_label, codec_label, family_label, resolution_label) if part
    )[:160]


def _job_encode_metadata(job: dict | None) -> dict:
    job = job if isinstance(job, dict) else {}
    return _encode_metadata_from_extra_args(
        job.get("extra_args", ""),
        job.get("preset", ""),
        {
            "encode_method": job.get("encode_method") or "",
            "encoder": job.get("encoder") or "",
            "video_codec": job.get("video_codec") or "",
            "encoder_family": job.get("encoder_family") or "",
            "bit_depth": job.get("bit_depth") or "",
        },
    )


def _preset_video_encoder(job: dict | None) -> str:
    """Read the selected HandBrake preset's encoder without materializing it."""
    job = job if isinstance(job, dict) else {}
    bundle = _normalize_preset_bundle(job.get("preset_bundle"))
    preset_name = ""
    data = None

    try:
        if bundle:
            data = json.loads(bundle["contents"])
            preset_name = str(bundle.get("name") or "").strip()
        else:
            preset_file, preset_name = resolve_preset_file_and_name(job.get("preset") or "1080")
            with open(preset_file, "r", encoding="utf-8") as preset_stream:
                data = json.load(preset_stream)
    except Exception:
        return ""

    candidates = []
    if isinstance(data, dict):
        if data.get("VideoEncoder"):
            candidates.append(data)
        preset_list = data.get("PresetList")
        if isinstance(preset_list, list):
            candidates.extend(row for row in preset_list if isinstance(row, dict))
    elif isinstance(data, list):
        candidates.extend(row for row in data if isinstance(row, dict))

    if preset_name:
        expected = preset_name.casefold()
        selected = next(
            (
                row
                for row in candidates
                if str(row.get("PresetName") or row.get("Name") or "").strip().casefold() == expected
            ),
            None,
        )
        if selected:
            return str(selected.get("VideoEncoder") or "").strip()
    return str(candidates[0].get("VideoEncoder") or "").strip() if candidates else ""


def _source_video_probe(src_path: str) -> dict:
    """Read source video facts and one complete track inventory with ffprobe."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        (
            "stream=index,codec_type,codec_name,codec_tag_string,profile,pix_fmt,"
            "width,height,avg_frame_rate,r_frame_rate:stream_tags=language,title:"
            "stream_disposition=attached_pic:format=format_name"
        ),
        "-of",
        "json",
        src_path,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            **background_process_options(),
        )
    except FileNotFoundError:
        return {"error": "ffprobe is not installed"}
    except subprocess.TimeoutExpired:
        return {"error": "ffprobe timed out"}
    except Exception as exc:
        return {"error": f"ffprobe failed: {exc}"}
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "ffprobe returned an error").strip()
        return {"error": detail[:300]}
    try:
        payload = json.loads(result.stdout or "{}")
        streams = payload.get("streams") if isinstance(payload, dict) else []
        streams = streams if isinstance(streams, list) else []
        stream = next(
            (
                row for row in streams
                if isinstance(row, dict)
                and row.get("codec_type") == "video"
                and not bool((row.get("disposition") or {}).get("attached_pic"))
            ),
            {},
        )
        if not stream:
            stream = next((row for row in streams if isinstance(row, dict)), {})
        average_fps = _parse_frame_rate(stream.get("avg_frame_rate"))
        nominal_fps = _parse_frame_rate(stream.get("r_frame_rate"))
        track_inventory = []
        for row in streams:
            if not isinstance(row, dict):
                continue
            tags = row.get("tags") if isinstance(row.get("tags"), dict) else {}
            disposition = row.get("disposition") if isinstance(row.get("disposition"), dict) else {}
            track_inventory.append({
                "index": int(row.get("index") or 0),
                "type": str(row.get("codec_type") or "").strip().lower(),
                "codec": str(row.get("codec_name") or "").strip().lower(),
                "codec_tag": str(row.get("codec_tag_string") or "").strip().lower(),
                "profile": str(row.get("profile") or "").strip(),
                "language": str(tags.get("language") or "und").strip().lower(),
                "title": str(tags.get("title") or "").strip(),
                "attached_pic": bool(disposition.get("attached_pic")),
            })
        return {
            "codec": str(stream.get("codec_name") or "").strip().lower(),
            "profile": str(stream.get("profile") or "").strip(),
            "pix_fmt": str(stream.get("pix_fmt") or "").strip().lower(),
            "width": max(0, int(stream.get("width") or 0)),
            "height": max(0, int(stream.get("height") or 0)),
            "fps": average_fps or nominal_fps,
            "nominal_fps": nominal_fps or average_fps,
            "format": str((payload.get("format") or {}).get("format_name") or "").strip().lower(),
            "streams": track_inventory,
            "error": "",
        }
    except Exception as exc:
        return {"error": f"invalid ffprobe response: {exc}"}


def _split_extra_args(extra_args: str) -> list[str]:
    try:
        return shlex.split(str(extra_args or ""))
    except ValueError:
        return str(extra_args or "").split()


def _parse_frame_rate(value) -> float:
    """Parse an ffprobe frame-rate value without treating 0/0 as valid."""
    text = str(value or "").strip()
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            denominator_f = float(denominator)
            return float(numerator) / denominator_f if denominator_f else 0.0
        return float(text or 0.0)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _format_handbrake_fps(value) -> str:
    """Return a stable HandBrake rate string for the probed source average."""
    try:
        fps = float(value or 0.0)
    except (TypeError, ValueError):
        return ""
    if fps < 1.0 or fps > 1000.0:
        return ""
    for standard in (23.976, 29.97, 47.952, 59.94, 119.88):
        if abs(fps - standard) <= 0.01:
            return str(standard)
    if abs(fps - round(fps)) <= 0.001:
        return str(int(round(fps)))
    return f"{fps:.6f}".rstrip("0").rstrip(".")


def _smart_source_framerate_args(extra_args: str, source_fps=0.0) -> str:
    """Replace every conflicting rate option with source-average CFR."""
    parts = _split_extra_args(extra_args)
    clean: list[str] = []
    skip_value = False
    for part in parts:
        if skip_value:
            skip_value = False
            continue
        token = str(part or "")
        if token in {"-r", "--rate"}:
            skip_value = True
            continue
        if token.startswith("--rate=") or (token.startswith("-r") and len(token) > 2):
            continue
        if token in {"--vfr", "--pfr", "--cfr"}:
            continue
        clean.append(token)
    rate = _format_handbrake_fps(source_fps)
    if rate:
        clean.extend(["--rate", rate])
    # If ffprobe is unavailable, --cfr without --rate makes HandBrake use the
    # source average, so the safety rule still applies.
    clean.append("--cfr")
    return shlex.join(clean)


def _argument_value(args: list[str], *names: str) -> str:
    # HandBrake short options are case-sensitive: ``-e`` selects the video
    # encoder while ``-E`` selects the audio encoder. Never normalize option
    # case here or an audio ``-E copy`` can overwrite the real QSV encoder.
    expected = {str(name) for name in names}
    value = ""
    for index, arg in enumerate(args):
        text = str(arg or "")
        if text in expected and index + 1 < len(args):
            value = str(args[index + 1])
        else:
            for name in expected:
                prefix = name + "="
                if text.startswith(prefix):
                    value = text[len(prefix):]
    return value


def _selected_video_encoder(job: dict, preset_file: str, preset_name: str) -> str:
    """Resolve the encoder after applying any command-line override."""
    args = _split_extra_args(job.get("extra_args") or "")
    override = _argument_value(args, "--encoder", "-e")
    if override:
        return override.strip().lower()
    try:
        selected = load_preset_definition(preset_file, preset_name)
        return str(selected.get("VideoEncoder") or "").strip().lower()
    except Exception:
        return _preset_video_encoder(job).strip().lower()


def _qsv_adapter_index() -> int:
    """Return a safe, explicit HandBrake QSV adapter index."""
    try:
        index = int(str(os.environ.get("TSD_QSV_ADAPTER") or "0").strip())
    except (TypeError, ValueError):
        return 0
    return index if index >= 0 else 0


NVENC_AV1_ENCODERS = {"nvenc_av1", "nvenc_av1_10bit"}


def _uses_default_encoder_profile(encoder: str) -> bool:
    """Return whether HandBrake must choose the encoder profile itself."""
    return str(encoder or "").strip().lower() in NVENC_AV1_ENCODERS


def _sanitize_encoder_profile_args(extra_args: str, encoder: str) -> tuple[str, str]:
    """Remove stale profile overrides that are invalid for the routed encoder."""
    if not _uses_default_encoder_profile(encoder):
        return str(extra_args or ""), ""
    args = _split_extra_args(extra_args)
    clean: list[str] = []
    removed = ""
    skip_value = False
    for token in args:
        if skip_value:
            removed = str(token)
            skip_value = False
            continue
        text = str(token)
        if text == "--encoder-profile":
            skip_value = True
            continue
        if text.startswith("--encoder-profile="):
            removed = text.split("=", 1)[1]
            continue
        clean.append(text)
    return shlex.join(clean), removed


def _set_preset_hardware_decode(
    data,
    preset_name: str,
    enabled: bool,
    *,
    video_encoder: str = "",
    gpu_index: int | None = None,
) -> bool:
    """Set per-job decode, encoder, and adapter policy on one video preset."""
    candidates = []

    def walk(value):
        if isinstance(value, dict):
            if str(value.get("VideoEncoder") or "").strip():
                candidates.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    if not candidates:
        return False
    expected = str(preset_name or "").strip().casefold()
    selected = next(
        (
            item for item in candidates
            if str(item.get("PresetName") or item.get("Name") or "").strip().casefold() == expected
        ),
        candidates[0],
    )
    routed_encoder = str(video_encoder or "").strip().lower()
    if routed_encoder:
        selected["VideoEncoder"] = routed_encoder
    effective_encoder = str(selected.get("VideoEncoder") or "").strip().lower()
    if _uses_default_encoder_profile(effective_encoder):
        # HandBrake 1.11.2 forwards AV1 NVENC's preset profile string to
        # FFmpeg. av1_nvenc does not accept the software AV1 preset's "main"
        # value, so omit the field and let HandBrake/FFmpeg choose Auto.
        selected.pop("VideoProfile", None)
    if gpu_index is not None and routed_encoder.startswith(("nvenc_", "vce_")):
        existing = str(selected.get("VideoOptionExtra") or "").strip()
        parts = [part for part in existing.split(":") if part and not part.lower().startswith("gpu=")]
        parts.append(f"gpu={max(0, int(gpu_index))}")
        selected["VideoOptionExtra"] = ":".join(parts)
        selected["VideoAdapterIndex"] = max(0, int(gpu_index))
    # HandBrake 1.x represents QSV with bit 0x02. Using 1 here means
    # software decode support and HandBrake normalizes it back to zero for a
    # QSV source, even when VideoQSVDecode is true.
    selected["VideoHWDecode"] = 2 if enabled else 0
    selected["VideoQSVDecode"] = bool(enabled)
    # Do not leave AdapterIndex at HandBrake's auto sentinel (-1). The CLI and
    # patched Linux hwaccel layer use this index to resolve the DRM render node.
    if enabled or str(selected.get("VideoEncoder") or "").strip().lower().startswith("qsv_"):
        selected["VideoAdapterIndex"] = _qsv_adapter_index()
    return True


def _materialize_decode_policy_preset(
    job_id: str,
    preset_file: str,
    preset_name: str,
    enabled: bool,
    work_dir: str = "",
    *,
    video_encoder: str = "",
    gpu_index: int | None = None,
) -> tuple[str, str] | None:
    """Create a job-scoped preset that enforces decode and GPU routing."""
    try:
        with open(preset_file, "r", encoding="utf-8") as stream:
            data = json.load(stream)
        if not _set_preset_hardware_decode(
            data,
            preset_name,
            enabled,
            video_encoder=video_encoder,
            gpu_index=gpu_index,
        ):
            return None
        target_dir = work_dir or os.path.join(PRESET_WORK_DIR, str(job_id))
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, "video-policy-preset.json")
        with open(target, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        return target, target_dir
    except Exception:
        return None


def _scaled_to_fit(width: int, height: int, max_width: int, max_height: int) -> tuple[int, int]:
    """Fit inside a resolution ceiling without ever enlarging the source."""
    width = max(0, int(width or 0))
    height = max(0, int(height or 0))
    if width <= 0 or height <= 0:
        return 0, 0
    scale = min(1.0, max_width / width, max_height / height)
    out_width = max(2, int(width * scale) // 2 * 2)
    out_height = max(2, int(height * scale) // 2 * 2)
    return min(width, out_width), min(height, out_height)


def _resolution_plan(preset_key: str, source: dict, extra_args: str = "") -> dict:
    """Build a no-upscale 1080p/4K ceiling and its expected dimensions."""
    key = str(preset_key or "1080").strip().lower()
    max_width, max_height = (3840, 2160) if key == "4k" else (1920, 1080)
    args = _split_extra_args(extra_args)
    try:
        requested_width = int(_argument_value(args, "--width", "-w") or 0)
    except (TypeError, ValueError):
        requested_width = 0
    try:
        requested_height = int(_argument_value(args, "--height", "-l") or 0)
    except (TypeError, ValueError):
        requested_height = 0
    source_width = max(0, int(source.get("width") or 0))
    source_height = max(0, int(source.get("height") or 0))
    if requested_width and not requested_height and source_width and source_height:
        requested_height = max(2, round(requested_width * source_height / source_width))
    elif requested_height and not requested_width and source_width and source_height:
        requested_width = max(2, round(requested_height * source_width / source_height))
    basis_width = requested_width or source_width
    basis_height = requested_height or source_height
    target_width, target_height = _scaled_to_fit(
        basis_width,
        basis_height,
        max_width,
        max_height,
    )
    return {
        "key": key if key in {"1080", "4k"} else "1080",
        "max_width": max_width,
        "max_height": max_height,
        "target_width": target_width,
        "target_height": target_height,
        "cli_args": ["--maxWidth", str(max_width), "--maxHeight", str(max_height)],
    }


def _hardware_decode_plan(encoder: str, source: dict, mode: str | None = None) -> dict:
    """Choose QSV decode only where policy, encoder, and source allow it."""
    requested_mode = str(mode if mode is not None else os.environ.get("TSD_HW_DECODE") or "auto").strip().lower()
    if requested_mode not in {"auto", "qsv", "off"}:
        requested_mode = "auto"
    encoder_name = str(encoder or "").strip().lower()
    codec = str(source.get("codec") or "").strip().lower()
    source_error = str(source.get("error") or "").strip()

    reason = ""
    enabled = False
    if requested_mode == "off":
        reason = "disabled by TSD_HW_DECODE=off"
    elif source_error:
        reason = f"source probe unavailable: {source_error}"
    elif codec not in QSV_DECODE_SOURCE_CODECS:
        reason = f"source codec {codec or 'unknown'} is not in the supported H.264/HEVC set"
    elif requested_mode == "auto" and encoder_name not in QSV_ENCODERS and not encoder_name.startswith("qsv_"):
        reason = f"video encoder {encoder_name or 'unknown'} is not Intel QSV"
    else:
        enabled = True

    return {
        "mode": requested_mode,
        "enabled": enabled,
        "decoder": "qsv" if enabled else "software",
        "reason": reason,
        "cli_args": ["--enable-hw-decoding", "qsv"] if enabled else ["--disable-hw-decoding"],
        "label": "QSV" if enabled else f"software fallback ({reason or 'QSV decode not selected'})",
    }


def _qsv_decode_log_evidence(line: str) -> str:
    """Classify HandBrake output as an active QSV path or software fallback."""
    text = str(line or "")
    if QSV_DECODE_FALLBACK_RE.search(text):
        return "fallback"
    if QSV_DECODE_POSITIVE_RE.search(text):
        return "active"
    return ""


def _job_uses_hardware_encoder(job: dict | None) -> bool:
    """Return True only when a job is known to use a GPU encoder."""
    job = job if isinstance(job, dict) else {}
    operations = job.get("operations") if isinstance(job.get("operations"), dict) else {}
    if str(job.get("job_type") or operations.get("job_type") or "").strip().lower() == "audio_only":
        return False
    method = _job_encode_metadata(job)
    family = str(method.get("encoder_family") or "").strip().lower()
    if family in HARDWARE_ENCODER_FAMILIES:
        return True
    if family == "software":
        return False

    preset_encoder = _preset_video_encoder(job)
    if not preset_encoder:
        return False
    preset_method = _encoder_method_from_encoder(preset_encoder, job.get("preset") or "")
    return str(preset_method.get("encoder_family") or "").lower() in HARDWARE_ENCODER_FAMILIES


def _hardware_transcode_limit(settings: dict | None = None, job: dict | None = None) -> int:
    """Return the bounded per-worker GPU encode limit."""
    stored_policy = job.get("encoding_policy") if isinstance(job, dict) else None
    worker_settings = {}
    if str(os.environ.get("TSD_WORKER_MODE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        try:
            worker_settings = load_settings()
        except Exception:
            worker_settings = {}
    if worker_settings.get("worker_controller_managed_capacity"):
        # The headless service has no local settings UI. Once a paired
        # controller manages its capacity, the latest controller value wins
        # for all queued work instead of leaving stale limits on older jobs.
        source = worker_settings
    elif (
        isinstance(job, dict)
        and job.get("mode") == "remote_transfer"
        and isinstance(stored_policy, dict)
        and "hardware_transcode_concurrency" in stored_policy
    ):
        source = stored_policy
    else:
        source = settings if isinstance(settings, dict) else load_settings()
    try:
        value = int(source.get("hardware_transcode_concurrency") or 1)
    except (TypeError, ValueError):
        value = 1
    return max(1, min(8, value))


def _can_dispatch_job(job: dict | None, running_jobs: list[dict], hardware_limit: int) -> bool:
    """Enforce GPU-only concurrency while keeping software jobs exclusive."""
    if not running_jobs:
        return True
    if not _job_uses_hardware_encoder(job):
        return False
    if any(not _job_uses_hardware_encoder(row) for row in running_jobs):
        return False
    return len(running_jobs) < max(1, int(hardware_limit or 1))


def _windows_gpu_route_settings() -> tuple[dict[str, str], bool]:
    """Return the desktop worker's persistent per-codec routing policy."""
    if str(os.environ.get("TSD_WINDOWS_WORKER") or "").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        return {}, True
    try:
        parsed = json.loads(os.environ.get("TSD_WINDOWS_GPU_ROUTES") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = {}
    routes = parsed if isinstance(parsed, dict) else {}
    fallback = str(os.environ.get("TSD_WINDOWS_GPU_FALLBACK_ANY") or "1").strip().lower() in {
        "1", "true", "yes", "on",
    }
    return {
        codec: str(routes.get(codec) or "auto").strip().lower()
        for codec in ("av1", "h265", "h264")
    }, fallback


def _windows_route_encoder(family: str, codec: str, bit_depth: str, available: list[str]) -> str:
    depth = "10" if str(bit_depth or "8") in {"10", "12"} else "8"
    candidate = WINDOWS_GPU_ENCODERS.get(family, {}).get((codec, depth), "")
    if candidate and (not available or candidate in available):
        return candidate
    return ""


def _assign_windows_gpu_route(job: dict, running_jobs: list[dict]) -> tuple[bool, dict | None]:
    """Reserve an idle compatible Windows adapter for one queued video job."""
    routes, fallback_any = _windows_gpu_route_settings()
    if not routes:
        job.pop("gpu_route_assignment", None)
        return True, None

    method = _job_encode_metadata(job)
    encoder = str(method.get("encoder") or _preset_video_encoder(job) or "").strip().lower()
    if encoder:
        method = _encoder_method_from_encoder(encoder, job.get("preset") or "")
    codec = str(method.get("video_codec") or "").strip().lower()
    if codec not in routes:
        job.pop("gpu_route_assignment", None)
        return True, None
    bit_depth = str(method.get("bit_depth") or "8")

    try:
        from .node_linking import encoder_hardware_profile

        hardware = encoder_hardware_profile()
    except Exception:
        hardware = {}
    gpus = [row for row in hardware.get("gpus") or [] if isinstance(row, dict)]
    compatible = []
    unmapped_nvidia = []
    incompatible_av1_nvidia = []
    family_seen: dict[str, int] = {}
    for row in gpus:
        family = str(row.get("encoder_family") or "").strip().lower()
        fallback_vendor_index = family_seen.get(family, 0)
        family_seen[family] = fallback_vendor_index + 1
        try:
            vendor_index = int(row.get("vendor_index"))
        except (TypeError, ValueError):
            if family == "nvenc":
                unmapped_nvidia.append(str(row.get("name") or "Unknown NVIDIA GPU"))
                continue
            vendor_index = fallback_vendor_index
        routed_encoder = _windows_route_encoder(
            family,
            codec,
            bit_depth,
            [str(value) for value in row.get("encoders") or []],
        )
        if not routed_encoder:
            if family == "nvenc" and codec == "av1":
                incompatible_av1_nvidia.append(
                    f"{row.get('name') or 'NVIDIA GPU'}: "
                    f"{row.get('av1_nvenc_verification') or 'AV1 NVENC is not supported'}"
                )
            continue
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            continue
        compatible.append({
            "key": f"gpu:{index}",
            "index": index,
            "vendor_index": vendor_index,
            "name": str(row.get("name") or f"GPU {index}"),
            "vendor": str(row.get("vendor") or ""),
            "encoder_family": family,
            "encoder": routed_encoder,
            "codec": codec,
            "bit_depth": bit_depth,
            "memory_bytes": int(row.get("memory_bytes") or 0),
            "gpu_uuid": str(row.get("gpu_uuid") or ""),
            "pci_bus_id": str(row.get("pci_bus_id") or ""),
            "identity_source": str(row.get("identity_source") or ""),
            "encoders": [str(value) for value in row.get("encoders") or []],
        })
    if not compatible:
        job.pop("gpu_route_assignment", None)
        if unmapped_nvidia:
            message = (
                "NVIDIA GPU routing is unavailable because nvidia-smi could not map "
                "these adapters by UUID/PCI bus: " + ", ".join(unmapped_nvidia)
            )
            job["gpu_route_error"] = message
            job["error_message"] = message
            job["phase"] = "waiting_for_gpu_mapping"
            return False, None
        if incompatible_av1_nvidia:
            message = "No verified AV1 NVENC adapter is available: " + "; ".join(incompatible_av1_nvidia)
            job["gpu_route_error"] = message
            job["error_message"] = message
            job["phase"] = "waiting_for_av1_gpu_validation"
            return False, None
        return True, None

    if codec == "av1":
        try:
            from .node_linking import verify_windows_nvenc_encoder
        except Exception:
            verify_windows_nvenc_encoder = None
        verified = []
        verification_errors = []
        for candidate in compatible:
            if candidate["encoder_family"] != "nvenc":
                verified.append(candidate)
                continue
            if verify_windows_nvenc_encoder is None:
                ok, reason = False, "NVENC capability verifier is unavailable"
            else:
                ok, reason = verify_windows_nvenc_encoder(candidate, candidate["encoder"])
            candidate["capability_verified"] = bool(ok)
            candidate["capability_verification"] = str(reason or "")
            if ok:
                verified.append(candidate)
            else:
                verification_errors.append(f"{candidate['name']}: {reason}")
        compatible = verified
        if not compatible:
            job.pop("gpu_route_assignment", None)
            message = "AV1 GPU validation failed: " + "; ".join(verification_errors)
            job["gpu_route_error"] = message
            job["error_message"] = message
            job["phase"] = "waiting_for_av1_gpu_validation"
            return False, None

    busy = {
        str(
            (row.get("gpu_route_assignment") or {}).get("identity_key")
            or (row.get("gpu_route_assignment") or {}).get("key")
            or ""
        )
        for row in running_jobs
        if isinstance(row.get("gpu_route_assignment"), dict)
    }
    for row in compatible:
        stable_identity = row.get("gpu_uuid") or row.get("pci_bus_id")
        row["identity_key"] = f"nvidia:{stable_identity}" if stable_identity else row["key"]
    requested = routes.get(codec, "auto")
    requested_rows = [row for row in compatible if row["key"] == requested]
    preferred_family = str(method.get("encoder_family") or "")
    idle = [row for row in compatible if row["identity_key"] not in busy and row["key"] not in busy]
    choices = [
        row for row in requested_rows
        if row["identity_key"] not in busy and row["key"] not in busy
    ]
    used_fallback = False
    if not choices and (requested == "auto" or fallback_any):
        choices = idle
        used_fallback = requested != "auto"
    if not choices:
        message = (
            f"Waiting for the selected {codec.upper()} GPU to finish its current process-tree cleanup"
            if requested != "auto"
            else f"Waiting for an idle GPU compatible with {codec.upper()}"
        )
        job["gpu_route_error"] = message
        job["error_message"] = message
        job["phase"] = "waiting_for_compatible_gpu"
        return False, None
    # Preserve AV1-capable NVIDIA hardware for AV1 when an older NVENC GPU can
    # handle H.264/HEVC. The reserved GPU is still used as a busy-GPU fallback.
    choices.sort(key=lambda row: (
        row["encoder_family"] != preferred_family,
        codec != "av1" and any("_av1" in name for name in row.get("encoders") or []),
        -row["memory_bytes"],
        row["index"],
    ))
    assignment = dict(choices[0])
    assignment.update({
        "requested": requested,
        "fallback_used": used_fallback,
        "assigned_at": _now_ts(),
    })
    job["gpu_route_assignment"] = assignment
    previous_gpu_error = str(job.get("gpu_route_error") or "")
    job.pop("gpu_route_error", None)
    if previous_gpu_error and str(job.get("error_message") or "") == previous_gpu_error:
        job["error_message"] = ""
    if str(job.get("phase") or "").startswith("waiting_for_"):
        job["phase"] = "queued"
    routed = _encoder_method_from_encoder(assignment["encoder"], job.get("preset") or "")
    job.update({
        "encode_method": routed.get("encode_method"),
        "encoder": routed.get("encoder"),
        "video_codec": routed.get("video_codec"),
        "encoder_family": routed.get("encoder_family"),
        "bit_depth": routed.get("bit_depth"),
    })
    return True, assignment


def _job_learning_metadata(metadata: dict | None) -> dict:
    """Keep the small, safe provenance needed for post-encode learning."""
    metadata = metadata if isinstance(metadata, dict) else {}
    context = metadata.get("smart_feedback_context")
    feedback = metadata.get("quality_feedback")
    episode_plan = metadata.get("smart_episode_plan")
    return {
        "smart_preset": bool(metadata.get("smart_preset", False)),
        "smart_profile_id": str(metadata.get("smart_profile_id") or "")[:80],
        "smart_candidate_id": str(metadata.get("smart_candidate_id") or "")[:80],
        "automation_source": str(metadata.get("automation_source") or "")[:40],
        "smart_feedback_context": context if isinstance(context, dict) else None,
        "smart_episode_plan": episode_plan if isinstance(episode_plan, dict) else None,
        "quality_feedback": feedback if isinstance(feedback, dict) else None,
    }


# -------------------------------------------------------------------
# Persistence: saving / loading jobs.json
# -------------------------------------------------------------------

def save_jobs():
    """
    Persist current job metadata + queue + queue_paused flag to disk.

    Writes a JSON file to JOBS_FILE. We intentionally do NOT persist pids,
    because the OS process won't survive container restarts anyway.
    """
    global queue_paused, dashboard_totals, history_cleared_before

    acquired = False
    try:
        JOBS_SAVE_LOCK.acquire()
        acquired = True
        serializable = {}
        for jid, j in list(jobs.items()):
            serializable[jid] = {
                "status": j.get("status"),
                "src": j.get("src"),
                "preset": j.get("preset"),
                "extra_args": j.get("extra_args", ""),
                "mode": j.get("mode", "local"),
                "dispatch_mode": j.get("dispatch_mode") or "local",
                "dispatch_plan": j.get("dispatch_plan") if isinstance(j.get("dispatch_plan"), dict) else None,
                "dispatch_node_id": j.get("dispatch_node_id") or "",
                "dispatch_node_name": j.get("dispatch_node_name") or "",
                "dispatch_error": j.get("dispatch_error") or "",
                "dispatch_attempts": int(j.get("dispatch_attempts") or 0),
                "dispatch_retry_at": float(j.get("dispatch_retry_at") or 0.0),
                "transfer": j.get("transfer") if isinstance(j.get("transfer"), dict) else None,
                "preset_bundle": _normalize_preset_bundle(j.get("preset_bundle")),
                "preset_selection": j.get("preset_selection") or j.get("preset") or "1080",
                "preset_adaptive": bool(j.get("preset_adaptive", False)),
                "preset_preferences": j.get("preset_preferences") if isinstance(j.get("preset_preferences"), dict) else {},
                "preset_snapshot_locked": bool(j.get("preset_snapshot_locked", bool(j.get("preset_bundle")))),
                "preset_revision": max(1, int(j.get("preset_revision") or 1)),
                "queued_preset_name": j.get("queued_preset_name") or "",
                "preset_adaptation": j.get("preset_adaptation") if isinstance(j.get("preset_adaptation"), dict) else None,
                "encoding_policy": _normalize_encoding_policy(j.get("encoding_policy")),
                "encode_method": j.get("encode_method"),
                "encoder": j.get("encoder"),
                "video_codec": j.get("video_codec"),
                "encoder_family": j.get("encoder_family"),
                "bit_depth": j.get("bit_depth"),
                "job_type": j.get("job_type") or "video_preserve_audio",
                "operations": _queued_operations(j, j.get("operations")),
                "parent_job_id": j.get("parent_job_id") or "",
                "audio_inventory": j.get("audio_inventory") if isinstance(j.get("audio_inventory"), dict) else None,
                "output_inventory": j.get("output_inventory") if isinstance(j.get("output_inventory"), dict) else None,
                "storage_breakdown": j.get("storage_breakdown") if isinstance(j.get("storage_breakdown"), dict) else None,
                **_job_learning_metadata(j),
                "log": j.get("log", ""),
                "returncode": j.get("returncode"),
                "pid": None,  # never persist the actual pid
                "progress": float(j.get("progress") or 0.0),
                # NEW: persist ETA if present
                "eta_seconds": j.get("eta_seconds"),
                "estimated_out_bytes": j.get("estimated_out_bytes"),
                "planned_out_bytes": j.get("planned_out_bytes"),
                "estimated_out_source": j.get("estimated_out_source") or "",
                "estimated_out_current_bytes": j.get("estimated_out_current_bytes"),
                "estimated_out_checked_progress": j.get("estimated_out_checked_progress"),
                "estimated_out_updated_at": j.get("estimated_out_updated_at"),
                "estimate_checkpoints_seen": j.get("estimate_checkpoints_seen") if isinstance(j.get("estimate_checkpoints_seen"), list) else [],
                "auto_stop_triggered": bool(j.get("auto_stop_triggered", False)),
                "auto_stop_details": j.get("auto_stop_details") if isinstance(j.get("auto_stop_details"), dict) else None,
                "cancel_reason": j.get("cancel_reason") or "",
                # Storage tracking
                "src_bytes": j.get("src_bytes"),
                "out_bytes": j.get("out_bytes"),
                "saved_bytes": j.get("saved_bytes"),
                "out_path": j.get("out_path"),
                "is_hdr": bool(j.get("is_hdr", False) or _looks_like_hdr_path(j.get("src", ""))),
                "created_at": j.get("created_at"),
                "started_at": j.get("started_at"),
                "finished_at": j.get("finished_at"),
                "duration_seconds": j.get("duration_seconds"),
                "phase": j.get("phase") or j.get("status") or "",
                "error_message": j.get("error_message") or "",
            }

        state = {
            "jobs": serializable,
            "queue": list(job_queue),
            "queue_paused": queue_paused,
            "dashboard_totals": _normalize_dashboard_totals(dashboard_totals),
            "history_cleared_before": max(0.0, float(history_cleared_before or 0.0)),
        }

        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = JOBS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, JOBS_FILE)
        shutil.copy2(JOBS_FILE, JOBS_FILE + ".bak")
    except Exception as e:
        print(f"[WARN] Failed to save jobs.json: {e}", flush=True)
    finally:
        if acquired:
            JOBS_SAVE_LOCK.release()


def load_jobs():
    """
    Load previous jobs + queue state from JOBS_FILE.

    - Any job that was "running" when we last saved is treated as "queued"
      again (since the process is gone after restart).
    - We also restore the queue order and queue_paused flag.
    """
    global jobs, job_queue, queue_paused, dashboard_totals, history_cleared_before

    if not os.path.isfile(JOBS_FILE):
        jobs = {}
        job_queue = []
        queue_paused = False
        dashboard_totals = _empty_dashboard_totals()
        history_cleared_before = 0.0
        return

    try:
        try:
            with open(JOBS_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            with open(JOBS_FILE + ".bak", "r", encoding="utf-8") as f:
                state = json.load(f)

        data = state.get("jobs") or {}
        q = state.get("queue") or []
        queue_paused = bool(state.get("queue_paused", False))
        dashboard_totals = _normalize_dashboard_totals(state.get("dashboard_totals"))
        try:
            history_cleared_before = max(0.0, float(state.get("history_cleared_before") or 0.0))
        except (TypeError, ValueError):
            history_cleared_before = 0.0

        jobs = {}
        for jid, j in data.items():
            if not isinstance(j, dict):
                continue

            status = j.get("status", "unknown")
            # If the container died while it was running, treat it as queued again.
            if status in {"running", "dispatching"}:
                status = "queued"
            method = _encode_metadata_from_extra_args(j.get("extra_args", ""), j.get("preset"), j)
            loaded_bundle = _normalize_preset_bundle(j.get("preset_bundle")) or snapshot_preset_bundle(j.get("preset") or "1080")
            loaded_dispatch_plan = deepcopy(j.get("dispatch_plan")) if isinstance(j.get("dispatch_plan"), dict) else None
            display_metadata = dict(j)
            if isinstance(loaded_dispatch_plan, dict) and isinstance(loaded_dispatch_plan.get("encode_metadata"), dict):
                display_metadata.update(loaded_dispatch_plan["encode_metadata"])
            loaded_preset_name = _queued_preset_display_name(
                str(j.get("preset") or "1080"),
                loaded_bundle,
                extra_args=str(j.get("extra_args") or ""),
                encode_metadata=display_metadata,
                preset_selection=str(j.get("preset_selection") or ""),
                preset_adaptive=bool(j.get("preset_adaptive", False)),
            )
            if j.get("mode") == "auto_node":
                loaded_dispatch_plan = loaded_dispatch_plan or {}
                loaded_dispatch_plan.setdefault("preset", j.get("preset"))
                loaded_dispatch_plan.setdefault("preset_bundle", loaded_bundle)
                loaded_dispatch_plan.setdefault("extra_args", j.get("extra_args", ""))
                loaded_dispatch_plan.setdefault("encode_metadata", {})
                loaded_dispatch_plan.setdefault("preset_selection", j.get("preset_selection") or j.get("preset") or "1080")
                loaded_dispatch_plan.setdefault("preset_adaptive", bool(j.get("preset_adaptive", False)))
                loaded_dispatch_plan.setdefault(
                    "preset_preferences",
                    j.get("preset_preferences") if isinstance(j.get("preset_preferences"), dict) else {},
                )
                loaded_dispatch_plan.setdefault(
                    "encoding_policy",
                    _normalize_encoding_policy(j.get("encoding_policy")),
                )
                # Repair stale Smart labels written by older releases.  A
                # persisted base bundle name must not override the actual
                # episode encoder selected in the immutable dispatch plan.
                loaded_dispatch_plan["queued_preset_name"] = loaded_preset_name
                loaded_dispatch_plan.setdefault("preset_revision", max(1, int(j.get("preset_revision") or 1)))

            jobs[jid] = {
                "status": status,
                "src": j.get("src"),
                "preset": j.get("preset"),
                "extra_args": j.get("extra_args", ""),
                "mode": j.get("mode", "local"),
                "dispatch_mode": j.get("dispatch_mode") or "local",
                "dispatch_plan": loaded_dispatch_plan,
                "dispatch_node_id": j.get("dispatch_node_id") or "",
                "dispatch_node_name": j.get("dispatch_node_name") or "",
                "dispatch_error": j.get("dispatch_error") or "",
                "dispatch_attempts": int(j.get("dispatch_attempts") or 0),
                "dispatch_retry_at": float(j.get("dispatch_retry_at") or 0.0),
                "transfer": j.get("transfer") if isinstance(j.get("transfer"), dict) else None,
                "preset_bundle": loaded_bundle,
                "preset_selection": j.get("preset_selection") or j.get("preset") or "1080",
                "preset_adaptive": bool(j.get("preset_adaptive", False)),
                "preset_preferences": j.get("preset_preferences") if isinstance(j.get("preset_preferences"), dict) else {},
                "preset_snapshot_locked": True,
                "preset_revision": max(1, int(j.get("preset_revision") or 1)),
                "queued_preset_name": loaded_preset_name,
                "preset_adaptation": j.get("preset_adaptation") if isinstance(j.get("preset_adaptation"), dict) else None,
                "encoding_policy": _normalize_encoding_policy(j.get("encoding_policy")),
                "encode_method": method.get("encode_method"),
                "encoder": method.get("encoder"),
                "video_codec": method.get("video_codec"),
                "encoder_family": method.get("encoder_family"),
                "bit_depth": method.get("bit_depth"),
                "job_type": str(j.get("job_type") or (j.get("operations") or {}).get("job_type") or "video_preserve_audio"),
                "operations": _queued_operations(j, j.get("operations")),
                "parent_job_id": j.get("parent_job_id") or "",
                "audio_inventory": j.get("audio_inventory") if isinstance(j.get("audio_inventory"), dict) else None,
                "output_inventory": j.get("output_inventory") if isinstance(j.get("output_inventory"), dict) else None,
                "storage_breakdown": j.get("storage_breakdown") if isinstance(j.get("storage_breakdown"), dict) else None,
                **_job_learning_metadata(j),
                "log": j.get("log", ""),
                "returncode": j.get("returncode"),
                "pid": None,
                "progress": float(j.get("progress") or 0.0),
                # NEW: restore ETA if it was saved
                "eta_seconds": j.get("eta_seconds"),
                "estimated_out_bytes": j.get("estimated_out_bytes"),
                "planned_out_bytes": j.get("planned_out_bytes"),
                "estimated_out_source": j.get("estimated_out_source") or "",
                "estimated_out_current_bytes": j.get("estimated_out_current_bytes"),
                "estimated_out_checked_progress": j.get("estimated_out_checked_progress"),
                "estimated_out_updated_at": j.get("estimated_out_updated_at"),
                "estimate_checkpoints_seen": j.get("estimate_checkpoints_seen") if isinstance(j.get("estimate_checkpoints_seen"), list) else [],
                "auto_stop_triggered": bool(j.get("auto_stop_triggered", False)),
                "auto_stop_details": j.get("auto_stop_details") if isinstance(j.get("auto_stop_details"), dict) else None,
                "cancel_reason": j.get("cancel_reason") or "",
                # Storage tracking
                "src_bytes": j.get("src_bytes"),
                "out_bytes": j.get("out_bytes"),
                "saved_bytes": j.get("saved_bytes"),
                "out_path": j.get("out_path"),
                "is_hdr": bool(j.get("is_hdr", False) or _looks_like_hdr_path(j.get("src", ""))),
                "created_at": j.get("created_at"),
                "started_at": j.get("started_at"),
                "finished_at": j.get("finished_at"),
                "duration_seconds": j.get("duration_seconds"),
                "phase": (
                    "waiting_for_node"
                    if status == "queued" and j.get("mode") == "auto_node"
                    else (j.get("phase") or status)
                ),
                "error_message": j.get("error_message") or "",
            }

        # rebuild queue, keeping only jobs that still exist and are queued
        job_queue = [
            jid
            for jid in q
            if jid in jobs and jobs[jid].get("status") == "queued"
        ]

    except Exception as e:
        print(f"[WARN] Failed to load jobs.json: {e}", flush=True)
        jobs = {}
        job_queue = []
        queue_paused = False
        dashboard_totals = _empty_dashboard_totals()
        history_cleared_before = 0.0


def _cleanup_orphaned_windows_encoders() -> int:
    """Kill stale HandBrake/FFmpeg processes owned by this worker's folders."""
    if os.name != "nt" or str(os.environ.get("TSD_WINDOWS_WORKER") or "").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        return 0
    roots = {
        str(os.environ.get("TSD_WORKER_TEMP_DIR") or "").strip(),
        str(os.environ.get("TSD_WORKER_SCRATCH_DIR") or "").strip(),
    }
    markers = {
        ntpath.normcase(ntpath.abspath(value)).rstrip("\\/")
        for value in roots
        if value
    }
    for job in jobs.values():
        transfer = job.get("transfer") if isinstance(job.get("transfer"), dict) else {}
        for value in (
            job.get("src"),
            job.get("out_path"),
            job.get("local_out_path"),
            transfer.get("local_src"),
            transfer.get("local_out"),
            transfer.get("work_dir"),
        ):
            text = str(value or "").strip()
            if text:
                markers.add(ntpath.normcase(ntpath.abspath(text)).rstrip("\\/"))
    markers = {value for value in markers if len(value) >= 3}
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            "Get-CimInstance Win32_Process | "
            "Where-Object {$_.Name -in @('HandBrakeCLI.exe','ffmpeg.exe')} | "
            "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
            "ConvertTo-Json -Compress"
        ),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
            **background_process_options(),
        )
        payload = json.loads(result.stdout or "[]") if result.returncode == 0 else []
    except Exception as exc:
        print(f"[WARN] Could not inspect orphaned Windows encoders: {exc}", flush=True)
        return 0
    if isinstance(payload, dict):
        payload = [payload]
    terminated = 0
    for row in payload if isinstance(payload, list) else []:
        if not isinstance(row, dict):
            continue
        command_line = ntpath.normcase(str(row.get("CommandLine") or ""))
        if not command_line or not any(marker in command_line for marker in markers):
            continue
        ok, detail = terminate_process_tree(row.get("ProcessId"), force=True)
        if ok:
            terminated += 1
            print(
                f"[RECOVERY] Terminated orphaned {row.get('Name') or 'encoder'} "
                f"PID {row.get('ProcessId')} from a ByteSqueeze work folder.",
                flush=True,
            )
        elif detail:
            print(f"[WARN] Could not terminate orphaned encoder: {detail}", flush=True)
    return terminated


def initialize_jobs_system():
    """
    Call this once at app startup.

    - Loads existing jobs from disk
    - Starts dispatcher thread (which just idles if there is nothing queued)
    """
    load_jobs()
    _cleanup_orphaned_windows_encoders()
    ensure_dispatcher()

def _find_existing_active_job_for_src(src: str) -> str | None:
    """
    Check if there is already a job for this src that is either queued
    or currently running.

    We treat those as "active", so the same file should not be added
    to the queue a second time while one of these is in-flight.

    Returns:
        job_id (str) if found, otherwise None.
    """
    for jid, j in jobs.items():
        if j.get("src") == src and j.get("status") in ("queued", "dispatching", "running", "waiting_to_upload"):
            return jid
    return None


# -------------------------------------------------------------------
# Core job creation / lookup helpers (used by routes)
# -------------------------------------------------------------------

def _queued_operations(metadata: dict | None = None, operations: dict | None = None) -> dict:
    """Keep an additive, backward-compatible operation snapshot on each job."""
    metadata = metadata if isinstance(metadata, dict) else {}
    source = operations if isinstance(operations, dict) else (
        metadata.get("operations") if isinstance(metadata.get("operations"), dict) else {}
    )
    settings = load_settings()
    explicit_job_type = source.get("job_type") or metadata.get("job_type")
    job_type = str(explicit_job_type or "video_preserve_audio").strip().lower()
    if job_type not in {"video_audio", "video_preserve_audio", "audio_only"}:
        job_type = "video_preserve_audio"
    audio_policy = str(
        source.get("audio_policy")
        or metadata.get("audio_policy")
        or settings.get("audio_policy_default")
        or "preserve"
    ).strip().lower()
    if audio_policy not in {"preserve", "optimize_lossless", "custom"}:
        audio_policy = "preserve"
    if not explicit_job_type and audio_policy != "preserve":
        job_type = "video_audio"
    if job_type == "video_preserve_audio":
        audio_policy = "preserve"
    return {
        "schema": 1,
        "job_type": job_type,
        "video_action": "copy" if job_type == "audio_only" else "encode",
        "audio_policy": audio_policy,
        "audio_actions": deepcopy(source.get("audio_actions")) if isinstance(source.get("audio_actions"), list) else [],
        "subtitle_action": str(source.get("subtitle_action") or "copy").strip().lower(),
        "allow_object_audio_loss": bool(
            source.get(
                "allow_object_audio_loss",
                settings.get("audio_allow_object_metadata_loss", False),
            )
        ),
        "default_target_codec": str(source.get("default_target_codec") or settings.get("audio_optimize_codec") or "aac"),
        "default_target_bitrate_kbps": int(
            source.get("default_target_bitrate_kbps")
            or settings.get("audio_optimize_bitrate_kbps")
            or 1024
        ),
        "deduplicate_languages": bool(
            source.get(
                "deduplicate_languages",
                settings.get("audio_deduplicate_languages", True),
            )
        ),
        "preferred_languages": deepcopy(
            source.get("preferred_languages")
            if isinstance(source.get("preferred_languages"), list)
            else settings.get("audio_preferred_languages", ["eng", "spa"])
        ),
        "replace_source": bool(source.get("replace_source", job_type == "audio_only")),
    }


def _planned_output_estimate(
    metadata: dict | None,
    operations: dict | None = None,
) -> int | None:
    """Return a real planner-provided output estimate, never a guessed ratio."""
    metadata = metadata if isinstance(metadata, dict) else {}
    operations = operations if isinstance(operations, dict) else {}
    estimates = metadata.get("estimates") if isinstance(metadata.get("estimates"), dict) else {}
    operation_estimate = operations.get("estimate") if isinstance(operations.get("estimate"), dict) else {}
    candidates = (
        metadata.get("estimated_output_bytes"),
        metadata.get("estimated_out_bytes"),
        estimates.get("estimated_output_bytes"),
        operation_estimate.get("output_total_bytes"),
        operation_estimate.get("estimated_output_bytes"),
    )
    for value in candidates:
        try:
            size = int(float(value or 0))
        except (TypeError, ValueError, OverflowError):
            continue
        if size > 0:
            return size

    episode_plan = metadata.get("smart_episode_plan")
    episode_plan = episode_plan if isinstance(episode_plan, dict) else {}
    target = episode_plan.get("target") if isinstance(episode_plan.get("target"), dict) else {}
    try:
        target_mb = float(target.get("target_mb") or metadata.get("target_mb") or 0.0)
    except (TypeError, ValueError, OverflowError):
        target_mb = 0.0
    return int(round(target_mb * 1024.0 * 1024.0)) if target_mb > 0 else None

def create_job(
    src: str,
    preset: str,
    extra_args: str = "",
    preset_bundle: dict | None = None,
    encode_metadata: dict | None = None,
    *,
    dispatch_mode: str = "local",
    preset_selection: str = "",
    preset_adaptive: bool = False,
    preset_preferences: dict | None = None,
    encoding_policy: dict | None = None,
    operations: dict | None = None,
    parent_job_id: str = "",
) -> str:
    """
    Create a single job and append it to the queue.

    This does NOT validate src path or preset value — the web layer
    should do that before calling this function.

    IMPORTANT:
      - If there is already a job for this src with status "queued"
        or "running", we do NOT create a duplicate. Instead, we just
        return the existing job_id.

    Args:
        src (str): Absolute path to source video file.
        preset (str): "1080" or "4k"

    Returns:
        str: job_id (UUID string, or existing one if already active)
    """
    # Check for an already-active job for this src
    existing_id = _find_existing_active_job_for_src(src)
    if existing_id is not None:
        # Do not enqueue a second job for the same src
        return existing_id

    job_id = str(uuid.uuid4())
    normalized_dispatch_mode = str(dispatch_mode or "local").strip().lower()
    automatic_dispatch = normalized_dispatch_mode in {"auto", "available", "next_available"}
    metadata_source = encode_metadata if isinstance(encode_metadata, dict) else {}
    queued_operations = _queued_operations(metadata_source, operations)
    metadata_episode_plan = (
        metadata_source.get("smart_episode_plan")
        if isinstance(metadata_source.get("smart_episode_plan"), dict)
        else {}
    )
    normalized_selection = str(preset_selection or metadata_source.get("preset_selection") or preset or "1080").strip().lower()
    adaptive_encoder = bool(preset_adaptive or metadata_source.get("preset_adaptive") or normalized_selection == "smart")
    preferences = preset_preferences if isinstance(preset_preferences, dict) else (
        metadata_source.get("preset_preferences") if isinstance(metadata_source.get("preset_preferences"), dict) else {}
    )
    queued_encoding_policy = _normalize_encoding_policy(encoding_policy)
    method = _encode_metadata_from_extra_args(extra_args, preset, encode_metadata)
    planned_out_bytes = _planned_output_estimate(metadata_source, queued_operations)
    normalized_bundle = _normalize_preset_bundle(preset_bundle) or snapshot_preset_bundle(preset)
    queued_preset_name = _queued_preset_display_name(
        preset,
        normalized_bundle,
        extra_args=extra_args,
        encode_metadata=metadata_source,
        preset_selection=normalized_selection,
        preset_adaptive=adaptive_encoder,
    )
    dispatch_plan = None
    if automatic_dispatch:
        dispatch_plan = {
            "preset": preset,
            "preset_bundle": normalized_bundle,
            "extra_args": extra_args or "",
            "encode_metadata": encode_metadata if isinstance(encode_metadata, dict) else {},
            "preset_selection": normalized_selection,
            "preset_adaptive": adaptive_encoder,
            "preset_preferences": preferences,
            "queued_preset_name": queued_preset_name,
            "preset_revision": 1,
            "encoding_policy": queued_encoding_policy,
            "operations": queued_operations,
            "job_type": queued_operations["job_type"],
            "parent_job_id": str(parent_job_id or metadata_source.get("parent_job_id") or ""),
        }
    jobs[job_id] = {
        "status": "queued",
        "src": src,
        "preset": preset,
        "extra_args": extra_args or "",
        "mode": "auto_node" if automatic_dispatch else "local",
        "dispatch_mode": "auto" if automatic_dispatch else "local",
        "dispatch_plan": dispatch_plan,
        "dispatch_node_id": "",
        "dispatch_node_name": "Next available node" if automatic_dispatch else "Main controller",
        "dispatch_error": "",
        "dispatch_attempts": 0,
        "dispatch_retry_at": 0.0,
        "transfer": None,
        "preset_bundle": normalized_bundle,
        "preset_selection": normalized_selection,
        "preset_adaptive": adaptive_encoder,
        "preset_preferences": preferences,
        "preset_snapshot_locked": True,
        "preset_revision": 1,
        "queued_preset_name": queued_preset_name,
        "preset_adaptation": (
            metadata_source.get("preset_adaptation")
            if isinstance(metadata_source.get("preset_adaptation"), dict)
            else None
        ),
        "encoding_policy": queued_encoding_policy,
        "encode_method": method.get("encode_method"),
        "encoder": method.get("encoder"),
        "video_codec": method.get("video_codec"),
        "encoder_family": method.get("encoder_family"),
        "bit_depth": method.get("bit_depth"),
        "job_type": queued_operations["job_type"],
        "operations": queued_operations,
        "parent_job_id": str(parent_job_id or metadata_source.get("parent_job_id") or ""),
        "audio_inventory": None,
        "output_inventory": None,
        "storage_breakdown": None,
        **_job_learning_metadata(encode_metadata),
        "log": "",
        "returncode": None,
        "pid": None,
        "progress": 0.0,
        "eta_seconds": None,  # if you already added ETA support
        "estimated_out_bytes": planned_out_bytes,
        "planned_out_bytes": planned_out_bytes,
        "estimated_out_source": "planned" if planned_out_bytes else "",
        "estimated_out_current_bytes": None,
        "estimated_out_checked_progress": None,
        "estimated_out_updated_at": None,
        "estimate_checkpoints_seen": [],
        # Storage tracking (filled on completion)
        "src_bytes": None,
        "out_bytes": None,
        "saved_bytes": None,
        "out_path": None,
        "is_hdr": bool(
            metadata_source.get("is_hdr")
            or ((metadata_episode_plan.get("source") or {}).get("is_hdr"))
            or _looks_like_hdr_path(src)
        ),
        "created_at": _now_ts(),
        "started_at": None,
        "finished_at": None,
        "duration_seconds": None,
        "phase": "waiting_for_node" if automatic_dispatch else "queued",
        "error_message": "",
    }
    job_queue.append(job_id)
    save_jobs()
    log_event(
        "job_queued",
        (
            f"Queued for next available node: {os.path.basename(src)} "
            f"({preset}, {method.get('encode_method') or 'preset'})"
            if automatic_dispatch
            else f"Queued: {os.path.basename(src)} ({preset}, {method.get('encode_method') or 'preset'})"
        ),
        job_id=job_id,
        src=src,
    )
    ensure_dispatcher()
    return job_id


def get_next_auto_dispatch_job() -> tuple[str, dict] | None:
    """Return the oldest queueable automatic job.

    Pinned controller jobs reserve the controller, but they must not create
    head-of-line blocking for work that is allowed to run on another node.
    Automatic jobs still keep FIFO order relative to each other: a retry delay
    on the oldest automatic job prevents a newer automatic job from passing it.
    """
    now = _now_ts()
    with DISPATCH_LOCK:
        for job_id in list(job_queue):
            job = jobs.get(job_id)
            if not job:
                continue
            if job.get("status") != "queued":
                continue
            if job.get("mode") != "auto_node":
                continue
            if float(job.get("dispatch_retry_at") or 0.0) > now:
                return None
            return job_id, dict(job)
    return None


def _queued_local_job_precedes(job_id: str) -> bool:
    """Return whether older queued work has reserved the controller."""
    for queued_id in list(job_queue):
        if queued_id == job_id:
            return False
        queued_job = jobs.get(queued_id)
        if not queued_job or queued_job.get("status") != "queued":
            continue
        if queued_job.get("mode") != "auto_node":
            return True
    return False


def _apply_planned_preset(job: dict, plan: dict, *, user_edit: bool = False) -> None:
    """Apply one fully snapshotted plan without re-resolving it at run time."""
    metadata = plan.get("encode_metadata") if isinstance(plan.get("encode_metadata"), dict) else {}
    preset = str(plan.get("preset") or job.get("preset") or "1080")
    bundle = _normalize_preset_bundle(plan.get("preset_bundle")) or snapshot_preset_bundle(preset)
    method = _encode_metadata_from_extra_args(str(plan.get("extra_args") or ""), preset, metadata)
    selection = str(plan.get("preset_selection") or job.get("preset_selection") or preset).strip().lower()
    adaptive = bool(plan.get("preset_adaptive") or selection == "smart")
    queued_preset_name = _queued_preset_display_name(
        preset,
        bundle,
        extra_args=str(plan.get("extra_args") or ""),
        encode_metadata=metadata,
        preset_selection=selection,
        preset_adaptive=adaptive,
    )
    queued_operations = _queued_operations(
        metadata,
        plan.get("operations") if isinstance(plan.get("operations"), dict) else job.get("operations"),
    )
    planned_out_bytes = _planned_output_estimate(metadata, queued_operations)
    job.update({
        "preset": preset,
        "extra_args": str(plan.get("extra_args") or ""),
        "preset_bundle": bundle,
        "encode_method": method.get("encode_method"),
        "encoder": method.get("encoder"),
        "video_codec": method.get("video_codec"),
        "encoder_family": method.get("encoder_family"),
        "bit_depth": method.get("bit_depth"),
        "job_type": queued_operations["job_type"],
        "operations": queued_operations,
        "parent_job_id": str(plan.get("parent_job_id") or metadata.get("parent_job_id") or job.get("parent_job_id") or ""),
        "queued_preset_name": queued_preset_name,
        "planned_out_bytes": planned_out_bytes,
        "estimated_out_bytes": planned_out_bytes,
        "estimated_out_source": "planned" if planned_out_bytes else "",
        "estimated_out_current_bytes": None,
        "estimated_out_checked_progress": None,
        "estimated_out_updated_at": _now_ts() if planned_out_bytes else None,
        "estimate_checkpoints_seen": [],
        **_job_learning_metadata(metadata),
    })
    if isinstance(plan.get("preset_adaptation"), dict):
        job["preset_adaptation"] = plan["preset_adaptation"]
    if user_edit:
        job.update({
            "preset_selection": selection,
            "preset_adaptive": adaptive,
            "preset_preferences": plan.get("preset_preferences") if isinstance(plan.get("preset_preferences"), dict) else {},
            "preset_snapshot_locked": True,
            "preset_revision": max(1, int(job.get("preset_revision") or 1)) + 1,
            "queued_preset_name": queued_preset_name,
            "preset_adaptation": None,
            "preset_last_edited_at": _now_ts(),
        })


def replace_queued_job_preset(job_id: str, plan: dict) -> tuple[bool, str | None]:
    """Replace a queued job's immutable preset only after an explicit edit."""
    with DISPATCH_LOCK:
        job = jobs.get(job_id)
        if not job:
            return False, "job not found"
        if job.get("status") != "queued":
            return False, "only queued jobs can have their preset edited"
        if not isinstance(plan, dict):
            return False, "invalid preset plan"
        _apply_planned_preset(job, plan, user_edit=True)
        if job.get("mode") == "auto_node":
            job["dispatch_plan"] = {
                "preset": job.get("preset"),
                "preset_bundle": job.get("preset_bundle"),
                "extra_args": job.get("extra_args") or "",
                "encode_metadata": plan.get("encode_metadata") if isinstance(plan.get("encode_metadata"), dict) else {},
                "preset_selection": job.get("preset_selection"),
                "preset_adaptive": bool(job.get("preset_adaptive")),
                "preset_preferences": job.get("preset_preferences") if isinstance(job.get("preset_preferences"), dict) else {},
                "queued_preset_name": job.get("queued_preset_name") or "",
                "preset_revision": max(1, int(job.get("preset_revision") or 1)),
                "encoding_policy": _job_encoding_policy(job),
                "operations": job.get("operations"),
                "job_type": job.get("job_type"),
                "parent_job_id": job.get("parent_job_id") or "",
            }
        job["dispatch_error"] = ""
        job["error_message"] = ""
        job["dispatch_retry_at"] = 0.0
    save_jobs()
    DISPATCH_WAKE_EVENT.set()
    return True, None


def auto_dispatch_local_available(job_id: str) -> bool:
    """Return whether the main controller can start this automatic job now."""
    with DISPATCH_LOCK:
        next_job = get_next_auto_dispatch_job()
        if not next_job or next_job[0] != job_id:
            return False
        job = jobs.get(job_id)
        if not job:
            return False
        # A later Next Available job may pass pinned work only by using a
        # different node.  Do not let it consume the controller reserved by an
        # older local/remote-transfer queue entry.
        if _queued_local_job_precedes(job_id):
            return False
        running_jobs = [
            jobs[running_id]
            for running_id, thread in RUNNING_JOB_THREADS.items()
            if (
                running_id in jobs
                and thread.is_alive()
                and jobs[running_id].get("status") == "running"
            )
        ]
        return _can_dispatch_job(
            job,
            running_jobs,
            _hardware_transcode_limit(job=job),
        )


def claim_auto_dispatch_job(job_id: str, node_id: str, node_name: str) -> dict | None:
    """Atomically claim an automatic job before a remote queue request."""
    with DISPATCH_LOCK:
        next_job = get_next_auto_dispatch_job()
        if not next_job or next_job[0] != job_id:
            return None
        job = jobs.get(job_id)
        if not job:
            return None
        job["status"] = "dispatching"
        job["phase"] = "dispatching_to_node"
        job["dispatch_node_id"] = str(node_id or "")
        job["dispatch_node_name"] = str(node_name or "Worker")
        job["dispatch_error"] = ""
        job["error_message"] = ""
        job["dispatch_attempts"] = int(job.get("dispatch_attempts") or 0) + 1
        snapshot = dict(job)
    save_jobs()
    return snapshot


def release_auto_dispatch_job(job_id: str, error: str, retry_seconds: float = 3.0) -> bool:
    """Put a failed remote claim back into the automatic queue."""
    with DISPATCH_LOCK:
        job = jobs.get(job_id)
        if not job or job.get("mode") != "auto_node" or job.get("status") != "dispatching":
            return False
        message = str(error or "worker dispatch failed")[:300]
        job["status"] = "queued"
        job["phase"] = "waiting_for_node"
        job["dispatch_node_id"] = ""
        job["dispatch_node_name"] = "Next available node"
        job["dispatch_error"] = message
        job["error_message"] = message
        job["dispatch_retry_at"] = _now_ts() + max(0.5, float(retry_seconds or 0.5))
    save_jobs()
    DISPATCH_WAKE_EVENT.set()
    return True


def complete_auto_dispatch_job(job_id: str) -> bool:
    """Remove the controller placeholder after a worker accepted the job."""
    with DISPATCH_LOCK:
        job = jobs.get(job_id)
        if not job or job.get("mode") != "auto_node" or job.get("status") != "dispatching":
            return False
        try:
            job_queue.remove(job_id)
        except ValueError:
            pass
        jobs.pop(job_id, None)
    save_jobs()
    DISPATCH_WAKE_EVENT.set()
    return True


def activate_auto_dispatch_locally(
    job_id: str,
    node_id: str = "",
    node_name: str = "Main controller",
    dispatch_plan: dict | None = None,
) -> bool:
    """Convert an automatic placeholder into a normal local queued job."""
    with DISPATCH_LOCK:
        next_job = get_next_auto_dispatch_job()
        if not next_job or next_job[0] != job_id:
            return False
        job = jobs.get(job_id)
        if not job:
            return False
        # Repeat the reservation check while holding the claim lock so a race
        # cannot move this job onto the controller after availability changed.
        if _queued_local_job_precedes(job_id):
            return False
        if isinstance(dispatch_plan, dict):
            _apply_planned_preset(job, dispatch_plan)
            job["dispatch_plan"] = dispatch_plan
        job["mode"] = "local"
        job["status"] = "queued"
        job["phase"] = "queued"
        job["dispatch_node_id"] = str(node_id or "")
        job["dispatch_node_name"] = str(node_name or "Main controller")
        job["dispatch_error"] = ""
        job["error_message"] = ""
        job["dispatch_retry_at"] = 0.0
    save_jobs()
    DISPATCH_WAKE_EVENT.set()
    return True



def create_jobs_batch(files_and_presets: list[tuple[str, str]]) -> int:
    """
    Create a batch of jobs (used for folder / recursive batch encode).

    For each (src, preset):
      - If there is already an "active" job (queued/running) for src,
        we skip creating a duplicate.
      - We also skip duplicates within the same batch call.

    Args:
        files_and_presets (list[(src, preset)]):
            List of tuples, each containing:
                - src: str (absolute path to file)
                - preset: str ("1080" or "4k")

    Returns:
        int: number of NEW jobs created (duplicates skipped)
    """
    count = 0
    seen_in_batch: set[str] = set()
    preset_snapshots: dict[str, dict | None] = {}
    queued_encoding_policy = _normalize_encoding_policy()
    queued_operations = _queued_operations()

    for src, preset in files_and_presets:
        # Avoid duplicates within the same batch call
        if src in seen_in_batch:
            continue
        seen_in_batch.add(src)

        # Skip if there is already an active job for this src
        if _find_existing_active_job_for_src(src) is not None:
            continue

        job_id = str(uuid.uuid4())
        method = _encode_metadata_from_extra_args("", preset)
        if preset not in preset_snapshots:
            preset_snapshots[preset] = snapshot_preset_bundle(preset)
        preset_bundle = preset_snapshots[preset]
        jobs[job_id] = {
            "status": "queued",
            "src": src,
            "preset": preset,
            "mode": "local",
            "transfer": None,
            "preset_bundle": preset_bundle,
            "preset_selection": preset,
            "preset_adaptive": False,
            "preset_preferences": {},
            "preset_snapshot_locked": True,
            "preset_revision": 1,
            "queued_preset_name": str((preset_bundle or {}).get("name") or preset),
            "preset_adaptation": None,
            "encoding_policy": queued_encoding_policy,
            "encode_method": method.get("encode_method"),
            "encoder": method.get("encoder"),
            "video_codec": method.get("video_codec"),
            "encoder_family": method.get("encoder_family"),
            "bit_depth": method.get("bit_depth"),
            "job_type": queued_operations["job_type"],
            "operations": deepcopy(queued_operations),
            "parent_job_id": "",
            "audio_inventory": None,
            "output_inventory": None,
            "storage_breakdown": None,
            "log": "",
            "returncode": None,
            "pid": None,
            "progress": 0.0,
            "eta_seconds": None,  # remove if you don't use ETA
            "estimated_out_bytes": None,
            "planned_out_bytes": None,
            "estimated_out_source": "",
            "estimated_out_current_bytes": None,
            "estimated_out_checked_progress": None,
            "estimated_out_updated_at": None,
            "estimate_checkpoints_seen": [],
            "src_bytes": None,
            "out_bytes": None,
            "saved_bytes": None,
            "out_path": None,
            "is_hdr": _looks_like_hdr_path(src),
            "created_at": _now_ts(),
            "started_at": None,
            "finished_at": None,
            "duration_seconds": None,
        }
        job_queue.append(job_id)
        count += 1

        log_event(
            "job_queued",
            f"Queued: {os.path.basename(src)} ({preset})",
            job_id=job_id,
            src=src,
        )

    if count > 0:
        save_jobs()
        ensure_dispatcher()

    return count


def create_remote_transfer_job(src: str, preset: str, transfer: dict, extra_args: str = "", preset_bundle: dict | None = None, encode_metadata: dict | None = None, encoding_policy: dict | None = None, operations: dict | None = None, parent_job_id: str = "") -> tuple[str, bool]:
    """
    Queue a job whose source is downloaded from a paired controller/storage node.

    The visible src remains the original controller path, but HandBrake runs
    against a temporary local copy created when the job starts.
    """
    transfer = _apply_observed_controller_route(transfer)
    display_src = str(src or transfer.get("original_path") or transfer.get("source_basename") or "").strip()
    existing_id = _find_existing_active_job_for_src(display_src)
    if existing_id is not None:
        return existing_id, False

    job_id = str(uuid.uuid4())
    clean_transfer = {
        "id": str(transfer.get("id") or transfer.get("transfer_id") or "").strip(),
        "controller_id": str(transfer.get("controller_id") or "").strip(),
        "controller_url": str(transfer.get("controller_url") or "").strip().rstrip("/"),
        "source_url": str(transfer.get("source_url") or "").strip(),
        "upload_url": str(transfer.get("upload_url") or "").strip(),
        "download_token": str(transfer.get("download_token") or "").strip(),
        "upload_token": str(transfer.get("upload_token") or "").strip(),
        "worker_node_id": str(transfer.get("worker_node_id") or "").strip(),
        "original_path": display_src,
        "source_basename": _safe_transfer_filename(transfer.get("source_basename") or os.path.basename(display_src)),
        "source_size": int(transfer.get("source_size") or 0),
        "remote_temp_dir": str(transfer.get("remote_temp_dir") or "").strip()[:500],
        "encode_metadata": transfer.get("encode_metadata") if isinstance(transfer.get("encode_metadata"), dict) else {},
        "status": "queued",
        "retry_count": 0,
        "next_retry_at": 0,
        "last_error": "",
    }
    method = _encode_metadata_from_extra_args(extra_args, preset, encode_metadata or transfer.get("encode_metadata"))
    learning_metadata = encode_metadata or transfer.get("encode_metadata")
    queued_operations = _queued_operations(learning_metadata, operations)
    planned_out_bytes = _planned_output_estimate(learning_metadata, queued_operations)
    learning_episode_plan = (
        (learning_metadata or {}).get("smart_episode_plan")
        if isinstance((learning_metadata or {}).get("smart_episode_plan"), dict)
        else {}
    )
    normalized_bundle = _normalize_preset_bundle(preset_bundle) or snapshot_preset_bundle(preset)
    selection = str((learning_metadata or {}).get("preset_selection") or preset).strip().lower()
    adaptive = bool((learning_metadata or {}).get("preset_adaptive") or (learning_metadata or {}).get("smart_preset"))
    queued_preset_name = _queued_preset_display_name(
        preset,
        normalized_bundle,
        extra_args=extra_args,
        encode_metadata=learning_metadata,
        preset_selection=selection,
        preset_adaptive=adaptive,
    )
    jobs[job_id] = {
        "status": "queued",
        "src": display_src,
        "preset": preset,
        "extra_args": extra_args or "",
        "mode": "remote_transfer",
        "transfer": clean_transfer,
        "preset_bundle": normalized_bundle,
        "preset_selection": selection,
        "preset_adaptive": adaptive,
        "preset_preferences": (learning_metadata or {}).get("preset_preferences") if isinstance((learning_metadata or {}).get("preset_preferences"), dict) else {},
        "preset_snapshot_locked": True,
        "preset_revision": max(1, int((learning_metadata or {}).get("preset_revision") or 1)),
        "queued_preset_name": queued_preset_name,
        "preset_adaptation": (learning_metadata or {}).get("preset_adaptation") if isinstance((learning_metadata or {}).get("preset_adaptation"), dict) else None,
        "encoding_policy": _normalize_encoding_policy(encoding_policy),
        "encode_method": method.get("encode_method"),
        "encoder": method.get("encoder"),
        "video_codec": method.get("video_codec"),
        "encoder_family": method.get("encoder_family"),
        "bit_depth": method.get("bit_depth"),
        "job_type": queued_operations["job_type"],
        "operations": queued_operations,
        "parent_job_id": str(parent_job_id or (learning_metadata or {}).get("parent_job_id") or ""),
        "audio_inventory": None,
        "output_inventory": None,
        "storage_breakdown": None,
        **_job_learning_metadata(learning_metadata),
        "log": "",
        "returncode": None,
        "pid": None,
        "progress": 0.0,
        "eta_seconds": None,
        "estimated_out_bytes": planned_out_bytes,
        "planned_out_bytes": planned_out_bytes,
        "estimated_out_source": "planned" if planned_out_bytes else "",
        "estimated_out_current_bytes": None,
        "estimated_out_checked_progress": None,
        "estimated_out_updated_at": None,
        "estimate_checkpoints_seen": [],
        "src_bytes": clean_transfer["source_size"] or None,
        "out_bytes": None,
        "saved_bytes": None,
        "out_path": None,
        "is_hdr": bool(
            (learning_metadata or {}).get("is_hdr")
            or ((learning_episode_plan.get("source") or {}).get("is_hdr"))
            or _looks_like_hdr_path(display_src)
        ),
        "created_at": _now_ts(),
        "started_at": None,
        "finished_at": None,
        "duration_seconds": None,
        "phase": "queued",
        "error_message": "",
    }
    job_queue.append(job_id)
    save_jobs()
    log_event(
        "node_transfer_job_queued",
        f"Queued remote-transfer job: {os.path.basename(display_src)} ({preset})",
        job_id=job_id,
        src=display_src,
    )
    ensure_dispatcher()
    return job_id, True



def get_job(job_id: str) -> dict | None:
    """
    Fetch a job by ID.

    Returns:
        dict | None: job dict or None if not found
    """
    return jobs.get(job_id)


def list_jobs_for_api(*, include_log_tail: bool = False) -> list[dict]:
    """
    Build a list of job dictionaries suitable for JSON responses.

    Each job dict includes:
      - id
      - src
      - preset
      - status
      - returncode
      - progress
      - eta_seconds (float | None)
      - has_log (bool)
    """
    job_items = []
    for jid, j in jobs.items():
        log_path = os.path.join(LOG_DIR, f"{jid}.log")
        has_log = os.path.isfile(log_path)

        eta_val = j.get("eta_seconds")
        if eta_val is not None:
            try:
                eta_val = float(eta_val)
            except (TypeError, ValueError):
                eta_val = None
        method = _job_encode_metadata(j)

        job_items.append(
            {
                "id": jid,
                "src": j.get("src"),
                "preset": j.get("preset"),
                "preset_name": j.get("preset_name") or "",
                "preset_selection": j.get("preset_selection") or j.get("preset") or "1080",
                "preset_adaptive": bool(j.get("preset_adaptive", False)),
                "preset_snapshot_locked": bool(j.get("preset_snapshot_locked", False)),
                "preset_revision": max(1, int(j.get("preset_revision") or 1)),
                "queued_preset_name": j.get("queued_preset_name") or str((j.get("preset_bundle") or {}).get("name") or j.get("preset") or ""),
                "preset_adaptation": j.get("preset_adaptation") if isinstance(j.get("preset_adaptation"), dict) else None,
                "encode_method": j.get("encode_method") or method.get("encode_method"),
                "encoder": j.get("encoder") or method.get("encoder"),
                "video_codec": j.get("video_codec") or method.get("video_codec"),
                "encoder_family": j.get("encoder_family") or method.get("encoder_family"),
                "bit_depth": j.get("bit_depth") or method.get("bit_depth"),
                "job_type": j.get("job_type") or "video_preserve_audio",
                "operations": j.get("operations") if isinstance(j.get("operations"), dict) else _queued_operations(),
                "parent_job_id": j.get("parent_job_id") or "",
                "audio_inventory": j.get("audio_inventory") if isinstance(j.get("audio_inventory"), dict) else None,
                "output_inventory": j.get("output_inventory") if isinstance(j.get("output_inventory"), dict) else None,
                "storage_breakdown": j.get("storage_breakdown") if isinstance(j.get("storage_breakdown"), dict) else None,
                "uses_hardware_encoder": _job_uses_hardware_encoder(j),
                **_job_learning_metadata(j),
                "mode": j.get("mode", "local"),
                "dispatch_mode": j.get("dispatch_mode") or "local",
                "dispatch_node_id": j.get("dispatch_node_id") or "",
                "dispatch_node_name": j.get("dispatch_node_name") or "",
                "node_id": j.get("dispatch_node_id") or "",
                "node_name": j.get("dispatch_node_name") or "",
                "dispatch_error": j.get("dispatch_error") or "",
                "gpu_route_error": j.get("gpu_route_error") or "",
                "cancel_process_tree_terminated": j.get("cancel_process_tree_terminated"),
                "cancel_process_detail": j.get("cancel_process_detail") or "",
                "dispatch_attempts": int(j.get("dispatch_attempts") or 0),
                "transfer": _remote_transfer_public(j.get("transfer")) if j.get("mode") == "remote_transfer" else None,
                "status": j.get("status"),
                "returncode": j.get("returncode"),
                "progress": float(j.get("progress") or 0.0),
                "eta_seconds": eta_val,
                "has_log": has_log,
                "phase": j.get("phase") or j.get("status") or "",
                "source_video": j.get("source_video") if isinstance(j.get("source_video"), dict) else {},
                "target_resolution": j.get("target_resolution") if isinstance(j.get("target_resolution"), dict) else {},
                "hardware_decode_mode": j.get("hardware_decode_mode") or "",
                "hardware_decode_requested": j.get("hardware_decode_requested") or "",
                "hardware_decode_active": j.get("hardware_decode_active"),
                "hardware_decode_reason": j.get("hardware_decode_reason") or "",
                "hardware_decode_preset_applied": bool(j.get("hardware_decode_preset_applied")),
                "error_message": j.get("error_message")
                or (
                    (j.get("transfer") or {}).get("last_error")
                    if isinstance(j.get("transfer"), dict)
                    else ""
                )
                or (
                    (j.get("transfer") or {}).get("error")
                    if isinstance(j.get("transfer"), dict)
                    else ""
                ),
                "log_tail": "",
                "estimated_out_bytes": j.get("estimated_out_bytes"),
                "planned_out_bytes": j.get("planned_out_bytes"),
                "estimated_out_source": j.get("estimated_out_source") or "",
                "estimated_out_gb": round((int(j.get("estimated_out_bytes") or 0) / (1024**3)), 3)
                if j.get("estimated_out_bytes") is not None
                else None,
                "estimated_out_current_bytes": j.get("estimated_out_current_bytes"),
                "estimated_out_checked_progress": j.get("estimated_out_checked_progress"),
                "estimated_out_updated_at": j.get("estimated_out_updated_at"),
                "auto_stop_triggered": bool(j.get("auto_stop_triggered", False)),
                "auto_stop_details": j.get("auto_stop_details") if isinstance(j.get("auto_stop_details"), dict) else None,
                "cancel_reason": j.get("cancel_reason") or "",
                # Storage tracking
                "src_bytes": j.get("src_bytes"),
                "out_bytes": j.get("out_bytes"),
                "saved_bytes": j.get("saved_bytes"),
                "saved_gb": round((int(j.get("saved_bytes") or 0) / (1024**3)), 3)
                if j.get("saved_bytes") is not None
                else None,
                "is_hdr": bool(j.get("is_hdr", False) or _looks_like_hdr_path(j.get("src", ""))),
                "created_at": j.get("created_at"),
                "started_at": j.get("started_at"),
                "finished_at": j.get("finished_at"),
                "duration_seconds": j.get("duration_seconds"),
                "queue_position": (job_queue.index(jid) + 1) if jid in job_queue and j.get("status") == "queued" else None,
            }
        )

    def _sort_key(item: dict):
        status = str(item.get("status") or "").lower()
        created = float(item.get("created_at") or 0.0)
        if status == "running":
            return (0, 0, -created, item["id"])
        if status == "queued":
            pos = int(item.get("queue_position") or 999999)
            return (1, pos, -created, item["id"])
        return (2, 0, -created, item["id"])

    # Keep the visible queue aligned with the real queue order.
    job_items.sort(key=_sort_key)
    if include_log_tail:
        included = 0
        for item in job_items:
            status = str(item.get("status") or "").lower()
            if status not in {"running", "error", "waiting_to_upload"} or included >= 8:
                continue
            source = jobs.get(str(item.get("id") or "")) or {}
            item["log_tail"] = str(source.get("log") or "")[-JOB_LOG_TAIL_CHARS:]
            included += 1
    return job_items


def _encode_history_job(row: dict, index: int) -> dict:
    """Convert one durable storage-ledger row into the Jobs API shape."""
    row = row if isinstance(row, dict) else {}

    try:
        finished_at = max(0.0, float(row.get("ts") or 0.0))
    except (TypeError, ValueError):
        finished_at = 0.0
    try:
        duration_seconds = max(0.0, float(row.get("duration_seconds") or 0.0))
    except (TypeError, ValueError):
        duration_seconds = 0.0
    try:
        src_bytes = max(0, int(row.get("src_bytes") or 0))
    except (TypeError, ValueError):
        src_bytes = 0
    try:
        out_bytes = max(0, int(row.get("out_bytes") or 0))
    except (TypeError, ValueError):
        out_bytes = 0
    try:
        saved_bytes = max(0, int(row.get("saved_bytes") or (src_bytes - out_bytes)))
    except (TypeError, ValueError):
        saved_bytes = max(0, src_bytes - out_bytes)

    job_id = str(row.get("job_id") or "").strip()
    if not job_id:
        job_id = f"history-{int(finished_at * 1000)}-{index}"
    started_at = max(0.0, finished_at - duration_seconds) if finished_at else 0.0

    return {
        "id": job_id,
        "src": row.get("src"),
        "out_path": row.get("out"),
        "preset": row.get("preset"),
        "encode_method": row.get("encode_method") or "",
        "encoder": row.get("encoder") or "",
        "video_codec": row.get("video_codec") or "",
        "encoder_family": row.get("encoder_family") or "",
        "bit_depth": row.get("bit_depth") or "",
        "job_type": row.get("job_type") or "video_preserve_audio",
        "operations": row.get("operations") if isinstance(row.get("operations"), dict) else _queued_operations(),
        "parent_job_id": row.get("parent_job_id") or "",
        "audio_inventory": row.get("input_inventory") if isinstance(row.get("input_inventory"), dict) else None,
        "output_inventory": row.get("output_inventory") if isinstance(row.get("output_inventory"), dict) else None,
        "storage_breakdown": row.get("storage_breakdown") if isinstance(row.get("storage_breakdown"), dict) else None,
        "mode": "linked_node" if row.get("node_id") else "local",
        "node_id": row.get("node_id") or "",
        "node_name": row.get("node_name") or "",
        "transfer": None,
        "status": "done",
        "returncode": 0,
        "progress": 100.0,
        "eta_seconds": None,
        # Queue log files are intentionally pruned independently of the
        # durable encode ledger. Do not advertise a download that the job-log
        # route cannot serve anymore.
        "has_log": False,
        "estimated_out_bytes": out_bytes or None,
        "planned_out_bytes": None,
        "estimated_out_source": "actual" if out_bytes else "",
        "estimated_out_gb": round(out_bytes / (1024**3), 3) if out_bytes else None,
        "estimated_out_current_bytes": None,
        "estimated_out_checked_progress": None,
        "estimated_out_updated_at": None,
        "auto_stop_triggered": False,
        "auto_stop_details": None,
        "cancel_reason": "",
        "src_bytes": src_bytes,
        "out_bytes": out_bytes,
        "saved_bytes": saved_bytes,
        "saved_gb": round(saved_bytes / (1024**3), 3) if saved_bytes else 0.0,
        "is_hdr": bool(row.get("is_hdr", False) or _looks_like_hdr_path(row.get("src", ""))),
        "created_at": started_at or finished_at,
        "started_at": started_at or None,
        "finished_at": finished_at or None,
        "duration_seconds": duration_seconds,
        "queue_position": None,
        "archived": True,
        "history_source": "encode_ledger",
    }


def list_job_history_for_api(limit: int = 5000) -> list[dict]:
    """Return active queue records plus durable completed encode history.

    ``jobs.json`` is the operational queue and may be intentionally pruned.
    Successful encodes are also written to ``storage_stats.json``; that ledger
    is the authoritative long-term source for the Job History table.
    """
    current_items = list_jobs_for_api()
    current_ids = {str(item.get("id") or "") for item in current_items}
    try:
        history_limit = max(0, min(5000, int(limit or 0)))
    except (TypeError, ValueError):
        history_limit = 5000

    archived_items = []
    for index, row in enumerate(list_encodes(limit=history_limit)):
        try:
            encoded_at = max(0.0, float(row.get("ts") or 0.0)) if isinstance(row, dict) else 0.0
        except (TypeError, ValueError):
            encoded_at = 0.0
        if history_cleared_before > 0.0 and encoded_at <= history_cleared_before:
            continue
        item = _encode_history_job(row, index)
        if item["id"] in current_ids:
            continue
        current_ids.add(item["id"])
        archived_items.append(item)

    active_states = {"queued", "running", "waiting_to_upload"}
    active_items = [
        item for item in current_items
        if str(item.get("status") or "").lower() in active_states
    ]
    terminal_items = [
        item for item in current_items
        if str(item.get("status") or "").lower() not in active_states
    ]
    terminal_items.extend(archived_items)

    def _finished_key(item: dict):
        try:
            timestamp = float(item.get("finished_at") or item.get("created_at") or 0.0)
        except (TypeError, ValueError):
            timestamp = 0.0
        return (-timestamp, str(item.get("id") or ""))

    terminal_items.sort(key=_finished_key)
    return active_items + terminal_items


# -------------------------------------------------------------------
# Dispatcher + HandBrake process runner
# -------------------------------------------------------------------

def _audio_only_paths(src_path: str, job_id: str, replace_source: bool) -> tuple[str, str]:
    folder = os.path.dirname(src_path)
    base, extension = os.path.splitext(os.path.basename(src_path))
    extension = extension if extension.lower() in {".mkv", ".mp4", ".m4v"} else ".mkv"
    temp_out = os.path.join(folder, f".{base}.bytesqueeze-audio-{job_id}.tmp{extension}")
    final_out = src_path if replace_source else os.path.join(folder, f"{base}-Audio-TSD{extension}")
    return temp_out, final_out


def _run_audio_only_job(
    job_id: str,
    job: dict,
    encode_src_path: str,
    display_src_path: str,
    source_inventory: dict,
    operations: dict,
    *,
    remote_transfer: bool,
    transfer: dict,
    transfer_work_dir: str,
    update_transfer_progress,
) -> None:
    """Remux one file with FFmpeg while video and non-audio streams are copied."""
    replace_source = bool(operations.get("replace_source", True)) and not remote_transfer
    temp_out, final_out = _audio_only_paths(encode_src_path, job_id, replace_source)
    job.update({
        "phase": "audio_optimizing",
        "out_path": final_out,
        "encode_method": "ffmpeg_audio_only",
        "encoder": "copy",
        "video_codec": str((source_inventory.get("video_streams") or [{}])[0].get("codec") or "copy"),
        "encoder_family": "stream_copy",
        "bit_depth": "copy",
    })
    command = ffmpeg_audio_only_command(encode_src_path, temp_out, operations)
    action_summary = ", ".join(
        f"a:{row.get('audio_ordinal')}={row.get('action')}"
        + (f"/{row.get('target_codec')}@{row.get('bitrate_kbps')}k/{row.get('channels')}ch" if row.get("action") == "encode" else "")
        for row in operations.get("audio_actions", [])
    )
    _append_job_log(
        job_id,
        (
            "[ByteSqueeze] Job type: Audio Optimization Only\n"
            "[ByteSqueeze] Video: COPY (FFmpeg stream copy; no re-encode)\n"
            "[ByteSqueeze] Subtitles, attachments, chapters, and metadata: COPY\n"
            f"[ByteSqueeze] Audio actions: {action_summary or 'no audio tracks'}\n"
            f"[ByteSqueeze] Estimated audio savings: {int((operations.get('estimate') or {}).get('audio_savings_bytes') or 0)} bytes"
        ),
    )
    save_jobs()
    try:
        if os.path.exists(temp_out):
            os.remove(temp_out)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **_process_group_options(),
        )
        job["pid"] = process.pid
        duration = max(0.0, float(source_inventory.get("duration_seconds") or 0.0))
        assert process.stdout is not None
        for line in process.stdout:
            _append_job_log(job_id, line.rstrip())
            if line.startswith(("out_time_us=", "out_time_ms=")) and duration:
                value = line.split("=", 1)[-1].strip()
                try:
                    job["progress"] = min(99.5, max(0.0, (float(value) / 1_000_000.0) / duration * 100.0))
                except (TypeError, ValueError):
                    pass
            if job.get("status") == "canceled":
                terminated, detail = terminate_process_tree(process.pid, force=True)
                job["cancel_process_tree_terminated"] = bool(terminated)
                job["cancel_process_detail"] = str(detail or "")[:500]
                break
        returncode = process.wait()
        job["returncode"] = returncode
        if job.get("status") == "canceled":
            raise RuntimeError("audio optimization was canceled")
        if returncode != 0:
            raise RuntimeError(f"FFmpeg exited with code {returncode}: {_job_error_excerpt(job)}")
        output_ok, output_reason = _encoded_output_is_valid(temp_out)
        if not output_ok:
            raise RuntimeError(f"audio output validation failed: {output_reason}")
        job["phase"] = "validating_stream_copy"
        output_inventory = scan_media(temp_out, exact=True, force=True)
        valid, validation_errors = validate_audio_only(source_inventory, output_inventory, operations)
        if not valid:
            raise RuntimeError("audio-only safeguards failed: " + "; ".join(validation_errors))
        input_total = int(job.get("src_bytes") or os.path.getsize(encode_src_path))
        output_total = int(os.path.getsize(temp_out))
        breakdown = storage_breakdown(source_inventory, output_inventory, input_total, output_total)
        job["output_inventory"] = output_inventory
        job["storage_breakdown"] = breakdown
        job["out_bytes"] = output_total
        job["saved_bytes"] = max(0, input_total - output_total)
        job["estimated_out_bytes"] = output_total
        job["estimated_out_source"] = "actual"

        if remote_transfer:
            transfer.update({"local_out": temp_out, "work_dir": transfer_work_dir, "status": "uploading"})
            job.update({"local_out_path": temp_out, "phase": "uploading", "transfer": transfer})
            save_jobs()
            try:
                upload_result = _upload_transfer_output(
                    transfer.get("upload_url") or "",
                    transfer.get("upload_token") or "",
                    transfer.get("worker_node_id") or "",
                    temp_out,
                    job_id=job_id,
                    duration_seconds=max(0.0, _now_ts() - float(job.get("started_at") or _now_ts())),
                    progress_callback=update_transfer_progress,
                )
                _apply_remote_upload_success(job_id, job, upload_result, temp_out)
            except Exception as upload_error:
                _mark_transfer_waiting(job_id, job, upload_error)
        else:
            if replace_source:
                backup = f"{encode_src_path}.bytesqueeze-audio-backup-{job_id}"
                os.replace(encode_src_path, backup)
                try:
                    os.replace(temp_out, encode_src_path)
                except Exception:
                    os.replace(backup, encode_src_path)
                    raise
                try:
                    os.remove(backup)
                except OSError:
                    pass
                final_out = encode_src_path
            else:
                if os.path.exists(final_out):
                    raise RuntimeError(f"output already exists: {final_out}")
                os.replace(temp_out, final_out)
            output_inventory["path"] = final_out
            job.update({"status": "done", "phase": "done", "progress": 100.0, "out_path": final_out, "output_inventory": output_inventory})
            try:
                from .node_linking import local_node_info
                local_node = local_node_info()
            except Exception:
                local_node = {}
            record_encode(
                job_id=job_id,
                src=display_src_path,
                out=final_out,
                preset="audio-only",
                src_bytes=input_total,
                out_bytes=output_total,
                duration_seconds=max(0.0, _now_ts() - float(job.get("started_at") or _now_ts())),
                is_hdr=bool(job.get("is_hdr")),
                node_id=local_node.get("id"),
                node_name=local_node.get("name"),
                encode_method="ffmpeg_audio_only",
                encoder="copy",
                video_codec=job.get("video_codec"),
                encoder_family="stream_copy",
                bit_depth="copy",
                job_type="audio_only",
                operations=operations,
                parent_job_id=job.get("parent_job_id"),
                input_inventory=source_inventory,
                output_inventory=output_inventory,
                storage_breakdown=breakdown,
            )
            _append_job_log(job_id, "[ByteSqueeze] Audio optimization completed; video stream copy verified.")
    except Exception as exc:
        if job.get("status") != "canceled":
            job.update({"status": "error", "phase": "audio_validation_error", "error_message": str(exc)[:500]})
        _append_job_log(job_id, f"[ByteSqueeze] ERROR: {exc}")
        if os.path.isfile(temp_out) and job.get("status") != "waiting_to_upload":
            try:
                os.remove(temp_out)
            except OSError:
                pass
    finally:
        job["pid"] = None
        job["eta_seconds"] = None
        job["finished_at"] = _now_ts()
        try:
            job["duration_seconds"] = max(0.0, job["finished_at"] - float(job.get("started_at") or job["finished_at"]))
        except Exception:
            job["duration_seconds"] = None
        if remote_transfer and job.get("status") != "waiting_to_upload":
            transfer = job.get("transfer") if isinstance(job.get("transfer"), dict) else transfer
            for key in ("download_token", "upload_token", "local_src", "local_out", "work_dir"):
                transfer.pop(key, None)
            job["transfer"] = transfer
            job["local_out_path"] = None
            _cleanup_transfer_work_dir(transfer_work_dir, transfer)
        save_jobs()


def run_encode(job_id: str, src_path: str, preset_key: str):
    """
    Run a single HandBrake encode (called by dispatcher).

    - Sets job status to "running"
    - Spawns /worker/encode-one.sh with proper env vars
    - Streams output into a log file & memory
    - Parses progress using PROGRESS_RE
    - Parses ETA using ETA_RE
    - Handles cancellation (if job["status"] is set to "canceled")
    - Updates final status to "done" or "error"
    """
    job = jobs[job_id]
    display_src_path = src_path
    encode_src_path = src_path
    transfer = job.get("transfer") if isinstance(job.get("transfer"), dict) else {}
    remote_transfer = job.get("mode") == "remote_transfer" and bool(transfer)
    transfer_work_dir = ""
    preset_work_dir = ""
    log_path = _job_log_path(job_id)

    job["status"] = "running"
    job["phase"] = "preparing"
    job["error_message"] = ""
    job["progress"] = 0.0
    job["started_at"] = _now_ts()
    job["finished_at"] = None
    job["duration_seconds"] = None
    job["eta_seconds"] = None  # reset ETA at the start
    method = _job_encode_metadata(job)
    job["encode_method"] = method.get("encode_method")
    job["encoder"] = method.get("encoder")
    job["video_codec"] = method.get("video_codec")
    job["encoder_family"] = method.get("encoder_family")
    job["bit_depth"] = method.get("bit_depth")
    # Reset storage tracking fields for this run
    job["src_bytes"] = None
    job["out_bytes"] = None
    job["saved_bytes"] = None
    job["out_path"] = None
    job["is_hdr"] = bool(job.get("is_hdr", False) or _looks_like_hdr_path(display_src_path))
    # Keep the planner's estimate visible while HandBrake starts. A live
    # projection replaces it once the output file has measurable bytes.
    planned_out_bytes = job.get("planned_out_bytes")
    job["estimated_out_bytes"] = planned_out_bytes
    job["estimated_out_source"] = "planned" if planned_out_bytes else ""
    job["estimated_out_current_bytes"] = None
    job["estimated_out_checked_progress"] = None
    job["estimated_out_updated_at"] = None
    job["estimate_checkpoints_seen"] = []
    _append_job_log(
        job_id,
        (
            f"[ByteSqueeze] Job {job_id} starting\n"
            f"[ByteSqueeze] Mode: {'remote transfer' if remote_transfer else 'local'}\n"
            f"[ByteSqueeze] Source: {display_src_path}\n"
            f"[ByteSqueeze] Preset: {preset_key}"
        ),
    )
    print(
        f"[JOB {job_id}] starting {os.path.basename(display_src_path)} "
        f"({preset_key}, {'remote' if remote_transfer else 'local'})",
        flush=True,
    )

    transfer_progress_state = {"phase": "", "started_at": _now_ts(), "last_save": 0.0}

    def update_transfer_progress(phase: str, transferred: int, total: int, force: bool = False) -> None:
        if not remote_transfer:
            return
        now = _now_ts()
        if phase != transfer_progress_state.get("phase"):
            transfer_progress_state["phase"] = phase
            transfer_progress_state["started_at"] = now
            transfer_progress_state["last_save"] = 0.0
        transfer["status"] = phase
        transfer["progress"] = _transfer_progress_payload(
            phase,
            int(transferred or 0),
            int(total or 0),
            float(transfer_progress_state.get("started_at") or now),
        )
        job["transfer"] = transfer
        if force or now - float(transfer_progress_state.get("last_save") or 0.0) >= 1.0:
            transfer_progress_state["last_save"] = now
            save_jobs()

    def log_download_attempt(message: str) -> None:
        _append_job_log(job_id, f"[ByteSqueeze] {message}")
        print(f"[JOB {job_id}] {message}", flush=True)

    if remote_transfer:
        try:
            transfer_work_dir = os.path.join(_remote_transfer_temp_root(transfer), job_id)
            basename = _safe_transfer_filename(transfer.get("source_basename") or os.path.basename(display_src_path))
            encode_src_path = os.path.join(transfer_work_dir, basename)
            job["phase"] = "downloading"
            _append_job_log(job_id, "[ByteSqueeze] Downloading source from controller...")
            transfer["status"] = "downloading"
            transfer["work_dir"] = transfer_work_dir
            transfer["local_src"] = encode_src_path
            job["transfer"] = transfer
            save_jobs()
            downloaded_size = _download_transfer_source(
                transfer.get("source_url") or "",
                transfer.get("download_token") or "",
                transfer.get("worker_node_id") or "",
                encode_src_path,
                int(transfer.get("source_size") or 0),
                progress_callback=update_transfer_progress,
                attempt_callback=log_download_attempt,
            )
            job["src_bytes"] = downloaded_size
            _append_job_log(
                job_id,
                f"[ByteSqueeze] Source download complete ({downloaded_size} bytes).",
            )
            transfer["status"] = "downloaded"
            update_transfer_progress("downloaded", downloaded_size, int(transfer.get("source_size") or downloaded_size), force=True)
            job["transfer"] = transfer
            save_jobs()
        except Exception as e:
            job["status"] = "error"
            job["phase"] = "download_error"
            job["returncode"] = None
            job["error_message"] = f"Remote source download failed: {e}"[:500]
            _append_job_log(job_id, f"[ByteSqueeze] ERROR: {job['error_message']}")
            print(f"[JOB {job_id}] {job['error_message']}", flush=True)
            transfer["status"] = "error"
            transfer["error"] = str(e)[:240]
            job["transfer"] = transfer
            log_event(
                "node_transfer_error",
                f"Remote source download failed: {os.path.basename(display_src_path)} ({e})",
                level="error",
                job_id=job_id,
                src=display_src_path,
            )
            job["eta_seconds"] = None
            job["finished_at"] = _now_ts()
            job["pid"] = None
            transfer.pop("download_token", None)
            transfer.pop("upload_token", None)
            transfer.pop("local_src", None)
            transfer.pop("work_dir", None)
            job["transfer"] = transfer
            _cleanup_transfer_work_dir(transfer_work_dir, transfer)
            save_jobs()
            return

    # Capture source size before the success path deletes the original.
    try:
        if job.get("src_bytes") is None:
            job["src_bytes"] = int(os.path.getsize(encode_src_path))
    except Exception:
        job["src_bytes"] = None

    # Inventory every stream before launching either HandBrake or FFmpeg.  An
    # unavailable inventory is a hard safety stop: ByteSqueeze will never run
    # a normal job without knowing which audio tracks its policy affects.
    job["phase"] = "scanning_audio"
    try:
        # Stream metadata is authoritative for safety. Track sizes shown before
        # the encode may be sampled estimates so a NAS file is not read twice
        # before HandBrake/FFmpeg can start.
        source_inventory = scan_media(encode_src_path, exact=False)
        operations = normalize_operations(job.get("operations"), source_inventory)
        job["audio_inventory"] = source_inventory
        job["operations"] = operations
        job["job_type"] = operations["job_type"]
        estimate = operations.get("estimate") if isinstance(operations.get("estimate"), dict) else {}
        _append_job_log(
            job_id,
            (
                f"[ByteSqueeze] Audio inventory: {len(source_inventory.get('audio_streams') or [])} tracks, "
                f"{int((source_inventory.get('aggregate') or {}).get('audio_bytes') or 0)} bytes\n"
                f"[ByteSqueeze] Audio policy: {operations.get('audio_policy')}\n"
                f"[ByteSqueeze] Estimated audio: {int(estimate.get('current_audio_bytes') or 0)} -> "
                f"{int(estimate.get('output_audio_bytes') or 0)} bytes "
                f"({float(estimate.get('audio_savings_percent') or 0):.1f}% potential savings)"
            ),
        )
        for warning in operations.get("warnings") or []:
            _append_job_log(job_id, f"[ByteSqueeze] Audio warning: {warning}")
        save_jobs()
    except Exception as exc:
        job.update({
            "status": "error",
            "phase": "audio_scan_error",
            "error_message": f"Audio inventory failed; encode not started: {exc}"[:500],
            "finished_at": _now_ts(),
            "pid": None,
        })
        _append_job_log(job_id, f"[ByteSqueeze] ERROR: {job['error_message']}")
        if remote_transfer:
            _cleanup_transfer_work_dir(transfer_work_dir, transfer)
        save_jobs()
        return

    if operations.get("job_type") == "audio_only":
        _run_audio_only_job(
            job_id,
            job,
            encode_src_path,
            display_src_path,
            source_inventory,
            operations,
            remote_transfer=remote_transfer,
            transfer=transfer,
            transfer_work_dir=transfer_work_dir,
            update_transfer_progress=update_transfer_progress,
        )
        return

    # Predict output path (must match worker/encode-one.sh)
    try:
        suffix = (os.environ.get("SUFFIX") or "TSD").strip() or "TSD"
    except Exception:
        suffix = "TSD"
    job_policy = _job_encoding_policy(job)

    log_event(
        "job_started",
        f"Started: {os.path.basename(display_src_path)} ({preset_key})",
        job_id=job_id,
        src=display_src_path,
    )
    save_jobs()

    # ------------------------------------------------------------
    # ENVIRONMENT SETUP FOR WORKER SCRIPT
    # - SRC: path to source video (always required)
    # - HB_PRESET_FILE / HB_PRESET_NAME: resolved from preset key
    # - HB_THREADS: optional override from settings.py (Settings page)
    # ------------------------------------------------------------
    env = os.environ.copy()
    env["SRC"] = encode_src_path  # encode-one.sh uses this
    # keep SUFFIX consistent across the stack
    env["SUFFIX"] = suffix

    # CPU thread setting pulled from global Settings:
    # Settings page stores hb_threads in settings.json.
    # If hb_threads > 0, we pass it down as HB_THREADS so encode-one.sh
    # can include "--encopts threads=<N>" when calling HandBrakeCLI.
    hb_threads = int(job_policy.get("hb_threads") or 0)

    if hb_threads > 0:
        env["HB_THREADS"] = str(hb_threads)
        # Example consumed downstream:
        #   HandBrakeCLI --encopts threads=$HB_THREADS ...
        # If hb_threads == 0 (auto), we simply do not set HB_THREADS.

    # Resolve HB_PRESET_FILE + HB_PRESET_NAME based on preset key ("1080" or "4k")
    preset_override = _materialize_job_preset(job_id, job.get("preset_bundle"))
    if preset_override:
        preset_file, preset_name, preset_work_dir = preset_override
        if not preset_name:
            _default_file, preset_name = resolve_preset_file_and_name(preset_key)
    else:
        preset_file, preset_name = resolve_preset_file_and_name(preset_key)
    env["HB_PRESET_FILE"] = preset_file
    env["HB_PRESET_NAME"] = preset_name

    source_video = _source_video_probe(encode_src_path)
    smart_framerate_locked = bool(
        job.get("smart_preset")
        or str(job.get("preset_selection") or "").lower() == "smart"
        or isinstance(job.get("smart_episode_plan"), dict)
    )
    launch_extra_args = str(job.get("extra_args") or "")
    if smart_framerate_locked:
        launch_extra_args = _smart_source_framerate_args(
            launch_extra_args,
            source_video.get("fps"),
        )
    selected_encoder = _selected_video_encoder(job, preset_file, preset_name)
    gpu_assignment = job.get("gpu_route_assignment") if isinstance(job.get("gpu_route_assignment"), dict) else {}
    routed_encoder = str(gpu_assignment.get("encoder") or "").strip().lower()
    if routed_encoder:
        selected_encoder = routed_encoder
        # HandBrake applies CLI values after imported preset values. Appending
        # the route keeps any user's quality/filter arguments intact while
        # making only the video encoder follow this worker's routing policy.
        launch_extra_args = (launch_extra_args + " --encoder " + shlex.quote(routed_encoder)).strip()
    try:
        preset_definition = load_preset_definition(preset_file, preset_name)
    except Exception:
        preset_definition = {}
    launch_extra_args, removed_cli_profile = _sanitize_encoder_profile_args(
        launch_extra_args,
        selected_encoder,
    )
    source_profile = str(preset_definition.get("VideoProfile") or "").strip()
    encoder_profile_log = ""
    if _uses_default_encoder_profile(selected_encoder):
        details = []
        if source_profile:
            details.append(f"preset {source_profile}")
        if removed_cli_profile:
            details.append(f"CLI {removed_cli_profile}")
        repaired = f"; removed {' and '.join(details)}" if details else ""
        encoder_profile_log = (
            "[ByteSqueeze] Encoder profile: Auto/default "
            f"(AV1 NVENC profile field omitted{repaired})\n"
        )
    output_plan = _output_container_plan(
        job_policy,
        source_video,
        job=job,
        preset_definition=preset_definition,
        selected_encoder=selected_encoder,
    )
    out_path = _output_path_for_source(
        encode_src_path,
        suffix,
        job_policy,
        plan=output_plan,
    )
    out_path_existed_before = os.path.exists(out_path)
    job["out_path"] = out_path
    job["output_container_requested"] = output_plan["requested_container"]
    job["output_container"] = output_plan["container"]
    job["output_container_reason"] = output_plan["reason"]
    job["web_optimized"] = output_plan["web_optimized"]
    resolution_plan = _resolution_plan(
        preset_key,
        source_video,
        job.get("extra_args") or "",
    )
    hardware_decode = _hardware_decode_plan(selected_encoder, source_video)
    controlled_preset = _materialize_decode_policy_preset(
        job_id,
        preset_file,
        preset_name,
        hardware_decode["enabled"],
        preset_work_dir,
        # Always snapshot the effective encoder. A CLI/manual NVENC selection
        # can otherwise inherit an incompatible profile from the base preset
        # even when no Windows GPU route rewrite was required.
        video_encoder=selected_encoder,
        gpu_index=(
            (
                0
                if str(gpu_assignment.get("encoder_family") or "") == "nvenc"
                and str(gpu_assignment.get("gpu_uuid") or "").strip()
                else int(gpu_assignment.get("vendor_index") or 0)
            )
        ) if routed_encoder.startswith(("nvenc_", "vce_")) else None,
    )
    if controlled_preset:
        preset_file, preset_work_dir = controlled_preset
        env["HB_PRESET_FILE"] = preset_file
    source_width = int(source_video.get("width") or 0)
    source_height = int(source_video.get("height") or 0)
    target_width = int(resolution_plan.get("target_width") or 0)
    target_height = int(resolution_plan.get("target_height") or 0)
    source_resolution_label = (
        f"{source_width}x{source_height}"
        if source_width and source_height
        else f"unknown ({source_video.get('error') or 'no video stream'})"
    )
    target_resolution_label = (
        f"{target_width}x{target_height}"
        if target_width and target_height
        else f"maximum {resolution_plan['max_width']}x{resolution_plan['max_height']}"
    )

    job["preset_name"] = preset_name
    job["encoder"] = selected_encoder or job.get("encoder") or ""
    if selected_encoder:
        actual_method = _encoder_method_from_encoder(selected_encoder, preset_key)
        job["encode_method"] = actual_method.get("encode_method")
        job["video_codec"] = actual_method.get("video_codec")
        job["encoder_family"] = actual_method.get("encoder_family")
        job["bit_depth"] = actual_method.get("bit_depth")
    job["source_video"] = source_video
    job["target_resolution"] = {
        "width": target_width,
        "height": target_height,
        "max_width": resolution_plan["max_width"],
        "max_height": resolution_plan["max_height"],
    }

    job["hardware_decode_mode"] = hardware_decode["mode"]
    job["hardware_decode_requested"] = hardware_decode["decoder"]
    job["hardware_decode_active"] = None if hardware_decode["enabled"] else False
    job["hardware_decode_reason"] = hardware_decode["reason"]
    job["hardware_decode_preset_applied"] = bool(controlled_preset)

    if controlled_preset and hardware_decode["enabled"]:
        preset_decode_policy = "VideoHWDecode=2, VideoQSVDecode=true"
    elif controlled_preset:
        preset_decode_policy = "VideoHWDecode=0, VideoQSVDecode=false"
    else:
        preset_decode_policy = "not materialized; using CLI decode option"

    # Controlled arguments are placed after user/Smart Preset arguments by
    # encode-one.sh so a logical 1080/4K choice remains a hard no-upscale cap.
    env["HB_EXTRA_ARGS"] = launch_extra_args
    env["HB_AUDIO_POLICY_OPTS"] = shlex.join(handbrake_audio_args(operations, source_inventory))
    env["HB_DIMENSION_OPTS"] = shlex.join(resolution_plan["cli_args"])
    env["HB_HW_DECODE_OPTS"] = shlex.join(hardware_decode["cli_args"])
    env["HB_OUTPUT_CONTAINER"] = output_plan["container"]
    env["HB_WEB_OPTIMIZED"] = "1" if output_plan["web_optimized"] else "0"
    env["HB_HW_DECODE_LABEL"] = hardware_decode["label"]
    env["HB_VIDEO_ENCODER"] = selected_encoder or "unknown"
    env["HB_SOURCE_RESOLUTION"] = source_resolution_label
    env["HB_TARGET_RESOLUTION"] = target_resolution_label
    qsv_adapter = (
        int(gpu_assignment.get("vendor_index") or 0)
        if str(gpu_assignment.get("encoder_family") or "") == "qsv"
        else _qsv_adapter_index()
    )
    qsv_render_device = str(os.environ.get("TSD_QSV_RENDER_DEVICE") or "/dev/dri/renderD128")
    qsv_va_driver = str(os.environ.get("LIBVA_DRIVER_NAME") or "iHD")
    qsv_job = selected_encoder.startswith("qsv_") or hardware_decode["enabled"]
    if qsv_job:
        env["TSD_QSV_ADAPTER"] = str(qsv_adapter)
        env["TSD_QSV_RENDER_DEVICE"] = qsv_render_device
        env["LIBVA_DRIVER_NAME"] = qsv_va_driver
        job["qsv_adapter"] = qsv_adapter
        job["qsv_render_device"] = qsv_render_device
        job["qsv_va_driver"] = qsv_va_driver
    routed_family = str(gpu_assignment.get("encoder_family") or "")
    routed_vendor_index = int(gpu_assignment.get("vendor_index") or 0) if gpu_assignment else 0
    if routed_family == "nvenc":
        selected_uuid = str(gpu_assignment.get("gpu_uuid") or "").strip()
        if selected_uuid:
            # Isolate by UUID, never by ByteSqueeze/WMI ordinal. HandBrake
            # can ignore gpu=N with multiple NVIDIA cards, but CUDA
            # honors this stable physical identity and exposes it as gpu=0.
            env["CUDA_VISIBLE_DEVICES"] = selected_uuid
            env["NVIDIA_VISIBLE_DEVICES"] = selected_uuid
            env["TSD_NVENC_LOGICAL_GPU_INDEX"] = "0"
        else:
            env.pop("CUDA_VISIBLE_DEVICES", None)
            env.pop("NVIDIA_VISIBLE_DEVICES", None)
            env["TSD_NVENC_LOGICAL_GPU_INDEX"] = str(routed_vendor_index)
        env["TSD_NVENC_GPU_INDEX"] = str(routed_vendor_index)
        env["TSD_NVENC_GPU_UUID"] = str(gpu_assignment.get("gpu_uuid") or "")
        env["TSD_NVENC_PCI_BUS_ID"] = str(gpu_assignment.get("pci_bus_id") or "")
    elif routed_family == "vce":
        env["GPU_DEVICE_ORDINAL"] = str(routed_vendor_index)
        env["HIP_VISIBLE_DEVICES"] = str(routed_vendor_index)
    episode_plan = job.get("smart_episode_plan") if isinstance(job.get("smart_episode_plan"), dict) else {}
    episode_source = episode_plan.get("source") if isinstance(episode_plan.get("source"), dict) else {}
    episode_target = episode_plan.get("target") if isinstance(episode_plan.get("target"), dict) else {}
    episode_floor = episode_plan.get("quality_floor") if isinstance(episode_plan.get("quality_floor"), dict) else {}
    episode_scene = episode_plan.get("scene_analysis") if isinstance(episode_plan.get("scene_analysis"), dict) else {}
    smart_episode_log = ""
    if episode_plan:
        scene_mode = (
            f"AI {episode_scene.get('provider') or 'provider'}"
            if episode_scene.get("used")
            else f"deterministic ({episode_scene.get('reason') or 'scene AI disabled'})"
        )
        scene_summary = str(episode_scene.get("summary") or "").strip()
        smart_episode_log = (
            f"[ByteSqueeze] Smart episode plan: independent snapshot {str(episode_plan.get('fingerprint') or '')[:16]}\n"
            f"[ByteSqueeze] HDR protection: {'on' if episode_source.get('is_hdr') else 'off'}"
            f" ({episode_source.get('hdr_reason') or 'SDR metadata'})\n"
            f"[ByteSqueeze] Episode target: {episode_target.get('target_mb') or 'unknown'} MB; "
            f"quality floor {episode_floor.get('target_mb') or 'unknown'} MB\n"
            f"[ByteSqueeze] Episode scene analysis: {scene_mode}"
            f"{f' — {scene_summary}' if scene_summary else ''}\n"
        )
    frame_rate_log = ""
    if smart_framerate_locked:
        source_fps = _format_handbrake_fps(source_video.get("fps"))
        frame_rate_log = (
            f"[ByteSqueeze] Source frame rate: {source_fps or 'source average'} fps\n"
            "[ByteSqueeze] Frame rate policy: CFR locked to source average "
            "(no scene-to-scene FPS changes)\n"
        )
    qsv_diagnostics_log = (
        f"[ByteSqueeze] Selected render device: {qsv_render_device}\n"
        f"[ByteSqueeze] Intel VA driver: {qsv_va_driver}\n"
        f"[ByteSqueeze] Selected QSV adapter: {qsv_adapter}\n"
        if qsv_job else ""
    )
    gpu_route_log = ""
    if gpu_assignment:
        route_note = " (compatible fallback)" if gpu_assignment.get("fallback_used") else ""
        gpu_route_log = (
            f"[ByteSqueeze] GPU route: {gpu_assignment.get('codec', '').upper()} -> "
            f"GPU {gpu_assignment.get('index')} {gpu_assignment.get('name')}"
            f" [{gpu_assignment.get('encoder_family')}; "
            f"NVENC adapter {gpu_assignment.get('vendor_index')}]"
            f"{route_note}\n"
            f"[ByteSqueeze] GPU identity: UUID {gpu_assignment.get('gpu_uuid') or 'unknown'}; "
            f"PCI {gpu_assignment.get('pci_bus_id') or 'unknown'}\n"
            f"[ByteSqueeze] HandBrake GPU mapping: physical NVENC adapter "
            f"{gpu_assignment.get('vendor_index')} -> logical gpu="
            f"{env.get('TSD_NVENC_LOGICAL_GPU_INDEX', gpu_assignment.get('vendor_index'))}\n"
            f"[ByteSqueeze] GPU capability check: "
            f"{gpu_assignment.get('capability_verification') or 'not required'}\n"
        )

    # Spawn the Docker shell runner or the native Windows runner. Both consume
    # the exact same snapshotted environment contract.
    launch_command, launch_label = _encoder_launch_command()
    job["phase"] = "encoding"
    _append_job_log(
        job_id,
        (
            f"[ByteSqueeze] Hardware decode: {hardware_decode['label']}\n"
            f"[ByteSqueeze] Video encoder: {selected_encoder or 'unknown'}\n"
            f"[ByteSqueeze] Source codec: {source_video.get('codec') or 'unknown'}\n"
            f"[ByteSqueeze] Source resolution: {source_resolution_label}\n"
            f"[ByteSqueeze] Target resolution: {target_resolution_label}\n"
            f"[ByteSqueeze] Selected preset: {preset_name}\n"
            f"[ByteSqueeze] Output container: {output_plan['container'].upper()}"
            f" (requested {output_plan['requested_container'].upper()})\n"
            f"[ByteSqueeze] Container decision: {output_plan['reason']}\n"
            f"[ByteSqueeze] Web optimized: {'on' if output_plan['web_optimized'] else 'off'}\n"
            f"{smart_episode_log}"
            f"{frame_rate_log}"
            f"{qsv_diagnostics_log}"
            f"{gpu_route_log}"
            f"{encoder_profile_log}"
            f"[ByteSqueeze] Preset decode policy: {preset_decode_policy}\n"
            f"[ByteSqueeze] Encoder launch: {launch_label}\n"
            f"[ByteSqueeze] Preset file: {preset_file}\n"
            f"[ByteSqueeze] Preset name: {preset_name}\n"
            f"[ByteSqueeze] HandBrakeCLI: {shutil.which('HandBrakeCLI') or 'NOT FOUND'}"
        ),
    )
    proc = subprocess.Popen(
        launch_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **_process_group_options(),
    )

    job["pid"] = proc.pid
    save_jobs()

    # ------------------------------------------------------------
    # STREAM OUTPUT:
    # - Write full log to file
    # - Keep in-memory tail for quick viewing in web UI
    # - Update progress and ETA based on HandBrake output
    # ------------------------------------------------------------
    decode_evidence = {"active": False, "fallback": False, "line": ""}
    with open(log_path, "a", encoding="utf-8", errors="replace") as lf:
        for line in proc.stdout:
            lf.write(line)
            lf.flush()

            # Keep a bounded in-memory tail for status APIs. The full output
            # remains in the UTF-8 log file for controller-side download.
            job["log"] = (str(job.get("log") or "") + line)[-JOB_LOG_TAIL_CHARS:]

            evidence = _qsv_decode_log_evidence(line) if hardware_decode["enabled"] else ""
            if evidence == "active":
                decode_evidence.update({"active": True, "fallback": False, "line": line.strip()[:300]})
            elif evidence == "fallback":
                # The shell wrapper can retry an otherwise valid QSV encode
                # with software decoding. The last observed path is the path
                # that produced the retained output.
                decode_evidence.update({"active": False, "fallback": True, "line": line.strip()[:300]})

            # Parse progress from this line, if present
            m = PROGRESS_RE.search(line)
            if m:
                try:
                    job["progress"] = float(m.group(1))
                except ValueError:
                    pass
                else:
                    if _maybe_update_output_estimate(job, out_path, job["progress"]):
                        stop_details = _estimated_output_stop_guard(job)
                        if stop_details:
                            job["auto_stop_triggered"] = True
                            job["auto_stop_details"] = stop_details
                            job["cancel_reason"] = "estimated_output_limit"
                            job["status"] = "canceled"
                            job["eta_seconds"] = None
                            log_event(
                                "job_auto_stopped",
                                (
                                    f"Stopped {os.path.basename(display_src_path)}: projected output "
                                    f"{stop_details['ratio_percent']}% of original reached the "
                                    f"{stop_details['threshold_percent']}% limit."
                                ),
                                level="warn",
                                job_id=job_id,
                                src=display_src_path,
                                extra=stop_details,
                            )
                            terminated, detail = terminate_process_tree(proc.pid, force=True)
                            job["cancel_process_tree_terminated"] = bool(terminated)
                            job["cancel_process_detail"] = str(detail or "")[:500]
                        save_jobs()

            # Parse ETA from this line, if present
            em = ETA_RE.search(line)
            if em:
                eta_str = em.group(1)
                eta_seconds = _parse_eta_to_seconds(eta_str)
                job["eta_seconds"] = eta_seconds

            # If the job was canceled externally, stop reading further
            if job.get("status") == "canceled":
                if proc.poll() is None:
                    terminated, detail = terminate_process_tree(proc.pid, force=True)
                    job["cancel_process_tree_terminated"] = bool(terminated)
                    job["cancel_process_detail"] = str(detail or "")[:500]
                break

    # Wait for process to exit
    ret = proc.wait()
    job["returncode"] = ret

    if hardware_decode["enabled"]:
        if decode_evidence["active"]:
            job["hardware_decode_active"] = True
            job["hardware_decode_reason"] = "verified in HandBrake job log"
            _append_job_log(
                job_id,
                f"[ByteSqueeze] Hardware decode verification: active QSV decode path ({decode_evidence['line']})",
            )
        elif decode_evidence["fallback"]:
            job["hardware_decode_active"] = False
            job["hardware_decode_reason"] = "HandBrake reported a QSV decoder failure or software retry"
            _append_job_log(
                job_id,
                "[ByteSqueeze] Hardware decode: software fallback "
                f"(HandBrake decoder failure: {decode_evidence['line']})",
            )
        else:
            job["hardware_decode_active"] = None
            job["hardware_decode_reason"] = "HandBrake emitted no decode-path verification marker"
            _append_job_log(
                job_id,
                "[ByteSqueeze] Hardware decode verification: QSV requested, "
                "but HandBrake emitted no recognized active/fallback marker.",
            )

    # If job was not canceled, finalize with done/error
    if job.get("status") != "canceled":
        job["status"] = "done" if ret == 0 else "error"
        if ret == 0:
            job["progress"] = 100.0
            job["phase"] = "validating"
        else:
            job["phase"] = "encode_error"
            job["error_message"] = (
                f"HandBrake exited with code {ret}: {_job_error_excerpt(job)}"
            )[:500]
            _append_job_log(job_id, f"[ByteSqueeze] ERROR: {job['error_message']}")
            print(f"[JOB {job_id}] {job['error_message']}", flush=True)

    output_validation_error = ""

    # Storage tracking + history (only when we have a real output file)
    if job.get("status") == "done" and ret == 0:
        try:
            output_ok, output_reason = _encoded_output_is_valid(out_path)
            if output_ok and out_path and os.path.isfile(out_path):
                out_bytes = int(os.path.getsize(out_path))
                job["out_bytes"] = out_bytes
                job["estimated_out_bytes"] = out_bytes
                job["estimated_out_source"] = "actual"

                src_bytes = job.get("src_bytes")
                try:
                    src_bytes_i = int(src_bytes) if src_bytes is not None else 0
                except Exception:
                    src_bytes_i = 0

                saved_bytes = max(0, src_bytes_i - out_bytes)
                job["saved_bytes"] = saved_bytes
                output_inventory = scan_media(out_path, exact=True, force=True)
                breakdown = storage_breakdown(
                    source_inventory,
                    output_inventory,
                    src_bytes_i,
                    out_bytes,
                )
                job["output_inventory"] = output_inventory
                job["storage_breakdown"] = breakdown
                duration_seconds_for_stats = None
                if job.get("started_at") is not None:
                    try:
                        duration_seconds_for_stats = max(0.0, _now_ts() - float(job["started_at"]))
                    except Exception:
                        duration_seconds_for_stats = None

                if remote_transfer:
                    transfer["local_out"] = out_path
                    transfer["work_dir"] = transfer_work_dir
                    job["local_out_path"] = out_path
                    job["duration_seconds"] = duration_seconds_for_stats
                    transfer["status"] = "uploading"
                    job["phase"] = "uploading"
                    job["transfer"] = transfer
                    save_jobs()
                    try:
                        upload_result = _upload_transfer_output(
                            transfer.get("upload_url") or "",
                            transfer.get("upload_token") or "",
                            transfer.get("worker_node_id") or "",
                            out_path,
                            job_id=job_id,
                            duration_seconds=duration_seconds_for_stats,
                            progress_callback=update_transfer_progress,
                        )
                        _apply_remote_upload_success(job_id, job, upload_result, out_path)
                    except Exception as upload_error:
                        _mark_transfer_waiting(job_id, job, upload_error)
                else:
                    try:
                        from .node_linking import local_node_info
                        local_node = local_node_info()
                    except Exception:
                        local_node = {}
                    # Persist to long-term history
                    record_encode(
                        job_id=job_id,
                        src=display_src_path,
                        out=out_path,
                        preset=preset_key,
                        src_bytes=src_bytes_i,
                        out_bytes=out_bytes,
                        duration_seconds=duration_seconds_for_stats,
                        is_hdr=bool(job.get("is_hdr", False)),
                        node_id=local_node.get("id"),
                        node_name=local_node.get("name"),
                        encode_method=job.get("encode_method"),
                        encoder=job.get("encoder"),
                        video_codec=job.get("video_codec"),
                        encoder_family=job.get("encoder_family"),
                        bit_depth=job.get("bit_depth"),
                        job_type=job.get("job_type"),
                        operations=operations,
                        parent_job_id=job.get("parent_job_id"),
                        input_inventory=source_inventory,
                        output_inventory=output_inventory,
                        storage_breakdown=breakdown,
                    )

                    source_deleted = False
                    try:
                        if os.path.isfile(encode_src_path):
                            os.remove(encode_src_path)
                            source_deleted = True
                    except Exception as e:
                        log_event(
                            "job_cleanup_error",
                            f"Finished but failed to delete original: {os.path.basename(display_src_path)} ({e})",
                            level="warn",
                            job_id=job_id,
                            src=display_src_path,
                            extra={
                                "out_path": out_path,
                            },
                        )

                    log_event(
                        "job_finished",
                        f"Finished: {os.path.basename(display_src_path)} - saved {round(saved_bytes/(1024**3), 3)} GB",
                        job_id=job_id,
                        src=display_src_path,
                        extra={
                            "saved_bytes": saved_bytes,
                            "out_path": out_path,
                            "source_deleted": source_deleted,
                        },
                    )
            else:
                output_validation_error = output_reason or "output validation failed"
                job["status"] = "error"
                job["phase"] = "validation_error"
                job["error_message"] = output_validation_error[:500]
                _append_job_log(
                    job_id,
                    f"[ByteSqueeze] ERROR: Output validation failed: {output_validation_error}",
                )
                log_event(
                    "job_error",
                    f"Output validation failed for {os.path.basename(display_src_path)}: {output_validation_error}",
                    level="error",
                    job_id=job_id,
                    src=display_src_path,
                    extra={
                        "out_path": out_path,
                    },
                )
        except Exception as e:
            job["status"] = "error"
            job["phase"] = "validation_error"
            output_validation_error = str(e)
            job["error_message"] = f"Output finalization failed: {e}"[:500]
            _append_job_log(job_id, f"[ByteSqueeze] ERROR: {job['error_message']}")
            log_event(
                "stats_error",
                f"Failed to record storage savings: {e}",
                level="warn",
                job_id=job_id,
                src=display_src_path,
            )

    if job.get("status") in ("error", "canceled"):
        deleted_failed_output = False

        if out_path and not out_path_existed_before:
            try:
                if os.path.isfile(out_path):
                    os.remove(out_path)
                    deleted_failed_output = True
            except Exception as e:
                log_event(
                    "job_cleanup_error",
                    f"Failed to delete failed output: {os.path.basename(out_path)} ({e})",
                    level="warn",
                    job_id=job_id,
                    src=display_src_path,
                    extra={
                        "out_path": out_path,
                    },
                )

        if job.get("status") == "error":
            msg = f"Error: {os.path.basename(display_src_path)} (exit {ret})"
            if output_validation_error:
                msg += f" - {output_validation_error}"
            if deleted_failed_output:
                msg += " - deleted failed output"
            log_event(
                "job_error",
                msg,
                level="error",
                job_id=job_id,
                src=display_src_path,
                extra={
                    "out_path": out_path,
                    "deleted_failed_output": deleted_failed_output,
                },
            )
        elif deleted_failed_output:
            log_event(
                "job_cleanup",
                f"Canceled: deleted partial output {os.path.basename(out_path)}",
                level="warn",
                job_id=job_id,
                src=display_src_path,
                extra={
                    "out_path": out_path,
                    "deleted_failed_output": True,
                },
            )

    if job.get("status") == "canceled":
        job["phase"] = "canceled"
        log_event(
            "job_canceled",
            f"Canceled: {os.path.basename(display_src_path)}",
            level="warn",
            job_id=job_id,
            src=display_src_path,
        )

    # Once finished (or canceled), ETA no longer makes sense
    job["eta_seconds"] = None
    job["finished_at"] = _now_ts()
    if job.get("started_at") is not None:
        try:
            job["duration_seconds"] = max(0.0, float(job["finished_at"]) - float(job["started_at"]))
        except Exception:
            job["duration_seconds"] = None

    job["pid"] = None
    if job.get("status") == "done":
        job["phase"] = "done"
        job["error_message"] = ""
        _append_job_log(job_id, "[ByteSqueeze] Job completed successfully.")
    if remote_transfer:
        transfer = job.get("transfer") if isinstance(job.get("transfer"), dict) else transfer
        if job.get("status") != "waiting_to_upload":
            transfer.pop("download_token", None)
            transfer.pop("upload_token", None)
            transfer.pop("local_src", None)
            transfer.pop("local_out", None)
            transfer.pop("work_dir", None)
            job["local_out_path"] = None
            job["transfer"] = transfer
            _cleanup_transfer_work_dir(transfer_work_dir, transfer)
        else:
            # Keep the validated output and transfer credentials durable until
            # the paired controller comes back and accepts the upload.
            job["transfer"] = transfer
    _cleanup_job_preset_dir(preset_work_dir)
    save_jobs()


def _run_dispatched_job(job_id: str) -> None:
    """Run one claimed job and always release its dispatcher slot."""
    job = jobs.get(job_id)
    if not job:
        with DISPATCH_LOCK:
            RUNNING_JOB_THREADS.pop(job_id, None)
        DISPATCH_WAKE_EVENT.set()
        return
    if job.get("status") == "canceled":
        with DISPATCH_LOCK:
            if job_id in job_queue:
                try:
                    job_queue.remove(job_id)
                except ValueError:
                    pass
            RUNNING_JOB_THREADS.pop(job_id, None)
        save_jobs()
        DISPATCH_WAKE_EVENT.set()
        return

    try:
        print(f"[DISPATCHER] starting job {job_id}", flush=True)
        run_encode(job_id, job["src"], job["preset"])
        print(f"[DISPATCHER] finished job {job_id}", flush=True)
    except Exception as exc:
        details = traceback.format_exc()
        if job.get("status") != "canceled":
            job["status"] = "error"
            job["phase"] = "dispatcher_error"
            job["returncode"] = -1
            job["pid"] = None
            job["eta_seconds"] = None
            job["finished_at"] = _now_ts()
            if job.get("started_at") is not None:
                try:
                    job["duration_seconds"] = max(0.0, float(job["finished_at"]) - float(job["started_at"]))
                except Exception:
                    job["duration_seconds"] = None
            job["error_message"] = f"Dispatcher error: {exc}"[:500]
            _append_job_log(
                job_id,
                f"[ByteSqueeze] ERROR: Dispatcher failed: {exc}\n{details}",
            )
            transfer = job.get("transfer") if isinstance(job.get("transfer"), dict) else {}
            failed_work_dir = str(transfer.get("work_dir") or "")
            if job.get("mode") == "remote_transfer" and failed_work_dir:
                _cleanup_transfer_work_dir(failed_work_dir, transfer)
                for key in ("download_token", "upload_token", "local_src", "local_out", "work_dir"):
                    transfer.pop(key, None)
                transfer["status"] = "error"
                transfer["error"] = str(exc)[:300]
                job["transfer"] = transfer
        print(
            f"[DISPATCHER] job {job_id} failed: {exc}\n{details}",
            flush=True,
        )
        log_event(
            "job_error",
            f"Dispatcher failed: {os.path.basename(job.get('src') or job_id)} ({exc})",
            level="error",
            job_id=job_id,
            src=job.get("src"),
        )
    finally:
        with DISPATCH_LOCK:
            if job_id in job_queue:
                try:
                    job_queue.remove(job_id)
                except ValueError:
                    pass
            RUNNING_JOB_THREADS.pop(job_id, None)
        save_jobs()
        DISPATCH_WAKE_EVENT.set()


def dispatcher_loop():
    """Dispatch FIFO jobs with CPU exclusivity and bounded GPU concurrency."""
    global job_queue, queue_paused
    print("[DISPATCHER] started", flush=True)

    while True:
        launched = False
        queue_changed = False

        if not queue_paused:
            with DISPATCH_LOCK:
                for job_id, thread in list(RUNNING_JOB_THREADS.items()):
                    if not thread.is_alive():
                        RUNNING_JOB_THREADS.pop(job_id, None)

                next_id = None
                for job_id in list(job_queue):
                    job = jobs.get(job_id)
                    if not job:
                        try:
                            job_queue.remove(job_id)
                        except ValueError:
                            pass
                        queue_changed = True
                        continue
                    if job.get("status") == "queued":
                        # Automatic jobs must remain at the head of the queue
                        # until the node coordinator assigns real capacity.
                        if job.get("mode") == "auto_node":
                            break
                        next_id = job_id
                        break

                if next_id:
                    next_job = jobs.get(next_id)
                    running_jobs = [
                        jobs[job_id]
                        for job_id in RUNNING_JOB_THREADS
                        if job_id in jobs and jobs[job_id].get("status") == "running"
                    ]
                    route_available = True
                    if next_job:
                        previous_route_error = str(next_job.get("gpu_route_error") or "")
                        route_available, _assignment = _assign_windows_gpu_route(next_job, running_jobs)
                        if str(next_job.get("gpu_route_error") or "") != previous_route_error:
                            queue_changed = True
                    if next_job and route_available and _can_dispatch_job(
                        next_job,
                        running_jobs,
                        _hardware_transcode_limit(job=next_job),
                    ):
                        # Claim before starting the thread so this loop cannot
                        # overfill the GPU while the thread is still booting.
                        next_job["status"] = "running"
                        thread = threading.Thread(
                            target=_run_dispatched_job,
                            args=(next_id,),
                            daemon=True,
                            name=f"encode-{next_id[:8]}",
                        )
                        RUNNING_JOB_THREADS[next_id] = thread
                        thread.start()
                        launched = True

        if launched or queue_changed:
            save_jobs()
        if launched:
            # Re-evaluate immediately so another hardware job can fill the
            # next slot without waiting for the polling interval.
            continue

        DISPATCH_WAKE_EVENT.wait(1.0)
        DISPATCH_WAKE_EVENT.clear()


def transfer_retry_loop():
    """Retry completed remote outputs without re-running the encode."""
    print("[TRANSFER-RETRY] started", flush=True)
    while True:
        now = _now_ts()
        for job_id, job in list(jobs.items()):
            if job.get("mode") != "remote_transfer" or job.get("status") != "waiting_to_upload":
                continue
            transfer = job.get("transfer") if isinstance(job.get("transfer"), dict) else {}
            if float(transfer.get("next_retry_at") or 0) > now:
                continue
            out_path = str(transfer.get("local_out") or job.get("local_out_path") or "").strip()
            if not out_path or not os.path.isfile(out_path):
                job["status"] = "error"
                transfer["status"] = "error"
                transfer["last_error"] = "completed worker output is missing"
                job["transfer"] = transfer
                save_jobs()
                continue

            transfer["status"] = "retrying_upload"
            transfer["last_attempt_at"] = now
            job["transfer"] = transfer
            save_jobs()
            try:
                # Renewing is cheap and also proves the paired controller is back.
                renewal = _renew_transfer_upload_grant(job_id, transfer)
                duration_seconds = job.get("duration_seconds")
                result = renewal if renewal.get("complete") else _upload_transfer_output(
                    transfer.get("upload_url") or "",
                    transfer.get("upload_token") or "",
                    transfer.get("worker_node_id") or "",
                    out_path,
                    job_id=job_id,
                    duration_seconds=duration_seconds,
                )
                _apply_remote_upload_success(job_id, job, result, out_path)
                work_dir = str(transfer.get("work_dir") or "").strip()
                transfer.pop("download_token", None)
                transfer.pop("upload_token", None)
                transfer.pop("local_src", None)
                transfer.pop("local_out", None)
                transfer.pop("work_dir", None)
                job["local_out_path"] = None
                job["transfer"] = transfer
                _cleanup_transfer_work_dir(work_dir, transfer)
                save_jobs()
            except Exception as e:
                _mark_transfer_waiting(job_id, job, e)
        time.sleep(10.0)


def ensure_dispatcher():
    """
    Ensure that the dispatcher thread is running (start it once).

    You can safely call this multiple times; only the first call starts
    the background thread.
    """
    global dispatcher_started, transfer_retry_started
    with DISPATCH_LOCK:
        if not dispatcher_started:
            dispatcher_started = True
            threading.Thread(target=dispatcher_loop, daemon=True, name="dispatcher").start()
        if not transfer_retry_started:
            transfer_retry_started = True
            threading.Thread(target=transfer_retry_loop, daemon=True, name="transfer-retry").start()
    DISPATCH_WAKE_EVENT.set()


# -------------------------------------------------------------------
# Queue control: pause / resume, remove, cancel, clear finished
# -------------------------------------------------------------------

def get_queue_state() -> bool:
    """
    Returns:
        bool: True if queue is paused, False if running.
    """
    return queue_paused


def set_queue_paused(paused: bool | None = None) -> bool:
    """
    Set or toggle the queue paused state.

    Args:
        paused (bool | None):
            - True/False to explicitly set state
            - None to toggle current state

    Returns:
        bool: new queue_paused value
    """
    global queue_paused

    if isinstance(paused, bool):
        queue_paused = paused
    else:
        queue_paused = not queue_paused

    save_jobs()
    DISPATCH_WAKE_EVENT.set()
    log_event(
        "queue_state",
        "Queue paused" if queue_paused else "Queue resumed",
        level="info",
    )
    return queue_paused


def cancel_job(job_id: str) -> tuple[bool, str | None]:
    """
    Cancel a job.

    Behavior:
      - If job is "queued": it is removed from job_queue and marked "canceled"
      - If job is "running": SIGTERM is sent to its pid (if any), status set to "canceled"
      - Other states: still mark as canceled, but nothing else to do

    Returns:
        (ok, error_message):
            - ok = True if cancel worked
            - ok = False with error_message if job not found
    """
    global job_queue

    job = jobs.get(job_id)
    if not job:
        return False, "job not found"

    src = job.get("src")

    # Queued but not started -> remove from queue
    if job["status"] == "queued":
        if job_id in job_queue:
            try:
                job_queue.remove(job_id)
            except ValueError:
                pass
        job["status"] = "canceled"
        job["returncode"] = None
        job["progress"] = 0.0
        job["eta_seconds"] = None
        job["finished_at"] = _now_ts()
        if job.get("started_at") is not None:
            try:
                job["duration_seconds"] = max(0.0, float(job["finished_at"]) - float(job["started_at"]))
            except Exception:
                job["duration_seconds"] = None
        save_jobs()
        _src = job.get("src")
        log_event(
            "job_canceled",
            f"Canceled (queued): {os.path.basename(_src or '')}",
            level="warn",
            job_id=job_id,
            src=_src,
        )
        return True, None

    # Mark canceled before terminating so the runner cannot race the kill and
    # convert an intentional cancellation into an encode error.
    pid = job.get("pid")
    job["status"] = "canceled"
    job["phase"] = "canceling_process_tree" if pid else "canceled"
    job["returncode"] = None
    job["progress"] = 0.0
    job["eta_seconds"] = None
    job["finished_at"] = _now_ts()
    if job.get("started_at") is not None:
        try:
            job["duration_seconds"] = max(0.0, float(job["finished_at"]) - float(job["started_at"]))
        except Exception:
            job["duration_seconds"] = None
    save_jobs()

    terminated = True
    termination_detail = "no active encoder pid"
    if pid:
        terminated, termination_detail = terminate_process_tree(pid, force=True)
    job["phase"] = "canceled" if terminated else "cancel_cleanup_failed"
    job["cancel_process_tree_terminated"] = bool(terminated)
    job["cancel_process_detail"] = str(termination_detail or "")[:500]
    _append_job_log(
        job_id,
        (
            f"[ByteSqueeze] Cancellation process-tree cleanup: "
            f"{'complete' if terminated else 'FAILED'}"
            f"{f' ({termination_detail})' if termination_detail else ''}"
        ),
    )
    save_jobs()
    DISPATCH_WAKE_EVENT.set()

    _src = job.get("src")
    log_event(
        "job_canceled",
        f"Canceled (running): {os.path.basename(_src or '')}",
        level="warn",
        job_id=job_id,
        src=_src,
    )

    return True, None




def move_queued_job(job_id: str, direction: str) -> tuple[bool, str | None]:
    """Reorder a queued job inside the queue.

    direction: up | down | top | bottom
    """
    global job_queue

    if direction not in {"up", "down", "top", "bottom"}:
        return False, "invalid direction"

    job = jobs.get(job_id)
    if not job:
        return False, "job not found"
    if job.get("status") != "queued":
        return False, "only queued jobs can be reordered"
    if job_id not in job_queue:
        return False, "job is not currently in queue"

    idx = job_queue.index(job_id)
    new_idx = idx
    if direction == "up":
        new_idx = max(0, idx - 1)
    elif direction == "down":
        new_idx = min(len(job_queue) - 1, idx + 1)
    elif direction == "top":
        new_idx = 0
    elif direction == "bottom":
        new_idx = len(job_queue) - 1

    if new_idx == idx:
        return True, None

    job_queue.pop(idx)
    job_queue.insert(new_idx, job_id)
    save_jobs()
    log_event(
        "job_reordered",
        f"Reordered queued job: {os.path.basename(job.get('src') or job_id)} → position {new_idx + 1}",
        job_id=job_id,
        src=job.get("src"),
    )
    return True, None



def move_queued_job_to_position(job_id: str, position: int) -> tuple[bool, str | None]:
    """Move a queued job to an explicit 1-based position in the queue."""
    global job_queue

    job = jobs.get(job_id)
    if not job:
        return False, "job not found"
    if job.get("status") != "queued":
        return False, "only queued jobs can be reordered"
    if job_id not in job_queue:
        return False, "job is not currently in queue"

    try:
        target_pos = int(position)
    except (TypeError, ValueError):
        return False, "invalid position"

    if target_pos < 1:
        target_pos = 1
    if target_pos > len(job_queue):
        target_pos = len(job_queue)

    idx = job_queue.index(job_id)
    new_idx = target_pos - 1
    if new_idx == idx:
        return True, None

    job_queue.pop(idx)
    job_queue.insert(new_idx, job_id)
    save_jobs()
    log_event(
        "job_reordered",
        f"Moved queued job: {os.path.basename(job.get('src') or job_id)} → position {new_idx + 1}",
        job_id=job_id,
        src=job.get("src"),
    )
    return True, None


def get_job_summary() -> dict:
    """Return lightweight dashboard metrics for the jobs page."""
    archived = _normalize_dashboard_totals(dashboard_totals)
    status_counts = {
        "queued": 0,
        "running": 0,
        "waiting_to_upload": 0,
        "done": int(archived.get("done") or 0),
        "error": int(archived.get("error") or 0),
        "canceled": int(archived.get("canceled") or 0),
    }
    active_error_count = 0
    total_saved_bytes = int(archived.get("saved_bytes") or 0)
    live_done_runtime_seconds = 0.0
    live_error_runtime_seconds = 0.0
    live_canceled_runtime_seconds = 0.0

    for job in jobs.values():
        status = str(job.get("status") or "").lower()
        if status in status_counts:
            status_counts[status] += 1
        if status == "error":
            active_error_count += 1

        try:
            total_saved_bytes += int(job.get("saved_bytes") or 0)
        except Exception:
            pass

        try:
            duration_seconds = float(job.get("duration_seconds") or 0.0)
        except Exception:
            duration_seconds = 0.0
        if status == "done":
            live_done_runtime_seconds += duration_seconds
        elif status == "error":
            live_error_runtime_seconds += duration_seconds
        elif status == "canceled":
            live_canceled_runtime_seconds += duration_seconds

    try:
        storage_summary = get_storage_summary()
    except Exception:
        storage_summary = {}

    try:
        storage_saved_bytes = int(storage_summary.get("saved_bytes") or 0)
        total_saved_bytes = max(total_saved_bytes, storage_saved_bytes)
    except Exception:
        pass

    try:
        storage_runtime_seconds = float(storage_summary.get("total_runtime_seconds") or 0.0)
    except Exception:
        storage_runtime_seconds = 0.0

    done_runtime_seconds = max(
        storage_runtime_seconds,
        float(archived.get("done_runtime_seconds") or 0.0) + live_done_runtime_seconds,
    )
    total_runtime_seconds = (
        done_runtime_seconds
        + float(archived.get("error_runtime_seconds") or 0.0)
        + float(archived.get("canceled_runtime_seconds") or 0.0)
        + live_error_runtime_seconds
        + live_canceled_runtime_seconds
    )

    try:
        status_counts["done"] = max(status_counts["done"], int(storage_summary.get("count") or 0))
    except Exception:
        pass

    queued_items = [jid for jid in job_queue if jid in jobs and jobs[jid].get("status") == "queued"]
    running_job_ids = [jid for jid, j in jobs.items() if j.get("status") == "running"]
    running_job_id = running_job_ids[0] if running_job_ids else None
    policy_job = next(
        (
            jobs[jid]
            for jid in running_job_ids + queued_items
            if jid in jobs
        ),
        None,
    )

    return {
        "counts": status_counts,
        "queue_paused": bool(queue_paused),
        "queued_count": len(queued_items),
        "running_job_id": running_job_id,
        "running_job_ids": running_job_ids,
        "hardware_transcode_concurrency": _hardware_transcode_limit(job=policy_job),
        "active_error_count": active_error_count,
        "saved_bytes": total_saved_bytes,
        "saved_gb": round(total_saved_bytes / (1024**3), 3) if total_saved_bytes else 0.0,
        "total_runtime_seconds": round(total_runtime_seconds, 1),
    }

def remove_queued_job(job_id: str) -> tuple[bool, str | None]:
    """
    Remove a job entirely from the queue *only if* it is still "queued".

    Used by the "Remove from queue" button in the UI.

    Returns:
        (ok, error_message):
            - ok = True if job removed
            - ok = False with error_message if job not found / not queued
    """
    global job_queue

    job = jobs.get(job_id)
    if not job:
        return False, "job not found"

    src = job.get("src")

    if job.get("status") != "queued":
        return False, "can only remove jobs in 'queued' status"

    if job_id in job_queue:
        try:
            job_queue.remove(job_id)
        except ValueError:
            pass

    jobs.pop(job_id, None)
    save_jobs()
    _src = job.get("src")
    log_event(
        "job_removed",
        f"Removed from queue: {os.path.basename(_src or '')}",
        level="info",
        job_id=job_id,
        src=_src,
    )
    return True, None


def clear_finished_jobs() -> int:
    """
    Remove all terminal jobs: status in {"done", "error", "canceled"}.

    - Does NOT touch queued or running jobs
    - Deletes the log files corresponding to removed jobs

    Returns:
        int: number of jobs removed
    """
    global jobs, job_queue, dashboard_totals, history_cleared_before

    # Capture the boundary before scanning the live queue. An encode that
    # finishes after the user presses Clear must remain visible as new work.
    clear_boundary = _now_ts()

    to_remove = []
    for jid, j in list(jobs.items()):
        if str(j.get("status") or "").lower() in {"done", "error", "canceled"}:
            to_remove.append(jid)

    archived = _normalize_dashboard_totals(dashboard_totals)
    removed = 0
    for jid in to_remove:
        job = jobs.get(jid) or {}
        status = str(job.get("status") or "").lower()
        if status in ("done", "error", "canceled"):
            archived[status] = int(archived.get(status) or 0) + 1
        try:
            duration_seconds = float(job.get("duration_seconds") or 0.0)
        except Exception:
            duration_seconds = 0.0
        try:
            archived["saved_bytes"] = int(archived.get("saved_bytes") or 0) + int(job.get("saved_bytes") or 0)
        except Exception:
            pass
        try:
            archived["runtime_seconds"] = float(archived.get("runtime_seconds") or 0.0) + duration_seconds
        except Exception:
            pass
        if status == "done":
            archived["done_runtime_seconds"] = float(archived.get("done_runtime_seconds") or 0.0) + duration_seconds
        elif status == "error":
            archived["error_runtime_seconds"] = float(archived.get("error_runtime_seconds") or 0.0) + duration_seconds
        elif status == "canceled":
            archived["canceled_runtime_seconds"] = float(
                archived.get("canceled_runtime_seconds") or 0.0
            ) + duration_seconds

        # Remove from jobs dict
        jobs.pop(jid, None)
        removed += 1

        # Make sure it's not in the queue
        if jid in job_queue:
            try:
                job_queue.remove(jid)
            except ValueError:
                pass

        # Remove log file if present
        log_path = os.path.join(LOG_DIR, f"{jid}.log")
        try:
            os.remove(log_path)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[WARN] Failed to remove log for {jid}: {e}", flush=True)

    dashboard_totals = archived
    history_cleared_before = max(float(history_cleared_before or 0.0), clear_boundary)
    save_jobs()
    return removed


def clear_error_status() -> int:
    """Remove error jobs and reset archived error counts so the app status can return to idle."""
    global jobs, job_queue, dashboard_totals

    archived = _normalize_dashboard_totals(dashboard_totals)
    cleared = int(archived.get("error") or 0)
    archived["error"] = 0
    archived["error_runtime_seconds"] = 0.0

    for jid, job in list(jobs.items()):
        if str(job.get("status") or "").lower() != "error":
            continue
        jobs.pop(jid, None)
        cleared += 1
        if jid in job_queue:
            try:
                job_queue.remove(jid)
            except ValueError:
                pass
        log_path = os.path.join(LOG_DIR, f"{jid}.log")
        try:
            os.remove(log_path)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[WARN] Failed to remove error log for {jid}: {e}", flush=True)

    dashboard_totals = archived
    save_jobs()
    return cleared

def clear_queued_jobs() -> int:
    """
    Remove all jobs that are currently queued or canceled.

    - Does NOT touch running jobs
    - Does NOT touch done / error jobs (those are history)
    - Also removes them from job_queue
    - Logs are removed if they exist (usually they won't for queued jobs)

    Returns:
        int: number of jobs removed
    """
    global jobs, job_queue, dashboard_totals

    to_remove: list[str] = []
    for jid, j in list(jobs.items()):
        if str(j.get("status") or "").lower() in {"queued", "canceled"}:
            to_remove.append(jid)

    archived = _normalize_dashboard_totals(dashboard_totals)
    removed = 0
    for jid in to_remove:
        job = jobs.get(jid) or {}
        status = str(job.get("status") or "").lower()
        if status == "canceled":
            archived["canceled"] = int(archived.get("canceled") or 0) + 1
            try:
                archived["canceled_runtime_seconds"] = (
                    float(archived.get("canceled_runtime_seconds") or 0.0)
                    + float(job.get("duration_seconds") or 0.0)
                )
            except Exception:
                pass
            try:
                archived["runtime_seconds"] = (
                    float(archived.get("runtime_seconds") or 0.0)
                    + float(job.get("duration_seconds") or 0.0)
                )
            except Exception:
                pass

        # Remove from jobs dict
        jobs.pop(jid, None)
        removed += 1

        # Make sure it's not in the queue
        if jid in job_queue:
            try:
                job_queue.remove(jid)
            except ValueError:
                pass

        # Remove log file if present (probably rare for queued jobs)
        log_path = os.path.join(LOG_DIR, f"{jid}.log")
        try:
            os.remove(log_path)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[WARN] Failed to remove log for queued job {jid}: {e}", flush=True)

    dashboard_totals = archived
    save_jobs()
    return removed
