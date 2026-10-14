"""Rippling adapter: board URLs → API URL, the paged jobs list → postings, and one job's detail → its description.

The list carries no descriptions, so `describe` fetches one for each posting the profile matched. The list repeats
a job once per location: rows sharing an id are one job, carrying every row's location. It comes 1,000 rows a page;
pages that disagree with each other fail the board (see `read`)."""
import json
import re
from dataclasses import dataclass
from urllib.parse import quote

from jobscan.payload import board_slug, html_to_text, http_url, text_field, with_remote
from jobscan.records import Board, BoardError, Get, Posting

ATS = "rippling"
DISPLAY_NAME = "Rippling"
BOARD_HOST = "ats.rippling.com"
CRAWL_HOSTS = (BOARD_HOST,)
API_PATH = ("api", "v2", "board")
API = f"https://{BOARD_HOST}/" + "/".join(API_PATH) + "/"
LIST = "/jobs"
JOB = f"https://{BOARD_HOST}/{{slug}}/jobs/{{id}}"
PAGE = 1000  # the API's maximum: 2,000 is refused
LOCALE = re.compile(r"[a-z]{2}-(?:[a-z]{2}|\d{3})", re.IGNORECASE)  # en-GB, en-gb, es-419
EMBED = "embed"


# Rippling board slugs are case-sensitive (`RudderStack-Careers` is a 404 where `rudderstack-careers` reads), so a
# slug keeps its case. `fetch.board_for` tries every adapter's api_board before any page_board.


def api_board(company: str, url: str) -> Board | None:
    """A jobs-API URL on any host (how tests point at a fake server); the host is kept."""
    parsed = http_url(url)
    path = [p for p in parsed.path.split("/") if p] if parsed else []
    if tuple(path[:3]) != API_PATH or path[4:5] != [LIST.strip("/")]:
        return None
    return _board(company, board_slug(path[3]), f"{parsed.scheme}://{parsed.netloc}/{'/'.join(API_PATH)}/")


def page_board(company: str, url: str) -> Board | None:
    """A board page, ats.rippling.com/<slug>/jobs[/…], also after a locale (`/en-GB/<slug>/jobs`) or `/embed`."""
    parsed = http_url(url)
    path = [p for p in parsed.path.split("/") if p] if parsed else []
    if parsed is None or parsed.hostname != BOARD_HOST:
        return None
    if path[2:3] == [LIST.strip("/")] and (path[0] == EMBED or LOCALE.fullmatch(path[0])):
        path = path[1:]
    if path[1:2] != [LIST.strip("/")] or path[0] == EMBED or LOCALE.fullmatch(path[0]):
        return None
    return _board(company, board_slug(path[0]), API)


def _board(company: str, slug: str | None, base: str) -> Board | None:
    if slug is None:
        return None
    return Board(ats=ATS, slug=slug, company=company or slug, api_url=base + quote(slug, safe="") + LIST)


@dataclass(frozen=True)
class _Row:
    job_id: str
    title: str
    places: tuple[str, ...]


def read(board: Board, get: Get) -> tuple[Posting, ...]:
    """Every page, then rows merged by job id. Drift checks, best effort: each page is the one asked for, no page
    before the last comes back empty, the totals hold across pages and the rows read match them. Rows can't be
    checked for overlap: lists repeat identical rows within one page (6 of 60 boards sampled on 14 Oct). Only a
    board of over PAGE rows pages at all."""
    rows, totals, page = [], None, 0
    while totals is None or page < totals[1]:
        page_totals, items = _page(json.loads(get(f"{board.api_url}?page={page}&pageSize={PAGE}")), page)
        if totals is not None and page_totals != totals:
            raise BoardError("list changed while paging: the totals moved")
        totals = page_totals
        if not items and page + 1 < totals[1]:
            raise BoardError("list changed while paging: a page came back empty")
        rows += [_row(len(rows) + index, item) for index, item in enumerate(items)]
        page += 1
    if len(rows) != totals[0]:
        raise BoardError("list changed while paging: the rows read don't match the total")
    return tuple(_posting(board, job_rows) for job_rows in _by_job(rows))


def _page(data, page: int) -> tuple[tuple[int, int], list]:
    if not isinstance(data, dict):
        raise BoardError("response is not an object")
    items, total_items, total_pages = data.get("items"), data.get("totalItems"), data.get("totalPages")
    if not isinstance(items, list) or not all(type(n) is int and n >= 0 for n in (total_items, total_pages)):
        raise BoardError("response has no `items` list or `totalItems` and `totalPages` counts")
    if data.get("page") != page:
        raise BoardError("list changed while paging: a page answered as another")
    return (total_items, total_pages), items


def _row(index: int, item) -> _Row:
    if not isinstance(item, dict):
        raise BoardError(f"job {index} is not an object")
    locations = item.get("locations")
    locations = [] if locations is None else locations
    if not isinstance(locations, list) or not all(isinstance(loc, dict) for loc in locations):
        raise BoardError(f"job {index}: `locations` is not a list of objects")
    places = [_place(loc, index) for loc in locations]
    places = [p for p in dict.fromkeys(places) if p]
    if any(loc.get("workplaceType") == "REMOTE" for loc in locations):
        places = with_remote(places)
    return _Row(job_id=text_field(item, "id", index, required=True).strip(),
                title=text_field(item, "name", index, required=True).strip(), places=tuple(places))


def _place(location: dict, index: int) -> str:
    """The location's name, with its country added when the name doesn't say it."""
    name = (text_field(location, "name", index) or "").strip()
    country = (text_field(location, "country", index) or "").strip()
    if country and country.casefold() not in name.casefold():
        return f"{name}, {country}" if name else country
    return name


def _by_job(rows: list[_Row]) -> list[list[_Row]]:
    jobs: dict[str, list[_Row]] = {}
    for row in rows:
        jobs.setdefault(row.job_id, []).append(row)
    for job_id, job_rows in jobs.items():
        if len({r.title for r in job_rows}) > 1:
            raise BoardError(f"job {job_id}: its rows disagree on the title")
    return list(jobs.values())


def _posting(board: Board, rows: list[_Row]) -> Posting:
    places = list(dict.fromkeys(place for row in rows for place in row.places))
    first = rows[0]
    return Posting(key=f"{board.id}:{first.job_id}", ats=ATS, board=board.id, company=board.company,
                   title=first.title, location=" / ".join(places),
                   url=JOB.format(slug=quote(board.slug, safe=""), id=quote(first.job_id, safe="")), description="")


def describe(board: Board, posting: Posting, get: Get) -> str:
    """The job's detail: the company's description, then the role's."""
    job_id = posting.key.removeprefix(f"{board.id}:")
    data = json.loads(get(f"{board.api_url}/{quote(job_id, safe='')}"))
    description = data.get("description") if isinstance(data, dict) else None
    if not isinstance(description, dict):
        raise BoardError("detail has no `description` object")
    parts = [html_to_text(text_field(description, part, 0) or "") for part in ("company", "role")]
    return "\n".join(part for part in parts if part)
