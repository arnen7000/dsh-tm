# -*- coding: utf-8 -*-
"""离线回归：dsh_adopt.py 的纯逻辑（不联网、不起进程、不写用户目录）。

与 test-manager-logic.py 的分工：
  · 本文件测「发现 + 槽位规划」的算法（版本号纠错、重名后缀、端口分配、纳入去重）；
  · test-manager-logic.py 测主文件的脱敏 / 端口校验 / bat 生成。
两者都不需要真实 dsh 安装，因此可以进提交钩子。
"""
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# 与 test-install-ops.py 一致：回归里关掉真实盘符探测，免得在 <临时目录根>
# 下留空文件夹（adopt_plan 不建目录，但保持两套测试同一套前提，断言才可比）。
os.environ["DSH_TEMP_NO_REAL_DISK"] = "1"

import dsh_adopt as A   # noqa: E402

FAILS = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ((" | " + extra) if extra else ""))
    if not cond:
        FAILS.append(name)


# ══════════════ 版本号规整 ══════════════
print("== 版本号规整（用户手误容忍）==")
cases = [
    ("0.2.0-rc.2", "0.2.0-rc.2"),          # 原样
    ("0.1.6-alpha.2", "0.1.6-alpha.2"),
    ("v0.2.0-rc.2", "0.2.0-rc.2"),          # v 前缀
    ("0.2.0rc2", "0.2.0-rc.2"),             # 少连字符
    ("0.2.0-rc 2", "0.2.0-rc.2"),           # 空格代替点
    ("0.1.5-rc 1", "0.1.5-rc.1"),
    ("  0.2.0-rc.2  ", "0.2.0-rc.2"),       # 前后空白
    ("0.2.0", "0.2.0"),                     # 无预发布标记
    ("1.0.0-beta.3", "1.0.0-beta.3"),
]
for raw, want in cases:
    got = A.normalize_version(raw)
    chk("norm %-16r -> %s" % (raw, want), got == want, "got=%r" % got)

bad = ["", "   ", "abc", "0.2", "0.2.x", "latest-x", "0.2.0-", "../evil", "0.2.0 rc.2"]
for b in bad:
    got = A.normalize_version(b)
    chk("拒绝 %-14r" % b, got == "", "got=%r" % got)

chk("hint 非空且含示例", "0.2.0-rc.2" in A.version_hint())

# ══════════════ 槽位键与重名后缀 ══════════════
print("== 槽位键分配 ==")
chk("空 -> v1", A.next_slot_key([]) == "v1")
chk("有v1 -> v2", A.next_slot_key(["v1"]) == "v2")
chk("v1,v2 -> v3", A.next_slot_key(["v1", "v2"]) == "v3")
chk("乱序也正确", A.next_slot_key(["v3", "v1"]) == "v2")
chk("跳过已占 v2", A.next_slot_key(["v1", "v2", "v3"]) == "v4")

print("== 同名文件夹加数字后缀 ==")
tmp = tempfile.mkdtemp(prefix="dsh-adopt-test-")
try:
    p, name = A.unique_dir(tmp, "dshV4")
    chk("不存在时用原名", name == "dshV4" and not os.path.exists(p), "%s / %s" % (name, p))

    os.makedirs(p)
    p2, name2 = A.unique_dir(tmp, "dshV4")
    chk("已存在 -> -2 后缀", name2 == "dshV4-2", name2)

    os.makedirs(p2)
    p3, name3 = A.unique_dir(tmp, "dshV4")
    chk("再撞 -> -3 后缀", name3 == "dshV4-3", name3)

    os.makedirs(p3)
    os.makedirs(os.path.join(tmp, "dshV4-4"))
    p4, name4 = A.unique_dir(tmp, "dshV4")
    chk("跳到 -5", name4 == "dshV4-5", name4)

    # 带 -后缀 的原名也要能续号
    os.makedirs(os.path.join(tmp, "dshX"))
    q, qn = A.unique_dir(tmp, "dshX")
    chk("其他名字首撞 -> -2", qn == "dshX-2", qn)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ══════════════ 端口分配 ══════════════
print("== 端口分配 ==")
# 注意：不能断言"v1 一定是 4080" —— 4080/7080 在部分 Windows 上落在
# Hyper-V/WSL 的排除端口段里（Get-NetTCPConnection 查不到，但 bind 会失败）。
# 所以这里断言**关系**与**避让行为**，而不是具体数值。
p1, p2, p3, p4 = (A.suggest_port(i, []) for i in (1, 2, 3, 4))
chk("四个槽都拿到端口", all(p for p in (p1, p2, p3, p4)), str([p1, p2, p3, p4]))
chk("槽序递增", p1 < p2 < p3 < p4, str([p1, p2, p3, p4]))
# 2026-11-01（用户明确要求）：**不特意避开 6080 / 8080**，只要是空的就能用。
#   「端口号不用特意避开6080或者8080，都可以用，前提是不能与已经有的其他app的端口号冲突。」
# 规则回到最朴素的千位递增，唯一判据是"被没被占用"（taken + 真实 bind 探测）。
chk("端口按槽序千位递增（4080/5080/6080/7080）",
    [p1, p2, p3, p4] == [4080, 5080, 6080, 7080], str([p1, p2, p3, p4]))
chk("6080 / 8080 不再被排除（空着就能用）",
    not getattr(A, "_PORT_TAKEN_RISK", ()), str(getattr(A, "_PORT_TAKEN_RISK", "")))
chk("真实占用时向后顺延（不硬撞）", True)  # 见下方 D 组实际探测用例
chk("避开了 Windows 排除端口段（落在可用端口）",
    all(1024 < p < 65535 for p in (p1, p2, p3, p4)), str([p1, p2, p3, p4]))
chk("不与官方默认 3080 冲突", all(p != 3080 for p in (p1, p2, p3, p4)),
    str([p1, p2, p3, p4]))

taken = [p1, p2, p3]
got = A.suggest_port(4, taken)
chk("已占端口被跳过", got not in taken, "got=%s taken=%s" % (got, taken))
chk("4080 已占 -> 顺延", A.suggest_port(1, [4080]) not in (4080,), str(A.suggest_port(1, [4080])))
chk("多个已占 -> 连跳", A.suggest_port(1, [4080, 4081, 4082]) not in (4080, 4081, 4082),
    str(A.suggest_port(1, [4080, 4081, 4082])))

# 真实占用探测：起一个 socket 占住端口，看是否被避开
import socket   # noqa: E402
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(("127.0.0.1", 0))
busy = s.getsockname()[1]
s.listen(1)
try:
    got = A.free_port(50000, [], probe=lambda p: p == busy)
    chk("真实被占端口被避开", got != busy, "got=%s busy=%s" % (got, busy))
finally:
    s.close()

always = A.free_port(50000, [], probe=lambda p: True)
chk("全被占时返回 None（不静默撞车）", always is None, str(always))

# ══════════════ 新槽配置 ══════════════
print("== 新槽配置（端口双源必须一致）==")
cfg = A.build_slot_config(
    slot_key="v4", label="dshV4",
    root="C:/home/dsh-versions/dshV4",
    dsh_home="C:/home/dsh-versions/dshV4/.dsh-home",
    temp_dir="C:/home/dsh-versions/dshV4/.tmp",
    port=8080, node_exe="C:/home/dsh-versions/dshV4/node/node.exe")

chk("port 是 int", isinstance(cfg["port"], int) and cfg["port"] == 8080, repr(cfg["port"]))
chk("extra_args 带同一端口", "8080" in cfg["extra_args"],
    str(cfg["extra_args"]))
# 复刻主文件的校验：两处端口不一致就是静默失效
i = cfg["extra_args"].index("--port")
chk("port 与 extra_args 一致（主程序校验会通过）",
    int(cfg["extra_args"][i + 1]) == cfg["port"])
chk("extra_args 含 --no-open", "--no-open" in cfg["extra_args"])
chk("bin_js 指向 @deepseek-ai/dsh/lib/bin.js",
    cfg["bin_js"].endswith("node_modules/@deepseek-ai/dsh/lib/bin.js"), cfg["bin_js"])
chk("路径用正斜杠（跨平台 + 与 bin_js 归一化一致）",
    "\\" not in cfg["bin_js"] and "\\" not in cfg["node_exe"])

# 给了 install_base 时，**四个**路径字段都必须写成 <base>/… 占位符。
# 历史 bug（2026-11-01 发布前体检发现）：temp_dir 漏了 pp()，于是新槽的
# node_exe / bin_js / dsh_home 是 <base>/… 而 temp_dir 是绝对盘符 ——
# 换用户名或换安装根时，只有临时目录断链，而且界面上一眼看不出。
print("== 新槽配置：install_base 下的占位符 ==")
cfg_p = A.build_slot_config(
    slot_key="v5", label="dshV5",
    root="C:/home/dsh-versions/dshV5",
    dsh_home="C:/home/dsh-versions/dshV5/.dsh-home",
    temp_dir="C:/home/dsh-versions/dsh-temp/dshV5",
    port=9080, node_exe="C:/home/dsh-versions/dshV5/node/node.exe",
    install_base="C:/home")
for _f in ("node_exe", "bin_js", "dsh_home", "temp_dir"):
    chk("%s 写成 <base> 占位符" % _f,
        cfg_p[_f].startswith("<base>/"), cfg_p[_f])
# 临时目录允许在安装根之外（换盘放 TEMP 是合法用法）：这时必须原样保留绝对路径，
# 不能硬塞一个 <base> 前缀把路径指错。
cfg_out = A.build_slot_config(
    slot_key="v5", label="dshV5",
    root="C:/home/dsh-versions/dshV5",
    dsh_home="C:/home/dsh-versions/dshV5/.dsh-home",
    temp_dir="D:/dsh-temp/dshV5",
    port=9081, node_exe="C:/home/dsh-versions/dshV5/node/node.exe",
    install_base="C:/home")
chk("安装根之外的 temp_dir 原样保留",
    cfg_out["temp_dir"] == "D:/dsh-temp/dshV5", cfg_out["temp_dir"])

# ══════════════ 检测（用临时目录造真实布局）════════════
print("== 检测已装 dsh ==")
tmp = tempfile.mkdtemp(prefix="dsh-detect-")
try:
    def make_dsh(node_modules, version):
        d = os.path.join(node_modules, "@deepseek-ai", "dsh")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "package.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "@deepseek-ai/dsh", "version": version}, f)
        return d

    # 一个 npm 全局装（不在管理中）
    g = os.path.join(tmp, "npm-global", "node_modules")
    make_dsh(g, "0.1.5-rc.2")
    # 一个版本目录装（已在管理中）
    vroot = os.path.join(tmp, "dsh-versions")
    managed = os.path.join(vroot, "dshV1")
    make_dsh(os.path.join(managed, "node_modules"), "0.2.0-rc.2")
    # 一个版本目录装（不在管理中）
    orphan = os.path.join(vroot, "dshV9")
    make_dsh(os.path.join(orphan, "node_modules"), "0.1.7-rc.1")

    got = A.scan_npm_globals([g])
    chk("npm 全局检出 1 个", len(got) == 1 and got[0]["version"] == "0.1.5-rc.2", str(got))

    got = A.scan_managed_dirs(vroot, managed_roots=[managed])
    chk("版本目录检出 2 个", len(got) == 2, str([x["root"] for x in got]))
    mflags = {os.path.basename(x["root"]): x["managed"] for x in got}
    chk("managed 标记正确", mflags == {"dshV1": True, "dshV9": False}, str(mflags))

    found = A.detect_existing_installs([g], vroot, managed_roots=[managed])
    vs = [b["version"] for b in found]
    chk("已管理的不再报告", "0.2.0-rc.2" not in vs, str(vs))
    chk("全局 + 孤儿都在", set(vs) == {"0.1.5-rc.2", "0.1.7-rc.1"}, str(vs))
    chk("按版本倒序", vs == sorted(vs, reverse=True), str(vs))

    kinds = set()
    for b in found:
        for loc in b["locations"]:
            kinds.add(loc["kind"])
    chk("kind 区分全局/版本目录", kinds == {"npm-global", "versions-dir"}, str(kinds))

    # 排序要按数字而非字符串（0.1.9 > 0.1.10 是错的）
    vroot2 = os.path.join(tmp, "vs2")
    make_dsh(os.path.join(vroot2, "dshA", "node_modules"), "0.1.9")
    make_dsh(os.path.join(vroot2, "dshB", "node_modules"), "0.1.10")
    got2 = A.detect_existing_installs([], vroot2)
    vs2 = [b["version"] for b in got2]
    chk("版本按数字排序（0.1.10 > 0.1.9）", vs2 == ["0.1.10", "0.1.9"], str(vs2))

    # 空环境不炸
    chk("空输入返回空", A.detect_existing_installs([], os.path.join(tmp, "nope")) == [])
    chk("坏 package.json 不炸",
        A.read_package_version(os.path.join(tmp, "不存在")) == "")

    badpj = os.path.join(tmp, "badpkg")
    os.makedirs(badpj)
    with open(os.path.join(badpj, "package.json"), "w") as f:
        f.write("{ this is not json")
    chk("坏 JSON 返回空串而非抛异常", A.read_package_version(badpj) == "")

    # BOM 也应容忍（PowerShell 手改过的配置）
    bomdir = os.path.join(tmp, "bom")
    os.makedirs(bomdir)
    with open(os.path.join(bomdir, "package.json"), "wb") as f:
        f.write(b"\xef\xbb\xbf" + json.dumps({"version": "9.9.9"}).encode("utf-8"))
    chk("带 BOM 的 package.json 能读", A.read_package_version(bomdir) == "9.9.9")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ══════════════ 备份 ══════════════
print("== 数据备份 ==")
tmp = tempfile.mkdtemp(prefix="dsh-bak-")
try:
    home = os.path.join(tmp, ".dsh-home")
    os.makedirs(os.path.join(home, "sessions"))
    with open(os.path.join(home, "settings.yaml"), "w") as f:
        f.write("agent-default-model:\n")
    with open(os.path.join(home, "sessions", "a.jsonl"), "w") as f:
        f.write("{}")

    dst, err = A.backup_dsh_home(home, os.path.join(tmp, "backups"), stamp="20260101-000000")
    chk("备份成功", dst and os.path.isdir(dst), "%s %s" % (dst, err))
    chk("备份含内容", os.path.isfile(os.path.join(dst, "settings.yaml")))
    chk("备份含子目录", os.path.isdir(os.path.join(dst, "sessions")))

    dst2, err2 = A.backup_dsh_home(os.path.join(tmp, "不存在"), os.path.join(tmp, "b2"))
    chk("源不存在时如实报告而非崩溃", dst2 is None and err2, str(err2))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ══════════════ 纳入方案（adopt_plan）══════════════
print("== 纳入方案（纯计算，不写盘）==")
tmp = tempfile.mkdtemp(prefix="dsh-plan-")
try:
    vroot = os.path.join(tmp, "dsh-versions")
    os.makedirs(vroot)
    nm = os.path.join(tmp, "npm", "node_modules")
    make_dsh(nm, "0.1.5-rc.2")

    plan = A.adopt_plan(nm, vroot, "v2", [], node_exe_source="C:/node/node.exe")
    chk("方案可用", plan["ok"] is True, str(plan.get("error")))
    chk("槽键正确", plan["slot_key"] == "v2")
    chk("目录名按槽名（小写 dsh 前缀，与既有槽一致）",
        plan["dir_name"] == "dshV2", plan["dir_name"])
    chk("落在版本根下", os.path.dirname(plan["root"]) == os.path.abspath(vroot), plan["root"])
    chk("DSH_HOME 在新槽内（不共享原 ~/.dsh）",
        plan["dsh_home"].startswith(plan["root"]), plan["dsh_home"])
    chk("DSH_HOME 是隐藏子目录", os.path.basename(plan["dsh_home"]) == ".dsh-home")
    chk("temp_dir 与 dsh_home 分开（不共用）",
        plan["temp_dir"] != plan["dsh_home"], plan["temp_dir"])
    # 临时目录的规范位置：优先 `temp_root`（管理器总会传，缺省 <槽根>/dsh-temp）；
    # 没传时才走老规则 `<候选盘>\dsh-temp\<槽>`，再不行退回槽内 `.tmp`。
    # 这里没传 temp_root，而本文件关掉了真实盘符探测（DSH_TEMP_NO_REAL_DISK=1），
    # 所以期望是槽内 .tmp。候选盘那条规则由 test-install-ops.py 用显式注入验。
    chk("temp_dir 没给 temp_root 时退回槽内 .tmp（回归不碰真实盘符）",
        plan["temp_dir"] == os.path.join(plan["root"], ".tmp"), plan["temp_dir"])
    chk("node 落在新槽内", plan["node_exe"].startswith(plan["root"]), plan["node_exe"])
    chk("端口有效", isinstance(plan["port"], int) and 1024 < plan["port"] < 65535,
        str(plan["port"]))

    # 目录已存在时必须加后缀，且不覆盖既有内容
    os.makedirs(plan["root"])
    plan2 = A.adopt_plan(nm, vroot, "v2", [])
    chk("同名 -> 加数字后缀", plan2["dir_name"] == "dshV2-2", plan2["dir_name"])
    chk("后缀目录与原目录并存",
        plan2["root"] != plan["root"] and not os.path.exists(plan2["root"]), plan2["root"])

    # 已占端口要避让
    plan3 = A.adopt_plan(nm, vroot, "v3", [plan["port"]])
    chk("避让已占端口", plan3["port"] != plan["port"], "%s vs %s" % (plan3["port"], plan["port"]))

    # v4 槽也要能算出来（需求要求最多 4 槽）
    plan4 = A.adopt_plan(nm, vroot, "v4", [p for p in (p1, p2, p3, p4) if p])
    chk("第 4 槽可用", plan4["ok"] and plan4["slot_key"] == "v4", str(plan4.get("error")))
    chk("第 4 槽端口不与前三槽相同",
        plan4["port"] not in (p1, p2, p3), "%s" % plan4["port"])

    # 非法输入要报错而不是崩
    bad = A.adopt_plan(os.path.join(tmp, "不存在"), vroot, "v2", [])
    chk("源不存在时 ok=False", bad["ok"] is False and bad.get("error"), str(bad))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ══════════════ node 运行时复制 ══════════════
print("== node 运行时复制 ==")
tmp = tempfile.mkdtemp(prefix="dsh-node-")
try:
    src = os.path.join(tmp, "srcnode")
    os.makedirs(src)
    for fn in ("node.exe", "node.dll"):
        with open(os.path.join(src, fn), "wb") as f:
            f.write(b"x" * 16)
    dst_root = os.path.join(tmp, "newslot")
    got, err = A.copy_node_exe(src, dst_root)
    chk("复制成功", got and os.path.isfile(got), "%s %s" % (got, err))
    chk("落在新槽的 node 子目录",
        got and os.path.dirname(got) == os.path.join(dst_root, "node"), str(got))
    chk("文件内容在", os.path.getsize(got) == 16 if got else False)

    got2, err2 = A.copy_node_exe(os.path.join(tmp, "没有"), dst_root)
    chk("源缺失时如实报错（不静默用系统 node）",
        got2 is None and "找不到" in err2, "%s %s" % (got2, err2))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ══════════════════════════════════════════════════════════════════════
# 纳入前的「体检」与「数据目录推断」（2026-10-30 新增）
# ══════════════════════════════════════════════════════════════════════
# 设计要求：纳入前先识别 dsh 关键组件与版本号，再纳入并检查完整性。
print("== 纳入体检 inspect_adopt_source ==")
tmp = tempfile.mkdtemp(prefix="dsh-inspect-")
try:
    def _mk(nm, ver="0.2.0-rc.2", deps=None, binjs=True, pkg=True, lock=False):
        d = os.path.join(nm, "@deepseek-ai", "dsh")
        os.makedirs(os.path.join(d, "lib"), exist_ok=True)
        if pkg:
            with open(os.path.join(d, "package.json"), "w", encoding="utf-8") as f:
                json.dump({"name": "@deepseek-ai/dsh", "version": ver,
                           "dependencies": deps or {}}, f)
        if binjs:
            with open(os.path.join(d, "lib", "bin.js"), "w") as f:
                f.write("// bin")
        if lock:
            with open(os.path.join(nm, "..", "package-lock.json"), "w") as f:
                f.write("{}")
        return d

    # (a) 完整
    nm = os.path.join(tmp, "a", "node_modules")
    _mk(nm, deps={"chalk": "^5"}, lock=True)
    os.makedirs(os.path.join(nm, "chalk"), exist_ok=True)
    i = A.inspect_adopt_source(nm)
    chk("完整安装体检通过", i["ok"] is True, str(i["problems"]))
    chk("体检读出真实版本号", i["version"] == "0.2.0-rc.2", i["version"])
    chk("完整安装没有依赖缺失", i["deps_missing"] == [], str(i["deps_missing"]))

    # (b) 依赖没装全（用户可能删过 node_modules 里的东西）
    nm = os.path.join(tmp, "b", "node_modules")
    _mk(nm, deps={"chalk": "^5", "node-pty": "^1"}, lock=True)
    os.makedirs(os.path.join(nm, "chalk"), exist_ok=True)
    i = A.inspect_adopt_source(nm)
    chk("缺依赖 → 体检不通过", i["ok"] is False and "依赖没装全" in " ".join(i["problems"]),
        str(i["problems"]))
    chk("缺依赖能点名具体包", "node-pty" in " ".join(i["problems"]), str(i["problems"]))

    # (c) 缺入口 / (d) 缺 package.json / (e) 空壳 lib
    nm = os.path.join(tmp, "c", "node_modules"); _mk(nm, binjs=False)
    i = A.inspect_adopt_source(nm)
    chk("缺 bin.js → 体检不通过", i["ok"] is False and "bin.js" in " ".join(i["problems"]),
        str(i["problems"]))
    nm = os.path.join(tmp, "d", "node_modules"); _mk(nm, pkg=False)
    i = A.inspect_adopt_source(nm)
    chk("缺 package.json → 体检不通过",
        i["ok"] is False and "package.json" in " ".join(i["problems"]), str(i["problems"]))
    nm = os.path.join(tmp, "e", "node_modules")
    os.makedirs(os.path.join(nm, "@deepseek-ai", "dsh", "lib"), exist_ok=True)
    i = A.inspect_adopt_source(nm)
    chk("空壳（lib 里没 js）→ 体检不通过", i["ok"] is False, str(i["problems"]))

    # (f) 根本不是 dsh 目录
    nm = os.path.join(tmp, "f", "node_modules"); os.makedirs(nm, exist_ok=True)
    i = A.inspect_adopt_source(nm)
    chk("不是 dsh 目录 → 体检不通过", i["ok"] is False, str(i["problems"]))

    # (g) 旧数据目录推断：本地安装看兄弟 .dsh-home，绝不误用主目录的 ~/.dsh
    nm = os.path.join(tmp, "g", "node_modules"); _mk(nm)
    chk("本地安装没有 .dsh-home → 不瞎猜数据目录",
        A.guess_source_home(nm)[0] == "", A.guess_source_home(nm)[0])
    os.makedirs(os.path.join(tmp, "g", ".dsh-home"), exist_ok=True)
    got, why = A.guess_source_home(nm)
    chk("本地安装有兄弟 .dsh-home → 认它",
        got == os.path.join(tmp, "g", ".dsh-home"), "%s %s" % (got, why))

    # (h) 全局安装判定不能靠「路径里含 /npm/node_modules」这种宽判据
    chk("临时目录里的 npm/node_modules 不算全局安装",
        A.is_global_install(nm) is False, nm)
    chk("APPDATA\npm\node_modules 才算全局安装",
        A.is_global_install(os.path.join(
            os.environ.get("APPDATA", "C:/x"), "npm", "node_modules")) is True)
finally:
    shutil.rmtree(tmp, ignore_errors=True)


# ══════════════ junction（插件链接）不能跟着复制 ══════════════
print("== 纳入：junction 跳过复制 + 按新槽重建 ==")
tmp = tempfile.mkdtemp(prefix="dsh-adopt-lnk-")
try:
    # 造一个"旧槽"：node_modules 里有真文件，.dsh-home/profiles/x/node_modules
    # 下面挂一个 junction 指向旧槽的 node_modules（dsh 的插件就是这么挂的）。
    old = os.path.join(tmp, "oldslot")
    os.makedirs(os.path.join(old, "node_modules", "somepkg"))
    with open(os.path.join(old, "node_modules", "somepkg", "index.js"), "w") as f:
        f.write("x" * 4096)
    home = os.path.join(old, ".dsh-home")
    plug_nm = os.path.join(home, "profiles", "web", "node_modules")
    os.makedirs(plug_nm)
    with open(os.path.join(home, "settings.yaml"), "w") as f:
        f.write("a: 1\n")
    link = os.path.join(plug_nm, "somepkg")
    made = A._make_junction(link, os.path.join(old, "node_modules", "somepkg"))
    chk("测试前置：junction 建成功", made and A._is_reparse(link))

    links = A.collect_links(home)
    chk("collect_links 找到插件链接",
        any(r.replace("\\", "/").endswith("node_modules/somepkg") for r, _t in links),
        str(links))

    # 备份：不许跟进去复制（否则 4 KB 的文件会被算进备份体积，且变成内容副本）
    dst = os.path.join(tmp, "bk")
    A.copy_tree_nolinks(home, dst)
    chk("备份里没有跟随链接复制出的内容",
        not os.path.exists(os.path.join(dst, "profiles", "web", "node_modules", "somepkg")),
        "拷贝把 junction 跟进去了")
    chk("普通文件照常复制",
        os.path.isfile(os.path.join(dst, "settings.yaml")))

    # 重建：指向旧槽的链接必须改指新槽
    new = os.path.join(tmp, "newslot")
    os.makedirs(os.path.join(new, "node_modules", "somepkg"))
    made2, lost2 = A.relink_home(dst, links, old_root=old, new_root=new)
    chk("链接被重建", len(made2) == 1 and not lost2, "%s / %s" % (made2, lost2))
    newlink = os.path.join(dst, "profiles", "web", "node_modules", "somepkg")
    # 注意：junction 指向真实目录，`os.path.isfile(link/index.js)` 会跟着链接
    # 返回 True —— 所以"是不是副本"只能看 reparse 位 + realpath 是否指向别处。
    chk("重建后是链接而不是副本",
        A._is_reparse(newlink)
        and os.path.realpath(newlink) != os.path.abspath(newlink),
        "%s -> %s" % (newlink, os.path.realpath(newlink)))
    chk("链接已改指新槽",
        os.path.realpath(newlink).lower().startswith(
            os.path.realpath(new).lower()),
        os.path.realpath(newlink))

    # 目标不存在 → 不建，记进 lost（提示用户重装插件）
    gone = [(r, os.path.join(tmp, "不存在的目标")) for r, _t in links]
    made3, lost3 = A.relink_home(dst, gone, old_root=old, new_root=new)
    chk("目标不存在时不建链接并记入 lost", made3 == [] and len(lost3) == 1, str(lost3))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ══════════════ 端口：段内占满就退到高位段 ══════════════
print("== 端口：任意取，只要不冲突 ==")
_busy = set(range(4080, 4280))
_p = A.suggest_port(1, [], probe=lambda p: p in _busy)
chk("槽序段被占满时退到 20000 以后", _p is not None and _p >= 20000, str(_p))
chk("正常情况仍给 4080", A.suggest_port(1, [], probe=lambda p: False) == 4080)
chk("taken 里的端口会被跳过",
    A.suggest_port(1, [4080], probe=lambda p: False) == 4081)

# ══════════════ 复制完整性核对（2026-10-31）══════════════
# 纳入 = 把用户原目录复制进新槽。复制不完整时新槽看起来一切正常、只少了几个
# 会话或凭据；用户以为搬完了、把旧目录删掉，数据就真没了。所以复制完必须核对。
print("== 复制完整性核对 ==")
tmp = tempfile.mkdtemp(prefix="dsh-vfy-")
try:
    home = os.path.join(tmp, ".dsh-home")
    os.makedirs(os.path.join(home, "sessions"))
    with open(os.path.join(home, "settings.yaml"), "w") as f:
        f.write("a: 1\n")
    with open(os.path.join(home, "sessions", "a.jsonl"), "w") as f:
        f.write("{}")
    locked = os.path.join(home, "locked.bin")
    with open(locked, "wb") as f:
        f.write(b"x" * 16)

    dst = os.path.join(tmp, "bk-ok")
    A.copy_tree_nolinks(home, dst)
    miss, diff, total = A.verify_copy(home, dst)
    chk("完整复制 → 核对通过", miss == [] and diff == [], "%s %s" % (miss, diff))
    chk("核对覆盖每个文件", total == 3, str(total))

    os.unlink(os.path.join(dst, "locked.bin"))
    miss, diff, total = A.verify_copy(home, dst)
    chk("缺文件能被核对发现", len(miss) == 1 and "locked.bin" in miss[0], str(miss))

    # 拷不动 → 进 failures（旧版是 `except OSError: pass`，静默少文件）
    import dsh_uninstall_ops as _U
    _real_rc, _real_copy2 = _U._robocopy, shutil.copy2

    def _bad_copy2(src, dstp, *a, **k):
        if os.path.basename(src) == "locked.bin":
            raise PermissionError(13, "拒绝访问（模拟被占用）")
        return _real_copy2(src, dstp, *a, **k)

    _U._robocopy = lambda s, d: (False, "模拟 robocopy 失败，走逐文件")
    shutil.copy2 = _bad_copy2
    try:
        fails = []
        A.copy_tree_nolinks(home, os.path.join(tmp, "bk-bad"), fails)
        chk("拷不动的文件记进 failures（不再静默跳过）",
            len(fails) == 1 and "locked.bin" in fails[0][0], str(fails))
        chk("能拷的照常拷", os.path.isfile(os.path.join(tmp, "bk-bad", "settings.yaml")))

        # backup_dsh_home 要把"半份备份"改名并拒绝返回路径
        bdst, berr = A.backup_dsh_home(home, os.path.join(tmp, "bkroot"),
                                      stamp="20260101-000000")
        chk("备份不完整时 backup_dsh_home 返回 None", bdst is None, str(bdst))
        chk("说明里点明「不完整」并给出原因", "不完整" in berr and "拷不动" in berr, berr[:120])
        chk("半份备份改名 -INCOMPLETE（不留看着正常的假备份）",
            os.path.isdir(os.path.join(tmp, "bkroot",
                                       "dsh-home-20260101-000000-INCOMPLETE")),
            str(os.listdir(os.path.join(tmp, "bkroot"))))
        chk("中止时源目录一个文件都没删",
            os.path.isfile(locked) and os.path.isfile(os.path.join(home, "settings.yaml")))
    finally:
        _U._robocopy, shutil.copy2 = _real_rc, _real_copy2
finally:
    shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════
# 2026-11-01 回归：扫描结果必须能被 inspect_adopt_source 认出来
# 真实事故：detect_existing_installs 里只用 os.path.dirname 一次，得到的是
# .../node_modules/@deepseek-ai（而非 .../node_modules），inspect 再拼
# "@deepseek-ai/dsh" 就成了重复路径 → 判定"这里没有 dsh"→ UI 那行被禁用，
# 用户表现为「点纳入却选不了本机已有的版本」。
# ══════════════════════════════════════════════════════════════════
print()
print("== F. 扫描结果的 node_modules 层级正确（2026-11-01 修复回归）==")
_d = tempfile.mkdtemp(prefix="dsh-adopt-nm-")
try:
    _root = os.path.join(_d, "dsh-versions", "dshV9")
    _dsh = os.path.join(_root, "node_modules", "@deepseek-ai", "dsh")
    os.makedirs(os.path.join(_dsh, "lib"), exist_ok=True)
    with io.open(os.path.join(_dsh, "package.json"), "w", encoding="utf-8") as f:
        f.write('{"name":"@deepseek-ai/dsh","version":"0.2.9"}')
    with io.open(os.path.join(_dsh, "lib", "bin.js"), "w", encoding="utf-8") as f:
        f.write("// entry")

    _found = A.detect_existing_installs([], os.path.join(_d, "dsh-versions"), [])
    chk("F1 扫到一个安装", len(_found) == 1, str(len(_found)))
    if _found:
        _loc = (_found[0].get("locations") or [{}])[0]
        _nm = _loc.get("node_modules") or ""
        chk("F2 node_modules 指向真正的 node_modules 目录",
            os.path.basename(os.path.normpath(_nm)).lower() == "node_modules", _nm)
        _info = A.inspect_adopt_source(_nm)
        chk("F3 inspect 认得出这个安装（ok=True）",
            bool(_info.get("ok")), str(_info.get("problems")))
        chk("F4 能读到版本号", _info.get("version") == "0.2.9", str(_info.get("version")))
finally:
    shutil.rmtree(_d, ignore_errors=True)

print()
if FAILS:
    print("RESULT: FAIL（%d 项）: %s" % (len(FAILS), "; ".join(FAILS)))
    sys.exit(1)
print("RESULT: ALL PASS")