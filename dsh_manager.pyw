# dsh_manager.pyw — 配置驱动的多版本 DeepSeek Harness 管理器
#
# 设计要点：
# - 所有版本号 / 安装路径 / node 路径 / 端口 / 浏览器都在 config.json 里，不写死。
# - 支持 <base> / <home> / <npm_global> 三种占位符，换机器只改 config.json。
# - 两个版本槽各自独立进程 / DSH_HOME / 端口，互不污染。
# - 日志写入 dsh-manager.log（含 dsh 自身 stdout/stderr）便于排查。
# - 窗口可缩放；启动前端口预检；自动清理 v2 的 .credentials.yaml.lock 死锁。
# - 进程清理委托同目录 dsh_kill.py：签名白名单 + 拒绝误杀守卫 + 三级回退。
#
# 用法：复制 config/config.example.json -> config.json，改路径后 `py dsh_manager.pyw`
#
# ── i18n 必须先于 tkinter 检查 ────────────────────────────────────────
# 下面「tkinter 缺失」的提示语本身也要翻译（用户可能只看得懂英文），
# 所以 T() 在这里就得可用。放在普通 import 区（87 行）会让 70 行报
# `NameError: name 'T' is not defined` —— 2026-11-14 被 test-tk-selfheal 抓到。
import argparse
import http.client
import os
import queue
import runpy
import signal
import sys

import dsh_platform as PLATFORM

if __name__ == "__main__" and len(sys.argv) > 1:
    command = sys.argv[1]
    if command == "--run-slot-supervisor":
        from dsh_slot_process import main
        raise SystemExit(main(sys.argv[2:]))
    if command == "--kill-slot":
        from dsh_kill import main
        raise SystemExit(main(sys.argv[2:]))
    entry = {"--cli": "dsh-tm.py", "--check-env": "scripts/check-env.py"}.get(command)
    if entry:
        script = str(PLATFORM.RESOURCE_DIR / entry)
        sys.argv = [script] + sys.argv[2:]
        runpy.run_path(script, run_name="__main__")
        raise SystemExit(0)

import dsh_i18n as I18N
from dsh_i18n import T

# ── tkinter 必须能import：这是本程序唯一的硬性第三方前提 ──────────────
# 为什么放在最前面显式检查：tkinter 是 CPython 自带模块，但**并非所有发行版都带**
# —— Debian/Ubuntu 需另装 python3-tk，部分精简版 Python、某些 Store 版也缺。
# 而 .pyw 用 pythonw 跑没有控制台，缺 tkinter 时 ImportError 会被彻底吞掉，
# 表现为「双击没反应」，用户根本无从判断是 Python 版本不对还是别的坏了。
# 所以这里立刻检查 + 用 MessageBox 明确告知（tkinter 缺失时 messagebox 不可用，
# 只能退回 ctypes 的 MessageBoxW）。
try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
except ImportError as _e:
    import sys as _sys

    def _tk_selfheal():
        """被「没有 tkinter 的 Python」打开时，改用包内自带的解释器重开自己。

        为什么需要（2026-10-31 实测踩到）：这台机器上有**三套** python ——
        包内 `runtime\\python`（3.10.21，带 tkinter 8.6）、商店版
        `WindowsApps\\python.exe`、以及别的工具链自带的 3.13（**没有 tkinter**）。
        只要 `dsh_manager.pyw` 被后两者打开（双击 .pyw / 命令行 `python xxx.pyw` /
        自动化脚本 exec 它），就会走到这里，弹出「请重装 Python 并勾选 tcl/tk」——
        而**包内明明有能用的解释器**，这句提示纯属误导，用户会去卸载重装 Python。

        三道闸，缺一不可：
          1. 环境变量 `DSH_MANAGER_TK_RELAUNCH=1` 已存在 → 不再重启（防死循环）；
          2. 包内解释器必须存在；
          3. 包内解释器**不能就是当前这个** —— 否则说明包内那份也缺 tkinter，
             重启只会无限套娃，这时才该弹真正的错误框。

        返回 True 表示已经拉起了新进程，调用方应当立刻退出。
        """
        import os as _os
        import subprocess as _sp
        if _os.environ.get("DSH_MANAGER_TK_RELAUNCH") == "1":
            return False
        try:
            _here = _os.path.dirname(_os.path.abspath(__file__))
        except NameError:
            return False
        _me = _os.path.normcase(_os.path.abspath(_sys.executable))
        for _name in ("pythonw.exe", "python.exe"):
            _cand = _os.path.join(_here, "runtime", "python", _name)
            if not _os.path.isfile(_cand):
                continue
            if _os.path.normcase(_os.path.abspath(_cand)) == _me:
                continue        # 就是它自己 → 包内那份也缺 tkinter，别再重启
            _env = dict(_os.environ)
            _env["DSH_MANAGER_TK_RELAUNCH"] = "1"
            try:
                _sp.Popen([_cand, _os.path.abspath(__file__)],
                          cwd=_here, env=_env, close_fds=True)
            except Exception:
                continue        # 拉不起来就继续找下一个，最后退回弹框
            return True
        return False

    if _tk_selfheal():
        _sys.exit(0)            # GUI 还没创建，直接退出无副作用

    _msg = (T("dsh 版本管理器需要 Python 的 tkinter 模块，但它不可用。\n\n"
            "原因：%s\n\n"
            "解决办法（任选其一）：\n"
            "  1) 双击桌面快捷方式 / start-manager.bat（它们会用包内自带的解释器）\n"
            "  2) Windows：重新安装 python.org 官方版 Python（勾选 tcl/tk 组件）\n"
            "  3) Debian/Ubuntu：sudo apt install python3-tk\n\n"
            "本程序需要 Python 3.8 或更高版本。") % _e)
    try:
        import ctypes as _ct
        _ct.windll.user32.MessageBoxW(0, _msg, T("dsh 版本管理器"), 0x10)
    except Exception:
        pass
    _sys.stderr.write(_msg + "\n")
    _sys.exit(1)
import subprocess, os, time, socket, json, sys, shutil, threading, re, csv, io
import dsh_slot_process as SLOT_PROCESS

BASE_DIR = str(PLATFORM.RESOURCE_DIR)
CONFIG_PATH = PLATFORM.default_config_path()
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=T("DSH 管理器"))
    parser.add_argument("--config", default=CONFIG_PATH)
    CONFIG_PATH = os.path.abspath(os.path.expanduser(parser.parse_args().config))
EXAMPLE_PATH = os.path.join(BASE_DIR, "config", "config.example.json")
LOG_FILE = PLATFORM.log_path(CONFIG_PATH)
if PLATFORM.IS_MAC:
    import fcntl
    os.makedirs(os.path.dirname(CONFIG_PATH), mode=0o700, exist_ok=True)
    _lock_fd = os.open(os.path.realpath(CONFIG_PATH) + ".manager.lock",
                       os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.stderr.write(T("该配置已由另一管理器窗口打开。") + "\n")
        raise SystemExit(0)
# 管理器自己的入口文件。重启要重开它 —— **不能依赖 __file__**：
# pythonw 下 __file__ 可能是相对路径，destroy() 之后更不安全（2026-11-01 实测
# 「说重启但没重启」的根因之一）。这里在模块加载时就固化成绝对路径。
MANAGER_ENTRY = os.path.abspath(__file__)

# 项目主页（2026-11-01）：界面上「★ 在 GitHub 收藏」按钮指向这里。
# 只改这一处即可换仓库地址。
PROJECT_URL = "https://github.com/arnen7000/dsh-tm"

CREATE_NO_WINDOW = PLATFORM.CREATE_NO_WINDOW   # 隐藏 node/taskkill 子进程的控制台窗口

# 日志脱敏：dsh 启动时会打印带 token 的入口 URL，那是该实例的唯一认证凭据，
# 而 README 鼓励用户把日志发出来排查，所以落盘前必须掩码。
# 内存里的 auth_urls 保留完整 token，"打开"按钮照常可用。
_TOKEN_TEXT_RE = re.compile(r"(token=)([A-Za-z0-9_\-]{8,})")
_TOKEN_BYTES_RE = re.compile(rb"(token=)([A-Za-z0-9_\-]{8,})")

# 槽位数量上限。界面是"每槽占 2 列"的自适应网格（N 槽 → 2N 列），
# 所以放开这个数字不需要改任何布局代码，只要别宽到超出屏幕。
# 演进：3 → 4（2026-10-06）→ 5（2026-10-30）→ **6（2026-11-01，用户要求）**。
# 6 槽时窗口宽度 ≈ 1550px，1080p（1920 宽）仍能完整显示；
# 若用户屏幕更窄，网格会自动换行（_grid_columns 负责），不会挤出屏幕外。
MAX_SLOTS = 6


def _mask_text(s):
    """字符串版 token 脱敏：保留 `token=` 后的前 4 字符，其余等长换成 `*`。

    为什么必须放在 log() 出口统一做，而不是逐个调用点打补丁：
    磁盘日志有两个写入者 —— dsh 子进程（由 _scan_slot 即时掩码）和管理器自己
    的 log()。后者原先**完全没有脱敏**，只能靠下次启动的 _scrub_existing_log()
    补擦；也就是说「管理器自己打印过 URL」的行会明文驻留磁盘直到下次重启
    （实测 dsh_manager.pyw 的「打开浏览器 -> http://...?token=...」就是这种）。
    改成在 log() 出口统一脱敏后，任何新增日志行天然安全，不会再漏。
    保留前 4 字符是为了出问题时还能和内存里的 URL 对上号；等长替换保证
    文件长度不变，_scan_slot 的偏移扫描不受影响。
    完整 URL 始终在内存 auth_urls 里，「复制 URL」按钮不受影响。"""
    def _repl(m):
        tok = m.group(2)
        if len(tok) <= 4:
            return m.group(0)
        return m.group(1) + tok[:4] + "*" * (len(tok) - 4)
    return _TOKEN_TEXT_RE.sub(_repl, s)

sys.path.insert(0, BASE_DIR)
try:
    import dsh_kill   # 同目录安全杀进程内核（签名门控 + 监控 + 多级回退）
    run_capture = dsh_kill.run_capture      # 编码安全的子进程调用（见 dsh_kill.decode_out）
except Exception:
    dsh_kill = None

    def run_capture(cmd, timeout=None, stdin_devnull=False, shell=False):
        """dsh_kill 缺失时的降级实现。
        仍然**不能**用裸 text=True：本机 PYTHONUTF8=1 + 中文 Windows 会让
        netstat 的 CP936 输出解码失败，且 subprocess 会把异常吞掉、stdout 变 None
        （实测：报出 "'NoneType' object has no attribute 'splitlines'"，与真因无关）。
        这里固定 errors="replace"：中文表头会被替换成 U+FFFD，但端口号/状态/PID
        这些 ASCII 列完好，解析照常可用。"""
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL if stdin_devnull else None,
                           creationflags=CREATE_NO_WINDOW,
                           timeout=timeout, shell=shell)
        return r.returncode, (r.stdout or ""), (r.stderr or "")

try:
    # 发现已装版本 / 规划新槽的纯逻辑。刻意独立成模块：主文件有二十多处模块级
    # 副作用（建 GUI、写日志）不可 import，拆出来才能进离线回归测试。
    import dsh_adopt
except Exception:
    dsh_adopt = None

try:
    # 版本列表（npm registry）与槽位卸载的纯逻辑。同样拆成独立模块：
    # 联网/解析、删除护栏都要能被 scripts/test-slot-ops.py 直接断言。
    import dsh_registry as REG
except Exception:
    REG = None

try:
    import dsh_uninstall_ops as UNINST
except Exception:
    UNINST = None

# ── 加载配置 ──────────────────────────────────────────────────────────────
def _fatal(msg):
    root = tk.Tk(); root.withdraw()
    messagebox.showerror(T("DSH 管理器"), msg)
    sys.exit(1)

def _boot_log(msg):
    """引导期日志：配置加载阶段（模块级、早于下面第 ~386 行 log() 定义）专用。

    为什么不能直接用 log()：配置解析失败的自愈分支在**模块级**执行，那时 log()
    还没定义，直接调用会抛 NameError。实测后果比"崩溃"更糟 ——
    config.json 已被换成示例模板、本该弹出的「已用示例重建」警告框永远弹不出来、
    pythonw 又无控制台，用户只看到"管理器打不开"，真实配置已被静默替换。

    这里只依赖本行之上已定义好的 LOG_FILE / _mask_text（第 44 / 59 行），可安全早调用；
    此时 GUI 尚未创建，故不写 _gui_sink（那时它也还只是个 no-op）。"""
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), _mask_text(str(msg)))
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as _fh:
            _fh.write(line + "\n")
    except Exception:
        pass
    return line

try:
    # 必须用 utf-8-sig：PowerShell 的 `Set-Content -Encoding utf8` 会写出 UTF-8 BOM，
    # 而 Python 的纯 utf-8 读取会直接抛 "Unexpected UTF-8 BOM"，把好配置判成坏配置。
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as _f:
        CFG = json.load(_f)
except FileNotFoundError:
    # 新手最常见的情况：只下载了仓库、没建 config.json。
    # 示例用的是 <base>/<home>/<npm_global> 可移植占位符，直接复制即可用，避免一上来就报错。
    if os.path.exists(EXAMPLE_PATH):
        try:
            PLATFORM.bootstrap_config(CONFIG_PATH, EXAMPLE_PATH)
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as _f:
                CFG = json.load(_f)
            print(T("[info] 未找到 config.json，已用 config/config.example.json 自动生成默认配置。"))
        except Exception as e:
            _fatal(T("找不到配置文件：\n%s\n\n且无法从示例自动生成：%s") % (CONFIG_PATH, e))
    else:
        _fatal(T("找不到配置文件：\n%s\n\n且示例 config/config.example.json 也不存在（仓库可能不完整）。") % CONFIG_PATH)
except Exception as e:
    # 解析失败也自愈：备份坏文件、再从示例重建。
    # 否则 config.json 一旦被写成半截，管理器就再也打不开了。
    if PLATFORM.IS_MAC:
        _fatal(T("config.json 解析失败：%s\n%s") % (CONFIG_PATH, e))
    _bad = "%s.bad-%s" % (CONFIG_PATH, time.strftime("%Y%m%d-%H%M%S"))
    try:
        shutil.move(CONFIG_PATH, _bad)
    except Exception:
        _bad = T("(备份失败)")
    if os.path.exists(EXAMPLE_PATH):
        try:
            shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as _f:
                CFG = json.load(_f)
            _msg = (T("config.json 解析失败：%s\n\n"
                    "原文件已备份为：\n%s\n\n"
                    "现在用的是【示例模板】路径（不是你的真实配置）——\n"
                    "请把 config.json 里的版本号/路径/端口改回来，"
                    "或参考上面的备份文件。")
                    % (e, _bad))
            print("[warn] " + _msg.replace("\n\n", " | "))
            _boot_log("[warn] " + _msg.replace("\n\n", " | "))
            # pythonw 没有控制台，print 没人看得见 —— 必须弹框，否则就是静默替换配置
            try:
                root_warn = tk.Tk(); root_warn.withdraw()
                messagebox.showwarning(T("config.json 解析失败（已用示例重建）"), _msg)
                root_warn.destroy()
            except Exception:
                pass
        except Exception as e2:
            _boot_log(T("[fatal] 配置文件解析失败：%s（无法从示例重建：%s，备份在 %s）") % (e, e2, _bad))
            _fatal(T("配置文件解析失败：%s\n且无法从示例重建：%s\n坏文件备份在：%s")
                   % (e, e2, _bad))
    else:
        _boot_log(T("[fatal] 配置文件解析失败：%s（坏文件已备份为 %s，"
                  "且找不到 config/config.example.json，无法自动重建）") % (e, _bad))
        _fatal(T("配置文件解析失败：%s\n坏文件已备份为 %s\n"
               "且找不到 config/config.example.json，无法自动重建。") % (e, _bad))

HOME = os.path.expanduser("~")
# <self> = 本文件（dsh_manager.pyw）所在目录 = 整个管理器/离线包根目录。
# 离线包用它做占位符，就能整目录搬家用（拷到 U 盘、换盘符都不会断链）。
INSTALL_BASE = PLATFORM.config_base(CFG)

# <npm_global> 解析一次（定位全局 dsh 的 lib/bin.js）
def _probe_npm_global():
    """探测 npm 全局包目录，优先返回**真正装着 @deepseek-ai/dsh** 的那个。
    （直接信 npm root -g 有坑：它可能命中被 PATH 污染的其它 node，或压根探测不到。）"""
    if PLATFORM.IS_MAC:
        return PLATFORM.probe_npm_global()
    # 1) 静态默认位置优先 —— 完全不起子进程。
    #    （关键：pythonw 无控制台时，npm.cmd 会派生 node 孙进程继承管道，
    #     即使 timeout 杀掉子进程，读管道仍会永久阻塞 → 管理器卡死在出窗口前。）
    cands = []
    _appdata = os.environ.get("APPDATA")
    if _appdata:
        cands.append(os.path.join(_appdata, "npm", "node_modules"))
    _localapp = os.environ.get("LOCALAPPDATA")
    if _localapp:
        cands.append(os.path.join(_localapp, "Programs", "nodejs", "node_modules"))
    for c in cands:
        try:
            if c and os.path.isdir(os.path.join(c, "@deepseek-ai", "dsh")):
                return c
        except Exception:
            pass
    # 2) 静态位置都没有时，再试 npm（DEVNULL 断掉 stdin，短超时兜底）
    for _cmd in (["npm", "root", "-g"], ["npm.cmd", "root", "-g"]):
        try:
            _rc, _out, _ = run_capture(_cmd, timeout=8, stdin_devnull=True, shell=True)
            if _rc == 0 and _out.strip():
                _p = _out.strip()
                if os.path.isdir(os.path.join(_p, "@deepseek-ai", "dsh")):
                    return _p
                cands.append(_p)
        except Exception:
            pass
    # 兜底：Windows 上 Node.js 安装器的默认全局位置
    _appdata = os.environ.get("APPDATA")
    if _appdata:
        cands.append(os.path.join(_appdata, "npm", "node_modules"))
    _localapp = os.environ.get("LOCALAPPDATA")
    if _localapp:
        cands.append(os.path.join(_localapp, "Programs", "nodejs", "node_modules"))
    # 只优先认真正装着 dsh 的目录
    for c in cands:
        try:
            if c and os.path.isdir(os.path.join(c, "@deepseek-ai", "dsh")):
                return c
        except Exception:
            pass
    return cands[0] if cands else None

NPM_GLOBAL = _probe_npm_global()

def resolve(p):
    """把 <base>/<home>/<npm_global>/<self> 占位符替换成实际路径；None 原样返回。"""
    if p is None or not isinstance(p, str):
        return p
    return PLATFORM.resolve_path(p, INSTALL_BASE, HOME, BASE_DIR, NPM_GLOBAL)

ICON_PATH = resolve(CFG.get("icon", "assets/dsh-icon.ico"))
if ICON_PATH and not os.path.isabs(ICON_PATH):
    # 关键修复：相对路径（如 assets/dsh-icon.ico）依赖启动时的工作目录——
    # 双击桌面 .lnk 时工作目录是快捷方式"起始位置"（很可能是 pythonw.exe 所在目录），
    # 此时 os.path.exists 相对路径返回 False，设图标的代码会【静默跳过】→ 任务栏羽毛。
    # 一律锚定到脚本自身目录，工作目录再也不会影响图标。
    ICON_PATH = os.path.join(BASE_DIR, ICON_PATH)

VERSIONS = CFG.get("versions", {})
# 允许 0 个槽：这是「刚下载、还没装 dsh」的正常起点。
# 2026-10-06 起支持「零配置启动」——打开界面 → 点「安装官方版 dsh」→ 用起来。
# 界面此时显示引导卡而不是空白；装完第一个版本后自动切回正常的槽位视图。
if len(VERSIONS) > MAX_SLOTS:
    # 界面按"每槽两列"自动排布（N 槽 → 2N 列），上限之外的不支持。
    # 上限 3 → 4（2026-10-06）→ 5（2026-10-30，设计要求），都是为了「一键装官方版」
    # 的多次追加安装场景（已有 v1 → 装 v2/v3/v4/v5）。
    _fatal(T("config.json 的 versions 最多支持 %d 个槽（当前 %d 个：%s）。\n"
           "想同时用更多版本，请让管理器只管 %d 个，其余用外部启动器。")
           % (MAX_SLOTS, len(VERSIONS), ", ".join(VERSIONS.keys()), MAX_SLOTS))

SLOT_KEYS = list(VERSIONS.keys())   # 顺序保留：第 0 个在最左，依次向右

# ── 界面显示偏好（2026-10-31 新增，「设置」按钮）────────────────────────
# config.json 顶层多一个可选段：
#   "ui": { "max_visible": 4,
#           "order":   ["v2","v1","v3","v4"],   # 界面从左到右的顺序
#           "visible": ["v2","v1","v3"] }       # 要显示哪些（缺省 = 全显示）
#
# 关键约定：**隐藏 ≠ 卸载**。这里只决定「画哪几张卡片、按什么顺序画」，
# 底层 SLOT_KEYS / VC / 进程 / 端口 / 数据目录永远是全量 —— 隐藏一个槽
# 不会停它的 dsh，也不会让它从「卸载」「杀全部」的名单里消失。
# 这样用户调布局时不可能丢东西，也就不需要为「隐藏」写任何数据迁移。
UI_CFG = CFG.get("ui") if isinstance(CFG.get("ui"), dict) else {}

# 界面语言（2026-11-14）：config 的 ui.lang = zh / en。
# **必须在建任何控件之前设好** —— 模块顶层那些 `text=T("设置")` 是在 import
# 过程中求值的，晚一步设置就会有一批控件停在中文上。
try:
    I18N.set_lang(UI_CFG.get("lang") or "zh")
except Exception:
    pass

# 主题模式（2026-11-01）：config 的 ui.theme = system / light / dark
try:
    _THEME_INIT = str(UI_CFG.get("theme") or "system").lower()
except Exception:
    _THEME_INIT = "system"
if _THEME_INIT not in ("system", "light", "dark"):
    _THEME_INIT = "system"

try:
    import dsh_ui_prefs as UIP
except Exception:
    UIP = None


def _ui_order():
    """界面上槽的左右顺序（纯逻辑在 dsh_ui_prefs.slot_order，那里能真跑测试）。"""
    if UIP is None:
        return list(SLOT_KEYS)
    return UIP.slot_order(UI_CFG, VERSIONS, SLOT_KEYS)


def _ui_max_visible():
    """界面最多同时显示几个槽（1..MAX_SLOTS）。"""
    if UIP is None:
        return MAX_SLOTS
    return UIP.max_visible(UI_CFG, MAX_SLOTS)


def _ui_visible():
    """要显示的槽键列表；None 表示「全显示」（老配置没有这个键）。"""
    if UIP is None:
        return None
    return UIP.visible_keys(UI_CFG, VERSIONS, SLOT_KEYS)


def _display_keys():
    """界面真正渲染的槽：按用户排的顺序，再按「最多显示 N 个」截断。"""
    if UIP is None:
        return list(SLOT_KEYS)[:MAX_SLOTS]
    return UIP.display_keys(UI_CFG, VERSIONS, SLOT_KEYS, MAX_SLOTS)


DISPLAY_KEYS = _display_keys()

# 布局：每个版本槽占 2 列（按钮成对横排），N 个槽 → 2N 列。
# 放开上限后，下面的列数 / 控件落位 / 窗口宽度全部自动跟随，无需再改。
N_SLOTS = len(SLOT_KEYS)
COLS = N_SLOTS * 2
FRESH_START = (N_SLOTS == 0)     # 零槽：首次使用，引导安装

# 解析后的每槽运行参数
def _build_vc(versions):
    """把 config 的 versions 段展开成运行参数（占位符已解析）。

    抽成函数是为了「原地刷新」：安装 / 纳入 / 移出 / 卸载之后要重新读
    config 并重建 VC，不能只改界面（2026-11-12 用户要求不重启进程）。
    """
    out = {}
    for _k, _v in (versions or {}).items():
        out[_k] = {
            "label":     str(_v.get("label", _k)),
            "node_exe":  resolve(_v.get("node_exe")),
            "bin_js":    resolve(_v.get("bin_js")),
            "dsh_home":  resolve(_v.get("dsh_home")),     # None = 用默认 ~/.dsh
            "temp_dir":  resolve(_v.get("temp_dir")),     # None = 继承系统/用户级 TEMP
            "port":      int(_v.get("port", 3080)),
            # 2026-10-31 起默认「系统默认浏览器」：每个人装的浏览器不一样，
            # 替用户挑一个（原来写死 chrome）只会让没装 Chrome 的人一打开就落到
            # 兜底分支。旧 config 里显式写了 chrome/edge 的照旧生效。
            "browser":   str(_v.get("browser", "default")),
            "extra_args": list(_v.get("extra_args", [])),
        }
        # 注意：是否需要入口 token 是 dsh【版本】的属性，与 DSH_HOME 是否隔离无关
        #       （0.1.0-rc.8 的 web 无 token；0.1.5 起引入 process token）。
        #       所以这里不做任何猜测，改由 _scan_slot() 从实际输出里抓取。
    return out


VC = _build_vc(VERSIONS)

def _norm_path(s):
    """路径 / 命令行归一化后再比较：反斜杠统一成正斜杠、转小写、合并重复分隔符。
    Windows 上 `C:\\a/b\\c` 与 `C:/a/b/c` 是同一路径但字符串不等；实测同一份
    dsh 用 config.json 拉起（混合斜杠）与用 bash 拉起（全正斜杠）命令行不同，
    不归一化的话签名会随"谁拉起来的"而失配 —— 表现就是【杀不掉自己的进程】。"""
    s = (s or "").lower().replace("\\", "/")
    while "//" in s:
        s = s.replace("//", "/")
    return s

def _make_sig(cfg):
    """构造该槽的进程签名：node 命令行必须落在【本槽的安装目录】内。

    判据是"安装根"（bin_js 里 /node_modules/ 之前那一段），而不是 bin.js 字面量：
      * 同一份安装里的子进程不是用 bin.js 拉起的（例如 dsh-host-directory-picker-native
        的 lib/worker.cjs，实测出现过），按 bin.js 匹配会漏杀它们；
      * 安装根每个槽唯一，仍然足以排除"另一个 dsh"和别的 app 的 node 进程。
    更早的版本还额外要求命令行出现字面量 --port N，结果是【管理器之外】启动的同版本
    进程（裸敲 dsh web / dshvm 连接器 / 手写启动器）清不掉，已去掉。"""
    n = _norm_path(cfg["bin_js"])
    root = (n.split("/node_modules/")[0] + "/node_modules/") if "/node_modules/" in n else n

    def sig(cmdline):
        return bool(root) and root in _norm_path(cmdline)
    return sig

def _check_port_consistency():
    """启动前校验 `port` 字段与 `extra_args` 里的 --port 是否一致。

    为什么必须查：这两处都能决定监听端口，但【只有 extra_args 真正生效】
    ——_build_cmd() 把它拼进命令行，而 port 字段只用于端口预检和状态显示。
    手改其中一个却不改另一个时，预检会查 4080、实际起在 4081，界面永远显示
    「已停止」，而日志里看不出任何异常——纯静默失效。宁可启动即报错。
    返回不一致的槽位列表（空 = 全一致）。

    2026-11-15 地基审计：判据搬到 `dsh_config_guard.py`，CLI 和新的
    `doctor --json` 用同一份；这里只做一层薄转发，避免「界面拦了、脚本没拦」。
    顺带修掉老实现 `cfg["extra_args"]` / `cfg["port"]` 直接取键，
    手改配置缺字段时会在启动前 KeyError 而非给出可读错误的问题。
    """
    import dsh_config_guard as G
    return G.port_problems(VC)

SIG = {k: _make_sig(cfg) for k, cfg in VC.items()}

# 端口双源校验：port 字段只用于预检/状态显示，extra_args --port 才真正决定监听端口。
# 两者不一致时界面会永远显示「已停止」而日志看不出异常，所以在这里直接挡住。
PORT_MISMATCH = _check_port_consistency()
if PORT_MISMATCH:
    _fatal(T("config.json 里 port 与 extra_args 的 --port 不一致：\n\n%s\n\n"
           "port 只用于端口预检和状态显示，真正决定监听端口的是 extra_args 里的 --port。\n"
           "两者不一致会导致预检查的端口和实际启动的端口不同，界面状态永远不更新。\n"
           "请把两处改成同一个端口（可参考 config/config.example.json）。")
           % "\n".join("  · %s：%s" % (k, m) for k, m in PORT_MISMATCH))

def _sig_any(cmdline):
    """"任何 DSH"的泛签名：dsh 的包路径必然含 @deepseek-ai/dsh（含子包
    dsh-base / dsh-web-app / dsh-host-directory-picker-native 等）。
    只用于"杀全部"的槽外兜底 —— 覆盖别的安装位置（旧 dsh-tmp、dshvm、手写启动器）
    与 bin.js 之外的子进程。实测 4 个 node 进程里只有 dsh 的两个命中，
    其它应用（各类 MCP 服务）的 node 进程不命中。"""
    return "@deepseek-ai/dsh" in _norm_path(cmdline)

SIG_ANY = _sig_any

def _installed_version(cfg):
    """读该槽【真正安装】的 dsh 版本。
    从 bin_js 所在目录逐级向上找最近的 package.json（不再硬编码"往上两级"：
    dsh 若改 bin 布局，旧写法会静默变成"实装 未知"）。"""
    binjs = cfg.get("bin_js")
    if not binjs:
        return None
    d = os.path.dirname(os.path.abspath(binjs))
    fallback = None
    for _ in range(6):
        pkg = os.path.join(d, "package.json")
        if os.path.exists(pkg):
            try:
                with open(pkg, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = None
            if data is not None:
                if str(data.get("name", "")) == "@deepseek-ai/dsh":
                    return str(data.get("version", "")).strip() or None
                if fallback is None:
                    fallback = str(data.get("version", "")).strip() or None
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return fallback

# 各槽实装版本（刷新时重读；GUI 标题用它，避免"显示 label 与实际装的版本不一致"）
INSTALLED = {k: _installed_version(cfg) for k, cfg in VC.items()}

# ── 日志（文件 + GUI 槽）───────────────────────────────────────────────────
try:
    LOG_FH = open(LOG_FILE, "a", encoding="utf-8")
except Exception:
    LOG_FH = None

_gui_sink = lambda line: None
_UI_QUEUE = queue.SimpleQueue()


def _post_ui(callback, *args):
    _UI_QUEUE.put((callback, args))


def _drain_ui_queue():
    # Tcl 9 can accept a timer from a worker without delivering it to the
    # Tk main loop. Only the main thread calls Tk, including its timer API.
    for _ in range(100):
        try:
            callback, args = _UI_QUEUE.get_nowait()
        except queue.Empty:
            break
        try:
            callback(*args)
        except Exception as exc:
            log(T("[界面] 后台操作的结果处理失败：%s") % exc)
        try:
            alive = root.winfo_exists()
        except tk.TclError:
            return
        if not alive:
            return
    root.after(25, _drain_ui_queue)

def log(msg):
    # 出口统一脱敏：管理器的日志可能带上入口 URL（?token=...），
    # 而 README 鼓励贴日志排查 —— 明文 token 等于把该实例的认证凭据交出去。
    # GUI 日志区同样走这里，所以界面上也看不到完整 token（符合设计意图：
    # 需要完整 URL 请用「复制 URL」按钮，它取的是内存 auth_urls）。
    msg = _mask_text(str(msg))
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    if LOG_FH:
        try:
            LOG_FH.write(line + "\n"); LOG_FH.flush()
        except Exception:
            pass
    _gui_sink(line)

if dsh_kill is not None:
    dsh_kill.LOG = log

# 尽早落一行启动日志：用于区分「进程压根没起来」与「起来后在建窗口前就崩了」
log(T("--- 解释器起来了 pid=%d exe=%s cwd=%s（下面开始建窗口）---")
    % (os.getpid(), sys.executable, os.getcwd()))

# ── 商店版 Python 守卫：包身份会劫持任务栏图标 ────────────────────────────
# 症状：从商店版 Python（WindowsApps\PythonSoftwareFoundation...）启动时，
# 任务栏永远显示 Python 图标——实测即使 WM_SETICON/ICON_BIG 设置成功
# （日志里 ICON_BIG=True），图标仍被商店应用的包身份覆盖。
# 根治：检测到商店解释器时，自动用独立 Python 重启自己，本进程随即退出。
# 独立解释器候选（按序取第一个存在的）：2026-09-27 起统一放进管理器目录内
# （盘根不再放东西）。
STANDALONE_PYW_CANDIDATES = [
    os.path.join(BASE_DIR, "runtime", "python", "pythonw.exe"),
]
STANDALONE_PYW = next((p for p in STANDALONE_PYW_CANDIDATES if os.path.exists(p)),
                      STANDALONE_PYW_CANDIDATES[0])

def _relaunch_if_store_python():
    exe = os.path.normcase(os.path.abspath(sys.executable))
    low = exe.lower()
    if ("windowsapps" not in low) and ("pythonsoftwarefoundation" not in low):
        return                      # 已经是独立/正常解释器，什么都不用做
    if os.environ.get("DSH_MANAGER_RELAUNCHED") == "1":
        log(T("!! 仍运行在商店版 Python 上（重启用），任务栏图标可能显示 Python"))
        return
    if not os.path.exists(STANDALONE_PYW):
        log(T("!! 当前是商店版 Python 且独立解释器缺失：%s —— 任务栏图标会被劫持") % STANDALONE_PYW)
        return
    env = dict(os.environ)
    env["DSH_MANAGER_RELAUNCHED"] = "1"
    try:
        subprocess.Popen([STANDALONE_PYW, os.path.abspath(__file__)],
                         cwd=BASE_DIR, env=env,
                         creationflags=CREATE_NO_WINDOW)
        log(T("检测到商店版 Python（任务栏图标会被劫持为 Python 图标），"
            "已自动用独立解释器重启：%s") % STANDALONE_PYW)
        os._exit(0)                 # GUI 尚未创建，直接退出无副作用
    except Exception as e:
        log(T("自动切换独立解释器失败：%s") % e)

_relaunch_if_store_python()

# ── 进程追踪 ──────────────────────────────────────────────────────────────
procs = {k: None for k in SLOT_KEYS}
auth_urls = {k: None for k in SLOT_KEYS}          # 各槽入口 URL（内存里含完整 token）
_scan_pos = {k: None for k in SLOT_KEYS}          # 各槽日志扫描偏移（扫描时预留重叠）

def proc_alive(p):
    return p is not None and p.pid is not None and p.poll() is None

def port_open(port):
    try:
        with socket.socket() as s:
            s.settimeout(0.3)
            s.connect(("127.0.0.1", port)); return True
    except Exception:
        return False

def _pid_alive(pid):
    """精确判断 PID 是否存活。不能用 `str(pid) in tasklist 输出`：pid=123 会命中
    1234、也会命中内存列（1,234 K），于是 _clean_stale_lock 误判"锁被存活进程持有"
    而拒绝清理 —— 恰好废掉那个函数存在的意义。

    查不到（tasklist 非 0 返回，例如"无匹配任务"或没有权限）= 认为不存在。
    这样判"死"是安全的：_clean_stale_lock 还有 _holder_is_this_slot() 兜底，
    命令行核对失败时会保守地保留锁，不会因为这里判死就误删有效锁。"""
    if PLATFORM.IS_MAC:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    try:
        rc, out, _ = run_capture(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"])
    except Exception:
        return False
    if rc != 0:
        return False
    want = str(pid)
    rows = list(csv.reader(io.StringIO(out)))
    if not rows:
        return False                      # tasklist 正常返回且无行 = 确实没有这个 pid
    return any(len(row) > 1 and row[1].strip() == want for row in rows)

def _clean_stale_lock(key):
    """v2 web 启动时会写 .credentials.yaml.lock（含 PID），强杀残留会卡死下次启动。
    只在 PID 已死时删除；.credentials.yaml 本身保留。"""
    home = VC[key]["dsh_home"]
    if not home:
        return
    lock = os.path.join(home, ".credentials.yaml.lock")
    if not os.path.exists(lock):
        return
    pid = None
    try:
        pid = int(open(lock, "r", encoding="utf-8").read().strip())
    except Exception:
        pid = None
    if pid is None:
        stale, why = True, T("内容不是 PID")
    elif not _pid_alive(pid):
        stale, why = True, T("持有者已死")
    elif not _holder_is_this_slot(key, pid):
        stale, why = True, T("pid=%d 存活但不是本槽 dsh（PID 复用）") % pid
    else:
        stale, why = False, ""
    if stale:
        try:
            os.remove(lock)
            log(T("[%s] 清理陈旧的 .credentials.yaml.lock (pid=%s，%s)") % (key, pid, why))
        except Exception as e:
            log(T("[%s] 清理 lock 失败：%s") % (key, e))
    else:
        log(T("[%s] lock 被存活的本槽 dsh 持有 (pid=%s)，不动") % (key, pid))


def _holder_is_this_slot(key, pid):
    # Unknown identity is insufficient evidence to remove an active lock.
    process = SLOT_PROCESS.process_info(pid)
    return process is None or SLOT_PROCESS.process_matches(pid, VC[key].get("bin_js"))

def _build_cmd(key):
    cfg = VC[key]
    return [cfg["node_exe"], cfg["bin_js"]] + cfg["extra_args"]

# ── 生成各版本的启动 / kill bat（2026-09-30 新增）──────────────────────────
# 目的：让「管理器启动」和「双击 bat 启动」两条路的环境完全一致（尤其 TEMP）。
# 设计要点：
#   1) 启动 bat 直接调 node + bin.js，**不经过 node_modules\.bin\dsh.cmd** ——
#      实测 0.1.5-rc.2 的 .bin 目录整个缺失，走 dsh.cmd 会报
#      "The system cannot find the path specified."；直接调 bin.js 三个版本都能跑。
#   2) 必须写 **CRLF** 换行 —— cmd.exe 不认裸 LF，会把 set "X=..." 从中间截断，
#      报 'ome"' is not recognized。ASCII 编码，避免 BOM 干扰 @echo off。
#   3) 已存在的 bat 先备份（带时间戳）再覆盖，可随时回滚。
#   4) 只写 bat 文件，不碰 node_modules，不改环境变量。

BAT_DIRS = {
    # key -> 该版本 bat 要落到哪个目录（= bin_js 往上 4 级的版本根目录）
    #   <root>/node_modules/@deepseek-ai/dsh/lib/bin.js
}

def _version_root(cfg):
    """从 bin_js 反推版本根目录：
    .../0.1.5-rc.2/node_modules/@deepseek-ai/dsh/lib/bin.js -> .../0.1.5-rc.2
    若结构不符（如全局安装），返回 None，调用方退化为临时目录根旁的统一目录。"""
    bj = cfg.get("bin_js") or ""
    if not bj:
        return None
    d = os.path.dirname(bj)                       # .../lib
    d = os.path.dirname(d)                        # .../dsh
    d = os.path.dirname(d)                        # .../@deepseek-ai
    d = os.path.dirname(d)                        # .../node_modules
    if os.path.basename(d).lower() != "node_modules":
        return None
    return os.path.dirname(d)                     # 版本根

def _bat_text_start(key, cfg, root):
    """启动 bat 内容。与旧 bat 保持同构，差异只在 TEMP/PATH/端口/入口方式。
    2026-10-01 起启动前先做端口占用预检：非 dsh 程序占用 → 明确报
    「XX 占用了端口，请处理」并写日志（dsh_kill.precheck_start，exit 3）；
    自家 dsh 在跑 → 提示不重复启动（exit 4）。检测脚本缺失则 fail-open。"""
    node = cfg.get("node_exe") or "node"
    binjs = cfg.get("bin_js") or ""
    home = cfg.get("dsh_home") or ""
    temp = cfg.get("temp_dir") or ""
    port = cfg.get("port")
    label = cfg.get("label") or key
    # 预检用的解释器与 kill bat 同源：优先独立运行时的 python.exe（有控制台）
    py = STANDALONE_PYW if "STANDALONE_PYW" in globals() else ""
    if not py:
        py = os.path.join(BASE_DIR, "runtime", "python", "pythonw.exe")
    _py_exe = os.path.join(os.path.dirname(py), "python.exe")
    if os.path.exists(_py_exe):
        py = _py_exe
    prechk = os.path.join(BASE_DIR, "dsh_kill.py")
    L = []
    L.append("@echo off")
    L.append("setlocal")
    L.append("REM ===== dsh %s 启动脚本（由 DSH 管理器自动生成，可手动修改）=====" % label)
    L.append('set "DSH_HOME=%s"' % home)
    L.append('if not exist "%DSH_HOME%" mkdir "%DSH_HOME%"')
    if temp:
        L.append("")
        L.append("REM --- TEMP 隔离（本版本专属，不影响其它软件）---")
        L.append('set "TEMP=%s"' % temp)
        L.append('set "TMP=%s"' % temp)
        L.append('if not exist "%TEMP%" mkdir "%TEMP%"')
        L.append("REM ---------------------------------------------")
    L.append("")
    L.append("REM --- 端口占用预检：其他程序（非 dsh）占用 %s 时拒绝启动并写日志 ---" % port)
    L.append('set "PRECHK_PY=%s"' % py)
    L.append('set "PRECHK=%s"' % prechk)
    # fail-open，而且**刻意不写 else 分支**（2026-10-31）：
    # ① 原来的 `if A if B (…) else (…)` 里 else 其实绑在**内层** if 上 ——
    #    A 不存在时整块连 else 一起跳过，那句「跳过检测」根本打不出来（实测）。
    # ② 更重要的是：管理器被卸载后 A 恒不存在，于是每次启动都少一行输出才对 ——
    #    用户要的是「删了管理器，dsh 照样自己跑」，不是每次启动都被念一句。
    #    说明放在下面的 REM 里，文件自己会解释。
    L.append('if exist "%PRECHK_PY%" if exist "%PRECHK%" (')
    L.append('  "%%PRECHK_PY%%" "%%PRECHK%%" --port %s --precheck-start %s --entrypoint "%s"' % (port, key, binjs))
    L.append("  if errorlevel 1 goto :eof")
    L.append(")")
    L.append("REM 预检脚本不存在时（例如 dsh-tm 管理器已卸载）静默跳过，直接启动 —— dsh 本身不依赖管理器。")
    L.append("")
    L.append("REM 直接调 node + bin.js：不依赖 node_modules\\.bin（该目录可能缺失）")
    L.append('"%s" "%s" web --no-open --port %s' % (node, binjs, port))
    L.append("endlocal")
    return "\r\n".join(L) + "\r\n"

def _bat_text_kill(key, cfg, root):
    """kill bat 内容：沿用 dsh_kill.py --port 的设计，与旧 bat 同构。"""
    port = cfg.get("port")
    label = cfg.get("label") or key
    py = STANDALONE_PYW if "STANDALONE_PYW" in globals() else ""
    if not py:
        py = os.path.join(BASE_DIR, "runtime", "python", "pythonw.exe")
    # kill bat 用 python.exe（有控制台，能看到"占用者是谁"的输出），
    # 而不是 pythonw.exe（无控制台，输出会被吞掉）
    _py_exe = os.path.join(os.path.dirname(py), "python.exe")
    if os.path.exists(_py_exe):
        py = _py_exe
    kill = os.path.join(BASE_DIR, "dsh_kill.py")
    others = "/".join(str(VC[k].get("port")) for k in SLOT_KEYS if k != key) or "无"
    L = []
    L.append("@echo off")
    L.append("setlocal EnableExtensions")
    L.append("REM ===== dsh %s kill 脚本（由 DSH 管理器自动生成）=====" % label)
    L.append('set "PORT=%s"' % port)
    L.append("REM 只处理 dsh %s 的保留端口 %s。其它版本用各自的 kill 脚本（%s）。" % (label, port, others))
    L.append('set "PY=%s"' % py)
    L.append('set "KILL=%s"' % kill)
    L.append('if not exist "%KILL%" (')
    L.append("  echo 找不到 dsh_kill.py：%KILL%")
    # 不写死任何本机路径（这里曾经写的是仓库的旧名字）：
    # 管理器被卸载/搬走后，这条提示要能自己说清「还能怎么办」。
    # 刻意不写 `netstat | findstr`：管道符在 echo 行里要么被当成重定向、
    # 要么得写成 ^|，而仓库的 bat 编码回归（test-bat-text B1）禁止 REM/echo
    # 行出现 < > | & ^ —— 让用户自己跑 netstat 一样能查。
    L.append("  echo 管理器可能已被卸载或搬走。dsh 本身不受影响，仍然可以照常启动。")
    L.append("  echo 只是想停掉它的话：先运行 netstat -ano，找到本地地址以 :%PORT% 结尾")
    L.append("  echo 的那一行，记下最后一列的数字（PID），再到任务管理器按 PID 结束。")
    L.append("  echo 只结束那个 node.exe，别动别的程序。")
    L.append("  goto :eof")
    L.append(")")
    L.append('echo [kill-%PORT%] 先查询端口 %PORT% 的占用者（不会立即杀，会先告诉你是谁）...')
    L.append('"%%PY%%" "%%KILL%%" --port %%PORT%% --entrypoint "%s"' % cfg.get("bin_js", ""))
    L.append("endlocal")
    return "\r\n".join(L) + "\r\n"

def _bat_text_cli(key, cfg, root):
    """CLI 启动 bat：与 web 同构的环境隔离（DSH_HOME / TEMP / 自带 node），
    直接调 node + bin.js 并透传全部参数；无参数时进入交互式 CLI REPL。
    每个版本各一份、互不影响（隔离）。"""
    node = cfg.get("node_exe") or "node"
    node_dir = os.path.dirname(node) if node and node != "node" else ""
    binjs = cfg.get("bin_js") or ""
    home = cfg.get("dsh_home") or ""
    temp = cfg.get("temp_dir") or ""
    label = cfg.get("label") or key
    L = []
    L.append("@echo off")
    L.append("setlocal")
    L.append("REM ===== dsh %s CLI 启动脚本（由 DSH 管理器自动生成，可手动修改）=====" % label)
    L.append('set "DSH_HOME=%s"' % home)
    L.append('if not exist "%DSH_HOME%" mkdir "%DSH_HOME%"')
    if temp:
        L.append("")
        L.append("REM --- TEMP 隔离（本版本专属，不影响其它软件）---")
        L.append('set "TEMP=%s"' % temp)
        L.append('set "TMP=%s"' % temp)
        L.append('if not exist "%TEMP%" mkdir "%TEMP%"')
        L.append("REM ---------------------------------------------")
    if node_dir:
        L.append("")
        L.append("REM --- 优先用本版本自带 node（隔离，不依赖系统 PATH）---")
        L.append('set "PATH=%s;%%PATH%%"' % node_dir)
    L.append("")
    L.append("REM 直接调 node + bin.js：不依赖 node_modules\\.bin（该目录可能缺失）")
    # 2026-10-15 修正：原写「双击进入交互式 CLI」是错的 —— 实测这几个版本的 dsh
    # 无参数直接 `error: --profile <name> is required` 退出（并没有 REPL）。
    # profile 就在本槽 .dsh-home\\profiles 下（本机是 headless / web）。
    L.append("REM 用法：dsh-cli.bat --profile headless    （可用 profile 见 %DSH_HOME%\\profiles）")
    L.append('"%s" "%s" %%*' % (node, binjs))
    L.append("endlocal")
    return "\r\n".join(L) + "\r\n"

def _write_bat(path, text, stamp):
    """写 bat：CRLF + **GBK**（不是 UTF-8）。已存在则先备份。返回 (是否写入, 备份路径或错误)。

    为什么必须是 GBK（2026-10-15 实测踩坑，用户报「dsh-cli.bat 双击没反应」）：
    cmd.exe 读 .bat 用的是 ANSI 代码页（中文 Windows = 936/GBK），**不认 UTF-8**。
      1) UTF-8 无 BOM：中文注释被按 GBK 解析成乱码；更糟的是两个中文 REM 行相邻时，
         按 GBK 解析出的字节边界会把上一行行尾的 CRLF 吞掉/撕裂 ——
         实测 `REM 用法：... dsh-cli.bat <子命令> <参数>` 被从「双击进入」处撕成两半，
         后半截 `<子命令> <参数>` 被当成【命令】执行，`<` 触发输入重定向
         → cmd 报「命令语法不正确」rc=255 → 双击只看到窗口一闪就没。
      2) UTF-8 带 BOM：更糟，cmd 把 BOM 当前提内容，同样 rc=255（已实测）。
    所以：GBK 是与 cmd.exe 唯一的正确约定（仓库 scripts/check-all.ps1 也一直校验
    「.bat 必须 GBK」，只是它只查仓库内的 bat、漏了这里生成到槽目录的 bat）。
    另外 bat 文本里的注释行也不写 < > | & ^ 等 cmd 元字符（见 _bat_text_cli）。
    """
    bak = None
    if os.path.exists(path):
        bak = "%s.bak-%s" % (path, stamp)
        try:
            shutil.copyfile(path, bak)
        except Exception as e:
            return (False, T("备份失败：%s") % e)
    try:
        # GBK：cmd.exe 的 ANSI 代码页。newline="" 保证我们写进去的 \r\n 不被二次转换。
        with open(path, "w", encoding="gbk", newline="") as f:
            f.write(text)
    except UnicodeEncodeError as e:
        # 理论不可达（GBK 覆盖全部常用中文），但真发生时要能说清楚原因
        return (False, T("GBK 编码失败：%s") % e)
    except Exception as e:
        return (False, T("写入失败：%s") % e)
    return (True, bak)

# 会原样拼进 `set "VAR=..."` / `REM ...` / `"node" "bin.js"` 的字符里，
# 这些会让 cmd.exe 提前闭合引号、展开变量、或把一行拆成两条命令。
# 2026-11-11：路径与 label 来自 config，可能被用户手工改成任意字符串，
# 生成前必须先拦一道 —— 宁可跳过这个槽的 bat，也不能写出可被注入的脚本。
_BAT_UNSAFE_CHARS = frozenset('"\r\n%&^|<>()')


def _bat_unsafe_fields(cfg):
    """返回第一个含 cmd 元字符的字段说明；空串表示可以安全生成 bat。"""
    for name in ("node_exe", "bin_js", "dsh_home", "temp_dir", "label"):
        v = str(cfg.get(name) or "")
        bad = "".join(sorted({c for c in v if c in _BAT_UNSAFE_CHARS}))
        if bad:
            return (T("%s 含 cmd.exe 元字符 %r，已跳过生成 .bat：%r")
                    % (name, bad, v))
    return ""


def _rebuild_bats(keys=None, only_missing=False):
    """重写三件 bat（不弹框）。返回 (made, failed, skipped) 三个列表。

    抽出来是因为「临时目录改了要重建 bat」这种场景不该再弹一次汇总框 ——
    设置对话框自己会报结果。gen_bats() 只是它 + 一个 messagebox。

    keys=None         → 全部槽（默认）。手动生成、改临时目录和移动安装位置
                        都使用这个模式重写所有脚本。
    only_missing=True → **只补缺失的文件**：已存在的 bat 一律跳过，既不备份
                        也不覆盖。槽体检修复和保留原端口的接回使用这个模式。
    """
    if PLATFORM.IS_MAC:
        made, failed, skipped = [], [], []
        for key in (SLOT_KEYS if keys is None else keys):
            cfg = VC.get(key)
            root = _version_root(cfg) if cfg else ""
            if not root:
                skipped.append(key)
                continue
            try:
                made.extend(PLATFORM.write_slot_launchers(root, cfg, only_missing=only_missing))
            except (OSError, ValueError) as exc:
                failed.append("%s: %s" % (key, exc))
        return made, failed, skipped

    stamp = time.strftime("%Y%m%d-%H%M%S")
    made, failed, skipped = [], [], []
    for key in (keys if keys is not None else SLOT_KEYS):
        cfg = VC[key]
        problem = _bat_unsafe_fields(cfg)
        if problem:
            failed.append("%s %s" % (key, problem))
            log(T("[%s] 跳过生成 bat：%s") % (key, problem))
            continue
        root = _version_root(cfg)
        if not root or not os.path.isdir(root):
            skipped.append(T("%s（找不到版本目录：%s）") % (key, root))
            continue
        port = cfg.get("port")
        jobs = [
            (os.path.join(root, "dsh.bat"),        _bat_text_start(key, cfg, root), T("启动")),
            (os.path.join(root, "kill-%s.bat" % port), _bat_text_kill(key, cfg, root), "kill"),
            (os.path.join(root, "dsh-cli.bat"),   _bat_text_cli(key, cfg, root), "CLI"),
        ]
        for path, text, kind in jobs:
            if only_missing and os.path.isfile(path):
                # 只补缺失；已存在（含用户手工改过）的一律不碰。
                continue
            if only_missing and kind == "kill" and not port:
                # 与 _bats_missing 同判据：没配端口时不需要 kill-<port>.bat，
                # 更不能凭空生成一个 kill-None.bat。
                continue
            ok, info = _write_bat(path, text, stamp)
            if ok:
                made.append("%s [%s] %s" % (key, kind, path))
                log(T("[%s] 已生成 %s bat：%s%s")
                    % (key, kind, path, (T("（原文件已备份为 %s）") % os.path.basename(info)) if info else ""))
            else:
                failed.append("%s [%s] %s —— %s" % (key, kind, path, info))
                log(T("[%s] 生成 %s bat 失败：%s") % (key, kind, info))
    return made, failed, skipped


def _launcher_action_label():
    return T("生成启动、停止和 CLI 脚本") if PLATFORM.IS_MAC else T("生成启动/kill/CLI bat")


def gen_bats():
    """为每个版本生成 启动 bat / kill bat / CLI bat。"""
    made, failed, skipped = _rebuild_bats()
    # 结果提示
    msg = []
    if made:
        msg.append(T("已生成 %d 个文件：") % len(made))
        msg.extend("  " + m for m in made)
    if skipped:
        msg.append("")
        msg.append(T("跳过 %d 个：") % len(skipped))
        msg.extend("  " + s for s in skipped)
    if failed:
        msg.append("")
        msg.append(T("失败 %d 个：") % len(failed))
        msg.extend("  " + f for f in failed)
    if not msg:
        msg.append(T("没有可生成的版本。"))
    body = "\n".join(msg)
    log(T("=== 启动脚本生成完成 ===\n") + body)
    try:
        messagebox.showinfo(_launcher_action_label(), body)
    except Exception:
        pass

def start_one(key):
    cfg = VC[key]
    ownership = SLOT_PROCESS.slot_state(cfg.get("bin_js"), cfg["port"])
    if ownership == "conflict":
        messagebox.showwarning(T("端口被占用"),
                               T("无法启动 %s：端口 %d 已被占用。") % (key, cfg["port"]))
        return
    if ownership == "owned" or proc_alive(procs.get(key)):
        return
    try:
        _spawn_slot_silent(key)
    except (OSError, ValueError) as exc:
        _notify_start_failure(key, str(exc), T("请运行槽体检。"))

def _notify_start_failure(key, brief, detail):
    """启动失败时弹框（pythonw 下这是用户唯一的反馈渠道）。
    GUI 不可用时静默跳过——此时日志仍是唯一记录，不能因弹框失败而中断。"""
    log(T("[%s] 启动失败（已弹框告知用户）：%s") % (key, brief))
    try:
        messagebox.showerror(T("启动失败 [%s]") % key, "%s\n\n%s" % (brief, detail))
    except Exception:
        pass

def _kill_pid_tree(pid, tag):
    """降级用（dsh_kill 未加载时）：只杀本 app 跟踪的进程树。"""
    rc, out, _ = run_capture(["taskkill", "/PID", str(pid), "/T", "/F"])
    log("[%s] taskkill pid=%d rc=%d %r" % (tag, pid, rc, out.strip()))

def _pids_on_port(port):
    _, listening = SLOT_PROCESS.slot_listeners(None, port)
    return listening

def _dsh_pid_on_port(key):
    cfg = VC[key]
    ownership, owners = SLOT_PROCESS.observe_slot(cfg.get("bin_js"), cfg["port"])
    return min(owners) if ownership == "owned" else None

def _refresh_detect_cache(key):
    """Refresh the same ownership snapshot used by the background poll."""
    cfg = VC.get(key)
    if cfg is not None:
        observed_at = time.monotonic()
        ownership, owners = SLOT_PROCESS.observe_slot(cfg.get("bin_js"), cfg["port"])
        _apply_detect_payload(key, cfg, observed_at, ownership, owners)


def _apply_detect_payload(key, cfg, observed_at, ownership, owners):
    if VC.get(key) is not cfg or observed_at < (_DETECT.get(key) or {}).get("ts", 0):
        return
    _DETECT[key] = {"listen": ownership != "idle", "ownership": ownership,
                    "owners": owners, "ext": min(owners) if ownership == "owned" else None,
                    "ts": observed_at}


def kill_one(key, report=True):
    cfg = VC[key]
    tracked = procs.get(key)
    count = SLOT_PROCESS.stop_slot(cfg.get("bin_js"), cfg["port"], tracked)
    if not proc_alive(tracked):
        procs[key] = None
        auth_urls[key] = None
    _refresh_detect_cache(key)
    if report:
        _report_port_after_kill(key, cfg["port"], count)
    return count

def _report_port_after_kill(key, port, killed):
    """停止之后复查端口占用并告知用户。

    为什么要复查：清理可能失败（进程没杀掉 / 权限不足 / 端口被别的程序占着）。
    此时若静默返回，用户看到的是「点了停止，状态还是运行中」，只能自己开任务
    管理器猜。所以必须把端口实况弹出来，并说清占用者是谁、是不是 dsh
    （不是 dsh 就绝不代杀，只交回给用户处理）。

    三种结果：
      · 端口空闲      —— 只写日志，不打扰（绝大多数情况）
      · 剩自家 dsh    —— 提示可能需要再点一次停止
      · 剩非 dsh 程序 —— 弹框说清是谁占着，绝不代杀（用户真正需要知道的）
    """
    if dsh_kill is None:
        return
    try:
        owners, text = dsh_kill.format_port_owners(port)
    except Exception as e:
        log(T("[%s] 停止后复查端口失败：%s") % (key, e))
        return
    if not owners and not port_open(port):
        log(T("[%s] 停止完成：端口 %d 已释放%s")
            % (key, port, (T("（清理 %d 个进程）") % killed) if killed else ""))
        return

    owned_pid = _dsh_pid_on_port(key)
    dsh_left = [o for o in owners if o["pid"] == owned_pid]

    if owned_pid:
        log(T("[%s] 停止后端口 %d 仍被自家 dsh 占用：%s")
            % (key, port, ", ".join("pid=%d" % o["pid"] for o in dsh_left)))
        try:
            messagebox.showwarning(
                T("停止未完全成功 [%s]") % key,
                T("已发出停止指令，但端口 %d 仍被 dsh 进程占用：\n\n%s\n\n"
                "通常是进程尚未退出，或存在子进程残留。\n"
                "可再点一次「停止」；反复不行就点「全部停止」，"
                "或用命令行：python dsh-tm.py kill-stray") % (port, text))
        except Exception:
            pass
        return

    # 端口被非 dsh 程序占着：最需要用户知道的情况（多半是别的软件抢了端口）
    log(T("[%s] 停止后端口 %d 被非 dsh 程序占用：\n%s") % (key, port, text))
    try:
        messagebox.showwarning(
            T("端口被其它程序占用 [%s]") % key,
            T("dsh 已停止，但端口 %d 仍被其它程序占用——所以它不会随 dsh 一起消失。\n\n%s\n\n"
            "管理器不会自动结束别人的程序（避免误杀）。\n"
            "请自行决定：关闭那个程序，或在 config.json 里把该槽的 port 改成别的"
            "（记得同步 extra_args 里的 --port）。") % (port, text))
    except Exception:
        pass

def stop_all_slots():
    """「全部停止」：把**本管理器管的**每个槽挨个停一遍。

    标题栏现在「全部启动」「全部停止」都有，两者都只作用于 config 里登记的槽
    （停止时按签名 + 端口门控，不碰槽外任何 dsh）。

    旧版的「杀全部进程」按钮已移除：它连没登记进 config 的 dsh 副本也一起清，
    属于排障动作，现在只走 CLI `python dsh-tm.py kill-stray --yes`。
    批量启动同样可以用 CLI `dsh-tm.py start --all`（脚本 / 计划任务场景）。
    """
    running = []
    for k in SLOT_KEYS:
        try:
            if procs.get(k) is not None and proc_alive(procs.get(k)):
                running.append(k)
                continue
        except Exception:
            pass
        # 只把能确认属于该槽的外部实例列为运行中。
        try:
            if _dsh_pid_on_port(k):
                running.append(k)
        except Exception:
            pass
    if not running:
        messagebox.showinfo(T("全部停止"), T("当前没有任何槽在运行。"))
        return
    if not messagebox.askyesno(
            T("全部停止"),
            T("将停止以下 %d 个槽：\n\n%s\n\n"
            "只停管理器管的这些槽（不碰其它 dsh 安装）。")
            % (len(running), "、".join(VC[k]["label"] for k in running))):
        return
    log(T("=== 全部停止：%s ===") % "、".join(running))
    # 2026-11-14 修两处：
    #  ① 方向：这里原来把「可用」当「忙碌」传反了，停完后所有按钮被禁用且不再恢复；
    #  ② 卡顿：原来在 UI 线程里同步跑 kill_one —— 每个槽都要枚举全部进程
    #    （find_version_pids）+ 停完再识别端口占用者（format_port_owners → tasklist），
    #    4 个槽就把界面冻住好几秒、鼠标一直转圈（用户实测）。批量启动不卡，
    #    因为它只 Popen，不做任何进程枚举。
    #    现在整个停止过程放后台线程，逐个杀、逐个写日志，结束后回主线程汇总。
    _actions_enabled(False)
    results = []          # [(key, killed, still_listening, err)]

    def work():
        for k in running:
            try:
                n = kill_one(k, report=False)     # 批量：不逐个弹窗
                _still = False
                try:
                    _still = port_open(VC[k]["port"])
                except Exception:
                    pass
                results.append((k, n, _still, ""))
            except Exception as e:
                results.append((k, None, False, str(e)))
        try:
            _post_ui(done)
        except Exception:
            pass

    def done():
        _actions_enabled(True)
        try:
            refresh(once=True)
        except Exception:
            pass
        failed = ["%s（%s）" % (k, err) for k, _n, _s, err in results if err]
        still = [T("%s（端口 %d 仍在监听）") % (k, VC[k]["port"])
                 for k, _n, s, err in results if s and not err]
        if failed:
            messagebox.showwarning(T("全部停止"), T("以下槽未能停止：\n\n") + "\n".join(failed))
        if still:
            messagebox.showwarning(
                T("全部停止"),
                T("已发出停止指令，但这些端口还在监听：\n\n  ") + "\n  ".join(still) +
                T("\n\n可能是进程尚未退出，或端口被别的程序占着。"
                "稍等再看状态；反复不行可用 CLI：python dsh-tm.py kill-stray"))
        if not failed and not still:
            log(T("[全部停止] 完成：%d 个槽已停止") % len(running))

    threading.Thread(target=work, daemon=True).start()

def start_all_slots():
    """「全部启动」：把**本管理器管的**每个槽都拉起来（2026-11-01 新增）。

    与 start_one 的区别
    -------------------
    start_one 是"点一张卡片"，里面有一堆交互式弹窗（端口被非 dsh 占用时问用户、
    被自家 dsh 占用时问要不要清理）。批量场景下一个人盯着 6 个弹窗是不可接受的，
    所以这里走**静默策略**：

      · 已在运行 → 跳过
      · 端口被 **非 dsh** 程序占用 → **不启动、不改动任何东西**，记录一条并汇总告诉用户
        （绝不为了"全部起来"去杀别人的程序）
      · 端口被**自家 dsh** 占用 → 视为已在运行，跳过
      · 其它失败 → 记录，继续下一个

    全部发完后等端口真正开始监听再回报（dsh 冷启动 5~8 秒），
    与 CLI 的 `start --all` 行为一致。
    """
    keys = [k for k in SLOT_KEYS]
    if not keys:
        messagebox.showinfo(T("全部启动"), T("当前没有任何槽可启动。"))
        return

    started, skipped, blocked = [], [], []
    port_conflict = False
    for k in keys:
        cfg = VC[k]
        try:
            ownership = SLOT_PROCESS.slot_state(cfg.get("bin_js"), cfg["port"])
            if ownership == "conflict":
                blocked.append(T("%s（端口 %d 已被占用）") % (k, cfg["port"]))
                port_conflict = True
                continue
            if ownership == "owned" or proc_alive(procs.get(k)):
                skipped.append(k)
                continue
            _spawn_slot_silent(k)
            started.append(k)
        except Exception as e:
            blocked.append("%s（%s）" % (k, e))

    if started:
        log(T("=== 全部启动：%s ===") % "、".join(started))
    if skipped:
        log(T("[全部启动] 已在运行，跳过：%s") % "、".join(skipped))
    for _b in blocked:
        log(T("[全部启动] 未能启动：%s") % _b)

    if blocked:
        detail = T("以下槽未能启动：\n\n  ") + "\n  ".join(blocked)
        if port_conflict:
            detail += T("\n\n非 dsh 程序占用的端口不会自动结束，请手动处理或改端口。")
        messagebox.showwarning(
            T("全部启动"), detail, parent=root)
    elif not started:
        messagebox.showinfo(T("全部启动"), T("所有槽都已经在运行了。"))
    else:
        log(T("[全部启动] 已发出 %d 个启动指令，等待端口监听…") % len(started))
    refresh(once=True)


def _spawn_slot_silent(key):
    cfg = VC[key]
    cmd = _build_cmd(key)
    node = PLATFORM.resolve_node(cmd[0])
    if not node:
        raise FileNotFoundError(T("找不到可用的 Node.js：%s") % cmd[0])
    if not os.path.isfile(cmd[1]):
        raise FileNotFoundError(T("找不到 dsh 入口：%s") % cmd[1])
    for field in ("dsh_home", "temp_dir"):
        if cfg.get(field):
            os.makedirs(cfg[field], exist_ok=True)
    _clean_stale_lock(key)
    env = PLATFORM.slot_env(dict(cfg, node_exe=node))
    auth_urls[key] = None
    launcher = [sys.executable, "--run-slot-supervisor"] if PLATFORM.FROZEN else None
    p = SLOT_PROCESS.spawn_slot(node, cmd[1], cfg["port"], env=env,
                               cwd=cfg.get("dsh_home") or BASE_DIR,
                               output_path=LOG_FILE, extra_args=cmd[2:], launcher=launcher)
    procs[key] = p
    log(T("[%s] 已发出启动指令，端口 %d。") % (key, cfg["port"]))


def kill_all():
    """杀全部 = 所有 DSH 相关的 node 进程：
    两个槽各自的签名（槽内，精确到安装目录）+ 泛签名兜底（槽外：别的安装位置、
    不是 bin.js 拉起的子进程）。泛签名只认 @deepseek-ai/dsh，绝不碰其他 node。"""
    if dsh_kill is None:
        log(T("=== 杀全部：dsh_kill 未加载，退化为杀 tracked 进程树 ==="))
        for k in SLOT_KEYS:
            kill_one(k)
        return
    dsh_kill.kill_all({k: {"entrypoint": VC[k]["bin_js"], "port": VC[k]["port"]} for k in SLOT_KEYS},
                      sig_any=SIG_ANY)
    for k in SLOT_KEYS:
        procs[k] = None

def _find_browser(candidates, fallback_cmd):
    for c in candidates:
        c = os.path.expandvars(c)          # 展开 %LOCALAPPDATA% 等
        if os.path.exists(c):
            return ("path", c)
    return ("cmd", fallback_cmd)

CHROME_CANDIDATES = [
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
]
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
FIREFOX_CANDIDATES = [
    r"C:\Program Files\Mozilla Firefox\firefox.exe",
    r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
]

# 每槽 browser 字段 → (显示名, 候选 exe 列表)。
# browser 也支持：写死 exe 绝对路径（用系统任何浏览器）；
# 或写 "default" = 不指定浏览器，直接交给系统默认浏览器打开。
if PLATFORM.IS_MAC:
    def _mac_browser_candidates(name):
        return [os.path.join(root, name + ".app")
                for root in ("/Applications", os.path.expanduser("~/Applications"))]

    CHROME_CANDIDATES = _mac_browser_candidates("Google Chrome")
    EDGE_CANDIDATES = _mac_browser_candidates("Microsoft Edge")
    FIREFOX_CANDIDATES = _mac_browser_candidates("Firefox")

BROWSER_KNOWN = {
    "chrome":  ("Chrome",  CHROME_CANDIDATES),
    "edge":    ("Edge",    EDGE_CANDIDATES),
    "firefox": ("Firefox", FIREFOX_CANDIDATES),
    "default": (T("默认浏览器"), []),
}

# 「设置」里下拉框的顺序：默认浏览器排第一 —— 这是大多数人的选择，
# 也是没写 browser 字段时的行为。
BROWSER_CHOICES = ["default", "chrome", "edge", "firefox"]
# 「设置」下拉里的最后一项：不是值，是个动作（弹文件选择框挑任意 exe）。
_B_OTHER = "选择其他程序…"

def _browser_filetypes():
    if PLATFORM.IS_MAC:
        return [(T("macOS 应用程序"), "*.app"), (T("所有文件"), "*")]
    return [(T("可执行程序"), "*.exe"), (T("所有文件"), "*.*")]

def _browser_label(browser):
    """按钮/标题显示名：chrome→Chrome、edge→Edge、firefox→Firefox、
    绝对路径→该文件名、default/空→浏览器。

    注意 default 返回的是「浏览器」而不是「默认浏览器」：按钮文案是
    「打开 %s」，写全会变成「打开 默认浏览器」六个字，在 240px 的卡片里
    按钮列放不下会被切。"""
    b = (browser or "").strip().lower()
    if b in BROWSER_KNOWN:
        return T("浏览器") if b == "default" else BROWSER_KNOWN[b][0]
    if b:
        return os.path.basename(browser.strip())
    return T("浏览器")


def _browser_installed(browser):
    """这个浏览器在本机装了吗？default 永远算「装好了」（系统总会有一个默认）。

    只用于在「设置」下拉里标注「（未安装）」——选它不会报错，
    _resolve_browser 会兜底到系统默认浏览器。"""
    b = (browser or "").strip().lower()
    if b in ("default", "") or b not in BROWSER_KNOWN:
        return True
    return _find_browser(BROWSER_KNOWN[b][1], "")[0] == "path"

def _mask_span(offset, length):
    """就地等长覆盖敏感片段：不改变文件长度，因此既不影响 dsh 子进程的 append 写入，
    也不影响本模块自己的扫描偏移。（不能改成管道读：dsh 源码里没有任何 EPIPE 处理，
    管理器退出时子进程会因为写坏管道而崩。）"""
    try:
        with open(LOG_FILE, "r+b") as f:
            f.seek(offset)
            f.write(b"*" * length)
            f.flush()
        return True
    except Exception:
        return False


def _scan_slot(key):
    proc = procs.get(key)
    if proc and proc.url:
        auth_urls[key] = proc.url

def _scrub_existing_log():
    """启动时对【已有】日志做一次全量就地掩码。
    补丁1 只掩码 dsh 子进程写的 `dsh web:` 入口行；而管理器自己写过的
    `[v2] 打开浏览器(edge) -> http://...?token=xxx` 属于历史遗留，得靠这一趟清掉。
    仍是等长覆盖、不改文件长度 —— 即使上次的 dsh 实例还在往这个文件里写，也安全。"""
    try:
        if not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) <= 0:
            return 0
        with open(LOG_FILE, "rb") as f:
            blob = f.read()
    except Exception:
        return 0
    n = 0
    for m in _TOKEN_BYTES_RE.finditer(blob):
        tok = m.group(2)
        if len(tok) <= 4:
            continue
        if _mask_span(m.start(2) + 4, len(tok) - 4):
            n += 1
    return n


def _resolve_browser(browser):
    """把 config 的 browser 字段解析成 (kind, exe)：
       kind ∈ path(找到exe) / explicit(用户写死exe路径) / start(交给系统默认浏览器)。
       已知名走候选列表；其它字符串若像 exe 路径则直接用；都不中→系统默认。"""
    b = (browser or "").strip().lower()
    if b in BROWSER_KNOWN and BROWSER_KNOWN[b][1]:
        kind, exe = _find_browser(BROWSER_KNOWN[b][1], "")
        if kind == "path":
            return ("path", exe)
    if b not in BROWSER_KNOWN and browser and os.path.exists(os.path.expandvars(browser)):
        return ("explicit", os.path.expandvars(browser))  # 用户写死 exe 绝对路径
    return ("start", "")                      # 交给系统默认浏览器

def _slot_needs_authentication(port):
    """Probe only the verified loopback listener, without following redirects."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.8)
    try:
        conn.request("GET", "/")
        with conn.getresponse() as response:
            return response.status == 401
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def _slot_browser_url(key):
    cfg = VC[key]
    port = cfg["port"]
    tracked = procs.get(key)
    alive = proc_alive(tracked)
    owned, listeners = SLOT_PROCESS.slot_listeners(cfg.get("bin_js"), port)
    if not owned or owned != listeners:
        if listeners or port_open(port):
            log(T("[%s] 无法确认监听端口属于该槽，未提供入口 URL。") % key)
        elif alive:
            log(T("[%s] 仍在启动：入口 URL 还没生成，状态变 ✓ 后再点") % key)
        else:
            log(T("[%s] 未运行，请先启动") % key)
        return None

    if alive and tracked.child_pid in owned:
        current = SLOT_PROCESS.process_info(tracked.child_pid)
        if current and current.birth == tracked.child.birth:
            if tracked.url:
                auth_urls[key] = tracked.url
                return tracked.url
            log(T("[%s] 仍在启动：入口 URL 还没生成，状态变 ✓ 后再点") % key)
            return None

    if _slot_needs_authentication(port):
        log(T("[%s] 服务正在运行，但当前窗口没有它的认证入口。") % key)
        messagebox.showinfo(
            T("需要认证入口"),
            T("这个槽仍在运行，当前窗口没有它的认证链接。\n\n"
              "可以继续使用原先已登录的浏览器标签页，或在管理器中停止并重新启动这个槽，再打开浏览器。"),
            parent=root)
        return None
    return "http://127.0.0.1:%d" % port


def open_browser(key):
    cfg = VC[key]
    url = _slot_browser_url(key)
    if not url:
        return
    kind, exe = _resolve_browser(cfg["browser"])
    if kind == "start":
        # 两种情形都落到这里：① 配置就是 default（默认值）；② 配了某个浏览器
        # 但本机没装。两者都交给系统默认浏览器，日志要分清，否则用户会以为
        # 「我没配 Chrome 啊，怎么提示找不到 Chrome」。
        # 使用该槽已核实的入口地址，浏览器会话由 dsh 自己建立。
        _b = (cfg["browser"] or "default").strip()
        if _b.lower() in ("", "default"):
            log(T("[%s] 用系统默认浏览器打开 %s") % (key, url))
        else:
            log(T("[%s] 本机没找到 %s，改由系统默认浏览器打开 %s") % (key, _b, url))
        try:
            _platform_open(url)
            log(T("[%s] 浏览器已启动（系统默认）") % key)
        except Exception as e:
            log(T("[%s] 系统默认浏览器打开失败：%s（请手动复制上面的 URL 到浏览器）") % (key, e))
        return
    log(T("[%s] 打开浏览器(%s) -> %s") % (key, cfg["browser"], url))
    try:
        if PLATFORM.IS_MAC and exe.endswith(".app"):
            subprocess.Popen(["/usr/bin/open", "-a", exe, url])
        else:
            subprocess.Popen([exe, url])             # GUI 浏览器绝不能加 CREATE_NO_WINDOW
        log(T("[%s] 浏览器已启动") % key)
    except Exception as e:
        log(T("[%s] 浏览器启动失败：%s") % (key, e))

def copy_url(key):
    """把该槽的完整入口 URL（含 token）复制到剪贴板。

    日志里的 token 已经就地打码，所以"从日志里复制 URL"这条路没有了；
    完整 URL 一直保存在内存的 auth_urls 里，用这个按钮取，既能粘到任意浏览器
    / 别的机器，也不会让 token 落到磁盘上。"""
    url = _slot_browser_url(key)
    if not url:
        return
    try:
        root.clipboard_clear()
        root.clipboard_append(url)
        root.update_idletasks()          # 让剪贴板内容在管理器退出后仍然有效
        if url == auth_urls.get(key):
            log(T("[%s] 已复制入口 URL 到剪贴板（含 token，请勿外发）") % key)
        else:
            log(T("[%s] 已复制无认证的入口 URL 到剪贴板。") % key)
    except Exception as e:
        log(T("[%s] 复制入口 URL 失败：%s") % (key, e))


# ── 长任务期间冻结按钮 ──────────────────────────────────────────────────
# 安装 / 纳入 / 卸载这类会改磁盘的操作进行中，必须把「启动/停止/打开/杀全部」
# 一起冻住 —— 否则可能在 node_modules 搬到一半时被「启动」起来，装出半残状态。
# （2026-10-30 起「换版本」功能已删除，这里只保留通用的冻结机制。）
# 2026-10-31 分两组：「btns」= 底部常驻按钮（一次建好不再变），
# 「card」= 槽卡片里的按钮（改设置要重画卡片，旧的必须能从冻结清单里摘掉，
# 否则 _actions_enabled 会去 configure 已经 destroy 的控件）。
BUSY = {"btns": [], "card": [], "card_run": [], "installing": False, "install_background": False}

def _slot_title(key):
    """卡片标题（单行，紧凑）：`≡ dshV1 · 0.1.5-rc.2`。

    两次瘦身，都是为了在 240px 宽的卡片里把版本号显示全：
    - 2026-10-02 去掉浏览器名（原来形如 "dshV1 (Chrome / 4080) · 实装 0.1.5-rc.2"）：
      「打开浏览器」按钮上本来就写着 Chrome/Edge，标题里再写一遍是重复信息。
    - 2026-10-31 去掉端口和「实装」二字：端口下面状态行就有（`已停止 :4080`），
      「实装」只是标签、不是信息 —— 右边那串数字本身就是真实版本号。
      原来标题长到被卡片裁掉（`0.2.1-al` 这种），反而看不出装的是哪个版本。
    """
    cfg = VC[key]
    ver = INSTALLED.get(key)
    vtxt = ver if ver else T("未知")
    label = cfg.get("label") or key
    # 防御：config 里若出现两条同 label 的记录（旧版纳入逻辑会造出「槽 v4
    # 顶着 dshV3 的名字」这种幽灵槽），标题带上槽键 —— 否则两张卡片同名，
    # 用户根本分不清哪个是哪个。正常情况下 label 与槽键一一对应，不会触发。
    try:
        if sum(1 for _k in SLOT_KEYS if (VC[_k].get("label") or _k) == label) > 1:
            label = "%s（%s）" % (label, key)
    except Exception:
        pass
    # 前缀 ≡ 是拖拽手柄的视觉提示（2026-10-31 加拖拽排序时一并加上）：
    # 光靠鼠标变成四向箭头太隐蔽，标题上给个握把符号用户才知道能拖。
    return "≡ %s · %s" % (label, vtxt)

def _build_version_picker(parent, var, height=9, width=38, multi=False):
    """版本选择器：列出 npm registry 上的**全部**已发布版本，点一行就填进 var。

    为什么要有它：原来只能靠用户手打版本号，而 dsh 的版本号形如 0.2.0-rc.2，
    记错一个字符就白等几分钟的 npm 安装（还常被误判成网络问题）。列表里直接
    标出「最新 / 预览 / 已弃用」，选中即填，手动输入框仍然保留（可填 latest/next）。

    `multi=True` 时列表进入多选模式（Ctrl 点选 / Shift 连选），选中的版本用
    「, 」连起来写进 `var` —— **输入框始终是唯一事实来源**，用户既能从列表多选，
    也能自己敲一串版本号，两者不必维护两套状态。

    联网在后台线程做，结果用 root.after 回主线程 —— tkinter 不是线程安全的。
    返回 {"frame", "refresh", "listbox", "status"}，调用方把它 pack/grid 进对话框。
    """
    box = ttk.LabelFrame(parent, text=T("可用版本（来自 npm registry）"), padding=(8, 6))
    inner = ttk.Frame(box)
    inner.pack(fill="both", expand=True)
    lb = tk.Listbox(inner, height=height, width=width,
                    exportselection=False,
                    selectmode="extended" if multi else "browse",
                    font=("Consolas", 9), activestyle="dotbox")
    sb = ttk.Scrollbar(inner, orient="vertical", command=lb.yview)
    lb.configure(yscrollcommand=sb.set)
    lb.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")

    row = ttk.Frame(box)
    row.pack(fill="x", pady=(6, 0))
    btn = ttk.Button(row, text=T("刷新列表"))
    btn.pack(side="left")
    status = ttk.Label(row, text=T("准备获取…"), foreground=_MUTED)
    status.pack(side="left", padx=(8, 0))

    state = {"items": [], "busy": False}

    def _alive():
        try:
            return bool(box.winfo_exists())
        except Exception:
            return False

    def apply_items(items, source):
        state["items"] = items
        lb.delete(0, "end")
        for it in items:
            lb.insert("end", REG.format_line(it))
        want = (var.get() or "").strip() or REG.default_pick(items)
        idx = next((i for i, it in enumerate(items) if it["version"] == want), None)
        if idx is None and items:
            idx = 0
        if idx is not None:
            lb.selection_clear(0, "end")
            lb.selection_set(idx)
            lb.see(idx)
            var.set(items[idx]["version"])
        status.configure(text=T("共 %d 个版本（%s）") % (len(items), source))
        state["busy"] = False
        btn.configure(state="normal")

    def fail(msg):
        status.configure(text=T("获取失败，可手动输入版本号"))
        log(T("[版本列表] 获取失败：%s") % msg)
        state["busy"] = False
        btn.configure(state="normal")

    def on_pick(_evt=None):
        sel = lb.curselection()
        if not sel:
            return
        vers = [state["items"][i]["version"] for i in sel
                if i < len(state["items"])]
        if not vers:
            return
        var.set(", ".join(vers) if multi else vers[0])

    def do_fetch(force=False):
        if REG is None:
            status.configure(text=T("缺少 dsh_registry.py，只能手动输入版本号"))
            return
        if state["busy"]:
            return
        if not force:
            items, source = REG.cached_versions()
            if items:
                apply_items(items, source + T("，缓存"))
                return
        state["busy"] = True
        btn.configure(state="disabled")
        status.configure(text=T("正在从 npm registry 获取版本列表…"))

        def work():
            ok, items, src = REG.fetch_versions()
            def back():
                if not _alive():
                    return
                if ok:
                    REG.remember(items, src)
                    apply_items(items, src)
                else:
                    fail(src)
            try:
                _post_ui(back)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True, name="dsh-registry").start()

    lb.bind("<<ListboxSelect>>", on_pick)
    btn.configure(command=lambda: do_fetch(True))
    try:
        box.after(60, do_fetch)      # 对话框一出现就开始拉，不让用户干等
    except Exception:
        pass
    return {"frame": box, "refresh": do_fetch, "listbox": lb, "status": status}


# ── 任务栏图标：给进程设独立 AppUserModelID ───────────────────────────────
def _set_app_id():
    """给本进程一个独立的 AppUserModelID，让任务栏图标用海豚图标、独立分组，
    而不是被归到 pythonw.exe 下显示 Python 图标。
    Windows 任务栏按 AppID 分组；pythonw 启动的 GUI 默认归到 pythonw.exe，
    于是任务栏显示 Python 图标。设了显式 AppID + iconbitmap 后，本窗口就有
    自己的海豚图标与分组。必须在创建任何窗口之前调用。"""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            ctypes.c_wchar_p("deepseek.dsh.dualversion.manager.3"))
        log(T("已设置进程 AppID（任务栏将使用海豚图标）"))
    except Exception as e:
        log(T("设置进程 AppID 失败（任务栏可能显示 Python 图标）：%s") % e)

# ── 单实例保护：已有实例时激活其窗口后退出（防「点任务栏又开一个」）─────────
import ctypes  # 此处可能早于文件其他位置的 ctypes 导入，局部确保可用
def _activate_existing_instance():
    """枚举可见顶层窗口，找到标题匹配的已有管理器窗口并前置。找到返回 True。"""
    user32 = ctypes.windll.user32
    found = []
    _ENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(64)
            user32.GetWindowTextW(hwnd, buf, 64)
            if buf.value in (T("DSH 管理器"), T("DSH 版本管理器")):
                found.append(hwnd)
        return 1
    _cb_ref = _ENUMPROC(_cb)          # 防 GC
    user32.EnumWindows(_cb_ref, None)
    if found:
        hwnd = found[0]
        user32.ShowWindow(hwnd, 9)    # SW_RESTORE（最小化则还原）
        user32.SetForegroundWindow(hwnd)
        return True
    return False

# 单实例互斥。**必须用 use_last_error=True + ctypes.get_last_error()**：
# ctypes.windll 的句柄不捕获 last-error，而 GetLastError() 读的是线程的"上一次
# 错误"——那个值可能来自**更早的任意一次 Win32 调用**，与 CreateMutexW 毫无关系。
# 2026-10-31 实测：连着 CreateMutexW 两个全新名字，第二次读到的都是 183，
# 于是「本实例退出」被误触发 —— 症状是双击管理器，窗口一闪不出，日志里却写着
# 「已有实例在运行」。改完后新建返回 0、重复返回 183，行为正确。
if PLATFORM.IS_WINDOWS:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _mutex_h = _k32.CreateMutexW(
        None, False, "DeepSeek.DSH.Manager.SingleInstance")
    if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
        if _activate_existing_instance():
            log(T("已有实例在运行：已激活其窗口，本实例退出（单实例保护）"))
        else:
            log(T("已有实例在运行但未找到其窗口，本实例退出（单实例保护）"))
        sys.exit(0)

# ── 任务栏固定项图标自愈 ──────────────────────────────────────────────────
# 「右键运行中窗口 → 固定到任务栏」生成的固定快捷方式不带 IconLocation，
# 任务栏会回落显示 pythonw.exe 内嵌的 Python logo。这里在每次启动时扫描
# 固定区：凡目标指向本管理器 pythonw 却没有图标的，自动补上海豚图标。
def _heal_pinned_shortcut_icon():
    try:
        from ctypes import (c_void_p, c_wchar_p, c_wchar, c_int, c_ubyte,
                            POINTER, byref, Structure, c_ulong, c_ushort,
                            WINFUNCTYPE)
        import os as _os
        pinned_dir = _os.path.expandvars(
            r"%APPDATA%\Microsoft\Internet Explorer\Quick Launch"
            r"\User Pinned\TaskBar")
        if not _os.path.isdir(pinned_dir):
            return
        target_lwr = _os.path.abspath(STANDALONE_PYW).lower()

        class GUID(Structure):
            _fields_ = [("d1", c_ulong), ("d2", c_ushort), ("d3", c_ushort),
                        ("d4", c_ubyte * 8)]
        def _g(a, b, c, d4):
            return GUID(a, b, c, tuple(int(x, 16) for x in d4.split()))
        CLSID_ShellLink  = _g(0x00021401, 0, 0, "C0 00 00 00 00 00 00 46")
        IID_IShellLinkW  = _g(0x000214F9, 0, 0, "C0 00 00 00 00 00 00 46")
        IID_IPersistFile = _g(0x0000010B, 0, 0, "C0 00 00 00 00 00 00 46")

        ole32 = ctypes.oledll.ole32
        ole32.CoInitialize(None)

        def _vt(obj, slot, restype, argtypes):
            vtbl_ptr = ctypes.cast(obj, POINTER(c_void_p)).contents
            vtbl = ctypes.cast(vtbl_ptr, POINTER(c_void_p))
            fn = ctypes.cast(vtbl[slot], c_void_p)
            return WINFUNCTYPE(restype, *([c_void_p] + argtypes))(fn.value)

        # 逐个 lnk 处理
        for name in _os.listdir(pinned_dir):
            if not name.lower().endswith(".lnk"):
                continue
            full = _os.path.join(pinned_dir, name)
            try:
                punk2 = c_void_p()
                hr = ole32.CoCreateInstance(byref(CLSID_ShellLink), None, 1,
                                            byref(IID_IShellLinkW), byref(punk2))
                if hr != 0 or not punk2.value:
                    continue
                vt_qi = _vt(punk2, 0, ctypes.HRESULT,
                            [POINTER(GUID), POINTER(c_void_p)])
                ppf2 = c_void_p()
                if vt_qi(punk2, byref(IID_IPersistFile), byref(ppf2)) != 0:
                    continue
                Load2   = _vt(ppf2, 5, ctypes.HRESULT, [c_wchar_p, c_int])
                Save2   = _vt(ppf2, 6, ctypes.HRESULT, [c_wchar_p, c_int])
                GetPath = _vt(punk2, 3, ctypes.HRESULT,
                              [c_wchar_p, c_int, c_void_p, c_ulong])
                GetIcon = _vt(punk2, 16, ctypes.HRESULT,
                              [c_wchar_p, c_int, POINTER(c_int)])
                SetIcon = _vt(punk2, 17, ctypes.HRESULT, [c_wchar_p, c_int])

                if Load2(ppf2, full, 0) != 0:
                    continue
                pbuf = ctypes.create_unicode_buffer(1024)
                if GetPath(punk2, pbuf, 1024, None, 0) != 0:
                    continue
                if not pbuf.value or _os.path.abspath(pbuf.value).lower() != target_lwr:
                    continue            # 不是我们管理器的固定项
                ibuf = ctypes.create_unicode_buffer(1024)
                iidx = c_int(0)
                GetIcon(punk2, ibuf, 1024, byref(iidx))
                if ibuf.value.strip():
                    continue            # 已有图标，不动

                # 图标缺失 → 整体重建（实测：自动生成的固定 lnk 结构特殊，
                # 原地 SetIconLocation+Save 不生效，GetIconLocation 回读仍为空）
                punk3 = c_void_p()
                hr3 = ole32.CoCreateInstance(byref(CLSID_ShellLink), None, 1,
                                             byref(IID_IShellLinkW), byref(punk3))
                if hr3 != 0 or not punk3.value:
                    continue
                vt_qi3 = _vt(punk3, 0, ctypes.HRESULT,
                             [POINTER(GUID), POINTER(c_void_p)])
                ppf3 = c_void_p()
                if vt_qi3(punk3, byref(IID_IPersistFile), byref(ppf3)) != 0:
                    continue
                SetPath3  = _vt(punk3, 20, ctypes.HRESULT, [c_wchar_p])
                SetArgs3  = _vt(punk3, 11, ctypes.HRESULT, [c_wchar_p])
                SetWorkD3 = _vt(punk3, 9,  ctypes.HRESULT, [c_wchar_p])
                SetIcon3  = _vt(punk3, 17, ctypes.HRESULT, [c_wchar_p, c_int])
                Save3     = _vt(ppf3, 6,  ctypes.HRESULT, [c_wchar_p, c_int])
                _script = _os.path.abspath(
                    sys.argv[0] if sys.argv and sys.argv[0]
                    else _os.path.join(BASE_DIR, "dsh_manager.pyw"))
                _icon_bs = _os.path.abspath(ICON_PATH)
                _ok = (SetPath3(punk3, STANDALONE_PYW) == 0
                       and SetArgs3(punk3, '"%s"' % _script) == 0
                       and SetWorkD3(punk3, BASE_DIR) == 0
                       and SetIcon3(punk3, _icon_bs, 0) == 0
                       and Save3(ppf3, full, 0) == 0)
                if _ok:
                    log(T("已自动修复任务栏固定项（整体重建+图标）：%s") % name)
            except Exception:
                continue
    except Exception as _e:
        try:
            log(T("固定项图标自愈失败（不影响运行）：%s") % _e)
        except Exception:
            pass

if PLATFORM.IS_WINDOWS:
    _heal_pinned_shortcut_icon()

# 显式 AppID 已停用：本机 ShellLink 的 COM 属性存储接口不可用（QI 恒 E_NOINTERFACE），
# 固定 lnk 上无法写 AppID 属性；保留显式 AppID 反而使运行窗口与固定按钮分家
# （出现两个按钮）。独立 Python 下任务栏图标由窗口 WM_SETICON + 固定 lnk 的
# IconLocation 双路保证，不再需要 AppID。
# _set_app_id()

def _icon_handles():
    """取 .ico 里的原始帧句柄（大/小）。失败返回 (0, 0)。

    用 ExtractIconEx 而不是 LoadImage：前者从 .ico 里挑**已有的帧**，
    后者的尺寸参数意思是"缩放到"，会插值 → 任务栏图标发虚（本项目实测踩过）。
    注意 ExtractIconExW 在 **shell32**，不在 user32（放错库会报 not found）。
    """
    try:
        import ctypes
        from ctypes import wintypes
        if not (ICON_PATH and os.path.exists(ICON_PATH)):
            return (0, 0)
        large = wintypes.HICON(); small = wintypes.HICON()
        if not ctypes.windll.shell32.ExtractIconExW(
                ICON_PATH, 0, ctypes.byref(large), ctypes.byref(small), 1):
            return (0, 0)
        return (large.value or 0, small.value or large.value or 0)
    except Exception:
        return (0, 0)

def _top_hwnd(tk_window):
    """取 Tk 窗口**真实顶层窗口**的 HWND。

    为什么必须这么写（踩过两次的坑）
    ------------------------------
    `tk_window.winfo_id()` 返回的是**内层子窗口**（类名 TkChild），不是任务栏
    和标题栏认的那个顶层窗口（类名 TkTopLevel）。给 TkChild 设图标，
    顶层窗口的图标纹丝不动 —— 表现就是「日志说设置成功，界面仍是羽毛」。
    实测：winfo_id()=6686618 类名 TkChild，其 GetParent()=2820228 类名 TkTopLevel。

    做法：沿 GetParent 一直向上，直到没有父窗口为止。Toplevel（对话框）也
    同样适用，所以 _apply_icon 与 _force_taskbar_icon 都走这里。
    """
    try:
        import ctypes
        u = ctypes.windll.user32
        hwnd = tk_window.winfo_id()
        # 最多向上 8 层，防御意外的环
        for _ in range(8):
            parent = u.GetParent(hwnd)
            if not parent or parent == hwnd:
                break
            hwnd = parent
        return hwnd
    except Exception:
        try:
            return tk_window.winfo_id()
        except Exception:
            return 0

# ── GUI ────────────────────────────────────────────────────────────────────
# 关键顺序：**先**替换 Tk 窗口类的图标，**再**让窗口显示。
# 否则 Tk 会先用它自带的默认图标（羽毛）把窗口映射出来，Windows 任务栏就把它
# 缓存住了；事后 WM_SETICON 只能改标题栏，任务栏仍是羽毛，直到重启资源管理器
# 才刷新（实测踩过）。见 _icon_handles / 下面这段的注释。
if PLATFORM.IS_MAC and tk.TkVersion < 8.6:
    _fatal(T("macOS 需要 Tk 8.6 或更新版本。请使用成品包或安装带新版 Tk 的 Python。"))
try:
    root = tk.Tk()
except Exception as _e:
    import traceback
    log(T("!!! 创建 tkinter 窗口失败：%s\n%s") % (_e, traceback.format_exc()))
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0,
            T("创建窗口失败：%s\n\n详情见日志：%s") % (_e, LOG_FILE),
            T("DSH 管理器"), 0x10)
    except Exception:
        pass
    sys.exit(1)
root.title(T("DSH 管理器"))

# 抢在窗口显示前替换窗口类图标。
# 两处要点（都踩过）：
#   1) 顶层窗口：必须用 _top_hwnd(root)，不能直接 root.winfo_id()——后者是
#      内层 TkChild，给它设图标对标题栏/任务栏无效（这正是之前"日志说成功、
#      界面仍是羽毛"的真正原因）。
#   2) 时机：在窗口首次显示之前。Tk 的顶层窗口类默认**没有**图标
#      （实测 GCLP_HICON=0），Windows 只能回退到系统默认的羽毛；一旦显示出来，
#      任务栏就缓存了它，事后再改也不刷新。
try:
    import ctypes as _ct
    _hb, _hs = _icon_handles()
    _hwnd = _top_hwnd(root)
    if _hb and _hwnd:
        _ct.windll.user32.SetClassLongPtrW(_hwnd, -14, _hb)   # GCLP_HICON
        if _hs:
            _ct.windll.user32.SetClassLongPtrW(_hwnd, -34, _hs)  # GCLP_HICONSM
        # 窗口级也设一次（双保险：类图标对已创建的窗口不一定立即生效）
        _ct.windll.user32.SendMessageW(_hwnd, 0x80, 1, _hb)      # WM_SETICON ICON_BIG
        _ct.windll.user32.SendMessageW(_hwnd, 0x80, 0, _hs or _hb)
        _rb = _ct.windll.user32.GetClassLongPtrW(_hwnd, -14)
        log(T("已在窗口显示前设置顶层窗口(%s)图标，回读 GCLP_HICON=%s")
            % (_hwnd, T("成功") if _rb == _hb else T("失败(%s)") % _rb))
    elif PLATFORM.IS_WINDOWS:
        log(T("未能取到 ico 帧或顶层窗口，窗口将使用 Tk 默认图标"))
except Exception as _e:
    log(T("替换窗口类图标失败（不影响运行）：%s") % _e)

# ── 界面配色：向 dsh 本体的视觉语言靠拢（2026-11-01）──────────────────
# 用户要求「整体风格往 dsh 靠近」。做法不是凭感觉配色，而是**从 dsh 自己的主题包里
# 取真实色值**（<dsh>/node_modules/@deepseek-ai/dsh-client-ui-theme/lib）：
#   品牌主色  #4176e6（brand-primary）/ #3964fe（accent）
#   近黑文字  #151517 / #43454a
#   页面底色  #f9fafb / #f5f6f7
#   成功      #22c55e      危险 #ec1313 / #f25a5a
# 与 dsh 保持一致后，两个界面并排使用不会有"两套设计"的割裂感。
# 仅改外观，不动布局与按钮冻结逻辑；主题初始化失败则静默回退默认主题。
_APP_BG    = "#f5f6f7"   # 应用底色（dsh 页面底）
_CARD_BG   = "#ffffff"   # 卡片白底
_CARD_BD   = "#e4e6ea"   # 卡片边框（dsh 细边框观感）
_INK       = "#151517"   # 主文字（dsh 近黑）
_MUTED     = "#545557"   # 次要文字（dsh 灰）
_DIVIDER   = "#e4e6ea"   # 分隔线
_BRAND     = "#4176e6"   # 品牌主色（dsh brand-primary）
_BRAND_D   = "#3358b3"   # 主色按下态
_GREEN     = "#22c55e"   # 成功（dsh success）
_GREEN_D   = "#16a34a"
_RED       = "#ec1313"   # 危险（dsh error）
_RED_D     = "#c01010"
_NEUTRAL   = "#f1f2f4"
_NEUTRAL_BD= "#dcdfe4"
_NEUTRAL_D = "#e6e8eb"
_AMBER     = "#d97706"
_LINK      = "#3964fe"   # 链接/强调（dsh accent）
_LINK_H    = "#2b4fd0"
# 运行态 → 状态文字颜色
_STATUS_COLORS = {
    "running_tracked":   _GREEN,
    "running_external":  _GREEN,
    "starting":          _AMBER,
    "start_timed_out":   _RED,
    "start_failed":      _RED,
    "external_conflict": _RED,
    "stopped":           _MUTED,
}
# 2026-11-01：配色统一由 _apply_palette() 应用（浅色/深色/跟随系统），
# 下面原有的 configure 保留作为兜底（在 _apply_palette 之前跑，随后被覆盖）。
try:
    _style = ttk.Style()
    _style.theme_use("clam")
    _style.configure(".", background=_APP_BG, foreground=_INK, font=("Segoe UI", 10))
    _style.configure("TLabel", background=_APP_BG, foreground=_INK)
    _style.configure("TFrame", background=_APP_BG)
    # 默认按钮：中性灰
    _style.configure("TButton",
                     background=_NEUTRAL, foreground=_INK,
                     bordercolor=_NEUTRAL_BD, darkcolor=_NEUTRAL,
                     lightcolor=_NEUTRAL, relief="raised", padding=(6, 3))
    _style.map("TButton",
               background=[("active", _NEUTRAL_D), ("pressed", _NEUTRAL_D)],
               foreground=[("active", _INK), ("pressed", _INK)])
    # 启动（绿）
    _style.configure("Green.TButton",
                     background=_GREEN, foreground="white",
                     bordercolor=_GREEN, darkcolor=_GREEN,
                     lightcolor=_GREEN, relief="raised", padding=(6, 3))
    _style.map("Green.TButton",
               background=[("active", _GREEN_D), ("pressed", _GREEN_D)],
               foreground=[("active", "white"), ("pressed", "white")])
    # 品牌蓝主按钮（2026-11-01）：用于「安装」这类**主要行动**。
    # 绿色只保留给"启动/运行"这种状态语义，避免满屏绿。
    _style.configure("Brand.TButton",
                     background=_BRAND, foreground="white",
                     bordercolor=_BRAND, darkcolor=_BRAND,
                     lightcolor=_BRAND, relief="raised", padding=(6, 3))
    _style.map("Brand.TButton",
               background=[("active", _BRAND_D), ("pressed", _BRAND_D)],
               foreground=[("active", "white"), ("pressed", "white")])
    # 停止 / 杀全部（红）
    _style.configure("Red.TButton",
                     background=_RED, foreground="white",
                     bordercolor=_RED, darkcolor=_RED,
                     lightcolor=_RED, relief="raised", padding=(6, 3))
    _style.map("Red.TButton",
               background=[("active", _RED_D), ("pressed", _RED_D)],
               foreground=[("active", "white"), ("pressed", "white")])
    _style.configure("TSeparator", background=_DIVIDER)
    # 轻红（描边式）：「杀全部进程」用。它虽然也是"杀"，但比「卸载」轻得多
    # （不删文件、随时能再启动），以前和卸载一样通栏纯红，视觉上比卸载还重，
    # 容易让人以为它更危险。改成浅底红字：一眼看得出是"危险动作"，但不抢戏。
    _style.configure("SoftRed.TButton",
                     background="#fdecec", foreground="#b91c1c",
                     bordercolor="#f3b4b4", darkcolor="#fdecec",
                     lightcolor="#fdecec", relief="raised", padding=(6, 3))
    _style.map("SoftRed.TButton",
               background=[("active", "#fbdada"), ("pressed", "#fbdada")],
               foreground=[("active", "#991b1b"), ("pressed", "#991b1b")])
    _style.configure("TLabelframe", background=_CARD_BG,
                     bordercolor=_CARD_BD, relief="solid")
    # 标题行留白：clam 主题下 LabelFrame 的标题**紧贴边框线**（文字下缘几乎被线切到），
    # 看着又挤又窄。2026-10-31 先给 (6,3)，用户反馈「还是太矮」，加到 (6,6)：
    # 上下各 6px，标题带从约 22px 变成约 28px，明显透气但不至于把卡片撑高。
    # （实测底色在 clam 里对 LabelFrame 标题不生效，灰底白底渲染一样，别在底色上折腾。）
    _style.configure("TLabelframe.Label", background=_APP_BG, foreground=_INK,
                     font=("Segoe UI", 10, "bold"), padding=(6, 6))
    # 卡片内部：白底标签 / 白底容器（盖掉全局浅灰，保证卡片是干净白块）
    _style.configure("Card.TFrame", background=_CARD_BG)
    _style.configure("Card.TLabel", background=_CARD_BG, foreground=_INK)
    # 可点击链接（卡片底部"数据目录"）
    _style.configure("Link.TLabel", background=_CARD_BG, foreground=_LINK,
                     font=("Segoe UI", 8, "underline"))
    # 卡片顶部拖拽条：2026-10-31 已删除（设计要求：不要「按住…即可拖动」这种提示，
    # 鼠标移上去变成四向箭头就够明显了）。现在整张卡片的空白处都能拖，见 _bind_drag。
    # 拖动中被"拿起来"的那张卡片：边框换成主题色 + 加粗，配合它下面的灰色
    # 影子块，做出手机拖动时"卡片浮起来"的观感（clam 主题认 bordercolor）。
    _style.configure("Lifted.TLabelframe", background=_CARD_BG,
                     bordercolor=_LINK, relief="solid", borderwidth=2)
    _style.configure("Lifted.TLabelframe.Label", background=_APP_BG,
                     foreground=_INK, font=("Segoe UI", 10, "bold"),
                     padding=(6, 6))
except Exception as _e:
    log(T("皮肤初始化失败，回退默认主题：%s") % _e)
root.configure(bg=_APP_BG)

root.resizable(True, True)
# 窗口宽度按**槽数**给，而不是写死 800：每槽要放得下「≡ dshV2 · 0.2.1-alpha.2」
# 这样的标题和两列按钮。4 槽时 800px 宽会让每槽只剩 ~190px，标题被截成
# "≡ dshV2 · 0.2.1-al"，按钮文字也被切（实测截图）。
# 240px/槽 是实测能完整显示的下限（2026-10-31 标题去掉端口与「实装」后，
# 最长的一条也只用掉 134px，留 77px 余量）；再窄就靠 minsize 让用户手动拉宽。
# 240px/槽 是实测能完整显示的下限；再窄就靠 minsize 让用户手动拉宽。
_SLOT_MIN_W = 240
# 2026-10-31：按**实际显示的槽数**算，不按 config 里的槽数 —— 用户把槽隐藏掉
# 就是为了界面窄一点，还按全量算就白设了。DISPLAY_KEYS 为空（零槽/全隐藏）时退回 800。
#
# 2026-11-01（槽上限 5→6）：**不再让窗口无限变宽**。
# 卡片是排成**一行**的（_grid_columns 用 n 列，没有换行逻辑），6 槽按 240px/槽
# 算出来 ≈1530px —— 1080p 还行，但 1366×768 的笔记本会直接溢出屏幕。
# 所以给一个上限：取"按槽数算出的理想宽度"与"屏幕可用宽度的 92%"中较小者。
# 超过时窗口不再加宽，卡片区自动出现横向滚动（见下方 _cards_scroll 的说明）
# —— 宁可滚动，也不让按钮被切在屏幕外。
def _screen_usable_w():
    """屏幕可用宽度（扣掉任务栏等）。取不到就退回 1366 这个保守值。"""
    try:
        return int(root.winfo_screenwidth() * 0.92)
    except Exception:
        return 1366


_ideal_w = max(800, len(DISPLAY_KEYS) * _SLOT_MIN_W + 90)
_win_w = min(_ideal_w, max(800, _screen_usable_w()))
root.minsize(min(_win_w, 680 + len(DISPLAY_KEYS) * 40), 560)
try:
    if ICON_PATH and os.path.exists(ICON_PATH):
        # 刻意**不**用 root.iconbitmap()：Tk 会把 .ico 整个读成一个位图再缩放，
        # 任务栏显示的就是缩放后的模糊结果（实测"图标很虚"）。而且 Tk 不支持
        # iconphoto(ICO)（实测报couldn't recognize data in image file）。
        # 统一交给下面的 _force_taskbar_icon() 用 Win32 ExtractIconEx 按原始帧设置。
        log(T("图标将由 _force_taskbar_icon() 以原始帧设置（不走 Tk 的缩放路径）"))
except Exception as e:
    log(T("窗口图标路径检查失败：%s") % e)

def _force_taskbar_icon():
    """用 Win32 API 显式设置窗口的 ICON_BIG / ICON_SMALL / 类图标。

    为什么不用 LoadImageW 缩放
    ----------------------
    原来写的是 `LoadImageW(..., 32, 32, LR_LOADFROMFILE)`。那个尺寸参数的意思是
    "**缩放到** 32x32"，不是"**取** 32x32 那帧"。而 LoadImage 默认不加
    LR_DEFAULTSIZE 之外的缩放质量控制，Windows 会用双线性把256 帧缩到 32，
    于是任务栏上的图标**发虚、边缘发毛**（实测反馈"图标很虚"）。
    本项目的 .ico 本身就带 16/24/32/48/64/128/256 七帧，**根本不需要缩放**。

    现在的做法：ExtractIconEx 按系统**实际请求的**尺寸挑最接近的那一帧，
    取的是原始像素，没有缩放损失。
    """
    try:
        import ctypes
        from ctypes import wintypes
        log(T("图标自诊断：cwd=%s ICON_PATH=%s 存在=%s")
            % (os.getcwd(), ICON_PATH, bool(ICON_PATH and os.path.exists(ICON_PATH))))
        if not (ICON_PATH and os.path.exists(ICON_PATH)):
            log(T("!! 图标文件找不到（相对路径+错误工作目录会导致这里静默失败）"))
            return
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32     # ExtractIconExW 属于 Shell Functions
        WM_SETICON = 0x80
        ICON_SMALL, ICON_BIG, ICON_SMALL2 = 0, 1, 2
        GCLP_HICON, GCLP_HICONSM = -14, -34
        # 关键：必须用 _top_hwnd() 取**真实顶层窗口**（TkTopLevel）。
        # 这里的注释曾经写反过一次（说 "winfo_id() 本身就是顶层窗口"）——
        # 实测 winfo_id() 返回的是内层 TkChild，给它设图标对标题栏/任务栏
        # 完全无效，症状就是"日志说设置成功、界面仍是羽毛"。
        hwnd = _top_hwnd(root)

        def _pick(cssize):
            """ExtractIconEx 会返回【大图标(32)】与【小图标(16)】两个句柄。
            cssize 只影响"取哪个尺寸变体"，内部仍从 .ico 里取原始帧，不缩放。

            注意：ExtractIconExW 在 **shell32**（Shell Functions），不在 user32。
            误放到 user32 会报 "function 'ExtractIconExW' not found"（实测踩过）。
            """
            large = wintypes.HICON()
            small = wintypes.HICON()
            # uIcons=1 表示"全部取同一个大图标"
            ok = shell32.ExtractIconExW(ICON_PATH, 0, ctypes.byref(large),
                                        ctypes.byref(small), 1)
            if ok and large.value:
                # 小图标句柄常常为 0（ico 无 16 帧时），退回用大的那份
                return large.value if cssize == ICON_BIG else (small.value or large.value)
            return 0

        h_big = _pick(ICON_BIG)
        h_small = _pick(ICON_SMALL)
        # 1) 窗口消息级：任务栏和标题栏都认这个
        if h_big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, h_big)
            user32.SetClassLongPtrW(hwnd, GCLP_HICON, h_big)
        if h_small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, h_small)
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL2, h_small)
            user32.SetClassLongPtrW(hwnd, GCLP_HICONSM, h_small)
        log(T("已用原始帧设置窗口图标（不缩放，避免发虚）"
            "（ICON_BIG=%s ICON_SMALL=%s）") % (bool(h_big), bool(h_small)))
    except Exception as e:
        log(T("强制设置任务栏图标失败：%s") % e)

if PLATFORM.IS_WINDOWS:
    _force_taskbar_icon()

def _make_taskbar_window(win):
    """让 `overrideredirect` 的无边框窗口**仍然出现在任务栏**里，2026-11-14。

    为什么需要它：进度窗要「右上角只有一个最小化、没有 X」，而 Windows 的标题栏
    按钮是**成套**的（去掉 `WS_SYSMENU` 会连最小化一起消失），所以只能不要系统
    标题栏、自己画一条（见 `_GuiProgress`）。代价是 `overrideredirect(True)` 会
    让窗口变成"工具窗口"：**Tk 给它默认加了 `WS_EX_TOOLWINDOW`，而工具窗口不进
    任务栏** —— 这就是早先试自绘标题栏时「最小化后找不到窗口」的真正原因
    （当时误判成"自绘这条路走不通"）。清掉 `WS_EX_TOOLWINDOW`、加上
    `WS_EX_APPWINDOW`，任务栏按钮就回来了；`ShowWindow(SW_RESTORE)` 能正常恢复。

    实测（2026-11-14，runtime python 3.10 + Win11）：
      exstyle before = 0x80 (WS_EX_TOOLWINDOW) → after = 0x40000 (WS_EX_APPWINDOW)
      最小化后 IsIconic=True、state=iconic，任务栏出现按钮，恢复后 mapped=1。
    """
    if not PLATFORM.IS_WINDOWS:
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        # 和 _hide_titlebar_buttons 同一个坑：Toplevel 刚建好时 wm_frame() 还是
        # "0x0"，拿去改样式会静默失败。必须先映射一次。
        win.update_idletasks()
        win.update()
        hwnd = int(win.wm_frame(), 16)
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_APPWINDOW = 0x00040000
        ex = user32.GetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE)
        user32.SetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE,
                                 (ex | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW)
        user32.SetWindowPos(wintypes.HWND(hwnd), None, 0, 0, 0, 0,
                            0x0020 | 0x0001 | 0x0002 | 0x0004)  # FRAMECHANGED|NOSIZE|NOMOVE|NOZORDER
    except Exception as e:
        try:
            log(T("[界面] 设置任务栏入口失败（不影响安装）：%s") % e)
        except Exception:
            pass


def _apply_icon(win):
    """给任意 Tk 窗口（主窗口 / Toplevel 对话框）设置图标，用原始帧不缩放。

    为什么需要它：原先靠 root.iconbitmap(default=...) 让 Toplevel 继承图标，
    去掉那条（它会导致缩放发虚）之后，对话框就会退回 Tk 默认图标。
    这里对每个对话框显式调一次。

    win 要在 update_idletasks() 之后调用——Toplevel 的 HWND 在那之前还没建好。

    2026-11-01 性能修复（打开设置时发白/闪一下）：
    以前在**对话框还没构建完**时就同步调 update_idletasks()，等于强行把半成品
    窗口画到屏幕上，再继续往上堆控件 —— 用户看到的就是"白块一闪"。
    现在改为把设图标这件事**排到事件队列末尾**（after_idle）：对话框先把该建的
    控件建完、由 Tk 正常一次性绘制，末尾再补图标。设图标本来就只是锦上添花，
    晚一帧完全无感，却省掉一次昂贵的中途强制重绘。
    """
    def _do():
        try:
            import ctypes
            from ctypes import wintypes
            if not (ICON_PATH and os.path.exists(ICON_PATH)):
                return
            hwnd = _top_hwnd(win)
            if not hwnd:
                return
            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32     # ExtractIconExW 在 shell32，不在 user32
            large = wintypes.HICON(); small = wintypes.HICON()
            if not shell32.ExtractIconExW(ICON_PATH, 0, ctypes.byref(large),
                                          ctypes.byref(small), 1) or not large.value:
                return
            user32.SendMessageW(hwnd, 0x80, 1, large.value)      # WM_SETICON ICON_BIG
            user32.SendMessageW(hwnd, 0x80, 0, small.value or large.value)
        except Exception:
            pass        # 图标只是锦上添花，绝不能因此让对话框打不开

    try:
        win.after_idle(_do)
    except Exception:
        pass

status_vars = {k: tk.StringVar(value="—") for k in SLOT_KEYS}
status_lbls = {}            # 各槽状态标签（用于按运行态上色）
title_lbls  = {}            # 各槽卡片（LabelFrame），刷新时更新 text

# ── 顶部标题 ───────────────────────────────────────────────
_hdr = ttk.Frame(root)
_hdr.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 8))
# 2026-11-02 用户要求：
#   · 标题显示项目名及版本号
#   · 「在 GitHub 收藏」放主界面（原来在设置里）
#   · 修深色模式下标题看不清
#     （原来 foreground 固定成创建时的 _INK=浅色值，切深色后不会变）
# 这里不固定 foreground，改由 _refresh_header_style() 在主题应用后统一刷。
_hdr_title = ttk.Label(_hdr, text="DSH-TM",
                       font=("Segoe UI", 18, "bold"))
_hdr_title.pack(side="left")
_hdr_ver = ttk.Label(_hdr, text="", font=("Segoe UI", 10))
_hdr_ver.pack(side="left", padx=(8, 0))
# 2026-11-02 用户要求：版本号右侧那行说明字去掉（主界面保持简洁）
_hdr_sub = None


def _refresh_header_style():
    """按当前主题刷标题栏三种文字的颜色（深色下必须可读）。"""
    pal = _palette()
    for w, key in ((_hdr_title, "ink"), (_hdr_ver, "muted"), (_hdr_sub, "muted")):
        if w is None:
            continue
        try:
            w.configure(foreground=pal[key])
        except Exception:
            pass
    try:
        _hdr_ver.configure(text="v" + _app_version())
    except Exception:
        pass


# 按钮放在函数定义之后创建（见下方），避免又出现"引用早于定义"的 NameError——
# .pyw 无控制台，这种错误会表现为"双击没反应"，极难定位。

_ENV_REPORT = set()


def _start_env_report(mode):
    """Run one check for pending startup and Settings requests."""
    if _ENV_REPORT:
        _ENV_REPORT.add(mode)
        return
    _ENV_REPORT.add(mode)
    tmp = os.path.join(os.path.dirname(CONFIG_PATH), "temp", "env-check.out")
    scratch = "%s.tmp-%d" % (tmp, os.getpid())
    script = os.path.join(BASE_DIR, "scripts", "check-env.py")
    env = dict(os.environ, DSH_LANG=I18N.lang())
    markers = ("[OK]", T("[注意]"), T("[缺失]"))
    log(T("[环境] 正在后台检查运行环境…"))

    def done(rc, raw, error=""):
        requests = set(_ENV_REPORT)
        _ENV_REPORT.clear()
        if rc is None:
            log(T("[环境] 自动体检失败：%s") % error)
            if "dialog" in requests:
                messagebox.showerror(T("检查失败"), T("运行环境检查出错：\n%s") % error,
                                     parent=root)
            return
        if "summary" in requests:
            for line in raw.splitlines():
                if line.strip().startswith(markers):
                    log(T("[环境] ") + line.strip())
        log(T("[环境] 完整报告：%s") % tmp)
        if "dialog" in requests:
            lines = [line.rstrip() for line in raw.splitlines()
                     if line.strip() and not set(line.strip()) <= set("=-")]
            body = "\n".join(lines[-40:])
            log(T("=== 环境检查（rc=%d）===\n%s") % (rc, body))
            (messagebox.showinfo if rc == 0 else messagebox.showwarning)(
                T("运行环境检查"), "%s\n\n%s" % (
                    T("环境满足要求。") if rc == 0 else T("环境有不满足项，请看下面的说明。"),
                    body), parent=root)

    def work():
        try:
            if not PLATFORM.FROZEN and not os.path.isfile(script):
                raise FileNotFoundError(T("找不到 %s") % script)
            os.makedirs(os.path.dirname(tmp), exist_ok=True)
            with open(scratch, "wb") as out:
                result = subprocess.run([sys.executable, "--check-env" if PLATFORM.FROZEN else script,
                                         "--config", CONFIG_PATH], stdout=out,
                                        stderr=subprocess.STDOUT, env=env,
                                        creationflags=CREATE_NO_WINDOW if PLATFORM.IS_WINDOWS else 0)
            with open(scratch, encoding="utf-8", errors="replace") as source:
                raw = source.read()
            os.replace(scratch, tmp)
        except (OSError, subprocess.SubprocessError) as exc:
            try:
                os.unlink(scratch)
            except OSError:
                pass
            _post_ui(done, None, "", str(exc))
            return
        _post_ui(done, result.returncode, raw)

    threading.Thread(target=work, daemon=True).start()


def _show_env_report():
    _start_env_report("dialog")

# 2026-10-31：标题栏的「检查运行环境」按钮撤掉了。它查的是**管理器自己**的前置条件
# （Python / tkinter / 标准库 / node / 权限 / 端口）—— 而这些在管理器已经打开的
# 情况下基本必然通过，剩下的 node 版本、槽文件完整性、端口占用，卡片状态行和
# 「槽体检」已经覆盖得更准（按槽、只读）。对日常用户它就是一段看不懂的噪音。
# 脚本仍然保留（scripts/check-env.py），换新机器 / 打包时由维护者直接跑；
# 零槽引导卡里也留了一个入口 —— 那是唯一「我这台机器到底行不行」的场合。

# ── 三个版本卡片（平等等宽，中间用竖向分隔线分开）─────────────
# 网格：每槽占 1 列，槽与槽之间插 1 列竖线；卡片列 weight=1 → 等宽。
def _platform_open(path):
    if PLATFORM.IS_MAC:
        subprocess.Popen(["/usr/bin/open", path], stderr=subprocess.DEVNULL)
    else:
        os.startfile(path)


def _open_dir(path, what=T("数据目录")):
    """用资源管理器打开目录。目录不存在 / 打开失败都要显式报错
    （pythonw 无控制台，只写日志用户看不见，所以同时弹框）。"""
    try:
        if not os.path.isdir(path):
            log(T("打开%s失败（目录不存在）：%s") % (what, path))
            messagebox.showerror(T("DSH 管理器"),
                                 T("%s不存在：\n%s\n\n可能尚未初始化（先启动一次 dsh）。") % (what, path))
            return
        _platform_open(path)          # Windows：交给资源管理器打开文件夹
        log(T("已打开%s：%s") % (what, path))
    except Exception as e:
        log(T("打开%s失败 %s：%s") % (what, path, e))
        messagebox.showerror(T("DSH 管理器"), T("打开%s失败：\n%s\n\n%s") % (what, path, e))

cards_frame = ttk.Frame(root)
cards_frame.grid(row=1, column=0, sticky="nsew", padx=12, pady=4)

# ── 窄屏说明（2026-11-01，槽上限 5→6）──────────────────────────────
# 卡片是**排成一行**的（_grid_columns 用 n 列，没有换行逻辑）。6 槽理想宽度 ≈1530px，
# 1080p 放得下；但 1366×768 这类笔记本屏会把最右侧卡片切在屏幕外。
# 处理见上方 _win_w：窗口宽度本身封顶在屏幕可用宽度的 92%，超出部分由用户
# 通过「设置 → 版本槽」隐藏不看的槽来腾地方（隐藏 ≠ 卸载，随时能调回来）。
# 刻意不改造成 Canvas + Frame —— 卡片拖拽依赖 cards_frame 的 grid_bbox /
# winfo_rootx 等几何接口（20+ 处引用），换容器会把拖拽整套打碎。

# ── 卡片拖拽排序（2026-10-31）─────────────────────────────────────────
# 直接在主窗口把卡片拖到想要的左右位置，松手即记住，不必打开设置对话框。
#
# 关键实现约束：拖动期间**不能**调 _build_cards()。它会 destroy 全部卡片，
# 正在拖的那个控件一起没了 → tk 的隐式 grab 断掉 → B1-Motion 再也送不到
# 拖动的控件，拖动当场"卡死"。所以拖动期间只改 grid 的列号（控件活着，
# 隐式 grab 还在），松手后才重建一次。
CARD_WIDGETS = {}          # 槽键 -> 卡片 LabelFrame（算落点、改列号都靠它）
CARD_SEPS = []             # 卡片之间的竖向分隔线（顺序变了要重建）
# 所有键都要在这里给初值 —— _drag_end 被 toplevel 的 <ButtonRelease-1> 兜底绑着，
# 点日志区、点按钮都会走到它；靠 _drag_start 才填的键一旦被先读就是 KeyError，
# 而 .pyw 没有控制台，这个异常会静默刷屏（2026-10-31 修）。
DRAG = {
    "key": None,            # 正在拖的槽键
    "x0": 0, "y0": 0,       # 按下时的屏幕坐标（判 8px 阈值）
    "moved": False,         # 是否已越过阈值（真拖过，决定要不要吞掉按钮点击）
    "picked": False,        # 卡片是否已浮起
    "shadow": None,         # 浮起时垫在下面那张"影子"块
    "gap": 0,               # 当前落点：插到其它卡片的第几个位置
    "order": [],            # 拖动开始时的工作顺序副本
    "off_x": 0, "off_y": 0,  # 光标在卡片内的偏移（不记就会"跳"到指针底下）
    "w": 0, "h": 0,         # 抽走前的宽高（place 不给尺寸卡片会缩水）
    "style0": "",           # 原样式（浮起时换 Lifted，落位后还原）
    "lift": 0.0,            # 浮起动画的当前抬升量（0 → _LIFT_MAX）
    "evt": None,            # 最近一次鼠标事件（动画重绘要用）
    "geom": None,           # 拖动开始时缓存的卡片区几何（避免每帧同步问 X 服务器）
    "move_job": None,       # 合并鼠标移动的 after id（每帧最多重绘一次）
    "lift_job": None,       # 浮起动画的 after id
    "drop_job": None,       # 落位动画的 after id
    "dropping": None,       # 正在滑回格子的那张卡片
}
_LIFT_MAX = 7              # 浮起时卡片比光标再抬高几像素
_DROP_STEPS = 9            # 落位动画帧数（9 帧 × 16ms ≈ 150ms）
_DRAG_FRAME_MS = 16        # 拖动重绘的合并间隔：16ms ≈ 60fps，肉眼已足够顺

# 当前活着的悬停提示窗（tkinter 没有内置 tooltip，只能自己拿 Toplevel 造）。
# 必须集中登记：卡片会因「改显示设置」被整体销毁重建，此时 <Leave> 不一定触发，
# 不主动收就会在屏幕上留一个飘着的黑框。
_TIPS = []


def _hide_tips():
    """关掉所有悬停提示。卡片重建 / 程序退出时调用。"""
    for w in _TIPS:
        try:
            w.destroy()
        except Exception:
            pass
    _TIPS[:] = []


def _tip_attach(widget, textfunc):
    """给控件挂一个悬停提示（tkinter 没有内置 tooltip）。

    `textfunc()` 返回空串就不显示 —— 状态行只在**体检有问题时**才需要解释，
    平时把鼠标放上去不该冒出个空框。文字用深灰底白字，跟卡片的白底拉开对比。

    为什么需要它（2026-10-31 反馈「dshV4 里的警示符号是什么意思」）：
    状态行末尾只有一个 `⚠1`，数字代表「体检发现 1 个错误/警告」，
    但界面上没有任何地方能看出**是哪一条** —— 用户只能去翻日志。
    挂上提示后鼠标一放就知道，不用点任何东西。
    """
    def _hide(_e=None):
        _hide_tips()

    def _show(_e=None):
        _hide_tips()
        try:
            txt = textfunc() if callable(textfunc) else str(textfunc or "")
        except Exception:
            txt = ""
        if not txt:
            return
        try:
            w = tk.Toplevel(widget)
            w.wm_overrideredirect(True)          # 无标题栏、不进任务栏
            try:
                w.attributes("-topmost", True)
            except Exception:
                pass
            tk.Label(w, text=txt, justify="left", bg="#374151", fg="#f9fafb",
                     font=("Microsoft YaHei UI", 9), padx=9, pady=6,
                     wraplength=340).pack()
            x = widget.winfo_rootx() + 6
            y = widget.winfo_rooty() + widget.winfo_height() + 6
            w.wm_geometry("+%d+%d" % (x, y))
            _TIPS.append(w)
        except Exception:
            pass

    widget.bind("<Enter>", _show, add="+")
    widget.bind("<Leave>", _hide, add="+")
    widget.bind("<Destroy>", _hide, add="+")
    return _show


def _clear_cards():
    """清空卡片区：销毁控件 + 复位列配置 + 把旧按钮从冻结清单里摘掉。

    列配置必须复位 —— 上一次显示 4 槽时给第 0/2/4/6 列配了 weight=1，
    改成 2 槽后若不复位，那两个空列仍然吃宽度，卡片会被挤到左边一小块。
    按钮也必须摘掉 —— 否则 _actions_enabled 会去 configure 已经 destroy 的控件。
    """
    for w in list(cards_frame.winfo_children()):
        w.destroy()
    for c in range(MAX_SLOTS * 2 + 2):
        try:
            # uniform 也要清：它把同组列的宽度绑在一起，只把 weight 归零
            # 仍可能被 uniform 组算进「等宽」里，留下列宽残影。
            cards_frame.columnconfigure(c, weight=0, minsize=0, uniform="")
        except Exception:
            pass
    for b in BUSY["card"]:
        try:
            BUSY["btns"].remove(b)
        except ValueError:
            pass
    BUSY["card"] = []
    BUSY["card_run"] = []
    title_lbls.clear()
    status_lbls.clear()
    CARD_WIDGETS.clear()
    CARD_SEPS[:] = []
    _hide_tips()


def _drag_target(others, x_root):
    """按鼠标横坐标算「插到其它卡片里的第几个位置」（0..len(others)）。

    只看**其它**卡片的中线：中线在鼠标右边的那一张，就是插入点。
    这样最左（返回 0）与最右（返回 len(others)）都自然得出，
    不需要为边界写特例。
    """
    for i, k in enumerate(others):
        w = CARD_WIDGETS.get(k)
        if w is None:
            continue
        try:
            if not w.winfo_exists():
                continue
            cx = w.winfo_rootx() + w.winfo_width() // 2
        except Exception:
            continue
        if x_root < cx:
            return i
    return len(others)


def _grid_columns(n):
    """按 n 张卡片配列宽（每槽 2 列：卡片 + 分隔线）。

    整段复位再配：上一次 4 槽留下的第 6 列仍配着 weight，不复位的话
    卡片会被挤到左边一小块（_clear_cards 里同样的坑）。
    """
    for c in range(MAX_SLOTS * 2 + 2):
        try:
            cards_frame.columnconfigure(c, weight=0, minsize=0, uniform="")
        except Exception:
            pass
    for c in range(n):
        cards_frame.columnconfigure(c * 2, weight=1, uniform="card")
    for c in range(n - 1):
        cards_frame.columnconfigure(c * 2 + 1, weight=0, minsize=16)


def _back_to_grid(w):
    """把（可能正浮着的）卡片交回 grid 管理。"""
    try:
        if w.winfo_manager() == "place":
            w.place_forget()
    except Exception:
        pass


def _layout_slots(order, gap=None):
    """按 order 摆卡片；gap 不为 None 时，在第 gap 个槽位留一个空洞。

    空洞 = 那一列不放控件但**保留列宽**。这就是手机拖动时"其它卡片让位"
    的效果 —— 被拿起来的那张从格子里抽走，剩下的按落点重新排队，
    中间空出一块等着它落下去。
    """
    n = len(order) + (1 if gap is not None else 0)
    _grid_columns(n)
    if gap is None:
        slots = list(range(n))
    else:
        slots = [i for i in range(n) if i != gap]
    for slot, k in zip(slots, order):
        w = CARD_WIDGETS.get(k)
        if w is None:
            continue
        _back_to_grid(w)
        try:
            w.grid(row=0, column=slot * 2, sticky="nsew", padx=2, pady=2)
        except Exception:
            pass
    # 分隔线重建；拖动中有空洞时不画（插在空洞旁边看着乱）
    for sp in list(CARD_SEPS):
        try:
            sp.destroy()
        except Exception:
            pass
    CARD_SEPS[:] = []
    if gap is None:
        for i in range(n - 1):
            sp = ttk.Separator(cards_frame, orient="vertical")
            sp.grid(row=0, column=i * 2 + 1, sticky="ns")
            CARD_SEPS.append(sp)


def _regrid(keys):
    """按新顺序把现有卡片重新摆好（不销毁、不重建）。"""
    _layout_slots(keys, gap=None)


def _slot_rect(slot):
    """量出第 slot 个格子此刻的像素矩形（相对 cards_frame）。

    用 grid_bbox 让 grid 自己算 —— 列宽是 weight=1 均分的、中间还夹着分隔线的
    minsize，手算容易差几像素，落位动画最后一帧会"跳"一下。
    grid 的 padx/pady=2 是卡片外留白，要减掉才是卡片真正该占的矩形。
    """
    try:
        cards_frame.update_idletasks()
        x, y, w, h = cards_frame.grid_bbox(slot * 2, 0, slot * 2, 0)
    except Exception:
        return None
    if w <= 4 or h <= 4:
        return None
    return (x + 2, y + 2, w - 4, h - 4)


def _move_float(evt=None):
    """让浮起来的那张卡片跟着鼠标走（含它下面那块"影子"）。

    lift 是浮起动画的当前抬升量：卡片比光标再高一点点，影子留在原处 ——
    两者之间拉开的那道缝就是"拿在手上"的手感。
    卡片不许被拖出卡片区：父控件会把超出的部分裁掉，卡片被切掉半截
    比停在边上难看得多，所以出界就贴边。
    """
    evt = evt or DRAG.get("evt")
    key = DRAG["key"]
    card = CARD_WIDGETS.get(key)
    if card is None or not DRAG["picked"] or evt is None:
        return
    DRAG["evt"] = evt
    lift = DRAG["lift"]
    # 2026-11-01 性能修复（拖动发白/发花）：几何值在拖动开始时缓存一次。
    # 旧实现每次鼠标移动都同步问一次 X 服务器 winfo_rootx/rooty/width/height ——
    # 鼠标事件每秒上百个，主线程全耗在这些同步往返上，Tk 排不上重绘，
    # 表现就是"卡片发白、发花、不跟手"。这些值在拖动期间不会变。
    g = DRAG.get("geom")
    if g is None:
        g = (cards_frame.winfo_rootx(), cards_frame.winfo_rooty(),
             cards_frame.winfo_width(), cards_frame.winfo_height())
        DRAG["geom"] = g
    cf_x, cf_y, cf_w, cf_h = g
    x = evt.x_root - cf_x - DRAG["off_x"]
    y = evt.y_root - cf_y - DRAG["off_y"] - lift
    x = max(0, min(x, max(0, cf_w - DRAG["w"])))
    y = max(0, min(y, max(0, cf_h - DRAG["h"])))
    try:
        card.place_configure(x=x, y=y)
    except Exception:
        return
    sh = DRAG.get("shadow")
    if sh is not None:
        # 影子不跟着 lift 走：卡片越"高"，影子离它越远
        try:
            sh.place_configure(x=x + 5, y=y + 5 + int(lift))
        except Exception:
            pass


def _lift_step():
    """浮起动画：约 100ms 内把卡片再抬 _LIFT_MAX 像素（ease-out，约 6 帧）。

    纯粹是手感 —— 一按下就"啪"地出现在光标上方，看着像瞬移；
    抬起来这几像素就是"被捏住、提起来"的那一下。

    2026-11-01：原来是「每 16ms 抬 1px、共 7 帧」线性。7px 拆成 7 次全卡片
    place_configure，在低配机/远程桌面上每次都要重绘整张卡片，叠上鼠标事件
    就是发白卡顿。改成 ease-out 的 6 帧、每帧跨度更大：视觉上更"跟手"
    （先快后慢符合手感预期），重绘次数反而更少。
    """
    DRAG["lift_job"] = None
    if not DRAG["picked"]:
        return
    _LIFT_FRAMES = 6
    _cur = DRAG.get("lift_i", 0) + 1
    DRAG["lift_i"] = _cur
    t = min(1.0, _cur / float(_LIFT_FRAMES))
    e = 1 - (1 - t) ** 3                       # ease-out
    DRAG["lift"] = _LIFT_MAX * e
    _move_float()
    if _cur < _LIFT_FRAMES:
        DRAG["lift_job"] = root.after(_DRAG_FRAME_MS, _lift_step)


def _finish_drop(card, style0, final_keys):
    """落位收尾：卡片交回 grid、样式还原、整表按最终顺序摆一遍。"""
    DRAG["dropping"] = None
    DRAG["drop_job"] = None
    try:
        if card is not None and card.winfo_exists():
            if card.winfo_manager() == "place":
                card.place_forget()
            card.configure(style=style0 or "TLabelframe")
    except Exception:
        pass
    try:
        _regrid(final_keys)
    except Exception:
        pass


def _drop_anim(card, style0, final_keys, key):
    """落位动画：卡片从手上滑进它该去的格子（约 150ms，ease-out）。

    手机列表松手那一下也是滑回去的，不是瞬移。滑完才交回 grid ——
    place 和 grid 不能并存，所以中途不能提前 place_forget。
    """
    try:
        slot = final_keys.index(key)
    except ValueError:
        slot = DRAG["gap"]
    _layout_slots([k for k in final_keys if k != key], gap=slot)
    rect = _slot_rect(slot)
    if rect is None:
        _finish_drop(card, style0, final_keys)
        return
    x0, y0 = card.winfo_x(), card.winfo_y()
    w0, h0 = card.winfo_width(), card.winfo_height()
    x1, y1, w1, h1 = rect
    DRAG["dropping"] = card

    def step(i):
        if DRAG["dropping"] is not card:
            return                              # 被新的一次拖动抢走了
        try:
            if not card.winfo_exists():
                DRAG["dropping"] = None
                DRAG["drop_job"] = None
                return
        except Exception:
            return
        if i > _DROP_STEPS:
            _finish_drop(card, style0, final_keys)
            return
        t = i / float(_DROP_STEPS)
        e = 1 - (1 - t) ** 3                    # ease-out：先快后慢，像被"吸"进格子
        try:
            card.place_configure(x=int(x0 + (x1 - x0) * e),
                                 y=int(y0 + (y1 - y0) * e),
                                 width=int(w0 + (w1 - w0) * e),
                                 height=int(h0 + (h1 - h0) * e))
        except Exception:
            _finish_drop(card, style0, final_keys)
            return
        DRAG["drop_job"] = root.after(16, lambda: step(i + 1))

    step(1)


def _abort_drop():
    """上一张还在滑回格子，用户又按下了：立刻把它按到终点。

    不收尾就重新 _drag_start，会同时存在两张 place 着的卡片，
    影子/样式/格位全乱。
    """
    job = DRAG.get("drop_job")
    if job:
        try:
            root.after_cancel(job)
        except Exception:
            pass
        DRAG["drop_job"] = None
    card = DRAG.get("dropping")
    if card is not None:
        _finish_drop(card, DRAG.get("style0") or "", _display_keys())


def _pick_up(evt):
    """把按住的卡片从格子里抽出来，改成"浮"在其它卡片上方。

    两个必须记住的偏移：卡片相对鼠标的偏移（不然卡片会"跳"到指针底下），
    以及抽走前的宽高（place 之后不指定就会被改成控件请求尺寸，卡片会缩水）。
    """
    key = DRAG["key"]
    card = CARD_WIDGETS.get(key)
    if card is None:
        return False
    w, h = card.winfo_width(), card.winfo_height()
    DRAG["off_x"] = evt.x_root - card.winfo_rootx()
    DRAG["off_y"] = evt.y_root - card.winfo_rooty()
    DRAG["w"], DRAG["h"] = w, h
    DRAG["style0"] = str(card.cget("style") or "")
    try:
        card.grid_forget()
        # 影子：贴一张深灰的实心块在卡片右下方，制造"浮起来"的立体感。
        # tk.Frame 而不是 ttk —— 要能直接设底色、不要边框。
        sh = tk.Frame(cards_frame, bg="#A2ABB8", bd=0, highlightthickness=0)
        sh.place(x=0, y=0, width=w, height=h)
        sh.lift()
        DRAG["shadow"] = sh
        card.place(in_=cards_frame, x=0, y=0, width=w, height=h)
        card.lift()
        card.configure(style="Lifted.TLabelframe")
    except Exception as e:
        log(T("[设置] 拖动浮起失败（退回普通排序）：%s") % e)
        _back_to_grid(card)
        return False
    DRAG["picked"] = True
    DRAG["lift"] = 0.0
    DRAG["gap"] = DRAG["order"].index(key)
    _layout_slots([k for k in DRAG["order"] if k != key], gap=DRAG["gap"])
    _move_float(evt)
    DRAG["lift_job"] = root.after(16, _lift_step)
    return True


def _drag_start(evt, key):
    if BUSY.get("installing"):
        return
    _abort_drop()                              # 上一张还在落位动画里就先收尾
    job = DRAG.get("lift_job")
    if job:
        try:
            root.after_cancel(job)
        except Exception:
            pass
    DRAG["key"] = key
    DRAG["x0"] = evt.x_root
    DRAG["y0"] = evt.y_root
    DRAG["moved"] = False
    DRAG["picked"] = False
    DRAG["shadow"] = None
    DRAG["gap"] = 0
    DRAG["lift"] = 0.0
    DRAG["lift_i"] = 0                       # 浮起动画帧计数（ease-out 用）
    DRAG["lift_job"] = None
    DRAG["evt"] = evt
    DRAG["geom"] = None                  # 拖动开始时才缓存一次几何（见 _move_float）
    DRAG["order"] = _display_keys()      # 拖动期间的工作副本


def _drag_move(evt):
    key = DRAG["key"]
    if not key:
        return
    if not DRAG["moved"]:
        # 8 px 阈值：单纯点一下（想点按钮、想选中）不该把卡片拿起来
        if abs(evt.x_root - DRAG["x0"]) < 8 and abs(evt.y_root - DRAG["y0"]) < 8:
            return
        DRAG["moved"] = True
        if not _pick_up(evt):
            return
    if not DRAG["picked"]:
        return
    # 2026-11-01 性能修复（拖动发白/发花）：把原始 <Motion> 事件**合并**成每帧一次。
    # 鼠标每秒可发上百个 Motion，旧实现逐个处理：每个都要 place_configure 整张卡片，
    # 落点变化时还要 _layout_slots 重排其余全部卡片 —— 主线程被塞满，
    # Tk 排不上重绘，屏幕上就是"白块 / 花屏 / 卡顿"。
    # 合并策略：只记住最新事件，16ms 内最多重绘一次（≈60fps，肉眼足够顺）。
    DRAG["evt"] = evt
    if DRAG.get("move_job"):
        return                       # 本帧已排队，等它统一处理最新位置
    DRAG["move_job"] = root.after(_DRAG_FRAME_MS, _drag_apply_frame)


def _drag_apply_frame():
    """把合并后的最新鼠标位置一次性应用（每帧最多执行一次）。"""
    DRAG["move_job"] = None
    key = DRAG["key"]
    if not key or not DRAG["picked"]:
        return
    evt = DRAG.get("evt")
    if evt is None:
        return
    _move_float(evt)
    others = [k for k in DRAG["order"] if k != key]
    gap = _drag_target(others, evt.x_root)
    if gap != DRAG["gap"]:
        DRAG["gap"] = gap
        _layout_slots(others, gap=gap)


def _drag_end(_evt=None):
    """松手：卡片滑回空洞、写回 config.json。拖回原位则不写盘。"""
    key, moved, picked = DRAG["key"], DRAG["moved"], DRAG["picked"]
    gap, style0, shadow = DRAG["gap"], DRAG["style0"], DRAG.get("shadow")
    DRAG["key"] = None
    DRAG["moved"] = False
    DRAG["picked"] = False
    DRAG["shadow"] = None
    DRAG["evt"] = None
    DRAG["geom"] = None
    DRAG["lift"] = 0.0
    # 取消还没执行的合并帧：否则松手后它还会按旧 key 再摆一次位置
    _mj = DRAG.get("move_job")
    if _mj:
        try:
            root.after_cancel(_mj)
        except Exception:
            pass
    DRAG["move_job"] = None
    job = DRAG.get("lift_job")
    if job:
        try:
            root.after_cancel(job)
        except Exception:
            pass
        DRAG["lift_job"] = None
    if shadow is not None:
        try:
            shadow.destroy()
        except Exception:
            pass
    if not key or not moved:
        return
    card = CARD_WIDGETS.get(key)
    floating = False
    if picked and card is not None:
        try:
            floating = card.winfo_manager() == "place"
        except Exception:
            floating = False
    keys = DRAG["order"]
    if UIP is None or key not in keys:
        _regrid(keys)
        return
    new_disp = UIP.move_to(keys, key, gap)
    if floating:
        # 先滑回格子（滑完自己交回 grid），界面立刻就对了，不依赖写盘结果
        _drop_anim(card, style0, new_disp, key)
    else:
        _regrid(new_disp)
    if not picked or new_disp == _display_keys():
        return                            # 没动（或拖回原处），不碰配置
    full = _ui_order()
    vis = _ui_visible()
    if vis is None:
        vis = list(full)
    # 界面上只画得出「显示中」的槽，隐藏的不在里面 —— 只重排显示中的那几个，
    # 隐藏的原地不动（否则以后重新显示时位置全变）。
    prefs = UIP.normalize({"max_visible": _ui_max_visible(),
                           "order": UIP.merge_display_order(full, new_disp),
                           "visible": vis},
                          VERSIONS, SLOT_KEYS, MAX_SLOTS)
    err = _save_ui_prefs(prefs)
    if err:
        log(T("[设置] 拖动排序保存失败：%s") % err)
        # 别在按钮的 <ButtonRelease> 处理里销毁控件，推迟到空闲时重建
        _abort_drop()
        root.after(0, _build_cards)
        messagebox.showerror(T("保存失败"), err)
        return
    UI_CFG.clear()
    UI_CFG.update(prefs)
    if not floating:
        _regrid(new_disp)
    log(T("[设置] 拖动排序：%s") % "、".join(_display_keys()))


def _drag_end_btn(_evt=None):
    """按钮上松手：真拖过就把这次点击吞掉，别让拖完还顺带启动一个版本。

    ttk 的按钮命令是在**类绑定**里执行的，widget 级绑定先跑 ——
    返回 "break" 就轮不到它，命令不会执行。
    """
    moved = DRAG["moved"]
    _drag_end()
    return "break" if moved else None


def _bind_drag(w, key, is_btn=False):
    """给控件挂拖拽手势。

    2026-10-31 起卡片上**不再有任何"可以拖"的文字提示**（设计要求：鼠标移上去
    光标就变成四向箭头，一眼就懂，多写一行字反而啰嗦）。所以这里的 cursor
    不再只是辅助 —— 它是**唯一的可拖信号**，不能省：
    只要鼠标落在卡片空白处/标题/状态行上，指针就是 fleur（四向箭头）。

    is_btn=True 用于按钮：按钮也**能**拖（手机那种"抓哪儿都行"），但松手要自己
    接住并在拖动过的情况下吞掉点击。按钮上**不设**四向箭头光标 —— 按钮要一眼
    看出"能点"，箭头会误导；按下去拖动依然生效。
    """
    if not is_btn:
        try:
            w.configure(cursor="fleur")
        except Exception:
            pass
    else:
        # 按钮上**必须显式写 "arrow"**，不能靠默认值。Tk 的 -cursor 为空串时
        # 是「继承父控件」—— 父控件（_body / _card）是 fleur，于是按钮上也会
        # 显示四向箭头，看着像整块都能抓，用户反而分不清哪儿能点。
        # （2026-10-31 用户实测反馈：按钮上该是普通箭头。）
        try:
            w.configure(cursor="arrow")
        except Exception:
            pass
    w.bind("<ButtonPress-1>", lambda e, k=key: _drag_start(e, k), add="+")
    w.bind("<B1-Motion>", _drag_move, add="+")
    w.bind("<ButtonRelease-1>", _drag_end_btn if is_btn else _drag_end, add="+")


# 兜底：motion / release 再挂一份到 toplevel。bindtags 里每个子控件都带 toplevel
# 这一层，所以鼠标拖到别的卡片、甚至拖到日志区，事件照样能到这里 ——
# 不依赖 tk 的隐式 grab。少了这一层，一旦 grab 不生效，拖动会「看着在动、
# 松手却没落盘」，或者干脆一动不动。（_drag_move/_drag_end 对没在拖的情况直接返回，
# 所以平时多点几次按钮也不受影响。）
root.bind("<B1-Motion>", _drag_move, add="+")
root.bind("<ButtonRelease-1>", _drag_end, add="+")


# ══════════════════════════════════════════════════════════════════
# 槽体检（2026-10-31）—— **只查、只报，一个文件都不改**
# ══════════════════════════════════════════════════════════════════
# 设计要求：除「安装」「卸载」这种用户显式发起、且提前把后果讲清楚的
# 批量操作外，管理器**不单独去动槽里的文件**。槽里少了什么有可能是用户故意的
# （自己换了 node、给 dsh 打了补丁、删掉用不到的插件）—— 管理器替用户"修好"
# 等于把用户的选择覆盖掉。所以这里只做两件事：
#   ① 启动时浅查一遍（只 stat 文件，几十毫秒）：问题写日志 + 状态行打 ⚠；
#   ② 点「槽体检」做深查（读 dsh 的 package.json、逐个核对依赖、跑 node --version），
#      结果开一个**只读**窗口列出来，不提供任何"修复"按钮。
try:
    import dsh_health as HEALTH_MOD
except Exception as _e:
    HEALTH_MOD = None
    log(T("[体检] 缺少 dsh_health.py，体检功能不可用：%s") % _e)

HEALTH = {}          # 槽键 -> [(级别, 项目, 说明)]
HEALTH_GLOBAL = []   # 跨槽问题（端口冲突等）


def _health_mark(key):
    """状态行末尾那个 ⚠ 计数（没有错误/警告时返回空串）。"""
    if HEALTH_MOD is None:
        return ""
    items = HEALTH.get(key) or []
    n = sum(1 for it in items
            if it[0] in (HEALTH_MOD.LEVEL_ERROR, HEALTH_MOD.LEVEL_WARN))
    return ("  ⚠%d" % n) if n else ""


def _health_tip(key):
    """状态行 `⚠N` 的悬停说明：把 N 条到底是哪几条列出来。没有就返回空串。

    为什么要有（2026-10-31 反馈「dshV4 里的警示符号是什么意思」）：
    光一个 `⚠1` 谁也猜不出是什么，而**点开体检窗**是"重"动作 ——
    悬停能给出摘要，想看全的再点按钮。
    """
    if HEALTH_MOD is None:
        return ""
    bad = [it for it in (HEALTH.get(key) or [])
           if it[0] in (HEALTH_MOD.LEVEL_ERROR, HEALTH_MOD.LEVEL_WARN)]
    if not bad:
        return ""
    lines = [T("⚠%d = 槽体检发现 %d 个问题（只影响提示，不影响启动）：")
             % (len(bad), len(bad))]
    for lv, tag, txt in bad[:5]:
        lines.append("· [%s] %s：%s" % (lv, tag, txt))
    if len(bad) > 5:
        lines.append(T("· …还有 %d 项") % (len(bad) - 5))
    lines.append("")
    lines.append(T("点「设置 → 维护 → 槽体检」看全部（只读，不会改任何文件）"))
    return "\n".join(lines)


def _health_apply(res, dialog=False):
    """把体检结果落进 HEALTH、写日志、（可选）开结果窗。"""
    global HEALTH_GLOBAL
    HEALTH.clear()
    HEALTH_GLOBAL = []
    for key, items in (res or {}).items():
        if key == "_global":
            HEALTH_GLOBAL = list(items)
            continue
        HEALTH[key] = items

    for lv, item, msg in HEALTH_GLOBAL:
        log(T("[体检] 全局 %s：%s —— %s") % (lv, item, msg))
    bad = 0
    for key in SLOT_KEYS:
        for lv, item, msg in HEALTH.get(key) or []:
            if lv in (HEALTH_MOD.LEVEL_ERROR, HEALTH_MOD.LEVEL_WARN):
                bad += 1
                log(T("[体检] %s %s：%s —— %s") % (key, lv, item, msg))
    if not bad and not HEALTH_GLOBAL:
        log(T("[体检] %d 个槽全部正常（程序入口 / node / 数据目录 / 启动脚本都在）")
            % len(HEALTH))
    refresh(once=True)          # 只刷一次状态行，不另起定时链
    if dialog:
        _health_dialog()


def _health_dialog():
    """体检结果窗口。**默认只报告**；只有用户点「修复…」才动手（只补不改）。

    2026-11-14 用户要求把修复入口放在这里（而不是纳入对话框）—— 体检本来就是
    汇总"每个槽差在哪"的地方，在这里修最顺手。
    """
    win = tk.Toplevel(root)
    win.title(T("槽体检结果"))
    win.transient(root)
    _apply_icon(win)
    win.geometry("720x460")

    ttk.Label(win, text=T("体检结果"), font=("Microsoft YaHei UI", 11, "bold")
              ).pack(padx=16, pady=(14, 2), anchor="w")
    ttk.Label(win, foreground=_MUTED, justify="left", wraplength=680,
              text=T("这里报告「现在的样子和登记的样子差在哪」，默认**不会修改任何文件**。"
                   "\n缺 node 运行时、数据目录或启动脚本的槽，可以在下面选一个点「修复…」——"
                   "只补缺的文件，不删、不覆盖你已有的文件。"
                   "确实装坏了就走「卸载 → 重装」（那条路有完整备份）。")
              ).pack(padx=16, pady=(0, 8), anchor="w")

    frame = ttk.Frame(win)
    frame.pack(fill="both", expand=True, padx=16, pady=(0, 8))
    txt = tk.Text(frame, wrap="char", font=("Consolas", 9), relief="solid",
                  borderwidth=1, height=18)
    sb = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    txt.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    txt.tag_configure("err", foreground="#B3261E")
    txt.tag_configure("warn", foreground="#8A5A00")
    txt.tag_configure("info", foreground="#555555")
    txt.tag_configure("head", font=("Consolas", 10, "bold"))

    lines = []
    for lv, item, msg in HEALTH_GLOBAL:
        lines.append((T("%s 全局 · %s：%s\n") % (lv, item, msg),
                      "err" if lv == HEALTH_MOD.LEVEL_ERROR else "warn"))
    for key in SLOT_KEYS:
        items = HEALTH.get(key) or []
        if not items:
            continue
        head = "%s（%s）  %s\n" % (key, VC[key].get("label") or key,
                                  HEALTH_MOD.summary(items))
        lines.append((head, "head"))
        for lv, item, msg in items:
            tag = ("err" if lv == HEALTH_MOD.LEVEL_ERROR else
                   "warn" if lv == HEALTH_MOD.LEVEL_WARN else "info")
            lines.append(("    [%s] %s  %s\n" % (lv, item, msg), tag))
        # 插件清单（只读展示）：调试/插件开发时最常问的就是「这个槽到底装了啥」。
        # 只列名字和版本，不展开、不解析配置内容。
        if DRIFT_MOD is not None:
            try:
                for pl in DRIFT_MOD.plugin_lines(VC[key].get("dsh_home") or "", limit=8):
                    lines.append(("      · %s\n" % pl, "info"))
            except Exception:
                pass
        lines.append(("\n", "info"))
    if not lines:
        lines = [(T("没有可体检的槽（管理器还没管理任何 dsh 版本）。\n"), "info")]

    txt.configure(state="normal")
    for text, tag in lines:
        txt.insert("end", text, tag)
    txt.configure(state="disabled")

    def _copy():
        try:
            root.clipboard_clear()
            root.clipboard_append("".join(t for t, _ in lines))
            log(T("[体检] 结果已复制到剪贴板"))
        except Exception as e:
            log(T("[体检] 复制失败：%s") % e)

    # ── 修复（用户点了才动）─────────────────────────────────────────
    # 只列"有错误/警告"的槽。ops 层 repair_slot 只补 node 运行时与
    # .dsh-home/.tmp（缺 bin.js / 依赖会当场拒绝并说明"等于重装"）；
    # 三件 bat 由本窗口在 repair_slot 成功后补回（只补缺失、不覆盖手改过的）。
    fixable = []
    if HEALTH_MOD is not None:
        fixable = [k for k in SLOT_KEYS
                   if any(lv in (HEALTH_MOD.LEVEL_ERROR, HEALTH_MOD.LEVEL_WARN)
                          for lv, _it, _m in (HEALTH.get(k) or []))]

    if fixable:
        frow = ttk.Frame(win)
        frow.pack(fill="x", padx=16, pady=(0, 6))
        ttk.Label(frow, text=T("修复")).pack(side="left")
        _fdisp = ["%s（%s）" % (k, VC[k].get("label") or k) for k in fixable]
        _fcb = ttk.Combobox(frow, values=_fdisp, state="readonly", width=26)
        _fcb.current(0)
        _fcb.pack(side="left", padx=(6, 6))

        def _do_fix():
            i = _fcb.current()
            if i < 0 or i >= len(fixable):
                return
            key = fixable[i]
            root_ = _version_root(VC[key])
            pin_node = (not PLATFORM.IS_MAC and str(VC[key].get("node_exe") or "")
                        .strip().lower() in ("node", "node.exe"))
            if pin_node and (proc_alive(procs.get(key)) or port_open(VC[key]["port"])):
                messagebox.showwarning(T("修复未完成"), T("请先停止该槽，再固定当前 Node.js。"))
                return
            # 只读列出这个槽缺哪几件 bat（判据与 _bats_missing 一致）：
            # 确认框必须说清「到底会生成什么」，不能让用户点完才发现多了文件。
            _need = [os.path.basename(path) for path in PLATFORM.slot_launcher_paths(
                root_, VC[key].get("port")).values()]
            _missing = [n for n in _need
                        if not os.path.isfile(os.path.join(root_, n))]
            _what = "、".join(_missing) or "—"
            _node_note = T("将复制当前 PATH 中的 Node.js，并更新本槽 node_exe。") if pin_node else ""
            if not messagebox.askyesno(
                    T("修复这个槽？"),
                    T("只会**补上缺的东西**，不会删除或覆盖你已有的文件：\n"
                      "  · node 运行时、.dsh-home / .tmp 空目录\n"
                      "  · 缺失的启动脚本：%s\n\n"
                      "已经存在、或你手工改过的脚本一律保留。\n\n"
                      "%s（%s）\n%s\n\n继续？")
                    % (_what, key, VC[key].get("label") or key, root_) + "\n" + _node_note,
                    default="yes"):
                return

            def work():
                try:
                    if pin_node:
                        changed = OPS.pin_path_node(CONFIG_PATH, key, root_, INSTALL_BASE)
                        ok, acts, err = UNINST.repair_slot(
                            root_, os.path.join(root_, "node", "node.exe"))
                        if changed:
                            acts.insert(0, T("已固定当前 Node.js，并更新槽配置。"))
                    else:
                        ok, acts, err = UNINST.repair_slot(
                            root_, _pick_node_source(T("修复")))
                except Exception as e:
                    ok, acts, err = False, [], str(e)
                _post_ui(_after_fix, ok, acts, err, key)

            threading.Thread(target=work, daemon=True).start()

        def _after_fix(ok, acts, err, key):
            if not ok:
                messagebox.showerror(T("修复未完成"), err or T("原因未知"))
                return
            acts = list(acts or [])
            _restart_or_tell(T("槽列表"))
            # 2026-11-15：repair_slot 只补 node / 空目录，bat 归管理器。
            # 这里在主线程补（log / T / tkinter 不是线程安全的），且只补本槽
            # 缺失的文件 —— 全量 _rebuild_bats() 会备份并覆盖所有槽的 bat，
            # 用户手工改过的那份会被冲掉，所以修复必须用 only_missing=True。
            bat_failed = []
            if _bats_missing(key):
                try:
                    made, failed, skipped = _rebuild_bats(
                        keys=[key], only_missing=True)
                except Exception as e:
                    made, failed, skipped = [], [], [str(e)]
                if made:
                    acts.append(T("补回缺失的启动脚本（%d 件）") % len(made))
                bat_failed = list(failed) + list(skipped)
            if bat_failed:
                # 绝不能出现「node 补了、bat 没补」却弹「已修复」。
                messagebox.showerror(
                    T("修复未完成"),
                    T("已补上：%s\n\n但启动脚本没补全：\n  %s\n\n"
                      "可到「设置 → 维护」点「%s」重试。")
                    % ("、".join(acts) or "—", "\n  ".join(bat_failed),
                       _launcher_action_label()))
                return
            messagebox.showinfo(
                T("已修复"),
                T("已完成：\n  ") + "\n  ".join(acts or [T("（无需改动）")]) +
                T("\n\n重新体检一次看结果。"))
            try:
                win.destroy()
            except Exception:
                pass
            try:
                root.after(60, lambda: _run_health(deep=True, dialog=True))
            except Exception:
                pass

        ttk.Button(frow, text=T("修复…"), command=_do_fix).pack(side="left")

    btns = ttk.Frame(win)
    btns.pack(fill="x", padx=16, pady=(0, 14))
    ttk.Button(btns, text=T("复制到剪贴板"), command=_copy).pack(side="left")
    ttk.Button(btns, text=T("关闭窗口"), command=win.destroy).pack(side="right")
    win.bind("<Escape>", lambda e: win.destroy())


def _run_health(deep=False, dialog=False):
    """后台跑体检（stat 几万个文件不能让界面卡住）。"""
    if HEALTH_MOD is None:
        if dialog:
            messagebox.showerror(T("体检不可用"),
                                 T("缺少 dsh_health.py，请确认管理器目录完整。"))
        return
    if dialog:
        log(T("[体检] 开始深度体检（只读，不改任何文件）…"))

    def work():
        try:
            res = HEALTH_MOD.check_all(VC, deep=deep)
        except Exception as e:
            _post_ui(log, T("[体检] 失败：%s") % e)
            return
        _post_ui(_health_apply, res, dialog)

    threading.Thread(target=work, daemon=True).start()


# ══════════════════════════════════════════════════════════════════
# 插件 / 配置漂移检测（2026-10-31）—— 只读，只往日志写一行
# ══════════════════════════════════════════════════════════════════
# 用户拿这个管理器主要是**调试版本、开发插件、做 agent 开发**。这类工作里最常
# 遇到的是「昨天还好好的，今天怎么不行了」—— 原因往往是某个插件被升级、某个
# cordis.yml 被改。这些动作发生在 dsh 自己或终端里，管理器完全不知情，日志里
# 也就查不到线索。所以这里给每个槽拍一份指纹（插件名+版本、配置文件哈希），
# 每约 10 秒比一次，变了写一行日志：
#     [变更] v2 插件 ~dsh-cost-meter 1.8.5→1.9.0（web）
#     [变更] v2 配置 web/cordis.yml 被修改
# 只读、不碰凭据与会话、不解析配置内容 —— 细节见 dsh_drift.py 的模块说明。
try:
    import dsh_drift as DRIFT_MOD
except Exception as _e:
    DRIFT_MOD = None
    log(T("[变更] 缺少 dsh_drift.py，插件/配置变更不会记进日志：%s") % _e)

DRIFT = {}                    # 槽键 -> 上一次的指纹
_DRIFT_TICK = {"n": 0}        # refresh 计数：每 7 次（约 10 秒）比一次


def _drift_seed():
    """启动时拍一次基线，之后的变化都相对它算。"""
    if DRIFT_MOD is None:
        return
    n = 0
    for key in SLOT_KEYS:
        home = (VC[key] or {}).get("dsh_home")
        try:
            DRIFT[key] = DRIFT_MOD.snapshot(home)
            if not DRIFT[key].get("missing"):
                n += 1
        except Exception as e:
            DRIFT[key] = None
            log(T("[变更] %s 读插件清单失败（不影响使用）：%s") % (key, e))
    # 2026-11-02：零槽时这条纯属噪音（没东西可记录），不打。
    if n > 0:
        log(T("[变更] 已记录 %d 个槽的插件与配置基线（之后插件或 cordis 配置有改动会记在这里）")
            % n)


def _check_drift():
    """比对一次。只读几个小文件，开销可忽略（每 10 秒才跑一次）。"""
    if DRIFT_MOD is None:
        return
    for key in SLOT_KEYS:
        home = (VC[key] or {}).get("dsh_home")
        if not home:
            continue
        try:
            new = DRIFT_MOD.snapshot(home)
        except Exception:
            continue
        old = DRIFT.get(key)
        DRIFT[key] = new
        if not old or old.get("missing") or new.get("missing"):
            continue          # 第一次见到 / 目录刚出现，没有可比基线
        try:
            changes = DRIFT_MOD.diff(old, new)
        except Exception:
            continue
        for line in changes[:20]:
            log(T("[变更] %s %s") % (key, line))
        if len(changes) > 20:
            log(T("[变更] %s 还有 %d 条变化未列出（改动太大，建议直接看槽目录）")
                % (key, len(changes) - 20))


def _build_cards():
    """按界面显示偏好画槽卡片。改设置后调它原地重画，不必重启管理器。

    为什么能原地重画（装/卸那种改动就得重启）：显示偏好只影响「画哪几张」，
    底层 VC / procs / SIG / status_vars 一个都不动 —— status_vars 本来就是给
    **全部**槽建的，refresh() 对没显示的槽只更新 StringVar，不碰控件。
    """
    if not SLOT_KEYS:
        # 用**实时**槽数而不是启动快照 FRESH_START：运行中把最后一个槽卸载/移出后
        # FRESH_START 不一定同步到，界面会一片空白（2026-11-14 用户实测）。
        return          # 零槽交给末尾的 _build_fresh_guide()，这里不插手

    keys = _display_keys()
    _clear_cards()
    n = len(keys)
    for _c in range(n):
        cards_frame.columnconfigure(_c * 2, weight=1, uniform="card")
    for _c in range(n - 1):
        cards_frame.columnconfigure(_c * 2 + 1, weight=0, minsize=16)
    cards_frame.rowconfigure(0, weight=1)

    if not keys:
        # 「全隐藏」：有槽，但用户把显示全关了。空白界面会被当成程序坏了。
        _w = ttk.LabelFrame(cards_frame, text=T("没有显示任何版本槽"), padding=(20, 18))
        _w.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)
        ttk.Label(_w, text=T("当前设置把 %d 个版本槽全部隐藏了。\n"
                           "它们的进程、端口、数据目录都还在，只是不画卡片 —— "
                           "想启动/停止可以直接打开设置把它们显示回来。")
                           % len(SLOT_KEYS),
                  style="Card.TLabel", justify="left"
                  ).pack(anchor="w", pady=(0, 12))
        ttk.Button(_w, text=T("打开设置…"),
                   command=settings_dialog).pack(anchor="w")
        return

    for i, key in enumerate(keys):
        cfg = VC[key]
        _card = ttk.LabelFrame(cards_frame, text=_slot_title(key), padding=(12, 8),
                               labelanchor="n")
        _card.grid(row=0, column=i * 2, sticky="nsew", padx=2, pady=2)
        title_lbls[key] = _card# refresh() 会改 text
        CARD_WIDGETS[key] = _card         # 拖拽排序要按它算落点、改列号
        _body = ttk.Frame(_card, style="Card.TFrame")
        # 顶部留 4px 空白：这段空白属于 _card（挂了拖拽 + 四向箭头光标），
        # 删掉提示条后它就是卡片上方的主要"抓手"。
        # 2026-10-31 第二轮：8 → 4。用户反馈「状态行那一行高度有点高」——
        # 状态行本身只有 19px（9pt 字），显得高是因为上下各留了 8px。
        _body.pack(fill="both", expand=True, padx=2, pady=(4, 4))
        _body.columnconfigure(0, weight=1); _body.columnconfigure(1, weight=1)

        # 2026-10-31：这里原本有一条告诉用户"按住这块空白就能拖"的提示条。
        # 按要求删掉 —— 鼠标移到卡片空白处就变四向箭头，已经足够说明问题，
        # 多一行字反而占地方、每张卡片都重复一遍很啰嗦。所以整条去掉，
        # 空出来的地方给状态行（它现在在卡片最上面一行）。
        _st = ttk.Label(_body, textvariable=status_vars[key], style="Card.TLabel",
                        font=("Segoe UI", 9))
        _st.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        status_lbls[key] = _st
        # 悬停解释状态行末尾的 ⚠N（没问题时 _health_tip 返回空串 → 不弹框）
        _tip_attach(_st, lambda k=key: _health_tip(k))
        # 拖拽手势：卡片边框（含标题）+ body + 状态行。按钮**不挂光标** ——
        # 按钮要一眼看出"能点"，四向箭头会误导；但按钮仍然**能拖**（见下面的
        # is_btn=True），只是松手时会把拖动过的那一下点击吞掉。
        # tk 的事件不冒泡到父控件，所以按按钮不会误触发拖动。
        for _w in (_card, _body, _st):
            _bind_drag(_w, key)

        _b_start = ttk.Button(_body, text=T("启动"), style="Green.TButton",
                              command=lambda k=key: start_one(k))
        _b_start.grid(row=1, column=0, padx=3, pady=3, sticky="ew")
        _b_stop = ttk.Button(_body, text=T("停止"), style="Red.TButton",
                             command=lambda k=key: kill_one(k))
        _b_stop.grid(row=1, column=1, padx=3, pady=3, sticky="ew")
        _b_open = ttk.Button(_body, text=T("打开 %s") % _browser_label(cfg["browser"]),
                             command=lambda k=key: open_browser(k))
        _b_open.grid(row=2, column=0, padx=3, pady=3, sticky="ew")
        _b_copy = ttk.Button(_body, text=T("复制入口 URL"), command=lambda k=key: copy_url(k))
        _b_copy.grid(row=2, column=1, padx=3, pady=3, sticky="ew")
        # 程序目录 / 数据目录 一键直达（2026-10-01 新增）
        _root_dir = _version_root(cfg) or ""
        _b_pdir = ttk.Button(_body, text=T("程序目录"),
                             command=lambda k=key, p=_root_dir: _open_dir(p, T("程序目录")))
        _b_pdir.grid(row=3, column=0, padx=3, pady=(3, 0), sticky="ew")
        _home_dir = cfg["dsh_home"] or os.path.join(os.path.expanduser("~"), ".dsh")
        _b_ddir = ttk.Button(_body, text=T("数据目录"),
                             command=lambda k=key, p=_home_dir: _open_dir(p, T("数据目录")))
        _b_ddir.grid(row=3, column=1, padx=3, pady=(3, 0), sticky="ew")
        BUSY["card"].extend([_b_start, _b_stop, _b_open, _b_copy, _b_pdir, _b_ddir])
        # 「启动 / 停止」单独记一份：装新版本期间这两个**不冻**（2026-11-14
        # 用户问「安装的时候不能用其他的 dsh 版本么」）。安装只在新建的槽目录里
        # 跑 npm、往 config 加一条记录，不碰已有槽；装完之前新槽也不在界面上，
        # 所以冻住它们纯属过度保护。见 _actions_enabled(keep_run=)。
        BUSY["card_run"].extend([_b_start, _b_stop, _b_open, _b_copy])
        # 按钮也挂拖拽（手机那种"抓哪儿都能拖"）：松手时若真拖过，
        # _drag_end_btn 会 return "break" 把这次点击吞掉，不会误触发。
        for _b in (_b_start, _b_stop, _b_open, _b_copy, _b_pdir, _b_ddir):
            _bind_drag(_b, key, is_btn=True)

        # 卡片之间插竖向分隔线（"中间分割开"）
        if i < n - 1:
            _sep = ttk.Separator(cards_frame, orient="vertical")
            _sep.grid(row=0, column=i * 2 + 1, sticky="ns")
            CARD_SEPS.append(_sep)

# 2026-11-01（第三轮）：两个全启停按钮移到**标题栏**，紧贴「设置」左侧。
# 用户要求：主窗口只留槽 + 日志，操作按钮收到顶部一行，与「设置」同一水平线。
# 底部那一排因此整个删除。
# 为每个版本生成 启动 bat + kill bat（2026-09-30 新增）：
# 让「管理器启动」与「双击 bat 启动」两条路的 TEMP 等环境完全一致。

# ── 装官方版 / 扫已有安装（2026-10-02 新增）──────────────────────────────
# 注意：按钮创建在下面 _do_install 定义之后 —— Python 自上而下执行，
# 回调函数必须先定义好再被 ttk.Button 引用，否则这里就是 NameError，
# 而 .pyw 没有控制台，异常会被彻底吞掉（用户只看到"点了没反应"）。
# 2026-10-02 首次实现时踩过这个坑：按钮写在函数前面，程序静默退出。
# scripts/test-manager-logic.py 的 C3 组断言已锁住这个顺序。

# ══════════════════════════════════════════════════════════════════
# 功能一：启动时检测本机已有 dsh，询问是否纳入管理
# 功能二：一键安装官方版（v2 / v3 / v4）
# ══════════════════════════════════════════════════════════════════
try:
    import dsh_install_ops as OPS
except Exception:
    OPS = None

def _versions_root():
    """优先按磁盘上仍存在的已登记槽确认版本根。"""
    fallback = ""
    for _k in SLOT_KEYS:
        root = _version_root(VC[_k])
        if root:
            parent = os.path.dirname(root)
            if os.path.isdir(root):
                return parent
            fallback = fallback or parent
    # Some older setups used the versions directory itself as install_base.
    name = os.path.basename(os.path.normpath(INSTALL_BASE))
    if name.lower() == "dsh-versions" and dsh_adopt:
        if dsh_adopt.scan_managed_dirs(INSTALL_BASE, ()):
            return INSTALL_BASE
    return fallback or os.path.join(INSTALL_BASE, "dsh-versions")

def _temp_root():
    """临时目录根：所有槽的 TEMP 都落在它下面的 `<槽目录名>` 里。

    优先级（2026-10-31 起：临时文件集中管理）：
      1. config.json 顶层 `temp_root`（设置界面可改，写成 `<base>/…` 占位符）
      2. 缺省 = `<槽根>/dsh-temp` —— 也就是临时文件**跟着安装根走**：
         install_base 换到 D 盘，临时目录自动跟过去，不用改两处。
         这也修掉了老规则的怪现象：槽在 C 盘、TEMP 在 D 盘的 dsh-temp 下，
         用户完全看不出两者的关系（dshV4 里遗留的 .tmp 就是这么来的）。
    """
    raw = str(CFG.get("temp_root") or "").strip()
    if raw:
        return resolve(raw)
    # 2026-11-02：默认值从「集中式 dsh-temp」改为**各槽自己的 .tmp**。
    # 这里仍返回一个"根"，但新装槽的落点由 install_ops 用 _slot_tmp_dir() 算，
    # 不再从这个根派生。保留返回值是为了兼容仍然调用它的旧代码路径。
    return ""

def _slot_tmp_dir(slot_root):
    """某个槽的默认临时目录 = **该槽目录下的 .tmp**（2026-11-02 新规则）。

    用户要求：「把临时文件直接放进各自 dshVX 的文件夹下」，
    这样目录设置里只需要一个「安装根」，不必再单独管一个临时目录根。

    ⚠ 只对**新装 / 新纳入**的槽生效。**已装的旧槽保持原 temp_dir 不动**：
      改它要停进程、重建三件 bat、还可能影响正在跑的 dsh，
      风险远大于收益；而临时目录本来就是"丢了也没关系"的东西，
      没有必要为了统一规则去动存量环境。
    """
    if not slot_root:
        return ""
    return os.path.join(slot_root, ".tmp")


def _detached_records():
    """config 顶层 `detached`：已移出管理、但**还在磁盘上**的槽。

    只用于界面展示与「重新纳入」，**绝不参与** SLOT_KEYS / 进程 / 端口 / 卸载
    名单 —— 移出管理的定义就是「管理器不管它了」，混进来就成了假管理。

    为什么必须有这条记录：换过安装根之后，旧位置的槽不在
    `dsh_adopt.detect_existing_installs` 的扫描范围里（它只扫当前 versions_root
    和 npm 全局），没有它用户就再也自动发现不了那几个槽。

    定义放在 `_managed_roots` 之前：后者在启动快照里就会用到它。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        raw = d.get("detached")
        return [r for r in (raw or []) if isinstance(r, dict) and r.get("root")]
    except Exception:
        return []


def _detached_alive(r):
    """这条「已移出管理」记录对应的槽目录**现在**还在不在磁盘上。

    2026-11-11 用户反馈：移出 dshV2 后把目录删了，点「扫描并纳入」却还提示
    「你有 1 个版本处于已移出管理状态（dshV2）」。根因是提示、已知集合都
    直接读 config 的记录，没有以磁盘为准复核 —— 记录只是「上次放走时的存档」，
    不是事实。

    **不自动删失效记录**：目录可能是移动硬盘 / 网络盘暂时没接上，盘一回来
    这条记录又该有效了。所以只在展示与排除时忽略它，配置原样留着。
    """
    p = (r or {}).get("root") or ""
    return bool(p) and os.path.isdir(p)


def _live_detached_records():
    """仍能在磁盘上找到的「已移出管理」记录。"""
    return [r for r in _detached_records() if _detached_alive(r)]


def _stale_detached_records():
    """目录已不存在的「已移出管理」记录（只用于判活与日志记数，不参与界面展示）。"""
    return [r for r in _detached_records() if not _detached_alive(r)]


def _managed_roots():
    """「已知的」安装根（绝对路径），用于把"已经知道的东西"从检测结果里排除。

    **只算 config.json 里登记的槽**（2026-11-13 简化）。旧版把 `detached`
    记录也算进来，于是同一份目录会在「已登记」和「已移出」两个集合里各出现
    一次 —— 手动扫描（include_detached=False）时它又被当成"新发现"补回候选，
    接回时槽键已被占用，就造出了「槽 v4 顶着 dshV3 的名字、和 v3 抢 6080」
    的幽灵槽。现在判据只有一条：**这个目录此刻在不在 versions 里**。"""
    out = []
    for _k in SLOT_KEYS:
        r = _version_root(VC[_k])
        if r:
            out.append(r)
    return out


def _is_under_path(child, parent):
    """child 是否**严格位于** parent 之内（不含 parent 自身，大小写/斜杠不敏感）。

    用来判断「这个 node_modules 是不是本管理器版本根下的某个槽」：
    是 → 原地接回；不是 → 外部安装，要复制进来。
    """
    c = os.path.normcase(os.path.abspath(child or ""))
    p = os.path.normcase(os.path.abspath(parent or ""))
    return bool(c) and bool(p) and c != p and c.startswith(p + os.sep)

def _taken_ports():
    return [VC[k]["port"] for k in SLOT_KEYS if VC[k].get("port")]

def _log_orphan_shells():
    """启动时把「没登记在 config 里、且一个文件都没有」的槽目录报出来。

    为什么只报不删（2026-10-30 再次反馈「为什么又产生了 dshV1-2」）：
    `dsh-versions\\dshV1` 会莫名其妙地出现，里面只有一个空 `.dsh-home`。
    它是「装到一半 / 卸载收尾被占住」留下的占名字的空壳 —— 本身没有价值，
    但**它占着槽名**，下次装同名槽就会让位成 dshV1-2。
    功能上的危害已经由 `_reuse_empty_shell`（装之前清掉）和
    `_drop_partial_install`（装失败当场收回）堵住了；这里只是让它可见，
    方便用户自己决定要不要删。**不自动删**：这是用户主目录下的东西，
    管理器不该替用户做删除决定（万一哪天判定标准变宽就是数据事故）。
    """
    if OPS is None:
        return
    try:
        root = _versions_root()
        if not os.path.isdir(root):
            return
        managed = set(os.path.normcase(os.path.abspath(p))
                      for p in _managed_roots())
        # 临时目录根也必须排除：它缺省就在槽根下面（`<槽根>/dsh-temp`），
        # 刚建出来时只有几个空的 `<槽名>` 子目录 —— 正好命中「零文件＝空壳」
        # 这条判据，于是每次启动都会把它报成卸载残留，还建议用户删掉
        # （2026-10-31 实测：改完临时目录集中放置后立刻踩到）。
        # 判据本身不改 —— 它防的是真残留，只是这个目录天生合法且天生是空的。
        managed.add(os.path.normcase(os.path.abspath(_temp_root())))
        shells, partials, detached_dirs = [], [], []
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            if not os.path.isdir(p):
                continue
            if os.path.normcase(os.path.abspath(p)) in managed:
                continue
            try:
                if OPS._dir_is_empty_shell(p):
                    shells.append(name)
                    continue
                # 2026-11-12：未登记、有 node_modules 但缺 lib/bin.js ——
                # 上次纳入 / 安装被中断（含任务管理器强杀）留下的半成品。
                # 典型就是 dshV3-2 这种「复制到一半」的目录，几百 MB 白占盘。
                _nm = os.path.join(p, "node_modules")
                _bin = os.path.join(_nm, "@deepseek-ai", "dsh", "lib", "bin.js")
                if os.path.isdir(_nm) and not os.path.isfile(_bin):
                    partials.append(name)
                elif os.path.isfile(_bin):
                    # 2026-11-13：完整的安装、但不在 config 里 —— 典型是「移出管理」。
                    # 它**占着编号**（新装会跳过 v3 直接用 v4），日志里点名说清楚。
                    detached_dirs.append(name)
            except Exception:
                continue
        if shells:
            log(T("[清理] 发现空壳目录（没有任何文件、也不在 config 里）：%s")
                % "、".join(shells))
            log(T("[清理] 它们只是占着槽名，不会影响使用；下次装同名槽会自动复用。"
                "要清掉可手工删除：%s")
                % "、".join(os.path.join(root, n) for n in shells))
        if partials:
            log(T("[清理] 发现未完成的槽目录（不在 config 里、且缺 lib/bin.js，"
                "多半是上次纳入 / 安装被中断）：%s") % "、".join(partials))
            log(T("[清理] 它们不参与任何操作，可整个删除：%s")
                % "、".join(os.path.join(root, n) for n in partials))
        if detached_dirs:
            log(T("[编号] 这些槽目录装着完整的 dsh、但不在管理里（多半是你移出的）：%s")
                % "、".join(detached_dirs))
            log(T("[编号] 新装会跳过这些编号。可在「设置 → 版本管理」扫描并纳入。"))
    except Exception as e:
        log(T("[清理] 空壳目录检查失败（不影响使用）：%s") % e)

def _slot_dir_occupied(key):
    """槽键对应的目录（`<槽根>/dshV<KEY>`）是否已被占用。

    「占用」= 目录存在**且不是零文件空壳**。空壳不算：装之前 `_reuse_empty_shell`
    会把它清掉复用，没必要为它跳号。
    """
    try:
        p = os.path.join(_versions_root(), "dsh%s" % str(key).upper())
    except Exception:
        return False
    if not os.path.isdir(p):
        return False
    try:
        if OPS is not None and OPS._dir_is_empty_shell(p):
            return False
    except Exception:
        pass
    return True


def _free_slot_keys(used=None):
    """当前可分配的槽键（config 里没登记、且磁盘上同名目录也没被占）。

    **编号必须与目录一一对应**（2026-11-13 用户反馈「为什么会有 dshV3-2」）：
    移出管理只删 config 记录，槽目录还在磁盘上。如果只看 config 判断"空槽"，
    安装就会把 dshV3 让位成 `dshV3-2`（键仍是 v3）—— 编号与目录从此错位，
    越装越乱。现在目录还占着编号，就直接跳到下一个空编号。
    """
    used = set(SLOT_KEYS if used is None else used)
    out = []
    for i in range(1, MAX_SLOTS + 1):
        k = "v%d" % i
        if k in used or _slot_dir_occupied(k):
            continue
        out.append(k)
    return out


def _next_free_slot_key():
    """下一个可用槽键（v1..vMAX_SLOTS 里第一个既没登记、目录也没被占的）。"""
    keys = _free_slot_keys()
    return keys[0] if keys else ""


def _occupied_unmanaged_keys():
    """磁盘上有目录、但 config 里没登记的编号（移出过的、或装了一半的）。

    这些编号**不参与分配**（`_free_slot_keys` 会跳过），所以它们决定了
    「下一个新槽是 v4 而不是 v3」—— 日志里点名说清楚，编号就不再神秘。
    """
    used = set(SLOT_KEYS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            used = set((json.load(f).get("versions") or {}).keys())
    except Exception:
        pass
    return [k for k in ("v%d" % i for i in range(1, MAX_SLOTS + 1))
            if k not in used and _slot_dir_occupied(k)]


def _slot_full_reason():
    """「没有空编号」时给用户的准确原因 —— 区分 config 满了 / 磁盘目录占着编号。"""
    occ = _occupied_unmanaged_keys()
    if occ:
        return (T("没有空编号了：磁盘上 %s 这些目录还占着编号（多半是移出管理后没删）。\n"
                "想接着用就点「扫描已装 dsh 并纳入」接回来；不要了就把对应目录删掉，"
                "编号会自动空出来。")
                % "、".join("dsh%s" % k.upper() for k in occ))
    return T("槽位已满（上限 %d）") % MAX_SLOTS

class _GuiProgress(OPS.Progress if OPS else object):
    """把后台线程的进度转发到主线程的日志区 + 进度条。
    通过主线程处理的队列转发，后台线程不调用 Tk。

    `modal=True`（默认）用 grab 锁住整个管理器 —— 卸载 / 移出 / 移动这些会删或搬
    磁盘的操作必须如此。`modal=False` 用于**装新版本**：装的时候用户还想用别的
    已装版本（2026-11-14 用户提出），进度窗不该把主界面锁死。
    """
    def __init__(self, title, done_cb, modal=True):
        self.title = title
        self.done_cb = done_cb
        self.modal = modal
        self.finished = False
        self.win = tk.Toplevel(root)
        if not self.modal and BUSY["install_background"]:
            self.win.withdraw()
        self.win.title(title)
        self.win.resizable(False, False)
        # ── 自绘标题栏（2026-11-14 用户第二次反馈）────────────────────────────
        # 用户要的是「—」在**整个窗口的右上角**，跟正常窗口的最小化按钮同一个位置。
        # Windows 做不到「标题栏只留最小化」：标题栏按钮是成套的，去掉 WS_SYSMENU
        # 会连最小化一起消失（只留一个灰 X 又像程序坏了）。所以唯一的路是
        # **不要系统标题栏，自己画一条**。
        #
        # 早先试过这条路、结论是"最小化后任务栏没入口"，其实那是个可修的细节：
        # overrideredirect 让 Tk 给窗口加了 WS_EX_TOOLWINDOW，工具窗口不进任务栏。
        # 清掉它并加 WS_EX_APPWINDOW 就好了（见 _make_taskbar_window）。
        if PLATFORM.IS_WINDOWS:
            self.win.overrideredirect(True)
        _apply_icon(self.win)
        _make_taskbar_window(self.win)
        try:
            self.win.configure(bg=_APP_BG)
        except Exception:
            pass

        # 1px 边框：无边框窗口在浅色桌面上会和背景糊在一起，自己描一圈。
        _border = tk.Frame(self.win, bg=_CARD_BD)
        _border.pack(fill="both", expand=True)
        body = tk.Frame(_border, bg=_APP_BG)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        if PLATFORM.IS_WINDOWS:
            # 标题栏：标题在左、「—」在右，**同一行** —— 这才是"界面右上角"。
            self.tbar = tk.Frame(body, bg=_APP_BG, height=34)
            self.tbar.pack(fill="x")
            self.tbar.pack_propagate(False)      # 不随子控件缩，固定 34px
            self.tlabel = tk.Label(self.tbar, text=title, bg=_APP_BG, fg=_INK,
                                   font=("Segoe UI", 9), anchor="w")
            self.tlabel.pack(side="left", padx=(12, 8))
            # 「—」用 tk.Label 不用 tk.Button：Windows 上 tk.Button 是原生控件，
            # `bg` 基本不生效，会在浅灰窗口上贴出一块白补丁（用户截图就是它）。
            self.mini = tk.Label(self.tbar, text="—", bg=_APP_BG, fg=_MUTED,
                                 font=("Segoe UI", 11, "bold"), padx=14,
                                 cursor="hand2")
            # fill="y" 让整个标题栏右端都是可点区域（跟系统按钮一样好点）。
            self.mini.pack(side="right", fill="y")
            self.mini.bind("<Button-1>", self._minimize)
            # 悬停加深给一点反馈，但不写文字提示（界面不写「点这里如何如何」）。
            self.mini.bind("<Enter>", lambda e: self.mini.configure(fg=_INK))
            self.mini.bind("<Leave>", lambda e: self.mini.configure(fg=_MUTED))
            # 无边框窗口得自己实现拖动；只让标题栏能拖，跟正常窗口一致。
            for _w in (self.tbar, self.tlabel):
                _w.bind("<Button-1>", self._drag_start)
                _w.bind("<B1-Motion>", self._drag_move)
            tk.Frame(body, bg=_DIVIDER, height=1).pack(fill="x")

        self.lbl = ttk.Label(body, text=T("准备中…"), wraplength=420,
                             justify="left")
        self.lbl.pack(padx=20, pady=(14, 12))
        self.bar = ttk.Progressbar(body, mode="indeterminate", length=400)
        self.bar.pack(padx=20, pady=(0, 18))
        self.bar.start(12)
        # 最小化期间必须释放 grab，否则窗口没了、主界面还是点不动，最小化等于白做。
        # <Unmap>/<Map> 也会在窗口刚创建时各触发一次，用 try 兜住 TclError。
        self.win.bind("<Unmap>", self._on_unmap, add="+")
        self.win.bind("<Map>", self._on_map, add="+")
        # 无边框窗口本来就没有关闭按钮，Alt+F4 一般也到不了这里；留着是第二道闸。
        self.win.protocol("WM_DELETE_WINDOW", lambda: None)
        # 居中到主窗口：无边框窗口没有 WM 帮忙摆位置，得自己算。
        self._center_on_root()
        if self.modal:
            self.win.grab_set()
        else:
            if not PLATFORM.IS_WINDOWS:
                self.win.transient(root)
            self.progress_button = ttk.Button(_hdr, text=T("安装进度…"), command=self._show_progress)
            self.progress_button.pack(side="left", padx=(8, 0))
            ttk.Label(body, text=T("可继续启停和访问已装槽。更改槽位的操作会在安装结束后恢复。"),
                      wraplength=400).pack(padx=20, pady=(0, 6))
            ttk.Button(body, text=T("在后台继续"), command=self._hide_progress).pack(pady=(0, 14))
            self.win.protocol("WM_DELETE_WINDOW", self._hide_progress)
            if not BUSY["install_background"]:
                self.win.after_idle(self._show_progress)

    def _hide_progress(self):
        BUSY["install_background"] = True
        self.win.withdraw()

    def _show_progress(self):
        if self.win.winfo_exists():
            BUSY["install_background"] = False
            self.win.deiconify()
            self.win.lift()

    def _center_on_root(self):
        """把窗口摆到主窗口正中。overrideredirect 之后没有 WM 负责初始位置。"""
        try:
            self.win.update_idletasks()
            w = self.win.winfo_reqwidth()
            h = self.win.winfo_reqheight()
            rx, ry = root.winfo_rootx(), root.winfo_rooty()
            rw, rh = root.winfo_width(), root.winfo_height()
            if rw <= 1 or rh <= 1:      # 主窗口还没映射，退回屏幕居中
                rx, ry = 0, 0
                rw, rh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
            self.win.geometry("+%d+%d" % (max(0, rx + (rw - w) // 2),
                                          max(0, ry + (rh - h) // 2)))
        except Exception:
            pass

    def _drag_start(self, event):
        """记下鼠标相对窗口左上角的偏移，拖动时保持这个偏移。"""
        self._dx = event.x_root - self.win.winfo_x()
        self._dy = event.y_root - self.win.winfo_y()

    def _drag_move(self, event):
        self.win.geometry("+%d+%d" % (event.x_root - self._dx,
                                      event.y_root - self._dy))

    def _minimize(self, event=None):
        """最小化。**不能用 Tk 的 `iconify()`** —— 对 overrideredirect 窗口它直接抛
        `TclError: can't iconify: override-redirect flag is set`（Tk 认为无边框窗口
        没有任务栏表示）。只能走 Win32 `ShowWindow(SW_MINIMIZE)`。

        但 `ShowWindow` 会**同步**派发 WM_SIZE / WM_SHOWWINDOW，Tk 的窗口过程在里面
        生成 `<Unmap>` 并回调 Python —— 而 ctypes 调用期间 GIL 已经释放，这时重入
        直接 `Fatal Python error: PyEval_RestoreThread`（实测 WinDLL 崩、PyDLL 也崩，
        因为重入发生在 Tk 的 C 代码里）。

        唯一稳的办法：**调用期间把 `<Unmap>` 这个 Python 回调摘掉**，回来再挂上。
        实测（runtime python 3.10 / Win11）：IsIconic=True、state=iconic、mapped=0；
        恢复后 IsIconic=False、state=normal、mapped=1。grab 在这里手动放开 ——
        摘掉 `<Unmap>` 之后没人替我们放了。
        """
        try:
            self.win.unbind("<Unmap>")
        except Exception:
            pass
        try:
            self.win.grab_release()
        except Exception:
            pass
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.ShowWindow.restype = wintypes.BOOL
            user32.ShowWindow(wintypes.HWND(int(self.win.wm_frame(), 16)),
                              6)          # SW_MINIMIZE
        except Exception as e:
            try:
                log(T("[界面] 最小化进度窗失败：%s") % e)
            except Exception:
                pass
        finally:
            try:
                self.win.bind("<Unmap>", self._on_unmap, add="+")
            except Exception:
                pass

    def _on_unmap(self, event):
        """窗口被最小化/隐藏 → 放开 grab，让用户能回去操作主界面。"""
        if event.widget is not self.win:
            return
        try:
            self.win.grab_release()
        except Exception:
            pass

    def _on_map(self, event):
        """从任务栏收回来 → 置顶并重新抓住焦点。

        没有 `transient(root)` 之后窗口不再是"父窗口的子窗口"，z-order 得自己管，
        否则恢复出来可能被主界面盖住。
        """
        if event.widget is not self.win:
            return
        try:
            self.win.lift()
        except Exception:
            pass
        if not self.modal:
            return
        try:
            self.win.grab_set()
        except Exception:
            pass

    def on_step(self, text):
        # 先记录进度，再让主线程更新界面。
        log("[%s] %s" % (self.title, text))
        _post_ui(lambda: self.lbl.configure(text=str(text)[:180]))

    def on_done(self, ok, payload=None, error=""):
        _post_ui(self._finish, ok, payload, error)

    def _finish(self, ok, payload, error):
        if self.finished:
            return
        self.finished = True
        if not self.modal:
            self.progress_button.destroy()
        try:
            self.bar.stop()
            self.win.grab_release()
            self.win.destroy()
        except Exception:
            pass
        if self.done_cb:
            self.done_cb(ok, payload, error)


def _installed_keys_from(payload):
    """从安装/纳入的 payload 里取出本次新增的槽键列表（供 _ensure_slots_visible 用）。

    payload 的形状随调用路径不同：单装返回 {"slot_key": "v5"}，
    队列安装可能带 "keys"/"installed"/"slot_key"。这里做**宽容收集**，
    取不到就返回空列表（等于不补白名单，退回旧行为，不会误伤）。
    """
    out = []
    if not isinstance(payload, dict):
        return out
    for field in ("slot_key", "key"):
        v = payload.get(field)
        if isinstance(v, str) and v:
            out.append(v)
    for field in ("keys", "installed", "slot_keys", "added"):
        v = payload.get(field)
        if isinstance(v, (list, tuple)):
            out.extend([x for x in v if isinstance(x, str) and x])
    seen = set()
    return [k for k in out if not (k in seen or seen.add(k))]


def _relaunch_self():
    """重启管理器本体。返回 (ok, 说明)。

    2026-11-01 用户实测：卸载完弹「管理器即将重启」，但实际根本没重启。

    根因（这是关键）：
      1. `__file__` 在 pythonw / 某些打包形态下可能是空或相对路径，
         destroy() 之后再取就更容易失败 —— 而原实现把异常整个吞掉了；
      2. 原来的顺序是「先 destroy 再 startfile」，一旦 startfile 失败，
         窗口已经没了、也起不来，用户只看到程序"消失"。

    现在的做法：
      · 用运行时记录的管理器入口（MANAGER_ENTRY），而不是依赖 __file__；
      · **先尝试启动新实例，成功后再关自己** —— 起不来就保持现状并如实返回错误，
        绝不让程序凭空消失；
      · 用 subprocess 脱离父进程启动，避免被一起杀掉。
    """
    entry = ""
    try:
        entry = os.path.abspath(MANAGER_ENTRY or __file__)
    except Exception:
        try:
            entry = os.path.abspath(sys.argv[0])
        except Exception:
            entry = ""
    if not entry or not os.path.exists(entry):
        return False, T("找不到管理器入口文件：%s") % (entry or T("(未知)"))

    # 用「同一个解释器 + 脚本」重启：pythonw 下 sys.executable 就是那个 pythonw.exe
    exe = sys.executable or ""
    pyw = exe
    try:
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(cand):
            pyw = cand
    except Exception:
        pass
    if not pyw or not os.path.exists(pyw):
        return False, T("找不到 Python 解释器：%s") % (pyw or T("(未知)"))

    try:
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：子进程与当前进程脱钩，
        # 我们随后 destroy 自己不会把它一起带走。
        flags = 0
        if PLATFORM.IS_WINDOWS:
            flags = 0x00000008 | 0x00000200
        command = [pyw] if PLATFORM.FROZEN else [pyw, entry]
        if PLATFORM.IS_MAC:
            command += ["--config", CONFIG_PATH]
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
        subprocess.Popen(command, cwd=BASE_DIR,
                         creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception as e:
        if PLATFORM.IS_MAC:
            fcntl.flock(_lock_fd, fcntl.LOCK_EX)
        return False, T("启动新实例失败：%s") % e
    return True, ""


def _reload_config_after_change(new_keys=None, check_health=True):
    """config.json 变了之后，**不重启进程**，把界面原地刷新成"重启后"的样子。

    2026-11-12 用户要求：装第一个版本、加装第二个、纳入、移出、卸载之后，
    应该直接看到结果界面 —— 不要提示"即将重启"，也不要用户手动重开。

    做法：重新读 config → 重建 VC / SLOT_KEYS / SIG / status_vars / 卡片，
    并保留仍在运行的 dsh 进程引用（procs）与已抓到的入口 URL（auth_urls）。
    返回 (ok, 说明)；失败时保持旧界面不动，由调用方决定怎么提示。
    """
    global CFG, VERSIONS, SLOT_KEYS, UI_CFG, N_SLOTS, COLS, FRESH_START
    global VC, SIG, INSTALLED, INSTALL_BASE, procs, auth_urls, HEALTH
    global status_vars, DISPLAY_KEYS, _win_w

    # 新装的槽要并进 ui.visible，否则"有白名单"的配置下根本画不出来
    try:
        err = _ensure_slots_visible(new_keys or [])
        if err:
            log(T("[配置] 新槽并入显示白名单失败（不影响使用）：%s") % err)
    except Exception as e:
        log(T("[配置] 同步 ui.visible 时出错（忽略）：%s") % e)

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            new_cfg = json.load(f)
    except Exception as e:
        return False, T("重新读取 config.json 失败：%s") % e

    try:
        new_versions = new_cfg.get("versions") or {}
        if len(new_versions) > MAX_SLOTS:
            return False, T("config.json 里的槽数超过上限 %d") % MAX_SLOTS
        CFG = new_cfg
        INSTALL_BASE = PLATFORM.config_base(CFG)
        VERSIONS = new_versions
        SLOT_KEYS = list(VERSIONS.keys())
        UI_CFG = CFG.get("ui") if isinstance(CFG.get("ui"), dict) else {}
        N_SLOTS = len(SLOT_KEYS)
        COLS = N_SLOTS * 2
        FRESH_START = (N_SLOTS == 0)
        _old_vc = VC
        VC = _build_vc(VERSIONS)
        SIG = {k: _make_sig(cfg) for k, cfg in VC.items()}
        INSTALLED = {k: _installed_version(cfg) for k, cfg in VC.items()}
        # 运行中的进程 / 已抓到的 token 按槽键继承，新槽从 None 开始
        _old_procs, _old_urls = procs, auth_urls
        procs = {k: _old_procs.get(k) for k in SLOT_KEYS}
        auth_urls = {k: _old_urls.get(k) for k in SLOT_KEYS}
        HEALTH = {k: v for k, v in HEALTH.items() if k in VC and _old_vc.get(k) == VC[k]}
        DRIFT.clear()
        status_vars = {k: tk.StringVar(value="—") for k in SLOT_KEYS}
        # 探测缓存 / 状态记忆必须跟着槽键重建：新槽在 _DETECT 里没有条目时，
        # refresh() 会一直显示「检测中…」，端口号也就不出来（2026-11-12 实测 v3）。
        # 后台探测线程访问 _DETECT[key] 还会 KeyError，被 except 吞掉 → 永远补不上。
        _old_detect = dict(_DETECT)
        _DETECT.clear()
        for _k in SLOT_KEYS:
            _same_listener = all(_old_vc.get(_k, {}).get(field) == VC[_k].get(field)
                                 for field in ("port", "bin_js"))
            _DETECT[_k] = (_old_detect.get(_k) if _same_listener else None) or {"ts": 0.0}
        _old_prev = dict(_prev_state)
        _prev_state.clear()
        _prev_state.update({k: _old_prev.get(k) for k in SLOT_KEYS})
        # 日志扫描偏移也必须跟着重建：新槽没有条目时 _scan_slot() 会在
        # `_scan_pos[key]` 上 KeyError（那一行在 try 之外），而 refresh 的循环
        # 没有隔离 → 它之后的槽全部停在「—」，界面就是「这个槽没有端口号」
        # （2026-11-13 实测 v4）。新槽从 None 开始 = 从日志末尾往后扫。
        _old_scan = dict(_scan_pos)
        _scan_pos.clear()
        _scan_pos.update({k: _old_scan.get(k) for k in SLOT_KEYS})
    except Exception as e:
        return False, T("重建槽状态失败：%s") % e

    # 设置页里的槽列表已经过期，直接关掉；再点「设置」会按新配置重画
    try:
        _w = _SETTINGS_WIN.get("win")
        if _w is not None and _w.winfo_exists():
            _w.destroy()
    except Exception:
        pass

    try:
        _clear_cards()
        _build_cards()
        _build_fresh_guide()
    except Exception as e:
        log(T("[配置] 重画卡片失败：%s") % e)

    # 零槽 ↔ 有槽时窗口宽度必须跟着变（引导卡很窄，槽卡片按 240px/槽）
    try:
        _disp = _display_keys()
        _ideal = max(800, len(_disp) * _SLOT_MIN_W + 90)
        _win_w = min(_ideal, max(800, _screen_usable_w()))
        root.geometry("%dx%d" % (_win_w, max(600, root.winfo_height())))
        root.minsize(min(_win_w, 680 + len(_disp) * 40), 560)
    except Exception:
        pass

    # 新槽先同步探一次端口：别让状态行停在「检测中…」等后台线程
    # （最多 0.8s，但用户会以为"没给端口"）。只探还没有数据（ts=0）的槽。
    for _k in SLOT_KEYS:
        if not (_DETECT.get(_k) or {}).get("ts"):
            try:
                _refresh_detect_cache(_k)
            except Exception as _e:
                # 不再静默：探测失败时状态行只能走兜底文案，必须留下原因。
                log(T("[配置] 探测槽 %s 端口失败（状态行先按「已停止 :端口」显示）：%s")
                    % (_k, _e))

    _missing = [k for k in SLOT_KEYS if not (_DETECT.get(k) or {}).get("ts")]
    if _missing:
        log(T("[配置] 刷新后有 %d 个槽还没探测数据（状态行先按「已停止 :端口」显示）：%s")
            % (len(_missing), "、".join(_missing)))

    try:
        _drift_seed()
    except Exception:
        pass
    if check_health:
        try:
            _run_health(deep=False)
        except Exception:
            pass
    try:
        refresh(once=True)
    except Exception:
        pass
    # 自检：把每个槽最终的状态行文字写进日志。以后再出「没有端口号」，
    # 不用靠猜、也不用让用户复现 —— 日志里直接能看到当时到底显示了什么。
    try:
        log(T("[配置] 刷新后状态行：%s")
            % " | ".join("%s=%s" % (k, status_vars[k].get()) for k in SLOT_KEYS))
    except Exception:
        pass
    log(T("[配置] 已原地刷新界面（无需重启）：%d 个槽") % len(SLOT_KEYS))
    return True, ""


def _restart_or_tell(what=T("版本列表"), new_keys=None, check_health=True):
    """配置变了之后刷新界面：**先原地刷新，失败才退回重启进程**。

    2026-11-12 用户要求：装 / 纳入 / 移出 / 卸载之后直接显示结果界面，
    不要提示"即将重启"，也不要用户手动重开。
    重启进程只作为兜底（配置被写坏、原地重建失败等极端情况）。
    """
    ok, why = _reload_config_after_change(new_keys, check_health=check_health)
    if ok:
        return True
    log(T("[配置] 原地刷新失败：%s，尝试重启管理器") % why)
    ok2, why2 = _relaunch_self()
    if ok2:
        try:
            root.destroy()
        except Exception:
            pass
        return False
    try:
        messagebox.showwarning(
            T("已更新，但界面没能刷新"),
            T("配置已改好，但管理器没能原地刷新（%s），重启也没成功（%s）。\n\n"
            "请手动关闭本窗口并重新打开管理器，新的%s就会生效。")
            % (why, why2, what))
    except Exception:
        pass
    return False


def _adopt_loc_nm(loc):
    """定位一个候选安装的 node_modules 目录。

    detect_existing_installs 给的 `path` 是 **@deepseek-ai/dsh 包目录**，
    不是 node_modules —— 纳入要复制的是整个 node_modules，这里必须用
    `node_modules` 字段（没有才从 path 反推）。2026-10-30 修：
    之前直接把 path 当 node_modules 传下去，体检必然报「这里没有 @deepseek-ai/dsh」。
    """
    nm = (loc or {}).get("node_modules") or ""
    if nm:
        return nm
    p = (loc or {}).get("path") or ""
    if not p:
        return ""
    d = os.path.dirname(p)                      # .../@deepseek-ai
    d = os.path.dirname(d)                      # .../node_modules
    return d


def _adopt_row_info(version, loc):
    """给纳入对话框准备一行：返回 dict（ok/title/组件/问题/提醒）。"""
    nm = _adopt_loc_nm(loc)
    _root = os.path.dirname(os.path.abspath(nm)) if nm else ""
    in_place = bool(_root and _is_under_path(_root, _versions_root()))
    info = {}
    try:
        info = dsh_adopt.inspect_adopt_source(nm, in_place=in_place) if dsh_adopt else {}
    except Exception as e:
        info = {"ok": False, "problems": [T("体检失败：%s") % e], "warnings": []}
    kind = T("npm 全局") if (loc or {}).get("kind") == "npm-global" else T("版本目录")
    ver = version or info.get("version") or "?"
    dsh_dir = info.get("dsh_dir") or ""
    have = lambda rel: bool(dsh_dir) and os.path.isfile(os.path.join(dsh_dir, rel))
    comp = []
    comp.append(T("入口 ") + ("✓" if have(os.path.join("lib", "bin.js")) else "✗"))
    comp.append("package.json " + ("✓" if have("package.json") else "✗"))
    nm_count = 0
    try:
        nm_count = len([x for x in os.listdir(nm)]) if os.path.isdir(nm) else 0
    except Exception:
        nm_count = 0
    comp.append(T("依赖 %d 项%s") % (nm_count, "" if not info.get("deps_missing")
                                  else T("（缺 %d）") % len(info["deps_missing"])))
    src_home = info.get("source_home") or ""
    comp.append(T("旧数据 ") + ("✓ " + src_home if src_home else T("（无）")))
    # 2026-11-02：给普通用户一句**能看懂**的摘要；下面的逐项体检细节收进"详情"。
    _deps_missing = len(info.get("deps_missing") or [])
    if info.get("ok") and not _deps_missing:
        _summary = T("可以纳入")
    elif info.get("ok"):
        _summary = T("可以纳入（有 %d 个可选依赖缺失，一般不影响使用）") % _deps_missing
    else:
        _summary = T("这个安装不完整，暂时不能纳入")
    # 2026-11-14：告诉用户「勾了之后会发生什么」—— 版本根下的原地接回（几秒），
    # 别处的要复制一份（几百 MB）。原来只在顶部写一段笼统说明，看不出这一行是哪种。
    _action = T("原地接回") if in_place else T("复制一份")
    return {"ok": bool(info.get("ok")), "version": ver, "kind": kind, "nm": nm,
            "summary": _summary, "action": _action, "root": _root,
            "component": "  ".join(comp),
            "problems": info.get("problems") or [],
            "warnings": info.get("warnings") or [],
            "loc": loc}


def _make_autohide_scroll(canvas, sb):
    """返回一个 yscrollcommand 回调：内容不足一屏时自动隐藏滚动条。

    2026-11-02：纳入对话框里只有一个可纳入版本时，右侧那条滚动条纯属多余
    （用户反馈）。做成模块级函数而不是嵌套定义 —— 嵌套的话
    测试里的回调正则也会把它误判成"按钮回调未定义"（test C3p）。
    """
    def _cb(first, last):
        try:
            if float(first) <= 0.0 and float(last) >= 1.0:
                sb.pack_forget()
            else:
                sb.pack(side="right", fill="y")
        except Exception:
            pass
        sb.set(first, last)
    return _cb


def _search_adopt_scope():
    path = filedialog.askdirectory(
        title=T("选择要扫描的盘符、上层目录或 dsh 安装目录"), mustexist=True)
    if not path:
        return None

    win = tk.Toplevel(root)
    win.title(T("扫描已安装的 dsh"))
    win.transient(root)
    ttk.Label(win, text=T("正在扫描 %s…") % path, wraplength=460).pack(padx=20, pady=12)
    cancelled = threading.Event()
    result = []

    def cancel():
        cancelled.set()
        win.destroy()

    ttk.Button(win, text=T("取消"), command=lambda: cancel()).pack(pady=(0, 12))
    win.protocol("WM_DELETE_WINDOW", cancel)
    win.bind("<Escape>", lambda _e: cancel())
    win.grab_set()
    known = _managed_roots()

    def work():
        try:
            result.append(dsh_adopt.search_installations(path, known, cancelled.is_set))
        except Exception as exc:
            result.append(exc)

    def poll():
        if result:
            win.destroy()
        else:
            win.after(100, poll)

    threading.Thread(target=work, daemon=True).start()
    win.after(100, poll)
    win.wait_window()
    if cancelled.is_set():
        return None
    outcome = result[0]
    if isinstance(outcome, Exception):
        messagebox.showerror(T("无法扫描"), str(outcome))
        return None
    hits, inaccessible, _ = outcome
    if inaccessible:
        messagebox.showwarning(T("扫描已安装的 dsh"),
                               T("有 %d 个目录无法读取，已跳过。") % inaccessible)
    if not hits:
        messagebox.showinfo(T("没找到 dsh"), T("所选范围内没有找到未纳入的 dsh 安装。"))
    return hits


def _adopt_picker_dialog(found):
    """让用户勾选要纳入的 dsh 安装。

    返回 picked（[row]）；取消 / 直接关窗口返回 None。

    这里**只做一次选择**：勾了就纳入，没勾 / 关掉就什么都不做，也不记录
    「以后不再提示」之类的名单（2026-11-13 用户要求）。下次点「扫描已装 dsh
    并纳入」照样会重新扫出来 —— 扫描永远是当场查磁盘。
    """
    rows = []
    for b in found:
        for loc in (b.get("locations") or []):
            rows.append(_adopt_row_info(b.get("version"), loc))

    win = tk.Toplevel(root)
    win.title(T("发现已安装的 dsh"))
    win.transient(root)
    # 2026-11-14 重做尺寸：原来按「126px/行 + 210」估，只有一行时也撑到 336 高，
    # 列表下面一大片空白；宽度还写死 700，短内容显得空荡。
    # 现在按**实际内容**算：可纳入的行 34px，不可纳入的多一行原因（52px），
    # 列表最高 300px（超出滚动），窗口高度 = 头部 + 列表 + 按钮。
    _row_h = sum(54 if r["ok"] else 92 for r in rows)
    _list_h = max(34, min(300, _row_h))
    _h = 150 + _list_h
    # 2026-11-14：宽 520 时「画布 484 + 滚动条 17 + 左右各 18」= 537 > 520，
    # 右边会被窗口裁掉。改成 560，画布不再写死宽度（由窗口分配，滚动条出现也不会溢出）。
    win.geometry("560x%d" % _h)
    win.resizable(False, False)
    _apply_icon(win)
    ttk.Label(win, text=T("发现 %d 个未纳入的 dsh") % len(rows),
              font=("Microsoft YaHei UI", 10, "bold")).pack(padx=18, pady=(14, 2))
    ttk.Label(win, foreground=_MUTED, wraplength=510, justify="left",
              # 显式分行：交给 Tk 自动换行会从「几百MB」中间断开（实测很难看）。
              text=T("勾选要纳入的版本。原地接回 = 使用原目录并补齐启动脚本；\n"
                   "复制一份 = 拷进版本根（几百 MB，较慢），原安装不动。")
              ).pack(padx=18, pady=(0, 8), anchor="w")

    outer = ttk.Frame(win)
    outer.pack(padx=18, pady=(0, 6), fill="x")
    canvas = tk.Canvas(outer, highlightthickness=0, height=_list_h)
    sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    inner = ttk.Frame(canvas)
    _win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
    inner.bind("<Configure>",
               lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    # 让内层宽度**跟随**画布：否则 inner 会按内容的自然宽度撑开，
    # 长路径会把整行推出可视区（右边被切掉）。
    canvas.bind("<Configure>",
                lambda e: canvas.itemconfigure(_win_id, width=e.width))
    # 2026-11-02 用户反馈：只发现一个可纳入版本时，右侧那条滚动条是多余的。
    # 改成**按需显示**：内容装得下就藏起来，装不下才出现。
    canvas.configure(yscrollcommand=_make_autohide_scroll(canvas, sb))
    canvas.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")

    vars_ = []
    for i, r in enumerate(rows):
        # 2026-11-03 用户反馈：
        #   · 「0.2.1 前面那个框格是什么」→ 那是 ttk.LabelFrame 的边框，不是勾选框；
        #     勾选标记是它右边的 ✅。
        #   · 「版本目录几个字有什么用，删掉」→ 删掉 [版本目录]。
        # 现在：去掉外层 LabelFrame（不再画那个框），整行就是
        #   ✅ 版本号
        # 简洁、不误导。
        box = ttk.Frame(inner)
        box.pack(fill="x", pady=2, padx=2)
        v = tk.BooleanVar(value=r["ok"])       # 完好的默认勾上，坏的不勾
        # 勾选态用「✅ / 空」：原生 Checkbutton 的 ☒/☐ 看着像错误标记。
        _title = str(r["version"] or "?")
        _row = ttk.Frame(box)
        _row.pack(anchor="w")
        _mark = ttk.Label(_row, text="✅" if v.get() else "  ",
                          font=("Segoe UI", 10))
        _mark.pack(side="left", padx=(0, 6))
        _txt = ttk.Label(_row, text=_title)
        _txt.pack(side="left")
        _tag = None
        if r.get("action"):
            _tag = ttk.Label(_row, text=r["action"], foreground=_MUTED)
            _tag.pack(side="left", padx=(12, 0))

        def _toggle(_e=None, _v=v, _m=_mark, _ok=r["ok"]):
            if not _ok:
                return                      # 有问题的安装不可选
            _v.set(not _v.get())
            _m.configure(text="✅" if _v.get() else "  ")

        for _w in (_mark, _txt):
            _w.bind("<Button-1>", _toggle)
            if r["ok"]:
                _w.configure(cursor="hand2")
        if not r["ok"]:
            _mark.configure(foreground="#999")
            _txt.configure(foreground="#999")
            if _tag is not None:
                _tag.configure(text=T("不能纳入"), foreground=_RED)
        # 2026-11-03 用户要求：「纳入时里面的详情可以删掉，直接显示版本号就行」。
        # 原来有「位置 / 组件 / 依赖 N 项 / 旧数据」+ 可展开的详情区，太啰嗦。
        # 现在：
        #   · 正常可纳入 → 只显示版本号（标题行已含），不追加任何说明
        #   · **不可纳入** → 才多显示一行原因（用户需要知道为什么点不了）
        ttk.Label(box, text=r["root"], foreground=_MUTED, wraplength=460,
                  justify="left").pack(anchor="w", padx=(22, 0))
        if not r["ok"]:
            _why = (r.get("problems") or [T("安装文件不完整")])[0]
            # 换行宽度必须小于画布宽（484）减去左边缩进（22）—— 原来写 600，
            # 比画布还宽，长原因会被右边缘切掉（2026-11-14 实测）。
            ttk.Label(box, text=_why, foreground=_RED,
                      wraplength=440, justify="left"
                      ).pack(anchor="w", padx=(22, 0))
            # 2026-11-14 用户定：修复入口放在「槽体检」里（那里汇总所有槽的毛病），
            # 这里只指路，不放按钮 —— 扫描窗口只负责"要不要纳入"这一个决定。
            try:
                _can_fix = bool(r.get("root")) and _is_under_path(
                    r["root"], _versions_root())
            except Exception:
                _can_fix = False
            if _can_fix:
                ttk.Label(box, text=T("可到「设置 → 维护 → 槽体检」里点「修复…」"
                                    "（只补缺的文件，不删不改）"),
                          foreground=_MUTED, wraplength=440, justify="left"
                          ).pack(anchor="w", padx=(22, 0))
        vars_.append((v, r))

    res = {"picked": None}

    def on_ok():
        picked = [r for v, r in vars_ if v.get()]
        if not picked:
            # 2026-11-13 用户要求：这里只做「纳入 / 不纳入」的选择，
            # 不记录任何"以后不再提示"的名单 —— 所以一个都没勾就等于不纳入。
            messagebox.showwarning(T("没有勾选版本"),
                                   T("请勾选要纳入的版本；不想纳入就直接关掉这个窗口。"))
            return
        res["picked"] = picked
        win.destroy()

    def on_cancel():
        # 关窗口 / 点「取消」= 这次不纳入。什么都不记 —— 下次点扫描照样会列出来。
        res["picked"] = None
        win.destroy()

    def on_search():
        res["picked"] = "search"
        win.destroy()

    btns = ttk.Frame(win)
    btns.pack(padx=18, pady=(4, 14), fill="x")
    ttk.Button(btns, text=T("纳入勾选的"), command=on_ok).pack(side="left", padx=(0, 6))
    ttk.Button(btns, text=T("取消"), command=on_cancel).pack(side="left")

    ttk.Button(btns, text=T("扫描其他位置…"), command=lambda: on_search()).pack(side="left", padx=6)

    win.protocol("WM_DELETE_WINDOW", on_cancel)
    win.bind("<Escape>", lambda e: on_cancel())
    win.grab_set()
    win.wait_window()
    return res["picked"]


def scan_and_offer_adopt():
    """扫本机已装 dsh，让用户挑哪些纳入管理。**只在用户点按钮时调用。**

    **判据只有一条：这个目录此刻在不在 config.json 的 versions 里。**
    在 → 不出现（它已经在管）；不在且有完整安装 → 出现，勾选后纳入。

    纳入分两条路（由**位置**决定，不由记录决定）：
      · 目录在版本根下 → 就是本管理器的槽目录，**原地接回**（只改配置、不复制）；
      · 在别处（npm 全局 / 别的安装）→ **复制**一份进版本根，分配新槽键。

    2026-11-13 用户要求：**不做启动自动扫描、也不记「以后不再提示」名单**。
    扫描只在这个按钮被点的时候发生，每次都是当场查磁盘；勾了就纳入，
    没勾就什么都不做，下次点还会重新列出来。
    """
    if BUSY["installing"]:
        return
    if dsh_adopt is None:
        messagebox.showerror(T("无法扫描"), T("缺少 dsh_adopt.py 模块。"))
        return
    try:
        _known = set(os.path.normcase(os.path.abspath(p))
                     for p in _managed_roots())
        # 扫描范围写进日志：扫不到时用户最想知道的就是"你到底找了哪儿"
        # （2026-11-14 用户反馈：新机器上有 dsh，点了扫描却什么都没有）
        _npm_g = _probe_npm_global()
        _vroot = _versions_root()
        log(T("[检测] 扫描范围 —— 版本根：%s；npm 全局：%s")
            % (_vroot, _npm_g or T("（未探测到：npm 不在 PATH 上，或没装过全局包）")))
        if not _npm_g:
            log(T("[检测] npm 全局没探测到 —— 如果你的 dsh 是用 npm -g 装的，"
                  "说明 npm 不在 PATH 上；可以用「扫描其他位置…」直接选择范围"))
        found = dsh_adopt.detect_existing_installs(
            [_npm_g] if _npm_g else [],
            _vroot,
            _managed_roots())

        # 「移出过」的槽目录：只有当它 (a) 目录还在、(b) 有完整安装、
        # (c) 不在 versions 里 时才算候选。
        # (c) 是 2026-11-13 修的关键一条：v3 被移出后重装过，旧记录仍指着
        # dshV3 那个目录；旧代码把它当"新发现"补进候选，接回时槽键 v3 已被
        # 占用，于是造出「槽 v4 顶着 dshV3 的名字、和 v3 抢 6080」的幽灵槽。
        try:
            for r in _live_detached_records():
                _rt = r.get("root") or ""
                if not _rt or os.path.normcase(os.path.abspath(_rt)) in _known:
                    continue
                _dsh = os.path.join(_rt, "node_modules", "@deepseek-ai", "dsh")
                if not os.path.isfile(os.path.join(_dsh, "lib", "bin.js")):
                    continue
                _nm = os.path.join(_rt, "node_modules")
                # 已在 found 里就不重复加
                _dup = False
                for b in found:
                    for loc in (b.get("locations") or []):
                        if os.path.normcase(os.path.abspath(
                                loc.get("node_modules") or "")) == os.path.normcase(_nm):
                            _dup = True
                            break
                    if _dup:
                        break
                if _dup:
                    continue
                try:
                    import dsh_adopt as _A
                    _ver = _A.read_package_version(_dsh) or ""
                except Exception:
                    _ver = ""
                found.append({
                    "version": _ver,
                    "locations": [{"kind": "versions-dir",
                                   "path": _dsh,
                                   "node_modules": _nm}],
                })
        except Exception as _e:
            log(T("[检测] 合并已移出记录时出错（忽略）：%s") % _e)
    except Exception as e:
        log(T("[检测] 扫描已装版本失败（不影响使用）：%s") % e)
        return

    if not found:
        _stale_n = len(_stale_detached_records())
        if _stale_n:
            log(T("[检测] 忽略 %d 条目录已不存在的移出记录（config 保留，"
                  "移动盘 / 网络盘接回后仍有效）。") % _stale_n)

    while True:
        if found:
            log(T("[检测] 发现 %d 个管理范围外的版本：%s")
                % (len(found), ", ".join(b.get("version") or "?" for b in found)))
            picked = _adopt_picker_dialog(found)
            if picked != "search":
                break
        hits = _search_adopt_scope()
        if hits is None:
            return
        seen = {os.path.normcase(os.path.realpath(_adopt_loc_nm(loc)))
                for group in found for loc in group.get("locations", [])}
        for hit in hits:
            identity = os.path.normcase(os.path.realpath(hit["node_modules"]))
            if identity not in seen:
                found.append({"version": hit["version"], "locations": [hit]})
                seen.add(identity)
        if not found:
            return
    if not picked:
        return
    # 第一次纳入（一个槽都没有）→ 先确认版本放哪（纳入会把程序复制进这个根）。
    # 必须在循环之前问一次：放进循环就会为每个安装各问一遍。
    if not SLOT_KEYS and not _ask_install_root():
        return
    # 分类只看**位置**（2026-11-13）：
    #   · node_modules 的父目录在版本根下 → 这就是本管理器的槽目录，原地接回；
    #   · 在别处（npm 全局 / 别的安装）→ 复制一份进版本根，分配新槽键。
    # 旧版按「有没有移出记录」分类，于是同一个目录既可能被判成"接回"、
    # 又可能被当成"新发现"复制一遍，正是 dshV3-2 / 幽灵 v4 的来源。
    _vr = os.path.normcase(os.path.abspath(_versions_root()))
    _readd_roots, _adopt_items = [], []
    for r in picked:
        _root = os.path.dirname(os.path.abspath(_adopt_loc_nm(r["loc"])))
        if _is_under_path(_root, _vr):
            _readd_roots.append(_root)
        else:
            _adopt_items.append(r)
    def copy_next():
        if _adopt_items:
            item = _adopt_items.pop(0)
            _adopt_one(item["version"], item["loc"], on_done=copy_next)

    if _readd_roots:
        log(T("[纳入] %d 个是版本根下的槽目录，按原样接回（不复制）")
            % len(_readd_roots))
        _do_readd(_readd_roots, on_done=copy_next)
    else:
        copy_next()


def _pick_node_source(tag="纳入"):
    """挑一个可复制的 node 运行时（既有槽 → 包内 runtime → 系统 PATH）。

    抽出来是因为「复制纳入」和「原地接回时补 node」用的是同一套来源
    （2026-11-14：接回一个缺 node 的槽时也要能补上）。
    """
    node_src = ""
    try:
        # _pick_node_exe 要的是 {槽键: 配置}，不是 list —— 2026-11-12 实测
        # 传 list 会报 'list' object has no attribute 'items'。
        _existing = {_k: VC[_k] for _k in SLOT_KEYS if VC.get(_k)}
        if OPS is not None and hasattr(OPS, "_pick_node_exe"):
            node_src = OPS._pick_node_exe(_existing, INSTALL_BASE, HOME) or ""
    except Exception as e:
        log(T("[%s] 挑选 node 运行时出错（继续尝试其它来源）：%s") % (tag, e))
    if not node_src:
        for _k in SLOT_KEYS:
            n = VC[_k].get("node_exe") or ""
            if n and os.path.isfile(n):
                node_src = n
                break
    if not node_src:
        for _cand in (os.path.join(BASE_DIR, "runtime", "node", "node.exe"),
                      os.path.join(BASE_DIR, "runtime", "node", "node")):
            if os.path.isfile(_cand):
                node_src = _cand
                break
    return node_src        # "" = 没有可复制的，交给调用方决定


def _adopt_one(version, loc, on_done=None):
    """复制并登记一份安装，界面刷新后才继续队列。"""
    if OPS is None or dsh_adopt is None:
        messagebox.showerror(T("无法纳入"), T("缺少 dsh_install_ops.py / dsh_adopt.py 模块。"))
        return
    slot_key = _next_free_slot_key()
    if not slot_key:
        messagebox.showwarning(T("槽位已满"), _slot_full_reason())
        return

    node_src = _pick_node_source("纳入")
    nm_src = _adopt_loc_nm(loc)
    log(T("[纳入] 开始：%s <- %s") % (version, nm_src))
    _actions_enabled(False)

    def work():
        # Only relay steps. run_in_thread owns the GUI completion callback.
        steps = OPS.Progress()
        steps.on_step = prog.on_step
        ok, key, msg = OPS.adopt_existing(
            dsh_adopt, nm_src, _versions_root(),
            CONFIG_PATH, slot_key, _taken_ports(),
            node_exe_source=node_src, max_slots=MAX_SLOTS,
            install_base=INSTALL_BASE, temp_root=_temp_root(), progress=steps)
        return (ok, {"slot_key": key, "message": msg}, msg)

    def done(ok, payload, msg):
        _actions_enabled(True)
        msg = (payload or {}).get("message") or msg or ""
        if ok:
            log(T("[纳入] 成功：%s") % msg)
            key = payload["slot_key"]
            _restart_or_tell(T("槽列表"), [key])
            slot = VC.get(key) or {}
            messagebox.showinfo(T("纳入成功"),
                                T("%s\n\n槽：%s（%s）\n端口：%d\n数据目录：%s\n\n"
                                  "原安装未做任何改动。")
                                % (msg, key, slot.get("label", key),
                                   slot.get("port", 0), slot.get("dsh_home") or "—"))
        else:
            log(T("[纳入] 失败：%s") % msg)
            messagebox.showerror(T("纳入失败"), msg)
        if on_done:
            root.after_idle(on_done)

    prog = _GuiProgress(T("纳入 dsh %s") % version, done)
    OPS.run_in_thread(work, prog, None)


def _split_versions(raw):
    """把「0.2.0-rc.2, 0.2.1-alpha.2」或换行/空格分隔的一串拆成列表（保序去重）。"""
    out = []
    for tok in re.split(r"[,，;；\s]+", raw or ""):
        t = tok.strip()
        if t and t.lower() not in [x.lower() for x in out]:
            out.append(t)
    return out


def _next_free_slot_key_live():
    """下一个可用槽键 —— **读磁盘上的 config.json**，不看内存里的 SLOT_KEYS。

    为什么要读盘：一次装多个版本时，第一个装完会立刻把新槽写进 config.json，
    而内存里的 SLOT_KEYS 是启动时的快照（要重开窗口才更新）。拿快照算第二个槽
    会算出和第一个相同的 key，安装器只会回一句「槽名 dshV2 已在使用」。

    2026-11-13 起还要跳过「目录已被占用」的编号（见 `_free_slot_keys`）——
    否则移出过 v3 之后重装会得到 `dshV3-2`。
    """
    used = set(SLOT_KEYS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            used = set((json.load(f).get("versions") or {}).keys())
    except Exception:
        pass
    keys = _free_slot_keys(used)
    return keys[0] if keys else ""


def install_official_version():
    """安装官方版：**从列表里多选版本**（或手填，逗号/空格分隔）-> 依次装进空槽。

    2026-10-29 起对话框会列出 npm registry 上的全部已发布版本（标出最新/预览/已弃用），
    点一行即填 —— 原来只能手打版本号，记错一个字符就要白等几分钟的 npm 安装，
    还容易被误判成网络问题。手动输入框仍保留（可填 latest / next 这类 dist-tag）。

    2026-10-30 起支持**一次装多个**：列表里 Ctrl 点选 / Shift 连选，或直接填
    `0.2.0-rc.2, 0.2.1-alpha.2`。安装按填写顺序逐个进行（每个装进当时的下一个
    空槽），中途某个失败不影响后面的，全部结束后只重启一次管理器。
    """
    if BUSY["installing"]:
        return
    if OPS is None or dsh_adopt is None:
        messagebox.showerror(T("无法安装"), T("缺少 dsh_install_ops.py / dsh_adopt.py 模块。"))
        return
    # 可用槽位 = 没登记、且磁盘上没有同名目录占着的编号（见 _free_slot_keys）
    _free_keys = _free_slot_keys()
    free = len(_free_keys)
    if free <= 0:
        _occ = [k for k in ("v%d" % i for i in range(1, MAX_SLOTS + 1))
                if k not in SLOT_KEYS]
        _occ_txt = ""
        if _occ:
            _occ_txt = (T("\n\n另外，磁盘上这些编号的目录还在（多半是移出管理后没删）：%s\n"
                        "它们占着编号。想接着用就点「扫描已装 dsh 并纳入」接回来；"
                        "不要了就把对应目录删掉，编号会自动空出来。")
                        % "、".join("dsh%s" % k.upper() for k in _occ))
        messagebox.showwarning(T("槽位已满"),
                               T("管理器最多管理 %d 个版本，当前已有 %d 个：%s\n\n"
                               "要装新版本，请先用「设置 → 版本管理 → 卸载 / 移出管理」卸掉不用的槽。"
                               "%s")
                               % (MAX_SLOTS, len(SLOT_KEYS), ", ".join(SLOT_KEYS),
                                  _occ_txt))
        return

    # 第一次装（一个槽都没有）→ 先确认版本放哪。取消就什么都不做。
    if not SLOT_KEYS and not _ask_install_root():
        return

    win = tk.Toplevel(root)
    win.title(T("安装官方版 dsh"))
    win.transient(root)
    win.resizable(False, False)
    _apply_icon(win)      # 安装对话框也要有图标
    ttk.Label(win, text=T("安装官方版 dsh（一次最多 %d 个）") % free,
              font=("Microsoft YaHei UI", 10, "bold")).pack(padx=20, pady=(16, 4))
    ttk.Label(win, text=T("从下面选版本：Ctrl 点选多个 / Shift 连选；"
                        "也可以直接在下面手填。"), foreground=_MUTED
              ).pack(padx=20, pady=(0, 6))

    ver_var = tk.StringVar(value="")
    _picker = _build_version_picker(win, ver_var, height=9, width=44, multi=True)
    _picker["frame"].pack(padx=20, pady=(0, 10), fill="both", expand=True)

    ttk.Label(win, text=T("或手动输入版本号（多个用逗号 / 空格分隔；也可填 latest / next）："),
              foreground=_MUTED).pack(anchor="w", padx=20)
    ent = ttk.Entry(win, textvariable=ver_var, width=44)
    ent.pack(padx=20, pady=(2, 6), anchor="w")
    ent.focus_set()

    ttk.Label(win, text=T("将从官方 npm registry 下载安装；需要 Node.js ≥ 22.19。\n"
                        "每个版本装进一个独立的空槽（自带数据目录与端口），不影响已有版本。"),
              foreground=_MUTED, wraplength=440, justify="left"
              ).pack(padx=20, pady=(0, 12))

    def on_ok(_evt=None):
        raw = (ver_var.get() or "").strip()
        if not raw:
            messagebox.showwarning(T("还没选版本"),
                                   T("请在上面的列表里点一个版本，或在输入框里填版本号。"))
            return
        # dist-tag（latest/next）是 npm 的合法入参，不能当成"格式错误"打回去
        vers = []
        for tok in _split_versions(raw):
            if tok.lower() in ("latest", "next", "beta", "alpha"):
                vers.append(tok.lower())
                continue
            ver = dsh_adopt.normalize_version(tok)
            if not ver:
                messagebox.showerror(T("版本号格式不对"),
                                     T("「%s」不是有效的官方版本号。\n\n%s")
                                     % (tok, dsh_adopt.version_hint()))
                return
            if ver != tok:
                log(T("[安装] 版本号按官方格式修正：%s -> %s") % (tok, ver))
            if ver.lower() not in [v.lower() for v in vers]:
                vers.append(ver)
        if not vers:
            messagebox.showwarning(T("还没选版本"), T("没有解析出任何版本号。"))
            return
        if len(vers) > free:
            messagebox.showwarning(
                T("槽位装不下"),
                T("本次填了 %d 个版本，但只剩 %d 个空槽（上限 %d）。\n\n"
                "请减少几个，或先卸载不用的槽。") % (len(vers), free, MAX_SLOTS))
            return
        try:
            win.destroy()
        except Exception:
            pass
        _install_queue(vers)

    btns = ttk.Frame(win)
    btns.pack(padx=20, pady=(0, 16))
    ttk.Button(btns, text=T("安装"), style="Green.TButton",
               command=on_ok).pack(side="left", padx=4)
    ttk.Button(btns, text=T("取消"), command=win.destroy).pack(side="left", padx=4)
    ent.bind("<Return>", on_ok)
    win.bind("<Escape>", lambda e: win.destroy())
    win.grab_set()


def _install_queue(versions):
    """按顺序装多个版本，全部结束后一次性刷新界面（不重启进程）。

    为什么写成「回调里递归下一步」而不是 for 循环：每次安装都是后台线程 + 进度窗，
    主线程必须继续跑事件循环（否则窗口冻结、进度条不动）。for 循环 + 阻塞等待
    会让界面假死。
    中途失败**不中断**：失败的版本单独记下来，最后一次性告诉用户重试哪个 ——
    装 5 个版本时因为第 2 个网络抖动就整批停下，用户体验更差。
    """
    if BUSY.get("installing"):
        return
    BUSY["installing"] = True
    BUSY["install_background"] = False
    settings = _SETTINGS_WIN.get("win")
    if settings is not None and settings.winfo_exists():
        settings.destroy()
    total = len(versions)
    results = []
    log(T("[安装] 队列：共 %d 个版本 —— %s") % (total, "、".join(versions)))
    # 编号为什么跳号，当场说清楚（用户反馈「为什么会有 dshV3-2」的直接答案）
    _occ = _occupied_unmanaged_keys()
    if _occ:
        log(T("[安装] 这些编号的目录还在磁盘上、但不在管理里：%s —— 本次会跳过它们"
            "（不删你的目录；想接着用就点「扫描已装 dsh 并纳入」）")
            % "、".join("dsh%s" % k.upper() for k in _occ))

    def finish():
        BUSY["installing"] = False
        BUSY["install_background"] = False
        _actions_enabled(True)
        okr = [r for r in results if r[1]]
        badr = [r for r in results if not r[1]]
        lines = []
        for ver, ok, key, msg in results:
            if ok:
                lines.append(T("  ✓ %s → 槽 %s") % (ver, key))
            else:
                first = ((msg or "").strip() or T("原因未知")).splitlines()[0][:120]
                lines.append("  ✗ %s —— %s" % (ver, first))
        body = "\n".join(lines)
        summary = body.replace("\n", " | ")
        if badr:
            log(T("[安装] 安装结束（成功 %d / 失败 %d）：%s")
                % (len(okr), len(badr), summary))
        else:
            log(T("[安装] 安装成功（共 %d 个）：%s") % (len(okr), summary))
        if okr:
            _restart_or_tell(T("槽列表"), [r[2] for r in okr if r[2]])
        # A failed refresh can relaunch the manager.
        try:
            alive = root.winfo_exists()
        except tk.TclError:
            return
        if not alive:
            return
        if badr:
            messagebox.showwarning(
                T("安装结束（成功 %d / 失败 %d）") % (len(okr), len(badr)),
                T("%s\n\n失败的版本可以直接再点「安装官方版 dsh」重试。") % body,
                parent=root)
        else:
            messagebox.showinfo(T("安装成功"), body, parent=root)

    def step(i):
        if i >= total:
            finish()
            return
        ver = versions[i]
        key = _next_free_slot_key_live()
        if not key:
            results.append((ver, False, "", _slot_full_reason()))
            step(i + 1)
            return

        def _next(ok, msg, _v=ver, _k=key):
            results.append((_v, ok, _k, msg))
            step(i + 1)

        _do_install(ver, key, on_done=_next, index=i + 1, total=total)

    # keep_run=True：装新版本期间**保留已装槽的「启动 / 停止」** —— 安装不碰
    # 已有槽，用户完全可以边装边用别的版本（2026-11-14 用户提出）。
    _actions_enabled(False, keep_run=True)
    step(0)


def _do_install(version, slot_key, on_done=None, index=1, total=1):
    """后台装官方版。

    2026-10-30：安装不再调用外部脚本（原先那个已随旧功能删除，曾导致这里报
    [WinError 2] 找不到文件）。安装内核在 OPS._npm_install_slot。

    `on_done(ok, msg)` 非空时由调用方接管收尾（一次装多个时用：接着装下一个），
    为空则沿用单装行为 —— 弹框 + 重启管理器。
    """
    log(T("[安装] 开始：官方版 %s -> 槽 %s") % (version, slot_key))

    def done(ok, payload, msg):
        msg = (payload or {}).get("message") or msg or ""
        if ok:
            log(T("[安装] 成功：%s") % msg)
        else:
            log(T("[安装] 失败：%s") % msg.replace("\n", " | "))
        if on_done:
            on_done(ok, msg)
            return
        if ok:
            messagebox.showinfo(T("安装成功"), msg)
            _restart_or_tell(T("槽列表"), _installed_keys_from(payload))
        else:
            messagebox.showerror(T("安装失败"), msg)

    title = (T("安装官方版 %s（%d/%d）") % (version, index, total)
             if total > 1 else T("安装官方版 %s") % version)

    # 进度窗必须先建：work() 要把它传给 run_official_install，否则下载/复制那几分钟
    # 窗口会一直停在「准备中…」，看起来像卡死（2026-10-29 实测踩到）。
    prog = _GuiProgress(title, done, modal=False)

    def work():
        steps = OPS.Progress()
        steps.on_step = prog.on_step
        ok, installed_key, msg = OPS.run_official_install(
            dsh_adopt, version, CONFIG_PATH, INSTALL_BASE, slot_key,
            _taken_ports(), progress=steps, max_slots=MAX_SLOTS,
            versions_root=_versions_root(), temp_root=_temp_root())
        # The worker wrapper reports completion once, after the install returns.
        payload = {"slot_key": installed_key, "message": msg} if ok else None
        return (ok, payload, msg)

    OPS.run_in_thread(work, prog, None)


# ══════════════════════════════════════════════════════════════════
# 功能三：卸载某个 / 某几个槽（2026-10-29 新增）
# ══════════════════════════════════════════════════════════════════
# 为什么需要它：以前要卸掉一个版本，只能手动改 config.json + 自己删目录，
# 而槽目录里既有几百 MB 的 node_modules，又有用户的会话/凭据 —— 手动删极易
# 连数据一起删掉，或删错槽（两个槽的目录名只差一个数字）。
# 这里的实现刻意与 scripts/uninstall.ps1 共用同一套安全规则（见
# dsh_uninstall_ops.py 的模块注释）：停进程走 dsh_kill 门控、用户数据只备份不删、
# 只删槽根之内的条目。

def _actions_enabled(enabled, keep_run=False):
    """操作按钮的可用开关。**参数是「可用」不是「忙碌」**（名字曾是 `_set_busy`，
    按 busy 理解会把方向写反 —— 2026-11-14「全部停止后按钮全失灵」就是这么来的）。

      _actions_enabled(False) —— 长操作开始：冻结
      _actions_enabled(True)  —— 长操作结束：恢复

    为什么必须冻：这些操作会在磁盘上搬 node_modules、改 config.json，
    期间点「启动」会拉起一个半装状态的实例。冻住的按钮清单见 BUSY["btns"]。

    `keep_run=True` 时保留已装槽的启动、停止和浏览器入口（见 BUSY["card_run"]）。
    只给「装新版本」用：安装只在新建的槽目录里跑 npm、往 config 加一条记录，
    既不杀进程也不动已有槽；装完之前新槽根本不在界面上，所以用户"边装边用
    别的版本"是安全的（2026-11-14 用户提出）。卸载 / 移出 / 移动仍然全冻 ——
    那几个会删或搬磁盘上的东西，卡片按钮跟着动容易出乱子。
    """
    targets = list(BUSY["btns"]) + list(BUSY["card"])
    if keep_run and not enabled:
        targets = [b for b in targets if b not in BUSY["card_run"]]
    for b in targets:
        try:
            b.configure(state="normal" if enabled else "disabled")
        except Exception:
            pass


def _slot_brief(key):
    """一行摘要：v1（dshV1，端口 4080，实装 0.1.5-rc.2）· 运行中"""
    cfg = VC[key]
    ver = INSTALLED.get(key) or T("未知")
    running = bool(_dsh_pid_on_port(key)) or proc_alive(procs.get(key))
    return T("%s（%s，端口 %d，实装 %s）· %s") % (
        key, cfg["label"], cfg["port"], ver, T("运行中") if running else T("已停止"))


def _stop_for_uninstall(key, port):
    cfg = VC[key]
    tracked = procs.get(key)
    SLOT_PROCESS.stop_slot(cfg.get("bin_js"), port, tracked)
    for _ in range(20):
        owned, listeners = SLOT_PROCESS.slot_listeners(cfg.get("bin_js"), port)
        unknown = any(SLOT_PROCESS.process_info(pid) is None for pid in listeners)
        if (not owned and not unknown and not proc_alive(tracked)
                and (listeners or not port_open(port))):
            procs[key] = None
            return True
        time.sleep(0.25)
    raise RuntimeError(T("无法确认槽 %s 已停止。") % key)


def uninstall_versions_dialog():
    """卸载 / 移出管理：两个动作放一个窗里，因为用户面对的是同一个决定 ——
    「这几个槽我还要不要」。

    卸载（红色）**删掉**；移出管理**只改 config，磁盘一个字节都不动**，
    槽里三件 bat 全是绝对路径，移出后双击照样能启动（管理器从没装过它也一样）。
    「已移出」的槽在同一个窗下半部分列出来，可以一键重新纳入。

    卸载仍是单选（2026-10-30 起）：一次删几百 MB，多选容易连带勾错，
    而 `dshV1` 与 `dshV1-2` 只差一个后缀，确认框里根本看不出差别。
    移出不是破坏性动作，但沿用同一个单选，省得两套选择逻辑互相干扰。
    """
    if UNINST is None or OPS is None:
        messagebox.showerror(T("无法卸载"),
                             T("缺少 dsh_uninstall_ops.py / dsh_install_ops.py 模块，"
                             "请确认管理器目录完整。"))
        return
    if not SLOT_KEYS:
        # 零槽时本窗口会直接返回，但「已移出管理」按钮画在窗口里 ——
        # 用户因此进不去、接不回仍存在的旧版本。这里直接把列表打开。
        _det_live = _live_detached_records()
        if _det_live:
            messagebox.showinfo(
                T("没有可卸载的版本"),
                T("当前管理器没有管理任何 dsh 版本。\n\n"
                "不过磁盘上还有 %d 个移出过、仍可接回的版本 ——\n"
                "点「扫描已装 dsh 并纳入」就能重新发现并原样接回（不复制）。")
                % len(_det_live))
        else:
            messagebox.showinfo(
                T("没有可卸载的版本"),
                T("当前管理器没有管理任何 dsh 版本。\n\n"
                "先到「设置 → 版本管理 → 安装官方版 dsh」装一个版本，\n"
                "或用「扫描已装 dsh 并纳入」把已有的接进来。"))
        return

    win = tk.Toplevel(root)
    win.title(T("卸载 / 移出管理"))
    win.transient(root)
    win.resizable(False, False)
    _apply_icon(win)
    ttk.Label(win, text=T("卸载会删除槽目录，移出管理会保留文件"),
              font=("Microsoft YaHei UI", 10, "bold")).pack(padx=20, pady=(16, 4))

    pick = tk.StringVar(value=SLOT_KEYS[0] if SLOT_KEYS else "")
    if SLOT_KEYS:
        box = ttk.LabelFrame(win, text=T("在管的版本槽（一次一个）"), padding=(10, 6))
        box.pack(padx=20, pady=(0, 8), fill="both", expand=True)
        for key in SLOT_KEYS:
            ttk.Radiobutton(box, text=_slot_brief(key), value=key, variable=pick
                            ).pack(anchor="w", pady=1)

    backup_root = os.path.join(INSTALL_BASE, "dsh备份")
    ttk.Label(win, justify="left", wraplength=470, foreground=_MUTED,
              text=(T("卸载会先停止槽进程，备份并校验用户数据，再删除整个槽目录。\n"
                    "  备份目录：%s\n"
                    "  备份包含 .dsh-home、插件清单和版本信息。要保留自己放进槽目录的其他文件，请先复制出去。\n\n"
                    "移出管理仅取消登记，保留槽内文件和已有的启动脚本。先停止运行中的槽，"
                    "之后可通过「扫描已装 dsh 并纳入」原地接回。")
                    % backup_root)).pack(padx=20, pady=(0, 10), anchor="w")

    def on_ok():
        key = pick.get()
        if not key or key not in SLOT_KEYS:
            messagebox.showwarning(T("还没选版本"), T("请选中一个要卸载的版本槽。"))
            return
        keys = [key]
        detail = "  · " + _slot_brief(key)
        slot_dir = _version_root(VC[key])
        # 2026-11-14：槽目录里可能有用户自己放的文件（笔记/自定义插件/备份），
        # 卸载会把它们一并删掉。这里**点名列出**，别只说"会删目录"。
        _foreign = []
        try:
            _foreign = UNINST.slot_foreign_entries(slot_dir)
        except Exception:
            pass
        _extra = ""
        if _foreign:
            _shown = "、".join(_foreign[:8]) + ("…" if len(_foreign) > 8 else "")
            _extra = (T("\n\n⚠ 这个槽目录里还有 %d 个不是 dsh 装出来的条目，"
                      "卸载会**一并删除**（不进备份）：\n  %s\n"
                      "如果里面有你要的东西，请先自己拷走。")
                      % (len(_foreign), _shown))
        if not messagebox.askyesno(
                T("确认卸载"),
                T("即将卸载这个版本：\n\n%s\n\n"
                "登记的槽目录：\n%s\n\n"
                "整个槽目录会被删除 —— 包括你自己放进这个槽目录里的任何文件。\n"
                "有备份的只有：.dsh-home（会话/凭据/插件配置）、槽的 package.json 与"
                "版本信息。\n%s\n"
                "备份会放到：\n%s\n\n"
                "只想让它脱离管理、文件都留着？请按「取消」，改用「移出管理」。\n\n"
                "确定继续？") % (detail, slot_dir or "—", _extra, backup_root),
                default="no", icon="warning"):
            return
        try:
            win.destroy()
        except Exception:
            pass
        _do_uninstall(keys)

    def on_detach():
        key = pick.get()
        if not key or key not in SLOT_KEYS:
            messagebox.showwarning(T("还没选版本"), T("请选中一个要移出的版本槽。"))
            return
        if not messagebox.askyesno(
                T("确认移出管理"),
                T("即将把这个版本移出管理：\n\n  · %s\n\n"
                "磁盘上的文件一个都不会动 —— 槽目录、node_modules、node、\n"
                ".dsh-home（会话/凭据/插件配置）全都原样保留。\n\n"
                "移出后：\n"
                "  · 管理器不再显示它、也不再管它的进程；\n"
                "  · 可以使用槽里的已有启动脚本；\n"
                "  · 可从「扫描已装 dsh 并纳入」重新接回。\n\n"
                "确定继续？") % _slot_brief(key),
                default="no"):
            return
        try:
            win.destroy()
        except Exception:
            pass
        _do_detach([key])

    btns = ttk.Frame(win)
    btns.pack(padx=20, pady=(0, 16))
    ttk.Button(btns, text=T("卸载选中"), style="Red.TButton",
               command=on_ok).pack(side="left", padx=4)
    _b_det = ttk.Button(btns, text=T("移出管理（不删文件）"), command=on_detach)
    _b_det.pack(side="left", padx=4)
    if not SLOT_KEYS:
        _b_det.state(["disabled"])
    ttk.Button(btns, text=T("取消"), command=win.destroy).pack(side="left", padx=4)
    # 2026-11-12：不再有「已移出管理」独立入口 —— 同目录下的槽，点
    # 「扫描已装 dsh 并纳入」就能重新发现并原样接回（见 scan_and_offer_adopt
    # 的 _readd_roots 分流）。界面少一个概念，用户也不用理解"记录"这回事。

    win.bind("<Escape>", lambda e: win.destroy())
    win.grab_set()


def _do_uninstall(keys):
    """后台卸载若干槽。进度窗在主线程回调 done 一次（不重复调）。"""
    BUSY["uninstalling"] = set(keys)
    _actions_enabled(False)
    log(T("[卸载] 开始：%s") % ", ".join(keys))

    def done(ok, payload, msg):
        BUSY.pop("uninstalling", None)
        _actions_enabled(True)
        msg = (payload or {}).get("message") or msg or ""
        if ok:
            if (payload or {}).get("kept_in_config"):
                log(T("[卸载] 未完成：%s") % msg.replace("\n", " | "))
                messagebox.showwarning(T("卸载未完成"), msg, parent=root)
            else:
                log(T("[卸载] 完成：%s") % msg.replace("\n", " | "))
                messagebox.showinfo(T("卸载完成"), msg, parent=root)
            _restart_or_tell(T("槽列表"))
        else:
            log(T("[卸载] 未完成：%s") % msg.replace("\n", " | "))
            messagebox.showerror(T("卸载未完成"), msg or T("原因未知"), parent=root)

    # 进度窗必须先建并把 prog 传进 uninstall_slots：删 node_modules 在机械盘上
    # 要几十秒，没有 step 回调窗口就一直停在「准备中…」——2026-10-29 实测被当成
    # "卸载失败"（其实已经卸干净了）。
    prog = _GuiProgress(T("卸载 dsh 版本"), done)

    def work():
        ok, payload, msg = UNINST.uninstall_slots(
            keys, CONFIG_PATH, HOME, BASE_DIR, BASE_DIR,
            versions_root=_versions_root(),
            backup_root=os.path.join(INSTALL_BASE, "dsh备份"),
            stop_fn=_stop_for_uninstall, progress=prog)
        if isinstance(payload, dict):
            payload["message"] = msg
        return (ok, payload, msg)

    OPS.run_in_thread(work, prog, None)


def _do_detach(keys):
    """后台把若干槽「移出管理」：只改 config.json，磁盘一个字节都不动。

    与 _do_uninstall 的关键差别：不传 stop_fn（不杀进程），而是传 running_fn ——
    正在跑的槽**拒绝移出**。移出后管理器就不再跟踪它，用户会失去「停止」入口；
    卸载可以先杀再删，移出什么都不动，所以只能要求先停。"""
    _actions_enabled(False)
    log(T("[移出] 开始：%s") % ", ".join(keys))

    def done(ok, payload, msg):
        _actions_enabled(True)
        msg = (payload or {}).get("message") or msg or ""
        if ok:
            log(T("[移出] 完成：%s") % msg.replace("\n", " | "))
            messagebox.showinfo(T("已移出管理"), msg)
            _restart_or_tell(T("槽列表"))
        else:
            log(T("[移出] 未完成：%s") % msg.replace("\n", " | "))
            messagebox.showerror(T("移出未完成"), msg or T("原因未知"))

    prog = _GuiProgress(T("移出管理"), done)

    def running(key, port):
        """判据与卡片上那个「运行中」一致：端口在听 或 我们跟踪的进程还活着。"""
        try:
            return bool(port_open(port)) or proc_alive(procs.get(key))
        except Exception:
            return False

    def work():
        ok, payload, msg = UNINST.detach_slots(
            keys, CONFIG_PATH, HOME, BASE_DIR, BASE_DIR,
            versions_root=_versions_root(), progress=prog, running_fn=running)
        if isinstance(payload, dict):
            payload["message"] = msg
        return (ok, payload, msg)

    OPS.run_in_thread(work, prog, None)


def _bats_missing(key):
    cfg = VC.get(key) or {}
    root = _version_root(cfg)
    return bool(root) and any(not os.path.isfile(path)
                              for path in PLATFORM.slot_launcher_paths(
                                  root, cfg.get("port")).values())


def restore_plugins_dialog():
    """从卸载备份把插件恢复到某个槽（2026-11-28）。

    只恢复插件清单 + 配置，不恢复会话/凭据；node_modules 重新装，
    顺便验证与目标槽 dsh 版本的兼容性。
    """
    import dsh_restore_plugins as RP
    bk_root = os.path.join(INSTALL_BASE, "dsh备份")
    backups = [b for b in RP.list_backups(bk_root) if b["usable"]]
    if not backups:
        messagebox.showinfo(
            T("没有可恢复的备份"),
            T("在下面这个目录里没有找到包含插件清单的备份：\n%s\n\n"
              "备份只在「卸载」时生成（「移出管理」不备份）。") % bk_root)
        return

    win = tk.Toplevel(root)
    win.title(T("从备份恢复插件"))
    win.transient(root)
    win.resizable(False, False)

    ttk.Label(win, text=T("把某个备份里的插件装到指定槽"),
              font=("", 11, "bold")).pack(anchor="w", padx=16, pady=(14, 4))
    ttk.Label(win, foreground=_MUTED, wraplength=470, justify="left",
              text=T("只恢复插件清单和配置，不恢复会话/凭据。"
                     "插件会重新安装，用来验证与目标槽 dsh 版本是否兼容。")
              ).pack(anchor="w", padx=16, pady=(0, 10))

    f1 = ttk.Frame(win)
    f1.pack(fill="x", padx=16, pady=3)
    ttk.Label(f1, text=T("备份"), width=8).pack(side="left")
    b_labels = ["%s · %s · %s · %d 个插件" % (
        b.get("label") or b.get("slot_key") or b["name"],
        b.get("installed_version") or "?",
        b.get("backed_up_at") or "?",
        b.get("plugin_count") or 0) for b in backups]
    b_cb = ttk.Combobox(f1, values=b_labels, state="readonly", width=44)
    b_cb.current(0)
    b_cb.pack(side="left", padx=(6, 0))

    f2 = ttk.Frame(win)
    f2.pack(fill="x", padx=16, pady=3)
    ttk.Label(f2, text=T("目标槽"), width=8).pack(side="left")
    slot_labels = ["%s（%s）" % (k, VC[k].get("label") or k) for k in SLOT_KEYS]
    s_cb = ttk.Combobox(f2, values=slot_labels, state="readonly", width=44)
    if slot_labels:
        s_cb.current(0)
    s_cb.pack(side="left", padx=(6, 0))

    preview = ttk.Label(win, foreground=_MUTED, wraplength=470,
                        justify="left", text="")
    preview.pack(anchor="w", padx=16, pady=(8, 4))

    def _plan_now():
        i, j = b_cb.current(), s_cb.current()
        if i < 0 or j < 0:
            return None, None
        key = SLOT_KEYS[j]
        root_dir = _version_root(VC[key])
        if not root_dir:
            return None, key
        return RP.plan_restore(backups[i]["path"], root_dir), key

    def _refresh(*_a):
        plan, key = _plan_now()
        if key is None:
            preview.configure(text="")
        elif plan is None:
            preview.configure(text=T("这个槽没有可用的版本根目录，无法恢复。"))
        elif not plan.get("ok"):
            preview.configure(text=T("这个备份里没有 web profile 的清单。"))
        else:
            txt = T("将追加 %d 个插件、%d 个 bundle") % (
                len(plan["add_deps"]), len(plan["add_bundles"]))
            if plan["version_conflicts"]:
                txt += T("；%d 个已存在（保留当前版本）") % len(
                    plan["version_conflicts"])
            preview.configure(text=txt)

    b_cb.bind("<<ComboboxSelected>>", _refresh)
    s_cb.bind("<<ComboboxSelected>>", _refresh)
    _refresh()

    btns = ttk.Frame(win)
    btns.pack(fill="x", padx=16, pady=(10, 14))

    def on_ok():
        plan, key = _plan_now()
        if plan is None or not plan.get("ok"):
            return
        if not messagebox.askyesno(
                T("确认恢复"),
                T("将向槽 %s 追加 %d 个插件，并重新安装依赖。\n\n"
                  "目标槽原有的插件清单会先备份成 .bak-文件。\n"
                  "插件与新版本 dsh 不兼容时，安装或启动可能失败。\n\n"
                  "继续？") % (key, len(plan["add_deps"])), parent=win):
            return
        win.destroy()
        node_exe = VC[key].get("node_exe") or ""
        step_names = {"write_manifest": T("写入插件清单"),
                      "pnpm_install": T("安装依赖（可能要几分钟）")}

        def work():
            return RP.apply_restore(
                plan, node_exe,
                progress=lambda m: log(T("[恢复插件] %s")
                                       % step_names.get(m, m)))

        def done(ok, rep):
            if ok:
                body = T("已恢复 %d 个插件：%s") % (
                    len(rep["installed"]), ", ".join(rep["installed"]) or "—")
                if rep.get("conflicts"):
                    body += T("\n\n保留目标槽版本的 %d 个：%s") % (
                        len(rep["conflicts"]), ", ".join(rep["conflicts"]))
                messagebox.showinfo(T("插件已恢复"), body)
            else:
                tail = "\n".join((rep.get("log") or [])[-8:])
                messagebox.showerror(
                    T("恢复未完成"),
                    T("原因：%s\n\n%s") % (rep.get("reason") or "?", tail))

        prog = _GuiProgress(T("恢复插件"), done)
        OPS.run_in_thread(work, prog, None)

    ttk.Button(btns, text=T("取消"), command=win.destroy).pack(side="right")
    ttk.Button(btns, text=T("开始恢复"), command=on_ok).pack(
        side="right", padx=(0, 8))


def _do_readd(roots, on_done=None):
    """重新登记已移出的槽，再按每个槽的端口是否变化补齐或重建脚本。"""
    _actions_enabled(False)
    log(T("[纳入] 原地接回槽：%s") % ", ".join(roots))

    def done(ok, payload, msg):
        _actions_enabled(True)
        msg = (payload or {}).get("message") or msg or ""
        if ok:
            keys = _installed_keys_from(payload)
            if not _restart_or_tell(T("槽列表"), keys, check_health=False):
                return
            port_changed = (payload or {}).get("port_changed") or {}
            launcher_issues = []
            for key in keys:
                only_missing = key not in port_changed
                if only_missing and not _bats_missing(key):
                    continue
                try:
                    made, failed, skipped = _rebuild_bats(keys=[key], only_missing=only_missing)
                    if made:
                        log(T("[纳入] 已为槽 %s 生成 %d 个启动脚本") % (key, len(made)))
                    launcher_issues.extend(failed)
                    launcher_issues.extend(skipped)
                except Exception as e:
                    launcher_issues.append("%s: %s" % (key, e))
            _run_health(deep=False)
            log(T("[纳入] 完成：%s") % msg.replace("\n", " | "))
            if launcher_issues:
                warning = T("部分启动脚本未生成，可到「设置 → 维护」重新生成：%s") % "；".join(launcher_issues)
                log("[纳入] " + warning)
                messagebox.showwarning(T("已重新纳入"), msg + "\n\n" + warning)
            else:
                messagebox.showinfo(T("已重新纳入"), msg)
        else:
            log(T("[纳入] 未完成：%s") % msg.replace("\n", " | "))
            messagebox.showerror(T("重新纳入未完成"), msg or T("原因未知"))

        if on_done:
            root.after_idle(on_done)

    prog = _GuiProgress(T("重新纳入"), done)

    def work():
        ok, payload, msg = UNINST.readd_slots(
            roots, CONFIG_PATH, HOME, BASE_DIR, BASE_DIR,
            versions_root=_versions_root(), progress=prog,
            taken_ports=_taken_ports(), max_slots=MAX_SLOTS)
        if isinstance(payload, dict):
            payload["message"] = msg
        return (ok, payload, msg)

    OPS.run_in_thread(work, prog, None)


# ── 操作行按钮（2026-11-01 起不再有这一排）──────────────────────────────
# 用户要求初始界面只留「槽 + 日志 + 全启停」，其余动作统一收进设置。
# 2026-11-11 审计发现：当时只删了这一排，没有在设置页补入口，导致装完第一个
# 版本后再也装不了第二个、也卸不掉。现在这三个动作在「设置 → 版本管理」
# （见 settings_hub_dialog），零槽引导卡只是「安装官方版 dsh」的快捷方式。
# 注意：下面这些函数必须在**所有** command= 引用之前定义，否则 .pyw 顶层
# 求值 NameError，表现为「双击没反应」。



# ── 日志区 ─────────────────────────────────────────────────
# 2026-11-02 用户反馈：日志区没有滚动条，鼠标没有中键（或用触摸板）就没法回看历史。
# 加一条竖向滚动条（只在内容超出时才有实际作用，占位很窄不碍事）；
# 同时 enable 鼠标滚轮：Windows 上 <MouseWheel> 的 delta 是 120 的倍数。
_log_wrap = ttk.Frame(root)
_log_wrap.grid(row=2, column=0, padx=12, pady=(0, 12), sticky="nsew")
_log_wrap.rowconfigure(0, weight=1)
_log_wrap.columnconfigure(0, weight=1)

log_text = tk.Text(_log_wrap, height=10, width=80, font=("Consolas", 9), wrap="char",
                   bg="#ffffff", fg=_INK, insertbackground=_INK,
                   relief="solid", borderwidth=1,
                   highlightbackground=_CARD_BD, highlightthickness=1)
log_sb = ttk.Scrollbar(_log_wrap, orient="vertical", command=log_text.yview)
log_text.configure(yscrollcommand=log_sb.set)
log_text.grid(row=0, column=0, sticky="nsew")
log_sb.grid(row=0, column=1, sticky="ns")
log_text.configure(state="disabled")


def _log_wheel(evt):
    """滚轮滚动日志（Tk 默认不处理）。delta>0 向上。"""
    try:
        if evt.delta:
            log_text.yview_scroll(int(-1 * (evt.delta / 120)), "units")
    except Exception:
        pass


try:
    # bind 在 Text 上而不是 bind_all：避免主界面上滚轮误滚日志区
    log_text.bind("<MouseWheel>", _log_wheel)
except Exception:
    pass

# 日志着色（2026-10-31 起，2026-11-01 扩充）
# 只做行内子串判断，不跑正则 —— 日志高峰时一次刷新能来十几行，开销要小。
#
# 2026-11-01 用户问「[变更] 为什么是绿色」：那是**有意的**——`change` 绑定 _LINK（青绿），
# 表示"有东西改了，值得看一眼"。但原来只有 2 个 tag，其余全黑、彼此分不清。
# 现在按"用户要不要管"分四档，一眼能扫：
#   红  = 出错（要处理）
#   橙  = 警告（可能要处理）
#   青绿 = 变更（只是告知，不用管）
#   灰  = 噪音（kill/探测等实现细节，正常浏览时可直接跳过）
# ══════════════════════════════════════════════════════════════════
# 日志分级与着色规范（2026-11-02 定稿）
# ══════════════════════════════════════════════════════════════════
# 原则：**颜色只用来表达"要不要管"，不用来表达"发生了什么"**。
# 判定顺序 = 优先级：错误 > 警告 > 用户操作 > 细节 > 普通（无颜色）
#
#   错误  红  —— 不处理就进行不下去（启动失败 / 端口被占 / 备份校验失败）
#   警告  橙  —— 能继续，但值得知道（体检异常 / 自愈 / 搬迁后旧目录没删净）
#   操作  蓝  —— **用户手动做的事**及其结果（安装/纳入/卸载/移出/移动/设置/主题…）
#   细节  紫  —— 排障或开发才看（杀进程过程 / 权限探测 / 端口扫描）
#   普通  无  —— 状态变化、自检、变更检测（`[v1] 状态变化`、`[环境]`、`[变更]`）
#
# 2026-11-02 之前是"关键词子串匹配"（如 "可能"、"注意"），太宽：
# `[变更] 已记录 0 个槽`、`[环境] 未装 pywin32` 这类信息行被误标成彩色。
# 现在改成**按前缀判定**（见 _log_tag），关键词只作兜底。
_ERR_WORDS = ("失败", "错误", "异常", "Traceback", "ERROR", "!!")
_WARN_WORDS = ("警告", "注意", "未能", "未找到", "请勿")

# 前缀 → 级别。**用户操作用蓝**，排障细节用紫。
_LOG_USER_PREFIXES = ("[设置]", "[安装]", "[纳入]", "[卸载]", "[移出]", "[移动]",
                      "[主题]", "[全部启动]", "[全部停止]", "[清理]", "[版本列表]")
_LOG_DETAIL_PREFIXES = ("[kill]", "[kill-all]", "[权限]", "[检测]", "[fatal]")

# 两套主题各一份（深色下必须同样可读，不能沿用浅色的深色调）
_LOG_COLORS = {
    "light": {"err": "#D31E1E", "warn": "#B45309", "user": "#1D4ED8",
              "detail": "#6D28D9", "plain": "#151517"},
    "dark":  {"err": "#FF6B6B", "warn": "#F0A64A", "user": "#7BA7FF",
              "detail": "#B794F6", "plain": "#E6E7E9"},
}
_LOG_MARKS = {"err": "✕", "warn": "!", "user": "›", "detail": "·", "plain": ""}


def _log_colors():
    try:
        return _LOG_COLORS[_resolved_theme()]
    except Exception:
        return _LOG_COLORS["light"]


def _apply_log_tags():
    """把当前主题下的日志配色刷到 Text 控件上（切主题时也要调）。"""
    pal = _log_colors()
    for tag in ("err", "warn", "user", "detail", "plain"):
        try:
            log_text.tag_configure(tag, foreground=pal[tag])
        except Exception:
            pass
    try:
        log_text.tag_configure("head", font=("Consolas", 10, "bold"))
    except Exception:
        pass


_apply_log_tags()


def _log_tag(line):
    """给一行日志挑级别 tag。规则见上方「日志分级与着色规范」。

    判定顺序（先命中先返回）：
      1. 关键词命中"失败/错误/异常" → err（**优先级最高**：任何级别里出现真错误都算错误）
      2. 前缀在"用户操作"名单里     → user（蓝）
      3. 关键词命中"警告/未能…"     → warn（橙）
      4. 前缀在"排障细节"名单里     → detail（紫）
      5. 其余                        → plain（无颜色）

    为什么"操作"排在"警告"之前：用户点「卸载」失败时，那行同时含 `[卸载]`
    和"失败"——它本质是**用户动作的结果**，但既然是真失败，第 1 条已经把它
    判成 err 了。所以顺序不会让"操作失败"被误标成普通的蓝色。

    为什么按前缀而不是纯关键词：`[变更] 已记录 0 个槽`、`[环境] 未装 pywin32`
    这类信息行曾被 "可能/注意" 之类的宽关键词误标成彩色，界面很吵。
    """
    txt = line or ""
    # 去掉行首时间戳，只看正文
    body = txt
    if body.startswith("[") and "] " in body:
        body = body.split("] ", 1)[1]

    if any(w in body for w in _ERR_WORDS):
        return "err"
    if any(body.startswith(p) or (" " + p) in body for p in _LOG_USER_PREFIXES):
        return "user"
    if any(w in body for w in _WARN_WORDS):
        return "warn"
    if any(body.startswith(p) or (" " + p) in body for p in _LOG_DETAIL_PREFIXES):
        return "detail"
    # 槽日志（[v1] …）走普通色，不加视觉标记（用户选择方案 A）
    return "plain"

# Tk 控件只能在主线程碰。安装 / 纳入 / 复制的进度回调都在后台线程，
# 直接 insert 会和主线程抢 Tcl 锁 —— 实测表现为界面卡住、日志一行都不出，
# 用户只能强杀（2026-11-12 纳入 v3 卡住就是这么来的）。
_MAIN_TID = threading.get_ident()


def _gui_sink_impl(line):
    if threading.get_ident() != _MAIN_TID:
        _post_ui(_gui_sink_impl, line)
        return
    log_text.configure(state="normal")
    log_text.insert("end", line + "\n", _log_tag(line))
    log_text.see("end")
    log_text.configure(state="disabled")

_gui_sink = _gui_sink_impl

_prev_state = {k: None for k in SLOT_KEYS}

# ── 后台探测线程（2026-09-27 启动提速）────────────────────────────────────
# 曾经 port_open + _dsh_pid_on_port 在 UI 线程逐槽直跑：启动快照 = 3×(netstat+CIM)
# ≈ 6 秒全在窗口出现之前；运行中每轮 refresh 还会周期性冻住界面。
# 后台按槽核实监听者，界面刷新只读最近一次观测结果。
_DETECT = {k: {"ts": 0.0} for k in SLOT_KEYS}

def _detect_worker():
    while True:
        for key in SLOT_KEYS:
            try:
                _refresh_detect_cache(key)
            except Exception as _e:
                # 以前静默 pass：探测一直失败时，状态行就永远没有端口
                # （2026-11-12 二次反馈的排查点）。首次失败记一行，避免刷屏。
                if not (_DETECT.get(key) or {}).get("ts"):
                    log(T("[探测] %s 端口探测失败（会重试）：%s") % (key, _e))
        time.sleep(0.8)


def _log_state_change(key, prev, state, cfg, listen, ext_pid, tracked_p):
    """只在状态「类别」变化时写日志，避免每 1.5s 轮询刷屏；
    便于以后用 Agent 复盘 运行/外部运行/冲突/停止 的切换。"""
    pid = ((tracked_p.child_pid or tracked_p.pid) if proc_alive(tracked_p) else None)
    if state == "running_tracked":
        log(T("[%s] 状态变化: 运行中（本管理器启动）pid=%s 端口=%d") % (key, pid, cfg["port"]))
    elif state == "running_external":
        log(T("[%s] 状态变化: 运行中（外部/上一会话启动，本管理器未跟踪）pid=%s 端口=%d")
            % (key, ext_pid, cfg["port"]))
    elif state == "starting":
        log(T("[%s] 状态变化: 启动中 pid=%s 端口=%d") % (key, pid, cfg["port"]))
    elif state == "start_timed_out":
        log(T("[%s] 启动等候超时（%d 秒），进程仍在后台初始化。")
            % (key, SLOT_PROCESS.START_TIMEOUT))
    elif state == "start_failed":
        log(T("[%s] 启动失败：未核实该槽在端口 %d 上监听。") % (key, cfg["port"]))
    elif state == "external_conflict":
        log("[%s] %s" % (key, T("端口 %s 已被占用，无法确认归属。") % cfg["port"]))
    elif state == "stopped":
        log(T("[%s] 状态变化: 已停止 端口=%d") % (key, cfg["port"]))


def _refresh_one(key):
    """刷新**单个**槽的状态行。

    2026-11-13 抽出来：原来整段写在 refresh 的循环里，任何一个槽抛异常
    （典型是 `_scan_pos` / `_DETECT` 这类按槽键建的缓存没跟上软刷新），
    后面所有槽的状态行就都停在初始的「—」，界面看起来就是「这个槽没有端口号」。
    现在 refresh 逐个调用并用 try 兜住 —— 一个槽出错只影响它自己。
    """
    if key in BUSY.get("uninstalling", ()):
        status_vars[key].set(T("正在卸载…"))
        return
    cfg = VC[key]
    # 实装版本每次刷新轻量重读：换了版本 / 外部重装都能自动反映到标题
    ver = _installed_version(cfg)
    if ver != INSTALLED.get(key):
        INSTALLED[key] = ver
        try: title_lbls[key].config(text=_slot_title(key))
        except Exception: pass
    p = procs.get(key)
    alive = proc_alive(p)
    d = _DETECT.get(key) or {}
    ownership = d.get("ownership")
    listen, ext_pid = d.get("listen"), d.get("ext")
    prev = _prev_state.get(key)
    child = p.child if alive else None
    observed = d.get("owners", {}).get(child.pid) if child else None
    is_tracked = ownership == "owned" and observed is not None and observed.birth == child.birth
    if is_tracked:
        p.start_confirmed = True
        _scan_slot(key)
    else:
        auth_urls[key] = None
    if p and not alive:
        p.wait(timeout=0)
        procs[key] = None

    if ownership == "conflict":
        state, s = "external_conflict", T("端口 %s 已被占用，无法确认归属。") % cfg["port"]
    elif ownership == "owned":
        state = "running_tracked" if is_tracked else "running_external"
        s = T("运行中  pid=%s  :%d  ✓") % (p.child_pid if is_tracked else ext_pid, cfg["port"])
    elif alive and not p.start_confirmed:
        if time.monotonic() - p.launched_at >= SLOT_PROCESS.START_TIMEOUT:
            state, s = "start_timed_out", T("启动等候超时  :%d（仍在后台）") % cfg["port"]
        else:
            state, s = "starting", T("启动中  pid=%s  :%d  …") % (p.pid, cfg["port"])
    elif (p and not p.start_confirmed) or (p is None and prev == "start_failed"):
        state, s = "start_failed", T("启动失败  :%d（查看日志）") % cfg["port"]
    else:
        # 即使还没有探测数据，也先显示已配置的端口。
        state, s = "stopped", T("已停止  :%d") % cfg["port"]
    # 状态类别变化才写日志（避免每 1.5s 刷屏）
    if state != prev:
        _log_state_change(key, prev, state, cfg, listen, ext_pid, p)
        _prev_state[key] = state
    status_vars[key].set(s + _health_mark(key))
    # 状态文字按运行态上色（绿=运行 / 琥珀=启动中 / 红=冲突 / 灰=停止）
    try:
        status_lbls[key].configure(foreground=_STATUS_COLORS.get(state, _MUTED))
    except Exception:
        pass
def refresh(once=False):
    """刷新所有槽的状态行。once=True 表示只刷这一次、不另起定时链
    （体检出结果后要立刻把 ⚠ 显示出来，但不能再排一条 after 循环）。"""
    for key in SLOT_KEYS:
        try:
            _refresh_one(key)
        except Exception as e:
            log(T("[%s] 状态刷新出错（只跳过这一个槽）：%s") % (key, e))
    # 插件/配置漂移：每 7 次刷新（约 10 秒）比对一次，只读几个小文件
    _DRIFT_TICK["n"] += 1
    if _DRIFT_TICK["n"] % 7 == 0:
        _check_drift()
    if not once:
        root.after(_refresh_interval_ms(), refresh)


# ── 自适应轮询间隔（2026-11-01）─────────────────────────────────────
# 窗口在前台时 1.5s（要立刻看到"运行中/已停止"的变化）；
# 切到后台（失焦）时放慢到 6s、最小化时 10s —— 用户看不见的时候没必要高频
# 去问 tasklist / netstat。4 个槽时每轮要跑 4×2 次系统调用，后台减负很明显。
_UI_AWAKE = {"focused": True, "iconified": False}
_POLL_FAST_MS = 1500
_POLL_SLOW_MS = 6000
_POLL_IDLE_MS = 10000


def _refresh_interval_ms():
    """按窗口当前状态挑轮询间隔（单位毫秒）。"""
    if _UI_AWAKE.get("iconified"):
        return _POLL_IDLE_MS
    if _UI_AWAKE.get("focused"):
        return _POLL_FAST_MS
    return _POLL_SLOW_MS


def _on_focus_in(_evt=None):
    _UI_AWAKE["focused"] = True
    _UI_AWAKE["iconified"] = False
    refresh(once=True)          # 回到前台立刻刷一次，不用等下一拍


def _on_focus_out(_evt=None):
    _UI_AWAKE["focused"] = False


def _on_iconify(_evt=None):
    # 只认主窗口自己的 <Unmap>：子控件的 Map/Unmap 也会冒泡到这里，
    # 若不判断就会拿子控件的事件去改主窗口状态（之前的写法有这个隐患）。
    try:
        if _evt is not None and _evt.widget is not root:
            return
    except Exception:
        pass
    _UI_AWAKE["iconified"] = True


def _on_deiconify(_evt=None):
    """最小化恢复 / 窗口重新映射。

    2026-11-01 修复「最小化再打开画面变黑」：
    Tk 在 Windows 上从最小化恢复时，客户端区**不会自动重绘**——窗口先被画成
    一片黑，要等下一次内容变化才补上。以前这里只 refresh 数据，不强制重绘，
    于是用户看到的就是一块黑的卡片区。

    修法两步：
      ① 先判断主窗口当前 wm_state，确认真的恢复成 normal 了才处理
         （<Map> 会在子控件上也触发，光凭事件不可靠）；
      ② 用 update_idletasks 把待处理的几何/绘制一次性做完，再显式刷新卡片区，
         相当于给窗口一个"现在请你重画"的指令。
    """
    try:
        if _evt is not None and _evt.widget is not root:
            return
    except Exception:
        pass
    try:
        if str(root.wm_state()) == "iconic":
            return                      # 还在最小化，别急着当成已恢复
    except Exception:
        pass
    _UI_AWAKE["iconified"] = False
    _UI_AWAKE["focused"] = True
    _force_repaint()
    refresh(once=True)


def _force_repaint():
    """让主窗口立刻把自己重画一遍（最小化恢复后防黑屏 / 防白块）。

    为什么不是简单 update()：update() 会处理所有事件（含用户输入），
    在焦点/映射回调里重入风险大。这里只做**几何 + 绘制**两件事。

    注意：cards_frame 是 ttk.Frame，**没有 bg 选项**（ttk 的底色走 style），
    所以不能对它 cget("bg")。改用两条对 tk/ttk 都安全的手段：
      ① update_idletasks()  —— 把待处理的几何/绘制一次性做完；
      ② 对每个卡片调 winfo_ismapped + place/grid 的空操作，
         再整体 event_generate("<Configure>") 触发一次重排重绘。
    """
    try:
        root.update_idletasks()
    except Exception:
        pass
    try:
        # 窗口从最小化恢复后，客户区可能仍是"未初始化"的一整块。
        # 触发一次 <Configure>：Tk 会按当前几何重新布局并重绘整个子控件树，
        # 不重建任何控件、不改任何数据。
        root.event_generate("<Configure>", when="now")
    except Exception:
        pass
    try:
        root.update_idletasks()
    except Exception:
        pass

def _log_state_snapshot():
    """启动快照：把当前各槽端口 / 运行态 / 外部 dsh 进程一次性写进日志，
    便于以后用 Agent 复盘「管理器重启时 DSH 在不在跑、谁在跑」。
    2026-09-27 改为消费后台探测线程（_DETECT）的现成结果 —— 以前在这里逐槽
    直跑 netstat+CIM，窗口出现前白卡 6 秒；现在由调度方在探测 warmed 后再调。"""
    log(T("=== 启动快照：各槽运行态 ==="))
    for key in SLOT_KEYS:
        cfg = VC[key]
        d = _DETECT.get(key) or {}
        listen, ext = d.get("listen"), d.get("ext")
        tracked = procs.get(key)
        if proc_alive(tracked):
            who = T("本管理器跟踪 pid=%s") % tracked.pid
        elif ext is not None:
            who = T("外部运行 pid=%s") % ext
        elif listen:
            who = T("端口被非 dsh 进程占用")
        else:
            who = T("未运行")
        log(T("[%s] 端口=%d 监听=%s 判定=%s") % (key, cfg["port"], listen, who))
    log(T("=== 启动快照结束 ==="))
    _log_orphan_shells()

def on_close():
    log(T("=== 管理器关闭 (pid=%d) ===") % os.getpid())
    # 显式保留所有运行中的 dsh：关闭管理器【不会】连带关闭它启动 / 跟踪的 dsh。
    # 原因：subprocess.Popen 默认不绑定进程组，pythonw 无控制台也不会向子进程发
    # Ctrl-C，子进程在父进程退出后继续存活。这里只做日志确认，便于以后排查。
    for key in SLOT_KEYS:
        cfg = VC[key]
        ext = _dsh_pid_on_port(key)
        tracked = procs.get(key)
        if proc_alive(tracked):
            log(T("[%s] 关闭时保留运行中的 dsh（本管理器跟踪 pid=%s 端口=%d）") % (key, tracked.pid, cfg["port"]))
        elif ext is not None:
            log(T("[%s] 关闭时保留运行中的 dsh（外部 pid=%s 端口=%d）") % (key, ext, cfg["port"]))
        else:
            log(T("[%s] 关闭时无运行中的 dsh（端口=%d）") % (key, cfg["port"]))
    try:
        if LOG_FH:
            LOG_FH.close()
    except Exception:
        pass
    root.destroy()

def _request_close():
    if BUSY["installing"]:
        messagebox.showinfo(T("正在安装 dsh"),
                            T("请等安装结束后再退出管理器。可点安装进度窗的「在后台继续」。"),
                            parent=root)
        return
    on_close()


root.protocol("WM_DELETE_WINDOW", _request_close)
if PLATFORM.IS_MAC:
    root.createcommand("tk::mac::Quit", _request_close)

# 启动日志
log(T("=== 管理器启动 pid=%d ===") % os.getpid())

# 权限自检：用**真实能力探测**代替 IsUserAnAdmin() 代理判断
# ---------------------------------------------------------------------------
# 旧实现只看 IsUserAnAdmin()（= 当前令牌是否已提权）。在本机这种
# 「账户属于 Administrators 组、但双击未提权，而要管的进程又同属本用户」的情形下
# 它会**误报**：实测两项能力都好用（CIM 读到全部 node 命令行、非提权 taskkill
# 成功杀掉自家 dsh），却每次启动都弹框，还把启动流程阻塞到用户点「确定」为止。
# 日志实证：07:05:04 打完权限行 → 07:06:21 才继续，中间 77 秒就是弹框在等点击。
# 现改为探测**真实能力**，且放到后台线程做（探测要跑 PowerShell 约 2-4 秒，
# 放主线程会让窗口迟迟不显示）；只有**确证**某项能力不可用才弹框。
_CAPS = {"read": None, "kill": None, "done": False}

def _probe_kill_capability():
    """能否对**同用户的其它进程**拿到结束权限。

    只 OpenProcess(PROCESS_TERMINATE) 取句柄、随即 CloseHandle，
    **绝不真的结束任何进程**。返回 True / False / None(无合适目标，无法判定)。

    ── 2026-10-31 修正：探测目标选错导致"误报权限不足" ──────────────────
    这里问的**不是**"我有没有管理员权限"，而是"我能不能结束**同一用户**起的
    进程"。Windows 上这是两件完全不同的事：

      · 自己起的子进程（dsh 就是管理器 Popen 出来的）→ **天生有终止权**，
        无需任何提权（实测：对自身子进程 / 同用户进程 OpenProcess
        PROCESS_TERMINATE 直接成功，lastErr=0）；
      · 别的用户或更高完整性级别的进程 → 才需要管理员。

    旧实现在拿不到候选时**回退到 `os.getppid()`** —— 那是"启动管理器的那个
    进程"，常常是 explorer / 某个宿主，可能比管理器本身完整性更高。一旦
    OpenProcess 对它失败，就判定"结束进程不可用"，进而弹框劝用户提权。
    **问题不在权限，在探测目标选错了。**

    现在：只探测**本进程真正起过的子进程**（`procs` 里活着的那几个，这正是
    dsh 的真实形态）；没有在跑的 dsh 就返回 None（未知），**不再**拿父进程充数，
    也绝不因为"探测不到"就去劝用户提权。
    """
    if dsh_kill is None:
        return None
    cand = []
    try:
        for _k, _p in list(procs.items()):
            try:
                if proc_alive(_p):
                    cand.append(int(_p.pid))
            except Exception:
                continue
    except Exception:
        cand = []
    if not cand:
        # 没有由本管理器拉起的 dsh 在跑 → 无从实测，返回「未知」而不是「不可用」。
        # 关键点：**不回退到父进程**（见上面说明）。
        return None
    try:
        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _k32.OpenProcess.restype = ctypes.c_void_p
        _k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    except Exception:
        return None
    for _pid in cand:
        try:
            _h = _k32.OpenProcess(0x0001, 0, int(_pid))   # PROCESS_TERMINATE
        except Exception:
            continue
        if _h:
            try:
                _k32.CloseHandle(_h)
            except Exception:
                pass
            return True
    return False

def _cap_txt(v):
    return {True: T("可用"), False: T("不可用")}.get(v, T("未知"))

def _start_capability_check():
    def _worker():
        try:
            _CAPS["read"] = dsh_kill._can_read_cmdline() if dsh_kill is not None else None
        except Exception:
            _CAPS["read"] = None
        try:
            _CAPS["kill"] = _probe_kill_capability()
        except Exception:
            _CAPS["kill"] = None
        _CAPS["done"] = True

    threading.Thread(target=_worker, daemon=True).start()

    def _poll():
        if not _CAPS["done"]:
            root.after(300, _poll); return
        _read_ok, _kill_ok = _CAPS["read"], _CAPS["kill"]
        # 只记日志，**不弹任何框**。理由见下。
        log(T("[权限] 能力实测：读命令行=%s，结束进程=%s")
            % (_cap_txt(_read_ok), _cap_txt(_kill_ok)))
        # ── 2026-10-31：移除启动时的"建议以管理员身份运行"弹框 ────────────
        # 本产品的定位是「不注册环境变量、轻量、严谨、靠谱」，用户不该在启动时
        # 被一个权限相关的弹框拦住。事实层面也支持这样做：
        #   ① dsh 是管理器自己 Popen 的子进程，结束它**不需要管理员**；
        #   ② "读不到命令行"只影响「端口被占用时说不说是谁」这一条**边缘诊断**，
        #      而且有 Get-Process 降级路径兜底，主流程零影响；
        #   ③ 弹框本身才是最大的体验损伤：时机在启动后几秒、无前置语境，
        #      用户会误以为程序在索要提权（用户实际反馈就是如此）。
        # 所以：能力状态只进日志 + 状态栏，真遇到「停止没反应」时再在**那一刻**
        # 给出针对性提示（见 kill_one 的失败分支），不做启动期打扰。
        if _read_ok is False:
            log(T("[权限] 读不到进程命令行 → 端口占用者可能显示不出程序名；"
                "已降级为 Get-Process，不影响启停主流程"))
        if _kill_ok is False:
            log(T("[权限] 无法结束同用户进程（罕见：多半是安全软件拦截）→ "
                "「停止」可能无效；此时会由停止动作本身报错，不再启动期打扰"))

    root.after(300, _poll)

if PLATFORM.IS_WINDOWS:
    _start_capability_check()

# 历史日志里可能残留旧版本写入的 token（子进程写的入口行之外，还有管理器自己写的
# "打开浏览器 -> ...?token=..." 之类）：启动时全量就地掩码一次。
_n_scrubbed = _scrub_existing_log()
if _n_scrubbed:
    log(T("已就地掩码历史日志里的 %d 个 token（等长覆盖，文件长度不变）") % _n_scrubbed)
log("install_base = %s" % INSTALL_BASE)
log("NPM_GLOBAL = %s" % (NPM_GLOBAL or T("(未探测到，<npm_global> 占位符不可用)")))
for key in SLOT_KEYS:
    cfg = VC[key]
    log("[%s] label=%s" % (key, cfg["label"]))
    log(T("[%s] 实装版本=%s") % (key, INSTALLED.get(key) or T("未知（读 package.json 失败，可能未装好）")))
    log("[%s] cmd  = %s" % (key, " ".join(_build_cmd(key))))
    log("[%s] DSH_HOME=%s  port=%d  browser=%s"
        % (key, cfg["dsh_home"] or T("(默认 ~/.dsh)"), cfg["port"], cfg["browser"]))
    # 逐槽对一遍「形状」：自带 node / bin.js / 数据目录 / 临时目录 / 三件 bat。
    # 只写日志不弹框 —— 缺个 kill bat 之类不值得打断启动，但排障时能一眼看到
    # 「哪个槽和别的槽不一样」。启动失败时的弹框在 _start_slot 里另有一套。
    if OPS is not None and hasattr(OPS, "slot_shape_problems"):
        _problems = OPS.slot_shape_problems(cfg, INSTALL_BASE, HOME, BASE_DIR)
        _errors = [detail for level, detail in _problems if level == T("错误")]
        if _errors:
            log(T("[%s] 槽配置需修复：%s。详见「设置 → 维护 → 槽体检」。")
                % (key, " / ".join(_errors)))
        elif _problems:
            log(T("[%s] 有 %d 项槽布局提示，详见「设置 → 维护 → 槽体检」。")
                % (key, len(_problems)))
# 启动快照改为延迟执行：等后台探测线程出第一批数据（一轮 ≈ 3-4 秒），
# 纯写日志零开销；窗口在此之前早已显示。
root.after(6000, _log_state_snapshot)
log("log file = %s" % LOG_FILE)

# 窗口初始大小按槽数算（见上方 _SLOT_MIN_W 的说明）：槽多时自动加宽，
# 保证每槽标题与按钮文字完整可见。日志区随窗口拉伸占满剩余高度。
root.geometry("%dx600" % _win_w)
root.columnconfigure(0, weight=1)
root.rowconfigure(2, weight=1)   # 日志区占满剩余高度（row 2 = 日志；操作按钮已收进「设置」）
root.update_idletasks()
# 窗口已 realize：再补设一次图标（双保险——就算 Tk 在 realize 时动过图标也会被这里盖回海豚）
if PLATFORM.IS_WINDOWS:
    _force_taskbar_icon()

threading.Thread(target=_detect_worker, daemon=True, name="dsh-detect").start()

# 自适应轮询：窗口在前台 1.5s、失焦 6s、最小化 10s（见 _refresh_interval_ms）。
# 必须先于第一次 refresh 绑定，否则第一次排的还是固定间隔。
root.bind("<FocusIn>", _on_focus_in, add="+")
root.bind("<FocusOut>", _on_focus_out, add="+")
# 最小化/恢复：只绑主窗口自己的 <Map>/<Unmap>。
# bind 会把事件发给**所有**子控件再冒泡，若不加 widget 判断，子控件重绘也会触发；
# 真正的判定放在回调里用 wm_state() 做（见 _on_deiconify）。
root.bind("<Map>", _on_deiconify, add="+")
root.bind("<Unmap>", _on_iconify, add="+")
root.after(800, refresh)

# 2026-11-13 用户要求：**不做启动自动扫描**。扫描只在点「扫描已装 dsh 并纳入」
# 时发生 —— 开机时不需要任何"发现新版本"的弹窗，也就没有「以后不再提示」的名单。

# ── 零槽引导卡（必须在所有回调函数定义之后构建）────────────────────
# 放在文件末尾是有原因的：ttk.Button(command=install_official_version) 会
# 在**模块执行到那一行**时去查这个名字。若构建代码排在 def 之前，就是
# NameError；而 .pyw 没有控制台，异常被吞掉 → 表现为「双击没反应」。
# 这个坑我踩了两次，现在有 C8 组断言守着。
def _build_fresh_guide():
    """零槽首屏：一段简短安装说明 + 两个按钮（安装 / 纳入）。

    2026-11-01 用户最终定的形态：
      ① 最早是「快速开始」大卡片 —— 撑不满、按钮与底部重复，废弃
      ② 后来改成两个假槽卡片 —— 用户觉得"空槽 1/空槽 2"是假的、容易误解，废弃
      ③ 现在：一张**朴素说明卡**，说清楚"这里还没有版本、装一个就能用"，
         下面两个真正的动作按钮（安装官方版 / 扫描并纳入）。
    不再画任何假槽位 —— 没装的槽不该在界面上占一个"空槽"的位置。
    """
    if SLOT_KEYS:
        # 实时判断，理由同 _build_cards：运行中卸载到 0 槽时 FRESH_START 可能还是旧值，
        # 会导致「所有槽都卸了、界面却一片空白」。
        return
    try:
        _clear_cards()
        cards_frame.columnconfigure(0, weight=1)
        cards_frame.rowconfigure(0, weight=1)

        # 2026-11-02 用户要求：去掉「开始使用」这个多余的分组标题
        _w = ttk.LabelFrame(cards_frame, text="", padding=(24, 20))
        _w.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)
        _f = ttk.Frame(_w, style="Card.TFrame")
        _f.pack(fill="both", expand=True)

        ttk.Label(_f, text=T("还没有安装 dsh"),
                  font=("Microsoft YaHei UI", 14, "bold"), style="Card.TLabel"
                  ).pack(anchor="w", pady=(0, 8))
        ttk.Label(_f,
                  text=(T("点下面的「安装 dsh」选一个版本号就会自动下载并配好隔离环境；\n"
                        "如果你机器上已经装过 dsh，点「扫描并纳入」把它接进来管理。\n"
                        "每个版本各自独立的数据目录、端口与 node 运行时，互不影响。")),
                  style="Card.TLabel", justify="left", wraplength=560
                  ).pack(anchor="w", pady=(0, 16))

        _b = ttk.Frame(_f, style="Card.TFrame")
        _b.pack(anchor="w")
        install_button = ttk.Button(_b, text=T("安装 dsh"), style="Brand.TButton",
                                    command=install_official_version)
        install_button.pack(side="left", padx=(0, 8))
        adopt_button = ttk.Button(_b, text=T("扫描并纳入"), command=scan_and_offer_adopt)
        adopt_button.pack(side="left")
        BUSY["card"].extend((install_button, adopt_button))
    except Exception as e:
        log(T("[引导] 零槽引导构建失败（不影响使用，可在「设置 → 版本管理」里安装）：%s") % e)


def _ensure_slots_visible(new_keys):
    """把**新装的槽**补进 `ui.visible`，避免"装了却看不见"。

    2026-11-01 用户实测：装完第 5 个版本、重启管理器后卡片没出现。
    根因：`ui.visible` 是**显式白名单** —— 一旦这个键存在，没列进去的槽就不画。
    而安装/纳入流程只往 `versions` 里加了槽，**从来没同步过 `ui.visible`**，
    于是新槽虽然装好了、`versions` 里也有，界面上就是不出现（用户以为装失败）。

    规则：
      · config 里没有 `ui.visible`（老配置 / 全显示模式）→ 什么都不做，本来就是全显示；
      · 有 `ui.visible` → 把 new_keys 追加进去（已在里面的不重复加），保持原有顺序。
    返回空串 = 成功或无需改动；否则是错误说明。
    """
    keys = [k for k in (new_keys or []) if k]
    if not keys:
        return ""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        return T("读取 config.json 失败：%s") % e
    ui = data.get("ui")
    if not isinstance(ui, dict):
        return ""                       # 没有 ui 段 = 全显示，无需处理
    vis = ui.get("visible")
    if not isinstance(vis, list):
        return ""                       # 同上
    changed = False
    for k in keys:
        if k in data.get("versions", {}) and k not in vis:
            vis.append(k)
            changed = True
    if not changed:
        return ""
    ui["visible"] = vis
    # 顺带确保 max_visible 能容纳现有槽数，否则新槽即使"可见"也会被截断掉
    try:
        n = len(data.get("versions") or {})
        if int(ui.get("max_visible") or 0) < min(n, MAX_SLOTS):
            ui["max_visible"] = min(n, MAX_SLOTS)
    except Exception:
        pass
    data["ui"] = ui
    tmp = CONFIG_PATH + ".tmp-%d" % os.getpid()
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return T("写入 config.json 失败：%s") % e
    return ""


def _save_ui_prefs(prefs):
    """把界面显示偏好写回 config.json。返回空串 = 成功，否则是错误说明。

    用「读-改-写」而不是整份覆盖：config.json 里还有 install_base 与 versions，
    整份覆盖等于把槽配置抹掉。原子写走「临时文件 + os.replace」，
    崩在中途也不会留下半截 JSON —— 这份文件是这台机器上所有槽的唯一真相。
    """
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        return T("读取 config.json 失败：%s") % e
    data["ui"] = prefs
    tmp = CONFIG_PATH + ".tmp-%d" % os.getpid()
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return T("写入 config.json 失败：%s") % e
    return ""


def _write_config_data(data):
    """把整份 config 字典原子写回。返回空串 = 成功，否则是错误说明。

    读-改-写 + 临时文件 + os.replace：config.json 是这台机器上所有槽的唯一
    真相，直接覆盖等于把别的槽抹掉（历史事故见 _save_ui_prefs 的注释）。
    """
    tmp = CONFIG_PATH + ".tmp-%d" % os.getpid()
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return T("写入 config.json 失败：%s") % e
    return ""


def _save_version_field(key, field, value):
    """把单个槽的某个字段写回 config.json。返回空串 = 成功，否则是错误说明。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        return T("读取 config.json 失败：%s") % e
    vs = data.get("versions")
    if not isinstance(vs, dict) or key not in vs:
        return T("config.json 里没有槽 %s（可能已被删除，请重开管理器）") % key
    if not isinstance(vs[key], dict):
        return T("config.json 里槽 %s 的格式不对（不是对象）") % key
    vs[key][field] = value
    return _write_config_data(data)


def _save_top_field(field, value):
    """把顶层字段（install_base / temp_root）写回 config.json。返回空串 = 成功。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        return T("读取 config.json 失败：%s") % e
    data[field] = value
    return _write_config_data(data)


# ── 目录设置（2026-10-31 新增）────────────────────────────────────────
# 用户诉求：「dsh 各个版本的整体的目录位置应该可以用户自己设定，因为用户的
# C 盘或者 D 盘空间可能不够」「临时文件希望集中到一个地方」。
# 机制其实一直有（config 顶层 install_base + `<base>` 占位符），但界面没有
# 入口 —— 只能手改 config.json。这里补上，并且把「改之前会发生什么」说清楚：
# 改 install_base 不会搬文件，只会让路径指向新地方，所以必须先搬目录。
def _norm_cmp(p):
    """路径比较用的归一化（大小写 / 斜杠 / 末尾分隔符都不算差异）。"""
    return os.path.normcase(os.path.normpath(str(p or ""))).replace("\\", "/")


def _cfg_form_base(p):
    """install_base 写进 config 的形式：等于用户主目录就写 `<home>`（可移植）。"""
    s = os.path.normpath(str(p or "")).replace("\\", "/")
    if _norm_cmp(s) == _norm_cmp(HOME):
        return "<home>"
    return s


def _cfg_form_temp(p, base):
    """temp_root 写进 config 的形式：在 install_base 之下就写 `<base>/…`。

    这样临时目录和槽一起搬 —— 换 install_base（换盘/换用户名）时不用改两处，
    这正是老规则（写死第二块盘的 `dsh-temp`）最别扭的地方。
    """
    s = os.path.normpath(str(p or "")).replace("\\", "/")
    b = str(base or "").replace("\\", "/").rstrip("/")
    if b and (s.lower() == b.lower() or s.lower().startswith(b.lower() + "/")):
        return "<base>" + s[len(b):]
    return s


def _ask_install_root():
    """第一次安装/纳入之前，先让用户确定「dsh 版本放在哪」。返回 True = 可以继续。

    只在**一个槽都没有**时调用 —— 有槽之后安装根就锁死了（见 `_dir_group` 的说明）。
    取消返回 False，调用方直接 return，什么都不做。

    为什么要有这一步（2026-10-31 起：装第一个版本之前就把「版本放在哪」定下来，
    缺省放在用户主目录）：
    以前 install_base 缺省是 `<home>`，装完才发现版本落在 C 盘、想换只能手改
    config.json。装第一个版本之前问一次，代价最小；装完再问就变成"要不要搬家"。
    """
    res = {"ok": False}
    win = tk.Toplevel(root)
    win.title(T("把 dsh 版本放在哪"))
    win.transient(root)
    win.resizable(False, False)
    _apply_icon(win)
    ttk.Label(win, text=T("先把位置定下来"),
              font=("Microsoft YaHei UI", 10, "bold")
              ).pack(padx=20, pady=(16, 4), anchor="w")
    ttk.Label(win,
              text=T("这是第一次安装/纳入 —— 位置一旦确定就不再改动。\n"
                   "要换位置，得先把所有版本卸载干净（卸载会先备份用户数据）。"),
              foreground=_MUTED, justify="left", wraplength=470
              ).pack(padx=20, pady=(0, 10), anchor="w")

    r = ttk.Frame(win)
    r.pack(padx=20, fill="x")
    ttk.Label(r, text=T("安装根目录")).pack(side="left")
    pv = tk.StringVar(value=os.path.normpath(INSTALL_BASE))
    ent = ttk.Entry(r, textvariable=pv, width=40, font=("Consolas", 9))
    ent.pack(side="left", padx=6, fill="x", expand=True)

    def _pick():
        p = filedialog.askdirectory(title=T("选择 dsh 版本的存放位置"), parent=win,
                                    initialdir=pv.get() or HOME)
        if p:
            pv.set(os.path.normpath(p))

    ttk.Button(r, text=T("浏览…"), width=7, command=_pick).pack(side="left")

    prev = ttk.Label(win, text="", foreground=_MUTED, justify="left",
                     wraplength=470)
    prev.pack(padx=20, pady=(8, 0), anchor="w")

    def _refresh(*_a):
        b = (pv.get() or "").strip() or INSTALL_BASE
        try:
            b = os.path.normpath(b)
        except Exception:
            pass
        # 2026-11-02：临时文件改为各槽自己的 .tmp，不再有集中目录
        prev.config(text=T("版本装进：%s\n"
                         "卸载备份：%s\n"
                         "临时文件：各槽目录下的 .tmp")
                         % (os.path.join(b, "dsh-versions"),
                            os.path.join(b, "dsh备份")))

    pv.trace_add("write", _refresh)
    _refresh()

    def on_ok(_evt=None):
        b = (pv.get() or "").strip()
        if not b:
            messagebox.showwarning(T("还没填目录"), T("请先选择 dsh 版本的存放位置。"),
                                   parent=win)
            return
        b = os.path.normpath(b)
        if not os.path.isdir(b):
            try:
                os.makedirs(b, exist_ok=True)
            except Exception as e:
                messagebox.showerror(T("无法创建目录"), "%s\n\n%s" % (b, e), parent=win)
                return
        err = _save_top_field("install_base", _cfg_form_base(b))
        if err:
            messagebox.showerror(T("保存失败"), err, parent=win)
            return
        global INSTALL_BASE
        INSTALL_BASE = b
        CFG["install_base"] = _cfg_form_base(b)
        log(T("[设置] 安装根目录（首次确定）→ %s") % b)
        res["ok"] = True
        win.destroy()

    btns = ttk.Frame(win)
    btns.pack(padx=20, pady=(12, 16))
    ttk.Button(btns, text=T("就用这个位置"), style="Green.TButton",
               command=on_ok).pack(side="left", padx=4)
    ttk.Button(btns, text=T("取消"), command=win.destroy).pack(side="left", padx=4)
    ent.bind("<Return>", on_ok)
    win.bind("<Escape>", lambda e: win.destroy())
    ent.focus_set()
    win.grab_set()
    win.wait_window()
    return res["ok"]


def _dir_group(parent, padx=20):
    """「目录」分组控件。返回 (安装根 var, 临时目录根 var)。

    安装根目录**有槽之后是只读的**（2026-10-31 起：位置在第一次安装/纳入时设定，
    之后不再改）：
    一次装到两个地方，管理器就得同时管两处 —— 备份目录、临时目录、空壳扫描、
    新建槽的落点全都得按"多根"重写，而跨盘的那半边一直是短板。与其做一个半成品
    多根管理，不如把位置锁在第一次：装之前问一次，代价最小；装完再问就变成搬家。
    临时目录根仍然可改（它只是 TEMP 指向哪里，改完会重写 bat，不影响程序本体）。
    """
    df = ttk.LabelFrame(parent, text=T("目录"), padding=(8, 6))
    df.pack(padx=padx, pady=(0, 10), fill="x")
    locked = bool(SLOT_KEYS)

    def _pick(var):
        p = filedialog.askdirectory(title=T("选择目录"), parent=df,
                                    initialdir=var.get() or HOME)
        if p:
            var.set(os.path.normpath(p))

    def _row(label, value, readonly=False):
        r = ttk.Frame(df)
        r.pack(fill="x", pady=2)
        ttk.Label(r, text=label, width=11).pack(side="left")
        var = tk.StringVar(value=value)
        ent = ttk.Entry(r, textvariable=var, width=40, font=("Consolas", 9))
        ent.pack(side="left", padx=6, fill="x", expand=True)
        if readonly:
            # 只读而不是禁用：禁用会把文字变灰到看不清，而这里恰恰要用户看清它在哪
            ent.state(["readonly"])
        else:
            ttk.Button(r, text=T("浏览…"), width=7,
                       command=lambda v=var: _pick(v)).pack(side="left")
        return var

    ib = _row(T("安装根目录"), os.path.normpath(INSTALL_BASE), readonly=locked)
    if locked:
        # 2026-11-01：安装根锁定后不能"改"，但**可以整体移动**（换盘/整理）。
        # 移动 = 复制 + 校验 + 改配置 + 删旧位置，安全性由 dsh_move 保证。
        try:
            ttk.Button(df, text=T("移动安装根目录…"), command=move_install_root_dialog
                       ).pack(anchor="w", pady=(2, 0))
        except Exception:
            pass
    # 2026-11-13：没配 temp_root 时 `_temp_root()` 返回空串，`os.path.normpath("")`
    # 会变成 "." —— 设置页上就显示成一个点，用户完全看不懂那是什么。
    # 现在显示成人话（实际落点是每个槽自己的 `.tmp`）。
    _tr_cur = str(CFG.get("temp_root") or "").strip()
    tr = _row(T("临时目录根"),
              os.path.normpath(_temp_root()) if _tr_cur
              else T("（默认）每个槽目录下的 .tmp"))
    if locked:
        # 2026-11-14 用户指出：这句话和上面的「移动安装根目录…」按钮自相矛盾 ——
        # 换位置**不需要**卸载任何版本，点那个按钮整体搬过去就行。
        _hint = ttk.Label(df,
                          text=T("已确定。要换盘 / 换位置请用上方「移动安装根目录…」——"
                               "会把所有版本连同数据一起搬过去，不用卸载。"),
                          foreground=_MUTED, justify="left")
        _hint.pack(anchor="w", pady=(4, 0))
        _auto_wrap(_hint, pad=28)
    # 2026-11-01：原来这里还有一段「安装根目录 = 版本装在哪（<路径>/dsh-versions 下…）」。
    # 用户指出**显示值与说明里的路径对不上**：输入框显示安装根本身，
    # 说明里写的却是"安装根/dsh-versions"（槽的实际落点）。两者都对，但并列摆着看不懂。
    # 现在把"槽到底装在哪"作为**单独一行只读信息**显示，不再塞进长句。
    try:
        _slots_root = os.path.join(os.path.normpath(INSTALL_BASE), "dsh-versions")
        ttk.Label(df, text=T("槽实际位置：%s\\<槽名>") % _slots_root,
                  foreground=_MUTED).pack(anchor="w", pady=(6, 0))
    except Exception:
        pass
    return ib, tr


def _apply_dir_settings(ib_var, tr_var):
    """保存「目录」设置。返回 (ok, 提示文本)；失败时文本是原因，由调用方弹框。"""
    new_base = os.path.normpath((ib_var.get() or "").strip() or INSTALL_BASE)
    _raw_temp = (tr_var.get() or "").strip()
    if not _raw_temp or _raw_temp.startswith(T("（默认）")):
        # 显示的是「（默认）每个槽目录下的 .tmp」这种说明文字，不是路径 ——
        # 当成"没改"处理，绝不能把它 normpath 成一个目录名写进 config。
        new_temp = _temp_root()
    else:
        new_temp = os.path.normpath(_raw_temp)
    base_changed = _norm_cmp(new_base) != _norm_cmp(INSTALL_BASE)
    temp_changed = _norm_cmp(new_temp) != _norm_cmp(_temp_root())
    if not base_changed and not temp_changed:
        return (True, "")
    if base_changed and temp_changed:
        # 两个一起改会算出「基于新 base 的临时目录」，但槽还没搬过去 ——
        # 等于把 TEMP 指到一片空地。宁可让用户分两步走。
        return (False, T("安装根目录和临时目录根不能一次同时改。\n\n"
                       "请先改安装根目录并保存，再回来改临时目录根。"))

    if base_changed:
        # 界面已经把安装根做成只读了，这里是第二道闸：万一有别的入口（旧版本
        # 留下的回调、探针脚本）把 var 改了，也不能真的写进 config。
        if SLOT_KEYS:
            return (False, T("安装根目录不能直接改。\n\n"
                           "它是首次安装/纳入时选定的位置。管两个不同位置的版本会让"
                           "备份目录、临时目录、新建槽的落点全部分叉，所以这里不支持"
                           "手填新路径。\n\n"
                           "要换位置请用「移动安装根目录…」：会把所有已装版本连同数据"
                           "一起搬过去（复制 → 校验 → 改配置 → 删旧位置），"
                           "**不需要卸载**。"))
        if not os.path.isdir(new_base):
            return (False, T("目录不存在：\n%s") % new_base)
        err = _save_top_field("install_base", _cfg_form_base(new_base))
        if err:
            return (False, err)
        log(T("[设置] 安装根目录 → %s") % new_base)
        return (True, T("安装根目录已设为：\n%s\n\n"
                      "版本会装进 %s；卸载备份落在 %s。\n"
                      "必须重启管理器才生效。")
                      % (new_base, os.path.join(new_base, "dsh-versions"),
                         os.path.join(new_base, "dsh备份")))

    # ── 临时目录根 ───────────────────────────────────────────────────
    if OPS is None:
        return (False, T("缺少 dsh_install_ops.py，无法迁移临时目录。"))
    try:
        os.makedirs(new_temp, exist_ok=True)
    except Exception as e:
        return (False, T("无法创建目录 %s：%s") % (new_temp, e))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        return (False, T("读取 config.json 失败：%s") % e)

    changes = OPS.plan_temp_relocation(data, INSTALL_BASE, new_temp, HOME)
    ok, msg = OPS.apply_temp_relocation(CONFIG_PATH, changes)
    if not ok:
        return (False, msg)
    err = _save_top_field("temp_root", _cfg_form_temp(new_temp, INSTALL_BASE))
    if err:
        return (False, err)
    CFG["temp_root"] = _cfg_form_temp(new_temp, INSTALL_BASE)
    # 内存里的 VC 必须同步，否则 _rebuild_bats 会照旧路径重写 bat（白改）
    for ch in changes:
        if ch["key"] in VC:
            VC[ch["key"]]["temp_dir"] = ch["new_abs"]
    made, failed, _skipped = _rebuild_bats()
    log(T("[设置] 临时目录根 → %s（%s；重写 %d 个启动脚本）")
        % (new_temp, msg, len(made)))

    lines = [T("临时目录已迁到："), "  " + new_temp, ""]
    for ch in changes:
        if ch["old_abs"] and _norm_cmp(ch["old_abs"]) != _norm_cmp(ch["new_abs"]):
            lines.append("  %-6s %s → %s" % (ch["key"], ch["old_abs"], ch["new_abs"]))
    lines.append("")
    lines.append(T("已重写 %d 个启动脚本（临时目录指向新位置）。") % len(made))
    if failed:
        lines.append(T("有 %d 个启动脚本写入失败，请看日志。") % len(failed))
    lines.append(T("旧临时目录里的文件没有搬（那是每次启动重建的垃圾），可手动删除。"))
    return (True, "\n".join(lines))


def _dirs_only_dialog():
    """零槽时的设置窗：只给「目录」。

    为什么零槽也要能进设置：最需要改安装根的时机恰恰是**装第一个版本之前**
    （C 盘满了想装到 D 盘）。老代码在这里直接弹「先装一个再来设置」，等于
    把唯一的入口堵死了。
    """
    win = tk.Toplevel(root)
    win.title(T("目录设置"))
    win.transient(root)
    win.resizable(False, False)
    _apply_icon(win)
    ttk.Label(win, text=T("目录设置"),
              font=("Microsoft YaHei UI", 10, "bold")
              ).pack(padx=20, pady=(16, 2), anchor="w")
    ttk.Label(win, text=T("还没有安装任何 dsh 版本 —— 正好先决定它装在哪。"),
              foreground=_MUTED).pack(padx=20, pady=(0, 10), anchor="w")
    ib_var, tr_var = _dir_group(win)

    def on_ok():
        ok, msg = _apply_dir_settings(ib_var, tr_var)
        if not ok:
            messagebox.showerror(T("保存失败"), msg, parent=win)
            return
        try:
            win.destroy()
        except Exception:
            pass
        if msg:
            messagebox.showinfo(T("已保存"), msg)

    btns = ttk.Frame(win)
    btns.pack(padx=20, pady=(0, 16))
    ttk.Button(btns, text=T("保存"), style="Green.TButton",
               command=on_ok).pack(side="left", padx=4)
    ttk.Button(btns, text=T("取消"), command=win.destroy).pack(side="left", padx=4)
    win.bind("<Escape>", lambda e: win.destroy())
    win.grab_set()


# ── 主题：浅色 / 深色 / 跟随系统（2026-11-01）──────────────────────────
# 用户要求：界面颜色能随系统深浅设置，也能自己指定。
#
# 实现要点：
#   · 深色色板同样取自 dsh 的主题包（近黑底 #151517 / 层 #1e1f22 / 文字 #e6e7e9），
#     保证两个界面并排时不割裂。
#   · 「跟随系统」读注册表 AppsUseLightTheme（0=深色 / 1=浅色），这是 Windows
#     应用都认的开关；读不到就回退浅色。
#   · 切换时**重新 configure 一遍 ttk 样式**即可；tk 原生控件（TextBox/Frame）
#     需要单独设背景色，见 _apply_theme_mode。
_THEME_PALETTES = {
    "light": {
        "app_bg": "#f5f6f7", "card_bg": "#ffffff", "card_bd": "#e4e6ea",
        "ink": "#151517", "muted": "#545557", "divider": "#e4e6ea",
        "neutral": "#f1f2f4", "neutral_bd": "#dcdfe4", "neutral_d": "#e6e8eb",
        "log_bg": "#ffffff", "log_fg": "#151517",
    },
    "dark": {
        "app_bg": "#151517", "card_bg": "#1e1f22", "card_bd": "#2f3136",
        "ink": "#e6e7e9", "muted": "#9a9da3", "divider": "#2f3136",
        "neutral": "#242629", "neutral_bd": "#33363b", "neutral_d": "#2b2d31",
        "log_bg": "#101113", "log_fg": "#d7d9dc",
    },
}
# 2026-11-02 修 bug：这里原来硬编码 "system"，而启动时从 config 读出的
# _THEME_INIT（第 358 行）**从没被用上** —— 结果是用户选了深色/浅色，
# 重启后又变回"跟随系统"，设置不持久（实测：config 里 theme=dark，
# 日志却打「生效：light（模式=system）」）。
_THEME_CUR = {"mode": (_THEME_INIT if _THEME_INIT in ("system", "light", "dark") else "system")}


def _apply_palette():
    """把当前配色刷到 ttk 样式与原生 tk 控件上。

    2026-11-01：**同时更新模块级颜色常量**。界面里有很多地方直接引用
    _INK / _MUTED / _RED 等（23+ 处 foreground=），只改 ttk 样式会导致
    深色背景下那些标签仍然是浅色主题的深灰字，看不清（用户实测反馈）。
    """
    global _INK, _MUTED, _DIVIDER, _NEUTRAL, _NEUTRAL_BD, _NEUTRAL_D
    # 2026-11-14 补：`_APP_BG` / `_CARD_BG` / `_CARD_BD` 原来**从没跟着主题走**，
    # 一直是浅色那份值。凡是后来新建、直接读这几个常量的控件（进度窗底色、
    # 自绘的按钮背景）在深色主题下就会冒出一块浅色补丁。
    global _APP_BG, _CARD_BG, _CARD_BD
    pal = _palette()
    _INK = pal["ink"]
    _MUTED = pal["muted"]
    _DIVIDER = pal["divider"]
    _NEUTRAL = pal["neutral"]
    _NEUTRAL_BD = pal["neutral_bd"]
    _NEUTRAL_D = pal["neutral_d"]
    _APP_BG = pal["app_bg"]
    _CARD_BG = pal["card_bg"]
    _CARD_BD = pal["card_bd"]
    st = ttk.Style()
    st.theme_use("clam")
    st.configure(".", background=pal["app_bg"], foreground=pal["ink"],
                 font=("Segoe UI", 10))
    st.configure("TLabel", background=pal["app_bg"], foreground=pal["ink"])
    st.configure("TFrame", background=pal["app_bg"])
    st.configure("TButton", background=pal["neutral"], foreground=pal["ink"],
                 bordercolor=pal["neutral_bd"], darkcolor=pal["neutral"],
                 lightcolor=pal["neutral"], relief="raised", padding=(6, 3))
    st.map("TButton", background=[("active", pal["neutral_d"]),
                                  ("pressed", pal["neutral_d"])])
    st.configure("Green.TButton", background=_GREEN, foreground="white",
                 bordercolor=_GREEN, darkcolor=_GREEN, lightcolor=_GREEN)
    st.map("Green.TButton", background=[("active", _GREEN_D), ("pressed", _GREEN_D)])
    st.configure("Brand.TButton", background=_BRAND, foreground="white",
                 bordercolor=_BRAND, darkcolor=_BRAND, lightcolor=_BRAND)
    st.map("Brand.TButton", background=[("active", _BRAND_D), ("pressed", _BRAND_D)])
    st.configure("Red.TButton", background=_RED, foreground="white",
                 bordercolor=_RED, darkcolor=_RED, lightcolor=_RED)
    st.map("Red.TButton", background=[("active", _RED_D), ("pressed", _RED_D)])
    st.configure("SoftRed.TButton", background=pal["neutral"], foreground=_RED,
                 bordercolor=pal["neutral_bd"], darkcolor=pal["neutral"],
                 lightcolor=pal["neutral"])
    # 主题选择用的「分段按钮」（2026-11-14 用户反馈「主题的几个选项看着不清楚，
    # 到底选的是哪个」）。ttk 的小圆点在浅色/深色下都只有几个像素，选中态几乎
    # 看不出来；改成「选中 = 品牌色填充 + 白字 + 加粗」，两种主题下都一样明确。
    try:
        st.configure("SegOff.TButton", background=pal["neutral"],
                     foreground=pal["ink"], bordercolor=pal["neutral_bd"],
                     darkcolor=pal["neutral"], lightcolor=pal["neutral"],
                     padding=(6, 5))
        st.map("SegOff.TButton",
               background=[("active", pal["neutral_d"]),
                           ("pressed", pal["neutral_d"])])
        st.configure("SegOn.TButton", background=_BRAND, foreground="white",
                     bordercolor=_BRAND, darkcolor=_BRAND, lightcolor=_BRAND,
                     padding=(6, 5), font=("Segoe UI", 10, "bold"))
        # 选中态悬停不变色：它就是"当前值"，不该因为鼠标划过而变淡
        st.map("SegOn.TButton",
               background=[("active", _BRAND), ("pressed", _BRAND)])
    except Exception:
        pass
    st.configure("Card.TFrame", background=pal["card_bg"])
    st.configure("Card.TLabel", background=pal["card_bg"], foreground=pal["ink"])
    st.configure("TLabelframe", background=pal["app_bg"], bordercolor=pal["card_bd"])
    st.configure("TLabelframe.Label", background=pal["app_bg"],
                 foreground=pal["muted"])
    st.configure("TSeparator", background=pal["divider"])
    # 2026-11-03 修用户实测「安装根目录里的路径字看不清」：
    # TEntry / TCombobox 从来没配过样式 —— 深色主题下它们仍是**浅底**，
    # 而文字色被设成浅色，于是白底白字、完全读不出来。
    # 这里补上：输入框/下拉框的背景、前景、边框都跟随主题。
    try:
        st.configure("TEntry",
                     fieldbackground=pal["card_bg"], background=pal["card_bg"],
                     foreground=pal["ink"], insertcolor=pal["ink"],
                     bordercolor=pal["neutral_bd"], lightcolor=pal["card_bd"],
                     darkcolor=pal["card_bd"])
    except Exception:
        pass
    try:
        st.configure("TCombobox",
                     fieldbackground=pal["card_bg"], background=pal["neutral"],
                     foreground=pal["ink"], arrowcolor=pal["ink"],
                     bordercolor=pal["neutral_bd"])
        # 下拉列表（popdown）是原生 Tk Listbox，只能通过 option 数据库改
        root.option_add("*TCombobox*Listbox.background", pal["card_bg"])
        root.option_add("*TCombobox*Listbox.foreground", pal["ink"])
        root.option_add("*TCombobox*Listbox.selectBackground", _BRAND)
        root.option_add("*TCombobox*Listbox.selectForeground", "white")
    except Exception:
        pass
    # 单选/复选按钮的底色也要跟随（否则深色下那圈小方框会浮在浅色块上）
    for _st in ("TRadiobutton", "TCheckbutton"):
        try:
            st.configure(_st, background=pal["app_bg"], foreground=pal["ink"],
                         indicatorcolor=pal["card_bg"])
            st.map(_st, background=[("active", pal["app_bg"])],
                   foreground=[("disabled", pal["muted"])])
        except Exception:
            pass
    # 滚动条（深色下默认是浅灰，很突兀）
    try:
        st.configure("Vertical.TScrollbar", background=pal["neutral"],
                     troughcolor=pal["app_bg"], bordercolor=pal["neutral_bd"],
                     arrowcolor=pal["muted"])
        st.configure("Horizontal.TScrollbar", background=pal["neutral"],
                     troughcolor=pal["app_bg"], bordercolor=pal["neutral_bd"],
                     arrowcolor=pal["muted"])
    except Exception:
        pass
    # 原生 tk 控件（不进 ttk 样式体系，必须单独设）
    for w, key_bg, key_fg in ((root, "app_bg", "ink"),
                              (cards_frame, "app_bg", "ink")):
        try:
            w.configure(bg=pal[key_bg])
        except Exception:
            pass
    try:
        log_text.configure(bg=pal["log_bg"], fg=pal["log_fg"],
                           insertbackground=pal["ink"],
                           selectbackground=_BRAND)
        # 2026-11-02：日志分级的颜色也要跟着主题走（深色下必须可读）
        _apply_log_tags()
        # 标题栏的标题/版本号/副标题同理（深色下原来几乎是黑的，看不清）
        try:
            _refresh_header_style()
        except Exception:
            pass
    except Exception:
        pass



def _system_is_dark():
    """读取平台的系统深色模式设置。"""
    if PLATFORM.IS_MAC:
        try:
            result = subprocess.run(
                ["/usr/bin/defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, encoding="utf-8", errors="replace", timeout=1)
            return result.returncode == 0 and result.stdout.strip() == "Dark"
        except (OSError, subprocess.TimeoutExpired):
            return False
    try:
        import winreg
        k = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        v, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
        winreg.CloseKey(k)
        return int(v) == 0
    except Exception:
        return False


def _theme_mode():
    """当前主题模式：system / light / dark（来自 config 的 ui.theme）。"""
    try:
        return _THEME_CUR.get("mode") or "system"
    except Exception:
        return "system"


def _resolved_theme():
    """最终生效的配色键：light 或 dark。"""
    m = _theme_mode()
    if m == "dark":
        return "dark"
    if m == "light":
        return "light"
    return "dark" if _system_is_dark() else "light"


def _palette():
    return _THEME_PALETTES[_resolved_theme()]


def _apply_theme_mode(mode, persist=True):
    """切换主题并立即重绘。

    persist=True 时把选择写进 config.json 的 ui.theme，下次启动沿用。
    """
    _THEME_CUR["mode"] = mode or "system"
    try:
        _apply_palette()
    except Exception as e:
        log(T("[主题] 应用失败：%s") % e)
    if persist:
        try:
            _save_ui_field("theme", _THEME_CUR["mode"])
        except Exception:
            pass
    # 主题名也要跟着界面语言走 —— 它是显示用的，不是枚举值
    log(T("[主题] 已切换为：%s") % T({"system": "跟随系统", "light": "浅色",
                                    "dark": "深色"}.get(_THEME_CUR["mode"], mode)))


def _save_ui_field(key, value):
    """往 config.json 的 ui 段写一个字段（读-改-写，原子落盘）。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        ui = data.get("ui")
        if not isinstance(ui, dict):
            ui = {}
        ui[key] = value
        data["ui"] = ui
        tmp = CONFIG_PATH + ".tmp-%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        return True
    except Exception as e:
        log(T("[配置] 写入 ui.%s 失败：%s") % (key, e))
        return False


# ── 并排吸附窗口（2026-11-01）──────────────────────────────────────────
# 需求：设置窗不要挡住主界面，而是**与主界面同高、并排靠右**显示；
#       用户可以把它拖走脱离（成为独立窗口），一旦拖回主界面附近就再次吸附。
#
# 实现要点（纯 Tk，不依赖额外库）：
#   · 用 <Configure> 监听窗口移动；拖动结束时判断与主窗口的距离。
#   · 距离小于阈值 → 吸附：设置窗贴到主窗口右侧，高度对齐主窗口。
#   · 拖远 → 视为脱离，之后主窗口移动不再带动它。
#   · 非模态（不用 grab_set），这样点主界面上的东西仍然有反应，"实时看到变化"才成立。
# 2026-11-02 用户要求「拉开一小段松手能被吸回，拉太远才分开」。
# 阈值 60 → 100：100px 以内松手都会吸回去，超过才真正脱离。
# 吸附时两窗外框重叠的像素数（左右顶部共用，保证四边观感一致）。
# 为什么是 2：Windows 的 DWM 给每个窗口各加一圈约 1px 的阴影/边框，
# 外框坐标贴合时视觉上仍有细缝；重叠 2px 正好吃掉它。
_DOCK_OVERLAP = 2

_DOCK = {"threshold": 100, "attached": False, "job": None, "side": "right"}


def _win_outer_rect(win):
    """窗口的**外框**屏幕矩形 (l, t, r, b)，含标题栏与边框。

    2026-11-02 修「设置窗没和主界面平齐」：
    `winfo_rooty()` 给的是**客户区**左上角（不含标题栏），而
    `geometry("+x+y")` 定位的是**窗口外框**。两者相差一个标题栏高度，
    直接拿来用就会让设置窗比主窗口低 30 来像素（实测 31px）。
    这里统一走 Win32 GetWindowRect 取外框，两个窗口用同一套坐标。
    """
    try:
        import ctypes
        from ctypes import wintypes
        win.update_idletasks()
        hwnd = _top_hwnd(win)
        if hwnd:
            r = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
            if r.right > r.left and r.bottom > r.top:
                return (r.left, r.top, r.right, r.bottom)
    except Exception:
        pass
    # 兜底：用 Tk 自己的坐标（会差一个标题栏，但至少能算）
    try:
        x, y = win.winfo_rootx(), win.winfo_rooty()
        return (x, y, x + win.winfo_width(), y + win.winfo_height())
    except Exception:
        return (0, 0, 800, 600)


def _main_rect():
    """主窗口的**外框**屏幕矩形 (x, y, w, h)。"""
    l, t, r, b = _win_outer_rect(root)
    return (l, t, r - l, b - t)


def _dock_settings_window(win):
    """把设置窗贴到主窗口右侧、顶边对齐、高度一致。

    先把设置窗自己的**外框**量出来：Tk 的 geometry 宽高指的是**客户区**，
    而主窗口的 mh 是外框高度。直接 `geometry("%dx%d" % (w, mh))` 会让
    设置窗外框比主窗口高出一个标题栏，看起来不齐。
    这里按"外框差"补偿：客户区高度 = 主窗外框高 - (设置窗外框高 - 设置窗客户区高)。
    """
    try:
        mx, my, mw, mh = _main_rect()
        # 设置窗的边框/标题栏占位：外框高 - 客户区高
        try:
            sl, st, sr, sb = _win_outer_rect(win)
            frame_h = (sb - st) - win.winfo_height()
            frame_w = (sr - sl) - win.winfo_width()
        except Exception:
            frame_h = 0
            frame_w = 0
        target_w = max(360, win.winfo_width())
        # 尺寸只在"还没定过"时设置一次；之后吸附一律只改位置，
        # 否则反复 geometry 会与用户的拖动互相打架（实测把窗口拉变形）。
        #
        # 2026-11-02 修「设置界面比主界面高 39px」：
        # 刚创建时 winfo_height() 还没 settle，frame_h 算成 0，
        # 于是客户区高度直接取了主窗外框高度 → 外框多出一个标题栏。
        # 现在延后一帧再量：等 Tk 把窗口建好、拿到真实边框尺寸后重设一次高度。
        # 2026-11-02：高度每次吸附都重算（幂等，不会抖）。
        # 只算"外框高 - 客户区高"这个差值再反推客户区高度，保证**外框等高**。
        def _apply_height():
            try:
                if not win.winfo_exists():
                    return
                wr2 = _win_outer_rect(win)
                fh = (wr2[3] - wr2[1]) - win.winfo_height()
                if fh > 0:
                    win.geometry("%dx%d" % (target_w, max(200, mh - fh)))
            except Exception:
                pass

        if not _DOCK.get("sized"):
            win.geometry("%dx%d" % (target_w, max(200, mh - max(0, frame_h))))
            _DOCK["sized"] = True
        # 立即做一次 + 延后一次（Tk 在首帧后才知道真实边框尺寸）
        _apply_height()
        try:
            win.after(150, _apply_height)
        except Exception:
            pass
            _DOCK["sized"] = True
        win.geometry("+%d+%d" % (mx + mw, my))
        _DOCK["attached"] = True
    except Exception:
        pass


def _undock_settings_window(win):
    _DOCK["attached"] = False


def _mac_dock_x(main_x, main_width, panel_width, left, right):
    right_of_main = main_x + main_width
    left_of_main = main_x - panel_width
    limit = max(left, right - panel_width)
    if left <= right_of_main <= limit:
        return right_of_main
    if left <= left_of_main <= limit:
        return left_of_main
    return limit


def _make_mac_side_panel(win, width):
    """Follow the main window. Settle a title-bar drag only after mouse-up."""
    class Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    class Size(ctypes.Structure):
        _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]

    class Rect(ctypes.Structure):
        _fields_ = [("origin", Point), ("size", Size)]

    try:
        quartz = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        button_state = quartz.CGEventSourceButtonState
        button_state.argtypes = [ctypes.c_int32, ctypes.c_uint32]
        button_state.restype = ctypes.c_bool
        displays_at = quartz.CGGetDisplaysWithPoint
        uint_ref = ctypes.POINTER(ctypes.c_uint32)
        displays_at.argtypes = [Point, ctypes.c_uint32, uint_ref, uint_ref]
        displays_at.restype = ctypes.c_int32
        display_bounds = quartz.CGDisplayBounds
        display_bounds.argtypes = [ctypes.c_uint32]
        display_bounds.restype = Rect
    except (AttributeError, OSError):
        button_state = None
        displays_at = None

    attached = True
    expected = None
    pending = None
    follow_job = None

    def _display_edges(mx, my, mw, mh):
        # Tk's screen width is for the primary display, even on an external monitor.
        if displays_at is not None:
            display, count = ctypes.c_uint32(), ctypes.c_uint32()
            center = Point(mx + mw / 2, my + mh / 2)
            if displays_at(center, 1, ctypes.byref(display), ctypes.byref(count)) == 0 and count.value:
                bounds = display_bounds(display)
                return int(bounds.origin.x), int(bounds.origin.x + bounds.size.width)
        return 0, root.winfo_screenwidth()

    def _target():
        # Aqua winfo_x/y, unlike winfo_rootx/y, are in wm geometry coordinates.
        mx, my, mw, mh = root.winfo_x(), root.winfo_y(), root.winfo_width(), root.winfo_height()
        return (_mac_dock_x(mx, mw, width, *_display_edges(mx, my, mw, mh)), my, mh)

    def _place():
        nonlocal expected
        target = _target()
        if target != expected or (win.winfo_x(), win.winfo_y()) != target[:2]:
            expected = target
            x, y, h = target
            win.geometry("%dx%d+%d+%d" % (width, h, x, y))

    def _settle():
        nonlocal attached, pending
        pending = None
        x, y = win.winfo_x(), win.winfo_y()
        if (x, y) == expected[:2]:
            return
        # Combined login-session event source and left mouse button.
        if button_state is not None and button_state(0, 0):
            pending = win.after(50, _settle)
            return
        tx, ty, _ = _target()
        attached = max(abs(x - tx), abs(y - ty)) <= _DOCK["threshold"]
        if attached:
            _place()

    def _moved(evt):
        nonlocal pending
        if evt.widget is not win or expected is None:
            return
        if pending is not None:
            win.after_cancel(pending)
        pending = win.after(120, _settle)

    def _follow_settled():
        nonlocal follow_job
        follow_job = None
        if attached:
            _place()

    def _follow(evt):
        nonlocal follow_job
        if evt.widget is root and attached and follow_job is None:
            # Aqua can deliver Configure before winfo_x/y reflect the move.
            follow_job = win.after_idle(_follow_settled)

    follow_id = root.bind("<Configure>", _follow, add="+")

    def _unbind(evt):
        if evt.widget is win:
            root.unbind("<Configure>", follow_id)
            if pending is not None:
                win.after_cancel(pending)
            if follow_job is not None:
                win.after_cancel(follow_job)

    win.bind("<Destroy>", _unbind, add="+")
    win.bind("<Configure>", _moved, add="+")
    # A centered main window can leave slightly less than a panel width on
    # either side. Make room on opening if both windows fit on the display.
    mx, my, mw, mh = root.winfo_x(), root.winfo_y(), root.winfo_width(), root.winfo_height()
    left, right = _display_edges(mx, my, mw, mh)
    edge = right - width
    if left <= edge - mw < mx < left + width:
        root.geometry("+%d+%d" % (edge - mw, my))
    _place()
    return win


def _make_side_panel(win, width=380):
    """把任意 Toplevel 变成"并排可吸附"的侧栏。

    返回一个 bind 好的处理器；调用方只需在创建完控件后调用一次。
    """
    if PLATFORM.IS_MAC:
        return _make_mac_side_panel(win, width)
    # 每次打开设置窗都重新定尺寸（用户可能关掉再开）。
    _DOCK["sized"] = False
    _DOCK.pop("w", None)
    _DOCK.pop("h", None)
    try:
        mx, my, mw, mh = _main_rect()
        win.geometry("%dx%d+%d+%d" % (width, mh, mx + mw, my))
        _DOCK["sized"] = True
        # 记录**实际生效**的宽高（Tk 可能按内容微调 width，不能沿用传参）。
        # 侧栏厚度固定为 width，高度目标是主窗外框高。
        _DOCK["w"] = width
        _DOCK["h"] = mh
        _DOCK["panel_w"] = width      # 固定宽度，跟随主窗时不再重测
    except Exception:
        pass

    def _on_configure(evt=None):
        # 只关心窗口自身位置变化
        if evt is not None and evt.widget is not win:
            return
        if _DOCK.get("job"):
            try:
                win.after_cancel(_DOCK["job"])
            except Exception:
                pass
        _DOCK["job"] = win.after(180, lambda: _check_dock(win))

    # 2026-11-02 修「打开设置没贴住主界面」：
    # <Configure> 在窗口**创建过程中**就会连续触发（Tk 每改一次几何都发一次）。
    # 那时 _main_rect() 拿到的还是主窗口的旧尺寸/未布局尺寸，
    # 算出来的 gap 必然超阈值 → 初始化阶段就把 attached 判成 False，
    # 于是"刚打开就脱开了"。
    # 现在：前 _DOCK_GRACE 次 Configure 一律强制吸附（把它们当作初始化噪声），
    # 之后才按真实拖动位置判断。
    _grace = {"n": 0}
    _DOCK_GRACE = 3

    def _check_dock(w):
        """拖动停下后判断：靠得够近就吸过去（四边都算）。

        2026-11-02 用户反馈「靠近时没有磁铁吸住的感觉」。原实现只判断
        **与主窗口右边缘**的水平距离，而且要求垂直也接近 —— 从左侧或下方
        拖过来时永远不会触发。
        改成：分别算 设置窗四边 与 对应主窗边 的距离，任一边进入阈值就吸附到那一侧。
        """
        _DOCK["job"] = None
        try:
            if not w.winfo_exists():
                return
            _grace["n"] += 1
            if _grace["n"] <= _DOCK_GRACE:
                _dock_settings_window(w)
                return
            mr = _main_rect()
            wr = _win_outer_rect(w)
            if not mr or not wr:
                return
            mx, my, mw, mh = mr
            wl, wt, wr_r, wb = wr
            th = _DOCK["threshold"]
            # 四个候选：吸到主窗 右侧 / 左侧 / 下方 / 上方
            d_right = abs(wl - (mx + mw))      # 设置窗左边 ↔ 主窗右边
            d_left = abs(wr_r - mx)            # 设置窗右边 ↔ 主窗左边
            d_below = abs(wt - (my + mh))      # 设置窗上边 ↔ 主窗下边
            d_above = abs(wb - my)             # 设置窗下边 ↔ 主窗上边

            best = min(d_right, d_left, d_below, d_above)
            if best > th:
                _DOCK["attached"] = False
                return

            # 2026-11-02：**左右两侧都吸附**，并让两窗各重叠 2px。
            # 为什么要重叠：Windows 的 DWM 会给每个窗口加一圈阴影，
            # GetWindowRect 量出来缝隙是 0，但视觉上中间仍有一道几像素的透明缝。
            # 让外框重叠 2px 就能吃掉它，看起来是真的"贴在一起"。
            # 2026-11-02：用**属性记录的目标宽高**，而不是动画中途的 winfo_width()。
            # 后者在 _dock_animate 正在改几何时会读到中间态，导致左侧吸附差 18px。
            # 用**实测外框宽**：_DOCK["w"] 是请求值（380），
            # 而 Tk 会按内容把它撑到 396，两者差 16px 正是左侧吸附差 18 的来源。
            # 2026-11-03：优先用**打开时固定的侧栏宽度**。
            # 原来这里用 _win_outer_rect 实测宽度，而那个值正是我们上一帧写进去的，
            # 在几何未 settle 时会读到中间态 → 宽度被逐次放大（实测涨到 844px）。
            cw_ = int(_DOCK.get("panel_w") or 0)
            if cw_ < 300:
                try:
                    _wrect = _win_outer_rect(w)
                    cw_ = max(360, _wrect[2] - _wrect[0])
                except Exception:
                    cw_ = max(360, int(_DOCK.get("w") or w.winfo_width()))
            try:
                _wrect2 = _win_outer_rect(w)
                ch_ = max(200, _wrect2[3] - _wrect2[1])
            except Exception:
                ch_ = max(200, int(_DOCK.get("h") or w.winfo_height()))
            OVERLAP = _DOCK_OVERLAP

            if best == d_right or best == d_left:
                if best == d_right:
                    x = mx + mw - OVERLAP
                else:
                    # 左侧：设置窗右边缘贴到主窗左边缘（再重叠 2px）
                    x = mx - cw_ + OVERLAP
                _dock_animate(w, x, my)
                _DOCK["side"] = "right" if best == d_right else "left"
            else:
                y = (my + mh - OVERLAP) if best == d_below else (my - ch_ + OVERLAP)
                _dock_animate(w, wl, y)
                _DOCK["side"] = "bottom" if best == d_below else "top"
            _DOCK["attached"] = True
        except Exception:
            pass

    try:
        win.bind("<Configure>", _on_configure, add="+")
    except Exception:
        pass

    # 2026-11-02 用户要求：「拖动主界面时设置界面跟着一起动」。
    # 做法：监听主窗口 <Configure>（位置或尺寸变化都会触发），
    # 处于吸附状态时把侧栏重新贴到主窗右侧。
    # 节流 16ms，避免拖动时每像素都改一次几何导致闪烁。
    def _on_main_moved(evt=None):
        if evt is not None and evt.widget is not root:
            return
        if not _DOCK.get("attached"):
            return                       # 用户已把侧栏拖离 → 不跟随
        if _DOCK.get("follow_job"):
            return
        _DOCK["follow_job"] = root.after(16, lambda: _do_follow(win))

    try:
        root.bind("<Configure>", _on_main_moved, add="+")
    except Exception:
        pass

    _DOCK["attached"] = True
    return win


def _dock_animate(win, target_x, target_y, steps=8, bounce=1):
    """把窗口**动画滑到** (target_x, target_y)，带一点回弹。

    2026-11-02 用户要求：「吸附效果强一些、明显一些…可以略弹一下」。
    纯瞬移看不出"吸"的感觉；这里用 ease-out 分 8 帧滑过去，
    最后一帧再多走 2px 再回正，制造轻微的"磁吸回弹"。
    """
    try:
        if not win.winfo_exists():
            return
        wr = _win_outer_rect(win)
        x0, y0 = wr[0], wr[1]
        if abs(x0 - target_x) <= 1 and abs(y0 - target_y) <= 1:
            return
        # 2026-11-02：若只给 "+x+y"（纯位置），Tk 在部分情况下会按"上次请求的尺寸"
        # 重排，实测把窗口拉成 854x1079。这里**始终带上当前宽高**，尺寸零变化。
        try:
            _cw = win.winfo_width()
            _ch = win.winfo_height()
        except Exception:
            _cw, _ch = 0, 0

        def step(i, _x0=x0, _y0=y0):
            try:
                if not win.winfo_exists():
                    return
                t = i / float(steps)
                e = 1 - (1 - t) ** 3            # ease-out：先快后慢
                x = int(_x0 + (target_x - _x0) * e)
                y = int(_y0 + (target_y - _y0) * e)
                # 最后一帧轻微过冲再回正（回弹感）
                if i == steps and bounce:
                    ox = 2 if target_x >= _x0 else -2
                    oy = 2 if target_y >= _y0 else -2
                    win.geometry("%dx%d+%d+%d" % (_cw, _ch, x + ox, y + oy))
                    win.after(60, lambda: (win.winfo_exists() and
                                           win.geometry("%dx%d+%d+%d" % (_cw, _ch, target_x, target_y))))
                    return
                win.geometry("%dx%d+%d+%d" % (_cw, _ch, x, y))
                if i < steps:
                    win.after(12, lambda: step(i + 1))
            except Exception:
                pass

        step(1)
    except Exception:
        try:
            win.geometry("+%d+%d" % (target_x, target_y))
        except Exception:
            pass


def _do_follow(win):
    """把侧栏重新贴到主窗口右侧，并把高度对齐（仅吸附状态下调用）。

    2026-11-02 用户要求：「移动主界面位置时设置界面跟着动；
    上下拉长或缩短主界面高度时，设置界面最好也能一起动」。
    所以这里同时处理**位置**与**高度**：
      · 位置：按当前吸附边（左/右）重新计算
      · 高度：用"外框高 - 客户区高"反推，保证外框等高
    """
    _DOCK["follow_job"] = None
    try:
        if not win.winfo_exists() or not _DOCK.get("attached"):
            return
        mx, my, mw, mh = _main_rect()
        wr = _win_outer_rect(win)
        # 2026-11-03 修 bug（用户实测：拉长主窗后设置窗横向变化了）：
        # 原来每次跟随都重新用 wr[2]-wr[0] 量宽度 —— 而那正是**我们上一帧
        # 刚写进去的值**，在几何尚未 settle 时会读到中间态，于是宽度被反复
        # 收缩/放大，看起来就是"横向变化"。
        # 宽度改用**打开时固定的侧栏宽度**，只有高度跟随主窗。
        cw_ = int(_DOCK.get("panel_w") or 0)
        if cw_ < 300:
            cw_ = max(360, wr[2] - wr[0])
            _DOCK["panel_w"] = cw_
        frame_h = (wr[3] - wr[1]) - win.winfo_height()
        target_ch = max(200, mh - max(0, frame_h))

        side = _DOCK.get("side") or "right"
        if side == "left":
            x = mx - cw_ + _DOCK_OVERLAP
        elif side in ("top", "bottom"):
            x = wr[0]
        else:
            x = mx + mw - _DOCK_OVERLAP

        # 位置 + 高度一次给全，避免只改位置导致的尺寸抖动
        if side == "top":
            y = my - (target_ch + max(0, frame_h)) + _DOCK_OVERLAP
        elif side == "bottom":
            y = my + mh - _DOCK_OVERLAP
        else:
            y = my
        win.geometry("%dx%d+%d+%d" % (cw_, target_ch, x, y))
    except Exception:
        pass


def _open_project_page():
    """打开项目主页（GitHub）。收藏 / 看更新 / 提 issue 都从这儿走。"""
    try:
        import webbrowser
        webbrowser.open(PROJECT_URL)
    except Exception:
        try:
            _platform_open(PROJECT_URL)
        except Exception as e:
            messagebox.showinfo(T("项目主页"), T("打不开浏览器，请手动访问：\n%s\n\n(%s)")
                                % (PROJECT_URL, e))


def _open_user_guide():
    """打开详细使用说明（文档文件）。找不到时给出明确说明。

    英文模式下优先打开 `docs/user-guide.en.md` —— 中文文档对英文用户没有意义。
    英文版缺失时退回中文版（总比打不开好）。
    """
    _cands = []
    if I18N.lang() == "en":
        _cands.append(os.path.join(BASE_DIR, "docs", "user-guide.en.md"))
    _cands.append(os.path.join(BASE_DIR, "docs", "dsh管理器使用说明.md"))
    p = _cands[-1]
    for _p in _cands:
        if os.path.exists(_p):
            p = _p
            try:
                _platform_open(p)
                return
            except Exception:
                pass
    messagebox.showinfo(T("详细使用说明"),
                        T("找不到文档文件：\n%s\n\n"
                        "完整说明见程序目录下的 docs/ 文件夹，"
                        "或项目主页的 README。") % p)


def about_dialog():
    """「关于」窗口：软件基本信息 + 使用说明入口 + 运行环境检查。

    2026-11-01 用户需求：左上加一个「关于」，点开能看到软件信息，
    也能看到详细使用说明。

    为什么把「检查运行环境」搬到这里：它查的是**管理器自身的运行前提**
    （Python / tkinter / node / 权限 / 端口），而管理器既然已经打开了，
    这些基本必然通过 —— 摆在首页引导卡上属于"占位置的按钮"。
    只有排障时才需要，放进「关于」正合适。
    """
    win = tk.Toplevel(root)
    win.title(T("关于 DSH 管理器"))
    win.transient(root)
    win.resizable(False, False)
    _apply_icon(win)

    ttk.Label(win, text=T("DSH 管理器（DSH-TM）"),
              font=("Microsoft YaHei UI", 13, "bold")).pack(padx=24, pady=(20, 2),
                                                           anchor="w")
    ttk.Label(win, text=T("多版本隔离 · 独立进程 / 端口 / 数据 / 运行时 / 临时目录"),
              foreground=_MUTED).pack(padx=24, anchor="w")

    body = ttk.Frame(win)
    body.pack(padx=24, pady=(14, 8), fill="both", expand=True)
    body.columnconfigure(1, weight=1)

    def _row(r, label, value):
        ttk.Label(body, text=label, foreground=_MUTED).grid(
            row=r, column=0, sticky="w", pady=2, padx=(0, 12))
        ttk.Label(body, text=value, wraplength=380, justify="left").grid(
            row=r, column=1, sticky="w", pady=2)

    def _safe(fn, default="—"):
        try:
            v = fn()
            return v if v not in (None, "") else default
        except Exception:
            return default

    _row(0, T("版本"), _safe(lambda: _app_version()))
    _row(1, T("槽上限"), T("%d 个") % MAX_SLOTS)
    _row(2, T("当前槽数"), T("%d 个") % len(SLOT_KEYS))
    _row(3, T("安装根目录"), os.path.normpath(INSTALL_BASE))
    _row(4, T("程序目录"), BASE_DIR)
    _row(5, T("运行环境"), "%s（%s）" % (
        _safe(lambda: "%d.%d.%d" % sys.version_info[:3]),
        _safe(lambda: os.path.basename(sys.executable) or "python")))
    _row(6, T("日志文件"), LOG_FILE)

    ttk.Separator(win, orient="horizontal").pack(fill="x", padx=24, pady=(8, 0))

    ttk.Label(win, text=T("使用说明"), font=("Microsoft YaHei UI", 10, "bold")
              ).pack(padx=24, pady=(12, 4), anchor="w")
    ttk.Label(win,
              justify="left", wraplength=470, foreground=_INK,
              text=(T("· 「安装官方版 dsh」→ 选版本号 → 自动下载并配好隔离环境\n"
                    "· 卡片上「启动 / 停止」管单个版本；「打开浏览器」带上登录 token\n"
                    "· 拖动卡片可调整左右顺序；「设置」里能隐藏不看的槽\n"
                    "· 「卸载 / 移出管理」：卸载会先备份用户数据再删；\n"
                    "  移出只改配置、磁盘一个字节不动（之后可在「已移出管理」接回）\n"
                    "· 「槽体检」只读检查，报告问题但不替你改\n"
                    "· 命令行：python dsh-tm.py list | start --all | stop --all"))
              ).pack(padx=24, anchor="w")

    btns = ttk.Frame(win)
    btns.pack(padx=24, pady=(14, 18), fill="x")

    ttk.Button(btns, text=T("打开详细使用说明"), command=_open_user_guide).pack(side="left")
    ttk.Button(btns, text=T("检查运行环境"), command=_show_env_report).pack(
        side="left", padx=(8, 0))
    # 「槽体检」从主界面底部搬到这里（一年用不了几次的排障工具）
    ttk.Button(btns, text=T("槽体检"),
               command=lambda: _run_health(deep=True, dialog=True)
               ).pack(side="left", padx=(8, 0))
    ttk.Button(btns, text=T("关闭"), command=win.destroy).pack(side="right")
    win.bind("<Escape>", lambda e: win.destroy())
    win.grab_set()


def _app_version():
    """程序版本号。**唯一事实来源是仓库根的 `VERSION` 文件**（2026-11-14）。

    为什么不再读 config.json：那是**每台机器一份**的运行时配置，版本号跟着它走，
    换台机器 clone 下来就显示成兜底值了。版本属于代码，应该跟代码一起提交。

    读取顺序：`<管理器目录>/VERSION` → config.json 的 `version`（兼容老配置）→ "0.0.0"。
    版本号本身不带 `v` 前缀（`0.1.0`），打 tag 时才写 `v0.1.0`。
    """
    try:
        with open(os.path.join(BASE_DIR, "VERSION"), "r", encoding="utf-8-sig") as f:
            v = (f.read() or "").strip()
        if v:
            return v
    except Exception:
        pass
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            v = (json.load(f) or {}).get("version")
        if v:
            return str(v)
    except Exception:
        pass
    return "0.0.0"


def move_install_root_dialog():
    """把已装的槽整体搬到另一个位置（换盘 / 整理目录）。

    2026-11-01 用户需求：装了版本后磁盘紧张想换盘，或纯粹想把安装根挪个位置
    做文件整理 —— 原来只能"全部卸载再重装"，代价太大。

    安全性由 dsh_move 模块保证（它有自己的离线回归 test-move.py）：
      · 先复制 → 校验（文件数+字节数）→ **成功才改 config、才删源**
      · 目标已有同名目录一律中止，绝不覆盖
      · junction / 符号链接不跟随、不复制内容
      · 旧目录删不干净也照样成功（提示手动清理），绝不让用户"搬一半"
    """
    try:
        import dsh_move as MV
    except Exception as e:
        messagebox.showerror(T("无法移动"), T("缺少 dsh_move.py 模块：%s") % e)
        return
    if not SLOT_KEYS:
        messagebox.showinfo(T("没有可移动的版本"),
                            T("当前管理器没有管理任何 dsh 版本。\n\n"
                            "装第一个版本时会让您选位置，那时选好即可。"))
        return

    # 运行中的槽不能搬（2026-11-14）：文件被占用时复制会失败，而且搬完那个
    # 进程还在用旧路径 —— 与其让它搬一半失败，不如先要求停。
    _running = []
    for _k in SLOT_KEYS:
        try:
            if port_open(VC[_k]["port"]) or proc_alive(procs.get(_k)):
                _running.append("%s（:%d）" % (VC[_k].get("label") or _k,
                                              VC[_k]["port"]))
        except Exception:
            pass
    if _running:
        messagebox.showwarning(
            T("有版本正在运行"),
            T("这些版本还在运行：%s\n\n"
            "请先点「全部停止」（或逐个停止）再移动 ——\n"
            "运行中的程序占着文件，复制会失败；搬完它也还指着旧位置。")
            % "、".join(_running))
        return

    win = tk.Toplevel(root)
    win.title(T("移动安装根目录"))
    win.transient(root)
    win.resizable(False, False)
    _apply_icon(win)

    ttk.Label(win, text=T("移动安装根目录"),
              font=("Microsoft YaHei UI", 10, "bold")
              ).pack(padx=20, pady=(16, 4), anchor="w")
    ttk.Label(win,
              text=(T("把已装的所有版本整体搬到另一个位置。\n"
                    "过程：复制 → 校验（文件数 + 字节数）→ 改配置 → 删旧位置。\n"
                    "复制或校验失败会保留旧位置、不动配置；\n"
                     "旧目录清理失败时会提示你手动清理。")),
              foreground=_MUTED, justify="left", wraplength=470
              ).pack(padx=20, pady=(0, 10), anchor="w")

    cur = ttk.Frame(win)
    cur.pack(padx=20, fill="x")
    ttk.Label(cur, text=T("当前位置")).pack(side="left", padx=(0, 6))
    ttk.Label(cur, text=os.path.normpath(INSTALL_BASE),
              foreground=_MUTED).pack(side="left")

    newv = tk.StringVar(value="")

    row = ttk.Frame(win)
    row.pack(padx=20, pady=(8, 0), fill="x")
    ttk.Label(row, text=T("新位置")).pack(side="left", padx=(0, 6))
    ent = ttk.Entry(row, textvariable=newv, width=42, font=("Consolas", 9))
    ent.pack(side="left", padx=(0, 6), fill="x", expand=True)

    def _pick():
        p = filedialog.askdirectory(title=T("选择新的安装根目录"), parent=win,
                                     initialdir=os.path.normpath(INSTALL_BASE))
        if p:
            newv.set(os.path.normpath(p))

    ttk.Button(row, text=T("浏览…"), width=7, command=_pick).pack(side="left")

    ttk.Label(win,
              text=(T("· 新位置下会创建 dsh-versions\\ 存放各版本槽\n"
                    "· 磁盘空间要够（每个版本约 0.7～1.3 GB）\n"
                    "· 移动期间请勿启动或停止任何版本")),
              foreground="#8A5A00", justify="left", wraplength=470
              ).pack(padx=20, pady=(10, 0), anchor="w")

    def on_ok():
        target = (newv.get() or "").strip()
        if not target:
            messagebox.showwarning(T("还没选位置"), T("请先选择新的安装根目录。"))
            return
        target = os.path.normpath(target)
        slot_names = []
        for k in SLOT_KEYS:
            source = _version_root(VC[k])
            if not source:
                continue
            nm = os.path.basename(os.path.normpath(source))
            expected = os.path.join(INSTALL_BASE, "dsh-versions", nm)
            if os.path.realpath(source) == os.path.realpath(expected):
                slot_names.append(nm)
        plan = MV.plan_move(INSTALL_BASE, target, slot_names)
        if not plan["ok"]:
            messagebox.showerror(T("无法移动"), plan["error"])
            return
        body = "\n".join("  · " + s for s in plan["steps"])
        _need = plan.get("need_bytes") or 0
        _free = plan.get("free_bytes")
        _space = ""
        if _free is not None:
            _space = (T("\n空间：需要约 %.2f GB，目标盘可用 %.2f GB。")
                      % (_need / 1073741824.0, _free / 1073741824.0))
        if not messagebox.askyesno(
                T("确认移动"),
                T("即将把 %d 个版本搬到：\n%s\n\n%s%s\n\n"
                "旧位置在全部校验通过后才会被删除。\n\n继续吗？")
                % (len(plan["slots"]), target, body, _space)):
            return
        try:
            win.destroy()
        except Exception:
            pass
        _do_move_root(MV, plan)

    btns = ttk.Frame(win)
    btns.pack(padx=20, pady=(16, 16), fill="x")
    ttk.Button(btns, text=T("开始移动"), style="Green.TButton",
               command=on_ok).pack(side="left")
    ttk.Button(btns, text=T("取消"), command=win.destroy).pack(side="left", padx=(8, 0))
    win.bind("<Escape>", lambda e: win.destroy())
    win.grab_set()


def _do_move_root(MV, plan):
    """后台执行搬迁，进度窗 + 完成后提示重启。"""
    log(T("[移动] 开始：%s → %s（%d 个槽）")
        % (plan["src_base"], plan["dst_base"], len(plan["slots"])))

    def done(ok, message):
        _actions_enabled(True)
        if ok:
            log(T("[移动] 完成：%s") % str(message).replace("\n", " | "))
            messagebox.showinfo(
                T("移动完成"),
                T("%s\n\n管理器已切换到新位置。") % message)
            _restart_or_tell(T("安装位置"))
        else:
            log(T("[移动] 失败：%s") % str(message).replace("\n", " | "))
            messagebox.showerror(T("移动失败"), str(message))

    prog = _GuiProgress(T("移动安装根目录"), done)

    def work():
        try:
            ok, msg = MV.move_now(
                plan, CONFIG_PATH,
                progress=lambda t: prog.on_step(t))
            return (ok, msg, "")
        except Exception as e:
            return (False, "", T("移动过程异常：%s") % e)

    OPS.run_in_thread(work, prog, None)


def _log_stats():
    """返回 (字节数, 行数)；文件不存在就是 (0, 0)。"""
    try:
        sz = os.path.getsize(LOG_FILE)
    except Exception:
        sz = 0
    lines = 0
    try:
        with open(LOG_FILE, "rb") as f:
            for _ in f:
                lines += 1
    except Exception:
        pass
    return sz, lines


def _fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f GB" % n


def _clear_logs_dialog(parent=None):
    """清空日志文件（先确认）。不动任何已安装版本、不动 config。"""
    sz, lines = _log_stats()
    if sz <= 0:
        messagebox.showinfo(T("日志已为空"), T("日志文件本来就是空的。"),
                            parent=parent)
        return
    if not messagebox.askyesno(
            T("清空日志"),
            T("当前日志：%s（%d 行）\n位置：%s\n\n"
            "清空后无法恢复，但不影响任何已安装版本、配置和进程。\n\n确定清空吗？")
            % (_fmt_size(sz), lines, LOG_FILE), parent=parent):
        return
    err = ""
    try:
        # 保持文件句柄可用：先关掉再截断再重开
        global LOG_FH
        try:
            if LOG_FH:
                LOG_FH.close()
        except Exception:
            pass
        LOG_FH = None
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write("")
        try:
            LOG_FH = open(LOG_FILE, "a", encoding="utf-8", errors="replace")
        except Exception:
            LOG_FH = None
    except Exception as e:
        err = str(e)
    if err:
        messagebox.showerror(T("清空失败"), err, parent=parent)
        return
    log(T("[清理] 日志已清空（用户手动操作）"))


def _open_log_folder(parent=None):
    try:
        if PLATFORM.IS_WINDOWS or PLATFORM.IS_MAC:
            _platform_open(os.path.dirname(LOG_FILE))
        else:
            messagebox.showinfo(T("日志位置"), LOG_FILE, parent=parent)
    except Exception as e:
        messagebox.showinfo(T("日志位置"), "%s\n\n(%s)" % (LOG_FILE, e), parent=parent)


def _auto_wrap(lbl, pad=0, min_w=120):
    """让长说明文字的换行宽度**跟着父容器走**。

    2026-11-14 用户实测：设置侧栏只有 380px 宽，而说明文字写死了
    `wraplength=430` —— 结果第二行被窗口右边缘裁掉（"…连同数" 就断了），
    第一行只剩 "换盘 /"。写死的像素值在可缩放窗口里迟早对不上，
    所以这里绑 <Configure> 动态改。
    """
    def _sync(_e=None):
        try:
            w = lbl.master.winfo_width() - pad
        except Exception:
            return
        if w >= min_w:
            try:
                lbl.configure(wraplength=w)
            except Exception:
                pass
    try:
        lbl.master.bind("<Configure>", _sync, add="+")
    except Exception:
        pass
    _sync()


def _scroll_host(win):
    """在 Toplevel 里造一个「可竖向滚动」的内容容器，返回内部 Frame。

    用于设置页：并排窗口高度被主窗口锁死（约 640px），内容一多就会把
    底部「关于」切掉。这里用 Canvas + 内嵌 Frame 的标准做法解决，
    滚动条只在内容真的超出时才出现。
    """
    outer = ttk.Frame(win)
    outer.pack(fill="both", expand=True)
    canvas = tk.Canvas(outer, highlightthickness=0, bd=0)
    sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    inner = ttk.Frame(canvas)
    win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    if PLATFORM.IS_MAC:
        canvas.configure(yscrollincrement=1)

    def _sync(_e=None):
        try:
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(win_id, width=canvas.winfo_width())
        except Exception:
            pass

    inner.bind("<Configure>", _sync)
    canvas.bind("<Configure>", _sync)

    # 主题：Canvas 属于原生 tk 控件，需要跟着配色
    try:
        pal = _palette()
        canvas.configure(bg=pal["app_bg"])
    except Exception:
        pass

    # 2026-11-02 修 bug（用户实测：滚鼠标看日志，设置界面也跟着动）：
    # 原来用 canvas.bind_all("<MouseWheel>")，那是**全局绑定** ——
    # 鼠标在主窗口日志区滚动也会触发这里的回调；而 Tk 在滚动越界时
    # 会尝试"滚动整个窗口"，表现为设置窗跟着乱跳。
    # 现在只在**设置窗区域内部**（canvas 及其子控件）绑定，主界面滚动与它无关。
    def _scroll_by(steps):
        first, last = canvas.yview()
        if steps and not (first <= 0.0 and last >= 1.0):
            canvas.yview_scroll(steps, "units")
            return "break"

    def _wheel(evt):
        steps = -evt.delta if PLATFORM.IS_MAC else -int(evt.delta / 120)
        steps = max(-6, min(6, steps))
        return _scroll_by(steps * 12 if PLATFORM.IS_MAC else steps)

    win.bind("<MouseWheel>", _wheel, add="+")
    if PLATFORM.IS_MAC and win.tk.call("info", "commands", "tk::PreciseScrollDeltas"):
        def _trackpad(evt):
            _, vertical = win.tk.splitlist(win.tk.call("tk::PreciseScrollDeltas", evt.delta))
            return _scroll_by(-int(vertical))

        win.bind("<TouchpadScroll>", _trackpad, add="+")
    return inner


_SETTINGS_WIN = {"win": None}


def _dir_group_single(parent):
    """「目录」分组：**只保留一个安装根**。

    2026-11-02 用户要求：「目录这里是否还能简化，只留一个路径，
    把临时文件直接放进各自 dshVX 的文件夹下」。

    设计：
      · 安装根 = 所有槽装在哪（<安装根>/dsh-versions/dshVx）
      · 各槽的临时目录 = <槽目录>/.tmp，跟着槽走，**不再需要单独配置**
      · 所以这里只显示一行安装根 + 「移动…」入口
    已装的旧槽保持原 temp_dir 不动（改它要停进程、重写 bat，风险大于收益），
    只对**新装/新纳入**的槽应用新规则。
    """
    df = ttk.LabelFrame(parent, text=T("目录"), padding=(12, 10))
    df.pack(padx=16, fill="x", pady=(0, 10))
    locked = bool(SLOT_KEYS)

    row = ttk.Frame(df)
    row.pack(fill="x")
    ttk.Label(row, text=T("安装根")).pack(side="left")
    # 2026-11-02 修 bug（用户实测：选了新位置后根目录没变化）：
    # 这里原来是固定文本，选完新位置后**不会刷新**，看起来像没生效。
    # 改成 StringVar，选择/移动后立刻更新。
    _path_var = tk.StringVar(value=os.path.normpath(INSTALL_BASE))
    ttk.Label(row, textvariable=_path_var, foreground=_MUTED
              ).pack(side="left", fill="x", expand=True)

    def _choose():
        before = INSTALL_BASE
        if _ask_install_root():
            # 保存成功后，模块级 INSTALL_BASE 已被更新，这里同步刷新显示
            try:
                _path_var.set(os.path.normpath(INSTALL_BASE))
            except Exception:
                pass
            log(T("[设置] 安装根目录已改为：%s（原：%s）") % (INSTALL_BASE, before))
            try:
                messagebox.showinfo(
                    T("安装位置已更新"),
                    T("新的安装根目录：\n%s\n\n"
                    "之后安装/纳入的版本会放这里。\n"
                    "（已经装好的版本不受影响；如需整体搬家，用「移动安装根目录…」）")
                    % os.path.normpath(INSTALL_BASE))
            except Exception:
                pass

    if locked:
        ttk.Button(df, text=T("移动安装根目录…"), command=move_install_root_dialog
                   ).pack(anchor="w", pady=(8, 0))
        _hint = ttk.Label(df,
                          text=T("换盘/换位置不用卸载：点上面这个按钮，所有版本连同数据"
                               "一起搬过去（复制 → 校验 → 改配置 → 删旧位置）。"),
                          foreground=_MUTED, justify="left")
        _hint.pack(anchor="w", pady=(4, 0))
        _auto_wrap(_hint, pad=28)
        _where = ttk.Label(df, text=T("版本根：%s\n临时文件默认位于各槽的 .tmp")
                                    % _versions_root(),
                           foreground=_MUTED, justify="left")
        _where.pack(anchor="w", pady=(6, 0))
        _auto_wrap(_where, pad=28)
    else:
        ttk.Button(df, text=T("选择安装位置…"), command=_choose
                   ).pack(anchor="w", pady=(8, 0))


def settings_hub_dialog():
    """「设置」侧栏（2026-11-02 按用户要求重新组织）。

    设计目标：**清晰、简洁**。原来的问题：
      · 顶部有"设置"大标题 + 一句引导语，占地方且没信息量
      · 「已移出管理」是低频动作，不该占一级按钮
      · 「目录」里有安装根 + 临时目录根两项，但用户只关心"装在哪"
      · 日志管理塞在「关于」里，混杂
      · 槽体检/生成 bat 属于维护动作，应与环境检查放一起

    现在的分组：
      版本管理 安装官方版 / 扫描并纳入 / 卸载 · 移出管理
      界面     显示哪些槽 / 顺序 / 浏览器、主题
      目录     只有「安装根」（临时目录自动落在各槽 .tmp 下）
      维护     检查运行环境 / 槽体检 / 生成 bat / 使用说明
      日志     大小 + 清理（独立成组）

    版本管理这一组不能省：主界面按设计只留「槽 + 日志 + 全启停」，
    装/纳入/卸载全部收在设置里。少了它，装完第一个版本就再也装不了第二个。
    """
    win = tk.Toplevel(root)
    _SETTINGS_WIN["win"] = win
    try:
        win.bind("<Destroy>",
                 lambda e: _SETTINGS_WIN.update({"win": None})
                 if e.widget is win else None, add="+")
    except Exception:
        pass
    win.title(T("设置"))
    win.resizable(True, True)
    _apply_icon(win)
    _make_side_panel(win, width=380)
    try:
        # 2026-11-14：侧栏再窄，主题那三个按钮就排不下（实测三个共需 297px，
        # 减去滚动条与两级内边距后 380 是下限）。允许拉宽，但不许拉到裁字。
        win.minsize(380, 200 if PLATFORM.IS_MAC else 480)
    except Exception:
        pass

    host = _scroll_host(win)

    # ── 版本管理（装 / 纳入 / 卸载 · 移出）────────────────────────
    # 2026-11-11 修：11-01 重构删掉了主界面第二排操作按钮，注释写着「已全部
    # 移进设置」，但设置页一直没补上这三个入口 —— 装完第一个版本后就再也
    # 装不了第二个、也卸不掉（FRESH_START 是启动快照，引导卡只在零槽出现）。
    # 这里补齐；零槽时同样可用，引导卡只是它的快捷方式。
    ops = ttk.LabelFrame(host, text=T("版本管理"), padding=(12, 10))
    ops.pack(padx=16, pady=(14, 10), fill="x")
    for _c in range(2):
        ops.columnconfigure(_c, weight=1)

    def _obtn(r, c, text, cmd, span=1):
        # 文案以**位置参数**传进来，白名单抓不到 text=，所以在函数内翻译。
        b = ttk.Button(ops, text=T(text), command=cmd)
        b.grid(row=r, column=c, columnspan=span, padx=3, pady=3, sticky="ew")
        try:
            BUSY["btns"].append(b)
        except Exception:
            pass
        return b

    _obtn(0, 0, T("安装官方版 dsh"), install_official_version)
    _obtn(0, 1, T("扫描已装 dsh 并纳入"),
          lambda: scan_and_offer_adopt())
    _obtn(1, 0, T("卸载 / 移出管理"), uninstall_versions_dialog, span=2)
    _obtn(2, 0, T("从备份恢复插件…"), restore_plugins_dialog, span=2)

    # ── 界面 ──────────────────────────────────────────────
    look = ttk.LabelFrame(host, text=T("界面"), padding=(12, 10))
    look.pack(padx=16, pady=(14, 10), fill="x")

    ttk.Button(look, text=T("显示哪些槽 / 顺序 / 浏览器…"),
               command=_settings_display_dialog).pack(fill="x", pady=(0, 8))

    # 2026-11-14 用户反馈：「主题的几个选项看着不清楚，到底选的是哪个？」
    # ① ttk.Radiobutton 的小圆点在浅色/深色下都只有几像素 → 换成三个分段按钮，
    #    选中的那个是品牌色底 + 白字 + 加粗。
    # ② 三个按钮原来用 pack(side="left") 横排，380px 的侧栏放不下 —— 第三个
    #    「深色」被压成一条缝。现在「主题」单独一行、按钮三等分占满宽度。
    _theme_var = tk.StringVar(value=_theme_mode())
    _seg_btns = {}

    def _refresh_theme_seg():
        """把「当前值」画成填充按钮 —— 主题切换后也要重刷（样式名不变，颜色会跟着换）。"""
        for _v, _b in _seg_btns.items():
            try:
                _b.configure(style=("SegOn.TButton" if _v == _theme_var.get()
                                    else "SegOff.TButton"))
            except Exception:
                pass

    def _pick_theme(_v):
        _theme_var.set(_v)
        _apply_theme_mode(_v)      # 立即生效 + 写进 config
        _refresh_theme_seg()

    trow = ttk.Frame(look)
    trow.pack(fill="x", pady=(2, 0))
    ttk.Label(trow, text=T("主题")).pack(anchor="w")
    _theme_row = ttk.Frame(trow)
    _theme_row.pack(fill="x", pady=(4, 0))
    for _c in range(3):
        _theme_row.columnconfigure(_c, weight=1, uniform="theme")
    for _i, (_val, _lbl) in enumerate((("system", T("跟随系统")),
                                       ("light", T("浅色")),
                                       ("dark", T("深色")))):
        _tb = ttk.Button(_theme_row, text=_lbl,
                         command=lambda v=_val: _pick_theme(v))
        _tb.grid(row=0, column=_i, sticky="ew",
                 padx=(0 if _i == 0 else 6, 0))
        _seg_btns[_val] = _tb
    _refresh_theme_seg()

    # ── 界面语言（2026-11-14）──────────────────────────────
    # 控件文案在**创建时**求值（`text=T("启动")`），所以切换语言没法原地刷新，
    # 必须重启。这里不偷偷重启 —— 先问一句，用户可能正开着别的事。
    _lang_row = ttk.Frame(look)
    _lang_row.pack(fill="x", pady=(10, 0))
    ttk.Label(_lang_row, text=T("界面语言")).pack(anchor="w")
    _lbtns = ttk.Frame(_lang_row)
    _lbtns.pack(fill="x", pady=(4, 0))
    for _c in range(2):
        _lbtns.columnconfigure(_c, weight=1, uniform="lang")
    _lang_var = tk.StringVar(value=I18N.lang())
    _lbtns_map = {}

    def _refresh_lang_seg():
        for _v, _b in _lbtns_map.items():
            try:
                _b.configure(style=("SegOn.TButton" if _v == _lang_var.get()
                                    else "SegOff.TButton"))
            except Exception:
                pass

    def _pick_lang(_v):
        """切换界面语言 —— **就地生效，不重启**（2026-11-14 用户反馈）。

        控件文案是创建时求值的，所以要遍历控件树重设 text（`I18N.retranslate`）。
        卡片上还有动态文本（状态行、标题里的版本号），重建一次卡片让它们也用新语言。
        """
        if _v == I18N.lang():
            return
        I18N.set_lang(_v)
        _save_ui_field("lang", _v)
        _lang_var.set(_v)
        _refresh_lang_seg()
        # 已建好的窗口逐个重译：主窗口（含全部卡片）+ 所有 Toplevel（设置窗自己）
        n = I18N.retranslate(root)
        # 卡片状态行是 textvariable，retranslate 不碰，重建卡片让它按新语言重算
        try:
            _clear_cards()
            _build_cards()
            # **零槽时 _build_cards() 会直接 return**（不画任何卡片），引导卡得由
            # _build_fresh_guide() 画。漏了这一句，英文下切语言后首页就是一片空白
            # （2026-11-14 用户实测）。_reload_config_after_change 里是三个一起调的。
            _build_fresh_guide()
        except Exception as e:
            log(T("[界面] 重建卡片失败（不影响语言切换）：%s") % e)
        try:
            _refresh_theme_seg()        # 主题三按钮的文案刚变过
            _refresh_lang_seg()
        except Exception:
            pass
        # 窗口标题不是控件的 text，retranslate 管不到，单独设一次
        for _w, _k in ((root, "DSH 管理器"), (win, "设置")):
            try:
                _w.title(T(_k))
            except Exception:
                pass
        log(T("[界面] 界面语言已切换为 %s（就地生效，%d 个控件）")
            % ("English" if _v == "en" else "中文", n))

    for _i, (_val, _lbl) in enumerate((("zh", T("中文")), ("en", "English"))):
        _lb = ttk.Button(_lbtns, text=_lbl,
                         command=lambda v=_val: _pick_lang(v))
        _lb.grid(row=0, column=_i, sticky="ew", padx=(0 if _i == 0 else 6, 0))
        _lbtns_map[_val] = _lb
    _refresh_lang_seg()

    # ── 目录（只剩一个安装根）──────────────────────────────
    try:
        _dir_group_single(host)
    except Exception as e:
        log(T("[设置] 目录分组构建失败：%s") % e)

    # ── 维护 ──────────────────────────────────────────────
    mnt = ttk.LabelFrame(host, text=T("维护"), padding=(12, 10))
    mnt.pack(padx=16, fill="x", pady=(0, 10))
    for _c in range(2):
        mnt.columnconfigure(_c, weight=1)

    def _mbtn(r, c, text, cmd):
        b = ttk.Button(mnt, text=T(text), command=cmd)
        b.grid(row=r, column=c, padx=3, pady=3, sticky="ew")
        try:
            BUSY["btns"].append(b)
        except Exception:
            pass
        return b

    _mbtn(0, 0, T("检查运行环境"), _show_env_report)
    _mbtn(0, 1, T("槽体检"), lambda: _run_health(deep=True, dialog=True))
    _mbtn(1, 0, _launcher_action_label(), gen_bats)
    _mbtn(1, 1, T("使用说明"), _open_user_guide)

    # ── 日志 ──────────────────────────────────────────────
    lg = ttk.LabelFrame(host, text=T("日志"), padding=(12, 10))
    lg.pack(padx=16, fill="x", pady=(0, 14))
    _lsz, _llines = _log_stats()
    _linfo = ttk.Label(lg, text=T("%s（%d 行）") % (_fmt_size(_lsz), _llines),
                       foreground=_MUTED)
    _linfo.pack(anchor="w", pady=(0, 8))

    def _refresh_log_info():
        sz, ln = _log_stats()
        try:
            _linfo.configure(text=T("%s（%d 行）") % (_fmt_size(sz), ln))
        except Exception:
            pass

    _lrow2 = ttk.Frame(lg)
    _lrow2.pack(fill="x")
    ttk.Button(_lrow2, text=T("打开文件夹"), command=_open_log_folder
               ).pack(side="left", expand=True, fill="x", padx=(0, 3))
    ttk.Button(_lrow2, text=T("清空…"),
               command=lambda: (_clear_logs_dialog(win), _refresh_log_info())
               ).pack(side="left", expand=True, fill="x", padx=(3, 0))

    # ── 关于项目（最下面）────────────────────────────────
    proj = ttk.Frame(host)
    proj.pack(padx=16, fill="x", pady=(4, 16))
    ttk.Button(proj, text=T("★ 在 GitHub 上收藏本项目"),
               command=_open_project_page).pack(fill="x")

    win.bind("<Escape>", lambda e: win.destroy())


def settings_dialog():
    """统一入口：主界面与各处「设置」都走这里 → 打开设置总页。

    2026-11-01：加**单例保护**。非模态之后反复点「设置」会叠出多个窗口，
    用户实测反馈"出现过几个设置页面"。已有活着的窗口就提到前面，不再新建。
    """
    if BUSY.get("installing"):
        return
    try:
        w = _SETTINGS_WIN.get("win")
        if w is not None and w.winfo_exists():
            try:
                w.deiconify()
                w.lift()
                w.focus_force()
            except Exception:
                pass
            return
    except Exception:
        pass
    try:
        settings_hub_dialog()
    except Exception as e:
        log(T("[设置] 打开设置窗口失败：%s") % e)
        messagebox.showerror(T("设置"), T("打开设置窗口失败：%s") % e)

def _settings_display_dialog():
    """设置：显示哪几个槽 / 顺序 / 浏览器 / 目录（安装根 + 临时目录根）。

    为什么「显示」类设置只改显示不改管理：隐藏只是不画卡片，进程 / 端口 /
    数据目录一律照旧，「卸载」「杀全部」的名单也不受影响。所以调布局不可能
    丢东西，也就没有「隐藏了怎么找回」这种问题 —— 这正是它敢做成即时生效的原因。
    「目录」则相反：它改的是磁盘真相，所以每一步都要先校验、后写入。
    """
    # 2026-11-01 修 bug：零槽时原来直接退到 _dirs_only_dialog（目录设置），
    # 于是点「显示哪些槽」弹出来的却是"目录"，与按钮名完全不符（用户实测反馈）。
    # 零槽时本来就没有槽可显示，正确做法是**如实说明**，而不是弹一个无关的窗。
    if not SLOT_KEYS:
        messagebox.showinfo(
            T("还没有可显示的槽"),
            T("当前没有任何版本槽，所以没有可勾选/排序的对象。\n\n"
            "先装一个版本（设置 → 版本管理 → 安装官方版 dsh），之后这里就能调整\n"
            "显示哪几个槽、它们的左右顺序。"))
        return

    order = _ui_order()                                   # 工作副本（含全部槽）
    _v = _ui_visible()
    shown = set(_v if _v is not None else order)
    maxv = _ui_max_visible()

    win = tk.Toplevel(root)
    win.title(T("设置"))
    win.transient(root)
    win.resizable(False, False)
    _apply_icon(win)

    ttk.Label(win, text=T("设置"),
              font=("Microsoft YaHei UI", 10, "bold")).pack(padx=20, pady=(16, 8),
                                                           anchor="w")

    box = ttk.LabelFrame(win, text=T("版本槽（勾选=显示 · 拖动=排序）"),
                         padding=(8, 6))
    box.pack(padx=20, fill="both", expand=True)

    side = ttk.Frame(box)
    side.pack(side="right", fill="y", padx=(8, 0))

    lb = tk.Listbox(box, height=max(4, len(order)), width=44, exportselection=False,
                    font=("Consolas", 9), activestyle="dotbox")
    lb.pack(side="left", fill="both", expand=True)

    def _row_text(k):
        cfg = VC[k]
        ver = INSTALLED.get(k) or T("未知")
        mark = "[✓]" if k in shown else "[  ]"
        return "%s %-3s  %-8s :%-5d %s" % (mark, k, cfg["label"], cfg["port"], ver)

    def _redraw(sel=None):
        lb.delete(0, "end")
        for k in order:
            lb.insert("end", _row_text(k))
        if sel is not None and 0 <= sel < len(order):
            lb.selection_set(sel)
            lb.activate(sel)
            lb.see(sel)

    def _sel():
        s = lb.curselection()
        return s[0] if s else None

    def _toggle(_evt=None):
        i = _sel()
        if i is None:
            return
        k = order[i]
        if k in shown:
            # 一个都不显示 = 界面空白，用户会以为程序坏了。宁可拦住。
            if len(shown) <= 1:
                messagebox.showwarning(T("至少留一个"),
                                       T("至少要显示一个版本槽，否则界面就空了。"))
                return
            shown.discard(k)
        else:
            shown.add(k)
        _redraw(i)

    def _move(delta):
        i = _sel()
        if i is None:
            return
        j = i + delta
        if not (0 <= j < len(order)):
            return
        order[i], order[j] = order[j], order[i]
        _redraw(j)

    ttk.Button(side, text=T("上移 ↑"), width=9,
               command=lambda: _move(-1)).pack(pady=2)
    ttk.Button(side, text=T("下移 ↓"), width=9,
               command=lambda: _move(1)).pack(pady=2)
    ttk.Button(side, text=T("显示/隐藏"), width=9,
               command=_toggle).pack(pady=(12, 2))
    lb.bind("<Double-Button-1>", _toggle)
    lb.bind("<space>", _toggle)

    # 列表里也能直接拖动排序（2026-10-31）：主窗口拖卡片是"大动作"，
    # 这里拖是"精细动作"，两者改的是同一份 order。
    # 处理完要 return "break" —— 否则 Listbox 自己的拖选会再改一次选中区，
    # 而 _redraw 已经把整个列表重建过，选中区对不上就跳到别处。
    _ldrag = {"i": None}

    def _lb_press(evt):
        i = lb.nearest(evt.y)
        _ldrag["i"] = i if 0 <= i < len(order) else None

    def _lb_move(evt):
        i = _ldrag["i"]
        if i is None or len(order) < 2:
            return "break"
        j = lb.nearest(evt.y)
        if not (0 <= j < len(order)) or j == i:
            return "break"
        k = order[i]
        if UIP is not None:
            order[:] = UIP.move_to(order, k, j)
        _ldrag["i"] = j
        _redraw(j)
        return "break"

    lb.bind("<ButtonPress-1>", _lb_press, add="+")
    lb.bind("<B1-Motion>", _lb_move, add="+")
    _redraw(0)

    opt = ttk.Frame(win)
    opt.pack(padx=20, pady=(12, 4), fill="x")
    ttk.Label(opt, text=T("界面最多同时显示")).pack(side="left")
    mv = tk.StringVar(value=str(maxv))
    tk.Spinbox(opt, from_=1, to=MAX_SLOTS, width=4, textvariable=mv,
               justify="center").pack(side="left", padx=6)
    ttk.Label(opt, text=T("个槽"), foreground=_MUTED).pack(side="left")
    ttk.Label(win, text=T("上限 %d。勾选数超过它时只显示靠前的几个 —— "
                        "想只看某几个，把不要的取消勾选即可。") % MAX_SLOTS,
              foreground=_MUTED, wraplength=470, justify="left"
              ).pack(padx=20, pady=(0, 12), anchor="w")

    # ── 目录（2026-10-31）──────────────────────────────────────────────
    # 放在浏览器之前：装在哪、临时文件在哪，比「用哪个浏览器打开」重要得多。
    ib_var, tr_var = _dir_group(win)

    # ── 每个槽用哪个浏览器打开（2026-10-31）────────────────────────────
    # 为什么放在这里而不是卡片上：卡片是「动作」的地方（启动/停止/打开），
    # 偏好属于设置。卡片上「打开 X」按钮的文案会跟着变，一眼能看出当前用哪个。
    # 默认是「系统默认浏览器」—— 每个人装的浏览器不一样，替用户挑一个没道理。
    bf = ttk.LabelFrame(win, text=T("选中的槽用哪个浏览器打开"), padding=(8, 6))
    bf.pack(padx=20, pady=(0, 10), fill="x")
    brow = ttk.Frame(bf)
    brow.pack(fill="x")
    ttk.Label(brow, text=T("当前槽：")).pack(side="left")
    bslot = ttk.Label(brow, text="-", width=5)
    bslot.pack(side="left")
    bchoice = {k: (VC[k]["browser"] or "default") for k in order}
    _bmap = {}                       # 下拉显示文本 -> browser 字段值

    def _b_values(cur):
        """重建下拉项。未安装的标注「（未安装）」；当前若是自定义 exe 就列出来。"""
        _bmap.clear()
        vals = []
        for b in BROWSER_CHOICES:
            name = BROWSER_KNOWN[b][0]
            if not _browser_installed(b):
                name += T("（未安装）")
            _bmap[name] = b
            vals.append(name)
        low = (cur or "").strip().lower()
        if cur and low not in BROWSER_KNOWN:
            d = T("其他程序：%s") % os.path.basename(cur)
            _bmap[d] = cur
            vals.append(d)
        vals.append(T(_B_OTHER))
        return vals

    def _b_display(v):
        v = (v or "default").strip()
        low = v.lower()
        if low in BROWSER_KNOWN:
            name = BROWSER_KNOWN[low][0]
            if not _browser_installed(low):
                name += T("（未安装）")
            return name
        return T("其他程序：%s") % os.path.basename(v)

    bcb = ttk.Combobox(brow, state="readonly", width=24)
    bcb.pack(side="left", padx=(8, 0))

    def _b_sync(_evt=None):
        """列表选中行变了 → 下拉跟着切到这个槽的浏览器。"""
        i = _sel()
        if i is None or not (0 <= i < len(order)):
            bslot.configure(text="-")
            bcb.configure(values=[], state="disabled")
            return
        k = order[i]
        cur = bchoice.get(k, "default")
        bslot.configure(text=k)
        bcb.configure(state="readonly", values=_b_values(cur))
        bcb.set(_b_display(cur))

    def _b_pick(_evt=None):
        i = _sel()
        if i is None:
            return
        k = order[i]
        sel = bcb.get()
        if sel == T(_B_OTHER):
            p = filedialog.askopenfilename(
                title=T("选择浏览器程序"), parent=win,
                filetypes=_browser_filetypes())
            if not p:
                _b_sync()                       # 取消 → 回到原值
                return
            p = os.path.normpath(p)
            bchoice[k] = p
            bcb.configure(values=_b_values(p))
            bcb.set(_b_display(p))
            return
        bchoice[k] = _bmap.get(sel, "default")

    lb.bind("<<ListboxSelect>>", _b_sync, add="+")
    bcb.bind("<<ComboboxSelected>>", _b_pick, add="+")
    ttk.Label(bf, text=T("「系统默认浏览器」= 你平时双击链接时用的那个。"
                       "改完点「保存」生效；卡片上的按钮会变成「打开 Chrome」这种。"),
              foreground=_MUTED, wraplength=460, justify="left"
              ).pack(anchor="w", pady=(6, 0))
    _b_sync()

    def on_ok():
        # 目录先落盘：它动的是磁盘真相，失败就不该继续保存其它偏好
        ok, dmsg = _apply_dir_settings(ib_var, tr_var)
        if not ok:
            messagebox.showerror(T("目录设置失败"), dmsg)
            return
        try:
            n = int(mv.get())
        except (TypeError, ValueError):
            n = maxv
        n = max(1, min(MAX_SLOTS, n))
        vis = [k for k in order if k in shown]
        if not vis:
            messagebox.showwarning(T("至少留一个"), T("请至少勾选一个要显示的版本槽。"))
            return
        raw = {"max_visible": n, "order": list(order), "visible": vis}
        # 收口：丢未知槽、钳上限、保证至少一个可见（逻辑在 dsh_ui_prefs，可离线测）
        prefs = (UIP.normalize(raw, VERSIONS, SLOT_KEYS, MAX_SLOTS)
                 if UIP else raw)
        err = _save_ui_prefs(prefs)
        if err:
            messagebox.showerror(T("保存失败"), err)
            return
        # 内存里的偏好同步更新，_display_keys() 立刻按新设置算
        UI_CFG.clear()
        UI_CFG.update(prefs)

        # 浏览器偏好：只写**改过**的槽，别的一次都不碰 config.json
        bchanged = []
        for k in order:
            new_b = bchoice.get(k, VC[k]["browser"])
            if (new_b or "") == (VC[k]["browser"] or ""):
                continue
            err = _save_version_field(k, "browser", new_b)
            if err:
                messagebox.showerror(T("保存浏览器设置失败"), err)
                return
            VC[k]["browser"] = new_b
            bchanged.append("%s→%s" % (k, _browser_label(new_b)))

        try:
            win.destroy()
        except Exception:
            pass
        _build_cards()
        vis = prefs["visible"]
        n = prefs["max_visible"]
        log(T("[设置] 界面显示：%s（最多 %d 个）") % ("、".join(vis), n))
        if bchanged:
            log(T("[设置] 浏览器：%s") % "、".join(bchanged))
        if len(vis) > n:
            messagebox.showinfo(T("已保存"),
                                T("已保存。勾选了 %d 个，但「最多显示 %d 个」限制下"
                                "只显示前 %d 个：\n\n%s")
                                % (len(vis), n, n, "、".join(vis[:n])))
        # 目录改动的说明放最后弹：它往往要求用户「重启」或「手工删旧目录」，
        # 盖在别的提示上面容易被忽略。
        if dmsg:
            messagebox.showinfo(T("目录已更新"), dmsg)

    btns = ttk.Frame(win)
    btns.pack(padx=20, pady=(0, 16))
    ttk.Button(btns, text=T("保存"), style="Green.TButton",
               command=on_ok).pack(side="left", padx=4)
    ttk.Button(btns, text=T("取消"), command=win.destroy).pack(side="left", padx=4)
    win.bind("<Escape>", lambda e: win.destroy())
    win.grab_set()


# 标题栏「设置」按钮：**必须**在 settings_dialog 定义之后创建 —— 否则模块执行到
# 那一行时函数还不存在 → NameError，而 .pyw 无控制台，表现为「双击没反应」。
# （这个坑本项目踩过两次，C3/C8 组断言守着。）
# 2026-11-01：标题栏只留「设置」一个入口。
# 「关于」「★ 在 GitHub 收藏」都收进设置页（用户要求：初始界面只有槽 + 日志 + 全启停）。
_b_settings = ttk.Button(_hdr, text=T("设置"), command=settings_dialog)
_b_settings.pack(side="right", padx=(0, 6))
BUSY["btns"].append(_b_settings)
# 2026-11-03 用户要求：「把在 github 上收藏还是放进设置界面，放最下面」
# 两个全启停按钮：放在「设置」左侧（pack side=right 时后 pack 的在更左边）
_b_stopall = ttk.Button(_hdr, text=T("全部停止"), style="Red.TButton",
                        command=stop_all_slots)
_b_stopall.pack(side="right", padx=(0, 6))
BUSY["btns"].append(_b_stopall)
_b_startall = ttk.Button(_hdr, text=T("全部启动"), style="Green.TButton",
                         command=start_all_slots)
_b_startall.pack(side="right", padx=(0, 6))
BUSY["btns"].append(_b_startall)
# 「槽体检」「生成 bat」「检查运行环境」在「设置 → 维护」；
# 「安装 / 纳入 / 卸载 · 移出」在「设置 → 版本管理」。标题栏只留「设置」。

# 卡片按「界面显示偏好」画。放在这里而不是定义处立即调用：
# _build_cards 里的「全隐藏」提示卡带 command=settings_dialog，
# 那时它还没定义（同上，NameError）。
_build_cards()
_build_fresh_guide()
# 插件/配置基线：只读拍一次，之后的变化写进日志（见 _check_drift）
_drift_seed()
# 启动时浅体检一次（只 stat 文件，几十毫秒，放后台线程）：槽被删过 / node 被换过
# 这类事，不该等到用户点「启动」才发现。有问题写日志 + 状态行打 ⚠，
# 一切正常只写一行。深查（依赖齐不齐）要点「槽体检」按钮才跑。
root.after(1200, lambda: _run_health(deep=False))

# 启动自愈（2026-11-01）：把"已在 versions 里、但没进 ui.visible"的槽补进去。
# 覆盖历史遗留 —— 之前安装流程不会同步白名单，老版本装出来的槽至今可能看不见。
# 只读 config，没有缺失就什么都不写。
if not FRESH_START:
    try:
        # 2026-11-03 修自愈失效：
        # 原来用 `_display_keys()` 判断"哪些槽没显示" —— 但那个函数**本身就是
        # 按 ui.visible 过滤后的结果**，拿它比等于用过滤结果检验过滤本身，
        # 永远查不出缺失（实测：v1 在 versions 里、不在 visible 里，自愈没触发）。
        # 改为直接读 config 的 ui.visible 原始值来比。
        _ui_raw = (CFG.get("ui") or {}) if isinstance(CFG.get("ui"), dict) else {}
        _vis_raw = _ui_raw.get("visible")
        if isinstance(_vis_raw, list):
            _missing = [k for k in SLOT_KEYS if k not in set(_vis_raw)]
        else:
            _missing = []          # 没有 visible 键 = 全显示，无需补
        if _missing:
            _err = _ensure_slots_visible(_missing)
            if not _err:
                log(T("[配置] 已补全界面显示列表：%s（此前不在 ui.visible 里）")
                    % "、".join(_missing))
            else:
                log(T("[配置] 补全界面显示列表失败：%s") % _err)
    except Exception as _e:
        log(T("[配置] 启动自愈 ui.visible 时出错（忽略）：%s") % _e)

if FRESH_START:
    log(T("[引导] 零槽启动：还没有任何版本槽，显示安装引导"))
    # 零槽时自动把环境体检结果摆出来，用户装之前就知道缺什么
    root.after(600, lambda: _log_fresh_env())

def _log_fresh_env():
    """零槽启动时把环境体检摘要写进日志（界面不打扰，想看滚日志即可）。"""
    _start_env_report("summary")

# ── 主题（2026-11-01）───────────────────────────────────────────────
# 必须放在文件末尾：_apply_palette() 定义在下方，而这里要**调用**它；
# 同时它还要触碰 log_text / cards_frame，那些控件也得先建好。
# （2026-11-01 实测踩到：放在控件创建处调用 → name '_apply_palette' is not defined）
try:
    _apply_palette()
    log(T("[主题] 生效：%s（模式=%s）") % (_resolved_theme(), _theme_mode()))
except Exception as _e:
    log(T("[主题] 应用失败（回退默认配色）：%s") % _e)

if PLATFORM.IS_MAC:
    # Keep Tk's native signal handler from destroying Python-bound widgets
    # while the main loop has released the GIL.
    signal.signal(signal.SIGTERM, lambda _signum, _frame: _post_ui(on_close))
root.after(0, _drain_ui_queue)
root.mainloop()
