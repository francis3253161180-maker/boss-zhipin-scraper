# BOSS scraper launcher for Windows
# Clears HTTP(S)_PROXY / ALL_PROXY for this process tree only, then runs
# scripts\boss_cdp_raw.py. Proxy env vars are restored afterwards, so other
# software keeps using the proxy as usual.
#
# Why: proxy exit IP (e.g. Clash 127.0.0.1:7897) can trigger BOSS risk control
# (code 37, "environment abnormal"). The active Clash profile should also
# route zhipin.com/zhipin.net DIRECT; this wrapper only clears child env vars.
#
# This wrapper is a thin passthrough: all arguments are forwarded to the engine.
# Core functional modes (see engine --help):
#   .\boss.ps1 --mode search --keyword "agent开发" --city 北京 --pages 3 [--stdout] [筛选参数]
#   .\boss.ps1 --mode homepage --homepage-url "https://www.zhipin.com/chengdu/?ka=header-home" --stdout
#   .\boss.ps1 --mode inbox --stdout  # 只读沟通进度，不输出私聊正文
#   .\boss.ps1 --mode inbox-discover --stdout  # 只读协议字段结构，不输出私聊正文
#   .\boss.ps1 --mode inbox-read-active --expect-contact "刘姗" --stdout  # 仅读当前已选会话
#   .\boss.ps1 --mode inbox-send-active --expect-contact "杨先生" --message "你好" --confirm-send --stdout  # 单次已确认发送
#   .\boss.ps1 --mode send --content "您好，我对该岗位很感兴趣..." --job_link "https://www.zhipin.com/job_detail/xxx.html?lid=..&securityId=.." --stdout  # 批量投递：打开 JD → 点立即沟通/继续沟通 → 自动发送
#   .\boss.ps1 --mode read --job_link "https://www.zhipin.com/job_detail/xxx.html?lid=..&securityId=.." --stdout  # 读取 JD 对应会话当前聊天历史（区分 对方/自己 发送；只读不发送）
#   $list | .\boss.ps1 --mode detail --job_id id1,id2 [--stdout]  # 管道自动读 stdin（无需任何参数）
#   .\boss.ps1 --mode detail --job_id id1,id2 --stdout            # 无管道：自动加载最新列表
#   .\boss.ps1 --mode detail --job_link "https://www.zhipin.com/job_detail/xxx.html?lid=..&securityId=.." --stdout  # 直接传完整链接，免列表文件
#   .\boss.ps1 --mode detail --job_id id1,id2 --stream-json  # 每完成一个详情输出一行 JSON
#   .\boss.ps1 --setup-chrome / --stop-chrome / --check
# Piped stdin ($input) is forwarded to python; in detail mode the BOSS_LIST_STDIN env var
# tells the engine to read the list from stdin (--input parameter removed).
#
# If blocked by execution policy:
#   pwsh.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\boss.ps1 --mode search --keyword "agent开发" --city 北京

$ErrorActionPreference = 'Continue'
Push-Location $PSScriptRoot

# --- save original process settings ---
$saved = @{
    'HTTP_PROXY'       = $env:HTTP_PROXY
    'HTTPS_PROXY'      = $env:HTTPS_PROXY
    'ALL_PROXY'        = $env:ALL_PROXY
    'BOSS_RESULT_DIR'  = $env:BOSS_RESULT_DIR
    'BOSS_LIST_STDIN'  = $env:BOSS_LIST_STDIN
    'PYTHONIOENCODING' = $env:PYTHONIOENCODING
}
$savedOutputEncoding = $OutputEncoding

# --- clear proxy for this process tree (avoid BOSS code 37) ---
$env:PYTHONIOENCODING = 'utf-8'   # avoid GBK console crash on Windows
# 管道编码：PS5.1 默认 us-ascii，中文管道会变 ?；强制 UTF-8 让两种 shell 都稳定（PS7 默认已是 utf-8）
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
# 结果目录指向工作区 job-data（search 默认写盘、detail 自动加载最新列表都用它）；已存在则不覆盖
if (-not $env:BOSS_RESULT_DIR) {
    $env:BOSS_RESULT_DIR = Join-Path (Split-Path $PSScriptRoot -Parent) 'job-data'
}
$env:HTTP_PROXY = ''
$env:HTTPS_PROXY = ''
$env:ALL_PROXY = ''

# --- auto pipe: detail 模式收到管道输入时，用环境变量告诉引擎读 stdin（无需 --input 参数） ---
if ($MyInvocation.ExpectingInput) {
    $isDetailMode = $false
    for ($i = 0; $i -lt $args.Count; $i++) {
        $a = $args[$i]
        if ($a -eq '--job_id' -or $a -eq '--job_link') { $isDetailMode = $true }
        elseif ($a -eq '--mode' -and $i + 1 -lt $args.Count -and $args[$i + 1] -eq 'detail') { $isDetailMode = $true }
    }
    if ($isDetailMode) { $env:BOSS_LIST_STDIN = '1' }
}

try {
    if ($MyInvocation.ExpectingInput) {
        $input | python scripts\boss_cdp_raw.py @args
    }
    else {
        python scripts\boss_cdp_raw.py @args
    }
    $code = $LASTEXITCODE
}
finally {
    # --- restore original process settings so Codex/other commands are unaffected ---
    foreach ($k in $saved.Keys) {
        if ($null -eq $saved[$k]) {
            Remove-Item "Env:$k" -ErrorAction SilentlyContinue
        }
        else {
            Set-Item "Env:$k" $saved[$k]
        }
    }
    $OutputEncoding = $savedOutputEncoding
    Pop-Location
}

exit $code
