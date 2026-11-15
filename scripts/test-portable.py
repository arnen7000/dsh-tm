# -*- coding: utf-8 -*-
"""便携包发布一致性回归。

历史事故（2026-11-14，v0.1.4）：文件名与 README-FIRST 读的是**工作区** VERSION
（已手改成 0.1.4、还没提交），源码却用 `git archive HEAD` 导出**已提交快照**
（当时 VERSION 还是 0.1.3）—— 发布出去的包叫 0.1.4，里面 VERSION / CHANGELOG
都是 0.1.3，用户解压后界面标题栏显示旧版本。

两道闸：
  1. 打包脚本必须有 preflight() / verify_zip()，且 main() 真的调用它们；
  2. 本地若存在 dist/*.zip，逐包核对
     「文件名版本 == 包内 VERSION == README-FIRST 首行 == CHANGELOG 条目」。
CI 上没有 dist/（被 .gitignore 排除），此时只跑第 1 道静态闸。
"""
import glob
import os
import re
import sys
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []
TOTAL = 0


def ck(cond, msg):
    global TOTAL
    TOTAL += 1
    if not cond:
        FAILS.append(msg)


def check_builder():
    p = os.path.join(REPO, "scripts", "make-portable.py")
    src = open(p, encoding="utf-8").read()
    ck("def preflight(ver):" in src, "make-portable.py 缺 preflight()")
    ck("def verify_zip(out_zip, ver):" in src, "make-portable.py 缺 verify_zip()")
    ck("HEAD:VERSION" in src, "preflight() 没有比对 HEAD:VERSION（事故根因）")
    main = src.split("def main():")[-1]
    ck("preflight(ver)" in main, "main() 没有调用 preflight()")
    ck("verify_zip(out_zip, ver)" in main, "main() 没有调用 verify_zip()")
    ck('"EN - Unzip anywhere' in src and '"中文 - 解压到任意目录' in src,
       "README-FIRST 不是「英文在前、中文紧随」")
    ck('newline=""' in src, "README-FIRST 丢了 newline=\"\"（会回归成 \\r\\r\\n）")


def check_zips():
    zips = sorted(glob.glob(os.path.join(REPO, "dist", "dsh-tm-*-portable.zip")))
    print("=== 本地便携包：%d 个" % len(zips))
    for p in zips:
        m = re.search(r"dsh-tm-([0-9][^-]*)-portable\.zip$", os.path.basename(p))
        if not m:
            continue
        ver, prefix = m.group(1), "dsh-tm/"
        with zipfile.ZipFile(p) as z:
            names = set(n.replace("\\", "/") for n in z.namelist())
            need = ("VERSION", "README-FIRST.txt", "CHANGELOG.md")
            if any(prefix + r not in names for r in need):
                ck(False, "%s 缺文件（%s）" % (os.path.basename(p), "/".join(need)))
                continue
            zv = z.read(prefix + "VERSION").decode("utf-8-sig").strip()
            first = z.read(prefix + "README-FIRST.txt").decode("utf-8-sig").splitlines()[0]
            changelog = z.read(prefix + "CHANGELOG.md").decode("utf-8-sig")
        ck(zv == ver, "%s 包内 VERSION=%s，应为 %s"
           % (os.path.basename(p), zv, ver))
        ck(ver in first, "%s 的 README-FIRST 首行没写 %s：%r"
           % (os.path.basename(p), ver, first))
        ck(("## [%s]" % ver) in changelog,
           "%s 包内 CHANGELOG 没有 ## [%s] 条目" % (os.path.basename(p), ver))
        print("    %s → 包内 VERSION=%s" % (os.path.basename(p), zv))


def main():
    check_builder()
    check_zips()
    print("=== test-portable: %d 项，FAIL=%d ===" % (TOTAL, len(FAILS)))
    for f in FAILS:
        print("  FAIL  " + f)
    print("RESULT: ALL PASS" if not FAILS else "RESULT: FAIL")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
