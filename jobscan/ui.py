"""`jobscan ui`: the set-up page, served on 127.0.0.1 to one person's browser. Every request must name this
server's host and port; every post must carry the server's one-time token and, when the browser sends one, the
page's own Origin, so another page in the browser can't drive it. One request at a time: two saves never
interleave. Secrets typed into the page are held in memory for this server run and never sent back."""
import hmac
import os
import secrets
import shlex
import webbrowser
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import MappingProxyType
from urllib.parse import parse_qs, urlsplit

from jobscan import describe, init, install, mail, page, preview, report, schedule, tag, titles
from jobscan.config import (
    DEFAULT_HOME,
    EMAIL_FILE,
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

FIRST_RUN_AT = "08:00"  # a new install's daily run, ticked; an existing one unscheduled shows it unticked
READ_TIMEOUT = 10  # seconds a connection may sit silent while sending its request: a browser's idle spare socket
# can't stall the one-at-a-time server
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
    """The page's one owner of state: the install, the token, the drafts of secrets, and the step-by-step draft of
    the search (nothing is written before Save). Each method answers one request. Every page carries the step and a
    revision; a post from any other page (an old tab, the browser's Back) is shown the current step, unapplied."""

    def __init__(self, home: Path, token: str, launchd: schedule.Launchd | None):
        """`launchd`: this Mac's, where Save sets the daily run; None elsewhere (the page gives a cron line)."""
        self.home, self.token, self.drafts, self.launchd = home, token, Drafts(), launchd
        self.revision, self.step, self.blocked = 0, "", ""
        self.form, self.offers = page.Form(), page.Offers()
        self.read_words = ""  # the description the offers came from: read again only when it changes
        self.named = False  # a new search's name is suggested once, when the summary is first reached
        self.saved: page.Saved | None = None  # what the page after Save says

    def show(self) -> str:
        self._start()
        return self._render()

    def post(self, fields: dict[str, list[str]]) -> str:
        self._start()
        if self.blocked:
            return self._render()
        if (fields.get("step") or [""])[0] != self.step or (fields.get("revision") or [""])[0] != str(self.revision):
            return self._render(notices=("That page was out of date: this is where you are.",))
        action = (fields.get("action") or [""])[0]
        if action not in page.actions(self.step):  # refused before anything is applied
            return self._render(errors={"": "Press one of the buttons."})
        self.revision += 1
        self.form, self.offers = page.read_step(self.step, fields, self.form, self.offers)
        if self.form.key and "key" not in self.form.too_long and usable_key(self.form.key):
            self.drafts.key = self.form.key
        if self.form.password and "password" not in self.form.too_long:
            self.drafts.password = self.form.password
        order = page.steps(self._fresh())
        if action == "next":
            return self._next()
        if action == "back":
            return self._go(order[order.index(self.step) - 1] if self._fresh() else "summary")
        if action.startswith("goto:"):
            return self._go(action[5:])
        if action == "skip":
            return self._skip()
        if action == "suggest":
            return self._suggest()
        checked = page.to_profile(self.form)
        if action == "save":
            return self._save(checked)
        return self._render(errors=checked.errors) if checked.profile is None else self._preview(checked.profile)


    def waiting(self, fields: dict[str, list[str]]) -> str:
        """What the page says while a post that waits on the model, the mail server or the database works, read
        before the post is applied; "" for any other post (a stale one included)."""
        self._start()
        step, revision = (fields.get("step") or [""])[0], (fields.get("revision") or [""])[0]
        if self.blocked or step != self.step or revision != str(self.revision):
            return ""
        return page.WAITING.get((step, (fields.get("action") or [""])[0]), "")

    def _start(self) -> None:
        """The first request decides where the steps begin: a new install at how it works, an existing one at its
        summary, or a message when its profile can't go through the page unchanged."""
        if self.step:
            return
        if self._fresh():
            self.step = "intro"
            self.form = replace(self.form, schedule_on=True, schedule_at=FIRST_RUN_AT)
            return
        self.step = "summary"
        try:
            profile = load_profile(self.home)
        except ConfigError as e:
            self.blocked = f"Your install doesn't load: {e}"
            return
        reason = page.unrepresentable(profile)
        if reason:
            self.blocked = f"This page can't edit your profile without changing it ({reason}): edit it in its file."
            return
        self.form = self._reopened(profile)

    def _reopened(self, profile: Profile) -> page.Form:
        """The draft as the install on disk stands: the profile, the email settings and the daily run."""
        at = schedule.scheduled_at(self.home, self.launchd.agents) if self.launchd else None
        return replace(page.form_from_profile(profile, self._prefill_email()), schedule_on=at is not None,
                       schedule_at=f"{at:%H:%M}" if at else FIRST_RUN_AT)

    def _next(self) -> str:
        errors = page.check_step(self.step, self.form, key_held=self._key_held(), fresh=self._fresh(),
                                 password=self._password(),
                                 email_saved=not self._fresh() and (self.home / EMAIL_FILE).exists())
        if errors:
            return self._render(errors=errors)
        if self.step == "describe":
            return self._describe()
        order = page.steps(self._fresh())
        return self._go(order[order.index(self.step) + 1] if self._fresh() else "summary")

    def _describe(self) -> str:
        words = self.form.description
        if not words:
            return self._skip()
        if words == self.read_words:  # read already: the draft, and any edits since, stand
            return self._go("titles")
        try:
            config = self._config()
            described = describe.read(words, config.model, config.api_key)
        except ConfigError as e:
            return self._render(errors={"description": f"Not read: {e}."})
        except describe.DescribeError as e:
            if e.unreachable:
                return self._render(errors={"description": f"Couldn't reach OpenRouter ({e}): check this computer is "
                                                           "online and that no VPN, firewall or web filter blocks "
                                                           "openrouter.ai, then try again, or go on without "
                                                           "suggestions."})
            return self._render(errors={"description": f"The model couldn't read it ({e}): try again, or go on "
                                                       "without suggestions."})
        self.read_words = words
        self.form, self.offers = _prefilled(self.form, described)
        return self._go("titles")

    def _skip(self) -> str:
        if not self.form.tags and not self.form.custom:
            self.form = replace(self.form, tags=frozenset(tag.builtin_names()))
        return self._go("titles")

    def _go(self, step: str) -> str:
        self.step = step
        if step == "summary" and not self.named:
            self.named = True
            if not self.form.name:
                self.form = replace(self.form, name=page.default_name(self.form))
        return self._render()

    def _suggest(self) -> str:
        profile = page.search(self.form)
        if profile is None:
            return self._render(errors={"titles": "Give at least one title first."})
        try:
            outcome = titles.for_home(self._config(), profile)
        except (ConfigError, titles.TitlesError) as e:
            return self._render(errors={"": f"No suggestions: {e}"})
        except RunLocked:
            return self._render(errors={"": "A run is using the database; try once it's done."})
        self.offers = page.with_suggestions(self.offers, outcome)
        return self._render()

    def _preview(self, profile: Profile) -> str:
        if self._fresh():
            return self._render(previewed=True)
        try:
            result = preview.for_home(self._config(), profile)
        except ConfigError as e:
            return self._render(errors={"": f"No preview: {e}"})
        except RunLocked:
            return self._render(errors={"": "A run is using the database; try once it's done."})
        return self._render(preview=result, previewed=True)

    def _save(self, checked: page.Checked) -> str:
        """Search (and, on a new install, settings and key), then a new key, then email, then the whole install
        checked as `jobscan check` does, then the daily run: each section reported. When all of it holds, the drafts
        are cleared and the page after Save says what now."""
        schedule_errors = page.schedule_errors(self.form)  # Back skips the step's own check
        if schedule_errors:
            return self._render(errors=schedule_errors)
        profile = checked.profile
        if profile is None:
            return self._unsaved(checked.errors)
        fresh = self._fresh()
        try:
            if fresh:
                if not self.drafts.key:
                    return self._unsaved({"key": "Your OpenRouter key is needed."})
                init.init_from(self.home, init.Answers(
                    name=profile.name, titles=list(profile.titles), exclude_titles=list(profile.exclude_titles),
                    locations=list(profile.locations), eligible_where=profile.eligible_where,
                    tags=list(profile.tags), key=self.drafts.key, custom_tags=profile.custom_tags,
                    require=profile.require, include_agencies=profile.include_agencies))
            else:
                init.save_profile(self.home, profile)
        except ConfigError as e:
            return self._unsaved({"": f"Nothing was saved: {e}"})
        done = [Done(notice="Saved your search, the settings and your key." if fresh
                     else "Saved your search (the previous file is kept as .toml.prev).")]
        if self.drafts.key and not fresh:
            done.append(self._save_key())
        done.append(self._save_email(profile.name))
        done.append(self._checked())
        done.append(self._save_schedule(ok=not any(d.error for d in done)))
        notices = tuple(d.notice for d in done if d.notice)
        if any(d.error for d in done):
            return self._render(errors={"": " ".join(d.error for d in done if d.error)}, notices=notices)
        self.saved = self._what_now(profile)
        self.drafts = Drafts()
        self.form, self.offers, self.step = self._reopened(profile), page.Offers(), "done"
        return self._render(notices=notices)

    def _unsaved(self, errors: dict[str, str]) -> str:
        """The search didn't save: nothing else is, but a daily run unticked is still removed."""
        done = self._save_schedule(ok=False)
        if done.error:
            errors = {**errors, "": " ".join(e for e in (errors.get(""), done.error) if e)}
        return self._render(errors=errors, notices=(done.notice,) if done.notice else ())

    def _save_schedule(self, ok: bool) -> Done:
        """The daily run, last: set or changed only when every other section held (a run on an install whose email
        failed would deliver its first postings to nobody, never to be resent); removed whatever else failed, as
        `jobscan schedule --off` is. Off macOS there is nothing to set: the page after Save gives the cron line."""
        launchd = self.launchd
        if launchd is None:
            return Done()
        at = schedule.parse_at(self.form.schedule_at) if self.form.schedule_on else None
        try:
            if at is None:
                ours = schedule.scheduled_at(self.home, launchd.agents) is not None or (
                    not schedule.job(self.home, launchd.agents).plist.exists() and schedule.loaded(self.home, launchd))
                if not ours:  # nothing scheduled, or a plist that isn't one daily time: left as it is
                    return Done()
                schedule.remove(self.home, launchd.agents, launchd.run)
                return Done(notice="No longer scheduled.")
            if not ok:
                return Done(error="Not scheduled: fix the above first; any previous schedule is as it was.")
            if schedule.in_place(self.home, at, launchd):
                return Done()
            schedule.install(self.home, at, launchd.agents, launchd.run)
            if not schedule.loaded(self.home, launchd):
                return Done(error="Scheduling failed: launchd didn't load the job.")
        except schedule.ScheduleError as e:  # its words say whether the previous job is back
            return Done(error=f"Scheduling failed: {e}")
        return Done(notice=f"Runs every day at {at:%H:%M}.")

    def _what_now(self, profile: Profile) -> page.Saved:
        """The facts the page after Save gives, from the draft as saved (before it is read back from disk)."""
        form, home = self.form, self.home.resolve()  # the commands work from any directory
        at = schedule.parse_at(form.schedule_at) if form.schedule_on else None
        prefix = "" if home.resolve() == DEFAULT_HOME.expanduser().resolve() else \
            f"JOBSCAN_HOME={shlex.quote(str(home))} "
        return page.Saved(run=f"{prefix}jobscan run", ui=f"{prefix}jobscan ui", check=f"{prefix}jobscan check",
                          digests=str(home / "digests" / profile.name), log=str(home / "runs.log"),
                          email_to="" if form.email_off else form.email_to,
                          at=f"{at:%H:%M}" if at and self.launchd else "",
                          cron=schedule.cron_line(home, at) if at and not self.launchd else "")

    def _save_key(self) -> Done:
        try:
            init.save_key(self.home, self.drafts.key)
        except ConfigError as e:
            return Done(error=f"Your new key wasn't saved: {e}.")
        return Done(notice="Saved your new key.")

    def _save_email(self, search: str) -> Done:
        form = self.form
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
        if form.email_port and not (form.email_port.isascii() and form.email_port.isdigit()
                                    and len(form.email_port) <= 5):
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

    def _password(self) -> str:
        """The app password typed here, else the saved one: what the email step's settings are checked with."""
        if self.drafts.password:
            return self.drafts.password
        try:
            saved = self._saved_email()
        except ConfigError:
            return ""
        return saved.password if saved else ""

    def _saved_email(self) -> EmailConfig | None:
        return None if self._fresh() else load_email(self.home)

    def _prefill_email(self) -> EmailConfig | None:
        """The saved settings to fill the form with; one that doesn't load fills nothing (`_render` says why)."""
        try:
            return self._saved_email()
        except ConfigError:
            return None

    def _schedule_kept(self) -> bool:
        """This install has a plist that isn't one daily time (a weekly one, or one that doesn't parse): Save leaves
        it unless a time is ticked."""
        launchd = self.launchd
        return bool(launchd) and schedule.job(self.home, launchd.agents).plist.exists() and \
            schedule.scheduled_at(self.home, launchd.agents) is None

    def _key_held(self) -> bool:
        return bool(self.drafts.key) or (not self._fresh() and (self.home / KEY_FILE).exists())

    def _fresh(self) -> bool:
        return not (self.home / "config.toml").exists()

    def _render(self, errors=None, **parts) -> str:
        if self.blocked:
            return page.blocked(self.blocked)
        errors = dict(errors or {})
        try:
            email = self._saved_email()
        except ConfigError as e:
            email = None
            errors.setdefault("email", f"Your email settings don't load: {e}")
        fresh = self._fresh()
        return page.render(page.View(
            form=self.form, offers=self.offers, step=self.step, revision=self.revision, token=self.token,
            catalogue=tag.builtins(), fresh=fresh, home=str(self.home.resolve()), launchd=self.launchd is not None,
            schedule_kept=self._schedule_kept(),
            saved=self.saved if self.step == "done" else None, key_held=self._key_held(),
            password_held=bool(self.drafts.password), email=email,
            email_file=not fresh and (self.home / EMAIL_FILE).exists(), errors=errors, **parts))


def _prefilled(form: page.Form, described: describe.Described) -> tuple[page.Form, page.Offers]:
    """The draft as the description asked for it (ticked), and what it may have missed (offered unticked, custom
    tags among them: the model proposes some a built-in already asks)."""
    form = replace(form, titles="\n".join(described.titles), exclude_titles="\n".join(described.leave_out),
                   locations="\n".join(described.places), eligible_where=described.eligible_where,
                   tags=frozenset(described.tags), custom=())
    return form, page.Offers(titles=described.other_titles, excludes=described.near_misses,
                             places=described.other_places, custom=tuple(page.row(c) for c in described.custom_tags),
                             note=f"Filled in from what you said (model call {report.cost(described.usage)}): "
                                  "untick what's wrong, tick what fits.")


class Server(HTTPServer):
    """127.0.0.1 only, one request at a time."""

    def __init__(self, home: Path, port: int, launchd: schedule.Launchd | None):
        super().__init__(("127.0.0.1", port), _Handler)
        self.site = Site(home, secrets.token_urlsafe(32), launchd)
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
        token = parse_qs(self.target.query).get("token", [""])[0]
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
        if not 0 <= length <= page.LARGEST_POST:
            return self._answer(413, "<p>Refused: too large.</p>")
        try:
            fields = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True, errors="strict")
        except UnicodeDecodeError:
            return self._answer(400, "<p>Refused: not UTF-8.</p>")
        if not _same((fields.get("token") or [""])[0], self.server.site.token):
            return self._answer(403, "<p>Open the link <code>jobscan ui</code> printed.</p>")
        waiting = self.server.site.waiting(fields)
        if not waiting:
            return self._answer(200, self.server.site.post(fields))
        # The browser draws what has arrived: the spinner first, the page once the work is done. No length is
        # sent, so the page ends when the connection closes (HTTP/1.0, one request per connection).
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page.waiting(waiting).encode())
        self.wfile.flush()
        self.wfile.write(page.after_waiting(self.server.site.post(fields)).encode())

    def _allowed(self) -> bool:
        if self.headers.get("Host") not in self.server.hosts:
            self._answer(403, "<p>Refused: wrong host.</p>")
            return False
        try:
            self.target = urlsplit(self.path)
        except ValueError:
            self._answer(400, "<p>Refused: not a page address.</p>")
            return False
        if self.target.path != "/":
            self._answer(404, "<p>Not here.</p>")
            return False
        return True

    def end_headers(self):
        """Every response carries the page's policy headers, http.server's own errors (501 and the like) too."""
        for name, value in HEADERS.items():
            self.send_header(name, value)
        super().end_headers()

    def _answer(self, status: int, body: str) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        """Silent: the request line holds the token."""


def _same(given: str, token: str) -> bool:
    """A constant-time comparison that refuses, rather than raises on, a token with non-ASCII characters."""
    return hmac.compare_digest(given.encode("utf-8", "replace"), token.encode())
