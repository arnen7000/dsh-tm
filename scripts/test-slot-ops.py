# -*- coding: utf-8 -*-
"""离线回归：dsh_registry.py（版本列表）+ dsh_uninstall_ops.py（槽位卸载）。

不联网、不起进程、不碰用户真实目录 —— 全部在 temp 下的临时目录里造布局。
与 scripts/uninstall.ps1 的关系：§护栏 那一节是 `Test-SafeToDelete` 的**同一批边界用例**，
两边改规则时必须同步改这里（ps1 那边有自己的 11 项断言，见审计报告 P2-1）。
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import dsh_registry as R      # noqa: E402
import dsh_uninstall_ops as U  # noqa: E402

FAILS = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ((" | " + extra) if extra else ""))
    if not cond:
        FAILS.append(name)


def section(t):
    print("\n== %s ==" % t)


# ══════════════════════════════════════════════════════════════════
# A. 版本号排序（决定列表里谁在最上面）
# ══════════════════════════════════════════════════════════════════
section("A. 版本号排序 / 解析")

pairs = [
    ("0.1.9", "0.1.10", True),        # 数字比，不是字典序
    ("0.2.0-rc.2", "0.2.0", True),    # 正式版 > 预发布
    ("0.2.0-rc.2", "0.2.0-rc.10", True),   # 预发布数字段按数字比
    ("0.2.0-alpha.1", "0.2.0-beta.1", True),
    ("0.2.0-beta.9", "0.2.0-rc.1", True),
    ("0.2.1", "0.2.2", True),
]
for a, b, expect_less in pairs:
    got = R.version_key(a) < R.version_key(b)
    chk("%s < %s" % (a, b), got == expect_less, "实际=%s" % got)

chk("非标准串排最后", R.version_key("latest") > R.version_key("0.0.1-rc.1"))
chk("空串不炸", R.version_key("") > R.version_key("0.0.1"))

DATA = {
    "dist-tags": {"latest": "0.2.1-rc.2", "next": "0.2.2-alpha.2",
                  "ghost": "9.9.9-not-published"},
    "versions": {
        "0.2.2-alpha.2": {"deprecated": ""},
        "0.2.1-rc.2": {},
        "0.2.1-rc.10": {},
        "0.1.10": {},
        "0.1.9": {"deprecated": "请改用 0.2.x"},
    },
}
items = R.parse_versions(DATA)
chk("解析出 5 个版本（dist-tag 指向未发布版本时不计入）", len(items) == 5, str(len(items)))
chk("排序：最新在前", [i["version"] for i in items][0] == "0.2.2-alpha.2",
    str([i["version"] for i in items]))
chk("0.1.10 排在 0.1.9 之前",
    [i["version"] for i in items].index("0.1.10") < [i["version"] for i in items].index("0.1.9"))
chk("dist-tag 归到对应版本",
    sorted(next(i for i in items if i["version"] == "0.2.1-rc.2")["tags"]) == ["latest"])
chk("deprecated 标记保留",
    next(i for i in items if i["version"] == "0.1.9")["deprecated"].startswith("请改用"))
chk("弃用版本出现在列表里（只是标注，不隐藏）",
    "已弃用" in R.format_line(next(i for i in items if i["version"] == "0.1.9")))
chk("最新版本行有「最新」标记",
    "最新" in R.format_line(next(i for i in items if i["version"] == "0.2.1-rc.2")))
chk("默认选 dist-tag latest", R.default_pick(items) == "0.2.1-rc.2", R.default_pick(items))
chk("latest 弃用则退回 next",
    R.default_pick([{"version": "1.0.0", "tags": ["latest"], "deprecated": "x"},
                    {"version": "0.9.0", "tags": ["next"], "deprecated": ""}]) == "0.9.0")
chk("全弃用则退回第一个", R.default_pick(
    [{"version": "1.0.0", "tags": [], "deprecated": "x"},
     {"version": "0.9.0", "tags": [], "deprecated": "y"}]) == "1.0.0")
chk("空列表 default_pick 返回空串", R.default_pick([]) == "")

for bad in (None, {}, {"versions": []}, "x", 3):
    chk("容错：parse_versions(%r) 返回空列表" % (bad,), R.parse_versions(bad) == [])

# 缓存语义
R.clear_cache()
chk("清缓存后 cached_versions 为空", R.cached_versions()[0] == [])
R.remember(items, "registry.test")
got, src = R.cached_versions()
chk("remember 后可读回", len(got) == 5 and src == "registry.test", "%d/%s" % (len(got), src))
chk("缓存过期后失效", R.cached_versions(max_age=-1)[0] == [])
R.clear_cache()

# ══════════════════════════════════════════════════════════════════
# B. 路径工具
# ══════════════════════════════════════════════════════════════════
section("B. 路径工具")

chk("norm 小写+反斜杠归一", U.norm(r"D:\A\\B\C") == "d:/a/b/c", U.norm(r"D:\A\\B\C"))
chk("is_under 严格子目录", U.is_under(r"D:\A\B", r"D:\A"))
chk("is_under 不认自己", not U.is_under(r"D:\A", r"D:\A"))
chk("is_under 前缀相同但不同目录不算", not U.is_under(r"D:\AB", r"D:\A"))
chk("盘符根识别", U.is_drive_root("D:\\") and U.is_drive_root("d:/") and not U.is_drive_root("D:\\x"))
chk("占位符展开",
    U.resolve("<base>/dsh-versions", "C:/base", "C:/home", "C:/self") == "C:/base/dsh-versions")
chk("占位符 <self>",
    U.resolve("<self>/x", "C:/base", "C:/home", "C:/self") == "C:/self/x")

FAKE = os.path.join(REPO, "temp", "_t_slotops")
if os.path.isdir(FAKE):
    shutil.rmtree(FAKE)
os.makedirs(FAKE)
VER = os.path.join(FAKE, "dsh-versions")


def make_slot(key, with_home=True):
    d = os.path.join(VER, "dsh" + key.upper())
    pkg = os.path.join(d, "node_modules", "@deepseek-ai", "dsh")
    os.makedirs(os.path.join(pkg, "lib"), exist_ok=True)
    open(os.path.join(pkg, "lib", "bin.js"), "w", encoding="utf-8").write("// x")
    open(os.path.join(pkg, "package.json"), "w", encoding="utf-8").write(
        json.dumps({"name": "@deepseek-ai/dsh", "version": "0.2.0-rc.2"}))
    os.makedirs(os.path.join(d, "node"), exist_ok=True)
    open(os.path.join(d, "node", "node.exe"), "wb").write(b"x" * 1024)
    if with_home:
        os.makedirs(os.path.join(d, ".dsh-home"), exist_ok=True)
        open(os.path.join(d, ".dsh-home", "settings.yaml"), "w", encoding="utf-8").write("a: 1")
    os.makedirs(os.path.join(d, ".tmp"), exist_ok=True)
    open(os.path.join(d, "dsh.bat"), "w", encoding="utf-8").write("@echo off")
    # 槽根的 npm 清单：卸载时要连它一起备份（用户装过哪些插件全在这里）
    open(os.path.join(d, "package.json"), "w", encoding="utf-8").write(
        json.dumps({"name": "dsh-slot", "dependencies": {"some-plugin": "^1.2.3"}}))
    return d


def slot_cfg(key, port, root=None, base="<self>"):
    r = root or ("<base>/dsh-versions/dsh" + key.upper())
    return {"label": "dsh" + key.upper(), "port": port,
            "node_exe": r + "/node/node.exe",
            "bin_js": r + "/node_modules/@deepseek-ai/dsh/lib/bin.js",
            "dsh_home": r + "/.dsh-home",
            "temp_dir": r + "/.tmp"}


d1 = make_slot("v1")
d2 = make_slot("v2")
cfg0 = {"install_base": "<self>", "versions": {"v1": slot_cfg("v1", 4080),
                                               "v2": slot_cfg("v2", 5080)}}
HOME = os.path.expanduser("~")

chk("slot_root 反推本地布局",
    U.norm(U.slot_root(slot_cfg("v1", 4080), FAKE, HOME, FAKE)) == U.norm(d1),
    U.slot_root(slot_cfg("v1", 4080), FAKE, HOME, FAKE))
GLOBAL_CFG = {"bin_js": os.path.join(
    os.environ.get("APPDATA", "C:/x"), "npm", "node_modules",
    "@deepseek-ai", "dsh", "lib", "bin.js")}
chk("识别 npm 全局安装", U.is_global_install(GLOBAL_CFG, FAKE, HOME, FAKE) is True)
chk("本地槽不是全局安装", U.is_global_install(slot_cfg("v1", 4080), FAKE, HOME, FAKE) is False)

# ══════════════════════════════════════════════════════════════════
# C. 删除护栏（与 uninstall.ps1 的 Test-SafeToDelete 同一批用例）
# ══════════════════════════════════════════════════════════════════
section("C. 删除护栏")

keep = [os.path.join(d1, ".dsh-home"), os.path.join(d1, ".tmp")]
cases = [
    ("槽内的 node_modules 可删", os.path.join(d1, "node_modules"), True),
    ("槽内的单个 bat 可删", os.path.join(d1, "dsh.bat"), True),
    ("用户数据不可删", os.path.join(d1, ".dsh-home"), False),
    ("临时目录不可删", os.path.join(d1, ".tmp"), False),
    ("槽根本身不可删", d1, False),
    ("槽的上级目录不可删", VER, False),
    ("别的槽不可删（不在本槽根内）", d2, False),
    ("用户主目录不可删", HOME, False),
    ("仓库目录不可删", REPO, False),
    ("盘符根不可删", "D:\\", False),
    ("不存在的路径不删", os.path.join(d1, "no-such-file"), False),
]
for name, path, want in cases:
    ok, why = U.is_safe_to_delete(path, d1, keep, HOME, REPO)
    chk(name, ok == want, "ok=%s why=%s" % (ok, why))

chk("槽根为空时一律拒绝", U.is_safe_to_delete(os.path.join(d1, "node"), "", keep, HOME, REPO)[0] is False)
chk("是 home 的上级 → 拒绝", U.is_safe_to_delete(os.path.dirname(HOME), d1, [], HOME, REPO)[0] is False)

# ══════════════════════════════════════════════════════════════════
# D. 卸载计划（纯计算）
# ══════════════════════════════════════════════════════════════════
section("D. 卸载计划")

plans = U.plan_many(cfg0, ["v1"], HOME, REPO, FAKE, VER)
chk("计划数 = 1", len(plans) == 1)
pl = plans[0]
chk("未阻断", pl["blocked"] == "", pl["blocked"])
chk("目录存在", pl["exists"])
del_names = sorted(c["name"] for c in pl["children"] if c["action"] == "delete")
purge_names = sorted(c["name"] for c in pl["children"] if c["action"] == "purge")
keep_names = sorted(c["name"] for c in pl["children"] if c["action"] == "keep")
chk("会删：dsh.bat / node / node_modules / package.json",
    del_names == ["dsh.bat", "node", "node_modules", "package.json"], str(del_names))
# 2026-10-30 起用户数据与临时目录也删（先备份），否则留空壳 → 下次装同槽变成 dshV4-2
chk("用户数据/临时目录标记为「备份后删除」", purge_names == [".dsh-home", ".tmp"], str(purge_names))
chk("没有任何子项被保留", keep_names == [], str(keep_names))
chk("计划要求删完槽根目录本身", pl.get("purge_root") is True)
chk("释放字节数 > 1KB", pl["bytes"] > 1024, U.human_size(pl["bytes"]))

pl_out = U.plan_many(cfg0, ["v1"], HOME, REPO, FAKE, os.path.join(FAKE, "别的地方"))
chk("槽根不在版本根之内 → 阻断", bool(pl_out[0]["blocked"]), pl_out[0]["blocked"])

cfg_global = {"install_base": "<self>", "versions": {"v1": dict(GLOBAL_CFG, label="g", port=4080)}}
chk("全局安装 → 阻断（不能按槽删）",
    "全局安装" in U.plan_many(cfg_global, ["v1"], HOME, REPO, FAKE, VER)[0]["blocked"],
    U.plan_many(cfg_global, ["v1"], HOME, REPO, FAKE, VER)[0]["blocked"])

cfg_gone = {"install_base": "<self>", "versions": {"v9": slot_cfg("v9", 9080)}}
pl_gone = U.plan_many(cfg_gone, ["v9"], HOME, REPO, FAKE, VER)[0]
chk("目录已不在 → 不阻断（仍可从配置移除）",
    (pl_gone["blocked"] == "" and pl_gone["exists"] is False))

chk("human_size 单位", U.human_size(0) == "0 B" and U.human_size(2048) == "2.0 KB",
    U.human_size(0) + "/" + U.human_size(2048))

# ══════════════════════════════════════════════════════════════════
# E. 执行卸载
# ══════════════════════════════════════════════════════════════════
section("E. 执行卸载")

cfgp = os.path.join(FAKE, "config.json")
open(cfgp, "w", encoding="utf-8").write(json.dumps(cfg0, ensure_ascii=False, indent=2))

ok, payload, msg = U.uninstall_slots([], cfgp, HOME, REPO, FAKE, versions_root=VER)
chk("空选择直接拒绝", ok is False and "没有选择" in msg, msg)

ok, payload, msg = U.uninstall_slots(["v8"], cfgp, HOME, REPO, FAKE, versions_root=VER)
chk("不存在的槽被拒绝", ok is False and "v8" in msg, msg)

stopped = []
ok, payload, msg = U.uninstall_slots(["v1"], cfgp, HOME, REPO, FAKE,
                                     versions_root=VER, stamp="20260101-010203",
                                     stop_fn=lambda k, p: stopped.append((k, p)))
chk("卸载 v1 成功", ok is True, msg)
chk("调用了停进程回调（带端口）", stopped == [("v1", 4080)], str(stopped))
cfg_after = json.load(open(cfgp, encoding="utf-8-sig"))
chk("config 里只剩 v2", list(cfg_after["versions"].keys()) == ["v2"], str(list(cfg_after.keys())))
chk("v1 程序文件已删", not os.path.exists(os.path.join(d1, "node_modules")))
chk("v1 的 bat 已删", not os.path.exists(os.path.join(d1, "dsh.bat")))
chk("v1 自带 node 已删", not os.path.exists(os.path.join(d1, "node")))
# 用户数据不再原地保留：已备份 → 整个槽目录删除（避免空壳导致 -2 后缀）
chk("v1 槽目录已整个删除", not os.path.exists(d1), d1)
bk = os.path.join(FAKE, "dsh备份")
chk("v1 用户数据已备份（settings.yaml 在备份里）",
    os.path.isdir(bk) and any(
        os.path.isfile(os.path.join(bk, n, "settings.yaml"))
        for n in os.listdir(bk) if "v1-userdata" in n),
    str(os.listdir(bk)) if os.path.isdir(bk) else "(无备份目录)")
# 备份里还要有：插件/依赖清单 + 版本信息（设计要求）
_bdir = [os.path.join(bk, n) for n in (os.listdir(bk) if os.path.isdir(bk) else [])
         if "v1-userdata" in n]
_bdir = _bdir[0] if _bdir else ""
chk("备份里有 _meta.json（含实装版本号）",
    bool(_bdir) and os.path.isfile(os.path.join(_bdir, "_meta.json")) and
    "installed_version" in open(os.path.join(_bdir, "_meta.json"),
                                encoding="utf-8").read())
chk("备份里有插件清单 _slot-package.json",
    bool(_bdir) and os.path.isfile(os.path.join(_bdir, "_slot-package.json")))
chk("v2 完全未受影响", os.path.isfile(os.path.join(d2, "node_modules", "@deepseek-ai", "dsh", "lib", "bin.js")))
bk = os.path.join(FAKE, "dsh备份")
chk("用户数据已备份", os.path.isdir(bk) and any("v1-userdata" in n for n in os.listdir(bk)),
    str(os.listdir(bk)) if os.path.isdir(bk) else "(无备份目录)")
chk("备份里含 settings.yaml",
    any(os.path.isfile(os.path.join(bk, n, "settings.yaml"))
        for n in os.listdir(bk) if "v1-userdata" in n))
chk("config.json 已备份",
    any(f.startswith("config.json.bak-20260101-010203") for f in os.listdir(FAKE)),
    str([f for f in os.listdir(FAKE) if f.startswith("config")]))
chk("返回值记录了释放字节", payload and payload["bytes"] > 0, str(payload and payload["bytes"]))
chk("提示里有备份路径", "dsh备份" in msg, msg)
# 备份内容要说清楚：用户数据 + 插件清单 + 版本信息
chk("提示里说明备份含插件清单与版本信息",
    "插件清单" in msg and "版本信息" in msg, msg[-200:])

# ── 空 .dsh-home（从没对话过）→ 不建备份目录 ─────────────────────────────
# 2026-11-14 用户反馈：「我都没在 v1 上对话过，怎么多出一个 v1-userdata-…」
# 那个目录里 0 个用户文件，只有 _meta.json + package-lock（440 KB）。
make_slot("v9")
_home9 = os.path.join(VER, "dshV9", ".dsh-home")
shutil.rmtree(_home9, ignore_errors=True)
os.makedirs(_home9)                       # 存在但一个条目都没有
cfgp9 = os.path.join(FAKE, "config9.json")
with open(cfgp9, "w", encoding="utf-8") as _f:
    json.dump({"install_base": "<self>",
               "versions": {"v9": slot_cfg("v9", 9080)}}, _f, ensure_ascii=False)
ok12, _p12, msg12 = U.uninstall_slots(["v9"], cfgp9, HOME, REPO, FAKE,
                                      versions_root=VER)
_bk9 = os.path.join(FAKE, "dsh备份")
_names9 = os.listdir(_bk9) if os.path.isdir(_bk9) else []
chk("I34 空 .dsh-home → 卸载成功，但不为它建备份目录",
    ok12 is True and not any(n.startswith("v9-userdata") for n in _names9),
    str(_names9))
chk("I35 提示里如实说「未生成备份」",
    "未生成备份" in msg12, msg12[-200:])

# ── 槽目录被手工增删文件时，三个动作仍要能跑（2026-11-14 用户提问）──────
_F1 = os.path.join(VER, "dshV10")
make_slot("v10")
open(os.path.join(_F1, "我的笔记.txt"), "w", encoding="utf-8").write("hi")
os.makedirs(os.path.join(_F1, "my-plugins"))
open(os.path.join(_F1, "my-plugins", "a.js"), "w", encoding="utf-8").write("//")
open(os.path.join(_F1, "kill-10080.bat"), "w", encoding="utf-8").write("@echo off")
_f1 = U.slot_foreign_entries(_F1)
chk("I36 认出用户自己放的条目，忽略标准条目与 kill-<端口>.bat",
    _f1 == ["my-plugins", "我的笔记.txt"], str(_f1))

cfgp10 = os.path.join(FAKE, "config10.json")
with open(cfgp10, "w", encoding="utf-8") as _f:
    json.dump({"install_base": "<self>",
               "versions": {"v10": slot_cfg("v10", 10080)}}, _f, ensure_ascii=False)
ok13, _p13, msg13 = U.uninstall_slots(["v10"], cfgp10, HOME, REPO, FAKE,
                                      versions_root=VER)
chk("I37 卸载会连用户文件一起删，但提示里点名了是哪些",
    ok13 is True and not os.path.exists(_F1) and "非 dsh 条目" in msg13,
    msg13[-220:])

# 缺 bin.js：纳入要拒绝，并告诉用户下一步（别只说"不能接回"）
make_slot("v11")
_R11 = os.path.join(VER, "dshV11")
os.remove(os.path.join(_R11, "node_modules", "@deepseek-ai", "dsh", "lib", "bin.js"))
cfgp11 = os.path.join(FAKE, "config11.json")
with open(cfgp11, "w", encoding="utf-8") as _f:
    json.dump({"install_base": "<self>", "versions": {}}, _f, ensure_ascii=False)
ok14, _p14, msg14 = U.readd_slots([_R11], cfgp11, HOME, REPO, FAKE,
                                  versions_root=VER)
chk("I38 缺 bin.js 时纳入被拒，且给出下一步（删掉 / 装到别的编号）",
    ok14 is False and "不完整" in msg14 and "别的编号" in msg14, msg14[:220])

# 缺 node 运行时 → **不代劳**（用户可能故意删的），如实拒绝并说明怎么办
make_slot("v12")
_R12 = os.path.join(VER, "dshV12")
shutil.rmtree(os.path.join(_R12, "node"), ignore_errors=True)
cfgp12 = os.path.join(FAKE, "config12.json")
with open(cfgp12, "w", encoding="utf-8") as _f:
    json.dump({"install_base": "<self>", "versions": {}}, _f, ensure_ascii=False)
ok15, _p15, msg15 = U.readd_slots([_R12], cfgp12, HOME, REPO, FAKE,
                                  versions_root=VER)
chk("I39 缺 node → 拒绝接回，不替用户补（可能是他自己删的）",
    ok15 is False and "不能接回" in msg15 and "node" in msg15, msg15[:220])
chk("I40 被拒后没有偷偷改用户的目录（node/ 仍然不存在）",
    not os.path.isdir(os.path.join(_R12, "node")))

# 缺 .dsh-home：仍然能接回（dsh 启动会自己建），但必须如实提醒
make_slot("v14")
_R14 = os.path.join(VER, "dshV14")
shutil.rmtree(os.path.join(_R14, ".dsh-home"), ignore_errors=True)
cfgp14 = os.path.join(FAKE, "config14.json")
with open(cfgp14, "w", encoding="utf-8") as _f:
    json.dump({"install_base": "<self>", "versions": {}}, _f, ensure_ascii=False)
ok17, _p17, msg17 = U.readd_slots([_R14], cfgp14, HOME, REPO, FAKE,
                                  versions_root=VER)
chk("I40b 缺 .dsh-home 仍可接回，但提示「没找到原来的用户数据目录」",
    ok17 is True and "用户数据" in msg17, msg17[-240:])

# ── 显式修复（用户点「修复…」才发生）：只补不删、不覆盖已有文件 ──────────
make_slot("v15")
_R15 = os.path.join(VER, "dshV15")
shutil.rmtree(os.path.join(_R15, "node"), ignore_errors=True)
shutil.rmtree(os.path.join(_R15, ".tmp"), ignore_errors=True)
_home15 = os.path.join(_R15, ".dsh-home")
with open(os.path.join(_home15, "settings.yaml"), "w", encoding="utf-8") as _f:
    _f.write("keep: 1")
_src15 = os.path.join(FAKE, "repair-node.exe")
open(_src15, "wb").write(b"MZ" + b"y" * 64)
ok18, acts18, err18 = U.repair_slot(_R15, node_src=_src15)
chk("I42 repair_slot 补上 node（固定叫 node.exe）并补建 .tmp",
    ok18 is True
    and os.path.isfile(os.path.join(_R15, "node", "node.exe"))
    and os.path.isdir(os.path.join(_R15, ".tmp")), str(err18))
chk("I43 修复不覆盖已有文件（.dsh-home/settings.yaml 原样）",
    open(os.path.join(_home15, "settings.yaml"), encoding="utf-8").read() == "keep: 1")
chk("I44 修复动作有清单可展示给用户",
    any("node" in a for a in acts18), str(acts18))

# 缺 bin.js：补不了（等于重装），而且**一个文件都不动**
make_slot("v16")
_R16 = os.path.join(VER, "dshV16")
os.remove(os.path.join(_R16, "node_modules", "@deepseek-ai", "dsh", "lib", "bin.js"))
_before16 = sorted(os.listdir(_R16))
ok19, acts19, err19 = U.repair_slot(_R16, node_src=_src15)
chk("I45 缺 bin.js 时拒绝修复，并说明等于重装",
    ok19 is False and "bin.js" in err19, str(err19)[:160])
chk("I46 被拒时目录内容一点没变（只补不删的底线）",
    sorted(os.listdir(_R16)) == _before16, str(sorted(os.listdir(_R16))))

# 缺依赖：补不了（等于重装）—— 必须如实说，不能回"已完成（无需改动）"
make_slot("v17")
_R17 = os.path.join(VER, "dshV17")
_pj17 = os.path.join(_R17, "node_modules", "@deepseek-ai", "dsh", "package.json")
with open(_pj17, "w", encoding="utf-8") as _f:
    json.dump({"name": "@deepseek-ai/dsh", "version": "0.2.2-rc.2",
               "dependencies": {"left-pad": "^1.0.0"}}, _f)
ok20, acts20, err20 = U.repair_slot(_R17, node_src=_src15)
chk("I47 缺依赖时修复如实返回失败（不能报「已完成」）",
    ok20 is False and "补不了" in err20, str(err20)[:220])
chk("I48 修复不会凭空装依赖（node_modules 里没多出 left-pad）",
    not os.path.isdir(os.path.join(_R17, "node_modules", "left-pad")))

# 缺依赖：补不了（等于重装）—— 必须如实说，不能回"已完成（无需改动）"
make_slot("v17")
_R17 = os.path.join(VER, "dshV17")
_pj17 = os.path.join(_R17, "node_modules", "@deepseek-ai", "dsh", "package.json")
with open(_pj17, "w", encoding="utf-8") as _f:
    json.dump({"name": "@deepseek-ai/dsh", "version": "0.2.2-rc.2",
               "dependencies": {"left-pad": "^1.0.0"}}, _f)
ok20, acts20, err20 = U.repair_slot(_R17, node_src=_src15)
chk("I47 缺依赖时修复如实返回失败（不能报「已完成」）",
    ok20 is False and "补不了" in err20, str(err20)[:220])
chk("I48 修复不会凭空装依赖（node_modules 里没多出 left-pad）",
    not os.path.isdir(os.path.join(_R17, "node_modules", "left-pad")))

# 缺依赖（package.json 声明了但没装）→ 体检拒绝，别让"看着正常、一启动就炸"
make_slot("v13")
_R13 = os.path.join(VER, "dshV13")
_pj13 = os.path.join(_R13, "node_modules", "@deepseek-ai", "dsh", "package.json")
with open(_pj13, "w", encoding="utf-8") as _f:
    json.dump({"name": "@deepseek-ai/dsh", "version": "0.2.2-rc.2",
               "dependencies": {"left-pad": "^1.0.0"}}, _f)
cfgp13 = os.path.join(FAKE, "config13.json")
with open(cfgp13, "w", encoding="utf-8") as _f:
    json.dump({"install_base": "<self>", "versions": {}}, _f, ensure_ascii=False)
ok16, _p16, msg16 = U.readd_slots([_R13], cfgp13, HOME, REPO, FAKE,
                                  versions_root=VER)
chk("I41 依赖没装全 → 接回被拒（和复制纳入同一套体检）",
    ok16 is False and "依赖" in msg16, msg16[:220])

# ── 进度回调必须真的被调用 ────────────────────────────────────────────────
# 2026-10-29 实测踩到：GUI 建了进度窗却没把 progress 传进来，删 node_modules 的
# 那 40 秒里窗口一直停在「准备中…」，用户以为卸载失败（其实已经卸干净了）。
class _Rec(object):
    def __init__(self):
        self.steps, self.done_args = [], None

    def on_step(self, text):
        self.steps.append(str(text))

    def on_done(self, ok, payload=None, error=""):
        self.done_args = (ok, payload, error)

    def on_cancel(self):
        pass


make_slot("v5")
cfg5 = json.load(open(cfgp, encoding="utf-8-sig"))
cfg5["versions"]["v5"] = slot_cfg("v5", 10080)
open(cfgp, "w", encoding="utf-8").write(json.dumps(cfg5, ensure_ascii=False, indent=2))
rec = _Rec()
_stop_calls = []


def _stop(key, port):
    _stop_calls.append((key, port))


ok, payload, msg = U.uninstall_slots(["v5"], cfgp, HOME, REPO, FAKE,
                                     versions_root=VER, stamp="20260101-030405",
                                     stop_fn=_stop, progress=rec)
joined = " || ".join(rec.steps)
chk("进度回调被调用（不是空转）", len(rec.steps) >= 4, str(len(rec.steps)))
chk("进度含「备份 config.json」", "备份 config.json" in joined, joined[:200])
chk("进度含「停止端口」", "停止端口" in joined, joined[:200])
chk("进度含「备份用户数据」", "备份用户数据" in joined)
chk("进度含逐项删除（不是只报一句）",
    "正在删除" in joined and "node_modules" in joined, joined[-200:])
# on_done 由 run_in_thread / GUI 线程包装器回调，ops 函数自己**不能**调
# —— 否则「工作线程一次 + 主线程一次」会弹出两个完成框（2026-10-29 修过同形态 bug）。
chk("ops 不自己调 on_done（交给线程包装器）", rec.done_args is None, str(rec.done_args))
chk("停进程回调带上了该槽端口", _stop_calls == [("v5", 10080)], str(_stop_calls))

# 备份失败 → 该槽不删文件、也不从 config 移除（保留重试入口）
make_slot("v3")
cfg3 = json.load(open(cfgp, encoding="utf-8-sig"))
cfg3["versions"]["v3"] = slot_cfg("v3", 9080)
open(cfgp, "w", encoding="utf-8").write(json.dumps(cfg3, ensure_ascii=False, indent=2))


class _BoomCopy(object):
    """让备份步骤炸掉：模拟磁盘满 / 权限不足。
    2026-10-30 起备份不再走 shutil.copytree（它跟随 junction，见 §F），
    所以这里改打 _copy_user_data 这个真实入口。"""
    def __call__(self, *a, **k):
        raise OSError("模拟备份失败（磁盘满）")


_real_copy = U._copy_user_data
U._copy_user_data = _BoomCopy()
try:
    ok, payload, msg = U.uninstall_slots(["v3"], cfgp, HOME, REPO, FAKE,
                                         versions_root=VER, stamp="20260101-020304")
finally:
    U._copy_user_data = _real_copy
cfg_after = json.load(open(cfgp, encoding="utf-8-sig"))
chk("备份失败 → 整体 ok=False", ok is False, msg)
chk("备份失败 → 该槽仍在 config 里", "v3" in cfg_after["versions"], str(list(cfg_after["versions"].keys())))
chk("备份失败 → 文件一个没删", os.path.isdir(os.path.join(VER, "dshV3", "node_modules")))
chk("备份失败 → 提示说清原因", "备份" in msg, msg)

shutil.rmtree(FAKE, ignore_errors=True)

# ══════════════════════════════════════════════════════════════════
section("F. junction / 只读文件（2026-10-30 实测两个坑）")
# 坑 1（慢）：shutil.copytree 会**跟随** junction。dsh 的
#   `.dsh-home/profiles/node_modules/*` 全是指向槽自己 node_modules 的 junction，
#   于是 487 MB 的 .dsh-home 被备份成 942 MB / 6 万文件 / 14 分钟。
# 坑 2（失败 + 危险）：shutil.rmtree 遇到只读文件抛 WinError 5（附件对象文件是
#   -r--r--r--）；更糟的是它把 junction 当普通目录递归进去，会删掉**槽外**目标的真实文件。
import subprocess


def _mkjunction(link, target):
    """建一个目录 junction（mklink /J 不需要管理员）。建不出来返回 False。"""
    try:
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                           capture_output=True)
        return r.returncode == 0 and os.path.isdir(link)
    except Exception:
        return False


JT = tempfile.mkdtemp(prefix="dsh-jt-")
try:
    # 槽外插件仓库（绝不能被删、也不该被拷进备份）
    outside = os.path.join(JT, "outside-plugin")
    os.makedirs(outside, exist_ok=True)
    open(os.path.join(outside, "keep.txt"), "w", encoding="utf-8").write("槽外数据")
    # 槽内 .dsh-home：一个正常文件 + 一个只读对象文件 + 一个指向槽外的 junction
    home = os.path.join(JT, "slot", ".dsh-home")
    nm = os.path.join(home, "profiles", "node_modules")
    os.makedirs(nm, exist_ok=True)
    open(os.path.join(home, "settings.yaml"), "w", encoding="utf-8").write("a: 1")
    ro = os.path.join(home, "ro-object.bin")
    open(ro, "wb").write(b"x" * 64)
    os.chmod(ro, 0o444)                       # 模拟 attachments 的对象文件
    made = _mkjunction(os.path.join(nm, "outside-plugin"), outside)

    dst = os.path.join(JT, "bk")
    skipped = U._copy_user_data(home, dst)
    chk("F1 普通文件照常备份", os.path.isfile(os.path.join(dst, "settings.yaml")))
    chk("F1b 只读文件也能备份（读只读不需要写权限）",
        os.path.isfile(os.path.join(dst, "ro-object.bin")))
    if made:
        chk("F2 junction 的目标内容没被拷进备份（不跟随）",
            not os.path.exists(os.path.join(dst, "profiles", "node_modules",
                                            "outside-plugin", "keep.txt")))
        chk("F3 跳过的链接记进了清单（供手工重建）",
            any("outside-plugin" in p for p, _t in skipped), str(skipped[:3]))
    else:
        print("SKIP  F2/F3（本机建不出 junction，跳过跟随相关断言）")

    # 体积统计也不能把 junction 目标算进去
    sz = U._dir_size(home)
    chk("F4 体积统计不重复计入 junction 目标", sz < 4096, "算成 %d 字节" % sz)

    okdel, err = True, ""
    try:
        U._force_rmtree(home)
    except Exception as e:
        okdel, err = False, str(e)
    chk("F5 只读文件不挡删除（WinError 5 已修）",
        okdel and not os.path.exists(home), err or ("剩余=%s" % os.path.exists(home)))
    if made:
        chk("F6 绝不跟进 junction 删槽外内容（最危险的一条）",
            os.path.isfile(os.path.join(outside, "keep.txt")))
    else:
        print("SKIP  F6（本机建不出 junction）")
finally:
    shutil.rmtree(JT, ignore_errors=True)

# ══════════════════════════════════════════════════════════════════
section("G. 备份走 robocopy 快路（2026-10-30 提速 4 倍）")
# 逐文件 shutil.copy2 的瓶颈是「每文件一次系统调用 + 杀软逐个扫」，
# 实测同一份 196 MB / 15326 文件：Python 15.3 s（12.9 MB/s）、robocopy 3.7 s（52.4 MB/s）。
# 这里不测速度（机器相关、会 flaky），只锁住**判断逻辑**：退出码语义、
# 缺席/失败时退回逐文件、以及退回路径下内容与链接清单都不出错。
GT = tempfile.mkdtemp(prefix="dsh-rc-")
try:
    src = os.path.join(GT, "src")
    os.makedirs(os.path.join(src, "sub"), exist_ok=True)
    with open(os.path.join(src, "a.txt"), "w", encoding="utf-8") as f:
        f.write("A")
    with open(os.path.join(src, "sub", "b.txt"), "w", encoding="utf-8") as f:
        f.write("B")

    _real_which, _real_run, _real_rc = U.shutil.which, U.subprocess.run, U._robocopy

    # G1 robocopy 缺席 → 明确返回失败（由调用方退回逐文件，不静默跳过备份）
    U.shutil.which = lambda *a, **k: None
    _saved_root = os.environ.get("SystemRoot")
    os.environ["SystemRoot"] = os.path.join(GT, "no-such-windows")
    try:
        ok_rc, why = U._robocopy(src, os.path.join(GT, "x"))
        chk("G1 robocopy 缺席时如实报失败", ok_rc is False and "robocopy" in why, why)
    finally:
        if _saved_root is None:
            os.environ.pop("SystemRoot", None)
        else:
            os.environ["SystemRoot"] = _saved_root
        U.shutil.which = _real_which

    # G2 退出码是位标志：0–7 成功、>=8 才失败。
    # 按"非 0 即失败"判断会把每一次正常备份都当成错误（robocopy 拷贝过文件就回 1）。
    class _P(object):
        def __init__(self, rc):
            self.returncode, self.stdout = rc, b""

    for _rc in (0, 1, 2, 3, 7):
        U.subprocess.run = lambda *a, **k: _P(_rc)
        ok_rc, why = U._robocopy(src, os.path.join(GT, "y"))
        chk("G2 退出码 %d 视为成功" % _rc, ok_rc is True, why)
    for _rc in (8, 16):
        U.subprocess.run = lambda *a, **k: _P(_rc)
        ok_rc, why = U._robocopy(src, os.path.join(GT, "y"))
        chk("G2 退出码 %d 视为失败" % _rc, ok_rc is False and str(_rc) in why, why)
    U.subprocess.run = _real_run

    # G3 robocopy 失败 → 退回逐文件，内容照样完整（备份不能因为工具失败就没了）
    U._robocopy = lambda s, d: (False, "模拟失败")
    dst2 = os.path.join(GT, "bk2")
    sk2 = U._copy_user_data(src, dst2)
    chk("G3 robocopy 失败时退回逐文件且内容完整",
        os.path.isfile(os.path.join(dst2, "a.txt"))
        and os.path.isfile(os.path.join(dst2, "sub", "b.txt")), str(sk2))
    chk("G3b 退回路径不产生假的「拷贝失败」记录",
        not any("拷贝失败" in t for _p, t in sk2), str(sk2))
    U._robocopy = _real_rc

    # G4 链接清单只由 _collect_links 产出一次 —— 退回路径若再记一遍会重复
    #    （_links.json 里同一个链接出现两次，重建时容易二次处理）。
    outside2 = os.path.join(GT, "outside-keep")
    os.makedirs(outside2, exist_ok=True)      # mklink /J 要求目标已存在
    if _mkjunction(os.path.join(src, "lnk"), outside2):
        os.makedirs(os.path.join(GT, "outside-keep"), exist_ok=True)
        U._robocopy = lambda s, d: (False, "模拟失败")
        sk3 = U._copy_user_data(src, os.path.join(GT, "bk3"))
        names = [p for p, _t in sk3 if "lnk" in p]
        chk("G4 退回路径下链接清单不重复", len(names) == len(set(names)), str(names))
        U._robocopy = _real_rc
    else:
        print("SKIP  G4（本机建不出 junction）")
finally:
    shutil.rmtree(GT, ignore_errors=True)

# ══════════════════════════════════════════════════════════════════
section("H. 备份完整性核对（2026-10-31：要么完整、要么中止）")
# 背景：以前 `_copy_user_data` 把「拷不动的文件」和「跳过的 junction」混在同一个
# 返回值里 → `_links.json` 里会写出 target="（拷贝失败）" 的假链接。备份看起来完整、
# 实际缺文件，而卸载照样往下删。dsh 运行时锁住 .dsh-home 里的库文件，
# 这个组合就是一条**静默丢数据**的路径。现在：拷不动的进 failures，拷完当场核对，
# 缺一个就改名 `-INCOMPLETE` 并中止，源目录一个字节都不删。
HT = tempfile.mkdtemp(prefix="dsh-vfy-")
try:
    slot = os.path.join(HT, "dshV9")
    home = os.path.join(slot, ".dsh-home")
    os.makedirs(os.path.join(home, "profiles"), exist_ok=True)
    with open(os.path.join(home, "settings.yaml"), "w", encoding="utf-8") as f:
        f.write("a: 1")
    with open(os.path.join(home, "profiles", "p.json"), "w", encoding="utf-8") as f:
        f.write('{"p":1}')
    locked = os.path.join(home, "locked.bin")
    with open(locked, "wb") as f:
        f.write(b"x" * 32)
    with open(os.path.join(slot, "package.json"), "w", encoding="utf-8") as f:
        f.write('{"name":"dsh-slot"}')
    out9 = os.path.join(HT, "outside")
    os.makedirs(out9, exist_ok=True)
    with open(os.path.join(out9, "keep.txt"), "w", encoding="utf-8") as f:
        f.write("槽外")
    jmade = _mkjunction(os.path.join(home, "profiles", "node_modules"), out9)

    # H1 _walk_file_sizes：相对路径 + 不跟随 junction
    sizes = U._walk_file_sizes(home)
    chk("H1 文件清单用相对路径", "settings.yaml" in sizes
        and os.path.join("profiles", "p.json") in sizes, str(sorted(sizes)))
    chk("H1b 文件清单带字节数", sizes.get("locked.bin") == 32, str(sizes.get("locked.bin")))
    if jmade:
        chk("H1c 文件清单不跟进 junction（槽外文件不计入）",
            not any("keep.txt" in p for p in sizes), str(sorted(sizes)))
    else:
        print("SKIP  H1c（本机建不出 junction）")

    # H2 完整备份 → 核对通过
    dst9 = os.path.join(HT, "bk-ok")
    U._copy_user_data(home, dst9)
    miss, diff, total = U.verify_backup(home, dst9)
    chk("H2 完整备份核对通过（无缺失）", miss == [], str(miss))
    chk("H2b 完整备份无大小差异", diff == [], str(diff))
    chk("H2c 核对覆盖到每个文件", total == 3, str(total))

    # H3 缺一个文件 → 必须报出来（这是"真丢数据"的判据）
    os.unlink(os.path.join(dst9, "locked.bin"))
    miss, diff, total = U.verify_backup(home, dst9)
    chk("H3 备份缺文件能被核对发现",
        len(miss) == 1 and "locked.bin" in miss[0], str(miss))
    chk("H3b 缺文件时总数不变（分母是源目录）", total == 3, str(total))

    # H4 大小不同 → 报 size_diff（多半是 dsh 没停干净，源还在被写）
    with open(os.path.join(dst9, "locked.bin"), "wb") as f:
        f.write(b"y" * 8)
    miss, diff, total = U.verify_backup(home, dst9)
    chk("H4 备份内容大小不符能被核对发现",
        len(diff) == 1 and "locked.bin" in diff[0], str(diff))
    chk("H4b 大小不符不算缺失（文件在，只是内容变了）", miss == [], str(miss))

    # H5 拷不动的文件进 failures，不进链接清单（旧版会写成假链接）
    _real_rc9, _real_copy2 = U._robocopy, U.shutil.copy2

    def _bad_copy2(src, dstp, *a, **k):
        if os.path.basename(src) == "locked.bin":
            raise PermissionError(13, "拒绝访问（模拟被 dsh 占用）")
        return _real_copy2(src, dstp, *a, **k)

    U._robocopy = lambda s, d: (False, "模拟 robocopy 失败，走逐文件")
    U.shutil.copy2 = _bad_copy2
    try:
        fails9 = []
        sk9 = U._copy_user_data(home, os.path.join(HT, "bk-bad"), fails9)
        chk("H5 拷不动的文件记进 failures",
            len(fails9) == 1 and "locked.bin" in fails9[0][0], str(fails9))
        chk("H5b 拷不动不再混进链接清单（旧版的假链接 bug）",
            not any("locked.bin" in p for p, _t in sk9), str(sk9))
        chk("H5c 能拷的照常拷（不因一个失败就全放弃）",
            os.path.isfile(os.path.join(HT, "bk-bad", "settings.yaml")))

        # H6 _backup_slot 遇到拷不动 → 抛错 + 改名 -INCOMPLETE + 源目录完好
        plan9 = {"root": slot, "dsh_home": home, "label": "dshV9",
                 "port": 4080, "temp_dir": os.path.join(slot, ".tmp"), "config": {}}
        bkroot9 = os.path.join(HT, "备份")
        raised, emsg = None, ""
        try:
            U._backup_slot(plan9, "v9", bkroot9, "20260101-000000")
        except Exception as e:
            raised, emsg = e, str(e)
        chk("H6 备份不完整时 _backup_slot 抛错", raised is not None, repr(raised))
        chk("H6b 错误说明含「缺」与「拷不动」",
            "缺" in emsg and "拷不动" in emsg, emsg[:160])
        chk("H6c 半份备份被改名 -INCOMPLETE（不留看着正常的假备份）",
            os.path.isdir(os.path.join(bkroot9, "v9-userdata-20260101-000000-INCOMPLETE")),
            str(os.listdir(bkroot9)))
        chk("H6d 中止时源目录一个文件都没删",
            os.path.isfile(locked) and os.path.isfile(
                os.path.join(home, "settings.yaml")))

        # H6e/H6f 改名失败时的兜底：目录留着**正常名字**，但必须有 _INCOMPLETE.txt
        #   （改名会被索引器/杀软挡住；名字正常的半份备份是最危险的状态）
        _real_rename = os.rename

        def _bad_rename(*a, **k):
            raise PermissionError(13, "拒绝访问（模拟改名被占用）")

        os.rename = _bad_rename
        try:
            raised2 = None
            try:
                U._backup_slot(plan9, "v9", bkroot9, "20260101-000002")
            except Exception as e:
                raised2 = e
        finally:
            os.rename = _real_rename
        named = os.path.join(bkroot9, "v9-userdata-20260101-000002")
        chk("H6e 改名失败时仍抛错中止", raised2 is not None, repr(raised2)[:60])
        chk("H6f 改名失败时目录保留原名，但有 _INCOMPLETE.txt 标记",
            os.path.isdir(named)
            and os.path.isfile(os.path.join(named, "_INCOMPLETE.txt")),
            str(os.listdir(named)) if os.path.isdir(named) else "(目录不在)")
        _mark = ""
        try:
            _mark = open(os.path.join(named, "_INCOMPLETE.txt"),
                         encoding="utf-8-sig").read()
        except Exception:
            pass
        chk("H6g 标记文件说清「不完整」「缺几个」「原目录没删」",
            "不完整" in _mark and "缺" in _mark and "一个文件都没删" in _mark,
            _mark[:120].replace("\n", " / "))
    finally:
        U._robocopy, U.shutil.copy2 = _real_rc9, _real_copy2

    # H7 正常路径：返回 (dst, verify)，verify 数字可读且被写进 _meta.json
    dst7, vfy7 = U._backup_slot(plan9, "v9", bkroot9, "20260101-000001")
    chk("H7 成功时返回 (备份路径, 核对结果)", isinstance(dst7, str)
        and isinstance(vfy7, dict), "%s %s" % (dst7, vfy7))
    chk("H7b 核对结果：checked=3 / missing=0 / copy_failed=0",
        vfy7.get("checked") == 3 and vfy7.get("missing") == 0
        and vfy7.get("copy_failed") == 0, str(vfy7))
    _meta7 = json.load(open(os.path.join(dst7, "_meta.json"), encoding="utf-8"))
    chk("H7c 核对结果写进了 _meta.json（事后可追溯）",
        _meta7.get("verify", {}).get("checked") == 3, str(_meta7.get("verify")))
finally:
    shutil.rmtree(HT, ignore_errors=True)

# H8 端到端：核对报缺失 → 整个卸载中止，槽文件一个不删
#    （与 §E 的「备份直接炸掉」互补：这条是"备份跑完了但核对不过"）
FAKE8 = os.path.join(REPO, "temp", "_t_vfy_e2e")
if os.path.isdir(FAKE8):
    shutil.rmtree(FAKE8)
os.makedirs(FAKE8)
try:
    VER8 = os.path.join(FAKE8, "dsh-versions")
    d8 = os.path.join(VER8, "dshV8")
    pkg8 = os.path.join(d8, "node_modules", "@deepseek-ai", "dsh")
    os.makedirs(os.path.join(pkg8, "lib"), exist_ok=True)
    open(os.path.join(pkg8, "lib", "bin.js"), "w", encoding="utf-8").write("// x")
    open(os.path.join(pkg8, "package.json"), "w", encoding="utf-8").write(
        json.dumps({"name": "@deepseek-ai/dsh", "version": "0.1.0"}))
    os.makedirs(os.path.join(d8, ".dsh-home"), exist_ok=True)
    open(os.path.join(d8, ".dsh-home", "settings.yaml"), "w", encoding="utf-8").write("a: 1")
    cfgp8 = os.path.join(FAKE8, "config.json")
    open(cfgp8, "w", encoding="utf-8").write(json.dumps(
        {"install_base": "<self>", "versions": {"v8": slot_cfg("v8", 12080)}},
        ensure_ascii=False, indent=2))

    _real_vfy = U.verify_backup
    U.verify_backup = lambda s, d: (["ghost-file.bin"], [], 7)   # 模拟核对不过
    try:
        ok8, payload8, msg8 = U.uninstall_slots(
            ["v8"], cfgp8, HOME, REPO, FAKE8, versions_root=VER8,
            stamp="20260101-040506")
    finally:
        U.verify_backup = _real_vfy
    cfg8 = json.load(open(cfgp8, encoding="utf-8-sig"))
    chk("H8 核对不过 → 整体 ok=False", ok8 is False, msg8[:200])
    chk("H8b 核对不过 → 槽仍在 config 里", "v8" in cfg8["versions"],
        str(list(cfg8["versions"].keys())))
    chk("H8c 核对不过 → 槽文件一个没删",
        os.path.isfile(os.path.join(pkg8, "lib", "bin.js")))
    chk("H8d 提示里说清是「备份不完整」", "不完整" in msg8, msg8[-200:])
finally:
    shutil.rmtree(FAKE8, ignore_errors=True)

# ══════════════════════════════════════════════════════════════════
# I. 移出管理 / 重新纳入（2026-10-31）
#    核心承诺：**只改 config.json，磁盘一个字节都不动**。
#    与卸载的区别是根本性的（卸载备份后删目录），所以判据也要分开写：
#    这里每一条都要么验「config 变了」，要么验「磁盘没变」。
# ══════════════════════════════════════════════════════════════════
section("I. 移出管理 / 重新纳入")

FAKEI = os.path.join(REPO, "temp", "_t_detach")
if os.path.isdir(FAKEI):
    shutil.rmtree(FAKEI)
os.makedirs(FAKEI)
VERI = os.path.join(FAKEI, "dsh-versions")


def _mk_slot(ver_dir, name, port, version="0.1.5-rc.2"):
    """造一个和真实槽同构的目录，返回它的 config 片段（用 <base> 占位符）。"""
    d = os.path.join(ver_dir, name)
    pkg = os.path.join(d, "node_modules", "@deepseek-ai", "dsh")
    os.makedirs(os.path.join(pkg, "lib"), exist_ok=True)
    open(os.path.join(pkg, "lib", "bin.js"), "w", encoding="utf-8").write("// x")
    open(os.path.join(pkg, "package.json"), "w", encoding="utf-8").write(
        json.dumps({"name": "@deepseek-ai/dsh", "version": version}))
    os.makedirs(os.path.join(d, "node"), exist_ok=True)
    open(os.path.join(d, "node", "node.exe"), "wb").write(b"x" * 512)
    os.makedirs(os.path.join(d, ".dsh-home"), exist_ok=True)
    open(os.path.join(d, ".dsh-home", "settings.yaml"), "w", encoding="utf-8").write("a: 1")
    # 真实槽的 dsh.bat 里有 `--port N` —— 接回时端口就是从这里读的，
    # 夹具必须同构，否则测不到解析逻辑（2026-11-13 加）。
    open(os.path.join(d, "dsh.bat"), "w", encoding="utf-8").write(
        "@echo off\r\n"
        '"%s" "%s" web --host 127.0.0.1 --port %d --no-open\r\n'
        % (os.path.join(d, "node", "node.exe"),
           os.path.join(pkg, "lib", "bin.js"), port))
    return {
        "label": name,
        "node_exe": "<base>/dsh-versions/%s/node/node.exe" % name,
        "bin_js": "<base>/dsh-versions/%s/node_modules/@deepseek-ai/dsh/lib/bin.js" % name,
        "dsh_home": "<base>/dsh-versions/%s/.dsh-home" % name,
        "temp_dir": "<base>/dsh-versions/dsh-temp/%s" % name,
        "port": port,
        "browser": "default",
        "extra_args": ["web", "--host", "127.0.0.1", "--port", str(port), "--no-open"],
    }


def _mk_cfg(where, name, slots):
    """slots: [(槽名, 端口)] → 写一份 config.json，返回 (config 路径, 版本根)。"""
    root = os.path.join(where, name)
    if os.path.isdir(root):
        shutil.rmtree(root)
    os.makedirs(root)
    vr = os.path.join(root, "dsh-versions")
    os.makedirs(vr)
    cfgp = os.path.join(root, "config.json")
    versions = {}
    for i, (slot_name, port) in enumerate(slots, 1):
        versions["v%d" % i] = _mk_slot(vr, slot_name, port)
    data = {"install_base": root, "temp_root": "<base>/dsh-versions/dsh-temp",
            "versions": versions}
    with open(cfgp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return cfgp, vr


def _tree(p):
    """目录里每个文件的 (相对路径, 字节数, md5) —— 用来证明「一个字节都没动」。

    只比大小不够：同长度改内容会漏过去。这里连 md5 一起比，才算真"没动"。"""
    import hashlib
    out = []
    for r, _ds, fs in os.walk(p):
        for f in fs:
            fp = os.path.join(r, f)
            try:
                h = hashlib.md5(open(fp, "rb").read()).hexdigest()
            except OSError:
                h = "?"
            out.append((os.path.relpath(fp, p), os.path.getsize(fp), h))
    return sorted(out)


def _load(cfgp):
    with open(cfgp, "r", encoding="utf-8-sig") as f:
        return json.load(f)


# ── S1：移出 → 磁盘全留 → 重新纳入 ────────────────────────────────────
CFGI, VERI1 = _mk_cfg(FAKEI, "s1", [("dshV1", 4080)])
_before = _tree(VERI1)
_cfg0 = _load(CFGI)
ok, payload, msg = U.detach_slots(["v1"], CFGI, HOME, REPO, FAKEI, versions_root=VERI1)
chk("I1 移出成功", ok is True, msg[:200])
_cfg1 = _load(CFGI)
chk("I2 槽已从 config 的 versions 里移除", "v1" not in _cfg1.get("versions", {}),
    str(list(_cfg1.get("versions", {}).keys())))
chk("I3 磁盘上一个文件都没少、字节数和内容都没变", _tree(VERI1) == _before,
    "%d → %d 个文件" % (len(_before), len(_tree(VERI1))))
chk("I4 三件 bat / .dsh-home / node / node_modules 全都还在",
    all(os.path.exists(os.path.join(VERI1, "dshV1", x)) for x in
        ("dsh.bat", ".dsh-home", "node", "node_modules")))
_recs = _cfg1.get("detached") or []
chk("I5 detached 只记绝对路径（不再存 label / port / 配置快照）",
    len(_recs) == 1 and _recs[0].get("root")
    and not (set(_recs[0]) & {"label", "port", "config", "key", "version"}),
    str(_recs)[:200])
chk("I6 记录里存的是绝对路径（不是 <base> 占位符）",
    "<base>" not in (_recs[0].get("root") or ""), _recs[0].get("root"))
chk("I7 记录里的路径指向真实存在的目录",
    os.path.isdir(_recs[0]["root"]), _recs[0]["root"])
chk("I8 其它顶层字段没被抹掉（install_base / temp_root 都在）",
    _cfg1.get("install_base") == _cfg0.get("install_base")
    and _cfg1.get("temp_root") == _cfg0.get("temp_root"))
chk("I9 全部移出后 versions 为空 —— 安装根因此解锁",
    not (_cfg1.get("versions") or {}))

ok2, _p2, msg2 = U.readd_slots([_recs[0]["root"]], CFGI, HOME, REPO, FAKEI,
                               versions_root=VERI1)
_cfg2 = _load(CFGI)
chk("I10 重新纳入成功", ok2 is True, msg2[:200])
chk("I11 槽回到 config，键沿用原来的 v1", "v1" in _cfg2.get("versions", {}),
    str(list(_cfg2.get("versions", {}).keys())))
chk("I12 端口原样保留（bat 里写死了，不能悄悄改）",
    _cfg2["versions"]["v1"].get("port") == 4080)
chk("I13 重新纳入也没动磁盘", _tree(VERI1) == _before)
chk("I14 detached 里那条已清掉", not (_cfg2.get("detached") or []),
    str(_cfg2.get("detached")))
chk("I15 路径按磁盘现状写回占位符（与安装一致），解析后仍指向原槽",
    _cfg2["versions"]["v1"]["dsh_home"].startswith("<base>/dsh-versions/dshV1/")
    and os.path.isdir(os.path.join(VERI1, "dshV1", ".dsh-home")),
    _cfg2["versions"]["v1"]["dsh_home"])

# ── S2：正在运行的槽拒绝移出 ───────────────────────────────────────────
CFG2, VER2 = _mk_cfg(FAKEI, "s2", [("dshV2", 5080)])
ok3, _p3, msg3 = U.detach_slots(["v1"], CFG2, HOME, REPO, FAKEI, versions_root=VER2,
                                running_fn=lambda k, port: True)
chk("I16 正在运行的槽拒绝移出", ok3 is False and "正在运行" in msg3, msg3[:200])
chk("I17 拒绝时槽仍在 config 里（没被半移出）", "v1" in _load(CFG2).get("versions", {}))
chk("I18 拒绝时不会留下 detached 记录", not (_load(CFG2).get("detached") or []))

# ── S3：目录已经被删了，重新纳入要拒绝 ─────────────────────────────────
CFG3, VER3 = _mk_cfg(FAKEI, "s3", [("dshV3", 7080)])
U.detach_slots(["v1"], CFG3, HOME, REPO, FAKEI, versions_root=VER3)
_rec3 = (_load(CFG3).get("detached") or [{}])[0]
shutil.rmtree(_rec3.get("root") or os.path.join(VER3, "dshV3"), ignore_errors=True)
ok4, _p4, msg4 = U.readd_slots([_rec3["root"]], CFG3, HOME, REPO, FAKEI,
                               versions_root=VER3)
chk("I19 目录不在了 → 重新纳入被拒", ok4 is False and "不在" in msg4, msg4[:200])
chk("I20 被拒时 detached 记录还在（用户还能修好路径再试）",
    len(_load(CFG3).get("detached") or []) == 1)

# ── S4：重新纳入时端口和别的槽撞车 → 自动换空闲端口（不能写撞车的端口）──
CFG4, VER4 = _mk_cfg(FAKEI, "s4", [("dshV4", 4080), ("dshV5", 4080)])
U.detach_slots(["v2"], CFG4, HOME, REPO, FAKEI, versions_root=VER4)
_rec4 = (_load(CFG4).get("detached") or [{}])[0]
ok5, _p5, msg5 = U.readd_slots([_rec4["root"]], CFG4, HOME, REPO, FAKEI,
                               versions_root=VER4)
chk("I21 端口撞车仍然接回成功（不因冲突而拒绝）", ok5 is True, msg5[:200])
_added4 = (_p5 or {}).get("added") or []
_cfg4 = _load(CFG4)
chk("I22 槽键从目录名推导（dshV5 → v5，不沿用旧键 v2）",
    _added4 == ["v5"] and "v5" in (_cfg4.get("versions") or {}), str(_added4))
chk("I22b 端口被占时自动换空闲端口，并在说明里点出来",
    "端口" in msg5 and "占用" in msg5
    and _cfg4["versions"]["v5"].get("port") != 4080,
    "%s | port=%s" % (msg5[-200:], _cfg4["versions"].get("v5", {}).get("port")))

# ── S5：移出 → 纳入 → 再移出，detached 不该堆出两条 ────────────────────
CFG5, VER5 = _mk_cfg(FAKEI, "s5", [("dshV1", 6080)])
U.detach_slots(["v1"], CFG5, HOME, REPO, FAKEI, versions_root=VER5)
_r5 = (_load(CFG5).get("detached") or [{}])[0]
U.readd_slots([_r5["root"]], CFG5, HOME, REPO, FAKEI, versions_root=VER5)
U.detach_slots(["v1"], CFG5, HOME, REPO, FAKEI, versions_root=VER5)
chk("I23 反复移出/纳入不会堆出重复记录",
    len(_load(CFG5).get("detached") or []) == 1,
    str(_load(CFG5).get("detached"))[:200])

# ── S6：不存在的槽 / 空选择 ────────────────────────────────────────────
ok6, _p6, msg6 = U.detach_slots(["v9"], CFG5, HOME, REPO, FAKEI, versions_root=VER5)
chk("I24 移出 config 里没有的槽 → 拒绝并说明", ok6 is False and "v9" in msg6, msg6[:160])
ok7, _p7, msg7 = U.detach_slots([], CFG5, HOME, REPO, FAKEI, versions_root=VER5)
chk("I25 空选择 → 拒绝", ok7 is False and "没有选择" in msg7, msg7[:120])
ok8, _p8, msg8 = U.readd_slots([], CFG5, HOME, REPO, FAKEI, versions_root=VER5)
chk("I26 重新纳入空选择 → 拒绝", ok8 is False and "没有选择" in msg8, msg8[:120])

# ── S7：config 里的 detached 是脏数据也不能崩 ──────────────────────────
CFG7, VER7 = _mk_cfg(FAKEI, "s7", [("dshV7", 6081)])
_d7 = _load(CFG7)
_d7["detached"] = "不是列表"
with open(CFG7, "w", encoding="utf-8") as f:
    json.dump(_d7, f, ensure_ascii=False, indent=2)
ok9, _p9, msg9 = U.detach_slots(["v1"], CFG7, HOME, REPO, FAKEI, versions_root=VER7)
chk("I27 detached 字段是脏数据时照常工作（当空处理，不崩）",
    ok9 is True and isinstance(_load(CFG7).get("detached"), list), msg9[:160])

# ── S8：磁盘是唯一事实来源（2026-11-13 修「幽灵槽 v4」）──────────────────
chk("I28 槽键从目录名推导（dshV3 → v3 / dshV3-2 → v3-2 / 认不出 → 空）",
    U.slot_key_from_dir("dshV3") == "v3"
    and U.slot_key_from_dir(r"D:\x\dshV3-2") == "v3-2"
    and U.slot_key_from_dir("whatever") == "")

CFG8, VER8 = _mk_cfg(FAKEI, "s8", [("dshV3", 7080)])
_s8 = os.path.join(VER8, "dshV3")
chk("I29 port_from_bats 从 bat 里读出真实端口", U.port_from_bats(_s8) == 7080,
    str(U.port_from_bats(_s8)))

# 已在管理里的目录：绝不能重复登记一次（旧版就是这么造出「槽 v4 顶着 dshV3」的）
ok10, _p10, msg10 = U.readd_slots([_s8], CFG8, HOME, REPO, FAKEI, versions_root=VER8)
_cfg8 = _load(CFG8)
chk("I30 目录已在 versions 里 → 拒绝重复纳入",
    ok10 is False and "已经在管理里" in msg10, msg10[:200])
chk("I31 拒绝后 versions 没多出槽（幽灵槽的根本防线）",
    list((_cfg8.get("versions") or {}).keys()) == ["v1"],
    str(list((_cfg8.get("versions") or {}).keys())))

# 移出 → 又重装（旧记录仍指着现役目录）：再点扫描/接回也不能造出第二个槽
U.detach_slots(["v1"], CFG8, HOME, REPO, FAKEI, versions_root=VER8)
_cfg8 = _load(CFG8)
_cfg8["versions"] = {"v1": _mk_slot(VER8, "dshV3", 7080)}
with open(CFG8, "w", encoding="utf-8") as f:
    json.dump(_cfg8, f, ensure_ascii=False, indent=2)
ok11, _p11, msg11 = U.readd_slots([_s8], CFG8, HOME, REPO, FAKEI, versions_root=VER8)
_cfg8b = _load(CFG8)
chk("I32 移出后重装（记录指向现役目录）→ 仍拒绝，不造幽灵槽",
    ok11 is False and len(_cfg8b.get("versions") or {}) == 1, msg11[:200])
chk("I33 被拒时陈旧记录不丢（盘没接上时用户还能再试）",
    len(_cfg8b.get("detached") or []) == 1, str(_cfg8b.get("detached"))[:160])

shutil.rmtree(FAKEI, ignore_errors=True)

# ══════════════════════════════════════════════════════════════════
print("")
if FAILS:
    print("FAILED %d 项：" % len(FAILS))
    for f in FAILS:
        print("  - " + f)
    print("RESULT: FAIL")
    sys.exit(1)
print("RESULT: ALL PASS")
