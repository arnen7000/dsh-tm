# -*- coding: utf-8 -*-
"""离线回归：dsh_health.py（槽体检 —— 只读，不改任何文件）。

重点验两件事：
  1. 该报的问题都能报出来（缺 bin.js / 缺 node / 依赖缺失 / 原生模块没编译 /
     node 版本不够 / 端口冲突）；
  2. **跑完不碰磁盘** —— 体检前后整个槽的文件清单、大小、mtime 完全一致。
     这是用户 2026-10-31 明确要求的设计原则：除安装/卸载外不单独动文件。
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import dsh_health as H        # noqa: E402

FAILS = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ((" | " + extra) if extra else ""))
    if not cond:
        FAILS.append(name)


def snapshot(root):
    """目录快照：每个文件的相对路径 + 大小 + mtime。用来证明体检没动过东西。"""
    items = []
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(base, f)
            try:
                st = os.stat(p)
                items.append((os.path.relpath(p, root), st.st_size, int(st.st_mtime_ns)))
            except OSError:
                items.append((os.path.relpath(p, root), -1, -1))
    return items


def touch(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def make_slot(tmp, key="dshV1", version="0.2.2-alpha.2", engines=None, deps=None):
    """造一个「完整」的槽，返回 (cfg, root)。各用例再按需拆掉某几样。"""
    root = os.path.join(tmp, key)
    dsh = os.path.join(root, "node_modules", "@deepseek-ai", "dsh")
    touch(os.path.join(dsh, "lib", "bin.js"))
    pj = {"name": "@deepseek-ai/dsh", "version": version}
    if engines:
        pj["engines"] = engines
    if deps is not None:
        pj["dependencies"] = deps
    with open(os.path.join(dsh, "package.json"), "w", encoding="utf-8") as f:
        json.dump(pj, f)
    touch(os.path.join(root, "node", "node.exe"), b"MZ fake")
    os.makedirs(os.path.join(root, ".dsh-home"), exist_ok=True)
    os.makedirs(os.path.join(root, "temp"), exist_ok=True)
    for n in ("dsh.bat", "dsh-cli.bat"):
        touch(os.path.join(root, n))
    cfg = {
        "label": key, "port": 4080,
        "bin_js": os.path.join(dsh, "lib", "bin.js"),
        "node_exe": os.path.join(root, "node", "node.exe"),
        "dsh_home": os.path.join(root, ".dsh-home"),
        "temp_dir": os.path.join(root, "temp"),
    }
    return cfg, root


def levels(items):
    return [it[0] for it in items]


def has(items, level, keyword):
    return any(it[0] == level and keyword in it[2] for it in items)


print("== A. 完整的槽：不该误报 ==")
tmp = tempfile.mkdtemp(prefix="dsh-health-")
try:
    cfg, root = make_slot(tmp, deps={"node-pty": "^1.0.0"})
    touch(os.path.join(root, "kill-4080.bat"), b"@echo off\r\n:4080\r\n")
    touch(os.path.join(root, "node_modules", "node-pty", "build", "Release", "pty.node"),
          b"\x7fELF")
    before = snapshot(tmp)
    items = H.check_slot("v1", cfg, deep=True)
    after = snapshot(tmp)
    errs = [it for it in items if it[0] == H.LEVEL_ERROR]
    chk("A1 完整的槽没有错误项", not errs, " | ".join("%s:%s" % (e[1], e[2]) for e in errs))
    chk("A2 报出实装版本", has(items, H.LEVEL_INFO, "0.2.2-alpha.2"))
    chk("A3 报出依赖齐备", has(items, H.LEVEL_INFO, "1 个依赖齐备"))
    chk("A4 体检前后磁盘快照完全一致（只读）", before == after,
        "%d 项" % len(before))

    print()
    print("== B. 浅层：文件缺了能不能看出来 ==")
    cfg2, root2 = make_slot(os.path.join(tmp, "b"))
    os.remove(os.path.join(root2, "node", "node.exe"))
    it = H.check_slot("v1", cfg2)
    chk("B1 缺 node.exe → 错误", has(it, H.LEVEL_ERROR, "找不到"))

    cfg3, root3 = make_slot(os.path.join(tmp, "c"))
    os.remove(os.path.join(root3, "node_modules", "@deepseek-ai", "dsh", "lib", "bin.js"))
    chk("B2 缺 bin.js → 错误", has(H.check_slot("v1", cfg3), H.LEVEL_ERROR, "找不到"))

    cfg4, _ = make_slot(os.path.join(tmp, "d"))
    cfg4["node_exe"] = "node"
    chk("B3 用系统 node → 警告（不是错误，能跑）",
        has(H.check_slot("v1", cfg4), H.LEVEL_WARN, "系统 PATH"))

    cfg5, root5 = make_slot(os.path.join(tmp, "e"))
    shutil.rmtree(os.path.join(root5, ".dsh-home"))
    chk("B4 数据目录不存在 → 警告（首次启动会自动建）",
        has(H.check_slot("v1", cfg5), H.LEVEL_WARN, "首次启动会自动建"))

    cfg6, _ = make_slot(os.path.join(tmp, "f"))
    cfg6["temp_dir"] = ""
    chk("B5 没配 temp_dir → 警告", has(H.check_slot("v1", cfg6), H.LEVEL_WARN, "不隔离"))

    cfg7, root7 = make_slot(os.path.join(tmp, "g"))
    os.remove(os.path.join(root7, "dsh.bat"))
    chk("B6 缺 dsh.bat → 警告", has(H.check_slot("v1", cfg7), H.LEVEL_WARN, "dsh.bat"))

    cfg8, root8 = make_slot(os.path.join(tmp, "h"))
    touch(os.path.join(root8, "kill-4080.bat"), b"@echo off\r\n:9999\r\n")
    chk("B7 kill bat 里的端口对不上 → 警告",
        has(H.check_slot("v1", cfg8), H.LEVEL_WARN, "没写端口"))

    print()
    print("== C. 深层：包完整性 ==")
    cfg9, root9 = make_slot(os.path.join(tmp, "i"), deps={"node-pty": "^1.0.0"})
    it = H.check_slot("v1", cfg9, deep=True)
    chk("C1 依赖目录不存在 → 错误", has(it, H.LEVEL_ERROR, "找不到"))
    chk("C2 报出缺的是哪个包", has(it, H.LEVEL_ERROR, "node-pty"))

    cfg10, root10 = make_slot(os.path.join(tmp, "j"), deps={"node-pty": "^1.0.0"})
    os.makedirs(os.path.join(root10, "node_modules", "node-pty"), exist_ok=True)
    chk("C3 node-pty 目录在但没 .node → 错误（启动会崩）",
        has(H.check_slot("v1", cfg10, deep=True), H.LEVEL_ERROR, "没有任何 .node"))

    cfg11, _ = make_slot(os.path.join(tmp, "k"), engines={"node": ">=99.0.0"})
    it = H.check_slot("v1", cfg11, deep=True, node_version="v24.21.0")
    chk("C4 node 版本低于 engines → 错误", has(it, H.LEVEL_ERROR, "要求 node >= 99.0"))
    chk("C4b 版本不够时点名实际版本", has(it, H.LEVEL_ERROR, "v24.21.0"))

    cfg11b, _ = make_slot(os.path.join(tmp, "k2"), engines={"node": ">=24.9.0"})
    it = H.check_slot("v1", cfg11b, deep=True, node_version="v24.21.0")
    chk("C4c 版本够时不报错", not [x for x in it if x[0] == H.LEVEL_ERROR])

    # 假 node.exe 跑不起来 —— 真实场景就是文件被换成别的东西 / 缺 DLL
    it = H.check_slot("v1", cfg11b, deep=True)
    chk("C4d node.exe 在但跑不起来 → 警告（比版本低更常见）",
        has(it, H.LEVEL_WARN, "跑 `--version` 失败"))

    cfg12, _ = make_slot(os.path.join(tmp, "l"), engines={"node": ">=22.19.0"})
    chk("C5 node 版本满足 engines → 不报错",
        not [x for x in H.check_slot("v1", cfg12, deep=True) if x[0] == H.LEVEL_ERROR])

    print()
    print("== D. 跨槽：端口冲突 ==")
    cfg13, _ = make_slot(os.path.join(tmp, "m"))
    cfg14, _ = make_slot(os.path.join(tmp, "n"), key="dshV2")
    cfg14["port"] = 4080
    res = H.check_all({"v1": cfg13, "v2": cfg14})
    chk("D1 两个槽同端口 → 全局错误",
        any(it[0] == H.LEVEL_ERROR and "4080" in it[2] for it in res["_global"]))
    chk("D2 每个槽都有自己的结果", "v1" in res and "v2" in res)

    # 两个槽指向同一个槽目录（2026-11-13：旧版「重新纳入」造出的幽灵槽 v4）
    cfg15, _root15 = make_slot(os.path.join(tmp, "o"))
    cfg16 = dict(cfg15)
    cfg16["port"] = 5080                      # 端口不同，只让目录重复
    res2 = H.check_all({"v1": cfg15, "v4": cfg16})
    chk("D3 两个槽指向同一目录 → 全局错误「重复槽」",
        any(it[0] == H.LEVEL_ERROR and it[1] == "重复槽" and "v1" in it[2] and "v4" in it[2]
            for it in res2["_global"]),
        str(res2["_global"]))
    # 占位符与绝对路径指向同一处时，也必须认出来是同一个目录
    cfg17 = dict(cfg15)
    cfg17["port"] = 6080
    cfg17["bin_js"] = "<base>/dshV1/node_modules/@deepseek-ai/dsh/lib/bin.js"
    res3 = H.check_all({"v1": cfg15, "v4": cfg17}, base=os.path.join(tmp, "o"))
    chk("D4 <base> 占位符与绝对路径指向同一目录时也算重复",
        any(it[1] == "重复槽" for it in res3["_global"]), str(res3["_global"]))

    print()
    print("== E. 汇总工具 ==")
    chk("E1 worst_level 取最严重", H.worst_level(
        [(H.LEVEL_INFO, "a", ""), (H.LEVEL_WARN, "b", ""), (H.LEVEL_ERROR, "c", "")])
        == H.LEVEL_ERROR)
    chk("E2 没问题时 worst_level 为空串", H.worst_level([]) == "")
    chk("E3 summary 会数个数", H.summary(
        [(H.LEVEL_ERROR, "a", ""), (H.LEVEL_WARN, "b", ""), (H.LEVEL_WARN, "c", "")])
        == "1 个错误、2 个警告", H.summary(
        [(H.LEVEL_ERROR, "a", ""), (H.LEVEL_WARN, "b", ""), (H.LEVEL_WARN, "c", "")]))
    chk("E4 没有问题时 summary 说未发现问题", H.summary([]) == "未发现问题")

    print()
    print("== G. 临时目录：bat 与配置是否一致 / 历史遗留 .tmp / 残留堆积 ==")
    # 2026-10-31：临时目录改成「集中到 <槽根>/dsh-temp」，改完若没重建 bat，
    # 双击启动仍然用旧 TEMP —— 界面全对、只有临时文件落在错的地方，极难自查。
    cfgG, rootG = make_slot(tmp, key="dshVG")
    tempG = os.path.join(tmp, "dsh-temp", "dshVG")
    os.makedirs(tempG, exist_ok=True)
    cfgG["temp_dir"] = tempG

    def _bat_with(temp):
        with open(os.path.join(rootG, "dsh.bat"), "wb") as f:
            f.write(('@echo off\r\nset "TEMP=%s"\r\nset "TMP=%s"\r\n'
                     % (temp, temp)).encode("gbk"))

    _bat_with("D:\\dsh-temp\\dshVG")
    it1 = H.check_slot("vG", cfgG, deep=False)
    chk("G1 bat 里的 TEMP 与配置不符 → 警告（提示重建 bat）",
        has(it1, H.LEVEL_WARN, "TEMP 与配置不符"),
        str([i for i in it1 if i[0] == H.LEVEL_WARN]))

    # 同一路径换个斜杠/大小写不该误报
    _bat_with(tempG.replace("\\", "/").lower())
    it2 = H.check_slot("vG", cfgG, deep=False)
    chk("G2 只差斜杠/大小写不算不符（否则每次都误报）",
        not has(it2, H.LEVEL_WARN, "TEMP 与配置不符"),
        str([i for i in it2 if i[0] == H.LEVEL_WARN]))

    # 槽内历史遗留 .tmp
    os.makedirs(os.path.join(rootG, ".tmp", "dsh-acl-skill-abc"), exist_ok=True)
    it3 = H.check_slot("vG", cfgG, deep=False)
    chk("G3 槽内还有旧规则留下的 .tmp → 警告并给项数",
        has(it3, H.LEVEL_WARN, "历史遗留的 .tmp"),
        str([i for i in it3 if i[0] == H.LEVEL_WARN]))

    # 2026-11-11：新规则下 temp_dir 本来就指向槽内 .tmp，不能当成历史遗留
    cfgG["temp_dir"] = os.path.join(rootG, ".tmp")
    it3b = H.check_slot("vG", cfgG, deep=False)
    chk("G3b 当前 TEMP 就是槽内 .tmp 时不再误报历史遗留",
        not has(it3b, H.LEVEL_WARN, "历史遗留的 .tmp"),
        str([i for i in it3b if i[0] == H.LEVEL_WARN]))
    # 空目录也没有提示价值（截图里就是 0 项的空 .tmp）
    shutil.rmtree(os.path.join(rootG, ".tmp"), ignore_errors=True)
    os.makedirs(os.path.join(rootG, ".tmp"), exist_ok=True)
    it3c = H.check_slot("vG", cfgG, deep=False)
    chk("G3c 空的 .tmp 不刷警告",
        not has(it3c, H.LEVEL_WARN, "历史遗留的 .tmp"),
        str([i for i in it3c if i[0] == H.LEVEL_WARN]))
    cfgG["temp_dir"] = tempG
    os.makedirs(os.path.join(rootG, ".tmp", "dsh-acl-skill-abc"), exist_ok=True)

    # 临时目录堆积（强杀残留）
    for i in range(25):
        os.makedirs(os.path.join(tempG, "dsh-acl-skill-%02d" % i), exist_ok=True)
    it4 = H.check_slot("vG", cfgG, deep=False)
    chk("G4 临时目录累积 > 20 项 → 警告",
        has(it4, H.LEVEL_WARN, "累积了"),
        str([i for i in it4 if i[0] == H.LEVEL_WARN]))

    print()
    print("== F. 只读承诺（全量体检前后磁盘不变）==")
    before2 = snapshot(tmp)
    for k, c in (("v1", cfg), ("v2", cfg9), ("v3", cfg10), ("v4", cfg11)):
        H.check_slot(k, c, deep=True)
    H.check_all({"v1": cfg, "v2": cfg9}, deep=True)
    after2 = snapshot(tmp)
    chk("F1 反复体检后整个临时目录没有任何变化", before2 == after2,
        "%d 项" % len(before2))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if FAILS:
    print("RESULT: FAIL（%d 项）: %s" % (len(FAILS), "; ".join(FAILS)))
    sys.exit(1)
print("RESULT: ALL PASS")
