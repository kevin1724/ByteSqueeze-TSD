"""Audio stream inventory, planning, estimation, and validation.

The module is deliberately independent from HandBrake job orchestration.  It
models video, audio, and subtitle operations separately so the same payload can
drive a normal HandBrake encode or an FFmpeg audio-only remux.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from copy import deepcopy

from .config import DATA_DIR
from .process_utils import background_process_options


AUDIO_CACHE_FILE = os.path.join(DATA_DIR.rstrip("/"), "audio_inventory_cache.json")
AUDIO_CACHE_SCHEMA = 2
AUDIO_CACHE_LOCK = threading.RLock()
QUICK_SAMPLE_WINDOW_SECONDS = 8.0
LOSSLESS_CODECS = {"alac", "ape", "flac", "mlp", "truehd", "wavpack"}
EFFICIENT_LOSSY_CODECS = {"aac", "ac3", "eac3", "mp3", "opus"}
OBJECT_AUDIO_MARKERS = ("atmos", "dts:x", "dtsx", "object based", "object-based")
COMMENTARY_MARKERS = ("commentary", "director", "descriptive", "description")
SPANISH_LATAM_MARKERS = (
    "latino", "latin american", "latin-american", "latam", "es-419",
    "es_419", "mexican", "mexico", "es-mx", "es_mx",
)
SPANISH_CASTILIAN_MARKERS = (
    "castellano", "castilian", "españa", "spain", "es-es", "es_es",
)
LANGUAGE_ALIASES = {
    "en": "eng", "eng": "eng", "english": "eng",
    "es": "spa", "spa": "spa", "spanish": "spa", "español": "spa",
}
JOB_TYPES = {"video_audio", "video_preserve_audio", "audio_only"}
AUDIO_POLICIES = {"preserve", "optimize_lossless", "custom"}


def _int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _signature(path: str) -> dict:
    stat = os.stat(path)
    return {
        "path": os.path.realpath(path),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _cache_key(signature: dict) -> str:
    raw = f"{signature['path']}\0{signature['size']}\0{signature['mtime_ns']}"
    return hashlib.sha256(raw.encode("utf-8", errors="surrogatepass")).hexdigest()


def _load_cache() -> dict:
    try:
        with open(AUDIO_CACHE_FILE, "r", encoding="utf-8") as stream:
            data = json.load(stream)
        if isinstance(data, dict) and data.get("schema") == AUDIO_CACHE_SCHEMA:
            data.setdefault("items", {})
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {"schema": AUDIO_CACHE_SCHEMA, "items": {}}


def _save_cache(data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    temp = f"{AUDIO_CACHE_FILE}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(temp, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, AUDIO_CACHE_FILE)
    finally:
        try:
            os.remove(temp)
        except FileNotFoundError:
            pass


def _tag_number(tags: dict, *names: str) -> int:
    folded = {str(key).casefold(): value for key, value in tags.items()}
    for name in names:
        value = _int(folded.get(name.casefold()))
        if value > 0:
            return value
    return 0


def _packet_sizes(path: str) -> dict[int, int]:
    """Sum packet payload bytes for all streams in one sequential ffprobe pass."""
    command = [
        "ffprobe", "-v", "error", "-show_packets",
        "-show_entries", "packet=stream_index,size", "-of", "csv=p=0", path,
    ]
    totals: dict[int, int] = {}
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **background_process_options(),
    )
    try:
        assert process.stdout is not None
        for line in process.stdout:
            fields = [field.strip() for field in line.strip().split(",")]
            if len(fields) < 2:
                continue
            index = _int(fields[-2], -1)
            size = _int(fields[-1], 0)
            if index >= 0 and size > 0:
                totals[index] = totals.get(index, 0) + size
        stderr = process.stderr.read() if process.stderr is not None else ""
        if process.wait() != 0:
            raise RuntimeError((stderr or "ffprobe packet scan failed").strip()[:300])
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    return totals


def _quick_audio_estimates(path: str, duration: float, stream_indexes: set[int]) -> dict[int, dict]:
    """Estimate missing audio sizes from three short, seeked packet samples.

    Interactive planners do not need a byte-perfect answer.  Sampling avoids a
    second full read of a large NAS file while still measuring VBR/lossless
    tracks from their real packets.  Failures deliberately fall through to the
    metadata-only estimate so an optional size cannot block a job.
    """
    duration = max(0.0, _float(duration))
    indexes = {int(index) for index in stream_indexes if int(index) >= 0}
    if duration <= 0 or not indexes:
        return {}

    window = min(QUICK_SAMPLE_WINDOW_SECONDS, max(2.0, duration / 24.0))
    if duration <= window * 3.0:
        starts = [0.0]
        window = duration
    else:
        starts = [
            max(0.0, min(duration - window, duration * fraction))
            for fraction in (0.12, 0.50, 0.82)
        ]
    intervals = ",".join(f"{start:.3f}%+{window:.3f}" for start in starts)
    command = [
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-read_intervals", intervals,
        "-show_packets", "-show_entries", "packet=stream_index,size,duration_time",
        "-of", "json", path,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=40,
            check=False,
            **background_process_options(),
        )
        if result.returncode != 0:
            return {}
        payload = json.loads(result.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}

    sampled_bytes: dict[int, int] = {}
    sampled_seconds: dict[int, float] = {}
    for packet in payload.get("packets") or []:
        if not isinstance(packet, dict):
            continue
        index = _int(packet.get("stream_index"), -1)
        size = max(0, _int(packet.get("size")))
        if index not in indexes or size <= 0:
            continue
        sampled_bytes[index] = sampled_bytes.get(index, 0) + size
        sampled_seconds[index] = sampled_seconds.get(index, 0.0) + max(
            0.0, _float(packet.get("duration_time"))
        )

    estimates = {}
    sampled_window_seconds = min(duration, window * len(starts))
    for index, byte_count in sampled_bytes.items():
        seconds = sampled_seconds.get(index) or sampled_window_seconds
        if seconds <= 0:
            continue
        bitrate = int(round(byte_count * 8.0 / seconds))
        size_bytes = int(round(bitrate * duration / 8.0))
        if bitrate > 0 and size_bytes > 0:
            estimates[index] = {
                "bitrate": bitrate,
                "size_bytes": size_bytes,
                "sample_seconds": round(seconds, 3),
            }
    return estimates


def _lossless(codec: str, profile: str) -> bool:
    codec = str(codec or "").lower()
    profile = str(profile or "").lower()
    return codec in LOSSLESS_CODECS or codec.startswith("pcm_") or (
        codec in {"dts", "dca"} and any(value in profile for value in ("dts-hd ma", "master audio", "lossless"))
    )


def _codec_label(codec: str, profile: str) -> str:
    codec_value = str(codec or "unknown").lower()
    profile_value = str(profile or "").strip()
    if codec_value in {"dts", "dca"} and "dts" in profile_value.lower():
        return profile_value
    labels = {
        "aac": "AAC", "ac3": "AC3", "eac3": "E-AC3", "truehd": "TrueHD",
        "dts": "DTS", "dca": "DTS", "flac": "FLAC", "alac": "ALAC",
        "opus": "Opus", "mp3": "MP3", "vorbis": "Vorbis",
    }
    base = labels.get(codec_value, codec_value.upper())
    return f"{base} {profile_value}".strip() if profile_value and profile_value.lower() not in base.lower() else base


def scan_media(path: str, *, exact: bool = True, force: bool = False) -> dict:
    """Return a cached stream inventory, using short samples unless exact is required."""
    signature = _signature(path)
    key = _cache_key(signature)
    with AUDIO_CACHE_LOCK:
        cache = _load_cache()
        cached = (cache.get("items") or {}).get(key)
        if isinstance(cached, dict) and not force and (not exact or cached.get("packet_sizes_exact")):
            return deepcopy(cached)

    command = [
        "ffprobe", "-v", "error", "-show_entries",
        (
            "format=duration,size,format_name:"
            "stream=index,codec_type,codec_name,codec_long_name,profile,channels,channel_layout,"
            "bit_rate,sample_rate,duration,nb_frames,width,height,pix_fmt:"
            "stream_tags=language,title,BPS,BPS-eng,NUMBER_OF_BYTES,NUMBER_OF_BYTES-eng:"
            "stream_disposition=default,forced:chapter=id,start_time,end_time"
        ),
        "-of", "json", path,
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,
        **background_process_options(),
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "ffprobe failed").strip()[:300])
    payload = json.loads(result.stdout or "{}")
    raw_streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    format_row = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration = max(0.0, _float(format_row.get("duration")))
    primary_streams = [
        raw for raw in raw_streams
        if isinstance(raw, dict) and str(raw.get("codec_type") or "").lower() in {"video", "audio"}
    ]
    missing_exact_sizes = [
        raw for raw in primary_streams
        if not _tag_number(
            raw.get("tags") if isinstance(raw.get("tags"), dict) else {},
            "NUMBER_OF_BYTES", "NUMBER_OF_BYTES-eng",
        )
    ]
    # Container-provided track byte counts are already exact.  The old path
    # performed a full packet walk before checking them, which was especially
    # expensive over SMB.  Only exact callers with missing tags pay that cost.
    packet_sizes = _packet_sizes(path) if exact and missing_exact_sizes else {}
    sample_indexes = {
        _int(raw.get("index"), -1)
        for raw in raw_streams
        if isinstance(raw, dict)
        and str(raw.get("codec_type") or "").lower() == "audio"
        and not _tag_number(
            raw.get("tags") if isinstance(raw.get("tags"), dict) else {},
            "NUMBER_OF_BYTES", "NUMBER_OF_BYTES-eng",
        )
        and not (
            max(0, _int(raw.get("bit_rate")))
            or _tag_number(
                raw.get("tags") if isinstance(raw.get("tags"), dict) else {},
                "BPS", "BPS-eng",
            )
        )
    }
    sampled_audio = _quick_audio_estimates(path, duration, sample_indexes) if not exact else {}
    ordinals: dict[str, int] = {}
    streams = []
    for raw in raw_streams:
        if not isinstance(raw, dict):
            continue
        stream_type = str(raw.get("codec_type") or "unknown").strip().lower()
        ordinal = ordinals.get(stream_type, 0)
        ordinals[stream_type] = ordinal + 1
        tags = raw.get("tags") if isinstance(raw.get("tags"), dict) else {}
        disposition = raw.get("disposition") if isinstance(raw.get("disposition"), dict) else {}
        index = _int(raw.get("index"), -1)
        stream_duration = max(0.0, _float(raw.get("duration"), duration)) or duration
        bitrate = max(0, _int(raw.get("bit_rate"))) or _tag_number(tags, "BPS", "BPS-eng")
        tagged_size = _tag_number(tags, "NUMBER_OF_BYTES", "NUMBER_OF_BYTES-eng")
        packet_size = max(0, _int(packet_sizes.get(index)))
        sampled = sampled_audio.get(index) if isinstance(sampled_audio.get(index), dict) else {}
        sampled_size = max(0, _int(sampled.get("size_bytes")))
        sampled_bitrate = max(0, _int(sampled.get("bitrate")))
        if not bitrate and sampled_bitrate:
            bitrate = sampled_bitrate
        estimated_size = int(bitrate * stream_duration / 8.0) if bitrate and stream_duration else 0
        size_bytes = packet_size or tagged_size or sampled_size or estimated_size
        if packet_size:
            size_source, size_accuracy = "packet_scan", 1.0
        elif tagged_size:
            size_source, size_accuracy = "container_tag", 1.0
        elif sampled_size:
            size_source, size_accuracy = "packet_sample", 0.8
        elif estimated_size:
            size_source, size_accuracy = "bitrate_estimate", 0.8
        else:
            size_source, size_accuracy = "unavailable", 0.0
        codec = str(raw.get("codec_name") or "unknown").strip().lower()
        profile = str(raw.get("profile") or "").strip()
        title = str(tags.get("title") or "").strip()
        searchable = f"{codec} {profile} {title}".lower()
        object_audio_capable = codec == "truehd" or (
            codec in {"dts", "dca"} and any(marker in profile.lower() for marker in ("dts-hd", "master audio"))
        )
        row = {
            "index": index,
            "type": stream_type,
            "type_ordinal": ordinal,
            "language": str(tags.get("language") or "und").strip().lower(),
            "title": title,
            "codec": codec,
            "codec_label": _codec_label(codec, profile),
            "profile": profile,
            "channels": max(0, _int(raw.get("channels"))),
            "channel_layout": str(raw.get("channel_layout") or "").strip(),
            "bitrate": bitrate,
            "sample_rate": max(0, _int(raw.get("sample_rate"))),
            "duration_seconds": round(stream_duration, 6),
            "nb_frames": max(0, _int(raw.get("nb_frames"))),
            "width": max(0, _int(raw.get("width"))),
            "height": max(0, _int(raw.get("height"))),
            "pix_fmt": str(raw.get("pix_fmt") or "").strip(),
            "lossless": _lossless(codec, profile),
            "default": bool(disposition.get("default")),
            "forced": bool(disposition.get("forced")),
            "size_bytes": size_bytes,
            "size_exact": bool(packet_size or tagged_size),
            "size_source": size_source,
            "size_accuracy": size_accuracy,
            "object_audio": any(marker in searchable for marker in OBJECT_AUDIO_MARKERS),
            "object_audio_capable": object_audio_capable,
            "commentary": any(marker in title.lower() for marker in COMMENTARY_MARKERS),
        }
        streams.append(row)

    chapters = payload.get("chapters") if isinstance(payload.get("chapters"), list) else []
    aggregate: dict[str, int] = {"video_bytes": 0, "audio_bytes": 0, "subtitle_bytes": 0, "attachment_bytes": 0}
    for row in streams:
        key_name = f"{row['type']}_bytes"
        if key_name in aggregate:
            aggregate[key_name] += int(row.get("size_bytes") or 0)
    total_bytes = max(0, _int(format_row.get("size"), signature["size"])) or signature["size"]
    known = sum(aggregate.values())
    aggregate["other_bytes"] = max(0, total_bytes - known)
    primary_rows = [row for row in streams if row["type"] in {"video", "audio"}]
    audio_rows = [row for row in streams if row["type"] == "audio"]
    primary_sizes_exact = all(row.get("size_exact") for row in primary_rows)
    audio_sizes_exact = all(row.get("size_exact") for row in audio_rows)
    inventory = {
        "schema": AUDIO_CACHE_SCHEMA,
        "path": path,
        "signature": signature,
        "format_name": str(format_row.get("format_name") or "").strip(),
        "duration_seconds": round(duration, 6),
        "total_bytes": total_bytes,
        "chapter_count": len(chapters),
        "streams": streams,
        "audio_streams": audio_rows,
        "video_streams": [row for row in streams if row["type"] == "video"],
        "subtitle_streams": [row for row in streams if row["type"] == "subtitle"],
        "attachment_streams": [row for row in streams if row["type"] == "attachment"],
        "aggregate": aggregate,
        # Keep the legacy field for callers/cache checks. It now truthfully
        # means every primary stream has an exact packet or container-tag size.
        "packet_sizes_exact": bool(primary_sizes_exact),
        "audio_sizes_exact": bool(audio_sizes_exact),
        "audio_size_accuracy": 1.0 if audio_sizes_exact else (0.8 if all(row.get("size_bytes") for row in audio_rows) else 0.0),
        "scanned_at": time.time(),
    }
    with AUDIO_CACHE_LOCK:
        cache = _load_cache()
        items = cache.setdefault("items", {})
        items[key] = inventory
        if len(items) > 2000:
            oldest = sorted(items, key=lambda item: _float(items[item].get("scanned_at")))[: len(items) - 2000]
            for old_key in oldest:
                items.pop(old_key, None)
        _save_cache(cache)
    return deepcopy(inventory)


def _target_bitrate(channels: int, surround_target_kbps: int = 1024) -> int:
    """Return a quality-first bitrate instead of the old stereo-sized budget."""
    surround_target_kbps = max(640, min(2048, _int(surround_target_kbps, 1024)))
    if channels >= 6:
        return surround_target_kbps
    if channels >= 3:
        return min(surround_target_kbps, 640)
    return 320


def _language_family(value: str) -> str:
    raw = str(value or "und").strip().lower().replace("_", "-")
    return LANGUAGE_ALIASES.get(raw, LANGUAGE_ALIASES.get(raw.split("-", 1)[0], raw or "und"))


def _language_group(track: dict) -> str:
    family = _language_family(track.get("language"))
    if family != "spa":
        return family
    searchable = f"{track.get('language') or ''} {track.get('title') or ''}".lower()
    if any(marker in searchable for marker in SPANISH_LATAM_MARKERS):
        return "spa:latam"
    if any(marker in searchable for marker in SPANISH_CASTILIAN_MARKERS):
        return "spa:castilian"
    return "spa:generic"


def _preferred_languages(value) -> list[str]:
    raw = value if isinstance(value, list) else str(value or "").replace(";", ",").split(",")
    result = []
    for item in raw:
        language = _language_family(str(item))
        if language and language != "und" and language not in result:
            result.append(language)
    return result


def _track_quality_score(track: dict) -> tuple:
    """Rank the main program track ahead of duplicates and commentary."""
    return (
        0 if track.get("commentary") else 1,
        1 if (track.get("object_audio") or track.get("object_audio_capable")) else 0,
        1 if track.get("lossless") else 0,
        max(0, _int(track.get("channels"))),
        max(0, _int(track.get("bitrate"))),
        max(0, _int(track.get("size_bytes"))),
        1 if track.get("default") else 0,
        -max(0, _int(track.get("type_ordinal"))),
    )


def _track_defaults(
    track: dict,
    policy: str,
    allow_object_audio_loss: bool,
    surround_target_kbps: int,
) -> tuple[dict, list[str]]:
    warnings = []
    action = "copy"
    if policy == "optimize_lossless" and track.get("lossless"):
        if (track.get("object_audio") or track.get("object_audio_capable")) and not allow_object_audio_loss:
            warnings.append(f"{track.get('codec_label')} may contain object audio; copied to preserve Atmos/DTS:X.")
        elif track.get("commentary"):
            warnings.append(f"Commentary track {track.get('type_ordinal', 0) + 1} was left untouched.")
        else:
            action = "encode"
            warnings.append(f"Encoding {track.get('codec_label')} is lossy; review before queueing.")
    channels = max(1, _int(track.get("channels"), 2))
    return {
        "stream_index": _int(track.get("index"), -1),
        "audio_ordinal": _int(track.get("type_ordinal"), 0),
        "language": str(track.get("language") or "und"),
        "title": str(track.get("title") or ""),
        "source_codec": str(track.get("codec") or "unknown"),
        "source_codec_label": str(track.get("codec_label") or "Unknown"),
        "source_channels": channels,
        "source_channel_layout": str(track.get("channel_layout") or ""),
        "source_sample_rate": _int(track.get("sample_rate")),
        "source_size_bytes": _int(track.get("size_bytes")),
        "lossless": bool(track.get("lossless")),
        "object_audio": bool(track.get("object_audio")),
        "object_audio_capable": bool(track.get("object_audio_capable")),
        "commentary": bool(track.get("commentary")),
        "action": action,
        "target_codec": "aac",
        "bitrate_kbps": _target_bitrate(channels, surround_target_kbps),
        "channels": channels,
        "channel_layout": str(track.get("channel_layout") or ""),
        "sample_rate": _int(track.get("sample_rate")),
        "gain_db": 0.0,
        "preserve_metadata": True,
        "allow_downmix": False,
    }, warnings


def normalize_operations(payload: dict | None, inventory: dict) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    job_type = str(payload.get("job_type") or "video_preserve_audio").strip().lower()
    if job_type not in JOB_TYPES:
        job_type = "video_preserve_audio"
    policy = str(payload.get("audio_policy") or "preserve").strip().lower()
    if policy not in AUDIO_POLICIES:
        policy = "preserve"
    allow_object = _bool(payload.get("allow_object_audio_loss"), False)
    surround_target_kbps = max(
        640,
        min(2048, _int(payload.get("default_target_bitrate_kbps"), 1024)),
    )
    deduplicate_languages = _bool(payload.get("deduplicate_languages"), True)
    preferred_languages = _preferred_languages(payload.get("preferred_languages", ["eng", "spa"]))
    transcode_selected_audio = _bool(payload.get("transcode_selected_audio"), False)
    audio_track_scope = str(payload.get("audio_track_scope") or "all").strip().lower()
    if audio_track_scope not in {"first", "all"}:
        audio_track_scope = "all"
    default_target_codec = str(payload.get("default_target_codec") or "aac").strip().lower()
    if default_target_codec not in {"aac", "eac3", "ac3", "opus", "flac"}:
        default_target_codec = "aac"
    proposed = payload.get("audio_actions") if isinstance(payload.get("audio_actions"), list) else []
    proposed_by_index = {
        _int(row.get("stream_index"), -1): row
        for row in proposed if isinstance(row, dict) and _int(row.get("stream_index"), -1) >= 0
    }
    actions = []
    warnings = []
    for track in inventory.get("audio_streams", []):
        action, track_warnings = _track_defaults(
            track, policy, allow_object, surround_target_kbps
        )
        action["target_codec"] = default_target_codec
        if policy == "optimize_lossless" and transcode_selected_audio:
            # Size Wizard's Smart optimize choice is an explicit request to
            # encode the retained tracks. Without this durable marker the
            # runtime normalizer used to turn the Wizard plan back into copy.
            action["action"] = "encode"
        override = proposed_by_index.get(_int(track.get("index"), -1))
        if policy in {"custom", "optimize_lossless"} and isinstance(override, dict):
            # Only per-track user controls may be overridden. Stream identity,
            # source facts and safety flags always come from the fresh ffprobe
            # inventory so a stale or forged client plan cannot target the
            # wrong track or bypass an object-audio warning.
            for key in (
                "action", "target_codec", "bitrate_kbps", "channels",
                "sample_rate", "gain_db", "preserve_metadata", "allow_downmix",
            ):
                if key in override:
                    action[key] = override[key]
        requested_action = str(action.get("action") or "copy").strip().lower()
        action["action"] = requested_action if requested_action in {"copy", "encode", "remove"} else "copy"
        action["target_codec"] = str(action.get("target_codec") or "aac").strip().lower()
        if action["target_codec"] not in {"aac", "eac3", "ac3", "opus", "flac"}:
            action["target_codec"] = "aac"
        action["bitrate_kbps"] = max(
            64,
            min(
                2048,
                _int(
                    action.get("bitrate_kbps"),
                    _target_bitrate(action["source_channels"], surround_target_kbps),
                ),
            ),
        )
        requested_channels = max(1, min(8, _int(action.get("channels"), action["source_channels"])))
        action["allow_downmix"] = _bool(action.get("allow_downmix"), False)
        if requested_channels < action["source_channels"] and not action["allow_downmix"]:
            warnings.append(
                f"{action['source_codec_label']} {action['source_channels']}ch was kept at {action['source_channels']}ch; enable downmix explicitly to reduce channels."
            )
            requested_channels = action["source_channels"]
        action["channels"] = requested_channels
        if action["action"] == "encode" and action["target_codec"] in {"ac3", "eac3"} and requested_channels > 6:
            warnings.append(
                f"{action['target_codec'].upper()} cannot safely preserve {requested_channels} channels with the bundled encoder; using AAC instead of downmixing."
            )
            action["target_codec"] = "aac"
        if action["action"] == "encode" and action["target_codec"] == "ac3" and action["bitrate_kbps"] > 640:
            warnings.append("AC3 is limited to 640 kbps; the selected track was capped at 640 kbps.")
            action["bitrate_kbps"] = 640
        action["sample_rate"] = max(0, min(192000, _int(action.get("sample_rate"), action["source_sample_rate"])))
        action["gain_db"] = max(-20.0, min(20.0, _float(action.get("gain_db"))))
        action["preserve_metadata"] = _bool(action.get("preserve_metadata"), True)
        if action["action"] == "remove":
            warnings.append(f"Removing {action['language']} {action['source_codec_label']} track is destructive.")
        if action["action"] == "encode" and action.get("object_audio"):
            warnings.append(f"Encoding {action['source_codec_label']} can remove Atmos/DTS:X object metadata.")
        elif action["action"] == "encode" and action.get("object_audio_capable"):
            warnings.append(
                f"{action['source_codec_label']} can carry Atmos/DTS:X extensions; encoding it removes any object metadata present."
            )
        if action["action"] != "copy" and action.get("commentary"):
            warnings.append("A commentary/descriptive track is being changed; verify this is intentional.")
        warnings.extend(track_warnings if action["action"] != "copy" or track.get("object_audio") else [])
        actions.append(action)

    # Smart language cleanup is applied only to the initial automatic plan.
    # Once the UI returns explicit per-track actions, those choices are
    # authoritative and are never silently rebuilt on validation/queueing.
    if policy == "optimize_lossless" and deduplicate_languages and not proposed:
        grouped: dict[str, list[tuple[dict, dict]]] = {}
        preferred = set(preferred_languages)
        inventory_by_index = {
            _int(track.get("index"), -1): track
            for track in inventory.get("audio_streams", [])
            if isinstance(track, dict)
        }
        for action in actions:
            track = inventory_by_index.get(action["stream_index"], action)
            group = _language_group(track)
            action["language_group"] = group
            family = group.split(":", 1)[0]
            if preferred and family not in preferred:
                action["action"] = "remove"
                action["selected_by_language_policy"] = False
                warnings.append(
                    f"Removing non-preferred {action['language']} {action['source_codec_label']} track; review before queueing."
                )
                continue
            grouped.setdefault(group, []).append((action, track))
        for group, candidates in grouped.items():
            winner, _winner_track = max(candidates, key=lambda pair: _track_quality_score(pair[1]))
            winner["selected_by_language_policy"] = True
            for action, _track in candidates:
                if action is winner:
                    continue
                action["action"] = "remove"
                action["selected_by_language_policy"] = False
                warnings.append(
                    f"Keeping the strongest {group.replace(':', ' ')} track and removing duplicate {action['source_codec_label']} {action['source_channels']}ch; review before queueing."
                )

        if audio_track_scope == "first":
            kept = [
                (action, inventory_by_index.get(action["stream_index"], action))
                for action in actions
                if action.get("action") != "remove"
            ]
            if kept:
                first_winner, _first_track = max(kept, key=lambda pair: _track_quality_score(pair[1]))
                for action, _track in kept:
                    if action is first_winner:
                        continue
                    action["action"] = "remove"
                    action["selected_by_language_policy"] = False
                    warnings.append(
                        f"Keeping one strongest preferred-language track and removing {action['language']} {action['source_codec_label']}."
                    )

    duration = max(0.0, _float(inventory.get("duration_seconds")))
    source_audio = sum(_int(row.get("source_size_bytes")) for row in actions)
    estimated_audio = 0
    for row in actions:
        if row["action"] == "copy":
            row["estimated_output_bytes"] = row["source_size_bytes"]
        elif row["action"] == "remove":
            row["estimated_output_bytes"] = 0
        elif row["target_codec"] == "flac":
            row["estimated_output_bytes"] = int(row["source_size_bytes"] * 0.72)
        else:
            row["estimated_output_bytes"] = int(row["bitrate_kbps"] * 1000 * duration / 8.0)
        row["estimated_savings_bytes"] = row["source_size_bytes"] - row["estimated_output_bytes"]
        estimated_audio += row["estimated_output_bytes"]
    estimated_savings = source_audio - estimated_audio
    return {
        "schema": 1,
        "job_type": job_type,
        "video_action": "copy" if job_type == "audio_only" else "encode",
        "audio_policy": policy,
        "audio_actions": actions,
        "subtitle_action": str(payload.get("subtitle_action") or "copy").lower() if job_type == "audio_only" else "copy",
        "allow_object_audio_loss": allow_object,
        "default_target_codec": default_target_codec,
        "default_target_bitrate_kbps": surround_target_kbps,
        "deduplicate_languages": deduplicate_languages,
        "preferred_languages": preferred_languages,
        "transcode_selected_audio": transcode_selected_audio,
        "audio_track_scope": audio_track_scope,
        "replace_source": _bool(payload.get("replace_source"), job_type == "audio_only"),
        "warnings": list(dict.fromkeys(warnings)),
        "estimate": {
            "current_audio_bytes": source_audio,
            "output_audio_bytes": max(0, estimated_audio),
            "audio_savings_bytes": estimated_savings,
            "audio_savings_percent": round((estimated_savings / source_audio) * 100.0, 1) if source_audio else 0.0,
            "estimated": True,
        },
    }


def handbrake_audio_args(operations: dict, inventory: dict) -> list[str]:
    actions = operations.get("audio_actions") if isinstance(operations.get("audio_actions"), list) else []
    kept = [row for row in actions if row.get("action") != "remove"]
    if not kept:
        return ["--audio", "none"]
    indexes = ",".join(str(_int(row.get("audio_ordinal")) + 1) for row in kept)
    encoders = []
    bitrates = []
    mixdowns = []
    rates = []
    gains = []
    codec_names = {"aac": "av_aac", "eac3": "eac3", "ac3": "ac3", "opus": "opus", "flac": "flac24"}
    for row in kept:
        if row.get("action") == "copy":
            encoders.append("copy")
            bitrates.append("auto")
            mixdowns.append("none")
        else:
            encoders.append(codec_names.get(str(row.get("target_codec")), "av_aac"))
            bitrates.append(str(_int(row.get("bitrate_kbps"), 256)))
            channels = _int(row.get("channels"), 2)
            mixdowns.append("7point1" if channels >= 8 else "5point1" if channels >= 6 else "stereo" if channels >= 2 else "mono")
        rates.append(str(_int(row.get("sample_rate"))) if _int(row.get("sample_rate")) else "auto")
        gains.append(str(_float(row.get("gain_db"))))
    return [
        "--audio", indexes,
        "--aencoder", ",".join(encoders),
        "--ab", ",".join(bitrates),
        "--mixdown", ",".join(mixdowns),
        "--arate", ",".join(rates),
        "--gain", ",".join(gains),
        "--audio-copy-mask", "aac,ac3,eac3,truehd,dts,dtshd,mp2,mp3,flac,alac,opus",
        "--audio-fallback", "none",
    ]


def ffmpeg_audio_only_command(src: str, temp_out: str, operations: dict) -> list[str]:
    actions = operations.get("audio_actions") if isinstance(operations.get("audio_actions"), list) else []
    command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", src,
        "-map", "0", "-map_metadata", "0", "-map_chapters", "0", "-c", "copy",
    ]
    for row in actions:
        if row.get("action") == "remove":
            command.extend(["-map", f"-0:{_int(row.get('stream_index'))}"])
    retained_ordinal = 0
    codec_names = {"aac": "aac", "eac3": "eac3", "ac3": "ac3", "opus": "libopus", "flac": "flac"}
    for row in actions:
        if row.get("action") == "remove":
            continue
        if row.get("action") == "encode":
            prefix = f":a:{retained_ordinal}"
            command.extend([f"-c{prefix}", codec_names.get(str(row.get("target_codec")), "aac")])
            if str(row.get("target_codec")) != "flac":
                command.extend([f"-b{prefix}", f"{_int(row.get('bitrate_kbps'), 256)}k"])
            if _int(row.get("channels")):
                command.extend([f"-ac{prefix}", str(_int(row.get("channels")))])
            if _int(row.get("sample_rate")):
                command.extend([f"-ar{prefix}", str(_int(row.get("sample_rate")))])
            if abs(_float(row.get("gain_db"))) >= 0.01:
                command.extend([f"-filter{prefix}", f"volume={_float(row.get('gain_db'))}dB"])
            if not row.get("preserve_metadata", True):
                command.extend([f"-map_metadata:s:a:{retained_ordinal}", "-1"])
        retained_ordinal += 1
    command.extend(["-progress", "pipe:1", "-nostats", temp_out])
    return command


def validate_audio_only(before: dict, after: dict, operations: dict) -> tuple[bool, list[str]]:
    errors = []
    before_video = before.get("video_streams") or []
    after_video = after.get("video_streams") or []
    if len(before_video) != len(after_video):
        errors.append("video stream count changed")
    else:
        for source, output in zip(before_video, after_video):
            for key in ("codec", "profile", "width", "height", "pix_fmt"):
                if source.get(key) != output.get(key):
                    errors.append(f"video {key} changed ({source.get(key)} -> {output.get(key)})")
            source_frames, output_frames = _int(source.get("nb_frames")), _int(output.get("nb_frames"))
            if source_frames and output_frames and abs(source_frames - output_frames) > 1:
                errors.append("video frame count changed")
            source_bytes, output_bytes = _int(source.get("size_bytes")), _int(output.get("size_bytes"))
            if source_bytes and output_bytes and source_bytes != output_bytes:
                errors.append("video packet payload size changed; stream copy could not be verified")
            stream_duration = _float(source.get("duration_seconds"))
            output_stream_duration = _float(output.get("duration_seconds"))
            if stream_duration and output_stream_duration and abs(stream_duration - output_stream_duration) > max(1.0, stream_duration * 0.005):
                errors.append("video stream duration changed")
    source_duration = _float(before.get("duration_seconds"))
    output_duration = _float(after.get("duration_seconds"))
    if source_duration and (not output_duration or abs(source_duration - output_duration) > max(2.0, source_duration * 0.01)):
        errors.append("output duration is inconsistent with the input")

    actions = operations.get("audio_actions") if isinstance(operations.get("audio_actions"), list) else []
    expected = [row for row in actions if row.get("action") != "remove"]
    actual = after.get("audio_streams") or []
    if len(expected) != len(actual):
        errors.append(f"expected {len(expected)} audio tracks, found {len(actual)}")
    else:
        for plan, output in zip(expected, actual):
            expected_codec = str(plan.get("source_codec") if plan.get("action") == "copy" else plan.get("target_codec") or "")
            actual_codec = str(output.get("codec") or "")
            if expected_codec == "dca":
                expected_codec = "dts"
            if actual_codec == "dca":
                actual_codec = "dts"
            if expected_codec and expected_codec != actual_codec:
                errors.append(f"audio codec mismatch ({expected_codec} -> {actual_codec})")
            expected_channels = _int(plan.get("source_channels") if plan.get("action") == "copy" else plan.get("channels"))
            if expected_channels and expected_channels != _int(output.get("channels")):
                errors.append(f"audio channel count mismatch for {plan.get('language') or 'track'}")
            if plan.get("preserve_metadata", True):
                expected_language = str(plan.get("language") or "und")
                actual_language = str(output.get("language") or "und")
                if expected_language != actual_language:
                    errors.append(f"audio language changed ({expected_language} -> {actual_language})")
    if operations.get("subtitle_action", "copy") == "copy":
        before_subs = [(row.get("codec"), row.get("language")) for row in before.get("subtitle_streams") or []]
        after_subs = [(row.get("codec"), row.get("language")) for row in after.get("subtitle_streams") or []]
        if before_subs != after_subs:
            errors.append("subtitle streams changed")
    if len(before.get("attachment_streams") or []) != len(after.get("attachment_streams") or []):
        errors.append("attachment streams changed")
    if _int(before.get("chapter_count")) != _int(after.get("chapter_count")):
        errors.append("chapters changed")
    return not errors, errors


def storage_breakdown(before: dict, after: dict, input_total: int, output_total: int) -> dict:
    before_agg = before.get("aggregate") if isinstance(before.get("aggregate"), dict) else {}
    after_agg = after.get("aggregate") if isinstance(after.get("aggregate"), dict) else {}
    input_video = _int(before_agg.get("video_bytes"))
    output_video = _int(after_agg.get("video_bytes"))
    input_audio = _int(before_agg.get("audio_bytes"))
    output_audio = _int(after_agg.get("audio_bytes"))
    input_other = max(0, _int(input_total) - input_video - input_audio)
    output_other = max(0, _int(output_total) - output_video - output_audio)
    total_saved = _int(input_total) - _int(output_total)
    video_saved = input_video - output_video
    audio_saved = input_audio - output_audio
    return {
        "input_total_bytes": _int(input_total),
        "output_total_bytes": _int(output_total),
        "input_video_bytes": input_video,
        "output_video_bytes": output_video,
        "input_audio_bytes": input_audio,
        "output_audio_bytes": output_audio,
        "input_other_bytes": input_other,
        "output_other_bytes": output_other,
        "total_saved_bytes": total_saved,
        "video_saved_bytes": video_saved,
        "audio_saved_bytes": audio_saved,
        "other_saved_bytes": input_other - output_other,
        "total_saved_percent": round((total_saved / _int(input_total)) * 100.0, 1) if _int(input_total) else 0.0,
        "video_saved_percent": round((video_saved / input_video) * 100.0, 1) if input_video else 0.0,
        "audio_saved_percent": round((audio_saved / input_audio) * 100.0, 1) if input_audio else 0.0,
        "measured": True,
    }
