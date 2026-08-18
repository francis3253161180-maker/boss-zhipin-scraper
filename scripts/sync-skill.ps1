# sync-skill.ps1 — 把仓库根目录的可运行文件同步进 skills/boss-tool/ 技能包
# 用法：
#   .\scripts\sync-skill.ps1                  # 只同步技能包（发布前运行）
#   .\scripts\sync-skill.ps1 -SyncLocalSkill  # 同时同步到本机个人技能 ~/.codex/skills/boss-tool
param(
  [switch]$SyncLocalSkill
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$skillDir = Join-Path $repoRoot 'skills\boss-tool'

$pairs = @(
  @{ Name = 'boss.ps1';                Src = Join-Path $repoRoot 'boss.ps1';                 Dst = Join-Path $skillDir 'boss.ps1' }
  @{ Name = 'scripts\boss_cdp_raw.py'; Src = Join-Path $repoRoot 'scripts\boss_cdp_raw.py'; Dst = Join-Path $skillDir 'scripts\boss_cdp_raw.py' }
  @{ Name = 'scripts\job_summary.py';  Src = Join-Path $repoRoot 'scripts\job_summary.py';  Dst = Join-Path $skillDir 'scripts\job_summary.py' }
  @{ Name = 'data\city_codes.json';    Src = Join-Path $repoRoot 'data\city_codes.json';    Dst = Join-Path $skillDir 'data\city_codes.json' }
  @{ Name = 'requirements.txt';        Src = Join-Path $repoRoot 'requirements.txt';        Dst = Join-Path $skillDir 'requirements.txt' }
  @{ Name = 'LICENSE';                 Src = Join-Path $repoRoot 'LICENSE';                 Dst = Join-Path $skillDir 'LICENSE' }
)

foreach ($p in $pairs) {
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $p.Dst) | Out-Null
  Copy-Item -LiteralPath $p.Src -Destination $p.Dst -Force
  $equal = (Get-FileHash $p.Src).Hash -eq (Get-FileHash $p.Dst).Hash
  if (-not $equal) { throw "同步失败：$($p.Name) 哈希不一致" }
  Write-Output "synced: $($p.Name)"
}

if ($SyncLocalSkill) {
  $localSkill = Join-Path $HOME '.codex\skills\boss-tool'
  if (Test-Path -LiteralPath $localSkill) {
    Copy-Item -LiteralPath (Join-Path $skillDir 'SKILL.md') -Destination (Join-Path $localSkill 'SKILL.md') -Force
    Copy-Item -LiteralPath (Join-Path $skillDir 'SKILL.md') -Destination (Join-Path $localSkill 'SKILL.md.new') -Force
    Write-Output "synced: ~/.codex/skills/boss-tool/SKILL.md (+ SKILL.md.new)"
  } else {
    Write-Warning "未找到本机个人技能目录 $localSkill ，跳过"
  }
}
Write-Output "技能包已同步：$skillDir"