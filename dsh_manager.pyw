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

    _msg = ("dsh 版本管理器需要 Python 的 tkinter 模块，但它不可用。\n\n"
            "原因：%s\n\n"
            "解决办法（任选其一）：\n"
            "  1) 双击桌面快捷方式 / start-manager.bat（它们会用包内自带的解释器）\n"
            "  2) Windows：重新安装 python.org 官方版 Python（勾选 tcl/tk 组件）\n"
            "  3) Debian/Ubuntu：sudo apt install python3-tk\n\n"
            "本程序需要 Python 3.8 或更高版本。" % _e)
    try:
        import ctypes as _ct
        _ct.windll.user32.MessageBoxW(0, _msg, "dsh 版本管理器", 0x10)
    except Exception:
        pass
    _sys.stderr.write(_msg + "\n")
    _sys.exit(1)
import subprocess, os, time, socket, json, sys, shutil, threading, re, csv, io

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
EXAMPLE_PATH = os.path.join(BASE_DIR, "config", "config.example.json")
LOG_FILE    = os.path.join(BASE_DIR, "dsh-manager.log")
# 管理器自己的入口文件。重启要重开它 —— **不能依赖 __file__**：
# pythonw 下 __file__ 可能是相对路径，destroy() 之后更不安全（2026-11-01 实测
# 「说重启但没重启」的根因之一）。这里在模块加载时就固化成绝对路径。
MANAGER_ENTRY = os.path.abspath(__file__)

# 项目主页（2026-11-01）：界面上「★ 在 GitHub 收藏」按钮指向这里。
# 只改这一处即可换仓库地址。
PROJECT_URL = "https://github.com/arnen7000/dsh-tm"

CREATE_NO_WINDOW = 0x08000000   # 隐藏 node/taskkill 子进程的控制台窗口

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
    messagebox.showerror("DSH 版本管理器", msg)
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
            shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as _f:
                CFG = json.load(_f)
            print("[info] 未找到 config.json，已用 config/config.example.json 自动生成默认配置。")
        except Exception as e:
            _fatal("找不到配置文件：\n%s\n\n且无法从示例自动生成：%s" % (CONFIG_PATH, e))
    else:
        _fatal("找不到配置文件：\n%s\n\n且示例 config/config.example.json 也不存在（仓库可能不完整）。" % CONFIG_PATH)
except Exception as e:
    # 解析失败也自愈：备份坏文件、再从示例重建。
    # 否则 config.json 一旦被写成半截，管理器就再也打不开了。
    _bad = "%s.bad-%s" % (CONFIG_PATH, time.strftime("%Y%m%d-%H%M%S"))
    try:
        shutil.move(CONFIG_PATH, _bad)
    except Exception:
        _bad = "(备份失败)"
    if os.path.exists(EXAMPLE_PATH):
        try:
            shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as _f:
                CFG = json.load(_f)
            _msg = ("config.json 解析失败：%s\n\n"
                    "原文件已备份为：\n%s\n\n"
                    "现在用的是【示例模板】路径（不是你的真实配置）——\n"
                    "请把 config.json 里的版本号/路径/端口改回来，"
                    "或参考上面的备份文件。"
                    % (e, _bad))
            print("[warn] " + _msg.replace("\n\n", " | "))
            _boot_log("[warn] " + _msg.replace("\n\n", " | "))
            # pythonw 没有控制台，print 没人看得见 —— 必须弹框，否则就是静默替换配置
            try:
                root_warn = tk.Tk(); root_warn.withdraw()
                messagebox.showwarning("config.json 解析失败（已用示例重建）", _msg)
                root_warn.destroy()
            except Exception:
                pass
        except Exception as e2:
            _boot_log("[fatal] 配置文件解析失败：%s（无法从示例重建：%s，备份在 %s）" % (e, e2, _bad))
            _fatal("配置文件解析失败：%s\n且无法从示例重建：%s\n坏文件备份在：%s"
                   % (e, e2, _bad))
    else:
        _boot_log("[fatal] 配置文件解析失败：%s（坏文件已备份为 %s，"
                  "且找不到 config/config.example.json，无法自动重建）" % (e, _bad))
        _fatal("配置文件解析失败：%s\n坏文件已备份为 %s\n"
               "且找不到 config/config.example.json，无法自动重建。" % (e, _bad))

HOME = os.path.expanduser("~")
# <self> = 本文件（dsh_manager.pyw）所在目录 = 整个管理器/离线包根目录。
# 离线包用它做占位符，就能整目录搬家用（拷到 U 盘、换盘符都不会断链）。
INSTALL_BASE = os.path.expanduser(
    str(CFG.get("install_base", "<home>"))
    .replace("<self>", BASE_DIR).replace("<home>", HOME))

# <npm_global> 解析一次（定位全局 dsh 的 lib/bin.js）
def _probe_npm_global():
    """探测 npm 全局包目录，优先返回**真正装着 @deepseek-ai/dsh** 的那个。
    （直接信 npm root -g 有坑：它可能命中被 PATH 污染的其它 node，或压根探测不到。）"""
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
    p = p.replace("<self>", BASE_DIR)
    p = p.replace("<base>", INSTALL_BASE).replace("<home>", HOME)
    if "<npm_global>" in p:
        p = p.replace("<npm_global>", NPM_GLOBAL or "")
    return p

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
    _fatal("config.json 的 versions 最多支持 %d 个槽（当前 %d 个：%s）。\n"
           "想同时用更多版本，请让管理器只管 %d 个，其余用外部启动器。"
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
VC = {}
for _k, _v in VERSIONS.items():
    VC[_k] = {
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
    返回不一致的槽位列表（空 = 全一致）。"""
    bad = []
    for key, cfg in VC.items():
        want = cfg["port"]
        args = cfg["extra_args"]
        try:
            i = args.index("--port")
            got = int(args[i + 1])
        except (ValueError, IndexError):
            bad.append((key, "extra_args 里没有 --port（实际监听端口无法确定）"))
            continue
        if got != want:
            bad.append((key, "port=%d 但 extra_args --port=%d（实际会起在 %d）"
                        % (want, got, got)))
    return bad

SIG = {k: _make_sig(cfg) for k, cfg in VC.items()}

# 端口双源校验：port 字段只用于预检/状态显示，extra_args --port 才真正决定监听端口。
# 两者不一致时界面会永远显示「已停止」而日志看不出异常，所以在这里直接挡住。
PORT_MISMATCH = _check_port_consistency()
if PORT_MISMATCH:
    _fatal("config.json 里 port 与 extra_args 的 --port 不一致：\n\n%s\n\n"
           "port 只用于端口预检和状态显示，真正决定监听端口的是 extra_args 里的 --port。\n"
           "两者不一致会导致预检查的端口和实际启动的端口不同，界面状态永远不更新。\n"
           "请把两处改成同一个端口（可参考 config/config.example.json）。"
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
log("--- 解释器起来了 pid=%d exe=%s cwd=%s（下面开始建窗口）---"
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
        log("!! 仍运行在商店版 Python 上（重启用），任务栏图标可能显示 Python")
        return
    if not os.path.exists(STANDALONE_PYW):
        log("!! 当前是商店版 Python 且独立解释器缺失：%s —— 任务栏图标会被劫持" % STANDALONE_PYW)
        return
    env = dict(os.environ)
    env["DSH_MANAGER_RELAUNCHED"] = "1"
    try:
        subprocess.Popen([STANDALONE_PYW, os.path.abspath(__file__)],
                         cwd=BASE_DIR, env=env,
                         creationflags=CREATE_NO_WINDOW)
        log("检测到商店版 Python（任务栏图标会被劫持为 Python 图标），"
            "已自动用独立解释器重启：%s" % STANDALONE_PYW)
        os._exit(0)                 # GUI 尚未创建，直接退出无副作用
    except Exception as e:
        log("自动切换独立解释器失败：%s" % e)

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
        pid = int(open(lock, "r").read().strip())
    except Exception:
        pid = None
    if pid is None:
        stale, why = True, "内容不是 PID"
    elif not _pid_alive(pid):
        stale, why = True, "持有者已死"
    elif not _holder_is_this_slot(key, pid):
        stale, why = True, "pid=%d 存活但不是本槽 dsh（PID 复用）" % pid
    else:
        stale, why = False, ""
    if stale:
        try:
            os.remove(lock)
            log("[%s] 清理陈旧的 .credentials.yaml.lock (pid=%s，%s)" % (key, pid, why))
        except Exception as e:
            log("[%s] 清理 lock 失败：%s" % (key, e))
    else:
        log("[%s] lock 被存活的本槽 dsh 持有 (pid=%s)，不动" % (key, pid))


def _holder_is_this_slot(key, pid):
    """锁的持有者是本槽的 dsh 吗？用命令行签名核对。
    dsh_kill 不可用/查询失败时一律判为"是"，宁可漏清也不误删。
    commandline 偶发为空（CIM 未返回）时降级为 node 进程判定，避免误判「非本槽」
    而漏清陈旧 lock、卡住下次启动。"""
    if dsh_kill is None:
        return True
    try:
        cmd = (dsh_kill._node_map() or {}).get(int(pid), "")
        if SIG[key](cmd):
            return True
        # commandline 取空 -> 降级：确为 node 进程则视为本槽 dsh
        return dsh_kill._is_node_exe(pid)
    except Exception:
        return True

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
    L.append('  "%%PRECHK_PY%%" "%%PRECHK%%" --port %s --precheck-start %s' % (port, key))
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
    L.append('"%PY%" "%KILL%" --port %PORT%')
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
            return (False, "备份失败：%s" % e)
    try:
        # GBK：cmd.exe 的 ANSI 代码页。newline="" 保证我们写进去的 \r\n 不被二次转换。
        with open(path, "w", encoding="gbk", newline="") as f:
            f.write(text)
    except UnicodeEncodeError as e:
        # 理论不可达（GBK 覆盖全部常用中文），但真发生时要能说清楚原因
        return (False, "GBK 编码失败：%s" % e)
    except Exception as e:
        return (False, "写入失败：%s" % e)
    return (True, bak)

def _rebuild_bats():
    """重写所有槽的三件 bat（不弹框）。返回 (made, failed, skipped) 三个列表。

    抽出来是因为「临时目录改了要重建 bat」这种场景不该再弹一次汇总框 ——
    设置对话框自己会报结果。gen_bats() 只是它 + 一个 messagebox。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    made, failed, skipped = [], [], []
    for key in SLOT_KEYS:
        cfg = VC[key]
        root = _version_root(cfg)
        if not root or not os.path.isdir(root):
            skipped.append("%s（找不到版本目录：%s）" % (key, root))
            continue
        port = cfg.get("port")
        jobs = [
            (os.path.join(root, "dsh.bat"),        _bat_text_start(key, cfg, root), "启动"),
            (os.path.join(root, "kill-%s.bat" % port), _bat_text_kill(key, cfg, root), "kill"),
            (os.path.join(root, "dsh-cli.bat"),   _bat_text_cli(key, cfg, root), "CLI"),
        ]
        for path, text, kind in jobs:
            ok, info = _write_bat(path, text, stamp)
            if ok:
                made.append("%s [%s] %s" % (key, kind, path))
                log("[%s] 已生成 %s bat：%s%s"
                    % (key, kind, path, ("（原文件已备份为 %s）" % os.path.basename(info)) if info else ""))
            else:
                failed.append("%s [%s] %s —— %s" % (key, kind, path, info))
                log("[%s] 生成 %s bat 失败：%s" % (key, kind, info))
    return made, failed, skipped


def gen_bats():
    """为每个版本生成 启动 bat / kill bat / CLI bat。"""
    made, failed, skipped = _rebuild_bats()
    # 结果提示
    msg = []
    if made:
        msg.append("已生成 %d 个文件：" % len(made))
        msg.extend("  " + m for m in made)
    if skipped:
        msg.append("")
        msg.append("跳过 %d 个：" % len(skipped))
        msg.extend("  " + s for s in skipped)
    if failed:
        msg.append("")
        msg.append("失败 %d 个：" % len(failed))
        msg.extend("  " + f for f in failed)
    if not msg:
        msg.append("没有可生成的版本。")
    body = "\n".join(msg)
    log("=== 生成 bat 完成 ===\n" + body)
    try:
        messagebox.showinfo("生成启动/kill/CLI bat", body)
    except Exception:
        pass

def start_one(key):
    cfg = VC[key]
    if proc_alive(procs[key]):
        log("[%s] 启动忽略：已在运行 pid=%s" % (key, procs[key].pid)); return
    if port_open(cfg["port"]):
        # 端口被占用：先说清「是谁」再决定动作，避免误伤别家程序。
        if dsh_kill is None:
            hint = ("端口 %d 被占用，但无法识别占用者（dsh_kill 未加载）。\n"
                    "请手动关闭占用程序，或改 config.json 里该版本的 port 后重启管理器。" % cfg["port"])
            log("[%s] 启动失败：%s" % (key, hint))
            try:
                messagebox.showwarning("端口被占用", "[%s]\n%s" % (key, hint))
            except Exception:
                pass
            return
        owners, text = dsh_kill.format_port_owners(cfg["port"])
        dsh_o = [o for o in owners if o["kind"] in ("dsh", "dsh-ambiguous")]
        other_o = [o for o in owners if o["kind"] not in ("dsh", "dsh-ambiguous")]
        if other_o:
            # 非 dsh 程序占了端口：绝不自动结束，给详细提示 + 不启动
            hint = (text + "\n\n这不是 dsh 进程。为避免影响其它程序，管理器不会自动结束它。\n"
                    "请手动关闭占用程序，或修改 config.json 里该版本的 port 后重启管理器。")
            log("[%s] 启动失败：端口被非 dsh 程序占用 ->\n%s" % (key, text))
            try:
                messagebox.showwarning("端口被占用（非 dsh 程序）", "[%s]\n%s" % (key, hint))
            except Exception:
                pass
            return
        # 占用者是自家 dsh（旧实例 / 外部用 bat 拉起）：提示后清理再启动
        log("[%s] 端口被自家 dsh 占用，准备先清理再启动 ->\n%s" % (key, text))
        try:
            ok = messagebox.askyesno("端口被你的 dsh 占用",
                "[%s]\n%s\n\n是否结束该 dsh 进程并启动新实例？" % (key, text))
        except Exception:
            ok = True  # GUI 不可用则默认继续（等价于原行为：直接清掉重启）
        if not ok:
            log("[%s] 用户取消：保留现有 dsh，不启动新实例。" % key)
            return
        kill_one(key)
        # 等端口释放（最多 ~6s），再走下面的启动逻辑
        for _ in range(12):
            if not port_open(cfg["port"]):
                break
            time.sleep(0.5)
    env = None
    if cfg["dsh_home"]:
        env = {**os.environ, "DSH_HOME": cfg["dsh_home"]}
        # --- TEMP 隔离：只注入给本版本的子进程，不动系统/用户级环境变量 ---
        td = (cfg.get("temp_dir") or "").strip()
        if td:
            td = resolve(td)
            try:
                os.makedirs(td, exist_ok=True)
                env["TEMP"] = td
                env["TMP"] = td
                log("[%s] TEMP 已隔离 -> %s" % (key, td))
            except OSError as e:
                # 建不出来就退回继承，绝不因此拦住启动
                log("[%s] 警告：TEMP 目录不可用（%s），退回继承默认 TEMP" % (key, e))
        # -------------------------------------------------------------------
        _clean_stale_lock(key)
        log("[%s] 启动 env: DSH_HOME=%s TEMP=%s"
            % (key, cfg["dsh_home"], env.get("TEMP", "(继承)")))
    # 入口 URL 的扫描与 dsh_home 无关（见 VC 里的说明）：每个槽都布防，抓到就用。
    auth_urls[key] = None
    _scan_pos[key] = os.path.getsize(LOG_FILE) if LOG_FILE and os.path.exists(LOG_FILE) else 0
    cmd = _build_cmd(key)
    log("[%s] START: %s" % (key, " ".join(cmd)))
    try:
        p = subprocess.Popen(cmd, env=env, stdout=LOG_FH, stderr=subprocess.STDOUT,
                             creationflags=CREATE_NO_WINDOW)
        procs[key] = p
        log("[%s] 已启动 pid=%s (port %d)" % (key, p.pid, cfg["port"]))
    except FileNotFoundError as e:
        log("[%s] 启动失败（找不到可执行文件）：%s" % (key, e))
        log("[%s] 自检：node_exe=%r 存在=%s | bin_js=%r 存在=%s（不存在说明该版本没装好，重新安装该槽）"
            % (key, cfg["node_exe"], os.path.exists(cfg["node_exe"]),
               cfg["bin_js"], os.path.exists(cfg["bin_js"])))
        # 必须弹框：pythonw 没有控制台，只写日志的话用户点了「启动」看着像没反应。
        _notify_start_failure(key, "找不到可执行文件：%s" % e,
                              "node_exe：%s（存在=%s）\nbin_js：%s（存在=%s）\n\n"
                              "该版本可能没装好，或路径变了。修好 config.json 后重试。"
                              % (cfg["node_exe"], os.path.exists(cfg["node_exe"]),
                                 cfg["bin_js"], os.path.exists(cfg["bin_js"])))
    except Exception as e:
        log("[%s] 启动失败：%s" % (key, e))
        _notify_start_failure(key, "%s" % e, str(e))

def _notify_start_failure(key, brief, detail):
    """启动失败时弹框（pythonw 下这是用户唯一的反馈渠道）。
    GUI 不可用时静默跳过——此时日志仍是唯一记录，不能因弹框失败而中断。"""
    log("[%s] 启动失败（已弹框告知用户）：%s" % (key, brief))
    try:
        messagebox.showerror("启动失败 [%s]" % key, "%s\n\n%s" % (brief, detail))
    except Exception:
        pass

def _kill_pid_tree(pid, tag):
    """降级用（dsh_kill 未加载时）：只杀本 app 跟踪的进程树。"""
    rc, out, _ = run_capture(["taskkill", "/PID", str(pid), "/T", "/F"])
    log("[%s] taskkill pid=%d rc=%d %r" % (tag, pid, rc, out.strip()))

def _pids_on_port(port):
    """端口 -> LISTENING 的 PID。必须按列解析：
    子串匹配 ":3080" 会同时命中 :30800、:30801，也会命中远端地址列。"""
    pids = set()
    try:
        _rc, out, _ = run_capture(["netstat", "-ano"])
        for line in out.splitlines():
            if "LISTENING" not in line:
                continue
            parts = line.split()
            if len(parts) < 5 or ":" not in parts[1]:
                continue
            try:
                if int(parts[1].rsplit(":", 1)[-1]) != port:
                    continue
                pids.add(int(parts[-1]))
            except ValueError:
                pass
    except Exception:
        pass
    return pids

def _dsh_pid_on_port(key):
    """返回占用本槽端口的 dsh PID；没有则返回 None。
    用途：管理器重启后 procs 全空，用来识别「外部 / 上一会话启动的 dsh 仍在运行」，
    从而显示「运行中」而非「外部占用」。判据 = 端口 LISTENING 且 PID 是 dsh 进程
    （dsh_kill._looks_like_dsh：node + 命令行含 @deepseek-ai/dsh），覆盖绝对路径
    与相对路径（桌面 dsh.bat）启动两种形态；「别的软件占了端口」不会误判成 dsh 在跑。"""
    cfg = VC[key]
    if dsh_kill is None:
        return None
    # 用缓存版探测（node_map TTL 8s / pid_ports TTL 2s；CIM 实测一次 ~4s，
    # TTL 必须大于它否则缓存永远过期）。外部 dsh 的识别最多延迟一个 TTL 窗口，
    # 换来轮询不再每次跑 netstat + CIM。杀进程（kill_one/kill_all）走 dsh_kill
    # 的无缓存路径，不受影响。
    m = dsh_kill._node_map_cached()
    pp = dsh_kill._pid_ports_cached()
    for pid, ports in pp.items():
        if cfg["port"] in ports and (dsh_kill._looks_like_dsh(m.get(pid, ""))
                                     or dsh_kill._is_node_exe(pid)):
            return pid
    return None

def kill_one(key):
    """停止某版本：委托 dsh_kill 按「签名 + 端口」精准清理，
    对外部拉起的孤儿进程也能清，且绝不误杀其它 app / 另一 dsh。

    停止后**复查端口**：清理可能失败（进程没杀掉 / 权限不足 / 端口被别的程序
    占着）。此时若静默返回，用户看到的是「点了停止，状态还是运行中」，只能自己
    开任务管理器猜。复查逻辑见 _report_port_after_kill。
    """
    port = VC[key]["port"]
    p = procs.get(key)
    tracked = p.pid if (p is not None and p.poll() is None) else None
    if dsh_kill is None:
        # 降级：只杀本 app 跟踪的进程树（仍不误杀）
        if tracked is not None:
            log("[%s] 停止：dsh_kill 未加载，退化为杀 tracked 进程树 pid=%d" % (key, tracked))
            _kill_pid_tree(tracked, key)
        else:
            log("[%s] 停止：dsh_kill 未加载且无 tracked 进程" % key)
        procs[key] = None
        _report_port_after_kill(key, port, None)
        return
    killed = dsh_kill.kill_version(key, SIG[key], port, tracked_pid=tracked)
    procs[key] = None
    _report_port_after_kill(key, port, killed)

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
        log("[%s] 停止后复查端口失败：%s" % (key, e))
        return
    if not owners:
        log("[%s] 停止完成：端口 %d 已释放%s"
            % (key, port, ("（清理 %d 个进程）" % killed) if killed else ""))
        return

    dsh_left = [o for o in owners if o["kind"] in ("dsh", "dsh-ambiguous")]
    other_left = [o for o in owners if o["kind"] not in ("dsh", "dsh-ambiguous")]

    if dsh_left and not other_left:
        log("[%s] 停止后端口 %d 仍被自家 dsh 占用：%s"
            % (key, port, ", ".join("pid=%d" % o["pid"] for o in dsh_left)))
        try:
            messagebox.showwarning(
                "停止未完全成功 [%s]" % key,
                "已发出停止指令，但端口 %d 仍被 dsh 进程占用：\n\n%s\n\n"
                "通常是进程尚未退出，或存在子进程残留。\n"
                "可再点一次「停止」；反复不行就点「全部停止」，"
                "或用命令行：python dsh-tm.py kill-stray" % (port, text))
        except Exception:
            pass
        return

    # 端口被非 dsh 程序占着：最需要用户知道的情况（多半是别的软件抢了端口）
    log("[%s] 停止后端口 %d 被非 dsh 程序占用：\n%s" % (key, port, text))
    try:
        messagebox.showwarning(
            "端口被其它程序占用 [%s]" % key,
            "dsh 已停止，但端口 %d 仍被其它程序占用——所以它不会随 dsh 一起消失。\n\n%s\n\n"
            "管理器不会自动结束别人的程序（避免误杀）。\n"
            "请自行决定：关闭那个程序，或在 config.json 里把该槽的 port 改成别的"
            "（记得同步 extra_args 里的 --port）。" % (port, text))
    except Exception:
        pass

def stop_all_slots():
    """「全部停止」：把**本管理器管的**每个槽挨个停一遍。

    与「杀全部进程」的区别（两个按钮都保留，因为用途不同）：
      · stop_all_slots —— 只停 config 里这几个槽（按签名 + 端口门控），
        语义就是"我今天不调试了，全部收工"；不碰槽外任何 dsh。
      · kill_all      —— 槽外兜底，连"没登记进 config 的 dsh 副本"也一起杀，
        用于"端口被某个我不知道的 dsh 占着"的排障场景。

    为什么界面只做「全部停止」不做「全部启动」：
    正常使用下并不需要同时开满所有版本（多开反而浪费内存与端口），
    真正需要**批量启动**的是自动化/脚本场景 —— 那走 CLI（`dsh-tm.py start --all`）。
    把批量启动放进界面，等于鼓励用户误开一堆之后还要手动关。
    """
    running = []
    for k in SLOT_KEYS:
        try:
            if procs.get(k) is not None and proc_alive(procs.get(k)):
                running.append(k)
                continue
        except Exception:
            pass
        # 外部/上一会话启动的：端口在听就算
        try:
            if port_open(VC[k]["port"]):
                running.append(k)
        except Exception:
            pass
    if not running:
        messagebox.showinfo("全部停止", "当前没有任何槽在运行。")
        return
    if not messagebox.askyesno(
            "全部停止",
            "将停止以下 %d 个槽：\n\n%s\n\n"
            "只停管理器管的这些槽（不碰其它 dsh 安装）。"
            % (len(running), "、".join(VC[k]["label"] for k in running))):
        return
    log("=== 全部停止：%s ===" % "、".join(running))
    _set_busy(True)
    failed = []
    try:
        for k in running:
            try:
                kill_one(k)
            except Exception as e:
                failed.append("%s（%s）" % (k, e))
    finally:
        _set_busy(False)
    refresh(once=True)
    if failed:
        messagebox.showwarning("全部停止", "以下槽未能停止：\n\n" + "\n".join(failed))
    else:
        log("[全部停止] 完成：%d 个槽已发出停止指令" % len(running))

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
        messagebox.showinfo("全部启动", "当前没有任何槽可启动。")
        return

    started, skipped, blocked = [], [], []
    for k in keys:
        cfg = VC[k]
        try:
            if proc_alive(procs.get(k)) or port_open(cfg["port"]):
                # 已在跑（含外部/上一会话启动的）
                if port_open(cfg["port"]):
                    skipped.append(k)
                    continue
            # 端口被占但不是 dsh 在听 → 查是不是别家程序
            if port_open(cfg["port"]):
                owners = []
                if dsh_kill is not None:
                    try:
                        owners, _t = dsh_kill.format_port_owners(cfg["port"])
                    except Exception:
                        owners = []
                if not any(o.get("kind") in ("dsh", "dsh-ambiguous") for o in owners):
                    blocked.append("%s（端口 %d 被非 dsh 程序占用）" % (k, cfg["port"]))
                    continue
                skipped.append(k)
                continue
            _spawn_slot_silent(k)
            started.append(k)
        except Exception as e:
            blocked.append("%s（%s）" % (k, e))

    if started:
        log("=== 全部启动：%s ===" % "、".join(started))
    if skipped:
        log("[全部启动] 已在运行，跳过：%s" % "、".join(skipped))
    for _b in blocked:
        log("[全部启动] 未能启动：%s" % _b)

    if blocked:
        messagebox.showwarning(
            "全部启动",
            "以下槽未能启动：\n\n  " + "\n  ".join(blocked) +
            "\n\n非 dsh 程序占用的端口不会自动结束，请手动处理或改端口。")
    elif not started:
        messagebox.showinfo("全部启动", "所有槽都已经在运行了。")
    else:
        log("[全部启动] 已发出 %d 个启动指令，等待端口监听…" % len(started))
    refresh(once=True)


def _spawn_slot_silent(key):
    """不带任何弹框地拉起一个槽（供批量启动用）。

    只做"能起就起"：不复用 start_one 的交互逻辑，避免批量时弹一堆窗。
    失败直接抛异常，由调用方汇总。
    """
    cfg = VC[key]
    cmd = _build_cmd(key)
    env = os.environ.copy()
    env["DSH_HOME"] = cfg["dsh_home"] or os.path.join(HOME, ".dsh")
    td = cfg.get("temp_dir")
    if td:
        try:
            os.makedirs(td, exist_ok=True)
        except Exception:
            pass
        env["TEMP"] = td
        env["TMP"] = td
    flags = CREATE_NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    p = subprocess.Popen(cmd, cwd=BASE_DIR, env=env,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=flags)
    procs[key] = p
    log("[%s] 已启动 pid=%s 端口=%d" % (key, p.pid, cfg["port"]))


def kill_all():
    """杀全部 = 所有 DSH 相关的 node 进程：
    两个槽各自的签名（槽内，精确到安装目录）+ 泛签名兜底（槽外：别的安装位置、
    不是 bin.js 拉起的子进程）。泛签名只认 @deepseek-ai/dsh，绝不碰其他 node。"""
    if dsh_kill is None:
        log("=== 杀全部：dsh_kill 未加载，退化为杀 tracked 进程树 ===")
        for k in SLOT_KEYS:
            kill_one(k)
        return
    dsh_kill.kill_all({k: {"sig": SIG[k], "port": VC[k]["port"]} for k in SLOT_KEYS},
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
BROWSER_KNOWN = {
    "chrome":  ("Chrome",  CHROME_CANDIDATES),
    "edge":    ("Edge",    EDGE_CANDIDATES),
    "firefox": ("Firefox", FIREFOX_CANDIDATES),
    "default": ("默认浏览器", []),
}

# 「设置」里下拉框的顺序：默认浏览器排第一 —— 这是大多数人的选择，
# 也是没写 browser 字段时的行为。
BROWSER_CHOICES = ["default", "chrome", "edge", "firefox"]
# 「设置」下拉里的最后一项：不是值，是个动作（弹文件选择框挑任意 exe）。
_B_OTHER = "选择其他程序…"

def _browser_label(browser):
    """按钮/标题显示名：chrome→Chrome、edge→Edge、firefox→Firefox、
    绝对路径→该文件名、default/空→浏览器。

    注意 default 返回的是「浏览器」而不是「默认浏览器」：按钮文案是
    「打开 %s」，写全会变成「打开 默认浏览器」六个字，在 240px 的卡片里
    按钮列放不下会被切。"""
    b = (browser or "").strip().lower()
    if b in BROWSER_KNOWN:
        return "浏览器" if b == "default" else BROWSER_KNOWN[b][0]
    if b:
        return os.path.basename(browser.strip())
    return "浏览器"


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
    """只处理【本槽端口】的入口行：把完整 URL 抓进内存，并把落盘的 token 就地等长掩码。
    预留 128 字节重叠，避免 token 恰好被读块边界截断而漏掩。"""
    if _scan_pos[key] is None or LOG_FH is None:
        return
    try:
        sz = os.path.getsize(LOG_FILE)
        if sz <= _scan_pos[key]:
            return
        start = _scan_pos[key]
        blob_start = max(0, start - 128)
        with open(LOG_FILE, "rb") as f:
            f.seek(blob_start)
            blob = f.read()
        _scan_pos[key] = blob_start + len(blob)
    except Exception:
        return

    marker = ("dsh web: http://127.0.0.1:%d" % VC[key]["port"]).encode()
    pos = 0
    while True:
        i = blob.find(marker, pos)
        if i < 0:
            return
        j = blob.find(b"\n", i)
        line_end = j if j >= 0 else len(blob)
        line = blob[i:line_end]
        pos = line_end + 1
        if auth_urls.get(key) is None:
            m = re.search(rb"https?://\S+", line)
            if m:
                auth_urls[key] = m.group(0).decode("utf-8", "replace")
                log("[%s] 已捕获入口 URL（日志里的 token 已就地隐去）" % key)
        for tm in _TOKEN_BYTES_RE.finditer(line):
            tok = tm.group(2)
            if len(tok) <= 4:
                continue
            _mask_span(blob_start + i + tm.start(2) + 4, len(tok) - 4)

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

def open_browser(key):
    cfg = VC[key]
    url = auth_urls.get(key)
    if not url:
        if proc_alive(procs[key]):
            log("[%s] 仍在启动：入口 URL 还没生成，状态变 ✓ 后再点" % key); return
        if not port_open(cfg["port"]):
            log("[%s] 未运行，请先启动" % key); return
        # 在监听但不是本管理器拉起的：抓不到 URL，只能给裸地址
        url = "http://127.0.0.1:%d" % cfg["port"]
        log("[%s] 未捕获到入口 URL（该实例不是本管理器启动的）；"
            "若该版本有 token 保护，请从管理器重启它" % key)
    kind, exe = _resolve_browser(cfg["browser"])
    if kind == "start":
        # 两种情形都落到这里：① 配置就是 default（默认值）；② 配了某个浏览器
        # 但本机没装。两者都交给系统默认浏览器，日志要分清，否则用户会以为
        # 「我没配 Chrome 啊，怎么提示找不到 Chrome」。
        # 各槽即使用同一个浏览器也不串台：cookie 绑定 host:port，端口不同就隔离。
        _b = (cfg["browser"] or "default").strip()
        if _b.lower() in ("", "default"):
            log("[%s] 用系统默认浏览器打开 %s" % (key, url))
        else:
            log("[%s] 本机没找到 %s，改由系统默认浏览器打开 %s" % (key, _b, url))
        try:
            os.startfile(url)
            log("[%s] 浏览器已启动（系统默认）" % key)
        except Exception as e:
            log("[%s] 系统默认浏览器打开失败：%s（请手动复制上面的 URL 到浏览器）" % (key, e))
        return
    log("[%s] 打开浏览器(%s) -> %s" % (key, cfg["browser"], url))
    try:
        subprocess.Popen([exe, url])                 # GUI 浏览器绝不能加 CREATE_NO_WINDOW
        log("[%s] 浏览器已启动" % key)
    except Exception as e:
        log("[%s] 浏览器启动失败：%s" % (key, e))

def copy_url(key):
    """把该槽的完整入口 URL（含 token）复制到剪贴板。

    日志里的 token 已经就地打码，所以"从日志里复制 URL"这条路没有了；
    完整 URL 一直保存在内存的 auth_urls 里，用这个按钮取，既能粘到任意浏览器
    / 别的机器，也不会让 token 落到磁盘上。"""
    cfg = VC[key]
    url = auth_urls.get(key)
    if not url:
        if proc_alive(procs[key]) or port_open(cfg["port"]):
            log("[%s] 入口 URL 还没抓到（可能还在启动）；状态变 ✓ 后再点" % key)
            return
        url = "http://127.0.0.1:%d" % cfg["port"]
        log("[%s] 未捕获到带 token 的入口 URL（该实例不是本管理器启动的），已复制裸地址"
            % key)
    try:
        root.clipboard_clear()
        root.clipboard_append(url)
        root.update_idletasks()          # 让剪贴板内容在管理器退出后仍然有效
        log("[%s] 已复制入口 URL 到剪贴板（含 token，请勿外发）" % key)
    except Exception as e:
        log("[%s] 复制入口 URL 失败：%s" % (key, e))


# ── 长任务期间冻结按钮 ──────────────────────────────────────────────────
# 安装 / 纳入 / 卸载这类会改磁盘的操作进行中，必须把「启动/停止/打开/杀全部」
# 一起冻住 —— 否则可能在 node_modules 搬到一半时被「启动」起来，装出半残状态。
# （2026-10-30 起「换版本」功能已删除，这里只保留通用的冻结机制。）
# 2026-10-31 分两组：「btns」= 底部常驻按钮（一次建好不再变），
# 「card」= 槽卡片里的按钮（改设置要重画卡片，旧的必须能从冻结清单里摘掉，
# 否则 _set_busy 会去 configure 已经 destroy 的控件）。
BUSY = {"btns": [], "card": []}

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
    vtxt = ver if ver else "未知"
    # 前缀 ≡ 是拖拽手柄的视觉提示（2026-10-31 加拖拽排序时一并加上）：
    # 光靠鼠标变成四向箭头太隐蔽，标题上给个握把符号用户才知道能拖。
    return "≡ %s · %s" % (cfg["label"], vtxt)

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
    box = ttk.LabelFrame(parent, text="可用版本（来自 npm registry）", padding=(8, 6))
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
    btn = ttk.Button(row, text="刷新列表")
    btn.pack(side="left")
    status = ttk.Label(row, text="准备获取…", foreground=_MUTED)
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
        status.configure(text="共 %d 个版本（%s）" % (len(items), source))
        state["busy"] = False
        btn.configure(state="normal")

    def fail(msg):
        status.configure(text="获取失败，可手动输入版本号")
        log("[版本列表] 获取失败：%s" % msg)
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
            status.configure(text="缺少 dsh_registry.py，只能手动输入版本号")
            return
        if state["busy"]:
            return
        if not force:
            items, source = REG.cached_versions()
            if items:
                apply_items(items, source + "，缓存")
                return
        state["busy"] = True
        btn.configure(state="disabled")
        status.configure(text="正在从 npm registry 获取版本列表…")

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
                root.after(0, back)
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
        log("已设置进程 AppID（任务栏将使用海豚图标）")
    except Exception as e:
        log("设置进程 AppID 失败（任务栏可能显示 Python 图标）：%s" % e)

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
            if buf.value == "DSH 版本管理器":
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
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_mutex_h = _k32.CreateMutexW(
    None, False, "DeepSeek.DSH.Manager.SingleInstance")
if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
    if _activate_existing_instance():
        log("已有实例在运行：已激活其窗口，本实例退出（单实例保护）")
    else:
        log("已有实例在运行但未找到其窗口，本实例退出（单实例保护）")
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
                    log("已自动修复任务栏固定项（整体重建+图标）：%s" % name)
            except Exception:
                continue
    except Exception as _e:
        try:
            log("固定项图标自愈失败（不影响运行）：%s" % _e)
        except Exception:
            pass

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
try:
    root = tk.Tk()
except Exception as _e:
    import traceback
    log("!!! 创建 tkinter 窗口失败：%s\n%s" % (_e, traceback.format_exc()))
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0,
            "创建窗口失败：%s\n\n详情见日志：%s" % (_e, LOG_FILE),
            "DSH 版本管理器", 0x10)
    except Exception:
        pass
    sys.exit(1)
root.title("DSH 版本管理器")

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
        log("已在窗口显示前设置顶层窗口(%s)图标，回读 GCLP_HICON=%s"
            % (_hwnd, "成功" if _rb == _hb else "失败(%s)" % _rb))
    else:
        log("未能取到 ico 帧或顶层窗口，窗口将使用 Tk 默认图标")
except Exception as _e:
    log("替换窗口类图标失败（不影响运行）：%s" % _e)

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
    log("皮肤初始化失败，回退默认主题：%s" % _e)
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
        log("图标将由 _force_taskbar_icon() 以原始帧设置（不走 Tk 的缩放路径）")
except Exception as e:
    log("窗口图标路径检查失败：%s" % e)

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
        log("图标自诊断：cwd=%s ICON_PATH=%s 存在=%s"
            % (os.getcwd(), ICON_PATH, bool(ICON_PATH and os.path.exists(ICON_PATH))))
        if not (ICON_PATH and os.path.exists(ICON_PATH)):
            log("!! 图标文件找不到（相对路径+错误工作目录会导致这里静默失败）")
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
        log("已用原始帧设置窗口图标（不缩放，避免发虚）"
            "（ICON_BIG=%s ICON_SMALL=%s）" % (bool(h_big), bool(h_small)))
    except Exception as e:
        log("强制设置任务栏图标失败：%s" % e)

_force_taskbar_icon()

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
#   · 标题显示 **dsh-tm 及版本号**（原来只写"DSH 版本管理器"，没版本）
#   · 「在 GitHub 收藏」放主界面（原来在设置里）
#   · 修深色模式下标题看不清
#     （原来 foreground 固定成创建时的 _INK=浅色值，切深色后不会变）
# 这里不固定 foreground，改由 _refresh_header_style() 在主题应用后统一刷。
_hdr_title = ttk.Label(_hdr, text="dsh-tm",
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

def _show_env_report():
    """在界面里显示环境体检结果（不要求用户另开命令行）。

    为什么要有：README 让人"先跑 check-env.py"对不熟悉命令行的用户是道坎。
    这里直接跑那个脚本并把输出收进弹窗，一步就能确认"我这台机器行不行"。
    """
    script = os.path.join(BASE_DIR, "scripts", "check-env.py")
    if not os.path.isfile(script):
        messagebox.showerror("无法检查", "找不到 %s" % script)
        return
    try:
        # pythonw 没有控制台，子进程输出重定向到临时文件再读
        tmp = os.path.join(BASE_DIR, "temp", "env-check.out")
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        import subprocess as _sp
        with open(tmp, "wb") as out:
            rc = _sp.run([sys.executable, script], stdout=out,
                         stderr=_sp.STDOUT,
                         creationflags=CREATE_NO_WINDOW).returncode
        raw = open(tmp, "rb").read().decode("utf-8", "replace")
    except Exception as e:
        messagebox.showerror("检查失败", "运行环境检查出错：\n%s" % e)
        return
    # 只保留有意义的行，压掉分隔线与空行
    lines = [ln.rstrip() for ln in raw.splitlines()
             if ln.strip() and not set(ln.strip()) <= set("=-")]
    body = "\n".join(lines[-40:])
    log("=== 环境检查（rc=%d）===\n%s" % (rc, body))
    (messagebox.showinfo if rc == 0 else messagebox.showwarning)(
        "运行环境检查", "%s\n\n%s" % (
            "环境满足要求。" if rc == 0 else "环境有不满足项，请看下面的说明。",
            body))

# 2026-10-31：标题栏的「检查运行环境」按钮撤掉了。它查的是**管理器自己**的前置条件
# （Python / tkinter / 标准库 / node / 权限 / 端口）—— 而这些在管理器已经打开的
# 情况下基本必然通过，剩下的 node 版本、槽文件完整性、端口占用，卡片状态行和
# 「槽体检」已经覆盖得更准（按槽、只读）。对日常用户它就是一段看不懂的噪音。
# 脚本仍然保留（scripts/check-env.py），换新机器 / 打包时由维护者直接跑；
# 零槽引导卡里也留了一个入口 —— 那是唯一「我这台机器到底行不行」的场合。

# ── 三个版本卡片（平等等宽，中间用竖向分隔线分开）─────────────
# 网格：每槽占 1 列，槽与槽之间插 1 列竖线；卡片列 weight=1 → 等宽。
def _open_dir(path, what="数据目录"):
    """用资源管理器打开目录。目录不存在 / 打开失败都要显式报错
    （pythonw 无控制台，只写日志用户看不见，所以同时弹框）。"""
    try:
        if not os.path.isdir(path):
            log("打开%s失败（目录不存在）：%s" % (what, path))
            messagebox.showerror("DSH 版本管理器",
                                 "%s不存在：\n%s\n\n可能尚未初始化（先启动一次 dsh）。" % (what, path))
            return
        os.startfile(path)          # Windows：交给资源管理器打开文件夹
        log("已打开%s：%s" % (what, path))
    except Exception as e:
        log("打开%s失败 %s：%s" % (what, path, e))
        messagebox.showerror("DSH 版本管理器", "打开%s失败：\n%s\n\n%s" % (what, path, e))

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
    按钮也必须摘掉 —— 否则 _set_busy 会去 configure 已经 destroy 的控件。
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
        log("[设置] 拖动浮起失败（退回普通排序）：%s" % e)
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
        log("[设置] 拖动排序保存失败：%s" % err)
        # 别在按钮的 <ButtonRelease> 处理里销毁控件，推迟到空闲时重建
        _abort_drop()
        root.after(0, _build_cards)
        messagebox.showerror("保存失败", err)
        return
    UI_CFG.clear()
    UI_CFG.update(prefs)
    if not floating:
        _regrid(new_disp)
    log("[设置] 拖动排序：%s" % "、".join(_display_keys()))


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
    log("[体检] 缺少 dsh_health.py，体检功能不可用：%s" % _e)

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
    lines = ["⚠%d = 槽体检发现 %d 个问题（只影响提示，不影响启动）："
             % (len(bad), len(bad))]
    for lv, tag, txt in bad[:5]:
        lines.append("· [%s] %s：%s" % (lv, tag, txt))
    if len(bad) > 5:
        lines.append("· …还有 %d 项" % (len(bad) - 5))
    lines.append("")
    lines.append("点底部「槽体检」看全部（只读，不会改任何文件）")
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
        log("[体检] 全局 %s：%s —— %s" % (lv, item, msg))
    bad = 0
    for key in SLOT_KEYS:
        for lv, item, msg in HEALTH.get(key) or []:
            if lv in (HEALTH_MOD.LEVEL_ERROR, HEALTH_MOD.LEVEL_WARN):
                bad += 1
                log("[体检] %s %s：%s —— %s" % (key, lv, item, msg))
    if not bad and not HEALTH_GLOBAL:
        log("[体检] %d 个槽全部正常（程序入口 / node / 数据目录 / 启动脚本都在）"
            % len(HEALTH))
    refresh(once=True)          # 只刷一次状态行，不另起定时链
    if dialog:
        _health_dialog()


def _health_dialog():
    """只读的结果窗口。**没有任何修复按钮** —— 修不修、怎么修由用户决定。"""
    win = tk.Toplevel(root)
    win.title("槽体检结果")
    win.transient(root)
    _apply_icon(win)
    win.geometry("720x460")

    ttk.Label(win, text="槽体检（只读）", font=("Microsoft YaHei UI", 11, "bold")
              ).pack(padx=16, pady=(14, 2), anchor="w")
    ttk.Label(win, foreground=_MUTED, justify="left", wraplength=680,
              text="这里只报告「现在的样子和登记的样子差在哪」，不会修改任何文件。"
                   "要不要处理、怎么处理由你决定；确实装坏了就走「卸载 → 重装」"
                   "（那条路有完整备份）。").pack(padx=16, pady=(0, 8), anchor="w")

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
        lines.append(("%s 全局 · %s：%s\n" % (lv, item, msg),
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
        lines = [("没有可体检的槽（管理器还没管理任何 dsh 版本）。\n", "info")]

    txt.configure(state="normal")
    for text, tag in lines:
        txt.insert("end", text, tag)
    txt.configure(state="disabled")

    def _copy():
        try:
            root.clipboard_clear()
            root.clipboard_append("".join(t for t, _ in lines))
            log("[体检] 结果已复制到剪贴板")
        except Exception as e:
            log("[体检] 复制失败：%s" % e)

    btns = ttk.Frame(win)
    btns.pack(fill="x", padx=16, pady=(0, 14))
    ttk.Button(btns, text="复制到剪贴板", command=_copy).pack(side="left")
    ttk.Button(btns, text="关闭窗口", command=win.destroy).pack(side="right")
    win.bind("<Escape>", lambda e: win.destroy())


def _run_health(deep=False, dialog=False):
    """后台跑体检（stat 几万个文件不能让界面卡住）。"""
    if HEALTH_MOD is None:
        if dialog:
            messagebox.showerror("体检不可用",
                                 "缺少 dsh_health.py，请确认管理器目录完整。")
        return
    if dialog:
        log("[体检] 开始深度体检（只读，不改任何文件）…")

    def work():
        try:
            res = HEALTH_MOD.check_all(VC, deep=deep)
        except Exception as e:
            root.after(0, lambda: log("[体检] 失败：%s" % e))
            return
        root.after(0, lambda: _health_apply(res, dialog))

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
    log("[变更] 缺少 dsh_drift.py，插件/配置变更不会记进日志：%s" % _e)

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
            log("[变更] %s 读插件清单失败（不影响使用）：%s" % (key, e))
    # 2026-11-02：零槽时这条纯属噪音（没东西可记录），不打。
    if n > 0:
        log("[变更] 已记录 %d 个槽的插件与配置基线（之后插件或 cordis 配置有改动会记在这里）"
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
            log("[变更] %s %s" % (key, line))
        if len(changes) > 20:
            log("[变更] %s 还有 %d 条变化未列出（改动太大，建议直接看槽目录）"
                % (key, len(changes) - 20))


def _build_cards():
    """按界面显示偏好画槽卡片。改设置后调它原地重画，不必重启管理器。

    为什么能原地重画（装/卸那种改动就得重启）：显示偏好只影响「画哪几张」，
    底层 VC / procs / SIG / status_vars 一个都不动 —— status_vars 本来就是给
    **全部**槽建的，refresh() 对没显示的槽只更新 StringVar，不碰控件。
    """
    if FRESH_START:
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
        _w = ttk.LabelFrame(cards_frame, text="没有显示任何版本槽", padding=(20, 18))
        _w.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)
        ttk.Label(_w, text="当前设置把 %d 个版本槽全部隐藏了。\n"
                           "它们的进程、端口、数据目录都还在，只是不画卡片 —— "
                           "想启动/停止可以直接打开设置把它们显示回来。"
                           % len(SLOT_KEYS),
                  style="Card.TLabel", justify="left"
                  ).pack(anchor="w", pady=(0, 12))
        ttk.Button(_w, text="打开设置…",
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

        _b_start = ttk.Button(_body, text="启动", style="Green.TButton",
                              command=lambda k=key: start_one(k))
        _b_start.grid(row=1, column=0, padx=3, pady=3, sticky="ew")
        _b_stop = ttk.Button(_body, text="停止", style="Red.TButton",
                             command=lambda k=key: kill_one(k))
        _b_stop.grid(row=1, column=1, padx=3, pady=3, sticky="ew")
        _b_open = ttk.Button(_body, text="打开 %s" % _browser_label(cfg["browser"]),
                             command=lambda k=key: open_browser(k))
        _b_open.grid(row=2, column=0, padx=3, pady=3, sticky="ew")
        _b_copy = ttk.Button(_body, text="复制入口 URL", command=lambda k=key: copy_url(k))
        _b_copy.grid(row=2, column=1, padx=3, pady=3, sticky="ew")
        # 程序目录 / 数据目录 一键直达（2026-10-01 新增）
        _root_dir = _version_root(cfg) or ""
        _b_pdir = ttk.Button(_body, text="程序目录",
                             command=lambda k=key, p=_root_dir: _open_dir(p, "程序目录"))
        _b_pdir.grid(row=3, column=0, padx=3, pady=(3, 0), sticky="ew")
        _home_dir = cfg["dsh_home"] or os.path.join(os.path.expanduser("~"), ".dsh")
        _b_ddir = ttk.Button(_body, text="数据目录",
                             command=lambda k=key, p=_home_dir: _open_dir(p, "数据目录"))
        _b_ddir.grid(row=3, column=1, padx=3, pady=(3, 0), sticky="ew")
        BUSY["card"].extend([_b_start, _b_stop, _b_open, _b_copy, _b_pdir, _b_ddir])
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
    """版本包根目录。优先按既有槽的 bin_js 反推（认得离线包布局），
    兜底用 install_base/dsh-versions（与既有三槽一致）。"""
    for _k in SLOT_KEYS:
        root = _version_root(VC[_k])
        if root:
            return os.path.dirname(root)
    return os.path.join(INSTALL_BASE, "dsh-versions")

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


def _managed_roots(include_detached=True):
    """「已知的」安装根（绝对路径），用于把"已经知道的东西"从检测结果里排除。

    两类都算已知：
      1. 已在 config.json 登记的槽（正在管）
      2. 已移出管理的槽（config 顶层 `detached`）—— 用户是**主动**让它脱离的，
         不该每次启动又被问一遍「要不要纳入」。回头的路在「卸载 / 移出…」窗口
         下半部分的「已移出管理」列表里（2026-10-31）。

    `include_detached=False`：只算**正在管**的槽。手动点「纳入已有的 dsh」时必须用
    这一档 —— 否则已移出的槽被当成"已知"过滤掉，用户点了纳入却一个都扫不到
    （2026-10-31 实测踩到）。移出记录留着是为了「重新纳入」，不是为了让它从
    纳入扫描里消失。"""
    out = []
    for _k in SLOT_KEYS:
        r = _version_root(VC[_k])
        if r:
            out.append(r)
    if include_detached:
        for _r in _detached_records():
            if _r.get("root"):
                out.append(_r["root"])
    return out

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
        shells = []
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            if not os.path.isdir(p):
                continue
            if os.path.normcase(os.path.abspath(p)) in managed:
                continue
            try:
                if OPS._dir_is_empty_shell(p):
                    shells.append(name)
            except Exception:
                continue
        if shells:
            log("[清理] 发现空壳目录（没有任何文件、也不在 config 里）：%s"
                % "、".join(shells))
            log("[清理] 它们只是占着槽名，不会影响使用；下次装同名槽会自动复用。"
                "要清掉可手工删除：%s"
                % "、".join(os.path.join(root, n) for n in shells))
    except Exception as e:
        log("[清理] 空壳目录检查失败（不影响使用）：%s" % e)

def _next_free_slot_key():
    """下一个可用槽键（v1..vMAX_SLOTS 里第一个没用过的）。"""
    used = set(SLOT_KEYS)
    for i in range(1, MAX_SLOTS + 1):
        if ("v%d" % i) not in used:
            return "v%d" % i
    return ""

class _GuiProgress(OPS.Progress if OPS else object):
    """把后台线程的进度转发到主线程的日志区 + 进度条。
    tkinter 不是线程安全的，所以一律用 root.after 切回主线程。"""
    def __init__(self, title, done_cb):
        self.title = title
        self.done_cb = done_cb
        self.win = tk.Toplevel(root)
        self.win.title(title)
        self.win.transient(root)
        self.win.resizable(False, False)
        _apply_icon(self.win)     # 进度窗也要有图标
        ttk.Label(self.win, text=title, font=("Microsoft YaHei UI", 10, "bold")
                  ).pack(padx=20, pady=(18, 8))
        self.lbl = ttk.Label(self.win, text="准备中…", wraplength=420,
                             justify="left")
        self.lbl.pack(padx=20, pady=(0, 12))
        self.bar = ttk.Progressbar(self.win, mode="indeterminate", length=400)
        self.bar.pack(padx=20, pady=(0, 18))
        self.bar.start(12)
        self.win.protocol("WM_DELETE_WINDOW", lambda: None)   # 不给中途关掉
        self.win.grab_set()

    def on_step(self, text):
        # 后台线程 -> 主线程
        root.after(0, lambda: self.lbl.configure(text=str(text)[:180]))
        log("[%s] %s" % (self.title, text))

    def on_done(self, ok, payload=None, error=""):
        root.after(0, lambda: self._finish(ok, payload, error))

    def _finish(self, ok, payload, error):
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
    for field in ("keys", "installed", "slot_keys"):
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
        return False, "找不到管理器入口文件：%s" % (entry or "(未知)")

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
        return False, "找不到 Python 解释器：%s" % (pyw or "(未知)")

    try:
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：子进程与当前进程脱钩，
        # 我们随后 destroy 自己不会把它一起带走。
        flags = 0
        if os.name == "nt":
            flags = 0x00000008 | 0x00000200
        subprocess.Popen([pyw, entry], cwd=BASE_DIR,
                         creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception as e:
        return False, "启动新实例失败：%s" % e
    return True, ""


def _reload_config_after_change(new_keys=None):
    """config.json 被改了之后，让界面整体重画。

    槽位数量可能变了（1 槽 -> 2 槽），只刷新卡片是不够的，直接重开窗口最稳。

    `new_keys`：本次新增的槽键（安装 / 纳入）。2026-11-01 起，
    这里会先把它们补进 `ui.visible` —— 否则新装的槽在"有白名单"的配置下
    根本不画出来，用户会以为装失败了（见 _ensure_slots_visible）。

    返回值：(ok, 说明)。**调用方据此决定提示文案** —— 重启失败时不能再对用户
    说"即将重启"，那正是 2026-11-01 用户实测到的"说了要重启却没重启"。
    """
    try:
        err = _ensure_slots_visible(new_keys or [])
        if err:
            log("[配置] 新槽并入显示白名单失败（不影响启动，但可能看不到新卡片）：%s" % err)
        elif new_keys:
            log("[配置] 已把新槽 %s 并入界面显示列表" % "、".join(new_keys))
    except Exception as e:
        log("[配置] 同步 ui.visible 时出错（忽略）：%s" % e)

    ok, why = _relaunch_self()
    if ok:
        log("[配置] 已变更，正在重启管理器以加载新槽。")
        try:
            root.destroy()
        except Exception:
            pass
        return True, ""

    # 起不来：保持窗口存活，把原因如实写进日志，由调用方提示用户手动重开。
    log("[配置] 已变更，但自动重启失败：%s（请手动关闭并重新打开管理器）" % why)
    return False, why


def _restart_or_tell(what="版本列表"):
    """重开管理器；失败时**如实**告诉用户，而不是谎称"即将重启"。

    2026-11-01 用户实测：「卸载完弹『管理器即将重启』，但实际没重启」。
    原因是原代码 destroy() 后 os.startfile 静默失败，异常被吞掉。
    现在改成：先启动新实例成功才关自己；失败就留下窗口并弹出说明。
    """
    ok, why = _relaunch_self()
    if ok:
        try:
            root.destroy()
        except Exception:
            pass
        return
    try:
        messagebox.showwarning(
            "已更新，但未能自动重启",
            "配置已改好，但管理器没能自动重启（%s）。\n\n"
            "请手动关闭本窗口并重新打开管理器，新的%s就会生效。"
            % (why, what))
    except Exception:
        pass


def _adopt_ignored():
    """config.json 里记着「用户明确跳过、以后别再提示」的安装路径（normcase 后）。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        return set(os.path.normcase(os.path.abspath(p))
                   for p in (d.get("adopt_ignored") or []) if p)
    except Exception:
        return set()


def _set_adopt_ignored(paths, add=True):
    """增/删「以后不再提示」名单。写失败只记日志 —— 提示逻辑不该因此崩。"""
    if not paths:
        return
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        cur = [p for p in (d.get("adopt_ignored") or []) if p]
        want = set(os.path.normcase(os.path.abspath(p)) for p in paths)
        if add:
            have = set(os.path.normcase(os.path.abspath(p)) for p in cur)
            for p in paths:
                if os.path.normcase(os.path.abspath(p)) not in have:
                    cur.append(os.path.abspath(p))
                    have.add(os.path.normcase(os.path.abspath(p)))
        else:
            cur = [p for p in cur
                   if os.path.normcase(os.path.abspath(p)) not in want]
        d["adopt_ignored"] = cur
        OPS.write_config_atomic(CONFIG_PATH, d)
        log("[检测] 已更新「不再提示」名单（%d 项）" % len(cur))
    except Exception as e:
        log("[检测] 保存「不再提示」名单失败（不影响使用）：%s" % e)


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
    info = {}
    try:
        info = dsh_adopt.inspect_adopt_source(nm) if dsh_adopt else {}
    except Exception as e:
        info = {"ok": False, "problems": ["体检失败：%s" % e], "warnings": []}
    kind = "npm 全局" if (loc or {}).get("kind") == "npm-global" else "版本目录"
    ver = version or info.get("version") or "?"
    dsh_dir = info.get("dsh_dir") or ""
    have = lambda rel: bool(dsh_dir) and os.path.isfile(os.path.join(dsh_dir, rel))
    comp = []
    comp.append("入口 " + ("✓" if have(os.path.join("lib", "bin.js")) else "✗"))
    comp.append("package.json " + ("✓" if have("package.json") else "✗"))
    nm_count = 0
    try:
        nm_count = len([x for x in os.listdir(nm)]) if os.path.isdir(nm) else 0
    except Exception:
        nm_count = 0
    comp.append("依赖 %d 项%s" % (nm_count, "" if not info.get("deps_missing")
                                  else "（缺 %d）" % len(info["deps_missing"])))
    src_home = info.get("source_home") or ""
    comp.append("旧数据 " + ("✓ " + src_home if src_home else "（无）"))
    # 2026-11-02：给普通用户一句**能看懂**的摘要；下面的逐项体检细节收进"详情"。
    _deps_missing = len(info.get("deps_missing") or [])
    if info.get("ok") and not _deps_missing:
        _summary = "可以纳入"
    elif info.get("ok"):
        _summary = "可以纳入（有 %d 个可选依赖缺失，一般不影响使用）" % _deps_missing
    else:
        _summary = "这个安装不完整，暂时不能纳入"
    return {"ok": bool(info.get("ok")), "version": ver, "kind": kind, "nm": nm,
            "summary": _summary,
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


def _adopt_picker_dialog(found):
    """让用户逐个选择要纳入的 dsh 安装。

    返回 (picked, skipped)，两者都是 [row]；用户直接关窗口返回 (None, None)。
    picked 里的是"要纳入"，skipped 里的是"以后不再提示"。
    """
    rows = []
    for b in found:
        for loc in (b.get("locations") or []):
            rows.append(_adopt_row_info(b.get("version"), loc))

    win = tk.Toplevel(root)
    win.title("发现已安装的 dsh")
    win.transient(root)
    # 固定尺寸：内容宽度是按 620px 排的，交给 WM 自适应时窗口会被长路径撑开，
    # 结果右边被屏幕/窗口边缘切掉（实拍验证过）。这里显式定死，配合下面的
    # wraplength 与 canvas.itemconfigure 保证任何路径长度都不会溢出。
    _h = min(680, 210 + 126 * max(1, len(rows)))
    win.geometry("700x%d" % _h)
    win.resizable(False, True)
    _apply_icon(win)
    ttk.Label(win, text="这些 dsh 不在管理器里，勾选要纳入的版本",
              font=("Microsoft YaHei UI", 10, "bold")).pack(padx=18, pady=(14, 2))
    ttk.Label(win, foreground=_MUTED, wraplength=600, justify="left",
              text="纳入 = 复制一份程序与依赖到本管理器的版本根，并分配独立端口与数据目录；"
                   "原安装一个字节都不动。没勾选的会被记下，以后启动不再提示"
                   "（想改主意点「扫描已装 dsh 并纳入」随时重扫）。"
              ).pack(padx=18, pady=(0, 8), anchor="w")

    outer = ttk.Frame(win)
    outer.pack(padx=18, pady=(0, 6), fill="both", expand=True)
    canvas = tk.Canvas(outer, highlightthickness=0, width=640,
                       height=min(420, 126 * len(rows) + 10))
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
        box = ttk.LabelFrame(inner, padding=(8, 5))
        box.pack(fill="x", pady=3, padx=2)
        v = tk.BooleanVar(value=r["ok"])       # 完好的默认勾上，坏的不勾
        # 2026-11-01 用户反馈：原生 Checkbutton 的勾选态是「☒ / ☐」，看着像叉号/报错。
        # 改成「✅ / 空」：用一个可点击的 Label 显示符号，点它就切换 BooleanVar。
        _title = "%s   [%s]" % (r["version"], r["kind"])
        _row = ttk.Frame(box)
        _row.pack(anchor="w")
        _mark = ttk.Label(_row, text="✅" if v.get() else "  ",
                          font=("Segoe UI", 10))
        _mark.pack(side="left", padx=(0, 6))
        _txt = ttk.Label(_row, text=_title)
        _txt.pack(side="left")

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
        # 2026-11-02 用户反馈：中间的"位置 / 组件 / 依赖 282 项 / 旧数据"都是
        # 诊断细节，普通用户不关心，堆在那里反而看不懂。
        # 现在：只留一句摘要 + 一个「详情」可展开（默认收起）。
        ttk.Label(box, text=r.get("summary", ""), foreground=_MUTED,
                  wraplength=600, justify="left").pack(anchor="w", padx=(22, 0))

        _detail = ttk.Frame(box)
        _detail.pack(anchor="w", fill="x", padx=(22, 0))
        _shown = {"on": False}

        def _mk_detail(d=_detail, rr=r):
            rows = [("位置", rr["nm"]), ("组件", rr["component"])]
            for t in rr["problems"]:
                rows.append(("问题", t))
            for t in rr["warnings"]:
                rows.append(("提醒", t))
            for k, v in rows:
                line = ttk.Frame(d)
                line.pack(anchor="w", fill="x", pady=1)
                ttk.Label(line, text=k + "：", foreground=_MUTED, width=5
                          ).pack(side="left", anchor="n")
                ttk.Label(line, text=v, foreground=_INK, wraplength=540,
                          justify="left").pack(side="left", anchor="w")

        def _toggle_detail(_e=None, d=_detail, st=_shown, btn=None):
            if st["on"]:
                for w in d.winfo_children():
                    w.destroy()
                st["on"] = False
                if btn is not None:
                    btn.configure(text="详情 ▾")
            else:
                _mk_detail()
                st["on"] = True
                if btn is not None:
                    btn.configure(text="详情 ▴")

        _btn = ttk.Label(box, text="详情 ▾", foreground=_LINK, cursor="hand2")
        _btn.pack(anchor="w", padx=(22, 0), pady=(2, 0))
        _btn.bind("<Button-1>", lambda e, b=_btn: _toggle_detail(btn=b))
        vars_.append((v, r))

    res = {"picked": None, "skipped": None}

    def on_ok():
        picked = [r for v, r in vars_ if v.get()]
        skipped = [r for v, r in vars_ if not v.get()]
        if not picked and not skipped:
            messagebox.showwarning("没有可纳入的版本", "列表里没有体检通过的安装。")
            return
        res["picked"], res["skipped"] = picked, skipped
        win.destroy()

    def on_skip_all():
        res["picked"], res["skipped"] = [], [r for _v, r in vars_]
        win.destroy()

    btns = ttk.Frame(win)
    btns.pack(padx=18, pady=(4, 14), fill="x")
    ttk.Button(btns, text="纳入勾选的", command=on_ok).pack(side="left", padx=(0, 6))
    ttk.Button(btns, text="全部跳过，以后不再提示",
               command=on_skip_all).pack(side="left")
    # 刻意**不**放一个"关闭就走、什么都不记"的按钮：需求是「没选择纳入 → 以后别再提示」。
    # 留着它会和右上角 X 语义打架（X 记名单、"关闭"不记），用户点哪个决定下次是否被
    # 再问一次，属于隐性行为差异。统一成：关窗口 = 全部跳过，想改主意用手动重扫。

    def _on_close():
        # 直接关窗口 = 全部跳过：需求就是「没选择纳入 → 以后别再提示」。
        # 但记下名单，手动重扫还能找回来。
        on_skip_all()

    win.protocol("WM_DELETE_WINDOW", _on_close)
    win.bind("<Escape>", lambda e: _on_close())
    win.grab_set()
    win.wait_window()
    return (res["picked"], res["skipped"])


def scan_and_offer_adopt(manual=False):
    """扫本机已装 dsh，让用户挑哪些纳入管理。

    manual=False（启动 1.5 秒后自动跑一次）：跳过用户之前明确"跳过"过的安装，
    不再骚扰 —— 以及**已移出的槽**：那是用户主动放走的，不该每次开机又被问。
    manual=True（点按钮）：全量重扫，连之前跳过的也重新列出来 —— 用户主动点了，
    说明他改主意了。

    两档都**不把已移出的槽**列进来：放走的那批有专门的回头路（「卸载 / 移出…」
    窗口下半部分的「已移出管理」列表），一个动作只对应一个入口，免得两条路都
    能"加回来"却语义不同（一个是复制、一个是原样接回）。

    刻意**不自动纳入**：纳入会复制几百 MB 并改 config.json，是有副作用的动作，
    必须用户明确同意。
    """
    if dsh_adopt is None:
        messagebox.showerror("无法扫描", "缺少 dsh_adopt.py 模块。")
        return
    try:
        # 2026-11-01 修：**自动扫描**必须把「已移出的槽」也算作已知，否则
        # 用户刚移出一个版本、下次启动就被当成"新发现了未纳入的 dsh"弹窗追问，
        # 而真正想接回来时又只能用「已移出管理」列表 —— 自相矛盾（用户实测踩到）。
        # 手动点「扫描已装 dsh 并纳入」时才用 include_detached=False（全量重扫）。
        # 2026-11-02 用户反馈：「点击扫描并纳入怎么没有找到 C 盘里的 dsh 版本」。
        # 实际原因是那个安装已经被「移出管理」，按原设计会被当成"已知"排除，
        # 于是手动扫描也看不到 —— 用户无法理解"文件明明在，为什么扫不到"。
        # 现在：**手动扫描**把已移出的也列进来（它们本来就是可纳入的），
        # 这样用户点一下就能看到全部未在管的安装。
        found = dsh_adopt.detect_existing_installs(
            [_probe_npm_global()] if _probe_npm_global() else [],
            _versions_root(),
            _managed_roots(include_detached=not manual))

        if manual:
            # 把「已移出管理」的记录补进候选（它们仍在磁盘上、可原样接回）
            try:
                _det_roots = {os.path.normcase(os.path.abspath(r["root"]))
                              for r in _detached_records() if r.get("root")}
                for r in _detached_records():
                    _rt = r.get("root") or ""
                    if not _rt or not os.path.isdir(_rt):
                        continue
                    _dsh = os.path.join(_rt, "node_modules", "@deepseek-ai", "dsh")
                    if not os.path.isdir(_dsh):
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
                        _ver = r.get("version") or ""
                    found.append({
                        "version": _ver,
                        "locations": [{"kind": "versions-dir",
                                       "path": _dsh,
                                       "node_modules": _nm}],
                    })
            except Exception as _e:
                log("[检测] 合并已移出记录时出错（忽略）：%s" % _e)
    except Exception as e:
        log("[检测] 扫描已装版本失败（不影响使用）：%s" % e)
        return

    if not manual:
        ignored = _adopt_ignored()
        kept = []
        for b in found:
            locs = [l for l in (b.get("locations") or [])
                    if os.path.normcase(os.path.abspath(_adopt_loc_nm(l))) not in ignored]
            if locs:
                kept.append({"version": b.get("version"), "locations": locs})
        found = kept

    if not found:
        log("[检测] 本机没有发现（新的）管理范围外的 dsh 安装。")
        if manual:
            _det = _detached_records()
            _hint = ""
            if _det:
                _hint = ("\n\n⚠ 你还有 %d 个「已移出管理」的版本（%s）——\n"
                         "它们不在这条扫描路径上：文件还在原处，只是不再由管理器跟踪。\n"
                         "那批要用另一条路回来：「卸载 / 移出…」窗口下半部分的\n"
                         "「已移出管理」列表，勾选后点「重新纳入勾选的」。"
                         % (len(_det),
                            "、".join((r.get("label") or r.get("root") or "?")
                                      for r in _det)))
            # 2026-11-02 用户反馈：「弹出了一些窗口，还有移出管理，不知道你怎么设计的」。
            # 原来的流程是：弹「没有可纳入的」→ 再弹「要打开已移出管理吗」→
            # 确认后打开「卸载/移出」窗 —— 三层弹窗，且打开的那个窗跟"纳入"没关系。
            # 改成：**只弹一个**说明框，把话一次说清，不再连环追问。
            if _det:
                _more = ("\n\n不过，你有 %d 个版本处于「已移出管理」状态"
                         "（%s）。\n它们不在纳入扫描范围内 —— 要接回来，"
                         "请到「设置 → 维护」里的卸载/移出窗口。"
                         % (len(_det),
                            "、".join((r.get("label") or r.get("root") or "?")
                                      for r in _det)))
            else:
                _more = ""
            messagebox.showinfo(
                "没有可纳入的 dsh",
                "没扫到管理范围外的 dsh。\n\n"
                "已经在你槽位里的版本、以及「已移出管理」的版本都不会出现在这里。"
                + _more)
        return

    log("[检测] 发现 %d 个管理范围外的版本：%s"
        % (len(found), ", ".join(b.get("version") or "?" for b in found)))
    picked, skipped = _adopt_picker_dialog(found)
    if picked is None:
        return

    # 没勾的 → 以后不再提示；勾了的 → 从忽略名单里删掉（万一之前跳过过）
    _set_adopt_ignored([r["nm"] for r in skipped], add=True)
    _set_adopt_ignored([r["nm"] for r in picked], add=False)
    if skipped:
        log("[检测] 用户跳过 %d 个安装（以后不再自动提示）" % len(skipped))
    if not picked:
        return
    # 第一次纳入（一个槽都没有）→ 先确认版本放哪（纳入会把程序复制进这个根）。
    # 必须在循环之前问一次：放进循环就会为每个安装各问一遍。
    if not SLOT_KEYS and not _ask_install_root():
        return
    for r in picked:
        _adopt_one(r["version"], r["loc"])


def _adopt_one(version, loc):
    """纳入单个安装。容量/耗时都大，走后台线程 + 进度窗。"""
    if OPS is None or dsh_adopt is None:
        messagebox.showerror("无法纳入", "缺少 dsh_install_ops.py / dsh_adopt.py 模块。")
        return
    if len(SLOT_KEYS) >= MAX_SLOTS:
        messagebox.showwarning("槽位已满",
                               "管理器最多管理 %d 个版本（当前 %d 个）。\n"
                               "请先在 config.json 里删掉不再使用的槽，或手动整理该安装。"
                               % (MAX_SLOTS, len(SLOT_KEYS)))
        return

    slot_key = _next_free_slot_key()
    # 2026-11-01 修 bug（用户实测「纳入失败」）：
    # 原逻辑只在**已有槽**里找 node；零槽时退化成裸 "node"，被当成相对路径
    # `D:\DSH-TM\node`，于是复制阶段报「找不到可用的 node 运行时」。
    # 正确做法：走 OPS._pick_node_exe() —— 先找既有槽，再找包内自带的
    # runtime/node/node.exe（发布包必带），两者都没有才退回系统 PATH。
    node_src = ""
    try:
        _existing = [VC[_k] for _k in SLOT_KEYS if VC.get(_k)]
        if hasattr(OPS, "_pick_node_exe"):
            node_src = OPS._pick_node_exe(_existing, INSTALL_BASE, HOME) or ""
    except Exception as e:
        log("[纳入] 挑选 node 运行时出错（继续尝试其它来源）：%s" % e)
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
    if not node_src:
        node_src = "node"       # 交给系统 PATH；复制阶段会如实报缺

    # 纳入要复制的是整个 node_modules，不是 @deepseek-ai/dsh 包目录 ——
    # detect_existing_installs 的 path 是后者，必须经 _adopt_loc_nm 换过来。
    # （2026-10-30 修：之前直接传 path，体检必然报「这里没有 @deepseek-ai/dsh」。）
    nm_src = _adopt_loc_nm(loc)
    log("[纳入] 开始：%s <- %s" % (version, nm_src))

    def work():
        ok, payload, msg = OPS.adopt_existing(
            dsh_adopt, nm_src, _versions_root(),
            CONFIG_PATH, slot_key, _taken_ports(),
            node_exe_source=node_src, max_slots=MAX_SLOTS,
            install_base=INSTALL_BASE, temp_root=_temp_root())
        # 说明塞进 payload：下面只让进度窗的 done_cb 回调一次。同一个 done
        # 同时传给 run_in_thread 会被调两次（工作线程一次 + 主线程一次）
        # → 两个「纳入成功」弹框 + 两次重启。2026-10-29 统一改成这种写法。
        if isinstance(payload, dict):
            payload["message"] = msg
        return (ok, payload, msg)

    def done(ok, payload, msg):
        msg = (payload or {}).get("message") or msg or ""
        if ok:
            log("[纳入] 成功：%s" % msg)
            messagebox.showinfo("纳入成功",
                                "%s\n\n槽：%s（%s）\n端口：%d\n数据目录：%s\n\n"
                                "原安装未做任何改动。管理器即将重启以显示新槽。"
                                % (msg, payload["slot_key"], payload["label"],
                                   payload["port"], payload["dsh_home"]))
            _ensure_slots_visible([payload.get("slot_key")])
            _restart_or_tell("槽列表")
        else:
            log("[纳入] 失败：%s" % msg)
            messagebox.showerror("纳入失败", msg)

    prog = _GuiProgress("纳入 dsh %s" % version, done)
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
    """
    used = set(SLOT_KEYS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            used = set((json.load(f).get("versions") or {}).keys())
    except Exception:
        pass
    for i in range(1, MAX_SLOTS + 1):
        if ("v%d" % i) not in used:
            return "v%d" % i
    return ""


def install_official_version():
    """安装官方版：**从列表里多选版本**（或手填，逗号/空格分隔）-> 依次装进空槽。

    2026-10-29 起对话框会列出 npm registry 上的全部已发布版本（标出最新/预览/已弃用），
    点一行即填 —— 原来只能手打版本号，记错一个字符就要白等几分钟的 npm 安装，
    还容易被误判成网络问题。手动输入框仍保留（可填 latest / next 这类 dist-tag）。

    2026-10-30 起支持**一次装多个**：列表里 Ctrl 点选 / Shift 连选，或直接填
    `0.2.0-rc.2, 0.2.1-alpha.2`。安装按填写顺序逐个进行（每个装进当时的下一个
    空槽），中途某个失败不影响后面的，全部结束后只重启一次管理器。
    """
    if OPS is None or dsh_adopt is None:
        messagebox.showerror("无法安装", "缺少 dsh_install_ops.py / dsh_adopt.py 模块。")
        return
    free = MAX_SLOTS - len(SLOT_KEYS)
    if free <= 0:
        messagebox.showwarning("槽位已满",
                               "管理器最多管理 %d 个版本，当前已有 %d 个：%s\n\n"
                               "要装新版本，请先用「卸载 / 移出…」处理掉不用的槽。"
                               % (MAX_SLOTS, len(SLOT_KEYS), ", ".join(SLOT_KEYS)))
        return

    # 第一次装（一个槽都没有）→ 先确认版本放哪。取消就什么都不做。
    if not SLOT_KEYS and not _ask_install_root():
        return

    win = tk.Toplevel(root)
    win.title("安装官方版 dsh")
    win.transient(root)
    win.resizable(False, False)
    _apply_icon(win)      # 安装对话框也要有图标
    ttk.Label(win, text="安装官方版 dsh（一次最多 %d 个）" % free,
              font=("Microsoft YaHei UI", 10, "bold")).pack(padx=20, pady=(16, 4))
    ttk.Label(win, text="从下面选版本：Ctrl 点选多个 / Shift 连选；"
                        "也可以直接在下面手填。", foreground=_MUTED
              ).pack(padx=20, pady=(0, 6))

    ver_var = tk.StringVar(value="")
    _picker = _build_version_picker(win, ver_var, height=9, width=44, multi=True)
    _picker["frame"].pack(padx=20, pady=(0, 10), fill="both", expand=True)

    ttk.Label(win, text="或手动输入版本号（多个用逗号 / 空格分隔；也可填 latest / next）：",
              foreground=_MUTED).pack(anchor="w", padx=20)
    ent = ttk.Entry(win, textvariable=ver_var, width=44)
    ent.pack(padx=20, pady=(2, 6), anchor="w")
    ent.focus_set()

    ttk.Label(win, text="将从官方 npm registry 下载安装；需要 Node.js ≥ 22.19。\n"
                        "每个版本装进一个独立的空槽（自带数据目录与端口），不影响已有版本。",
              foreground=_MUTED, wraplength=440, justify="left"
              ).pack(padx=20, pady=(0, 12))

    def on_ok(_evt=None):
        raw = (ver_var.get() or "").strip()
        if not raw:
            messagebox.showwarning("还没选版本",
                                   "请在上面的列表里点一个版本，或在输入框里填版本号。")
            return
        # dist-tag（latest/next）是 npm 的合法入参，不能当成"格式错误"打回去
        vers = []
        for tok in _split_versions(raw):
            if tok.lower() in ("latest", "next", "beta", "alpha"):
                vers.append(tok.lower())
                continue
            ver = dsh_adopt.normalize_version(tok)
            if not ver:
                messagebox.showerror("版本号格式不对",
                                     "「%s」不是有效的官方版本号。\n\n%s"
                                     % (tok, dsh_adopt.version_hint()))
                return
            if ver != tok:
                log("[安装] 版本号按官方格式修正：%s -> %s" % (tok, ver))
            if ver.lower() not in [v.lower() for v in vers]:
                vers.append(ver)
        if not vers:
            messagebox.showwarning("还没选版本", "没有解析出任何版本号。")
            return
        if len(vers) > free:
            messagebox.showwarning(
                "槽位装不下",
                "本次填了 %d 个版本，但只剩 %d 个空槽（上限 %d）。\n\n"
                "请减少几个，或先卸载不用的槽。" % (len(vers), free, MAX_SLOTS))
            return
        try:
            win.destroy()
        except Exception:
            pass
        _install_queue(vers)

    btns = ttk.Frame(win)
    btns.pack(padx=20, pady=(0, 16))
    ttk.Button(btns, text="安装", style="Green.TButton",
               command=on_ok).pack(side="left", padx=4)
    ttk.Button(btns, text="取消", command=win.destroy).pack(side="left", padx=4)
    ent.bind("<Return>", on_ok)
    win.bind("<Escape>", lambda e: win.destroy())
    win.grab_set()


def _install_queue(versions):
    """按顺序装多个版本，全部结束后只重启一次管理器。

    为什么写成「回调里递归下一步」而不是 for 循环：每次安装都是后台线程 + 进度窗，
    主线程必须继续跑事件循环（否则窗口冻结、进度条不动）。for 循环 + 阻塞等待
    会让界面假死。
    中途失败**不中断**：失败的版本单独记下来，最后一次性告诉用户重试哪个 ——
    装 5 个版本时因为第 2 个网络抖动就整批停下，用户体验更差。
    """
    total = len(versions)
    results = []
    log("[安装] 队列：共 %d 个版本 —— %s" % (total, "、".join(versions)))

    def finish():
        _set_busy(True)
        okr = [r for r in results if r[1]]
        badr = [r for r in results if not r[1]]
        lines = []
        for ver, ok, key, msg in results:
            if ok:
                lines.append("  ✓ %s → 槽 %s" % (ver, key))
            else:
                first = (msg or "原因未知").strip().splitlines()[0][:120]
                lines.append("  ✗ %s —— %s" % (ver, first))
        body = "\n".join(lines)
        if badr:
            messagebox.showwarning(
                "安装结束（成功 %d / 失败 %d）" % (len(okr), len(badr)),
                "%s\n\n失败的版本可以直接再点「安装官方版 dsh」重试。%s"
                % (body, "\n\n管理器即将重启以显示新装的槽。" if okr else ""))
        else:
            messagebox.showinfo("安装成功",
                                "%s\n\n管理器即将重启以显示新槽。" % body)
        if okr:
            _ensure_slots_visible(_installed_keys_from(payload))
            _restart_or_tell("槽列表")

    def step(i):
        if i >= total:
            finish()
            return
        ver = versions[i]
        key = _next_free_slot_key_live()
        if not key:
            results.append((ver, False, "", "槽位已满（上限 %d）" % MAX_SLOTS))
            step(i + 1)
            return

        def _next(ok, msg, _v=ver, _k=key):
            results.append((_v, ok, _k, msg))
            step(i + 1)

        _do_install(ver, key, on_done=_next, index=i + 1, total=total)

    _set_busy(False)
    step(0)


def _do_install(version, slot_key, on_done=None, index=1, total=1):
    """后台装官方版。

    2026-10-30：安装不再调用外部脚本（原先那个已随旧功能删除，曾导致这里报
    [WinError 2] 找不到文件）。安装内核在 OPS._npm_install_slot。

    `on_done(ok, msg)` 非空时由调用方接管收尾（一次装多个时用：接着装下一个），
    为空则沿用单装行为 —— 弹框 + 重启管理器。
    """
    log("[安装] 开始：官方版 %s -> 槽 %s" % (version, slot_key))

    def done(ok, payload, msg):
        msg = (payload or {}).get("message") or msg or ""
        if ok:
            log("[安装] 成功：%s" % msg)
        else:
            log("[安装] 失败：%s" % msg.replace("\n", " | "))
        if on_done:
            on_done(ok, msg)
            return
        if ok:
            messagebox.showinfo("安装成功",
                                "%s\n\n管理器即将重启以显示新槽。" % msg)
            _ensure_slots_visible(_installed_keys_from(payload))
            _restart_or_tell("槽列表")
        else:
            messagebox.showerror("安装失败", msg)

    title = ("安装官方版 %s（%d/%d）" % (version, index, total)
             if total > 1 else "安装官方版 %s" % version)

    # 进度窗必须先建：work() 要把它传给 run_official_install，否则下载/复制那几分钟
    # 窗口会一直停在「准备中…」，看起来像卡死（2026-10-29 实测踩到）。
    prog = _GuiProgress(title, done)

    def work():
        ok, payload, msg = OPS.run_official_install(
            dsh_adopt, version, CONFIG_PATH, INSTALL_BASE, slot_key,
            _taken_ports(), progress=prog, max_slots=MAX_SLOTS,
            versions_root=_versions_root(), temp_root=_temp_root())
        # 把说明塞进 payload：下面只用进度窗的 done_cb 回调一次，
        # 不再把同一个 done 同时传给 run_in_thread（那会让它被调用两次：
        # 一次在工作线程、一次在主线程 → 两个"安装成功"弹框 + 两次重启）。
        if isinstance(payload, dict):
            payload["message"] = msg
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

def _set_busy(enabled):
    """安装/纳入/卸载进行中冻结操作按钮。

    为什么必须冻：这些操作会在磁盘上搬 node_modules、改 config.json，
    期间点「启动」会拉起一个半装状态的实例。冻住的按钮清单见 BUSY["btns"]。"""
    for b in list(BUSY["btns"]) + list(BUSY["card"]):
        try:
            b.configure(state="normal" if enabled else "disabled")
        except Exception:
            pass


def _slot_brief(key):
    """一行摘要：v1（dshV1，端口 4080，实装 0.1.5-rc.2）· 运行中"""
    cfg = VC[key]
    ver = INSTALLED.get(key) or "未知"
    running = port_open(cfg["port"]) or proc_alive(procs.get(key))
    return "%s（%s，端口 %d，实装 %s）· %s" % (
        key, cfg["label"], cfg["port"], ver, "运行中" if running else "已停止")


def _stop_for_uninstall(key, port):
    """卸载前停掉该槽。**在后台线程里被调用** —— 只做进程操作，不碰任何 tk 控件。

    走 dsh_kill 的「签名 + 端口」门控：端口若被非 dsh 程序占着，它会拒绝杀
    （那正是我们想要的 —— 卸载不该顺手结束别人的软件）。"""
    p = procs.get(key)
    tracked = p.pid if (p is not None and p.poll() is None) else None
    if dsh_kill is not None:
        dsh_kill.kill_version(key, SIG[key], port, tracked_pid=tracked)
    elif tracked is not None:
        _kill_pid_tree(tracked, key)
    procs[key] = None
    # 等端口真正释放（最多 10 秒）：进程刚收到终止信号时文件可能还被占用，
    # 立刻删 node_modules 会撞 "另一个程序正在使用此文件"。
    for _ in range(20):
        if not port_open(port):
            return
        time.sleep(0.5)


def detached_manager_dialog():
    """「已移出管理」独立窗口：把以前移出的槽原样接回来。

    2026-11-01 从卸载窗里拆出来。用户反馈：只想卸一个版本，弹出来的窗里
    却塞着一整块"已移出管理（可重新纳入）"，感觉像两个功能被硬拼在一起。
    现在的分工：
      · 「卸载 / 移出…」 —— 只处理**在管**的槽（卸 or 移出）
      · 「已移出管理」   —— 只处理**已移出**的槽（重新纳入）
    一个窗口只回答一个问题。
    """
    recs = _detached_records()
    if not recs:
        messagebox.showinfo(
            "没有已移出的版本",
            "当前没有「已移出管理」的版本。\n\n"
            "移出 = 只从 config.json 里摘掉记录，磁盘上一个字节都不动；\n"
            "槽里的 dsh.bat 仍然可以双击启动。想放走某个版本时，\n"
            "在「卸载 / 移出…」窗口里选「移出管理（不删文件）」。")
        return
    win = tk.Toplevel(root)
    win.title("已移出管理")
    win.transient(root)
    win.resizable(False, False)
    _apply_icon(win)
    ttk.Label(win, text="已移出管理",
              font=("Microsoft YaHei UI", 10, "bold")).pack(padx=20, pady=(16, 2),
                                                           anchor="w")
    ttk.Label(win,
              text="这些版本的文件都还在原处，只是不再由管理器跟踪。\n"
                   "勾选后点「重新纳入」，会原样接回来（端口沿用、磁盘不动）。",
              foreground=_MUTED, justify="left", wraplength=470
              ).pack(padx=20, pady=(0, 8), anchor="w")

    box = ttk.LabelFrame(win, text="已移出的槽", padding=(10, 6))
    box.pack(padx=20, pady=(0, 10), fill="both", expand=True)
    dvar = {}
    for r in recs:
        v = tk.BooleanVar(value=False)
        dvar[r["root"]] = v
        ttk.Checkbutton(
            box,
            text="%s（%s，端口 %s）\n    %s" % (
                r.get("label") or "?", r.get("version") or "版本未知",
                r.get("port") or "?", r.get("root")),
            variable=v).pack(anchor="w", pady=1)

    def on_readd():
        roots = [p for p, v in dvar.items() if v.get()]
        if not roots:
            messagebox.showwarning("还没选", "请勾选要重新纳入的槽。")
            return
        try:
            win.destroy()
        except Exception:
            pass
        _do_readd(roots)

    btns = ttk.Frame(win)
    btns.pack(padx=20, pady=(0, 16))
    ttk.Button(btns, text="重新纳入勾选的", style="Green.TButton",
               command=on_readd).pack(side="left", padx=4)
    ttk.Button(btns, text="关闭", command=win.destroy).pack(side="left", padx=4)
    win.bind("<Escape>", lambda e: win.destroy())
    win.grab_set()


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
        messagebox.showerror("无法卸载",
                             "缺少 dsh_uninstall_ops.py / dsh_install_ops.py 模块，"
                             "请确认管理器目录完整。")
        return
    if not SLOT_KEYS:
        messagebox.showinfo("没有可卸载的版本",
                            "当前管理器没有管理任何 dsh 版本。\n\n"
                            "（想找回以前「移出管理」的版本？点卡片区的"
                            "「已移出管理」按钮。）")
        return

    win = tk.Toplevel(root)
    win.title("卸载 / 移出管理")
    win.transient(root)
    win.resizable(False, False)
    _apply_icon(win)
    ttk.Label(win, text="卸载 = 删掉它；移出管理 = 放它自己跑",
              font=("Microsoft YaHei UI", 10, "bold")).pack(padx=20, pady=(16, 4))
    ttk.Label(win, text="两个动作改的都是 config.json；区别在磁盘上的文件动没动。",
              foreground=_MUTED).pack(padx=20, pady=(0, 8))

    pick = tk.StringVar(value=SLOT_KEYS[0] if SLOT_KEYS else "")
    if SLOT_KEYS:
        box = ttk.LabelFrame(win, text="在管的版本槽（一次一个）", padding=(10, 6))
        box.pack(padx=20, pady=(0, 8), fill="both", expand=True)
        for key in SLOT_KEYS:
            ttk.Radiobutton(box, text=_slot_brief(key), value=key, variable=pick
                            ).pack(anchor="w", pady=1)

    backup_root = os.path.join(INSTALL_BASE, "dsh备份")
    ttk.Label(win, justify="left", wraplength=470, foreground=_MUTED,
              text=("【卸载】停进程 → 备份 → 删除整个槽目录 → 从 config 移除\n"
                    "  备份到 %s\\<槽>-userdata-<时间>\\：.dsh-home（会话/设置/凭据/\n"
                    "  插件配置）+ 插件清单 + 版本信息；**不含** node_modules 与 node。\n"
                    "  ⚠ 你自己放进槽目录的东西（笔记、补丁、下载的文件）也会一起删掉且不进备份。\n"
                    "    要留请先复制出去。\n\n"
                    "【移出管理】只从 config 移除，**磁盘一个字节都不动**\n"
                    "  · 槽里的 dsh.bat / kill-<端口>.bat / dsh-cli.bat 全都还在，\n"
                    "    双击就能启动 —— 管理器从没装过它也一样（bat 里全是绝对路径）\n"
                    "  · 记进「已移出」列表，随时能在下面一键重新纳入\n"
                    "  · 正在运行的槽要先点卡片上的「停止」才允许移出\n"
                    "  · 全部移出后安装根解锁，可以换到别的位置再装 / 纳入新的 dsh"
                    % backup_root)).pack(padx=20, pady=(0, 10), anchor="w")

    def on_ok():
        key = pick.get()
        if not key or key not in SLOT_KEYS:
            messagebox.showwarning("还没选版本", "请选中一个要卸载的版本槽。")
            return
        keys = [key]
        detail = "\n".join("  · " + _slot_brief(k) for k in keys)
        if not messagebox.askyesno(
                "确认卸载",
                "即将卸载这个版本：\n\n%s\n\n"
                "整个槽目录会被删除（不可恢复）—— 包括你自己放进这个槽目录里的\n"
                "任何文件（那些不会进备份）。\n\n"
                "删之前会先备份用户数据、插件清单与版本信息到：\n%s\n\n"
                "只想让它脱离管理、文件都留着？请按「取消」，改用「移出管理」。\n\n"
                "确定继续？" % (detail, backup_root), default="no", icon="warning"):
            return
        try:
            win.destroy()
        except Exception:
            pass
        _do_uninstall(keys)

    def on_detach():
        key = pick.get()
        if not key or key not in SLOT_KEYS:
            messagebox.showwarning("还没选版本", "请选中一个要移出的版本槽。")
            return
        cfgk = VC.get(key) or {}
        if not messagebox.askyesno(
                "确认移出管理",
                "即将把这个版本移出管理：\n\n  · %s\n\n"
                "磁盘上的文件**一个都不会动** —— 槽目录、node_modules、node、\n"
                ".dsh-home（会话/凭据/插件配置）全都原样保留。\n\n"
                "移出后：\n"
                "  · 管理器不再显示它、也不再管它的进程；\n"
                "  · 直接双击 %s\\dsh.bat 就能启动它；\n"
                "  · 它会被记进「已移出」列表，随时可以重新纳入。\n\n"
                "确定继续？" % (_slot_brief(key), cfgk.get("label") or key),
                default="no"):
            return
        try:
            win.destroy()
        except Exception:
            pass
        _do_detach([key])

    btns = ttk.Frame(win)
    btns.pack(padx=20, pady=(0, 16))
    ttk.Button(btns, text="卸载选中", style="Red.TButton",
               command=on_ok).pack(side="left", padx=4)
    _b_det = ttk.Button(btns, text="移出管理（不删文件）", command=on_detach)
    _b_det.pack(side="left", padx=4)
    if not SLOT_KEYS:
        _b_det.state(["disabled"])
    ttk.Button(btns, text="取消", command=win.destroy).pack(side="left", padx=4)
    # 2026-11-01：「已移出管理」不再占主界面，收进这个窗口（它本就是这里的功能）
    try:
        _recs_n = len(_detached_records())
        if _recs_n:
            ttk.Button(btns, text="已移出管理（%d）…" % _recs_n,
                       command=detached_manager_dialog
                       ).pack(side="left", padx=(12, 4))
    except Exception:
        pass

    win.bind("<Escape>", lambda e: win.destroy())
    win.grab_set()


def _do_uninstall(keys):
    """后台卸载若干槽。进度窗在主线程回调 done 一次（不重复调）。"""
    _set_busy(False)
    log("[卸载] 开始：%s" % ", ".join(keys))

    def done(ok, payload, msg):
        _set_busy(True)
        msg = (payload or {}).get("message") or msg or ""
        if ok:
            log("[卸载] 完成：%s" % msg.replace("\n", " | "))
            messagebox.showinfo("卸载完成",
                                "%s\n\n管理器即将重启以反映新的版本列表。" % msg)
            _restart_or_tell("槽列表")
        else:
            log("[卸载] 未完成：%s" % msg.replace("\n", " | "))
            messagebox.showerror("卸载未完成", msg or "原因未知")

    # 进度窗必须先建并把 prog 传进 uninstall_slots：删 node_modules 在机械盘上
    # 要几十秒，没有 step 回调窗口就一直停在「准备中…」——2026-10-29 实测被当成
    # "卸载失败"（其实已经卸干净了）。
    prog = _GuiProgress("卸载 dsh 版本", done)

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
    _set_busy(False)
    log("[移出] 开始：%s" % ", ".join(keys))

    def done(ok, payload, msg):
        _set_busy(True)
        msg = (payload or {}).get("message") or msg or ""
        if ok:
            log("[移出] 完成：%s" % msg.replace("\n", " | "))
            messagebox.showinfo("已移出管理",
                                "%s\n\n管理器即将重启以反映新的版本列表。" % msg)
            _restart_or_tell("槽列表")
        else:
            log("[移出] 未完成：%s" % msg.replace("\n", " | "))
            messagebox.showerror("移出未完成", msg or "原因未知")

    prog = _GuiProgress("移出管理", done)

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


def _do_readd(roots):
    """后台把「已移出」的槽重新纳入管理：只改 config.json，磁盘一个字节都不动。

    端口原样保留 —— 槽里的 bat 已经把端口写死在 `--port` 里，改端口就得重写三件
    bat，那不是"重新纳入"该干的事（ops 层会在端口已被别的槽占用时给出警告）。"""
    _set_busy(False)
    log("[纳入] 重新纳入已移出的槽：%s" % ", ".join(roots))

    def done(ok, payload, msg):
        _set_busy(True)
        msg = (payload or {}).get("message") or msg or ""
        if ok:
            log("[纳入] 完成：%s" % msg.replace("\n", " | "))
            messagebox.showinfo("已重新纳入",
                                "%s\n\n管理器即将重启以反映新的版本列表。" % msg)
            _ensure_slots_visible(_installed_keys_from(payload))
            _restart_or_tell("槽列表")
        else:
            log("[纳入] 未完成：%s" % msg.replace("\n", " | "))
            messagebox.showerror("重新纳入未完成", msg or "原因未知")

    prog = _GuiProgress("重新纳入", done)

    def work():
        ok, payload, msg = UNINST.readd_slots(
            roots, CONFIG_PATH, HOME, BASE_DIR, BASE_DIR,
            versions_root=_versions_root(), progress=prog)
        if isinstance(payload, dict):
            payload["message"] = msg
        return (ok, payload, msg)

    OPS.run_in_thread(work, prog, None)


# ── 操作行按钮（必须在上面这些函数定义之后创建）──────────────────────────
# 「安装官方版 dsh」每点一次装到下一个空槽：已有 v1 → 装 v2，再点装 v3，再点装 v4；
# 槽满（MAX_SLOTS）会明确提示，并指向「卸载 dsh 版本…」。
# ── 2026-11-01：第二排按钮（安装 / 纳入 / 卸载）已全部移进「设置」窗口 ──
# 用户要求初始界面只留「槽 + 日志 + 全启停」，其余动作统一收进设置。



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

def _gui_sink_impl(line):
    log_text.configure(state="normal")
    log_text.insert("end", line + "\n", _log_tag(line))
    log_text.see("end")
    log_text.configure(state="disabled")

_gui_sink = _gui_sink_impl

_prev_state = {k: None for k in SLOT_KEYS}

# ── 后台探测线程（2026-09-27 启动提速）────────────────────────────────────
# 曾经 port_open + _dsh_pid_on_port 在 UI 线程逐槽直跑：启动快照 = 3×(netstat+CIM)
# ≈ 6 秒全在窗口出现之前；运行中每轮 refresh 还会周期性冻住界面。
# 现在：netstat/CIM 探测全部挪到这个 daemon 线程（配合 dsh_kill 的 TTL 缓存，
# 每轮只跑一次 netstat + 一次 CIM），UI 线程只读现成结果 —— 窗口秒开、轮询零卡顿。
_DETECT = {k: {"listen": None, "ext": None, "ts": 0.0} for k in SLOT_KEYS}

def _detect_worker():
    while True:
        for key in SLOT_KEYS:
            try:
                cfg = VC[key]
                listen = port_open(cfg["port"])
                ext = _dsh_pid_on_port(key) if listen else None
                d = _DETECT[key]
                d["listen"], d["ext"], d["ts"] = listen, ext, time.time()
            except Exception:
                pass    # 探测失败保持上一轮结果；下轮重试
        time.sleep(0.8)


def _log_state_change(key, prev, state, cfg, listen, ext_pid, tracked_p):
    """只在状态「类别」变化时写日志，避免每 1.5s 轮询刷屏；
    便于以后用 Agent 复盘 运行/外部运行/冲突/停止 的切换。"""
    pid = (tracked_p.pid if (tracked_p is not None and tracked_p.poll() is None) else None)
    if state == "running_tracked":
        log("[%s] 状态变化: 运行中（本管理器启动）pid=%s 端口=%d" % (key, pid, cfg["port"]))
    elif state == "running_external":
        log("[%s] 状态变化: 运行中（外部/上一会话启动，本管理器未跟踪）pid=%s 端口=%d"
            % (key, ext_pid, cfg["port"]))
    elif state == "starting":
        log("[%s] 状态变化: 启动中 pid=%s 端口=%d" % (key, pid, cfg["port"]))
    elif state == "external_conflict":
        who = _pids_on_port(cfg["port"])
        log("[%s] 状态变化: 端口 %d 被非 dsh 进程占用 pid=%s"
            "\n     点「停止」可看到占用者是谁；管理器不会自动结束别人的程序"
            % (key, cfg["port"], ",".join(str(x) for x in who) or "未知"))
    elif state == "stopped":
        log("[%s] 状态变化: 已停止 端口=%d" % (key, cfg["port"]))

def refresh(once=False):
    """刷新所有槽的状态行。once=True 表示只刷这一次、不另起定时链
    （体检出结果后要立刻把 ⚠ 显示出来，但不能再排一条 after 循环）。"""
    for key in SLOT_KEYS:
        _scan_slot(key)
        cfg = VC[key]
        # 实装版本每次刷新轻量重读：换了版本 / 外部重装都能自动反映到标题
        ver = _installed_version(cfg)
        if ver != INSTALLED.get(key):
            INSTALLED[key] = ver
            try: title_lbls[key].config(text=_slot_title(key))
            except Exception: pass
        p = procs.get(key); alive = proc_alive(p)
        d = _DETECT.get(key) or {}
        if not d.get("ts"):
            # 后台探测还没出第一批数据：先占位，不写状态日志（避免误报"已停止"）
            status_vars[key].set("检测中…" + _health_mark(key))
            try: status_lbls[key].configure(foreground=_MUTED)
            except Exception: pass
            continue
        listen, ext_pid = d.get("listen"), d.get("ext")
        if alive and listen:
            state, s = "running_tracked", "运行中  pid=%s  :%d  ✓" % (p.pid, cfg["port"])
        elif alive:
            state, s = "starting", "启动中  pid=%s  :%d  …" % (p.pid, cfg["port"])
        elif listen and ext_pid is not None:
            # 关键修正：外部（或上一会话）启动的同版本 dsh 仍在跑 —— 直接显示「运行中」，
            # 不再标「外部程序打开」。只有端口被非 dsh 进程占了才报冲突。
            state, s = "running_external", "运行中  pid=%s  :%d  ✓" % (ext_pid, cfg["port"])
        elif listen:
            state, s = "external_conflict", "端口被非 dsh 程序占用 :%d（点「停止」看是谁）" % cfg["port"]
        else:
            state, s = "stopped", "已停止  :%d" % cfg["port"]
            if procs[key] is not None:
                try:
                    procs[key].wait(timeout=0)     # 回收句柄，避免 Popen 一直挂着
                except Exception:
                    pass
                procs[key] = None
        # 状态类别变化才写日志（避免每 1.5s 刷屏）
        prev = _prev_state.get(key)
        if state != prev:
            _log_state_change(key, prev, state, cfg, listen, ext_pid, p)
            _prev_state[key] = state
        status_vars[key].set(s + _health_mark(key))
        # 状态文字按运行态上色（绿=运行 / 琥珀=启动中 / 红=冲突 / 灰=停止）
        try:
            status_lbls[key].configure(foreground=_STATUS_COLORS.get(state, _MUTED))
        except Exception:
            pass
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
    log("=== 启动快照：各槽运行态 ===")
    for key in SLOT_KEYS:
        cfg = VC[key]
        d = _DETECT.get(key) or {}
        listen, ext = d.get("listen"), d.get("ext")
        tracked = procs.get(key)
        if proc_alive(tracked):
            who = "本管理器跟踪 pid=%s" % tracked.pid
        elif ext is not None:
            who = "外部运行 pid=%s" % ext
        elif listen:
            who = "端口被非 dsh 进程占用"
        else:
            who = "未运行"
        log("[%s] 端口=%d 监听=%s 判定=%s" % (key, cfg["port"], listen, who))
    log("=== 启动快照结束 ===")
    _log_orphan_shells()

def on_close():
    log("=== 管理器关闭 (pid=%d) ===" % os.getpid())
    # 显式保留所有运行中的 dsh：关闭管理器【不会】连带关闭它启动 / 跟踪的 dsh。
    # 原因：subprocess.Popen 默认不绑定进程组，pythonw 无控制台也不会向子进程发
    # Ctrl-C，子进程在父进程退出后继续存活。这里只做日志确认，便于以后排查。
    for key in SLOT_KEYS:
        cfg = VC[key]
        ext = _dsh_pid_on_port(key)
        tracked = procs.get(key)
        if proc_alive(tracked):
            log("[%s] 关闭时保留运行中的 dsh（本管理器跟踪 pid=%s 端口=%d）" % (key, tracked.pid, cfg["port"]))
        elif ext is not None:
            log("[%s] 关闭时保留运行中的 dsh（外部 pid=%s 端口=%d）" % (key, ext, cfg["port"]))
        else:
            log("[%s] 关闭时无运行中的 dsh（端口=%d）" % (key, cfg["port"]))
    try:
        if LOG_FH:
            LOG_FH.close()
    except Exception:
        pass
    root.destroy()

root.protocol("WM_DELETE_WINDOW", on_close)

# 启动日志
log("=== 管理器启动 pid=%d ===" % os.getpid())

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
        # 没有由本管理器拉起的 dsh 在跑 → 无从实测，返回"未知"而不是"不可用"。
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
    return {True: "可用", False: "不可用"}.get(v, "未知")

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
        log("[权限] 能力实测：读命令行=%s，结束进程=%s"
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
            log("[权限] 读不到进程命令行 → 端口占用者可能显示不出程序名；"
                "已降级为 Get-Process，不影响启停主流程")
        if _kill_ok is False:
            log("[权限] 无法结束同用户进程（罕见：多半是安全软件拦截）→ "
                "「停止」可能无效；此时会由停止动作本身报错，不再启动期打扰")

    root.after(300, _poll)

_start_capability_check()

# 历史日志里可能残留旧版本写入的 token（子进程写的入口行之外，还有管理器自己写的
# "打开浏览器 -> ...?token=..." 之类）：启动时全量就地掩码一次。
_n_scrubbed = _scrub_existing_log()
if _n_scrubbed:
    log("已就地掩码历史日志里的 %d 个 token（等长覆盖，文件长度不变）" % _n_scrubbed)
log("install_base = %s" % INSTALL_BASE)
log("NPM_GLOBAL = %s" % (NPM_GLOBAL or "(未探测到，<npm_global> 占位符不可用)"))
for key in SLOT_KEYS:
    cfg = VC[key]
    log("[%s] label=%s" % (key, cfg["label"]))
    log("[%s] 实装版本=%s" % (key, INSTALLED.get(key) or "未知（读 package.json 失败，可能未装好）"))
    log("[%s] cmd  = %s" % (key, " ".join(_build_cmd(key))))
    log("[%s] DSH_HOME=%s  port=%d  browser=%s"
        % (key, cfg["dsh_home"] or "(默认 ~/.dsh)", cfg["port"], cfg["browser"]))
    # 逐槽对一遍「形状」：自带 node / bin.js / 数据目录 / 临时目录 / 三件 bat。
    # 只写日志不弹框 —— 缺个 kill bat 之类不值得打断启动，但排障时能一眼看到
    # 「哪个槽和别的槽不一样」。启动失败时的弹框在 _start_slot 里另有一套。
    if OPS is not None and hasattr(OPS, "slot_shape_problems"):
        for _lv, _t in OPS.slot_shape_problems(cfg, INSTALL_BASE, HOME, BASE_DIR):
            log("[%s] 槽结构%s：%s" % (key, _lv, _t))
# 启动快照改为延迟执行：等后台探测线程出第一批数据（一轮 ≈ 3-4 秒），
# 纯写日志零开销；窗口在此之前早已显示。
root.after(6000, _log_state_snapshot)
log("log file = %s" % LOG_FILE)

# 窗口初始大小按槽数算（见上方 _SLOT_MIN_W 的说明）：槽多时自动加宽，
# 保证每槽标题与按钮文字完整可见。日志区随窗口拉伸占满剩余高度。
root.geometry("%dx600" % _win_w)
root.columnconfigure(0, weight=1)
root.rowconfigure(2, weight=1)   # 日志区占满剩余高度（row 2 = 日志；底部按钮已移到标题栏）
root.update_idletasks()
# 窗口已 realize：再补设一次图标（双保险——就算 Tk 在 realize 时动过图标也会被这里盖回海豚）
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

# 启动后自动检测本机已有的 dsh，问用户是否纳入管理。
# 延后到窗口稳定之后再问：此刻 config/日志/控件都已就绪，弹窗不会撞上初始化。
# 默认答案是「否」——纳入会复制几百 MB 并改配置，必须用户明确同意。
if dsh_adopt is not None:
    root.after(1500, scan_and_offer_adopt)

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
    if not FRESH_START:
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

        ttk.Label(_f, text="还没有安装 dsh",
                  font=("Microsoft YaHei UI", 14, "bold"), style="Card.TLabel"
                  ).pack(anchor="w", pady=(0, 8))
        ttk.Label(_f,
                  text=("点下面的「安装 dsh」选一个版本号就会自动下载并配好隔离环境；\n"
                        "如果你机器上已经装过 dsh，点「扫描并纳入」把它接进来管理。\n"
                        "每个版本各自独立的数据目录、端口与 node 运行时，互不影响。"),
                  style="Card.TLabel", justify="left", wraplength=560
                  ).pack(anchor="w", pady=(0, 16))

        _b = ttk.Frame(_f, style="Card.TFrame")
        _b.pack(anchor="w")
        ttk.Button(_b, text="安装 dsh", style="Brand.TButton",
                   command=install_official_version).pack(side="left", padx=(0, 8))
        ttk.Button(_b, text="扫描并纳入",
                   command=lambda: scan_and_offer_adopt(manual=True)
                   ).pack(side="left")
    except Exception as e:
        log("[引导] 零槽引导构建失败（不影响使用，可点底部按钮安装）：%s" % e)


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
        return "读取 config.json 失败：%s" % e
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
        return "写入 config.json 失败：%s" % e
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
        return "读取 config.json 失败：%s" % e
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
        return "写入 config.json 失败：%s" % e
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
        return "写入 config.json 失败：%s" % e
    return ""


def _save_version_field(key, field, value):
    """把单个槽的某个字段写回 config.json。返回空串 = 成功，否则是错误说明。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        return "读取 config.json 失败：%s" % e
    vs = data.get("versions")
    if not isinstance(vs, dict) or key not in vs:
        return "config.json 里没有槽 %s（可能已被删除，请重开管理器）" % key
    if not isinstance(vs[key], dict):
        return "config.json 里槽 %s 的格式不对（不是对象）" % key
    vs[key][field] = value
    return _write_config_data(data)


def _save_top_field(field, value):
    """把顶层字段（install_base / temp_root）写回 config.json。返回空串 = 成功。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        return "读取 config.json 失败：%s" % e
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
    win.title("把 dsh 版本放在哪")
    win.transient(root)
    win.resizable(False, False)
    _apply_icon(win)
    ttk.Label(win, text="先把位置定下来",
              font=("Microsoft YaHei UI", 10, "bold")
              ).pack(padx=20, pady=(16, 4), anchor="w")
    ttk.Label(win,
              text="这是第一次安装/纳入 —— 位置一旦确定就不再改动。\n"
                   "要换位置，得先把所有版本卸载干净（卸载会先备份用户数据）。",
              foreground=_MUTED, justify="left", wraplength=470
              ).pack(padx=20, pady=(0, 10), anchor="w")

    r = ttk.Frame(win)
    r.pack(padx=20, fill="x")
    ttk.Label(r, text="安装根目录", width=11).pack(side="left")
    pv = tk.StringVar(value=os.path.normpath(INSTALL_BASE))
    ent = ttk.Entry(r, textvariable=pv, width=40, font=("Consolas", 9))
    ent.pack(side="left", padx=6, fill="x", expand=True)

    def _pick():
        p = filedialog.askdirectory(title="选择 dsh 版本的存放位置", parent=win,
                                    initialdir=pv.get() or HOME)
        if p:
            pv.set(os.path.normpath(p))

    ttk.Button(r, text="浏览…", width=7, command=_pick).pack(side="left")

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
        prev.config(text="版本装进：%s\n"
                         "卸载备份：%s\n"
                         "临时文件：各槽目录下的 .tmp"
                         % (os.path.join(b, "dsh-versions"),
                            os.path.join(b, "dsh备份")))

    pv.trace_add("write", _refresh)
    _refresh()

    def on_ok(_evt=None):
        b = (pv.get() or "").strip()
        if not b:
            messagebox.showwarning("还没填目录", "请先选择 dsh 版本的存放位置。",
                                   parent=win)
            return
        b = os.path.normpath(b)
        if not os.path.isdir(b):
            try:
                os.makedirs(b, exist_ok=True)
            except Exception as e:
                messagebox.showerror("无法创建目录", "%s\n\n%s" % (b, e), parent=win)
                return
        err = _save_top_field("install_base", _cfg_form_base(b))
        if err:
            messagebox.showerror("保存失败", err, parent=win)
            return
        global INSTALL_BASE
        INSTALL_BASE = b
        CFG["install_base"] = _cfg_form_base(b)
        log("[设置] 安装根目录（首次确定）→ %s" % b)
        res["ok"] = True
        win.destroy()

    btns = ttk.Frame(win)
    btns.pack(padx=20, pady=(12, 16))
    ttk.Button(btns, text="就用这个位置", style="Green.TButton",
               command=on_ok).pack(side="left", padx=4)
    ttk.Button(btns, text="取消", command=win.destroy).pack(side="left", padx=4)
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
    df = ttk.LabelFrame(parent, text="目录", padding=(8, 6))
    df.pack(padx=padx, pady=(0, 10), fill="x")
    locked = bool(SLOT_KEYS)

    def _pick(var):
        p = filedialog.askdirectory(title="选择目录", parent=df,
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
            ttk.Button(r, text="浏览…", width=7,
                       command=lambda v=var: _pick(v)).pack(side="left")
        return var

    ib = _row("安装根目录", os.path.normpath(INSTALL_BASE), readonly=locked)
    if locked:
        # 2026-11-01：安装根锁定后不能"改"，但**可以整体移动**（换盘/整理）。
        # 移动 = 复制 + 校验 + 改配置 + 删旧位置，安全性由 dsh_move 保证。
        try:
            ttk.Button(df, text="移动安装根目录…", command=move_install_root_dialog
                       ).pack(anchor="w", pady=(2, 0))
        except Exception:
            pass
    tr = _row("临时目录根", os.path.normpath(_temp_root()))
    if locked:
        # 只留一句必要的（不可逆操作的后果必须就地说明），其余细节移入使用说明。
        ttk.Label(df,
                  text="已确定，不可更改（要换位置请先卸载全部版本）",
                  foreground=_AMBER
                  ).pack(anchor="w", pady=(4, 0))
    # 2026-11-01：原来这里还有一段「安装根目录 = 版本装在哪（<路径>/dsh-versions 下…）」。
    # 用户指出**显示值与说明里的路径对不上**：输入框显示安装根本身，
    # 说明里写的却是"安装根/dsh-versions"（槽的实际落点）。两者都对，但并列摆着看不懂。
    # 现在把"槽到底装在哪"作为**单独一行只读信息**显示，不再塞进长句。
    try:
        _slots_root = os.path.join(os.path.normpath(INSTALL_BASE), "dsh-versions")
        ttk.Label(df, text="槽实际位置：%s\\<槽名>" % _slots_root,
                  foreground=_MUTED).pack(anchor="w", pady=(6, 0))
    except Exception:
        pass
    return ib, tr


def _apply_dir_settings(ib_var, tr_var):
    """保存「目录」设置。返回 (ok, 提示文本)；失败时文本是原因，由调用方弹框。"""
    new_base = os.path.normpath((ib_var.get() or "").strip() or INSTALL_BASE)
    new_temp = os.path.normpath((tr_var.get() or "").strip() or _temp_root())
    base_changed = _norm_cmp(new_base) != _norm_cmp(INSTALL_BASE)
    temp_changed = _norm_cmp(new_temp) != _norm_cmp(_temp_root())
    if not base_changed and not temp_changed:
        return (True, "")
    if base_changed and temp_changed:
        # 两个一起改会算出「基于新 base 的临时目录」，但槽还没搬过去 ——
        # 等于把 TEMP 指到一片空地。宁可让用户分两步走。
        return (False, "安装根目录和临时目录根不能一次同时改。\n\n"
                       "请先改安装根目录并保存，再回来改临时目录根。")

    if base_changed:
        # 界面已经把安装根做成只读了，这里是第二道闸：万一有别的入口（旧版本
        # 留下的回调、探针脚本）把 var 改了，也不能真的写进 config。
        if SLOT_KEYS:
            return (False, "安装根目录已确定，不能再改。\n\n"
                           "它是首次安装/纳入时选定的位置。管两个不同位置的版本会让"
                           "备份目录、临时目录、新建槽的落点全部分叉，所以不支持中途改。\n\n"
                           "要换位置：先把所有版本卸载干净（卸载会先备份用户数据），"
                           "再重新安装 —— 那时会重新问一次位置。")
        if not os.path.isdir(new_base):
            return (False, "目录不存在：\n%s" % new_base)
        err = _save_top_field("install_base", _cfg_form_base(new_base))
        if err:
            return (False, err)
        log("[设置] 安装根目录 → %s" % new_base)
        return (True, "安装根目录已设为：\n%s\n\n"
                      "版本会装进 %s；卸载备份落在 %s。\n"
                      "必须**重启管理器**才生效。"
                      % (new_base, os.path.join(new_base, "dsh-versions"),
                         os.path.join(new_base, "dsh备份")))

    # ── 临时目录根 ───────────────────────────────────────────────────
    if OPS is None:
        return (False, "缺少 dsh_install_ops.py，无法迁移临时目录。")
    try:
        os.makedirs(new_temp, exist_ok=True)
    except Exception as e:
        return (False, "无法创建目录 %s：%s" % (new_temp, e))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        return (False, "读取 config.json 失败：%s" % e)

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
    log("[设置] 临时目录根 → %s（%s；重写 %d 个 bat）"
        % (new_temp, msg, len(made)))

    lines = ["临时目录已迁到：", "  " + new_temp, ""]
    for ch in changes:
        if ch["old_abs"] and _norm_cmp(ch["old_abs"]) != _norm_cmp(ch["new_abs"]):
            lines.append("  %-6s %s → %s" % (ch["key"], ch["old_abs"], ch["new_abs"]))
    lines.append("")
    lines.append("已重写 %d 个 bat（TEMP/TMP 指向新位置）。" % len(made))
    if failed:
        lines.append("有 %d 个 bat 写入失败，请看日志。" % len(failed))
    lines.append("旧临时目录里的文件没有搬（那是每次启动重建的垃圾），可手动删除。")
    return (True, "\n".join(lines))


def _dirs_only_dialog():
    """零槽时的设置窗：只给「目录」。

    为什么零槽也要能进设置：最需要改安装根的时机恰恰是**装第一个版本之前**
    （C 盘满了想装到 D 盘）。老代码在这里直接弹「先装一个再来设置」，等于
    把唯一的入口堵死了。
    """
    win = tk.Toplevel(root)
    win.title("目录设置")
    win.transient(root)
    win.resizable(False, False)
    _apply_icon(win)
    ttk.Label(win, text="目录设置",
              font=("Microsoft YaHei UI", 10, "bold")
              ).pack(padx=20, pady=(16, 2), anchor="w")
    ttk.Label(win, text="还没有安装任何 dsh 版本 —— 正好先决定它装在哪。",
              foreground=_MUTED).pack(padx=20, pady=(0, 10), anchor="w")
    ib_var, tr_var = _dir_group(win)

    def on_ok():
        ok, msg = _apply_dir_settings(ib_var, tr_var)
        if not ok:
            messagebox.showerror("保存失败", msg, parent=win)
            return
        try:
            win.destroy()
        except Exception:
            pass
        if msg:
            messagebox.showinfo("已保存", msg)

    btns = ttk.Frame(win)
    btns.pack(padx=20, pady=(0, 16))
    ttk.Button(btns, text="保存", style="Green.TButton",
               command=on_ok).pack(side="left", padx=4)
    ttk.Button(btns, text="取消", command=win.destroy).pack(side="left", padx=4)
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
    pal = _palette()
    _INK = pal["ink"]
    _MUTED = pal["muted"]
    _DIVIDER = pal["divider"]
    _NEUTRAL = pal["neutral"]
    _NEUTRAL_BD = pal["neutral_bd"]
    _NEUTRAL_D = pal["neutral_d"]
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
    st.configure("Card.TFrame", background=pal["card_bg"])
    st.configure("Card.TLabel", background=pal["card_bg"], foreground=pal["ink"])
    st.configure("TLabelframe", background=pal["app_bg"], bordercolor=pal["card_bd"])
    st.configure("TLabelframe.Label", background=pal["app_bg"],
                 foreground=pal["muted"])
    st.configure("TSeparator", background=pal["divider"])
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
    """读 Windows 的"应用使用深色模式"开关。读不到就当浅色。"""
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
        log("[主题] 应用失败：%s" % e)
    if persist:
        try:
            _save_ui_field("theme", _THEME_CUR["mode"])
        except Exception:
            pass
    log("[主题] 已切换为：%s" % {"system": "跟随系统", "light": "浅色",
                                 "dark": "深色"}.get(_THEME_CUR["mode"], mode))


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
        log("[配置] 写入 ui.%s 失败：%s" % (key, e))
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


def _make_side_panel(win, width=380):
    """把任意 Toplevel 变成"并排可吸附"的侧栏。

    返回一个 bind 好的处理器；调用方只需在创建完控件后调用一次。
    """
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
            try:
                _wrect = _win_outer_rect(w)
                cw_ = max(360, _wrect[2] - _wrect[0])
                ch_ = max(200, _wrect[3] - _wrect[1])
            except Exception:
                cw_ = max(360, int(_DOCK.get("w") or w.winfo_width()))
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
        cw_ = max(360, wr[2] - wr[0])
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
            os.startfile(PROJECT_URL)
        except Exception as e:
            messagebox.showinfo("项目主页", "打不开浏览器，请手动访问：\n%s\n\n(%s)"
                                % (PROJECT_URL, e))


def _open_user_guide():
    """打开详细使用说明（文档文件）。找不到时给出明确说明。"""
    p = os.path.join(BASE_DIR, "docs", "dsh管理器使用说明.md")
    if os.path.exists(p):
        try:
            os.startfile(p)
            return
        except Exception:
            pass
    messagebox.showinfo("详细使用说明",
                        "找不到文档文件：\n%s\n\n"
                        "完整说明见程序目录下的 docs/ 文件夹，"
                        "或项目主页的 README。" % p)


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
    win.title("关于 DSH 版本管理器")
    win.transient(root)
    win.resizable(False, False)
    _apply_icon(win)

    ttk.Label(win, text="DSH 版本管理器（dsh-tm）",
              font=("Microsoft YaHei UI", 13, "bold")).pack(padx=24, pady=(20, 2),
                                                           anchor="w")
    ttk.Label(win, text="多版本隔离 · 独立进程 / 端口 / 数据 / 运行时 / 临时目录",
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

    _row(0, "版本", _safe(lambda: _app_version()))
    _row(1, "槽上限", "%d 个" % MAX_SLOTS)
    _row(2, "当前槽数", "%d 个" % len(SLOT_KEYS))
    _row(3, "安装根目录", os.path.normpath(INSTALL_BASE))
    _row(4, "程序目录", BASE_DIR)
    _row(5, "运行环境", "%s（%s）" % (
        _safe(lambda: "%d.%d.%d" % sys.version_info[:3]),
        _safe(lambda: os.path.basename(sys.executable) or "python")))
    _row(6, "日志文件", LOG_FILE)

    ttk.Separator(win, orient="horizontal").pack(fill="x", padx=24, pady=(8, 0))

    ttk.Label(win, text="使用说明", font=("Microsoft YaHei UI", 10, "bold")
              ).pack(padx=24, pady=(12, 4), anchor="w")
    ttk.Label(win,
              justify="left", wraplength=470, foreground=_INK,
              text=("· 「安装官方版 dsh」→ 选版本号 → 自动下载并配好隔离环境\n"
                    "· 卡片上「启动 / 停止」管单个版本；「打开浏览器」带上登录 token\n"
                    "· 拖动卡片可调整左右顺序；「设置」里能隐藏不看的槽\n"
                    "· 「卸载 / 移出…」：卸载会先备份用户数据再删；\n"
                    "  移出只改配置、磁盘一个字节不动（之后可在「已移出管理」接回）\n"
                    "· 「槽体检」只读检查，报告问题但不替你改\n"
                    "· 命令行：python dsh-tm.py list | start --all | stop --all")
              ).pack(padx=24, anchor="w")

    btns = ttk.Frame(win)
    btns.pack(padx=24, pady=(14, 18), fill="x")

    ttk.Button(btns, text="打开详细使用说明", command=_open_user_guide).pack(side="left")
    ttk.Button(btns, text="检查运行环境", command=_show_env_report).pack(
        side="left", padx=(8, 0))
    # 「槽体检」从主界面底部搬到这里（一年用不了几次的排障工具）
    ttk.Button(btns, text="槽体检",
               command=lambda: _run_health(deep=True, dialog=True)
               ).pack(side="left", padx=(8, 0))
    ttk.Button(btns, text="关闭", command=win.destroy).pack(side="right")
    win.bind("<Escape>", lambda e: win.destroy())
    win.grab_set()


def _app_version():
    """读程序版本：优先 config.json 的 version 字段，其次 git describe，最后兜底。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            v = (json.load(f) or {}).get("version")
        if v:
            return str(v)
    except Exception:
        pass
    return "1.0"


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
        messagebox.showerror("无法移动", "缺少 dsh_move.py 模块：%s" % e)
        return
    if not SLOT_KEYS:
        messagebox.showinfo("没有可移动的版本",
                            "当前管理器没有管理任何 dsh 版本。\n\n"
                            "装第一个版本时会让您选位置，那时选好即可。")
        return

    win = tk.Toplevel(root)
    win.title("移动安装根目录")
    win.transient(root)
    win.resizable(False, False)
    _apply_icon(win)

    ttk.Label(win, text="移动安装根目录",
              font=("Microsoft YaHei UI", 10, "bold")
              ).pack(padx=20, pady=(16, 4), anchor="w")
    ttk.Label(win,
              text=("把已装的所有版本整体搬到另一个位置。\n"
                    "过程：复制 → 校验（文件数 + 字节数）→ 改配置 → 删旧位置。\n"
                    "任何一步失败都会保留旧位置、不动配置。"),
              foreground=_MUTED, justify="left", wraplength=470
              ).pack(padx=20, pady=(0, 10), anchor="w")

    cur = ttk.Frame(win)
    cur.pack(padx=20, fill="x")
    ttk.Label(cur, text="当前位置", width=10).pack(side="left")
    ttk.Label(cur, text=os.path.normpath(INSTALL_BASE),
              foreground=_MUTED).pack(side="left")

    newv = tk.StringVar(value="")

    row = ttk.Frame(win)
    row.pack(padx=20, pady=(8, 0), fill="x")
    ttk.Label(row, text="新位置", width=10).pack(side="left")
    ent = ttk.Entry(row, textvariable=newv, width=42, font=("Consolas", 9))
    ent.pack(side="left", padx=(0, 6), fill="x", expand=True)

    def _pick():
        p = filedialog.askdirectory(title="选择新的安装根目录", parent=win,
                                     initialdir=os.path.normpath(INSTALL_BASE))
        if p:
            newv.set(os.path.normpath(p))

    ttk.Button(row, text="浏览…", width=7, command=_pick).pack(side="left")

    ttk.Label(win,
              text=("· 新位置下会创建 dsh-versions\\ 存放各版本槽\n"
                    "· 磁盘空间要够（每个版本约 0.7～1.3 GB）\n"
                    "· 移动期间请勿启动或停止任何版本"),
              foreground="#8A5A00", justify="left", wraplength=470
              ).pack(padx=20, pady=(10, 0), anchor="w")

    def on_ok():
        target = (newv.get() or "").strip()
        if not target:
            messagebox.showwarning("还没选位置", "请先选择新的安装根目录。")
            return
        target = os.path.normpath(target)
        slot_names = []
        for k in SLOT_KEYS:
            nm = os.path.basename(os.path.normpath(_version_root(VC[k]) or ""))
            if nm:
                slot_names.append(nm)
        plan = MV.plan_move(INSTALL_BASE, target, slot_names)
        if not plan["ok"]:
            messagebox.showerror("无法移动", plan["error"])
            return
        body = "\n".join("  · " + s for s in plan["steps"])
        if not messagebox.askyesno(
                "确认移动",
                "即将把 %d 个版本搬到：\n%s\n\n%s\n\n"
                "旧位置在全部校验通过后才会被删除。\n\n继续吗？"
                % (len(plan["slots"]), target, body)):
            return
        try:
            win.destroy()
        except Exception:
            pass
        _do_move_root(MV, plan)

    btns = ttk.Frame(win)
    btns.pack(padx=20, pady=(16, 16), fill="x")
    ttk.Button(btns, text="开始移动", style="Green.TButton",
               command=on_ok).pack(side="left")
    ttk.Button(btns, text="取消", command=win.destroy).pack(side="left", padx=(8, 0))
    win.bind("<Escape>", lambda e: win.destroy())
    win.grab_set()


def _do_move_root(MV, plan):
    """后台执行搬迁，进度窗 + 完成后提示重启。"""
    log("[移动] 开始：%s → %s（%d 个槽）"
        % (plan["src_base"], plan["dst_base"], len(plan["slots"])))

    def done(ok, message):
        _set_busy(True)
        if ok:
            log("[移动] 完成：%s" % str(message).replace("\n", " | "))
            messagebox.showinfo(
                "移动完成",
                "%s\n\n管理器需要重启才能使用新位置。" % message)
            _restart_or_tell("安装位置")
        else:
            log("[移动] 失败：%s" % str(message).replace("\n", " | "))
            messagebox.showerror("移动失败", str(message))

    prog = _GuiProgress("移动安装根目录", done)

    def work():
        try:
            ok, msg = MV.move_now(
                plan, CONFIG_PATH,
                progress=lambda t: prog.on_step(t))
            return (ok, msg, "")
        except Exception as e:
            return (False, "", "移动过程异常：%s" % e)

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
        messagebox.showinfo("日志已为空", "日志文件本来就是空的。",
                            parent=parent)
        return
    if not messagebox.askyesno(
            "清空日志",
            "当前日志：%s（%d 行）\n位置：%s\n\n"
            "清空后**无法恢复**，但不影响任何已安装版本、配置和进程。\n\n确定清空吗？"
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
        messagebox.showerror("清空失败", err, parent=parent)
        return
    log("[清理] 日志已清空（用户手动操作）")


def _open_log_folder(parent=None):
    try:
        if os.name == "nt":
            os.startfile(os.path.dirname(LOG_FILE))
        else:
            messagebox.showinfo("日志位置", LOG_FILE, parent=parent)
    except Exception as e:
        messagebox.showinfo("日志位置", "%s\n\n(%s)" % (LOG_FILE, e), parent=parent)


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
    canvas.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")

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
    def _wheel(evt):
        try:
            # 只在指针确实落在本 canvas 内时才滚
            x, y = canvas.winfo_pointerxy()
            cx, cy = canvas.winfo_rootx(), canvas.winfo_rooty()
            if not (cx <= x <= cx + canvas.winfo_width() and
                    cy <= y <= cy + canvas.winfo_height()):
                return
            canvas.yview_scroll(int(-1 * (evt.delta / 120)), "units")
        except Exception:
            pass

    def _bind_wheel(widget):
        """把滚轮绑到这个控件及其所有子控件（只作用于设置窗内部）。"""
        try:
            widget.bind("<MouseWheel>", _wheel)
        except Exception:
            pass
        try:
            for ch in widget.winfo_children():
                _bind_wheel(ch)
        except Exception:
            pass

    try:
        canvas.bind("<MouseWheel>", _wheel)
        inner.bind("<MouseWheel>", _wheel)
        # 子控件是后续才创建的，延后一帧再统一绑一次
        canvas.after(300, lambda: _bind_wheel(canvas))
    except Exception:
        pass
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
    df = ttk.LabelFrame(parent, text="目录", padding=(12, 10))
    df.pack(padx=16, fill="x", pady=(0, 10))
    locked = bool(SLOT_KEYS)

    row = ttk.Frame(df)
    row.pack(fill="x")
    ttk.Label(row, text="安装根", width=8).pack(side="left")
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
            log("[设置] 安装根目录已改为：%s（原：%s）" % (INSTALL_BASE, before))
            try:
                messagebox.showinfo(
                    "安装位置已更新",
                    "新的安装根目录：\n%s\n\n"
                    "之后安装/纳入的版本会放这里。\n"
                    "（已经装好的版本不受影响；如需整体搬家，用「移动安装根目录…」）"
                    % os.path.normpath(INSTALL_BASE))
            except Exception:
                pass

    if locked:
        ttk.Button(df, text="移动安装根目录…", command=move_install_root_dialog
                   ).pack(anchor="w", pady=(8, 0))
        ttk.Label(df, text="版本装在：<安装根>\\dsh-versions\\<槽>；"
                           "临时文件在各槽的 .tmp 下",
                  foreground=_MUTED).pack(anchor="w", pady=(8, 0))
    else:
        ttk.Button(df, text="选择安装位置…", command=_choose
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
      界面     显示哪些槽 / 顺序 / 浏览器、主题
      目录     只有「安装根」（临时目录自动落在各槽 .tmp 下）
      维护     检查运行环境 / 槽体检 / 生成 bat / 使用说明
      日志     大小 + 清理（独立成组）
    """
    win = tk.Toplevel(root)
    _SETTINGS_WIN["win"] = win
    try:
        win.bind("<Destroy>",
                 lambda e: _SETTINGS_WIN.update({"win": None})
                 if e.widget is win else None, add="+")
    except Exception:
        pass
    win.title("设置")
    win.resizable(True, True)
    _apply_icon(win)
    _make_side_panel(win, width=380)

    host = _scroll_host(win)

    # ── 界面 ──────────────────────────────────────────────
    look = ttk.LabelFrame(host, text="界面", padding=(12, 10))
    look.pack(padx=16, pady=(14, 10), fill="x")

    ttk.Button(look, text="显示哪些槽 / 顺序 / 浏览器…",
               command=_settings_display_dialog).pack(fill="x", pady=(0, 8))

    lrow = ttk.Frame(look)
    lrow.pack(fill="x", pady=(2, 0))
    ttk.Label(lrow, text="主题", width=8).pack(side="left")
    _theme_var = tk.StringVar(value=_theme_mode())
    for _val, _lbl in (("system", "跟随系统"), ("light", "浅色"), ("dark", "深色")):
        ttk.Radiobutton(lrow, text=_lbl, value=_val, variable=_theme_var,
                        command=lambda v=_val: _apply_theme_mode(v)
                        ).pack(side="left", padx=(0, 10))

    # ── 目录（只剩一个安装根）──────────────────────────────
    try:
        _dir_group_single(host)
    except Exception as e:
        log("[设置] 目录分组构建失败：%s" % e)

    # ── 维护 ──────────────────────────────────────────────
    mnt = ttk.LabelFrame(host, text="维护", padding=(12, 10))
    mnt.pack(padx=16, fill="x", pady=(0, 10))
    for _c in range(2):
        mnt.columnconfigure(_c, weight=1)

    def _mbtn(r, c, text, cmd):
        b = ttk.Button(mnt, text=text, command=cmd)
        b.grid(row=r, column=c, padx=3, pady=3, sticky="ew")
        try:
            BUSY["btns"].append(b)
        except Exception:
            pass
        return b

    _mbtn(0, 0, "检查运行环境", _show_env_report)
    _mbtn(0, 1, "槽体检", lambda: _run_health(deep=True, dialog=True))
    _mbtn(1, 0, "生成启动/kill/CLI bat", gen_bats)
    _mbtn(1, 1, "使用说明", _open_user_guide)

    # ── 日志 ──────────────────────────────────────────────
    lg = ttk.LabelFrame(host, text="日志", padding=(12, 10))
    lg.pack(padx=16, fill="x", pady=(0, 14))
    _lsz, _llines = _log_stats()
    _linfo = ttk.Label(lg, text="%s（%d 行）" % (_fmt_size(_lsz), _llines),
                       foreground=_MUTED)
    _linfo.pack(anchor="w", pady=(0, 8))

    def _refresh_log_info():
        sz, ln = _log_stats()
        try:
            _linfo.configure(text="%s（%d 行）" % (_fmt_size(sz), ln))
        except Exception:
            pass

    _lrow2 = ttk.Frame(lg)
    _lrow2.pack(fill="x")
    ttk.Button(_lrow2, text="打开文件夹", command=_open_log_folder
               ).pack(side="left", expand=True, fill="x", padx=(0, 3))
    ttk.Button(_lrow2, text="清空…",
               command=lambda: (_clear_logs_dialog(win), _refresh_log_info())
               ).pack(side="left", expand=True, fill="x", padx=(3, 0))

    win.bind("<Escape>", lambda e: win.destroy())


def settings_dialog():
    """统一入口：主界面与各处「设置」都走这里 → 打开设置总页。

    2026-11-01：加**单例保护**。非模态之后反复点「设置」会叠出多个窗口，
    用户实测反馈"出现过几个设置页面"。已有活着的窗口就提到前面，不再新建。
    """
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
        log("[设置] 打开设置窗口失败：%s" % e)
        messagebox.showerror("设置", "打开设置窗口失败：%s" % e)

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
            "还没有可显示的槽",
            "当前没有任何版本槽，所以没有可勾选/排序的对象。\n\n"
            "先装一个版本（设置 → 安装官方版 dsh），之后这里就能调整\n"
            "显示哪几个槽、它们的左右顺序。")
        return

    order = _ui_order()                                   # 工作副本（含全部槽）
    _v = _ui_visible()
    shown = set(_v if _v is not None else order)
    maxv = _ui_max_visible()

    win = tk.Toplevel(root)
    win.title("设置")
    win.transient(root)
    win.resizable(False, False)
    _apply_icon(win)

    ttk.Label(win, text="设置",
              font=("Microsoft YaHei UI", 10, "bold")).pack(padx=20, pady=(16, 8),
                                                           anchor="w")

    box = ttk.LabelFrame(win, text="版本槽（勾选=显示 · 拖动=排序）",
                         padding=(8, 6))
    box.pack(padx=20, fill="both", expand=True)

    side = ttk.Frame(box)
    side.pack(side="right", fill="y", padx=(8, 0))

    lb = tk.Listbox(box, height=max(4, len(order)), width=44, exportselection=False,
                    font=("Consolas", 9), activestyle="dotbox")
    lb.pack(side="left", fill="both", expand=True)

    def _row_text(k):
        cfg = VC[k]
        ver = INSTALLED.get(k) or "未知"
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
                messagebox.showwarning("至少留一个",
                                       "至少要显示一个版本槽，否则界面就空了。")
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

    ttk.Button(side, text="上移 ↑", width=9,
               command=lambda: _move(-1)).pack(pady=2)
    ttk.Button(side, text="下移 ↓", width=9,
               command=lambda: _move(1)).pack(pady=2)
    ttk.Button(side, text="显示/隐藏", width=9,
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
    ttk.Label(opt, text="界面最多同时显示").pack(side="left")
    mv = tk.StringVar(value=str(maxv))
    tk.Spinbox(opt, from_=1, to=MAX_SLOTS, width=4, textvariable=mv,
               justify="center").pack(side="left", padx=6)
    ttk.Label(opt, text="个槽", foreground=_MUTED).pack(side="left")
    ttk.Label(win, text="上限 %d。勾选数超过它时只显示靠前的几个 —— "
                        "想只看某几个，把不要的取消勾选即可。" % MAX_SLOTS,
              foreground=_MUTED, wraplength=470, justify="left"
              ).pack(padx=20, pady=(0, 12), anchor="w")

    # ── 目录（2026-10-31）──────────────────────────────────────────────
    # 放在浏览器之前：装在哪、临时文件在哪，比「用哪个浏览器打开」重要得多。
    ib_var, tr_var = _dir_group(win)

    # ── 每个槽用哪个浏览器打开（2026-10-31）────────────────────────────
    # 为什么放在这里而不是卡片上：卡片是「动作」的地方（启动/停止/打开），
    # 偏好属于设置。卡片上「打开 X」按钮的文案会跟着变，一眼能看出当前用哪个。
    # 默认是「系统默认浏览器」—— 每个人装的浏览器不一样，替用户挑一个没道理。
    bf = ttk.LabelFrame(win, text="选中的槽用哪个浏览器打开", padding=(8, 6))
    bf.pack(padx=20, pady=(0, 10), fill="x")
    brow = ttk.Frame(bf)
    brow.pack(fill="x")
    ttk.Label(brow, text="当前槽：").pack(side="left")
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
                name += "（未安装）"
            _bmap[name] = b
            vals.append(name)
        low = (cur or "").strip().lower()
        if cur and low not in BROWSER_KNOWN:
            d = "其他程序：%s" % os.path.basename(cur)
            _bmap[d] = cur
            vals.append(d)
        vals.append(_B_OTHER)
        return vals

    def _b_display(v):
        v = (v or "default").strip()
        low = v.lower()
        if low in BROWSER_KNOWN:
            name = BROWSER_KNOWN[low][0]
            if not _browser_installed(low):
                name += "（未安装）"
            return name
        return "其他程序：%s" % os.path.basename(v)

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
        if sel == _B_OTHER:
            p = filedialog.askopenfilename(
                title="选择浏览器程序", parent=win,
                filetypes=[("可执行程序", "*.exe"), ("所有文件", "*.*")])
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
    ttk.Label(bf, text="「系统默认浏览器」= 你平时双击链接时用的那个。"
                       "改完点「保存」生效；卡片上的按钮会变成「打开 Chrome」这种。",
              foreground=_MUTED, wraplength=460, justify="left"
              ).pack(anchor="w", pady=(6, 0))
    _b_sync()

    def on_ok():
        # 目录先落盘：它动的是磁盘真相，失败就不该继续保存其它偏好
        ok, dmsg = _apply_dir_settings(ib_var, tr_var)
        if not ok:
            messagebox.showerror("目录设置失败", dmsg)
            return
        try:
            n = int(mv.get())
        except (TypeError, ValueError):
            n = maxv
        n = max(1, min(MAX_SLOTS, n))
        vis = [k for k in order if k in shown]
        if not vis:
            messagebox.showwarning("至少留一个", "请至少勾选一个要显示的版本槽。")
            return
        raw = {"max_visible": n, "order": list(order), "visible": vis}
        # 收口：丢未知槽、钳上限、保证至少一个可见（逻辑在 dsh_ui_prefs，可离线测）
        prefs = (UIP.normalize(raw, VERSIONS, SLOT_KEYS, MAX_SLOTS)
                 if UIP else raw)
        err = _save_ui_prefs(prefs)
        if err:
            messagebox.showerror("保存失败", err)
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
                messagebox.showerror("保存浏览器设置失败", err)
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
        log("[设置] 界面显示：%s（最多 %d 个）" % ("、".join(vis), n))
        if bchanged:
            log("[设置] 浏览器：%s" % "、".join(bchanged))
        if len(vis) > n:
            messagebox.showinfo("已保存",
                                "已保存。勾选了 %d 个，但「最多显示 %d 个」限制下"
                                "只显示前 %d 个：\n\n%s"
                                % (len(vis), n, n, "、".join(vis[:n])))
        # 目录改动的说明放最后弹：它往往要求用户「重启」或「手工删旧目录」，
        # 盖在别的提示上面容易被忽略。
        if dmsg:
            messagebox.showinfo("目录已更新", dmsg)

    btns = ttk.Frame(win)
    btns.pack(padx=20, pady=(0, 16))
    ttk.Button(btns, text="保存", style="Green.TButton",
               command=on_ok).pack(side="left", padx=4)
    ttk.Button(btns, text="取消", command=win.destroy).pack(side="left", padx=4)
    win.bind("<Escape>", lambda e: win.destroy())
    win.grab_set()


# 标题栏「设置」按钮：**必须**在 settings_dialog 定义之后创建 —— 否则模块执行到
# 那一行时函数还不存在 → NameError，而 .pyw 无控制台，表现为「双击没反应」。
# （这个坑本项目踩过两次，C3/C8 组断言守着。）
# 2026-11-01：标题栏只留「设置」一个入口。
# 「关于」「★ 在 GitHub 收藏」都收进设置页（用户要求：初始界面只有槽 + 日志 + 全启停）。
ttk.Button(_hdr, text="设置", command=settings_dialog).pack(side="right", padx=(0, 6))
# 「★ 在 GitHub 收藏」：2026-11-02 从设置里移到主界面（用户要求）
ttk.Button(_hdr, text="★ 收藏", command=_open_project_page).pack(
    side="right", padx=(0, 6))
# 两个全启停按钮：放在「设置」左侧（pack side=right 时后 pack 的在更左边）
_b_stopall = ttk.Button(_hdr, text="全部停止", style="Red.TButton",
                        command=stop_all_slots)
_b_stopall.pack(side="right", padx=(0, 6))
BUSY["btns"].append(_b_stopall)
_b_startall = ttk.Button(_hdr, text="全部启动", style="Green.TButton",
                         command=start_all_slots)
_b_startall.pack(side="right", padx=(0, 6))
BUSY["btns"].append(_b_startall)
# 「槽体检」按钮已挪到下面「安装/纳入/体检/卸载」那一排（2026-10-31 起）——
# 那四个都是「对整个槽做的事」，摆在一起才符合心智。标题栏只留「设置」。

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
        _missing = [k for k in SLOT_KEYS if k not in set(_display_keys())]
        if _missing:
            _err = _ensure_slots_visible(_missing)
            if not _err:
                log("[配置] 已补全界面显示列表：%s（此前不在 ui.visible 里）"
                    % "、".join(_missing))
            else:
                log("[配置] 补全界面显示列表失败：%s" % _err)
    except Exception as _e:
        log("[配置] 启动自愈 ui.visible 时出错（忽略）：%s" % _e)

if FRESH_START:
    log("[引导] 零槽启动：还没有任何版本槽，显示安装引导")
    # 零槽时自动把环境体检结果摆出来，用户装之前就知道缺什么
    root.after(600, lambda: _log_fresh_env())

def _log_fresh_env():
    """零槽启动时把环境体检摘要写进日志（界面不打扰，想看滚日志即可）。"""
    try:
        import subprocess as _sp
        tmp = os.path.join(BASE_DIR, "temp", "env-check.out")
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        with open(tmp, "wb") as out:
            rc = _sp.run([sys.executable,
                          os.path.join(BASE_DIR, "scripts", "check-env.py")],
                         stdout=out, stderr=_sp.STDOUT,
                         creationflags=CREATE_NO_WINDOW).returncode
        raw = open(tmp, "rb").read().decode("utf-8", "replace")
        for ln in raw.splitlines():
            if ln.strip().startswith(("[OK]", "[注意]", "[缺失]")):
                log("[环境] " + ln.strip())
        log("[环境] 完整报告见 dsh-manager.log；要自己再跑一遍："
            "python scripts\\check-env.py（换新机器 / 排查管理器起不来时用）")
    except Exception as e:
        log("[环境] 自动体检失败：%s" % e)

# ── 主题（2026-11-01）───────────────────────────────────────────────
# 必须放在文件末尾：_apply_palette() 定义在下方，而这里要**调用**它；
# 同时它还要触碰 log_text / cards_frame，那些控件也得先建好。
# （2026-11-01 实测踩到：放在控件创建处调用 → name '_apply_palette' is not defined）
try:
    _apply_palette()
    log("[主题] 生效：%s（模式=%s）" % (_resolved_theme(), _theme_mode()))
except Exception as _e:
    log("[主题] 应用失败（回退默认配色）：%s" % _e)

root.mainloop()
