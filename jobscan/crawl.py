"""Common Crawl index reader: the newest crawls, and the URLs a crawl captured on one host, read straight from the
published index files by HTTP range requests (the CC index server is not used).

A crawl's `cluster.idx` is sorted by SURT key, one line per block of CDX lines: the key of the block's first line
and the gzip member holding the block (file, offset, length). One host's captures are a contiguous key range, so
a binary search over `cluster.idx` finds them; the block before the first entry inside the range is read too,
because it can start before the range and run into it. Any response or record that isn't what the format promises
raises `CrawlError`: a crawl reads completely or not at all."""
import gzip
import http.client
import json
import re
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass

BASE = "https://data.commoncrawl.org/"
CRAWL_ID = re.compile(r"\bCC-MAIN-\d{4}-\d{2}\b")
WINDOW = 65536  # bytes per cluster.idx read; far longer than one index line
MAX_BLOCK = 8 << 20  # a CDX block is ~250 KB compressed
MAX_PAGE = 4 << 20
TIMEOUT = 60
RETRY_PAUSES = (5, 20, 60)  # Common Crawl asks clients to back off on 503
RETRYABLE = {429, 500, 502, 503, 504}
HEADERS = {"User-Agent": "jobscan (+https://github.com/holarchic/job-board-scanner)"}
CONTENT_RANGE = re.compile(r"bytes (\d{1,15})-(\d{1,15})/(\d{1,15})")
# cluster.idx line: "<SURT key> <14-digit timestamp>\t<cdx file>\t<offset>\t<length>\t<sequence>"
CDX_HEADER = re.compile(rb"\S+ \d{14} ")  # SURT key and timestamp before the JSON record
INDEX_LINE = re.compile(r"(\S+) \d{14}\t(cdx-\d{5}\.gz)\t(\d{1,15})\t(\d{1,9})\t\d{1,9}")


class CrawlError(Exception):
    """A crawl's index couldn't be read completely."""


@dataclass(frozen=True)
class Block:
    """One gzip member of CDX lines in a crawl's index files."""
    file: str
    offset: int
    length: int


@dataclass(frozen=True)
class Response:
    body: bytes
    headers: http.client.HTTPMessage
    status: int


@dataclass(frozen=True)
class IndexEntry:
    key: bytes  # SURT key of the block's first CDX line
    block: Block


def latest_crawls(n: int, base: str = BASE) -> list[str]:
    """The n newest crawl ids that have index files."""
    try:
        page = _get(base + "cc-index/collections/index.html", limit=MAX_PAGE).decode()
    except UnicodeDecodeError:
        raise CrawlError("crawl list is not valid UTF-8") from None
    crawls = sorted(set(CRAWL_ID.findall(page)), reverse=True)
    if len(crawls) < n:
        raise CrawlError(f"crawl list names {len(crawls)} crawls, need {n}")
    return crawls[:n]


def captures(crawl: str, host: str, base: str = BASE) -> list[str]:
    """URLs on `host` (`*.example.com`: on any subdomain of example.com) that `crawl` fetched with status 200 as
    text/html."""
    prefix = surt_prefix(host).encode()
    indexes = f"{base}cc-index/collections/{crawl}/indexes/"
    urls = []
    for block in _blocks(indexes + "cluster.idx", prefix):
        urls += _block_urls(indexes, block, prefix)
    if not urls:
        raise CrawlError(f"{crawl}: no captures of {host}")
    return urls


def surt_prefix(host: str) -> str:
    """`jobs.example.com` → `com,example,jobs)/`. `*.example.com` → `com,example,`, which every subdomain's key
    starts with, and neither the apex's (`com,example)/`) nor a look-alike's (`com,examplex,`)."""
    if host.startswith("*."):
        return ",".join(reversed(host[2:].lower().split("."))) + ","
    return ",".join(reversed(host.lower().split("."))) + ")/"


def _blocks(index_url: str, prefix: bytes) -> list[Block]:
    size = _size(index_url)
    lo, hi = 0, size  # the first whole line after lo sorts below prefix (or lo is 0); after hi, not below
    while hi - lo > WINDOW:
        mid = (lo + hi) // 2
        key = _first_key_after(index_url, mid, size)
        if key is not None and key < prefix:
            lo = mid
        else:
            hi = mid
    blocks, before = [], None
    for entry in _entries_from(index_url, lo, size):
        if entry.key < prefix:
            before = entry.block
            continue
        if before:
            blocks.append(before)
            before = None
        if not entry.key.startswith(prefix):
            return blocks
        blocks.append(entry.block)
    return blocks + ([before] if before else [])


def _first_key_after(index_url: str, pos: int, size: int) -> bytes | None:
    data = _range(index_url, pos, min(pos + WINDOW, size) - 1)
    lines = data.split(b"\n")
    whole = lines[1:-1] if pos else lines[:-1]
    return _parse_entry(whole[0]).key if whole else None


def _entries_from(index_url: str, pos: int, size: int) -> Iterator[IndexEntry]:
    """Every whole index line starting after pos (from pos itself when pos is 0)."""
    carry, skip_partial = b"", pos > 0
    while pos < size:
        end = min(pos + WINDOW, size)
        lines = (carry + _range(index_url, pos, end - 1)).split(b"\n")
        carry, pos = lines.pop(), end
        if skip_partial:
            lines, skip_partial = lines[1:], False
        for line in lines:
            yield _parse_entry(line)
    if carry:
        yield _parse_entry(carry)


def _parse_entry(line: bytes) -> IndexEntry:
    try:
        match = INDEX_LINE.fullmatch(line.decode("ascii"))
    except UnicodeDecodeError:
        match = None
    if not match or not 0 < int(match[4]) <= MAX_BLOCK:
        raise CrawlError("malformed cluster.idx line")
    return IndexEntry(key=match[1].encode(), block=Block(file=match[2], offset=int(match[3]), length=int(match[4])))


def _block_urls(indexes: str, block: Block, prefix: bytes) -> list[str]:
    where = f"{block.file}@{block.offset}"
    try:
        text = gzip.decompress(_range(indexes + block.file, block.offset, block.offset + block.length - 1))
    except (OSError, EOFError, zlib.error) as e:
        raise CrawlError(f"{where}: corrupt block ({type(e).__name__})") from None
    urls = []
    for line in text.split(b"\n"):
        if not line.startswith(prefix):
            continue
        header = CDX_HEADER.match(line)
        if not header:
            raise CrawlError(f"{where}: malformed CDX line")
        try:
            record = json.loads(line[header.end():])
            fields = [record[k] for k in ("url", "status", "mime")] if isinstance(record, dict) else None
            if fields is None or not all(isinstance(f, str) for f in fields):
                raise CrawlError(f"{where}: CDX record without a string url, status and mime")
            record["url"].encode()  # a lone surrogate from a JSON escape is not a URL
        except (KeyError, ValueError, RecursionError):
            raise CrawlError(f"{where}: malformed CDX line") from None
        if record.get("status") == "200" and record.get("mime") == "text/html":
            urls.append(record["url"])
    return urls


def _size(url: str) -> int:
    try:
        return int(_request(url, method="HEAD").headers["Content-Length"])
    except (TypeError, ValueError):
        raise CrawlError(f"{url}: no Content-Length") from None


def _range(url: str, start: int, end: int) -> bytes:
    """The bytes start to end. An answer that isn't exactly that range is asked again, as a dropped connection is: a
    connection closed early leaves a short body without an error (18 Oct: 1,027 of 335,556 bytes)."""
    def refusal(response: Response) -> str | None:
        body, status = response.body, response.status
        match = CONTENT_RANGE.fullmatch(response.headers.get("Content-Range") or "")
        if status != 206 or not match or (int(match[1]), int(match[2])) != (start, end) or int(match[3]) <= end \
                or len(body) != end - start + 1:
            return f"range {start}-{end} answered {status} with {len(body)} bytes"
        return None

    return _request(url, headers={"Range": f"bytes={start}-{end}"}, limit=end - start + 1, refuse=refusal).body


def _get(url: str, limit: int) -> bytes:
    response = _request(url, limit=limit)
    if response.status != 200:
        raise CrawlError(f"{url}: HTTP {response.status}")
    return response.body


def _request(url: str, method: str = "GET", headers: dict | None = None, limit: int = 0,
             refuse: Callable[[Response], str | None] | None = None) -> Response:
    """One HTTP exchange, retrying 429/5xx, network errors and an answer `refuse` gives a reason against. A body
    longer than `limit` is an error."""
    request = urllib.request.Request(url, headers=HEADERS | (headers or {}), method=method)
    for pause in (*RETRY_PAUSES, None):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                body = response.read(limit + 1) if method != "HEAD" else b""
                if len(body) > limit:
                    raise CrawlError(f"{url}: response longer than {limit} bytes")
                answer = Response(body=body, headers=response.headers, status=response.status)
            reason = refuse(answer) if refuse is not None else None
            if reason is None:
                return answer
            if pause is None:
                raise CrawlError(f"{url}: {reason}")
        except urllib.error.HTTPError as e:
            e.close()
            if e.code not in RETRYABLE or pause is None:
                raise CrawlError(f"{url}: HTTP {e.code}") from None
        except (OSError, http.client.HTTPException) as e:
            if pause is None:
                raise CrawlError(f"{url}: network ({type(e).__name__})") from None
        time.sleep(pause)
