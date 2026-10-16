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

from jobscan import init, report, tag
from jobscan.config import (
    CUSTOM_TAG_VALUES,
    ConfigError,
    CustomTag,
    EmailConfig,
    Profile,
    custom_tag,
    usable_key,
)
from jobscan.describe import Described
from jobscan.preview import Preview
from jobscan.titles import Outcome

MOST_TAGS = 99  # custom tags the page edits, plus one blank row to add another; a profile with more is edited in
# its file
TEXT_LIMIT = 20_000  # characters in one field; a longer one is refused, never cut
LARGEST_POST = 256 * 1024  # bytes the server reads from one post
STEPS = ("key", "describe", "titles", "exclude", "places", "tags", "email", "summary")
EXISTING_STEPS = tuple(s for s in STEPS if s != "describe")  # an existing install is edited, not described again
HEADINGS = MappingProxyType({
    "key": "Your OpenRouter key", "describe": "What are you looking for?", "titles": "Job titles",
    "exclude": "Titles to leave out", "places": "Places", "tags": "Tags", "email": "Email (optional)",
    "summary": "Your search"})
STEP_FIELDS = MappingProxyType({  # where each error shows; custom-N rows belong to tags
    "key": ("key",), "describe": ("description",), "titles": ("titles",), "exclude": ("exclude_titles",),
    "places": ("locations", "eligible_where"), "tags": ("tags",),
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
class View:
    """Everything one page shows."""
    form: Form
    offers: Offers
    step: str
    revision: int
    token: str
    catalogue: tuple[tag.BuiltinTag, ...]  # the built-in tags, loaded by the caller: rendering reads nothing
    fresh: bool  # no install yet: every step in order, the name asked and the key required
    key_held: bool  # a key is saved, or typed earlier on this page
    password_held: bool
    email: EmailConfig | None  # saved email settings, shown without the password
    email_file: bool = False  # email.toml exists, loading or not: it can be turned off
    errors: Mapping[str, str] = field(default_factory=dict)  # by field; "" for the page
    notices: tuple[str, ...] = ()
    preview: Preview | None = None
    previewed: bool = False  # Preview pressed: None then means no postings yet


def steps(fresh: bool) -> tuple[str, ...]:
    return STEPS if fresh else EXISTING_STEPS


def form_from_profile(profile: Profile, email: EmailConfig | None) -> Form:
    return Form(name=profile.name, titles="\n".join(profile.titles), exclude_titles="\n".join(profile.exclude_titles),
                locations="\n".join(profile.locations), eligible_where=profile.eligible_where,
                tags=frozenset(profile.tags), custom=tuple(_row(c) for c in profile.custom_tags),
                email_to=email.to if email else "", email_from=email.sender if email else "",
                email_host=email.host if email else "", email_port=str(email.port) if email else "")


def prefilled(form: Form, described: Described, note: str) -> tuple[Form, Offers]:
    """The draft as the description asked for it (ticked), and what it may have missed (offered unticked, custom
    tags among them)."""
    form = replace(form, titles="\n".join(described.titles), exclude_titles="\n".join(described.leave_out),
                   locations="\n".join(described.places), eligible_where=described.eligible_where,
                   tags=frozenset(described.tags), custom=())
    return form, Offers(titles=described.other_titles, excludes=described.near_misses,
                        places=described.other_places, custom=tuple(_row(c) for c in described.custom_tags),
                        note=note)


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
        added = [line for line in _lines(one(more, label)) if line.casefold() not in {v.casefold() for v in values}]
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
        case "summary":
            changes["name"] = one("name").strip() if "name" in fields else form.name
    new = replace(form, **changes, too_long=frozenset(long))
    if (new.titles, new.exclude_titles, new.locations) != (form.titles, form.exclude_titles, form.locations):
        offers = replace(offers, adds={}, leaves_out={})
    return new, offers


def _rows(fields: Mapping[str, list[str]], one, offers: Offers) -> tuple[CustomRow, ...]:
    """The ticked offered tags, then the custom rows as posted (a row with no name is removed, and nothing in it is
    checked); a name given twice stays for the check to refuse."""
    # Sorted as numbers without converting them (a 5,000-digit index would overflow int()); "1" and "01" kept apart.
    indexes = sorted({m.group(1) for name in fields if (m := re.fullmatch(r"custom-(\d+)-\w+", name))},
                     key=lambda i: (len(i.lstrip("0")), i.lstrip("0"), i))
    ticked = set(fields.get("add_custom", []))
    rows: list[CustomRow] = [r for r in offers.custom if r.name in ticked]
    for i in indexes:
        if not (fields.get(f"custom-{i}-name") or [""])[0].strip():
            continue
        at = f"custom-{len(rows)}"  # the row's place on the page, where its error shows
        rows.append(CustomRow(name=one(f"custom-{i}-name", at).strip(), definition=one(f"custom-{i}-definition", at),
                              values=one(f"custom-{i}-values", at), multi=(fields.get(f"custom-{i}-multi") or [""])[0]
                              == "on", default=one(f"custom-{i}-default", at).strip()))
    return tuple(rows)


def check_step(step: str, form: Form, *, key_held: bool, fresh: bool, password: str) -> dict[str, str]:
    """The step's own fields by the rules Save applies; other steps' fields never count. `password`: the app password
    typed or saved, for checking email settings without sending."""
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
            errors = _email_errors(form, password) | errors
    return errors


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


def _email_errors(form: Form, password: str) -> dict[str, str]:
    if form.email_off or not form.email_to:
        return {}
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


def _row(c: CustomTag) -> CustomRow:
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
    return f'{_HEAD}<h1>jobscan</h1><p class="error">{_x(message)}</p></body></html>'


def render(view: View) -> str:
    order = steps(view.fresh)
    where = (f"Step {order.index(view.step) + 1} of {len(order)}" if view.fresh and view.step != "summary"
             else "")
    parts = [_HEAD, f"<h1>jobscan: {_x(HEADINGS[view.step])}</h1>"]
    if where:
        parts.append(f'<p class="hint">{where}</p>')
    parts += [f'<p class="notice">{_x(n)}</p>' for n in view.notices]
    if "" in view.errors:
        parts.append(f'<p class="error">{_x(view.errors[""])}</p>')
    parts.append(f'<form method="post" action="/"><input type="hidden" name="token" value="{_x(view.token)}">'
                 f'<input type="hidden" name="step" value="{_x(view.step)}">'
                 f'<input type="hidden" name="revision" value="{view.revision}">')
    parts.append(_BODIES[view.step](view))
    parts.append(_buttons(view))
    parts.append("</form>")
    if view.step == "summary" and view.previewed:
        lines = report.preview_lines(view.form.name, view.preview) if view.preview \
            else ["No postings yet: after the first `jobscan run`, Preview counts what your search matches."]
        parts.append("<h2>Preview</h2><pre>" + _x("\n".join(lines)) + "</pre>")
    parts.append("</body></html>")
    return "\n".join(p for p in parts if p)


def _buttons(view: View) -> str:
    """Next first, so Enter moves on."""
    if view.step == "summary":
        return ('<p class="buttons"><button name="action" value="save">Save</button> '
                '<button name="action" value="preview">Preview matches</button></p>'
                "<p class=\"hint\">Saving rewrites your profile file; comments you wrote in it by hand aren't kept "
                "(the previous file stays beside it as <code>.toml.prev</code>).</p>")
    label = "Next" if view.fresh else "Done"
    back = "" if view.fresh and view.step == "key" else ' <button name="action" value="back">Back</button>'
    skip = ' <button name="action" value="skip">Go on without suggestions</button>' \
        if view.step == "describe" else ""
    return f'<p class="buttons"><button name="action" value="next">{label}</button>{skip}{back}</p>'


def _key_body(view: View) -> str:
    held = "A key is held: leave this empty to keep it." if view.key_held else \
        "Paste it from openrouter.ai/keys. It's kept in a file only you can read, and pays for the model that " \
        "reads your words and tags each posting (cents a day)." if view.fresh else \
        "Needed only for an OpenRouter model."
    return (f'<p><label>OpenRouter key<br><input type="password" name="key" autocomplete="off" size="50"></label></p>'
            f'<p class="hint">{_x(held)}</p>{_err("key", view.errors)}')


def _describe_body(view: View) -> str:
    return ('<p>Say what you\'re looking for, as you would to a friend: the job, the kind of company, where, and '
            "anything you care about.</p>"
            f'<p><textarea name="description" rows="6" placeholder="I\'m looking for account executive jobs with '
            f'founding teams in London, and I\'m happy to be remote.">{_x(view.form.description)}</textarea></p>'
            '<p class="hint">To talk instead of typing on a Mac, click in the box and press Fn (🌐) twice. Next '
            "reads it with your key (well under a cent) and fills in the next steps for you to tick.</p>"
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
        rows.append(f'<label><input type="checkbox" name="tag" value="{_x(t.name)}"{checked}> <b>{_x(t.name)}</b>: '
                    f"{_x(t.summary)} <i>({_x(report.VALIDATED[t.validated])})</i></label><br>")
    named = {r.name for r in form.custom}
    suggested = ("<fieldset><legend>Tags of your own, from your description</legend>" + "".join(
        f'<label><input type="checkbox" name="add_custom" value="{_x(r.name)}"'
        f'{" checked" if r.name in named else ""}> <b>{_x(r.name)}</b>: {_x(r.definition)} '
        f"<i>({_x(', '.join(_lines(r.values)) or 'yes, no, unknown')})</i></label><br>"
        for r in view.offers.custom) + "</fieldset>") if view.offers.custom else ""
    own = tuple(r for r in form.custom if r not in view.offers.custom)  # an offered tag is its tick box
    return ("<p>The model answers each ticked tag for every matching posting, quoting it.</p>"
            + "<fieldset><legend>Built-in tags</legend>" + "".join(rows) + "</fieldset>" + _err("tags", e)
            + suggested + _custom(own, e, start=len(form.custom) - len(own)))


def _custom(own: tuple[CustomRow, ...], e: Mapping[str, str], start: int) -> str:
    """The editable rows; `start` is how many ticked offers come first in the draft, so a row's error (indexed by
    its place among all custom tags) shows beside it."""
    rows = [*own, CustomRow()]
    out = ["<fieldset><legend>Your own tags</legend><p class=\"hint\">Say what each answer means. Leave the name "
           "empty to remove one; fill the empty row to add one.</p>"]
    for i, row in enumerate(rows):
        multi = " checked" if row.multi else ""
        out.append(
            f'<div class="custom">{_err(f"custom-{start + i}", e)}'
            f'<label>Name <input name="custom-{i}-name" value="{_x(row.name)}"></label> '
            f'<label>Default <input name="custom-{i}-default" value="{_x(row.default)}"></label> '
            f'<label><input type="checkbox" name="custom-{i}-multi"{multi}> several answers</label><br>'
            f'<label>Definition<br><textarea name="custom-{i}-definition" rows="3">{_x(row.definition)}</textarea>'
            f'</label><br><label>Answers, one per line (empty: yes, no, unknown)<br>'
            f'<textarea name="custom-{i}-values" rows="2">{_x(row.values)}</textarea></label></div>')
    return "".join(out) + "</fieldset>"


def _email_body(view: View) -> str:
    f, e = view.form, view.errors
    saved = f"Now: digests to {view.email.to} from {view.email.sender}." if view.email else \
        "Now: your email settings don't load." if view.email_file else \
        "Now: off. Leave the address empty to keep it off."
    held = " An app password is held: leave it empty to keep it." if view.password_held else ""
    off = (f' <label><input type="checkbox" name="email_off"{" checked" if f.email_off else ""}> turn email off'
           "</label>") if view.email or view.email_file or f.email_off else ""
    return (f'<p class="hint">{_x(saved)}{_x(held)} Gmail and iCloud need only the address and an app password '
            "(Gmail: myaccount.google.com/apppasswords; iCloud: account.apple.com). A test email is sent when you "
            f"save.</p>{_err('email', e)}"
            + _text("email_to", "Send digests to", f.email_to, e)
            + _text("email_from", "From (empty: the same address)", f.email_from, e)
            + _text("email_host", "Mail server (empty: Gmail or iCloud's)", f.email_host, e)
            + _text("email_port", "Port (465 or 587)", f.email_port, e)
            + '<label>App password <input type="password" name="password" autocomplete="off"></label>'
            + _err("password", e) + off)


def _summary_body(view: View) -> str:
    f, e = view.form, view.errors
    name = _text("name", "Name for this search", f.name, e, "lower-case letters, digits and -") if view.fresh \
        else f"<p>Search: <b>{_x(f.name)}</b></p>"
    custom = [r.name for r in f.custom]
    email = "off" if f.email_off or not f.email_to else f"to {f.email_to}"
    key = "held" if view.key_held else "none"
    sections = [("key", "OpenRouter key", key), ("titles", "Titles", "; ".join(_lines(f.titles))),
                ("exclude", "Left out", "; ".join(_lines(f.exclude_titles)) or "none"),
                ("places", "Places", "; ".join(_lines(f.locations)) + f" (you can work from: {f.eligible_where})"),
                ("tags", "Tags", ", ".join([*sorted(f.tags, key=_tag_order), *custom]) or "none"),
                ("email", "Email", email)]
    rows = []
    for step, label, value in sections:
        mistakes = [m for n, m in e.items() if n and _step_of(n) == step]
        rows.append(f"<tr><th>{_x(label)}</th><td>{_x(value)}"
                    + "".join(f'<span class="error">{_x(m)}</span>' for m in mistakes)
                    + f'</td><td><button name="action" value="goto:{step}">Edit</button></td></tr>')
    return name + "<table>" + "".join(rows) + "</table>"


_BODIES = MappingProxyType({"key": _key_body, "describe": _describe_body, "titles": _titles_body,
                            "exclude": _exclude_body, "places": _places_body, "tags": _tags_body,
                            "email": _email_body, "summary": _summary_body})


def _ticks(name: str, chosen: tuple[str, ...], offered: tuple[str, ...], counts: Mapping[str, int], sign: str) -> str:
    """One box per distinct value: the draft's (ticked), then the offers not among them (unticked)."""
    values = list(dict.fromkeys((*chosen, *offered)))
    if not values:
        return ""
    return "".join(f'<label><input type="checkbox" name="{name}" value="{_x(v)}"'
                   f'{" checked" if v in chosen else ""}> {_x(v)}'
                   + (f" <i>{sign}{counts[v]}</i>" if v in counts else "") + "</label><br>" for v in values)


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


_HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>jobscan</title><style>
body{font:15px/1.4 -apple-system,system-ui,sans-serif;max-width:52rem;margin:2rem auto;padding:0 1rem}
fieldset{margin:1rem 0;border:1px solid #ccc}.hint{color:#666;font-size:13px}.error{color:#b00;display:block}
.notice{background:#eef7ee;padding:.5rem}.custom{border-top:1px dashed #ddd;padding:.5rem 0}
textarea{width:100%}pre{background:#f6f6f6;padding:.5rem;white-space:pre-wrap}
label{line-height:1.9}th{text-align:left;padding-right:1rem;vertical-align:top}td{padding:.3rem .5rem}
</style></head><body>"""
