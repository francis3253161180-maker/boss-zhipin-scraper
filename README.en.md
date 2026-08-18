# BOSS Zhipin Scraper · Job Crawler v2.11 (Chrome CDP / Plaintext Salary)

> 🌐 中文文档：[README.md](./README.md)

![Python](https://img.shields.io/badge/python-3.10+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)
![Version](https://img.shields.io/badge/version-2.11.0-orange.svg)

> ⭐ **Personal enhanced fork**: this repository is a personally maintained fork of [eatmoreduck/boss-zhipin-scraper](https://github.com/eatmoreduck/boss-zhipin-scraper), adding **batch delivery (`--mode send`), conversation reading (`--mode read`), and a self-contained Codex skill package (`skills/boss-tool/`)** installable into local Codex.

A low-frequency personal job-search tool for [zhipin.com](https://www.zhipin.com). It connects to an isolated already-logged-in Chrome via CDP, navigates to the target search page, and captures the page's native `joblist.json` response for plaintext salary data. Detail pages are fetched serially and can emit streaming NDJSON.

> 📌 **In one sentence**: no Selenium/Playwright — connect to your logged-in Chrome over CDP, hit the search API with the real session, get JSON/CSV with plaintext salaries, plus salary-distribution, skill-frequency stats and a résumé-optimization prompt.

---

## ⚠️ Disclaimer

This project is for **learning and technical research purposes only**. It is intended to explore Chrome DevTools Protocol, front-end anti-scraping mechanisms, and data-collection techniques. Do **not** use it for any purpose that violates the [BOSS Zhipin Terms of Service](https://www.zhipin.com/about/protocol.html) or applicable laws and regulations, including commercial resale, malicious scraping, or any activity that imposes undue load on the target site. Users are solely responsible for the consequences of using this project; the author is not liable for any misuse.

---

## 🚀 30-Second Quick Start

```bash
# 1. Clone + install deps
git clone https://github.com/eatmoreduck/boss-zhipin-scraper.git
cd boss-zhipin-scraper
pip install -r requirements.txt          # or: uv sync

# 2. Launch an isolated Chrome and log in (only once; session persists)
python3 scripts/boss_cdp_raw.py --setup-chrome

# 3. Search the job list
python3 scripts/boss_cdp_raw.py --mode search --keyword "AI Agent" --city 上海 --pages 3 --stdout

# Optional: capture personalized homepage recommendations and latest jobs
python3 scripts/boss_cdp_raw.py --mode homepage --homepage-url "https://www.zhipin.com/chengdu/?ka=header-home" --stdout

# Optional: discover inbox JSON/WebSocket envelope schemas without any chat content
python3 scripts/boss_cdp_raw.py --mode inbox-discover --stdout

# Explicitly read the currently open dedicated-Chrome conversation (no navigation, scroll, or send)
python3 scripts/boss_cdp_raw.py --mode inbox-read-active --expect-contact "Liu Shan" --stdout

# Send one explicitly confirmed text to the already open conversation
python3 scripts/boss_cdp_raw.py --mode inbox-send-active --expect-contact "Mr Yang" --message "Hello" --confirm-send --stdout

# Batch delivery: open the JD, click 立即沟通/继续沟通, auto-send --content (auto read-back verification, returns send_success)
python3 scripts/boss_cdp_raw.py --mode send --content "Hi, I am very interested in this role..." --job_link "https://www.zhipin.com/job_detail/xxx.html?lid=..&securityId=.." --stdout
# Read the JD's conversation history (sender direction, read-only, --stdout required)
python3 scripts/boss_cdp_raw.py --mode read --job_link "https://www.zhipin.com/job_detail/xxx.html?lid=..&securityId=.." --stdout
# List all sidebar conversations (name/avatar/company/job_link/read-or-delivered status, last-message sender and read state, unread count, and more)
python3 scripts/boss_cdp_raw.py --mode read --list --stdout
# Read the currently selected conversation on the already open chat page (no switching/reopening)
python3 scripts/boss_cdp_raw.py --mode read --chat --stdout
# Prefer switching the sidebar row on the open chat page; fall back to opening the job_link
python3 scripts/boss_cdp_raw.py --mode read --chat --job_link "https://www.zhipin.com/job_detail/xxx.html?lid=..&securityId=.." --stdout
# Switch conversations by directly clicking sidebar indices on the open chat page (no job_link reopen)
python3 scripts/boss_cdp_raw.py --mode read --chat --switch-index 0,1 --stdout

# Cities nationwide are supported (incl. tier-3/4/5), e.g.:
python3 scripts/boss_cdp_raw.py --keyword "前端" --city 赣州 --pages 3
# List supported cities: --list-cities [keyword]
python3 scripts/boss_cdp_raw.py --list-cities 江

# 4. Generate an aggregated summary + prompt after scraping (reads the latest result)
python3 scripts/job_summary.py
```

Right after scraping you get: salary ranges, experience requirements, top skill keywords, and a job-application optimization prompt. The prompt is based solely on the scraped job data — it never reads your local résumé file and never scores personal-job match.

## Install as a Codex Skill (optional)

The `skills/boss-tool/` folder is a self-contained Codex skill package (Chinese command manual + `boss.ps1` + core scripts + city codes). Install it into local Codex with the skill installer:

```powershell
# Ask Codex to run the skill-installer, or run directly (~ expands to your home dir; do not quote it):
python ~\.codex\skills\.system\skill-installer\scripts\install-skill-from-github.py `
  --repo francis3253161180-maker/boss-zhipin-scraper --ref master --path skills/boss-tool
```

After install the skill lives at `~/.codex/skills/boss-tool/` and takes effect after restarting Codex; it bundles `boss.ps1`, `scripts/`, and `data/`, so no separate repo clone is needed. Maintainers run `scripts/sync-skill.ps1` to keep the package in sync with the repo root code.

## ✨ Features

- Plaintext salary (API mode, bypasses font-based obfuscation)
- Native homepage response capture for personalized and latest jobs (`--mode homepage`)
- Batch delivery (`--mode send`): opens each `--job_link` JD, clicks `立即沟通`/`继续沟通` so BOSS opens and switches to the conversation, then sends the exact `--content` with one Enter; after sending it keeps the chat page open, reads back the last history row, and returns `send_success`/`verified_last_sender`/`verified_last_text`; serial and low-frequency, one send per job, no automatic retry, and immediate batch abort on risk control
- Conversation read (`--mode read`) variants: `--list` merges the native conversation list with the rendered sidebar to return each conversation's name/avatar/company/job_link/read-or-delivered status, last message (sender `self`/`other`, read state 已读/送达/未读, text, time), unread count, pinned/selected flags, and sidebar `index`; `--chat` reads the currently selected conversation on the open chat page; `--chat --job_link` prefers switching the sidebar row directly on the already open chat page and falls back to opening the JD when switching fails or no chat page is open (`entered_via: sidebar|job_link`, summary counts `via_sidebar`/`via_job_link`); `--chat --switch-index N` switches directly by clicking sidebar indices with trusted mouse events (no job_link reopen). Reads only the currently rendered chat history with per-message `sender` (`self`/`other`/`system`/`platform`/`attachment`/`unknown`); no older-history scrolling, never sends, stdout-only so chat bodies are not written to disk
- Boss activity status as a separate field (`boss_active_status`): list maps `bossOnline`→"在线"; detail can provide finer labels like "刚刚活跃"
- Dual JSON / CSV output
- Detail-page JD scraping + skill analysis
- Aggregated summary + copy-paste prompt after scraping
- Incremental writes (no data loss on crash)
- One-shot environment check + persistent isolated Chrome CDP profile
- Multi-dimension filters (scale, funding, salary, experience, degree, industry)
- Windows, macOS, and Linux (use `boss.ps1` in the Windows workspace)

### Request path and output modes

- List search captures the page's native `joblist.json` network response. It does not inject a second synchronous XHR or send a fixed preflight login probe.
- Homepage mode captures the page's own `recommend/job/list.json` responses: `sortType=1` is `selected`, while `sortType=2` is `latest`.
- `--check` is local-only: dependencies and CDP connectivity. The real target search is the source of truth for login and API availability.
- `--stdout` emits one final JSON document; detail-only `--stream-json` emits one NDJSON object after each completed job.
- Direct `--job_link` detail mode fills visible title, company, salary, location, tags, and company link fields from the rendered page when available.
- Send mode (`--mode send`) opens each JD page in the dedicated Chrome, clicks `立即沟通`/`继续沟通` (BOSS itself opens and switches to the conversation), verifies `--content` is in the composer, then sends it with a trusted Enter sequence (rawKeyDown + char + keyUp). After sending it keeps the chat page open and reads back the last history row: `send_success=true` when it matches `--content`, plus `verified_last_sender` (self/other) and `verified_last_text` (raw read-back, truncated to 200 chars); the batch summary also reports `sent_verified`. Existing fields `post_send_visible` (outgoing count confirmed by a bounded ≤6s poll) and `composer_cleared_after_send` remain; a failed confirmation is reported, never auto-resent. A 5-second pre-flight countdown with Ctrl+C precedes the batch, 8-15s between jobs, and any detected risk-control marker stops the rest of the batch.
- Read mode (`--mode read`) has three forms: `--list` returns every sidebar conversation with full fields (name/avatar/company/job_link/read-or-delivered status, `last_message_sender`=`self`/`other`, `last_message_read`=已读/送达/未读, `last_message_text`/`last_message_time`, unread count, pinned/selected flags, and sidebar `index` for `--chat --switch-index`); `--chat` reads only the currently selected conversation on the already open chat page; `--chat --job_link` first switches the sidebar row on the open chat page with trusted mouse events and only falls back to opening the JD when switching fails or no chat page is open (`entered_via` marks the actual entry, summary reports `via_sidebar`/`via_job_link`); `--chat --switch-index 0,1` clicks sidebar indices directly with trusted mouse events on the open chat page (no job_link reopen). Every row is tagged with `sender` (`self`/`other`/`system`/`platform`/`attachment`/`unknown`); it requires `--stdout`, never writes chat bodies to disk, never sends, never auto-scrolls older history, and aborts the rest of the batch on any risk-control marker.
- Incremental JSON writes use atomic replacement so an interrupted run does not leave a truncated result.
- Timing is adaptive: 8–15 seconds between search pages, 5–8 seconds for initial detail rendering, at most two short scroll retries when the JD section is missing, and 8–15 seconds between detail jobs with no final-job delay.

<details>
<summary>🔍 Why not a Selenium / Playwright crawler?</summary>

- Selenium/Playwright spins up a full instrumented browser — it's heavy, has an obvious fingerprint, and is easily flagged by BOSS Zhipin's risk-control / CAPTCHA.
- This tool connects to your own already-logged-in Chrome (via CDP), reusing a real fingerprint and session, and calls the same legitimate search API the page uses. The `salaryDesc` it returns is already plaintext — no need to parse font-obfuscated DOM salaries.
- This reduces duplicate requests and page injection, which is generally more stable than scraping by issuing extra XHRs.

</details>

## Installation

### Option 1: Clone then install locally (recommended)

Because `hermes skills install` may not reach GitHub directly in some environments, clone the repo first and install locally:

```bash
# 1. Clone the repo
git clone https://github.com/eatmoreduck/boss-zhipin-scraper.git
cd boss-zhipin-scraper

# 2. Copy into the Hermes skills directory
mkdir -p ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts
cp SKILL.md ~/.hermes/skills/data-science/boss-zhipin-scraper/
cp scripts/boss_cdp_raw.py ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts/
cp scripts/job_summary.py ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts/
mkdir -p ~/.hermes/skills/data-science/boss-zhipin-scraper/data
cp data/city_codes.json ~/.hermes/skills/data-science/boss-zhipin-scraper/data/
```

### Option 2: One-line curl install

No need to clone the whole repo — download just the files you need:

```bash
mkdir -p ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts && \
curl -sL https://raw.githubusercontent.com/eatmoreduck/boss-zhipin-scraper/master/SKILL.md \
  -o ~/.hermes/skills/data-science/boss-zhipin-scraper/SKILL.md && \
curl -sL https://raw.githubusercontent.com/eatmoreduck/boss-zhipin-scraper/master/scripts/boss_cdp_raw.py \
  -o ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts/boss_cdp_raw.py && \
curl -sL https://raw.githubusercontent.com/eatmoreduck/boss-zhipin-scraper/master/scripts/job_summary.py \
  -o ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts/job_summary.py && \
mkdir -p ~/.hermes/skills/data-science/boss-zhipin-scraper/data && \
curl -sL https://raw.githubusercontent.com/eatmoreduck/boss-zhipin-scraper/master/data/city_codes.json \
  -o ~/.hermes/skills/data-science/boss-zhipin-scraper/data/city_codes.json
```

### Option 3: `hermes skills install` (requires direct GitHub access)

```bash
hermes skills install https://raw.githubusercontent.com/eatmoreduck/boss-zhipin-scraper/master/SKILL.md --category data-science
```

> Note: this depends on the hermes process being able to reach GitHub directly. If you hit a timeout or connection failure, use Option 1 or 2.

### Verify the installation

```bash
# Check that the files exist
ls ~/.hermes/skills/data-science/boss-zhipin-scraper/SKILL.md
ls ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts/boss_cdp_raw.py
ls ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts/job_summary.py
ls ~/.hermes/skills/data-science/boss-zhipin-scraper/data/city_codes.json
```

After installing, just say in a Hermes conversation: "Search BOSS Zhipin for AI Agent jobs in Shanghai."

## Use as a CLI tool

You don't have to install it as a Skill — use it as a plain CLI:

```bash
# 1. Clone + install deps
git clone https://github.com/eatmoreduck/boss-zhipin-scraper.git
cd boss-zhipin-scraper
pip install -r requirements.txt

# 2. Start Chrome CDP
python3 scripts/boss_cdp_raw.py --setup-chrome
# First run won't copy your main Chrome session; log in to zhipin.com in the dedicated BOSS browser that pops up
# setup only starts the dedicated Chrome; log in manually, then run the target search

# 3. Check the environment
python3 scripts/boss_cdp_raw.py --check

# Optional: real browser/API smoke test (writes no result files)
python3 scripts/boss_cdp_raw.py --smoke-test

# 4. Scrape
python3 scripts/boss_cdp_raw.py --mode search --keyword "AI Agent" --city 上海 --pages 3 --format csv

# 5. Summary + prompt after scraping
python3 scripts/job_summary.py --top 15
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `--keyword` | Search keyword (default "AI Agent") |
| `--city` | City (Chinese name or 9-digit code, default Shanghai). **Supports cities nationwide** (300+, incl. tier-3/4/5); city codes auto-sync from BOSS at runtime. See [`data/city_codes.json`](data/city_codes.json), or run `--list-cities`. An unrecognized city name now exits with an error instead of silently producing zero results |
| `--list-cities [keyword]` | Print the supported city list, optional keyword filter, e.g. `--list-cities 江` |
| `--pages` | Number of pages (max 10) |
| `--format` | json / csv; csv also exports list and detail CSVs |
| `--mode search/detail/homepage/send/read` | Search lists, fetch selected details, capture homepage jobs, batch-deliver, or read conversations |
| `--content` | Exact text to send in `send` mode (required, max 500 chars) |
| `--list` | In `read` mode: list all sidebar conversations (name/company/job_link/read-or-delivered status/time) |
| `--chat` | In `read` mode: read chat; alone = current selection, with `--job_link` = enter that conversation, with `--switch-index` = click sidebar indices |
| `--switch-index` | In `read --chat` mode: sidebar conversation index (0-based, comma-separated); switch by direct click without reopening a job_link |
| `--expect-contact` | Contact name verification for `inbox-read-active` / `read --chat` |
| `--max-chat-items` | Max rendered message rows output by `inbox-read-active` / `read` (1–200) |
| `--homepage-url` | Target homepage URL for `homepage` mode |
| `--inbox-url` | Target chat-page URL for `inbox-discover` / `read --list` mode |
| `--capture-seconds` | Native homepage/inbox-discover response window, 5–30 seconds |
| `--job_link` | Fetch details directly from complete links (the only job-selection parameter, includes `lid`/`securityId`) |
| `--stdout` | Emit one final JSON document |
| `--stream-json` | Detail-only NDJSON, one completed job per line |
| `--allow-dom-fallback` | Allow DOM extraction fallback when the API has no data; off by default, salaries may be unreliable |
| `--check` | Local environment check (CDP + deps; no BOSS request) |
| `--smoke-test` | Run one real Chrome/CDP BOSS search API smoke test, writes no result files |
| `--setup-chrome` | One-shot launch of Chrome CDP (persistent isolated profile) |
| `--copy-login-state` | Manually import the main Chrome's Local State + cookie-related files into the isolated profile (never copied by default, on first run, or on repeated runs) |
| `--reset-chrome-profile` | Rebuild the dedicated BOSS Chrome profile; clears the login state inside this dedicated browser |
| `--stop-chrome` | Close the dedicated BOSS CDP Chrome (matched precisely by the isolated profile; never touches your main Chrome) |
| `--close-chrome` | Auto-close the dedicated Chrome after a scrape finishes normally (off by default; not triggered on errors, so the login state is kept) |
| `--output` | List output path (default `~/.boss-zhipin-scraper/job-result/`) |
| `--detail-output` | Detail output path (default `~/.boss-zhipin-scraper/job-result/`) |
| `--cdp-port` | CDP port (default 9222) |
| `--scale/--salary/--experience/--degree` | Filters |

## Post-Scrape Summary & Prompt

`scripts/job_summary.py` only reads the already-scraped `boss_jobs_*.json` and `boss_details_*.json`, does simple aggregation, and produces a copy-paste prompt. It never reads your local résumé file, pulls in no PDF dependency, and never scores a person against a job.

```bash
# Read the newest boss_jobs_*.json under the default result dir and auto-match the same-timestamp or newest detail file
python3 scripts/job_summary.py

# Specify list and detail files
python3 scripts/job_summary.py \
  --input ~/.boss-zhipin-scraper/job-result/boss_jobs_20260625_1200.json \
  --details ~/.boss-zhipin-scraper/job-result/boss_details_20260625_1200.json \
  --top 15

# Only emit the prompt
python3 scripts/job_summary.py --prompt-only
```

After installing the package you can also use the entry command:

```bash
uv run boss-summary --top 15
```

The summary covers: salary ranges, experience requirements, degree requirements, regional distribution, top companies, skill tags, frequent JD terms. The prompt asks the model to use these stats to fill in résumé keywords, suggest project-story rewrite directions, and produce an interview-prep checklist — while explicitly instructing it not to fabricate experience.

## File Structure

```
boss-zhipin-scraper/
├── SKILL.md              # Hermes Skill definition
├── README.md             # Chinese docs
├── README.en.md          # English docs
├── CHANGELOG.md
├── LICENSE
├── pyproject.toml
├── data/
│   └── city_codes.json   # Full city-code map
├── scripts/
│   ├── boss_cdp_raw.py   # Main scraping script
│   └── job_summary.py    # Post-scrape summary + prompt
└── requirements.txt
```

## How It Works

This is a Chrome-CDP-based BOSS Zhipin crawler. Core flow:

1. Connect to an already-open Chrome via the Chrome DevTools Protocol (CDP)
2. Navigate to the target search page or homepage and capture the page's native job JSON responses with CDP Network events
3. The native response returns plaintext `salaryDesc` and preserves `securityId` / `lid` context
4. Open detail pages serially and extract JD plus visible direct-link metadata
5. Write each completed result atomically and dedupe by `job_id`

DOM extraction is not used for the list by default, since DOM salaries may be hit by font-based obfuscation. Only when `--allow-dom-fallback` is explicitly passed will it fall back to DOM when the API returns no data.

For detail pages, the scraper only extracts a section containing the job-description heading. Full-page `body` text is diagnostic input for detecting login walls and navigation shells and is never written directly as a JD. If the page contains the login-to-view-full-content marker, the crawl fails explicitly and stops before truncated text, recruiter metadata, company sections, or recommended jobs can be saved as a complete JD.

List-to-detail runs use a PowerShell pipeline (fetches every job in the piped list) or a complete `--job_link` containing `lid`/`securityId` to select individual jobs. `--job_link` is now the only job-selection parameter, replacing `--job_id`.

## Chrome Profile Security Policy

`--setup-chrome` uses a persistent isolated profile by default — it neither symlinks nor copies your main Chrome data. First launch and subsequent launches only create or reuse this dedicated profile:

- `~/.boss-zhipin-scraper/chrome-profile`

Without an explicit `--output` or `--detail-output`, scraping results are saved under:

- `~/.boss-zhipin-scraper/job-result`

On first use you must log in to BOSS Zhipin manually inside this dedicated Chrome. Run `--setup-chrome`, log in, then run the target search; the program never sends a fixed login-probe request, and the session is stored inside the dedicated profile.

`--check` does not send a BOSS search request. The native response from the real target search is the source of truth for login, risk control, and data availability. On `code: 31` or `code: 37`, stop and do not repeat probes or retries.

The interactive login page opened by `--setup-chrome` is the only temporary page intentionally brought to the foreground. Temporary tabs used by environment checks, list/detail scraping, and the smoke test run in the background so automation does not repeatedly steal focus. “Background” here only means the tab is not activated; the dedicated Chrome still runs with a visible UI and can be opened manually for inspection.

If you really need to import the BOSS session from your main Chrome, run explicitly:

```bash
python3 scripts/boss_cdp_raw.py --setup-chrome --copy-login-state
```

`--copy-login-state` overwrites the corresponding cookie-related files inside the isolated profile on every run; do not pass this for daily launches. It only copies `Local State` and `Default/Cookies*`, `Default/Network/Cookies*`-style cookie database files — not password stores, history, extensions, or a full profile. To wipe the dedicated browser's login state:

```bash
python3 scripts/boss_cdp_raw.py --setup-chrome --reset-chrome-profile
```

### Tearing down when you're done

After a scrape/analysis finishes, the dedicated Chrome is **not** closed automatically (the login state is kept by default so you can run the next scrape right away). When you're sure you no longer need it, tear it down manually:

```bash
python3 scripts/boss_cdp_raw.py --stop-chrome
```

`--stop-chrome` only closes the Chrome process(es) that belong to the scraper's isolated profile (`--user-data-dir`). It **never** kills by port or process name, so it cannot accidentally take down your main Chrome, Gmail, GitHub, or other signed-in sessions.

If you'd rather have a particular scrape close the dedicated Chrome once it finishes normally, add `--close-chrome`:

```bash
python3 scripts/boss_cdp_raw.py --keyword "AI Agent" --city 上海 --pages 3 --close-chrome
```

`--close-chrome` is off by default, and it only fires on the **success path** of a completed scrape — login failures, crashes, and other early exits leave the Chrome running so the login state is preserved.

## 📌 TODO

- [ ] Strengthen the detail-page `Referer` and request fingerprinting to further reduce risk-control triggers

## License

MIT

## Friends

- [LINUX DO](https://linux.do/) — A sincere, friendly, and vibrant tech community. This project endorses and recommends it.

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=eatmoreduck/boss-zhipin-scraper&type=Date)](https://star-history.com/#eatmoreduck/boss-zhipin-scraper&Date)
