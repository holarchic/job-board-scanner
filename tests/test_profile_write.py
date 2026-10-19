"""The one profile writer (`config.profile_toml`) and the writers the set-up page saves through."""
import stat
import tempfile
import unittest
from pathlib import Path

from jobscan import init
from jobscan.config import ConfigError, CustomTag, Profile, Requirement, custom_tag, load_profile, profile_toml

CLIMATE = custom_tag("climate", {"definition": 'Cuts "emissions"\\n- yes: says so'}, "test")
SEGMENT = custom_tag("buyer", {"definition": "Who buys.", "values": ["smb", "enterprise"], "multi": True}, "test")
REMOTE = custom_tag("remote_ok", {"definition": "Remote allowed.", "values": ["yes", "no", "unknown"],
                                  "default": "unknown"}, "test")
PROFILE = Profile(name="ae", titles=("Account Executive", 'Head "Sales"\\x7f'), exclude_titles=(),
                  locations=("London",), eligible_where="the UK", tags=("founding",),
                  custom_tags=(CLIMATE, SEGMENT, REMOTE))


class Home(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / "profiles").mkdir()


class ProfileTomlTest(Home):
    def test_the_writer_and_the_reader_agree_custom_tags_included(self):
        (self.home / "profiles" / "ae.toml").write_text(profile_toml(PROFILE))
        self.assertEqual(load_profile(self.home), PROFILE)

    def test_the_writer_and_the_reader_agree_on_require(self):
        required = Profile(**{**vars(PROFILE), "require": (Requirement("founding", "yes"),
                                                            Requirement("buyer", "smb"))})
        text = profile_toml(required)
        self.assertIn('require = { founding = "yes", buyer = "smb" }', text)
        (self.home / "profiles" / "ae.toml").write_text(text)
        self.assertEqual(load_profile(self.home), required)
        self.assertNotIn("require", profile_toml(PROFILE))

    def test_a_require_that_isnt_a_table_of_strings_is_refused(self):
        for line in ['require = "founding"', "require = { founding = true }", 'require = { founding = "" }']:
            (self.home / "profiles" / "ae.toml").write_text(profile_toml(PROFILE).replace(
                'tags = ["founding"]', f'tags = ["founding"]\n{line}'))
            with self.subTest(line=line), self.assertRaisesRegex(ConfigError, "`require` must be a table"):
                load_profile(self.home)

    def test_init_without_custom_tags_keeps_its_commented_example(self):
        answers = init.Answers(name="ae", titles=["AE"], exclude_titles=[], locations=["London"],
                               eligible_where="the UK", tags=["founding"], key="sk-or-v1-abc")
        self.assertIn("# [custom_tags.b2b]", init.profile_text(answers))
        with_custom = init.Answers(**{**vars(answers), "custom_tags": (CLIMATE,)})
        self.assertNotIn("# [custom_tags.b2b]", init.profile_text(with_custom))


class ValidateProfileTest(unittest.TestCase):
    def test_custom_tags_alone_are_enough(self):
        init.validate_profile(Profile(**{**vars(PROFILE), "tags": ()}))

    def test_refused_profiles(self):
        cases = {
            "no tags given": {"tags": (), "custom_tags": ()},
            "use built-in names": {"tags": (),
                                   "custom_tags": (CustomTag("founding", "x", ("yes", "no"), False, None),)},
            "given twice": {"custom_tags": (CLIMATE, CLIMATE)},
            "asks the same question": {"custom_tags": (CLIMATE, CustomTag("climate2", CLIMATE.definition,
                                                                         CLIMATE.values, False, None))},
            "profile name": {"name": "../x"},
            "which the profile doesn't tag": {"require": (Requirement("segment", "smb"),)},
            "isn't an answer it can require": {"require": (Requirement("founding", "maybe"),)},
        }
        for message, change in cases.items():
            with self.assertRaisesRegex(ConfigError, message):
                init.validate_profile(Profile(**{**vars(PROFILE), **change}))


class SaveTest(Home):
    def test_saving_keeps_the_previous_file_and_reads_back(self):
        path = self.home / "profiles" / "ae.toml"
        path.write_text('titles = ["AE"]  # mine\nlocations = ["Leeds"]\neligible_where = "UK"\ntags = ["founding"]\n')
        before = path.read_bytes()
        self.assertEqual(init.save_profile(self.home, PROFILE), path)
        self.assertEqual(load_profile(self.home), PROFILE)
        self.assertEqual((self.home / "profiles" / "ae.toml.prev").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in (self.home / "profiles").iterdir()), ["ae.toml", "ae.toml.prev"])

    def test_a_refused_profile_changes_nothing(self):
        path = self.home / "profiles" / "ae.toml"
        path.write_text("keep me")
        with self.assertRaises(ConfigError):
            init.save_profile(self.home, Profile(**{**vars(PROFILE), "titles": ()}))
        self.assertEqual(path.read_text(), "keep me")
        self.assertEqual([p.name for p in (self.home / "profiles").iterdir()], ["ae.toml"])

    def test_the_key_is_replaced_readable_only_by_you(self):
        (self.home / "openrouter.key").write_text("old\n")
        init.save_key(self.home, "sk-or-v1-new")
        key = self.home / "openrouter.key"
        self.assertEqual((key.read_text(), stat.S_IMODE(key.stat().st_mode)), ("sk-or-v1-new\n", 0o600))
        with self.assertRaises(ConfigError):
            init.save_key(self.home, "sk or v1")
        self.assertEqual(key.read_text(), "sk-or-v1-new\n")

    def test_a_new_install_with_custom_tags_only(self):
        answers = init.Answers(name="ae", titles=["AE"], exclude_titles=[], locations=["London"],
                               eligible_where="the UK", tags=[], key="sk-or-v1-abc", custom_tags=(CLIMATE,))
        init.init_from(self.home / "new", answers)
        self.assertEqual(load_profile(self.home / "new").custom_tags, (CLIMATE,))


if __name__ == "__main__":
    unittest.main()
