# -*- coding: utf-8 -*-
"""离线回归：验证 dsh_manager.pyw 的【第①层】日志脱敏（log() 出口打码）仍然有效。

为什么要有这个脚本
------------------
README「日志脱敏」与 docs/架构与维护说明.md §5.3 都承诺了**三层**脱敏，其中第①层是
「log() 出口统一打码」。但这一层在 a34e20b（P1 修复）里**只建了正则 `_TOKEN_TEXT_RE`、
忘了接线** —— 该正则在当时及之后的版本里零引用（`git grep -n "_TOKEN_TEXT_RE" HEAD` 只有定义行）。
后果：管理器自己 `log()` 写出的任何含 token 文本都会明文落盘（实测 631 行的
`打开浏览器 -> http://...?token=`），且因为第③层 `_scrub_existing_log()` 会在**下次启动**
把历史日志全量补擦，症状被掩盖成"偶发、只有最近几行是明文"，很难被发现。
2026-09-21 补上第①层（`_mask_text()` + log() 出口调用）并建立本脚本。

它挡的是什么
------------
任何人把 `log()` 里的 `_mask_text(...)` 调用删掉/改坏，本脚本立刻失败。
注意与 check-all.ps1 的 4/6 分工：4/6 查的是**磁盘现状**（已经写进日志的明文），
本脚本查的是**代码逻辑**（log() 出口还在不在打码）—— 只有本脚本能在"还没写出日志"时就发现问题。

怎么做到无副作用
----------------
用 AST 从 dsh_manager.pyw 里**只**抽取 `_TOKEN_TEXT_RE` / `_TOKEN_BYTES_RE` / `_mask_text` / `log`
四个定义到独立命名空间执行，**不执行**模块顶层其余代码 —— 因此不建 GUI、不写真实日志文件。

跑法：python scripts/test-log-mask.py      （退出码 0 = 全过，1 = 有失败）
"""
import ast
import io
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, os.pardir, "dsh_manager.pyw"))

if not os.path.exists(SRC):
    print("找不到 %s —— 请在管理器仓库内运行本脚本" % SRC)
    sys.exit(1)

src = open(SRC, encoding="utf-8").read()
tree = ast.parse(src)

keep = []
for n in tree.body:
    if isinstance(n, ast.Assign):
        names = [getattr(t, "id", None) for t in n.targets]
        if "_TOKEN_TEXT_RE" in names or "_TOKEN_BYTES_RE" in names:
            keep.append(n)
    elif isinstance(n, ast.FunctionDef) and n.name in ("_mask_text", "log"):
        keep.append(n)

have = sorted(n.name for n in keep if isinstance(n, ast.FunctionDef))
if have != ["_mask_text", "log"]:
    print("抽取失败：期望 ['_mask_text', 'log']，实际 %r" % have)
    print("提示：若 _mask_text 被改名/删除，说明第①层脱敏被移除了。")
    sys.exit(1)

sink, gui = io.StringIO(), []
ns = {"re": re, "time": time, "LOG_FH": sink, "_gui_sink": gui.append}
exec(compile(ast.Module(body=keep, type_ignores=[]), "<dsh_manager>", "exec"), ns)
_mask_text, log = ns["_mask_text"], ns["log"]

FAILS = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ((" | " + extra) if extra else ""))
    if not cond:
        FAILS.append(name)


# 形态同真实 token：43 字符，含 base64url 的 - 和 _
TOK = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6-XyZ_9"

# ── 纯函数层 ──────────────────────────────────────────────────────────────
masked = _mask_text("x?token=" + TOK)
chk("1 保留 token= 前缀与 token 前 4 位", masked.startswith("x?token=a1b2"), masked)
chk("2 第 5 位起全部换成 *", masked.endswith("a1b2" + "*" * (len(TOK) - 4)))
chk("3 等长替换（不改文件长度）", len(masked) == len("x?token=" + TOK))
chk("4 无 token 的文本原样返回",
    _mask_text("http://127.0.0.1:8780/ 无 token") == "http://127.0.0.1:8780/ 无 token")
chk("5 短于 8 字符的 token 不误伤（占位符如 token=short 不该被打码）",
    _mask_text("token=short") == "token=short")
chk("6 一行内多个 token 全部打码",
    TOK not in _mask_text("a?token=%s b?token=%s" % (TOK, TOK[::-1]))
    and TOK[::-1] not in _mask_text("a?token=%s b?token=%s" % (TOK, TOK[::-1])))

# ── log() 出口端到端（631 行那条真实路径）────────────────────────────────
url = "http://127.0.0.1:8781/?token=" + TOK
log("[v1] 打开浏览器(chrome) -> %s" % url)
line = sink.getvalue()
chk("7 log() 落盘行里没有明文 token（第①层仍接线）", TOK not in line, line.strip()[:70])
chk("8 落盘行仍保留前 4 位便于与内存 URL 对照", ("token=a1b2" + "*" * (len(TOK) - 4)) in line)
chk("9 GUI 日志区同样不出现明文 token", TOK not in "".join(gui))
ts = "[%s] " % time.strftime("%Y-%m-%d %H:%M:%S")
chk("10 脱敏行与未脱敏行等长（含 log 补的换行）",
    len(line) == len(ts + "[v1] 打开浏览器(chrome) -> " + url) + 1,
    "%d" % len(line))

print("\n== test-log-mask: 10 项，FAIL=%d ==" % len(FAILS))
sys.exit(1 if FAILS else 0)
