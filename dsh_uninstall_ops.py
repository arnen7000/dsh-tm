# -*- coding: utf-8 -*-
"""Plan and execute slot removal for the manager and Windows PowerShell entry.

Both entry points verify backups, protect other slots, and preserve failed slots
in the configuration. The manager removes backed-up slot data with the slot.
The PowerShell entry retains user data, the temporary directory, and Node.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

# 与 dsh_install_ops 共用同一套配置读写（原子写、备份），不重复实现。
import dsh_install_ops as OPS
import dsh_platform as PLATFORM
from dsh_adopt import existing_slot_node
from dsh_i18n import T


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
    npm_global = PLATFORM.probe_npm_global() if isinstance(p, str) and "<npm_global>" in p else ""
    return PLATFORM.resolve_path(p, base, home, self_dir, npm_global)


def install_base_of(cfg, home, self_dir):
    cfg = cfg or {}
    raw = str(cfg.get("install_base") or
              ("<appsupport>/DSH-TM" if PLATFORM.IS_MAC else "<home>"))
    if (PLATFORM.IS_MAC and home == os.path.expanduser("~")
            and raw in ("<appsupport>/DSH-TM", os.path.join(PLATFORM.app_support(home), "DSH-TM"))):
        return PLATFORM.config_base(cfg)
    return PLATFORM.resolve_path(raw, home=home, self_dir=self_dir)


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
# 图形界面和 PowerShell 入口共用的删除护栏
# ══════════════════════════════════════════════════════════════════

def is_safe_to_delete(path, slot_root_dir, keep_paths, home, repo):
    """能否删除 path。返回 (bool, 原因)。

    规则（缺一不可）：
      a) path 存在，且不是 keep 列表里的任何一个（用户数据 / 临时目录）；
      b) 严格位于 slot_root 之内（槽根本身不能删）；
      c) 不等于用户主目录 / 仓库目录 / 盘符根；
      d) 不是 home 或 repo 的**上级**（防止配置写错把整片目录端掉）。
    """
    if not path or not os.path.lexists(path):
        return (False, T("不存在"))
    if not slot_root_dir:
        return (False, T("反推不出槽根目录，拒绝删除"))
    if not _path_within(path, slot_root_dir, strict=True):
        return (False, T("不在槽根目录之内"))
    try:
        _assert_unlinked_path(os.path.dirname(path))
    except OSError as exc:
        return (False, str(exc))
    for protected in keep_paths or []:
        if protected and _paths_overlap(path, protected):
            return (False, T("用户数据/临时目录，保留"))
    for prot, why in ((home, T("用户主目录")), (repo, T("管理器目录"))):
        if prot and _path_within(prot, path):
            return (False, why)
    return (True, "")


def _has_parent_reference(path):
    return ".." in os.fspath(path).replace(os.altsep or os.sep, os.sep).split(os.sep)


def _native_path(path):
    path = os.path.abspath(path)
    if sys.platform == "darwin":
        for alias in ("/tmp", "/var", "/etc"):
            if path == alias or path.startswith(alias + "/"):
                target = "/private" + alias
                if os.path.islink(alias) and os.readlink(alias) in (target, target[1:]):
                    return target + path[len(alias):]
    return path


def _path_relative_to(path, root):
    """Return the spelling below root, with a filesystem check for macOS aliases."""
    if not path or not root or _has_parent_reference(path) or _has_parent_reference(root):
        return None
    path, root = _native_path(path), _native_path(root)
    try:
        if os.path.normcase(os.path.commonpath((path, root))) == os.path.normcase(root):
            return os.path.relpath(path, root)
    except ValueError:
        return None  # Windows paths on different drives.

    if sys.platform != "darwin":
        return None
    parts = Path(path).parts
    length = len(Path(root).parts)
    if len(parts) < length:
        return None
    ancestor = os.path.join(*parts[:length])
    try:
        _assert_unlinked_path(os.path.dirname(ancestor))
        _assert_unlinked_path(os.path.dirname(root))
        # Inspect the directory entries themselves. A symlink pointing at root
        # does not establish containment. Match aliases only if the volume does.
        if os.path.samestat(os.lstat(ancestor), os.lstat(root)):
            return os.path.join(*parts[length:]) if len(parts) > length else "."
    except OSError:
        pass
    return None


def _path_within(path, root, strict=False):
    relative = _path_relative_to(path, root)
    return relative is not None and (not strict or relative != ".")


def _paths_overlap(first, second):
    return _path_within(first, second) or _path_within(second, first)


def _stat_is_link(st):
    return stat.S_ISLNK(st.st_mode) or bool(
        getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _is_reparse_path(path):
    """Recognize symbolic links and Windows reparse points without following them."""
    try:
        return _stat_is_link(os.lstat(path))
    except (FileNotFoundError, NotADirectoryError):
        return False


def _assert_unlinked_path(path):
    """Reject traversal through links, including ancestors of a slot root."""
    if _has_parent_reference(path):
        raise OSError(T("路径包含上级目录引用：%s") % path)
    path = _native_path(path)
    for part in reversed((path, *_path_parents(path))):
        if _is_reparse_path(part):
            raise OSError(T("路径经过符号链接或重解析点：%s") % part)


def _path_parents(path):
    while (parent := os.path.dirname(path)) != path:
        yield parent
        path = parent


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


def _collect_links(src, failures):
    """列出 src 下所有 junction / 符号链接，返回 [(相对路径, 目标)]。

    只读目录项、不读文件内容，1.5 万文件约 0.2 秒。"""
    out = []
    stack = [src]
    while stack:
        s = stack.pop()
        try:
            with os.scandir(s) as entries:
                for e in entries:
                    if _is_reparse(e):
                        out.append((os.path.relpath(e.path, src), os.readlink(e.path)))
                    elif e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
        except OSError as exc:
            failures.append((os.path.relpath(s, src), str(exc)))
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
        return (False, T("找不到 robocopy"))
    cmd = [exe, src, dst, "/E", "/XJ", "/COPY:DAT", "/DCOPY:DAT",
           "/MT:8", "/R:1", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS", "/NP"]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT,
                              creationflags=getattr(subprocess,
                                                    "CREATE_NO_WINDOW", 0))
    except Exception as e:
        return (False, T("无法启动 robocopy：%s") % e)
    # robocopy 的退出码是**位标志**：0–7 都是成功（0=无变化，1=有拷贝，2=有额外项…），
    # >=8 才是真失败。按"非 0 即失败"判断会把正常备份当成错误。
    if proc.returncode < 8:
        return (True, "")
    return (False, T("robocopy 退出码 %d") % proc.returncode)


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
    fails = failures if failures is not None else []
    skipped = _collect_links(src, fails)

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
            fails.append((os.path.relpath(s, src) or ".", T("读不到目录：%s") % e))
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
    return _file_inventory(root, lambda entry: entry.stat(follow_symlinks=False).st_size)


def _file_inventory(root, fingerprint):
    """List regular files without following links; propagate enumeration errors."""
    _assert_unlinked_path(root)
    out = {}
    stack = [root]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if _is_reparse(entry):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    out[os.path.relpath(entry.path, root)] = fingerprint(entry)
                else:
                    raise OSError(T("无法备份此文件类型：%s") % entry.path)
    return out


def _file_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.digest()


def _backup_required_file(path, stamp, folder=None, label=None):
    _assert_unlinked_path(path)
    prefix = label or os.path.basename(path) + ".bak"
    folder = folder or os.path.dirname(os.path.abspath(path))
    _assert_unlinked_path(folder)
    os.makedirs(folder, exist_ok=True)
    destination = os.path.join(folder, prefix + "-" + stamp)
    try:
        _copy_new_file(path, destination)
    except FileExistsError:
        fd, destination = tempfile.mkstemp(prefix=prefix + "-" + stamp + "-", dir=folder)
        os.close(fd)
        try:
            shutil.copyfile(path, destination)
        except OSError:
            os.unlink(destination)
            raise
    if _file_digest(destination) != _file_digest(path):
        os.unlink(destination)
        raise OSError(T("备份内容与原文件不一致：%s") % path)
    return destination


def verify_backup(src, dst):
    """Return (missing, mismatched, total) after comparing file contents."""
    source = _file_inventory(src, lambda entry: _file_digest(entry.path))
    backup = _file_inventory(dst, lambda entry: _file_digest(entry.path))
    missing = [path for path in source if path not in backup]
    changed = [path for path in source if path in backup and source[path] != backup[path]]
    return missing, changed, len(source)


def _force_rmtree(path):
    """Remove the entry and its contents without following or chmod'ing links."""
    _assert_unlinked_path(os.path.dirname(path))
    st = os.lstat(path)

    def remove(func, entry, entry_stat):
        try:
            func(entry)
        except PermissionError:
            if (os.name != "nt" or entry_stat.st_nlink > 1
                    or _is_reparse_path(entry)):
                raise
            # Unlike os.chmod on Windows Python < 3.13, SetFileAttributesW
            # changes attributes on the entry itself if it becomes a symlink.
            import ctypes
            attributes = entry_stat.st_file_attributes
            if not attributes & stat.FILE_ATTRIBUTE_READONLY:
                raise
            attributes &= ~stat.FILE_ATTRIBUTE_READONLY
            if not ctypes.windll.kernel32.SetFileAttributesW(
                    str(entry), attributes or stat.FILE_ATTRIBUTE_NORMAL):
                raise
            func(entry)

    def walk(entry, entry_stat):
        if _stat_is_link(entry_stat):
            if (os.name == "nt"
                    and entry_stat.st_file_attributes & stat.FILE_ATTRIBUTE_DIRECTORY):
                os.rmdir(entry)
            else:
                os.unlink(entry)
        elif stat.S_ISDIR(entry_stat.st_mode):
            with os.scandir(entry) as children:
                paths = [child.path for child in children]
            for child in paths:
                walk(child, os.lstat(child))
            remove(os.rmdir, entry, entry_stat)
        else:
            remove(os.unlink, entry, entry_stat)

    if os.name == "nt" or _stat_is_link(st):
        walk(path, st)
    elif stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def mark_incomplete(dirpath, kind, total, missing, fails):
    """在「不完整的备份」目录里留一个显眼的 `_INCOMPLETE.txt`。

    为什么需要（2026-10-31）：正常情况会把目录改名成 `…-INCOMPLETE`，一眼就能看出
    是废的。但改名本身可能失败（目录被索引器/杀软占住、权限不足），此时目录会保留
    **正常的名字** —— 那正是最危险的状态：用户以为数据已经安全了。
    标记文件是这个兜底：只要打开目录就能看到为什么不能删原数据。

    返回 True 表示标记写成功（写不进去也不算致命，调用方已经在抛错了）。
    """
    lines = [
        T("这份备份【不完整】，不要拿它当后悔药。"),
        "",
        T("类型：%s") % (kind or T("备份")),
        T("时间：%s") % time.strftime("%Y-%m-%d %H:%M:%S"),
        T("应备份 %d 个文件，缺 %d 个，拷不动 %d 个。") % (
            total or 0, len(missing or []), len(fails or [])),
        "",
    ]
    if missing:
        lines.append(T("缺失（源里有、这里没有）："))
        for p in list(missing)[:20]:
            lines.append("  - %s" % p)
        if len(missing) > 20:
            lines.append(T("  … 还有 %d 个") % (len(missing) - 20))
        lines.append("")
    if fails:
        lines.append(T("拷不动（被占用 / 权限不足）："))
        for p, why in list(fails)[:20]:
            lines.append("  - %s —— %s" % (p, why))
        if len(fails) > 20:
            lines.append(T("  … 还有 %d 个") % (len(fails) - 20))
        lines.append("")
    lines.append(T("原目录一个文件都没删。处理好占用/权限后重试即可。"))
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

    **`.dsh-home` 为空（从没启动/对话过）时返回 ("", …)，一个目录都不建**
    —— 2026-11-14 用户反馈「我都没在 v1 上对话过，怎么多出一个备份目录」。
    """
    home = plan.get("dsh_home") or ""

    # 2026-11-14 用户反馈：「我都没在 v1 上对话过，怎么多出一个
    # v1-userdata-20261114-095110？」—— 那个目录里 0 个用户文件，只有
    # _meta.json + package-lock（440 KB）。**没有用户数据就没有要救的东西**，
    # 建一个空备份目录只会让「dsh备份」越堆越乱。现在先探一次：.dsh-home
    # 一个条目都没有就完全不建，只在日志里留一行（含实装版本）。
    _has_user = False
    if home:
        _assert_unlinked_path(home)
        try:
            with os.scandir(home) as entries:
                _has_user = next(entries, None) is not None
        except FileNotFoundError:
            pass
    if not _has_user:
        return ("", {"checked": 0, "missing": 0, "size_changed": 0,
                     "copy_failed": 0, "skipped_empty": True})

    _assert_unlinked_path(backup_root)
    if any(_paths_overlap(backup_root, path) for path in (home, plan.get("root"))):
        raise OSError(T("备份目录和源目录重叠：%s") % backup_root)
    os.makedirs(backup_root, exist_ok=True)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        raise OSError(T("无效的槽键：%s") % key)
    prefix = "%s-userdata-%s" % (key, stamp)
    dst = os.path.join(backup_root, prefix)
    try:
        os.mkdir(dst)
    except FileExistsError:
        dst = tempfile.mkdtemp(prefix=prefix + "-", dir=backup_root)

    links = []
    fails = []
    verify = {"checked": 0, "missing": 0, "size_changed": 0, "copy_failed": 0}
    if home and os.path.isdir(home):
        missing, changed, total = [], [], 0
        try:
            links = _copy_user_data(home, dst, fails)
            missing, changed, total = verify_backup(home, dst)
        except OSError as exc:
            fails.append((".", str(exc)))
        verify = {"checked": total, "missing": len(missing),
                  "size_changed": len(changed), "copy_failed": len(fails)}
        if missing or changed or fails:
            fails.extend((path, T("备份内容与原文件不一致")) for path in changed)
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
            mark_incomplete(bad, T("卸载备份"), total, missing, fails)
            raise IOError(
                T("备份不完整，已中止（槽目录一个文件都没删）：\n"
                "  · 应备份 %d 个文件，缺 %d 个%s\n"
                "  · 拷不动 %d 个%s\n"
                "常见原因：该槽的 dsh 还在运行（锁住了 .dsh-home 里的文件）、"
                "或杀软正在扫描。请先停止该槽再重试。\n"
                "已拷出的部分留在：%s")
                % (total, len(missing),
                   (T("（例：%s）") % missing[0]) if missing else "",
                   len(fails),
                   (T("（例：%s —— %s）") % fails[0]) if fails else "",
                   bad))
    # Backups contain user-owned names. Metadata must not overwrite their files.
    metadata = dst
    reserved = ("_links.json", "_slot-package.json", "_slot-package-lock.json", "_meta.json")
    if any(os.path.lexists(os.path.join(dst, name)) for name in reserved):
        metadata = tempfile.mkdtemp(prefix="_dsh-backup-metadata-", dir=dst)
    if links:
        with open(os.path.join(metadata, "_links.json"), "x",
                  encoding="utf-8", newline="\n") as f:
            json.dump([{"path": path, "target": target} for path, target in links],
                      f, ensure_ascii=False, indent=2)

    root = plan.get("root") or ""
    for src_name, dst_name in (("package.json", "_slot-package.json"),
                               ("package-lock.json", "_slot-package-lock.json")):
        src = os.path.join(root, src_name)
        if os.path.isfile(src) and not _is_reparse_path(src):
            shutil.copy2(src, os.path.join(metadata, dst_name))

    meta = {
        "slot_key": key,
        "label": plan.get("label"),
        "root": root,
        "installed_version": _installed_version(root),
        "port": plan.get("port"),
        "dsh_home": home,
        "temp_dir": plan.get("temp_dir"),
        "backed_up_at": stamp,
        "note": T("dsh 版本管理器卸载前自动备份。"
                ".dsh-home 内容在本目录；插件清单见 _slot-package*.json。"
                "junction/符号链接**未复制**（它们指向槽的 node_modules 或槽外目录，"
                "不是用户数据），清单见 _links.json。"),
        "skipped_links": len(links),
        "verify": verify,
        "config_snippet": plan.get("config") or {},
    }
    with open(os.path.join(metadata, "_meta.json"), "x",
              encoding="utf-8", newline="\n") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return dst, verify


# ══════════════════════════════════════════════════════════════════
# 规划（纯计算，不写盘 —— 界面先用它把「会删什么」摆给用户看）
# ══════════════════════════════════════════════════════════════════

# 一个「标准槽」应该有的条目。其余一律算「用户自己放进去的」——
# 卸载会删整个槽目录，这些也会一起没，所以必须点名（2026-11-14）。
_SLOT_KNOWN = {"node_modules", "node", ".dsh-home", ".tmp",
               "dsh.bat", "dsh-cli.bat", "dsh.command", "dsh-cli.command",
               "package.json", "package-lock.json"}


def slot_foreign_entries(root):
    """槽目录里**不是 dsh 装出来的**条目名（用户自己放的笔记/插件/备份等）。

    只读一次 listdir，不统计体积 —— 确认框里要用，不能卡。
    """
    if not root or not os.path.isdir(root):
        return []
    out = []
    try:
        for name in sorted(os.listdir(root)):
            low = name.lower()
            if low in _SLOT_KNOWN:
                continue
            if re.match(r"^kill-\d+\.(bat|command)$", low):     # 端口变了会换文件名
                continue
            out.append(name)
    except OSError:
        return []
    return out


def plan_slot(key, cfg, base, home, repo, self_dir, versions_root="",
              protected_paths=(), retain_runtime=False, allow_global=False):
    """算出一个槽的卸载计划。

    返回 dict：
      key/label/port/root/dsh_home/temp_dir/exists
      children: [{path, name, action: 'delete'|'keep', reason, size}]
      bytes:    将释放的字节数
      blocked:  非空 = 这个槽不能卸（原因）
    """
    cfg = cfg or {}
    root = slot_root(cfg, base, home, self_dir)
    default_home = os.path.join(root, ".dsh-home") if root else ""
    dsh_home = resolve(cfg.get("dsh_home") or "", base, home, self_dir) or default_home
    temp_dir = resolve(cfg.get("temp_dir") or "", base, home, self_dir)
    keep = [dsh_home, temp_dir, os.path.join(root, ".tmp"),
            os.path.join(root, "node")] if retain_runtime else []
    plan = {
        "key": key,
        "label": cfg.get("label") or key,
        "port": cfg.get("port"),
        "root": root,
        "dsh_home": dsh_home,
        "temp_dir": temp_dir,
        "exists": False,
        "children": [],
        "bytes": 0,
        "blocked": "",
        "config": dict(cfg),          # 原始片段：备份的 _meta.json 里照抄，便于手工恢复
    }

    if not root:
        plan["blocked"] = T("反推不出槽根目录（bin_js 不在本地 node_modules 布局里）")
        return plan
    # 全局安装：先于「目录是否存在」判断 —— 否则一个不存在的全局路径会被当成
    # 「目录已不在，直接移出配置」，用户会以为卸干净了，实际全局包还在。
    if is_global_install(cfg, base, home, self_dir):
        plan["global_install"] = True
        if not allow_global:
            plan["blocked"] = T("这是 npm 全局安装，不能按槽删除。")
    else:
        versions_root = versions_root or os.path.join(base, "dsh-versions")
        if not _path_within(root, versions_root, strict=True):
            plan["blocked"] = T("槽根目录不在版本根之内（%s 不在 %s 下），拒绝删除") % (root, versions_root)
            return plan
    for path in (home, repo):
        if _path_within(path, root):
            plan["blocked"] = T("槽根目录是系统/用户/仓库目录，拒绝删除：%s") % root
            return plan
    for path in protected_paths:
        if path and _paths_overlap(root, path):
            plan["blocked"] = T("槽目录与受保护的路径重叠：%s") % path
            return plan
    if plan.get("global_install"):
        return plan
    try:
        _assert_unlinked_path(root)
        if os.path.lexists(root) and not os.path.isdir(root):
            raise OSError(T("槽根路径不是目录：%s") % root)
    except OSError as exc:
        plan["blocked"] = str(exc)
        return plan
    for path in (dsh_home, temp_dir):
        if path and (_has_parent_reference(path) or _path_within(root, path)):
            plan["blocked"] = T("用户数据或临时目录配置无效：%s") % path
            return plan
    if not _path_within(default_home, dsh_home) and os.path.lexists(default_home):
        keep.append(default_home)
    plan["exists"] = os.path.isdir(root)
    if not plan["exists"]:
        # The GUI cannot tell a removed directory from a stale registered path.
        # Keep the entry available for repair or explicit detachment.
        if not retain_runtime:
            plan["blocked"] = T("槽根目录不存在：%s") % root
        return plan

    try:
        names = sorted(os.listdir(root))
    except Exception as e:
        plan["blocked"] = T("列不出槽根目录内容：%s") % e
        return plan

    for name in names:
        p = os.path.join(root, name)
        # 2026-10-30 起：用户数据/临时目录也**要删**（备份成功后），否则会留下
        # `.dsh-home`+`.tmp` 的空壳目录，下次往同一槽装版本时 unique_dir 认为
        # 名字被占 → 装出 `dshV4-2` 这种名字，用户一看就觉得「上次没卸干净」。
        # 所以这里把 keep 列表传空跑护栏（护栏的其它条件一条不少），
        # 再单独标成 purge 让执行阶段知道"这些是先备份后删"。
        is_userdata = any(_path_within(path, p) for path in (dsh_home, temp_dir))
        ok, why = is_safe_to_delete(p, root, keep, home, repo)
        if ok:
            entry_stat = os.lstat(p)
            size = 0
            if not _stat_is_link(entry_stat):
                size = (_dir_size(p) if stat.S_ISDIR(entry_stat.st_mode)
                        else entry_stat.st_size)
            plan["bytes"] += size
            plan["children"].append({"path": p, "name": name,
                                     "action": "purge" if is_userdata else "delete",
                                     "reason": "", "size": size})
        else:
            plan["children"].append({"path": p, "name": name, "action": "keep",
                                     "reason": why, "size": 0})
    # 所有子项都删完后，槽根应当空掉 → 连目录本身一起收掉（rmdir 只删空目录，
    # 万一还有 keep 的项就自然失败，不会误删）。
    plan["purge_root"] = not retain_runtime and not any(
        child["action"] == "keep" for child in plan["children"])
    plan["children"].sort(key=lambda child: child["action"] != "purge")
    plan["foreign"] = slot_foreign_entries(root)
    return plan


def plan_many(cfg, keys, home, repo, self_dir, versions_root="", *,
              extra_protected=(), retain_runtime=False, allow_global=False,
              base_override=""):
    """Plan each selected slot while protecting directories owned by other slots."""
    base = base_override or install_base_of(cfg, home, self_dir)
    versions = (cfg or {}).get("versions") or {}
    detached = [record.get("root") for record in detached_records(cfg)]
    plans = []
    for key in keys:
        if key not in versions:
            continue
        protected = [*detached, *extra_protected]
        for other_key, other in versions.items():
            if other_key != key:
                protected.append(slot_root(other, base, home, self_dir))
                for field in ("dsh_home", "temp_dir", "node_exe", "bin_js"):
                    protected.append(resolve(other.get(field) or "", base, home, self_dir))
        plans.append(plan_slot(key, versions[key], base, home, repo, self_dir,
                               versions_root, protected, retain_runtime, allow_global))
    return plans


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
# 留档（config 顶层 `detached`）只记**绝对路径**，别的什么都不记。
#
# 为什么要留：换过安装根之后，旧位置的槽**不在** `dsh_adopt.detect_existing_installs`
# 的扫描范围里（它只扫当前 versions_root 和 npm 全局），没有这条路径就再也
# 自动发现不了，用户只能手工浏览目录。
#
# 为什么**只记路径**（2026-11-13 重写）：旧版还把 label / port / 整份槽配置
# 一起存进去，重新纳入时原样放回。结果只要槽键变了（比如 v3 被重装占用），
# 就会出现「槽 v4 顶着 dshV3 的名字、和 v3 抢 6080 端口、三个路径还都指向
# dshV3」的幽灵槽 —— 因为那些字段描述的是**当时**那个槽，不是现在这个键。
# 现在一切以磁盘为准：键从目录名推、端口从 bat 里读、路径按实际位置算。
# ══════════════════════════════════════════════════════════════════

DETACHED_FIELD = "detached"

# 槽目录名 <-> 槽键 的一一对应：dshV3 <-> v3，dshV3-2 <-> v3-2。
_SLOT_NAME_RE = re.compile(r"^dshV(\d+)(?:-(\d+))?$", re.IGNORECASE)


def detached_records(cfg):
    """config 顶层 `detached` 的安全读取：不是 list / 缺字段一律当空。"""
    raw = (cfg or {}).get(DETACHED_FIELD)
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict) and (r.get("root") or r.get("config"))]


def slot_key_from_dir(name):
    """目录名 -> 槽键：`dshV3` -> `v3`，`dshV3-2` -> `v3-2`；认不出返回 ''。

    槽键与目录名必须是**一一对应**的：卡片标题显示的是目录名，配置里记的是
    槽键，两者一旦能各自漂移，就会出现「槽 v4 显示成 dshV3」这种自相矛盾的界面。
    """
    name = str(name or "").replace(chr(92), "/").rstrip("/").rsplit("/", 1)[-1]
    m = _SLOT_NAME_RE.match(name)
    if not m:
        return ""
    return "v" + m.group(1) + (("-" + m.group(2)) if m.group(2) else "")


def port_from_bats(root):
    """从槽里已有的三件 bat 解析 `--port N`。解析不到返回 0。

    为什么读 bat 而不是读记录：移出之后 config 里已经没有这个槽了，bat 是
    **磁盘上唯一还记着端口**的东西（而且它就是实际启动时用的那个值）。
    """
    for fn in ("dsh.bat", "start.bat", "kill.bat"):
        p = os.path.join(root, fn)
        try:
            with open(p, "rb") as f:
                raw = f.read()
        except (IOError, OSError):
            continue
        for enc in ("gbk", "utf-8", "latin-1"):
            try:
                txt = raw.decode(enc)
            except Exception:
                continue
            m = re.search(r"--port\s+(\d{2,5})", txt)
            if m:
                return int(m.group(1))
    # 兜底：kill-<port>.bat 的文件名里带端口
    try:
        for name in os.listdir(root):
            m = re.match(r"^kill-(\d{2,5})\.bat$", name, re.IGNORECASE)
            if m:
                return int(m.group(1))
    except OSError:
        pass
    return 0


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
        return (False, None, T("没有选择要移出的槽"))

    try:
        cfg = OPS.read_config(config_path)
    except Exception as e:
        return (False, None, T("读 config.json 失败：%s") % e)
    versions = cfg.get("versions") or {}
    missing = [k for k in keys if k not in versions]
    if missing:
        return (False, None, T("config.json 里没有这些槽：%s") % ", ".join(missing))

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
            d["warnings"].append(T("反推不出槽根目录，记录里没有路径；重新纳入时要手工指定"))
        if running_fn:
            try:
                if running_fn(key, plan["port"]):
                    d["errors"].append(
                        T("这个槽正在运行，请先点「停止」再移出 —— "
                        "移出后管理器就不再跟踪它的进程了"))
                    kept.append(key)
                    p.on_step(T("[%s] 跳过：正在运行") % key)
                    continue
            except Exception:
                pass    # 判不了就当没在跑：移出不破坏任何东西

        d["version"] = _installed_version(root) if root else ""
        # 统一成反斜杠：`<base>` 展开后是 `<home>` 的绝对形式（反斜杠），而配置里
        # 其余部分写的是 `/`，拼出来就是 `<home>/dsh-versions/dshV1` 这种混写。
        # Windows 认，但记录里看着像坏的。
        rec_root = os.path.normpath(root) if root else ""
        # 只记路径（2026-11-13）：label / port / 配置快照一律不存 ——
        # 它们描述的是"当时那个槽"，重新纳入时槽键可能已经变了，照抄就会造出
        # 与现役槽同名、同端口的幽灵槽。接回时全部从磁盘重新读。
        rec = {
            "root": rec_root,                   # 绝对路径：这是「它现在在哪」的事实
            "at": stamp,
        }
        # 同一目录只留一条（移出→重新纳入→再移出不该堆出两条）
        if root:
            recs = [r for r in recs
                    if norm(r.get("root") or "") != norm(root)]
        new_recs.append(rec)
        moved.append(key)
        p.on_step(T("[%s] 移出管理：%s（磁盘未改动）") % (key, root or plan["label"]))

    if not moved:
        msgs = ["%s：%s" % (d["key"], "；".join(d["errors"])) for d in details if d["errors"]]
        return (False, {"moved": [], "kept_in_config": kept, "details": details},
                T("没有槽被移出。\n\n") + "\n".join(msgs or [T("（原因未知）")]))

    # 先备份 config 再写：移出是「只改配置」，配置写坏了就全丢
    try:
        OPS.backup_config(config_path, stamp=stamp)
    except IOError as e:
        return (False, {"moved": [], "kept_in_config": keys, "details": details},
                T("备份 config.json 失败，已中止（什么都没改）：%s") % e)

    try:
        cfg = OPS.read_config(config_path)
        for k in moved:
            (cfg.get("versions") or {}).pop(k, None)
        if recs or new_recs:
            cfg[DETACHED_FIELD] = recs + new_recs
        OPS.write_config_atomic(config_path, cfg)
    except Exception as e:
        return (False, {"moved": [], "kept_in_config": keys, "details": details},
                T("写 config.json 失败（磁盘上的东西一个都没动，配置可能仍是原样，"
                "请检查）：%s") % e)

    payload = {"moved": moved, "kept_in_config": kept, "details": details,
               "records": new_recs, "stamp": stamp}
    parts = [T("已移出管理：%s") % ", ".join(moved)]
    parts.append(T("磁盘上一个字节都没动 —— 这些槽仍然可以用它们自己的启动脚本启动。"))
    if kept:
        parts.append(T("没移出的（仍在管理里）：%s") % ", ".join(kept))
    for d in details:
        for w in d["warnings"]:
            parts.append("  · %s：%s" % (d["key"], w))
    if not (cfg.get("versions") or {}):
        parts.append(T("现在管理器不管理任何版本了 —— 安装根目录已解锁，"
                     "可以在「设置 → 目录」里换到别的位置，再装/纳入新的 dsh。"))
    return (True, payload, "\n".join(parts))


def _copy_new_file(source, destination):
    # Exclusive creation also preserves an existing file if another repair
    # completed after the initial existence check.
    with open(source, "rb") as incoming:
        with open(destination, "xb") as outgoing:
            try:
                shutil.copyfileobj(incoming, outgoing)
                shutil.copystat(source, destination)
            except OSError:
                outgoing.close()
                os.unlink(destination)
                raise


def repair_slot(root, node_src=""):
    """Restore missing runtime and state directories, preserving existing files.

    启动脚本由管理器以「单槽 + 只补缺失」模式补回。
    """
    actions = []
    if not root or not os.path.isdir(root):
        return (False, actions, T("目录不存在：%s") % (root or T("(空)")))
    try:
        _assert_unlinked_path(root)
    except OSError as exc:
        return (False, actions, str(exc))
    root = os.path.abspath(root)

    bin_js = os.path.join(root, "node_modules", "@deepseek-ai", "dsh", "lib", "bin.js")
    if not os.path.isfile(bin_js):
        return (False, actions,
                T("缺 lib/bin.js —— 这是程序本体，补不了（等于重装）。\n"
                "请删掉这个目录后用「安装官方版 dsh」重装一份。"))

    if not existing_slot_node(root):
        if not (node_src and os.path.isfile(node_src)):
            return (False, actions,
                    T("缺 node 运行时，而且没有可复制的来源（先装/保留一个别的槽）。"))
        target = PLATFORM.slot_node_path(root)
        try:
            _assert_unlinked_path(os.path.dirname(target))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            _copy_new_file(node_src, target)
            actions.append(T("补上 node 运行时（从 %s 复制）") % node_src)
        except OSError as e:
            return (False, actions, T("复制 node 失败：%s") % e)

    for sub in (".dsh-home", ".tmp"):
        p = os.path.join(root, sub)
        if not os.path.isdir(p):
            try:
                os.mkdir(p)
                actions.append(T("补建 %s 目录") % sub)
            except Exception as e:
                return (False, actions, T("创建 %s 失败：%s") % (sub, e))

    # 补完再体检一次：缺依赖 / 缺包 package.json 这类**补不了**的，不能回
    # "已完成（无需改动）" —— 那会让用户以为好了，下次启动照样炸（2026-11-14）。
    try:
        import dsh_adopt as _A
        _info = _A.inspect_adopt_source(
            os.path.join(root, "node_modules")) or {}
    except Exception as _e:
        return (True, actions, "")          # 体检本身不可用就别误报失败
    if not _info.get("ok"):
        _why = (_info.get("problems") or [T("安装仍不完整")])[0]
        _head = (T("已补上：%s\n\n") % "、".join(actions)) if actions else ""
        return (False, actions,
                _head + T("但仍有问题补不了：%s\n\n"
                        "这一类（程序本体 / 依赖 / 包清单缺失）等于重装 —— "
                        "请删掉这个目录后用「安装官方版 dsh」重装一份。") % _why)

    return (True, actions, "")


def readd_slots(roots, config_path, home, repo, self_dir, versions_root="",
                progress=None, taken_ports=None, probe=None, max_slots=6):
    """把「已移出」的槽重新登记到 config.json。管理器负责生成脚本。

    **一切以磁盘为准**（2026-11-13 重写）：
      · 槽键从目录名推导（`dshV3` -> `v3`），不沿用记录里的键 ——
        记录里的键可能已经被别的槽占了，硬套就会造出「槽 v4 显示 dshV3」。
      · 端口从槽里已有的 bat 读（`--port N`），那是实际启动用的值。
      · 路径按它现在的位置算，不抄记录里的旧配置。
      · 同一个目录已经在管理里 → 直接拒绝，绝不重复登记。

    端口若已被别的槽占用，**自动换一个空闲端口**并在 payload 里标出来
    （调用方据此重写三件 bat）；绝不写一个注定撞车的端口进配置。

    返回 (ok, payload, 说明)；payload 含 added / details / port_changed。
    """
    p = progress or OPS.Progress()
    roots = [os.path.normpath(os.path.abspath(r)) for r in (roots or []) if r]
    if not roots:
        return (False, None, T("没有选择要重新纳入的槽"))

    try:
        cfg = OPS.read_config(config_path)
    except Exception as e:
        return (False, None, T("读 config.json 失败：%s") % e)
    recs = detached_records(cfg)
    versions = cfg.get("versions") or {}
    base = install_base_of(cfg, home, self_dir)
    vr = versions_root or os.path.join(base, "dsh-versions")

    # 已登记的目录 -> 槽键：同一目录只能对应一个槽
    managed = {}
    for k, v in versions.items():
        r = slot_root(v or {}, base, home, self_dir)
        if r:
            managed[norm(r)] = k

    taken = set()
    for k, v in versions.items():
        try:
            taken.add(int((v or {}).get("port") or 0))
        except Exception:
            pass
    for x in (taken_ports or []):
        try:
            taken.add(int(x))
        except Exception:
            pass
    taken.discard(0)

    adopt_mod = None
    try:
        import dsh_adopt as adopt_mod          # 端口分配与安装/纳入共用一套
    except Exception:
        adopt_mod = None

    added, details, drop, port_changed = [], [], [], {}

    def _free(start):
        if adopt_mod is None:
            return None
        return adopt_mod.free_port(start, taken, probe=probe)

    for root in roots:
        d = {"root": root, "label": os.path.basename(root), "key": "",
             "errors": [], "warnings": []}
        details.append(d)

        if not os.path.isdir(root):
            d["errors"].append(T("目录已经不在了：%s") % root)
            continue
        # 2026-11-14：接回也走**同一套体检**（复制纳入早就这么做）。
        # 只查 bin.js 不够 —— 缺 package.json、缺依赖的目录接回后一启动就炸，
        # 比当场拒绝更难排查。缺什么就说什么，并给出下一步。
        try:
            import dsh_adopt as _A
            _info = _A.inspect_adopt_source(os.path.join(root, "node_modules"),
                                            in_place=True) or {}
        except Exception as _e:
            _info = {"ok": False, "problems": [T("体检失败：%s") % _e]}
        for _w in (_info.get("warnings") or []):
            # 只是提醒，不阻断：比如「没找到原来的用户数据目录（.dsh-home）」
            # —— 用户可能自己删了，接回来就是一个干净的新槽，必须让他知道。
            d["warnings"].append(_w)
        if not _info.get("ok"):
            _why = (_info.get("problems") or [T("安装不完整")])[0]
            d["errors"].append(
                T("这个安装不完整，不能接回：%s\n"
                "可以手动删掉这个目录；或用「安装官方版 dsh」装到别的编号"
                "（它占着自己的编号，不会被自动复用）。") % _why)
            continue
        node_exe = _info["node_exe"]
        if norm(root) in managed:
            d["errors"].append(T("这个目录已经在管理里（槽 %s），不需要重复纳入")
                               % managed[norm(root)])
            continue

        # ── 槽键：目录名说了算 ────────────────────────────────
        key0 = slot_key_from_dir(root)
        stale_root = os.path.join(base, "dsh-versions", os.path.basename(root))
        old_slot = versions.get(key0)
        # Recognize the precise legacy duplicate directory before assigning another key.
        if (old_slot and norm(os.path.dirname(root)) == norm(base)
                and norm(stale_root) == norm(slot_root(old_slot, base, home, self_dir))
                and not os.path.lexists(stale_root)):
            try:
                port = int(old_slot.get("port") or 0)
            except (TypeError, ValueError):
                port = 0
            if not 1 <= port <= 65535:
                d["errors"].append(T("原登记端口无效，无法修复路径：%s") % key0)
                continue
            defaults = _A.build_slot_config(
                key0, d["label"], root, os.path.join(root, ".dsh-home"),
                os.path.join(root, ".tmp"), port, node_exe, install_base=base,
            )
            fixed = {**defaults, **old_slot}
            for field in ("bin_js", "node_exe", "dsh_home", "temp_dir"):
                old_path = resolve(old_slot.get(field) or "", base, home, self_dir)
                if not old_path or is_under(old_path, stale_root):
                    fixed[field] = defaults[field]
            versions[key0] = fixed
            launcher_port = PLATFORM.read_slot_port(root) if PLATFORM.IS_MAC else port_from_bats(root)
            if launcher_port != port:
                port_changed[key0] = (launcher_port or 0, port)
            managed[norm(root)] = key0
            added.append(key0)
            drop.extend((root, stale_root))
            d["key"] = key0
            d["warnings"].append(T("已修正槽 %s 的登记路径，保留原端口 %d") % (key0, port))
            p.on_step(T("[%s] 接回：%s（端口 %d）") % (key0, root, port))
            continue
        key = key0
        if not key or key in versions:
            key = ""
            for i in range(1, int(max_slots) + 1):
                if ("v%d" % i) not in versions:
                    key = "v%d" % i
                    break
            if not key:
                d["errors"].append(T("槽位已满，没法再纳入"))
                continue
            if key0:
                d["warnings"].append(T("槽键 %s 已被别的目录占用，改用 %s") % (key0, key))
        label = d["label"] or ("dsh" + key.upper())
        # 标签必须唯一（它是卡片标题）：目录名已被别的槽占用时，改用槽键派生的名字。
        # 这正是「槽 v4 显示成 dshV3」那个 bug 的另一半 —— 光修槽键不够，
        # label 抄自旧记录一样会让两张卡片同名。
        _used = set(str((v or {}).get("label") or "").lower()
                    for v in versions.values())
        if label.lower() in _used:
            _alt = "dsh" + key.upper()
            label = _alt if _alt.lower() not in _used else "%s-%s" % (label, key)
            d["warnings"].append(T("显示名 %s 已被别的槽占用，改用 %s")
                                 % (d["label"], label))

        # ── 端口：bat 里写的是事实 ────────────────────────────
        port = (PLATFORM.read_slot_port(root) if PLATFORM.IS_MAC else port_from_bats(root))
        if port and port in taken:
            newp = _free(port + 1) or _free(4080)
            if not newp:
                d["errors"].append(T("原端口 %d 已被别的槽占用，也找不到空闲端口") % port)
                continue
            d["warnings"].append(T("原端口 %d 已被别的槽占用，改用 %d"
                                 "（启动脚本会同步更新）") % (port, newp))
            port_changed[key] = (port, newp)
            port = newp
        elif not port:
            idx = len(versions) + 1
            newp = (adopt_mod.suggest_port(idx, taken, probe=probe)
                    if adopt_mod is not None else None)
            if not newp:
                d["errors"].append(T("分配不到空闲端口"))
                continue
            d["warnings"].append(T("槽里的启动脚本没有端口信息，新分配 %d"
                                 "（启动脚本会同步更新）") % newp)
            port_changed[key] = (0, newp)
            port = newp

        # The version root may be the install base itself or live elsewhere.
        # Encode <base> relative to the same directory used to resolve it.
        versions[key] = _A.build_slot_config(
            key, label, root, os.path.join(root, ".dsh-home"), os.path.join(root, ".tmp"),
            port, node_exe, install_base=base if is_under(root, vr) else "",
        )
        taken.add(int(port))
        managed[norm(root)] = key
        added.append(key)
        drop.append(root)
        d["key"] = key
        p.on_step(T("[%s] 接回：%s（端口 %d）") % (key, root, port))

    if not added:
        msgs = ["%s：%s" % (d["root"], "；".join(d["errors"]))
                for d in details if d["errors"]]
        return (False, {"added": [], "details": details},
                T("没有槽被重新纳入。\n\n") + "\n".join(msgs or [T("（原因未知）")]))

    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        OPS.backup_config(config_path, stamp=stamp)
    except IOError as e:
        return (False, {"added": [], "details": details},
                T("备份 config.json 失败，已中止（什么都没改）：%s") % e)
    try:
        cfg["versions"] = versions
        drop_n = {norm(x) for x in drop}
        # 接回的记录删掉；顺带清掉「指向已在管目录」的陈旧记录（重装后会留下这种）
        left = []
        for r in recs:
            rn = norm(r.get("root") or "")
            if rn in drop_n or rn in managed:
                continue
            left.append(r)
        if left:
            cfg[DETACHED_FIELD] = left
        else:
            cfg.pop(DETACHED_FIELD, None)
        OPS.write_config_atomic(config_path, cfg)
    except Exception as e:
        return (False, {"added": [], "details": details},
                T("写 config.json 失败（磁盘未改动）：%s") % e)

    parts = [T("已重新纳入：%s") % ", ".join(added)]
    for d in details:
        for w in d["warnings"]:
            parts.append("  · %s：%s" % (d["key"] or d["root"], w))
    return (True, {"added": added, "details": details,
                   "port_changed": port_changed}, "\n".join(parts))


# ══════════════════════════════════════════════════════════════════
# 执行
# ══════════════════════════════════════════════════════════════════

def uninstall_slots(keys, config_path, home, repo, self_dir,
                    versions_root="", backup_root="", stop_fn=None,
                    progress=None, stamp=None, *, retain_runtime=False,
                    skip_backup=False, base_override="", global_uninstall=None):
    """卸载若干个槽。返回 (ok, payload, 说明)。

    stop_fn(key, port) -> 可选回调：停该槽进程（GUI 传 dsh_kill 版本，测试传 None）。
    progress: 与 dsh_install_ops.Progress 同接口。
    返回 payload = {"removed": [...], "kept_in_config": [...], "bytes": N,
                    "backup_dir": "...", "details": [...]}
    """
    p = progress or OPS.Progress()
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    keys = list(dict.fromkeys(k for k in (keys or []) if k))
    if not keys:
        return (False, None, T("没有选择要卸载的槽"))
    if skip_backup and not retain_runtime:
        return (False, None, T("删除用户数据前必须备份。"))

    try:
        _assert_unlinked_path(config_path)
        cfg = OPS.read_config(config_path)
    except Exception as e:
        return (False, None, T("读 config.json 失败：%s") % e)
    versions = cfg.get("versions") or {}
    missing = [k for k in keys if k not in versions]
    if missing:
        return (False, None, T("config.json 里没有这些槽：%s") % ", ".join(missing))

    base = base_override or install_base_of(cfg, home, self_dir)
    backup_root = backup_root or os.path.join(base, T("dsh备份"))

    p.on_step(T("备份 config.json…"))
    try:
        folder = backup_root if retain_runtime else None
        label = "config" if retain_runtime else None
        _backup_required_file(config_path, stamp, folder, label)
    except IOError as e:
        return (False, None, str(e))

    removed, kept, details = [], [], []
    total_bytes = 0

    plans = plan_many(cfg, keys, home, repo, self_dir, versions_root,
                      extra_protected=(backup_root, config_path),
                      retain_runtime=retain_runtime, allow_global=global_uninstall is not None,
                      base_override=base_override)
    for plan in plans:
        key = plan["key"]
        port = plan["port"]
        d = {"key": key, "label": plan["label"], "port": port,
             "root": plan["root"], "temp_dir": plan["temp_dir"],
             "bytes": 0, "errors": [], "warnings": [],
             "stopped": False}
        details.append(d)

        if plan["blocked"]:
            d["errors"].append(plan["blocked"])
            kept.append(key)
            p.on_step(T("[%s] 跳过：%s") % (key, plan["blocked"]))
            continue

        # 1) 停进程。停止失败时保留该槽和配置。
        if stop_fn:
            p.on_step(T("[%s] 停止端口 %s 上的 dsh（门控：只杀 dsh）…") % (key, port))
            try:
                if stop_fn(key, port) is False:
                    raise OSError(T("端口 %s 尚未释放") % port)
                d["stopped"] = True
            except Exception as e:
                d["errors"].append(T("停进程失败，已保留该槽：%s") % e)
                kept.append(key)
                continue

        # 2) 备份用户数据 + 插件清单 + 版本信息（**在删任何东西之前**）
        #    2026-10-30 起备份内容扩展：除了 .dsh-home（会话/配置/凭据/插件配置），
        #    还留一份槽根的 package.json / package-lock.json（用户装过哪些插件、
        #    钉的是哪个版本全在里面）和 _meta.json（槽名/实装版本/端口/时间）。
        p.on_step(T("[%s] 备份用户数据与插件清单…") % key)
        try:
            d["foreign"] = plan.get("foreign") or []
            dst, vfy = ("", {}) if skip_backup else _backup_slot(plan, key, backup_root, stamp)
            d["backup"] = dst
            d["verify"] = vfy
        except Exception as e:
            # 备份失败就不删程序 —— 用户数据没保险，宁可中止这个槽
            d["errors"].append(T("备份失败（已中止该槽，未删除任何文件）：%s") % e)
            kept.append(key)
            p.on_step(T("[%s] 中止：备份失败 %s") % (key, e))
            continue
        if not d["backup"] and not skip_backup:
            # 没有用户数据 → 不建备份目录（见 _backup_slot 开头）。日志里留版本号，
            # 以后想装回同一个版本照着抄即可。
            p.on_step(T("[%s] 没有用户数据（.dsh-home 为空），未生成备份（实装 %s）")
                      % (key, d.get("version") or T("未知")))
        if d["backup"]:
            p.on_step(T("[%s] 已备份并核对通过：%d 个文件 → %s")
                      % (key, vfy.get("checked", 0), d["backup"]))
        # 3) 删程序产物 + 用户数据（备份已成功，空壳不再保留）
        if plan.get("global_install"):
            try:
                global_uninstall(plan)
            except (OSError, subprocess.SubprocessError) as exc:
                d["errors"].append(T("npm 全局卸载失败：%s") % exc)
        elif plan["exists"]:
            p.on_step(T("[%s] 删除槽目录内容（共 %s）…") % (key, human_size(plan["bytes"])))
            for ch in plan["children"]:
                if ch["action"] not in ("delete", "purge"):
                    continue
                # 逐个报进度：node_modules 里几万个文件，Windows 上删一次要几十秒，
                # 只报一句「删除程序文件…」会让人以为卡死（2026-10-29 实测踩到）。
                p.on_step(T("[%s] 正在删除 %s（%s）…")
                          % (key, ch["name"], human_size(ch["size"])))
                try:
                    _force_rmtree(ch["path"])
                    d["bytes"] += ch["size"]
                except Exception as e:
                    d["errors"].append(T("删除 %s 失败：%s") % (ch["name"], e))
                    # 用户数据删不掉就**立刻停手**：此时程序文件还没动，槽仍然完整可用，
                    # 用户关掉占用程序/修好权限后重试即可。继续删程序会留下
                    # 「程序没了、数据还在」的半残槽（2026-10-30 实测踩到）。
                    if ch["action"] == "purge":
                        d["errors"].append(
                            T("已停止删除该槽的其余部分（程序文件保持完整，可修好后重试）"))
                        p.on_step(T("[%s] 中止删除：%s 删不掉") % (key, ch["name"]))
                        break

            # 4) 槽根自身：删空目录。只删**空**的，所以万一还有保留下来的项
            #    会自然失败 —— 不会误删。
            #    重试几次：Windows 刚删完几万个文件时，目录常被索引器/杀软/资源
            #    管理器短暂占住，rmdir 会失败；留下空壳会让下次装同名槽变成 dshV1-2。
            if plan.get("purge_root") and not d["errors"]:
                last = None
                for _ in range(3):
                    try:
                        _assert_unlinked_path(plan["root"])
                        os.rmdir(plan["root"])
                        d["purged_root"] = True
                        break
                    except OSError as e:
                        last = e
                        time.sleep(0.4)
                if not d.get("purged_root"):
                    d["errors"].append(
                        T("槽目录没能整个删掉（%s）：%s —— 下次装同一槽可能会被命名成 %s-2")
                        % (plan["root"], last, os.path.basename(plan["root"])))
                    p.on_step(T("[%s] 提示：槽目录没能整个删掉（%s），下次装同名槽会复用或改名")
                              % (key, last))

        total_bytes += d["bytes"]

        # 4) 只有「程序文件都清干净了」才从配置里移除，否则保留重试入口
        if d["errors"]:
            kept.append(key)
        else:
            removed.append(key)

    # 5) 写回 config.json（只移除成功的那些）
    if removed:
        p.on_step(T("更新 config.json（移除 %s）…") % ", ".join(removed))
        try:
            _assert_unlinked_path(config_path)
            cfg = OPS.read_config(config_path)
            current_versions = cfg.get("versions") or {}
            for k in removed[:]:
                if current_versions.get(k) != versions.get(k):
                    removed.remove(k)
                    kept.append(k)
                else:
                    current_versions.pop(k, None)
            if removed:
                OPS.write_config_atomic(config_path, cfg)
        except Exception as e:
            return (False, {"details": details}, T("删除已完成，但写 config.json 失败：%s\n"
                                                 "请手动从 config.json 里删掉 %s")
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
        return (False, payload, T("没有槽被卸载。\n\n") + "\n".join(msgs or [T("（原因未知）")]))

    parts = [T("已卸载：%s（释放 %s）") % (", ".join(removed), human_size(total_bytes))]
    for d in details:
        _f = d.get("foreign") or []
        if _f and not d.get("errors"):
            parts.append(T("  · %s 里另有 %d 个非 dsh 条目被一并删除：%s")
                         % (d["key"], len(_f), "、".join(_f[:6])
                            + ("…" if len(_f) > 6 else "")))
    if kept:
        parts.append(T("保留在配置里（未完全清理，可重试）：%s") % ", ".join(kept))
        for d in details:
            if d["errors"]:
                parts.append("  · %s：%s" % (d["key"], "；".join(d["errors"])))
    _with_bak = [d for d in details if d.get("backup")]
    _no_bak = [d for d in details if not d.get("backup") and not d.get("errors")]
    if _with_bak:
        parts.append(T("备份（用户数据 + 插件清单 + 版本信息）在：%s") % backup_root)
        for d in _with_bak:
            parts.append("  · %s → %s" % (d["key"], d["backup"]))
    if _no_bak and not skip_backup:
        # 没有用户数据就不建备份目录（2026-11-14）—— 别让用户去一个空目录里找东西
        parts.append(T("未生成备份（这些槽没有用户数据）：%s")
                     % ", ".join(d["key"] for d in _no_bak))
    # 槽目录现在整个删掉了（含 .dsh-home/.tmp），所以下次装同一槽不会再变成
    # `dshV4-2`。万一某个槽根没删成（被占用等），如实说出来 —— 那才会导致 -2 后缀。
    leftover = [d["root"] for d in details
                if d.get("root") and os.path.isdir(d["root"])]
    if leftover and not retain_runtime:
        parts.append(T("注意：这些槽目录没能整个删掉：%s\n"
                     "下次往同一槽装版本可能被命名成带 -2 后缀的目录，"
                     "确认上面的备份无误后可手动删掉它们。") % "、".join(leftover))
    # 槽外临时目录（<临时目录根>\<槽>）不归槽所有，不动它，但要说一声免得残留
    ext = []
    for d in details:
        t = d.get("temp_dir") or ""
        r = d.get("root") or ""
        if t and os.path.isdir(t) and not (r and is_under(t, r)):
            ext.append(t)
    if ext:
        parts.append(T("槽外的临时目录未动（不是槽的一部分，可自行清理）：%s")
                     % "、".join(ext))
    warns = []
    for d in details:
        for w in d.get("warnings") or []:
            warns.append("%s：%s" % (d["key"], w))
    if warns:
        parts.append(T("注意：\n  ") + "\n  ".join(warns))
    return (True, payload, "\n".join(parts))


def _stop_cli_slot(entrypoint, port):
    import dsh_slot_process as processes

    try:
        port = int(port)
    except (ValueError, TypeError) as exc:
        raise OSError(T("端口配置无效：%s") % port) from exc
    if not 1 <= port <= 65535:
        raise OSError(T("端口配置无效：%s") % port)
    if not entrypoint:
        raise OSError(T("没有槽程序入口，无法确认进程已停止。"))
    processes.stop_slot(entrypoint, port)
    for _ in range(20):
        owned, listeners = processes.slot_listeners(entrypoint, port)
        unknown = any(processes.process_info(pid) is None for pid in listeners)
        if not owned and not unknown and (listeners or not processes.port_reachable(port)):
            return True
        time.sleep(0.25)
    return False


def _uninstall_global(plan):
    candidates = (shutil.which(PLATFORM.NODE_NAME), PLATFORM.resolve_node())
    for node in candidates:
        npm = PLATFORM.npm_cli_path(node)
        if npm:
            break
    else:
        raise OSError(T("找不到 npm"))
    _assert_unlinked_path(plan["root"])
    # Invoke npm through node to keep the configured prefix out of cmd.exe.
    subprocess.run([node, npm, "uninstall", "-g", "--prefix", plan["root"],
                    "@deepseek-ai/dsh"], check=True)


def _desktop_shortcut():
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    if PLATFORM.IS_WINDOWS:
        import winreg
        try:
            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as settings:
                desktop = os.path.expandvars(winreg.QueryValueEx(settings, "Desktop")[0])
        except OSError:
            pass
    return os.path.join(desktop, "dsh-manager.lnk")


def _finish_command_uninstall(config_path, repo_config, backup_root, skip_backup, stamp):
    latest = OPS.read_config(config_path)
    if latest.get("versions") or detached_records(latest):
        return
    log = os.path.join(os.path.dirname(config_path), "dsh-manager.log")
    config_is_default = _path_within(config_path, repo_config) and _path_within(repo_config, config_path)
    paths = [config_path]
    if config_is_default or os.path.dirname(log) != os.path.dirname(repo_config):
        if os.path.lexists(log):
            paths.append(log)
    if not skip_backup:
        for path in paths:
            label = "config" if path == config_path else None
            _backup_required_file(path, stamp, backup_root, label)
    for path in reversed(paths):
        _assert_unlinked_path(path)
        os.unlink(path)
    if config_is_default and PLATFORM.IS_WINDOWS:
        desktop = _desktop_shortcut()
        if os.path.lexists(desktop):
            _force_rmtree(desktop)


def _command_main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=T("dsh-tm 卸载器"))
    parser.add_argument("--base", default="")
    parser.add_argument("--config-path", default="")
    parser.add_argument("--target", default="")
    for option in ("dry-run", "yes", "skip-backup", "skip-global"):
        parser.add_argument("--" + option, action="store_true")
    args = parser.parse_args(argv)
    home = os.path.expanduser("~")
    repo = os.path.dirname(os.path.abspath(__file__))
    default_config = PLATFORM.default_config_path()
    base = PLATFORM.resolve_path(args.base, home=home, self_dir=repo) if args.base else ""
    candidates = [os.path.join(base, "config.json")] if base else []
    config_path = args.config_path or next(
        (path for path in candidates + [default_config] if os.path.isfile(path)), default_config)
    config_path = os.path.abspath(config_path)
    try:
        _assert_unlinked_path(config_path)
        cfg = OPS.read_config(config_path)
        versions = cfg.get("versions") or {}
        if not isinstance(versions, dict) or any(not isinstance(slot, dict) for slot in versions.values()):
            raise ValueError(T("versions 配置格式无效"))
    except (OSError, ValueError, AttributeError) as exc:
        print(T("无法读取配置 %s：%s") % (config_path, exc), file=sys.stderr)
        return 1
    if not versions:
        print(T("没有需要卸载的槽。"))
        return 0

    keys = list(versions)
    selected = args.target.strip().lower()
    if not selected:
        for index, key in enumerate(keys, 1):
            print("%d) %s (%s)" % (index, key, versions[key].get("label") or key))
        try:
            selected = input(T("输入编号（可用逗号分隔）或 a（全部）：")).strip().lower()
        except EOFError:
            return 0
        selected = ",".join(keys[int(token) - 1] if token.isdigit()
                            and 1 <= int(token) <= len(keys) else token
                            for token in (part.strip() for part in selected.split(",")))
    if selected not in ("all", "a", "both"):
        requested = {name.strip().lower() for name in selected.split(",")}
        keys = [key for key in keys if key.lower() in requested]
    if not keys:
        print(T("没有选中有效槽。"))
        return 0
    actual_base = base or install_base_of(cfg, home, repo)
    backup_root = os.path.join(actual_base, T("dsh备份"))
    plans = plan_many(cfg, keys, home, repo, repo, extra_protected=(backup_root, config_path),
                      retain_runtime=True, allow_global=True, base_override=base)
    for plan in plans:
        print("%s  %s%s" % (plan["key"], plan["root"],
                            "  (" + plan["blocked"] + ")" if plan["blocked"] else ""))
        if plan.get("global_install") and not args.skip_global:
            print("  npm uninstall -g @deepseek-ai/dsh")
        for child in plan["children"]:
            if child["action"] == "delete":
                print("  " + T("删除 %s") % child["path"])
    if args.dry_run:
        print("[DryRun] " + T("未做任何改动。"))
        return 0
    if not args.yes:
        try:
            if input(T("确认卸载？输入 yes 继续：")).strip().lower() != "yes":
                return 0
        except EOFError:
            return 0

    class ConsoleProgress(OPS.Progress):
        def on_step(self, message):
            print(message)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    ok, payload, message = uninstall_slots(
        keys, config_path, home, repo, repo, backup_root=backup_root,
        stop_fn=lambda key, port: _stop_cli_slot(
            resolve(versions[key].get("bin_js"), actual_base, home, repo), port),
        progress=ConsoleProgress(), stamp=stamp,
        retain_runtime=True, skip_backup=args.skip_backup, base_override=base,
        global_uninstall=(lambda plan: None) if args.skip_global else _uninstall_global,
    )
    print(message)
    if not ok or payload["kept_in_config"]:
        return 1
    try:
        _finish_command_uninstall(config_path, default_config, backup_root, args.skip_backup, stamp)
    except OSError as exc:
        print(T("收尾失败：%s") % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_command_main())
