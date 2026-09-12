"""Cross-platform subprocess options used by background worker tasks."""

from __future__ import annotations

import os
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
