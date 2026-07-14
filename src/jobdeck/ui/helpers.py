"""Small UI utilities shared by pages."""

import subprocess
import sys


def open_in_system(path: str) -> None:
    """Open a file or folder with the system's default application.

    The UI runs on the same machine as the browser (local-first app), so
    spawning the desktop opener is the natural way to show documents.
    """
    try:
        if sys.platform.startswith("win"):
            import os

            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass
