"""`jobscan ui` served for real on 127.0.0.1 (port 0) in a thread, driven over HTTP as a browser would: the security
checks, then each button on a fresh home and on an existing install."""
import http.client
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from jobscan import init, lock, ui
from jobscan.config import ModelConfig, load_config, load_email, load_profile
from tests.fakes import FakeServer, model_reply, patch_smtp

FIELDS = {"name": "ae", "titles": "Account Executive", "locations": "London", "eligible_where": "the UK",
          "tag": "founding"}
REPLY = {"variants": ["Sales Executive"], "near_misses": ["Account Manager"]}


class UiTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.model = FakeServer()
        self.addCleanup(self.model.close)
        self.model.routes["/chat/completions"] = (200, lambda payload: model_reply(REPLY, cost=0.002))
        fake = ModelConfig(base_url=self.model.url, slug="test/model", provider=None, quantization=None)
        for patcher in [mock.patch.object(init, "default_model", return_value=fake),
                        mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}), *patch_smtp(calls := [])]:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.smtp = calls
        self.server = ui.Server(self.home, 0)
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

    def post(self, action, fields=None, token=None, headers=None, **extra):
        data = urlencode({"token": self.token if token is None else token, "action": action, **(fields or FIELDS),
                          **extra}, doseq=True)
        status, _, text = self.request("POST", body=data, headers={
            "Content-Type": "application/x-www-form-urlencoded", "Origin": f"http://127.0.0.1:{self.port}",
            **(headers or {})})
        return status, text

    def install(self, key="sk-or-v1-old", model_url=None):
        model = f'[model]\nbase_url = "{model_url}"\nslug = "x"\n' if model_url else init.CONFIG
        (self.home / "profiles").mkdir(parents=True)
        (self.home / "config.toml").write_text(model)
        (self.home / "profiles" / "ae.toml").write_text(
            'titles = ["AE"]  # mine\nlocations = ["London"]\neligible_where = "the UK"\ntags = ["founding"]\n')
        if key:
            init.save_key(self.home, key)

    def tearDown(self):
        for text in self.pages:  # no page ever carries a typed secret back
            for secret in ("sk-or-v1-new", "sk-or-v1-old", "sk-or-v1-fresh", "abcd efgh", "abcdefgh"):
                self.assertNotIn(secret, text)


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
        self.assertEqual(self.post("suggest", token="wrong")[0], 403)
        for origin in ["https://evil.example", "null", f"http://127.0.0.1:{self.port + 1}"]:
            self.assertEqual(self.post("save", headers={"Origin": origin}, key="sk-or-v1-fresh")[0], 403, origin)
        self.assertFalse(self.home.exists())  # nothing saved by a refused post
        status, _, _ = self.request("POST", body="x" * (ui.LARGEST_POST + 1), headers={
            "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 413)

    def test_odd_tokens_origins_and_actions_are_refused(self):
        self.assertEqual(self.request("GET", "/?token=%C3%A9")[0], 403)
        self.assertEqual(self.post("save", headers={"Origin": f"127.0.0.1:{self.port}"}, key="sk-or-v1-fresh")[0], 403)
        _, text = self.post("delete-everything", key="sk-or-v1-fresh")
        self.assertIn("Press one of the buttons.", text)
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


class FreshHomeTest(UiTest):
    def test_a_whole_set_up_on_the_page(self):
        status, text = self.post("save", key="sk-or-v1-fresh", email_to="me@gmail.com", password="abcd efgh ijkl mnop")
        self.assertEqual(status, 200)
        self.assertIn("Saved your search, the settings and your key.", text)
        self.assertIn("Sent a test email to me@gmail.com", text)
        self.assertIn("The install loads as a run would", text)
        self.assertEqual(load_profile(self.home).titles, ("Account Executive",))
        key = self.home / "openrouter.key"
        self.assertEqual((key.read_text(), stat.S_IMODE(key.stat().st_mode)), ("sk-or-v1-fresh\n", 0o600))
        self.assertEqual(load_email(self.home).password, "abcdefghijklmnop")
        self.assertEqual(load_config(self.home, environ={}).api_key, "sk-or-v1-fresh")

    def test_the_typed_key_survives_suggest_and_a_refused_save(self):
        _, text = self.post("suggest", key="sk-or-v1-fresh")
        self.assertIn('name="add_title" value="Sales Executive"', text)
        self.assertIn("Counts come after the first run.", text)
        self.assertIn("A key is held", text)
        _, text = self.post("save", {**FIELDS, "titles": ""})
        self.assertIn("Give at least one title.", text)
        self.assertFalse(self.home.exists())
        _, text = self.post("save", add_title="Sales Executive", add_exclude="Account Manager")
        self.assertIn("Saved your search", text)
        profile = load_profile(self.home)
        self.assertEqual((profile.titles, profile.exclude_titles),
                         (("Account Executive", "Sales Executive"), ("Account Manager",)))
        self.assertEqual((self.home / "openrouter.key").read_text(), "sk-or-v1-fresh\n")

    def test_no_key_no_install(self):
        _, text = self.post("save")
        self.assertIn("Your OpenRouter key is needed.", text)
        _, text = self.post("suggest")
        self.assertIn("type your OpenRouter key first", text)
        self.assertFalse(self.home.exists())

    def test_a_refused_test_email_saves_the_rest(self):
        with mock.patch("jobscan.mail.send_test", side_effect=init.mail.MailError("535 bad credentials")):
            _, text = self.post("save", key="sk-or-v1-fresh", email_to="me@gmail.com", password="abcd efgh")
        self.assertIn("Saved your search, the settings and your key.", text)
        self.assertIn("The test email wasn&#x27;t sent (535 bad credentials); email settings not saved.", text)
        self.assertIsNone(load_email(self.home))

    def test_preview_before_any_run(self):
        self.assertIn("No postings yet", self.post("preview")[1])

    def test_a_ticked_suggestion_stays_through_preview_and_a_refused_save(self):
        _, text = self.post("preview", add_title="Sales Executive")
        self.assertIn("Account Executive\nSales Executive</textarea>", text)
        _, text = self.post("save", {**FIELDS, "titles": "Account Executive\nSales Executive", "eligible_where": ""})
        self.assertIn("Account Executive\nSales Executive</textarea>", text)


class ExistingInstallTest(UiTest):
    def test_the_page_shows_the_saved_search(self):
        self.install()
        _, _, text = self.request("GET", f"/?token={self.token}")
        self.assertIn(">AE</textarea>", text)
        self.assertIn("A key is held", text)

    def test_saving_keeps_the_previous_profile_and_the_key(self):
        self.install()
        _, text = self.post("save")
        self.assertIn("previous file is kept as .toml.prev", text)
        self.assertIn("# mine", (self.home / "profiles" / "ae.toml.prev").read_text())
        self.assertEqual(load_profile(self.home).titles, ("Account Executive",))
        self.assertEqual((self.home / "openrouter.key").read_text(), "sk-or-v1-old\n")

    def test_a_replacement_key_survives_suggest_and_never_reverts(self):
        self.install(model_url=self.model.url)
        self.post("suggest", key="sk-or-v1-new")
        self.post("save")
        self.assertEqual((self.home / "openrouter.key").read_text(), "sk-or-v1-new\n")
        self.post("save")
        self.assertEqual((self.home / "openrouter.key").read_text(), "sk-or-v1-new\n")

    def test_a_local_model_install_needs_no_key(self):
        self.install(key=None, model_url=self.model.url)
        _, text = self.post("suggest")
        self.assertIn('name="add_title" value="Sales Executive"', text)
        _, text = self.post("save")
        self.assertIn("The install loads as a run would", text)
        self.assertFalse((self.home / "openrouter.key").exists())

    def test_email_unchanged_sends_nothing_and_off_removes_it(self):
        self.install()
        self.post("save", email_to="me@gmail.com", password="abcd efgh")
        sends = len(self.smtp)
        _, text = self.post("save", email_to="me@gmail.com")  # the saved password, nothing changed
        self.assertEqual(len(self.smtp), sends)
        self.assertNotIn("Sent a test email", text)
        _, text = self.post("save", email_to="me@gmail.com", email_off="on")
        self.assertIn("Email turned off.", text)
        self.assertIsNone(load_email(self.home))

    def test_a_failed_key_save_says_what_was_saved(self):
        self.install()
        with mock.patch.object(init, "save_key", side_effect=init.ConfigError("disk full")):
            _, text = self.post("save", key="sk-or-v1-new")
        self.assertIn("Saved your search", text)
        self.assertIn("Your new key wasn&#x27;t saved: disk full.", text)
        self.assertEqual(load_profile(self.home).titles, ("Account Executive",))

    def test_a_failed_email_removal_is_said_not_crashed(self):
        self.install()
        self.post("save", email_to="me@gmail.com", password="abcd efgh")
        with mock.patch.object(init, "remove_email", side_effect=init.ConfigError("read-only")):
            status, text = self.post("save", email_to="me@gmail.com", email_off="on")
        self.assertEqual(status, 200)
        self.assertIn("Email wasn&#x27;t turned off: read-only.", text)
        self.assertIn('name="email_off" checked', text)  # the tick is kept to try again

    def test_a_form_for_another_search_name_is_refused(self):
        self.install()
        _, text = self.post("save", {**FIELDS, "name": "other"})
        self.assertIn("this install&#x27;s profile is ae.toml", text)
        self.assertEqual(sorted(p.name for p in (self.home / "profiles").iterdir()), ["ae.toml"])

    def test_a_typed_key_is_used_when_the_saved_one_is_missing(self):
        self.install(key=None)
        (self.home / "config.toml").write_text('[model]\nbase_url = "https://openrouter.ai/api/v1"\nslug = "x"\n'
                                               'provider = "p"\nquantization = "q"\n')
        with mock.patch("jobscan.tag._OPENER.open", side_effect=OSError("offline")) as opened:
            _, text = self.post("suggest", key="sk-or-v1-new")
        self.assertIn("No suggestions: OSError", text)  # it got as far as asking, with the typed key
        request = opened.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer sk-or-v1-new")

    def test_an_install_named_against_the_rules_says_why(self):
        self.install()
        (self.home / "profiles" / "ae.toml").rename(self.home / "profiles" / "my_search.toml")
        _, _, text = self.request("GET", f"/?token={self.token}")
        self.assertIn("rename the file", text)

    def test_a_too_long_password_is_said(self):
        self.install()
        _, text = self.post("save", email_to="me@gmail.com", password="p" * 30_000)
        self.assertIn("Too long", text.split('name="password"')[1].split("</fieldset>")[0])

    def test_a_too_long_key_is_never_held(self):
        _, text = self.post("save", key="k" * 30_000)
        self.assertIn("Too long", text)
        _, text = self.post("save")
        self.assertIn("Your OpenRouter key is needed.", text)
        self.assertFalse(self.home.exists())

    def test_a_huge_port_is_a_section_error_after_the_search_saved(self):
        self.install()
        status, text = self.post("save", email_to="me@gmail.com", email_port="9" * 5000, password="abcd efgh")
        self.assertEqual(status, 200)
        self.assertIn("Saved your search", text)
        self.assertIn("the port is a number", text)

    def test_broken_email_settings_can_be_turned_off(self):
        self.install()
        (self.home / "email.toml").write_text("not toml [")
        _, _, text = self.request("GET", f"/?token={self.token}")
        self.assertIn("your email settings don&#x27;t load", text)
        self.assertIn('name="email_off"', text)
        _, text = self.post("save", email_off="on")
        self.assertIn("Email turned off.", text)
        self.assertFalse((self.home / "email.toml").exists())

    def test_a_profile_the_page_would_change_isnt_offered(self):
        self.install()
        (self.home / "profiles" / "ae.toml").write_text(
            'titles = ["AE\\nSDR"]\nlocations = ["London"]\neligible_where = "the UK"\ntags = ["founding"]\n')
        _, _, text = self.request("GET", f"/?token={self.token}")
        self.assertIn("would split in two", text)

    def test_too_many_custom_tags_for_the_page(self):
        self.install()
        tags = "".join(f'\n[custom_tags.t{i}]\ndefinition = "Tag {i}."\n' for i in range(100))
        (self.home / "profiles" / "ae.toml").write_text(
            'titles = ["AE"]\nlocations = ["London"]\neligible_where = "the UK"\ntags = []\n' + tags)
        _, _, text = self.request("GET", f"/?token={self.token}")
        self.assertIn("more than 99 custom tags", text)

    def test_a_run_holding_the_database_is_said(self):
        self.install(model_url=self.model.url)
        (self.home / "jobscan.sqlite").write_bytes(b"")
        with lock.run_lock(self.home):
            _, text = self.post("suggest")
        self.assertIn("A run is using the database", text)
        self.assertEqual([r for r in self.model.requests if r[1] == "/chat/completions"], [])


if __name__ == "__main__":
    unittest.main()
