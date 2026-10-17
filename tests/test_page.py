"""The set-up page's values, step by step: posted fields merged into the draft, each step checked on its own, the
whole draft checked into a profile, and pages rendered without leaking secrets or markup."""
import re
import unittest
from dataclasses import replace
from unittest import mock

from jobscan import page, tag
from jobscan.config import EmailConfig, custom_tag
from jobscan.titles import Counted, Outcome, Suggested

DRAFT = page.Form(name="ae", titles="Account Executive\nSales Executive", locations="London\nUK",
                  eligible_where="the UK", tags=frozenset({"founding"}),
                  custom=(page.CustomRow(name="climate", definition="Cuts emissions."),
                          page.CustomRow(name="buyer", definition="Who buys.", values="smb\nenterprise", multi=True)))


SAVED = page.Saved(run="JOBSCAN_HOME='/h o'\"'\"'me' jobscan run", ui="jobscan ui", check="jobscan check",
                   digests="/h/digests/ae", log="/h/runs.log", email_to="me@gmail.com", at="08:00", cron="")
ALL_STEPS = (*page.STEPS, "done")


def checks(step, form, **given):
    return page.check_step(step, form, **{"key_held": False, "fresh": True, "password": "", **given})


class ReadStepTest(unittest.TestCase):
    def test_a_list_keeps_ticked_values_in_order_then_ticked_offers_then_new_lines(self):
        form = replace(DRAFT, titles="AE\nae\nAE\nSDR")
        offers = page.Offers(titles=("Sales Executive", "BDR"))
        merged, offers = page.read_step("titles", {"title": ["AE", "ae", "BDR"], "title_more": ["\r\nae\r\n Closer "]},
                                        form, offers)
        self.assertEqual(merged.titles, "AE\nae\nAE\nBDR\nCloser")  # SDR unticked; "ae" typed again not added
        self.assertEqual(offers.titles, ("Sales Executive", "BDR", "Closer"))
        typed, _ = page.read_step("titles", {"title_more": ["AE\nae\nAE"]}, page.Form(), page.Offers())
        self.assertEqual(typed.titles, "AE")

    def test_each_step_merges_only_its_own_fields(self):
        fields = {"title": ["X"], "place": ["Paris"], "eligible_where": [" France "], "tag": ["segment"],
                  "email_to": [" me@gmail.com "], "description": [" words "], "name": [" n "]}
        for step, changed in [("titles", {"titles": "X"}),
                              ("places", {"locations": "Paris", "eligible_where": "France"}),
                              ("email", {"email_to": "me@gmail.com"}), ("describe", {"description": "words"}),
                              ("summary", {"name": "n"})]:
            merged, _ = page.read_step(step, fields, DRAFT, page.Offers())
            self.assertEqual(merged, replace(DRAFT, **changed), step)
        merged, _ = page.read_step("tags", fields, DRAFT, page.Offers())
        self.assertEqual((merged.tags, merged.custom), (frozenset({"segment"}), ()))

    def test_a_ticked_offer_becomes_a_row_unless_a_row_has_its_name(self):
        offers = page.Offers(custom=(page.CustomRow(name="salary_stated", definition="Salary stated?"),))
        form, _ = page.read_step("tags", {"add_custom": ["salary_stated"]}, DRAFT, offers)
        self.assertEqual(form.custom, offers.custom)
        edited = {"add_custom": ["salary_stated"], "custom-0-name": ["salary_stated"],
                  "custom-0-definition": ["Is a pay range stated?"]}
        form, _ = page.read_step("tags", edited, DRAFT, offers)
        self.assertEqual([r.definition for r in form.custom], ["Is a pay range stated?"])

    def test_custom_rows_as_posted_after_ticked_offers(self):
        offered = page.CustomRow(name="salary_stated", definition="Salary stated?")
        fields = {"add_custom": ["salary_stated"], "custom-01-name": ["other"], "custom-01-definition": ["Other."],
                  "custom-1-name": ["buyer"], "custom-1-definition": ["Line one\r\nLine two\rthree"],
                  f"custom-{'9' * 5000}-name": ["late"], f"custom-{'9' * 5000}-definition": ["L."],
                  "custom-3-name": [" "], "custom-3-definition": ["x" * 50_000]}  # no name: removed, not checked
        form, _ = page.read_step("tags", fields, DRAFT, page.Offers(custom=(offered,)))
        self.assertEqual([r.name for r in form.custom], ["other", "buyer", "late", "salary_stated"])  # "01" by "1"
        self.assertEqual(form.custom[1].definition, "Line one\nLine two\nthree")
        self.assertEqual(form.too_long, frozenset())

    def test_too_long_or_too_many_is_noted_not_cut(self):
        form, _ = page.read_step("titles", {"title_more": ["x" * 50_000]}, DRAFT, page.Offers())
        self.assertEqual(form.too_long, {"titles"})
        self.assertIn("Too long", checks("titles", form)["titles"])
        rows = {f"custom-{i}-{part}": [f"t{i}" if part == "name" else f"Tag {i}."]
                for i in range(page.MOST_TAGS + 1) for part in ("name", "definition")}
        form, _ = page.read_step("tags", rows, DRAFT, page.Offers())
        self.assertIn(f"More than {page.MOST_TAGS} custom tags", checks("tags", form)["tags"])

    def test_the_schedule_is_a_box_and_a_time(self):
        on, _ = page.read_step("schedule", {"schedule_on": ["on"], "schedule_at": [" 07:30 "]}, DRAFT, page.Offers())
        self.assertEqual((on.schedule_on, on.schedule_at), (True, "07:30"))
        off, _ = page.read_step("schedule", {"schedule_at": ["07:30"]}, on, page.Offers())  # unticked: not posted
        self.assertEqual((off.schedule_on, off.schedule_at), (False, "07:30"))
        elsewhere, _ = page.read_step("titles", {"title": ["Account Executive"]}, on, page.Offers())
        self.assertTrue(elsewhere.schedule_on)

    def test_counts_go_once_the_lists_they_counted_change(self):
        offers = page.Offers(titles=("Sales Executive",), adds={"Sales Executive": 4}, leaves_out={"Assistant": 2})
        _, kept = page.read_step("titles", {"title": ["Account Executive", "Sales Executive"]},
                                 replace(DRAFT, titles="Account Executive"), offers)
        self.assertEqual(kept.adds, {})  # Sales Executive was added
        for step, fields in [("exclude", {"exclude_more": ["Assistant"]}), ("places", {"place": ["London"]})]:
            _, after = page.read_step(step, fields, DRAFT, offers)
            self.assertEqual((after.adds, after.leaves_out), ({}, {}), step)
        _, same = page.read_step("titles", {"title": ["Account Executive", "Sales Executive"]}, DRAFT, offers)
        self.assertEqual(same.adds, {"Sales Executive": 4})


class CheckStepTest(unittest.TestCase):
    def test_each_step_checks_only_its_own_fields(self):
        empty = page.Form()
        self.assertEqual(checks("titles", replace(empty, titles="AE")), {})  # no places yet: not this step's
        self.assertEqual(set(checks("titles", empty)), {"titles"})
        self.assertEqual(set(checks("places", empty)), {"locations", "eligible_where"})
        self.assertEqual(checks("exclude", empty), {})
        self.assertIn("padded", checks("titles", replace(empty, titles="AE\n\ud800"))["titles"])

    def test_the_key(self):
        self.assertIn("is needed", checks("key", page.Form())["key"])
        self.assertEqual(checks("key", page.Form(), key_held=True), {})
        self.assertEqual(checks("key", page.Form(), fresh=False), {})
        self.assertIn("isn't an OpenRouter key", checks("key", page.Form(key="sk or-v1"), key_held=True)["key"])

    def test_tags_by_the_rules_save_applies(self):
        self.assertEqual(checks("tags", DRAFT), {})
        self.assertIn("at least one tag", checks("tags", replace(DRAFT, tags=frozenset(), custom=()))["tags"])
        self.assertIn("unknown tags", checks("tags", replace(DRAFT, tags=frozenset({"founder"})))["tags"])
        shadow = (page.CustomRow(name="segment", definition="Mine."),)
        self.assertIn("built-in names", checks("tags", replace(DRAFT, custom=shadow))["tags"])
        twice = (page.CustomRow(name="a", definition="Same?"), page.CustomRow(name="b", definition="Same?"))
        self.assertIn("asks the same question", checks("tags", replace(DRAFT, custom=twice))["tags"])
        named = (page.CustomRow(name="a", definition="One?"), page.CustomRow(name="a", definition="Two?"))
        self.assertIn("named twice", checks("tags", replace(DRAFT, custom=named))["tags"])
        bad = (page.CustomRow(name="ok", definition="Fine."), page.CustomRow(name="x", definition=""))
        self.assertEqual(set(checks("tags", replace(DRAFT, custom=bad))), {"custom-1"})

    def test_email_settings_are_checked_without_sending(self):
        self.assertEqual(checks("email", DRAFT), {})  # no address: email stays off
        self.assertIn("tick the box", checks("email", DRAFT, email_saved=True)["email_to"])
        self.assertEqual(checks("email", replace(DRAFT, email_off=True), email_saved=True), {})
        mail = replace(DRAFT, email_to="me@gmail.com")
        self.assertIn("app password", checks("email", mail)["password"])
        self.assertEqual(checks("email", mail, password="abcd efgh"), {})
        self.assertIn("port", checks("email", replace(mail, email_port="9" * 9), password="p")["email_port"])
        self.assertIn("not an email address", checks("email", replace(mail, email_to="me"), password="p")["email"])
        self.assertEqual(checks("email", replace(mail, email_to="me", email_off=True)), {})


    def test_a_daily_run_needs_a_time(self):
        for at in ["", "25:00", "8.30", "08:00:00"]:
            self.assertEqual(checks("schedule", replace(DRAFT, schedule_on=True, schedule_at=at)),
                             {"schedule_at": "Use HH:MM, 24-hour, e.g. 08:00."}, at)
            self.assertEqual(checks("schedule", replace(DRAFT, schedule_at=at)), {}, at)  # unticked: no time needed
        self.assertEqual(checks("schedule", replace(DRAFT, schedule_on=True, schedule_at="8:30")), {})
        self.assertEqual(checks("summary", replace(DRAFT, schedule_on=True)), {})  # Save checks it itself


class ToProfileTest(unittest.TestCase):
    def test_the_draft_becomes_a_profile(self):
        checked = page.to_profile(DRAFT)
        self.assertEqual(checked.errors, {})
        self.assertEqual((checked.profile.titles, checked.profile.locations),
                         (("Account Executive", "Sales Executive"), ("London", "UK")))
        self.assertEqual(checked.profile.custom_tags[1], custom_tag(
            "buyer", {"definition": "Who buys.", "values": ["smb", "enterprise"], "multi": True}, "test"))

    def test_errors_sit_by_their_fields(self):
        errors = page.to_profile(replace(DRAFT, titles=" \n", eligible_where="",
                                         custom=(page.CustomRow(name="climate"),))).errors
        self.assertEqual(set(errors), {"titles", "eligible_where", "custom-0"})
        self.assertIn("lower-case letters", page.to_profile(replace(DRAFT, name="../x")).errors["name"])

    def test_a_profile_the_form_would_change_is_not_representable(self):
        profile = page.to_profile(DRAFT).profile
        self.assertIsNone(page.unrepresentable(profile))
        self.assertIsNone(page.unrepresentable(replace(profile, titles=("AE", "ae", "AE"))))
        for changed in [replace(profile, titles=("AE\nSDR",)), replace(profile, titles=("Account\u2028Executive",)),
                        replace(profile, exclude_titles=("A\x85B",)), replace(profile, eligible_where="the\nUK"),
                        replace(profile, name="my_search")]:
            self.assertIsNotNone(page.unrepresentable(changed), changed)
        buyer = replace(profile.custom_tags[1], values=("small\nmedium", "large"))
        self.assertIsNotNone(page.unrepresentable(replace(profile, custom_tags=(buyer,))))

    def test_a_profile_too_long_or_too_large_for_the_page_is_not_offered(self):
        profile = page.to_profile(DRAFT).profile
        self.assertIn("Too long", page.unrepresentable(replace(profile, locations=("x" * 20_001,))))
        defined = replace(profile.custom_tags[0], definition="x" * 19_000)
        big = replace(profile, custom_tags=tuple(replace(defined, name=f"t{i}", definition=f"{i}" + defined.definition)
                                                 for i in range(10)))  # each a different question
        self.assertIn("Too large for one page", page.unrepresentable(big))
        carriage = replace(profile.custom_tags[0], definition="One\r\nTwo")
        self.assertIn("carriage return", page.unrepresentable(replace(profile, custom_tags=(carriage,))))

    def test_whatever_the_page_saves_it_can_reopen(self):
        rows = tuple(page.CustomRow(name=f"t{i}", definition=f"{i} " + "x" * 19_000) for i in range(4))
        # A vertical tab splits a list as a line break does, but posts in half the bytes of the CRLF it becomes.
        tabbed = replace(DRAFT, custom=rows, locations="a\v" * 10_000)
        self.assertLess(page._posted_size(tabbed), page.LARGEST_POST // 2)  # as typed, it would fit
        self.assertIsNone(page.to_profile(tabbed).profile)
        fits = page.to_profile(replace(tabbed, locations="London\v" * 100)).profile
        self.assertIsNone(page.unrepresentable(fits))

    def test_a_search_part_way_needs_only_titles(self):
        self.assertIsNone(page.search(page.Form()))
        self.assertEqual(page.search(page.Form(titles="AE")).titles, ("AE",))


class SuggestionTest(unittest.TestCase):
    def test_a_new_search_is_named_from_its_first_title(self):
        for titles, name in [("Account Executive\nAE", "account-executive"), ("", "my-search"), ("*", "my-search"),
                             ("Ünïcode Lead", "n-code-lead")]:
            self.assertEqual(page.default_name(page.Form(titles=titles)), name, titles)


class RenderTest(unittest.TestCase):
    def view(self, step="titles", **changes):
        form = replace(DRAFT, titles='<script>alert(1)</script>\n"quoted"', key="sk-or-v1-abc", password="pass word")
        return page.View(**{"form": form, "offers": page.Offers(), "step": step, "revision": 3, "token": "tok&en",
                            "catalogue": tag.builtins(), "fresh": True, "key_held": False, "password_held": False,
                            "email": None, "home": "/h o'me", "launchd": True, "saved": SAVED, **changes})

    def test_text_is_escaped_and_secrets_never_shown(self):
        for step in ALL_STEPS:
            html = page.render(self.view(step))
            self.assertNotIn("<script>", html, step)
            self.assertIn('value="tok&amp;en"', html)
            self.assertIn(f'name="step" value="{step}"', html)
            self.assertIn('name="revision" value="3"', html)
            self.assertNotIn("sk-or-v1-abc", html)
            self.assertNotIn("pass word", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page.render(self.view("titles")))

    def test_ticks_are_the_draft_then_the_offers_with_counts(self):
        offers = page.Offers(titles=("Sales Executive", "AE"), adds={"Sales Executive": 4}, note="From your words.")
        html = page.render(self.view(form=replace(DRAFT, titles="AE\nae"), offers=offers))
        self.assertIn('name="title" value="AE" checked> AE<', html)
        self.assertIn('name="title" value="ae" checked> ae<', html)
        self.assertIn('name="title" value="Sales Executive"> Sales Executive <i>+4</i>', html)
        self.assertEqual(html.count('value="AE"'), 1)
        self.assertIn("From your words.", html)

    def test_every_built_in_tag_is_a_tick_box_and_offered_custom_tags_too(self):
        offered = (page.CustomRow(name="salary_stated", definition="Salary stated?"),
                   page.CustomRow(name="visa", definition="Visa offered?"))
        edited = replace(offered[0], definition="Is a pay range stated?")
        html = page.render(self.view("tags", form=replace(DRAFT, custom=(*DRAFT.custom, edited)),
                                     offers=page.Offers(custom=offered), errors={"custom-1": "Bad buyer."}))
        for t in tag.builtins():
            self.assertIn(f'name="tag" value="{t.name}"', html)
        self.assertIn('value="founding" checked', html)
        self.assertNotIn('value="segment" checked', html)
        self.assertIn('name="add_custom" value="visa">', html)
        self.assertNotIn('name="add_custom" value="salary_stated"', html)  # a row has its name: no longer offered
        self.assertIn('name="custom-2-name" value="salary_stated"', html)  # but editable
        self.assertIn('Bad buyer.</span><label>Name <input name="custom-1-name" value="buyer"', html)

    def test_errors_and_held_secrets_are_said(self):
        email = EmailConfig(to="me@gmail.com", sender="me@gmail.com", host="smtp.gmail.com", port=465, password="p")
        html = page.render(self.view("email", errors={"password": "Your app password is needed.", "": "Not saved."},
                                     password_held=True, email=email, fresh=False))
        self.assertIn("Your app password is needed.", html)
        self.assertIn("Not saved.", html)
        self.assertIn("An app password is held", html)
        self.assertIn("Now: digests to me@gmail.com", html)
        self.assertIn('name="email_off"', html)
        self.assertIn("A key is held", page.render(self.view("key", key_held=True)))

    def test_buttons_follow_the_step(self):
        self.assertNotIn('value="back"', page.render(self.view("intro")))  # nothing before the intro
        self.assertIn('value="back"', page.render(self.view("key")))
        self.assertIn(">Start</button>", page.render(self.view("intro")))
        self.assertIn('value="skip"', page.render(self.view("describe")))
        self.assertIn('value="suggest"', page.render(self.view("titles")))
        self.assertIn(">Done</button>", page.render(self.view("titles", fresh=False)))
        self.assertNotIn("Fn", page.render(self.view("describe")))
        summary = page.render(self.view("summary", fresh=False, errors={"titles": "Give at least one title."}))
        self.assertIn('value="save"', summary)
        self.assertIn('Give at least one title.</span></td><td><button name="action" value="goto:titles">', summary)
        self.assertNotIn('name="name"', summary)  # an existing search's name is fixed
        self.assertIn('name="name" value="ae"', page.render(self.view("summary")))

    def test_rendering_reads_nothing(self):
        views = [self.view(step) for step in ALL_STEPS]
        with mock.patch.object(tag, "builtin_names", side_effect=AssertionError("read tags.toml")), \
                mock.patch.object(tag, "builtins", side_effect=AssertionError("read tags.toml")):
            for view in views:
                page.render(view)

    def test_every_button_is_an_action_the_server_takes_and_enter_moves_on(self):
        for fresh in (True, False):
            for step in (*page.steps(fresh), "done"):
                html = page.render(self.view(step, fresh=fresh))
                pressed = re.findall(r'<button name="action" value="([^"]+)"', html)
                self.assertEqual(set(pressed), page.actions(step), (step, fresh))
                enter = {"summary": "save", "done": "goto:summary"}.get(step, "next")
                self.assertEqual(pressed[0], enter, step)  # what Enter presses
        said = replace(DRAFT, description="AE jobs in London")
        self.assertIn("replaces the titles, places and tags", page.render(self.view("describe", form=said)))
        self.assertNotIn("replaces", page.render(self.view("describe", form=page.Form())))

    def test_the_intro_says_how_and_where_it_runs(self):
        mac, other = page.render(self.view("intro")), page.render(self.view("intro", launchd=False))
        self.assertIn("/h o&#x27;me", mac)
        self.assertIn("when the Mac wakes", mac)
        self.assertIn("waits for the next day", mac)
        self.assertNotIn("crontab", mac)
        self.assertIn("crontab", other)
        self.assertNotIn("Step ", mac)  # unnumbered: the steps count from the key
        self.assertIn("Step 1 of 9", page.render(self.view("key")))

    def test_the_schedule_step_and_its_summary_row(self):
        on = replace(DRAFT, schedule_on=True, schedule_at="07:30")
        html = page.render(self.view("schedule", form=on, errors={"schedule_at": "Use HH:MM."}))
        self.assertIn('<input type="checkbox" name="schedule_on" checked>', html)
        self.assertIn('<input type="time" name="schedule_at" value="07:30">', html)
        self.assertIn("Use HH:MM.", html)
        self.assertIn("when the Mac wakes", html)
        self.assertIn("Unticking removes nothing", page.render(self.view("schedule", launchd=False)))
        kept = page.render(self.view("schedule", schedule_kept=True))
        self.assertIn("left as it is unless you tick a time", kept)
        self.assertNotIn("runs only when you type", kept)  # the kept schedule may still run
        self.assertIn('name="schedule_on">', page.render(self.view("schedule", form=replace(DRAFT, tags=frozenset()))))
        for launchd, form, said in [(True, on, "every day at 07:30"), (True, DRAFT, "not scheduled"),
                                    (False, on, "a cron line at 07:30, added by you"), (False, DRAFT, "not set here")]:
            summary = page.render(self.view("summary", form=form, launchd=launchd))
            self.assertIn(f"<th>When to run</th><td>{said}", summary)
            self.assertIn('value="goto:schedule"', summary)

    def test_the_email_step_says_how_to_get_an_app_password(self):
        html = page.render(self.view("email"))
        for said in ["https://myaccount.google.com/apppasswords", "2-Step Verification", "https://account.apple.com",
                     "App-Specific", "465 (SSL/TLS) or 587 (STARTTLS)", "Outlook.com"]:
            self.assertIn(said, html)
        links = re.findall(r"<a [^>]*>", html)
        self.assertEqual(len(links), 2)
        self.assertTrue(all('rel="noopener noreferrer"' in link and 'target="_blank"' in link for link in links))

    def test_after_save_the_page_says_what_now(self):
        html = page.render(self.view("done"))
        for said in ["JOBSCAN_HOME=&#x27;/h o&#x27;&quot;&#x27;&quot;&#x27;me&#x27; jobscan run",
                     "every day at 08:00", "/h/digests/ae", "emails it to me@gmail.com", "<code>jobscan ui</code>",
                     "<code>jobscan check</code>", "/h/runs.log", f'href="{page.REPOSITORY}/issues"',
                     "fork the repository", "pull request", "CONTRIBUTING.md", "Ctrl-C"]:
            self.assertIn(said, html)
        self.assertNotIn("crontab", html)
        bare = page.render(self.view("done", saved=replace(SAVED, at="", email_to="", cron="5 7 * * *  x % y")))
        self.assertNotIn("by itself", bare)
        self.assertNotIn("emails it", bare)
        self.assertIn("<pre>5 7 * * *  x % y</pre>", bare)
        self.assertIn("crontab -e", bare)
        self.assertTrue(all('rel="noopener noreferrer"' in a for a in re.findall(r"<a [^>]*>", html)))

    def test_one_question_a_screen(self):
        key = page.render(self.view("key"))
        self.assertIn('aria-label="Step 1 of 9" aria-valuemin="0" aria-valuemax="100" aria-valuenow="11">'
                      '<span style="width:11%">', key)  # the progress bar
        self.assertIn('<h1><span class="num">1 →</span>Your OpenRouter key</h1>', key)
        self.assertIn('aria-label="OpenRouter key"', key)
        self.assertIn('value="next" class="primary">Next</button>', key)
        self.assertIn("press <b>Enter ↵</b>", key)
        self.assertNotIn("Enter ↵", page.render(self.view("titles")))  # its box takes Enter as a new line
        self.assertIn("Enter ↵", page.render(self.view("summary")))  # the name box
        self.assertNotIn("Enter ↵", page.render(self.view("summary", fresh=False)))  # nothing to type in
        self.assertNotIn('class="progress"', page.render(self.view("intro")))
        self.assertNotIn('class="progress"', page.render(self.view("titles", fresh=False)))
        self.assertIn('<h1>Your search</h1>', page.render(self.view("summary")))
        self.assertIn('<label class="choice"><input type="checkbox" name="title" value="Sales Executive" checked>',
                      page.render(self.view("titles", form=replace(DRAFT, titles="Sales Executive"))))

    def test_what_most_people_skip_is_folded_away_unless_set(self):
        plain = page.render(self.view("email", form=page.Form()))
        self.assertIn("<details><summary>Another provider", plain)
        for typed in ({"email_host": "smtp.example.com"}, {"email_port": "587"}, {"email_from": "a@b.c"}):
            self.assertIn("<details open><summary>Another provider",
                          page.render(self.view("email", form=replace(page.Form(), **typed))), typed)
        self.assertIn("<details open><summary>Another provider",
                      page.render(self.view("email", form=page.Form(), errors={"email_port": "Bad port."})))
        self.assertIn("<details><summary>Tags of your own", page.render(self.view("tags", form=page.Form())))
        self.assertIn("<details open><summary>Tags of your own", page.render(self.view("tags")))  # DRAFT has rows
        self.assertIn("<details open><summary>Tags of your own", page.render(self.view(
            "tags", form=page.Form(), errors={"custom-0": "Bad."})))

    def test_a_preview_before_any_run_says_so(self):
        self.assertIn("No postings yet", page.render(self.view("summary", previewed=True)))

    def test_suggested_titles_join_the_offers_with_counts_when_there_are_some(self):
        suggested = Suggested(variants=("Sales Executive",), near_misses=("Account Manager",), usage=tag.Usage(0.002))
        offers = page.with_suggestions(page.Offers(), Outcome(suggested, Counted({"Sales Executive": 4},
                                                                                 {"Account Manager": 2})))
        self.assertEqual((offers.titles, offers.excludes, offers.adds, offers.leaves_out),
                         (("Sales Executive",), ("Account Manager",), {"Sales Executive": 4}, {"Account Manager": 2}))
        self.assertIn("$0.0020", offers.note)
        self.assertIn("Counts come after the first run.", page.with_suggestions(page.Offers(),
                                                                               Outcome(suggested, None)).note)

    def test_the_blocked_page_has_nothing_to_press(self):
        html = page.blocked("<b>can't</b>")
        self.assertIn("&lt;b&gt;", html)
        self.assertNotIn("<button", html)


if __name__ == "__main__":
    unittest.main()
