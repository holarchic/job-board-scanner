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
from typing import NoReturn
from xml.parsers.expat import ExpatError

LABEL_PREFIX = "io.github.holarchic.jobscan."
NOT_LOADED = 3  # `launchctl bootout` of a label that isn't loaded: ESRCH, "No such process"
TIMEOUT = 30


class ScheduleError(Exception):
    """launchd refused the job, its plist couldn't be written or deleted, or launchctl couldn't be asked. From
    `install`, unless the message says otherwise, the previous schedule, if any, is back in place; from `remove`, the
    job may be unloaded with its plist left behind (`in_place` then sets it up again)."""


@dataclass(frozen=True)
class Job:
    label: str
    plist: Path


Launchctl = Callable[[list[str]], subprocess.CompletedProcess]


@dataclass(frozen=True)
class Launchd:
    """Where this user's launch agents live and how launchd is asked: the real ones from `launchd()`, fakes in tests."""
    agents: Path
    run: Launchctl


def launchctl(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=TIMEOUT, check=False)


def launchd() -> Launchd | None:
    """This Mac's launchd; None elsewhere, where a schedule is a cron line the user adds."""
    if sys.platform != "darwin":
        return None
    return Launchd(agents=Path.home() / "Library" / "LaunchAgents", run=launchctl)


def parse_at(text: str) -> time | None:
    """A daily time as HH:MM on the 24-hour clock (the hour may be one digit), else None."""
    hour, _, minute = text.partition(":")
    if not (hour.isascii() and hour.isdigit() and minute.isascii() and minute.isdigit() and len(hour) <= 2
            and len(minute) == 2 and int(hour) < 24 and int(minute) < 60):
        return None
    return time(int(hour), int(minute))


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


def scheduled_at(home: Path, agents: Path) -> time | None:
    """The time this install's plist runs at, as written; None when there is none, it doesn't parse, or it isn't a
    daily plist as `install` writes."""
    try:
        spec = plistlib.loads(job(home, agents).plist.read_bytes())
    # What plistlib raises on files it can't read: a bad XML value or encoding, a truncated binary or XML file.
    except (OSError, ExpatError, ValueError, TypeError, AttributeError, LookupError, OverflowError):
        return None
    when = spec.get("StartCalendarInterval") if isinstance(spec, dict) else None
    if not (isinstance(when, dict) and set(when) == {"Hour", "Minute"}
            and all(type(v) is int for v in when.values()) and 0 <= when["Hour"] < 24 and 0 <= when["Minute"] < 60):
        return None
    return time(when["Hour"], when["Minute"])


def loaded(home: Path, launchd: Launchd) -> bool:
    """launchd's own answer: whether it holds this install's job, whatever its plist says."""
    try:
        return launchd.run(["print", f"gui/{os.getuid()}/{job(home, launchd.agents).label}"]).returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        raise ScheduleError(f"{type(e).__name__}: {e}") from None


def in_place(home: Path, at: time, launchd: Launchd) -> bool:
    """The job `install` would set up is already there: the plist byte for byte (same time, same interpreter) and
    loaded. A job left unloaded beside its plist (by hand, or a removal whose delete failed) is not. ScheduleError
    when launchctl can't be asked."""
    j = job(home, launchd.agents)
    try:
        written = j.plist.read_bytes()
    except OSError:
        return False
    return written == plist(j, home, at) and loaded(home, launchd)


def install(home: Path, at: time, agents: Path, run: Launchctl | None = None) -> Job:
    """Replaces this install's job with one daily at `at`. A refused bootout changes nothing. Any later failure,
    Ctrl-C included, puts the old job back (or leaves none when there was none) and raises; if that fails too, the
    ScheduleError says so."""
    run = run or launchctl
    j = job(home, agents)
    domain = f"gui/{os.getuid()}"
    try:
        agents.mkdir(parents=True, exist_ok=True)
        previous = j.plist.read_bytes() if j.plist.exists() else None
    except OSError as e:
        raise ScheduleError(f"{type(e).__name__}: {e}") from None
    try:
        _bootout(run, domain, j)
    except ScheduleError:
        raise  # launchctl answered and refused: the old job is as it was
    except OSError as e:
        raise ScheduleError(f"{type(e).__name__}: {e}") from None  # launchctl never ran
    except BaseException as failure:  # interrupted or timed out: the old job may or may not be loaded
        _roll_back(run, domain, j, previous, failure)
    try:
        _write(j.plist, plist(j, home, at))
        _check(run(["bootstrap", domain, str(j.plist)]), "bootstrap")
    except BaseException as failure:
        _roll_back(run, domain, j, previous, failure)
    return j


def _roll_back(run: Launchctl, domain: str, j: Job, previous: bytes | None, failure: BaseException) -> NoReturn:
    """Puts the previous job back, then re-raises: operational failures as ScheduleError, Ctrl-C as itself."""
    cause = str(failure) if isinstance(failure, ScheduleError) else f"{type(failure).__name__}: {failure}"
    try:
        _restore(run, domain, j, previous)
    except (OSError, subprocess.SubprocessError, ScheduleError) as e:
        raise ScheduleError(f"{cause}; putting the previous schedule back also failed: {e}") from None
    if isinstance(failure, (OSError, subprocess.SubprocessError, ScheduleError)):
        raise ScheduleError(cause) from None
    raise failure


def _restore(run: Launchctl, domain: str, j: Job, previous: bytes | None) -> None:
    """Whatever launchd has loaded under the label goes first: an interrupted bootstrap may have loaded the new job."""
    _bootout(run, domain, j)
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
