# -*- coding: utf-8 -*-
"""离线回归：**被没有 tkinter 的 Python 打开时，自动换包内解释器重开**。

背景（2026-10-31 实测踩到）
--------------------------
这台机器上至少有三套 python：包内 `runtime\\python`（3.10.21，tkinter 8.6 正常）、
商店版 `WindowsApps\\python.exe`、以及别的工具链自带的 3.13（**没有 tkinter**）。
只要 `dsh_manager.pyw` 被后两者打开（双击 .pyw / 命令行 `python xxx.pyw` /
自动化脚本 exec 它），就会弹：

    dsh 版本管理器需要 Python 的 tkinter 模块，但它不可用。
    原因：No module named 'tkinter'
    解决办法：重新安装 python.org 官方版 Python（勾选 tcl/tk 组件）…

而**包内明明有能用的解释器** —— 这句提示会把人引去卸载重装 Python。
现在改成：先尝试用 `runtime\\python\\pythonw.exe` 重开自己，实在没有再弹框。

本脚本把 tkinter「弄坏」再 exec 一遍源码，断言三条分支：
  A 包内解释器在、且没重试过  → 拉起新进程（参数/环境变量正确），自己 exit 0，不弹框
  B 已经重试过（防死循环）    → 不拉起，弹框，exit 1
  C 包内解释器不存在          → 不拉起，弹框，exit 1

跑法：python scripts/test-tk-selfheal.py    （退出码 0 = 全过，1 = 有失败）
"""
import io
import os
import sys
import tempfile
import types
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, os.pardir))
SRC = os.path.join(REPO, "dsh_manager.pyw")
# exec 整个 .pyw 时，它自己的 `from dsh_i18n import T` 要能找到模块
if REPO not in sys.path:
    sys.path.insert(0, REPO)

if not os.path.exists(SRC):
    print("找不到 %s —— 请在管理器仓库内运行本脚本" % SRC)
    sys.exit(1)

FAILS = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ((" | " + extra) if extra else ""))
    if not cond:
        FAILS.append(name)


def skip(name, why=""):
    print("SKIP  " + name + ((" | " + why) if why else ""))


src = io.open(SRC, encoding="utf-8").read()
BUNDLED = os.path.join(REPO, "runtime", "python", "pythonw.exe")
have_bundled = os.path.isfile(BUNDLED)


def run_case(file_for_dirname, env_flag, label):
    """在「tkinter 不可用」的环境里 exec 一遍管理器源码，收集它做了什么。"""
    popen_calls = []
    box_calls = []

    fake_subprocess = types.ModuleType("subprocess")

    def fake_popen(args, **kw):
        popen_calls.append((list(args), dict(kw)))
        return None

    fake_subprocess.Popen = fake_popen

    fake_ctypes = types.ModuleType("ctypes")

    class _User32(object):
        @staticmethod
        def MessageBoxW(*a):
            box_calls.append(a)
            return 1

    class _Windll(object):
        user32 = _User32()

    fake_ctypes.windll = _Windll()

    saved = {}
    for name, mod in (("subprocess", fake_subprocess), ("ctypes", fake_ctypes),
                      ("tkinter", None)):
        saved[name] = sys.modules.get(name, "<absent>")
        sys.modules[name] = mod
    old_flag = os.environ.get("DSH_MANAGER_TK_RELAUNCH")
    if env_flag is None:
        os.environ.pop("DSH_MANAGER_TK_RELAUNCH", None)
    else:
        os.environ["DSH_MANAGER_TK_RELAUNCH"] = env_flag
    code = None
    try:
        g = {"__name__": "__selfheal_probe__", "__file__": file_for_dirname}
        try:
            exec(compile(src, "dsh_manager.pyw", "exec"), g)
        except SystemExit as e:
            code = e.code
    finally:
        for name, val in saved.items():
            if val == "<absent>":
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = val
        if old_flag is None:
            os.environ.pop("DSH_MANAGER_TK_RELAUNCH", None)
        else:
            os.environ["DSH_MANAGER_TK_RELAUNCH"] = old_flag
    return popen_calls, box_calls, code


print("== A. 包内解释器在 → 自动重开 ==")
if not have_bundled:
    skip("A1..A4（本机没有 runtime\\python\\pythonw.exe，无法验证自愈分支）")
else:
    calls, boxes, code = run_case(SRC, None, "A")
    chk("A1 拉起了新进程（不是直接弹框）", len(calls) == 1, str(len(calls)))
    if calls:
        argv, kw = calls[0]
        chk("A2 用的是包内解释器", argv[0] == BUNDLED, argv[0])
        chk("A3 参数是管理器自己的绝对路径",
            len(argv) > 1 and os.path.normcase(argv[1])
            == os.path.normcase(os.path.abspath(SRC)), str(argv[1:]))
        chk("A4 带上防死循环的环境变量",
            (kw.get("env") or {}).get("DSH_MANAGER_TK_RELAUNCH") == "1",
            str((kw.get("env") or {}).get("DSH_MANAGER_TK_RELAUNCH")))
    chk("A5 重开成功后自己退出（exit 0），不再弹框",
        code == 0 and not boxes, "code=%s boxes=%d" % (code, len(boxes)))

print("\n== B. 已经重试过 → 不再重开，弹框说明 ==")
calls, boxes, code = run_case(SRC, "1", "B")
chk("B1 不再拉起进程（防死循环）", not calls, str(len(calls)))
chk("B2 弹出原生错误框（tkinter 不可用时只能用 MessageBoxW）", len(boxes) == 1,
    str(len(boxes)))
if boxes:
    msg = boxes[0][1] if len(boxes[0]) > 1 else ""
    chk("B3 提示里第一条是「用桌面快捷方式 / start-manager.bat」而不是重装 Python",
        "start-manager.bat" in msg and msg.index("start-manager.bat")
        < msg.index("重新安装 python.org"),
        " / ".join(msg.splitlines()[4:6])[:140])
    chk("B4 仍保留「重装 Python 勾选 tcl/tk」作为兜底",
        "tcl/tk" in msg)
chk("B5 退出码 1", code == 1, str(code))

print("\n== C. 包内解释器不存在 → 不重开，弹框 ==")
TMP = tempfile.mkdtemp(prefix="dsh-noRuntime-")
try:
    ghost = os.path.join(TMP, "dsh_manager.pyw")
    calls, boxes, code = run_case(ghost, None, "C")
    chk("C1 没有可用解释器时不乱拉进程", not calls, str(len(calls)))
    chk("C2 弹框告知", len(boxes) == 1, str(len(boxes)))
    chk("C3 退出码 1", code == 1, str(code))
finally:
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)

if sys.platform == "win32":
    import dsh_platform as platform

    with tempfile.TemporaryDirectory(prefix="dsh-config-portable-") as folder:
        template = os.path.join(folder, "template.json")
        config = os.path.join(folder, "config.json")
        with open(template, "wb") as handle:
            handle.write(b"original")
        with patch.object(os, "link", side_effect=OSError("hard links unsupported")):
            platform.bootstrap_config(config, template)
            with open(template, "wb") as handle:
                handle.write(b"replacement")
            platform.bootstrap_config(config, template)
        with open(config, "rb") as handle:
            chk("D1 Windows 无硬链接也能引导且保留已有配置",
                handle.read() == b"original")


print()
if FAILS:
    print("RESULT: FAIL（%d 项）: %s" % (len(FAILS), "; ".join(FAILS)))
    sys.exit(1)
print("RESULT: ALL PASS")
