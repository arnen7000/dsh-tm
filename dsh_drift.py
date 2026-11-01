# -*- coding: utf-8 -*-
"""dsh 配置漂移检测：把「插件装了什么、配置改了什么」记进日志。

为什么需要（2026-10-31 用户提出）
================================
用户拿这个管理器主要是**调试 dsh 版本、开发插件、做 agent 开发**。这类工作里
最常见的一句是「昨天还好好的，今天怎么不行了」——而原因往往是某个插件被升了级、
某个 `cordis.yml` 被改了。这些动作都发生在 **dsh 自己或终端里**，管理器完全不知道，
日志里也就查不到线索。

所以这里做一件很轻的事：给每个槽拍一份**指纹**（插件名 + 版本、几个配置文件的内容
哈希），每 10 秒比对一次，变了就往日志里写一行。之后翻 `dsh-manager.log` 就能看到
「14:32 v2 插件 ~dsh-cost-meter 1.8.5→1.9.0」「14:35 v2 配置 web/cordis.yml 被修改」。

边界（与槽体检同一条原则）
--------------------------
**只读。** 不解析配置内容、不写任何文件、不改插件。读不到就跳过本次，绝不阻塞。

**不碰凭据。** `.credentials.yaml`、账号标记这类文件完全排除在快照之外 ——
连哈希都不算（哈希本身虽然不可逆，但没有理由让它进日志）。

**不碰会话。** `sessions/`、`logs/`、`storages/` 一概不看。
"""

import hashlib
import json
import os

# 每个 profile 里要盯的文件（相对 profiles/<name>/）。只挑「改了就说明配置变了」的：
#   cordis.yml        —— 插件启停与配置
#   cordis.patch.yml  —— 本项目的端口/行为补丁
#   compatibility.json—— dsh 自己记的兼容性判定
# **不含 package.json**：它的变化已经由「插件 +/~/-」和「启用列表 +/-」表达了，
# 再报一条"package.json 被修改"纯属噪音。
_WATCH_FILES = ("cordis.yml", "cordis.patch.yml", "compatibility.json")

# 单文件超过这个大小就只记大小、不算哈希（防某个配置被写成几 MB 拖慢刷新）
_MAX_HASH_BYTES = 512 * 1024

# 明确**不读**的文件名前缀/名字（凭据、账号、心跳等）
_SKIP_NAMES = (".credentials", ".workbuddy", ".anonymous-user-id")


def _md5(path):
    """文件内容哈希。读不到（被锁/权限）返回空串，调用方按"这次跳过"处理。"""
    try:
        st = os.stat(path)
        if st.st_size > _MAX_HASH_BYTES:
            return "size:%d" % st.st_size
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def _deps(profile_dir):
    """从 profile 的 package.json 里读插件清单 {name: version}。

    同时读 `dsh.profile.bundles`（启用顺序）—— 插件装了但没进 bundles 等于没启用，
    这也是排查时最常踩的一个点，所以单独记。
    """
    pj = None
    try:
        with open(os.path.join(profile_dir, "package.json"), "r", encoding="utf-8") as f:
            pj = json.load(f)
    except Exception:
        return {}, []
    if not isinstance(pj, dict):
        return {}, []
    deps = pj.get("dependencies") or {}
    if not isinstance(deps, dict):
        deps = {}
    bundles = []
    try:
        bundles = list(((pj.get("dsh") or {}).get("profile") or {}).get("bundles") or [])
    except Exception:
        bundles = []
    return {str(k): str(v) for k, v in deps.items()}, [str(b) for b in bundles]


def snapshot(home):
    """拍一份指纹。返回 dict：

        {"profiles": {"web": {"deps": {...}, "bundles": [...], "files": {名: 哈希}}},
         "missing": False}

    home 不存在时 `missing=True`（新槽还没跑过，不算异常）。
    """
    snap = {"profiles": {}, "missing": False}
    home = home or ""
    if not home or not os.path.isdir(home):
        snap["missing"] = True
        return snap
    pdir = os.path.join(home, "profiles")
    if not os.path.isdir(pdir):
        return snap
    try:
        names = sorted(os.listdir(pdir))
    except Exception:
        return snap
    for name in names:
        if name.startswith(_SKIP_NAMES):
            continue
        d = os.path.join(pdir, name)
        if not os.path.isdir(d):
            continue
        deps, bundles = _deps(d)
        files = {}
        for fn in _WATCH_FILES:
            h = _md5(os.path.join(d, fn))
            if h:
                files[fn] = h
        snap["profiles"][name] = {"deps": deps, "bundles": bundles, "files": files}
    return snap


def diff(old, new):
    """比对两份指纹，返回人类可读的变化列表（空列表 = 没变化）。

    输出形如：
        "插件 +dsh-cost-meter@1.8.5"        新装
        "插件 -dsh-cost-meter"              卸载
        "插件 ~dsh-cost-meter 1.8.5→1.9.0"  版本变了
        "启用列表 +dshmarket"               装上了但没启用 / 刚启用
        "配置 web/cordis.yml 被修改"
        "新 profile：headless"
    """
    out = []
    if not old or not new:
        return out
    op = (old.get("profiles") or {})
    np = (new.get("profiles") or {})

    for name in sorted(set(np) - set(op)):
        out.append("新 profile：%s（%d 个插件）" % (name, len(np[name].get("deps") or {})))
    for name in sorted(set(op) - set(np)):
        out.append("profile 消失：%s" % name)

    for name in sorted(set(op) & set(np)):
        o, n = op[name], np[name]
        od, nd = o.get("deps") or {}, n.get("deps") or {}
        for pkg in sorted(set(nd) - set(od)):
            out.append("插件 +%s@%s（%s）" % (pkg, nd[pkg], name))
        for pkg in sorted(set(od) - set(nd)):
            out.append("插件 -%s（%s）" % (pkg, name))
        for pkg in sorted(set(od) & set(nd)):
            if od[pkg] != nd[pkg]:
                out.append("插件 ~%s %s→%s（%s）" % (pkg, od[pkg], nd[pkg], name))
        ob, nb = set(o.get("bundles") or []), set(n.get("bundles") or [])
        for b in sorted(nb - ob):
            out.append("启用列表 +%s（%s）" % (b, name))
        for b in sorted(ob - nb):
            out.append("启用列表 -%s（%s）" % (b, name))
        of, nf = o.get("files") or {}, n.get("files") or {}
        for fn in sorted(set(of) | set(nf)):
            if of.get(fn) != nf.get(fn):
                what = "新增" if fn not in of else ("被删除" if fn not in nf else "被修改")
                out.append("配置 %s/%s %s" % (name, fn, what))
    return out


def plugin_lines(home, limit=0):
    """当前插件清单（供体检窗口展示）。返回 ["web：6 个 —— a@1.0、b@2.1 …", …]。"""
    snap = snapshot(home)
    lines = []
    for name in sorted(snap["profiles"]):
        info = snap["profiles"][name]
        deps = info.get("deps") or {}
        bundles = info.get("bundles") or []
        if not deps:
            lines.append("%s：没有装插件" % name)
            continue
        items = ["%s@%s" % (k, v) for k, v in sorted(deps.items())]
        if limit and len(items) > limit:
            shown = "、".join(items[:limit])
            lines.append("%s：%d 个 —— %s …（另 %d 个）"
                         % (name, len(items), shown, len(items) - limit))
        else:
            lines.append("%s：%d 个 —— %s" % (name, len(items), "、".join(items)))
        if bundles:
            lines.append("%s 启用顺序：%s" % (name, "、".join(bundles)))
    return lines
