"""Gem adapter: board URLs → boards, and job board API payloads → postings."""
import unittest

from jobscan import gem, prefilter, tag
from jobscan.config import Profile
from jobscan.records import Board, BoardError
from tests.fakes import anywhere, gem_job

API = "https://api.gem.com/job_board/v0/"


def board_for(url):
    return gem.api_board("Acme", url) or gem.page_board("Acme", url)


class BoardTest(unittest.TestCase):
    def test_every_url_form_is_one_board(self):
        for url in ["https://jobs.gem.com/acme", "https://jobs.gem.com/acme/am9icG9zdDpC",
                    "https://Jobs.Gem.com:443/acme/", "https://api.gem.com/job_board/v0/acme/job_posts/",
                    "https://api.gem.com/job_board/v0/acme"]:
            b = board_for(url)
            self.assertEqual((b.id, b.company, b.api_url), ("gem:acme", "Acme", API + "acme/job_posts/"), url)

    def test_slugs_keep_their_case_and_are_encoded_again(self):
        self.assertEqual(board_for("https://jobs.gem.com/BigPanda").id, "gem:BigPanda")
        b = board_for("https://jobs.gem.com/-joinforage%2B")
        self.assertEqual((b.id, b.api_url), ("gem:-joinforage+", API + "-joinforage%2B/job_posts/"))

    def test_api_url_on_another_host_is_kept(self):
        self.assertEqual(board_for("http://127.0.0.1:8000/job_board/v0/acme/job_posts/").api_url,
                         "http://127.0.0.1:8000/job_board/v0/acme/job_posts/")

    def test_other_urls_are_not_gem_boards(self):
        for url in ["https://jobs.gem.com/", "https://jobs.gem.com/a%3Ab", "https://api.gem.com/job_board/v0/",
                    "https://www.gem.com/acme", "https://jobs.ashbyhq.com/acme", "not a url"]:
            self.assertIsNone(board_for(url), url)


    def test_sandbox_boards_are_not_boards(self):
        for url in ["https://jobs.gem.com/vercel-ats-sandbox", "https://jobs.gem.com/SandboxSchonfeld-com/am9i",
                    "https://api.gem.com/job_board/v0/atssandboxyello-co/job_posts/"]:
            self.assertIsNone(board_for(url), url)

    def test_a_sandbox_board_already_listed_reads_as_empty_without_a_request(self):
        board = Board("gem", "acme-sandbox", "acme-sandbox", API + "acme-sandbox/job_posts/")
        self.assertEqual(gem.read(board, lambda url: self.fail(f"asked {url}"), anywhere), ())


class ParseTest(unittest.TestCase):
    board = gem.page_board("Acme", "https://jobs.gem.com/acme")

    def test_posting_fields(self):
        job = gem_job("am9i", " Account Executive ", "London, UK",
                      offices=[("London HQ", "London, UK"), ("Remote - UK", "United Kingdom")],
                      content_plain="\nAccount Executive role. We sell software.\n")
        (p,) = gem.parse(self.board, [job])
        self.assertEqual((p.key, p.ats, p.board, p.company, p.title, p.location, p.url),
                         ("gem:acme:am9i", "gem", "gem:acme", "Acme", "Account Executive",
                          "London, UK / London HQ / Remote - UK / United Kingdom", "https://jobs.gem.com/acme/am9i"))
        self.assertEqual(p.description, "Account Executive role. We sell software.")

    def test_remote_jobs_say_so_once(self):
        for location, expected in [("United States", "United States / Remote"),
                                   ("United States - Remote", "United States - Remote")]:
            job = gem_job("1", "AE", location, location_type="remote")
            self.assertEqual(gem.parse(self.board, [job])[0].location, expected)

    def test_remote_is_added_unless_a_name_says_remote_as_a_word(self):
        remote_only = Profile(name="r", titles=("AE",), exclude_titles=(), locations=("Remote",), eligible_where="",
                              tags=(), custom_tags=())
        for location, offices in [("Work remotely", ()), ("United States", [("RemoteHQ", "")])]:
            (p,) = gem.parse(self.board, [gem_job("1", "AE", location, offices=offices, location_type="remote")])
            self.assertTrue(p.location.endswith(" / Remote"), p.location)
            self.assertTrue(prefilter.matches(p, remote_only), p.location)

    def test_missing_location_and_offices_are_empty(self):
        for change in [{"location": None, "offices": None}, {"location": {"name": ""}, "offices": []}]:
            self.assertEqual(gem.parse(self.board, [{**gem_job("1", "AE"), **change}])[0].location, "", change)

    def test_html_description_when_the_plain_text_is_blank(self):
        job = {**gem_job("1", "AE"), "content_plain": " ",
               "content": "<div><p>You'll be our <strong>first sales hire</strong> &amp; more.</p>"
                          "<ul><li>Own EMEA</li></ul></div>"}
        (p,) = gem.parse(self.board, [job])
        self.assertEqual(p.description, "You'll be our first sales hire & more.\nOwn EMEA")
        self.assertTrue(tag.verify_quote("our first sales hire & more", p))

    def test_a_malformed_job_fails_the_board(self):
        bad = [{**gem_job("1", "AE"), **change} for change in [
            {"id": None}, {"id": " "}, {"id": 3}, {"title": None}, {"title": 3}, {"location": "London"},
            {"location": {"name": 3}}, {"offices": "London"}, {"offices": [None]},
            {"offices": [{"name": "HQ", "location": "London"}]}, {"content_plain": 5}, {"content": 5},
            {"absolute_url": 1}, {"location_type": 1}, {"title": "\ud800"}]]
        for job in [*bad, "not a job"]:
            with self.assertRaises(BoardError, msg=job):
                gem.parse(self.board, [gem_job("ok", "AE"), job])
        for data in [{}, {"jobs": []}, None, [gem_job("1", "A"), gem_job("1", "B")]]:
            with self.assertRaises(BoardError, msg=data):
                gem.parse(self.board, data)

    def test_a_demo_job_is_skipped_and_the_rest_of_the_board_read(self):
        jobs = [gem_job("1", "Enterprise Account Executive", content_plain="\n***DEMO JOB POST***\n"),
                {**gem_job("2", "CFO"), "content_plain": "", "content": "<p>***DEMO JOB POST***</p>"},
                gem_job("3", "AE", content_plain="Not a ***DEMO JOB POST*** at all."), gem_job("4", "SE"),
                gem_job("5", "AE", content_plain="Acme is the leading platform.\n ***DEMO JOB POST*** \nApply!")]
        self.assertEqual([p.key for p in gem.parse(self.board, jobs)], ["gem:acme:3", "gem:acme:4"])

    def test_no_jobs_is_an_empty_board(self):
        self.assertEqual(gem.parse(self.board, []), ())


if __name__ == "__main__":
    unittest.main()
