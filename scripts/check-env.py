# -*- coding: utf-8 -*-
"""环境自检：确认这台机器能不能跑 dsh 版本管理器。

给发布后的使用者：双击运行，或
    python scripts\\check-env.py

退出码 0 = 全部满足；非 0 = 有硬性缺项。警告项不影响退出码。
"""
import os
import sys
import subprocess
import platform

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# ── 输出语言（2026-11-14）────────────────────────────────────────────
# 这个脚本是**独立进程**，管理器的 i18n 管不到它，所以单独接一套：
#   命令行 --lang en / --lang=en
#   或环境变量 DSH_LANG=en（管理器就是这样调的）
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from dsh_i18n import T, set_lang   # noqa: E402

_lang = os.environ.get("DSH_LANG") or "zh"
for _i, _a in enumerate(sys.argv):
    if _a == "--lang" and _i + 1 < len(sys.argv):
        _lang = sys.argv[_i + 1]
    elif _a.startswith("--lang="):
        _lang = _a.split("=", 1)[1]
set_lang(_lang)

MIN_PY = (3, 8)          # 见 README「运行环境」：代码未用 3.9/3.10 专有语法
# node 版本要求分两档，别混：
#   MIN_NODE = 老版本 dsh 的绝对红线（官方 docs 写的 >= 22.19，低于它会缺
#              node:zlib.createZstdDecompress / AbortSignal.timeout，直接报错）
#   REC_NODE = 新版本 dsh 的 engines 要求（0.2.2 起 package.json 里写 >=24.9.0）。
#              22.19 ~ 24.9 之间跑老版本没事，装 0.2.2 会在运行时崩得很莫名其妙。
# 2026-10-31 起管理器包内自带 runtime\node（v24.21.0，两档都满足），
# 所以下面优先查包内那份 —— 它才是真正会被拷进新槽的 node。
MIN_NODE = (22, 19)
REC_NODE = (24, 9)

HARD, SOFT = [], []


def ok(m):
    print("  [OK]   " + m)


def warn(m):
    SOFT.append(m)
    print(T("  [注意] ") + m)


def bad(m):
    HARD.append(m)
    print(T("  [缺失] ") + m)


def check_python():
    print("== 1. Python ==")
    v = sys.version_info
    got = "%d.%d.%d" % v[:3]
    if v[:2] >= MIN_PY:
        ok(T("Python %s（需要 >= %d.%d）") % (got, MIN_PY[0], MIN_PY[1]))
    else:
        bad(T("Python %s 太低，需要 >= %d.%d") % (got, MIN_PY[0], MIN_PY[1]))
    print(T("         解释器: %s") % sys.executable)


def check_tkinter():
    print(T("== 2. tkinter（图形界面，必需）=="))
    try:
        import tkinter
        ok(T("tkinter 可用（Tcl/Tk %s）") % tkinter.TkVersion)
    except ImportError as e:
        bad(T("缺少 tkinter：%s") % e)
        print(T("         Windows：重装 python.org 官方版（勾选 tcl/tk），"
              "或直接用 runtime\\python\\pythonw.exe 启动管理器"))
        print("         Debian/Ubuntu：sudo apt install python3-tk")


def check_stdlib():
    print(T("== 3. 标准库（全部必需）=="))
    mods = ["json", "os", "sys", "subprocess", "socket", "shutil", "threading",
            "queue", "re", "csv", "io", "time", "ctypes", "struct", "base64",
            "pathlib", "glob", "argparse", "tempfile", "ast"]
    missing = []
    for m in mods:
        try:
            __import__(m)
        except ImportError:
            missing.append(m)
    if missing:
        bad(T("缺少标准库模块：%s（Python 安装可能不完整）") % ", ".join(missing))
    else:
        ok(T("%d 个标准库模块齐备") % len(mods))


def check_optional():
    print(T("== 4. 可选依赖 =="))
    try:
        import winreg
        ok(T("winreg 可用（注册表读快捷方式）"))
    except ImportError:
        warn(T("缺 winreg（仅影响读取已有 .lnk，不影响主流程）"))
    try:
        import pythoncom  # noqa: F401
        import win32com.client  # noqa: F401
        ok(T("pywin32 可用（建桌面快捷方式走 COM 方式）"))
    except ImportError:
        # 2026-11-02 用户反馈：「日志里不要有未装 pywin32，用户还以为软件有问题」。
        # 评估结论：pywin32 **只影响桌面快捷方式的生成方式**，且已有降级路径
        # （build_lnk.py 手工构造 .lnk，效果等价）。它既不必要、也不该报警。
        # 改为 [OK]：说明"不需要它"，而不是"缺了它"。
        ok(T("桌面快捷方式：用内置方式生成（不需要 pywin32）"))


def _node_version(exe):
    """跑一下 node --version，返回 "v24.21.0" 或 None。"""
    try:
        # 必须显式指定 encoding/errors：中文 Windows 上 node 的输出是 GBK，
        # 而 text=True 不给 encoding 会按 locale 严格解码，失败后
        # subprocess 把异常吞在内部、stdout 变None——症状与真因完全无关
        #（见 check-all.ps1 2/6 的说明）。这里只要版本号，replace 足够。
        r = subprocess.run([exe, "--version"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0:
            return (r.stdout or "").strip()
    except Exception:
        pass
    return None


def _parse_ver(v):
    try:
        return tuple(int(x) for x in v.lstrip("v").split(".")[:2])
    except Exception:
        return None


def check_node():
    print(T("== 5. Node.js（安装/运行 dsh 需要）=="))
    # ① 包内自带的 node —— 这才是装 dsh 时真正会用到的（拷进新槽，ABI 与槽内
    #    node-pty 匹配）。有它就等于「不需要用户装 node」。
    bundled = os.path.join(ROOT, "runtime", "node",
                           "node.exe" if os.name == "nt" else "node")
    has_bundled = os.path.isfile(bundled)
    if has_bundled:
        v = _node_version(bundled)
        if v:
            pv = _parse_ver(v)
            if pv and pv >= REC_NODE:
                ok(T("包内自带 Node %s（装 dsh 用它，>= %d.%d 满足所有版本）")
                   % (v, REC_NODE[0], REC_NODE[1]))
            elif pv and pv >= MIN_NODE:
                warn(T("包内 Node %s 只满足老版本 dsh；0.2.2 及以后要求 >= %d.%d")
                     % (v, REC_NODE[0], REC_NODE[1]))
            else:
                bad(T("包内 Node %s 太低（绝对红线 >= %d.%d）") % (v, MIN_NODE[0], MIN_NODE[1]))
            print("         %s" % bundled)
        else:
            warn(T("包内 node 存在但跑不起来：%s（可能被杀软隔离或拷贝不全）") % bundled)
    else:
        print(T("         没有 runtime\\node —— 会退回系统 PATH 里的 node"
              "（从 git 检出时正常，runtime\\ 不入库）"))

    # ② 系统 PATH 里的 node —— 只在「用户自己动手跑 dsh / 改配置去用系统 node」时
    #    才相关，所以包内那份健康时它只是信息项。
    node = None
    for cand in ("node", "node.exe"):
        node = _node_version(cand)
        if node:
            break
    if not node:
        if has_bundled:
            print(T("         PATH 里没有 node —— 不影响使用（装 dsh 走包内那份）"))
        else:
            bad(T("包内没有 node，PATH 里也没有 —— 装不了 dsh（安装走 npm）"))
            print(T("         装 Node.js >= %d.%d：https://nodejs.org/"
                  "，或把一份 node 放进 runtime\\node") % REC_NODE)
        return
    print(T("         PATH 里的 node: %s") % node)
    pv = _parse_ver(node)
    if pv is None:
        warn(T("无法解析 PATH 里的 node 版本号：%s") % node)
    elif pv >= REC_NODE:
        ok(T("PATH 里的 Node %s 也能满足所有版本") % node)
    elif pv >= MIN_NODE:
        warn(T("PATH 里的 Node %s 只够跑老版本 dsh；0.2.2 及以后要 >= %d.%d"
             "（用管理器装槽不受影响，它用包内那份）")
             % (node, REC_NODE[0], REC_NODE[1]))
    else:
        warn(T("PATH 里的 Node %s 低于红线 %d.%d —— 自己手动跑 dsh 会报"
             " zstd / AbortSignal 错（用管理器装槽不受影响）")
             % (node, MIN_NODE[0], MIN_NODE[1]))


def check_files():
    print(T("== 6. 管理器文件完整性 =="))
    need = ["dsh_manager.pyw", "dsh_kill.py", "dsh_adopt.py",
            "dsh_install_ops.py", "dsh_uninstall_ops.py"]
    miss = [f for f in need if not os.path.isfile(os.path.join(ROOT, f))]
    if miss:
        bad(T("缺少文件：%s") % ", ".join(miss))
    else:
        ok(T("%d 个核心文件齐备") % len(need))
    if os.path.isfile(os.path.join(ROOT, "config.json")):
        ok(T("config.json 已生成"))
    else:
        warn(T("config.json 不存在（首次启动会自动从 "
             "config/config.example.json 生成）"))


def check_min_version_claim():
    """校验「最低 Python 3.8」这个承诺仍然成立。

    为什么需要：README 明确写了 3.8+，但代码若日后用上 3.9/3.10 的专有语法
    （match 语句、X | Y 注解、removeprefix、dirs()），文档就成了谎言，
    而 3.8 用户报的是「语法错误 SyntaxError」——极难自己联想到版本问题。
    这里在提交前把它查出来。
    """
    print(T("== 7. 最低版本承诺（README 声明 >= %d.%d）==") % MIN_PY)
    import ast
    import re as _re
    # 正则形式：一旦出现就锁死最低版本
    PATTERNS = [
        (r"\bmatch\s+[\w\.\[\(]+\s*:", "3.10", T("match 语句")),
        (r"\bremoveprefix\(|\bremovesuffix\(", "3.9", "str.removeprefix/suffix"),
        (r"\bfunctools\s*\.\s*cache\b", "3.9", "functools.cache"),
        (r"\bgraphlib\b|\bzoneinfo\b", "3.9", "graphlib / zoneinfo"),
        (r":\s*[\w\.]+\s*\|\s*(None|[\w\.]+)\s*[,)=]", "3.10", T("X | Y 类型注解")),
        (r"\bdirs\(\s*\)", "3.10", T("dirs() 无参调用")),
    ]
    files = ["dsh_manager.pyw", "dsh_kill.py", "dsh_adopt.py",
             "dsh_install_ops.py", "build_lnk.py",
             "scripts/fix-bat-encoding.py", "scripts/py-syntax-check.py",
             "scripts/fix-bom.py"]
    problems = []
    for rel in files:
        p = os.path.join(ROOT, rel)
        if not os.path.isfile(p):
            continue
        src = open(p, encoding="utf-8").read()
        for pat, ver, desc in PATTERNS:
            if _re.search(pat, src):
                problems.append(T("%s 用了 %s（需 %s+）") % (rel, desc, ver))
        # AST 确认 match 语句（正则可能漏）
        try:
            for n in ast.walk(ast.parse(src)):
                if type(n).__name__ == "Match":
                    problems.append(T("%s 第 %d 行有 match 语句（需 3.10+）") % (rel, n.lineno))
        except SyntaxError as e:
            problems.append(T("%s 语法错误：%s") % (rel, e))
    if problems:
        for pr in problems:
            bad(pr)
        print(T("         要么改回 %d.%d 兼容写法，要么把 README 与本文件 MIN_PY 一起上调。")
              % MIN_PY)
    else:
        ok(T("未使用 %d.%d 以上才有的专有语法，「最低 %d.%d」的说法成立")
           % (MIN_PY[0] + 1, MIN_PY[1], MIN_PY[0], MIN_PY[1]))


def check_scope():
    """校验本机已装的 dsh 是否落在管理器声明的适用范围内。

    为什么要有：README 承诺「0.1.2-rc.1 及以后」。但 dsh 是外部项目，
    它若改了包结构（入口不再叫 lib/bin.js、或 web 不再收--port），
    管理器会静默失效——启动才报"找不到文件"，用户完全无从判断根因。
    这里把假设变成可执行的检查。
    """
    print(T("== 8. 已装 dsh 是否在适用范围内 =="))
    import glob as _glob
    import json as _json
    vroot = os.path.join(os.path.expanduser("~"), "dsh-versions")
    roots = sorted([d for d in _glob.glob(os.path.join(vroot, "dsh*"))
                     if os.path.isdir(d)])
    if not roots:
        warn(T("没找到 %s\\dsh*槽目录（若你还没装dsh 可忽略）") % vroot)
        return
    problems = []
    for root in roots:
        dsh = os.path.join(root, "node_modules", "@deepseek-ai", "dsh")
        if not os.path.isdir(dsh):
            continue
        try:
            ver = _json.load(open(os.path.join(dsh, "package.json"),
                                  encoding="utf-8-sig")).get("version", "?")
        except Exception:
            ver = "?"
        binjs = os.path.join(dsh, "lib", "bin.js")
        # 假设 A：入口恒为 lib/bin.js
        if not os.path.isfile(binjs):
            problems.append(T("%s (dsh %s)：找不到 lib/bin.js —— "
                            "该版本包结构与管理器假设不符")
                            % (os.path.basename(root), ver))
            continue
        # 假设 B：bin 字段指向它
        try:
            b = _json.load(open(os.path.join(dsh, "package.json"),
                                encoding="utf-8-sig")).get("bin", {})
            decl = b.get("dsh") if isinstance(b, dict) else None
            if decl and decl.replace("\\", "/") != "lib/bin.js":
                problems.append(T("%s (dsh %s)：package.bin.dsh=%r，与管理器假设的 "
                                "lib/bin.js 不符") % (os.path.basename(root), ver, decl))
        except Exception:
            pass
        # 最低版本
        try:
            nums = [int(x) for x in ver.split("-")[0].split(".")[:3]]
            if tuple(nums) < (0, 1, 2):
                problems.append(T("%s：dsh %s 低于声明的最低版本 0.1.2-rc.1")
                                % (os.path.basename(root), ver))
        except Exception:
            pass
        print(T("         %-10s dsh %-14s 入口 OK") % (os.path.basename(root), ver))
    for pr in problems:
        bad(pr)
    if not problems:
        ok(T("已装版本均符合声明的适用范围"))


def check_privilege():
    print(T("== 9. 权限（非管理员会缺哪些能力）=="))
    try:
        import ctypes
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception as e:
        warn(T("无法检测权限（%s）") % e)
        return
    if is_admin:
        ok(T("以管理员身份运行（非必需 —— 本管理器普通权限即可完成全部功能）"))
        return

    # 实测结论：非管理员下 tasklist / taskkill / Get-CimInstance 可能「拒绝访问」。
    # 但 dsh 是管理器自己起的子进程，结束它**不需要提权**；这些限制只影响
    # 「端口被占用时说不说得清是谁」这一条边缘诊断。这里逐项实测，而不是照念结论。
    print(T("         当前**非管理员**运行（这是推荐状态）。逐项实测边缘能力："))
    import subprocess as _sp
    _cn = getattr(_sp, "CREATE_NO_WINDOW", 0)

    def _run(cmd):
        try:
            return _sp.run(cmd, capture_output=True, encoding="utf-8",
                           errors="replace", timeout=25, creationflags=_cn)
        except Exception:
            return None

    r = _run(["tasklist", "/FI", "PID eq 4", "/FO", "CSV", "/NH"])
    if r is not None and r.returncode == 0 and (r.stdout or "").strip():
        ok(T("tasklist 可用（能读进程名）"))
    else:
        warn(T("受限：读进程名/命令行 —— 端口占用者可能显示不出是谁"))

    r2 = _run(["taskkill", "/PID", "999999", "/F"])
    txt = ""
    if r2 is not None:
        txt = ((r2.stdout or "") + (r2.stderr or "")).lower()
    if T("拒绝") in txt or "denied" in txt or "access" in txt:
        warn(T("受限：结束进程 —— 「停止」按钮可能无效"))
    elif r2 is None:
        warn(T("受限：无法验证结束进程能力"))

    print(T("         → 这些限制只影响「端口被占用时说不说得清是谁」，"))
    print(T("           不影响 启动 / 停止 / 打开浏览器 / 换版本 / 看状态 —— "
          "本管理器**不需要管理员权限**。"))
    print(T("         → 例外：若「停止」真的没反应，多半是安全软件拦截，"
          "而不是权限不足。"))


def check_ports():
    print(T("== 10. 默认端口占用情况（仅供参考）=="))
    import socket
    base, step = 4080, 1000
    ports = [base + step * i for i in range(5)]      # 4080 / 5080 / 6080 / 7080 / 8080
    busy = []
    for p in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p))
        except OSError:
            busy.append(p)
        finally:
            s.close()
    if busy:
        warn(T("这些端口已被占用：%s") % ", ".join(str(x) for x in busy))
        print(T("         不影响使用：新增槽位时会自动避让到相邻空闲端口"))
    else:
        ok(T("默认端口 %s 均空闲") % " / ".join(str(p) for p in ports))


print("=" * 64)
print(T(" dsh 版本管理器 · 环境自检"))
print("=" * 64)
check_python()
check_tkinter()
check_stdlib()
check_optional()
check_node()
check_files()
check_min_version_claim()
check_scope()
check_privilege()
check_ports()
print()
print("-" * 64)
if HARD:
    print(T("结论：有 %d 项硬性缺失，管理器无法正常工作：") % len(HARD))
    for h in HARD:
        print("  · " + h)
    sys.exit(1)
if SOFT:
    print(T("结论：可以运行。有 %d 项提示（不影响启动）：") % len(SOFT))
    for s_ in SOFT:
        print("  · " + s_)
else:
    print(T("结论：环境完全满足要求。"))
sys.exit(0)