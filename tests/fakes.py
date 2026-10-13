"""Local servers standing in for Ashby, the model endpoint, Common Crawl's index files and mail servers in tests."""
import gzip
import json
import re
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeServer:
    """Serves `routes[path]` = (status, body). A callable body gets the parsed JSON request and returns
    (status, body). A bytes body is sent truncated: its Content-Length promises more than arrives.
    Every request is recorded in `requests` as (method, path, json_body)."""

    def __init__(self):
        self.routes = {}
        self.requests = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self._answer(None)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self._answer(json.loads(self.rfile.read(length) or b"null"))

            def _answer(self, payload):
                server.requests.append((self.command, self.path, payload))
                status, body = server.routes.get(self.path, (404, {"error": "not found"}))
                if callable(body):
                    status, body = body(payload)
                truncated = isinstance(body, bytes)
                raw = body if truncated else body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(status)
                for name, value in getattr(server, "extra_headers", {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw) + (100 if truncated else 0)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeFiles:
    """Serves `files[path]` = bytes with HEAD and single-range GET (206), like data.commoncrawl.org. `fail[path]`
    is a list of statuses answered (and consumed) before the file; `ignore_range` answers 200 with the whole file;
    `content_range(start, end, size)` builds the Content-Range header of every 206 instead.
    Every request is recorded in `requests` as (method, path, Range header)."""

    def __init__(self):
        self.files, self.fail, self.ignore_range, self.content_range, self.requests = {}, {}, False, None, []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self):
                self._answer(head=True)

            def do_GET(self):
                self._answer(head=False)

            def _answer(self, head):
                server.requests.append((self.command, self.path, self.headers.get("Range")))
                pending = server.fail.get(self.path)
                if pending:
                    self._send(pending.pop(0), b"", {})
                    return
                body = server.files.get(self.path)
                if body is None:
                    self._send(404, b"", {})
                    return
                wanted = re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers.get("Range") or "")
                if head or not wanted or server.ignore_range:
                    self._send(200, b"" if head else body, {"Content-Length": str(len(body))}, head=head)
                    return
                start, end = int(wanted[1]), min(int(wanted[2]), len(body) - 1)
                header = (server.content_range or (lambda s, e, n: f"bytes {s}-{e}/{n}"))(start, end, len(body))
                self._send(206, body[start:end + 1], {"Content-Range": header})

            def _send(self, status, body, headers, head=False):
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                if "Content-Length" not in headers:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if not head:
                    self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def cdx_line(url, status="200", mime="text/html", timestamp="20260901000000"):
    """A CDX line for `url` (scheme://host/path) as Common Crawl writes it: SURT key, timestamp, JSON."""
    host, _, path = url.split("://", 1)[1].partition("/")
    surt = ",".join(reversed(host.split("."))) + ")/" + path.lower()
    return f"{surt} {timestamp} " + json.dumps({"url": url, "mime": mime, "status": status})


def crawl_files(crawl, lines, per_block=2):
    """The index files of one crawl holding `lines` (sorted here), `per_block` CDX lines per gzip member, as
    {path: bytes} for FakeFiles: cluster.idx and cdx-00000.gz."""
    lines = sorted(lines)
    cdx, index = b"", []
    for n, start in enumerate(range(0, len(lines), per_block)):
        block = gzip.compress("".join(line + "\n" for line in lines[start:start + per_block]).encode())
        key = lines[start].rsplit(" {", 1)[0]
        index.append(f"{key}\tcdx-00000.gz\t{len(cdx)}\t{len(block)}\t{n + 1}\n")
        cdx += block
    base = f"/cc-index/collections/{crawl}/indexes/"
    return {base + "cluster.idx": "".join(index).encode(), base + "cdx-00000.gz": cdx}


def crawl_list(*crawls):
    rows = "".join(f'<tr><td><a href="/crawl-data/{c}/index.html">{c}</a></td></tr>\n' for c in crawls)
    return {"/cc-index/collections/index.html": f"<table>{rows}</table>".encode()}


def ashby_job(job_id, title, location="London", description=None, **extra):
    return {"id": job_id, "title": title, "location": location, "isListed": True,
            "jobUrl": f"https://jobs.ashbyhq.com/acme/{job_id}",
            "descriptionPlain": description if description is not None else f"{title} role. We sell software.",
            **extra}


def greenhouse_job(job_id, title, location="London", content=None, offices=()):
    """A Greenhouse Job Board API job as `?content=true` returns it: markup entity-escaped once."""
    markup = content if content is not None else f"&lt;p&gt;{title} role. We sell software.&lt;/p&gt;"
    return {"id": job_id, "title": title, "location": {"name": location},
            "offices": [{"name": o} for o in offices], "content": markup,
            "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{job_id}"}


def model_reply(tags: dict, cost: float = 0.0005):
    """An OpenRouter-shaped chat-completions response carrying `tags` as the message content."""
    return 200, {"choices": [{"message": {"content": json.dumps(tags)}}], "usage": {"cost": cost}}


def tags_quoting_the_posting(payload):
    """A well-behaved model: founding yes quoting the title, location_eligible yes quoting the location."""
    text = payload["messages"][0]["content"]
    title = text.split("<title>", 1)[1].split("</title>", 1)[0]
    location = text.split("<location>", 1)[1].split("</location>", 1)[0]
    return {"founding": {"value": "yes", "quote": title},
            "location_eligible": {"value": "yes", "quote": location},
            "segment": {"value": [], "quote": None},
            "language_required": {"value": "no", "quote": None},
            "tech_company": {"value": "unknown", "quote": None}}


def fake_smtp(calls: list, fail: dict | None = None):
    """A stand-in for smtplib.SMTP / SMTP_SSL recording each call as (name, args) in `calls`; `fail[name]` is raised
    by that call (`connect` is the constructor), read when the call happens."""
    fail = {} if fail is None else fail

    class FakeSMTP:
        def __init__(self, host, port, timeout=None, context=None):
            self._call("connect", host=host, port=port, timeout=timeout, context=context)

        def _call(self, name, **args):
            calls.append((name, args))
            if name in fail:
                raise fail[name]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            calls.append(("quit", {}))

        def starttls(self, context=None):
            self._call("starttls", context=context)

        def login(self, user, password):
            self._call("login", user=user, password=password)

        def send_message(self, message):
            self._call("send", message=message)

    return FakeSMTP


class PlainSMTPServer:
    """A real SMTP conversation over plain TCP that never offers STARTTLS; every line received is kept in `lines`."""

    def __init__(self):
        self.lines = []
        server = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b"220 fake ESMTP\r\n")
                for raw in self.rfile:
                    line = raw.decode("ascii", "replace").rstrip("\r\n")
                    server.lines.append(line)
                    verb = line.split(" ")[0].upper()
                    if verb == "EHLO":
                        self.wfile.write(b"250-fake\r\n250 AUTH PLAIN LOGIN\r\n")
                    elif verb == "QUIT":
                        self.wfile.write(b"221 bye\r\n")
                        return
                    else:
                        self.wfile.write(b"250 ok\r\n")

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
