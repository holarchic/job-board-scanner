"""The one profile writer (`config.profile_toml`) and the writers the set-up page saves through."""
import stat
import tempfile
import unittest
from pathlib import Path

from jobscan import init
from jobscan.config import (
    CompanyNote,
    ConfigError,
    CustomTag,
    Profile,
    Requirement,
    custom_tag,
    load_profile,
    profile_toml,
)

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
        required = Profile(**{**vars(PROFILE), "require": (Requirement("founding", ("yes",)),
                                                            Requirement("buyer", ("smb",)))})
        text = profile_toml(required)
        self.assertIn('require = { founding = "yes", buyer = "smb" }', text)
        (self.home / "profiles" / "ae.toml").write_text(text)
        self.assertEqual(load_profile(self.home), required)
        self.assertNotIn("require", profile_toml(PROFILE))

    def test_several_answers_are_a_list_and_one_stays_a_string(self):
        required = Profile(**{**vars(PROFILE), "require": (Requirement("buyer", ("smb", "enterprise")),
                                                            Requirement("founding", ("yes",)))})
        text = profile_toml(required)
        self.assertIn('require = { buyer = ["smb", "enterprise"], founding = "yes" }', text)
        (self.home / "profiles" / "ae.toml").write_text(text)
        self.assertEqual(load_profile(self.home), required)

    def test_an_answer_listed_twice_is_kept_once(self):
        (self.home / "profiles" / "ae.toml").write_text(profile_toml(PROFILE).replace(
            'tags = ["founding"]', 'tags = ["founding"]\nrequire = { buyer = ["smb", "enterprise", "smb"] }'))
        self.assertEqual(load_profile(self.home).require, (Requirement("buyer", ("smb", "enterprise")),))

    def test_include_agencies_is_written_only_when_true_and_read_back(self):
        self.assertNotIn("include_agencies", profile_toml(PROFILE))
        including = Profile(**{**vars(PROFILE), "include_agencies": True})
        self.assertIn("include_agencies = true", profile_toml(including))
        (self.home / "profiles" / "ae.toml").write_text(profile_toml(including))
        self.assertEqual(load_profile(self.home), including)
        (self.home / "profiles" / "ae.toml").write_text('include_agencies = "yes"\n' + profile_toml(PROFILE))
        with self.assertRaisesRegex(ConfigError, "`include_agencies` must be true or false"):
            load_profile(self.home)

    def test_the_watchlist_is_written_only_when_set_and_read_back(self):
        self.assertNotIn("watch", profile_toml(PROFILE))
        watching = Profile(**{**vars(PROFILE), "watch": ("Acme", "Acme AI", "hooli-eu", 'Q "quoted"')})
        self.assertIn('watch = ["Acme", "Acme AI", "hooli-eu", "Q \\"quoted\\""]', profile_toml(watching))
        (self.home / "profiles" / "ae.toml").write_text(profile_toml(watching))
        self.assertEqual(load_profile(self.home), watching)
        (self.home / "profiles" / "ae.toml").write_text("watch = []\n" + profile_toml(PROFILE))
        self.assertEqual(load_profile(self.home), PROFILE)

    def test_a_watchlist_that_isnt_names_or_names_one_twice_is_refused(self):
        for value, message in [('[""]', "`watch` must be a list of strings"), ("[1]", "`watch` must be a list"),
                               ('"Acme"', "`watch` must be a list"),
                               ('["Acme", "ACME"]', "`watch` lists a company twice"),
                               ('["Acme", " acme "]', "`watch` lists a company twice")]:
            (self.home / "profiles" / "ae.toml").write_text(f"watch = {value}\n" + profile_toml(PROFILE))
            with self.assertRaisesRegex(ConfigError, message, msg=value):
                load_profile(self.home)

    def test_company_notes_are_a_table_written_only_when_set_and_read_back(self):
        self.assertNotIn("company_notes", profile_toml(PROFILE))
        noting = Profile(**{**vars(PROFILE), "watch": ("Acme",), "company_notes": (
            CompanyNote("Acme AI", 'applied 29 Sep, "PM"'), CompanyNote('Q "x" Ltd', "line one\nline two"),
            CompanyNote("hooli-eu", "talking to Sam"), CompanyNote("Zürich Ltd", "rejected"))})
        text = profile_toml(noting)
        self.assertIn('\n[company_notes]\n"Acme AI" = "applied 29 Sep, \\"PM\\""\n', text)
        self.assertLess(text.index("[company_notes]"), text.index("[custom_tags."))
        (self.home / "profiles" / "ae.toml").write_text(text)
        self.assertEqual(load_profile(self.home), noting)

    def test_company_notes_that_arent_names_to_notes_or_name_one_twice_are_refused(self):
        for table, message in [('company_notes = "Acme"', "must be a table of company names to your notes"),
                               ("[company_notes]\nAcme = 3", "must be a table"),
                               ('[company_notes]\nAcme = " "', "must be a table"),
                               ('[company_notes]\n" " = "x"', "must be a table"),
                               ('[company_notes]\nAcme = "a"\nACME = "b"', "names a company twice"),
                               ('[company_notes]\nAcme = "a"\n" acme " = "b"', "names a company twice")]:
            top, _, tables = profile_toml(PROFILE).partition("\n\n[custom_tags")
            text = (table + "\n" + top if table.startswith("company_notes =") else top + "\n\n" + table) + \
                "\n\n[custom_tags" + tables
            (self.home / "profiles" / "ae.toml").write_text(text)
            with self.assertRaisesRegex(ConfigError, message, msg=table):
                load_profile(self.home)

    def test_tagging_limits_are_written_only_when_not_the_defaults_and_read_back(self):
        self.assertNotIn("max_", profile_toml(PROFILE))
        (self.home / "profiles" / "ae.toml").write_text(profile_toml(PROFILE))
        self.assertEqual((load_profile(self.home).max_tag_per_run, load_profile(self.home).max_spend_per_run),
                         (500, 1.0))
        for count, spend in [(200, 0.555), (100_000, 1000.0), (1, 2.0), (500, 1e-05)]:
            limited = Profile(**{**vars(PROFILE), "max_tag_per_run": count, "max_spend_per_run": spend})
            (self.home / "profiles" / "ae.toml").write_text(profile_toml(limited))
            with self.subTest(count=count, spend=spend):
                self.assertEqual(load_profile(self.home), limited)
        (self.home / "profiles" / "ae.toml").write_text("max_spend_per_run = 2\n" + profile_toml(PROFILE))
        self.assertEqual(load_profile(self.home).max_spend_per_run, 2.0)  # a TOML integer is dollars too

    def test_a_tagging_limit_out_of_range_or_of_the_wrong_type_is_refused(self):
        lines = {"max_tag_per_run": ["0", "100001", "-1", "1.5", "true", '"500"'],
                 "max_spend_per_run": ["0", "-1", "1000.01", "true", "inf", "nan", '"1.00"']}
        for key, values in lines.items():
            for value in values:
                (self.home / "profiles" / "ae.toml").write_text(f"{key} = {value}\n" + profile_toml(PROFILE))
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ConfigError, f"`{key}` must be"):
                    load_profile(self.home)

    def test_validate_profile_refuses_a_limit_the_reader_would(self):
        for changes in [{"max_tag_per_run": 0}, {"max_spend_per_run": 0.0}, {"max_tag_per_run": True}]:
            with self.subTest(changes=changes), self.assertRaises(ConfigError):
                init.validate_profile(Profile(**{**vars(PROFILE), **changes}))

    def test_a_require_that_isnt_a_table_of_strings_is_refused(self):
        for line in ['require = "founding"', "require = { founding = true }", 'require = { founding = "" }',
                     "require = { founding = [] }", 'require = { founding = ["yes", 1] }',
                     'require = { founding = ["yes", " "] }', 'require = { founding = [["yes"]] }']:
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
            "which the profile doesn't tag": {"require": (Requirement("segment", ("smb",)),)},
            "isn't an answer it can require": {"require": (Requirement("founding", ("maybe",)),)},
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
