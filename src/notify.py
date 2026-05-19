"""
macOS desktop notification helper. Silent on Linux/Windows.

Usage as a library:
    from src.notify import send
    send("Title", "Body")

Usage from CLI:
    python3 -m src.notify "Title" "Body"
"""

from __future__ import annotations
import platform
import subprocess
import sys


def send(title: str, body: str) -> bool:
    """Show a desktop notification. Returns True if dispatched, False otherwise."""
    if platform.system() != "Darwin":
        return False
    title = title.replace('"', '\\"').replace("\\", "\\\\")
    body = body.replace('"', '\\"').replace("\\", "\\\\")
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{body}" with title "{title}"'],
            check=False,
            capture_output=True,
            timeout=5,
        )
        return True
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        send(sys.argv[1], sys.argv[2])
    else:
        send("Moomoo", "Test notification")
