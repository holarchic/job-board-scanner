# jobscan

jobscan finds company job boards on Ashby and Greenhouse, keeps the postings that match your titles and locations, and has a language model tag each match. You run it yourself; it writes a markdown digest of what's new.

Answers that need evidence quote the posting, and jobscan checks the quote is really there; a missing or unmatched quote shows ⚠. A quote that's there doesn't prove the answer right.

```
# ae-london — 2026-10-12 · 1 new

ⓘ marks a tag that hasn't been validated against labelled postings.

## Acme AI — Founding Account Executive · London
https://jobs.ashbyhq.com/acme/1 · Ashby · first seen 2026-10-12 · 14 open roles
- founding: **yes** — "our first and only sales hire"
- location_eligible: **yes** — "London (hybrid)"
- segment ⓘ: **mid_market, enterprise** — "mid-market and enterprise accounts"
- language_required: no
```

## Quick start

macOS or Linux, and an [OpenRouter](https://openrouter.ai) API key with credit.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
uv tool install git+https://github.com/holarchic/jobscan   # fetches Python 3.11+ if needed
jobscan init                   # asks for your search and your key
jobscan run                    # prints the path of your first digest
jobscan schedule --at 08:00    # Mac: runs it daily; Linux: prints a cron line for `crontab -e`
```

## Strengths and gaps

**Good at**
- Searches where a few titles narrow the field and the posting answers the rest. I built it for my London Account Executive search.
- Finding boards you didn't know about: about 4,000 on Ashby and 4,800 on Greenhouse, of which about 8,000 answer. Of my hand-built list it finds 111 of 114 Ashby boards and 33 of 37 Greenhouse ones.
- Cost: about $0.0005 a posting with the default model. My first runs: AE, 233 postings, 47 minutes, at least $0.11; PM, 169 postings, 7 minutes, $0.08, then 118 more from Greenhouse for $0.05.
- No runtime dependencies: standard-library Python and one SQLite file.

**Gaps**
- Ashby and Greenhouse only: no Lever, Workable, Workday (where Nvidia, Intel and Salesforce hire) or job sites. Companies that embed Greenhouse in their own careers page, such as Fivetran and Wiz, need the boards file.
- Tags label matches; they don't filter the digest. There's no spend cap.
- Tests cover AE and PM postings only, with just 8 founding roles. In the five-tag test, 8 of 11 `founding: yes` answers were right.
- No funding stage or company age; the open-role count is a rough read on size.
- Discovered companies show under their board name (`1password`, not `1Password`) unless you list them in a boards file.
- One search per install. A role posted separately per city, or reposted, appears each time.
- Heavy: every run fetches about 216,000 postings, in about eight minutes; Greenhouse alone is roughly 2 GB of downloads. The database grows to about 1.4 GB.
- Notifications are Mac only, and the schedule runs only while you're logged in.

## Tags

| Tag | Answers |
|-----|---------|
| `founding` | First and only hire in the role's function, across the company? |
| `location_eligible` | Can you work it from where you said? Visa sponsorship isn't checked. |
| `segment` | Sales roles: enterprise, mid-market or SMB customers (one or more) |
| `language_required` | A required language other than English, or `no` |
| `tech_company` | Is the company's main business technology? |

ⓘ marks the tags no labelled set has checked: `segment`, `tech_company`, `location_eligible` outside the UK, and your own.

## Your search

Your search lives in `~/.jobscan/profiles/`, a TOML file `init` writes; edit it any time. `init` also writes `config.toml` and your key in `openrouter.key`, readable only by you.

```toml
titles = ["Product Manager"]
exclude_titles = ["Product Marketing", "Project Manager", "Program Manager"]
locations = ["London", "United Kingdom", "UK", "Remote"]
eligible_where = "the UK, working from London"
tags = ["founding", "location_eligible", "language_required", "tech_company"]

[custom_tags.b2b]
definition = "The company sells to businesses, not consumers."
```

Titles and locations match whole words, ignoring case; `locations` is checked against the posting's location field only. `eligible_where` is what `location_eligible` asks the model about.

Your own tags run on every matching posting. Say what each answer means; `jobscan/tags.toml` has examples. `values` defaults to yes, no, unknown; `multi = true` answers a list; `default` is the answer when the posting is silent.

To add companies discovery misses, put `boards_file` above `[model]` in `config.toml`:

```toml
boards_file = "boards.json"          # relative to ~/.jobscan, or absolute
```

```json
{"companies": [{"company": "Acme AI", "board_url": "https://jobs.ashbyhq.com/acme"}]}
```

For a second search, `export JOBSCAN_HOME="$HOME/.jobscan-pm"`, then run `init`, `run` and `schedule` with it set.

## Running it

**Boards.** jobscan reads the three newest [Common Crawl](https://commoncrawl.org) crawls for Ashby and Greenhouse boards, in about twenty seconds. `run` does this itself every 30 days, and when jobscan learns a new job site; `jobscan discover` does it now. If any crawl can't be read, the old list stays. A discovered board is skipped after three "not found" answers without a successful read between them; boards you list are always tried.

**Digests.** `run` prints the search name, the count of new postings and the digest's path, in `~/.jobscan/digests/<search>/`. A second digest on the same day gets `-2`, `-3`. A posting appears in one digest only; a repost under a new ID is new. Matches the model couldn't tag wait for a later run. No new matches, no file. Exit codes: 0 done, 1 some postings left untagged or the digest couldn't be written, 2 a config or usage error. Logs: `~/.jobscan/runs.log`.

**Schedule.** On a Mac, `jobscan schedule --at 08:00` runs daily while you're logged in, and notifies you of new postings or a problem; a quiet day stays quiet. Asleep at 08:00: it runs when the Mac wakes, and several missed days make one run. Powered off: the next day's slot. Notifications may need allowing in System Settings › Notifications. `jobscan schedule --off` stops it.

**Updating and troubleshooting.** `uv tool upgrade jobscan` updates it; the schedule keeps working. `jobscan: command not found`: run `uv tool update-shell` and open a new terminal. A fresh Mac may ask to install Apple's command-line developer tools; say yes. If daily runs stop after a uv update, run `uv tool install --reinstall git+https://github.com/holarchic/jobscan`, then `jobscan schedule --at 08:00` again.

## Models

I tested models on 120 AE and PM postings, labelled by Claude and checked against my own labels and an audit. The bar: 90% accuracy per tag, 80% recall of the rarer answer, and 95% of quotes found verbatim.

| Model | Verdict | Cost per posting |
|-------|---------|------------------|
| **DeepSeek V4 Flash** (Parasail, fp8, reasoning on) | The default. Passes the three scored tags with all five in the prompt; `founding` precision 8/11 (73%) | ~$0.0005 |
| Qwen 3.8 27B (reasoning on) | Passed the earlier test, best on `founding` (8/8); ~4× the output tokens | ~$0.009 |
| gpt-oss-120b | Missed the earlier bar on `segment` and on quotes; `founding` precision ~40% | ~$0.0003 |

In the earlier test, turning reasoning off cut Flash's `founding` precision from 89% to 38%. `init` pins the provider and quantisation, since the same model served elsewhere can behave differently. Another endpoint can go in `base_url`; it gets no API key and must support strict JSON-schema output. Local models are untested.

`jobscan eval <set.json>` scores `founding`, `location_eligible` and `language_required` against your own labelled postings: a JSON list of `{id, title, location, description, labels}`, where `labels` maps tag names to answers. It judges eligibility for the UK and uses the five built-in tags, not your profile or your own tags.

## Tests

In a clone, with Python 3.11+ on your path:

```bash
python3 -m unittest      # offline: fake boards, model and Common Crawl
tests/smoke.sh           # live: three real boards; export OPENROUTER_API_KEY first; costs under $0.01
```

## License

MIT
