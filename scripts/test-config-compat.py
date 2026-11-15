# -*- coding: utf-8 -*-
"""离线回归（第 19 套）：config.json 跨版本兼容。

守什么
------
`config.json` 是这台机器上所有槽的唯一真相，而且**不在 git 里** —— 写坏了
没有回滚。升级 dsh-tm 时，旧版本写出的配置必须还能被新版本正确读取：
缺字段走合理默认、未知字段原样保留、坏数据明确报错（而不是伪装成零槽）。

覆盖
----
  A. 缺字段：默认值合理，且区分「没写 extra_args」与「写了但没带 --port」
  B. 写回保真：未知顶层/槽字段、中文值、带 BOM 的文件，一轮读写后不丢不乱
  C. 旧版样本：0.1.4 风格的配置能被 list / status / doctor 正常读取
  D. 写路径：移出 / 接回 / 卸载都是「先读整份再写回」，不是构造新字典

跑法：python scripts/test-config-compat.py   （退出码 0 = 全过）
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import dsh_config_guard as G      # noqa: E402
import dsh_install_ops as O       # noqa: E402

CLI = os.path.join(REPO, "dsh-tm.py")
FAILS = []


def chk(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name
          + ((" | " + str(detail)) if detail else ""))
    if not cond:
        FAILS.append(name)


def codes(problems):
    return sorted(p["code"] for p in problems)


TMP = tempfile.mkdtemp(prefix="dsh-cfg-compat-")

# ══════════════════════════════════════════════════════════════════
# A. 缺字段：默认值合理
# ══════════════════════════════════════════════════════════════════
print("== A. 缺字段 ==")

# 0.1.4 时代最精简的槽：只有入口和端口，没有 browser / temp_dir / extra_args
_min = {"versions": {"v1": {
    "port": 4080,
    "bin_js": "<base>/dsh-versions/dshV1/node_modules/@deepseek-ai/dsh/lib/bin.js",
    "node_exe": "<base>/dsh-versions/dshV1/node/node.exe",
    "dsh_home": "<base>/dsh-versions/dshV1/.dsh-home"}}}
_probs = G.validate_config(_min)
chk("A1 只写 5 个字段的槽没有任何 blocking 问题",
    not G.fatal(_probs), codes(_probs))
chk("A2 没写 extra_args 不算「缺 --port」（由构造保证一致）",
    "port_arg_missing" not in codes(_probs), codes(_probs))
_no_port = G.validate_config({"versions": {"v1": {"bin_js": "x", "node_exe": "y"}}})
chk("A3 缺 port 会报 port_missing（doctor 能看见）",
    "port_missing" in codes(_no_port), codes(_no_port))
chk("A4 缺 port 且没写 extra_args 时不会误报 port_arg_missing",
    "port_arg_missing" not in codes(_no_port), codes(_no_port))
_str_port = G.validate_config(
    {"versions": {"v1": {"port": "4080", "bin_js": "x"}}})
chk("A5 port 写成字符串只警告、不阻断",
    not G.fatal(_str_port) and "port_is_string" in codes(_str_port),
    codes(_str_port))

# ══════════════════════════════════════════════════════════════════
# B. 写回保真：读 -> 改一个字段 -> 写回 -> 再读
# ══════════════════════════════════════════════════════════════════
print("== B. 写回保真 ==")
cfg_path = os.path.join(TMP, "config.json")
old = {
    "install_base": "<home>",
    "icon": "assets/dsh-icon-violet.ico",
    "schema_version": 2,                  # 未来版本才认识的字段
    "created_at": "2026-01-01T00:00:00",  # 同上
    "versions": {
        "v1": {"label": "dshV1", "port": 4080,
               "bin_js": "<base>/x/bin.js", "node_exe": "<base>/x/node.exe",
               "dsh_home": "<base>/x/.dsh-home", "temp_dir": "<base>/x/.tmp",
               "browser": "default",
               "extra_args": ["web", "--port", "4080"],
               "created_at": "2026-01-01", "note": "中文说明 ✅"},
    },
}
with io.open(cfg_path, "w", encoding="utf-8", newline="\n") as f:
    json.dump(old, f, ensure_ascii=False, indent=2)

data = O.read_config(cfg_path)
data["versions"]["v1"]["port"] = 4081
O.write_config_atomic(cfg_path, data)
back = O.read_config(cfg_path)
chk("B1 未知顶层字段经过读-改-写后仍在",
    back.get("schema_version") == 2
    and back.get("created_at") == "2026-01-01T00:00:00",
    sorted(back))
chk("B2 未知槽字段经过读-改-写后仍在",
    back["versions"]["v1"].get("created_at") == "2026-01-01",
    sorted(back["versions"]["v1"]))
chk("B3 中文 / 非 ASCII 值往返不乱码",
    back["versions"]["v1"].get("note") == "中文说明 ✅",
    repr(back["versions"]["v1"].get("note")))
chk("B4 改一个字段不影响其它字段",
    back["versions"]["v1"]["port"] == 4081
    and back["versions"]["v1"]["browser"] == "default"
    and back["install_base"] == "<home>")
chk("B5 原子写不留下 .tmp-* 临时文件",
    not [n for n in os.listdir(TMP) if ".tmp-" in n], os.listdir(TMP))

bom_path = os.path.join(TMP, "bom.json")
with io.open(bom_path, "w", encoding="utf-8-sig", newline="\n") as f:
    json.dump({"versions": {"v1": {"port": 4080}}}, f)
chk("B6 带 BOM 的配置也能读（Windows 记事本写过）",
    (O.read_config(bom_path).get("versions") or {})
    .get("v1", {}).get("port") == 4080)

# ══════════════════════════════════════════════════════════════════
# C. 旧版样本：CLI 真跑（只读命令，不启停任何进程）
# ══════════════════════════════════════════════════════════════════
print("== C. 旧版样本 ==")


def run_cli(cfg, *args):
    code = ("import sys, runpy; sys.path.insert(0, %r); "
            "sys.argv = [%r, '--config', %r, '--lang', 'zh'] + %r; "
            "runpy.run_path(%r, run_name='__main__')"
            % (REPO, CLI, cfg, list(args), CLI))
    p = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=90)
    return p.returncode, p.stdout, p.stderr


def jobj(out):
    try:
        return json.loads(out)
    except Exception as e:
        return {"_err": str(e)[:80]}


LEGACY = {
    "install_base": "<self>",
    "versions": {
        "v1": {"label": "dshV1", "port": 4080,
               "node_exe": "<self>/__nope__/node.exe",
               "bin_js": "<self>/__nope__/node_modules/@deepseek-ai/dsh/lib/bin.js",
               "dsh_home": "<self>/__nope__/.dsh-home"},
        "v2": {"label": "dshV2", "port": 5080,
               "node_exe": "<self>/__nope__/node.exe",
               "bin_js": "<self>/__nope__/node_modules/@deepseek-ai/dsh/lib/bin.js",
               "dsh_home": "<self>/__nope__/.dsh-home"},
    },
}
legacy = os.path.join(TMP, "legacy-0.1.4.json")
with io.open(legacy, "w", encoding="utf-8", newline="\n") as f:
    json.dump(LEGACY, f, ensure_ascii=False, indent=2)

rc, out, _err = run_cli(legacy, "list", "--json")
o = jobj(out)
chk("C1 老配置 list --json 列出两个槽",
    rc == 0 and o.get("count") == 2
    and {s["key"] for s in o.get("slots") or []} == {"v1", "v2"},
    "rc=%s count=%s" % (rc, o.get("count")))

rc, out, _err = run_cli(legacy, "status", "--json")
o = jobj(out)
_ports = sorted(s.get("port") for s in o.get("slots") or [])
chk("C2 老配置 status --json 端口读对、不崩",
    rc == 0 and _ports == [4080, 5080], "rc=%s ports=%s" % (rc, _ports))

rc, out, _err = run_cli(legacy, "doctor", "--json")
o = jobj(out)
chk("C3 老配置 doctor --json 能出结论、config_valid=true",
    rc in (0, 1) and o.get("config_valid") is True,
    "rc=%s config_valid=%s" % (rc, o.get("config_valid")))

_thin = dict(LEGACY)
_thin["versions"] = {
    k: {kk: vv for kk, vv in v.items()
        if kk in ("label", "port", "node_exe", "bin_js", "dsh_home")}
    for k, v in LEGACY["versions"].items()}
thin_path = os.path.join(TMP, "thin.json")
with io.open(thin_path, "w", encoding="utf-8", newline="\n") as f:
    json.dump(_thin, f, ensure_ascii=False, indent=2)
rc, out, err = run_cli(thin_path, "list", "--json")
o = jobj(out)
chk("C4 缺 browser/temp_dir/extra_args 的槽仍能列出",
    rc == 0 and o.get("count") == 2,
    "rc=%s count=%s err=%s" % (rc, o.get("count"), (err or "")[:60]))

# ══════════════════════════════════════════════════════════════════
# D. 写路径都是「先读整份再写回」
# ══════════════════════════════════════════════════════════════════
print("== D. 写路径 ==")
_uops = io.open(os.path.join(REPO, "dsh_uninstall_ops.py"),
                encoding="utf-8").read()
for _fn in ("detach_slots", "readd_slots", "uninstall_slots"):
    try:
        _body = _uops.split("def %s(" % _fn)[1].split("\ndef ")[0]
    except IndexError:
        _body = ""
    chk("D1 %s 先读整份再写回（不会丢未知字段）" % _fn,
        "read_config" in _body and "write_config_atomic" in _body,
        "read=%s write=%s" % ("read_config" in _body,
                              "write_config_atomic" in _body))

shutil.rmtree(TMP, ignore_errors=True)
print()
if FAILS:
    print("RESULT: FAIL（%d 项）: %s" % (len(FAILS), "; ".join(FAILS)))
    sys.exit(1)
print("RESULT: ALL PASS")
