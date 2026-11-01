# -*- coding: utf-8 -*-
"""
fix-bat-encoding.py —— 把仓库根目录的 .bat 统一转成 GBK(cp936) 并去掉 chcp 行。

为什么必须这么做（Win10 家庭版乱码根因）：
  * 中文 Windows 的 cmd.exe 用「ANSI 代码页(GBK)」逐字节解析 .bat 文件内容。
  * 若 .bat 存成 UTF-8 无 BOM（我们之前的写法），中文部分在 cmd 眼里就是非法字节 → 乱码。
  * 在 bat 里再执行 `chcp 65001` 只能改变【后续输出】的代码页，改变不了 cmd 已经
    按 GBK 解析脚本内容的事实；而且切页后 PowerShell 回显到 cmd 的字节又被按 UTF-8
    解释，反而出现第二重乱码。
  * 正确做法：脚本文件本身存成 GBK，且【不要】chcp 切页 —— 读写两端代码页一致。

用法：
    python scripts/fix-bat-encoding.py            # 修复并回写（仓库根的 .bat）
    python scripts/fix-bat-encoding.py --check    # 只检查不写回
    python scripts/fix-bat-encoding.py --slots    # 检查（并修复）生成到各槽目录的 bat
    python scripts/fix-bat-encoding.py --slots --check   # 只检查槽目录里的 bat

为什么要有 --slots（2026-10-15 故障反馈「dsh-cli.bat 双击没反应」后补）：
  管理器会往**每个版本槽目录**生成三份 bat（dsh.bat / kill-<port>.bat / dsh-cli.bat）。
  这些文件不在仓库里，上面的默认模式**扫不到**——而它们恰恰是用户会双击的文件。
  实测踩坑：生成器一度写成 UTF-8 无 BOM，cmd.exe 按 GBK 读 → 相邻中文 REM 行被
  字节边界撕裂 → 后半截被当命令执行 → 「命令语法不正确」rc=255 → 双击窗口一闪就没。
  （生成器本身已改成 GBK，见 dsh_manager.pyw:_write_bat；--slots 用于守住存量文件。）
  槽目录从 config.json 的 bin_js 反推（取 node_modules 的上一级 = 槽根）。
"""
import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# GBK 无法编码的排版字符 → 替换为 ASCII 近似（避免写成 '?' 影响可读性）
REPLACE = {
    "\u2014": "-", "\u2013": "-",      # 破折号
    "\u2018": "'", "\u2019": "'",      # 单引号
    "\u201c": '"', "\u201d": '"',      # 双引号
    "\u2026": "...",                   # 省略号
    "\u00b7": "-",                     # 间隔号
    "\u00a0": " ",                     # 不换行空格
    "\u2192": "->", "\u2190": "<-",    # 箭头
    "\u25cf": "*", "\u25cb": "o",      # 圆点
    "\u26a0": "!", "\ufe0f": "",       # 警告三角 + 变体选择符
    "\u2713": "v", "\u2714": "v", "\u2717": "x",  # 勾/叉
    "\u2605": "*", "\u2606": "-",      # 星
}


def convert(txt):
    """返回 (去 chcp 后的文本, 被移除的行, 被替换的字符集合)"""
    kept, removed = [], []
    for ln in txt.splitlines(keepends=True):
        if re.match(r"^\s*chcp\s+\d+", ln, re.I):
            removed.append(ln.rstrip())
        else:
            kept.append(ln)
    txt2 = "".join(kept)

    bad = []
    for ch in sorted(set(txt2)):
        if ord(ch) > 127:
            try:
                ch.encode("gbk")
            except Exception:
                bad.append(ch)
    for ch in bad:
        txt2 = txt2.replace(ch, REPLACE.get(ch, "?"))
    return txt2, removed, bad


def slot_roots():
    """从 config.json 反推各版本槽的根目录（= bin_js 里 node_modules 的上一级）。"""
    cfg_path = os.path.join(REPO, "config.json")
    if not os.path.isfile(cfg_path):
        return []
    try:
        cfg = json.load(open(cfg_path, encoding="utf-8-sig"))
    except Exception as e:
        print("[warn] 读 config.json 失败：%s" % e)
        return []
    home = os.path.expanduser("~")
    base = str(cfg.get("install_base", "<home>")).replace("<self>", REPO) \
                                                   .replace("<home>", home) \
                                                   .replace("<base>", home)
    roots = []
    for key, v in (cfg.get("versions") or {}).items():
        binjs = v.get("bin_js") or ""
        binjs = binjs.replace("<base>", base).replace("<home>", home)
        if "/node_modules/" not in binjs.replace("\\", "/"):
            continue
        root = binjs.replace("\\", "/").split("/node_modules/")[0]
        if os.path.isdir(root):
            roots.append((key, root))
        else:
            print("[warn] 槽 %s 的目录不存在：%s" % (key, root))
    return roots


def check_one(p, check_only, fix_install_hint=True):
    """检查/修复单个 bat 文件。返回是否含非 GBK 字符。"""
    raw = open(p, "rb").read()
    try:
        txt = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            txt = raw.decode("gbk")
        except UnicodeDecodeError:
            print("%-46s [!] 既非 UTF-8 也非 GBK，跳过" % p)
            return True

    txt2, removed, bad = convert(txt)
    # 行尾：.bat 必须是 CRLF。
    # **--check 以前不查这一项** —— 于是「LF-only 的 bat」能一路绿灯通过门禁。
    # 2026-10-31 实际踩到：一次 GBK 重写（读时把 CRLF 转成 LF、写时没转回来）
    # 把两个入口 bat 全变成 LF-only，而本脚本仍报 OK。cmd 对 LF-only 的容忍度
    # 因机器而异（见文件头的「双击没反应」踩坑），必须在门禁里挡住。
    lf_only = raw.count(b"\n") - raw.count(b"\r\n")
    if lf_only:
        print("%-46s [!] 行尾是 LF-only（%d 行）—— .bat 必须 CRLF" % (p, lf_only))
    # 统一 CRLF（.bat 必须用 CRLF，LF 在某些机器上会吞行）
    body = txt2.replace("\r\n", "\n").replace("\n", "\r\n")
    out = body.encode("gbk")

    if bad:
        pass
    status = "OK " if not bad else "FIX"
    if not check_only and out != raw:
        open(p, "wb").write(out)
    print("%-46s %s 去chcp=%d 非GBK字符=%d 写入=%dB%s" %
          (p, status, len(removed), len(bad), len(out),
           "（已回写）" if (not check_only and out != raw) else ""))
    for r in removed:
        print("      移除: %s" % r.strip())
    if bad:
        print("      替换: %s" % " ".join(repr(c) for c in bad))
    return bool(bad) or (check_only and lf_only > 0)


def main():
    check_only = "--check" in sys.argv
    slots_mode = "--slots" in sys.argv
    os.chdir(REPO)

    if slots_mode:
        # ── 扫生成到各槽目录的 bat（check-all.py 调的就是这条）──
        roots = slot_roots()
        if not roots:
            print("没从 config.json 推求出任何槽目录（或都不存在）")
            return 1
        bad_all = 0
        total = 0
        for key, root in roots:
            bats = sorted(f for f in glob.glob(os.path.join(root, "*.bat")))
            if not bats:
                print("[skip] 槽 %s：目录里没有 bat -> %s" % (key, root))
                continue
            print("== 槽 %s：%s" % (key, root))
            for p in bats:
                total += 1
                if check_one(p, check_only):
                    bad_all += 1
        print("\n%s：%d 个槽目录里的 bat，%d 个含非 GBK 字符" %
              ("检查完成" if check_only else "修复完成", total, bad_all))
        return 0

    files = sorted(glob.glob("*.bat"))
    if not files:
        print("未找到 .bat 文件（cwd=%s）" % REPO)
        return 1

    bad_all = 0
    for p in files:
        raw = open(p, "rb").read()
        try:
            txt = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                txt = raw.decode("gbk")
            except UnicodeDecodeError:
                print("%-26s [!] 既非 UTF-8 也非 GBK，跳过" % p)
                bad_all += 1
                continue

        txt2, removed, bad = convert(txt)

        # 行尾：.bat 必须是 CRLF。与 check_one 里同一道门（那边给 --slots 用）。
        # 2026-10-31 实际踩到：一次 GBK 重写把两个入口 bat 变成 LF-only，
        # 而本脚本仍报 OK —— 因为 --check 当时只查编码，不查行尾。
        lf_only = raw.count(b"\n") - raw.count(b"\r\n")
        if lf_only:
            print("%-26s [!] 行尾是 LF-only（%d 行）—— .bat 必须 CRLF" % (p, lf_only))
            if check_only:
                # 修复模式下这条会被下面的写回改好，不该算"失败"
                bad_all += 1

        # 统一 CRLF（.bat 必须用 CRLF，LF 在某些机器上会吞行）
        body = txt2.replace("\r\n", "\n").replace("\n", "\r\n")
        out = body.encode("gbk")

        if bad:
            bad_all += 1
        status = "OK " if not bad else "FIX"
        if not check_only:
            open(p, "wb").write(out)

        print("%-26s %s 去chcp=%d 非GBK字符=%d 写入=%dB" %
              (p, status, len(removed), len(bad), len(out)))
        for r in removed:
            print("      移除: %s" % r.strip())
        if bad:
            print("      替换: %s" % " ".join(repr(c) for c in bad))

    print("\n%s：%d 个 bat，%d 个含非 GBK 字符" %
          ("检查完成" if check_only else "修复完成", len(files), bad_all))
    # 2026-10-31 修：以前这里无条件 `return 0`，于是 `--check` **永远不会失败** ——
    # check-all.ps1 里那句 `if ($LASTEXITCODE -ne 0) { Bad … }` 等于装饰品。
    # （--slots 那条一直是正确的，只有仓库模式漏了。）
    return 0 if bad_all == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
