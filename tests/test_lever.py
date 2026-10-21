"""Lever adapter: board URLs on either instance → boards, and postings-API payloads → postings."""
import unittest

from jobscan import lever
from jobscan.records import BoardError
from tests.fakes import lever_job

US = "https://api.lever.co/v0/postings/"
EU = "https://api.eu.lever.co/v0/postings/"


def board_for(url):
    return lever.api_board("Acme", url) or lever.page_board("Acme", url)


class BoardTest(unittest.TestCase):
    def test_each_instance_reads_from_its_own_api_host(self):
        for url, api in [("https://jobs.lever.co/acme", US), ("https://jobs.lever.co/acme/", US),
                         ("https://jobs.lever.co/acme/223f9722-3579-4ff2-b123-e4cc7b0c1a64", US),
                         ("https://jobs.lever.co/acme/223f9722-3579-4ff2-b123-e4cc7b0c1a64/apply", US),
                         ("https://Jobs.Lever.co:443/acme?lever-origin=applied", US),
                         ("https://jobs.eu.lever.co/acme", EU),
                         ("https://jobs.eu.lever.co/acme/259824ae-61c3-4a04-8ae5-c35ea590e76e/apply", EU),
                         ("https://api.lever.co/v0/postings/acme?mode=json", US),
                         ("https://api.eu.lever.co/v0/postings/acme", EU)]:
            b = board_for(url)
            self.assertEqual((b.id, b.company, b.api_url), ("lever:acme", "Acme", api + "acme?mode=json"), url)

    def test_slugs_keep_their_case_and_are_encoded_again(self):
        self.assertEqual(board_for("https://jobs.lever.co/Spotify").id, "lever:Spotify")
        b = board_for("https://jobs.eu.lever.co/ep%2Bco")
        self.assertEqual((b.id, b.api_url), ("lever:ep+co", EU + "ep%2Bco?mode=json"))

    def test_api_url_on_another_host_is_kept(self):
        b = board_for("http://127.0.0.1:8000/v0/postings/acme")
        self.assertEqual(b.api_url, "http://127.0.0.1:8000/v0/postings/acme?mode=json")

    def test_other_urls_are_not_lever_boards(self):
        for url in ["https://jobs.lever.co/", "https://jobs.lever.co/a%3Ab", "https://jobs.lever.co/a%2Fb",
                    "https://lever.co/acme", "https://jobs.us.lever.co/acme", "https://api.lever.co/v0/postings/",
                    "https://api.lever.co/v1/postings/acme", "https://jobs.ashbyhq.com/acme", "not a url"]:
            self.assertIsNone(board_for(url), url)


class ParseTest(unittest.TestCase):
    board = lever.page_board("Acme", "https://jobs.lever.co/acme")

    def test_posting_fields(self):
        (p,) = lever.parse(self.board, [lever_job("j1", " Account Executive ")])
        self.assertEqual((p.key, p.ats, p.board, p.company, p.title, p.location, p.url),
                         ("lever:acme:j1", "lever", "lever:acme", "Acme", "Account Executive", "London",
                          "https://jobs.lever.co/acme/j1"))
        self.assertEqual(p.description, "Account Executive role. We sell software.\nWhat you'll do\nClose deals\n"
                                        "Open doors\nWe are an equal opportunity employer.")

    def test_every_listed_location_once_and_remote_added_for_a_remote_job(self):
        jobs = [lever_job("j1", "AE", all_locations=["London", " Paris ", "London"]),
                lever_job("j2", "AE", location="Berlin", all_locations=None, workplace="remote"),
                lever_job("j3", "AE", location="Remote - UK", all_locations=["Remote - UK"], workplace="remote"),
                lever_job("j4", "AE", workplace="hybrid")]
        del jobs[1]["categories"]["allLocations"]
        self.assertEqual([p.location for p in lever.parse(self.board, jobs)],
                         ["London / Paris", "Berlin / Remote", "Remote - UK", "London"])

    def test_missing_optional_fields_leave_them_empty(self):
        (p,) = lever.parse(self.board, [{"id": "j1", "text": "AE"}])
        self.assertEqual((p.location, p.url, p.description), ("", "", ""))
        (p,) = lever.parse(self.board, [{"id": "j1", "text": "AE", "categories": None, "lists": None}])
        self.assertEqual(p.location, "")

    def test_a_malformed_board_fails_whole(self):
        def job(**changes):
            return {**lever_job("j1", "AE"), **changes}

        for data in [{"ok": False}, None, ["job"], [job(id=None)], [job(text=" ")], [job(id=7)],
                     [job(), job()], [job(categories=["London"])], [job(categories={"allLocations": "London"})],
                     [job(categories={"allLocations": ["London", 3]})],
                     [job(categories={"allLocations": ["\ud800"]})], [job(categories={"location": 3,
                                                                                    "allLocations": None})],
                     [job(lists={"text": "x"})], [job(lists=["x"])], [job(lists=[{"text": "x", "content": 3}])],
                     [job(lists=[{"text": 3}])], [job(description=3)], [job(additional=["x"])],
                     [job(hostedUrl=3)], [job(workplaceType=3)]]:
            with self.assertRaises(BoardError, msg=data):
                lever.parse(self.board, data)


if __name__ == "__main__":
    unittest.main()
