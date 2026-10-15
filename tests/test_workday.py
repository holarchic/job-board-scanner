import json
import unittest
from unittest import mock

from jobscan import fetch, workday
from jobscan.records import BoardError
from tests.fakes import workday_detail, workday_job, workday_site

URL = "https://acme.wd3.myworkdayjobs.com/en-US/Careers"
BOARD = workday.page_board("Acme", URL)


def uk(text):
    return text in {"London", "United Kingdom", "Remote"}


class BoardTest(unittest.TestCase):
    def test_site_pages_name_one_board_whatever_the_locale_or_case(self):
        for url in [URL, "https://acme.wd3.myworkdayjobs.com/Careers", "https://ACME.WD3.myworkdayjobs.com/careers",
                    "https://acme.wd3.myworkdayjobs.com/en-us/Careers/job/London/AE_R1",
                    "https://acme.wd3.myworkdayjobs.com/ko-kr/Careers/details/x", "http://acme.wd3.myworkdayjobs.com/es/CAREERS",
                    "https://acme.wd3.myworkdayjobs.com/es-419/Careers"]:
            board = workday.page_board("", url)
            self.assertEqual((board.slug, board.api_url), (
                "acme.wd3.careers", "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/careers/jobs"), url)

    def test_a_site_named_like_a_locale_is_the_site(self):
        for url in ["https://wm.wd12.myworkdayjobs.com/WM", "https://wm.wd12.myworkdayjobs.com/WM/job/x/Clerk_R1"]:
            self.assertEqual(workday.page_board("", url).slug, "wm.wd12.wm", url)

    def test_data_centres_are_separate_boards(self):
        self.assertNotEqual(workday.page_board("", "https://acme.wd5.myworkdayjobs.com/Careers").id, BOARD.id)

    def test_non_site_urls_name_nothing(self):
        for url in ["https://acme.wd3.myworkdayjobs.com/", "https://acme.wd3.myworkdayjobs.com/en-US",
                    "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/Careers/jobs",
                    "https://acme.wd3.myworkdayjobs.com/en-US/job/x", "https://acme.myworkdayjobs.com/Careers",
                    "https://acme.web.myworkdayjobs.com/Careers", "https://wd3.myworkdaysite.com/recruiting/acme/x",
                    "https://acme.wd3.myworkdayjobs.com.evil.example/Careers", "ftp://acme.wd3.myworkdayjobs.com/Careers"]:
            self.assertIsNone(workday.page_board("", url), url)

    def test_fetch_routes_site_pages_here_and_the_company_is_the_tenant(self):
        self.assertEqual(fetch.board_for("", URL).ats, "workday")
        self.assertEqual(BOARD.company, "Acme")
        self.assertEqual(workday.company(BOARD, lambda url, body=None: self.fail("asked")).name, "acme")


class ReadTest(unittest.TestCase):
    def read(self, answer, places=uk):
        self.asked = []

        def get(url, body=None):
            self.assertEqual(url, BOARD.api_url)
            self.asked.append(json.loads(body))
            return json.dumps(answer(json.loads(body))[1]).encode()
        return {p.key.rsplit(":", 1)[1]: p for p in workday.read(BOARD, get, places)}

    def filters_asked(self):
        return [body["appliedFacets"] for body in self.asked]

    def test_only_the_matching_place_values_are_queried_and_each_job_carries_its_matches(self):
        jobs = [workday_job("AE_R1", "Account Executive", ["London", "United Kingdom"], text="London, Greater London"),
                workday_job("AE_R2", "Account Executive", ["Paris", "France"]),
                workday_job("SE_R3", "Engineer", ["London", "Paris", "United Kingdom", "France"]),
                {"bulletFields": ["R4"], "_places": ("London", "United Kingdom")}]
        site = workday_site(jobs, {"locationCountry": ["France", "United Kingdom"],
                                   "locations": ["Leeds", "London", "Paris"],
                                   "jobFamilyGroup": ["Sales"]}, grouped=True)
        postings = self.read(site)
        self.assertEqual(self.filters_asked(), [{}, {"locationCountry": ["id-United Kingdom"]},
                                                {"locations": ["id-London"]}])
        self.assertEqual(sorted(postings), ["R1", "R3"])  # keyed by requisition
        self.assertEqual(postings["R1"].location, "London, Greater London / United Kingdom / London")
        self.assertEqual(postings["R3"].location, "United Kingdom / London")  # `4 Locations` placed
        self.assertEqual(postings["R1"].key, "workday:acme.wd3.careers:R1")
        self.assertEqual(postings["R1"].url, "https://acme.wd3.myworkdayjobs.com/careers/job/Somewhere/AE_R1")
        self.assertEqual((postings["R1"].company, postings["R1"].description), ("Acme", ""))

    def test_a_matching_remote_work_value_is_queried_too_on_a_site_with_places(self):
        jobs = [workday_job("R1", "AE", ["Austin", "Remote"], text="Austin"), workday_job("R2", "AE", ["Paris"])]
        postings = self.read(workday_site(jobs, {"locations": ["Austin", "Paris"], "remoteType": ["Hybrid", "Remote"]}))
        self.assertEqual(self.filters_asked(), [{}, {"remoteType": ["id-Remote"]}])
        self.assertEqual({k: p.location for k, p in postings.items()}, {"job/R1": "Austin / Remote"})

    def test_no_value_matching_reads_as_an_empty_board_after_one_request(self):
        site = workday_site([workday_job("R1", "AE", ["Paris"])], {"locations": ["Paris"]})
        self.assertEqual(self.read(site), {})
        self.assertEqual(len(self.asked), 1)

    def test_a_site_without_a_geographic_filter_is_read_whole_on_its_own_location_text(self):
        jobs = [workday_job(f"R{n}", "AE", ["Anywhere"], text="London" if n % 2 else "Paris") for n in range(25)]
        postings = self.read(workday_site(jobs, {"remoteType": ["Remote"], "timeType": ["Full time"]}))
        self.assertEqual(len(postings), 25)
        self.assertEqual([(b["offset"], b["appliedFacets"]) for b in self.asked], [(0, {}), (20, {})])
        self.assertEqual(postings["job/R1"].location, "London")

    def test_a_capped_total_is_read_past_to_a_short_page(self):
        jobs = [workday_job(f"R{n}", "AE", ["X"]) for n in range(2005)]
        self.assertEqual(len(self.read(workday_site(jobs, {}, total=2000))), 2005)
        self.assertEqual(self.asked[-1]["offset"], 2000)

    def test_a_capped_total_with_fewer_rows_fails_the_board(self):
        jobs = [workday_job(f"R{n}", "AE", ["X"]) for n in range(1990)]
        with self.assertRaisesRegex(BoardError, "don't match the total"):
            self.read(workday_site(jobs, {}, total=2000))

    def test_a_total_over_the_cap_is_read_exactly(self):
        jobs = [workday_job(f"R{n}", "AE", ["X"]) for n in range(2050)]
        self.assertEqual(len(self.read(workday_site(jobs, {}))), 2050)
        self.assertEqual(len(self.asked), 103)

    def test_placeholders_count_towards_the_total(self):
        jobs = [{"bulletFields": ["R0"]}] + [workday_job(f"R{n}", "AE", ["X"]) for n in range(1, 21)]
        self.assertEqual(len(self.read(workday_site(jobs, {}))), 20)
        self.assertEqual(len(self.asked), 2)

    def test_a_titled_row_without_a_job_path_fails_the_board(self):
        for row in [{"title": "AE", "locationsText": "London"}, {"title": "AE", "externalPath": "/job/x/"},
                    {"title": "AE", "externalPath": "/about"}, {"title": 7, "externalPath": "/job/x/R1"}]:
            with self.assertRaises(BoardError, msg=row):
                self.read(workday_site([row], {}))

    def test_a_malformed_link_fails_the_board_through_fetch(self):
        for path in [[], {}]:
            site = workday_site([{"title": "AE", "externalPath": path, "locationsText": "London"}], {})
            with mock.patch.object(fetch, "_get", lambda board, url, body=None, site=site: json.dumps(
                    site(json.loads(body))[1]).encode()):
                (outcome,) = fetch.fetch_each([BOARD], uk)
            self.assertIsInstance(outcome, fetch.BoardFailure, path)

    def test_an_exact_total_beyond_the_capped_reads_limit_is_read_in_full(self):
        jobs = [workday_job(f"R{n}", "AE", ["X"]) for n in range(workday.MAX_ROWS + 1)]
        self.assertEqual(len(self.read(workday_site(jobs, {}))), workday.MAX_ROWS + 1)

    def test_one_job_under_two_values_must_keep_its_title(self):
        site = workday_site([workday_job("R1", "AE", ["London"])], {"locations": ["London", "United Kingdom"]})

        def retitled(body):
            status, page = site(body)
            if body["appliedFacets"] == {"locations": ["id-United Kingdom"]}:
                page = page | {"total": 1, "jobPostings": [page["jobPostings"][0] | {"title": "Director"}]
                               if page["jobPostings"] else [workday_job("R1", "Director")]}
            return status, page
        with self.assertRaisesRegex(BoardError, "disagree on the title"):
            self.read(retitled)

    def test_a_capped_list_that_never_ends_a_page_short_fails_the_board(self):
        def endless(body):
            return 200, {"total": 2000 if body["offset"] == 0 else 0, "jobPostings": [{"bulletFields": ["x"]}] * 20}
        with self.assertRaisesRegex(BoardError, "doesn't end"):
            self.read(endless)
        self.assertEqual(len(self.asked), workday.MAX_ROWS // 20)

    def test_a_list_that_changes_while_paging_fails_the_board(self):
        jobs = [workday_job(f"R{n}", "AE", ["X"]) for n in range(30)]
        site = workday_site(jobs, {})

        def repeated(body):  # the second page repeats a job of the first
            status, page = site(body)
            return status, page | {"jobPostings": page["jobPostings"][:9] + page["jobPostings"][:1]} if body[
                "offset"] else page

        def emptied(body):
            status, page = site(body)
            return status, page | {"jobPostings": []} if body["offset"] else page

        grown = workday_site([workday_job(f"R{n}", "AE", ["X"]) for n in range(35)], {}, total=30)

        for answer, reason in [(repeated, "twice"), (emptied, "empty"), (grown, "don't match")]:
            with self.assertRaisesRegex(BoardError, reason):
                self.read(answer)

    def test_a_malformed_place_value_fails_the_board_and_other_filters_are_ignored(self):
        def answer(facets):
            return lambda body: (200, {"total": 0, "jobPostings": [], "facets": facets})
        with self.assertRaisesRegex(BoardError, "locations"):
            self.read(answer([{"facetParameter": "locations", "values": [{"id": "1"}]}]))
        with self.assertRaisesRegex(BoardError, "locationCountry"):
            self.read(answer([{"facetParameter": "locationCountry", "values": "UK"}]))
        for value in [{"id": "1", "descriptor": "London\ud800"}, {"id": "1", "descriptor": " "},
                      {"id": "", "descriptor": "London"}, {"id": "1", "descriptor": "Lon\ndon"}]:
            with self.assertRaisesRegex(BoardError, "usable", msg=value):
                self.read(answer([{"facetParameter": "locations", "values": [value]}]))
        for group in [{"facetParameter": None, "values": [{"id": "1", "descriptor": "London"}]},
                      {"facetParameter": "", "values": [{"id": "1", "descriptor": "London"}]},
                      {"facetParameter": "locationCountry", "values": None}]:  # a broken group under a place filter
            with self.assertRaises(BoardError, msg=group):
                self.read(answer([{"facetParameter": "locationMainGroup", "values": [group]}]))
        self.assertEqual(self.read(answer([{"facetParameter": "jobFamily", "values": [{"id": 1}]}, "junk"])), {})
        grouped = [{"facetParameter": "locationMainGroup", "values": [
            {"facetParameter": "jobFamilyGroup", "values": [{"id": 1}, {"id": "s", "descriptor": "London Sales"}]}]}]
        self.assertEqual(self.read(answer(grouped)), {})  # a non-place filter in a place group is ignored
        self.assertEqual(len(self.asked), 1)  # and read whole: no place filter, so no value queried

    def test_a_malformed_page_fails_the_board(self):
        for page in [[], {"total": "3", "jobPostings": []}, {"total": 0, "jobPostings": {}},
                     {"total": 0, "jobPostings": [], "facets": {}},
                     {"total": 21, "jobPostings": [workday_job(f"R{n}", "AE") for n in range(21)]}]:
            with self.assertRaises(BoardError, msg=page):
                self.read(lambda body, page=page: (200, page))


class RequisitionTest(unittest.TestCase):
    """A Workday job's key is its requisition, read from `bulletFields`, so a company's sites share it."""

    def keys(self, *rows):
        site = workday_site(list(rows), {})
        get = lambda url, body=None: json.dumps(site(json.loads(body))[1]).encode()  # noqa: E731
        return [p.key.rsplit(":", 1)[1] for p in workday.read(BOARD, get, uk)]

    def row(self, link, fields):
        return {"title": "AE", "externalPath": f"/job/London/{link}", "locationsText": "London", "bulletFields": fields}

    def test_the_requisition_is_the_field_the_link_ends_with(self):
        cases = [("Account-Executive_JR350367", ["JR350367"], "JR350367"),
                 ("Account-Executive_JR350367-2", ["JR350367"], "JR350367"),
                 ("Senior-Account-Executive_R26_0000003039", ["R26_0000003039"], "R26_0000003039"),
                 ("Altra--Sales-Lead_R-20260930-0003", ["R-20260930-0003"], "R-20260930-0003"),
                 ("Analyst_R-20260903-0024-4", ["R-20260903-0024"], "R-20260903-0024"),
                 ("Research-Assistant_JR0000017782", ["Fixed Term", "JR0000017782"], "JR0000017782"),
                 ("Lead_A_B12", ["B12", "A_B12"], "A_B12"),  # the longest field the link ends with
                 ("Community-Support_R-26263", ["R - 26263"], "job/Community-Support_R-26263"),
                 ("Clerk_R1", None, "job/Clerk_R1"), ("Clerk_R1", [7, ""], "job/Clerk_R1"),
                 ("Clerk_R12", ["R1"], "job/Clerk_R12"), ("Clerk_XR1", ["R1"], "job/Clerk_XR1")]
        for link, fields, key in cases:
            self.assertEqual(self.keys(self.row(link, fields)), [key], link)

    def test_two_links_of_one_requisition_on_a_site_are_one_posting(self):
        site = workday_site([workday_job("AE_R1", "Account Executive", ["London"]),
                             workday_job("AE-UK_R1-2", "Account Executive, UK", ["United Kingdom"])],
                            {"locations": ["London", "United Kingdom"]})
        get = lambda url, body=None: json.dumps(site(json.loads(body))[1]).encode()  # noqa: E731
        (posting,) = workday.read(BOARD, get, uk)
        self.assertEqual((posting.key, posting.title, posting.location),
                         ("workday:acme.wd3.careers:R1", "Account Executive", "London / United Kingdom"))
        self.assertTrue(posting.url.endswith("/careers/job/Somewhere/AE_R1"))

    def test_same_job_joins_a_tenants_sites_by_requisition_but_never_fallbacks(self):
        self.assertEqual(workday.same_job("workday:acme.wd3.careers:R1"), "workday:acme:R1")
        self.assertEqual(workday.same_job("workday:acme.wd3.europe:R1"), "workday:acme:R1")
        self.assertEqual(workday.same_job("workday:acme.wd3.careers:job/AE_R1"), "workday:acme.wd3.careers:job/AE_R1")
        self.assertEqual(fetch.same_job("workday:acme.wd3.careers:R1"), "workday:acme:R1")
        self.assertEqual(fetch.same_job("ashby:acme:1"), "ashby:acme:1")


class DescribeTest(unittest.TestCase):
    def setUp(self):
        site = workday_site([workday_job("AE_R1", "Account Executive")], {"locations": ["London"]})
        get = lambda url, body=None: json.dumps(site(json.loads(body))[1]).encode()  # noqa: E731
        (self.posting,) = workday.read(BOARD, get, uk)

    def test_the_detail_gives_the_description(self):
        asked = []

        def get(url, body=None):
            asked.append((url, body))
            return json.dumps(workday_detail("<p>Sell.</p><p>In London.</p>")).encode()
        self.assertEqual(workday.describe(BOARD, self.posting, get), "Sell.\nIn London.")
        self.assertEqual(asked, [("https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/careers/job/AE_R1", None)])

    def test_a_detail_without_text_fails_the_posting(self):
        for detail in [{}, {"jobPostingInfo": {}}, workday_detail("<p> </p>"),
                       {"jobPostingInfo": {"jobDescription": 3}}]:
            with self.assertRaises(BoardError, msg=detail):
                workday.describe(BOARD, self.posting, lambda url, body=None, d=detail: json.dumps(d).encode())
