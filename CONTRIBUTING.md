# Contributing

Contributions are welcome: bug reports, fixes, new job-board adapters, docs.

For anything bigger than a small fix, open an issue first so we can agree the approach before you build it.

## The one hard rule

Standard-library Python 3.11+ only. No runtime dependencies, ever: the install is one `uv tool install` on a machine that may have nothing else. `ruff` is a dev tool and stays out of `pyproject.toml`'s dependencies.

## Setup

In a clone:

```bash
python3 -m unittest      # offline: fake boards, model and Common Crawl
ruff check .             # config in pyproject.toml
```

`tests/smoke.sh` runs two real `jobscan run`s over three live boards with the real model. It needs `OPENROUTER_API_KEY` and costs under $0.01, so CI never runs it.

## Conventions

- Tests use synthetic fixtures. No real job-ad text, keys, profiles or digests in tracked files; the one exception is the README's sample digest screenshot, `docs/digest-email.png`.
- One board's failure never stops a run, and a board's job list parses completely or not at all, so a partial read never closes live postings. The exceptions are jobs that must never reach a digest, a Personio job with no name (no title filter could match it) and a Gem demo job: they are skipped.
- Tag definitions are data (`jobscan/tags.toml`), not code.
- A new job board is a new adapter file (`ATS`, `DISPLAY_NAME`, `CRAWL_HOSTS`, `api_board()`, `page_board()`, `read()`, `describe`, `company()`) plus one line in `fetch.ADAPTERS`. `read(board, get)` reads the board's whole job list through `get`, fetch's request with retries, paging if it must. `describe` is `None` when the list carries descriptions; otherwise `describe(board, posting, get)` returns one posting's description, fetched only for postings the profile matched. `company(board, get)` returns the board's company name, and its website if the job site gives one, looked up once per board; it raises `BoardError` when the answer names no company. Keep any board-page URL it reads in a module constant (`BOARD_PAGE`) so the run tests can point it at their fake server. A crawl host written `*.example.com` covers every subdomain. Check its API's rate limits at full scale before giving it crawl hosts.

## Pull requests

- One concern per PR, with tests.
- `python3 -m unittest` and `ruff check .` pass.
- Say what changed and how you verified it.

## Bug reports

Include what you ran, what you expected, the relevant lines of `~/.jobscan/runs.log`, and your OS and Python version. Leave out your key, your profile and digest contents.

## License

By contributing, you agree your contributions are licensed under the MIT License.
