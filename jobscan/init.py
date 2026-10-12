"""`jobscan init`: asks for the search in the terminal, then writes config.toml, the profile and the key file.

Nothing is written until every answer is in and the install it makes loads; a failure part-way through tries to
remove whatever this invocation created and names anything it couldn't."""
import getpass
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from jobscan import tag
from jobscan.config import KEY_FILE, ConfigError, load_config, load_profile, usable_key

CONFIG = """# The model that tags postings: DeepSeek V4 Flash on OpenRouter, pinned to one provider and quantisation.
[model]
base_url = "https://openrouter.ai/api/v1"
slug = "deepseek/deepseek-v4-flash"
provider = "parasail"
quantization = "fp8"
"""
CUSTOM_TAGS_EXAMPLE = """
# Your own tags: uncomment and edit. Each one is asked of every matching posting and marked ⓘ in the digest,
# since no labelled set has checked it.
# [custom_tags.b2b]
# definition = "The company sells to businesses, not consumers."
# values = ["yes", "no", "unknown"]
"""
NEXT_STEPS = """
Next:
  jobscan run                    the first run finds about 4,000 job boards, then tags every matching posting:
                                 a few hundred, so anything from ten minutes to most of an hour, and roughly
                                 $0.05 to $0.30. Later runs take minutes.
  jobscan schedule --at 08:00    then a run every day, with a notification when there's something new.

Change your search, or add your own tags, in {profile}"""
# Titles a plain match would let through for a common search; offered as the starting exclusion list.
NEAR_MISSES = MappingProxyType({
    "product manager": ("Product Marketing", "Project Manager", "Program Manager", "Programme Manager"),
    "account executive": ("Account Manager", "Executive Assistant", "Sales Development"),
})
PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")


class InitCancelled(Exception):
    """Ctrl-C or end of input while asking: nothing was written."""


@dataclass(frozen=True)
class Setup:
    config: Path
    profile: Path
    key: Path


@dataclass(frozen=True)
class File:
    text: str
    mode: int


@dataclass(frozen=True)
class Answers:
    name: str
    titles: list[str]
    exclude_titles: list[str]
    locations: list[str]
    eligible_where: str
    tags: list[str]
    key: str  # always written to the key file: a scheduled run has no shell environment


def init(home: Path) -> Setup:
    """Asks, writes, and returns the files written. Raises ConfigError when the install exists already or a file
    can't be written, InitCancelled on Ctrl-C."""
    existing = [p for p in [home / "config.toml", home / KEY_FILE, *sorted((home / "profiles").glob("*.toml"))]
                if p.exists()]
    if existing:
        raise ConfigError(f"{home} is already set up ({existing[0].name} exists); edit its files instead")
    try:
        answers = _ask()
    except (KeyboardInterrupt, EOFError):
        raise InitCancelled from None
    setup = Setup(config=home / "config.toml", profile=home / "profiles" / f"{answers.name}.toml",
                  key=home / KEY_FILE)
    files = {setup.config: File(CONFIG, 0o644), setup.profile: File(profile_text(answers), 0o644),
             setup.key: File(answers.key + "\n", 0o600)}
    try:
        _check(answers, home, files)
    except OSError as e:
        raise ConfigError(f"could not check the new install in a temporary directory ({type(e).__name__})") from None
    _write(home, files)
    return setup


def profile_text(answers: Answers) -> str:
    return "\n".join([
        f"titles = {_list(answers.titles)}",
        f"exclude_titles = {_list(answers.exclude_titles)}",
        f"locations = {_list(answers.locations)}",
        f"eligible_where = {_string(answers.eligible_where)}",
        f"tags = {_list(answers.tags)}",
        CUSTOM_TAGS_EXAMPLE,
    ])


def _string(value: str) -> str:
    """A TOML basic string. JSON escapes quotes, backslashes and C0 controls the way TOML does; non-ASCII stays
    literal (a JSON surrogate-pair escape isn't valid TOML); DEL is the one control JSON leaves raw."""
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _list(values: list[str]) -> str:
    return "[" + ", ".join(_string(v) for v in values) + "]"


def _ask() -> Answers:
    print("Set up jobscan. Separate several answers with commas.\n")
    name = _answer("Name for this search (lower-case letters, digits and -)", "my-search",
                   lambda a: PROFILE_NAME.fullmatch(a) is not None)
    titles = _split(_answer("Job titles to look for (e.g. Product Manager, Senior Product Manager)", None, _items))
    misses = [m for t in titles for m in NEAR_MISSES.get(t.lower(), ()) if m not in titles]
    exclude = _answer("Titles to leave out, even if they match (- for none)", ", ".join(misses) or "-",
                      lambda a: a == "-" or _items(a))
    locations = _split(_answer("Locations to keep, as job ads write them (e.g. London, UK, Remote)", None, _items))
    eligible_where = _answer("Where you can work from, in a few words (e.g. the UK, working from London)", None,
                             bool)
    print("\nTags: the model answers each one for every matching posting, quoting the ad.")
    tags = _choose_tags()
    return Answers(name=name, titles=titles, exclude_titles=[] if exclude == "-" else _split(exclude),
                   locations=locations, eligible_where=eligible_where, tags=tags, key=_ask_key())


def _ask_key() -> str:
    """Saved to the key file either way: the scheduled run can't see the shell's OPENROUTER_API_KEY."""
    from_env = os.environ.get("OPENROUTER_API_KEY", "").strip()
    hint = "Enter to save the one in OPENROUTER_API_KEY" if usable_key(from_env) else "from openrouter.ai/keys"
    while True:
        key = getpass.getpass(f"\nOpenRouter API key (hidden; {hint}): ").strip() or from_env
        if usable_key(key):
            return key
        print("  That isn't a key: paste it again, with no spaces.")


def _choose_tags() -> list[str]:
    while True:
        tags = [n for n, summary in tag.builtin_summaries().items()
                if _answer(f"  {n} — {summary} [y/n]", "y", lambda a: a in ("y", "n")) == "y"]
        if tags:
            return tags
        print("  Pick at least one; you can add your own tags to the profile afterwards.")


def _answer(question: str, default: str | None, valid) -> str:
    prompt = f"{question}{f' [{default}]' if default else ''}: "
    while True:
        answer = input(prompt).strip() or (default or "")
        if _encodable(answer) and valid(answer):
            return answer
        print("  Not a valid answer; try again.")


def _encodable(text: str) -> bool:
    try:
        text.encode()
    except UnicodeEncodeError:  # a lone surrogate from a terminal that sent undecodable bytes
        return False
    return True


def _items(answer: str) -> bool:
    return bool(_split(answer))


def _split(answer: str) -> list[str]:
    return [item.strip() for item in answer.split(",") if item.strip()]


def _check(answers: Answers, home: Path, files: dict[Path, File]) -> None:
    """The install must load exactly as answered, through the loaders `run` uses, as a scheduled run sees it: with
    no shell environment."""
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        (root / "profiles").mkdir()
        for path, file in files.items():
            target = root / path.relative_to(home)
            target.write_text(file.text, encoding="utf-8")
            target.chmod(file.mode)
        load_config(root, environ={})
        profile = load_profile(root)
    expected = {"titles": answers.titles, "exclude_titles": answers.exclude_titles, "locations": answers.locations,
                "eligible_where": answers.eligible_where, "tags": answers.tags}
    read_back = {"titles": list(profile.titles), "exclude_titles": list(profile.exclude_titles),
                 "locations": list(profile.locations), "eligible_where": profile.eligible_where,
                 "tags": list(profile.tags)}
    if read_back != expected:
        raise ConfigError("the profile didn't read back as written")
    tag.definitions(profile.tags, profile.eligible_where, profile.custom_tags)


def _write(home: Path, files: dict[Path, File]) -> None:
    created: list[Path] = []
    try:
        for directory in (home, home / "profiles"):
            if not directory.exists():
                directory.mkdir()
                created.append(directory)
        for path, file in files.items():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, file.mode)
            created.append(path)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(file.text)
    except BaseException as e:
        left = _remove(created)
        kept = f"delete {', '.join(map(str, left))} before trying again" if left else "nothing was kept"
        if isinstance(e, OSError):
            raise ConfigError(f"could not write {home} ({type(e).__name__}); {kept}") from None
        if left:
            e.add_note(f"jobscan init: {kept}")
        raise


def _remove(created: list[Path]) -> list[Path]:
    """Removes what this invocation created, newest first, trying every path; returns what couldn't be removed."""
    left = []
    for path in reversed(created):
        try:
            path.rmdir() if path.is_dir() else path.unlink()
        except OSError:
            left.append(path)
    return left
