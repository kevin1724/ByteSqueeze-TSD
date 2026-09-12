"""Persistent preference learning for Size Wizard smart presets.

The model is deliberately small and explainable.  HandBrake planning remains
deterministic; this module only learns which safe plans a person tends to
approve for similar sources.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import time
import uuid

from .config import DATA_DIR


SMART_PRESETS_FILE = os.path.join(DATA_DIR, "smart_presets.json")
SMART_PRESET_VERSION = 5
SMART_FEEDBACK_LIMIT = 1000
SMART_LOCK = threading.RLock()

GOALS = {"balanced", "quality", "small", "speed", "archive"}
COMPATIBILITY = {"broad", "modern", "maximum"}
HARDWARE = {"auto", "software", "qsv", "nvenc"}
AUDIO_STRATEGIES = {"copy", "eac3_surround"}
SMART_LANGUAGES = ["eng", "spa"]


def _truthy(value, default=False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _choice(value, allowed: set[str], default: str) -> str:
    clean = str(value or default).strip().lower()
    return clean if clean in allowed else default


def _bounded(value, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    return max(low, min(high, number))


def _languages(value, fallback: list[str] | None = None) -> list[str]:
    """Normalize a saved ISO language list while allowing an empty all-language filter."""
    if value is None:
        value = fallback if fallback is not None else SMART_LANGUAGES
    if isinstance(value, str):
        values = re.split(r"[\s,;]+", value)
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = fallback if fallback is not None else SMART_LANGUAGES
    out = []
    for item in values:
        token = str(item or "").strip().lower()
        if not re.fullmatch(r"[a-z]{2,3}", token) or token in out:
            continue
        out.append(token)
        if len(out) >= 24:
            break
    return out


def default_profile() -> dict:
    now = time.time()
    return {
        "id": "default",
        "name": "My smart preset",
        "goal": "balanced",
        "compatibility": "modern",
        "hardware": "auto",
        "audio_strategy": "copy",
        "audio_languages": SMART_LANGUAGES.copy(),
        "subtitle_languages": SMART_LANGUAGES.copy(),
        # Preservation-first guardrails. These are deliberately enabled for
        # existing profiles when they migrate to v4 so a tight Smart target
        # cannot silently resize or strip tracks from a season.
        "never_downscale": True,
        "keep_black_bars": True,
        "keep_aspect_ratio": True,
        "keep_all_audio_languages": True,
        "keep_all_subtitle_languages": True,
        "never_transcode_audio": True,
        "preserve_audio": True,
        "preserve_subtitles": True,
        # Explicitly opt-in because representative JPEG frames leave the
        # controller when a cloud vision provider evaluates an episode. Each
        # episode is sampled and cached independently; this never changes the
        # deterministic HDR, resolution, audio, or subtitle guardrails.
        "episode_ai_enabled": False,
        "episode_ai_frame_count": 4,
        "automation_enabled": False,
        "minimum_feedback": 2,
        "confidence_threshold": 0.72,
        "created_at": now,
        "updated_at": now,
    }


def normalize_profile(values: dict | None, existing: dict | None = None) -> dict:
    base = default_profile()
    if isinstance(existing, dict):
        base.update(existing)
    values = values if isinstance(values, dict) else {}
    name = " ".join(str(values.get("name", base["name"]) or "").split())[:80]
    legacy_preserve_audio = _truthy(
        values.get("preserve_audio"), base.get("preserve_audio", True)
    )
    legacy_strategy = "copy" if legacy_preserve_audio else "eac3_surround"
    if "audio_strategy" in values:
        requested_audio_strategy = values.get("audio_strategy")
    elif "preserve_audio" in values:
        requested_audio_strategy = legacy_strategy
    else:
        requested_audio_strategy = base.get("audio_strategy", "copy")
    audio_languages = _languages(
        values.get("audio_languages") if "audio_languages" in values else base.get("audio_languages"),
        SMART_LANGUAGES,
    )
    subtitle_languages = _languages(
        values.get("subtitle_languages") if "subtitle_languages" in values else base.get("subtitle_languages"),
        SMART_LANGUAGES,
    )
    never_transcode_audio = _truthy(
        values.get("never_transcode_audio"), base.get("never_transcode_audio", True)
    )
    keep_all_subtitle_languages = _truthy(
        values.get("keep_all_subtitle_languages"),
        base.get("keep_all_subtitle_languages", True),
    )
    base.update(
        {
            "id": "default",
            "name": name or "My smart preset",
            "goal": _choice(values.get("goal", base["goal"]), GOALS, "balanced"),
            "compatibility": _choice(
                values.get("compatibility", base["compatibility"]), COMPATIBILITY, "modern"
            ),
            "hardware": _choice(values.get("hardware", base["hardware"]), HARDWARE, "auto"),
            "audio_strategy": _choice(
                requested_audio_strategy,
                AUDIO_STRATEGIES,
                legacy_strategy,
            ),
            "audio_languages": audio_languages,
            "subtitle_languages": subtitle_languages,
            "never_downscale": _truthy(
                values.get("never_downscale"), base.get("never_downscale", True)
            ),
            "keep_black_bars": _truthy(
                values.get("keep_black_bars"), base.get("keep_black_bars", True)
            ),
            "keep_aspect_ratio": _truthy(
                values.get("keep_aspect_ratio"), base.get("keep_aspect_ratio", True)
            ),
            "keep_all_audio_languages": _truthy(
                values.get("keep_all_audio_languages"),
                base.get("keep_all_audio_languages", True),
            ),
            "keep_all_subtitle_languages": keep_all_subtitle_languages,
            "never_transcode_audio": never_transcode_audio,
            # Keep the legacy booleans synchronized for older clients.
            "preserve_audio": never_transcode_audio,
            "preserve_subtitles": keep_all_subtitle_languages,
            "episode_ai_enabled": _truthy(
                values.get("episode_ai_enabled"), base.get("episode_ai_enabled", False)
            ),
            "episode_ai_frame_count": int(
                round(
                    _bounded(
                        values.get("episode_ai_frame_count"),
                        base.get("episode_ai_frame_count", 4),
                        3,
                        8,
                    )
                )
            ),
            "automation_enabled": _truthy(
                values.get("automation_enabled"), base["automation_enabled"]
            ),
            "minimum_feedback": int(
                round(_bounded(values.get("minimum_feedback"), base["minimum_feedback"], 2, 20))
            ),
            "confidence_threshold": round(
                _bounded(values.get("confidence_threshold"), base["confidence_threshold"], 0.55, 0.95), 2
            ),
            "created_at": float(base.get("created_at") or time.time()),
            "updated_at": time.time(),
        }
    )
    return base


def _empty_state() -> dict:
    return {"version": SMART_PRESET_VERSION, "profile": default_profile(), "feedback": []}


def load_state() -> dict:
    with SMART_LOCK:
        try:
            with open(SMART_PRESETS_FILE, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            return _empty_state()
        except Exception:
            return _empty_state()

        state = raw if isinstance(raw, dict) else {}
        profile = normalize_profile(state.get("profile"), state.get("profile"))
        # Version 2 shipped with a hidden three-preview default. Version 3's
        # dedicated training page makes the target visible and uses two
        # consistent approvals, while preserving higher custom targets saved
        # after this migration.
        try:
            previous_version = int(state.get("version") or 0)
        except (TypeError, ValueError):
            previous_version = 0
        if previous_version < 3 and int(profile.get("minimum_feedback") or 0) == 3:
            profile["minimum_feedback"] = 2
        rows = []
        for row in state.get("feedback") if isinstance(state.get("feedback"), list) else []:
            if not isinstance(row, dict):
                continue
            if str(row.get("verdict") or "") not in {"approve", "reject"}:
                continue
            rows.append(row)
        return {
            "version": SMART_PRESET_VERSION,
            "profile": profile,
            "feedback": rows[-SMART_FEEDBACK_LIMIT:],
        }


def _save_state_unlocked(state: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="smart_presets_", suffix=".tmp", dir=DATA_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, SMART_PRESETS_FILE)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def save_profile(values: dict | None) -> dict:
    with SMART_LOCK:
        state = load_state()
        profile = normalize_profile(values, state.get("profile"))
        state["profile"] = profile
        _save_state_unlocked(state)
        return profile


def resolution_bucket(width, height) -> str:
    try:
        width_i, height_i = int(width or 0), int(height or 0)
    except (TypeError, ValueError):
        width_i, height_i = 0, 0
    if height_i >= 1800 or width_i >= 3200:
        return "4k"
    if height_i >= 900 or width_i >= 1600:
        return "1080p"
    return "sd_hd"


def duration_bucket(value) -> str:
    try:
        minutes = float(value or 0.0) / 60.0
    except (TypeError, ValueError):
        minutes = 0.0
    if minutes <= 0:
        return "unknown"
    if minutes < 30:
        return "short_episode"
    if minutes <= 75:
        return "episode"
    if minutes <= 150:
        return "feature"
    return "long_feature"


def _language_key(value) -> str:
    if isinstance(value, str):
        values = re.split(r"[\s,;]+", value)
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = []
    return ",".join(sorted({str(item).strip().lower() for item in values if str(item).strip()}))[:40]


def feedback_context(plan: dict, candidate_id: str = "manual") -> dict:
    probe = plan.get("probe") if isinstance(plan.get("probe"), dict) else {}
    options = plan.get("options") if isinstance(plan.get("options"), dict) else {}
    estimates = plan.get("estimates") if isinstance(plan.get("estimates"), dict) else {}
    inputs = plan.get("inputs") if isinstance(plan.get("inputs"), dict) else {}
    output = estimates.get("output_resolution") if isinstance(estimates.get("output_resolution"), dict) else {}
    source_bytes = max(1, int(probe.get("source_size_bytes") or 1))
    target_bytes = max(1.0, float(inputs.get("target_mb") or 0.0) * 1024.0 * 1024.0)
    return {
        "candidate_id": str(candidate_id or "manual")[:40],
        "source": {
            "kind": str(probe.get("source_type") or "unknown")[:20],
            "hdr": bool(probe.get("is_hdr")),
            "resolution": resolution_bucket(probe.get("width"), probe.get("height")),
            "duration": duration_bucket(probe.get("duration_sec")),
        },
        "features": {
            "goal": str(options.get("ai_goal") or "balanced")[:20],
            "codec": str(options.get("video_codec") or "")[:20],
            "encoder_family": str(options.get("encoder_family") or "")[:20],
            "bit_depth": str(options.get("bit_depth") or "")[:8],
            "quality": str(options.get("quality") or "")[:20],
            "speed": str(options.get("encoder_speed") or "")[:20],
            "output_resolution": resolution_bucket(output.get("width"), output.get("height")),
            "resolution_mode": str(options.get("resolution_mode") or "")[:20],
            "target_ratio": round(target_bytes / float(source_bytes), 4),
            "target_size_auto": bool(options.get("target_size_auto")),
            "audio_strategy": str(options.get("smart_audio_strategy") or options.get("audio_mode") or "")[:24],
            "audio_languages": _language_key(options.get("audio_languages")),
            "subtitle_mode": str(options.get("subtitle_mode") or "")[:20],
            "subtitle_languages": _language_key(options.get("subtitle_languages")),
            "framerate_mode": str(options.get("framerate_mode") or "")[:12],
        },
        "plan": {
            "preset": str(plan.get("preset") or "")[:20],
            "target_mb": round(float(inputs.get("target_mb") or 0.0), 2),
            "video_bitrate_kbps": round(float(estimates.get("video_bitrate_kbps") or 0.0), 1),
            "encoder": str(estimates.get("encoder") or "")[:40],
            "quality_code": str(estimates.get("quality_code") or "")[:20],
        },
    }


def learned_plan_defaults(probe: dict, state: dict | None = None) -> dict:
    """Summarize approved choices from genuinely similar sources.

    This feeds the next Smart recommendation without weakening HDR/FPS/audio
    guardrails. Queueing a Size Wizard plan is an explicit approval of that
    configuration, while a later post-encode rejection can still correct it.
    """
    state = state if isinstance(state, dict) else load_state()
    probe = probe if isinstance(probe, dict) else {}
    source = {
        "kind": str(probe.get("source_type") or probe.get("kind") or "unknown")[:20],
        "hdr": bool(probe.get("is_hdr", probe.get("hdr", False))),
        "resolution": resolution_bucket(probe.get("width"), probe.get("height")),
        "duration": duration_bucket(probe.get("duration_sec")),
    }
    categorical = {
        key: {}
        for key in (
            "goal",
            "codec",
            "encoder_family",
            "bit_depth",
            "quality",
            "speed",
            "output_resolution",
            "resolution_mode",
            "audio_strategy",
            "audio_languages",
            "subtitle_mode",
            "subtitle_languages",
        )
    }
    ratio_total = 0.0
    ratio_weight = 0.0
    total_weight = 0.0
    samples = 0

    for row in reversed((state.get("feedback") or [])[-250:]):
        if not isinstance(row, dict) or row.get("verdict") != "approve":
            continue
        context = row.get("context") if isinstance(row.get("context"), dict) else {}
        saved_source = context.get("source") if isinstance(context.get("source"), dict) else {}
        saved_features = context.get("features") if isinstance(context.get("features"), dict) else {}
        # Never transfer SDR choices to HDR content (or the reverse), and do
        # not make a movie preference the default for episodic material.
        if bool(saved_source.get("hdr")) != source["hdr"]:
            continue
        saved_kind = str(saved_source.get("kind") or "unknown")
        if source["kind"] != "unknown" and saved_kind != "unknown" and saved_kind != source["kind"]:
            continue
        weight = 0.45
        if saved_kind == source["kind"]:
            weight += 0.20
        if str(saved_source.get("resolution") or "") == source["resolution"]:
            weight += 0.35
        saved_duration = str(saved_source.get("duration") or "unknown")
        if saved_duration != "unknown" and saved_duration == source["duration"]:
            weight += 0.18
        if str(row.get("origin") or "") == "wizard_queue":
            weight *= 1.12
        total_weight += weight
        samples += 1

        for key, votes in categorical.items():
            value = str(saved_features.get(key) or "").strip().lower()
            if value:
                votes[value] = float(votes.get(value) or 0.0) + weight
        try:
            ratio = float(saved_features.get("target_ratio") or 0.0)
        except (TypeError, ValueError):
            ratio = 0.0
        if 0.01 <= ratio <= 1.5:
            ratio_total += ratio * weight
            ratio_weight += weight

    if not samples or total_weight <= 0:
        return {"sample_count": 0, "confidence": 0.0, "source": source}

    result = {
        "sample_count": samples,
        "confidence": round(min(1.0, total_weight / 3.0), 3),
        "source": source,
    }
    for key, votes in categorical.items():
        if votes:
            result[key] = max(votes.items(), key=lambda item: item[1])[0]
    if ratio_weight:
        result["target_ratio"] = round(max(0.03, min(1.25, ratio_total / ratio_weight)), 4)
    return result


def _similarity(left: dict, right: dict) -> float:
    left_source = left.get("source") if isinstance(left.get("source"), dict) else {}
    right_source = right.get("source") if isinstance(right.get("source"), dict) else {}
    left_features = left.get("features") if isinstance(left.get("features"), dict) else {}
    right_features = right.get("features") if isinstance(right.get("features"), dict) else {}

    score = 0.12
    if left_source.get("kind") == right_source.get("kind"):
        score += 0.14
    if bool(left_source.get("hdr")) == bool(right_source.get("hdr")):
        score += 0.14
    if left_source.get("resolution") == right_source.get("resolution"):
        score += 0.14
    for key, weight in (
        ("codec", 0.12),
        ("encoder_family", 0.08),
        ("quality", 0.07),
        ("output_resolution", 0.08),
        ("audio_strategy", 0.05),
        ("audio_languages", 0.03),
        ("subtitle_languages", 0.03),
    ):
        if left_features.get(key) == right_features.get(key):
            score += weight
    try:
        ratio_delta = abs(float(left_features.get("target_ratio") or 0) - float(right_features.get("target_ratio") or 0))
        score += 0.11 * math.exp(-ratio_delta * 7.0)
    except (TypeError, ValueError):
        pass
    return max(0.05, min(1.0, score))


def candidate_learning(context: dict, state: dict | None = None) -> dict:
    state = state if isinstance(state, dict) else load_state()
    positive = 0.0
    negative = 0.0
    related = 0
    for row in state.get("feedback") or []:
        saved_context = row.get("context") if isinstance(row.get("context"), dict) else {}
        weight = _similarity(context, saved_context)
        if weight < 0.24:
            continue
        related += 1
        if row.get("verdict") == "approve":
            positive += weight
        else:
            reason = str(row.get("reason") or "")
            candidate_features = context.get("features") if isinstance(context.get("features"), dict) else {}
            saved_features = saved_context.get("features") if isinstance(saved_context.get("features"), dict) else {}
            try:
                candidate_ratio = float(candidate_features.get("target_ratio") or 0)
                saved_ratio = float(saved_features.get("target_ratio") or 0)
            except (TypeError, ValueError):
                candidate_ratio = saved_ratio = 0.0

            # A rejection teaches both what failed and which safe direction is
            # more promising. Quality complaints favor a roomier target;
            # size complaints favor a smaller one. The correction is weaker
            # than an explicit approval and still requires later confirmation.
            corrective = (
                reason == "quality" and candidate_ratio > saved_ratio * 1.05
            ) or (
                reason == "size" and 0 < candidate_ratio < saved_ratio * 0.95
            )
            if corrective:
                positive += weight * 0.65
            else:
                negative += weight
    evidence = positive + negative
    acceptance = (1.0 + positive) / (2.0 + evidence)
    confidence = min(1.0, evidence / max(1.0, float(state.get("profile", {}).get("minimum_feedback") or 3)))
    return {
        "acceptance": round(acceptance, 3),
        "confidence": round(confidence, 3),
        "weighted_evidence": round(evidence, 2),
        "related_reviews": related,
    }


def learning_status(state: dict | None = None) -> dict:
    state = state if isinstance(state, dict) else load_state()
    profile = state.get("profile") if isinstance(state.get("profile"), dict) else default_profile()
    rows = state.get("feedback") if isinstance(state.get("feedback"), list) else []
    approvals = sum(1 for row in rows if row.get("verdict") == "approve")
    rejections = sum(1 for row in rows if row.get("verdict") == "reject")
    count = approvals + rejections
    posterior = (1.0 + approvals) / (2.0 + count)
    evidence_confidence = min(1.0, count / max(1.0, float(profile.get("minimum_feedback") or 3)))
    ready = bool(
        profile.get("automation_enabled")
        and count >= int(profile.get("minimum_feedback") or 3)
        and approvals >= 2
        and posterior >= float(profile.get("confidence_threshold") or 0.72)
    )
    needed = max(0, int(profile.get("minimum_feedback") or 3) - count)
    return {
        "feedback_count": count,
        "approvals": approvals,
        "rejections": rejections,
        "approval_probability": round(posterior, 3),
        "evidence_confidence": round(evidence_confidence, 3),
        "automation_enabled": bool(profile.get("automation_enabled")),
        "automation_ready": ready,
        "reviews_needed": needed,
        "message": (
            "Learned automation is ready for familiar sources."
            if ready
            else (
                f"Approve {needed} more preview or queued Wizard plan{'s' if needed != 1 else ''} to unlock automation."
                if needed
                else "More consistent approvals are needed before automation unlocks."
            )
        ),
    }


def record_feedback(
    context: dict,
    verdict: str,
    reason: str = "",
    *,
    origin: str = "preview",
    job_id: str = "",
) -> dict:
    verdict = str(verdict or "").strip().lower()
    if verdict not in {"approve", "reject"}:
        raise ValueError("verdict must be approve or reject")
    reason = str(reason or ("looks_good" if verdict == "approve" else "other")).strip().lower()
    allowed_reasons = {
        "looks_good", "quality", "size", "speed", "compatibility",
        "audio", "subtitles", "playback", "other",
    }
    if reason not in allowed_reasons:
        reason = "other"
    if not isinstance(context, dict) or not isinstance(context.get("features"), dict):
        raise ValueError("invalid preview feedback context")

    with SMART_LOCK:
        state = load_state()
        row = {
            "id": uuid.uuid4().hex,
            "profile_id": "default",
            "verdict": verdict,
            "reason": reason,
            "context": context,
            "origin": str(origin or "preview")[:40],
            "job_id": str(job_id or "")[:80],
            "created_at": time.time(),
        }
        state["feedback"] = (state.get("feedback") or [])[-(SMART_FEEDBACK_LIMIT - 1):] + [row]
        _save_state_unlocked(state)
        return {"feedback": row, "learning": learning_status(state)}


def public_state() -> dict:
    state = load_state()
    recent = []
    for row in reversed(state.get("feedback") or []):
        context = row.get("context") if isinstance(row.get("context"), dict) else {}
        source = context.get("source") if isinstance(context.get("source"), dict) else {}
        plan = context.get("plan") if isinstance(context.get("plan"), dict) else {}
        recent.append(
            {
                "id": row.get("id"),
                "verdict": row.get("verdict"),
                "reason": row.get("reason"),
                "created_at": row.get("created_at"),
                "origin": row.get("origin") or "preview",
                "job_id": row.get("job_id") or "",
                "source": source,
                "plan": plan,
            }
        )
        if len(recent) >= 8:
            break
    return {"profile": state["profile"], "learning": learning_status(state), "recent_feedback": recent}
