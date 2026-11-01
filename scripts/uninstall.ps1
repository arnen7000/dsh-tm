<#
.SYNOPSIS
    dsh-tm 卸载器（config.json 驱动，适配「最多 5 槽」模型）。
.DESCRIPTION
    按 config.json 里**实际配置的槽**卸载，不再写死 v1/v2 两个版本。
    本机部署形态：每一槽都是本地隔离安装（<base>\dsh-versions\dshVx\，自带 node、
    node_modules、.dsh-home），因此卸载 = 停该槽端口上的 dsh → 备份用户数据 →
    删除该槽的程序产物（保留用户数据目录）。

    设计铁律（沿用旧版契约，用户数据绝不删）：
      1) 停进程一律走 dsh_kill.py（签名 + 端口门控）：非 dsh 程序占用端口时**拒绝杀**，
         只告警。绝不「见端口就 taskkill」——那会误杀别的软件。
      2) 用户数据（该槽的 DSH_HOME）**只备份、不删除**，随时可从 <Base>\dsh备份\ 取回。
      3) 临时目录（temp_dir）保留。
      4) 自带 node 运行时、仓库源码（README/脚本/图标/config.example.json）保留。
      5) 全部槽卸完时：备份后移除 config.json / dsh-manager.log / 桌面 dsh-manager.lnk。

    参数化（与旧版的区别）：
      -Target     槽键（v1/v2/v3/v4，可逗号分隔多个）或 all；缺省交互选择
      -DryRun     只打印计划，不做任何改动（强烈建议先跑一次）
      -Yes        跳过执行前确认
      -SkipBackup 跳过用户数据备份（不推荐）
      -SkipGlobal 若某槽是「npm 全局安装」，跳过 npm uninstall -g
.PARAMETER Base
    安装根目录。缺省从 config.json 的 install_base 推导（本机是 <home> = 用户主目录）；
    显式传值则覆盖。
.PARAMETER Target
    槽键或 all。缺省交互选择。
.PARAMETER ConfigPath
    显式指定 config.json。缺省自动探测（<Base>\config.json，再找本脚本上级目录）。
#>
param(
    [string]$Base = "",
    [string]$Target = "",
    [string]$ConfigPath = "",
    [switch]$DryRun,
    [switch]$Yes,
    [switch]$SkipBackup,
    [switch]$SkipGlobal
)

$ErrorActionPreference = "Stop"

# ── 定位 config.json（两处都找：安装根目录 / 本脚本上级目录）─────────────────
# 坑（沿用 2026-09-10 的教训）：管理器读的是 dsh_manager.pyw 所在目录下的 config.json，
# 而安装根目录未必等于那个目录 —— 本机部署就是 Base=$HOME、管理器在仓库目录里。
function Find-ConfigPath {
    $cands = @()
    if ($ConfigPath) { $cands += $ConfigPath }
    if ($Base) { $cands += (Join-Path $Base "config.json") }
    $cands += (Join-Path (Split-Path -Parent $PSScriptRoot) "config.json")
    foreach ($c in $cands) { if ($c -and (Test-Path $c)) { return $c } }
    return $null
}

# ── 解析全部槽：占位符展开 + 派生「程序根目录」──────────────────────────────
function Resolve-Slots {
    param([string]$CfgPath)
    $raw = Get-Content $CfgPath -Raw | ConvertFrom-Json
    $b = $Base
    if (-not $b) {
        $b = [string]$raw.install_base
        if (-not $b) { $b = "<home>" }
    }
    $b = $b.Replace("<self>", (Split-Path -Parent $PSScriptRoot))
    $b = $b.Replace("<home>", $env:USERPROFILE).Replace("<base>", $env:USERPROFILE)
    $out = @()
    foreach ($p in $raw.versions.PSObject.Properties) {
        $k = $p.Name
        $v = $p.Value
        $fix = {
            param($s)
            if (-not $s) { return "" }
            # 注意：PowerShell 不能用反引号续行后以 .方法 开头，必须用中间变量
            $t = ([string]$s).Replace("<self>", (Split-Path -Parent $PSScriptRoot))
            $t = $t.Replace("<base>", $b)
            $t = $t.Replace("<home>", $env:USERPROFILE)
            return $t
        }
        $binjs = & $fix $v.bin_js
        # <root>\node_modules\@deepseek-ai\dsh\lib\bin.js → 往上剥 4 层得到程序根
        $progRoot = ""
        if ($binjs) {
            $d = Split-Path $binjs -Parent
            foreach ($i in 1..4) { if ($d) { $d = Split-Path $d -Parent } }
            $progRoot = $d
        }
        # 是否「npm 全局安装」：bin.js 落在 npm 全局 node_modules 下
        $isGlobal = $false
        if ($binjs) {
            $npmGlobal = Join-Path $env:APPDATA "npm\node_modules"
            if ($binjs.ToLower().StartsWith($npmGlobal.ToLower())) { $isGlobal = $true }
        }
        $out += [pscustomobject]@{
            Key        = $k
            Label      = $(if ($v.label) { $v.label } else { $k })
            Port       = $v.port
            BinJs      = $binjs
            NodeExe    = (& $fix $v.node_exe)
            DshHome    = (& $fix $v.dsh_home)
            TempDir    = (& $fix $v.temp_dir)
            ProgRoot   = $progRoot
            IsGlobal   = $isGlobal
            RootExists = $(if ($progRoot) { Test-Path $progRoot } else { $false })
        }
    }
    return $out
}

# ── 解释器：优先仓库自带的 runtime\python\python.exe（有控制台，输出不丢）───
function Find-Python {
    $cands = @(
        (Join-Path (Split-Path -Parent $PSScriptRoot) "runtime\python\python.exe"),
        (Join-Path (Split-Path -Parent $PSScriptRoot) "runtime\python\pythonw.exe")
    )
    foreach ($c in $cands) { if (Test-Path $c) { return $c } }
    $g = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($g) { return $g.Source }
    return ""
}

# ── 备份目录：只记路径，真正创建推迟到第一次备份时（保证 -DryRun 零副作用）──
$script:BackupDir = ""
function Set-BackupDir {
    param([string]$Under)
    if (-not $script:BackupDir) {
        $script:BackupDir = Join-Path $Under "dsh备份"
    }
    return $script:BackupDir
}

function Backup-UserItem {
    param([string]$Src, [string]$Label)
    if (-not (Test-Path $Src)) {
        Write-Host "   [skip] 备份 $Label：源不存在 -> $Src" -ForegroundColor DarkGray
        return $true        # 没有数据要备份，不算失败
    }
    if (-not (Test-Path $script:BackupDir)) {
        New-Item -ItemType Directory -Force -Path $script:BackupDir | Out-Null
    }
    $ts = Get-Date -Format "yyyyMMdd-HHmmss"
    $dst = Join-Path $script:BackupDir ($Label + "-" + $ts)
    if (Test-Path $dst) { $dst = $dst + "-" + (Get-Random) }
    Write-Host "   [*] 备份 $Label -> $dst（原文件保留）" -ForegroundColor Yellow
    # 单个文件（config.json / dsh-manager.log）**不能**用 robocopy：它的源必须是目录，
    # 传文件进去一律 rc=16（实测踩到，还连带把文件删了）。文件走 Copy-Item，
    # 落点仍是 "$dst" 这个文件名（与历史备份布局一致）。
    if (Test-Path $Src -PathType Leaf) {
        try {
            Copy-Item $Src $dst -Force
            Write-Host "       [ok] 已备份" -ForegroundColor Green
            return $true
        } catch {
            Write-Warning "备份 $Label 失败：$_"
            return $false
        }
    }
    # 先建目标目录：源目录为空时 robocopy 不会创建它，备份目录缺失会被误读成"没备份"。
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    # 用 robocopy 而不是 Copy-Item -Recurse（2026-10-30，与 GUI 的 _copy_user_data 对齐）：
    #   /XJ  不跟随 junction。dsh 的 .dsh-home\profiles\node_modules 里有几百个指向
    #        槽 node_modules 的 junction，Copy-Item 会跟着拷一遍 —— 实测 487 MB 的
    #        用户数据备份成 942 MB / 6 万文件 / 14 分钟，而且部分 junction 指向槽外。
    #   /MT  多线程，实测比逐文件快约 3 倍（196 MB / 15326 文件：15.3 s -> 5.2 s）。
    # robocopy 退出码是**位标志**：0–7 成功（1=有文件被拷），>=8 才是真失败。
    try {
        robocopy $Src $dst /E /XJ /COPY:DAT /DCOPY:DAT /MT:8 /R:1 /W:1 `
            /NFL /NDL /NJH /NJS /NP | Out-Null
        $rc = $LASTEXITCODE
        if ($rc -lt 8) {
            Write-Host "       [ok] 已备份（robocopy rc=$rc）" -ForegroundColor Green
            return $true
        }
        Write-Warning "备份 $Label 失败：robocopy 退出码 $rc"
        return $false
    } catch {
        Write-Warning "备份 $Label 失败：$_"
        return $false
    }
}

# ── 停进程：复用 dsh_kill.py 的安全内核（签名 + 端口门控，拒绝误杀）────────
function Stop-SlotDsh {
    param([int]$Port, [string]$Key)
    $py = Find-Python
    $kill = Join-Path (Split-Path -Parent $PSScriptRoot) "dsh_kill.py"
    if (-not $py -or -not (Test-Path $kill)) {
        Write-Host "   [warn] 找不到 dsh_kill.py 或 Python，跳过停进程（删除时可能撞文件占用）" -ForegroundColor Yellow
        return
    }
    Write-Host "   [*] 停止端口 $Port 上的 dsh（走 dsh_kill 门控，不杀非 dsh 程序）..." -ForegroundColor Yellow
    & $py $kill --port $Port --yes 2>&1 | ForEach-Object { Write-Host "       $_" }
    # dsh_kill 退出码：0=放行/已停 3=端口被非 dsh 占用（拒绝杀）
    if ($LASTEXITCODE -eq 3) {
        Write-Warning "端口 $Port 被非 dsh 程序占用，已按设计拒绝结束它。请手动关闭该程序，或在 config.json 里改掉该槽端口。"
    }
    # 等端口真正释放
    for ($i = 0; $i -lt 20; $i++) {
        $busy = netstat -ano 2>$null | Select-String ":$Port\s.*LISTENING"
        if (-not $busy) { break }
        Start-Sleep -Milliseconds 500
    }
}

# ── 删除护栏：只允许删「槽程序根目录里、且不是用户数据/临时目录」的条目 ────
function Test-SafeToDelete {
    param([string]$Path, [string]$SlotRoot, [string]$Keep1, [string]$Keep2)
    if (-not $Path -or -not (Test-Path $Path)) { return $false }
    $norm = $Path.ToLower().Replace("\", "/")
    $keep = @()
    if ($Keep1) { $keep += $Keep1.ToLower().Replace("\", "/") }
    if ($Keep2) { $keep += $Keep2.ToLower().Replace("\", "/") }
    foreach ($k in $keep) { if ($k -and $norm -eq $k) { return $false } }
    # 必须在槽根目录之内，且槽根本身不能被删
    if (-not $SlotRoot) { return $false }
    $root = $SlotRoot.ToLower().Replace("\", "/").TrimEnd("/")
    if (-not $norm.StartsWith($root + "/")) { return $false }
    # 绝不能碰用户主目录 / 仓库 / 盘符根
    # 坑：变量别叫 $home！$HOME 是 PowerShell 只读自动变量，赋值直接抛
    # "Cannot overwrite variable HOME because it is read-only or constant."
    # （与旧 Stop-Port 里 $pid 那个坑同一类，DryRun 走不到这里，真删文件时才炸）
    $homeDir = $env:USERPROFILE.ToLower().Replace("\", "/")
    $repoDir = (Split-Path -Parent $PSScriptRoot).ToLower().Replace("\", "/")
    if ($norm -eq $homeDir -or $norm -eq $repoDir) { return $false }
    if ($norm -match "^[a-z]:/?$") { return $false }
    return $true
}

function Remove-IfExists {
    param([string]$Path, [string]$Label)
    if (Test-Path $Path) {
        try {
            Remove-Item $Path -Recurse -Force -ErrorAction Stop
            Write-Host "   [ok] 已删除 $Label -> $Path" -ForegroundColor Green
        } catch {
            Write-Warning "删除 $Label 失败（$Path）：$_ —— 可能有进程仍占用，重试或手动删"
        }
    } else {
        Write-Host "   [skip] 删除 $Label：不存在 -> $Path" -ForegroundColor DarkGray
    }
}

# ── 从 config.json 移除多个槽（先备份原配置；UTF-8 无 BOM，Python 才能读）──
function Strip-Slots {
    param([string]$CfgPath, [string[]]$Keys)
    if (-not $CfgPath) { return }
    # 备份失败就不改 config.json —— 它是"这台机器装过哪些槽"的唯一记录。
    # 注意必须接住返回值：PowerShell 函数 `return $true` 会把值送进管道，
    # 不接住就污染调用方的输出流（曾让 verify-uninstall-sim 的总判定失真成 ALL PASS）。
    if (-not (Backup-UserItem $CfgPath "config")) {
        Write-Warning "   [abort] config.json 备份失败，未修改（这些槽仍留在配置里）"
        return
    }
    try {
        $obj = Get-Content $CfgPath -Raw | ConvertFrom-Json
        $removed = @()
        foreach ($k in $Keys) {
            if ($obj.versions.PSObject.Properties.Name -contains $k) {
                $obj.versions.PSObject.Properties.Remove($k)
                $removed += $k
            }
        }
        if ($removed.Count -eq 0) {
            Write-Host "   [skip] config.json 里没有要移除的槽" -ForegroundColor DarkGray
            return
        }
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($CfgPath, ($obj | ConvertTo-Json -Depth 8), $utf8NoBom)
        Write-Host "   [ok] 已从 config.json 移除槽：$($removed -join ', ')（其余槽保留，管理器仍可用）" -ForegroundColor Green
    } catch {
        Write-Warning "改写 config.json 失败：$_（已备份原配置，请手动编辑）"
    }
}

function Find-DesktopLnk {
    $cands = @(
        ([System.Environment]::GetFolderPath("Desktop") + "\dsh-manager.lnk"),
        (Join-Path $env:USERPROFILE "Desktop\dsh-manager.lnk")
    )
    foreach ($c in $cands) { if ($c -and (Test-Path $c)) { return $c } }
    return $null
}

# ══════════════════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════════════════
$Cfg = Find-ConfigPath
if (-not $Cfg) {
    Write-Host "找不到 config.json（试过 -ConfigPath / <Base>\config.json / 本脚本上级目录）。" -ForegroundColor Red
    Write-Host "没有配置就无法知道有哪些槽 —— 请先确认路径，或手动删除各版本目录。" -ForegroundColor Red
    exit 1
}
Write-Host "[*] 配置文件：$Cfg" -ForegroundColor DarkGray

$Slots = @(Resolve-Slots -CfgPath $Cfg)
if ($Slots.Count -eq 0) {
    Write-Host "config.json 里没有任何版本槽（versions 为空）——已是「未安装 dsh」状态。" -ForegroundColor Yellow
    exit 0
}

if (-not $Base) { $Base = $env:USERPROFILE }
Set-BackupDir -Under $Base | Out-Null

# ── 选择要卸的槽 ─────────────────────────────────────────────────────────
$Keys = @()
if ($Target) {
    if ($Target -eq "all" -or $Target -eq "both") {
        $Keys = @($Slots | ForEach-Object { $_.Key })
    } else {
        $Keys = @($Target.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    }
} else {
    Write-Host "`n============================================" -ForegroundColor Cyan
    Write-Host "  config.json 里配置了 $($Slots.Count) 个槽：" -ForegroundColor Cyan
    $i = 1
    foreach ($s in $Slots) {
        $st = if ($s.RootExists) { "程序目录在" } else { "程序目录缺失" }
        Write-Host ("    {0}) {1,-4} {2,-16} 端口 {3,-6} {4}" -f $i, $s.Key, $s.Label, $s.Port, $st) -ForegroundColor White
        $i++
    }
    Write-Host ("    a) 全部卸载（{0} 个槽）" -f $Slots.Count) -ForegroundColor White
    Write-Host "============================================" -ForegroundColor Cyan
    $ans = Read-Host "输入编号（可多选，用逗号分隔）或 a"
    if ($ans -eq "a") {
        $Keys = @($Slots | ForEach-Object { $_.Key })
    } else {
        foreach ($n in ($ans.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
            if ($n -match '^\d+$' -and [int]$n -ge 1 -and [int]$n -le $Slots.Count) {
                $Keys += $Slots[[int]$n - 1].Key
            } else {
                Write-Warning "忽略无效输入：$n"
            }
        }
    }
}
if ($Keys.Count -eq 0) { Write-Host "没有选中任何槽，退出。" -ForegroundColor Yellow; exit 0 }

$Targets = @($Slots | Where-Object { $Keys -contains $_.Key })
$Unknown = @($Keys | Where-Object { $_ -notin @($Slots | ForEach-Object { $_.Key }) })
foreach ($u in $Unknown) { Write-Warning "config.json 里没有槽 '$u'，已忽略" }

# ── 打印计划 ────────────────────────────────────────────────────────────
Write-Host "`n========== 卸载计划（$($Targets.Count) 个槽） ==========" -ForegroundColor Cyan
foreach ($s in $Targets) {
    Write-Host ("  槽 {0}（{1}，端口 {2}）" -f $s.Key, $s.Label, $s.Port) -ForegroundColor White
    Write-Host "     1. 停止端口 $($s.Port) 上的 dsh（门控：只杀 dsh，拒绝误杀）"
    if ($s.DshHome) { Write-Host "     2. 备份用户数据 -> $script:BackupDir\  （$($s.DshHome) 原样保留，不删）" }
    if ($s.IsGlobal) {
        Write-Host "     3. npm 全局卸载 @deepseek-ai/dsh（该槽是全局安装）"
    } elseif ($s.ProgRoot) {
        Write-Host "     3. 删除程序产物：$($s.ProgRoot) 下除用户数据/临时目录以外的内容"
    } else {
        Write-Host "     3. （无反推出程序目录，跳过删除）"
    }
    Write-Host "     4. 从 config.json 移除该槽"
}
if ($Targets.Count -eq $Slots.Count) {
    Write-Host "  【全部槽都会被卸掉】随后还会：备份并移除「本配置所在目录」的 config.json / dsh-manager.log" -ForegroundColor Yellow
    Write-Host "      （用 -ConfigPath 指向别处时，仓库日志与桌面快捷方式不会被动）" -ForegroundColor DarkGray
}

if ($DryRun) {
    Write-Host "`n[DryRun] 以上只是计划，未做任何改动。" -ForegroundColor Green
    exit 0
}

# ── 确认 ────────────────────────────────────────────────────────────────
if (-not $Yes) {
    Write-Host ""
    $ok = Read-Host "确认执行？输入 yes 继续，其它任意键取消"
    if ($ok -ne "yes") { Write-Host "已取消，未做任何改动。" -ForegroundColor Yellow; exit 0 }
}

# ── 执行 ────────────────────────────────────────────────────────────────
Write-Host "`n========== 开始卸载 ==========" -ForegroundColor Cyan
$Aborted = @()      # 备份失败而**没动过**的槽 —— 收尾时不能从 config 里移除它们
foreach ($s in $Targets) {
    Write-Host "`n--- 槽 $($s.Key)（$($s.Label)）---" -ForegroundColor Cyan
    Stop-SlotDsh -Port $s.Port -Key $s.Key

    if (-not $SkipBackup -and $s.DshHome) {
        $backedUp = Backup-UserItem $s.DshHome ("{0}-userdata" -f $s.Key)
        # 铁律：备份不成功就**一个字都不删**。以前这里只 warn 然后继续删，
        # 等于拿用户数据赌"备份失败也能恢复"（2026-10-30 与 GUI 行为对齐）。
        if (-not $backedUp) {
            Write-Warning "   [abort] 该槽用户数据未备份成功，跳过删除与清理（数据保持原样，可重试）"
            $Aborted += $s.Key
            continue
        }
    }

    if ($s.IsGlobal) {
        if ($SkipGlobal) {
            Write-Host "   [skip] 已指定 -SkipGlobal，不执行 npm uninstall -g" -ForegroundColor DarkGray
        } else {
            Write-Host "   [*] npm uninstall -g @deepseek-ai/dsh（仅此一个包）..." -ForegroundColor Yellow
            npm uninstall -g "@deepseek-ai/dsh" 2>&1 | ForEach-Object { Write-Host "       $_" }
        }
    } elseif ($s.ProgRoot -and (Test-Path $s.ProgRoot)) {
        Write-Host "   [*] 清理程序产物（保留用户数据 $(if ($s.DshHome) { $s.DshHome } else { '(未配置)' }) 与临时目录）..." -ForegroundColor Yellow
        $kept = @()
        Get-ChildItem -Force -Path $s.ProgRoot | ForEach-Object {
            if (Test-SafeToDelete -Path $_.FullName -SlotRoot $s.ProgRoot -Keep1 $s.DshHome -Keep2 $s.TempDir) {
                try {
                    Remove-Item $_.FullName -Recurse -Force -ErrorAction Stop
                    Write-Host "       [ok] 删除 $($_.Name)" -ForegroundColor Green
                } catch {
                    Write-Warning "删除 $($_.FullName) 失败：$_"
                }
            } else {
                $kept += $_.Name
            }
        }
        if ($kept.Count -gt 0) {
            Write-Host "       [keep] 保留：$($kept -join ', ')" -ForegroundColor DarkGray
        }
    } else {
        Write-Host "   [skip] 程序目录不存在：$($s.ProgRoot)" -ForegroundColor DarkGray
    }
}

# ── 改写 config.json ────────────────────────────────────────────────────
Write-Host "`n--- 更新 config.json ---" -ForegroundColor Cyan
# 备份失败、什么都没删的槽必须留在配置里，否则管理器会显示一个"已卸载"的槽、
# 而磁盘上数据其实还在（用户下次点启动会找不到程序）。
$StripKeys = @($Keys | Where-Object { $Aborted -notcontains $_ })
if ($Aborted.Count -gt 0) {
    Write-Warning "   以下槽因备份失败被跳过，仍保留在 config.json 里：$($Aborted -join ', ')"
}
if ($StripKeys.Count -gt 0) {
    Strip-Slots -CfgPath $Cfg -Keys $StripKeys
} else {
    Write-Host "   没有需要移除的槽，config.json 未改动。" -ForegroundColor DarkGray
}

# ── 全部卸完：收尾（备份后移除配置/日志/桌面图标）────────────────────────
# 安全边界（2026-10-15 实测踩坑后加）：收尾**只动「本次编辑的那份 config.json 所在目录」**
# 里的日志；桌面快捷方式也只有当「卸的就是仓库自身那份 config.json」时才删。
# 否则：用 -ConfigPath/-Base 指到别处做测试/局部卸载时，会把仓库的日志、
# 用户桌面的 dsh-manager.lnk 一起删掉 —— 实测已经误删过一次桌面快捷方式。
if ($Targets.Count -eq $Slots.Count) {
    Write-Host "`n--- 收尾：已卸完全部槽 ---" -ForegroundColor Cyan
    # 备份失败就不删 —— 删掉 config.json 等于丢掉"这台机器装过哪些槽"的唯一记录。
    $cfgBacked = $true
    if (-not $SkipBackup) { $cfgBacked = Backup-UserItem $Cfg "config" }
    if ($cfgBacked) {
        Remove-IfExists $Cfg "config.json（已备份到 dsh备份）"
    } else {
        Write-Warning "   [abort] config.json 未备份成功，保留原文件（未删除）"
    }
    $cfgDir = Split-Path -Parent $Cfg
    $log = Join-Path $cfgDir "dsh-manager.log"
    $logBacked = $true
    if (-not $SkipBackup) { $logBacked = Backup-UserItem $log "dsh-manager.log" }
    if ($logBacked) {
        Remove-IfExists $log "dsh-manager.log（已备份到 dsh备份）"
    } else {
        Write-Warning "   [abort] dsh-manager.log 未备份成功，保留原文件（未删除）"
    }
    $repoCfg = Join-Path (Split-Path -Parent $PSScriptRoot) "config.json"
    $isRepoCfg = ((Split-Path -Parent $Cfg) -ieq (Split-Path -Parent $repoCfg))
    if ($isRepoCfg) {
        $lnk = Find-DesktopLnk
        if ($lnk) { Remove-IfExists $lnk "桌面 dsh-manager.lnk" }
        else { Write-Host "   [skip] 桌面无 dsh-manager.lnk" -ForegroundColor DarkGray }
    } else {
        Write-Host "   [keep] 本次卸载的不是仓库自身配置（$Cfg），桌面快捷方式与仓库日志保持不动" -ForegroundColor DarkGray
    }
}

Write-Host "`n========== 卸载完成 ==========" -ForegroundColor Cyan
Write-Host "  • 用户数据已备份到：$script:BackupDir" -ForegroundColor White
Write-Host "  • 【未删除】：各槽 DSH_HOME 用户数据、临时目录、自带 node 运行时、仓库源码" -ForegroundColor White
if ($Targets.Count -eq $Slots.Count) {
    Write-Host "  • 已恢复到「未安装 dsh」状态；想恢复某版本，打开管理器点「安装官方版 dsh」即可" -ForegroundColor White
} else {
    Write-Host "  • 其余槽仍可正常使用：双击桌面 dsh-manager.lnk（已移除卸载的那个槽）" -ForegroundColor White
}
Write-Host "  • 想先看计划再动手：下次加 -DryRun 跑一遍`n"
