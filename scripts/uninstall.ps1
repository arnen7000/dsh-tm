<#
.SYNOPSIS
    按 config.json 卸载 dsh 槽，保留用户数据、临时目录和 Node 运行时。
.PARAMETER Base
    覆盖配置中的安装根目录。
.PARAMETER Target
    槽键（可用逗号分隔）或 all。缺省交互选择。
.PARAMETER ConfigPath
    显式指定配置文件。缺省查找安装根或管理器目录下的 config.json。
.PARAMETER DryRun
    只显示计划。
.PARAMETER Yes
    跳过执行确认。
.PARAMETER SkipBackup
    跳过用户数据备份。
.PARAMETER SkipGlobal
    不执行 npm 全局卸载。
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
$Repo = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Repo "runtime\python\python.exe"
$CliArgs = @("-B", "-X", "utf8", (Join-Path $Repo "dsh_uninstall_ops.py"))
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Command = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue
    if (-not $Command -or $Command.Source -like "*\WindowsApps\python.exe") {
        $Launcher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue
        if ($Launcher) {
            $Command = $Launcher
            $CliArgs = @("-3") + $CliArgs
        }
    }
    if (-not $Command) {
        Write-Error "找不到 Python。请使用带 Python 的发行版或安装 Python 3。"
        exit 1
    }
    $Python = $Command.Source
}
if ($Base) { $CliArgs += "--base=$Base" }
if ($Target) { $CliArgs += "--target=$Target" }
if ($ConfigPath) { $CliArgs += "--config-path=$ConfigPath" }
if ($DryRun) { $CliArgs += "--dry-run" }
if ($Yes) { $CliArgs += "--yes" }
if ($SkipBackup) { $CliArgs += "--skip-backup" }
if ($SkipGlobal) { $CliArgs += "--skip-global" }

& $Python @CliArgs
exit $LASTEXITCODE
