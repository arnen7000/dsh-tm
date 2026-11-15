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


def _git(*args):
    """在仓库里跑一条只读 git 命令，返回 (rc, stdout)。"""
    r = subprocess.run(["git", "-C", REPO] + list(args),
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "").strip()


def preflight(ver):
    """打包前把「版本号」这个事实来源钉死；不满足就地中止，绝不出包。

    历史事故（2026-11-14，v0.1.4）：工作区 VERSION 已手改成 0.1.4 但还没提交，
    文件名和 README-FIRST 读的是**工作区**，源码却用 `git archive HEAD` 导出
    **已提交快照**（当时还是 0.1.3）—— 打出来的包叫 0.1.4，里面 VERSION 与
    CHANGELOG 却是 0.1.3，用户解压后界面标题栏显示旧版本。两者必须同源。
    """
    rc, head_ver = _git("show", "HEAD:VERSION")
    if rc != 0 or head_ver != ver:
        raise SystemExit(
            "版本号不一致，拒绝打包：工作区 VERSION=%s，HEAD:VERSION=%s\n"
            "  先提交 VERSION / CHANGELOG.md（或打 tag）再打包。包内源码来自\n"
            "  git archive HEAD，不能拿工作区版本号当文件名。"
            % (ver, head_ver or "(读不到)"))
    rc, dirty = _git("status", "--porcelain", "--", "VERSION", "CHANGELOG.md")
    if rc != 0 or dirty:
        raise SystemExit("VERSION / CHANGELOG.md 有未提交改动，拒绝打包：\n%s"
                         % (dirty or rc))
    rc, changelog = _git("show", "HEAD:CHANGELOG.md")
    if rc != 0 or ("## [%s]" % ver) not in changelog:
        raise SystemExit("HEAD 的 CHANGELOG.md 里没有 ## [%s] 条目，拒绝打包。" % ver)
    print("      preflight OK：HEAD 与工作区都是 %s，CHANGELOG 有对应条目" % ver)


def verify_zip(out_zip, ver):
    """成品自检：包内三处版本号必须一致，不一致就报错（且退出码非 0）。"""
    with zipfile.ZipFile(out_zip) as z:
        names = set(n.replace("\\", "/") for n in z.namelist())
        prefix = "dsh-tm/"
        for rel in ("VERSION", "README-FIRST.txt", "CHANGELOG.md"):
            if prefix + rel not in names:
                raise SystemExit("成品自检失败：包里缺 %s" % rel)
        zip_ver = z.read(prefix + "VERSION").decode("utf-8-sig").strip()
        first = z.read(prefix + "README-FIRST.txt").decode("utf-8-sig").splitlines()[0]
        changelog = z.read(prefix + "CHANGELOG.md").decode("utf-8-sig")
    problems = []
    if zip_ver != ver:
        problems.append("包内 VERSION=%s，应为 %s" % (zip_ver, ver))
    if ver not in first:
        problems.append("README-FIRST 首行没写 %s：%r" % (ver, first))
    if ("## [%s]" % ver) not in changelog:
        problems.append("包内 CHANGELOG 没有 ## [%s] 条目" % ver)
    if problems:
        raise SystemExit("成品自检失败，这个包不要发布：\n  - "
                         + "\n  - ".join(problems))
    print("      verify OK：包内 VERSION / README-FIRST / CHANGELOG 都是 %s" % ver)


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
    print("0/3 版本号一致性预检 …")
    preflight(ver)
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

        # 解压后第一眼能看到的东西：告诉用户接下来做哪三步。
        # 文件名用 **ASCII**：zip 里非 ASCII 名靠 UTF-8 标志位，Windows 资源管理器
        # 和 7-Zip 认，但 GNU unzip / 部分老工具会显示成乱码。这个文件是"第一眼"，
        # 不能有任何环境差异 —— 内容仍然是中文。
        # newline="" 表示**不做任何换行转换** —— 内容里写的 `\r\n` 就是最终的
        # `\r\n`。写成 newline="\r\n" 会把内容里的 `\n` 再转一次，得到 `\r\r\n`
        # （Windows 记事本打开时每行之间多一个空行，2026-11-14 实测踩到）。
        with open(os.path.join(root, "README-FIRST.txt"), "w",
                  encoding="utf-8-sig", newline="") as f:
            # 公开分发物：英文在前、中文紧随（2026-11-15 审计补上）。
            f.write(
                "dsh-tm portable %s\r\n"
                "========================================\r\n\r\n"
                "EN - Unzip anywhere, then:\r\n"
                "  1. Double-click start-manager.bat\r\n"
                "  2. Click \"Install dsh\", pick a version, then \"Start\" on its card\r\n"
                "  3. Switch language: Settings > Interface > Interface language\r\n"
                "  4. Desktop shortcut: run runtime\\python\\python.exe build_lnk.py\r\n"
                "  No Python or Node needed - both are bundled.\r\n"
                "  Docs: docs/user-guide.en.md / README.en.md\r\n\r\n"
                "中文 - 解压到任意目录，然后：\r\n"
                "  1. 双击 start-manager.bat 打开管理器\r\n"
                "  2. 点「安装 dsh」选一个版本 → 装好后点卡片上的「启动」\r\n"
                "  3. 想换语言：设置 → 界面 → 界面语言\r\n"
                "  4. 想要桌面图标：在本目录运行\r\n"
                "     runtime\\python\\python.exe build_lnk.py\r\n"
                "  不需要安装 Python 或 Node —— 包里自带了，解压即用。\r\n"
                "  详细说明见 docs 文件夹，或 README.md\r\n"
                % ver)

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
        print("3/3 成品自检 …")
        verify_zip(out_zip, ver)
        import hashlib
        sha = hashlib.sha256()
        with open(out_zip, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                sha.update(chunk)
        print("\n完成：%s" % out_zip)
        print("      %d 个文件，%.1f MB" % (n, size))
        print("      SHA256: %s" % sha.hexdigest())
        print("\n下一步：把 zip 作为附件传到 GitHub Release，并把上面这串 SHA256 写进发布说明。")
    finally:
        if keep:
            print("中间目录保留在：%s" % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
