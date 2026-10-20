"""`jobscan init`: scripted terminal answers → files `run` can load, or nothing at all."""
import contextlib
import io
import os
import smtplib
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobscan import cli, init, mail, tag
from jobscan.config import ConfigError, EmailConfig, load_config, load_email, load_profile
from tests.fakes import patch_smtp

TAG_COUNT = len(tag.builtins())
LATER_TAGS = TAG_COUNT - 5  # built-ins after the first five, which these answers leave unticked


def answers(name="doa-pm", titles="Product Manager, Senior Product Manager", exclude="", locations="London, UK",
            where="the UK, working from London", tags=("y", "y", "n", "y", "y", *["n"] * LATER_TAGS), email=("",)):
    """`email`: the answers to the email questions; Enter alone says no to email."""
    return [name, titles, exclude, locations, where, *tags, *email]


class InitTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "jobscan-home"

    def run_init(self, typed, keys=("sk-or-v1-abc",), env_key=None):
        environ = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        if env_key:
            environ["OPENROUTER_API_KEY"] = env_key
        with mock.patch("builtins.input", side_effect=typed), mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(init.getpass, "getpass", side_effect=keys), \
                contextlib.redirect_stdout(io.StringIO()):
            return init.init(self.home)

    def files(self):
        return sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*")) if self.home.exists() else []

    def test_answers_become_a_loadable_install(self):
        setup = self.run_init(answers())
        self.assertEqual(self.files(), ["config.toml", "openrouter.key", "profiles", "profiles/doa-pm.toml"])
        profile = load_profile(self.home)
        self.assertEqual(profile.titles, ("Product Manager", "Senior Product Manager"))
        self.assertEqual(profile.exclude_titles,
                         ("Product Marketing", "Project Manager", "Program Manager", "Programme Manager"))
        self.assertEqual(profile.tags, ("founding", "location_eligible", "language_required", "tech_company"))
        self.assertEqual(stat.S_IMODE(setup.key.stat().st_mode), 0o600)
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            self.assertEqual(load_config(self.home).api_key, "sk-or-v1-abc")
        self.assertIn("# [custom_tags.b2b]", setup.profile.read_text())

    def test_enter_saves_the_environment_key_for_scheduled_runs(self):
        setup = self.run_init(answers(exclude="-"), keys=("",), env_key="sk-or-v1-env")
        self.assertEqual(setup.key.read_text(), "sk-or-v1-env\n")
        self.assertEqual(load_profile(self.home).exclude_titles, ())

    def test_a_key_is_required(self):
        setup = self.run_init(answers(), keys=("", "sk or v1", "sk-or-v1-abc"))
        self.assertEqual(setup.key.read_text(), "sk-or-v1-abc\n")

    def test_awkward_text_round_trips(self):
        titles = 'Lead "Growth" PM, Head\\Product, PM 😀, Café\x7fx, Tab\there'
        self.run_init(answers(titles=titles, where='the "UK"\\ 🇬🇧'))
        profile = load_profile(self.home)
        self.assertEqual(profile.titles, ('Lead "Growth" PM', "Head\\Product", "PM 😀", "Café\x7fx", "Tab\there"))
        self.assertEqual(profile.eligible_where, 'the "UK"\\ 🇬🇧')

    def test_invalid_answers_are_asked_again(self):
        typed = ["Doa PM", "doa-pm", " , ", "Product Manager", "", "London", "", "the UK",
                 *["n"] * TAG_COUNT, "maybe", "y", *["n"] * (TAG_COUNT - 1), "maybe", "n"]
        self.run_init(typed)
        self.assertEqual(load_profile(self.home).tags, ("founding",))

    def test_existing_install_is_refused_before_asking(self):
        (self.home / "profiles").mkdir(parents=True)
        (self.home / "profiles" / "ae.toml").write_text("titles = []\n")
        with mock.patch("builtins.input") as typed, self.assertRaises(ConfigError):
            init.init(self.home)
        typed.assert_not_called()
        self.assertEqual(self.files(), ["profiles", "profiles/ae.toml"])

    def test_cancelling_writes_nothing(self):
        for interrupt in (KeyboardInterrupt, EOFError):
            with self.assertRaises(init.InitCancelled):
                self.run_init([*answers()[:3], interrupt])
            self.assertEqual(self.files(), [])

    def test_a_failed_write_keeps_nothing(self):
        real_open = os.open

        def failing_open(path, flags, mode=0o777, **kwargs):
            if str(path).endswith("openrouter.key"):
                raise OSError(28, "No space left on device")
            return real_open(path, flags, mode, **kwargs)

        with mock.patch.object(init.os, "open", failing_open), self.assertRaises(ConfigError):
            self.run_init(answers())
        self.assertFalse(self.home.exists())

    def test_a_failed_cleanup_names_what_is_left(self):
        real_open, real_unlink = os.open, Path.unlink

        def failing_open(path, flags, mode=0o777, **kwargs):
            if str(path).endswith("openrouter.key"):
                raise OSError(28, "No space left on device")
            return real_open(path, flags, mode, **kwargs)

        def stuck_unlink(path, missing_ok=False):
            if path.name == "config.toml":
                raise PermissionError(1, "Operation not permitted")
            return real_unlink(path, missing_ok)

        with mock.patch.object(init.os, "open", failing_open), mock.patch.object(Path, "unlink", stuck_unlink), \
                self.assertRaises(ConfigError) as caught:
            self.run_init(answers())
        self.assertIn(f"delete {self.home / 'config.toml'}", str(caught.exception))
        self.assertNotIn("nothing was kept", str(caught.exception))

    def test_through_the_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"JOBSCAN_HOME": str(self.home)}), \
                mock.patch("builtins.input", side_effect=answers()), \
                mock.patch.object(init.getpass, "getpass", return_value="sk-or-v1-abc"), \
                mock.patch("sys.stdin.isatty", return_value=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["init"]), 0)
            self.assertEqual(cli.main(["init"]), 2)
        self.assertIn("jobscan schedule --at 08:00", out.getvalue())
        self.assertIn("already set up", err.getvalue())


class EmailSetupTest(unittest.TestCase):
    """The email questions, from `init` and `jobscan email`: settings are saved only after a test email is accepted."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "jobscan-home"
        self.calls, self.refuse = [], {}
        for patcher in patch_smtp(self.calls, self.refuse):
            patcher.start()
            self.addCleanup(patcher.stop)

    def installed(self):
        (self.home / "profiles").mkdir(parents=True)
        (self.home / "profiles" / "doa-pm.toml").write_text(
            'titles = ["PM"]\nlocations = ["London"]\neligible_where = "the UK"\ntags = ["founding"]\n')

    def email_cli(self, typed, passwords=("abcd efgh ijkl mnop",), *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"JOBSCAN_HOME": str(self.home)}), \
                mock.patch("builtins.input", side_effect=typed), \
                mock.patch.object(init.getpass, "getpass", side_effect=passwords), \
                mock.patch("sys.stdin.isatty", return_value=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["email", *args])
        return code, out.getvalue(), err.getvalue()

    def sent(self):
        return [args["message"] for name, args in self.calls if name == "send"]

    def test_init_with_gmail_tests_then_saves_it(self):
        typed = answers(email=("y", "doa@gmail.com", ""))
        with mock.patch("builtins.input", side_effect=typed), \
                mock.patch.object(init.getpass, "getpass", side_effect=["sk-or-v1-abc", "abcd efgh ijkl mnop"]), \
                contextlib.redirect_stdout(io.StringIO()):
            setup = init.init(self.home)
        self.assertEqual((setup.email, setup.email_error), (self.home / "email.toml", None))
        self.assertEqual(stat.S_IMODE(setup.email.stat().st_mode), 0o600)
        self.assertEqual(load_email(self.home), EmailConfig(to="doa@gmail.com", sender="doa@gmail.com",
                                                            host="smtp.gmail.com", port=465,
                                                            password="abcdefghijklmnop"))
        (message,) = self.sent()
        self.assertEqual((message["To"], message["Subject"]), ("doa@gmail.com", "jobscan: email set up for doa-pm"))

    def test_init_whose_test_email_fails_is_written_without_email(self):
        self.refuse["login"] = smtplib.SMTPAuthenticationError(535, b"5.7.8 not accepted")
        with mock.patch("builtins.input", side_effect=answers(email=("y", "doa@gmail.com", ""))), \
                mock.patch.object(init.getpass, "getpass", side_effect=["sk-or-v1-abc", "wrong"]), \
                contextlib.redirect_stdout(io.StringIO()):
            setup = init.init(self.home)
        self.assertIsNone(setup.email)
        self.assertIsInstance(setup.email_error, mail.MailError)
        self.assertIn("SMTPAuthenticationError", str(setup.email_error))
        self.assertFalse((self.home / "email.toml").exists())
        self.assertEqual(load_profile(self.home).name, "doa-pm")

    def test_init_refuses_a_home_with_email_set_up(self):
        self.home.mkdir()
        (self.home / "email.toml").write_text("")
        with mock.patch("builtins.input") as typed, self.assertRaises(ConfigError):
            init.init(self.home)
        typed.assert_not_called()

    def test_icloud_starts_tls_and_keeps_the_password_as_typed(self):
        self.installed()
        code, out, _ = self.email_cli(["doa@icloud.com", ""], ("abcd-efgh-ijkl-mnop",))
        self.assertEqual(code, 0)
        self.assertIn("saved", out)
        self.assertEqual([name for name, _ in self.calls], ["SMTP", "starttls", "login", "send", "quit"])
        email = load_email(self.home)
        self.assertEqual((email.host, email.port, email.password), ("smtp.mail.me.com", 587, "abcd-efgh-ijkl-mnop"))

    def test_outlook_is_refused_and_another_account_accepted(self):
        self.installed()
        code, out, _ = self.email_cli(["doa@outlook.com", "", "doa@gmail.com"])
        self.assertEqual(code, 0)
        self.assertIn("OAuth", out)
        email = load_email(self.home)
        self.assertEqual((email.to, email.sender, email.host), ("doa@outlook.com", "doa@gmail.com", "smtp.gmail.com"))

    def test_no_asks_which_account_sends_it(self):
        self.installed()
        code, out, _ = self.email_cli(["david@example.com", "yes", "n", "me@gmail.com"])
        self.assertEqual(code, 0)
        self.assertIn("Not a valid answer", out)
        email = load_email(self.home)
        self.assertEqual((email.to, email.sender, email.host), ("david@example.com", "me@gmail.com", "smtp.gmail.com"))
        (message,) = self.sent()
        self.assertEqual((message["From"], message["To"]), ("me@gmail.com", "david@example.com"))

    def test_another_provider_is_asked_for_its_server(self):
        self.installed()
        code, _, _ = self.email_cli(["doa@fastmail.com", "", "", "²", "9" * 5000, "465"], ("pass word",))
        self.assertEqual(code, 0)
        email = load_email(self.home)
        self.assertEqual((email.host, email.port, email.password), ("smtp.fastmail.com", 465, "pass word"))

    def test_a_failed_test_saves_nothing_and_keeps_the_previous_settings(self):
        self.installed()
        self.email_cli(["doa@gmail.com", ""])
        before = (self.home / "email.toml").read_text()
        self.refuse["SMTP_SSL"] = TimeoutError("timed out")
        code, _, err = self.email_cli(["new@gmail.com", ""])
        self.assertEqual(code, 1)
        self.assertIn("test email wasn't sent (TimeoutError: timed out); nothing was saved", err)
        self.assertEqual((self.home / "email.toml").read_text(), before)

    def test_success_replaces_the_previous_settings(self):
        self.installed()
        self.email_cli(["doa@gmail.com", ""])
        self.assertEqual(self.email_cli(["new@icloud.com", ""], ("app-pass",))[0], 0)
        self.assertEqual(load_email(self.home).to, "new@icloud.com")
        self.assertEqual(stat.S_IMODE((self.home / "email.toml").stat().st_mode), 0o600)

    def test_a_failed_save_keeps_the_previous_file_and_no_temporary(self):
        self.installed()
        self.email_cli(["doa@gmail.com", ""])
        with mock.patch.object(init.os, "replace", side_effect=OSError(28, "No space left on device")):
            code, _, err = self.email_cli(["new@gmail.com", ""])
        self.assertEqual(code, 2)
        self.assertIn("the previous settings are kept", err)
        self.assertEqual(load_email(self.home).to, "doa@gmail.com")
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["email.toml", "profiles"])

    def test_a_temporary_file_left_behind_is_named(self):
        self.installed()
        real_unlink = os.unlink

        def stuck_unlink(path, *args, **kwargs):
            if ".email-" in str(path):
                raise PermissionError(1, "Operation not permitted")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(init.os, "replace", side_effect=OSError(28, "No space left on device")), \
                mock.patch.object(Path, "unlink", lambda path, missing_ok=False: stuck_unlink(path)):
            code, _, err = self.email_cli(["new@gmail.com", ""])
        self.assertEqual(code, 2)
        self.assertRegex(err, r"delete .*\.email-.*\.tmp, which holds the password")

    def test_off_removes_even_a_broken_file_and_is_idempotent(self):
        self.installed()
        (self.home / "email.toml").write_text("not toml [")
        self.assertEqual(self.email_cli([], (), "--off")[:2], (0, f"removed\t{self.home / 'email.toml'}\n"))
        self.assertEqual(self.email_cli([], (), "--off")[:2], (0, "jobscan: no email set up\n"))

    def test_no_install_no_questions(self):
        code, _, err = self.email_cli([])
        self.assertEqual(code, 2)
        self.assertIn("profiles", err)


if __name__ == "__main__":
    unittest.main()
