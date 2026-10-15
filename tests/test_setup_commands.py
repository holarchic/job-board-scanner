"""The scriptable set-up commands, through the CLI in a temporary home with no terminal: `tags`, `check`,
`boards`, `preview`, and `init` and `email` by flags."""
import contextlib
import io
import json
import os
import smtplib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobscan import cli, init, tag
from jobscan.config import load_config, load_email, load_profile
from tests.fakes import patch_smtp

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
        patcher = mock.patch.dict(os.environ, {**environ, "JOBSCAN_HOME": str(self.home)}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cli(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(stdin)):
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
                     "(key: openrouter.key)", "Email: off", "Boards file: none", "OK: `jobscan run` can start."]:
            self.assertIn(line, out)
        self.assertNotIn("Warning", out)
        self.assertEqual(self.files(), before)

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


FLAGS = ("--name", "my-pm", "--title", "Product Manager", "--title", "Senior Product Manager",
         "--exclude-title", "Product Marketing", "--location", "London", "--location", "UK",
         "--eligible-where", "the UK, working from London")


class InitFlagsTest(CliHome):
    def test_flags_write_what_the_same_answers_typed_would(self):
        code, out, err = self.cli("init", *FLAGS, "--tag", "founding", "--tag", "tech_company", "--key-stdin",
                                  stdin="sk-or-v1-abc\n")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("jobscan schedule --at 08:00", out)
        typed_home = self.home.parent / "typed"
        typed = ["my-pm", "Product Manager, Senior Product Manager", "Product Marketing", "London, UK",
                 "the UK, working from London", "y", "n", "n", "n", "y", ""]
        with mock.patch("builtins.input", side_effect=typed), \
                mock.patch.object(init.getpass, "getpass", return_value="sk-or-v1-abc"), \
                contextlib.redirect_stdout(io.StringIO()):
            init.init(typed_home)
        for name in ["config.toml", "profiles/my-pm.toml", "openrouter.key"]:
            self.assertEqual((self.home / name).read_bytes(), (typed_home / name).read_bytes(), name)
        self.assertEqual(oct((self.home / "openrouter.key").stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.cli("check")[0], 0)

    def test_every_built_in_tag_by_default_and_the_environment_key_saved(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-v1-env"}):
            self.assertEqual(self.cli("init", *FLAGS)[0], 0)
        self.assertEqual((self.home / "openrouter.key").read_text(), "sk-or-v1-env\n")
        self.assertEqual(len(load_profile(self.home).tags), len(tag.builtin_names()))

    def test_a_title_holding_a_comma_stays_one_title(self):
        self.cli("init", *FLAGS, "--title", "Head of Sales, EMEA", "--key-stdin", stdin="sk-or-v1-abc\n")
        self.assertIn("Head of Sales, EMEA", load_profile(self.home).titles)

    def test_refused_values_write_nothing(self):
        cases = [
            ((), "no OpenRouter key", ""),
            (("--key-stdin",), "isn't an OpenRouter key", "sk or v1\n"),
            (("--name", "../escape", "--key-stdin"), "profile name '../escape'", "sk-or-v1-abc\n"),
            (("--name", "Bad", "--key-stdin"), "profile name 'Bad'", "sk-or-v1-abc\n"),
            (("--title", " PM", "--key-stdin"), "titles: ' PM'", "sk-or-v1-abc\n"),
            (("--location", "", "--key-stdin"), "locations: ''", "sk-or-v1-abc\n"),
            (("--tag", "founder", "--key-stdin"), "unknown tags ['founder']", "sk-or-v1-abc\n"),
            (("--tag", "founding", "--tag", "founding", "--key-stdin"), "tags", "sk-or-v1-abc\n"),
        ]
        for extra, message, stdin in cases:
            code, out, err = self.cli("init", *FLAGS, *extra, stdin=stdin)
            self.assertEqual((code, out), (2, ""), extra)
            self.assertIn(message, err, extra)
            self.assertFalse(self.home.exists(), extra)
            self.assertFalse((self.home.parent / "escape.toml").exists())

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
        code, out, err = self.email("--to", "you@gmail.com", "--password-stdin")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Sending a test email to you@gmail.com", out)
        email = load_email(self.home)
        self.assertEqual((email.to, email.sender, email.host, email.port, email.password),
                         ("you@gmail.com", "you@gmail.com", "smtp.gmail.com", 465, "abcdefghijklmnop"))
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
            (("--to", "you@gmail.com",), "add --password-stdin"),
            (("--to", "you@gmail.com", "--off"), "--off takes no other flags"),
        ]
        for flags, message in cases:
            code, _, err = self.email(*flags)
            self.assertEqual(code, 2, flags)
            self.assertIn(message, err, flags)
        code, _, err = self.email("--to", "you@gmail.com", "--password-stdin", password="\n")
        self.assertEqual(code, 2)
        self.assertIn("isn't an app password", err)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.home / "email.toml").exists())

    def test_a_refused_test_email_is_exit_1_and_saves_nothing(self):
        self.refuse["login"] = smtplib.SMTPAuthenticationError(535, b"bad credentials")
        code, _, err = self.email("--to", "you@gmail.com", "--password-stdin")
        self.assertEqual(code, 1)
        self.assertIn("nothing was saved", err)
        self.assertFalse((self.home / "email.toml").exists())

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

    def test_no_file_yet_says_where_add_writes(self):
        code, out, _ = self.cli("boards")
        self.assertEqual(code, 0)
        self.assertIn(f"No boards file yet ({self.file})", out)


if __name__ == "__main__":
    unittest.main()
