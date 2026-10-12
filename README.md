# jobscan

A job-board scanner you run yourself. It finds about 9,000 company job boards on its own, keeps the postings that match your titles and locations, has an LLM tag each one, and writes a markdown digest of what's new.

Every tag quotes the posting, and jobscan checks the quote is really there; when it isn't, the tag is marked ⚠, not hidden.

```
# ae-london — 2026-10-12 · 1 new

ⓘ marks a tag that hasn't been validated against labelled postings.

## Acme AI — Founding Account Executive · London
https://jobs.ashbyhq.com/acme/1 · Ashby · first seen 2026-10-12 · 14 open roles
- founding: **yes** — "our first sales hire in Europe"
- location_eligible: **yes** — "London (hybrid)"
- segment ⓘ: **mid_market, enterprise** — "mid-market and enterprise accounts"
- language_required: no
```

## Quick start

You need an [OpenRouter](https://openrouter.ai) API key.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh       # uv; it fetches a recent Python if yours is too old
source $HOME/.local/bin/env                            # uv on this terminal's path
uv tool install git+https://github.com/holarchic/jobscan
jobscan init                                           # asks what you're looking for, and your key
jobscan run                                            # first digest
jobscan schedule --at 08:00                            # then every day
```

## Strengths and gaps

**Good at**
- Roles at tech companies, especially startups and scale-ups, where the title gets you most of the way and the posting tells you the rest. I built it for my London Account Executive search; it works the same for any role you can name in a few titles.
- Finding boards you didn't know about: about 4,000 on Ashby and 4,800 on Greenhouse. Of my hand-built list it finds 111 of 114 Ashby boards and 33 of 37 Greenhouse ones.
- Cheap: about $0.0005 a posting. The first run tags a backlog (my AE search: 233 postings, 47 minutes, $0.11; a PM search: 169, 7 minutes, $0.08); later runs still fetch every board (about eight minutes) and tag only what's new.
- Evidence: every tag quotes the ad; unchecked tags are marked ⓘ.
- No dependencies: standard-library Python and one SQLite file.

**Gaps**
- Ashby and Greenhouse boards only. Lever comes next; Workday and the like, which is most of big tech, are out of reach. Companies that embed Greenhouse in their own careers page (Fivetran, Wiz) need the boards file.
- Heavy: a full run fetches about 215,000 postings in eight minutes, Greenhouse's alone roughly 2 GB of downloads, and the database grows to about 1.5 GB.
- No funding stage or company age. The open-role count is a rough read on size.
- Companies found by discovery show under their board name (`1password`, not `1Password`) unless you list them in a boards file.
- Tags are validated on AE and PM postings only. `founding` is right about three times in four when it says yes.
- One search per install; the same role posted in several cities appears once per city.
- Notifications are Mac only; on Linux, `schedule` gives you a cron line.

## Tags

| Tag | Answers |
|-----|---------|
| `founding` | Is this the first and only hire in the role at the company? |
| `location_eligible` | Can you take it from where you are? |
| `segment` | Enterprise, mid-market or SMB (one or more) |
| `language_required` | Does it need a language other than English? |
| `tech_company` | Is the company's main business technology? |

Add your own in the profile. ⓘ marks your own tags, `segment`, `tech_company`, and `location_eligible` anywhere but the UK.

## Your search

`init` writes everything to `~/.jobscan/`: `config.toml`, your profile in `profiles/`, and your key in `openrouter.key`, readable only by you. The profile is plain TOML; edit it any time. Titles and locations match whole words, ignoring case.

```toml
titles = ["Product Manager", "Senior Product Manager"]
exclude_titles = ["Product Marketing", "Project Manager", "Program Manager"]
locations = ["London", "United Kingdom", "UK", "Remote"]
eligible_where = "the UK, working from London"
tags = ["founding", "location_eligible", "language_required", "tech_company"]

[custom_tags.b2b]
definition = "The company sells to businesses, not consumers."
values = ["yes", "no", "unknown"]   # the default; multi = true answers a list; default = "no" if the ad is silent
```

A custom tag is a question in plain words, answered for every matching posting with a quote. Say what counts as each answer, as the built-ins in `jobscan/tags.toml` do.

To add companies discovery misses, point `config.toml` at a boards file:

```toml
boards_file = "boards.json"          # relative to ~/.jobscan, or absolute
```

```json
{"companies": [{"company": "Acme AI", "board_url": "https://jobs.ashbyhq.com/acme"}]}
```

For a second search, set `JOBSCAN_HOME` to another directory and run `init` there.

## Running it

**Boards.** jobscan reads the three newest [Common Crawl](https://commoncrawl.org) crawls and keeps every Ashby and Greenhouse board they captured, in about twenty seconds. `run` refreshes the list every 30 days, and when jobscan learns a new job site; `jobscan discover` forces it. A board that answers "not found" three runs in a row is dropped; boards you list are never dropped.

**Digests.** `run` prints `profile <tab> new <tab> digest path` and writes `~/.jobscan/digests/<profile>/<date>.md`. A posting appears in one digest only; run again straight away and you get 0 new and no file. Postings the model fails on wait for the next run. Exit codes: 0 done, 1 some postings left untagged, 2 config problem. Logs: `~/.jobscan/runs.log`.

**Schedule.** On a Mac, `jobscan schedule --at 08:00` runs every day and notifies you of new postings or a problem; quiet days stay quiet. Asleep at 08:00: it runs on wake, and missed days make one run. Off: the next day's slot. The first notification may need allowing in System Settings › Notifications. `jobscan schedule --off` stops it.

**Troubleshooting.** `jobscan: command not found`: run `uv tool update-shell` and open a new terminal. A fresh Mac may ask to install Apple's command-line developer tools; say yes. If daily runs stop after a uv update, run `uv tool install --reinstall git+https://github.com/holarchic/jobscan`, then `jobscan schedule --at 08:00` again.

## Models

I tested models on 120 hand-labelled AE and PM postings, with a bar of 90% accuracy per tag and 95% of quotes found verbatim.

| Model | Verdict | Cost per posting |
|-------|---------|------------------|
| **DeepSeek V4 Flash** (Parasail, fp8, reasoning on) | Recommended, and the default. Clears the bar on every tag; `founding` precision ~73% with all five tags | ~$0.0005 |
| Qwen 3.8 27B (reasoning on) | Clears the bar, best on `founding`; ~4× the output tokens | ~$0.009 |
| gpt-oss-120b | Misses the bar on `segment` and on quotes; `founding` precision ~40% | ~$0.0003 |

Reasoning is what makes `founding` work: with it off, Flash's precision fell from 89% to 38%. Pin the provider and quantisation in `config.toml`: the same model served elsewhere can behave differently. Any OpenAI-compatible endpoint can go in `base_url`; the key is only ever sent to OpenRouter. Local models are untested.

`jobscan eval <set.json>` scores `founding`, `location_eligible` and `language_required` against your own labelled postings (a JSON list of `{id, title, location, description, labels}`; `location_eligible` is judged for the UK), so you can test a model or a tag change before trusting it.

## Tests

In a clone of the repo:

```bash
python3 -m unittest      # offline: fake boards, model and Common Crawl
tests/smoke.sh           # live: three real boards, needs OPENROUTER_API_KEY, < $0.01
```

## License

MIT
