# 更新记录

版本号约定：`主版本.次版本.修订号`，**唯一事实来源是仓库根的 [`VERSION`](VERSION) 文件**
（界面标题栏和 `dsh-tm.py --version` 都读它）。

- 现在是 **`0.x`**：接口还可能变 —— 改 `config.json` 结构、改 CLI 参数这类破坏性改动，升次版本（`0.1` → `0.2`）；
  新增功能升修订号；修 bug 也升修订号（`0.1.0` → `0.1.1`）。等接口稳定了再发 `1.0.0`。
- Git tag 带 `v` 前缀（`v0.1.0`），`VERSION` 文件里不带。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

---

## [待发布]

## [0.1.14]

- 环境自检在后台运行。零槽启动与「检查运行环境」同时触发时共用一次检查，窗口保持响应，完整报告写完后才替换上一份。
- 补验同一 cookie 容器内的双槽登录，并更正认证注释。
- [验收记录](docs/test-coverage-v0.1.14.md)列出自检触发方式、失败恢复和实测环境。

## [0.1.13]

- macOS 安装遇到疑似过时的 npm 版本缓存后向当前源重新核对一次。真实 npm 回归复现了旧依赖元数据的 `ETARGET`，在线重试成功后保持真实缺失版本的错误提示。
- 独立启动器回归识别项目内的临时夹具路径，并继续检查硬编码的仓库依赖。
- 源码自检用语法树识别实际类型注解，避免 npm 正则误报 Python 兼容性问题。
- 整理跨平台使用手册，记录架构、数据和进程安全、性能设计、各类用户的验收路径及发布口径。

- 扩充真实槽位的卸载备份验收、原生 Tk 安装队列和纳入目录冲突回归。记录 v0.1.12 的测试覆盖边界，并更正卸载说明。
- 2026-11-25 macOS 验收补测。零槽自检报告正常状态并给出完整报告位置。安装成功和下载进度使用相应级别显示，卸载中显示准确的槽状态。验收记录和适用范围见 [v0.1.12 测试覆盖复核](docs/test-coverage-v0.1.12.md)。
- GitHub 发布说明列出同一发布运行验证的系统、架构和场景，单列最低部署目标与待实机验收的环境。

## [0.1.12]

- macOS 安装和维护进度窗使用系统标题栏，跳过 Windows 任务栏设置。
- 安装进度可主动转入后台，并从主界面重新打开。已装槽可启停和打开浏览器，改变槽位配置的操作在安装期间暂缓。安装结束时刷新槽卡片并报告结果。
- macOS 版本预检和安装限制单次 npm 请求的等待时间，优先使用缓存。默认源超时后尝试一次镜像，终止超时安装的子进程并在失败时清理暂存目录。安装完成只通知一次。

## [0.1.11]

### 新增

- macOS 支持 Apple Silicon 和 Intel。两平台共用六槽 GUI、设置和 CLI JSON v1。macOS 提供原生 App 和槽位 `.command` 入口，源码和 App 共用配置目录。

### 改进

- Mac 应用与主界面品牌统一为 DSH-TM，中文窗口标题统一为「DSH 管理器」。

### 修复

- 修复 macOS 完成卸载后进度窗仍锁住界面的问题。后台进度和完成通知由主线程处理，系统终止管理器时走正常退出路径。
- 打开浏览器时直接使用本次启动取得的认证链接。上一次运行保留下来的服务若要求认证，说明如何继续访问。批量启动只在实际发生端口冲突时显示端口提示。

- macOS 设置窗跟随主窗移动和拉高，拖离一小段时松手吸回。设置页支持触控板滚动并保留可拖动的滚动条。启动日志汇总槽位诊断，详情可在槽体检查看。

- 启停前核实监听进程的槽位身份，防止误报运行状态或停止无关进程。启动超时会明确提示，认证入口仅在当前管理器内存中保留。
- 卸载先停机并校验备份。搬迁校验后才提交配置，保留链接并只更新受影响的启动器。
- 修正 Mac App 的环境自检，检查包内模块以及当前指定的配置文件。

## [0.1.10] — 2026-11-19

### 修复

- **纳入已有安装**可选盘符、共同上层目录或精确安装目录。后台扫描可取消，跳过重解析点和依赖目录。同一版本安装在多个目录时可以分别选择。
- **纳入便携安装**优先复制源安装自带的 Node 和所需 DLL，识别相邻的同名 `-home` 数据目录，并为每份数据创建独立备份。
- **安装与体检**优先为新槽保留独立 Node。依赖系统 PATH Node 的旧槽可以停机后执行体检修复。修复会备份配置并保留已有启动脚本。

## [0.1.9] — 2026-11-16

### 修复

- **槽体检修复**默认选中第一个可修复槽。直接点击「修复」即可打开确认框。
- **接回已移出的槽**时，保留原端口的槽只补缺失的启动脚本。分配新端口的槽只重建本槽的脚本，并备份已有的同名文件。批量接回会按每个槽自己的端口状态处理。

## [0.1.8] — 2026-11-15

继续「**地基优先**」：补上用户实测到的「点修复没修好」，并修掉两处
「界面语言一变、判定就失效」的问题。没有新增 CLI 能力，接口契约不变。

### 新增

- **「修复」现在会补回缺失的三件 bat**：`dsh.bat` / `dsh-cli.bat` /
  `kill-<port>.bat`。确认框先列清这次要生成哪几件；实现是「单槽 + 只补缺失」
  （`_rebuild_bats(keys=…, only_missing=True)`），**已存在、或你手工改过的 bat
  一律不碰**，其它槽也不动；没配端口时不会凭空生成 `kill-None.bat`。
- 升级指引补进文档：`README.md` §4.12b、`README.en.md`、`docs/dsh管理器使用说明.md`
  §3、`docs/user-guide.en.md` 与 FAQ Q19e。核心是「新 zip 解压覆盖原目录」——
  `config.json` 与槽都不在包里；**先删旧目录再解压**才会丢配置。

### 修复（均为实跑复现）

- **删掉 bat 后点「修复」只弹「无需改动」**：`repair_slot()` 本来就只管
  `node` 运行时与 `.dsh-home` / `.tmp`（docstring 写明不生成 bat），而缺 bat
  只报「警告」→ 槽进得了修复下拉，点完却什么都不做。现在 `repair_slot`
  成功后由管理器补回缺失的 bat；部分失败会明确报「修复未完成」，不再假报成功。
- **英文界面下安装 / 纳入的形状复核失效**：`slot_shape_problems()` 返回的级别是
  `T("错误")`（英文下为 `"error"`），而这两条流程拿中文字面量比较 → hard 项全被
  当成提示，坏槽也报成功。新增语言无关的 `is_hard()`，四处比较统一走它；
  **返回值本身不变**（GUI 日志、CLI `doctor`、回归断言都依赖它）。
- **英文界面下 `health`（人读版）有错误却退出 0**：同一个中文字面量问题，
  `bad` 恒为 0。改用已有的 `_lv_en()` 归一后，中英 × 人读/JSON 四种组合退出码一致。

### 测试

- `scripts/test-manager-logic.py` 新增 C3z28a–g：`_rebuild_bats` 默认行为不变、
  只补缺失、不产生备份、失败不假报成功、确认框列明文件。
- `scripts/test-install-ops.py` 新增 F1–F6：`is_hard()` 在中英两种界面下都识别
  错误级，且 `slot_shape_problems()` 返回值仍是本地化字符串。
- `scripts/test-slot-ops.py` 新增 I44b：`repair_slot` 仍不写 bat（职责边界不变）。
- `scripts/test-cli-boundary.py` 新增 E1–E3：`health` 中英 × 人读/JSON 四种组合
  退出码一致（35 → 41 项）。
- 18 套离线回归、`check-all.ps1 -Fast` 6 项静态检查全绿；`lang/en.json` 1102 条。

## [0.1.7] — 2026-11-15

继续按「**地基优先**」推进：动手做扩展功能之前先跑基础层审计，确认的真问题
先修并补回归，再做 CLI 读写分界硬标记。

### 新增

- **CLI 读写分界从文档变成代码事实**：`dsh-tm.py` 新增 `CMD_CLASS` 唯一分类表
  （`list` / `status` / `url` / `health` / `doctor` = `read`；
  `start` / `stop` / `kill-stray` = `runtime_write`）。`main()` 每次分发前过
  `_gate_registry()`：命令漏分类、分类表里有旧命令、或出现 `disk_write` 都当场失败。
  **CLI 永不提供改磁盘命令**。
- 每个 `--json` 结果顶层新增 `effect`（取值就是分类）；写命令一律带 `changed[]`
  （含用法错误 / 配置错误路径），只读命令不带。加字段不升 `schema_version`（仍为 1）。
- 给了未登记的槽名时新增 `unknown_slots[]`，四个只读命令统一走 `_selected_keys()`。
- 第 18 套离线回归 `scripts/test-cli-boundary.py`（35 项）：分类表 ↔ handler 表 ↔
  parser 子命令 ↔ `docs/agent-api.md` 英中表格逐项对账，实跑八个命令的
  `effect` / `changed[]`，并守住下面四个地基修复。

### 修复（地基审计确认，均为实跑复现）

- **`health --json` 在 `dsh_health` 加载失败时 stdout 是 0 字节**，违反「一次调用
  一个 JSON 对象」。现在输出 `ok: false` + `errors[].code = module_unavailable`。
- **`health --json` 把「只有提示条目」误报成 `warning`**：`level` 现在只按
  `error` / `warning` 计，仅 `info` 的槽是 `ok`；枚举集合不变，不升契约版本。
- **显式 `--config <不存在的路径>` 会静默生成一份零槽模板并回 `ok:true, count:0`**：
  路径写错被伪装成「这台机器没装版本」，盘上还凭空多出文件。现在只有**默认**
  `config.json` 缺失才从模板 bootstrap，显式路径缺失直接 `config_unreadable` /
  退出 3、不写盘。
- **未知槽处理不一致**：以前 `status v9 --json` 返回 0 槽、`url v9 --json`
  返回全部槽。现在统一给 `unknown_slots[]`，stdout 干净。

### 文档

- `docs/agent-api.md`（英文在前中文紧随）补 `effect` / `changed[]` /
  `unknown_slots[]`、三分类与 `CMD_CLASS` 的关系、配置 bootstrap 边界、
  `module_unavailable` 说明；`README.md` / `README.en.md`、两份使用说明、
  `常见问题.md`（新增 Q19c）、`CONTRIBUTING.md`、PR 模板、
  `工程经验总结.md`、路线图同步。
- 离线回归 17 → 18 套；`scripts/test-cli.py` 56 项、`test-config-guard.py`
  43 项保持不变且全绿。

---

## [0.1.6] — 2026-11-15

这一版按「**地基优先**」推进：先把审计确认的基础问题修掉、补上回归，再动扩展功能。

### 新增

- **`doctor --json`**：一次只读调用合成「配置校验 + 槽体检 + 槽形状复核」。
  每条问题带固定英文 `code` / `source` / `action`，逐槽给 `level` / `issues[]` /
  `actions[]`（`fix_config` / `reinstall_slot` / `regenerate_bats` /
  `create_data_dir` / `review_manually`），顶层给 `summary` / `config_valid` /
  `config_issues` / `global_issues`。`health --json` 只有随语言变的自由文本，
  agent 要分类就走 `doctor`。不写盘、不启停进程、不修文件。
- **`dsh_config_guard.py`**：配置层的**唯一判据**，GUI / CLI / `doctor` 共用。
  `dsh_manager.pyw` 的 `_check_port_consistency()` 改为薄转发，不再另写一份。
- 所有 `--json` 结果新增 `config_problems[]`（加字段不升 schema_version）。
  `docs/agent-api.md`（中英双语）补 doctor 字段表、动作码、配置错误码与示例；
  两个 README、两份使用说明、路线图同步。
- 第 17 套离线回归 `scripts/test-config-guard.py`（43 项）：纯函数判据 + 三处入口
  共用判据的静态断言 + CLI 实跑（假配置，不启动也不杀任何进程）。

### 修复（地基）

- **损坏配置被伪装成零槽**：CLI 的 `_cfg_or_die()` 以前把类型写坏的 `versions`
  静默改成 `{}`，agent 会据此判断「这台机器没装过任何版本」。现在阻断级问题
  （`versions` 缺失/类型错、槽不是对象、槽数超上限）结构化报错并退出 3。
- **`--json` 遇坏配置 stdout 是空的**：以前只往 stderr 写一行就 `exit(3)`，
  破坏了「一次调用 stdout 只有一个 JSON 对象」的契约。现在两种模式都给结论，
  `ok: false` / `config_valid: false` / 固定英文 `code`。
- **CLI 没有 GUI 的端口双源校验**：`start --json` / `list --json` 正是用 `port`
  判断 `state` / `listening`，`port` 与 `extra_args --port` 不一致时会稳定误报。
  现在读命令照常出报告但列出 `config_problems`，`start` / `stop` 拒绝受影响的槽
  （`config_port_invalid`），不会拿错端口去启停进程。
- GUI 端口校验老实现用 `cfg["extra_args"]` 硬取键，手改配置缺字段会 KeyError；
  改用共享模块后给可读错误，并顺带支持 `--port=4080` 写法。
- **回归假红**：`test-cli.py` 的 D10/D11 断言中文报错文案，却让语言跟着**本机**
  `config.json` 的 `ui.lang` 走 —— 用户把界面切成英文后本地必红，而 CI 用零槽模板
  （无 `ui.lang`）永远绿。现在这两条显式钉成 zh，新增回归的两条文案断言改成
  中英都认。

### 变更

- `scripts/test-cli.py` 53 → 56 项：八个命令的 JSON 契约、doctor 字段、
  `config_problems` 全覆盖。
- 离线回归 16 → 17 套；`README.md` / `CONTRIBUTING.md` 里的旧套数一并更正。

---

## [0.1.5] — 2026-11-15

### 新增

- **Agent 接口契约**：`--json` 覆盖到写操作 `start` / `stop` / `kill-stray`，
  之前只有 4 个只读命令有。现在所有 7 个命令都返回同一个外层形状
  （`schema_version` / `command` / `ok` / `count` / `slots` / `errors`），
  写操作额外给 `changed[]`、逐槽 `state` / `listening` 与固定英文 `error_code`。
  一次调用 `stdout` 只有一个 JSON 对象；字段名与枚举值不随 `--lang` 变。
- **[`docs/agent-api.md`](docs/agent-api.md)**（入库，中英双语）：读写分界、
  逐命令字段表、错误码、退出码、示例、契约版本规则。改磁盘的操作
  （安装 / 纳入 / 卸载）**不在 CLI 里**，脚本不可能误卸载。
- 写操作不给槽、也不给 `--all` 时返回 `no_target`（退出码 2），不会误启动全部槽；
  `kill-stray --json` 不加 `--yes` 是结构化 dry-run（`executed: false`）。

### 修复

- **`url` / `health` 不带 `--json` 直接崩**：这两个函数的非 JSON 分支用了只在
  JSON 分支里赋值的 `keys` → `UnboundLocalError`（0.1.4 就有）。现在两个分支
  共用 `_selected_keys()`，并新增实跑回归守着。
- JSON 模式下等端口的 WARN 不再打进 `stdout`（原来会破坏「单个 JSON」契约）。

### 变更

- `scripts/test-cli.py` 从 42 项扩到 53 项：新增 E9–E11 静态断言与 F 段实跑
  （假配置，不启动也不杀任何进程），覆盖 7 个命令的 JSON 契约与两个分支 bug。
- 两个 README 的 CLI 章节与文档索引接上 `agent-api.md`；路线图里
  「CLI 还没有 `--json`」等过时描述已更正。

---

## [0.1.4] — 2026-11-14

### 修复

- **切换语言后首页空白（零槽状态）**：`_pick_lang` 只重画了卡片、漏了引导卡 ——
  零槽时 `_build_cards()` 直接 return（不画任何卡片），引导卡又没人画，界面就空了。
  `_reload_config_after_change` 里是三个一起调的，切语言这条路漏了一个。
- **扫描不到装在别处的 dsh**：自动扫描只看「版本根 + npm 全局 node_modules」两处，
  而且一条日志都没有。现在：扫描范围写进日志（含 npm 未探测到的单独提示）；
  没扫到时给「手动指定目录…」出口，自动识别你选的是 `node_modules`、它的父目录、
  `@deepseek-ai` 还是包目录本身。
- **提示里的 Windows 路径被吃掉**：`\node_modules` 在 Python 字符串里会被解析成
  「换行 + ode_modules」，用户看到的是错乱路径。
- **发布包里的版本号错成 `0.1.3`**（发布后审计发现）：`make-portable.py` 的文件名与
  引导文件读**工作区** `VERSION`，源码却用 `git archive HEAD` 导出**已提交快照** ——
  工作区改成 0.1.4 还没提交就打包，于是包叫 0.1.4、包内 `VERSION` / `CHANGELOG`
  还是 0.1.3，用户解压后界面标题栏显示旧版本。现在打包前 `preflight()` 要求
  `HEAD:VERSION` 与工作区一致且 `CHANGELOG` 有对应条目，出包后 `verify_zip()`
  再核对三处版本号；新增 `scripts/test-portable.py` 进全量门禁。修复后重新生成发布包。

### 变更

- **README 第一屏加下载入口**：之前新用户要滚到「2.1 两种获取方式」才知道在哪下。
  现在标题下面直接给 Releases 链接和「解压 → 双击」三步。
- **使用说明补「建桌面图标」一节**：之前 `docs/dsh管理器使用说明.md` **完全没提**
  `build_lnk.py`（README 只有快速开始末尾一句）。现在两种办法都写了，并说明
  便携包用户要用 `runtime\python\python.exe build_lnk.py`。
- `user-guide.en.md` 同步桌面快捷方式一节。
- **公开文档补齐双语**：`SECURITY.md` / `CONTRIBUTING.md` 之前只有中文，英文 README
  却标注为双语；便携包引导文件 `README-FIRST.txt` 也改成「英文在前、中文紧随」。
- **全量门禁补齐两套**：`test-log-mask.py`（原来只在 pre-commit 里跑）与
  `test-portable.py`（发布包一致性），现共 16 套；英文 README 的 `check-env`
  命令改用包内 Python，便携包用户不再需要系统 Python。

---

## [0.1.3] — 2026-11-14

### 新增

- **便携包**（本次最重要的变化）：`scripts/make-portable.py` 把源码 + 精简过的
  runtime 打成 `dsh-tm-<版本>-portable.zip`（约 53 MB），作为 Release 附件发布。
  **下载解压双击就能用，不需要装 Python 或 Node。**
  精简掉 62 MB 的 `.pdb` 调试符号，以及 `include/` `libs/` `Lib/test` `Lib/idlelib`
  这些运行时用不到的东西（233 MB → 156 MB 解压后，53 MB 压缩后）。
- **CLI `--json`**：只读命令（`list` / `status` / `url` / `health`）输出固定 schema，
  字段名与枚举值固定英文、不随界面语言漂移，供另一个 agent 驱动。
- 便携包里带 `使用前先读我.txt`：解压后第一眼就能看到「三步走」。

### 变更

- README「2.1 两种获取方式」把**便携包提到首位**（原来和源码并列），
  并说明它由 `make-portable.py` 生成
- 「快速开始」第 1 步补上「从源码跑需要 Python ≥3.8 带 tkinter + Node ≥22.19」
- `.gitignore` 加 `dist/`

---

## [0.1.2] — 2026-11-14

### 新增

- **英文版 README**（`README.en.md`）：完整英文说明，与中文版顶部互加语言切换。
  英文读者现在有 README + `docs/user-guide.en.md` 两份入口。
- README 新增「界面」章节：主界面与设置侧栏两张实拍截图（`docs/images/`）。
- 第 15 套离线回归 `scripts/test-i18n.py`：查每个 `T()` 是否都有译文、
  有没有把换行写成字面 `/n`、有没有空译文 —— 它第一次跑就抓出 59 条漏翻。

### 变更

- **文档精简**：移除 `docs/架构与维护说明.md` 与 `docs/发布与更新流程.md`
  （本地保留，不随仓库发布）。改代码看 `CONTRIBUTING.md` 的代码地图与硬约束；
  发版按 `VERSION` → `CHANGELOG` → tag → `check-all.ps1` → 实测走一遍。
- 文档索引按受众分三组：**终端用户 / 贡献者 / 维护者**，并注明
  「只想装来用的话，看第一组就够」。

### 修复

- **CLI 的 59 条文案补上翻译**（`dsh-tm.py`）：`--help`、`list` 表头、各种输出。
  它此前从未被覆盖率检查统计到 —— 检查脚本用 `dsh_*.py` 匹配，而文件名带连字符。
- CLI 支持 `--lang zh|en`；不给时跟随 `config.json` 的 `ui.lang`。

---

## [0.1.1] — 2026-11-14

### 新增

- **中英双语界面**：设置 → 界面 → 「界面语言」可切 中文 / English，存 `config.json` 的 `ui.lang`，
  缺省 `zh`（老配置无感）。**切换就地生效，不需要重启** —— 见下面的 `retranslate`。
- 翻译层 `dsh_i18n.py`：**中文原文就是 key**，英文查 `lang/en.json`，查不到原样返回中文。
  这样翻译可以分批补，任何时刻界面都不会出现空白、`None` 或 key 名。
- **918 条运行时文案 100% 覆盖**：按钮、标签、对话框正文、状态行、**全部日志**、
  错误消息、安装/卸载/移动/纳入的每一步提示、体检与关于窗。
  逻辑模块（`dsh_install_ops` / `dsh_uninstall_ops` / `dsh_adopt` / `dsh_move` /
  `dsh_kill` / `dsh_health` / `dsh_drift`）里的 `return` 消息与内部日志也在内。
- `I18N.retranslate()`：遍历控件树，把当前 text **反查回中文 key** 再按新语言翻译，
  zh→en、en→zh 双向就地切；卡片状态行是 `textvariable`，重建一次卡片让它跟上。
- **英文版使用说明** `docs/user-guide.en.md`：英文模式下点「使用说明」打开它，
  缺失时退回中文版。
- 工具 `scripts/i18n-wrap.py`：AST 白名单，只包 `text=` / `title=` / `prompt=` / `log()` /
  `messagebox.*` / `xxx.set()` / `p.on_step()` 这些**确定只用于显示**的位置。

### 修复

- 英文界面下按钮被截断：卡片按钮（240px 里只有约 105px）改成 `Copy URL` / `Program dir` /
  `Data dir`；设置窗的 `width=8/10/11` 是按中文字数写死的，直接去掉让 Tk 自己算。
- 自定义按钮函数 `_obtn` / `_mbtn` 里的文案以**位置参数**传入，AST 白名单抓不到，
  改为在函数内部 `T(text)`。
- `i18n` 的 import 顺序：tkinter 缺失时的提示语本身要用 `T()`，原来排在普通 import 区
  会 `NameError: name 'T' is not defined`（被 `test-tk-selfheal` 抓到）。
- `_apply_palette` 现在同步重绑 `_APP_BG` / `_CARD_BG` / `_CARD_BD` —— 此前深色主题下
  进度窗会冒出一块浅色。

### 为什么 i18n 用「原文当 key」而不是 `ui.install.title` 这种键

一千多条文案，起名 + 维护映射表的工作量远超收益，而且很容易起错、两处不同步。
用原文当 key 时源码本身就是可读的，改文案即改 key，不会出现「表里有、代码里没有」。
代价是同一句话在多处出现时只能一起翻 —— 对本项目够用。

---

## [0.1.0] — 2026-11-14

首个公开发布版本。一个配置驱动的 Windows 桌面工具，用来**同时管理多份互不干扰的
DeepSeek Harness（dsh）**：每个版本一个独立槽，自带 node、独立 `DSH_HOME`、独立 TEMP、独立端口。

### 主要能力

- **槽管理**：最多 6 个槽；卡片式启停 / 打开浏览器 / 复制入口 URL / 打开程序或数据目录；
  标题栏「全部启动 / 全部停止」；卡片拖拽排序、可隐藏显示。
- **安装**：从 npm registry 拉版本列表（标注最新 / 预览 / 已弃用），一次可装多个；
  失败当场收回半成品。
- **纳入**：扫描本机已有的 dsh（版本根 + npm 全局），两条路 —— 版本根下**原地接回**
  （只改配置、不复制），别处**复制**一份进版本根（不跟随 junction）。
- **卸载 / 移出**：卸载先备份用户数据并逐文件核对，对不上就中止；移出只改配置，磁盘一个字节不动。
- **搬迁**：整体换盘 / 换目录，复制 → 校验 → 改配置 → 删旧位置；事前算目标盘空间，运行中的槽拒绝搬。
- **槽体检**：逐项对比"现在"与"登记"，分错误 / 警告 / 提示三级；**默认只报告**，
  要修得自己点「修复…」（只补 node 运行时与 `.dsh-home`/`.tmp` 空目录，不删不覆盖）。
- **CLI**：`dsh-tm.py list/status/url/health/start/stop/kill-stray`，与 GUI 共用同一份配置。

### 本次发布前的关键修复

- **编号与目录一一对应**：`v3` 固定是 `dshV3`。目录还在（哪怕已移出管理）就占着编号，
  新装直接跳到下一个空编号，不再出现 `dshV3-2` 这种名字。
- **「全部停止」不再卡界面**：原来在 UI 线程里逐个枚举进程 + 识别端口占用，4 个槽会冻住几秒；
  现在整个停止过程在后台线程执行。顺带修掉"停完后所有操作按钮被永久禁用"的方向写反 bug。
- **日志不再自相矛盾**：杀进程后立刻刷新端口探测缓存，不会出现「端口已释放」紧跟「运行中（外部）」。
- **纳入与接回统一体检**：缺 `bin.js` / 缺依赖一律拒绝并说明缺什么，不再"看着正常、一启动就炸"。
- **卸载不再生成空备份**：`.dsh-home` 为空（槽从没启动过）时不建只有 `_meta.json` 的备份目录；
  确认框会**点名列出**你放进槽目录的非 dsh 条目（那些会一起删、且不进备份）。
- **安装失败也能换镜像重试**：`ECONNRESET` 之类不再只看退出码正负，下载阶段的重试也调紧了。
- **界面细节**：主题改为三个分段按钮（选中即填充，深色下也看得清）；设置页在 380px 侧栏内不再挤掉按钮；
  纳入对话框按内容定尺寸、每行标出「原地接回 / 复制一份」；安装进度窗改成**自绘标题栏**——
  标题在左、右上角只有「—」（最小化），没有 X，窗口可拖动、任务栏有入口
  （「—」用 Label 自绘，不会在浅灰窗口上留白补丁；底色跟随主题，深色下不会冒出浅色块）。
- **装新版本时可以用别的版本**：安装进度窗不再锁主界面，已装槽的「启动 / 停止」保持可用
  （安装只在新建的槽目录里跑 npm，不碰已有槽）；卸载 / 移出 / 移动仍然全冻结。
