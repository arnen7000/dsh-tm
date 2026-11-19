# -*- coding: utf-8 -*-
"""移动安装根目录（把已装的槽整体搬到另一个位置）。

为什么单独成模块
----------------
搬家的逻辑要能**离线测试**（在一个假的目录树上跑，不碰真实槽），
所以不能写在 dsh_manager.pyw 里 —— 那个文件模块级就会建窗口。

核心约束（违反任一条都会把用户的版本搬坏）
------------------------------------------
复制并核对所有槽后才提交配置；失败时清理目标副本，保留源。
符号链接和 Windows junction 作为链接复制，且保留目标关系。
"""
from __future__ import annotations

from difflib import SequenceMatcher
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time

import dsh_uninstall_ops as FS
import dsh_platform as PLATFORM
from dsh_i18n import T


is_link_like = FS._is_reparse_path


def _entries(root):
    """Enumerate directory entries, descending only into ordinary directories."""
    FS._assert_unlinked_path(root)
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as children:
            for child in children:
                info = child.stat(follow_symlinks=False)
                yield child.path, info
                if not FS._stat_is_link(info) and stat.S_ISDIR(info.st_mode):
                    pending.append(child.path)


def tree_stats(root):
    stats = {"files": 0, "bytes": 0, "links": 0, "errors": []}
    try:
        for path, info in _entries(root):
            if FS._stat_is_link(info):
                stats["links"] += 1
            elif stat.S_ISREG(info.st_mode):
                stats["files"] += 1
                stats["bytes"] += info.st_size
            elif not stat.S_ISDIR(info.st_mode):
                stats["errors"].append(T("无法搬迁此文件类型：%s") % path)
    except OSError as exc:
        stats["errors"].append(str(exc))
    return stats


def _safe_rmtree(path):
    try:
        FS._force_rmtree(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        return str(exc)
    return ""


def _moved_path(path, relocations):
    for source, destination in relocations:
        relative = FS._path_relative_to(path, source)
        if relative is not None:
            return os.path.normpath(os.path.join(destination, relative))
    return path


def _link_path(path):
    if os.name == "nt":
        # Windows readlink returns the NT substitute spelling for junctions.
        prefix = chr(92) * 2 + "?" + chr(92)
        if path.startswith(prefix + "UNC" + chr(92)):
            return chr(92) * 2 + path[len(prefix) + 4:]
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def _copy_link(path, to, final_path, info, relocations):
    target = _link_path(os.readlink(path))
    source_target = os.path.normpath(os.path.join(os.path.dirname(path), target))
    new_target = _moved_path(source_target, relocations)
    if not os.path.isabs(target):
        if os.path.normpath(os.path.join(os.path.dirname(final_path), target)) == new_target:
            new_target = target
        else:
            try:
                new_target = os.path.relpath(new_target, os.path.dirname(final_path))
            except ValueError:
                pass  # The destination is on a different Windows drive.
    tag = getattr(info, "st_reparse_tag", None)
    if tag == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1):
        import _winapi
        _winapi.CreateJunction(new_target, to)
    elif stat.S_ISLNK(info.st_mode):
        directory_link = bool(getattr(info, "st_file_attributes", 0)
                              & stat.FILE_ATTRIBUTE_DIRECTORY)
        os.symlink(new_target, to, target_is_directory=directory_link)
    else:
        raise OSError(T("无法复制此重解析点：%s") % path)
    if _link_path(os.readlink(to)) != new_target:
        raise OSError(T("复制链接后校验失败：%s") % path)


def _copy_tree_no_follow(src, dst, final_root, relocations):
    os.mkdir(dst)
    directories = [(src, dst)]
    for path, info in _entries(src):
        relative = os.path.relpath(path, src)
        destination = os.path.join(dst, relative)
        if FS._stat_is_link(info):
            _copy_link(path, destination, os.path.join(final_root, relative),
                       info, relocations)
        elif stat.S_ISDIR(info.st_mode):
            os.mkdir(destination)
            directories.append((path, destination))
        elif stat.S_ISREG(info.st_mode):
            shutil.copy2(path, destination, follow_symlinks=False)
        else:
            raise OSError(T("无法搬迁此文件类型：%s") % path)
    for source, target in reversed(directories):
        shutil.copystat(source, target, follow_symlinks=False)


def _free_bytes(path):
    """目标路径所在磁盘的剩余字节数。

    目标目录可能还不存在（用户刚选的新位置），这时往上找最近的已存在父目录 ——
    `shutil.disk_usage` 对不存在的路径会抛错，但它所在盘是同一个。
    """
    p = os.path.abspath(path)
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent
    try:
        return shutil.disk_usage(p).free
    except Exception:
        return None


def plan_move(src_base, dst_base, slot_dirs, backup_dirname=T("dsh备份"),
              temp_rootname="dsh-temp"):
    """算出搬迁计划，不改任何东西。

    src_base / dst_base：安装根（不含 dsh-versions）
    slot_dirs：要搬的槽目录名（如 ["dshV2", "dshV4"]）

    返回 {"ok": bool, "error": str, "steps": [...], "slots": [...]}
    """
    def reject(message):
        return {"ok": False, "error": message, "steps": [], "slots": []}

    try:
        FS._assert_unlinked_path(src_base)
        FS._assert_unlinked_path(dst_base)
    except OSError as exc:
        return reject(str(exc))
    src_base = os.path.abspath(src_base)
    dst_base = os.path.abspath(dst_base)
    if FS._path_within(src_base, dst_base) and FS._path_within(dst_base, src_base):
        return reject(T("目标位置与当前位置相同，无需移动。"))
    if FS._paths_overlap(src_base, dst_base):
        return reject(T("目标位置与当前位置互相包含，无法移动。"))

    src_versions = os.path.join(src_base, "dsh-versions")
    dst_versions = os.path.join(dst_base, "dsh-versions")
    slots = []
    steps = []
    names = set()
    for name in slot_dirs:
        if (not name or name in (".", "..") or os.path.basename(name) != name
                or os.path.splitdrive(name)[0] or os.path.normcase(name) in names):
            return reject(T("槽目录名无效或重复：%s") % name)
        names.add(os.path.normcase(name))
        s = os.path.join(src_versions, name)
        d = os.path.join(dst_versions, name)
        if not os.path.isdir(s):
            return {"ok": False, "error": T("源槽不存在：%s") % s, "steps": [], "slots": []}
        if os.path.lexists(d):
            return {"ok": False,
                    "error": T("目标位置已存在同名目录，为避免覆盖已中止：\n%s") % d,
                    "steps": [], "slots": []}
        try:
            FS._assert_unlinked_path(os.path.dirname(d))
        except OSError as exc:
            return reject(str(exc))
        st = tree_stats(s)
        if st["errors"]:
            return reject(st["errors"][0])
        slots.append({"name": name, "src": s, "dst": d, "stats": st})
        steps.append(T("复制 %s → %s（%d 个文件，%.1f MB）")
                     % (s, d, st["files"], st["bytes"] / 1048576.0))

    if not slots:
        return {"ok": False, "error": T("没有要移动的槽。"), "steps": [], "slots": []}

    extras = []
    for name in dict.fromkeys((backup_dirname, temp_rootname)):
        if (not name or name in (".", "..") or os.path.basename(name) != name
                or os.path.splitdrive(name)[0] or name == "dsh-versions"):
            return reject(T("备份或临时目录名无效：%s") % name)
        source, destination = os.path.join(src_base, name), os.path.join(dst_base, name)
        if not os.path.lexists(source):
            continue
        if os.path.lexists(destination):
            return reject(T("目标位置已存在：%s") % destination)
        info = tree_stats(source)
        if info["errors"]:
            return reject(info["errors"][0])
        extras.append({"name": name, "src": source, "dst": destination, "stats": info})
        steps.append(T("复制 %s → %s") % (source, destination))

    need = sum(item["stats"]["bytes"] for item in slots + extras)
    free = _free_bytes(dst_base)
    margin = int(need * 0.05) + 200 * 1048576      # 5% + 200MB 余量
    if free is not None and need + margin > free:
        return {"ok": False,
                "error": (T("目标磁盘空间不足，已中止（什么都没动）：\n"
                          "需要约 %.2f GB（含 5%% 余量），该盘可用 %.2f GB。\n\n"
                          "请清理空间，或换一个位置。")
                          % ((need + margin) / 1073741824.0,
                             free / 1073741824.0)),
                "steps": [], "slots": []}

    return {"ok": True, "error": "", "steps": steps, "slots": slots,
            "src_base": src_base, "dst_base": dst_base,
            "src_versions": src_versions, "dst_versions": dst_versions,
            "extras": extras,
            "need_bytes": need, "free_bytes": free}


def _verify_copy(source, target, expected):
    stats = tree_stats(target)
    if stats != expected:
        raise OSError(T("目录校验失败：%s") % source)
    missing, changed, _ = FS.verify_backup(source, target)
    if missing or changed:
        raise OSError(T("文件内容校验失败：%s") % source)
    source_names = {os.path.relpath(path, source) for path, _ in _entries(source)}
    target_names = {os.path.relpath(path, target) for path, _ in _entries(target)}
    if source_names != target_names:
        raise OSError(T("目录项校验失败：%s") % source)


def _windows_launcher_text(text, relocations):
    def path(value):
        if not os.path.isabs(value):
            return value
        moved = _moved_path(value, relocations)
        if moved != value and any(c in moved for c in '"\r\n%&^|<>()'):
            raise OSError(T("目标路径含有 Windows 启动脚本不支持的字符：%s") % moved)
        return moved

    # Recognize the launcher's path operands and environment assignments. Keep
    # the original GBK/CRLF and do not rewrite arbitrary comments or commands.
    text = re.sub(r'(?m)^(set "(?:DSH_HOME|TEMP|TMP|PATH)=)([^"\r\n]*?)(;%PATH%)?("\r?$)',
                  lambda m: m[1] + path(m[2]) + (m[3] or "") + m[4], text)
    text = re.sub(r'(?m)^"([^"\r\n]+)" "([^"\r\n]+)"(?= (?:web |%\*\r?$))',
                  lambda m: '"%s" "%s"' % (path(m[1]), path(m[2])), text)
    return re.sub(r'(?m)^([ \t]*"[^"\r\n]+" "[^"\r\n]+" --port [^\r\n]+ --entrypoint ")'
                  r'([^"\r\n]+)("\r?$)',
                  lambda m: m[1] + path(m[2]) + m[3], text)


def _refresh_staged_launchers(item, stage_path, configs, relocations):
    before, after = configs.get(os.path.normcase(FS._native_path(item["src"])), ({}, {}))
    port = before.get("port")
    if not port:
        return
    candidates = PLATFORM.slot_launcher_paths(stage_path, port).values()
    paths = [Path(path) for path in candidates
             if not FS._is_reparse_path(path) and os.path.isfile(path)]
    if not paths:
        return
    if PLATFORM.IS_MAC:
        old = PLATFORM.slot_launcher_texts(item["src"], before)
        new = PLATFORM.slot_launcher_texts(item["dst"], after)
        old = {os.path.basename(path): text.splitlines(keepends=True) for path, text in old.items()}
        new = {os.path.basename(path): text.splitlines(keepends=True) for path, text in new.items()}
    encoding = "utf-8" if PLATFORM.IS_MAC else "gbk"
    for path in paths:
        original = path.read_bytes()
        try:
            text = original.decode(encoding)
        except UnicodeError:
            continue  # A launcher in a different encoding is user-maintained.
        if PLATFORM.IS_MAC:
            before_lines, after_lines = old[path.name], new[path.name]
            # Apply only generated sections that still match the pre-move
            # launcher verbatim, leaving customizations around them intact.
            for tag, start, end, replacement, limit in SequenceMatcher(
                    None, before_lines, after_lines, autojunk=False).get_opcodes():
                if tag == "replace":
                    text = text.replace("".join(before_lines[start:end]),
                                        "".join(after_lines[replacement:limit]), 1)
        elif PLATFORM.IS_WINDOWS:
            text = _windows_launcher_text(text, relocations)
        updated = text.encode(encoding)
        if updated != original:
            path.write_bytes(updated)


def _update_paths(data, plan, config_path, relocations):
    old_base = plan["src_base"]
    home = os.path.expanduser("~")
    self_dir = os.path.dirname(os.path.abspath(config_path))

    def swap(value):
        if not isinstance(value, str) or not value:
            return value
        resolved = FS.resolve(value, old_base, home, self_dir)
        moved = _moved_path(resolved, relocations)
        if moved != resolved or "<base>" in value:
            return moved
        return value

    new_base = plan["dst_base"]
    data["install_base"] = new_base
    configs = {}
    for cfg in (data.get("versions") or {}).values():
        before = dict(cfg)
        source = FS.slot_root(before, old_base, home, self_dir)
        for field in ("node_exe", "bin_js", "dsh_home", "temp_dir"):
            if field in cfg:
                before[field] = FS.resolve(cfg[field], old_base, home, self_dir)
                cfg[field] = swap(cfg[field])
        if source:
            after = {field: FS.resolve(value, new_base, home, self_dir)
                     if field in ("node_exe", "bin_js", "dsh_home", "temp_dir") else value
                     for field, value in cfg.items()}
            configs[os.path.normcase(FS._native_path(source))] = before, after
    if "temp_root" in data:
        data["temp_root"] = swap(data["temp_root"])
    return configs


def move_now(plan, config_path, progress=None):
    """Stage and verify all copies before publishing or touching the source."""
    if not isinstance(plan, dict) or not plan.get("ok"):
        return False, (plan.get("error") if isinstance(plan, dict) else "") or T("搬迁计划无效")

    def step(message):
        if progress is not None:
            try:
                progress(message)
            except Exception:
                pass

    config_path = os.fspath(config_path)
    items = plan["slots"] + plan.get("extras", [])
    relocations = [(item["src"], item["dst"]) for item in items]
    staging = None
    published = []
    try:
        FS._assert_unlinked_path(config_path)
        with open(config_path, "rb") as f:
            original = f.read()
        data = json.loads(original.decode("utf-8-sig"))
        base = FS.install_base_of(data, os.path.expanduser("~"), os.path.dirname(config_path))
        if not (FS._path_within(base, plan["src_base"])
                and FS._path_within(plan["src_base"], base)):
            raise OSError(T("安装目录已更改，请重新生成搬迁计划。"))
        for record in FS.detached_records(data):
            for item in plan["slots"]:
                if FS._paths_overlap(record.get("root"), item["src"]):
                    raise OSError(T("搬迁与已移出管理的槽目录重叠：%s") % record["root"])
        for source, destination in relocations:
            FS._assert_unlinked_path(source)
            FS._assert_unlinked_path(os.path.dirname(destination))
            if os.path.lexists(destination):
                raise FileExistsError(destination)
            if FS._paths_overlap(config_path, source):
                raise OSError(T("搬迁目录包含当前配置：%s") % source)
        configs = _update_paths(data, plan, config_path, relocations)
        os.makedirs(plan["dst_base"], exist_ok=True)
        staging = tempfile.mkdtemp(prefix=".dsh-move-", dir=plan["dst_base"])

        for index, item in enumerate(items):
            stage_path = os.path.join(staging, str(index))
            step(T("复制 %s …") % item["name"])
            _copy_tree_no_follow(item["src"], stage_path, item["dst"], relocations)
            step(T("校验 %s …") % item["name"])
            _verify_copy(item["src"], stage_path, item["stats"])
            _refresh_staged_launchers(item, stage_path, configs, relocations)

        for index, item in enumerate(items):
            destination = item["dst"]
            FS._assert_unlinked_path(os.path.dirname(destination))
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            if os.path.lexists(destination):
                raise FileExistsError(destination)
            os.rename(os.path.join(staging, str(index)), destination)
            published.append(destination)

        step(T("更新 config.json …"))
        with open(config_path, "rb") as f:
            if f.read() != original:
                raise OSError(T("配置在搬迁过程中已更改，请重新生成计划。"))
        FS._backup_required_file(config_path, time.strftime("%Y%m%d-%H%M%S"))
        _write_move_config(config_path, data)
    except Exception as exc:
        failures = [error for path in reversed(published) if (error := _safe_rmtree(path))]
        message = T("搬迁失败，源未改动：%s") % exc
        if failures:
            message += "\n" + T("以下目标副本未能清理：%s") % "；".join(failures)
        return False, message
    finally:
        if staging:
            _safe_rmtree(staging)

    failed = []
    for item in plan["slots"]:
        step(T("清理旧位置 %s …") % item["name"])
        error = _safe_rmtree(item["src"])
        if error:
            failed.append("%s（%s）" % (item["src"], error))
    if failed:
        return True, T("搬迁完成，但以下旧目录未能清理：%s") % "\n".join(failed)
    return True, T("搬迁完成：%d 个槽已移到新位置，config.json 已更新。") % len(plan["slots"])


def _write_move_config(path, data):
    # Use a distinct temporary file so failed replacements and concurrent writers
    # cannot overwrite one another's staging files.
    fd, staging = tempfile.mkstemp(prefix=".dsh-move-config-", dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(staging, path)
    finally:
        if os.path.lexists(staging):
            os.unlink(staging)
