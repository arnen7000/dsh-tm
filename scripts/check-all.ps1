# check-all.ps1 —— 提交前自检（零依赖、不联网、约 3 秒；-Fast 时更短）
#
# 为什么需要它：本项目踩过的坑集中在"改完看不出问题"的地方 ——
#   · 编码/BOM（.ps1 无 BOM → PS 5.1 按 GBK 解码 → 中文乱码甚至语法坏掉；.json 带 BOM → Python 读不了）
#   · 敏感文件入库（config.json / 日志 / 凭据）
#   · token 明文落到日志或文档里
#   · 文档改了链接却指不到文件
#   · 恒真断言、依赖真实状态的测试
# 这些都不会让程序"报错退出"，只会静默跑偏 —— 所以要用检查代替记忆。
#
# 编码约定：本文件必须存为 **UTF-8 with BOM**（Windows PowerShell 5.1 读无 BOM 的 .ps1 会按 ANSI 解码）。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check-all.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check-all.ps1 -Fast   # 跳过耗时回归
# 退出码：0 = 全部通过（警告不算失败）；非 0 = 第几项失败见输出。

param(
    [switch]$Fast,
    [string]$Repo,
    [string]$Python          # 可选：手工指定解释器；默认自动探测
)

$ErrorActionPreference = "Continue"

if (-not $Repo) {
    $here = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
    $Repo = Split-Path $here -Parent          # scripts\ 的上级 = 仓库根
}
$Repo = [string](Resolve-Path -LiteralPath $Repo)
$fail = 0

function Section($n) { Write-Host ""; Write-Host ("== $n ==") -ForegroundColor Cyan }
function Ok($m)      { Write-Host ("  + " + $m) -ForegroundColor Green }
function Warn($m)    { Write-Host ("  ! " + $m) -ForegroundColor Yellow }
function Bad($m)     { Write-Host ("  x " + $m) -ForegroundColor Red; $script:fail++ }

function Test-Bom($path) {
    $b = [System.IO.File]::ReadAllBytes($path)
    return ($b.Length -ge 3 -and $b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF)
}

# ── 临时目录根 ────────────────────────────────────────────────────────────
# 离线回归的「卫生闸」要知道回归可能往哪儿写。这里**不写死路径**：
# 从 config.json 的 temp_root / install_base 推算（缺省 <安装根>\dsh-versions\dsh-temp），
# 读不到配置就退回主目录下的默认位置。换安装根、换机器都会自动跟随。
function Get-TempWatchDir {
    $base = $env:USERPROFILE
    $tr = ""
    $cfgPath = Join-Path $Repo "config.json"
    if (Test-Path -LiteralPath $cfgPath) {
        try {
            $cfg = Get-Content -LiteralPath $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $ib = [string]$cfg.install_base
            if ($ib -and $ib -ne "<base>") { $base = $ib }
            $base = $base.Replace("<home>", $env:USERPROFILE).Replace("<self>", $Repo)
            $tr = [string]$cfg.temp_root
        } catch { }
    }
    if (-not $tr) { $tr = Join-Path $base "dsh-versions\dsh-temp" }
    $tr = $tr.Replace("<base>", $base).
              Replace("<home>", $env:USERPROFILE).
              Replace("<self>", $Repo)
    if (-not [System.IO.Path]::IsPathRooted($tr)) { $tr = Join-Path $base $tr }
    return $tr
}

# ── Python 解释器解析 ─────────────────────────────────────────────────────
# 为什么需要它：本脚本原来硬编码 `py`。`py` 是 python.org 安装器的 "py launcher"
# 组件，**Microsoft Store / WindowsApps 版 Python 不带它** —— 于是本机（只装了
# WindowsApps 版 Python 3.10）自检固定失败 2 项，pre-commit 钩子随之拦住任何提交，
# 而真正的原因跟被检查的代码毫无关系。
# 策略与 start-manager.bat 保持一致：优先 python，退 py。
function Resolve-Python {
    if ($Python) {
        $c = Get-Command $Python -ErrorAction SilentlyContinue
        if ($c) { return $c.Source }
        if (Test-Path -LiteralPath $Python) { return (Resolve-Path -LiteralPath $Python).Path }
        return $null
    }
    foreach ($n in @('python', 'py', 'python3')) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c) { return $c.Source }
    }
    # PATH 上没有时，探常见安装位置（Store 版 / python.org 版）
    $guess = @()
    if ($env:LOCALAPPDATA) {
        $guess += (Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\python.exe')
        $guess += @(Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Programs\Python') -Filter 'python.exe' -Recurse -ErrorAction SilentlyContinue |
                    ForEach-Object { $_.FullName })
    }
    $guess += @(Get-ChildItem 'C:\Python*' -Filter 'python.exe' -ErrorAction SilentlyContinue |
                ForEach-Object { $_.FullName })
    foreach ($g in $guess) { if ($g -and (Test-Path -LiteralPath $g)) { return $g } }
    return $null
}

# 只关心"我们自己写的东西"：跳过官方手册快照、node_modules、缓存
#
# runtime/ 与 temp/ 必须排除：
#   · runtime/ 是便携 Python 发行版（约 100MB，已在 .gitignore 里），里面的
#     .ps1 是发行版自带的，缺 BOM 属正常，报出来只是噪声，还会拖慢递归；
#   · temp/ 是本会话的临时产物（ACL 报告、验证脚本等），不该参与门禁。
# 两者都不在版本控制内，与"我们写的代码"无关。
function Get-OwnFiles($pattern) {
    # 排除：官方手册快照（按上游原样保留）、node_modules、__pycache__、.git、
    # runtime（第三方运行时）、temp（一次性探针）、.workbuddy-ai（AI 助手的工作记忆，
    # 已被 .gitignore 排除、不随仓库发布；里面难免出现"引用某个坏字符"的笔记，
    # 让它把 U+FFFD 门禁顶红没有意义）。
    #
    # 2026-11-11：-Include 在 PS 5.1 上对不带通配符的 -Path 时灵时不灵
    # （干净 clone 副本里 1/6 直接返回 0 个文件，语法检查被静默跳过）。
    # 改成先枚举再按扩展名过滤，行为与 PowerShell 版本无关。
    $exts = @($pattern | ForEach-Object { [IO.Path]::GetExtension($_).ToLowerInvariant() })
    Get-ChildItem $Repo -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object {
            if (-not ($exts -contains $_.Extension.ToLowerInvariant())) { return $false }
            # 只按**仓库内相对路径**排除目录，不能用绝对路径全文匹配：
            # 仓库若恰好放在 C:\Temp\... 下，祖先路径里的 Temp 会把所有文件
            # 一起排掉，1/6 语法检查被静默跳过（2026-11-11 干净 clone 实测）。
            $rel = $_.FullName.Substring($Repo.Length).TrimStart('\', '/')
            return ($rel -notmatch '(^|\\)(node_modules|__pycache__|\.git|runtime|temp|\.workbuddy-ai)(\\|$)' -and
                    $rel -notmatch '^docs\\官方手册-')
        }
}

Write-Host "仓库：$Repo" -ForegroundColor DarkGray

# ── 0/6 Python 解释器（后面 3 项检查都要用它）──────────────────────────────
Section "0/6 Python 解释器"
$PyExe = Resolve-Python
if (-not $PyExe) {
    Bad "找不到 Python 解释器（试过 py / python / python3 与常见安装位置）。装好 Python，或用 -Python <路径> 指定。"
} else {
    Ok "使用 $PyExe"
}

# ── 1/6 Python 语法 ────────────────────────────────────────────────────────
Section "1/6 Python 语法"
$pys = @(Get-OwnFiles @('*.py', '*.pyw'))
$checker = Join-Path $Repo "scripts\py-syntax-check.py"
if (-not (Test-Path $checker)) {
    Bad "缺少 scripts\py-syntax-check.py（用内存编译检查，不写 __pycache__）"
} elseif ($pys.Count -eq 0) {
    Warn "没有找到 .py/.pyw"
} elseif (-not $PyExe) {
    Warn "跳过：没有可用的 Python 解释器（见 0/6）"
} else {
    # 显式指定编码/容错：否则 PS 5.1 用 GBK 解码 Python 的 UTF-8 输出，
    # 中文报错信息会变成乱码，甚至让整段输出被当成失败（假阳性）。
    $prevEnc = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
        $env:PYTHONIOENCODING = 'utf-8'
        $out = (& $PyExe $checker @($pys | ForEach-Object { $_.FullName }) 2>&1 | Out-String).Trim()
    } finally {
        [Console]::OutputEncoding = $prevEnc
    }
    if ($LASTEXITCODE -ne 0) { Bad "语法检查未通过：`n$out" }
    else { Ok "$($pys.Count) 个 .py/.pyw 编译通过（内存编译，不落 __pycache__）" }
}

# ── 2/6 编码策略 ──────────────────────────────────────────────────────────
Section "2/6 编码策略（.ps1 要 BOM；.json/.md/.py 不要 BOM；.bat 走专用检查；子进程输出要指定解码）"
$before = $fail

foreach ($f in @(Get-OwnFiles '*.ps1')) {
    if (-not (Test-Bom $f.FullName)) {
        Bad "缺 UTF-8 BOM：$($f.FullName.Substring($Repo.Length + 1))（PS 5.1 会把中文按 GBK 解码）"
    }
}
foreach ($f in @(Get-OwnFiles @('*.json', '*.md', '*.py', '*.pyw'))) {
    if (Test-Bom $f.FullName) {
        Bad "多了 UTF-8 BOM：$($f.FullName.Substring($Repo.Length + 1))"
    }
}
foreach ($f in @(Get-OwnFiles '*.bat')) {
    if (Test-Bom $f.FullName) {
        Bad "BOM：$($f.FullName.Substring($Repo.Length + 1))（.bat 必须 GBK，见 scripts/fix-bat-encoding.py）"
    }
}
# 文本文件必须能按 UTF-8 **严格**解码。只看 BOM 是看不出"混进 GBK 字节"的污染 ——
# 典型成因：用 PowerShell 的 Add-Content/Set-Content 往 UTF-8 文件里追加中文。
# .bat 按约定是 GBK，故意跳过。
$strictDec = New-Object System.Text.UTF8Encoding($false, $true)
foreach ($f in @(Get-OwnFiles @('*.md', '*.json', '*.ps1', '*.py', '*.pyw', '*.yml', '*.yaml', '*.txt'))) {
    try {
        $null = $strictDec.GetString([System.IO.File]::ReadAllBytes($f.FullName))
    } catch {
        Bad "不是有效的 UTF-8（可能混进了 GBK 字节）：$($f.FullName.Substring($Repo.Length + 1))"
    }
}
$gi = Join-Path $Repo ".gitignore"      # 没有扩展名，会被上面的 Include 漏掉，单独查
# U+FFFD（替换字符）本身是**合法** UTF-8，上面的严格解码查不出来 —— 但它意味着
# 原文在某次转码里已经被换掉了，读者看到的是「在??台 Windows」。README 第一段
# 曾这样存在过（2026-10-31 发布前检查时才发现），所以门禁里补这一道。
$replChar = [string][char]0xFFFD
$replHits = @()
foreach ($f in @(Get-OwnFiles @('*.md', '*.json', '*.ps1', '*.py', '*.pyw', '*.yml', '*.yaml', '*.txt'))) {
    try {
        $txt = $strictDec.GetString([System.IO.File]::ReadAllBytes($f.FullName))
    } catch { continue }        # 上面已经报过"不是有效 UTF-8"
    if ($txt.Contains($replChar)) {
        $replHits += $f.FullName.Substring($Repo.Length + 1)
    }
}
if ($replHits.Count -gt 0) {
    Bad "含替换字符 U+FFFD（中文在转码时被替换掉了）：$($replHits -join '、')"
} else {
    Ok "没有替换字符 U+FFFD（中文没在转码里丢过）"
}
if (Test-Path $gi) {
    try { $null = $strictDec.GetString([System.IO.File]::ReadAllBytes($gi)) }
    catch { Bad "不是有效的 UTF-8：.gitignore" }
}

$fixer = Join-Path $Repo "scripts\fix-bat-encoding.py"
if (-not $PyExe) {
    Warn "跳过 .bat 编码检查：没有可用的 Python 解释器（见 0/6）"
} elseif (Test-Path $fixer) {
    $out = (& $PyExe $fixer --check 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { Bad ".bat 编码检查未通过：$out" } else { Ok ".bat 编码（GBK）检查通过" }
    # 生成到各版本槽目录的三份 bat（dsh.bat / kill-<port>.bat / dsh-cli.bat）不在仓库里，
    # 上面的 --check 扫不到。2026-10-15 故障反馈「dsh-cli.bat 双击没反应」就是漏在这：
    # 生成器写成 UTF-8，cmd 按 GBK 读导致行被撕裂、「命令语法不正确」rc=255。
    # 槽目录从 config.json 反推；本机没有槽（如刚 clone 的空仓库）时只提示不算失败。
    $out2 = (& $PyExe $fixer --slots --check 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        if ($out2 -match '没有推求出任何槽目录|没从 config.json 推求出|目录里没有 bat|零槽') {
            Warn "槽目录 .bat 检查：本机没有可扫的版本槽（config.json 为空或未安装）"
        } else {
            Bad "槽目录 .bat 编码检查未通过：$out2"
        }
    } else { Ok "槽目录 .bat 编码（GBK）检查通过" }
}

# 文本模式的 open() 必须显式 encoding。
# 为什么单独设闸（2026-11-14，CI 连红 25 次才查出来）：GitHub Actions 的 windows
# runner 是**英文系统**，ANSI 代码页 cp1252；本机中文 Windows 是 cp936。同一行
#   open(p, "w").write("槽外数据")
# 本地写得进去，CI 抛 `UnicodeEncodeError: 'charmap' codec can't encode`。
# PYTHONIOENCODING 只管 stdout/stderr，**管不到 open()**，所以设了也没用。
# 症状是"本地一直绿、线上一直红"，属于最难查的一类 —— 用静态检查代替记忆。
$openc = Join-Path $Repo "scripts\check-open-encoding.py"
if (-not $PyExe) {
    Warn "跳过 open() 编码检查：没有可用的 Python 解释器（见 0/6）"
} elseif (Test-Path $openc) {
    $out3 = (& $PyExe $openc 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { Bad "文本模式 open() 未指定 encoding：`n$out3" }
    else { Ok "文本模式 open() 都写了 encoding（英文 Windows 上也不会炸）" }
}
# ── Python 行内的“行尾注释剥离”（引号内的 # 不算注释）────────────────────
# 检查器自己误报过一次（见 4/6 里关于 token 占位符的注释），所以这里先把
# 字符串/注释排除掉，再判断是否是真正的代码 —— 否则文档里写一句“不要用 text=True”
# 就会被当成违规。
function Strip-PyComment($line) {
    $q = $null
    for ($k = 0; $k -lt $line.Length; $k++) {
        $ch = $line[$k]
        if ($q) {
            if ($ch -eq $q) { $q = $null }
        } elseif ($ch -eq '#') {
            return $line.Substring(0, $k)
        } elseif ($ch -eq '"' -or $ch -eq "'") {
            $q = $ch
        }
    }
    return $line
}

# 子进程输出解码：`text=True`（或 universal_newlines）而不指定 encoding/errors，
# 会按 locale **严格**解码。中文 Windows 的 netstat / taskkill 输出是 CP936，
# 而本机 Python 被 PYTHONUTF8=1 固定成 UTF-8 → 解码失败，且 subprocess 会把异常
# 吞在内部线程里、**只把 stdout 置成 None**。于是症状与真因完全无关：
#   "'NoneType' object has no attribute 'splitlines'"
# 正确做法见 dsh_kill.run_capture：取字节回来自己解（utf-8 -> cp936 -> replace）。
foreach ($f in @(Get-OwnFiles @('*.py', '*.pyw'))) {
    # 只查真正会起子进程的文件，避免无关文本里的巧合命中
    if (-not (Select-String -Path $f.FullName -Pattern 'subprocess|Popen' -Quiet -ErrorAction SilentlyContinue)) {
        continue
    }
    $lines = [System.IO.File]::ReadAllLines($f.FullName, [System.Text.Encoding]::UTF8)
    $inDoc = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        # 维护三引号块状态：奇数个三引号 = 本行进入/离开文档字符串
        $tq = ([regex]::Matches($lines[$i], '("{3})|('''''')')).Count
        $wasInDoc = $inDoc
        if ($tq % 2 -eq 1) { $inDoc = -not $inDoc }
        if ($wasInDoc) { continue }                       # 文档字符串内容：不是代码
        if ((Strip-PyComment $lines[$i]) -notmatch 'text\s*=\s*True|universal_newlines\s*=\s*True') {
            continue
        }
        $end = [Math]::Min($i + 3, $lines.Count - 1)
        $blk = (($lines[$i..$end] | ForEach-Object { Strip-PyComment $_ }) -join " ")
        if ($blk -notmatch 'encoding\s*=' -and $blk -notmatch 'errors\s*=') {
            Bad "子进程 text=True 未指定 encoding/errors（$($f.Name):$($i + 1)）——中文输出会解码失败并被静默吞掉，请用 dsh_kill.run_capture"
        }
    }
}

if ($fail -eq $before) { Ok "编码约定全部符合（含子进程输出解码）" }

# ── 3/6 敏感文件与凭据 ────────────────────────────────────────────────────
Section "3/6 敏感文件（config.json / 日志 / 凭据 不该入库）"
$before = $fail
Push-Location $Repo
$tracked = @(git ls-files)
if ($LASTEXITCODE -ne 0) { Bad "git ls-files 失败（这里不是 git 仓库？）" }
else {
    $sus = $tracked | Where-Object {
        $_ -notmatch '^docs/官方手册-' -and (
            $_ -match '(^|/)config\.json$' -or
            $_ -match '\.log$' -or
            $_ -match 'credentials' -or
            $_ -match '(^|/)\.dsh/'
        )
    }
    if ($sus) { foreach ($s in $sus) { Bad "不该入库：$s" } }
    else { Ok "$($tracked.Count) 个受控文件里没有配置/日志/凭据" }
}
Pop-Location
if ($fail -eq $before) { Ok "敏感文件检查通过" }

# ── 4/6 token 明文 ────────────────────────────────────────────────────────
Section "4/6 token 明文（日志与文档里不该出现完整 token）"
$before = $fail
$scan = @()
$scan += @(Get-ChildItem $Repo -File -Filter *.md -ErrorAction SilentlyContinue)
$scan += @(Get-ChildItem "$Repo\docs" -File -Filter *.md -Recurse -ErrorAction SilentlyContinue |
           Where-Object { $_.FullName -notmatch '\\官方手册-' })
$scan += @(Get-ChildItem $Repo -File -Filter *.log -ErrorAction SilentlyContinue)
foreach ($f in $scan) {
    $hit = Select-String -Path $f.FullName -Pattern 'token=([A-Za-z0-9_\-]{8,})' -AllMatches -ErrorAction SilentlyContinue
    foreach ($h in @($hit)) {
        foreach ($m in $h.Matches) {
            $tok = $m.Groups[1].Value
            # 全是同一个字符（xxxxxxxx / AAAAAAAA）显然是占位符或掩码，不是真 token。
            # 教训：第一版没排除它，结果把自己文档里的 `token=xxxxxxxx` 报成了泄漏 —— 检查器也会误报。
            if ($tok -match '^(.)\1+$') { continue }
            Bad "出现疑似真实 token：$($f.Name)（第 $($h.LineNumber) 行，形如 token=$($tok.Substring(0,4))…）"
        }
    }
}
if ($fail -eq $before) { Ok "$($scan.Count) 个文档/日志里没有完整 token" }

# ── 5/6 文档相对链接 ──────────────────────────────────────────────────────
Section "5/6 文档相对链接（Markdown 里的 ](路径) 是否都能落地）"
$before = $fail
$docs = @()
$docs += @(Get-ChildItem $Repo -File -Filter *.md -ErrorAction SilentlyContinue)
$docs += @(Get-ChildItem "$Repo\docs" -File -Filter *.md -Recurse -ErrorAction SilentlyContinue |
           Where-Object { $_.FullName -notmatch '\\官方手册-' })

# 只检查**会发布**的文档。2026-11-14 修：原来扫的是磁盘上所有 .md，
# 而本地「不发布」的文档还在工作区（`git rm --cached` 只删 index、不删文件），
# 于是 `工程经验总结.md` 引用已删除的 `架构与维护说明.md` 时
# **本地一直通过、CI 一直红** —— 本地过、线上挂，是最难查的一类。
$kept = @()
foreach ($d in $docs) {
    & git -C $Repo check-ignore -q -- "$($d.FullName)" 2>$null
    if ($LASTEXITCODE -ne 0) { $kept += $d }
}
$skippedDocs = $docs.Count - $kept.Count
$docs = $kept
if ($skippedDocs -gt 0) {
    Ok "跳过 $skippedDocs 个未入库的文档（它们不在仓库里，链接对读者就是断的）"
}
$links = 0
foreach ($d in $docs) {
    $dir = Split-Path $d.FullName -Parent
    # 围栏代码块里的 ](...) 不是 Markdown 链接。2026-11-11 修：发布流程文档
    # 的 grep 样例 `Users[\\/](...)` 曾被误判成断链，让总检长期假红。
    $inFence = $false
    foreach ($line in @(Get-Content -LiteralPath $d.FullName -Encoding UTF8 -ErrorAction SilentlyContinue)) {
        if ($line -match '^\s*(```|~~~)') { $inFence = -not $inFence; continue }
        if ($inFence) { continue }
        foreach ($g in [regex]::Matches($line, '\]\(([^)]+)\)')) {
            $t = $g.Groups[1].Value.Trim()
            if ($t -match '^https?://' -or $t -match '^mailto:' -or $t -match '^#') { continue }
            $t = ($t -split '#')[0]
            if (-not $t) { continue }
            $links++
            if (-not (Test-Path (Join-Path $dir ($t -replace '/', [IO.Path]::DirectorySeparatorChar)))) {
                Bad "断链：$($d.Name) -> $t"
            }
        }
    }
}
if ($fail -eq $before) { Ok "$($docs.Count) 个文档、$links 个相对链接全部可落地" }
Write-Host "  （官方手册快照 docs\官方手册-* 按其上游结构原样保留，不参与链接检查）" -ForegroundColor DarkGray

# ── 6/6 离线回归（-Fast 跳过）─────────────────────────────────────────────
Section "6/6 离线回归测试"
if ($Fast) {
    Warn "已用 -Fast 跳过（提交钩子默认走这条；想全量就跑不带 -Fast）"
} else {
    # 候选按"仓库内优先"。历史包袱：这里曾写死一条仓库外的绝对路径，
    # 换机器就找不到，回归测试于是永远静默跳过——门禁形同虚设。
    $all = @(
        (Join-Path $Repo "scripts\test-manager-logic.py"),
        (Join-Path $Repo "scripts\test-adopt-logic.py"),
        (Join-Path $Repo "scripts\test-install-ops.py"),
        (Join-Path $Repo "scripts\test-slot-ops.py"),
        (Join-Path $Repo "scripts\test-bat-text.py"),
        (Join-Path $Repo "scripts\test-ui-prefs.py"),
        (Join-Path $Repo "scripts\test-health.py"),
        (Join-Path $Repo "scripts\test-drift.py"),
        (Join-Path $Repo "scripts\test-standalone.py"),
        (Join-Path $Repo "scripts\test-tk-selfheal.py"),
        (Join-Path $Repo "scripts\test-cli.py"),
        # 配置层地基 + doctor 契约（2026-11-15 新增，第 17 套）
        (Join-Path $Repo "scripts\test-config-guard.py"),
        # CLI 读写分界硬标记 + 四个地基修复（2026-11-15 新增，第 18 套）
        (Join-Path $Repo "scripts\test-cli-boundary.py"),
        # 日志脱敏（pre-commit 也跑这一套，全量总检以前漏了它）
        (Join-Path $Repo "scripts\test-log-mask.py"),
        # 中英双语的完整性（每个 T() 都有译文、换行没被写成 /n、无空译文）
        (Join-Path $Repo "scripts\test-i18n.py"),
        (Join-Path $Repo "scripts\test-move.py"),
        (Join-Path $Repo "scripts\test-log-levels.py"),
        # 便携包发布一致性：包内 VERSION / CHANGELOG / 引导文件必须与文件名同源
        (Join-Path $Repo "scripts\test-portable.py"),
        (Join-Path $Repo "tests\test-manager.py"),
        (Join-Path $Repo "scripts\test-manager.py")
    )
    # 注意：`@(...) | Where-Object {…}` 只命中 1 个时会被**展开成字符串**，
    # 于是 $cand[0] 变成第一个字符（曾实测取到 "E"）。用外层 @() 强制保持数组。
    $cand = @($all | Where-Object { Test-Path $_ })
    if (-not $PyExe) {
        Warn "跳过离线回归：没有可用的 Python 解释器（见 0/6）"
    } elseif ($cand.Count -eq 0) {
        Warn "没找到离线测试脚本（候选：scripts\test-manager-logic.py、scripts\test-adopt-logic.py、scripts\test-install-ops.py、scripts\test-slot-ops.py、scripts\test-bat-text.py、scripts\test-ui-prefs.py、scripts\test-health.py、scripts\test-drift.py、scripts\test-standalone.py、scripts\test-tk-selfheal.py、tests\test-manager.py、scripts\test-manager.py）"
    } else {
        # 卫生闸：回归**不许**在真实盘上留东西。
        # 历史事故（2026-10-31）：test-install-ops.py 真调 run_official_install /
        # adopt_existing，test-bat-text.py 真把 bat 交给 cmd 跑，两边都会 makedirs
        # —— 于是每跑一次总检（含每次 git commit 的 pre-commit 钩子），
        # 临时目录根下就凭空长出 dshV1 / dshV2-2 / dshV9 一串空文件夹。
        # dshV9 从来不是真的槽，它只存在于测试脚本里。
        # 各自修好之后再加这道闸：谁再往真实盘写，总检当场红。
        #
        # 盯哪个目录**不写死**：从 config.json 的 temp_root / install_base 推算，
        # 读不到就退回主目录下的默认位置。这样换安装根、换机器都自动跟随。
        $watch = @(Get-TempWatchDir)
        $snap = @{}
        foreach ($w in $watch) {
            $snap[$w] = if (Test-Path $w) {
                (@(Get-ChildItem -Force $w | ForEach-Object { $_.Name }) -join '|')
            } else { '<absent>' }
        }

        foreach ($t in $cand) {
            Write-Host "  运行：$($t.Substring($Repo.Length + 1))" -ForegroundColor DarkGray
            $out = (& $PyExe $t 2>&1 | Out-String)
            $code = $LASTEXITCODE
            # 失败时只列 FAIL / RESULT：原来连 PASS 一起取最后 4 行，
            # 真正的失败断言会被末尾几条 PASS 挡住，排查时看不到原因。
            $tail = (($out -split "`n") | Where-Object { $_ -match '^(FAIL|RESULT|\s*x\s)' } |
                     Select-Object -Last 12) -join "`n"
            if (-not $tail) { $tail = (($out -split "`n") | Select-Object -Last 8) -join "`n" }
            if ($code -ne 0) {
                # 失败时把完整输出留一份（控制台只显示摘要，排查时要看全貌）。
                $dump = Join-Path $env:TEMP ("dsh-check-all-fail-" + [IO.Path]::GetFileNameWithoutExtension($t) + ".txt")
                try { [IO.File]::WriteAllText($dump, $out, (New-Object System.Text.UTF8Encoding($false))) } catch {}
                Bad "离线回归未通过（$($t.Substring($Repo.Length + 1))，退出码 $code）：`n$tail`n完整输出：$dump"
            }
            else { Ok "$(($out -split "`n" | Where-Object { $_ -match '^RESULT' }))" }
        }

        $dirty = @()
        foreach ($w in $watch) {
            $now = if (Test-Path $w) {
                (@(Get-ChildItem -Force $w | ForEach-Object { $_.Name }) -join '|')
            } else { '<absent>' }
            if ($now -ne $snap[$w]) {
                $dirty += ("{0}（跑前 {1} → 跑后 {2}）" -f $w,
                           $(if ($snap[$w] -eq '<absent>') { '不存在' } else { $snap[$w] }),
                           $(if ($now -eq '<absent>') { '不存在' } else { $now }))
            }
        }
        if ($dirty.Count -gt 0) {
            Bad "回归在真实盘上留了东西（该由测试自己收拾干净）：$($dirty -join '；')"
        } else {
            Ok "回归没在真实盘（$($watch -join '、')）上留任何东西"
        }
    }
}

# ── 汇总 ──────────────────────────────────────────────────────────────────
Write-Host ""
if ($fail -gt 0) {
    Write-Host "自检未通过：$fail 项失败。" -ForegroundColor Red
    exit 1
}
Write-Host "自检全部通过。" -ForegroundColor Green
exit 0
