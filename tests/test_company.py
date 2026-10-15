"""Company lookups: each adapter's `company` against the page or API it reads, and the name clean-up they share."""
import json
import unittest

from jobscan import ashby, fetch, gem, greenhouse, payload, personio, rippling, smartrecruiters, teamtailor, workable
from jobscan.records import BoardError, Company


def getter(pages):
    """A fake `get` serving `pages` (URL → str or JSON value) and recording the URLs asked for."""
    asked = []

    def get(url):
        asked.append(url)
        page = pages[url]
        return (page if isinstance(page, str) else json.dumps(page)).encode()
    return get, asked


def look_up(adapter, url, pages):
    board = fetch.board_for("", url)
    get, asked = getter(pages)
    return adapter.company(board, get), asked


def html_page(title):
    return f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>{title}</title></head><body></body></html>'


class NamedCompanyTest(unittest.TestCase):
    def test_a_name_is_decoded_and_flattened_to_one_line(self):
        self.assertEqual(payload.named_company(" Acme &amp; Co\n\x07 Ltd\t"), Company("Acme & Co Ltd", None))

    def test_a_website_is_kept_only_as_an_http_url(self):
        for website, kept in [(" https://acme.com/ ", "https://acme.com/"), ("http://acme.com", "http://acme.com"),
                              ("acme.com", None), ("javascript:alert(1)", None), ("", None), (None, None), (3, None),
                              ("https://acme.com/\ud800", None), ("https://acme.com/\nCompany: x", None),
                              ("https://acme.com/a b", None), ("https://acme.com/\x07", None)]:
            self.assertEqual(payload.named_company("Acme", website).website, kept, website)

    def test_no_usable_name_is_a_board_error(self):
        for name in [None, 3, "", " \n ", "\x00\x07", "&#32;"]:
            with self.assertRaises(BoardError, msg=name):
                payload.named_company(name)

    def test_html_title(self):
        self.assertEqual(payload.html_title(b"<html><TITLE lang='en'>\n Acme  Careers\n</Title>"), "Acme Careers")
        for page in [b"<html><body>no title</body></html>", b"<title>never closed", b"<title"]:
            self.assertEqual(payload.html_title(page), "", page)

    def test_element_text_takes_linear_time_on_unterminated_tags(self):
        page = '<script id="__NEXT_DATA__"' * 200_000
        self.assertIsNone(payload.element_text(page, '<script id="__NEXT_DATA__"', "</script>"))
        self.assertEqual(payload.element_text('<script id="__NEXT_DATA__" type="json">{}</script>',
                                              '<script id="__NEXT_DATA__"', "</script>"), "{}")


class AdapterTest(unittest.TestCase):
    def test_every_adapter_looks_up_companies(self):
        for name, adapter in fetch.ADAPTERS.items():
            self.assertTrue(callable(getattr(adapter, "company", None)), name)

    def test_ashby_reads_the_board_pages_app_data(self):
        app = {"organization": {"name": "Acme AI", "publicWebsite": "https://acme.ai/"}, "posting": None}
        page = f"<script>window.__appData = {json.dumps(app)};\nwindow.other = 1;</script>"
        company, asked = look_up(ashby, "https://jobs.ashbyhq.com/Acme", {"https://jobs.ashbyhq.com/acme": page})
        self.assertEqual((company, asked), (Company("Acme AI", "https://acme.ai/"), ["https://jobs.ashbyhq.com/acme"]))

    def test_ashby_asks_for_the_page_as_its_api_url_writes_the_slug(self):
        page = 'window.__appData = {"organization": {"name": "Acme"}}'
        company, asked = look_up(ashby, "https://jobs.ashbyhq.com/cirrus%20systems",
                                 {"https://jobs.ashbyhq.com/cirrus%20systems": page})
        self.assertEqual((company, asked), (Company("Acme", None), ["https://jobs.ashbyhq.com/cirrus%20systems"]))

    def test_ashby_pages_naming_no_organisation_fail(self):
        for page in ["<html>no app data</html>", "window.__appData = {broken", "window.__appData = []",
                     'window.__appData = {"organization": null}', 'window.__appData = {"organization": {"name": ""}}']:
            with self.assertRaises((BoardError, ValueError), msg=page):
                look_up(ashby, "https://jobs.ashbyhq.com/acme", {"https://jobs.ashbyhq.com/acme": page})

    def test_greenhouse_reads_the_boards_api_record(self):
        company, asked = look_up(greenhouse, "https://job-boards.greenhouse.io/acme",
                                 {"https://boards-api.greenhouse.io/v1/boards/acme": {"name": "Acme ", "content": ""}})
        self.assertEqual((company, asked), (Company("Acme", None), ["https://boards-api.greenhouse.io/v1/boards/acme"]))

    def test_workable_reads_the_account_without_its_details(self):
        url = "https://apply.workable.com/api/v1/widget/accounts/Acme"
        company, asked = look_up(workable, "https://apply.workable.com/Acme/", {url: {"name": "Acme", "jobs": []}})
        self.assertEqual((company, asked), (Company("Acme", None), [url]))

    def test_gem_reads_the_board_pages_title(self):
        company, asked = look_up(gem, "https://jobs.gem.com/Acme-co",
                                 {"https://jobs.gem.com/Acme-co": html_page("Acme &amp; Co Careers")})
        self.assertEqual((company, asked), (Company("Acme & Co", None), ["https://jobs.gem.com/Acme-co"]))
        for title in ["Access denied", "Careers", ""]:
            with self.assertRaises(BoardError, msg=title):
                look_up(gem, "https://jobs.gem.com/Acme-co", {"https://jobs.gem.com/Acme-co": html_page(title)})

    def test_teamtailor_reads_a_one_job_feeds_channel_title(self):
        url = "https://acme.teamtailor.com/jobs.rss?per_page=1"
        feed = '<?xml version="1.0"?><rss version="2.0"><channel><title>Acme Group</title></channel></rss>'
        company, asked = look_up(teamtailor, "https://acme.teamtailor.com/jobs", {url: feed})
        self.assertEqual((company, asked), (Company("Acme Group", None), [url]))

    def test_personio_reads_the_board_pages_title_and_a_bare_one_names_nobody(self):
        url = "https://acme.jobs.personio.com/?language=en"
        company, asked = look_up(personio, "https://acme.jobs.personio.de/", {url: html_page("Jobs at Acme GmbH")})
        self.assertEqual((company, asked), (Company("Acme GmbH", None), [url]))
        for title in ["Jobs at ", "Access denied", "Jobs bei Acme"]:
            with self.assertRaises(BoardError, msg=title):
                look_up(personio, "https://acme.jobs.personio.de/", {url: html_page(title)})

    def test_smartrecruiters_reads_a_one_posting_pages_company(self):
        url = "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=1"
        page = {"totalFound": 9, "content": [{"id": "1", "company": {"identifier": "Acme", "name": "Acme"}}]}
        company, asked = look_up(smartrecruiters, "https://jobs.smartrecruiters.com/Acme/1-ae", {url: page})
        self.assertEqual((company, asked), (Company("Acme", None), [url]))
        for empty in [{"content": []}, {"content": [{"company": None}]}, []]:
            with self.assertRaises(BoardError, msg=empty):
                look_up(smartrecruiters, "https://jobs.smartrecruiters.com/Acme/1-ae", {url: empty})

    def test_rippling_reads_the_board_pages_next_data(self):
        url = "https://ats.rippling.com/Acme-careers/jobs"
        data = {"props": {"pageProps": {"apiData": {"jobBoard": {"companyName": "Acme", "isTest": False}}}}}
        page = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'
        company, asked = look_up(rippling, "https://ats.rippling.com/en-GB/Acme-careers/jobs/1", {url: page})
        self.assertEqual((company, asked), (Company("Acme", None), [url]))
        for broken in ["<html></html>", '<script id="__NEXT_DATA__">{"props": {}}</script>']:
            with self.assertRaises(BoardError, msg=broken):
                look_up(rippling, "https://ats.rippling.com/Acme-careers/jobs", {url: broken})


if __name__ == "__main__":
    unittest.main()
