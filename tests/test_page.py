"""The set-up page's values: posted fields read, checked into a profile, and rendered without leaking secrets or
markup."""
import unittest
from dataclasses import replace

from jobscan import page, tag
from jobscan.config import EmailConfig, custom_tag
from jobscan.titles import Counted, Outcome, Suggested

FIELDS = {"name": ["ae"], "titles": ["Account Executive\r\n\r\n  Sales Executive \n"], "exclude_titles": [""],
          "locations": ["London\nUK"], "eligible_where": ["the UK"], "tag": ["founding"],
          "custom-0-name": ["climate"], "custom-0-definition": ["Cuts emissions."],
          "custom-1-name": ["buyer"], "custom-1-definition": ["Who buys."], "custom-1-values": ["smb\nenterprise"],
          "custom-1-multi": ["on"], "custom-3-name": [""], "custom-3-definition": ["  "],
          "add_title": ["AE", "account executive"], "add_exclude": ["Assistant"],
          "key": [" sk-or-v1-abc "], "password": ["pass word"], "token": ["t"]}


class ReadTest(unittest.TestCase):
    def test_posted_fields_become_the_form(self):
        form = page.read({**FIELDS, "tag": ["founding"]})
        self.assertEqual((form.name, form.eligible_where, form.tags, form.key, form.password),
                         ("ae", "the UK", frozenset({"founding"}), "sk-or-v1-abc", "pass word"))
        self.assertEqual([r.name for r in form.custom], ["climate", "buyer"])  # empty rows dropped
        self.assertTrue(form.custom[1].multi)

    def test_missing_fields_are_empty_and_too_long_or_too_many_is_refused_not_cut(self):
        form = page.read({"titles": ["x" * 50_000]})
        self.assertEqual((form.name, len(form.titles), form.custom, form.too_long), ("", 50_000, (), {"titles"}))
        self.assertIn("Too long", page.to_profile(form).errors["titles"])
        rows = {f"custom-{i}-{part}": [f"t{i}" if part == "name" else f"Tag {i}."]
                for i in range(page.MOST_TAGS) for part in ("name", "definition")}
        self.assertEqual(page.to_profile(page.read({**FIELDS, **rows})).errors, {})  # the most the page edits
        many = page.read({**FIELDS, **rows, "custom-5000-name": ["one-too-many"], "custom-5000-definition": ["x"]})
        self.assertIn(f"More than {page.MOST_TAGS} custom tags", page.to_profile(many).errors[""])
        long_tick = page.read({**FIELDS, "add_title": ["x" * 50_000]})
        self.assertIn("Too long", page.to_profile(long_tick).errors["titles"])
        together = page.read({**FIELDS, "titles": ["x" * page.TEXT_LIMIT], "add_title": ["Sales Executive"]})
        self.assertIn("Too long", page.to_profile(together).errors["titles"])
        moved = page.read({**FIELDS, "custom-0-name": [""], "custom-1-definition": ["x" * 50_000]})
        self.assertEqual(set(page.to_profile(moved).errors), {"custom-0"})  # buyer is now the first row shown

    def test_a_browsers_crlf_is_stored_as_lf(self):
        form = page.read({**FIELDS, "custom-0-definition": ["Line one\r\nLine two\rthree"]})
        self.assertEqual(form.custom[0].definition, "Line one\nLine two\nthree")

    def test_a_huge_row_index_is_just_a_row(self):
        form = page.read({**FIELDS, f"custom-{'9' * 5000}-name": ["late"], f"custom-{'9' * 5000}-definition": ["L."]})
        self.assertEqual([r.name for r in form.custom], ["climate", "buyer", "late"])

    def test_custom_rows_keep_their_posted_names(self):
        form = page.read({**FIELDS, "custom-01-name": ["other"], "custom-01-definition": ["Other."]})
        self.assertEqual([r.name for r in form.custom], ["climate", "other", "buyer"])

    def test_a_custom_row_without_a_name_is_removed(self):
        form = page.read({**FIELDS, "custom-0-name": [""], "custom-0-definition": ["x" * 50_000]})  # not checked
        self.assertEqual([r.name for r in form.custom], ["buyer"])
        self.assertEqual(page.to_profile(form).errors, {})

    def test_a_profile_the_form_would_change_is_not_representable(self):
        profile = page.to_profile(page.read(FIELDS)).profile
        self.assertIsNone(page.unrepresentable(profile))
        for changed in [replace(profile, titles=("AE\nSDR",)), replace(profile, titles=("Account\u2028Executive",)),
                        replace(profile, exclude_titles=("A\x85B",)), replace(profile, eligible_where="the\nUK"),
                        replace(profile, name="my_search")]:
            self.assertIsNotNone(page.unrepresentable(changed), changed)
        buyer = replace(profile.custom_tags[1], values=("small\nmedium", "large"))
        self.assertIsNotNone(page.unrepresentable(replace(profile, custom_tags=(buyer,))))

    def test_a_profile_too_long_or_too_large_for_the_page_is_not_offered(self):
        profile = page.to_profile(page.read(FIELDS)).profile
        self.assertIn("Too long", page.unrepresentable(replace(profile, locations=("x" * 20_001,))))
        defined = replace(profile.custom_tags[0], definition="x" * 19_000)
        big = replace(profile, custom_tags=tuple(replace(defined, name=f"t{i}", definition=f"{i}" + defined.definition)
                                                 for i in range(10)))  # each a different question
        self.assertIn("Too large for one page", page.unrepresentable(big))
        self.assertIn("Too large for one page", page.to_profile(page.form_from_profile(big, None)).errors[""])
        carriage = replace(profile.custom_tags[0], definition="One\r\nTwo")
        self.assertIn("carriage return", page.unrepresentable(replace(profile, custom_tags=(carriage,))))

    def test_whatever_the_page_saves_it_can_reopen(self):
        rows = {f"custom-{i}-{part}": [f"t{i}" if part == "name" else f"{i} " + "x" * 19_000]
                for i in range(7) for part in ("name", "definition")}
        checked = page.to_profile(page.read({**FIELDS, **rows}))
        self.assertIsNone(checked.profile)  # refused before saving, not after
        small = page.to_profile(page.read(FIELDS)).profile
        self.assertIsNone(page.unrepresentable(small))

    def test_an_unknown_tag_posted_is_refused(self):
        self.assertIn("unknown tags", page.to_profile(page.read({**FIELDS, "tag": ["founder"]})).errors[""])

    def test_ticked_suggestions_join_the_lists(self):
        form = page.with_ticked(page.read(FIELDS))
        self.assertEqual((form.titles, form.exclude_titles, form.add_titles), (
            "Account Executive\nSales Executive\nAE", "Assistant", ()))


class ToProfileTest(unittest.TestCase):
    def test_the_form_becomes_a_profile_with_ticked_suggestions(self):
        checked = page.to_profile(page.read(FIELDS))
        profile = checked.profile
        self.assertEqual(checked.errors, {})
        self.assertEqual(profile.titles, ("Account Executive", "Sales Executive", "AE"))  # a repeat not added twice
        self.assertEqual(profile.exclude_titles, ("Assistant",))
        self.assertEqual(profile.locations, ("London", "UK"))
        self.assertEqual(profile.custom_tags[1], custom_tag("buyer", {"definition": "Who buys.",
                                                                      "values": ["smb", "enterprise"], "multi": True},
                                                            "test"))

    def test_errors_sit_by_their_fields(self):
        errors = page.to_profile(page.read({**FIELDS, "titles": [" \n"], "add_title": [], "eligible_where": [""],
                                               "custom-0-definition": [""]})).errors
        self.assertEqual(set(errors), {"titles", "eligible_where", "custom-0"})
        errors = page.to_profile(page.read({**FIELDS, "name": ["../x"]})).errors
        self.assertIn("lower-case letters", errors["name"])
        errors = page.to_profile(page.read({**FIELDS, "tag": [], "custom-0-name": [""], "custom-0-definition": [""],
                                               "custom-1-name": [""], "custom-1-definition": [""]})).errors
        self.assertIn("no tags given", errors[""])


class RenderTest(unittest.TestCase):
    def view(self, **changes):
        form = page.read({**FIELDS, "titles": ['<script>alert(1)</script>\n"quoted"']})
        return page.View(**{"form": form, "token": "tok&en", "catalogue": tag.builtins(), "fresh": True,
                            "key_held": False, "password_held": False,
                            "email": None, **changes})

    def test_text_is_escaped_and_secrets_never_shown(self):
        html = page.render(self.view())
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn('value="tok&amp;en"', html)
        self.assertNotIn("sk-or-v1-abc", html)
        self.assertNotIn("pass word", html)

    def test_every_built_in_tag_is_a_tick_box_and_the_forms_are_ticked(self):
        html = page.render(self.view())
        for t in tag.builtins():
            self.assertIn(f'name="tag" value="{t.name}"', html)
        self.assertIn('value="founding" checked', html)
        self.assertNotIn('value="segment" checked', html)

    def test_errors_and_held_secrets_are_said(self):
        email = EmailConfig(to="me@gmail.com", sender="me@gmail.com", host="smtp.gmail.com", port=465, password="p")
        html = page.render(self.view(errors={"titles": "Give at least one title.", "": "Not saved."}, key_held=True,
                                     password_held=True, email=email, fresh=False))
        self.assertIn("Give at least one title.", html)
        self.assertIn("Not saved.", html)
        self.assertIn("A key is held: leave this empty to keep it.", html)
        self.assertIn("An app password is held", html)
        self.assertIn("Now: digests to me@gmail.com", html)
        self.assertIn('name="email_off"', html)
        self.assertNotIn('name="name" value="ae" size', html)  # an existing search's name is fixed

    def test_suggestions_are_tick_boxes_with_counts_when_there_are_some(self):
        suggested = Suggested(variants=("Sales Executive",), near_misses=("Account Manager",), usage=tag.Usage(0.002))
        html = page.render(self.view(suggestions=Outcome(suggested, Counted({"Sales Executive": 4},
                                                                             {"Account Manager": 2}))))
        self.assertIn('name="add_title" value="Sales Executive"> Sales Executive <i>+4</i>', html)
        self.assertIn('name="add_exclude" value="Account Manager"> Account Manager <i>-2</i>', html)
        self.assertIn("$0.0020", html)
        html = page.render(self.view(suggestions=Outcome(suggested, None)))
        self.assertIn("Counts come after the first run.", html)
        self.assertNotIn("<i>+", html)

    def test_a_preview_before_any_run_says_so(self):
        self.assertIn("No postings yet", page.render(self.view(previewed=True)))


if __name__ == "__main__":
    unittest.main()
