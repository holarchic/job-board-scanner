"""The set-up page's description: the person says what they're looking for in their own words (typed or dictated),
and one model call turns it into suggestions for every later step. Everything is cleaned to what the page can show
and save, then offered for ticking; nothing is saved from here."""
import json
from dataclasses import dataclass

from jobscan import tag, titles
from jobscan.config import CUSTOM_TAG_NAME, ConfigError, CustomTag, ModelConfig, custom_tag
from jobscan.payload import encodable, one_line

MOST_CUSTOM = 5
LONGEST_NAME = 40
LONGEST_QUESTION = 500
LONGEST_ANSWER = 40
FEWEST_ANSWERS, MOST_ANSWERS = 2, 10
LONGEST_WHERE = 200
PROMPT = """Someone is setting up a job search. In their own words:

<words>
{words}
</words>

The search keeps postings whose title contains one of their titles as whole words (case-insensitive) and whose
location text contains one of their places as whole words. A language model then answers tags about every kept
posting. The built-in tags:
{catalogue}

Return JSON with:
- `titles`: the job titles they asked for, as job ads write them ("Account Executive").
- `other_titles`: up to 8 other titles employers commonly post the same job under, which their titles miss as
  words ("Sales Executive", "AE" for Account Executive). The same job only: not a different role.
- `leave_out`: titles they said to skip.
- `near_misses`: up to 6 real titles, common in job ads, that their titles match as words but are different jobs,
  to leave out ("Account Executive Assistant"); never the same job at another level ("Senior", "Junior").
- `places`: the places they named or implied, as job ads write locations, with the country of each city named
  ("London", "UK", "Remote").
- `other_places`: up to 4 other ways job ads commonly write those same places ("United Kingdom", "England" for UK);
  never a wider area than they asked for.
- `eligible_where`: where they can work from, in a few words: what they said, else the country of their places
  ("the UK" for London); "" if no place names a country.
- `tags`: the names of the built-in tags that fit what they care about.
- `custom_tags`: up to 5 tags for things they said they care about that no built-in tag above already asks, each
  with `name`
  (lower_case_with_underscores), `question` (what to answer about a posting, saying what each answer means) and
  `answers` (2 to 10 short answers, "unknown" among them). [] if none."""
LISTS = ("titles", "other_titles", "leave_out", "near_misses", "places", "other_places", "tags")


def _schema() -> dict:
    """Built per request: the request body is JSON, and no caller can change a shared copy."""
    strings = {"type": "array", "items": {"type": "string"}}
    custom = {"type": "object", "additionalProperties": False, "required": ["name", "question", "answers"],
              "properties": {"name": {"type": "string"}, "question": {"type": "string"}, "answers": strings}}
    return {"type": "object", "additionalProperties": False, "required": [*LISTS, "eligible_where", "custom_tags"],
            "properties": {**dict.fromkeys(LISTS, strings), "eligible_where": {"type": "string"},
                           "custom_tags": {"type": "array", "items": custom}}}


class DescribeError(Exception):
    """The model call failed or its reply wasn't the shape asked for. `usage` is the reply's, when one came back."""

    def __init__(self, reason: str, usage: tag.Usage | None = None, unreachable: bool = False):
        super().__init__(reason)
        self.usage, self.unreachable = usage, unreachable  # unreachable: no answer came back at all


@dataclass(frozen=True)
class Described:
    """Suggestions for each step. The first of each pair is what the person asked for (offered ticked), the second
    what they may have missed (offered unticked); built-in tags are offered ticked, custom tags unticked (the model
    proposes some a built-in already asks)."""
    titles: tuple[str, ...]
    other_titles: tuple[str, ...]
    leave_out: tuple[str, ...]
    near_misses: tuple[str, ...]
    places: tuple[str, ...]
    other_places: tuple[str, ...]
    eligible_where: str  # "" when they didn't say
    tags: tuple[str, ...]  # built-in names, in the catalogue's order
    custom_tags: tuple[CustomTag, ...]
    usage: tag.Usage


def read(words: str, model: ModelConfig, api_key: str | None) -> Described:
    catalogue = "\n".join(f"- {t.name}: {t.summary}" for t in tag.builtins())
    try:
        reply = tag.call(model, api_key, PROMPT.format(words=words, catalogue=catalogue), _schema(), "description",
                         reasoning=False)
    except tag.CallFailed as e:  # an HTTP refusal isn't billed; another failure may have been
        raise DescribeError(e.reason, usage=None if e.http_status else e.usage, unreachable=not e.http_status) from None
    try:
        data = json.loads(reply.content) if reply.content is not None else None
    except (ValueError, RecursionError):
        data = None
    if not _well_formed(data):
        raise DescribeError("the reply isn't the suggestions asked for", usage=reply.usage)
    asked = titles.clean(data["titles"])
    leave_out = titles.clean(data["leave_out"], {t.casefold() for t in asked})
    other_titles, near_misses = titles.sift(asked, leave_out, data["other_titles"], data["near_misses"])
    places = titles.clean(data["places"])
    named = set(data["tags"])
    builtins = tuple(n for n in tag.builtin_names() if n in named)
    return Described(titles=asked, other_titles=other_titles, leave_out=leave_out, near_misses=near_misses,
                     places=places, other_places=titles.clean(data["other_places"], {p.casefold() for p in places}),
                     eligible_where=_where(data["eligible_where"]), tags=builtins,
                     custom_tags=_custom(data["custom_tags"]), usage=reply.usage)


def _well_formed(data) -> bool:
    if not isinstance(data, dict) or set(data) != {*LISTS, "eligible_where", "custom_tags"}:
        return False
    lists = all(isinstance(data[k], list) and all(isinstance(v, str) for v in data[k]) for k in LISTS)
    custom = isinstance(data["custom_tags"], list) and all(
        isinstance(c, dict) and set(c) == {"name", "question", "answers"} and isinstance(c["name"], str)
        and isinstance(c["question"], str) and isinstance(c["answers"], list)
        and all(isinstance(a, str) for a in c["answers"]) for c in data["custom_tags"])
    return lists and custom and isinstance(data["eligible_where"], str)


def _where(text: str) -> str:
    where = one_line(text) if encodable(text) else ""
    return where if len(where) <= LONGEST_WHERE else ""


def _custom(raw: list[dict]) -> tuple[CustomTag, ...]:
    """Suggested custom tags the page can show and save as rows: each bounded, none named like a built-in or asking
    another's question (a built-in's question spans lines, a suggestion's never does); one breaking a rule is
    dropped."""
    kept: list[CustomTag] = []
    for item in raw:
        name, question = item["name"].strip(), one_line(item["question"]) if encodable(item["question"]) else ""
        answers = [one_line(a) if encodable(a) else "" for a in item["answers"]]
        if not (CUSTOM_TAG_NAME.fullmatch(name) and len(name) <= LONGEST_NAME
                and name not in (*tag.builtin_names(), tag.AGENCY)
                and name not in {k.name for k in kept} and question and len(question) <= LONGEST_QUESTION
                and question.casefold() not in {k.definition.casefold() for k in kept}
                and FEWEST_ANSWERS <= len(answers) <= MOST_ANSWERS
                and all(a and len(a) <= LONGEST_ANSWER for a in answers)):
            continue
        try:  # the profile's own rules too: answers all different
            kept.append(custom_tag(name, {"definition": question, "values": answers}, "a suggested tag"))
        except ConfigError:
            continue
        if len(kept) == MOST_CUSTOM:
            break
    return tuple(kept)
