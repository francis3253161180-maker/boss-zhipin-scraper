---
name: boss-zhipin-scraper
description: "Low-frequency personal job-search workflow for BOSS直聘 via an already logged-in Chrome CDP profile. Searches job lists, captures native page responses, fetches selected JD details, and outputs JSON/CSV."
version: 2.9.0
author: eatmoreduck
license: MIT
platforms: [windows, macos, linux]
---

# BOSS直聘职位抓取工具 v2.9

Use this skill only for personal job-search research. Keep list searches small and detail requests serial. Do not add proxy rotation, fingerprint spoofing, CAPTCHA bypass, or bulk crawling.

## Windows entry point

In this workspace always run `boss.ps1`, not the Python script directly:

```powershell
.\boss-zhipin-scraper\boss.ps1 --mode search --keyword "agent开发实习" --city 北京 --pages 1 --stdout
```

The wrapper temporarily clears proxy environment variables for the Python child and restores them afterward. Clash remains enabled for Codex and other websites. In the active Clash profile, add these as front rules:

```text
DOMAIN-SUFFIX,zhipin.com  DIRECT
DOMAIN-SUFFIX,zhipin.net  DIRECT
```

Do not force the whole BOSS Chrome to `direct://`; the same browser may need Clash for YouTube and other sites.

## Workflow

1. If the dedicated CDP Chrome is not running, start it with `--setup-chrome`.
2. Ask the user to log in manually in that dedicated browser if needed.
3. Run the actual target search. Do not run a fixed `Java/Shanghai` probe first.
4. The list path navigates to the target search page and captures its native `joblist.json` response using CDP Network events. It must not inject a second synchronous XHR.
5. The homepage path captures the page's native recommendation responses. Treat `sortType=1` as `selected` and `sortType=2` as `latest`; do not control the user's main browser.
6. Select at most 3 jobs, then fetch details serially.
7. Use `--detail-output` for long detail runs. Use `--stream-json` when each completed detail should be available immediately.

Timing policy: wait 8–15 seconds between search pages; wait 5–8 seconds after opening a detail page; only if the first extraction lacks a `职位描述` section, perform at most two short 0.8–1.5 second scroll retries; wait 8–15 seconds between detail jobs, with no extra wait after the final job.

`--check` is local-only: it checks Python dependencies and CDP connectivity and does not contact BOSS. `--smoke-test` is an explicit native-page API test; use it sparingly because it still makes one search request.

## Commands

```powershell
.\boss-zhipin-scraper\boss.ps1 --setup-chrome
.\boss-zhipin-scraper\boss.ps1 --stop-chrome
.\boss-zhipin-scraper\boss.ps1 --check
.\boss-zhipin-scraper\boss.ps1 --mode search --keyword "agent开发实习" --city 北京 --pages 1 --stdout
.\boss-zhipin-scraper\boss.ps1 --mode homepage --homepage-url "https://www.zhipin.com/chengdu/?ka=header-home" --stdout
.\boss-zhipin-scraper\boss.ps1 --mode inbox --stdout
.\boss-zhipin-scraper\boss.ps1 --mode inbox-discover --stdout
.\boss-zhipin-scraper\boss.ps1 --mode inbox-read-active --expect-contact "刘姗" --stdout
.\boss-zhipin-scraper\boss.ps1 --mode inbox-send-active --expect-contact "杨先生" --message "你好" --confirm-send --stdout
.\boss-zhipin-scraper\boss.ps1 --mode detail --job_id id1,id2 --detail-output .\job-data\details.json
.\boss-zhipin-scraper\boss.ps1 --mode detail --job_link "https://www.zhipin.com/job_detail/xxx.html?lid=...&securityId=..." --stdout
.\boss-zhipin-scraper\boss.ps1 --mode detail --job_id id1,id2 --stream-json
```

When installing this skill into a local skills tree, package the summary helper as well:

```bash
cp boss-zhipin-scraper/scripts/job_summary.py ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts/
```

`--stdout` emits one final JSON document. `--stream-json` is detail-only and emits one JSON object per completed job as NDJSON. Direct `--job_link` mode fills title, salary, location, company, company link, visible tags, internship constraints, activity status, and JD from the rendered page when available; unavailable fields stay empty rather than being guessed.

`homepage` output contains a flattened deduplicated `jobs` list plus `sections.selected` and `sections.latest`. Each job keeps `homepage_sections` and native response provenance so repeated jobs remain traceable.

`inbox` only returns job-progress metadata: company, linked job, unread count, last activity time and opaque conversation IDs. `inbox-discover` may summarize JSON/WebSocket envelope keys, but never recruiter names, chat previews, or message bodies. Do not implement automatic message sending; drafts may be prepared separately, but an actual send must be a single, explicit user-confirmed action.

`inbox-read-active` is an explicit, read-only exception for one conversation the user has already opened in the dedicated Chrome. It requires `--expect-contact`, verifies that name appears in the **main message-pane header** rather than merely in the contact list, then returns only the currently rendered logical message rows and their broad type (`incoming_text`, `outgoing_text`, `system_event`, `platform_card`, or attachment). It does not navigate, click, scroll for older history, write a local content file, or send a message.

`inbox-send-active` sends exactly one text only after all of the following are supplied in the same command: the manually selected conversation's `--expect-contact`, the exact `--message`, and `--confirm-send`. It verifies the main message-pane header before focusing the composer, presses Enter once, then checks whether the exact outgoing text count increased. A failed verification is reported but **never retried automatically**.

## Inbox interface register and extension rules

The inbox workflow is deliberately split into **progress monitoring**, **one named conversation**, and **external actions**. The default must always remain the first category.

| Native interface / transport | Current handling | Permitted future use |
| --- | --- | --- |
| `friend/getGeekFriendList.json` | Implemented by `--mode inbox`: company, linked-job key, unread count, last activity, opaque conversation ID | Synchronize application progress and match a reply to one locally stored job |
| `friend/geekFilterByLabel` | Discovered; not yet exposed as a CLI option | Add explicit filters such as unread/new greeting only after the page exposes stable label IDs; output metadata only |
| `zpchat/config/ws` | Discovered; `--mode inbox-discover` passively reports WebSocket envelope schema without values | Establish whether a **specific named conversation** uses history, receipt, or send message types |
| `zpchat/gray/get` | Discovered only | Inspect support flags once when a feature is blocked; do not alter read/notification settings |
| `zpchat/notify/setting/get` | Discovered only | Read notification configuration for diagnosis; never change a setting automatically |
| `zpchat/wechat/guide`, `zpchat/wechat/setting` | Discovered only | Never automate account binding, WeChat exchange, or contact sharing |
| `zpchat/sticker/get` | Explicitly out of scope | Do not integrate |
| `zpchat/group/groupInfoList`, `group/gravityGroupInfoList` | Explicitly out of scope | Do not enumerate or interact with group chats unless the user separately asks for a named group |

### Conversation-content and send guardrails

- Never bulk-read chat bodies. A history read requires a user-named `conversation_id` or an unambiguous company + job pair. For `inbox-read-active`, the expected name must be verified inside the main message-pane header; a match in the left contact list is insufficient.
- Reading an unread conversation can mark it read. Tell the user before a first read when `unread_count > 0`.
- Never send, withdraw, forward, mark interview/exchange, reject, share a phone/WeChat, or send a resume by default.
- An actual message requires one fresh confirmation containing the exact recipient/conversation and exact text. A previous confirmation cannot be reused for another message.
- The sole sender is `inbox-send-active`; it has no contact search, queue, schedule, retry, attachment, resume, phone, WeChat, or status-action capability. It must operate only on a conversation the user has already selected manually in the dedicated Chrome.
- Before implementing a sender, first observe the selected conversation's native protocol structure in read-only mode. Do not guess or replay a WebSocket payload.
- Stop if BOSS returns `code 31`, `code 37`, an authentication/verification screen, or another unexpected business error such as `code 7`; do not probe repeatedly.

## Limits and recovery

- Keep searches to 1–3 pages and details to at most 3 per run.
- On `code 31` or `code 37`, stop. Do not repeatedly run `--check`, `--smoke-test`, or the same search. Complete any visible verification manually and wait before retrying.
- Do not open F12/DevTools during a run.
- The command runner may need 180–240 seconds for three serial details; this is an execution wait setting, not a scraper page limit.

## Implementation notes

- List data comes from the page's native network response, preserving plaintext `salaryDesc`, `lid`, and `securityId`.
- Details use a separate target in the dedicated profile and extract validated JD text from the rendered page.
- JSON writes are incremental and atomic. A timeout after a completed detail should preserve earlier details in the output file.
- `--check` does not validate BOSS login; the real target search is the source of truth for login and API availability.
- `scripts/job_summary.py` reads saved list/detail JSON and produces aggregate salary, skill, JD-term summaries, and a 求职提示词 without reading a local resume.
- `--setup-chrome` never sends a login probe. After manual login, run the actual target search.
- Do not add page-script injection, synthetic mouse jitter, proxy rotation, fingerprint spoofing, CAPTCHA bypass, or bulk crawling.
