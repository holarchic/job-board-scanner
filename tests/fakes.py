"""Local servers standing in for Ashby, the model endpoint, Common Crawl's index files and mail servers in tests."""
import gzip
import json
import re
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from xml.sax.saxutils import escape


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


def workable_job(shortcode, title, city="London", country="United Kingdom", description=None, **extra):
    """A Workable widget-API job as `?details=true` returns it: one entry per location."""
    return {"shortcode": shortcode, "title": title, "url": f"https://apply.workable.com/j/{shortcode}",
            "description": description if description is not None else f"<p>{title} role. We sell software.</p>",
            "telecommuting": False, "city": city, "state": "", "country": country,
            "locations": [{"city": city, "region": None, "country": country, "countryCode": "GB", "hidden": False}],
            **extra}


def gem_job(job_id, title, location="London", offices=(), content_plain=None, **extra):
    """A Gem job board API job: plain-text and HTML descriptions, a location and offices with their own."""
    text = content_plain if content_plain is not None else f"{title} role. We sell software."
    return {"id": job_id, "title": title, "absolute_url": f"https://jobs.gem.com/acme/{job_id}",
            "content_plain": text, "content": f"<p>{text}</p>", "location": {"name": location},
            "offices": [{"name": name, "location": {"name": place}} for name, place in offices],
            "location_type": "in_office", **extra}


def teamtailor_feed(*items):
    """A Teamtailor jobs.rss feed holding `items` (from teamtailor_item)."""
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0" xmlns:tt="https://teamtailor.com/locations">'
            f"<channel><title>Acme</title><link>https://acme.teamtailor.com/jobs</link>{''.join(items)}</channel></rss>")


def teamtailor_item(guid, title, places=(("London", "United Kingdom"),), remote="none", description=None):
    """One feed item: `places` are (city, country[, name]); the description is HTML, escaped into the XML."""
    text = description if description is not None else f"<p>{title} role. We sell software.</p>"
    locations = "".join(f"<tt:location><tt:name>{escape((*rest, f'{city} office')[0])}</tt:name>"
                        f"<tt:city>{escape(city)}</tt:city><tt:country>{escape(country)}</tt:country></tt:location>"
                        for city, country, *rest in places)
    return (f"<item><title>{escape(title)}</title><description>{escape(text)}</description>"
            f"<link>https://acme.teamtailor.com/jobs/{escape(guid)}-job</link>"
            f"<remoteStatus>{escape(remote)}</remoteStatus><guid>{escape(guid)}</guid>"
            f"<tt:locations>{locations}</tt:locations></item>")


def personio_feed(*positions):
    """A Personio /xml feed holding `positions` (from personio_position)."""
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<workzag-jobs>{"".join(positions)}</workzag-jobs>'


def personio_position(job_id, name, office="London", additional=(), sections=None):
    """One position: `sections` are (heading, HTML) pairs, the HTML escaped into the XML."""
    sections = sections if sections is not None else [("Your role", f"<p>{name} role. We sell software.</p>")]
    extra = "".join(f"<office>{escape(o)}</office>" for o in additional)
    descriptions = "".join(f"<jobDescription><name>{escape(h)}</name><value>{escape(v)}</value></jobDescription>"
                           for h, v in sections)
    return (f"<position><id>{escape(job_id)}</id><subcompany>Acme GmbH</subcompany><office>{escape(office)}</office>"
            f"<additionalOffices>{extra}</additionalOffices><name>{escape(name)}</name>"
            f"<jobDescriptions>{descriptions}</jobDescriptions><createdAt>2026-10-01T09:00:00+00:00</createdAt>"
            "</position>")



def smartrecruiters_job(job_id, title, location="London, , United Kingdom", remote=False):
    """A SmartRecruiters postings-list entry: no description, and `fullLocation` with an empty region."""
    return {"id": job_id, "name": title, "uuid": f"uuid-{job_id}", "company": {"identifier": "Acme", "name": "Acme"},
            "location": {"city": location.split(",")[0], "country": "gb", "remote": remote, "fullLocation": location},
            "ref": f"https://api.smartrecruiters.com/v1/companies/Acme/postings/{job_id}"}


def smartrecruiters_page(jobs, offset=0, total=None):
    """One page of a SmartRecruiters postings list; `total` defaults to the jobs on this page."""
    return {"offset": offset, "limit": 100, "totalFound": len(jobs) if total is None else total, "content": list(jobs)}


def smartrecruiters_detail(title, sections=None):
    """A SmartRecruiters posting detail: `sections` are (key, title, HTML), in the ad's order; an ad always has a
    `jobDescription`."""
    sections = sections if sections is not None else [
        ("companyDescription", "Company Description", "<p>We sell software.</p>"),
        ("jobDescription", "Job Description", f"<p>{title} role.</p>")]
    return {"id": "1", "name": title, "jobAd": {"sections": {key: {"title": t, "text": h} for key, t, h in sections}}}


def rippling_item(job_id, title, place="London, United Kingdom", country="United Kingdom", workplace="ON_SITE"):
    """A Rippling jobs-list row: one location (a job with several comes as several rows), no description."""
    return {"id": job_id, "name": title, "url": f"https://ats.rippling.com/acme/jobs/{job_id}",
            "department": {"name": "Sales"}, "language": "en-US",
            "locations": [{"name": place, "country": country, "countryCode": "GB", "state": None, "stateCode": None,
                           "city": None, "workplaceType": workplace}]}


def rippling_page(items, page=0, total_items=None, total_pages=None):
    """One page of a Rippling jobs list; the totals default to this page being the only one."""
    return {"items": list(items), "page": page, "pageSize": 1000,
            "totalItems": len(items) if total_items is None else total_items,
            "totalPages": (1 if items else 0) if total_pages is None else total_pages}


def rippling_detail(company="<p>We sell software.</p>", role="<p>Sell it.</p>"):
    """A Rippling job detail: the company's and the role's descriptions, HTML."""
    return {"uuid": "1", "name": "Account Executive", "companyName": "Acme", "workLocations": ["London"],
            "description": {"company": company, "role": role}}

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


def fake_smtp(calls: list, fail: dict | None = None, *, tls: bool):
    """A stand-in for smtplib.SMTP_SSL (`tls`) or smtplib.SMTP, with the real constructor's signature, recording each
    call as (name, args) in `calls`; the constructor records as "SMTP_SSL" or "SMTP". `fail[name]` is raised by that
    call, read when the call happens."""
    fail = {} if fail is None else fail

    class FakeSMTP:
        def _call(self, name, **args):
            calls.append((name, args))
            if name in fail:
                raise fail[name]

        def quit(self):
            self._call("quit")

        def close(self):
            calls.append(("close", {}))

        def starttls(self, *, context=None):
            self._call("starttls", context=context)

        def login(self, user, password):
            self._call("login", user=user, password=password)

        def send_message(self, message):
            self._call("send", message=message)

    def tls_init(self, host="", port=0, local_hostname=None, *, timeout=None, source_address=None, context=None):
        self._call("SMTP_SSL", host=host, port=port, timeout=timeout, context=context)

    def plain_init(self, host="", port=0, local_hostname=None, timeout=None, source_address=None):
        self._call("SMTP", host=host, port=port, timeout=timeout)

    FakeSMTP.__init__ = tls_init if tls else plain_init
    return FakeSMTP


def patch_smtp(calls: list, fail: dict | None = None) -> list:
    """Patchers replacing both smtplib constructors with recording fakes; the caller starts and stops them."""
    return [mock.patch("smtplib.SMTP_SSL", fake_smtp(calls, fail, tls=True)),
            mock.patch("smtplib.SMTP", fake_smtp(calls, fail, tls=False))]


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
