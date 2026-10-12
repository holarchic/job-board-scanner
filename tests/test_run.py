"""End to end: `jobscan run` against a fake Ashby and a fake model, through the real CLI."""
import contextlib
import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from jobscan import ashby, cli, crawl, digest, fetch, greenhouse, notify, scan, store, tag
from jobscan.records import Board
from tests.fakes import (
    FakeFiles,
    FakeServer,
    ashby_job,
    cdx_line,
    crawl_files,
    crawl_list,
    greenhouse_job,
    model_reply,
    tags_quoting_the_posting,
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
        (self.home / "boards.json").write_text(json.dumps({"companies": [
            {"company": "Acme", "board_url": f"{self.ats.url}/posting-api/job-board/acme"},
            {"company": "Broken", "board_url": f"{self.ats.url}/posting-api/job-board/broken"},
        ]}))
        self.write_config(boards_file=True)
        self.ats.routes["/posting-api/job-board/broken"] = (500, {"error": "boom"})
        self.jobs = [ashby_job("1", "Account Executive"), ashby_job("2", "Enterprise Account Executive"),
                     ashby_job("3", "Account Executive", location="Berlin"), ashby_job("4", "Engineer")]
        self.ats.routes["/posting-api/job-board/acme"] = (200, lambda _: (200, {"jobs": self.jobs}))
        self.model_ok()
        for target, name, value in [(fetch, "RETRY_PAUSES", (0, 0)), (tag, "PAUSE", 0),
                                    (tag, "RATE_LIMIT_PAUSES", (0, 0, 0, 0))]:
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
        (self.home / "config.toml").write_text(('boards_file = "boards.json"\n' if boards_file else "")
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
            'boards_file = "boards.json"\n[model]\nbase_url = "https://openrouter.ai/api/v1"\nslug = "x/y"\n')
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            code, _ = self.run_cli()
        self.assertEqual(code, 2)
        self.assertEqual(self.ats.requests, [])


class GreenhouseRunTest(RunFixture):
    def test_ashby_and_greenhouse_boards_in_one_digest(self):
        (self.home / "boards.json").write_text(json.dumps({"companies": [
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
        (self.home / "boards.json").write_text(json.dumps({"companies": [
            {"company": "Acme", "board_url": f"{self.ats.url}/posting-api/job-board/acme"},
            {"company": "Globex", "board_url": f"{self.ats.url}/v1/boards/globex/jobs"},
        ]}))
        self.ats.routes["/v1/boards/globex/jobs?content=true"] = (200, {"jobs": [
            greenhouse_job("a\ud800", "Account Executive")]})
        code, (_, new, _) = self.run_cli()
        self.assertEqual((code, new), (0, "2"))
        self.assertIn("board_failed company='Globex' board=greenhouse:globex", (self.home / "runs.log").read_text())


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
                mock.patch.object(scan, "scan", side_effect=RuntimeError("boom")):
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


class BoardSourcesTest(RunFixture):
    """Boards come from the boards file and from discovery; a board is one board whichever source names it."""

    def discover(self, *slugs):
        self.seed_discovery(*slugs)

    def list_boards(self, *slugs):
        (self.home / "boards.json").write_text(json.dumps({"companies": [
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


DISCOVERED = ["ashby:acme", "greenhouse:gh-acme", "greenhouse:gh-eu"]


class DiscoveryRefreshTest(RunFixture):
    """`run` rediscovers boards itself when the list is empty or 30 days old, and carries on if it can't."""

    def setUp(self):
        super().setUp()
        self.tripwire.stop()
        self.cc = FakeFiles()
        self.addCleanup(self.cc.close)
        for target, name, value in [(crawl, "BASE", self.cc.url), (crawl, "RETRY_PAUSES", (0, 0, 0)),
                                    (ashby, "API", f"{self.ats.url}/posting-api/job-board/"),
                                    (greenhouse, "API", f"{self.ats.url}/v1/boards/")]:
            mock.patch.object(target, name, value).start()
        crawls = ("CC-MAIN-2026-39", "CC-MAIN-2026-34", "CC-MAIN-2026-30")
        self.cc.files.update(crawl_list(*crawls))
        for crawl_id in crawls:
            self.cc.files.update(crawl_files(crawl_id, [cdx_line("https://jobs.ashbyhq.com/acme/job-1"),
                                                        cdx_line("https://job-boards.greenhouse.io/gh-acme"),
                                                        cdx_line("https://job-boards.eu.greenhouse.io/gh-eu"),
                                                        cdx_line("https://www.example.com/")]))
        self.write_config(boards_file=False)

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
        self.seed_discovery("acme", at=self.days_ago(1))
        conn = store.open_db(self.home)
        with conn:
            conn.execute("DELETE FROM boards WHERE ats = 'greenhouse'")  # as an install from before Greenhouse
        conn.close()
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(self.discovered()[0], DISCOVERED)

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
