"""What `jobscan --version` prints and each run logs: the version, and the commit when one is known.

A checkout and an install answer from different sources, never mixed: a checkout's `jobscan.egg-info` (left by a
build) can name an older version than its `pyproject.toml`."""
import json
import subprocess
import tomllib
from importlib import metadata
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parent.parent
NAME = "jobscan"


def line(checkout: Path = CHECKOUT) -> str:
    """`0.4.0 (5aef4aa)`; the version alone where no commit is known (an install from a zip or a wheel); `unknown`
    with no version at all."""
    version, commit = _checkout(checkout) or _installed()
    if version is None:
        return "unknown"
    return f"{version} ({commit})" if commit else version


def _checkout(root: Path) -> tuple[str, str | None] | None:
    """The version in the checkout's manifest and its HEAD; None when the package isn't running from a checkout."""
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(project, dict) or project.get("name") != NAME or not isinstance(project.get("version"), str):
        return None
    try:
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "--short=7", "HEAD"], capture_output=True,
                              text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):  # git missing, failing (not a repository) or hung
        return project["version"], None
    return project["version"], head.stdout.strip() or None


def _installed() -> tuple[str | None, str | None]:
    """The installed distribution's version, and the commit a git install recorded (PEP 610 `direct_url.json`)."""
    try:
        dist = metadata.distribution(NAME)
    except metadata.PackageNotFoundError:
        return None, None
    try:
        vcs = json.loads(dist.read_text("direct_url.json") or "{}").get("vcs_info")
    except (ValueError, AttributeError):
        vcs = None
    commit = vcs.get("commit_id") if isinstance(vcs, dict) else None
    return dist.version, commit[:7] if isinstance(commit, str) and commit else None
