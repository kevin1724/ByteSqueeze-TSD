"""Compatibility import for the shared ByteSqueeze encoder launcher.

The canonical implementation lives in ``webui.app`` because that package is
present in controllers, Docker workers, source checkouts, and packaged native
workers. Existing imports through ``worker.encode_runner`` remain supported.
"""

from webui.app.encode_runner import (
    RUNNER_CONTRACT_VERSION,
    build_command,
    output_path,
    run,
)

__all__ = [
    "RUNNER_CONTRACT_VERSION",
    "build_command",
    "output_path",
    "run",
]


if __name__ == "__main__":
    raise SystemExit(run())
