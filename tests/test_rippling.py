"""Rippling adapter: board URLs → boards, paged lists → postings merged by job, and a job's detail → its text."""
import json
import unittest
from unittest import mock

from jobscan import fetch, rippling
from jobscan.records import BoardError
from tests.fakes import anywhere, rippling_detail, rippling_item, rippling_page

API = "https://ats.rippling.com/api/v2/board/Acme-Careers/jobs"
BOARD = rippling.page_board("Acme", "https://ats.rippling.com/Acme-Careers/jobs")


def board_for(url):
    return rippling.api_board("Acme", url) or rippling.page_board("Acme", url)


def getter(pages):
    def get(url):
        return json.dumps(pages[url]).encode()
    return get


def read(*pages, page_size=1000):
    get = getter({f"{API}?page={n}&pageSize={page_size}": page for n, page in enumerate(pages)})
    with mock.patch.object(rippling, "PAGE", page_size):
        return rippling.read(BOARD, get, anywhere)


class BoardTest(unittest.TestCase):
    def test_board_pages_with_or_without_a_locale_or_embed_are_one_board(self):
        for url in ["https://ats.rippling.com/Acme-Careers/jobs", "https://ats.rippling.com/Acme-Careers/jobs/abc-1",
                    "https://ats.rippling.com/en-GB/Acme-Careers/jobs", "https://ats.rippling.com/es-419/Acme-Careers/jobs/1",
                    "https://ats.rippling.com/en-gb/Acme-Careers/jobs", "https://ats.rippling.com/embed/Acme-Careers/jobs?s=x",
                    "https://ats.rippling.com/api/v2/board/Acme-Careers/jobs?page=0"]:
            b = board_for(url)
            self.assertEqual((b.id, b.company, b.api_url), ("rippling:Acme-Careers", "Acme", API), url)

    def test_api_urls_keep_their_host_and_slugs_their_case(self):
        self.assertEqual(board_for("http://127.0.0.1:9/api/v2/board/acme-careers/jobs").api_url,
                         "http://127.0.0.1:9/api/v2/board/acme-careers/jobs")

    def test_other_urls_are_not_rippling_boards(self):
        for url in ["https://ats.rippling.com/", "https://ats.rippling.com/acme", "https://ats.rippling.com/acme/apply",
                    "https://ats.rippling.com/en-GB/jobs", "https://ats.rippling.com/other/acme/jobs",
                    "https://www.rippling.com/acme/jobs", "http://127.0.0.1:9/api/v2/board/acme",
                    "not a url"]:
            self.assertIsNone(board_for(url), url)

    def test_fetch_routes_board_pages_here(self):
        self.assertEqual(fetch.board_for("", "https://ats.rippling.com/en-GB/acme/jobs").id, "rippling:acme")


class ReadTest(unittest.TestCase):
    def test_rows_of_one_job_merge_with_every_location(self):
        postings = read(rippling_page([
            rippling_item("j1", " Account Executive ", "London, United Kingdom"),
            rippling_item("j2", "SE", "Remote (United States)", "United States", workplace="REMOTE"),
            rippling_item("j1", "Account Executive", "Hybrid (Manchester, England, GB)"),
            rippling_item("j1", "Account Executive", "London, United Kingdom")]))  # identical rows happen
        self.assertEqual([(p.key, p.ats, p.board, p.company, p.title, p.location, p.url, p.description)
                          for p in postings],
                         [("rippling:Acme-Careers:j1", "rippling", "rippling:Acme-Careers", "Acme", "Account Executive",
                           "London, United Kingdom / Hybrid (Manchester, England, GB), United Kingdom",
                           "https://ats.rippling.com/Acme-Careers/jobs/j1", ""),
                          ("rippling:Acme-Careers:j2", "rippling", "rippling:Acme-Careers", "Acme", "SE",
                           "Remote (United States)", "https://ats.rippling.com/Acme-Careers/jobs/j2", "")])

    def test_a_remote_place_not_named_remote_gains_remote_and_no_place_is_none(self):
        (p,) = read(rippling_page([rippling_item("j1", "AE", "", "United Kingdom", workplace="REMOTE")]))
        self.assertEqual(p.location, "United Kingdom / Remote")
        (p,) = read(rippling_page([{**rippling_item("j1", "AE"), "locations": None}]))
        self.assertEqual(p.location, "")

    def test_pages_are_read_to_the_last(self):
        first = rippling_page([rippling_item("j1", "AE"), rippling_item("j2", "AE")], 0, 3, 2)
        last = rippling_page([rippling_item("j1", "AE", "Leeds, United Kingdom")], 1, 3, 2)
        self.assertEqual([(p.key, p.location) for p in read(first, last, page_size=2)],
                         [("rippling:Acme-Careers:j1", "London, United Kingdom / Leeds, United Kingdom"),
                          ("rippling:Acme-Careers:j2", "London, United Kingdom")])

    def test_no_jobs_is_an_empty_board(self):
        self.assertEqual(read(rippling_page([])), ())

    def test_a_list_that_changes_while_paging_fails_the_board(self):
        first = rippling_page([rippling_item("j1", "AE"), rippling_item("j2", "AE")], 0, 3, 2)
        for last in [rippling_page([rippling_item("j3", "AE")], 1, 4, 2),  # a total moved
                     rippling_page([rippling_item("j3", "AE")], 1, 3, 3),
                     rippling_page([], 1, 3, 2),  # short of the total
                     rippling_page([rippling_item("j3", "AE"), rippling_item("j4", "AE")], 1, 3, 2),  # past it
                     rippling_page([rippling_item("j3", "AE")], 0, 3, 2)]:  # another page answered
            with self.assertRaises(BoardError, msg=last):
                read(first, last, page_size=2)
        with self.assertRaises(BoardError):  # an empty page before the last
            read(rippling_page([], 0, 0, 3), rippling_page([], 1, 0, 3), rippling_page([], 2, 0, 3))

    def test_a_malformed_page_or_row_fails_the_board(self):
        good = rippling_item("j1", "AE")
        for page in [[], {"items": [good], "page": 0, "totalItems": 1}, {**rippling_page([good]), "totalItems": "1"},
                     {**rippling_page([good]), "items": {}}, rippling_page(["job"]),
                     {**rippling_page([good]), "page": False}, {**rippling_page([good]), "page": 0.0},
                     rippling_page([{**good, "name": ""}]), rippling_page([{**good, "id": 7}]),
                     rippling_page([{**good, "locations": {}}]), rippling_page([{**good, "locations": ["London"]}]),
                     rippling_page([{**good, "locations": [{"name": 3}]}]),
                     rippling_page([good, rippling_item("j1", "SE")])]:  # one job's rows disagree on its title
            with self.assertRaises(BoardError, msg=page):
                read(page)


class DescribeTest(unittest.TestCase):
    def test_company_then_role_from_the_boards_own_api(self):
        board = board_for("http://127.0.0.1:9/api/v2/board/Acme-Careers/jobs")
        (posting,) = read(rippling_page([rippling_item("j/1", "AE")]))
        asked = []

        def get(url):
            asked.append(url)
            return json.dumps(rippling_detail("<p>We sell <b>software</b>.</p>", "")).encode()
        self.assertEqual(rippling.describe(board, posting, get), "We sell software.")
        self.assertEqual(asked, ["http://127.0.0.1:9/api/v2/board/Acme-Careers/jobs/j%2F1"])
        self.assertEqual(rippling.describe(BOARD, posting, getter({f"{API}/j%2F1": rippling_detail()})),
                         "We sell software.\nSell it.")

    def test_a_malformed_detail_fails_the_posting(self):
        (posting,) = read(rippling_page([rippling_item("j1", "AE")]))
        for detail in [[], {}, {"description": "text"}, {"description": {}}, {"description": {"company": "<p>Hi</p>"}},
                       {"description": {"company": 3, "role": ""}}, {"description": {"company": "", "role": 3}},
                       {"description": {"role": ""}}, {"description": {"company": "<p> </p>", "role": "<br>"}},
                       {"description": {"company": "", "role": "\ud800"}}]:
            with self.assertRaises(BoardError, msg=detail):
                rippling.describe(BOARD, posting, getter({f"{API}/j1": detail}))


if __name__ == "__main__":
    unittest.main()
