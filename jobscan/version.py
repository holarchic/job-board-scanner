"""What `jobscan --version` prints and each run logs: the version, and the commit when one is known.

A checkout and an install answer from different sources, never mixed: a checkout's `jobscan.egg-info` (left by a
build) can name an older version than its `pyproject.toml`."""
import json
import subprocess
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parent.parent
NAME = "jobscan"


@dataclass(frozen=True)
class Provenance:
    version: str | None  # None: no metadata anywhere
    commit: str | None  # seven hex digits; None where none is known (an install from a zip or a wheel)


def line(checkout: Path = CHECKOUT) -> str:
    """`0.4.0 (5aef4aa)`; the version alone where no commit is known; `unknown` with no version at all."""
    found = _checkout(checkout) or _installed()
    if found.version is None:
        return "unknown"
    return f"{found.version} ({found.commit})" if found.commit else found.version


def _checkout(root: Path) -> Provenance | None:
    """The version in the checkout's manifest and its HEAD; None when the package isn't running from a checkout. A
    copy of the source inside some other repository has no commit of its own: git would answer for the outer one."""
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(project, dict) or project.get("name") != NAME or not isinstance(project.get("version"), str):
        return None
    try:
        answer = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel", "--short=7", "HEAD"],
                                capture_output=True, text=True, timeout=5, check=True).stdout.split("\n")
    except (OSError, subprocess.SubprocessError):  # git missing, failing (not a repository) or hung
        return Provenance(project["version"], None)
    ours = len(answer) > 1 and Path(answer[0]).resolve() == root.resolve() and answer[1].strip()
    return Provenance(project["version"], answer[1].strip() if ours else None)


def _installed() -> Provenance:
    """The installed distribution's version, and the commit a git install recorded (PEP 610 `direct_url.json`)."""
    try:
        dist = metadata.distribution(NAME)
    except metadata.PackageNotFoundError:
        return Provenance(None, None)
    try:
        vcs = json.loads(dist.read_text("direct_url.json") or "{}").get("vcs_info")
    except (ValueError, AttributeError):
        vcs = None
    commit = vcs.get("commit_id") if isinstance(vcs, dict) else None
    return Provenance(dist.version, commit[:7] if isinstance(commit, str) and commit else None)
