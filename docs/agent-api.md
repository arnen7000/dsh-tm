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

1. read a structured report (`health --json`, `list --json`);
2. decide the root cause and the next action;
3. call one write command, then re-read the state to confirm.

Commands are split into three classes. Only the first two are available on the CLI:

| Class | Commands | Effect |
|---|---|---|
| **read** | `list`, `status`, `url`, `health` | Nothing is changed. Safe to call anytime |
| **runtime write** | `start`, `stop`, `kill-stray` | Starts/stops processes. Reversible |
| **disk write** | install / adopt / uninstall / detach / move | **CLI does not expose these.** GUI only, with confirmation and backup |

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
| `--verbose` | Send low-level diagnostics to **stderr** |
| `--version` | Print `dsh-tm <version>` and exit |

`--json` / `--lang` may appear before **or after** the subcommand (the CLI reorders argv).
`stdout` always contains **exactly one JSON object**; diagnostics go to stderr.

### 3. Common envelope

Every command returns at least these six top-level fields:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | int | Currently `1` |
| `command` | string | The command name, e.g. `"start"` |
| `ok` | bool | See below — **the meaning differs for read vs write commands** |
| `count` | int | Number of entries in `slots` |
| `slots` | array | Per-slot results. Always an array of objects, possibly empty |
| `errors` | array | `{"key": str\|null, "code": str, "detail": str}`; empty on success |

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
| `start` / `stop` | `changed[]`, plus `waited_seconds` for `start` | `key`, `action`, `ok`, `state`, `listening`, `detail`, optional `error_code` |
| `kill-stray` | `executed`, `dry_run`, `requires_confirm`, `managed_slots`, `sig_any`, `changed[]` | slot snapshot (same shape as `list`) |

Notes:

- `changed[]` lists slots whose running state actually flipped. An idempotent call
  (`start` on an already-running slot) returns `ok: true` with `changed: []`.
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
| `module_unavailable` | `dsh_kill` could not be imported | 1 |
| `kill_failed` | `kill-stray --yes` raised | 1 |
| `state_not_reached` | Fallback for a failed write item | 1 |

Process exit codes: `0` success / `1` some failure (including `health` with
`level: "error"`) / `2` usage error / `3` config error.

**Write commands never default to "all slots".** `start --json` with no target returns
`no_target` instead of silently starting everything.

### 6. Examples

```bat
python dsh-tm.py list --json
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

A typical agent loop: `health --json` → pick the slot with `level: "error"` →
`stop <slot> --json` → `health --json` again to confirm. Do not assume a write
succeeded just because the process returned 0 — re-read the state.

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
  script.
- `config.json` paths use placeholders; never write absolute drive letters by hand.

See [Contributing](../CONTRIBUTING.md) for the code map and
[Changelog](../CHANGELOG.md) for the version that introduced this contract.

---

## 中文

### 1. 设计分界

`dsh-tm` **刻意不自己长出 LLM 判断层**。它的价值是「确定、可复现、不猜」。外部 agent
的正确用法是：① 读结构化报告（`health --json` / `list --json`）→ ② 判断根因与下一步
→ ③ 调一个写命令，再读一次状态确认。

命令分三类，CLI 只提供前两类：

| 类别 | 命令 | 影响 |
|---|---|---|
| **只读** | `list`、`status`、`url`、`health` | 不改任何东西，随时可调 |
| **改运行时** | `start`、`stop`、`kill-stray` | 起停进程，可逆 |
| **改磁盘** | 安装 / 纳入 / 卸载 / 移出 / 搬迁 | **CLI 不提供**，只在 GUI 里做，带确认与备份 |

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
| `--verbose` | 底层详细过程打到 **stderr** |
| `--version` | 打印 `dsh-tm <版本>` 后退出 |

`--json` / `--lang` 可写在子命令之前或之后（CLI 会重排 argv）。`stdout` 里**永远只有一个
JSON 对象**，诊断信息只走 stderr。

### 3. 公共外层字段

每个命令至少返回这六个顶层字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `schema_version` | int | 目前是 `1` |
| `command` | string | 命令名，如 `"start"` |
| `ok` | bool | 见下 —— **只读与写操作含义不同** |
| `count` | int | `slots` 的条目数 |
| `slots` | array | 逐槽结果，永远是对象数组，可以为空 |
| `errors` | array | `{"key": str\|null, "code": str, "detail": str}`，成功时为空 |

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
| `start` / `stop` | `changed[]`；`start` 另有 `waited_seconds` | `key`、`action`、`ok`、`state`、`listening`、`detail`，失败时加 `error_code` |
| `kill-stray` | `executed`、`dry_run`、`requires_confirm`、`managed_slots`、`sig_any`、`changed[]` | 槽快照（与 `list` 同形） |

说明：

- `changed[]` 是运行状态**真的发生翻转**的槽。幂等调用（对已在运行的槽 `start`）会
  返回 `ok: true` 且 `changed: []`。
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
| `module_unavailable` | 导入 `dsh_kill` 失败 | 1 |
| `kill_failed` | `kill-stray --yes` 抛异常 | 1 |
| `state_not_reached` | 写操作失败项的兜底码 | 1 |

进程退出码：`0` 成功 / `1` 有失败（含 `health` 的 `level: "error"`）/ `2` 用法错误 /
`3` 配置错误。

**写操作绝不默认「对全部槽执行」**。`start --json` 不给目标会返回 `no_target`，不会
悄悄把所有版本都启动起来。

### 6. 示例

```bat
python dsh-tm.py list --json
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

典型 agent 循环：`health --json` → 挑出 `level: "error"` 的槽 → `stop <槽> --json` →
再跑一次 `health --json` 确认。**不要因为退出码是 0 就假定写操作成功**，要重读状态。

### 7. 契约版本规则

加字段**不**升 `schema_version`；改字段名、改枚举值、改 `ok` 的含义才升。调用方应检查
`schema_version`，并忽略不认识的字段。

### 8. 安全边界

- 杀进程只认「该槽 `bin.js` 绝对路径 + 端口」；端口被非 dsh 程序占用时**拒绝杀**。
- `kill-stray` 只认命令行含 `@deepseek-ai/dsh` 的 node 进程。
- 改磁盘的操作完全不在 CLI 里 —— 脚本不可能误卸载。
- `config.json` 路径一律占位符，别手写绝对路径。

代码地图见 [参与开发](../CONTRIBUTING.md)，契约引入的版本见
[更新记录](../CHANGELOG.md)。
