"""Minimal configuration required by the transfer and encode engine.

This worker build intentionally has no media roots, scanner, AI planner,
Size Wizard, metadata providers, templates, or static website assets.
"""

from __future__ import annotations

import os


VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts")

DATA_DIR = os.path.abspath(os.environ.get("HB_DATA_DIR") or "/work/state")
LOG_DIR = os.path.join(DATA_DIR, "logs")
JOBS_FILE = os.path.join(DATA_DIR, "jobs.json")
PRESET_CONFIG_FILE = os.path.join(DATA_DIR, "preset_config.json")
PRESET_DIR = os.path.abspath(os.environ.get("HB_PRESET_DIR") or "/presets")

# Headless workers never browse controller media. This prefix only prevents a
# shared helper from treating arbitrary worker filesystem paths as media roots.
WORK_DIR = os.path.abspath(os.environ.get("TSD_WORKER_TEMP_DIR") or "/work/jobs")
ROOTS: list[tuple[str, str]] = []
ALLOWED_PREFIXES = [WORK_DIR]

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

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
