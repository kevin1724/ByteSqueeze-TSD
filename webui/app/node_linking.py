from __future__ import annotations

import hashlib
import hmac
import csv
import io
import json
import os
import glob
import re
import secrets
import shutil
import subprocess
import platform
import tempfile
import threading
import time
import uuid
from copy import deepcopy
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import DATA_DIR
from .process_utils import background_process_options


NODE_LINK_FILE = os.path.join(DATA_DIR.rstrip("/"), "linked_nodes.json")
NODE_TRANSFER_FILE = os.path.join(DATA_DIR.rstrip("/"), "node_transfers.json")
PAIRING_TTL_SECONDS = 15 * 60
PAIRING_RETRY_GRACE_SECONDS = 5 * 60
HEARTBEAT_WARN_SECONDS = 2 * 60
HEARTBEAT_STALE_SECONDS = 10 * 60
HEARTBEAT_RUNNING_GRACE_SECONDS = 2 * 60 * 60
HEARTBEAT_MAX_MISSES = 3
TRANSFER_TTL_SECONDS = 48 * 60 * 60
NODE_STATE_SCHEMA_VERSION = 2
NODE_PROTOCOL_VERSION = 2
NODE_CAPABILITIES = [
    "heartbeat",
    "pair-recovery",
    "remote-transfer",
    "preset-bundle",
    "job-dispatch",
    "diagnostics",
    "controller-encoding-policy",
    "gpu-multi-encode",
    "cpu-software-exclusive",
    "output-size-guard",
    "remote-job-logs",
    "observed-controller-route",
    "resilient-transfer-download",
    "qsv-hardware-decode",
    "remote-job-history-control",
    "remote-job-control",
    "remote-queue-control",
    "remote-preset-edit",
    "hardware-profile",
]
STATE_LOCK = threading.RLock()
TRANSFER_LOCK = threading.RLock()
HARDWARE_PROFILE_LOCK = threading.RLock()
HARDWARE_PROFILE_CACHE = {"expires_at": 0.0, "value": None}
NVENC_PROBE_LOCK = threading.RLock()
NVENC_PROBE_CACHE: dict[tuple[str, str, str], tuple[float, bool, str]] = {}
HARDWARE_ENCODERS = {
    "qsv": ("qsv_h264", "qsv_h265", "qsv_h265_10bit", "qsv_av1", "qsv_av1_10bit"),
    "nvenc": ("nvenc_h264", "nvenc_h265", "nvenc_h265_10bit", "nvenc_av1", "nvenc_av1_10bit"),
    "vce": ("vce_h264", "vce_h265", "vce_h265_10bit", "vce_av1", "vce_av1_10bit"),
    "software": ("x264", "x264_10bit", "x265", "x265_10bit", "x265_12bit", "svt_av1", "svt_av1_10bit"),
}


def _now() -> float:
    return time.time()


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _read_gpu_vendor(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            value = handle.read().strip().lower()
    except OSError:
        return ""
    return {
        "0x8086": "intel",
        "8086": "intel",
        "0x1002": "amd",
        "1002": "amd",
        "0x10de": "nvidia",
        "10de": "nvidia",
    }.get(value, value)


def _gpu_vendor(name: str, pnp_device_id: str = "") -> str:
    value = f"{name} {pnp_device_id}".lower()
    if "nvidia" in value or "ven_10de" in value:
        return "nvidia"
    if any(token in value for token in ("amd", "radeon", "advanced micro devices", "ven_1002")):
        return "amd"
    if "intel" in value or "ven_8086" in value:
        return "intel"
    return "unknown"


def _pci_location_key(value: str) -> tuple[int, int, int] | None:
    """Normalize Windows location text and NVIDIA PCI bus IDs."""
    text = str(value or "").strip()
    windows = re.search(
        r"pci\s+bus\s+(\d+)\s*,\s*device\s+(\d+)\s*,\s*function\s+(\d+)",
        text,
        re.IGNORECASE,
    )
    if windows:
        return tuple(int(windows.group(index)) for index in (1, 2, 3))
    nvidia = re.search(
        r"(?:[0-9a-f]{4,8}:)?([0-9a-f]{2,4}):([0-9a-f]{2})\.([0-7])",
        text,
        re.IGNORECASE,
    )
    if nvidia:
        return (
            int(nvidia.group(1), 16),
            int(nvidia.group(2), 16),
            int(nvidia.group(3), 16),
        )
    return None


def _normalized_gpu_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _nvidia_smi_inventory() -> list[dict]:
    """Return NVIDIA's own adapter ordinals and stable physical identities."""
    command = [
        "nvidia-smi",
        "--query-gpu=index,uuid,pci.bus_id,name,memory.total,driver_version",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
            check=False,
            **background_process_options(),
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    rows = []
    for values in csv.reader(io.StringIO(result.stdout or ""), skipinitialspace=True):
        if len(values) < 6:
            continue
        try:
            vendor_index = int(str(values[0]).strip())
        except (TypeError, ValueError):
            continue
        pci_bus_id = str(values[2]).strip().lower()
        rows.append({
            "vendor_index": vendor_index,
            "gpu_uuid": str(values[1]).strip(),
            "pci_bus_id": pci_bus_id,
            "pci_location_key": _pci_location_key(pci_bus_id),
            "name": str(values[3]).strip(),
            "memory_bytes": max(0, _safe_int(values[4])) * 1024 * 1024,
            "driver_version": str(values[5]).strip(),
        })
    return rows


def _match_nvidia_identities(rows: list[dict], nvidia_rows: list[dict]) -> list[dict]:
    """Attach NVENC ordinals to WMI adapters without equating their indexes."""
    used: set[int] = set()
    name_counts: dict[str, int] = {}
    for candidate in nvidia_rows:
        name = _normalized_gpu_name(candidate.get("name"))
        name_counts[name] = name_counts.get(name, 0) + 1
    for row in rows:
        if str(row.get("vendor") or "") != "nvidia":
            continue
        match = None
        source = ""
        location = row.get("pci_location_key")
        if location:
            match = next(
                (
                    candidate for candidate in nvidia_rows
                    if int(candidate.get("vendor_index", -1)) not in used
                    and candidate.get("pci_location_key") == location
                ),
                None,
            )
            source = "pci_bus_id" if match else ""
        normalized_name = _normalized_gpu_name(row.get("name"))
        if match is None and normalized_name and name_counts.get(normalized_name) == 1:
            match = next(
                (
                    candidate for candidate in nvidia_rows
                    if int(candidate.get("vendor_index", -1)) not in used
                    and _normalized_gpu_name(candidate.get("name")) == normalized_name
                ),
                None,
            )
            source = "unique_name" if match else ""
        if match is None:
            continue
        vendor_index = int(match["vendor_index"])
        used.add(vendor_index)
        row.update({
            "vendor_index": vendor_index,
            "gpu_uuid": str(match.get("gpu_uuid") or ""),
            "pci_bus_id": str(match.get("pci_bus_id") or ""),
            "identity_source": source,
            # WMI AdapterRAM is a 32-bit field and commonly reports only 4 GiB.
            "memory_bytes": int(match.get("memory_bytes") or row.get("memory_bytes") or 0),
            "driver_version": str(match.get("driver_version") or row.get("driver_version") or ""),
        })
    return rows


def _windows_gpu_inventory() -> list[dict]:
    """Read display adapters without requiring vendor-specific SDKs."""
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            "Get-CimInstance Win32_VideoController | ForEach-Object { "
            "$location=(Get-PnpDeviceProperty -InstanceId $_.PNPDeviceID "
            "-KeyName 'DEVPKEY_Device_LocationInfo' -ErrorAction SilentlyContinue).Data; "
            "[pscustomobject]@{Name=$_.Name;PNPDeviceID=$_.PNPDeviceID;"
            "DriverVersion=$_.DriverVersion;AdapterRAM=$_.AdapterRAM;Status=$_.Status;"
            "LocationInfo=$location} } | "
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
            timeout=12,
            check=False,
            **background_process_options(),
        )
        payload = json.loads(result.stdout or "[]") if result.returncode == 0 else []
    except Exception:
        payload = []
    if isinstance(payload, dict):
        payload = [payload]
    rows = []
    for index, item in enumerate(payload if isinstance(payload, list) else []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or f"GPU {index + 1}").strip()
        pnp = str(item.get("PNPDeviceID") or "").strip()
        location_info = str(item.get("LocationInfo") or "").strip()
        rows.append({
            "index": index,
            "name": name,
            "vendor": _gpu_vendor(name, pnp),
            "driver_version": str(item.get("DriverVersion") or "").strip(),
            "memory_bytes": max(0, _safe_int(item.get("AdapterRAM"))),
            "status": str(item.get("Status") or "").strip(),
            "pnp_device_id": pnp,
            "location_info": location_info,
            "pci_location_key": _pci_location_key(location_info),
        })
    return _match_nvidia_identities(rows, _nvidia_smi_inventory())


def verify_windows_nvenc_encoder(
    gpu: dict,
    encoder: str,
    *,
    force: bool = False,
) -> tuple[bool, str]:
    """Verify AV1 on the mapped adapter using the worker's real encode engine.

    FFmpeg is bundled for probes and audio-only jobs, but it is not the engine
    used by normal video jobs. A newer FFmpeg can require a newer NVENC API
    than the bundled HandBrake build and must not incorrectly disqualify an
    otherwise working adapter. Generate a tiny software-only source with
    FFmpeg, then encode it through HandBrake using the exact NVENC ordinal.
    """
    encoder = str(encoder or "").strip().lower()
    if encoder not in {"nvenc_av1", "nvenc_av1_10bit"}:
        return True, "adapter-specific probe not required"
    try:
        vendor_index = int(gpu.get("vendor_index"))
    except (TypeError, ValueError):
        return False, "NVIDIA adapter has no mapped NVENC index"
    identity = str(
        gpu.get("gpu_uuid")
        or gpu.get("pci_bus_id")
        or gpu.get("name")
        or f"nvenc:{vendor_index}"
    )
    if not str(gpu.get("gpu_uuid") or gpu.get("pci_bus_id") or "").strip():
        return False, "NVIDIA adapter identity could not be matched by UUID or PCI bus"
    ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    handbrake = shutil.which("HandBrakeCLI") or shutil.which("HandBrakeCLI.exe")
    if not ffmpeg:
        return False, "FFmpeg is unavailable to create the AV1 capability test source"
    if not handbrake:
        return False, "HandBrakeCLI is unavailable for the AV1 capability check"
    cache_key = (identity, str(gpu.get("driver_version") or ""), f"handbrake:{encoder}")
    now = _now()
    with NVENC_PROBE_LOCK:
        cached = NVENC_PROBE_CACHE.get(cache_key)
        if not force and cached and cached[0] > now:
            return cached[1], cached[2]
    def failure_detail(result, fallback: str) -> str:
        lines = [
            line.strip()
            for line in (result.stderr or result.stdout or "").splitlines()
            if line.strip()
        ]
        # HandBrake ends most failures with the unhelpful generic
        # "Encode failed (error 3)."  Prefer the underlying NVENC/driver line
        # even though it appears earlier in the output.
        specific = next(
            (
                line for line in lines
                if any(
                    phrase in line.casefold()
                    for phrase in (
                        "driver does not support",
                        "no capable devices",
                        "cannot load nvencode",
                        "cannot load nvcuda",
                        "unsupported device",
                        "invalid param",
                        "avcodec_open failed",
                    )
                )
            ),
            "",
        )
        useful = next(
            (
                line for line in reversed(lines)
                if "encode failed (error " not in line.casefold()
                and any(word in line.casefold() for word in ("error", "failed", "nvenc"))
            ),
            "",
        )
        return specific or useful or (lines[-1] if lines else fallback)

    try:
        with tempfile.TemporaryDirectory(prefix="bytesqueeze-nvenc-") as temp_dir:
            source_path = os.path.join(temp_dir, "source.mkv")
            output_path = os.path.join(temp_dir, "probe.mkv")
            source_result = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner", "-loglevel", "error", "-y",
                    # Use a normal 1080p frame. Tiny sources can be rejected by
                    # hardware codec constraints even when the adapter works.
                    "-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=24:d=1",
                    "-an", "-c:v", "ffv1", source_path,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
                **background_process_options(),
            )
            if source_result.returncode != 0:
                ok = False
                detail = failure_detail(
                    source_result,
                    f"AV1 test source creation exited with code {source_result.returncode}",
                )
            else:
                # Some HandBrake NVENC builds can ignore FFmpeg's gpu=N option on a
                # multi-NVIDIA Windows host. Restrict CUDA enumeration by the
                # stable UUID, then address the selected device as logical
                # gpu=0. This preserves the physical WMI/nvidia-smi mapping
                # while avoiding HandBrake's ambiguous adapter order.
                gpu_uuid = str(gpu.get("gpu_uuid") or "").strip()
                probe_environment = os.environ.copy()
                probe_index = vendor_index
                if gpu_uuid:
                    probe_environment["CUDA_VISIBLE_DEVICES"] = gpu_uuid
                    probe_environment["NVIDIA_VISIBLE_DEVICES"] = gpu_uuid
                    probe_index = 0
                command = [
                    handbrake,
                    "-i", source_path,
                    "-o", output_path,
                    "--format", "av_mkv",
                    "--encoder", encoder,
                    "--quality", "35",
                    "--encopts", f"gpu={probe_index}",
                    "--no-markers",
                ]
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=45,
                    check=False,
                    env=probe_environment,
                    **background_process_options(),
                )
                ok = result.returncode == 0 and os.path.isfile(output_path) and os.path.getsize(output_path) > 0
                if not ok:
                    diagnostic_tail = "\n".join(
                        (result.stderr or result.stdout or "").splitlines()[-80:]
                    )
                    print(
                        f"[NVENC-PREFLIGHT] {encoder} adapter={vendor_index} "
                        f"identity={identity} failed:\n{diagnostic_tail}",
                        flush=True,
                    )
                detail = (
                    (
                        f"HandBrake {encoder} verified on physical NVENC adapter "
                        f"{vendor_index} (logical gpu={probe_index})"
                    )
                    if ok
                    else failure_detail(
                        result,
                        f"HandBrake capability probe exited with code {result.returncode}",
                    )
                )
    except Exception as exc:
        ok = False
        detail = f"HandBrake capability probe failed: {exc}"
    with NVENC_PROBE_LOCK:
        NVENC_PROBE_CACHE[cache_key] = (now + (3600.0 if ok else 300.0), ok, detail[:500])
    return ok, detail[:500]


def _windows_gpu_encoder_capabilities(gpu: dict, family: str, encoders: list[str]) -> list[str]:
    """Return conservative per-adapter capabilities from a machine-wide probe.

    HandBrake reports NVENC support for the machine, not for each physical
    adapter.  On mixed-generation systems that made an RTX 30-series adapter
    appear AV1-capable merely because an RTX 40/50-series adapter was also
    installed.  Remove codecs that are known to be absent on an adapter while
    leaving unknown/professional model names backward compatible.
    """
    supported = [name for name in encoders if name in HARDWARE_ENCODERS.get(family, ())]
    name = str(gpu.get("name") or "").strip().lower()
    if family == "nvenc":
        # AV1 NVENC starts with Ada-generation GeForce RTX 40-series hardware.
        # Turing (20), Ampere (30), GTX, and Quadro adapters cannot encode AV1.
        known_without_av1 = bool(
            re.search(r"(?:geforce\s+)?rtx\s*(?:20|30)\d{2}", name)
            or "geforce gtx" in name
            or "quadro" in name
            or re.search(r"\brtx\s+a\d{3,4}\b", name)
        )
        if known_without_av1:
            supported = [encoder for encoder in supported if "_av1" not in encoder]
    elif family == "qsv":
        # Intel Arc-class graphics expose AV1 encode. UHD/Iris generations can
        # commonly decode AV1 but must not be advertised as AV1 encoders.
        if "arc" not in name:
            supported = [encoder for encoder in supported if "_av1" not in encoder]
    elif family == "vce":
        # RDNA2 and older consumer Radeon parts do not provide AV1 encoding.
        known_without_av1 = bool(
            re.search(r"(?:radeon\s+)?rx\s*(?:4|5|6)\d{3}", name)
            or any(token in name for token in ("vega", " r9 ", " r7 "))
        )
        if known_without_av1:
            supported = [encoder for encoder in supported if "_av1" not in encoder]
    return supported


def _linux_qsv_av1_capability(render_devices: list[str]) -> tuple[bool, str]:
    """Verify AV1 encode support from the active Intel VA driver.

    HandBrake's help output describes what the binary was compiled to support,
    not what an older Intel GPU can encode.  `vainfo` is fast and reports the
    profiles exposed by the actual render node/driver, so it prevents AV1 Smart
    jobs from being routed to HEVC-only Quick Sync nodes.
    """
    vainfo = shutil.which("vainfo")
    if not vainfo:
        return False, "vainfo is unavailable; AV1 QSV was not advertised"
    if not render_devices:
        return False, "no Intel DRM render device is available"
    failures = []
    for render_device in render_devices:
        try:
            result = subprocess.run(
                [vainfo, "--display", "drm", "--device", render_device],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                check=False,
                **background_process_options(),
            )
        except Exception as exc:
            failures.append(f"{render_device}: {exc}")
            continue
        output = f"{result.stdout}\n{result.stderr}"
        if result.returncode == 0 and re.search(
            r"VAProfileAV1[^\r\n]*VAEntrypointEncSlice",
            output,
            flags=re.IGNORECASE,
        ):
            return True, f"AV1 encode profile verified on {render_device}"
        failures.append(f"{render_device}: VA driver exposes no AV1 encode profile")
    return False, "; ".join(failures)[:500]


def _requested_family_order() -> list[str]:
    requested = []
    for value in re.split(r"[,;\s]+", str(os.environ.get("TSD_ENCODER_FAMILIES") or "")):
        family = value.strip().lower()
        if family in HARDWARE_ENCODERS and family not in requested:
            requested.append(family)
    return requested


def encoder_hardware_profile(*, force: bool = False) -> dict:
    """Report encoder families that are usable by this node's container."""
    now = _now()
    with HARDWARE_PROFILE_LOCK:
        cached = HARDWARE_PROFILE_CACHE.get("value")
        if not force and isinstance(cached, dict) and float(HARDWARE_PROFILE_CACHE.get("expires_at") or 0) > now:
            return deepcopy(cached)

        windows = os.name == "nt"
        windows_gpus = _windows_gpu_inventory() if windows else []
        # Keep probing Unix device paths even on Windows. They are normally an
        # empty set there, and doing so keeps WSL/mocked diagnostics useful.
        render_devices = sorted(glob.glob("/dev/dri/renderD*"))
        render_vendors = {
            path: _read_gpu_vendor(
                f"/sys/class/drm/{os.path.basename(path)}/device/vendor"
            )
            for path in render_devices
        }
        vendors = {
            vendor
            for vendor in (
                _read_gpu_vendor(path)
                for path in glob.glob("/sys/class/drm/renderD*/device/vendor")
            )
            if vendor
        }
        vendors.update(
            row.get("vendor") for row in windows_gpus if row.get("vendor") != "unknown"
        )
        if render_devices and not vendors:
            driver = str(os.environ.get("LIBVA_DRIVER_NAME") or "").strip().lower()
            if driver == "ihd":
                vendors.add("intel")
            elif driver in {"radeonsi", "amd"}:
                vendors.add("amd")
        intel_render_devices = [
            path for path in render_devices if render_vendors.get(path) == "intel"
        ]
        if not intel_render_devices and "intel" in vendors and str(
            os.environ.get("LIBVA_DRIVER_NAME") or ""
        ).strip().lower() == "ihd":
            intel_render_devices = list(render_devices)

        nvidia_devices = sorted(glob.glob("/dev/nvidia[0-9]*"))
        if nvidia_devices:
            vendors.add("nvidia")
        if shutil.which("nvidia-smi"):
            try:
                probe = subprocess.run(
                    ["nvidia-smi", "-L"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                    **background_process_options(),
                )
                if probe.returncode == 0 and "GPU" in (probe.stdout or ""):
                    vendors.add("nvidia")
            except Exception:
                pass

        requested_order = _requested_family_order()
        families = set(requested_order)
        if "intel" in vendors and (render_devices or windows):
            families.add("qsv")
        if "nvidia" in vendors:
            families.add("nvenc")
        if "amd" in vendors and (render_devices or windows):
            families.add("vce")
        families.add("software")

        help_text = ""
        handbrake = shutil.which("HandBrakeCLI")
        if handbrake:
            try:
                help_result = subprocess.run(
                    [handbrake, "--help"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=12,
                    check=False,
                    **background_process_options(),
                )
                help_text = f"{help_result.stdout}\n{help_result.stderr}".lower()
            except Exception:
                help_text = ""

        encoders = []
        usable_families = set()
        for family in ("qsv", "nvenc", "vce", "software"):
            if family not in families:
                continue
            supported = [name for name in HARDWARE_ENCODERS[family] if not help_text or name.lower() in help_text]
            if not supported:
                if help_text and family != "software":
                    # A GPU device alone is not enough: the HandBrake build
                    # must expose that encoder family too.
                    continue
                supported = list(HARDWARE_ENCODERS[family][:3])
            usable_families.add(family)
            encoders.extend(supported)

        usable_families.add("software")
        windows_worker = windows and str(os.environ.get("TSD_WINDOWS_WORKER") or "").strip().lower() in {"1", "true", "yes", "on"}
        default_order = ("nvenc", "vce", "qsv", "software") if windows_worker else ("qsv", "nvenc", "vce", "software")
        ordered_families = []
        for family in [*requested_order, *default_order]:
            if family in usable_families and family not in ordered_families:
                ordered_families.append(family)
        preferred = str(os.environ.get("TSD_PREFERRED_ENCODER_FAMILY") or "auto").strip().lower()
        if preferred not in {*HARDWARE_ENCODERS, "auto"}:
            preferred = "auto"
        vendor_family = {"nvidia": "nvenc", "amd": "vce", "intel": "qsv"}
        for gpu in windows_gpus:
            family = vendor_family.get(str(gpu.get("vendor") or ""), "")
            gpu["encoder_family"] = family
            gpu["encoders"] = _windows_gpu_encoder_capabilities(gpu, family, encoders)
            if windows_worker and family == "nvenc":
                av1_encoders = [name for name in gpu["encoders"] if "_av1" in name]
                if av1_encoders:
                    # Verify the most demanding advertised path. A successful
                    # 10-bit probe also establishes ordinary AV1 availability.
                    probe_encoder = next(
                        (name for name in av1_encoders if name.endswith("_10bit")),
                        av1_encoders[0],
                    )
                    verified, reason = verify_windows_nvenc_encoder(gpu, probe_encoder)
                    gpu["av1_nvenc_verified"] = bool(verified)
                    gpu["av1_nvenc_verification"] = str(reason or "")
                    if not verified:
                        gpu["encoders"] = [name for name in gpu["encoders"] if "_av1" not in name]
                else:
                    gpu["av1_nvenc_verified"] = False
                    gpu["av1_nvenc_verification"] = "AV1 NVENC is not supported by this adapter model"
        if windows_worker and any(gpu.get("encoder_family") == "nvenc" for gpu in windows_gpus):
            if not any(
                "_av1" in name
                for gpu in windows_gpus
                if gpu.get("encoder_family") == "nvenc"
                for name in gpu.get("encoders") or []
            ):
                encoders = [name for name in encoders if name not in {"nvenc_av1", "nvenc_av1_10bit"}]
        qsv_av1_verified = None
        qsv_av1_verification = ""
        if not windows and "qsv" in usable_families and any(name.startswith("qsv_av1") for name in encoders):
            qsv_av1_verified, qsv_av1_verification = _linux_qsv_av1_capability(intel_render_devices)
            if not qsv_av1_verified:
                encoders = [name for name in encoders if not name.startswith("qsv_av1")]
        value = {
            "encoder_families": ordered_families,
            "encoders": list(dict.fromkeys(encoders)),
            "gpu_vendors": sorted(vendors),
            "gpus": windows_gpus,
            "render_devices": render_devices,
            "nvidia_devices": nvidia_devices,
            "preferred_encoder_family": preferred,
            "qsv_av1_verified": qsv_av1_verified,
            "qsv_av1_verification": qsv_av1_verification,
            "cpu": {
                "name": platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER") or "Unknown CPU",
                "logical_cores": os.cpu_count() or 1,
            },
            "platform": "windows" if windows else "linux",
            "handbrake_path": handbrake or "",
            "ffmpeg_path": shutil.which("ffmpeg") or "",
            "ffprobe_path": shutil.which("ffprobe") or "",
            "detected_at": now,
        }
        HARDWARE_PROFILE_CACHE.update({"expires_at": now + 60.0, "value": value})
        return deepcopy(value)


def _empty_state() -> dict:
    return {
        "schema_version": NODE_STATE_SCHEMA_VERSION,
        "local_node_id": uuid.uuid4().hex,
        "local_node_name": "HandBrake TSD Node",
        "pairing": {},
        "trusted_controllers": {},
        "nodes": {},
        "updated_at": 0,
    }


def _normalize_state(data) -> dict:
    if not isinstance(data, dict):
        data = _empty_state()

    data["schema_version"] = NODE_STATE_SCHEMA_VERSION
    data.setdefault("local_node_id", uuid.uuid4().hex)
    data.setdefault("local_node_name", "HandBrake TSD Node")
    data.setdefault("pairing", {})
    data.setdefault("trusted_controllers", {})
    data.setdefault("nodes", {})
    data.setdefault("updated_at", 0)
    return data


def _load_state_unlocked() -> dict:
    data = None
    for path in (NODE_LINK_FILE, NODE_LINK_FILE + ".bak"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                candidate = json.load(f)
            if isinstance(candidate, dict):
                data = candidate
                break
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return _normalize_state(data if isinstance(data, dict) else _empty_state())


def _write_json_atomic(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or DATA_DIR, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _save_state_unlocked(data: dict) -> None:
    data["schema_version"] = NODE_STATE_SCHEMA_VERSION
    data["updated_at"] = _now()
    _write_json_atomic(NODE_LINK_FILE, data)
    try:
        shutil.copy2(NODE_LINK_FILE, NODE_LINK_FILE + ".bak")
    except OSError:
        # The primary file is already durable. A backup failure on a NAS must
        # not turn a successful pairing or heartbeat into an HTTP 500.
        pass


def _load_state() -> dict:
    with STATE_LOCK:
        data = _load_state_unlocked()
        if not os.path.exists(NODE_LINK_FILE):
            _save_state_unlocked(data)
        return deepcopy(data)


def _save_state(data: dict) -> None:
    with STATE_LOCK:
        _save_state_unlocked(_normalize_state(data))


def _mutate_state(mutator):
    """Run one read/modify/write transaction under the process state lock."""
    with STATE_LOCK:
        data = _load_state_unlocked()
        result = mutator(data)
        _save_state_unlocked(data)
        return deepcopy(result)


def local_node_info() -> dict:
    data = _load_state()
    return {
        "id": data.get("local_node_id"),
        "name": data.get("local_node_name") or "HandBrake TSD Node",
    }


def node_discovery() -> dict:
    local = local_node_info()
    headless = str(os.environ.get("TSD_WORKER_MODE") or "").strip().lower() in {"1", "true", "yes", "on"}
    capabilities = list(NODE_CAPABILITIES)
    if headless:
        capabilities.extend(["headless-worker", "remote-transfer-only"])
    if str(os.environ.get("TSD_WINDOWS_WORKER") or "").strip().lower() in {"1", "true", "yes", "on"}:
        capabilities.extend(["windows-desktop-worker", "multi-vendor-gpu-discovery", "notification-area"])
    return {
        "service": "handbrake-tsd-node",
        "node_id": local["id"],
        "node_name": local["name"],
        "protocol_version": NODE_PROTOCOL_VERSION,
        "state_schema_version": NODE_STATE_SCHEMA_VERSION,
        "capabilities": capabilities,
        "hardware": encoder_hardware_profile(),
        "worker_mode": "headless" if headless else "full",
        "requires_remote_transfer": headless,
        "recommended_transfer_mode": "remote" if headless else "auto",
        "pairing": {
            "code_format": "XXXXX-XXXXX",
            "idempotent_retry_seconds": PAIRING_RETRY_GRACE_SECONDS,
        },
    }


def set_local_node_name(name: str) -> dict:
    value = str(name or "").strip()[:80] or "HandBrake TSD Node"
    _mutate_state(lambda data: data.update({"local_node_name": value}))
    return local_node_info()


def _hash_pairing_code(code: str) -> str:
    return hashlib.sha256(str(code or "").encode("utf-8", errors="ignore")).hexdigest()


def _hash_secret(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8", errors="ignore")).hexdigest()


def normalize_transfer_mode(value: str) -> str:
    mode = str(value or "local").strip().lower()
    if mode in {"auto", "automatic", "local_then_remote", "fallback"}:
        return "auto"
    if mode in {"remote", "remote_transfer", "transfer"}:
        return "remote"
    return "local"


def normalize_hardware_transcode_concurrency(value, default: int = 1) -> int:
    """Return the safe GPU slot count stored by the controller per worker."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default or 1)
    return max(1, min(8, parsed))


def node_has_running_work(row: dict) -> bool:
    row = row if isinstance(row, dict) else {}
    summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    try:
        if int(counts.get("running") or 0) > 0 or int(counts.get("waiting_to_upload") or 0) > 0:
            return True
    except Exception:
        pass
    jobs = row.get("jobs") if isinstance(row.get("jobs"), list) else []
    return any(str(job.get("status") or "").lower() in {"running", "waiting_to_upload"} for job in jobs if isinstance(job, dict))


def heartbeat_allowed_age(row: dict) -> int:
    return HEARTBEAT_RUNNING_GRACE_SECONDS if node_has_running_work(row) else HEARTBEAT_STALE_SECONDS


def create_pairing_code(ttl_seconds: int = PAIRING_TTL_SECONDS) -> dict:
    # Human-friendly Crockford-style code: easy to type and avoids 0/O/1/I.
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    raw = "".join(secrets.choice(alphabet) for _ in range(10))
    code = f"{raw[:5]}-{raw[5:]}"
    expires_at = _now() + max(60, min(3600, int(ttl_seconds or PAIRING_TTL_SECONDS)))

    def apply(data):
        data["pairing"] = {
            "code_hash": _hash_pairing_code(code),
            "expires_at": expires_at,
            "created_at": _now(),
            "used_at": 0,
            "used_by_controller_id": "",
            "attempt_count": 0,
        }

    _mutate_state(apply)
    return {"code": code, "expires_at": expires_at}


def accept_pairing(code: str, controller: dict) -> dict:
    normalized_code = str(code or "").strip().upper()
    controller_id = str(controller.get("controller_id") or controller.get("id") or "").strip()
    if not controller_id:
        raise ValueError("controller identity is required")
    controller_name = str(controller.get("controller_name") or controller.get("name") or "Controller").strip()[:80]
    controller_url = str(controller.get("controller_url") or controller.get("url") or "").strip().rstrip("/")[:300]
    advertised_controller_url = str(
        controller.get("advertised_controller_url") or controller_url
    ).strip().rstrip("/")[:300]
    observed_controller_url = str(
        controller.get("observed_controller_url") or ""
    ).strip().rstrip("/")[:300]
    allowed_ips = controller.get("allowed_ips")
    if not isinstance(allowed_ips, list):
        allowed_ips = []

    controller_protocol = max(1, _safe_int(controller.get("protocol_version"), 1))

    def apply(data):
        pairing = data.get("pairing") if isinstance(data.get("pairing"), dict) else {}
        now = _now()
        expected = pairing.get("code_hash") or ""
        if not expected or not hmac.compare_digest(expected, _hash_pairing_code(normalized_code)):
            raise ValueError("invalid pairing code")
        if float(pairing.get("expires_at") or 0) < now:
            raise ValueError("pairing code expired")

        used_at = float(pairing.get("used_at") or 0)
        used_by = str(pairing.get("used_by_controller_id") or "")
        retrying_same_controller = bool(
            used_at
            and used_by == controller_id
            and now - used_at <= PAIRING_RETRY_GRACE_SECONDS
        )
        if used_at and not retrying_same_controller:
            raise ValueError("pairing code already used")

        rotate_requested = bool(controller.get("rotate_worker_identity"))
        expected_worker_id = str(controller.get("expected_worker_id") or "").strip()
        identity_rotated = False
        identity_rotated_from = ""
        if rotate_requested:
            current_worker_id = str(data.get("local_node_id") or "").strip()
            retrying_completed_rotation = bool(
                retrying_same_controller
                and expected_worker_id
                and str(pairing.get("identity_rotated_from") or "") == expected_worker_id
                and str(pairing.get("identity_rotated_to") or "") == current_worker_id
            )
            if retrying_completed_rotation:
                identity_rotated = True
                identity_rotated_from = expected_worker_id
            elif used_at:
                raise ValueError("worker identity rotation cannot be changed after pairing")
            else:
                if expected_worker_id and current_worker_id != expected_worker_id:
                    raise ValueError("worker identity changed before pairing; refresh and try again")
                identity_rotated_from = current_worker_id
                data["local_node_id"] = uuid.uuid4().hex
                # A duplicated /work/state also duplicates trusted controller
                # secrets. The valid one-time pairing code authorizes giving
                # this physical worker its own clean identity and trust set.
                data["trusted_controllers"] = {}
                pairing["identity_rotated_from"] = identity_rotated_from
                pairing["identity_rotated_to"] = data["local_node_id"]
                pairing["identity_rotated_at"] = now
                identity_rotated = True

        token = secrets.token_urlsafe(32)
        recovery_token = secrets.token_urlsafe(40)
        trusted = data.setdefault("trusted_controllers", {})
        existing = trusted.get(controller_id) if isinstance(trusted.get(controller_id), dict) else {}
        paired_at = float(existing.get("paired_at") or now)
        trusted[controller_id] = {
            **existing,
            "id": controller_id,
            "name": controller_name,
            "token": token,
            "recovery_token_hash": _hash_secret(recovery_token),
            "url": controller_url,
            "advertised_url": advertised_controller_url,
            "observed_url": observed_controller_url,
            "paired_at": paired_at,
            "last_seen": 0,
            "allowed_ips": [str(ip).strip() for ip in allowed_ips if str(ip).strip()][:20],
            "protocol_version": min(NODE_PROTOCOL_VERSION, controller_protocol),
            "last_pair_request_id": str(controller.get("request_id") or "")[:80],
        }
        pairing.update({
            "used_at": used_at or now,
            "used_by_controller_id": controller_id,
            "last_retry_at": now if retrying_same_controller else 0,
            "attempt_count": int(pairing.get("attempt_count") or 0) + 1,
        })
        data["pairing"] = pairing
        return {
            "worker_id": data["local_node_id"],
            "worker_name": data.get("local_node_name") or "HandBrake TSD Node",
            "token": token,
            "recovery_token": recovery_token,
            "controller_url": controller_url,
            "advertised_controller_url": advertised_controller_url,
            "observed_controller_url": observed_controller_url,
            "paired_at": paired_at,
            "protocol_version": NODE_PROTOCOL_VERSION,
            "capabilities": NODE_CAPABILITIES,
            "retry_recovered": retrying_same_controller,
            "identity_rotated": identity_rotated,
            "identity_rotated_from": identity_rotated_from,
        }

    return _mutate_state(apply)


def recover_pairing(controller: dict) -> dict:
    """Repair a paired controller session without a new pairing code."""
    controller_id = str(controller.get("controller_id") or controller.get("id") or "").strip()
    if not controller_id:
        raise ValueError("controller identity is required")

    def apply(data):
        trusted = data.setdefault("trusted_controllers", {})
        row = trusted.get(controller_id)
        if not isinstance(row, dict):
            raise ValueError("controller is not paired")

        supplied_recovery = str(controller.get("recovery_token") or "").strip()
        supplied_session = str(controller.get("session_token") or "").strip()
        expected_recovery = str(row.get("recovery_token_hash") or "")
        recovery_ok = bool(expected_recovery and supplied_recovery and hmac.compare_digest(expected_recovery, _hash_secret(supplied_recovery)))
        session_ok = bool(supplied_session and hmac.compare_digest(str(row.get("token") or ""), supplied_session))
        if not recovery_ok and not session_ok:
            raise ValueError("pair recovery credential rejected")

        new_token = secrets.token_urlsafe(32)
        new_recovery = supplied_recovery if recovery_ok else secrets.token_urlsafe(40)
        now = _now()
        controller_url = str(controller.get("controller_url") or row.get("url") or "").strip().rstrip("/")[:300]
        advertised_controller_url = str(
            controller.get("advertised_controller_url")
            or row.get("advertised_url")
            or controller_url
        ).strip().rstrip("/")[:300]
        observed_controller_url = str(
            controller.get("observed_controller_url")
            or row.get("observed_url")
            or ""
        ).strip().rstrip("/")[:300]
        row.update({
            "token": new_token,
            "recovery_token_hash": _hash_secret(new_recovery),
            "url": controller_url,
            "advertised_url": advertised_controller_url,
            "observed_url": observed_controller_url,
            "last_seen": now,
            "recovered_at": now,
            "protocol_version": min(NODE_PROTOCOL_VERSION, max(1, _safe_int(controller.get("protocol_version"), row.get("protocol_version") or 1))),
        })
        trusted[controller_id] = row
        return {
            "worker_id": data.get("local_node_id"),
            "worker_name": data.get("local_node_name") or "HandBrake TSD Node",
            "token": new_token,
            "recovery_token": new_recovery,
            "controller_url": controller_url,
            "advertised_controller_url": advertised_controller_url,
            "observed_controller_url": observed_controller_url,
            "recovered_at": now,
            "protocol_version": NODE_PROTOCOL_VERSION,
            "capabilities": list(NODE_CAPABILITIES),
        }

    return _mutate_state(apply)


def enable_pair_recovery(controller_id: str) -> str:
    controller_id = str(controller_id or "")

    def apply(data):
        trusted = data.setdefault("trusted_controllers", {})
        row = trusted.get(controller_id)
        if not isinstance(row, dict):
            raise ValueError("controller is not paired")
        recovery_token = secrets.token_urlsafe(40)
        row["recovery_token_hash"] = _hash_secret(recovery_token)
        row["recovery_enabled_at"] = _now()
        trusted[controller_id] = row
        return recovery_token

    return _mutate_state(apply)


def public_node(row: dict) -> dict:
    row = row if isinstance(row, dict) else {}
    last_heartbeat = float(row.get("last_heartbeat") or 0)
    now = _now()
    age = max(0.0, now - last_heartbeat) if last_heartbeat else None
    allowed_age = heartbeat_allowed_age(row)
    online = bool(last_heartbeat) and bool(row.get("online")) and (age is not None and age <= allowed_age)
    status = str(row.get("status") or ("online" if online else "offline")).strip().lower()
    if online and status == "error" and not row.get("last_error"):
        status = "idle"
    heartbeat_misses = int(row.get("heartbeat_misses") or 0)
    running_work = node_has_running_work(row)
    if online and heartbeat_misses > 0:
        status = "reconnecting" if running_work else "stale"
    elif online and age is not None and age > HEARTBEAT_WARN_SECONDS:
        status = "reconnecting" if running_work else "stale"
    if not online and not last_heartbeat:
        status = "paired"
    elif not online:
        status = "offline"
    return {
        "id": row.get("id"),
        "name": row.get("name") or "Worker",
        "worker_reported_name": row.get("worker_reported_name") or "",
        "name_source": row.get("name_source") or "controller",
        "url": row.get("url") or "",
        "role": row.get("role") or "worker",
        "online": online,
        "status": status,
        "connection_state": status,
        "heartbeat_misses": heartbeat_misses,
        "last_heartbeat_age_seconds": round(age, 1) if age is not None else None,
        "heartbeat_grace_seconds": allowed_age,
        "running_work": running_work,
        "summary": row.get("summary") if isinstance(row.get("summary"), dict) else {},
        "last_heartbeat": last_heartbeat,
        "last_error": row.get("last_error") or "",
        "last_job_error": row.get("last_job_error") or "",
        "path_mappings": row.get("path_mappings") if isinstance(row.get("path_mappings"), list) else [],
        "transfer_mode": normalize_transfer_mode(row.get("transfer_mode")),
        "controller_url": row.get("controller_url") or "",
        "remote_temp_dir": row.get("remote_temp_dir") or "",
        "hardware_transcode_concurrency": normalize_hardware_transcode_concurrency(
            row.get("hardware_transcode_concurrency"),
            1,
        ),
        "worker_encoding_policy": (
            row.get("worker_encoding_policy")
            if isinstance(row.get("worker_encoding_policy"), dict)
            else {}
        ),
        "worker_release": row.get("worker_release") or "",
        "worker_mode": row.get("worker_mode") or "full",
        "requires_remote_transfer": bool(row.get("requires_remote_transfer")),
        "paired_at": row.get("paired_at") or 0,
        "paired_controllers": row.get("paired_controllers") if isinstance(row.get("paired_controllers"), list) else [],
        "jobs": row.get("jobs") if isinstance(row.get("jobs"), list) else [],
        "prediction_profile": row.get("prediction_profile") if isinstance(row.get("prediction_profile"), dict) else {},
        "protocol_version": max(1, _safe_int(row.get("protocol_version"), 1)),
        "capabilities": row.get("capabilities") if isinstance(row.get("capabilities"), list) else [],
        "hardware": row.get("hardware") if isinstance(row.get("hardware"), dict) else {},
        "pairing_notice": row.get("pairing_notice") or "",
        "identity_rotated": bool(row.get("identity_rotated")),
        "address_conflict_with": row.get("address_conflict_with") or "",
        "last_success_at": row.get("last_success_at") or 0,
        "consecutive_failures": _safe_int(row.get("consecutive_failures"), heartbeat_misses),
    }


def public_trusted_controller(row: dict) -> dict:
    row = row if isinstance(row, dict) else {}
    last_seen = float(row.get("last_seen") or 0)
    online = bool(last_seen and (_now() - last_seen <= HEARTBEAT_STALE_SECONDS))
    return {
        "id": row.get("id") or "",
        "name": row.get("name") or "Controller",
        "url": row.get("url") or "",
        "advertised_url": row.get("advertised_url") or "",
        "observed_url": row.get("observed_url") or "",
        "role": "controller",
        "online": online,
        "status": "online" if online else "paired",
        "paired_at": row.get("paired_at") or 0,
        "last_seen": last_seen,
        "allowed_ips": row.get("allowed_ips") if isinstance(row.get("allowed_ips"), list) else [],
        "protocol_version": max(1, _safe_int(row.get("protocol_version"), 1)),
    }


def list_trusted_controllers_public() -> list[dict]:
    data = _load_state()
    controllers = data.get("trusted_controllers") if isinstance(data.get("trusted_controllers"), dict) else {}
    return [public_trusted_controller(row) for row in controllers.values() if isinstance(row, dict)]


def local_node_overview() -> dict:
    data = _load_state()
    workers = [public_node(row) for row in (data.get("nodes") or {}).values() if isinstance(row, dict)]
    controllers = list_trusted_controllers_public()
    is_controller = bool(workers)
    is_worker = bool(controllers)
    if is_controller and is_worker:
        role = "controller_worker"
        role_label = "Master / Controller + Worker"
    elif is_controller:
        role = "controller"
        role_label = "Master / Controller"
    elif is_worker:
        role = "worker"
        role_label = "Worker"
    else:
        role = "standalone"
        role_label = "Standalone"
    return {
        "id": data.get("local_node_id"),
        "name": data.get("local_node_name") or "HandBrake TSD Node",
        "role": role,
        "role_label": role_label,
        "paired_worker_count": len(workers),
        "paired_controller_count": len(controllers),
        "paired_workers": workers,
        "paired_controllers": controllers,
        "hardware": encoder_hardware_profile(),
    }


def list_nodes_public() -> list[dict]:
    data = _load_state()
    return [public_node(row) for row in (data.get("nodes") or {}).values()]


def list_nodes_private() -> list[dict]:
    data = _load_state()
    return [row for row in (data.get("nodes") or {}).values() if isinstance(row, dict)]


def get_node_private(node_id: str) -> dict | None:
    data = _load_state()
    row = (data.get("nodes") or {}).get(str(node_id or ""))
    return row if isinstance(row, dict) else None


def save_node(row: dict) -> dict:
    node_id = str(row.get("id") or "").strip()
    if not node_id:
        raise ValueError("missing node id")

    def apply(data):
        nodes = data.setdefault("nodes", {})
        existing = nodes.get(node_id) if isinstance(nodes.get(node_id), dict) else {}
        merged = {**existing, **row, "id": node_id}
        nodes[node_id] = merged
        return merged

    return _mutate_state(apply)


def rename_node(node_id: str, name: str) -> dict:
    """Set the controller-side alias for a linked worker."""
    node_id = str(node_id or "").strip()
    value = re.sub(r"\s+", " ", str(name or "")).strip()
    if not value:
        raise ValueError("worker name is required")
    if len(value) > 80:
        raise ValueError("worker name must be 80 characters or fewer")

    def apply(data):
        nodes = data.setdefault("nodes", {})
        row = nodes.get(node_id)
        if not isinstance(row, dict):
            raise LookupError("node not found")
        duplicate = next(
            (
                candidate
                for candidate_id, candidate in nodes.items()
                if str(candidate_id) != node_id
                and isinstance(candidate, dict)
                and str(candidate.get("name") or "").strip().casefold() == value.casefold()
            ),
            None,
        )
        if duplicate:
            raise ValueError("another linked worker already uses that name")
        row["name"] = value
        row["name_source"] = "controller"
        row["renamed_at"] = _now()
        nodes[node_id] = row
        return row

    return _mutate_state(apply)


def delete_node(node_id: str) -> bool:
    node_id = str(node_id or "")

    def apply(data):
        nodes = data.setdefault("nodes", {})
        existed = node_id in nodes
        nodes.pop(node_id, None)
        return existed

    return _mutate_state(apply)


def _node_url_key(url: str) -> tuple[str, str, int | None, str]:
    value = str(url or "").strip().rstrip("/")
    try:
        parsed = urlparse(value)
        default_port = 443 if parsed.scheme.lower() == "https" else 80
        return (
            parsed.scheme.lower(),
            (parsed.hostname or "").lower(),
            parsed.port or default_port,
            (parsed.path or "").rstrip("/"),
        )
    except Exception:
        return ("", value.casefold(), None, "")


def _unique_worker_name(preferred: str, worker_id: str, worker_url: str) -> str:
    base = str(preferred or "Worker").strip()[:80] or "Worker"
    used = {
        str(row.get("name") or "").strip().casefold()
        for row in list_nodes_private()
        if str(row.get("id") or "") != str(worker_id or "")
    }
    if base.casefold() not in used:
        return base
    try:
        parsed = urlparse(str(worker_url or ""))
        host = parsed.hostname or "node"
        default_port = 443 if parsed.scheme == "https" else 80
        label = f"{host}:{parsed.port}" if parsed.port and parsed.port != default_port else host
    except Exception:
        label = "node"
    candidate = f"{base} ({label})"[:80]
    if candidate.casefold() not in used:
        return candidate
    index = 2
    while True:
        suffix = f" ({index})"
        candidate = f"{base[:max(1, 80 - len(suffix))]}{suffix}"
        if candidate.casefold() not in used:
            return candidate
        index += 1


def trusted_controller(controller_id: str) -> dict | None:
    data = _load_state()
    row = (data.get("trusted_controllers") or {}).get(str(controller_id or ""))
    return row if isinstance(row, dict) else None


def trusted_controller_by_url(url: str) -> dict | None:
    wanted = str(url or "").strip().rstrip("/")
    if not wanted:
        return None
    data = _load_state()
    controllers = data.get("trusted_controllers") if isinstance(data.get("trusted_controllers"), dict) else {}
    for row in controllers.values():
        if isinstance(row, dict) and str(row.get("url") or "").strip().rstrip("/") == wanted:
            return row
    return None


def update_trusted_controller(controller_id: str, updates: dict) -> None:
    controller_id = str(controller_id or "")

    def apply(data):
        trusted = data.setdefault("trusted_controllers", {})
        row = trusted.get(controller_id)
        if not isinstance(row, dict):
            return
        row.update(updates)
        trusted[controller_id] = row

    _mutate_state(apply)


def delete_trusted_controller(controller_id: str) -> None:
    _mutate_state(lambda data: data.setdefault("trusted_controllers", {}).pop(str(controller_id or ""), None))


def normalize_url(url: str) -> str:
    value = str(url or "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("worker URL must start with http:// or https://")
    return value


def _request_json(
    url: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    headers: dict | None = None,
    timeout: int = 8,
    retries: int = 0,
) -> dict:
    body_bytes = b""
    if body is not None:
        body_bytes = json.dumps(body).encode("utf-8")
    req = Request(
        url,
        data=body_bytes if body is not None else None,
        method=method,
        headers={
            "accept": "application/json",
            **({"Content-Type": "application/json"} if body is not None else {}),
            **(headers or {}),
        },
    )
    payload = {}
    for attempt in range(max(0, int(retries)) + 1):
        try:
            with urlopen(req, timeout=timeout) as res:
                payload = json.loads(res.read().decode("utf-8", errors="replace"))
            break
        except HTTPError as e:
            try:
                error_payload = json.loads(e.read().decode("utf-8", errors="replace"))
            except Exception:
                error_payload = {"error": str(e)}
            raise RuntimeError(error_payload.get("error") or str(e))
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            if attempt >= max(0, int(retries)):
                reason = getattr(e, "reason", e)
                raise RuntimeError(f"node request failed: {reason}")
            time.sleep(0.25 * (attempt + 1))
    return payload if isinstance(payload, dict) else {}


def pair_worker(
    worker_url: str,
    code: str,
    *,
    name: str = "",
    path_mappings: list | None = None,
    transfer_mode: str = "local",
    controller_url: str = "",
    remote_temp_dir: str = "",
    hardware_transcode_concurrency: int = 1,
) -> dict:
    url = normalize_url(worker_url)
    local = local_node_info()
    try:
        discovery = _request_json(f"{url}/api/node/discovery", timeout=5, retries=1)
    except RuntimeError as exc:
        message = str(exc)
        if "404" in message or "not found" in message.lower():
            # Protocol v1 workers predate discovery and remain pairable.
            discovery = {"protocol_version": 1, "capabilities": []}
        else:
            raise RuntimeError(
                f"Cannot reach the worker at {url}. On the Windows Worker, "
                "click Allow controller access and approve the Windows Firewall prompt, "
                f"then retry. Details: {message}"
            ) from exc
    request_id = uuid.uuid4().hex
    discovered_worker_id = str(discovery.get("node_id") or discovery.get("worker_id") or "").strip()
    existing_discovered = get_node_private(discovered_worker_id) if discovered_worker_id else None
    identity_collision = bool(
        existing_discovered
        and _node_url_key(existing_discovered.get("url") or "") != _node_url_key(url)
    )
    raw_code = str(code or "").strip()
    negotiated_protocol = max(1, _safe_int(discovery.get("protocol_version"), 1))
    payload = {
        # Legacy v1 codes were case-sensitive URL-safe tokens. v2 uses the
        # upper-case human format shown by discovery.
        "code": raw_code.upper() if negotiated_protocol >= 2 else raw_code,
        "controller_id": local["id"],
        "controller_name": local["name"],
        "controller_url": str(controller_url or "").strip().rstrip("/"),
        "protocol_version": NODE_PROTOCOL_VERSION,
        "capabilities": list(NODE_CAPABILITIES),
        "request_id": request_id,
        "rotate_worker_identity": identity_collision,
        "expected_worker_id": discovered_worker_id if identity_collision else "",
    }
    # v2 pairing is idempotent for this controller during a short recovery
    # window, so a lost response can safely be retried.
    data = _request_json(f"{url}/api/node/pair/accept", method="POST", body=payload, timeout=10, retries=1)
    token = str(data.get("token") or "")
    recovery_token = str(data.get("recovery_token") or "")
    worker_id = str(data.get("worker_id") or data.get("id") or "").strip()
    if not token or not worker_id:
        raise RuntimeError("pairing response missing worker token")
    if identity_collision and worker_id == discovered_worker_id:
        raise RuntimeError(
            "worker identity duplicates an existing node; update the worker image and pair again "
            "with a separate /work directory"
        )
    response_collision = get_node_private(worker_id)
    if (
        response_collision
        and _node_url_key(response_collision.get("url") or "") != _node_url_key(url)
        and not data.get("identity_rotated")
    ):
        raise RuntimeError(
            "worker identity duplicates an existing linked node; the original record was retained. "
            "Update the second worker, generate a new pairing code, and pair it again."
        )

    stored_controller_url = str(data.get("controller_url") or controller_url or "").strip().rstrip("/")
    requires_remote_transfer = bool(data.get("requires_remote_transfer") or discovery.get("requires_remote_transfer"))
    selected_transfer_mode = "remote" if requires_remote_transfer else normalize_transfer_mode(transfer_mode)
    worker_mode = str(data.get("worker_mode") or discovery.get("worker_mode") or "full").strip().lower()
    existing_same_id = get_node_private(worker_id)
    response_name = str(data.get("worker_name") or discovery.get("node_name") or "Worker").strip()
    preferred_name = str(name or "").strip() or (
        str(existing_same_id.get("name") or "").strip()
        if existing_same_id and _node_url_key(existing_same_id.get("url") or "") == _node_url_key(url)
        else response_name
    )
    display_name = _unique_worker_name(preferred_name, worker_id, url)
    notices = []
    if data.get("identity_rotated"):
        notices.append("A duplicate worker identity was detected and safely replaced with a new identity for this node.")
    if display_name != preferred_name:
        notices.append(f"The worker was named {display_name} so linked nodes remain distinguishable.")
    address_conflicts = [
        row
        for row in list_nodes_private()
        if str(row.get("id") or "") != worker_id
        and _node_url_key(row.get("url") or "") == _node_url_key(url)
    ]
    if address_conflicts:
        notices.append("An older worker record uses the same address and was retained instead of being deleted.")
    row = {
        "id": worker_id,
        "name": display_name,
        "worker_reported_name": response_name,
        "name_source": "controller",
        "url": url,
        "role": "worker",
        "token": token,
        "recovery_token": recovery_token,
        "paired_at": float((existing_same_id or {}).get("paired_at") or _now()),
        "last_heartbeat": 0,
        "heartbeat_misses": 0,
        "last_failed_at": 0,
        "online": False,
        "status": "paired",
        "summary": {},
        "last_error": "",
        "path_mappings": [] if requires_remote_transfer else normalize_path_mappings(path_mappings or []),
        "transfer_mode": selected_transfer_mode,
        "controller_url": stored_controller_url,
        "remote_temp_dir": str(remote_temp_dir or "").strip()[:500],
        "hardware_transcode_concurrency": normalize_hardware_transcode_concurrency(
            hardware_transcode_concurrency,
            1,
        ),
        "worker_mode": worker_mode,
        "requires_remote_transfer": requires_remote_transfer,
        "protocol_version": max(1, _safe_int(data.get("protocol_version"), discovery.get("protocol_version") or 1)),
        "capabilities": data.get("capabilities") if isinstance(data.get("capabilities"), list) else (
            discovery.get("capabilities") if isinstance(discovery.get("capabilities"), list) else []
        ),
        "hardware": data.get("hardware") if isinstance(data.get("hardware"), dict) else (
            discovery.get("hardware") if isinstance(discovery.get("hardware"), dict) else {}
        ),
        "pair_request_id": request_id,
        "pair_retry_recovered": bool(data.get("retry_recovered")),
        "identity_rotated": bool(data.get("identity_rotated")),
        "identity_rotated_from": str(data.get("identity_rotated_from") or ""),
        "pairing_notice": " ".join(notices),
        "address_conflict_with": str(address_conflicts[0].get("id") or "") if address_conflicts else "",
    }
    save_node(row)
    for conflict in address_conflicts:
        save_node({
            **conflict,
            "address_conflict_with": worker_id,
            "pairing_notice": (
                f"Another worker ({display_name}) was paired at this address. "
                "This record was retained; update its URL or forget it when safe."
            ),
        })
    return public_node(row)


def recover_worker_session(row: dict, *, controller_url: str = "") -> dict:
    worker_url = normalize_url(row.get("url") or "")
    local = local_node_info()
    payload = {
        "controller_id": local["id"],
        "controller_name": local["name"],
        "controller_url": str(controller_url or row.get("controller_url") or "").strip().rstrip("/"),
        "recovery_token": str(row.get("recovery_token") or ""),
        "session_token": str(row.get("token") or ""),
        "protocol_version": NODE_PROTOCOL_VERSION,
    }
    data = _request_json(f"{worker_url}/api/node/pair/recover", method="POST", body=payload, timeout=12)
    token = str(data.get("token") or "").strip()
    recovery_token = str(data.get("recovery_token") or row.get("recovery_token") or "").strip()
    if not token or not recovery_token:
        raise RuntimeError("pair recovery response was incomplete")
    updated = {
        **row,
        "token": token,
        "recovery_token": recovery_token,
        "controller_url": str(data.get("controller_url") or payload["controller_url"] or "").strip().rstrip("/"),
        "last_error": "",
        "heartbeat_misses": 0,
        "recovered_at": _now(),
        "status": "reconnecting",
        "protocol_version": max(1, _safe_int(data.get("protocol_version"), row.get("protocol_version") or 1)),
        "capabilities": data.get("capabilities") if isinstance(data.get("capabilities"), list) else row.get("capabilities", []),
        "hardware": data.get("hardware") if isinstance(data.get("hardware"), dict) else row.get("hardware", {}),
    }
    return save_node(updated)


def normalize_path_mappings(rows) -> list[dict]:
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        controller = str(row.get("controller") or row.get("controller_prefix") or "").strip()
        worker = str(row.get("worker") or row.get("worker_prefix") or "").strip()
        if not controller or not worker:
            continue
        out.append({
            "controller": controller.rstrip("/\\"),
            "worker": worker.rstrip("/\\"),
        })
        if len(out) >= 50:
            break
    return out


def translate_path(path: str, mappings: list[dict]) -> str | None:
    value = str(path or "").strip()
    if not value:
        return None
    best = None
    for row in normalize_path_mappings(mappings):
        controller = row["controller"]
        key = controller.lower()
        value_l = value.lower()
        if value_l == key or value_l.startswith(key + "/") or value_l.startswith(key + "\\"):
            if best is None or len(controller) > len(best["controller"]):
                best = row
    if not best:
        return None
    rel = value[len(best["controller"]):].lstrip("/\\")
    sep = "\\" if "\\" in best["worker"] else "/"
    return best["worker"] + (sep + rel.replace("\\", sep).replace("/", sep) if rel else "")


def _empty_transfers() -> dict:
    return {"transfers": {}, "updated_at": 0}


def _load_transfers() -> dict:
    with TRANSFER_LOCK:
        try:
            with open(NODE_TRANSFER_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = _empty_transfers()
            _save_transfers(data)
            return data
        except Exception:
            try:
                with open(NODE_TRANSFER_FILE + ".bak", "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = _empty_transfers()
                _save_transfers(data)
                return data
    if not isinstance(data, dict):
        data = _empty_transfers()
    data.setdefault("transfers", {})
    data.setdefault("updated_at", 0)
    return data


def _save_transfers(data: dict) -> None:
    with TRANSFER_LOCK:
        data["updated_at"] = _now()
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = NODE_TRANSFER_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, NODE_TRANSFER_FILE)
        shutil.copy2(NODE_TRANSFER_FILE, NODE_TRANSFER_FILE + ".bak")


def create_transfer_grant(src: str, worker_node_id: str, *, source_size: int = 0, ttl_seconds: int = TRANSFER_TTL_SECONDS) -> dict:
    data = _load_transfers()
    transfers = data.setdefault("transfers", {})
    now = _now()
    transfer_id = uuid.uuid4().hex
    download_token = secrets.token_urlsafe(32)
    upload_token = secrets.token_urlsafe(32)
    expires_at = now + max(300, min(72 * 60 * 60, int(ttl_seconds or TRANSFER_TTL_SECONDS)))
    row = {
        "id": transfer_id,
        "src": str(src or ""),
        "source_basename": os.path.basename(str(src or "")) or "source.mkv",
        "source_size": int(source_size or 0),
        "worker_node_id": str(worker_node_id or ""),
        "download_token_hash": _hash_secret(download_token),
        "upload_token_hash": _hash_secret(upload_token),
        "created_at": now,
        "expires_at": expires_at,
        "download_used_at": 0,
        "upload_used_at": 0,
        "completed_at": 0,
        "status": "created",
        "error": "",
        "out_path": "",
        "out_bytes": 0,
        "saved_bytes": 0,
        "source_deleted": False,
    }
    transfers[transfer_id] = row
    _save_transfers(data)
    return {**row, "download_token": download_token, "upload_token": upload_token}


def get_transfer(transfer_id: str) -> dict | None:
    data = _load_transfers()
    row = (data.get("transfers") or {}).get(str(transfer_id or ""))
    return row if isinstance(row, dict) else None


def find_active_transfer_for_src(src: str, *, retention_seconds: int = 7 * 24 * 60 * 60) -> dict | None:
    """Return a durable in-flight remote transfer reservation for ``src``.

    The controller queue placeholder is removed after a worker accepts a job,
    while the transfer ledger remains until the worker returns the result.  A
    library scan during that window must still see the source as busy or it can
    dispatch the same episode twice and let the first result delete the source
    out from under the second upload.
    """
    value = str(src or "").strip()
    if not value:
        return None
    try:
        target = os.path.normcase(os.path.realpath(value))
    except Exception:
        target = os.path.normcase(os.path.abspath(value))
    now = _now()
    data = _load_transfers()
    rows = (data.get("transfers") or {}).values()
    candidates = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").strip().lower()
        if row.get("completed_at") or status in {"complete", "completed", "canceled", "cancelled", "expired"}:
            continue
        row_src = str(row.get("src") or "").strip()
        if not row_src:
            continue
        try:
            row_target = os.path.normcase(os.path.realpath(row_src))
        except Exception:
            row_target = os.path.normcase(os.path.abspath(row_src))
        if row_target != target:
            continue
        last_activity = max(
            float(row.get("created_at") or 0),
            float(row.get("renewed_at") or 0),
            float(row.get("download_used_at") or 0),
            float(row.get("upload_used_at") or 0),
        )
        if last_activity and now - last_activity > max(TRANSFER_TTL_SECONDS, int(retention_seconds)):
            continue
        candidates.append(row)
    if not candidates:
        return None
    return deepcopy(max(candidates, key=lambda item: float(item.get("created_at") or 0)))


def save_transfer(row: dict) -> dict:
    with TRANSFER_LOCK:
        data = _load_transfers()
        transfer_id = str(row.get("id") or "").strip()
        if not transfer_id:
            raise ValueError("missing transfer id")
        transfers = data.setdefault("transfers", {})
        existing = transfers.get(transfer_id) if isinstance(transfers.get(transfer_id), dict) else {}
        merged = {**existing, **row, "id": transfer_id}
        transfers[transfer_id] = merged
        _save_transfers(data)
        return merged


def cancel_transfer_grant(transfer_id: str, worker_node_id: str = "") -> dict:
    """Invalidate both transfer tokens so a canceled job cannot resume I/O."""
    with TRANSFER_LOCK:
        data = _load_transfers()
        transfers = data.setdefault("transfers", {})
        row = transfers.get(str(transfer_id or ""))
        if not isinstance(row, dict):
            raise ValueError("transfer not found")
        expected_worker = str(row.get("worker_node_id") or "")
        if worker_node_id and expected_worker != str(worker_node_id):
            raise ValueError("transfer belongs to a different worker")
        if row.get("completed_at") or str(row.get("status") or "").lower() == "complete":
            return deepcopy(row)
        now = _now()
        row.update({
            "status": "canceled",
            "canceled_at": now,
            "expires_at": now,
            "download_token_hash": "",
            "upload_token_hash": "",
            "error": "",
        })
        transfers[str(transfer_id)] = row
        _save_transfers(data)
        return deepcopy(row)


def renew_transfer_upload_grant(transfer_id: str, worker_node_id: str, *, ttl_seconds: int = TRANSFER_TTL_SECONDS) -> dict:
    with TRANSFER_LOCK:
        data = _load_transfers()
        transfers = data.setdefault("transfers", {})
        row = transfers.get(str(transfer_id or ""))
        if not isinstance(row, dict):
            raise ValueError("transfer not found")
        if str(row.get("worker_node_id") or "") != str(worker_node_id or ""):
            raise ValueError("transfer belongs to a different worker")
        if str(row.get("status") or "").strip().lower() in {"canceled", "cancelled"}:
            raise ValueError("transfer was canceled")
        if row.get("completed_at") or row.get("status") == "complete":
            return {
                "id": row.get("id"),
                "complete": True,
                "out_path": row.get("out_path") or "",
                "out_bytes": int(row.get("out_bytes") or 0),
                "saved_bytes": int(row.get("saved_bytes") or 0),
                "source_deleted": bool(row.get("source_deleted")),
            }

        token = secrets.token_urlsafe(32)
        now = _now()
        row.update({
            "upload_token_hash": _hash_secret(token),
            "upload_used_at": 0,
            "expires_at": now + max(300, min(72 * 60 * 60, int(ttl_seconds or TRANSFER_TTL_SECONDS))),
            "status": "waiting_for_upload",
            "error": "",
            "renewed_at": now,
        })
        transfers[str(transfer_id)] = row
        _save_transfers(data)
        return {**row, "upload_token": token, "complete": False}


def transfer_token_matches(row: dict, kind: str, token: str, *, require_unused: bool = True) -> bool:
    row = row if isinstance(row, dict) else {}
    kind = str(kind or "").strip().lower()
    if kind not in {"download", "upload"}:
        return False
    if str(row.get("status") or "").strip().lower() in {"canceled", "cancelled"}:
        return False
    if float(row.get("expires_at") or 0) < _now():
        return False
    if require_unused and row.get(f"{kind}_used_at"):
        return False
    expected = str(row.get(f"{kind}_token_hash") or "")
    if not expected:
        return False
    return hmac.compare_digest(expected, _hash_secret(token))


def hmac_headers(method: str, path: str, body_bytes: bytes, *, node_id: str, token: str, timestamp: str | None = None) -> dict:
    ts = str(timestamp or int(_now()))
    message = b"\n".join([
        method.upper().encode("utf-8"),
        path.encode("utf-8"),
        ts.encode("utf-8"),
        body_bytes or b"",
    ])
    sig = hmac.new(str(token).encode("utf-8"), message, hashlib.sha256).hexdigest()
    return {
        "X-Node-Id": str(node_id),
        "X-Node-Timestamp": ts,
        "X-Node-Signature": sig,
    }


def verify_hmac(method: str, path: str, body_bytes: bytes, *, node_id: str, token: str, timestamp: str, signature: str, max_skew: int = 15 * 60) -> bool:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(_now()) - ts) > max_skew:
        return False
    expected = hmac_headers(
        method,
        path,
        body_bytes,
        node_id=node_id,
        token=token,
        timestamp=str(ts),
    ).get("X-Node-Signature") or ""
    return hmac.compare_digest(expected, str(signature or ""))


def signed_json_request(node: dict, api_path: str, *, method: str = "GET", body: dict | None = None, timeout: int = 8) -> dict:
    # Worker-side trusted controller rows retain both the address advertised
    # by the browser and the source route observed on authenticated requests.
    # The observed route is the one known to be reachable from this worker.
    url = str(node.get("observed_url") or node.get("url") or "").rstrip("/") + api_path
    body_bytes = json.dumps(body).encode("utf-8") if body is not None else b""
    headers = hmac_headers(
        method,
        api_path,
        body_bytes,
        node_id=local_node_info()["id"],
        token=str(node.get("token") or ""),
    )
    return _request_json(url, method=method, body=body, headers=headers, timeout=timeout)
