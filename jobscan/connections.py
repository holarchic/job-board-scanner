"""HTTP requests over connections kept open between them, at most one per thread, to the host it last asked. A
connection is kept only after a whole HTTP 200 its server didn't close, so no unread answer ever sits ahead of the next
one. Statuses are answers here: what counts as success, and what is tried again later, is the caller's."""
import http.client
import string
import threading
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass

from jobscan.payload import http_url

REDIRECTS = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 10


@dataclass(frozen=True)
class Answer:
    status: int
    headers: http.client.HTTPMessage
    body: bytes  # read whole for a 200; empty, and left unread, for any other status


@dataclass(frozen=True)
class _Origin:
    scheme: str
    host: str  # an IPv6 address without its brackets
    port: int  # the scheme's default when the URL names none


@dataclass(frozen=True)
class _Url:
    origin: _Origin
    target: str  # path and query, as the request line carries them
    whole: str  # what a redirect's Location is read against


@dataclass(frozen=True)
class _Kept:
    origin: _Origin
    connection: http.client.HTTPConnection


class Connections:
    """The kept connections of the threads sending through it. `close`, or leaving its `with`, closes them all; it
    belongs after every such thread has finished."""

    def __init__(self, timeout: float):
        self._timeout = timeout
        self._held: dict[int, _Kept] = {}  # by thread
        self._lock = threading.Lock()

    def __enter__(self) -> "Connections":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            held, self._held = list(self._held.values()), {}
        for kept in held:
            kept.connection.close()

    def send(self, url: str, body: bytes | None, headers: Mapping[str, str]) -> Answer:
        """A GET, or a POST of `body`. A GET's redirects to http(s) URLs are followed, up to MAX_REDIRECTS; any other
        redirect, or one past them, is the answer. A URL that isn't http(s) with a host and a valid port raises
        ValueError."""
        parsed = _parse(url)
        if parsed is None:
            raise ValueError(f"not an http(s) URL: {url!r}")
        for _ in range(MAX_REDIRECTS):
            answer = self._exchange(parsed, body, headers)
            target = _redirect(parsed, answer) if body is None else None
            if target is None:
                return answer
            parsed = target
        return self._exchange(parsed, body, headers)

    def _exchange(self, url: _Url, body: bytes | None, headers: Mapping[str, str]) -> Answer:
        method = "GET" if body is None else "POST"
        connection = self._take(url.origin)
        reused = connection is not None
        if connection is None:
            connection = self._open(url.origin)
        response = None
        try:
            try:
                response = _ask(connection, method, url.target, body, headers)
            except ConnectionError:
                if not reused:
                    raise
                # No answer came back (a reset while sending, or before the answer's headers were whole): the server
                # dropped the connection while it waited. Every request here only reads, so once more on a new one.
                connection.close()
                connection = self._open(url.origin)
                response = _ask(connection, method, url.target, body, headers)
            if response.status != 200:
                response.close()
                connection.close()
                return Answer(response.status, response.headers, b"")
            data = response.read()
        except BaseException:
            if response is not None:
                response.close()  # once the server says it closes, the response, not the connection, holds the socket
            connection.close()
            raise
        if response.will_close:
            connection.close()
        else:
            with self._lock:
                self._held[threading.get_ident()] = _Kept(url.origin, connection)
        return Answer(200, response.headers, data)

    def _take(self, origin: _Origin) -> http.client.HTTPConnection | None:
        """The calling thread's kept connection when it is to `origin`; one to another host is closed."""
        with self._lock:
            kept = self._held.pop(threading.get_ident(), None)
        if kept is None:
            return None
        if kept.origin == origin:
            return kept.connection
        kept.connection.close()
        return None

    def _open(self, origin: _Origin) -> http.client.HTTPConnection:
        kind = http.client.HTTPSConnection if origin.scheme == "https" else http.client.HTTPConnection
        return kind(origin.host, origin.port, timeout=self._timeout)


def _ask(connection: http.client.HTTPConnection, method: str, target: str, body: bytes | None,
         headers: Mapping[str, str]) -> http.client.HTTPResponse:
    connection.request(method, target, body=body, headers=dict(headers))
    return connection.getresponse()


def _parse(url: str) -> _Url | None:
    """The URL to send to, or None unless it is http(s) with a host and a valid port (`payload.http_url`'s rule)."""
    parts = http_url(url)
    if parts is None:
        return None
    default = http.client.HTTPS_PORT if parts.scheme == "https" else http.client.HTTP_PORT
    origin = _Origin(parts.scheme, parts.hostname, default if parts.port is None else parts.port)
    return _Url(origin, (parts.path or "/") + (f"?{parts.query}" if parts.query else ""), url)


def _redirect(url: _Url, answer: Answer) -> _Url | None:
    """Where a redirect answer sends a GET, or None when it isn't one this client follows. The Location's bytes, as
    http.client decodes headers (Latin-1), are percent-encoded where a request line can't carry them, as urllib does."""
    location = answer.headers.get("Location") if answer.status in REDIRECTS else None
    if not location:
        return None
    try:
        target = urllib.parse.urljoin(url.whole, urllib.parse.quote(location, encoding="iso-8859-1",
                                                                    safe=string.punctuation))
    except ValueError:  # an unclosed IPv6 bracket
        return None
    return _parse(target)
