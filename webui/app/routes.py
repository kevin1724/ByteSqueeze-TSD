"""
Flask route definitions for HandBrake TSD Helper.

This module:
- Defines all HTTP endpoints (API + web UI)
- Uses helpers from jobs.py and presets.py
- Renders the single-page Web UI (index.html template)

NOTES FOR FUTURE MAINTAINERS
---------------------------
This file historically accumulated multiple partial implementations of the same
"HandBrake scan JSON" parsing logic. The Size Wizard depends on a reliable probe
(duration / width / height / fps). HandBrakeCLI is a bit quirky:
- It may print JSON to *stderr* (not stdout)
- It may print other log lines before/after the JSON
- It may print multiple JSON values in the same run (objects or arrays)
So we:
1) capture stdout+stderr
2) extract *all* JSON values safely (brace/bracket matching)
3) find the value that contains TitleList/Titles (sometimes nested)
4) choose the best title (valid geometry + longest duration)
5) if that shape changes or is incomplete, fall back to ffprobe and then a text scan


Implementation notes:
- We run a short HandBrakeCLI encode (e.g., 8–12 seconds) into a temp file,
  using the same preset base (1080/4k/auto -> preset_config mapping) plus
  wizard args (--target-size, --encoder-preset, optional downscale).
- We return base64 JPEGs so the web UI can render them directly.
"""

import os
import json
import math
import re
import difflib
import subprocess
import base64
import tempfile
import uuid
import signal
import time
import hashlib
import threading
import secrets
import shutil
import socket
import shlex
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlencode
from urllib.request import Request, urlopen


from flask import (
    request,
    jsonify,
    render_template,
    send_file,
    abort,
    redirect,
)
from werkzeug.utils import secure_filename

from .config import (
    ROOTS,
    VIDEO_EXTS,
    PRESET_DIR,
    LOG_DIR,
    DATA_DIR,
)
from .jobs import (
    is_allowed_path,
    create_job,
    create_jobs_batch,
    create_remote_transfer_job,
    get_job,
    list_jobs_for_api,
    list_job_history_for_api,
    read_job_log,
    cancel_job,
    clear_error_status,
    remove_queued_job,
    clear_finished_jobs as clear_finished_jobs_core,
    clear_queued_jobs,
    get_queue_state,
    set_queue_paused,
    move_queued_job_to_position,
    move_queued_job,
    get_job_summary,
    save_jobs,
    ensure_dispatcher,
    get_next_auto_dispatch_job,
    auto_dispatch_local_available,
    claim_auto_dispatch_job,
    release_auto_dispatch_job,
    complete_auto_dispatch_job,
    activate_auto_dispatch_locally,
    replace_queued_job_preset,
    _queued_preset_display_name,
    _encoded_output_is_valid,
    _format_handbrake_fps,
)

from .presets import (
    list_preset_files,
    preset_config,
    save_preset_config,
    guess_preset_from_filename,
    resolve_preset_file_and_name,
)
from .settings import (
    load_settings,
    normalize_output_container,
    save_settings,
)
from .mobile_linking import (
    accept_mobile_pairing,
    authenticate_mobile_token,
    create_mobile_pairing,
    list_mobile_devices,
    mobile_discovery,
    refresh_mobile_token,
    revoke_mobile_device,
)

from .cpu_profiles import (
    list_cpu_profiles,
    get_cpu_profile,
)
from .wizard_llm import (
    analyze_episode_scenes,
    episode_scene_fingerprint,
    run_wizard_llm,
    wizard_llm_status,
)
from .smart_presets import (
    candidate_learning as smart_candidate_learning,
    feedback_context as smart_feedback_context,
    learning_status as smart_learning_status,
    learned_plan_defaults as smart_learned_plan_defaults,
    load_state as load_smart_preset_state,
    normalize_profile as normalize_smart_profile,
    public_state as public_smart_preset_state,
    record_feedback as record_smart_preset_feedback,
    save_profile as save_smart_preset_profile,
)

from .events import load_event_summaries, load_events, clear_events, log_event
from .storage_stats import get_summary as get_storage_summary, get_dashboard_analytics as get_storage_dashboard_analytics, list_encodes as list_storage_encodes, clear_stats as clear_storage_stats, record_encode, update_encode_output_inventory
from .audio_optimization import normalize_operations, scan_media, storage_breakdown, validate_audio_only
from .media_metadata import artwork_path as media_artwork_path, enrich_library as enrich_media_library
from .node_linking import (
    accept_pairing,
    create_transfer_grant,
    create_pairing_code,
    delete_node,
    delete_trusted_controller,
    enable_pair_recovery,
    encoder_hardware_profile,
    get_node_private,
    heartbeat_allowed_age,
    hmac_headers,
    list_nodes_private,
    list_nodes_public,
    local_node_overview,
    node_discovery,
    normalize_hardware_transcode_concurrency,
    normalize_path_mappings,
    normalize_transfer_mode,
    node_has_running_work,
    pair_worker,
    public_node,
    recover_pairing,
    recover_worker_session,
    rename_node,
    renew_transfer_upload_grant,
    save_node,
    get_transfer,
    save_transfer,
    set_local_node_name,
    signed_json_request,
    translate_path,
    trusted_controller,
    transfer_token_matches,
    update_trusted_controller,
    verify_hmac,
)

# -------------------------------------------------------------------
# Media probing + preview estimation helpers
# -------------------------------------------------------------------

def _run_cmd(cmd):
    """Run a command and return (ok, stdout, stderr)."""
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return (p.returncode == 0, p.stdout, p.stderr)
    except FileNotFoundError:
        return (False, "", f"not found: {cmd[0]}")
    except Exception as e:
        return (False, "", str(e))

PREVIEW_PID_DIR = "/tmp"
PREVIEW_CLIPS: dict[str, tuple[str, float]] = {}
PREVIEW_CLIPS_LOCK = threading.Lock()
PREVIEW_TASKS: dict[str, dict] = {}
PREVIEW_TASK_LOCK = threading.Lock()
SMART_FEEDBACK_TOKENS: dict[str, tuple[dict, float]] = {}
SMART_FEEDBACK_TOKEN_LOCK = threading.Lock()
AUTOPILOT_REVIEW_STATE: dict = {"cursor": 0}
AUTOPILOT_REVIEW_LOCK = threading.RLock()
PREVIEW_PROGRESS_RE = re.compile(r"Encoding:\s+task\s+\d+\s+of\s+\d+,\s*([\d.]+)\s*%", re.IGNORECASE)
PREVIEW_QSV_CHECK = {"checked_at": 0.0, "available": False, "reason": "not checked"}

def _preview_pidfile(preview_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", (preview_id or ""))
    return os.path.join(PREVIEW_PID_DIR, f"hbwiz_{safe}.pgid")

def _kill_preview_by_id(preview_id: str) -> bool:
    """Kill a running HandBrake preview process group by preview_id."""
    path = _preview_pidfile(preview_id)
    if not preview_id or not os.path.isfile(path):
        return False

    try:
        with open(path, "r", encoding="utf-8") as f:
            pgid = int((f.read() or "").strip() or "0")
        if pgid <= 0:
            return False

        # Try graceful, then hard kill
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass

        time.sleep(0.25)

        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass

        return True
    except Exception:
        return False
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


def _preview_set_task(preview_id: str, **updates) -> dict:
    preview_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(preview_id or "")) or uuid.uuid4().hex
    with PREVIEW_TASK_LOCK:
        row = PREVIEW_TASKS.get(preview_id)
        if not isinstance(row, dict):
            row = {
                "preview_id": preview_id,
                "state": "queued",
                "progress": 0.0,
                "message": "Queued accurate preview.",
                "created_at": time.time(),
                "updated_at": time.time(),
            }
        row.update(updates)
        row["updated_at"] = time.time()
        PREVIEW_TASKS[preview_id] = row
        return row.copy()


def _preview_get_task(preview_id: str) -> dict | None:
    with PREVIEW_TASK_LOCK:
        row = PREVIEW_TASKS.get(str(preview_id or ""))
        return row.copy() if isinstance(row, dict) else None


def _preview_cleanup_tasks() -> None:
    cutoff = time.time() - 60 * 30
    with PREVIEW_TASK_LOCK:
        for key, row in list(PREVIEW_TASKS.items()):
            try:
                ts = float(row.get("updated_at") or row.get("created_at") or 0)
            except Exception:
                ts = 0
            if ts < cutoff:
                PREVIEW_TASKS.pop(key, None)
    with SMART_FEEDBACK_TOKEN_LOCK:
        for token, (_context, created_at) in list(SMART_FEEDBACK_TOKENS.items()):
            if float(created_at or 0) < cutoff:
                SMART_FEEDBACK_TOKENS.pop(token, None)


def _register_smart_feedback_context(context: dict) -> str:
    token = secrets.token_urlsafe(24)
    with SMART_FEEDBACK_TOKEN_LOCK:
        SMART_FEEDBACK_TOKENS[token] = (context, time.time())
    return token


def _consume_smart_feedback_context(token: str) -> dict | None:
    with SMART_FEEDBACK_TOKEN_LOCK:
        row = SMART_FEEDBACK_TOKENS.pop(str(token or ""), None)
    return row[0] if row and isinstance(row[0], dict) else None


def _qsv_preview_available(force: bool = False) -> tuple[bool, str]:
    now = time.time()
    if not force and now - float(PREVIEW_QSV_CHECK.get("checked_at") or 0) < 60:
        return bool(PREVIEW_QSV_CHECK.get("available")), str(PREVIEW_QSV_CHECK.get("reason") or "")

    reason = ""
    available = False
    render = "/dev/dri/renderD128"
    if not os.path.exists(render):
        reason = "/dev/dri/renderD128 is not mounted"
    else:
        ok, out, err = _run_cmd(["vainfo", "--display", "drm", "--device", render])
        text = f"{out}\n{err}".lower()
        available = bool(ok and ("vainfo:" in text or "driver version" in text or "vainfo" in text))
        reason = "VAAPI render device is available" if available else (err or out or "vainfo failed").strip()[:180]

    PREVIEW_QSV_CHECK.update({"checked_at": now, "available": available, "reason": reason})
    return available, reason


def _software_preview_payload(data: dict) -> dict:
    out = dict(data or {})
    out["encoder_family"] = "software"
    out["ai_hardware"] = "software"
    codec = str(out.get("video_codec") or "").lower()
    if codec not in {"h264", "h265", "av1"} or codec == "av1":
        out["video_codec"] = "h265"
    if not out.get("bit_depth"):
        out["bit_depth"] = "10" if out.get("video_codec") == "h265" else "8"
    if str(out.get("encoder_speed") or "").lower() in {"", "auto", "slower", "veryslow"}:
        out["encoder_speed"] = "fast"
    return out


def _extract_all_json_values(text: str):
    """
    Extract ALL JSON values from an arbitrary string using bracket/brace matching.

    Why:
    - HandBrakeCLI may output multiple JSON values
    - JSON can be an object {...} OR an array [...]
    - It may include non-JSON logs before/after
    - Naively slicing from first '{' to last '}' often includes extra text and breaks json.loads

    Returns: list[str] of JSON blobs (each blob is a standalone JSON value)
    """
    results = []
    i = 0
    n = len(text)

    while i < n:
        # Find next potential JSON start
        next_obj = text.find("{", i)
        next_arr = text.find("[", i)

        if next_obj == -1 and next_arr == -1:
            break

        if next_obj == -1:
            start = next_arr
            open_ch, close_ch = "[", "]"
        elif next_arr == -1:
            start = next_obj
            open_ch, close_ch = "{", "}"
        else:
            if next_obj < next_arr:
                start = next_obj
                open_ch, close_ch = "{", "}"
            else:
                start = next_arr
                open_ch, close_ch = "[", "]"

        depth = 0
        in_str = False
        escape = False

        for j in range(start, n):
            ch = text[j]

            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            else:
                if ch == '"':
                    in_str = True
                    continue

            if ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    results.append(text[start:j + 1])
                    i = j + 1
                    break
        else:
            # ran out of text without closing
            break

    return results


def _find_title_list(obj):
    """Find a title list across HandBrake versions and wrapper shapes."""
    if isinstance(obj, list):
        # Some builds emit the title array as the top-level JSON value rather
        # than wrapping it in TitleList. Avoid treating unrelated arrays as
        # titles by requiring at least one media-title field.
        if any(
            isinstance(item, dict)
            and any(key in item for key in ("Duration", "Geometry", "Video", "Streams", "StreamList"))
            for item in obj
        ):
            return obj
        for item in obj:
            found = _find_title_list(item)
            if found is not None:
                return found
        return None

    if not isinstance(obj, dict):
        return None

    # Match key casing defensively; HandBrake-compatible builds do not all use
    # the same capitalization.
    for key, value in obj.items():
        if str(key).replace("_", "").lower() in {"titlelist", "titles"} and isinstance(value, list):
            return value

    # TitleSet, Scan, Result, and other wrappers have all appeared in scan
    # output. Recursing through JSON containers is safer than maintaining a
    # version-specific wrapper allow-list.
    for value in obj.values():
        if isinstance(value, (dict, list)):
            found = _find_title_list(value)
            if found is not None:
                return found

    return None


def _parse_duration_seconds(t: dict) -> float:
    """
    Parse duration in seconds from a HandBrake title dict.

    HandBrake JSON is not consistent across versions. Duration can be:
    - dict with Hours/Minutes/Seconds/Ticks
    - dict with Seconds or seconds
    - number
    - string timecode "HH:MM:SS(.sss)" or "MM:SS(.sss)"
    - sometimes top-level keys like DurationSeconds
    """
    d = t.get("Duration")

    # Dict form
    if isinstance(d, dict):
        try:
            hours = float(d.get("Hours", d.get("hours", 0)) or 0)
            minutes = float(d.get("Minutes", d.get("minutes", 0)) or 0)
            seconds = float(d.get("Seconds", d.get("seconds", 0)) or 0)
            ticks = float(d.get("Ticks", d.get("ticks", 0)) or 0)

            dur = hours * 3600 + minutes * 60 + seconds
            if ticks and ticks > 0:
                dur += (ticks / 10000000.0)  # typical HB tick scale

            if dur > 0:
                return dur
        except Exception:
            pass

    # Numeric seconds
    if isinstance(d, (int, float)):
        try:
            dur = float(d)
            if dur > 0:
                return dur
        except Exception:
            pass

    # String timecode
    if isinstance(d, str) and d.strip():
        s = d.strip()
        parts = s.split(":")
        try:
            if len(parts) == 3:
                hh = float(parts[0])
                mm = float(parts[1])
                ss = float(parts[2])
                dur = hh * 3600 + mm * 60 + ss
                if dur > 0:
                    return dur
            elif len(parts) == 2:
                mm = float(parts[0])
                ss = float(parts[1])
                dur = mm * 60 + ss
                if dur > 0:
                    return dur
        except Exception:
            pass

    # Fallback keys sometimes present
    for k in ("Seconds", "seconds", "duration", "DurationSeconds", "duration_sec"):
        if k in t:
            try:
                dur = float(t.get(k))
                if dur > 0:
                    return dur
            except Exception:
                pass

    return 0.0


def _extract_whfps(vdict: dict):
    """
    Extract width/height/fps/codec from a dict that *might* represent video fields.
    Supports Geometry/Video/Stream variations.
    """
    w = (
        vdict.get("Width") or vdict.get("width") or
        vdict.get("PixelWidth") or vdict.get("pixel_width")
    )
    h = (
        vdict.get("Height") or vdict.get("height") or
        vdict.get("PixelHeight") or vdict.get("pixel_height")
    )

    try:
        w = int(w or 0)
        h = int(h or 0)
    except Exception:
        w, h = 0, 0

    fps = vdict.get("FrameRate") or vdict.get("fps") or vdict.get("FrameRateNum")
    if isinstance(fps, dict):
        fps = fps.get("Rate") or fps.get("rate")

    try:
        fps_val = float(fps or 0.0)
    except Exception:
        fps_val = 0.0

    codec = (
        vdict.get("CodecName") or vdict.get("Codec") or
        vdict.get("codec") or vdict.get("VideoCodec") or None
    )

    return w, h, fps_val, codec


def _extract_title_video_info(title: dict):
    """
    Extract duration / width / height / fps / codec from a *title object*.

    Priority:
    1) title.Geometry + title.FrameRate + title.Duration  (most common and most reliable)
    2) title.Video
    3) title.Streams / title.StreamList
    """
    dur = _parse_duration_seconds(title)

    # Geometry (best source of width/height)
    w = h = 0
    geo = title.get("Geometry")
    if isinstance(geo, dict):
        gw, gh, _g_fps, _g_codec = _extract_whfps(geo)
        w, h = gw, gh

    # FrameRate at title-level
    fps = 0.0
    fr = title.get("FrameRate")
    if isinstance(fr, dict):
        try:
            fps = float(fr.get("Rate") or fr.get("rate") or 0.0)
        except Exception:
            fps = 0.0
    elif isinstance(fr, (int, float, str)):
        try:
            fps = float(fr)
        except Exception:
            fps = 0.0

    codec = None

    # Video dict
    v = title.get("Video")
    if isinstance(v, dict):
        vw, vh, vfps, vcodec = _extract_whfps(v)
        if w <= 0 and vw > 0:
            w = vw
        if h <= 0 and vh > 0:
            h = vh
        if fps <= 0 and vfps > 0:
            fps = vfps
        if not codec and vcodec:
            codec = vcodec

    # Streams / StreamList
    streams = title.get("Streams") or title.get("StreamList")
    if isinstance(streams, list):
        for s in streams:
            if not isinstance(s, dict):
                continue
            if s.get("Type") == "Video" or s.get("Codec") or s.get("Width") or s.get("Height"):
                sw, sh, sfps, scodec = _extract_whfps(s)
                if w <= 0 and sw > 0:
                    w = sw
                if h <= 0 and sh > 0:
                    h = sh
                if fps <= 0 and sfps > 0:
                    fps = sfps
                if not codec and scodec:
                    codec = scodec
                break

    return dur, w, h, fps, codec



def _ffprobe_media_fast(src_path: str):
    """
    Fast probe using ffprobe (much faster than HandBrake scan).
    Returns: dict with duration_sec, width, height, fps
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries",
        "format=duration:stream=duration,width,height,r_frame_rate,codec_name,pix_fmt,color_space,color_transfer,color_primaries,side_data_list",
        "-of", "json",
        src_path,
    ]
    ok, out, err = _run_cmd(cmd)
    if not ok or not out.strip():
        raise RuntimeError(f"ffprobe failed: {(err or out or '').strip()[:400]}")

    j = json.loads(out)
    fmt = (j.get("format") or {})
    streams = (j.get("streams") or [])
    v0 = streams[0] if streams else {}

    duration_sec = float(fmt.get("duration") or v0.get("duration") or 0.0)
    width = int(v0.get("width") or 0)
    height = int(v0.get("height") or 0)

    # r_frame_rate like "24000/1001"
    fps = 0.0
    rfr = v0.get("r_frame_rate") or ""
    if isinstance(rfr, str) and "/" in rfr:
        num, den = rfr.split("/", 1)
        try:
            fps = float(num) / float(den)
        except Exception:
            fps = 0.0
    else:
        try:
            fps = float(rfr or 0.0)
        except Exception:
            fps = 0.0

    if duration_sec <= 0 or width <= 0 or height <= 0:
        raise RuntimeError("ffprobe returned incomplete video info")

    is_hdr, hdr_reason = _detect_hdr_from_video_info(v0, src_path)
    hdr_format = _hdr_format_from_video_info(v0, src_path) if is_hdr else "sdr"

    bit_depth_match = re.search(r"(?:p|yuv)\d*(10|12)(?:le|be)?", str(v0.get("pix_fmt") or ""), re.IGNORECASE)
    return {
        "duration_sec": duration_sec,
        "width": width,
        "height": height,
        "fps": fps or 24.0,
        "video_codec": v0.get("codec_name") or None,
        "is_hdr": is_hdr,
        "hdr_reason": hdr_reason,
        "hdr_format": hdr_format,
        "pix_fmt": v0.get("pix_fmt") or "",
        "color_space": v0.get("color_space") or "",
        "color_transfer": v0.get("color_transfer") or "",
        "color_primaries": v0.get("color_primaries") or "",
        "bit_depth": int(bit_depth_match.group(1)) if bit_depth_match else 8,
        "hdr_side_data": v0.get("side_data_list") if isinstance(v0.get("side_data_list"), list) else [],
        "probe_source": "ffprobe",
    }


def _probe_media_text_fallback(src_path: str):
    """
    LAST RESORT fallback if JSON is present but missing duration/geometry.

    This runs a normal scan and tries to regex out:
    - duration
    - resolution

    This is intentionally permissive so the Size Wizard preview still works.
    """
    ok, out, err = _run_cmd(["HandBrakeCLI", "--scan", "-i", src_path])
    raw = ((out or "") + "\n" + (err or "")).strip()
    if not raw:
        raise RuntimeError("HandBrake scan returned no output (fallback)")

    # duration patterns
    dur_sec = 0.0
    m = re.search(r"duration:\s*(\d+):(\d+):(\d+)", raw, re.IGNORECASE)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2))
        ss = int(m.group(3))
        dur_sec = float(hh * 3600 + mm * 60 + ss)
    else:
        m2 = re.search(r"duration:\s*(\d+):(\d+)", raw, re.IGNORECASE)
        if m2:
            mm = int(m2.group(1))
            ss = int(m2.group(2))
            dur_sec = float(mm * 60 + ss)

    # resolution patterns
    w = h = 0
    m3 = re.search(r"size:\s*(\d+)\s*x\s*(\d+)", raw, re.IGNORECASE)
    if m3:
        w = int(m3.group(1))
        h = int(m3.group(2))
    else:
        # last ditch: first reasonable NxM pattern
        m4 = re.search(r"(\d{3,5})\s*x\s*(\d{3,5})", raw)
        if m4:
            w = int(m4.group(1))
            h = int(m4.group(2))

    fps = 24.0  # fallback

    if dur_sec <= 0 or w <= 0 or h <= 0:
        snippet = raw[:600].replace("\n", "\\n")
        raise RuntimeError(f"HandBrake scan JSON incomplete (fallback failed). Output snippet: {snippet}")

    return {
        "duration_sec": float(dur_sec),
        "width": int(w),
        "height": int(h),
        "fps": float(fps),
        "video_codec": None,
    }


def _probe_media_fallback(src_path: str, handbrake_reason: str):
    """Recover from a changed or partial HandBrake scan without blocking planning."""
    failures = [f"HandBrake JSON: {handbrake_reason}"]
    try:
        return _ffprobe_media_fast(src_path)
    except Exception as exc:
        failures.append(f"ffprobe: {exc}")

    try:
        return _probe_media_text_fallback(src_path)
    except Exception as exc:
        failures.append(f"HandBrake text scan: {exc}")

    detail = "; ".join(str(item)[:500] for item in failures)
    raise RuntimeError(f"Could not read this video's duration and dimensions. {detail}")


def _probe_media(src_path):
    """
    Probe using HandBrakeCLI scan JSON.

    This is the canonical probe used by:
    - /probe
    - /wizard_preview
    - /encode_wizard
    - /wizard_preview_images

    Strategy:
    1) Run --scan --json and capture stdout+stderr
    2) Extract all JSON values (objects/arrays)
    3) Find TitleList/Titles (possibly nested)
    4) Choose best title (valid geometry + longest duration)
    5) If JSON changed or is incomplete, try ffprobe and then a text scan
    """
    ok, out, err = _run_cmd(["HandBrakeCLI", "--scan", "--json", "-i", src_path])

    raw = ((out or "") + "\n" + (err or "")).strip()
    if not raw:
        return _probe_media_fallback(src_path, "scan returned no output")

    json_blobs = _extract_all_json_values(raw)
    if not json_blobs:
        snippet = raw[:600].replace("\n", "\\n")
        return _probe_media_fallback(src_path, f"no JSON found; output: {snippet}")

    scan_obj = None

    # Find a JSON value that contains TitleList/Titles
    for blob in json_blobs:
        try:
            candidate = json.loads(blob)
        except Exception:
            continue

        # Sometimes the top-level value is an array of objects
        if isinstance(candidate, list):
            for item in candidate:
                tl = _find_title_list(item)
                if isinstance(tl, list) and len(tl) > 0:
                    scan_obj = item
                    break
            if scan_obj is not None:
                break

        # Most common: object containing TitleList
        tl = _find_title_list(candidate)
        if isinstance(tl, list) and len(tl) > 0:
            scan_obj = candidate
            break

    if scan_obj is None:
        return _probe_media_fallback(src_path, "no usable title list was present")

    title_list = _find_title_list(scan_obj) or []

    # Choose best title
    best = None
    best_score = -1.0

    for t in title_list:
        if not isinstance(t, dict):
            continue

        dur, w, h, fps, codec = _extract_title_video_info(t)

        # Score: prefer valid geometry, then longer duration
        score = 0.0
        if w > 0 and h > 0:
            score += 1_000_000.0
        score += dur

        if score > best_score:
            best_score = score
            best = (dur, w, h, fps, codec)

    if not best:
        return _probe_media_fallback(src_path, "the title list contained no usable titles")

    dur, w, h, fps, codec = best
    fps = fps or 24.0

    if dur <= 0 or w <= 0 or h <= 0:
        # JSON exists but doesn't expose these fields in our parseable shape
        return _probe_media_fallback(src_path, "the selected title omitted duration or dimensions")

    hdr_reason = _hdr_filename_reason(src_path)
    result = {
        "duration_sec": float(dur),
        "width": int(w),
        "height": int(h),
        "fps": float(fps),
        "video_codec": codec,
        "is_hdr": bool(hdr_reason),
        "hdr_reason": hdr_reason,
        "probe_source": "handbrake",
    }
    # HandBrake's title JSON is reliable for title selection and geometry, but
    # several releases omit color-transfer/mastering fields from that shape.
    # Merge an ffprobe video-stream inspection for every source so an episode
    # cannot become SDR merely because its filename lacks “HDR”. Geometry and
    # duration remain sourced from the selected HandBrake title.
    try:
        technical = _ffprobe_media_fast(src_path)
    except Exception as exc:
        result["technical_probe_warning"] = str(exc)[:240]
    else:
        for key in (
            "video_codec",
            "is_hdr",
            "hdr_reason",
            "hdr_format",
            "pix_fmt",
            "color_space",
            "color_transfer",
            "color_primaries",
            "bit_depth",
            "hdr_side_data",
        ):
            if key in technical and technical.get(key) is not None and technical.get(key) != "":
                result[key] = technical[key]
        result["probe_source"] = "handbrake+ffprobe"
    return result


def _size_to_mb(value, unit):
    u = (unit or "MB").upper()
    v = float(value)
    return v * (1024.0 if u == "GB" else 1.0)


def _quality_audio_kbps(quality):
    q = (quality or "balanced").lower()
    if q == "high":
        return 256
    if q == "small":
        return 128
    return 192


def _preset_from_quality(quality):
    q = (quality or "balanced").lower()
    if q == "high":
        return "slow"
    if q == "small":
        return "fast"
    return "medium"


def _estimate_encode_fps(width, height, enc_preset):
    """Rough FPS estimates on a 'generic' modern CPU for x265.

    These are heuristics until we add job-history learning.
    """
    p = (enc_preset or "medium").lower()
    megapixels = (width * height) / 1_000_000.0 if width and height else 2.0

    base_1080 = 35.0

    mult = 1.0
    if p == "slow":
        mult = 0.65
    elif p == "fast":
        mult = 1.6

    fps = base_1080 * mult * (2.0 / max(megapixels, 0.5))
    return max(2.0, min(fps, 120.0))


def _bpp(video_bitrate_kbps, width, height, fps):
    if not video_bitrate_kbps or not width or not height or not fps:
        return 0.0
    return (video_bitrate_kbps * 1000.0) / (width * height * fps)


def _quality_label_from_bpp(bpp):
    if bpp >= 0.08:
        return ("good", "🟢 Good")
    if bpp >= 0.045:
        return ("ok", "🟡 OK / Streaming")
    return ("risky", "🔴 Risky (likely artifacts)")


WIZARD_PRESETS_FILE = os.path.join(DATA_DIR, "wizard_presets.json")
WIZARD_PRESET_LIMIT = 100

WIZARD_VIDEO_CODECS = {"h265", "h264", "av1"}
WIZARD_ENCODER_FAMILIES = {"software", "qsv"}
WIZARD_BIT_DEPTHS = {"8", "10"}
WIZARD_QUALITIES = {"high", "balanced", "small"}
WIZARD_ENCODER_SPEEDS = {"auto", "fast", "medium", "slow"}
WIZARD_RESOLUTION_MODES = {"auto", "keep", "2160", "1440", "1080", "720"}
WIZARD_AUDIO_MODES = {"auto", "aac", "copy", "eac3"}
WIZARD_SMART_AUDIO_STRATEGIES = {"", "copy", "eac3_surround"}
WIZARD_AUDIO_TRACKS = {"first", "all"}
WIZARD_SUBTITLE_MODES = {"none", "first", "all"}
WIZARD_FRAMERATE_MODES = {"same", "pfr", "cfr"}
WIZARD_FRAMERATES = {"23.976", "24", "25", "29.97", "30", "50", "59.94", "60"}
WIZARD_DEINTERLACE_MODES = {"off", "decomb", "yadif"}
WIZARD_CROP_MODES = {"auto", "none"}
WIZARD_AI_GOALS = {"balanced", "quality", "speed", "small", "archive"}
WIZARD_AI_HARDWARE = {"auto", "software", "qsv"}
WIZARD_AI_CODEC_PREFS = {"auto", "h264", "h265", "av1"}
WIZARD_AI_TRACK_SCOPES = {"first", "all"}
WIZARD_AI_SUBTITLE_SCOPES = {"none", "first", "all"}
WIZARD_AI_RISK_LEVELS = {"safe", "smart", "explorer", "bold"}

WIZARD_DEFAULT_OPTIONS = {
    "ai_mode": False,
    "ai_goal": "balanced",
    "ai_hardware": "auto",
    "ai_codec_preference": "auto",
    "ai_risk": "smart",
    "ai_copy_audio": True,
    "ai_audio_scope": "all",
    "ai_subtitle_scope": "all",
    "audio_languages": ["eng", "spa"],
    "subtitle_languages": ["eng", "spa"],
    "preset": "auto",
    "target_size_auto": True,
    # Automatic sizing replaces this neutral parsing fallback with a source-
    # aware estimate. It must never be presented as a universal movie target.
    "target_size_value": 1.0,
    "target_size_unit": "GB",
    "quality": "balanced",
    "video_codec": "h265",
    "encoder_family": "software",
    "bit_depth": "10",
    "encoder_speed": "auto",
    "resolution_mode": "auto",
    "audio_mode": "copy",
    "smart_audio_strategy": "",
    "smart_subtitle_strategy": "",
    "smart_never_downscale": False,
    "smart_keep_black_bars": False,
    "smart_keep_aspect_ratio": False,
    "smart_keep_all_audio_languages": False,
    "smart_keep_all_subtitle_languages": False,
    "smart_never_transcode_audio": False,
    "audio_bitrate": "auto",
    "audio_tracks": "all",
    "subtitle_mode": "all",
    "framerate_mode": "same",
    "framerate": "23.976",
    "smart_lock_source_framerate": False,
    "deinterlace": "off",
    "crop_mode": "auto",
    "two_pass": False,
}

WIZARD_SOURCE_TARGETS = {
    "movie": {"label": "Movie"},
    "show": {"label": "Show episode"},
}

WIZARD_SHOW_PATTERNS = [
    re.compile(r"(?:^|[ ._\-\[\(])s\d{1,2}e\d{1,3}(?:\D|$)", re.IGNORECASE),
    re.compile(r"(?:^|[ ._\-\[\(])\d{1,2}x\d{1,3}(?:\D|$)", re.IGNORECASE),
    re.compile(r"(?:^|[ ._\-\[\(])season[ ._\-]*\d+", re.IGNORECASE),
    re.compile(r"(?:^|[ ._\-\[\(])episode[ ._\-]*\d+", re.IGNORECASE),
]


def _choice(value, allowed: set[str], default: str) -> str:
    val = str(value or default).strip().lower()
    return val if val in allowed else default


def _truthy(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


WIZARD_LANGUAGE_ALIASES = {
    "en": "eng",
    "english": "eng",
    "eng": "eng",
    "ja": "jpn",
    "jp": "jpn",
    "japanese": "jpn",
    "jpn": "jpn",
    "es": "spa",
    "spanish": "spa",
    "spa": "spa",
    "fr": "fre",
    "french": "fre",
    "fre": "fre",
    "fra": "fre",
    "de": "ger",
    "german": "ger",
    "ger": "ger",
    "deu": "ger",
    "it": "ita",
    "italian": "ita",
    "ita": "ita",
    "pt": "por",
    "portuguese": "por",
    "por": "por",
    "ko": "kor",
    "korean": "kor",
    "kor": "kor",
    "zh": "chi",
    "chinese": "chi",
    "chi": "chi",
    "zho": "chi",
    "nl": "dut",
    "dutch": "dut",
    "dut": "dut",
    "nld": "dut",
    "ru": "rus",
    "russian": "rus",
    "rus": "rus",
    "pl": "pol",
    "polish": "pol",
    "pol": "pol",
    "und": "und",
    "unknown": "und",
}


def _wizard_languages(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw = " ".join(str(v) for v in value)
    else:
        raw = str(value)

    out = []
    seen = set()
    for part in re.split(r"[,;/\s]+", raw.strip().lower()):
        if not part:
            continue
        token = WIZARD_LANGUAGE_ALIASES.get(part, part)
        token = re.sub(r"[^a-z0-9]", "", token)[:3]
        if len(token) != 3 or token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= 12:
            break
    return out


def _wizard_source_target(kind: str) -> dict:
    target = WIZARD_SOURCE_TARGETS.get(kind) or WIZARD_SOURCE_TARGETS["movie"]
    return {"label": target["label"]}


def _wizard_detect_source_type(src_path: str, duration_sec=None) -> dict:
    base = os.path.basename(src_path or "")
    lower_base = base.lower()
    lower_path = (src_path or "").replace("\\", "/").lower()
    path_parts = [part for part in re.split(r"[\\/]+", lower_path) if part]

    for pattern in WIZARD_SHOW_PATTERNS:
        if pattern.search(lower_base):
            target = _wizard_source_target("show")
            return {"kind": "show", "reason": "episode filename pattern", **target}

    if any(part in {"tv", "tv shows", "shows", "show", "series", "seasons"} for part in path_parts):
        target = _wizard_source_target("show")
        return {"kind": "show", "reason": "show folder path", **target}

    if any(part in {"movie", "movies", "film", "films"} for part in path_parts):
        target = _wizard_source_target("movie")
        return {"kind": "movie", "reason": "movie folder path", **target}

    try:
        duration = float(duration_sec or 0.0)
    except Exception:
        duration = 0.0

    if 0 < duration <= 75 * 60:
        target = _wizard_source_target("show")
        return {"kind": "show", "reason": "runtime under 75 minutes", **target}

    target = _wizard_source_target("movie")
    reason = "runtime over 75 minutes" if duration > 0 else "default"
    return {"kind": "movie", "reason": reason, **target}


def _wizard_target_geometry(options: dict, src_w: int, src_h: int) -> tuple[int, int]:
    """Return the largest picture the current Wizard choice can produce.

    This is deliberately independent of the target bitrate so automatic size
    estimation and automatic resolution selection do not form a circular
    calculation. Explicit caps never upscale a smaller source.
    """
    mode = str(options.get("resolution_mode") or "auto")
    if mode in {"720", "1080", "1440", "2160"} and src_h > int(mode):
        return _scale_to_height(src_w, src_h, int(mode))
    return src_w, src_h


def _wizard_size_preference_factor(options: dict) -> float:
    """Comparable size pressure for learned/history target adjustment."""
    quality = {"high": 1.28, "balanced": 1.0, "small": 0.74}.get(
        str(options.get("quality") or "balanced"), 1.0
    )
    codec = {"h264": 1.24, "h265": 1.0, "av1": 0.84}.get(
        str(options.get("video_codec") or "h265"), 1.0
    )
    family = 1.10 if str(options.get("encoder_family") or "software") == "qsv" else 1.0
    speed_name = str(options.get("encoder_speed") or options.get("speed") or "auto")
    speed = {"fast": 1.09, "medium": 1.0, "slow": 0.94, "auto": 1.0}.get(speed_name, 1.0)
    return quality * codec * family * speed


def _wizard_auto_target(
    options: dict,
    info: dict,
    source_type: dict,
    source_size_bytes: int,
    effective_preset: str,
    learned_defaults: dict | None = None,
) -> tuple[float, dict]:
    """Estimate an output target from this title instead of a fixed 5 GB.

    The baseline is a bits-per-pixel model. Completed encodes and explicitly
    approved Size Wizard plans are blended in when available, while the
    current codec/quality/resolution choices always continue to affect the
    estimate.
    """
    duration_sec = max(1.0, float(info.get("duration_sec") or 0.0))
    fps = max(1.0, float(info.get("fps") or 24.0))
    src_w = max(2, int(info.get("width") or 0))
    src_h = max(2, int(info.get("height") or 0))
    out_w, out_h = _wizard_target_geometry(options, src_w, src_h)

    codec = str(options.get("video_codec") or "h265")
    quality = str(options.get("quality") or "balanced")
    bpp_table = {
        "h264": {"high": 0.105, "balanced": 0.078, "small": 0.057},
        "h265": {"high": 0.078, "balanced": 0.057, "small": 0.042},
        "av1": {"high": 0.066, "balanced": 0.048, "small": 0.035},
    }
    target_bpp = bpp_table.get(codec, bpp_table["h265"]).get(quality, 0.057)
    if str(options.get("encoder_family") or "software") == "qsv":
        target_bpp *= 1.10
    speed = str(options.get("encoder_speed") or "auto")
    target_bpp *= {"fast": 1.08, "medium": 1.0, "slow": 0.95, "auto": 1.0}.get(speed, 1.0)
    if bool(info.get("is_hdr")):
        target_bpp *= 1.14

    video_kbps = max(300.0, target_bpp * out_w * out_h * fps / 1000.0)
    audio_kbps = max(64, _wizard_audio_kbps(options))
    modeled_mb = ((video_kbps + audio_kbps) * 1000.0 * duration_sec / 8.0) / (1024.0 * 1024.0)
    estimate_mb = modeled_mb
    signals = ["title runtime", f"{out_w}x{out_h}", codec.upper(), quality]

    learned = learned_defaults if isinstance(learned_defaults, dict) else {}
    learned_adjusted_ratio = 0.0
    try:
        learned_ratio = float(learned.get("target_ratio") or 0.0)
        learned_samples = int(learned.get("sample_count") or 0)
        learned_confidence = float(learned.get("confidence") or 0.0)
    except (TypeError, ValueError):
        learned_ratio = learned_confidence = 0.0
        learned_samples = 0
    if source_size_bytes > 0 and learned_ratio > 0 and learned_samples > 0:
        learned_options = {
            "quality": learned.get("quality") or "balanced",
            "video_codec": learned.get("codec") or "h265",
            "encoder_family": learned.get("encoder_family") or "software",
            "encoder_speed": learned.get("speed") or "auto",
        }
        adjustment = _wizard_size_preference_factor(options) / max(
            0.25, _wizard_size_preference_factor(learned_options)
        )
        learned_mb = source_size_bytes * learned_ratio * adjustment / (1024.0 * 1024.0)
        learned_adjusted_ratio = learned_ratio * adjustment
        learned_weight = min(0.55, 0.20 + (0.30 * max(0.0, min(1.0, learned_confidence))))
        estimate_mb = estimate_mb * (1.0 - learned_weight) + learned_mb * learned_weight
        signals.append(f"{learned_samples} approved similar plan{'s' if learned_samples != 1 else ''}")

    try:
        history = _history_prediction_for(
            source_size_bytes,
            effective_preset,
            bool(info.get("is_hdr")),
            encode_method=_wizard_encoder_name(options),
        )
    except Exception:
        history = {"available": False}
    if history.get("available") and history.get("estimated_out_bytes"):
        history_mb = float(history["estimated_out_bytes"]) / (1024.0 * 1024.0)
        history_mb *= {"high": 1.20, "balanced": 1.0, "small": 0.80}.get(quality, 1.0)
        history_weight = {"high": 0.30, "medium": 0.22, "low": 0.12}.get(
            str(history.get("confidence") or "low"), 0.12
        )
        estimate_mb = estimate_mb * (1.0 - history_weight) + history_mb * history_weight
        signals.append(f"{int(history.get('sample_count') or 0)} completed encode samples")

    # An output target larger than an already compressed source is not useful.
    # Keep enough room for audio and a conservative video floor on tiny files.
    minimum_mb = max(
        32.0,
        ((audio_kbps + 300.0) * 1000.0 * duration_sec / 8.0) / (1024.0 * 1024.0),
    )
    maximum_mb = 0.0
    if source_size_bytes > 0:
        source_mb = source_size_bytes / (1024.0 * 1024.0)
        # The ceiling also reflects the chosen compression goal. Otherwise an
        # already-small source could cap High, Balanced, H.264, and H.265 to
        # the same number and make the controls appear broken again.
        ceiling_ratio = {
            "high": 0.93,
            "balanced": 0.80,
            "small": 0.62,
        }.get(quality, 0.80)
        ceiling_ratio *= {"h264": 1.08, "h265": 1.0, "av1": 0.92}.get(codec, 1.0)
        if str(options.get("encoder_family") or "software") == "qsv":
            ceiling_ratio *= 1.03
        if learned_adjusted_ratio > 0:
            ceiling_ratio = max(ceiling_ratio, min(0.97, learned_adjusted_ratio))
        ceiling_ratio = max(0.35, min(0.97, ceiling_ratio))
        maximum_mb = max(minimum_mb, source_mb * ceiling_ratio)
        estimate_mb = min(estimate_mb, maximum_mb)
    estimate_mb = round(max(minimum_mb, estimate_mb), 1)
    return estimate_mb, {
        "mode": "source_aware",
        "target_mb": estimate_mb,
        "model_target_mb": round(modeled_mb, 1),
        "source_ceiling_mb": round(maximum_mb, 1) if maximum_mb else None,
        "source_kind": str(source_type.get("kind") or "movie"),
        "signals": signals,
        "summary": "Calculated for this source from " + ", ".join(signals) + ".",
        "learned_sample_count": learned_samples,
        "history_sample_count": int(history.get("sample_count") or 0),
    }


def _wizard_apply_source_target(
    options: dict,
    source_type: dict,
    *,
    info: dict | None = None,
    source_size_bytes: int = 0,
    effective_preset: str = "auto",
    learned_defaults: dict | None = None,
) -> tuple[dict, dict]:
    if not options.get("target_size_auto"):
        return options, {"mode": "manual", "summary": "Manual target size."}
    info = info if isinstance(info, dict) else {}
    target_mb, details = _wizard_auto_target(
        options,
        info,
        source_type,
        source_size_bytes,
        effective_preset,
        learned_defaults,
    )
    out = options.copy()
    out["target_size_value"] = target_mb
    out["target_size_unit"] = "MB"
    return out, details


def _wizard_encoder_label(encoder_name: str) -> str:
    labels = {
        "x264": "H.264 CPU",
        "x265": "H.265 CPU",
        "x265_10bit": "H.265 10-bit CPU",
        "svt_av1": "AV1 SVT CPU",
        "svt_av1_10bit": "AV1 10-bit SVT CPU",
        "qsv_h264": "H.264 Intel QSV",
        "qsv_h265": "H.265 Intel QSV",
        "qsv_h265_10bit": "H.265 10-bit Intel QSV",
    }
    return labels.get(encoder_name, encoder_name)


def _wizard_encoder_name(options: dict) -> str:
    codec = options["video_codec"]
    family = options["encoder_family"]
    bit_depth = options["bit_depth"]

    if codec == "av1":
        return "svt_av1_10bit" if bit_depth == "10" else "svt_av1"

    if family == "qsv":
        if codec == "h264":
            return "qsv_h264"
        return "qsv_h265_10bit" if bit_depth == "10" else "qsv_h265"

    if codec == "h264":
        return "x264"
    return "x265_10bit" if bit_depth == "10" else "x265"


def _wizard_encoder_preset(options: dict) -> str:
    speed = options["encoder_speed"]
    quality = options["quality"]
    family = options["encoder_family"]
    codec = options.get("video_codec")

    if speed == "auto":
        speed = "slow" if quality == "high" else ("fast" if quality == "small" else "medium")

    if codec == "av1":
        return {"fast": "8", "medium": "6", "slow": "4"}.get(speed, "6")
    if family == "qsv":
        return {"fast": "speed", "medium": "balanced", "slow": "quality"}.get(speed, "balanced")
    return {"fast": "fast", "medium": "medium", "slow": "slow"}.get(speed, "medium")


def _wizard_audio_kbps(options: dict) -> int:
    if options["audio_mode"] == "copy":
        return 256
    if options["audio_mode"] == "eac3":
        return 640
    if options["audio_bitrate"] != "auto":
        try:
            return max(64, min(640, int(options["audio_bitrate"])))
        except Exception:
            pass
    return _quality_audio_kbps(options["quality"])


def _scale_to_height(width: int, height: int, target_height: int) -> tuple[int, int]:
    if width <= 0 or height <= 0 or target_height <= 0 or height <= target_height:
        return width, height
    scale = target_height / float(height)
    out_w = int((width * scale) // 2 * 2)
    out_h = int((height * scale) // 2 * 2)
    return max(2, out_w), max(2, out_h)


def _wizard_likely_qsv(cpu_label: str) -> bool:
    label = (cpu_label or "").lower()
    if "intel" not in label:
        return False
    return "no igpu" not in label and "kf" not in label


def _wizard_ai_resolution_mode(
    *,
    goal: str,
    src_w: int,
    src_h: int,
    bpp_source: float,
    cpu_score: float,
    source_kind: str,
) -> tuple[str, list[str], list[str]]:
    """Choose a resolution cap from target-size pressure without probing again."""
    notes: list[str] = []
    warnings: list[str] = []
    is_4k = src_h >= 1800 or src_w >= 3200
    is_1080 = src_h >= 900 or src_w >= 1600
    weak_cpu = cpu_score < 0.75

    if goal == "speed":
        if is_4k and (weak_cpu or bpp_source < 0.055):
            notes.append("Capped 4K source at 1080p for a faster, safer encode.")
            return "1080", notes, warnings
        if is_1080 and source_kind == "show" and bpp_source < 0.032:
            warnings.append("Target is very tight for this episode; 720p may look cleaner than starved 1080p.")
            return "720", notes, warnings
        return "auto", notes, warnings

    if goal == "small":
        if is_4k:
            cap = "1080" if bpp_source < 0.055 else "1440"
            notes.append(f"Capped 4K source at {cap}p to save space without starving bitrate.")
            return cap, notes, warnings
        if is_1080 and bpp_source < 0.035:
            notes.append("Capped tight 1080p target at 720p to avoid blocky output.")
            return "720", notes, warnings
        return "auto", notes, warnings

    if goal == "quality":
        if is_4k and bpp_source < 0.035:
            warnings.append("Target size is tight for 4K quality; AI will allow auto downscale to protect detail.")
            return "auto", notes, warnings
        notes.append("Keeping resolution unless the target is too tight.")
        return "keep", notes, warnings

    if goal == "archive":
        if is_4k and bpp_source < 0.04:
            notes.append("Archive target is tight for 4K, so AI will allow a 1440p cap.")
            return "1440", notes, warnings
        return "auto", notes, warnings

    # Balanced profile.
    if is_4k and bpp_source < 0.038:
        warnings.append("Target is aggressive for 4K; AI capped at 1080p for a cleaner result.")
        return "1080", notes, warnings
    if is_4k and bpp_source < 0.055:
        notes.append("Target is moderately tight for 4K; AI capped at 1440p.")
        return "1440", notes, warnings
    return "auto", notes, warnings


def _wizard_ai_choices(
    options: dict,
    cpu,
    cpu_override: float,
    info: dict,
    source_type: dict,
    source_size_bytes: int,
    target_mb: float,
    effective_preset: str,
    qsv_device_available: bool,
) -> tuple[dict, dict]:
    if not options.get("ai_mode"):
        return options, {
            "summary": "",
            "decisions": [],
            "warnings": [],
            "profile": {},
        }

    out = options.copy()
    goal = out["ai_goal"]
    hw = out["ai_hardware"]
    codec_pref = out.get("ai_codec_preference") or "auto"
    risk = out.get("ai_risk") or "smart"
    risk_score = {"safe": 0, "smart": 1, "explorer": 2, "bold": 3}.get(risk, 1)
    cpu_score = max(0.1, float(getattr(cpu, "speed_index", 1.0)) * float(cpu_override or 1.0))
    cpu_qsv_capable = _wizard_likely_qsv(getattr(cpu, "label", ""))
    qsv_ok = bool(qsv_device_available) and (hw == "qsv" or (hw == "auto" and cpu_qsv_capable))
    weak_cpu = cpu_score < 0.75
    strong_cpu = cpu_score >= 1.35
    very_strong_cpu = cpu_score >= 2.0
    src_w = int(info.get("width") or 0)
    src_h = int(info.get("height") or 0)
    duration_sec = float(info.get("duration_sec") or 0.0)
    fps = float(info.get("fps") or 0.0) or 24.0
    is_hdr = bool(info.get("is_hdr"))
    is_4k = src_h >= 1800 or src_w >= 3200
    source_kind = source_type.get("kind") or "movie"
    source_size_bytes_i = max(1, int(source_size_bytes or 1))
    target_bytes = max(1.0, float(target_mb or 0.0) * 1024.0 * 1024.0)
    target_ratio = target_bytes / float(source_size_bytes_i)
    source_bitrate_kbps = (source_size_bytes_i * 8.0 / max(1.0, duration_sec)) / 1000.0
    target_total_kbps = (target_bytes * 8.0 / max(1.0, duration_sec)) / 1000.0
    if out.get("smart_audio_strategy") == "eac3_surround":
        rough_audio_kbps = 640
    elif out.get("smart_audio_strategy") == "copy" or out.get("ai_copy_audio", True):
        rough_audio_kbps = 256
    else:
        rough_audio_kbps = _quality_audio_kbps(out.get("quality"))
    rough_video_kbps = max(250.0, target_total_kbps - rough_audio_kbps)
    bpp_source = _bpp(rough_video_kbps, src_w, src_h, fps)
    decisions: list[str] = []
    warnings: list[str] = []
    explored: list[dict] = []

    risk_label = {
        "safe": "Safe",
        "smart": "Smart",
        "explorer": "Explorer",
        "bold": "Bold",
    }.get(risk, "Smart")
    decisions.append(f"AI risk mode: {risk_label}.")
    if risk_score >= 2:
        warnings.append("Explorer AI may choose slower or more aggressive settings when the source and target suggest a better result.")

    if is_hdr:
        decisions.append("Detected HDR/10-bit hints, so AI protects 10-bit encoder choices.")
    if target_ratio < 0.18:
        warnings.append("Target is much smaller than the source; expect stronger compression.")
    elif target_ratio < 0.35:
        decisions.append("Target is a meaningful storage-saving encode.")

    if hw == "qsv" and not qsv_device_available:
        out["encoder_family"] = "software"
        warnings.append("Intel QSV was requested, but Settings says /dev/dri is not available, so AI used CPU encoding.")
    elif hw == "qsv" and not cpu_qsv_capable:
        out["encoder_family"] = "software"
        warnings.append("Intel QSV was requested, but the selected CPU profile does not look QSV-capable.")
    elif hw == "software":
        out["encoder_family"] = "software"
        decisions.append("Hardware set to CPU only.")
    elif hw == "qsv":
        out["encoder_family"] = "qsv"
        decisions.append("Intel QSV is enabled in Settings and was requested.")
    elif goal == "speed" and qsv_ok:
        out["encoder_family"] = "qsv"
        decisions.append("Using Intel QSV because speed is the goal and /dev/dri is enabled.")
    elif weak_cpu and qsv_ok:
        out["encoder_family"] = "qsv"
        decisions.append("Using Intel QSV because the selected CPU profile is slower and /dev/dri is enabled.")
    elif goal in {"small", "archive", "quality"} and not weak_cpu:
        out["encoder_family"] = "software"
        decisions.append("Using CPU software encoding for better compression efficiency.")
    else:
        out["encoder_family"] = "software"

    if goal == "speed":
        out["quality"] = "small"
        out["video_codec"] = "h265" if (is_hdr or out["encoder_family"] == "qsv" or not weak_cpu) else "h264"
        out["encoder_speed"] = "fast"
        decisions.append("Speed profile selected fast encoder settings.")
    elif goal == "small":
        out["quality"] = "small"
        out["video_codec"] = "h265"
        out["encoder_speed"] = "slow" if out["encoder_family"] == "software" and strong_cpu else "medium"
        decisions.append("Small-file profile selected HEVC and tighter compression.")
    elif goal == "quality":
        out["quality"] = "high"
        out["video_codec"] = "h265"
        out["encoder_speed"] = "slow" if out["encoder_family"] == "software" and strong_cpu else "medium"
        decisions.append("Quality profile selected higher bitrate protection.")
    elif goal == "archive":
        out["quality"] = "balanced"
        out["video_codec"] = "h265"
        out["encoder_speed"] = "slow" if out["encoder_family"] == "software" and very_strong_cpu else "medium"
        decisions.append("Archive profile selected long-term HEVC settings.")
    else:
        out["quality"] = "balanced"
        out["video_codec"] = "h265"
        out["encoder_speed"] = "medium" if out["encoder_family"] == "software" else "auto"
        decisions.append("Balanced profile selected a safe quality/size mix.")

    history_choice = {}
    history_candidates = []
    preset_for_history = effective_preset if effective_preset in {"1080", "4k"} else ("4k" if is_4k else "1080")
    try:
        history_model = _history_prediction_model()
        for method, codec, family, depth, runtime_weight in (
            ("x264", "h264", "software", "8", 1.15),
            ("x265_10bit", "h265", "software", "10", 1.0),
            ("qsv_h265_10bit", "h265", "qsv", "10", 0.35),
            ("svt_av1_10bit", "av1", "software", "10", 2.8),
        ):
            if family == "qsv" and not qsv_ok:
                continue
            if codec == "av1" and goal == "speed" and codec_pref != "av1":
                continue
            pred = _history_prediction_for(
                source_size_bytes_i,
                preset_for_history,
                is_hdr,
                history_model,
                encode_method=method,
                strict_method=True,
            )
            if pred.get("available"):
                src_gb = max(source_size_bytes_i / float(1024**3), 0.01)
                candidate = {
                    "method": method,
                    "codec": codec,
                    "family": family,
                    "bit_depth": depth,
                    "out_ratio": float(pred.get("out_ratio") or 0.0),
                    "seconds_per_gb": (float(pred.get("estimated_runtime_seconds") or 0.0) / src_gb) if pred.get("estimated_runtime_seconds") else None,
                    "sample_count": int(pred.get("sample_count") or 0),
                    "runtime_weight": runtime_weight,
                }
                history_candidates.append(candidate)
                explored.append({
                    "method": method,
                    "codec": codec,
                    "encoder_family": family,
                    "sample_count": candidate["sample_count"],
                    "estimated_ratio": round(candidate["out_ratio"], 4),
                    "seconds_per_gb": round(candidate["seconds_per_gb"], 1) if candidate["seconds_per_gb"] else None,
                })
    except Exception:
        history_candidates = []

    if history_candidates:
        decisions.append(f"AI explored {len(history_candidates)} learned encoder path{'' if len(history_candidates) == 1 else 's'} from job history.")
    elif risk_score >= 2:
        decisions.append("AI did not find enough matching history, so Explorer mode falls back to source/target heuristics.")

    def apply_history_candidate(candidate: dict, reason: str) -> None:
        nonlocal history_choice
        if not candidate:
            return
        out["video_codec"] = candidate["codec"]
        out["encoder_family"] = candidate["family"]
        out["bit_depth"] = candidate["bit_depth"]
        if candidate["codec"] == "av1":
            out["encoder_speed"] = "slow" if very_strong_cpu and goal in {"quality", "archive"} else "medium"
        elif candidate["family"] == "qsv":
            out["encoder_speed"] = "fast" if goal == "speed" else "auto"
        history_choice = {
            "method": candidate["method"],
            "sample_count": candidate["sample_count"],
            "out_ratio": round(candidate["out_ratio"], 4),
            "reason": reason,
        }
        decisions.append(
            f"History learned from {candidate['sample_count']} {candidate['method']} job"
            f"{'' if candidate['sample_count'] == 1 else 's'} and selected it because {reason}."
        )

    if codec_pref in {"h264", "h265", "av1"}:
        out["video_codec"] = codec_pref
        if codec_pref == "av1":
            out["encoder_family"] = "software"
            out["bit_depth"] = "10"
            if goal == "speed":
                warnings.append("AV1 was requested, but AV1 is usually not a fast encode. Expect longer runtimes.")
        elif codec_pref == "h264":
            out["encoder_family"] = "software" if out["encoder_family"] != "qsv" else out["encoder_family"]
            out["bit_depth"] = "8"
        decisions.append(f"AI codec preference forced {codec_pref.upper()}.")
    elif history_candidates:
        if goal == "speed":
            speed_candidates = [c for c in history_candidates if c["codec"] != "av1"]
            if speed_candidates:
                best = min(speed_candidates, key=lambda c: c["seconds_per_gb"] if c["seconds_per_gb"] else c["runtime_weight"] * 9999)
                if best["family"] == "qsv" or best["sample_count"] >= 3:
                    apply_history_candidate(best, "it has been the fastest matching method in past history")
        else:
            efficient_candidates = [c for c in history_candidates if c["codec"] in {"h265", "av1"}]
            if efficient_candidates:
                best = min(efficient_candidates, key=lambda c: c["out_ratio"])
                current_method = "svt_av1_10bit" if out["video_codec"] == "av1" else ("qsv_h265_10bit" if out["encoder_family"] == "qsv" else "x265_10bit")
                current = next((c for c in efficient_candidates if c["method"] == current_method), None)
                improvement = (current["out_ratio"] - best["out_ratio"]) if current else 0.0
                av1_reasonable = best["codec"] != "av1" or (
                    cpu_score >= (0.85 if risk_score >= 3 else 1.0)
                    and duration_sec <= 4 * 60 * 60
                    and (target_ratio < (0.45 if risk_score >= 2 else 0.35) or goal in {"quality", "archive", "small"})
                )
                improvement_needed = 0.035 if risk_score >= 3 else (0.05 if risk_score >= 2 else 0.08)
                if av1_reasonable and ((best["codec"] == "av1" and (improvement >= improvement_needed or target_ratio < (0.30 if risk_score >= 2 else 0.22))) or (best["sample_count"] >= 3 and improvement >= improvement_needed)):
                    apply_history_candidate(best, "it has produced better size efficiency for similar source and preset history")

    if codec_pref == "auto" and out["video_codec"] != "av1":
        av1_quality_ratio = 0.34 if risk_score >= 3 else (0.28 if risk_score >= 2 else 0.22)
        av1_balanced_ratio = 0.22 if risk_score >= 3 else (0.18 if risk_score >= 2 else 0.16)
        av1_cpu_ok = cpu_score >= (0.85 if risk_score >= 3 else (1.0 if risk_score >= 2 else 1.35))
        if goal in {"quality", "archive"} and target_ratio < av1_quality_ratio and av1_cpu_ok and duration_sec <= (4 * 60 * 60 if risk_score >= 2 else 3 * 60 * 60):
            out["video_codec"] = "av1"
            out["encoder_family"] = "software"
            out["bit_depth"] = "10"
            out["encoder_speed"] = "medium" if goal == "quality" else "slow"
            decisions.append("Target is tight and CPU profile is strong, so AI selected AV1 for better compression efficiency.")
        elif goal == "balanced" and target_ratio < av1_balanced_ratio and av1_cpu_ok and duration_sec <= (3.5 * 60 * 60 if risk_score >= 2 else 2.5 * 60 * 60):
            out["video_codec"] = "av1"
            out["encoder_family"] = "software"
            out["bit_depth"] = "10"
            out["encoder_speed"] = "medium"
            warnings.append("Balanced target is very aggressive; AI selected AV1, which may be much slower but can preserve quality at smaller sizes.")
        elif risk_score >= 3 and goal == "small" and target_ratio < 0.42 and cpu_score >= 0.85:
            out["video_codec"] = "av1"
            out["encoder_family"] = "software"
            out["bit_depth"] = "10"
            out["encoder_speed"] = "medium"
            warnings.append("Bold AI chose AV1 for small-file exploration. It may be slow, but it can win on difficult size targets.")

    requested_resolution = str(out.get("resolution_mode") or "auto")
    resolution_locked = requested_resolution != "auto"
    if resolution_locked:
        out["resolution_mode"] = requested_resolution
        if requested_resolution == "keep":
            decisions.append("Kept the source resolution because the user locked resolution; AI will not downscale it.")
            if is_4k and bpp_source < 0.045:
                warnings.append("Keeping 4K is locked, but the target bitrate is tight. Increase the target size if the preview looks soft or blocky.")
        else:
            decisions.append(f"Honored the user-selected {requested_resolution}p resolution cap.")
    else:
        resolution_mode, resolution_notes, resolution_warnings = _wizard_ai_resolution_mode(
            goal=goal,
            src_w=src_w,
            src_h=src_h,
            bpp_source=bpp_source,
            cpu_score=cpu_score,
            source_kind=source_kind,
        )
        out["resolution_mode"] = resolution_mode
        decisions.extend(resolution_notes)
        warnings.extend(resolution_warnings)

    if not resolution_locked and risk_score >= 2 and goal in {"small", "balanced"} and is_4k and target_ratio < 0.48 and out["resolution_mode"] in {"auto", "keep"}:
        out["resolution_mode"] = "1440" if target_ratio >= 0.26 and risk_score < 3 else "1080"
        warnings.append(f"Explorer AI capped 4K at {out['resolution_mode']}p to spend bitrate on cleaner pixels instead of sheer resolution.")
    elif not resolution_locked and risk_score >= 3 and source_kind == "show" and src_h >= 900 and target_mb <= 700 and out["resolution_mode"] in {"auto", "keep"}:
        out["resolution_mode"] = "720"
        warnings.append("Bold AI capped this episode at 720p because the target is very small.")

    if is_hdr:
        if out["video_codec"] == "h264":
            out["video_codec"] = "h265"
        out["bit_depth"] = "10"
    else:
        out["bit_depth"] = "8" if out["video_codec"] == "h264" else "10"

    if out["video_codec"] == "av1":
        out["encoder_family"] = "software"
        out["bit_depth"] = "10" if out.get("bit_depth") != "8" else "8"

    if out["encoder_family"] == "qsv" and out["video_codec"] == "h264" and is_hdr:
        out["video_codec"] = "h265"
        out["bit_depth"] = "10"

    deinterlace_hint = bool(re.search(r"(?<!\d)(480i|576i|1080i)(?!\d)|interlac", str(info.get("path") or "") + " " + str(info.get("source_name") or ""), re.IGNORECASE))
    if deinterlace_hint:
        out["deinterlace"] = "decomb"
        decisions.append("Detected interlaced filename hint, so decomb is enabled.")
    else:
        out["deinterlace"] = "off"

    out["two_pass"] = (
        out["encoder_family"] == "software"
        and goal in {"small", "archive", "balanced", "quality"}
        and cpu_score >= (0.85 if risk_score >= 2 else 1.0)
        and duration_sec <= (5 * 60 * 60 if risk_score >= 2 else 4 * 60 * 60)
        and bpp_source < (0.085 if risk_score >= 2 else 0.07)
    )
    if out["two_pass"]:
        decisions.append("Enabled two-pass because the target is tight and CPU mode can benefit from it.")

    smart_audio_strategy = out.get("smart_audio_strategy") or ""
    if smart_audio_strategy == "eac3_surround":
        out["audio_mode"] = "eac3"
        out["audio_bitrate"] = "640"
        decisions.append("Encoding the selected audio tracks to E-AC3 5.1 at 640 kbps to save space while retaining surround sound.")
    elif smart_audio_strategy == "copy":
        out["audio_mode"] = "copy"
    else:
        out["audio_mode"] = "copy" if out["ai_copy_audio"] else "aac"

    if smart_audio_strategy:
        out["audio_tracks"] = "all"
        out["ai_audio_scope"] = "all"
        out["ai_subtitle_scope"] = out.get("smart_subtitle_strategy") or "all"

    if out["audio_mode"] == "copy":
        out["audio_bitrate"] = "auto"
        decisions.append("Copying audio to avoid quality loss.")
    elif out["audio_mode"] == "eac3":
        # The fixed surround-safe rate above is intentional; do not let the
        # video quality goal reduce it to a stereo-oriented AAC budget.
        out["audio_bitrate"] = "640"
    elif goal == "quality":
        out["audio_bitrate"] = "256"
        decisions.append("Converting audio at 256 kbps for quality.")
    elif goal in {"small", "speed"}:
        out["audio_bitrate"] = "160"
        decisions.append("Converting audio at 160 kbps to save space.")
    else:
        out["audio_bitrate"] = "192"

    out["audio_tracks"] = out["ai_audio_scope"]
    out["subtitle_mode"] = out["ai_subtitle_scope"]
    out["framerate_mode"] = "same"
    out["crop_mode"] = "auto"

    if out["subtitle_mode"] == "none":
        warnings.append("Subtitles are disabled for this AI profile.")
    elif out["subtitle_mode"] == "all":
        decisions.append("Keeping all matching subtitles without burn-in.")

    decisions.append(f"CPU profile {getattr(cpu, 'label', 'default')} at x{cpu_score:.2f}.")
    decisions.append(f"Selected {out['encoder_family'].upper()} {out['video_codec'].upper()} {out['bit_depth']}-bit, {out['encoder_speed']} speed.")
    if out["audio_languages"]:
        decisions.append("Audio languages: " + ", ".join(out["audio_languages"]) + ".")
    else:
        decisions.append("Audio language filter disabled; track scope controls what is kept.")
    if out["subtitle_languages"]:
        decisions.append("Subtitle languages: " + ", ".join(out["subtitle_languages"]) + ".")
    elif out["subtitle_mode"] != "none":
        decisions.append("Subtitle language filter disabled; keeping by subtitle scope.")

    goal_label = {
        "balanced": "Balanced",
        "quality": "Best quality",
        "speed": "Fast encode",
        "small": "Small file",
        "archive": "Archive",
    }.get(goal, "AI")
    audio_summary = (
        "copy audio"
        if out["audio_mode"] == "copy"
        else ("E-AC3 5.1 audio" if out["audio_mode"] == "eac3" else out["audio_bitrate"] + " kbps audio")
    )
    summary = (
        f"{goal_label}: {_wizard_encoder_label(_wizard_encoder_name(out))}, "
        f"{out['resolution_mode']} resolution, "
        f"{audio_summary}, "
        f"{out['subtitle_mode']} subtitles."
    )
    profile = {
        "goal": goal,
        "source_kind": source_kind,
        "source_resolution": f"{src_w}x{src_h}",
        "source_bitrate_kbps": round(source_bitrate_kbps, 1),
        "target_ratio": round(target_ratio, 3),
        "target_bpp_at_source": round(bpp_source, 5),
        "cpu_score": round(cpu_score, 3),
        "qsv_likely": bool(qsv_ok),
        "qsv_device_available": bool(qsv_device_available),
        "codec_preference": codec_pref,
        "history_choice": history_choice,
        "explored": explored[:6],
        "risk": risk,
        "hdr": bool(is_hdr),
        "resolution_mode": out["resolution_mode"],
        "resolution_locked": resolution_locked,
    }
    return out, {
        "summary": summary,
        "decisions": decisions,
        "warnings": warnings,
        "profile": profile,
    }


def _wizard_ai_add_recommendation(rows: list[dict], label: str, detail: str, severity: str = "info") -> None:
    if not detail:
        return
    rows.append(
        {
            "label": label,
            "detail": detail,
            "severity": severity if severity in {"info", "warning", "danger", "success"} else "info",
        }
    )


def _wizard_ai_confidence_label(score: int) -> str:
    if score >= 82:
        return "high"
    if score >= 58:
        return "medium"
    return "low"


def _wizard_ai_plan_insights(
    *,
    options: dict,
    ai_info: dict,
    info: dict,
    source_type: dict,
    source_size_bytes: int,
    target_mb: float,
    total_bitrate_kbps: float,
    video_kbps: float,
    audio_kbps: int,
    bpp_final: float,
    q_code: str,
    eta_sec: float,
    history_prediction: dict,
) -> dict:
    if not options.get("ai_mode"):
        return {
            "enabled": False,
            "recommendations": [],
            "safety": {"risk_level": "manual", "flags": []},
            "confidence": {"score": 0, "label": "manual", "reasons": []},
        }

    recommendations: list[dict] = []
    flags: list[dict] = []
    confidence_reasons: list[str] = []
    confidence = 94

    source_size_mb = max(0.0, float(source_size_bytes or 0) / (1024.0 * 1024.0))
    target_mb_f = max(0.0, float(target_mb or 0.0))
    target_ratio = (target_mb_f / source_size_mb) if source_size_mb > 0 else 0.0
    source_kind = source_type.get("kind") or "movie"
    is_hdr = bool(info.get("is_hdr"))
    duration_sec = float(info.get("duration_sec") or 0.0)
    src_h = int(info.get("height") or 0)
    src_w = int(info.get("width") or 0)
    is_4k = src_h >= 1800 or src_w >= 3200

    if duration_sec <= 0 or src_w <= 0 or src_h <= 0:
        confidence -= 35
        confidence_reasons.append("missing probe detail")
    else:
        confidence_reasons.append("source duration and resolution known")

    history_available = bool(history_prediction.get("available"))
    if history_available:
        confidence_reasons.append(f"{history_prediction.get('sample_count', 0)} matching history samples")
        predicted_mb = float(history_prediction.get("predicted_out_mb") or 0.0)
        if predicted_mb > 0 and target_mb_f > 0:
            if predicted_mb > target_mb_f * 1.25:
                _wizard_ai_add_recommendation(
                    recommendations,
                    "History check",
                    "Past jobs suggest this may finish larger than the target; use a smaller-file goal or a lower cap if size matters most.",
                    "warning",
                )
            elif predicted_mb < target_mb_f * 0.70:
                _wizard_ai_add_recommendation(
                    recommendations,
                    "History check",
                    "Past jobs suggest there is room to raise quality while still staying near the target.",
                    "success",
                )
    else:
        confidence -= 10
        confidence_reasons.append("no matching history yet")

    ai_profile = ai_info.get("profile") if isinstance(ai_info.get("profile"), dict) else {}
    explored = ai_profile.get("explored") if isinstance(ai_profile.get("explored"), list) else []
    if explored:
        methods = ", ".join(str(row.get("method") or row.get("codec") or "").strip() for row in explored[:4] if row)
        if methods:
            _wizard_ai_add_recommendation(
                recommendations,
                "Explored options",
                f"AI compared learned paths for {methods} before choosing the current plan.",
                "info",
            )
    history_choice = ai_profile.get("history_choice") if isinstance(ai_profile.get("history_choice"), dict) else {}
    if history_choice:
        _wizard_ai_add_recommendation(
            recommendations,
            "Learned choice",
            f"AI used past {history_choice.get('method', 'encoder')} results because {history_choice.get('reason', 'it matched this plan')}.",
            "success",
        )

    if target_ratio and target_ratio < 0.12:
        confidence -= 18
        flags.append({"label": "Very aggressive target", "detail": "The output target is below 12% of the source size.", "severity": "danger"})
    elif target_ratio and target_ratio < 0.25:
        confidence -= 8
        flags.append({"label": "Aggressive target", "detail": "The output target is less than 25% of the source size.", "severity": "warning"})

    if q_code == "risky":
        flags.append({"label": "Quality risk", "detail": "The video bitrate per pixel is low for this resolution.", "severity": "danger"})
        _wizard_ai_add_recommendation(
            recommendations,
            "Protect quality",
            "Raise the target size or let AI cap the resolution so the encode does not starve the video bitrate.",
            "warning",
        )
    elif q_code == "ok":
        _wizard_ai_add_recommendation(
            recommendations,
            "Streaming fit",
            "This plan should be a reasonable streaming-sized encode, with some quality tradeoff.",
            "info",
        )
    else:
        _wizard_ai_add_recommendation(
            recommendations,
            "Quality fit",
            "The bitrate budget looks healthy for the selected output resolution.",
            "success",
        )

    if is_hdr and (options.get("video_codec") not in {"h265", "av1"} or options.get("bit_depth") != "10"):
        flags.append({"label": "HDR protection", "detail": "HDR sources should use HEVC or AV1 10-bit to avoid bad color/detail choices.", "severity": "danger"})
    elif is_hdr:
        _wizard_ai_add_recommendation(
            recommendations,
            "HDR protected",
            "AI kept HEVC 10-bit because the source looks HDR or 10-bit.",
            "success",
        )

    if is_4k and options.get("resolution_mode") == "keep" and q_code != "good":
        _wizard_ai_add_recommendation(
            recommendations,
            "4K bitrate",
            "Keeping full 4K at this target may be tight; auto or 1440p can look cleaner at the same size.",
            "warning",
        )

    if options.get("audio_mode") == "eac3":
        _wizard_ai_add_recommendation(
            recommendations,
            "Surround retained",
            "Selected audio tracks will use E-AC3 5.1 at 640 kbps for smaller surround-capable tracks.",
            "success",
        )
    elif options.get("audio_mode") == "copy" and options.get("audio_tracks") == "all" and total_bitrate_kbps < 2500:
        flags.append({"label": "Audio budget", "detail": "Copying all audio on a tight target can leave less bitrate for video.", "severity": "warning"})
        _wizard_ai_add_recommendation(
            recommendations,
            "Audio choice",
            "For very small targets, keep selected languages instead of every audio track.",
            "info",
        )
    elif options.get("audio_mode") == "copy":
        _wizard_ai_add_recommendation(
            recommendations,
            "Audio safe",
            "Audio will be copied, so the selected tracks keep original quality.",
            "success",
        )

    if options.get("subtitle_mode") == "none":
        flags.append({"label": "Subtitles removed", "detail": "This plan will not keep subtitle tracks.", "severity": "warning"})
    elif options.get("subtitle_mode") == "all" and not options.get("subtitle_languages"):
        _wizard_ai_add_recommendation(
            recommendations,
            "Subtitle scope",
            "All subtitle languages are allowed; choose languages if you want less clutter.",
            "info",
        )

    if options.get("encoder_family") == "qsv":
        _wizard_ai_add_recommendation(
            recommendations,
            "Fast worker",
            "QSV should keep CPU use lower and finish faster when the worker has Intel media hardware exposed.",
            "info",
        )
    elif options.get("two_pass"):
        _wizard_ai_add_recommendation(
            recommendations,
            "Target accuracy",
            "Two-pass is enabled to help hit a tight size target more accurately.",
            "info",
        )

    if source_kind == "show" and target_mb_f >= 1200 and src_h <= 1080:
        _wizard_ai_add_recommendation(
            recommendations,
            "Episode target",
            "This is generous for a 1080p episode; smaller-file mode may save more space.",
            "info",
        )
    elif source_kind == "movie" and target_mb_f <= 2500 and duration_sec >= 90 * 60:
        _wizard_ai_add_recommendation(
            recommendations,
            "Movie target",
            "This is a tight movie target; quality depends heavily on resolution cap and source grain.",
            "warning",
        )

    warning_count = len([f for f in flags if f.get("severity") == "warning"])
    danger_count = len([f for f in flags if f.get("severity") == "danger"])
    if danger_count:
        risk_level = "high"
    elif warning_count or q_code == "ok":
        risk_level = "medium"
    else:
        risk_level = "low"

    confidence -= danger_count * 16
    confidence -= warning_count * 6
    confidence = int(max(20, min(99, confidence)))

    if not recommendations:
        _wizard_ai_add_recommendation(
            recommendations,
            "Plan ready",
            "The AI plan looks balanced for the selected source and target.",
            "success",
        )

    quality_expectation = {
        "code": q_code,
        "label": {
            "good": "Good",
            "ok": "Streaming",
            "risky": "Risky",
        }.get(q_code, q_code),
        "bpp": round(float(bpp_final or 0.0), 5),
        "summary": {
            "good": "Healthy bitrate for this output resolution.",
            "ok": "Usable streaming quality with visible tradeoffs on hard scenes.",
            "risky": "Likely artifacts unless the source is easy to compress or resolution is capped.",
        }.get(q_code, ""),
    }

    return {
        "enabled": True,
        "summary": ai_info.get("summary") or "",
        "recommendations": recommendations[:6],
        "safety": {
            "risk_level": risk_level,
            "flags": flags[:6],
        },
        "confidence": {
            "score": confidence,
            "label": _wizard_ai_confidence_label(confidence),
            "reasons": confidence_reasons[:4],
        },
        "quality_expectation": quality_expectation,
        "target_analysis": {
            "source_size_mb": round(source_size_mb, 1),
            "target_mb": round(target_mb_f, 1),
            "target_ratio": round(target_ratio, 3),
            "total_bitrate_kbps": round(float(total_bitrate_kbps or 0.0), 1),
            "video_bitrate_kbps": round(float(video_kbps or 0.0), 1),
            "audio_bitrate_kbps": int(audio_kbps or 0),
            "eta_seconds": int(round(float(eta_sec or 0.0))),
        },
    }


def _wizard_normalize_options(data: dict) -> dict:
    data = data or {}
    options = WIZARD_DEFAULT_OPTIONS.copy()
    audio_language_value = data["audio_languages"] if "audio_languages" in data else options["audio_languages"]
    subtitle_language_value = data["subtitle_languages"] if "subtitle_languages" in data else options["subtitle_languages"]

    preset = _choice(data.get("preset"), {"1080", "4k", "auto"}, options["preset"])
    unit = str(data.get("target_size_unit") or options["target_size_unit"]).strip().upper()
    if unit not in {"MB", "GB"}:
        raise ValueError("invalid target_size_unit")

    try:
        size_value = float(data.get("target_size_value") or options["target_size_value"])
    except Exception:
        raise ValueError("invalid target_size_value")
    if size_value <= 0:
        raise ValueError("invalid target_size_value")

    options.update(
        {
            "ai_mode": _truthy(data.get("ai_mode"), options["ai_mode"]),
            "ai_goal": _choice(data.get("ai_goal"), WIZARD_AI_GOALS, options["ai_goal"]),
            "ai_hardware": _choice(data.get("ai_hardware"), WIZARD_AI_HARDWARE, options["ai_hardware"]),
            "ai_codec_preference": _choice(data.get("ai_codec_preference"), WIZARD_AI_CODEC_PREFS, options["ai_codec_preference"]),
            "ai_risk": _choice(data.get("ai_risk"), WIZARD_AI_RISK_LEVELS, options["ai_risk"]),
            "ai_copy_audio": _truthy(data.get("ai_copy_audio"), options["ai_copy_audio"]),
            "ai_audio_scope": _choice(data.get("ai_audio_scope"), WIZARD_AI_TRACK_SCOPES, options["ai_audio_scope"]),
            "ai_subtitle_scope": _choice(data.get("ai_subtitle_scope"), WIZARD_AI_SUBTITLE_SCOPES, options["ai_subtitle_scope"]),
            "smart_audio_strategy": _choice(
                data.get("smart_audio_strategy"),
                WIZARD_SMART_AUDIO_STRATEGIES,
                options["smart_audio_strategy"],
            ),
            "smart_subtitle_strategy": _choice(
                data.get("smart_subtitle_strategy"),
                WIZARD_SUBTITLE_MODES | {""},
                options["smart_subtitle_strategy"],
            ),
            "smart_never_downscale": _truthy(
                data.get("smart_never_downscale"), options["smart_never_downscale"]
            ),
            "smart_keep_black_bars": _truthy(
                data.get("smart_keep_black_bars"), options["smart_keep_black_bars"]
            ),
            "smart_keep_aspect_ratio": _truthy(
                data.get("smart_keep_aspect_ratio"), options["smart_keep_aspect_ratio"]
            ),
            "smart_keep_all_audio_languages": _truthy(
                data.get("smart_keep_all_audio_languages"),
                options["smart_keep_all_audio_languages"],
            ),
            "smart_keep_all_subtitle_languages": _truthy(
                data.get("smart_keep_all_subtitle_languages"),
                options["smart_keep_all_subtitle_languages"],
            ),
            "smart_never_transcode_audio": _truthy(
                data.get("smart_never_transcode_audio"),
                options["smart_never_transcode_audio"],
            ),
            "audio_languages": _wizard_languages(audio_language_value),
            "subtitle_languages": _wizard_languages(subtitle_language_value),
            "preset": preset,
            "target_size_auto": _truthy(data.get("target_size_auto"), options["target_size_auto"]),
            "target_size_value": size_value,
            "target_size_unit": unit,
            "quality": _choice(data.get("quality"), WIZARD_QUALITIES, options["quality"]),
            "video_codec": _choice(data.get("video_codec"), WIZARD_VIDEO_CODECS, options["video_codec"]),
            "encoder_family": _choice(data.get("encoder_family"), WIZARD_ENCODER_FAMILIES, options["encoder_family"]),
            "bit_depth": _choice(data.get("bit_depth"), WIZARD_BIT_DEPTHS, options["bit_depth"]),
            "encoder_speed": _choice(data.get("encoder_speed"), WIZARD_ENCODER_SPEEDS, options["encoder_speed"]),
            "audio_mode": _choice(data.get("audio_mode"), WIZARD_AUDIO_MODES, options["audio_mode"]),
            "audio_tracks": _choice(data.get("audio_tracks"), WIZARD_AUDIO_TRACKS, options["audio_tracks"]),
            "subtitle_mode": _choice(data.get("subtitle_mode"), WIZARD_SUBTITLE_MODES, options["subtitle_mode"]),
            "framerate_mode": _choice(data.get("framerate_mode"), WIZARD_FRAMERATE_MODES, options["framerate_mode"]),
            "framerate": _choice(data.get("framerate"), WIZARD_FRAMERATES, options["framerate"]),
            "smart_lock_source_framerate": _truthy(
                data.get("smart_lock_source_framerate"),
                options["smart_lock_source_framerate"],
            ),
            "deinterlace": _choice(data.get("deinterlace"), WIZARD_DEINTERLACE_MODES, options["deinterlace"]),
            "crop_mode": _choice(data.get("crop_mode"), WIZARD_CROP_MODES, options["crop_mode"]),
            "two_pass": _truthy(data.get("two_pass"), options["two_pass"]),
        }
    )

    audio_bitrate = str(data.get("audio_bitrate") or options["audio_bitrate"]).strip().lower()
    if audio_bitrate != "auto":
        try:
            audio_bitrate_i = int(audio_bitrate)
        except Exception:
            audio_bitrate_i = 192
        audio_bitrate = str(max(64, min(640, audio_bitrate_i)))
    options["audio_bitrate"] = audio_bitrate

    resolution_mode = data.get("resolution_mode")
    if not resolution_mode:
        force_4k = _truthy(data.get("force_4k"), False)
        allow_downscale = _truthy(data.get("allow_downscale"), True)
        resolution_mode = "keep" if force_4k or not allow_downscale else "auto"
    options["resolution_mode"] = _choice(resolution_mode, WIZARD_RESOLUTION_MODES, options["resolution_mode"])

    if options["video_codec"] == "av1":
        options["encoder_family"] = "software"
        if options["bit_depth"] not in {"8", "10"}:
            options["bit_depth"] = "10"

    return options


def _enforce_smart_guardrails(options: dict) -> dict:
    """Re-apply saved Smart protections after AI and one-time tuning choices."""
    out = dict(options or {})
    if out.get("smart_never_downscale"):
        out["resolution_mode"] = "keep"
    if out.get("smart_keep_black_bars"):
        out["crop_mode"] = "none"
    if out.get("smart_never_transcode_audio"):
        out["smart_audio_strategy"] = "copy"
        out["ai_copy_audio"] = True
        out["audio_mode"] = "copy"
        out["audio_bitrate"] = "auto"
    if out.get("smart_keep_all_audio_languages"):
        out["audio_languages"] = []
        out["audio_tracks"] = "all"
        out["ai_audio_scope"] = "all"
    if out.get("smart_keep_all_subtitle_languages"):
        out["subtitle_languages"] = []
        out["subtitle_mode"] = "all"
        out["ai_subtitle_scope"] = "all"
        out["smart_subtitle_strategy"] = "all"
    if out.get("smart_lock_source_framerate"):
        # AI, preset mapping, and node adaptation may never change the frame
        # rate policy selected for a Smart source.
        out["framerate_mode"] = "same"
    return out


def _wizard_resolution_decision(options: dict, src_w: int, src_h: int, fps: float, video_kbps: float):
    mode = options["resolution_mode"]
    out_w, out_h = src_w, src_h
    decision = "keep"
    note = ""

    if mode == "keep":
        return out_w, out_h, decision, note

    if mode in {"720", "1080", "1440", "2160"}:
        target_h = int(mode)
        if src_h > target_h:
            out_w, out_h = _scale_to_height(src_w, src_h, target_h)
            decision = f"cap_{target_h}"
            note = f"Resolution capped at {target_h}p."
        return out_w, out_h, decision, note

    bpp_src = _bpp(video_kbps, src_w, src_h, fps)
    cand_w, cand_h = _scale_to_height(src_w, src_h, 1080)
    bpp_1080 = _bpp(video_kbps, cand_w, cand_h, fps) if (cand_w, cand_h) != (src_w, src_h) else bpp_src

    if src_h > 1080 and bpp_src < 0.045 and bpp_1080 >= bpp_src * 1.6:
        out_w, out_h = cand_w, cand_h
        decision = "auto_downscale"
        note = "Auto downscale selected to protect quality at the requested size."

    return out_w, out_h, decision, note


def _wizard_build_extra_args(options: dict, video_kbps: float, out_w: int, out_h: int, src_w: int, src_h: int, *, source_fps: float = 0.0, preview: bool = False) -> list[str]:
    args = [
        "-b",
        str(int(video_kbps)),
        "--encoder",
        _wizard_encoder_name(options),
        "--encoder-preset",
        _wizard_encoder_preset(options),
    ]

    if options.get("source_is_hdr"):
        # Static HDR10 mastering/light metadata is passed automatically by
        # HandBrake. This additionally requests supported HDR10+/Dolby Vision
        # dynamic metadata instead of inheriting a preset that disables it.
        args += ["--hdr-dynamic-metadata", "all"]

    if options.get("smart_never_downscale"):
        # Explicit dimensions override any lower resolution limit embedded in
        # the selected JSON preset, not just the Smart planner's own decision.
        args += ["--width", str(int(src_w)), "--height", str(int(src_h))]
    elif (out_w, out_h) != (src_w, src_h):
        args += ["--width", str(int(out_w)), "--height", str(int(out_h))]

    if options.get("smart_keep_aspect_ratio"):
        args.append("--keep-display-aspect")

    if options.get("smart_lock_source_framerate"):
        rate = _format_handbrake_fps(source_fps)
        if rate:
            args += ["--rate", rate]
        args.append("--cfr")
    elif options["framerate_mode"] in {"pfr", "cfr"}:
        args += ["-r", options["framerate"], f"--{options['framerate_mode']}"]

    if options["deinterlace"] == "decomb":
        args.append("--decomb")
    elif options["deinterlace"] == "yadif":
        args.append("--deinterlace")

    if options["crop_mode"] == "none":
        args += ["--crop", "0:0:0:0"]

    if not preview:
        audio_langs = options.get("audio_languages") or []
        subtitle_langs = options.get("subtitle_languages") or []

        if audio_langs:
            args += ["--audio-lang-list", ",".join(audio_langs)]

        if options["audio_tracks"] == "all":
            args.append("--all-audio")
        elif audio_langs:
            args.append("--first-audio")

        if options["audio_mode"] == "copy":
            args += ["-E", "copy"]
            if options.get("smart_never_transcode_audio"):
                args += [
                    "--audio-copy-mask",
                    "aac,ac3,eac3,truehd,dts,dtshd,mp2,mp3,opus,vorbis,flac,alac",
                    "--audio-fallback",
                    "none",
                ]
        elif options["audio_mode"] == "eac3":
            args += ["-E", "eac3", "-B", "640", "-6", "5point1"]
        else:
            args += ["-E", "av_aac", "-B", str(_wizard_audio_kbps(options))]

        if subtitle_langs:
            args += ["--subtitle-lang-list", ",".join(subtitle_langs)]

        if options["subtitle_mode"] == "first":
            if subtitle_langs:
                args.append("--first-subtitle")
            else:
                args += ["--subtitle", "1"]
        elif options["subtitle_mode"] == "all":
            args.append("--all-subtitles")

        if options["subtitle_mode"] != "none":
            args.append("--subtitle-burned=none")

        if options["two_pass"] and options["encoder_family"] == "software":
            args.append("--multi-pass" if options.get("video_codec") == "av1" else "--two-pass")

    return args


def _wizard_plan(
    data: dict,
    *,
    probe_func=_probe_media,
    for_queue: bool = False,
    preview: bool = False,
    learned_defaults: dict | None = None,
) -> dict:
    src = (data.get("src") or "").strip()
    if not src or not os.path.isfile(src):
        raise ValueError("invalid src")
    if not is_allowed_path(src):
        raise ValueError("path not allowed")

    base = os.path.basename(src)
    name_only, _ext = os.path.splitext(base)
    if for_queue and name_only.lower().endswith("-tsd"):
        raise ValueError("file already tagged -TSD, not queuing")

    options = _wizard_normalize_options(data)
    manual_audio_override = (
        "audio_mode" in data
        and not _truthy(data.get("ai_mode"), options.get("ai_mode", False))
    )
    effective_preset = options["preset"]
    if effective_preset == "auto":
        effective_preset = guess_preset_from_filename(base)

    info = dict(probe_func(src) or {})
    info["path"] = src
    info["source_name"] = base
    source_size_bytes = int(os.path.getsize(src))
    info["source_size_bytes"] = source_size_bytes
    info["source_size_mb"] = round(source_size_bytes / (1024.0 * 1024.0), 2)
    filename_hdr_reason = _hdr_filename_reason(src)
    info["is_hdr"] = bool(info.get("is_hdr") or filename_hdr_reason)
    if info["is_hdr"] and not info.get("hdr_reason"):
        info["hdr_reason"] = filename_hdr_reason or "filename"
    if not info.get("hdr_format"):
        hdr_text = f"{info.get('hdr_reason') or ''} {filename_hdr_reason}".lower()
        info["hdr_format"] = (
            "dolby_vision"
            if any(token in hdr_text for token in ("dolby", "dovi", "dvhe", "dvh1"))
            else ("hlg" if "hlg" in hdr_text or "arib-std-b67" in hdr_text else ("hdr10" if info["is_hdr"] else "sdr"))
        )
    duration_sec = float(info.get("duration_sec") or 0.0)
    src_w = int(info.get("width") or 0)
    src_h = int(info.get("height") or 0)
    fps = float(info.get("fps") or 0.0) or 24.0
    if duration_sec <= 0 or src_w <= 0 or src_h <= 0:
        raise RuntimeError("probe incomplete")

    source_type = _wizard_detect_source_type(src, duration_sec)
    info["source_type"] = source_type["kind"]
    info["source_type_label"] = source_type["label"]
    info["source_type_reason"] = source_type["reason"]
    if not isinstance(learned_defaults, dict):
        try:
            learned_defaults = smart_learned_plan_defaults(info)
        except Exception:
            learned_defaults = {"sample_count": 0, "confidence": 0.0}
    options, auto_target = _wizard_apply_source_target(
        options,
        source_type,
        info=info,
        source_size_bytes=source_size_bytes,
        effective_preset=effective_preset,
        learned_defaults=learned_defaults,
    )

    settings = load_settings()
    cpu = get_cpu_profile(settings.get("cpu_profile"))
    try:
        cpu_override_f = float(settings.get("cpu_speed_override", 1.0))
    except Exception:
        cpu_override_f = 1.0
    if cpu_override_f <= 0:
        cpu_override_f = 1.0

    pre_ai_target_mb = _size_to_mb(options["target_size_value"], options["target_size_unit"])
    options, ai_info = _wizard_ai_choices(
        options,
        cpu,
        cpu_override_f,
        info,
        source_type,
        source_size_bytes,
        pre_ai_target_mb,
        effective_preset,
        bool(settings.get("qsv_device_available", False)),
    )
    # Smart recommendations may start from a plan the user explicitly queued
    # in Size Wizard. Reapply the exact quality/speed/depth preferences after
    # the general AI heuristic has run, but keep format-safety rules (HDR and
    # H.264 bit-depth constraints) authoritative.
    if options.get("ai_mode") and isinstance(learned_defaults, dict):
        learned_quality = str(learned_defaults.get("quality") or "")
        if learned_quality in WIZARD_QUALITIES:
            options["quality"] = learned_quality
        learned_speed = str(learned_defaults.get("speed") or "")
        if learned_speed in WIZARD_ENCODER_SPEEDS:
            options["encoder_speed"] = learned_speed
        learned_depth = str(learned_defaults.get("bit_depth") or "")
        learned_codec = str(options.get("video_codec") or "")
        if learned_depth in WIZARD_BIT_DEPTHS:
            if info.get("is_hdr"):
                options["bit_depth"] = "10"
            elif learned_codec == "h264":
                options["bit_depth"] = "8"
            else:
                options["bit_depth"] = learned_depth
    hdr_format = str(info.get("hdr_format") or ("hdr10" if info.get("is_hdr") else "sdr")).lower()
    if hdr_format in {"hdr10plus", "dolby_vision"} and options.get("encoder_family") in {
        "qsv",
        "nvenc",
        "vce",
        "vaapi",
    }:
        # These hardware paths retain static HDR10 but are not HandBrake's
        # documented HDR10+/Dolby Vision passthrough encoders. Preserve the
        # episode's dynamic metadata instead of silently flattening it.
        options["encoder_family"] = "software"
        if options.get("video_codec") not in {"h265", "av1"}:
            options["video_codec"] = "h265"
        options["bit_depth"] = "10"
        ai_info.setdefault("warnings", []).append(
            f"{hdr_format.replace('_', ' ').upper()} metadata requires a preservation-capable 10-bit software encoder."
        )
        ai_info.setdefault("decisions", []).append(
            "Kept dynamic HDR metadata by avoiding an unsupported hardware-encoder path."
        )
    options["source_is_hdr"] = bool(info.get("is_hdr"))
    options["source_hdr_format"] = hdr_format
    if manual_audio_override:
        # A saved Smart profile may start with the safe passthrough guardrail,
        # but choosing an audio mode in Size Wizard is an explicit per-job
        # decision. Preserve it across estimate refreshes instead of silently
        # restoring passthrough.
        selected_audio_mode = _choice(
            data.get("audio_mode"), WIZARD_AUDIO_MODES, options.get("audio_mode", "copy")
        )
        options["audio_mode"] = selected_audio_mode
        options["ai_copy_audio"] = selected_audio_mode == "copy"
        options["smart_audio_strategy"] = (
            "copy"
            if selected_audio_mode == "copy"
            else ("eac3_surround" if selected_audio_mode == "eac3" else "")
        )
        if selected_audio_mode != "copy":
            options["smart_never_transcode_audio"] = False
    options = _enforce_smart_guardrails(options)
    # AI and learned preferences can change codec, quality, encoder, or output
    # resolution. Recalculate automatic size from those final choices so every
    # visible Wizard change produces a corresponding per-title estimate.
    options, auto_target = _wizard_apply_source_target(
        options,
        source_type,
        info=info,
        source_size_bytes=source_size_bytes,
        effective_preset=effective_preset,
        learned_defaults=learned_defaults,
    )
    target_mb = _size_to_mb(options["target_size_value"], options["target_size_unit"])
    target_bytes = target_mb * 1024.0 * 1024.0
    total_bitrate_kbps = (target_bytes * 8.0 / duration_sec) / 1000.0
    audio_kbps = _wizard_audio_kbps(options)
    video_kbps = max(250.0, total_bitrate_kbps - audio_kbps)
    out_w, out_h, decision, note = _wizard_resolution_decision(options, src_w, src_h, fps, video_kbps)
    bpp_final = _bpp(video_kbps, out_w, out_h, fps)
    q_code, q_label = _quality_label_from_bpp(bpp_final)

    encoder_preset = _wizard_encoder_preset(options)
    encoder_name = _wizard_encoder_name(options)
    extra_args = _wizard_build_extra_args(
        options,
        video_kbps,
        out_w,
        out_h,
        src_w,
        src_h,
        source_fps=fps,
        preview=preview,
    )

    base_est_fps = _estimate_encode_fps(out_w, out_h, encoder_preset)
    if options["encoder_family"] == "qsv":
        base_est_fps *= 3.0
    if encoder_name.startswith("svt_av1"):
        base_est_fps *= 0.38
    if info.get("is_hdr"):
        base_est_fps *= 0.88
    est_fps = max(1.0, min(base_est_fps * float(cpu.speed_index) * cpu_override_f, 500.0))
    eta_sec = (duration_sec * fps) / est_fps if est_fps > 0 else 0.0
    history_prediction = _history_prediction_for(
        source_size_bytes,
        effective_preset,
        bool(info.get("is_hdr")),
        encode_method=encoder_name,
    )
    ai_insights = _wizard_ai_plan_insights(
        options=options,
        ai_info=ai_info,
        info=info,
        source_type=source_type,
        source_size_bytes=source_size_bytes,
        target_mb=target_mb,
        total_bitrate_kbps=total_bitrate_kbps,
        video_kbps=video_kbps,
        audio_kbps=audio_kbps,
        bpp_final=bpp_final,
        q_code=q_code,
        eta_sec=eta_sec,
        history_prediction=history_prediction,
    )
    ai_warnings = list(ai_info.get("warnings") or [])
    for flag in (ai_insights.get("safety") or {}).get("flags") or []:
        if flag.get("severity") in {"danger", "warning"}:
            detail = str(flag.get("detail") or "").strip()
            if detail and detail not in ai_warnings:
                ai_warnings.append(detail)

    return {
        "src": src,
        "preset": effective_preset,
        "options": options,
        "probe": info,
        "extra_args": extra_args,
        "inputs": {
            "target_mb": target_mb,
            **options,
        },
        "estimates": {
            "total_bitrate_kbps": round(total_bitrate_kbps, 1),
            "audio_bitrate_kbps": audio_kbps,
            "video_bitrate_kbps": round(video_kbps, 1),
            "bpp": round(bpp_final, 5),
            "quality_code": q_code,
            "quality_label": q_label,
            "encoder": encoder_name,
            "encoder_label": _wizard_encoder_label(encoder_name),
            "encoder_preset": encoder_preset,
            "ai_mode": bool(options.get("ai_mode")),
            "ai_summary": ai_info.get("summary") or "",
            "ai_decisions": ai_info.get("decisions") or [],
            "ai_warnings": ai_warnings,
            "ai_profile": ai_info.get("profile") or {},
            "ai_recommendations": ai_insights.get("recommendations") or [],
            "ai_safety": ai_insights.get("safety") or {},
            "ai_confidence": ai_insights.get("confidence") or {},
            "ai_quality_expectation": ai_insights.get("quality_expectation") or {},
            "ai_target_analysis": ai_insights.get("target_analysis") or {},
            "source_type": source_type,
            "target_size_auto": bool(options.get("target_size_auto")),
            "auto_target": auto_target,
            "estimated_output_mb": round(target_mb, 1),
            "estimated_output_bytes": int(round(target_mb * 1024.0 * 1024.0)),
            "eta_seconds": int(round(eta_sec)),
            "eta_human": f"{int(eta_sec//3600)}h {int((eta_sec%3600)//60)}m" if eta_sec >= 3600 else f"{int(eta_sec//60)}m",
            "history_prediction": history_prediction,
            "cpu_profile": cpu.id,
            "cpu_label": cpu.label,
            "cpu_speed_index": float(cpu.speed_index),
            "cpu_speed_override": float(cpu_override_f),
            "est_fps": round(est_fps, 1),
            "decision": decision,
            "decision_note": note,
            "output_resolution": {"width": out_w, "height": out_h},
        },
    }


def _wizard_ai_chat_updates(question: str) -> dict:
    """Translate a narrow conversational request into validated wizard options."""
    text = " ".join(str(question or "").lower().split())
    if re.match(r"^(why|how|what|when|where|will|would|does|do|is|are|can you explain)\b", text):
        return {}
    change_words = ("use ", "switch ", "change ", "set ", "make ", "keep ", "preserve ", "prioritize ", "prefer ")
    if not text or not any(word in text for word in change_words):
        return {}

    updates: dict = {"ai_mode": True}
    if any(phrase in text for phrase in ("keep 4k", "keep source", "source resolution", "preserve 4k", "do not downscale", "don't downscale")):
        updates["resolution_mode"] = "keep"
    elif any(phrase in text for phrase in ("auto resolution", "allow downscale", "choose resolution")):
        updates["resolution_mode"] = "auto"
    else:
        cap = re.search(r"\b(720|1080|1440|2160)p?\b", text)
        if cap and any(word in text for word in ("cap", "resolution", "downscale", "set")):
            updates["resolution_mode"] = cap.group(1)

    if "av1" in text:
        updates["ai_codec_preference"] = "av1"
    elif any(codec in text for codec in ("h.265", "h265", "hevc")):
        updates["ai_codec_preference"] = "h265"
    elif any(codec in text for codec in ("h.264", "h264", "avc")):
        updates["ai_codec_preference"] = "h264"

    if any(phrase in text for phrase in ("qsv", "hardware encode", "intel gpu")):
        updates["ai_hardware"] = "qsv"
    elif any(phrase in text for phrase in ("cpu encode", "software encode", "use cpu")):
        updates["ai_hardware"] = "software"

    if any(phrase in text for phrase in ("faster", "fast encode", "prioritize speed")):
        updates["ai_goal"] = "speed"
    elif any(phrase in text for phrase in ("best quality", "more quality", "prioritize quality", "look better")):
        updates["ai_goal"] = "quality"
    elif any(phrase in text for phrase in ("balanced", "good balance")):
        updates["ai_goal"] = "balanced"
    elif any(phrase in text for phrase in ("archive", "long term")):
        updates["ai_goal"] = "archive"
    elif any(phrase in text for phrase in ("smaller", "save more space", "small file")):
        updates["ai_goal"] = "small"

    target = re.search(r"\b(?:target|size|make it|set it to)?\s*(\d+(?:\.\d+)?)\s*(gb|mb)\b", text)
    if target:
        updates["target_size_auto"] = False
        updates["target_size_value"] = float(target.group(1))
        updates["target_size_unit"] = target.group(2).upper()

    if any(phrase in text for phrase in ("copy audio", "keep audio quality", "preserve audio")):
        updates["ai_copy_audio"] = True
    elif any(phrase in text for phrase in ("compress audio", "convert audio")):
        updates["ai_copy_audio"] = False
    if any(phrase in text for phrase in ("keep all subtitles", "all subtitles")):
        updates["ai_subtitle_scope"] = "all"
    elif any(phrase in text for phrase in ("remove subtitles", "no subtitles")):
        updates["ai_subtitle_scope"] = "none"

    return updates if len(updates) > 1 else {}


def _wizard_ai_sanitize_model_updates(value) -> dict:
    """Keep local-model proposals inside the wizard's supported option surface."""
    value = value if isinstance(value, dict) else {}
    choices = {
        "ai_goal": WIZARD_AI_GOALS,
        "ai_hardware": WIZARD_AI_HARDWARE,
        "ai_codec_preference": WIZARD_AI_CODEC_PREFS,
        "ai_risk": WIZARD_AI_RISK_LEVELS,
        "resolution_mode": WIZARD_RESOLUTION_MODES,
        "target_size_unit": {"MB", "GB"},
        "ai_audio_scope": WIZARD_AI_TRACK_SCOPES,
        "ai_subtitle_scope": WIZARD_AI_SUBTITLE_SCOPES,
    }
    clean: dict = {}
    for key, valid in choices.items():
        raw = str(value.get(key) or "").strip()
        normalized = raw.upper() if key == "target_size_unit" else raw.lower()
        if normalized in valid:
            clean[key] = normalized

    for key in ("target_size_auto", "ai_copy_audio"):
        if key in value and isinstance(value[key], bool):
            clean[key] = value[key]
    if "target_size_value" in value:
        try:
            size = float(value["target_size_value"])
            if 0.05 <= size <= 200000:
                clean["target_size_value"] = size
                clean.setdefault("target_size_auto", False)
        except (TypeError, ValueError):
            pass

    if clean:
        clean["ai_mode"] = True
    return clean


def _wizard_ai_chat_answer(plan: dict, question: str, changed: bool = False) -> dict:
    estimates = plan.get("estimates") or {}
    inputs = plan.get("inputs") or {}
    probe = plan.get("probe") or {}
    profile = estimates.get("ai_profile") or {}
    confidence = estimates.get("ai_confidence") or {}
    quality = estimates.get("ai_quality_expectation") or {}
    history = estimates.get("history_prediction") or {}
    output = estimates.get("output_resolution") or {}
    decisions = [str(row) for row in estimates.get("ai_decisions") or [] if row]
    warnings = [str(row) for row in estimates.get("ai_warnings") or [] if row]
    text = " ".join(str(question or "").lower().split())

    encoder = estimates.get("encoder_label") or estimates.get("encoder") or "the selected encoder"
    resolution = f"{output.get('width', '?')}x{output.get('height', '?')}"
    target_mb = float(inputs.get("target_mb") or 0.0)
    target_text = f"{target_mb / 1024.0:.1f} GB" if target_mb >= 1024 else f"{target_mb:.0f} MB"
    quality_text = quality.get("label") or estimates.get("quality_label") or "Unknown"
    confidence_text = confidence.get("label") or "developing"

    if changed:
        opening = f"I updated the plan and ran it back through the model. It now uses {encoder} at {resolution}, targeting {target_text}."
    elif any(word in text for word in ("resolution", "4k", "1080", "downscale")):
        locked = bool(profile.get("resolution_locked"))
        opening = (
            f"The output is {resolution}. Resolution is a hard user constraint, so AI cannot downscale it."
            if locked else
            f"The output is {resolution}. AI was allowed to choose resolution from the bitrate available per pixel."
        )
    elif any(word in text for word in ("codec", "av1", "h265", "hevc", "h264", "encoder", "qsv")):
        opening = f"I chose {encoder} by weighing target size, CPU/QSV availability, HDR, encode time, and matching job history."
    elif any(word in text for word in ("time", "long", "fast", "speed", "eta")):
        opening = f"The current ETA is {estimates.get('eta_human') or 'not available'} at about {estimates.get('est_fps') or '?'} fps. Encoder type, source resolution, HDR, and your CPU profile drive that estimate."
    elif any(word in text for word in ("quality", "look", "artifact", "block", "good")):
        opening = f"Quality is rated {quality_text} at {estimates.get('bpp') or '?'} bits per pixel per frame. The main pressure is fitting {resolution} into {target_text}."
    elif any(word in text for word in ("size", "space", "storage", "target")):
        ratio = float(profile.get("target_ratio") or 0.0) * 100.0
        opening = f"The target is {target_text}, about {ratio:.0f}% of the source size. The model budgets audio first, then gives the remaining bitrate to video."
    elif any(word in text for word in ("audio", "subtitle", "language")):
        audio = "copied without re-encoding" if inputs.get("audio_mode") == "copy" else f"converted near {estimates.get('audio_bitrate_kbps') or '?'} kbps"
        opening = f"Audio is {audio}; subtitle scope is {inputs.get('subtitle_mode') or 'none'}. Language filters are applied by the normal HandBrake command builder."
    else:
        opening = f"This is a {confidence_text.lower()}-confidence {quality_text.lower()} plan: {encoder}, {resolution}, and a {target_text} target."

    evidence = decisions[-4:]
    if history.get("available"):
        samples = int(history.get("sample_count") or 0)
        evidence.append(f"History estimate uses {samples} matching completed job{'' if samples == 1 else 's'}.")
    if warnings:
        evidence.append("Watch-out: " + warnings[0])

    return {
        "answer": opening + (" " + " ".join(evidence[:4]) if evidence else ""),
        "evidence": evidence[:5],
        "suggestions": [
            "Why did you choose this codec?",
            "Will this keep the source resolution?",
            "Make it faster without removing audio",
            "Prioritize quality and keep 4K",
        ],
    }


def _wizard_public_options(options: dict) -> dict:
    normalized = _wizard_normalize_options(options)
    if isinstance(options, dict) and "target_size_auto" not in options and "target_size_value" in options:
        normalized["target_size_auto"] = False
    return normalized


def _load_wizard_presets() -> list[dict]:
    try:
        with open(WIZARD_PRESETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[WARN] Failed to load wizard_presets.json: {e}", flush=True)
        return []

    rows = data if isinstance(data, list) else []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            options = _wizard_public_options(row.get("options") or {})
        except Exception:
            options = WIZARD_DEFAULT_OPTIONS.copy()
        out.append(
            {
                "id": str(row.get("id") or uuid.uuid4()),
                "name": str(row.get("name") or "Wizard preset"),
                "options": options,
                "created_at": float(row.get("created_at") or time.time()),
                "updated_at": float(row.get("updated_at") or row.get("created_at") or time.time()),
            }
        )
    out.sort(key=lambda r: (r["name"].lower(), r["updated_at"]))
    return out[:WIZARD_PRESET_LIMIT]


def _save_wizard_presets(rows: list[dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(WIZARD_PRESETS_FILE, "w", encoding="utf-8") as f:
        json.dump(rows[:WIZARD_PRESET_LIMIT], f, indent=2)


def _clean_wizard_preset_name(name: str) -> str:
    value = (name or "").strip()
    value = re.sub(r"\s+", " ", value)
    if not value:
        raise ValueError("missing preset name")
    return value[:80]


def _smart_profile_options(data: dict, profile: dict) -> dict:
    """Apply a few user goals to the validated Size Wizard option surface."""
    options = _wizard_public_options(data)
    goal = str(profile.get("goal") or "balanced")
    compatibility = str(profile.get("compatibility") or "modern")
    hardware = str(profile.get("hardware") or "auto")
    codec = {
        "broad": "h264",
        "modern": "h265",
        "maximum": "av1" if goal in {"small", "archive"} else "h265",
    }.get(compatibility, "h265")
    never_transcode_audio = bool(profile.get("never_transcode_audio", True))
    audio_strategy = "copy" if never_transcode_audio else str(profile.get("audio_strategy") or "copy")
    if audio_strategy not in {"copy", "eac3_surround"}:
        audio_strategy = "copy"
    copy_audio = audio_strategy == "copy"
    audio_languages = profile.get("audio_languages") if isinstance(profile.get("audio_languages"), list) else ["eng", "spa"]
    subtitle_languages = profile.get("subtitle_languages") if isinstance(profile.get("subtitle_languages"), list) else ["eng", "spa"]
    keep_all_audio_languages = bool(profile.get("keep_all_audio_languages", True))
    keep_all_subtitle_languages = bool(profile.get("keep_all_subtitle_languages", True))

    options.update(
        {
            "ai_mode": True,
            "ai_goal": goal,
            "ai_hardware": hardware,
            "ai_codec_preference": codec,
            "ai_risk": "safe" if compatibility == "broad" else ("explorer" if goal == "small" else "smart"),
            "ai_copy_audio": copy_audio,
            "ai_audio_scope": "all",
            "ai_subtitle_scope": "all",
            "smart_audio_strategy": audio_strategy,
            "audio_mode": "copy" if copy_audio else "eac3",
            "audio_bitrate": "auto" if copy_audio else "640",
            "audio_tracks": "all",
            "audio_languages": [] if keep_all_audio_languages else audio_languages,
            "subtitle_mode": "all",
            "subtitle_languages": [] if keep_all_subtitle_languages else subtitle_languages,
            "resolution_mode": "keep" if profile.get("never_downscale", True) else options.get("resolution_mode", "auto"),
            "crop_mode": "none" if profile.get("keep_black_bars", True) else options.get("crop_mode", "auto"),
            "smart_never_downscale": bool(profile.get("never_downscale", True)),
            "smart_keep_black_bars": bool(profile.get("keep_black_bars", True)),
            "smart_keep_aspect_ratio": bool(profile.get("keep_aspect_ratio", True)),
            "smart_keep_all_audio_languages": keep_all_audio_languages,
            "smart_keep_all_subtitle_languages": keep_all_subtitle_languages,
            "smart_never_transcode_audio": never_transcode_audio,
            "smart_lock_source_framerate": True,
        }
    )
    return _enforce_smart_guardrails(options)


def _smart_candidate_definitions(profile: dict) -> list[dict]:
    goal = str(profile.get("goal") or "balanced")
    rows = {
        "quality": [
            ("detail", "Detail first", 1.24, "quality", "Protects fine detail with a larger target."),
            ("balanced", "Balanced", 1.0, "balanced", "Balances visible quality, time, and storage."),
            ("compact", "Smaller", 0.82, "small", "Tests a smaller target while guarding resolution."),
        ],
        "small": [
            ("compact", "Space saver", 0.74, "small", "Prioritizes storage savings and efficient compression."),
            ("balanced", "Balanced", 0.92, "balanced", "Keeps extra quality headroom at a modest size."),
            ("detail", "Detail first", 1.10, "quality", "Uses more space when detail is worth keeping."),
        ],
        "speed": [
            ("fast", "Fast", 0.90, "speed", "Prioritizes a short encode time."),
            ("balanced", "Balanced", 1.0, "balanced", "Trades some speed for compression efficiency."),
            ("detail", "Detail first", 1.18, "quality", "Protects detail when speed is less important."),
        ],
        "archive": [
            ("archive", "Archive", 1.08, "archive", "Uses efficient long-term settings with quality headroom."),
            ("detail", "Detail first", 1.25, "quality", "Keeps more source detail for important masters."),
            ("compact", "Smaller archive", 0.84, "small", "Reduces storage with a more aggressive archive target."),
        ],
        "balanced": [
            ("balanced", "Balanced", 1.0, "balanced", "Balances visible quality, time, and storage."),
            ("detail", "Detail first", 1.22, "quality", "Protects detail with a larger target."),
            ("compact", "Smaller", 0.80, "small", "Tests meaningful savings at a tighter target."),
        ],
    }
    return [
        {"id": cid, "name": name, "target_factor": factor, "goal": candidate_goal, "summary": summary}
        for cid, name, factor, candidate_goal, summary in rows.get(goal, rows["balanced"])
    ]


def _smart_objective_score(plan: dict, profile: dict, fastest_eta: float) -> tuple[float, str]:
    estimates = plan.get("estimates") if isinstance(plan.get("estimates"), dict) else {}
    inputs = plan.get("inputs") if isinstance(plan.get("inputs"), dict) else {}
    probe = plan.get("probe") if isinstance(plan.get("probe"), dict) else {}
    options = plan.get("options") if isinstance(plan.get("options"), dict) else {}
    bpp = float(estimates.get("bpp") or 0.0)
    quality = max(0.0, min(1.0, (bpp - 0.025) / 0.065))
    source_mb = max(1.0, float(probe.get("source_size_bytes") or 1) / (1024.0 * 1024.0))
    ratio = max(0.0, min(1.5, float(inputs.get("target_mb") or 0.0) / source_mb))
    savings = max(0.0, min(1.0, 1.0 - ratio))
    eta = max(1.0, float(estimates.get("eta_seconds") or 1.0))
    speed = max(0.0, min(1.0, fastest_eta / eta))
    codec = str(options.get("video_codec") or "")
    compatibility_mode = str(profile.get("compatibility") or "modern")
    compatibility = {
        "broad": {"h264": 1.0, "h265": 0.65, "av1": 0.35},
        "modern": {"h264": 0.9, "h265": 1.0, "av1": 0.72},
        "maximum": {"h264": 0.7, "h265": 0.9, "av1": 1.0},
    }.get(compatibility_mode, {}).get(codec, 0.7)
    goal = str(profile.get("goal") or "balanced")
    weights = {
        "quality": (0.56, 0.16, 0.08, 0.20),
        "small": (0.28, 0.47, 0.10, 0.15),
        "speed": (0.27, 0.18, 0.40, 0.15),
        "archive": (0.47, 0.28, 0.08, 0.17),
        "balanced": (0.42, 0.28, 0.15, 0.15),
    }.get(goal, (0.42, 0.28, 0.15, 0.15))
    score = quality * weights[0] + savings * weights[1] + speed * weights[2] + compatibility * weights[3]
    reason = (
        f"{round(quality * 100)}% quality headroom, {round(savings * 100)}% target savings, "
        f"and {round(speed * 100)}% relative speed fit."
    )
    return score, reason


def _normalize_smart_tuning(value) -> dict:
    """Normalize transient Library overrides without changing the saved Smart profile."""
    raw = value if isinstance(value, dict) else {}
    tuning = {}
    choices = {
        "goal": WIZARD_AI_GOALS,
        "compatibility": {"broad", "modern", "maximum"},
        "hardware": WIZARD_AI_HARDWARE,
        "resolution_mode": WIZARD_RESOLUTION_MODES,
        "audio_strategy": {"copy", "eac3_surround"},
        "subtitle_mode": WIZARD_SUBTITLE_MODES,
    }
    for key, allowed in choices.items():
        candidate = str(raw.get(key) or "").strip().lower()
        if candidate in allowed:
            tuning[key] = candidate
    try:
        target_scale = float(raw.get("target_scale") or 1.0)
    except (TypeError, ValueError):
        target_scale = 1.0
    tuning["target_scale"] = round(max(0.70, min(1.30, target_scale)), 2)
    return tuning


def _smart_episode_quality_floor(plan: dict) -> dict:
    """Calculate a codec-aware minimum target that prevents starved episodes."""
    probe = plan.get("probe") if isinstance(plan.get("probe"), dict) else {}
    options = plan.get("options") if isinstance(plan.get("options"), dict) else {}
    estimates = plan.get("estimates") if isinstance(plan.get("estimates"), dict) else {}
    output = estimates.get("output_resolution") if isinstance(estimates.get("output_resolution"), dict) else {}
    width = max(1, int(output.get("width") or probe.get("width") or 1))
    height = max(1, int(output.get("height") or probe.get("height") or 1))
    fps = max(1.0, float(probe.get("fps") or 24.0))
    duration = max(1.0, float(probe.get("duration_sec") or 1.0))
    is_hdr = bool(probe.get("is_hdr"))
    codec = str(options.get("video_codec") or "h265").lower()
    encoder_family = str(options.get("encoder_family") or "software").lower()

    # Bits per pixel per frame is deliberately conservative here. The floor
    # is a last line of defense for complete-season jobs, not the quality
    # target itself. HDR, hardware HEVC, grain, and high-resolution sources
    # need more headroom than clean SDR software encodes.
    min_bpp = 0.050 if is_hdr else 0.034
    if codec == "h264":
        min_bpp *= 1.35
    elif codec == "av1":
        min_bpp *= 0.82
    if encoder_family in {"qsv", "nvenc", "vce", "vaapi", "videotoolbox"}:
        min_bpp *= 1.06
    if fps >= 50:
        min_bpp *= 1.08
    if width * height >= 3_200 * 1_800:
        min_bpp *= 1.04

    min_video_kbps = width * height * fps * min_bpp / 1000.0
    audio_kbps = max(128.0, float(_wizard_audio_kbps(options) or 0.0))
    target_mb = (min_video_kbps + audio_kbps) * 1000.0 * duration / 8.0 / (1024.0 * 1024.0)
    source_mb = max(1.0, float(probe.get("source_size_bytes") or 0.0) / (1024.0 * 1024.0))
    target_mb = min(target_mb, source_mb * 0.92)
    return {
        "target_mb": round(max(1.0, target_mb), 1),
        "min_bpp": round(min_bpp, 5),
        "video_kbps": round(min_video_kbps, 1),
        "is_hdr": is_hdr,
        "resolution": {"width": width, "height": height},
        "reason": (
            "HDR/color-detail safety floor"
            if is_hdr
            else "per-episode detail safety floor"
        ),
    }


def _smart_episode_snapshot(plan: dict, scene_analysis: dict, quality_floor: dict) -> dict:
    probe = plan.get("probe") if isinstance(plan.get("probe"), dict) else {}
    estimates = plan.get("estimates") if isinstance(plan.get("estimates"), dict) else {}
    try:
        fingerprint = episode_scene_fingerprint(str(plan.get("src") or ""))
    except OSError:
        fingerprint = str(scene_analysis.get("fingerprint") or "")
    return {
        "version": 1,
        "fingerprint": fingerprint,
        "source_name": os.path.basename(str(plan.get("src") or ""))[:240],
        "source": {
            "width": int(probe.get("width") or 0),
            "height": int(probe.get("height") or 0),
            "fps": round(float(probe.get("fps") or 0.0), 3),
            "duration_sec": round(float(probe.get("duration_sec") or 0.0), 2),
            "video_codec": str(probe.get("video_codec") or "")[:30],
            "bit_depth": int(probe.get("bit_depth") or 0),
            "is_hdr": bool(probe.get("is_hdr")),
            "hdr_reason": str(probe.get("hdr_reason") or "")[:120],
            "hdr_format": str(probe.get("hdr_format") or "")[:40],
            "color_transfer": str(probe.get("color_transfer") or "")[:40],
            "color_primaries": str(probe.get("color_primaries") or "")[:40],
            "probe_source": str(probe.get("probe_source") or "")[:40],
        },
        "target": {
            "preset": str(plan.get("preset") or "")[:20],
            "width": int((estimates.get("output_resolution") or {}).get("width") or 0),
            "height": int((estimates.get("output_resolution") or {}).get("height") or 0),
            "target_mb": round(float((plan.get("inputs") or {}).get("target_mb") or 0.0), 1),
            "video_bitrate_kbps": round(float(estimates.get("video_bitrate_kbps") or 0.0), 1),
            "encoder": str(estimates.get("encoder") or "")[:50],
            "fps": _format_handbrake_fps(probe.get("fps")),
            "framerate_mode": "cfr",
        },
        "quality_floor": quality_floor,
        "scene_analysis": scene_analysis,
    }


def _smart_recommendation(data: dict, *, require_automation_ready: bool = False) -> dict:
    """Build and rank three safe plans for one source."""
    data = dict(data or {})
    state = load_smart_preset_state()
    profile_override = data.get("smart_profile")
    if isinstance(profile_override, dict):
        profile = normalize_smart_profile(profile_override, profile_override)
        # A tracked show owns a stable profile snapshot. Normalization validates
        # it, but must not make the saved profile appear to change on every plan.
        profile["created_at"] = float(profile_override.get("created_at") or profile.get("created_at") or time.time())
        profile["updated_at"] = float(profile_override.get("updated_at") or profile.get("updated_at") or time.time())
    else:
        profile = dict(state.get("profile")) if isinstance(state.get("profile"), dict) else {}
    learning = smart_learning_status(state)
    tuning = _normalize_smart_tuning(data.get("smart_tuning"))

    for key in ("goal", "compatibility", "hardware", "audio_strategy"):
        if tuning.get(key):
            profile[key] = tuning[key]

    src = str(data.get("src") or "").strip()
    if not src or not os.path.isfile(src):
        raise ValueError("invalid src")
    if not is_allowed_path(src):
        raise ValueError("path not allowed")
    # One authoritative probe is created for this episode and reused only by
    # its own candidates. A season/show request calls this function again for
    # every path, so no HDR or scene characteristic can leak across episodes.
    episode_probe = dict(_probe_media(src) or {})
    if not episode_probe.get("source_type"):
        episode_probe["source_type"] = _wizard_detect_source_type(
            src,
            episode_probe.get("duration_sec"),
        ).get("kind")
    probe_func = lambda _path: dict(episode_probe)
    learned_defaults = smart_learned_plan_defaults(episode_probe, state)
    if learned_defaults.get("sample_count"):
        if learned_defaults.get("goal") and not tuning.get("goal"):
            profile["goal"] = learned_defaults["goal"]
        if learned_defaults.get("encoder_family") in {"qsv", "software"} and not tuning.get("hardware"):
            profile["hardware"] = learned_defaults["encoder_family"]
        learned_profile_audio = str(learned_defaults.get("audio_strategy") or "")
        if learned_profile_audio == "eac3":
            learned_profile_audio = "eac3_surround"
        if learned_profile_audio in {"copy", "eac3_surround"} and not tuning.get("audio_strategy"):
            profile["audio_strategy"] = learned_profile_audio

    base_options = _smart_profile_options(data, profile)
    if learned_defaults.get("sample_count"):
        learned_codec = str(learned_defaults.get("codec") or "")
        if learned_codec in WIZARD_VIDEO_CODECS:
            base_options["ai_codec_preference"] = learned_codec
            base_options["video_codec"] = learned_codec
        learned_family = str(learned_defaults.get("encoder_family") or "")
        if learned_family in WIZARD_ENCODER_FAMILIES and not tuning.get("hardware"):
            base_options["ai_hardware"] = learned_family
            base_options["encoder_family"] = learned_family
        learned_depth = str(learned_defaults.get("bit_depth") or "")
        if learned_depth in WIZARD_BIT_DEPTHS:
            base_options["bit_depth"] = learned_depth
        learned_quality = str(learned_defaults.get("quality") or "")
        if learned_quality in WIZARD_QUALITIES:
            base_options["quality"] = learned_quality
        learned_speed = str(learned_defaults.get("speed") or "")
        if learned_speed in WIZARD_ENCODER_SPEEDS:
            base_options["encoder_speed"] = learned_speed
        if not profile.get("never_downscale", True) and not tuning.get("resolution_mode"):
            learned_resolution = str(learned_defaults.get("resolution_mode") or "")
            if learned_resolution in WIZARD_RESOLUTION_MODES:
                base_options["resolution_mode"] = learned_resolution
        if not profile.get("never_transcode_audio", True) and not tuning.get("audio_strategy"):
            learned_audio = str(learned_defaults.get("audio_strategy") or "")
            if learned_audio in {"eac3", "eac3_surround"}:
                base_options.update({"smart_audio_strategy": "eac3_surround", "audio_mode": "eac3", "audio_bitrate": "640"})
        if not profile.get("keep_all_subtitle_languages", True) and not tuning.get("subtitle_mode"):
            learned_subtitle_mode = str(learned_defaults.get("subtitle_mode") or "")
            if learned_subtitle_mode in WIZARD_SUBTITLE_MODES:
                base_options["subtitle_mode"] = learned_subtitle_mode
                base_options["ai_subtitle_scope"] = learned_subtitle_mode
                base_options["smart_subtitle_strategy"] = learned_subtitle_mode
        # Keep automatic sizing automatic. The planner blends the learned
        # target ratio with this episode's own runtime, resolution, codec, and
        # quality instead of turning one past choice into a fixed file size.
    if tuning.get("resolution_mode"):
        base_options["resolution_mode"] = tuning["resolution_mode"]
    if tuning.get("subtitle_mode"):
        base_options["subtitle_mode"] = tuning["subtitle_mode"]
        base_options["ai_subtitle_scope"] = tuning["subtitle_mode"]
        base_options["smart_subtitle_strategy"] = tuning["subtitle_mode"]
    base_options = _enforce_smart_guardrails(base_options)
    baseline = _wizard_plan(
        {**data, **base_options},
        probe_func=probe_func,
        preview=False,
        learned_defaults=learned_defaults,
    )
    scene_analysis = analyze_episode_scenes(
        src,
        baseline.get("probe") or episode_probe,
        profile=profile,
        settings=load_settings(),
    )
    scene_scale = float(scene_analysis.get("target_scale") or 1.0)
    quality_floor = _smart_episode_quality_floor(baseline)
    target_mb = max(
        1.0,
        float(baseline.get("inputs", {}).get("target_mb") or 1.0)
        * float(tuning.get("target_scale") or 1.0)
        * scene_scale,
    )
    plans = []
    errors = []
    for definition in _smart_candidate_definitions(profile):
        candidate_options = dict(base_options)
        candidate_options.update(
            {
                "ai_goal": definition["goal"],
                "target_size_auto": False,
                "target_size_value": round(
                    max(
                        target_mb * float(definition["target_factor"]),
                        float(quality_floor.get("target_mb") or 1.0),
                    ),
                    1,
                ),
                "target_size_unit": "MB",
            }
        )
        try:
            plan = _wizard_plan(
                {**data, **candidate_options},
                probe_func=probe_func,
                preview=False,
                learned_defaults=learned_defaults,
            )
            candidate_floor = _smart_episode_quality_floor(plan)
            planned_target = float((plan.get("inputs") or {}).get("target_mb") or 0.0)
            if planned_target + 0.05 < float(candidate_floor.get("target_mb") or 0.0):
                candidate_options["target_size_value"] = float(candidate_floor["target_mb"])
                plan = _wizard_plan(
                    {**data, **candidate_options},
                    probe_func=probe_func,
                    preview=False,
                    learned_defaults=learned_defaults,
                )
                candidate_floor = _smart_episode_quality_floor(plan)
            plan["quality_floor"] = candidate_floor
            plan["estimates"]["smart_quality_floor_mb"] = candidate_floor.get("target_mb")
            plan["estimates"]["episode_scene_target_scale"] = scene_scale
            plans.append((definition, plan))
        except Exception as exc:
            errors.append(f"{definition['name']}: {exc}")
    if not plans:
        raise RuntimeError("no smart preset candidates could be planned")

    fastest_eta = min(max(1.0, float(plan.get("estimates", {}).get("eta_seconds") or 1.0)) for _d, plan in plans)
    candidates = []
    for definition, plan in plans:
        context = smart_feedback_context(plan, definition["id"])
        learned = smart_candidate_learning(context, state)
        objective, reason = _smart_objective_score(plan, profile, fastest_eta)
        evidence = float(learned.get("weighted_evidence") or 0.0)
        learned_weight = min(0.38, evidence / max(1.0, float(profile.get("minimum_feedback") or 3)) * 0.38)
        final_score = objective * (1.0 - learned_weight) + float(learned.get("acceptance") or 0.5) * learned_weight
        estimates = plan.get("estimates") or {}
        options = plan.get("options") or {}
        public_options = _wizard_public_options(options)
        candidates.append(
            {
                "id": definition["id"],
                "name": definition["name"],
                "summary": definition["summary"],
                "score": round(final_score, 3),
                "score_percent": int(round(final_score * 100)),
                "reason": reason,
                "learned": learned,
                "options": public_options,
                "plan": {
                    "preset": plan.get("preset"),
                    "target_mb": round(float(plan.get("inputs", {}).get("target_mb") or 0.0), 1),
                    "video_bitrate_kbps": estimates.get("video_bitrate_kbps"),
                    "quality_label": estimates.get("quality_label"),
                    "encoder": estimates.get("encoder"),
                    "encoder_label": estimates.get("encoder_label"),
                    "eta_seconds": estimates.get("eta_seconds"),
                    "eta_human": estimates.get("eta_human"),
                    "output_resolution": estimates.get("output_resolution"),
                    "is_hdr": bool((plan.get("probe") or {}).get("is_hdr")),
                    "hdr_reason": (plan.get("probe") or {}).get("hdr_reason") or "",
                    "quality_floor_mb": (plan.get("quality_floor") or {}).get("target_mb"),
                    "episode_scene_target_scale": scene_scale,
                },
                "_queue_plan": plan,
            }
        )
    candidates.sort(key=lambda row: float(row.get("score") or 0.0), reverse=True)
    for index, row in enumerate(candidates):
        row["recommended"] = index == 0
    recommended_learning = candidates[0].get("learned") if isinstance(candidates[0].get("learned"), dict) else {}
    context_auto_ready = bool(
        learning.get("automation_ready")
        and float(recommended_learning.get("confidence") or 0.0) >= 0.65
        and float(recommended_learning.get("acceptance") or 0.0) >= float(profile.get("confidence_threshold") or 0.72)
    )
    if require_automation_ready and not context_auto_ready:
        raise ValueError("smart preset automation needs more reviews for this kind of source")
    selected_plan = candidates[0].pop("_queue_plan")
    selected_quality_floor = (
        selected_plan.get("quality_floor")
        if isinstance(selected_plan.get("quality_floor"), dict)
        else quality_floor
    )
    episode_plan = _smart_episode_snapshot(selected_plan, scene_analysis, selected_quality_floor)
    selected_plan["episode_plan"] = episode_plan
    for row in candidates[1:]:
        row.pop("_queue_plan", None)
    return {
        "profile": profile,
        "learning": learning,
        "tuning": tuning,
        "recommended_id": candidates[0]["id"],
        "auto_apply": context_auto_ready,
        "candidates": candidates,
        "errors": errors,
        "episode_plan": episode_plan,
        "scene_analysis": scene_analysis,
        "quality_floor": selected_quality_floor,
        "learned_defaults": learned_defaults,
        "selected_plan": selected_plan,
    }


def _smart_recommendations_for_paths(
    paths: list[str],
    tuning: dict | None = None,
    smart_profile: dict | None = None,
) -> tuple[dict, dict]:
    """Plan a season/show concurrently while retaining one result per path."""
    clean_paths = list(dict.fromkeys(str(path or "").strip() for path in paths if str(path or "").strip()))
    if not clean_paths:
        return {}, {}

    try:
        configured = int(os.environ.get("TSD_SMART_PLAN_WORKERS") or 3)
    except (TypeError, ValueError):
        configured = 3
    worker_count = max(1, min(4, configured, len(clean_paths)))

    def build(path: str) -> dict:
        return _smart_recommendation(
            {
                "src": path,
                "preset": "auto",
                "smart_tuning": tuning or {},
                "smart_profile": smart_profile,
            }
        )

    if worker_count == 1:
        try:
            return {clean_paths[0]: build(clean_paths[0])}, {}
        except Exception as exc:
            return {}, {clean_paths[0]: str(exc)[:240]}

    planned = {}
    errors = {}
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="smart-episode") as executor:
        futures = {executor.submit(build, path): path for path in clean_paths}
        for future in as_completed(futures):
            path = futures[future]
            try:
                planned[path] = future.result()
            except Exception as exc:
                errors[path] = str(exc)[:240]
    return planned, errors


def _create_smart_job(
    src: str,
    *,
    require_automation_ready: bool = False,
    automation_source: str = "smart_preset",
    tuning: dict | None = None,
    recommendation: dict | None = None,
    operations: dict | None = None,
) -> tuple[str, dict]:
    if isinstance(recommendation, dict):
        recommendation = deepcopy(recommendation)
    else:
        recommendation = _smart_recommendation(
            {"src": src, "preset": "auto", "smart_tuning": tuning or {}},
            require_automation_ready=require_automation_ready,
        )
    plan = recommendation.pop("selected_plan")
    recommended_id = recommendation.get("recommended_id")
    feedback_context = smart_feedback_context(plan, str(recommended_id or "balanced"))
    episode_plan = plan.get("episode_plan") if isinstance(plan.get("episode_plan"), dict) else {}
    preset_preferences = dict(plan.get("options") or {})
    preset_preferences["smart_episode_plan"] = episode_plan
    job_id = create_job(
        plan["src"],
        plan["preset"],
        extra_args=" ".join(plan["extra_args"]),
        encode_metadata={
            "encode_method": plan["estimates"].get("encoder"),
            "encoder": plan["estimates"].get("encoder"),
            "video_codec": plan["options"].get("video_codec"),
            "encoder_family": plan["options"].get("encoder_family"),
            "bit_depth": plan["options"].get("bit_depth"),
            "audio_strategy": plan["options"].get("smart_audio_strategy") or plan["options"].get("audio_mode"),
            "audio_languages": plan["options"].get("audio_languages"),
            "subtitle_languages": plan["options"].get("subtitle_languages"),
            "smart_preset": True,
            "smart_profile_id": "default",
            "smart_candidate_id": recommended_id,
            "smart_feedback_context": feedback_context,
            "smart_episode_plan": episode_plan,
            "is_hdr": bool((plan.get("probe") or {}).get("is_hdr")),
            "hdr_reason": str((plan.get("probe") or {}).get("hdr_reason") or ""),
            "automation_source": automation_source,
            "preset_selection": "smart",
            "preset_adaptive": True,
            "preset_preferences": preset_preferences,
        },
        preset_selection="smart",
        preset_adaptive=True,
        preset_preferences=preset_preferences,
        operations=operations,
    )
    scene = episode_plan.get("scene_analysis") if isinstance(episode_plan.get("scene_analysis"), dict) else {}
    log_event(
        "smart_episode_planned",
        (
            f"Built an independent Smart plan for {os.path.basename(src)}: "
            f"{'HDR' if (plan.get('probe') or {}).get('is_hdr') else 'SDR'}, "
            f"{plan.get('estimates', {}).get('encoder') or 'encoder unknown'}, "
            f"{round(float((plan.get('inputs') or {}).get('target_mb') or 0.0), 1)} MB."
        ),
        job_id=job_id,
        src=src,
        extra={
            "episode_fingerprint": str(episode_plan.get("fingerprint") or "")[:16],
            "hdr_reason": str((plan.get("probe") or {}).get("hdr_reason") or ""),
            "quality_floor_mb": (episode_plan.get("quality_floor") or {}).get("target_mb"),
            "scene_ai_used": bool(scene.get("used")),
            "scene_ai_reason": str(scene.get("reason") or "")[:160],
        },
    )
    return job_id, recommendation


def _queue_wizard_job(data: dict) -> dict:
    """Queue one fixed Wizard plan and record it as an approved preference."""
    data = dict(data or {})
    plan = _wizard_plan(data, probe_func=_probe_media, for_queue=True, preview=False)
    learning_context = smart_feedback_context(
        plan,
        str(data.get("smart_candidate_id") or "manual"),
    )
    active_before = {
        str(row.get("id") or "")
        for row in list_jobs_for_api()
        if str(row.get("src") or "") == str(plan["src"])
        and str(row.get("status") or "")
        in {"queued", "dispatching", "running", "waiting_to_upload"}
    }
    requested_mode = str(data.get("mode") or "local").strip().lower()
    if requested_mode in {"best", "auto", "available", "next_available"}:
        dispatch_mode = "auto"
        response_mode = (
            "best"
            if requested_mode == "best"
            else "next_available"
        )
    elif requested_mode == "local":
        dispatch_mode = "local"
        response_mode = "local"
    else:
        raise ValueError("Size Wizard destination must be local or best available")

    preset_preferences = dict(plan.get("options") or {})
    job_id = create_job(
        plan["src"],
        plan["preset"],
        extra_args=" ".join(plan["extra_args"]),
        encode_metadata={
            "encode_method": plan["estimates"].get("encoder"),
            "encoder": plan["estimates"].get("encoder"),
            "video_codec": plan["options"].get("video_codec"),
            "encoder_family": plan["options"].get("encoder_family"),
            "bit_depth": plan["options"].get("bit_depth"),
            "audio_strategy": plan["options"].get("smart_audio_strategy")
            or plan["options"].get("audio_mode"),
            "audio_languages": plan["options"].get("audio_languages"),
            "subtitle_languages": plan["options"].get("subtitle_languages"),
            "smart_preset": True,
            "smart_profile_id": str(data.get("smart_profile_id") or "default"),
            "smart_candidate_id": str(data.get("smart_candidate_id") or "manual"),
            "smart_feedback_context": learning_context,
            "automation_source": "size_wizard",
            "preset_selection": "wizard",
            "preset_adaptive": False,
            "preset_preferences": preset_preferences,
        },
        dispatch_mode=dispatch_mode,
        preset_selection="wizard",
        preset_adaptive=False,
        preset_preferences=preset_preferences,
        operations=data.get("operations") if isinstance(data.get("operations"), dict) else None,
    )
    if dispatch_mode == "auto":
        _wake_auto_node_dispatch()
    learning_result = None
    learning_recorded = bool(job_id and str(job_id) not in active_before)
    if learning_recorded:
        try:
            learning_result = record_smart_preset_feedback(
                learning_context,
                "approve",
                "looks_good",
                origin="wizard_queue",
                job_id=str(job_id),
            )
            log_event(
                "wizard_queue_learning",
                f"Learned the approved Size Wizard plan for {os.path.basename(str(plan['src']))}.",
                level="info",
                job_id=str(job_id),
                src=str(plan["src"]),
                extra={
                    "candidate_id": str(data.get("smart_candidate_id") or "manual"),
                    "encoder": str(plan.get("estimates", {}).get("encoder") or ""),
                    "target_mb": round(float(plan.get("inputs", {}).get("target_mb") or 0.0), 1),
                    "queue_source": str(data.get("queue_source") or "web"),
                    "destination": response_mode,
                },
            )
        except Exception as exc:
            learning_recorded = False
            print(
                f"[WARN] Size Wizard queued job {job_id}, but Smart learning could not be saved: {exc}",
                flush=True,
            )
    return {
        "job_id": job_id,
        "preset": plan["preset"],
        "extra_args": plan["extra_args"],
        "estimates": plan["estimates"],
        "dispatch_mode": response_mode,
        "learning_recorded": learning_recorded,
        "learning": (learning_result or {}).get("learning") or smart_learning_status(),
    }


BETA_LIBRARY_CACHE_FILE = os.path.join(DATA_DIR, "beta_library_cache.json")
BETA_LIBRARY_SUMMARY_FILE = os.path.join(DATA_DIR, "beta_library_summary.json")
BETA_TRACKED_SHOWS_FILE = os.path.join(DATA_DIR, "beta_tracked_shows.json")
BETA_SCAN_INDEX_FILE = os.path.join(DATA_DIR, "beta_scan_index.json")
BETA_AUTOSCAN_STATUS_FILE = os.path.join(DATA_DIR, "beta_autoscan_status.json")
NODE_TRANSFER_TMP_DIR = os.path.join(DATA_DIR, "node_transfer_uploads")
BETA_POSTER_CACHE: dict[tuple, dict] = {}
BETA_LIBRARY_CACHE_LOCK = threading.RLock()
BETA_TRACKING_LOCK = threading.RLock()
BETA_TRACKING_PLAN_ACTIVE: set[str] = set()
BETA_LIBRARY_MEMORY_CACHE = {"signature": None, "data": None}
BETA_LIBRARY_SUMMARY_CACHE = {"signature": None, "data": None}
BETA_AUTOSCAN_THREAD = None
BETA_AUTOSCAN_STOP = threading.Event()
BETA_AUTOSCAN_WAKE = threading.Event()
BETA_AUTOSCAN_LOCK = threading.Lock()
BETA_AUTOSCAN_THREAD_LOCK = threading.Lock()
BETA_AUTOSCAN_HEALTH = {
    "running": False,
    "started_at": 0,
    "last_cycle_at": 0,
    "last_success_at": 0,
    "cycle_errors": 0,
    "last_error": "",
}
BETA_LIBRARY_PARSER_VERSION = 6


def _beta_scan_full_verify_seconds() -> int:
    try:
        hours = int(os.environ.get("TSD_LIBRARY_FULL_VERIFY_HOURS") or 12)
    except (TypeError, ValueError):
        hours = 12
    return max(3600, min(7 * 86400, hours * 3600))


BETA_SCAN_FULL_VERIFY_SECONDS = _beta_scan_full_verify_seconds()
NODE_HEARTBEAT_THREAD = None
NODE_HEARTBEAT_STOP = threading.Event()
NODE_HEARTBEAT_LOCK = threading.Lock()
AUTO_NODE_DISPATCH_THREAD = None
AUTO_NODE_DISPATCH_STOP = threading.Event()
AUTO_NODE_DISPATCH_WAKE = threading.Event()
AUTO_NODE_DISPATCH_LOCK = threading.Lock()
AUTO_NODE_LAST_ASSIGNMENT: dict[str, float] = {}
NODE_HEARTBEAT_HEALTH = {
    "running": False,
    "started_at": 0,
    "last_cycle_at": 0,
    "last_success_at": 0,
    "cycle_errors": 0,
    "last_error": "",
}
BETA_MEDIA_TAG_RE = re.compile(
    r"(?<!\w)(480p|576p|720p|1080p|2160p|4320p|4k|8k|uhd|hdr10\+|hdr10plus|hdr10|hdr|hlg|dv|dovi|dolby ?vision|"
    r"bluray|blu-ray|brrip|webrip|web-dl|webdl|hdtv|remux|proper|repack|"
    r"x264|x265|h264|h265|hevc|av1|aac|ac3|eac3|e-ac3|ddp|ddplus|dd\+|dts|truehd|atmos|"
    r"extended|unrated|directors? ?cut|theatrical)(?!\w)",
    re.IGNORECASE,
)


APP_RELEASE = "3.24.0"
BETA_DIMENSION_TAG_RE = re.compile(r"(?<!\d)(?:\d{3,4}x\d{3,4}|(?:8|10|12)bit)(?!\d)", re.IGNORECASE)
HDR_PATH_RE = re.compile(
    r"(?:^|[ ._\-\[\(])(?:"
    r"hdr(?:10(?:[ ._\-]*(?:plus|\+))?)?|hdr10plus|hdr10\+|hlg|"
    r"dolby[ ._\-]*vision|dovi|dvhe|dvh1|dv|"
    r"bt[ ._\-]?2020|rec[ ._\-]?2020"
    r")(?=$|[ ._\-\]\)\+])",
    re.IGNORECASE,
)


def _beta_file_signature(path: str) -> tuple[int, int] | None:
    try:
        stat = os.stat(path)
        return int(stat.st_mtime_ns), int(stat.st_size)
    except OSError:
        return None


def _beta_write_json(path: str, value) -> None:
    """Atomically persist compact JSON so readers never see partial state."""
    os.makedirs(DATA_DIR, exist_ok=True)
    temp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass


def _beta_invalidate_library_memory_cache() -> None:
    with BETA_LIBRARY_CACHE_LOCK:
        BETA_LIBRARY_MEMORY_CACHE.update({"signature": None, "data": None})
        BETA_LIBRARY_SUMMARY_CACHE.update({"signature": None, "data": None})


def _beta_library_dependency_signature(settings=None) -> tuple:
    settings = settings or {}
    return (
        _beta_file_signature(BETA_LIBRARY_CACHE_FILE),
        _beta_file_signature(BETA_TRACKED_SHOWS_FILE),
        _beta_file_signature(os.path.join(DATA_DIR, "storage_stats.json")),
        bool(_beta_tmdb_config(settings)),
    )


def _beta_library_summary_from_data(data: dict, settings=None) -> dict:
    data = data if isinstance(data, dict) else {}
    catalog = data.get("catalog") if isinstance(data.get("catalog"), dict) else {}
    stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    movies = int(catalog.get("movies") or len(data.get("movies") or []))
    shows = int(catalog.get("shows") or len(data.get("shows") or []))
    episodes = int(catalog.get("episodes") or stats.get("episodes") or 0)
    return {
        "movies": movies,
        "shows": shows,
        "episodes": episodes,
        "updated_at": data.get("generated_at") or data.get("scanned_at") or data.get("updated_at") or 0,
        "configured": bool(_beta_mapped_roots(settings or {})),
    }
HDR_SIZE_HINT_RE = re.compile(r"(?:^|[ ._\-\[\(])(?:2160p|4320p|4k|8k|uhd)(?=$|[ ._\-\]\)])", re.IGNORECASE)
HDR_REMUX_HINT_RE = re.compile(r"(?:^|[ ._\-\[\(])(?:remux|uhd[ ._\-]*blu[ ._\-]*ray|uhd[ ._\-]*bd)(?=$|[ ._\-\]\)])", re.IGNORECASE)
HDR_VIDEO_HINT_RE = re.compile(r"(?:^|[ ._\-\[\(])(?:hevc|x265|h[ ._\-]*265|main[ ._\-]*10|10[ ._\-]*bit)(?=$|[ ._\-\]\)])", re.IGNORECASE)
HDR_TENBIT_HINT_RE = re.compile(r"(?:^|[ ._\-\[\(])(?:main[ ._\-]*10|10[ ._\-]*bit)(?=$|[ ._\-\]\)])", re.IGNORECASE)
HDR_AUDIO_HINT_RE = re.compile(r"(?:^|[ ._\-\[\(])(?:ddp(?:[ ._\-]*[257]\.?1)?|dd\+|e[ ._\-]*ac3|eac3|atmos|truehd)(?=$|[ ._\-\]\)])", re.IGNORECASE)


def _hdr_filename_reason(path: str) -> str:
    text = f"{os.path.basename(path or '')} {path or ''}"
    direct = HDR_PATH_RE.search(text)
    if direct:
        return f"filename: {direct.group(0).strip(' ._-[]()').lower()}"

    has_size = bool(HDR_SIZE_HINT_RE.search(text))
    has_remux = bool(HDR_REMUX_HINT_RE.search(text))
    has_video = bool(HDR_VIDEO_HINT_RE.search(text))
    has_tenbit = bool(HDR_TENBIT_HINT_RE.search(text))
    has_audio = bool(HDR_AUDIO_HINT_RE.search(text))

    if has_size and (has_remux or has_video or has_audio):
        return "filename: 4k release tags"
    if has_remux and has_tenbit:
        return "filename: remux 10-bit tags"
    return ""


def _path_looks_hdr(path: str) -> bool:
    return bool(_hdr_filename_reason(path))


def _hdr_format_from_video_info(stream: dict, path: str = "") -> str:
    stream = stream if isinstance(stream, dict) else {}
    transfer = str(stream.get("color_transfer") or "").lower()
    side_data = stream.get("side_data_list") if isinstance(stream.get("side_data_list"), list) else []
    side_text = json.dumps(side_data).lower() if side_data else ""
    path_text = str(path or "").lower()
    if any(token in side_text for token in ("dovi configuration record", "dolby vision", "dv profile")):
        return "dolby_vision"
    if any(token in side_text for token in ("smpte2094-40", "smpte 2094-40", "hdr10+")) or "hdr10+" in path_text or "hdr10plus" in path_text:
        return "hdr10plus"
    if transfer == "arib-std-b67" or "hlg" in path_text:
        return "hlg"
    if transfer == "smpte2084" or _hdr_filename_reason(path):
        return "hdr10"
    return "sdr"


def _detect_hdr_from_video_info(stream: dict, path: str = "") -> tuple[bool, str]:
    stream = stream if isinstance(stream, dict) else {}
    transfer = str(stream.get("color_transfer") or "").lower()
    primaries = str(stream.get("color_primaries") or "").lower()
    color_space = str(stream.get("color_space") or "").lower()
    pix_fmt = str(stream.get("pix_fmt") or "").lower()
    side_data = stream.get("side_data_list") if isinstance(stream.get("side_data_list"), list) else []
    side_text = json.dumps(side_data).lower() if side_data else ""

    if transfer in {"smpte2084", "arib-std-b67"}:
        return True, transfer
    if any(token in side_text for token in ("dovi configuration record", "dolby vision", "dv profile")):
        return True, "dolby vision metadata"
    if "mastering display metadata" in side_text or "content light level" in side_text:
        return True, "hdr metadata"
    if "bt2020" in primaries or "bt2020" in color_space:
        return True, "bt2020"
    filename_reason = _hdr_filename_reason(path)
    if "p010" in pix_fmt and filename_reason:
        return True, "10-bit hdr filename"
    if filename_reason:
        return True, filename_reason
    return False, ""


def _median(values: list[float]) -> float | None:
    values = sorted(v for v in values if isinstance(v, (int, float)) and math.isfinite(float(v)))
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return float(values[mid])
    return (float(values[mid - 1]) + float(values[mid])) / 2.0


def _history_clean_encode_method(value: str, preset: str = "") -> str:
    method = str(value or "").strip().lower()
    if method:
        return re.sub(r"[^a-z0-9_:+.-]+", "_", method)[:80]
    preset_key = str(preset or "auto").strip().lower() or "auto"
    return f"preset:{preset_key}"


def _history_row_encode_method(row: dict, job: dict, preset: str) -> str:
    for key in ("encode_method", "encoder"):
        value = row.get(key)
        if value:
            return _history_clean_encode_method(str(value), preset)
    for key in ("encode_method", "encoder"):
        value = job.get(key)
        if value:
            return _history_clean_encode_method(str(value), preset)
    return _history_clean_encode_method("", preset)


def _history_prediction_model(node_id: str | None = None) -> dict:
    jobs_by_id = {row.get("id"): row for row in list_jobs_for_api()}
    buckets: dict[tuple, list[dict]] = {}
    node_id = str(node_id or "").strip()

    for row in list_storage_encodes(limit=5000):
        if not isinstance(row, dict):
            continue
        if node_id and str(row.get("node_id") or "") != node_id:
            continue
        try:
            src_bytes = int(row.get("src_bytes") or 0)
            out_bytes = int(row.get("out_bytes") or 0)
        except Exception:
            continue
        if src_bytes <= 0 or out_bytes <= 0:
            continue

        job = jobs_by_id.get(row.get("job_id")) or {}
        preset = str(row.get("preset") or job.get("preset") or "").strip().lower()
        if preset not in {"1080", "4k"}:
            preset = guess_preset_from_filename(os.path.basename(row.get("src") or ""))

        duration_seconds = row.get("duration_seconds")
        if duration_seconds is None:
            duration_seconds = job.get("duration_seconds")
        try:
            duration_seconds = float(duration_seconds or 0.0)
        except Exception:
            duration_seconds = 0.0

        is_hdr = row.get("is_hdr")
        if is_hdr is None:
            is_hdr = job.get("is_hdr")
        filename_hdr = _path_looks_hdr(str(row.get("src") or row.get("out") or ""))
        is_hdr = bool(is_hdr) or filename_hdr
        encode_method = _history_row_encode_method(row, job, preset)

        out_ratio = out_bytes / float(src_bytes)
        if out_ratio <= 0 or out_ratio > 2.0:
            continue

        src_gb = src_bytes / float(1024**3)
        sample = {
            "preset": preset,
            "is_hdr": is_hdr,
            "encode_method": encode_method,
            "out_ratio": out_ratio,
            "saved_ratio": max(0.0, (src_bytes - out_bytes) / float(src_bytes)),
            "seconds_per_gb": (duration_seconds / src_gb) if duration_seconds > 0 and src_gb > 0 else None,
        }

        for key in (
            (preset, is_hdr, encode_method),
            (preset, None, encode_method),
            ("any", is_hdr, encode_method),
            ("any", None, encode_method),
            (preset, is_hdr),
            (preset, None),
            ("any", is_hdr),
            ("any", None),
        ):
            buckets.setdefault(key, []).append(sample)

    return {"buckets": buckets, "node_id": node_id}


def _history_stats_from_samples(samples: list[dict]) -> dict | None:
    if not samples:
        return None
    out_ratio = _median([float(s.get("out_ratio") or 0.0) for s in samples])
    saved_ratio = _median([float(s.get("saved_ratio") or 0.0) for s in samples])
    seconds_per_gb = _median([
        float(s.get("seconds_per_gb") or 0.0)
        for s in samples
        if s.get("seconds_per_gb")
    ])
    if out_ratio is None:
        return None
    return {
        "sample_count": len(samples),
        "runtime_sample_count": len([s for s in samples if s.get("seconds_per_gb")]),
        "out_ratio": max(0.01, min(1.5, float(out_ratio))),
        "saved_ratio": max(0.0, min(1.0, float(saved_ratio or 0.0))),
        "seconds_per_gb": seconds_per_gb,
    }


def _history_prediction_profile(node_id: str | None = None) -> dict:
    model = _history_prediction_model(node_id=node_id)
    buckets = model.get("buckets") or {}
    out = {}
    total_samples = 0
    for preset in ("1080", "4k", "any"):
        for hdr_value, hdr_label in ((True, "hdr"), (False, "sdr"), (None, "any")):
            stats = _history_stats_from_samples(buckets.get((preset, hdr_value)) or [])
            if not stats:
                continue
            key = f"{preset}|{hdr_label}"
            out[key] = {
                "sample_count": int(stats.get("sample_count") or 0),
                "runtime_sample_count": int(stats.get("runtime_sample_count") or 0),
                "out_ratio": round(float(stats.get("out_ratio") or 0.0), 4),
                "saved_ratio": round(float(stats.get("saved_ratio") or 0.0), 4),
                "seconds_per_gb": round(float(stats.get("seconds_per_gb") or 0.0), 3) if stats.get("seconds_per_gb") else None,
            }
            if preset in {"1080", "4k"} and hdr_value is not None:
                total_samples += int(stats.get("sample_count") or 0)
    method_keys = [
        key for key in buckets.keys()
        if isinstance(key, tuple) and len(key) == 3 and key[2]
    ]
    for key in sorted(method_keys, key=lambda item: (str(item[0]), str(item[1]), str(item[2])))[:120]:
        preset, hdr_value, method = key
        stats = _history_stats_from_samples(buckets.get(key) or [])
        if not stats:
            continue
        hdr_label = "hdr" if hdr_value is True else ("sdr" if hdr_value is False else "any")
        out[f"{preset}|{hdr_label}|{method}"] = {
            "sample_count": int(stats.get("sample_count") or 0),
            "runtime_sample_count": int(stats.get("runtime_sample_count") or 0),
            "out_ratio": round(float(stats.get("out_ratio") or 0.0), 4),
            "saved_ratio": round(float(stats.get("saved_ratio") or 0.0), 4),
            "seconds_per_gb": round(float(stats.get("seconds_per_gb") or 0.0), 3) if stats.get("seconds_per_gb") else None,
            "encode_method": str(method),
        }
    return {
        "node_id": str(node_id or ""),
        "sample_count": total_samples,
        "buckets": out,
    }


def _history_prediction_for(
    src_bytes: int,
    preset: str,
    is_hdr: bool,
    model: dict | None = None,
    encode_method: str = "",
    strict_method: bool = False,
) -> dict:
    try:
        src_bytes_i = int(src_bytes or 0)
    except Exception:
        src_bytes_i = 0
    if src_bytes_i <= 0:
        return {"available": False, "sample_count": 0, "reason": "missing source size"}

    preset_key = preset if preset in {"1080", "4k"} else "1080"
    method_key = _history_clean_encode_method(encode_method, preset_key) if encode_method else ""
    model = model or _history_prediction_model()
    buckets = model.get("buckets") or {}
    match = "none"
    stats = None
    candidates = []
    if method_key:
        candidates.extend([
            ((preset_key, bool(is_hdr), method_key), "preset+hdr+method"),
            ((preset_key, None, method_key), "preset+method"),
            (("any", bool(is_hdr), method_key), "hdr+method"),
            (("any", None, method_key), "method"),
        ])
    if not strict_method:
        candidates.extend([
            ((preset_key, bool(is_hdr)), "preset+hdr"),
            ((preset_key, None), "preset"),
            (("any", bool(is_hdr)), "hdr"),
            (("any", None), "all"),
        ])
    for key, label in candidates:
        stats = _history_stats_from_samples(buckets.get(key) or [])
        if stats:
            match = label
            break

    if not stats:
        return {
            "available": False,
            "sample_count": 0,
            "reason": "not enough matching encoder history" if strict_method and method_key else "not enough history",
            "preset": preset_key,
            "is_hdr": bool(is_hdr),
            "encode_method": method_key,
        }

    estimated_out = int(round(src_bytes_i * stats["out_ratio"]))
    estimated_saved = max(0, src_bytes_i - estimated_out)
    src_gb = src_bytes_i / float(1024**3)
    estimated_runtime = None
    if stats.get("seconds_per_gb"):
        estimated_runtime = int(round(src_gb * float(stats["seconds_per_gb"])))

    sample_count = int(stats.get("sample_count") or 0)
    confidence = "low"
    if match == "preset+hdr+method" and sample_count >= 6:
        confidence = "high"
    elif match in {"preset+hdr+method", "preset+method", "preset+hdr", "preset"} and sample_count >= 3:
        confidence = "medium"

    return {
        "available": True,
        "preset": preset_key,
        "is_hdr": bool(is_hdr),
        "encode_method": method_key,
        "match": match,
        "confidence": confidence,
        "sample_count": sample_count,
        "runtime_sample_count": int(stats.get("runtime_sample_count") or 0),
        "estimated_out_bytes": estimated_out,
        "estimated_saved_bytes": estimated_saved,
        "estimated_runtime_seconds": estimated_runtime,
        "out_ratio": round(float(stats["out_ratio"]), 4),
        "saved_ratio": round(float(stats.get("saved_ratio") or 0.0), 4),
    }


def _combine_history_predictions(predictions: list[dict], total_size_bytes: int = 0) -> dict:
    usable = [p for p in predictions if isinstance(p, dict) and p.get("available")]
    if not usable:
        return {"available": False, "sample_count": 0, "reason": "not enough history"}

    estimated_out = sum(int(p.get("estimated_out_bytes") or 0) for p in usable)
    estimated_saved = sum(int(p.get("estimated_saved_bytes") or 0) for p in usable)
    runtime_values = [p.get("estimated_runtime_seconds") for p in usable if p.get("estimated_runtime_seconds") is not None]
    total_runtime = sum(int(v or 0) for v in runtime_values) if runtime_values else None
    sample_count = sum(int(p.get("sample_count") or 0) for p in usable)

    return {
        "available": True,
        "confidence": "medium" if len(usable) >= 3 else "low",
        "sample_count": sample_count,
        "items_predicted": len(usable),
        "items_total": len(predictions),
        "estimated_out_bytes": estimated_out,
        "estimated_saved_bytes": estimated_saved if estimated_saved else max(0, int(total_size_bytes or 0) - estimated_out),
        "estimated_runtime_seconds": total_runtime,
    }


def _beta_mapped_roots(settings=None) -> list[dict]:
    settings = settings or load_settings()
    folders = settings.get("beta_media_folders") or {}
    out = []
    seen = set()

    for kind, prefix in (("movies", "Movies"), ("shows", "Shows")):
        rows = folders.get(kind) if isinstance(folders, dict) else []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            path = str(row.get("path") or "").strip()
            if not path:
                continue
            key = os.path.normcase(os.path.realpath(path))
            if key in seen:
                continue
            seen.add(key)
            label = str(row.get("label") or "").strip() or os.path.basename(path.rstrip("/\\")) or prefix
            out.append({"path": path, "label": f"{prefix}: {label}", "kind": kind})

    return out


def _beta_roots_payload(settings=None) -> list[dict]:
    return _beta_mapped_roots(settings)


def _beta_root_is_mapped(root_path: str, settings=None) -> bool:
    target = os.path.normcase(os.path.realpath(root_path or ""))
    return any(os.path.normcase(os.path.realpath(row["path"])) == target for row in _beta_mapped_roots(settings))


def _beta_empty_library(settings=None) -> dict:
    settings = settings or {}
    return {
        "scope": "empty",
        "root": "",
        "roots": [],
        "recursive": True,
        "generated_at": 0,
        "stats": {
            "movies": 0,
            "shows": 0,
            "episodes": 0,
            "scanned": 0,
            "skipped_tsd": 0,
            "limited": False,
        },
        "tmdb_configured": bool(_beta_tmdb_config(settings)),
        "metadata": {"keyless": bool(settings.get("metadata_no_key_enabled", True)), "providers": []},
        "release_calendar": {"generated_at": 0, "episodes": []},
        "catalog": {"total_titles": 0, "movies": 0, "shows": 0, "episodes": 0},
        "movies": [],
        "shows": [],
    }


def _beta_empty_tracking() -> dict:
    return {"shows": {}, "updated_at": 0}


def _beta_show_tracking_key(show: dict) -> str:
    show = show if isinstance(show, dict) else {}
    existing = str(show.get("id") or show.get("show_id") or "").strip()
    if existing:
        return existing
    key = f"{str(show.get('title') or 'Unknown Title').strip().lower()}::{show.get('year') or ''}"
    return uuid.uuid5(uuid.NAMESPACE_DNS, key).hex


def _beta_show_paths(show: dict) -> list[str]:
    show = show if isinstance(show, dict) else {}
    paths = []
    for ep in show.get("files") or []:
        if isinstance(ep, dict) and ep.get("path"):
            paths.append(str(ep.get("path")))
    if not paths and show.get("path"):
        paths.append(str(show.get("path")))
    return sorted(set(paths))


def _beta_clean_path_list(paths) -> list[str]:
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list):
        return []
    cleaned = []
    seen = set()
    for raw in paths:
        path = str(raw or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        cleaned.append(path)
    return cleaned


def _beta_clean_episode_plans(value) -> dict:
    """Keep only valid, queueable episode-plan snapshots from tracked-show state."""
    rows = value if isinstance(value, dict) else {}
    cleaned = {}
    for key, raw in rows.items():
        if not isinstance(raw, dict):
            continue
        path = str(raw.get("path") or key or "").strip()
        queue_plan = raw.get("queue_plan") if isinstance(raw.get("queue_plan"), dict) else None
        if not path or queue_plan is None:
            continue
        cleaned[path] = {
            "version": max(1, int(raw.get("version") or 1)),
            "path": path,
            "fingerprint": str(raw.get("fingerprint") or "")[:128],
            "profile_revision": str(raw.get("profile_revision") or "")[:128],
            "planned_at": float(raw.get("planned_at") or 0),
            "recommended_id": str(raw.get("recommended_id") or "balanced")[:80],
            "preset": str(raw.get("preset") or queue_plan.get("preset") or "")[:160],
            "encoder": str(raw.get("encoder") or (queue_plan.get("estimates") or {}).get("encoder") or "")[:160],
            "is_hdr": bool(raw.get("is_hdr", (queue_plan.get("probe") or {}).get("is_hdr"))),
            "episode_plan": deepcopy(raw.get("episode_plan")) if isinstance(raw.get("episode_plan"), dict) else {},
            "queue_plan": deepcopy(queue_plan),
        }
    return cleaned


def _beta_smart_profile_revision(profile: dict) -> str:
    meaningful = {
        key: value
        for key, value in (profile if isinstance(profile, dict) else {}).items()
        if key not in {"created_at", "updated_at"}
    }
    encoded = json.dumps(meaningful, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _beta_show_smart_profile(row: dict, *, refresh: bool = False) -> tuple[dict, str]:
    row = row if isinstance(row, dict) else {}
    saved = row.get("smart_profile") if isinstance(row.get("smart_profile"), dict) else None
    if refresh or saved is None:
        state = load_smart_preset_state()
        source = state.get("profile") if isinstance(state.get("profile"), dict) else {}
    else:
        source = saved
    profile = normalize_smart_profile(source, source)
    profile["created_at"] = float(source.get("created_at") or profile.get("created_at") or time.time())
    profile["updated_at"] = float(source.get("updated_at") or profile.get("updated_at") or time.time())
    return profile, _beta_smart_profile_revision(profile)


def _beta_episode_plan_record(path: str, recommendation: dict, profile_revision: str) -> dict:
    recommendation = recommendation if isinstance(recommendation, dict) else {}
    plan = recommendation.get("selected_plan") if isinstance(recommendation.get("selected_plan"), dict) else {}
    if not plan:
        raise ValueError("smart recommendation has no queue plan")
    episode_plan = plan.get("episode_plan") if isinstance(plan.get("episode_plan"), dict) else {}
    fingerprint = str(episode_plan.get("fingerprint") or "")
    if not fingerprint:
        fingerprint = episode_scene_fingerprint(path)
    return {
        "version": 1,
        "path": path,
        "fingerprint": fingerprint,
        "profile_revision": profile_revision,
        "planned_at": time.time(),
        "recommended_id": str(recommendation.get("recommended_id") or "balanced")[:80],
        "preset": str(plan.get("preset") or "")[:160],
        "encoder": str((plan.get("estimates") or {}).get("encoder") or "")[:160],
        "is_hdr": bool((plan.get("probe") or {}).get("is_hdr")),
        "episode_plan": deepcopy(episode_plan),
        "queue_plan": deepcopy(plan),
    }


def _beta_saved_episode_recommendation(record: dict, path: str, profile_revision: str) -> dict | None:
    if not isinstance(record, dict) or str(record.get("profile_revision") or "") != profile_revision:
        return None
    try:
        current_fingerprint = episode_scene_fingerprint(path)
    except OSError:
        return None
    if str(record.get("fingerprint") or "") != current_fingerprint:
        return None
    plan = record.get("queue_plan") if isinstance(record.get("queue_plan"), dict) else None
    if not plan:
        return None
    plan = deepcopy(plan)
    plan["src"] = path
    return {
        "recommended_id": str(record.get("recommended_id") or "balanced"),
        "episode_plan": deepcopy(record.get("episode_plan") or {}),
        "selected_plan": plan,
    }


def _beta_show_plan_needs_refresh(row: dict, paths: list[str]) -> bool:
    if not isinstance(row, dict) or not row.get("smart_profile"):
        return True
    records = _beta_clean_episode_plans(row.get("episode_plans"))
    profile_revision = str(row.get("smart_profile_revision") or "")
    if not profile_revision:
        return True
    for path in _beta_clean_path_list(paths):
        if (
            os.path.isfile(path)
            and is_allowed_path(path)
            and path.lower().endswith(VIDEO_EXTS)
            and not os.path.splitext(os.path.basename(path))[0].lower().endswith("-tsd")
            and _beta_saved_episode_recommendation(records.get(path), path, profile_revision) is None
        ):
            return True
    return False


def _beta_plan_episode_records(row: dict, paths: list[str], *, force: bool = False) -> tuple[dict, dict]:
    """Build independent episode plans under one durable show-level policy."""
    clean_paths = _beta_clean_path_list(paths)
    profile, profile_revision = _beta_show_smart_profile(row)
    records = _beta_clean_episode_plans(row.get("episode_plans"))
    to_plan = []
    errors = {}
    for path in clean_paths:
        reason = ""
        if not os.path.isfile(path):
            reason = "not a file"
        elif not is_allowed_path(path):
            reason = "path not allowed"
        elif not path.lower().endswith(VIDEO_EXTS):
            reason = "not a video"
        elif os.path.splitext(os.path.basename(path))[0].lower().endswith("-tsd"):
            reason = "already tagged -TSD"
        if reason:
            errors[path] = reason
            continue
        if force or _beta_saved_episode_recommendation(records.get(path), path, profile_revision) is None:
            to_plan.append(path)

    if to_plan:
        planned, planning_errors = _smart_recommendations_for_paths(
            to_plan,
            smart_profile=profile,
        )
        errors.update(planning_errors)
        for path, recommendation in planned.items():
            try:
                records[path] = _beta_episode_plan_record(path, recommendation, profile_revision)
            except Exception as exc:
                errors[path] = str(exc)[:240]

    current_records = {path: records[path] for path in clean_paths if path in records}
    tracked_paths = sorted(set(_beta_clean_path_list(row.get("tracked_paths"))) | set(clean_paths))
    tracked_record_count = sum(1 for path in tracked_paths if path in records)
    row["smart_profile"] = profile
    row["smart_profile_revision"] = profile_revision
    row["smart_profile_saved_at"] = float(row.get("smart_profile_saved_at") or time.time())
    row["episode_plans"] = records
    row["tracked_paths"] = tracked_paths
    row["smart_episode_count"] = len(tracked_paths)
    row["smart_planned_episode_count"] = tracked_record_count
    row["smart_plan_errors"] = {path: str(reason)[:240] for path, reason in errors.items()}
    row["smart_plan_error"] = next(iter(row["smart_plan_errors"].values()), "")
    if errors:
        row["smart_plan_status"] = "partial" if tracked_record_count else "error"
    else:
        row["smart_plan_status"] = "ready" if tracked_record_count >= len(tracked_paths) else "partial"
    row["smart_plan_updated_at"] = time.time()
    return current_records, errors


def _beta_plan_tracked_show_episodes(show_id: str, paths: list[str], *, force: bool = False) -> tuple[dict, dict]:
    with BETA_TRACKING_LOCK:
        tracking = _beta_load_tracking()
        shows = tracking.get("shows") if isinstance(tracking.get("shows"), dict) else {}
        row = shows.get(str(show_id or ""))
        if not isinstance(row, dict) or not row.get("tracked"):
            return {}, {"show": "show is no longer tracked"}
        records, errors = _beta_plan_episode_records(row, paths, force=force)
        row["updated_at"] = time.time()
        shows[str(show_id)] = row
        tracking["shows"] = shows
        _beta_save_tracking(tracking)
        log_event(
            "tracked_show_smart_plans",
            (
                f"Saved {len(records)} tailored episode Smart plan(s) for "
                f"{row.get('title') or show_id} under {str((row.get('smart_profile') or {}).get('name') or 'My smart preset')}."
            ),
            level="warn" if errors else "info",
            extra={"show_id": str(show_id), "planned": len(records), "errors": len(errors)},
        )
        return records, errors


def _beta_schedule_tracked_show_planning(show_id: str, paths: list[str], *, force: bool = False) -> bool:
    show_id = str(show_id or "").strip()
    clean_paths = _beta_clean_path_list(paths)
    with BETA_TRACKING_LOCK:
        if not show_id or show_id in BETA_TRACKING_PLAN_ACTIVE:
            return False
        tracking = _beta_load_tracking()
        shows = tracking.get("shows") if isinstance(tracking.get("shows"), dict) else {}
        row = shows.get(show_id)
        if not isinstance(row, dict) or not row.get("tracked"):
            return False
        row["smart_plan_status"] = "planning"
        row["smart_episode_count"] = len(clean_paths)
        row["updated_at"] = time.time()
        shows[show_id] = row
        tracking["shows"] = shows
        BETA_TRACKING_PLAN_ACTIVE.add(show_id)
        _beta_save_tracking(tracking)

    def run() -> None:
        try:
            _beta_plan_tracked_show_episodes(show_id, clean_paths, force=force)
        except Exception as exc:
            with BETA_TRACKING_LOCK:
                failed = _beta_load_tracking()
                failed_shows = failed.get("shows") if isinstance(failed.get("shows"), dict) else {}
                failed_row = failed_shows.get(show_id)
                if isinstance(failed_row, dict):
                    failed_row["smart_plan_status"] = "error"
                    failed_row["smart_plan_error"] = str(exc)[:240]
                    failed_row["updated_at"] = time.time()
                    failed_shows[show_id] = failed_row
                    failed["shows"] = failed_shows
                    _beta_save_tracking(failed)
        finally:
            with BETA_TRACKING_LOCK:
                BETA_TRACKING_PLAN_ACTIVE.discard(show_id)

    threading.Thread(target=run, name=f"tracked-show-{show_id[:12]}", daemon=True).start()
    return True


def _beta_load_tracking() -> dict:
    try:
        with open(BETA_TRACKED_SHOWS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return _beta_empty_tracking()
    except Exception as e:
        print(f"[WARN] Failed to load beta tracked shows: {e}", flush=True)
        return _beta_empty_tracking()

    if not isinstance(data, dict):
        return _beta_empty_tracking()

    rows = data.get("shows") if isinstance(data.get("shows"), dict) else {}
    cleaned = {}
    for key, row in rows.items():
        if not isinstance(row, dict):
            continue
        row_id = str(row.get("id") or key or "").strip()
        if not row_id:
            continue
        known_paths = _beta_clean_path_list(row.get("known_paths"))
        tracked_paths = _beta_clean_path_list(row.get("tracked_paths"))
        smart_profile = row.get("smart_profile") if isinstance(row.get("smart_profile"), dict) else None
        episode_plans = _beta_clean_episode_plans(row.get("episode_plans"))
        cleaned[row_id] = {
            "id": row_id,
            "title": str(row.get("title") or "Unknown Title"),
            "year": row.get("year"),
            "tmdb_id": row.get("tmdb_id"),
            "tvmaze_id": row.get("tvmaze_id"),
            "poster_url": str(row.get("poster_url") or ""),
            "tracked": bool(row.get("tracked", True)),
            "monitor_releases": bool(row.get("monitor_releases", True)),
            "auto_queue": bool(row.get("auto_queue", True)),
            "known_paths": known_paths,
            "tracked_paths": tracked_paths,
            "smart_profile": deepcopy(smart_profile) if smart_profile else None,
            "smart_profile_revision": str(row.get("smart_profile_revision") or "")[:128],
            "smart_profile_saved_at": float(row.get("smart_profile_saved_at") or 0),
            "episode_plans": episode_plans,
            "smart_episode_count": max(0, int(row.get("smart_episode_count") or 0)),
            "smart_planned_episode_count": max(0, int(row.get("smart_planned_episode_count") or len(episode_plans))),
            "smart_plan_status": str(row.get("smart_plan_status") or ("ready" if episode_plans else "pending"))[:24],
            "smart_plan_error": str(row.get("smart_plan_error") or "")[:240],
            "smart_plan_errors": deepcopy(row.get("smart_plan_errors")) if isinstance(row.get("smart_plan_errors"), dict) else {},
            "smart_plan_updated_at": float(row.get("smart_plan_updated_at") or 0),
            "created_at": float(row.get("created_at") or 0),
            "updated_at": float(row.get("updated_at") or 0),
        }

    return {
        "shows": cleaned,
        "updated_at": float(data.get("updated_at") or 0),
    }


def _beta_save_tracking(data: dict) -> None:
    data = data if isinstance(data, dict) else _beta_empty_tracking()
    data["updated_at"] = time.time()
    _beta_write_json(BETA_TRACKED_SHOWS_FILE, data)
    _beta_invalidate_library_memory_cache()


def _beta_apply_tracking(data: dict, tracking: dict | None = None) -> dict:
    data = data if isinstance(data, dict) else {}
    tracking = tracking if isinstance(tracking, dict) else _beta_load_tracking()
    tracked_rows = tracking.get("shows") if isinstance(tracking.get("shows"), dict) else {}
    tracked_count = 0
    pending_count = 0

    for show in data.get("shows") or []:
        if not isinstance(show, dict):
            continue
        show_id = _beta_show_tracking_key(show)
        row = tracked_rows.get(show_id) or {}
        paths = set(_beta_show_paths(show))
        known = set(_beta_clean_path_list(row.get("known_paths")))
        is_tracked = bool(row.get("tracked")) if row else False
        new_paths = sorted(path for path in paths if path not in known)

        show["tracked"] = is_tracked
        show["monitor_releases"] = bool(row.get("monitor_releases", True)) if row else False
        show["auto_queue_downloads"] = bool(row.get("auto_queue", True)) if row else False
        show["tracking"] = {
            "tracked": is_tracked,
            "known_episode_count": len(paths & known),
            "new_episode_count": len(new_paths) if is_tracked else 0,
            "new_paths": new_paths if is_tracked else [],
            "updated_at": row.get("updated_at") if row else 0,
            "monitor_releases": bool(row.get("monitor_releases", True)) if row else False,
            "auto_queue": bool(row.get("auto_queue", True)) if row else False,
            "smart_profile_name": str((row.get("smart_profile") or {}).get("name") or "") if row else "",
            "smart_profile_revision": str(row.get("smart_profile_revision") or "")[:12] if row else "",
            "smart_plan_status": str(row.get("smart_plan_status") or "pending") if row else "",
            "smart_episode_count": int(row.get("smart_episode_count") or len(paths)) if row else 0,
            "smart_planned_episode_count": int(row.get("smart_planned_episode_count") or 0) if row else 0,
            "smart_plan_error": str(row.get("smart_plan_error") or "") if row else "",
            "smart_plan_updated_at": float(row.get("smart_plan_updated_at") or 0) if row else 0,
        }
        if is_tracked:
            tracked_count += 1
            pending_count += len(new_paths)

    data["tracking"] = {
        "tracked_count": tracked_count,
        "new_episode_count": pending_count,
        "updated_at": float(tracking.get("updated_at") or 0),
    }
    calendar = data.get("release_calendar") if isinstance(data.get("release_calendar"), dict) else {}
    for episode in calendar.get("episodes") or []:
        if not isinstance(episode, dict):
            continue
        row = tracked_rows.get(str(episode.get("library_show_id") or "")) or {}
        episode["tracked"] = bool(row.get("tracked"))
        episode["monitor_releases"] = bool(row.get("monitor_releases", True)) if row else False
    return data


def _beta_queue_tracked_episode_paths(row: dict, paths: list[str], *, automation_source: str) -> dict:
    """Queue tracked episodes from their saved per-episode Smart plans."""
    result = {"queued_count": 0, "planned_count": 0, "known_paths": [], "queued_paths": [], "skipped": []}
    clean_paths = _beta_clean_path_list(paths)
    records, planning_errors = _beta_plan_episode_records(row, clean_paths)
    result["planned_count"] = len(records)
    profile_revision = str(row.get("smart_profile_revision") or "")
    active_paths = _beta_active_job_paths()
    known = set(_beta_clean_path_list(row.get("known_paths")))

    for path in clean_paths:
        if path in active_paths:
            known.add(path)
            result["known_paths"].append(path)
            result["skipped"].append({"path": path, "reason": "already queued"})
            continue
        if path in planning_errors:
            reason = str(planning_errors[path])[:240]
            # A completed -TSD source should not be retried on every scan. All
            # real planning/IO failures remain unknown so the next scan retries.
            if reason == "already tagged -TSD":
                known.add(path)
                result["known_paths"].append(path)
            result["skipped"].append({"path": path, "reason": reason})
            continue
        recommendation = _beta_saved_episode_recommendation(records.get(path), path, profile_revision)
        if recommendation is None:
            result["skipped"].append({"path": path, "reason": "saved Smart episode plan is stale"})
            continue
        try:
            job_id, _recommendation = _create_smart_job(
                path,
                automation_source=automation_source,
                recommendation=recommendation,
            )
            record = row.get("episode_plans", {}).get(path)
            if isinstance(record, dict):
                record["last_queued_at"] = time.time()
                record["last_job_id"] = str(job_id or "")
            known.add(path)
            result["known_paths"].append(path)
            result["queued_paths"].append(path)
            result["queued_count"] += 1
        except Exception as exc:
            result["skipped"].append({"path": path, "reason": f"Smart queue failed: {str(exc)[:180]}"})

    row["known_paths"] = sorted(known)
    row["updated_at"] = time.time()
    return result


def _beta_auto_queue_tracked_episodes(data: dict, tracking: dict) -> dict:
    result = {
        "detected_count": 0,
        "queued_count": 0,
        "skipped": [],
        "_planning_requests": [],
    }
    tracking = tracking if isinstance(tracking, dict) else _beta_empty_tracking()
    tracked_rows = tracking.get("shows") if isinstance(tracking.get("shows"), dict) else {}
    changed = False

    for show in data.get("shows") or []:
        if not isinstance(show, dict):
            continue
        show_id = _beta_show_tracking_key(show)
        row = tracked_rows.get(show_id)
        if not isinstance(row, dict) or not row.get("tracked") or not row.get("auto_queue", True):
            continue

        current_paths = _beta_show_paths(show)
        row["tracked_paths"] = current_paths
        if _beta_show_plan_needs_refresh(row, current_paths):
            result["_planning_requests"].append((show_id, current_paths))
        known = set(_beta_clean_path_list(row.get("known_paths")))
        new_paths = [path for path in current_paths if path not in known]
        if not new_paths:
            row["known_paths"] = sorted(set(current_paths) | known)
            continue

        result["detected_count"] += len(new_paths)
        queued = _beta_queue_tracked_episode_paths(
            row,
            new_paths,
            automation_source="tracked_show_library_scan",
        )
        result["queued_count"] += int(queued.get("queued_count") or 0)
        result["skipped"].extend(queued.get("skipped") or [])
        row["title"] = show.get("title") or row.get("title") or "Unknown Title"
        row["year"] = show.get("year")
        row["tmdb_id"] = show.get("tmdb_id") or row.get("tmdb_id")
        row["tvmaze_id"] = show.get("tvmaze_id") or row.get("tvmaze_id")
        row["poster_url"] = show.get("poster_url") or row.get("poster_url") or ""
        row["updated_at"] = time.time()
        changed = True

    if changed:
        tracking["shows"] = tracked_rows
    return result


def _beta_load_library_cache(settings=None) -> dict:
    settings = settings or {}
    with BETA_LIBRARY_CACHE_LOCK:
        signature = _beta_library_dependency_signature(settings)
        if BETA_LIBRARY_MEMORY_CACHE.get("data") is not None and BETA_LIBRARY_MEMORY_CACHE.get("signature") == signature:
            return BETA_LIBRARY_MEMORY_CACHE["data"]

        try:
            with open(BETA_LIBRARY_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = _beta_empty_library(settings)
        except Exception as e:
            print(f"[WARN] Failed to load beta library cache: {e}", flush=True)
            data = _beta_empty_library(settings)

        if not isinstance(data, dict):
            data = _beta_empty_library(settings)

        data.setdefault("movies", [])
        data.setdefault("shows", [])
        data.setdefault("stats", {})
        if _beta_sanitize_duplicate_artwork(data):
            # Repair older caches in place. Otherwise an incremental scan can copy
            # the poisoned poster assignments back into every newly scanned row.
            _beta_save_library_cache(data)
        data["tmdb_configured"] = bool(_beta_tmdb_config(settings))
        data = _beta_apply_tracking(_beta_refresh_predictions(_beta_finalize_catalog(data)), _beta_load_tracking())
        BETA_LIBRARY_MEMORY_CACHE.update({
            "signature": _beta_library_dependency_signature(settings),
            "data": data,
        })
        return data


def _beta_load_library_summary(settings=None) -> dict:
    """Load title counts from a tiny sidecar instead of the full media catalog."""
    settings = settings or {}
    source_signature = _beta_file_signature(BETA_LIBRARY_CACHE_FILE)
    configured_roots = tuple(
        (str(row.get("kind") or ""), str(row.get("path") or ""))
        for row in _beta_mapped_roots(settings)
    )
    cache_signature = (source_signature, configured_roots)
    with BETA_LIBRARY_CACHE_LOCK:
        if BETA_LIBRARY_SUMMARY_CACHE.get("data") is not None and BETA_LIBRARY_SUMMARY_CACHE.get("signature") == cache_signature:
            return BETA_LIBRARY_SUMMARY_CACHE["data"].copy()

        summary = None
        try:
            with open(BETA_LIBRARY_SUMMARY_FILE, "r", encoding="utf-8") as handle:
                sidecar = json.load(handle)
            saved_signature = tuple(sidecar.get("source_signature") or []) if isinstance(sidecar, dict) else ()
            if saved_signature == tuple(source_signature or ()) and isinstance(sidecar.get("summary"), dict):
                summary = sidecar["summary"].copy()
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
            summary = None

        if summary is None:
            library_was_cached = BETA_LIBRARY_MEMORY_CACHE.get("data") is not None
            summary = _beta_library_summary_from_data(_beta_load_library_cache(settings), settings)
            if not library_was_cached:
                # Home needed the catalog once to create its sidecar. Release
                # it again so idle Home-only installations do not retain the
                # expanded multi-megabyte library in memory.
                BETA_LIBRARY_MEMORY_CACHE.update({"signature": None, "data": None})
            if source_signature:
                _beta_write_json(BETA_LIBRARY_SUMMARY_FILE, {
                    "source_signature": list(source_signature),
                    "summary": summary,
                })
        summary["configured"] = bool(configured_roots)
        BETA_LIBRARY_SUMMARY_CACHE.update({"signature": cache_signature, "data": summary.copy()})
        return summary


def _autopilot_review_samples() -> list[dict]:
    """Return real, distinct library files that can teach Smart Presets."""
    data = _beta_load_library_cache(load_settings())
    rows: list[dict] = []
    seen: set[str] = set()

    def add(path, title, media_type, poster_url="", year=None):
        source = str(path or "").strip()
        if (
            not source
            or source in seen
            or not os.path.isfile(source)
            or not is_allowed_path(source)
            or not source.lower().endswith(VIDEO_EXTS)
            or os.path.splitext(os.path.basename(source))[0].lower().endswith("-tsd")
        ):
            return
        seen.add(source)
        try:
            size_bytes = int(os.path.getsize(source))
        except OSError:
            size_bytes = 0
        rows.append(
            {
                "id": hashlib.sha256(source.encode("utf-8", errors="ignore")).hexdigest()[:20],
                "path": source,
                "title": str(title or os.path.basename(source))[:180],
                "media_type": media_type,
                "poster_url": str(poster_url or ""),
                "year": year,
                "size_bytes": size_bytes,
            }
        )

    for movie in data.get("movies") or []:
        if not isinstance(movie, dict):
            continue
        paths = [movie.get("path"), *(movie.get("paths") or [])]
        add(next((path for path in paths if path), ""), movie.get("title"), "movie", movie.get("poster_url"), movie.get("year"))
    for show in data.get("shows") or []:
        if not isinstance(show, dict):
            continue
        files = [row for row in show.get("files") or [] if isinstance(row, dict)]
        if files:
            episode = files[len(files) // 2]
            episode_label = episode.get("filename") or os.path.basename(str(episode.get("path") or ""))
            add(episode.get("path"), f"{show.get('title') or 'Show'} · {episode_label}", "show", show.get("poster_url"), show.get("year"))
    rows.sort(key=lambda row: (-int(row.get("size_bytes") or 0), str(row.get("title") or "").lower()))
    return rows


def _autopilot_review_payload(base_url: str = "", *, include_result: bool = True) -> dict:
    """Merge the current accurate-preview task with shared learning state."""
    with AUTOPILOT_REVIEW_LOCK:
        review = json.loads(json.dumps(AUTOPILOT_REVIEW_STATE))
    preview_id = str(review.get("preview_id") or "")
    task = _preview_get_task(preview_id) if preview_id else None
    if task:
        preview = task
        result = preview.get("result") if isinstance(preview.get("result"), dict) else None
        if result and base_url and str(result.get("clip_url") or "").startswith("/"):
            result["clip_url"] = f"{base_url.rstrip('/')}{result['clip_url']}"
            preview["result"] = result
        if not include_result and isinstance(preview.get("result"), dict):
            compact_result = dict(preview["result"])
            compact_result.pop("old_b64", None)
            compact_result.pop("new_b64", None)
            preview["result"] = compact_result
        review["preview"] = preview
    elif preview_id:
        review["preview"] = {
            "preview_id": preview_id,
            "state": "expired",
            "progress": 0,
            "message": "This preview expired. Generate another training sample.",
        }
    review["available_samples"] = len(_autopilot_review_samples())
    review["learning"] = smart_learning_status()
    return review


def _autopilot_review_summary() -> dict:
    review = _autopilot_review_payload(include_result=False)
    preview = review.get("preview") if isinstance(review.get("preview"), dict) else {}
    return {
        "active": bool(review.get("preview_id")),
        "review_id": review.get("review_id"),
        "title": review.get("title"),
        "state": preview.get("state") or review.get("state") or "idle",
        "progress": preview.get("progress") or 0,
        "message": preview.get("message") or review.get("message") or "Generate a preview to begin teaching Autopilot.",
        "available_samples": review.get("available_samples") or 0,
        "learning": review.get("learning") or smart_learning_status(),
    }


def _beta_save_library_cache(data: dict) -> None:
    with BETA_LIBRARY_CACHE_LOCK:
        _beta_sanitize_duplicate_artwork(data)
        _beta_write_json(BETA_LIBRARY_CACHE_FILE, data)
        source_signature = _beta_file_signature(BETA_LIBRARY_CACHE_FILE)
        summary = _beta_library_summary_from_data(data, load_settings())
        _beta_write_json(BETA_LIBRARY_SUMMARY_FILE, {
            "source_signature": list(source_signature or ()),
            "summary": summary,
        })
        _beta_invalidate_library_memory_cache()


def _beta_sanitize_duplicate_artwork(data: dict) -> bool:
    """Remove one poster incorrectly shared by unrelated library titles."""
    groups: dict[str, list[dict]] = {}
    for row in [*(data.get("movies") or []), *(data.get("shows") or [])]:
        if not isinstance(row, dict):
            continue
        url = str(row.get("poster_url") or "").strip()
        if url:
            groups.setdefault(url, []).append(row)

    changed = False
    for rows in groups.values():
        identities = {
            (
                str(row.get("type") or "").lower(),
                re.sub(r"[^a-z0-9]+", "", str(row.get("title") or "").lower()),
                str(row.get("year") or ""),
            )
            for row in rows
        }
        if len(identities) <= 1:
            continue
        for row in rows:
            row["poster_url"] = ""
            previous_source = str(row.get("metadata_source") or "unknown")
            for key in ("poster_source", "metadata_match_title", "metadata_match_year", "metadata_provider_id"):
                row.pop(key, None)
            row["metadata_source"] = f"{previous_source}_duplicate_removed"[:80]
            row["metadata_error"] = "Artwork shared by unrelated titles was removed; refresh the library to find a title-specific match."
            changed = True
    return changed


def _beta_clear_library_cache() -> None:
    for path in (BETA_LIBRARY_CACHE_FILE, BETA_LIBRARY_SUMMARY_FILE):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[WARN] Failed to clear {os.path.basename(path)}: {e}", flush=True)
    _beta_invalidate_library_memory_cache()


def _beta_clean_title(value: str) -> str:
    text = os.path.basename(value or "")
    root, extension = os.path.splitext(text)
    # Title fragments such as "Joker.Folie.a.Deux" are not filenames; do
    # not mistake the final dotted word for an extension and discard it.
    if extension.lower() in VIDEO_EXTS:
        text = root
    text = re.sub(r"[-_.]+", " ", text)
    text = re.sub(r"\bTSD\b$", " ", text, flags=re.IGNORECASE)
    technical_starts = [
        match.start()
        for match in (BETA_MEDIA_TAG_RE.search(text), BETA_DIMENSION_TAG_RE.search(text))
        if match
    ]
    if technical_starts:
        text = text[: min(technical_starts)]
    text = re.sub(r"[\[\]\(\)\{\}]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -._")
    return text or "Unknown Title"


def _beta_clean_show_title(value: str) -> str:
    raw = os.path.basename(str(value or ""))
    dated_folder = re.search(r"\s*[\[(](?:19|20)\d{2}(?:\s*[-–]\s*(?:19|20)\d{2})?[\])]", raw)
    # Release-pack folders often append an edition/year range followed by a
    # long contents list. The part before that marker is the stable show name.
    title_prefix = raw[: dated_folder.start()].strip(" -._") if dated_folder else ""
    text = _beta_clean_title(title_prefix if len(title_prefix) >= 2 else raw)
    text = re.sub(r"\s+(?:mp4|mkv)\s*$", "", text, flags=re.IGNORECASE)
    # Strip everything from a release-pack season suffix. This handles names
    # such as "Fallout.S01.COMPLETE" and "Futurama Season 1-7 S01-S07 Movies"
    # after separators and technical tags have been normalized.
    text = re.sub(r"\s+(?:seasons?\s*\d|s\d{1,2}\b).*$", "", text, flags=re.IGNORECASE)
    for _ in range(2):
        text = re.sub(r"\s+complete(?:\s+series)?\s*$", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" -._") or "Unknown Title"


def _beta_show_root_folder_name(src_path: str, library_root: str = "") -> str:
    if not library_root:
        return ""
    try:
        relative = os.path.relpath(os.path.dirname(src_path), library_root)
    except (OSError, ValueError):
        return ""
    parts = [part for part in re.split(r"[\\/]+", relative) if part and part != "."]
    if not parts or parts[0] == "..":
        return ""
    return parts[0]


def _beta_show_folder_is_generic(value: str) -> bool:
    normalized = re.sub(r"[^a-z]+", "", str(value or "").lower())
    return normalized in {
        "misc",
        "miscellaneous",
        "shows",
        "television",
        "telenov",
        "telonov",
        "telenovela",
        "telenovelas",
        "tv",
        "unsorted",
    }


def _beta_show_title_from_directories(src_path: str, library_root: str = "") -> str:
    root_folder = _beta_show_root_folder_name(src_path, library_root)
    if root_folder and not _beta_show_folder_is_generic(root_folder):
        return _beta_clean_show_title(root_folder)
    if root_folder:
        return ""
    generic = re.compile(
        r"^(?:season\s*\d+|s\d{1,2}|specials?|extras?|featurettes?|bonus(?:\s+features?)?)$",
        re.IGNORECASE,
    )
    current = os.path.dirname(src_path)
    root_real = os.path.normcase(os.path.realpath(library_root)) if library_root else ""
    for _depth in range(6):
        if not current:
            break
        current_real = os.path.normcase(os.path.realpath(current))
        if root_real and current_real == root_real:
            break
        name = os.path.basename(current)
        if name and not generic.fullmatch(name.strip()):
            return _beta_clean_show_title(name)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return ""


def _beta_title_from_path(
    src_path: str,
    title_part: str,
    media_type: str,
    *,
    root_kind: str = "",
    library_root: str = "",
) -> str:
    parent = os.path.basename(os.path.dirname(src_path))
    grandparent = os.path.basename(os.path.dirname(os.path.dirname(src_path)))
    parent_l = parent.lower()

    if media_type == "show":
        generic_episode_folders = re.compile(
            r"^(?:season\s*\d+|s\d{1,2}|specials?|extras?|featurettes?|bonus(?:\s+features?)?)$",
            re.IGNORECASE,
        )
        parent_is_root = bool(
            library_root
            and os.path.normcase(os.path.realpath(os.path.dirname(src_path)))
            == os.path.normcase(os.path.realpath(library_root))
        )
        # A mapped Shows root gives folder layout more authority than release
        # names. This keeps every episode in "Show/Season 01" or "Show" under
        # one stable catalog identity, even when episode filenames contain
        # scene titles, audio tags, or inconsistent separators.
        if root_kind == "shows":
            folder_title = _beta_show_title_from_directories(src_path, library_root)
            if folder_title:
                return folder_title
        if generic_episode_folders.search(parent_l) and grandparent:
            return _beta_clean_show_title(grandparent)
        if title_part and len(title_part.strip()) >= 2:
            return _beta_clean_show_title(title_part)
        if parent and not parent_is_root:
            return _beta_clean_show_title(parent)

    if title_part and len(title_part.strip()) >= 2:
        return _beta_clean_title(title_part)

    return _beta_clean_title(os.path.basename(src_path))


def _beta_parse_media(src_path: str, *, root_kind: str = "", library_root: str = "") -> dict:
    filename = os.path.basename(src_path)
    name_only, _ext = os.path.splitext(filename)
    source_type = _wizard_detect_source_type(src_path)
    media_type = "show" if source_type["kind"] == "show" else "movie"

    season = episode = None
    title_part = name_only
    show_match = re.search(
        r"(?i)(?:^|[ ._\-\[\(])s(\d{1,2})[ ._\-]*e(\d{1,3})(?=$|[^0-9])",
        name_only,
    )
    if not show_match:
        show_match = re.search(r"(?i)(?:^|[ ._\-\[\(])(\d{1,2})[ ._\-]*x[ ._\-]*(\d{1,3})(?=$|[^0-9])", name_only)
    if not show_match:
        show_match = re.search(
            r"(?i)(?:^|[ ._\-\[\(])season[ ._\-]*(\d{1,2})[ ._\-]+episode[ ._\-]*(\d{1,3})(?=$|[^0-9])",
            name_only,
        )
    if show_match:
        media_type = "show"
        season = int(show_match.group(1))
        episode = int(show_match.group(2))
        title_part = name_only[: show_match.start()]

    if root_kind == "shows":
        media_type = "show"
        parent = os.path.basename(os.path.dirname(src_path))
        folder_season = re.fullmatch(r"(?i)(?:season\s*|s)(\d{1,2})", parent.strip())
        if season is None and folder_season:
            season = int(folder_season.group(1))
        root_folder = _beta_show_root_folder_name(src_path, library_root)
        if show_match is None and _beta_show_folder_is_generic(root_folder):
            numbered_episode = re.match(
                r"(?i)^(.{2,80}?)[ ._\-]+(?:episode[ ._\-]*|ep[ ._\-]*)?(\d{2,4})(?=$|[^0-9])",
                name_only,
            )
            if numbered_episode:
                title_part = numbered_episode.group(1)
                season = season or 1
                episode = int(numbered_episode.group(2))
            else:
                special_episode = re.match(
                    r"(?i)^(.{2,80}?)[ ._\-]+(?:final|special|bonus|trailer|extras?)\b",
                    name_only,
                )
                if special_episode:
                    title_part = special_episode.group(1)
                    season = 0

    year = None
    year_match = re.search(r"(?:^|[ ._\-\[\(])((?:19|20)\d{2})(?:\D|$)", name_only)
    if year_match:
        year = int(year_match.group(1))
        if media_type == "movie":
            before_year = name_only[: year_match.start()].strip(" ._-[(")
            # Some collections use "1976.Movie.Title...". Treat a leading
            # year as metadata, not as the whole title or an empty title.
            title_part = before_year or name_only[year_match.end() :]

    title = _beta_title_from_path(
        src_path,
        title_part,
        media_type,
        root_kind=root_kind,
        library_root=library_root,
    )
    if root_kind == "shows":
        root_folder = _beta_show_root_folder_name(src_path, library_root)
        folder_year = re.search(r"(?:^|[^0-9])((?:19|20)\d{2})(?:[^0-9]|$)", root_folder)
        # Episode filenames may contain air/release years. They must not split
        # one show into multiple catalog identities; only the show folder owns
        # the series year.
        year = int(folder_year.group(1)) if folder_year else None
    # Folder names commonly carry a trailing release year. Keep it as match
    # metadata but remove it from the searchable title (except titles that are
    # themselves a year, such as "1923").
    title_year_match = re.search(r"(?:^|\s)((?:19|20)\d{2})$", title)
    if title_year_match:
        title_without_year = title[: title_year_match.start()].strip(" -._()[]")
        if len(title_without_year) >= 2:
            year = year or int(title_year_match.group(1))
            title = title_without_year
    try:
        size_bytes = int(os.path.getsize(src_path))
    except Exception:
        size_bytes = 0
    try:
        modified_at = float(os.path.getmtime(src_path))
    except Exception:
        modified_at = 0.0

    hdr_reason = _hdr_filename_reason(src_path)
    return {
        "id": uuid.uuid5(uuid.NAMESPACE_URL, src_path).hex,
        "type": media_type,
        "title": title,
        "year": year,
        "season": season,
        "episode": episode,
        "filename": filename,
        "path": src_path,
        "folder": os.path.dirname(src_path),
        "size_bytes": size_bytes,
        "modified_at": modified_at,
        "is_hdr": bool(hdr_reason),
        "hdr_reason": hdr_reason,
        "detected_reason": source_type.get("reason") or "filename",
        "parser_version": BETA_LIBRARY_PARSER_VERSION,
        "target": _wizard_source_target("show" if media_type == "show" else "movie"),
    }


def _beta_clean_tmdb_secret(value: str) -> str:
    value = str(value or "").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value


def _beta_looks_like_tmdb_bearer(value: str) -> bool:
    value = _beta_clean_tmdb_secret(value)
    return value.startswith("eyJ") or value.count(".") >= 2


def _beta_tmdb_config(settings: dict):
    bearer = _beta_clean_tmdb_secret(settings.get("tmdb_bearer_token") or "")
    api_key = _beta_clean_tmdb_secret(settings.get("tmdb_api_key") or "")
    if not bearer and _beta_looks_like_tmdb_bearer(api_key):
        bearer = api_key
        api_key = ""
    if bearer:
        return {"Authorization": f"Bearer {bearer}"}, {}
    if api_key:
        return {}, {"api_key": api_key}
    return None


def _beta_tmdb_auth_cache_tag(settings: dict) -> str:
    auth = _beta_tmdb_config(settings or {})
    if not auth:
        return "off"
    headers, extra_params = auth
    secret = headers.get("Authorization") or extra_params.get("api_key") or ""
    if not secret:
        return "off"
    return hashlib.sha256(secret.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _beta_tmdb_title_tokens(value: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if token not in {"the", "a", "an"}
    ]


def _beta_choose_tmdb_result(rows: list, title: str, year=None) -> dict | None:
    """Choose a TMDb result only when its title really matches the library title."""
    wanted = re.sub(r"[^a-z0-9]+", "", str(title or "").lower())
    wanted_tokens = set(_beta_tmdb_title_tokens(title))
    try:
        wanted_year = int(year) if year not in (None, "") else None
    except (TypeError, ValueError):
        wanted_year = None
    ranked = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        names = [row.get("title"), row.get("name"), row.get("original_title"), row.get("original_name")]
        best_title_score = None
        for raw_name in names:
            candidate = re.sub(r"[^a-z0-9]+", "", str(raw_name or "").lower())
            if not wanted or not candidate:
                continue
            candidate_tokens = set(_beta_tmdb_title_tokens(raw_name))
            shared = len(wanted_tokens & candidate_tokens)
            coverage = shared / max(1, len(wanted_tokens))
            candidate_coverage = shared / max(1, len(candidate_tokens))
            similarity = difflib.SequenceMatcher(None, wanted, candidate).ratio()
            exact = candidate == wanted
            if not (exact or similarity >= 0.74 or (coverage >= 0.75 and candidate_coverage >= 0.6)):
                continue
            title_score = (6.0 if exact else 0.0) + similarity * 4.0 + coverage * 2.0 + candidate_coverage
            best_title_score = max(best_title_score or 0.0, title_score)
        if best_title_score is None:
            continue

        date_text = str(row.get("first_air_date") or row.get("release_date") or "")
        try:
            candidate_year = int(date_text[:4]) if len(date_text) >= 4 else None
        except (TypeError, ValueError):
            candidate_year = None
        if wanted_year is not None and candidate_year is not None:
            year_delta = abs(candidate_year - wanted_year)
            if year_delta > 1:
                continue
            year_score = 1.5 if year_delta == 0 else 0.5
        else:
            year_score = 0.2
        poster_score = 0.35 if row.get("poster_path") else 0.0
        try:
            popularity = float(row.get("popularity") or 0.0)
        except (TypeError, ValueError):
            popularity = 0.0
        popularity_score = min(0.25, max(0.0, popularity / 1000.0))
        ranked.append((best_title_score + year_score + poster_score + popularity_score, row))
    ranked.sort(key=lambda entry: entry[0], reverse=True)
    return ranked[0][1] if ranked else None


def _public_settings(settings: dict) -> dict:
    """Return browser-safe settings without cloud-provider secrets."""
    public = dict(settings or {})
    for key in (
        "gemini_api_key",
        "openai_api_key",
        "worker_controller_managed_capacity",
    ):
        public.pop(key, None)
    public["gemini_api_configured"] = bool(os.environ.get("GEMINI_API_KEY") or settings.get("gemini_api_key"))
    public["openai_api_configured"] = bool(os.environ.get("OPENAI_API_KEY") or settings.get("openai_api_key"))
    return public


def _wizard_ai_settings_payload(settings: dict | None = None) -> dict:
    settings = settings or load_settings()
    status = wizard_llm_status(settings)
    return {
        "provider": status.get("provider") or "local",
        "model": status.get("model") or "",
        "ready": bool(status.get("ready")),
        "providers": status.get("providers") or {},
        "gemini_model": str(settings.get("gemini_model") or "gemini-3.6-flash"),
        "openai_model": str(settings.get("openai_model") or "gpt-5.6-luna"),
        "gemini_api_configured": bool(os.environ.get("GEMINI_API_KEY") or settings.get("gemini_api_key")),
        "openai_api_configured": bool(os.environ.get("OPENAI_API_KEY") or settings.get("openai_api_key")),
        "gemini_key_source": "environment" if os.environ.get("GEMINI_API_KEY") else ("settings" if settings.get("gemini_api_key") else "none"),
        "openai_key_source": "environment" if os.environ.get("OPENAI_API_KEY") else ("settings" if settings.get("openai_api_key") else "none"),
    }


def _beta_tmdb_search(media_type: str, title: str, year=None, settings=None) -> dict:
    settings = settings or {}
    auth = _beta_tmdb_config(settings)
    if not auth or not title:
        return {"configured": bool(auth), "poster_url": "", "source": "placeholder"}

    cache_key = (media_type, title.lower(), str(year or ""), _beta_tmdb_auth_cache_tag(settings))
    cached = BETA_POSTER_CACHE.get(cache_key)
    if cached:
        return cached.copy()

    headers, extra_params = auth
    endpoint = "tv" if media_type == "show" else "movie"
    params = {
        "query": title,
        "include_adult": "false",
        "language": "en-US",
        "page": "1",
        **extra_params,
    }
    if year:
        params["first_air_date_year" if media_type == "show" else "year"] = str(year)

    url = f"https://api.themoviedb.org/3/search/{endpoint}?{urlencode(params)}"
    req = Request(url, headers={"accept": "application/json", **headers})
    try:
        with urlopen(req, timeout=8) as res:
            payload = json.loads(res.read().decode("utf-8", errors="replace"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        result = {"configured": True, "poster_url": "", "source": "tmdb_error", "error": str(e)[:180]}
        BETA_POSTER_CACHE[cache_key] = result
        return result.copy()

    rows = payload.get("results") if isinstance(payload, dict) else []
    rows = rows if isinstance(rows, list) else []
    best = _beta_choose_tmdb_result(rows, title, year)
    if not isinstance(best, dict):
        result = {"configured": True, "poster_url": "", "source": "tmdb_empty"}
        BETA_POSTER_CACHE[cache_key] = result
        return result.copy()

    poster_path = best.get("poster_path") or ""
    poster_url = f"https://image.tmdb.org/t/p/w342{poster_path}" if poster_path else ""
    result = {
        "configured": True,
        "poster_url": poster_url,
        "source": "tmdb" if poster_url else "tmdb_no_poster",
        "poster_source": "tmdb" if poster_url else "",
        "metadata_source": "tmdb" if poster_url else "tmdb_no_poster",
        "metadata_provider": {"name": "TMDb", "url": "https://www.themoviedb.org/"},
        "tmdb_id": best.get("id"),
        "tmdb_title": best.get("name") or best.get("title") or "",
        "tmdb_year": (best.get("first_air_date") or best.get("release_date") or "")[:4],
        "metadata_match_title": best.get("name") or best.get("title") or "",
        "metadata_match_year": (best.get("first_air_date") or best.get("release_date") or "")[:4],
        "metadata_provider_id": best.get("id"),
    }
    BETA_POSTER_CACHE[cache_key] = result
    return result.copy()


def _beta_tmdb_season_art(tmdb_id, seasons: list[int], settings=None) -> dict:
    settings = settings or {}
    auth = _beta_tmdb_config(settings)
    if not auth or not tmdb_id:
        return {}

    headers, extra_params = auth
    auth_tag = _beta_tmdb_auth_cache_tag(settings)
    art = {}
    for raw_season in seasons or []:
        try:
            season = int(raw_season)
        except (TypeError, ValueError):
            continue
        if season < 0:
            continue

        cache_key = ("season", str(tmdb_id), str(season), auth_tag)
        cached = BETA_POSTER_CACHE.get(cache_key)
        if cached:
            result = cached.copy()
        else:
            params = {"language": "en-US", **extra_params}
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}?{urlencode(params)}"
            req = Request(url, headers={"accept": "application/json", **headers})
            try:
                with urlopen(req, timeout=8) as res:
                    payload = json.loads(res.read().decode("utf-8", errors="replace"))
                poster_path = payload.get("poster_path") if isinstance(payload, dict) else ""
                poster_url = f"https://image.tmdb.org/t/p/w342{poster_path}" if poster_path else ""
                result = {"poster_url": poster_url, "source": "tmdb_season" if poster_url else "tmdb_season_no_poster"}
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
                result = {"poster_url": "", "source": "tmdb_error", "error": str(e)[:180]}
            BETA_POSTER_CACHE[cache_key] = result

        if result.get("poster_url"):
            art[str(season)] = result["poster_url"]
    return art


def _beta_finalize_catalog(data: dict) -> dict:
    """Add compact catalog views used by the web and mobile home screens."""
    movies = [row for row in data.get("movies") or [] if isinstance(row, dict)]
    shows = [row for row in data.get("shows") or [] if isinstance(row, dict)]
    for show in shows:
        show["modified_at"] = max(
            [float(ep.get("modified_at") or 0) for ep in show.get("files") or [] if isinstance(ep, dict)] or [0.0]
        )
    recent = sorted(
        [*movies, *shows],
        key=lambda row: float(row.get("modified_at") or 0),
        reverse=True,
    )[:24]
    episodes = sum(int(row.get("episode_count") or 0) for row in shows)
    data["catalog"] = {
        "total_titles": len(movies) + len(shows),
        "movies": len(movies),
        "shows": len(shows),
        "episodes": episodes,
        "complete": not bool((data.get("stats") or {}).get("limited")),
        "recently_added": recent,
    }
    all_titles = [*movies, *shows]
    matched = sum(1 for row in all_titles if str(row.get("poster_url") or "").strip())
    sources: dict[str, int] = {}
    for row in all_titles:
        source = str(row.get("poster_source") or row.get("metadata_source") or "missing").strip() or "missing"
        sources[source] = sources.get(source, 0) + 1
    metadata = data.setdefault("metadata", {})
    if isinstance(metadata, dict):
        metadata["artwork"] = {
            "matched": matched,
            "missing": max(0, len(all_titles) - matched),
            "total": len(all_titles),
            "percent": round((matched / len(all_titles)) * 100, 1) if all_titles else 100.0,
            "sources": sources,
        }
    return data


def _beta_enrich_metadata(data: dict, settings: dict, *, enabled: bool = True) -> dict:
    """Prefer TMDb artwork when configured, with local/keyless fallback."""
    data = _beta_apply_cached_art(data)
    tmdb_enabled = bool(enabled and _beta_tmdb_config(settings))
    preferred_art: list[tuple[dict, dict, dict]] = []
    if tmdb_enabled:
        # Resolve TMDb first. Keyless metadata still supplies release calendars
        # and acts as the artwork fallback, then successful TMDb art is applied
        # again so it always wins in the final catalog sent to web and mobile.
        for show in data.get("shows") or []:
            if not isinstance(show, dict):
                continue
            if show.get("poster_url") and show.get("tmdb_id") and show.get("metadata_source") == "tmdb":
                result = {
                    "poster_url": show.get("poster_url"),
                    "poster_source": "tmdb",
                    "metadata_source": "tmdb",
                    "tmdb_id": show.get("tmdb_id"),
                }
            else:
                result = _beta_tmdb_search("show", show.get("title"), show.get("year"), settings)
            existing_season_art = show.get("season_art") if isinstance(show.get("season_art"), dict) else {}
            missing_seasons = [season for season in show.get("seasons") or [] if not existing_season_art.get(str(season))]
            season_art = {**existing_season_art, **_beta_tmdb_season_art(result.get("tmdb_id"), missing_seasons, settings)}
            if result.get("poster_url"):
                show.update(result)
            preferred_art.append((show, result, season_art))
        for movie in data.get("movies") or []:
            if isinstance(movie, dict):
                if movie.get("poster_url") and movie.get("tmdb_id") and movie.get("metadata_source") == "tmdb":
                    result = {
                        "poster_url": movie.get("poster_url"),
                        "poster_source": "tmdb",
                        "metadata_source": "tmdb",
                        "tmdb_id": movie.get("tmdb_id"),
                    }
                else:
                    result = _beta_tmdb_search("movie", movie.get("title"), movie.get("year"), settings)
                if result.get("poster_url"):
                    movie.update(result)
                preferred_art.append((movie, result, {}))

    if enabled or settings.get("episode_release_monitor_enabled", True):
        data = enrich_media_library(data, settings)

    for item, result, season_art in preferred_art:
        if result.get("poster_url"):
            item.update(result)
        elif result.get("tmdb_id"):
            item["tmdb_id"] = result.get("tmdb_id")
        if season_art:
            item["season_art"] = season_art

    metadata = data.setdefault("metadata", {})
    if isinstance(metadata, dict):
        metadata["tmdb_configured"] = bool(_beta_tmdb_config(settings))
        metadata["artwork_priority"] = "tmdb_then_keyless" if tmdb_enabled else "keyless"

    _beta_sanitize_duplicate_artwork(data)
    return _beta_finalize_catalog(data)


def _beta_show_group_key(title: str, year=None) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(title or "Unknown Title").lower()).strip()
    return f"{normalized}::{year or ''}"


def _beta_resolve_show_group_key(groups: dict, title: str, year=None) -> str:
    exact = _beta_show_group_key(title, year)
    normalized = exact.rsplit("::", 1)[0]
    candidates = [key for key in groups if key.rsplit("::", 1)[0] == normalized]
    if exact in groups:
        return exact
    if year not in (None, ""):
        missing_year_key = f"{normalized}::"
        if missing_year_key in groups and len(candidates) == 1:
            groups[exact] = groups.pop(missing_year_key)
            if not groups[exact].get("year"):
                groups[exact]["year"] = year
            return exact
    elif len(candidates) == 1:
        return candidates[0]
    return exact


def _beta_scan_library(root_path: str, *, recursive: bool, posters: bool, settings: dict, root_kind: str = "") -> dict:
    movies = []
    shows = {}
    scanned = 0
    skipped_tsd = 0

    walker = os.walk(root_path) if recursive else [(root_path, [], os.listdir(root_path))]
    for root_dir, _dirs, names in walker:
        for name in sorted(names):
            if not name.lower().endswith(VIDEO_EXTS):
                continue
            full = os.path.join(root_dir, name)
            if not os.path.isfile(full):
                continue
            if os.path.splitext(name)[0].lower().endswith("-tsd"):
                skipped_tsd += 1
                continue

            item = _beta_parse_media(full, root_kind=root_kind, library_root=root_path)
            if root_kind == "movies":
                item["type"] = "movie"
                item["target"] = _wizard_source_target("movie")
            elif root_kind == "shows":
                item["type"] = "show"
                item["target"] = _wizard_source_target("show")
            scanned += 1
            if item["type"] == "show":
                key = _beta_resolve_show_group_key(shows, item.get("title"), item.get("year"))
                identity = f"{item['title'].lower()}::{item.get('year') or ''}"
                group = shows.setdefault(
                    key,
                    {
                        "id": uuid.uuid5(uuid.NAMESPACE_DNS, identity).hex,
                        "type": "show",
                        "title": item["title"],
                        "year": item.get("year"),
                        "episode_count": 0,
                        "season_count": 0,
                        "seasons": [],
                        "total_size_bytes": 0,
                        "files": [],
                        "target": _wizard_source_target("show"),
                    },
                )
                group["episode_count"] += 1
                group["total_size_bytes"] += item["size_bytes"]
                group["files"].append(item)
            else:
                movies.append(item)

    for group in shows.values():
        seasons = sorted({ep["season"] for ep in group["files"] if ep.get("season") is not None})
        group["season_count"] = len(seasons)
        group["seasons"] = seasons
        group["files"].sort(key=lambda ep: (ep.get("season") or 0, ep.get("episode") or 0, ep["filename"].lower()))
    movies.sort(key=lambda item: ((item.get("title") or "").lower(), item.get("year") or 0))
    show_rows = sorted(shows.values(), key=lambda item: ((item.get("title") or "").lower(), item.get("year") or 0))

    data = {
        "scope": "root",
        "root": root_path,
        "roots": [next((row for row in _beta_mapped_roots(settings) if row["path"] == root_path), {"path": root_path, "label": root_path})],
        "recursive": recursive,
        "stats": {
            "movies": len(movies),
            "shows": len(show_rows),
            "episodes": sum(row["episode_count"] for row in show_rows),
            "scanned": scanned,
            "skipped_tsd": skipped_tsd,
            "limited": False,
        },
        "tmdb_configured": bool(_beta_tmdb_config(settings)),
        "movies": movies,
        "shows": show_rows,
    }
    return _beta_enrich_metadata(data, settings, enabled=True) if posters else _beta_finalize_catalog(data)


def _beta_merge_show_group(groups: dict, incoming: dict) -> None:
    key = _beta_resolve_show_group_key(groups, incoming.get("title"), incoming.get("year"))
    identity = f"{str(incoming.get('title') or '').lower()}::{incoming.get('year') or ''}"
    group = groups.setdefault(
        key,
        {
            "id": incoming.get("id") or uuid.uuid5(uuid.NAMESPACE_DNS, identity).hex,
            "type": "show",
            "title": incoming.get("title") or "Unknown Title",
            "year": incoming.get("year"),
            "episode_count": 0,
            "season_count": 0,
            "seasons": [],
            "total_size_bytes": 0,
            "files": [],
            "target": incoming.get("target") or _wizard_source_target("show"),
        },
    )

    group["episode_count"] += int(incoming.get("episode_count") or 0)
    group["total_size_bytes"] += int(incoming.get("total_size_bytes") or 0)
    group["files"].extend(incoming.get("files") or [])
    if not group.get("year") and incoming.get("year"):
        group["year"] = incoming.get("year")

    for key_name in ("poster_url", "poster_source", "source", "tmdb_id", "tmdb_title", "tmdb_year", "error"):
        if not group.get(key_name) and incoming.get(key_name):
            group[key_name] = incoming.get(key_name)

    incoming_art = incoming.get("season_art") if isinstance(incoming.get("season_art"), dict) else {}
    if incoming_art:
        group_art = group.setdefault("season_art", {})
        if isinstance(group_art, dict):
            for season, poster_url in incoming_art.items():
                if poster_url and not group_art.get(str(season)):
                    group_art[str(season)] = poster_url

    seasons = sorted({ep["season"] for ep in group["files"] if ep.get("season") is not None})
    group["season_count"] = len(seasons)
    group["seasons"] = seasons
    group["files"].sort(key=lambda ep: (ep.get("season") or 0, ep.get("episode") or 0, ep.get("filename", "").lower()))


def _beta_combine_library_scans(scans: list[dict], *, recursive: bool, settings: dict) -> dict:
    movies = []
    show_groups = {}
    roots = []
    skipped_tsd = 0
    scanned = 0

    for scan in scans:
        if not isinstance(scan, dict):
            continue
        roots.extend(scan.get("roots") or [])
        movies.extend(scan.get("movies") or [])
        for show in scan.get("shows") or []:
            _beta_merge_show_group(show_groups, show)
        stats = scan.get("stats") or {}
        skipped_tsd += int(stats.get("skipped_tsd") or 0)
        scanned += int(stats.get("scanned") or 0)

    movies.sort(key=lambda item: ((item.get("title") or "").lower(), item.get("year") or 0))
    show_rows = sorted(show_groups.values(), key=lambda item: ((item.get("title") or "").lower(), item.get("year") or 0))

    return {
        "scope": "all",
        "root": "__all__",
        "roots": roots,
        "recursive": recursive,
        "stats": {
            "movies": len(movies),
            "shows": len(show_rows),
            "episodes": sum(int(row.get("episode_count") or 0) for row in show_rows),
            "scanned": scanned,
            "skipped_tsd": skipped_tsd,
            "limited": False,
        },
        "tmdb_configured": bool(_beta_tmdb_config(settings)),
        "movies": movies,
        "shows": show_rows,
    }


def _beta_refresh_predictions(data: dict) -> dict:
    data = data if isinstance(data, dict) else {}
    model = _history_prediction_model()

    for item in data.get("movies") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        preset = guess_preset_from_filename(os.path.basename(path))
        filename_hdr_reason = _hdr_filename_reason(path)
        is_hdr = bool(item.get("is_hdr") or filename_hdr_reason)
        item["is_hdr"] = is_hdr
        if is_hdr and (not item.get("hdr_reason") or item.get("hdr_reason") == "filename"):
            item["hdr_reason"] = filename_hdr_reason or item.get("hdr_reason") or "filename"
        item["prediction"] = _history_prediction_for(int(item.get("size_bytes") or 0), preset, is_hdr, model)

    for show in data.get("shows") or []:
        if not isinstance(show, dict):
            continue
        file_predictions = []
        show_is_hdr = bool(show.get("is_hdr"))
        for ep in show.get("files") or []:
            if not isinstance(ep, dict):
                continue
            path = str(ep.get("path") or "")
            preset = guess_preset_from_filename(os.path.basename(path))
            filename_hdr_reason = _hdr_filename_reason(path)
            is_hdr = bool(ep.get("is_hdr") or filename_hdr_reason)
            ep["is_hdr"] = is_hdr
            if is_hdr and (not ep.get("hdr_reason") or ep.get("hdr_reason") == "filename"):
                ep["hdr_reason"] = filename_hdr_reason or ep.get("hdr_reason") or "filename"
            show_is_hdr = show_is_hdr or is_hdr
            ep["prediction"] = _history_prediction_for(int(ep.get("size_bytes") or 0), preset, is_hdr, model)
            file_predictions.append(ep["prediction"])
        show["is_hdr"] = bool(show_is_hdr)
        if show["is_hdr"] and not show.get("hdr_reason"):
            show["hdr_reason"] = "episode"
        show["prediction"] = _combine_history_predictions(file_predictions, int(show.get("total_size_bytes") or 0))

    return data


def _beta_scan_all_libraries(*, recursive: bool, posters: bool, settings: dict) -> dict:
    scans = []
    for row in _beta_mapped_roots(settings):
        root_path = row["path"]
        if not root_path or not is_allowed_path(root_path) or not os.path.isdir(root_path):
            continue
        scan = _beta_scan_library(
            root_path,
            recursive=recursive,
            posters=False,
            settings=settings,
            root_kind=row.get("kind") or "",
        )
        scans.append(scan)

    data = _beta_combine_library_scans(scans, recursive=recursive, settings=settings)
    return _beta_enrich_metadata(data, settings, enabled=posters)


def _beta_empty_scan_index() -> dict:
    return {
        "version": BETA_LIBRARY_PARSER_VERSION,
        "generated_at": 0,
        "last_full_scan_at": 0,
        "files": {},
        "directories": {},
        "unavailable_paths": [],
    }


def _beta_load_scan_index() -> dict:
    try:
        with open(BETA_SCAN_INDEX_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return _beta_empty_scan_index()
    except Exception as e:
        print(f"[WARN] Failed to load beta scan index: {e}", flush=True)
        return _beta_empty_scan_index()
    if not isinstance(data, dict):
        return _beta_empty_scan_index()
    data.setdefault("version", 1)
    data.setdefault("generated_at", 0)
    data.setdefault("last_full_scan_at", 0)
    data.setdefault("files", {})
    data.setdefault("directories", {})
    data.setdefault("unavailable_paths", [])
    if not isinstance(data["files"], dict):
        data["files"] = {}
    if not isinstance(data["directories"], dict):
        data["directories"] = {}
    if not isinstance(data["unavailable_paths"], list):
        data["unavailable_paths"] = []
    return data


def _beta_save_scan_index(index: dict) -> None:
    index = index if isinstance(index, dict) else _beta_empty_scan_index()
    index["version"] = BETA_LIBRARY_PARSER_VERSION
    index["generated_at"] = time.time()
    _beta_write_json(BETA_SCAN_INDEX_FILE, index)


def _beta_autoscan_interval_seconds(settings: dict | None) -> int:
    try:
        minutes = int((settings or {}).get("beta_auto_scan_interval_minutes") or 30)
    except (TypeError, ValueError):
        minutes = 30
    return max(300, min(7 * 86400, minutes * 60))


def _beta_empty_autoscan_status(settings=None) -> dict:
    settings = settings or {}
    interval_seconds = _beta_autoscan_interval_seconds(settings)
    return {
        "running": False,
        "last_started_at": 0,
        "last_finished_at": 0,
        "last_status": "idle",
        "last_message": "No auto scan has run yet.",
        "last_summary": {},
        "next_scan_at": time.time() + interval_seconds,
    }


def _beta_load_autoscan_status(settings=None) -> dict:
    try:
        with open(BETA_AUTOSCAN_STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return _beta_empty_autoscan_status(settings)
    except Exception:
        return _beta_empty_autoscan_status(settings)
    if not isinstance(data, dict):
        return _beta_empty_autoscan_status(settings)
    empty = _beta_empty_autoscan_status(settings)
    empty.update(data)
    return empty


def _beta_save_autoscan_status(status: dict) -> dict:
    status = status if isinstance(status, dict) else {}
    try:
        _beta_write_json(BETA_AUTOSCAN_STATUS_FILE, status)
    except Exception as exc:
        # Status persistence should never terminate the scheduler or leave its
        # run lock held. The next cycle can repair a transient NAS write error.
        status["persistence_error"] = str(exc)[:240]
        print(f"[WARN] Failed to save auto-scan status: {exc}", flush=True)
    return status


def _beta_file_is_stable(index_row: dict, settings: dict, now: float | None = None) -> bool:
    if not settings.get("beta_auto_scan_file_stability_enabled", True):
        return True
    now = now or time.time()
    try:
        stable_passes = int(index_row.get("stable_passes") or 0)
    except Exception:
        stable_passes = 0
    try:
        changed_at = float(index_row.get("changed_at") or index_row.get("first_seen") or now)
    except Exception:
        changed_at = now
    stability_seconds = max(60, int(settings.get("beta_auto_scan_file_stability_minutes") or 10) * 60)
    return stable_passes >= 1 and (now - changed_at) >= stability_seconds


def _beta_cached_art_maps() -> tuple[dict, dict]:
    try:
        with open(BETA_LIBRARY_CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        cache = {}
    movies = {}
    shows = {}
    if not isinstance(cache, dict):
        return movies, shows
    _beta_sanitize_duplicate_artwork(cache)

    for item in cache.get("movies") or []:
        if not isinstance(item, dict):
            continue
        keys = {
            str(item.get("id") or ""),
            str(item.get("path") or ""),
            f"{str(item.get('title') or '').lower()}::{item.get('year') or ''}",
        }
        art = {
            k: item.get(k)
            for k in (
                "poster_url", "poster_source", "source", "tmdb_id", "tmdb_title", "tmdb_year", "error",
                "metadata_source", "metadata_provider", "metadata_url", "summary", "genres", "release_date",
                "metadata_match_title", "metadata_match_year", "metadata_provider_id",
            )
            if item.get(k)
        }
        for key in keys:
            if key and art:
                movies[key] = art

    for item in cache.get("shows") or []:
        if not isinstance(item, dict):
            continue
        keys = {
            str(item.get("id") or ""),
            f"{str(item.get('title') or '').lower()}::{item.get('year') or ''}",
            _beta_show_group_key(item.get("title"), item.get("year")),
        }
        art = {
            k: item.get(k)
            for k in (
                "poster_url", "poster_source", "source", "tmdb_id", "tmdb_title", "tmdb_year", "error", "season_art",
                "metadata_source", "metadata_provider", "metadata_url", "summary", "genres", "tvmaze_id",
                "show_status", "network", "next_episode", "release_calendar", "metadata_match_title",
                "metadata_match_year", "metadata_provider_id",
            )
            if item.get(k)
        }
        for key in keys:
            if key and art:
                shows[key] = art
    return movies, shows


def _beta_apply_cached_art(data: dict) -> dict:
    movie_art, show_art = _beta_cached_art_maps()
    for item in data.get("movies") or []:
        if not isinstance(item, dict):
            continue
        art = (
            movie_art.get(str(item.get("id") or ""))
            or movie_art.get(str(item.get("path") or ""))
            or movie_art.get(f"{str(item.get('title') or '').lower()}::{item.get('year') or ''}")
            or {}
        )
        for key, value in art.items():
            if value and not item.get(key):
                item[key] = value
    for show in data.get("shows") or []:
        if not isinstance(show, dict):
            continue
        art = (
            show_art.get(str(show.get("id") or ""))
            or show_art.get(f"{str(show.get('title') or '').lower()}::{show.get('year') or ''}")
            or show_art.get(_beta_show_group_key(show.get("title"), show.get("year")))
            or {}
        )
        for key, value in art.items():
            if value and not show.get(key):
                show[key] = value
    return data


def _beta_library_from_scan_index(index: dict, *, settings: dict, recursive: bool = True) -> dict:
    movies = []
    shows = {}
    scanned = 0
    skipped_tsd = 0
    files = index.get("files") if isinstance(index.get("files"), dict) else {}

    for row in files.values():
        if not isinstance(row, dict) or row.get("removed"):
            continue
        item = row.get("item") if isinstance(row.get("item"), dict) else None
        if not item:
            continue
        scanned += 1
        if os.path.splitext(os.path.basename(item.get("path") or ""))[0].lower().endswith("-tsd"):
            skipped_tsd += 1
            continue
        if item.get("type") == "show":
            key = _beta_resolve_show_group_key(shows, item.get("title"), item.get("year"))
            identity = f"{item.get('title', '').lower()}::{item.get('year') or ''}"
            group = shows.setdefault(
                key,
                {
                    "id": uuid.uuid5(uuid.NAMESPACE_DNS, identity).hex,
                    "type": "show",
                    "title": item.get("title") or "Unknown Title",
                    "year": item.get("year"),
                    "episode_count": 0,
                    "season_count": 0,
                    "seasons": [],
                    "total_size_bytes": 0,
                    "files": [],
                    "target": _wizard_source_target("show"),
                },
            )
            group["episode_count"] += 1
            group["total_size_bytes"] += int(item.get("size_bytes") or 0)
            group["files"].append(item.copy())
        else:
            movies.append(item.copy())

    for group in shows.values():
        seasons = sorted({ep["season"] for ep in group["files"] if ep.get("season") is not None})
        group["season_count"] = len(seasons)
        group["seasons"] = seasons
        group["files"].sort(key=lambda ep: (ep.get("season") or 0, ep.get("episode") or 0, ep.get("filename", "").lower()))

    movies.sort(key=lambda item: ((item.get("title") or "").lower(), item.get("year") or 0))
    show_rows = sorted(shows.values(), key=lambda item: ((item.get("title") or "").lower(), item.get("year") or 0))
    roots = [row for row in _beta_mapped_roots(settings) if row.get("path")]

    data = {
        "scope": "all",
        "root": "__all__",
        "roots": roots,
        "recursive": recursive,
        "stats": {
            "movies": len(movies),
            "shows": len(show_rows),
            "episodes": sum(int(row.get("episode_count") or 0) for row in show_rows),
            "scanned": scanned,
            "skipped_tsd": skipped_tsd,
            "limited": False,
        },
        "tmdb_configured": bool(_beta_tmdb_config(settings)),
        "movies": movies,
        "shows": show_rows,
    }
    return _beta_finalize_catalog(_beta_apply_cached_art(data))


def _beta_active_job_paths() -> set[str]:
    active = set()
    for job in list_jobs_for_api():
        if str(job.get("status") or "").lower() in {"queued", "running"} and job.get("src"):
            active.add(str(job.get("src")))
    return active


def _beta_update_scan_index(settings: dict, *, force_full: bool = False) -> tuple[dict, dict]:
    """Refresh the media index with a low-I/O directory cache.

    Most libraries live on SMB/NFS storage where repeated ``isfile`` and
    ``stat`` calls dominate scan time.  A quick pass stats each directory and
    reuses its direct file list when the directory mtime is unchanged.  New or
    still-growing files are always statted, and a periodic full verification
    catches rare in-place edits that do not update a parent directory mtime.
    """
    started = time.monotonic()
    now = time.time()
    index = _beta_load_scan_index()
    files = index.setdefault("files", {})
    directories = index.setdefault("directories", {})
    try:
        last_full_scan_at = float(index.get("last_full_scan_at") or 0)
    except (TypeError, ValueError):
        last_full_scan_at = 0
    full_scan = bool(
        force_full
        or not directories
        or not last_full_scan_at
        or (now - last_full_scan_at) >= BETA_SCAN_FULL_VERIFY_SECONDS
    )
    seen_paths: set[str] = set()
    seen_directories: set[str] = set()
    unavailable_paths: set[str] = set()
    index_dirty = False
    summary = {
        "mode": "full_verify" if full_scan else "quick_incremental",
        "scanned": 0,
        "new": 0,
        "changed": 0,
        "reclassified": 0,
        "removed": 0,
        "unchanged": 0,
        "skipped_tsd": 0,
        "directories_scanned": 0,
        "directories_cached": 0,
        "file_stats": 0,
        "unavailable_roots": 0,
        "errors": 0,
    }

    files_by_directory: dict[str, list[str]] = {}
    for path, row in files.items():
        if isinstance(row, dict) and not row.get("removed"):
            files_by_directory.setdefault(os.path.dirname(path), []).append(path)

    def mtime_matches(row: dict, stat_result) -> bool:
        stored_ns = row.get("mtime_ns")
        if stored_ns is not None:
            try:
                return int(stored_ns) == int(stat_result.st_mtime_ns)
            except (TypeError, ValueError, AttributeError):
                return False
        try:
            return abs(float(row.get("mtime") or 0) - float(stat_result.st_mtime)) < 0.0001
        except (TypeError, ValueError):
            return False

    def accept_cached(path: str, root_kind: str, root_path: str) -> None:
        """Reuse a stable row, but keep statting files still being copied."""
        nonlocal index_dirty
        if path in seen_paths:
            return
        row = files.get(path) if isinstance(files.get(path), dict) else {}
        if not row or row.get("removed"):
            return
        if not _beta_file_is_stable(row, settings, now):
            try:
                stat_result = os.stat(path)
                summary["file_stats"] += 1
            except OSError:
                summary["errors"] += 1
                unavailable_paths.add(path)
                # A transient NAS read error must not delete a previously
                # indexed file. A later scan will verify it again.
                seen_paths.add(path)
                summary["scanned"] += 1
                summary["unchanged"] += 1
                return
            process_file(path, stat_result, root_kind, root_path)
            return
        seen_paths.add(path)
        summary["scanned"] += 1
        summary["unchanged"] += 1

    def process_file(path: str, stat_result, root_kind: str, root_path: str) -> None:
        nonlocal index_dirty
        if path in seen_paths:
            return
        seen_paths.add(path)
        summary["scanned"] += 1
        size_bytes = int(stat_result.st_size)
        mtime = float(stat_result.st_mtime)
        mtime_ns = int(getattr(stat_result, "st_mtime_ns", int(mtime * 1_000_000_000)))
        row = files.get(path) if isinstance(files.get(path), dict) else {}
        content_same = bool(
            row
            and not row.get("removed")
            and int(row.get("size_bytes") or -1) == size_bytes
            and mtime_matches(row, stat_result)
            and isinstance(row.get("item"), dict)
        )
        same = (
            content_same
            and int(row.get("parser_version") or 0) == BETA_LIBRARY_PARSER_VERSION
            and str(row.get("root_kind") or "") == root_kind
        )
        if same:
            if row.get("mtime_ns") is None:
                row["mtime_ns"] = mtime_ns
                index_dirty = True
            stable_passes = min(2, int(row.get("stable_passes") or 0) + 1)
            if stable_passes != int(row.get("stable_passes") or 0):
                row["stable_passes"] = stable_passes
                index_dirty = True
            files[path] = row
            summary["unchanged"] += 1
            return

        try:
            item = _beta_parse_media(path, root_kind=root_kind, library_root=root_path)
            if root_kind == "movies":
                item["type"] = "movie"
                item["target"] = _wizard_source_target("movie")
            elif root_kind == "shows":
                item["type"] = "show"
                item["target"] = _wizard_source_target("show")
        except Exception:
            summary["errors"] += 1
            # Preserve a previously valid row on a transient parse failure.
            if row and not row.get("removed"):
                summary["unchanged"] += 1
            return

        is_new = not row or row.get("removed")
        is_reclassified = bool(content_same and not is_new)
        files[path] = {
            "path": path,
            "size_bytes": size_bytes,
            "mtime": mtime,
            "mtime_ns": mtime_ns,
            "root_kind": root_kind,
            "parser_version": BETA_LIBRARY_PARSER_VERSION,
            "item": item,
            "first_seen": float(row.get("first_seen") or now),
            "last_seen": now,
            "changed_at": float(row.get("changed_at") or row.get("first_seen") or now) if is_reclassified else now,
            "stable_passes": min(2, int(row.get("stable_passes") or 0) + 1) if is_reclassified else 0,
            "removed": False,
            "queued_at": row.get("queued_at") if isinstance(row, dict) else 0,
        }
        index_dirty = True
        summary["new" if is_new else ("reclassified" if is_reclassified else "changed")] += 1

    def preserve_cached_subtree(directory: str, root_kind: str, root_path: str) -> None:
        """Keep cached rows when a share or directory is temporarily unreadable."""
        stack = [directory]
        preserved: set[str] = set()
        while stack:
            current = stack.pop()
            if current in preserved:
                continue
            preserved.add(current)
            seen_directories.add(current)
            record = directories.get(current) if isinstance(directories.get(current), dict) else {}
            current_kind = str(record.get("root_kind") or root_kind)
            current_root = str(record.get("root_path") or root_path)
            for path in files_by_directory.get(current, []):
                accept_cached(path, current_kind, current_root)
            stack.extend(
                child for child in (record.get("children") or [])
                if isinstance(child, str) and child not in preserved
            )

    for root in _beta_mapped_roots(settings):
        root_path = str(root.get("path") or "").strip()
        root_kind = str(root.get("kind") or "")
        if not root_path or not is_allowed_path(root_path):
            continue
        root_path = os.path.normpath(root_path)
        try:
            root_is_directory = os.path.isdir(root_path)
        except OSError:
            root_is_directory = False
        if not root_is_directory:
            summary["unavailable_roots"] += 1
            summary["errors"] += 1
            unavailable_paths.add(root_path)
            preserve_cached_subtree(root_path, root_kind, root_path)
            continue

        stack = [root_path]
        while stack:
            root_dir = stack.pop()
            if root_dir in seen_directories:
                continue
            try:
                before_stat = os.stat(root_dir)
                before_mtime_ns = int(before_stat.st_mtime_ns)
            except OSError:
                summary["errors"] += 1
                unavailable_paths.add(root_dir)
                preserve_cached_subtree(root_dir, root_kind, root_path)
                continue

            record = directories.get(root_dir) if isinstance(directories.get(root_dir), dict) else {}
            can_reuse = bool(
                not full_scan
                and record
                and int(record.get("mtime_ns") or -1) == before_mtime_ns
                and str(record.get("root_kind") or "") == root_kind
                and str(record.get("root_path") or "") == root_path
            )
            seen_directories.add(root_dir)
            if can_reuse:
                summary["directories_cached"] += 1
                for path in files_by_directory.get(root_dir, []):
                    accept_cached(path, root_kind, root_path)
                stack.extend(
                    child for child in (record.get("children") or [])
                    if isinstance(child, str) and child not in seen_directories
                )
                continue

            summary["directories_scanned"] += 1
            children = []
            try:
                with os.scandir(root_dir) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                children.append(entry.path)
                                continue
                            if not entry.name.lower().endswith(VIDEO_EXTS) or not entry.is_file():
                                continue
                            if os.path.splitext(entry.name)[0].lower().endswith("-tsd"):
                                summary["skipped_tsd"] += 1
                                continue
                            stat_result = entry.stat()
                            summary["file_stats"] += 1
                            process_file(entry.path, stat_result, root_kind, root_path)
                        except OSError:
                            summary["errors"] += 1
                            unavailable_paths.add(entry.path)
                            # Do not turn one transient SMB metadata failure
                            # into a removal. Keep whichever indexed subtree or
                            # file row was last known to be valid.
                            if entry.path in directories:
                                preserve_cached_subtree(entry.path, root_kind, root_path)
                            elif entry.path in files:
                                accept_cached(entry.path, root_kind, root_path)
            except OSError:
                summary["errors"] += 1
                unavailable_paths.add(root_dir)
                preserve_cached_subtree(root_dir, root_kind, root_path)
                continue

            try:
                after_mtime_ns = int(os.stat(root_dir).st_mtime_ns)
            except OSError:
                after_mtime_ns = -1
            # A directory that changed while it was being enumerated is forced
            # through the next scan again so an in-flight copy cannot be missed.
            stored_mtime_ns = after_mtime_ns if after_mtime_ns == before_mtime_ns else -1
            new_record = {
                "mtime_ns": stored_mtime_ns,
                "root_kind": root_kind,
                "root_path": root_path,
                "children": children,
            }
            if record != new_record:
                directories[root_dir] = new_record
                index_dirty = True
            stack.extend(child for child in children if child not in seen_directories)

    for path, row in list(files.items()):
        if path in seen_paths or not isinstance(row, dict) or row.get("removed"):
            continue
        row["removed"] = True
        row["removed_at"] = now
        files[path] = row
        index_dirty = True
        summary["removed"] += 1

    for directory in list(directories):
        if directory not in seen_directories:
            directories.pop(directory, None)
            index_dirty = True

    normalized_unavailable = sorted(unavailable_paths)
    if index.get("unavailable_paths") != normalized_unavailable:
        index["unavailable_paths"] = normalized_unavailable
        index_dirty = True

    if full_scan:
        index["last_full_scan_at"] = now
        index_dirty = True
    if index_dirty:
        _beta_save_scan_index(index)
    summary["index_saved"] = bool(index_dirty)
    summary["duration_ms"] = int((time.monotonic() - started) * 1000)
    return index, summary


def _beta_queue_stable_tracked_episodes(data: dict, index: dict, settings: dict) -> dict:
    result = {
        "queued": 0,
        "smart_planned": 0,
        "skipped_unstable": 0,
        "skipped_active": 0,
        "skipped_missing_mapping": 0,
        "skipped": [],
    }
    if not settings.get("beta_auto_scan_auto_queue_tracked", True):
        return result
    now = time.time()
    planning_requests = []
    with BETA_TRACKING_LOCK:
        tracking = _beta_load_tracking()
        tracked_rows = tracking.get("shows") if isinstance(tracking.get("shows"), dict) else {}
        index_files = index.get("files") if isinstance(index.get("files"), dict) else {}
        active_paths = _beta_active_job_paths()
        changed_tracking = False

        for show in data.get("shows") or []:
            if not isinstance(show, dict):
                continue
            show_id = _beta_show_tracking_key(show)
            row = tracked_rows.get(show_id)
            if (
                not isinstance(row, dict)
                or not row.get("tracked")
                or not row.get("auto_queue", True)
            ):
                continue

            known = set(_beta_clean_path_list(row.get("known_paths")))
            current_show_paths = _beta_show_paths(show)
            if _beta_clean_path_list(row.get("tracked_paths")) != current_show_paths:
                row["tracked_paths"] = current_show_paths
                changed_tracking = True
            if _beta_show_plan_needs_refresh(row, current_show_paths):
                planning_requests.append((show_id, current_show_paths))
            ready_paths = []
            newly_known = set()
            for ep in show.get("files") or []:
                if not isinstance(ep, dict):
                    continue
                path = str(ep.get("path") or "")
                if not path or path in known:
                    continue
                idx_row = index_files.get(path) if isinstance(index_files.get(path), dict) else {}
                if path in active_paths:
                    newly_known.add(path)
                    result["skipped_active"] += 1
                    continue
                if not _beta_file_is_stable(idx_row, settings, now):
                    result["skipped_unstable"] += 1
                    continue
                ready_paths.append(path)

            if ready_paths:
                queued = _beta_queue_tracked_episode_paths(
                    row,
                    ready_paths,
                    automation_source="tracked_show_auto_scan",
                )
                result["queued"] += int(queued.get("queued_count") or 0)
                result["smart_planned"] += int(queued.get("planned_count") or 0)
                result["skipped"].extend(queued.get("skipped") or [])
                newly_known.update(queued.get("known_paths") or [])
                for path in queued.get("queued_paths") or []:
                    if isinstance(index_files.get(path), dict):
                        index_files[path]["queued_at"] = now
                changed_tracking = True

            if newly_known:
                row["known_paths"] = sorted(set(_beta_clean_path_list(row.get("known_paths"))) | newly_known)
                row["updated_at"] = now
                tracked_rows[show_id] = row
                changed_tracking = True

        if changed_tracking:
            tracking["shows"] = tracked_rows
            _beta_save_tracking(tracking)
            _beta_save_scan_index(index)
    for planning_show_id, planning_paths in planning_requests:
        _beta_schedule_tracked_show_planning(planning_show_id, planning_paths)
    return result


def _autopilot_schedule_open(settings: dict, now: float | None = None) -> bool:
    now_struct = time.localtime(now or time.time())
    minute = now_struct.tm_hour * 60 + now_struct.tm_min

    def parse(value: str, fallback: int) -> int:
        try:
            hour, part = str(value or "").split(":", 1)
            return max(0, min(1439, int(hour) * 60 + int(part)))
        except (TypeError, ValueError):
            return fallback

    start = parse(settings.get("autopilot_schedule_start"), 0)
    end = parse(settings.get("autopilot_schedule_end"), 1439)
    if start <= end:
        return start <= minute <= end
    return minute >= start or minute <= end


def _autopilot_active_job_count() -> int:
    active_states = {"queued", "running", "waiting_to_upload"}
    return sum(1 for job in list_jobs_for_api() if str(job.get("status") or "").lower() in active_states)


def _autopilot_candidates(data: dict, index: dict, settings: dict) -> dict:
    """Explain and, in manage mode, apply one bounded Autopilot decision."""
    now = time.time()
    enabled = bool(settings.get("autopilot_enabled", False))
    mode = str(settings.get("autopilot_mode") or "observe").strip().lower()
    if mode not in {"observe", "manage"}:
        mode = "observe"
    schedule_open = _autopilot_schedule_open(settings, now)
    min_bytes = int(float(settings.get("autopilot_min_size_gb") or 2.0) * (1024 ** 3))
    min_savings = float(settings.get("autopilot_min_savings_percent") or 0.0) / 100.0
    active_paths = _beta_active_job_paths()
    active_jobs = _autopilot_active_job_count()
    max_active = max(1, int(settings.get("autopilot_max_active_jobs") or 5))
    batch_limit = max(1, int(settings.get("autopilot_batch_limit") or 3))
    capacity = max(0, max_active - active_jobs)
    index_files = index.get("files") if isinstance(index.get("files"), dict) else {}
    unavailable_prefixes = []
    for value in index.get("unavailable_paths") or []:
        if not value:
            continue
        normalized = os.path.normcase(os.path.normpath(str(value)))
        unavailable_prefixes.append(normalized)

    def path_is_temporarily_unavailable(path: str) -> bool:
        normalized = os.path.normcase(os.path.normpath(path))
        for prefix in unavailable_prefixes:
            if normalized == prefix or normalized.startswith(prefix.rstrip(os.sep) + os.sep):
                return True
        return False

    decisions = []
    eligible = []
    smart_learning = smart_learning_status()
    smart_ready = bool(smart_learning.get("automation_ready"))

    items = []
    for movie in data.get("movies") or []:
        if isinstance(movie, dict):
            items.append(("movie", movie.get("title") or os.path.basename(str(movie.get("path") or "")), movie))
    for show in data.get("shows") or []:
        if not isinstance(show, dict):
            continue
        for episode in show.get("files") or []:
            if isinstance(episode, dict):
                label = f"{show.get('title') or 'Show'} · {episode.get('filename') or os.path.basename(str(episode.get('path') or ''))}"
                items.append(("show", label, episode))

    for media_type, label, item in items:
        path = str(item.get("path") or "")
        size_bytes = int(item.get("size_bytes") or 0)
        idx_row = index_files.get(path) if isinstance(index_files.get(path), dict) else {}
        prediction = item.get("prediction") if isinstance(item.get("prediction"), dict) else {}
        saved_ratio = float(prediction.get("saved_ratio") or 0.0) if prediction.get("available") else None
        reason = "Eligible: stable, allowed, and within the configured policy."
        decision = "eligible"

        if not path or not is_allowed_path(path) or not idx_row or idx_row.get("removed"):
            decision, reason = "skip", "File is missing or outside an allowed media root."
        elif path_is_temporarily_unavailable(path):
            decision, reason = "wait", "The library path is temporarily unavailable; the next scan will verify it again."
        elif media_type == "movie" and not settings.get("autopilot_include_movies", True):
            decision, reason = "skip", "Movies are disabled in this Autopilot policy."
        elif media_type == "show" and not settings.get("autopilot_include_shows", False):
            decision, reason = "skip", "TV episodes are disabled in this Autopilot policy."
        elif size_bytes < min_bytes:
            decision, reason = "skip", "Source is below the configured minimum size."
        elif path in active_paths:
            decision, reason = "skip", "A queue or encode job already owns this file."
        elif float(idx_row.get("queued_at") or 0) > 0:
            decision, reason = "skip", "This indexed file was already queued by automation."
        elif not _beta_file_is_stable(idx_row, settings, now):
            decision, reason = "wait", "File is still inside the write-stability window."
        elif saved_ratio is not None and saved_ratio < min_savings:
            decision, reason = "skip", "History predicts savings below the configured threshold."
        elif saved_ratio is None:
            reason = "Eligible by safety rules; encode-history confidence is not available yet."

        row = {
            "path": path,
            "label": str(label or os.path.basename(path))[:180],
            "media_type": media_type,
            "size_bytes": size_bytes,
            "decision": decision,
            "reason": reason,
            "preset": "smart" if smart_ready else guess_preset_from_filename(os.path.basename(path)),
            "predicted_saved_bytes": int(prediction.get("estimated_saved_bytes") or 0),
            "prediction_confidence": prediction.get("confidence") or ("unknown" if not prediction.get("available") else "low"),
        }
        decisions.append(row)
        if decision == "eligible":
            eligible.append(row)

    eligible.sort(key=lambda row: (int(row.get("predicted_saved_bytes") or 0), int(row.get("size_bytes") or 0)), reverse=True)
    selected = eligible[: min(batch_limit, capacity)]
    should_queue = enabled and mode == "manage" and schedule_open and smart_ready and bool(selected)
    queued = 0
    if should_queue:
        for row in selected:
            try:
                _job_id, recommendation = _create_smart_job(
                    row["path"],
                    require_automation_ready=True,
                    automation_source="autopilot",
                )
                queued += 1
                row["smart_candidate_id"] = recommendation.get("recommended_id")
                row["reason"] = f"{row['reason']} Learned Smart Preset selected {recommendation.get('recommended_id')}."
            except Exception as exc:
                row["decision"] = "error"
                row["reason"] = f"Smart Preset planning failed; file was not queued: {exc}"
        for row in selected:
            if row.get("decision") != "error" and isinstance(index_files.get(row["path"]), dict):
                index_files[row["path"]]["queued_at"] = now
                index_files[row["path"]]["autopilot_reason"] = row["reason"]
        _beta_save_scan_index(index)

    return {
        "enabled": enabled,
        "mode": mode,
        "schedule_open": schedule_open,
        "active_jobs": active_jobs,
        "max_active_jobs": max_active,
        "capacity": capacity,
        "considered": len(items),
        "eligible": len(eligible),
        "selected": len(selected),
        "queued": queued,
        "estimated_selected_savings_bytes": sum(int(row.get("predicted_saved_bytes") or 0) for row in selected),
        "smart_presets": smart_learning,
        "decisions": sorted(decisions, key=lambda row: (row["decision"] != "eligible", -int(row.get("size_bytes") or 0)))[:50],
    }


def _autopilot_readiness(settings: dict | None = None) -> dict:
    settings = settings or load_settings()
    roots = _beta_mapped_roots(settings)
    valid_roots = [row for row in roots if row.get("path") and os.path.isdir(row["path"]) and is_allowed_path(row["path"])]
    nodes = list_nodes_public()
    recommendations = []
    checks = []

    def add(key: str, ok: bool, label: str, detail: str, *, required: bool = False):
        checks.append({"key": key, "ok": bool(ok), "label": label, "detail": detail, "required": required})
        if not ok:
            recommendations.append(detail)

    add("library", bool(valid_roots), "Library folders", f"{len(valid_roots)} accessible media folder(s)." if valid_roots else "Map at least one accessible Movies or Shows folder.", required=True)
    add("data", os.path.isdir(DATA_DIR) and os.access(DATA_DIR, os.W_OK), "Durable data", "Automation state storage is writable." if os.path.isdir(DATA_DIR) and os.access(DATA_DIR, os.W_OK) else "Make the app data directory writable so decisions can be recovered.", required=True)
    add("preset", bool(list_preset_files()), "Encode presets", "At least one HandBrake preset is available." if list_preset_files() else "Install or configure a HandBrake preset before enabling manage mode.", required=True)
    learning = smart_learning_status()
    add(
        "learning",
        bool(learning.get("automation_ready")),
        "Preference learning",
        learning.get("message") or "Approve accurate previews or queue Size Wizard plans before enabling Manage mode.",
        required=True,
    )
    add("stability", bool(settings.get("beta_auto_scan_file_stability_enabled", True)), "Write protection", "New files must become stable before they can be queued." if settings.get("beta_auto_scan_file_stability_enabled", True) else "Enable the file-stability window to avoid encoding files that are still being copied.")
    add("hardware", bool(settings.get("qsv_device_available", False)), "Hardware acceleration", "Intel QSV is configured." if settings.get("qsv_device_available", False) else "Optional: configure Intel QSV for faster unattended encoding.")
    add("nodes", any(node.get("online") for node in nodes) if nodes else True, "Worker network", f"{sum(1 for node in nodes if node.get('online'))} of {len(nodes)} linked worker(s) online." if nodes else "Standalone mode is ready; link workers later for more capacity.")

    required = [check for check in checks if check.get("required")]
    ready = all(check["ok"] for check in required)
    score = round(100 * sum(1 for check in checks if check["ok"]) / max(1, len(checks)))
    return {"ready": ready, "score": score, "checks": checks, "recommendations": recommendations[:6]}


def _autopilot_completed_feedback_jobs(limit: int = 24) -> list[dict]:
    """Completed learned jobs that can improve future automatic choices."""
    rows = []
    for job in list_jobs_for_api():
        context = job.get("smart_feedback_context")
        if (
            job.get("status") != "done"
            or not job.get("smart_preset")
            or not isinstance(context, dict)
            or str(job.get("automation_source") or "") not in {"autopilot", "smart_preset", "size_wizard"}
        ):
            continue
        src = str(job.get("src") or "")
        feedback = job.get("quality_feedback") if isinstance(job.get("quality_feedback"), dict) else None
        rows.append(
            {
                "id": job.get("id"),
                "title": _beta_clean_title(os.path.basename(src)),
                "src": src,
                "preset": job.get("preset"),
                "candidate_id": job.get("smart_candidate_id") or "smart",
                "encoder": job.get("encoder") or job.get("encode_method") or "Smart Preset",
                "saved_bytes": int(job.get("saved_bytes") or 0),
                "finished_at": job.get("finished_at"),
                "feedback": feedback,
                "needs_feedback": not bool(feedback),
            }
        )
    rows.sort(key=lambda row: float(row.get("finished_at") or 0), reverse=True)
    return rows[: max(1, min(100, int(limit or 24)))]


def _autopilot_status_payload(*, compact: bool = False) -> dict:
    settings = load_settings()
    scan = _beta_load_autoscan_status(settings)
    autopilot = (scan.get("last_summary") or {}).get("autopilot")
    if not isinstance(autopilot, dict):
        active_jobs = _autopilot_active_job_count()
        max_active = max(1, int(settings.get("autopilot_max_active_jobs") or 5))
        autopilot = {
            "enabled": bool(settings.get("autopilot_enabled", False)),
            "mode": settings.get("autopilot_mode") or "observe",
            "eligible": 0,
            "queued": 0,
            "decisions": [],
            "schedule_open": _autopilot_schedule_open(settings),
            "active_jobs": active_jobs,
            "max_active_jobs": max_active,
            "capacity": max(0, max_active - active_jobs),
            "estimated_selected_savings_bytes": 0,
        }
    feedback_jobs = _autopilot_completed_feedback_jobs()
    continuous_learning_enabled = bool(settings.get("autopilot_continuous_learning_enabled", True))
    readiness = _autopilot_readiness(settings)
    if compact:
        compact_autopilot = {
            key: autopilot.get(key)
            for key in (
                "enabled", "mode", "schedule_open", "eligible", "queued",
                "active_jobs", "max_active_jobs", "capacity",
                "estimated_selected_savings_bytes",
            )
            if key in autopilot
        }
        return {
            "release": APP_RELEASE,
            "autopilot": compact_autopilot,
            "readiness": readiness,
            "continuous_learning": {
                "enabled": continuous_learning_enabled,
                "pending": sum(1 for row in feedback_jobs if row.get("needs_feedback")) if continuous_learning_enabled else 0,
            },
        }

    smart_state = public_smart_preset_state()
    return {
        "release": APP_RELEASE,
        "autopilot": autopilot,
        "readiness": readiness,
        "scan": scan,
        "queue": {"paused": get_queue_state(), "summary": get_job_summary()},
        "nodes": list_nodes_public(),
        "review": _autopilot_review_summary(),
        "continuous_learning": {
            "enabled": continuous_learning_enabled,
            "pending": sum(1 for row in feedback_jobs if row.get("needs_feedback")) if continuous_learning_enabled else 0,
            "jobs": feedback_jobs if continuous_learning_enabled else [],
        },
        "onboarding": {
            "tour_completed": bool(settings.get("autopilot_tour_completed", False)),
            "training_target": int((smart_state.get("profile") or {}).get("minimum_feedback") or 2),
        },
        "guide": {
            "steps": [
                {"id": "folders", "title": "Map your library", "detail": "Choose the exact Movies and Shows folders ByteSqueeze may watch."},
                {"id": "observe", "title": "Start in Observe", "detail": "Run a cycle and review every eligible, waiting, and skipped decision without queueing anything."},
                {"id": "preview", "title": "Teach Smart Presets", "detail": "Approve or reject a few Size Wizard previews so automation learns your quality and size comfort zone."},
                {"id": "limits", "title": "Set guardrails", "detail": "Choose a file-stability window, minimum savings, schedule, and queue capacity."},
                {"id": "manage", "title": "Turn on Manage", "detail": "Only then may Autopilot queue stable work inside the limits you selected."},
            ],
            "safety": "Observe never queues. Manage never bypasses file stability, output verification, or source-file protection.",
        },
        "policy_profiles": [
            {"id": "safe", "name": "Safe starter", "description": "Observe only with a 20-minute write window and small batches.", "settings": {"autopilot_enabled": True, "autopilot_mode": "observe", "autopilot_min_size_gb": 2, "autopilot_min_savings_percent": 15, "autopilot_batch_limit": 2, "autopilot_max_active_jobs": 3, "beta_auto_scan_enabled": True, "beta_auto_scan_file_stability_enabled": True, "beta_auto_scan_file_stability_minutes": 20}},
            {"id": "balanced", "name": "Balanced", "description": "Observe first, moderate savings threshold, and up to five active jobs.", "settings": {"autopilot_enabled": True, "autopilot_mode": "observe", "autopilot_min_size_gb": 1, "autopilot_min_savings_percent": 10, "autopilot_batch_limit": 3, "autopilot_max_active_jobs": 5, "beta_auto_scan_enabled": True, "beta_auto_scan_file_stability_enabled": True, "beta_auto_scan_file_stability_minutes": 10}},
            {"id": "hands_off", "name": "Hands-off", "description": "Manage mode for users who have already reviewed Smart Preset previews.", "settings": {"autopilot_enabled": True, "autopilot_mode": "manage", "autopilot_min_size_gb": 0.5, "autopilot_min_savings_percent": 8, "autopilot_batch_limit": 5, "autopilot_max_active_jobs": 8, "beta_auto_scan_enabled": True, "beta_auto_scan_file_stability_enabled": True, "beta_auto_scan_file_stability_minutes": 15}},
        ],
    }


def _beta_calendar_payload(data: dict, *, tracked_only: bool = False, days: int = 120) -> dict:
    calendar = data.get("release_calendar") if isinstance(data.get("release_calendar"), dict) else {}
    episodes = [row.copy() for row in calendar.get("episodes") or [] if isinstance(row, dict)]
    if tracked_only:
        episodes = [row for row in episodes if row.get("tracked") and row.get("monitor_releases", True)]
    today = datetime.now().date()
    cutoff = today + timedelta(days=max(1, min(730, int(days or 120))))
    filtered = []
    for row in episodes:
        try:
            airdate = datetime.strptime(str(row.get("airdate") or ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        if today <= airdate <= cutoff:
            filtered.append(row)
    grouped = {}
    for row in filtered:
        grouped.setdefault(str(row.get("airdate") or ""), []).append(row)
    return {
        "generated_at": calendar.get("generated_at") or 0,
        "provider": calendar.get("provider") or {"name": "TVmaze", "url": "https://www.tvmaze.com/"},
        "tracked_only": tracked_only,
        "days": [{"date": date, "episodes": rows} for date, rows in sorted(grouped.items())],
        "episodes": filtered,
        "count": len(filtered),
    }


def _absolute_media_urls(value, base_url: str):
    """Make cached artwork routes usable by a paired phone."""
    if isinstance(value, list):
        return [_absolute_media_urls(row, base_url) for row in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, row in value.items():
        if key in {"poster_url", "image_url"} and isinstance(row, str) and row.startswith("/"):
            out[key] = f"{base_url.rstrip('/')}{row}"
        else:
            out[key] = _absolute_media_urls(row, base_url)
    return out


def _beta_encoding_is_running() -> bool:
    try:
        summary = get_job_summary()
        counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
        return int(counts.get("running") or 0) > 0
    except Exception:
        return False


def _beta_autoscan_event_summary(summary: dict) -> dict:
    """Keep the activity feed useful without duplicating full decision reports."""
    summary = summary if isinstance(summary, dict) else {}
    compact = {
        key: summary.get(key)
        for key in (
            "mode", "duration_ms", "directories_scanned", "directories_cached",
            "file_stats", "catalog_rebuilt", "scanned", "new", "changed",
            "removed", "unchanged", "skipped_tsd", "unavailable_roots",
            "queue_queued", "queue_skipped_unstable", "queue_skipped_active",
            "queue_skipped_missing_mapping", "error",
        )
        if key in summary
    }
    autopilot = summary.get("autopilot") if isinstance(summary.get("autopilot"), dict) else {}
    if autopilot:
        compact["autopilot"] = {
            key: autopilot.get(key)
            for key in (
                "enabled", "mode", "schedule_open", "considered", "eligible",
                "selected", "queued", "active_jobs", "max_active_jobs",
                "estimated_selected_savings_bytes",
            )
            if key in autopilot
        }
    return compact


def _beta_run_incremental_auto_scan(*, reason: str = "timer", force: bool = False) -> dict:
    if not BETA_AUTOSCAN_LOCK.acquire(blocking=False):
        try:
            status = _beta_load_autoscan_status(load_settings())
        except Exception:
            status = _beta_empty_autoscan_status()
        status["running"] = True
        status["last_status"] = "running"
        return status
    started = time.time()
    settings = {}
    interval_seconds = 30 * 60
    status = _beta_empty_autoscan_status()
    try:
        settings = load_settings()
        interval_seconds = _beta_autoscan_interval_seconds(settings)
        status = _beta_load_autoscan_status(settings)
        status.update({
            "running": True,
            "last_started_at": started,
            "last_status": "running",
            "last_message": "Auto scan running.",
            "last_reason": str(reason or "timer"),
        })
        _beta_save_autoscan_status(status)

        if not force and not (settings.get("beta_auto_scan_enabled", False) or settings.get("autopilot_enabled", False)):
            status.update({
                "running": False,
                "last_finished_at": time.time(),
                "last_status": "disabled",
                "last_message": "Auto scan disabled.",
                "next_scan_at": time.time() + interval_seconds,
            })
            return _beta_save_autoscan_status(status)

        if not force and settings.get("beta_auto_scan_skip_while_encoding", True) and _beta_encoding_is_running():
            summary = {"skipped": "encoder busy"}
            message = "Auto scan skipped, encoder busy."
            log_event("beta_auto_scan_skipped", message, level="info", extra=summary)
            status.update({
                "running": False,
                "last_finished_at": time.time(),
                "last_status": "skipped",
                "last_message": message,
                "last_summary": summary,
                # Recheck soon after an active encode instead of sleeping for
                # the entire configured interval again.
                "next_scan_at": time.time() + min(60, interval_seconds),
            })
            return _beta_save_autoscan_status(status)

        index, scan_summary = _beta_update_scan_index(settings)
        catalog_changed = any(
            int(scan_summary.get(key) or 0) > 0
            for key in ("new", "changed", "reclassified", "removed")
        )
        cache_available = os.path.isfile(BETA_LIBRARY_CACHE_FILE)
        if catalog_changed or not cache_available:
            data = _beta_library_from_scan_index(index, settings=settings, recursive=True)
            data = _beta_enrich_metadata(data, settings, enabled=True)
            data = _beta_refresh_predictions(data)
            scan_summary["catalog_rebuilt"] = True
        else:
            # The expensive catalog grouping, artwork providers, and prediction
            # pass are already represented by the saved cache. Reuse it when
            # the filesystem index is unchanged.
            data = _beta_load_library_cache(settings)
            scan_summary["catalog_rebuilt"] = False
        tracking = _beta_load_tracking()
        data = _beta_apply_tracking(data, tracking)
        queue_summary = _beta_queue_stable_tracked_episodes(data, index, settings)
        tracking = _beta_load_tracking()
        data = _beta_apply_tracking(data, tracking)
        data.setdefault("tracking", {})["auto_queue"] = {
            "queued_count": queue_summary.get("queued", 0),
            "skipped_unstable": queue_summary.get("skipped_unstable", 0),
            "skipped_active": queue_summary.get("skipped_active", 0),
        }
        autopilot_summary = _autopilot_candidates(data, index, settings)
        data["autopilot"] = autopilot_summary
        if catalog_changed or not cache_available:
            data = _beta_stamp_library_scan(data)
            _beta_save_library_cache(data)

        summary = {**scan_summary, **{f"queue_{k}": v for k, v in queue_summary.items()}, "autopilot": autopilot_summary}
        message = (
            f"Auto scan complete: {scan_summary['scanned']} scanned, "
            f"{scan_summary['new'] + scan_summary['changed'] + scan_summary.get('reclassified', 0)} updated, "
            f"{scan_summary['removed']} removed, "
            f"{queue_summary.get('queued', 0) + autopilot_summary.get('queued', 0)} queued."
        )
        log_event("beta_auto_scan", message, level="info", extra=_beta_autoscan_event_summary(summary))
        status.update({
            "running": False,
            "last_finished_at": time.time(),
            "last_status": "ok",
            "last_message": message,
            "last_summary": summary,
            "next_scan_at": time.time() + interval_seconds,
        })
        return _beta_save_autoscan_status(status)
    except Exception as e:
        summary = {"error": str(e)[:240]}
        message = f"Auto scan failed: {str(e)[:180]}"
        try:
            log_event("beta_auto_scan_error", message, level="error", extra=summary)
        except Exception as log_error:
            print(f"[WARN] Could not save auto-scan error event: {log_error}", flush=True)
        status.update({
            "running": False,
            "last_finished_at": time.time(),
            "last_status": "error",
            "last_message": message,
            "last_summary": summary,
            "next_scan_at": time.time() + interval_seconds,
        })
        return _beta_save_autoscan_status(status)
    finally:
        BETA_AUTOSCAN_LOCK.release()


def _beta_autoscan_loop() -> None:
    BETA_AUTOSCAN_HEALTH.update({
        "running": True,
        "started_at": time.time(),
        "last_error": "",
    })
    try:
        while not BETA_AUTOSCAN_STOP.is_set():
            try:
                now = time.time()
                BETA_AUTOSCAN_HEALTH["last_cycle_at"] = now
                settings = load_settings()
                interval_seconds = _beta_autoscan_interval_seconds(settings)
                status = _beta_load_autoscan_status(settings)
                try:
                    next_scan_at = float(status.get("next_scan_at") or 0)
                    has_completed_scan = bool(float(status.get("last_finished_at") or 0))
                except (TypeError, ValueError):
                    next_scan_at = 0
                    has_completed_scan = False

                automation_enabled = bool(
                    settings.get("beta_auto_scan_enabled", False)
                    or settings.get("autopilot_enabled", False)
                )
                if automation_enabled and (not has_completed_scan or now >= next_scan_at):
                    _beta_run_incremental_auto_scan(reason="timer", force=False)
                    BETA_AUTOSCAN_HEALTH["last_success_at"] = time.time()
                    BETA_AUTOSCAN_HEALTH["last_error"] = ""
                    continue

                wait_seconds = 30
                if automation_enabled and next_scan_at > now:
                    wait_seconds = max(5, min(30, int(next_scan_at - now)))
                if BETA_AUTOSCAN_WAKE.wait(timeout=wait_seconds):
                    BETA_AUTOSCAN_WAKE.clear()
                    continue
                BETA_AUTOSCAN_HEALTH["last_success_at"] = time.time()
                BETA_AUTOSCAN_HEALTH["last_error"] = ""
            except Exception as exc:
                BETA_AUTOSCAN_HEALTH["cycle_errors"] = int(
                    BETA_AUTOSCAN_HEALTH.get("cycle_errors") or 0
                ) + 1
                BETA_AUTOSCAN_HEALTH["last_error"] = str(exc)[:240]
                try:
                    log_event(
                        "beta_auto_scan_scheduler_error",
                        f"Auto-scan scheduler recovered from an error: {str(exc)[:180]}",
                        level="warn",
                    )
                except Exception:
                    print(f"[WARN] Auto-scan scheduler recovered from an error: {exc}", flush=True)
                BETA_AUTOSCAN_STOP.wait(5)
    finally:
        BETA_AUTOSCAN_HEALTH["running"] = False


def _start_beta_autoscan_thread(*, force_serving_process: bool = False) -> bool:
    global BETA_AUTOSCAN_THREAD
    if os.environ.get("TSD_DISABLE_BETA_AUTOSCAN") == "1":
        return False
    if (
        not force_serving_process
        and os.environ.get("FLASK_DEBUG") == "1"
        and os.environ.get("WERKZEUG_RUN_MAIN") != "true"
    ):
        return False
    with BETA_AUTOSCAN_THREAD_LOCK:
        if BETA_AUTOSCAN_THREAD and BETA_AUTOSCAN_THREAD.is_alive():
            return False
        restarted = BETA_AUTOSCAN_THREAD is not None
        BETA_AUTOSCAN_STOP.clear()
        BETA_AUTOSCAN_THREAD = threading.Thread(
            target=_beta_autoscan_loop,
            name="beta-auto-scan",
            daemon=True,
        )
        BETA_AUTOSCAN_THREAD.start()
    try:
        log_event(
            "beta_auto_scan_scheduler_started",
            "Automatic library scan scheduler is running.",
            extra={"restarted": restarted},
        )
    except Exception:
        pass
    return True


def _ensure_beta_autoscan_scheduler() -> bool:
    """Restart a missing scan coordinator and wake an overdue schedule."""
    try:
        settings = load_settings()
    except Exception:
        settings = {}
    enabled = bool(
        settings.get("beta_auto_scan_enabled", False)
        or settings.get("autopilot_enabled", False)
    )
    started = _start_beta_autoscan_thread(force_serving_process=True)
    if enabled:
        status = _beta_load_autoscan_status(settings)
        try:
            overdue = (
                not float(status.get("last_finished_at") or 0)
                or time.time() >= float(status.get("next_scan_at") or 0)
            )
        except (TypeError, ValueError):
            overdue = True
        if overdue:
            BETA_AUTOSCAN_WAKE.set()
    thread_alive = bool(BETA_AUTOSCAN_THREAD and BETA_AUTOSCAN_THREAD.is_alive())
    return bool(started or thread_alive)


def _node_summary_status(summary: dict) -> str:
    summary = summary if isinstance(summary, dict) else {}
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    if int(counts.get("running") or 0) > 0:
        return "running"
    if int(counts.get("waiting_to_upload") or 0) > 0:
        return "waiting_upload"
    if int(counts.get("queued") or 0) > 0:
        return "queued"
    return "idle"


def _latest_worker_job_error(worker_jobs: list) -> dict:
    error_jobs = [
        job for job in worker_jobs
        if isinstance(job, dict) and str(job.get("status") or "").lower() == "error"
    ]
    if not error_jobs:
        return {}

    def sort_key(job: dict) -> tuple[float, float]:
        try:
            finished = float(job.get("finished_at") or 0)
        except (TypeError, ValueError):
            finished = 0.0
        try:
            created = float(job.get("created_at") or 0)
        except (TypeError, ValueError):
            created = 0.0
        return finished, created

    job = max(error_jobs, key=sort_key)
    transfer = job.get("transfer") if isinstance(job.get("transfer"), dict) else {}
    message = str(
        job.get("error_message")
        or transfer.get("last_error")
        or transfer.get("error")
        or "Worker encode failed; open the worker log for details."
    ).strip()[:500]
    return {
        "id": str(job.get("id") or ""),
        "src": str(job.get("src") or ""),
        "message": message,
        "log_tail": str(job.get("log_tail") or "")[-12_000:],
        "finished_at": job.get("finished_at") or 0,
    }


def _smart_preset_public_payload() -> dict:
    payload = public_smart_preset_state()
    status = wizard_llm_status(load_settings())
    provider = str(status.get("provider") or "local")
    payload["scene_ai"] = {
        "beta": True,
        "provider": provider,
        "model": str(status.get("model") or "")[:100],
        "ready": bool(status.get("ready") and provider in {"openai", "gemini"}),
        "message": (
            f"{provider.title()} vision is ready for per-episode analysis."
            if status.get("ready") and provider in {"openai", "gemini"}
            else "Configure and select OpenAI or Gemini under AI & API Keys; deterministic episode planning remains active."
        ),
    }
    return payload


def _linked_worker_jobs_for_api() -> list[dict]:
    """Normalize cached worker jobs into the main Jobs API shape."""
    combined = []
    for node in list_nodes_public():
        node_id = str(node.get("id") or "").strip()
        node_name = str(node.get("name") or "Worker").strip()
        for source in node.get("jobs") if isinstance(node.get("jobs"), list) else []:
            if not isinstance(source, dict):
                continue
            worker_job_id = str(source.get("id") or "").strip()
            if not worker_job_id:
                continue
            item = source.copy()
            item.update({
                "id": f"worker:{node_id}:{worker_job_id}",
                "worker_job_id": worker_job_id,
                "controller_history_id": f"remote-{worker_job_id}",
                "node_id": node_id,
                "node_name": node_name,
                "mode": "linked_worker",
                "is_worker_job": True,
                "queue_position": source.get("queue_position"),
                "log_url": (
                    f"/api/nodes/{node_id}/jobs/{worker_job_id}/log"
                    if node_id and (source.get("has_log") or source.get("log_tail"))
                    else ""
                ),
            })
            combined.append(item)
    return combined


def _combined_jobs_for_api() -> list[dict]:
    """Return local/controller history plus live and failed worker jobs."""
    items = list_job_history_for_api()
    by_id = {str(item.get("id") or ""): item for item in items}
    for worker_item in _linked_worker_jobs_for_api():
        status = str(worker_item.get("status") or "").lower()
        history_item = by_id.get(str(worker_item.get("controller_history_id") or ""))
        if history_item and status == "done":
            # The transfer ledger owns durable size/savings data. Enrich that
            # row with the worker's exact preset/log identity instead of
            # displaying the same completed encode twice.
            history_item.update({
                "worker_job_id": worker_item.get("worker_job_id"),
                "node_id": worker_item.get("node_id") or history_item.get("node_id"),
                "node_name": worker_item.get("node_name") or history_item.get("node_name"),
                "is_worker_job": True,
                "preset_name": worker_item.get("preset_name") or history_item.get("preset_name") or "",
                "hardware_decode_mode": worker_item.get("hardware_decode_mode") or "",
                "hardware_decode_requested": worker_item.get("hardware_decode_requested") or "",
                "hardware_decode_active": worker_item.get("hardware_decode_active"),
                "hardware_decode_reason": worker_item.get("hardware_decode_reason") or "",
                "hardware_decode_preset_applied": bool(worker_item.get("hardware_decode_preset_applied")),
                "source_video": worker_item.get("source_video") or {},
                "target_resolution": worker_item.get("target_resolution") or {},
                "has_log": bool(worker_item.get("has_log") or worker_item.get("log_tail")),
                "log_url": worker_item.get("log_url") or "",
            })
            continue
        items.append(worker_item)

    active_order = {"running": 0, "waiting_to_upload": 1, "queued": 2}

    def sort_key(item: dict):
        status = str(item.get("status") or "").lower()
        if status in active_order:
            try:
                queue_position = int(item.get("queue_position") or 999999)
            except (TypeError, ValueError):
                queue_position = 999999
            return (active_order[status], queue_position, 0.0, str(item.get("id") or ""))
        try:
            timestamp = float(item.get("finished_at") or item.get("created_at") or 0.0)
        except (TypeError, ValueError):
            timestamp = 0.0
        return (3, 0, -timestamp, str(item.get("id") or ""))

    items.sort(key=sort_key)
    return items


def _combined_job_summary() -> dict:
    """Add worker active/error state to controller-owned lifetime totals."""
    summary = get_job_summary()
    counts = dict(summary.get("counts") or {})
    worker_jobs = _linked_worker_jobs_for_api()
    active_states = {"queued", "running", "waiting_to_upload"}
    for state in active_states:
        counts[state] = int(counts.get(state) or 0) + sum(
            1 for item in worker_jobs if str(item.get("status") or "").lower() == state
        )
    worker_errors = sum(
        1 for item in worker_jobs if str(item.get("status") or "").lower() == "error"
    )
    counts["error"] = int(counts.get("error") or 0) + worker_errors
    summary["counts"] = counts
    summary["queued_count"] = int(summary.get("queued_count") or 0) + sum(
        1 for item in worker_jobs if str(item.get("status") or "").lower() == "queued"
    )
    running_ids = list(summary.get("running_job_ids") or [])
    running_ids.extend(
        str(item.get("id") or "")
        for item in worker_jobs
        if str(item.get("status") or "").lower() == "running"
    )
    summary["running_job_ids"] = running_ids
    summary["running_job_id"] = running_ids[0] if running_ids else None
    summary["active_error_count"] = int(summary.get("active_error_count") or 0) + worker_errors
    summary["worker_job_count"] = len(worker_jobs)
    return summary


def _clear_linked_worker_jobs(target: str = "finished") -> dict:
    """Clear terminal worker rows through the authenticated node channel."""
    removed = 0
    results = []
    for row in list_nodes_private():
        node_id = str(row.get("id") or "")
        node_name = str(row.get("name") or node_id or "Worker")
        try:
            result = signed_json_request(
                row,
                "/api/node/jobs/clear",
                method="POST",
                body={"target": target},
                timeout=20,
            )
            count = max(0, int(result.get("removed") or 0))
            removed += count
            if isinstance(result.get("jobs"), list):
                row["jobs"] = result["jobs"]
            if isinstance(result.get("summary"), dict):
                row["summary"] = result["summary"]
                row["status"] = _node_summary_status(result["summary"])
            row["last_error"] = ""
            save_node(row)
            results.append({"node_id": node_id, "node_name": node_name, "removed": count, "ok": True})
        except Exception as exc:
            results.append({
                "node_id": node_id,
                "node_name": node_name,
                "removed": 0,
                "ok": False,
                "error": str(exc)[:240],
            })
    return {"removed": removed, "workers": results}


def _refresh_linked_node(row: dict, *, allow_recovery: bool = True) -> dict:
    row = row.copy()
    try:
        data = signed_json_request(row, "/api/node/status", method="GET", timeout=12)
        if not str(row.get("recovery_token") or "").strip():
            try:
                recovery = signed_json_request(row, "/api/node/pair/enable-recovery", method="POST", body={}, timeout=8)
                if recovery.get("recovery_token"):
                    row["recovery_token"] = str(recovery.get("recovery_token"))
            except Exception:
                # Older workers can continue with their durable session token;
                # recovery is enabled automatically after both sides upgrade.
                pass
        summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
        local_controller_id = str(local_node_overview().get("id") or "")
        paired_controllers = data.get("paired_controllers") if isinstance(data.get("paired_controllers"), list) else []
        worker_jobs = data.get("jobs") if isinstance(data.get("jobs"), list) else []
        latest_job_error = _latest_worker_job_error(worker_jobs)
        reported_controller_url = ""
        for controller_row in paired_controllers:
            if not isinstance(controller_row, dict):
                continue
            if str(controller_row.get("id") or "") == local_controller_id:
                reported_controller_url = str(
                    controller_row.get("observed_url")
                    or controller_row.get("url")
                    or ""
                ).strip().rstrip("/")
                break
        if not reported_controller_url and paired_controllers:
            first_controller = next(
                (
                    item for item in paired_controllers
                    if isinstance(item, dict)
                    and (item.get("observed_url") or item.get("url"))
                ),
                {},
            )
            reported_controller_url = str(
                first_controller.get("observed_url")
                or first_controller.get("url")
                or ""
            ).strip().rstrip("/")
        try:
            protocol_version = max(1, int(data.get("protocol_version") or row.get("protocol_version") or 1))
        except (TypeError, ValueError):
            protocol_version = 1
        row.update({
            # Keep the controller alias stable. The worker's own configured
            # name remains visible separately for diagnostics.
            "name": row.get("name") or data.get("name") or "Worker",
            "worker_reported_name": data.get("name") or row.get("worker_reported_name") or "",
            "last_heartbeat": time.time(),
            "heartbeat_misses": 0,
            "last_failed_at": 0,
            "online": True,
            "status": _node_summary_status(summary),
            "summary": summary,
            "last_error": "",
            "last_job_error": latest_job_error.get("message") or "",
            "last_success_at": time.time(),
            "consecutive_failures": 0,
            "paired_controllers": paired_controllers,
            "jobs": worker_jobs,
            "prediction_profile": data.get("prediction_profile") if isinstance(data.get("prediction_profile"), dict) else {},
            "worker_encoding_policy": data.get("encoding_policy") if isinstance(data.get("encoding_policy"), dict) else {},
            "worker_release": str(data.get("release") or row.get("worker_release") or "")[:80],
            "capabilities": data.get("capabilities") if isinstance(data.get("capabilities"), list) else row.get("capabilities", []),
            "hardware": data.get("hardware") if isinstance(data.get("hardware"), dict) else row.get("hardware", {}),
            "protocol_version": protocol_version,
            "remote_temp_dir": str(data.get("remote_transfer_temp_dir") or row.get("remote_temp_dir") or "").strip()[:500],
            "worker_mode": str(data.get("worker_mode") or row.get("worker_mode") or "full").strip().lower(),
            "requires_remote_transfer": bool(data.get("requires_remote_transfer") or row.get("requires_remote_transfer")),
        })
        # Active encodes retain fast progress reporting. Idle workers back off
        # to one heartbeat per minute to avoid constant network and disk churn.
        row["next_heartbeat_at"] = time.time() + (15 if node_has_running_work(row) else 60)
        if reported_controller_url:
            row["controller_url"] = reported_controller_url
        error_id = str(latest_job_error.get("id") or "")
        previous_error_id = str(row.get("last_remote_job_error_id") or "")
        if error_id:
            row["last_remote_job_error_id"] = error_id
            if error_id != previous_error_id:
                filename = os.path.basename(latest_job_error.get("src") or "") or error_id
                log_event(
                    "remote_job_error",
                    (
                        f"Worker {row.get('name') or row.get('id')} failed {filename}: "
                        f"{latest_job_error.get('message') or 'unknown error'}"
                    ),
                    level="error",
                    job_id=f"remote-{error_id}",
                    src=latest_job_error.get("src"),
                    extra={
                        "worker_id": row.get("id"),
                        "worker_job_id": error_id,
                        "worker_log_tail": latest_job_error.get("log_tail") or "",
                    },
                )
    except Exception as e:
        error_text = str(e)
        if allow_recovery and "unauthorized" in error_text.lower():
            try:
                controller_url = _controller_base_url(row.get("controller_url") or "", row.get("url") or "")
                recovered = recover_worker_session(row, controller_url=controller_url)
                log_event("node_reconnected", f"Recovered paired worker session: {recovered.get('name') or recovered.get('id')}", level="info")
                return _refresh_linked_node(recovered, allow_recovery=False)
            except Exception as recovery_error:
                error_text = f"{error_text}; automatic recovery failed: {recovery_error}"
        now = time.time()
        misses = int(row.get("heartbeat_misses") or 0) + 1
        row["heartbeat_misses"] = misses
        row["consecutive_failures"] = misses
        row["last_failed_at"] = now
        row["last_error"] = error_text[:180]
        retry_delay = min(5 * 60, 10 * (2 ** min(max(0, misses - 1), 5)))
        row["next_heartbeat_at"] = now + retry_delay
        last_heartbeat = float(row.get("last_heartbeat") or 0.0)
        age = now - last_heartbeat if last_heartbeat else None
        still_in_grace = bool(last_heartbeat and age is not None and age <= heartbeat_allowed_age(row))
        if still_in_grace:
            row["online"] = True
            row["status"] = "reconnecting" if node_has_running_work(row) else "stale"
        else:
            row["online"] = False
            row["status"] = "offline"
    save_node(row)
    return row


def _node_heartbeat_loop() -> None:
    NODE_HEARTBEAT_HEALTH.update({"running": True, "started_at": time.time(), "last_error": ""})
    try:
        while not NODE_HEARTBEAT_STOP.is_set():
            try:
                now = time.time()
                NODE_HEARTBEAT_HEALTH["last_cycle_at"] = now
                for row in list_nodes_private():
                    if float(row.get("next_heartbeat_at") or 0) > now:
                        continue
                    try:
                        refreshed = _refresh_linked_node(row)
                        if public_node(refreshed).get("online"):
                            NODE_HEARTBEAT_HEALTH["last_success_at"] = time.time()
                    except Exception as node_error:
                        NODE_HEARTBEAT_HEALTH["cycle_errors"] = int(NODE_HEARTBEAT_HEALTH.get("cycle_errors") or 0) + 1
                        NODE_HEARTBEAT_HEALTH["last_error"] = str(node_error)[:240]
                        log_event(
                            "node_heartbeat_error",
                            f"Node monitor recovered from an error for {(row or {}).get('name') or (row or {}).get('id')}: {str(node_error)[:140]}",
                            level="warn",
                        )
            except Exception as cycle_error:
                NODE_HEARTBEAT_HEALTH["cycle_errors"] = int(NODE_HEARTBEAT_HEALTH.get("cycle_errors") or 0) + 1
                NODE_HEARTBEAT_HEALTH["last_error"] = str(cycle_error)[:240]
            # A worker becoming idle is the capacity-release signal for the
            # next automatic job.  Do not rely on the coordinator having
            # survived a reload or an earlier background-thread failure.
            _ensure_auto_node_dispatch_for_pending_jobs()
            NODE_HEARTBEAT_STOP.wait(timeout=10)
    finally:
        NODE_HEARTBEAT_HEALTH["running"] = False


def _start_node_heartbeat_thread(*, force_serving_process: bool = False) -> bool:
    """Ensure worker health polling runs in the process serving requests."""
    global NODE_HEARTBEAT_THREAD
    if os.environ.get("TSD_DISABLE_NODE_HEARTBEAT") == "1":
        return False
    if (
        not force_serving_process
        and os.environ.get("FLASK_DEBUG") == "1"
        and os.environ.get("WERKZEUG_RUN_MAIN") != "true"
    ):
        return False
    with NODE_HEARTBEAT_LOCK:
        if NODE_HEARTBEAT_THREAD and NODE_HEARTBEAT_THREAD.is_alive():
            return False
        NODE_HEARTBEAT_STOP.clear()
        NODE_HEARTBEAT_THREAD = threading.Thread(
            target=_node_heartbeat_loop,
            name="node-heartbeat",
            daemon=True,
        )
        NODE_HEARTBEAT_THREAD.start()
    return True


def _authenticated_controller():
    node_id = request.headers.get("X-Node-Id") or ""
    timestamp = request.headers.get("X-Node-Timestamp") or ""
    signature = request.headers.get("X-Node-Signature") or ""
    controller = trusted_controller(node_id)
    if not controller:
        return None

    allowed_ips = controller.get("allowed_ips") if isinstance(controller.get("allowed_ips"), list) else []
    remote_addr = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    if allowed_ips and remote_addr not in allowed_ips:
        return None

    body_bytes = request.get_data(cache=True) or b""
    if not verify_hmac(
        request.method,
        request.path,
        body_bytes,
        node_id=node_id,
        token=str(controller.get("token") or ""),
        timestamp=timestamp,
        signature=signature,
    ):
        return None
    updates = {"last_seen": time.time()}
    observed_url = _observed_controller_url(controller.get("url") or "")
    if observed_url:
        updates["observed_url"] = observed_url
    update_trusted_controller(node_id, updates)
    return {**controller, **updates}


def _authenticated_worker():
    node_id = request.headers.get("X-Node-Id") or ""
    timestamp = request.headers.get("X-Node-Timestamp") or ""
    signature = request.headers.get("X-Node-Signature") or ""
    worker = get_node_private(node_id)
    if not worker:
        return None
    body_bytes = request.get_data(cache=True) or b""
    if not verify_hmac(
        request.method,
        request.path,
        body_bytes,
        node_id=node_id,
        token=str(worker.get("token") or ""),
        timestamp=timestamp,
        signature=signature,
    ):
        return None
    worker.update({
        "last_heartbeat": time.time(),
        "heartbeat_misses": 0,
        "online": True,
        "status": worker.get("status") if worker.get("status") in {"running", "queued"} else "idle",
        "last_error": "",
    })
    save_node(worker)
    return worker


def _authenticated_mobile(required_scope: str = "read"):
    authorization = str(request.headers.get("Authorization") or "").strip()
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return authenticate_mobile_token(token, required_scope=required_scope)


def _request_scheme() -> str:
    value = str(request.headers.get("X-Forwarded-Proto") or request.scheme or "http").split(",")[0].strip().lower()
    return value if value in {"http", "https"} else "http"


def _request_port(default: int | None = None) -> int | None:
    host = str(request.headers.get("X-Forwarded-Host") or request.host or "").strip()
    try:
        parsed = urlparse(f"//{host}")
        if parsed.port:
            return int(parsed.port)
    except Exception:
        pass
    if default:
        return int(default)
    scheme = _request_scheme()
    return 443 if scheme == "https" else 80


def _is_loopback_host(hostname: str) -> bool:
    value = str(hostname or "").strip().lower().strip("[]")
    return value in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _infer_controller_url_from_pair_request() -> str:
    remote_host = str(request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()
    if not remote_host:
        return ""
    port = _request_port()
    port_text = "" if port in {80, 443, None} else f":{port}"
    return f"{_request_scheme()}://{remote_host}{port_text}"


def _observed_controller_url(advertised_url: str = "") -> str:
    """Build a controller callback URL from the authenticated request route."""
    remote_host = str(
        request.headers.get("X-Forwarded-For") or request.remote_addr or ""
    ).split(",")[0].strip().strip("[]")
    if not remote_host or _is_loopback_host(remote_host):
        return ""
    advertised = urlparse("")
    try:
        advertised = urlparse(str(advertised_url or "").strip())
        scheme = advertised.scheme if advertised.scheme in {"http", "https"} else _request_scheme()
        port = advertised.port
    except Exception:
        scheme = _request_scheme()
        port = None
    if (
        scheme == "https"
        and advertised.hostname
        and advertised.hostname.strip("[]").lower() != remote_host.lower()
    ):
        return ""
    host_text = f"[{remote_host}]" if ":" in remote_host else remote_host
    default_port = 443 if scheme == "https" else 80
    port_text = f":{port}" if port and port != default_port else ""
    return f"{scheme}://{host_text}{port_text}"


def _infer_controller_url_for_worker(worker_url: str = "") -> str:
    parsed_worker = urlparse(str(worker_url or ""))
    worker_host = parsed_worker.hostname or ""
    if not worker_host:
        return ""

    port = _request_port(parsed_worker.port)
    try:
        family = socket.AF_INET6 if ":" in worker_host else socket.AF_INET
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.2)
            sock.connect((worker_host, int(parsed_worker.port or port or 80)))
            local_host = sock.getsockname()[0]
    except Exception:
        local_host = ""
    if not local_host or _is_loopback_host(local_host):
        return ""
    port_text = "" if port in {80, 443, None} else f":{port}"
    return f"{_request_scheme()}://{local_host}{port_text}"


def _queue_local_paths(
    raw_paths,
    preset: str,
    smart_tuning: dict | None = None,
    *,
    automation_source: str = "library_smart",
) -> tuple[int, list[dict]]:
    if isinstance(raw_paths, str):
        raw_paths = [raw_paths]
    if not isinstance(raw_paths, list):
        raw_paths = []
    preset = str(preset or "auto").strip().lower()
    if preset not in {"auto", "1080", "4k", "smart"}:
        preset = "auto"

    seen = set()
    to_create = []
    skipped = []
    for raw in raw_paths:
        src = str(raw or "").strip()
        if not src or src in seen:
            continue
        seen.add(src)
        reason = ""
        if not os.path.isfile(src):
            reason = "not a file"
        elif not is_allowed_path(src):
            reason = "path not allowed"
        elif not src.lower().endswith(VIDEO_EXTS):
            reason = "not a video"
        elif os.path.splitext(os.path.basename(src))[0].lower().endswith("-tsd"):
            reason = "already tagged -TSD"
        if reason:
            skipped.append({"path": src, "reason": reason})
            continue
        effective = guess_preset_from_filename(os.path.basename(src)) if preset == "auto" else preset
        to_create.append((src, effective))
    if preset == "smart":
        recommendations, planning_errors = _smart_recommendations_for_paths(
            [src for src, _effective in to_create], smart_tuning
        )
        count = 0
        for src, _effective in to_create:
            if src in planning_errors:
                skipped.append({"path": src, "reason": f"smart preset planning failed: {planning_errors[src]}"})
                continue
            try:
                _create_smart_job(
                    src,
                    tuning=smart_tuning,
                    automation_source=automation_source,
                    recommendation=recommendations.get(src),
                )
                count += 1
            except Exception as exc:
                skipped.append({"path": src, "reason": f"smart preset planning failed: {str(exc)[:140]}"})
        return count, skipped
    return int(create_jobs_batch(to_create) or 0), skipped


def _queue_paths_to_destination(
    paths: list[str],
    preset: str,
    mode: str,
    node_id: str = "",
    smart_tuning: dict | None = None,
) -> tuple[int, list[dict]]:
    """Queue an already validated batch using the Queue screen destination."""
    normalized_mode = str(mode or "local").strip().lower()
    if normalized_mode == "local":
        return _queue_local_paths(paths, preset, smart_tuning)

    smart_plans, smart_errors = ({}, {})
    if str(preset or "").strip().lower() == "smart":
        smart_plans, smart_errors = _smart_recommendations_for_paths(paths, smart_tuning)

    def plan_for(src: str) -> dict:
        if src in smart_errors:
            raise RuntimeError(smart_errors[src])
        return _node_queue_plan(
            src,
            preset,
            smart_tuning,
            smart_recommendation=smart_plans.get(src),
        )

    if normalized_mode in {"available", "auto_node", "next_available"}:
        job_ids = []
        skipped = []
        for src in paths:
            try:
                plan = plan_for(src)
                job_id = create_job(
                    src,
                    plan.get("preset") or "1080",
                    extra_args=str(plan.get("extra_args") or ""),
                    preset_bundle=plan.get("preset_bundle"),
                    encode_metadata=plan.get("encode_metadata") if isinstance(plan.get("encode_metadata"), dict) else None,
                    dispatch_mode="auto",
                    preset_selection=plan.get("preset_selection") or preset,
                    preset_adaptive=bool(plan.get("preset_adaptive")),
                    preset_preferences=plan.get("preset_preferences") if isinstance(plan.get("preset_preferences"), dict) else None,
                )
                if job_id not in job_ids:
                    job_ids.append(job_id)
            except Exception as exc:
                skipped.append({"path": src, "reason": str(exc)[:160]})
        if job_ids:
            _wake_auto_node_dispatch()
        return len(job_ids), skipped

    if normalized_mode == "node":
        selected = get_node_private(node_id)
        if not selected:
            return 0, [{"path": "", "reason": "selected worker was not found"}]
        count = 0
        skipped = []
        for src in paths:
            try:
                plan = plan_for(src)
                selected, _result, _transfer_mode = _dispatch_plan_to_worker(selected, src, plan)
                count += 1
            except Exception as exc:
                skipped.append({"path": src, "reason": str(exc)[:160]})
        return count, skipped

    return 0, [{"path": "", "reason": "invalid dispatch mode"}]


def _controller_base_url(default_url: str = "", worker_url: str = "") -> str:
    value = str(default_url or "").strip().rstrip("/")
    if value:
        try:
            parsed = urlparse(value)
            if parsed.scheme in {"http", "https"} and parsed.netloc and not _is_loopback_host(parsed.hostname or ""):
                return value
        except Exception:
            pass
    inferred = _infer_controller_url_for_worker(worker_url)
    if inferred:
        return inferred
    if value:
        return value
    try:
        return request.host_url.rstrip("/")
    except Exception:
        return ""


def _node_preset_bundle(preset_key: str) -> dict | None:
    key = str(preset_key or "").strip().lower()
    if key not in {"1080", "4k"}:
        key = "1080"
    try:
        file_path, preset_name = resolve_preset_file_and_name(key)
        with open(file_path, "r", encoding="utf-8") as f:
            contents = f.read()
        json.loads(contents)
        return {
            "key": key,
            "file_name": os.path.basename(file_path),
            "name": preset_name,
            "contents": contents,
        }
    except Exception as e:
        log_event(
            "node_preset_bundle_error",
            f"Failed to prepare controller preset {key}: {e}",
            level="warn",
            extra={"preset": key},
        )
        return None


def _node_queue_plan(
    src: str,
    requested_preset: str,
    smart_tuning: dict | None = None,
    *,
    smart_recommendation: dict | None = None,
) -> dict:
    requested = str(requested_preset or "auto").strip().lower()
    if requested == "smart":
        recommendation = (
            deepcopy(smart_recommendation)
            if isinstance(smart_recommendation, dict)
            else _smart_recommendation({"src": src, "preset": "auto", "smart_tuning": smart_tuning or {}})
        )
        plan = recommendation.get("selected_plan") or {}
        options = plan.get("options") if isinstance(plan.get("options"), dict) else {}
        estimates = plan.get("estimates") if isinstance(plan.get("estimates"), dict) else {}
        episode_plan = plan.get("episode_plan") if isinstance(plan.get("episode_plan"), dict) else {}
        preset_preferences = dict(options)
        preset_preferences["smart_episode_plan"] = episode_plan
        effective = str(plan.get("preset") or guess_preset_from_filename(os.path.basename(src)))
        return {
            "preset": effective,
            "preset_bundle": _node_preset_bundle(effective),
            "extra_args": " ".join(str(arg) for arg in plan.get("extra_args") or []),
            "preset_selection": "smart",
            "preset_adaptive": True,
            "preset_preferences": preset_preferences,
            "encode_metadata": {
                "encode_method": estimates.get("encoder"),
                "encoder": estimates.get("encoder"),
                "video_codec": options.get("video_codec"),
                "encoder_family": options.get("encoder_family"),
                "bit_depth": options.get("bit_depth"),
                "audio_strategy": options.get("smart_audio_strategy") or options.get("audio_mode"),
                "audio_languages": options.get("audio_languages"),
                "subtitle_languages": options.get("subtitle_languages"),
                "smart_preset": True,
                "smart_profile_id": "default",
                "smart_candidate_id": recommendation.get("recommended_id"),
                "smart_feedback_context": smart_feedback_context(plan, str(recommendation.get("recommended_id") or "balanced")),
                "smart_episode_plan": episode_plan,
                "is_hdr": bool((plan.get("probe") or {}).get("is_hdr")),
                "hdr_reason": str((plan.get("probe") or {}).get("hdr_reason") or ""),
                "automation_source": "library_smart",
                "preset_selection": "smart",
                "preset_adaptive": True,
                "preset_preferences": preset_preferences,
            },
        }
    effective = guess_preset_from_filename(os.path.basename(src)) if requested == "auto" else requested
    if effective not in {"1080", "4k"}:
        effective = guess_preset_from_filename(os.path.basename(src))
    return {
        "preset": effective,
        "preset_bundle": _node_preset_bundle(effective),
        "extra_args": "",
        "preset_selection": requested if requested in {"auto", "1080", "4k"} else effective,
        "preset_adaptive": False,
        "preset_preferences": {},
        "encode_metadata": {
            "encode_method": f"preset:{effective}",
            "encoder": "",
            "video_codec": "",
            "encoder_family": "preset",
            "bit_depth": "",
        },
    }


NODE_ENCODERS = {
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
    "software": {
        ("h264", "8"): "x264",
        ("h264", "10"): "x264_10bit",
        ("h265", "8"): "x265",
        ("h265", "10"): "x265_10bit",
        ("av1", "8"): "svt_av1",
        ("av1", "10"): "svt_av1_10bit",
    },
}


def _bundle_video_encoder(bundle: dict | None) -> str:
    """Read VideoEncoder from the selected preset, never an audio encoder."""
    if not isinstance(bundle, dict):
        return ""
    try:
        data = json.loads(str(bundle.get("contents") or ""))
    except Exception:
        return ""
    expected = str(bundle.get("name") or "").strip().casefold()
    candidates = []

    def walk(value):
        if isinstance(value, dict):
            encoder = str(value.get("VideoEncoder") or "").strip().lower()
            if encoder:
                candidates.append((value, encoder))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    for row, encoder in candidates:
        name = str(row.get("PresetName") or row.get("Name") or "").strip().casefold()
        if expected and name == expected:
            return encoder
    return candidates[0][1] if candidates else ""


def _plan_encoder(plan: dict) -> tuple[str, str, str, str]:
    metadata = plan.get("encode_metadata") if isinstance(plan.get("encode_metadata"), dict) else {}
    encoder = str(metadata.get("encoder") or metadata.get("encode_method") or "").strip().lower()
    if not encoder:
        try:
            args = shlex.split(str(plan.get("extra_args") or ""))
        except Exception:
            args = str(plan.get("extra_args") or "").split()
        for index, arg in enumerate(args):
            if arg in {"--encoder", "-e"} and index + 1 < len(args):
                encoder = str(args[index + 1]).strip().lower()
                break
            if str(arg).startswith("--encoder="):
                encoder = str(arg).split("=", 1)[1].strip().lower()
                break
    if not encoder:
        encoder = _bundle_video_encoder(plan.get("preset_bundle"))
    family = str(metadata.get("encoder_family") or "").strip().lower()
    if (not family or family == "preset") and encoder:
        family = next((name for name in NODE_ENCODERS if encoder in NODE_ENCODERS[name].values()), "software")
    codec = str(metadata.get("video_codec") or "").strip().lower()
    if not codec:
        codec = "av1" if "av1" in encoder else ("h265" if "265" in encoder else "h264")
    depth = str(metadata.get("bit_depth") or "").strip()
    if not depth:
        depth = "10" if "10bit" in encoder or "_10" in encoder else "8"
    return encoder, family or "software", codec, depth


def _hardware_supports_plan(plan: dict, hardware: dict) -> bool:
    encoder, family, _codec, _depth = _plan_encoder(plan)
    families = {str(value).lower() for value in hardware.get("encoder_families") or []}
    encoders = {str(value).lower() for value in hardware.get("encoders") or []}
    if not families and not encoders:
        return True  # Older workers did not report a hardware profile.
    if family == "software":
        return True
    return bool((encoder and encoder in encoders) or family in families and not encoders)


def _replace_plan_encoder_args(extra_args: str, encoder: str) -> str:
    try:
        args = shlex.split(str(extra_args or ""))
    except Exception:
        args = str(extra_args or "").split()
    out = []
    replaced = False
    index = 0
    while index < len(args):
        arg = str(args[index])
        if arg in {"--encoder", "-e"}:
            if not replaced:
                out.extend(["--encoder", encoder])
                replaced = True
            index += 2
            continue
        if arg.startswith("--encoder=") or arg.startswith("-e="):
            if not replaced:
                out.extend(["--encoder", encoder])
                replaced = True
            index += 1
            continue
        if arg == "--encoder-preset":
            # Preset vocabularies differ between QSV, NVENC, VCE, and x265.
            # Let HandBrake choose the safe default for a derived fallback.
            index += 2
            continue
        if arg.startswith("--encoder-preset="):
            index += 1
            continue
        out.append(arg)
        index += 1
    if not replaced:
        out.extend(["--encoder", encoder])
    return shlex.join(out)


def _derived_preset_bundle(
    bundle: dict | None,
    encoder: str,
    family: str,
    display_name: str = "",
) -> dict | None:
    if not isinstance(bundle, dict):
        return None
    try:
        data = json.loads(str(bundle.get("contents") or ""))
    except Exception:
        return bundle
    expected = str(bundle.get("name") or "").strip().casefold()
    candidates = []

    def walk(value):
        if isinstance(value, dict):
            if value.get("VideoEncoder"):
                candidates.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    selected = next(
        (
            row for row in candidates
            if str(row.get("PresetName") or row.get("Name") or "").strip().casefold() == expected
        ),
        candidates[0] if candidates else None,
    )
    if not selected:
        return bundle
    family_label = {"qsv": "Intel QSV", "nvenc": "NVIDIA NVENC", "vce": "AMD VCE", "software": "Software"}.get(family, family.upper())
    original_name = str(bundle.get("name") or selected.get("PresetName") or "Smart preset")
    derived_name = str(display_name or f"{original_name} [Smart {family_label}]")[:160]
    selected["VideoEncoder"] = encoder
    selected["VideoHWDecode"] = 0
    selected["VideoQSVDecode"] = False
    selected["VideoAdapterIndex"] = -1
    if selected.get("PresetName") is not None:
        selected["PresetName"] = derived_name
    elif selected.get("Name") is not None:
        selected["Name"] = derived_name
    out = dict(bundle)
    out["name"] = derived_name
    out["contents"] = json.dumps(data, indent=2)
    return out


def _adapt_smart_plan_for_node(plan: dict, node: dict) -> dict:
    """Derive only the encoder for Smart jobs; locked presets stay byte-for-byte."""
    out = deepcopy(plan if isinstance(plan, dict) else {})
    metadata = out.get("encode_metadata") if isinstance(out.get("encode_metadata"), dict) else {}
    original_bundle = out.get("preset_bundle") if isinstance(out.get("preset_bundle"), dict) else {}
    out.setdefault("preset_revision", max(1, int(metadata.get("preset_revision") or 1)))
    adaptive = bool(out.get("preset_adaptive") or metadata.get("preset_adaptive") or metadata.get("smart_preset"))
    if not adaptive:
        out.setdefault("queued_preset_name", str(original_bundle.get("name") or out.get("preset") or ""))
        return out
    old_encoder, old_family, codec, depth = _plan_encoder(out)
    display_name = _queued_preset_display_name(
        str(out.get("preset") or "1080"),
        original_bundle,
        extra_args=str(out.get("extra_args") or ""),
        encode_metadata=metadata,
        preset_selection=str(out.get("preset_selection") or "smart"),
        preset_adaptive=True,
    )
    # Keep the snapshotted HandBrake preset and its public label aligned with
    # the encoder selected by the per-episode Smart plan, even when the node
    # already supports that encoder and no fallback is required.
    out["queued_preset_name"] = display_name
    out["preset_bundle"] = _derived_preset_bundle(
        out.get("preset_bundle"),
        old_encoder,
        old_family,
        display_name,
    )
    metadata = dict(metadata)
    metadata["queued_preset_name"] = display_name
    out["encode_metadata"] = metadata
    hardware = node.get("hardware") if isinstance(node.get("hardware"), dict) else {}
    if not hardware or _hardware_supports_plan(out, hardware):
        return out

    families = [str(value).lower() for value in hardware.get("encoder_families") or []]
    supported = {str(value).lower() for value in hardware.get("encoders") or []}
    selected_encoder = ""
    selected_family = "software"
    for family in [value for value in families if value != "software"] + ["software"]:
        candidate = NODE_ENCODERS.get(family, {}).get((codec, depth))
        if candidate and (not supported or candidate in supported):
            selected_encoder = candidate
            selected_family = family
            break
    if not selected_encoder:
        selected_encoder = NODE_ENCODERS["software"].get((codec, depth)) or (
            "svt_av1_10bit" if codec == "av1" else ("x265_10bit" if codec == "h265" else "x264")
        )
        selected_family = "software"

    metadata = dict(metadata)
    metadata.update({
        "encode_method": selected_encoder,
        "encoder": selected_encoder,
        "encoder_family": selected_family,
    })
    adaptation = {
        "node_id": str(node.get("id") or ""),
        "node_name": str(node.get("name") or "Worker"),
        "from_encoder": old_encoder,
        "from_family": old_family,
        "to_encoder": selected_encoder,
        "to_family": selected_family,
        "reason": f"{old_family or 'requested'} encoder is unavailable on the selected node",
        "adapted_at": time.time(),
    }
    metadata["preset_adaptation"] = adaptation
    metadata["preset_selection"] = "smart"
    metadata["preset_adaptive"] = True
    metadata["preset_preferences"] = out.get("preset_preferences") if isinstance(out.get("preset_preferences"), dict) else metadata.get("preset_preferences", {})
    out["encode_metadata"] = metadata
    out["extra_args"] = _replace_plan_encoder_args(str(out.get("extra_args") or ""), selected_encoder)
    display_name = _queued_preset_display_name(
        str(out.get("preset") or "1080"),
        out.get("preset_bundle"),
        extra_args=out["extra_args"],
        encode_metadata=metadata,
        preset_selection="smart",
        preset_adaptive=True,
    )
    metadata["queued_preset_name"] = display_name
    out["queued_preset_name"] = display_name
    out["preset_bundle"] = _derived_preset_bundle(
        out.get("preset_bundle"),
        selected_encoder,
        selected_family,
        display_name,
    )
    out["preset_adaptation"] = adaptation
    return out


def _plan_metadata_for_worker(plan: dict) -> dict:
    metadata = dict(plan.get("encode_metadata")) if isinstance(plan.get("encode_metadata"), dict) else {}
    metadata.update({
        "preset_selection": str(plan.get("preset_selection") or metadata.get("preset_selection") or plan.get("preset") or "1080"),
        "preset_adaptive": bool(plan.get("preset_adaptive") or metadata.get("preset_adaptive")),
        "preset_preferences": (
            plan.get("preset_preferences")
            if isinstance(plan.get("preset_preferences"), dict)
            else metadata.get("preset_preferences", {})
        ),
        "preset_snapshot_locked": True,
    })
    bundle = plan.get("preset_bundle") if isinstance(plan.get("preset_bundle"), dict) else {}
    metadata["queued_preset_name"] = str(plan.get("queued_preset_name") or bundle.get("name") or plan.get("preset") or "")
    metadata["preset_revision"] = max(1, int(plan.get("preset_revision") or metadata.get("preset_revision") or 1))
    adaptation = plan.get("preset_adaptation") if isinstance(plan.get("preset_adaptation"), dict) else None
    if adaptation:
        metadata["preset_adaptation"] = adaptation
    operations = plan.get("operations") if isinstance(plan.get("operations"), dict) else None
    if operations is None:
        settings = load_settings()
        audio_policy = str(settings.get("audio_policy_default") or "preserve")
        operations = {
            "job_type": "video_audio" if audio_policy != "preserve" else "video_preserve_audio",
            "audio_policy": audio_policy,
            "default_target_codec": str(settings.get("audio_optimize_codec") or "aac"),
            "allow_object_audio_loss": bool(settings.get("audio_allow_object_metadata_loss", False)),
            "subtitle_action": "copy",
        }
    metadata["operations"] = operations
    metadata["job_type"] = str(operations.get("job_type") or plan.get("job_type") or "video_preserve_audio")
    if plan.get("parent_job_id"):
        metadata["parent_job_id"] = str(plan.get("parent_job_id"))
    return metadata


def _prepare_plan_for_node(plan: dict, node: dict) -> dict:
    operations = plan.get("operations") if isinstance(plan.get("operations"), dict) else {}
    if str(operations.get("job_type") or plan.get("job_type") or "").strip().lower() == "audio_only":
        prepared = dict(plan)
        prepared["encode_metadata"] = _plan_metadata_for_worker(prepared)
        return prepared
    prepared = _adapt_smart_plan_for_node(plan, node)
    metadata = prepared.get("encode_metadata") if isinstance(prepared.get("encode_metadata"), dict) else {}
    adaptive = bool(prepared.get("preset_adaptive") or metadata.get("preset_adaptive") or metadata.get("smart_preset"))
    hardware = node.get("hardware") if isinstance(node.get("hardware"), dict) else {}
    if not adaptive and not _hardware_supports_plan(prepared, hardware):
        encoder, _family, _codec, _depth = _plan_encoder(prepared)
        node_name = str(node.get("name") or "selected node")
        raise ValueError(
            f"{node_name} does not support locked video encoder {encoder or 'from the queued preset'}; "
            "choose Smart or click Edit preset"
        )
    prepared["encode_metadata"] = _plan_metadata_for_worker(prepared)
    return prepared


def _worker_encoding_policy(selected: dict, source_policy: dict | None = None) -> dict:
    controller_settings = load_settings()
    source = source_policy if isinstance(source_policy, dict) else controller_settings
    output_container = normalize_output_container(source.get("output_container"))
    web_optimized = source.get("web_optimized", False)
    if isinstance(web_optimized, str):
        web_optimized = web_optimized.strip().lower() in {"1", "true", "yes", "on"}
    return {
        "hb_threads": source.get("hb_threads", controller_settings.get("hb_threads", 0)),
        "hardware_transcode_concurrency": normalize_hardware_transcode_concurrency(
            selected.get("hardware_transcode_concurrency"),
            controller_settings.get("hardware_transcode_concurrency", 1),
        ),
        "auto_stop_large_output_enabled": source.get(
            "auto_stop_large_output_enabled",
            controller_settings.get("auto_stop_large_output_enabled", False),
        ),
        "auto_stop_large_output_percent": source.get(
            "auto_stop_large_output_percent",
            controller_settings.get("auto_stop_large_output_percent", 90),
        ),
        "output_container": output_container,
        "web_optimized": bool(
            output_container == "mp4" and web_optimized
        ),
        "audio_policy_default": str(source.get("audio_policy_default") or controller_settings.get("audio_policy_default") or "preserve"),
        "audio_optimize_codec": str(source.get("audio_optimize_codec") or controller_settings.get("audio_optimize_codec") or "aac"),
        "audio_allow_downmix": bool(source.get("audio_allow_downmix", controller_settings.get("audio_allow_downmix", False))),
        "audio_allow_object_metadata_loss": bool(source.get("audio_allow_object_metadata_loss", controller_settings.get("audio_allow_object_metadata_loss", False))),
    }


def _dispatch_plan_to_worker(
    selected: dict,
    src: str,
    plan: dict,
    *,
    controller_url_hint: str = "",
    require_available_for: dict | None = None,
) -> tuple[dict, dict, str]:
    """Send one planned job to a linked worker, including remote fallback."""
    selected = _refresh_linked_node(selected)
    if not public_node(selected).get("online"):
        raise ValueError(selected.get("last_error") or "worker is offline")
    if isinstance(require_available_for, dict) and not _worker_available_for_auto(selected, require_available_for)[0]:
        raise ValueError("worker capacity changed before assignment")
    plan = _prepare_plan_for_node(plan, selected)

    selected_mode = normalize_transfer_mode(selected.get("transfer_mode") or "local")
    controller_url = _controller_base_url(
        selected.get("controller_url") or controller_url_hint or "",
        selected.get("url") or "",
    )
    if selected_mode in {"remote", "auto"} and not controller_url:
        raise ValueError("controller URL could not be inferred for remote transfer")
    if controller_url and controller_url != str(selected.get("controller_url") or "").strip().rstrip("/"):
        selected["controller_url"] = controller_url
        save_node(selected)

    policy = _worker_encoding_policy(selected, plan.get("encoding_policy"))

    def remote_payload() -> dict:
        source_size = int(os.path.getsize(src))
        grant = create_transfer_grant(src, selected.get("id") or "", source_size=source_size)
        effective_preset = plan.get("preset") or "1080"
        encode_metadata = _plan_metadata_for_worker(plan)
        transfer_row = get_transfer(grant["id"]) or {}
        transfer_row["preset"] = effective_preset
        transfer_row["controller_url"] = controller_url
        transfer_row["remote_temp_dir"] = str(selected.get("remote_temp_dir") or "").strip()
        transfer_row["encode_metadata"] = encode_metadata
        transfer_row["output_container"] = policy["output_container"]
        transfer_row["web_optimized"] = policy["web_optimized"]
        transfer_row["operations"] = plan.get("operations") if isinstance(plan.get("operations"), dict) else encode_metadata.get("operations", {})
        transfer_row["job_type"] = str(plan.get("job_type") or (transfer_row["operations"].get("job_type") if transfer_row["operations"] else "") or "video_preserve_audio")
        transfer_row["parent_job_id"] = str(plan.get("parent_job_id") or "")
        save_transfer(transfer_row)
        return {
            "src": src,
            "preset": effective_preset,
            "preset_bundle": plan.get("preset_bundle"),
            "extra_args": str(plan.get("extra_args") or ""),
            "encode_metadata": encode_metadata,
            "encoding_policy": policy,
            "operations": transfer_row["operations"],
            "parent_job_id": transfer_row["parent_job_id"],
            "transfer": {
                "id": grant["id"],
                "controller_id": str(local_node_overview().get("id") or ""),
                "controller_url": controller_url,
                "source_url": f"{controller_url}/api/node/transfers/{grant['id']}/source",
                "upload_url": f"{controller_url}/api/node/transfers/{grant['id']}/output",
                "download_token": grant["download_token"],
                "upload_token": grant["upload_token"],
                "worker_node_id": selected.get("id") or "",
                "original_path": src,
                "source_basename": grant.get("source_basename") or os.path.basename(src),
                "source_size": source_size,
                "remote_temp_dir": str(selected.get("remote_temp_dir") or "").strip(),
                "encode_metadata": encode_metadata,
            },
        }

    worker_path = translate_path(src, selected.get("path_mappings") or [])
    use_remote = selected_mode == "remote" or (selected_mode == "auto" and not worker_path)
    if use_remote:
        payload = remote_payload()
    else:
        if not worker_path:
            raise ValueError("no path mapping for worker")
        payload = {
            "src": worker_path,
            "original_path": src,
            "preset": plan.get("preset"),
            "preset_bundle": plan.get("preset_bundle"),
            "extra_args": plan.get("extra_args") or "",
            "encode_metadata": _plan_metadata_for_worker(plan),
            "encoding_policy": policy,
            "operations": plan.get("operations") if isinstance(plan.get("operations"), dict) else {},
            "parent_job_id": str(plan.get("parent_job_id") or ""),
        }

    result = signed_json_request(
        selected,
        "/api/node/jobs",
        method="POST",
        body={"jobs": [payload], "encoding_policy": policy},
        timeout=15,
    )
    result_skipped = result.get("skipped") if isinstance(result.get("skipped"), list) else []
    if selected_mode == "auto" and not use_remote and result_skipped:
        reasons = {str(item.get("reason") or "").lower() for item in result_skipped if isinstance(item, dict)}
        if reasons.intersection({"not a file", "path not allowed", "headless worker requires remote transfer"}):
            result = signed_json_request(
                selected,
                "/api/node/jobs",
                method="POST",
                body={"jobs": [remote_payload()], "encoding_policy": policy},
                timeout=15,
            )
            result_skipped = result.get("skipped") if isinstance(result.get("skipped"), list) else []
            use_remote = True

    if result_skipped:
        first = result_skipped[0] if isinstance(result_skipped[0], dict) else {}
        raise ValueError(first.get("reason") or "worker rejected the job")
    if not result.get("ok", True):
        raise ValueError(result.get("error") or "worker rejected the job")
    return selected, result, "remote" if use_remote else "local"


def _plan_uses_hardware_encoder(job: dict) -> bool:
    operations = job.get("operations") if isinstance(job.get("operations"), dict) else {}
    if str(job.get("job_type") or operations.get("job_type") or "").strip().lower() == "audio_only":
        return False
    if bool(job.get("uses_hardware_encoder")):
        return True
    family = str(job.get("encoder_family") or "").strip().lower()
    encoder = str(job.get("encoder") or job.get("encode_method") or "").strip().lower()
    if family in {"qsv", "nvenc", "vce", "amf", "videotoolbox", "vaapi"}:
        return True
    if any(token in encoder for token in ("qsv", "nvenc", "vce", "amf", "videotoolbox", "vaapi")):
        return True
    plan = job.get("dispatch_plan") if isinstance(job.get("dispatch_plan"), dict) else {
        "preset": job.get("preset"),
        "preset_bundle": job.get("preset_bundle"),
        "extra_args": job.get("extra_args") or "",
        "preset_selection": job.get("preset_selection") or job.get("preset") or "1080",
        "preset_adaptive": bool(job.get("preset_adaptive")),
        "preset_preferences": job.get("preset_preferences") if isinstance(job.get("preset_preferences"), dict) else {},
        "encode_metadata": {
            "encoder": job.get("encoder") or job.get("encode_method") or "",
            "encode_method": job.get("encode_method") or job.get("encoder") or "",
            "encoder_family": job.get("encoder_family") or "",
            "video_codec": job.get("video_codec") or "",
            "bit_depth": job.get("bit_depth") or "",
            "smart_preset": bool(job.get("smart_preset")),
        },
    }
    bundle = plan.get("preset_bundle") if isinstance(plan.get("preset_bundle"), dict) else {}
    try:
        preset_data = json.loads(str(bundle.get("contents") or ""))
    except Exception:
        preset_data = None

    def video_encoder(value) -> str:
        if isinstance(value, dict):
            direct = str(value.get("VideoEncoder") or "").strip().lower()
            if direct:
                return direct
            for child in value.values():
                found = video_encoder(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = video_encoder(child)
                if found:
                    return found
        return ""

    preset_encoder = video_encoder(preset_data)
    return any(token in preset_encoder for token in ("qsv", "nvenc", "vce", "amf", "videotoolbox", "vaapi"))


def _worker_available_for_auto(row: dict, job: dict) -> tuple[bool, float]:
    public = public_node(row)
    if not public.get("online") or public.get("status") in {"offline", "paired", "reconnecting", "stale"}:
        return False, 1.0
    plan = job.get("dispatch_plan") if isinstance(job.get("dispatch_plan"), dict) else {
        "preset": job.get("preset"),
        "preset_bundle": job.get("preset_bundle"),
        "extra_args": job.get("extra_args") or "",
        "preset_selection": job.get("preset_selection") or job.get("preset") or "1080",
        "preset_adaptive": bool(job.get("preset_adaptive")),
        "preset_preferences": job.get("preset_preferences") if isinstance(job.get("preset_preferences"), dict) else {},
        "encode_metadata": {
            "encoder": job.get("encoder") or job.get("encode_method") or "",
            "encode_method": job.get("encode_method") or job.get("encoder") or "",
            "encoder_family": job.get("encoder_family") or "",
            "video_codec": job.get("video_codec") or "",
            "bit_depth": job.get("bit_depth") or "",
            "smart_preset": bool(job.get("smart_preset")),
        },
    }
    metadata = plan.get("encode_metadata") if isinstance(plan.get("encode_metadata"), dict) else {}
    adaptive = bool(plan.get("preset_adaptive") or metadata.get("preset_adaptive") or metadata.get("smart_preset"))
    if not adaptive and not _hardware_supports_plan(plan, public.get("hardware") or {}):
        return False, 1.0
    prepared_plan = _adapt_smart_plan_for_node(plan, public) if adaptive else plan
    _prepared_encoder, prepared_family, _prepared_codec, _prepared_depth = _plan_encoder(prepared_plan)
    prepared_uses_hardware = prepared_family in {"qsv", "nvenc", "vce"}
    worker_jobs = public.get("jobs") if isinstance(public.get("jobs"), list) else []
    active = [
        item for item in worker_jobs
        if isinstance(item, dict) and str(item.get("status") or "").lower() in {"queued", "running"}
    ]
    summary = public.get("summary") if isinstance(public.get("summary"), dict) else {}
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    reported_active = max(0, int(counts.get("queued") or 0)) + max(0, int(counts.get("running") or 0))
    if not active and reported_active:
        return False, 1.0
    # Queued work has already reserved this worker.  A later automatic job may
    # use another node, but must not jump onto this node before its pinned work
    # starts.  Once the older job is running, normal hardware concurrency rules
    # can use any genuinely spare slots.
    if any(str(item.get("status") or "").lower() == "queued" for item in active):
        return False, 1.0
    limit = normalize_hardware_transcode_concurrency(public.get("hardware_transcode_concurrency"), 1)
    if not active:
        return True, 0.0
    if not prepared_uses_hardware:
        return False, 1.0
    if any(not bool(item.get("uses_hardware_encoder")) for item in active):
        return False, 1.0
    return len(active) < limit, min(1.0, len(active) / max(1, limit))


def _auto_node_dispatch_loop() -> None:
    while not AUTO_NODE_DISPATCH_STOP.is_set():
        try:
            if get_queue_state():
                AUTO_NODE_DISPATCH_WAKE.wait(2.0)
                AUTO_NODE_DISPATCH_WAKE.clear()
                continue
            pending = get_next_auto_dispatch_job()
            if not pending:
                AUTO_NODE_DISPATCH_WAKE.wait(2.0)
                AUTO_NODE_DISPATCH_WAKE.clear()
                continue
            job_id, job = pending
            candidates = []
            local = local_node_overview()
            local_id = str(local.get("id") or "local")
            if auto_dispatch_local_available(job_id):
                local_summary = get_job_summary()
                local_counts = local_summary.get("counts") if isinstance(local_summary.get("counts"), dict) else {}
                local_active = max(0, int(local_counts.get("running") or 0))
                local_limit = max(1, int(local_summary.get("hardware_transcode_concurrency") or 1))
                local_plan = job.get("dispatch_plan") if isinstance(job.get("dispatch_plan"), dict) else {}
                local_metadata = local_plan.get("encode_metadata") if isinstance(local_plan.get("encode_metadata"), dict) else {}
                local_adaptive = bool(local_plan.get("preset_adaptive") or local_metadata.get("preset_adaptive") or local_metadata.get("smart_preset"))
                if local_adaptive or _hardware_supports_plan(local_plan, local.get("hardware") or {}):
                    prepared_local_plan = _prepare_plan_for_node(local_plan, local)
                    _local_encoder, local_family, _local_codec, _local_depth = _plan_encoder(prepared_local_plan)
                    if not (local_active and local_family == "software"):
                        candidates.append({
                            "kind": "local",
                            "id": local_id,
                            "name": local.get("name") or "Main controller",
                            "load": min(1.0, local_active / local_limit),
                            "plan": prepared_local_plan,
                        })
            for row in list_nodes_private():
                available, load = _worker_available_for_auto(row, job)
                if available:
                    candidates.append({
                        "kind": "worker",
                        "id": str(row.get("id") or ""),
                        "name": row.get("name") or "Worker",
                        "load": load,
                        "row": row,
                    })
            if not candidates:
                AUTO_NODE_DISPATCH_WAKE.wait(2.0)
                AUTO_NODE_DISPATCH_WAKE.clear()
                continue
            candidates.sort(key=lambda item: (
                float(item.get("load") or 0.0),
                float(AUTO_NODE_LAST_ASSIGNMENT.get(str(item.get("id") or ""), 0.0)),
                str(item.get("name") or "").casefold(),
            ))
            selected = candidates[0]
            if selected["kind"] == "local":
                if activate_auto_dispatch_locally(
                    job_id,
                    selected["id"],
                    selected["name"],
                    dispatch_plan=selected.get("plan"),
                ):
                    AUTO_NODE_LAST_ASSIGNMENT[selected["id"]] = time.time()
                    log_event(
                        "auto_node_assigned",
                        f"Assigned {os.path.basename(job.get('src') or '')} to {selected['name']}.",
                        job_id=job_id,
                        src=job.get("src"),
                    )
                continue

            AUTO_NODE_LAST_ASSIGNMENT[selected["id"]] = time.time()
            claimed = claim_auto_dispatch_job(job_id, selected["id"], selected["name"])
            if not claimed:
                continue
            plan = claimed.get("dispatch_plan") if isinstance(claimed.get("dispatch_plan"), dict) else {
                "preset": claimed.get("preset"),
                "preset_bundle": claimed.get("preset_bundle"),
                "extra_args": claimed.get("extra_args") or "",
                "encode_metadata": {},
            }
            try:
                refreshed, _result, transfer_mode = _dispatch_plan_to_worker(
                    selected["row"],
                    str(claimed.get("src") or ""),
                    plan,
                    require_available_for=claimed,
                )
                if complete_auto_dispatch_job(job_id):
                    log_event(
                        "auto_node_assigned",
                        (
                            f"Assigned {os.path.basename(claimed.get('src') or '')} to "
                            f"{refreshed.get('name') or selected['name']} ({transfer_mode})."
                        ),
                        job_id=job_id,
                        src=claimed.get("src"),
                        extra={"worker_id": selected["id"], "transfer_mode": transfer_mode},
                    )
                    _refresh_linked_node(refreshed)
            except Exception as exc:
                release_auto_dispatch_job(job_id, str(exc), retry_seconds=3.0)
                log_event(
                    "auto_node_dispatch_retry",
                    f"Could not assign {os.path.basename(claimed.get('src') or '')} to {selected['name']}: {str(exc)[:160]}",
                    level="warn",
                    job_id=job_id,
                    src=claimed.get("src"),
                )
        except Exception as exc:
            log_event("auto_node_dispatch_error", f"Automatic node dispatcher recovered from an error: {str(exc)[:180]}", level="warn")
            AUTO_NODE_DISPATCH_STOP.wait(2.0)


def _start_auto_node_dispatch_thread(*, force_serving_process: bool = False) -> bool:
    """Ensure the automatic node coordinator is alive.

    A wake event cannot revive a missing daemon thread.  Whole-show queueing
    made that failure particularly visible because every episode remained in
    ``waiting_for_node`` with zero attempts.  This function is intentionally
    safe to call on startup and again from every queue wake.
    """
    global AUTO_NODE_DISPATCH_THREAD
    if os.environ.get("TSD_DISABLE_AUTO_NODE_DISPATCH") == "1":
        return False
    if (
        not force_serving_process
        and os.environ.get("FLASK_DEBUG") == "1"
        and os.environ.get("WERKZEUG_RUN_MAIN") != "true"
    ):
        return False
    with AUTO_NODE_DISPATCH_LOCK:
        if AUTO_NODE_DISPATCH_THREAD and AUTO_NODE_DISPATCH_THREAD.is_alive():
            return False
        restarted = AUTO_NODE_DISPATCH_THREAD is not None
        # The event is reserved for an intentional application shutdown.  A
        # replacement coordinator must not inherit an already-set event from
        # a dead predecessor.
        AUTO_NODE_DISPATCH_STOP.clear()
        thread = threading.Thread(
            target=_auto_node_dispatch_loop,
            name="auto-node-dispatch",
            daemon=True,
        )
        AUTO_NODE_DISPATCH_THREAD = thread
        thread.start()
    try:
        log_event(
            "auto_node_dispatch_started",
            "Automatic node dispatcher is running.",
            extra={"restarted": restarted},
        )
    except Exception as exc:
        print(f"[WARN] Could not log automatic node dispatcher startup: {exc}", flush=True)
    return True


def _wake_auto_node_dispatch() -> None:
    # Set first so a newly started coordinator observes pending work without
    # sleeping through its initial cycle.
    AUTO_NODE_DISPATCH_WAKE.set()
    try:
        # Eligibility is based on current heartbeats.  Revive this monitor as
        # well so stale-but-reachable workers become selectable on the next
        # coordinator cycle.
        _start_node_heartbeat_thread(force_serving_process=True)
        _start_auto_node_dispatch_thread(force_serving_process=True)
    except Exception as exc:
        try:
            log_event(
                "auto_node_dispatch_start_error",
                f"Could not start automatic node dispatcher: {str(exc)[:180]}",
                level="error",
            )
        except Exception:
            print(f"[ERROR] Could not start automatic node dispatcher: {exc}", flush=True)


def _ensure_auto_node_dispatch_for_pending_jobs() -> bool:
    """Revive the coordinator whenever durable automatic work is waiting."""
    try:
        pending = get_next_auto_dispatch_job()
    except Exception as exc:
        try:
            log_event(
                "auto_node_dispatch_queue_error",
                f"Could not inspect the automatic node queue: {str(exc)[:180]}",
                level="warn",
            )
        except Exception:
            pass
        return False
    if not pending:
        return False
    _wake_auto_node_dispatch()
    return True


def _transfer_output_path_for_src(src: str, output_container: str | None = None) -> str:
    suffix = (os.environ.get("SUFFIX") or "TSD").strip() or "TSD"
    folder = os.path.dirname(src)
    name = os.path.splitext(os.path.basename(src))[0]
    extension = ".mp4" if normalize_output_container(output_container) == "mp4" else ".mkv"
    return os.path.join(folder, f"{name}-{suffix}{extension}")


def _resolved_transfer_output_container(requested: str | None, reported: str | None) -> str:
    """Honor fixed policies and accept a worker's per-file choice for Auto."""
    configured = normalize_output_container(requested)
    if configured != "auto":
        return configured
    selected = normalize_output_container(reported)
    return selected if selected in {"mp4", "mkv"} else "mkv"


def _authorize_transfer_request(transfer_id: str, kind: str) -> tuple[dict | None, str | None]:
    row = get_transfer(transfer_id)
    if not row:
        return None, "transfer not found"
    worker_id = request.headers.get("X-Worker-Node-Id") or request.headers.get("X-Node-Id") or ""
    if str(row.get("worker_node_id") or "") != str(worker_id or ""):
        return None, "unauthorized worker"
    token = request.headers.get("X-Transfer-Token") or ""
    # A source stream may time out after the controller has accepted the
    # request. Let the same paired worker retry with the same still-valid
    # token; output uploads remain one-shot and use renewal after a failure.
    require_unused = kind != "download"
    if kind == "download" and (row.get("completed_at") or row.get("status") == "complete"):
        return None, "transfer already complete"
    if not transfer_token_matches(row, kind, token, require_unused=require_unused):
        return None, "invalid or expired transfer token"
    return row, None


def _stream_upload_to_file(path: str) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    total = 0
    with open(path, "wb") as f:
        while True:
            chunk = request.stream.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            f.write(chunk)
    return total


def _finalize_transfer_output(row: dict, upload_tmp: str) -> dict:
    transfer_id = str(row.get("id") or "")
    src = str(row.get("src") or "")
    operations = row.get("operations") if isinstance(row.get("operations"), dict) else {}
    job_type = str(row.get("job_type") or operations.get("job_type") or "video_preserve_audio").strip().lower()
    audio_only = job_type == "audio_only"
    if not src or not is_allowed_path(src) or not os.path.isfile(src):
        raise RuntimeError("original source is missing or not allowed")
    if os.path.splitext(os.path.basename(src))[0].lower().endswith("-tsd") and not audio_only:
        raise RuntimeError("refusing to replace an already tagged -TSD source")

    input_inventory = scan_media(src, exact=True)
    normalized_operations = normalize_operations(operations, input_inventory)
    output_inventory = scan_media(upload_tmp, exact=True, force=True)
    if audio_only:
        valid_copy, validation_errors = validate_audio_only(
            input_inventory,
            output_inventory,
            normalized_operations,
        )
        if not valid_copy:
            raise RuntimeError("audio-only safeguards failed: " + "; ".join(validation_errors))

    upload_ok, upload_reason = _encoded_output_is_valid(upload_tmp)
    if not upload_ok:
        raise RuntimeError(f"uploaded output failed validation: {upload_reason}")

    selected_container = _resolved_transfer_output_container(
        row.get("output_container"),
        request.headers.get("X-Output-Container"),
    )
    if audio_only:
        selected_container = "mp4" if os.path.splitext(src)[1].lower() in {".mp4", ".m4v"} else "mkv"
    container_reason = str(request.headers.get("X-Output-Container-Reason") or "").strip()[:240]
    out_path = src if audio_only else _transfer_output_path_for_src(src, selected_container)
    if not audio_only and os.path.exists(out_path):
        raise RuntimeError(f"output already exists: {out_path}")

    final_part = f"{out_path}.transfer-{transfer_id}.part"
    backup_path = f"{src}.bytesqueeze-audio-backup-{transfer_id}" if audio_only else ""
    try:
        if os.path.exists(final_part):
            os.remove(final_part)
        shutil.copy2(upload_tmp, final_part)
        part_ok, part_reason = _encoded_output_is_valid(final_part)
        if not part_ok:
            raise RuntimeError(f"copied output failed validation: {part_reason}")
        if not audio_only and os.path.exists(out_path):
            raise RuntimeError(f"output already exists: {out_path}")
        if audio_only:
            if os.path.exists(backup_path):
                raise RuntimeError("an audio optimization backup already exists; refusing unsafe replacement")
            os.replace(src, backup_path)
            try:
                os.replace(final_part, src)
            except Exception:
                os.replace(backup_path, src)
                raise
        else:
            os.replace(final_part, out_path)
        final_ok, final_reason = _encoded_output_is_valid(out_path)
        if not final_ok:
            raise RuntimeError(f"final output failed validation: {final_reason}")
        final_inventory = scan_media(out_path, exact=True, force=True)
        if audio_only:
            valid_final, final_errors = validate_audio_only(
                input_inventory,
                final_inventory,
                normalized_operations,
            )
            if not valid_final:
                raise RuntimeError("installed audio output failed safeguards: " + "; ".join(final_errors))
        output_inventory = final_inventory
        if audio_only and os.path.exists(backup_path):
            os.remove(backup_path)
    except Exception:
        try:
            if os.path.isfile(final_part):
                os.remove(final_part)
        except Exception:
            pass
        if audio_only and backup_path and os.path.isfile(backup_path):
            try:
                if os.path.isfile(src):
                    os.remove(src)
                os.replace(backup_path, src)
            except Exception:
                pass
        raise

    try:
        src_bytes = int(row.get("source_size") or os.path.getsize(src))
    except Exception:
        src_bytes = 0
    out_bytes = int(os.path.getsize(out_path))
    saved_bytes = max(0, src_bytes - out_bytes)
    actual_breakdown = storage_breakdown(input_inventory, output_inventory, src_bytes, out_bytes)
    worker_job_id = request.headers.get("X-Worker-Job-Id") or ""
    try:
        duration_seconds = float(request.headers.get("X-Encode-Duration-Seconds") or 0.0)
    except Exception:
        duration_seconds = 0.0
    worker_node_id = str(row.get("worker_node_id") or "")
    worker_node = get_node_private(worker_node_id) or {}
    encode_metadata = row.get("encode_metadata") if isinstance(row.get("encode_metadata"), dict) else {}
    encode_method = request.headers.get("X-Encode-Method") or encode_metadata.get("encode_method") or ""
    encoder = request.headers.get("X-Encode-Encoder") or encode_metadata.get("encoder") or ""
    video_codec = request.headers.get("X-Encode-Video-Codec") or encode_metadata.get("video_codec") or ""
    encoder_family = request.headers.get("X-Encode-Encoder-Family") or encode_metadata.get("encoder_family") or ""
    bit_depth = request.headers.get("X-Encode-Bit-Depth") or encode_metadata.get("bit_depth") or ""
    record_encode(
        job_id=f"remote-{worker_job_id or transfer_id}",
        src=src,
        out=out_path,
        preset=str(row.get("preset") or "auto"),
        src_bytes=src_bytes,
        out_bytes=out_bytes,
        duration_seconds=duration_seconds if duration_seconds > 0 else None,
        is_hdr=bool(_path_looks_hdr(src)),
        node_id=worker_node_id,
        node_name=worker_node.get("name"),
        encode_method=encode_method,
        encoder=encoder,
        video_codec=video_codec,
        encoder_family=encoder_family,
        bit_depth=bit_depth,
        job_type=job_type,
        operations=normalized_operations,
        parent_job_id=str(row.get("parent_job_id") or ""),
        input_inventory=input_inventory,
        output_inventory=output_inventory,
        storage_breakdown=actual_breakdown,
    )

    source_deleted = False
    warning = ""
    try:
        if not audio_only and os.path.isfile(src):
            os.remove(src)
            source_deleted = True
    except Exception as e:
        warning = f"output verified but failed to delete original: {e}"
        log_event(
            "node_transfer_cleanup_error",
            f"Remote transfer output written but original delete failed: {os.path.basename(src)} ({e})",
            level="warn",
            src=src,
            extra={"out_path": out_path, "transfer_id": transfer_id},
        )

    row.update({
        "status": "complete",
        "completed_at": time.time(),
        "out_path": out_path,
        "output_container_selected": selected_container,
        "output_container_reason": container_reason,
        "out_bytes": out_bytes,
        "saved_bytes": saved_bytes,
        "source_deleted": source_deleted,
        "warning": warning,
        "job_type": job_type,
        "operations": normalized_operations,
        "input_inventory": input_inventory,
        "output_inventory": output_inventory,
        "storage_breakdown": actual_breakdown,
    })
    save_transfer(row)
    log_event(
        "node_transfer_finished",
        f"Remote worker output accepted: {os.path.basename(src)} - saved {round(saved_bytes/(1024**3), 3)} GB",
        src=src,
        extra={
            "out_path": out_path,
            "transfer_id": transfer_id,
            "source_deleted": source_deleted,
        },
    )
    return {
        "out_path": out_path,
        "output_container": selected_container,
        "out_bytes": out_bytes,
        "saved_bytes": saved_bytes,
        "source_deleted": source_deleted,
        "warning": warning,
        "output_inventory": output_inventory,
        "storage_breakdown": actual_breakdown,
    }


def _beta_stamp_library_scan(data: dict) -> dict:
    data = data if isinstance(data, dict) else {}
    data["generated_at"] = time.time()
    return data


# -------------------------------------------------------------------
# Side-by-side preview helpers
# -------------------------------------------------------------------

def _b64_jpg(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _ffmpeg_extract_jpg(input_path: str, t_sec: float, out_path: str) -> None:
    # -ss before -i is fast seek (good enough for preview)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-ss", str(max(0.0, float(t_sec))),
        "-i", input_path,
        "-frames:v", "1",
        "-q:v", "2",
        out_path,
    ]
    ok, out, err = _run_cmd(cmd)
    if not ok or not os.path.isfile(out_path):
        raise RuntimeError(f"ffmpeg frame extract failed: {(err or out or '').strip()[:400]}")


def _ffmpeg_extract_jpg_precise(input_path: str, t_sec: float, out_path: str) -> None:
    # Hybrid seek: jump near the point, then decode a tiny offset for better frame alignment.
    target = max(0.0, float(t_sec or 0))
    coarse = max(0.0, target - 2.0)
    fine = target - coarse
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-ss", f"{coarse:.3f}",
        "-i", input_path,
        "-ss", f"{fine:.3f}",
        "-frames:v", "1",
        "-q:v", "2",
        out_path,
    ]
    ok, out, err = _run_cmd(cmd)
    if not ok or not os.path.isfile(out_path):
        raise RuntimeError(f"ffmpeg precise frame extract failed: {(err or out or '').strip()[:400]}")


def _collect_preset_names(obj):
    """Recursively collect all PresetName strings from a HandBrake preset JSON structure."""
    names = []

    if isinstance(obj, dict):
        # direct hit
        if isinstance(obj.get("PresetName"), str) and obj["PresetName"].strip():
            names.append(obj["PresetName"].strip())

        # common structures
        for k, v in obj.items():
            names.extend(_collect_preset_names(v))

    elif isinstance(obj, list):
        for item in obj:
            names.extend(_collect_preset_names(item))

    return names


def _preset_names_from_file(preset_file: str):
    """Load preset JSON and return a de-duped list of PresetName values in order."""
    with open(preset_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    raw = _collect_preset_names(data)

    # de-dupe while preserving order
    seen = set()
    out = []
    for n in raw:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _pick_best_preset_name(names, base_hint: str):
    """
    If there are multiple presets in one file, pick a reasonable one.
    - If base_hint includes '4k', prefer names containing '4k' or '2160'
    - If base_hint includes '1080', prefer names containing '1080'
    Otherwise pick the first.
    """
    if not names:
        return None

    hint = (base_hint or "").lower()

    if "4k" in hint:
        for n in names:
            nl = n.lower()
            if "4k" in nl or "2160" in nl:
                return n

    if "1080" in hint:
        for n in names:
            nl = n.lower()
            if "1080" in nl:
                return n

    return names[0]



def _hb_preset_args_for_base(base: str, src_basename: str):
    """
    Convert preset base selection (1080/4k/auto) into HandBrakeCLI preset args.
    Uses preset_config mapping, but if the configured preset name is wrong,
    auto-picks the correct PresetName from the preset JSON and saves it.
    """
    base = (base or "auto").lower()
    effective = base
    if effective == "auto":
        effective = guess_preset_from_filename(src_basename)

    cfg = preset_config.get(effective)
    if not cfg:
        raise RuntimeError(f"Unknown preset base: {effective}")

    preset_file = cfg.get("file")
    preset_name = cfg.get("name")

    if not preset_file or not os.path.isfile(preset_file):
        raise RuntimeError(f"Preset file missing: {preset_file}")

    # Auto-pick preset name from the JSON if missing or incorrect
    try:
        names = _preset_names_from_file(preset_file)
        if not names:
            raise RuntimeError("No PresetName found inside preset JSON")

        # If current name is not in the JSON, pick a valid one and persist it
        if (not preset_name) or (preset_name not in names):
            picked = _pick_best_preset_name(names, effective) or names[0]
            preset_config[effective]["name"] = picked
            save_preset_config()
            preset_name = picked

    except Exception as e:
        # If JSON parsing fails, keep old behavior but with a clearer error
        if not preset_name:
            raise RuntimeError(f"Preset name missing and could not auto-detect from JSON: {e}")

    # HandBrakeCLI preset usage:
    # --preset-import-file <file>  and  -Z "<preset name>"
    return effective, ["--preset-import-file", preset_file, "-Z", preset_name]

def _preset_names_in_file(preset_file: str):
    try:
        with open(preset_file, "r", encoding="utf-8") as f:
            j = json.load(f)
    except Exception:
        return []

    names = []

    def walk(x):
        if isinstance(x, dict):
            if isinstance(x.get("PresetName"), str):
                names.append(x["PresetName"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(j)

    # de-dupe preserve order
    out, seen = [], set()
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _flatten_args(arg_list):
    """
    Turn a list that may contain combined strings into a clean argv list.
    e.g. ["--width 1920 --height 1080", "--encoder-preset", "slow"]
      -> ["--width","1920","--height","1080","--encoder-preset","slow"]
    """
    out = []
    for a in (arg_list or []):
        if a is None:
            continue
        if isinstance(a, (list, tuple)):
            out.extend([str(x) for x in a if x is not None])
        else:
            out.extend(str(a).split())
    return out


# -------------------------------------------------------------------
# Route registration
# -------------------------------------------------------------------

def register_routes(app):
    """Attach all routes to the given Flask app."""

    @app.context_processor
    def inject_interface_context():
        """Expose the stable V3 shell and optional V2 Classic UI."""
        settings = load_settings()
        requested = str(request.args.get("ui") or "").strip().lower()
        saved = str(settings.get("ui_version") or "v3").strip().lower()
        ui_version = requested if requested in {"v2", "v3"} else saved
        if ui_version not in {"v2", "v3"}:
            ui_version = "v3"
        density = str(settings.get("ui_density") or "comfortable").strip().lower()
        if density not in {"comfortable", "compact"}:
            density = "comfortable"
        return {
            "ui_version": ui_version,
            "ui_density": density,
            "ui_release_label": "V3",
            "app_release": APP_RELEASE,
        }

    # -------------- cancel preview -----------

    @app.route("/wizard_preview_cancel", methods=["POST"])
    def wizard_preview_cancel():
        data = request.get_json(silent=True) or {}
        preview_id = (data.get("preview_id") or "").strip()
        killed = _kill_preview_by_id(preview_id)
        return jsonify(ok=True, killed=killed)



    # ------------- UI -------------

    @app.route("/jobs")
    def jobs_page():
        """Render file search, one-shot encoding, and queue operations."""
        preset_files = list_preset_files()
        settings = load_settings()
        return render_template(
            "index.html",
            roots=ROOTS,
            preset_files=preset_files,
            preset_dir=PRESET_DIR,
            settings=settings,
        )

    @app.route("/")
    @app.route("/dashboard")
    def home_page():
        """Render the clean system overview used as the main landing page."""
        return render_template("home.html")

    @app.route("/autopilot")
    def autopilot_page():
        """Render the dedicated guided Autopilot workspace."""
        return render_template("autopilot.html", settings=_public_settings(load_settings()))

    @app.route("/size_wizard")
    def size_wizard_page():
        """Render the Size Wizard page (prefill via query string)."""
        return render_template("size_wizard.html")

    @app.route("/library")
    @app.route("/beta")
    def beta_page():
        """Render the primary media library experience."""
        settings = load_settings()
        return render_template(
            "beta.html",
            roots=_beta_roots_payload(settings),
            tmdb_configured=bool(_beta_tmdb_config(settings)),
        )

    @app.route("/api/beta/library")
    def beta_library_api():
        """Scan an allowed media root and group videos into movies and shows."""
        root = request.args.get("root") or "__all__"
        if not root:
            return jsonify(error="no media roots configured"), 400
        settings = load_settings()
        if root != "__all__":
            if not _beta_root_is_mapped(root, settings):
                return jsonify(error="root is not mapped for Beta scanning"), 400
            if not is_allowed_path(root) or not os.path.isdir(root):
                return jsonify(error="root path not allowed or not a directory"), 400

        recursive = str(request.args.get("recursive", "1")).lower() not in {"0", "false", "no"}
        posters = str(request.args.get("posters", "1")).lower() not in {"0", "false", "no"}

        try:
            if root == "__all__" and recursive:
                # The primary refresh path is incremental. Besides avoiding a
                # full reparse on every click, parser-version changes migrate
                # unchanged files once and cached artwork is reused before any
                # provider request is considered.
                index, scan_summary = _beta_update_scan_index(settings)
                catalog_changed = any(
                    int(scan_summary.get(key) or 0) > 0
                    for key in ("new", "changed", "reclassified", "removed")
                )
                cache_available = os.path.isfile(BETA_LIBRARY_CACHE_FILE)
                if catalog_changed or not cache_available:
                    data = _beta_library_from_scan_index(index, settings=settings, recursive=True)
                    data = _beta_enrich_metadata(data, settings, enabled=posters) if posters else _beta_finalize_catalog(data)
                    data = _beta_refresh_predictions(data)
                    scan_summary["catalog_rebuilt"] = True
                else:
                    # A normal refresh should be cheap when no folders changed:
                    # reuse the existing grouped catalog/artwork instead of
                    # rebuilding and rewriting several megabytes of JSON.
                    data = _beta_load_library_cache(settings)
                    scan_summary["catalog_rebuilt"] = False
                scan_strategy = str(scan_summary.get("mode") or "quick_incremental")
                data["scan"] = {**scan_summary, "mode": "incremental", "strategy": scan_strategy}
            elif root == "__all__":
                data = _beta_scan_all_libraries(recursive=False, posters=posters, settings=settings)
                data["scan"] = {"mode": "full"}
            else:
                mapped_root = next((row for row in _beta_mapped_roots(settings) if row["path"] == root), {})
                data = _beta_scan_library(
                    root,
                    recursive=recursive,
                    posters=posters,
                    settings=settings,
                    root_kind=mapped_root.get("kind") or "",
                )
                data["scan"] = {"mode": "full"}
            if not (root == "__all__" and recursive):
                data = _beta_refresh_predictions(data)
            with BETA_TRACKING_LOCK:
                tracking = _beta_load_tracking()
                data = _beta_apply_tracking(data, tracking)
                auto_queue = _beta_auto_queue_tracked_episodes(data, tracking)
                _beta_save_tracking(tracking)
                data = _beta_apply_tracking(data, tracking)
            planning_requests = auto_queue.pop("_planning_requests", [])
            for planning_show_id, planning_paths in planning_requests:
                _beta_schedule_tracked_show_planning(planning_show_id, planning_paths)
            data.setdefault("tracking", {})["auto_queue"] = auto_queue
            if not (root == "__all__" and recursive) or data.get("scan", {}).get("catalog_rebuilt"):
                data = _beta_stamp_library_scan(data)
                _beta_save_library_cache(data)
        except Exception as e:
            return jsonify(error=str(e)), 500

        return jsonify(data)

    @app.route("/api/beta/library_cache")
    def beta_library_cache_api():
        """Return the last saved Beta library scan without touching the filesystem tree."""
        return jsonify(_beta_load_library_cache(load_settings()))

    @app.route("/api/library/calendar")
    def library_calendar_api():
        data = _beta_load_library_cache(load_settings())
        tracked_only = str(request.args.get("tracked", "0")).lower() in {"1", "true", "yes"}
        try:
            days = int(request.args.get("days") or 120)
        except (TypeError, ValueError):
            days = 120
        return jsonify(ok=True, calendar=_beta_calendar_payload(data, tracked_only=tracked_only, days=days))

    @app.route("/api/media/artwork/<filename>")
    def media_artwork_api(filename):
        path = media_artwork_path(filename)
        if not path:
            abort(404)
        return send_file(path, conditional=True, max_age=86400)

    @app.route("/api/beta/auto_scan/status")
    def beta_auto_scan_status_api():
        """Return Beta auto-scan settings and the last scan status."""
        _ensure_beta_autoscan_scheduler()
        settings = load_settings()
        status = _beta_load_autoscan_status(settings)
        return jsonify(
            settings={
                "enabled": bool(settings.get("beta_auto_scan_enabled")),
                "interval_minutes": int(settings.get("beta_auto_scan_interval_minutes") or 30),
                "skip_while_encoding": bool(settings.get("beta_auto_scan_skip_while_encoding", True)),
                "auto_queue_tracked": bool(settings.get("beta_auto_scan_auto_queue_tracked", True)),
                "file_stability_enabled": bool(settings.get("beta_auto_scan_file_stability_enabled", True)),
                "file_stability_minutes": int(settings.get("beta_auto_scan_file_stability_minutes") or 10),
            },
            status=status,
            scheduler={
                **dict(BETA_AUTOSCAN_HEALTH),
                "thread_alive": bool(BETA_AUTOSCAN_THREAD and BETA_AUTOSCAN_THREAD.is_alive()),
            },
        )

    @app.route("/api/autopilot/status")
    def autopilot_status_api():
        _ensure_beta_autoscan_scheduler()
        return jsonify(ok=True, **_autopilot_status_payload())

    @app.route("/api/autopilot/run", methods=["POST"])
    def autopilot_run_api():
        status = _beta_run_incremental_auto_scan(reason="autopilot-manual", force=True)
        return jsonify(ok=True, status=status, **_autopilot_status_payload())

    @app.route("/api/autopilot/onboarding", methods=["POST"])
    def autopilot_onboarding_api():
        data = request.get_json(silent=True) or {}
        completed = bool(data.get("completed", True))
        save_settings({"autopilot_tour_completed": completed})
        return jsonify(ok=True, completed=completed, onboarding=_autopilot_status_payload().get("onboarding"))

    def _autopilot_completed_feedback_response(job_id: str, *, actor: str):
        job = get_job(str(job_id or ""))
        if not job:
            return jsonify(error="completed job not found"), 404
        if job.get("status") != "done":
            return jsonify(error="quality feedback is available after the encode completes"), 409
        context = job.get("smart_feedback_context")
        if not job.get("smart_preset") or not isinstance(context, dict):
            return jsonify(error="this job was not created from a learned preset"), 400
        if isinstance(job.get("quality_feedback"), dict):
            return jsonify(error="feedback was already submitted for this encode"), 409

        data = request.get_json(silent=True) or {}
        verdict = str(data.get("verdict") or "").strip().lower()
        reason = str(data.get("reason") or "").strip().lower()
        try:
            feedback = record_smart_preset_feedback(
                context,
                verdict,
                reason,
                origin="post_encode",
                job_id=str(job_id),
            )
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

        saved = feedback.get("feedback") if isinstance(feedback.get("feedback"), dict) else {}
        job["quality_feedback"] = {
            "id": saved.get("id"),
            "verdict": saved.get("verdict"),
            "reason": saved.get("reason"),
            "created_at": saved.get("created_at") or time.time(),
        }
        save_jobs()
        log_event(
            "autopilot_post_encode_feedback",
            f"{actor} saved {verdict} quality feedback for {os.path.basename(str(job.get('src') or 'completed encode'))}.",
            level="info" if verdict == "approve" else "warn",
            job_id=str(job_id),
            src=str(job.get("src") or ""),
        )
        return jsonify(ok=True, feedback=job["quality_feedback"], learning=feedback.get("learning"), continuous_learning=_autopilot_status_payload().get("continuous_learning"))

    @app.route("/api/autopilot/completed/<job_id>/feedback", methods=["POST"])
    def autopilot_completed_feedback_api(job_id):
        return _autopilot_completed_feedback_response(job_id, actor="Web dashboard")

    @app.route("/api/beta/auto_scan/run", methods=["POST"])
    def beta_auto_scan_run_api():
        """Trigger an immediate incremental Beta auto scan."""
        status = _beta_run_incremental_auto_scan(reason="manual", force=True)
        return jsonify(ok=True, status=status)

    @app.route("/api/beta/tracked_show", methods=["POST"])
    def beta_tracked_show_api():
        """Persist a show Smart policy and independently plan each episode."""
        data = request.get_json(force=True) or {}
        tracked = bool(data.get("tracked"))
        rebuild_smart = bool(data.get("rebuild_smart"))
        show_id = str(data.get("show_id") or data.get("id") or "").strip()
        if not show_id:
            show_id = _beta_show_tracking_key(data)
        title = str(data.get("title") or "Unknown Title").strip() or "Unknown Title"
        paths = _beta_clean_path_list(data.get("paths"))

        with BETA_TRACKING_LOCK:
            tracking = _beta_load_tracking()
            shows = tracking.setdefault("shows", {})
            now = time.time()
            if tracked:
                existing = shows.get(show_id) if isinstance(shows.get(show_id), dict) else {}
                profile, profile_revision = _beta_show_smart_profile(
                    existing,
                    refresh=rebuild_smart,
                )
                known_paths = sorted(set(_beta_clean_path_list(existing.get("known_paths"))) | set(paths))
                shows[show_id] = {
                    **existing,
                    "id": show_id,
                    "title": title,
                    "year": data.get("year"),
                    "tmdb_id": data.get("tmdb_id"),
                    "tvmaze_id": data.get("tvmaze_id"),
                    "poster_url": str(data.get("poster_url") or ""),
                    "tracked": True,
                    "monitor_releases": bool(data.get("monitor_releases", existing.get("monitor_releases", True))),
                    "auto_queue": bool(data.get("auto_queue", existing.get("auto_queue", True))),
                    "known_paths": known_paths,
                    "tracked_paths": sorted(set(_beta_clean_path_list(existing.get("tracked_paths"))) | set(paths)),
                    "smart_profile": profile,
                    "smart_profile_revision": profile_revision,
                    "smart_profile_saved_at": now if rebuild_smart or not existing.get("smart_profile") else float(existing.get("smart_profile_saved_at") or now),
                    "smart_plan_status": "planning",
                    "smart_episode_count": len(paths),
                    "created_at": float(existing.get("created_at") or now),
                    "updated_at": now,
                }
            else:
                shows.pop(show_id, None)
            _beta_save_tracking(tracking)

        planning_started = _beta_schedule_tracked_show_planning(show_id, paths, force=rebuild_smart) if tracked else False
        saved_row = shows.get(show_id) if isinstance(shows.get(show_id), dict) else {}
        return jsonify(
            ok=True,
            show_id=show_id,
            tracked=tracked,
            smart_planning=planning_started or show_id in BETA_TRACKING_PLAN_ACTIVE,
            smart_plan_status=saved_row.get("smart_plan_status") if tracked else "",
            smart_profile_name=(saved_row.get("smart_profile") or {}).get("name") if tracked else "",
            tracked_count=sum(1 for row in shows.values() if isinstance(row, dict) and row.get("tracked")),
        )

    @app.route("/api/beta/queue", methods=["POST"])
    def beta_queue_api():
        """Queue selected Beta library files using the normal Jobs presets."""
        data = request.get_json(force=True) or {}
        preset = str(data.get("preset") or "auto").strip().lower()
        if preset not in {"auto", "1080", "4k", "smart"}:
            return jsonify(error="invalid preset"), 400

        raw_paths = data.get("paths")
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        if not isinstance(raw_paths, list):
            return jsonify(error="missing paths"), 400

        seen = set()
        to_create = []
        skipped = []
        for raw in raw_paths:
            src = str(raw or "").strip()
            if not src or src in seen:
                continue
            seen.add(src)

            reason = ""
            if not os.path.isfile(src):
                reason = "not a file"
            elif not is_allowed_path(src):
                reason = "path not allowed"
            elif not src.lower().endswith(VIDEO_EXTS):
                reason = "not a video"
            elif os.path.splitext(os.path.basename(src))[0].lower().endswith("-tsd"):
                reason = "already tagged -TSD"

            if reason:
                skipped.append({"path": src, "reason": reason})
                continue

            effective_preset = guess_preset_from_filename(os.path.basename(src)) if preset == "auto" else preset
            to_create.append((src, effective_preset))

        if not to_create:
            return jsonify(error="no queueable files selected", skipped=skipped), 400

        if preset == "smart":
            count = 0
            for src, _effective in to_create:
                try:
                    _create_smart_job(
                        src,
                        tuning=data.get("smart_tuning"),
                        automation_source="library_smart",
                    )
                    count += 1
                except Exception as exc:
                    skipped.append({"path": src, "reason": f"smart preset planning failed: {exc}"})
        else:
            count = create_jobs_batch(to_create)
        if count <= 0:
            return jsonify(error="no files could be queued", skipped=skipped), 400
        return jsonify(
            ok=True,
            count=count,
            requested=len(seen),
            skipped=skipped,
            preset=preset,
        )

    @app.route("/api/wizard_presets", methods=["GET", "POST"])
    def wizard_presets_api():
        """List or save Size Wizard recipes."""
        if request.method == "GET":
            return jsonify(presets=_load_wizard_presets())

        data = request.get_json(force=True) or {}
        try:
            preset_id = str(data.get("id") or "").strip()
            name = _clean_wizard_preset_name(data.get("name") or "")
            options_data = data.get("options") if isinstance(data.get("options"), dict) else data
            options = _wizard_public_options(options_data)
        except ValueError as e:
            return jsonify(error=str(e)), 400

        rows = _load_wizard_presets()
        now = time.time()
        existing = None
        for row in rows:
            if preset_id and row.get("id") == preset_id:
                existing = row
                break
            if not preset_id and row.get("name", "").strip().lower() == name.lower():
                existing = row
                break

        if existing:
            existing["name"] = name
            existing["options"] = options
            existing["updated_at"] = now
            saved = existing
        else:
            saved = {
                "id": uuid.uuid4().hex,
                "name": name,
                "options": options,
                "created_at": now,
                "updated_at": now,
            }
            rows.append(saved)

        rows.sort(key=lambda r: float(r.get("updated_at") or 0), reverse=True)
        _save_wizard_presets(rows)
        return jsonify(ok=True, preset=saved, presets=_load_wizard_presets())

    @app.route("/api/wizard_presets/<preset_id>", methods=["DELETE"])
    def wizard_preset_delete_api(preset_id):
        """Delete a saved Size Wizard recipe."""
        preset_id = (preset_id or "").strip()
        rows = _load_wizard_presets()
        kept = [row for row in rows if row.get("id") != preset_id]
        if len(kept) == len(rows):
            return jsonify(error="wizard preset not found"), 404
        _save_wizard_presets(kept)
        return jsonify(ok=True, presets=_load_wizard_presets())

    @app.route("/api/smart_presets", methods=["GET"])
    def smart_presets_api():
        """Return the user's smart-preset goals and learning progress."""
        return jsonify(ok=True, **_smart_preset_public_payload())

    @app.route("/api/smart_presets/profile", methods=["POST"])
    def smart_preset_profile_api():
        data = request.get_json(silent=True) or {}
        profile_data = data.get("profile") if isinstance(data.get("profile"), dict) else data
        profile = save_smart_preset_profile(profile_data)
        return jsonify(ok=True, profile=profile, learning=smart_learning_status())

    @app.route("/api/smart_presets/recommend", methods=["POST"])
    def smart_preset_recommend_api():
        data = request.get_json(silent=True) or {}
        try:
            recommendation = _smart_recommendation(data)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except Exception as exc:
            return jsonify(error=str(exc)), 500
        recommendation.pop("selected_plan", None)
        return jsonify(ok=True, **recommendation)

    @app.route("/api/smart_presets/feedback", methods=["POST"])
    def smart_preset_feedback_api():
        data = request.get_json(silent=True) or {}
        context = _consume_smart_feedback_context(data.get("token") or "")
        if not context:
            return jsonify(error="preview feedback expired or was already submitted"), 409
        try:
            result = record_smart_preset_feedback(context, data.get("verdict"), data.get("reason"))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(ok=True, **result)

    @app.route("/settings")
    @app.route("/settings/<settings_page>")
    def settings_page(settings_page="main"):
        """Render the settings page (global app settings)."""
        settings_page = str(settings_page or "main").strip().lower()
        if settings_page not in {"main", "automation", "smart", "ai", "beta", "nodes"}:
            abort(404)
        if settings_page == "automation":
            return redirect("/autopilot", code=302)
        settings = load_settings()
        preset_files = list_preset_files()
        return render_template(
            "settings.html",
            settings=_public_settings(settings),
            settings_page=settings_page,
            preset_files=preset_files,
            preset_dir=PRESET_DIR,
            roots=_beta_roots_payload(settings),
            allowed_roots=[{"path": path, "label": label or path} for path, label in ROOTS],
        )

    @app.route("/debug_config")
    def debug_config():
        """Debug endpoint showing roots and preset files."""
        preset_files = list_preset_files()
        return jsonify(
            roots=ROOTS,
            preset_files=preset_files,
            preset_dir=PRESET_DIR,
        )

    @app.route("/api/health")
    def health_api():
        try:
            usage = shutil.disk_usage(DATA_DIR)
            storage = {"writable": os.access(DATA_DIR, os.W_OK), "free_bytes": int(usage.free)}
        except Exception as e:
            storage = {"writable": False, "free_bytes": 0, "error": str(e)[:160]}
        monitor = dict(NODE_HEARTBEAT_HEALTH)
        healthy = bool(storage.get("writable")) and bool(monitor.get("running"))
        return jsonify(
            ok=healthy,
            status="healthy" if healthy else "degraded",
            release=APP_RELEASE,
            storage=storage,
            queue={"paused": get_queue_state(), "summary": get_job_summary()},
            schedulers={"node_monitor": monitor, "autopilot": _beta_load_autoscan_status(load_settings())},
        ), (200 if healthy else 503)

    @app.route("/api/home/summary")
    def home_summary_api():
        settings = load_settings()
        library = _beta_load_library_summary(settings)
        jobs = list_jobs_for_api()
        active = [row for row in jobs if row.get("status") in {"running", "queued"}]
        for row in active:
            row.pop("smart_feedback_context", None)
        autopilot = _autopilot_status_payload(compact=True)
        nodes = list_nodes_public()
        storage = get_storage_summary()
        analytics = get_storage_dashboard_analytics(days=30, worker_limit=6)
        current_node_names = {
            str(node.get("id") or ""): str(node.get("name") or "").strip()
            for node in nodes
            if node.get("id") and node.get("name")
        }
        display_name_counts = {}
        for worker in analytics.get("workers", []):
            display_name = current_node_names.get(str(worker.get("node_id") or "")) or str(worker.get("node_name") or "Worker")
            worker["display_name"] = display_name
            display_name_counts[display_name.casefold()] = display_name_counts.get(display_name.casefold(), 0) + 1
        for worker in analytics.get("workers", []):
            display_name = str(worker.get("display_name") or "Worker")
            node_id = str(worker.get("node_id") or "")
            if node_id and display_name_counts.get(display_name.casefold(), 0) > 1:
                worker["display_name"] = f"{display_name} · {node_id[:6]}"
        storage["analytics"] = analytics
        return jsonify(
            ok=True,
            queue={"paused": get_queue_state(), "summary": get_job_summary(), "active": active[:8]},
            library={
                "movies": int(library.get("movies") or 0),
                "shows": int(library.get("shows") or 0),
                "episodes": int(library.get("episodes") or 0),
                "updated_at": library.get("updated_at") or 0,
            },
            storage=storage,
            nodes=nodes,
            autopilot={
                "autopilot": autopilot.get("autopilot"),
                "readiness": autopilot.get("readiness"),
                "continuous_learning": autopilot.get("continuous_learning"),
            },
            events=load_event_summaries(limit=6),
        )

    # ------------- Global settings (JSON API) -------------

    @app.route("/api/settings", methods=["GET", "POST"])
    def settings_api():
        """GET returns settings; POST updates settings."""
        if request.method == "GET":
            settings = load_settings()
            return jsonify(settings=_public_settings(settings))

        data = request.get_json(silent=True) or {}
        old_tmdb_tag = _beta_tmdb_auth_cache_tag(load_settings())
        new_settings = save_settings(data)
        ensure_dispatcher()
        _ensure_beta_autoscan_scheduler()
        BETA_AUTOSCAN_WAKE.set()
        tmdb_changed = _beta_tmdb_auth_cache_tag(new_settings) != old_tmdb_tag
        if tmdb_changed:
            BETA_POSTER_CACHE.clear()
            _beta_clear_library_cache()
        return jsonify(settings=_public_settings(new_settings), tmdb_changed=tmdb_changed)

    @app.route("/api/ai/settings", methods=["GET", "POST"])
    def wizard_ai_settings_api():
        if request.method == "GET":
            return jsonify(ok=True, **_wizard_ai_settings_payload())
        data = request.get_json(silent=True) or {}
        updates = {
            "wizard_ai_provider": data.get("provider"),
            "gemini_model": data.get("gemini_model"),
            "openai_model": data.get("openai_model"),
        }
        if "gemini_api_key" in data and str(data.get("gemini_api_key") or "").strip():
            updates["gemini_api_key"] = str(data.get("gemini_api_key") or "").strip()
        if "openai_api_key" in data and str(data.get("openai_api_key") or "").strip():
            updates["openai_api_key"] = str(data.get("openai_api_key") or "").strip()
        if data.get("clear_gemini_key"):
            updates["gemini_api_key"] = ""
        if data.get("clear_openai_key"):
            updates["openai_api_key"] = ""
        settings = save_settings(updates)
        return jsonify(ok=True, **_wizard_ai_settings_payload(settings))

    @app.route("/api/ai/test", methods=["POST"])
    def wizard_ai_test_api():
        settings = load_settings()
        result = run_wizard_llm(
            "In one sentence, confirm that you can explain a safe balanced plan. Do not change settings.",
            {
                "src": "connection-test.mkv",
                "probe": {"width": 1920, "height": 1080, "duration_sec": 5400, "source_size_bytes": 12 * 1024**3, "is_hdr": False, "source_type": "movie"},
                "inputs": {"ai_goal": "balanced", "ai_risk": "safe", "target_mb": 5500, "video_codec": "h265", "encoder_family": "software", "resolution_mode": "keep", "audio_mode": "copy", "audio_tracks": "all", "subtitle_mode": "all"},
                "estimates": {"encoder": "x265", "quality_label": "Good", "output_resolution": {"width": 1920, "height": 1080}, "ai_decisions": [], "ai_warnings": []},
            },
            settings,
        )
        status_code = 200 if result.get("ok") else 400
        return jsonify(ok=bool(result.get("ok")), answer=result.get("answer") or "", error=result.get("error") or "", status=result.get("status") or wizard_llm_status(settings)), status_code

    # ------------- Future Android companion API (v1) -------------

    @app.route("/api/mobile/v1/discovery")
    def mobile_discovery_api():
        return jsonify(ok=True, **mobile_discovery())

    @app.route("/api/mobile/v1/pair", methods=["POST"])
    def mobile_pair_api():
        data = request.get_json(silent=True) or {}
        try:
            credentials = accept_mobile_pairing(data.get("code") or "", data)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        log_event("mobile_paired", f"Paired mobile device: {credentials.get('device_name')}", level="info")
        return jsonify(ok=True, **credentials)

    @app.route("/api/mobile/v1/token/refresh", methods=["POST"])
    def mobile_refresh_api():
        data = request.get_json(silent=True) or {}
        try:
            credentials = refresh_mobile_token(data.get("device_id") or "", data.get("refresh_token") or "")
        except ValueError as e:
            return jsonify(error=str(e)), 401
        return jsonify(ok=True, **credentials)

    @app.route("/api/mobile/v1/status")
    def mobile_status_api():
        device = _authenticated_mobile("read")
        if not device:
            return jsonify(error="unauthorized mobile device"), 401
        nodes = list_nodes_public()
        return jsonify(
            ok=True,
            device=device,
            queue={"paused": get_queue_state(), "summary": _combined_job_summary()},
            nodes={
                "paired": len(nodes),
                "online": sum(1 for node in nodes if node.get("online")),
            },
        )

    @app.route("/api/mobile/v1/dashboard")
    def mobile_dashboard_api():
        device = _authenticated_mobile("read")
        if not device:
            return jsonify(error="unauthorized mobile device"), 401
        settings = load_settings()
        nodes = list_nodes_public()
        jobs = _combined_jobs_for_api()
        active_jobs = [
            row for row in jobs
            if str(row.get("status") or "").lower() in {"queued", "running", "waiting_to_upload"}
        ]
        library = _beta_load_library_summary(settings)
        return jsonify(
            ok=True,
            release=APP_RELEASE,
            device=device,
            queue={"paused": get_queue_state(), "summary": _combined_job_summary()},
            active_jobs=active_jobs[:8],
            nodes={
                "local": local_node_overview(),
                "items": nodes,
                "paired": len(nodes),
                "online": sum(1 for node in nodes if node.get("online")),
            },
            library={
                "movies": int(library.get("movies") or 0),
                "shows": int(library.get("shows") or 0),
                "last_scan_at": library.get("updated_at") or 0,
                "configured": bool(library.get("configured")),
            },
            automation=_autopilot_status_payload(compact=True),
            storage={
                **get_storage_summary(),
                "analytics": get_storage_dashboard_analytics(),
            },
            events=load_event_summaries(limit=8),
        )

    @app.route("/api/mobile/v1/jobs")
    def mobile_jobs_api():
        if not _authenticated_mobile("read"):
            return jsonify(error="unauthorized mobile device"), 401
        return jsonify(ok=True, jobs=_combined_jobs_for_api(), summary=_combined_job_summary(), paused=get_queue_state())

    @app.route("/api/mobile/v1/jobs/<job_id>/action", methods=["POST"])
    def mobile_job_action_api(job_id):
        device = _authenticated_mobile("control")
        if not device:
            return jsonify(error="control permission required"), 403
        data = request.get_json(silent=True) or {}
        action = str(data.get("action") or "").strip().lower()
        worker_parts = str(job_id or "").split(":", 2)
        if len(worker_parts) == 3 and worker_parts[0] == "worker":
            node_id, worker_job_id = worker_parts[1], worker_parts[2]
            row = get_node_private(node_id)
            if not row:
                return jsonify(error="worker node not found"), 404
            try:
                result = signed_json_request(
                    row,
                    f"/api/node/jobs/{worker_job_id}/action",
                    method="POST",
                    body={"action": action, **({"position": data.get("position")} if "position" in data else {})},
                    timeout=15,
                )
            except Exception as exc:
                return jsonify(error=str(exc)), 400
            if isinstance(result.get("jobs"), list):
                row["jobs"] = result["jobs"]
            if isinstance(result.get("summary"), dict):
                row["summary"] = result["summary"]
                row["status"] = _node_summary_status(result["summary"])
            save_node(row)
            ok, error = True, None
        elif action == "cancel":
            ok, error = cancel_job(job_id)
        elif action == "remove":
            ok, error = remove_queued_job(job_id)
        elif action in {"up", "down", "top", "bottom"}:
            ok, error = move_queued_job(job_id, action)
        elif action == "position":
            ok, error = move_queued_job_to_position(job_id, data.get("position"))
        else:
            return jsonify(error="invalid job action"), 400
        if not ok:
            return jsonify(error=error or "job action failed"), 400
        log_event(
            "mobile_job_action",
            f"{device.get('name')} requested {action} for job {job_id}.",
            level="warn" if action in {"cancel", "remove"} else "info",
            job_id=job_id,
        )
        return jsonify(ok=True, job_id=job_id, action=action, jobs=_combined_jobs_for_api(), summary=_combined_job_summary())

    @app.route("/api/mobile/v1/jobs/<job_id>/preset", methods=["POST"])
    def mobile_job_preset_api(job_id):
        device = _authenticated_mobile("control")
        if not device:
            return jsonify(error="control permission required"), 403
        data = request.get_json(silent=True) or {}
        requested = str(data.get("preset") or data.get("selection") or "").strip().lower()
        if requested not in {"smart", "auto", "1080", "4k"}:
            return jsonify(error="preset must be smart, auto, 1080, or 4k"), 400

        worker_parts = str(job_id or "").split(":", 2)
        if len(worker_parts) == 3 and worker_parts[0] == "worker":
            node_id, worker_job_id = worker_parts[1], worker_parts[2]
            row = get_node_private(node_id)
            if not row:
                return jsonify(error="worker node not found"), 404
            worker_job = next(
                (
                    item for item in row.get("jobs") if isinstance(item, dict)
                    and str(item.get("id") or "") == worker_job_id
                ),
                None,
            ) if isinstance(row.get("jobs"), list) else None
            if not worker_job:
                return jsonify(error="worker job not found"), 404
            try:
                plan = _node_queue_plan(str(worker_job.get("src") or ""), requested, data.get("smart_tuning"))
                plan = _prepare_plan_for_node(plan, public_node(row))
                result = signed_json_request(
                    row,
                    f"/api/node/jobs/{worker_job_id}/preset",
                    method="POST",
                    body={"plan": plan},
                    timeout=20,
                )
            except Exception as exc:
                return jsonify(error=f"preset edit failed: {str(exc)[:180]}"), 400
            if isinstance(result.get("job"), dict):
                row["jobs"] = [
                    result["job"] if isinstance(item, dict) and str(item.get("id") or "") == worker_job_id else item
                    for item in row.get("jobs") or []
                ]
            save_node(row)
        else:
            job = get_job(job_id)
            if not job:
                return jsonify(error="job not found on this node"), 404
            try:
                plan = _node_queue_plan(str(job.get("src") or ""), requested, data.get("smart_tuning"))
            except Exception as exc:
                return jsonify(error=f"preset planning failed: {str(exc)[:180]}"), 400
            ok, error = replace_queued_job_preset(job_id, plan)
            if not ok:
                return jsonify(error=error or "preset edit failed"), 400
            _wake_auto_node_dispatch()

        log_event(
            "mobile_queued_preset_edited",
            f"{device.get('name')} changed queued job {job_id} to {requested}.",
            level="info",
        )
        return jsonify(ok=True, job_id=job_id, preset=requested, jobs=_combined_jobs_for_api(), summary=_combined_job_summary())

    @app.route("/api/mobile/v1/jobs/clear", methods=["POST"])
    def mobile_jobs_clear_api():
        device = _authenticated_mobile("control")
        if not device:
            return jsonify(error="control permission required"), 403
        data = request.get_json(silent=True) or {}
        target = str(data.get("target") or "finished").strip().lower()
        if target == "finished":
            local_removed = clear_finished_jobs_core()
            worker_clear = _clear_linked_worker_jobs(target="finished")
        elif target == "queued":
            local_removed = clear_queued_jobs()
            worker_clear = _clear_linked_worker_jobs(target="queued")
        else:
            return jsonify(error="target must be finished or queued"), 400
        worker_removed = int(worker_clear.get("removed") or 0)
        removed = local_removed + worker_removed
        log_event("mobile_jobs_clear", f"{device.get('name')} cleared {removed} {target} jobs.", level="warn")
        return jsonify(
            ok=True,
            removed=removed,
            local_removed=local_removed,
            worker_removed=worker_removed,
            workers=worker_clear.get("workers") or [],
            target=target,
            summary=_combined_job_summary(),
        )

    @app.route("/api/mobile/v1/library")
    def mobile_library_api():
        if not _authenticated_mobile("read"):
            return jsonify(error="unauthorized mobile device"), 401
        settings = load_settings()
        data = _beta_load_library_cache(settings).copy()
        data["configured"] = bool(_beta_mapped_roots(settings))
        data["roots"] = [
            {"label": row.get("label") or row.get("kind") or "Media", "kind": row.get("kind") or ""}
            for row in _beta_mapped_roots(settings)
        ]
        return jsonify(ok=True, library=_absolute_media_urls(data, request.host_url))

    @app.route("/api/mobile/v1/calendar")
    def mobile_calendar_api():
        if not _authenticated_mobile("read"):
            return jsonify(error="unauthorized mobile device"), 401
        try:
            days = int(request.args.get("days") or 120)
        except (TypeError, ValueError):
            days = 120
        tracked_only = str(request.args.get("tracked", "0")).lower() in {"1", "true", "yes"}
        data = _beta_load_library_cache(load_settings())
        calendar = _beta_calendar_payload(data, tracked_only=tracked_only, days=days)
        return jsonify(ok=True, calendar=_absolute_media_urls(calendar, request.host_url))

    @app.route("/api/mobile/v1/library/refresh", methods=["POST"])
    def mobile_library_refresh_api():
        device = _authenticated_mobile("control")
        if not device:
            return jsonify(error="control permission required"), 403
        status = _beta_run_incremental_auto_scan(reason="bytesqueeze", force=True)
        log_event("mobile_library_refresh", f"{device.get('name')} refreshed the media library.", level="info")
        library = _beta_load_library_cache(load_settings())
        return jsonify(ok=True, status=status, library=_absolute_media_urls(library, request.host_url))

    @app.route("/api/mobile/v1/library/queue", methods=["POST"])
    def mobile_library_queue_api():
        device = _authenticated_mobile("control")
        if not device:
            return jsonify(error="control permission required"), 403
        data = request.get_json(silent=True) or {}
        raw_paths = data.get("paths")
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        if not isinstance(raw_paths, list):
            return jsonify(error="missing paths"), 400
        preset = str(data.get("preset") or "smart").strip().lower()
        if preset not in {"auto", "1080", "4k", "smart"}:
            return jsonify(error="invalid preset"), 400
        dispatch_mode = str(data.get("mode") or "local").strip().lower()
        if dispatch_mode in {
            "best",
            "node",
            "available",
            "auto_node",
            "next_available",
        }:
            return nodes_dispatch_api()
        if dispatch_mode != "local":
            return jsonify(
                error="mode must be local, next available, best, or a specific node"
            ), 400

        seen = set()
        queueable = []
        skipped = []
        for raw in raw_paths:
            src = str(raw or "").strip()
            if not src or src in seen:
                continue
            seen.add(src)
            reason = ""
            if not os.path.isfile(src):
                reason = "not a file"
            elif not is_allowed_path(src):
                reason = "path not allowed"
            elif not src.lower().endswith(VIDEO_EXTS):
                reason = "not a video"
            elif os.path.splitext(os.path.basename(src))[0].lower().endswith("-tsd"):
                reason = "already tagged -TSD"
            if reason:
                skipped.append({"path": src, "reason": reason})
            else:
                queueable.append(src)

        queued = 0
        if preset == "smart":
            queued, smart_skipped = _queue_local_paths(
                queueable,
                "smart",
                data.get("smart_tuning"),
                automation_source="mobile_library_smart",
            )
            skipped.extend(smart_skipped)
        else:
            jobs_to_create = [
                (src, guess_preset_from_filename(os.path.basename(src)) if preset == "auto" else preset)
                for src in queueable
            ]
            queued = int(create_jobs_batch(jobs_to_create) or 0)
        if queued <= 0:
            return jsonify(error="no files could be queued", skipped=skipped), 400
        log_event("mobile_library_queue", f"{device.get('name')} queued {queued} library item(s) with {preset}.", level="info")
        return jsonify(ok=True, queued=queued, requested=len(seen), skipped=skipped, preset=preset)

    @app.route("/api/mobile/v1/size_wizard/plan", methods=["POST"])
    def mobile_size_wizard_plan_api():
        device = _authenticated_mobile("read")
        if not device:
            return jsonify(error="unauthorized mobile device"), 401
        data = request.get_json(silent=True) or {}
        try:
            if _truthy(data.get("smart_start"), False):
                recommendation = _smart_recommendation(
                    {
                        "src": data.get("src"),
                        "preset": data.get("preset") or "auto",
                        "smart_tuning": data.get("smart_tuning") or {},
                    }
                )
                plan = recommendation["selected_plan"]
                selected_id = str(recommendation.get("recommended_id") or "balanced")
                selected = next(
                    (
                        row
                        for row in recommendation.get("candidates") or []
                        if str(row.get("id") or "") == selected_id
                    ),
                    {},
                )
                return jsonify(
                    ok=True,
                    smart_start=True,
                    smart_candidate_id=selected_id,
                    smart_candidate_name=str(selected.get("name") or "Smart starting point"),
                    learned_defaults=recommendation.get("learned_defaults") or {},
                    learning=recommendation.get("learning") or smart_learning_status(),
                    plan=plan,
                )
            plan = _wizard_plan(data, probe_func=_probe_media, preview=False)
            return jsonify(
                ok=True,
                smart_start=False,
                smart_candidate_id=str(data.get("smart_candidate_id") or "manual"),
                smart_candidate_name="Custom Size Wizard plan",
                learned_defaults={},
                learning=smart_learning_status(),
                plan=plan,
            )
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except Exception as exc:
            return jsonify(error=f"Size Wizard planning failed: {str(exc)[:220]}"), 500

    @app.route("/api/mobile/v1/size_wizard/queue", methods=["POST"])
    def mobile_size_wizard_queue_api():
        device = _authenticated_mobile("control")
        if not device:
            return jsonify(error="control permission required"), 403
        data = dict(request.get_json(silent=True) or {})
        data["queue_source"] = "bytesqueeze_app"
        try:
            result = _queue_wizard_job(data)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except Exception as exc:
            return jsonify(error=f"Size Wizard queue failed: {str(exc)[:220]}"), 500
        log_event(
            "mobile_size_wizard_queue",
            f"{device.get('name')} queued {os.path.basename(str(data.get('src') or 'media'))} from Size Wizard.",
            level="info",
            job_id=str(result.get("job_id") or ""),
        )
        return jsonify(ok=True, **result)

    @app.route("/api/mobile/v1/library/tracked_show", methods=["POST"])
    def mobile_library_tracked_show_api():
        device = _authenticated_mobile("control")
        if not device:
            return jsonify(error="control permission required"), 403
        data = request.get_json(silent=True) or {}
        tracked = bool(data.get("tracked"))
        rebuild_smart = bool(data.get("rebuild_smart"))
        show_id = str(data.get("show_id") or data.get("id") or "").strip() or _beta_show_tracking_key(data)
        if not show_id:
            return jsonify(error="missing show id"), 400
        paths = _beta_clean_path_list(data.get("paths"))
        with BETA_TRACKING_LOCK:
            tracking = _beta_load_tracking()
            shows = tracking.setdefault("shows", {})
            now = time.time()
            if tracked:
                existing = shows.get(show_id) if isinstance(shows.get(show_id), dict) else {}
                profile, profile_revision = _beta_show_smart_profile(existing, refresh=rebuild_smart)
                shows[show_id] = {
                    **existing,
                    "id": show_id,
                    "title": str(data.get("title") or "Unknown Title").strip()[:160],
                    "year": data.get("year"),
                    "tmdb_id": data.get("tmdb_id"),
                    "tvmaze_id": data.get("tvmaze_id"),
                    "poster_url": str(data.get("poster_url") or ""),
                    "tracked": True,
                    "monitor_releases": bool(data.get("monitor_releases", existing.get("monitor_releases", True))),
                    "auto_queue": bool(data.get("auto_queue", existing.get("auto_queue", True))),
                    "known_paths": sorted(set(_beta_clean_path_list(existing.get("known_paths"))) | set(paths)),
                    "tracked_paths": sorted(set(_beta_clean_path_list(existing.get("tracked_paths"))) | set(paths)),
                    "smart_profile": profile,
                    "smart_profile_revision": profile_revision,
                    "smart_profile_saved_at": now if rebuild_smart or not existing.get("smart_profile") else float(existing.get("smart_profile_saved_at") or now),
                    "smart_plan_status": "planning",
                    "smart_episode_count": len(paths),
                    "created_at": float(existing.get("created_at") or now),
                    "updated_at": now,
                }
            else:
                shows.pop(show_id, None)
            _beta_save_tracking(tracking)
        planning_started = _beta_schedule_tracked_show_planning(show_id, paths, force=rebuild_smart) if tracked else False
        log_event("mobile_show_tracking", f"{device.get('name')} {'tracked' if tracked else 'untracked'} {data.get('title') or show_id}.", level="info")
        return jsonify(ok=True, show_id=show_id, tracked=tracked, smart_planning=planning_started)

    @app.route("/api/mobile/v1/nodes")
    def mobile_nodes_api():
        if not _authenticated_mobile("read"):
            return jsonify(error="unauthorized mobile device"), 401
        return jsonify(ok=True, local=local_node_overview(), nodes=list_nodes_public())

    @app.route("/api/mobile/v1/nodes/<node_id>/action", methods=["POST"])
    def mobile_node_action_api(node_id):
        device = _authenticated_mobile("control")
        if not device:
            return jsonify(error="control permission required"), 403
        row = get_node_private(node_id)
        if not row:
            return jsonify(error="node not found"), 404
        data = request.get_json(silent=True) or {}
        action = str(data.get("action") or "refresh").strip().lower()
        warning = ""
        if action == "refresh":
            row = _refresh_linked_node(row)
        elif action == "rename":
            try:
                row = rename_node(node_id, data.get("name") or "")
            except ValueError as exc:
                return jsonify(error=str(exc)), 400
        elif action == "capacity":
            limit = normalize_hardware_transcode_concurrency(
                data.get("hardware_transcode_concurrency"),
                row.get("hardware_transcode_concurrency") or 1,
            )
            row["hardware_transcode_concurrency"] = limit
            save_node(row)
            try:
                result = signed_json_request(
                    row,
                    "/api/node/config",
                    method="POST",
                    body=_worker_encoding_policy(row),
                    timeout=10,
                )
                if isinstance(result.get("encoding_policy"), dict):
                    row["worker_encoding_policy"] = result["encoding_policy"]
                    save_node(row)
            except Exception as exc:
                warning = f"Saved on the controller; the worker will receive it with the next dispatch ({str(exc)[:120]})."
        elif action == "clear_finished":
            try:
                result = signed_json_request(
                    row,
                    "/api/node/jobs/clear",
                    method="POST",
                    body={"target": "finished"},
                    timeout=20,
                )
            except Exception as exc:
                return jsonify(error=str(exc)), 400
            if isinstance(result.get("jobs"), list):
                row["jobs"] = result["jobs"]
            if isinstance(result.get("summary"), dict):
                row["summary"] = result["summary"]
                row["status"] = _node_summary_status(result["summary"])
            save_node(row)
        elif action in {"unlink", "forget"}:
            if action == "unlink":
                try:
                    signed_json_request(row, "/api/node/unlink", method="POST", body={}, timeout=8)
                except Exception as exc:
                    warning = f"The worker did not confirm unlink; its controller record was removed locally ({str(exc)[:120]})."
            delete_node(node_id)
            log_event("mobile_node_unlinked", f"{device.get('name')} removed linked worker {row.get('name') or node_id}.", level="warn")
            return jsonify(ok=True, removed=True, warning=warning, nodes=list_nodes_public())
        else:
            return jsonify(error="invalid node action"), 400

        log_event(
            "mobile_node_action",
            f"{device.get('name')} requested {action} for {row.get('name') or node_id}.",
            level="info",
        )
        return jsonify(ok=True, node=public_node(row), warning=warning, nodes=list_nodes_public())

    @app.route("/api/mobile/v1/operations", methods=["GET", "POST"])
    def mobile_operations_api():
        required_scope = "control" if request.method == "POST" else "read"
        device = _authenticated_mobile(required_scope)
        if not device:
            return jsonify(
                error=(
                    "control permission required"
                    if request.method == "POST"
                    else "unauthorized mobile device"
                )
            ), (403 if request.method == "POST" else 401)

        editable_keys = {
            "hardware_transcode_concurrency",
            "auto_stop_large_output_enabled",
            "auto_stop_large_output_percent",
            "audio_policy_default",
            "audio_optimize_codec",
            "audio_allow_downmix",
            "audio_allow_object_metadata_loss",
        }
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            updates = {key: data[key] for key in editable_keys if key in data}
            if not updates:
                return jsonify(error="no supported operation settings supplied"), 400
            save_settings(updates)
            log_event(
                "mobile_operations_settings",
                f"{device.get('name')} updated encoder capacity and safety settings.",
                level="info",
            )

        settings = load_settings()
        public_settings = {
            "hardware_transcode_concurrency": settings.get("hardware_transcode_concurrency", 1),
            "qsv_device_available": bool(settings.get("qsv_device_available")),
            "auto_stop_large_output_enabled": bool(settings.get("auto_stop_large_output_enabled")),
            "auto_stop_large_output_percent": settings.get("auto_stop_large_output_percent", 90),
            "audio_policy_default": settings.get("audio_policy_default", "preserve"),
            "audio_optimize_codec": settings.get("audio_optimize_codec", "aac"),
            "audio_allow_downmix": bool(settings.get("audio_allow_downmix")),
            "audio_allow_object_metadata_loss": bool(settings.get("audio_allow_object_metadata_loss")),
        }
        return jsonify(
            ok=True,
            settings=public_settings,
            capabilities={
                "hardware_concurrency_min": 1,
                "hardware_concurrency_max": 8,
                "software_concurrency": 1,
                "software_jobs_are_exclusive": True,
            },
        )

    @app.route("/api/mobile/v1/automation", methods=["GET", "POST"])
    def mobile_automation_api():
        required_scope = "control" if request.method == "POST" else "read"
        device = _authenticated_mobile(required_scope)
        if not device:
            return jsonify(error="control permission required" if request.method == "POST" else "unauthorized mobile device"), (403 if request.method == "POST" else 401)
        allowed_keys = {
            "autopilot_enabled",
            "autopilot_mode",
            "autopilot_include_movies",
            "autopilot_include_shows",
            "autopilot_min_size_gb",
            "autopilot_min_savings_percent",
            "autopilot_batch_limit",
            "autopilot_max_active_jobs",
            "autopilot_schedule_start",
            "autopilot_schedule_end",
            "autopilot_continuous_learning_enabled",
            "beta_auto_scan_enabled",
            "beta_auto_scan_interval_minutes",
            "beta_auto_scan_skip_while_encoding",
            "beta_auto_scan_auto_queue_tracked",
            "beta_auto_scan_file_stability_enabled",
            "beta_auto_scan_file_stability_minutes",
        }
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            action = str(data.get("action") or "save").strip().lower()
            if action == "run":
                scan_status = _beta_run_incremental_auto_scan(reason="bytesqueeze-autopilot", force=True)
                log_event("mobile_autopilot_run", f"{device.get('name')} ran Autopilot.", level="info")
            elif action == "save":
                updates = {key: data[key] for key in allowed_keys if key in data}
                save_settings(updates)
                _ensure_beta_autoscan_scheduler()
                BETA_AUTOSCAN_WAKE.set()
                scan_status = None
                log_event("mobile_autopilot_settings", f"{device.get('name')} updated automation settings.", level="info")
            else:
                return jsonify(error="action must be save or run"), 400
        else:
            _ensure_beta_autoscan_scheduler()
            scan_status = None

        settings = load_settings()
        public_settings = {key: settings.get(key) for key in allowed_keys}
        return jsonify(
            ok=True,
            settings=public_settings,
            status=_autopilot_status_payload(),
            scan_status=scan_status,
        )

    @app.route("/api/mobile/v1/autopilot/onboarding", methods=["POST"])
    def mobile_autopilot_onboarding_api():
        device = _authenticated_mobile("control")
        if not device:
            return jsonify(error="control permission required"), 403
        data = request.get_json(silent=True) or {}
        completed = bool(data.get("completed", True))
        save_settings({"autopilot_tour_completed": completed})
        log_event(
            "mobile_autopilot_onboarding",
            f"{device.get('name')} {'completed' if completed else 'restarted'} the Autopilot tour.",
            level="info",
        )
        return jsonify(
            ok=True,
            onboarding=_autopilot_status_payload().get("onboarding"),
        )

    @app.route("/api/mobile/v1/storage")
    def mobile_storage_api():
        if not _authenticated_mobile("read"):
            return jsonify(error="unauthorized mobile device"), 401
        try:
            limit = max(1, min(500, int(request.args.get("limit") or 100)))
        except (TypeError, ValueError):
            limit = 100
        return jsonify(
            ok=True,
            summary=get_storage_summary(),
            analytics=get_storage_dashboard_analytics(),
            encodes=list_storage_encodes(limit=limit),
        )

    @app.route("/api/mobile/v1/smart_presets", methods=["GET", "POST"])
    def mobile_smart_presets_api():
        required_scope = "control" if request.method == "POST" else "read"
        if not _authenticated_mobile(required_scope):
            return jsonify(error="control permission required" if request.method == "POST" else "unauthorized mobile device"), (403 if request.method == "POST" else 401)
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            profile_data = data.get("profile") if isinstance(data.get("profile"), dict) else data
            save_smart_preset_profile(profile_data)
        return jsonify(ok=True, **_smart_preset_public_payload())

    @app.route("/api/mobile/v1/events")
    def mobile_events_api():
        if not _authenticated_mobile("read"):
            return jsonify(error="unauthorized mobile device"), 401
        try:
            limit = max(1, min(200, int(request.args.get("limit") or 50)))
        except (TypeError, ValueError):
            limit = 50
        return jsonify(ok=True, events=load_events(limit=limit))

    @app.route("/api/mobile/v1/events/clear", methods=["POST"])
    def mobile_events_clear_api():
        device = _authenticated_mobile("control")
        if not device:
            return jsonify(error="control permission required"), 403
        clear_events()
        log_event("mobile_events_clear", f"{device.get('name')} cleared the event history.", level="warn")
        return jsonify(ok=True)

    @app.route("/api/mobile/v1/queue", methods=["POST"])
    def mobile_queue_control_api():
        device = _authenticated_mobile("control")
        if not device:
            return jsonify(error="control permission required"), 403
        data = request.get_json(silent=True) or {}
        if not isinstance(data.get("paused"), bool):
            return jsonify(error="paused must be true or false"), 400
        paused = set_queue_paused(data["paused"])
        workers = []
        for row in list_nodes_private():
            try:
                result = signed_json_request(
                    row,
                    "/api/node/queue",
                    method="POST",
                    body={"paused": paused},
                    timeout=10,
                )
                if isinstance(result.get("summary"), dict):
                    row["summary"] = result["summary"]
                    row["status"] = _node_summary_status(result["summary"])
                    save_node(row)
                workers.append({
                    "node_id": row.get("id"),
                    "node_name": row.get("name") or row.get("id"),
                    "ok": True,
                    "paused": bool(result.get("paused")),
                })
            except Exception as exc:
                workers.append({
                    "node_id": row.get("id"),
                    "node_name": row.get("name") or row.get("id"),
                    "ok": False,
                    "error": str(exc)[:180],
                })
        log_event("mobile_queue_control", f"{device.get('name')} {'paused' if paused else 'resumed'} the queue.", level="info")
        return jsonify(ok=True, paused=paused, workers=workers, summary=_combined_job_summary())

    # Browser-admin endpoints used by the web settings page.
    @app.route("/api/mobile/pairing_code", methods=["POST"])
    def mobile_pairing_code_admin_api():
        data = request.get_json(silent=True) or {}
        pairing = create_mobile_pairing(scope=data.get("scope") or "control")
        log_event("mobile_pairing_code", "Generated a mobile pairing code.", level="info")
        return jsonify(ok=True, pairing=pairing, discovery=mobile_discovery())

    @app.route("/api/mobile/devices")
    def mobile_devices_admin_api():
        return jsonify(ok=True, devices=list_mobile_devices())

    @app.route("/api/mobile/devices/<device_id>", methods=["DELETE"])
    def mobile_device_revoke_admin_api(device_id):
        if not revoke_mobile_device(device_id):
            return jsonify(error="mobile device not found"), 404
        log_event("mobile_revoked", f"Revoked mobile device {device_id}.", level="warn")
        return jsonify(ok=True)

    # ------------- Multi-node linking -------------

    @app.route("/api/node/local", methods=["GET", "POST"])
    def node_local_api():
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            set_local_node_name(data.get("name") or "")
            return jsonify(local=local_node_overview())
        return jsonify(local=local_node_overview())

    @app.route("/api/node/discovery")
    def node_discovery_api():
        """Unauthenticated protocol metadata used before a pairing exists."""
        return jsonify(ok=True, **node_discovery())

    @app.route("/api/node/pairing_code", methods=["POST"])
    def node_pairing_code_api():
        pairing = create_pairing_code()
        log_event("node_pairing_code", "Generated node pairing code.", level="info")
        return jsonify(ok=True, pairing=pairing)

    @app.route("/api/node/pair/accept", methods=["POST"])
    def node_pair_accept_api():
        data = request.get_json(force=True) or {}
        advertised_url = str(data.get("controller_url") or "").strip().rstrip("/")
        observed_url = _observed_controller_url(advertised_url)
        if advertised_url:
            data["advertised_controller_url"] = advertised_url
        if observed_url:
            data["observed_controller_url"] = observed_url
            data["controller_url"] = observed_url
        elif not advertised_url:
            data["controller_url"] = _infer_controller_url_from_pair_request()
        try:
            accepted = accept_pairing(data.get("code") or "", data)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        log_event("node_paired", "Controller paired with this worker.", level="info")
        return jsonify(ok=True, **accepted)

    @app.route("/api/node/pair/recover", methods=["POST"])
    def node_pair_recover_api():
        data = request.get_json(force=True) or {}
        advertised_url = str(data.get("controller_url") or "").strip().rstrip("/")
        observed_url = _observed_controller_url(advertised_url)
        if advertised_url:
            data["advertised_controller_url"] = advertised_url
        if observed_url:
            data["observed_controller_url"] = observed_url
            data["controller_url"] = observed_url
        elif not advertised_url:
            data["controller_url"] = _infer_controller_url_from_pair_request()
        try:
            recovered = recover_pairing(data)
        except ValueError as e:
            return jsonify(error=str(e)), 401
        log_event("node_reconnected", "Paired controller session recovered automatically.", level="info")
        return jsonify(ok=True, **recovered)

    @app.route("/api/node/pair/enable-recovery", methods=["POST"])
    def node_pair_enable_recovery_api():
        controller = _authenticated_controller()
        if not controller:
            return jsonify(error="unauthorized"), 401
        try:
            recovery_token = enable_pair_recovery(str(controller.get("id") or ""))
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True, recovery_token=recovery_token)

    @app.route("/api/node/status")
    def node_status_api():
        controller = _authenticated_controller()
        if not controller:
            return jsonify(error="unauthorized"), 401
        current_controller_url = str(controller.get("url") or "").strip()
        try:
            parsed_controller_url = urlparse(current_controller_url)
            controller_url_needs_infer = (not current_controller_url) or _is_loopback_host(parsed_controller_url.hostname or "")
        except Exception:
            controller_url_needs_infer = True
        if controller_url_needs_infer:
            inferred_controller_url = _infer_controller_url_from_pair_request()
            if inferred_controller_url:
                update_trusted_controller(controller["id"], {"url": inferred_controller_url})
        local = local_node_overview()
        return jsonify(
            ok=True,
            id=local["id"],
            name=local["name"],
            role=local["role"],
            role_label=local["role_label"],
            paired_controllers=local["paired_controllers"],
            remote_transfer_temp_dir=str(load_settings().get("remote_transfer_temp_dir") or ""),
            summary=get_job_summary(),
            jobs=list_jobs_for_api(include_log_tail=True),
            prediction_profile=_history_prediction_profile(),
            hardware=local.get("hardware") if isinstance(local.get("hardware"), dict) else {},
        )

    @app.route("/api/node/transfers/<transfer_id>/source")
    def node_transfer_source_api(transfer_id):
        row, err = _authorize_transfer_request(transfer_id, "download")
        if err or not row:
            return jsonify(error=err or "unauthorized"), 401
        src = str(row.get("src") or "")
        if not src or not is_allowed_path(src) or not os.path.isfile(src):
            return jsonify(error="source missing or not allowed"), 404
        row["download_used_at"] = time.time()
        row["status"] = "downloading"
        save_transfer(row)
        return send_file(
            src,
            as_attachment=True,
            download_name=row.get("source_basename") or os.path.basename(src),
        )

    @app.route("/api/node/transfers/<transfer_id>/output", methods=["POST"])
    def node_transfer_output_api(transfer_id):
        row, err = _authorize_transfer_request(transfer_id, "upload")
        if err or not row:
            return jsonify(error=err or "unauthorized"), 401

        transfer_dir = os.path.join(NODE_TRANSFER_TMP_DIR, transfer_id)
        upload_tmp = os.path.join(transfer_dir, "output.upload")
        upload_part = upload_tmp + ".part"
        try:
            row["upload_used_at"] = time.time()
            row["status"] = "uploading"
            save_transfer(row)
            size = _stream_upload_to_file(upload_part)
            if size <= 0:
                raise RuntimeError("uploaded output is empty")
            os.replace(upload_part, upload_tmp)
            result = _finalize_transfer_output(row, upload_tmp)
            return jsonify(ok=True, **result)
        except Exception as e:
            row["status"] = "error"
            row["error"] = str(e)[:240]
            save_transfer(row)
            log_event(
                "node_transfer_error",
                f"Remote transfer output rejected: {str(e)[:160]}",
                level="error",
                src=row.get("src"),
                extra={"transfer_id": transfer_id},
            )
            return jsonify(error=str(e)), 400
        finally:
            for path in (upload_part, upload_tmp):
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                except Exception:
                    pass
            try:
                if os.path.isdir(transfer_dir) and not os.listdir(transfer_dir):
                    os.rmdir(transfer_dir)
            except Exception:
                pass

    @app.route("/api/node/transfers/<transfer_id>/renew-upload", methods=["POST"])
    def node_transfer_renew_upload_api(transfer_id):
        worker = _authenticated_worker()
        if not worker:
            return jsonify(error="unauthorized worker"), 401
        try:
            grant = renew_transfer_upload_grant(transfer_id, str(worker.get("id") or ""))
        except ValueError as e:
            return jsonify(error=str(e)), 404
        if grant.get("complete"):
            return jsonify(ok=True, **grant)
        controller_url = _controller_base_url(str(grant.get("controller_url") or ""), str(worker.get("url") or ""))
        return jsonify(
            ok=True,
            complete=False,
            upload_token=grant.get("upload_token"),
            upload_url=f"{controller_url}/api/node/transfers/{transfer_id}/output" if controller_url else "",
            expires_at=grant.get("expires_at"),
        )

    @app.route("/api/node/jobs", methods=["POST"])
    def node_receive_jobs_api():
        controller = _authenticated_controller()
        if not controller:
            return jsonify(error="unauthorized"), 401
        data = request.get_json(force=True) or {}
        jobs_payload = data.get("jobs")
        if not isinstance(jobs_payload, list):
            return jsonify(error="missing jobs"), 400
        local_jobs = []
        remote_jobs = []
        for job in jobs_payload:
            if not isinstance(job, dict):
                continue
            transfer = job.get("transfer") if isinstance(job.get("transfer"), dict) else None
            if transfer:
                remote_jobs.append(job)
                continue
            src = str(job.get("src") or "").strip()
            if not src:
                continue
            local_jobs.append(job)

        seen = []
        skipped = []
        count = 0
        for job in remote_jobs:
            transfer = job.get("transfer") if isinstance(job.get("transfer"), dict) else {}
            src = str(job.get("src") or transfer.get("original_path") or transfer.get("source_basename") or "").strip()
            preset = str(job.get("preset") or "auto").strip().lower()
            if preset not in {"auto", "1080", "4k"}:
                preset = "auto"
            missing = [
                key for key in ("source_url", "upload_url", "download_token", "upload_token", "worker_node_id")
                if not str(transfer.get(key) or "").strip()
            ]
            if missing:
                skipped.append({"path": src, "reason": "missing transfer data"})
                continue
            effective = guess_preset_from_filename(os.path.basename(src)) if preset == "auto" else preset
            _job_id, created = create_remote_transfer_job(
                src,
                effective,
                transfer,
                extra_args=str(job.get("extra_args") or ""),
                preset_bundle=job.get("preset_bundle"),
                encode_metadata=job.get("encode_metadata") if isinstance(job.get("encode_metadata"), dict) else None,
                encoding_policy=(
                    job.get("encoding_policy")
                    if isinstance(job.get("encoding_policy"), dict)
                    else data.get("encoding_policy")
                ),
                operations=job.get("operations") if isinstance(job.get("operations"), dict) else None,
                parent_job_id=str(job.get("parent_job_id") or ""),
            )
            count += 1 if created else 0

        for job in local_jobs:
            src = str(job.get("src") or "").strip()
            original_path = str(job.get("original_path") or src).strip()
            operations = job.get("operations") if isinstance(job.get("operations"), dict) else {}
            audio_only = str(operations.get("job_type") or job.get("job_type") or "").strip().lower() == "audio_only"
            if src in seen:
                continue
            seen.append(src)
            reason = ""
            if not os.path.isfile(src):
                reason = "not a file"
            elif not is_allowed_path(src):
                reason = "path not allowed"
            elif not src.lower().endswith(VIDEO_EXTS):
                reason = "not a video"
            elif os.path.splitext(os.path.basename(src))[0].lower().endswith("-tsd") and not audio_only:
                reason = "already tagged -TSD"
            if reason:
                skipped.append({"path": original_path or src, "worker_path": src, "reason": reason})
                continue
            preset = str(job.get("preset") or "auto").strip().lower()
            if preset not in {"auto", "1080", "4k"}:
                preset = "auto"
            effective = guess_preset_from_filename(os.path.basename(src)) if preset == "auto" else preset
            before = len([j for j in list_jobs_for_api() if j.get("src") == src and j.get("status") in {"queued", "running"}])
            create_job(
                src,
                effective,
                extra_args=str(job.get("extra_args") or ""),
                preset_bundle=job.get("preset_bundle"),
                encode_metadata=job.get("encode_metadata") if isinstance(job.get("encode_metadata"), dict) else None,
                encoding_policy=(
                    job.get("encoding_policy")
                    if isinstance(job.get("encoding_policy"), dict)
                    else data.get("encoding_policy")
                ),
                operations=job.get("operations") if isinstance(job.get("operations"), dict) else None,
                parent_job_id=str(job.get("parent_job_id") or ""),
            )
            after = len([j for j in list_jobs_for_api() if j.get("src") == src and j.get("status") in {"queued", "running"}])
            count += 1 if after > before else 0
        log_event("node_jobs_received", f"Received {count} node job(s).", level="info")
        return jsonify(ok=True, count=count, skipped=skipped, summary=get_job_summary())

    @app.route("/api/node/jobs/<job_id>/log")
    def node_worker_job_log_api(job_id):
        controller = _authenticated_controller()
        if not controller:
            return jsonify(error="unauthorized"), 401
        job = get_job(job_id)
        if not job:
            return jsonify(error="job not found"), 404
        try:
            contents, truncated = read_job_log(job_id)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(
            ok=True,
            job_id=job_id,
            status=job.get("status"),
            error_message=job.get("error_message") or "",
            log=contents,
            truncated=truncated,
        )

    @app.route("/api/node/jobs/<job_id>/action", methods=["POST"])
    def node_worker_job_action_api(job_id):
        controller = _authenticated_controller()
        if not controller:
            return jsonify(error="unauthorized"), 401
        data = request.get_json(silent=True) or {}
        action = str(data.get("action") or "").strip().lower()
        if action == "cancel":
            ok, error = cancel_job(job_id)
        elif action == "remove":
            ok, error = remove_queued_job(job_id)
        elif action in {"up", "down", "top", "bottom"}:
            ok, error = move_queued_job(job_id, action)
        elif action == "position":
            ok, error = move_queued_job_to_position(job_id, data.get("position"))
        else:
            return jsonify(error="invalid job action"), 400
        if not ok:
            return jsonify(error=error or "job action failed"), 400
        return jsonify(
            ok=True,
            job_id=job_id,
            action=action,
            jobs=list_jobs_for_api(include_log_tail=True),
            summary=get_job_summary(),
        )

    @app.route("/api/node/jobs/<job_id>/preset", methods=["POST"])
    def node_worker_job_preset_api(job_id):
        controller = _authenticated_controller()
        if not controller:
            return jsonify(error="unauthorized"), 401
        data = request.get_json(silent=True) or {}
        plan = data.get("plan") if isinstance(data.get("plan"), dict) else None
        ok, error = replace_queued_job_preset(job_id, plan)
        if not ok:
            return jsonify(error=error or "preset edit failed"), 400
        return jsonify(
            ok=True,
            job=next(
                (row for row in list_jobs_for_api(include_log_tail=True) if row.get("id") == job_id),
                None,
            ),
            summary=get_job_summary(),
        )

    @app.route("/api/node/jobs/clear", methods=["POST"])
    def node_worker_jobs_clear_api():
        controller = _authenticated_controller()
        if not controller:
            return jsonify(error="unauthorized"), 401
        data = request.get_json(silent=True) or {}
        target = str(data.get("target") or "finished").strip().lower()
        if target == "finished":
            removed = clear_finished_jobs_core()
        elif target == "queued":
            removed = clear_queued_jobs()
        else:
            return jsonify(error="target must be finished or queued"), 400
        return jsonify(
            ok=True,
            removed=removed,
            jobs=list_jobs_for_api(include_log_tail=True),
            summary=get_job_summary(),
        )

    @app.route("/api/node/queue", methods=["POST"])
    def node_worker_queue_control_api():
        controller = _authenticated_controller()
        if not controller:
            return jsonify(error="unauthorized"), 401
        data = request.get_json(silent=True) or {}
        if not isinstance(data.get("paused"), bool):
            return jsonify(error="paused must be true or false"), 400
        paused = set_queue_paused(data["paused"])
        return jsonify(
            ok=True,
            paused=paused,
            summary=get_job_summary(),
        )

    @app.route("/api/node/rotate_secret", methods=["POST"])
    def node_rotate_secret_api():
        controller = _authenticated_controller()
        if not controller:
            return jsonify(error="unauthorized"), 401
        data = request.get_json(force=True) or {}
        new_token = str(data.get("token") or "").strip()
        if len(new_token) < 24:
            return jsonify(error="invalid token"), 400
        update_trusted_controller(controller["id"], {"token": new_token})
        log_event("node_secret_rotated", "Controller node secret rotated.", level="info")
        return jsonify(ok=True)

    @app.route("/api/node/unlink", methods=["POST"])
    def node_unlink_controller_api():
        controller = _authenticated_controller()
        if not controller:
            return jsonify(error="unauthorized"), 401
        delete_trusted_controller(controller["id"])
        log_event("node_unlinked", "Controller unlinked from this worker.", level="warn")
        return jsonify(ok=True)

    @app.route("/api/nodes")
    def nodes_api():
        _start_node_heartbeat_thread(force_serving_process=True)
        _ensure_auto_node_dispatch_for_pending_jobs()
        nodes = []
        for node in list_nodes_public():
            controller_profile = _history_prediction_profile(node.get("id"))
            worker_profile = node.get("prediction_profile") if isinstance(node.get("prediction_profile"), dict) else {}
            controller_samples = int(controller_profile.get("sample_count") or 0)
            worker_samples = int(worker_profile.get("sample_count") or 0)
            node["controller_prediction_profile"] = controller_profile
            if controller_samples >= worker_samples and controller_samples > 0:
                node["prediction_profile"] = controller_profile
            elif worker_samples > 0:
                node["prediction_profile"] = worker_profile
            nodes.append(node)
        return jsonify(local=local_node_overview(), nodes=nodes, monitor=dict(NODE_HEARTBEAT_HEALTH))

    @app.route("/api/nodes/diagnostics")
    def nodes_diagnostics_api():
        _start_node_heartbeat_thread(force_serving_process=True)
        _ensure_auto_node_dispatch_for_pending_jobs()
        nodes = list_nodes_public()
        return jsonify(
            ok=True,
            protocol=node_discovery(),
            monitor=dict(NODE_HEARTBEAT_HEALTH),
            totals={
                "paired": len(nodes),
                "online": sum(1 for node in nodes if node.get("online")),
                "reconnecting": sum(1 for node in nodes if node.get("status") in {"reconnecting", "stale"}),
                "offline": sum(1 for node in nodes if node.get("status") == "offline"),
            },
            nodes=nodes,
        )

    @app.route("/api/nodes/pair", methods=["POST"])
    def nodes_pair_api():
        data = request.get_json(force=True) or {}
        worker_url = data.get("url") or ""
        controller_url = _controller_base_url(data.get("controller_url") or request.host_url, worker_url)
        try:
            controller_settings = load_settings()
            node = pair_worker(
                worker_url,
                data.get("code") or "",
                name=data.get("name") or "",
                path_mappings=data.get("path_mappings") or [],
                transfer_mode=data.get("transfer_mode") or "remote",
                controller_url=controller_url,
                remote_temp_dir="",
                hardware_transcode_concurrency=controller_settings.get(
                    "hardware_transcode_concurrency",
                    1,
                ),
            )
        except Exception as e:
            return jsonify(error=str(e)), 400
        private = get_node_private(node.get("id") or "")
        if private:
            node = public_node(_refresh_linked_node(private))
        verified = bool(node.get("online"))
        warning = node.get("pairing_notice") or (
            "" if verified else (node.get("last_error") or "Paired, but the first worker status check did not complete.")
        )
        log_event("node_paired", f"Paired worker node: {node.get('name') or node.get('url')}", level="info")
        return jsonify(ok=True, node=node, verified=verified, warning=warning)

    @app.route("/api/nodes/<node_id>/refresh", methods=["POST"])
    def nodes_refresh_api(node_id):
        row = get_node_private(node_id)
        if not row:
            return jsonify(error="node not found"), 404
        row = _refresh_linked_node(row)
        node = public_node(row)
        controller_profile = _history_prediction_profile(node.get("id"))
        controller_samples = int(controller_profile.get("sample_count") or 0)
        if controller_samples > 0 and controller_samples >= int((node.get("prediction_profile") or {}).get("sample_count") or 0):
            node["prediction_profile"] = controller_profile
        node["controller_prediction_profile"] = controller_profile
        return jsonify(ok=True, node=node)

    @app.route("/api/nodes/<node_id>/name", methods=["POST"])
    def nodes_name_api(node_id):
        data = request.get_json(silent=True) or {}
        previous = get_node_private(node_id)
        if not previous:
            return jsonify(error="node not found"), 404
        try:
            row = rename_node(node_id, data.get("name") or "")
        except LookupError:
            return jsonify(error="node not found"), 404
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        log_event(
            "node_renamed",
            f"Renamed linked worker {previous.get('name') or node_id} to {row.get('name')}.",
            level="info",
        )
        return jsonify(ok=True, node=public_node(row))

    @app.route("/api/nodes/refresh", methods=["POST"])
    def nodes_refresh_all_api():
        nodes = []
        for row in list_nodes_private():
            node = public_node(_refresh_linked_node(row))
            controller_profile = _history_prediction_profile(node.get("id"))
            controller_samples = int(controller_profile.get("sample_count") or 0)
            if controller_samples > 0 and controller_samples >= int((node.get("prediction_profile") or {}).get("sample_count") or 0):
                node["prediction_profile"] = controller_profile
            node["controller_prediction_profile"] = controller_profile
            nodes.append(node)
        return jsonify(ok=True, nodes=nodes)

    @app.route("/api/nodes/<node_id>/jobs/<job_id>/log")
    def nodes_worker_job_log_proxy_api(node_id, job_id):
        row = get_node_private(node_id)
        if not row:
            return jsonify(error="node not found"), 404
        api_path = f"/api/node/jobs/{job_id}/log"
        try:
            result = signed_json_request(row, api_path, method="GET", timeout=20)
        except Exception as exc:
            return jsonify(error=f"worker log unavailable: {exc}"), 502
        contents = str(result.get("log") or "")
        if result.get("truncated"):
            contents = "[Earlier worker log output omitted.]\n" + contents
        filename = secure_filename(f"{job_id}.worker.log") or "worker-job.log"
        response = app.response_class(contents, mimetype="text/plain; charset=utf-8")
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    @app.route("/api/nodes/<node_id>/path_mappings", methods=["POST"])
    def nodes_path_mappings_api(node_id):
        row = get_node_private(node_id)
        if not row:
            return jsonify(error="node not found"), 404
        data = request.get_json(force=True) or {}
        if row.get("requires_remote_transfer") or row.get("worker_mode") == "headless":
            row["path_mappings"] = []
            row["transfer_mode"] = "remote"
        else:
            row["path_mappings"] = normalize_path_mappings(data.get("path_mappings") or [])
            row["transfer_mode"] = normalize_transfer_mode(data.get("transfer_mode") or row.get("transfer_mode") or "local")
        row["controller_url"] = _controller_base_url(row.get("controller_url") or "", row.get("url") or "")
        save_node(row)
        return jsonify(ok=True, node=public_node(row))

    @app.route("/api/nodes/<node_id>/settings", methods=["POST"])
    def nodes_worker_settings_api(node_id):
        """Store worker capacity on the controller and apply it when online."""
        row = get_node_private(node_id)
        if not row:
            return jsonify(error="node not found"), 404
        data = request.get_json(silent=True) or {}
        if "hardware_transcode_concurrency" not in data:
            return jsonify(error="hardware_transcode_concurrency is required"), 400

        limit = normalize_hardware_transcode_concurrency(
            data.get("hardware_transcode_concurrency"),
            row.get("hardware_transcode_concurrency") or 1,
        )
        row["hardware_transcode_concurrency"] = limit
        save_node(row)

        policy = _worker_encoding_policy(row)
        applied_online = False
        warning = ""
        try:
            result = signed_json_request(
                row,
                "/api/node/config",
                method="POST",
                body=policy,
                timeout=8,
            )
            applied_online = bool(result.get("ok"))
            if isinstance(result.get("encoding_policy"), dict):
                row["worker_encoding_policy"] = result["encoding_policy"]
                save_node(row)
        except Exception as exc:
            # Older workers still receive the controller policy with every
            # newly dispatched job, so saving centrally remains useful.
            warning = (
                "Saved on the controller. The worker will receive this limit "
                f"with its next dispatch ({str(exc)[:120]})."
            )

        log_event(
            "worker_capacity_updated",
            f"Set {row.get('name') or node_id} to {limit} simultaneous GPU transcode(s).",
            level="info",
        )
        return jsonify(
            ok=True,
            node=public_node(row),
            applied_online=applied_online,
            warning=warning,
        )

    @app.route("/api/nodes/<node_id>/rotate_secret", methods=["POST"])
    def nodes_rotate_secret_api(node_id):
        row = get_node_private(node_id)
        if not row:
            return jsonify(error="node not found"), 404
        new_token = secrets.token_urlsafe(32)
        try:
            signed_json_request(row, "/api/node/rotate_secret", method="POST", body={"token": new_token}, timeout=8)
        except Exception as e:
            return jsonify(error=str(e)), 400
        row["token"] = new_token
        save_node(row)
        log_event("node_secret_rotated", f"Rotated secret for worker node: {row.get('name') or node_id}", level="info")
        return jsonify(ok=True, node=public_node(row))

    @app.route("/api/nodes/<node_id>/unlink", methods=["POST"])
    def nodes_unlink_api(node_id):
        row = get_node_private(node_id)
        if not row:
            return jsonify(error="node not found"), 404
        try:
            signed_json_request(row, "/api/node/unlink", method="POST", body={}, timeout=5)
        except Exception:
            pass
        delete_node(node_id)
        log_event("node_unlinked", f"Unlinked worker node: {row.get('name') or node_id}", level="warn")
        return jsonify(ok=True)

    @app.route("/api/nodes/<node_id>/forget", methods=["POST", "DELETE"])
    def nodes_forget_api(node_id):
        row = get_node_private(node_id)
        existed = delete_node(node_id)
        if not existed:
            return jsonify(error="node not found"), 404
        log_event("node_forgotten", f"Forgot local worker node record: {(row or {}).get('name') or node_id}", level="warn")
        return jsonify(ok=True)

    @app.route("/api/nodes/dispatch", methods=["POST"])
    def nodes_dispatch_api():
        data = request.get_json(force=True) or {}
        request_operations = data.get("operations") if isinstance(data.get("operations"), dict) else {}
        request_audio_only = str(request_operations.get("job_type") or "").strip().lower() == "audio_only"
        mode = str(data.get("mode") or "local").strip().lower()
        preset = str(data.get("preset") or "auto").strip().lower()
        paths = data.get("paths")
        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, list) or not paths:
            return jsonify(error="missing paths"), 400

        smart_batch_plans = {}
        smart_batch_errors = {}
        smart_batch_initialized = False

        def planned_node_queue(src: str) -> dict:
            nonlocal smart_batch_plans, smart_batch_errors, smart_batch_initialized
            if preset == "smart" and not smart_batch_initialized:
                smart_paths = []
                for raw in paths:
                    candidate = str(raw or "").strip()
                    if (
                        candidate
                        and candidate not in smart_paths
                        and os.path.isfile(candidate)
                        and is_allowed_path(candidate)
                        and candidate.lower().endswith(VIDEO_EXTS)
                        and not os.path.splitext(os.path.basename(candidate))[0].lower().endswith("-tsd")
                    ):
                        smart_paths.append(candidate)
                smart_batch_plans, smart_batch_errors = _smart_recommendations_for_paths(
                    smart_paths, data.get("smart_tuning")
                )
                smart_batch_initialized = True
            if src in smart_batch_errors:
                raise RuntimeError(smart_batch_errors[src])
            return _node_queue_plan(
                src,
                preset,
                data.get("smart_tuning"),
                smart_recommendation=smart_batch_plans.get(src),
            )

        if mode in {"available", "auto_node", "next_available"}:
            job_ids = []
            skipped = []
            seen = set()
            for raw in paths:
                src = str(raw or "").strip()
                if not src or src in seen:
                    continue
                seen.add(src)
                reason = ""
                if not os.path.isfile(src):
                    reason = "not a file"
                elif not is_allowed_path(src):
                    reason = "path not allowed"
                elif not src.lower().endswith(VIDEO_EXTS):
                    reason = "not a video"
                elif os.path.splitext(os.path.basename(src))[0].lower().endswith("-tsd") and not request_audio_only:
                    reason = "already tagged -TSD"
                if reason:
                    skipped.append({"path": src, "reason": reason})
                    continue
                try:
                    plan = planned_node_queue(src)
                    if isinstance(data.get("operations"), dict):
                        plan["operations"] = data["operations"]
                        plan["job_type"] = str(data["operations"].get("job_type") or "video_preserve_audio")
                    job_id = create_job(
                        src,
                        plan.get("preset") or "1080",
                        extra_args=str(plan.get("extra_args") or ""),
                        preset_bundle=plan.get("preset_bundle"),
                        encode_metadata=plan.get("encode_metadata") if isinstance(plan.get("encode_metadata"), dict) else None,
                        dispatch_mode="auto",
                        preset_selection=plan.get("preset_selection") or preset,
                        preset_adaptive=bool(plan.get("preset_adaptive")),
                        preset_preferences=plan.get("preset_preferences") if isinstance(plan.get("preset_preferences"), dict) else None,
                        operations=plan.get("operations") if isinstance(plan.get("operations"), dict) else None,
                    )
                    if job_id not in job_ids:
                        job_ids.append(job_id)
                except Exception as exc:
                    skipped.append({"path": src, "reason": f"queue planning failed: {str(exc)[:140]}"})
            if not job_ids:
                return jsonify(error="no queueable files", skipped=skipped), 400
            _wake_auto_node_dispatch()
            return jsonify(
                ok=True,
                target="next available node",
                count=len(job_ids),
                skipped=skipped,
                job_ids=job_ids,
                dispatch_mode="auto",
            )

        if mode == "local":
            count, skipped = _queue_local_paths(paths, preset, data.get("smart_tuning"))
            return jsonify(ok=True, target="local", count=count, skipped=skipped)

        selected = None
        if mode == "node":
            selected = get_node_private(data.get("node_id") or "")
        elif mode == "best":
            candidates = [_refresh_linked_node(row) for row in list_nodes_private()]
            online = [row for row in candidates if public_node(row).get("online")]
            idle = [row for row in online if public_node(row).get("status") == "idle"]
            ranked = idle or online
            selected = (ranked or [None])[0]
            if preset != "smart" and ranked:
                probe_src = next((str(path or "").strip() for path in paths if str(path or "").strip()), "")
                if probe_src:
                    try:
                        probe_plan = _node_queue_plan(probe_src, preset, data.get("smart_tuning"))
                        selected = next(
                            (row for row in ranked if _hardware_supports_plan(probe_plan, row.get("hardware") or {})),
                            selected,
                        )
                    except Exception:
                        pass
        else:
            return jsonify(error="invalid dispatch mode"), 400

        if not selected:
            return jsonify(error="no worker node available"), 400

        selected = _refresh_linked_node(selected)
        selected_mode = normalize_transfer_mode(selected.get("transfer_mode") or "local")
        controller_url = _controller_base_url(selected.get("controller_url") or data.get("controller_url") or "", selected.get("url") or "")
        if selected_mode in {"remote", "auto"} and not controller_url:
            return jsonify(error="controller URL could not be inferred for remote transfer"), 400
        if controller_url and controller_url != str(selected.get("controller_url") or "").strip().rstrip("/"):
            selected["controller_url"] = controller_url
            save_node(selected)
        jobs_payload = []
        skipped = []
        worker_encoding_policy = _worker_encoding_policy(selected)

        def build_remote_job_payload(src: str, plan: dict) -> tuple[dict | None, str | None]:
            try:
                source_size = int(os.path.getsize(src))
                grant = create_transfer_grant(src, selected.get("id") or "", source_size=source_size)
                effective_preset = plan.get("preset") or "1080"
                encode_metadata = _plan_metadata_for_worker(plan)
                transfer_row = get_transfer(grant["id"]) or {}
                transfer_row["preset"] = effective_preset
                transfer_row["controller_url"] = controller_url
                transfer_row["remote_temp_dir"] = str(selected.get("remote_temp_dir") or "").strip()
                transfer_row["encode_metadata"] = encode_metadata
                transfer_row["output_container"] = worker_encoding_policy["output_container"]
                transfer_row["web_optimized"] = worker_encoding_policy["web_optimized"]
                transfer_row["operations"] = plan.get("operations") if isinstance(plan.get("operations"), dict) else encode_metadata.get("operations", {})
                transfer_row["job_type"] = str(plan.get("job_type") or transfer_row["operations"].get("job_type") or "video_preserve_audio")
                transfer_row["parent_job_id"] = str(plan.get("parent_job_id") or "")
                save_transfer(transfer_row)
                transfer_payload = {
                    "id": grant["id"],
                    "controller_id": str(local_node_overview().get("id") or ""),
                    "controller_url": controller_url,
                    "source_url": f"{controller_url}/api/node/transfers/{grant['id']}/source",
                    "upload_url": f"{controller_url}/api/node/transfers/{grant['id']}/output",
                    "download_token": grant["download_token"],
                    "upload_token": grant["upload_token"],
                    "worker_node_id": selected.get("id") or "",
                    "original_path": src,
                    "source_basename": grant.get("source_basename") or os.path.basename(src),
                    "source_size": source_size,
                    "remote_temp_dir": str(selected.get("remote_temp_dir") or "").strip(),
                    "encode_metadata": encode_metadata,
                }
                return {
                    "src": src,
                    "preset": effective_preset,
                    "preset_bundle": plan.get("preset_bundle"),
                    "extra_args": str(plan.get("extra_args") or ""),
                    "encode_metadata": encode_metadata,
                    "encoding_policy": worker_encoding_policy,
                    "operations": transfer_row["operations"],
                    "parent_job_id": transfer_row["parent_job_id"],
                    "transfer": transfer_payload,
                }, None
            except Exception as e:
                return None, f"transfer setup failed: {str(e)[:120]}"

        for raw in paths:
            src = str(raw or "").strip()
            if not src:
                continue
            if not is_allowed_path(src):
                skipped.append({"path": src, "reason": "path not allowed"})
                continue
            if not os.path.isfile(src):
                skipped.append({"path": src, "reason": "not a file"})
                continue
            if not src.lower().endswith(VIDEO_EXTS):
                skipped.append({"path": src, "reason": "not a video"})
                continue
            if os.path.splitext(os.path.basename(src))[0].lower().endswith("-tsd") and not request_audio_only:
                skipped.append({"path": src, "reason": "already tagged -TSD"})
                continue

            try:
                plan = planned_node_queue(src)
                if isinstance(data.get("operations"), dict):
                    plan["operations"] = data["operations"]
                    plan["job_type"] = str(data["operations"].get("job_type") or "video_preserve_audio")
                plan = _prepare_plan_for_node(plan, selected)
            except Exception as exc:
                skipped.append({"path": src, "reason": f"preset planning failed: {str(exc)[:180]}"})
                continue
            worker_path = translate_path(src, selected.get("path_mappings") or [])
            use_remote = selected_mode == "remote" or (selected_mode == "auto" and not worker_path)

            if use_remote:
                remote_payload, remote_error = build_remote_job_payload(src, plan)
                if remote_payload:
                    jobs_payload.append(remote_payload)
                else:
                    skipped.append({"path": src, "reason": remote_error or "transfer setup failed"})
                continue

            if not worker_path:
                skipped.append({"path": src, "reason": "no path mapping for worker"})
                continue
            jobs_payload.append({
                "src": worker_path,
                "original_path": src,
                "preset": plan.get("preset"),
                "preset_bundle": plan.get("preset_bundle"),
                "extra_args": plan.get("extra_args") or "",
                "encode_metadata": _plan_metadata_for_worker(plan),
                "encoding_policy": worker_encoding_policy,
                "operations": plan.get("operations") if isinstance(plan.get("operations"), dict) else _plan_metadata_for_worker(plan).get("operations", {}),
                "parent_job_id": str(plan.get("parent_job_id") or ""),
            })

        if not jobs_payload:
            return jsonify(error="no worker-queueable files", skipped=skipped), 400

        try:
            result = signed_json_request(
                selected,
                "/api/node/jobs",
                method="POST",
                body={
                    "jobs": jobs_payload,
                    "encoding_policy": worker_encoding_policy,
                },
                timeout=15,
            )
        except Exception as e:
            return jsonify(error=str(e), skipped=skipped), 400

        result_skipped = result.get("skipped") if isinstance(result.get("skipped"), list) else []
        if selected_mode == "auto" and result_skipped:
            retry_payload = []
            retried_paths = set()
            remaining_result_skipped = []
            for item in result_skipped:
                if not isinstance(item, dict):
                    remaining_result_skipped.append(item)
                    continue
                reason = str(item.get("reason") or "").lower()
                original_path = str(item.get("path") or "").strip()
                if reason not in {"not a file", "path not allowed"} or not original_path or original_path in retried_paths:
                    remaining_result_skipped.append(item)
                    continue
                if not is_allowed_path(original_path) or not os.path.isfile(original_path):
                    remaining_result_skipped.append(item)
                    continue
                try:
                    retry_plan = planned_node_queue(original_path)
                    if isinstance(data.get("operations"), dict):
                        retry_plan["operations"] = data["operations"]
                        retry_plan["job_type"] = str(data["operations"].get("job_type") or "video_preserve_audio")
                    retry_plan = _prepare_plan_for_node(retry_plan, selected)
                except Exception as exc:
                    remaining_result_skipped.append({"path": original_path, "reason": f"preset planning failed: {str(exc)[:180]}"})
                    continue
                remote_payload, remote_error = build_remote_job_payload(original_path, retry_plan)
                if remote_payload:
                    retry_payload.append(remote_payload)
                    retried_paths.add(original_path)
                else:
                    remaining_result_skipped.append({"path": original_path, "reason": remote_error or "transfer setup failed"})

            if retry_payload:
                try:
                    retry_result = signed_json_request(
                        selected,
                        "/api/node/jobs",
                        method="POST",
                        body={
                            "jobs": retry_payload,
                            "encoding_policy": worker_encoding_policy,
                        },
                        timeout=15,
                    )
                    result["count"] = int(result.get("count") or 0) + int(retry_result.get("count") or 0)
                    result_skipped = remaining_result_skipped + (retry_result.get("skipped") or [])
                    result["skipped"] = result_skipped
                except Exception as e:
                    result_skipped = remaining_result_skipped + [{"path": path, "reason": f"remote fallback failed: {str(e)[:120]}"} for path in sorted(retried_paths)]
                    result["skipped"] = result_skipped

        _refresh_linked_node(selected)
        return jsonify(ok=True, target=public_node(selected), count=result.get("count", 0), skipped=skipped + (result.get("skipped") or []), transfer_mode=selected_mode)

    # ------------- CPU profiles (JSON API) -------------

    @app.route("/api/cpu_profiles", methods=["GET"])
    def cpu_profiles_api():
        """Return CPU profiles for the Settings dropdown / ETA estimation."""
        return jsonify(profiles=list_cpu_profiles())

    # ------------- Events (Lidarr-style feed) -------------

    @app.route("/api/events", methods=["GET"])
    def events_api():
        """Return newest-first event log entries."""
        try:
            limit = int(request.args.get("limit") or 200)
        except ValueError:
            limit = 200
        limit = max(1, min(2000, limit))
        return jsonify(events=load_events(limit=limit))

    @app.route("/api/events/clear", methods=["POST"])
    def events_clear_api():
        clear_events()
        return jsonify(ok=True)

    # ------------- Storage savings stats -------------

    @app.route("/api/storage_stats/summary", methods=["GET"])
    def storage_summary_api():
        return jsonify(summary=get_storage_summary())

    @app.route("/api/storage_stats", methods=["GET"])
    def storage_list_api():
        try:
            limit = int(request.args.get("limit") or 200)
        except ValueError:
            limit = 200
        limit = max(1, min(5000, limit))
        return jsonify(encodes=list_storage_encodes(limit=limit))

    @app.route("/api/storage_stats/clear", methods=["POST"])
    def storage_clear_api():
        clear_storage_stats()
        return jsonify(ok=True)

    # ------------- Audio inventory / optimization -------------

    def _audio_job_source(job_id: str) -> tuple[dict | None, str]:
        for row in list_job_history_for_api(limit=5000):
            if str(row.get("id") or "") != str(job_id or ""):
                continue
            candidates = [row.get("out_path"), row.get("src")]
            for candidate in candidates:
                path = str(candidate or "").strip()
                if path and os.path.isfile(path) and is_allowed_path(path):
                    return row, path
            return row, ""
        return None, ""

    def _audio_scan_response(path: str, payload: dict | None = None) -> dict:
        payload = payload if isinstance(payload, dict) else {}
        # The planner needs stream identity and an actionable size estimate,
        # not a blocking packet-perfect traversal of a multi-gigabyte file.
        # Exact scans remain available to explicit callers and completion QA.
        inventory = scan_media(
            path,
            exact=_truthy(payload.get("exact"), False),
            force=_truthy(payload.get("force"), False),
        )
        operations = payload.get("operations") if isinstance(payload.get("operations"), dict) else {}
        operations = dict(operations)
        if payload.get("audio_policy"):
            operations["audio_policy"] = payload.get("audio_policy")
        if payload.get("job_type"):
            operations["job_type"] = payload.get("job_type")
        operations.setdefault("default_target_codec", load_settings().get("audio_optimize_codec") or "aac")
        return {
            "ok": True,
            "path": path,
            "inventory": inventory,
            "operations": normalize_operations(operations, inventory),
        }

    @app.route("/api/audio/scan", methods=["POST"])
    def audio_scan_api():
        data = request.get_json(silent=True) or {}
        path = str(data.get("src") or "").strip()
        if data.get("job_id") and not path:
            _row, path = _audio_job_source(str(data.get("job_id") or ""))
        if not path or not os.path.isfile(path):
            return jsonify(error="media file is missing"), 404
        if not is_allowed_path(path):
            return jsonify(error="path not allowed"), 400
        try:
            result = _audio_scan_response(path, data)
            if data.get("job_id"):
                update_encode_output_inventory(str(data.get("job_id")), result["inventory"])
            return jsonify(**result)
        except Exception as exc:
            return jsonify(error=f"audio scan failed: {str(exc)[:300]}"), 500

    def _queue_audio_path(path: str, data: dict, *, parent_job_id: str = "") -> dict:
        if not path or not os.path.isfile(path):
            raise ValueError("the media file no longer exists")
        if not is_allowed_path(path):
            raise ValueError("path not allowed")
        inventory = scan_media(path, exact=False)
        requested = data.get("operations") if isinstance(data.get("operations"), dict) else {}
        requested = dict(requested)
        requested.update({
            "job_type": "audio_only",
            "video_action": "copy",
            "subtitle_action": "copy",
            "replace_source": True,
        })
        requested.setdefault("audio_policy", "optimize_lossless")
        requested.setdefault("default_target_codec", load_settings().get("audio_optimize_codec") or "aac")
        operations = normalize_operations(requested, inventory)
        if not any(row.get("action") != "copy" for row in operations.get("audio_actions") or []):
            raise ValueError("no audio tracks are selected for optimization or removal")
        mode = str(data.get("mode") or "local").strip().lower()
        dispatch_mode = "auto" if mode in {"auto", "best", "available", "next_available"} else "local"
        preset = guess_preset_from_filename(os.path.basename(path))
        child_id = create_job(
            path,
            preset,
            encode_metadata={
                "job_type": "audio_only",
                "parent_job_id": str(parent_job_id),
                "encode_method": "ffmpeg_audio_only",
                "encoder": "copy",
                "encoder_family": "stream_copy",
                "video_codec": str(((inventory.get("video_streams") or [{}])[0]).get("codec") or "copy"),
            },
            dispatch_mode=dispatch_mode,
            preset_selection="audio-only",
            preset_adaptive=False,
            operations=operations,
            parent_job_id=str(parent_job_id),
        )
        if dispatch_mode == "auto":
            _wake_auto_node_dispatch()
        return {
            "ok": True,
            "job_id": child_id,
            "parent_job_id": str(parent_job_id),
            "path": path,
            "operations": operations,
            "dispatch_mode": dispatch_mode,
        }

    def _queue_audio_child(job_id: str, data: dict) -> dict:
        parent, path = _audio_job_source(job_id)
        if not parent:
            raise ValueError("completed job was not found")
        if not path:
            raise ValueError("the completed output file no longer exists")
        if str(parent.get("status") or "") not in {"done", "complete", "completed"}:
            raise ValueError("audio optimization is available after the original job completes")
        return _queue_audio_path(path, data, parent_job_id=str(job_id))

    @app.route("/api/jobs/<job_id>/optimize-audio", methods=["POST"])
    def job_optimize_audio_api(job_id):
        try:
            return jsonify(**_queue_audio_child(job_id, request.get_json(silent=True) or {}))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except Exception as exc:
            return jsonify(error=f"audio optimization could not be queued: {str(exc)[:300]}"), 500

    @app.route("/api/audio/queue", methods=["POST"])
    def audio_queue_api():
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(**_queue_audio_path(str(data.get("src") or "").strip(), data))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except Exception as exc:
            return jsonify(error=f"audio optimization could not be queued: {str(exc)[:300]}"), 500

    @app.route("/api/mobile/v1/audio/scan", methods=["POST"])
    def mobile_audio_scan_api():
        if not _authenticated_mobile("read"):
            return jsonify(error="unauthorized mobile device"), 401
        data = request.get_json(silent=True) or {}
        path = str(data.get("src") or "").strip()
        if data.get("job_id") and not path:
            _row, path = _audio_job_source(str(data.get("job_id") or ""))
        if not path or not os.path.isfile(path) or not is_allowed_path(path):
            return jsonify(error="media file is missing or not allowed"), 404
        try:
            result = _audio_scan_response(path, data)
            if data.get("job_id"):
                update_encode_output_inventory(str(data.get("job_id")), result["inventory"])
            return jsonify(**result)
        except Exception as exc:
            return jsonify(error=f"audio scan failed: {str(exc)[:300]}"), 500

    @app.route("/api/mobile/v1/jobs/<job_id>/optimize-audio", methods=["POST"])
    def mobile_job_optimize_audio_api(job_id):
        if not _authenticated_mobile("control"):
            return jsonify(error="control permission required"), 403
        try:
            return jsonify(**_queue_audio_child(job_id, request.get_json(silent=True) or {}))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except Exception as exc:
            return jsonify(error=f"audio optimization could not be queued: {str(exc)[:300]}"), 500

    @app.route("/api/mobile/v1/audio/queue", methods=["POST"])
    def mobile_audio_queue_api():
        if not _authenticated_mobile("control"):
            return jsonify(error="control permission required"), 403
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(**_queue_audio_path(str(data.get("src") or "").strip(), data))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except Exception as exc:
            return jsonify(error=f"audio optimization could not be queued: {str(exc)[:300]}"), 500


    # ------------- Directory listing -------------

    @app.route("/list")
    def list_path():
        """List folders + video files for a given path (used by the browser UI)."""
        path = request.args.get("path")
        if not path:
            return jsonify(error="missing path"), 400

        if not is_allowed_path(path) or not os.path.isdir(path):
            return jsonify(error="path not allowed or not a directory"), 400

        try:
            entries = os.listdir(path)
        except Exception as e:
            return jsonify(error=str(e)), 500

        dirs = sorted([e for e in entries if os.path.isdir(os.path.join(path, e))])
        files = sorted(
            [
                e
                for e in entries
                if os.path.isfile(os.path.join(path, e))
                and e.lower().endswith(VIDEO_EXTS)
            ]
        )
        file_details = {}
        for name in files:
            full = os.path.join(path, name)
            try:
                stat = os.stat(full)
                file_details[name] = {
                    "size_bytes": int(stat.st_size),
                    "mtime": float(stat.st_mtime),
                    "codec_hint": (
                        "AV1" if re.search(r"\bav1\b", name, re.IGNORECASE)
                        else "HEVC" if re.search(r"\b(?:hevc|h[ ._-]*265|x265)\b", name, re.IGNORECASE)
                        else "H.264" if re.search(r"\b(?:h[ ._-]*264|x264)\b", name, re.IGNORECASE)
                        else ""
                    ),
                }
            except Exception:
                file_details[name] = {"size_bytes": 0, "mtime": 0, "codec_hint": ""}

        return jsonify(path=path, dirs=dirs, files=files, file_details=file_details)

    # ------------- Single encode -------------

    @app.route("/encode", methods=["POST"])
    def encode():
        """Queue a single file for encoding."""
        data = request.get_json(force=True)
        src = data.get("src")
        preset = data.get("preset") or "1080"

        if not src or not os.path.isfile(src):
            return jsonify(error="invalid src"), 400

        if not is_allowed_path(src):
            return jsonify(error="path not allowed"), 400

        if preset not in ("1080", "4k", "auto", "smart"):
            return jsonify(error="invalid preset"), 400

        base = os.path.basename(src)
        name_only, _ext = os.path.splitext(base)
        requested_operations = data.get("operations") if isinstance(data.get("operations"), dict) else {}
        audio_only = str(requested_operations.get("job_type") or "").strip().lower() == "audio_only"
        if name_only.lower().endswith("-tsd") and not audio_only:
            return jsonify(error="file already tagged -TSD, not queuing"), 400

        if preset == "smart":
            try:
                job_id, recommendation = _create_smart_job(
                    src,
                    operations=data.get("operations") if isinstance(data.get("operations"), dict) else None,
                )
            except ValueError as exc:
                return jsonify(error=str(exc)), 400
            except Exception as exc:
                return jsonify(error=f"smart preset planning failed: {exc}"), 500
            return jsonify(
                job_id=job_id,
                preset="smart",
                smart_candidate_id=recommendation.get("recommended_id"),
                learning=recommendation.get("learning"),
            )

        if preset == "auto":
            preset = guess_preset_from_filename(base)

        job_id = create_job(
            src,
            preset,
            operations=data.get("operations") if isinstance(data.get("operations"), dict) else None,
        )
        return jsonify(job_id=job_id)

    @app.route("/encode_wizard", methods=["POST"])
    def encode_wizard():
        """Queue a Wizard plan and teach Smart Presets from that approval."""
        data = request.get_json(force=True)
        try:
            return jsonify(**_queue_wizard_job(data))
        except ValueError as e:
            return jsonify(error=str(e)), 400
        except Exception as e:
            return jsonify(error=str(e)), 500

    # ------------- Wizard preview helpers -------------

    @app.route("/probe")
    def probe():
        """Probe a media file to get basic info (duration/resolution/fps)."""
        src = request.args.get("src")
        if not src or not os.path.isfile(src):
            return jsonify(error="invalid src"), 400
        if not is_allowed_path(src):
            return jsonify(error="path not allowed"), 400
        try:
            info = _probe_media(src)
            return jsonify(src=src, info=info)
        except Exception as e:
            return jsonify(error=str(e)), 500


    
    def _ffmpeg_extract_jpg_scaled(src_path: str, t_sec: int, out_jpg: str, w: int, h: int):
        """Extract a single JPEG frame at integer second t_sec, optionally scaled."""
        # Use integer seconds to avoid keyframe seek surprises.
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-ss", str(int(t_sec)),
            "-i", src_path,
            "-frames:v", "1",
            "-vf", f"scale={int(w)}:{int(h)}:flags=bicubic",
            "-q:v", "2",
            "-y", out_jpg,
        ]
        ok, out, err = _run_cmd(cmd)
        if not ok or not os.path.isfile(out_jpg):
            raise RuntimeError((err or out or "ffmpeg failed").strip())


    def _choose_preview_start_second(duration_sec: float, preview_seconds: int = 1) -> int:
        """Pick a random preview point away from intros and credits."""
        try:
            duration = float(duration_sec or 0)
        except Exception:
            duration = 0.0
        if duration <= 0:
            return 0

        clip_room = max(1.0, float(preview_seconds or 1)) + 2.0
        edge_guard = max(10.0, duration * 0.10)

        # For normal movie/show lengths, sample the middle 80%.
        min_start = min(edge_guard, max(0.0, duration - clip_room))
        max_start = max(min_start, duration - edge_guard - clip_room)

        # Very short clips may not have enough middle; use the safest center-ish point.
        if max_start <= min_start:
            center = max(0.0, (duration - clip_room) * 0.5)
            return int(center)

        span = int(max_start - min_start)
        if span <= 0:
            return int(min_start)
        return int(min_start) + secrets.randbelow(span + 1)


    def _ffmpeg_make_side_by_side_preview(src_path: str, start_sec: int, seconds: int, encoded_path: str, out_mp4: str, layout: str = "side_by_side"):
        """Create a browser-friendly original/transcoded comparison clip."""
        target_h = 540
        duration = max(1, int(seconds or 1))
        if layout == "split_frame":
            filter_complex = (
                f"[0:v]setpts=PTS-STARTPTS,scale=-2:{target_h}:flags=bicubic,setsar=1[left];"
                f"[1:v]setpts=PTS-STARTPTS,scale=-2:{target_h}:flags=bicubic,setsar=1[right];"
                "[left]crop=iw/2:ih:0:0[left_half];"
                "[right]crop=iw/2:ih:iw/2:0[right_half];"
                "[left_half][right_half]hstack=inputs=2,format=yuv420p[v]"
            )
        else:
            filter_complex = (
                f"[0:v]setpts=PTS-STARTPTS,scale=-2:{target_h}:flags=bicubic,setsar=1[left];"
                f"[1:v]setpts=PTS-STARTPTS,scale=-2:{target_h}:flags=bicubic,setsar=1[right];"
                "[left][right]hstack=inputs=2,format=yuv420p[v]"
            )
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-ss", str(max(0, int(start_sec or 0))),
            "-t", str(duration),
            "-i", src_path,
            "-i", encoded_path,
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-an",
            "-t", str(duration),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "18",
            "-movflags", "+faststart",
            out_mp4,
        ]
        ok, out, err = _run_cmd(cmd)
        if not ok or not os.path.isfile(out_mp4):
            raise RuntimeError((err or out or "ffmpeg side-by-side preview failed").strip()[:800])


    def _remove_preview_clip(path: str):
        try:
            os.remove(path)
        except Exception:
            pass
        try:
            parent = os.path.dirname(path)
            if os.path.basename(parent).startswith("hbwiz_"):
                os.rmdir(parent)
        except Exception:
            pass


    def _register_preview_clip(token: str, path: str):
        # Keep a small in-memory registry so we can serve clips by token.
        # Clips are cleaned up lazily on new preview requests.
        now = time.time()
        stale_paths: list[str] = []
        with PREVIEW_CLIPS_LOCK:
            PREVIEW_CLIPS[token] = (path, now)

            # Lazy cleanup (keep up to 30 clips or 30 minutes)
            try:
                if len(PREVIEW_CLIPS) > 30:
                    # Remove oldest
                    for tok, (p, _ts) in sorted(PREVIEW_CLIPS.items(), key=lambda kv: kv[1][1])[:-30]:
                        PREVIEW_CLIPS.pop(tok, None)
                        stale_paths.append(p)
                cutoff = now - (30 * 60)
                for tok, (p, ts) in list(PREVIEW_CLIPS.items()):
                    if ts < cutoff:
                        PREVIEW_CLIPS.pop(tok, None)
                        stale_paths.append(p)
            except Exception:
                pass
        for stale_path in stale_paths:
            _remove_preview_clip(stale_path)


    @app.route("/wizard_preview_clip/<token>", methods=["GET"])
    def wizard_preview_clip(token):
        """Serve a previously generated preview MP4 clip."""
        with PREVIEW_CLIPS_LOCK:
            item = PREVIEW_CLIPS.get(token)
        if not item:
            abort(404)
        path, _ts = item
        if not os.path.isfile(path):
            abort(404)
        return send_file(path, mimetype="video/mp4", as_attachment=False)


    @app.route("/wizard_preview_fast_images", methods=["POST"])
    def wizard_preview_fast_images():
        """Fast visual compare: OLD frame vs NEW (scaled) frame. No HandBrake."""
        data = request.get_json(force=True) or {}
        src = data.get("src")
        if not src or not os.path.isfile(src):
            return jsonify(error="invalid src"), 400
        if not is_allowed_path(src):
            return jsonify(error="path not allowed"), 400

        # Make sure ffmpeg exists
        ok_ff, _o, _e = _run_cmd(["ffmpeg", "-version"])
        if not ok_ff:
            return jsonify(error="ffmpeg not found in container (needed for previews)"), 500

        try:
            plan = _wizard_plan(data, probe_func=_ffprobe_media_fast, preview=True)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        except Exception as e:
            return jsonify(error=str(e)), 500

        info = plan["probe"]
        duration_sec = float(info.get("duration_sec") or 0.0)

        # Preview timestamp (integer seconds), randomized away from intro/credits.
        t_int = _choose_preview_start_second(duration_sec)

        out_res = plan["estimates"]["output_resolution"]
        out_w = int(out_res.get("width") or 0)
        out_h = int(out_res.get("height") or 0)
        decision = plan["estimates"].get("decision") or "keep"

        # Temp outputs
        tmpdir = tempfile.mkdtemp(prefix="hbwiz_fast_")
        token = uuid.uuid4().hex
        old_jpg = os.path.join(tmpdir, f"old_{token}.jpg")
        new_jpg = os.path.join(tmpdir, f"new_{token}.jpg")

        try:
            _ffmpeg_extract_jpg(src, t_int, old_jpg)
            _ffmpeg_extract_jpg_scaled(src, t_int, new_jpg, out_w, out_h)
            return jsonify(
                ok=True,
                mode="fast",
                t_seconds=float(t_int),
                decision=decision,
                out_w=out_w,
                out_h=out_h,
                old_b64=_b64_jpg(old_jpg),
                new_b64=_b64_jpg(new_jpg),
            )
        finally:
            # Best-effort cleanup
            try:
                os.remove(old_jpg)
                os.remove(new_jpg)
                os.rmdir(tmpdir)
            except Exception:
                pass



    def _run_accurate_preview_task(preview_id: str, data: dict) -> None:
        tmpdir = ""
        pidfile = _preview_pidfile(preview_id)
        proc = None

        def fail(message: str, detail: str = "") -> None:
            text = f"{message}: {detail}" if detail else message
            _preview_set_task(preview_id, state="error", progress=100.0, message=text[:1200], error=text[:1200])

        try:
            _preview_cleanup_tasks()
            _preview_set_task(preview_id, state="planning", progress=3.0, message="Planning accurate preview...")

            try:
                plan = _wizard_plan(data, probe_func=_ffprobe_media_fast, preview=True)
            except Exception as e:
                fail("Preview planning failed", str(e))
                return

            qsv_reason = ""
            used_fallback = False
            options = plan.get("options") if isinstance(plan.get("options"), dict) else {}
            if options.get("encoder_family") == "qsv":
                qsv_ok, qsv_reason = _qsv_preview_available()
                if not qsv_ok:
                    used_fallback = True
                    _preview_set_task(preview_id, progress=8.0, message=f"QSV unavailable ({qsv_reason}); using software preview...")
                    try:
                        plan = _wizard_plan(_software_preview_payload(data), probe_func=_ffprobe_media_fast, preview=True)
                    except Exception as e:
                        fail("Software preview planning failed", str(e))
                        return

            src = plan["src"]
            base = os.path.basename(src)
            info = plan["probe"]
            duration_sec = float(info.get("duration_sec") or 0.0)
            estimates = plan["estimates"]
            out_res = estimates["output_resolution"]
            out_w = int(out_res.get("width") or 0)
            out_h = int(out_res.get("height") or 0)
            decision = estimates.get("decision") or "keep"
            video_kbps = float(estimates.get("video_bitrate_kbps") or 0)
            encoder_preset = estimates.get("encoder_preset") or ""
            encoder_label = estimates.get("encoder_label") or estimates.get("encoder") or ""
            preview_layout = "split_frame" if str(data.get("accurate_preview_layout") or "").strip() == "split_frame" else "side_by_side"

            preview_seconds = int(max(4, min(8, duration_sec - 1)))
            t_int = _choose_preview_start_second(duration_sec, preview_seconds)
            preview_seconds = int(max(4, min(8, duration_sec - t_int - 1)))

            tmpdir = tempfile.mkdtemp(prefix="hbwiz_acc_")
            token = uuid.uuid4().hex
            out_clip = os.path.join(tmpdir, f"clip_{token}.mp4")
            compare_clip = os.path.join(tmpdir, f"compare_{token}.mp4")
            old_jpg = os.path.join(tmpdir, f"old_{token}.jpg")
            new_jpg = os.path.join(tmpdir, f"new_{token}.jpg")

            try:
                _effective, preset_a = _hb_preset_args_for_base(plan["preset"], base)
            except Exception as e:
                fail("Preset setup failed", str(e))
                return

            hb_cmd = [
                "HandBrakeCLI",
                "-i", src,
                "-o", out_clip,
                "--start-at", f"duration:{t_int}",
                "--stop-at", f"duration:{preview_seconds}",
            ] + preset_a + _flatten_args(plan["extra_args"]) + ["-a", "none"]

            _kill_preview_by_id(preview_id)
            _preview_set_task(
                preview_id,
                state="encoding",
                progress=18.0,
                message=f"Encoding {preview_seconds}s preview with {encoder_label or estimates.get('encoder') or 'HandBrake'}...",
                encoder=estimates.get("encoder"),
                encoder_label=encoder_label,
                encoder_preset=encoder_preset,
                fallback=used_fallback,
                qsv_reason=qsv_reason,
            )

            proc = subprocess.Popen(
                hb_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
                bufsize=1,
            )
            try:
                with open(pidfile, "w", encoding="utf-8") as f:
                    f.write(str(proc.pid))
            except Exception:
                pass

            lines: list[str] = []
            started = time.time()
            for line in proc.stdout or []:
                lines.append(line)
                if len(lines) > 120:
                    lines = lines[-120:]
                match = PREVIEW_PROGRESS_RE.search(line)
                if match:
                    try:
                        hb_pct = max(0.0, min(100.0, float(match.group(1))))
                    except Exception:
                        hb_pct = 0.0
                    _preview_set_task(
                        preview_id,
                        state="encoding",
                        progress=18.0 + (hb_pct * 0.64),
                        message=f"Encoding accurate preview... {hb_pct:.1f}%",
                    )
                elif time.time() - started > 180:
                    raise TimeoutError("accurate preview timed out")

            ret = proc.wait(timeout=10)
            if ret != 0 or not os.path.isfile(out_clip):
                snippet = "".join(lines).strip().replace("\r", "\n")[-1200:]
                if options.get("encoder_family") == "qsv" and not used_fallback:
                    _preview_set_task(preview_id, progress=82.0, message="QSV preview failed; retrying with software...")
                    fallback_data = _software_preview_payload(data)
                    fallback_data["preview_id"] = preview_id
                    _run_accurate_preview_task(preview_id, fallback_data)
                    return
                fail("HandBrake accurate preview failed", snippet or f"exit {ret}")
                return

            _preview_set_task(preview_id, state="framing", progress=84.0, message="Extracting matched preview frames...")
            within = min(max(1.0, preview_seconds * 0.5), max(0.5, preview_seconds - 0.5))
            _ffmpeg_extract_jpg_precise(src, t_int + within, old_jpg)
            _ffmpeg_extract_jpg_precise(out_clip, within, new_jpg)

            _preview_set_task(preview_id, state="muxing", progress=91.0, message="Building comparison clip...")
            _ffmpeg_make_side_by_side_preview(src, t_int, preview_seconds, out_clip, compare_clip, preview_layout)
            try:
                os.remove(out_clip)
            except Exception:
                pass
            _register_preview_clip(token, compare_clip)
            feedback_context = smart_feedback_context(plan, str(data.get("smart_candidate_id") or "manual"))
            feedback_token = _register_smart_feedback_context(feedback_context)

            result = {
                "ok": True,
                "preview_id": preview_id,
                "t_seconds": t_int,
                "frame_seconds": t_int + within,
                "seconds": preview_seconds,
                "preview_seconds": preview_seconds,
                "preset": plan["preset"],
                "decision": decision,
                "out_width": out_w,
                "out_height": out_h,
                "bitrate_kbps": int(video_kbps),
                "encoder": estimates.get("encoder"),
                "encoder_label": encoder_label,
                "encoder_preset": encoder_preset,
                "clip_url": f"/wizard_preview_clip/{token}",
                "clip_layout": preview_layout,
                "old_b64": _b64_jpg(old_jpg),
                "new_b64": _b64_jpg(new_jpg),
                "fallback": used_fallback,
                "qsv_reason": qsv_reason,
                "smart_feedback_token": feedback_token,
                "smart_profile_id": "default",
                "smart_candidate_id": str(data.get("smart_candidate_id") or "manual")[:40],
            }
            _preview_set_task(preview_id, state="done", progress=100.0, message="Accurate preview ready.", result=result)
        except Exception as e:
            try:
                if proc and proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                pass
            fail("Accurate preview failed", str(e))
        finally:
            try:
                os.remove(pidfile)
            except Exception:
                pass
            if tmpdir:
                for name in os.listdir(tmpdir) if os.path.isdir(tmpdir) else []:
                    path = os.path.join(tmpdir, name)
                    if path.endswith(".jpg"):
                        try:
                            os.remove(path)
                        except Exception:
                            pass
                try:
                    os.rmdir(tmpdir)
                except Exception:
                    pass

    def _start_library_smart_preview(data: dict | None = None, *, actor: str = "web") -> dict:
        """Start a real Smart Preset comparison for one Library source."""
        data = data if isinstance(data, dict) else {}
        src = str(data.get("src") or "").strip()
        if not src or not os.path.isfile(src):
            raise ValueError("The selected Library file is no longer available.")
        if not is_allowed_path(src):
            raise ValueError("The selected file is outside the mapped Library folders.")
        if not src.lower().endswith(VIDEO_EXTS):
            raise ValueError("The selected Library item is not a supported video file.")

        recommendation = _smart_recommendation(
            {
                "src": src,
                "preset": "auto",
                "smart_tuning": data.get("smart_tuning") or {},
            }
        )
        plan = recommendation.pop("selected_plan")
        candidate_id = str(recommendation.get("recommended_id") or "balanced")
        candidate = next(
            (row for row in recommendation.get("candidates") or [] if row.get("id") == candidate_id),
            (recommendation.get("candidates") or [{}])[0],
        )
        preview_data = {
            "src": src,
            **_wizard_public_options(plan.get("options") or {}),
            "target_size_auto": False,
            "target_size_value": float((plan.get("inputs") or {}).get("target_mb") or 1.0),
            "target_size_unit": "MB",
            "smart_candidate_id": candidate_id,
            "accurate_preview_layout": "side_by_side",
        }
        preview_id = f"library_{uuid.uuid4().hex}"
        _kill_preview_by_id(preview_id)
        _preview_set_task(
            preview_id,
            state="queued",
            progress=0.0,
            message="Queued Library Smart preview.",
            result=None,
            error="",
        )
        thread = threading.Thread(
            target=_run_accurate_preview_task,
            args=(preview_id, preview_data),
            name=f"library-preview-{preview_id[-8:]}",
            daemon=True,
        )
        thread.start()
        title = os.path.splitext(os.path.basename(src))[0]
        log_event(
            "library_preview_started",
            f"{actor} started a Smart Preset preview for {title}.",
            level="info",
        )
        return {
            "preview_id": preview_id,
            "title": title,
            "candidate_id": candidate_id,
            "candidate_name": candidate.get("name") or candidate_id,
            "candidate_summary": candidate.get("summary") or "",
            "tuning": recommendation.get("tuning") or {},
        }

    def _library_preview_status(preview_id: str) -> dict:
        clean_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(preview_id or ""))
        if not clean_id or not clean_id.startswith("library_"):
            raise KeyError("preview not found")
        row = _preview_get_task(clean_id)
        if not row:
            raise KeyError("preview not found")
        return row

    @app.route("/api/library/preview", methods=["POST"])
    def library_preview_start_api():
        try:
            preview = _start_library_smart_preview(
                request.get_json(silent=True) or {},
                actor="Web Library",
            )
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except Exception as exc:
            return jsonify(error=f"Could not start Library preview: {exc}"), 500
        return jsonify(ok=True, preview=preview), 202

    @app.route("/api/library/preview/<preview_id>")
    def library_preview_status_api(preview_id):
        try:
            return jsonify(ok=True, preview=_library_preview_status(preview_id))
        except KeyError:
            return jsonify(error="preview not found"), 404

    @app.route("/api/mobile/v1/library/preview", methods=["POST"])
    def mobile_library_preview_start_api():
        device = _authenticated_mobile("control")
        if not device:
            return jsonify(error="control permission required"), 403
        try:
            preview = _start_library_smart_preview(
                request.get_json(silent=True) or {},
                actor=str(device.get("name") or "ByteSqueeze"),
            )
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except Exception as exc:
            return jsonify(error=f"Could not start Library preview: {exc}"), 500
        return jsonify(ok=True, preview=preview), 202

    @app.route("/api/mobile/v1/library/preview/<preview_id>")
    def mobile_library_preview_status_api(preview_id):
        if not _authenticated_mobile("read"):
            return jsonify(error="unauthorized mobile device"), 401
        try:
            return jsonify(ok=True, preview=_library_preview_status(preview_id))
        except KeyError:
            return jsonify(error="preview not found"), 404

    def _start_autopilot_review(data: dict | None = None, *, actor: str = "web") -> dict:
        data = data if isinstance(data, dict) else {}
        samples = _autopilot_review_samples()
        if not samples:
            raise ValueError("No unencoded movie or episode is available in the mapped library. Refresh the library first.")

        with AUTOPILOT_REVIEW_LOCK:
            existing_id = str(AUTOPILOT_REVIEW_STATE.get("preview_id") or "")
            existing_task = _preview_get_task(existing_id) if existing_id else None
            force_next = bool(data.get("next"))
            if (
                existing_task
                and not force_next
                and not AUTOPILOT_REVIEW_STATE.get("reviewed")
                and existing_task.get("state") in {"queued", "planning", "encoding", "framing", "muxing", "done"}
            ):
                return _autopilot_review_payload(request.host_url)

            cursor = int(AUTOPILOT_REVIEW_STATE.get("cursor") or 0)
            if force_next or AUTOPILOT_REVIEW_STATE.get("reviewed"):
                cursor += 1
            requested_path = str(data.get("path") or "").strip()
            selected = next((row for row in samples if row["path"] == requested_path), None)
            if selected is None:
                selected = samples[cursor % len(samples)]

        state = load_smart_preset_state()
        profile = state.get("profile") if isinstance(state.get("profile"), dict) else {}
        if not profile.get("automation_enabled"):
            save_smart_preset_profile({"automation_enabled": True})

        recommendation = _smart_recommendation({"src": selected["path"], "preset": "auto"})
        plan = recommendation.pop("selected_plan")
        candidate_id = str(recommendation.get("recommended_id") or "balanced")
        candidate = next(
            (row for row in recommendation.get("candidates") or [] if row.get("id") == candidate_id),
            (recommendation.get("candidates") or [{}])[0],
        )
        preview_data = {
            "src": selected["path"],
            **_wizard_public_options(plan.get("options") or {}),
            "target_size_auto": False,
            "target_size_value": float((plan.get("inputs") or {}).get("target_mb") or 1.0),
            "target_size_unit": "MB",
            "smart_candidate_id": candidate_id,
            "accurate_preview_layout": "side_by_side",
        }
        preview_id = f"autopilot_{uuid.uuid4().hex}"
        _kill_preview_by_id(preview_id)
        _preview_set_task(preview_id, state="queued", progress=0.0, message="Queued Autopilot comparison preview.", result=None, error="")
        with AUTOPILOT_REVIEW_LOCK:
            AUTOPILOT_REVIEW_STATE.clear()
            AUTOPILOT_REVIEW_STATE.update(
                {
                    "cursor": cursor,
                    "review_id": uuid.uuid4().hex,
                    "preview_id": preview_id,
                    "sample_id": selected["id"],
                    "path": selected["path"],
                    "title": selected["title"],
                    "media_type": selected["media_type"],
                    "poster_url": selected.get("poster_url") or "",
                    "year": selected.get("year"),
                    "size_bytes": selected.get("size_bytes") or 0,
                    "candidate_id": candidate_id,
                    "candidate_name": candidate.get("name") or candidate_id,
                    "candidate_summary": candidate.get("summary") or "",
                    "candidate_plan": candidate.get("plan") or {},
                    "reviewed": False,
                    "created_at": time.time(),
                }
            )
        thread = threading.Thread(
            target=_run_accurate_preview_task,
            args=(preview_id, preview_data),
            name=f"autopilot-preview-{preview_id[-8:]}",
            daemon=True,
        )
        thread.start()
        log_event("autopilot_preview_started", f"{actor} started Smart Preset training for {selected['title']}.", level="info")
        return _autopilot_review_payload(request.host_url)

    def _submit_autopilot_review(data: dict | None = None, *, actor: str = "web") -> dict:
        data = data if isinstance(data, dict) else {}
        review = _autopilot_review_payload(request.host_url)
        preview = review.get("preview") if isinstance(review.get("preview"), dict) else {}
        result = preview.get("result") if isinstance(preview.get("result"), dict) else {}
        if preview.get("state") != "done" or not result:
            raise ValueError("The accurate preview is not ready to review yet.")
        context = _consume_smart_feedback_context(result.get("smart_feedback_token") or "")
        if not context:
            raise RuntimeError("This preview feedback expired or was already submitted.")
        feedback = record_smart_preset_feedback(context, data.get("verdict"), data.get("reason"))
        with AUTOPILOT_REVIEW_LOCK:
            AUTOPILOT_REVIEW_STATE.update(
                {
                    "reviewed": True,
                    "verdict": feedback.get("feedback", {}).get("verdict"),
                    "reason": feedback.get("feedback", {}).get("reason"),
                    "reviewed_at": time.time(),
                }
            )
        log_event(
            "autopilot_preview_reviewed",
            f"{actor} submitted {AUTOPILOT_REVIEW_STATE.get('verdict')} feedback for {AUTOPILOT_REVIEW_STATE.get('title') or 'media'}.",
            level="info",
        )
        payload = _autopilot_review_payload(request.host_url)
        payload["learning"] = feedback.get("learning") or smart_learning_status()
        return payload

    @app.route("/api/autopilot/review", methods=["GET", "POST"])
    def autopilot_review_api():
        if request.method == "POST":
            try:
                review = _start_autopilot_review(request.get_json(silent=True) or {}, actor="Web dashboard")
            except ValueError as exc:
                return jsonify(error=str(exc)), 400
            except Exception as exc:
                return jsonify(error=f"Could not start Autopilot preview: {exc}"), 500
        else:
            review = _autopilot_review_payload(request.host_url)
        return jsonify(ok=True, review=review)

    @app.route("/api/autopilot/review/feedback", methods=["POST"])
    def autopilot_review_feedback_api():
        try:
            review = _submit_autopilot_review(request.get_json(silent=True) or {}, actor="Web dashboard")
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 409
        return jsonify(ok=True, review=review)

    @app.route("/api/mobile/v1/autopilot/review", methods=["GET", "POST"])
    def mobile_autopilot_review_api():
        required_scope = "control" if request.method == "POST" else "read"
        device = _authenticated_mobile(required_scope)
        if not device:
            return jsonify(error="control permission required" if request.method == "POST" else "unauthorized mobile device"), (403 if request.method == "POST" else 401)
        if request.method == "POST":
            try:
                review = _start_autopilot_review(request.get_json(silent=True) or {}, actor=str(device.get("name") or "ByteSqueeze"))
            except ValueError as exc:
                return jsonify(error=str(exc)), 400
            except Exception as exc:
                return jsonify(error=f"Could not start Autopilot preview: {exc}"), 500
        else:
            review = _autopilot_review_payload(request.host_url)
        return jsonify(ok=True, review=review)

    @app.route("/api/mobile/v1/autopilot/review/feedback", methods=["POST"])
    def mobile_autopilot_review_feedback_api():
        device = _authenticated_mobile("control")
        if not device:
            return jsonify(error="control permission required"), 403
        try:
            review = _submit_autopilot_review(request.get_json(silent=True) or {}, actor=str(device.get("name") or "ByteSqueeze"))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 409
        return jsonify(ok=True, review=review)

    @app.route("/api/mobile/v1/autopilot/completed/<job_id>/feedback", methods=["POST"])
    def mobile_autopilot_completed_feedback_api(job_id):
        device = _authenticated_mobile("control")
        if not device:
            return jsonify(error="control permission required"), 403
        return _autopilot_completed_feedback_response(
            job_id,
            actor=str(device.get("name") or "ByteSqueeze"),
        )

    @app.route("/wizard_preview_accurate/start", methods=["POST"])
    def wizard_preview_accurate_start():
        data = request.get_json(force=True) or {}
        preview_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(data.get("preview_id") or uuid.uuid4().hex)) or uuid.uuid4().hex
        _kill_preview_by_id(preview_id)
        _preview_set_task(preview_id, state="queued", progress=0.0, message="Queued accurate preview.", result=None, error="")
        thread = threading.Thread(target=_run_accurate_preview_task, args=(preview_id, data), name=f"wizard-preview-{preview_id[:8]}", daemon=True)
        thread.start()
        return jsonify(ok=True, preview_id=preview_id)

    @app.route("/wizard_preview_accurate/status/<preview_id>")
    def wizard_preview_accurate_status(preview_id):
        row = _preview_get_task(preview_id)
        if not row:
            return jsonify(error="preview not found"), 404
        return jsonify(row)

    @app.route("/wizard_preview_accurate", methods=["POST"])
    def wizard_preview_accurate():
        data = request.get_json(force=True) or {}
        preview_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(data.get("preview_id") or uuid.uuid4().hex)) or uuid.uuid4().hex
        _kill_preview_by_id(preview_id)
        _preview_set_task(preview_id, state="queued", progress=0.0, message="Queued accurate preview.", result=None, error="")
        _run_accurate_preview_task(preview_id, data)
        row = _preview_get_task(preview_id) or {}
        if row.get("state") == "done" and isinstance(row.get("result"), dict):
            return jsonify(row["result"])
        return jsonify(error=row.get("error") or row.get("message") or "Accurate preview failed"), 500
    @app.route("/wizard_preview_images", methods=["POST"])
    def wizard_preview_images():
        return wizard_preview_accurate()


    @app.route("/wizard_ai_chat", methods=["POST"])
    def wizard_ai_chat():
        """Explain or safely adjust the current Size Wizard plan."""
        data = request.get_json(force=True) or {}
        question = str(data.pop("question", "") or "").strip()[:500]
        if not question:
            return jsonify(error="Ask the wizard AI a question first."), 400

        try:
            initial_plan = _wizard_plan(data, probe_func=_probe_media, preview=False)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        except Exception as e:
            return jsonify(error=str(e)), 500

        wizard_settings = load_settings()
        model_result = run_wizard_llm(question, initial_plan, wizard_settings)
        rule_updates = _wizard_ai_chat_updates(question)
        model_updates = _wizard_ai_sanitize_model_updates(model_result.get("updates")) if model_result.get("ok") and rule_updates else {}
        updates = {**model_updates, **rule_updates}
        plan = initial_plan
        if updates:
            try:
                plan = _wizard_plan({**data, **updates}, probe_func=_probe_media, preview=False)
            except Exception:
                updates = rule_updates
                plan = _wizard_plan({**data, **updates}, probe_func=_probe_media, preview=False) if updates else initial_plan

        reply = _wizard_ai_chat_answer(plan, question, changed=bool(updates))
        if model_result.get("ok"):
            model_answer = str(model_result.get("answer") or "").strip()
            if updates:
                model_answer += " " + reply["answer"].split(".", 1)[0] + "."
            reply["answer"] = model_answer
        return jsonify(
            ok=True,
            option_updates=updates,
            inputs=plan["inputs"],
            estimates=plan["estimates"],
            model_used=bool(model_result.get("ok")),
            model_status=model_result.get("status") or wizard_llm_status(wizard_settings),
            model_fallback_reason="" if model_result.get("ok") else str(model_result.get("error") or "model unavailable"),
            **reply,
        )

    @app.route("/wizard_ai_status")
    def wizard_ai_status_api():
        return jsonify(wizard_llm_status(load_settings()))



    @app.route("/wizard_preview", methods=["POST"])
    def wizard_preview():
        """Return an estimated outcome for Size Wizard inputs."""
        data = request.get_json(force=True) or {}
        try:
            plan = _wizard_plan(data, probe_func=_probe_media, preview=False)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        except Exception as e:
            return jsonify(error=str(e)), 500

        return jsonify(
            src=plan["src"],
            preset=plan["preset"],
            probe=plan["probe"],
            inputs=plan["inputs"],
            estimates=plan["estimates"],
            suggested_extra_args=plan["extra_args"],
        )


    # ------------- Job status -------------

    @app.route("/status/<job_id>")
    def status(job_id):
        """Return status + recent log for a specific job."""
        job = get_job(job_id)
        if not job:
            return jsonify(error="job not found"), 404
        return jsonify(job)

    # ------------- Cancel job -------------

    @app.route("/cancel/<job_id>", methods=["POST"])
    def cancel_route(job_id):
        """Cancel a running job or mark a queued one as canceled."""
        ok, err = cancel_job(job_id)
        if not ok:
            return jsonify(error=err or "cancel failed"), 400
        return jsonify(ok=True, job_id=job_id)

    # ------------- Job list -------------

    @app.route("/api/jobs")
    def jobs_list():
        """Return one coherent queue, summary, and durable-history snapshot."""
        _ensure_auto_node_dispatch_for_pending_jobs()
        items = _combined_jobs_for_api()
        return jsonify(
            jobs=items,
            summary=_combined_job_summary(),
            paused=get_queue_state(),
        )

    @app.route("/api/jobs/<job_id>/preset", methods=["POST"])
    def jobs_edit_queued_preset(job_id):
        """Replace a queued job snapshot only after an explicit user edit."""
        job = get_job(job_id)
        if not job:
            return jsonify(error="job not found on this node"), 404
        data = request.get_json(silent=True) or {}
        requested = str(data.get("preset") or data.get("selection") or "").strip().lower()
        if requested not in {"smart", "auto", "1080", "4k"}:
            return jsonify(error="preset must be smart, auto, 1080, or 4k"), 400
        try:
            plan = _node_queue_plan(str(job.get("src") or ""), requested, data.get("smart_tuning"))
        except Exception as exc:
            return jsonify(error=f"preset planning failed: {str(exc)[:180]}"), 400
        ok, error = replace_queued_job_preset(job_id, plan)
        if not ok:
            return jsonify(error=error or "preset edit failed"), 400
        _wake_auto_node_dispatch()
        updated = next((row for row in list_jobs_for_api() if row.get("id") == job_id), None)
        log_event(
            "queued_preset_edited",
            f"Queued preset changed to {requested} for {os.path.basename(str(job.get('src') or ''))}.",
            job_id=job_id,
            src=job.get("src"),
        )
        return jsonify(ok=True, job=updated)

    @app.route("/jobs/summary")
    def jobs_summary():
        """Return dashboard metrics for the jobs page."""
        _ensure_auto_node_dispatch_for_pending_jobs()
        return jsonify(summary=_combined_job_summary())

    @app.route("/jobs/clear_error_status", methods=["POST"])
    def jobs_clear_error_status():
        """Clear error jobs and archived error counters without touching successful savings totals."""
        cleared = clear_error_status()
        return jsonify(ok=True, cleared=cleared, summary=get_job_summary())

    # ------------- Job log download -------------

    @app.route("/job_log/<job_id>")
    def job_log(job_id):
        """Download the full log file for a given job."""
        if not get_job(job_id):
            abort(404)
        log_path = os.path.join(LOG_DIR, f"{job_id}.log")
        if not os.path.isfile(log_path):
            abort(404)
        return send_file(
            log_path,
            mimetype="text/plain",
            as_attachment=True,
            download_name=f"{job_id}.log",
        )

    # ------------- Batch rename -------------

    @app.route("/batch_rename", methods=["POST"])
    def batch_rename():
        """Rename all video files in a directory to add -TSD before the extension."""
        data = request.get_json(force=True)
        path = data.get("path")

        if not path or not os.path.isdir(path):
            return jsonify(error="invalid path"), 400

        if not is_allowed_path(path):
            return jsonify(error="path not allowed"), 400

        renamed = 0
        skipped = 0

        try:
            for entry in os.listdir(path):
                full = os.path.join(path, entry)
                if not os.path.isfile(full):
                    continue

                if not entry.lower().endswith(VIDEO_EXTS):
                    continue

                name, ext = os.path.splitext(entry)
                if name.lower().endswith("-tsd"):
                    skipped += 1
                    continue

                new_name = f"{name}-TSD{ext}"
                new_full = os.path.join(path, new_name)

                if os.path.exists(new_full):
                    skipped += 1
                    continue

                os.rename(full, new_full)
                renamed += 1
        except Exception as e:
            return jsonify(error=str(e)), 500

        return jsonify(renamed_count=renamed, skipped_count=skipped)

    # ------------- Batch encode (non-recursive) -------------

    @app.route("/batch_encode", methods=["POST"])
    def batch_encode():
        """Queue encode jobs for all video files in a folder (non-recursive)."""
        data = request.get_json(force=True)
        path = data.get("path")
        preset = data.get("preset") or "1080"
        dispatch_mode = str(data.get("mode") or "local").strip().lower()
        node_id = str(data.get("node_id") or "").strip()

        if not path or not os.path.isdir(path):
            return jsonify(error="invalid path"), 400

        if not is_allowed_path(path):
            return jsonify(error="path not allowed"), 400

        if preset not in ("1080", "4k", "auto", "smart"):
            return jsonify(error="invalid preset"), 400

        files = sorted(
            [
                e
                for e in os.listdir(path)
                if os.path.isfile(os.path.join(path, e))
                and e.lower().endswith(VIDEO_EXTS)
                and not os.path.splitext(e)[0].lower().endswith("-tsd")
            ]
        )

        if not files:
            return jsonify(error="no video files found"), 400

        paths = [os.path.join(path, entry) for entry in files]
        count, skipped = _queue_paths_to_destination(paths, preset, dispatch_mode, node_id, data.get("smart_tuning"))
        if not count and skipped:
            return jsonify(error=skipped[0].get("reason") or "no files could be queued", skipped=skipped), 400
        return jsonify(count=count, skipped=skipped)

    # ------------- Batch encode (recursive) -------------

    @app.route("/batch_encode_recursive", methods=["POST"])
    def batch_encode_recursive():
        """Queue encode jobs for all video files in a folder and all subfolders."""
        data = request.get_json(force=True)
        path = data.get("path")
        preset = data.get("preset") or "1080"
        dispatch_mode = str(data.get("mode") or "local").strip().lower()
        node_id = str(data.get("node_id") or "").strip()

        if not path or not os.path.isdir(path):
            return jsonify(error="invalid path"), 400

        if not is_allowed_path(path):
            return jsonify(error="path not allowed"), 400

        if preset not in ("1080", "4k", "auto", "smart"):
            return jsonify(error="invalid preset"), 400

        to_create = []
        for root, _dirs, files in os.walk(path):
            for entry in files:
                if not entry.lower().endswith(VIDEO_EXTS):
                    continue

                name, _ext = os.path.splitext(entry)
                if name.lower().endswith("-tsd"):
                    continue

                src = os.path.join(root, entry)
                effective_preset = guess_preset_from_filename(entry) if preset == "auto" else preset
                to_create.append((src, effective_preset))

        if not to_create:
            return jsonify(error="no video files found"), 400

        paths = [src for src, _effective_preset in to_create]
        count, skipped = _queue_paths_to_destination(paths, preset, dispatch_mode, node_id, data.get("smart_tuning"))
        if not count and skipped:
            return jsonify(error=skipped[0].get("reason") or "no files could be queued", skipped=skipped), 400
        return jsonify(count=count, skipped=skipped)

    # ------------- Clear finished jobs -------------

    @app.route("/clear_finished_jobs", methods=["POST"])
    def clear_finished_jobs_route():
        """Delete all terminal jobs (done/error/canceled), including worker rows."""
        local_removed = clear_finished_jobs_core()
        worker_clear = _clear_linked_worker_jobs(target="finished")
        return jsonify(
            removed=local_removed + int(worker_clear.get("removed") or 0),
            local_removed=local_removed,
            worker_removed=int(worker_clear.get("removed") or 0),
            workers=worker_clear.get("workers") or [],
        )

    # ------------- Clear queued jobs -------------

    @app.route("/clear_queued_jobs", methods=["POST"])
    def clear_queued_jobs_route():
        """Delete queued and canceled jobs without touching running jobs."""
        local_removed = clear_queued_jobs()
        worker_clear = _clear_linked_worker_jobs(target="queued")
        return jsonify(
            removed=local_removed + int(worker_clear.get("removed") or 0),
            local_removed=local_removed,
            worker_removed=int(worker_clear.get("removed") or 0),
            workers=worker_clear.get("workers") or [],
        )

    # ------------- Preset config (1080 / 4k mapping) -------------

    @app.route("/preset_config", methods=["GET", "POST"])
    def preset_config_route():
        """Get or update default preset files for 1080 and 4K."""
        if request.method == "GET":
            exposed = {
                "1080": {"file": preset_config["1080"]["file"]},
                "4k": {"file": preset_config["4k"]["file"]},
            }
            return jsonify(config=exposed)

        data = request.get_json(force=True) or {}
        changed = False

        for key in ("1080", "4k"):
            if key in data and isinstance(data[key], dict):
                current = preset_config.get(key)
                if not current:
                    continue
                file_val = data[key].get("file") or current["file"]
                name_val = current["name"]
                preset_config[key] = {"file": file_val, "name": name_val}
                changed = True

        if changed:
            save_preset_config()

        exposed = {
            "1080": {"file": preset_config["1080"]["file"]},
            "4k": {"file": preset_config["4k"]["file"]},
        }
        return jsonify(config=exposed)

    # ------------- preset uploads -------------

    @app.route("/api/presets/upload", methods=["POST"])
    def upload_preset_file():
        """Upload a HandBrake preset JSON file into PRESET_DIR."""
        if "preset_file" not in request.files:
            return jsonify(error="missing file field 'preset_file'"), 400

        f = request.files["preset_file"]
        if not f or f.filename == "":
            return jsonify(error="no file selected"), 400

        filename = secure_filename(f.filename)
        if not filename.lower().endswith(".json"):
            return jsonify(error="only .json preset files are supported"), 400

        contents = f.read()
        if not contents:
            return jsonify(error="empty file"), 400

        try:
            json.loads(contents.decode("utf-8") if isinstance(contents, bytes) else contents)
        except Exception:
            return jsonify(error="file is not valid JSON"), 400

        os.makedirs(PRESET_DIR, exist_ok=True)

        dest_path = os.path.join(PRESET_DIR, filename)
        try:
            with open(dest_path, "wb") as out_f:
                out_f.write(contents)
        except Exception as e:
            return jsonify(error=f"failed to save preset: {e}"), 500

        updated_files = list_preset_files()

        return jsonify(
            ok=True,
            filename=filename,
            preset_files=updated_files,
        )

    # ------------- Preset delete -------------

    @app.route("/api/presets/delete", methods=["POST"])
    def delete_preset_file():
        """Delete a HandBrake preset JSON file from PRESET_DIR."""
        data = request.get_json(force=True) or {}
        path = data.get("path") or ""
        if not path:
            return jsonify(error="missing 'path' for preset to delete"), 400

        real_target = os.path.realpath(path)
        real_root = os.path.realpath(PRESET_DIR)

        if not real_target.startswith(real_root + os.sep) and real_target != real_root:
            return jsonify(error="refusing to delete file outside preset directory"), 400

        if not os.path.isfile(real_target):
            return jsonify(error="preset file not found"), 404

        try:
            os.remove(real_target)
        except Exception as e:
            return jsonify(error=f"failed to delete preset: {e}"), 500

        updated_files = list_preset_files()
        return jsonify(
            ok=True,
            preset_files=updated_files,
        )

    # ------------------ preset download ----------------

    @app.route("/api/presets/download")
    def download_preset_file():
        """Download a preset JSON file from PRESET_DIR."""
        path = request.args.get("path") or ""
        if not path:
            return jsonify(error="missing 'path'"), 400

        real_target = os.path.realpath(path)
        real_root = os.path.realpath(PRESET_DIR)

        if not real_target.startswith(real_root + os.sep) and real_target != real_root:
            return jsonify(error="refusing to access file outside preset directory"), 400

        if not os.path.isfile(real_target):
            return jsonify(error="preset file not found"), 404

        return send_file(
            real_target,
            mimetype="application/json",
            as_attachment=True,
            download_name=os.path.basename(real_target),
        )

    # ------------- Queue state (pause / resume) -------------

    @app.route("/queue_state")
    def queue_state():
        """Return whether the dispatcher queue is paused."""
        paused = get_queue_state()
        return jsonify(paused=paused)

    @app.route("/pause_queue", methods=["POST"])
    def pause_queue():
        """Pause or resume the dispatcher queue."""
        data = request.get_json(silent=True) or {}
        if "paused" in data and isinstance(data["paused"], bool):
            new_state = set_queue_paused(data["paused"])
        else:
            new_state = set_queue_paused(None)
        return jsonify(paused=new_state)

    # ------------- Remove queued job -------------

    @app.route("/remove/<job_id>", methods=["POST"])
    def remove_job_route(job_id):
        """Remove a job from the queue if its status is still 'queued'."""
        ok, err = remove_queued_job(job_id)
        if not ok:
            return jsonify(error=err or "remove failed"), 400
        return jsonify(ok=True, job_id=job_id)

    @app.route("/move/<job_id>", methods=["POST"])
    def move_job_route(job_id):
        """Move a queued job up/down/top/bottom or to a specific position in the queue."""
        data = request.get_json(silent=True) or {}

        if "position" in data and data.get("position") not in (None, ""):
            ok, err = move_queued_job_to_position(job_id, data.get("position"))
            if not ok:
                return jsonify(error=err or "move failed"), 400
            return jsonify(ok=True, job_id=job_id, position=int(data.get("position")))

        direction = (data.get("direction") or "").strip().lower()
        ok, err = move_queued_job(job_id, direction)
        if not ok:
            return jsonify(error=err or "move failed"), 400
        return jsonify(ok=True, job_id=job_id, direction=direction)

    # ------------- Media search (files + folders, fuzzy-ish) -------------

    @app.route("/search_media")
    def search_media():
        """Search for video files AND folders by name."""
        q = (request.args.get("q") or "").strip()
        if not q:
            return jsonify(error="missing 'q' search term"), 400

        base = (request.args.get("base") or "").strip()

        def normalize(s: str) -> str:
            s = s.lower()
            for ch in [".", "_", "-"]:
                s = s.replace(ch, " ")
            return " ".join(s.split())

        query_terms = normalize(q).split()
        if not query_terms:
            return jsonify(error="search term too short"), 400

        matches = []
        SEARCH_LIMIT = 200

        def name_matches(name: str) -> bool:
            n = normalize(name)
            return all(term in n for term in query_terms)

        def walk_root(root_path: str) -> bool:
            """Walk a single root path and collect up to SEARCH_LIMIT matches."""
            nonlocal matches

            if not os.path.isdir(root_path):
                return False

            for root_dir, dirs, files in os.walk(root_path):
                for d in dirs:
                    if not name_matches(d):
                        continue

                    full_path = os.path.join(root_dir, d)
                    if not is_allowed_path(full_path):
                        continue

                    matches.append(
                        {
                            "path": full_path,
                            "name": d,
                            "folder": root_dir,
                            "type": "dir",
                        }
                    )
                    if len(matches) >= SEARCH_LIMIT:
                        return True

                for name in files:
                    if not name.lower().endswith(VIDEO_EXTS):
                        continue
                    if not name_matches(name):
                        continue

                    full_path = os.path.join(root_dir, name)
                    if not is_allowed_path(full_path):
                        continue

                    matches.append(
                        {
                            "path": full_path,
                            "name": name,
                            "folder": root_dir,
                            "type": "file",
                        }
                    )
                    if len(matches) >= SEARCH_LIMIT:
                        return True

            return False

        if base:
            if not is_allowed_path(base) or not os.path.isdir(base):
                return jsonify(error="base path not allowed or not a directory"), 400
            walk_root(base)
        else:
            for root_path, _label in ROOTS:
                if walk_root(root_path):
                    break

        return jsonify(matches=matches, limit=SEARCH_LIMIT)

    _start_beta_autoscan_thread()
    _start_node_heartbeat_thread()
    _start_auto_node_dispatch_thread()
