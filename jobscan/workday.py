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
from urllib.parse import quote, unquote

from jobscan.payload import board_slug, detail_text, html_to_text, http_url, named_company, one_line, text_field
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
FALLBACK = "job/"  # a key's job part without a requisition: the link's last segment, which holds no `/`


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
    job_id: str  # the requisition, else FALLBACK + the link's last segment
    posting_id: str  # the link's last segment
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
        if total == CAP and rows >= MAX_ROWS:
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


def _place_values(facets: list, in_place_group: bool = False) -> list[_Value]:
    """Every value of the geographic and remote-work filters, groups (`locationMainGroup`) flattened; each filter is
    classed by its own parameter. Other filters (job family, time type) are ignored, malformed or not. A malformed
    place filter, or anything malformed directly in a place group, fails the board: skipping it could hide every job
    in that place, and close them."""
    values = []
    for facet in facets:
        parameter = facet.get("facetParameter") if isinstance(facet, dict) else None
        listed = facet.get("values") if isinstance(facet, dict) else None
        if not _usable(parameter) or not isinstance(listed, list):
            if in_place_group or (isinstance(parameter, str) and _is_place(parameter)):
                raise BoardError(f"filter {parameter!r}: no usable `facetParameter` or `values` list")
            continue
        place = _is_place(parameter)
        for value in listed:
            if isinstance(value, dict) and "facetParameter" in value:
                values += _place_values([value], in_place_group=place)
            elif place:
                values.append(_value(parameter, value))
    return values


def _is_place(parameter: str) -> bool:
    return bool(GEOGRAPHIC.search(parameter) or ARRANGEMENT.search(parameter))


def _usable(text) -> bool:
    """A non-empty, printable string: printable also rules out a lone surrogate from a JSON escape, which could not
    be stored."""
    return isinstance(text, str) and bool(text.strip()) and text.isprintable()


def _value(parameter: str, value) -> _Value:
    value_id = value.get("id") if isinstance(value, dict) else None
    descriptor = value.get("descriptor") if isinstance(value, dict) else None
    name = one_line(descriptor) if isinstance(descriptor, str) else ""  # Airbus: 'Istres (AD - Base 125\xa0) \xa0'
    if not _usable(value_id) or not name:
        raise BoardError(f"filter {parameter}: a value without a usable `id` or `descriptor`")
    return _Value(parameter=parameter, id=value_id, name=name)


def _postings(board: Board, found: list[_Found]) -> tuple[Posting, ...]:
    """Jobs merged by job id, each with every row's location text and every place name it matched, so a requisition
    listed per city keeps them all. One link under two filter values must agree on its title and requisition; two
    links of one requisition keep the first's title and link."""
    jobs: dict[str, _Job] = {}
    links: dict[str, _Job] = {}  # each link's first row
    places: dict[str, list[str]] = {}
    for item in found:
        first = links.setdefault(item.job.posting_id, item.job)
        if (first.title, first.job_id) != (item.job.title, item.job.job_id):
            raise BoardError(f"job {item.job.posting_id}: its rows disagree on the title or requisition")
        job = jobs.setdefault(item.job.job_id, item.job)
        named = places.setdefault(job.job_id, [])
        for place in (item.job.text, item.place):
            if place and place not in named:
                named.append(place)
    return tuple(_posting(board, job, places[job.job_id]) for job in jobs.values())


def _job(index: int, row) -> _Job | None:
    """A list row as a job; None for a placeholder, with neither a title nor a link (one in Autodesk's 395): no title
    filter could match it."""
    if not isinstance(row, dict):
        raise BoardError(f"job {index} is not an object")
    if row.get("title") is None and row.get("externalPath") is None:
        return None
    title = one_line(text_field(row, "title", index, required=True))
    if not title:
        raise BoardError(f"job {index}: `title` holds no visible text")
    path = text_field(row, "externalPath", index, required=True).strip()
    posting_id = path.rsplit("/", 1)[-1]
    if not path.startswith("/job/") or not posting_id:
        raise BoardError(f"job {index}: `externalPath` is not a job path")
    text = one_line(text_field(row, "locationsText", index) or "")
    return _Job(job_id=_requisition(posting_id, row.get("bulletFields")) or FALLBACK + posting_id,
                posting_id=posting_id, title=title, path=path, text="" if SEVERAL.fullmatch(text) else text)


def _requisition(posting_id: str, fields) -> str | None:
    """The requisition id: the longest `bulletFields` string the link's last segment ends with as `_<it>` or
    `_<it>-<digits>` (`…_JR350367-2`). Read from the field, never the link alone: requisitions hold `_` and `-<digits>`
    themselves (`R26_0000003039`, `R-20260930-0003`). On 14,260 rows of 250 sites, 96% have one and none two."""
    if not isinstance(fields, list):
        return None
    named = [f for f in fields if _usable(f) and re.search("_" + re.escape(f) + r"(?:-\d+)?$", posting_id)]
    return max(named, key=len) if named else None


def _posting(board: Board, job: _Job, places: list[str]) -> Posting:
    tenant, dc, site = board.slug.split(".", 2)
    return Posting(key=f"{board.id}:{job.job_id}", ats=ATS, board=board.id, company=board.company,
                   title=job.title, location=" / ".join(places),
                   url=SITE.format(tenant=tenant, dc=dc) + "/" + quote(site, safe="") + job.path, description="")


def same_job(key: str) -> str:
    """The key every posting of one job shares across a tenant's sites (`workday:<tenant>:<requisition>`): a company
    lists one job on each of its sites, under one requisition. A posting without a requisition is its own job."""
    ats, slug, job_id = key.split(":", 2)
    return key if job_id.startswith(FALLBACK) else f"{ats}:{slug.split('.', 1)[0]}:{job_id}"


def describe(board: Board, posting: Posting, get: Get) -> str:
    """The job's detail, its description as text; a detail without one fails the posting: it would be tagged and
    delivered with no description, and never described again."""
    posting_id = unquote(posting.url.rsplit("/", 1)[-1])  # the link's last segment; the key holds the requisition
    data = json.loads(get(f"{board.api_url.removesuffix(LIST)}/job/{quote(posting_id, safe='')}"))
    info = data.get("jobPostingInfo") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        raise BoardError("detail has no `jobPostingInfo` object")
    text = html_to_text(detail_text(info, "jobDescription", "jobPostingInfo"))
    if not text:
        raise BoardError("detail has no text")
    return text
