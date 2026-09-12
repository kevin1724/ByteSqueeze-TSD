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


AUDIO_CACHE_FILE = os.path.join(DATA_DIR.rstrip("/"), "audio_inventory_cache.json")
AUDIO_CACHE_SCHEMA = 1
AUDIO_CACHE_LOCK = threading.RLock()
LOSSLESS_CODECS = {"alac", "ape", "flac", "mlp", "truehd", "wavpack"}
EFFICIENT_LOSSY_CODECS = {"aac", "ac3", "eac3", "mp3", "opus"}
OBJECT_AUDIO_MARKERS = ("atmos", "dts:x", "dtsx", "object based", "object-based")
COMMENTARY_MARKERS = ("commentary", "director", "descriptive", "description")
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
    """Return a cached full stream inventory with exact packet sizes when asked."""
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
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "ffprobe failed").strip()[:300])
    payload = json.loads(result.stdout or "{}")
    raw_streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    packet_sizes = _packet_sizes(path) if exact else {}
    format_row = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration = max(0.0, _float(format_row.get("duration")))
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
        estimated_size = int(bitrate * stream_duration / 8.0) if bitrate and stream_duration else 0
        size_bytes = packet_size or tagged_size or estimated_size
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
    inventory = {
        "schema": AUDIO_CACHE_SCHEMA,
        "path": path,
        "signature": signature,
        "format_name": str(format_row.get("format_name") or "").strip(),
        "duration_seconds": round(duration, 6),
        "total_bytes": total_bytes,
        "chapter_count": len(chapters),
        "streams": streams,
        "audio_streams": [row for row in streams if row["type"] == "audio"],
        "video_streams": [row for row in streams if row["type"] == "video"],
        "subtitle_streams": [row for row in streams if row["type"] == "subtitle"],
        "attachment_streams": [row for row in streams if row["type"] == "attachment"],
        "aggregate": aggregate,
        "packet_sizes_exact": bool(exact),
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


def _target_bitrate(channels: int) -> int:
    if channels >= 8:
        return 1024
    if channels >= 6:
        return 640
    if channels >= 3:
        return 448
    return 256


def _track_defaults(track: dict, policy: str, allow_object_audio_loss: bool) -> tuple[dict, list[str]]:
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
        "bitrate_kbps": _target_bitrate(channels),
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
        action, track_warnings = _track_defaults(track, policy, allow_object)
        action["target_codec"] = default_target_codec
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
        action["bitrate_kbps"] = max(64, min(2048, _int(action.get("bitrate_kbps"), _target_bitrate(action["source_channels"]))))
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
