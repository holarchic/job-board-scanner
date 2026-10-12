"""`jobscan schedule` (launchd job lifecycle through a fake launchctl) and the notifier."""
import contextlib
import io
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobscan import cli, notify, schedule

PROFILE = 'titles = ["PM"]\nlocations = ["London"]\neligible_where = "the UK"\ntags = ["founding"]\n'
CONFIG = ('[model]\nbase_url = "https://openrouter.ai/api/v1"\nslug = "x/y"\nprovider = "p"\n'
          'quantization = "q"\n')


class FakeLaunchctl:
    """Records each launchctl call; `answers[verb]` lists the exit statuses to give, then 0."""

    def __init__(self, **answers):
        self.calls, self.answers = [], {verb: list(codes) for verb, codes in answers.items()}

    def __call__(self, args):
        self.calls.append(args)
        codes = self.answers.get(args[0], [])
        code = codes.pop(0) if codes else 0
        return subprocess.CompletedProcess(["launchctl", *args], code, stdout="", stderr=f"{args[0]} said {code}")


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir()
        self.agents = Path(tmp.name) / "LaunchAgents"
        self.job = schedule.job(self.home, self.agents)
        self.domain = f"gui/{os.getuid()}"

    def leftovers(self):
        return sorted(p.name for p in self.agents.iterdir() if p != self.job.plist) if self.agents.exists() else []

    def test_plist_runs_this_jobscan_daily_with_its_home(self):
        job = schedule.install(self.home, 8, 5, self.agents, FakeLaunchctl(bootout=[3]))
        spec = plistlib.loads(job.plist.read_bytes())
        self.assertEqual(spec["Label"], job.label)
        self.assertTrue(job.label.startswith(schedule.LABEL_PREFIX))
        self.assertEqual(spec["ProgramArguments"], [sys.executable, "-m", "jobscan", "run", "--notify"])
        self.assertEqual(spec["EnvironmentVariables"], {"JOBSCAN_HOME": str(self.home.resolve())})
        self.assertEqual(spec["StartCalendarInterval"], {"Hour": 8, "Minute": 5})
        self.assertEqual(spec["StandardErrorPath"], str(self.home.resolve() / "launchd.log"))

    def test_one_label_per_install(self):
        other = self.home.parent / "other"
        self.assertEqual(schedule.job(self.home / ".." / "home", self.agents).label, self.job.label)
        self.assertNotEqual(schedule.job(other, self.agents).label, self.job.label)

    def test_first_install_and_replacement(self):
        launchctl = FakeLaunchctl(bootout=[3])
        schedule.install(self.home, 8, 0, self.agents, launchctl)
        schedule.install(self.home, 9, 30, self.agents, launchctl)
        target = f"{self.domain}/{self.job.label}"
        self.assertEqual(launchctl.calls, [["bootout", target], ["bootstrap", self.domain, str(self.job.plist)],
                                           ["bootout", target], ["bootstrap", self.domain, str(self.job.plist)]])
        self.assertEqual(plistlib.loads(self.job.plist.read_bytes())["StartCalendarInterval"],
                         {"Hour": 9, "Minute": 30})
        self.assertEqual(self.leftovers(), [])

    def test_unexpected_bootout_failure_touches_nothing(self):
        schedule.install(self.home, 8, 0, self.agents, FakeLaunchctl(bootout=[3]))
        before = self.job.plist.read_bytes()
        launchctl = FakeLaunchctl(bootout=[5])
        with self.assertRaises(schedule.ScheduleError):
            schedule.install(self.home, 9, 0, self.agents, launchctl)
        self.assertEqual((self.job.plist.read_bytes(), [c[0] for c in launchctl.calls]), (before, ["bootout"]))

    def test_failed_first_install_leaves_no_plist(self):
        with self.assertRaises(schedule.ScheduleError) as caught:
            schedule.install(self.home, 8, 0, self.agents, FakeLaunchctl(bootout=[3], bootstrap=[5]))
        self.assertIn("bootstrap said 5", str(caught.exception))
        self.assertFalse(self.job.plist.exists())
        self.assertEqual(self.leftovers(), [])

    def test_failed_replacement_restores_the_previous_job(self):
        schedule.install(self.home, 8, 0, self.agents, FakeLaunchctl(bootout=[3]))
        before = self.job.plist.read_bytes()
        launchctl = FakeLaunchctl(bootstrap=[5])
        with self.assertRaises(schedule.ScheduleError):
            schedule.install(self.home, 9, 0, self.agents, launchctl)
        self.assertEqual(self.job.plist.read_bytes(), before)
        self.assertEqual([c[0] for c in launchctl.calls], ["bootout", "bootstrap", "bootstrap"])
        self.assertEqual(self.leftovers(), [])

    def test_remove_is_idempotent(self):
        schedule.install(self.home, 8, 0, self.agents, FakeLaunchctl(bootout=[3]))
        launchctl = FakeLaunchctl(bootout=[0, 3])
        schedule.remove(self.home, self.agents, launchctl)
        schedule.remove(self.home, self.agents, launchctl)
        self.assertFalse(self.job.plist.exists())
        with self.assertRaises(schedule.ScheduleError):
            schedule.remove(self.home, self.agents, FakeLaunchctl(bootout=[5]))

    def test_cron_line_elsewhere(self):
        line = schedule.cron_line(self.home, 7, 5)
        expected = f"5 7 * * *  JOBSCAN_HOME={self.home.resolve()} {sys.executable} -m jobscan run"
        self.assertTrue(line.startswith(expected), line)


class ScheduleCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "jobscan"
        (self.home / "profiles").mkdir(parents=True)
        (self.home / "profiles" / "pm.toml").write_text(PROFILE)
        (self.home / "config.toml").write_text(CONFIG)
        self.launchctl = FakeLaunchctl(bootout=[3])
        for patcher in [mock.patch.dict(os.environ, {"JOBSCAN_HOME": str(self.home), "HOME": str(self.root),
                                                     "OPENROUTER_API_KEY": "sk-or-v1-env"}),
                        mock.patch.object(schedule, "launchctl", self.launchctl)]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["schedule", *args])
        return code, out.getvalue().strip(), err.getvalue().strip()

    def key_file(self):
        key = self.home / "openrouter.key"
        key.write_text("sk-or-v1-file\n")
        key.chmod(0o600)

    def test_key_only_in_the_shell_is_refused(self):
        code, _, err = self.run_cli("--at", "08:00")
        self.assertEqual((code, self.launchctl.calls), (2, []))
        self.assertIn("openrouter.key", err)

    def test_schedule_and_unschedule(self):
        self.key_file()
        code, out, _ = self.run_cli("--at", "8:30")
        plist = self.root / "Library" / "LaunchAgents" / f"{schedule.job(self.home, Path('.')).label}.plist"
        self.assertEqual((code, out), (0, f"scheduled\t08:30\t{plist}"))
        self.assertTrue(plist.exists())
        self.assertEqual(self.run_cli("--off")[:2], (0, f"unscheduled\t{plist}"))
        self.assertFalse(plist.exists())

    def test_bad_times_are_usage_errors(self):
        for at in ["24:00", "8", "08:60", "08:5", "ab:cd"]:
            self.assertEqual(self.run_cli("--at", at)[0], 2, at)
        self.assertEqual(self.run_cli()[0], 2)

    def test_launchctl_refusal_is_exit_1(self):
        self.key_file()
        self.launchctl.answers["bootstrap"] = [5]
        code, _, err = self.run_cli("--at", "08:00")
        self.assertEqual(code, 1)
        self.assertIn("bootstrap said 5", err)

    def test_elsewhere_prints_the_cron_line(self):
        self.key_file()
        with mock.patch.object(sys, "platform", "linux"):
            code, out, _ = self.run_cli("--at", "07:00")
        self.assertEqual((code, self.launchctl.calls), (0, []))
        self.assertTrue(out.startswith("0 7 * * *  JOBSCAN_HOME="))


class NotifyTest(unittest.TestCase):
    def test_text_goes_as_arguments(self):
        calls = []
        with mock.patch.object(sys, "platform", "darwin"):
            notify.notify('2 new "postings" & end run', run=lambda argv, **kw: calls.append((argv, kw)))
        argv, kw = calls[0]
        self.assertEqual(argv, ["osascript", *notify.SCRIPT, '2 new "postings" & end run', "jobscan"])
        self.assertEqual((kw["timeout"], kw["check"]), (notify.TIMEOUT, True))

    def test_failures_are_logged_not_raised(self):
        for error in [subprocess.TimeoutExpired("osascript", 10), OSError("no osascript"),
                      subprocess.CalledProcessError(1, "osascript")]:
            def failing(argv, error=error, **kw):
                raise error
            with mock.patch.object(sys, "platform", "darwin"), self.assertLogs("jobscan", "WARNING"):
                notify.notify("x", run=failing)

    def test_silent_off_macos(self):
        with mock.patch.object(sys, "platform", "linux"):
            notify.notify("x", run=lambda *a, **k: self.fail("osascript called off macOS"))


if __name__ == "__main__":
    unittest.main()
