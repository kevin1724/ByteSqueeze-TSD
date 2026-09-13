"""Cross-platform subprocess options used by background worker tasks."""

from __future__ import annotations

import os
import signal
import subprocess


def background_process_options(*, process_group: bool = False) -> dict:
    """Keep helper/encoder processes invisible on Windows.

    The packaged worker is a windowed executable, but console programs such as
    PowerShell, ffprobe, FFmpeg, and HandBrake can still create a short-lived
    console window unless every launch explicitly suppresses it.
    """
    if os.name != "nt":
        return {"start_new_session": True} if process_group else {}

    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if process_group:
        flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    options: dict = {"creationflags": flags}
    startupinfo_type = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_type is not None:
        startupinfo = startupinfo_type()
        startupinfo.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0))
        startupinfo.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
        options["startupinfo"] = startupinfo
    return options


def terminate_process_tree(pid: int, *, force: bool = True) -> tuple[bool, str]:
    """Terminate an encoder and every child process it launched.

    Windows does not implement POSIX process-group termination. Killing the
    ByteSqueeze ``--encode-one`` launcher alone leaves HandBrakeCLI running and
    holding the GPU/output pipe, so use the native tree-aware taskkill command.
    """
    try:
        process_id = int(pid)
    except (TypeError, ValueError):
        return False, "invalid process id"
    if process_id <= 0 or process_id == os.getpid():
        return False, "refusing to terminate invalid/current process"
    if os.name == "nt":
        command = ["taskkill.exe", "/PID", str(process_id), "/T"]
        if force:
            command.append("/F")
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
                **background_process_options(),
            )
        except Exception as exc:
            return False, f"taskkill failed: {exc}"
        detail = str(result.stdout or result.stderr or "").strip()
        # A process that exited between poll and taskkill is already clean.
        already_gone = "not found" in detail.lower() or "no running instance" in detail.lower()
        return result.returncode == 0 or already_gone, detail[:500]
    try:
        os.killpg(process_id, signal.SIGKILL if force else signal.SIGTERM)
        return True, "process group terminated"
    except ProcessLookupError:
        return True, "process already exited"
    except Exception as exc:
        try:
            os.kill(process_id, signal.SIGKILL if force else signal.SIGTERM)
            return True, "process terminated"
        except ProcessLookupError:
            return True, "process already exited"
        except Exception as fallback_exc:
            return False, f"process termination failed: {exc}; {fallback_exc}"
