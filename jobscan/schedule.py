"""`jobscan schedule`: a daily launchd job that runs `jobscan run --notify` (macOS).

launchd, not cron, because launchd starts a calendar job on wake when the Mac slept through its slot; several missed
slots collapse into one run. The plist names this install's own interpreter, so the job runs the jobscan that
scheduled it."""
import hashlib
import os
import plistlib
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

LABEL_PREFIX = "io.github.holarchic.jobscan."
NOT_LOADED = 3  # `launchctl bootout` of a label that isn't loaded: ESRCH, "No such process"
TIMEOUT = 30


class ScheduleError(Exception):
    """launchd refused the job; the previous schedule, if any, is back in place."""


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


def plist(job: Job, home: Path, hour: int, minute: int) -> bytes:
    home = home.resolve()
    return plistlib.dumps({
        "Label": job.label,
        # sys.executable as invoked, not resolved: uv's tool environment is the symlinked interpreter's venv.
        "ProgramArguments": [sys.executable, "-m", "jobscan", "run", "--notify"],
        "EnvironmentVariables": {"JOBSCAN_HOME": str(home)},
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(home / "launchd.log"),
        "StandardErrorPath": str(home / "launchd.log"),
    })


def install(home: Path, hour: int, minute: int, agents: Path, run: Launchctl | None = None) -> Job:
    """Replaces this install's job with one at hour:minute. Any failure after the old job is unloaded puts the old
    one back (or removes the new plist when there was none) and raises ScheduleError."""
    run = run or launchctl
    j = job(home, agents)
    domain = f"gui/{os.getuid()}"
    agents.mkdir(parents=True, exist_ok=True)
    previous = j.plist.read_bytes() if j.plist.exists() else None
    _bootout(run, domain, j)
    try:
        _write(j.plist, plist(j, home, hour, minute))
        _check(run(["bootstrap", domain, str(j.plist)]), "bootstrap")
    except BaseException:
        if previous is None:
            j.plist.unlink(missing_ok=True)
        else:
            _write(j.plist, previous)
            run(["bootstrap", domain, str(j.plist)])
        raise
    return j


def remove(home: Path, agents: Path, run: Launchctl | None = None) -> Job:
    """Unloads and deletes this install's job; nothing to remove is not an error."""
    j = job(home, agents)
    _bootout(run or launchctl, f"gui/{os.getuid()}", j)
    j.plist.unlink(missing_ok=True)
    return j


def cron_line(home: Path, hour: int, minute: int) -> str:
    return (f"{minute} {hour} * * *  JOBSCAN_HOME={home.resolve()} {sys.executable} -m jobscan run "
            f">> {home.resolve() / 'cron.log'} 2>&1")


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
