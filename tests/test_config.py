import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobscan.config import ConfigError, CustomTag, load_config, load_profile

OPENROUTER = 'boards_file = "b.json"\n[model]\nbase_url = "https://openrouter.ai/api/v1"\nslug = "x/y"\n'


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def load(self, text, key="sk-or-v1-abc"):
        (self.home / "config.toml").write_text(text)
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": key}):
            return load_config(self.home)

    def test_openrouter_needs_pinned_provider_and_a_clean_key(self):
        pinned = OPENROUTER + 'provider = "parasail"\nquantization = "fp8"\n'
        self.assertEqual(self.load(pinned).api_key, "sk-or-v1-abc")
        for text, key in [(OPENROUTER, "sk-or-v1-abc"), (pinned, ""), (pinned, "sk-or\n-v1"), (pinned, "sk or")]:
            with self.assertRaises(ConfigError, msg=(text, key)) as caught:
                self.load(text, key)
            self.assertNotIn(key.strip() or "\0", str(caught.exception))

    def test_key_file_when_the_environment_has_none(self):
        pinned = OPENROUTER + 'provider = "parasail"\nquantization = "fp8"\n'
        key_file = self.home / "openrouter.key"
        key_file.write_text("sk-or-v1-file\n")
        key_file.chmod(0o600)
        self.assertEqual(self.load(pinned, key="").api_key, "sk-or-v1-file")
        self.assertEqual(self.load(pinned, key="sk-or-v1-env").api_key, "sk-or-v1-env")
        for mode, text in [(0o640, "sk-or-v1-file"), (0o604, "sk-or-v1-file"), (0o600, "  \n"),
                           (0o600, "sk-or-v1 file")]:
            key_file.write_text(text)
            key_file.chmod(mode)
            with self.assertRaises(ConfigError, msg=(oct(mode), text)) as caught:
                self.load(pinned, key="")
            self.assertNotIn("sk-or-v1", str(caught.exception))
        key_file.unlink()
        with self.assertRaises(ConfigError) as caught:
            self.load(pinned, key="")
        self.assertIn("openrouter.key", str(caught.exception))

    def test_key_never_sent_to_other_endpoints(self):
        config = self.load('boards_file = "b.json"\n[model]\nbase_url = "http://localhost:11434/v1"\nslug = "q"\n')
        self.assertIsNone(config.api_key)
        self.assertNotIn("sk-or", repr(config))

    def test_boards_file_is_optional_and_resolves_against_home(self):
        model = '[model]\nbase_url = "http://localhost:11434/v1"\nslug = "q"\n'
        self.assertIsNone(self.load(model).boards_file)
        self.assertEqual(self.load('boards_file = "b.json"\n' + model).boards_file, self.home / "b.json")
        self.assertEqual(self.load('boards_file = "/x/b.json"\n' + model).boards_file, Path("/x/b.json"))
        with self.assertRaises(ConfigError):
            self.load('boards_file = ""\n' + model)

    def test_bad_base_url(self):
        pinned = '\nprovider = "p"\nquantization = "q"\n'
        for url in ["localhost:11434", "https://[::1", "http://localhost:99999/v1", "http://openrouter.ai/api/v1",
                    "http://local host/v1", "http://model.invalid/v1#frag", "http://model.invalid/v1?x=1"]:
            with self.assertRaises(ConfigError, msg=url):
                self.load(f'boards_file = "b.json"\n[model]\nbase_url = "{url}"\nslug = "q"' + pinned)

    def test_unreadable_toml_is_a_config_error(self):
        (self.home / "config.toml").write_bytes(b'boards_file = "\xff"\n')
        with self.assertRaises(ConfigError):
            load_config(self.home)

    def test_empty_exclusions_allowed(self):
        (self.home / "profiles").mkdir()
        (self.home / "profiles" / "ae.toml").write_text('titles = ["AE"]\nexclude_titles = []\nlocations = ["London"]\n'
                                                        'eligible_where = "the UK"\ntags = ["founding"]\n')
        self.assertEqual(load_profile(self.home).exclude_titles, ())

    def profile(self, text):
        (self.home / "profiles").mkdir(exist_ok=True)
        (self.home / "profiles" / "pm.toml").write_text(
            'titles = ["PM"]\nlocations = ["London"]\neligible_where = "the UK"\n' + text)
        return load_profile(self.home)

    def test_custom_tags_in_file_order_with_defaults(self):
        profile = self.profile('tags = ["founding"]\n'
                               '[custom_tags.climate]\ndefinition = " Climate product. "\n'
                               '[custom_tags.model]\ndefinition = "B2B or B2C."\n'
                               'values = ["b2b", "b2c"]\nmulti = true\n'
                               '[custom_tags.remote]\ndefinition = "Fully remote."\ndefault = "no"\n')
        self.assertEqual(profile.tags, ("founding",))
        self.assertEqual(profile.custom_tags, (
            CustomTag("climate", "Climate product.", ("yes", "no", "unknown"), multi=False, default=None),
            CustomTag("model", "B2B or B2C.", ("b2b", "b2c"), multi=True, default=None),
            CustomTag("remote", "Fully remote.", ("yes", "no", "unknown"), multi=False, default="no"),
        ))

    def test_custom_tags_alone_need_no_builtins(self):
        for tags in ['tags = []\n', '']:
            profile = self.profile(tags + '[custom_tags.climate]\ndefinition = "Climate product."\n')
            self.assertEqual((profile.tags, len(profile.custom_tags)), ((), 1))
        for text in ['tags = []\n', '']:
            with self.assertRaises(ConfigError, msg=text):
                self.profile(text)

    def test_bad_custom_tags_rejected(self):
        for table in ['[custom_tags.Climate]\ndefinition = "x"', '[custom_tags.2x]\ndefinition = "x"',
                      '[custom_tags.climate]\ndefinition = " "', '[custom_tags.climate]\nvalues = ["yes"]',
                      '[custom_tags.climate]\ndefinition = "x"\nvalues = []',
                      '[custom_tags.climate]\ndefinition = "x"\nvalues = ["yes", "yes"]',
                      '[custom_tags.climate]\ndefinition = "x"\nvalues = ["b2b ", "b2c"]',
                      '[custom_tags.climate]\ndefinition = "x"\nmulti = "yes"',
                      '[custom_tags.climate]\ndefinition = "x"\ndefault = "maybe"',
                      '[custom_tags.climate]\ndefinition = "x"\nmulti = true\ndefault = "no"',
                      '[custom_tags.climate]\ndefinition = "x"\nnote = "y"',
                      'custom_tags = "climate"']:
            with self.assertRaises(ConfigError, msg=table):
                self.profile('tags = ["founding"]\n' + table + "\n")

    def test_duplicate_tags_rejected(self):
        (self.home / "profiles").mkdir()
        (self.home / "profiles" / "ae.toml").write_text(
            'titles = ["AE"]\nlocations = ["London"]\neligible_where = "the UK"\ntags = ["founding", "founding"]\n')
        with self.assertRaises(ConfigError):
            load_profile(self.home)


if __name__ == "__main__":
    unittest.main()
