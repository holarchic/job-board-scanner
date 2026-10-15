import json
import unittest
from urllib.parse import parse_qs, urlsplit

from jobscan import fetch, oracle
from jobscan.records import BoardError
from tests.fakes import anywhere, oracle_detail, oracle_job, oracle_page

URL = "https://acme.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001"
BOARD = oracle.page_board("Acme", URL)


def offset(url):
    """The `offset=` inside the list URL's `finder` parameter."""
    finder = parse_qs(urlsplit(url).query)["finder"][0]
    return int(dict(part.split("=", 1) for part in finder.split(";", 1)[1].split(","))["offset"])


class BoardTest(unittest.TestCase):
    def test_site_pages_name_one_board_whatever_the_language_case_or_page(self):
        for url in [URL, "https://acme.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/fr/sites/cx_1001",
                    "https://ACME.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/pt-BR/sites/CX_1001/job/42",
                    "http://acme.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/requisitions"]:
            board = oracle.page_board("", url)
            self.assertEqual(board.slug, "acme.em2.cx_1001", url)
        self.assertTrue(BOARD.api_url.startswith(
            "https://acme.fa.em2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions?"))
        self.assertIn("siteNumber=cx_1001,limit=200,offset=0", BOARD.api_url)

    def test_pods_regions_and_sites_are_separate_boards(self):
        others = ["https://acme.fa.em3.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001",
                  "https://acme2.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001",
                  "https://acme.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1"]
        self.assertEqual(len({BOARD.id, *(oracle.page_board("", u).id for u in others)}), 4)

    def test_test_and_dev_pods_are_no_boards(self):
        for pod in ["efpv-dev10", "eluq-dev19", "emcm-test", "fa-euzu-test-saasfaprod1", "acme-uat", "acme-stg2"]:
            url = f"https://{pod}.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX"
            self.assertIsNone(oracle.page_board("", url), pod)
        for pod in ["fa-ewdg-saasfaprod1", "devon", "attest"]:  # a word holding the letters is a real pod
            url = f"https://{pod}.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX"
            self.assertIsNotNone(oracle.page_board("", url), pod)

    def test_other_urls_name_nothing(self):
        for url in ["https://acme.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/",
                    "https://acme.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en",
                    "https://acme.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/english/sites/CX",
                    "https://acme.fa.em2.oraclecloud.com/hcmUI/Other/en/sites/CX",
                    "https://acme.fa.em2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
                    "https://acme.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX",
                    "https://acme.fa.em2.oraclecloud.com.evil.example/hcmUI/CandidateExperience/en/sites/CX",
                    "https://www.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX",
                    "https://acme.fa..oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX",
                    "ftp://acme.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX"]:
            self.assertIsNone(oracle.page_board("", url), url)

    def test_fetch_routes_site_pages_here(self):
        self.assertEqual(fetch.board_for("", URL).ats, "oracle")
        self.assertEqual(BOARD.company, "Acme")

    def test_one_pods_sites_share_a_job_and_other_pods_dont(self):
        same = oracle.same_job("oracle:acme.em2.cx_1001:42")
        self.assertEqual(same, "oracle:acme.em2:42")
        self.assertEqual(oracle.same_job("oracle:acme.em2.htv:42"), same)
        self.assertNotEqual(oracle.same_job("oracle:acme.em3.cx_1001:42"), same)
        self.assertNotEqual(oracle.same_job("oracle:acme.em2.cx_1001:43"), same)
        self.assertEqual(fetch.same_job("oracle:acme.em2.cx.v2:42"), "oracle:acme.em2:42")  # a dot in the site


class ReadTest(unittest.TestCase):
    def read(self, pages):
        """`pages` maps an offset to its answer, or is a callable of the offset."""
        self.asked = []

        def get(url):
            self.assertTrue(url.startswith(BOARD.api_url.rsplit("offset=", 1)[0]), url)
            self.asked.append(offset(url))
            answer = pages(offset(url)) if callable(pages) else pages[offset(url)]
            return json.dumps(answer).encode()
        return {p.key.split(":", 2)[2]: p for p in oracle.read(BOARD, get, anywhere)}

    def jobs(self, n):
        return [oracle_job(str(i), "Account Executive") for i in range(n)]

    def test_every_page_is_read_200_apart(self):
        jobs = self.jobs(450)
        postings = self.read(lambda at: oracle_page(jobs, at))
        self.assertEqual((len(postings), self.asked), (450, [0, 200, 400]))
        posting = postings["7"]
        self.assertEqual((posting.key, posting.ats, posting.board, posting.company, posting.description),
                         ("oracle:acme.em2.cx_1001:7", "oracle", "oracle:acme.em2.cx_1001", "Acme", ""))
        self.assertEqual(posting.url,
                         "https://acme.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/cx_1001/job/7")

    def test_a_short_page_before_the_end_steps_on_by_200_and_loses_nothing(self):
        jobs = self.jobs(445)
        pages = {0: oracle_page(jobs[:199], total=446), 200: oracle_page(jobs[199:399], total=446),
                 400: oracle_page(jobs[399:], total=446)}
        self.assertEqual(len(self.read(pages)), 445)
        self.assertEqual(self.asked, [0, 200, 400])

    def test_a_count_over_the_jobs_returned_is_accepted(self):
        self.assertEqual(len(self.read({0: oracle_page(self.jobs(140), total=145)})), 140)
        self.assertEqual(len(self.read({0: oracle_page(self.jobs(25), total=66)})), 25)  # jobs it never shows
        self.assertEqual(self.read({0: oracle_page([], total=1)}), {})
        self.assertEqual(self.read({0: oracle_page([], total=0)}), {})

    def test_a_pod_paging_100_at_a_time_fails_the_board(self):
        jobs = self.jobs(1000)
        with self.assertRaisesRegex(BoardError, "a page before the end holds 100 jobs"):
            self.read(lambda at: oracle_page(jobs, at, size=100))
        with self.assertRaisesRegex(BoardError, "holds 179 jobs"):
            self.read(lambda at: oracle_page(jobs, at, size=179))
        hidden = {0: oracle_page(jobs[:180], total=400), 200: oracle_page(jobs[200:400], total=400)}
        self.assertEqual(len(self.read(hidden)), 380)  # 180 shown of a window of 200 is enough

    def test_a_count_over_the_window_fails_the_board_after_one_request(self):
        with self.assertRaisesRegex(BoardError, "over 10,000 jobs"):
            self.read({0: oracle_page(self.jobs(200), total=10_001)})
        self.assertEqual(self.asked, [0])
        jobs = self.jobs(10_000)
        self.assertEqual(len(self.read(lambda at: oracle_page(jobs, at))), 10_000)  # the window itself reads

    def test_drift_fails_the_board(self):
        jobs = self.jobs(450)
        untitled = {"Id": "0", "Title": None}  # only it repeats, standing in for a titled job page two loses
        repeated = {0: [untitled, *jobs[1:200]], 200: [untitled, *jobs[200:399]], 400: jobs[399:]}
        cases = [
            ("the count moved", lambda at: oracle_page(jobs, at, total=450 if at == 0 else 451)),
            ("a page before the end holds 0 jobs",
             lambda at: oracle_page(jobs if at in (0, 400) else [], at, total=450)),
            ("came back twice", lambda at: oracle_page(jobs[:200] * 3, at if at < 400 else 0, total=450)),
            ("came back twice", lambda at: oracle_page(repeated[at], total=450)),
            ("read 400 jobs of a count of 300", lambda at: oracle_page(jobs[at:at + 200], total=300)),
            ("more jobs than asked for", lambda at: oracle_page(jobs, at, size=201)),
        ]
        for reason, pages in cases:
            with self.assertRaisesRegex(BoardError, reason):
                self.read(pages)

    def test_malformed_pages_and_rows_fail_the_board(self):
        for answer in [{}, {"items": []}, {"items": [{"requisitionList": []}]},
                       {"items": [{"TotalJobsCount": "3", "requisitionList": []}]},
                       {"items": [{"TotalJobsCount": -1, "requisitionList": []}]}, [],
                       oracle_page([{"Title": "AE"}]), oracle_page([{"Id": "1", "Title": 7}]),
                       oracle_page([{"Id": 1, "Title": "AE"}]),
                       oracle_page(["AE"]), oracle_page([{"Id": "1\n2", "Title": "AE"}]),
                       oracle_page([{**oracle_job("1", "AE"), "secondaryLocations": "Leeds"}]),
                       oracle_page([{**oracle_job("1", "AE"), "PrimaryLocation": 7}]),
                       *(oracle_page([{**oracle_job("1", "AE"), "secondaryLocations": v}]) for v in ({}, False, 0, "")),
                       oracle_page([{**oracle_job("1", "AE"), "WorkplaceTypeCode": ["ORA_REMOTE"]}])]:
            with self.assertRaises(BoardError, msg=answer):
                self.read({0: answer})

    def test_titles_lose_invisible_characters_and_rows_without_one_are_skipped(self):
        jobs = [oracle_job("1", "GHD\xa0Graduate\u200b  Program\n"), {"Id": "2", "Title": None},
                {"Id": "3", "Title": " \u200b "}, oracle_job("4", "AE")]
        postings = self.read({0: oracle_page(jobs)})
        self.assertEqual({k: p.title for k, p in postings.items()}, {"1": "GHD Graduate Program", "4": "AE"})

    def test_location_is_every_place_once_and_remote_jobs_say_so(self):
        jobs = [oracle_job("1", "AE", "London, United Kingdom", ["Leeds, United Kingdom", "London, United Kingdom"]),
                oracle_job("2", "AE", "Paris, France", workplace="ORA_REMOTE"),
                oracle_job("3", "AE", "Paris, France", workplace="ORA_HYBRID"),
                oracle_job("4", "AE", None, ["Leeds"]),
                oracle_job("5", "AE", "Remote, United Kingdom", workplace="ORA_REMOTE")]
        postings = self.read({0: oracle_page(jobs)})
        self.assertEqual({k: p.location for k, p in postings.items()}, {
            "1": "London, United Kingdom / Leeds, United Kingdom", "2": "Paris, France / Remote",
            "3": "Paris, France", "4": "Leeds", "5": "Remote, United Kingdom"})


class DescribeTest(unittest.TestCase):
    POSTING = oracle.read(BOARD, lambda url: json.dumps(oracle_page([oracle_job("42", "AE")])).encode(),
                          anywhere)[0]

    def describe(self, detail):
        self.asked = []

        def get(url):
            self.asked.append(url)
            return json.dumps(detail).encode()
        return oracle.describe(BOARD, self.POSTING, get)

    def test_every_section_with_text_in_reading_order(self):
        text = self.describe(oracle_detail(ExternalQualificationsStr="<p>Five years.</p>",
                                           CorporateDescriptionStr="<p>We sell software.</p>",
                                           ExternalResponsibilitiesStr="<p>Close deals.</p>",
                                           ExternalDescriptionStr="<ul><li>Sell it.</li></ul>",
                                           OrganizationDescriptionStr="<p>The UK team.</p>"))
        self.assertEqual(text, "We sell software.\n\nThe UK team.\n\nSell it.\n\nClose deals.\n\nFive years.")
        only = oracle_detail(CorporateDescriptionStr="  ", ExternalDescriptionStr="<p>Sell.</p>")
        self.assertEqual(self.describe(only), "Sell.")
        self.assertEqual(self.asked, ["https://acme.fa.em2.oraclecloud.com/hcmRestApi/resources/latest/"
                                      "recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
                                      "&finder=ById;Id=%2242%22,siteNumber=cx_1001"])

    def test_a_job_id_holding_a_colon_is_asked_for_whole(self):
        self.POSTING = oracle.read(BOARD, lambda url: json.dumps(oracle_page([oracle_job("42:7", "AE")])).encode(),
                                   anywhere)[0]
        self.assertEqual(oracle.same_job(self.POSTING.key), "oracle:acme.em2:42:7")
        self.describe(oracle_detail(ExternalDescriptionStr="x"))
        self.assertIn("Id=%2242%3A7%22", self.asked[0])

    def test_a_detail_without_text_or_malformed_fails_the_posting(self):
        for detail in [oracle_detail(), oracle_detail(ExternalDescriptionStr="<p> </p>"), {"items": []}, {},
                       {"items": ["x"]}, oracle_detail(ExternalDescriptionStr=7), []]:
            with self.assertRaises(BoardError, msg=detail):
                self.describe(detail)


class CompanyTest(unittest.TestCase):
    def look_up(self, page):
        self.asked = []

        def get(url):
            self.asked.append(url)
            return page.encode()
        return oracle.company(BOARD, get)

    def test_the_site_pages_title_names_the_company(self):
        self.assertEqual(self.look_up("<html><head><title>Hearst  Television</title></head></html>").name,
                         "Hearst Television")
        self.assertEqual(self.asked,
                         ["https://acme.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/cx_1001"])

    def test_oracles_default_title_or_none_names_nobody(self):
        for page in ["<title>Candidate Experience site</title>", "<title>CANDIDATE EXPERIENCE SITE</title>",
                     "<title>Candidate&#32;Experience&nbsp;site</title>", "<title> </title>", "<html></html>"]:
            with self.assertRaises(BoardError, msg=page):
                self.look_up(page)


if __name__ == "__main__":
    unittest.main()
