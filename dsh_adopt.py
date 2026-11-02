# -*- coding: utf-8 -*-
"""dsh_manager 的「发现已有安装」与「一键装官方版」支撑模块。

为什么独立成文件
----------------
dsh_manager.pyw 有二十多处模块级副作用（建 GUI、写日志），不可 import、不可单测。
本模块刻意保持**纯函数 + 无副作用**：不碰 tkinter、不起子进程、不写文件（除了
显式的备份函数）。这样 scripts/test-manager-logic.py 能直接 import 它做真断言，
而不必像测主文件那样绕道 AST 抽取。

两件事
------
1. detect_existing_installs() —— 扫出本机已装但**不在管理槽里**的 dsh，
   供启动时询问用户是否纳入管理。
2. 槽位规划（unique_slot_dir / next_slot_key / free_port / build_slot_config）
   ——「一键装官方版」要用：算下一个槽位 key / 目录名（同名加数字后缀）/ 空闲端口。
"""
import json
import os
import re
import shutil
import time

# 官方版本号形态（实测自 npm registry dist-tags 与 versions 键）：
#   0.0.1-rc.1 / 0.1.0-rc.2 / 0.1.2-alpha.5 / 0.1.5-alpha.1 / 0.1.6-alpha.2 / 0.2.0-rc.2
# 三段式 + 预发布标记。刻意**不含** v 前缀（用户手打 v0.2.0 常见，靠 normalize 纠）。
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?$")

# 常见手误 → 修正规则。安装内核也会按 npm 的报错兜底，但那时已经起进程了；
# 这里先纠能少一次往返。
# 注意：修正规则**逐条串行尝试**，命中一条即停 —— 所以顺序有意义：
# 先剥 v 前缀，再补预发布标记的连字符，最后把空格换成点。
_VERSION_FIXES = [
    (re.compile(r"^v(?=\d)"), ""),                          # v0.2.0-rc.2  → 0.2.0-rc.2
    (re.compile(r"^(\d+\.\d+\.\d+)[- ]?(alpha|beta|rc)[- ]?(\d+)$", re.I),
     r"\1-\2.\3"),                                          # 0.2.0rc2 / 0.2.0 rc 2 → 0.2.0-rc.2
]


def normalize_version(text):
    """把用户填的版本号规整成官方形态；不合法则返回 ''。

    为什么不直接透传给 npm：用户会在对话框里打 'v0.2.0-rc.2'、'0.2.0rc.2'、
    '0.2.0-rc 2' 这类手误，直接透传会走到 npm 才报错，用户以为是网络问题。
    这里先纠 + 先校验，错误当场可见。"""
    s = (text or "").strip()
    if not s:
        return ""
    for pat, rep in _VERSION_FIXES:
        s2 = pat.sub(rep, s)
        if s2 != s:
            return s2
    return s if VERSION_RE.match(s) else ""


def version_hint():
    """给用户的格式说明（放在输入框下方，避免他凭空猜）。"""
    return "官方版本号形如 0.2.0-rc.2 / 0.1.6-alpha.2（三段数字 + -rc.N 或 -alpha.N）"


# ══════════════════════════════════════════════════════════════════
# 1. 发现本机已有安装
# ══════════════════════════════════════════════════════════════════

def read_package_version(dsh_dir):
    """读 <dsh_dir>/package.json 的 version；读不到返回 ''。

    dsh_dir 应是 @deepseek-ai/dsh 包目录（含 package.json 与 lib/bin.js）。"""
    if not dsh_dir or not os.path.isdir(dsh_dir):
        return ""
    pj = os.path.join(dsh_dir, "package.json")
    if not os.path.isfile(pj):
        return ""
    try:
        with open(pj, "r", encoding="utf-8-sig") as f:
            return str(json.load(f).get("version") or "")
    except Exception:
        return ""


def _looks_like_dsh_install(node_modules_dir):
    """<某处>/node_modules 里是否装着 @deepseek-ai/dsh。
    返回该包目录；没有返回 ''。"""
    cand = os.path.join(node_modules_dir, "@deepseek-ai", "dsh")
    return cand if os.path.isdir(cand) else ""


def is_global_install(node_modules):
    """这份安装是不是 npm 全局安装（%APPDATA%\\npm\\node_modules 或 nodejs 自带目录）。

    为什么要区分：全局安装的用户数据在 `~/.dsh`；而**本地隔离安装**的数据在
    它自己的 `<安装目录>/.dsh-home`。搞混了就会去备份/复制另一个安装的数据
    （实测踩过：拿临时目录里造的假安装去跑测试，却去备份了本机真实的 ~/.dsh）。"""
    nm = os.path.abspath(node_modules or "").replace("\\", "/").lower()
    if not nm:
        return False
    appdata = os.environ.get("APPDATA", "").replace("\\", "/").lower().rstrip("/")
    if appdata and nm.startswith(appdata + "/npm/"):
        return True
    # nodejs 自带 npm 的全局目录：...\Programs\nodejs\node_modules
    if nm.endswith("/programs/nodejs/node_modules") or \
            nm.endswith("/program files/nodejs/node_modules"):
        return True
    # 刻意**不**用「路径里含 /npm/node_modules」这种宽判据：任何临时目录下造个
    # npm/node_modules 都会被误判成全局安装，进而把用户主目录的 ~/.dsh 当成它的
    # 数据目录去备份（实测踩过：测试脚本因此去拷了几 GB 真实数据，进程被拖死）。
    return False


def guess_source_home(node_modules):
    """猜这份安装「原来的用户数据目录」在哪，返回 (路径, 人话说明)。

    只认两种有把握的情形，其余一律返回 ("", "") —— 宁可当"没有旧数据"，
    也不能把别的安装的数据当成它的：
      · 本地隔离安装：<X>/node_modules + <X>/.dsh-home（我们的槽就是这种）
      · npm 全局安装：数据在 dsh 的默认 home ~/.dsh
    **只找不搬** —— 调用方负责备份/复制。
    """
    nm = os.path.abspath(node_modules or "")
    parent = os.path.dirname(nm)
    own = os.path.join(parent, ".dsh-home")
    if os.path.isdir(own):
        return (own, "安装目录里的 .dsh-home")
    if is_global_install(nm):
        default_home = os.path.join(os.path.expanduser("~"), ".dsh")
        if os.path.isdir(default_home):
            return (default_home, "全局安装的默认 home ~/.dsh")
    return ("", "")


def inspect_adopt_source(node_modules, report_limit=5):
    """体检一份「别人装好的 dsh」能不能规范地纳入管理（纯只读）。

    为什么必须体检：纳入的动作是「复制一份到我们的槽目录并登记」，如果来源本身
    是半装/装坏的（目录在、package.json 或 bin.js 缺失、依赖没装全），复制过去
    只会得到一个**看起来正常但一启动就炸**的槽 —— 比直接报错更坏，因为用户会
    以为是管理器的启动逻辑有问题。

    返回 dict：
      ok            能否纳入
      version       版本号（读不到为 ''）
      dsh_dir       @deepseek-ai/dsh 包目录
      problems      [str] 致命问题（非空 = 不许纳入）
      warnings      [str] 能纳入、但必须告诉用户的差异
      deps_missing  [str] 缺的依赖（已计入 problems）
      source_home   原来的用户数据目录（'' = 没找到）
      source_home_why  该目录的判断依据（给界面显示）
    """
    info = {"ok": False, "version": "", "dsh_dir": "", "problems": [],
            "warnings": [], "deps_missing": [], "source_home": "",
            "source_home_why": ""}
    nm = os.path.abspath(node_modules or "")
    if not nm or not os.path.isdir(nm):
        info["problems"].append("目录不存在：%s" % (nm or "(空)"))
        return info

    dsh_dir = _looks_like_dsh_install(nm)
    if not dsh_dir:
        info["problems"].append("这里没有 @deepseek-ai/dsh（不是 dsh 的 node_modules）")
        return info
    info["dsh_dir"] = dsh_dir

    # 1) package.json 与版本号
    pj = os.path.join(dsh_dir, "package.json")
    meta = {}
    if not os.path.isfile(pj):
        info["problems"].append("缺 package.json（装了一半，或被删过）")
    else:
        try:
            with open(pj, "r", encoding="utf-8-sig") as f:
                meta = json.load(f)
        except Exception as e:
            meta = {}
            info["problems"].append("package.json 读不出来：%s" % e)
        ver = str((meta or {}).get("version") or "")
        if ver and not VERSION_RE.match(ver):
            info["problems"].append("版本号形态不对：%r" % ver)
        elif not ver:
            info["problems"].append("package.json 里没有 version")
        info["version"] = ver if VERSION_RE.match(ver or "") else ""

    # 2) 入口文件
    bin_js = os.path.join(dsh_dir, "lib", "bin.js")
    if not os.path.isfile(bin_js):
        info["problems"].append("缺 lib/bin.js（没有入口，纳入后必然启动失败）")
    lib = os.path.join(dsh_dir, "lib")
    try:
        n_lib = len([x for x in os.listdir(lib) if x.lower().endswith(".js")]) \
            if os.path.isdir(lib) else 0
    except Exception:
        n_lib = 0
    if os.path.isdir(lib) and n_lib == 0:
        info["problems"].append("lib/ 里一个 .js 都没有（空壳目录）")

    # 3) 依赖是否装全（只看 package.json 声明的一层依赖，npm 会提升到顶层）
    deps = {}
    if isinstance(meta, dict):
        deps = meta.get("dependencies") or {}
        if not isinstance(deps, dict):
            deps = {}
    missing = []
    for name in deps:
        hit = (os.path.isdir(os.path.join(nm, name))
               or os.path.isdir(os.path.join(dsh_dir, "node_modules", name)))
        if not hit:
            missing.append(name)
    info["deps_missing"] = missing
    if missing:
        shown = ", ".join(missing[:report_limit])
        more = "" if len(missing) <= report_limit else "（还有 %d 个）" % (
            len(missing) - report_limit)
        info["problems"].append(
            "依赖没装全：%d 个声明依赖找不到 —— %s%s" % (len(missing), shown, more))

    # 4) 只是提醒的差异
    if not os.path.isfile(os.path.join(nm, "..", "package-lock.json")):
        info["warnings"].append("没有 package-lock.json（依赖版本没被钉住）")
    norm = nm.replace("\\", "/").lower()
    if is_global_install(nm):
        info["warnings"].append(
            "这是 npm 全局安装（%s）—— 纳入会复制一份独立副本，"
            "全局那份不受影响，但两者以后各升各的" % nm)
    if os.path.isdir(os.path.join(nm, "node-pty")) or \
            os.path.isdir(os.path.join(dsh_dir, "node_modules", "node-pty")):
        info["warnings"].append(
            "含原生模块 node-pty：复制过来的那份必须配同 ABI 的 node，"
            "所以纳入时会一并复制一个自带 node")

    home, why = guess_source_home(nm)
    info["source_home"], info["source_home_why"] = home, why
    if not home:
        info["warnings"].append(
            "没找到原来的用户数据目录（.dsh-home / ~/.dsh）—— 纳入后是一个干净的新槽")

    info["ok"] = not info["problems"]
    return info


def scan_npm_globals(candidates):
    """在若干 node_modules 根里找 @deepseek-ai/dsh。

    candidates 通常是 [APPDATA/npm/node_modules, LOCALAPPDATA/Programs/nodejs/node_modules, ...]
    —— 静态位置优先，因为起 npm 子进程在 pythonw 下会卡管道（见主文件 _probe_npm_global 的注释）。
    返回 [{node_modules, dsh_dir, version}]。"""
    found, seen = [], set()
    for nm in candidates:
        if not nm:
            continue
        key = os.path.normcase(os.path.abspath(nm))
        if key in seen:
            continue
        seen.add(key)
        d = _looks_like_dsh_install(nm)
        if d:
            found.append({
                "node_modules": os.path.abspath(nm),
                "dsh_dir": os.path.abspath(d),
                "version": read_package_version(d),
            })
    return found


def scan_managed_dirs(versions_root, managed_roots=()):
    """扫本管理器的版本根（如 <home>/dsh-versions），找所有已装 dsh。

    managed_roots：已登记在 config.json 里的安装根（绝对路径），
    用于把「已经在管理中」的排除掉，只报告**孤儿**。
    返回 [{root, dsh_dir, version, managed:bool}]。"""
    out = []
    if not versions_root or not os.path.isdir(versions_root):
        return out
    managed = set()
    for r in managed_roots:
        if r:
            managed.add(os.path.normcase(os.path.abspath(r)))
    try:
        entries = sorted(os.listdir(versions_root))
    except Exception:
        return out
    for name in entries:
        root = os.path.join(versions_root, name)
        if not os.path.isdir(root):
            continue
        d = _looks_like_dsh_install(os.path.join(root, "node_modules"))
        if not d:
            continue
        out.append({
            "root": os.path.abspath(root),
            "dsh_dir": os.path.abspath(d),
            "version": read_package_version(d),
            "managed": os.path.normcase(os.path.abspath(root)) in managed,
        })
    return out


def detect_existing_installs(npm_globals, versions_root, managed_roots=()):
    """汇总「本机装了、但不在管理槽里」的 dsh。

    返回 {version, locations:[{kind, path, version}]} 列表，按版本号排序。
    kind 取 'npm-global' / 'versions-dir'，用于界面分组与文案。"""
    buckets = {}

    def add(kind, path, version, node_modules=""):
        if not version:
            return
        b = buckets.setdefault(version, {"version": version, "locations": []})
        b["locations"].append({"kind": kind, "path": path, "node_modules": node_modules})

    for f in scan_npm_globals(npm_globals):
        add("npm-global", f["dsh_dir"], f["version"], f["node_modules"])

    for f in scan_managed_dirs(versions_root, managed_roots):
        if not f["managed"]:                     # 已在 config.json 里的不重复报告
            # 2026-11-01 修 bug：dsh_dir 是 .../node_modules/@deepseek-ai/dsh，
            # 要的是 .../node_modules —— 必须向上两级，只 dirname 一次会得到
            # .../@deepseek-ai，后续 inspect 再拼 "@deepseek-ai/dsh" 就成了
            # .../@deepseek-ai/@deepseek-ai/dsh，判定为"这里没有 dsh"→ 纳入项被禁用。
            add("versions-dir", f["dsh_dir"], f["version"],
                os.path.dirname(os.path.dirname(f["dsh_dir"])))

    def _key(v):
        # 排序用「版本元组」，让 0.1.9 < 0.1.10；非标准串排最后。
        m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-(.+))?$", v)
        if not m:
            return (1, 0, 0, 0, v)
        tail = m.group(4) or ""
        rank = {"alpha": 0, "beta": 1, "rc": 2}.get(tail.split(".")[0], 3)
        return (0, int(m.group(1)), int(m.group(2)), rank * 1000 + int(m.group(3)), tail)

    return sorted(buckets.values(), key=lambda b: _key(b["version"]), reverse=True)


# ══════════════════════════════════════════════════════════════════
# 2. 槽位规划（一键装官方版用）
# ══════════════════════════════════════════════════════════════════

def next_slot_key(existing_keys):
    """下一个可用槽位键：v1 / v2 / … 跳过已占用的。

    为什么不复用空着的键：槽位一旦分配并写进 config.json，就成了用户心里的
    "第 N 个环境"。中途插队会让界面顺序（按 SLOT_KEYS 排列）突然变化。
    永远往后加更符合直觉。"""
    used = set(existing_keys or [])
    i = 1
    while ("v%d" % i) in used:
        i += 1
    return "v%d" % i


def unique_dir(parent, name):
    """在 parent 下取一个可用目录名；同名就加数字后缀：dshV1 → dshV1-2 → dshV1-3。

    为什么用 '-' 而不是别的：dshV1-2 读起来仍是同一族，且不会被 Windows 的
    保留名（CON/PRN…）或尾点尾空格规则绊住。上限 999 次后放弃（实际不可能）。
    返回 (full_path, 实际使用的目录名)。"""
    p = os.path.join(parent, name)
    if not os.path.exists(p):
        return p, name
    for n in range(2, 1000):
        cand = "%s-%d" % (name, n)
        p2 = os.path.join(parent, cand)
        if not os.path.exists(p2):
            return p2, cand
    return None, ""


def free_port(start, taken, probe=None, span=200, skip=None):
    """从 start 起找一个既不在 taken 里、探测也没被占用的端口。

    taken：配置里已用端口（避免两槽撞车）。
    probe(port)->bool：端口是否被占用；默认用 socket 试bind。
    span：往后最多试多少个（默认 200）。
    skip：**额外要跳过**的端口集合（2026-11-01 新增，用于避开 6080/8080 这类
          常被别的软件占的端口）。它和 taken 的区别是语义：taken 是"我们自己在用"，
          skip 是"我们不想用"，但两者都会让循环直接跳过去。
    找不到返回 None（由调用方报错，不静默取一个撞车的端口）。"""
    if probe is None:
        import socket

        def probe(port):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(("127.0.0.1", port))
                return False
            except OSError:
                return True
            finally:
                s.close()

    taken = set(taken or [])
    skip = set(skip or [])
    for p in range(int(start), int(start) + int(span)):
        if p in taken or p in skip:
            continue
        try:
            if not probe(p):
                return p
        except Exception:
            continue
    return None


# 端口步进：千位递增，观感统一（4080 / 5080 / 6080 / 7080 / 8080）。
# 起点固定 4080：官方默认是 3080，这里刻意避开，免得和"没被管理器接管的那份
# dsh"或别的常见本地服务打架。
PORT_BASE = 4080
PORT_STEP = 1000

# 2026-11-01（用户明确要求，推翻当天上午的"避让表"方案）：
#   「端口号不用特意避开 6080 或 8080，都可以用，前提是不能与已经有的其他 app 的端口号冲突。」
#
# 所以回到最简单的规则：**按槽序千位递增**，唯一判据是"这个端口此刻有没有被占"。
# 不做"哪个端口看起来安全"的猜测 —— 那种硬编码表维护成本高，且挡不住真正占用的软件
# （6080 空着就能用；用了 9999 的软件照样会撞）。
#
# 冲突检测有三道，全部在 free_port 里：
#   ① taken     —— config 里其它槽已用的端口（自己人之间不撞）
#   ② probe     —— 真实试 bind，看系统层面是否已被任何程序占用
#   ③ OS 层     —— bind 失败即视为占用，直接顺延
_PORT_TAKEN_RISK = ()          # 保留名字（测试与调用点引用），不再排除任何端口
_PORT_PREFERRED = ()           # 同上：不再有"首选表"


def suggest_port(slot_index, taken, base=PORT_BASE, step=PORT_STEP, probe=None):
    """按槽序建议端口：1→4080、2→5080、3→6080 …（每槽递增 1000），撞了就往后顺延。

    slot_index 是 **1 基**的槽序（第 1 个槽传 1），不是数组下标。

    唯一的判据是"**有没有被占用**"，不预设哪个号段危险：
      ① 先按槽序算出顺眼的千位端口，从它起在 200 个以内找最近的空闲端口
         （既跳过 taken，也真实试 bind 探测系统占用）；
      ② 这一段全被占（或全在 taken 里）就退到 20000–29999 找一个空的。
    两段都找不到才返回 None —— 调用方必须报错，绝不能静默返回一个撞车的端口。
    """
    p = free_port(base + step * (int(slot_index) - 1), taken, probe=probe)
    if p:
        return p
    return free_port(20000, taken, probe=probe, span=10000)


def portable_path(path, base=""):
    """把绝对路径写成可移植的 `<base>/…` 形式（正斜杠，与既有槽的写法一致）。

    为什么要有它：config.json 里 v1–v3 的 `node_exe` / `bin_js` / `dsh_home`
    全是 `<base>/dsh-versions/dshVx/…`，换用户名、换盘符都不断链。但装新槽时
    一直写的是绝对路径（`<home>/…` 展开后的真实盘符），于是新槽天然成了"异类"，
    审计报告 P3-1 也提过这种混写。现在统一由这里产出。
    base 为空或路径不在 base 之下 → 原样返回（只把反斜杠换成正斜杠）。
    """
    s = str(path or "").replace("\\", "/")
    if not s:
        return ""
    b = str(base or "").replace("\\", "/").rstrip("/")
    if b and (s.lower() == b.lower() or s.lower().startswith(b.lower() + "/")):
        return "<base>" + s[len(b):]
    return s


# 临时目录根的兜底候选（2026-10-31 之前的规则）。
# 历史：v1–v3 手工部署时把 TEMP 放在 D 盘（本机 C 盘长期紧张），于是新槽也
# 跟着写 <临时目录根>\<槽名>。但这条规则**把临时目录和安装位置解绑了** ——
# 槽装在 C 盘、临时文件在 D 盘，换盘/换 install_base 要改两处，而且用户
# 在界面上完全看不出这层关系（`dshV4\.tmp` 就是这么来的）。
# 现在改为 temp_root 优先（默认 <槽根>/dsh-temp），这里只作兼容兜底。
TEMP_ROOT_CANDIDATES = ("D:\\dsh-temp",)

# 临时目录根下面按槽名分文件夹，与备份目录（<base>\dsh备份\<槽>-userdata-…）同构。
DEFAULT_TEMP_DIRNAME = "dsh-temp"


def _temp_candidates():
    r"""真实盘符候选（下面 `default_temp_dir` 的第 2 步用）。

    **离线回归必须关掉它。** 理由不是洁癖：`run_official_install` 和
    `adopt_existing` 都会 `makedirs(temp_dir)`，而回归里这两个函数是拿真模块
    跑的（只把 npm 内核换成替身），且不传 `temp_root` —— 于是每跑一次总检
    （含每次 `git commit` 的 pre-commit 钩子），`D:\dsh-temp` 下就凭空
    长出 `dshV1`/`dshV2`/`dshV2-2`/`dshV9` 一串空文件夹。

    `dshV9` 只存在于 `scripts/test-install-ops.py` 里，从来不是真的槽 ——
    看到它出现在磁盘上，就说明是回归自己造的。

    测试脚本在 import 之后设 `DSH_TEMP_NO_REAL_DISK=1` 即可让候选列表为空，
    `default_temp_dir` 会落到第 3 步（槽内 `.tmp`，落在测试自己的 tmp 里）。
    规则本身的断言用显式 `candidates=` 注入，不依赖本机有没有 D 盘。
    """
    if os.environ.get("DSH_TEMP_NO_REAL_DISK") == "1":
        return ()
    return TEMP_ROOT_CANDIDATES


def default_temp_dir(dir_name, root, temp_root="", candidates=None):
    """算出新槽的临时目录。

    优先级（2026-10-31 起）：
      1. `temp_root` 显式给定 → `<temp_root>\\<槽目录名>`
         （管理器传 config 顶层 `temp_root`，缺省 = `<槽根>/dsh-temp`）
      2. 老规则：`<候选盘>\\dsh-temp\\<槽目录名>`，候选盘不存在才跳过
         （回归测试会把候选列表清空，见 `_temp_candidates`）
      3. 兜底：槽内 `.tmp`

    第 2 步为什么还留着：单独跑本模块（离线回归 / 手工装）时没有 config，
    给它一个「和本机既有部署一致」的结果，不至于落到槽内 `.tmp` 变成异类。

    返回的是**绝对路径**（不写成占位符）；要写进 config 时由
    `portable_path()` 转成 `<base>/…`。
    candidates 可注入，便于测试在不依赖本机盘符的情况下断言规则。
    """
    # 2026-11-02 新规则（用户要求）：临时文件直接放进各自槽目录下的 .tmp，
    # 这样设置里只需要一个「安装根」，不再单独管临时目录根。
    # 优先级调整：显式 temp_root > 槽内 .tmp > 老候选盘规则
    if temp_root:
        return os.path.join(temp_root, dir_name)
    if root:
        return os.path.join(root, ".tmp")
    for c in (_temp_candidates() if candidates is None else candidates):
        drive = os.path.splitdrive(str(c))[0]
        if drive and os.path.isdir(drive + os.sep):
            return os.path.join(c, dir_name)
    return os.path.join(root, ".tmp")


def build_slot_config(slot_key, label, root, dsh_home, temp_dir, port, node_exe,
                     browser="default", install_base=""):
    """按既有 config.json 的形状拼出一个新槽配置。

    字段与 config/config.example.json 保持一致 —— 包括 extra_args 里必须
    显式重复 --port 这件反直觉的事：主程序的 _build_cmd 真正读的是 extra_args，
    而 port 字段只用于预检与状态显示。少一处就会触发 _check_port_consistency 的拦截。
    （见主文件 _check_port_consistency 的注释：双源不一致 = 静默失效。）

    `install_base` 给定时，槽内路径写成 `<base>/…` 占位符 —— 既有四槽都是这么写的
    （换用户名/换盘符不断链）。不给就写绝对路径，便于单独跑本模块做测试。
    """
    j = lambda *p: os.path.join(*p).replace("\\", "/")
    pp = lambda p: portable_path(p, install_base)
    return {
        "label": label,
        "node_exe": pp(node_exe),
        "bin_js": pp(j(root, "node_modules", "@deepseek-ai", "dsh", "lib", "bin.js")),
        "dsh_home": pp(dsh_home),
        "temp_dir": pp(temp_dir),
        "port": int(port),
        "browser": browser,
        "extra_args": ["web", "--host", "127.0.0.1",
                       "--port", str(int(port)), "--no-open"],
    }


def copy_tree(src, dst):
    """复制目录树（用于把已装好的 dsh 整理进新槽）。返回 (成功, 说明)。"""
    src, dst = os.path.abspath(src), os.path.abspath(dst)
    if not os.path.isdir(src):
        return (False, "源目录不存在：%s" % src)
    if os.path.exists(dst):
        return (False, "目标已存在：%s" % dst)
    try:
        shutil.copytree(src, dst)
    except Exception as e:
        return (False, "复制失败：%s" % e)
    return (True, "")


def copy_node_exe(src_node, dst_root):
    """把一份 node 运行时复制进新槽（隔离关键：每槽自带 node，升级不互相影响）。

    src_node 可以是 **node.exe 文件本身**，也可以是**它所在的 node 目录**
    （既有槽就是 <槽>/node/node.exe 这种布局，调用方手上常常只有目录）。
    返回 (目标 node.exe 路径, 说明)。找不到可用源就如实报错 —— 不静默退回
    系统 node，因为 node-pty 等原生模块按 ABI 编译，版本不匹配会直接崩。"""
    src_node = os.path.abspath(src_node or "")
    if os.path.isfile(src_node):
        src_dir = os.path.dirname(src_node)
    elif os.path.isdir(src_node):
        src_dir = src_node
    else:
        return (None, "找不到可用的 node 运行时：%s" % src_node)

    # 必须是能真正跑的 node.exe，否则复制了也是废的（只拷个目录进来更误导）
    exe = os.path.join(src_dir, "node.exe")
    if not os.path.isfile(exe):
        return (None, "该目录下没有 node.exe：%s" % src_dir)

    dst_dir = os.path.join(dst_root, "node")
    try:
        os.makedirs(dst_dir, exist_ok=True)
        shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
    except Exception as e:
        return (None, "复制 node 失败：%s" % e)
    return (os.path.join(dst_dir, "node.exe"), "")


def adopt_plan(node_modules, versions_root, slot_key, taken_ports,
               node_exe_source=None, browser="default", temp_root=None):
    """为「把一份已装好的 dsh 纳入管理」算出一份完整方案（不执行任何写操作）。

    返回 dict，可直接喂给 build_slot_config；另附 root/dir_name/backup 建议。
    纯计算 —— 调用方确认后才做复制与写 config，出错时不会留下半成品。

    关于"整理"的含义：把 node_modules（含全部依赖）复制到
    <versions_root>/<dir_name>/ 下，与既有槽完全同构。之后管理器就能用
    标准流程启动/停止/换版本，不需要为"外来安装"写任何特判。"""
    nm = os.path.abspath(node_modules or "")
    if not os.path.isdir(nm):
        return {"ok": False, "error": "node_modules 目录不存在：%s" % nm}

    root, dir_name = unique_dir(versions_root, "dsh%s" % slot_key.upper())
    if not root:
        return {"ok": False, "error": "无法在 %s 下分配目录名（试了 999 次都被占用）" % versions_root}

    port = suggest_port(int(re.sub(r"\D", "", slot_key) or 1), taken_ports)
    if port is None:
        return {"ok": False, "error": "4080 起的端口段都找不到空闲端口，请手工指定"}

    temp_root = temp_root or ""
    temp_dir = default_temp_dir(dir_name, root, temp_root=temp_root)
    # DSH_HOME 必须是新槽目录下的独立子目录 —— 不能指向原安装的 ~/.dsh，
    # 否则两个实例共享会话与凭据，隔离就白做了。
    dsh_home = os.path.join(root, ".dsh-home")

    return {
        "ok": True,
        "slot_key": slot_key,
        "label": dir_name,
        "root": root,
        "dir_name": dir_name,
        "port": port,
        "dsh_home": dsh_home,
        "temp_dir": temp_dir,
        "node_exe": os.path.join(root, "node", "node.exe"),
        "node_exe_source": node_exe_source or "",
        "browser": browser,
    }


def _is_reparse(path):
    """是不是 junction / 符号链接。

    **不能用 os.path.islink()** —— 它只认符号链接，对 Windows 的 junction 一律返回
    False。而 dsh 的 `.dsh-home/profiles/*/node_modules` 里全是 junction。
    必须读文件属性位（FILE_ATTRIBUTE_REPARSE_POINT = 0x400）。"""
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)


def collect_links(src):
    """列出 src 下所有 junction / 符号链接，返回 [(相对路径, 目标)]。

    优先借 dsh_uninstall_ops 的实现（已验证过 1.5 万文件约 0.2 秒）；
    那边不在时用本文件的兜底实现，保证纳入功能不依赖另一个模块。"""
    try:
        import dsh_uninstall_ops as _U
        return _U._collect_links(src)
    except Exception:
        pass
    out = []
    stack = [src]
    while stack:
        s = stack.pop()
        try:
            entries = list(os.scandir(s))
        except OSError:
            continue
        for e in entries:
            if _is_reparse(e.path):
                try:
                    out.append((os.path.relpath(e.path, src), os.readlink(e.path)))
                except OSError:
                    out.append((os.path.relpath(e.path, src), ""))
            else:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                except OSError:
                    pass
    return out


def copy_tree_nolinks(src, dst, failures=None):
    """复制目录，**不跟随** junction / 符号链接（链接本身不复制，稍后重建）。

    为什么不能直接 shutil.copytree（2026-10-30 实测，与卸载备份同一个坑）：
    `.dsh-home/profiles/*/node_modules` 里全是指向槽 node_modules 或槽外插件仓库的
    junction，copytree 会跟进去把内容整份复制 —— 487 MB 的目录变成 942 MB、6 万文件、
    14 分钟（用户以为卡死），而且复制出来的是**内容副本**而不是链接：
    以后插件一升级，两边就各走各的。
    返回 True 表示用了快路（robocopy /XJ），False 表示退回逐文件。

    `failures`（2026-10-31 新增）：拷不动的文件追加进这个 list。以前是 `except
    OSError: pass` —— 复制"看起来成功"，实际少了文件，而调用方会当成已带过来。
    调用方拿到 failures 就能当场中止（见 `verify_copy`）。
    """
    fails = failures if failures is not None else []
    try:
        import dsh_uninstall_ops as _U
        os.makedirs(dst, exist_ok=True)
        if _U._robocopy(src, dst)[0]:
            return True
    except Exception:
        pass
    os.makedirs(dst, exist_ok=True)
    stack = [(src, dst)]
    while stack:
        s, d = stack.pop()
        try:
            entries = list(os.scandir(s))
        except OSError as e:
            fails.append((os.path.relpath(s, src) or ".", "读不到目录：%s" % e))
            continue
        for e in entries:
            if _is_reparse(e.path):
                continue          # 链接：跳过，由 relink_home 重建
            target = os.path.join(d, e.name)
            try:
                if e.is_dir(follow_symlinks=False):
                    os.makedirs(target, exist_ok=True)
                    stack.append((e.path, target))
                else:
                    shutil.copy2(e.path, target)
            except OSError as ex:
                # 拷不动（被占用/权限）不能静默跳过：这可能是用户的会话或凭据
                fails.append((os.path.relpath(e.path, src), str(ex)))
    return False


def verify_copy(src, dst):
    """核对复制结果是否完整。返回 (missing, size_diff, total)。

    判据与卸载备份的 `dsh_uninstall_ops.verify_backup` **完全一致**（同一份实现，
    不另写一套）：missing = 源里有、目标里没有的文件；size_diff = 两边都有但大小不同。
    两边都跳过 junction，所以"链接没复制"不会被误判成缺失。

    为什么纳入也要核对（2026-10-31 反馈「备份完成是否需要进行相关的核对」）：
    纳入是把用户原目录复制进新槽。复制不完整时，新槽看起来一切正常、只少了几个
    会话或凭据；用户以为搬完了，把旧目录删掉，数据就真没了。
    """
    import dsh_uninstall_ops as _U
    return _U.verify_backup(src, dst)


def _make_junction(link, target):
    """建一个目录 junction（Windows 上不需要管理员权限）。

    路径必须先 normpath 成反斜杠：`mklink` 是 cmd 内建命令，给它正斜杠路径
    会直接报「语法不正确」（实测：target 写成 `C:/x/y` 时 rc=1，链接建不出来，
    插件就静默失效）。"""
    if not os.path.isdir(target):
        return False
    link = os.path.normpath(link)
    target = os.path.normpath(target)
    try:
        os.makedirs(os.path.dirname(link), exist_ok=True)
    except OSError:
        return False
    if os.path.exists(link) or _is_reparse(link):
        return True               # 已经有了，别重复建
    try:
        import subprocess
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                           capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except Exception:
        return False


def _norm_link_target(t):
    """把 os.readlink 读到的目标规范化成普通绝对路径。

    Windows 上 junction 的目标常带 `\\?\\` 前缀（如 `\\\\?\\C:\\x\\y`），
    直接拿它跟普通路径比前缀永远匹配不上 —— 插件链接就改指不到新槽。"""
    s = (t or "").replace("\\", "/")
    for pre in ("//?/UNC/", "//?/"):
        if s.startswith(pre):
            s = s[len(pre):]
            break
    return s


def relink_home(home, links, old_root="", new_root=""):
    """把用户数据目录里的 junction 按原样重建（复制时被跳过的那些）。

    三种目标分别处理：
      · 指向**旧槽**内部（`<old_root>/node_modules/...`）→ 改指新槽同名路径。
        插件本来就是"链接到槽里的 node_modules"，程序被复制到新槽了，链接也得跟着搬，
        否则纳入后插件全部失效（用户看到的是一堆"插件加载失败"）。
      · 指向**槽外**（用户自己的插件仓库 / 全局 npm）→ 原样指回去（路径没变，仍然有效）。
      · 目标已经不存在 → 不建，记进 missing 里由调用方提示。

    返回 (ok_list, missing_list)。"""
    ok, missing = [], []
    old = _norm_link_target(old_root).rstrip("/").lower()
    new = _norm_link_target(new_root).rstrip("/")
    for rel, target in links or []:
        dest = os.path.join(home, rel)
        tgt = target or ""
        # 改指新槽：只对"原来指向旧槽内部"的链接做替换
        if old and new and tgt:
            tn = _norm_link_target(tgt)
            if tn.lower() == old or tn.lower().startswith(old + "/"):
                tgt = new + tn[len(old):]
        if not tgt or not os.path.isdir(tgt):
            missing.append((rel, tgt))
            continue
        if _make_junction(dest, tgt):
            ok.append(rel)
        else:
            missing.append((rel, tgt))
    return ok, missing


def backup_dsh_home(dsh_home, backup_root, stamp=None):
    """纳入管理前备份数据目录（会话/配置/凭据）。返回 (路径, 说明) 或 (None, 原因)。

    为什么必须备份：用户的会话、设置、凭据都在 DSH_HOME 里。
    纳入管理意味着要改它的位置/归属，备份是唯一的后悔药。
    只备份、不搬移 —— 搬移涉及跨目录重写路径，风险更高；让用户确认后再动。

    2026-10-30：改用 `copy_tree_nolinks`（不跟随 junction）。原先的
    `shutil.copytree` 会把 `.dsh-home/profiles/*/node_modules` 里的几百个 junction
    跟进去复制，487 MB 变成 942 MB / 6 万文件 / 14 分钟。

    2026-10-31：复制完**当场核对**（`verify_copy`），缺文件就把目录改名
    `-INCOMPLETE` 并返回 None —— 调用方据此中止纳入。以前这里只返回一句
    "备份失败" 就算完，而"半份备份"名字正常，用户会以为数据已经安全了。"""
    if not dsh_home or not os.path.isdir(dsh_home):
        return (None, "数据目录不存在，跳过备份：%s" % dsh_home)
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    fails = []
    try:
        os.makedirs(backup_root, exist_ok=True)
        dst = os.path.join(backup_root, "dsh-home-%s" % stamp)
        copy_tree_nolinks(dsh_home, dst, fails)
    except Exception as e:
        return (None, "备份失败（已中止纳入，请手动复制后再试）：%s" % e)
    missing, _changed, total = verify_copy(dsh_home, dst)
    if missing or fails:
        bad = dst + "-INCOMPLETE"
        try:
            os.rename(dst, bad)
        except Exception:
            # 改名失败就退回原名，但必须留下标记文件 —— 名字正常的"半份备份"
            # 比没有更危险（用户会以为数据已经安全了）。见 mark_incomplete 的说明。
            bad = dst
        try:
            import dsh_uninstall_ops as _U
            _U.mark_incomplete(bad, "纳入前的用户数据备份", total, missing, fails)
        except Exception:
            pass
        return (None,
                "备份不完整，已中止纳入（原数据一个字节都没动）：\n"
                "  · 应备份 %d 个文件，缺 %d 个%s\n"
                "  · 拷不动 %d 个%s\n"
                "常见原因：那个 dsh 还在运行（锁住了 .dsh-home 里的文件）、"
                "或杀软正在扫描。请先退出它再重试。\n"
                "已拷出的部分留在：%s"
                % (total, len(missing),
                   ("（例：%s）" % missing[0]) if missing else "",
                   len(fails),
                   ("（例：%s —— %s）" % fails[0]) if fails else "",
                   bad))
    return (dst, "")