"""LLM tagging: tag definitions + posting → one model call → validated values with checked quotes.

Shared by `run` and `eval`, so eval measures exactly the production path."""
import hashlib
import http.client
import json
import math
import re
import threading
import time
import tomllib
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from jobscan.config import ConfigError, CustomTag, ModelConfig
from jobscan.records import Posting

TRIES = 3
PAUSE = 5  # seconds before a retry
# Provider rate limits (429) outlast a short retry. A headless run can afford to wait, and every posting
# given up on is a day late; so 429s get their own, longer schedule: up to len(RATE_LIMIT_PAUSES) + 1 tries.
RATE_LIMIT_PAUSES = (30, 60, 120, 240)
TIMEOUT = 300
WORKERS = 4
STOP_AFTER_FAILURES = 3  # consecutive failed postings → treat the model as unavailable
UNKNOWN = "unknown"
# G2 validated `location_eligible` for UK eligibility only.
UK = re.compile(r"(?<!\w)(UK|U\.K\.|United Kingdom|Britain|England|Scotland|Wales|Northern Ireland|London)(?!\w)",
                re.IGNORECASE)


@dataclass(frozen=True)
class TagDef:
    name: str
    definition: str
    values: tuple[str, ...] | None  # None: free text
    multi: bool
    default: str | None
    validated: bool  # an `eval` has passed this tag; shown in the digest, not part of the question

    @property
    def hash(self) -> str:
        """Identity of the question asked; an edited definition invalidates cached answers."""
        return hashlib.sha256(json.dumps([self.definition, self.values, self.multi, self.default]).encode()).hexdigest()


@dataclass(frozen=True)
class TagResult:
    value: str | tuple[str, ...]
    quote: str | None
    verified: bool  # quote found in the posting text
    warn: bool  # a quote was needed and is missing or not found: shown with ⚠


@dataclass(frozen=True)
class Usage:
    """Billing and reasoning evidence summed over every reply received for one operation (a posting's tags, a
    titles request), rejected ones included."""
    cost: float = 0.0
    cost_reported: bool = True  # False if any reply came without a cost (local endpoints never report one)
    reasoning_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.cost + other.cost, self.cost_reported and other.cost_reported,
                     self.reasoning_tokens + other.reasoning_tokens)


@dataclass(frozen=True)
class TagSuccess:
    results: dict[str, TagResult]
    usage: Usage  # every reply for this posting, rejected ones included
    reasoning_tokens: int  # the accepted reply's alone: evidence that the answer used reasoning


@dataclass(frozen=True)
class TagFailure:
    error: str  # safe to log: an HTTP status, an exception class or a reply-shape problem, never request text
    http_status: int | None
    usage: Usage


TagAttempt = TagSuccess | TagFailure


@dataclass(frozen=True)
class Reply:
    content: str | None  # None when the response envelope carries no message text
    usage: Usage


class CallFailed(Exception):
    """The request got no reply: `reason` is the HTTP status or the exception's class, never its text (request
    errors can echo headers, and the key is one). `usage` says whether it may have been billed."""

    def __init__(self, reason: str, http_status: int | None, usage: "Usage"):
        super().__init__(reason)
        self.reason, self.http_status, self.usage = reason, http_status, usage


class ReplyError(Exception):
    """The model answered, but not with a valid tag object."""


def builtin_names() -> tuple[str, ...]:
    return tuple(_builtins())


@dataclass(frozen=True)
class BuiltinTag:
    """A built-in tag as people choosing tags see it; the model never sees `summary`."""
    name: str
    summary: str
    validated: str  # "yes", "no", or "uk": validated only when `eligible_where` names the UK


def builtins() -> tuple[BuiltinTag, ...]:
    return tuple(_builtin_tag(name, spec) for name, spec in _builtins().items())


@dataclass(frozen=True)
class Found:
    definition: TagDef  # as the model reads it
    builtin: BuiltinTag | None  # None: the profile's own tag


def lookup(name: str, eligible_where: str, custom: tuple[CustomTag, ...] = ()) -> Found:
    """One tag: a custom tag of that name, else the built-in. ConfigError when neither exists."""
    for c in custom:
        if c.name == name:
            return Found(definition=_custom(c), builtin=None)
    builtins = _builtins()
    if name not in builtins:
        raise ConfigError(f"no tag named {name!r}; `jobscan tags` lists them")
    return Found(definition=_builtin(name, builtins[name], eligible_where), builtin=_builtin_tag(name, builtins[name]))


def _builtin_tag(name: str, spec: dict) -> BuiltinTag:
    validated = spec.get("validated", False)
    return BuiltinTag(name=name, summary=spec["summary"],
                      validated="uk" if validated == "uk" else "yes" if validated is True else "no")


def _builtins() -> dict:
    return tomllib.loads((Path(__file__).parent / "tags.toml").read_text())


def definitions(tag_names: tuple[str, ...], eligible_where: str, custom: tuple[CustomTag, ...] = ()) -> list[TagDef]:
    """The built-ins named, then the custom tags. Two tags asking the same question are refused: cached answers are
    keyed by the question, so they would overwrite each other."""
    builtins = _builtins()
    unknown = [n for n in tag_names if n not in builtins]
    if unknown:
        raise ConfigError(f"unknown tags {unknown}; built-ins are {sorted(builtins)}")
    shadowing = [c.name for c in custom if c.name in builtins]
    if shadowing:
        raise ConfigError(f"custom tags {shadowing} use built-in names")
    defs = [_builtin(name, builtins[name], eligible_where) for name in tag_names] + [_custom(c) for c in custom]
    asked: dict[str, str] = {}
    for d in defs:
        if d.hash in asked:
            raise ConfigError(f"tag `{d.name}` asks the same question as `{asked[d.hash]}`")
        asked[d.hash] = d.name
    return defs


def _builtin(name: str, spec: dict, eligible_where: str) -> TagDef:
    validated = spec.get("validated", False)
    return TagDef(
        name=name,
        definition=spec["definition"].strip().replace("{eligible_where}", eligible_where),
        values=tuple(spec["values"]) if "values" in spec else None,
        multi=spec.get("multi", False),
        default=spec.get("default"),
        validated=bool(UK.search(eligible_where)) if validated == "uk" else validated is True,
    )


def _custom(tag: CustomTag) -> TagDef:
    # Built-in definitions state their own defaults; a custom one gets the line here, so the model knows it.
    default = f"\n- Default `{tag.default}`: the posting doesn't say." if tag.default else ""
    return TagDef(name=tag.name, definition=tag.definition + default, values=tag.values, multi=tag.multi,
                  default=tag.default, validated=False)


PROMPT = """You tag job postings. Apply the rules below exactly, using only the posting text.

## Ground rules
- **Posting text only:** title, location field and job description. Do not use what you know about the company from elsewhere; outside knowledge turns an `unknown` into a false `yes`.
- `unknown` means the posting doesn't say. A `no` needs text that rules the tag out; the absence of a `yes` is `unknown`, except where a tag below sets a different default.

## Tags
{tags}

## Output
Return JSON with every tag. `value` is one of the tag's allowed values.
`quote` is a short span (≤200 characters) copied verbatim from the title, location or description that supports the value, or null when the value is `unknown`, an empty list, or a default with nothing to cite.

## Posting
Quote text from inside the tags, never the tags themselves.
<title>{title}</title>
<location>{location}</location>
<description>
{description}
</description>"""  # noqa: E501 -- the model reads these lines as written; wrapping changes the evaluated prompt


def prompt(posting: Posting, defs: list[TagDef]) -> str:
    tags = "\n\n".join(f"### `{d.name}`\n{_allowed(d)}\n{d.definition}" for d in defs)
    return PROMPT.format(tags=tags, title=posting.title, location=posting.location, description=posting.description)


def _allowed(d: TagDef) -> str:
    if d.values is None:
        return f"Value: `{d.default}` or free text."
    listed = " · ".join(f"`{v}`" for v in d.values)
    return f"Value: a list of zero or more of {listed}." if d.multi else f"Value: one of {listed}."


def schema(defs: list[TagDef]) -> dict:
    def value_schema(d: TagDef) -> dict:
        if d.values is None:
            return {"type": "string"}
        if d.multi:
            return {"type": "array", "items": {"type": "string", "enum": list(d.values)}}
        return {"type": "string", "enum": list(d.values)}

    return {"type": "object", "additionalProperties": False, "required": [d.name for d in defs],
            "properties": {d.name: {"type": "object", "additionalProperties": False, "required": ["value", "quote"],
                                    "properties": {"value": value_schema(d), "quote": {"type": ["string", "null"]}}}
                           for d in defs}}


def tag_posting(posting: Posting, defs: list[TagDef], model: ModelConfig, api_key: str | None) -> TagAttempt:
    """Tries until success or the retry budget for the latest failure is spent; usage sums every try."""
    usage, tries = Usage(), 0
    while True:
        tries += 1
        outcome = _try_once(posting, defs, model, api_key)
        usage += outcome.usage
        if isinstance(outcome, TagSuccess):
            return TagSuccess(results=outcome.results, usage=usage, reasoning_tokens=outcome.reasoning_tokens)
        rate_limited = outcome.http_status == 429
        if tries >= (len(RATE_LIMIT_PAUSES) + 1 if rate_limited else TRIES):
            return TagFailure(error=outcome.error, http_status=outcome.http_status, usage=usage)
        time.sleep(RATE_LIMIT_PAUSES[min(tries, len(RATE_LIMIT_PAUSES)) - 1] if rate_limited else PAUSE)


def _try_once(posting: Posting, defs: list[TagDef], model: ModelConfig, api_key: str | None) -> TagAttempt:
    """One request; the outcome's usage covers this try only."""
    try:
        reply = call(model, api_key, prompt(posting, defs), schema(defs), "tags")
    except CallFailed as e:
        return TagFailure(error=e.reason, http_status=e.http_status, usage=e.usage)
    try:
        if reply.content is None:
            raise ReplyError("no message content")
        return TagSuccess(results=parse_reply(reply.content, defs, posting), usage=reply.usage,
                          reasoning_tokens=reply.usage.reasoning_tokens)
    except ReplyError as e:
        return TagFailure(error=f"invalid reply: {e}", http_status=None, usage=reply.usage)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the Authorization header to wherever it points; fail instead (as an HTTPError)."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def call(model: ModelConfig, api_key: str | None, text: str, response_schema: dict, name: str) -> Reply:
    """One chat-completions request answering with JSON in `response_schema` (strict, called `name`): every model
    call goes through here, so each refuses redirects and pins the provider. Usage is read before anything is
    validated, so a rejected reply still counts. CallFailed when there is no reply; a reply without readable
    content (`content` None) is the caller's to refuse, with its usage."""
    body = {"model": model.slug, "temperature": 0, "usage": {"include": True},
            "messages": [{"role": "user", "content": text}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": name, "strict": True, "schema": response_schema}}}
    if model.provider:
        body["provider"] = {"only": [model.provider], "allow_fallbacks": False, "require_parameters": True,
                            **({"quantizations": [model.quantization]} if model.quantization else {})}
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {api_key}"} if api_key else {})}
    request = urllib.request.Request(f"{model.base_url}/chat/completions", data=json.dumps(body).encode(),
                                     headers=headers)
    try:
        with _OPENER.open(request, timeout=TIMEOUT) as response:
            try:
                reply = json.loads(response.read())
            # The request was accepted (and may be billed), so a cut-off or unreadable body is an unknown cost,
            # not an unbilled failure. RecursionError: deeply nested JSON on Python < 3.14.
            except (OSError, http.client.HTTPException, ValueError, RecursionError):
                reply = None
    except urllib.error.HTTPError as e:
        e.close()
        raise CallFailed(f"HTTP {e.code}", e.code, Usage()) from None
    except (OSError, http.client.HTTPException, ValueError) as e:
        # Unless the connection was refused, the request may have been sent and billed.
        unknown = Usage() if isinstance(e, ConnectionRefusedError) else Usage(cost_reported=False)
        raise CallFailed(type(e).__name__, None, unknown) from None
    if not isinstance(reply, dict):  # answered, possibly billed, but nothing readable: billing unknown
        return Reply(content=None, usage=Usage(cost_reported=False))
    usage = reply.get("usage") if isinstance(reply.get("usage"), dict) else {}
    details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    cost, reasoning = usage.get("cost"), details.get("reasoning_tokens")
    cost_ok = _finite_non_negative(cost)
    reasoning_ok = isinstance(reasoning, int) and not isinstance(reasoning, bool) and reasoning >= 0
    choices = reply.get("choices")
    message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) \
        else None
    content = message.get("content") if isinstance(message, dict) else None
    return Reply(content=content if isinstance(content, str) else None,
                  usage=Usage(cost=float(cost) if cost_ok else 0.0, cost_reported=cost_ok,
                              reasoning_tokens=reasoning if reasoning_ok else 0))


def _finite_non_negative(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:  # a JSON integer too large for a float
        return False


def parse_reply(content: str, defs: list[TagDef], posting: Posting) -> dict[str, TagResult]:
    try:
        data = json.loads(content)
    except (ValueError, RecursionError):  # also integers past the digit limit, and deeply nested JSON
        raise ReplyError("not JSON") from None
    if not isinstance(data, dict) or set(data) != {d.name for d in defs}:
        raise ReplyError("not an object with exactly the requested tags")
    return {d.name: _result(d, data[d.name], posting) for d in defs}


def _result(d: TagDef, cell, posting: Posting) -> TagResult:
    if not isinstance(cell, dict) or set(cell) != {"value", "quote"}:
        raise ReplyError(f"`{d.name}` is not a {{value, quote}} object")
    value, quote = _value(d, cell["value"]), cell["quote"]
    if quote is not None and not isinstance(quote, str):
        raise ReplyError(f"`{d.name}` quote is not a string")
    quote = quote.strip() if quote and quote.strip() else None
    verified = quote is not None and verify_quote(quote, posting)
    return TagResult(value=value, quote=quote, verified=verified, warn=_needs_quote(d, value) and not verified)


def _value(d: TagDef, raw):
    if d.multi:
        if not isinstance(raw, list) or any(v not in d.values for v in raw):
            raise ReplyError(f"`{d.name}` must be a list of {d.values}")
        return tuple(v for v in d.values if v in raw)  # canonical order, duplicates dropped
    if not isinstance(raw, str) or not raw.strip():
        raise ReplyError(f"`{d.name}` must be a non-empty string")
    value = raw.strip()
    if d.values is None:
        return d.default if d.default and value.lower() == d.default else value
    if value not in d.values:
        raise ReplyError(f"`{d.name}` value not in {d.values}")  # the rejected text itself is never logged
    return value


def _needs_quote(d: TagDef, value) -> bool:
    return value not in (UNKNOWN, (), d.default)


PUNCT = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                       "\u2010": "-", "\u2011": "-", "\u2013": "-", "\u2014": "-", "\u00a0": " "})


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).translate(PUNCT)).strip()


def verify_quote(quote: str, posting: Posting) -> bool:
    """True when the quote appears within one of the posting's title, location or description (never spanning
    two), ignoring Unicode compatibility forms, curly quotes, dash variants and whitespace runs.
    Blank quotes never verify."""
    needle = _normalise(quote)
    return bool(needle) and any(needle in _normalise(field)
                                for field in (posting.title, posting.location, posting.description))


def tag_all(postings: list[Posting], defs: list[TagDef], model: ModelConfig, api_key: str | None,
            ) -> dict[str, TagAttempt]:
    """Tag postings in parallel, keyed by posting key. After STOP_AFTER_FAILURES consecutive failures (in
    completion order) no further postings start; in-flight results are kept. Unstarted postings are absent."""
    stop = threading.Event()

    def work(posting: Posting) -> TagAttempt | None:  # None: not started because the breaker tripped
        return None if stop.is_set() else tag_posting(posting, defs, model, api_key)

    attempts, consecutive_failures = {}, 0
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(work, p): p.key for p in postings}
        for future in as_completed(futures):
            attempt = future.result()
            if attempt is None:
                continue
            attempts[futures[future]] = attempt
            consecutive_failures = 0 if isinstance(attempt, TagSuccess) else consecutive_failures + 1
            if consecutive_failures >= STOP_AFTER_FAILURES:
                stop.set()
    return attempts
