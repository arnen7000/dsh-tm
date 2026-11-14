# 参与开发

这是一个 Windows 桌面小工具（Tkinter + 纯 Python），没有第三方依赖。改动之前请先读
下面的代码地图和
[`docs/工程经验总结.md`](docs/工程经验总结.md)（为什么有些地方看起来"绕"）。

## 环境

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

## 代码地图

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

## 六条硬约束（改代码前先记住）

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

## 跑测试

```powershell
# 提交前总检：6 项静态检查 + 14 套离线回归（约 40 秒）
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

它只跑**纯内存、零副作用**的检查（语法 + 3 套回归 + 用户名 / token / 敏感文件扫描），
全量门禁仍需手动跑 `check-all.ps1`。

> 离线回归会在 `temp\` 下建临时目录并在结束时删掉；跑完 `git status` 应当是干净的。

## 提交与 PR

- 提交信息：**英文在前、中文紧随**（方便中英读者都能看懂），首行 ≤ 72 字符：

  ```
  Fix numbering so a detached slot keeps its name

  移出的槽目录仍占着自己的编号，新装不再产生 dshV3-2。
  ```

- 一个 PR 只做一件事；行为变更请同时更新 `CHANGELOG.md` 与相关文档。
- PR 描述里写清：改了什么、为什么、怎么验证的（跑了哪几套回归）。
- CI（`.github/workflows/ci.yml`）会在 windows-latest 上跑全量总检；红了先本地复现。

## 多机开发

- 换机器只要 `git clone` + 装 Python（带 tkinter）；`config.json` 会自动生成，不需要从别处拷。
- **不要**把 `config.json` 或 `dsh备份` 在机器之间同步 —— 里面是本机绝对路径和明文凭据。
- 发布前：改 `VERSION` → 写 `CHANGELOG.md` → 打 tag（`v` 前缀）→ 跑一遍 `check-all.ps1` → 实测一次真实安装。
