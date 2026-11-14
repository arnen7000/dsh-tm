# -*- coding: utf-8 -*-
"""槽体检：**只检查、只报告，不修改任何文件**。

设计原则（2026-10-31 起）
==================================
除「安装」「卸载」这种用户显式发起、且提前把后果讲清楚的批量操作外，
管理器**不单独去动用户槽里的文件**。槽里少了什么、多了什么，有可能是
用户故意的 —— 自己换了 node、给 dsh 打了补丁、删掉用不到的插件、把
`.dsh-home` 挪到别的盘。管理器替用户"修好"，等于把用户的选择覆盖掉。

所以这里只做两件事：

  1. 把「这个槽现在的样子，跟它登记的样子差在哪」查出来；
  2. 分级报告（错误 / 警告 / 提示），供界面显示、日志记录。

真正的修复交给用户自己，或者走「卸载 → 重装」这条**本来就有完整备份**的路径。

级别约定
--------
  错误：现在起不来（缺 bin.js / 缺 node / 槽目录没了）
  警告：能跑，但和登记的配置不一致，或迟早出事（用系统 node、原生模块缺失、
        端口与 kill bat 不符、两个槽抢同一端口）
  提示：只是信息（实装版本、数据目录大小之类）
"""

import json
import os
import subprocess

try:
    import dsh_install_ops as _OPS
except Exception:                                    # 离线单测时可缺
    _OPS = None

LEVEL_ERROR, LEVEL_WARN, LEVEL_INFO = "错误", "警告", "提示"

# dsh 靠它做终端，Windows 上必须有预编译的 .node；缺了启动就崩，
# 而崩的地方在 dsh 内部，报错完全看不出跟这个有关。
_NATIVE_DEPS = ("node-pty",)

_BAT_NAMES = ("dsh.bat", "dsh-cli.bat")


def _expand(p, base="", home="", self_dir=""):
    if _OPS is not None and hasattr(_OPS, "_expand_slot_path"):
        try:
            return _OPS._expand_slot_path(p, base, home)
        except Exception:
            pass
    return str(p or "")


def _norm(p):
    """路径比较用的归一化：大小写 / 斜杠 / 末尾分隔符都不算差异。

    Windows 上 `D:\\dsh-temp\\dshV4` 与 `<临时目录根>/dshV4/` 是同一路径但
    字符串不等 —— 不归一化就会把「完全正常」报成「bat 与配置不符」。
    """
    s = str(p or "").strip().replace("\\", "/")
    while "//" in s:
        s = s.replace("//", "/")
    return s.rstrip("/").lower()


def slot_root_from_bin_js(bin_js):
    """从 `<root>/node_modules/@deepseek-ai/dsh/lib/bin.js` 反推槽根。"""
    if _OPS is not None and hasattr(_OPS, "slot_root_from_bin_js"):
        try:
            return _OPS.slot_root_from_bin_js(bin_js)
        except Exception:
            pass
    p = str(bin_js or "").replace("\\", "/")
    i = p.lower().find("/node_modules/")
    return p[:i].replace("/", os.sep) if i > 0 else ""


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _node_version(exe):
    """跑一次 `node --version`（只读）。拿不到就返回空串。"""
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def _min_node(engines):
    """从 `engines.node` 里抠出最低版本，如 ">=24.9.0" → (24, 9)。

    只认 `>=` / `>` / `^` / `~` 后面那串数字。npm 的 `engines` 语法很杂，
    这里只做「够不够老」的判断 —— 判不出来就返回 None，宁可不报也不误报。
    """
    s = str((engines or {}).get("node") or "")
    for i, ch in enumerate(s):
        if ch.isdigit():
            tail = s[i:]
            parts = []
            for seg in tail.split(".")[:2]:
                num = ""
                for c in seg:
                    if c.isdigit():
                        num += c
                    else:
                        break
                if not num:
                    break
                parts.append(int(num))
            return tuple(parts) if len(parts) == 2 else None
    return None


def _parse_version(v):
    s = str(v or "").strip().lstrip("vV")
    parts = []
    for seg in s.split(".")[:2]:
        num = ""
        for c in seg:
            if c.isdigit():
                num += c
            else:
                break
        if not num:
            return None
        parts.append(int(num))
    return tuple(parts) if len(parts) == 2 else None


def _missing_deps(node_modules, dsh_dir):
    """dsh 自己 package.json 里声明的 dependencies，哪些在 node_modules 里找不到。

    只做 `os.path.isdir` 判断（毫秒级）。嵌套安装（`a/node_modules/b`）理论上
    会漏，但那属于 npm 的正常布局，报成"缺"会误导 —— 所以调用方只在 deep
    模式下展示，并注明"可能是嵌套安装"。
    """
    pj = _read_json(os.path.join(dsh_dir, "package.json")) or {}
    deps = list((pj.get("dependencies") or {}).keys())
    miss = []
    for name in deps:
        if not os.path.isdir(os.path.join(node_modules, name)):
            miss.append(name)
    return deps, miss


def _native_ok(node_modules, dep):
    """该原生依赖下有没有 .node 二进制（只看两层，够用且不慢）。"""
    base = os.path.join(node_modules, dep)
    if not os.path.isdir(base):
        return None                       # 整个包都没有 —— 由 _missing_deps 报
    for root, dirs, files in os.walk(base):
        depth = root[len(base):].count(os.sep)
        if depth >= 3:
            dirs[:] = []
            continue
        for f in files:
            if f.endswith(".node"):
                return True
    return False


def check_slot(key, cfg, base="", home="", self_dir="", deep=False, node_version=""):
    """体检一个槽。返回 [(级别, 项目, 说明), ...]，**不写盘、不改文件**。

    deep=False 只查文件在不在（启动时自动跑，几十毫秒）；
    deep=True 额外读 dsh 的 package.json、逐个核对依赖、跑一次 node --version
    （点「体检」按钮时跑，一两百毫秒）。

    `node_version` 可外部传入（如 "v24.21.0"）：批量体检时同一个 node 只跑一次；
    传空串且 deep=True 时才自己去跑。
    """
    cfg = cfg or {}
    out = []
    bin_js = _expand(cfg.get("bin_js"), base, home, self_dir)
    node_exe = _expand(cfg.get("node_exe"), base, home, self_dir)
    dsh_home = _expand(cfg.get("dsh_home"), base, home, self_dir)
    temp_dir = _expand(cfg.get("temp_dir"), base, home, self_dir)
    port = cfg.get("port")
    root = slot_root_from_bin_js(bin_js)

    # ── 1. 程序入口 ────────────────────────────────────────────
    if not bin_js:
        out.append((LEVEL_ERROR, "入口", "config.json 里没配 bin_js"))
    elif not os.path.isfile(bin_js):
        out.append((LEVEL_ERROR, "入口",
                    "找不到 %s（槽可能被删过、或没装完）" % bin_js))
    elif root and not os.path.isdir(root):
        out.append((LEVEL_ERROR, "槽目录", "槽根目录不存在：%s" % root))

    # ── 2. node 运行时 ─────────────────────────────────────────
    if not node_exe:
        out.append((LEVEL_ERROR, "node", "config.json 里没配 node_exe"))
    elif node_exe.strip().lower() in ("node", "node.exe"):
        out.append((LEVEL_WARN, "node",
                    "用的是系统 PATH 上的 node（没有自带 node\\，与其它槽不同构；"
                    "系统 node 一升级，这个槽可能因原生模块 ABI 不匹配而崩）"))
    elif not os.path.isfile(node_exe):
        out.append((LEVEL_ERROR, "node", "找不到 %s" % node_exe))

    # ── 3. 数据目录 / 临时目录 ─────────────────────────────────
    if not dsh_home:
        out.append((LEVEL_ERROR, "数据目录", "config.json 里没配 dsh_home"))
    elif not os.path.isdir(dsh_home):
        out.append((LEVEL_WARN, "数据目录",
                    "目录还不存在（首次启动会自动建）：%s" % dsh_home))
    if not temp_dir:
        out.append((LEVEL_WARN, "临时目录",
                    "没配 temp_dir，会继承系统 TEMP，多版本之间不隔离"))
    elif not os.path.isdir(temp_dir):
        out.append((LEVEL_WARN, "临时目录", "目录还不存在：%s" % temp_dir))

    # ── 4. 三件 bat（缺了不影响管理器，但双击启动会失效）───────
    if root and os.path.isdir(root):
        for n in _BAT_NAMES:
            if not os.path.isfile(os.path.join(root, n)):
                out.append((LEVEL_WARN, "启动脚本",
                            "缺 %s（可在管理器里点「生成启动/kill/CLI bat」补）" % n))
        if port:
            kb = os.path.join(root, "kill-%s.bat" % port)
            if not os.path.isfile(kb):
                out.append((LEVEL_WARN, "启动脚本",
                            "缺 kill-%s.bat（同上）" % port))
            else:
                txt = ""
                try:
                    with open(kb, "r", encoding="gbk", errors="replace") as f:
                        txt = f.read()
                except Exception:
                    pass
                if txt and (":%d" % int(port)) not in txt and str(port) not in txt:
                    out.append((LEVEL_WARN, "启动脚本",
                                "kill-%s.bat 里没写端口 %s —— 可能改过端口没重新生成 bat"
                                % (port, port)))

        # 4b) dsh.bat 里的 TEMP 是否与 config 的 temp_dir 一致（2026-10-31）
        # 为什么查这个：临时目录改过之后，若没重建 bat，双击启动仍然用旧 TEMP ——
        # 界面/日志全对，只有「临时文件到底落在哪」是错的，几乎无法自查。
        # 只报不修（重建 bat 是用户的动作）。
        bat_start = os.path.join(root, "dsh.bat")
        if temp_dir and os.path.isfile(bat_start):
            btxt = ""
            try:
                with open(bat_start, "r", encoding="gbk", errors="replace") as f:
                    btxt = f.read()
            except Exception:
                pass
            got = ""
            for ln in btxt.splitlines():
                s = ln.strip()
                if s.lower().startswith('set "temp='):
                    got = s.split("=", 1)[1].strip().strip('"')
                    break
            if got and _norm(got) != _norm(temp_dir):
                out.append((LEVEL_WARN, "临时目录",
                            "dsh.bat 里的 TEMP 与配置不符（bat: %s / 配置: %s）—— "
                            "点「生成启动/kill/CLI bat」重建" % (got, temp_dir)))

        # 4c) 槽内 .tmp：只有**确实不是当前 TEMP、且里面还有东西**才叫历史遗留。
        # 2026-11-11 修：11-02 起新规则就是把临时文件放各槽 `.tmp`，
        # config.temp_dir 往往正好指向它。旧代码无条件报「历史遗留、可整个
        # 删除」，于是每个正常槽启动都顶一个 ⚠1，还会误导用户删掉正在用的
        # TEMP（截图里 dsh.bat 的 TEMP 与 config 一致，已证明它是当前目录）。
        legacy = os.path.join(root, ".tmp")
        if os.path.isdir(legacy) and _norm(legacy) != _norm(temp_dir or ""):
            n = 0
            try:
                n = len(os.listdir(legacy))
            except Exception:
                pass
            if n:
                out.append((LEVEL_WARN, "临时目录",
                            "槽内还有历史遗留的 .tmp（%d 项，旧规则留下的，"
                            "现在的 TEMP 已不指向它，可整个删除）" % n))

    # 4d) 临时目录里堆了多少残留（强杀时 dsh 来不及清理自己建的临时目录）
    if temp_dir and os.path.isdir(temp_dir):
        try:
            n = len(os.listdir(temp_dir))
        except Exception:
            n = 0
        if n > 20:
            out.append((LEVEL_WARN, "临时目录",
                        "%s 累积了 %d 项（多为强杀/崩溃留下的残留，可清空）"
                        % (temp_dir, n)))

    # ── 5. 深层：包本身是否完整 ────────────────────────────────
    if deep and root:
        dsh_dir = os.path.join(root, "node_modules", "@deepseek-ai", "dsh")
        node_modules = os.path.join(root, "node_modules")
        pj = _read_json(os.path.join(dsh_dir, "package.json"))
        if pj is None:
            out.append((LEVEL_ERROR, "包清单",
                        "读不到 %s（目录可能被删/被改）" % os.path.join(dsh_dir, "package.json")))
        else:
            ver = str(pj.get("version") or "")
            if ver:
                out.append((LEVEL_INFO, "实装版本", ver))
            deps, miss = _missing_deps(node_modules, dsh_dir)
            if miss:
                out.append((LEVEL_ERROR, "依赖",
                            "dsh 声明的 %d 个依赖里有 %d 个找不到：%s%s"
                            % (len(deps), len(miss), "、".join(miss[:6]),
                               " 等" if len(miss) > 6 else "")))
            else:
                out.append((LEVEL_INFO, "依赖", "%d 个依赖齐备" % len(deps)))
            for dep in _NATIVE_DEPS:
                if dep not in deps:
                    continue
                ok = _native_ok(node_modules, dep)
                if ok is False:
                    out.append((LEVEL_ERROR, "原生模块",
                                "%s 目录在，但里面没有任何 .node 二进制 —— "
                                "启动会崩（多半是 npm 装的时候没跑 postinstall）" % dep))
            # node 版本 vs dsh 要求的 engines
            need = _min_node(pj.get("engines"))
            if need:
                got_txt = node_version
                if not got_txt and node_exe and os.path.isfile(node_exe):
                    got_txt = _node_version(node_exe)
                    if not got_txt:
                        # node.exe 在，但跑不起来 —— 被换成别的文件、或缺 DLL。
                        # 这比"版本太低"更常见，且症状一样是启动即崩。
                        out.append((LEVEL_WARN, "node 版本",
                                    "node.exe 在，但跑 `--version` 失败 —— "
                                    "文件可能损坏或被替换"))
                got = _parse_version(got_txt)
                if got and got < need:
                    out.append((LEVEL_ERROR, "node 版本",
                                "该版本 dsh 要求 node >= %d.%d，本槽是 %s —— "
                                "原生模块会因 ABI 不匹配而崩"
                                % (need[0], need[1], got_txt)))
                elif got:
                    out.append((LEVEL_INFO, "node 版本",
                                "%s（要求 >= %d.%d）" % (got_txt, need[0], need[1])))
    return out


def check_all(versions, base="", home="", self_dir="", deep=False, node_version=""):
    """体检全部槽。返回 {槽键: [(级别, 项目, 说明)]}，另加一个 "_global" 键放跨槽问题。

    跨槽问题两条：
      1. 两个槽配了同一个端口（后启动的那个必然失败，而界面只会显示
         "端口被非 dsh 程序占用"，极难自查）；
      2. 两个槽指向同一个槽目录（2026-11-13：旧版「重新纳入」造出的幽灵槽）。
    """
    result = {}
    seen = {}
    for key, cfg in (versions or {}).items():
        result[key] = check_slot(key, cfg, base, home, self_dir,
                                 deep=deep, node_version=node_version)
        port = (cfg or {}).get("port")
        if port:
            seen.setdefault(int(port), []).append(key)

    glob = []
    for port, keys in sorted(seen.items()):
        if len(keys) > 1:
            glob.append((LEVEL_ERROR, "端口冲突",
                         "%s 都配了端口 %d —— 后启动的必然失败" % ("、".join(keys), port)))

    # 两个槽指向同一个槽目录（2026-11-13 新增）。
    # 旧版「重新纳入」会把旧记录的 label / port / 路径整套复制到新槽键上，
    # 于是 config 里出现「槽 v4 顶着 dshV3 的名字、和 v3 抢 6080、三个路径都
    # 指向 dshV3」的幽灵槽。这类记录必须报出来 —— 它会让界面出现两张同名
    # 卡片，而用户以为"我装了 v4"。
    dirs = {}
    for key, cfg in (versions or {}).items():
        bj = _expand((cfg or {}).get("bin_js") or "", base, home, self_dir)
        r = slot_root_from_bin_js(bj)
        if r:
            dirs.setdefault(_norm(r), []).append(key)
    for _d, keys in sorted(dirs.items()):
        if len(keys) > 1:
            glob.append((LEVEL_ERROR, "重复槽",
                         "%s 指向同一个槽目录（%s）—— 其实是同一个版本，"
                         "删掉多余的那条记录即可" % ("、".join(keys), _d)))
    result["_global"] = glob
    return result


def worst_level(items):
    """一组问题里最严重的级别，没有问题时返回空串（供卡片上色用）。"""
    levels = [it[0] for it in (items or [])]
    for lv in (LEVEL_ERROR, LEVEL_WARN, LEVEL_INFO):
        if lv in levels:
            return lv
    return ""


def summary(items):
    """一行摘要，如「2 个错误、1 个警告」。"""
    if not items:
        return "未发现问题"
    cnt = {}
    for it in items:
        cnt[it[0]] = cnt.get(it[0], 0) + 1
    order = [LEVEL_ERROR, LEVEL_WARN, LEVEL_INFO]
    return "、".join("%d 个%s" % (cnt[lv], lv) for lv in order if cnt.get(lv))
