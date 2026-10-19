"""The scriptable set-up commands, through the CLI in a temporary home with no terminal: `tags`, `check`,
`boards`, `preview`, and `init` and `email` by flags."""
import contextlib
import fcntl
import io
import json
import os
import plistlib
import smtplib
import sys
import tempfile
import threading
import unittest
from datetime import time
from pathlib import Path
from unittest import mock

from jobscan import boards, cli, init, lock, schedule, store, tag
from jobscan.config import Requirement, load_config, load_email, load_profile
from jobscan.records import Board, Company, Posting
from tests.fakes import StatefulLaunchctl, patch_smtp

PROFILE = """titles = ["Account Executive"]
locations = ["London"]
eligible_where = "the UK"
tags = ["founding", "location_eligible"]

[custom_tags.climate]
definition = "The company's product reduces emissions."
"""


class CliHome(unittest.TestCase):
    """A temporary $JOBSCAN_HOME and a CLI caller capturing exit code, stdout and stderr."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        environ = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        self.launchd = schedule.Launchd(Path(tmp.name) / "LaunchAgents", StatefulLaunchctl())  # never the host's
        for patcher in (mock.patch.dict(os.environ, {**environ, "JOBSCAN_HOME": str(self.home)}, clear=True),
                        mock.patch.object(schedule, "launchd", return_value=self.launchd)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def cli(self, *argv, stdin="", terminal=False):
        out, err, typed = io.StringIO(), io.StringIO(), io.StringIO(stdin)
        typed.isatty = lambda: terminal
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch("sys.stdin", typed):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def write_profile(self, text=PROFILE, name="ae"):
        (self.home / "profiles").mkdir(parents=True, exist_ok=True)
        (self.home / "profiles" / f"{name}.toml").write_text(text)

    def write_install(self, key="sk-or-v1-abc"):
        self.write_profile()
        (self.home / "config.toml").write_text(init.CONFIG)
        if key is not None:
            (self.home / "openrouter.key").write_text(key + "\n")
            (self.home / "openrouter.key").chmod(0o600)

    def files(self):
        return sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*"))


class TagsTest(CliHome):
    def test_before_init_every_built_in_is_listed_unmarked(self):
        code, out, _ = self.cli("tags")
        self.assertEqual(code, 0)
        self.assertIn("    founding ", out)
        self.assertIn("[validated for the UK only]", out)
        self.assertNotIn("*", out.splitlines()[1])
        self.assertFalse(self.home.exists())  # nothing written

    def test_the_profiles_tags_are_marked_and_its_custom_ones_listed(self):
        self.write_profile()
        code, out, _ = self.cli("tags")
        self.assertEqual(code, 0)
        self.assertIn("  * founding ", out)
        self.assertIn("    segment ", out)
        self.assertIn("  * climate ", out)
        self.assertIn("The company's product reduces emissions.", out)

    def test_one_tags_definition_as_the_model_reads_it(self):
        self.write_profile()
        code, out, _ = self.cli("tags", "location_eligible")
        self.assertEqual(code, 0)
        self.assertIn("location_eligible (built-in tag, validated; in your profile)", out)
        self.assertIn("Someone who wants to work from the UK can do this job.", out)
        code, out, _ = self.cli("tags", "climate")
        self.assertIn("climate (custom tag, not validated; in your profile)", out)
        self.assertIn("Answers: yes, no, unknown", out)
        code, out, _ = self.cli("tags", "segment")
        self.assertIn("(built-in tag, not validated; not in your profile)", out)

    def test_before_init_the_place_is_a_placeholder(self):
        code, out, _ = self.cli("tags", "location_eligible")
        self.assertEqual(code, 0)
        self.assertIn("(built-in tag, validated for the UK only)", out)
        self.assertIn("work from <where you can work from>", out)

    def test_a_profile_elsewhere_is_not_validated_for_eligibility(self):
        self.write_profile(PROFILE.replace('"the UK"', '"Germany"'))
        self.assertIn("(built-in tag, not validated; in your profile)", self.cli("tags", "location_eligible")[1])

    def test_an_unknown_tag_or_a_broken_profile_is_exit_2(self):
        code, out, err = self.cli("tags", "nope")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("no tag named 'nope'", err)
        self.write_profile("titles = [")
        code, _, err = self.cli("tags")
        self.assertEqual(code, 2)
        self.assertIn("ae.toml", err)


class CheckTest(CliHome):
    def test_a_good_install_is_described_and_nothing_written(self):
        self.write_install()
        before = self.files()
        code, out, err = self.cli("check")
        self.assertEqual((code, err), (0, ""))
        for line in ["Profile: " + str(self.home / "profiles" / "ae.toml"), "  titles: Account Executive",
                     "  excluded titles: (none)", "  tags: founding, location_eligible; custom: climate",
                     "(key: openrouter.key)", "Email: off", "Boards file: none", "Schedule: none",
                     "  no require: no tag of yours hides a posting",
                     "  agency postings: left out (include_agencies = true keeps them)",
                     "OK: `jobscan run` can start."]:
            self.assertIn(line, out)
        self.assertNotIn("Warning", out)
        self.assertEqual(self.files(), before)

    def test_require_is_shown_and_one_the_tags_cant_answer_is_exit_2(self):
        self.write_install()
        self.write_profile(PROFILE.replace('tags = ["founding", "location_eligible"]',
                                           'tags = ["founding", "location_eligible"]\n'
                                           'require = { location_eligible = "yes", climate = "no" }'))
        code, out, _ = self.cli("check")
        self.assertEqual(code, 0)
        self.assertIn("  hidden when a tag definitely answers other than: location_eligible yes; climate no\n", out)
        self.write_profile(PROFILE.replace('tags = ["founding", "location_eligible"]',
                                           'tags = ["founding"]\nrequire = { location_eligible = "yes" }'))
        code, out, err = self.cli("check")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("require names `location_eligible`, which the profile doesn't tag", err)
        self.assertEqual(self.cli("preview")[0], 2)

    def test_the_daily_run_as_launchd_and_its_plist_have_it(self):
        self.write_install()
        plist = schedule.job(self.home, self.launchd.agents).plist
        schedule.install(self.home, time(7, 30), self.launchd.agents, self.launchd.run)
        out = self.cli("check")[1]
        self.assertIn("Schedule: every day at 07:30 (launchd)\nOK: `jobscan run` can start.", out)
        with mock.patch.object(sys, "executable", "/nowhere/python3"):  # a uv update moved the interpreter
            schedule.install(self.home, time(7, 30), self.launchd.agents, self.launchd.run)
        code, out, _ = self.cli("check")
        self.assertEqual(code, 0)  # a schedule is optional: a warning, never a failure
        self.assertIn("Warning: the daily run at 07:30 names a Python that no longer exists: "
                      "`jobscan schedule --at 07:30` sets it up again", out)
        schedule.install(self.home, time(7, 30), self.launchd.agents, self.launchd.run)
        self.launchd.run(["bootout", f"gui/{os.getuid()}/{schedule.job(self.home, self.launchd.agents).label}"])
        self.assertIn("Warning: the plist says 07:30, but launchd isn't running it", self.cli("check")[1])
        plist.write_bytes(plistlib.dumps({"StartCalendarInterval": {"Weekday": 1, "Hour": 7, "Minute": 0}}))
        self.assertIn("isn't a daily run as jobscan writes it, so jobscan leaves it alone", self.cli("check")[1])
        for program in ("bad", ["/usr/bin/true"], [sys.executable, "-m", "jobscan", "discover", "--notify"]):
            schedule.install(self.home, time(7, 30), self.launchd.agents, self.launchd.run)  # loaded, 07:30
            spec = plistlib.loads(plist.read_bytes())
            plist.write_bytes(plistlib.dumps({**spec, "ProgramArguments": program}))
            self.assertIn("isn't a daily run as jobscan writes it", self.cli("check")[1], program)
        schedule.install(self.home, time(7, 30), self.launchd.agents, self.launchd.run)
        plist.unlink()
        self.assertIn("Schedule: launchd holds a job with no plist: `jobscan schedule --off` removes it",
                      self.cli("check")[1])
        with mock.patch.object(schedule, "launchd", return_value=None):
            self.assertIn("Schedule: not checked here (a cron line", self.cli("check")[1])
        broken = schedule.Launchd(self.launchd.agents, mock.Mock(side_effect=FileNotFoundError("launchctl")))
        with mock.patch.object(schedule, "launchd", return_value=broken):
            code, out, _ = self.cli("check")
        self.assertEqual(code, 0)
        self.assertIn("Schedule: launchd couldn't be asked (FileNotFoundError: launchctl)", out)

    def test_a_key_only_in_the_environment_is_a_warning(self):
        self.write_install(key=None)
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-v1-env"}):
            code, out, _ = self.cli("check")
        self.assertEqual(code, 0)
        self.assertIn("(key: OPENROUTER_API_KEY)", out)
        self.assertIn("Warning: the key is only in OPENROUTER_API_KEY, which a scheduled run won't see", out)

    def test_no_key_anywhere_is_exit_2(self):
        self.write_install(key=None)
        code, out, err = self.cli("check")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("no OpenRouter key", err)

    def test_a_key_file_a_scheduled_run_cant_use_is_exit_2_even_with_the_environment_set(self):
        self.write_install()
        (self.home / "openrouter.key").chmod(0o644)
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-v1-env"}):
            code, _, err = self.cli("check")
        self.assertEqual(code, 2)
        self.assertIn("chmod 600", err)

    def test_a_malformed_key_in_the_environment_is_exit_2(self):
        self.write_install()
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or v1"}):
            code, _, err = self.cli("check")
        self.assertEqual(code, 2)
        self.assertIn("whitespace", err)

    def test_both_keys_are_named(self):
        self.write_install()
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-v1-env"}):
            self.assertIn("(key: OPENROUTER_API_KEY here, openrouter.key for scheduled runs)", self.cli("check")[1])

    def test_a_local_model_needs_no_key(self):
        self.write_install(key=None)
        (self.home / "config.toml").write_text('[model]\nbase_url = "http://localhost:11434/v1"\nslug = "qwen"\n')
        code, out, _ = self.cli("check")
        self.assertEqual(code, 0)
        self.assertIn("(key: none needed)", out)

    def test_each_broken_file_is_named(self):
        for name, text in [("profiles/ae.toml", "titles = ["), ("email.toml", "to = 1"), ("config.toml", "x = 1"),
                           ("boards.json", "{")]:
            self.write_install()
            if name == "boards.json":
                (self.home / "config.toml").write_text(f'boards_file = "boards.json"\n{init.CONFIG}')
            path = self.home / name
            path.write_text(text)
            path.chmod(0o600)
            code, out, err = self.cli("check")
            self.assertEqual((code, out), (2, ""), name)
            self.assertIn(Path(name).name, err)
            path.unlink()

    def test_a_tag_the_profile_cant_ask_is_exit_2(self):
        self.write_install()
        self.write_profile(PROFILE.replace('"founding"', '"founder"'))
        code, _, err = self.cli("check")
        self.assertEqual(code, 2)
        self.assertIn("unknown tags ['founder']", err)


FLAGS = ("--name", "doa-pm", "--title", "Product Manager", "--title", "Senior Product Manager",
         "--exclude-title", "Product Marketing", "--location", "London", "--location", "UK",
         "--eligible-where", "the UK, working from London")


class InitFlagsTest(CliHome):
    def test_flags_write_what_the_same_answers_typed_would(self):
        code, out, err = self.cli("init", *FLAGS, "--tag", "founding", "--tag", "tech_company", "--key-stdin",
                                  stdin="sk-or-v1-abc\n")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("jobscan schedule --at 08:00", out)
        typed_home = self.home.parent / "typed"
        typed = ["doa-pm", "Product Manager, Senior Product Manager", "Product Marketing", "London, UK",
                 "the UK, working from London", "y", "n", "n", "n", "y", ""]
        with mock.patch("builtins.input", side_effect=typed), \
                mock.patch.object(init.getpass, "getpass", return_value="sk-or-v1-abc"), \
                contextlib.redirect_stdout(io.StringIO()):
            init.init(typed_home)
        for name in ["config.toml", "profiles/doa-pm.toml", "openrouter.key"]:
            self.assertEqual((self.home / name).read_bytes(), (typed_home / name).read_bytes(), name)
        self.assertEqual(oct((self.home / "openrouter.key").stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.cli("check")[0], 0)

    def test_every_built_in_tag_by_default_and_the_environment_key_saved(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-v1-env"}):
            self.assertEqual(self.cli("init", *FLAGS)[0], 0)
        self.assertEqual((self.home / "openrouter.key").read_text(), "sk-or-v1-env\n")
        self.assertEqual(len(load_profile(self.home).tags), len(tag.builtin_names()))

    def test_require_by_flags_reaches_the_profile(self):
        code, _, err = self.cli("init", *FLAGS, "--require", "language_required=no", "--require",
                                "location_eligible=yes", "--key-stdin", stdin="sk-or-v1-abc\n")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(load_profile(self.home).require,
                         (Requirement("language_required", "no"), Requirement("location_eligible", "yes")))
        _, out, _ = self.cli("check")
        self.assertIn("hidden when a tag definitely answers other than: language_required no; location_eligible yes",
                      out)

    def test_require_alone_is_flags_and_asks_nothing(self):
        with mock.patch("builtins.input") as typed:
            code, _, err = self.cli("init", "--require", "language_required=no", terminal=True)
        typed.assert_not_called()
        self.assertEqual(code, 2)
        self.assertIn("missing --name, --title, --location, --eligible-where", err)
        self.assertFalse(self.home.exists())

    def test_a_title_holding_a_comma_stays_one_title(self):
        self.cli("init", *FLAGS, "--title", "Head of Sales, EMEA", "--key-stdin", stdin="sk-or-v1-abc\n")
        self.assertIn("Head of Sales, EMEA", load_profile(self.home).titles)

    def test_refused_values_write_nothing(self):
        cases = [
            ((), "no OpenRouter key", ""),
            (("--key-stdin",), "isn't an OpenRouter key", "sk or v1\n"),
            (("--name", "../escape", "--key-stdin"), "profile name '../escape'", "sk-or-v1-abc\n"),
            (("--name", "Doa", "--key-stdin"), "profile name 'Doa'", "sk-or-v1-abc\n"),
            (("--title", " PM", "--key-stdin"), "titles: ' PM'", "sk-or-v1-abc\n"),
            (("--location", "", "--key-stdin"), "locations: ''", "sk-or-v1-abc\n"),
            (("--tag", "founder", "--key-stdin"), "unknown tags ['founder']", "sk-or-v1-abc\n"),
            (("--tag", "founding", "--tag", "founding", "--key-stdin"), "tags", "sk-or-v1-abc\n"),
            (("--require", "language_required", "--key-stdin"), "write it TAG=VALUE", "sk-or-v1-abc\n"),
            (("--require", "=no", "--key-stdin"), "write it TAG=VALUE", "sk-or-v1-abc\n"),
            (("--require", "founding=yes", "--require", "founding=no", "--key-stdin"), "names `founding` twice",
             "sk-or-v1-abc\n"),
            (("--tag", "founding", "--require", "language_required=no", "--key-stdin"),
             "require names `language_required`, which the profile doesn't tag", "sk-or-v1-abc\n"),
            (("--require", "founding=maybe", "--key-stdin"), "isn't an answer it can require", "sk-or-v1-abc\n"),
        ]
        for extra, message, stdin in cases:
            code, out, err = self.cli("init", *FLAGS, *extra, stdin=stdin)
            self.assertEqual((code, out), (2, ""), extra)
            self.assertIn(message, err, extra)
            self.assertFalse(self.home.exists(), extra)
            self.assertFalse((self.home.parent / "escape.toml").exists())

    def test_an_empty_flag_is_a_flag_given_and_refused(self):
        for flags in [("--name", ""),
                      ("--name", "ae", "--title", "PM", "--location", "London", "--eligible-where", "")]:
            with mock.patch("builtins.input") as typed:
                code, _, _ = self.cli("init", *flags, terminal=True)
            typed.assert_not_called()
            self.assertEqual(code, 2, flags)
            self.assertFalse(self.home.exists())

    def test_missing_flags_are_named(self):
        code, _, err = self.cli("init", "--name", "ae", "--key-stdin", stdin="sk-or-v1-abc\n")
        self.assertEqual(code, 2)
        self.assertIn("missing --title, --location, --eligible-where", err)
        self.assertFalse(self.home.exists())

    def test_an_existing_install_is_refused(self):
        self.write_install()
        code, _, err = self.cli("init", *FLAGS, "--key-stdin", stdin="sk-or-v1-abc\n")
        self.assertEqual(code, 2)
        self.assertIn("already set up", err)
        self.assertEqual(self.files(), ["config.toml", "openrouter.key", "profiles", "profiles/ae.toml"])

    def test_no_flags_and_no_terminal_asks_nothing(self):
        with mock.patch("builtins.input") as typed:
            code, _, err = self.cli("init")
        typed.assert_not_called()
        self.assertEqual(code, 2)
        self.assertIn("no terminal to ask in: give the flags instead", err)


class EmailFlagsTest(CliHome):
    def setUp(self):
        super().setUp()
        self.calls, self.refuse = [], {}
        for patcher in patch_smtp(self.calls, self.refuse):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.write_install()

    def email(self, *flags, password="abcd efgh ijkl mnop\n"):
        return self.cli("email", *flags, stdin=password)

    def test_gmail_needs_only_the_address_and_the_password(self):
        code, out, err = self.email("--to", "doa@gmail.com", "--password-stdin")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Sending a test email to doa@gmail.com", out)
        email = load_email(self.home)
        self.assertEqual((email.to, email.sender, email.host, email.port, email.password),
                         ("doa@gmail.com", "doa@gmail.com", "smtp.gmail.com", 465, "abcdefghijklmnop"))
        self.assertEqual([name for name, _ in self.calls], ["SMTP_SSL", "login", "send", "quit"])

    def test_another_sender_and_provider(self):
        code, _, _ = self.email("--to", "me@work.example", "--from", "me@fastmail.com", "--host", "smtp.fastmail.com",
                                "--port", "587", "--password-stdin", password="pass word\n")
        self.assertEqual(code, 0)
        email = load_email(self.home)
        self.assertEqual((email.sender, email.host, email.port, email.password),
                         ("me@fastmail.com", "smtp.fastmail.com", 587, "pass word"))

    def test_refused_flags_send_and_save_nothing(self):
        cases = [
            (("--to", "nobody", "--password-stdin"), "--to: not an email address"),
            (("--to", "me@outlook.com", "--password-stdin"), "OAuth"),
            (("--to", "me@fastmail.com", "--password-stdin"), "no preset mail server for me@fastmail.com"),
            (("--to", "me@fastmail.com", "--host", "smtp.fastmail.com", "--password-stdin"), "together"),
            (("--to", "me@fastmail.com", "--host", "a b", "--port", "465", "--password-stdin"), "server name"),
            (("--to", "doa@gmail.com",), "add --password-stdin"),
            (("--to", "doa@gmail.com", "--off"), "--off takes no other flags"),
            (("--to", "doa@gmail.com", "--from", "", "--password-stdin"), "--from: not an email address"),
            (("--off", "--port", "0"), "--off takes no other flags"),
            (("--to", "me@fastmail.com", "--host", "smtp.fastmail.com", "--port", "0", "--password-stdin"),
             "port number"),
        ]
        for flags, message in cases:
            code, _, err = self.email(*flags)
            self.assertEqual(code, 2, flags)
            self.assertIn(message, err, flags)
        code, _, err = self.email("--to", "doa@gmail.com", "--password-stdin", password="\n")
        self.assertEqual(code, 2)
        self.assertIn("isn't an app password", err)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.home / "email.toml").exists())

    def test_a_refused_test_email_is_exit_1_and_saves_nothing(self):
        self.refuse["login"] = smtplib.SMTPAuthenticationError(535, b"bad credentials")
        code, _, err = self.email("--to", "doa@gmail.com", "--password-stdin")
        self.assertEqual(code, 1)
        self.assertIn("nothing was saved", err)
        self.assertFalse((self.home / "email.toml").exists())

    def test_a_port_0_alone_is_a_flag_and_asks_nothing(self):
        with mock.patch("builtins.input") as typed:
            code, _, err = self.cli("email", "--port", "0", terminal=True)
        typed.assert_not_called()
        self.assertEqual(code, 2)
        self.assertIn("add --password-stdin", err)

    def test_no_flags_and_no_terminal_asks_nothing(self):
        with mock.patch("builtins.input") as typed:
            code, _, err = self.cli("email")
        typed.assert_not_called()
        self.assertEqual(code, 2)
        self.assertIn("no terminal to ask in", err)


GREENHOUSE = "https://job-boards.greenhouse.io/acme"
ASHBY = "https://jobs.ashbyhq.com/beta/1a2b"


class BoardsTest(CliHome):
    def setUp(self):
        super().setUp()
        self.write_install()
        self.file = self.home / "boards.json"

    def test_added_boards_land_in_the_default_file_that_runs_read(self):
        self.assertIsNone(load_config(self.home).boards_file)
        code, out, err = self.cli("boards", "add", GREENHOUSE, ASHBY)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("added greenhouse:acme", out)
        self.assertEqual(json.loads(self.file.read_text()),
                         {"companies": [{"board_url": GREENHOUSE}, {"board_url": ASHBY}]})
        self.assertEqual(load_config(self.home).boards_file, self.file)
        code, out, _ = self.cli("boards")
        self.assertIn(f"(name looked up)  {GREENHOUSE}  [Greenhouse]", out)
        self.assertIn("Boards file: " + str(self.file) + " (2 boards)", self.cli("check")[1])

    def test_a_listed_board_is_skipped_and_a_company_kept(self):
        self.cli("boards", "add", ASHBY, "--company", "Beta AI")
        code, out, _ = self.cli("boards", "add", "https://jobs.ashbyhq.com/BETA", GREENHOUSE)
        self.assertEqual(code, 0)
        self.assertIn("already listed ashby:beta", out)
        self.assertEqual(json.loads(self.file.read_text())["companies"],
                         [{"company": "Beta AI", "board_url": ASHBY}, {"board_url": GREENHOUSE}])

    def test_the_configured_file_keeps_what_else_it_holds(self):
        other = self.home / "mine.json"
        other.write_text(json.dumps({"note": "mine", "companies": [{"company": "X", "board_url": "https://x.example"}]}))
        (self.home / "config.toml").write_text(f'boards_file = "mine.json"\n{init.CONFIG}')
        self.assertEqual(self.cli("boards", "add", GREENHOUSE)[0], 0)
        data = json.loads(other.read_text())
        self.assertEqual(data["note"], "mine")
        self.assertEqual(len(data["companies"]), 2)
        self.assertFalse(self.file.exists())
        self.assertIn("not recognised: a run skips it", self.cli("boards")[1])

    def test_one_unknown_url_adds_nothing(self):
        self.cli("boards", "add", ASHBY)
        before = self.file.read_bytes()
        code, out, err = self.cli("boards", "add", GREENHOUSE, "https://example.com/careers")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("https://example.com/careers: not a job board URL jobscan knows; nothing was added", err)
        self.assertEqual(self.file.read_bytes(), before)

    def test_a_broken_file_or_a_company_for_two_is_refused_untouched(self):
        self.file.write_text("{")
        code, _, err = self.cli("boards", "add", GREENHOUSE)
        self.assertEqual(code, 2)
        self.assertIn("not a readable boards file", err)
        self.assertEqual(self.file.read_text(), "{")
        self.file.unlink()
        code, _, err = self.cli("boards", "add", GREENHOUSE, ASHBY, "--company", "Acme")
        self.assertEqual(code, 2)
        self.assertIn("--company names one board", err)
        self.assertFalse(self.file.exists())
        self.assertEqual([p.name for p in self.home.iterdir() if p.name.startswith(".boards-")], [])

    def test_text_that_cant_be_written_is_refused_and_nothing_left(self):
        self.file.write_text('{"note": "\\ud800", "companies": []}')
        code, _, err = self.cli("boards", "add", GREENHOUSE)
        self.assertEqual(code, 2)
        self.assertIn("can't be written back", err)
        code, _, err = self.cli("boards", "add", GREENHOUSE, "--company", "\ud800")
        self.assertEqual(code, 2)
        self.assertIn("--company", err)
        self.assertEqual(self.file.read_text(), '{"note": "\\ud800", "companies": []}')
        self.assertEqual([p.name for p in self.home.iterdir() if p.name.startswith(".boards-")], [])

    def test_a_failed_write_and_cleanup_names_the_temporary_file(self):
        with mock.patch("jobscan.boards.os.replace", side_effect=PermissionError(1, "denied")), \
                mock.patch.object(Path, "unlink", side_effect=PermissionError(1, "denied")):
            code, _, err = self.cli("boards", "add", GREENHOUSE)
        self.assertEqual(code, 2)
        self.assertRegex(err, r"could not write .*boards.json \(PermissionError\); it is unchanged; delete .*\.boards-")

    def test_an_add_waits_for_another_to_finish_writing(self):
        with (self.home / ".boards.json.lock").open("w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)  # another `boards add` mid-write
            done = threading.Event()
            writer = threading.Thread(target=lambda: (boards.add(self.file, [GREENHOUSE], None), done.set()))
            writer.start()
            self.assertFalse(done.wait(0.3))
            self.file.write_text(json.dumps({"companies": [{"board_url": ASHBY}]}))  # what the other wrote
        writer.join(5)
        self.assertEqual(json.loads(self.file.read_text())["companies"],
                         [{"board_url": ASHBY}, {"board_url": GREENHOUSE}])

    def test_a_filesystem_without_locks_is_a_config_error(self):
        with mock.patch("jobscan.boards.fcntl.flock", side_effect=OSError(77, "No locks available")):
            code, _, err = self.cli("boards", "add", GREENHOUSE)
        self.assertEqual(code, 2)
        self.assertIn("could not lock", err)

    def test_no_file_yet_says_where_add_writes(self):
        code, out, _ = self.cli("boards")
        self.assertEqual(code, 0)
        self.assertIn(f"No boards file yet ({self.file})", out)


def posting(board, key, title, location="London, UK"):
    return Posting(key=f"{board}:{key}", ats=board.split(":")[0], board=board, company=board.split(":")[1],
                   title=title, location=location, url=f"https://example.com/{key}", description="")


class PreviewTest(CliHome):
    def setUp(self):
        super().setUp()
        self.write_install()
        self.write_profile(PROFILE.replace('titles = ["Account Executive"]',
                                           'titles = ["Account Executive"]\nexclude_titles = ["Assistant"]'))

    def seed(self):
        conn = store.open_db(self.home)
        try:
            for board, postings, day in [
                ("ashby:acme", [posting("ashby:acme", "1", "Account Executive"),
                                posting("ashby:acme", "2", "Account Executive Assistant"),
                                posting("ashby:acme", "3", "Engineer"),
                                posting("ashby:acme", "4", "Account Executive", "Paris")], "01"),
                ("greenhouse:beta", [posting("greenhouse:beta", "9", "Senior Account Executive")], "03"),
                ("workday:wd.wd3.ext", [posting("workday:wd.wd3.ext", "R1", "Account Executive"),
                                        posting("workday:wd.wd3.ext", "R2", "Account Executive")], "02"),
                ("workday:wd.wd3.uni", [posting("workday:wd.wd3.uni", "R1", "Account Executive"),
                                        posting("workday:wd.wd3.uni", "R2", "Account Executive")], "02"),
            ]:
                store.apply_fetch(conn, board, tuple(postings), f"2026-10-{day}T00:00:00+00:00")
            store.apply_fetch(conn, "greenhouse:gone", (posting("greenhouse:gone", "7", "Account Executive"),),
                              "2026-10-04T00:00:00+00:00")  # a board the next run doesn't read
            store.replace_discovered(conn, [Board(ats, slug, slug, "") for ats, slug in [
                ("ashby", "acme"), ("greenhouse", "beta"), ("workday", "wd.wd3.ext"), ("workday", "wd.wd3.uni")]],
                "2026-10-01T00:00:00+00:00")
            store.deliver(conn, "ae", ["workday:wd.wd3.ext:R2"], self.home / "d.md")
            store.save_companies(conn, {"greenhouse:beta": Company(name="Beta Inc", website=None)}, "2026-10-03")
        finally:
            conn.close()

    def test_new_jobs_are_counted_as_the_run_counts_them(self):
        self.seed()
        (self.home / "boards.json").write_text(json.dumps({"companies": [
            {"company": "Acme AI", "board_url": "https://jobs.ashbyhq.com/acme"}]}))
        code, out, err = self.cli("preview")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("ae, against the 9 open postings stored for the boards the next run reads:", out)
        self.assertIn("matching your titles and locations: 6 postings", out)  # 1, 9, R1 and R2 on both sites
        self.assertIn("new jobs the next run would tag: 3 (about $0.00)", out)  # 1, 9, R1 once
        self.assertIn("already sent to you: 2", out)  # R2 on both sites
        self.assertIn("left out by your excluded titles: 1", out)
        lines = out.splitlines()
        new = lines[lines.index("New jobs, newest first:") + 1:lines.index("Left out by your excluded titles:")]
        self.assertEqual(new, ["  Beta Inc — Senior Account Executive — London, UK",
                               "  wd.wd3.ext — Account Executive — London, UK",
                               "  Acme AI — Account Executive — London, UK"])
        self.assertIn("  Acme AI — Account Executive Assistant — London, UK", out)
        self.assertIn("Left out at the digest's end, tagged as usual and not counted here: agency postings.\n", out)
        self.write_profile(PROFILE.replace('tags = ["founding", "location_eligible"]',
                                           'tags = ["founding", "location_eligible"]\n'
                                           'require = { location_eligible = "yes" }'))
        _, out, _ = self.cli("preview")
        self.assertIn("Left out at the digest's end, tagged as usual and not counted here: postings a tag definitely "
                      "answers other than location_eligible yes and agency postings.\n", out)
        self.write_profile(PROFILE.replace('tags = ["founding", "location_eligible"]',
                                           'tags = ["founding", "location_eligible"]\ninclude_agencies = true'))
        _, out, _ = self.cli("preview")
        self.assertNotIn("Left out at the digest's end", out)
        _, out, _ = self.cli("check")
        self.assertIn("  agency postings: kept (include_agencies = true)", out)

    def test_before_any_run_it_says_so_and_creates_nothing(self):
        before = self.files()
        code, out, _ = self.cli("preview")
        self.assertEqual(code, 0)
        self.assertIn("No postings yet: `jobscan run` fetches them", out)
        self.assertEqual(self.files(), before)

    def test_a_run_holding_the_lock_is_exit_1_and_the_database_unopened(self):
        self.seed()
        with lock.run_lock(self.home), mock.patch.object(store, "open_db", side_effect=AssertionError) as opened:
            code, out, err = self.cli("preview")
        opened.assert_not_called()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("a run is using the database", err)

    def test_a_filesystem_without_locks_is_exit_2(self):
        self.seed()
        with mock.patch("jobscan.lock.fcntl.flock", side_effect=OSError(77, "No locks available")):
            code, _, err = self.cli("preview")
        self.assertEqual(code, 2)
        self.assertIn("can't be locked", err)

    def test_no_profile_is_exit_2(self):
        (self.home / "profiles" / "ae.toml").unlink()
        self.assertEqual(self.cli("preview")[0], 2)


if __name__ == "__main__":
    unittest.main()
