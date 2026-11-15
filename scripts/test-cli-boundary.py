# -*- coding: utf-8 -*-
"""离线回归（第 18 套）：CLI 读写分界硬标记 + 四个地基修复。

2026-11-15 地基审计确认并修掉的四个真问题，必须有回归守着：

  1. `health --json` 在 dsh_health 加载不了时 stdout 是空的（0 字节），
     违反「一次调用一个 JSON 对象」；
  2. `health --json` 只有一条 info（如实装版本）时 level 被误报成 warning；
  3. 显式 `--config <不存在的路径>` 会静默生成一份零槽模板并回
     `ok:true, count:0` —— 路径拼错被伪装成「这台机器没装版本」，还写盘；
  4. 未知槽处理不一致：`status v9 --json` 给 0 槽，`url v9 --json` 给全部槽。

外加把 docs/agent-api.md §1 的三分类变成**代码事实**：CMD_CLASS 唯一分类表、
main() 注册闸门、每个 --json 顶层 effect、写命令 changed[]、文档表格对账、
CLI 永不出现 disk_write。

全部离线：假配置写在系统临时目录，端口 59999、bin.js 指向不存在的路径，
kill-stray 不加 --yes，故障注入只改 sys.modules。不启动/杀死任何进程，
不改仓库里任何文件（bootstrap 测试在临时目录里换掉模块级路径常量）。
"""
import argparse
import ast
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, os.pardir))
CLI = os.path.join(REPO, "dsh-tm.py")
DOC = os.path.join(REPO, "docs", "agent-api.md")
sys.path.insert(0, REPO)

FAIL = 0
N = 0


def chk(name, cond, detail=""):
    global FAIL, N
    N += 1
    if cond:
        print("PASS  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s%s" % (name, (" | " + str(detail)) if detail else ""))


def _raises(fn):
    try:
        fn()
        return False
    except Exception:
        return True


src = io.open(CLI, encoding="utf-8").read()
doc = io.open(DOC, encoding="utf-8").read()
tree = ast.parse(src)

spec = importlib.util.spec_from_file_location("dsh_tm_boundary", CLI)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# ── A. 分类表 / 注册闸门 / 文档对账（静态）─────────────────────────────
print("== A. CMD_CLASS 是唯一分类表 ==")


def _assigned(name, scope=None):
    for node in (scope.body if scope else tree.body):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
    return None


_cc_node = _assigned("CMD_CLASS")
try:
    classes = ast.literal_eval(_cc_node) if _cc_node is not None else None
except Exception:
    classes = None
chk("A1 CMD_CLASS 是可静态读出的字面量字典", isinstance(classes, dict), type(classes))
classes = classes or {}
chk("A2 分类值只允许 read / runtime_write（CLI 永不提供 disk_write）",
    set(classes.values()) <= {"read", "runtime_write"}
    and set(classes.values()) == {"read", "runtime_write"},
    sorted(set(classes.values())))
chk("A3 disk_write 不出现在 CLI 的分类表里",
    "disk_write" not in classes.values() and '"disk_write"' not in src)

_main = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == "main"), None)
_h_node = _assigned("handlers", _main) if _main else None
handler_keys = ([k.value for k in _h_node.keys
                 if isinstance(k, ast.Constant)] if _h_node else [])
chk("A4 main() 的 handler 表与 CMD_CLASS 一一对应（不多不少）",
    set(handler_keys) == set(classes),
    "handlers=%s classes=%s" % (sorted(handler_keys), sorted(classes)))
chk("A5 main() 调用 _gate_registry 后才分发命令",
    "_gate_registry(handlers)" in src and "handlers[args.cmd](ctx, args)" in src)
chk("A6 注册闸门能挡住漏分类 / 多余分类 / 非法分类",
    all(_raises(lambda: mod._gate_registry(h)) for h in (
        dict((k, None) for k in list(classes) + ["new-command"]),
        dict((k, None) for k in list(classes)[:-1]),
    )))
_bad_cls = dict(mod.CMD_CLASS)
_bad_cls["list"] = "disk_write"
_saved_cls = dict(mod.CMD_CLASS)
mod.CMD_CLASS.clear()
mod.CMD_CLASS.update(_bad_cls)
chk("A7 分类表里混进 disk_write 时闸门当场失败",
    _raises(lambda: mod._gate_registry(dict((k, None) for k in _bad_cls))))
mod.CMD_CLASS.clear()
mod.CMD_CLASS.update(_saved_cls)

_parser = mod.build_parser()
_sub = [a for a in _parser._actions if isinstance(a, argparse._SubParsersAction)]
parser_keys = set(_sub[0].choices) if _sub else set()
chk("A8 parser 注册的子命令集合与 CMD_CLASS 一致",
    parser_keys == set(classes),
    "parser=%s classes=%s" % (sorted(parser_keys), sorted(classes)))

lines = doc.splitlines()


def _row(prefix):
    hits = [l for l in lines if l.startswith(prefix)]
    return re.findall(r"`([a-z][a-z-]*)`", hits[0]) if hits else []


doc_read = _row("| **read** ")
doc_write = _row("| **runtime write** ")
doc_read_zh = _row("| **只读** ")
doc_write_zh = _row("| **改运行时** ")
expected_read = sorted(k for k, v in classes.items() if v == "read")
expected_write = sorted(k for k, v in classes.items() if v == "runtime_write")
chk("A9 英文文档分类表与 CMD_CLASS 一致",
    sorted(doc_read) == expected_read and sorted(doc_write) == expected_write,
    "read=%s write=%s" % (doc_read, doc_write))
chk("A10 中文文档分类表与 CMD_CLASS 一致",
    sorted(doc_read_zh) == expected_read and sorted(doc_write_zh) == expected_write,
    "read=%s write=%s" % (doc_read_zh, doc_write_zh))
chk("A11 文档明确写 disk write 只在 GUI / CLI 不提供",
    any("CLI does not expose" in l for l in lines if l.startswith("| **disk write** "))
    and any("CLI 不提供" in l for l in lines if l.startswith("| **改磁盘** ")))
chk("A12 CLI 源码里没有任何改磁盘函数 / 卸载模块",
    not any(b in src for b in (
        "run_official_install", "adopt_existing", "dsh_uninstall_ops",
        "dsh_adopt", "_force_rmtree", "shutil.rmtree", "os.remove(",
        "os.unlink(", "rmdir(")),
    [b for b in ("run_official_install", "adopt_existing", "dsh_uninstall_ops",
                 "dsh_adopt", "_force_rmtree", "shutil.rmtree", "os.remove(",
                 "os.unlink(", "rmdir(") if b in src])
chk("A13 doctor 只读复核只调 slot_shape_problems",
    "OPS.slot_shape_problems(" in src)
chk("A14 _emit 统一补 effect / changed[] / unknown_slots[]",
    'setdefault("effect", effect)' in src
    and 'setdefault("changed", [])' in src
    and 'setdefault("unknown_slots"' in src)
chk("A15 加字段不升 schema_version（仍为 1）", mod.SCHEMA_VERSION == 1)

# ── B. 实跑：effect / changed[] 契约 ──────────────────────────────────
print()
print("== B. 实跑：八个命令的 effect / changed[] ==")
TMP = tempfile.mkdtemp(prefix="dsh-cli-boundary-")
CFG = os.path.join(TMP, "config.json")
FAKE = {"install_base": "<self>", "versions": {
    "v1": {"label": "fake", "port": 59999,
           "node_exe": "<self>/__no_such__/node.exe",
           "bin_js": "<self>/__no_such__/node_modules/@deepseek-ai/dsh/lib/bin.js",
           "dsh_home": "<self>/__no_such__/home",
           "temp_dir": "<self>/__no_such__/tmp"}}}
with io.open(CFG, "w", encoding="utf-8") as f:
    json.dump(FAKE, f)


def run(cfg, *argv, **kw):
    """返回 (rc, stdout, stderr)；pre 可注入 sys.modules 故障。"""
    pre = kw.pop("pre", "")
    code = ("import sys, runpy; sys.path.insert(0, %r); " % REPO + pre +
            "sys.argv = [%r, '--config', %r] + %r; "
            "runpy.run_path(%r, run_name='__main__')"
            % (CLI, cfg, list(argv), CLI))
    p = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=90)
    return p.returncode, p.stdout, p.stderr


def jobj(out):
    return json.loads(out)      # stdout 里不止一个对象 / 为空时直接失败


CASES = [("list", []), ("status", []), ("url", []), ("health", []),
         ("doctor", []), ("start", ["v1", "--wait", "0"]),
         ("stop", ["v1"]), ("kill-stray", [])]
bad_effect = []
for cmd, extra in CASES:
    try:
        rc, out, err = run(CFG, cmd, *(extra + ["--json"]))
        o = jobj(out)
        want = classes.get(cmd)
        if not (o.get("effect") == want and o.get("command") == cmd
                and "Traceback" not in err):
            bad_effect.append("%s:effect=%s(want %s)" % (cmd, o.get("effect"), want))
        if want == "runtime_write":
            if not isinstance(o.get("changed"), list):
                bad_effect.append("%s:changed 缺失/不是数组" % cmd)
        elif "changed" in o:
            bad_effect.append("%s:只读命令不该有 changed" % cmd)
    except Exception as e:
        bad_effect.append("%s:%s" % (cmd, str(e)[:70]))
chk("B1 八个命令 --json 的 effect 都等于 CMD_CLASS", not bad_effect,
    "; ".join(bad_effect[:4]))
chk("B2 写命令都有 changed[]、只读命令都没有", not bad_effect)

rc, out, _ = run(CFG, "start", "--json")
o = jobj(out)
chk("B3 用法错误（no_target）也带 effect / changed[]",
    rc == 2 and o.get("effect") == "runtime_write"
    and isinstance(o.get("changed"), list))
rc, out, _ = run(CFG, "--lang", "en", "doctor", "--json")
o = jobj(out)
chk("B4 --lang en 不改 effect 枚举", o.get("effect") == "read")

_broken_cfg = os.path.join(TMP, "broken.json")
with io.open(_broken_cfg, "w", encoding="utf-8") as f:
    json.dump({"versions": [1, 2]}, f)
rc, out, _ = run(_broken_cfg, "list", "--json")
o = jobj(out)
chk("B5 配置错误（退出 3）也带 effect，stdout 仍是一个 JSON",
    rc == 3 and o.get("effect") == "read" and o.get("ok") is False)

# ── C. 实跑：四个地基修复 ─────────────────────────────────────────────
print()
print("== C. 实跑：地基修复 ==")
_bad_mod = "sys.modules['dsh_health'] = None; "
rc, out, err = run(CFG, "health", "--json", pre=_bad_mod)
try:
    o = jobj(out)
    chk("C1 health 模块加载失败仍输出单个 JSON（module_unavailable）",
        rc == 1 and o["ok"] is False
        and (o.get("errors") or [{}])[0].get("code") == "module_unavailable"
        and o.get("effect") == "read", repr(out[:80]))
except Exception as e:
    chk("C1 health 模块加载失败仍输出单个 JSON（module_unavailable）",
        False, "stdout=%r (%s)" % (out[:80], e))

_stub = ("import types; m = types.ModuleType('dsh_health'); "
         "m.check_slot = lambda *a, **k: [('提示', '实装版本', '0.1.6')]; "
         "m.check_all = lambda *a, **k: {}; "
         "sys.modules['dsh_health'] = m; ")
rc, out, _ = run(CFG, "health", "--json", pre=_stub)
o = jobj(out)
chk("C2 只有 info 条目时 level=ok（不再误报 warning）",
    rc == 0 and o.get("level") == "ok"
    and (o.get("slots") or [{}])[0].get("level") == "ok", o.get("level"))

_ghost = os.path.join(TMP, "ghost.json")
_before = os.path.exists(_ghost)
rc, out, err = run(_ghost, "list", "--json")
try:
    o = jobj(out)
    chk("C3 显式 --config 缺文件：不写盘、rc=3、config_unreadable",
        not _before and not os.path.exists(_ghost) and rc == 3
        and o.get("ok") is False
        and (o.get("errors") or [{}])[0].get("code") == "config_unreadable",
        "exists=%s rc=%s" % (os.path.exists(_ghost), rc))
except Exception as e:
    chk("C3 显式 --config 缺文件：不写盘、rc=3、config_unreadable", False, str(e))

_unknown = []
for cmd in ("status", "url", "health", "doctor"):
    rc, out, err = run(CFG, cmd, "v9", "--json")
    try:
        o = jobj(out)
        _unknown.append((cmd, o.get("unknown_slots"), o.get("count"),
                         "Traceback" in err))
    except Exception as e:
        _unknown.append((cmd, "parse-error", None, str(e)[:50]))
chk("C4 未知槽：四个只读命令统一给 unknown_slots[] 且 stdout 干净",
    all(u[1] == ["v9"] and u[2] == 1 and u[3] is False for u in _unknown),
    str(_unknown))

# 默认路径 bootstrap 仍保留：换掉模块级常量，只在临时目录里造文件。
_boot = tempfile.mkdtemp(prefix="dsh-cli-bootstrap-")
_saved = (mod.CONFIG_PATH, mod.EXAMPLE_PATH)
_example = os.path.join(_boot, "example.json")
with io.open(_example, "w", encoding="utf-8") as f:
    json.dump({"versions": {}}, f)
mod.EXAMPLE_PATH = _example
mod.CONFIG_PATH = os.path.join(_boot, "config.json")
try:
    # bootstrap 的提示 / 拒绝原因走 stderr 且随 ui.lang 变；回归不解析文案，
    # 顺手重定向掉，免得把它当成失败信号或让总检输出飘。
    with contextlib.redirect_stderr(io.StringIO()):
        _cfg = mod._cfg_or_die(mod.CONFIG_PATH)
    chk("C5 默认 config.json 缺失仍从模板生成（与 GUI 一致）",
        os.path.exists(mod.CONFIG_PATH) and _cfg.get("versions") == {})
    _explicit = os.path.join(_boot, "explicit-missing.json")
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            mod._cfg_or_die(_explicit, auto_create=False)
        _refused = False
    except SystemExit as e:
        _refused = (e.code == 3)
    chk("C6 auto_create=False 时拒绝执行、不生成任何文件",
        _refused and not os.path.exists(_explicit))
finally:
    mod.CONFIG_PATH, mod.EXAMPLE_PATH = _saved
    shutil.rmtree(_boot, ignore_errors=True)

chk("C7 人读分支仍走 _target_keys（未知槽给 stderr 提示，行为未变）",
    "_target_keys(ctx, args)" in src and "_selected_keys(ctx, args)" in src)

# ── D. 文档契约同步 ───────────────────────────────────────────────────
print()
print("== D. 文档同步 ==")
for token in ("effect", "changed[]", "runtime_write", "disk_write",
              "unknown_slots", "config_unreadable", "module_unavailable"):
    chk("D: agent-api.md 写到 %s" % token, token in doc)
chk("D8 文档说明 effect 是命令类别而非副作用证明",
    "not proof" in doc and "不是本次调用" in doc)

# ── E. 人读版 health 的退出码不能随界面语言漂移（2026-11-15 真跑复现） ──
# 坏槽（bin_js / node_exe 都不存在）在英文下照样打印 [error]，但过去
# `if lv == "错误"` 让 bad 恒为 0 → 退出码 0，脚本 / CI 会把坏槽误判成健康。
print()
print("== E. 人读版 health 退出码中英一致 ==")
_rc_map = {}
for _lg in ("zh", "en"):
    for _kind, _extra in (("plain", []), ("json", ["--json"])):
        _rc, _out, _err = run(CFG, *(["--lang", _lg, "health"] + _extra))
        _rc_map[(_lg, _kind)] = _rc
        _marked = ("[错误]" in _out or "[error]" in _out
                   or '"level": "error"' in _out)
        chk("E1 %s / %s：报了 error 就退出 1" % (_lg, _kind),
            _rc == 1 and _marked, "rc=%s marked=%s" % (_rc, _marked))
chk("E2 四种组合退出码完全一致", len(set(_rc_map.values())) == 1, str(_rc_map))
chk("E3 源码里不再拿中文字面量比级别",
    'lv == "错误"' not in src and '_lv_en(lv) == "error"' in src)

shutil.rmtree(TMP, ignore_errors=True)
print()
print("RESULT: %s（%d 项，FAIL=%d）" % ("ALL PASS" if not FAIL else "FAILED", N, FAIL))
sys.exit(1 if FAIL else 0)
