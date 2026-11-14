# -*- coding: utf-8 -*-
"""把源码里**确定是 UI 文案**的中文字符串包成 `T("...")`，供 dsh_i18n 运行时翻译。

用法：
    python scripts/i18n-wrap.py            # 只统计，不改文件
    python scripts/i18n-wrap.py --apply    # 真的改

## 为什么是白名单，不是黑名单

第一版用「排除法」：包所有中文常量，只排除比较值 / 字典键。跑完回归发现漏了
两类**致命**的：`os.path.join(b, "dsh备份")` 里的目录名、`_pick_node_source("纳入")`
里的枚举值。英文模式下前者会把备份目录改名、后者会让判断永远不成立 —— 而且
`"纳入"` 这种既不是字典键也不是比较值，排除法根本看不见它。

所以改成白名单：**只包下面这几种位置**，其它中文一律原样保留。宁可少翻几句，
也不能把逻辑值翻掉。

  1. 关键字参数 `text=` / `title=` / `prompt=` 的值
  2. `log(...)` 的第一个位置参数
  3. `messagebox.showinfo / showerror / showwarning / askyesno / ...` 的所有位置参数
  4. `xxx.set(...)`（状态行 `status_vars[key].set(...)`）的第一个位置参数
  5. `win.title(...)` 的第一个位置参数

## 其它规则

  · 跳过 docstring。
  · 跳过已经包在 `T(...)` 里的（可重复执行）。
  · 只处理单行字符串；跨行的长段落列出来留给人工。
  · 用 AST 而不是正则：正则会把注释、变量名、以及 `d["错误"]` 这种键一起改掉。
"""
import ast
import io
import os
import re
import sys

CJK = re.compile(r"[\u4e00-\u9fa5]")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = ["dsh_manager.pyw", "dsh-tm.py", "dsh_adopt.py", "dsh_drift.py",
           "dsh_health.py", "dsh_install_ops.py", "dsh_kill.py", "dsh_move.py",
           "dsh_registry.py", "dsh_uninstall_ops.py"]
IMPORT_LINE = "from dsh_i18n import T\n"

# 弹框函数名（messagebox.xxx / tkinter.messagebox.xxx 都算）
MSGBOX = {"showinfo", "showerror", "showwarning", "askyesno", "askokcancel",
          "askretrycancel", "askquestion", "askyesnocancel"}


def _doc_ids(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            b = getattr(n, "body", None)
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) \
               and isinstance(b[0].value.value, str):
                out.add(id(b[0].value))
    return out


def _ui_nodes(tree):
    """白名单：返回 {id(Constant): Constant}，只含确定是 UI 文案的字符串。"""
    out = {}

    def take(node):
        """登记一个"显示用"字符串。

        `log("失败 %s" % e)` 这种要**先翻译模板再填参数**，所以往左剥一层：
        取 `%` 的左操作数、或 `.format()` 的接收者。直接对格式化后的成品查表是
        查不到的（每句都带不同的路径/版本号）。
        """
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            node = node.left
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "format":
            node = node.func.value
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
           and CJK.search(node.value):
            out[id(node)] = node

    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        # ① 关键字参数 text= / title= / prompt=
        for kw in n.keywords:
            if kw.arg in ("text", "title", "prompt"):
                take(kw.value)
        f = n.func
        fname = None
        is_attr = isinstance(f, ast.Attribute)
        if is_attr:
            fname = f.attr
        elif isinstance(f, ast.Name):
            fname = f.id
        # ② log(...) 第一个位置参数
        if fname == "log" and n.args:
            take(n.args[0])
        # ③ messagebox.* 的所有位置参数
        if is_attr and fname in MSGBOX:
            for a in n.args:
                take(a)
        # ④ xxx.set(...)：状态行 status_vars[key].set("已停止 ...")
        if is_attr and fname == "set" and n.args:
            take(n.args[0])
        # ⑤ win.title("...")
        if is_attr and fname == "title" and n.args:
            take(n.args[0])
        # ⑥ 进度回调 p.on_step("正在下载…")：参数只用于显示
        if is_attr and fname == "on_step" and n.args:
            take(n.args[0])
    return out


def _collect(src):
    tree = ast.parse(src)
    doc = _doc_ids(tree)
    ui = _ui_nodes(tree)
    parent = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            parent[id(c)] = p

    def already_wrapped(node):
        p = parent.get(id(node))
        if isinstance(p, ast.Call):
            f = p.func
            return (isinstance(f, ast.Name) and f.id == "T") or \
                   (isinstance(f, ast.Attribute) and f.attr == "T")
        return False

    todo, skipped = [], []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in doc or not CJK.search(node.value):
            continue
        if id(node) not in ui:
            continue
        if already_wrapped(node):
            skipped.append((node.lineno, "已包T", node.value[:36]))
            continue
        if node.lineno != node.end_lineno:
            skipped.append((node.lineno, "跨行", node.value[:36]))
            continue
        seg = ast.get_source_segment(src, node)
        if seg is None:
            skipped.append((node.lineno, "取不到片段", node.value[:36]))
            continue
        todo.append((node.lineno, node.col_offset, node.end_col_offset, seg))
    return todo, skipped


def _apply(src, todo):
    """按字节偏移倒序替换（AST 的 col_offset 是 UTF-8 字节偏移，不是字符）。"""
    lines = src.splitlines(keepends=True)
    for lineno, col, end_col, seg in sorted(todo, key=lambda x: (x[0], x[1]), reverse=True):
        b = lines[lineno - 1].encode("utf-8")
        new = b[:col] + ("T(" + seg + ")").encode("utf-8") + b[end_col:]
        lines[lineno - 1] = new.decode("utf-8")
    return "".join(lines)


def _ensure_import(src):
    if "from dsh_i18n import T" in src or "import dsh_i18n" in src:
        return src
    lines = src.splitlines(keepends=True)
    # 只认**顶格**的 import（col 0）。缩进的 import 在函数体或 try 块里，
    # 插进去会直接把 `try:` 和 `except` 拆开 —— 2026-11-14 实测，三个文件当场语法错。
    last = -1
    for i, l in enumerate(lines[:200]):
        if l[:1].isspace():
            continue
        s = l.strip()
        if s.startswith("import ") or s.startswith("from "):
            last = i
    if last >= 0:
        lines.insert(last + 1, IMPORT_LINE)
    else:
        at = 0
        for i, l in enumerate(lines[:5]):
            if l.startswith("#") or l.startswith('"""') or l.startswith("'''"):
                at = i + 1
        lines.insert(at, IMPORT_LINE)
    return "".join(lines)


def main():
    apply = "--apply" in sys.argv
    grand = 0
    for name in TARGETS:
        path = os.path.join(ROOT, name)
        if not os.path.isfile(path):
            print("!! 缺失", name)
            continue
        src = io.open(path, encoding="utf-8-sig").read()
        try:
            todo, skipped = _collect(src)
        except SyntaxError as e:
            print("!! 语法错误 %s: %s" % (name, e))
            continue
        print("%-24s 待包 %4d   跳过 %3d" % (name, len(todo), len(skipped)))
        for ln, why, txt in skipped:
            print("      %5d %-10s %s" % (ln, why, txt))
        grand += len(todo)
        if apply and todo:
            out = _ensure_import(_apply(src, todo))
            io.open(path, "w", encoding="utf-8", newline="\n").write(out)
    print("=" * 48)
    print("合计待包:", grand, "|", "已写入" if apply else "dry-run，未改文件")
    if not apply:
        print("加 --apply 才会真的改。")


if __name__ == "__main__":
    main()
