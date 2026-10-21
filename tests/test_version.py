"""`jobscan --version`: a checkout's manifest and HEAD, else the install's metadata and recorded commit."""
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from importlib import metadata
from pathlib import Path
from unittest import mock

from jobscan import cli, version

MANIFEST = '[project]\nname = "jobscan"\nversion = "9.8.7"\n'


class FakeDist:
    def __init__(self, version: str, direct_url: dict | None):
        self.version = version
        self.direct_url = direct_url

    def read_text(self, name: str) -> str | None:
        return json.dumps(self.direct_url) if name == "direct_url.json" and self.direct_url is not None else None


def installed(dist: FakeDist | None):
    found = mock.Mock(side_effect=metadata.PackageNotFoundError("jobscan")) if dist is None else \
        mock.Mock(return_value=dist)
    return mock.patch.object(version.metadata, "distribution", found)


class VersionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@example.com",
                               *args], check=True, capture_output=True, text=True).stdout.strip()

    def test_a_checkout_names_its_manifest_version_and_head_never_the_installed_one(self):
        (self.root / "pyproject.toml").write_text(MANIFEST)
        self.git("init", "-q")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "x")
        head = self.git("rev-parse", "HEAD")[:7]
        with installed(FakeDist("0.0.1", {"vcs_info": {"commit_id": "f" * 40}})):
            self.assertEqual(version.line(self.root), f"9.8.7 ({head})")

    def test_a_checkout_without_git_or_outside_a_repository_names_the_version_alone(self):
        (self.root / "pyproject.toml").write_text(MANIFEST)
        self.assertEqual(version.line(self.root), "9.8.7")  # not a repository: git fails
        with mock.patch.object(version.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertEqual(version.line(self.root), "9.8.7")

    def test_an_install_names_the_commit_a_git_install_recorded(self):
        with installed(FakeDist("1.2.3", {"url": "https://example.com/x", "vcs_info": {"vcs": "git",
                                                                                       "commit_id": "abcdef1234"}})):
            self.assertEqual(version.line(self.root), "1.2.3 (abcdef1)")

    def test_an_install_from_a_zip_or_a_wheel_names_the_version_alone(self):
        for direct_url in [{"url": "https://example.com/main.zip", "archive_info": {}}, None]:
            with installed(FakeDist("1.2.3", direct_url)):
                self.assertEqual(version.line(self.root), "1.2.3", direct_url)

    def test_another_projects_manifest_is_not_a_checkout(self):
        (self.root / "pyproject.toml").write_text(MANIFEST.replace('"jobscan"', '"other"'))
        with installed(FakeDist("1.2.3", None)):
            self.assertEqual(version.line(self.root), "1.2.3")

    def test_no_version_anywhere_is_unknown(self):
        with installed(None):
            self.assertEqual(version.line(self.root), "unknown")

    def test_the_flag_prints_the_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--version"]), 0)
        self.assertEqual(out.getvalue(), f"jobscan {version.line()}\n")
        self.assertRegex(out.getvalue(), r"^jobscan \d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
