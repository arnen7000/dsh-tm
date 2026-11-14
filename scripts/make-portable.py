# -*- coding: utf-8 -*-
"""打一个「解压即用」的便携包：仓库源码 + 精简过的 runtime。

为什么要它：仓库不含 `runtime/`（200+ MB，`.gitignore` 排除），从 GitHub 下源码
的人还得自己装 Python 和 Node。这个脚本把两者合成一个 zip，作为 GitHub Release
的附件发布 —— 用户下载解压就能双击运行，什么都不用装。

精简掉的东西（都不影响运行）：
  · `*.pdb`        调试符号，37 个约 62 MB。Python/Node 跑起来完全用不到
  · `include/`     编译扩展用的 C 头文件
  · `libs/`        编译扩展用的静态库
  · `Lib/test/`    CPython 自带测试套件
  · `Lib/idlelib/` 自带的 IDE，本程序不用
  · `__pycache__`  字节码缓存，首次运行会自己生成
  · `Scripts/`     pip / easy_install 等，装 dsh 走槽自带的 npm，不用它们

用法：
    python scripts/make-portable.py              # 输出 dist/dsh-tm-<ver>-portable.zip
    python scripts/make-portable.py --keep-temp  # 保留中间目录，便于检查
"""
import os
import shutil
import subprocess
import sys
import tempfile
import io
import tarfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DIST = os.path.join(REPO, "dist")

# 复制 runtime 时跳过这些（相对 runtime/ 的路径片段）
RUNTIME_SKIP_DIRS = {"include", "libs", "test", "idlelib", "Scripts", "__pycache__",
                     "turtledemo", "ensurepip", "distutils"}
RUNTIME_SKIP_EXT = {".pdb", ".pyc", ".pyo", ".chm", ".lib", ".exp"}


def app_version():
    try:
        with open(os.path.join(REPO, "VERSION"), encoding="utf-8-sig") as f:
            return (f.read() or "").strip() or "0.0.0"
    except Exception:
        return "0.0.0"


def export_source(dst):
    """用 git archive 导出**受控文件**，避免把 config.json / 日志 / 备份带进去。"""
    os.makedirs(dst, exist_ok=True)
    r = subprocess.run(["git", "-C", REPO, "archive", "HEAD"],
                       capture_output=True)
    if r.returncode != 0:
        raise SystemExit("git archive 失败：%s" % r.stderr.decode("utf-8", "replace"))
    # 用 tarfile 而不是外部 tar：Windows 自带的 bsdtar 对路径/空目录的兼容性
    # 不稳定（实测返回 exit 1），内置模块没有这个问题。
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
        tf.extractall(dst)


def copy_runtime(src, dst):
    """复制 runtime，跳过调试符号和编译期文件。返回 (保留文件数, 跳过文件数)。"""
    kept = skipped = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d.lower() not in RUNTIME_SKIP_DIRS]
        rel = os.path.relpath(root, src)
        out = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(out, exist_ok=True)
        for name in files:
            if os.path.splitext(name)[1].lower() in RUNTIME_SKIP_EXT:
                skipped += 1
                continue
            try:
                shutil.copy2(os.path.join(root, name), os.path.join(out, name))
                kept += 1
            except Exception:
                skipped += 1
    return kept, skipped


def main():
    keep = "--keep-temp" in sys.argv
    ver = app_version()
    os.makedirs(DIST, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="dsh-tm-portable-")
    root = os.path.join(tmp, "dsh-tm")
    try:
        print("1/3 导出源码（git archive HEAD）…")
        export_source(root)
        print("2/3 复制并精简 runtime …")
        src_rt = os.path.join(REPO, "runtime")
        if not os.path.isdir(src_rt):
            raise SystemExit("找不到 runtime/ —— 请在装好运行时的机器上跑本脚本")
        kept, skipped = copy_runtime(src_rt, os.path.join(root, "runtime"))
        print("      保留 %d 个文件，跳过 %d 个（pdb / 编译期文件）" % (kept, skipped))

        # 解压后第一眼能看到的东西：告诉用户接下来做哪三步
        with open(os.path.join(root, "使用前先读我.txt"), "w",
                  encoding="utf-8-sig", newline="\r\n") as f:
            f.write(
                "dsh-tm 便携版 %s\r\n"
                "========================================\r\n\r\n"
                "1. 双击 start-manager.bat 打开管理器\r\n"
                "2. 点「安装 dsh」选一个版本 → 装好后点卡片上的「启动」\r\n"
                "3. 想换语言：设置 → 界面 → 界面语言\r\n\r\n"
                "不需要安装 Python 或 Node —— 包里自带了，解压即用。\r\n\r\n"
                "详细说明见 docs 文件夹，或 README.md\r\n"
                % app_version())

        # 便携包不需要开发用的东西
        for junk in ("dist", ".git", "config.json", "dsh-manager.log"):
            p = os.path.join(root, junk)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.isfile(p):
                os.remove(p)

        out_zip = os.path.join(DIST, "dsh-tm-%s-portable.zip" % ver)
        print("3/3 打包 %s …" % os.path.basename(out_zip))
        n = 0
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for r, _d, fs in os.walk(root):
                for name in fs:
                    full = os.path.join(r, name)
                    z.write(full, os.path.relpath(full, tmp))
                    n += 1
        size = os.path.getsize(out_zip) / 1024 / 1024
        print("\n完成：%s" % out_zip)
        print("      %d 个文件，%.1f MB" % (n, size))
        print("\n下一步：把它作为附件传到 GitHub Release（单个文件上限 2 GB，没问题）。")
    finally:
        if keep:
            print("中间目录保留在：%s" % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
