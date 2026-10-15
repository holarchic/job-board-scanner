"""Text for the read-only set-up commands (`tags`, `check`, `preview`, `boards`): typed values in, lines out. The
commands' handlers in `cli` gather the values and print what these return."""
from pathlib import Path

from jobscan import fetch, tag
from jobscan.boards import Added, Entry
from jobscan.config import Config, EmailConfig, Profile
from jobscan.preview import Preview, Sample

VALIDATED = {"yes": "validated", "uk": "validated for the UK only", "no": "not validated"}
WHERE = "<where you can work from>"  # stands in for `eligible_where` before there is a profile


def tag_list(catalogue: tuple[tag.BuiltinTag, ...], profile: Profile | None) -> list[str]:
    """Every built-in tag, then the profile's custom ones; `*` marks the tags the profile asks."""
    used = set(profile.tags) if profile else set()
    width = max(len(t.name) for t in catalogue)
    lines = ["Built-in tags" + (" (* = in your profile)" if profile else "") + ":"]
    for t in catalogue:
        lines.append(f"  {'*' if t.name in used else ' '} {t.name:<{width}}  {t.summary}  [{VALIDATED[t.validated]}]")
    if profile and profile.custom_tags:
        lines.append("Your custom tags (every one is asked; none is validated):")
        lines += [f"  * {c.name:<{width}}  {c.definition.splitlines()[0]}" for c in profile.custom_tags]
    lines.append("`jobscan tags NAME` shows a tag's full definition, as the model reads it.")
    return lines


def tag_detail(found: tag.Found, profile: Profile | None) -> list[str]:
    """The tag's definition as the model reads it, with its answers."""
    d = found.definition
    if found.builtin is None:
        kind = "custom tag, not validated"
    else:
        status = found.builtin.validated
        if status == "uk" and profile:
            status = "yes" if d.validated else "no"  # what the digest will mark, for this profile's place
        kind = f"built-in tag, {VALIDATED[status]}"
    answers = "free text" if d.values is None else ", ".join(d.values) + (" (a list of these)" if d.multi else "")
    asked = profile is not None and (d.name in profile.tags or d.name in {c.name for c in profile.custom_tags})
    in_use = "" if profile is None else "; in your profile" if asked else "; not in your profile"
    return [f"{d.name} ({kind}{in_use})", f"Answers: {answers}" + (f"; default {d.default}" if d.default else ""),
            "", d.definition]


def install(config: Config, profile: Profile, email: EmailConfig | None, listed: int | None, unrecognised: int,
            key_source: str, warnings: list[str]) -> list[str]:
    """What `check` loaded, as `run` will use it. `listed`: the boards file's boards (None: no file), beside
    `unrecognised` entries no adapter knows."""
    profile_path = config.home / "profiles" / f"{profile.name}.toml"
    custom = f"; custom: {', '.join(c.name for c in profile.custom_tags)}" if profile.custom_tags else ""
    lines = [
        f"Install: {config.home}",
        f"Profile: {profile_path}",
        f"  titles: {_items(profile.titles)}",
        f"  excluded titles: {_items(profile.exclude_titles)}",
        f"  locations: {_items(profile.locations)}",
        f"  where you can work from: {profile.eligible_where}",
        f"  tags: {', '.join(profile.tags) or '(none)'}{custom}",
        f"Model: {config.model.slug} at {config.model.base_url} (key: {key_source})",
        f"Email: digests to {email.to} from {email.sender} ({email.host}:{email.port})" if email
        else "Email: off (`jobscan email` sets it up)",
    ]
    if listed is None:
        lines.append("Boards file: none, so discovered boards only (`jobscan boards add URL` starts one)")
    else:
        unknown = f", {unrecognised} not recognised" if unrecognised else ""
        lines.append(f"Boards file: {config.boards_file} ({listed} boards{unknown})")
    lines += [f"Warning: {w}" for w in warnings]
    lines.append("OK: `jobscan run` can start.")
    return lines


def _items(values: tuple[str, ...]) -> str:
    return "; ".join(values) or "(none)"


def board_list(file: Path, entries: list[Entry]) -> list[str]:
    if not entries:
        return [f"No boards file yet ({file}): runs read discovered boards only. `jobscan boards add URL` adds one."]
    lines = [f"{file}: {len(entries)} entries"]
    for e in entries:
        ats = fetch.ADAPTERS[e.board.ats].DISPLAY_NAME if e.board else "not recognised: a run skips it"
        lines.append(f"  {e.company or '(name looked up)'}  {e.url}  [{ats}]")
    return lines


def boards_added(file: Path, outcome: Added) -> list[str]:
    lines = [f"added {b.ats}:{b.slug}" for b in outcome.added]
    lines += [f"already listed {b.ats}:{b.slug}" for b in outcome.listed_already]
    if outcome.added:
        lines.append(f"Saved {file}; the next run reads {'it' if len(outcome.added) == 1 else 'them'}.")
    return lines


def preview_lines(profile: Profile, p: Preview) -> list[str]:
    lines = [
        f"{profile.name}, against the {p.open:,} open postings stored for the boards the next run reads:",
        f"  matching your titles and locations: {p.matched:,} postings",
        f"  new jobs the next run would tag: {p.new_jobs:,} (about ${p.cost:.2f})",
        f"  already sent to you: {p.sent:,}",
        f"  left out by your excluded titles: {p.excluded:,}",
    ]
    if p.new_samples:
        lines += ["New jobs, newest first:", *map(_sample, p.new_samples)]
    if p.excluded_samples:
        lines += ["Left out by your excluded titles:", *map(_sample, p.excluded_samples)]
    lines.append("Workday sites store only postings in the places of the last run: a place just added finds Workday "
                 "jobs after the next run.")
    return lines


def _sample(s: Sample) -> str:
    return f"  {s.company} — {s.title} — {s.location or '(no location)'}"
