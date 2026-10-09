# GameLife 实际使用测试脚本
# ------------------------------------------------------------------
# 目的：用「真实游戏库」跑一遍 GameLife 的终端命令，把每条的真实退出码
#       与关键输出行汇总成 PASS/FAIL 表，最后打印总计。
#
# 只调用 CLI，不修改 game-life\ 下任何源码、不写业务数据
# （默认对会落库的命令强制 --dry-run，除非显式加 -IncludeWrites）。
#
# 用法：
#   .\real_usage_test.ps1                          # 默认账号，只跑离线只读命令
#   .\real_usage_test.ps1 -AccountId 10000001 -AlsoAccounts 10000002,10000004
#   .\real_usage_test.ps1 -IncludeWrites           # 额外真实执行 run / backfill（会落库）
#   .\real_usage_test.ps1 -IncludeNetwork          # 额外测 --with-metadata / radar 联网（慢）
#   .\real_usage_test.ps1 -TestExe                 # 额外测 dist\game-life.exe
# ------------------------------------------------------------------
[CmdletBinding()]
param(
    [string]$AccountId = "10000001",
    [string[]]$AlsoAccounts = @(),
    [switch]$IncludeWrites,
    [switch]$IncludeNetwork,
    [switch]$TestExe,
    [string]$ExePath = ""
)

$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$GameDir  = Join-Path $RepoRoot "game-life"
$Python   = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$MainPy   = Join-Path $GameDir  "main.py"
$Exe      = if ($ExePath) { $ExePath } else { Join-Path $RepoRoot "dist\game-life.exe" }

if (-not (Test-Path $Python)) { Write-Host "[错误] 找不到 venv python：$Python" -ForegroundColor Red; exit 2 }
if (-not (Test-Path $MainPy)) { Write-Host "[错误] 找不到 main.py：$MainPy" -ForegroundColor Red; exit 2 }

$TmpOut = Join-Path $env:TEMP "gam_life_real_test"
New-Item -ItemType Directory -Force -Path $TmpOut | Out-Null

$script:Pass = 0
$script:Fail = 0
$script:Rows = New-Object System.Collections.ArrayList

function Add-Result {
    param([string]$Name, [string]$Group, [bool]$Ok, [string]$ExitCode, [string]$Note)
    $target = if ($Ok) { $script:Pass++ } else { $script:Fail++ }
    $null = $script:Rows.Add([pscustomobject]@{
        Group = $Group; Check = $Name; Result = $(if ($Ok) { "PASS" } else { "FAIL" });
        Exit = $ExitCode; Note = $Note
    })
    $tag = if ($Ok) { "PASS" } else { "FAIL" }
    $color = if ($Ok) { "Green" } else { "Red" }
    Write-Host ("[{0}] {1,-42} exit={2,-4} {3}" -f $tag, $Name, $ExitCode, $Note) -ForegroundColor $color
}

function Get-Line {
    param([string]$Text, [string]$Pattern)
    foreach ($l in ($Text -split "`r?`n")) {
        if ($l -match $Pattern) { return $l.Trim() }
    }
    return ""
}

function Invoke-Src {
    param([string[]]$CliArgs)
    $out = ""
    try {
        $out = (& $Python $MainPy @CliArgs 2>&1 | Out-String)
        $code = $LASTEXITCODE
    } catch {
        $out = $_.Exception.Message
        $code = 999
    }
    return @{ Text = $out; Exit = $code }
}

function Invoke-Exe {
    param([string[]]$CliArgs)
    $out = ""
    try {
        $out = (& $Exe @CliArgs 2>&1 | Out-String)
        $code = $LASTEXITCODE
    } catch {
        $out = $_.Exception.Message
        $code = 999
    }
    return @{ Text = $out; Exit = $code }
}

# 通用检查：退出码符合预期 + 关键行命中
function Test-Cli {
    param(
        [string]$Group, [string]$Name, [string[]]$CliArgs,
        [string[]]$MustMatch = @(), [int]$ExpectExit = 0, [string]$Note = ""
    )
    $r = Invoke-Src $CliArgs
    $ok = ($r.Exit -eq $ExpectExit)
    $hits = @()
    foreach ($p in $MustMatch) {
        $line = Get-Line $r.Text $p
        if ($line) { $hits += $line } else { $ok = $false; $hits += "(未命中/$p)" }
    }
    $note = if ($Note) { $Note + " | " + ($hits -join " ; ") } else { ($hits -join " ; ") }
    Add-Result -Name $Name -Group $Group -Ok $ok -ExitCode $r.Exit -Note $note
    return $r
}

Write-Host ""
Write-Host "=== GameLife 实际使用测试 ===" -ForegroundColor Cyan
Write-Host "账号: $AccountId   源码: $MainPy"
Write-Host "写入类命令: $(if ($IncludeWrites) { '开启（会真实落库）' } else { '关闭（全部 --dry-run）' })"
Write-Host "联网类命令: $(if ($IncludeNetwork) { '开启（慢）' } else { '关闭' })"
Write-Host ""

# ---------- A. 基础与只读终端命令 ----------
Test-Cli -Group "A" -Name "version"        -CliArgs @("--version")  -MustMatch @("game-life") | Out-Null
Test-Cli -Group "A" -Name "help"           -CliArgs @("--help")     -MustMatch @("可用命令") | Out-Null
Test-Cli -Group "A" -Name "stats"          -CliArgs @("stats","--account-id",$AccountId) -MustMatch @("总游戏数","总时长") | Out-Null
Test-Cli -Group "A" -Name "run --dry-run"  -CliArgs @("run","--account-id",$AccountId,"--dry-run") -MustMatch @("状态判定结果","dry-run") | Out-Null
Test-Cli -Group "A" -Name "persona"        -CliArgs @("persona","--account-id",$AccountId) -MustMatch @("游戏人格画像","人格标签") | Out-Null
Test-Cli -Group "A" -Name "recommend"      -CliArgs @("recommend","--account-id",$AccountId,"--time","1h") -MustMatch @("推荐（") | Out-Null
Test-Cli -Group "A" -Name "price"          -CliArgs @("price","--account-id",$AccountId) -MustMatch @("性价比 TOP") | Out-Null
Test-Cli -Group "A" -Name "family"         -CliArgs @("family","--account-id",$AccountId) -MustMatch @("部，通关") | Out-Null
Test-Cli -Group "A" -Name "wishlist"       -CliArgs @("wishlist","--account-id",$AccountId) -MustMatch @("反向愿望单") | Out-Null
Test-Cli -Group "A" -Name "challenge"      -CliArgs @("challenge","--account-id",$AccountId) -MustMatch @("清库挑战","XP") | Out-Null
Test-Cli -Group "A" -Name "radar --offline" -CliArgs @("radar","--account-id",$AccountId,"--with-achievements","--offline") -MustMatch @("成就冲刺雷达") | Out-Null
Test-Cli -Group "A" -Name "backfill --dry-run" -CliArgs @("backfill","--account-id",$AccountId,"--dry-run") -MustMatch @("历史快照回填","dry-run") | Out-Null
Test-Cli -Group "A" -Name "config --get"   -CliArgs @("config","--get","playtime.recent_days") -MustMatch @("playtime.recent_days") | Out-Null

# ---------- B. 可视化（真实库数据源） ----------
foreach ($v in @("report","graveyard","starmap","timeline","today","checkup")) {
    $outFile = Join-Path $TmpOut "$v.html"
    if (Test-Path $outFile) { Remove-Item -LiteralPath $outFile -Force -ErrorAction SilentlyContinue }
    $r = Test-Cli -Group "B" -Name "$v (真实库)" -CliArgs @($v,"--account-id",$AccountId,"--no-open","--output",$outFile) -MustMatch @("已生成（真实库数据）")
    if ($r.Exit -eq 0 -and (Test-Path $outFile)) {
        $sz = (Get-Item -LiteralPath $outFile).Length
        Add-Result -Name "$v 产物" -Group "B" -Ok ($sz -gt 1000) -ExitCode "0" -Note "$sz 字节 -> $outFile"
    } else {
        Add-Result -Name "$v 产物" -Group "B" -Ok $false -ExitCode "$($r.Exit)" -Note "未生成产物"
    }
}

# ---------- C. writeback 拦截（Steam 运行时） ----------
$wb = Invoke-Src @("writeback","--account-id",$AccountId)
$wbBlocked = ($wb.Exit -ne 0) -and ($wb.Text -match "Steam")
$wbOk = ($wb.Exit -eq 0) -and ($wb.Text -match "写回|分类")
Add-Result -Name "writeback（拦截或成功）" -Group "C" -Ok ($wbBlocked -or $wbOk) -ExitCode $wb.Exit `
    -Note ($(if ($wbBlocked) { "已正确拦截：Steam 正在运行" } elseif ($wbOk) { "实际执行成功" } else { "未预期的输出" }))

# ---------- D. 多账号 ----------
$AlsoAccounts = @($AlsoAccounts | ForEach-Object { $_ -split "," } | Where-Object { $_ -and $_.Trim() } | ForEach-Object { $_.Trim() })
foreach ($acc in $AlsoAccounts) {
    Test-Cli -Group "D" -Name "stats @ $acc" -CliArgs @("stats","--account-id",$acc) -MustMatch @("总游戏数") | Out-Null
}

# ---------- E. 写入类（可选，会落库） ----------
if ($IncludeWrites) {
    Test-Cli -Group "E" -Name "run（真实写快照）" -CliArgs @("run","--account-id",$AccountId) -MustMatch @("快照已保存") | Out-Null
    Test-Cli -Group "E" -Name "backfill（真实回填）" -CliArgs @("backfill","--account-id",$AccountId) -MustMatch @("回填条数") | Out-Null
    Test-Cli -Group "E" -Name "backfill 复核（应 0 条）" -CliArgs @("backfill","--account-id",$AccountId,"--dry-run") -MustMatch @("回填条数：0") | Out-Null
} else {
    Add-Result -Name "写入类命令" -Group "E" -Ok $true -ExitCode "-" -Note "已跳过（加 -IncludeWrites 开启）"
}

# ---------- F. 联网类（可选，慢） ----------
if ($IncludeNetwork) {
    Test-Cli -Group "F" -Name "stats --with-metadata" -CliArgs @("stats","--account-id",$AccountId,"--with-metadata") -MustMatch @("总游戏数") | Out-Null
    Test-Cli -Group "F" -Name "radar --with-achievements(联网)" -CliArgs @("radar","--account-id",$AccountId,"--with-achievements") -MustMatch @("成就冲刺雷达") | Out-Null
} else {
    Add-Result -Name "联网类命令" -Group "F" -Ok $true -ExitCode "-" -Note "已跳过（加 -IncludeNetwork 开启）"
}

# ---------- G. 打包产物（可选） ----------
if ($TestExe) {
    if (-not (Test-Path $Exe)) {
        Add-Result -Name "exe 存在性" -Group "G" -Ok $false -ExitCode "-" -Note "找不到 $Exe"
    } else {
        $sz = (Get-Item -LiteralPath $Exe).Length
        $r = Invoke-Exe @("--version")
        $verOk = ($r.Exit -eq 0) -and ($r.Text -match "game-life")
        Add-Result -Name "exe --version" -Group "G" -Ok $verOk -ExitCode $r.Exit -Note ("$sz 字节 ; " + (Get-Line $r.Text "game-life"))
        $s = Invoke-Exe @("stats","--account-id",$AccountId)
        Add-Result -Name "exe stats" -Group "G" -Ok ($s.Exit -eq 0) -ExitCode $s.Exit -Note "退出码 0 即视为通过（exe 中文输出为 GBK）"
    }
} else {
    Add-Result -Name "打包产物" -Group "G" -Ok $true -ExitCode "-" -Note "已跳过（加 -TestExe 开启）"
}

# ---------- 汇总 ----------
Write-Host ""
Write-Host "=== 汇总 ===" -ForegroundColor Cyan
$script:Rows | Format-Table -AutoSize Group, Check, Result, Exit, Note
Write-Host ("PASS {0} / FAIL {1}" -f $script:Pass, $script:Fail) -ForegroundColor $(if ($script:Fail -eq 0) { "Green" } else { "Red" })

if ($script:Fail -gt 0) { exit 1 } else { exit 0 }