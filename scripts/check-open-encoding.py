# -*- coding: utf-8 -*-
"""检查：文本模式的 open() 是否都写了 encoding。

为什么值得单独一道闸（2026-11-14，CI 连红 25 次才查出来的）：
    GitHub Actions 的 windows runner 是**英文系统**，ANSI 代码页是 cp1252，
    `locale.getpreferredencoding()` 因此是 cp1252。本机中文 Windows 是 cp936。
    于是同一行 `open(p, "w").write("槽外数据")`：
        · 本地（cp936）：写成功，测试全绿
        · CI（cp1252）：UnicodeEncodeError: 'charmap' codec can't encode
                        characters in position 0-3
    而 PYTHONIOENCODING 只管 stdout/stderr，**管不到 open()** —— 所以设了也没用。
    症状是"本地一直绿、线上一直红"，属于最难查的一类；用静态检查代替记忆。

判据：
    · 只看**裸 open(...)** 和 io.open(...)（tarfile.open / webbrowser.open
      这类同名方法不属于文件 I/O，不能误伤 —— 第一版就误报过）；
    · 二进制模式（mode 里带 b）不要求 encoding；
    · 已经有 encoding= 或 errors= 的放过。

用法：
    python scripts/check-open-encoding.py            # 检查仓库，0 = 通过
    python scripts/check-open-encoding.py --quiet    # 只报汇总
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "node_modules", "__pycache__", "runtime", "temp",
             ".workbuddy-ai", ".venv", "venv", "build", "dist"}


def _mode_of(call):
    """取出 open() 的 mode（位置参数第 2 个或 mode= 关键字），缺省 'r'。"""
    mode = None
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        mode = call.args[1].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    return "r" if mode is None else str(mode)


def _is_file_open(call):
    """只认裸 open(...) 与 io.open(...)；不认 tarfile.open / webbrowser.open。"""
    fn = call.func
    if isinstance(fn, ast.Name) and fn.id == "open":
        return True
    if (isinstance(fn, ast.Attribute) and fn.attr == "open"
            and isinstance(fn.value, ast.Name) and fn.value.id == "io"):
        return True
    return False


def scan():
    bad = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if not name.endswith((".py", ".pyw")):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, ROOT)
            try:
                with open(path, encoding="utf-8") as f:
                    src = f.read()
                tree = ast.parse(src, filename=rel)
            except (OSError, SyntaxError, UnicodeDecodeError) as e:
                bad.append((rel, 0, "?", "读不了/解析不了：%s" % e))
                continue
            lines = src.splitlines()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not _is_file_open(node):
                    continue
                mode = _mode_of(node)
                if "b" in mode:
                    continue
                if any(kw.arg in ("encoding", "errors") for kw in node.keywords):
                    continue
                text = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
                bad.append((rel, node.lineno, mode, text))
    return bad


def main(argv):
    quiet = "--quiet" in argv
    bad = scan()
    if not bad:
        print("RESULT: OK —— 所有文本模式的 open() 都显式指定了 encoding")
        return 0
    for rel, line, mode, text in sorted(bad):
        print("%s:%d  mode=%s  %s" % (rel, line, mode, text[:110]))
    print("")
    print("共 %d 处文本模式 open() 没写 encoding —— 英文 Windows（cp1252）上"
          "写/读中文会抛 UnicodeEncodeError，而本机中文 Windows 是绿的。"
          % len(bad))
    print("改法：open(path, \"%s\", encoding=\"utf-8\")" % (bad[0][2] or "r"))
    if not quiet:
        print("RESULT: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
