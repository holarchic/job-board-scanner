"""Oracle Recruiting Cloud adapter: site URLs → the site's job-list API, the whole list → postings, and one job's
detail → its description.

A site is <pod>.fa.<region>.oraclecloud.com/hcmUI/CandidateExperience/<language>/sites/<site>. Its list API answers
200 jobs a page and nothing at an offset of 10,000 or more. Its location filter lists only a site's ten biggest
places, so a site is read whole, asking only for the fields the title and location filter need. The list carries no
descriptions, so `describe` fetches one for each posting the profile matched."""
import json
import re
from dataclasses import dataclass
from urllib.parse import quote

from jobscan.payload import (
    board_slug,
    detail_text,
    html_title,
    html_to_text,
    http_url,
    named_company,
    text_field,
    with_remote,
)
from jobscan.records import Board, BoardError, Company, Get, Places, Posting

ATS = "oracle"
DISPLAY_NAME = "Oracle"
DOMAIN = "oraclecloud.com"
CRAWL_HOSTS = (f"*.{DOMAIN}",)
SITE = "https://{pod}.fa.{region}." + DOMAIN  # run tests point it at their fake server
SITE_PATH = ("hcmUI", "CandidateExperience")
PAGE_PATH = "/hcmUI/CandidateExperience/en/sites/{site}"
JOB_PATH = PAGE_PATH + "/job/{id}"
REST = "/hcmRestApi/resources/latest/"
LIST = (REST + "recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations"
        "&fields=TotalJobsCount;requisitionList:Id,Title,PrimaryLocation,WorkplaceTypeCode;"
        "requisitionList.secondaryLocations:Name&finder=findReqs;siteNumber={site},limit={limit},offset={offset}")
DETAIL = REST + "recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder=ById;Id=%22{id}%22,siteNumber={site}"
PAGE = 200  # the API's maximum: a larger limit still answers 200 rows, so offsets must step by 200
WINDOW = 10_000  # an offset this far in answers no rows, so a longer list can't be read
LANGUAGE = re.compile(r"[a-z]{2}(?:-[a-z0-9]{2,4})?", re.IGNORECASE)  # en, pt-br, zh-hans
COPY_POD = re.compile(r"(?:dev|test|uat|stage|stg)\d*")  # a part of a test or dev pod's name: `efpv-dev10`
REMOTE = "ORA_REMOTE"
DEFAULT_TITLE = "candidate experience site"  # the title Oracle gives a site page nobody named
# The description sections a detail can carry, in reading order; each site fills its own subset.
SECTIONS = ("CorporateDescriptionStr", "OrganizationDescriptionStr", "ExternalDescriptionStr",
            "ExternalResponsibilitiesStr", "ExternalQualificationsStr")


# A slug is `<pod>.<region>.<site>`, lower case: the site number is case-insensitive (`ubercareers` reads as
# `UBERCAREERS`), and neither pod nor region holds a dot, so the slug splits back into the three. The language in a
# site URL is dropped: every language of a site is one list.


def api_board(company: str, url: str) -> Board | None:
    """None: the API lives on the site's own host, which page_board reads. (`fetch.board_for` tries every api_board
    first.)"""
    return None


def page_board(company: str, url: str) -> Board | None:
    """A site page, <pod>.fa.<region>.oraclecloud.com/hcmUI/CandidateExperience/<language>/sites/<site>[/…], unless
    the pod is a test or dev copy (they repeat real jobs)."""
    parsed = http_url(url)
    labels = parsed.hostname.split(".") if parsed else []
    if len(labels) != 5 or labels[1] != "fa" or ".".join(labels[3:]) != DOMAIN:
        return None
    path = [p for p in parsed.path.split("/") if p]
    if (tuple(path[:2]) != SITE_PATH or len(path) < 5 or not LANGUAGE.fullmatch(path[2]) or path[3] != "sites"
            or any(COPY_POD.fullmatch(part) for part in labels[0].split("-"))):
        return None
    pod, site = board_slug(labels[0], decoded=True), board_slug(path[4])
    if pod is None or site is None:
        return None
    slug = f"{pod}.{labels[2]}.{site}".lower()
    return Board(ats=ATS, slug=slug, company=company or slug, api_url=_url(slug, LIST, limit=PAGE, offset=0))


def _url(slug: str, path: str, **fields) -> str:
    """`path` on the slug's site host, its site number and `fields` filled in, each percent-encoded."""
    pod, region, site = slug.split(".", 2)
    encoded = {name: quote(str(value), safe="") for name, value in fields.items()}
    return SITE.format(pod=pod, region=region) + path.format(site=quote(site, safe=""), **encoded)


def company(board: Board, get: Get) -> Company:
    """The site page's title; Oracle's default title, or none, names nobody."""
    title = html_title(get(_url(board.slug, PAGE_PATH)))
    if title.casefold() == DEFAULT_TITLE:
        raise BoardError("the site page has Oracle's default title")
    return named_company(title)


@dataclass(frozen=True)
class _Page:
    total: int
    rows: list


def read(board: Board, get: Get, places: Places) -> tuple[Posting, ...]:
    """Every page, 200 apart, until the first page's count. The count can exceed the jobs the list returns (a page
    may come back short before the end), so it is only a bound: the rows read may fall short of it by a tenth plus
    five (a pod paging fewer than 200 at a time comes up about half short). Drift checks, best effort: a count that
    changes, a job twice, a page before the end coming back empty, or rows outside those bounds fail the board."""
    first = _page(get(_url(board.slug, LIST, limit=PAGE, offset=0)))
    if first.total > WINDOW:
        raise BoardError(f"over {WINDOW:,} jobs: Oracle pages no further")
    page, offset, postings = first, 0, {}
    while True:
        for row in page.rows:
            posting = _posting(board, len(postings), row)
            if postings.setdefault(posting.key, posting) is not posting:
                raise BoardError("list changed while paging: a job came back twice")
        offset += PAGE
        if offset >= first.total:
            break
        page = _page(get(_url(board.slug, LIST, limit=PAGE, offset=offset)))
        if page.total != first.total:
            raise BoardError("list changed while paging: the count moved")
        if not page.rows:
            raise BoardError("list changed while paging: a page came back empty")
    if not first.total - first.total // 10 - 5 <= len(postings) <= first.total:
        raise BoardError(f"read {len(postings)} jobs of a count of {first.total}")
    return tuple(postings.values())


def _page(raw: bytes) -> _Page:
    data = json.loads(raw)
    items = data.get("items") if isinstance(data, dict) else None
    item = items[0] if isinstance(items, list) and items else None
    total = item.get("TotalJobsCount") if isinstance(item, dict) else None
    rows = item.get("requisitionList") if isinstance(item, dict) else None
    if type(total) is not int or total < 0 or not isinstance(rows, list):
        raise BoardError("response has no `TotalJobsCount` count or `requisitionList` list")
    if len(rows) > PAGE:
        raise BoardError("a page holds more jobs than asked for")
    return _Page(total=total, rows=rows)


def _posting(board: Board, index: int, row) -> Posting:
    if not isinstance(row, dict):
        raise BoardError(f"job {index} is not an object")
    job_id = text_field(row, "Id", index, required=True).strip()
    title = text_field(row, "Title", index, required=True).strip()
    if not job_id.isprintable() or not title.isprintable():
        raise BoardError(f"job {index}: `Id` or `Title` is not printable")
    return Posting(key=f"{board.id}:{job_id}", ats=ATS, board=board.id, company=board.company, title=title,
                   location=" / ".join(_places(row, index)), url=_url(board.slug, JOB_PATH, id=job_id),
                   description="")


def _places(row: dict, index: int) -> list[str]:
    """The primary location, then each secondary one, without repeats; `Remote` added for a remote job."""
    secondary = row.get("secondaryLocations") or []
    if not isinstance(secondary, list) or not all(isinstance(s, dict) for s in secondary):
        raise BoardError(f"job {index}: `secondaryLocations` is not a list of objects")
    places = []
    for named in [text_field(row, "PrimaryLocation", index)] + [text_field(s, "Name", index) for s in secondary]:
        place = (named or "").strip()
        if place and place not in places:
            places.append(place)
    return with_remote(places) if row.get("WorkplaceTypeCode") == REMOTE else places


def same_job(key: str) -> str:
    """The key every posting of one job shares across a pod's sites (`oracle:<pod>.<region>:<id>`): a pod numbers
    its jobs once, and each of its sites lists a job under that number."""
    ats, slug, job_id = key.split(":", 2)
    pod, region, _ = slug.split(".", 2)
    return f"{ats}:{pod}.{region}:{job_id}"


def describe(board: Board, posting: Posting, get: Get) -> str:
    """The job's detail, its description sections as text; a detail without any text fails the posting: it would be
    tagged and delivered with no description, and never described again."""
    job_id = posting.key.rsplit(":", 1)[-1]
    data = json.loads(get(_url(board.slug, DETAIL, id=job_id)))
    items = data.get("items") if isinstance(data, dict) else None
    detail = items[0] if isinstance(items, list) and items else None
    if not isinstance(detail, dict):
        raise BoardError("detail has no `items` object")
    text = "\n\n".join(t for t in (html_to_text(detail_text(detail, s, "items[0]")) for s in SECTIONS) if t)
    if not text:
        raise BoardError("detail has no text")
    return text
