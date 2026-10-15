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
DATA_CENTRE = re.compile(r"wd\d+")
REGIONAL = re.compile(r"[a-z]{2}-[a-z]{2,3}", re.IGNORECASE)  # en-US, en-us, es-419: a locale, never a site
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


@dataclass
class _Job:
    posting_id: str
    title: str
    path: str
    text: str  # `locationsText`
    places: list[str]  # the matched values' names


def read(board: Board, get: Get, places: Places) -> tuple[Posting, ...]:
    """The unfiltered first page, then either every page of it (no geographic filter) or every page of each matched
    place value's query. No value passing `places` reads as an empty board."""
    first = _page(get(board.api_url, _body(0, {})))
    values = _place_values(first.facets)
    if not any(GEOGRAPHIC.search(v.parameter) for v in values):
        return _postings(board, [(row, None) for row in _query(board, get, {}, first)])
    found = []
    for value in values:
        if places(value.name):
            found += [(row, value.name) for row in _query(board, get, {value.parameter: [value.id]})]
    return _postings(board, found)


def _body(offset: int, facets: dict) -> bytes:
    return json.dumps({"limit": PAGE, "offset": offset, "searchText": "", "appliedFacets": facets}).encode()


def _query(board: Board, get: Get, facets: dict, first: _Page | None = None) -> list:
    """Every row one query answers, placeholders included. A total of exactly CAP may be a cap, so paging runs to a
    short page and needs at least CAP rows; any other total must equal the rows read. Drift checks, best effort: a
    page before the end coming back empty, an id twice, or a count failing those rules fails the board."""
    page = first or _page(get(board.api_url, _body(0, facets)))
    total, rows, ids = page.total, [], set()
    while True:
        for row in page.rows:
            path = row.get("externalPath") if isinstance(row, dict) else None
            if path is not None and path in ids:
                raise BoardError("list changed while paging: a job came back twice")
            ids.add(path)
        rows += page.rows
        more = len(page.rows) == PAGE if total == CAP else len(rows) < total
        if not more:
            break
        page = _page(get(board.api_url, _body(len(rows), facets)))
        if not page.rows and total != CAP:
            raise BoardError("list changed while paging: a page came back empty")
    short = len(rows) < CAP if total == CAP else len(rows) != total
    if short:
        raise BoardError("list changed while paging: the rows read don't match the total")
    return rows


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


def _place_values(facets: list) -> list[_Value]:
    """Every value of the geographic and remote-work filters, groups (`locationMainGroup`) flattened. Other filters
    (job family, time type) are ignored, malformed or not; a malformed place value fails the board, since skipping it
    could hide every job in that place."""
    values = []
    for facet in facets:
        parameter = facet.get("facetParameter") if isinstance(facet, dict) else None
        if not isinstance(parameter, str):
            continue
        listed = facet.get("values")
        place = GEOGRAPHIC.search(parameter) or ARRANGEMENT.search(parameter)
        if not isinstance(listed, list):
            if place:
                raise BoardError(f"filter {parameter}: no `values` list")
            continue
        nested = [v for v in listed if isinstance(v, dict) and "facetParameter" in v]
        values += _place_values(nested)
        if not place:
            continue
        for value in listed:
            if value in nested:
                continue
            if not isinstance(value, dict) or not all(isinstance(value.get(k), str) for k in ("id", "descriptor")):
                raise BoardError(f"filter {parameter}: a value has no `id` or `descriptor`")
            values.append(_Value(parameter=parameter, id=value["id"], name=value["descriptor"]))
    return values


def _postings(board: Board, found: list[tuple[object, str | None]]) -> tuple[Posting, ...]:
    """Rows merged by posting id, each job with every place name it matched. Placeholder rows, with neither a title
    nor a link (one in Autodesk's 395), are skipped: no title filter could match them."""
    jobs: dict[str, _Job] = {}
    for index, (row, place) in enumerate(found):
        job = _job(index, row)
        if job is None:
            continue
        merged = jobs.setdefault(job.posting_id, job)
        if merged.title != job.title:
            raise BoardError(f"job {job.posting_id}: its rows disagree on the title")
        if place is not None and place not in merged.places:
            merged.places.append(place)
    return tuple(_posting(board, job) for job in jobs.values())


def _job(index: int, row) -> _Job | None:
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
    return _Job(posting_id=posting_id, title=title, path=path, text="" if SEVERAL.fullmatch(text) else text,
                places=[])


def _posting(board: Board, job: _Job) -> Posting:
    tenant, dc, site = board.slug.split(".", 2)
    places = list(dict.fromkeys(p for p in (job.text, *job.places) if p))
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
