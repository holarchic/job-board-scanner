"""SmartRecruiters adapter: board URLs → boards, paged lists → postings, and a posting's detail → its description."""
import json
import unittest
from dataclasses import replace
from unittest import mock

from jobscan import fetch, smartrecruiters, tag
from jobscan.records import BoardError
from tests.fakes import smartrecruiters_detail, smartrecruiters_job, smartrecruiters_page

API = "https://api.smartrecruiters.com/v1/companies/acme/postings"
BOARD = smartrecruiters.page_board("Acme", "https://jobs.smartrecruiters.com/Acme/1-account-executive")


def board_for(url):
    return smartrecruiters.api_board("Acme", url) or smartrecruiters.page_board("Acme", url)


def getter(pages):
    """A fake `get` serving `pages` (URL → JSON value) and recording the URLs asked for."""
    asked = []

    def get(url):
        asked.append(url)
        return json.dumps(pages[url]).encode()
    return get, asked


def read(*pages):
    """Reads the board from `pages`, each served at its own offset, two postings a page."""
    offsets = [0]
    for page in pages[:-1]:
        offsets.append(offsets[-1] + len(page["content"]))
    get, _ = getter({f"{API}?limit=2&offset={o}": page for o, page in zip(offsets, pages, strict=True)})
    with mock.patch.object(smartrecruiters, "PAGE", 2):
        return smartrecruiters.read(BOARD, get)


class BoardTest(unittest.TestCase):
    def test_board_pages_on_both_hosts_and_ni_links_are_one_board(self):
        for url in ["https://jobs.smartrecruiters.com/Acme/744-ae", "https://jobs.smartrecruiters.com/ni/ACME/uuid-ae",
                    "https://careers.smartrecruiters.com/acme", "https://careers.smartrecruiters.com/Acme/page?x=1",
                    "https://api.smartrecruiters.com/v1/companies/Acme/postings?limit=1"]:
            b = board_for(url)
            self.assertEqual((b.id, b.company, b.api_url), ("smartrecruiters:acme", "Acme", API), url)

    def test_api_urls_keep_their_host(self):
        self.assertEqual(board_for("http://127.0.0.1:9/v1/companies/acme/postings").api_url,
                         "http://127.0.0.1:9/v1/companies/acme/postings")

    def test_other_urls_are_not_smartrecruiters_boards(self):
        for url in ["https://jobs.smartrecruiters.com/", "https://jobs.smartrecruiters.com/?company=Acme",
                    "https://jobs.smartrecruiters.com/ni/", "https://www.smartrecruiters.com/acme",
                    "http://127.0.0.1:9/v1/companies/acme", "http://127.0.0.1:9/v1/companies/acme/jobs",
                    "not a url"]:
            self.assertIsNone(board_for(url), url)

    def test_fetch_routes_board_pages_here(self):
        self.assertEqual(fetch.board_for("", "https://careers.smartrecruiters.com/Acme").id, "smartrecruiters:acme")


class ReadTest(unittest.TestCase):
    def test_pages_are_read_to_the_total_and_give_postings(self):
        first = smartrecruiters_page([smartrecruiters_job("1", " Account Executive "),
                                      smartrecruiters_job("2", "SE", "Berlin, Berlin, Germany", remote=True)], total=3)
        last = smartrecruiters_page([{**smartrecruiters_job("3", "AE"), "location": None}], offset=2, total=3)
        postings = read(first, last)
        self.assertEqual([(p.key, p.ats, p.board, p.company, p.title, p.location, p.url, p.description)
                          for p in postings],
                         [("smartrecruiters:acme:1", "smartrecruiters", "smartrecruiters:acme", "Acme",
                           "Account Executive", "London, United Kingdom", "https://jobs.smartrecruiters.com/acme/1",
                           ""),
                          ("smartrecruiters:acme:2", "smartrecruiters", "smartrecruiters:acme", "Acme", "SE",
                           "Berlin, Berlin, Germany / Remote", "https://jobs.smartrecruiters.com/acme/2", ""),
                          ("smartrecruiters:acme:3", "smartrecruiters", "smartrecruiters:acme", "Acme", "AE", "",
                           "https://jobs.smartrecruiters.com/acme/3", "")])

    def test_a_short_page_is_followed_from_where_it_ended(self):
        first = smartrecruiters_page([smartrecruiters_job("1", "AE")], total=2)
        self.assertEqual(len(read(first, smartrecruiters_page([smartrecruiters_job("2", "AE")], offset=1, total=2))), 2)

    def test_no_postings_is_an_empty_board(self):
        self.assertEqual(read(smartrecruiters_page([])), ())

    def test_location_falls_back_to_city_region_country(self):
        job = {**smartrecruiters_job("1", "AE"), "location": {"city": "London", "region": "", "country": "gb"}}
        self.assertEqual(read(smartrecruiters_page([job]))[0].location, "London, gb")

    def test_a_list_that_changes_while_paging_fails_the_board(self):
        job = smartrecruiters_job
        first = smartrecruiters_page([job("1", "AE"), job("2", "AE")], total=3)
        for last in [smartrecruiters_page([job("3", "AE")], offset=2, total=4),  # the total moved
                     smartrecruiters_page([], offset=2, total=3),  # short of the total
                     smartrecruiters_page([job("2", "AE")], offset=2, total=3),  # an id again, count right
                     smartrecruiters_page([job("3", "AE"), job("4", "AE")], offset=2, total=3),  # past the total
                     smartrecruiters_page([job("3", "AE")], offset=0, total=3)]:  # another offset answered
            with self.assertRaises(BoardError, msg=last):
                read(first, last)

    def test_a_malformed_page_or_job_fails_the_board(self):
        good = smartrecruiters_job("1", "AE")
        for page in [[], {"offset": 0, "content": [good]}, {**smartrecruiters_page([good]), "totalFound": True},
                     {**smartrecruiters_page([good]), "totalFound": -1}, {**smartrecruiters_page([]), "content": {}},
                     {**smartrecruiters_page([good]), "offset": False}, {**smartrecruiters_page([good]), "offset": 0.0},
                     smartrecruiters_page(["job"]), smartrecruiters_page([{**good, "name": " "}]),
                     smartrecruiters_page([{**good, "id": 1}]), smartrecruiters_page([{**good, "location": "London"}]),
                     smartrecruiters_page([{**good, "location": {"fullLocation": 3}}])]:
            with self.assertRaises(BoardError, msg=page):
                read(page)


class DescribeTest(unittest.TestCase):
    def test_sections_become_titled_text_from_the_boards_own_api(self):
        board = board_for("http://127.0.0.1:9/v1/companies/acme/postings")
        (posting,) = read(smartrecruiters_page([smartrecruiters_job("74/1", "AE")]))
        detail = smartrecruiters_detail("AE", [
            ("companyDescription", "Company Description", "<p>We sell <b>software</b> &amp; more.</p>"),
            ("jobDescription", "Job Description", ""), ("videos", "Videos", None),
            ("additionalInformation", "", "<ul><li>Equity</li></ul>")])
        get, asked = getter({"http://127.0.0.1:9/v1/companies/acme/postings/74%2F1": detail})
        text = smartrecruiters.describe(board, posting, get)
        self.assertEqual(text, "Company Description\nWe sell software & more.\nEquity")  # text-less sections go
        self.assertEqual(asked, ["http://127.0.0.1:9/v1/companies/acme/postings/74%2F1"])
        self.assertTrue(tag.verify_quote("We sell software & more.", replace(posting, description=text)))

    def test_a_malformed_detail_fails_the_posting(self):
        (posting,) = read(smartrecruiters_page([smartrecruiters_job("1", "AE")]))
        ad = smartrecruiters_detail("AE")["jobAd"]["sections"]
        for detail in [[], {}, {"jobAd": {}}, {"jobAd": {"sections": []}}, {"jobAd": {"sections": {}}},
                       {"jobAd": {"sections": {"companyDescription": ad["companyDescription"]}}},
                       {"jobAd": {"sections": {"jobDescription": {}}}},
                       {"jobAd": {"sections": {"jobDescription": {"title": "Job", "text": " <p> </p> "}}}},
                       {"jobAd": {"sections": {**ad, "a": "text"}}},
                       {"jobAd": {"sections": {**ad, "a": {"title": "T", "text": 3}}}},
                       {"jobAd": {"sections": {**ad, "a": {"title": 3, "text": "x"}}}},
                       {"jobAd": {"sections": {**ad, "a": {"title": "T", "text": "\ud800"}}}}]:
            get, _ = getter({f"{API}/1": detail})
            with self.assertRaises(BoardError, msg=detail):
                smartrecruiters.describe(BOARD, posting, get)


if __name__ == "__main__":
    unittest.main()
