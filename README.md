# jobscan

A job-board scanner you run yourself. It finds company job boards on its own, reads them, keeps the postings whose title and location match, has an LLM tag each one, and writes a markdown digest of what's new since the last run.

Every tag comes with a quote from the posting, and jobscan checks the quote is really in the posting. When it isn't, the digest marks it ⚠ instead of hiding it.

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

Standard-library Python and one SQLite file. No dependencies.

## Where the boards come from

jobscan reads the three newest [Common Crawl](https://commoncrawl.org) crawls and keeps every job board they captured: about 4,000 Ashby boards, in ten seconds. Checked against a hand-built list of 114 tech companies, it finds 111. `run` refreshes the list itself every 30 days, as new crawls land; `jobscan discover` forces it.

Boards it can't see go in a boards file, which `run` reads alongside the discovered ones. A board that answers "not found" three runs in a row is dropped from the discovered list; boards you list yourself are never dropped.

## What it's good for

Roles at tech companies, startups and scale-ups especially, where the title gets you most of the way and the posting has to tell you the rest. I built it for my own Account Executive search in London; it works the same for PM, engineering or any role you can name in a few titles.

The tags answer what a title can't:

| Tag | Answers |
|-----|---------|
| `founding` | Is this the first and only hire in the role at the company? |
| `location_eligible` | Can you take it from where you are? |
| `segment` | Enterprise, mid-market or SMB (one or more) |
| `language_required` | Does it need a language other than English? |
| `tech_company` | Is the company's main business technology? |

Add your own in the profile (below). A tag no labelled set has checked shows ⓘ: your own tags, `segment`, `tech_company`, and `location_eligible` anywhere but the UK.

"Open roles" counts every open posting on the company's board, not just the matches: a rough read on size.

It reads Ashby boards today; Greenhouse and Lever come next. Companies hiring through Workday and the like, which is most of big tech, are out of reach.

Companies found by discovery show under their board name (`1password`, not `1Password`); list a company in the boards file to name it properly.

## Install

You need an [OpenRouter](https://openrouter.ai) API key. Then, in a terminal:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
uv tool install git+https://github.com/holarchic/jobscan
jobscan init
```

The second line puts uv, and the `jobscan` it installs, on this terminal's path; new terminals have them already. uv installs a recent Python by itself if yours is too old (a Mac's is). If a terminal ever says `jobscan: command not found`, run `uv tool update-shell` and open a new window. On a fresh Mac the install may ask to install Apple's command-line developer tools; say yes.

`init` asks what you're looking for — job titles, titles to leave out, locations, where you can work from, which tags — and your OpenRouter key. It writes everything under `~/.jobscan/`: `config.toml`, your profile in `profiles/`, and the key in `openrouter.key`, readable only by you.

## Run

```bash
jobscan run
```

The first run finds the boards, then tags every posting that matches: a backlog. For my London AE profile that was 233 postings from 4,000 boards: fetching took under a minute, tagging 47 minutes, and the model cost $0.11. Later runs tag only what's new and take a minute or two.

`run` prints `profile <tab> new postings <tab> digest path`, and the digest lands in `~/.jobscan/digests/<profile>/<date>.md`. A posting appears in exactly one digest. Run it again straight away and you get 0 new and no file.

If the model fails on some postings, they stay out of today's digest and appear in the next run's. The run exits 1 when that happens, 0 when everything was tagged, and 2 on a config problem. Logs go to `~/.jobscan/runs.log`.

## Schedule

```bash
jobscan schedule --at 08:00
```

On a Mac this runs jobscan every day at 08:00 and shows a notification when there are new postings or something went wrong; a quiet day stays quiet. If the Mac is asleep at 08:00, the run starts when it wakes, and several missed days make one run. If it's off, the next day's slot runs. The first notification may need allowing in System Settings › Notifications.

`jobscan schedule --off` stops it. If the daily runs stop after a uv update, reinstall and schedule again:

```bash
uv tool install --reinstall git+https://github.com/holarchic/jobscan
jobscan schedule --at 08:00
```

On Linux or a VPS, `jobscan schedule --at 07:00` prints a cron line instead; add it with `crontab -e`.

## Your search

The profile is plain TOML; edit it any time. Titles and locations match whole words, ignoring case.

```toml
titles = ["Product Manager", "Senior Product Manager"]
exclude_titles = ["Product Marketing", "Project Manager", "Program Manager"]
locations = ["London", "United Kingdom", "UK", "Remote"]
eligible_where = "the UK, working from London"
tags = ["founding", "location_eligible", "language_required", "tech_company"]

[custom_tags.b2b]
definition = "The company sells to businesses, not consumers."
values = ["yes", "no", "unknown"]   # optional; this is the default
# multi = true answers a list; default = "no" is the answer when the posting doesn't say
```

A custom tag is a question in plain words; the model answers it for every matching posting, quoting the ad. Write it the way the built-ins in `jobscan/tags.toml` are written: what counts as each answer.

`config.toml` holds the model and, optionally, a boards file for companies discovery misses:

```toml
boards_file = "boards.json"          # optional; relative to ~/.jobscan, or absolute

[model]
base_url = "https://openrouter.ai/api/v1"
slug = "deepseek/deepseek-v4-flash"
provider = "parasail"                # pinned, with no fallback to another provider
quantization = "fp8"
```

```json
{"companies": [
  {"company": "Acme AI", "board_url": "https://jobs.ashbyhq.com/acme"}
]}
```

One profile per install. Set `JOBSCAN_HOME` to keep a second search in its own directory; each one gets its own `init` and its own schedule.

## Models

I tested models on 120 hand-labelled sales postings, with a bar of 90% accuracy per tag and 95% of quotes found verbatim.

| Model | Verdict | Cost per posting |
|-------|---------|------------------|
| **DeepSeek V4 Flash** (Parasail, fp8, reasoning on) | Recommended. Clears the bar on every tag; `founding` precision is the weak spot (~73% with all five tags) | ~$0.0005 |
| Qwen 3.8 27B (reasoning on) | Clears the bar, best on `founding`; ~4× the output tokens | ~$0.009 |
| gpt-oss-120b | Misses the bar on `segment` and on quotes; `founding` precision ~40% | ~$0.0003 |

Reasoning is what makes `founding` work: with it off, Flash's `founding` precision fell from 89% to 38%. Pin the provider and quantisation, because the same model served elsewhere can behave differently (DeepSeek V4 Pro on one provider effectively ran without reasoning).

Any OpenAI-compatible endpoint can go in `base_url`; the API key is only ever sent to OpenRouter. Local models are untested.

`jobscan eval <set.json>` scores `founding`, `location_eligible` and `language_required` against your own labelled postings (a JSON list of `{id, title, location, description, labels}`; `location_eligible` is judged for the UK), so you can test a model or a tag change before trusting it.

## Tests

From a clone of the repo:

```bash
python3 -m unittest      # offline: fake boards, model and Common Crawl files
tests/smoke.sh           # live, three real boards, needs OPENROUTER_API_KEY, costs < $0.01
```

## License

MIT
