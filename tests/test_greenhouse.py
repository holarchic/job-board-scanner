"""Greenhouse adapter: board URLs → boards, and Job Board API payloads → postings."""
import unittest

from jobscan import fetch, greenhouse, tag
from jobscan.records import BoardError
from tests.fakes import greenhouse_job

API = "https://boards-api.greenhouse.io/v1/boards/"


class BoardTest(unittest.TestCase):
    def test_every_url_form_is_one_board(self):
        for url in ["https://job-boards.greenhouse.io/acme", "https://job-boards.greenhouse.io/ACME/jobs/123",
                    "https://job-boards.eu.greenhouse.io/acme", "https://boards.greenhouse.io/acme",
                    "https://boards.eu.greenhouse.io/acme", "https://boards.greenhouse.io/embed/job_board?for=Acme",
                    "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
                    "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"]:
            b = fetch.board_for("Acme", url)
            self.assertEqual((b.id, b.api_url), ("greenhouse:acme", API + "acme/jobs?content=true"), url)

    def test_encoded_tokens_are_one_board_and_encoded_again_for_the_api(self):
        for url in ["https://job-boards.greenhouse.io/ep%2Bco", "https://job-boards.greenhouse.io/EP+Co",
                    "https://boards.greenhouse.io/embed/job_board?for=ep%2Bco"]:
            b = fetch.board_for("", url)
            self.assertEqual((b.id, b.company, b.api_url),
                             ("greenhouse:ep+co", "ep+co", API + "ep%2Bco/jobs?content=true"), url)
        self.assertEqual(fetch.board_for("", "https://job-boards.greenhouse.io/harrison%26star").api_url,
                         API + "harrison%26star/jobs?content=true")

    def test_semicolons_stay_in_the_token(self):
        for url in ["https://job-boards.greenhouse.io/x;y", "https://job-boards.greenhouse.io/x%3By",
                    "https://job-boards.greenhouse.io/x;y/jobs/1"]:
            self.assertEqual(fetch.board_for("", url).id, "greenhouse:x;y", url)

    def test_host_case_and_default_port_are_one_board(self):
        for url in ["https://Job-Boards.Greenhouse.io/acme", "https://job-boards.greenhouse.io:443/acme"]:
            self.assertEqual(fetch.board_for("", url).id, "greenhouse:acme", url)

    def test_api_url_on_another_host_is_kept(self):
        b = fetch.board_for("Acme", "http://127.0.0.1:8000/v1/boards/acme/jobs")
        self.assertEqual(b.api_url, "http://127.0.0.1:8000/v1/boards/acme/jobs?content=true")

    def test_other_urls_are_not_greenhouse(self):
        self.assertEqual(fetch.board_for("", "https://jobs.ashbyhq.com/acme").ats, "ashby")
        for url in ["https://jobs.lever.co/acme", "https://job-boards.greenhouse.io/",
                    "https://boards.greenhouse.io/embed/job_board", "https://job-boards.greenhouse.io/a%3Ab",
                    "https://job-boards.greenhouse.io/a%2Fb", "https://job-boards.greenhouse.io/a%20b",
                    "https://www.example.com/acme", "ftp://job-boards.greenhouse.io/acme", "not a url"]:
            self.assertIsNone(fetch.board_for("", url), url)


class ParseTest(unittest.TestCase):
    board = fetch.board_for("Acme", "https://job-boards.greenhouse.io/acme")

    def test_posting_fields(self):
        (p,) = greenhouse.parse(self.board, {"jobs": [greenhouse_job(42, "Account Executive", "London, UK",
                                                                     offices=["London", "London, UK", "Remote (UK)"])]})
        self.assertEqual((p.key, p.ats, p.board, p.company, p.title, p.location, p.url),
                         ("greenhouse:acme:42", "greenhouse", "greenhouse:acme", "Acme", "Account Executive",
                          "London, UK / London / Remote (UK)", "https://job-boards.greenhouse.io/acme/jobs/42"))

    def test_escaped_once_or_twice_gives_the_same_text(self):
        markup = ("<div><p>You'll be our <strong>first sales hire</strong> &amp; more.</p>"
                  "<ul><li>Own EMEA</li></ul></div>")
        once = markup.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        twice = once.replace("&", "&amp;")
        for content in (once, twice, markup):
            (p,) = greenhouse.parse(self.board, {"jobs": [greenhouse_job(1, "AE", content=content)]})
            self.assertEqual(p.description, "You'll be our first sales hire & more.\nOwn EMEA", content[:20])
            self.assertTrue(tag.verify_quote("our first sales hire & more", p))

    def test_literal_escaped_text_survives_any_number_of_escapings(self):
        markup = "<p>Use &lt;customer&gt; APIs.</p>"
        once = markup.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        for content in (markup, once, once.replace("&", "&amp;")):
            (p,) = greenhouse.parse(self.board, {"jobs": [greenhouse_job(1, "AE", content=content)]})
            self.assertEqual(p.description, "Use <customer> APIs.", content)

    def test_quoted_angle_brackets_and_bare_literals(self):
        quoted = '&lt;p&gt;Our &lt;strong title="a &amp;gt; b"&gt;first sales hire&lt;/strong&gt;&lt;/p&gt;'
        for content, text in [(quoted, "Our first sales hire"),
                              ("Use &lt;customer&gt; APIs.", "Use <customer> APIs.")]:
            (p,) = greenhouse.parse(self.board, {"jobs": [greenhouse_job(1, "AE", content=content)]})
            self.assertEqual(p.description, text, content)

    def test_any_tag_in_any_case_unescapes_alike(self):
        markup = "<ARTICLE><P>Our <MARK>first sales hire</MARK>.</P></ARTICLE>"
        once = markup.replace("<", "&lt;").replace(">", "&gt;")
        for content in (markup, once, once.replace("&", "&amp;")):
            (p,) = greenhouse.parse(self.board, {"jobs": [greenhouse_job(1, "AE", content=content)]})
            self.assertEqual(p.description, "Our first sales hire.", content)
            self.assertTrue(tag.verify_quote("Our first sales hire", p))

    def test_break_only_and_unclosed_paragraphs_unescape_alike(self):
        for markup, text in [("First sales hire<BR>Own EMEA", "First sales hire\nOwn EMEA"),
                             ("<p>First sales hire<p>Own EMEA", "First sales hire\nOwn EMEA"),
                             ("First sales hire Own EM<wbr>EA", "First sales hire Own EMEA")]:
            once = markup.replace("<", "&lt;").replace(">", "&gt;")
            for content in (markup, once, once.replace("&", "&amp;")):
                (p,) = greenhouse.parse(self.board, {"jobs": [greenhouse_job(1, "AE", content=content)]})
                self.assertEqual(p.description, text, content)
                self.assertTrue(tag.verify_quote("First sales hire Own EMEA", p))

    def test_ids_may_be_numbers_or_strings(self):
        postings = greenhouse.parse(self.board, {"jobs": [greenhouse_job(1, "A"), greenhouse_job("x2", "B")]})
        self.assertEqual([p.key for p in postings], ["greenhouse:acme:1", "greenhouse:acme:x2"])

    def test_missing_location_and_offices_are_empty(self):
        job = greenhouse_job(1, "AE")
        job["location"], job["offices"] = None, None
        self.assertEqual(greenhouse.parse(self.board, {"jobs": [job]})[0].location, "")

    def test_a_malformed_job_fails_the_board(self):
        bad = []
        for change in [{"id": True}, {"id": None}, {"id": " "}, {"title": None}, {"title": 3}, {"location": "London"},
                       {"offices": "London"}, {"offices": [3]}, {"offices": [None]}, {"content": 5},
                       {"absolute_url": 1}, {"title": "\ud800"}, {"id": "a\ud800"}]:
            bad.append({**greenhouse_job(1, "AE"), **change})
        for job in [*bad, "not a job"]:
            with self.assertRaises(BoardError, msg=job):
                greenhouse.parse(self.board, {"jobs": [job]})
        for data in [{}, {"jobs": "x"}, [], {"jobs": [greenhouse_job(1, "A"), greenhouse_job(1, "B")]}]:
            with self.assertRaises(BoardError, msg=data):
                greenhouse.parse(self.board, data)


if __name__ == "__main__":
    unittest.main()
