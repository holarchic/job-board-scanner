"""jobscan's commands, flags and help text. `jobscan --help` lays out set-up in order, and each command's help has
examples, so a person or an agent can set an install up from the help alone."""
import argparse
from datetime import time
from pathlib import Path

from jobscan import schedule

DESCRIPTION = """Self-hosted job-board scanner. jobscan finds company job boards itself (Ashby, Greenhouse, Gem,
Teamtailor, Personio, SmartRecruiters, Rippling, Workday and Oracle sites, from Common Crawl; Workable boards from the
boards file), keeps the postings whose titles and locations match your search, has a language model answer your tags
about each one, quoting the posting, and writes a markdown digest of what is new: emailed if you like, daily if you
schedule it. Everything lives under $JOBSCAN_HOME (default ~/.jobscan)."""
SET_UP = """Set up, in order (a person in a terminal, or an agent by flags):
  1. jobscan init                  the search and the OpenRouter key (openrouter.ai/keys); a person can use
                                   `jobscan ui` instead, a local page that does steps 1, 2, 4 and 7
       printf '%s\\n' "$KEY" | jobscan init --name ae --title 'Account Executive' \\
           --location London --location UK --location Remote --eligible-where 'the UK' --key-stdin
  2. jobscan email                 optional: each digest emailed from your own Gmail or iCloud account
       printf '%s\\n' "$APP_PASSWORD" | jobscan email --to me@gmail.com --password-stdin
  3. jobscan boards add URL        optional: a company discovery misses
  4. jobscan check                 the install loads as a run would; run it after editing the profile
  5. jobscan run                   fetch, filter, tag, write the digest; the first run finds the boards too and
                                   tags up to the profile's limits (500 postings, $1.00), later runs the rest
  6. jobscan preview               what the profile matches among the stored postings: tune titles without a run
  7. jobscan schedule --at 08:00   macOS: a daily run, with a notification when there's something new

The profile is profiles/NAME.toml: edit it by hand, then `jobscan check`. For example:
  titles = ["Product Manager"]       # whole words, case-folded, anywhere in the title: matches "Senior Product
                                     # Manager", not "Product Marketing Manager"
  exclude_titles = ["Associate"]     # left out even when a title matches
  locations = ["London", "UK", "Remote"]
                                     # the same rule over the posting's location text only. "Remote" keeps
                                     # remote jobs naming no place; a remote job naming the UK by any name, or
                                     # a region holding it (EMEA, Europe), is kept too; "Remote - US" is not
  eligible_where = "the UK"          # where you can work from: what location_eligible asks about
  tags = ["location_eligible"]       # built-in tags to ask: `jobscan tags` lists them, `jobscan tags NAME`
                                     # shows one
  require = { location_eligible = "yes" }
                                     # optional: a posting a tag definitely answers otherwise is listed at the
                                     # digest's end, not as an entry; unknown answers stay
  include_agencies = true            # optional: keep jobs agencies post for clients; by default jobscan asks
                                     # of every posting and lists those at the digest's end
  max_tag_per_run = 200              # optional (default 500): postings a run sends to the model at most
  max_spend_per_run = 0.50           # optional (default 1.00): dollars; once a run's spend reaches it, it starts
                                     # no more. Either way the rest wait for later runs (exit 0)
  [custom_tags.work_policy]          # your own tag: asked of every match, without listing it in `tags`
  definition = "Where the job is done: remote, hybrid or onsite, as the posting states it."
  values = ["remote", "hybrid", "onsite", "unknown"]
                                     # values defaults to yes, no, unknown; `multi = true` answers a list;
                                     # `default = "unknown"` (one of values) answers when the posting is silent
Tags label each posting in the digest; they hide one only as `require` says. `jobscan titles` suggests titles to
add or leave out.
Secrets never go on the command line: the key comes from stdin or OPENROUTER_API_KEY, the app password from stdin.
Exit codes: 0 done, matches held back by the tagging limits included; 1 finished with something left undone
(postings whose tagging failed, carried over; a digest not written or emailed; a test email refused; a run holding
the database); 2 a config or usage error, before any work.
Every command has --help with examples."""


def parser() -> argparse.ArgumentParser:
    """The commands in the order set-up uses them."""
    root = argparse.ArgumentParser(prog="jobscan", formatter_class=argparse.RawDescriptionHelpFormatter,
                                   description=DESCRIPTION, epilog=SET_UP)
    commands = root.add_subparsers(dest="command", required=True)
    for add in (_init, _email, _boards, _check, _run, _preview, _schedule, _ui, _titles, _tags, _discover, _eval):
        add(commands)
    return root


def port(text: str) -> int:
    if not (text.isascii() and text.isdigit() and int(text) <= 65535):
        raise argparse.ArgumentTypeError("a port number, 0 to 65535")
    return int(text)


def clock(text: str) -> time:
    at = schedule.parse_at(text)
    if at is None:
        raise argparse.ArgumentTypeError("use HH:MM, 24-hour, e.g. 08:00")
    return at


def _command(commands, name: str, summary: str, description: str, examples: list[str]) -> argparse.ArgumentParser:
    """A command whose help keeps its own line breaks and ends with examples."""
    return commands.add_parser(name, help=summary, description=description, epilog="Examples:\n" + "\n".join(
        f"  {line}" for line in examples), formatter_class=argparse.RawDescriptionHelpFormatter)


def _init(commands) -> None:
    command = _command(
        commands, "init", "set up a search: asks in the terminal, or takes flags; writes config, profile and key",
        "Write a new install: config.toml, the profile (profiles/NAME.toml) and the OpenRouter key file "
        "(openrouter.key, readable only by you), under $JOBSCAN_HOME (default ~/.jobscan). With no flags it asks in "
        "the terminal. With flags it asks nothing: --name, --title, --location and --eligible-where are then "
        "required; repeat a list flag once per value (a value may hold a comma). Titles and locations match whole "
        "words, case-folded, anywhere in a posting's title or location text. The key never goes on the command "
        "line: --key-stdin reads it from stdin, else OPENROUTER_API_KEY is saved. Nothing is written if any value "
        "is refused, or if the home already holds an install. Next: `jobscan email` (optional), `jobscan check`, "
        "`jobscan run`.",
        ["jobscan init",
         "printf '%s\\n' \"$KEY\" | jobscan init --name ae --title 'Account Executive' \\",
         "    --title 'Enterprise Account Executive' --exclude-title 'Account Manager' \\",
         "    --location London --location UK --location Remote \\",
         "    --eligible-where 'the UK, working from London' --key-stdin",
         "OPENROUTER_API_KEY=... jobscan init --name pm --title 'Product Manager' --location Berlin \\",
         "    --eligible-where 'Germany' --tag founding --tag location_eligible --require location_eligible=yes \\",
         "    --include-agencies --max-tag-per-run 200 --max-spend-per-run 0.50"])
    command.add_argument("--name", help="the search's name: lower-case letters, digits and -")
    command.add_argument("--title", action="append", default=[], metavar="TITLE",
                         help="a job title to look for (repeat for each)")
    command.add_argument("--exclude-title", action="append", default=[], metavar="TITLE",
                         help="a title to leave out even if it matches (repeat for each)")
    command.add_argument("--location", action="append", default=[], metavar="PLACE",
                         help="a place to keep, as job ads write it (repeat for each)")
    command.add_argument("--eligible-where", metavar="TEXT",
                         help="where you can work from, in a few words: what the location tag tests")
    command.add_argument("--tag", action="append", default=[], metavar="NAME",
                         help="a built-in tag to ask (repeat for each; default: all but the opt-in ones; see "
                              "`jobscan tags`); your own tags go in the profile file (`jobscan --help` shows how)")
    command.add_argument("--require", action="append", default=[], metavar="TAG=VALUE",
                         help="hide a posting when this tag definitely answers otherwise, listing it at the digest's "
                              "end instead (repeat for each; e.g. language_required=no; unknown answers stay)")
    command.add_argument("--include-agencies", action="store_true",
                         help="keep jobs recruitment and staffing agencies post for clients (default: each is listed "
                              "at the digest's end instead)")
    command.add_argument("--max-tag-per-run", type=int, metavar="N",
                         help="postings a run sends to the model at most (default 500); the rest wait for later runs")
    command.add_argument("--max-spend-per-run", type=float, metavar="USD",
                         help="once a run's tagging spend reaches this many dollars, it starts no more (default "
                              "1.00); in-flight postings finish")
    command.add_argument("--key-stdin", action="store_true", help="read the OpenRouter key from stdin")


def _email(commands) -> None:
    command = _command(
        commands, "email", "email each digest from your own mail account: asks or takes flags, sends a test, saves",
        "Email each digest from your own mail account to an address. With no flags it asks in the terminal. With "
        "flags it asks nothing: --to and --password-stdin are required; the sender defaults to --to, and Gmail and "
        "iCloud addresses need no server (other providers: --host and --port together). The app password is read "
        "from stdin, never the command line: Gmail's from myaccount.google.com/apppasswords (needs 2-Step "
        "Verification), iCloud's from account.apple.com. Outlook.com and Hotmail can't be used (OAuth only). A test "
        "email is sent first; the settings are saved to email.toml (readable only by you) only once it is accepted. "
        "Exit 1 when the test email is refused.",
        ["jobscan email",
         "printf '%s\\n' \"$APP_PASSWORD\" | jobscan email --to me@gmail.com --password-stdin",
         "printf '%s\\n' \"$APP_PASSWORD\" | jobscan email --to me@work.example --from me@fastmail.com \\",
         "    --host smtp.fastmail.com --port 465 --password-stdin",
         "jobscan email --off"])
    command.add_argument("--off", action="store_true", help="stop emailing digests")
    command.add_argument("--to", metavar="ADDRESS", help="where each digest goes")
    command.add_argument("--from", dest="sender", metavar="ADDRESS",
                         help="the account that sends it, also the login (default: --to)")
    command.add_argument("--host", help="the mail (SMTP) server, for a provider other than Gmail or iCloud")
    command.add_argument("--port", type=int, help="its port: 465 (TLS) or 587 (STARTTLS)")
    command.add_argument("--password-stdin", action="store_true", help="read the app password from stdin")


def _boards(commands) -> None:
    command = _command(
        commands, "boards", "list the boards file's boards, or add board URLs to it",
        "Discovery finds most boards by itself. Companies it misses (a board Common Crawl never captured, or one "
        "embedded in a company's own careers page) go in the boards file, read by every run beside the discovered "
        "boards: config.toml's boards_file, else $JOBSCAN_HOME/boards.json. With no subcommand, list it.",
        ["jobscan boards", "jobscan boards add https://job-boards.greenhouse.io/acme"])
    add = _command(
        command.add_subparsers(dest="boards_command"), "add", "add board URLs to the boards file",
        "Add each board by its careers-page or board URL (Ashby, Greenhouse, Workable, Gem, Teamtailor, Personio, "
        "SmartRecruiters, Rippling, Workday, Oracle). Every URL is checked first: one no adapter recognises adds "
        "nothing (exit 2). A board the file lists already is skipped. Without --company, the run looks the "
        "company's name up. No request is made: the next run reads it.",
        ["jobscan boards add https://job-boards.greenhouse.io/acme https://apply.workable.com/beta",
         "jobscan boards add https://acme.wd3.myworkdayjobs.com/Careers --company Acme"])
    add.add_argument("urls", nargs="+", metavar="URL", help="a board or careers-page URL")
    add.add_argument("--company", help="the company's name for the digest (one URL only)")


def _check(commands) -> None:
    _command(
        commands, "check", "load the install as a run would and print what it holds, or the first problem",
        "Load config.toml, the key, the profile and its tags, email.toml and the boards file exactly as `jobscan "
        "run` loads them here, then the key as a scheduled run reads it (with no shell environment). Prints what "
        "the install holds, the daily run included (macOS: what launchd and its plist say, with a warning and the "
        "fix when it won't run), or the first problem with exit 2. Reads only: no network, no database. Run it "
        "after editing the profile by hand.",
        ["jobscan check", "$EDITOR ~/.jobscan/profiles/*.toml && jobscan check"])


def _run(commands) -> None:
    command = _command(
        commands, "run", "fetch → filter → tag → write the profile's digest",
        "Rediscover boards when the list is over 30 days old, fetch every board, keep the postings matching the "
        "profile's titles and locations, have the model answer the profile's tags about each new one, and write "
        "digests/NAME/DATE.md; email it when email is set up. Prints `NAME<tab>NEW<tab>DIGEST PATH`. Exit 1 leaves "
        "untagged postings for the next run; logs go to runs.log.",
        ["jobscan run", "jobscan run --notify"])
    command.add_argument("--notify", action="store_true",
                         help="macOS: a desktop notification on new postings or a problem")


def _preview(commands) -> None:
    _command(
        commands, "preview", "what the saved profile matches among the postings the last run stored",
        "Count what the saved profile's titles, excluded titles and locations match among the open postings in the "
        "database: new jobs waiting to be tagged, counted as a run counts them (one per job, none already sent), "
        "with what tagging them all costs and the per-run limits, postings already sent, and samples, plus samples "
        "of what the excluded titles leave out. Edit "
        "the profile, then preview again, to tune a search without a run. A snapshot of what the last run stored: "
        "it fetches nothing, so before the first run there is nothing to preview. It waits for no run: while one "
        "is going, exit 1. The first time new code opens the database it backs it up first, as a run would.",
        ["jobscan preview", "$EDITOR ~/.jobscan/profiles/*.toml && jobscan check && jobscan preview"])


def _schedule(commands) -> None:
    command = _command(
        commands, "schedule", "macOS: run every day at a set time, even after sleep (launchd); elsewhere: a cron line",
        "On macOS, install a launchd job running `jobscan run --notify` daily at --at; a slot missed while the Mac "
        "slept runs on wake. Elsewhere, print the cron line to add. Use the installed `jobscan`, not a checkout. "
        "`jobscan ui` sets the same job from its When to run step.",
        ["jobscan schedule --at 08:00", "jobscan schedule --off"])
    when = command.add_mutually_exclusive_group(required=True)
    when.add_argument("--at", type=clock, metavar="HH:MM", help="daily run time, 24-hour clock")
    when.add_argument("--off", action="store_true", help="remove the scheduled run")


def _ui(commands) -> None:
    command = _command(
        commands, "ui", "set up or edit the whole search on a local web page",
        "Serve a page on this machine (127.0.0.1 only) to set up a new install one step at a time, or edit this one "
        "from a summary. A new install starts with how jobscan works, asks for the OpenRouter key, then what you're "
        "looking for in your own words; one model call (reasoning off: seconds, a fraction of a cent) fills titles, "
        "titles to leave out, places and tags as tick boxes, with suggestions beside them; then email, when to run "
        "and a summary to save, then what now: the command for a first run and where digests land. On macOS, Save "
        "sets the daily job `schedule` sets up, only once everything else saved (unticking it removes the job "
        "whatever else failed); elsewhere the page gives the cron line to add. Suggest titles and Preview work on "
        "the search as it stands. It opens in your browser through a link with a one-time token; anyone with that "
        "link can use the page until it stops. Ctrl-C stops it. Saving rewrites the profile without its comments (the "
        "previous file is kept as .toml.prev). Typed keys and passwords stay in memory until you save; the page "
        "never shows them.",
        ["jobscan ui", "jobscan ui --port 8765 --no-browser"])
    command.add_argument("--port", type=port, default=0, help="the port to serve on (default: any free one)")
    command.add_argument("--no-browser", action="store_true", help="print the link without opening a browser")


def _titles(commands) -> None:
    _command(
        commands, "titles", "the model suggests other titles for your jobs, and near-misses to leave out",
        "Ask the model, once, for other titles employers post your jobs under (variants) and for titles your "
        "titles match as words that are other jobs (near-misses). Each is counted against the postings the last "
        "run stored in your places: how many a variant would add, how many of your current matches a near-miss "
        "would leave out. Counts are each suggestion's own effect; `preview` after editing shows them together. "
        "Before any run there is nothing to count, so only the suggestions show. Your profile is never changed: "
        "copy the ones you keep into titles or exclude_titles, then `jobscan check` and `jobscan preview`. One "
        "model call (about $0.002 with the default model); exit 1 if it fails or a run holds the database. The "
        "first time new code opens the database it backs it up first, as a run would.",
        ["jobscan titles", "jobscan titles && $EDITOR ~/.jobscan/profiles/*.toml && jobscan preview"])


def _tags(commands) -> None:
    command = _command(
        commands, "tags", "list the tags the model can answer, or show one's definition",
        "List the built-in tags (and the profile's custom ones), marking those the profile asks and whether each "
        "has been validated against labelled postings. With a NAME, print that tag's definition exactly as the "
        "model reads it. Works before `init` too.",
        ["jobscan tags", "jobscan tags founding"])
    command.add_argument("name", nargs="?", help="a tag's name, to show its definition")


def _discover(commands) -> None:
    _command(
        commands, "discover", "rebuild the board list from the newest Common Crawl crawls (monthly)",
        "Rebuild the discovered board list from the three newest Common Crawl crawls (about 90 seconds). `run` "
        "does this itself when the list is over 30 days old; run it by hand only to refresh sooner.",
        ["jobscan discover"])


def _eval(commands) -> None:
    command = _command(
        commands, "eval", "score the tagging path against a labelled posting set",
        "Tag a labelled set of postings through the same code `run` uses and score each tag's accuracy, minority "
        "recall and quote rate. Predictions are saved under $JOBSCAN_HOME/eval/.",
        ["jobscan eval my-labelled-set.json"])
    command.add_argument("set", type=Path, help="JSON list of {id, title, location, description, labels}")
