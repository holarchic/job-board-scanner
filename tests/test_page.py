"""The set-up page's values: posted fields read, checked into a profile, and rendered without leaking secrets or
markup."""
import unittest

from jobscan import page, tag
from jobscan.config import EmailConfig, custom_tag
from jobscan.titles import Counted, Outcome, Suggested

FIELDS = {"name": ["ae"], "titles": ["Account Executive\r\n\r\n  Sales Executive \n"], "exclude_titles": [""],
          "locations": ["London\nUK"], "eligible_where": ["the UK"], "tag": ["founding", "nope"],
          "custom-0-name": ["climate"], "custom-0-definition": ["Cuts emissions."],
          "custom-1-name": ["buyer"], "custom-1-definition": ["Who buys."], "custom-1-values": ["smb\nenterprise"],
          "custom-1-multi": ["on"], "custom-3-name": [""], "custom-3-definition": ["  "],
          "add_title": ["AE", "account executive"], "add_exclude": ["Assistant"],
          "key": [" sk-or-v1-abc "], "password": ["pass word"], "token": ["t"]}


class ReadTest(unittest.TestCase):
    def test_posted_fields_become_the_form(self):
        form = page.read(FIELDS)
        self.assertEqual((form.name, form.eligible_where, form.tags, form.key, form.password),
                         ("ae", "the UK", frozenset({"founding"}), "sk-or-v1-abc", "pass word"))
        self.assertEqual([r.name for r in form.custom], ["climate", "buyer"])  # empty rows dropped
        self.assertTrue(form.custom[1].multi)

    def test_missing_fields_are_empty_and_long_ones_cut(self):
        form = page.read({"titles": ["x" * 50_000]})
        self.assertEqual((form.name, len(form.titles), form.custom), ("", page.TEXT_LIMIT, ()))


class ToProfileTest(unittest.TestCase):
    def test_the_form_becomes_a_profile_with_ticked_suggestions(self):
        profile, errors = page.to_profile(page.read(FIELDS))
        self.assertEqual(errors, {})
        self.assertEqual(profile.titles, ("Account Executive", "Sales Executive", "AE"))  # a repeat not added twice
        self.assertEqual(profile.exclude_titles, ("Assistant",))
        self.assertEqual(profile.locations, ("London", "UK"))
        self.assertEqual(profile.custom_tags[1], custom_tag("buyer", {"definition": "Who buys.",
                                                                      "values": ["smb", "enterprise"], "multi": True},
                                                            "test"))

    def test_errors_sit_by_their_fields(self):
        _, errors = page.to_profile(page.read({**FIELDS, "titles": [" \n"], "add_title": [], "eligible_where": [""],
                                               "custom-0-definition": [""]}))
        self.assertEqual(set(errors), {"titles", "eligible_where", "custom-0"})
        _, errors = page.to_profile(page.read({**FIELDS, "name": ["../x"]}))
        self.assertIn("profile name", errors["name"])
        _, errors = page.to_profile(page.read({**FIELDS, "tag": [], "custom-0-name": [""], "custom-0-definition": [""],
                                               "custom-1-name": [""], "custom-1-definition": [""]}))
        self.assertIn("no tags given", errors[""])


class RenderTest(unittest.TestCase):
    def view(self, **changes):
        form = page.read({**FIELDS, "titles": ['<script>alert(1)</script>\n"quoted"']})
        return page.View(**{"form": form, "token": "tok&en", "fresh": True, "key_held": False, "password_held": False,
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
