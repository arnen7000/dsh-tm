# -*- coding: utf-8 -*-
r"""py-syntax-check.py —— 只做内存编译检查，不写 __pycache__。

为什么不用 `python -m py_compile`：
  · 它会在源码旁写 __pycache__/*.pyc —— 检查动作污染仓库，还要求对该目录有写权限；
  · 在只读/受限环境下会以"权限失败"的形式假报语法错。
`compile(bytes, ...)` 会按 PEP 263 自动识别编码声明，纯内存、零副作用。

用法：py py-syntax-check.py <文件...>     退出码 0 = 全部通过，1 = 有语法错

**编译警告也算失败。** 典型是 `SyntaxWarning: invalid escape sequence '\d'` ——
docstring 里写了 `D:\dsh-temp` 却没加 `r` 前缀。它不会让程序跑不起来，但意味着
那个反斜杠的含义和作者以为的不一样；而且一旦将来变成 `re` 的模式串就是真 bug。
零容忍比"看到了懒得修"便宜。
"""
import pathlib
import sys
import warnings

bad = 0
for arg in sys.argv[1:]:
    path = pathlib.Path(arg)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")          # 警告升级成异常
            compile(path.read_bytes(), str(path), "exec")
    except SyntaxError as exc:
        print("SYNTAX %s  line %s col %s: %s" % (path.name, exc.lineno, exc.offset, exc.msg))
        bad += 1
    except Exception as exc:                      # 读不了 / 编译警告也算失败，别静默放过
        print("WARN   %s: %s: %s" % (path.name, type(exc).__name__, exc))
        bad += 1

print("checked %d file(s), %d problem(s)" % (len(sys.argv) - 1, bad))
sys.exit(1 if bad else 0)
