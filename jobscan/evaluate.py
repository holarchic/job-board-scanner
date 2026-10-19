"""`jobscan eval`: score the production tagging path against a labelled posting set, as G2 scored the spike."""
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from jobscan import tag
from jobscan.config import Config, ConfigError, Profile
from jobscan.records import Posting

ELIGIBLE_WHERE = "the UK"  # the G2 set's labels answer UK eligibility
MINORITY = {"founding": lambda v: v == "yes", "location_eligible": lambda v: v == "no",
            "language_required": lambda v: v != "no"}  # the gate tags and their minority answer
BAR_ACCURACY, BAR_RECALL, BAR_QUOTES = 0.90, 0.80, 0.95


@dataclass(frozen=True)
class LabelledPosting:
    id: str
    posting: Posting
    labels: dict[str, str]


@dataclass(frozen=True)
class TagScore:
    tag: str
    labelled: int
    predicted: int
    accuracy: float
    minority: int
    recall: float | None  # None: no minority answers in the set, which fails the gate
    precision: float | None
    clears: bool


@dataclass(frozen=True)
class EvalReport:
    postings: int
    tagged: int
    scores: list[TagScore]
    quotes: int
    quote_rate: float | None  # None: no quotes at all, which fails the gate
    usage: tag.Usage
    unreasoned: int  # tagged postings whose replies carried no reasoning tokens
    passed: bool


def load_set(path: Path) -> list[LabelledPosting]:
    """Parse the whole set before any model call: a malformed row is a usage error, not a mid-run crash."""
    try:
        rows = json.loads(path.read_text())
    except (OSError, ValueError, RecursionError) as e:
        raise ConfigError(f"{path}: not readable JSON ({type(e).__name__})") from None
    if not isinstance(rows, list):
        raise ConfigError(f"{path}: must be a JSON list")
    items = [_labelled(path, n, row) for n, row in enumerate(rows)]
    if len({i.id for i in items}) != len(items):
        raise ConfigError(f"{path}: duplicate ids")
    return items


def _labelled(path: Path, n: int, row) -> LabelledPosting:
    fields = ("id", "title", "location", "description")
    if not isinstance(row, dict) or not all(isinstance(row.get(f), str) for f in fields) \
            or not isinstance(row.get("labels"), dict):
        raise ConfigError(f"{path}: row {n} needs string {', '.join(fields)} and a `labels` object")
    labels = {}
    for d in tag.definitions(tuple(MINORITY), ELIGIBLE_WHERE):
        if d.name not in row["labels"]:
            continue
        value = row["labels"][d.name]
        if not isinstance(value, str) or not value.strip() or (d.values and value.lower() not in d.values):
            raise ConfigError(f"{path}: row {n}: label `{d.name}` = {value!r} is not allowed")
        labels[d.name] = value.strip().lower()
    return LabelledPosting(id=row["id"], posting=Posting(key=row["id"], ats="eval", board="eval", company="",
                                                         title=row["title"], location=row["location"], url="",
                                                         description=row["description"]), labels=labels)


def run_eval(items: list[LabelledPosting], config: Config) -> EvalReport:
    """Tags every posting with all built-in tags and the agency check in one call each (the production prompt of a
    profile asking them all), scores the gate tags,
    and keeps the raw predictions under $JOBSCAN_HOME/eval/ for inspection."""
    defs = tag.profile_definitions(Profile(name="eval", titles=(), exclude_titles=(), locations=(),
                                           eligible_where=ELIGIBLE_WHERE, tags=tag.builtin_names(), custom_tags=()))
    attempts = tag.tag_all([i.posting for i in items], defs, config.model, config.api_key)
    predictions = {key: a.results for key, a in attempts.items() if isinstance(a, tag.TagSuccess)}
    _save(config.home, items, attempts)
    return score(items, predictions, usage=sum((a.usage for a in attempts.values()), tag.Usage()),
                 unreasoned=sum(1 for a in attempts.values()
                                if isinstance(a, tag.TagSuccess) and not a.reasoning_tokens))


def score(items: list[LabelledPosting], predictions: dict[str, dict[str, tag.TagResult]],
          usage: tag.Usage = tag.Usage(), unreasoned: int = 0) -> EvalReport:  # noqa: B008 -- Usage is frozen
    scores = [_score_tag(name, is_minority, items, predictions) for name, is_minority in MINORITY.items()]
    quotes = [r for results in predictions.values() for r in results.values() if r.quote is not None]
    quote_rate = sum(r.verified for r in quotes) / len(quotes) if quotes else None
    passed = all(s.clears for s in scores) and quote_rate is not None and quote_rate >= BAR_QUOTES
    return EvalReport(postings=len(items), tagged=len(predictions), scores=scores, quotes=len(quotes),
                      quote_rate=quote_rate, usage=usage, unreasoned=unreasoned, passed=passed)


def _score_tag(name, is_minority, items, predictions) -> TagScore:
    pairs = [(i.labels[name].lower(), _predicted(predictions.get(i.id), name)) for i in items if name in i.labels]
    right = [_same(name, truth, pred) for truth, pred in pairs]
    minority = [ok for (truth, _), ok in zip(pairs, right, strict=True) if is_minority(truth)]
    flagged = [ok for (_, pred), ok in zip(pairs, right, strict=True) if pred is not None and is_minority(pred)]
    accuracy = sum(right) / len(pairs) if pairs else 0.0
    recall = sum(minority) / len(minority) if minority else None
    return TagScore(tag=name, labelled=len(pairs), predicted=sum(p is not None for _, p in pairs), accuracy=accuracy,
                    minority=len(minority), recall=recall, precision=sum(flagged) / len(flagged) if flagged else None,
                    clears=accuracy >= BAR_ACCURACY and recall is not None and recall >= BAR_RECALL)


def _predicted(results: dict[str, tag.TagResult] | None, name: str) -> str | None:
    """The gate tags are single-valued; a missing prediction is None, which never matches a label."""
    value = results[name].value if results is not None and name in results else None
    return value.lower() if isinstance(value, str) else None


def _same(name: str, truth: str, pred: str | None) -> bool:
    if pred is None:
        return False
    if name == "language_required" and truth != "no" and pred != "no":
        return truth in pred or pred in truth  # "mandarin" vs "mandarin chinese", as G2 scored it
    return truth == pred


def _save(home: Path, items: list[LabelledPosting], attempts: dict[str, tag.TagAttempt]) -> None:
    directory = home / "eval"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    with (directory / f"{stamp}.jsonl").open("x") as f:
        for item in items:
            attempt = attempts.get(item.id)
            row = {"id": item.id, "labels": item.labels,
                   "error": (attempt.error if isinstance(attempt, tag.TagFailure) else None) if attempt
                   else "not started",
                   "usage": asdict(attempt.usage) if attempt else None,
                   "accepted_reasoning_tokens":
                       attempt.reasoning_tokens if isinstance(attempt, tag.TagSuccess) else None,
                   "tags": {k: asdict(v) for k, v in attempt.results.items()}
                   if isinstance(attempt, tag.TagSuccess) else None}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def format_report(report: EvalReport) -> str:
    def pct(x):
        return "n/a" if x is None else f"{x:.1%}"

    u = report.usage
    lines = [f"postings {report.postings} · tagged {report.tagged} · cost ${u.cost:.4f}"
             f"{'' if u.cost_reported else ' (incomplete: some replies reported no cost)'} · reasoning tokens "
             f"{u.reasoning_tokens:,} ({report.unreasoned} tagged postings without reasoning)"]
    for s in report.scores:
        lines.append(f"{s.tag:18} acc {pct(s.accuracy)} (n={s.labelled}, predicted {s.predicted}) · minority recall "
                     f"{pct(s.recall)} of {s.minority} · precision {pct(s.precision)}  {'✓' if s.clears else '✗'}")
    lines.append(f"quotes verbatim {pct(report.quote_rate)} of {report.quotes}  "
                 f"{'✓' if report.quote_rate is not None and report.quote_rate >= BAR_QUOTES else '✗'}")
    lines.append(f"G2 bar: {'PASS' if report.passed else 'FAIL'}")
    return "\n".join(lines)
