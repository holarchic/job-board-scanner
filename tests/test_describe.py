"""The set-up page's description: one model call, its suggestions cleaned to what the page can show and save."""
import unittest

from jobscan import describe, page, tag
from jobscan.config import ModelConfig, Profile
from tests.fakes import FakeServer, model_reply

WORDS = "I'm looking for account executive jobs with founding teams in London, happy to be remote."


def suggestion(name, question="Does the posting state a salary range?", answers=("yes", "no", "unknown")):
    return {"name": name, "question": question, "answers": list(answers)}


REPLY = {
    "titles": ["Account Executive", " account executive ", "x" * 81],
    "other_titles": ["Sales Executive", "Senior Account Executive", "AE"],  # the second, its title matches already
    "leave_out": ["Account Manager"],
    "near_misses": ["Account Executive Assistant", "Executive", "account manager"],  # the 2nd would exclude its own
    "places": ["London", "Remote", "london"],
    "other_places": ["UK", "United Kingdom", "Remote"],
    "eligible_where": "  the UK,\nworking from London ",
    "tags": ["location_eligible", "no_such_tag", "founding"],
    "custom_tags": [
        suggestion("salary_stated"),
        suggestion("work_policy", "Where is the job done: remote, hybrid or onsite?", ["remote", "hybrid", "onsite",
                                                                                      "unknown"]),
        suggestion("founding", "Is it founding?"),  # a built-in's name
        suggestion("salary_stated", "Another question."),  # a name twice
        suggestion("pay", "Does the posting state a SALARY range?"),  # a question twice
        suggestion("Bad Name", "Is it bad?"),
        suggestion("n" * 41, "Is the name long?"),
        suggestion("long_answer", "Is an answer long?", ["yes", "a" * 41]),
        suggestion("one_answer", "Is there one answer?", ["yes"]),
        suggestion("many_answers", "Are there many answers?", [f"a{i}" for i in range(11)]),
        suggestion("repeated_answer", "Is an answer repeated?", ["yes", "yes"]),
        suggestion("long_question", "q" * 501),
        suggestion("visa", "Does it offer visa sponsorship?\nSay yes only if stated."),  # flattened to one line
    ],
}


class DescribeTest(unittest.TestCase):
    def setUp(self):
        self.model = FakeServer()
        self.addCleanup(self.model.close)
        self.config = ModelConfig(base_url=self.model.url, slug="test/model", provider=None, quantization=None)
        self.answer(REPLY)

    def answer(self, reply, status=200, cost=0.0008):
        self.model.routes["/chat/completions"] = (status, lambda payload: model_reply(reply, cost=cost)) \
            if status == 200 else (status, {"error": "no"})

    def read(self):
        return describe.read(WORDS, self.config, "sk-or-v1-x")

    def test_one_call_names_the_words_and_the_catalogue(self):
        self.read()
        (_, _, body), = self.model.requests
        prompt = body["messages"][0]["content"]
        self.assertIn(WORDS, prompt)
        for t in tag.builtins():
            self.assertIn(f"- {t.name}: {t.summary}", prompt)
        self.assertEqual(body["response_format"]["json_schema"]["name"], "description")
        self.assertEqual(body["reasoning"], {"enabled": False})  # a page waits on it: seconds, not a minute

    def test_suggestions_are_cleaned(self):
        d = self.read()
        self.assertEqual(d.titles, ("Account Executive",))
        self.assertEqual(d.other_titles, ("Sales Executive", "AE"))
        self.assertEqual(d.leave_out, ("Account Manager",))
        self.assertEqual(d.near_misses, ("Account Executive Assistant",))
        self.assertEqual((d.places, d.other_places), (("London", "Remote"), ("UK", "United Kingdom")))
        self.assertEqual(d.eligible_where, "the UK, working from London")
        self.assertEqual(d.tags, ("founding", "location_eligible"))  # known names, in the catalogue's order
        self.assertEqual([c.name for c in d.custom_tags], ["salary_stated", "work_policy", "visa"])
        self.assertEqual(d.custom_tags[2].definition, "Does it offer visa sponsorship? Say yes only if stated.")
        self.assertEqual(d.usage.cost, 0.0008)

    def test_at_most_five_custom_tags(self):
        self.answer({**REPLY, "custom_tags": [suggestion(f"t{i}", f"Question {i}?") for i in range(8)]})
        self.assertEqual([c.name for c in self.read().custom_tags], [f"t{i}" for i in range(5)])

    def test_a_where_too_long_is_dropped(self):
        self.answer({**REPLY, "eligible_where": "w" * 201})
        self.assertEqual(self.read().eligible_where, "")

    def test_what_it_keeps_the_page_can_save_and_reopen_unchanged(self):
        d = self.read()
        profile = Profile(name="ae", titles=d.titles, exclude_titles=d.leave_out, locations=d.places,
                          eligible_where=d.eligible_where, tags=d.tags, custom_tags=d.custom_tags)
        self.assertIsNone(page.unrepresentable(profile))
        self.assertEqual(page.to_profile(page.form_from_profile(profile, None)).profile, profile)

    def test_a_refused_call_or_a_malformed_reply_is_an_error_with_its_usage(self):
        self.answer(None, status=500)
        with self.assertRaises(describe.DescribeError) as caught:
            self.read()
        self.assertIsNone(caught.exception.usage)  # refused: not billed
        for bad in [{**REPLY, "tags": "founding"}, {k: v for k, v in REPLY.items() if k != "places"},
                    {**REPLY, "custom_tags": [{"name": "x", "question": "q?"}]}, ["not", "an", "object"]]:
            self.answer(bad, cost=0.0004)
            with self.assertRaises(describe.DescribeError, msg=bad) as caught:
                self.read()
            self.assertEqual(caught.exception.usage.cost, 0.0004)


if __name__ == "__main__":
    unittest.main()
