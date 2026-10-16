"""`jobscan ui`: the set-up page, served on 127.0.0.1 to one person's browser. Every request must name this
server's host and port; every post must carry the server's one-time token and, when the browser sends one, the
page's own Origin, so another page in the browser can't drive it. One request at a time: two saves never
interleave. Secrets typed into the page are held in memory for this server run and never sent back."""
import hmac
import secrets
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from jobscan import init, mail, page, preview, tag, titles
from jobscan.config import (
    KEY_FILE,
    Config,
    ConfigError,
    EmailConfig,
    load_config,
    load_email,
    load_profile,
)
from jobscan.lock import RunLocked

READ_TIMEOUT = 10  # seconds a connection may take to send its request: a browser's idle spare socket can't stall
LARGEST_POST = 256 * 1024
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                               "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    # strict-origin keeps the token's query out of any Referer, while the page's own posts still carry its Origin
    # (no-referrer would make them `Origin: null`).
    "Referrer-Policy": "strict-origin",
    "Cache-Control": "no-store",
}


@dataclass
class Drafts:
    """Secrets typed on the page, kept for this server run so a reload doesn't lose them; never rendered."""
    key: str = ""
    password: str = ""


class Site:
    """The page's one owner of state: the install, the token and the drafts. Each method answers one request."""

    def __init__(self, home: Path, token: str):
        self.home, self.token, self.drafts = home, token, Drafts()

    def show(self) -> str:
        if self._fresh():
            return page.render(self._view(page.Form(tags=frozenset(tag.builtin_names()))))
        try:
            form = page.form_from_profile(load_profile(self.home), load_email(self.home))
        except ConfigError as e:
            return page.render(self._view(page.Form(), errors={"": f"Your install doesn't load: {e}"}))
        return page.render(self._view(form))

    def post(self, fields: dict[str, list[str]]) -> str:
        form = page.read(fields)
        if form.key:
            self.drafts.key = form.key
        if form.password:
            self.drafts.password = form.password
        profile, errors = page.to_profile(form)
        action = (fields.get("action") or [""])[0]
        if profile is None:
            return page.render(self._view(form, errors=errors))
        if action == "suggest":
            return self._suggest(form, profile)
        if action == "preview":
            return self._preview(form, profile)
        return self._save(form, profile)

    def _suggest(self, form: page.Form, profile) -> str:
        try:
            config = self._config()
            outcome = titles.for_home(self.home, config, profile)
        except (ConfigError, titles.TitlesError) as e:
            return page.render(self._view(form, errors={"": f"No suggestions: {e}"}))
        except RunLocked:
            return page.render(self._view(form, errors={"": "A run is using the database; try once it's done."}))
        return page.render(self._view(replace(form, add_titles=(), add_excludes=()), suggestions=outcome))

    def _preview(self, form: page.Form, profile) -> str:
        if self._fresh():
            return page.render(self._view(form, previewed=True))
        try:
            result = preview.for_home(self.home, load_config(self.home), profile)
        except ConfigError as e:
            return page.render(self._view(form, errors={"": f"No preview: {e}"}))
        except RunLocked:
            return page.render(self._view(form, errors={"": "A run is using the database; try once it's done."}))
        return page.render(self._view(form, preview=result, previewed=True))

    def _save(self, form: page.Form, profile) -> str:
        notices, errors = [], {}
        try:
            if self._fresh():
                key = self.drafts.key
                if not key:
                    return page.render(self._view(form, errors={"key": "Your OpenRouter key is needed."}))
                init.init_from(self.home, init.Answers(
                    name=profile.name, titles=list(profile.titles), exclude_titles=list(profile.exclude_titles),
                    locations=list(profile.locations), eligible_where=profile.eligible_where,
                    tags=list(profile.tags), key=key, custom_tags=profile.custom_tags))
                notices.append("Saved your search, the settings and your key.")
            else:
                init.save_profile(self.home, profile)
                notices.append("Saved your search (the previous file is kept as .toml.prev).")
                if self.drafts.key:
                    init.save_key(self.home, self.drafts.key)
                    notices.append("Saved your new key.")
        except ConfigError as e:
            return page.render(self._view(form, errors={"": f"Not saved: {e}"}))
        email_notice, email_error = self._save_email(form, profile.name)
        notices += [email_notice] if email_notice else []
        if email_error:
            errors["email"] = email_error
        else:
            self.drafts = Drafts()
        notices.append(self._loads())
        return page.render(self._view(page.form_from_profile(profile, self._saved_email()) if not errors else form,
                                      errors=errors, notices=tuple(notices)))

    def _save_email(self, form: page.Form, search: str) -> tuple[str, str]:
        """(notice, error): one of them, or neither when nothing changed."""
        saved = self._saved_email()
        if form.email_off:
            return ("Email turned off." if init.remove_email(self.home) else "", "")
        if not form.email_to:
            return ("", "") if saved is None else ("", "To turn email off, tick the box; the address is needed.")
        try:
            port = int(form.email_port) if form.email_port else None
        except ValueError:
            return "", "The port is a number: 465 or 587."
        password = self.drafts.password or (saved.password if saved else "")
        if not password:
            return "", "Your app password is needed to send email."
        try:
            email = init.email_config(form.email_to, form.email_from or None, form.email_host or None, port, password)
        except ConfigError as e:
            return "", str(e)
        if email == saved:
            return "", ""
        try:
            init.save_email(self.home, init.email_from(email, search))
        except mail.MailError as e:
            return "", f"The test email wasn't sent ({e}); email settings not saved."
        except ConfigError as e:
            return "", str(e)
        return f"Sent a test email to {email.to} and saved the email settings.", ""

    def _loads(self) -> str:
        try:
            load_config(self.home)
            load_profile(self.home)
            load_email(self.home)
        except ConfigError as e:
            return f"Saved, but the install doesn't load: {e}"
        return "The install loads as a run would: `jobscan run` can start."

    def _config(self) -> Config:
        """The model to ask: the install's, or on a fresh home the one `init` will write, with any key typed here."""
        if self._fresh():
            if not self.drafts.key:
                raise ConfigError("type your OpenRouter key first")
            return Config(home=self.home, boards_file=None, model=init.default_model(), api_key=self.drafts.key)
        config = load_config(self.home)
        return replace(config, api_key=self.drafts.key) if self.drafts.key and config.api_key else config

    def _saved_email(self) -> EmailConfig | None:
        try:
            return None if self._fresh() else load_email(self.home)
        except ConfigError:
            return None

    def _fresh(self) -> bool:
        return not (self.home / "config.toml").exists()

    def _view(self, form: page.Form, **parts) -> page.View:
        fresh = self._fresh()
        key_saved = not fresh and (self.home / KEY_FILE).exists()
        return page.View(form=form, token=self.token, fresh=fresh, key_held=bool(self.drafts.key) or key_saved,
                         password_held=bool(self.drafts.password), email=self._saved_email(), **parts)


class Server(HTTPServer):
    """127.0.0.1 only, one request at a time."""

    def __init__(self, home: Path, port: int):
        super().__init__(("127.0.0.1", port), _Handler)
        self.site = Site(home, secrets.token_urlsafe(32))
        self.hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/?token={self.site.token}"


class _Handler(BaseHTTPRequestHandler):
    timeout = READ_TIMEOUT
    server: Server

    def do_GET(self):
        if not self._allowed():
            return
        token = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
        if not hmac.compare_digest(token, self.server.site.token):
            return self._answer(403, "<p>Open the link <code>jobscan ui</code> printed.</p>")
        self._answer(200, self.server.site.show())

    def do_POST(self):
        if not self._allowed():
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin.removeprefix("http://") not in self.server.hosts:
            return self._answer(403, "<p>Refused: the form came from another page.</p>")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._answer(411, "<p>Refused: no length.</p>")
        if not 0 <= length <= LARGEST_POST:
            return self._answer(413, "<p>Refused: too large.</p>")
        fields = parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)
        if not hmac.compare_digest((fields.get("token") or [""])[0], self.server.site.token):
            return self._answer(403, "<p>Open the link <code>jobscan ui</code> printed.</p>")
        self._answer(200, self.server.site.post(fields))

    def _allowed(self) -> bool:
        if self.headers.get("Host") not in self.server.hosts:
            self._answer(403, "<p>Refused: wrong host.</p>")
            return False
        if urlsplit(self.path).path != "/":
            self._answer(404, "<p>Not here.</p>")
            return False
        return True

    def _answer(self, status: int, body: str) -> None:
        data = body.encode()
        self.send_response(status)
        for name, value in {**HEADERS, "Content-Type": "text/html; charset=utf-8",
                            "Content-Length": str(len(data))}.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        """Silent: the request line holds the token."""
