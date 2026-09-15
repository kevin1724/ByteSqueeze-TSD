"""Cross-platform HandBrake launcher used by every ByteSqueeze worker.

The launcher consumes one environment contract on Linux and Windows.  Keeping
container choice, destination path, validation target, and HandBrake arguments
in this process prevents mixed-version shell scripts from deriving a second
output filename.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


TRUE_VALUES = {"1", "true", "yes", "on"}

# HandBrake rejects the language/all-track selectors produced by legacy Smart
# Presets when the audio policy supplies an explicit per-track ``--audio``
# plan.  The policy is the authoritative, post-ffprobe plan, so remove every
# older audio selector/encoder option before appending it.
_AUDIO_OPTIONS_WITH_VALUE = {
    "--audio",
    "--aencoder",
    "--ab",
    "--mixdown",
    "--arate",
    "--gain", "--aname",
    "--audio-copy-mask", "--audio-fallback", "--audio-lang-list",
    "--audio-dither", "--audio-compression", "--audio-normalize-mix",
}
_AUDIO_SHORT_OPTIONS_WITH_VALUE = {"-a", "-E", "-B", "-6", "-R"}
_AUDIO_FLAG_OPTIONS = {"--all-audio", "--first-audio"}
_NVENC_AV1_ENCODERS = {"nvenc_av1", "nvenc_av1_10bit"}
RUNNER_CONTRACT_VERSION = "2"


def _background_process_options() -> dict:
    """Prevent HandBrake from flashing a console behind the tray app."""
    if os.name != "nt":
        return {}
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    options: dict = {"creationflags": flags}
    startupinfo_type = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_type is not None:
        startupinfo = startupinfo_type()
        startupinfo.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0))
        startupinfo.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
        options["startupinfo"] = startupinfo
    return options


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


def _without_audio_options(arguments: list[str]) -> list[str]:
    """Remove legacy HandBrake audio options from a tokenized CLI fragment."""
    cleaned: list[str] = []
    skip_value = False
    for argument in arguments:
        if skip_value:
            skip_value = False
            continue
        raw_option = str(argument).split("=", 1)[0]
        option = raw_option.lower() if raw_option.startswith("--") else raw_option
        if option in _AUDIO_FLAG_OPTIONS:
            continue
        if option in _AUDIO_OPTIONS_WITH_VALUE or raw_option in _AUDIO_SHORT_OPTIONS_WITH_VALUE:
            skip_value = "=" not in str(argument)
            continue
        cleaned.append(argument)
    return cleaned


def _argument_value(arguments: list[str], name: str) -> str:
    value = ""
    for index, argument in enumerate(arguments):
        text = str(argument)
        if text == name and index + 1 < len(arguments):
            value = str(arguments[index + 1])
        elif text.startswith(name + "="):
            value = text.split("=", 1)[1]
    return value


def _selected_preset_definition(preset_file: str, preset_name: str) -> dict:
    if not preset_file or not os.path.isfile(preset_file):
        return {}
    import json

    with open(preset_file, "r", encoding="utf-8") as stream:
        data = json.load(stream)
    candidates: list[dict] = []

    def walk(value) -> None:
        if isinstance(value, dict):
            if str(value.get("VideoEncoder") or "").strip():
                candidates.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    expected = str(preset_name or "").strip().casefold()
    return next(
        (
            item for item in candidates
            if str(item.get("PresetName") or item.get("Name") or "").strip().casefold() == expected
        ),
        candidates[0] if candidates else {},
    )


def _validate_encoder_profile(
    video_encoder: str,
    preset_file: str,
    preset_name: str,
    extra_args: list[str],
) -> None:
    """Reject known-invalid encoder/profile combinations before launching."""
    preset = _selected_preset_definition(preset_file, preset_name)
    encoder = (
        _argument_value(extra_args, "--encoder")
        or video_encoder
        or str(preset.get("VideoEncoder") or "")
    ).strip().lower()
    profile = (
        _argument_value(extra_args, "--encoder-profile")
        or str(preset.get("VideoProfile") or "")
    ).strip().lower()
    if encoder in _NVENC_AV1_ENCODERS and profile not in {"", "auto"}:
        raise ValueError(
            f"Unsupported profile {profile!r} for {encoder}; "
            "AV1 NVENC must use HandBrake Auto/default."
        )


def _tool(name: str) -> str:
    found = shutil.which(name) or shutil.which(f"{name}.exe")
    return found or name


def output_path(source: str, env: dict[str, str] | None = None) -> Path:
    values = env or os.environ
    src = Path(source)
    suffix = str(values.get("SUFFIX") or "TSD").strip() or "TSD"
    container = str(values.get("HB_OUTPUT_CONTAINER") or "mkv").strip().lower()
    extension = "mp4" if container == "mp4" else "mkv"
    canonical = str(values.get("HB_OUTPUT_PATH") or "").strip()
    if canonical:
        out = Path(canonical)
        if out.suffix.lower() != f".{extension}":
            raise ValueError(
                "HB_OUTPUT_PATH extension does not match HB_OUTPUT_CONTAINER: "
                f"{canonical!r} is not .{extension}"
            )
        return out
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

    extra_args = _split(values.get("HB_EXTRA_ARGS"))
    audio_policy_args = _split(values.get("HB_AUDIO_POLICY_OPTS"))
    if audio_policy_args:
        extra_args = _without_audio_options(extra_args)
    video_encoder = str(values.get("HB_VIDEO_ENCODER") or "").lower()
    _validate_encoder_profile(video_encoder, preset_file, preset_name, extra_args)
    command += extra_args
    command += audio_policy_args
    command += _split(values.get("HB_DIMENSION_OPTS"))

    hw_decode = _split(values.get("HB_HW_DECODE_OPTS") or "--disable-hw-decoding")
    if disable_qsv_decode:
        hw_decode = ["--disable-hw-decoding"]
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
        **_background_process_options(),
    )
    assert process.stdout is not None
    for line in process.stdout:
        _emit(line.rstrip("\r\n"))
    return process.wait()


def _requested_qsv_decode(values: dict[str, str]) -> bool:
    return _split(values.get("HB_HW_DECODE_OPTS")) == ["--enable-hw-decoding", "qsv"]


def _qsv_job(values: dict[str, str]) -> bool:
    encoder = str(values.get("HB_VIDEO_ENCODER") or "").strip().lower()
    return encoder.startswith("qsv_") or _requested_qsv_decode(values)


def _qsv_preflight(values: dict[str, str]) -> tuple[bool, str]:
    """Validate Linux render-node access and stream diagnostic output."""
    if os.name == "nt" or not _qsv_job(values):
        return True, "not required"
    helper = shutil.which("bytesqueeze-qsv-preflight")
    if not helper:
        return False, "QSV preflight helper is missing"
    status = _run([helper, "encode"])
    if status:
        return False, f"QSV render-device preflight exited {status}"
    return True, "passed"


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
    _emit(f"[ByteSqueeze] Encoder runner contract: {RUNNER_CONTRACT_VERSION}")
    _emit("=== ByteSqueeze encode ===")
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
        preflight_ok, preflight_reason = _qsv_preflight(values)
        disable_qsv_decode = bool(not preflight_ok and _requested_qsv_decode(values))
        if disable_qsv_decode:
            _emit(f"[ByteSqueeze] Hardware decode: software fallback ({preflight_reason})")
        command = build_command(values, disable_qsv_decode=disable_qsv_decode)
        command_text = subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
        _emit("[ByteSqueeze] Command: " + command_text)
        status = _run(command)
        if status and _requested_qsv_decode(values) and not disable_qsv_decode:
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
