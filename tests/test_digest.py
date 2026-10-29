import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from jobscan import digest
from jobscan.config import Requirement
from jobscan.records import Posting
from jobscan.store import StoredPosting
from jobscan.tag import TagResult


def entry(job_id="1", title="Founding Account Executive", tags=None, open_roles=14, watched=False, notes=()):
    p = Posting(key=f"ashby:acme:{job_id}", ats="ashby", board="ashby:acme", company="Acme AI", title=title,
                location="London", url=f"https://jobs.ashbyhq.com/acme/{job_id}", description="")
    return digest.DigestEntry(posting=StoredPosting(posting=p, input_hash="h",
                                                    first_seen="2026-10-12T08:00:00.000000+00:00"), tags=tags or {},
                              open_roles=open_roles, watched=watched, notes=notes)


def said(value, quote=None, verified=True, warn=False):
    return TagResult(value=value, quote=quote, verified=verified, warn=warn)


UNKNOWN = said("unknown", verified=False)


class RenderTest(unittest.TestCase):
    def test_header_and_entry(self):
        text = digest.render("david-ae", "2026-10-12", [entry()], carried_over=0)
        self.assertEqual(text, "# david-ae — 2026-10-12 · 1 new\n\n"
                               "## Acme AI — Founding Account Executive · London\n"
                               "https://jobs.ashbyhq.com/acme/1 · Ashby · 14 open roles\n")
        self.assertIn("· Ashby · 1 open role\n", digest.render("a", "2026-10-12", [entry(open_roles=1)], 0))

    def test_first_seen_only_before_the_digest_date_then_the_website_on_the_link_line(self):
        sited = replace(entry(), website="https://acme.ai/")
        self.assertIn("\nhttps://jobs.ashbyhq.com/acme/1 · Ashby · 14 open roles · https://acme.ai/\n",
                      digest.render("pm", "2026-10-12", [sited], 0))
        self.assertIn("\nhttps://jobs.ashbyhq.com/acme/1 · Ashby · 14 open roles · first seen 2026-10-12 · "
                      "https://acme.ai/\n", digest.render("pm", "2026-10-14", [sited], 0))
        self.assertNotIn("Company:", digest.render("pm", "2026-10-12", [sited], 0))

    def test_first_seen_compares_the_machines_date_not_utcs(self):
        def seen(stamp):
            plain = entry()
            return replace(plain, posting=replace(plain.posting, first_seen=stamp))
        self.addCleanup(time.tzset)
        for zone, stamp, shown in [("Australia/Sydney", "2026-10-11T22:00:00.000000+00:00", None),
                                   ("America/Los_Angeles", "2026-10-12T05:00:00.000000+00:00", "2026-10-11")]:
            with self.subTest(zone), mock.patch.dict(os.environ, {"TZ": zone}):
                time.tzset()
                text = digest.render("pm", "2026-10-12", [seen(stamp)], 0)
                self.assertEqual("first seen" in text, shown is not None)
                if shown:
                    self.assertIn(f"· first seen {shown}\n", text)

    def test_watched_entries_lead_in_their_own_order_marked_with_one_legend(self):
        entries = [entry("1"), entry("2", watched=True), entry("3"), entry("4", watched=True)]
        text = digest.render("pm", "2026-10-12", entries, carried_over=0)
        self.assertTrue(text.startswith("# pm — 2026-10-12 · 4 new · 2 watched\n\n" + digest.WATCH_LEGEND + "\n\n"))
        self.assertEqual([line.split("/")[-1].split(" ")[0] for line in text.splitlines() if line.startswith("https://")],
                         ["2", "4", "1", "3"])
        self.assertEqual(text.count("## ★ Acme AI — "), 2)
        self.assertNotIn("★", digest.render("pm", "2026-10-12", [entry()], carried_over=0))

    def test_a_watched_left_out_posting_is_starred_and_explained_but_not_counted(self):
        german = {"language_required": TagResult(value="German", quote="German", verified=True, warn=False)}
        text = digest.render("pm", "2026-10-12", [entry()], carried_over=0, left_out=[digest.LeftOut(
            entry("2", tags=german, watched=True), (Requirement("language_required", ("no",)),))])
        self.assertTrue(text.startswith("# pm — 2026-10-12 · 1 new · 1 left out\n\n" + digest.WATCH_LEGEND))
        self.assertIn("\n- ★ Acme AI — Founding Account Executive · London · language_required: German · ", text)
        self.assertIn("\n## Acme AI — ", text)

    def test_notes_follow_the_link_line_one_line_each_before_the_tags_line(self):
        noted = replace(entry(tags={"founding": said("yes", "first")}, notes=("applied 29 Sep", "talking\n## to Sam")),
                        website="https://acme.ai/")
        lines = digest.render("pm", "2026-10-12", [noted], carried_over=0).splitlines()
        self.assertTrue(lines[3].endswith(" · https://acme.ai/"))
        self.assertEqual(lines[4:], ["Your note: applied 29 Sep", "Your note: talking ## to Sam", "✅ founding"])
        self.assertNotIn("Your note", digest.render("pm", "2026-10-12", [entry()], carried_over=0))
        german = {"language_required": TagResult(value="German", quote="German", verified=True, warn=False)}
        text = digest.render("pm", "2026-10-12", [entry()], carried_over=0, left_out=[digest.LeftOut(
            entry("2", tags=german, notes=("applied",)), (Requirement("language_required", ("no",)),))])
        self.assertNotIn("Your note", text)

    def test_pay_follows_the_link_line_as_the_site_writes_it(self):
        plain = entry()
        stored = replace(plain.posting, posting=replace(plain.posting.posting, salary="£85K to £120K • Offers Equity"))
        lines = digest.render("pm", "2026-10-12", [replace(plain, posting=stored)], carried_over=0).splitlines()
        self.assertEqual(lines[4:], ["Pay: £85K to £120K • Offers Equity"])
        self.assertNotIn("Pay:", digest.render("pm", "2026-10-12", [entry()], carried_over=0))

    def test_about_is_a_line_after_pay_before_notes_and_the_tags_line(self):
        about = said("Call-recording software\nfor sales teams", "records every sales call")
        plain = entry(tags={"founding": said("yes", "first"), "about": about}, notes=("applied",))
        stored = replace(plain.posting, posting=replace(plain.posting.posting, salary="£85K"))
        lines = digest.render("pm", "2026-10-12", [replace(plain, posting=stored)], carried_over=0,
                              view=digest.TagView(unvalidated=frozenset({"about"}))).splitlines()
        self.assertEqual(lines[0:3], ["# pm — 2026-10-12 · 1 new", "", f"{digest.LEGEND} about."])
        self.assertEqual(lines[6:], ["Pay: £85K", "About: Call-recording software for sales teams",
                                     "Your note: applied", "✅ founding"])  # about is not on the tags line

    def test_about_shows_its_warning_and_no_line_when_unknown(self):
        flagged = said("Ads for brands", "not there", verified=False, warn=True)
        self.assertIn("\nAbout: Ads for brands ⚠ quote not in posting\n",
                      digest.render("pm", "2026-10-12", [entry(tags={"about": flagged})], 0))
        text = digest.render("pm", "2026-10-12", [entry(tags={"about": UNKNOWN})], 0,
                             view=digest.TagView(unvalidated=frozenset({"about"})))
        self.assertNotIn("About", text)
        self.assertNotIn("unknown", text)
        self.assertNotIn(digest.LEGEND, text)  # nothing unvalidated shown

    def test_the_tags_line_forms_without_quotes(self):
        tags = {
            "founding": said("yes", "our first sales hire"),
            "segment": said(("mid_market", "enterprise"), "mid-market and enterprise", verified=False, warn=True),
            "location_eligible": said("yes", verified=False, warn=True),
            "language_required": said("no"),
            "tech_company": said("no", "a media agency"),
            "climate": UNKNOWN,
            "reports_to": said("VP\nSales", "reports to our VP Sales"),
        }
        text = digest.render("ae", "2026-10-12", [entry(tags=tags)], carried_over=0,
                             view=digest.TagView(defaults={"language_required": "no"}))
        self.assertEqual(text.splitlines()[4:], [
            "✅ founding · segment: mid_market, enterprise ⚠ quote not in posting · ✅ location_eligible ⚠ no quote"
            " · ❌ tech_company · reports_to: VP Sales"])
        for quote in ("our first sales hire", "mid-market and enterprise", "a media agency", "reports to our", "\""):
            self.assertNotIn(quote, text)

    def test_defaults_and_settled_answers_go_unsaid_unless_flagged_and_required_unknowns_show(self):
        view = digest.TagView(required=frozenset({"location_eligible", "language_required", "reports_to"}),
                              settled=frozenset({"location_eligible", "language_required", "reports_to"}),
                              defaults={"founding": "no", "language_required": "no", "reports_to": "unknown"})
        expected = {"location_eligible": said("yes", "London"), "language_required": said("no"),
                    "reports_to": said("ceo", "reports to the CEO")}
        quiet_day = entry("1", tags={"founding": said("no", "a sales team of 40"), **expected})
        unfound = entry("2", tags={"founding": said("no", "not there", verified=False), **expected})
        unknowns = entry("3", tags={"founding": said("no"), "location_eligible": UNKNOWN,
                                    "language_required": said("no"), "reports_to": UNKNOWN})
        lines = digest.render("ae", "2026-10-12", [quiet_day, unfound, unknowns], 0, view=view).split("\n\n")[1:]
        self.assertEqual([block.splitlines()[2:] for block in lines], [
            [], ["❌ founding ⚠ quote not in posting"], ["❓ location_eligible · ❓ reports_to"]])

    def test_a_list_answer_shows_whole_and_an_empty_one_only_when_required(self):
        required = digest.TagView(required=frozenset({"segment"}))
        enterprise = entry("1", tags={"segment": said(("enterprise", "smb"), "enterprise and SMB")})
        empty = entry("2", tags={"segment": said(())})
        text = digest.render("ae", "2026-10-12", [enterprise, empty], 0, view=required)
        self.assertIn("\nsegment: enterprise, smb\n", text)
        self.assertTrue(text.endswith("/2 · Ashby · 14 open roles\n❓ segment\n"), text)
        self.assertTrue(digest.render("ae", "2026-10-12", [empty], 0).endswith(" · 14 open roles\n"))

    def test_a_custom_unknown_in_any_case_goes_unsaid_with_no_empty_line(self):
        text = digest.render("pm", "2026-10-12", [entry(tags={"stage": said("Unknown")})], 0)
        self.assertTrue(text.endswith("· 14 open roles\n"), text)

    def test_the_legend_names_the_unvalidated_tags_shown_in_tag_order_once(self):
        first = entry("1", tags={"segment": UNKNOWN, "tech_company": said("yes", "software"), "founding": said("yes")})
        later = entry("2", tags={"segment": said(("enterprise",), "enterprise"), "tech_company": UNKNOWN,
                                 "founding": said("yes")}, watched=True)
        view = digest.TagView(unvalidated=frozenset({"segment", "tech_company", "climate"}))
        for entries in ([first, later], [later, first], [first, replace(later, watched=False)]):
            text = digest.render("pm", "2026-10-12", entries, 0, view=view)
            self.assertEqual(text.count(digest.LEGEND), 1)
            self.assertIn(f"\n{digest.LEGEND} segment, tech_company.\n", text)
        self.assertIn(f"{digest.LEGEND} tech_company.", digest.render("pm", "2026-10-12", [first], 0, view=view))

    def test_no_legend_without_unvalidated_tags_shown(self):
        view = digest.TagView(unvalidated=frozenset({"climate"}))
        for entries in ([entry(tags={"founding": said("yes", "first"), "climate": UNKNOWN})], []):
            text = digest.render("pm", "2026-10-12", entries, carried_over=1, view=view)
            self.assertNotIn(digest.LEGEND, text)

    def test_header_counts_carry_over(self):
        text = digest.render("david-ae", "2026-10-12", [], carried_over=3)
        self.assertEqual(text, "# david-ae — 2026-10-12 · 0 new · 3 untagged, carried over\n")

    def test_unread_boards_get_a_line_under_the_header(self):
        text = digest.render("ae", "2026-10-12", [entry()], carried_over=0, unread=digest.Unread(2040, 20700))
        self.assertTrue(text.startswith(
            "# ae — 2026-10-12 · 1 new\n\n2,040 of 20,700 boards couldn't be read this run, most often the network "
            "down or the Mac asleep; the next run reads them.\n\n## "), text)
        self.assertNotIn("couldn't be read", digest.render("ae", "2026-10-12", [entry()], carried_over=0))

    def test_left_out_postings_follow_the_entries_one_line_each(self):
        german = {"founding": TagResult(value="no", quote=None, verified=False, warn=False),
                  "segment": TagResult(value=(), quote=None, verified=False, warn=False),
                  "language_required": TagResult(value="German", quote="German speaker", verified=True, warn=False)}
        left = [digest.LeftOut(entry("2", "Account Executive - German Speaking", tags=german),
                               (Requirement("founding", ("yes",)), Requirement("language_required", ("no",))))]
        text = digest.render("ae", "2026-10-12", [entry()], carried_over=1,
                             view=digest.TagView(unvalidated=frozenset({"segment"})), left_out=left)
        self.assertTrue(text.startswith("# ae — 2026-10-12 · 1 new · 1 untagged, carried over · 1 left out\n\n## "))
        self.assertTrue(text.endswith(
            "\n\n## Left out (1)\n" + digest.LEFT_OUT_NOTE + "\n"
            "- Acme AI — Account Executive - German Speaking · London · founding: no · language_required: German · "
            "https://jobs.ashbyhq.com/acme/2\n"))
        self.assertNotIn(digest.LEGEND, text)  # the legend keys on entries' tags, not the left-out ones'

    def test_a_quiet_tag_shows_only_on_a_left_out_row(self):
        tags = {"founding": said("no"), "agency": said("no")}
        agency = {**tags, "agency": said("yes", "Our client")}
        view = digest.TagView(unvalidated=frozenset({"agency"}), quiet=frozenset({"agency"}))
        text = digest.render("ae", "2026-10-12", [entry(tags=tags)], carried_over=0, view=view,
                             left_out=[digest.LeftOut(entry("2", tags=agency), (Requirement("agency", ("no",)),))])
        self.assertNotIn("❌ agency", text)
        self.assertNotIn(digest.LEGEND, text)  # a quiet unvalidated tag shows on no entry, so no legend
        self.assertIn("· London · agency: yes · https://jobs.ashbyhq.com/acme/2\n", text)
        self.assertIn("\n❌ founding\n", text)


class PublishTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "digests" / "ae"

    def test_same_day_digests_get_suffixes_and_nothing_is_overwritten(self):
        paths = [digest.publish(self.dir, "2026-10-12", f"run {n}\n") for n in range(3)]
        self.assertEqual([p.name for p in paths], ["2026-10-12.md", "2026-10-12-2.md", "2026-10-12-3.md"])
        self.assertEqual([p.read_text() for p in paths], ["run 0\n", "run 1\n", "run 2\n"])
        self.assertEqual(sorted(f.name for f in self.dir.iterdir()), sorted(p.name for p in paths))

    def test_file_appearing_after_name_choice_survives(self):
        real_link = os.link

        def racing_link(src, dst):
            if Path(dst).name == "2026-10-12.md" and not Path(dst).exists():
                Path(dst).write_text("someone else's digest\n")
            return real_link(src, dst)

        with mock.patch.object(digest.os, "link", racing_link):
            path = digest.publish(self.dir, "2026-10-12", "mine\n")
        self.assertEqual(path.name, "2026-10-12-2.md")
        self.assertEqual((self.dir / "2026-10-12.md").read_text(), "someone else's digest\n")
        self.assertEqual(path.read_text(), "mine\n")

    def test_unwritable_directory_raises(self):
        self.dir.parent.mkdir(parents=True)
        self.dir.write_text("a file where the directory should be")
        with self.assertRaises(OSError):
            digest.publish(self.dir, "2026-10-12", "x\n")


if __name__ == "__main__":
    unittest.main()
