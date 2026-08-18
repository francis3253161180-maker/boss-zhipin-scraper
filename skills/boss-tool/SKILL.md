---
name: boss-zhipin-scraper
description: "BOSS直聘低频求职工具（Windows）：检索岗位、抓取详情、投递消息、读取聊天会话。本手册为个人中文使用说明。"
---

# BOSS 直聘求职工具 · 命令手册（v2.11）

> 边界说明：本文件是**个人中文使用手册**，只介绍 boss 工具全部命令与参数的用法。
> 仓库内还有两份配套文件，职责不同，避免重复维护冲突：
> - `boss-zhipin-scraper/AGENTS.md` —— 仓库开发规则（给在该仓库内改代码的代理/开发者看，不介绍 CLI 用法）。
> - `boss-zhipin-scraper/SKILL.md` —— 开源技能声明（面向开源用户/技能安装，英文为主）。
> 用法以本手册为准；遇到与代码不一致时，以 `scripts/boss_cdp_raw.py --help` 为准。

## 1. 入口与生命周期命令

所有命令都在 boss 工具目录下执行（仓库根目录，或安装到 Codex 技能后的 `~/.codex/skills/boss-tool/`），用封装脚本 `boss.ps1` 而不是直接跑 Python：

```powershell
.\boss.ps1 --setup-chrome      # 启动/重启专用 Chrome（登录态持久，之后无需重复登录）
.\boss.ps1 --stop-chrome       # 关闭专用 Chrome
.\boss.ps1 --check             # 环境诊断（只查依赖与 CDP 连通性，不请求 BOSS）
.\boss.ps1 --smoke-test        # 真实 Chrome/CDP 跑一次搜索 API 测试（会发一次请求，慎用）
.\boss.ps1 --list-cities       # 打印城市列表；可加关键词过滤，如 --list-cities 江
```

- 专用 Chrome 监听 CDP 端口 9222；**保持运行即可反复连接**，无需重复启动。
- `boss.ps1` 会临时清空本进程的 `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY`（结束后恢复），并强制 UTF-8 输出；结果目录自动指向 boss.ps1 所在目录的上一级 `job-data`（仓库内运行时为 `找实习\job-data`）。
- 代理出口 IP 会触发 BOSS 风控 `code 37`；Clash 配置里请把 `zhipin.com`、`zhipin.net` 走 `DIRECT`，其余流量保持代理。

其它 Chrome 工具参数（一般用不到）：

| 参数 | 作用 |
|---|---|
| `--copy-login-state` | 配 `--setup-chrome` 用：从主 Chrome 手动导入登录态文件到独立 profile |
| `--reset-chrome-profile` | 配 `--setup-chrome` 用：重建专用 profile，**会清除专用浏览器登录态** |
| `--close-chrome` | 抓取正常结束后自动关闭专用 Chrome（默认不关） |
| `--cdp-port 9222` | 自定义 CDP 调试端口 |

## 2. 模式总览（--mode）

`--mode` 取值（默认 `search`）：`search`、`detail`、`homepage`、`inbox-discover`、`inbox-read-active`、`inbox-send-active`、`send`、`read`。

| 模式 | 功能 | 必填/常用参数 |
|---|---|---|
| `search` | 多条件检索岗位列表 | `--keyword`、`--city`、`--pages`，可加筛选代码 |
| `detail` | 精选岗位抓取 JD 详情 | `--job_link`（唯一岗位选择参数）或管道喂列表 |
| `homepage` | 首页推荐/最新职位 | `--homepage-url` |

| `inbox-discover` | 只读发现收件箱接口/协议字段 | 无 |
| `inbox-read-active` | 读取专用 Chrome 当前已选会话 | `--expect-contact` |
| `inbox-send-active` | 向当前已选会话发送一条消息 | `--expect-contact`、`--message`、`--confirm-send` |
| `send` | 批量投递：打开 JD → 点立即沟通/继续沟通 → 自动发送 | `--content`、`--job_link` |
| `read` | 读取聊天（`--list`/`--chat`/`--chat --job_link`/`--chat --switch-index`） | 必须加 `--stdout` |

## 3. 各模式用法

### 3.1 search（检索岗位列表）

```powershell
.\boss.ps1 --mode search --keyword "agent开发" --city 北京 --pages 2 --stdout
# 加筛选：--scale 305 --salary 406 --experience 104 --degree 203 --industry 1001
.\boss.ps1 --mode search --keyword "agent开发" --city 北京 --pages 3 --format csv
```

- `--keyword` 搜索关键词；`--city` 中文名或城市代码（默认 `上海`）；`--pages` 页数（最大 10，默认 3）。
- `--format json|csv` 输出格式（默认 json）。
- `--stdout`：结果 JSON 输出到 stdout（日志走 stderr），`jobs[].job_link` 已自动补全 `lid`/`securityId`，可直接给 detail/send/read 使用。
- 不加 `--stdout`：写文件 `boss_jobs_*.json`。
- 筛选参数代码表见第 5 节。

### 3.2 detail（抓取 JD 详情）

列表来源：直接传 `--job_link`（唯一岗位选择参数），或管道喂精选列表（对列表内全部抓详情）。

```powershell
# 管道：search 的 --stdout 直接喂给 detail，对列表内全部岗位抓详情
.\boss.ps1 --mode search --keyword "agent开发" --city 北京 --pages 2 --stdout |
  .\boss.ps1 --mode detail --stdout
# 直接传完整链接精选（唯一岗位选择参数，免列表）
.\boss.ps1 --mode detail --job_link "https://www.zhipin.com/job_detail/xxx.html?lid=..&securityId=.." --stdout
# NDJSON：每个岗位完成即输出一行，适合长任务
.\boss.ps1 --mode detail --job_link "https://www.zhipin.com/job_detail/xxx.html?lid=..&securityId=.." --stream-json --detail-output .\job-data\details.json
```

- `--job_link`：完整 JD 链接（逗号分隔；含 lid/securityId），是**唯一岗位选择参数**，已取代 `--job_id`。
- `--max-details N`：最多抓几个详情；`--detail-output`：详情输出路径；`--allow-dom-fallback`：API 无数据时允许降级 DOM 提取（薪资可能受字体反爬影响，默认关闭）。
- 无 `--job_link` 且无管道时自动加载最新列表会**报错停止**（避免误抓全部岗位）；要抓全部请走管道传入精选列表。
- 安全节奏：详情串行、页面间间隔 8–15s；直接 `--job_link` 模式会从渲染页补齐可获得的字段，缺失字段留空不猜测。

### 3.3 homepage（首页推荐/最新职位）

```powershell
.\boss.ps1 --mode homepage --homepage-url "https://www.zhipin.com/chengdu/?ka=header-home" --stdout
```

- 捕获首页原生推荐响应，`sortType=1` 对应精选岗位，`sortType=2` 对应最新职位。
- 输出含去重后的 `jobs` 列表与 `sections.selected` / `sections.latest`。

### 3.4 inbox-discover（接口发现）

```powershell
.\boss.ps1 --mode inbox-discover --stdout
```

- 只读发现收件箱数据接口与 WebSocket 协议字段结构（不含联系人/聊天内容）。
- 目标页面默认为 `https://www.zhipin.com/web/geek/chat`（可用 `--inbox-url` 改）。

### 3.5 inbox-read-active（读取当前已选会话）

```powershell
.\boss.ps1 --mode inbox-read-active --expect-contact "刘姗" --stdout
```

- 读取专用 Chrome **当前已选中**会话的已渲染内容，不切换、不滚动、不发送。
- `--expect-contact` 必填：会在主消息区标题校验联系人姓名，避免读错对象。
- `--max-chat-items`（1–200，默认 80）限制输出消息条数。

### 3.6 inbox-send-active（单次确认发送）

```powershell
.\boss.ps1 --mode inbox-send-active --expect-contact "杨先生" --message "你好" --confirm-send --stdout
```

- 只向当前已选会话发送，发送前校验主标题联系人；**必须带 `--confirm-send` 才会发送**，缺失则拒绝。
- `--message` 精确发送文本（必填，≤500 字符）。
- 发送后只读校验并返回 `post_send_visible`（页面可见）与 `composer_cleared_after_send`（输入框是否清空）。

### 3.7 send（批量投递）

```powershell
.\boss.ps1 --mode send --content "您好，我对该岗位很感兴趣..." --job_link "https://www.zhipin.com/job_detail/xxx.html?lid=..&securityId=.." --stdout
# 多个岗位：--job_link 逗号分隔
```

流程：直接打开每个 JD 链接 → 页面自动点「立即沟通/继续沟通」→ BOSS 自动打开并切换到对应会话 → 把 `--content` 写入输入框后用受信任的 Enter 事件（rawKeyDown+char+keyUp）发送，全程无逐条确认。

- `--content` 必填、≤500 字符；`--job_link` 必填（逗号分隔）。
- 预检：显示待投递岗位数与文案预览，首个岗位发送前 5 秒倒计时（`Ctrl+C` 可取消）。
- 每岗位只发一次，失败**不自动重试**；岗位间串行间隔 8–15s。
- 发送后自动做**只读验证**：回读当前聊天历史最后一条是否为刚发送的内容，返回 `send_success`（true=发送成功/false=失败，仅报告不重发）。
- 汇总输出 `total/sent/sent_verified/skipped/aborted/results`；每条结果含 `send_success`、`verified_last_sender`、`verified_last_text`、`post_send_visible`、`composer_cleared_after_send`。
- 检测到风控文案（环境异常/访问过于频繁/操作频繁/验证码/请完成验证/稍后再试）立即停止整批。

### 3.8 read（读取聊天，只读）

四种形态，均**必须加 `--stdout`**（不写聊天正文到磁盘）：

```powershell
# 列出侧边栏所有会话（完整结构化字段）
.\boss.ps1 --mode read --list --stdout
# 直接读取当前已打开消息页面的选中会话（不切换/不重新打开）
.\boss.ps1 --mode read --chat --stdout
# 按 job_link 进入指定会话：消息页已打开 → 先在侧边栏直接切换；切换失败/未打开 → 回退打开 JD 进入
.\boss.ps1 --mode read --chat --job_link "https://www.zhipin.com/job_detail/xxx.html?lid=..&securityId=.." --stdout
# 在当前消息页直接点击侧边栏会话序号切换后读取（无需重新打开 job_link）
.\boss.ps1 --mode read --chat --switch-index 0,1 --stdout
# 向后兼容：直接打开 JD 进入对应会话读取
.\boss.ps1 --mode read --job_link "https://www.zhipin.com/job_detail/xxx.html?lid=..&securityId=.." --stdout
```

#### read --list 输出字段（会话级）

| 字段 | 含义 |
|---|---|
| `recruiter_name` / `recruiter_avatar` / `recruiter_title` | 会话名称 / 头像 / 职位头衔 |
| `company` / `job_title` | 公司 / 岗位名 |
| `job_link` | 会话绑定的岗位链接（含 lid/securityId） |
| `last_message_sender` | 最后一条消息发送者：`self`=自己 / `other`=对方 |
| `last_message_read` | 最后一条消息状态：`已读` / `送达` / `未读` |
| `last_message_text` / `last_message_time` | 最后一条消息文本 / 时间 |
| `unread_count` | 未读数 |
| `is_top` / `chat_status` / `relation_type` | 置顶 / 会话状态 / 关系类型 |
| `index` / `rendered` / `selected` | 侧边栏序号（0 起，可直接用于 `--chat --switch-index`）/ 是否渲染到侧边栏 / 是否选中 |
| `conversation_id` / `job_id` | 会话 ID / 岗位 ID |

汇总字段：`conversation_total`（会话总数）、`unread_total`（未读总数）。

#### read --chat 输出字段（消息级）

- 每条消息带 `sender`：`self`（自己发送）/ `other`（对方发送）/ `system` / `platform` / `attachment` / `unknown`，以及 `text`、`time` 等。
- `--chat --job_link`：结果带 `entered_via: sidebar|job_link`（实际入口），汇总含 `via_sidebar` / `via_job_link`。
- `--chat --switch-index N`：直接在已打开的消息页点击侧边栏会话切换（受信任鼠标事件），不通过 job_link 重新打开。
- 汇总：`total/read/skipped/aborted/results`；`--max-chat-items`（1–200，默认 200）。

#### 关于按 job_link 切换（无需映射表）

`--mode read --chat --job_link` 已内置**运行时动态映射**：从 job_link 提取 job_id，实时匹配当前侧边栏行（先按 姓名+公司 对齐，安全时才按位置对齐），因此**不需要持久化的映射表**；`--switch-index` 只是基于 `--list` 输出的 `index` 的便捷入口。

## 4. 通用参数全表

| 参数 | 适用 | 说明 |
|---|---|---|
| `--version` | 通用 | 打印版本号 |
| `--keyword` | search | 搜索关键词（默认 `AI Agent`） |
| `--city` | search | 城市中文名或代码（默认 `上海`） |
| `--pages` | search | 抓取页数（最大 10，默认 3） |
| `--output` | search | 列表数据输出路径 |
| `--detail-output` | detail | 详情数据输出路径 |
| `--homepage-url` | homepage | 目标地址（默认 `https://www.zhipin.com/chengdu/?ka=header-home`） |
| `--inbox-url` | inbox-discover / read --list | 消息页地址（默认 `https://www.zhipin.com/web/geek/chat`） |
| `--capture-seconds` | homepage/inbox-discover | 捕获原生响应秒数（5–30，默认 15） |
| `--expect-contact` | inbox-read-active / inbox-send-active | 当前会话联系人校验姓名 |
| `--max-chat-items` | read/inbox-read-active | 最多输出消息条数（1–200；read 默认 200，inbox-read-active 默认 80） |
| `--list` | read | 列出侧边栏所有会话 |
| `--chat` | read | 读取聊天（当前选中 / `--job_link` 进入 / `--switch-index` 直切） |
| `--switch-index` | read --chat | 侧边栏会话序号（0 起，逗号分隔），直接点击切换 |
| `--message` | inbox-send-active | 精确发送文本 |
| `--confirm-send` | inbox-send-active | 确认执行发送；缺失则拒绝 |
| `--content` | send | 批量投递文案（必填，≤500 字符） |
| `--job_link` | detail/send/read | 完整 JD 链接（逗号分隔；含 lid/securityId，免列表文件；唯一岗位选择参数） |
| `--max-details` | detail | 最多抓几个详情 |
| `--stdout` | 通用 | 结果 JSON 输出到 stdout（日志走 stderr，可 `2>log.txt` 分离） |
| `--stream-json` | detail | 每个详情完成输出一行 JSON（NDJSON） |
| `--allow-dom-fallback` | detail | API 无数据时允许降级 DOM 提取（默认关闭） |
| `--format` | search | `json` 或 `csv`（默认 json） |
| `--cdp-port` | 通用 | CDP 调试端口（默认 9222） |
| `--scale`/`--stage`/`--salary`/`--experience`/`--degree`/`--industry` | search | 筛选代码（见第 5 节） |

## 5. 筛选参数代码表（--mode search 可选）

| 参数 | 可选代码 |
|---|---|
| `--scale` 公司规模 | 301=0-20人 302=20-99 303=100-499 304=500-999 305=1000-9999 306=10000+ |
| `--stage` 融资阶段 | 801=未融资 802=天使轮 803=A轮 804=B轮 805=C轮 806=D轮及以上 807=已上市 808=不需要融资 |
| `--salary` 薪资 | 402=3K以下 403=3-5K 404=5-10K 405=10-20K 406=20-50K 407=50K以上 |
| `--experience` 经验 | 108=在校生 102=应届生 101=经验不限 103=1年以内 104=1-3年 105=3-5年 106=5-10年 107=10年以上 |
| `--degree` 学历 | 209=初中及以下 208=中专/中技 206=高中 202=大专 203=本科 204=硕士 205=博士 |
| `--industry` 行业 | 1001=互联网 1002=电子商务 1003=金融 1004=游戏 1005=企业服务 1006=教育培训 1007=社交网络 1008=医疗健康 1009=生活服务 1010=广告营销 |

示例：`--scale 305 --salary 406 --experience 104 --degree 203 --industry 1001`

## 6. 已废弃并删除的旧指令（勿再用）

- `--detail` / `--no-detail`（search 时一次性抓全部详情）→ 改用 `--mode detail --job_id ...`
- `--analysis`（内置统计报告）→ 由 Codex 对认可的岗位做统计
- `--merge`（合并文件去重）→ 由 Codex 处理数据
- `--detail-ids` / `--ids` / `--job_id`（旧参数名）→ 统一只用 `--job_link`
- `--input` 已删除：列表来源只在 `--mode detail` 生效（管道 / 自动加载最新）

## 7. 安全边界（必须遵守）

- 抓取期间**不要打开 F12/DevTools**；低频小批量（列表 ≤3 页、detail 一次 ≤3 个）最稳。
- 遇到 `code 31` / `code 37` / 验证码 / 其它业务异常（如 `code 7`）：**立即停止**，完成可见的手动验证并等待（建议 30–60 分钟）后再试，不要反复重试。
- `--mode read` 只读：不发送、不滚动加载更早历史、stdout-only 不落盘；读取未读会话可能将其标记为已读，首次读取前先提示。
- `--mode send` 与 `inbox-send-active` 是仅有的两个发送入口；send 把用户提供的 `--job_link` 列表 + 精确 `--content` 视为整批授权，仍保留 5 秒预检倒计时；失败不自动重发。
- 没有联系人搜索、定时、附件、简历、电话、微信等能力；不要伪造或重放 WebSocket 载荷。

## 8. 输出位置与本机环境

- 文件模式：`boss.ps1` 自动把结果目录指向 boss.ps1 所在目录的上一级 `job-data`（仓库内运行为 `找实习\job-data`，技能安装后为 `~/.codex/skills/job-data`），存放 `boss_jobs_*.json` 列表、`boss_details_*.json` 详情、`boss_sends_*.json` 投递记录。
- BOSS 网页无限滚动（无传统分页），脚本按 API 的 `page` 参数取页，两者数据一致。
- 本机环境：Chrome `C:\Program Files\Google\Chrome\Application\chrome.exe`（151.x）；Python 3.13；依赖仅 `requests` + `websocket-client`；官方只保证 macOS/Linux，本机 Windows 已验证。
