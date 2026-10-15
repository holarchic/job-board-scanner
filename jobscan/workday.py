"""Workday adapter: board URLs → the site's list API, the list read through the site's place filters → postings, and
one job's detail → its description.

A site is <tenant>.<data centre>.myworkdayjobs.com/<site>. Its list API is POST-only, 20 jobs a page, and returns the
site's filters (facets) with each answer. Read whole, every site is ~33,000 requests a run, and the search box's
boolean syntax differs by tenant (`London OR EMEA` finds 1 job where `London` finds 31), so a site with geographic
filters is read through them: one query per geographic or remote-work value whose name passes the profile's place
test, each job carrying the names it matched (a job in several places lists only `N Locations`). A site without a
geographic filter is read whole. The list carries no descriptions, so `describe` fetches one for each posting the
profile matched."""
import json
import re
from dataclasses import dataclass
from urllib.parse import quote

from jobscan.payload import board_slug, detail_text, html_to_text, http_url, named_company, text_field
from jobscan.records import Board, BoardError, Company, Get, Places, Posting

ATS = "workday"
DISPLAY_NAME = "Workday"
DOMAIN = "myworkdayjobs.com"
CRAWL_HOSTS = (f"*.{DOMAIN}",)
SITE = "https://{tenant}.{dc}." + DOMAIN  # run tests point it at their fake server
API = "/wday/cxs/{tenant}/{site}"
LIST = "/jobs"
PAGE = 20  # the API's maximum: 21 is refused
CAP = 2000  # a total some sites report when they hold more (pnc); others report theirs (PwC: 3,864)
MAX_ROWS = 10 * CAP  # a capped query that never ends a page short fails here
DATA_CENTRE = re.compile(r"wd\d+")
REGIONAL = re.compile(r"[a-z]{2}-(?:[a-z]{2}|\d{3})", re.IGNORECASE)  # en-US, en-us, es-419: a locale, never a site
LANGUAGE = re.compile(r"[a-z]{2}", re.IGNORECASE)  # `es`: a locale before a site, or a site (`WM`)
NOT_SITES = frozenset({"wday", "job"})  # the API, and a job page with its site missing
GEOGRAPHIC = re.compile(r"location|country|city|geograph|region|state|province", re.IGNORECASE)
ARRANGEMENT = re.compile(r"remote", re.IGNORECASE)  # `remoteType`: queried when it matches, but places nothing
SEVERAL = re.compile(r"\d+ Locations")


# A slug is `<tenant>.<data centre>.<site>`, lower case: the tenant comes from the host name, and the site is
# case-insensitive (`Ext` = `ext`). Neither tenant nor data centre holds a dot, so the slug splits back into the three.


def api_board(company: str, url: str) -> Board | None:
    """None: the API lives on the site's own host, which page_board reads. (`fetch.board_for` tries every api_board
    first.)"""
    return None


def page_board(company: str, url: str) -> Board | None:
    """A site page, <tenant>.wd<N>.myworkdayjobs.com/[<locale>/]<site>[/…]."""
    parsed = http_url(url)
    labels = parsed.hostname.split(".") if parsed else []
    if len(labels) != 4 or ".".join(labels[2:]) != DOMAIN or not DATA_CENTRE.fullmatch(labels[1]):
        return None
    path = [p for p in parsed.path.split("/") if p]
    if path and (REGIONAL.fullmatch(path[0]) or (LANGUAGE.fullmatch(path[0]) and len(path) > 1
                                                 and path[1].lower() not in NOT_SITES)):
        path = path[1:]
    tenant, site = board_slug(labels[0], decoded=True), board_slug(path[0]) if path else None
    if tenant is None or site is None or site.lower() in NOT_SITES:
        return None
    slug = f"{tenant}.{labels[1]}.{site}".lower()
    return Board(ats=ATS, slug=slug, company=company or slug, api_url=_site_api(slug) + LIST)


def _site_api(slug: str) -> str:
    tenant, dc, site = slug.split(".", 2)
    return SITE.format(tenant=tenant, dc=dc) + API.format(tenant=quote(tenant, safe=""), site=quote(site, safe=""))


def company(board: Board, get: Get) -> Company:
    """The tenant: no Workday endpoint names the company (a job's `hiringOrganization` is a legal entity per job, such
    as "Autodesk S.A. (Spain)", and page titles vary)."""
    return named_company(board.slug.split(".", 1)[0])


@dataclass(frozen=True)
class _Page:
    total: int  # reported with the first page only; later pages say 0
    rows: list
    facets: list


@dataclass(frozen=True)
class _Value:
    parameter: str
    id: str
    name: str


@dataclass(frozen=True)
class _Job:
    posting_id: str
    title: str
    path: str
    text: str  # `locationsText`, "" for `N Locations`


@dataclass(frozen=True)
class _Found:
    job: _Job
    place: str | None  # the name of the filter value it came back under; None from an unfiltered read


def read(board: Board, get: Get, places: Places) -> tuple[Posting, ...]:
    """The unfiltered first page, then either every page of it (no geographic filter) or every page of each matched
    place value's query. No value passing `places` reads as an empty board."""
    first = _page(get(board.api_url, _body(0, {})))
    values = _place_values(first.facets)
    if not any(GEOGRAPHIC.search(v.parameter) for v in values):
        return _postings(board, [_Found(job, None) for job in _query(board, get, {}, first)])
    found = []
    for value in values:
        if places(value.name):
            found += [_Found(job, value.name) for job in _query(board, get, {value.parameter: [value.id]})]
    return _postings(board, found)


def _body(offset: int, facets: dict) -> bytes:
    return json.dumps({"limit": PAGE, "offset": offset, "searchText": "", "appliedFacets": facets}).encode()


def _query(board: Board, get: Get, facets: dict, first: _Page | None = None) -> list[_Job]:
    """Every job one query answers, its rows counted raw (placeholders included). A total of exactly CAP may be a cap,
    so paging runs to a short page and needs at least CAP rows, up to MAX_ROWS; any other total must equal the rows
    read. Drift checks, best effort: a page before the end coming back empty, a job twice, or a count failing those
    rules fails the board."""
    page = first or _page(get(board.api_url, _body(0, facets)))
    total, rows, jobs = page.total, 0, {}
    while True:
        for row in page.rows:
            job = _job(rows, row)
            rows += 1
            if job is not None and jobs.setdefault(job.posting_id, job) is not job:
                raise BoardError("list changed while paging: a job came back twice")
        more = len(page.rows) == PAGE if total == CAP else rows < total
        if not more:
            break
        if rows >= MAX_ROWS:
            raise BoardError(f"list doesn't end: {rows} rows read")
        page = _page(get(board.api_url, _body(rows, facets)))
        if not page.rows and total != CAP:
            raise BoardError("list changed while paging: a page came back empty")
    if rows < CAP if total == CAP else rows != total:
        raise BoardError("list changed while paging: the rows read don't match the total")
    return list(jobs.values())


def _page(raw: bytes) -> _Page:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise BoardError("response is not an object")
    total, rows, facets = data.get("total"), data.get("jobPostings"), data.get("facets", [])
    if type(total) is not int or total < 0 or not isinstance(rows, list) or not isinstance(facets, list):
        raise BoardError("response has no `total` count, `jobPostings` list or `facets` list")
    if len(rows) > PAGE:
        raise BoardError("a page holds more jobs than asked for")
    return _Page(total=total, rows=rows, facets=facets)


def _place_values(facets: list, in_place: bool = False) -> list[_Value]:
    """Every value of the geographic and remote-work filters, groups (`locationMainGroup`) flattened. Other filters
    (job family, time type) are ignored, malformed or not. Anything malformed in a place filter, or in a group under
    one, fails the board: skipping it could hide every job in that place, and closing them."""
    values = []
    for facet in facets:
        parameter = facet.get("facetParameter") if isinstance(facet, dict) else None
        listed = facet.get("values") if isinstance(facet, dict) else None
        place = in_place or (isinstance(parameter, str) and _is_place(parameter))
        if not isinstance(parameter, str) or not isinstance(listed, list):
            if place:
                raise BoardError(f"filter {parameter!r}: no `facetParameter` or `values` list")
            continue
        for value in listed:
            if isinstance(value, dict) and "facetParameter" in value:
                values += _place_values([value], in_place=place)
            elif place:
                values.append(_value(parameter, value))
    return values


def _is_place(parameter: str) -> bool:
    return bool(GEOGRAPHIC.search(parameter) or ARRANGEMENT.search(parameter))


def _value(parameter: str, value) -> _Value:
    """A place value with a non-empty, printable id and name: printable also rules out a lone surrogate from a JSON
    escape, which could not be stored."""
    fields = [value.get(key) if isinstance(value, dict) else None for key in ("id", "descriptor")]
    if not all(isinstance(f, str) and f.strip() and f.isprintable() for f in fields):
        raise BoardError(f"filter {parameter}: a value without a usable `id` or `descriptor`")
    return _Value(parameter=parameter, id=fields[0], name=fields[1].strip())


def _postings(board: Board, found: list[_Found]) -> tuple[Posting, ...]:
    """Jobs merged by posting id, each with every place name it matched; the same job under two names must agree on
    the title."""
    jobs: dict[str, _Job] = {}
    places: dict[str, list[str]] = {}
    for item in found:
        job = jobs.setdefault(item.job.posting_id, item.job)
        if job.title != item.job.title:
            raise BoardError(f"job {job.posting_id}: its rows disagree on the title")
        named = places.setdefault(job.posting_id, [])
        if item.place is not None and item.place not in named:
            named.append(item.place)
    return tuple(_posting(board, job, places[job.posting_id]) for job in jobs.values())


def _job(index: int, row) -> _Job | None:
    """A list row as a job; None for a placeholder, with neither a title nor a link (one in Autodesk's 395): no title
    filter could match it."""
    if not isinstance(row, dict):
        raise BoardError(f"job {index} is not an object")
    if row.get("title") is None and row.get("externalPath") is None:
        return None
    title = text_field(row, "title", index, required=True).strip()
    path = text_field(row, "externalPath", index, required=True).strip()
    posting_id = path.rsplit("/", 1)[-1]
    if not path.startswith("/job/") or not posting_id:
        raise BoardError(f"job {index}: `externalPath` is not a job path")
    text = (text_field(row, "locationsText", index) or "").strip()
    return _Job(posting_id=posting_id, title=title, path=path, text="" if SEVERAL.fullmatch(text) else text)


def _posting(board: Board, job: _Job, matched: list[str]) -> Posting:
    tenant, dc, site = board.slug.split(".", 2)
    places = list(dict.fromkeys(p for p in (job.text, *matched) if p))
    return Posting(key=f"{board.id}:{job.posting_id}", ats=ATS, board=board.id, company=board.company,
                   title=job.title, location=" / ".join(places),
                   url=SITE.format(tenant=tenant, dc=dc) + "/" + quote(site, safe="") + job.path, description="")


def describe(board: Board, posting: Posting, get: Get) -> str:
    """The job's detail, its description as text; a detail without one fails the posting: it would be tagged and
    delivered with no description, and never described again."""
    posting_id = posting.key.removeprefix(f"{board.id}:")
    data = json.loads(get(f"{board.api_url.removesuffix(LIST)}/job/{quote(posting_id, safe='')}"))
    info = data.get("jobPostingInfo") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        raise BoardError("detail has no `jobPostingInfo` object")
    text = html_to_text(detail_text(info, "jobDescription", "jobPostingInfo"))
    if not text:
        raise BoardError("detail has no text")
    return text
