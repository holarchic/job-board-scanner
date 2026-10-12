"""Desktop notifications for scheduled runs (macOS). Best effort: a notification that fails or hangs is logged and
never changes the run's outcome."""
import logging
import subprocess
import sys
from collections.abc import Callable

TIMEOUT = 10
# The text travels as arguments, never spliced into the script, so nothing in it can change what runs.
SCRIPT = ["-e", "on run argv", "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
          "-e", "end run"]
log = logging.getLogger("jobscan")


def notify(message: str, run: Callable[..., subprocess.CompletedProcess] | None = None) -> None:
    if sys.platform != "darwin":
        return
    try:
        (run or subprocess.run)(["osascript", *SCRIPT, message, "jobscan"], capture_output=True, timeout=TIMEOUT,
                                check=True)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("notify_failed error=%s", type(e).__name__)
