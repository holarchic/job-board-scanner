"""End to end: `jobscan discover` against fake Common Crawl index files, through the real CLI."""
import contextlib
import fcntl
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobscan import cli, crawl, store
from tests.fakes import FakeFiles, cdx_line, crawl_files, crawl_list

CRAWLS = ("CC-MAIN-2026-39", "CC-MAIN-2026-34", "CC-MAIN-2026-30")


FIRST_LIST = ["ashby:acme", "ashby:beta", "ashby:gamma", "gem:gem-acme", "greenhouse:gh-eu", "greenhouse:gh-us",
              "personio:pe-acme", "rippling:rp-acme", "smartrecruiters:sr-acme", "teamtailor:tt-acme",
              "workday:wd-acme.wd3.careers"]


def captures(*slugs, greenhouse=("gh-us",), greenhouse_eu=("gh-eu",), workable=("wk-acme",), gem=("gem-acme",),
             teamtailor=("tt-acme",), personio_de=("pe-acme",), personio_com=("pe-acme",), sr_jobs=("SR-Acme",),
             sr_careers=("sr-acme",), rippling=("rp-acme",), workday=("wd-acme.wd3/en-US/Careers",)):
    """One crawl's index lines: Ashby boards, Greenhouse boards on both hosts, Gem boards, Teamtailor and Personio
    subdomains, SmartRecruiters boards on both hosts, Rippling boards (a slug may carry a locale or `embed/` prefix),
    Workday sites (`<tenant>.<dc>/<path>`), and noise, including Workable boards, which are never discovered."""
    return [cdx_line(f"https://jobs.ashbyhq.com/{slug}/job-1") for slug in slugs] + \
        [cdx_line(f"https://job-boards.greenhouse.io/{slug}/jobs/1") for slug in greenhouse] + \
        [cdx_line(f"https://job-boards.eu.greenhouse.io/{slug}") for slug in greenhouse_eu] + \
        [cdx_line(f"https://apply.workable.com/{slug}/j/AB12/") for slug in workable] + \
        [cdx_line("https://apply.workable.com/j/AB12") for _ in workable[:1]] + \
        [cdx_line(f"https://jobs.gem.com/{slug}/am9i") for slug in gem] + \
        [cdx_line(f"https://{slug}.teamtailor.com/jobs/1-ae") for slug in teamtailor] + \
        [cdx_line(f"https://{slug}.jobs.personio.de/job/1") for slug in personio_de] + \
        [cdx_line(f"https://{slug}.jobs.personio.com/") for slug in personio_com] + \
        [cdx_line(f"https://jobs.smartrecruiters.com/{slug}/744000155574869-account-executive") for slug in sr_jobs] + \
        [cdx_line(f"https://careers.smartrecruiters.com/{slug}") for slug in sr_careers] + \
        [cdx_line("https://jobs.smartrecruiters.com/"), cdx_line("https://jobs.smartrecruiters.com/?company=Acme")] + \
        [cdx_line(f"https://ats.rippling.com/{slug}/jobs/0f1e-ae") for slug in rippling] + \
        [cdx_line("https://ats.rippling.com/en-GB/jobs"), cdx_line("https://ats.rippling.com/")] + \
        [cdx_line(f"https://{host}.myworkdayjobs.com/{path}/job/London/AE_R1") for host, _, path in
         (site.partition("/") for site in workday)] + \
        [cdx_line("https://wd-acme.wd3.myworkdayjobs.com/"), cdx_line("https://www.myworkdayjobs.com/Careers")] + \
        [cdx_line("https://app.teamtailor.com/login"), cdx_line("https://teamtailor.com/en")] + \
        [cdx_line("https://app.ashbyhq.com/login"), cdx_line("https://www.example.com/")]


class DiscoverTest(unittest.TestCase):
    def setUp(self):
        self.cc = FakeFiles()
        self.addCleanup(self.cc.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)  # no config, profile, boards file or key: discover needs none of them
        for target, name, value in [(crawl, "BASE", self.cc.url), (crawl, "RETRY_PAUSES", (0, 0, 0))]:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, {"JOBSCAN_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("OPENROUTER_API_KEY", None)
        self.serve({CRAWLS[0]: captures("acme", "Beta"), CRAWLS[1]: captures("beta", "gamma"),
                    CRAWLS[2]: captures("acme")})

    def serve(self, by_crawl):
        self.cc.files.clear()
        self.cc.files.update(crawl_list("CC-MAIN-2025-51", *by_crawl))
        for crawl_id, lines in by_crawl.items():
            self.cc.files.update(crawl_files(crawl_id, lines))

    def run_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["discover"])
        return code, out.getvalue().strip()

    def discovered(self):
        conn = store.open_db(self.home)
        try:
            return sorted((b.id, b.api_url) for b in store.live_discovered(conn))
        finally:
            conn.close()

    def test_union_of_three_crawls_case_folded(self):
        self.assertEqual(self.run_cli(), (0, "discovered\t11\t" + ",".join(CRAWLS)))
        self.assertEqual(self.discovered(), [
            ("ashby:acme", "https://api.ashbyhq.com/posting-api/job-board/acme"),
            ("ashby:beta", "https://api.ashbyhq.com/posting-api/job-board/beta"),
            ("ashby:gamma", "https://api.ashbyhq.com/posting-api/job-board/gamma"),
            ("gem:gem-acme", "https://api.gem.com/job_board/v0/gem-acme/job_posts/"),
            ("greenhouse:gh-eu", "https://boards-api.greenhouse.io/v1/boards/gh-eu/jobs?content=true"),
            ("greenhouse:gh-us", "https://boards-api.greenhouse.io/v1/boards/gh-us/jobs?content=true"),
            ("personio:pe-acme", "https://pe-acme.jobs.personio.com/xml?language=en"),
            ("rippling:rp-acme", "https://ats.rippling.com/api/v2/board/rp-acme/jobs"),
            ("smartrecruiters:sr-acme", "https://api.smartrecruiters.com/v1/companies/sr-acme/postings"),
            ("teamtailor:tt-acme", "https://tt-acme.teamtailor.com/jobs.rss?per_page=1000"),
            ("workday:wd-acme.wd3.careers", "https://wd-acme.wd3.myworkdayjobs.com/wday/cxs/wd-acme/careers/jobs"),
        ])

    def test_greenhouse_hosts_union_and_fold(self):
        self.serve({CRAWLS[0]: captures("acme", greenhouse=("Shared", "us-only"), greenhouse_eu=("shared",)),
                    CRAWLS[1]: captures("acme", greenhouse=("ep%2Bco",), greenhouse_eu=("eu-only",)),
                    CRAWLS[2]: captures("acme", greenhouse=("EP+Co",))})
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual([b for b, _ in self.discovered()],
                         ["ashby:acme", "gem:gem-acme", "greenhouse:ep+co", "greenhouse:eu-only", "greenhouse:gh-eu",
                          "greenhouse:shared", "greenhouse:us-only", "personio:pe-acme", "rippling:rp-acme",
                          "smartrecruiters:sr-acme", "teamtailor:tt-acme", "workday:wd-acme.wd3.careers"])

    def test_gem_slugs_keep_their_case_and_workable_is_never_discovered(self):
        self.serve({CRAWLS[0]: captures("acme", workable=("Mixed", "mixed"), gem=("Gem-B", "gem-b")),
                    CRAWLS[1]: captures("acme", workable=("other",)), CRAWLS[2]: captures("acme")})
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual([b for b, _ in self.discovered() if b.startswith("gem")],
                         ["gem:Gem-B", "gem:gem-acme", "gem:gem-b"])
        self.assertNotIn("host=apply.workable.com", (self.home / "runs.log").read_text())

    def test_subdomain_boards_fold_and_a_personio_tenant_on_both_hosts_is_one_board(self):
        self.serve({CRAWLS[0]: captures("acme", teamtailor=("Acme-TT", "acme-tt"), personio_de=("Both", "de-only")),
                    CRAWLS[1]: captures("acme", personio_com=("both", "com-only")), CRAWLS[2]: captures("acme")})
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual([b for b, _ in self.discovered() if b.startswith(("personio", "teamtailor"))],
                         ["personio:both", "personio:com-only", "personio:de-only", "personio:pe-acme",
                          "teamtailor:acme-tt", "teamtailor:tt-acme"])

    def test_smartrecruiters_boards_on_both_hosts_fold_and_ni_links_name_their_board(self):
        self.serve({CRAWLS[0]: captures("acme", sr_jobs=("ni/Nexthink", "Both"), sr_careers=("both", "careers-only")),
                    CRAWLS[1]: captures("acme"), CRAWLS[2]: captures("acme")})
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual([b for b, _ in self.discovered() if b.startswith("smartrecruiters")],
                         ["smartrecruiters:both", "smartrecruiters:careers-only", "smartrecruiters:nexthink",
                          "smartrecruiters:sr-acme"])

    def test_workday_sites_fold_past_a_locale_and_keep_their_data_centre(self):
        self.serve({CRAWLS[0]: captures("acme", workday=("wd-acme.wd3/en-US/Careers", "WD-Acme.wd3/careers")),
                    CRAWLS[1]: captures("acme", workday=("wd-acme.wd5/Careers", "wm.wd12/WM")),
                    CRAWLS[2]: captures("acme")})
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual([b for b in self.discovered() if b[0].startswith("workday")], [
            ("workday:wd-acme.wd3.careers", "https://wd-acme.wd3.myworkdayjobs.com/wday/cxs/wd-acme/careers/jobs"),
            ("workday:wd-acme.wd5.careers", "https://wd-acme.wd5.myworkdayjobs.com/wday/cxs/wd-acme/careers/jobs"),
            ("workday:wm.wd12.wm", "https://wm.wd12.myworkdayjobs.com/wday/cxs/wm/wm/jobs")])

    def test_rippling_slugs_keep_their_case_past_a_locale_or_embed_prefix(self):
        self.serve({CRAWLS[0]: captures("acme", rippling=("en-GB/Mixed-Case", "embed/mixed-case")),
                    CRAWLS[1]: captures("acme", rippling=("es-419/other",)), CRAWLS[2]: captures("acme")})
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual([b for b, _ in self.discovered() if b.startswith("rippling")],
                         ["rippling:Mixed-Case", "rippling:mixed-case", "rippling:other", "rippling:rp-acme"])

    def test_a_crawl_without_careers_smartrecruiters_captures_keeps_the_whole_list(self):
        self.assertEqual(self.run_cli()[0], 0)
        self.serve({c: captures("delta", **({"sr_careers": ()} if c == CRAWLS[0] else {})) for c in CRAWLS})
        self.assertEqual(self.run_cli(), (1, ""))
        self.assertEqual([b for b, _ in self.discovered()], FIRST_LIST)

    def test_a_crawl_without_one_personio_host_keeps_the_whole_list(self):
        self.assertEqual(self.run_cli()[0], 0)
        self.serve({c: captures("delta", **({"personio_com": ()} if c == CRAWLS[2] else {})) for c in CRAWLS})
        self.assertEqual(self.run_cli(), (1, ""))
        self.assertEqual([b for b, _ in self.discovered()], FIRST_LIST)

    def test_a_crawl_without_gem_captures_keeps_the_list_one_without_workable_does_not(self):
        self.assertEqual(self.run_cli()[0], 0)
        self.serve({c: captures("delta", **({"gem": ()} if c == CRAWLS[1] else {})) for c in CRAWLS})
        self.assertEqual(self.run_cli(), (1, ""))
        self.assertEqual([b for b, _ in self.discovered()], FIRST_LIST)
        self.serve({c: captures("delta", workable=()) for c in CRAWLS})
        self.assertEqual(self.run_cli()[0], 0)

    def test_a_failing_greenhouse_host_keeps_the_whole_list(self):
        self.assertEqual(self.run_cli()[0], 0)
        conn = store.open_db(self.home)
        with conn:
            conn.execute("UPDATE boards SET not_found = 2 WHERE id = 'ashby:beta'")
        conn.close()
        before = self.rows()
        self.serve({c: captures("delta", greenhouse=("new",), greenhouse_eu=()) for c in CRAWLS})
        self.assertEqual(self.run_cli(), (1, ""))
        self.assertEqual(self.rows(), before)  # ids, URLs, 404 counts and timestamps alike

    def rows(self):
        conn = store.open_db(self.home)
        try:
            return [tuple(r) for r in conn.execute("SELECT * FROM boards ORDER BY id")]
        finally:
            conn.close()

    def test_unreadable_crawl_keeps_the_previous_list(self):
        self.assertEqual(self.run_cli()[0], 0)
        self.serve({CRAWLS[0]: captures("delta"), CRAWLS[1]: captures("delta"), CRAWLS[2]: captures("delta")})
        self.cc.fail[f"/cc-index/collections/{CRAWLS[2]}/indexes/cluster.idx"] = [503] * 4
        self.assertEqual(self.run_cli(), (1, ""))
        self.assertEqual([b for b, _ in self.discovered()], FIRST_LIST)
        self.assertIn("discover_failed", (self.home / "runs.log").read_text())

    def test_unwritable_database_is_a_usage_error_and_keeps_the_list(self):
        self.assertEqual(self.run_cli()[0], 0)
        self.serve({CRAWLS[0]: captures("delta"), CRAWLS[1]: captures("delta"), CRAWLS[2]: captures("delta")})
        database = self.home / "jobscan.sqlite"
        database.chmod(0o444)
        self.addCleanup(database.chmod, 0o644)
        self.assertEqual(self.run_cli(), (2, ""))
        database.chmod(0o644)
        self.assertEqual([b for b, _ in self.discovered()], FIRST_LIST)

    def test_refused_while_a_run_holds_the_lock(self):
        with (self.home / ".lock").open("w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.run_cli(), (2, ""))
        self.assertEqual(self.discovered(), [])

    def test_missing_home_is_a_usage_error(self):
        with mock.patch.dict(os.environ, {"JOBSCAN_HOME": str(self.home / "nowhere")}):
            self.assertEqual(self.run_cli(), (2, ""))


if __name__ == "__main__":
    unittest.main()
