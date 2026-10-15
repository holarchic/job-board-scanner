"""The scriptable set-up commands, through the CLI in a temporary home with no terminal: `tags`, `check`,
`boards`, `preview`, and `init` and `email` by flags."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobscan import cli, init

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


if __name__ == "__main__":
    unittest.main()
