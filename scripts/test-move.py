# -*- coding: utf-8 -*-
"""dsh_move 的离线回归：搬迁是**破坏性**操作，必须逐条守住安全性。

全部在临时目录里跑，用假的槽树，不碰真实安装。
重点验证：
  - 目标已有内容时**中止**，绝不覆盖
  - 校验失败时**源不删、目标清理**
  - junction 不被穿透
  - config.json 的路径正确改写
  - 互相包含的路径被拒绝
"""
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, os.pardir))
sys.path.insert(0, REPO)
import dsh_move as M  # noqa: E402

FAIL = 0
N = 0


def chk(name, cond, detail=""):
    global FAIL, N
    N += 1
    if cond:
        print("PASS  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s%s" % (name, (" | " + detail) if detail else ""))


def make_slot(base, name, files=3, size=100):
    """造一个假槽：<base>/dsh-versions/<name>/node/node.exe 等"""
    root = os.path.join(base, "dsh-versions", name)
    os.makedirs(os.path.join(root, "node"), exist_ok=True)
    os.makedirs(os.path.join(root, "node_modules", "@deepseek-ai", "dsh"), exist_ok=True)
    with open(os.path.join(root, "node", "node.exe"), "wb") as f:
        f.write(b"\x00" * size)
    with open(os.path.join(root, "node_modules", "@deepseek-ai", "dsh", "package.json"),
              "w", encoding="utf-8") as f:
        json.dump({"version": "0.2.1-rc.1"}, f)
    for i in range(files):
        with open(os.path.join(root, "f%d.txt" % i), "w", encoding="utf-8") as f:
            f.write("x" * size)
    return root


def make_config(path, base, slots):
    """写一份 config：路径用绝对形式（模拟已在用的配置）。"""
    data = {"install_base": base, "versions": {}}
    for i, s in enumerate(slots):
        root = os.path.join(base, "dsh-versions", s)
        data["versions"]["v%d" % (i + 1)] = {
            "label": s,
            "node_exe": os.path.join(root, "node", "node.exe"),
            "bin_js": os.path.join(root, "node_modules", "@deepseek-ai", "dsh", "lib", "bin.js"),
            "dsh_home": os.path.join(root, ".dsh-home"),
            "temp_dir": os.path.join(base, "dsh-versions", "dsh-temp", s),
            "port": 4080 + i * 1000,
        }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


print("== A. 计划生成：拒绝危险情况 ==")
d = tempfile.mkdtemp(prefix="dsh-move-test-")
try:
    src = os.path.join(d, "old")
    dst = os.path.join(d, "new")
    make_slot(src, "dshV1")
    cfgp = os.path.join(d, "config.json")
    make_config(cfgp, src, ["dshV1"])

    p = M.plan_move(src, src, ["dshV1"])
    chk("A1 源与目标相同 → 拒绝", not p["ok"] and "相同" in p["error"], p.get("error"))

    p = M.plan_move(src, os.path.join(src, "inner"), ["dshV1"])
    chk("A2 目标在源内部 → 拒绝", not p["ok"] and "包含" in p["error"], p.get("error"))

    p = M.plan_move(src, src + "_sub", ["dshV9"])
    chk("A3 槽不存在 → 拒绝", not p["ok"] and "不存在" in p["error"], p.get("error"))

    # 目标已有同名目录
    os.makedirs(os.path.join(dst, "dsh-versions", "dshV1"))
    p = M.plan_move(src, dst, ["dshV1"])
    chk("A4 目标已存在同名槽 → 拒绝（绝不覆盖）",
        not p["ok"] and "已存在" in p["error"], p.get("error"))
    shutil.rmtree(os.path.join(dst, "dsh-versions", "dshV1"))

    p = M.plan_move(src, dst, ["dshV1"])
    chk("A5 正常情况 → 计划可用", p["ok"], p.get("error"))
    chk("A6 计划里带文件数统计", p["slots"][0]["stats"]["files"] > 0,
        str(p["slots"][0]["stats"]))

    # A7：目标盘空间不够时**事前**拒绝（2026-11-14）—— 不能拷到一半才失败
    _real_free = M._free_bytes
    try:
        M._free_bytes = lambda _p: 1024          # 只有 1 KB
        p = M.plan_move(src, dst, ["dshV1"])
        chk("A7 目标盘空间不足 → 事前拒绝并说明需要多少",
            not p["ok"] and "空间不足" in p["error"] and "需要" in p["error"],
            p.get("error"))
        M._free_bytes = lambda _p: None          # 探测不到 → 不阻断（宁可放行）
        p = M.plan_move(src, dst, ["dshV1"])
        chk("A8 探测不到剩余空间时不阻断（只跳过这项检查）", p["ok"], p.get("error"))
    finally:
        M._free_bytes = _real_free
    chk("A9 计划里带 need_bytes / free_bytes 供确认框显示",
        isinstance(M.plan_move(src, dst, ["dshV1"]).get("need_bytes"), int))

    print()
    print("== B. 实际搬迁 ==")
    before_cfg = json.load(io.open(cfgp, encoding="utf-8"))
    src_root = os.path.join(src, "dsh-versions", "dshV1")
    n_before = M.tree_stats(src_root)

    ok, msg = M.move_now(p, cfgp)
    chk("B1 搬迁成功", ok, msg)
    chk("B2 新位置有槽", os.path.isdir(os.path.join(dst, "dsh-versions", "dshV1")))
    chk("B3 旧位置已清理", not os.path.exists(src_root))
    after = M.tree_stats(os.path.join(dst, "dsh-versions", "dshV1"))
    chk("B4 文件数与字节数一致",
        after["files"] == n_before["files"] and after["bytes"] == n_before["bytes"],
        "before=%s after=%s" % (n_before, after))

    cfg_after = json.load(io.open(cfgp, encoding="utf-8"))
    chk("B5 install_base 已更新为新位置",
        os.path.normcase(cfg_after["install_base"]) == os.path.normcase(dst),
        cfg_after["install_base"])
    ne = cfg_after["versions"]["v1"]["node_exe"]
    chk("B6 槽内路径指到新位置", os.path.normcase(ne).startswith(os.path.normcase(dst)), ne)
    chk("B7 槽内路径仍指向新位置里的 node.exe", ne.endswith("node.exe"), ne)

    print()
    print("== C. 校验失败时源不被破坏 ==")
    src2 = os.path.join(d, "old2")
    dst2 = os.path.join(d, "new2")
    make_slot(src2, "dshV2")
    cfgp2 = os.path.join(d, "config2.json")
    make_config(cfgp2, src2, ["dshV2"])
    p2 = M.plan_move(src2, dst2, ["dshV2"])
    chk("C1 计划可用", p2["ok"], p2.get("error"))

    # 故意把源目录的统计改大，模拟"复制不完整"→ 校验必失败
    p2["slots"][0]["stats"]["files"] += 999
    src2_root = os.path.join(src2, "dsh-versions", "dshV2")
    existed_before = os.path.isdir(src2_root)
    ok2, msg2 = M.move_now(p2, cfgp2)
    chk("C2 校验失败 → 返回失败", not ok2, msg2)
    chk("C3 校验失败 → 源目录完好", os.path.isdir(src2_root) and existed_before)
    chk("C4 校验失败 → 目标副本被清理",
        not os.path.isdir(os.path.join(dst2, "dsh-versions", "dshV2")))
    cfg2 = json.load(io.open(cfgp2, encoding="utf-8"))
    chk("C5 校验失败 → config 未被改动",
        os.path.normcase(cfg2["install_base"]) == os.path.normcase(src2),
        cfg2["install_base"])

    print()
    print("== D. junction 不被穿透 ==")
    src3 = os.path.join(d, "old3")
    dst3 = os.path.join(d, "new3")
    root3 = make_slot(src3, "dshV3")
    outside = os.path.join(d, "outside_target")
    os.makedirs(outside)
    with open(os.path.join(outside, "keep.txt"), "w", encoding="utf-8") as f:
        f.write("必须保留")
    # 在槽里放一个 junction 指向外部
    link = os.path.join(root3, "profiles_link")
    made_link = False
    try:
        import subprocess
        rc = subprocess.call(["cmd", "/c", "mklink", "/J", link, outside],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        made_link = (rc == 0 and os.path.exists(link))
    except Exception:
        made_link = False

    if made_link:
        chk("D1 junction 被识别为链接", M.is_link_like(link))
        st = M.tree_stats(root3)
        chk("D2 统计时不跟进去（links >= 1）", st["links"] >= 1, str(st))
        cfgp3 = os.path.join(d, "config3.json")
        make_config(cfgp3, src3, ["dshV3"])
        p3 = M.plan_move(src3, dst3, ["dshV3"])
        ok3, msg3 = M.move_now(p3, cfgp3)
        chk("D3 带 junction 的槽也能搬迁", ok3, msg3)
        chk("D4 junction 指向的外部目录**内容未丢**",
            os.path.exists(os.path.join(outside, "keep.txt")))
    else:
        print("SKIP  D1-D4（当前环境无法创建 junction）")

    print()
    print("== E. 空槽列表 ==")
    p4 = M.plan_move(src, os.path.join(d, "x"), [])
    chk("E1 没选任何槽 → 拒绝", not p4["ok"], p4.get("error"))

finally:
    shutil.rmtree(d, ignore_errors=True)

print()
print("== test-move: %d 项，FAIL=%d ==" % (N, FAIL))
sys.exit(1 if FAIL else 0)
