"""Workable adapter: board URLs → API URL, and the public widget API payload → postings.

The widget lists a job once per location: entries sharing a shortcode are one job, carrying every entry's
locations. Parsing is all-or-nothing per board, as for the others.

Boards come from the boards file only: Workable rate-limits (HTTP 429 on every request) a daily sweep of the ~5,300
boards Common Crawl holds, so discovery leaves it out."""
import json
from dataclasses import dataclass
from urllib.parse import quote

from jobscan.payload import board_slug, html_to_text, http_url, named_company, text_field
from jobscan.records import Board, BoardError, Company, Get, Posting

ATS = "workable"
DISPLAY_NAME = "Workable"
describe = None  # the list carries each job's description
BOARD_HOST = "apply.workable.com"
CRAWL_HOSTS = ()  # not discovered: see the module docstring
API_PATH = ("api", "v1", "widget", "accounts")
API = f"https://{BOARD_HOST}/" + "/".join(API_PATH) + "/"
DETAILS = "?details=true"  # descriptions in the list, requirements and benefits included
NOT_BOARDS = frozenset({"j", "api"})  # apply.workable.com/j/<shortcode> links a job without naming its account


# Workable slugs are case-sensitive (`HuggingFace` is a 404 where `huggingface` reads), so a slug keeps its case.
# `fetch.board_for` tries every adapter's api_board before any page_board.


def api_board(company: str, url: str) -> Board | None:
    """A widget-API URL on any host (how tests point at a fake server); the host is kept."""
    parsed = http_url(url)
    path = [p for p in parsed.path.split("/") if p] if parsed else []
    if tuple(path[:4]) != API_PATH or len(path) < 5:
        return None
    return _board(company, board_slug(path[4]), f"{parsed.scheme}://{parsed.netloc}/{'/'.join(API_PATH)}/")


def page_board(company: str, url: str) -> Board | None:
    """A board page, apply.workable.com/<slug>[/…]."""
    parsed = http_url(url)
    path = [p for p in parsed.path.split("/") if p] if parsed else []
    if not path or parsed.hostname != BOARD_HOST:
        return None
    slug = board_slug(path[0])
    return None if slug in NOT_BOARDS else _board(company, slug, API)


def _board(company: str, slug: str | None, base: str) -> Board | None:
    if slug is None:
        return None
    return Board(ats=ATS, slug=slug, company=company or slug, api_url=base + quote(slug, safe="") + DETAILS)


@dataclass(frozen=True)
class _Entry:
    shortcode: str
    title: str
    url: str
    description: str
    places: list[str]


def company(board: Board, get: Get) -> Company:
    """The account the widget API serves, without its job details."""
    data = json.loads(get(board.api_url.removesuffix(DETAILS)))
    return named_company(data.get("name") if isinstance(data, dict) else None)


def read(board: Board, get: Get) -> tuple[Posting, ...]:
    return parse(board, json.loads(get(board.api_url)))


def parse(board: Board, data) -> tuple[Posting, ...]:
    """One posting per shortcode. Repeats must agree on title and description (Workable shares one description
    across a job's locations); a repeat that doesn't fails the board rather than hide one of two ads."""
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise BoardError("response has no `jobs` list")
    firsts: dict[str, _Entry] = {}  # by shortcode, in first-seen order
    places: dict[str, list[str]] = {}
    for index, job in enumerate(jobs):
        entry = _entry(index, job)
        first = firsts.setdefault(entry.shortcode, entry)
        if (entry.title, entry.description) != (first.title, first.description):
            raise BoardError(f"job {index}: shortcode {entry.shortcode} repeats with a different title or description")
        known = places.setdefault(entry.shortcode, [])
        known += [p for p in dict.fromkeys(entry.places) if p not in known]
    return tuple(Posting(key=f"{board.id}:{code}", ats=ATS, board=board.id, company=board.company, title=entry.title,
                         location=" / ".join(places[code]), url=entry.url, description=entry.description)
                 for code, entry in firsts.items())


def _entry(index: int, job) -> _Entry:
    if not isinstance(job, dict):
        raise BoardError(f"job {index} is not an object")
    return _Entry(
        shortcode=text_field(job, "shortcode", index, required=True),
        title=text_field(job, "title", index, required=True).strip(),
        url=text_field(job, "url", index) or "",
        description=html_to_text(text_field(job, "description", index) or ""),
        places=_places(index, job),
    )


def _places(index: int, job: dict) -> list[str]:
    """`city, region, country` per location record, else from the job's own fields; `Remote` when the job is."""
    records = job.get("locations")
    records = [] if records is None else records
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise BoardError(f"job {index}: `locations` is not a list of objects")
    remote = job.get("telecommuting")
    if remote is not None and not isinstance(remote, bool):
        raise BoardError(f"job {index}: `telecommuting` is not a boolean")
    named = [[text_field(r, k, index) for k in ("city", "region", "country")] for r in records] \
        or [[text_field(job, k, index) for k in ("city", "state", "country")]]
    places = [", ".join(part.strip() for part in parts if part and part.strip()) for parts in named]
    return [p for p in places if p] + (["Remote"] if remote else [])
