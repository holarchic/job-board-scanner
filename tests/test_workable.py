"""Workable adapter: board URLs → boards, and widget-API payloads → postings."""
import unittest

from jobscan import tag, workable
from jobscan.records import BoardError
from tests.fakes import workable_job

API = "https://apply.workable.com/api/v1/widget/accounts/"


def board_for(url):
    return workable.api_board("Acme", url) or workable.page_board("Acme", url)


class BoardTest(unittest.TestCase):
    def test_every_url_form_is_one_board(self):
        for url in ["https://apply.workable.com/acme", "https://apply.workable.com/acme/",
                    "https://apply.workable.com/acme/j/599813E1DD/", "https://Apply.Workable.com:443/acme",
                    "https://apply.workable.com/api/v1/widget/accounts/acme",
                    "https://apply.workable.com/api/v1/widget/accounts/acme?details=true"]:
            b = board_for(url)
            self.assertEqual((b.id, b.company, b.api_url), ("workable:acme", "Acme", API + "acme?details=true"), url)

    def test_slugs_keep_their_case_and_are_encoded_again(self):
        self.assertEqual(board_for("https://apply.workable.com/HuggingFace").id, "workable:HuggingFace")
        b = board_for("https://apply.workable.com/ep%2Bco")
        self.assertEqual((b.id, b.api_url), ("workable:ep+co", API + "ep%2Bco?details=true"))

    def test_api_url_on_another_host_is_kept(self):
        b = board_for("http://127.0.0.1:8000/api/v1/widget/accounts/acme")
        self.assertEqual(b.api_url, "http://127.0.0.1:8000/api/v1/widget/accounts/acme?details=true")

    def test_other_urls_are_not_workable_boards(self):
        for url in ["https://apply.workable.com/", "https://apply.workable.com/j/599813E1DD",
                    "https://apply.workable.com/%6a/599813E1DD", "https://apply.workable.com/api",
                    "https://apply.workable.com/api/v1/widget/accounts/", "https://apply.workable.com/a%3Ab",
                    "https://apply.workable.com/a%2Fb", "https://jobs.workable.com/company/x/jobs-at-acme",
                    "https://acme.workable.com/", "https://jobs.ashbyhq.com/acme", "not a url"]:
            self.assertIsNone(board_for(url), url)


class ParseTest(unittest.TestCase):
    board = workable.page_board("Acme", "https://apply.workable.com/acme")

    def test_posting_fields(self):
        (p,) = workable.parse(self.board, {"name": "Acme", "jobs": [workable_job("AB12", " Account Executive ")]})
        self.assertEqual((p.key, p.ats, p.board, p.company, p.title, p.location, p.url),
                         ("workable:acme:AB12", "workable", "workable:acme", "Acme", "Account Executive",
                          "London, United Kingdom", "https://apply.workable.com/j/AB12"))
        self.assertEqual(p.description, "Account Executive role. We sell software.")

    def test_a_job_listed_once_per_location_is_one_posting_with_every_location(self):
        jobs = [workable_job("AB12", "AE", "London"), workable_job("CD34", "SE", "Paris", "France"),
                workable_job("AB12", "AE", "Manchester"), workable_job("AB12", "AE", "London")]
        postings = workable.parse(self.board, {"jobs": jobs})
        self.assertEqual([(p.key, p.location) for p in postings],
                         [("workable:acme:AB12", "London, United Kingdom / Manchester, United Kingdom"),
                          ("workable:acme:CD34", "Paris, France")])

    def test_repeats_that_disagree_fail_the_board(self):
        for change in [{"title": "Senior AE"}, {"description": "<p>Another ad.</p>"}]:
            jobs = [workable_job("AB12", "AE", "London"), {**workable_job("AB12", "AE", "Paris"), **change}]
            with self.assertRaisesRegex(BoardError, "repeats with a different", msg=change):
                workable.parse(self.board, {"jobs": jobs})

    def test_locations_regions_hidden_records_and_remote(self):
        job = workable_job("AB12", "AE", telecommuting=True, locations=[
            {"city": "London", "region": "England", "country": "United Kingdom", "hidden": False},
            {"city": "", "region": None, "country": "Ireland", "hidden": True},
            {"city": "London", "region": "England", "country": "United Kingdom", "hidden": True}])
        self.assertEqual(workable.parse(self.board, {"jobs": [job]})[0].location,
                         "London, England, United Kingdom / Ireland / Remote")

    def test_without_location_records_the_jobs_own_fields_are_used(self):
        for locations in ([], None):
            job = workable_job("AB12", "AE", locations=locations, city="Porto", state="Porto District",
                               country="Portugal")
            self.assertEqual(workable.parse(self.board, {"jobs": [job]})[0].location,
                             "Porto, Porto District, Portugal", locations)

    def test_html_description_becomes_text_quotes_can_match(self):
        job = workable_job("AB12", "AE", description="<p>You'll be our <strong>first sales hire</strong> &amp; more."
                                                      "</p><ul><li>Own EMEA</li></ul>")
        (p,) = workable.parse(self.board, {"jobs": [job]})
        self.assertEqual(p.description, "You'll be our first sales hire & more.\nOwn EMEA")
        self.assertTrue(tag.verify_quote("our first sales hire & more", p))

    def test_a_malformed_job_fails_the_board(self):
        bad = [{**workable_job("AB12", "AE"), **change} for change in [
            {"shortcode": None}, {"shortcode": " "}, {"shortcode": 12}, {"title": None}, {"title": 3},
            {"description": 5}, {"url": 1}, {"telecommuting": "yes"}, {"locations": "London"},
            {"locations": [None]}, {"locations": [{"city": 3}]}, {"locations": None, "city": 3},
            {"title": "\ud800"}]]
        for job in [*bad, "not a job"]:
            with self.assertRaises(BoardError, msg=job):
                workable.parse(self.board, {"jobs": [workable_job("OK1", "AE"), job]})
        for data in [{}, {"jobs": "x"}, [], None]:
            with self.assertRaises(BoardError, msg=data):
                workable.parse(self.board, data)

    def test_no_jobs_is_an_empty_board(self):
        self.assertEqual(workable.parse(self.board, {"name": "Acme", "description": "", "jobs": []}), ())


if __name__ == "__main__":
    unittest.main()
