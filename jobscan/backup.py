"""Automatic copies of the database: before code the database hasn't seen opens it, a consistent copy goes under
`$JOBSCAN_HOME/backups`, so every upgrade has an undo. `store.open_db` calls `before_open` first, so the copy precedes
every migration and write. A copy that can't be made stops the caller: no new code touches a database without one."""
import hashlib
import logging
import os
import sqlite3
import time
from datetime import UTC, datetime
from functools import cache
from pathlib import Path

from jobscan.config import ConfigError

FOLDER = "backups"
MARKER = "opened-by"  # the code identity that last opened the database
PREFIX = "auto-"  # copies made here; anything else in the folder (copies made by hand) is never touched
KEEP = 2
log = logging.getLogger("jobscan")


@cache
def code_identity() -> str:
    """SHA-256 of the package's own files, names and contents in name order. It changes with every upgrade, where the
    version string doesn't: every git install reports 0.1.0."""
    package = Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted([*package.glob("*.py"), package / "tags.toml"]):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def before_open(database: Path) -> None:
    """Copies the database when the code opening it isn't the code that last did, then records this code. No database
    yet: only the record. Raises ConfigError, leaving the record as it was, when the copy can't be made."""
    folder = database.parent / FOLDER
    try:
        identity = code_identity()
    except OSError as e:
        raise ConfigError(f"{Path(__file__).parent}: cannot read jobscan's own files ({type(e).__name__})") from None
    try:
        if (folder / MARKER).read_text().strip() == identity:
            return
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        raise ConfigError(f"{folder / MARKER}: unreadable ({type(e).__name__})") from None
    try:
        folder.mkdir(exist_ok=True)
        if database.exists():
            _copy(database, folder, identity)
        _write_marker(folder, identity)
    except (OSError, sqlite3.Error) as e:
        raise ConfigError(f"{folder}: cannot copy the database before this version of jobscan first uses it "
                          f"({type(e).__name__}: {e}); nothing was changed") from None


def _copy(database: Path, folder: Path, identity: str) -> None:
    """Prunes to the newest copy first, so at most two exist; then a consistent copy through SQLite's backup API into
    a temp file, published by hard link (never over an existing file)."""
    for old in sorted(folder.glob(f"{PREFIX}*.sqlite"))[:-(KEEP - 1)]:
        old.unlink()
    started = time.monotonic()
    target = folder / f"{PREFIX}{_stamp()}-{identity[:8]}.sqlite"
    temp = folder / f".{target.name}.tmp"
    try:
        source = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            copy = sqlite3.connect(temp)
            try:
                source.backup(copy)
            finally:
                copy.close()
        finally:
            source.close()
        os.link(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    log.info("backup_done path=%s seconds=%.1f", target, time.monotonic() - started)


def _stamp() -> str:
    """UTC time to the microsecond: copies sort by name in the order they were made."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")


def _write_marker(folder: Path, identity: str) -> None:
    """Replaced in one rename, through a temp file of this process's own (callers hold the run lock; this keeps an
    unlocked caller from clobbering it)."""
    temp = folder / f".{MARKER}.{os.getpid()}.tmp"
    try:
        temp.write_text(identity + "\n")
        os.replace(temp, folder / MARKER)
    finally:
        temp.unlink(missing_ok=True)
