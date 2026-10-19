import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobscan import digest
from jobscan.config import Requirement
from jobscan.records import Posting
from jobscan.store import StoredPosting
from jobscan.tag import TagResult


def entry(job_id="1", title="Founding Account Executive", tags=None, open_roles=14):
    p = Posting(key=f"ashby:acme:{job_id}", ats="ashby", board="ashby:acme", company="Acme AI", title=title,
                location="London", url=f"https://jobs.ashbyhq.com/acme/{job_id}", description="")
    return digest.DigestEntry(posting=StoredPosting(posting=p, input_hash="h",
                                                    first_seen="2026-10-12T08:00:00.000000+00:00"), tags=tags or {},
                              open_roles=open_roles)


class RenderTest(unittest.TestCase):
    def test_header_and_entry(self):
        text = digest.render("david-ae", "2026-10-12", [entry()], carried_over=0)
        self.assertEqual(text, "# david-ae — 2026-10-12 · 1 new\n\n"
                               "## Acme AI — Founding Account Executive · London\n"
                               "https://jobs.ashbyhq.com/acme/1 · Ashby · first seen 2026-10-12 · 14 open roles\n")
        self.assertIn("· first seen 2026-10-12 · 1 open role\n", digest.render("a", "d", [entry(open_roles=1)], 0))

    def test_tag_lines(self):
        tags = {
            "founding": TagResult(value="yes", quote="our first sales hire", verified=True, warn=False),
            "segment": TagResult(value=("mid_market", "enterprise"), quote="mid-market and enterprise",
                                 verified=False, warn=True),
            "location_eligible": TagResult(value="yes", quote=None, verified=False, warn=True),
            "language_required": TagResult(value="no", quote=None, verified=False, warn=False),
            "tech_company": TagResult(value=(), quote=None, verified=False, warn=False),
        }
        lines = digest.render("ae", "2026-10-12", [entry(tags=tags)], carried_over=0).splitlines()[4:]
        self.assertEqual(lines, [
            '- founding: **yes** — "our first sales hire"',
            '- segment: **mid_market, enterprise** — "mid-market and enterprise" ⚠ quote not in posting',
            "- location_eligible: **yes** ⚠ no quote",
            "- language_required: no",
            "- tech_company: unknown",
        ])

    def test_unvalidated_tags_marked_with_one_legend(self):
        tags = {"founding": TagResult(value="yes", quote="first", verified=True, warn=False),
                "climate": TagResult(value="unknown", quote=None, verified=False, warn=False)}
        text = digest.render("pm", "2026-10-12", [entry(tags=tags), entry("2", tags=tags)], carried_over=0,
                             unvalidated=frozenset({"climate"}))
        self.assertEqual(text.count(digest.LEGEND), 1)
        self.assertTrue(text.startswith("# pm — 2026-10-12 · 2 new\n\n" + digest.LEGEND + "\n\n## "))
        self.assertEqual(text.count("- climate ⓘ: unknown"), 2)
        self.assertEqual(text.count('- founding: **yes** — "first"'), 2)

    def test_no_legend_without_unvalidated_tags_shown(self):
        tags = {"founding": TagResult(value="yes", quote="first", verified=True, warn=False)}
        for entries in ([entry(tags=tags)], []):
            text = digest.render("pm", "2026-10-12", entries, carried_over=1, unvalidated=frozenset({"climate"}))
            self.assertNotIn("ⓘ", text)

    def test_header_counts_carry_over(self):
        text = digest.render("david-ae", "2026-10-12", [], carried_over=3)
        self.assertEqual(text, "# david-ae — 2026-10-12 · 0 new · 3 untagged, carried over\n")

    def test_left_out_postings_follow_the_entries_one_line_each(self):
        german = {"founding": TagResult(value="no", quote=None, verified=False, warn=False),
                  "segment": TagResult(value=(), quote=None, verified=False, warn=False),
                  "language_required": TagResult(value="German", quote="German speaker", verified=True, warn=False)}
        left = [digest.LeftOut(entry("2", "Account Executive - German Speaking", tags=german),
                               (Requirement("founding", "yes"), Requirement("language_required", "no")))]
        text = digest.render("ae", "2026-10-12", [entry()], carried_over=1, unvalidated=frozenset({"segment"}),
                             left_out=left)
        self.assertTrue(text.startswith("# ae — 2026-10-12 · 1 new · 1 untagged, carried over · 1 left out\n\n## "))
        self.assertTrue(text.endswith(
            "\n\n## Left out (1)\n" + digest.LEFT_OUT_NOTE + "\n"
            "- Acme AI — Account Executive - German Speaking · London · founding: no · language_required: German · "
            "https://jobs.ashbyhq.com/acme/2\n"))
        self.assertNotIn(digest.LEGEND, text)  # the legend keys on entries' tags, not the left-out ones'


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
