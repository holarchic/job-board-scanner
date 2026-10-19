import hashlib
import json
import socket
import ssl
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from jobscan import store, tag
from jobscan.config import ConfigError, CustomTag, ModelConfig, Profile, Requirement
from jobscan.records import Posting
from tests.fakes import FakeServer, model_reply

ALL = ("founding", "location_eligible", "segment", "language_required", "tech_company")
DEFS = tag.definitions(ALL, "the UK")
POSTING = Posting(key="ashby:acme:1", ats="ashby", board="ashby:acme", company="Acme",
                  title="Founding Account Executive", location="London", url="https://jobs.ashbyhq.com/acme/1",
                  description="You\u2019ll be our first\u00a0sales hire \u2014  selling to mid-market teams.")


def reply(**cells):
    base = {"founding": {"value": "no", "quote": None}, "location_eligible": {"value": "unknown", "quote": None},
            "segment": {"value": [], "quote": None}, "language_required": {"value": "no", "quote": None},
            "tech_company": {"value": "unknown", "quote": None}}
    return json.dumps({**base, **cells})


class QuoteTest(unittest.TestCase):
    def test_exact_and_normalised_quotes_verify(self):
        self.assertTrue(tag.verify_quote("Founding Account Executive", POSTING))
        self.assertTrue(tag.verify_quote("You'll be our first sales hire - selling", POSTING))
        self.assertTrue(tag.verify_quote("our first sales hire", POSTING))

    def test_fabricated_and_blank_quotes_fail(self):
        self.assertFalse(tag.verify_quote("you will be the first seller", POSTING))
        self.assertFalse(tag.verify_quote("   ", POSTING))

    def test_quote_spanning_two_fields_fails(self):
        self.assertFalse(tag.verify_quote("Founding Account Executive London", POSTING))


class ParseReplyTest(unittest.TestCase):
    def parse(self, content):
        return tag.parse_reply(content, DEFS, POSTING)

    def test_verified_affirmative(self):
        r = self.parse(reply(founding={"value": "yes", "quote": "our first sales hire"}))["founding"]
        self.assertEqual((r.value, r.verified, r.warn), ("yes", True, False))

    def test_fabricated_quote_warns_but_keeps_value(self):
        r = self.parse(reply(founding={"value": "yes", "quote": "the first seller ever"}))["founding"]
        self.assertEqual((r.value, r.verified, r.warn), ("yes", False, True))

    def test_expected_quote_missing_or_blank_warns(self):
        for quote in (None, "  "):
            r = self.parse(reply(location_eligible={"value": "no", "quote": quote}))["location_eligible"]
            self.assertEqual((r.quote, r.warn), (None, True))

    def test_defaults_unknowns_and_empty_lists_need_no_quote(self):
        results = self.parse(reply())
        self.assertFalse(any(r.warn for r in results.values()))
        self.assertEqual(results["segment"].value, ())

    def test_multi_value_canonical_order_without_duplicates(self):
        r = self.parse(reply(segment={"value": ["enterprise", "mid_market", "enterprise"],
                                      "quote": "mid-market teams"}))["segment"]
        self.assertEqual(r.value, ("enterprise", "mid_market"))

    def test_free_text_default_is_normalised(self):
        self.assertEqual(self.parse(reply(language_required={"value": "No", "quote": None}))
                         ["language_required"].value, "no")
        r = self.parse(reply(language_required={"value": "German", "quote": None}))["language_required"]
        self.assertEqual((r.value, r.warn), ("German", True))

    def test_invalid_replies_raise(self):
        missing_quote = json.loads(reply())
        del missing_quote["founding"]["quote"]
        extra_field = json.loads(reply())
        extra_field["founding"]["note"] = "x"
        extra_tag = {**json.loads(reply()), "climate": {"value": "yes", "quote": None}}
        for content in ["not json", "[]", json.dumps({"founding": {"value": "yes", "quote": None}}),
                        json.dumps(missing_quote), json.dumps(extra_field), json.dumps(extra_tag),
                        reply(founding={"value": "maybe", "quote": None}),
                        reply(segment={"value": "enterprise", "quote": None}),
                        reply(segment={"value": ["huge"], "quote": None}),
                        reply(language_required={"value": " ", "quote": None}),
                        reply(founding={"value": "yes", "quote": 3})]:
            with self.assertRaises(tag.ReplyError, msg=content):
                self.parse(content)


class DefinitionsTest(unittest.TestCase):
    def test_eligible_where_substituted_and_hashed(self):
        (uk,) = tag.definitions(("location_eligible",), "the UK")
        (de,) = tag.definitions(("location_eligible",), "Germany")
        self.assertIn("work from the UK", uk.definition)
        self.assertNotEqual(uk.hash, de.hash)

    def test_hash_covers_every_behaviour_field(self):
        (founding,) = tag.definitions(("founding",), "the UK")
        changed = [tag.TagDef(**{**vars(founding), field: value}) for field, value in
                   [("definition", "x"), ("values", ("yes",)), ("multi", True), ("default", None)]]
        self.assertEqual(len({founding.hash, *(d.hash for d in changed)}), 5)

    def test_builtin_questions_and_prompt_unchanged_by_custom_tags(self):
        """The G2-evaluated prompt and the cache keys of every built-in stay exactly as they were."""
        self.assertEqual({d.name: d.hash[:12] for d in DEFS}, {
            "founding": "fc5a33558680", "location_eligible": "5c04fac66302", "segment": "400d4fe9edfe",
            "language_required": "f725e786dd09", "tech_company": "b685ee1ad84e"})
        posting = Posting(key="k", ats="ashby", board="b", company="c", title="T", location="L", url="u",
                          description="D")
        self.assertEqual(hashlib.sha256(tag.prompt(posting, DEFS).encode()).hexdigest()[:12], "2dc4a0825037")

    def test_custom_tags_follow_builtins_with_their_default_stated(self):
        defs = tag.definitions(("founding",), "the UK", (
            CustomTag("climate", "Climate product.", ("yes", "no", "unknown"), multi=False, default="no"),
            CustomTag("model", "B2B or B2C.", ("b2b", "b2c"), multi=True, default=None)))
        self.assertEqual([(d.name, d.validated) for d in defs],
                         [("founding", True), ("climate", False), ("model", False)])
        self.assertEqual(defs[1].definition, "Climate product.\n- Default `no`: the posting doesn't say.")
        self.assertEqual(defs[2].definition, "B2B or B2C.")
        text = tag.prompt(POSTING, defs)
        self.assertIn("### `climate`\nValue: one of `yes` · `no` · `unknown`.\nClimate product.\n- Default `no`", text)
        self.assertIn("### `model`\nValue: a list of zero or more of `b2b` · `b2c`.\nB2B or B2C.", text)
        props = tag.schema(defs)["properties"]
        self.assertEqual(props["model"]["properties"]["value"],
                         {"type": "array", "items": {"type": "string", "enum": ["b2b", "b2c"]}})
        results = tag.parse_reply(json.dumps({"founding": {"value": "no", "quote": None},
                                              "climate": {"value": "no", "quote": None},
                                              "model": {"value": ["b2c", "b2b"], "quote": "sales hire"}}),
                                  defs, POSTING)
        self.assertEqual((results["climate"].warn, results["model"].value), (False, ("b2b", "b2c")))

    def test_custom_tag_using_a_builtin_name_or_question_is_refused(self):
        eligible = tag.definitions(("location_eligible",), "the UK")[0]
        twin = CustomTag("twin", "Climate product.", ("yes", "no", "unknown"), multi=False, default=None)
        for custom in [(CustomTag("founding", "x", ("yes", "no"), multi=False, default=None),),
                       (twin, CustomTag("other", "Climate product.", ("yes", "no", "unknown"), False, None)),
                       (CustomTag("copy", eligible.definition, eligible.values, False, None),)]:
            with self.assertRaises(ConfigError, msg=custom):
                tag.definitions(("founding", "location_eligible"), "the UK", custom)

    def test_location_eligible_validated_only_for_the_uk(self):
        for where, validated in [("the UK, working from London", True), ("United Kingdom", True), ("the U.K.", True),
                                 ("U.K. or Ireland", True),
                                 ("Berlin", False), ("remote anywhere", False), ("Ukraine", False)]:
            (d,) = tag.definitions(("location_eligible",), where)
            self.assertEqual(d.validated, validated, where)

    def test_unknown_tag_is_a_config_error(self):
        with self.assertRaises(ConfigError):
            tag.definitions(("founding", "climate"), "the UK")

    def test_schema_types_per_tag(self):
        props = tag.schema(DEFS)["properties"]
        self.assertEqual(props["founding"]["properties"]["value"], {"type": "string", "enum": ["yes", "no"]})
        self.assertEqual(props["language_required"]["properties"]["value"], {"type": "string"})
        self.assertEqual(props["segment"]["properties"]["value"]["type"], "array")


def profile(require=(), custom=()):
    return Profile(name="ae", titles=("AE",), exclude_titles=(), locations=("London",), eligible_where="the UK",
                   tags=ALL, custom_tags=custom, require=require)


MOTION = CustomTag(name="motion", definition="Who it sells to.", values=("B2B", "b2c", "unknown"), multi=False,
                   default=None)
CHANNELS = CustomTag(name="channels", definition="Where it sells.", values=("web", "store", "unknown"), multi=True,
                     default=None)


class RequireTest(unittest.TestCase):
    """`misses` by tag kind, through `parse_reply` so the results are what the run stores."""
    defs = tag.profile_definitions(profile(custom=(MOTION, CHANNELS)))

    def missed(self, required, **cells):
        cells = json.loads(reply()) | {"motion": {"value": "unknown", "quote": None},
                                       "channels": {"value": [], "quote": None}} | cells
        results = tag.parse_reply(json.dumps(cells), self.defs, POSTING)
        return tag.misses(self.defs, tuple(Requirement(t, v) for t, v in required.items()), results)

    def test_listed_values_compare_exactly_and_unknown_never_misses(self):
        quoted = "our first sales hire"
        self.assertEqual(self.missed({"location_eligible": "yes"},
                                     location_eligible={"value": "no", "quote": quoted}),
                         (Requirement("location_eligible", "yes"),))
        self.assertEqual(self.missed({"location_eligible": "yes"},
                                     location_eligible={"value": "yes", "quote": quoted}), ())
        self.assertEqual(self.missed({"location_eligible": "yes"}), ())  # unknown
        self.assertEqual(self.missed({"motion": "B2B"}, motion={"value": "b2c", "quote": quoted}),
                         (Requirement("motion", "B2B"),))
        self.assertEqual(self.missed({"motion": "B2B"}, motion={"value": "B2B", "quote": quoted}), ())

    def test_free_text_compares_case_folded(self):
        quoted = "our first sales hire"
        self.assertEqual(self.missed({"language_required": "no"}), ())  # the default, unquoted
        self.assertEqual(self.missed({"language_required": "no"}, language_required={"value": "No", "quote": None}),
                         ())
        self.assertEqual(self.missed({"language_required": "no"},
                                     language_required={"value": "Unknown", "quote": None}), ())
        self.assertEqual(self.missed({"language_required": "no"},
                                     language_required={"value": "German", "quote": quoted}),
                         (Requirement("language_required", "no"),))

    def test_lists_miss_only_holding_a_known_value_and_not_the_one_required(self):
        quoted = "our first sales hire"
        self.assertEqual(self.missed({"segment": "smb"}), ())  # empty
        self.assertEqual(self.missed({"segment": "smb"}, segment={"value": ["mid_market", "smb"], "quote": quoted}),
                         ())
        self.assertEqual(self.missed({"segment": "smb"}, segment={"value": ["mid_market"], "quote": quoted}),
                         (Requirement("segment", "smb"),))
        self.assertEqual(self.missed({"channels": "web"}, channels={"value": ["unknown"], "quote": quoted}), ())
        self.assertEqual(self.missed({"channels": "web"}, channels={"value": ["store"], "quote": quoted}),
                         (Requirement("channels", "web"),))

    def test_an_answer_shown_with_a_warning_never_misses(self):
        self.assertEqual(self.missed({"location_eligible": "yes"},
                                     location_eligible={"value": "no", "quote": None}), ())  # warn: no quote
        self.assertEqual(self.missed({"location_eligible": "yes"},
                                     location_eligible={"value": "no", "quote": "never in the posting"}), ())
        # A default answer needs no quote, so one given and not found doesn't warn; it is still shown with ⚠.
        self.assertEqual(self.missed({"founding": "yes"}, founding={"value": "no", "quote": "never in the posting"}),
                         ())

    def test_a_default_answer_without_a_quote_misses(self):
        self.assertEqual(self.missed({"founding": "yes"}), (Requirement("founding", "yes"),))

    def test_every_miss_is_named_in_file_order(self):
        quoted = "our first sales hire"
        self.assertEqual(self.missed({"location_eligible": "yes", "founding": "yes", "language_required": "no"},
                                     location_eligible={"value": "no", "quote": quoted},
                                     language_required={"value": "German", "quote": quoted}),
                         (Requirement("location_eligible", "yes"), Requirement("founding", "yes"),
                          Requirement("language_required", "no")))

    def test_requirements_a_tag_cant_answer_are_refused(self):
        for tag_name, value in [("climate", "yes"), ("location_eligible", "unknown"), ("location_eligible", "maybe"),
                                ("language_required", "Unknown"), ("language_required", "no\nGerman"),
                                ("language_required", " no"), ("motion", "b2b"), ("channels", "unknown")]:
            with self.subTest(tag=tag_name, value=value), self.assertRaises(ConfigError):
                tag.profile_definitions(profile(require=(Requirement(tag_name, value),), custom=(MOTION, CHANNELS)))
        self.assertEqual(len(tag.profile_definitions(profile(
            require=(Requirement("language_required", "French"), Requirement("channels", "web")),
            custom=(MOTION, CHANNELS)))), 7)


class CacheIdentityTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open_db(Path(tmp.name))
        self.addCleanup(self.conn.close)
        self.stored = store.StoredPosting(posting=POSTING, input_hash=store.input_hash(POSTING), first_seen="t")
        self.model = ModelConfig("https://openrouter.ai/api/v1", "deepseek/deepseek-v4-flash", "parasail", "fp8")
        store.save_results(self.conn, self.stored, DEFS, self.model.id, tag.parse_reply(reply(), DEFS, POSTING))

    def test_hit_on_same_identity(self):
        self.assertEqual(len(store.cached_results(self.conn, self.stored, DEFS, self.model.id)), len(DEFS))

    def test_miss_on_location_change_and_provider_change(self):
        moved = Posting(**{**vars(POSTING), "location": "Remote - UK"})
        moved_stored = store.StoredPosting(posting=moved, input_hash=store.input_hash(moved), first_seen="t")
        self.assertEqual(store.cached_results(self.conn, moved_stored, DEFS, self.model.id), {})
        other = ModelConfig(self.model.base_url, self.model.slug, "deepinfra", "fp8")
        self.assertEqual(store.cached_results(self.conn, self.stored, DEFS, other.id), {})


class TagPostingTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.addCleanup(self.server.close)
        self.model = ModelConfig(self.server.url, "test/model", "parasail", "fp8")
        for name, value in (("PAUSE", 0), ("RATE_LIMIT_PAUSES", (0, 0, 0, 0))):
            patcher = mock.patch.object(tag, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_billed_invalid_reply_then_valid_retry_counts_both_costs(self):
        replies = iter([(200, {"choices": [{"message": {"content": "garbage"}}], "usage": {"cost": 0.001}}),
                        model_reply(json.loads(reply()), cost=0.002)])
        self.server.routes["/chat/completions"] = (200, lambda payload: next(replies))
        attempt = tag.tag_posting(POSTING, DEFS, self.model, None)
        self.assertIsInstance(attempt, tag.TagSuccess)
        self.assertAlmostEqual(attempt.usage.cost, 0.003)
        self.assertTrue(attempt.usage.cost_reported)

    def test_invalid_usage_values_count_as_unreported(self):
        for cost, reasoning in [(float("nan"), True), (True, -3), (-0.5, 1.5), ("0.001", None)]:
            status, body = model_reply(json.loads(reply()))
            body["usage"] = {"cost": cost, "completion_tokens_details": {"reasoning_tokens": reasoning}}
            self.server.routes["/chat/completions"] = (200, lambda payload, status=status, body=body: (status, body))
            usage = tag.tag_posting(POSTING, DEFS, self.model, None).usage
            self.assertEqual((usage.cost, usage.cost_reported, usage.reasoning_tokens), (0.0, False, 0), cost)

    def test_unreadable_200_reply_marks_billing_unknown(self):
        replies = iter([(200, "{garbage"), model_reply(json.loads(reply()), cost=0.002)])
        self.server.routes["/chat/completions"] = (200, lambda payload: next(replies))
        attempt = tag.tag_posting(POSTING, DEFS, self.model, None)
        self.assertIsInstance(attempt, tag.TagSuccess)
        self.assertEqual((attempt.usage.cost, attempt.usage.cost_reported), (0.002, False))

    def test_truncated_200_body_marks_billing_unknown(self):
        replies = iter([(200, b'{"choices": [{"mess'), model_reply(json.loads(reply()), cost=0.002)])
        self.server.routes["/chat/completions"] = (200, lambda payload: next(replies))
        attempt = tag.tag_posting(POSTING, DEFS, self.model, None)
        self.assertIsInstance(attempt, tag.TagSuccess)
        self.assertEqual((attempt.usage.cost, attempt.usage.cost_reported), (0.002, False))

    def test_overflowing_cost_is_unreported_not_a_crash(self):
        status, body = model_reply(json.loads(reply()))
        body["usage"] = {"cost": 10 ** 400}
        self.server.routes["/chat/completions"] = (200, lambda payload: (status, body))
        attempt = tag.tag_posting(POSTING, DEFS, self.model, None)
        self.assertIsInstance(attempt, tag.TagSuccess)
        self.assertFalse(attempt.usage.cost_reported)

    def test_success_reports_the_accepted_replys_reasoning(self):
        bad = {"choices": [{"message": {"content": "garbage"}}],
               "usage": {"cost": 0.001, "completion_tokens_details": {"reasoning_tokens": 100}}}
        replies = iter([(200, bad), model_reply(json.loads(reply()))])
        self.server.routes["/chat/completions"] = (200, lambda payload: next(replies))
        attempt = tag.tag_posting(POSTING, DEFS, self.model, None)
        self.assertEqual((attempt.reasoning_tokens, attempt.usage.reasoning_tokens), (0, 100))

    def test_rejected_value_is_not_echoed_in_the_error(self):
        leaky = json.loads(reply())
        leaky["founding"]["value"] = "call jane@example.com"
        self.server.routes["/chat/completions"] = (200, lambda payload: model_reply(leaky))
        attempt = tag.tag_posting(POSTING, DEFS, self.model, None)
        self.assertIsInstance(attempt, tag.TagFailure)
        self.assertNotIn("jane", attempt.error)

    def test_dropped_connection_then_success_marks_billing_unknown(self):
        calls = iter(["drop", "ok"])
        real_open = tag._OPENER.open

        def flaky(*args, **kwargs):  # the connection drops inside the request, as `call` meets it
            if next(calls) == "drop":
                raise ConnectionResetError
            return real_open(*args, **kwargs)

        self.server.routes["/chat/completions"] = (200, lambda payload: model_reply(json.loads(reply()), cost=0.002))
        with mock.patch.object(tag._OPENER, "open", flaky):
            attempt = tag.tag_posting(POSTING, DEFS, self.model, None)
        self.assertIsInstance(attempt, tag.TagSuccess)
        self.assertEqual((attempt.usage.cost, attempt.usage.cost_reported), (0.002, False))

    def test_a_call_that_reaches_nothing_names_why_by_class_only(self):
        for cause, named in [(socket.gaierror(8, "nodename nor servname provided, sk-or-v1-x"), "URLError (gaierror)"),
                             (ssl.SSLCertVerificationError("certificate verify failed"),
                              "URLError (SSLCertVerificationError)"),
                             ("no cause, just text sk-or-v1-x", "URLError")]:
            with mock.patch.object(tag._OPENER, "open", side_effect=urllib.error.URLError(cause)), \
                    self.assertRaises(tag.CallFailed) as failed:
                tag.call(self.model, "sk-or-v1-x", "text", {"type": "object"}, "tags")
            self.assertEqual((failed.exception.reason, failed.exception.http_status), (named, None))
            self.assertNotIn("sk-or", str(failed.exception))

    def test_huge_integer_in_reply_is_an_invalid_reply(self):
        self.server.routes["/chat/completions"] = (
            200, lambda payload: (200, {"choices": [{"message": {"content": '{"founding": ' + "9" * 5000 + "}"}}],
                                        "usage": {"cost": 0.001}}))
        attempt = tag.tag_posting(POSTING, DEFS, self.model, None)
        self.assertEqual((type(attempt), attempt.error), (tag.TagFailure, "invalid reply: not JSON"))

    def test_deeply_nested_reply_is_an_invalid_reply(self):
        nested = "[" * 200_000 + "]" * 200_000
        with mock.patch.object(tag.json, "loads", side_effect=RecursionError):
            with self.assertRaises(tag.ReplyError):
                tag.parse_reply(nested, DEFS, POSTING)

    def test_redirects_are_not_followed(self):
        elsewhere = FakeServer()
        self.addCleanup(elsewhere.close)
        elsewhere.routes["/chat/completions"] = (200, lambda payload: model_reply(json.loads(reply())))

        def redirect(payload):
            return 302, {"moved": True}

        self.server.routes["/chat/completions"] = (302, redirect)
        with mock.patch("tests.fakes.FakeServer.extra_headers", {"Location": f"{elsewhere.url}/chat/completions"},
                        create=True):
            attempt = tag.tag_posting(POSTING, DEFS, self.model, "sk-or-v1-secret")
        self.assertEqual((type(attempt), attempt.http_status), (tag.TagFailure, 302))
        self.assertEqual(elsewhere.requests, [])

    def test_reasoning_tokens_and_missing_cost_are_recorded(self):
        status, body = model_reply(json.loads(reply()))
        body["usage"] = {"completion_tokens_details": {"reasoning_tokens": 812}}
        self.server.routes["/chat/completions"] = (200, lambda payload: (status, body))
        usage = tag.tag_posting(POSTING, DEFS, self.model, None).usage
        self.assertEqual((usage.reasoning_tokens, usage.cost, usage.cost_reported), (812, 0.0, False))

    def test_request_pins_provider_and_strict_schema(self):
        self.server.routes["/chat/completions"] = (200, lambda payload: model_reply(json.loads(reply())))
        tag.tag_posting(POSTING, DEFS, self.model, None)
        (_, _, body), = self.server.requests
        self.assertEqual(body["provider"], {"only": ["parasail"], "allow_fallbacks": False,
                                            "require_parameters": True, "quantizations": ["fp8"]})
        self.assertEqual(body["temperature"], 0)
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertNotIn("reasoning", body)

    def test_rate_limits_get_the_longer_schedule(self):
        replies = iter([(429, {"error": "slow down"})] * 4 + [model_reply(json.loads(reply()))])
        self.server.routes["/chat/completions"] = (200, lambda payload: next(replies))
        attempt = tag.tag_posting(POSTING, DEFS, self.model, None)
        self.assertIsInstance(attempt, tag.TagSuccess)
        self.assertEqual(len(self.server.requests), 5)

    def test_persistent_rate_limit_gives_up_after_five_tries(self):
        self.server.routes["/chat/completions"] = (429, {"error": "slow down"})
        attempt = tag.tag_posting(POSTING, DEFS, self.model, None)
        self.assertEqual((type(attempt), attempt.http_status, len(self.server.requests)), (tag.TagFailure, 429, 5))

    def test_unavailable_model_fails_after_three_tries(self):
        self.server.routes["/chat/completions"] = (503, {"error": "down"})
        attempt = tag.tag_posting(POSTING, DEFS, self.model, None)
        self.assertEqual(attempt, tag.TagFailure(error="HTTP 503", http_status=503, usage=tag.Usage()))
        self.assertEqual(len(self.server.requests), 3)

    def test_three_consecutive_failures_stop_unstarted_postings(self):
        self.server.routes["/chat/completions"] = (503, {"error": "down"})
        postings = [Posting(**{**vars(POSTING), "key": f"ashby:acme:{n}"}) for n in range(5)]
        with mock.patch.object(tag, "WORKERS", 1):
            attempts = tag.tag_all(postings, DEFS, self.model, None)
        # The breaker trips in completion order, so with one worker the next posting may already have started.
        self.assertIn(len(attempts), (3, 4))
        self.assertEqual(len(self.server.requests), len(attempts) * tag.TRIES)


if __name__ == "__main__":
    unittest.main()
