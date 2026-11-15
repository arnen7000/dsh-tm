# Contributing / 参与开发

> **English first, Chinese follows.** 英文在前、中文紧随。

---

## English

A small Windows desktop tool (Tkinter + pure Python), with no third-party dependencies.
Before changing anything, read the code map below and
[`docs/工程经验总结.md`](docs/工程经验总结.md) (why some parts look "roundabout").

### Environment

| Need | Version | Notes |
|---|---|---|
| Windows | 10 / 11 | Windows APIs only (processes, ports, junctions); no cross-platform plan |
| Python | 3.10+, **with tkinter** | Use system Python for development; releases use `runtime\python` (3.10, with tkinter) |
| Node | >= 22.19 | Only needed to install a new slot; tests never need a real node |

First run generates `config.json` from `config/config.example.json`
(**this file is not tracked**).

```powershell
python dsh_manager.pyw            # with console (see tracebacks)
pythonw dsh_manager.pyw           # no console (same as release; exceptions are swallowed)
```

### Code map

| File | Responsibility |
|---|---|
| `dsh_manager.pyw` | Main UI (**module-level side effects — never import it**). Pure logic goes into the modules below so it can be covered by offline regressions |
| `dsh_install_ops.py` | Install an official build: npm registry probe / mirror fallback / slot registration / bat generation / rollback on failure |
| `dsh_adopt.py` | Scan existing installs, health check, copy-and-adopt, junction handling |
| `dsh_uninstall_ops.py` | Uninstall / detach / re-attach / repair / deletion guards / backup verification |
| `dsh_move.py` | Move the whole install root |
| `dsh_health.py` | Slot health check (read-only diagnosis) |
| `dsh_drift.py` | Plugin and config drift detection (never touches credentials or sessions) |
| `dsh_ui_prefs.py` | Display preferences (pure logic) |
| `dsh_kill.py` | Kill processes precisely by "signature + port" |
| `dsh_registry.py` | Fetch the npm version list |
| `dsh_i18n.py` | i18n layer: **the Chinese source string is the key**, English comes from `lang/en.json`, missing entries fall back to Chinese; `retranslate()` switches in place |
| `dsh-tm.py` | CLI |
| `lang/en.json` | English table (`Chinese source -> English`); a missing entry falls back to Chinese, never blank |
| `scripts/i18n-wrap.py` | AST tool that wraps UI strings in `T("...")` (whitelist-based, safe to re-run) |
| `scripts/make-portable.py` | Build the unzip-and-run release zip (source + trimmed runtime) |

### Six hard rules

1. **`.bat` must be GBK + CRLF + no BOM.** UTF-8 splits adjacent Chinese `REM` lines;
   cmd returns 255 and the window flashes and closes. Write bats only through
   `_write_bat` / `dsh_install_ops._gen_slot_bats`; `scripts/test-bat-text.py` guards this.
2. **Slot paths always use placeholders** (`<base>` / `<home>` / `<self>` /
   `<npm_global>`): `portable_path()` to write, `_expand_slot_path()` to read.
   **`.bat` is read by cmd.exe, so paths must be expanded first.**
3. **Every deletion needs a guard**: only entries inside the slot root; abort on an
   incomplete backup; never follow a junction into its target. When changing this,
   also run `scripts/test-slot-ops.py`.
4. **Never swallow exceptions silently.** Log the error and show it to the user;
   `.pyw` has no console, so `except: pass` hides the problem.
5. **Do not commit** `config.json`, `*.log`, `dsh备份/`, `runtime/`, `dsh-versions/`.
   `.gitignore` covers them; the commit hook checks again.
6. **Wrap new UI strings in `T()` and add a row to `lang/en.json`.** Run
   `python scripts/i18n-wrap.py` to wrap automatically (AST whitelist, re-runnable).
   **Never wrap logic values**: log-level keywords (`失败`/`错误`/`异常`), the `dsh备份`
   directory name, enum values such as `纳入`/`运行中`, or the browser picker's
   `选择其他程序…`. Translating those is a bug that never raises — a condition just
   stops being true.

### Running tests

```powershell
# Full pre-release gate: 6 static checks + 16 offline regressions (~40 s)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check-all.ps1

# Static checks only (fast, for frequent edits)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check-all.ps1 -Fast

# One regression on its own
python scripts\test-manager-logic.py
```

The commit hook (`.githooks/pre-commit`) is off by default; enable it once after cloning:

```powershell
git config core.hooksPath .githooks
```

It runs only **in-memory, side-effect-free** checks (syntax + 4 regressions + username /
token / sensitive-file scans); the full gate still needs a manual `check-all.ps1` run.

> Regressions create temp directories under `temp\` and remove them on exit;
> `git status` should be clean afterwards.

### Commits and PRs

- Commit message: **English first, Chinese follows** (so both audiences can read it),
  first line <= 72 characters:

  ```
  Fix numbering so a detached slot keeps its name

  移出的槽目录仍占着自己的编号，新装不再产生 dshV3-2。
  ```

- One PR does one thing; for behaviour changes also update `CHANGELOG.md` and the
  relevant docs.
- In the PR description, state what changed, why, and how you verified it (which
  regressions you ran).
- CI (`.github/workflows/ci.yml`) runs the full gate on `windows-latest`; if it goes
  red, reproduce locally first.

### Multiple machines

- Moving to another machine is just `git clone` + Python with tkinter; `config.json`
  is generated, never copied.
- **Do not** sync `config.json` or `dsh备份` between machines — they contain
  machine-specific absolute paths and plaintext credentials.
- Before a release: edit `VERSION` → write `CHANGELOG.md` → commit → tag (`v` prefix)
  → run `check-all.ps1` → **then** build the portable zip (the builder refuses to run
  if `HEAD:VERSION` differs from the working copy) → smoke-test a real install.

---

## 中文

这是一个 Windows 桌面小工具（Tkinter + 纯 Python），没有第三方依赖。改动之前请先读
下面的代码地图和
[`docs/工程经验总结.md`](docs/工程经验总结.md)（为什么有些地方看起来"绕"）。

### 环境

| 需要 | 版本 | 说明 |
|---|---|---|
| Windows | 10 / 11 | 只用 Windows API（进程、端口、junction），没有跨平台计划 |
| Python | 3.10+，**必须带 tkinter** | 开发时可直接用系统 Python；发布包里用 `runtime\python`（3.10，含 tkinter） |
| Node | ≥ 22.19 | 只在"装一个新槽"时需要；测试用不到真实 node |

首次运行会从 `config/config.example.json` 生成 `config.json`（**这个文件不入库**）。

```powershell
python dsh_manager.pyw            # 直接跑（有控制台看 traceback）
pythonw dsh_manager.pyw           # 无控制台（与发布版一致，异常会被吞，排障时别用）
```

### 代码地图

| 文件 | 职责 |
|---|---|
| `dsh_manager.pyw` | 主界面（**有模块级副作用，不可 import**）。纯逻辑一律拆到下面几个模块，才能进离线回归 |
| `dsh_install_ops.py` | 装官方版：npm 源预检 / 换镜像 / 登记槽 / 生成 bat / 失败收回 |
| `dsh_adopt.py` | 扫描已有安装、体检、复制纳入、junction 处理 |
| `dsh_uninstall_ops.py` | 卸载 / 移出 / 接回 / 修复 / 删除护栏 / 备份与核对 |
| `dsh_move.py` | 整体搬迁安装根 |
| `dsh_health.py` | 槽体检（只读诊断） |
| `dsh_drift.py` | 插件与配置变更检测（不碰凭据 / 会话） |
| `dsh_ui_prefs.py` | 界面显示偏好（纯逻辑） |
| `dsh_kill.py` | 按「签名 + 端口」精准清理进程 |
| `dsh_registry.py` | 拉 npm 版本列表 |
| `dsh_i18n.py` | 中英翻译层：**中文原文就是 key**，英文查 `lang/en.json`，查不到原样返回中文；`retranslate()` 负责就地切换 |
| `dsh-tm.py` | CLI |
| `lang/en.json` | 英文文案表（`中文原文 → English`），缺一条就回退中文，不会空白 |
| `scripts/i18n-wrap.py` | 把源码里的 UI 文案包成 `T("...")` 的 AST 工具（白名单，可重复执行） |
| `scripts/make-portable.py` | 打「解压即用」发布包（源码 + 精简 runtime） |

### 六条硬约束（改代码前先记住）

1. **`.bat` 必须是 GBK + CRLF + 无 BOM**。UTF-8 会让相邻中文 `REM` 撕裂，cmd 报 255、双击闪退。
   写 bat 只能走 `_write_bat` / `dsh_install_ops._gen_slot_bats`，回归 `scripts/test-bat-text.py` 守着。
2. **槽路径一律用占位符**（`<base>` / `<home>` / `<self>` / `<npm_global>`）：写用 `portable_path()`，
   读用 `_expand_slot_path()`。**`.bat` 给 cmd 读，必须先展开**。
3. **删除类操作必须有护栏**：只删槽根之内的条目；备份不完整就中止；junction 不跟随、不递归删目标。
   改这部分请同时跑 `scripts/test-slot-ops.py`。
4. **不要静默吞异常**。该报的错写进日志并让用户看见；`.pyw` 无控制台，`except: pass` 等于把问题藏起来。
5. **不要提交** `config.json`、`*.log`、`dsh备份/`、`runtime/`、`dsh-versions/`。
   `.gitignore` 已覆盖，提交钩子会再拦一次。
6. **新增的界面文案要包 `T()`，并在 `lang/en.json` 补一条翻译**。用
   `python scripts/i18n-wrap.py` 自动包（AST 白名单，可重复执行）。
   **逻辑值绝不包**：日志分级关键词（`失败`/`错误`/`异常`）、`dsh备份` 目录名、
   `纳入`/`运行中` 等枚举值、下拉里的 `选择其他程序…` —— 翻了就是 bug，
   而且这类 bug 不会报错，只会让判断永远不成立。

### 跑测试

```powershell
# 提交前总检：6 项静态检查 + 16 套离线回归（约 40 秒）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check-all.ps1

# 只跑静态检查（快，适合频繁改）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check-all.ps1 -Fast

# 单跑某一套
python scripts\test-manager-logic.py
```

提交钩子（`.githooks/pre-commit`）默认不启用，clone 后手动开一次：

```powershell
git config core.hooksPath .githooks
```

它只跑**纯内存、零副作用**的检查（语法 + 4 套回归 + 用户名 / token / 敏感文件扫描），
全量门禁仍需手动跑 `check-all.ps1`。

> 离线回归会在 `temp\` 下建临时目录并在结束时删掉；跑完 `git status` 应当是干净的。

### 提交与 PR

- 提交信息：**英文在前、中文紧随**（方便中英读者都能看懂），首行 ≤ 72 字符：

  ```
  Fix numbering so a detached slot keeps its name

  移出的槽目录仍占着自己的编号，新装不再产生 dshV3-2。
  ```

- 一个 PR 只做一件事；行为变更请同时更新 `CHANGELOG.md` 与相关文档。
- PR 描述里写清：改了什么、为什么、怎么验证的（跑了哪几套回归）。
- CI（`.github/workflows/ci.yml`）会在 windows-latest 上跑全量总检；红了先本地复现。

### 多机开发

- 换机器只要 `git clone` + 装 Python（带 tkinter）；`config.json` 会自动生成，不需要从别处拷。
- **不要**把 `config.json` 或 `dsh备份` 在机器之间同步 —— 里面是本机绝对路径和明文凭据。
- 发布前：改 `VERSION` → 写 `CHANGELOG.md` → **提交** → 打 tag（`v` 前缀）→ 跑 `check-all.ps1`
  → **再**打便携包（打包脚本会拒绝在 `HEAD:VERSION` 与工作区不一致时出包）→ 实测一次真实安装。
