# -*- coding: utf-8 -*-
"""管理器侧的「卸载某个/某几个槽」实现。

与 scripts/uninstall.ps1 的分工（**两者必须保持一致**）
--------------------------------------------------------
  · `scripts/uninstall.ps1` —— 命令行场景的卸载器（含交互菜单、`-DryRun`）。
  · 本文件 —— 管理器 GUI 里「卸载 dsh 版本…」按钮的实现。

为什么不干脆让 GUI 去调 uninstall.ps1：GUI 有三件 ps1 做不好的事 ——
  1. 停进程可以**在进程内**走 `dsh_kill`（GUI 本来就持有签名与 tracked pid），
     不必再起一个 PowerShell + Python 子进程；
  2. 改 config.json 要跟安装/纳入用**同一套**原子写（`ensure_ascii=False`、indent=2），
     而 ps1 的 `ConvertTo-Json` 会把中文转成 \\uXXXX、缩进也变，diff 噪声大；
  3. 卸载完要能立刻刷新界面，不能等子进程。
代价是删除护栏有两份实现 —— 所以这里的规则与 ps1 的 `Test-SafeToDelete` **逐条对齐**，
并有 `scripts/test-slot-ops.py` 用同一批边界用例锁住（见该文件的 §护栏 一节）。

安全铁律（与 ps1 相同）
-----------------------
1. 停进程只走 dsh_kill 的「签名 + 端口」门控，非 dsh 程序占端口时**拒绝杀**。
2. 用户数据（`DSH_HOME`）**先备份、再删除**；备份失败则整个槽不动。
3. 临时目录（`temp_dir`）保留。
4. 只删「槽根目录之内、且不是上面两个目录」的条目；槽根本身不删。
5. 任何一步失败都**不把该槽从 config.json 移除** —— 否则用户失去重试入口。
6. 用户数据排在**程序文件之前**删：万一删不掉，程序还在，槽仍然可用；
   反过来会留下「程序没了、数据还在」的半残槽（2026-10-30 实测踩到过）。
"""
import json
import os
import shutil
import stat
import subprocess
import time

# 与 dsh_install_ops 共用同一套配置读写（原子写、备份），不重复实现。
import dsh_install_ops as OPS


# ══════════════════════════════════════════════════════════════════
# 路径工具
# ══════════════════════════════════════════════════════════════════

def norm(p):
    """小写 + 反斜杠转正斜杠 + 折叠重复斜杠（与 dsh_kill.norm_path 同语义）。
    Windows 路径大小写不敏感、正反斜杠混用极常见（config.json 里两种都有），
    不归一化就会出现「明明是同一个目录，字符串比不相等」。"""
    s = (p or "").lower().replace("\\", "/")
    while "//" in s:
        s = s.replace("//", "/")
    return s.rstrip("/") if len(s) > 2 else s


def is_under(child, parent):
    """child 是否**严格位于** parent 之内（不包含 parent 自身）。"""
    c, p = norm(child), norm(parent)
    if not c or not p:
        return False
    if c == p:
        return False
    return c.startswith(p + "/")


def is_drive_root(p):
    n = norm(p)
    return bool(n) and len(n) <= 3 and n.endswith(":")


def resolve(p, base, home, self_dir):
    """展开 `<base>` / `<home>` / `<self>` / `<npm_global>` 占位符。
    与 dsh_manager.resolve() 同语义 —— 那边不可 import，故此处保留一份。"""
    if not p or not isinstance(p, str):
        return p
    p = p.replace("<self>", self_dir)
    p = p.replace("<base>", base).replace("<home>", home)
    if "<npm_global>" in p:
        p = p.replace("<npm_global>", os.path.join(
            os.environ.get("APPDATA", ""), "npm", "node_modules"))
    return p


def install_base_of(cfg, home, self_dir):
    raw = str((cfg or {}).get("install_base") or "<home>")
    return os.path.expanduser(
        raw.replace("<self>", self_dir).replace("<home>", home))


def slot_root(cfg, base, home, self_dir):
    """从 bin_js 反推「含 node_modules 的那一层」：
    …/dshV1/node_modules/@deepseek-ai/dsh/lib/bin.js -> …/dshV1
    结构不符（bin_js 不在 …/node_modules/ 里）返回 ''。

    注意：对 **npm 全局安装** 也会返回 `%APPDATA%\\npm` —— 那里确实含有 node_modules，
    但它不是「某个槽的目录」，删它的子项等于删掉所有全局包。所以调用方必须先用
    `is_global_install()` 排除，不能只靠这个返回值。"""
    bj = resolve((cfg or {}).get("bin_js") or "", base, home, self_dir)
    if not bj:
        return ""
    d = os.path.dirname(bj)                     # .../lib
    d = os.path.dirname(d)                      # .../dsh
    d = os.path.dirname(d)                      # .../@deepseek-ai
    d = os.path.dirname(d)                      # .../node_modules
    if os.path.basename(d).lower() != "node_modules":
        return ""
    return os.path.dirname(d)


def is_global_install(cfg, base, home, self_dir):
    """这个槽是不是「npm 全局安装」（bin_js 落在 %APPDATA%\\npm 或 Node 安装器的
    node_modules 下）。是的话不能按槽删 —— 那里是**所有**全局包共用的目录，
    卸载只能走 `npm uninstall -g @deepseek-ai/dsh`。"""
    bj = norm(resolve((cfg or {}).get("bin_js") or "", base, home, self_dir))
    if not bj:
        return False
    for env in ("APPDATA", "LOCALAPPDATA"):
        root = os.environ.get(env)
        if not root:
            continue
        for sub in (("npm", "node_modules"), ("Programs", "nodejs", "node_modules")):
            pfx = norm(os.path.join(root, *sub))
            if pfx and bj.startswith(pfx + "/"):
                return True
    return False


# ══════════════════════════════════════════════════════════════════
# 删除护栏（与 uninstall.ps1 的 Test-SafeToDelete 逐条对齐）
# ══════════════════════════════════════════════════════════════════

def is_safe_to_delete(path, slot_root_dir, keep_paths, home, repo):
    """能否删除 path。返回 (bool, 原因)。

    规则（缺一不可）：
      a) path 存在，且不是 keep 列表里的任何一个（用户数据 / 临时目录）；
      b) 严格位于 slot_root 之内（槽根本身不能删）；
      c) 不等于用户主目录 / 仓库目录 / 盘符根；
      d) 不是 home 或 repo 的**上级**（防止配置写错把整片目录端掉）。
    """
    if not path or not os.path.exists(path):
        return (False, "不存在")
    if not slot_root_dir:
        return (False, "反推不出槽根目录，拒绝删除")
    n = norm(path)
    for k in (keep_paths or []):
        if k and n == norm(k):
            return (False, "用户数据/临时目录，保留")
    if not is_under(path, slot_root_dir):
        return (False, "不在槽根目录之内")
    if is_drive_root(path):
        return (False, "盘符根目录")
    for prot, why in ((home, "用户主目录"), (repo, "管理器目录")):
        if not prot:
            continue
        if n == norm(prot):
            return (False, why)
        if is_under(prot, path):
            return (False, "是%s的上级目录" % why)
    return (True, "")


def _is_reparse_path(p):
    """路径本身是不是 junction / 符号链接（不跟随）。"""
    try:
        st = os.stat(p, follow_symlinks=False)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)   # FILE_ATTRIBUTE_REPARSE_POINT


def _dir_size(path, budget_files=200000):
    """估算目录占用（用于告诉用户「释放了多少」）。**不跟随 junction/符号链接**。

    注意 os.walk(followlinks=False) 挡不住 junction：它内部用 os.path.islink()
    判断，而 islink() 对 junction 返回 False。本机 `.dsh-home` 里有 635 个
    junction 指向槽的 node_modules，会被重复计入 —— 实测 308 MB 报成 1.0 GB。
    所以这里显式把 reparse point 从 dirs 里剔掉。
    """
    total, seen = 0, 0
    try:
        for root, dirs, files in os.walk(path, followlinks=False):
            dirs[:] = [d for d in dirs
                       if not _is_reparse_path(os.path.join(root, d))]
            for f in files:
                fp = os.path.join(root, f)
                if _is_reparse_path(fp):
                    continue
                try:
                    total += os.path.getsize(fp)
                except Exception:
                    pass
                seen += 1
                if seen >= budget_files:
                    return total
    except Exception:
        pass
    return total


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.1f %s" % (n, unit)) if unit != "B" else ("%d B" % n)
        n /= 1024.0
    return "%d B" % n


def _installed_version(root):
    """读槽里真正装着的 dsh 版本号（不信 config 的 label）。
    读不到返回 ''。"""
    pj = os.path.join(root or "", "node_modules", "@deepseek-ai", "dsh",
                      "package.json")
    try:
        with open(pj, "r", encoding="utf-8-sig") as f:
            return str(json.load(f).get("version") or "")
    except Exception:
        return ""


def _is_reparse(entry):
    """entry 是不是 junction / 符号链接（Windows 的 reparse point）。

    为什么不用 os.path.islink()：它只认真正的 symlink（IO_REPARSE_TAG_SYMLINK），
    对 junction（IO_REPARSE_TAG_MOUNT_POINT）返回 False —— 而 dsh 的
    `.dsh-home/profiles/node_modules` 里全是 junction。必须读文件属性位。
    """
    return _is_reparse_path(entry.path)


def _collect_links(src):
    """列出 src 下所有 junction / 符号链接，返回 [(相对路径, 目标)]。

    只读目录项、不读文件内容，1.5 万文件约 0.2 秒。"""
    out = []
    stack = [src]
    while stack:
        s = stack.pop()
        try:
            entries = list(os.scandir(s))
        except OSError:
            continue
        for e in entries:
            if _is_reparse(e):
                try:
                    out.append((os.path.relpath(e.path, src), os.readlink(e.path)))
                except OSError:
                    out.append((os.path.relpath(e.path, src), ""))
            else:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                except OSError:
                    pass
    return out


def _robocopy(src, dst):
    """用系统 robocopy 多线程拷贝，返回 (ok, 说明)。

    为什么值得为它加一层依赖（2026-10-30 实测，同一份 196 MB / 15326 文件）：
        Python 逐文件 shutil.copy2  15.3 s（12.9 MB/s）
        robocopy /E /XJ /MT:8        3.7 s（52.4 MB/s）
    差距全在"每文件一次系统调用 + 杀软逐个扫"上，多线程能把这段重叠掉。
    `/XJ` 是关键：**不跟随 junction**（实测体积 196.1 MB，与不含链接的真实数据一致），
    否则又会回到 copytree 那个 942 MB / 14 分钟的老坑。
    robocopy 缺席或失败时由调用方退回逐文件实现 —— 备份是删数据前的最后一道网，
    不能因为少个工具就放弃。"""
    exe = shutil.which("robocopy") or os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "robocopy.exe")
    if not os.path.isfile(exe):
        return (False, "找不到 robocopy")
    cmd = [exe, src, dst, "/E", "/XJ", "/COPY:DAT", "/DCOPY:DAT",
           "/MT:8", "/R:1", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS", "/NP"]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT,
                              creationflags=getattr(subprocess,
                                                    "CREATE_NO_WINDOW", 0))
    except Exception as e:
        return (False, "无法启动 robocopy：%s" % e)
    # robocopy 的退出码是**位标志**：0–7 都是成功（0=无变化，1=有拷贝，2=有额外项…），
    # >=8 才是真失败。按"非 0 即失败"判断会把正常备份当成错误。
    if proc.returncode < 8:
        return (True, "")
    return (False, "robocopy 退出码 %d" % proc.returncode)


def _copy_user_data(src, dst, failures=None):
    """把 src 的内容拷到 dst，**跳过 junction / 符号链接**，返回跳过的链接清单。

    为什么不能用 shutil.copytree（2026-10-30 故障实证）：
    dsh 的 `.dsh-home/profiles/node_modules/*` 是指向**槽自己 node_modules** 的
    junction（实测 635 个），copytree 默认跟随链接，会把整套依赖再拷一遍 ——
    实测 487 MB 的 .dsh-home 备份成 942 MB、6 万文件、耗时 14 分钟，用户以为卡死。
    更糟的是有些 junction 指向槽外（指向别的插件仓库目录），
    把别人的目录整个拷进备份是错的。

    链接本来也不是"用户数据"：卸载后它的目标（槽 node_modules）就没了，链过去也是断链。
    所以只把链接记进 `_links.json`（相对路径 -> 目标），需要时手工重建即可。

    `failures`（2026-10-31 新增）：传入一个 list 时，**拷不动的文件**追加到它里面，
    而不是混进返回值。以前两者混在一起，`_links.json` 里会出现
    `{"path": "...", "target": "（拷贝失败）"}` 这种假链接 —— 备份看起来"完整"，
    实际少了文件，而卸载照样往下删。现在调用方拿到 failures 就能当场中止。
    """
    os.makedirs(dst, exist_ok=True)
    skipped = _collect_links(src)
    fails = failures if failures is not None else []

    # 快路：robocopy 多线程（约 4 倍）。失败则退回逐文件，慢但能把"哪个文件拷不动"
    # 逐个记下来 —— 备份宁可慢，也不能悄悄不完整。
    if _robocopy(src, dst)[0]:
        return skipped

    stack = [(src, dst)]
    while stack:
        s, d = stack.pop()
        try:
            entries = list(os.scandir(s))
        except OSError as e:
            fails.append((os.path.relpath(s, src) or ".", "读不到目录：%s" % e))
            continue
        for e in entries:
            target = os.path.join(d, e.name)
            if _is_reparse(e):
                continue        # 链接已在 _collect_links 里记过，这里不再重复记
            try:
                if e.is_dir(follow_symlinks=False):
                    os.makedirs(target, exist_ok=True)
                    stack.append((e.path, target))
                elif e.is_file(follow_symlinks=False):
                    shutil.copy2(e.path, target)
            except OSError as ex:
                # 单个文件拷不动（被占用/权限）不该让整份备份失败，
                # 但也别静默：记进 failures，由调用方决定中止（卸载必须中止）。
                fails.append((os.path.relpath(e.path, src), str(ex)))
    return skipped


def _walk_file_sizes(root):
    """列出 root 下所有**非链接**文件：相对路径 -> 字节数。不跟随 junction。"""
    out = {}
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            try:
                if _is_reparse(e):
                    continue
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    out[os.path.relpath(e.path, root)] = e.stat(
                        follow_symlinks=False).st_size
            except OSError:
                continue
    return out


def verify_backup(src, dst):
    """核对备份是否完整。返回 (missing, size_diff, total)。

    missing    —— 源里有、备份里没有的文件（**真正的丢数据**，调用方必须中止）
    size_diff  —— 两边都有但大小不同（多半是源正在被写：dsh 没停干净）
    total      —— 源里参与核对的文件数

    为什么要核对（2026-10-31 用户提问「备份完成是否需要进行相关的核对」）：
    在此之前备份是"尽力而为"—— robocopy 失败就退回逐文件，逐文件再失败就
    静默记一笔，然后照样往下删槽目录。dsh 运行时会锁住 `.dsh-home` 里的库文件，
    这个组合就是一条**静默丢数据**的路径。核对把"尽力而为"变成"要么完整、要么中止"。
    """
    s = _walk_file_sizes(src)
    d = _walk_file_sizes(dst)
    missing = [p for p in s if p not in d]
    size_diff = [p for p in s if p in d and d[p] != s[p]]
    return missing, size_diff, len(s)


def _force_rmtree(path):
    """删除整个目录树：**不跟随 junction/符号链接**，并先清只读位。

    为什么不能用 shutil.rmtree（2026-10-30 实测两个坑）：
      1) 只读文件（WinError 5）：`.dsh-home\\attachments` 里的对象文件是 `-r--r--r--`
         （附件按内容寻址存储，天然只读），rmtree 直接抛「拒绝访问」，整个卸载
         因此失败、留下半个槽。→ 删之前先 chmod +S_IWRITE。
      2) **junction 会被跟进**：`.dsh-home\\profiles\\node_modules\\*` 是指向槽自己
         node_modules 的 junction，还有几个指向**槽外**的插件仓库。
         shutil.rmtree 把 junction 当普通
         目录递归进去，会**删掉目标目录里的真实文件** —— 等于删到槽外去。
         → 碰到 reparse point 只删链接本身（对 junction 调 os.rmdir 即可，
            RemoveDirectory 只摘掉重解析点，目标原封不动）。
    """
    errors = []

    def _clear_readonly(p):
        try:
            os.chmod(p, stat.S_IWRITE)
        except OSError:
            pass

    def _walk(p):
        try:
            entries = list(os.scandir(p))
        except OSError as e:
            errors.append("%s（%s）" % (p, e))
            return
        for e in entries:
            child = e.path
            if _is_reparse(e):
                # 只摘链接，绝不进去删目标内容
                _clear_readonly(child)
                try:
                    os.rmdir(child)          # junction / 目录符号链接
                except OSError:
                    try:
                        os.unlink(child)     # 文件符号链接
                    except OSError as ex:
                        errors.append("%s（%s）" % (child, ex))
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    _walk(child)
                else:
                    _clear_readonly(child)
                    os.unlink(child)
            except OSError as ex:
                errors.append("%s（%s）" % (child, ex))
        _clear_readonly(p)
        try:
            os.rmdir(p)
        except OSError as ex:
            errors.append("%s（%s）" % (p, ex))

    _walk(path)
    if os.path.exists(path):
        raise OSError("有 %d 项删不掉（可能被进程占用或权限不足），首个：%s"
                      % (len(errors) or 1, errors[0] if errors else path))


def mark_incomplete(dirpath, kind, total, missing, fails):
    """在「不完整的备份」目录里留一个显眼的 `_INCOMPLETE.txt`。

    为什么需要（2026-10-31）：正常情况会把目录改名成 `…-INCOMPLETE`，一眼就能看出
    是废的。但改名本身可能失败（目录被索引器/杀软占住、权限不足），此时目录会保留
    **正常的名字** —— 那正是最危险的状态：用户以为数据已经安全了。
    标记文件是这个兜底：只要打开目录就能看到为什么不能删原数据。

    返回 True 表示标记写成功（写不进去也不算致命，调用方已经在抛错了）。
    """
    lines = [
        "这份备份【不完整】，不要拿它当后悔药。",
        "",
        "类型：%s" % (kind or "备份"),
        "时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "应备份 %d 个文件，缺 %d 个，拷不动 %d 个。" % (
            total or 0, len(missing or []), len(fails or [])),
        "",
    ]
    if missing:
        lines.append("缺失（源里有、这里没有）：")
        for p in list(missing)[:20]:
            lines.append("  - %s" % p)
        if len(missing) > 20:
            lines.append("  … 还有 %d 个" % (len(missing) - 20))
        lines.append("")
    if fails:
        lines.append("拷不动（被占用 / 权限不足）：")
        for p, why in list(fails)[:20]:
            lines.append("  - %s —— %s" % (p, why))
        if len(fails) > 20:
            lines.append("  … 还有 %d 个" % (len(fails) - 20))
        lines.append("")
    lines.append("原目录一个文件都没删。处理好占用/权限后重试即可。")
    try:
        with open(os.path.join(dirpath, "_INCOMPLETE.txt"), "w",
                  encoding="utf-8-sig", newline="\r\n") as f:
            f.write("\n".join(lines) + "\n")
        return True
    except Exception:
        return False


def _backup_slot(plan, key, backup_root, stamp):
    """把一个槽「卸掉前需要留的东西」备份成一个目录，返回备份路径。

    里面装什么（设计要求：用户数据 + 插件配置 + 版本号）：
      · `.dsh-home` 的全部内容 —— 会话 / 设置 / 凭据 / 插件配置（profiles）
      · `_slot-package.json` / `_slot-package-lock.json` —— 槽根的 npm 清单，
        用户装过哪些插件、钉的是哪个版本，全在这两份里（它们本来是要被删的程序文件）
      · `_meta.json` —— 槽名、实装版本、端口、浏览器、数据/临时目录、卸载时间、
        以及 config.json 里这个槽的原始片段（想手工恢复照着抄就行）
      · `_links.json` —— 只出现于原目录里有 junction/符号链接时：记下**没复制**的
        链接及其目标（内容不拷，见 `_copy_user_data` 的说明）

    布局刻意与旧版兼容：内容仍然直接落在 `<key>-userdata-<stamp>/` 下，
    老备份里的 settings.yaml 之类还在原位，只是多了几个下划线开头的说明文件。
    """
    os.makedirs(backup_root, exist_ok=True)
    dst = os.path.join(backup_root, "%s-userdata-%s" % (key, stamp))
    if os.path.exists(dst):
        dst = "%s-%d" % (dst, int(time.time() % 1000))
    os.makedirs(dst, exist_ok=True)

    home = plan.get("dsh_home") or ""
    links = []
    fails = []
    verify = {"checked": 0, "missing": 0, "size_changed": 0, "copy_failed": 0}
    if home and os.path.isdir(home):
        links = _copy_user_data(home, dst, fails)
        missing, changed, total = verify_backup(home, dst)
        verify = {"checked": total, "missing": len(missing),
                  "size_changed": len(changed), "copy_failed": len(fails)}
        if missing or fails:
            # **要么完整、要么中止**：源目录一个字节都还没删，用户修好占用/权限
            # 重试即可。留一个名字正常的"半份备份"比没有更危险 —— 用户会以为
            # 数据已经安全了，然后放心让管理器把槽删掉。
            bad = dst + "-INCOMPLETE"
            try:
                os.rename(dst, bad)
            except Exception:
                # 改名失败（目录被占用/权限）就退回原名 —— 但必须留下**明确的**
                # 标记文件，否则这份"名字正常的半份备份"就是上面说的那种危险品。
                bad = dst
            mark_incomplete(bad, "卸载备份", total, missing, fails)
            raise IOError(
                "备份不完整，已中止（槽目录一个文件都没删）：\n"
                "  · 应备份 %d 个文件，缺 %d 个%s\n"
                "  · 拷不动 %d 个%s\n"
                "常见原因：该槽的 dsh 还在运行（锁住了 .dsh-home 里的文件）、"
                "或杀软正在扫描。请先停止该槽再重试。\n"
                "已拷出的部分留在：%s"
                % (total, len(missing),
                   ("（例：%s）" % missing[0]) if missing else "",
                   len(fails),
                   ("（例：%s —— %s）" % fails[0]) if fails else "",
                   bad))
    if links:
        # 跳过的链接单独记一份，别让"备份看起来完整"变成误判
        try:
            with open(os.path.join(dst, "_links.json"), "w",
                      encoding="utf-8", newline="\n") as f:
                json.dump([{"path": p, "target": t} for p, t in links],
                          f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    root = plan.get("root") or ""
    for src_name, dst_name in (("package.json", "_slot-package.json"),
                               ("package-lock.json", "_slot-package-lock.json")):
        src = os.path.join(root, src_name)
        if os.path.isfile(src):
            try:
                shutil.copy2(src, os.path.join(dst, dst_name))
            except Exception:
                pass        # 清单只是"锦上添花"，拷不到不该让整个卸载失败

    meta = {
        "slot_key": key,
        "label": plan.get("label"),
        "root": root,
        "installed_version": _installed_version(root),
        "port": plan.get("port"),
        "dsh_home": home,
        "temp_dir": plan.get("temp_dir"),
        "backed_up_at": stamp,
        "note": "dsh 版本管理器卸载前自动备份。"
                ".dsh-home 内容在本目录；插件清单见 _slot-package*.json。"
                "junction/符号链接**未复制**（它们指向槽的 node_modules 或槽外目录，"
                "不是用户数据），清单见 _links.json。",
        "skipped_links": len(links),
        "verify": verify,
        "config_snippet": plan.get("config") or {},
    }
    try:
        with open(os.path.join(dst, "_meta.json"), "w",
                  encoding="utf-8", newline="\n") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return dst, verify


# ══════════════════════════════════════════════════════════════════
# 规划（纯计算，不写盘 —— 界面先用它把「会删什么」摆给用户看）
# ══════════════════════════════════════════════════════════════════

def plan_slot(key, cfg, base, home, repo, self_dir, versions_root=""):
    """算出一个槽的卸载计划。

    返回 dict：
      key/label/port/root/dsh_home/temp_dir/exists
      children: [{path, name, action: 'delete'|'keep', reason, size}]
      bytes:    将释放的字节数
      blocked:  非空 = 这个槽不能卸（原因）
    """
    cfg = cfg or {}
    root = slot_root(cfg, base, home, self_dir)
    dsh_home = resolve(cfg.get("dsh_home") or "", base, home, self_dir)
    temp_dir = resolve(cfg.get("temp_dir") or "", base, home, self_dir)
    keep = [dsh_home, temp_dir]
    plan = {
        "key": key,
        "label": cfg.get("label") or key,
        "port": cfg.get("port"),
        "root": root,
        "dsh_home": dsh_home,
        "temp_dir": temp_dir,
        "exists": bool(root and os.path.isdir(root)),
        "children": [],
        "bytes": 0,
        "blocked": "",
        "config": dict(cfg),          # 原始片段：备份的 _meta.json 里照抄，便于手工恢复
    }

    if not root:
        plan["blocked"] = "反推不出槽根目录（bin_js 不在本地 node_modules 布局里）"
        return plan
    # 全局安装：先于「目录是否存在」判断 —— 否则一个不存在的全局路径会被当成
    # 「目录已不在，直接移出配置」，用户会以为卸干净了，实际全局包还在。
    if is_global_install(cfg, base, home, self_dir):
        plan["blocked"] = ("这是 npm 全局安装（bin_js 在 npm 全局 node_modules 下），"
                           "不能按槽删除 —— 请用 npm uninstall -g @deepseek-ai/dsh")
        return plan
    # 槽根自身的合法性：必须在 versions_root 之内，且不是 home/repo/盘符根。
    # 这一条也要在「存在与否」之前 —— 配置写错指向盘根时，绝不能因为"目录不存在"
    # 就跳过校验。
    if versions_root and not is_under(root, versions_root):
        plan["blocked"] = "槽根目录不在版本根之内（%s 不在 %s 下），拒绝删除" % (root, versions_root)
        return plan
    if is_drive_root(root) or norm(root) in (norm(home), norm(repo)):
        plan["blocked"] = "槽根目录是系统/用户/仓库目录，拒绝删除：%s" % root
        return plan
    if not plan["exists"]:
        # 目录已不在（手工删过 / 装到一半失败）：仍允许把它从 config.json 移除，
        # 这样用户能自己收尾。删除阶段会跳过。
        return plan

    try:
        names = sorted(os.listdir(root))
    except Exception as e:
        plan["blocked"] = "列不出槽根目录内容：%s" % e
        return plan

    for name in names:
        p = os.path.join(root, name)
        # 2026-10-30 起：用户数据/临时目录也**要删**（备份成功后），否则会留下
        # `.dsh-home`+`.tmp` 的空壳目录，下次往同一槽装版本时 unique_dir 认为
        # 名字被占 → 装出 `dshV4-2` 这种名字，用户一看就觉得「上次没卸干净」。
        # 所以这里把 keep 列表传空跑护栏（护栏的其它条件一条不少），
        # 再单独标成 purge 让执行阶段知道"这些是先备份后删"。
        is_userdata = any(k and norm(p) == norm(k) for k in keep)
        ok, why = is_safe_to_delete(p, root, [], home, repo)
        if ok:
            size = _dir_size(p) if os.path.isdir(p) else (
                os.path.getsize(p) if os.path.isfile(p) else 0)
            plan["bytes"] += size
            plan["children"].append({"path": p, "name": name,
                                     "action": "purge" if is_userdata else "delete",
                                     "reason": "", "size": size})
        else:
            plan["children"].append({"path": p, "name": name, "action": "keep",
                                     "reason": why, "size": 0})
    # 所有子项都删完后，槽根应当空掉 → 连目录本身一起收掉（rmdir 只删空目录，
    # 万一还有 keep 的项就自然失败，不会误删）。
    plan["purge_root"] = True
    return plan


def plan_many(cfg, keys, home, repo, self_dir, versions_root=""):
    """给若干个槽出计划，返回 plan 列表（顺序与 keys 一致）。"""
    base = install_base_of(cfg, home, self_dir)
    versions = (cfg or {}).get("versions") or {}
    out = []
    for k in keys:
        if k in versions:
            out.append(plan_slot(k, versions[k], base, home, repo, self_dir, versions_root))
    return out


# ══════════════════════════════════════════════════════════════════
# 移出管理（detach）/ 重新纳入（readd）—— 磁盘一个字节都不动
#
# 与「卸载」的区别是根本性的：
#   · 卸载 = 停进程 → 备份用户数据 → **删整个槽目录** → 从 config 移除（程序没了）
#   · 移出 = 只从 config 移除，**磁盘一个字节都不动**（程序还在，能自己跑）
#
# 为什么移出是安全的：槽目录里的三件 bat 全是绝对路径，不读 config.json、
# 不读注册表 —— 这正是 `scripts/test-standalone.py` 守着的那条性质。移出之后
# 那个槽就是「一个普通的、能自己双击运行的 dsh 安装」，管理器从没装过它也一样。
#
# 为什么要留档（config 顶层 `detached`）：换过安装根之后，旧位置的槽**不在**
# `dsh_adopt.detect_existing_installs` 的扫描范围里（它只扫当前 versions_root
# 和 npm 全局），没有这条记录就再也自动发现不了，用户只能手工浏览目录。
#
# 记录里存**绝对路径**（不是 `<base>` 占位符）：占位符会跟着新安装根解析，
# 换根之后反而指错地方。同理，记录里那份槽配置也要先把 `<base>` 烘成绝对路径
# （见 `_bake_base`）—— 否则重新纳入时 dsh_home/temp_dir 会指到新根下面去。
# ══════════════════════════════════════════════════════════════════

DETACHED_FIELD = "detached"


def detached_records(cfg):
    """config 顶层 `detached` 的安全读取：不是 list / 缺字段一律当空。"""
    raw = (cfg or {}).get(DETACHED_FIELD)
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict) and (r.get("root") or r.get("config"))]


def _bake_base(slot_cfg, base):
    """把槽配置里的 `<base>` 占位符换成**当时**的绝对路径。

    移出管理之后，这条记录描述的是「那个槽此刻在磁盘上的位置」，是历史事实，
    不该再跟着 `<base>` 漂移 —— 用户把安装根从 C 盘换到 D 盘之后，
    `<base>/dsh-versions/dshV1` 会解析到 D 盘一个根本不存在的目录。
    `<home>` 不动：它不随安装根变化。"""
    out = {}
    for k, v in (slot_cfg or {}).items():
        if isinstance(v, str) and base and "<base>" in v:
            # normpath：`<base>/dsh-versions/x` 烘完是 `<base 的绝对路径>/dsh-versions/x`，
            # 正反斜杠混写虽然 Windows 认，但配置里看着像坏的，统一成反斜杠。
            out[k] = os.path.normpath(v.replace("<base>", base))
        else:
            out[k] = v
    return out


def detach_slots(keys, config_path, home, repo, self_dir, versions_root="",
                 progress=None, running_fn=None):
    """把若干槽「移出管理」：只改 config.json，磁盘一个字节都不动。

    running_fn(key, port) -> bool：可选。**正在运行的槽拒绝移出** —— 移出之后
    管理器就不再跟踪它的进程了，用户没地方点「停止」。（卸载可以先杀进程再删，
    移出什么都不动，所以只能要求先停。）

    返回 (ok, payload, 说明)。payload = {"moved": [...], "kept_in_config": [...],
    "details": [...], "records": [...]}。
    """
    p = progress or OPS.Progress()
    keys = [k for k in (keys or []) if k]
    if not keys:
        return (False, None, "没有选择要移出的槽")

    try:
        cfg = OPS.read_config(config_path)
    except Exception as e:
        return (False, None, "读 config.json 失败：%s" % e)
    versions = cfg.get("versions") or {}
    missing = [k for k in keys if k not in versions]
    if missing:
        return (False, None, "config.json 里没有这些槽：%s" % ", ".join(missing))

    base = install_base_of(cfg, home, self_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    recs = detached_records(cfg)
    moved, kept, details, new_recs = [], [], [], []

    for key in keys:
        scfg = versions.get(key) or {}
        plan = plan_slot(key, scfg, base, home, repo, self_dir, versions_root)
        root = plan["root"] or ""
        d = {"key": key, "label": plan["label"], "port": plan["port"],
             "root": root, "version": "", "errors": [], "warnings": []}
        details.append(d)

        if not root:
            # 反推不出槽根也能移出（毕竟只是删 config 里一条），但重新纳入时
            # 得让用户手工指路径 —— 说清楚，别让人以为还能一键回来。
            d["warnings"].append("反推不出槽根目录，记录里没有路径；重新纳入时要手工指定")
        if running_fn:
            try:
                if running_fn(key, plan["port"]):
                    d["errors"].append(
                        "这个槽正在运行，请先点「停止」再移出 —— "
                        "移出后管理器就不再跟踪它的进程了")
                    kept.append(key)
                    p.on_step("[%s] 跳过：正在运行" % key)
                    continue
            except Exception:
                pass    # 判不了就当没在跑：移出不破坏任何东西

        d["version"] = _installed_version(root) if root else ""
        # 统一成反斜杠：`<base>` 展开后是 `<home>` 的绝对形式（反斜杠），而配置里
        # 其余部分写的是 `/`，拼出来就是 `<home>/dsh-versions/dshV1` 这种混写。
        # Windows 认，但记录里看着像坏的。
        rec_root = os.path.normpath(root) if root else ""
        rec = {
            "key": key,
            "label": plan["label"],
            "root": rec_root,                   # 绝对路径：这是「它现在在哪」的事实
            "port": plan["port"],
            "version": d["version"],
            "at": stamp,
            # 原槽配置（<base> 已烘成绝对）—— 重新纳入时原样放回去，不猜
            "config": _bake_base(scfg, base),
        }
        # 同一目录只留一条（移出→重新纳入→再移出不该堆出两条）
        if root:
            recs = [r for r in recs
                    if norm(r.get("root") or "") != norm(root)]
        new_recs.append(rec)
        moved.append(key)
        p.on_step("[%s] 移出管理：%s（磁盘未改动）" % (key, root or plan["label"]))

    if not moved:
        msgs = ["%s：%s" % (d["key"], "；".join(d["errors"])) for d in details if d["errors"]]
        return (False, {"moved": [], "kept_in_config": kept, "details": details},
                "没有槽被移出。\n\n" + "\n".join(msgs or ["（原因未知）"]))

    # 先备份 config 再写：移出是「只改配置」，配置写坏了就全丢
    try:
        OPS.backup_config(config_path, stamp=stamp)
    except IOError as e:
        return (False, {"moved": [], "kept_in_config": keys, "details": details},
                "备份 config.json 失败，已中止（什么都没改）：%s" % e)

    try:
        cfg = OPS.read_config(config_path)
        for k in moved:
            (cfg.get("versions") or {}).pop(k, None)
        if recs or new_recs:
            cfg[DETACHED_FIELD] = recs + new_recs
        OPS.write_config_atomic(config_path, cfg)
    except Exception as e:
        return (False, {"moved": [], "kept_in_config": keys, "details": details},
                "写 config.json 失败（磁盘上的东西一个都没动，配置可能仍是原样，"
                "请检查）：%s" % e)

    payload = {"moved": moved, "kept_in_config": kept, "details": details,
               "records": new_recs, "stamp": stamp}
    parts = ["已移出管理：%s" % ", ".join(moved)]
    parts.append("磁盘上一个字节都没动 —— 这些槽仍然可以自己双击 dsh.bat 启动。")
    if kept:
        parts.append("没移出的（仍在管理里）：%s" % ", ".join(kept))
    for d in details:
        for w in d["warnings"]:
            parts.append("  · %s：%s" % (d["key"], w))
    if not (cfg.get("versions") or {}):
        parts.append("现在管理器不管理任何版本了 —— 安装根目录已解锁，"
                     "可以在「设置 → 目录」里换到别的位置，再装/纳入新的 dsh。")
    return (True, payload, "\n".join(parts))


def readd_slots(roots, config_path, home, repo, self_dir, versions_root="",
                progress=None):
    """把「已移出」的槽重新纳入管理。只改 config.json，磁盘一个字节都不动。

    端口**原样保留**：槽里的 bat 已经把端口写死在 `--port` 里，改端口就得重写
    三件 bat，那不是"重新纳入"该干的事。端口若已被别的槽占了，如实报冲突。

    返回 (ok, payload, 说明)；payload 含 added / kept / details。
    """
    p = progress or OPS.Progress()
    roots = [r for r in (roots or []) if r]
    if not roots:
        return (False, None, "没有选择要重新纳入的槽")

    try:
        cfg = OPS.read_config(config_path)
    except Exception as e:
        return (False, None, "读 config.json 失败：%s" % e)
    recs = detached_records(cfg)
    versions = cfg.get("versions") or {}
    base = install_base_of(cfg, home, self_dir)

    added, kept, details, drop = [], [], [], []
    taken_ports = set()
    for k, v in versions.items():
        try:
            taken_ports.add(int((v or {}).get("port") or 0))
        except Exception:
            pass

    for root in roots:
        rec = None
        for r in recs:
            if norm(r.get("root") or "") == norm(root):
                rec = r
                break
        if rec is None:
            kept.append(root)
            details.append({"root": root, "errors": ["记录里没有这个槽（可能已经被纳入过）"]})
            continue

        d = {"root": root, "label": rec.get("label") or os.path.basename(root),
             "errors": [], "warnings": [], "key": ""}
        details.append(d)

        if not os.path.isdir(root):
            d["errors"].append("目录已经不在了：%s" % root)
            kept.append(root)
            continue
        scfg = dict(rec.get("config") or {})
        if not scfg.get("bin_js"):
            d["errors"].append("记录里缺槽配置（bin_js），没法重建 —— 请用「纳入管理」手工收进来")
            kept.append(root)
            continue
        port = int(rec.get("port") or scfg.get("port") or 0)
        if port and port in taken_ports:
            d["warnings"].append(
                "端口 %d 已被另一个槽占用 —— 槽里的 bat 写死了这个端口，"
                "两个槽同时启动会撞车。建议只留一个在跑，或手工改端口后重建 bat。" % port)
        # 槽键：原来的还空着就沿用，否则顺延（键只是界面上的位置，不影响磁盘）
        key = str(rec.get("key") or "")
        if not key or key in versions:
            n = 1
            while ("v%d" % n) in versions:
                n += 1
            key = "v%d" % n
        d["key"] = key
        versions[key] = scfg
        taken_ports.add(port)
        added.append(key)
        drop.append(root)
        p.on_step("[%s] 重新纳入：%s" % (key, root))

    if not added:
        msgs = ["%s：%s" % (d["root"], "；".join(d["errors"])) for d in details if d["errors"]]
        return (False, {"added": [], "details": details},
                "没有槽被重新纳入。\n\n" + "\n".join(msgs or ["（原因未知）"]))

    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        OPS.backup_config(config_path, stamp=stamp)
    except IOError as e:
        return (False, {"added": [], "details": details},
                "备份 config.json 失败，已中止（什么都没改）：%s" % e)
    try:
        cfg["versions"] = versions
        left = [r for r in recs if norm(r.get("root") or "") not in {norm(x) for x in drop}]
        if left:
            cfg[DETACHED_FIELD] = left
        else:
            cfg.pop(DETACHED_FIELD, None)
        OPS.write_config_atomic(config_path, cfg)
    except Exception as e:
        return (False, {"added": [], "details": details},
                "写 config.json 失败（磁盘未改动）：%s" % e)

    parts = ["已重新纳入：%s" % ", ".join(added)]
    parts.append("磁盘上一个字节都没动 —— 原来怎么装的还是怎么装的。")
    for d in details:
        for w in d["warnings"]:
            parts.append("  · %s：%s" % (d["key"] or d["root"], w))
    return (True, {"added": added, "details": details}, "\n".join(parts))


# ══════════════════════════════════════════════════════════════════
# 执行
# ══════════════════════════════════════════════════════════════════

def uninstall_slots(keys, config_path, home, repo, self_dir,
                    versions_root="", backup_root="", stop_fn=None,
                    progress=None, stamp=None):
    """卸载若干个槽。返回 (ok, payload, 说明)。

    stop_fn(key, port) -> 可选回调：停该槽进程（GUI 传 dsh_kill 版本，测试传 None）。
    progress: 与 dsh_install_ops.Progress 同接口。
    返回 payload = {"removed": [...], "kept_in_config": [...], "bytes": N,
                    "backup_dir": "...", "details": [...]}
    """
    p = progress or OPS.Progress()
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    keys = [k for k in (keys or []) if k]
    if not keys:
        return (False, None, "没有选择要卸载的槽")

    try:
        cfg = OPS.read_config(config_path)
    except Exception as e:
        return (False, None, "读 config.json 失败：%s" % e)
    versions = cfg.get("versions") or {}
    missing = [k for k in keys if k not in versions]
    if missing:
        return (False, None, "config.json 里没有这些槽：%s" % ", ".join(missing))

    base = install_base_of(cfg, home, self_dir)
    backup_root = backup_root or os.path.join(base, "dsh备份")

    p.on_step("备份 config.json…")
    try:
        OPS.backup_config(config_path, stamp=stamp)
    except IOError as e:
        return (False, None, str(e))

    removed, kept, details = [], [], []
    total_bytes = 0

    for key in keys:
        scfg = versions.get(key) or {}
        plan = plan_slot(key, scfg, base, home, repo, self_dir, versions_root)
        port = plan["port"]
        d = {"key": key, "label": plan["label"], "port": port,
             "root": plan["root"], "temp_dir": plan["temp_dir"],
             "bytes": 0, "errors": [], "warnings": [],
             "stopped": False}
        details.append(d)

        if plan["blocked"]:
            d["errors"].append(plan["blocked"])
            kept.append(key)
            p.on_step("[%s] 跳过：%s" % (key, plan["blocked"]))
            continue

        # 1) 停进程。失败只记 warning 不阻断：
        #    进程可能本来就没在跑（正常），或者停不掉但文件仍可删。
        #    若真被占用，下面的删除会失败 —— 那时才算错误。
        if stop_fn:
            p.on_step("[%s] 停止端口 %s 上的 dsh（门控：只杀 dsh）…" % (key, port))
            try:
                stop_fn(key, port)
                d["stopped"] = True
            except Exception as e:
                d["warnings"].append("停进程失败（继续尝试删除）：%s" % e)

        # 2) 备份用户数据 + 插件清单 + 版本信息（**在删任何东西之前**）
        #    2026-10-30 起备份内容扩展：除了 .dsh-home（会话/配置/凭据/插件配置），
        #    还留一份槽根的 package.json / package-lock.json（用户装过哪些插件、
        #    钉的是哪个版本全在里面）和 _meta.json（槽名/实装版本/端口/时间）。
        p.on_step("[%s] 备份用户数据与插件清单…" % key)
        try:
            dst, vfy = _backup_slot(plan, key, backup_root, stamp)
            d["backup"] = dst
            d["verify"] = vfy
        except Exception as e:
            # 备份失败就不删程序 —— 用户数据没保险，宁可中止这个槽
            d["errors"].append("备份失败（已中止该槽，未删除任何文件）：%s" % e)
            kept.append(key)
            p.on_step("[%s] 中止：备份失败 %s" % (key, e))
            continue
        if d["backup"]:
            p.on_step("[%s] 已备份并核对通过：%d 个文件 → %s"
                      % (key, vfy.get("checked", 0), d["backup"]))
            # 大小不一致 = 备份过程中源文件被改过（多半是 dsh 没停干净）。
            # 文件都在，不阻断；但必须让用户看见，否则"备份完整"是假的。
            if vfy.get("size_changed"):
                msg = ("备份期间有 %d 个文件大小变了（该槽可能仍在运行，"
                       "备份的是当时那一刻的内容）" % vfy["size_changed"])
                d["warnings"].append(msg)
                p.on_step("[%s] 注意：%s" % (key, msg))

        # 3) 删程序产物 + 用户数据（备份已成功，空壳不再保留）
        if plan["exists"]:
            p.on_step("[%s] 删除槽目录内容（共 %s）…" % (key, human_size(plan["bytes"])))
            for ch in plan["children"]:
                if ch["action"] not in ("delete", "purge"):
                    continue
                # 逐个报进度：node_modules 里几万个文件，Windows 上删一次要几十秒，
                # 只报一句「删除程序文件…」会让人以为卡死（2026-10-29 实测踩到）。
                p.on_step("[%s] 正在删除 %s（%s）…"
                          % (key, ch["name"], human_size(ch["size"])))
                try:
                    if os.path.isdir(ch["path"]):
                        _force_rmtree(ch["path"])
                    else:
                        os.remove(ch["path"])
                    d["bytes"] += ch["size"]
                except Exception as e:
                    d["errors"].append("删除 %s 失败：%s" % (ch["name"], e))
                    # 用户数据删不掉就**立刻停手**：此时程序文件还没动，槽仍然完整可用，
                    # 用户关掉占用程序/修好权限后重试即可。继续删程序会留下
                    # 「程序没了、数据还在」的半残槽（2026-10-30 实测踩到）。
                    if ch["action"] == "purge":
                        d["errors"].append(
                            "已停止删除该槽的其余部分（程序文件保持完整，可修好后重试）")
                        p.on_step("[%s] 中止删除：%s 删不掉" % (key, ch["name"]))
                        break

            # 4) 槽根自身：删空目录。只删**空**的，所以万一还有保留下来的项
            #    会自然失败 —— 不会误删。
            #    重试几次：Windows 刚删完几万个文件时，目录常被索引器/杀软/资源
            #    管理器短暂占住，rmdir 会失败；留下空壳会让下次装同名槽变成 dshV1-2。
            if plan.get("purge_root") and not d["errors"]:
                last = None
                for _ in range(3):
                    try:
                        os.rmdir(plan["root"])
                        d["purged_root"] = True
                        break
                    except OSError as e:
                        last = e
                        time.sleep(0.4)
                if not d["purged_root"]:
                    d["warnings"].append(
                        "槽目录没能整个删掉（%s）：%s —— 下次装同一槽可能会被命名成 %s-2"
                        % (plan["root"], last, os.path.basename(plan["root"])))
                    p.on_step("[%s] 提示：槽目录没能整个删掉（%s），下次装同名槽会复用或改名"
                              % (key, last))

        total_bytes += d["bytes"]

        # 4) 只有「程序文件都清干净了」才从配置里移除，否则保留重试入口
        if d["errors"]:
            kept.append(key)
        else:
            removed.append(key)

    # 5) 写回 config.json（只移除成功的那些）
    if removed:
        p.on_step("更新 config.json（移除 %s）…" % ", ".join(removed))
        try:
            cfg = OPS.read_config(config_path)
            for k in removed:
                (cfg.get("versions") or {}).pop(k, None)
            OPS.write_config_atomic(config_path, cfg)
        except Exception as e:
            return (False, {"details": details}, "删除已完成，但写 config.json 失败：%s\n"
                                                 "请手动从 config.json 里删掉 %s"
                    % (e, ", ".join(removed)))

    payload = {
        "removed": removed,
        "kept_in_config": kept,
        "bytes": total_bytes,
        "backup_dir": backup_root,
        "details": details,
    }
    if not removed:
        msgs = []
        for d in details:
            if d["errors"]:
                msgs.append("%s：%s" % (d["key"], "；".join(d["errors"])))
        return (False, payload, "没有槽被卸载。\n\n" + "\n".join(msgs or ["（原因未知）"]))

    parts = ["已卸载：%s（释放 %s）" % (", ".join(removed), human_size(total_bytes))]
    if kept:
        parts.append("保留在配置里（未完全清理，可重试）：%s" % ", ".join(kept))
    parts.append("备份（用户数据 + 插件清单 + 版本信息）在：%s" % backup_root)
    for d in details:
        if d.get("backup"):
            parts.append("  · %s → %s" % (d["key"], d["backup"]))
    # 槽目录现在整个删掉了（含 .dsh-home/.tmp），所以下次装同一槽不会再变成
    # `dshV4-2`。万一某个槽根没删成（被占用等），如实说出来 —— 那才会导致 -2 后缀。
    leftover = [d["root"] for d in details
                if d.get("root") and os.path.isdir(d["root"])]
    if leftover:
        parts.append("注意：这些槽目录没能整个删掉：%s\n"
                     "下次往同一槽装版本可能被命名成带 -2 后缀的目录，"
                     "确认上面的备份无误后可手动删掉它们。" % "、".join(leftover))
    # 槽外临时目录（<临时目录根>\<槽>）不归槽所有，不动它，但要说一声免得残留
    ext = []
    for d in details:
        t = d.get("temp_dir") or ""
        r = d.get("root") or ""
        if t and os.path.isdir(t) and not (r and is_under(t, r)):
            ext.append(t)
    if ext:
        parts.append("槽外的临时目录未动（不是槽的一部分，可自行清理）：%s"
                     % "、".join(ext))
    warns = []
    for d in details:
        for w in d.get("warnings") or []:
            warns.append("%s：%s" % (d["key"], w))
    if warns:
        parts.append("注意：\n  " + "\n  ".join(warns))
    return (True, payload, "\n".join(parts))
