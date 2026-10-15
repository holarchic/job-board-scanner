"""`jobscan titles`: one model call, suggestions cleaned, each counted against the stored postings in the profile's
places, nothing written."""
import json
from unittest import mock

from jobscan import cli, store, titles
from jobscan.records import Board
from tests.fakes import FakeServer, model_reply
from tests.test_setup_commands import CliHome, posting

PROFILE = """titles = ["Account Executive"]
exclude_titles = [" Assistant "]
locations = ["London"]
eligible_where = "the UK"
tags = ["founding"]
"""


class TitlesTest(CliHome):
    def setUp(self):
        super().setUp()
        self.model = FakeServer()
        self.addCleanup(self.model.close)
        self.write_profile(PROFILE)
        (self.home / "config.toml").write_text(f'[model]\nbase_url = "{self.model.url}"\nslug = "test/model"\n')
        self.answer({"variants": ["Sales Executive", "sales executive", " AE ", "Account Executive", "", "x" * 81,
                                  "Enterprise Account Executive"],  # the profile's title matches it already
                     "near_misses": ["Account Manager", "Partnerships", "Executive", "assistant"]})

    def answer(self, reply, cost=0.0003):
        self.model.routes["/chat/completions"] = (200, lambda payload: model_reply(reply, cost=cost))

    def seed(self):
        conn = store.open_db(self.home)
        try:
            store.apply_fetch(conn, "ashby:acme", tuple(posting("ashby:acme", k, title, place) for k, title, place in [
                ("1", "Account Executive", "London"),  # a match now
                ("2", "Senior Account Executive", "London"),  # a match now
                ("3", "Sales Executive", "London"),  # added by the variant
                ("4", "Sales Executive", "Paris"),  # out of places
                ("5", "Sales Executive Assistant", "London"),  # excluded with or without the variant
                ("6", "Account Executive Assistant", "London"),  # excluded already
                ("7", "AE, Mid-Market", "London"),  # added by the variant
                ("8", "Account Executive, Partnerships", "London"),  # a match now; the near-miss would drop it
            ]), "2026-10-01T00:00:00+00:00")
            store.apply_fetch(conn, "ashby:gone", (posting("ashby:gone", "9", "Sales Executive", "London"),),
                              "2026-10-01T00:00:00+00:00")  # a board the next run doesn't read
            store.replace_discovered(conn, [Board("ashby", "acme", "acme", "")], "2026-10-01T00:00:00+00:00")
        finally:
            conn.close()

    def test_suggestions_are_counted_by_their_own_effect(self):
        self.seed()
        code, out, err = self.cli("titles")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0], "Other titles your jobs are posted under:")
        self.assertEqual([line.split() for line in lines[1:3]], [["AE", "+1"], ["Sales", "Executive", "+1"]])
        self.assertNotIn("Enterprise", out)
        self.assertEqual(lines[3], "Near-misses: titles your titles match that are other jobs:")
        # "Executive" would leave out the search itself, "assistant" is excluded already: both dropped
        self.assertEqual([line.split() for line in lines[4:6]], [["Partnerships", "-1"], ["Account", "Manager", "-0"]])
        self.assertEqual(lines[6][:3], "+N:")
        self.assertIn(str(self.home / "profiles" / "ae.toml"), out)
        self.assertIn("Model call: $0.0003.", out)

    def test_the_request_carries_the_profile_and_the_strict_schema(self):
        self.cli("titles")
        (_, _, body), = [r for r in self.model.requests if r[1] == "/chat/completions"]
        text = body["messages"][0]["content"]
        self.assertIn("Their titles: Account Executive", text)
        self.assertIn("Titles they already leave out: Assistant", text)
        self.assertEqual(body["response_format"]["json_schema"]["name"], "titles")
        self.assertTrue(body["response_format"]["json_schema"]["strict"])

    def test_before_any_run_the_suggestions_show_without_counts(self):
        before = self.files()
        code, out, _ = self.cli("titles")
        self.assertEqual(code, 0)
        self.assertIn("  Sales Executive\n", out)
        self.assertNotIn("+", out.split("Near-misses")[0])
        self.assertIn("No postings stored yet", out)
        self.assertEqual(self.files(), before)

    def test_a_run_holding_the_database_costs_no_call(self):
        self.seed()
        with cli._run_lock(self.home):
            code, _, err = self.cli("titles")
        self.assertEqual(code, 1)
        self.assertIn("a run is using the database", err)
        self.assertEqual(self.model.requests, [])

    def test_a_database_that_wont_open_costs_no_call(self):
        self.seed()
        with mock.patch.object(store, "open_db", side_effect=store.ConfigError("cannot open database")):
            code, _, err = self.cli("titles")
        self.assertIn("cannot open database", err)
        self.assertEqual(code, 2)
        self.assertEqual(self.model.requests, [])

    def test_a_failed_or_malformed_reply_is_exit_1_with_its_cost(self):
        self.model.routes["/chat/completions"] = (500, {"error": "boom"})
        code, out, err = self.cli("titles")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no title suggestions: HTTP 500", err)
        for reply in [{"variants": "Sales Executive", "near_misses": []}, {"variants": [7], "near_misses": []},
                      {"variants": []},
                      {"variants": [], "near_misses": [], "note": "x"}]:
            self.answer(reply, cost=0.0002)
            code, _, err = self.cli("titles")
            self.assertEqual(code, 1, reply)
            self.assertIn("the reply isn't the two lists of titles; $0.0002", err)

    def test_an_unreported_cost_says_so(self):
        self.model.routes["/chat/completions"] = (200, lambda payload: (200, {"choices": [{"message": {
            "content": json.dumps({"variants": [], "near_misses": []})}}]}))
        code, out, _ = self.cli("titles")
        self.assertEqual(code, 0)
        self.assertIn("none suggested", out)
        self.assertIn("Model call: cost not reported.", out)

    def test_at_most_fifteen_of_each(self):
        self.answer({"variants": [f"Title {i}" for i in range(30)], "near_misses": [f"Other {i}" for i in range(30)]})
        out = self.cli("titles")[1]
        self.assertEqual(sum(1 for line in out.splitlines() if line.startswith("  Title ")), titles.MOST)
        self.assertEqual(sum(1 for line in out.splitlines() if line.startswith("  Other ")), titles.MOST)
