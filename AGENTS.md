# boss-zhipin-scraper agent guide

## Scope

This project is for low-frequency, personal job-search research on BOSS直聘. Keep requests serial and small; do not add proxy rotation, fingerprint spoofing, CAPTCHA bypass, or bulk crawling behavior.

## Current request flow

- List search navigates to the target BOSS search page and captures the page's native `joblist.json` response through CDP Network events.
- Do not add a fixed preflight search or inject a second synchronous XHR into the page. Those extra requests can trigger BOSS `code 37` on Windows.
- Detail pages are opened serially in isolated CDP targets, rendered, scrolled lightly, and extracted from the page. Do not inject visibility overrides or synthetic mouse events.
- Timing is adaptive and bounded: search-page gaps 8–15s, detail initial render 5–8s, at most two 0.8–1.5s scroll retries when the JD section is missing, and 8–15s between detail jobs; never wait after the final job.
- Direct `--job_link` detail extraction should collect visible job-limit/tag text and exact internship constraints such as `4天/周` or `持续3个月` into `tags_list`; do not infer absent fields from prose.
- `--check` is local-only: dependencies and CDP connectivity. It must not make a BOSS search request.
- `--smoke-test` is an explicit one-request native-page test and should not be run repeatedly.
- `--setup-chrome` starts the dedicated browser only; it must not run an automatic login probe. The actual target search is the login/API check.

## Windows workspace entry point

Use `boss.ps1`, not the Python script directly. It temporarily clears proxy environment variables for the Python process and restores them afterward. Clash itself remains enabled; the active Clash profile must route `zhipin.com` and `zhipin.net` to `DIRECT` while other traffic keeps its normal proxy route.

The wrapper must preserve the caller's PowerShell working directory. The dedicated Chrome profile is separate from the user's main Chrome profile.

## Safe operating limits

- Search: normally 1–3 pages per run.
- Detail: normally no more than 3 jobs per run, serially.
- On BOSS `code 31` or `code 37`, stop immediately. Do not retry probes or rotate proxies. Complete any visible browser verification manually and wait before trying again.
- Do not open DevTools/F12 during a BOSS run.

## Output behavior

- `--stdout` emits one final JSON document, suitable for a pipeline.
- `--stream-json` is detail-only and emits one completed detail as NDJSON immediately after it is saved. It is useful for long runs and command wait limits.
- File writes are incremental and atomic. A partial JSON file must never replace a valid previous result.
- Direct `--job_link` detail mode may fill missing list metadata from the rendered detail page; fields that are not present remain empty rather than being inferred.

## Skill package maintenance

This repo has two roles: the repo root is the runnable project, and `skills/boss-tool/` is a self-contained, installable Codex skill package (Chinese command manual + `boss.ps1` + core scripts + city codes).

- The authoritative files live in the repo root (`boss.ps1`, `scripts/boss_cdp_raw.py`, `scripts/job_summary.py`, `data/city_codes.json`, `requirements.txt`, `LICENSE`). Never edit the copies under `skills/boss-tool/` directly.
- After any code or CLI change, refresh the package with `.\scripts\sync-skill.ps1` before committing. Add `-SyncLocalSkill` to also mirror `skills/boss-tool/SKILL.md` into the author's local `~/.codex/skills/boss-tool/` (Codex reloads skills on restart).
- `skills/boss-tool/SKILL.md` is the canonical Chinese command manual and must stay identical to the local personal skill copies (`SKILL.md` / `SKILL.md.new`).
- Others install the skill from GitHub with the skill installer: `--repo francis3253161180-maker/boss-zhipin-scraper --ref master --path skills/boss-tool`. The installed skill is self-contained and needs no separate repo clone.
- Keep the local repo as the maintenance and runtime workspace. Local-only branches (e.g. `codex`, `agent/*`) must be pushed to `origin` before any local cleanup; they are the recovery baseline. Dedicated Chrome login/profile live outside the repo (`~/.boss-zhipin-scraper/chrome-profile`), so deleting or re-cloning the repo does not affect login state.

## Change and test rules

- Keep the core logic in `scripts/boss_cdp_raw.py` unless a change clearly requires a new module.
- Update `README.md`, `README.en.md`, `SKILL.md`, and `CHANGELOG.md` when CLI behavior changes, then run `scripts/sync-skill.ps1` to refresh `skills/boss-tool/`.
- Add mock tests for CDP event handling, native response parsing, direct-link metadata fallback, stream output, and atomic writes.
- Run `python -m py_compile scripts/boss_cdp_raw.py` and the relevant unittest modules before real BOSS validation.
- Keep version declarations synchronized across `scripts/boss_cdp_raw.py`, `pyproject.toml`, `SKILL.md`, and README files.
