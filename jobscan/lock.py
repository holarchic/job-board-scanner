"""One process at a time on a $JOBSCAN_HOME's database: a run, `preview`, `titles` and the set-up page take the
same lock, so new code never backs up or migrates the database under a run, and two runs never double-tag."""
import fcntl
from pathlib import Path

from jobscan.config import ConfigError


class RunLocked(Exception):
    """Another jobscan process holds this $JOBSCAN_HOME."""


class run_lock:
    """Held for the `with` block; never waits: RunLocked at once when another process holds it."""

    def __init__(self, home: Path):
        self.path = home / ".lock"

    def __enter__(self):
        try:
            self.file = self.path.open("w")
        except OSError as e:
            raise ConfigError(f"{self.path}: not writable ({type(e).__name__})") from None
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise RunLocked from None
        except OSError as e:  # a filesystem without locks (ENOLCK)
            self.file.close()
            raise ConfigError(f"{self.path}: can't be locked ({type(e).__name__})") from None
        return self

    def __exit__(self, *exc):
        self.file.close()
