"""Command-line entry point: parses arguments, owns logging, the run lock and exit codes; dispatches to the modules."""
import argparse
import fcntl
import logging
import os
import sys
from datetime import time
from pathlib import Path
from time import monotonic

from jobscan import boards, discover, evaluate, fetch, init, mail, notify, report, scan, schedule, store, tag
from jobscan.config import (
    EMAIL_FILE,
    KEY_FILE,
    Config,
    ConfigError,
    EmailConfig,
    Profile,
    is_host,
    is_mailbox,
    is_port,
    jobscan_home,
    load_config,
    load_email,
    load_profile,
)
from jobscan.crawl import CrawlError

EXIT_OK, EXIT_INCOMPLETE, EXIT_USAGE = 0, 1, 2
log = logging.getLogger("jobscan")


class RunLocked(Exception):
    """Another jobscan process holds this $JOBSCAN_HOME."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobscan")
    commands = parser.add_subparsers(dest="command", required=True)
    init_parser = commands.add_parser(
        "init", help="set up a search: asks in the terminal, or takes flags; writes config, profile and key",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Write a new install: config.toml, the profile (profiles/NAME.toml) and the OpenRouter key file "
                    "(openrouter.key, readable only by you), under $JOBSCAN_HOME (default ~/.jobscan). With no "
                    "flags it asks in the terminal. With flags it asks nothing: --name, --title, --location and "
                    "--eligible-where are then required; repeat a list flag once per value (a value may hold a "
                    "comma). The key never goes on the command line: --key-stdin reads it from stdin, else "
                    "OPENROUTER_API_KEY is saved. Nothing is written if any value is refused, or if the home "
                    "already holds an install.",
        epilog="Examples:\n"
               "  jobscan init\n"
               "  printf '%s\\n' \"$KEY\" | jobscan init --name ae --title 'Account Executive' \\\n"
               "      --title 'Enterprise Account Executive' --exclude-title 'Account Manager' \\\n"
               "      --location London --location UK --location Remote \\\n"
               "      --eligible-where 'the UK, working from London' --key-stdin\n"
               "  OPENROUTER_API_KEY=... jobscan init --name pm --title 'Product Manager' --location Berlin \\\n"
               "      --eligible-where 'Germany' --tag founding --tag location_eligible\n"
               "\nTitles match whole words, case-folded, anywhere in a posting's title; locations the same against "
               "its location text.\nAfter init: `jobscan email` (optional), `jobscan check`, `jobscan run`.")
    init_parser.add_argument("--name", help="the search's name: lower-case letters, digits and -")
    init_parser.add_argument("--title", action="append", default=[], metavar="TITLE",
                             help="a job title to look for (repeat for each)")
    init_parser.add_argument("--exclude-title", action="append", default=[], metavar="TITLE",
                             help="a title to leave out even if it matches (repeat for each)")
    init_parser.add_argument("--location", action="append", default=[], metavar="PLACE",
                             help="a place to keep, as job ads write it (repeat for each)")
    init_parser.add_argument("--eligible-where", metavar="TEXT",
                             help="where you can work from, in a few words: what the location tag tests")
    init_parser.add_argument("--tag", action="append", default=[], metavar="NAME",
                             help="a built-in tag to ask (repeat for each; default: all; see `jobscan tags`)")
    init_parser.add_argument("--key-stdin", action="store_true", help="read the OpenRouter key from stdin")
    commands.add_parser("discover", help="rebuild the board list from the newest Common Crawl crawls (monthly)")
    run_parser = commands.add_parser("run", help="fetch → filter → tag → write the profile's digest")
    run_parser.add_argument("--notify", action="store_true",
                            help="macOS: a desktop notification on new postings or a problem")
    schedule_parser = commands.add_parser(
        "schedule", help="macOS: run every day at a set time, even after sleep (launchd); elsewhere: print a cron line")
    when = schedule_parser.add_mutually_exclusive_group(required=True)
    when.add_argument("--at", type=_clock, metavar="HH:MM", help="daily run time, 24-hour clock")
    when.add_argument("--off", action="store_true", help="remove the scheduled run")
    email_parser = commands.add_parser(
        "email", help="email each digest from your own mail account: asks or takes flags, sends a test, saves",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Email each digest from your own mail account to an address. With no flags it asks in the "
                    "terminal. With flags it asks nothing: --to and --password-stdin are required; the sender "
                    "defaults to --to, and Gmail and iCloud addresses need no server (other providers: --host and "
                    "--port together). The app password is read from stdin, never the command line: Gmail's from "
                    "myaccount.google.com/apppasswords (needs 2-Step Verification), iCloud's from "
                    "account.apple.com. Outlook.com and Hotmail can't be used (OAuth only). A test email is sent "
                    "first; the settings are saved to email.toml (readable only by you) only once it is accepted. "
                    "Exit 1 when the test email is refused.",
        epilog="Examples:\n"
               "  jobscan email\n"
               "  printf '%s\\n' \"$APP_PASSWORD\" | jobscan email --to me@gmail.com --password-stdin\n"
               "  printf '%s\\n' \"$APP_PASSWORD\" | jobscan email --to me@work.example --from me@fastmail.com \\\n"
               "      --host smtp.fastmail.com --port 465 --password-stdin\n"
               "  jobscan email --off")
    email_parser.add_argument("--off", action="store_true", help="stop emailing digests")
    email_parser.add_argument("--to", metavar="ADDRESS", help="where each digest goes")
    email_parser.add_argument("--from", dest="sender", metavar="ADDRESS",
                              help="the account that sends it, also the login (default: --to)")
    email_parser.add_argument("--host", help="the mail (SMTP) server, for a provider other than Gmail or iCloud")
    email_parser.add_argument("--port", type=int, help="its port: 465 (TLS) or 587 (STARTTLS)")
    email_parser.add_argument("--password-stdin", action="store_true", help="read the app password from stdin")
    commands.add_parser(
        "check", help="load the install as a run would and print what it holds, or the first problem",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Load config.toml, the key, the profile and its tags, email.toml and the boards file exactly as "
                    "`jobscan run` loads them here, then the key as a scheduled run reads it (with no shell "
                    "environment). Prints what the install holds, or the first problem with exit 2. Reads only: "
                    "no network, no database. Run it after editing the profile by hand.",
        epilog="Examples:\n  jobscan check\n  $EDITOR ~/.jobscan/profiles/*.toml && jobscan check")
    boards_parser = commands.add_parser(
        "boards", help="list the boards file's boards, or add board URLs to it",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Discovery finds most boards by itself. Companies it misses (a board Common Crawl never "
                    "captured, or one embedded in a company's own careers page) go in the boards file, read by "
                    "every run beside the discovered boards: config.toml's boards_file, else "
                    "$JOBSCAN_HOME/boards.json. With no subcommand, list it.",
        epilog="Examples:\n  jobscan boards\n  jobscan boards add https://job-boards.greenhouse.io/acme\n"
               "  jobscan boards add https://jobs.ashbyhq.com/acme --company 'Acme AI'")
    boards_commands = boards_parser.add_subparsers(dest="boards_command")
    boards_add = boards_commands.add_parser(
        "add", help="add board URLs to the boards file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Add each board by its careers-page or board URL (Ashby, Greenhouse, Workable, Gem, Teamtailor, "
                    "Personio, SmartRecruiters, Rippling, Workday, Oracle). Every URL is checked first: one no "
                    "adapter recognises adds nothing (exit 2). A board the file lists already is skipped. Without "
                    "--company, the run looks the company's name up. No request is made: the next run reads it.",
        epilog="Examples:\n  jobscan boards add https://job-boards.greenhouse.io/acme https://apply.workable.com/beta\n"
               "  jobscan boards add https://acme.wd3.myworkdayjobs.com/Careers --company Acme")
    boards_add.add_argument("urls", nargs="+", metavar="URL", help="a board or careers-page URL")
    boards_add.add_argument("--company", help="the company's name for the digest (one URL only)")
    tags_parser = commands.add_parser(
        "tags", help="list the tags the model can answer, or show one's definition",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="List the built-in tags (and the profile's custom ones), marking those the profile asks and "
                    "whether each has been validated against labelled postings. With a NAME, print that tag's "
                    "definition exactly as the model reads it. Works before `init` too.",
        epilog="Examples:\n  jobscan tags\n  jobscan tags founding")
    tags_parser.add_argument("name", nargs="?", help="a tag's name, to show its definition")
    eval_parser = commands.add_parser("eval", help="score the tagging path against a labelled posting set")
    eval_parser.add_argument("set", type=Path, help="JSON list of {id, title, location, description, labels}")
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return EXIT_OK if e.code == 0 else EXIT_USAGE
    home = jobscan_home()
    if args.command == "init":
        return _init(home, args)
    if args.command == "eval":
        return _eval(home, args.set)
    if args.command == "discover":
        return _discover(home)
    if args.command == "schedule":
        return _schedule(home, args.at)
    if args.command == "email":
        return _email(home, args)
    if args.command == "tags":
        return _tags(home, args.name)
    if args.command == "check":
        return _check(home)
    if args.command == "boards":
        return _boards(home, args)
    return _run(home, args.notify)


def _run(home: Path, notifying: bool) -> int:
    """With `notifying`, a scheduled run reports news and problems on the desktop; nothing on a quiet run."""
    entered = monotonic()
    try:
        config = load_config(home)
        email = load_email(home)
        profile = load_profile(home)
        defs = tag.definitions(profile.tags, profile.eligible_where, profile.custom_tags)
        _setup_logging(home)
        with _run_lock(home):
            log.info("run_start profile=%s setup_seconds=%.1f", profile.name, monotonic() - entered)
            outcome = scan.scan(config, profile, defs)
    except ConfigError as e:
        return _refused(str(e), notifying)
    except RunLocked:
        return _refused(f"another run is using {home}", notifying)
    except Exception as e:
        if notifying:
            notify.notify(f"jobscan stopped with an error ({type(e).__name__}); see {home / 'launchd.log'}")
        raise
    print(f"{outcome.profile}\t{outcome.new}\t{outcome.digest_path or '-'}")
    email_error = _email_digest(email, outcome)
    news = _news(outcome, email_error)
    if notifying and news:
        notify.notify(news)
    return EXIT_OK if outcome.complete and email_error is None else EXIT_INCOMPLETE


def _email_digest(email: EmailConfig | None, outcome: scan.ScanOutcome) -> mail.MailError | None:
    """A digest that couldn't be emailed stays on disk, and no later run resends it."""
    if email is None or outcome.digest_path is None:
        return None
    try:
        mail.send_digest(email, outcome.digest_path, outcome.profile, outcome.new, outcome.carried_over)
    except mail.MailError as e:
        log.error("email_failed reason=%r", str(e))
        return e
    log.info("email_sent")
    return None


def _refused(reason: str, notifying: bool) -> int:
    print(f"jobscan: {reason}", file=sys.stderr)
    if notifying:
        notify.notify(f"jobscan couldn't run: {reason}")
    return EXIT_USAGE


def _news(outcome: scan.ScanOutcome, email_error: mail.MailError | None) -> str | None:
    parts = []
    if outcome.new:
        parts.append(f"{outcome.new} new posting{'' if outcome.new == 1 else 's'} in {outcome.digest_path.parent}")
    if outcome.carried_over:
        parts.append(f"{outcome.carried_over} couldn't be tagged yet; see runs.log")
    if outcome.digest_failed:
        parts.append("the digest couldn't be written; see runs.log")
    if email_error:
        parts.append("the digest couldn't be emailed; see runs.log")
    return "; ".join(parts) or None


def _clock(text: str) -> time:
    hour, _, minute = text.partition(":")
    if not (hour.isdigit() and minute.isdigit() and len(minute) == 2 and int(hour) < 24 and int(minute) < 60):
        raise argparse.ArgumentTypeError("use HH:MM, 24-hour, e.g. 08:00")
    return time(int(hour), int(minute))


def _schedule(home: Path, at: time | None) -> int:
    """`at` None: remove. A schedule is installed only for a config that loads as the job will run it: with no
    shell environment, so the key must be in the key file."""
    if at is not None:
        try:
            load_config(home, environ={})
            load_email(home)
            profile = load_profile(home)
            tag.definitions(profile.tags, profile.eligible_where, profile.custom_tags)
        except ConfigError as e:
            print(f"jobscan: {e}", file=sys.stderr)
            return EXIT_USAGE
    if sys.platform != "darwin":
        print(schedule.cron_line(home, at) if at else "jobscan: remove the jobscan line with `crontab -e`")
        return EXIT_OK
    agents = Path.home() / "Library" / "LaunchAgents"
    try:
        if at is None:
            job = schedule.remove(home, agents)
            print(f"unscheduled\t{job.plist}")
        else:
            job = schedule.install(home, at, agents)
            print(f"scheduled\t{at:%H:%M}\t{job.plist}")
    except schedule.ScheduleError as e:
        print(f"jobscan: could not change the schedule: {e}", file=sys.stderr)
        return EXIT_INCOMPLETE
    return EXIT_OK


EMAIL_FLAGS = ("to", "sender", "host", "port", "password_stdin")


def _email(home: Path, args: argparse.Namespace) -> int:
    """Saves the settings only once a test email is accepted; `--off` works however broken the saved file is."""
    flagged = any(getattr(args, flag) is not None and getattr(args, flag) is not False for flag in EMAIL_FLAGS)
    try:
        if args.off:
            if flagged:
                raise ConfigError("--off takes no other flags")
            print(f"removed\t{home / EMAIL_FILE}" if init.remove_email(home) else "jobscan: no email set up")
            return EXIT_OK
        profile = load_profile(home).name
        if flagged:
            email = _email_settings(args)
            print(f"Sending a test email to {email.to} ...")
            text = init.email_from(email, profile)
        elif not sys.stdin.isatty():
            raise ConfigError("no terminal to ask in: give the flags instead (see `jobscan email --help`)")
        else:
            text = init.email_setup(profile)
        init.save_email(home, text)
    except ConfigError as e:
        print(f"jobscan: {e}", file=sys.stderr)
        return EXIT_USAGE
    except (KeyboardInterrupt, EOFError):
        print("\njobscan: cancelled; nothing was saved", file=sys.stderr)
        return EXIT_USAGE
    except mail.MailError as e:
        print(f"jobscan: the test email wasn't sent ({e}); nothing was saved", file=sys.stderr)
        return EXIT_INCOMPLETE
    print(f"Sent a test email; saved {home / EMAIL_FILE}. Each digest will be emailed from now on.")
    return EXIT_OK


INIT_FLAGS = ("name", "title", "exclude_title", "location", "eligible_where", "tag", "key_stdin")


def _email_settings(args: argparse.Namespace) -> EmailConfig:
    sender = args.sender or args.to
    for flag, value in [("--to", args.to), ("--from", sender)]:
        if not is_mailbox(value):
            raise ConfigError(f"{flag}: not an email address (see `jobscan email --help`)")
    if init.oauth_only(sender):
        raise ConfigError(f"{sender}: Outlook.com lets mail apps sign in only with OAuth, which jobscan can't do")
    if (args.host is None) != (args.port is None):
        raise ConfigError("give --host and --port together")
    if args.host is not None and not (is_host(args.host) and is_port(args.port)):
        raise ConfigError("--host must be a server name and --port a port number")
    server = init.MailServer(args.host, args.port) if args.host else init.preset(sender)
    if server is None:
        raise ConfigError(f"no preset mail server for {sender}: give --host and --port")
    if not args.password_stdin:
        raise ConfigError("the app password comes from stdin: add --password-stdin")
    password = init.app_password(server, sys.stdin.readline().rstrip("\r\n"))
    if password is None:
        raise ConfigError("that isn't an app password jobscan can send (empty, or control characters)")
    return EmailConfig(to=args.to, sender=sender, host=server.host, port=server.port, password=password)


def _init(home: Path, args: argparse.Namespace) -> int:
    try:
        if any(getattr(args, flag) for flag in INIT_FLAGS):
            setup = init.init_from(home, _init_answers(args))
        elif not sys.stdin.isatty():
            raise ConfigError("no terminal to ask in: give the flags instead (see `jobscan init --help`)")
        else:
            setup = init.init(home)
    except ConfigError as e:
        print(f"jobscan: {e}", file=sys.stderr)
        return EXIT_USAGE
    except init.InitCancelled:
        print("\njobscan: cancelled; nothing was written", file=sys.stderr)
        return EXIT_USAGE
    print(f"\nWrote {setup.config}, {setup.profile}, {setup.key}" + (f" and {setup.email}." if setup.email else "."))
    if setup.email_error:
        print(f"The test email wasn't sent ({setup.email_error}), so email isn't set up; `jobscan email` tries again.")
    print(init.NEXT_STEPS.format(profile=setup.profile))
    return EXIT_OK


def _init_answers(args: argparse.Namespace) -> init.Answers:
    missing = [f"--{flag.replace('_', '-')}" for flag in ("name", "title", "location", "eligible_where")
               if not getattr(args, flag)]
    if missing:
        raise ConfigError(f"missing {', '.join(missing)} (see `jobscan init --help`)")
    if args.key_stdin:
        key = sys.stdin.readline().strip()
    else:
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise ConfigError("no OpenRouter key: pipe it in with --key-stdin, or set OPENROUTER_API_KEY")
    return init.Answers(name=args.name, titles=args.title, exclude_titles=args.exclude_title,
                        locations=args.location, eligible_where=args.eligible_where,
                        tags=args.tag or list(tag.builtin_names()), key=key)


def _discover(home: Path) -> int:
    """Needs only a writable home: no model config, key, profile or boards file."""
    try:
        _setup_logging(home)
        with _run_lock(home):
            conn = store.open_db(home)
            try:
                outcome = discover.discover(conn)
            finally:
                conn.close()
    except ConfigError as e:
        print(f"jobscan: {e}", file=sys.stderr)
        return EXIT_USAGE
    except RunLocked:
        print(f"jobscan: another run is using {home}", file=sys.stderr)
        return EXIT_USAGE
    except CrawlError as e:
        log.error("discover_failed error=%r kept=previous_list", str(e))
        return EXIT_INCOMPLETE
    print(f"discovered\t{outcome.boards}\t{','.join(outcome.crawls)}")
    return EXIT_OK


def _check(home: Path) -> int:
    try:
        config = load_config(home)
        profile = load_profile(home)
        tag.definitions(profile.tags, profile.eligible_where, profile.custom_tags)
        email = load_email(home)
        listed, unrecognised = fetch.load_boards(config.boards_file) if config.boards_file else (None, 0)
        warnings = _scheduled_key(home, config)
    except ConfigError as e:
        print(f"jobscan: {e}", file=sys.stderr)
        return EXIT_USAGE
    print("\n".join(report.install(config, profile, email, None if listed is None else len(listed), unrecognised,
                                   _key_source(home, config), warnings)))
    return EXIT_OK


def _scheduled_key(home: Path, config: Config) -> list[str]:
    """The key as launchd's run reads it, with no shell environment: a key file present but unusable is an error, a
    key only in this shell's environment a warning."""
    if config.api_key is None:
        return []
    try:
        load_config(home, environ={})
    except ConfigError:
        if (home / KEY_FILE).exists():
            raise
        return [f"the key is only in OPENROUTER_API_KEY, which a scheduled run won't see: save it in "
                f"{home / KEY_FILE}, readable only by you (chmod 600)"]
    return []


def _key_source(home: Path, config: Config) -> str:
    if config.api_key is None:
        return "none needed"
    if os.environ.get("OPENROUTER_API_KEY"):
        return f"OPENROUTER_API_KEY here, {KEY_FILE} for scheduled runs" if (home / KEY_FILE).exists() \
            else "OPENROUTER_API_KEY"
    return KEY_FILE


def _boards(home: Path, args: argparse.Namespace) -> int:
    try:
        file = boards.path(load_config(home))
        if args.boards_command == "add":
            outcome = boards.add(file, args.urls, args.company)
        else:
            print("\n".join(report.board_list(file, boards.entries(file))))
            return EXIT_OK
    except ConfigError as e:
        print(f"jobscan: {e}", file=sys.stderr)
        return EXIT_USAGE
    print("\n".join(report.boards_added(file, outcome)))
    return EXIT_OK


def _tags(home: Path, name: str | None) -> int:
    try:
        profile = _profile_if_any(home)
        lines = report.tag_list(profile) if name is None else report.tag_detail(name, profile)
    except ConfigError as e:
        print(f"jobscan: {e}", file=sys.stderr)
        return EXIT_USAGE
    if lines is None:
        print(f"jobscan: no tag named {name!r}; `jobscan tags` lists them", file=sys.stderr)
        return EXIT_USAGE
    print("\n".join(lines))
    return EXIT_OK


def _profile_if_any(home: Path) -> Profile | None:
    """None before `init` has written a profile; a broken profile is still a ConfigError."""
    return load_profile(home) if any((home / "profiles").glob("*.toml")) else None


def _eval(home: Path, set_path: Path) -> int:
    try:
        config = load_config(home)
        items = evaluate.load_set(set_path)
    except ConfigError as e:
        print(f"jobscan: {e}", file=sys.stderr)
        return EXIT_USAGE
    try:
        report = evaluate.run_eval(items, config)
    except OSError as e:
        print(f"jobscan: could not save eval predictions under {home / 'eval'} ({type(e).__name__})", file=sys.stderr)
        return EXIT_INCOMPLETE
    print(evaluate.format_report(report))
    return EXIT_OK


def _setup_logging(home: Path) -> None:
    for old in list(log.handlers):  # main() can run more than once per process (tests)
        log.removeHandler(old)
        old.close()
    try:
        file_handler = logging.FileHandler(home / "runs.log")
    except OSError as e:
        raise ConfigError(f"{home / 'runs.log'}: not writable ({type(e).__name__})") from None
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in (logging.StreamHandler(sys.stderr), file_handler):
        handler.setFormatter(formatter)
        log.addHandler(handler)
    log.setLevel(logging.INFO)


class _run_lock:
    """One run per $JOBSCAN_HOME: overlapping runs would double-tag and race delivery."""

    def __init__(self, home: Path):
        self.path = home / ".lock"

    def __enter__(self):
        try:
            self.file = self.path.open("w")
        except OSError as e:
            raise ConfigError(f"{self.path}: not writable ({type(e).__name__})") from None
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise RunLocked from None
        return self

    def __exit__(self, *exc):
        self.file.close()
