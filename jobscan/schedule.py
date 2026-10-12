"""`jobscan schedule`: a daily launchd job that runs `jobscan run --notify` (macOS).

launchd, not cron, because launchd starts a calendar job on wake when the Mac slept through its slot; several missed
slots collapse into one run. The plist names this install's own interpreter, so the job runs the jobscan that
scheduled it."""
import hashlib
import os
import plistlib
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import time
from pathlib import Path

LABEL_PREFIX = "io.github.holarchic.jobscan."
NOT_LOADED = 3  # `launchctl bootout` of a label that isn't loaded: ESRCH, "No such process"
TIMEOUT = 30


class ScheduleError(Exception):
    """launchd refused the job or its plist couldn't be written; unless the message says otherwise, the previous
    schedule, if any, is back in place."""


@dataclass(frozen=True)
class Job:
    label: str
    plist: Path


Launchctl = Callable[[list[str]], subprocess.CompletedProcess]


def launchctl(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=TIMEOUT, check=False)


def job(home: Path, agents: Path) -> Job:
    """One job per install: the label carries a hash of the resolved home, so two installs never collide."""
    label = LABEL_PREFIX + hashlib.sha256(str(home.resolve()).encode()).hexdigest()[:8]
    return Job(label=label, plist=agents / f"{label}.plist")


def plist(job: Job, home: Path, at: time) -> bytes:
    home = home.resolve()
    return plistlib.dumps({
        "Label": job.label,
        # sys.executable as invoked, not resolved: uv's tool environment is the symlinked interpreter's venv.
        "ProgramArguments": [sys.executable, "-m", "jobscan", "run", "--notify"],
        "EnvironmentVariables": {"JOBSCAN_HOME": str(home)},
        "StartCalendarInterval": {"Hour": at.hour, "Minute": at.minute},
        "StandardOutPath": str(home / "launchd.log"),
        "StandardErrorPath": str(home / "launchd.log"),
    })


def install(home: Path, at: time, agents: Path, run: Launchctl | None = None) -> Job:
    """Replaces this install's job with one daily at `at`. Any failure after the old job is unloaded puts the old
    one back (or removes the new plist when there was none) and raises; if putting it back fails too, the
    ScheduleError says so."""
    run = run or launchctl
    j = job(home, agents)
    domain = f"gui/{os.getuid()}"
    try:
        agents.mkdir(parents=True, exist_ok=True)
        previous = j.plist.read_bytes() if j.plist.exists() else None
        _bootout(run, domain, j)
    except (OSError, subprocess.SubprocessError) as e:
        raise ScheduleError(f"{type(e).__name__}: {e}") from None
    try:
        _write(j.plist, plist(j, home, at))
        _check(run(["bootstrap", domain, str(j.plist)]), "bootstrap")
    except BaseException as failure:  # Ctrl-C too: the old job is unloaded at this point
        cause = str(failure) if isinstance(failure, ScheduleError) else f"{type(failure).__name__}: {failure}"
        try:
            _restore(run, domain, j, previous)
        except (OSError, subprocess.SubprocessError, ScheduleError) as e:
            raise ScheduleError(f"{cause}; putting the previous schedule back also failed: {e}") from None
        if isinstance(failure, (OSError, subprocess.SubprocessError, ScheduleError)):
            raise ScheduleError(cause) from None
        raise
    return j


def _restore(run: Launchctl, domain: str, j: Job, previous: bytes | None) -> None:
    if previous is None:
        j.plist.unlink(missing_ok=True)
        return
    if not j.plist.exists() or j.plist.read_bytes() != previous:  # intact when the new plist never got written
        _write(j.plist, previous)
    _check(run(["bootstrap", domain, str(j.plist)]), "bootstrap")


def remove(home: Path, agents: Path, run: Launchctl | None = None) -> Job:
    """Unloads and deletes this install's job; nothing to remove is not an error."""
    j = job(home, agents)
    try:
        _bootout(run or launchctl, f"gui/{os.getuid()}", j)
        j.plist.unlink(missing_ok=True)
    except (OSError, subprocess.SubprocessError) as e:
        raise ScheduleError(f"{type(e).__name__}: {e}") from None
    return j


def cron_line(home: Path, at: time) -> str:
    home = home.resolve()
    command = (f"JOBSCAN_HOME={shlex.quote(str(home))} {shlex.quote(sys.executable)} -m jobscan run "
               f">> {shlex.quote(str(home / 'cron.log'))} 2>&1")
    return f"{at.minute} {at.hour} * * *  " + command.replace("%", "\\%")  # cron reads a bare % as a newline


def _bootout(run: Launchctl, domain: str, j: Job) -> None:
    result = run(["bootout", f"{domain}/{j.label}"])
    if result.returncode not in (0, NOT_LOADED):
        _check(result, "bootout")


def _check(result: subprocess.CompletedProcess, step: str) -> None:
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise ScheduleError(f"launchctl {step} failed ({result.returncode}): {detail}")


def _write(path: Path, data: bytes) -> None:
    """Atomic: launchd never sees half a plist. The temp file is always removed."""
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)
