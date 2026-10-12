#!/usr/bin/env bash
# Live smoke test: two headless `jobscan run`s over three small Ashby boards with the real model.
# Needs OPENROUTER_API_KEY. Cost is estimated, not capped: the preflight admits 1-3 matching postings
# (worst observed G2 call $0.00097 x 3 tries x 3 postings ~ $0.009) and the run's recorded cost is asserted < $0.01.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
: "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY must be set}"
home=$(mktemp -d)
trap 'rm -rf "$home"' EXIT
cp -R "$root/tests/smoke/." "$home/"

# Same clean environment for every step; only the jobscan runs get stdin closed, since the checks
# read their Python from a heredoc on stdin.
clean_env() {
  env -i PATH="$PATH" HOME="$home" JOBSCAN_HOME="$home" OPENROUTER_API_KEY="$OPENROUTER_API_KEY" \
    PYTHONPATH="$root" PYTHONDONTWRITEBYTECODE=1 "$@"
}
headless_run() { clean_env python3 -m jobscan run </dev/null; }

clean_env python3 - <<'PY'
from jobscan import fetch, prefilter
from jobscan.config import jobscan_home, load_config, load_profile
home = jobscan_home()
config, profile = load_config(home), load_profile(home)
boards, _skipped = fetch.load_boards(config.boards_file)
outcomes = list(fetch.fetch_each(boards))
fetched = [o for o in outcomes if isinstance(o, fetch.BoardFetch)]
failed = [o for o in outcomes if isinstance(o, fetch.BoardFailure)]
matched = [p for r in fetched for p in r.postings if prefilter.matches(p, profile)]
assert not failed, f"preflight: boards failed {[f.board.slug for f in failed]}"
assert 1 <= len(matched) <= 3, f"preflight: {len(matched)} postings match; the fixture needs 1-3 (update tests/smoke/)"
assert all(len(p.description) <= 15000 for p in matched), "preflight: a matched description exceeds 15,000 chars"
print(f"preflight: {len(matched)} matching postings")
# A fresh discovered list holding one dead board: `run` reads exactly the boards file's three, without crawling.
from datetime import UTC, datetime
from jobscan import store
from jobscan.records import Board
conn = store.open_db(home)
store.replace_discovered(conn, [Board("ashby", "sentinel", "sentinel", "http://127.0.0.1:9/never")],
                         datetime.now(UTC).isoformat(timespec="microseconds"))
with conn:
    conn.execute("UPDATE boards SET not_found = ?", (store.DEAD_AFTER,))
conn.close()
PY

first=$(headless_run 2>>"$home/smoke-stderr.log") || { echo "FAIL: first run exited $?"; cat "$home/smoke-stderr.log"; exit 1; }
second=$(headless_run 2>>"$home/smoke-stderr.log") || { echo "FAIL: second run exited $?"; exit 1; }
echo "run 1: $first"
grep -q "fetch_start boards=3 " "$home/runs.log" || { echo "FAIL: run did not read exactly the three smoke boards"; exit 1; }
if grep -qE "discover( crawl=|_done|_failed)" "$home/runs.log"; then echo "FAIL: run crawled Common Crawl"; exit 1; fi
echo "run 2: $second"

clean_env env FIRST="$first" SECOND="$second" python3 - <<'PY'
import os, sqlite3
from pathlib import Path
from jobscan import tag
from jobscan.config import jobscan_home, load_profile
home = jobscan_home()
profile, new, path = os.environ["FIRST"].split("\t")
assert int(new) >= 1 and path != "-", "first run delivered nothing"
assert os.environ["SECOND"].split("\t")[1:] == ["0", "-"], f"second run not empty: {os.environ['SECOND']!r}"
digest = Path(path).read_text()
db = sqlite3.connect(home / "jobscan.sqlite")
profile_cfg = load_profile(home)
founding_hash = next(d.hash for d in tag.definitions(profile_cfg.tags, profile_cfg.eligible_where)
                     if d.name == "founding")
rows = db.execute("""SELECT t.posting_key, t.model_id, t.def_hash, t.quote, t.verified FROM tag_results t
                     JOIN postings p ON p.key = t.posting_key AND p.input_hash = t.input_hash
                     JOIN deliveries d ON d.posting_key = t.posting_key""").fetchall()
calls = {}  # (posting, model) -> that call's results: all tags of one posting are saved from one reply
for key, model_id, def_hash, quote, verified in rows:
    calls.setdefault((key, model_id), []).append((def_hash, quote, verified))
evidence = [quote for results in calls.values() if any(h == founding_hash for h, _, _ in results)
            for _, quote, verified in results if verified and quote and quote.strip()]
assert evidence, "no delivered posting has a model-sourced founding value plus a verified quote from the same reply"
assert any(f'"{quote}"' in digest for quote in evidence), "verified quote missing from the rendered digest"
cost, complete = db.execute("SELECT sum(cost), min(cost_complete) FROM runs").fetchone()
assert complete == 1, "some model reply reported no cost: the recorded total is only a lower bound"
assert 0 < cost < 0.01, f"recorded cost ${cost:.4f} is not in (0, $0.01)"
print(f"PASS: {new} postings tagged and delivered, second run empty, cost ${cost:.4f}")
PY
