"""Command-line entry point: parses arguments, owns logging, the run lock and exit codes; dispatches to the modules."""
import argparse
import fcntl
import logging
import sys
from pathlib import Path

from jobscan import discover, evaluate, init, scan, store, tag
from jobscan.config import ConfigError, jobscan_home, load_config, load_profile
from jobscan.crawl import CrawlError

EXIT_OK, EXIT_INCOMPLETE, EXIT_USAGE = 0, 1, 2
log = logging.getLogger("jobscan")


class RunLocked(Exception):
    """Another jobscan process holds this $JOBSCAN_HOME."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobscan")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="set up a search: asks in the terminal, writes config, profile and key")
    commands.add_parser("discover", help="rebuild the board list from the newest Common Crawl crawls (monthly)")
    commands.add_parser("run", help="fetch → filter → tag → write the profile's digest")
    eval_parser = commands.add_parser("eval", help="score the tagging path against a labelled posting set")
    eval_parser.add_argument("set", type=Path, help="JSON list of {id, title, location, description, labels}")
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return EXIT_OK if e.code == 0 else EXIT_USAGE
    home = jobscan_home()
    if args.command == "init":
        return _init(home)
    if args.command == "eval":
        return _eval(home, args.set)
    if args.command == "discover":
        return _discover(home)
    try:
        config = load_config(home)
        profile = load_profile(home)
        defs = tag.definitions(profile.tags, profile.eligible_where, profile.custom_tags)
        _setup_logging(home)
        with _run_lock(home):
            outcome = scan.scan(config, profile, defs)
    except ConfigError as e:
        print(f"jobscan: {e}", file=sys.stderr)
        return EXIT_USAGE
    except RunLocked:
        print(f"jobscan: another run is using {home}", file=sys.stderr)
        return EXIT_USAGE
    print(f"{outcome.profile}\t{outcome.new}\t{outcome.digest_path or '-'}")
    return EXIT_OK if outcome.complete else EXIT_INCOMPLETE


def _init(home: Path) -> int:
    try:
        setup = init.init(home)
    except ConfigError as e:
        print(f"jobscan: {e}", file=sys.stderr)
        return EXIT_USAGE
    except init.InitCancelled:
        print("\njobscan: cancelled; nothing was written", file=sys.stderr)
        return EXIT_USAGE
    written = [setup.config, setup.profile] + ([setup.key] if setup.key else [])
    print("\nWrote " + ", ".join(str(p) for p in written) + ".")
    print(init.NEXT_STEPS.format(profile=setup.profile))
    return EXIT_OK


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
        log.error("discover_failed error=%r (the previous board list is kept)", str(e))
        return EXIT_INCOMPLETE
    print(f"discovered\t{outcome.boards}\t{','.join(outcome.crawls)}")
    return EXIT_OK


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
