#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""dsh-tm 命令行接口（CLI）。

设计原则（2026-11-01）
----------------------
1. **不导入 dsh_manager.pyw**。那个文件是 GUI 入口，模块级就会建窗口、
   起线程、弹对话框；CLI 必须能在无图形环境（含 CI / 计划任务）里跑。
   所以这里只复用**纯逻辑**模块：dsh_kill / dsh_health / dsh_registry。
2. **与 GUI 用同一份 config.json、同一套隔离语义**（DSH_HOME / TEMP / 端口）。
   启动参数直接从 config 的 extra_args 取，不另造一套。
3. **只做 GUI 做不到或做起来笨的事**：批量启动、脚本化查询状态。
   单槽启停在界面上点一下更直观，CLI 也提供但主要用于自动化。
4. **CLI 拿不到入口 token**：完整 token 只存在 GUI 进程内存的 auth_urls 里，
   写进日志的是等长掩码后的 `token=abcd****`。所以 `url` 只打印裸地址，
   带 token 的完整 URL 请在管理器卡片上点「复制入口 URL」。

用法
----
    python dsh-tm.py list                  # 列出所有槽与运行状态
    python dsh-tm.py status [槽名]         # 同 list，可指定单个
    python dsh-tm.py start <槽名>          # 启动一个
    python dsh-tm.py start --all           # 启动全部
    python dsh-tm.py stop <槽名>           # 停止一个
    python dsh-tm.py stop --all            # 停止全部
    python dsh-tm.py url <槽名>            # 打印入口地址（不含 token）
    python dsh-tm.py health [槽名]         # 槽体检（只读）
    python dsh-tm.py doctor --json         # 综合诊断（配置 + 体检 + 槽形状 + 建议动作）
    python dsh-tm.py list --json           # 机器可读（所有命令都支持）
    python dsh-tm.py start v1 --json       # 写操作也返回结构化结果

配置层的判据统一走 `dsh_config_guard.py`（与 GUI 同一份）：类型写坏的
`versions` 不再被静默当成零槽，port 与 extra_args --port 不一致会附在
`config_problems` 里，并拒绝据此执行写操作。

`--json` 的固定 schema、读写分界与错误码见 docs/agent-api.md。
命令的读 / 写分类只认 `CMD_CLASS` 这一张表，`main()` 有注册闸门，
每个 JSON 输出都带 `effect`；写命令带 `changed[]`。CLI 永不提供改磁盘命令。
退出码：0 成功 / 1 有失败 / 2 用法错误 / 3 配置错误
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import dsh_i18n as I18N
from dsh_i18n import T
import dsh_config_guard as G

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def _adopt_config_lang(path=None):
    """按 config.json 的 `ui.lang` 设置 CLI 的输出语言。

    为什么放在模块顶层就试一次：argparse 的 `help=` 文本在**构建 parser 时**
    就求值了，等 `main()` 里读完 config 再设就晚了 —— `--help` 会一直是中文。
    这里先按默认路径猜一下；`main()` 里读到真实 config（可能来自 `--config`）
    之后再校正一次。
    """
    try:
        with open(path or CONFIG_PATH, encoding="utf-8-sig") as f:
            ui = (json.load(f) or {}).get("ui") or {}
        I18N.set_lang(ui.get("lang") or "zh")
    except Exception:
        pass


_adopt_config_lang()
HOME = os.path.expanduser("~")
LOG_FILE = os.path.join(BASE_DIR, "dsh-manager.log")

# 槽位数量上限。**必须与 dsh_manager.pyw 的 MAX_SLOTS 保持一致**，
# 否则 CLI 会放行界面上装不下的 config（或反过来误报超限）。
# scripts/test-cli.py 的 B4 断言锁着这条一致性。
MAX_SLOTS = 6

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3

# --verbose 打开后，底层模块（dsh_kill / dsh_health）的详细过程才打到 stderr。
_VERBOSE = {"on": False}

# `_cfg_or_die()` 校验出的配置层问题。所有 --json 输出都带上它，agent 不必
# 先猜「这份配置可信吗」；写操作还会据此拒绝端口不可信的槽。只读、不落盘。
_CONFIG_PROBLEMS = []

# ── 命令读写分界：唯一分类表（2026-11-15）────────────────────────────
# docs/agent-api.md §1 的三分类以前只写在文档里：代码里加一条新命令时，文档
# 可以不改、分类可以漏，脚本照样能驱动 —— 直到某天有人在 CLI 里接了「卸载」
# 而契约还写着「CLI 不碰磁盘」。现在分类表是代码事实：
#   · main() 每次调用都用 _gate_registry() 对账 handler 表，漏分类直接失败；
#   · 每个 --json 输出由 _emit() 补 effect，agent 不必自己维护白名单；
#   · 写命令统一带 changed[]，只读命令不带这个字段。
# **CLI 永不提供 disk_write**：安装 / 纳入 / 卸载 / 移出 / 搬迁只在 GUI，
# 带确认与备份。scripts/test-cli-boundary.py 会把这张表与文档逐项对账。
CMD_CLASS = {
    "list": "read",
    "status": "read",
    "url": "read",
    "health": "read",
    "doctor": "read",
    "start": "runtime_write",
    "stop": "runtime_write",
    "kill-stray": "runtime_write",
}
ALLOWED_EFFECTS = ("read", "runtime_write")

# _selected_keys() 记下被忽略的未知槽名，由 _emit() 显式摊给 agent。
# 地基审计实跑：以前 `status v9 --json` 返回 0 槽 ok=true，而 `url v9 --json`
# 返回全部槽 —— 同一个拼错槽名两种表现，agent 无从判断。
_UNKNOWN_SLOTS = []


def _gate_registry(handlers):
    """运行时闸门：命令表和分类表必须一一对应，且不允许 disk_write。"""
    missing = sorted(set(handlers) - set(CMD_CLASS))
    stale = sorted(set(CMD_CLASS) - set(handlers))
    bad = sorted(k for k, v in CMD_CLASS.items() if v not in ALLOWED_EFFECTS)
    if missing or stale or bad:
        raise RuntimeError(
            "CLI 命令分类表与实现不一致：未分类=%s 表里多出=%s 非法分类=%s"
            % (missing, stale, bad))


# ─────────────────────────── 配置 ───────────────────────────
def _probe_npm_global():
    """npm 全局 node_modules（供 <npm_global> 占位符用）。失败返回空串。"""
    try:
        if os.name != "nt":
            return ""
        appdata = os.environ.get("APPDATA") or ""
        p = os.path.join(appdata, "npm", "node_modules")
        return p if os.path.isdir(p) else ""
    except Exception:
        return ""


def load_config(path=CONFIG_PATH):
    """读 config.json。返回 (cfg, err)；失败时 cfg 为 None。"""
    if not os.path.exists(path):
        return None, T("找不到配置文件：%s") % path
    try:
        # 必须无 BOM：有 BOM 时 json.load 直接抛异常（项目文档里的硬规矩）
        with open(path, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except Exception as e:
        return None, T("config.json 解析失败：%s") % e
    if not isinstance(cfg, dict):
        return None, T("config.json 顶层必须是对象")
    return cfg, ""


# ── 机器可读输出（2026-11-14 建；2026-11-15 覆盖写操作）────────────────
# 为什么要 `--json`：dsh-tm 的价值是"确定、可复现、不猜"，最适合被另一个
# agent 驱动。但给人看的对齐文本 agent 解析起来很痛苦，所以**每个命令**都
# 提供固定 schema 的 JSON 出口（只读 + 写操作）。
#
# 约定（完整契约见 docs/agent-api.md）：
#   · **字段名与枚举值一律英文**，不跟 `--lang` 变 —— 机器读的东西不该随界面语言漂移
#   · 顶层必有 `schema_version`，加字段可以，改已有字段名要升版本
#   · 一次调用 stdout 只有**一个** JSON 对象；诊断信息只走 stderr，不混进 stdout
#   · 只读命令的 `ok` = "命令跑通了"，不代表槽健康（槽问题在 slots[].level）；
#     写操作的 `ok` = "请求的目标状态已达成"（start 后要真的在监听）
#   · `detail` / `title` 是给人看的自由文本，会随 --lang 变，**agent 不要解析**；
#     要判断就认 `error` / `code` / `level` 这些固定英文枚举
SCHEMA_VERSION = 1


def _emit(obj):
    """统一的 JSON 出口：补齐所有命令共有的顶层字段，保证契约形状一致。

    每个命令的 JSON 都至少有 schema_version / command / ok / count / slots /
    errors / effect；命令自己的字段（deep / dry_run / changed …）由调用方追加。
    `effect` 从 CMD_CLASS 查，是**命令类别**（它被允许改什么），不是本次调用的
    副作用证明；要看结果认 `ok` / `changed[]`。
    """
    obj = dict(obj)
    obj.setdefault("schema_version", SCHEMA_VERSION)
    obj.setdefault("count", len(obj.get("slots") or []))
    obj.setdefault("errors", [])
    # 加字段不升 schema_version（契约规则见 docs/agent-api.md §7）。
    # 配置坏但还能读时，只读命令照常出报告，同时把不可信之处摊开给 agent。
    obj.setdefault("config_problems", [dict(p) for p in _CONFIG_PROBLEMS])
    effect = CMD_CLASS.get(obj.get("command"), "read")
    obj.setdefault("effect", effect)
    if effect == "runtime_write":
        obj.setdefault("changed", [])
    if _UNKNOWN_SLOTS:
        obj.setdefault("unknown_slots", list(_UNKNOWN_SLOTS))
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _op_emit(command, rows, changed=None, extra=None):
    """写操作（start / stop）的统一 JSON 出口。

    rows[] 每项含 key / ok / action / state / detail（+ 可选 error_code）。
    `changed` 是状态真正发生翻转的槽；`errors` 是失败项的固定英文 code 汇总。
    """
    obj = {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "ok": all(r["ok"] for r in rows) if rows else True,
        "count": len(rows),
        "changed": list(changed or []),
        "slots": rows,
        "errors": [{"key": r["key"],
                    "code": r.get("error_code") or "state_not_reached",
                    "detail": r.get("detail", "")}
                   for r in rows if not r["ok"]],
    }
    if extra:
        obj.update(extra)
    _emit(obj)
    return EXIT_OK if obj["ok"] else EXIT_FAIL


def _usage_emit(command, err):
    """写操作缺目标 / 给了未知槽时的结构化用法错误（退出码 2）。"""
    obj = {"schema_version": SCHEMA_VERSION, "command": command,
           "ok": False, "count": 0, "changed": [], "slots": [],
           "errors": [{"key": None, "code": err.get("error", "no_target"),
                       "detail": ""}]}
    obj.update(err)
    _emit(obj)
    return EXIT_USAGE


def _slot_json(ctx, key):
    """一个槽的机器可读快照。只读，不启动任何东西。"""
    c = ctx.slot(key) or {}
    return {
        "key": key,
        "label": c.get("label") or key,
        "port": c.get("port"),
        "state": "running" if _is_running(ctx, key) else "stopped",
        "version": _installed_version(c) or None,
        "bin_js": c.get("bin_js"),
        "dsh_home": c.get("dsh_home"),
        "temp_dir": c.get("temp_dir"),
        "browser": c.get("browser"),
    }


# 体检条目的 level 在界面语言下是本地化字符串（中文「错误」/ 英文 "error"）。
# JSON 里必须归一成语言无关的枚举，否则 agent 得同时认两种写法。
_LV_EN = {"错误": "error", "error": "error", "Error": "error",
          "警告": "warning", "warning": "warning", "Warning": "warning",
          "提示": "info", "info": "info", "note": "info", "Note": "info"}


def _lv_en(lv):
    return _LV_EN.get(str(lv).strip(), str(lv).strip().lower())


class Ctx(object):
    """解析后的运行上下文：占位符已展开、版本表已就绪。"""

    def __init__(self, cfg):
        self.cfg = cfg
        # 先展开 install_base 自身：它可能出现 <home> / <self>，但不会出现 <base>
        # （旧实现直接调 _norm，而 _norm 里要读 self.install_base → AttributeError）
        _raw_base = cfg.get("install_base") or HOME
        if isinstance(_raw_base, str):
            _raw_base = _raw_base.replace("<home>", HOME).replace("<self>", BASE_DIR)
        self.install_base = os.path.normpath(_raw_base) if _raw_base else HOME
        self.npm_global = _probe_npm_global()
        self.versions = cfg.get("versions") or {}
        self.keys = list(self.versions.keys())

    def _norm(self, p):
        if not isinstance(p, str):
            return p
        p = p.replace("<home>", HOME).replace("<self>", BASE_DIR)
        p = p.replace("<base>", self.install_base)
        if "<npm_global>" in p:
            p = p.replace("<npm_global>", self.npm_global or "")
        return os.path.normpath(p) if p else p

    def slot(self, key):
        """取一个槽的展开后配置（node_exe / bin_js / dsh_home / temp_dir / port ...）。"""
        raw = self.versions.get(key)
        if raw is None:
            return None
        c = dict(raw)
        for f in ("node_exe", "bin_js", "dsh_home", "temp_dir"):
            if c.get(f):
                c[f] = self._norm(c[f])
        return c


EXAMPLE_PATH = os.path.join(BASE_DIR, "config", "config.example.json")


def _config_error(json_mode, command, code, detail):
    """配置不可用时的**唯一出口**：人读模式走 stderr，--json 仍给一个 JSON 对象。

    2026-11-15 地基审计：以前这里只往 stderr 写一行就 sys.exit(3)，
    `--json` 模式下 stdout 是空的 —— 契约说「一次调用 stdout 只有一个 JSON
    对象」，agent 解析到空串就直接崩。现在两种模式都结构化。
    """
    if not json_mode:
        sys.stderr.write(T("[错误] 配置不可用：%s\n") % detail)
    else:
        _emit({"schema_version": SCHEMA_VERSION, "command": command or "config",
               "ok": False, "count": 0, "slots": [], "config_valid": False,
               "errors": [{"key": None, "code": code, "detail": detail}]})
    sys.exit(EXIT_CONFIG)


def _cfg_or_die(path=CONFIG_PATH, json_mode=False, command="", auto_create=True):
    """读配置 + 统一校验；**默认路径**缺失时和 GUI 一样从模板自动生成。

    2026-11-01：全新 clone 后直接跑 `dsh-tm.py list` 会报「找不到配置文件」，
    但 GUI 双击却能自动生成 —— 补齐了这个不一致。
    2026-11-15：**删掉 `cfg["versions"] = {}` 那段静默兜底**。类型写坏的
    versions（例如被写成数组）以前会伪装成「零槽」，agent 会据此判断这台
    机器没装过任何版本。现在阻断级问题直接结构化报错退出 3；端口类问题
    不阻断读命令，但挂进 `config_problems` 并挡住写操作。
    2026-11-15 地基审计：显式 `--config <路径>` 指向不存在的文件时**不再**
    自动生成。以前会静默写出一份零槽模板并回 `ok:true, count:0` —— 路径
    写错的 agent 会误判「这台机器没装任何版本」，盘上还凭空多出一个文件。
    现在只有默认 config.json 缺失（用户没指定路径）才做 bootstrap。
    """
    if not os.path.exists(path) and auto_create and os.path.exists(EXAMPLE_PATH):
        try:
            import shutil
            shutil.copyfile(EXAMPLE_PATH, path)
            sys.stderr.write(T("[提示] 未找到 config.json，已从 config/config.example.json "
                             "生成默认配置。\n"))
        except Exception as e:
            sys.stderr.write(T("[警告] 自动生成 config.json 失败：%s\n") % e)
    cfg, err = load_config(path)
    if cfg is None:
        _CONFIG_PROBLEMS[:] = []
        _config_error(json_mode, command, "config_unreadable", err)

    problems = G.validate_config(cfg, max_slots=MAX_SLOTS)
    _CONFIG_PROBLEMS[:] = problems
    blocking = G.fatal(problems)
    if blocking:
        detail = "；".join("%s: %s" % (p["code"], p["detail"]) for p in blocking)
        _config_error(json_mode, command, blocking[0]["code"], detail)
    if problems and not json_mode:
        for p in problems:
            sys.stderr.write(T("[警告] config.json 有问题：%s\n") % p["detail"])
    return cfg


def _blocked_by_config(keys):
    """哪些槽的端口不可信，不能执行写操作。返回 {槽键: [code, ...]}。

    `start` / `stop` 都用 `port` 字段判断「起来了没有 / 停掉了没有」，
    配置一错，结论就是稳定的假象。GUI 在启动前会 fatal 拦住，CLI 以前没有
    这道闸 —— 现在对齐：只读照常报，写操作拒绝这些槽。
    """
    bad = {}
    for p in _CONFIG_PROBLEMS:
        if p["level"] != "error":
            continue
        k = p.get("key")
        if k is not None and k in keys:
            bad.setdefault(k, []).append(p["code"])
    return bad


# ─────────────────────────── 运行态探测 ───────────────────────────
def _port_open(port, timeout=0.25):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    finally:
        try:
            s.close()
        except Exception:
            pass


def _is_running(ctx, key):
    """该槽是否在运行：端口在听即视为运行（与 GUI 判定一致）。"""
    c = ctx.slot(key)
    if not c:
        return False
    try:
        return _port_open(c["port"])
    except Exception:
        return False


def _build_start_cmd(ctx, key):
    """拼出启动命令：本槽 node + bin.js + extra_args（缺省 web --host/--port --no-open）。"""
    c = ctx.slot(key)
    node = c.get("node_exe") or "node"
    bin_js = c.get("bin_js")
    args = c.get("extra_args") or ["web", "--host", "127.0.0.1",
                                   "--port", str(c.get("port")), "--no-open"]
    return [node, bin_js] + [str(a) for a in args]


def _slot_env(ctx, key):
    """每槽的隔离环境：DSH_HOME + TEMP/TMP（与 GUI、与槽内 bat 完全一致）。"""
    c = ctx.slot(key)
    env = os.environ.copy()
    if c.get("dsh_home"):
        env["DSH_HOME"] = c["dsh_home"]
    if c.get("temp_dir"):
        # 纯函数：这里**不建目录**。以前在这里 makedirs，任何调用方
        # （包括 test-cli.py 这种只读回归）都会在真实盘上留下
        # `<home>/dsh-versions/dsh-temp/<槽>`，总检的卫生闸因此报红
        # （2026-11-12 定位到 test-cli）。建目录挪到 _spawn。
        env["TEMP"] = c["temp_dir"]
        env["TMP"] = c["temp_dir"]
    try:
        env["NODE_OPTIONS"] = env.get("NODE_OPTIONS", "")
    except Exception:
        pass
    return env


def _spawn(ctx, key):
    """后台启动一个槽。返回 (ok, 说明)。"""
    c = ctx.slot(key)
    if c is None:
        return False, T("没有这个槽：%s") % key
    if _is_running(ctx, key):
        return True, T("已在运行（端口 %s）") % c.get("port")
    bin_js = c.get("bin_js")
    if not bin_js or not os.path.exists(bin_js):
        return False, T("bin.js 不存在：%s") % bin_js
    node = c.get("node_exe")
    if node and not os.path.exists(node):
        return False, T("node.exe 不存在：%s") % node
    cmd = _build_start_cmd(ctx, key)
    # 真正启动前才建 TEMP（见 _slot_env 的说明：算环境变量不该有副作用）
    _td = c.get("temp_dir")
    if _td:
        try:
            os.makedirs(_td, exist_ok=True)
        except Exception:
            pass
    env = _slot_env(ctx, key)
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | \
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(cmd, cwd=c.get("dsh_home") or BASE_DIR, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, creationflags=flags)
    except Exception as e:
        return False, T("启动失败：%s") % e
    return True, T("已发出启动指令（端口 %s）") % c.get("port")


def _stop(ctx, key):
    """走 dsh_kill 的签名门控停止（与 GUI「停止」同一条路，不误杀别的 node）。"""
    c = ctx.slot(key)
    if c is None:
        return False, T("没有这个槽：%s") % key
    if not _is_running(ctx, key):
        return True, T("本来就没在运行")
    try:
        sys.path.insert(0, BASE_DIR)
        import dsh_kill
    except Exception as e:
        return False, T("无法加载 dsh_kill：%s") % e
    # dsh_kill 的 LOG 默认是 print：GUI 里那叫"日志区"，CLI 里就只是噪音。
    # 静音后由本模块自己输出一行结论；--verbose 时才恢复详细过程。
    if not _VERBOSE.get("on"):
        dsh_kill.LOG = lambda *_a, **_k: None
    try:
        res = dsh_kill.kill_version(key, _make_sig(c), c.get("port"))
    except TypeError:
        # 老签名兼容
        try:
            res = dsh_kill.kill_version(_make_sig(c), c.get("port"))
        except Exception as e:
            return False, T("停止失败：%s") % e
    except Exception as e:
        return False, T("停止失败：%s") % e
    time.sleep(0.6)
    if _is_running(ctx, key):
        return False, T("已发出停止指令，但端口 %s 仍被占用") % c.get("port")
    return True, T("已停止")


def _make_sig(cfg):
    """签名 = 安装根（bin_js 里 /node_modules/ 之前那段），与 GUI 的 _make_sig 同义。"""
    bin_js = (cfg.get("bin_js") or "").replace("\\", "/").lower()
    i = bin_js.find("/node_modules/")
    return bin_js[:i] if i > 0 else bin_js


def _entry_url(ctx, key):
    """从日志里捞该槽最近一条入口 URL（拿不到完整 token 时返回空串）。"""
    c = ctx.slot(key)
    if not c or not os.path.exists(LOG_FILE):
        return ""
    marker = ("dsh web: http://127.0.0.1:%d" % c["port"]).encode()
    try:
        with open(LOG_FILE, "rb") as f:
            blob = f.read()
    except Exception:
        return ""
    pos = 0
    last = ""
    while True:
        i = blob.find(marker, pos)
        if i < 0:
            break
        j = blob.find(b"\n", i)
        line = blob[i: j if j >= 0 else len(blob)]
        m = re.search(rb"https?://\S+", line)
        if m:
            last = m.group(0).decode("utf-8", "replace")
        pos = (j + 1) if j >= 0 else len(blob)
    # 管理器把日志里的 token 等长掩码成 `abcd********`。这种 URL 复制到
    # 浏览器也登不进去，绝不能当成「带 token 的入口」返回。
    if "*" in last:
        return ""
    return last


# ─────────────────────────── 子命令 ───────────────────────────
def cmd_list(ctx, args):
    if getattr(args, "json", False):
        _emit({"schema_version": SCHEMA_VERSION, "command": "list", "ok": True,
               "count": len(ctx.keys),
               "slots": [_slot_json(ctx, k) for k in ctx.keys]})
        return EXIT_OK
    if not ctx.keys:
        print(T("当前没有管理任何版本槽（config.json 的 versions 为空）。"))
        return EXIT_OK
    width = max([len(k) for k in ctx.keys] + [4])
    print("%-*s  %-10s  %-6s  %-10s  %s" % (width, T("槽"), T("标签"), T("端口"), T("状态"), T("版本")))
    print("-" * 78)
    for k in ctx.keys:
        c = ctx.slot(k)
        running = _is_running(ctx, k)
        ver = _installed_version(c) or T("未知")
        print("%-*s  %-10s  %-6s  %-10s  %s" % (
            width, k, c.get("label") or k, c.get("port"),
            T("运行中") if running else T("已停止"), ver))
    return EXIT_OK


def _installed_version(cfg):
    """从 node_modules/@deepseek-ai/dsh/package.json 读实装版本。"""
    try:
        bin_js = cfg.get("bin_js") or ""
        root = bin_js
        i = root.replace("\\", "/").find("/lib/bin.js")
        if i > 0:
            root = root[:i]
        pkg = os.path.join(root, "package.json")
        if os.path.exists(pkg):
            with open(pkg, "r", encoding="utf-8-sig") as f:
                return json.load(f).get("version") or ""
    except Exception:
        pass
    return ""


def _config_refusal_detail(codes):
    return T("配置端口不可信（%s）：先修正 config.json 的 port 与 extra_args --port") \
        % "、".join(sorted(set(codes)))


def cmd_start(ctx, args):
    if getattr(args, "json", False):
        keys, err = _json_target_keys(ctx, args)
        if err:
            return _usage_emit("start", err)
        blocked = _blocked_by_config(keys)
        work = [k for k in keys if k not in blocked]
        before = {k: _is_running(ctx, k) for k in keys}
        rows = []
        for k in keys:
            if k in blocked:
                # 不 spawn：端口判断不可信，起起来也没法给出正确结论。
                rows.append({"key": k, "action": "start",
                             "detail": _config_refusal_detail(blocked[k]),
                             "_spawn_ok": False, "_refused": True})
                continue
            ok, msg = _spawn(ctx, k)
            # `_spawn_ok` 是中间量，输出前会摘掉；`ok` 留给"目标状态是否达成"
            rows.append({"key": k, "action": "start", "detail": msg,
                         "_spawn_ok": bool(ok)})
        # dsh 从 spawn 到 listen 实测要 5~8 秒（0.2.x 要加载插件树）。
        # quiet=True：JSON 模式下 WARN 不能打到 stdout，否则就不是单个 JSON 了。
        if work:
            _wait_until_listening(ctx, work,
                                  timeout=float(getattr(args, "wait", 20)), quiet=True)
        changed = []
        for r in rows:
            k = r["key"]
            spawn_ok = r.pop("_spawn_ok")
            refused = r.pop("_refused", False)
            now = _is_running(ctx, k)
            r["state"] = "running" if now else "stopped"
            r["listening"] = now
            r["ok"] = spawn_ok and now
            if not r["ok"]:
                r["error_code"] = ("config_port_invalid" if refused
                                   else "spawn_failed" if not spawn_ok
                                   else "port_not_listening")
            if before[k] != now:
                changed.append(k)
        return _op_emit("start", rows, changed=changed,
                        extra={"waited_seconds": float(getattr(args, "wait", 20))})
    keys = _target_keys(ctx, args)
    if not keys:
        return EXIT_USAGE
    blocked = _blocked_by_config(keys)
    rc = EXIT_OK
    todo = []
    for k in keys:
        if k in blocked:
            print("%-6s FAIL  %s" % (k, _config_refusal_detail(blocked[k])))
            rc = EXIT_FAIL
            continue
        todo.append(k)
    for k in todo:
        ok, msg = _spawn(ctx, k)
        print("%-6s %s  %s" % (k, "OK  " if ok else "FAIL", msg))
        if not ok:
            rc = EXIT_FAIL
    if todo:
        # 等端口真正开始监听再回报。dsh 从 spawn 到 listen 实测要 5~8 秒
        # （0.2.x 要加载插件树），固定 sleep 1.5s 会把"已启动"误报成"已停止"。
        _wait_until_listening(ctx, todo, timeout=float(getattr(args, "wait", 20)))
        print(T("\n实际状态："))
        rc |= cmd_list(ctx, args)
    return rc


def _wait_until_listening(ctx, keys, timeout=20.0, step=0.4, quiet=False):
    """轮询到所有目标槽都开始监听，或超时。返回仍未起来的槽名列表。

    `quiet=True` 给 JSON 模式用：超时 WARN 不能打到 stdout，否则 stdout 里
    就不止一个 JSON 对象了。调用方从返回值（或随后再查状态）拿这个信息。
    """
    deadline = time.time() + max(0.0, timeout)
    pending = list(keys)
    while pending and time.time() < deadline:
        pending = [k for k in pending if not _is_running(ctx, k)]
        if not pending:
            break
        time.sleep(step)
    if not quiet:
        for k in pending:
            print(T("%-6s WARN  等了 %.0f 秒仍未监听（dsh 可能仍在加载插件，稍后自行确认）")
                  % (k, timeout))
    return pending


def cmd_stop(ctx, args):
    if getattr(args, "json", False):
        keys, err = _json_target_keys(ctx, args)
        if err:
            return _usage_emit("stop", err)
        blocked = _blocked_by_config(keys)
        before = {k: _is_running(ctx, k) for k in keys}
        rows, changed = [], []
        for k in keys:
            if k in blocked:
                # 门控按端口找进程；配置端口不可信时宁可什么都不做。
                now = _is_running(ctx, k)
                rows.append({"key": k, "action": "stop", "ok": False,
                             "state": "running" if now else "stopped",
                             "listening": now,
                             "detail": _config_refusal_detail(blocked[k]),
                             "error_code": "config_port_invalid"})
                if before[k] != now:
                    changed.append(k)
                continue
            ok, msg = _stop(ctx, k)
            now = _is_running(ctx, k)
            reached = bool(ok) and not now
            row = {"key": k, "action": "stop", "ok": reached,
                   "state": "running" if now else "stopped",
                   "listening": now, "detail": msg}
            if not reached:
                row["error_code"] = ("stop_failed" if not ok else "still_running")
            rows.append(row)
            if before[k] != now:
                changed.append(k)
        return _op_emit("stop", rows, changed=changed)
    keys = _target_keys(ctx, args)
    if not keys:
        return EXIT_USAGE
    blocked = _blocked_by_config(keys)
    rc = EXIT_OK
    for k in keys:
        if k in blocked:
            print("%-6s FAIL  %s" % (k, _config_refusal_detail(blocked[k])))
            rc = EXIT_FAIL
            continue
        ok, msg = _stop(ctx, k)
        print("%-6s %s  %s" % (k, "OK  " if ok else "FAIL", msg))
        if not ok:
            rc = EXIT_FAIL
    return rc


def cmd_status(ctx, args):
    if getattr(args, "json", False):
        # 2026-11-15 地基审计：原来走 _target_keys（只给写操作的人读分支用），
        # 未知槽会被静默过滤成 0 槽 ok=true；现在与其它只读命令统一走
        # _selected_keys，并且 unknown_slots[] 会把拼错的槽名讲清楚。
        keys = _selected_keys(ctx, args)
        _emit({"schema_version": SCHEMA_VERSION, "command": "status", "ok": True,
               "count": len(keys),
               "slots": [_slot_json(ctx, k) for k in keys]})
        return EXIT_OK
    if args.slot:
        for k in _target_keys(ctx, args):
            print("%s: %s" % (k, T("运行中") if _is_running(ctx, k) else T("已停止")))
        return EXIT_OK
    return cmd_list(ctx, args)


def cmd_url(ctx, args):
    # 2026-11-15 修：以前 keys 只在 JSON 分支里赋值，非 JSON 分支直接
    # `for k in keys` → UnboundLocalError（url 不带 --json 必崩）。
    # 两个分支现在共用 _selected_keys：不写 stderr，JSON 安全。
    keys = _selected_keys(ctx, args)
    if getattr(args, "json", False):
        rows = []
        for k in keys:
            u = _entry_url(ctx, k)
            c = ctx.slot(k) or {}
            bare = ("http://127.0.0.1:%s/" % c["port"]) if c.get("port") else ""
            rows.append({"key": k, "url": u or bare or None,
                         # has_token=False 表示这只是裸地址，带 token 的完整 URL
                         # 只存在管理器进程内存里，CLI 拿不到
                         "has_token": bool(u)})
        _emit({"schema_version": SCHEMA_VERSION, "command": "url", "ok": True,
               "slots": rows})
        return EXIT_OK
    for s in (getattr(args, "slot", None) or []):
        if s not in ctx.versions:
            sys.stderr.write(T("[错误] 没有这个槽：%s\n") % s)
    found = 0
    for k in keys:
        u = _entry_url(ctx, k)
        c = ctx.slot(k) or {}
        bare = ("http://127.0.0.1:%s/" % c["port"]) if c.get("port") else ""
        if u or bare:
            found += 1
            print(u or bare)
            if not u:
                print(T("  （仅入口地址；带 token 的完整 URL 请在管理器点「复制入口 URL」）"),
                      file=sys.stderr)
        else:
            print(T("%s  找不到入口地址（槽配置里没有 port）") % k)
    return EXIT_OK if found == len(keys) else EXIT_FAIL


def cmd_health(ctx, args):
    try:
        sys.path.insert(0, BASE_DIR)
        import dsh_health
    except Exception as e:
        # 2026-11-15 地基审计：以前这里只往 stderr 写一行就 return，--json
        # 时 stdout 是空的（实测 0 字节），直接违反「一次调用一个 JSON 对象」。
        # doctor 早就把模块不可用结构化了，这里对齐，并复用同一个错误码。
        if getattr(args, "json", False):
            _emit({"command": "health", "ok": False, "count": 0, "slots": [],
                   "level": "error", "deep": bool(getattr(args, "deep", False)),
                   "errors": [{"key": None, "code": "module_unavailable",
                               "detail": str(e)}]})
            return EXIT_FAIL
        sys.stderr.write(T("[错误] 无法加载 dsh_health：%s\n") % e)
        return EXIT_FAIL
    keys = _selected_keys(ctx, args)   # 见 cmd_url：修非 JSON 分支的 UnboundLocalError
    if getattr(args, "json", False):
        # 不调 _target_keys —— 它会在没给槽时往 stderr 写中文提示；
        # 没指定槽就查全部（agent 通常想要全貌）。
        slots = []
        for k in keys:
            c = ctx.slot(k) or {}
            try:
                items = dsh_health.check_slot(
                    k, c, base=ctx.install_base, home=HOME, self_dir=BASE_DIR,
                    deep=bool(getattr(args, "deep", False)))
            except Exception as e:
                slots.append({"key": k, "label": c.get("label") or k,
                              "level": "error",
                              "items": [{"level": "error",
                                         "title": "health check failed",
                                         "detail": str(e)}]})
                continue
            mapped = [{"level": _lv_en(lv), "title": title, "detail": detail}
                      for lv, title, detail in items]
            # 2026-11-15 地基审计：以前「只要 items 非空」就报 warning，
            # 于是只有一条提示（如实装版本）的槽被误报成有警告。info 不是
            # 问题；枚举仍保持 ok / warning / error 三值，不升 schema_version。
            level = ("error" if any(m["level"] == "error" for m in mapped)
                     else "warning" if any(m["level"] == "warning" for m in mapped)
                     else "ok")
            slots.append({"key": k, "label": c.get("label") or k,
                          "level": level, "items": mapped})
        worst = ("error" if any(x["level"] == "error" for x in slots)
                 else "warning" if any(x["level"] == "warning" for x in slots)
                 else "ok")
        _emit({"schema_version": SCHEMA_VERSION, "command": "health", "ok": True,
               "deep": bool(getattr(args, "deep", False)),
               "level": worst, "count": len(slots), "slots": slots})
        return EXIT_FAIL if worst == "error" else EXIT_OK
    for s in (getattr(args, "slot", None) or []):
        if s not in ctx.versions:
            sys.stderr.write(T("[错误] 没有这个槽：%s\n") % s)
    bad = 0
    for k in keys:
        c = ctx.slot(k)
        print("=== %s (%s) ===" % (k, c.get("label") or k))
        try:
            items = dsh_health.check_slot(
                k, c, base=ctx.install_base, home=HOME, self_dir=BASE_DIR,
                deep=bool(getattr(args, "deep", False)))
        except Exception as e:
            print(T("  体检失败：%s") % e)
            bad += 1
            continue
        if not items:
            # 浅查干净时 items 是空的。只打标题、下面一片空白会让人以为命令没跑，
            # 所以明确说一句「没发现问题」，并提示深查开关。
            print(T("  未发现问题%s") % ("" if getattr(args, "deep", False)
                                      else T("（浅查；加 --deep 做依赖深查）")))
        for lv, title, detail in items:
            print("  [%s] %s：%s" % (lv, title, detail))
            if lv == "错误":
                bad += 1
        print()
    return EXIT_FAIL if bad else EXIT_OK


# ── doctor：一次调用把「配置 + 体检 + 槽形状」合成机器可读结论 ─────────────
# 与 health --json 的差别：health 的 items[] 只有随语言变的自由文本，agent
# 无从分类；doctor 给每条问题固定英文 code、source 和动作码，能直接据此决策。
# 只读：不写盘、不启停进程、不修文件（--deep 最多跑一次 node --version）。
_DOCTOR_TITLE = {
    "入口": ("entry", "reinstall_slot"),
    "槽目录": ("slot_root", "reinstall_slot"),
    "node": ("node", "reinstall_slot"),
    "node 版本": ("node_version", "reinstall_slot"),
    "数据目录": ("dsh_home", "create_data_dir"),
    "临时目录": ("temp_dir", "regenerate_bats"),
    "启动脚本": ("launcher_bats", "regenerate_bats"),
    "包清单": ("package_json", "reinstall_slot"),
    "实装版本": ("version", "none"),
    "依赖": ("dependencies", "reinstall_slot"),
    "原生模块": ("native_module", "reinstall_slot"),
    "端口冲突": ("port_conflict", "fix_config"),
    "重复槽": ("duplicate_slot", "fix_config"),
}
_DOCTOR_UNKNOWN = ("other", "review_manually")


def _doctor_title_map(title):
    """把随语言变的体检「项目」还原成固定英文 (code, action)。"""
    key = I18N.to_key(title) or str(title)
    return _DOCTOR_TITLE.get(key, _DOCTOR_UNKNOWN)


def _doctor_issue(level, source, code, title, detail, action):
    return {"level": level, "source": source, "code": code,
            "title": title, "detail": detail, "action": action}


def _doctor_level(issues):
    lv = "ok"
    for i in issues:
        if i["level"] == "error":
            return "error"
        if i["level"] == "warning":
            lv = "warning"
    return lv


def _doctor_actions(issues):
    rank = {"error": 0, "warning": 1, "info": 2}
    ordered = sorted(issues, key=lambda i: rank.get(i["level"], 3))
    out = []
    for i in ordered:
        a = i["action"]
        if a and a != "none" and a not in out:
            out.append(a)
    return out


def cmd_doctor(ctx, args):
    """综合诊断（只读）。返回结构化问题清单 + 固定英文建议动作。"""
    _json = getattr(args, "json", False)
    deep = bool(getattr(args, "deep", False))
    keys = _selected_keys(ctx, args)
    errors = []

    cfg_issues = []
    for p in _CONFIG_PROBLEMS:
        item = _doctor_issue(p["level"], "config", p["code"], "",
                             p["detail"], "fix_config")
        item["key"] = p["key"]          # 槽级问题时挂到对应槽，全局问题 key=None
        cfg_issues.append(item)
    global_issues = []
    health_by_key = {}
    shape_by_key = {}

    try:
        sys.path.insert(0, BASE_DIR)
        import dsh_health
        # check_all 内部会对 port 做 int()；配置里端口写坏时先摘掉这个字段，
        # 否则整份体检会因为一个坏槽全崩（配置问题本身已由 config_problems 报出）。
        safe = {}
        for k, raw in (ctx.versions or {}).items():
            if not isinstance(raw, dict):
                continue
            c = dict(raw)
            try:
                if c.get("port") is not None:
                    int(c.get("port"))
            except (TypeError, ValueError):
                c.pop("port", None)
            safe[k] = c
        res = dsh_health.check_all(safe, base=ctx.install_base, home=HOME,
                                   self_dir=BASE_DIR, deep=deep) or {}
        health_by_key = {k: v for k, v in res.items() if k != "_global"}
        for lv, title, detail in (res.get("_global") or []):
            code, action = _doctor_title_map(title)
            global_issues.append(_doctor_issue(_lv_en(lv), "health", code,
                                               title, detail, action))
    except Exception as e:
        errors.append({"key": None, "code": "health_unavailable", "detail": str(e)})

    try:
        sys.path.insert(0, BASE_DIR)
        import dsh_install_ops as OPS
        for k in keys:
            shape_by_key[k] = OPS.slot_shape_problems(
                ctx.slot(k) or {}, base=ctx.install_base, home=HOME)
    except Exception as e:
        errors.append({"key": None, "code": "shape_unavailable", "detail": str(e)})

    slots = []
    for k in keys:
        c = ctx.slot(k) or {}
        # 配置问题只挂到它对应的槽上（key=None 的全局问题留在 config_issues 里）。
        issues = [i for i in cfg_issues if i.get("key") == k]
        seen = set()
        for lv, title, detail in (health_by_key.get(k) or []):
            code, action = _doctor_title_map(title)
            issues.append(_doctor_issue(_lv_en(lv), "health", code,
                                        title, detail, action))
            seen.add((str(lv), str(detail)))
        for lv, detail in (shape_by_key.get(k) or []):
            if (str(lv), str(detail)) in seen:
                continue                      # health 已报过同一件事，别刷屏
            issues.append(_doctor_issue(_lv_en(lv), "shape", "shape", "",
                                        detail, "review_manually"))
        slots.append({"key": k, "label": c.get("label") or k,
                      "port": c.get("port"),
                      "state": "running" if _is_running(ctx, k) else "stopped",
                      "version": _installed_version(c) or None,
                      "level": _doctor_level(issues), "issues": issues,
                      "actions": _doctor_actions(issues)})

    # 槽级 config 问题已并进 slots[].issues，别在汇总里数两遍。
    all_issues = ([i for i in cfg_issues if i.get("key") is None]
                  + global_issues + [i for s in slots for i in s["issues"]])
    level = _doctor_level(all_issues)
    counts = {"error": 0, "warning": 0, "info": 0}
    for i in all_issues:
        if i["level"] in counts:
            counts[i["level"]] += 1
    ok = not errors

    if _json:
        _emit({"schema_version": SCHEMA_VERSION, "command": "doctor", "ok": ok,
               "count": len(slots), "level": level, "deep": deep,
               "config_valid": not cfg_issues,
               "summary": counts, "global_issues": global_issues,
               "config_issues": cfg_issues, "slots": slots, "errors": errors})
        return EXIT_FAIL if (not ok or level == "error") else EXIT_OK

    print(T("=== dsh-tm 诊断（只读，不改任何文件）==="))
    if cfg_issues:
        print(T("配置：%d 个问题") % len(cfg_issues))
    else:
        print(T("配置正常"))
    for i in cfg_issues:
        print("  [%-7s] [config] %s: %s" % (i["level"], i["code"], i["detail"]))
    if global_issues:
        print(T("全局问题"))
        for i in global_issues:
            print("  [%-7s] [health] %s: %s" % (i["level"], i["code"], i["detail"]))
    if not slots:
        print(T("没有登记任何槽（先装一个版本，或加 --deep 做依赖深查）"))
    for s in slots:
        print("--- %s (%s) [%s] state=%s ---"
              % (s["key"], s["label"], s["level"], s["state"]))
        for i in s["issues"]:
            print("  [%-7s] [%-6s] %s%s%s"
                  % (i["level"], i["source"], i["code"],
                     (" / " + i["title"]) if i["title"] else "",
                     (": " + i["detail"]) if i["detail"] else ""))
        if s["actions"]:
            print(T("建议动作：%s") % "、".join(s["actions"]))
    for e in errors:
        sys.stderr.write(T("[错误] 诊断模块不可用：%s\n") % e["detail"])
    print(T("结论：%s —— %d 个错误、%d 个警告、%d 条提示")
          % (level, counts["error"], counts["warning"], counts["info"]))
    return EXIT_FAIL if (not ok or level == "error") else EXIT_OK


def cmd_kill_stray(ctx, args):
    """清理**槽外**的 dsh 残留（界面上取消了这个按钮，改由 CLI 承担）。

    与 stop --all 的区别：
      · stop --all   —— 只停 config 里登记的这几个槽；
      · kill-stray   —— 连"没登记进 config 的 dsh 副本 / 子进程"一起清，
                        用于"端口被一个我不认识的 dsh 占着"的排障场景。

    安全边界与管理器的停止逻辑一致：只认命令行里出现
    @deepseek-ai/dsh 的 node 进程（SIG_ANY），绝不碰其它应用的 node。
    """
    _json = getattr(args, "json", False)
    try:
        sys.path.insert(0, BASE_DIR)
        import dsh_kill
    except Exception as e:
        if _json:
            _emit({"schema_version": SCHEMA_VERSION, "command": "kill-stray",
                   "ok": False, "executed": False, "count": 0, "changed": [],
                   "slots": [], "errors": [{"key": None,
                                            "code": "module_unavailable",
                                            "detail": str(e)}]})
            return EXIT_FAIL
        sys.stderr.write(T("[错误] 无法加载 dsh_kill：%s\n") % e)
        return EXIT_FAIL
    if not _VERBOSE.get("on"):
        dsh_kill.LOG = lambda *_a, **_k: None
    versions = {}
    for k in ctx.keys:
        c = ctx.slot(k)
        versions[k] = {"sig": _make_sig(c), "port": c.get("port")}
    sig_any = "@deepseek-ai/dsh"
    if not getattr(args, "yes", False):
        if _json:
            # 缺省只出计划，不杀任何进程。`slots` 与其它命令一样是对象数组，
            # 这里是执行前的状态快照；agent 看 `executed` / `requires_confirm`。
            _emit({"schema_version": SCHEMA_VERSION, "command": "kill-stray",
                   "ok": True, "executed": False, "dry_run": True,
                   "requires_confirm": True, "managed_slots": len(versions),
                   "sig_any": sig_any, "count": len(ctx.keys),
                   "changed": [], "slots": [_slot_json(ctx, k) for k in ctx.keys],
                   "errors": []})
            return EXIT_OK
        print(T("将清理：本管理器管的 %d 个槽 + 所有命令行含 %s 的 node 进程。")
              % (len(versions), sig_any))
        print(T("（只杀 dsh，不碰其它软件的 node 进程）"))
        print(T("确认执行请加 --yes"))
        return EXIT_OK
    before = {k: _is_running(ctx, k) for k in ctx.keys}
    try:
        dsh_kill.kill_all(versions, sig_any=sig_any)
    except Exception as e:
        if _json:
            _emit({"schema_version": SCHEMA_VERSION, "command": "kill-stray",
                   "ok": False, "executed": False, "count": 0, "changed": [],
                   "slots": [], "errors": [{"key": None, "code": "kill_failed",
                                            "detail": str(e)}]})
            return EXIT_FAIL
        sys.stderr.write(T("[错误] 清理失败：%s\n") % e)
        return EXIT_FAIL
    time.sleep(0.8)
    if _json:
        changed = [k for k in ctx.keys if before[k] != _is_running(ctx, k)]
        _emit({"schema_version": SCHEMA_VERSION, "command": "kill-stray",
               "ok": True, "executed": True, "dry_run": False,
               "count": len(ctx.keys), "changed": changed,
               "slots": [_slot_json(ctx, k) for k in ctx.keys], "errors": []})
        return EXIT_OK
    print(T("已清理。当前状态："))
    return cmd_list(ctx, args)


def _selected_keys(ctx, args):
    """只读命令选槽：给了槽名就只选登记过的，没给就选全部。**不写 stderr**。

    2026-11-15 修：`cmd_url` / `cmd_health` 的非 JSON 分支以前用了只在 JSON
    分支里赋值的 `keys`，不带 `--json` 时直接 UnboundLocalError。两个分支
    现在共用这里；未知槽名在非 JSON 分支另外提示（JSON 模式保持 stdout 干净）。
    2026-11-15 地基审计：把被忽略的槽名记进 `_UNKNOWN_SLOTS`，由 `_emit()`
    显式输出 `unknown_slots[]`，agent 不会再拿到一份看不出问题的空报告。
    """
    given_all = list(getattr(args, "slot", None) or [])
    _UNKNOWN_SLOTS[:] = [k for k in given_all if k not in ctx.versions]
    given = [k for k in given_all if k in ctx.versions]
    return given or list(ctx.keys)


def _json_target_keys(ctx, args):
    """写操作在 JSON 模式下选槽，返回 (keys, err_dict)。

    与只读命令不同：`start` / `stop` 不给槽名时**不能默认全开**（会误启动
    所有版本），必须显式给槽名或 `--all`。err_dict 用固定英文枚举，供 agent
    判断；命中时由 `_usage_emit()` 输出结构化用法错误（退出码 2）。
    """
    slots = list(getattr(args, "slot", None) or [])
    unknown = [s for s in slots if s not in ctx.versions]
    if unknown:
        return [], {"error": "unknown_slot", "unknown": unknown,
                    "available": list(ctx.keys)}
    if getattr(args, "all", False):
        return list(ctx.keys), None
    given = [k for k in slots if k in ctx.versions]
    if not given:
        return [], {"error": "no_target", "available": list(ctx.keys)}
    return given, None


def _target_keys(ctx, args):
    """决定要操作哪些槽：--all / 指定槽名 / 缺省报错。"""
    if getattr(args, "all", False):
        if not ctx.keys:
            sys.stderr.write(T("[错误] 当前没有任何槽\n"))
            return []
        return list(ctx.keys)
    if not args.slot:
        sys.stderr.write(T("[错误] 请指定槽名（或用 --all）\n"))
        sys.stderr.write(T("可用槽：%s\n") % ("、".join(ctx.keys) or T("（无）")))
        return []
    keys = [s for s in args.slot if s in ctx.versions]
    for s in args.slot:
        if s not in ctx.versions:
            sys.stderr.write(T("[错误] 没有这个槽：%s\n") % s)
    return keys


def app_version():
    """程序版本：与 GUI 同源 —— 读仓库根的 `VERSION` 文件（2026-11-14）。

    以前版本号只存在于 config.json（每台机器一份），换台机器就显示兜底值；
    现在版本跟代码一起提交，`--version` 和界面标题栏读到的是同一个数。
    """
    try:
        with open(os.path.join(BASE_DIR, "VERSION"), "r", encoding="utf-8-sig") as f:
            v = (f.read() or "").strip()
        if v:
            return v
    except Exception:
        pass
    return "0.0.0"


def build_parser():
    p = argparse.ArgumentParser(
        prog="dsh-tm",
        description=T("dsh-tm 命令行接口（与图形界面共用同一份 config.json）"),
        epilog=T("示例：python dsh-tm.py start --all / python dsh-tm.py list"))
    p.add_argument("--config", default=CONFIG_PATH, help=T("配置文件路径（缺省同目录 config.json）"))
    p.add_argument("--lang", choices=("zh", "en"),
                   help=T("输出语言（缺省跟随 config.json 的 ui.lang）"))
    p.add_argument("--json", action="store_true",
                   help=T("输出机器可读 JSON（字段名与枚举值固定英文，不随界面语言变）"))
    p.add_argument("--verbose", action="store_true", help=T("打印底层详细过程（排障用）"))
    p.add_argument("--version", action="version", version="dsh-tm " + app_version(),
                   help=T("打印版本号后退出"))
    sub = p.add_subparsers(dest="cmd")

    for name, help_text in (("list", T("列出所有槽与运行状态")),
                            ("status", T("同 list，可指定槽")),
                            ("url", T("打印入口地址（不含 token）")),
                            ("health", T("槽体检（只读）")),
                            ("doctor", T("综合诊断（只读）：配置 + 体检 + 槽形状 + 建议动作"))):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("slot", nargs="*", help=T("槽名（如 v1）；可给多个"))
        sp.add_argument("--all", action="store_true", help=T("对所有槽执行"))
        if name in ("health", "doctor"):
            sp.add_argument("--deep", action="store_true", help=T("深度体检"))

    for name, help_text in (("start", T("启动槽（支持 --all）")),
                            ("stop", T("停止槽（支持 --all）"))):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("slot", nargs="*", help=T("槽名（如 v1）；可给多个"))
        sp.add_argument("--all", action="store_true", help=T("对全部槽执行"))
        if name == "start":
            sp.add_argument("--wait", type=float, default=20.0, metavar=T("秒"),
                            help=T("等待端口监听的超时秒数（缺省 20）"))

    sp = sub.add_parser(
        "kill-stray",
        help=T("清理槽外 dsh 残留（排障用；旧版界面按钮已移除）"))
    sp.add_argument("--yes", action="store_true", help=T("确认执行（不加只打印计划）"))

    return p


def main(argv=None):
    # 语言必须在**建 parser 之前**定：`help=` / `description=` 是构建时求值的，
    # 而 `--help` 在 parse_args 里立刻打印并退出，等解析完再 set_lang 就来不及了。
    _argv = list(sys.argv[1:] if argv is None else argv)
    # `--lang` / `--json` 允许写在子命令之后（`list --json` 比 `--json list` 自然），
    # 但 argparse 只认子命令之前的全局参数 —— 这里先把它们挪到最前面。
    _pre, _i = [], 0
    while _i < len(_argv):
        _a = _argv[_i]
        if _a == "--json":
            _pre.append(_a); _argv.pop(_i); continue
        if _a == "--lang" and _i + 1 < len(_argv):
            _pre += [_a, _argv[_i + 1]]; del _argv[_i:_i + 2]; continue
        if _a.startswith("--lang="):
            _pre.append(_a); _argv.pop(_i); continue
        _i += 1
    _argv = _pre + _argv
    for _i, _a in enumerate(_argv):
        if _a == "--lang" and _i + 1 < len(_argv):
            I18N.set_lang(_argv[_i + 1])
            break
        if _a.startswith("--lang="):
            I18N.set_lang(_a.split("=", 1)[1])
            break
    parser = build_parser()
    args = parser.parse_args(_argv)
    if not args.cmd:
        parser.print_help()
        return EXIT_USAGE
    # 显式 --lang 优先；否则跟随 config.json 的 ui.lang（--config 可能指到别处）
    if getattr(args, "lang", None):
        I18N.set_lang(args.lang)
    else:
        _adopt_config_lang(args.config)
    _VERBOSE["on"] = bool(getattr(args, "verbose", False))
    # 显式 --config 是调用方「我知道文件在哪」的声明：文件不存在就是错误，
    # 绝不能自动生成一份零槽配置把路径拼错伪装成「这台机器没装版本」。
    _explicit_cfg = (os.path.normcase(os.path.abspath(args.config))
                     != os.path.normcase(os.path.abspath(CONFIG_PATH)))
    ctx = Ctx(_cfg_or_die(args.config,
                          json_mode=bool(getattr(args, "json", False)),
                          command=args.cmd, auto_create=not _explicit_cfg))
    handlers = {
        "list": cmd_list, "status": cmd_status, "start": cmd_start,
        "stop": cmd_stop, "url": cmd_url, "health": cmd_health,
        "doctor": cmd_doctor, "kill-stray": cmd_kill_stray,
    }
    _gate_registry(handlers)   # 新增命令忘了分类 → 当场失败，不会静默绕过
    return handlers[args.cmd](ctx, args)


if __name__ == "__main__":
    sys.exit(main())
