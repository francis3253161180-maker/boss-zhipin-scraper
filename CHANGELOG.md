# Changelog

## Unreleased

- Add a self-contained `skills/boss-tool/` Codex skill package (Chinese command manual + `boss.ps1` + core scripts + city codes) so others can install the tool into local Codex with the skill installer (`--repo francis3253161180-maker/boss-zhipin-scraper --ref master --path skills/boss-tool`). Add `scripts/sync-skill.ps1` to keep the package in sync with the repo root code.
- Remove the `--mode inbox` CLI mode. `--mode read --list` now supersedes it with a superset of sidebar fields (company, linked job, read/delivered status, last-message sender and read state, text, time, unread count), so the inbox-progress mode and its `scrape_inbox` / `normalize_inbox_conversations` helpers are deleted. `inbox-discover`, `inbox-read-active`, and `inbox-send-active` remain unchanged.

- Remove the `--job_id` CLI selector. `--job_link` (a complete URL with `lid`/`securityId`) is now the only job-selection parameter in `detail` mode. Piped lists still grab every job in the list; auto-loaded lists now error out without `--job_link` to avoid bulk scraping. The `--job_id` argument and `filter_jobs_by_ids` are deleted. The `job_id` data field remains in outputs as the encrypted job ID (derived from the link) for dedup and matching.
- Add `--mode read`: opens each `--job_link` JD in the dedicated Chrome, clicks `立即沟通`/`继续沟通` so BOSS opens and switches to the conversation, then reads only the currently rendered chat history with per-message sender direction (`self` for own text, `other` for the recruiter, plus `system`/`platform`/`attachment`/`unknown` for non-text rows). Read-only: never types or sends, does not scroll older history, and is stdout-only so private chat bodies are not written to disk. Serial 3-6s pacing; per-job failures are skipped; risk-control markers abort the whole batch.
- Add `--mode send` batch delivery: opens each `--job_link` JD in the dedicated Chrome, clicks `立即沟通`/`继续沟通` so BOSS opens and switches to the conversation, verifies the rendered composer contains `--content`, then sends it with a trusted Enter sequence (rawKeyDown + char + keyUp). Serial with 8-15s pacing, one send per job, and an immediate whole-batch abort on risk-control markers. After sending it keeps the chat page open and reads back the last rendered history row: `send_success=true` when it matches `--content`, plus `verified_last_sender` (self/other) and `verified_last_text` (raw read-back, truncated to 200 chars); the batch summary reports `sent_verified`. Existing `post_send_visible` (outgoing count confirmed by a bounded ≤6s poll) and `composer_cleared_after_send` remain; a failed confirmation is reported but never triggers a resend. A 5-second pre-flight countdown with Ctrl+C precedes the first send.
- Add `--mode read` sidebar and current-chat forms: `--list` merges the native conversation-list response with the rendered sidebar to return every conversation's name, company, linked `job_link`, read/delivered status, and last-message time, exposing a 0-based `index` for direct switching; `--chat` reads only the currently selected conversation on the already open chat page; `--chat --job_link` opens the JD and reads that conversation; `--chat --switch-index 0,1` clicks sidebar rows directly with trusted mouse events (BOSS's SPA ignores `element.click()`) so conversations can be switched without reopening a job_link. Every row keeps the `sender` direction (`self`/`other`/`system`/`platform`/`attachment`/`unknown`); all read forms are read-only, stdout-only, and do not scroll older history.
- Extend `--mode read --list` to full sidebar structure: it now captures the raw `getGeekFriendList.json` items (`wait_for_native_inbox_list` gains `normalized=False`) and merges them with the rendered rows, returning per-conversation `recruiter_avatar`, `selected`, `last_message_sender` (`self`/`other` derived from `lastMessageInfo.fromId` vs `uid`), `last_message_read` (已读/送达/未读 from native `status` 2/1/0), `last_message_text`, `last_message_time`, `unread_count`, `is_top`, `chat_status`, `relation_type`, plus the existing name/company/job_link/index fields. Filtered (non-rendered) conversations are excluded and rows are ordered by sidebar `index`.
- Prefer switching on the open chat page in `--mode read --chat --job_link`: instead of always opening the JD, it now locates the job in the open chat page's sidebar (native items are captured from a fresh hidden target so the user's page is never reloaded or disrupted), clicks the row with trusted mouse events, and reads the history; on any attach/match/click failure or when no chat page is open it falls back to opening the job_link. Each result reports `entered_via` (`sidebar`/`job_link`) and the summary adds `via_sidebar`/`via_job_link` counts. `attach_active_inbox_target` tolerates multiple open chat pages (uses the first) and side-car chat pages are closed.
- Fix post-send confirmation: BOSS prefixes delivered outgoing rows with timestamps (`12:45 送达 `), so `COUNT_ACTIVE_OUTGOING_TEXT_JS` now strips the prefix and uses a contains match; this fixes `post_send_visible` always reporting `false`.
- Add `inbox-send-active`, a current-conversation-only single text sender requiring a named header match, exact text, and `--confirm-send`. It has no recipient search, batch queue, attachment, status action, scheduling, or automatic retry; post-send verification checks the outgoing text count once.
- Correct active-chat logical message classification for BOSS `item-myself` and `item-system` rows, reporting outgoing text and system events separately from incoming text.
- Harden `inbox-read-active` contact verification: the expected name must occur in the main conversation header, not merely in the left contact list. Abort before reading if the active header does not match.
- Add `inbox-read-active`: an explicit, named-contact verified, current-view-only conversation reader. It attaches to an already open dedicated Chrome chat page and extracts logical rendered message rows/types without navigating, clicking, scrolling, writing a chat transcript to disk, or sending.
- Extend read-only `inbox-discover` with WebSocket envelope/schema summaries. It reports protocol keys and frame direction only, never chat body values, attachment URLs, recruiter names, or opaque identifiers embedded in frames.
- Add `--mode homepage` to capture the homepage's native personalized/latest-job responses without controlling the user's main browser; map `sortType=1/2` to `selected/latest`, retain response provenance, and deduplicate the flattened job list.
- Add privacy-minimizing `inbox-discover`: native conversation-list monitoring can return company/job/unread/last-activity metadata while excluding recruiter names, previews, and message bodies. Automatic message sending is intentionally out of scope.
- Capture native BOSS list-page network responses instead of injecting a second synchronous XHR.
- Make `--check` local-only and remove automatic login-probe requests from setup.
- Remove injected page scripts and synthetic mouse events from the normal CDP path.
- Add direct-link detail metadata fallback, visible internship-constraint tags, and detail `--stream-json` NDJSON output.
- Make JSON writes atomic and keep Windows PowerShell/subprocess handling stable.
- Reduce unnecessary waiting: remove list-page scrolling, use 8–15s page/detail gaps, and retry detail scrolling only when the JD section is not yet available.

## 未发布

### 新增
- 详情/列表结果新增独立字段 `boss_active_status`（如「今日活跃」「在线」）：列表兼容 `activeTimeDesc` 与 `bossOnline`（仅在线时映射为「在线」）；详情页从招聘者卡片解析更细粒度状态并优先保留；JD 正文仍剔除该行，不混入描述
- 新增 `--stop-chrome` 命令：抓取/分析完成后关闭 BOSS 专用 CDP Chrome（按 user-data-dir 精准匹配隔离 profile，不碰主 Chrome）；抓取命令新增 `--close-chrome` 选项，正常结束后自动收尾（默认关闭，异常退出不触发以保留登录态）。复用已有 `stop_cdp_chrome` 的安全匹配逻辑，补齐进程关闭/收尾链路的单元测试。（#26）
- 城市码表外置为 `data/city_codes.json`（全量 300+ 城市，覆盖一二三四五线），新增 `--list-cities [关键词]` 命令查看支持的城市；`resolve_city` 查询链改为「本地静态码表 → 运行时拉 BOSS 接口 → 9 位裸码兜底」。城市码表打进 wheel，`pip install` 用户也可用。（#24）

### 修复
- 城市解析先执行本地及在线码表的正反向映射，再接受未收录的 9 位裸城市码；未知城市名现在会在抓取前明确报错退出。在线城市接口同时校验业务 `code`，不再把 `code: 35` 等风控响应静默当作空码表
- 登录探测识别 BOSS 风控码 `code: 37`「您的环境存在异常」为限制状态（RESTRICTED），并对未知风控码按 message 关键字（环境存在异常、访问频繁、安全校验等）兜底识别；避免已登录但被风控/限流的用户被误判为「登录探测响应异常」而无法继续。（#33）
- 登录探测改为区分可用、未登录、限制、空结果和响应异常；每轮仅请求一次并采用有上限的退避等待，`code: 31` 等明确限制会立即停止。探测请求现已纳入全局请求预算，CLI 不再把风控或异常统一提示为未登录。（#31）
- 登录检查、列表/详情抓取和 smoke test 的临时标签页统一在后台创建，仅人工登录页置前，避免自动流程抢占前台焦点（#28）
- 详情页 JD 改为只提取“职位描述”区，并在登录墙、导航页或过短正文出现时拒绝写入，不再把整页 `body`、招聘者信息、公司介绍和推荐职位当作 JD
- 同步 BOSS 当前 `city.json` / `condition.json` 映射，修正城市码以及薪资、经验、学历筛选枚举漂移，并在内置城市表未命中时自动加载 BOSS `cityGroup.json` 支持更多城市中文名
- `scrape_details` 最终保存改用 `os.path.dirname(path) or "."`，`--detail-output` 传不带目录的裸文件名时不再抛 `FileNotFoundError`（与循环内及其它写文件处保持一致）
- 修正城市码：天津 `101030100`、沈阳 `101070100`（原均误用 `101060100`）
- `require_runtime_dependencies` 缺失依赖时同时提示 uv 和 pip 安装方式
- `--merge` 现在会合并旧详情并落盘到 `--detail-output`（之前只合并列表，详情丢失）
- API URL filter 改用 `urlencode`（原字符串拼接，filter 值含特殊字符会出错）

### 变更
- 平台支持声明改为 macOS + Linux（Windows 代码分支保留但未经实测，不再声称支持，避免过度承诺）
- `pyproject.toml` 删除空的 `[csv]` extra（csv 是标准库）
- SKILL.md 脚本路径解析改用 Python `os.path.realpath`（macOS 自带 `readlink` 无 `-f`）

### 新增
- `scripts/job_summary.py` 抓取后摘要脚本：读取已有 JSON，输出岗位聚合摘要和求职材料优化提示词
- `boss-summary` 命令行入口，便于打包安装后直接运行摘要脚本
- 抓取后摘要测试：覆盖 JSON 加载、聚合维度、提示词输出和项目边界
- 版本号一致性测试：校验脚本、pyproject.toml、SKILL.md、README.md 四处版本同步
- CONTRIBUTING.md 贡献指南

## v2.0.0 (2026-06)

### 新功能
- `--check` 环境检查（CDP 连通性、依赖、登录态）
- `--setup-chrome` 一键启动 Chrome CDP（持久隔离 profile）
- `--copy-login-state` 手动导入主 Chrome 的 Local State + Cookie 相关文件到隔离 profile
- `--reset-chrome-profile` 重建 BOSS 专用 Chrome profile
- `--setup-chrome` 默认等待 BOSS 登录完成，并确认接口返回明文薪资
- `--no-wait-login` / `--login-timeout` 控制 setup 登录等待
- 默认抓取结果保存到 `~/.boss-zhipin-scraper/job-result`
- 未传 `--city` 时默认搜索上海
- `--format csv` 同时导出列表 CSV 和详情 CSV
- `--merge` 合并多次抓取结果（去重）
- `--cdp-port` 自定义 CDP 端口（默认 9222）
- `--smoke-test` 用真实 Chrome/CDP 跑一次搜索 API smoke test，不写结果文件
- `--allow-dom-fallback` 显式允许 API 失败时降级 DOM 提取
- `--version` 查看版本号
- 登录态检测：未登录时给出明确提示
- 分析报告技术词动态提取（不再硬编码）
- 进度显示：`[2/3 页, 45/90 条]`

### 改进
- CDP WebSocket 消息过滤 + 超时重试（不再无限卡死）
- 详情页写入去重（中断重跑不重复）
- 请求频率保护（最多 10 页，全局 500 次上限）
- 清除所有 bare except，改为具体异常类型
- API 路径提取为常量，方便维护
- DOM fallback 标记为 deprecated
- DOM fallback 默认关闭，避免把字体反爬后的薪资写进结果
- API 错误行不再被当成职位数据处理
- 详情输出保留 `job_id`、`job_link` 和 `salary_source`
- 详情页访问会带上列表 API 返回的 `securityId` / `lid` 上下文
- `--input ... --analysis --no-detail` 会从 `--detail-output`、同目录同时间戳详情文件、默认结果目录最新详情文件中加载详情
- 登录态检测改为多关键词、多城市 probe，但仍要求接口返回明文薪资
- Linux / Windows 平台支持（Chrome 路径 + 隔离 profile）
- pyproject.toml 版本锁定依赖

### 安全
- 默认不软链接、不复制主 Chrome profile；首次启动也不自动导入主 Chrome 登录态，避免影响 Gmail/GitHub 等主浏览器登录态
- API URL 可配置（`API_JOB_LIST_PATH` 常量）

## v1.0.0 (2026-06)

### 初始版本
- Chrome CDP 抓取 BOSS直聘职位列表
- API 明文薪资（绕过字体反爬）
- 详情页 JD 抓取 + 技能标签提取
- 增量写入（异常退出不丢数据）
- 分析报告（薪资分布、经验要求、简历建议）
- 多维筛选（规模、融资、薪资、经验、学历、行业）
