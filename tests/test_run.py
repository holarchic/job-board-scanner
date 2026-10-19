"""End to end: `jobscan run` against a fake Ashby and a fake model, through the real CLI."""
import contextlib
import fcntl
import io
import json
import os
import re
import smtplib
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest import mock

from jobscan import (
    ashby,
    backup,
    cli,
    crawl,
    digest,
    fetch,
    gem,
    greenhouse,
    init,
    notify,
    oracle,
    personio,
    rippling,
    scan,
    schedule,
    smartrecruiters,
    store,
    tag,
    teamtailor,
    workable,
    workday,
)
from jobscan.records import Board, Posting
from tests.fakes import (
    FakeFiles,
    FakeServer,
    StatefulLaunchctl,
    anywhere,
    ashby_job,
    cdx_line,
    crawl_files,
    crawl_list,
    gem_job,
    greenhouse_job,
    model_reply,
    oracle_detail,
    oracle_job,
    oracle_page,
    patch_smtp,
    personio_feed,
    personio_position,
    rippling_detail,
    rippling_item,
    rippling_page,
    smartrecruiters_detail,
    smartrecruiters_job,
    smartrecruiters_page,
    tags_quoting_the_posting,
    teamtailor_feed,
    teamtailor_item,
    workable_job,
    workday_detail,
    workday_job,
    workday_site,
)

PROFILE = """titles = ["Account Executive"]
locations = ["London"]
eligible_where = "the UK"
tags = ["founding", "location_eligible", "segment", "language_required", "tech_company"]
"""
# Keep the discovered list fresh and covering every ATS without adding a board to read: `run` never crawls here.
SENTINELS = [Board(ats, "sentinel", "sentinel", "http://127.0.0.1:9/never") for ats in fetch.ADAPTERS]


class RunFixture(unittest.TestCase):
    def setUp(self):
        self.ats, self.model = FakeServer(), FakeServer()
        self.addCleanup(self.ats.close)
        self.addCleanup(self.model.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / "profiles").mkdir()
        (self.home / "profiles" / "ae.toml").write_text(PROFILE)
        (self.home / "listed.json").write_text(json.dumps({"companies": [
            {"company": "Acme", "board_url": f"{self.ats.url}/posting-api/job-board/acme"},
            {"company": "Broken", "board_url": f"{self.ats.url}/posting-api/job-board/broken"},
        ]}))
        self.write_config(boards_file=True)
        self.ats.routes["/posting-api/job-board/broken"] = (500, {"error": "boom"})
        self.jobs = [ashby_job("1", "Account Executive"), ashby_job("2", "Enterprise Account Executive"),
                     ashby_job("3", "Account Executive", location="Berlin"), ashby_job("4", "Engineer")]
        self.ats.routes["/posting-api/job-board/acme"] = (200, lambda _: (200, {"jobs": self.jobs}))
        self.model_ok()
        # Board pages answer company lookups; the fake answers 404 until a test routes one.
        pages = [(adapter, "BOARD_PAGE", f"{self.ats.url}/page/{adapter.ATS}/{{slug}}")
                 for adapter in (ashby, gem, personio, rippling)]
        pages.append((workday, "SITE", self.ats.url + "/workday/{tenant}.{dc}"))  # Workday's API is on each site
        pages.append((oracle, "SITE", self.ats.url + "/oracle/{pod}.{region}"))  # so are Oracle's API and pages
        for target, name, value in [(fetch, "RETRY_PAUSES", (0, 0)), (tag, "PAUSE", 0),
                                    (tag, "RATE_LIMIT_PAUSES", (0, 0, 0, 0)), *pages]:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, {"JOBSCAN_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.seed_discovery()
        # Not a CrawlError, which `run` would log and swallow: an unexpected crawl fails the test.
        self.tripwire = mock.patch.object(crawl, "latest_crawls", side_effect=AssertionError("unexpected crawl"))
        self.tripwire.start()
        self.addCleanup(mock.patch.stopall)

    def seed_discovery(self, *slugs, at=None):
        """Make `slugs` the discovered boards, as `discover` last did `at` (default now)."""
        conn = store.open_db(self.home)
        try:
            boards = [Board("ashby", s, s, f"{self.ats.url}/posting-api/job-board/{s}") for s in slugs]
            store.replace_discovered(conn, [*boards, *SENTINELS],
                                     at or datetime.now(UTC).isoformat(timespec="microseconds"))
            with conn:
                conn.execute("UPDATE boards SET not_found = ? WHERE slug = 'sentinel'", (store.DEAD_AFTER,))
        finally:
            conn.close()

    def write_config(self, boards_file):
        (self.home / "config.toml").write_text(('boards_file = "listed.json"\n' if boards_file else "")
                                               + f'[model]\nbase_url = "{self.model.url}"\nslug = "test/model"\n')

    def model_ok(self):
        self.model.routes["/chat/completions"] = (200, lambda payload: model_reply(tags_quoting_the_posting(payload)))

    def run_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["run"])
        return code, out.getvalue().strip().split("\t")

    def digests(self):
        return sorted((self.home / "digests" / "ae").glob("*.md"))

    def assert_logged_in_order(self, *patterns):
        """Each regex matches a runs.log line, in this order."""
        lines = (self.home / "runs.log").read_text().splitlines()
        at = [next((i for i, line in enumerate(lines) if re.search(pattern, line)), None) for pattern in patterns]
        self.assertNotIn(None, at, dict(zip(patterns, at, strict=True)))
        self.assertEqual(at, sorted(at), dict(zip(patterns, at, strict=True)))


class RunTest(RunFixture):
    def test_new_then_nothing_then_one_added(self):
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        text = Path(path).read_text()
        self.assertIn("## Acme — Enterprise Account Executive · London", text)
        self.assertIn('- founding: **yes** — "Account Executive"', text)
        self.assertEqual(text.count("· 4 open roles\n"), 2)  # the Berlin and Engineer roles count too
        self.assertNotIn("Berlin", text)
        self.assertEqual(self.run_cli(), (0, ["ae", "0", "-"]))
        self.assertEqual(len(self.digests()), 1)
        self.jobs.append(ashby_job("5", "Founding Account Executive"))
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "1"))
        self.assertIn("Founding Account Executive", Path(path).read_text())
        self.assertEqual(len(self.digests()), 2)

    def test_remote_jobs_reach_the_digest_only_where_the_profile_can_work(self):
        (self.home / "profiles" / "ae.toml").write_text(PROFILE.replace('["London"]', '["London", "UK", "Remote"]'))
        self.jobs = [ashby_job("1", "Account Executive", location="Remote - United States"),
                     ashby_job("2", "Account Executive", location="Remote - EMEA"),
                     ashby_job("3", "Account Executive", location="Remote"),
                     ashby_job("4", "Account Executive", location="Canada / Remote")]
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        text = Path(path).read_text()
        self.assertIn("## Acme — Account Executive · Remote - EMEA", text)
        self.assertIn("## Acme — Account Executive · Remote\n", text)
        self.assertNotIn("United States", text)
        self.assertNotIn("Canada", text)

    def test_the_run_times_its_steps_before_fetching(self):
        self.run_cli()
        self.assert_logged_in_order(r"run_start profile=ae setup_seconds=\d+\.\d$",
                                    r"prefetch db_open_seconds=\d+\.\d discovery_check_seconds=\d+\.\d$",
                                    r"fetch_start boards=")

    def test_board_failure_is_logged_and_run_completes(self):
        code, (_, new, _) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        log = (self.home / "runs.log").read_text()
        self.assertIn("board_failed company='Broken' board=ashby:broken reason='HTTP 500'", log)

    def test_model_down_carries_postings_over_until_it_returns(self):
        self.model.routes["/chat/completions"] = (503, {"error": "down"})
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (1, "0"))
        self.assertEqual(Path(path).read_text(), "# ae — " + Path(path).stem + " · 0 new · 2 untagged, carried over\n")
        self.model_ok()
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        self.assertIn("Enterprise Account Executive", Path(path).read_text())
        self.assertEqual(self.run_cli(), (0, ["ae", "0", "-"]))

    def test_fabricated_quote_is_flagged_not_dropped(self):
        def fabricating(payload):
            tags = tags_quoting_the_posting(payload)
            tags["founding"]["quote"] = "you will be our very first seller"
            return model_reply(tags)

        self.model.routes["/chat/completions"] = (200, fabricating)
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        self.assertIn('- founding: **yes** — "you will be our very first seller" ⚠ quote not in posting',
                      Path(path).read_text())

    def test_cached_tags_are_not_requested_again(self):
        self.run_cli()
        calls = len(self.model.requests)
        self.jobs.append(ashby_job("5", "Account Executive"))
        self.run_cli()
        self.assertEqual(len(self.model.requests), calls + 1)

    def test_digest_write_failure_leaves_postings_pending(self):
        (self.home / "digests").mkdir()
        (self.home / "digests" / "ae").write_text("a file where the directory should be")
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new, path), (1, "0", "-"))
        (self.home / "digests" / "ae").unlink()
        code, (_, new, _) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))

    def test_second_run_while_locked_is_refused(self):
        with (self.home / ".lock").open("w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, _ = self.run_cli()
        self.assertEqual(code, 2)

    def test_missing_openrouter_key_is_a_config_error(self):
        (self.home / "config.toml").write_text(
            'boards_file = "listed.json"\n[model]\nbase_url = "https://openrouter.ai/api/v1"\nslug = "x/y"\n')
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            code, _ = self.run_cli()
        self.assertEqual(code, 2)
        self.assertEqual(self.ats.requests, [])


class CompanyRunTest(RunFixture):
    """Company names and websites in the digest: looked up once per board, kept, and a boards-file name wins."""

    PAGE = "/page/ashby/initech"

    def setUp(self):
        super().setUp()
        self.seed_discovery("initech")
        self.initech = [ashby_job("i1", "Account Executive")]
        self.ats.routes["/posting-api/job-board/initech"] = (200, lambda _: (200, {"jobs": self.initech}))
        self.page_answers(200, "Initech Ltd", "https://initech.example/")

    def page_answers(self, status, name="", website=None):
        app = {"organization": {"name": name, "publicWebsite": website}}
        self.ats.routes[self.PAGE] = (status, f"<script>window.__appData = {json.dumps(app)};</script>")

    def page_requests(self):
        return sum(1 for _, path, _ in self.ats.requests if path == self.PAGE)

    def test_a_discovered_board_shows_its_company_and_website(self):
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "3"))
        text = Path(path).read_text()
        self.assertIn("## Initech Ltd — Account Executive · London\nhttps://jobs.ashbyhq.com/acme/i1 · Ashby · first "
                      "seen", text)
        self.assertIn("· 1 open role\nCompany: Initech Ltd · https://initech.example/\n- founding", text)
        self.assertNotIn("## initech", text)

    def test_a_board_is_looked_up_once_and_its_company_kept_across_rediscovery(self):
        self.run_cli()
        self.page_answers(500)
        self.seed_discovery("initech")
        self.initech.append(ashby_job("i2", "Enterprise Account Executive"))
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new, self.page_requests()), (0, "1", 1))
        self.assertIn("## Initech Ltd — Enterprise Account Executive · London", Path(path).read_text())
        self.assertIn("Company: Initech Ltd · https://initech.example/", Path(path).read_text())

    def test_a_website_unfit_for_the_digest_is_dropped_and_the_name_kept(self):
        for n, website in enumerate(["https://initech.example/\ud800", "https://initech.example/\nCompany: Fake"]):
            with self.subTest(website=website):
                board = f"initech{n}"
                self.PAGE = f"/page/ashby/{board}"
                self.seed_discovery(board)
                jobs = {"jobs": [ashby_job(f"x{n}", "Account Executive")]}
                self.ats.routes[f"/posting-api/job-board/{board}"] = (200, jobs)
                self.page_answers(200, "Initech Ltd", website)
                code, (_, _, path) = self.run_cli()
                self.assertEqual(code, 0)
                text = Path(path).read_text()
                self.assertIn(f"## Initech Ltd — Account Executive · London\nhttps://jobs.ashbyhq.com/acme/x{n}", text)
                self.assertNotIn("Company:", text)

    def test_a_failed_lookup_shows_the_slug_and_is_tried_again_with_the_next_match(self):
        self.page_answers(500)
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "3"))
        text = Path(path).read_text()
        self.assertIn("## initech — Account Executive", text)
        self.assertNotIn("Company: initech", text)
        self.assertIn("company_failed board=ashby:initech reason='HTTP 500'", (self.home / "runs.log").read_text())
        self.page_answers(200, "Initech Ltd")
        self.initech.append(ashby_job("i2", "Enterprise Account Executive"))
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "1"))
        self.assertIn("## Initech Ltd — Enterprise Account Executive", Path(path).read_text())
        self.assertNotIn("Company:", Path(path).read_text())  # no website given

    def test_a_boards_file_name_wins_even_for_a_posting_kept_from_an_earlier_read(self):
        self.ats.routes["/page/ashby/acme"] = (200, '<script>window.__appData = {"organization": '
                                                    '{"name": "Acme Corporation", "publicWebsite": "https://acme.example/"}};'
                                                    "</script>")
        stale = Posting(key="ashby:acme:9", ats="ashby", board="ashby:acme", company="Old Acme",
                        title="Account Executive", location="London", url="https://jobs.ashbyhq.com/acme/9",
                        description="Account Executive role. We sell software.")
        conn = store.open_db(self.home)
        try:
            store.apply_fetch(conn, "ashby:acme", (stale,), datetime.now(UTC).isoformat(timespec="microseconds"))
        finally:
            conn.close()
        self.ats.routes["/posting-api/job-board/acme"] = (500, {"error": "boom"})
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        text = Path(path).read_text()
        self.assertIn("## Acme — Account Executive · London\nhttps://jobs.ashbyhq.com/acme/9", text)
        self.assertIn("Company: Acme · https://acme.example/", text)
        self.assertNotIn("Old Acme", text)
        self.assertNotIn("Acme Corporation", text)


class GreenhouseRunTest(RunFixture):
    def test_ashby_and_greenhouse_boards_in_one_digest(self):
        (self.home / "listed.json").write_text(json.dumps({"companies": [
            {"company": "Acme", "board_url": f"{self.ats.url}/posting-api/job-board/acme"},
            {"company": "Globex", "board_url": f"{self.ats.url}/v1/boards/globex/jobs"},
        ]}))
        content = "&lt;p&gt;You'll be our &lt;strong&gt;first&lt;/strong&gt; Account Executive.&lt;/p&gt;"
        self.ats.routes["/v1/boards/globex/jobs?content=true"] = (200, {"jobs": [
            greenhouse_job(7, "Account Executive", "London, UK", content=content), greenhouse_job(8, "Engineer")]})

        def quoting_the_description(payload):
            tags = tags_quoting_the_posting(payload)
            if "first Account Executive" in payload["messages"][0]["content"]:
                tags["founding"] = {"value": "yes", "quote": "our first Account Executive"}
            return model_reply(tags)

        self.model.routes["/chat/completions"] = (200, quoting_the_description)
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "3"))
        text = Path(path).read_text()
        self.assertIn("## Globex — Account Executive · London, UK\n"
                      "https://job-boards.greenhouse.io/acme/jobs/7 · Greenhouse · first seen", text)
        self.assertIn('- founding: **yes** — "our first Account Executive"\n', text)  # verified: no ⚠
        self.assertIn("· 2 open roles", text)


    def test_a_malformed_greenhouse_board_fails_alone(self):
        (self.home / "listed.json").write_text(json.dumps({"companies": [
            {"company": "Acme", "board_url": f"{self.ats.url}/posting-api/job-board/acme"},
            {"company": "Globex", "board_url": f"{self.ats.url}/v1/boards/globex/jobs"},
        ]}))
        self.ats.routes["/v1/boards/globex/jobs?content=true"] = (200, {"jobs": [
            greenhouse_job("a\ud800", "Account Executive")]})
        code, (_, new, _) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        self.assertIn("board_failed company='Globex' board=greenhouse:globex", (self.home / "runs.log").read_text())


class WorkableGemRunTest(RunFixture):
    def test_workable_and_gem_boards_reach_the_digest(self):
        (self.home / "listed.json").write_text(json.dumps({"companies": [
            {"company": "Acme", "board_url": f"{self.ats.url}/posting-api/job-board/acme"},
            {"company": "Initech", "board_url": f"{self.ats.url}/api/v1/widget/accounts/initech"},
            {"company": "Hooli", "board_url": f"{self.ats.url}/job_board/v0/hooli/job_posts/"},
        ]}))
        self.ats.routes["/api/v1/widget/accounts/initech?details=true"] = (200, {"jobs": [
            workable_job("AB12", "Account Executive", "London"), workable_job("CD34", "Engineer"),
            workable_job("AB12", "Account Executive", "Manchester")]})
        self.ats.routes["/job_board/v0/hooli/job_posts/"] = (200, [
            gem_job("g1", "Account Executive", "London, UK"), gem_job("g2", "Engineer")])
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "4"))
        text = Path(path).read_text()
        self.assertIn("## Initech — Account Executive · London, United Kingdom / Manchester, United Kingdom\n"
                      "https://apply.workable.com/j/AB12 · Workable · first seen", text)
        self.assertIn("## Hooli — Account Executive · London, UK\nhttps://jobs.gem.com/acme/g1 · Gem · first seen",
                      text)
        self.assertEqual(text.count("· 2 open roles"), 2)  # one per new board: jobs, not Workable's entries
        self.assertEqual(self.run_cli()[1][1:], ["0", "-"])


    def test_a_sandbox_board_already_discovered_closes_its_postings_without_a_request(self):
        board = Board("gem", "acme-sandbox", "acme-sandbox", f"{self.ats.url}/job_board/v0/acme-sandbox/job_posts/")
        stored = Posting(key="gem:acme-sandbox:g1", ats="gem", board=board.id, company=board.company,
                         title="Account Executive", location="London, UK", url="https://jobs.gem.com/acme-sandbox/g1",
                         description="Test posting.")
        conn = store.open_db(self.home)
        try:
            store.replace_discovered(conn, [board, *SENTINELS], datetime.now(UTC).isoformat(timespec="microseconds"))
            store.apply_fetch(conn, board.id, (stored,), datetime.now(UTC).isoformat(timespec="microseconds"))
        finally:
            conn.close()
        self.ats.routes["/job_board/v0/acme-sandbox/job_posts/"] = (200, [gem_job("g1", "Account Executive")])
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        self.assertNotIn("acme-sandbox", Path(path).read_text())
        self.assertNotIn(("GET", "/job_board/v0/acme-sandbox/job_posts/", None), self.ats.requests)
        conn = store.open_db(self.home)
        try:
            self.assertEqual(conn.execute("SELECT closed FROM postings WHERE key = ?", (stored.key,)).fetchone()[0], 1)
        finally:
            conn.close()


class TeamtailorPersonioRunTest(RunFixture):
    def setUp(self):
        super().setUp()
        for adapter, feed in [(teamtailor, "/teamtailor/{slug}/jobs.rss"),
                              (personio, "/personio/{slug}/xml?language=en")]:
            mock.patch.object(adapter, "FEED", self.ats.url + feed).start()
        (self.home / "listed.json").write_text(json.dumps({"companies": [
            {"company": "Initech", "board_url": "https://initech.teamtailor.com/jobs"},
            {"company": "Hooli", "board_url": "https://hooli.jobs.personio.de/"},
        ]}))
        self.feed = [teamtailor_item("tt1", "Account Executive"), teamtailor_item("tt2", "Engineer")]
        self.ats.routes["/teamtailor/initech/jobs.rss?per_page=1000"] = (
            200, lambda _: (200, teamtailor_feed(*self.feed)))
        self.ats.routes["/personio/hooli/xml?language=en"] = (200, personio_feed(
            personio_position("p1", "Account Executive", "London", additional=["Berlin"]),
            personio_position("p2", "Engineer")))

        def quoting_the_description(payload):
            tags = tags_quoting_the_posting(payload)
            return model_reply({**tags, "tech_company": {"value": "yes", "quote": "We sell software."}})
        self.model.routes["/chat/completions"] = (200, quoting_the_description)

    def test_teamtailor_and_personio_boards_reach_the_digest_with_verified_quotes(self):
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        text = Path(path).read_text()
        self.assertIn("## Initech — Account Executive · London, United Kingdom\n"
                      "https://acme.teamtailor.com/jobs/tt1-job · Teamtailor · first seen", text)
        self.assertIn("## Hooli — Account Executive · London / Berlin\n"
                      "https://hooli.jobs.personio.com/job/p1 · Personio · first seen", text)
        self.assertEqual(text.count('- tech_company ⓘ: **yes** — "We sell software."\n'), 2)
        self.assertNotIn("⚠", text)
        self.assertEqual(self.run_cli()[1][1:], ["0", "-"])

    def test_a_german_only_personio_job_is_tagged_on_the_default_feeds_text(self):
        german = [("Deine Rolle", "<p>Du verkaufst Software.</p>")]
        self.ats.routes["/personio/hooli/xml?language=en"] = (200, personio_feed(
            personio_position("p1", "Account Executive", "London", sections=[])))
        self.ats.routes["/personio/hooli/xml"] = (200, personio_feed(
            personio_position("p1", "Account Executive", "London", sections=german)))

        def quoting_whichever_language(payload):
            tags = tags_quoting_the_posting(payload)
            quote = "Du verkaufst Software." if "Du verkaufst" in json.dumps(payload, ensure_ascii=False) else \
                "We sell software."
            return model_reply({**tags, "tech_company": {"value": "yes", "quote": quote}})
        self.model.routes["/chat/completions"] = (200, quoting_whichever_language)
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        self.assertIn('## Hooli — Account Executive · London\nhttps://hooli.jobs.personio.com/job/p1 · Personio · '
                      'first seen', Path(path).read_text())
        self.assertIn('- tech_company ⓘ: **yes** — "Du verkaufst Software."\n', Path(path).read_text())
        self.assertNotIn("⚠", Path(path).read_text())
        self.assertEqual(sum("Deine Rolle\\nDu verkaufst Software." in json.dumps(payload, ensure_ascii=False)
                             for _, _, payload in self.model.requests), 1)  # the prompt, as the request's JSON

    def test_a_personio_job_that_loses_its_name_closes_and_reopens_without_a_second_delivery(self):
        named = personio_position("p1", "Account Executive", "London")
        for position, new, open_roles in [(named, "2", 1), (personio_position("p1", " ", "London"), "0", 0),
                                          (named, "0", 1)]:
            self.ats.routes["/personio/hooli/xml?language=en"] = (200, personio_feed(position))
            self.assertEqual(self.run_cli()[1][1], new)
            conn = store.open_db(self.home)
            try:
                self.assertEqual(store.open_counts(conn, {"personio:hooli"}), {"personio:hooli": open_roles})
            finally:
                conn.close()

    def test_a_feed_that_may_be_cut_leaves_its_open_postings_open(self):
        self.ats.routes["/teamtailor/initech/jobs.rss?per_page=3"] = self.ats.routes[
            "/teamtailor/initech/jobs.rss?per_page=1000"]
        with mock.patch.object(teamtailor, "PER_PAGE", 3):
            self.assertEqual(self.run_cli()[1][1], "2")  # one per board
            self.feed[:] = [teamtailor_item("tt1", "Account Executive"), teamtailor_item("tt3", "Designer"),
                            teamtailor_item("tt4", "Analyst")]  # a full page that leaves tt2 out
            self.run_cli()
        self.assertIn("board_failed company='Initech' board=teamtailor:initech", (self.home / "runs.log").read_text())
        conn = store.open_db(self.home)
        try:
            rows = dict(conn.execute("SELECT key, closed FROM postings WHERE board = 'teamtailor:initech'").fetchall())
        finally:
            conn.close()
        self.assertEqual(rows, {"teamtailor:initech:tt1": 0, "teamtailor:initech:tt2": 0})


class SmartRecruitersRunTest(RunFixture):
    """A list without descriptions: matched postings are described by a detail request, never stored."""

    LIST = "/v1/companies/initrode/postings"

    def setUp(self):
        super().setUp()
        mock.patch.object(smartrecruiters, "PAGE", 2).start()
        (self.home / "listed.json").write_text(json.dumps({"companies": [
            {"company": "Initrode", "board_url": f"{self.ats.url}{self.LIST}"}]}))
        self.jobs = [smartrecruiters_job("sr1", "Account Executive"), smartrecruiters_job("sr2", "Engineer"),
                     smartrecruiters_job("sr3", "Account Executive", "Berlin, , Germany")]
        self.serve_list()
        self.detail(["We sell software."])

        def quoting_the_description(payload):
            tags = tags_quoting_the_posting(payload)
            text = payload["messages"][0]["content"].split("<description>", 1)[1]
            claim = next(line for line in text.splitlines() if line.startswith("We sell"))
            return model_reply({**tags, "tech_company": {"value": "yes", "quote": claim}})
        self.model.routes["/chat/completions"] = (200, quoting_the_description)

    def serve_list(self, pages=None):
        """The board's list, two jobs a page; `pages` replaces the pages served."""
        pages = pages or [smartrecruiters_page(self.jobs[o:o + 2], o, len(self.jobs))
                          for o in range(0, len(self.jobs), 2)]
        for page in pages:
            self.ats.routes[f"{self.LIST}?limit=2&offset={page['offset']}"] = (200, page)

    def detail(self, lines, status=200):
        sections = [("jobDescription", "About Initrode", "".join(f"<p>{line}</p>" for line in lines))]
        self.ats.routes[f"{self.LIST}/sr1"] = (status, smartrecruiters_detail("Account Executive", sections))

    def asked(self, path):
        return sum(1 for _, asked, _ in self.ats.requests if asked == path)

    def model_calls(self):
        return len(self.model.requests)

    def query(self, sql):
        conn = store.open_db(self.home)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def stored_description(self):
        return self.query("SELECT description FROM postings WHERE key = 'smartrecruiters:initrode:sr1'")[0][0]

    def tag_hashes(self):
        return {r[0] for r in self.query("SELECT input_hash FROM tag_results "
                                         "WHERE posting_key = 'smartrecruiters:initrode:sr1'")}

    def closed_flags(self):
        return dict(self.query("SELECT key, closed FROM postings WHERE board = 'smartrecruiters:initrode'"))

    def described_hash(self, text):
        board = fetch.board_for("Initrode", f"{self.ats.url}{self.LIST}")
        (posting,) = [p for p in smartrecruiters.read(board, lambda url: json.dumps(
            self.ats.routes[url.removeprefix(self.ats.url)][1]).encode(), anywhere) if p.key.endswith(":sr1")]
        return store.input_hash(replace(posting, description=text))

    def block_digests(self):
        (self.home / "digests").mkdir(exist_ok=True)
        (self.home / "digests" / "ae").write_text("a file where the directory should be")

    def test_a_matched_posting_is_described_and_its_quote_verified_but_nothing_is_stored(self):
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "1"))
        text = Path(path).read_text()
        self.assertIn("## Initrode — Account Executive · London, United Kingdom\n"
                      "https://jobs.smartrecruiters.com/initrode/sr1 · SmartRecruiters · first seen", text)
        self.assertIn('- tech_company ⓘ: **yes** — "We sell software."\n', text)
        self.assertNotIn("⚠", text)
        self.assertEqual([self.asked(f"{self.LIST}/{key}") for key in ("sr1", "sr2", "sr3")], [1, 0, 0])
        self.assertEqual(self.stored_description(), "")
        self.assertEqual(self.tag_hashes(), {self.described_hash("About Initrode\nWe sell software.")})
        self.assertIn("describe_done described=1 failed=0", (self.home / "runs.log").read_text())
        self.assertEqual(self.run_cli()[1][1:], ["0", "-"])

    def test_a_failed_detail_waits_for_the_next_run_without_failing_this_one(self):
        self.detail(["We sell software."], status=500)
        self.assertEqual(self.run_cli(), (0, ["ae", "0", "-"]))
        log = (self.home / "runs.log").read_text()
        self.assertIn("describe_failed posting=smartrecruiters:initrode:sr1 reason='HTTP 500'", log)
        self.assertEqual(self.model_calls(), 0)
        self.detail(["We sell software."])
        self.assertEqual(self.run_cli()[1][1], "1")

    def test_a_detail_without_description_text_waits_for_the_next_run(self):
        for detail, reason in [({"jobAd": {"sections": {}}}, "detail has no `jobDescription` section"),
                               (smartrecruiters_detail("AE", [("jobDescription", "Job", "<p> </p>")]),
                                "detail has no text")]:
            self.ats.routes[f"{self.LIST}/sr1"] = (200, detail)
            self.assertEqual(self.run_cli(), (0, ["ae", "0", "-"]))
            self.assertIn(f"describe_failed posting=smartrecruiters:initrode:sr1 reason='{reason}'",
                          (self.home / "runs.log").read_text())
        self.assertEqual(self.model_calls(), 0)
        self.detail(["We sell software."])
        self.assertEqual(self.run_cli()[1][1], "1")

    def test_tags_are_reused_while_the_description_holds_and_redone_when_it_changes(self):
        self.block_digests()
        for run in (1, 2):  # nothing is delivered, so the posting stays pending and is described each run
            self.assertEqual(self.run_cli(), (1, ["ae", "0", "-"]), run)
            self.assertEqual((self.asked(f"{self.LIST}/sr1"), self.model_calls()), (run, 1))
        (self.home / "digests" / "ae").unlink()
        self.detail(["We sell hardware."])
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new, self.asked(f"{self.LIST}/sr1"), self.model_calls()), (0, "1", 3, 2))
        self.assertIn('- tech_company ⓘ: **yes** — "We sell hardware."\n', Path(path).read_text())
        self.assertIn(self.described_hash("About Initrode\nWe sell hardware."), self.tag_hashes())

    def test_a_list_that_shifts_while_paging_fails_the_board_and_keeps_its_postings_open(self):
        self.assertEqual(self.run_cli()[1][1], "1")
        jobs = self.jobs
        self.serve_list([smartrecruiters_page(jobs[0:2], 0, 3), smartrecruiters_page(jobs[1:2], 2, 3)])
        self.assertEqual(self.run_cli(), (0, ["ae", "0", "-"]))
        self.assertIn("board_failed company='Initrode' board=smartrecruiters:initrode reason='duplicate job ids'",
                      (self.home / "runs.log").read_text())
        self.assertEqual(self.closed_flags(), {f"smartrecruiters:initrode:sr{n}": 0 for n in (1, 2, 3)})

    def test_the_boards_file_endpoint_describes_even_while_its_list_fails(self):
        conn = store.open_db(self.home)
        try:  # discovery names the same board on a host that answers nothing
            store.replace_discovered(conn, [Board("smartrecruiters", "initrode", "initrode",
                                                  "http://127.0.0.1:9/v1/companies/initrode/postings"), *SENTINELS],
                                     datetime.now(UTC).isoformat(timespec="microseconds"))
        finally:
            conn.close()
        self.block_digests()
        self.assertEqual(self.run_cli()[0], 1)
        (self.home / "digests" / "ae").unlink()
        self.ats.routes[f"{self.LIST}?limit=2&offset=0"] = (500, {"error": "boom"})
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new, self.asked(f"{self.LIST}/sr1")), (0, "1", 2))
        self.assertIn("## Initrode — Account Executive", Path(path).read_text())


class RipplingRunTest(RunFixture):
    """Rows of one job merge; a matched job is described by a detail request."""

    LIST = "/api/v2/board/initrode/jobs"

    def setUp(self):
        super().setUp()
        (self.home / "listed.json").write_text(json.dumps({"companies": [
            {"company": "Initrode", "board_url": f"{self.ats.url}{self.LIST}"}]}))
        self.ats.routes[f"{self.LIST}?page=0&pageSize=1000"] = (200, rippling_page([
            rippling_item("r1", "Account Executive", "London, United Kingdom"),
            rippling_item("r2", "Engineer"),
            rippling_item("r1", "Account Executive", "Hybrid (Manchester, England, GB)")]))
        self.ats.routes[f"{self.LIST}/r1"] = (200, rippling_detail(role="<p>You will be our first sales hire.</p>"))

        def quoting_the_description(payload):
            return model_reply({**tags_quoting_the_posting(payload),
                                "tech_company": {"value": "yes", "quote": "We sell software."},
                                "founding": {"value": "yes", "quote": "You will be our first sales hire."}})
        self.model.routes["/chat/completions"] = (200, quoting_the_description)

    def test_a_matched_job_reaches_the_digest_with_every_location_and_verified_quotes(self):
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "1"))
        text = Path(path).read_text()
        self.assertIn("## Initrode — Account Executive · London, United Kingdom / Hybrid (Manchester, England, GB), "
                      "United Kingdom\nhttps://ats.rippling.com/initrode/jobs/r1 · Rippling · first seen", text)
        self.assertIn("· 2 open roles", text)  # jobs, not rows
        self.assertIn('- founding: **yes** — "You will be our first sales hire."\n', text)
        self.assertIn('- tech_company ⓘ: **yes** — "We sell software."\n', text)
        self.assertNotIn("⚠", text)
        self.assertEqual([path for _, path, _ in self.ats.requests if "/jobs/" in path], [f"{self.LIST}/r1"])

    def test_a_detail_without_text_waits_for_the_next_run(self):
        self.ats.routes[f"{self.LIST}/r1"] = (200, rippling_detail(company="", role="<p></p>"))
        self.assertEqual(self.run_cli(), (0, ["ae", "0", "-"]))
        self.assertIn("describe_failed posting=rippling:initrode:r1 reason='detail has no text'",
                      (self.home / "runs.log").read_text())
        self.assertEqual(len(self.model.requests), 0)
        self.ats.routes[f"{self.LIST}/r1"] = (200, rippling_detail())
        self.assertEqual(self.run_cli()[1][1], "1")

    def test_totals_that_move_between_pages_fail_the_board_and_keep_its_postings_open(self):
        self.assertEqual(self.run_cli()[1][1], "1")
        with mock.patch.object(rippling, "PAGE", 2):
            self.ats.routes[f"{self.LIST}?page=0&pageSize=2"] = (200, rippling_page(
                [rippling_item("r1", "Account Executive"), rippling_item("r3", "Designer")], 0, 3, 2))
            self.ats.routes[f"{self.LIST}?page=1&pageSize=2"] = (200, rippling_page(
                [rippling_item("r4", "Analyst")], 1, 4, 2))
            self.assertEqual(self.run_cli(), (0, ["ae", "0", "-"]))
        self.assertIn("board_failed company='Initrode' board=rippling:initrode reason='list changed while paging",
                      (self.home / "runs.log").read_text())
        conn = store.open_db(self.home)
        try:
            rows = dict(conn.execute("SELECT key, closed FROM postings WHERE board = 'rippling:initrode'"))
        finally:
            conn.close()
        self.assertEqual(rows, {"rippling:initrode:r1": 0, "rippling:initrode:r2": 0})


class NotifyTest(RunFixture):
    """`run --notify` tells the desktop about news and problems, never about a quiet run, and never changes the
    outcome."""

    def run_notified(self):
        out = io.StringIO()
        with mock.patch.object(notify, "notify") as sent, contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["run", "--notify"])
        return code, [c.args[0] for c in sent.call_args_list]

    def test_news_then_quiet(self):
        code, sent = self.run_notified()
        self.assertEqual((code, sent), (0, [f"2 new postings in {self.home / 'digests' / 'ae'}"]))
        self.assertEqual(self.run_notified(), (0, []))

    def test_untagged_postings(self):
        self.model.routes["/chat/completions"] = (503, {"error": "down"})
        self.assertEqual(self.run_notified(), (1, ["2 couldn't be tagged yet; see runs.log"]))

    def test_digest_write_failure(self):
        (self.home / "digests").mkdir()
        (self.home / "digests" / "ae").write_text("a file where the directory should be")
        self.assertEqual(self.run_notified(), (1, ["the digest couldn't be written; see runs.log"]))

    def test_config_error(self):
        (self.home / "config.toml").write_text("not toml [")
        code, sent = self.run_notified()
        self.assertEqual(code, 2)
        self.assertTrue(sent[0].startswith("jobscan couldn't run: "), sent)

    def test_crash(self):
        with mock.patch.object(notify, "notify") as sent, self.assertRaises(RuntimeError), \
                mock.patch.object(scan, "scan", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(io.StringIO()):
            cli.main(["run", "--notify"])
        self.assertIn("stopped with an error (RuntimeError)", sent.call_args.args[0])

    def test_failing_notifier_changes_nothing(self):
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(notify.subprocess, "run", side_effect=subprocess.TimeoutExpired("osascript", 10)):
            code, (_, new, _) = self.run_cli_notify()
        self.assertEqual((code, new), (0, "2"))
        self.assertIn("notify_failed error=TimeoutExpired", (self.home / "runs.log").read_text())
        self.assertEqual(self.run_cli(), (0, ["ae", "0", "-"]))

    def run_cli_notify(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["run", "--notify"])
        return code, out.getvalue().strip().split("\t")


class WorkdayRunTest(RunFixture):
    """A Workday site read through its place filters: only the profile's places are queried, matched postings are
    described, and the stored list follows the profile's locations."""

    SITE = "/workday/acme.wd3/wday/cxs/acme/careers"

    def setUp(self):
        super().setUp()
        self.write_config(boards_file=False)
        board = workday.page_board("", "https://acme.wd3.myworkdayjobs.com/en-US/Careers")
        conn = store.open_db(self.home)
        try:
            store.replace_discovered(conn, [board, *SENTINELS], datetime.now(UTC).isoformat(timespec="microseconds"))
            with conn:
                conn.execute("UPDATE boards SET not_found = ? WHERE slug = 'sentinel'", (store.DEAD_AFTER,))
        finally:
            conn.close()
        site = workday_site([workday_job("AE_R1", "Account Executive", ["London"]),
                             workday_job("AE_R2", "Account Executive", ["Paris"]),
                             workday_job("AE_R3", "Account Executive", ["London", "Leeds"])],
                            {"locations": ["Leeds", "London", "Paris"]})
        self.filtered_status = 200  # what queries through a filter value answer

        def answer(body):
            return (self.filtered_status, {}) if body["appliedFacets"] and self.filtered_status != 200 else site(body)
        self.ats.routes[f"{self.SITE}/jobs"] = (200, answer)
        for job in ("AE_R1", "AE_R2", "AE_R3"):
            self.detail(job)

        def quoting_the_description(payload):  # a quote only the fetched detail holds
            return model_reply({**tags_quoting_the_posting(payload),
                                "tech_company": {"value": "yes", "quote": "We sell software."}})
        self.model.routes["/chat/completions"] = (200, quoting_the_description)

    def detail(self, job, status=200):
        self.ats.routes[f"{self.SITE}/job/{job}"] = (status, workday_detail("<p>We sell software.</p>"))

    def filters_asked(self):
        return [body["appliedFacets"] for method, path, body in self.ats.requests if path == f"{self.SITE}/jobs"]

    def details_asked(self):
        return sorted(path.rsplit("/", 1)[1] for _, path, _ in self.ats.requests if "/job/" in path)

    def locations(self, *places):
        (self.home / "profiles" / "ae.toml").write_text(PROFILE.replace('["London"]', json.dumps(list(places))))

    def open_flags(self):
        conn = store.open_db(self.home)
        try:
            return dict(conn.execute("SELECT key, closed FROM postings WHERE ats = 'workday'"))
        finally:
            conn.close()

    def test_only_the_profiles_places_are_queried_and_matched_postings_are_described(self):
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        self.assertEqual(self.filters_asked(), [{}, {"locations": ["id-London"]}])
        self.assertEqual(self.details_asked(), ["AE_R1", "AE_R3"])
        text = Path(path).read_text()
        self.assertIn(f"## acme — Account Executive · London\n{self.ats.url}/workday/acme.wd3/careers/job/Somewhere/"
                      "AE_R1 · Workday · first seen", text)
        self.assertIn("/careers/job/Somewhere/AE_R3 · Workday", text)  # `2 Locations`, placed by its filter value
        self.assertNotIn("⚠", text)
        self.assertEqual(text.count('tech_company ⓘ: **yes** — "We sell software."'), 2)
        self.assertTrue(all("We sell software." in body["messages"][0]["content"]
                            for _, _, body in self.model.requests))
        self.assertIn("describe_done described=2 failed=0", (self.home / "runs.log").read_text())

    def test_the_stored_list_follows_the_profiles_places_and_returns_without_redelivery(self):
        self.detail("AE_R3", status=500)
        self.assertEqual(self.run_cli()[1][1], "1")  # AE_R3 is left pending
        self.locations("Paris")
        self.assertEqual(self.run_cli()[1][1], "1")  # AE_R2
        self.assertEqual(self.open_flags(), {"workday:acme.wd3.careers:R1": 1, "workday:acme.wd3.careers:R2": 0,
                                             "workday:acme.wd3.careers:R3": 1})
        self.locations("London")
        self.detail("AE_R3")
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "1"))
        self.assertIn("AE_R3 · Workday", Path(path).read_text())
        self.assertNotIn("AE_R1 · Workday", Path(path).read_text())
        self.assertEqual(self.open_flags()["workday:acme.wd3.careers:R1"], 0)

    def test_a_failing_place_query_fails_the_board_and_closes_nothing(self):
        self.run_cli()
        self.filtered_status = 500
        self.run_cli()
        self.assertIn("board_failed company='acme.wd3.careers' board=workday:acme.wd3.careers",
                      (self.home / "runs.log").read_text())
        self.assertEqual(set(self.open_flags().values()), {0})


class WorkdayTwinsRunTest(RunFixture):
    """One job on several Workday sites of a company: one digest entry, one description, one model call."""

    def setUp(self):
        super().setUp()
        self.write_config(boards_file=False)
        self.sites = {"careers": [workday_job("Account-Executive_R1", "Account Executive"),
                                  workday_job("Account-Executive_R9", "Account Executive")],
                      "europe": [workday_job("Account-Executive--UK_R1-2", "Account Executive, UK")],
                      "emea": [workday_job("AE_R1-3", "Account Executive")]}
        self.discover("careers", "europe")
        for name, jobs in self.sites.items():
            base = f"/workday/acme.wd3/wday/cxs/acme/{name}"
            self.ats.routes[f"{base}/jobs"] = (200, workday_site(jobs, {"locations": ["London"]}))
            for job in jobs:
                link = job["externalPath"].rsplit("/", 1)[1]
                self.ats.routes[f"{base}/job/{link}"] = (200, workday_detail(f"<p>We sell software. {link}</p>"))

    def discover(self, *names):
        boards = [workday.page_board("", f"https://acme.wd3.myworkdayjobs.com/{name}") for name in names]
        conn = store.open_db(self.home)
        try:
            store.replace_discovered(conn, [*boards, *SENTINELS], datetime.now(UTC).isoformat(timespec="microseconds"))
            with conn:
                conn.execute("UPDATE boards SET not_found = ? WHERE slug = 'sentinel'", (store.DEAD_AFTER,))
        finally:
            conn.close()

    def details_asked(self):
        return sorted(path.split("/wday/cxs/acme/", 1)[1] for _, path, _ in self.ats.requests if "/job/" in path)

    def test_a_job_on_two_sites_is_tagged_and_shown_once_and_never_again(self):
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))  # R1 once, R9
        text = Path(path).read_text()
        self.assertEqual(text.count("## acme — Account Executive"), 2)
        self.assertIn("/careers/job/Somewhere/Account-Executive_R1 · Workday", text)
        self.assertNotIn("Account Executive, UK", text)
        self.assertEqual(len(self.model.requests), 2)
        self.assertEqual(self.details_asked(), ["careers/job/Account-Executive_R1", "careers/job/Account-Executive_R9"])
        self.assertIn("twins_skipped postings=1", (self.home / "runs.log").read_text())
        self.assertEqual(self.run_cli()[1][1], "0")
        self.discover("careers", "europe", "emea")  # a third site lists it later
        self.assertEqual(self.run_cli()[1][1], "0")
        self.assertEqual(len(self.model.requests), 2)


class OracleRunTest(RunFixture):
    """Oracle sites read whole: matched postings described and named from the site page, one entry per job across a
    pod's sites, and a read that may skip jobs closing nothing."""

    def setUp(self):
        super().setUp()
        self.write_config(boards_file=False)
        self.boards = [oracle.page_board("", f"https://acme.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/{s}")
                       for s in ("CX", "HTV")]
        conn = store.open_db(self.home)
        try:
            store.replace_discovered(conn, [*self.boards, *SENTINELS],
                                     datetime.now(UTC).isoformat(timespec="microseconds"))
            with conn:
                conn.execute("UPDATE boards SET not_found = ? WHERE slug = 'sentinel'", (store.DEAD_AFTER,))
        finally:
            conn.close()
        self.sites = {"cx": [oracle_job("42", "Account Executive", "London, United Kingdom"),
                             oracle_job("43", "Account Executive", "Paris, France")],
                      "htv": [oracle_job("42", "Account Executive", "London, United Kingdom")]}
        for site in self.sites:
            self.list(site)
            self.route(oracle.PAGE_PATH.format(site=site), "<html><title>Acme Television</title></html>")
            for job in self.sites[site]:
                self.route(oracle.DETAIL.format(site=site, id=job["Id"]),
                           oracle_detail(ExternalDescriptionStr="<p>We sell software.</p>"))

        def quoting_the_description(payload):  # a quote only the fetched detail holds
            return model_reply({**tags_quoting_the_posting(payload),
                                "tech_company": {"value": "yes", "quote": "We sell software."}})
        self.model.routes["/chat/completions"] = (200, quoting_the_description)

    def route(self, path, answer):
        self.ats.routes["/oracle/acme.em2" + path] = (200, answer)

    def list(self, site, total=None):
        self.route(oracle.LIST.format(site=site, limit=200, offset=0), oracle_page(self.sites[site], total=total))

    def asked(self, word):
        return [path for _, path, _ in self.ats.requests if word in path]

    def test_a_job_on_two_sites_of_a_pod_is_described_named_and_shown_once(self):
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "1"))
        text = Path(path).read_text()
        self.assertEqual(text.count("## Acme Television — Account Executive"), 1)
        self.assertIn("/hcmUI/CandidateExperience/en/sites/cx/job/42 · Oracle", text)
        self.assertIn("We sell software.", json.dumps(self.model.requests))  # the fetched description was tagged
        self.assertEqual(len(self.model.requests), 1)
        self.assertEqual(len(self.asked("recruitingCEJobRequisitionDetails")), 1)
        self.assertEqual(self.asked("/hcmUI/CandidateExperience/en/sites/"), [
            "/oracle/acme.em2/hcmUI/CandidateExperience/en/sites/cx"])
        self.assertIn("twins_skipped postings=1", (self.home / "runs.log").read_text())
        self.assertEqual(self.run_cli()[1][1], "0")
        self.assertEqual(len(self.model.requests), 1)

    def test_a_read_that_may_skip_jobs_fails_the_board_and_closes_nothing(self):
        self.assertEqual(self.run_cli()[0], 0)
        self.list("cx", total=450)  # a short page before the end: the pod may be paging fewer than 200
        self.list("htv", total=10_001)
        self.run_cli()
        log = (self.home / "runs.log").read_text()
        self.assertIn("a page before the end holds 2 jobs", log)
        self.assertIn("over 10,000 jobs", log)
        conn = store.open_db(self.home)
        try:
            rows = conn.execute("SELECT key, closed FROM postings WHERE ats = 'oracle'")
            closed = {r["key"]: r["closed"] for r in rows}
        finally:
            conn.close()
        self.assertEqual(closed, {"oracle:acme.em2.cx:42": 0, "oracle:acme.em2.cx:43": 0, "oracle:acme.em2.htv:42": 0})


class BackupRunTest(RunFixture):
    """The first run of new code copies the database before using it; a copy that fails stops the run."""

    def setUp(self):
        super().setUp()
        mock.patch.object(backup, "code_identity", lambda: "f" * 64).start()  # code this database hasn't seen

    def test_the_first_run_of_new_code_copies_the_database(self):
        self.assertEqual(self.run_cli()[0], 0)
        (copy,) = (self.home / "backups").glob("auto-*-ffffffff.sqlite")
        self.assertIn(f"backup_done path={copy}", (self.home / "runs.log").read_text())
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(len(list((self.home / "backups").glob("auto-*.sqlite"))), 1)

    def test_a_copy_that_fails_stops_the_run_before_any_work(self):
        with mock.patch.object(os, "link", side_effect=OSError(28, "No space left on device")):
            code, _ = self.run_cli()
        self.assertEqual(code, 2)
        self.assertEqual(self.ats.requests, [])
        self.assertEqual(self.model.requests, [])


class EmailRunTest(RunFixture):
    """With email.toml, a run that publishes a digest emails its text; a failed send is reported, never resent."""

    PASSWORD = "app\\\\secret"  # a backslash: escaped when printed, so masking must happen on the text itself

    def setUp(self):
        super().setUp()
        self.write_email()
        self.calls, self.refuse = [], {}
        for patcher in patch_smtp(self.calls, self.refuse):
            patcher.start()

    def write_email(self, mode=0o600, password=PASSWORD):
        path = self.home / "email.toml"
        path.write_text('to = "doa@example.com"\nfrom = "me@gmail.com"\nhost = "smtp.gmail.com"\nport = 465\n'
                        + (f'password = "{password}"\n' if password else ""))
        path.chmod(mode)

    def sent(self):
        """Each sent message as the recipient's client reads it."""
        return [BytesParser(policy=policy.default).parsebytes(args["message"].as_bytes(policy=policy.SMTP))
                for name, args in self.calls if name == "send"]

    def run_notified(self):
        out = io.StringIO()
        with mock.patch.object(notify, "notify") as notified, contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["run", "--notify"])
        return code, out.getvalue().strip().split("\t"), [c.args[0] for c in notified.call_args_list]

    def test_the_digest_is_emailed_and_a_quiet_run_sends_nothing(self):
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        (message,) = self.sent()
        self.assertEqual((message["Subject"], message["From"], message["To"]),
                         ("jobscan: 2 new for ae", "me@gmail.com", "doa@example.com"))
        self.assertEqual(message.get_content().replace("\r\n", "\n"), Path(path).read_text(encoding="utf-8"))
        log = (self.home / "runs.log").read_text()
        self.assertIn("INFO email_sent\n", log)
        self.assertNotIn("doa@example.com", log)
        self.assertEqual(self.run_cli(), (0, ["ae", "0", "-"]))
        self.assertEqual(len(self.sent()), 1)

    def test_a_digest_of_untagged_postings_is_emailed_too(self):
        self.model.routes["/chat/completions"] = (503, {"error": "down"})
        self.assertEqual(self.run_cli()[0], 1)
        (message,) = self.sent()
        self.assertEqual(message["Subject"], "jobscan: 0 new for ae · 2 untagged")

    def test_no_digest_written_no_email(self):
        (self.home / "digests").mkdir()
        (self.home / "digests" / "ae").write_text("a file where the directory should be")
        self.assertEqual(self.run_cli()[0], 1)
        self.assertEqual(self.calls, [])

    def test_a_failed_send_is_reported_and_never_resent(self):
        self.refuse["login"] = smtplib.SMTPAuthenticationError(535, b"5.7.8 app\\secret not accepted")
        code, (_, new, path), notified = self.run_notified()
        self.assertEqual((code, new), (1, "2"))
        self.assertEqual(notified, [f"2 new postings in {self.home / 'digests' / 'ae'}; "
                                    "the digest couldn't be emailed; see runs.log"])
        self.assertTrue(Path(path).exists())
        log = (self.home / "runs.log").read_text()
        self.assertIn("ERROR email_failed reason='SMTPAuthenticationError: 535 5.7.8 <password> not accepted'", log)
        self.assertNotIn("secret", log)
        self.assertIn("<password> not accepted", log)
        del self.refuse["login"]
        self.assertEqual(self.run_notified(), (0, ["ae", "0", "-"], []))
        self.assertEqual(self.sent(), [])
        self.assertEqual(len(self.digests()), 1)

    def test_a_digest_that_cant_be_read_back_is_a_failed_send(self):
        readable = Path.read_text

        def unreadable_digests(path, *args, **kwargs):
            if path.parent == self.home / "digests" / "ae":
                raise PermissionError(13, "Permission denied")
            return readable(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", autospec=True, side_effect=unreadable_digests):
            code, _, notified = self.run_notified()
        self.assertEqual(code, 1)
        self.assertIn("the digest couldn't be emailed; see runs.log", notified[0])
        self.assertEqual(self.sent(), [])

    def test_a_broken_email_setup_stops_the_run_before_any_work(self):
        for mode, password in [(0o644, self.PASSWORD), (0o600, None)]:
            self.write_email(mode, password)
            code, _, notified = self.run_notified()
            self.assertEqual(code, 2, (mode, password))
            self.assertIn("email.toml", notified[0])
            self.assertEqual((self.ats.requests, self.calls), ([], []))


class CustomTagsTest(RunFixture):
    def setUp(self):
        super().setUp()
        (self.home / "profiles" / "ae.toml").write_text(
            'titles = ["Account Executive"]\nlocations = ["London"]\neligible_where = "the UK"\n'
            'tags = ["founding", "location_eligible"]\n'
            '[custom_tags.motion]\ndefinition = "Who the role sells to."\nvalues = ["b2b", "b2c"]\nmulti = true\n')

        def answering(payload):
            tags = tags_quoting_the_posting(payload)
            return model_reply({"founding": tags["founding"], "location_eligible": tags["location_eligible"],
                                "motion": {"value": ["b2b"], "quote": "We sell software."}})

        self.model.routes["/chat/completions"] = (200, answering)

    def test_custom_tag_reaches_the_digest_marked(self):
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        text = Path(path).read_text()
        self.assertEqual(text.count(digest.LEGEND), 1)
        self.assertEqual(text.count('- motion ⓘ: **b2b** — "We sell software."'), 2)
        self.assertIn('- founding: **yes**', text)

    def test_cached_custom_tags_render_the_same(self):
        (self.home / "digests").mkdir()
        (self.home / "digests" / "ae").write_text("a file where the directory should be")
        self.assertEqual(self.run_cli()[0], 1)
        calls = len(self.model.requests)
        (self.home / "digests" / "ae").unlink()
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new, len(self.model.requests)), (0, "2", calls))
        text = Path(path).read_text()
        self.assertEqual(text.count(digest.LEGEND), 1)
        self.assertEqual(text.count('- motion ⓘ: **b2b** — "We sell software."'), 2)


class RequireRunTest(RunFixture):
    """`require`: a posting a required tag definitely rules out is listed under Left out, never as an entry, and
    delivered only with a digest that lists it."""

    def setUp(self):
        super().setUp()
        (self.home / "profiles" / "ae.toml").write_text(PROFILE + 'require = { language_required = "no" }\n')
        self.jobs = [ashby_job("1", "Account Executive"), ashby_job("2", "Account Executive - German Speaking"),
                     ashby_job("3", "Account Executive - Dutch, or so we say")]
        self.down = set()  # titles the model fails on

        def answering(payload):
            tags = tags_quoting_the_posting(payload)
            title = tags["founding"]["quote"]
            if title in self.down:
                return 503, {"error": "down"}
            if "German" in title:
                tags["language_required"] = {"value": "German", "quote": title}
            if "Dutch" in title:  # an answer whose quote isn't in the posting: shown with ⚠, never hidden
                tags["language_required"] = {"value": "Dutch", "quote": "Fluent Dutch required"}
            return model_reply(tags)

        self.model.routes["/chat/completions"] = (200, answering)

    def test_a_ruled_out_posting_is_listed_once_under_left_out(self):
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        text = Path(path).read_text()
        self.assertTrue(text.startswith(f"# ae — {Path(path).stem} · 2 new · 1 left out\n"))
        self.assertIn("## Acme — Account Executive · London\n", text)
        self.assertIn('- language_required: **Dutch** — "Fluent Dutch required" ⚠ quote not in posting', text)
        self.assertNotIn("## Acme — Account Executive - German Speaking", text)
        self.assertTrue(text.endswith(
            "## Left out (1)\n" + digest.LEFT_OUT_NOTE + "\n- Acme — Account Executive - German Speaking · London · "
            "language_required: German · https://jobs.ashbyhq.com/acme/2\n"))
        self.assertEqual(self.run_cli(), (0, ["ae", "0", "-"]))
        self.assertEqual(len(self.digests()), 1)

    def test_left_out_alone_publishes_nothing_and_waits_for_the_next_digest(self):
        self.jobs = [self.jobs[1]]
        self.assertEqual(self.run_cli(), (0, ["ae", "0", "-"]))
        self.assertEqual(self.digests(), [])
        self.assertIn("left_out_pending profile=ae postings=1", (self.home / "runs.log").read_text())
        calls = len(self.model.requests)
        self.jobs.append(ashby_job("1", "Account Executive"))
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new, len(self.model.requests)), (0, "1", calls + 1))  # the held posting was cached
        self.assertIn("## Left out (1)\n", Path(path).read_text())
        self.assertEqual(self.run_cli(), (0, ["ae", "0", "-"]))

    def test_a_failed_digest_write_delivers_neither_kind(self):
        (self.home / "digests").mkdir()
        (self.home / "digests" / "ae").write_text("a file where the directory should be")
        self.assertEqual(self.run_cli()[0], 1)
        (self.home / "digests" / "ae").unlink()
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        self.assertIn("## Left out (1)\n", Path(path).read_text())

    def test_a_carried_over_digest_delivers_the_left_out_and_keeps_the_untagged(self):
        self.jobs = self.jobs[:2]
        self.down.add("Account Executive")
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (1, "0"))
        text = Path(path).read_text()
        self.assertTrue(text.startswith(f"# ae — {Path(path).stem} · 0 new · 1 untagged, carried over · 1 left out\n"))
        self.assertIn("- Acme — Account Executive - German Speaking · London · language_required: German", text)
        self.down.clear()
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "1"))
        self.assertNotIn("Left out", Path(path).read_text())


class AgentSetupTest(RunFixture):
    """The set-up an agent runs, with no terminal: every step by flags, secrets on stdin, then a run and a preview."""

    def setUp(self):
        super().setUp()
        for name in ("config.toml", "listed.json", "profiles/ae.toml"):
            (self.home / name).unlink()
        self.calls = []
        for patcher in patch_smtp(self.calls):
            patcher.start()
        # check asks launchd: a fake one, never the host's
        mock.patch.object(schedule, "launchd", return_value=schedule.Launchd(
            self.home.parent / "LaunchAgents", StatefulLaunchctl())).start()
        # init writes the OpenRouter config; here the model is the fake one.
        mock.patch.object(init, "CONFIG", f'[model]\nbase_url = "{self.model.url}"\nslug = "test/model"\n').start()

    def step(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(stdin)):
            code = cli.main(list(argv))
        self.assertEqual(code, 0, (argv, err.getvalue()))
        return out.getvalue()

    def test_flags_alone_set_up_an_install_that_runs_and_previews(self):
        self.step("init", "--name", "ae", "--title", "Account Executive", "--location", "London",
                  "--eligible-where", "the UK", "--key-stdin", stdin="sk-or-v1-abc\n")
        self.step("email", "--to", "me@gmail.com", "--password-stdin", stdin="abcd efgh ijkl mnop\n")
        self.step("boards", "add", f"{self.ats.url}/posting-api/job-board/acme", "--company", "Acme")
        self.assertIn("OK: `jobscan run` can start.", self.step("check"))
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        self.assertIn("## Acme — Enterprise Account Executive · London", Path(path).read_text())
        self.assertEqual(sum(1 for name, _ in self.calls if name == "send"), 2)  # the test email, then the digest
        out = self.step("preview")
        self.assertIn("new jobs the next run would tag: 0", out)
        self.assertIn("already sent to you: 2", out)


class BoardSourcesTest(RunFixture):
    """Boards come from the boards file and from discovery; a board is one board whichever source names it."""

    def discover(self, *slugs):
        self.seed_discovery(*slugs)

    def list_boards(self, *slugs):
        (self.home / "listed.json").write_text(json.dumps({"companies": [
            {"company": s.title(), "board_url": f"{self.ats.url}/posting-api/job-board/{s}"} for s in slugs]}))

    def requests_to(self, slug):
        return sum(1 for _, path, _ in self.ats.requests if path == f"/posting-api/job-board/{slug}")

    def test_discovered_only_install(self):
        self.write_config(boards_file=False)
        self.discover("acme")
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        self.assertIn("## acme — Enterprise Account Executive · London", Path(path).read_text())

    def test_listed_name_wins_over_the_discovered_slug(self):
        self.list_boards("acme")
        self.discover("acme")
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        self.assertIn("## Acme — Enterprise Account Executive · London", Path(path).read_text())
        self.assertEqual(self.requests_to("acme"), 1)

    def test_no_boards_is_a_usage_error(self):
        self.write_config(boards_file=False)
        self.assertEqual(self.run_cli()[0], 2)

    def test_with_no_boards_file_configured_the_home_boards_json_is_read(self):
        self.write_config(boards_file=False)
        self.list_boards("acme")
        (self.home / "listed.json").rename(self.home / "boards.json")  # where `jobscan boards add` writes
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        self.assertIn("## Acme — Enterprise Account Executive · London", Path(path).read_text())

    def test_board_moving_between_sources_is_read_once_and_never_redelivered(self):
        self.write_config(boards_file=False)
        self.discover("acme")
        self.assertEqual(self.run_cli()[1][1], "2")
        self.write_config(boards_file=True)
        self.list_boards("ACME")
        before = self.requests_to("acme")
        self.assertEqual(self.run_cli(), (0, ["ae", "0", "-"]))
        self.assertEqual(self.requests_to("acme"), before + 1)
        self.discover()
        self.assertEqual(self.run_cli(), (0, ["ae", "0", "-"]))
        self.write_config(boards_file=False)
        self.discover("acme")
        self.assertEqual(self.run_cli(), (0, ["ae", "0", "-"]))

    def test_discovered_board_is_skipped_after_three_404s_but_a_listed_one_never(self):
        self.list_boards("acme", "listedgone")
        self.discover("gone")
        for _ in range(store.DEAD_AFTER + 2):
            self.run_cli()
        self.assertEqual(self.requests_to("gone"), store.DEAD_AFTER)
        self.assertEqual(self.requests_to("listedgone"), store.DEAD_AFTER + 2)

    def test_carried_over_postings_of_a_404_board_wait_and_of_a_failing_board_go_out(self):
        for status, delivered in [(404, "0"), (500, "2")]:
            with self.subTest(status=status):
                self.setUp()
                self.list_boards("acme")
                self.model.routes["/chat/completions"] = (503, {"error": "down"})
                self.assertEqual(self.run_cli()[0], 1)
                self.model_ok()
                self.ats.routes["/posting-api/job-board/acme"] = (status, {"error": "x"})
                self.assertEqual(self.run_cli()[1][1], delivered)

    def test_board_back_after_a_404_delivers_its_carried_postings(self):
        self.list_boards("acme")
        self.model.routes["/chat/completions"] = (503, {"error": "down"})
        self.run_cli()
        self.model_ok()
        self.ats.routes["/posting-api/job-board/acme"] = (404, {"error": "x"})
        self.assertEqual(self.run_cli()[1][1], "0")
        self.ats.routes["/posting-api/job-board/acme"] = (200, lambda _: (200, {"jobs": self.jobs}))
        self.assertEqual(self.run_cli()[1][1], "2")

    def test_board_dropped_from_discovery_contributes_nothing_until_listed(self):
        self.write_config(boards_file=False)
        self.discover("acme")
        self.model.routes["/chat/completions"] = (503, {"error": "down"})
        self.run_cli()
        self.model_ok()
        self.ats.routes["/posting-api/job-board/other"] = (200, {"jobs": []})
        self.discover("other")
        self.assertEqual(self.run_cli(), (0, ["ae", "0", "-"]))
        self.write_config(boards_file=True)
        self.list_boards("acme")
        self.assertEqual(self.run_cli()[1][1], "2")


DISCOVERED = ["ashby:acme", "gem:gem-acme", "greenhouse:gh-acme", "greenhouse:gh-eu", "oracle:or-acme.em2.cx",
              "personio:pe-acme", "rippling:rp-acme", "smartrecruiters:sr-acme", "teamtailor:tt-acme",
              "workday:wd-acme.wd3.careers"]  # never Workable


class DiscoveryRefreshTest(RunFixture):
    """`run` rediscovers boards itself when the list is empty or 30 days old, and carries on if it can't."""

    def setUp(self):
        super().setUp()
        self.tripwire.stop()
        self.cc = FakeFiles()
        self.addCleanup(self.cc.close)
        for target, name, value in [(crawl, "BASE", self.cc.url), (crawl, "RETRY_PAUSES", (0, 0, 0)),
                                    (ashby, "API", f"{self.ats.url}/posting-api/job-board/"),
                                    (greenhouse, "API", f"{self.ats.url}/v1/boards/"),
                                    (workable, "API", f"{self.ats.url}/api/v1/widget/accounts/"),
                                    (gem, "API", f"{self.ats.url}/job_board/v0/"),
                                    (teamtailor, "FEED", self.ats.url + "/teamtailor/{slug}/jobs.rss"),
                                    (personio, "FEED", self.ats.url + "/personio/{slug}/xml"),
                                    (smartrecruiters, "API", f"{self.ats.url}/v1/companies/"),
                                    (rippling, "API", f"{self.ats.url}/api/v2/board/")]:
            mock.patch.object(target, name, value).start()
        crawls = ("CC-MAIN-2026-39", "CC-MAIN-2026-34", "CC-MAIN-2026-30")
        self.cc.files.update(crawl_list(*crawls))
        for crawl_id in crawls:
            self.cc.files.update(crawl_files(crawl_id, [cdx_line("https://jobs.ashbyhq.com/acme/job-1"),
                                                        cdx_line("https://job-boards.greenhouse.io/gh-acme"),
                                                        cdx_line("https://job-boards.eu.greenhouse.io/gh-eu"),
                                                        cdx_line("https://apply.workable.com/wk-acme"),
                                                        cdx_line("https://jobs.gem.com/gem-acme"),
                                                        cdx_line("https://tt-acme.teamtailor.com/jobs"),
                                                        cdx_line("https://pe-acme.jobs.personio.de/"),
                                                        cdx_line("https://pe-acme.jobs.personio.com/"),
                                                        cdx_line("https://jobs.smartrecruiters.com/sr-acme/1-ae"),
                                                        cdx_line("https://careers.smartrecruiters.com/sr-acme"),
                                                        cdx_line("https://ats.rippling.com/rp-acme/jobs"),
                                                        cdx_line("https://wd-acme.wd3.myworkdayjobs.com/Careers"),
                                                        cdx_line("https://or-acme.fa.em2.oraclecloud.com/hcmUI/"
                                                                 "CandidateExperience/en/sites/CX"),
                                                        cdx_line("https://www.example.com/")]))
        self.write_config(boards_file=False)
        self.ats.routes["/teamtailor/tt-acme/jobs.rss?per_page=1000"] = (200, teamtailor_feed(
            teamtailor_item("t1", "Engineer")))
        self.ats.routes["/personio/pe-acme/xml"] = (200, personio_feed(personio_position("p1", "Engineer")))
        self.ats.routes["/v1/companies/sr-acme/postings?limit=100&offset=0"] = (200, smartrecruiters_page(
            [smartrecruiters_job("s1", "Engineer")]))
        self.ats.routes["/api/v2/board/rp-acme/jobs?page=0&pageSize=1000"] = (200, rippling_page(
            [rippling_item("r1", "Engineer")]))
        self.ats.routes["/workday/wd-acme.wd3/wday/cxs/wd-acme/careers/jobs"] = (200, workday_site(
            [workday_job("Engineer_R1", "Engineer")], {"locations": ["London"]}))
        self.ats.routes["/oracle/or-acme.em2" + oracle.LIST.format(site="cx", limit=200, offset=0)] = (
            200, oracle_page([oracle_job("o1", "Engineer")]))

    def days_ago(self, days):
        return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="microseconds")

    def discovered(self):
        conn = store.open_db(self.home)
        try:
            return [b.id for b in store.live_discovered(conn)], store.discovered_at(conn)
        finally:
            conn.close()

    def test_stale_list_is_rediscovered_before_fetching(self):
        self.seed_discovery("old", at=self.days_ago(31))
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        self.assertIn("## acme — Enterprise Account Executive", Path(path).read_text())
        boards, at = self.discovered()
        self.assertEqual(boards, DISCOVERED)
        self.assertGreater(at, datetime.fromisoformat(self.days_ago(1)))
        self.assert_logged_in_order("run_start", "discover_done", "prefetch db_open_seconds=", "fetch_start")

    def test_fresh_list_is_not_rediscovered(self):
        self.seed_discovery("acme", at=self.days_ago(29))
        self.assertEqual(self.run_cli()[1][1], "2")
        self.assertEqual(self.cc.requests, [])

    def test_empty_list_is_rediscovered(self):
        conn = store.open_db(self.home)
        with conn:
            conn.execute("DELETE FROM boards")
        conn.close()
        self.assertEqual(self.run_cli()[1][1], "2")
        self.assertEqual(self.discovered()[0], DISCOVERED)

    def test_unreadable_crawl_keeps_the_old_list_and_the_run_goes_on(self):
        stale = self.days_ago(31)
        self.seed_discovery("acme", at=stale)
        self.cc.fail["/cc-index/collections/CC-MAIN-2026-30/indexes/cluster.idx"] = [503] * 4
        code, (_, new, _) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        self.assertEqual(self.discovered(), (["ashby:acme"], datetime.fromisoformat(stale)))
        self.assertIn("discover_failed", (self.home / "runs.log").read_text())

    def test_a_newly_supported_ats_is_discovered_on_the_next_run(self):
        for ats in ("greenhouse", "gem", "teamtailor", "personio", "smartrecruiters", "rippling", "workday", "oracle"):
            self.seed_discovery("acme", at=self.days_ago(1))
            conn = store.open_db(self.home)
            with conn:
                conn.execute("DELETE FROM boards WHERE ats = ?", (ats,))  # as an install from before the ATS
            conn.close()
            self.assertEqual(self.run_cli()[0], 0, ats)
            self.assertEqual(self.discovered()[0], DISCOVERED, ats)
        conn = store.open_db(self.home)
        try:
            boards = {r[0] for r in conn.execute("SELECT DISTINCT board FROM postings")}
        finally:
            conn.close()
        self.assertLessEqual({"teamtailor:tt-acme", "personio:pe-acme", "smartrecruiters:sr-acme",
                              "rippling:rp-acme", "workday:wd-acme.wd3.careers", "oracle:or-acme.em2.cx"},
                             boards)  # the new ATSes' lists were read

    def test_a_list_without_workable_is_not_rediscovered(self):
        self.seed_discovery("acme", at=self.days_ago(1))
        conn = store.open_db(self.home)
        with conn:
            conn.execute("DELETE FROM boards WHERE ats = 'workable'")
        conn.close()
        self.assertEqual(self.run_cli()[1][1], "2")
        self.assertEqual(self.cc.requests, [])

    def test_fresh_install_whose_crawl_fails_has_no_boards(self):
        conn = store.open_db(self.home)
        with conn:
            conn.execute("DELETE FROM boards")
        conn.close()
        self.cc.fail["/cc-index/collections/CC-MAIN-2026-30/indexes/cluster.idx"] = [503] * 4
        self.assertEqual(self.run_cli()[0], 2)
        self.assertIn("discover_failed", (self.home / "runs.log").read_text())

    def test_no_list_and_no_crawl_is_a_usage_error(self):
        conn = store.open_db(self.home)
        with conn:
            conn.execute("DELETE FROM boards")
        conn.close()
        self.cc.files.clear()
        self.assertEqual(self.run_cli()[0], 2)


if __name__ == "__main__":
    unittest.main()
