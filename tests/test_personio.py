"""Personio adapter: board URLs → boards, and /xml feeds → postings."""
import unittest

from jobscan import fetch, payload, personio, tag
from jobscan.records import BoardError
from tests.fakes import personio_feed, personio_position

FEED = "https://acme.jobs.personio.com/xml?language=en"
BOARD = personio.page_board("Acme", "https://acme.jobs.personio.de/")


def board_for(url):
    return personio.api_board("Acme", url) or personio.page_board("Acme", url)


def parse(*positions):
    return personio.parse(BOARD, payload.xml_root(personio_feed(*positions).encode()))


class BoardTest(unittest.TestCase):
    def test_both_hosts_and_every_url_on_them_are_one_board(self):
        for url in ["https://acme.jobs.personio.de", "https://acme.jobs.personio.com/job/2786564?language=de",
                    "https://ACME.Jobs.Personio.DE:443/xml", "http://acme.jobs.personio.com/"]:
            b = board_for(url)
            self.assertEqual((b.id, b.company, b.api_url), ("personio:acme", "Acme", FEED), url)

    def test_other_urls_are_not_personio_boards(self):
        for url in ["https://jobs.personio.de/", "https://acme.personio.de/", "https://a.b.jobs.personio.com/",
                    "https://acme.jobs.personio.co/", "http://127.0.0.1:8000/xml", "https://acme.teamtailor.com/",
                    "not a url"]:
            self.assertIsNone(board_for(url), url)

    def test_fetch_routes_board_pages_here_and_never_claims_a_bare_feed_path(self):
        self.assertEqual(fetch.board_for("", "https://acme.jobs.personio.com/").id, "personio:acme")
        self.assertIsNone(fetch.board_for("", "https://careers.example.com/xml"))


class ParseTest(unittest.TestCase):
    def test_posting_fields(self):
        position = personio_position(
            "2786564", " Account Executive (m/f/d) ", "London", additional=["Berlin", "London", " "],
            sections=[("Your mission", "<p>You'll be our <strong>first sales hire</strong> &amp; more.</p>"),
                      ("What we offer", "<ul><li>Equity</li></ul>"), ("Empty", "")])
        (p,) = parse(position)
        self.assertEqual((p.key, p.ats, p.board, p.company, p.title, p.location, p.url),
                         ("personio:acme:2786564", "personio", "personio:acme", "Acme", "Account Executive (m/f/d)",
                          "London / Berlin", "https://acme.jobs.personio.com/job/2786564"))
        self.assertEqual(p.description,
                         "Your mission\nYou'll be our first sales hire & more.\nWhat we offer\nEquity\nEmpty")
        self.assertTrue(tag.verify_quote("our first sales hire & more", p))

    def test_a_job_without_descriptions_or_offices_is_still_a_posting(self):
        (p,) = parse(personio_position("1", "AE", office="", sections=[]))
        self.assertEqual((p.location, p.description), ("", ""))

    def test_a_malformed_feed_or_job_fails_the_board(self):
        for positions in [(personio_position("1", "AE"), personio_position("1", "SE")),
                          (personio_position(" ", "AE"),), (personio_position(" ", " "), personio_position("2", "AE")),
                          (personio_position("1", " "), personio_position("1", "AE"))]:
            with self.assertRaises(BoardError, msg=positions):
                parse(*positions)
        with self.assertRaises(BoardError):
            personio.parse(BOARD, payload.xml_root(b"<rss><position/></rss>"))

    def test_a_job_without_a_name_is_skipped_and_the_rest_of_the_board_read(self):
        self.assertEqual([p.key for p in parse(personio_position("1", " "), personio_position("2", "AE"))],
                         ["personio:acme:2"])

    def test_no_jobs_is_an_empty_board(self):
        self.assertEqual(parse(), ())


if __name__ == "__main__":
    unittest.main()
