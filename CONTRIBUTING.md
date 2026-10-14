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
- One board's failure never stops a run, and a board's job list parses completely or not at all, so a partial read never closes live postings. The one exception is a job no title filter could ever match, such as a Personio job with no name: it is skipped.
- Tag definitions are data (`jobscan/tags.toml`), not code.
- A new job board is a new adapter file (`ATS`, `DISPLAY_NAME`, `DECODE`, `CRAWL_HOSTS`, `api_board()`, `page_board()`, `parse()`) plus one line in `fetch.ADAPTERS`. `DECODE` turns the response body into what `parse` reads: `json.loads`, or `payload.xml_root` for an XML feed. A crawl host written `*.example.com` covers every subdomain. Check its API's rate limits at full scale before giving it crawl hosts.

## Pull requests

- One concern per PR, with tests.
- `python3 -m unittest` and `ruff check .` pass.
- Say what changed and how you verified it.

## Bug reports

Include what you ran, what you expected, the relevant lines of `~/.jobscan/runs.log`, and your OS and Python version. Leave out your key, your profile and digest contents.

## License

By contributing, you agree your contributions are licensed under the MIT License.
