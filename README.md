# BOSS直聘爬虫 · 职位抓取工具 v2.11（Chrome CDP / 明文薪资）

> 🌐 English documentation: [README.en.md](./README.en.md)

![Python](https://img.shields.io/badge/python-3.10+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)
![Version](https://img.shields.io/badge/version-2.11.0-orange.svg)

> ⭐ **个人增强 fork**：本仓库是 [eatmoreduck/boss-zhipin-scraper](https://github.com/eatmoreduck/boss-zhipin-scraper) 的个人维护增强版，新增 **批量投递（`--mode send`）、会话读取（`--mode read`）与自包含的 Codex 技能包（`skills/boss-tool/`）**，可一键安装为本地 Codex 技能。

一个面向个人求职研究的低频职位工具：通过 Chrome DevTools Protocol 连接隔离的已登录 Chrome，导航到目标搜索页并捕获页面自身的 `joblist.json` 响应，输出含**明文薪资**的职位数据（JSON / CSV）。详情页串行抓取并支持流式 NDJSON 输出。

> 📌 **一句话介绍**：不用 Selenium/Playwright，直接通过 Chrome DevTools Protocol 连接本地已登录的 Chrome，复用真实登录态调搜索 API，输出含明文薪资的 JSON/CSV，并生成薪资分布、技能词频和求职材料优化提示词。

![cover](cover.png)

---

## ⚠️ 免责声明

本项目仅供学习和技术研究参考，旨在探讨 Chrome DevTools Protocol、前端反爬机制与数据采集技术。请勿用于任何违反 [BOSS直聘用户协议](https://www.zhipin.com/about/protocol.html) 或相关法律法规的用途，不得用于商业转售、恶意爬取或对目标网站造成负担的行为。使用本项目所产生的一切后果由使用者自行承担，作者不对任何滥用行为负责。

---

## 🚀 30 秒快速开始

```bash
# 1. 克隆 + 装依赖
git clone https://github.com/eatmoreduck/boss-zhipin-scraper.git
cd boss-zhipin-scraper
pip install -r requirements.txt          # 或 uv sync

# 2. 启动隔离 Chrome 并登录（只需一次，登录态持久保存）
python3 scripts/boss_cdp_raw.py --setup-chrome

# 3. 搜索岗位列表
python3 scripts/boss_cdp_raw.py --mode search --keyword "AI Agent" --city 上海 --pages 3 --stdout

# 可选：读取首页个性化推荐与最新职位
python3 scripts/boss_cdp_raw.py --mode homepage --homepage-url "https://www.zhipin.com/chengdu/?ka=header-home" --stdout

# 可选：仅发现收件箱 JSON/WebSocket 协议字段结构（不输出任何会话内容）
python3 scripts/boss_cdp_raw.py --mode inbox-discover --stdout

# 显式读取专用 Chrome 当前已打开的一个会话（不切换、不滚动、不发送）
python3 scripts/boss_cdp_raw.py --mode inbox-read-active --expect-contact "刘姗" --stdout

# 仅在该次已明确确认的前提下，向当前已打开会话发送一条精确文本
python3 scripts/boss_cdp_raw.py --mode inbox-send-active --expect-contact "杨先生" --message "你好" --confirm-send --stdout

# 批量投递：打开 JD → 点击 立即沟通/继续沟通 → 自动发送 --content（发送后自动回读校验并返回 send_success）
python3 scripts/boss_cdp_raw.py --mode send --content "您好，我对该岗位很感兴趣，这是我的简历..." --job_link "https://www.zhipin.com/job_detail/xxx.html?lid=..&securityId=.." --stdout
# 读取 JD 对应会话的当前聊天历史（区分 对方/自己 发送；只读不发送，必须 --stdout）
python3 scripts/boss_cdp_raw.py --mode read --job_link "https://www.zhipin.com/job_detail/xxx.html?lid=..&securityId=.." --stdout
# 列出消息页侧边栏所有会话（名称/公司/job_link/已读或送达/最后消息发送者与已读状态/头像/未读数等完整字段）
python3 scripts/boss_cdp_raw.py --mode read --list --stdout
# 读取当前已打开消息页面的选中会话（不切换、不重新打开）
python3 scripts/boss_cdp_raw.py --mode read --chat --stdout
# 消息页已打开时优先在侧边栏直接切换会话再读取；切换失败回退打开 job_link 进入
python3 scripts/boss_cdp_raw.py --mode read --chat --job_link "https://www.zhipin.com/job_detail/xxx.html?lid=..&securityId=.." --stdout
# 在已打开的消息页直接点击侧边栏会话序号切换并读取（无需重新打开 job_link）
python3 scripts/boss_cdp_raw.py --mode read --chat --switch-index 0,1 --stdout

# 支持全国城市（含三四五线），例如：
python3 scripts/boss_cdp_raw.py --keyword "前端" --city 赣州 --pages 3
# 查看支持的城市：--list-cities [关键词]
python3 scripts/boss_cdp_raw.py --list-cities 江

# 4. 抓取后生成聚合摘要 + 提示词（默认读取最新结果）
python3 scripts/job_summary.py
```

抓完直接拿到：薪资分布、经验要求、高频技能词、求职材料优化提示词。提示词只基于岗位数据，不读取本地简历文件，也不给岗位算个人匹配分。

## 安装为 Codex Skill（可选）

仓库内 `skills/boss-tool/` 是自包含的 Codex 技能包（中文命令手册 + `boss.ps1` + 核心脚本 + 城市码表），可一键安装到本地 Codex：

```powershell
# 在 Codex 中让代理执行 skill-installer，或直接运行（~ 自动展开为用户目录，勿加引号）：
python ~\.codex\skills\.system\skill-installer\scripts\install-skill-from-github.py `
  --repo francis3253161180-maker/boss-zhipin-scraper --ref master --path skills/boss-tool
```

安装后技能位于 `~/.codex/skills/boss-tool/`，重启 Codex 生效；技能包自带 `boss.ps1`、`scripts/`、`data/`，无需另外 clone 仓库。作者更新仓库后用 `scripts/sync-skill.ps1` 保持技能包与根目录代码一致。

## ✨ 特性

- 明文薪资（API 模式，绕过字体反爬）
- 首页个性化推荐与最新职位的原生响应捕获（`--mode homepage`）
- 指定当前会话只读：`inbox-read-active` 在主消息区标题校验联系人后，仅提取页面已渲染的逻辑消息行及消息类型；不读取其它会话、不加载更早历史、不发送
- 单次确认发送：`inbox-send-active` 必须同时提供当前会话联系人、精确文本和 `--confirm-send`；无搜索、无队列、无附件、无自动重试，发送后仅校验该文本是否出现在当前会话
- 批量投递（`--mode send`）：打开 `--job_link` 指定的 JD → 点击「立即沟通/继续沟通」让 BOSS 自动打开并切换会话 → 发送精确 `--content`；发送后不关闭消息页，立即回读聊天历史最后一条做校验，返回 `send_success`/`verified_last_sender`/`verified_last_text`；串行低频、每岗位只发一次、失败不自动重试，检测到风控立即停止整个批次
- 会话读取（`--mode read`）多形态：`--list` 合并原生会话列表与侧边栏 DOM，返回每个会话的名称/头像/公司/职位/job_link/已读或送达状态/最后一条消息（发送者 `self`/`other`、已读或送达状态、文本、时间）/未读数/是否置顶/是否选中/侧边栏序号等完整字段；`--chat` 读取当前已打开消息页面的选中会话；`--chat --job_link` 在消息页已打开时**优先点击侧边栏直接切换会话**，切换失败或未打开消息页才回退打开 job_link 进入（结果带 `entered_via: sidebar|job_link`，汇总含 `via_sidebar`/`via_job_link`）；`--chat --switch-index N` 在已打开的消息页用受信任鼠标事件直接点击侧边栏会话切换（无需 job_link 重新打开）。逐条区分 `self`/`other` 及系统/平台卡片/附件；不滚动更早记录、不发送、stdout-only 不落盘
- Boss 活跃状态独立字段（`boss_active_status`）：列表兼容 `bossOnline`→「在线」，详情可得到「刚刚活跃」等更细状态
- JSON / CSV 双格式输出
- 详情页 JD 抓取 + 技能分析
- 抓取后聚合摘要 + 可复制提示词
- 增量写入（异常退出不丢数据）
- 一键环境检查 + 持久隔离 Chrome CDP profile
- 多维筛选（规模、融资、薪资、经验、学历、行业）
- Windows、macOS、Linux（Windows 工作区请使用 `boss.ps1`）

### 请求路径与输出模式

- 列表搜索使用页面原生网络响应捕获，不再注入第二个同步 XHR，也不在正式搜索前发送固定登录探测。
- 首页模式捕获页面自身的 `recommend/job/list.json` 响应：`sortType=1` 标记为 `selected`（精选岗位），`sortType=2` 标记为 `latest`（最新职位）。
- `--check` 只检查依赖和 CDP，不访问 BOSS；真实目标搜索才是登录态和接口可用性的判断依据。
- `--stdout` 在完成后输出一个完整 JSON 文档；`--stream-json`（仅 detail）每完成一个岗位输出一行 NDJSON。
- `--job_link` 直链详情会从渲染页面补齐可见的标题、公司、薪资、地点、标签和公司链接；不存在的字段保持为空。
- 投递模式（`--mode send`）逐条打开 JD 页面，点击「立即沟通/继续沟通」由 BOSS 自行打开并切换到对应会话，确认 `--content` 已写入输入框后，用受信任的 Enter 序列（rawKeyDown+char+keyUp）发送；发送后**不关闭消息页**，立即回读聊天历史最后一条，若与 `--content` 一致则 `send_success=true`，并附 `verified_last_sender`（self/other）与 `verified_last_text`（回读原文，截断 200 字符）；汇总层额外给出 `sent_verified` 计数。另有 `post_send_visible` 与 `composer_cleared_after_send` 字段；失败只报告、绝不自动重发；开始前有 5 秒预检倒计时可 Ctrl+C 取消，岗位之间 8–15 秒间隔，检测到风控提示立即停止剩余投递。
- 读取模式（`--mode read`）支持三种形态：`--list` 合并原生会话列表与侧边栏 DOM，返回全部会话的 名称/头像/公司/职位/job_link/已读或送达状态/最后一条消息（`last_message_sender` 区分 `self`/`other`、`last_message_read` 区分 已读/送达/未读、`last_message_text`/`last_message_time`）/未读数/是否置顶/是否选中/侧边栏序号 `index`（可直接用于 `--chat --switch-index`）；`--chat` 只读当前已打开消息页面的选中会话；`--chat --job_link` 消息页已打开时先在侧边栏用受信任鼠标事件切换会话再读取，切换失败或无消息页才回退打开 JD 进入（`entered_via` 标记实际入口，汇总含 `via_sidebar`/`via_job_link`）；`--chat --switch-index 0,1` 在当前消息页直接点击侧边栏会话切换后读取（不通过 job_link 重新打开）。每条消息标注 `sender`（`self`/`other`/`system`/`platform`/`attachment`/`unknown`）；必须配 `--stdout`，不写聊天正文到磁盘，不发送、不自动滚动更早历史，检测到风控立即停止。
- JSON 增量写盘使用临时文件原子替换，避免中断留下半截结果。
- 等待采用自适应策略：分页间隔 8–15 秒，详情初始渲染 5–8 秒，仅在缺少“职位描述”时最多补两次短滚动，详情之间 8–15 秒，最后一个岗位不额外等待。

<details>
<summary>🔍 为什么不选 Selenium / Playwright 类爬虫？</summary>

- Selenium/Playwright 会启动完整的受控浏览器，体积大、指纹明显，容易触发 BOSS 的风控和验证码。
- 本工具直接连接你已经登录的真实 Chrome（CDP），复用真实指纹和登录态，调用的也是页面内合法的搜索 API，返回的 `salaryDesc` 本就是明文——不需要解析被字体反爬加密的 DOM 薪资。
- 因此减少了重复请求和不必要的页面注入，通常比额外发起 XHR 的 DOM 抓取方式更稳定。

</details>

## 安装

### 方式 1：克隆到本地再安装（推荐）

由于 `hermes skills install` 的网络请求在某些环境下可能无法直接访问 GitHub，推荐先克隆仓库再本地安装：

```bash
# 1. 克隆仓库
git clone https://github.com/eatmoreduck/boss-zhipin-scraper.git
cd boss-zhipin-scraper

# 2. 复制到 Hermes skills 目录
mkdir -p ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts
cp SKILL.md ~/.hermes/skills/data-science/boss-zhipin-scraper/
cp scripts/boss_cdp_raw.py ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts/
cp scripts/job_summary.py ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts/
mkdir -p ~/.hermes/skills/data-science/boss-zhipin-scraper/data
cp data/city_codes.json ~/.hermes/skills/data-science/boss-zhipin-scraper/data/
```

### 方式 2：curl 一键安装

不需要克隆整个仓库，直接下载必要文件：

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

### 方式 3：hermes skills install（需网络直连 GitHub）

```bash
hermes skills install https://raw.githubusercontent.com/eatmoreduck/boss-zhipin-scraper/master/SKILL.md --category data-science
```

> 注意：此方式依赖 hermes 进程能直接访问 GitHub，如果遇到超时或连接失败，请使用方式 1 或 2。

### 验证安装

```bash
# 检查文件是否存在
ls ~/.hermes/skills/data-science/boss-zhipin-scraper/SKILL.md
ls ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts/boss_cdp_raw.py
ls ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts/job_summary.py
ls ~/.hermes/skills/data-science/boss-zhipin-scraper/data/city_codes.json
```

安装后直接在 Hermes 对话中说"帮我搜一下 BOSS直聘 上上海的 AI Agent 岗位"。

## 作为命令行工具使用

不想装成 Skill 也可以直接当 CLI 用：

```bash
# 1. 克隆 + 安装依赖
git clone https://github.com/eatmoreduck/boss-zhipin-scraper.git
cd boss-zhipin-scraper
pip install -r requirements.txt

# 2. 启动 Chrome CDP
python3 scripts/boss_cdp_raw.py --setup-chrome
# 首次使用也不会复制主 Chrome 登录态；请在弹出的 BOSS 专用浏览器中登录 zhipin.com
# setup 只启动专用 Chrome；手动登录后直接运行目标搜索

# 3. 检查环境
python3 scripts/boss_cdp_raw.py --check

# 可选：真实浏览器/API smoke test（不写结果文件）
python3 scripts/boss_cdp_raw.py --smoke-test

# 4. 抓取
python3 scripts/boss_cdp_raw.py --mode search --keyword "AI Agent" --city 上海 --pages 3 --format csv

# 5. 抓取后摘要和提示词
python3 scripts/job_summary.py --top 15
```

## 参数

| 参数 | 说明 |
|------|------|
| `--keyword` | 搜索关键词（默认 "AI Agent"） |
| `--city` | 城市（中文或 9 位代码，默认上海）。**支持全国城市**（一二三四五线全覆盖，共 300+ 个），运行时自动从 BOSS 同步最新城市码；码表见 [`data/city_codes.json`](data/city_codes.json)，或用 `--list-cities` 查看。本地及在线码表均无法识别的城市名会报错退出，避免静默得到 0 条结果 |
| `--list-cities [关键词]` | 打印支持的城市列表，可选关键词过滤，如 `--list-cities 江` |
| `--pages` | 页数（上限 10） |
| `--format` | json / csv；csv 会同时导出列表和详情 CSV |
| `--mode search/detail/homepage/send/read` | 列表搜索、精选详情、首页推荐/最新职位、批量投递或会话读取 |
| `--content` | `send` 模式要发送的精确文案（必填，最多 500 字符） |
| `--list` | `read` 模式：列出侧边栏所有会话摘要（名称/公司/job_link/已读或送达/时间） |
| `--chat` | `read` 模式：读取聊天；无附加参数=读当前选中会话，配 `--job_link`=从 JD 进入，配 `--switch-index`=直切侧边栏会话 |
| `--switch-index` | `read --chat` 模式：侧边栏会话序号（0 起，逗号分隔），直接点击切换，无需重新打开 job_link |
| `--expect-contact` | `inbox-read-active` / `read --chat` 的当前会话联系人校验姓名 |
| `--max-chat-items` | `inbox-read-active` / `read` 最多输出的已渲染消息条数（1–200） |
| `--homepage-url` | `homepage` 模式目标首页地址 |
| `--inbox-url` | `inbox-discover` / `read --list` 的目标消息页地址 |
| `--capture-seconds` | `homepage` / `inbox-discover` 原生响应捕获窗口，5–30 秒 |
| `--job_link` | 按完整岗位链接选择详情（唯一岗位选择参数，含 lid/securityId） |
| `--stdout` | 完成后输出一个完整 JSON 文档 |
| `--stream-json` | detail 模式每完成一个岗位输出一行 NDJSON |
| `--allow-dom-fallback` | API 无数据时允许降级 DOM 提取；默认关闭，薪资可能不可信 |
| `--check` | 本地环境检查（依赖 + CDP，不访问 BOSS） |
| `--smoke-test` | 用真实 Chrome/CDP 跑一次 BOSS 搜索 API smoke test，不写结果文件 |
| `--setup-chrome` | 一键启动 Chrome CDP（持久隔离 profile） |
| `--copy-login-state` | 手动导入主 Chrome 的 Local State + Cookie 相关文件到隔离 profile（默认、首次启动、重复启动都不复制） |
| `--reset-chrome-profile` | 重建 BOSS 专用 Chrome profile，会清除此专用浏览器内的登录态 |
| `--stop-chrome` | 关闭 BOSS 专用 CDP Chrome（按隔离 profile 精准匹配，不碰主 Chrome） |
| `--close-chrome` | 抓取正常结束后自动关闭专用 Chrome（默认不关；异常退出不触发，保留登录态） |
| `--output` | 列表输出路径（默认 `~/.boss-zhipin-scraper/job-result/`） |
| `--detail-output` | 详情输出路径（默认 `~/.boss-zhipin-scraper/job-result/`） |
| `--cdp-port` | CDP 端口（默认 9222） |
| `--scale/--salary/--experience/--degree` | 筛选条件 |

## 抓取后摘要与提示词

`scripts/job_summary.py` 只读取已抓取的 `boss_jobs_*.json` 和 `boss_details_*.json`，做简单聚合分析并生成一段可复制提示词。它不读取本地简历文件，不引入 PDF 依赖，也不给个人与岗位做分数判断。

```bash
# 读取默认结果目录下最新的 boss_jobs_*.json，并自动匹配同时间戳或最新详情文件
python3 scripts/job_summary.py

# 指定列表和详情文件
python3 scripts/job_summary.py \
  --input ~/.boss-zhipin-scraper/job-result/boss_jobs_20260625_1200.json \
  --details ~/.boss-zhipin-scraper/job-result/boss_details_20260625_1200.json \
  --top 15

# 只输出提示词
python3 scripts/job_summary.py --prompt-only
```

打包安装后也可以使用入口命令：

```bash
uv run boss-summary --top 15
```

摘要会覆盖这些维度：薪资区间、经验要求、学历要求、地区分布、高频公司、技能标签、JD 高频词。提示词会要求模型基于这些统计去做简历关键词补齐、项目经历改写方向和面试准备清单，但明确要求不要虚构经历。

## 文件结构

```
boss-zhipin-scraper/
├── SKILL.md              # Hermes Skill 定义
├── README.md
├── CHANGELOG.md
├── LICENSE
├── pyproject.toml
├── data/
│   └── city_codes.json   # 全量城市码表
├── scripts/
│   ├── boss_cdp_raw.py   # 抓取主脚本
│   └── job_summary.py    # 抓取后摘要 + 提示词
└── requirements.txt
```

## 工作原理

这是一个基于 Chrome CDP 的 BOSS直聘爬虫，核心流程：

1. 通过 Chrome DevTools Protocol (CDP) 连接到已打开的 Chrome
2. 导航到目标搜索页或首页，通过 CDP Network 事件捕获页面自身的职位 JSON 响应
3. 原生页面响应包含明文 `salaryDesc`，并保留 `securityId` / `lid` 等上下文
4. 详情页串行打开并从渲染页面提取 JD 和直链模式缺失的可见字段
5. 每条完成数据原子写入文件，按 `job_id` 去重

默认不会使用 DOM 提取列表，因为 DOM 薪资可能受字体反爬影响。只有明确传 `--allow-dom-fallback` 时，API 无数据才会降级 DOM。

详情页只从包含“职位描述”的详情区提取 JD，整页 `body` 仅用于识别登录墙和导航页，不会直接写入结果。若页面出现“登录查看完整内容”，抓取会明确报错并停止，避免把截断正文、招聘者信息、公司介绍和推荐职位当成完整 JD 保存。

列表到详情：通过 PowerShell 管道传入列表（对列表内全部岗位抓详情），或直接用带 `lid/securityId` 的 `--job_link` 精选单个/多个岗位。`--job_link` 是唯一岗位选择参数，已取代 `--job_id`。

## Chrome profile 安全策略

`--setup-chrome` 默认使用持久隔离 profile，不软链接、不复制你的主 Chrome 数据。首次启动和后续重复启动都只是创建或复用这个专用 profile：

- `~/.boss-zhipin-scraper/chrome-profile`

未显式指定 `--output` 或 `--detail-output` 时，抓取结果默认保存到：

- `~/.boss-zhipin-scraper/job-result`

首次使用需要在这个专用 Chrome 中手动登录 BOSS直聘。使用 `--setup-chrome` 启动后手动登录，再直接运行目标搜索；程序不会发送固定登录探测请求，登录态保存在专用 profile 内，重启机器后仍然保留。

`--check` 不再发送 BOSS 搜索请求；真实目标搜索的页面原生响应同时承担登录态、风控和数据可用性判断。遇到 `code: 31` 或 `code: 37` 会立即停止，不要重复检查或重试。

`--setup-chrome` 的交互式登录页是唯一会主动置前的临时页面；环境检查、列表/详情抓取和 smoke test 创建的临时标签页都会在后台运行，避免自动流程反复抢占当前窗口。这里的“后台”仅表示不激活标签页，专用 Chrome 仍以有界面模式运行，必要时可以手动打开检查。

如确实需要从主 Chrome 手动导入 BOSS 登录态，可以显式运行：

```bash
python3 scripts/boss_cdp_raw.py --setup-chrome --copy-login-state
```

`--copy-login-state` 每次运行都会覆盖隔离 profile 内对应的 Cookie 相关文件；日常启动不要加这个参数。它只复制 `Local State` 和 `Default/Cookies*`、`Default/Network/Cookies*` 这类 Cookie 数据库相关文件，不复制密码库、历史记录、扩展或完整 profile。需要清空专用浏览器登录态时使用：

```bash
python3 scripts/boss_cdp_raw.py --setup-chrome --reset-chrome-profile
```

### 用完如何收尾

抓取/分析结束后，专用 Chrome 不会自动关闭（默认保留登录态，方便你接着跑下一条抓取）。确认不再使用时，可以手动收尾：

```bash
python3 scripts/boss_cdp_raw.py --stop-chrome
```

`--stop-chrome` 只关闭 scraper 隔离 profile（`--user-data-dir`）对应的 Chrome 进程，**绝不**按端口或进程名去 kill，因此不会误伤你正在用的主 Chrome、Gmail、GitHub 等账号。

如果你希望某次抓取正常结束后就顺手关掉 Chrome，可以加 `--close-chrome`：

```bash
python3 scripts/boss_cdp_raw.py --keyword "AI Agent" --city 上海 --pages 3 --close-chrome
```

`--close-chrome` 默认不开启；且只在抓取走完的**成功路径**上触发，登录失败、异常退出等情况不会关闭 Chrome，登录态得以保留。

## 📌 TODO

- [ ] 详情页抓取补强 Referer 与请求指纹，进一步降低风控触发概率

## License

MIT

## 友情链接

- [LINUX DO](https://linux.do/) — 真诚、友善、充满活力的技术社区，本项目认可并推荐。

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=eatmoreduck/boss-zhipin-scraper&type=Date)](https://star-history.com/#eatmoreduck/boss-zhipin-scraper&Date)
