import gzip
import json
import unittest
from unittest import mock

from jobscan import crawl
from jobscan.crawl import CrawlError
from tests.fakes import FakeFiles, cdx_line, crawl_files, crawl_list

CRAWL = "CC-MAIN-2026-39"
INDEXES = f"/cc-index/collections/{CRAWL}/indexes/"


def ashby_urls(n):
    return [f"https://jobs.ashbyhq.com/acme{i:03d}/job-{i}" for i in range(n)]


def lines_around(targets, before, after):
    """Captures of other hosts sorting before and after jobs.ashbyhq.com, around the target captures."""
    return ([cdx_line(f"https://app.ashbyhq.com/p{i:04d}") for i in range(before)]
            + [cdx_line(u) for u in targets]
            + [cdx_line(f"https://www.example.com/p{i:04d}") for i in range(after)])


class CapturesTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeFiles()
        self.addCleanup(self.server.close)
        pauses = mock.patch.object(crawl, "RETRY_PAUSES", (0, 0, 0))
        pauses.start()
        self.addCleanup(pauses.stop)

    def serve(self, lines, per_block=2):
        self.server.files.update(crawl_files(CRAWL, lines, per_block))

    def captures(self):
        return crawl.captures(CRAWL, "jobs.ashbyhq.com", base=self.server.url)

    def test_finds_every_capture_wherever_the_host_starts_and_ends(self):
        targets = ashby_urls(7)
        for window in (crawl.WINDOW, 160):
            for before in (0, 1, 2, 3, 40):
                for after in (0, 1, 40):
                    with self.subTest(window=window, before=before, after=after), \
                            mock.patch.object(crawl, "WINDOW", window):
                        self.server.files.clear()
                        self.serve(lines_around(targets, before, after), per_block=3)
                        self.assertEqual(sorted(self.captures()), targets)

    def test_captures_inside_the_last_block_that_starts_before_the_host(self):
        self.serve(lines_around(ashby_urls(1), 2, 0), per_block=3)
        self.assertEqual(self.captures(), ashby_urls(1))

    def test_binary_search_reads_a_fraction_of_a_large_index(self):
        self.serve(lines_around(ashby_urls(3), 3000, 3000), per_block=1)
        with mock.patch.object(crawl, "WINDOW", 512):
            self.assertEqual(len(self.captures()), 3)
        index_reads = [r for r in self.server.requests if r[1].endswith("cluster.idx") and r[0] == "GET"]
        index_size = len(self.server.files[INDEXES + "cluster.idx"])
        self.assertLess(len(index_reads) * 512, index_size / 10)

    def test_keeps_only_status_200_html(self):
        self.serve([cdx_line("https://jobs.ashbyhq.com/a/1"),
                    cdx_line("https://jobs.ashbyhq.com/b/1", status="404"),
                    cdx_line("https://jobs.ashbyhq.com/c/1", status="301"),
                    cdx_line("https://jobs.ashbyhq.com/d/robots.txt", mime="text/plain"),
                    cdx_line("https://app.ashbyhq.com/x")])
        self.assertEqual(self.captures(), ["https://jobs.ashbyhq.com/a/1"])

    def test_wildcard_host_reads_every_subdomain_and_nothing_else(self):
        inside = ["https://acme.teamtailor.com/jobs", "https://beta.teamtailor.com/jobs/1-ae",
                  "https://x.y.teamtailor.com/"]
        self.serve([cdx_line(u) for u in inside] + [cdx_line("https://teamtailor.com/en"),
                                                     cdx_line("https://teamtailorx.com/a"),
                                                     cdx_line("https://www.teamtailor.co/b")])
        self.assertEqual(sorted(crawl.captures(CRAWL, "*.teamtailor.com", base=self.server.url)), sorted(inside))
        self.assertEqual(crawl.surt_prefix("*.Jobs.Personio.de"), "de,personio,jobs,")

    def test_no_captures_is_an_error(self):
        self.serve([cdx_line("https://app.ashbyhq.com/x"), cdx_line("https://jobs.ashbyhq.com/a/1", status="404")])
        with self.assertRaisesRegex(CrawlError, "no captures"):
            self.captures()

    def test_retries_then_reads(self):
        self.serve(lines_around(ashby_urls(2), 2, 2))
        self.server.fail[INDEXES + "cluster.idx"] = [503, 429]
        self.assertEqual(len(self.captures()), 2)

    def test_persistent_failure_is_an_error(self):
        self.serve(lines_around(ashby_urls(2), 2, 2))
        self.server.fail[INDEXES + "cdx-00000.gz"] = [503] * 4
        with self.assertRaisesRegex(CrawlError, "HTTP 503"):
            self.captures()

    def test_a_range_cut_short_is_asked_again(self):
        self.serve(lines_around(ashby_urls(2), 2, 2))
        self.server.cut[INDEXES + "cdx-00000.gz"] = [10]
        self.assertEqual(len(self.captures()), 2)
        ranges = [r for r in self.server.requests if r[1].endswith("cdx-00000.gz") and r[0] == "GET"]
        self.assertEqual(ranges[0][2], ranges[1][2])  # the same range, asked twice

    def test_a_range_cut_short_every_time_is_an_error(self):
        self.serve(lines_around(ashby_urls(2), 2, 2))
        self.server.cut[INDEXES + "cdx-00000.gz"] = [10] * 50
        with self.assertRaisesRegex(CrawlError, r"cdx-00000\.gz: range \d+-\d+ answered 206 with 10 bytes"):
            self.captures()
        tries = [r for r in self.server.requests if r[1].endswith("cdx-00000.gz") and r[0] == "GET"]
        self.assertEqual(len(tries), 1 + len(crawl.RETRY_PAUSES))

    def test_server_ignoring_range_is_an_error(self):
        self.serve(lines_around(ashby_urls(2), 2, 2))
        self.server.ignore_range = True
        with self.assertRaisesRegex(CrawlError, "answered 200"):
            self.captures()

    def test_bad_content_range_is_an_error(self):
        self.serve(lines_around(ashby_urls(2), 2, 2))
        for name, header in [("total not past end", lambda s, e, n: f"bytes {s}-{e}/{e}"),
                             ("huge number", lambda s, e, n: f"bytes {s}-{'9' * 5000}/{n}"),
                             ("unsatisfied form", lambda s, e, n: f"bytes */{n}")]:
            with self.subTest(name):
                self.server.content_range = header
                with self.assertRaisesRegex(CrawlError, "range"):
                    self.captures()

    def test_block_past_end_of_file_is_an_error(self):
        self.serve(lines_around(ashby_urls(2), 0, 0))
        cdx = self.server.files[INDEXES + "cdx-00000.gz"]
        self.server.files[INDEXES + "cdx-00000.gz"] = cdx[:-5]
        with self.assertRaisesRegex(CrawlError, "range"):
            self.captures()

    def test_corrupt_block_is_an_error(self):
        self.serve(lines_around(ashby_urls(2), 0, 0), per_block=10)
        cdx = self.server.files[INDEXES + "cdx-00000.gz"]
        self.server.files[INDEXES + "cdx-00000.gz"] = cdx[:20] + b"\x00" * (len(cdx) - 20)
        with self.assertRaisesRegex(CrawlError, "corrupt block"):
            self.captures()

    def test_malformed_index_line_is_an_error(self):
        self.serve(lines_around(ashby_urls(4), 2, 2), per_block=1)
        index = self.server.files[INDEXES + "cluster.idx"].decode().splitlines(keepends=True)
        inside = next(i for i, line in enumerate(index) if line.startswith("com,ashbyhq,jobs)/acme002"))
        index[inside] = index[inside].replace("\tcdx-00000.gz\t", "\tcdx-00000.gz\tx", 1)
        self.server.files[INDEXES + "cluster.idx"] = "".join(index).encode()
        with self.assertRaisesRegex(CrawlError, "malformed cluster.idx"):
            self.captures()

    def test_index_line_without_a_timestamp_is_an_error(self):
        self.serve(lines_around(ashby_urls(2), 0, 0), per_block=1)
        index = self.server.files[INDEXES + "cluster.idx"].decode()
        self.server.files[INDEXES + "cluster.idx"] = index.replace(" 20260901000000\t", "\t", 1).encode()
        with self.assertRaisesRegex(CrawlError, "malformed cluster.idx"):
            self.captures()

    def test_non_ascii_index_line_is_an_error(self):
        self.serve(lines_around(ashby_urls(2), 0, 0), per_block=1)
        index = self.server.files[INDEXES + "cluster.idx"]
        self.server.files[INDEXES + "cluster.idx"] = index.replace(b"acme000", b"acme\xff00", 1)
        with self.assertRaisesRegex(CrawlError, "malformed cluster.idx"):
            self.captures()

    def test_cdx_line_with_a_bad_timestamp_or_a_surrogate_url_is_an_error(self):
        for line in [b'com,ashbyhq,jobs)/a/1 2026-09 {"url": "https://jobs.ashbyhq.com/a/1", "status": "200", '
                     b'"mime": "text/html"}',
                     b'com,ashbyhq,jobs)/a/1 20260901000000 {"url": "https://jobs.ashbyhq.com/\\ud800", '
                     b'"status": "200", "mime": "text/html"}']:
            with self.subTest(line=line[:40]):
                block = gzip.compress(line + b"\n")
                self.server.files[INDEXES + "cdx-00000.gz"] = block
                self.server.files[INDEXES + "cluster.idx"] = \
                    f"com,ashbyhq,jobs)/a/1 20260901000000\tcdx-00000.gz\t0\t{len(block)}\t1\n".encode()
                with self.assertRaisesRegex(CrawlError, "malformed CDX"):
                    self.captures()

    def test_malformed_cdx_record_is_an_error(self):
        block = gzip.compress(b"com,ashbyhq,jobs)/a/1 20260901000000 {not json\n")
        self.server.files[INDEXES + "cdx-00000.gz"] = block
        self.server.files[INDEXES + "cluster.idx"] = \
            f"com,ashbyhq,jobs)/a/1 20260901000000\tcdx-00000.gz\t0\t{len(block)}\t1\n".encode()
        with self.assertRaisesRegex(CrawlError, "malformed CDX"):
            self.captures()


    def test_record_with_a_non_string_field_is_an_error(self):
        for field, value in [("status", 200), ("mime", None), ("url", ["x"])]:
            with self.subTest(field=field):
                good = cdx_line("https://jobs.ashbyhq.com/a/1")
                bad = cdx_line("https://jobs.ashbyhq.com/b/1")
                record = json.loads(bad.split(" ", 2)[2])
                record[field] = value
                bad = bad.rsplit(" {", 1)[0] + " " + json.dumps(record)
                self.server.files.clear()
                self.serve([good, bad])
                with self.assertRaisesRegex(CrawlError, "string url, status and mime"):
                    self.captures()


class LatestCrawlsTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeFiles()
        self.addCleanup(self.server.close)

    def test_newest_distinct_crawls(self):
        self.server.files.update(crawl_list("CC-MAIN-2026-30", "CC-MAIN-2026-39", "CC-MAIN-2025-51",
                                            "CC-MAIN-2026-34", "CC-MAIN-2026-39"))
        self.assertEqual(crawl.latest_crawls(3, base=self.server.url),
                         ["CC-MAIN-2026-39", "CC-MAIN-2026-34", "CC-MAIN-2026-30"])

    def test_undecodable_crawl_list_is_an_error(self):
        self.server.files.update(crawl_list("CC-MAIN-2026-39", "CC-MAIN-2026-34", "CC-MAIN-2026-30", "CC-MAIN-2026-25"))
        page = "/cc-index/collections/index.html"
        self.server.files[page] = self.server.files[page].replace(b"2026-39", b"2026\xff39")
        with self.assertRaisesRegex(CrawlError, "not valid UTF-8"):
            crawl.latest_crawls(3, base=self.server.url)

    def test_too_few_crawls_is_an_error(self):
        self.server.files.update(crawl_list("CC-MAIN-2026-39", "CC-MAIN-2026-34"))
        with self.assertRaisesRegex(CrawlError, "need 3"):
            crawl.latest_crawls(3, base=self.server.url)


class SurtTest(unittest.TestCase):
    def test_host_prefix(self):
        self.assertEqual(crawl.surt_prefix("jobs.AshbyHQ.com"), "com,ashbyhq,jobs)/")
