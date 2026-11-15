# -*- coding: utf-8 -*-
"""离线回归：配置层地基（dsh_config_guard）+ doctor --json 契约。

2026-11-15 地基审计确认并修掉的两个真问题，必须有回归守着：

  1. CLI 的 `_cfg_or_die()` 把类型写坏的 `versions` 静默改成 `{}`，
     损坏配置被伪装成「零槽」，agent 会据此误判这台机器没装过任何版本；
  2. GUI 启动前会 fatal 拦住 port 与 extra_args --port 不一致，CLI 完全没有
     这道校验，而 `start --json` / `list --json` 正是用 port 判断状态。

外加：`--json` 遇到坏配置时 stdout 必须是**一个** JSON 对象（以前是空的），
以及 `doctor` 的固定英文 code / action 不随 --lang 漂移。

全部离线的做法：假配置写在系统临时目录，端口用 59999、bin.js 指向不存在的
路径；`start --json` 被配置闸门挡住或 spawn 失败，**不会启动/杀死任何进程**。
"""
import ast
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, os.pardir))
CLI = os.path.join(REPO, "dsh-tm.py")
GUARD = os.path.join(REPO, "dsh_config_guard.py")
GUI = os.path.join(REPO, "dsh_manager.pyw")

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


spec = importlib.util.spec_from_file_location("dsh_config_guard_test", GUARD)
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)

print("== A. 共享配置判据（纯函数）==")


def slot(port=4080, args=("--port", "4080")):
    c = {"label": "fake", "port": port,
         "bin_js": "<base>/dshV1/node_modules/@deepseek-ai/dsh/lib/bin.js"}
    if args is not None:
        c["extra_args"] = ["web"] + list(args)
    return c


def codes(cfg, max_slots=6):
    return [p["code"] for p in G.validate_config(cfg, max_slots=max_slots)]


clean = {"install_base": "<home>", "versions": {"v1": slot()}}
chk("A1 干净配置零问题", codes(clean) == [], codes(clean))
chk("A2 零槽配置合法（全新 clone 的正常起点）",
    codes({"versions": {}}) == [], codes({"versions": {}}))
chk("A3 validate_config 不改原配置",
    json.dumps(clean, sort_keys=True) == json.dumps(
        {"install_base": "<home>", "versions": {"v1": slot()}}, sort_keys=True))

bad = {"versions": []}
chk("A4 versions 写成数组 → versions_not_object（不再静默清零）",
    codes(bad) == ["versions_not_object"], codes(bad))
chk("A5 versions 类型错是阻断级", G.fatal(G.validate_config(bad)) != [])
chk("A6 缺 versions 段 → versions_missing（零槽与配置损坏必须能区分）",
    "versions_missing" in codes({"install_base": "<home>"}))
chk("A7 顶层不是对象 → config_not_object",
    codes([1, 2]) == ["config_not_object"])
chk("A8 单个槽不是对象 → slot_not_object",
    "slot_not_object" in codes({"versions": {"v1": "oops"}}))
chk("A9 槽数超上限（边界显式传 max_slots，不验产品默认值）",
    "too_many_slots" in codes({"versions": dict((str(i), slot(4080 + i))
                                                 for i in range(5))}, max_slots=4))

chk("A10 一致时无告警",
    "port_mismatch" not in codes({"versions": {"v1": slot()}}))
chk("A11 port 与 --port 不一致 → port_mismatch + 结构化期望值",
    "port_mismatch" in codes({"versions": {"v1": slot(4080, ("--port", "4081"))}}))
_pm = [p for p in G.validate_config({"versions": {"v1": slot(4080, ("--port", "4081"))}})
       if p["code"] == "port_mismatch"]
chk("A12 port_mismatch 带 expected/actual（agent 不用解析中文）",
    bool(_pm) and _pm[0].get("expected") == 4080 and _pm[0].get("actual") == 4081)
chk("A13 缺 --port → port_arg_missing",
    "port_arg_missing" in codes({"versions": {"v1": slot(4080, ())}}))
chk("A14 --port 值非法 → port_arg_invalid",
    "port_arg_invalid" in codes({"versions": {"v1": slot(4080, ("--port", "abc"))}}))
chk("A15 --port=4080 写法也认（GUI 老实现漏了）",
    "port_mismatch" not in codes({"versions": {"v1": slot(4080, ("--port=4080",))}}))
chk("A16 extra_args 整个没写 → 由 CLI 合成默认参数，不算错",
    "port_arg_missing" not in codes({"versions": {"v1": slot(4080, None)}}))
chk("A17 extra_args 写成字符串 → extra_args_not_list",
    "extra_args_not_list" in codes(
        {"versions": {"v1": dict(slot(), extra_args="web --port 4080")}}))
chk("A18 port 缺失/非法/写成字符串分别报不同码",
    "port_missing" in codes({"versions": {"v1": dict(slot(), port=None)}})
    and "port_invalid" in codes({"versions": {"v1": dict(slot(), port="abc")}})
    and "port_is_string" in codes({"versions": {"v1": dict(slot(), port="4080")}}))
_dup = G.validate_config({"versions": {"v1": slot(4080), "v2": slot(4080)}})
chk("A19 两槽抢同一端口 → duplicate_port 带 conflicts_with",
    any(p["code"] == "duplicate_port" and p.get("conflicts_with") == "v1"
        for p in _dup), [p["code"] for p in _dup])
chk("A20 端口类问题不阻断读命令（doctor 才看得见）",
    not G.fatal(_pm))

# GUI 的 _check_port_consistency 老接口形状：[(key, 说明), ...]
pp = G.port_problems({
    "v1": {"port": 4080, "extra_args": ["web", "--port", "4080"]},
    "v2": {"port": 5080, "extra_args": ["web", "--port", "5081"]},
    "v3": {"port": 7080, "extra_args": ["web"]},
})
chk("A21 port_problems 保持 GUI 老形状，只报有问题的槽",
    sorted(k for k, _ in pp) == ["v2", "v3"], pp)
chk("A22 GUI 报错文案点明实际端口（B4 的判据）",
    any("5081" in m for _, m in pp), pp)

print()
print("== B. 三处入口共用同一份判据（静态）==")
cli_src = io.open(CLI, encoding="utf-8").read()
gui_src = io.open(GUI, encoding="utf-8").read()
chk("B1 CLI 导入 dsh_config_guard", "import dsh_config_guard as G" in cli_src)
# 用 AST 而不是字符串查找：函数说明里会引用这段历史代码，字符串匹配会误报。
_tree = ast.parse(cli_src)
_has_silent_reset = any(
    isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
        and t.value.id == "cfg" and isinstance(t.slice, ast.Constant)
        and t.slice.value == "versions" for t in n.targets)
    for n in ast.walk(_tree))
chk("B2 CLI 不再静默清空 versions（AST 查赋值语句）", not _has_silent_reset)
chk("B3 CLI 走 validate_config 且按 MAX_SLOTS 校验",
    "G.validate_config(cfg, max_slots=MAX_SLOTS)" in cli_src)
chk("B4 CLI 所有 --json 输出都带 config_problems",
    'setdefault("config_problems"' in cli_src)
chk("B5 CLI 缺配置在 --json 下也输出 JSON（stdout 不为空）",
    "def _config_error(" in cli_src and "json_mode" in cli_src)
chk("B6 写操作按配置闸门拒绝（start / stop 两处）",
    cli_src.count("_blocked_by_config(keys)") >= 4,
    cli_src.count("_blocked_by_config(keys)"))
chk("B7 GUI 转发到共享模块，不再自己实现一份",
    "import dsh_config_guard as G" in gui_src
    and "return G.port_problems(VC)" in gui_src
    and "def _check_port_consistency" in gui_src)
chk("B8 GUI 仍保留启动前 fatal 拦截",
    "PORT_MISMATCH = _check_port_consistency()" in gui_src and "_fatal(" in gui_src)
chk("B9 doctor 命令存在且接进 parser / handler",
    '"doctor": cmd_doctor' in cli_src and '("doctor", T(' in cli_src)

print()
print("== C. 实跑：坏配置、端口闸门、doctor 契约 ==")
_FDIR = tempfile.mkdtemp(prefix="dsh-config-guard-test-")


def _run(cfg, *argv):
    p = os.path.join(_FDIR, "config.json")
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    code = ("import sys, runpy; "
            "sys.path.insert(0, %r); "
            "sys.argv = [%r, '--config', %r] + %r; "
            "runpy.run_path(%r, run_name='__main__')"
            % (REPO, CLI, p, list(argv), CLI))
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60)
    return r.returncode, r.stdout, r.stderr


def _json_of(out):
    return json.loads(out)          # stdout 里不止一个对象时会直接失败


try:
    broken = {"install_base": "<self>", "versions": [1, 2]}
    rc, out, err = _run(broken, "list", "--json")
    o = _json_of(out)
    chk("C1 坏配置 --json 仍给单个 JSON 对象、退出码 3",
        rc == 3 and o["ok"] is False and o["config_valid"] is False,
        "rc=%s" % rc)
    chk("C2 坏配置给固定英文 code，不伪装成零槽",
        (o.get("errors") or [{}])[0].get("code") == "versions_not_object"
        and o.get("count") == 0)
    rc, out, err = _run(broken, "list")
    chk("C3 人读模式坏配置只写 stderr、stdout 为空",
        rc == 3 and not out
        and ("配置不可用" in err or "config unusable" in err), repr(err[:80]))

    mismatch = {"install_base": "<self>", "versions": {
        "v1": {"label": "fake", "port": 59999,
               "bin_js": "<self>/__no_such__/node_modules/@deepseek-ai/dsh/lib/bin.js",
               "node_exe": "<self>/__no_such__/node.exe",
               "extra_args": ["web", "--port", "59998"]}}}
    rc, out, _ = _run(mismatch, "list", "--json")
    o = _json_of(out)
    chk("C4 端口不一致时读命令照常出报告，并附 config_problems",
        rc == 0 and o["ok"] is True
        and any(p["code"] == "port_mismatch" for p in o["config_problems"]))
    rc, out, _ = _run(mismatch, "start", "v1", "--json", "--wait", "0")
    o = _json_of(out)
    chk("C5 start 拒绝端口不可信的槽（config_port_invalid，不 spawn）",
        o["ok"] is False
        and (o.get("slots") or [{}])[0].get("error_code") == "config_port_invalid",
        str(o.get("slots"))[:120])
    rc, out, _ = _run(mismatch, "stop", "v1", "--json")
    o = _json_of(out)
    chk("C6 stop 同样拒绝（不会按错端口去杀进程）",
        (o.get("slots") or [{}])[0].get("error_code") == "config_port_invalid")

    rc, out, _ = _run(mismatch, "doctor", "--json")
    o = _json_of(out)
    need = ("schema_version", "command", "ok", "count", "slots", "errors",
            "level", "config_valid", "config_issues", "global_issues",
            "summary", "config_problems")
    chk("C7 doctor --json 顶层契约齐", all(k in o for k in need),
        [k for k in need if k not in o])
    chk("C8 doctor 每条问题带固定英文 code / source / action",
        all(set(("level", "source", "code", "action")) <= set(i)
            for i in o["config_issues"] + o["global_issues"]
            + [x for s in o["slots"] for x in s["issues"]]))
    chk("C9 doctor 把端口问题转成建议动作 fix_config",
        "fix_config" in (o["slots"][0]["actions"] if o["slots"] else []),
        o["slots"][0]["actions"] if o["slots"] else "no slots")
    chk("C10 doctor 是只读命令：ok=true 但 level=error、退出码 1",
        o["ok"] is True and o["level"] == "error" and rc == 1,
        "ok=%s level=%s rc=%s" % (o["ok"], o["level"], rc))
    rc, out, _ = _run(mismatch, "--lang", "en", "doctor", "--json")
    o2 = _json_of(out)
    chk("C11 --lang en 不改字段名/枚举值",
        all(k in o2 for k in need)
        and o2["config_issues"][0]["code"] == "port_mismatch"
        and o2["slots"][0]["actions"] == o["slots"][0]["actions"])
    rc, out, err = _run(mismatch, "doctor")
    chk("C12 doctor 人读模式不崩、且不带 Traceback",
        "Traceback" not in err and ("结论" in out or "Verdict" in out),
        repr(err[:100]))
finally:
    shutil.rmtree(_FDIR, ignore_errors=True)

print()
print("RESULT: %s（%d 项，FAIL=%d）" % ("ALL PASS" if not FAIL else "FAILED", N, FAIL))
sys.exit(1 if FAIL else 0)
