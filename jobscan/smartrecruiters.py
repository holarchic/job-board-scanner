"""SmartRecruiters adapter: board URLs → API URL, the paged postings list → postings, and one posting's detail → its
description.

The list carries no descriptions, so `describe` fetches one for each posting the profile matched. The list comes
100 postings a page; pages that disagree with each other fail the board (see `read`). An unknown company answers
an empty list, never a 404, so a board that has gone reads as empty and is never retired."""
import json
from urllib.parse import quote

from jobscan.payload import board_slug, detail_text, html_to_text, http_url, named_company, text_field, with_remote
from jobscan.records import Board, BoardError, Company, Get, Places, Posting

ATS = "smartrecruiters"
DISPLAY_NAME = "SmartRecruiters"
CRAWL_HOSTS = ("jobs.smartrecruiters.com", "careers.smartrecruiters.com")
API_PATH = ("v1", "companies")
API = "https://api.smartrecruiters.com/" + "/".join(API_PATH) + "/"
LIST = "/postings"
JOB = "https://jobs.smartrecruiters.com/{slug}/{id}"
PAGE = 100  # the API's maximum: a larger limit is answered with 100
PREFIXED = "ni"  # jobs.smartrecruiters.com/ni/<slug>/… names its board one segment later


# SmartRecruiters company identifiers are case-insensitive (`nexthink` reads as `Nexthink`), so slugs are lower case.
# `fetch.board_for` tries every adapter's api_board before any page_board.


def api_board(company: str, url: str) -> Board | None:
    """A postings-API URL on any host (how tests point at a fake server); the host is kept."""
    parsed = http_url(url)
    path = [p for p in parsed.path.split("/") if p] if parsed else []
    if tuple(path[:2]) != API_PATH or path[3:4] != [LIST.strip("/")]:
        return None
    return _board(company, board_slug(path[2]), f"{parsed.scheme}://{parsed.netloc}/{'/'.join(API_PATH)}/")


def page_board(company: str, url: str) -> Board | None:
    """A board page: jobs.smartrecruiters.com/<slug>/… or /ni/<slug>/…, or careers.smartrecruiters.com/<slug>[/…]."""
    parsed = http_url(url)
    path = [p for p in parsed.path.split("/") if p] if parsed else []
    if parsed is None or parsed.hostname not in CRAWL_HOSTS:
        return None
    if parsed.hostname == CRAWL_HOSTS[0] and path[:1] == [PREFIXED]:
        path = path[1:]
    return _board(company, board_slug(path[0]), API) if path else None


def _board(company: str, slug: str | None, base: str) -> Board | None:
    if slug is None:
        return None
    slug = slug.lower()
    return Board(ats=ATS, slug=slug, company=company or slug, api_url=base + quote(slug, safe="") + LIST)


def company(board: Board, get: Get) -> Company:
    """The company the list names on each posting, read from a one-posting page; an empty board names none."""
    data = json.loads(get(f"{board.api_url}?limit=1"))
    content = data.get("content") if isinstance(data, dict) else None
    first = content[0] if isinstance(content, list) and content else None
    owner = first.get("company") if isinstance(first, dict) else None
    return named_company(owner.get("name") if isinstance(owner, dict) else None)


def read(board: Board, get: Get, places: Places) -> tuple[Posting, ...]:
    """Every page, each asked for at the count read so far. Drift checks, best effort: each page answers the offset
    asked for, the total holds across pages, the postings read match it and no id repeats. A change that keeps the
    count and moves no id across a page boundary passes; the next run reads it right."""
    rows, total = [], None
    while total is None or len(rows) < total:
        offset = len(rows)
        page_total, content = _page(json.loads(get(f"{board.api_url}?limit={PAGE}&offset={offset}")), offset)
        if total is not None and page_total != total:
            raise BoardError("list changed while paging: the total moved")
        if not content and offset < page_total:
            raise BoardError("list changed while paging: a page came back empty")
        total = page_total
        rows += content
    if len(rows) != total:
        raise BoardError("list changed while paging: more postings than the total")
    postings = tuple(_posting(board, index, row) for index, row in enumerate(rows))
    if len({p.key for p in postings}) != len(postings):
        raise BoardError("duplicate job ids")
    return postings


def _page(data, offset: int) -> tuple[int, list]:
    if not isinstance(data, dict):
        raise BoardError("response is not an object")
    total, content = data.get("totalFound"), data.get("content")
    if type(total) is not int or total < 0 or not isinstance(content, list):
        raise BoardError("response has no `totalFound` count or `content` list")
    if type(data.get("offset")) is not int or data["offset"] != offset:
        raise BoardError("list changed while paging: a page answered another offset")
    return total, content


def _posting(board: Board, index: int, row) -> Posting:
    if not isinstance(row, dict):
        raise BoardError(f"job {index} is not an object")
    job_id = text_field(row, "id", index, required=True).strip()
    return Posting(key=f"{board.id}:{job_id}", ats=ATS, board=board.id, company=board.company,
                   title=text_field(row, "name", index, required=True).strip(), location=_location(row, index),
                   url=JOB.format(slug=quote(board.slug, safe=""), id=quote(job_id, safe="")), description="")


def _location(row: dict, index: int) -> str:
    """`fullLocation` without its empty parts (`London, , United Kingdom` → `London, United Kingdom`), else city,
    region and country; `Remote` added for a remote job."""
    place = row.get("location")
    if place is None:
        return ""
    if not isinstance(place, dict):
        raise BoardError(f"job {index}: `location` is not an object")
    full = text_field(place, "fullLocation", index) or ", ".join(
        filter(None, (text_field(place, part, index) for part in ("city", "region", "country"))))
    parts = [part.strip() for part in full.split(",") if part.strip()]
    places = [", ".join(parts)] if parts else []
    return " / ".join(with_remote(places) if place.get("remote") is True else places)


def describe(board: Board, posting: Posting, get: Get) -> str:
    """Each section of the posting's job ad with text (company, job, qualifications, more) as its title, then its
    text. An ad without a `jobDescription` section, or with no text at all, fails the posting: it would be tagged
    and delivered with no description, and never described again."""
    job_id = posting.key.removeprefix(f"{board.id}:")
    data = json.loads(get(f"{board.api_url}/{quote(job_id, safe='')}"))
    ad = data.get("jobAd") if isinstance(data, dict) else None
    sections = ad.get("sections") if isinstance(ad, dict) else None
    if not isinstance(sections, dict) or not all(isinstance(s, dict) for s in sections.values()):
        raise BoardError("detail has no `jobAd.sections` object")
    if "jobDescription" not in sections:
        raise BoardError("detail has no `jobDescription` section")
    lines = []
    for name, section in sections.items():
        text = html_to_text(detail_text(section, "text", f"section {name}"))
        if text:
            lines += [detail_text(section, "title", f"section {name}").strip(), text]
    if not lines:
        raise BoardError("detail has no text")
    return "\n".join(line for line in lines if line)
