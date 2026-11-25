"""Run the native progress-window and dispatcher regressions with controlled install results."""
import ast
from pathlib import Path
from types import SimpleNamespace
import queue
import sys
import threading
import time

if sys.platform != "darwin":
    print("SKIP: native macOS window regression")
    raise SystemExit(0)

import tkinter as tk
from tkinter import ttk

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import dsh_install_ops as ops

names = {"_make_taskbar_window", "_GuiProgress", "_install_queue", "_do_install",
         "_actions_enabled", "_request_close", "_build_fresh_guide", "_post_ui", "_drain_ui_queue",
         "install_official_version", "scan_and_offer_adopt"}
parsed = ast.parse((REPO / "dsh_manager.pyw").read_text(encoding="utf-8"))
root = tk.Tk()
root.geometry("720x420")
header = ttk.Frame(root)
header.pack(fill="x")
cards = ttk.Frame(root)
cards.pack(fill="x")
stoppable = ttk.Button(root, text="Start existing")
browser = ttk.Button(root, text="Open existing")
settings_button = ttk.Button(root, text="Settings")
for button in (stoppable, browser, settings_button):
    button.pack()
busy = {"installing": False, "install_background": False, "btns": [settings_button],
        "card": [stoppable, browser], "card_run": [stoppable, browser]}
events, pending, logs = [], queue.SimpleQueue(), []
jobs = [threading.Event(), threading.Event()]
keys = iter(["v2", "v3"])
attempts = []


def installer(_adopt, ver, config, base, key, ports, *, progress, **kwargs):
    n = len(attempts)
    attempts.append((ver, key))
    progress.on_step("Download")
    assert jobs[n].wait(timeout=15)
    if n == 0:
        progress.on_done(True, {"slot_key": key, "version": ver})
        return True, key, "version installed"
    return False, "", "npm installation timed out"


def notify(title, body, parent):
    assert parent is root
    events.append(("notice", title, body))


env = {
    "root": root, "_hdr": header, "tk": tk, "ttk": ttk,
    "PLATFORM": SimpleNamespace(IS_WINDOWS=False), "T": lambda s: s, "log": logs.append,
    "OPS": SimpleNamespace(Progress=ops.Progress, run_official_install=installer,
                           run_in_thread=ops.run_in_thread),
    "queue": queue, "_UI_QUEUE": pending, "_apply_icon": lambda win: None,
    "_APP_BG": "white", "_CARD_BD": "gray", "_INK": "black", "_MUTED": "gray", "_DIVIDER": "gray",
    "BUSY": busy, "_SETTINGS_WIN": {"win": None}, "_occupied_unmanaged_keys": lambda: [],
    "_next_free_slot_key_live": lambda: next(keys), "dsh_adopt": None,
    "CONFIG_PATH": "", "INSTALL_BASE": "", "MAX_SLOTS": 6, "_taken_ports": lambda: [],
    "_versions_root": lambda: "", "_temp_root": lambda: "",
    "_restart_or_tell": lambda what, keys: events.append(("refresh", keys)),
    "messagebox": SimpleNamespace(showwarning=notify, showinfo=notify, showerror=notify),
    "on_close": lambda: events.append(("close",)),
    "cards_frame": cards, "SLOT_KEYS": [], "_clear_cards": lambda: None,
}
definitions = [node for node in parsed.body
               if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
exec(compile(ast.Module(body=definitions, type_ignores=[]), "manager-install", "exec"), env)
progress_class = env["_GuiProgress"]
progresses = []


def progress(*args, **kwargs):
    value = progress_class(*args, **kwargs)
    progresses.append(value)
    return value


env["_GuiProgress"] = progress


def pump(predicate=lambda: True):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return
        time.sleep(.015)
    raise AssertionError((events, attempts, logs))


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


try:
    root.after(0, env["_drain_ui_queue"])
    env["_build_fresh_guide"]()
    empty_slot_buttons = [widget for widget in descendants(cards) if isinstance(widget, ttk.Button)]
    assert len(empty_slot_buttons) == 2
    assert all(not button.instate(["disabled"]) for button in empty_slot_buttons)
    env["_install_queue"](["0.2.2-rc.5", "0.2.2-rc.4"])
    pump(lambda: len(attempts) == 1 and progresses[0].lbl.cget("text") == "Download")
    first = progresses[0]
    assert first.win.winfo_ismapped() and first.win.transient() == "."
    assert not first.win.overrideredirect() and root.grab_current() is None
    assert not stoppable.instate(["disabled"]) and not browser.instate(["disabled"])
    assert settings_button.instate(["disabled"])
    assert all(button.instate(["disabled"]) for button in empty_slot_buttons)
    env["install_official_version"]()
    env["scan_and_offer_adopt"]()
    env["_install_queue"](["0.2.2-rc.3"])
    assert len(progresses) == 1 and len(attempts) == 1
    assert not events
    env["_request_close"]()
    assert len(events) == 1 and events.pop()[:2] == ("notice", "正在安装 dsh")
    hide = next(widget for widget in descendants(first.win)
                if isinstance(widget, ttk.Button) and widget.cget("text") == "在后台继续")
    hide.invoke()
    pump()
    assert not first.win.winfo_ismapped()
    first.progress_button.invoke()
    pump()
    assert first.win.winfo_ismapped()
    assert not any("WinDLL" in log for log in logs)
    print("PASS native first show, background, reopen, and action restrictions", flush=True)
    first.win.tk.call(first.win.protocol("WM_DELETE_WINDOW"))
    jobs[0].set()
    pump(lambda: len(attempts) == 2)
    assert not progresses[1].win.winfo_ismapped()
    assert first.finished and not first.win.winfo_exists()
    assert not events
    jobs[1].set()
    pump(lambda: len(events) == 2)
    assert events[0] == ("refresh", ["v2"]), events
    assert "成功 1 / 失败 1" in events[1][1] and "timed out" in events[1][2], events
    assert not busy["installing"] and not settings_button.instate(["disabled"])
    assert all(not button.instate(["disabled"]) for button in empty_slot_buttons)
    assert len(attempts) == 2 and not header.winfo_children()
    assert not any("后台操作的结果处理失败" in line for line in logs), logs
    assert sum("[安装] 安装结束" in line for line in logs) == 1, logs
    env["_request_close"]()
    assert events[-1] == ("close",)
    print("PASS two original work items, one post-refresh notice and one log summary", flush=True)
finally:
    for job in jobs:
        job.set()
    root.destroy()
