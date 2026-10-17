"""The set-up page as values, one step at a time: the draft a person builds (`Form`), the suggestions offered as tick
boxes (`Offers`), each step's posted fields merged into the draft (`read_step`, never raising), each step checked
on its own (`check_step`), the whole draft checked into a profile (`to_profile`), and every page as HTML (`render`,
pure). Secrets typed into the page are read but never rendered."""
import html
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from urllib.parse import quote_plus

from jobscan import init, report, schedule, tag
from jobscan.config import (
    CUSTOM_TAG_VALUES,
    ConfigError,
    CustomTag,
    EmailConfig,
    Profile,
    custom_tag,
    usable_key,
)
from jobscan.preview import Preview
from jobscan.titles import Outcome

REPOSITORY = "https://github.com/holarchic/job-board-scanner"
MOST_TAGS = 99  # custom tags the page edits, plus one blank row to add another; a profile with more is edited in
# its file
TEXT_LIMIT = 20_000  # characters in one field; a longer one is refused, never cut
LARGEST_POST = 256 * 1024  # bytes the server reads from one post
STEPS = ("intro", "key", "describe", "titles", "exclude", "places", "tags", "email", "schedule", "summary")
# An existing install is edited, not introduced or described again; `done`, after Save, is in neither order.
EXISTING_STEPS = tuple(s for s in STEPS if s not in ("intro", "describe"))
EDITABLE = tuple(s for s in EXISTING_STEPS if s != "summary")  # the summary's Edit buttons, new install or not
WAITING = MappingProxyType({  # (step, action) → what the page says while that post works
    ("describe", "next"): "Reading what you said…", ("titles", "suggest"): "Asking for more titles…",
    ("summary", "save"): "Saving, and sending a test email if you set one up…",
    ("summary", "preview"): "Counting what your search matches…"})
HEADINGS = MappingProxyType({
    "intro": "How it works", "key": "Your OpenRouter key", "describe": "What are you looking for?",
    "titles": "Job titles", "exclude": "Titles to leave out", "places": "Places", "tags": "Tags",
    "email": "Email (optional)", "schedule": "When to run", "summary": "Your search", "done": "Saved: what now"})
STEP_FIELDS = MappingProxyType({  # where each error shows; custom-N rows belong to tags
    "key": ("key",), "describe": ("description",), "titles": ("titles",), "exclude": ("exclude_titles",),
    "places": ("locations", "eligible_where"), "tags": ("tags",), "schedule": ("schedule_at",),
    "email": ("email", "email_to", "email_from", "email_host", "email_port", "password"), "summary": ("name", "")})
WHERE_UNSAID = "anywhere"  # fills location_eligible's question while the tags step is checked before a where is set


@dataclass(frozen=True)
class CustomRow:
    name: str = ""
    definition: str = ""
    values: str = ""  # one answer per line; empty: yes, no, unknown
    multi: bool = False
    default: str = ""


@dataclass(frozen=True)
class Form:
    """The draft: every list one value per line, as the person left it."""
    name: str = ""
    description: str = ""
    titles: str = ""
    exclude_titles: str = ""
    locations: str = ""
    eligible_where: str = ""
    tags: frozenset[str] = frozenset()
    custom: tuple[CustomRow, ...] = ()
    email_to: str = ""
    email_from: str = ""
    email_host: str = ""
    email_port: str = ""
    email_off: bool = False
    schedule_on: bool = False  # a daily run; the server fills both from this install's schedule
    schedule_at: str = ""
    key: str = field(default="", repr=False)  # typed in this post; never rendered
    password: str = field(default="", repr=False)
    too_long: frozenset[str] = frozenset()  # fields over TEXT_LIMIT, refused by the checks
    too_many: bool = False  # more custom tags than MOST_TAGS


@dataclass(frozen=True)
class Offers:
    """Suggestions shown as tick boxes beside the draft's own values; ticking one puts it in the draft."""
    titles: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()
    places: tuple[str, ...] = ()
    custom: tuple[CustomRow, ...] = ()
    adds: Mapping[str, int] = field(default_factory=dict)  # suggested title → stored postings it would add
    leaves_out: Mapping[str, int] = field(default_factory=dict)  # suggested exclusion → matches it would drop
    note: str = ""  # where the suggestions came from, and their cost


@dataclass(frozen=True)
class Checked:
    """The profile the form describes, or why not, by field ("" for the whole form)."""
    profile: Profile | None
    errors: Mapping[str, str]


@dataclass(frozen=True)
class Saved:
    """What the page after Save says, as display strings the server builds."""
    run: str  # the commands, home included when it isn't the default
    ui: str
    check: str
    digests: str  # the folder digests land in
    log: str
    email_to: str  # "" when email is off
    at: str  # "08:00" when launchd runs it daily, else ""
    cron: str  # off macOS with a time chosen: the line to add with `crontab -e`; else ""


@dataclass(frozen=True)
class View:
    """Everything one page shows."""
    form: Form
    offers: Offers
    step: str
    revision: int
    token: str
    catalogue: tuple[tag.BuiltinTag, ...]  # the built-in tags, loaded by the caller: rendering reads nothing
    fresh: bool  # no install yet: every step in order, the name asked and the key required
    home: str  # where the install lives, as shown
    launchd: bool  # this Mac can schedule a daily run; elsewhere a schedule is a cron line the person adds
    key_held: bool  # a key is saved, or typed earlier on this page
    password_held: bool
    email: EmailConfig | None  # saved email settings, shown without the password
    email_file: bool = False  # email.toml exists, loading or not: it can be turned off
    errors: Mapping[str, str] = field(default_factory=dict)  # by field; "" for the page
    notices: tuple[str, ...] = ()
    preview: Preview | None = None
    previewed: bool = False  # Preview pressed: None then means no postings yet
    saved: Saved | None = None  # the done page's facts
    schedule_kept: bool = False  # a launchd plist that isn't one daily time: kept unless a time is ticked


def steps(fresh: bool) -> tuple[str, ...]:
    return STEPS if fresh else EXISTING_STEPS


def form_from_profile(profile: Profile, email: EmailConfig | None) -> Form:
    return Form(name=profile.name, titles="\n".join(profile.titles), exclude_titles="\n".join(profile.exclude_titles),
                locations="\n".join(profile.locations), eligible_where=profile.eligible_where,
                tags=frozenset(profile.tags), custom=tuple(row(c) for c in profile.custom_tags),
                email_to=email.to if email else "", email_from=email.sender if email else "",
                email_host=email.host if email else "", email_port=str(email.port) if email else "")


def with_suggestions(offers: Offers, outcome: Outcome) -> Offers:
    """`jobscan titles`' suggestions joined to the offers, with their counts once there are postings to count."""
    s, c = outcome.suggested, outcome.counted
    return replace(offers, titles=_union(offers.titles, s.variants), excludes=_union(offers.excludes, s.near_misses),
                   adds={**offers.adds, **(c.adds if c else {})},
                   leaves_out={**offers.leaves_out, **(c.leaves_out if c else {})},
                   note=f"Suggested titles: model call {report.cost(s.usage)}."
                        + ("" if c else " Counts come after the first run."))


def default_name(form: Form) -> str:
    """A name for a new search from its first title (`account-executive`), else `my-search`."""
    first = next(iter(_lines(form.titles)), "")
    name = re.sub(r"[^a-z0-9]+", "-", first.casefold()).strip("-")[:40].strip("-")
    return name if init.PROFILE_NAME.fullmatch(name) else "my-search"


def read_step(step: str, fields: Mapping[str, list[str]], form: Form, offers: Offers) -> tuple[Form, Offers]:
    """`form` with the posted fields of `step` merged in, and the offers grown by what was typed; nothing is checked
    here. A list keeps the draft's values whose boxes stay ticked (in order, repeats kept), then ticked offers, then
    typed lines not already there; counts go once the lists they were counted against change."""
    long = set()

    def one(name: str, label: str | None = None) -> str:
        """The field's value, line endings as stored (a browser posts a textarea's as CRLF); one over TEXT_LIMIT is
        noted under `label` (the field shown beside its error)."""
        value = (fields.get(name) or [""])[0].replace("\r\n", "\n").replace("\r", "\n")
        if len(value) > TEXT_LIMIT:
            long.add(label or name)
        return value

    def merged(current: str, ticks: str, more: str, label: str) -> tuple[str, tuple[str, ...]]:
        ticked = fields.get(ticks, [])
        if any(len(t) > TEXT_LIMIT for t in ticked):
            long.add(label)
        values = [v for v in current.split("\n") if v and v in ticked]
        values += [t for t in dict.fromkeys(ticked) if t and t not in values]
        seen, added = {v.casefold() for v in values}, []
        for line in _lines(one(more, label)):  # a loop: lines repeated within the box count too
            if line.casefold() not in seen:
                seen.add(line.casefold())
                added.append(line)
        return "\n".join(values + added), tuple(added)

    changes: dict = {"key": one("key").strip(), "password": one("password")}
    match step:
        case "describe":
            changes["description"] = one("description").strip()
        case "titles":
            changes["titles"], added = merged(form.titles, "title", "title_more", "titles")
            offers = replace(offers, titles=_union(offers.titles, added))
        case "exclude":
            changes["exclude_titles"], added = merged(form.exclude_titles, "exclude", "exclude_more", "exclude_titles")
            offers = replace(offers, excludes=_union(offers.excludes, added))
        case "places":
            changes["locations"], added = merged(form.locations, "place", "place_more", "locations")
            changes["eligible_where"] = one("eligible_where").strip()
            offers = replace(offers, places=_union(offers.places, added))
        case "tags":
            changes["tags"] = frozenset(fields.get("tag", []))
            changes["custom"] = _rows(fields, one, offers)
            changes["too_many"] = len(changes["custom"]) > MOST_TAGS
        case "email":
            changes.update(email_to=one("email_to").strip(), email_from=one("email_from").strip(),
                           email_host=one("email_host").strip(), email_port=one("email_port").strip(),
                           email_off=one("email_off") == "on")
        case "schedule":
            changes.update(schedule_on=one("schedule_on") == "on", schedule_at=one("schedule_at").strip())
        case "summary":
            changes["name"] = one("name").strip() if "name" in fields else form.name
    new = replace(form, **changes, too_long=frozenset(long))
    if (new.titles, new.exclude_titles, new.locations) != (form.titles, form.exclude_titles, form.locations):
        offers = replace(offers, adds={}, leaves_out={})
    return new, offers


def _rows(fields: Mapping[str, list[str]], one, offers: Offers) -> tuple[CustomRow, ...]:
    """The custom rows as posted (a row with no name is removed, and nothing in it is checked), then each ticked
    offered tag no row is named like, as a row of its own from then on; a name typed twice stays for the check to
    refuse."""
    # Sorted as numbers without converting them (a 5,000-digit index would overflow int()); "1" and "01" kept apart.
    indexes = sorted({m.group(1) for name in fields if (m := re.fullmatch(r"custom-(\d+)-\w+", name))},
                     key=lambda i: (len(i.lstrip("0")), i.lstrip("0"), i))
    rows: list[CustomRow] = []
    for i in indexes:
        if not (fields.get(f"custom-{i}-name") or [""])[0].strip():
            continue
        at = f"custom-{len(rows)}"  # the row's place on the page, where its error shows
        rows.append(CustomRow(name=one(f"custom-{i}-name", at).strip(), definition=one(f"custom-{i}-definition", at),
                              values=one(f"custom-{i}-values", at), multi=(fields.get(f"custom-{i}-multi") or [""])[0]
                              == "on", default=one(f"custom-{i}-default", at).strip()))
    ticked = set(fields.get("add_custom", []))
    rows += [r for r in offers.custom if r.name in ticked and r.name not in {row.name for row in rows}]
    return tuple(rows)


def actions(step: str) -> frozenset[str]:
    """The buttons a step's page shows: the server accepts only these."""
    if step == "summary":
        return frozenset({"save", "preview", *(f"goto:{s}" for s in EDITABLE)})
    if step == "done":
        return frozenset({"goto:summary"})
    return frozenset({"next", *(() if step == "intro" else ("back",)),
                      *(("skip",) if step == "describe" else ()), *(("suggest",) if step == "titles" else ())})


def check_step(step: str, form: Form, *, key_held: bool, fresh: bool, password: str,
               email_saved: bool = False) -> dict[str, str]:
    """The step's own fields by the rules Save applies; other steps' fields never count. `password`: the app password
    typed or saved, for checking email settings without sending; `email_saved`: email.toml exists, so a blank
    address is refused unless email is turned off."""
    errors = _too_long(form, step)
    match step:
        case "key":
            if form.key and not usable_key(form.key):
                errors["key"] = "That isn't an OpenRouter key: paste it again."
            elif fresh and not (form.key or key_held):
                errors["key"] = "Your OpenRouter key is needed: openrouter.ai/keys."
        case "titles" | "exclude" | "places" | "tags" | "summary":
            errors = _profile_errors(step, form) | errors
        case "email":
            errors = _email_errors(form, password, email_saved) | errors
        case "schedule":
            errors = schedule_errors(form) | errors
    return errors


def schedule_errors(form: Form) -> dict[str, str]:
    """The schedule step's check, which Save repeats: Back skips a step's check."""
    if form.schedule_on and schedule.parse_at(form.schedule_at) is None:
        return {"schedule_at": "Use HH:MM, 24-hour, e.g. 08:00."}
    return {}


def _too_long(form: Form, step: str) -> dict[str, str]:
    return {name: f"Too long: over {TEXT_LIMIT:,} characters." for name in form.too_long if _step_of(name) == step}


def _profile_errors(step: str, form: Form) -> dict[str, str]:
    errors: dict[str, str] = {}
    if step == "titles":
        _values(errors, "titles", _lines(form.titles), "Give at least one title.")
    elif step == "exclude":
        _values(errors, "exclude_titles", _lines(form.exclude_titles), None)
    elif step == "places":
        _values(errors, "locations", _lines(form.locations), "Give at least one place.")
        _values(errors, "eligible_where", (form.eligible_where,) if form.eligible_where else (),
                "Say where you can work from.")
    elif step == "tags":
        errors |= _tag_errors(form)
    elif step == "summary" and not init.PROFILE_NAME.fullmatch(form.name):
        errors["name"] = "Use lower-case letters, digits and -, starting with a letter or digit."
    return errors


def _values(errors: dict[str, str], name: str, values: tuple[str, ...], missing: str | None) -> None:
    if name in errors:
        return
    if len("\n".join(values)) > TEXT_LIMIT:
        errors[name] = f"Too long: over {TEXT_LIMIT:,} characters."
    elif not values and missing:
        errors[name] = missing
    else:
        try:
            init.check_values(name.replace("_", " "), values)
        except ConfigError as e:
            errors[name] = str(e)


def _tag_errors(form: Form) -> dict[str, str]:
    custom, errors = _custom_tags(form)
    if form.too_many:
        errors["tags"] = f"More than {MOST_TAGS} custom tags: edit them in your profile file instead."
    if errors:
        return errors
    if not form.tags and not custom:
        return {"tags": "Tick at least one tag, or add your own."}
    names = [*form.tags, *(c.name for c in custom)]
    twice = sorted({n for n in names if names.count(n) > 1})
    if twice:
        return {"tags": f"{', '.join(twice)} named twice."}
    try:
        tag.definitions(tuple(sorted(form.tags, key=_tag_order)), form.eligible_where or WHERE_UNSAID, custom)
    except ConfigError as e:
        return {"tags": str(e)}
    return {}


def _custom_tags(form: Form) -> tuple[tuple[CustomTag, ...], dict[str, str]]:
    custom, errors = [], {}
    for i, row in enumerate(form.custom):
        if any(len(v) > TEXT_LIMIT for v in (row.name, row.definition, row.values, row.default)):
            errors[f"custom-{i}"] = f"Too long: over {TEXT_LIMIT:,} characters."
            continue
        table = {"definition": row.definition, "multi": row.multi}
        if _lines(row.values):
            table["values"] = list(_lines(row.values))
        if row.default:
            table["default"] = row.default
        try:
            custom.append(custom_tag(row.name, table, f"custom tag {row.name}"))
        except ConfigError as e:
            errors[f"custom-{i}"] = str(e)
    return tuple(custom), errors


def _email_errors(form: Form, password: str, email_saved: bool) -> dict[str, str]:
    if form.email_off:
        return {}
    if not form.email_to:
        return {"email_to": "To turn email off, tick the box; else give the address."} if email_saved else {}
    if form.email_port and not (form.email_port.isascii() and form.email_port.isdigit() and len(form.email_port) <= 5):
        return {"email_port": "The port is a number, 465 or 587."}
    if not password:
        return {"password": "Your app password is needed."}
    try:
        init.email_config(form.email_to, form.email_from or None, form.email_host or None,
                          int(form.email_port) if form.email_port else None, password)
    except ConfigError as e:
        return {"email": str(e)}
    return {}


def to_profile(form: Form) -> Checked:
    """The profile the draft describes, or why not: every profile step's checks, the page's size rule, then the
    profile validator the CLI uses."""
    errors: dict[str, str] = {}
    for step in ("titles", "exclude", "places", "tags", "summary"):
        errors |= _profile_errors(step, form) | _too_long(form, step)
    custom, _ = _custom_tags(form)
    profile = Profile(name=form.name, titles=_lines(form.titles), exclude_titles=_lines(form.exclude_titles),
                      locations=_lines(form.locations), eligible_where=form.eligible_where,
                      tags=tuple(sorted(form.tags, key=_tag_order)), custom_tags=custom)
    # Measured as the page would show the profile, not as typed (a vertical tab posts smaller than the line break it
    # becomes): one rule for saving and for offering a profile, so the page can always reopen what it saved, with room
    # left for the email settings and the browser's encoding.
    size = _posted_size(form_from_profile(profile, None))
    if size > LARGEST_POST // 2:
        errors.setdefault("", f"Too large for one page ({size:,} bytes): edit the file instead.")
    if errors:
        return Checked(None, errors)
    try:
        init.validate_profile(profile)
    except ConfigError as e:
        return Checked(None, {"": str(e)})
    return Checked(profile, {})


def search(form: Form) -> Profile | None:
    """The search as the draft stands, for title suggestions part-way through: its titles are all it needs."""
    titles = _lines(form.titles)
    if not titles:
        return None
    return Profile(name=form.name, titles=titles, exclude_titles=_lines(form.exclude_titles),
                   locations=_lines(form.locations), eligible_where=form.eligible_where, tags=(), custom_tags=())


def unrepresentable(profile: Profile) -> str | None:
    """Why the page can't edit this profile without changing it, or None when it can: the profile goes through the
    form and back (single-line fields lose line breaks in a browser, and lists split at every line break
    `str.splitlines` knows) and must come back the same."""
    if len(profile.custom_tags) > MOST_TAGS:
        return f"it has more than {MOST_TAGS} custom tags"
    if not init.PROFILE_NAME.fullmatch(profile.name):
        return f"its name, {profile.name!r}, isn't lower-case letters, digits and -: rename the file"
    single = (profile.name, profile.eligible_where, *(c.name for c in profile.custom_tags),
              *(c.default or "" for c in profile.custom_tags))
    if any(len(v.splitlines()) > 1 or "\n" in v or "\r" in v for v in single):
        return "a one-line answer in it holds a line break"
    if any("\r" in c.definition for c in profile.custom_tags):  # posted back, it would become a plain line break
        return "a custom tag's definition holds a carriage return"
    checked = to_profile(form_from_profile(profile, None))
    if checked.profile is None:
        return "; ".join(checked.errors.values())
    in_order = sorted(profile.tags, key=_tag_order)
    if replace(checked.profile, tags=tuple(in_order)) != replace(profile, tags=tuple(in_order)):
        return "a value in it holds a line break, which the page would split in two"
    return None


def _posted_size(form: Form) -> int:
    """Roughly what a browser posts for this form: every value percent-encoded, line breaks as CRLF."""
    values = [form.name, form.titles, form.exclude_titles, form.locations, form.eligible_where,
              *(v for r in form.custom for v in (r.name, r.definition, r.values, r.default))]
    return sum(len(quote_plus(v.replace("\n", "\r\n"))) + 32 for v in values)


def _step_of(name: str) -> str:
    if name.startswith("custom-"):
        return "tags"
    return next((s for s, names in STEP_FIELDS.items() if name in names), "summary")


def row(c: CustomTag) -> CustomRow:
    """A custom tag as the page's editable row."""
    return CustomRow(name=c.name, definition=c.definition,
                     values="" if c.values == CUSTOM_TAG_VALUES else "\n".join(c.values), multi=c.multi,
                     default=c.default or "")


def _tag_order(name: str) -> tuple[int, str]:
    """Built-in tags in the catalogue's order; an unknown name last, for the check to refuse."""
    names = tag.builtin_names()
    return (names.index(name) if name in names else len(names), name)


def _lines(text: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in text.splitlines() if line.strip())


def _union(first: tuple[str, ...], more) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*first, *more)))


# Rendering: pure, every value escaped.

def blocked(message: str) -> str:
    """The page when the install can't be edited here: why, and nothing to press."""
    return (f'{_HEAD}<main><h1>This install can\'t be edited here</h1><p class="error">{_x(message)}</p>'
            "</main></body></html>")


def waiting(message: str) -> str:
    """The top of a slow post's page, sent before the work: a spinner over everything until the rest arrives."""
    return (f'{_HEAD}<div class="waiting" role="status"><div class="spinner"></div><p>{_x(message)}</p>'
            '<p class="hint">This takes a few seconds.</p></div>')


def after_waiting(html: str) -> str:
    """The rest of a page whose top was `waiting`: the spinner hidden, then the page itself."""
    return "<style>.waiting{display:none}</style>" + html.removeprefix(_HEAD)


def render(view: View) -> str:
    numbered = steps(view.fresh)[1:]  # from the key: the intro goes unnumbered, the summary fills the bar
    at = numbered.index(view.step) + 1 if view.fresh and view.step in numbered else 0
    parts = [_HEAD]
    if at:
        done = 100 * at // len(numbered)
        parts.append(f'<div class="progress" role="progressbar" aria-label="Step {at} of {len(numbered)}" '
                     f'aria-valuemin="0" aria-valuemax="100" aria-valuenow="{done}"><span style="width:{done}%">'
                     "</span></div>")
    number = f'<span class="num">{at} →</span>' if at and view.step != "summary" else ""
    parts += ["<main>", f"<h1>{number}{_x(HEADINGS[view.step])}</h1>"]
    parts += [f'<p class="notice">{_x(n)}</p>' for n in view.notices]
    if "" in view.errors:
        parts.append(f'<p class="error">{_x(view.errors[""])}</p>')
    parts.append(f'<form method="post" action="/"><input type="hidden" name="token" value="{_x(view.token)}">'
                 f'<input type="hidden" name="step" value="{_x(view.step)}">'
                 f'<input type="hidden" name="revision" value="{view.revision}">'
                 # Enter submits the form's first button: this one, out of sight, so Enter moves on, never Edit.
                 f'<button name="action" value="{_DEFAULT_ACTION.get(view.step, "next")}" tabindex="-1" '
                 'aria-hidden="true" style="position:absolute;left:-9999px">Next</button>')
    parts.append(_BODIES[view.step](view))
    parts.append(_buttons(view))
    parts.append("</form>")
    if view.step == "summary" and view.previewed:
        lines = report.preview_lines(view.form.name, view.preview) if view.preview \
            else ["No postings yet: after the first `jobscan run`, Preview counts what your search matches."]
        parts.append("<h2>Preview</h2><pre>" + _x("\n".join(lines)) + "</pre>")
    parts.append("</main></body></html>")
    return "\n".join(p for p in parts if p)


def _buttons(view: View) -> str:
    """The step's buttons from `actions` (the summary's Edit buttons sit in its table), in this order."""
    labels = {"save": "Save", "preview": "Preview matches",
              "next": "Start" if view.step == "intro" else "Next" if view.fresh else "Done",
              "skip": "Go on without suggestions", "back": "Back", "goto:summary": "Back to your search"}
    shown = actions(view.step)
    buttons = []
    for a in (a for a in labels if a in shown):
        kind = "primary" if a in _PRIMARY else "secondary"
        buttons.append(f'<button name="action" value="{a}" class="{kind}">{labels[a]}</button>')
    typed_in = view.step in _ENTER_MOVES_ON and (view.fresh or view.step != "summary")  # the name box: new only
    enter = ' <span class="enter">press <b>Enter ↵</b></span>' if typed_in else ""
    out = '<p class="buttons">' + " ".join(buttons) + enter + "</p>"
    if view.step == "summary":
        out += ("<p class=\"hint\">Saving rewrites your profile file; comments you wrote in it by hand aren't kept "
                "(the previous file stays beside it as <code>.toml.prev</code>).</p>")
    return out


def _intro_body(view: View) -> str:
    when = ("<p>It runs by itself once a day, at a time you choose, while you're logged in. If your Mac is asleep "
            "then (the lid closed), it runs when the Mac wakes, and several missed days make one run. If the Mac is "
            "switched off, it waits for the next day's time. You can also run it yourself whenever you like.</p>"
            if view.launchd else
            "<p>It runs when you run it, or daily from a line you add to your crontab, which runs only while the "
            "computer is awake.</p>")
    return ("<p class=\"lead\">jobscan reads tech companies' own job boards for you and writes you a digest of the "
            "new postings that fit your search.</p>"
            "<h2>What a run does</h2><ol>"
            "<li>Reads the job boards of thousands of companies, on Ashby, Greenhouse, Workday and seven other job "
            "sites. It finds the boards itself.</li>"
            "<li>Keeps the postings whose title and place match your search.</li>"
            "<li>Asks a language model your questions about each new one (your tags: is it a founding team? can you "
            "work from where you are?), each answer quoting the posting.</li>"
            "<li>Writes a digest of what's new since the last run, and emails it to you if you set that up.</li></ol>"
            "<p>A run takes around 45 minutes, mostly reading boards. Your OpenRouter key pays for the model: usually "
            "cents a run.</p>"
            f"<h2>Where it runs</h2><p>On this computer, not on a server. Everything lives in <code>{_x(view.home)}"
            "</code>; your key and your email password in files only you can read.</p>" + when +
            "<h2>What you'll do here</h2><p>About five minutes: your OpenRouter key, what you're looking for in your "
            "own words, ticks on what that suggests, email if you want it, and when to run. Nothing is saved until "
            "the last page.</p>")


def _key_body(view: View) -> str:
    held = "A key is held: leave this empty to keep it." if view.key_held else \
        "Paste it from openrouter.ai/keys. It's kept in a file only you can read, and pays for the model that " \
        "reads your words and tags each posting (cents a day)." if view.fresh else \
        "Needed only for an OpenRouter model."
    return ('<p><input type="password" name="key" aria-label="OpenRouter key" autocomplete="off" size="50" '
            'placeholder="sk-or-v1-…" autofocus></p>'
            f'<p class="hint">{_x(held)}</p>{_err("key", view.errors)}')


def _describe_body(view: View) -> str:
    return ('<p>Say what you\'re looking for, as you would to a friend: the job, the kind of company, where, and '
            "anything you care about.</p>"
            f'<p><textarea name="description" rows="5" autofocus placeholder="I\'m looking for account executive '
            f'jobs with founding teams in London, and I\'m happy to be remote.">{_x(view.form.description)}'
            "</textarea></p>"
            '<p class="hint">Next reads it with your key (well under a cent) and fills in the next steps for you to '
            "tick"
            + ("; changing what you said replaces the titles, places and tags chosen since" if view.form.description
               else "") + ".</p>"
            + _err("description", view.errors))


def _titles_body(view: View) -> str:
    return ("<p>Postings whose title holds one of these, as whole words, are kept. Tick the ones you want.</p>"
            + _note(view.offers)
            + _ticks("title", _lines(view.form.titles), view.offers.titles, view.offers.adds, "+")
            + _more("title_more", "Add other titles, one per line")
            + '<p><button name="action" value="suggest">Suggest more titles</button></p>' + _err("titles", view.errors))


def _exclude_body(view: View) -> str:
    return ("<p>A posting whose title holds one of these is skipped, even when it matches.</p>"
            + _ticks("exclude", _lines(view.form.exclude_titles), view.offers.excludes, view.offers.leaves_out, "-")
            + _more("exclude_more", "Add others, one per line") + _err("exclude_titles", view.errors))


def _places_body(view: View) -> str:
    return ("<p>A posting is kept when its location names one of these, as whole words: a city, a country, "
            "Remote.</p>"
            + _ticks("place", _lines(view.form.locations), view.offers.places, {}, "")
            + _more("place_more", "Add other places, one per line") + _err("locations", view.errors)
            + _text("eligible_where", "Where you can work from", view.form.eligible_where, view.errors,
                    "e.g. the UK, working from London: the location tag checks each posting against it"))


def _tags_body(view: View) -> str:
    form, e = view.form, view.errors
    rows = []
    for t in view.catalogue:
        checked = " checked" if t.name in form.tags else ""
        rows.append(f'<label class="choice"><input type="checkbox" name="tag" value="{_x(t.name)}"{checked}> '
                    f'<span><b>{_x(t.name)}</b>: {_x(t.summary)} <i>({_x(report.VALIDATED[t.validated])})</i>'
                    "</span></label>")
    named = {r.name for r in form.custom}
    offered = [r for r in view.offers.custom if r.name not in named]  # once ticked, a tag is an editable row
    suggested = ("<fieldset><legend>Suggested tags of your own: tick to add</legend>" + "".join(
        f'<label class="choice"><input type="checkbox" name="add_custom" value="{_x(r.name)}"> <span><b>'
        f"{_x(r.name)}</b>: {_x(r.definition)} <i>({_x(', '.join(_lines(r.values)) or 'yes, no, unknown')})</i>"
        "</span></label>"
        for r in offered) + "</fieldset>") if offered else ""
    return ("<p>Tags are questions about each posting, answered beside it in your digest with a quote from the ad: "
            "is it a founding role? can you work it from where you are?</p>"
            '<p class="notice">Tags never hide a posting. Every posting that matches your titles and places is in '
            "your digest, whatever its answers; tick the questions you'd like answered when you skim.</p>"
            + "<fieldset><legend>Built-in tags</legend>" + "".join(rows) + "</fieldset>" + _err("tags", e)
            + suggested + _custom(form, e))


def _custom(form: Form, e: Mapping[str, str]) -> str:
    rows = [*form.custom, CustomRow()]
    opened = " open" if form.custom or any(n.startswith("custom-") for n in e) else ""
    out = [f"<details{opened}><summary>Tags of your own</summary><fieldset><p class=\"hint\">Say what each answer "
           "means. Leave the name empty to remove one; fill the empty row to add one.</p>"]
    for i, row in enumerate(rows):
        multi = " checked" if row.multi else ""
        out.append(
            f'<div class="custom">{_err(f"custom-{i}", e)}'
            f'<label>Name <input name="custom-{i}-name" value="{_x(row.name)}"></label> '
            f'<label>Default <input name="custom-{i}-default" value="{_x(row.default)}"></label> '
            f'<label><input type="checkbox" name="custom-{i}-multi"{multi}> several answers</label><br>'
            f'<label>Definition<br><textarea name="custom-{i}-definition" rows="3">{_x(row.definition)}</textarea>'
            f'</label><br><label>Answers, one per line (empty: yes, no, unknown)<br>'
            f'<textarea name="custom-{i}-values" rows="2">{_x(row.values)}</textarea></label></div>')
    return "".join(out) + "</fieldset></details>"


def _email_body(view: View) -> str:
    f, e = view.form, view.errors
    saved = f"Now: digests to {view.email.to} from {view.email.sender}." if view.email else \
        "Now: your email settings don't load." if view.email_file else \
        "Now: off. Leave the address empty to keep it off."
    held = " An app password is held: leave it empty to keep it." if view.password_held else ""
    fields = ("email_from", "email_host", "email_port")
    other = " open" if any(getattr(f, n) for n in fields) or any(n in e for n in fields) else ""
    off = (' <label class="choice"><input type="checkbox" name="email_off"'
           f'{" checked" if f.email_off else ""}> turn email off</label>') \
        if view.email or view.email_file or f.email_off else ""
    return (f'<p class="hint">{_x(saved)}{_x(held)} A test email is sent when you save.</p>' + _MAIL_HELP
            + _err("email", e)
            + _text("email_to", "Send digests to", f.email_to, e)
            + '<label>App password <input type="password" name="password" autocomplete="off"></label>'
            + _err("password", e)
            # Gmail and iCloud need none of these: out of the way unless one is set or wrong.
            + f"<details{other}><summary>Another provider, or sending from another account</summary>"
            + _text("email_from", "From (empty: the same address)", f.email_from, e)
            + _text("email_host", "Mail server (empty: Gmail or iCloud's)", f.email_host, e)
            + _text("email_port", "Port (465 or 587)", f.email_port, e) + "</details>"
            + off)


def _schedule_body(view: View) -> str:
    f, e = view.form, view.errors
    ticked = " checked" if f.schedule_on else ""
    hint = ("Your Mac needs to be on and you logged in. Asleep at that time (the lid closed), it runs when the Mac "
            "wakes, and several missed days make one run; switched off, it waits for the next day's time. Pick a "
            "time your Mac is usually awake, such as when you start work. A notification shows when there's "
            "something new or a problem (you may need to allow it in System Settings, Notifications). Unticked, it "
            "runs only when you type jobscan run." if view.launchd and not view.schedule_kept else
            "Your Mac needs to be on and you logged in; a run missed while it slept runs when it wakes." if view.launchd
            else
            "jobscan can't see your crontab here. Tick to get the line to add with crontab -e after Save; it runs "
            "only while the computer is awake. Unticking removes nothing: delete the line yourself.")
    return ("<p>Each run looks for new postings; when there are any, it writes the digest and, with email set up, "
            "emails it to you. A quiet day sends nothing. Once a day is enough: boards change slowly.</p>"
            f'<label class="choice"><input type="checkbox" name="schedule_on"{ticked}> Run it every day at</label><p>'
            f'<input type="time" name="schedule_at" value="{_x(f.schedule_at)}">{_err("schedule_at", e)}</p>'
            + ('<p class="hint">This install has a schedule this page can\'t show (not one daily time): it is '
               "left as it is unless you tick a time, which replaces it.</p>" if view.schedule_kept else "")
            + f'<p class="hint">{_x(hint)}</p>')


def _done_body(view: View) -> str:
    s = view.saved
    if s is None:
        return ""
    by_itself = f", or wait: it runs by itself every day at {_x(s.at)}" if s.at else ""
    cron = (f"<p>To run it daily, add this line with <code>crontab -e</code>; nothing runs until you do (to stop, "
            f"delete the line):</p><pre>{_x(s.cron)}</pre>") if s.cron else ""
    email = f" and emails it to {_x(s.email_to)}" if s.email_to else ""
    return (f"<h2>Your first run</h2><p>Run it now in a terminal: <code>{_x(s.run)}</code>{by_itself}. A run takes "
            "around 45 minutes; the first also finds the boards and tags every posting your search matches, so it can "
            "take over an hour. Later runs tag only the new ones.</p>"
            + cron +
            f"<h2>Your digest</h2><p>Each run with something new writes it to <code>{_x(s.digests)}</code>{email}. "
            "A quiet run writes nothing.</p>"
            f"<h2>Changing your search</h2><p>Run <code>{_x(s.ui)}</code> again: it opens at your summary, each step "
            "one Edit away. After a first run, Preview matches shows what your search finds among the stored "
            "postings, and Suggest more titles counts what each title would add.</p>"
            f"<h2>If something's wrong</h2><p><code>{_x(s.check)}</code> says what doesn't load, and "
            f"<code>{_x(s.log)}</code> logs each run.</p>"
            f'<h2>Feedback and ideas</h2><p>Found a problem, or want something it doesn\'t do? '
            f'{_link(REPOSITORY + "/issues", "Open an issue")}: say what you ran and what happened, and leave out your '
            "key, your search and your digests. To build it yourself, fork the repository on GitHub, make a branch, "
            "change it with tests (<code>python3 -m unittest</code>) and open a pull request; for anything bigger "
            "than a small fix, open an issue first. "
            f'{_link(REPOSITORY + "/blob/main/CONTRIBUTING.md", "CONTRIBUTING.md")} has the rules, such as '
            "standard-library Python only.</p>"
            '<p class="hint">You can close this tab: Ctrl-C in the terminal stops the page.</p>')


def _summary_body(view: View) -> str:
    f, e = view.form, view.errors
    name = _text("name", "Name for this search", f.name, e, "lower-case letters, digits and -") if view.fresh \
        else f"<p>Search: <b>{_x(f.name)}</b></p>"
    custom = [r.name for r in f.custom]
    email = "off" if f.email_off or not f.email_to else f"to {f.email_to}"
    key = "held" if view.key_held else "none"
    if view.launchd:
        when = f"every day at {f.schedule_at}" if f.schedule_on else \
            "a schedule this page can't show, left as it is" if view.schedule_kept else "not scheduled"
    else:
        when = f"a cron line at {f.schedule_at}, added by you" if f.schedule_on else "not set here"
    shown = {"key": ("OpenRouter key", key), "titles": ("Titles", "; ".join(_lines(f.titles))),
             "exclude": ("Left out", "; ".join(_lines(f.exclude_titles)) or "none"),
             "places": ("Places", "; ".join(_lines(f.locations)) + f" (you can work from: {f.eligible_where})"),
             "tags": ("Tags", ", ".join([*(t.name for t in view.catalogue if t.name in f.tags), *custom]) or "none"),
             "email": ("Email", email), "schedule": ("When to run", when)}
    rows = []
    for step, (label, value) in ((s, shown[s]) for s in EDITABLE):
        mistakes = [m for n, m in e.items() if n and _step_of(n) == step]
        rows.append(f"<tr><th>{_x(label)}</th><td>{_x(value)}"
                    + "".join(f'<span class="error">{_x(m)}</span>' for m in mistakes)
                    + f'</td><td><button name="action" value="goto:{step}">Edit</button></td></tr>')
    return name + "<table>" + "".join(rows) + "</table>"


_BODIES = MappingProxyType({"intro": _intro_body, "key": _key_body, "describe": _describe_body,
                            "titles": _titles_body, "exclude": _exclude_body, "places": _places_body,
                            "tags": _tags_body, "email": _email_body, "schedule": _schedule_body,
                            "summary": _summary_body, "done": _done_body})
_DEFAULT_ACTION = MappingProxyType({"summary": "save", "done": "goto:summary"})  # Enter's button; else next
_PRIMARY = frozenset(_DEFAULT_ACTION.values()) | {"next"}  # the one bold button: what Enter presses
_ENTER_MOVES_ON = frozenset({"key", "email", "summary"})  # a one-line box to type in, none taking Enter as a line


def _link(url: str, text: str) -> str:
    """A link out that opens a tab of its own and sends no Referer."""
    return f'<a href="{_x(url)}" target="_blank" rel="noopener noreferrer">{_x(text)}</a>'



def _ticks(name: str, chosen: tuple[str, ...], offered: tuple[str, ...], counts: Mapping[str, int], sign: str) -> str:
    """One box per distinct value: the draft's (ticked), then the offers not among them (unticked)."""
    values = list(dict.fromkeys((*chosen, *offered)))
    if not values:
        return ""
    return "".join(f'<label class="choice"><input type="checkbox" name="{name}" value="{_x(v)}"'
                   f'{" checked" if v in chosen else ""}> {_x(v)}'
                   + (f" <i>{sign}{counts[v]}</i>" if v in counts else "") + "</label>" for v in values)


def _note(offers: Offers) -> str:
    return f'<p class="hint">{_x(offers.note)}</p>' if offers.note else ""


def _more(name: str, label: str) -> str:
    return f'<p><label>{_x(label)}<br><textarea name="{name}" rows="3"></textarea></label></p>'


def _text(name: str, label: str, value: str, e: Mapping[str, str], hint: str = "") -> str:
    hint_html = f' <span class="hint">{_x(hint)}</span>' if hint else ""
    return (f'<p><label>{_x(label)}<br><input name="{name}" value="{_x(value)}" size="50"></label>{hint_html}'
            f"{_err(name, e)}</p>")


def _err(name: str, e: Mapping[str, str]) -> str:
    return f'<span class="error">{_x(e[name])}</span>' if name in e else ""


def _x(text: str) -> str:
    return html.escape(text, quote=True)


# One question a screen, big type, a bar for progress: inline CSS only (the policy allows no script, font or image).
_HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>jobscan</title><style>
:root{--ink:#191919;--muted:#6b6b6b;--line:#d6d6d6;--accent:#0445af;--on-accent:#fff;--tint:rgba(4,69,175,.07);
--bg:#fff;--error:#b3261e;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--ink:#f1f1f1;--muted:#a0a0a0;--line:#3b3b3b;--accent:#8ab0ff;
--on-accent:#0b1530;--tint:rgba(138,176,255,.1);--bg:#141414;--error:#ff8a80}}
*{box-sizing:border-box}html{background:var(--bg);color:var(--ink)}
body{margin:0;font:18px/1.55 -apple-system,BlinkMacSystemFont,"Helvetica Neue",system-ui,sans-serif;
-webkit-font-smoothing:antialiased}
header{position:fixed;top:4px;left:0;right:0;padding:.9rem 1.5rem;font-weight:700;letter-spacing:-.02em;
background:var(--bg);z-index:1}
.progress{position:fixed;top:0;left:0;right:0;height:4px;background:var(--tint);z-index:2}
.progress span{display:block;height:100%;background:var(--accent)}
main{max-width:40rem;margin:0 auto;padding:clamp(5rem,18vh,11rem) 1.5rem 6rem;
animation:rise .45s cubic-bezier(.2,.7,.2,1)}
@keyframes rise{from{opacity:0;transform:translateY(24px)}to{opacity:1;transform:none}}
.waiting{position:fixed;inset:0;z-index:5;display:flex;flex-direction:column;align-items:center;
justify-content:center;gap:.4rem;background:var(--bg);font-size:1.3rem;text-align:center;padding:1.5rem}
.spinner{width:46px;height:46px;margin-bottom:1rem;border-radius:50%;border:4px solid var(--tint);
border-top-color:var(--accent);animation:spin .8s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){main{animation:none}.spinner{animation-duration:2.4s}}
h1{font-size:clamp(1.6rem,3.4vw,2.25rem);font-weight:500;line-height:1.25;letter-spacing:-.02em;margin:0 0 1.25rem}
h1 .num{color:var(--accent);font-size:.55em;font-weight:600;vertical-align:.3em;margin-right:.5em;white-space:nowrap}
h2{font-size:1.15rem;font-weight:600;margin:2rem 0 .4rem}p{margin:.6rem 0}ol{padding-left:1.3rem}li{margin:.35rem 0}
a{color:var(--accent)}.lead{font-size:1.3rem;line-height:1.45}.hint{color:var(--muted);font-size:15px}.enter{color:var(--muted);font-size:13px}
code,pre{font:15px ui-monospace,SFMono-Regular,Menlo,monospace}code{background:var(--tint);padding:.1em .35em;
border-radius:4px;overflow-wrap:anywhere}pre{background:var(--tint);padding:.8rem 1rem;border-radius:8px;
white-space:pre-wrap;overflow-wrap:anywhere}
label{display:block;margin:.5rem 0}
input:not([type]),input[type=password],input[type=time],textarea{display:block;width:100%;margin-top:.3rem;
font:inherit;font-size:1.35rem;color:var(--accent);background:transparent;border:0;border-bottom:2px solid
var(--muted);border-radius:0;padding:.35rem 0;outline:none;transition:border-color .2s}
input[type=time]{width:auto;min-width:9rem}textarea{resize:vertical;line-height:1.4}
input:focus,textarea:focus{border-bottom-color:var(--accent)}::placeholder{color:var(--muted);opacity:1}
.choice{display:flex;gap:.8rem;align-items:flex-start;margin:.5rem 0;padding:.65rem .9rem;border:1px solid
var(--line);border-radius:8px;cursor:pointer;transition:border-color .15s,box-shadow .15s,background .15s}
.choice:hover{border-color:var(--accent)}
.choice:has(input:checked){border-color:var(--accent);background:var(--tint);box-shadow:inset 0 0 0 1px var(--accent)}
.choice input{flex:none;width:1.15rem;height:1.15rem;margin:.22rem 0 0;accent-color:var(--accent)}
.choice i{color:var(--muted);font-style:normal;font-size:15px}
fieldset{border:0;padding:0;margin:1.75rem 0}legend{padding:0;margin-bottom:.4rem;font-size:13px;font-weight:600;
text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
button{font:inherit;font-size:16px;font-weight:600;padding:.55rem 1.1rem;border-radius:8px;cursor:pointer;
color:var(--ink);background:transparent;border:1px solid var(--line)}
button:hover{border-color:var(--accent)}
button.primary{font-size:19px;padding:.6rem 1.6rem;color:var(--on-accent);background:var(--accent);
border-color:var(--accent);box-shadow:0 4px 14px rgba(0,0,0,.12)}button.primary:hover{filter:brightness(1.1)}
.buttons{display:flex;flex-wrap:wrap;align-items:center;gap:.9rem;margin-top:2.2rem}
.error{display:block;color:var(--error);font-size:15px;margin:.35rem 0}
.notice{margin:.6rem 0;padding:.55rem .9rem;border-left:3px solid var(--accent);background:var(--tint);
border-radius:0 8px 8px 0}
table{width:100%;border-collapse:collapse;margin:1.2rem 0}th,td{text-align:left;vertical-align:top;
padding:.8rem .4rem;border-bottom:1px solid var(--line)}th{width:9.5rem;font-weight:500;font-size:15px;
color:var(--muted)}td:last-child{text-align:right;width:1%}td button{font-size:14px;padding:.3rem .8rem}
details{margin:1.2rem 0;padding:.7rem 1rem;border:1px solid var(--line);border-radius:8px}
summary{cursor:pointer;font-weight:600}details[open] summary{margin-bottom:.4rem}
details fieldset{margin:.4rem 0}
.custom{padding:.9rem 0;border-top:1px dashed var(--line)}.custom label{font-size:15px;color:var(--muted)}
.custom input:not([type]),.custom textarea{font-size:1.05rem;color:var(--ink)}
.custom .choice{display:inline-flex;padding:.3rem .7rem}
</style></head><body><header>jobscan</header>"""

# Checked 17 Oct 2026 against support.google.com/accounts/answer/185833, support.apple.com/102654 and /102525.
_MAIL_HELP = (
    "<details><summary>How to get an app password, and your mail server</summary>"
    "<p>Mail apps sign in with an app password: a separate password you make for one app, not the one you sign in "
    "with.</p>"
    f'<p><b>Gmail.</b> Turn on 2-Step Verification, then make an app password at '
    f'{_link("https://myaccount.google.com/apppasswords", "myaccount.google.com/apppasswords")}: name it jobscan and '
    "paste the 16 letters below. Google offers none to accounts under Advanced Protection or signing in only with "
    "security keys; on a work or school account it depends on your organisation.</p>"
    f'<p><b>iCloud.</b> With two-factor authentication on, sign in at '
    f'{_link("https://account.apple.com", "account.apple.com")}, open Sign-In and Security, then App-Specific '
    "Passwords, and generate one. Send from your @icloud.com address.</p>"
    "<p>Gmail and iCloud addresses need nothing else: the server and port fill themselves in.</p>"
    "<p><b>Other providers.</b> Search the web for your provider's SMTP settings: you need the outgoing mail server "
    "(its name usually starts with smtp.) and its port, 465 (SSL/TLS) or 587 (STARTTLS). Your full address is the "
    "login, and most providers want an app password made in their security settings. Outlook.com and Hotmail "
    "addresses can't be used: Microsoft lets mail apps sign in only with OAuth.</p>"
    "<p><b>From</b> is for sending from an account other than the one you read, such as a Gmail account sending to "
    "your work address. That account's app password goes below.</p></details>")
