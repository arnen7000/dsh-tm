# -*- coding: utf-8 -*-
"""离线回归：dsh_drift.py（插件 / 配置漂移检测）。

重点：
  1. 插件增删改、启用列表变化、配置文件变化、新 profile 都能认出来；
  2. **凭据与会话不进快照**（`.credentials.yaml` 连哈希都不算）；
  3. **只读**：快照前后磁盘快照完全一致。
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import dsh_drift as D        # noqa: E402

FAILS = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ((" | " + extra) if extra else ""))
    if not cond:
        FAILS.append(name)


def snapshot_disk(root):
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


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def make_profile(home, name, deps, bundles=None, cordis="a: 1\n"):
    d = os.path.join(home, "profiles", name)
    pj = {"name": "dsh-profile-%s" % name, "private": True, "dependencies": deps}
    if bundles is not None:
        pj["dsh"] = {"profile": {"bundles": bundles}}
    write(os.path.join(d, "package.json"), json.dumps(pj, ensure_ascii=False, indent=2))
    write(os.path.join(d, "cordis.yml"), cordis)
    return d


tmp = tempfile.mkdtemp(prefix="dsh-drift-")
try:
    print("== A. 快照 ==")
    home = os.path.join(tmp, "home")
    os.makedirs(os.path.join(home, "sessions"), exist_ok=True)
    os.makedirs(os.path.join(home, "logs"), exist_ok=True)
    write(os.path.join(home, ".credentials.yaml"), "token: SUPER-SECRET\n")
    write(os.path.join(home, "sessions", "abc.json"), '{"x":1}')
    make_profile(home, "web", {"a": "1.0.0", "b": "2.0.0"}, ["a", "b"])
    make_profile(home, "headless", {"c": "3.0.0"}, ["c"])

    before = snapshot_disk(home)
    s1 = D.snapshot(home)
    after = snapshot_disk(home)

    chk("A1 读到两个 profile", sorted(s1["profiles"]) == ["headless", "web"],
        repr(sorted(s1["profiles"])))
    chk("A2 插件清单与版本正确",
        s1["profiles"]["web"]["deps"] == {"a": "1.0.0", "b": "2.0.0"},
        repr(s1["profiles"]["web"]["deps"]))
    chk("A3 读到启用列表", s1["profiles"]["web"]["bundles"] == ["a", "b"])
    chk("A4 记下了配置文件哈希", "cordis.yml" in s1["profiles"]["web"]["files"])
    chk("A5 凭据文件不在快照里",
        "SUPER-SECRET" not in json.dumps(s1, ensure_ascii=False)
        and ".credentials" not in json.dumps(s1, ensure_ascii=False))
    chk("A6 会话目录不在快照里", "sessions" not in json.dumps(s1, ensure_ascii=False))
    chk("A7 快照前后磁盘完全一致（只读）", before == after, "%d 项" % len(before))
    chk("A8 不存在的 home 标 missing", D.snapshot(os.path.join(tmp, "nope"))["missing"] is True)

    print()
    print("== B. 变化检测 ==")
    s2 = D.snapshot(home)
    chk("B1 没变就是空列表", D.diff(s1, s2) == [], repr(D.diff(s1, s2)))

    # 新装一个插件
    make_profile(home, "web", {"a": "1.0.0", "b": "2.0.0", "dsh-cost-meter": "1.8.5"},
                 ["a", "b"])
    ch = D.diff(s2, D.snapshot(home))
    chk("B2 认出新增插件", any("插件 +dsh-cost-meter@1.8.5" in c for c in ch), repr(ch))

    s3 = D.snapshot(home)
    make_profile(home, "web", {"a": "1.0.0", "dsh-cost-meter": "1.8.5"}, ["a"])
    ch = D.diff(s3, D.snapshot(home))
    chk("B3 认出卸载插件", any("插件 -b" in c for c in ch), repr(ch))
    chk("B4 认出启用列表变化", any("启用列表 -b" in c for c in ch), repr(ch))

    s4 = D.snapshot(home)
    make_profile(home, "web", {"a": "1.0.0", "dsh-cost-meter": "1.9.0"}, ["a"])
    ch = D.diff(s4, D.snapshot(home))
    chk("B5 认出插件版本升级", any("插件 ~dsh-cost-meter 1.8.5→1.9.0" in c for c in ch), repr(ch))

    s5 = D.snapshot(home)
    make_profile(home, "web", {"a": "1.0.0", "dsh-cost-meter": "1.9.0"}, ["a"],
                 cordis="a: 2\n")
    ch = D.diff(s5, D.snapshot(home))
    chk("B6 认出配置文件被修改", any("配置 web/cordis.yml 被修改" in c for c in ch), repr(ch))

    s6 = D.snapshot(home)
    make_profile(home, "debug", {}, [])
    ch = D.diff(s6, D.snapshot(home))
    chk("B7 认出新增 profile", any("新 profile：debug" in c for c in ch), repr(ch))

    s7 = D.snapshot(home)
    shutil.rmtree(os.path.join(home, "profiles", "debug"))
    ch = D.diff(s7, D.snapshot(home))
    chk("B8 认出 profile 被删", any("profile 消失：debug" in c for c in ch), repr(ch))

    print()
    print("== C. 插件清单展示 ==")
    lines = D.plugin_lines(home)
    chk("C1 列出 profile 的插件", any("web：" in l and "dsh-cost-meter@1.9.0" in l for l in lines),
        repr(lines))
    chk("C2 没有插件的 profile 也有说明", any("debug" not in l for l in lines))
    lines2 = D.plugin_lines(home, limit=1)
    chk("C3 limit 会截断并注明还剩几个",
        any("另 1 个" in l for l in lines2), repr(lines2))

    print()
    print("== D. 容错 ==")
    bad = os.path.join(tmp, "bad")
    os.makedirs(os.path.join(bad, "profiles", "web"), exist_ok=True)
    write(os.path.join(bad, "profiles", "web", "package.json"), "{ 这不是 JSON")
    s = D.snapshot(bad)
    chk("D1 坏 JSON 不炸，插件清单当空", s["profiles"]["web"]["deps"] == {}, repr(s))
    chk("D2 空快照 diff 不出错", D.diff({}, {}) == [] and D.diff(None, None) == [])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if FAILS:
    print("RESULT: FAIL（%d 项）: %s" % (len(FAILS), "; ".join(FAILS)))
    sys.exit(1)
print("RESULT: ALL PASS")
