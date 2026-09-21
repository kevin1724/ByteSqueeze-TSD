"""
Global configuration for HandBrake TSD Helper.

This file centralizes:
- environment variables
- directory paths
- allowed media roots
- supported video extensions
"""

import os
import json

# -----------------------------
# Video formats we allow browsing/encoding
# -----------------------------
VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts")


# ============================================================
# MEDIA ROOTS
#
# We support:
# 1) Optional manual override via env HB_ROOTS_JSON
# 2) Otherwise, auto-discover roots under HB_MEDIA_BASE (default: /media)
#
# This makes it possible to have 1..infinity roots by just adding
# more volume mappings in docker-compose, e.g.:
#
#   - /path/to/movies:/media/movies
#   - /path/to/shows:/media/shows
#   - /path/to/anime:/media/anime
#
# The app will treat each /media/<subfolder> as a root.
# ============================================================

# Base directory inside the container where media volumes are mounted.
# Users generally should NOT need to change this; they just bind-mount into /media/*.
MEDIA_BASE = os.environ.get("HB_MEDIA_BASE", "/media")


def _roots_from_env():
    """
    Load ROOTS from HB_ROOTS_JSON env var if set.

    Expected env format (string JSON list):
      [
        ["/media/movies", "Movies"],
        ["/media/shows", "TV Shows"]
      ]

    Returns:
      list[(path, label)] or None if env not set / invalid.
    """
    raw = os.environ.get("HB_ROOTS_JSON")
    if not raw:
        return None  # env not set → let auto-discovery handle it

    try:
        parsed = json.loads(raw)
        roots = []
        for item in parsed:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                path, label = item
                roots.append((str(path), str(label)))
        if roots:
            print("Loaded ROOTS from HB_ROOTS_JSON:", roots)
            return roots

        print("WARNING: HB_ROOTS_JSON exists but had no valid entries, ignoring.")

    except Exception as e:
        print("WARNING: Failed to parse HB_ROOTS_JSON:", e)

    return None  # fall back to auto-discovery


def _auto_discover_roots():
    """
    Auto-discover roots under MEDIA_BASE.

    Logic:
      - If MEDIA_BASE exists and has subdirectories:
            create a root for each immediate subdirectory.
      - Else if MEDIA_BASE exists but no subdirs:
            use MEDIA_BASE itself as a single root.
      - Else:
            fall back to a single '/media' root label (even if it doesn't exist yet).

    This supports 1..infinity roots depending on how many volumes
    the user mounts into /media/*.
    """
    roots = []

    base = MEDIA_BASE
    if os.path.isdir(base):
        # List immediate subdirectories
        try:
            entries = sorted(os.listdir(base))
        except OSError as e:
            print(f"WARNING: Could not list {base}: {e}")
            entries = []

        for name in entries:
            full = os.path.join(base, name)
            if os.path.isdir(full):
                label = f"{name} ({full})"
                roots.append((full, label))

        if roots:
            print("Auto-discovered ROOTS under", base, ":", roots)
            return roots

        # Base exists but no subdirectories → use base itself
        print(f"MEDIA_BASE {base} exists but has no subfolders; using it as single root.")
        return [(base, f"Media ({base})")]

    # MEDIA_BASE doesn't exist (yet) – still return something sane
    print(f"WARNING: MEDIA_BASE {base} does not exist; using it as single root anyway.")
    return [(base, f"Media ({base})")]


def _load_roots():
    """
    Final ROOTS loader:

    1) Try env HB_ROOTS_JSON (power users).
    2) Otherwise, auto-discover under /media (or HB_MEDIA_BASE).
    """
    from_env = _roots_from_env()
    if from_env is not None:
        return from_env
    return _auto_discover_roots()


# Final list used by the app/UI
ROOTS = _load_roots()

# Used for validating user-submitted file paths
ALLOWED_PREFIXES = [r[0] for r in ROOTS]


# -----------------------------
# Persistent storage directories
# -----------------------------
DATA_DIR = os.environ.get("HB_DATA_DIR", "/app/data")
LOG_DIR = os.path.join(DATA_DIR, "logs")
JOBS_FILE = os.path.join(DATA_DIR, "jobs.json")
PRESET_CONFIG_FILE = os.path.join(DATA_DIR, "preset_config.json")

# Ensure directories exist
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)


# -----------------------------
# HandBrake preset directory
# -----------------------------
PRESET_DIR = os.environ.get("HB_PRESET_DIR", "/presets")


# -----------------------------
# Default fallback preset config
# Name fields are just human-readable labels
# -----------------------------
DEFAULT_PRESET_CONFIG = {
    "1080": {
        "file": os.path.join(PRESET_DIR, "Plex-AV1-1080p-CFR-CRF24-ENG-SPA.json"),
        "name": "Plex-AV1-1080p-CFR-CRF24-ENG-SPA",
    },
    "4k": {
        "file": os.path.join(PRESET_DIR, "Plex-AV1-4K-Source-Dimensions-CFR-CRF28-ENG-SPA.json"),
        "name": "Plex-AV1-4K-Source-Dimensions-CFR-CRF28-ENG-SPA",
    },
}

QSV_1080_PRESET_CONFIG = {
    "file": os.path.join(PRESET_DIR, "Plex-HEVC-QSV-1080p-ICQ28-Smaller-AudioCopy-ENG-SPA.json"),
    "name": "Plex-HEVC-QSV-1080p-ICQ28-Smaller-AudioCopy-ENG-SPA",
}
