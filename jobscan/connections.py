"""HTTP requests over connections kept open between them, at most one per thread, to the host it last asked. A
connection is kept only after a whole HTTP 200 its server didn't close, so no unread answer ever sits ahead of the next
one. Statuses are answers here: what counts as success, and what is tried again later, is the caller's."""
import http.client
import threading
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass

REDIRECTS = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 10


@dataclass(frozen=True)
class Answer:
    status: int
    headers: http.client.HTTPMessage
    body: bytes  # read whole for a 200; empty, and left unread, for any other status


@dataclass(frozen=True)
class _Origin:
    scheme: str
    host: str
    port: int | None


class Connections:
    """The kept connections of the threads sending through it. `close`, or leaving its `with`, closes them all; it
    belongs after every such thread has finished."""

    def __init__(self, timeout: float):
        self._timeout = timeout
        self._held: dict[int, tuple[_Origin, http.client.HTTPConnection]] = {}  # by thread
        self._lock = threading.Lock()

    def __enter__(self) -> "Connections":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            held, self._held = list(self._held.values()), {}
        for _, connection in held:
            connection.close()

    def send(self, url: str, body: bytes | None, headers: Mapping[str, str]) -> Answer:
        """A GET, or a POST of `body`. A GET's redirects to http(s) URLs are followed, up to MAX_REDIRECTS; any other
        redirect, or one past them, is the answer. A URL that isn't http(s) with a host raises ValueError."""
        for _ in range(MAX_REDIRECTS):
            answer = self._exchange(url, body, headers)
            target = _redirect(url, answer) if body is None else None
            if target is None:
                return answer
            url = target
        return self._exchange(url, body, headers)

    def _exchange(self, url: str, body: bytes | None, headers: Mapping[str, str]) -> Answer:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"not an http(s) URL: {url!r}")
        origin = _Origin(parts.scheme, parts.hostname, parts.port)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        method = "GET" if body is None else "POST"
        connection = self._take(origin)
        reused = connection is not None
        if connection is None:
            connection = self._open(origin)
        try:
            try:
                response = _ask(connection, method, target, body, headers)
            except ConnectionError:
                if not reused:
                    raise
                # The server dropped it while it waited, before reading this request: once more on a new one.
                connection.close()
                connection = self._open(origin)
                response = _ask(connection, method, target, body, headers)
            if response.status != 200:
                response.close()
                connection.close()
                return Answer(response.status, response.headers, b"")
            data = response.read()
        except BaseException:
            connection.close()
            raise
        if response.will_close:
            connection.close()
        else:
            with self._lock:
                self._held[threading.get_ident()] = (origin, connection)
        return Answer(200, response.headers, data)

    def _take(self, origin: _Origin) -> http.client.HTTPConnection | None:
        """The calling thread's kept connection when it is to `origin`; one to another host is closed."""
        with self._lock:
            held = self._held.pop(threading.get_ident(), None)
        if held is None:
            return None
        held_origin, connection = held
        if held_origin == origin:
            return connection
        connection.close()
        return None

    def _open(self, origin: _Origin) -> http.client.HTTPConnection:
        kind = http.client.HTTPSConnection if origin.scheme == "https" else http.client.HTTPConnection
        return kind(origin.host, origin.port, timeout=self._timeout)


def _ask(connection: http.client.HTTPConnection, method: str, target: str, body: bytes | None,
         headers: Mapping[str, str]) -> http.client.HTTPResponse:
    connection.request(method, target, body=body, headers=dict(headers))
    return connection.getresponse()


def _redirect(url: str, answer: Answer) -> str | None:
    """Where a redirect answer sends a GET, or None when it isn't one this client follows."""
    location = answer.headers.get("Location") if answer.status in REDIRECTS else None
    if not location:
        return None
    target = urllib.parse.urljoin(url, location)
    parts = urllib.parse.urlsplit(target)
    return target if parts.scheme in ("http", "https") and parts.hostname else None
