import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from jobscan import fetch
from jobscan.config import ConfigError
from jobscan.records import Board
from tests.fakes import FakeServer, ashby_job


def split(boards):
    outcomes = list(fetch.fetch_each(boards))
    return ([o for o in outcomes if isinstance(o, fetch.BoardFetch)],
            [o for o in outcomes if isinstance(o, fetch.BoardFailure)])


class AshbyBoardTest(unittest.TestCase):
    def test_human_board_url_maps_to_api(self):
        board = fetch.board_for("Acme", "https://jobs.ashbyhq.com/acme")
        self.assertEqual((board.id, board.api_url), ("ashby:acme", "https://api.ashbyhq.com/posting-api/job-board/acme"))

    def test_api_url_used_as_is_on_any_host(self):
        self.assertEqual(fetch.board_for("Acme", "http://127.0.0.1:9/posting-api/job-board/acme?x=1").api_url,
                         "http://127.0.0.1:9/posting-api/job-board/acme")

    def test_slug_is_case_folded_in_both_url_forms(self):
        for url in ["https://jobs.ashbyhq.com/MotherDuck", "https://api.ashbyhq.com/posting-api/job-board/MotherDuck"]:
            board = fetch.board_for("MotherDuck", url)
            self.assertEqual((board.id, board.api_url, board.company),
                             ("ashby:motherduck", "https://api.ashbyhq.com/posting-api/job-board/motherduck",
                              "MotherDuck"), url)

    def test_other_ats_and_malformed_urls_are_none(self):
        self.assertEqual(fetch.board_for("Acme", "https://boards.greenhouse.io/acme").ats, "greenhouse")
        self.assertIsNone(fetch.board_for("Acme", "https://[::1/posting-api/job-board/acme"))
        self.assertIsNone(fetch.board_for("Acme", "https://jobs.ashbyhq.com:99999/acme"))
        for url in ["/posting-api/job-board/acme", "http:///posting-api/job-board/acme",
                    "file://host/posting-api/job-board/acme"]:
            self.assertIsNone(fetch.board_for("Acme", url), url)

    def test_load_boards_dedupes_and_counts_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "boards.json"
            path.write_text(json.dumps({"companies": [
                {"company": "Acme", "board_url": "https://jobs.ashbyhq.com/acme"},
                {"company": "Acme again", "board_url": "https://api.ashbyhq.com/posting-api/job-board/acme"},
                {"company": "ACME", "board_url": "https://jobs.ashbyhq.com/ACME"},
                {"company": "Other", "board_url": "https://jobs.lever.co/other"},
                {"company": None, "board_url": 7},
            ]}))
            boards, skipped = fetch.load_boards(path)
        self.assertEqual([(b.company, b.id) for b in boards], [("Acme", "ashby:acme")])
        self.assertEqual(skipped, 2)

    def test_api_paths_win_over_board_pages(self):
        for url, ats in [("https://jobs.ashbyhq.com/acme", "ashby"),
                         ("https://api.ashbyhq.com/posting-api/job-board/acme", "ashby"),
                         ("https://job-boards.greenhouse.io/acme", "greenhouse"),
                         ("https://boards-api.greenhouse.io/v1/boards/acme/jobs", "greenhouse"),
                         ("https://jobs.ashbyhq.com/v1/boards/acme/jobs", "greenhouse"),  # an API path wins
                         ("https://job-boards.greenhouse.io/posting-api/job-board/acme", "ashby"),
                         ("http://127.0.0.1:8000/v1/boards/acme/jobs", "greenhouse"),
                         ("http://127.0.0.1:8000/posting-api/job-board/acme", "ashby")]:
            self.assertEqual(fetch.board_for("", url).ats, ats, url)

    def test_load_boards_rejects_lone_surrogates(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "boards.json"
            for entry in ['{"company": "\\ud800", "board_url": "https://jobs.ashbyhq.com/acme"}',
                          '{"company": "Acme", "board_url": "https://jobs.ashbyhq.com/\\ud800"}']:
                path.write_text('{"companies": [' + entry + ']}')
                with self.assertRaisesRegex(ConfigError, "entry 0 is not valid Unicode"):
                    fetch.load_boards(path)

    def test_load_boards_rejects_bad_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "boards.json"
            path.write_text("{}")
            with self.assertRaises(ConfigError):
                fetch.load_boards(path)


class FetchBoardTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.addCleanup(self.server.close)
        patcher = mock.patch.object(fetch, "RETRY_PAUSES", (0, 0))
        patcher.start()
        self.addCleanup(patcher.stop)

    def board(self, slug="acme"):
        return Board(ats="ashby", slug=slug, company="Acme", api_url=f"{self.server.url}/posting-api/job-board/{slug}")

    def serve(self, slug, status, body):
        self.server.routes[f"/posting-api/job-board/{slug}"] = (status, body)

    def fetch_one(self, slug="acme"):
        fetched, failed = split([self.board(slug)])
        return fetched[0].postings if fetched else failed[0].reason

    def test_parses_listed_jobs_with_all_locations(self):
        self.serve("acme", 200, {"jobs": [
            ashby_job("1", "Account Executive", "London", secondaryLocations=[{"location": "Remote - UK"}]),
            {**ashby_job("2", "Hidden", isListed=False), "title": None},
        ]})
        (posting,) = self.fetch_one()
        self.assertEqual((posting.key, posting.ats, posting.board), ("ashby:acme:1", "ashby", "ashby:acme"))
        self.assertEqual(posting.location, "London / Remote - UK")

    def test_html_only_description_keeps_block_breaks_and_entities(self):
        job = ashby_job("1", "AE", description="")
        job["descriptionHtml"] = "<p>Sell&nbsp;to <b>CISOs</b> &amp; CTOs</p><ul><li>Close</li><li>Grow</li></ul>"
        self.serve("acme", 200, {"jobs": [job]})
        (posting,) = self.fetch_one()
        self.assertEqual(posting.description, "Sell to CISOs & CTOs\nClose\nGrow")

    def test_success_rules(self):
        cases = {
            "malformed": (200, "{not json"),
            "nolist": (200, {"jobs": {"1": "x"}}),
            "missing": (200, {"apiVersion": "1"}),
            "notobject": (200, [1, 2]),
            "error": (500, {"error": "boom"}),
            "accepted": (203, {"jobs": []}),
        }
        for slug, (status, body) in cases.items():
            self.serve(slug, status, body)
        self.serve("empty", 200, {"jobs": []})
        fetched, failed = split([self.board(s) for s in [*cases, "empty"]])
        self.assertEqual([(r.board.slug, r.postings) for r in fetched], [("empty", ())])
        self.assertEqual({f.board.slug: f.reason for f in failed}, {
            "malformed": "malformed JSON", "nolist": "response has no `jobs` list",
            "missing": "response has no `jobs` list", "notobject": "response has no `jobs` list", "error": "HTTP 500",
            "accepted": "HTTP 203",
        })

    def test_one_malformed_listed_job_fails_the_whole_board(self):
        for bad in [{"title": None}, {"secondaryLocations": 1}, {"secondaryLocations": [1]},
                    {"secondaryLocations": {}}, {"secondaryLocations": ""}, {"secondaryLocations": False},
                    {"isListed": "false"}, {"isListed": 0},
                    {"descriptionHtml": {"x": 1}, "descriptionPlain": None}, {"location": 7}, {"id": 12}]:
            self.serve("acme", 200, {"jobs": [ashby_job("1", "AE"), {**ashby_job("2", "AE"), **bad}]})
            reason = self.fetch_one()
            self.assertIsInstance(reason, str, bad)
            self.assertTrue(reason.startswith("job 1:"), reason)

    def test_lone_surrogate_fails_only_its_board(self):
        self.serve("acme", 200, '{"jobs": [{"id": "1", "title": "AE", "descriptionPlain": "\\ud800"}]}')
        self.serve("beta", 200, {"jobs": [ashby_job("1", "AE")]})
        fetched, failed = split([self.board("acme"), self.board("beta")])
        self.assertEqual(([r.board.slug for r in fetched], [(f.board.slug, f.reason) for f in failed]),
                         (["beta"], [("acme", "job 0: `descriptionPlain` is not valid Unicode")]))

    def test_duplicate_listed_ids_fail_the_board(self):
        self.serve("acme", 200, {"jobs": [ashby_job("1", "AE"), ashby_job("1", "Other AE")]})
        self.assertEqual(self.fetch_one(), "duplicate listed job ids")

    def test_retries_server_errors_then_gives_up(self):
        self.serve("acme", 503, {"error": "busy"})
        self.assertEqual(self.fetch_one(), "HTTP 503")
        self.assertEqual(len(self.server.requests), 3)

    def test_failure_carries_the_http_status(self):
        self.serve("gone", 404, {"error": "not found"})
        self.serve("busy", 503, {"error": "busy"})
        self.serve("bad", 200, "{not json")
        _, failed = split([self.board(s) for s in ("gone", "busy", "bad")])
        self.assertEqual({f.board.slug: f.http_status for f in failed}, {"gone": 404, "busy": 503, "bad": None})

    def test_every_429_response_is_logged(self):
        for statuses, logged in [([429, 200], 1), ([429, 429, 429], 3), ([503, 503, 429], 1)]:
            with self.subTest(statuses=statuses):
                answers = iter(statuses)
                self.server.routes["/posting-api/job-board/acme"] = (
                    200, lambda _, answers=answers: (s := next(answers), {"jobs": []} if s == 200 else {}))
                with self.assertLogs("jobscan", "INFO") as logs:
                    split([self.board()])
                lines = [m for m in logs.output if "board_429 board=ashby:acme" in m]
                self.assertEqual(len(lines), logged)

    def test_slow_consumer_bounds_boards_in_flight(self):
        started = []

        def fake_fetch(board):
            started.append(board.id)
            return fetch.BoardFetch(board, ())

        boards = [self.board(f"b{i}") for i in range(5 * fetch.IN_FLIGHT)]
        with mock.patch.object(fetch, "_fetch_isolated", fake_fetch):
            for consumed, _ in enumerate(fetch.fetch_each(boards), start=1):
                time.sleep(0.001)
                self.assertLessEqual(len(started) - consumed, fetch.IN_FLIGHT)  # started, not yet handed over
        self.assertEqual(len(started), len(boards))

    def test_unreachable_board_is_a_failure(self):
        board = Board(ats="ashby", slug="gone", company="Gone", api_url="http://127.0.0.1:9/posting-api/job-board/gone")
        _, (failure,) = split([board])
        self.assertTrue(failure.reason.startswith("network"))


if __name__ == "__main__":
    unittest.main()
