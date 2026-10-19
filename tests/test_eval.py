import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobscan import cli, evaluate, tag
from jobscan.records import Posting
from tests.fakes import FakeServer, model_reply


def item(id_, **labels):
    return evaluate.LabelledPosting(id=id_, posting=Posting(key=id_, ats="eval", board="eval", company="",
                                                            title="AE", location="London", url="",
                                                            description="text"), labels=labels)


def result(value, quote=None, verified=False):
    return tag.TagResult(value=value, quote=quote, verified=verified, warn=False)


def prediction(founding="no", location="yes", language="no", quote=None, verified=False):
    return {"founding": result(founding, quote, verified), "location_eligible": result(location),
            "language_required": result(language)}


class ScoreTest(unittest.TestCase):
    def test_missing_predictions_count_wrong_and_coverage_is_reported(self):
        items = [item(str(n), founding="no", location_eligible="yes", language_required="no") for n in range(10)]
        report = evaluate.score(items, {str(n): prediction() for n in range(8)})
        founding = report.scores[0]
        self.assertEqual((report.tagged, founding.labelled, founding.predicted), (8, 10, 8))
        self.assertAlmostEqual(founding.accuracy, 0.8)

    def test_absent_minority_and_absent_quotes_fail_the_gate(self):
        items = [item("1", founding="no", location_eligible="yes", language_required="no")]
        report = evaluate.score(items, {"1": prediction()})
        self.assertTrue(all(s.accuracy == 1.0 for s in report.scores))
        self.assertTrue(all(s.recall is None and not s.clears for s in report.scores))
        self.assertIsNone(report.quote_rate)
        self.assertFalse(report.passed)

    def test_minority_miss_and_language_loose_match(self):
        items = [item("1", founding="yes", location_eligible="no", language_required="Mandarin"),
                 item("2", founding="yes", location_eligible="no", language_required="German")]
        report = evaluate.score(items, {
            "1": prediction(founding="yes", location="no", language="Mandarin Chinese", quote="q", verified=True),
            "2": prediction(founding="no", location="no", language="no", quote="made up", verified=False)})
        founding, location, language = report.scores
        self.assertEqual((founding.recall, founding.precision), (0.5, 1.0))
        self.assertEqual(location.recall, 1.0)
        self.assertEqual(language.recall, 0.5)
        self.assertEqual((report.quotes, report.quote_rate), (2, 0.5))

    def test_passes_when_every_bar_is_cleared(self):
        items = [item("1", founding="yes", location_eligible="no", language_required="French")]
        report = evaluate.score(items, {"1": prediction("yes", "no", "French", quote="q", verified=True)})
        self.assertTrue(report.passed)


class EvalCommandTest(unittest.TestCase):
    """The command drives the production tagging path: every posting goes to the model with all built-ins."""

    def test_eval_calls_the_model_through_tag_posting(self):
        model = FakeServer()
        self.addCleanup(model.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        (home / "config.toml").write_text(f'boards_file = "b.json"\n[model]\nbase_url = "{model.url}"\nslug = "t/m"\n')
        (home / "set.json").write_text(json.dumps([
            {"id": "P1", "title": "Founding AE", "location": "Berlin", "description": "Our first sales hire.",
             "labels": {"founding": "yes", "location_eligible": "no", "language_required": "no"}}]))
        model.routes["/chat/completions"] = (200, lambda payload: model_reply({
            "founding": {"value": "yes", "quote": "Our first sales hire."},
            "location_eligible": {"value": "no", "quote": "Berlin"},
            "segment": {"value": [], "quote": None}, "language_required": {"value": "no", "quote": None},
            "tech_company": {"value": "unknown", "quote": None}, "agency": {"value": "no", "quote": None}}))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"JOBSCAN_HOME": str(home)}), contextlib.redirect_stdout(out):
            code = cli.main(["eval", str(home / "set.json")])
        self.assertEqual(code, 0)
        (_, _, body), = model.requests
        self.assertEqual(set(body["response_format"]["json_schema"]["schema"]["required"]),
                         {*tag.builtin_names(), tag.AGENCY})
        self.assertIn("work from the UK", body["messages"][0]["content"])
        self.assertIn("G2 bar: FAIL", out.getvalue())  # language_required has no minority answer in a 1-posting set
        self.assertEqual(len(list((home / "eval").glob("*.jsonl"))), 1)

    def test_malformed_set_is_a_usage_error_before_any_model_call(self):
        model = FakeServer()
        self.addCleanup(model.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        (home / "config.toml").write_text(f'boards_file = "b.json"\n[model]\nbase_url = "{model.url}"\nslug = "t/m"\n')
        good = {"id": "P1", "title": "AE", "location": "London", "description": "d", "labels": {"founding": "no"}}
        for bad in [{"id": "P1"}, {**good, "title": None}, {**good, "labels": {"founding": "maybe"}},
                    {**good, "labels": {"language_required": 3}}, {**good, "labels": []}]:
            (home / "set.json").write_text(json.dumps([good, bad]))
            with mock.patch.dict(os.environ, {"JOBSCAN_HOME": str(home)}), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["eval", str(home / "set.json")]), 2, bad)
        self.assertEqual(model.requests, [])


if __name__ == "__main__":
    unittest.main()
