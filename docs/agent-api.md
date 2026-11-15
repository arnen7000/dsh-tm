# Agent API / Agent 接口契约

> **English first, Chinese follows.** 英文在前、中文紧随。
>
> For anyone driving `dsh-tm.py` from another agent, script, or CI job. This document is
> the **only coupling point** between the deterministic tool (`dsh-tm`) and the intelligent
> layer (an external agent that reads reports and decides what to do).
> 给想用另一个 agent / 脚本 / CI 驱动 `dsh-tm.py` 的人。本文档是「确定性工具」与
> 「智能层」之间唯一的耦合点：工具只出结构化事实，判断交给外部 agent。

---

## English

### 1. Design boundary

`dsh-tm` deliberately **does not grow an LLM judgement layer**. Its value is being
deterministic, reproducible, and never guessing. An external agent should:

1. read a structured report (`doctor --json`, `health --json`, `list --json`);
2. decide the root cause and the next action;
3. call one write command, then re-read the state to confirm.

`doctor --json` is the preferred entry point for diagnosis: one read-only call merges
config validation, health checks and slot-shape checks, and returns fixed English
`code` / `source` / `action` labels instead of localised free text.

Commands are split into three classes. Only the first two are available on the CLI:

| Class | Commands | Effect |
|---|---|---|
| **read** | `list`, `status`, `url`, `health`, `doctor` | Nothing is changed. Safe to call anytime |
| **runtime write** | `start`, `stop`, `kill-stray` | Starts/stops processes. Reversible |
| **disk write** | install / adopt / uninstall / detach / move | **CLI does not expose these.** GUI only, with confirmation and backup |

The table is not decoration: `CMD_CLASS` in `dsh-tm.py` is the single source of
truth. `main()` refuses to dispatch when a command is missing from (or stale in)
that table, or when any entry is `disk_write`. Every `--json` result carries the
class as a top-level `effect`:

| `effect` | Class | Allowed to change |
|---|---|---|
| `read` | read | nothing (except the one-time default-config bootstrap, see §8) |
| `runtime_write` | runtime write | running processes only |
| `disk_write` | disk write | **never emitted by the CLI** |

`effect` is the command's **class** — what it is *allowed* to change, **not proof**
that something changed. Use `ok` / `changed[]` for the outcome. Write commands
always carry `changed[]` (an array, possibly empty); read commands never do.

### 2. Invocation

```bat
python dsh-tm.py <command> [slots] --json
python dsh-tm.py --json <command> [slots]     :: both orders work
```

| Flag | Meaning |
|---|---|
| `--json` | Emit machine-readable JSON. Every command supports it |
| `--lang zh\|en` | Language of **human-readable free text only**. Field names and enum values never change |
| `--config <path>` | Use another `config.json` |
| `--deep` | Deeper check for `health` / `doctor` (package deps, one `node --version`) |
| `--verbose` | Send low-level diagnostics to **stderr** |
| `--version` | Print `dsh-tm <version>` and exit |

`--json` / `--lang` may appear before **or after** the subcommand (the CLI reorders argv).
`stdout` always contains **exactly one JSON object**; diagnostics go to stderr.

### 3. Common envelope

Every command returns at least these seven top-level fields:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | int | Currently `1` |
| `command` | string | The command name, e.g. `"start"` |
| `effect` | string | `read` / `runtime_write` — the command's class from `CMD_CLASS`, never `disk_write` |
| `ok` | bool | See below — **the meaning differs for read vs write commands** |
| `count` | int | Number of entries in `slots` |
| `slots` | array | Per-slot results. Always an array of objects, possibly empty |
| `errors` | array | `{"key": str\|null, "code": str, "detail": str}`; empty on success |

`unknown_slots[]` appears only when a named slot is not in `config.json`: the
read command still reports the registered slots, and lists the mistyped names
here instead of silently returning an empty/partial-looking report.

Every result also carries `config_problems[]`: a list of
`{"code", "key", "level", "blocking", "detail", ...}`. Empty means the config is
trustworthy. Blocking problems (a malformed `versions` section, a slot that is not an
object, too many slots) abort the command with `ok: false` and exit `3`; non-blocking
port problems are reported here while read commands still return their data.

**`ok` semantics**

- **Read commands**: `ok: true` means "the command ran". It does **not** mean the slots
  are healthy — see `slots[].level` and `level`. A `health` call can return
  `ok: true` with `level: "error"` and exit code `1`.
- **Write commands**: `ok: true` means "the requested state was reached". For `start`
  that includes the port really listening; for `stop` that includes the port really closed.

> `detail`, `title`, and similar free-text fields follow `--lang` and are **for humans**.
> Never parse them. Use `error`, `code`, `level`, `state` — fixed English enums.

### 4. Per-command fields

| Command | Extra top-level fields | `slots[]` entry |
|---|---|---|
| `list` / `status` | — | `key`, `label`, `port`, `state` (`running`/`stopped`), `version`, `bin_js`, `dsh_home`, `temp_dir`, `browser` |
| `url` | — | `key`, `url`, `has_token` |
| `health` | `deep`, `level` (`ok`/`warning`/`error`) | `key`, `label`, `level`, `items[]` of `{level, title, detail}` |
| `doctor` | `deep`, `level`, `config_valid`, `summary`, `config_issues[]`, `global_issues[]` | `key`, `label`, `port`, `state`, `version`, `level`, `issues[]`, `actions[]` |
| `start` / `stop` | `changed[]`, plus `waited_seconds` for `start` | `key`, `action`, `ok`, `state`, `listening`, `detail`, optional `error_code` |
| `kill-stray` | `executed`, `dry_run`, `requires_confirm`, `managed_slots`, `sig_any`, `changed[]` | slot snapshot (same shape as `list`) |

`doctor` issue shape: `{level, source, code, title, detail, action}`.
`source` is `config` / `health` / `shape`; `level` is `error` / `warning` / `info`.
`code` and `action` are **fixed English enums** — never localised, never free text.
`actions[]` is the deduplicated, severity-ordered list of `action` values for that slot:

| Action | Meaning |
|---|---|
| `fix_config` | Fix `config.json` (port / `--port` mismatch, duplicate port, malformed values) |
| `reinstall_slot` | The slot's program files are missing or broken; reinstall that version |
| `regenerate_bats` | Regenerate the slot's launcher / kill bats from the GUI |
| `create_data_dir` | The per-slot data directory will be created on first launch |
| `review_manually` | A slot-shape difference needs a human decision; the manager never auto-repairs |
| `none` | Informational only (`info` items such as the installed version) |

`doctor` is read-only: `ok: true` means the report was produced, **not** that the
machine is healthy. Read `level` (exit `1` when `error`) and `summary`.
If a diagnostic module cannot be loaded, `ok: false` and `errors[].code` is
`health_unavailable` / `shape_unavailable`.

Notes:

- `changed[]` lists slots whose running state actually flipped. An idempotent call
  (`start` on an already-running slot) returns `ok: true` with `changed: []`.
  Every write command (`effect: "runtime_write"`) always includes `changed[]`,
  including usage errors (`no_target`) and config errors (exit `2` / `3`).
- `health --json` counts only `error` / `warning` items as problems: a slot with
  just `info` items (e.g. the installed version) is `level: "ok"`. If a diagnostic
  module cannot be imported, the result is still one JSON object with
  `ok: false` and `errors[].code = "module_unavailable"`.
- `url` never contains a login token. `has_token: false` means a bare address; the
  full tokenised URL exists only in the GUI process memory — use the card's
  **Copy entry URL** button.
- `kill-stray --json` **without `--yes` is a dry run**: `executed: false`,
  `requires_confirm: true`, and no process is touched. Add `--yes` to actually clean up.

### 5. Error codes and exit statuses

`errors[].code` and the usage-level `error` field use fixed English enums:

| Code | Meaning | Exit |
|---|---|---|
| `no_target` | A write command got no slot and no `--all` | 2 |
| `unknown_slot` | A named slot is not in `config.json` (`unknown[]`, `available[]` included) | 2 |
| `spawn_failed` | `start` could not launch (missing `bin.js` / `node.exe`, Popen failed) | 1 |
| `port_not_listening` | `start` launched but the port never listened within `--wait` | 1 |
| `stop_failed` | `stop` could not issue the stop | 1 |
| `still_running` | `stop` issued but the port is still occupied | 1 |
| `module_unavailable` | A diagnostic / kill module could not be imported (`health` / `kill-stray`) | 1 |
| `kill_failed` | `kill-stray --yes` raised | 1 |
| `state_not_reached` | Fallback for a failed write item | 1 |
| `config_port_invalid` | A write command refused a slot whose port config is untrustworthy | 1 |
| `health_unavailable` / `shape_unavailable` | `doctor` could not load a diagnostic module (`ok: false`) | 1 |
| `config_unreadable` | `config.json` cannot be read or parsed — including an explicit `--config` path that does not exist | 3 |
| `versions_missing` / `versions_not_object` / `slot_not_object` / `too_many_slots` | Config cannot be safely enumerated; **never** treated as "zero slots" | 3 |

Config problems in `config_problems[]` use the same fixed English `code` values:
`versions_missing`, `versions_not_object`, `slot_not_object`, `too_many_slots`,
`port_missing`, `port_invalid`, `port_is_string`, `extra_args_not_list`,
`port_arg_missing`, `port_arg_invalid`, `port_mismatch`, `duplicate_port`.
`port_mismatch` also carries `expected` / `actual`; `duplicate_port` carries
`conflicts_with` / `port`.

Process exit codes: `0` success / `1` some failure (including `health` / `doctor`
with `level: "error"`) / `2` usage error / `3` config error.

**Write commands never default to "all slots".** `start --json` with no target returns
`no_target` instead of silently starting everything.

### 6. Examples

```bat
python dsh-tm.py list --json
python dsh-tm.py doctor --json                :: start here when diagnosing
python dsh-tm.py doctor --json --deep
python dsh-tm.py health --json --deep
python dsh-tm.py start v1 --json
python dsh-tm.py stop --all --json
python dsh-tm.py kill-stray --json            :: dry run, always safe
python dsh-tm.py kill-stray --yes --json      :: really kills dsh processes
```

```powershell
$r = python dsh-tm.py list --json | ConvertFrom-Json
if (-not $r.ok) { throw "command failed" }
$r.slots | Where-Object { $_.state -eq 'running' }
```

A typical agent loop: `doctor --json` → pick the slot with `level: "error"` and
read its `actions[]` → `stop <slot> --json` → `doctor --json` again to confirm.
Do not assume a write succeeded just because the process returned 0 — re-read the
state, and always inspect `config_problems[]` before trusting `port` / `state`.

### 7. Contract versioning

Adding a field does **not** bump `schema_version`. Renaming a field, changing an enum
value, or changing the meaning of `ok` does. Consumers should check `schema_version`
and treat unknown fields as ignorable.

### 8. Safety boundaries

- Killing processes only matches "that slot's absolute `bin.js` path + port"; if a port
  is held by a non-dsh program, the manager **refuses** to kill it.
- `kill-stray` only recognises node processes whose command line contains
  `@deepseek-ai/dsh`.
- Disk-changing operations are not on the CLI at all — no accidental uninstall from a
  script. `doctor` neither writes nor repairs anything.
- `config.json` paths use placeholders; never write absolute drive letters by hand.
- Config validation is shared by the GUI, the CLI and `doctor` (`dsh_config_guard.py`).
  When `start` / `stop` see a port mismatch they refuse with `config_port_invalid`
  rather than acting on a wrong port.
- When the **default** `config.json` is missing, the first CLI call copies
  `config/config.example.json` into place. That is the only disk write a read
  command can ever make. An explicit `--config <path>` pointing at a missing file
  is **never** auto-created: it fails with `config_unreadable` / exit `3`, so a
  mistyped path can never look like "zero versions installed".
- `CMD_CLASS` is checked at runtime and in the offline regression
  (`scripts/test-cli-boundary.py`) against the command table **and this document**;
  the CLI cannot grow a disk-changing command silently.

See [Contributing](../CONTRIBUTING.md) for the code map and
[Changelog](../CHANGELOG.md) for the version that introduced this contract.

---

## 中文

### 1. 设计分界

`dsh-tm` **刻意不自己长出 LLM 判断层**。它的价值是「确定、可复现、不猜」。外部 agent
的正确用法是：① 读结构化报告（`doctor --json` / `health --json` / `list --json`）→
② 判断根因与下一步 → ③ 调一个写命令，再读一次状态确认。

排障优先用 `doctor --json`：一次只读调用把配置校验、槽体检、槽形状复核合成一份
结论，每条问题给固定英文 `code` / `source` / `action`，不用解析随语言变的自由文本。

命令分三类，CLI 只提供前两类：

| 类别 | 命令 | 影响 |
|---|---|---|
| **只读** | `list`、`status`、`url`、`health`、`doctor` | 不改任何东西，随时可调 |
| **改运行时** | `start`、`stop`、`kill-stray` | 起停进程，可逆 |
| **改磁盘** | 安装 / 纳入 / 卸载 / 移出 / 搬迁 | **CLI 不提供**，只在 GUI 里做，带确认与备份 |

这张表不是说明文字：`dsh-tm.py` 里的 `CMD_CLASS` 是唯一事实来源。`main()` 每次
分发前都对账，命令漏分类、表里多出旧命令、或出现 `disk_write` 都会当场失败。
每个 `--json` 结果的顶层 `effect` 就是这条命令的类别：

| `effect` | 类别 | 允许改动 |
|---|---|---|
| `read` | 只读 | 不改任何东西（§8 里默认配置的一次性初始化除外） |
| `runtime_write` | 改运行时 | 只改进程运行状态 |
| `disk_write` | 改磁盘 | **CLI 永不输出** |

`effect` 是**命令类别**（它被允许改什么），**不是本次调用**确实改动了什么的证明；
结果请看 `ok` / `changed[]`。写命令一律带 `changed[]`（数组，可以为空），
只读命令不带这个字段。

### 2. 调用方式

```bat
python dsh-tm.py <命令> [槽名] --json
python dsh-tm.py --json <命令> [槽名]     :: 两种顺序都可以
```

| 参数 | 含义 |
|---|---|
| `--json` | 输出机器可读 JSON，**所有命令**都支持 |
| `--lang zh\|en` | 只影响**给人看的自由文本**；字段名和枚举值永不变化 |
| `--config <路径>` | 换一份 `config.json` |
| `--deep` | `health` / `doctor` 的深查（依赖核对、跑一次 `node --version`） |
| `--verbose` | 底层详细过程打到 **stderr** |
| `--version` | 打印 `dsh-tm <版本>` 后退出 |

`--json` / `--lang` 可写在子命令之前或之后（CLI 会重排 argv）。`stdout` 里**永远只有一个
JSON 对象**，诊断信息只走 stderr。

### 3. 公共外层字段

每个命令至少返回这七个顶层字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `schema_version` | int | 目前是 `1` |
| `command` | string | 命令名，如 `"start"` |
| `effect` | string | `read` / `runtime_write` —— 取自 `CMD_CLASS` 的命令类别，绝不出现 `disk_write` |
| `ok` | bool | 见下 —— **只读与写操作含义不同** |
| `count` | int | `slots` 的条目数 |
| `slots` | array | 逐槽结果，永远是对象数组，可以为空 |
| `errors` | array | `{"key": str\|null, "code": str, "detail": str}`，成功时为空 |

`unknown_slots[]` 只在给了未登记的槽名时出现：只读命令照常报告已登记的槽，
并把拼错的名字列在这里，不会悄悄给出一份看着像「空报告」的结果。

每个结果还带 `config_problems[]`：`{"code", "key", "level", "blocking", "detail", ...}`，
空数组 = 配置可信。**阻断级**问题（`versions` 类型写坏、槽不是对象、槽数超上限）
让命令 `ok: false` 并退出 3；端口类问题不阻断读命令，只列在这里。

**`ok` 的语义**

- **只读命令**：`ok: true` 表示「命令跑通了」，**不代表槽健康** —— 看 `slots[].level`
  与顶层 `level`。`health` 完全可能 `ok: true`、`level: "error"`、退出码 `1`。
- **写操作**：`ok: true` 表示「请求的目标状态已达成」。`start` 要求端口真的在监听，
  `stop` 要求端口真的不通。

> `detail`、`title` 这类自由文本会随 `--lang` 变，**给人看的，不要解析**。判断请认
> `error` / `code` / `level` / `state` 这些固定英文枚举。

### 4. 各命令特有字段

| 命令 | 额外顶层字段 | `slots[]` 条目 |
|---|---|---|
| `list` / `status` | — | `key`、`label`、`port`、`state`（`running`/`stopped`）、`version`、`bin_js`、`dsh_home`、`temp_dir`、`browser` |
| `url` | — | `key`、`url`、`has_token` |
| `health` | `deep`、`level`（`ok`/`warning`/`error`） | `key`、`label`、`level`、`items[]`（`{level, title, detail}`） |
| `doctor` | `deep`、`level`、`config_valid`、`summary`、`config_issues[]`、`global_issues[]` | `key`、`label`、`port`、`state`、`version`、`level`、`issues[]`、`actions[]` |
| `start` / `stop` | `changed[]`；`start` 另有 `waited_seconds` | `key`、`action`、`ok`、`state`、`listening`、`detail`，失败时加 `error_code` |
| `kill-stray` | `executed`、`dry_run`、`requires_confirm`、`managed_slots`、`sig_any`、`changed[]` | 槽快照（与 `list` 同形） |

`doctor` 的问题条目形状：`{level, source, code, title, detail, action}`。
`source` 取 `config` / `health` / `shape`；`level` 取 `error` / `warning` / `info`。
`code` 与 `action` 是**固定英文枚举**，绝不本地化、绝不是自由文本。
`actions[]` 是该槽所有 `action` 去重后按严重程度排好的列表：

| 动作码 | 含义 |
|---|---|
| `fix_config` | 修 `config.json`（port / `--port` 不一致、端口重复、字段值非法） |
| `reinstall_slot` | 槽的程序文件缺失或损坏，重装这个版本 |
| `regenerate_bats` | 在管理器里重新生成启动 / kill bat |
| `create_data_dir` | 数据目录尚未创建，首次启动会自动建 |
| `review_manually` | 槽形状与其它槽不同，需人工判断；管理器不会自动修 |
| `none` | 仅提示（`info` 条目，如实装版本） |

`doctor` 是只读命令：`ok: true` 只表示「报告出来了」，**不代表机器健康**。看 `level`
（为 `error` 时退出 1）与 `summary`。诊断模块加载不了时 `ok: false`，
`errors[].code` 为 `health_unavailable` / `shape_unavailable`。

说明：

- `changed[]` 是运行状态**真的发生翻转**的槽。幂等调用（对已在运行的槽 `start`）会
  返回 `ok: true` 且 `changed: []`。所有写命令（`effect: "runtime_write"`）都带
  `changed[]`，包括用法错误（`no_target`）和配置错误（退出码 2 / 3）。
- `health --json` 只把 `error` / `warning` 当问题：只有 `info` 条目（如实装版本）的槽
  是 `level: "ok"`。诊断模块加载不了时仍输出一个 JSON 对象，`ok: false`、
  `errors[].code = "module_unavailable"`。
- `url` 永远不含登录 token。`has_token: false` 表示只是裸地址；带 token 的完整 URL
  只存在 GUI 进程内存里，请在卡片上点「复制入口 URL」。
- `kill-stray --json` **不加 `--yes` 就是演练**：`executed: false`、
  `requires_confirm: true`，不动任何进程。加 `--yes` 才真的清理。

### 5. 错误码与退出码

`errors[].code` 与用法错误顶层的 `error` 用固定英文枚举：

| 码 | 含义 | 退出码 |
|---|---|---|
| `no_target` | 写操作没给槽、也没给 `--all` | 2 |
| `unknown_slot` | 指定的槽不在 `config.json`（附 `unknown[]` / `available[]`） | 2 |
| `spawn_failed` | `start` 起不来（缺 `bin.js` / `node.exe`、Popen 失败） | 1 |
| `port_not_listening` | `start` 已发出，但 `--wait` 内端口始终没监听 | 1 |
| `stop_failed` | `stop` 指令没发出去 | 1 |
| `still_running` | `stop` 已发出但端口仍被占用 | 1 |
| `module_unavailable` | 诊断 / 杀进程模块导入失败（`health` / `kill-stray`） | 1 |
| `kill_failed` | `kill-stray --yes` 抛异常 | 1 |
| `state_not_reached` | 写操作失败项的兜底码 | 1 |
| `config_port_invalid` | 写操作拒绝端口配置不可信的槽 | 1 |
| `health_unavailable` / `shape_unavailable` | `doctor` 诊断模块加载失败（`ok: false`） | 1 |
| `config_unreadable` | `config.json` 读不到或解析失败；显式 `--config` 指向不存在的文件也算 | 3 |
| `versions_missing` / `versions_not_object` / `slot_not_object` / `too_many_slots` | 配置无法安全枚举；**绝不伪装成「零槽」** | 3 |

`config_problems[]` 的 `code` 也是固定英文枚举：`versions_missing`、
`versions_not_object`、`slot_not_object`、`too_many_slots`、`port_missing`、
`port_invalid`、`port_is_string`、`extra_args_not_list`、`port_arg_missing`、
`port_arg_invalid`、`port_mismatch`、`duplicate_port`。`port_mismatch` 另带
`expected` / `actual`，`duplicate_port` 另带 `conflicts_with` / `port`。

进程退出码：`0` 成功 / `1` 有失败（含 `health` / `doctor` 的 `level: "error"`）/
`2` 用法错误 / `3` 配置错误。

**写操作绝不默认「对全部槽执行」**。`start --json` 不给目标会返回 `no_target`，不会
悄悄把所有版本都启动起来。

### 6. 示例

```bat
python dsh-tm.py list --json
python dsh-tm.py doctor --json                :: 排障先跑这一条
python dsh-tm.py doctor --json --deep
python dsh-tm.py health --json --deep
python dsh-tm.py start v1 --json
python dsh-tm.py stop --all --json
python dsh-tm.py kill-stray --json            :: 只出计划，安全
python dsh-tm.py kill-stray --yes --json      :: 真的清理
```

```powershell
$r = python dsh-tm.py list --json | ConvertFrom-Json
if (-not $r.ok) { throw "command failed" }
$r.slots | Where-Object { $_.state -eq 'running' }
```

典型 agent 循环：`doctor --json` → 挑出 `level: "error"` 的槽、看它的 `actions[]` →
`stop <槽> --json` → 再跑一次 `doctor --json` 确认。**不要因为退出码是 0 就假定写操作
成功**，要重读状态；信任 `port` / `state` 之前先看 `config_problems[]`。

### 7. 契约版本规则

加字段**不**升 `schema_version`；改字段名、改枚举值、改 `ok` 的含义才升。调用方应检查
`schema_version`，并忽略不认识的字段。

### 8. 安全边界

- 杀进程只认「该槽 `bin.js` 绝对路径 + 端口」；端口被非 dsh 程序占用时**拒绝杀**。
- `kill-stray` 只认命令行含 `@deepseek-ai/dsh` 的 node 进程。
- 改磁盘的操作完全不在 CLI 里 —— 脚本不可能误卸载；`doctor` 不写盘、不修文件。
- `config.json` 路径一律占位符，别手写绝对路径。
- 配置校验由 GUI / CLI / `doctor` 共用一份实现（`dsh_config_guard.py`）。
  `start` / `stop` 遇到端口不一致会拒绝执行并报 `config_port_invalid`，
  不会拿错端口去启停进程。
- **默认** `config.json` 缺失时，第一次调用会从 `config/config.example.json`
  复制一份出来 —— 这是只读命令唯一可能写盘的情形。显式 `--config <路径>`
  指向不存在的文件时**绝不自动生成**，直接报 `config_unreadable`、退出 3，
  路径写错不会被伪装成「这台机器没装版本」。
- `CMD_CLASS` 在运行时和离线回归（`scripts/test-cli-boundary.py`）里都与命令表、
  与本文档逐项对账，CLI 不可能悄悄多出一条改磁盘命令。

代码地图见 [参与开发](../CONTRIBUTING.md)，契约引入的版本见
[更新记录](../CHANGELOG.md)。
