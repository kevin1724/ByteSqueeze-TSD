"""Storage savings tracking.

Tracks per-encode storage savings and keeps a longer history so you can see
how much space you've recovered over time.

We write a single JSON file: data/storage_stats.json

Schema (loose):
{
  "encodes": [
     {
       "ts": 1700000000.123,
       "job_id": "...",
       "src": "/path/to/source.mkv",
       "out": "/path/to/output-TSD.mkv",
       "preset": "1080"|"4k"|"auto",
       "src_bytes": 123,
       "out_bytes": 456,
       "saved_bytes": 789,
       "duration_seconds": 1234.5,
       "is_hdr": false,
       "encode_method": "x265_10bit"
     }, ...
  ],
  "totals": {"count": 0, "saved_bytes": 0}
}
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from .config import DATA_DIR


STATS_FILE = os.path.join(DATA_DIR.rstrip("/"), "storage_stats.json")
MAX_ROWS = 5000
STATS_LOCK = threading.RLock()
_stats_cache: dict | None = None
_stats_signature: tuple[int, int] | None = None
_summary_cache: dict | None = None
_analytics_cache: dict[tuple[int, int, str], dict] = {}


def _ensure_dict(obj: Any) -> dict:
    return obj if isinstance(obj, dict) else {}


def _ensure_list(obj: Any) -> list:
    return obj if isinstance(obj, list) else []


def _file_signature() -> tuple[int, int] | None:
    try:
        stat = os.stat(STATS_FILE)
        return int(stat.st_mtime_ns), int(stat.st_size)
    except OSError:
        return None


def _load_unlocked() -> dict:
    global _stats_cache, _stats_signature, _summary_cache, _analytics_cache
    signature = _file_signature()
    if _stats_cache is not None and signature == _stats_signature:
        return _stats_cache

    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except Exception as e:
        print(f"[WARN] Failed to load storage_stats.json: {e}", flush=True)
        data = {}

    data = _ensure_dict(data)
    data.setdefault("encodes", [])
    data.setdefault("totals", {"count": 0, "saved_bytes": 0})
    data["encodes"] = _ensure_list(data.get("encodes"))
    data["totals"] = _ensure_dict(data.get("totals"))
    data["totals"].setdefault("count", 0)
    data["totals"].setdefault("saved_bytes", 0)
    data["totals"].setdefault("video_saved_bytes", 0)
    data["totals"].setdefault("audio_saved_bytes", 0)
    _stats_cache = data
    _stats_signature = signature
    _summary_cache = None
    _analytics_cache = {}
    return data


def _load() -> dict:
    with STATS_LOCK:
        return _load_unlocked()


def _save_unlocked(data: dict) -> None:
    global _stats_cache, _stats_signature, _summary_cache, _analytics_cache
    temp_path = f"{STATS_FILE}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, STATS_FILE)
        _stats_cache = data
        _stats_signature = _file_signature()
        _summary_cache = None
        _analytics_cache = {}
    except Exception as exc:
        print(f"[WARN] Failed to save storage_stats.json: {exc}", flush=True)
    finally:
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass


def _save(data: dict) -> None:
    with STATS_LOCK:
        _save_unlocked(data)


def record_encode(
    *,
    job_id: str,
    src: str,
    out: str,
    preset: str,
    src_bytes: int,
    out_bytes: int,
    duration_seconds: float | None = None,
    is_hdr: bool | None = None,
    node_id: str | None = None,
    node_name: str | None = None,
    encode_method: str | None = None,
    encoder: str | None = None,
    video_codec: str | None = None,
    encoder_family: str | None = None,
    bit_depth: str | None = None,
    job_type: str | None = None,
    operations: dict | None = None,
    parent_job_id: str | None = None,
    input_inventory: dict | None = None,
    output_inventory: dict | None = None,
    storage_breakdown: dict | None = None,
) -> dict:
    """Record a successful encode's storage savings."""
    try:
        src_bytes_i = int(src_bytes)
    except Exception:
        src_bytes_i = 0
    try:
        out_bytes_i = int(out_bytes)
    except Exception:
        out_bytes_i = 0

    saved = max(0, src_bytes_i - out_bytes_i)

    row = {
        "ts": time.time(),
        "job_id": str(job_id),
        "src": str(src),
        "out": str(out),
        "preset": str(preset),
        "src_bytes": src_bytes_i,
        "out_bytes": out_bytes_i,
        "saved_bytes": saved,
    }
    if duration_seconds is not None:
        try:
            row["duration_seconds"] = max(0.0, float(duration_seconds))
        except Exception:
            pass
    if is_hdr is not None:
        row["is_hdr"] = bool(is_hdr)
    if node_id:
        row["node_id"] = str(node_id)
    if node_name:
        row["node_name"] = str(node_name)
    if encode_method:
        row["encode_method"] = str(encode_method)
    if encoder:
        row["encoder"] = str(encoder)
    if video_codec:
        row["video_codec"] = str(video_codec)
    if encoder_family:
        row["encoder_family"] = str(encoder_family)
    if bit_depth:
        row["bit_depth"] = str(bit_depth)
    if job_type:
        row["job_type"] = str(job_type)
    if isinstance(operations, dict):
        row["operations"] = operations
    if parent_job_id:
        row["parent_job_id"] = str(parent_job_id)
    if isinstance(input_inventory, dict):
        row["input_inventory"] = input_inventory
    if isinstance(output_inventory, dict):
        row["output_inventory"] = output_inventory
    if isinstance(storage_breakdown, dict):
        row["storage_breakdown"] = storage_breakdown

    with STATS_LOCK:
        data = _load_unlocked()
        encodes = _ensure_list(data.get("encodes"))
        encodes.insert(0, row)  # newest-first
        if len(encodes) > MAX_ROWS:
            encodes = encodes[:MAX_ROWS]
        data["encodes"] = encodes

        totals = _ensure_dict(data.get("totals"))
        totals["count"] = int(totals.get("count") or 0) + 1
        totals["saved_bytes"] = int(totals.get("saved_bytes") or 0) + saved
        if isinstance(storage_breakdown, dict):
            totals["video_saved_bytes"] = int(totals.get("video_saved_bytes") or 0) + int(storage_breakdown.get("video_saved_bytes") or 0)
            totals["audio_saved_bytes"] = int(totals.get("audio_saved_bytes") or 0) + int(storage_breakdown.get("audio_saved_bytes") or 0)
        data["totals"] = totals

        _save_unlocked(data)
    return row


def get_summary() -> dict:
    global _summary_cache
    with STATS_LOCK:
        data = _load_unlocked()
        if _summary_cache is None:
            totals = _ensure_dict(data.get("totals"))
            saved_bytes = int(totals.get("saved_bytes") or 0)
            count = int(totals.get("count") or 0)
            total_runtime_seconds = 0.0
            for row in _ensure_list(data.get("encodes")):
                try:
                    total_runtime_seconds += float(row.get("duration_seconds") or 0.0)
                except Exception:
                    pass
            _summary_cache = {
                "count": count,
                "saved_bytes": saved_bytes,
                "saved_gb": round(saved_bytes / (1024**3), 3) if saved_bytes else 0.0,
                "total_runtime_seconds": round(total_runtime_seconds, 1),
                "video_saved_bytes": int(totals.get("video_saved_bytes") or 0),
                "audio_saved_bytes": int(totals.get("audio_saved_bytes") or 0),
            }
        return _summary_cache.copy()


def _analytics_copy(payload: dict) -> dict:
    copied = payload.copy()
    copied["trend"] = [row.copy() for row in payload.get("trend", [])]
    copied["workers"] = [row.copy() for row in payload.get("workers", [])]
    copied["peak_day"] = (payload.get("peak_day") or {}).copy()
    return copied


def get_dashboard_analytics(
    days: int = 30,
    worker_limit: int = 6,
    *,
    now_ts: float | None = None,
) -> dict:
    """Return compact, durable storage-impact analytics for operational UIs.

    The ledger is newest-first and bounded, while ``totals`` remains lifetime
    data. Any totals older than the retained ledger are kept visible as an
    unattributed contribution instead of silently disappearing.
    """
    global _analytics_cache
    days = max(7, min(365, int(days or 30)))
    worker_limit = max(1, min(20, int(worker_limit or 6)))
    current_ts = float(now_ts if now_ts is not None else time.time())
    today = datetime.fromtimestamp(current_ts).date()
    cache_key = (days, worker_limit, today.isoformat())

    with STATS_LOCK:
        data = _load_unlocked()
        cached = _analytics_cache.get(cache_key)
        if cached is not None:
            return _analytics_copy(cached)

        rows = _ensure_list(data.get("encodes"))
        totals = _ensure_dict(data.get("totals"))
        lifetime_count = max(0, int(totals.get("count") or 0))
        lifetime_saved = max(0, int(totals.get("saved_bytes") or 0))
        first_day = today - timedelta(days=days - 1)
        trend_index = {
            (first_day + timedelta(days=offset)).isoformat(): {
                "date": (first_day + timedelta(days=offset)).isoformat(),
                "saved_bytes": 0,
                "completed": 0,
            }
            for offset in range(days)
        }
        workers: dict[str, dict] = {}
        active_dates = set()
        row_saved_total = 0
        total_source_bytes = 0
        total_output_bytes = 0
        first_encode_at = 0.0
        last_encode_at = 0.0

        for row in rows:
            try:
                encoded_at = max(0.0, float(row.get("ts") or 0.0))
            except Exception:
                encoded_at = 0.0
            try:
                saved_bytes = max(0, int(row.get("saved_bytes") or 0))
            except Exception:
                saved_bytes = 0
            try:
                source_bytes = max(0, int(row.get("src_bytes") or 0))
            except Exception:
                source_bytes = 0
            try:
                output_bytes = max(0, int(row.get("out_bytes") or 0))
            except Exception:
                output_bytes = 0
            try:
                runtime_seconds = max(0.0, float(row.get("duration_seconds") or 0.0))
            except Exception:
                runtime_seconds = 0.0

            row_saved_total += saved_bytes
            total_source_bytes += source_bytes
            total_output_bytes += output_bytes
            if encoded_at > 0:
                first_encode_at = encoded_at if not first_encode_at else min(first_encode_at, encoded_at)
                last_encode_at = max(last_encode_at, encoded_at)
                encoded_day = datetime.fromtimestamp(encoded_at).date()
                active_dates.add(encoded_day)
                point = trend_index.get(encoded_day.isoformat())
                if point is not None:
                    point["saved_bytes"] += saved_bytes
                    point["completed"] += 1

            node_id = str(row.get("node_id") or "").strip()
            node_name = str(row.get("node_name") or "").strip() or "Main controller"
            worker_key = node_id or f"legacy::{node_name.casefold()}"
            worker = workers.setdefault(worker_key, {
                "node_id": node_id,
                "node_name": node_name,
                "completed": 0,
                "saved_bytes": 0,
                "runtime_seconds": 0.0,
            })
            worker["completed"] += 1
            worker["saved_bytes"] += saved_bytes
            worker["runtime_seconds"] += runtime_seconds

        unattributed_count = max(0, lifetime_count - len(rows))
        unattributed_saved = max(0, lifetime_saved - row_saved_total)
        if unattributed_count or unattributed_saved:
            workers["__unattributed__"] = {
                "node_id": "",
                "node_name": "Older history",
                "completed": unattributed_count,
                "saved_bytes": unattributed_saved,
                "runtime_seconds": 0.0,
                "unattributed": True,
            }

        worker_rows = sorted(
            workers.values(),
            key=lambda worker: (int(worker.get("saved_bytes") or 0), int(worker.get("completed") or 0)),
            reverse=True,
        )
        if len(worker_rows) > worker_limit:
            overflow = worker_rows[worker_limit - 1:]
            worker_rows = worker_rows[:worker_limit - 1] + [{
                "node_id": "",
                "node_name": "Other workers",
                "completed": sum(int(row.get("completed") or 0) for row in overflow),
                "saved_bytes": sum(int(row.get("saved_bytes") or 0) for row in overflow),
                "runtime_seconds": sum(float(row.get("runtime_seconds") or 0.0) for row in overflow),
                "grouped": True,
            }]
        contribution_total = max(1, lifetime_saved or row_saved_total)
        for worker in worker_rows:
            worker["runtime_seconds"] = round(float(worker.get("runtime_seconds") or 0.0), 1)
            worker["share_percent"] = round((int(worker.get("saved_bytes") or 0) / contribution_total) * 100.0, 1)

        trend = list(trend_index.values())
        recent_saved = sum(int(point["saved_bytes"]) for point in trend)
        recent_completed = sum(int(point["completed"]) for point in trend)
        peak_day = max(trend, key=lambda point: int(point.get("saved_bytes") or 0), default={})

        longest_streak = 0
        streak = 0
        previous_day = None
        for active_day in sorted(active_dates):
            if previous_day is not None and active_day == previous_day + timedelta(days=1):
                streak += 1
            else:
                streak = 1
            longest_streak = max(longest_streak, streak)
            previous_day = active_day

        payload = {
            "window_days": days,
            "trend": trend,
            "recent_saved_bytes": recent_saved,
            "recent_completed": recent_completed,
            "peak_day": peak_day.copy(),
            "workers": worker_rows,
            "worker_count": len([row for row in workers.values() if not row.get("unattributed")]),
            "tracked_rows": len(rows),
            "history_complete": lifetime_count <= len(rows),
            "total_source_bytes": total_source_bytes,
            "total_output_bytes": total_output_bytes,
            "efficiency_percent": round((row_saved_total / total_source_bytes) * 100.0, 1) if total_source_bytes else 0.0,
            "average_saved_bytes": round(lifetime_saved / lifetime_count) if lifetime_count else 0,
            "active_days": len(active_dates),
            "longest_streak_days": longest_streak,
            "first_encode_at": first_encode_at,
            "last_encode_at": last_encode_at,
        }
        _analytics_cache[cache_key] = payload
        return _analytics_copy(payload)


def list_encodes(limit: int = 200) -> list[dict]:
    with STATS_LOCK:
        rows = list(_ensure_list(_load_unlocked().get("encodes")))
        rows.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
        return [row.copy() for row in rows[: max(0, int(limit or 0))]]


def update_encode_output_inventory(job_id: str, inventory: dict) -> bool:
    """Lazily backfill audio/stream facts for an existing completed job."""
    if not job_id or not isinstance(inventory, dict):
        return False
    with STATS_LOCK:
        data = _load_unlocked()
        for row in _ensure_list(data.get("encodes")):
            if str(row.get("job_id") or "") != str(job_id):
                continue
            row["output_inventory"] = inventory
            row["audio_inventory_backfilled_at"] = time.time()
            _save_unlocked(data)
            return True
    return False


def clear_stats() -> None:
    with STATS_LOCK:
        _save_unlocked({"encodes": [], "totals": {"count": 0, "saved_bytes": 0, "video_saved_bytes": 0, "audio_saved_bytes": 0}})
