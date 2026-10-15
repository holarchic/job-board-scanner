"""Text for the read-only set-up commands (`tags`, `check`, `preview`, `boards`): typed values in, lines out. The
commands' handlers in `cli` gather the values and print what these return."""
from jobscan import tag
from jobscan.config import Config, EmailConfig, Profile

VALIDATED = {"yes": "validated", "uk": "validated for the UK only", "no": "not validated"}
WHERE = "<where you can work from>"  # stands in for `eligible_where` before there is a profile


def tag_list(profile: Profile | None) -> list[str]:
    """Every built-in tag, then the profile's custom ones; `*` marks the tags the profile asks."""
    used = set(profile.tags) if profile else set()
    width = max(len(t.name) for t in tag.builtins())
    lines = ["Built-in tags" + (" (* = in your profile)" if profile else "") + ":"]
    for t in tag.builtins():
        lines.append(f"  {'*' if t.name in used else ' '} {t.name:<{width}}  {t.summary}  [{VALIDATED[t.validated]}]")
    if profile and profile.custom_tags:
        lines.append("Your custom tags (every one is asked; none is validated):")
        lines += [f"  * {c.name:<{width}}  {c.definition.splitlines()[0]}" for c in profile.custom_tags]
    lines.append("`jobscan tags NAME` shows a tag's full definition, as the model reads it.")
    return lines


def tag_detail(name: str, profile: Profile | None) -> list[str] | None:
    """The tag's definition as the model reads it, with its answers; None when no tag has the name."""
    where = profile.eligible_where if profile else WHERE
    custom = {c.name: c for c in profile.custom_tags} if profile else {}
    if name in custom:
        d = tag.definitions((), where, (custom[name],))[0]
        kind = "custom tag, not validated"
    elif name in tag.builtin_names():
        d = tag.definitions((name,), where)[0]
        status = next(t.validated for t in tag.builtins() if t.name == name)
        if status == "uk" and profile:
            status = "yes" if d.validated else "no"  # what the digest will mark, for this profile's place
        kind = f"built-in tag, {VALIDATED[status]}"
    else:
        return None
    answers = "free text" if d.values is None else ", ".join(d.values) + (" (a list of these)" if d.multi else "")
    in_use = "" if profile is None else "; in your profile" if name in profile.tags or name in custom else \
        "; not in your profile"
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
