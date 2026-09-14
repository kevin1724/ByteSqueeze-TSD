"""Versioned mobile pairing and token storage for future companion apps.

The web UI is the administrator: it creates a short-lived pairing code and can
revoke devices. Mobile clients receive opaque access/refresh tokens whose hashes
are the only credentials persisted on disk.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import threading
import time
import uuid
from copy import deepcopy

from .config import DATA_DIR


MOBILE_API_VERSION = "v1"
MOBILE_STATE_SCHEMA_VERSION = 1
MOBILE_STATE_FILE = os.path.join(DATA_DIR.rstrip("/"), "mobile_devices.json")
MOBILE_PAIRING_TTL_SECONDS = 10 * 60
MOBILE_PAIRING_RETRY_GRACE_SECONDS = 5 * 60
MOBILE_ACCESS_TTL_SECONDS = 30 * 24 * 60 * 60
MOBILE_REFRESH_TTL_SECONDS = 180 * 24 * 60 * 60
MOBILE_CAPABILITIES = [
    "status:read",
    "dashboard:read",
    "jobs:read",
    "jobs:control",
    "jobs:combined-nodes",
    "jobs:preset-edit",
    "library:read",
    "library:control",
    "nodes:read",
    "nodes:control",
    "events:read",
    "events:control",
    "automation:read",
    "automation:control",
    "storage:read",
    "smart-presets:read",
    "smart-presets:control",
    "queue:control",
    "token:refresh",
]
MOBILE_LOCK = threading.RLock()


def _now() -> float:
    return time.time()


def _hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8", errors="ignore")).hexdigest()


def _empty_state() -> dict:
    return {
        "schema_version": MOBILE_STATE_SCHEMA_VERSION,
        "pairing": {},
        "devices": {},
        "updated_at": 0,
    }


def _normalize(data) -> dict:
    if not isinstance(data, dict):
        data = _empty_state()
    data["schema_version"] = MOBILE_STATE_SCHEMA_VERSION
    data.setdefault("pairing", {})
    data.setdefault("devices", {})
    data.setdefault("updated_at", 0)
    return data


def _load_unlocked() -> dict:
    for path in (MOBILE_STATE_FILE, MOBILE_STATE_FILE + ".bak"):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = json.load(handle)
            if isinstance(value, dict):
                return _normalize(value)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
    return _empty_state()


def _write_atomic(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or DATA_DIR, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _save_unlocked(data: dict) -> None:
    data["updated_at"] = _now()
    _write_atomic(MOBILE_STATE_FILE, data)
    try:
        shutil.copy2(MOBILE_STATE_FILE, MOBILE_STATE_FILE + ".bak")
    except OSError:
        pass


def _mutate(callback):
    with MOBILE_LOCK:
        data = _load_unlocked()
        result = callback(data)
        _save_unlocked(data)
        return deepcopy(result)


def mobile_discovery() -> dict:
    return {
        "service": "handbrake-tsd-mobile-api",
        "api_version": MOBILE_API_VERSION,
        "state_schema_version": MOBILE_STATE_SCHEMA_VERSION,
        "capabilities": list(MOBILE_CAPABILITIES),
        "authentication": "Bearer",
    }


def _normalize_scope(value: str) -> str:
    return "read" if str(value or "").strip().lower() == "read" else "control"


def create_mobile_pairing(*, scope: str = "control", ttl_seconds: int = MOBILE_PAIRING_TTL_SECONDS) -> dict:
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    raw = "".join(secrets.choice(alphabet) for _ in range(8))
    code = f"{raw[:4]}-{raw[4:]}"
    now = _now()
    expires_at = now + max(60, min(3600, int(ttl_seconds or MOBILE_PAIRING_TTL_SECONDS)))
    normalized_scope = _normalize_scope(scope)

    def apply(data):
        data["pairing"] = {
            "code_hash": _hash(code),
            "created_at": now,
            "expires_at": expires_at,
            "scope": normalized_scope,
            "used_at": 0,
            "used_by_device_id": "",
            "attempt_count": 0,
        }

    _mutate(apply)
    return {"code": code, "expires_at": expires_at, "scope": normalized_scope}


def _new_credentials() -> tuple[str, str]:
    return secrets.token_urlsafe(40), secrets.token_urlsafe(48)


def accept_mobile_pairing(code: str, device: dict) -> dict:
    normalized_code = str(code or "").strip().upper()
    device_id = str(device.get("device_id") or device.get("id") or "").strip()[:120]
    if not device_id:
        device_id = uuid.uuid4().hex
    device_name = str(device.get("device_name") or device.get("name") or "Android device").strip()[:80]
    platform = str(device.get("platform") or "android").strip().lower()[:32]

    def apply(data):
        pairing = data.get("pairing") if isinstance(data.get("pairing"), dict) else {}
        now = _now()
        expected = str(pairing.get("code_hash") or "")
        if not expected or not hmac.compare_digest(expected, _hash(normalized_code)):
            raise ValueError("invalid mobile pairing code")
        if float(pairing.get("expires_at") or 0) < now:
            raise ValueError("mobile pairing code expired")

        used_at = float(pairing.get("used_at") or 0)
        used_by = str(pairing.get("used_by_device_id") or "")
        retrying_same_device = bool(
            used_at and used_by == device_id and now - used_at <= MOBILE_PAIRING_RETRY_GRACE_SECONDS
        )
        if used_at and not retrying_same_device:
            raise ValueError("mobile pairing code already used")

        access_token, refresh_token = _new_credentials()
        scope = _normalize_scope(pairing.get("scope") or "control")
        devices = data.setdefault("devices", {})
        existing = devices.get(device_id) if isinstance(devices.get(device_id), dict) else {}
        paired_at = float(existing.get("paired_at") or now)
        devices[device_id] = {
            **existing,
            "id": device_id,
            "name": device_name,
            "platform": platform,
            "scope": scope,
            "access_token_hash": _hash(access_token),
            "refresh_token_hash": _hash(refresh_token),
            "access_expires_at": now + MOBILE_ACCESS_TTL_SECONDS,
            "refresh_expires_at": now + MOBILE_REFRESH_TTL_SECONDS,
            "paired_at": paired_at,
            "last_seen": now,
            "revoked_at": 0,
        }
        pairing.update({
            "used_at": used_at or now,
            "used_by_device_id": device_id,
            "last_retry_at": now if retrying_same_device else 0,
            "attempt_count": int(pairing.get("attempt_count") or 0) + 1,
        })
        data["pairing"] = pairing
        return {
            "device_id": device_id,
            "device_name": device_name,
            "scope": scope,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "access_expires_at": now + MOBILE_ACCESS_TTL_SECONDS,
            "refresh_expires_at": now + MOBILE_REFRESH_TTL_SECONDS,
            "api_version": MOBILE_API_VERSION,
            "capabilities": list(MOBILE_CAPABILITIES),
            "retry_recovered": retrying_same_device,
        }

    return _mutate(apply)


def _public_device(row: dict) -> dict:
    row = row if isinstance(row, dict) else {}
    now = _now()
    revoked_at = float(row.get("revoked_at") or 0)
    access_expires_at = float(row.get("access_expires_at") or 0)
    return {
        "id": row.get("id") or "",
        "name": row.get("name") or "Mobile device",
        "platform": row.get("platform") or "android",
        "scope": _normalize_scope(row.get("scope") or "control"),
        "paired_at": float(row.get("paired_at") or 0),
        "last_seen": float(row.get("last_seen") or 0),
        "access_expires_at": access_expires_at,
        "revoked_at": revoked_at,
        "active": not revoked_at and access_expires_at > now,
    }


def list_mobile_devices() -> list[dict]:
    with MOBILE_LOCK:
        data = _load_unlocked()
        return [_public_device(row) for row in data.get("devices", {}).values() if isinstance(row, dict)]


def authenticate_mobile_token(token: str, *, required_scope: str = "read") -> dict | None:
    digest = _hash(str(token or "").strip())
    if not token:
        return None

    def apply(data):
        now = _now()
        for row in data.get("devices", {}).values():
            if not isinstance(row, dict):
                continue
            if row.get("revoked_at") or float(row.get("access_expires_at") or 0) <= now:
                continue
            if not hmac.compare_digest(str(row.get("access_token_hash") or ""), digest):
                continue
            if _normalize_scope(required_scope) == "control" and _normalize_scope(row.get("scope")) != "control":
                return None
            if now - float(row.get("last_seen") or 0) >= 30:
                row["last_seen"] = now
            return _public_device(row)
        return None

    return _mutate(apply)


def refresh_mobile_token(device_id: str, refresh_token: str) -> dict:
    device_id = str(device_id or "").strip()
    supplied_hash = _hash(str(refresh_token or "").strip())

    def apply(data):
        row = data.get("devices", {}).get(device_id)
        now = _now()
        if not isinstance(row, dict) or row.get("revoked_at"):
            raise ValueError("mobile device is not active")
        if float(row.get("refresh_expires_at") or 0) <= now:
            raise ValueError("mobile refresh token expired")
        if not refresh_token or not hmac.compare_digest(str(row.get("refresh_token_hash") or ""), supplied_hash):
            raise ValueError("mobile refresh token rejected")
        access, refresh = _new_credentials()
        row.update({
            "access_token_hash": _hash(access),
            "refresh_token_hash": _hash(refresh),
            "access_expires_at": now + MOBILE_ACCESS_TTL_SECONDS,
            "refresh_expires_at": now + MOBILE_REFRESH_TTL_SECONDS,
            "last_seen": now,
            "refreshed_at": now,
        })
        return {
            "device_id": device_id,
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "access_expires_at": row["access_expires_at"],
            "refresh_expires_at": row["refresh_expires_at"],
            "scope": _normalize_scope(row.get("scope")),
        }

    return _mutate(apply)


def revoke_mobile_device(device_id: str) -> bool:
    device_id = str(device_id or "").strip()

    def apply(data):
        row = data.get("devices", {}).get(device_id)
        if not isinstance(row, dict):
            return False
        row["revoked_at"] = _now()
        row["access_token_hash"] = ""
        row["refresh_token_hash"] = ""
        return True

    return _mutate(apply)


def forget_mobile_device(device_id: str) -> bool:
    """Permanently remove a paired app and invalidate its credentials."""
    device_id = str(device_id or "").strip()

    def apply(data):
        devices = data.setdefault("devices", {})
        row = devices.get(device_id)
        if not isinstance(row, dict):
            return False
        # Deleting the only persisted token hashes invalidates both access and
        # refresh credentials immediately.
        devices.pop(device_id, None)
        return True

    return _mutate(apply)


def clear_mobile_devices(*, inactive_only: bool = True) -> int:
    """Forget revoked/expired app records, or every app when explicitly asked."""

    def apply(data):
        devices = data.setdefault("devices", {})
        now = _now()
        remove_ids = []
        for device_id, row in devices.items():
            if not isinstance(row, dict):
                remove_ids.append(device_id)
                continue
            inactive = bool(row.get("revoked_at")) or float(row.get("refresh_expires_at") or 0) <= now
            if not inactive_only or inactive:
                remove_ids.append(device_id)
        for device_id in remove_ids:
            devices.pop(device_id, None)
        return len(remove_ids)

    return _mutate(apply)
