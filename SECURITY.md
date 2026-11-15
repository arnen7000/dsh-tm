# Security Policy / 安全策略

> **English first, Chinese follows.** 英文在前、中文紧随。
>
> This tool touches your dsh user data (sessions, settings, login credentials) and local
> processes, so we treat security issues as "possible data loss or credential leakage",
> not as ordinary bugs.
> 这个工具会接触**你的 dsh 用户数据**（会话、设置、登录凭据）和**本机进程**，所以安全问题
> 按「可能造成数据丢失或凭据泄漏」来对待，不按普通 bug 处理。

---

## English

### Reporting a vulnerability

**Please do not open a public issue.** Use GitHub's private reporting channel:

> Repository → **Security** → **Report a vulnerability** (GitHub Security Advisories)

Please include: reproduction steps, the impact (data loss / credential leak / deletion
outside a slot), your Windows version, and the manager version (from the title bar or
`version` in `config.json`). You may attach a log — first check "What the logs contain"
below.

### What we care about

| Category | Examples |
|---|---|
| **Credential leakage** | A token appears in logs / UI / CLI output; `.credentials.yaml` is read, uploaded, or written anywhere other than the backup |
| **Accidental data loss** | Uninstall / move / cleanup deletes outside the slot directory; deletion continues despite an incomplete backup; a junction target's contents are deleted along with the link |
| **Out-of-scope writes** | A misconfigured install root writes to `C:\` or the user profile; registry or global `PATH` edits |
| **Command injection** | A slot path or `label` contains cmd metacharacters that end up executed inside a generated `.bat` |
| **Silent failure** | An exception is swallowed where an error should be reported, making the user believe an operation succeeded |

### Existing safeguards (so you can tell "by design" from "bug")

- **Tokens**: masked at equal length in logs (three layers: on write, in subprocess output,
  and a full re-scan at startup); the full value lives only in the GUI process memory.
  `dsh-tm.py url` prints only the bare address.
- **Deletion**: only entries *inside* the slot root are removed; an incomplete backup
  (missing files / uncopyable items) **aborts** before a single byte is deleted; backups
  never follow junctions.
- **Detach**: changes `config.json` only — nothing on disk is touched.
- **Killing processes**: only matches "that slot's absolute `bin.js` path + port";
  it **refuses** to kill when the port is held by a non-dsh program.
- **Config writes**: atomic write plus a pre-change backup; a parse failure is never
  silently overwritten — the file is saved as `.bad-<timestamp>` and a dialog is shown.
- **No network**: everything is offline except "fetch version list" and "install dsh"
  (both hit the npm registry and both are started by you). No telemetry.

### What the logs contain (check before attaching one)

`?token=...` is masked, but **local paths and the Windows username are kept verbatim**
(they are needed for diagnosis). Consider replacing `C:\Users\<your name>` before
attaching. Also, the `dsh备份\` folder holds a plaintext copy of `.credentials.yaml` —
**do not** bundle that folder into anything you send.

### Supported versions

Fixes are provided only for the **latest release** and the latest commit on `main`.
For an older version, please upgrade and reproduce first.

### Response

This is a personally maintained open-source project with no SLA. Confirmed issues get
their fix version recorded in `CHANGELOG.md`; severe ones (credential leakage / data
loss) take priority.

---

## 中文

### 怎么报告

**请不要开公开 issue。** 用 GitHub 的私密报告通道：

> 仓库页 → **Security** → **Report a vulnerability**（GitHub Security Advisories）

在报告里写清：复现步骤、影响（丢数据 / 泄漏凭据 / 越权删除）、你的 Windows 版本、
管理器版本（界面标题栏或 `config.json` 的 `version`）。日志可以先自查再附上 —— 见下面的「日志里有什么」。

### 我们在意什么

| 类别 | 例子 |
|---|---|
| **凭据泄漏** | token 出现在日志 / 界面 / CLI 输出；`.credentials.yaml` 被读、被上传、被写进备份以外的位置 |
| **误删用户数据** | 卸载 / 搬迁 / 清理时删到槽目录之外；备份不完整却继续删除；把 junction 目标内容一起删掉 |
| **越界写** | 安装根配错时往 `C:\` 或用户主目录写；改注册表 / 全局 `PATH` |
| **命令注入** | 槽路径或 `label` 含 cmd 元字符，拼进 `.bat` 后被执行 |
| **静默失败** | 该报错的地方吞掉异常，让用户以为成功 |

### 已经做了哪些防护（方便你判断"这是不是设计如此"）

- **token**：日志里等长掩码（三层：写入时脱敏、子进程输出就地覆盖、启动时全量重扫）；
  完整值只存在 GUI 进程内存里。CLI 的 `dsh-tm.py url` 只打印裸地址。
- **删除**：只删「槽根目录之内」的条目；备份不完整（缺文件 / 拷不动）就**中止**，一个字节都不删；
  备份是 junction 时不跟随。
- **移出管理**：只改 `config.json`，磁盘不动。
- **杀进程**：只认「该槽 `bin.js` 绝对路径 + 端口」；端口被非 dsh 程序占用时**拒绝杀**。
- **配置写入**：原子写 + 改前备份；解析失败不静默覆盖，会备份成 `.bad-<时间戳>` 并弹框。
- **不联网**：除「拉版本列表」「安装 dsh」两处（都走 npm registry，且由你主动触发）外全部离线，无遥测。

### 日志里有什么（附日志前请自查）

`?token=...` 已打码，但**本机路径和用户名照常保留**（排查需要）。附之前建议把
`C:\Users\<你的用户名>` 这类替换掉。另外 `dsh备份\` 目录里有 `.credentials.yaml` 的明文副本，
**不要**把那个目录一起打包发出来。

### 支持范围

只对**最新发布版本**和 `main` 分支的最新提交提供修复。旧版本的问题请先升级再复现。

### 响应

这是个人维护的开源项目，没有 SLA。确认的问题会尽快在 `CHANGELOG.md` 里记录修复版本；
严重问题（凭据泄漏 / 数据丢失）优先处理。
