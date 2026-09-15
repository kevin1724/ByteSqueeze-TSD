"""Fallback media artwork and TV release metadata.

The catalog's outer resolver prefers TMDb whenever credentials are configured.
This module supplies the no-key fallback: artwork stored beside the user's
media, TVmaze for shows, and Apple Search for movies. Responses are cached on
disk so normal incremental scans do not hammer providers or make the UI depend
on a live request.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import html
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .config import DATA_DIR


METADATA_CACHE_FILE = os.path.join(DATA_DIR, "media_metadata_cache.json")
ARTWORK_DIR = os.path.join(DATA_DIR, "media_artwork")
TVMAZE_ATTRIBUTION = {
    "name": "TVmaze",
    "url": "https://www.tvmaze.com/",
    "license": "CC BY-SA",
}
APPLE_ATTRIBUTION = {
    "name": "Apple Search",
    "url": "https://www.apple.com/apple-tv-app/",
}
METADATA_CACHE_VERSION = 3

_CACHE_LOCK = threading.RLock()
_CACHE_MEMORY: dict = {"signature": None, "data": None}
_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_AT: dict[str, float] = {}
_SIDECAR_NAMES = (
    "poster.jpg",
    "poster.jpeg",
    "poster.png",
    "poster.webp",
    "folder.jpg",
    "folder.jpeg",
    "folder.png",
    "cover.jpg",
    "cover.jpeg",
    "cover.png",
)
_VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".m4v",
    ".avi",
    ".mov",
    ".wmv",
    ".ts",
    ".m2ts",
    ".webm",
    ".mpg",
    ".mpeg",
}


def _empty_cache() -> dict:
    return {"version": METADATA_CACHE_VERSION, "entries": {}, "updated_at": 0.0}


def _cache_signature():
    try:
        stat = os.stat(METADATA_CACHE_FILE)
        return int(stat.st_mtime_ns), int(stat.st_size)
    except OSError:
        return None


def _load_cache() -> dict:
    signature = _cache_signature()
    if _CACHE_MEMORY.get("data") is not None and _CACHE_MEMORY.get("signature") == signature:
        return _CACHE_MEMORY["data"]
    try:
        with open(METADATA_CACHE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = _empty_cache()
    if not isinstance(data, dict):
        data = _empty_cache()
    elif data.get("version") != METADATA_CACHE_VERSION:
        # Version 1 could cache year-only Apple matches, including unrelated
        # movies. Rebuild those entries using the strict title matcher.
        data = _empty_cache()
    data.setdefault("entries", {})
    if not isinstance(data["entries"], dict):
        data["entries"] = {}
    _CACHE_MEMORY.update({"signature": signature, "data": data})
    return data


def _save_cache(data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    data["updated_at"] = time.time()
    temp_path = f"{METADATA_CACHE_FILE}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, METADATA_CACHE_FILE)
    _CACHE_MEMORY.update({"signature": _cache_signature(), "data": data})


def _cache_key(kind: str, title: str, year=None, country: str = "") -> str:
    return f"{kind}:{_normal_title(title)}:{str(year or '')}:{str(country or '').lower()}"


def _normal_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _title_tokens(value: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", str(value or "").lower()) if token not in {"the", "a", "an"}]


def _strip_markup(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _http_json(url: str, *, timeout: int = 10) -> dict | list:
    """Fetch JSON with provider-friendly pacing and a descriptive user agent."""
    host = (urlparse(url).hostname or "provider").lower()
    # Apple documents an approximate 20-call/minute limit. TVmaze guarantees
    # at least 20 calls per 10 seconds. Pace each provider to its slower public
    # limit; the disk cache means this mainly affects the first catalog scan.
    interval = 3.1 if host.endswith("itunes.apple.com") else 0.55
    with _REQUEST_LOCK:
        delay = interval - (time.monotonic() - float(_LAST_REQUEST_AT.get(host) or 0.0))
        if delay > 0:
            time.sleep(delay)
        request = Request(
            url,
            headers={
                "accept": "application/json",
                "user-agent": "ByteSqueeze/3.12 (+https://github.com/kevin1724/ByteSqueeze-TSD)",
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        finally:
            _LAST_REQUEST_AT[host] = time.monotonic()
    return payload


def _sidecar_directories(paths: list[str], *, include_common: bool = False) -> list[str]:
    """Find title-owned artwork folders without leaking a shared root poster."""
    directories = []
    seen = set()
    path_directories = []
    for raw in paths or []:
        path = str(raw or "").strip()
        if not path:
            continue
        current = path if os.path.isdir(path) else os.path.dirname(path)
        if not current:
            continue
        path_directories.append(current)
        key = os.path.normcase(os.path.realpath(current))
        if key not in seen:
            seen.add(key)
            directories.append(current)
    # Episodes may live in several Season folders. Their common directory is
    # the show folder and is safe to inspect; arbitrary parents are not.
    if include_common and len(path_directories) > 1:
        try:
            common = os.path.commonpath(path_directories)
        except ValueError:
            common = ""
        key = os.path.normcase(os.path.realpath(common)) if common else ""
        if common and key not in seen:
            directories.insert(0, common)
    return directories


def _cache_sidecar(paths: list[str], *, include_common: bool = False) -> dict:
    for directory in _sidecar_directories(paths, include_common=include_common):
        if not include_common and not _movie_sidecar_directory_is_dedicated(directory, paths):
            continue
        for name in _SIDECAR_NAMES:
            source = os.path.join(directory, name)
            if not os.path.isfile(source):
                continue
            try:
                stat = os.stat(source)
                digest = hashlib.sha256(
                    f"{os.path.realpath(source)}:{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8", errors="ignore")
                ).hexdigest()[:28]
                extension = os.path.splitext(source)[1].lower()
                target_name = f"local-{digest}{extension}"
                os.makedirs(ARTWORK_DIR, exist_ok=True)
                target = os.path.join(ARTWORK_DIR, target_name)
                if not os.path.isfile(target):
                    shutil.copy2(source, target)
                return {
                    "poster_url": f"/api/media/artwork/{target_name}",
                    "metadata_source": "local",
                    "metadata_provider": {"name": "Local artwork"},
                }
            except OSError:
                continue
    return {}


def _movie_sidecar_directory_is_dedicated(directory: str, paths: list[str]) -> bool:
    """Reject generic movie artwork stored beside unrelated loose movies."""
    try:
        video_paths = {
            os.path.normcase(os.path.realpath(entry.path))
            for entry in os.scandir(directory)
            if entry.is_file() and os.path.splitext(entry.name)[1].lower() in _VIDEO_EXTENSIONS
        }
    except OSError:
        return False

    requested_paths = {
        os.path.normcase(os.path.realpath(str(path)))
        for path in paths or []
        if path and not os.path.isdir(str(path))
    }
    # A title folder normally contains one movie. Multiple versions are also
    # safe when the caller explicitly identifies all of them as the same item.
    return len(video_paths) <= 1 or (bool(video_paths) and video_paths.issubset(requested_paths))


def _year(value) -> int | None:
    match = re.match(r"((?:19|20)\d{2})", str(value or ""))
    return int(match.group(1)) if match else None


def _choose_show(rows: list, title: str, year=None) -> dict | None:
    wanted = _normal_title(title)
    wanted_tokens = set(_title_tokens(title))
    wanted_year = _year(year)
    ranked = []
    for wrapper in rows if isinstance(rows, list) else []:
        show = wrapper.get("show") if isinstance(wrapper, dict) else None
        if not isinstance(show, dict):
            continue
        candidate = _normal_title(show.get("name"))
        candidate_tokens = set(_title_tokens(show.get("name")))
        if not wanted or not candidate:
            continue
        candidate_year = _year(show.get("premiered"))
        exact = candidate == wanted
        similarity = difflib.SequenceMatcher(None, wanted, candidate).ratio()
        shared = len(wanted_tokens & candidate_tokens)
        coverage = shared / max(1, len(wanted_tokens))
        candidate_coverage = shared / max(1, len(candidate_tokens))
        title_fit = exact or similarity >= 0.74 or (coverage >= 0.75 and candidate_coverage >= 0.6)
        if not title_fit:
            continue
        if wanted_year is not None and candidate_year is not None:
            year_delta = abs(candidate_year - wanted_year)
            if year_delta > 1:
                continue
            year_score = 1.5 if year_delta == 0 else 0.5
        else:
            year_score = 0.25
        provider_score = min(1.0, max(0.0, float(wrapper.get("score") or 0.0)))
        score = (6.0 if exact else 0.0) + similarity * 4.0 + coverage * 2.0 + candidate_coverage + year_score + provider_score
        ranked.append((score, similarity, show))
    ranked.sort(key=lambda row: row[0], reverse=True)
    return ranked[0][2] if ranked else None


def _episode_row(episode: dict, show: dict) -> dict:
    image = episode.get("image") if isinstance(episode.get("image"), dict) else {}
    return {
        "id": episode.get("id"),
        "show_id": show.get("id"),
        "show_title": show.get("name") or "Unknown show",
        "season": episode.get("season"),
        "episode": episode.get("number"),
        "name": episode.get("name") or "Untitled episode",
        "airdate": episode.get("airdate") or "",
        "airtime": episode.get("airtime") or "",
        "airstamp": episode.get("airstamp") or "",
        "runtime": episode.get("runtime"),
        "image_url": image.get("original") or image.get("medium") or "",
        "summary": _strip_markup(episode.get("summary"))[:500],
        "url": episode.get("url") or "",
    }


def _show_remote(title: str, year=None) -> dict:
    query = urlencode({"q": title})
    rows = _http_json(f"https://api.tvmaze.com/search/shows?{query}")
    show = _choose_show(rows if isinstance(rows, list) else [], title, year)
    if not show:
        return {"metadata_source": "tvmaze_empty", "metadata_error": "No matching TVmaze show."}

    show_id = show.get("id")
    episodes_payload = _http_json(f"https://api.tvmaze.com/shows/{show_id}/episodes?specials=1") if show_id else []
    episodes = [_episode_row(row, show) for row in episodes_payload if isinstance(row, dict)] if isinstance(episodes_payload, list) else []
    today = datetime.now(timezone.utc).date().isoformat()
    recent_and_future = [row for row in episodes if str(row.get("airdate") or "") >= today]
    image = show.get("image") if isinstance(show.get("image"), dict) else {}
    return {
        "poster_url": image.get("original") or image.get("medium") or "",
        "metadata_source": "tvmaze",
        "metadata_provider": TVMAZE_ATTRIBUTION,
        "tvmaze_id": show_id,
        "metadata_url": show.get("url") or "",
        "show_status": show.get("status") or "",
        "network": ((show.get("network") or show.get("webChannel") or {}).get("name") if isinstance(show.get("network") or show.get("webChannel"), dict) else ""),
        "genres": show.get("genres") if isinstance(show.get("genres"), list) else [],
        "summary": _strip_markup(show.get("summary"))[:900],
        "next_episode": recent_and_future[0] if recent_and_future else None,
        "release_calendar": recent_and_future[:80],
        "metadata_match_title": show.get("name") or "",
        "metadata_match_year": _year(show.get("premiered")),
        "metadata_provider_id": show_id,
    }


def _choose_movie(rows: list, title: str, year=None) -> dict | None:
    wanted = _normal_title(title)
    wanted_tokens = set(_title_tokens(title))
    wanted_year = _year(year)
    ranked = []
    for movie in rows if isinstance(rows, list) else []:
        if not isinstance(movie, dict):
            continue
        candidate = _normal_title(movie.get("trackName"))
        candidate_tokens = set(_title_tokens(movie.get("trackName")))
        if not wanted or not candidate:
            continue
        candidate_year = _year(movie.get("releaseDate"))
        exact = candidate == wanted
        similarity = difflib.SequenceMatcher(None, wanted, candidate).ratio()
        shared = len(wanted_tokens & candidate_tokens)
        coverage = shared / max(1, len(wanted_tokens))
        candidate_coverage = shared / max(1, len(candidate_tokens))
        title_fit = exact or similarity >= 0.72 or (coverage >= 0.75 and candidate_coverage >= 0.6)
        if not title_fit:
            continue
        if wanted_year is not None and candidate_year is not None:
            year_delta = abs(candidate_year - wanted_year)
            if year_delta > 1:
                continue
            year_score = 1.5 if year_delta == 0 else 0.5
        else:
            year_score = 0.25
        score = (5.0 if exact else 0.0) + similarity * 4.0 + coverage * 2.0 + candidate_coverage + year_score
        ranked.append((score, similarity, movie))
    ranked.sort(key=lambda row: row[0], reverse=True)
    return ranked[0][2] if ranked else None


def _apple_artwork_url(value: str) -> str:
    url = str(value or "")
    return re.sub(r"/\d+x\d+(?:bb)?\.(jpg|png)$", r"/600x900bb.\1", url, flags=re.IGNORECASE)


def _movie_remote(title: str, year=None, country: str = "US") -> dict:
    query = urlencode({"term": title, "country": country, "media": "movie", "entity": "movie", "limit": 12})
    payload = _http_json(f"https://itunes.apple.com/search?{query}")
    rows = payload.get("results") if isinstance(payload, dict) else []
    movie = _choose_movie(rows if isinstance(rows, list) else [], title, year)
    if not movie:
        return {"metadata_source": "apple_empty", "metadata_error": "No matching Apple movie artwork."}
    return {
        "poster_url": _apple_artwork_url(movie.get("artworkUrl100")),
        "metadata_source": "apple",
        "metadata_provider": APPLE_ATTRIBUTION,
        "metadata_url": movie.get("trackViewUrl") or movie.get("collectionViewUrl") or "",
        "summary": str(movie.get("longDescription") or movie.get("shortDescription") or "")[:900],
        "genres": [movie.get("primaryGenreName")] if movie.get("primaryGenreName") else [],
        "release_date": movie.get("releaseDate") or "",
        "metadata_match_title": movie.get("trackName") or "",
        "metadata_match_year": _year(movie.get("releaseDate")),
        "metadata_provider_id": movie.get("trackId") or movie.get("collectionId"),
    }


def lookup(kind: str, title: str, year=None, paths: list[str] | None = None, *, country: str = "US", refresh_hours: int = 12) -> dict:
    """Return local or cached keyless metadata for one movie/show."""
    kind = "show" if str(kind).lower() == "show" else "movie"
    paths = paths or []
    local = _cache_sidecar(paths, include_common=kind == "show")
    key = _cache_key(kind, title, year, country if kind == "movie" else "")
    now = time.time()

    with _CACHE_LOCK:
        cache = _load_cache()
        cached = cache["entries"].get(key) if isinstance(cache["entries"].get(key), dict) else {}
        fresh = cached and now - float(cached.get("cached_at") or 0) < max(1, int(refresh_hours)) * 3600
        if fresh:
            result = copy.deepcopy(cached.get("value") or {})
        else:
            try:
                result = _show_remote(title, year) if kind == "show" else _movie_remote(title, year, country)
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError) as error:
                result = copy.deepcopy(cached.get("value") or {})
                if not result:
                    result = {"metadata_source": "keyless_error", "metadata_error": str(error)[:200]}
            cache["entries"][key] = {"cached_at": now, "value": result}
            _save_cache(cache)

    if local.get("poster_url"):
        result = {**result, **local}
    result["metadata_keyless"] = True
    return result


def enrich_library(data: dict, settings: dict | None = None) -> dict:
    """Attach keyless artwork, descriptions, and release metadata in place."""
    settings = settings or {}
    if not settings.get("metadata_no_key_enabled", True):
        return data
    country = str(settings.get("metadata_country") or "US").strip().upper()[:2] or "US"
    refresh_hours = int(settings.get("episode_release_refresh_hours") or 12)

    for movie in data.get("movies") or []:
        if not isinstance(movie, dict):
            continue
        # TMDb was already resolved by the catalog pipeline. Avoid an unused
        # Apple request when the preferred provider supplied valid artwork.
        if movie.get("poster_source") == "tmdb" and movie.get("poster_url"):
            continue
        paths = [movie.get("path")] + list(movie.get("paths") or [])
        metadata = lookup("movie", movie.get("title") or "", movie.get("year"), paths, country=country, refresh_hours=max(24, refresh_hours))
        for key, value in metadata.items():
            if value not in (None, "", []) or key not in movie:
                movie[key] = value

    release_rows = []
    for show in data.get("shows") or []:
        if not isinstance(show, dict):
            continue
        paths = [row.get("path") for row in show.get("files") or [] if isinstance(row, dict)]
        metadata = lookup("show", show.get("title") or "", show.get("year"), paths, country=country, refresh_hours=refresh_hours)
        for key, value in metadata.items():
            if value not in (None, "", []) or key not in show:
                show[key] = value
        for episode in show.get("release_calendar") or []:
            if isinstance(episode, dict):
                release_rows.append({**episode, "library_show_id": show.get("id"), "poster_url": show.get("poster_url") or ""})

    release_rows.sort(key=lambda row: (str(row.get("airstamp") or row.get("airdate") or ""), str(row.get("show_title") or "")))
    data["release_calendar"] = {
        "generated_at": time.time(),
        "provider": TVMAZE_ATTRIBUTION,
        "episodes": release_rows[:500],
    }
    data["metadata"] = {
        "keyless": True,
        "providers": [TVMAZE_ATTRIBUTION, APPLE_ATTRIBUTION],
        "local_artwork": True,
    }
    return data


def artwork_path(filename: str) -> str | None:
    name = os.path.basename(str(filename or ""))
    if name != filename or not re.fullmatch(r"local-[a-f0-9]{28}\.(?:jpg|jpeg|png|webp)", name, flags=re.IGNORECASE):
        return None
    path = os.path.join(ARTWORK_DIR, name)
    return path if os.path.isfile(path) else None
