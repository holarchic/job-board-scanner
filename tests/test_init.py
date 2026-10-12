"""`jobscan init`: scripted terminal answers → files `run` can load, or nothing at all."""
import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobscan import cli, init
from jobscan.config import ConfigError, load_config, load_profile

TAG_COUNT = 5


def answers(name="my-pm", titles="Product Manager, Senior Product Manager", exclude="", locations="London, UK",
            where="the UK, working from London", tags=("y", "y", "n", "y", "y")):
    return [name, titles, exclude, locations, where, *tags]


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
        self.assertEqual(self.files(), ["config.toml", "openrouter.key", "profiles", "profiles/my-pm.toml"])
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
        typed = ["My PM", "my-pm", " , ", "Product Manager", "", "London", "", "the UK",
                 *["n"] * TAG_COUNT, "maybe", "y", *["n"] * (TAG_COUNT - 1)]
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

    def test_through_the_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"JOBSCAN_HOME": str(self.home)}), \
                mock.patch("builtins.input", side_effect=answers()), \
                mock.patch.object(init.getpass, "getpass", return_value="sk-or-v1-abc"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["init"]), 0)
            self.assertEqual(cli.main(["init"]), 2)
        self.assertIn("jobscan schedule --at 08:00", out.getvalue())
        self.assertIn("already set up", err.getvalue())


if __name__ == "__main__":
    unittest.main()
