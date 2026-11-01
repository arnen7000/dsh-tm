# build_lnk.py — 生成 Windows .lnk 快捷方式，指向 dsh_manager.pyw
#
# 【为什么改成 COM 优先】
# 之前是手工构造 MS-SHLLINK 二进制（因为这台机器的 PowerShell 里 WScript.Shell COM
# 被安全策略禁掉）。但手工构造出来的 .lnk 有个致命问题：
#   回读校验用的是「自己写的解析器」，只能证明读写自洽，证明不了 Explorer 认账
#   —— 属于循环验证。实测结果就是：字段回读全对，但双击毫无反应。
# 后来发现在 Python310 里 pywin32（win32com）是可用的，于是改为：
#   ① 优先用 Windows 原生 WScript.Shell COM 生成（2061 字节，与资源管理器手工新建的
#      快捷方式完全一致，已用 os.startfile() 走 ShellExecute 实测可启动）；
#   ② 没有 pywin32 时才退回手工构造，且此时目标直接指向 .pyw（不依赖 ARGUMENTS 传递，
#      因为手工 .lnk 的 ARGUMENTS 在部分环境不被 Explorer 传递，会导致 pythonw 无脚本
#      可跑、又无控制台 → 进程空转挂死，表现为「双击没反应」）。
import base64
import os
import shutil
import struct
import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000   # 隐藏探测子进程的控制台窗口
BASE = os.path.dirname(os.path.abspath(__file__))
MANAGER = os.path.join(BASE, "dsh_manager.pyw")
ICON_PATH = os.path.join(BASE, "assets", "dsh-icon-violet.ico")


def desktop_dir():
    """真实的「桌面」目录（跨机稳定版）。

    不能直接用 ~/Desktop：开了 OneDrive「已知文件夹移动 / 桌面备份」的机器上，
    桌面会被重定向到 <OneDrive>\\Desktop，写过去等于快捷方式落在错误位置（甚至目录不存在）。

    三级取值：
      1) SHGetKnownFolderPath(FOLDERID_Desktop) —— 权威 API，自动跟随任何重定向；
      2) 注册表 HKCU\\...\\User Shell Folders\\Desktop（需 ExpandEnvironmentStrings）；
      3) ~/Desktop、~/OneDrive/Desktop 兜底。
    """
    # ① Win32 API
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", wintypes.BYTE * 8)]

        # FOLDERID_Desktop = {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
        fid = GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                   (wintypes.BYTE * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))
        p = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(fid), 0, None, ctypes.byref(p)) == 0 and p.value:
            val = p.value
            ctypes.windll.ole32.CoTaskMemFree(p)
            if os.path.isdir(val):
                return val
    except Exception:
        pass

    # ② 注册表
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            val, _ = winreg.QueryValueEx(k, "Desktop")
        val = os.path.expandvars(val)
        if val and os.path.isdir(val):
            return val
    except Exception:
        pass

    # ③ 兜底
    home = os.path.expanduser("~")
    for c in (os.path.join(home, "Desktop"),
              os.path.join(home, "OneDrive", "Desktop")):
        if os.path.isdir(c):
            return c
    return os.path.join(home, "Desktop")


DESKTOP = desktop_dir()
# 允许用环境变量改输出路径：离线包自测时不想覆盖用户桌面上已有的图标
OUT = os.environ.get("DSH_LNK_OUT") or os.path.join(DESKTOP, "dsh-manager.lnk")


def _has_tkinter(candidate):
    try:
        r = subprocess.run([candidate, "-c", "import tkinter"],
                           capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def _iter_python_candidates(exe_name):
    """枚举候选解释器（跨机：不能只认 Python310/311）。

    不同机器装的 Python 版本/位置千差万别（3.8~3.14、用户级/D 盘/Program Files），
    所以改成「目录通配 + PATH」枚举，并**排除 WindowsApps**（微软商店的 App Execution Alias
    是个 0 字节重定向壳，用它当 .lnk 目标会静默失败）。
    """
    import glob
    home = os.path.expanduser("~")
    roots = [
        os.path.join(home, "AppData", "Local", "Programs", "Python"),
        r"C:\Program Files",
        r"C:\Program Files (x86)",
    ]
    seen, out = set(), []

    def add(c):
        if c and c not in seen and os.path.exists(c) and "WindowsApps" not in c:
            seen.add(c)
            out.append(c)

    for root in roots:
        for hit in sorted(glob.glob(os.path.join(root, "Python3*", exe_name))):
            add(hit)
        add(os.path.join(root, exe_name))
        add(os.path.join(root, "Python", exe_name))
    for nm in (exe_name, exe_name[:-4] if exe_name.endswith(".exe") else exe_name):
        add(shutil.which(nm))
    return out


def find_pythonw():
    r"""选一个「带 tkinter」的 pythonw（管理器是 GUI，没 tkinter 起不来）。

    取值顺序（包内解释器优先，这是刻意的）：
      1) 包内自带 `runtime\python\pythonw.exe`（便携包 / 本机固有布局）——
         **必须排在第一位**：用小节解释见下；
      2) 环境变量 `DSH_PYTHONW`（想强制指定解释器时用）；
      3) `_iter_python_candidates` 扫系统常见安装位置（Program Files 等，已排除
         WindowsApps）；
      4) 兜底：裸 `pythonw.exe`（交给运行时 PATH 解析 —— 最弱的一档）。

    为什么自带解释器要排第一
    ----------------------
    原实现把系统扫描放在环境变量之前，扫不到就让 target 退回裸 `pythonw.exe`。
    但 .lnk 的 TargetPath 是裸文件名时，Explorer 解析走的是**当前用户的 PATH**:
    本机没有 python.org 版 Python（只有 WindowsApps 商店版），PATH 解析于是命中
    商店 App Execution Alias —— 每次双击都要绕一圈：先拉起商店版 Python、被管理器
    检测到「会被劫持任务栏图标」、再用独立解释器重启一次（约 2 秒 + 窗口先闪一下，
    见 `dsh_manager.pyw` 的商店版守卫）。
    指向 `runtime\python\pythonw.exe` 的绝对路径即可一次到位，且离线包换盘/换机器
    都不会断链（`BASE` 由本脚本位置推出，天然跟着 .lnk 所在项目走）。
    """
    # ① 包内自带解释器（绝对路径，最高优先）
    bundled = os.path.join(BASE, "runtime", "python", "pythonw.exe")
    if os.path.exists(bundled) and _has_tkinter(bundled):
        return bundled
    # ② 离线安装脚本指定的解释器
    env = os.environ.get("DSH_PYTHONW")
    if env and os.path.exists(env) and _has_tkinter(env):
        return env
    # ③ 系统常见位置
    cands = _iter_python_candidates("pythonw.exe")
    for c in cands:
        if _has_tkinter(c):
            return c
    if cands:
        return cands[0]
    return "pythonw.exe"   # ④ 退回 PATH 查找


def _probe_pywin32(exe):
    """该解释器能否 import win32com（pywin32）？"""
    try:
        r = subprocess.run([exe, "-c", "import win32com.client, pythoncom"],
                           capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def find_pywin32_python():
    """当前解释器没有 pywin32 时，另找一个有的（用于委托重跑本脚本）。

    背景：本机 `py` 默认指向微软商店 Python 3.14，没有 pywin32；
    而 Python310 装了。若直接静默降级成手工构造 .lnk，Explorer 双击打不开。
    """
    home = os.path.expanduser("~")
    cands = _iter_python_candidates("python.exe")
    cands += [
        os.path.join(home, "AppData", "Local", "Programs", "Python", "Python310", "python.exe"),
        os.path.join(home, "AppData", "Local", "Programs", "Python", "Python311", "python.exe"),
    ]
    for nm in ("python", "python3"):
        p = shutil.which(nm)
        if p:
            cands.append(p)
    seen = set()
    for exe in cands:
        if not exe or exe in seen or not os.path.exists(exe) or "WindowsApps" in exe:
            continue
        seen.add(exe)
        if _probe_pywin32(exe):
            return exe
    return None


def build_via_powershell(target, arguments, working_dir, icon_path, out_path):
    """第二道兜底：子进程调 PowerShell 的 WScript.Shell COM。
    （某些受管的沙箱环境会拦 PowerShell 的 New-Object，但用户自己双击运行不受限。）
    用 -EncodedCommand(UTF-16LE base64) 传脚本，规避中文/引号编码问题。"""
    ps = (shutil.which("powershell") or shutil.which("pwsh")
          or r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    if not ps or not os.path.exists(ps):
        return False
    icon_line = ""
    if icon_path and os.path.exists(icon_path):
        icon_line = '$sc.IconLocation = "%s,0"\n' % icon_path
    script = (
        '$ErrorActionPreference = "Stop"\n'
        '$ws = New-Object -ComObject WScript.Shell\n'
        '$sc = $ws.CreateShortcut("%s")\n'
        '$sc.TargetPath = "%s"\n'
        '$sc.Arguments = \'%s\'\n'
        '$sc.WorkingDirectory = "%s"\n'
        '$sc.Description = "DSH Manager"\n'
        '%s'
        '$sc.Save()\n'
    ) % (out_path, target, arguments, working_dir, icon_line)
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive",
                            "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
                           capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


# ── 方案①：Windows 原生 COM（推荐） ────────────────────────────────────────
def build_via_com(target, arguments, working_dir, icon_path, out_path):
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    try:
        ws = win32com.client.Dispatch("WScript.Shell")
        sc = ws.CreateShortcut(out_path)
        sc.TargetPath = target
        sc.Arguments = arguments
        sc.WorkingDirectory = working_dir
        sc.Description = "DSH 双版本管理器"
        if icon_path and os.path.exists(icon_path):
            sc.IconLocation = "%s,0" % icon_path
        sc.Save()
        return True
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


# ── 方案②：手工构造 MS-SHLLINK（无 pywin32 时的降级） ──────────────────────
def ustring(s):
    """StringData 项体：uint16 字符数 + UTF-16LE 字符串（无 null 结尾，无 ID 字段）。"""
    return struct.pack("<H", len(s)) + s.encode("utf-16-le")


def build_manual(target, arguments, working_dir, icon_path, out_path):
    clsid = b"\x01\x14\x02\x00\x00\x00\x00\x00\xC0\x00\x00\x00\x00\x00\x00\x46"
    try:
        ansi_path = target.encode("ascii") + b"\x00"
    except Exception:
        ansi_path = target.encode("mbcs", "replace") + b"\x00"
    uni_path = target.encode("utf-16-le") + b"\x00\x00"

    header_size = 0x24
    local_base_off = header_size
    uni_off = header_size + len(ansi_path)
    linkinfo = struct.pack("<I", header_size + len(ansi_path) + len(uni_path))
    linkinfo += struct.pack("<I", header_size)
    linkinfo += struct.pack("<I", 0x01)
    linkinfo += struct.pack("<I", 0)
    linkinfo += struct.pack("<I", local_base_off)
    linkinfo += struct.pack("<I", 0)
    linkinfo += struct.pack("<I", 0)
    linkinfo += struct.pack("<I", uni_off)
    linkinfo += struct.pack("<I", 0)
    linkinfo += ansi_path
    linkinfo += uni_path

    s_work = ustring(working_dir)
    s_args = ustring(arguments) if arguments else b""
    has_icon = bool(icon_path and os.path.exists(icon_path))
    s_icon = ustring(icon_path) if has_icon else b""

    flags = 0x80 | 0x02 | 0x10
    if s_args:
        flags |= 0x20
    if has_icon:
        flags |= 0x40

    header = b""
    header += struct.pack("<I", 0x4C)
    header += clsid
    header += struct.pack("<I", flags)
    header += struct.pack("<I", 0x20)
    header += b"\x00" * 8 * 3
    header += struct.pack("<I", 0)
    header += struct.pack("<I", 0)
    header += struct.pack("<I", 1)
    header += struct.pack("<H", 0)
    header += struct.pack("<H", 0)
    header += struct.pack("<I", 0)
    header += struct.pack("<I", 0)
    assert len(header) == 76, "header len %d" % len(header)

    with open(out_path, "wb") as f:
        f.write(header + linkinfo + s_work + s_args + s_icon)
    return True


if __name__ == "__main__":
    icon = ICON_PATH if os.path.exists(ICON_PATH) else None
    args = '"%s"' % MANAGER

    # ── 0) 当前解释器有没有 pywin32？没有就委托给一个有的解释器重跑 ──
    have_com = True
    try:
        import pythoncom  # noqa: F401
        import win32com.client  # noqa: F401
    except Exception:
        have_com = False

    if not have_com:
        helper = find_pywin32_python()
        if helper and os.path.abspath(helper) != os.path.abspath(sys.executable):
            sys.stderr.write(
                "[info] 当前解释器 %s 没有 pywin32，委托给 %s 重新生成（原生 COM）\n"
                % (sys.executable, helper))
            rc = subprocess.run([helper, os.path.abspath(__file__)]).returncode
            sys.exit(rc)

    target = find_pythonw()
    if have_com:
        build_via_com(target, args, BASE, icon, OUT)
        mode = "COM(原生)"
    elif build_via_powershell(target, args, BASE, icon, OUT):
        mode = "PowerShell COM"
    else:
        # 最后兜底：手工构造。实测这种 .lnk 在 Explorer 双击打不开，必须大声警告。
        build_manual(MANAGER, "", BASE, icon, OUT)
        mode = "手工(降级)"
        sys.stderr.write(
            "\n[严重警告] 没找到任何可用方式生成「原生」快捷方式，"
            "已退回手工构造 —— 该文件 Explorer 双击很可能打不开！\n"
            "  解决：给任一 Python 装上 pywin32 后重跑，例如：\n"
            "        py -3.10 -m pip install pywin32\n"
            "        py -3.10 build_lnk.py\n")

    # 目标指向**绝对路径**才算一次到位；裸 pythonw.exe 会走 PATH 解析，
    # 在 WindowsApps 商店版 Python 的机器上会绕一圈（见 find_pythonw 注释）。
    target_kind = ("绝对路径" if os.path.isabs(target) and os.path.exists(target)
                   else "裸名(PATH解析)⚠")
    size = os.path.getsize(OUT)
    print("OK %s (%d bytes)  mode=%s  target=%s [%s]\n   args=%s\n   icon=%s" % (
        OUT, size, mode, target, target_kind, args,
        "yes" if icon else "MISSING"))
    if target_kind.startswith("裸名"):
        sys.stderr.write(
            "[警告] 快捷方式 target 是裸名 %s，Explorer 会按 PATH 解析；\n"
            "  若机器只有 WindowsApps 商店版 Python，双击会先拉起商店版再被重启。\n"
            "  建议确认包内 runtime\\python\\pythonw.exe 存在，或设置环境变量 DSH_PYTHONW。\n"
            % target)
    if mode == "手工(降级)" or size < 800:
        sys.stderr.write(
            "[警告] 生成的 .lnk 仅 %d 字节（原生快捷方式通常 >1.5KB），"
            "请按上面的提示装 pywin32 后重跑。\n" % size)
