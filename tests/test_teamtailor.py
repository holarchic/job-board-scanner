"""Teamtailor adapter: board URLs → boards, and jobs.rss feeds → postings."""
import unittest
from unittest import mock

from jobscan import fetch, payload, prefilter, tag, teamtailor
from jobscan.config import Profile
from jobscan.records import Board, BoardError
from tests.fakes import FakeServer, anywhere, teamtailor_feed, teamtailor_item

FEED = "https://acme.teamtailor.com/jobs.rss?per_page=1000"


def board_for(url):
    return teamtailor.api_board("Acme", url) or teamtailor.page_board("Acme", url)


def parse(*items):
    return teamtailor.parse(BOARD, payload.xml_root(teamtailor_feed(*items).encode()))


BOARD = teamtailor.page_board("Acme", "https://acme.teamtailor.com/jobs")


class BoardTest(unittest.TestCase):
    def test_every_url_on_the_board_host_is_one_board(self):
        for url in ["https://acme.teamtailor.com", "https://acme.teamtailor.com/jobs/8118064-account-executive",
                    "https://ACME.TeamTailor.com:443/jobs.rss", "http://acme.teamtailor.com/en/jobs?query=x"]:
            b = board_for(url)
            self.assertEqual((b.id, b.company, b.api_url), ("teamtailor:acme", "Acme", FEED), url)

    def test_other_urls_are_not_teamtailor_boards(self):
        for url in ["https://teamtailor.com/jobs", "https://www.teamtailor.com/en", "https://app.teamtailor.com/x",
                    "https://a.b.teamtailor.com/jobs", "https://acme.teamtailor.co/jobs",
                    "https://acme.teamtailorx.com/jobs", "http://127.0.0.1:8000/jobs.rss",
                    "https://jobs.ashbyhq.com/acme", "not a url"]:
            self.assertIsNone(board_for(url), url)

    def test_fetch_routes_board_pages_here_and_never_claims_a_bare_feed_path(self):
        self.assertEqual(fetch.board_for("", "https://acme.teamtailor.com/jobs").id, "teamtailor:acme")
        self.assertIsNone(fetch.board_for("", "https://careers.example.com/jobs.rss"))


class ParseTest(unittest.TestCase):
    def test_posting_fields(self):
        item = teamtailor_item("4ff07fd5", " Account Executive ", [("London", "United Kingdom"), ("Leeds", "")],
                               description="<p>You'll be our <strong>first sales hire</strong> &amp; more.</p>"
                                           "<ul><li>Own EMEA</li></ul>")
        (p,) = parse(item)
        self.assertEqual((p.key, p.ats, p.board, p.company, p.title, p.location, p.url),
                         ("teamtailor:acme:4ff07fd5", "teamtailor", "teamtailor:acme", "Acme", "Account Executive",
                          "London, United Kingdom / Leeds", "https://acme.teamtailor.com/jobs/4ff07fd5-job"))
        self.assertEqual(p.description, "You'll be our first sales hire & more.\nOwn EMEA")
        self.assertTrue(tag.verify_quote("our first sales hire & more", p))

    def test_places_are_listed_once_and_fall_back_to_the_location_name(self):
        item = teamtailor_item("1", "AE", [("London", "United Kingdom"), ("London", "United Kingdom"),
                                           ("Singapore", "Singapore"), ("", "", "Head office"), ("", "", "")])
        self.assertEqual(parse(item)[0].location, "London, United Kingdom / Singapore / Head office")
        self.assertEqual(parse(teamtailor_item("1", "AE", places=()))[0].location, "")

    def test_fully_remote_jobs_say_so_once(self):
        remote_only = Profile(name="r", titles=("AE",), exclude_titles=(), locations=("Remote",), eligible_where="",
                              tags=(), custom_tags=())
        for places, remote, expected in [([("London", "United Kingdom")], "fully", "London, United Kingdom / Remote"),
                                         ([("Remote", "United Kingdom")], "fully", "Remote, United Kingdom"),
                                         ([("London", "United Kingdom")], "hybrid", "London, United Kingdom")]:
            (p,) = parse(teamtailor_item("1", "AE", places, remote=remote))
            self.assertEqual(p.location, expected)
            self.assertEqual(prefilter.matches(p, remote_only), remote == "fully")

    def test_a_feed_that_fills_its_page_fails_the_board(self):
        with mock.patch.object(teamtailor, "PER_PAGE", 2):
            self.assertEqual(len(parse(teamtailor_item("1", "AE"))), 1)
            with self.assertRaisesRegex(BoardError, "may be cut"):
                parse(teamtailor_item("1", "AE"), teamtailor_item("2", "AE"))

    def test_a_malformed_feed_or_job_fails_the_board(self):
        for items in [(teamtailor_item("1", "AE"), teamtailor_item("1", "SE")), (teamtailor_item(" ", "AE"),),
                      (teamtailor_item("1", " "),)]:
            with self.assertRaises(BoardError, msg=items):
                parse(*items)
        for body in ["<rss/>", "<feed><channel/></feed>", "<channel><item/></channel>"]:
            with self.assertRaises(BoardError, msg=body):
                teamtailor.parse(BOARD, payload.xml_root(body.encode()))

    def test_no_jobs_is_an_empty_board(self):
        self.assertEqual(parse(), ())


class FetchTest(unittest.TestCase):
    def test_an_undecodable_feed_fails_only_its_board(self):
        server = FakeServer()
        self.addCleanup(server.close)
        server.routes["/good"] = (200, teamtailor_feed(teamtailor_item("1", "AE")))
        server.routes["/unknown"] = (200, '<?xml version="1.0" encoding="x-unknown"?><rss/>')
        server.routes["/multibyte"] = (200, '<?xml version="1.0" encoding="shift_jis"?><rss/>')
        boards = [Board(ats="teamtailor", slug=s, company=s, api_url=f"{server.url}/{s}")
                  for s in ("good", "unknown", "multibyte")]
        outcomes = {o.board.slug: o for o in fetch.fetch_each(boards, anywhere)}
        self.assertEqual(len(outcomes["good"].postings), 1)
        self.assertEqual({outcomes[s].reason for s in ("unknown", "multibyte")}, {"malformed XML"})


if __name__ == "__main__":
    unittest.main()
