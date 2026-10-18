import gzip
import json
import tempfile
import threading
import time
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path
from unittest import mock

from jobscan import ashby, fetch, payload
from jobscan.config import ConfigError
from jobscan.connections import Connections
from jobscan.records import Board, BoardError
from tests.fakes import (
    FakeServer,
    anywhere,
    ashby_job,
    smartrecruiters_detail,
    smartrecruiters_job,
    smartrecruiters_page,
)


def split(boards, places=anywhere):
    outcomes = list(fetch.fetch_each(boards, places))
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

    def test_malformed_markup_still_gives_text(self):
        for markup in ["<p>Before <![bogus]> after</p>", "<p>Our <strong title=\"a > b\">first</strong> hire</p>"]:
            self.assertIn("after" if "bogus" in markup else "Our first hire", payload.html_to_text(markup), markup)
        markup = '<p>Our <strong title="a > b">first</strong> hire <![bogus]></p><p>Next</p>'
        real_feed = payload._Text.feed

        def asserting_on_declarations(parser, data):
            if "<!" in data:
                raise AssertionError("unexpected call to parse_marked_section")
            return real_feed(parser, data)

        with mock.patch.object(payload._Text, "feed", asserting_on_declarations):
            self.assertEqual(payload.html_to_text(markup), "Our first hire\nNext")
        with mock.patch.object(payload._Text, "feed", side_effect=AssertionError):
            self.assertEqual(payload.html_to_text("<p>Before <![bogus]> after</p>"), "Before after")

    def test_xml_root_reads_a_feed_and_fails_the_board_on_anything_else(self):
        self.assertEqual(payload.xml_root(b'<?xml version="1.0" encoding="UTF-8"?><rss><channel/></rss>').tag, "rss")
        for body in [b"", b"<rss>", b"{}", b"<a>\xff</a>", b'<?xml version="1.0" encoding="x-unknown"?><a/>',
                     b'<?xml version="1.0" encoding="shift_jis"?><a/>',
                     b'<!DOCTYPE a [<!ENTITY e SYSTEM "file:///etc/passwd">]><a>&e;</a>']:
            with self.assertRaisesRegex(BoardError, "^malformed XML$", msg=body):
                payload.xml_root(body)

    def test_api_paths_win_over_board_pages(self):
        for url, ats in [("https://jobs.ashbyhq.com/acme", "ashby"),
                         ("https://api.ashbyhq.com/posting-api/job-board/acme", "ashby"),
                         ("https://job-boards.greenhouse.io/acme", "greenhouse"),
                         ("https://boards-api.greenhouse.io/v1/boards/acme/jobs", "greenhouse"),
                         ("https://jobs.ashbyhq.com/v1/boards/acme/jobs", "greenhouse"),  # an API path wins
                         ("https://job-boards.greenhouse.io/posting-api/job-board/acme", "ashby"),
                         ("http://127.0.0.1:8000/v1/boards/acme/jobs", "greenhouse"),
                         ("http://127.0.0.1:8000/posting-api/job-board/acme", "ashby"),
                         ("https://apply.workable.com/acme", "workable"),
                         ("https://apply.workable.com/api/v1/widget/accounts/acme", "workable"),
                         ("https://jobs.ashbyhq.com/api/v1/widget/accounts/acme", "workable"),
                         ("https://apply.workable.com/v1/boards/acme/jobs", "greenhouse"),
                         ("https://jobs.gem.com/acme", "gem"),
                         ("https://api.gem.com/job_board/v0/acme/job_posts/", "gem"),
                         ("https://jobs.gem.com/posting-api/job-board/acme", "ashby"),
                         ("https://job-boards.greenhouse.io/job_board/v0/acme", "gem")]:
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
        self.serve("accepted", 203, {"jobs": []})
        _, failed = split([self.board(s) for s in ("gone", "busy", "bad", "accepted")])
        self.assertEqual({f.board.slug: f.http_status for f in failed},
                         {"gone": 404, "busy": 503, "bad": None, "accepted": 203})

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

        def fake_fetch(connections, board, places):
            started.append(board.id)
            return fetch.BoardFetch(board, ())

        boards = [self.board(f"b{i}") for i in range(5 * fetch.IN_FLIGHT)]
        with mock.patch.object(fetch, "_fetch_isolated", fake_fetch):
            for consumed, _ in enumerate(fetch.fetch_each(boards, anywhere), start=1):
                time.sleep(0.001)
                self.assertLessEqual(len(started) - consumed, fetch.IN_FLIGHT)  # started, not yet handed over
        self.assertEqual(len(started), len(boards))

    def test_each_ats_reads_in_its_own_lane_all_lanes_at_once(self):
        lock, running, peak = threading.Lock(), Counter(), Counter()
        # Each lane's first WORKERS boards wait until every worker of all three lanes is busy: one shared pool of
        # WORKERS threads could never fill the barrier.
        together = threading.Barrier(3 * fetch.WORKERS, timeout=10)

        def fake_fetch(connections, board, places):
            with lock:
                running[board.ats] += 1
                peak[board.ats] = max(peak[board.ats], running[board.ats])
            if int(board.slug[-2:]) < fetch.WORKERS:
                together.wait()  # broken (a test error) unless all three lanes run at once
            time.sleep(0.002)
            with lock:
                running[board.ats] -= 1
            return fetch.BoardFetch(board, ())

        # In id order, as the run hands them over: one shared pool would read one ATS after another.
        boards = [Board(ats=ats, slug=f"{ats}{i:02d}", company="x", api_url="http://unused")
                  for ats in ("ashby", "greenhouse", "workday") for i in range(60)]
        with mock.patch.object(fetch, "_fetch_isolated", fake_fetch), self.assertLogs("jobscan", "INFO") as logs:
            handed = [o.board.id for o in fetch.fetch_each(boards, anywhere)]
        self.assertEqual(sorted(handed), sorted(b.id for b in boards))
        self.assertEqual(set(peak), {"ashby", "greenhouse", "workday"})
        self.assertTrue(all(n <= fetch.WORKERS for n in peak.values()), peak)
        done = [m for m in logs.output if "fetch_lane_done" in m]
        self.assertEqual(sorted(m.split("ats=")[1].split()[0] for m in done), ["ashby", "greenhouse", "workday"])
        self.assertTrue(all("boards=60 " in m for m in done))

    def test_a_slow_consumer_bounds_boards_in_flight_per_lane(self):
        lock, started = threading.Lock(), Counter()

        def fake_fetch(connections, board, places):
            with lock:
                started[board.ats] += 1
            return fetch.BoardFetch(board, ())

        boards = [Board(ats=ats, slug=f"{ats}{i}", company="x", api_url="http://unused")
                  for ats in ("ashby", "gem") for i in range(5 * fetch.IN_FLIGHT)]
        handed = Counter()
        with mock.patch.object(fetch, "_fetch_isolated", fake_fetch):
            for outcome in fetch.fetch_each(boards, anywhere):
                handed[outcome.board.ats] += 1
                time.sleep(0.001)
                with lock:
                    in_flight = {ats: started[ats] - handed[ats] for ats in ("ashby", "gem")}
                self.assertTrue(all(n <= fetch.IN_FLIGHT for n in in_flight.values()), in_flight)
        self.assertEqual(started, handed)

    def test_a_body_is_posted_as_json_and_a_bare_url_is_a_get(self):
        self.server.routes["/wday/cxs/acme/ext/jobs"] = (200, lambda payload: (200, {"echo": payload}))
        board, connections = self.board(), self.enterContext(Connections(fetch.TIMEOUT))
        posted = fetch._get(connections, board, f"{self.server.url}/wday/cxs/acme/ext/jobs", b'{"limit": 20}')
        fetched = fetch._get(connections, board, f"{self.server.url}/wday/cxs/acme/ext/jobs")
        self.assertEqual((json.loads(posted), json.loads(fetched)), ({"echo": {"limit": 20}}, {"echo": None}))
        self.assertEqual([(method, payload) for method, _, payload in self.server.requests],
                         [("POST", {"limit": 20}), ("GET", None)])
        self.assertEqual(self.server.content_types, [("POST", "/wday/cxs/acme/ext/jobs", "application/json"),
                                                     ("GET", "/wday/cxs/acme/ext/jobs", None)])

    def test_every_request_asks_for_gzip_and_a_gzip_answer_is_read_inflated(self):
        self.serve("acme", 200, {"jobs": [ashby_job("1", "Account Executive")]})
        self.server.encode = lambda raw: (gzip.compress(raw), "gzip")
        board = self.board()
        postings = self.fetch_one()
        self.assertEqual([p.title for p in postings], ["Account Executive"])
        connections = self.enterContext(Connections(fetch.TIMEOUT))
        fetch._get(connections, board, f"{self.server.url}/posting-api/job-board/acme", b"{}")
        self.assertEqual(self.server.accept_encodings, ["gzip", "gzip"])

    def test_a_gzip_answer_in_several_members_is_read_whole(self):
        self.serve("acme", 200, {"jobs": [ashby_job("1", "Account Executive")]})
        self.server.encode = lambda raw: (gzip.compress(raw[:7]) + gzip.compress(raw[7:]) + b"\0\0", "gzip")
        self.assertEqual([p.title for p in self.fetch_one()], ["Account Executive"])

    def test_a_plain_answer_is_read_as_sent(self):
        self.serve("acme", 200, {"jobs": [ashby_job("1", "Account Executive")]})
        self.server.encode = lambda raw: (raw, "identity")
        self.assertEqual(len(self.fetch_one()), 1)

    def test_a_bad_or_oversized_gzip_answer_fails_its_board(self):
        whole = gzip.compress(json.dumps({"jobs": []}).encode())
        for name, encode, reason in [
            ("not gzip", lambda raw: (raw, "gzip"), "malformed gzip"),
            ("cut short", lambda raw: (whole[:len(whole) // 2], "gzip"), "malformed gzip: the stream ends early"),
            ("not asked for", lambda raw: (raw, "br"), "answer encoded 'br', not asked for"),
            ("too big", lambda raw: (gzip.compress(b" " * 2048 + raw), "gzip"), "answer inflates past 0 MB"),
            ("too big in all", lambda raw: (gzip.compress(b" " * 600) + gzip.compress(b" " * 600 + raw), "gzip"),
             "answer inflates past 0 MB"),
            ("a later member broken", lambda raw: (gzip.compress(raw) + b"junk", "gzip"), "malformed gzip"),
            ("a later member cut short", lambda raw: (gzip.compress(raw) + whole[:len(whole) // 2], "gzip"),
             "malformed gzip: the stream ends early"),
        ]:
            with self.subTest(name), mock.patch.object(fetch, "MAX_BODY", 1024):
                self.serve("acme", 200, {"jobs": []})
                self.server.encode = encode
                self.assertEqual(self.fetch_one(), reason)

    def test_places_reach_each_read(self):
        seen = []

        def read(board, get, places):
            seen.append(places)
            return ()

        def places(text):
            return text == "London"

        with mock.patch.object(ashby, "read", read):
            split([self.board("a"), self.board("b")], places)
        self.assertEqual(seen, [places, places])

    def test_unreachable_board_is_a_failure(self):
        board = Board(ats="ashby", slug="gone", company="Gone", api_url="http://127.0.0.1:9/posting-api/job-board/gone")
        _, (failure,) = split([board])
        self.assertTrue(failure.reason.startswith("network"))



class KeptConnectionTest(unittest.TestCase):
    """Fetch, descriptions and company lookups each keep their connections, at most one per worker."""

    def setUp(self):
        self.server = FakeServer(keep_alive=True)
        self.addCleanup(self.server.close)
        patcher = mock.patch.object(fetch, "RETRY_PAUSES", (0, 0))
        patcher.start()
        self.addCleanup(patcher.stop)

    def ashby(self, slug):
        self.server.routes[f"/posting-api/job-board/{slug}"] = (200, {"jobs": [ashby_job(slug, f"AE {slug}")]})
        return Board(ats="ashby", slug=slug, company="x", api_url=f"{self.server.url}/posting-api/job-board/{slug}")

    def test_a_cut_answer_is_tried_again_on_a_new_connection(self):
        board = self.ashby("acme")
        whole = json.dumps({"jobs": [ashby_job("1", "AE")]})  # text: sent whole; bytes are sent cut
        for sent, outcome in [([b'{"jobs": [', b'{"jobs"', whole], "AE"),
                                 ([b'{"jobs": [', b'{"jobs"', b"{"], "network: IncompleteRead")]:
            with self.subTest(outcome):
                before = len(self.server.requests)
                answers = iter(sent)
                self.server.routes["/posting-api/job-board/acme"] = (200, lambda _, a=answers: (200, next(a)))
                fetched, failed = split([board])
                self.assertEqual([p.title for f in fetched for p in f.postings] or [f.reason for f in failed],
                                 [outcome])
                ports = self.server.client_ports[before:]
                self.assertEqual((len(ports), len(set(ports))), (3, 3))  # each cut connection left behind

    def test_a_lane_reads_over_at_most_a_connection_per_worker(self):
        boards = [self.ashby(f"b{n:02d}") for n in range(3 * fetch.WORKERS)]
        fetched, failed = split(boards)
        self.assertEqual(failed, [])
        self.assertEqual(sorted((f.board.slug, [p.title for p in f.postings]) for f in fetched),
                         [(b.slug, [f"AE {b.slug}"]) for b in boards])
        self.assertEqual(sorted(path for _, path, _ in self.server.requests),
                         sorted(f"/posting-api/job-board/{b.slug}" for b in boards))
        self.assertLessEqual(len(set(self.server.client_ports)), fetch.WORKERS)

    def test_descriptions_are_read_over_at_most_a_connection_per_worker(self):
        board = fetch.board_for("Acme", f"{self.server.url}/v1/companies/acme/postings")
        ids = [str(n) for n in range(3 * fetch.WORKERS)]
        self.server.routes["/v1/companies/acme/postings?limit=100&offset=0"] = (200, smartrecruiters_page(
            [smartrecruiters_job(n, "AE") for n in ids]))
        ((fetched,), _) = split([board])
        listed = len(self.server.requests)
        for n in ids:
            self.server.routes[f"/v1/companies/acme/postings/{n}"] = (200, smartrecruiters_detail(f"AE {n}"))
        outcomes = fetch.describe_all([(board, p) for p in fetched.postings])
        self.assertTrue(all(isinstance(o, fetch.Described) for o in outcomes), outcomes)
        self.assertEqual([o.posting.description.rsplit("\n", 1)[-1] for o in outcomes],
                         [f"AE {p.key.rsplit(':', 1)[-1]} role." for p in fetched.postings])
        self.assertEqual(sorted(path for _, path, _ in self.server.requests[listed:]),
                         sorted(f"/v1/companies/acme/postings/{n}" for n in ids))
        self.assertLessEqual(len(set(self.server.client_ports[listed:])), fetch.WORKERS)

    def test_companies_are_looked_up_over_at_most_a_connection_per_worker(self):
        boards = [fetch.board_for("", f"{self.server.url}/v1/boards/b{n:02d}/jobs") for n in range(3 * fetch.WORKERS)]
        for board in boards:
            self.server.routes[f"/v1/boards/{board.slug}"] = (200, {"name": f"Company {board.slug}"})
        outcomes = fetch.company_all(boards)
        self.assertEqual([(o.board.slug, o.company.name) for o in outcomes],
                         [(b.slug, f"Company {b.slug}") for b in boards])
        self.assertEqual(sorted(path for _, path, _ in self.server.requests),
                         sorted(f"/v1/boards/{b.slug}" for b in boards))
        self.assertLessEqual(len(set(self.server.client_ports)), fetch.WORKERS)


class DescribeAllTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.addCleanup(self.server.close)
        patcher = mock.patch.object(fetch, "RETRY_PAUSES", (0, 0))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_posting_is_described_in_order_and_a_failure_stays_its_own(self):
        board = fetch.board_for("Acme", f"{self.server.url}/v1/companies/acme/postings")
        self.server.routes["/v1/companies/acme/postings?limit=100&offset=0"] = (200, smartrecruiters_page(
            [smartrecruiters_job(n, "AE") for n in ("1", "2", "3", "4")]))
        ((fetched,), _) = split([board])
        self.server.routes["/v1/companies/acme/postings/1"] = (200, smartrecruiters_detail("AE"))
        self.server.routes["/v1/companies/acme/postings/3"] = (500, {"error": "boom"})
        self.server.routes["/v1/companies/acme/postings/4"] = (200, {"jobAd": None})
        outcomes = fetch.describe_all([(board, p) for p in fetched.postings])
        self.assertEqual([o.posting.key for o in outcomes], [p.key for p in fetched.postings])
        self.assertIsInstance(outcomes[0], fetch.Described)
        self.assertEqual(outcomes[0].posting, replace(
            fetched.postings[0], description="Company Description\nWe sell software.\nJob Description\nAE role."))
        self.assertEqual([(o.reason, o.http_status) for o in outcomes[1:]],
                         [("HTTP 404", 404), ("HTTP 500", 500), ("detail has no `jobAd.sections` object", None)])
        self.assertEqual(sum(1 for r in self.server.requests if r[1] == "/v1/companies/acme/postings/3"), 3)

if __name__ == "__main__":
    unittest.main()
