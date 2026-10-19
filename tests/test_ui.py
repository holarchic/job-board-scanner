"""`jobscan ui` served for real on 127.0.0.1 (port 0) in a thread, driven over HTTP as a browser would: each page's
own fields read back from its HTML (ticked boxes, text, textareas) and posted with the button pressed. The security
checks, then the steps of a new install and the summary of an existing one."""
import html
import http.client
import os
import plistlib
import re
import shlex
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import time as time_of_day
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from jobscan import init, lock, page, schedule, tag, ui
from jobscan.config import ModelConfig, Requirement, custom_tag, load_config, load_email, load_profile
from jobscan.describe import Described
from tests.fakes import FakeServer, StatefulLaunchctl, model_reply, patch_smtp

WORDS = "Account executive jobs with founding teams in London, happy to be remote."
DESCRIBED = {
    "titles": ["Account Executive"], "other_titles": ["Sales Executive"], "leave_out": [],
    "near_misses": ["Account Executive Assistant"], "places": ["London", "Remote"], "other_places": ["UK"],
    "eligible_where": "the UK", "tags": ["founding", "location_eligible"],
    "custom_tags": [{"name": "salary_stated", "question": "Does the posting state a salary range?",
                     "answers": ["yes", "no", "unknown"]}]}
SUGGESTED = {"variants": ["Sales Executive"], "near_misses": ["Account Manager"]}


def answers(payload):
    reply = DESCRIBED if payload["response_format"]["json_schema"]["name"] == "description" else SUGGESTED
    return model_reply(reply, cost=0.002)


class Fields(HTMLParser):
    """What a browser would post from a page: inputs with their values (a checkbox only when ticked; a password box
    empty), textareas with their text, and selects with the selected option (else the first)."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, list[str]] = {}
        self.area: str | None = None
        self.select: str | None = None
        self.options: list[tuple[str, bool]] = []
        self.feed(text)

    def handle_starttag(self, name, attrs):
        a = dict(attrs)
        if name == "input" and "name" in a:
            if a.get("type") == "checkbox" and "checked" not in a:
                return
            value = "" if a.get("type") == "password" else a.get("value", "on" if a.get("type") == "checkbox" else "")
            self.fields.setdefault(a["name"], []).append(value)
        elif name == "textarea":
            self.area = a["name"]
            self.fields.setdefault(self.area, []).append("")
        elif name == "select":
            self.select, self.options = a["name"], []
        elif name == "option" and self.select:
            self.options.append((a.get("value", ""), "selected" in a))

    def handle_data(self, data):
        if self.area:
            self.fields[self.area][-1] += data

    def handle_endtag(self, name):
        if name == "textarea":
            self.area = None
        elif name == "select" and self.select:
            chosen = next((v for v, selected in self.options if selected), self.options[0][0] if self.options else "")
            self.fields.setdefault(self.select, []).append(chosen)
            self.select = None


class UiTest(unittest.TestCase):
    home_name = "home"
    mac = True  # launchd present; else the page gives a cron line

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / self.home_name
        self.model = FakeServer()
        self.addCleanup(self.model.close)
        self.model.routes["/chat/completions"] = (200, answers)
        fake = ModelConfig(base_url=self.model.url, slug="test/model", provider=None, quantization=None)
        for patcher in [mock.patch.object(init, "default_model", return_value=fake),
                        mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}), *patch_smtp(calls := [])]:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.smtp = calls
        self.launchd = schedule.Launchd(Path(tmp.name) / "LaunchAgents", StatefulLaunchctl()) if self.mac else None
        self.server = ui.Server(self.home, 0, self.launchd)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port, self.token = self.server.server_port, self.server.site.token
        self.pages = []

    def request(self, method, path="/", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request(method, path, body=body, headers={"Host": f"127.0.0.1:{self.port}", **(headers or {})})
            response = connection.getresponse()
            text = response.read().decode()
            self.pages.append(text)
            return response.status, dict(response.getheaders()), text
        finally:
            connection.close()

    def open(self) -> str:
        return self.request("GET", f"/?token={self.token}")[2]

    def post(self, fields, headers=None):
        status, _, text = self.request("POST", body=urlencode(fields, doseq=True), headers={
            "Content-Type": "application/x-www-form-urlencoded", "Origin": f"http://127.0.0.1:{self.port}",
            **(headers or {})})
        return status, text

    def press(self, action, ticked=None, unticked=(), **typed):
        """Submits the last page as a browser would, with `typed` fields replaced, `ticked` boxes (name → values)
        added and `unticked` ones (name, value) cleared."""
        fields = Fields(self.pages[-1]).fields
        for name, values in (ticked or {}).items():
            fields[name] = [*fields.get(name, []), *values]
        for name, value in unticked:
            fields[name] = [v for v in fields.get(name, []) if v != value]
        fields |= {name: [value] for name, value in typed.items()}
        return self.post({**fields, "action": action})[1]

    def start(self) -> str:
        """A new install, past the page saying how it works: the key step."""
        self.open()
        return self.press("next")

    def step(self) -> str:
        return re.search(r'name="step" value="(\w+)"', self.pages[-1]).group(1)

    def described(self) -> int:
        return sum(1 for _, path, body in self.model.requests if path == "/chat/completions"
                   and body["response_format"]["json_schema"]["name"] == "description")

    def install(self, key="sk-or-v1-old", model_url=None, profile=None):
        model = f'[model]\nbase_url = "{model_url}"\nslug = "x"\n' if model_url else init.CONFIG
        (self.home / "profiles").mkdir(parents=True)
        (self.home / "config.toml").write_text(model)
        (self.home / "profiles" / "ae.toml").write_text(profile or (
            'titles = ["AE"]  # mine\nlocations = ["London"]\neligible_where = "the UK"\ntags = ["founding"]\n'))
        if key:
            init.save_key(self.home, key)

    def tearDown(self):
        for text in self.pages:  # no page ever carries a typed secret back
            for secret in ("sk-or-v1-new", "sk-or-v1-old", "sk-or-v1-fresh", "sk-or-v1-stale", "abcd efgh",
                           "abcdefgh"):
                self.assertNotIn(secret, text)


def checked(text: str, name: str) -> list[str]:
    return Fields(text).fields.get(name, [])


class PrefillTest(unittest.TestCase):
    def test_a_description_ticks_what_was_asked_and_offers_the_rest(self):
        salary = custom_tag("salary_stated", {"definition": "Salary stated?"}, "test")
        described = Described(titles=("Account Executive",), other_titles=("Sales Executive",), leave_out=(),
                              near_misses=("Account Executive Assistant",), places=("London",),
                              other_places=("UK",), eligible_where="the UK", tags=("founding",),
                              custom_tags=(salary,), usage=tag.Usage(0.001))
        form, offers = ui._prefilled(page.Form(description="words"), described)
        self.assertEqual((form.titles, form.locations, form.tags, form.custom, form.description),
                         ("Account Executive", "London", frozenset({"founding"}), (), "words"))
        self.assertEqual((offers.titles, offers.excludes, offers.places, [r.name for r in offers.custom]),
                         (("Sales Executive",), ("Account Executive Assistant",), ("UK",), ["salary_stated"]))
        self.assertIn("$0.0010", offers.note)


class SecurityTest(UiTest):
    def test_the_page_needs_the_token_and_carries_the_headers(self):
        status, headers, text = self.request("GET", f"/?token={self.token}")
        self.assertEqual(status, 200)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual((headers["Referrer-Policy"], headers["X-Content-Type-Options"], headers["Cache-Control"]),
                         ("strict-origin", "nosniff", "no-store"))
        self.assertIn(f'name="token" value="{self.token}"', text)
        for path in ["/", "/?token=wrong"]:
            self.assertEqual(self.request("GET", path)[0], 403)
        self.assertEqual(self.request("GET", f"/other?token={self.token}")[0], 404)

    def test_another_host_name_is_refused(self):  # a DNS name rebound to 127.0.0.1
        status, _, _ = self.request("GET", f"/?token={self.token}", headers={"Host": f"evil.example:{self.port}"})
        self.assertEqual(status, 403)
        self.assertEqual(self.request("GET", f"/?token={self.token}", headers={"Host": f"localhost:{self.port}"})[0],
                         200)

    def test_posts_need_the_token_and_the_pages_own_origin(self):
        self.open()
        fields = {**Fields(self.pages[-1]).fields, "action": "next", "key": "sk-or-v1-fresh"}
        self.assertEqual(self.post({**fields, "token": "wrong"})[0], 403)
        for origin in ["https://evil.example", "null", f"http://127.0.0.1:{self.port + 1}"]:
            self.assertEqual(self.post(fields, headers={"Origin": origin})[0], 403, origin)
        self.assertEqual(self.server.site.drafts.key, "")  # nothing held from a refused post
        status, _, _ = self.request("POST", body="x", headers={  # refused by its stated length, before reading it
            "Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(page.LARGEST_POST + 1)})
        self.assertEqual(status, 413)

    def test_odd_tokens_origins_and_actions_are_refused(self):
        self.assertEqual(self.request("GET", "/?token=%C3%A9")[0], 403)
        self.open()
        fields = Fields(self.pages[-1]).fields
        self.assertEqual(self.post({**fields, "action": "next"}, headers={"Origin": f"127.0.0.1:{self.port}"})[0], 403)
        self.open()
        self.assertIn("Press one of the buttons.", self.press("delete-everything", key="sk-or-v1-fresh"))
        self.assertIn("Press one of the buttons.", self.press("goto:titles", key="sk-or-v1-fresh"))  # the summary's
        self.assertIn("Press one of the buttons.", self.press("back", key="sk-or-v1-fresh"))  # nothing before
        self.assertEqual((self.server.site.drafts.key, self.server.site.revision), ("", 0))  # nothing applied
        self.assertFalse(self.home.exists())

    def test_malformed_requests_get_an_answer_with_the_headers(self):
        status, headers, _ = self.request("GET", "http://[/")
        self.assertEqual(status, 400)
        status, headers, _ = self.request("HEAD", f"/?token={self.token}")  # answered by http.server itself
        self.assertEqual(status, 501)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        status, _, _ = self.request("POST", body=b"token=x&titles=Account%FFExecutive", headers={
            "Content-Type": "application/x-www-form-urlencoded", "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", body=b"token=\xff", headers={
            "Content-Type": "application/x-www-form-urlencoded", "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 400)

    def test_an_idle_connection_doesnt_hold_the_server(self):
        self.assertEqual(ui._Handler.timeout, ui.READ_TIMEOUT)  # every connection gets it; shortened below
        with mock.patch.object(ui._Handler, "timeout", 1):
            idle = socket.create_connection(("127.0.0.1", self.port))
            self.addCleanup(idle.close)
            started = time.monotonic()
            self.assertEqual(self.request("GET", f"/?token={self.token}")[0], 200)
            self.assertLess(time.monotonic() - started, 5)

    def test_a_stale_page_changes_nothing_and_holds_no_secret(self):
        first = self.start()
        self.press("next", key="sk-or-v1-fresh")
        self.press("back")  # the key step again, a newer page of it
        stale = {**Fields(first).fields, "key": "sk-or-v1-stale", "action": "next"}
        _, text = self.post(stale)
        self.assertIn("That page was out of date", text)
        self.assertEqual((self.server.site.drafts.key, self.server.site.step), ("sk-or-v1-fresh", "key"))


class NewInstallTest(UiTest):
    def to_titles(self, words=WORDS):
        self.start()
        self.press("next", key="sk-or-v1-fresh")
        return self.press("next", description=words)

    def test_a_whole_set_up_from_one_sentence_mostly_ticking(self):
        text = self.to_titles()
        self.assertEqual(checked(text, "title"), ["Account Executive"])
        self.assertIn('value="Sales Executive">', text)  # offered, unticked
        text = self.press("next", ticked={"title": ["Sales Executive"]}, title_more="AE")
        self.assertEqual(self.step(), "exclude")
        self.assertIn('value="Account Executive Assistant">', text)
        text = self.press("next", ticked={"exclude": ["Account Executive Assistant"]})
        self.assertEqual((checked(text, "place"), checked(text, "eligible_where")), (["London", "Remote"], ["the UK"]))
        text = self.press("next", ticked={"place": ["UK"]})
        self.assertEqual((checked(text, "tag"), checked(text, "add_custom")), (["founding", "location_eligible"], []))
        self.assertIn('name="add_custom" value="salary_stated">', text)  # offered, one click to add
        self.press("next", ticked={"add_custom": ["salary_stated"]}, **{"require-location_eligible": "yes"})
        text = self.press("next", email_to="me@gmail.com", password="abcd efgh ijkl mnop")
        self.assertEqual((self.step(), checked(text, "schedule_on"), checked(text, "schedule_at")),
                         ("schedule", ["on"], ["08:00"]))  # a daily run, ticked
        text = self.press("next")
        self.assertEqual(checked(text, "name"), ["account-executive"])
        self.assertIn("every day at 08:00", text)
        text = self.press("save")
        self.assertEqual(self.step(), "done")
        self.assertIn("Saved your search, the settings and your key.", text)
        self.assertIn("Sent a test email to me@gmail.com", text)
        self.assertIn("The install loads as a run would", text)
        self.assertIn("Runs every day at 08:00.", text)
        self.assertIn(f"JOBSCAN_HOME={self.home.resolve()} jobscan run", text)  # resolved: from any directory
        self.assertIn(str(self.home.resolve() / "digests" / "account-executive"), text)
        self.assertEqual(schedule.scheduled_at(self.home, self.launchd.agents), time_of_day(8, 0))
        self.assertTrue(schedule.loaded(self.home, self.launchd))
        self.press("goto:summary")
        self.assertEqual(self.step(), "summary")
        profile = load_profile(self.home)
        self.assertEqual((profile.name, profile.titles, profile.exclude_titles, profile.locations),
                         ("account-executive", ("Account Executive", "Sales Executive", "AE"),
                          ("Account Executive Assistant",), ("London", "Remote", "UK")))
        self.assertEqual((profile.tags, [c.name for c in profile.custom_tags], profile.require),
                         (("founding", "location_eligible"), ["salary_stated"],
                          (Requirement("location_eligible", "yes"),)))
        key = self.home / "openrouter.key"
        self.assertEqual((key.read_text(), stat.S_IMODE(key.stat().st_mode)), ("sk-or-v1-fresh\n", 0o600))
        self.assertEqual(load_email(self.home).password, "abcdefghijklmnop")
        self.assertEqual(load_config(self.home, environ={}).api_key, "sk-or-v1-fresh")
        self.assertEqual(self.described(), 1)

    def test_the_spinner_arrives_before_the_model_answers(self):
        release = threading.Event()

        def slow(payload):
            release.wait(10)
            return answers(payload)

        self.model.routes["/chat/completions"] = (200, slow)
        self.start()
        self.press("next", key="sk-or-v1-fresh")
        fields = {**Fields(self.pages[-1]).fields, "description": [WORDS], "action": ["next"]}
        body = urlencode(fields, doseq=True).encode()
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as raw:
            raw.sendall(b"POST / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Type: application/x-www-form-urlencoded"
                        b"\r\nContent-Length: %d\r\n\r\n" % (self.port, len(body)) + body)
            received = b""
            while b"Reading what you said" not in received:
                chunk = raw.recv(65536)
                self.assertTrue(chunk, received[-300:])
                received += chunk
            self.assertFalse(release.is_set())  # the model hasn't answered: the spinner came first
            self.assertNotIn(b"Content-Length", received.split(b"\r\n\r\n", 1)[0])
            release.set()
            while chunk := raw.recv(65536):
                received += chunk
        page_text = received.decode().split("\r\n\r\n", 1)[1]
        self.assertEqual(page_text.count("<!doctype html>"), 1)
        self.assertIn("<style>.waiting{display:none}</style>", page_text)
        self.assertEqual(checked(page_text, "title"), ["Account Executive"])
        self.assertEqual(self.server.site.step, "titles")

    def test_quick_posts_and_stale_slow_ones_answer_whole(self):
        first = self.start()
        _, headers, _ = self.request("POST", body=urlencode({**Fields(first).fields, "action": "next"}, doseq=True),
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertIn("Content-Length", headers)  # Next on the key: quick, one piece
        stale = {**Fields(first).fields, "step": "describe", "action": "next"}  # an old revision
        self.assertEqual(self.server.site.waiting(stale), "")

    def test_back_keeps_what_was_typed_and_the_same_words_are_not_read_twice(self):
        self.to_titles()
        self.press("back", title_more="SDR")
        self.assertEqual(self.step(), "describe")
        text = self.press("next")
        self.assertEqual(checked(text, "title"), ["Account Executive", "SDR"])
        self.assertEqual(self.described(), 1)
        self.press("back")
        text = self.press("next", description="Product manager jobs in Berlin.")
        self.assertEqual(self.described(), 2)  # new words, read again: the draft follows them
        self.assertEqual(checked(text, "title"), ["Account Executive"])

    def test_without_a_description_suggest_works_before_places_and_every_tag_starts_ticked(self):
        self.start()
        self.press("next", key="sk-or-v1-fresh")
        text = self.press("skip")
        self.assertEqual((self.step(), checked(text, "title")), ("titles", []))
        self.assertIn("Give at least one title first.", self.press("suggest"))
        text = self.press("suggest", title_more="Account Executive")
        self.assertIn('value="Sales Executive">', text)
        self.assertIn("Counts come after the first run.", text)
        self.press("next")
        self.press("next")
        text = self.press("next", place_more="London", eligible_where="the UK")
        self.assertEqual(checked(text, "tag"), list(tag.builtin_names()))
        self.press("next")
        self.press("next")
        self.press("next", unticked=[("schedule_on", "on")])
        self.assertIn("Saved your search", self.press("save"))
        self.assertEqual(self.server.site.launchd.run.calls, ["print"])  # unscheduled: nothing to remove
        self.assertEqual(load_profile(self.home).titles, ("Account Executive",))
        self.assertEqual(self.described(), 0)

    def test_a_failed_description_offers_to_go_on_without(self):
        self.model.routes["/chat/completions"] = (500, {"error": "down"})
        text = self.to_titles()
        self.assertEqual(self.step(), "describe")
        self.assertIn("The model couldn&#x27;t read it", text)
        self.assertIn('value="skip"', text)
        self.assertEqual(self.press("skip") and self.step(), "titles")

    def test_a_model_out_of_reach_says_check_the_connection(self):
        with mock.patch("jobscan.tag._OPENER.open", side_effect=urllib.error.URLError(socket.gaierror(8, "x"))):
            text = self.to_titles()
        self.assertEqual(self.step(), "describe")
        self.assertIn("Couldn&#x27;t reach OpenRouter (URLError (gaierror))", text)
        self.assertIn("VPN, firewall or web filter", text)
        self.assertIn('value="skip"', text)

    def test_each_step_keeps_a_wrong_value_but_never_waits_on_later_steps(self):
        self.start()
        self.assertIn("Your OpenRouter key is needed", self.press("next"))
        self.assertIn("isn&#x27;t an OpenRouter key", self.press("next", key="sk or-v1"))
        self.assertIn("Your OpenRouter key is needed", self.press("next"))  # a refused key is never held
        self.press("next", key="sk-or-v1-fresh")
        self.press("skip")
        self.assertIn("Give at least one title.", self.press("next"))
        self.press("next", title_more="Account Executive")  # places, tags and email still empty: not asked yet
        self.press("next")
        self.assertIn("Say where you can work from.", self.press("next", place_more="London"))
        self.press("next", eligible_where="the UK")
        unticked = [("tag", name) for name in tag.builtin_names()]
        self.assertIn("Tick at least one tag", self.press("next", unticked=unticked))
        text = self.press("next", **{"custom-0-name": "founding", "custom-0-definition": "Founding?"})
        self.assertIn("use built-in names", text)
        text = self.press("next", unticked=unticked, **{
            "custom-0-name": "pay", "custom-0-definition": "Is pay stated?",
            "custom-1-name": "salary", "custom-1-definition": "Is pay stated?"})
        self.assertIn("asks the same question", text)
        self.press("next", **{"custom-1-name": ""})
        self.assertEqual(self.step(), "email")
        self.assertIn("Your app password is needed.", self.press("next", email_to="me@gmail.com"))
        self.assertIn("The port is a number", self.press("next", email_port="9" * 5000, password="abcd efgh"))
        self.assertIn("Too long", self.press("next", email_port="", password="p" * 30_000))
        self.press("next", email_to="")
        self.assertIn("Use HH:MM", self.press("next", schedule_at="8"))
        self.press("next", schedule_at="8:30")
        self.assertEqual(self.step(), "summary")
        self.assertIn("every day at 8:30", self.pages[-1])
        self.assertFalse(self.home.exists())

    def test_a_suggested_tag_ticked_then_edited_stays_one_tag(self):
        self.to_titles()
        for _ in range(3):
            self.press("next")
        self.press("next", ticked={"add_custom": ["salary_stated"]})
        self.press("back")
        text = self.press("next", **{"custom-0-definition": "Is a pay range stated?"})  # now a row of its own
        self.assertEqual(self.step(), "email")
        self.press("back")
        self.assertNotIn('name="add_custom"', self.pages[-1])
        self.press("next")
        self.assertEqual(self.step(), "email", text)
        self.assertEqual([(r.name, r.definition) for r in self.server.site.form.custom],
                         [("salary_stated", "Is a pay range stated?")])

    def test_the_name_is_suggested_once(self):
        self.to_titles()
        for _ in range(6):
            self.press("next")
        self.assertEqual(checked(self.pages[-1], "name"), ["account-executive"])
        self.press("goto:titles", name="")
        for _ in range(6):
            text = self.press("next")
        self.assertEqual((self.step(), checked(text, "name")), ("summary", [""]))
        self.assertIn("lower-case letters", self.press("save"))
        self.assertIn("Press one of the buttons.", self.press("next"))  # nothing after the summary
        self.assertFalse(self.home.exists())

    def test_a_too_long_key_is_never_held(self):
        self.start()
        self.assertIn("Too long", self.press("next", key="k" * 30_000))
        self.assertIn("Your OpenRouter key is needed", self.press("next"))
        self.assertFalse(self.home.exists())

    def test_a_refused_test_email_saves_the_rest(self):
        self.to_titles()
        for _ in range(4):
            self.press("next")
        self.press("next", email_to="me@gmail.com", password="abcd efgh")
        self.press("next")
        with mock.patch("jobscan.mail.send_test", side_effect=init.mail.MailError("535 bad credentials")):
            text = self.press("save")
        self.assertEqual(self.step(), "summary")
        self.assertIn("Saved your search, the settings and your key.", text)
        self.assertIn("The test email wasn&#x27;t sent (535 bad credentials); email settings not saved.", text)
        self.assertIn("Not scheduled: fix the above first", text)  # a run would email nobody
        self.assertIsNone(load_email(self.home))
        self.assertIsNone(schedule.scheduled_at(self.home, self.launchd.agents))
        self.assertNotIn("bootstrap", self.launchd.run.calls)

    def test_preview_before_any_run(self):
        self.to_titles()
        for _ in range(6):
            self.press("next")
        self.assertIn("No postings yet", self.press("preview"))


class ExistingInstallTest(UiTest):
    def edit(self, step, **typed):
        """From the summary (or the page after Save): Edit `step`, change what's typed, Done."""
        if self.step() == "done":
            self.press("goto:summary")
        self.press(f"goto:{step}")
        return self.press("next", **typed)

    def test_it_opens_at_the_summary_with_an_edit_per_step(self):
        self.install()
        text = self.open()
        self.assertEqual(self.step(), "summary")
        self.assertIn("<td>AE</td>", text)
        for step in page.EXISTING_STEPS[:-1]:
            self.assertIn(f'value="goto:{step}"', text)
        self.assertNotIn('value="goto:describe"', text)

    def test_every_step_done_unchanged_saves_the_same_profile(self):
        self.install(profile='titles = ["AE", "ae", "AE"]\nlocations = ["London"]\neligible_where = "the UK"\n'
                             'tags = ["founding"]\nrequire = { founding = "yes", buyer = "smb" }\n\n'
                             '[custom_tags.buyer]\ndefinition = "Who buys.\\nSay smb or '
                             'enterprise."\nvalues = ["smb", "enterprise"]\n')
        before = load_profile(self.home)
        self.assertEqual(before.require, (Requirement("founding", "yes"), Requirement("buyer", "smb")))
        self.open()
        for step in page.EXISTING_STEPS[:-1]:
            self.press(f"goto:{step}")
            self.press("next")
            self.assertEqual(self.step(), "summary", step)
        text = self.press("save")
        self.assertIn("previous file is kept as .toml.prev", text)
        after = load_profile(self.home)
        self.assertEqual(after, before)
        self.assertEqual([d.hash for d in tag.definitions(after.tags, after.eligible_where, after.custom_tags)],
                         [d.hash for d in tag.definitions(before.tags, before.eligible_where, before.custom_tags)])
        self.assertEqual((self.home / "openrouter.key").read_text(), "sk-or-v1-old\n")

    def test_a_replacement_key_is_saved_and_never_reverts(self):
        self.install(model_url=self.model.url)
        self.open()
        self.edit("key", key="sk-or-v1-new")
        self.press("save")
        self.assertEqual((self.home / "openrouter.key").read_text(), "sk-or-v1-new\n")
        self.press("goto:summary")
        self.press("save")
        self.assertEqual((self.home / "openrouter.key").read_text(), "sk-or-v1-new\n")

    def test_a_local_model_install_needs_no_key(self):
        self.install(key=None, model_url=self.model.url)
        self.open()
        self.press("goto:titles")
        self.assertIn('value="Sales Executive">', self.press("suggest"))
        self.press("next")
        text = self.press("save")
        self.assertIn("The install loads as a run would", text)
        self.assertFalse((self.home / "openrouter.key").exists())

    def test_email_unchanged_sends_nothing_and_off_removes_it(self):
        self.install()
        self.open()
        self.edit("email", email_to="me@gmail.com", password="abcd efgh")
        self.press("save")
        sends = len(self.smtp)
        self.edit("email")  # the saved password, nothing changed
        text = self.press("save")
        self.assertEqual(len(self.smtp), sends)
        self.assertNotIn("Sent a test email", text)
        self.edit("email", email_off="on")
        self.assertIn("Email turned off.", self.press("save"))
        self.assertIsNone(load_email(self.home))

    def test_a_blank_address_needs_the_off_box(self):
        self.install()
        self.open()
        self.edit("email", email_to="me@gmail.com", password="abcd efgh")
        self.press("save")
        self.assertIn("To turn email off, tick the box", self.edit("email", email_to=""))
        self.assertEqual(self.step(), "email")

    def test_a_failed_key_save_says_what_was_saved(self):
        self.install()
        self.open()
        self.edit("key", key="sk-or-v1-new")
        with mock.patch.object(init, "save_key", side_effect=init.ConfigError("disk full")):
            text = self.press("save")
        self.assertIn("Saved your search", text)
        self.assertIn("Your new key wasn&#x27;t saved: disk full.", text)

    def test_a_failed_email_removal_is_said_not_crashed(self):
        self.install()
        self.open()
        self.edit("email", email_to="me@gmail.com", password="abcd efgh")
        self.press("save")
        self.edit("email", email_off="on")
        with mock.patch.object(init, "remove_email", side_effect=init.ConfigError("read-only")):
            text = self.press("save")
        self.assertIn("Email wasn&#x27;t turned off: read-only.", text)
        self.press("goto:email")
        self.assertIn('name="email_off" checked', self.pages[-1])  # the tick is kept to try again

    def test_a_posted_name_for_another_search_is_refused(self):
        self.install()
        self.open()
        text = self.press("save", name="other")
        self.assertIn("this install&#x27;s profile is ae.toml", text)
        self.assertEqual(sorted(p.name for p in (self.home / "profiles").iterdir()), ["ae.toml"])

    def test_a_typed_key_is_used_when_the_saved_one_is_missing(self):
        self.install(key=None)
        (self.home / "config.toml").write_text('[model]\nbase_url = "https://openrouter.ai/api/v1"\nslug = "x"\n'
                                               'provider = "p"\nquantization = "q"\n')
        self.open()
        self.edit("key", key="sk-or-v1-new")
        self.press("goto:titles")
        with mock.patch("jobscan.tag._OPENER.open", side_effect=OSError("offline")) as opened:
            text = self.press("suggest")
        self.assertIn("No suggestions: OSError", text)  # it got as far as asking, with the typed key
        self.assertEqual(opened.call_args.args[0].get_header("Authorization"), "Bearer sk-or-v1-new")

    def test_broken_email_settings_can_be_turned_off(self):
        self.install()
        (self.home / "email.toml").write_text("not toml [")
        self.assertIn("Your email settings don&#x27;t load", self.open())
        self.press("goto:email")
        self.assertIn('name="email_off"', self.pages[-1])
        self.press("next", email_off="on")
        self.assertIn("Email turned off.", self.press("save"))
        self.assertFalse((self.home / "email.toml").exists())

    def test_installs_the_page_cant_edit_say_why(self):
        for text, reason in [
                ('titles = ["AE\\nSDR"]\nlocations = ["London"]\neligible_where = "the UK"\ntags = ["founding"]\n',
                 "would split in two"),
                ('titles = ["AE"]\nlocations = ["London"]\neligible_where = "the UK"\ntags = []\n' + "".join(
                    f'\n[custom_tags.t{i}]\ndefinition = "Tag {i}."\n' for i in range(100)),
                 "more than 99 custom tags")]:
            with self.subTest(reason):
                site = ui.Site(self.home, "t", None)
                if not self.home.exists():
                    self.install()
                (self.home / "profiles" / "ae.toml").write_text(text)
                self.assertIn(reason, site.show())
                self.assertNotIn("<button", site.show())
        (self.home / "profiles" / "ae.toml").write_text(
            'titles = ["AE"]\nlocations = ["London"]\neligible_where = "the UK"\ntags = ["founding"]\n')
        (self.home / "profiles" / "ae.toml").rename(self.home / "profiles" / "my_search.toml")
        self.assertIn("rename the file", ui.Site(self.home, "t", None).show())

    def test_a_run_holding_the_database_is_said(self):
        self.install(model_url=self.model.url)
        (self.home / "jobscan.sqlite").write_bytes(b"")
        self.open()
        self.press("goto:titles")
        with lock.run_lock(self.home):
            text = self.press("suggest")
        self.assertIn("A run is using the database", text)
        self.assertEqual([r for r in self.model.requests if r[1] == "/chat/completions"], [])


class ScheduleTest(UiTest):
    def scheduled(self, at=time_of_day(7, 30)):
        """This install's job, set up as `jobscan schedule` would; launchctl's calls counted from here."""
        schedule.install(self.home, at, self.launchd.agents, self.launchd.run)
        self.launchd.run.calls.clear()

    def calls(self):
        return [c for c in self.launchd.run.calls if c != "print"]

    def save(self, **typed):
        self.press("goto:schedule")
        self.press("next", **typed)
        return self.press("save")

    def test_unchanged_it_only_asks_launchd(self):
        self.install()
        self.scheduled()
        text = self.open()
        self.assertIn("every day at 07:30", text)
        for step in page.EDITABLE:
            self.press(f"goto:{step}")
            self.press("next")
        text = self.press("save")
        self.assertEqual((self.step(), self.calls()), ("done", []))
        self.assertIn("every day at 07:30", text)
        self.assertNotIn("Runs every day", text)  # nothing changed, nothing said

    def test_a_new_time_replaces_the_job_and_unticking_removes_it(self):
        self.install()
        self.scheduled()
        self.open()
        self.assertIn("Runs every day at 09:05.", self.save(schedule_at="9:05"))
        self.assertEqual((self.calls(), schedule.scheduled_at(self.home, self.launchd.agents)),
                         (["bootout", "bootstrap"], time_of_day(9, 5)))
        self.press("goto:summary")
        text = self.save(unticked=[("schedule_on", "on")])
        self.assertIn("No longer scheduled.", text)
        self.assertNotIn("by itself", text)
        self.assertFalse(schedule.job(self.home, self.launchd.agents).plist.exists())
        self.assertFalse(schedule.loaded(self.home, self.launchd))

    def test_launchd_refusing_says_so_and_what_became_of_the_old_job(self):
        self.install()
        self.scheduled()
        self.open()
        self.launchd.run.refuse["bootstrap"] = 1
        text = self.save(schedule_at="9:00")
        self.assertEqual(self.step(), "summary")
        self.assertIn("Scheduling failed: launchctl bootstrap failed (5)", text)
        self.assertEqual(schedule.scheduled_at(self.home, self.launchd.agents), time_of_day(7, 30))  # put back
        self.assertTrue(schedule.loaded(self.home, self.launchd))
        self.launchd.run.refuse["bootstrap"] = 2
        text = self.press("save")
        self.assertIn("putting the previous schedule back also failed", text)

    def test_a_removal_half_done_is_set_up_again(self):
        self.install()
        self.scheduled()
        self.open()
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("locked")):
            text = self.save(unticked=[("schedule_on", "on")])
        self.assertIn("Scheduling failed: PermissionError: locked", text)
        self.assertFalse(schedule.loaded(self.home, self.launchd))  # unloaded, its plist left behind
        reopened = ui.Site(self.home, "t", self.launchd)
        self.assertIn("every day at 07:30", reopened.show())
        fields = {**Fields(reopened.show()).fields, "token": ["t"], "action": ["save"]}
        self.assertIn("Runs every day at 07:30.", reopened.post(fields))
        self.assertTrue(schedule.loaded(self.home, self.launchd))

    def test_unticking_removes_the_job_even_when_the_search_fails_to_save(self):
        self.install()
        self.scheduled()
        self.open()
        self.press("goto:schedule")
        self.press("next", unticked=[("schedule_on", "on")])
        with mock.patch.object(init, "save_profile", side_effect=init.ConfigError("disk full")):
            text = self.press("save")
        self.assertIn("Nothing was saved: disk full", text)
        self.assertIn("No longer scheduled.", text)
        self.assertFalse(schedule.loaded(self.home, self.launchd))
        self.press("goto:titles")
        self.press("back", unticked=[("title", "AE")])  # no titles, past the step's check: Save refuses the search
        self.press("goto:schedule")
        text = self.press("next", schedule_on="on", schedule_at="07:30")
        self.assertIn("Not scheduled: fix the above first", self.press("save"))
        self.assertNotIn("bootstrap", self.launchd.run.calls)

    def test_a_plist_the_page_didnt_write_is_left_alone(self):
        self.install()
        plist = schedule.job(self.home, self.launchd.agents).plist
        plist.parent.mkdir(parents=True)
        for written in [plistlib.dumps({"StartCalendarInterval": {"Weekday": 1, "Hour": 7, "Minute": 0}}),
                        plistlib.dumps({"StartCalendarInterval": {"Hour": 7, "Minute": 0}})[:120],
                        b'<?xml version="1.0"?><plist version="1.0"><date>invalid</date></plist>']:
            plist.write_bytes(written)
            site = ui.Site(self.home, "t", self.launchd)
            self.assertIn("a schedule this page can&#x27;t show, left as it is", site.show())  # never a crash
            text = site.post({**Fields(site.show()).fields, "token": ["t"], "action": ["save"]})
            self.assertIn("Saved your search", text)
            self.assertEqual(plist.read_bytes(), written)

    def test_a_time_cleared_then_back_is_refused_at_save(self):
        self.install()
        self.open()
        self.press("goto:schedule")
        self.press("back", schedule_on="on", schedule_at="")
        before = (self.home / "profiles" / "ae.toml").read_bytes()
        text = self.press("save")
        self.assertEqual(self.step(), "summary")
        self.assertIn("Use HH:MM", text)
        self.assertEqual(((self.home / "profiles" / "ae.toml").read_bytes(), self.launchd.run.calls), (before, []))
        self.assertFalse((self.home / "profiles" / "ae.toml.prev").exists())

    def test_a_key_a_scheduled_run_cant_see_schedules_nothing_but_unticking_still_removes(self):
        self.install(key=None)
        self.open()
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-v1-env"}):
            text = self.save(schedule_on="on", schedule_at="08:00")
        self.assertIn("a scheduled run won&#x27;t find your key", text)
        self.assertIn("Not scheduled: fix the above first", text)
        self.assertNotIn("bootstrap", self.launchd.run.calls)
        init.save_key(self.home, "sk-or-v1-old")
        self.scheduled()
        (self.home / "email.toml").write_text("not toml [")
        broken = ui.Site(self.home, "t", self.launchd)
        broken.show()
        broken.post({"token": ["t"], "step": ["summary"], "revision": ["0"], "action": ["goto:schedule"]})
        text = broken.post({"token": ["t"], "step": ["schedule"], "revision": ["1"], "action": ["next"],
                            "schedule_at": ["07:30"]})
        text = broken.post({"token": ["t"], "step": ["summary"], "revision": ["2"], "action": ["save"]})
        self.assertIn("No longer scheduled.", text)
        self.assertIn("email settings don&#x27;t load", text)
        self.assertFalse(schedule.loaded(self.home, self.launchd))

    def test_a_moved_interpreter_is_written_again(self):
        self.install()
        self.scheduled()
        self.open()
        with mock.patch.object(sys, "executable", "/elsewhere/python3"):
            self.assertIn("Runs every day at 07:30.", self.press("save"))
        self.assertEqual(self.calls(), ["bootout", "bootstrap"])
        self.assertIn(b"/elsewhere/python3", schedule.job(self.home, self.launchd.agents).plist.read_bytes())


class ElsewhereTest(UiTest):
    mac = False
    home_name = "it's my home"

    def test_the_page_gives_a_cron_line_and_quoted_commands(self):
        self.start()
        self.assertIn("crontab", self.pages[0])
        self.press("next", key="sk-or-v1-fresh")
        self.press("skip")
        self.press("next", title_more="Account Executive")
        self.press("next")
        self.press("next", place_more="London", eligible_where="the UK")
        self.press("next")
        self.press("next")
        self.assertIn("Unticking removes nothing", self.pages[-1])
        self.press("next")
        self.assertIn("a cron line at 08:00, added by you", self.pages[-1])
        text = self.press("save")
        self.assertEqual(self.step(), "done")
        quoted = shlex.quote(str(self.home.resolve()))
        for command in ("run", "ui", "check"):
            self.assertIn(html.escape(f"JOBSCAN_HOME={quoted} jobscan {command}"), text)
        self.assertIn(html.escape(schedule.cron_line(self.home, time_of_day(8, 0))), text)
        self.assertNotIn("by itself", text)
        self.assertEqual(shlex.split(f"JOBSCAN_HOME={quoted}")[0], f"JOBSCAN_HOME={self.home.resolve()}")


if __name__ == "__main__":
    unittest.main()
