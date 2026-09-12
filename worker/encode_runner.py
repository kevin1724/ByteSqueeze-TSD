"""Cross-platform HandBrake launcher used by the packaged Windows worker.

The Docker worker keeps using ``encode-one.sh``.  This module intentionally
accepts the same environment contract so the dispatcher and controller do not
need a Windows-specific job format.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


TRUE_VALUES = {"1", "true", "yes", "on"}


def _emit(message: str = "") -> None:
    """Write a UTF-8-safe line even from a windowed PyInstaller executable."""
    line = f"{message}\n"
    stream = getattr(sys, "stdout", None)
    if stream is not None:
        try:
            stream.write(line)
            stream.flush()
            return
        except Exception:
            pass
    # A --windowed child still inherits the pipe handle created by jobs.py,
    # but Python may expose sys.stdout as None.  Write to that Win32 handle.
    if os.name == "nt":
        try:
            import ctypes

            data = line.encode("utf-8", errors="replace")
            handle = ctypes.windll.kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            written = ctypes.c_ulong(0)
            ctypes.windll.kernel32.WriteFile(handle, data, len(data), ctypes.byref(written), None)
        except Exception:
            pass


def _split(value: str | None) -> list[str]:
    value = str(value or "").strip()
    if not value:
        return []
    # jobs.py serializes controlled arguments with shlex.join on every host,
    # so the transport syntax is POSIX quoting even when the consumer is a
    # Windows process. list2cmdline below handles the final Win32 quoting.
    return shlex.split(value, posix=True)


def _tool(name: str) -> str:
    found = shutil.which(name) or shutil.which(f"{name}.exe")
    return found or name


def output_path(source: str, env: dict[str, str] | None = None) -> Path:
    values = env or os.environ
    src = Path(source)
    suffix = str(values.get("SUFFIX") or "TSD").strip() or "TSD"
    container = str(values.get("HB_OUTPUT_CONTAINER") or "mkv").strip().lower()
    extension = "mp4" if container == "mp4" else "mkv"
    return src.with_name(f"{src.stem}-{suffix}.{extension}")


def build_command(env: dict[str, str] | None = None, *, disable_qsv_decode: bool = False) -> list[str]:
    values = env or os.environ
    source = str(values.get("SRC") or "").strip()
    if not source:
        raise ValueError("SRC is required")
    out = output_path(source, values)
    preset_file = str(values.get("HB_PRESET_FILE") or "")
    preset_name = str(values.get("HB_PRESET_NAME") or "MyPresetName")
    command = [_tool("HandBrakeCLI")]
    if preset_file and os.path.isfile(preset_file):
        command += ["--preset-import-file", preset_file, "-Z", preset_name]
    else:
        command += ["-e", "x264", "-q", "20", "-B", "160"]

    threads = str(values.get("HB_THREADS") or "").strip()
    if threads.isdigit() and int(threads) > 0:
        command += ["--encopts", f"threads={int(threads)}"]

    command += _split(values.get("HB_EXTRA_ARGS"))
    command += _split(values.get("HB_AUDIO_POLICY_OPTS"))
    command += _split(values.get("HB_DIMENSION_OPTS"))

    hw_decode = _split(values.get("HB_HW_DECODE_OPTS") or "--disable-hw-decoding")
    if disable_qsv_decode:
        hw_decode = ["--disable-hw-decoding"]
    video_encoder = str(values.get("HB_VIDEO_ENCODER") or "").lower()
    if video_encoder.startswith("qsv_") or hw_decode == ["--enable-hw-decoding", "qsv"]:
        adapter = str(values.get("TSD_QSV_ADAPTER") or "0").strip()
        if not adapter.isdigit():
            adapter = "0"
        command += ["--qsv-adapter", adapter]
    command += hw_decode

    container = str(values.get("HB_OUTPUT_CONTAINER") or "mkv").strip().lower()
    if container == "mp4":
        command += ["--format", "av_mp4"]
        if str(values.get("HB_WEB_OPTIMIZED") or "0").strip().lower() in TRUE_VALUES:
            command.append("--optimize")
    else:
        command += ["--format", "av_mkv"]
    command += ["-i", source, "-o", str(out)]
    return command


def _run(command: list[str]) -> int:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        _emit(line.rstrip("\r\n"))
    return process.wait()


def run(env: dict[str, str] | None = None) -> int:
    values = env or os.environ
    source = str(values.get("SRC") or "").strip()
    if not source:
        _emit("ERROR: You must provide the source file in SRC.")
        return 1
    if not os.path.isfile(source):
        _emit(f"ERROR: File not found: {source}")
        return 1
    handbrake = _tool("HandBrakeCLI")
    if not shutil.which(handbrake) and not os.path.isfile(handbrake):
        _emit("ERROR: HandBrakeCLI.exe is not installed or on PATH.")
        return 127
    out = output_path(source, values)
    if source.lower().rsplit(".", 1)[0].endswith("-tsd"):
        _emit(f"INFO: Source already has -TSD tag, skipping encode: {source}")
        return 0
    if out.exists():
        _emit(f"ERROR: Output already exists: {out}")
        _emit("Refusing to overwrite. Delete or rename it first.")
        return 1

    preset_name = str(values.get("HB_PRESET_NAME") or "MyPresetName")
    container = str(values.get("HB_OUTPUT_CONTAINER") or "mkv").strip().lower()
    hw_decode = str(values.get("HB_HW_DECODE_LABEL") or "software (not configured)")
    _emit("=== ByteSqueeze Windows encode ===")
    _emit(f"Source : {source}")
    _emit(f"Target : {out}")
    _emit(f"[ByteSqueeze] Output container: {container.upper()}")
    _emit(f"[ByteSqueeze] Hardware decode: {hw_decode}")
    _emit(f"[ByteSqueeze] Video encoder: {values.get('HB_VIDEO_ENCODER') or 'unknown'}")
    _emit(f"[ByteSqueeze] Source resolution: {values.get('HB_SOURCE_RESOLUTION') or 'unknown'}")
    _emit(f"[ByteSqueeze] Target resolution: {values.get('HB_TARGET_RESOLUTION') or 'unknown'}")
    _emit(f"[ByteSqueeze] Selected preset: {preset_name}")
    _emit("=======================================")

    try:
        command = build_command(values)
        _emit("[ByteSqueeze] Command: " + subprocess.list2cmdline(command))
        status = _run(command)
        requested_qsv_decode = _split(values.get("HB_HW_DECODE_OPTS")) == ["--enable-hw-decoding", "qsv"]
        if status and requested_qsv_decode:
            _emit(f"[ByteSqueeze] Hardware decode: software fallback (QSV decode attempt exited {status})")
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass
            status = _run(build_command(values, disable_qsv_decode=True))
        if status:
            return status
    except (OSError, ValueError) as exc:
        _emit(f"ERROR: Could not start HandBrake: {exc}")
        return 1
    if not out.is_file() or out.stat().st_size <= 0:
        _emit("ERROR: Encode failed, output file was not created.")
        return 1
    _emit("Done!")
    _emit(f"New file: {out}")
    _emit(f"Source kept for app-level validation: {source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
