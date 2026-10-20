# Architecture

Updated: 20/10/2026

How jobscan is built, module by module. The README says what it does and how to use it; this file is for changing it. Each section names a module's purpose, its main functions and types, and the decisions behind them.

## Pipelines

`discover` → Common Crawl index files → boards table. `run`: refresh discovery if stale → boards file ∪ live discovered boards → fetch → upsert → prefilter → describe (ATSes whose lists lack descriptions) → name (a board's company, looked up once) → tag (newest first, up to the profile's per-run limits; the rest wait for later runs) → select (`require`, and the agency check unless `include_agencies`: what they definitely rule out is listed under Left out) → digest (→ email with `email.toml` → notification with `--notify`). One fetch pass serves the profile. Setup: `init` writes the install (email optional, last), from questions or flags; `email` sets up or removes emailing, likewise; `boards add` appends to the boards file; `check` loads the install as `run` and launchd would; `preview` counts the saved profile's matches among stored postings as the run counts them; `tags` lists tags; `schedule` installs a launchd job running `run --notify`; `ui` does all of it on a local page, one step at a time, pre-filled from the person's own description of the search.

## Command line

What each command parses, prints and exits with.

### `jobscan/arguments.py`

every command, flag and help text (`parser()`): `jobscan --help` lists set-up in order (`SET_UP`); each command's help ends with examples using each of its flags (`tests/test_arguments.py` holds it to that); commands added in set-up order

### `jobscan/cli.py`

dispatch on `arguments.parser()`, logging setup, the per-home run lock, exit codes (0 done · 1 carried over / digest write or email failed / a crawl unreadable / launchd refused the schedule / setup's test email refused · 2 config or usage error); dispatches to `init`, `email`, `discover`, `scan`, `schedule` and `evaluate`, and handles `check` (the effective config, then `config.key_only_in_environment`: a key only in the environment is a warning, an unusable key file exit 2), `tags`, `boards` and `preview` (no database → says run first; under the run lock, held → exit 1); `init` and `email` by flags (`_init_answers` with `_requirements` for `--require TAG=VALUE`, `_email_settings`; secrets from stdin, else the environment for the key; `INIT_FLAGS` decides flags mode; `--max-tag-per-run`/`--max-spend-per-run` default to None there, the profile's defaults applied after), refused with exit 2 when there are no flags and no terminal; after a run, hands the published digest to `mail.send_digest` (a `MailError` → exit 1 and the notification) and words the `--notify` message (`_news`: new postings, postings carried over, matches waiting under the tagging limits; `_refused`); logs `run_start` with setup time

### `jobscan/report.py`

the text `tags`, `check`, `boards`, `preview` and `titles` print, from typed values; `limits` (a profile's tagging limits as one phrase, which check, preview, the notification and the page's summary share), `dollars` and `spend_text` (an amount exactly: cents when they read back the same, else every digit; the page shows a stored spend by it) (`check` and `preview_lines` state the profile's `require`, which preview doesn't count)

### `jobscan/install.py`

`check(home)` → `Install` (config, profile, email, boards-file counts, key source, scheduled-key warning) or the first `ConfigError`: `jobscan check` and the page's Save both use it

### `jobscan/lock.py`

`run_lock(home)` (never waits; `RunLocked`) and the lock every database user takes: run, discover, `preview`, `titles`, the page

### `jobscan/notify.py`

best-effort macOS notification via `osascript`, text as arguments, bounded, never raises

## Configuration and storage

The install's files, the database and its upgrade backup.

### `jobscan/config.py`

`profile_toml(profile)` (the one profile writer, custom tags included, beside the one reader), `custom_tag(name, table, source)` (the page's custom tags by the profile's rules), `toml_string`/`toml_list`; `config.toml` (its `boards_file`, else `BOARDS_FILE` = `$JOBSCAN_HOME/boards.json` when it exists) and the single profile → `Config`, `ModelConfig`, `Profile` (with `CustomTag`s and `Requirement`s: `require` read as a table of strings, its names and values checked by `tag.profile_definitions`; `include_agencies`, a boolean written only when true; `max_tag_per_run` and `max_spend_per_run`, defaults `MAX_TAG_PER_RUN` 500 and `MAX_SPEND_PER_RUN` $1.00, written only when not the default, the spend by `repr` so it reads back exactly, each checked by `check_max_tag`/`check_max_spend`, the one rule the reader, `init.validate_profile` and the page share), or `ConfigError`; the OpenRouter key is read only for https `openrouter.ai`, from the environment (injectable, so `schedule` checks as launchd will run) or the key file; `load_email` → `EmailConfig` or None (read by `run`, `schedule` and setup's checks; `is_mailbox` also requires the email library to read the address back unchanged; the `is_*` checks are shared with setup; parse errors never quote the file); secret files are refused when group- or world-readable

### `jobscan/store.py`

SQLite (`DB_FILE`; `exists` checks without creating; opened only through `open_db`, which calls `backup.before_open` first; `delivered_keys` per profile): postings (closed by fetched keys per successful board), tag-result cache, deliveries, discovered boards (with a consecutive-404 count; dead at `DEAD_AFTER`), companies (by board id, apart from `boards` so a lookup survives rediscovery; a plain `IF NOT EXISTS` table, no version step), runs (with `cost_complete`); `pending` reads lazily, scoped to the run's boards; `open_counts` per board; `discovered_at` = the list's replacement time; `PRAGMA user_version` migrations

### `jobscan/backup.py`

`before_open(path)`, called first by `store.open_db`: when the package's file hash (`code_identity`) differs from `backups/opened-by`, prunes to the newest `auto-*.sqlite`, copies the database through SQLite's backup API to a temp file, publishes it by hard link as `auto-<UTC µs>-<hash[:8]>.sqlite`, then records the hash; no database yet → record only; any failure → `ConfigError` (the run exits 2), marker untouched

## Job sites

One adapter file per ATS, the shared request and parsing code, and the registry that routes a board URL to its adapter.

### `jobscan/records.py`

`Board`, `Posting` (both carry their ATS), `Company` (name, website or None), `BoardError`, `Get` (an adapter's view of fetch's request: URL, and a JSON body to POST if any → body), `Places` (the profile's location test, `prefilter.in_places`)

### `jobscan/fetch.py`

boards file → boards via the `ADAPTERS` registry (`read_boards_file` the one reader, `entry_board` an entry's board; `boards` shares both); `fetch_each(boards, places)` streams outcomes, each ATS in its own lane of `WORKERS` with every lane running at once, at most `IN_FLIGHT` boards per lane in memory, logging `fetch_lane_done` as each ATS finishes; `describe_all` fetches matched postings' descriptions and `company_all` boards' companies, `WORKERS` at a time, each failure its own `DescribeFailure` / `CompanyFailure`; each of the three sends through one `connections.Connections`, closed after its workers finish. Every request (a GET, or a POST of a JSON body): gzip asked for and inflated to at most `MAX_BODY` (256 MB; another encoding fails the board), HTTP 200 only (any other a `StatusError` carrying it), retries, every 429 logged under its board; a failure carries its HTTP status; one board's or posting's failure never stops the run. `board_for` routes any board URL: every adapter's `api_board` first, then every `page_board`, so no adapter knows another's paths. A new ATS = a new adapter file (`ATS`, `DISPLAY_NAME`, `CRAWL_HOSTS` (`*.domain` = every subdomain), `api_board()`, `page_board()`, `read(board, get, places)` (its list, paged if need be, all-or-nothing; it may leave out postings whose places all fail `places`, as Workday does), `describe` (None when the list carries descriptions, else `describe(board, posting, get)` → the posting's description), `company(board, get)` → `Company` (board-page URLs in a `BOARD_PAGE` constant, or composed at call time from a `SITE` host template, so run tests can point them at the fake), `same_job` (None, or `same_job(key)` → the key all postings of one job share across boards)) plus one registry line; `same_job(key)` dispatches it by the key's ATS

### `jobscan/connections.py`

HTTP over `http.client` for fetch: `Connections` (one kept connection per thread, to the host it last asked, kept only after a whole HTTP 200 its server didn't close; a request on a kept connection the server dropped sent once more at once on a new one; `close` on exit), `send(url, body, headers)` → `Answer` (status, headers, body read only for a 200), URLs by `payload.http_url`'s rule, an IPv6 host's port defaulted by scheme; GET redirects to http(s) followed up to `MAX_REDIRECTS`, the Location percent-encoded from its Latin-1 header bytes as urllib does, any other redirect the answer; no proxy. Statuses are answers: fetch decides success and retries

### `jobscan/payload.py`

shared by adapters: `http_url` (urlsplit + scheme/host/port checks), `board_slug` (percent-decoded, case kept, printable ASCII without `/:?#`; Ashby and Greenhouse case-fold on top), `xml_root` (an XML feed's root: any malformed or undecodable feed is a `BoardError`), `xml_text` (an element's stripped text, "" when absent), `with_remote` (adds `Remote` unless a place says it as a word), `detail_text` (a posting detail's string field, errors naming where), `text_field` (string fields that fail the board when malformed), `html_to_text` (`html.parser`, block tags → line breaks), `encodable` (no lone surrogate: the one rule fetch, boards and init use), `one_line` (unprintable characters dropped, whitespace collapsed), `named_company` (a name entity-decoded and flattened by `one_line`, a website only if a printable, whitespace-free http(s) URL; no name is a `BoardError`), `element_text` (an element's raw text by plain search, linear on any page) and `html_title`

### `jobscan/ashby.py`

Ashby adapter: `api_board` (posting-API URL, any host) and `page_board` (jobs.ashbyhq.com) → `Board` (slug case-folded); payload → postings, all-or-nothing per board; `CRAWL_HOSTS` for discovery; `company` from the board page's `window.__appData` (name and public website; the page URL keeps the slug's %-escapes, as the API URL does)

### `jobscan/greenhouse.py`

Greenhouse adapter: `api_board` (`/v1/boards/`, any host) and `page_board` (job-boards, EU, legacy, embed; host matched case-insensitively) → one `Board` (token percent-decoded and case-folded, re-encoded for the API); `?content=true` payload → postings, content unescaped once or twice only while the content is wholly escaped with known description tags, then `html_to_text`; location = job location + offices; crawls job-boards.greenhouse.io and its EU twin; `company` = the board API record's `name`

### `jobscan/workable.py`

Workable adapter, boards file only (`CRAWL_HOSTS` empty: a full sweep draws HTTP 429): `api_board` (`/api/v1/widget/accounts/<slug>`, any host) and `page_board` (apply.workable.com, not `/j/` links) → `Board` (slug case kept: the API is case-sensitive); `?details=true` payload → postings: entries repeated once per location merge by shortcode with every location (`city, region, country`; `Remote` when telecommuting), and repeats disagreeing on title or description fail the board; `company` = the account's `name`

### `jobscan/gem.py`

Gem adapter: `api_board` (`/job_board/v0/<slug>`, any host) and `page_board` (jobs.gem.com) → `Board` (case kept); bare-list payload → postings; `content_plain` else `html_to_text(content)`; location = job location + each office's name and location, plus `Remote` for a remote job no name marks; a job with a description line reading exactly `***DEMO JOB POST***` is skipped, a slug containing `sandbox` is no board, and a sandbox board already listed reads as empty without a request; `company` = the board page's title, which must end ` Careers` (any other title, an error page's, names nobody)

### `jobscan/teamtailor.py`

Teamtailor adapter: no `api_board` (the feed is on the board's own host); `page_board` (`<slug>.teamtailor.com`, not `www`/`app`) → `Board` (slug lower case); `jobs.rss?per_page=1000` RSS → postings (the default stops at 100; a feed filling the page fails the board); location = `city, country` per `tt:location`, plus `Remote` when `remoteStatus` is `fully`; `company` = the channel title of a one-item feed

### `jobscan/personio.py`

Personio adapter: no `api_board`; `page_board` (`<slug>.jobs.personio.de` or `.com`, one tenant) → `Board` read from `.com/xml?language=en`, plus the default-language `/xml` when a named job has no English text (section values, not headings): such a job takes the default feed's sections for its id, and feeds listing different ids fail the board; `company` = the English board page's title after `Jobs at` (a bare `Jobs at`, or any other title, names nobody); location = office + additional offices; description = each section's heading and text; every job needs a unique id; a job with no name (some boards leave one empty in every language) is skipped, not the board failed

### `jobscan/smartrecruiters.py`

SmartRecruiters adapter: `api_board` (`/v1/companies/<slug>/postings`, any host) and `page_board` (jobs.smartrecruiters.com, `ni/` links too, and careers.smartrecruiters.com) → `Board` (slug lower case); `read` pages the list 100 at a time by offset, failing the board when pages disagree (offset, total, an id twice, count); location = `fullLocation` without empty parts, plus `Remote`; no descriptions in the list: `describe` reads the posting's job-ad sections with text (no `jobDescription` section, or no text at all, fails the posting); `company` = the company a one-posting page names. An unknown company answers an empty list, never a 404

### `jobscan/rippling.py`

Rippling adapter: `api_board` (`/api/v2/board/<slug>/jobs`, any host) and `page_board` (ats.rippling.com/<slug>/jobs, past a locale or `embed/` prefix) → `Board` (case kept: the API is case-sensitive); `read` pages 1,000 rows at a time, failing the board when totals move; rows repeated per location merge by id (identical rows occur), and rows of one job disagreeing on the title fail the board; location = each location's name, plus its country when the name doesn't say it, `Remote` for a remote job; `describe` reads the job's company and role text (no `role`, or no text at all, fails the posting); `company` = the board page's `__NEXT_DATA__` `jobBoard.companyName`

### `jobscan/workday.py`

Workday adapter: no `api_board` (the API is on each site's host, `SITE`, patched in run tests); `page_board` (`<tenant>.wd<N>.myworkdayjobs.com/[<locale>/]<site>`) → `Board` (slug `<tenant>.<dc>.<site>`, lower case: the site is case-insensitive; a bare two-letter segment is a locale only before a site); `read` POSTs the list (20 a page): the first page's filters, then one query per geographic or `remoteType` value passing `places`, each job carrying its matched value names (`N Locations` text dropped); a site without a geographic filter is read whole. Paging per query: a total of exactly 2,000 may be a cap (read to a short page, at least 2,000 rows), any other must equal the rows read; an id twice or an early empty page fails the board; placeholder rows (no title, no path) skipped; titles, location text and filter names through `payload.one_line`. posting key = the requisition (the longest `bulletFields` string the link's last segment ends with as `_<it>` or `_<it>-<digits>`), else `job/<segment>`; a site's links of one requisition merge; `same_job` = `workday:<tenant>:<requisition>` (fallback keys stay their own job); `describe` = the job detail's `jobDescription`, by the link in the posting URL; `company` = the tenant, without a request

### `jobscan/oracle.py`

Oracle Recruiting Cloud adapter: no `api_board` (the API is on each site's host, `SITE`, patched in run tests; list, detail, posting and page URLs composed from it at call time); `page_board` (`<pod>.fa.<region>.oraclecloud.com/hcmUI/CandidateExperience/<language>/sites/<site>`) → `Board` (slug `<pod>.<region>.<site>`, lower case: the site number is case-insensitive; languages fold; test and dev pods refused); `read` takes the whole list with only the filter's fields, 200 a page, stepping the offset by 200 (the count includes jobs the list never shows, so pages can come back short before the end): a count over 10,000 (the API's window), a moving count, a job twice (titled or not), more rows than the count, or a page before the last holding under 180 (`FULL`; none seen under 195) fail the board; untitled rows skipped (the detail may name them; 2 of 159 sites checked); titles and places through `one_line`; location = primary + secondary locations, `Remote` for `ORA_REMOTE`; `same_job` = `oracle:<pod>.<region>:<id>`; `describe` = the detail's five description sections, empty ones dropped; `company` = the site page's title (Oracle's default `Candidate Experience site`, compared after decoding, names nobody)

## Discovery

Finding boards in Common Crawl's index files.

### `jobscan/crawl.py`

Common Crawl index reader: newest crawl ids; a host's (or, for `*.domain`, every subdomain's) status-200 HTML captures by binary search over `cluster.idx` with range requests; a range answered short or not as asked is asked again on the 429/5xx schedule (`_request`'s `refuse`); `CrawlError` on any malformed response

### `jobscan/discover.py`

the `discover` pipeline on the caller's connection, under the caller's lock: three crawls × adapters → boards; replaces the discovered list only when every crawl read. `refresh_if_stale` is `run`'s call: when the list is 30 days old or an adapter with crawl hosts has no discovered boards; a crawl failure is logged and the old list kept. Every host of every adapter must have captures in every crawl

## The run

Filter, tag, select and deliver.

### `jobscan/scan.py`

`first_per_job(keys, delivered)` and `shown_company(board, looked_up, slug)`, the rules `preview` shares; the `run` pipeline: refreshes stale discovery (logging `prefetch` timings: DB open, discovery check), merges boards (file entry wins), counts 404s against discovered boards, keeps one matched posting per job (`_one_per_job`: by `fetch.same_job`, the first by key, none once the job was delivered under any key; the rest stay pending, logged `twins_skipped`), describes matched postings of `describe` adapters through their boards (unstored; the tag cache keys on the fetched text; a failure leaves the posting pending, not carried over), names each matched posting from its board this run (a boards-file name, else the stored or newly looked-up company, else the slug; a failed lookup logs `company_failed` and retries with the board's next match), tags (`_tag` → `Tagging`: postings the cache can't answer in `tag_queue` order, newest first seen first and one per board in turn on a tie, at most `max_tag_per_run` sent with `max_spend_per_run` as `tag_all`'s budget; the rest wait, pending, counted in `ScanOutcome.waiting` (exit 0, logged `tag_limited`), apart from those carried over (failed, or stopped by the breaker); each posting's results saved as it finishes, `tag_progress` every `PROGRESS_EVERY`), selects (`_select`: `tag.misses` splits tagged postings into entries and `digest.LeftOut`s), delivers only fully tagged postings of boards read or transiently failing this run, after the digest is published; left-out ones go with the digest that lists them, and with only left-out postings nothing publishes and they stay pending, cached

### `jobscan/prefilter.py`

word-adjacent, case-folded title and location terms; `verdict` → match, excluded (an excluded title hits a posting that otherwise matches) or other, `matches` its match; `in_places` (the location half) is also what `fetch` hands adapters as `places`: a location naming one of the profile's places, else, only when it says remote (`REMOTE`), naming a country the profile names by any of its names or a region holding one (`regions.containing`, matched longest first through `REGION`), or, with a remote place in the profile (a term of `UNPLACED` words only, "Remote"), naming no place at all (every word `UNPLACED` or a number); the profile's split cached on its locations

### `jobscan/regions.py`

data: `COUNTRIES` (Europe, the US and Canada, APAC and India, each with its other names; a name holding another country's, "New England", listed under its own), `REGIONS` (`Region` records: names as ads write them, `_part_of` giving every part of Europe and Asia as noun and adjective in each spelling, with their countries; a part holding none, Central Asia, kept so it's never read as Asia; the EU and EEA exact; tables frozen), `NAMES` (all, case-folded, longest first); `containing(places)` → every name of each country a place names exactly, and of every region holding it. Upward only

### `jobscan/tags.toml`

the built-in tag definitions (data), and the agency check (`check = true`: asked by jobscan, no tag to pick), opt-in tags (`opt_in = true`: `reports_to`, `visa_sponsorship`, `credential_required`; asked only when picked), each with `validated` (`"uk"`: only when `eligible_where` names the UK) and the one-line `summary` `init` shows; neither is part of the question or its hash

### `jobscan/tag.py`

`call` (the one chat-completions request every model call makes: no redirects, provider pinned, usage read before validation; `reasoning=False` for set-up suggestions, tagging keeps it; `CallFailed` with the HTTP status or exception class, a URLError's cause by class, and whether it may be billed when no reply came); `builtins()` (each built-in's name, summary, validated `yes`/`uk`/`no` and `requirable` answers (its values but unknown; free text its default) and `opt_in`, for `init`, `tags` and the page); `default_names()` (every built-in but the opt-in ones: `init` without `--tag`, typed `init`'s defaults, the page's Skip and `eval`); definitions (built-ins then the profile's custom tags; two tags asking one question are refused, since the cache is keyed by the question), `profile_definitions` (the profile's definitions with its `require` checked by `check_require`: a tag it tags, one printable value it can answer, not unknown; what run, schedule, check, preview and `init.validate_profile` call; the page's tags step calls `check_require` on its draft; then `agency_definition()` unless `include_agencies`; the name `agency` (`AGENCY`) refused in tags, custom tags and `require`), `requirements(require, include_agencies)` (the effective ones: the agency check's no appended; scan, preview and the page use it), the catalogue (`_builtins`) excludes checks, `misses` (the requirements a posting's results rule out: never unknown, an empty or unknown-only list, or a `TagResult.flagged` answer, the one ⚠ rule the digest shares; listed values exact, free text case-folded), prompt, strict schema, model call (no redirects), reply parsing, quote verification (case ignored), `tag_all` → `TagRun(attempts, stopped)` (WORKERS at a time, finished postings taken one at a time in the order they finish, the next started only once one is accounted for; none started after STOP_AFTER_FAILURES consecutive failures (`stopped` "failures") or once the attempts' known cost reaches `budget` ("budget"), those in flight finishing; `on_attempt` called in the caller's thread as each posting finishes; anything raised starts nothing more); outcomes are `TagSuccess | TagFailure` with `Usage`

### `jobscan/digest.py`

pure render (ⓘ on unvalidated tags with a one-line legend, keyed on entries only; `## Left out (N)` after the entries, one `one_line` row each with the failing answers and the URL, and ` · N left out` in the header, after ` · K waiting for later runs` when the run's limits held matches back; `quiet` tags (asked, not named by the profile: the agency check) never on entries or in the legend test; open-role count per entry; a `Company: <name> · <website>` line when the ATS gave a website); publish by hard link (never overwrites, `-2`/`-3` suffixes)

### `jobscan/mail.py`

`send_digest` (reads the published file back, subject with counts) and `send_test`; `send` over SMTP: TLS on 465, else STARTTLS before any login (refused when not offered); a refused QUIT after acceptance isn't a failure. Every failure is a `MailError` whose text is built from the SMTP code and decoded reply with the password and both addresses masked; other smtplib errors give their type only (their text can hold an escaped repr)

## Set-up

Writing an install, from a terminal, flags or the local page.

### `jobscan/init.py`

`validate_profile` (the profile check the questions, flags, page and `save_profile` share: custom tags may stand in for built-ins; each value by `check_values`, which the page's steps use too), `save_profile` (validated, read back in a scratch home, previous file kept as `.toml.prev`), `save_key`, `default_model` (`CONFIG` read by the loader, never written); `jobscan init`: terminal questions (`init`) or `Answers` (custom tags and `require` included; `.profile`) built from flags (`init_from`) → the same `_install`: refuse an existing install, `validate` every answer (the profile name before any path), load the install in a scratch home, write `config.toml`, the profile, `openrouter.key` (and `email.toml` from the questions); writes nothing partial. `email_setup` (asks; shared with `jobscan email`; `init` asks yes/no first) and `email_from` (flags) share `oauth_only`, `preset` (Gmail, iCloud), `app_password` (Gmail's spaces dropped), the scratch-home load and the test email; `save_email` replaces `email.toml` in one rename and names a temp file it couldn't remove; `remove_email` for `--off`

### `jobscan/boards.py`

the boards file `jobscan boards` edits: `path(config)`, `entries` (each with its board, or none when no adapter knows it), `add` (every URL checked before writing; listed boards skipped; an exclusive `fcntl` lock on `.<file>.lock` across read and replace; replaced in one rename)

### `jobscan/preview.py`

`for_home(config, profile)` (no database → None, nothing created; else under the run lock) and `preview(conn, profile, listed)` → `Preview`: open postings stored, matched, new jobs waiting to be tagged, as the run counts them (`scan.first_per_job`), with the profile's per-run limits beside them, already sent (by job), excluded, newest samples named by `scan.shown_company`; reads no descriptions (`store.open_brief`)

### `jobscan/titles.py`

`jobscan titles`: `for_home` (lock and database before the model call; `Outcome` with counts or none), `suggest` (one `tag.call` with reasoning off, schema `titles`: variants and near-misses through `sift`; `TitlesError` keeps a billed reply's usage), `clean` (printable one-line titles or places, at most 80 characters and 15, none twice or taken) and `sift` (also drops variants the titles already match and near-misses that would exclude its own titles; shared with `describe`) and `count` (over `store.open_brief` on `store.next_run_boards`, in the profile's places: a variant's added matches, exclusions still applying; a near-miss's current matches left out)

### `jobscan/describe.py`

the page's description: `read(words, model, key)` → `Described` (one `tag.call`, reasoning off, schema `description`, the built-in catalogue in the prompt): titles and other titles, titles to leave out and near-misses (`titles.clean`/`sift`), places and other names for them, where (one line, ≤ 200), built-in tags by name in catalogue order, up to 5 custom tags (name ≤ 40, not a built-in's, no name or question twice, a one-line question ≤ 500, 2–10 one-line answers ≤ 40, then `config.custom_tag`); a suggestion breaking a rule is dropped; `DescribeError` keeps a billed reply's usage

### `jobscan/page.py`

the set-up page as values, one step at a time (`STEPS`: intro, key, describe, titles, exclude, places, tags, limits, email, schedule, summary; an existing install's skip intro and describe; `done`, after Save, is in neither): `Form` (the draft; secrets read, never rendered; `require`: built-in tags' from the tags step's selects, the person's own tags' carried as the file has them; `include_agencies`, the tags step's tick; `max_tag` (one of `COUNTS`, 200/500/1000, or `other` with `max_tag_other`) and `max_spend`, the limits step's, as posted, read by `_limits` → `Limits` (each limit, or None with why under its field) through the profile's checks, a stored spend shown by `report.spend_text`), `Offers` (suggestions as tick boxes, with counts), `read_step(step, fields, form, offers)` (never raises; that step's fields only; a list keeps the draft's ticked values in order, repeats included, then ticked offers, then typed lines not present; browser CRLF stored as LF; custom rows after ticked offered tags, a blank name removes one; `require-<tag>` selects in catalogue order, then the draft's requirements on own tags; counts dropped once titles, exclusions or places change), `check_step` (the step's own fields by Save's rules: key usable, `init.check_values`, tags through `config.custom_tag` and `tag.definitions`, requirements through `tag.check_require` (an unticked tag's, an own tag's with a pointer to the profile file) and the length rule, email through `init.email_config` without sending), `to_profile(form)` → `Checked` (every profile step's checks, the size rule on the profile as reopened, then `init.validate_profile`), `unrepresentable(profile)` (the profile round-trips through the form, within the limits: 20,000 characters a field, 99 custom tags, half the 256 KB post), `actions(step)` (the buttons a page shows, the only actions the server takes; `EDITABLE`, the summary's Edit buttons; done's one button back to the summary), `schedule_errors` (the schedule step's check, which Save repeats), `Saved` (the done page's facts as display strings ui builds), `row` (a custom tag as an editable row; a ticked offered tag becomes one, and no offer shows for a name a row has), `with_suggestions`, `search` (a part-way draft for Suggest titles), `default_name`, `form_from_profile`, `render(View)` (pure HTML, reads nothing, every value escaped; per step, with step and revision hidden and a hidden default button first, so Enter moves on; the summary's Edit buttons and errors by step; the email step's app-password help and links out with `rel="noopener noreferrer"`; one question a screen in inline CSS only: a progress bar, tick tiles, one primary button (`_PRIMARY`), "press Enter" only on `_ENTER_MOVES_ON` steps, From/server/port and the custom-tag editor folded into `<details>` unless set or wrong; beside each built-in tag a select of its `requirable` answers, plus a file value it doesn't offer, selected), `WAITING`/`waiting`/`after_waiting` (a slow post's spinner, then the rest of its page), `blocked`

### `jobscan/ui.py`

`jobscan ui`: `Server` (HTTPServer on 127.0.0.1, one request at a time, 10-second read timeout per connection, 256 KB posts; a post `Site.waiting` names streams the spinner first, unlengthed, closed by HTTP/1.0), `_Handler` (Host must be 127.0.0.1 or localhost with the port; GET needs the token in the query, POST in the body; a foreign or null Origin refused; CSP, nosniff, `strict-origin`, no-store; silent logs: the request line holds the token) and `Site`, the one owner of state (the token, the `Drafts` of typed secrets, and the draft, offers, step and revision until Save; a post whose step or revision isn't current is shown the current step unapplied, holding nothing; `launchd` injected, required: tests never reach the real one): next (`page.check_step`, then the next step, or the summary on an existing install), back, `goto:` (the summary's Edit), skip, describe (`describe.read` once per different description; `_prefilled`: asked-for ticked, other titles, near-misses, other place names and custom tags offered unticked), Save (the schedule step re-checked first; fresh: `init.init_from` (every profile field, the limits included); existing: `init.save_profile`, `save_key`, then email via `init.email_config`/`email_from`/`save_email`, or `remove_email`, then `install.check`, then the daily run: `schedule.install` only when every earlier section held and not `in_place`, `remove` when unticked whatever else failed; each section reported; all held → the done page, from `_what_now`: home-aware shell-quoted commands, the digest folder, the cron line off macOS), Preview (`preview.for_home`), Suggest titles (`titles.for_home` on `page.search`; a fresh home's model is `init.default_model()` with the typed key); an install it can't edit unchanged gets `page.blocked`

### `jobscan/schedule.py`

launchd job per install (label from the resolved home's hash): plist, bootout/bootstrap with rollback, removal; the cron line elsewhere; `Launchd` (agents dir + launchctl; `launchd()` this Mac's, None elsewhere: `schedule` and `ui` share it), `parse_at` (the one HH:MM rule, `arguments.clock` wraps it), `scheduled_at` (the plist's daily time, None when absent or malformed), `loaded` (`launchctl print`: launchd's own answer), `in_place` (the plist byte for byte as `install` would write it, and loaded), `state` → `State` (for `check`: the plist's time if it runs `jobscan run --notify` daily, loaded, a Python that no longer exists)

## Evaluation

Scoring the tags against a labelled set.

### `jobscan/evaluate.py`

`eval`: tags a labelled set through `tag.tag_all` (no budget) with a profile's default definitions (`tag.default_names()`, the five built-ins the labelled test covered, and the agency check, via `tag.profile_definitions`), scores the three tags with a pass bar (`founding`, `location_eligible`, `language_required`), saves predictions under `$JOBSCAN_HOME/eval/`

## Tests

`fakes.py` (local fake Ashby and model servers; `FakeFiles` + `crawl_files` for Common Crawl index files with HEAD/206 (`cut`: a range closed early); `FakeServer.encode` for gzip and other encodings, `keep_alive` (HTTP/1.1) with `drop_after`/`hang_up` and `client_ports`/`closed` to see which requests shared a connection; `ashby_job`/`greenhouse_job`/`workable_job`/`gem_job` payload builders, `teamtailor_feed`/`teamtailor_item` and `personio_feed`/`personio_position` XML builders; `smartrecruiters_job`/`_page`/`_detail` and `rippling_item`/`_page`/`_detail` paged-list and detail builders; `workday_job`, `workday_site` (a POST list endpoint applying filters as Workday does) and `workday_detail`; `oracle_job`, `oracle_page` (one offset's page, 200 rows) and `oracle_detail`; `anywhere`, a `places` test passing everything; `fake_smtp`/`patch_smtp`, recording fakes with each smtplib constructor's real signature; `PlainSMTPServer`, a real SMTP peer without STARTTLS) · unit tests per module (`test_company.py`: every adapter's `company` and the name clean-up; `test_regions.py`: the table's own rules; `test_connections.py`: reuse, drops, cuts, redirects and close at `send`; `test_prefilter.py` `RemoteTest`: the Remote rule case by case) · `test_run.py` and `test_discover.py` end to end through the CLI; `RunFixture` keeps a fresh dead sentinel board, points every `BOARD_PAGE` and Workday's and Oracle's `SITE` at the fake (404 until routed) and fails any unexpected crawl · `test_ui.py` (the real server on port 0, driven as a browser: each page's own fields read back from its HTML and posted with the button pressed; token, Host, Origin, size, idle socket, stale revisions; a new install from one sentence through every step to the done page; an existing install through Edit → Done on every step saved unchanged; the daily run through a stateful fake launchd: unchanged, a new time, unticked, refused, half-removed, after a failed save, a moved interpreter; off macOS the cron line and quoted commands; no page ever carries a typed secret) · `test_describe.py` (the call, every cleaning rule, the result through the page unchanged) · `test_page.py` (read_step, check_step, to_profile, render) · `test_profile_write.py` (writer and reader agree; validation; `.toml.prev`) · `test_titles.py` · `test_setup_commands.py` (`tags`, `check`, `boards`, `preview`, `init` and `email` by flags, through the CLI with no terminal) · `test_arguments.py` (examples use every flag; set-up order; no flag takes a secret) · `test_run.py` `AgentSetupTest` (flags alone → run → preview) · `test_init.py` (scripted answers), `test_schedule.py` (fake launchctl) · `smoke.sh` live, needs the key, reads exactly its three boards
