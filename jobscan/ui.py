"""`jobscan ui`: the set-up page, served on 127.0.0.1 to one person's browser. Every request must name this
server's host and port; every post must carry the server's one-time token and, when the browser sends one, the
page's own Origin, so another page in the browser can't drive it. One request at a time: two saves never
interleave. Secrets typed into the page are held in memory for this server run and never sent back."""
import hmac
import os
import secrets
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import MappingProxyType
from urllib.parse import parse_qs, urlsplit

from jobscan import init, install, mail, page, preview, tag, titles
from jobscan.config import (
    KEY_FILE,
    Config,
    ConfigError,
    EmailConfig,
    Profile,
    load_config,
    load_email,
    load_profile,
    usable_key,
)
from jobscan.lock import RunLocked

READ_TIMEOUT = 10  # seconds a connection may take to send its request: a browser's idle spare socket can't stall
LARGEST_POST = 256 * 1024
HEADERS = MappingProxyType({
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                               "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    # strict-origin keeps the token's query out of any Referer, while the page's own posts still carry its Origin
    # (no-referrer would make them `Origin: null`).
    "Referrer-Policy": "strict-origin",
    "Cache-Control": "no-store",
})


@dataclass
class Drafts:
    """Secrets typed on the page, kept for this server run so a reload doesn't lose them; never rendered."""
    key: str = ""
    password: str = ""


@dataclass(frozen=True)
class Done:
    """One section of a save: what was done, or why not; neither when nothing changed."""
    notice: str = ""
    error: str = ""


class Site:
    """The page's one owner of state: the install, the token and the drafts. Each method answers one request."""

    def __init__(self, home: Path, token: str):
        self.home, self.token, self.drafts = home, token, Drafts()

    def show(self) -> str:
        if self._fresh():
            return page.render(self._view(page.Form(tags=frozenset(tag.builtin_names()))))
        try:
            profile = load_profile(self.home)
        except ConfigError as e:
            return page.render(self._view(page.Form(), errors={"": f"Your install doesn't load: {e}"}))
        if len(profile.custom_tags) >= page.MOST_CUSTOM:
            return page.render(self._view(page.Form(), errors={"": f"Your profile has more custom tags than this "
                                                                   f"page shows ({page.MOST_CUSTOM - 1}): edit it "
                                                                   "in its file."}))
        return page.render(self._view(page.form_from_profile(profile, self._prefill_email())))

    def post(self, fields: dict[str, list[str]]) -> str:
        form = page.read(fields)
        if form.key:
            self.drafts.key = form.key
        if form.password:
            self.drafts.password = form.password
        action = (fields.get("action") or [""])[0]
        if action not in ("save", "preview", "suggest"):
            return page.render(self._view(page.with_ticked(form), errors={"": "Press one of the buttons."}))
        checked = page.to_profile(form)
        form = page.with_ticked(form)  # ticked suggestions stay in the lists whatever happens next
        if checked.profile is None:
            return page.render(self._view(form, errors=checked.errors))
        if action == "suggest":
            return self._suggest(form, checked.profile)
        if action == "preview":
            return self._preview(form, checked.profile)
        return self._save(form, checked.profile)

    def _suggest(self, form: page.Form, profile: Profile) -> str:
        try:
            outcome = titles.for_home(self._config(), profile)
        except (ConfigError, titles.TitlesError) as e:
            return page.render(self._view(form, errors={"": f"No suggestions: {e}"}))
        except RunLocked:
            return page.render(self._view(form, errors={"": "A run is using the database; try once it's done."}))
        return page.render(self._view(form, suggestions=outcome))

    def _preview(self, form: page.Form, profile: Profile) -> str:
        if self._fresh():
            return page.render(self._view(form, previewed=True))
        try:
            result = preview.for_home(self._config(), profile)
        except ConfigError as e:
            return page.render(self._view(form, errors={"": f"No preview: {e}"}))
        except RunLocked:
            return page.render(self._view(form, errors={"": "A run is using the database; try once it's done."}))
        return page.render(self._view(form, preview=result, previewed=True))

    def _save(self, form: page.Form, profile: Profile) -> str:
        """Search (and, on a new install, settings and key), then a new key, then email, then the whole install
        checked as `jobscan check` does: each section reported; drafts cleared only when all of it holds."""
        fresh = self._fresh()
        if self.drafts.key and not usable_key(self.drafts.key):
            return page.render(self._view(form, errors={"key": "That isn't an OpenRouter key: paste it again."}))
        try:
            if fresh:
                if not self.drafts.key:
                    return page.render(self._view(form, errors={"key": "Your OpenRouter key is needed."}))
                init.init_from(self.home, init.Answers(
                    name=profile.name, titles=list(profile.titles), exclude_titles=list(profile.exclude_titles),
                    locations=list(profile.locations), eligible_where=profile.eligible_where,
                    tags=list(profile.tags), key=self.drafts.key, custom_tags=profile.custom_tags))
            else:
                init.save_profile(self.home, profile)
        except ConfigError as e:
            return page.render(self._view(form, errors={"": f"Nothing was saved: {e}"}))
        done = [Done(notice="Saved your search, the settings and your key." if fresh
                     else "Saved your search (the previous file is kept as .toml.prev).")]
        if self.drafts.key and not fresh:
            done.append(self._save_key())
        done.append(self._save_email(form, profile.name))
        done.append(self._checked())
        errors = {"": " ".join(d.error for d in done if d.error)} if any(d.error for d in done) else {}
        if not errors:
            self.drafts = Drafts()
        return page.render(self._view(form if errors else page.form_from_profile(profile, self._prefill_email()),
                                      errors=errors, notices=tuple(d.notice for d in done if d.notice)))

    def _save_key(self) -> Done:
        try:
            init.save_key(self.home, self.drafts.key)
        except ConfigError as e:
            return Done(error=f"Your new key wasn't saved: {e}.")
        return Done(notice="Saved your new key.")

    def _save_email(self, form: page.Form, search: str) -> Done:
        try:
            saved = load_email(self.home)
        except ConfigError as e:
            saved = None
            if not form.email_off and not form.email_to:
                return Done(error=f"Your email settings don't load: {e}.")
        if form.email_off:
            try:
                return Done(notice="Email turned off.") if init.remove_email(self.home) else Done()
            except ConfigError as e:
                return Done(error=f"Email wasn't turned off: {e}.")
        if not form.email_to:
            return Done() if saved is None else Done(error="To turn email off, tick the box; else give the address.")
        if not ((form.email_port.isascii() and form.email_port.isdigit()) or not form.email_port):
            return Done(error="Email not saved: the port is a number, 465 or 587.")
        password = self.drafts.password or (saved.password if saved else "")
        if not password:
            return Done(error="Email not saved: your app password is needed.")
        try:
            email = init.email_config(form.email_to, form.email_from or None, form.email_host or None,
                                      int(form.email_port) if form.email_port else None, password)
            if email == saved:
                return Done()
            init.save_email(self.home, init.email_from(email, search))
        except mail.MailError as e:
            return Done(error=f"The test email wasn't sent ({e}); email settings not saved.")
        except ConfigError as e:
            return Done(error=f"Email not saved: {e}.")
        return Done(notice=f"Sent a test email to {email.to} and saved the email settings.")

    def _checked(self) -> Done:
        try:
            checked = install.check(self.home)
        except ConfigError as e:
            return Done(error=f"Saved, but the install doesn't load as a run would: {e}.")
        if checked.key_only_in_environment:
            return Done(error="Saved, but a scheduled run won't find your key: save it here too.")
        return Done(notice="The install loads as a run would: `jobscan run` can start.")

    def _config(self) -> Config:
        """The model to ask: the install's, or on a fresh home the one `init` will write; a key typed here wins."""
        if self._fresh():
            if not self.drafts.key:
                raise ConfigError("type your OpenRouter key first")
            return Config(home=self.home, boards_file=None, model=init.default_model(), api_key=self.drafts.key)
        environ = {**os.environ, "OPENROUTER_API_KEY": self.drafts.key} if self.drafts.key else None
        return load_config(self.home, environ=environ)

    def _saved_email(self) -> EmailConfig | None:
        return None if self._fresh() else load_email(self.home)

    def _prefill_email(self) -> EmailConfig | None:
        """The saved settings to fill the form with; one that doesn't load fills nothing (`_view` says why)."""
        try:
            return self._saved_email()
        except ConfigError:
            return None

    def _fresh(self) -> bool:
        return not (self.home / "config.toml").exists()

    def _view(self, form: page.Form, errors=None, **parts) -> page.View:
        fresh = self._fresh()
        errors = dict(errors or {})
        try:
            email = self._saved_email()
        except ConfigError as e:
            email = None
            errors.setdefault("email", f"Your email settings don't load: {e}")
        key_saved = not fresh and (self.home / KEY_FILE).exists()
        return page.View(form=form, token=self.token, catalogue=tag.builtins(), fresh=fresh,
                         key_held=bool(self.drafts.key) or key_saved, password_held=bool(self.drafts.password),
                         email=email, errors=errors, **parts)


class Server(HTTPServer):
    """127.0.0.1 only, one request at a time."""

    def __init__(self, home: Path, port: int):
        super().__init__(("127.0.0.1", port), _Handler)
        self.site = Site(home, secrets.token_urlsafe(32))
        self.hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/?token={self.site.token}"

    def run(self, open_browser: bool) -> None:
        """Serves until Ctrl-C, after opening the page in the browser when asked."""
        if open_browser:
            webbrowser.open(self.url)
        try:
            self.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.server_close()


class _Handler(BaseHTTPRequestHandler):
    timeout = READ_TIMEOUT
    server: Server

    def do_GET(self):
        if not self._allowed():
            return
        token = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
        if not _same(token, self.server.site.token):
            return self._answer(403, "<p>Open the link <code>jobscan ui</code> printed.</p>")
        self._answer(200, self.server.site.show())

    def do_POST(self):
        if not self._allowed():
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{host}" for host in self.server.hosts}:
            return self._answer(403, "<p>Refused: the form came from another page.</p>")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._answer(411, "<p>Refused: no length.</p>")
        if not 0 <= length <= LARGEST_POST:
            return self._answer(413, "<p>Refused: too large.</p>")
        fields = parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)
        if not _same((fields.get("token") or [""])[0], self.server.site.token):
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


def _same(given: str, token: str) -> bool:
    """A constant-time comparison that refuses, rather than raises on, a token with non-ASCII characters."""
    return hmac.compare_digest(given.encode("utf-8", "replace"), token.encode())
