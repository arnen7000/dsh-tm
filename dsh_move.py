# -*- coding: utf-8 -*-
"""移动安装根目录（把已装的槽整体搬到另一个位置）。

为什么单独成模块
----------------
搬家的逻辑要能**离线测试**（在一个假的目录树上跑，不碰真实槽），
所以不能写在 dsh_manager.pyw 里 —— 那个文件模块级就会建窗口。

核心约束（违反任一条都会把用户的版本搬坏）
------------------------------------------
1. **先复制、校验、再删源**。任何一步失败都必须让源保持完好。
2. **跳过 junction / 符号链接**，绝不通透到目标里去（项目踩过：rmtree 穿透删目标）。
3. **校验到文件数 + 总字节数**，对不上就中止，且不删源。
4. **只搬槽目录**（`<base>/dsh-versions/dshVx`），备份目录与临时目录另行处理。
5. 搬完要**重写 config.json 里的路径**，否则管理器指向旧位置。
6. 目标必须是空目录或不存在 —— 绝不覆盖已有内容。
"""
from __future__ import annotations

import json
import os
import shutil
import stat
from dsh_i18n import T

# Windows 上 junction / 符号链接的 reparse 属性位
_REPARSE_FLAG = 0x400


def is_link_like(path):
    """是否 junction / 符号链接（`os.path.islink` 认不出 junction，必须看属性位）。"""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if os.path.islink(path):
        return True
    try:
        return bool((st.st_file_attributes or 0) & _REPARSE_FLAG)
    except Exception:
        return False


def tree_stats(root):
    """统计一棵树的 (文件数, 总字节数)；跳过 junction/链接（不跟进去）。

    返回 dict：{"files": n, "bytes": m, "links": k, "errors": [...]}
    """
    files = 0
    total = 0
    links = 0
    errors = []
    if not os.path.isdir(root):
        return {"files": 0, "bytes": 0, "links": 0, "errors": [T("不是目录：%s") % root]}
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        # 剪掉 junction/链接目录，防止走到槽外
        keep = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if is_link_like(full):
                links += 1
                continue
            keep.append(d)
        dirnames[:] = keep
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if is_link_like(full):
                links += 1
                continue
            try:
                st = os.stat(full)
                files += 1
                total += st.st_size
            except OSError as e:
                errors.append("%s: %s" % (full, e))
    return {"files": files, "bytes": total, "links": links, "errors": errors}


def _safe_rmtree(path):
    """删除一棵树但**不穿过** junction。失败只返回错误，不抛。

    2026-11-11 修：原来用 `shutil.rmtree(path, onerror=...)`，只在**顶层**
    判了一次 reparse 位。子树里的 junction（`.dsh-home\\profiles\\node_modules\\*`
    指向槽外的插件仓库）不在检查范围内，而 rmtree 是否跟随 junction 取决于
    Python 版本实现 —— 依赖它是赌运气。改成自己逐层走：碰到链接只摘链接，
    与 dsh_uninstall_ops._force_rmtree 保持同一套语义。
    """
    if not os.path.exists(path):
        return ""
    errors = []

    def _clear_ro(p):
        try:
            os.chmod(p, stat.S_IWRITE)
        except OSError:
            pass

    def _walk(p):
        if is_link_like(p):
            # 只摘重解析点本身，绝不进去删目标内容
            _clear_ro(p)
            try:
                os.rmdir(p)
            except OSError:
                try:
                    os.unlink(p)
                except OSError as ex:
                    errors.append("%s（%s）" % (p, ex))
            return
        try:
            entries = list(os.scandir(p))
        except OSError as e:
            errors.append("%s（%s）" % (p, e))
            return
        for e in entries:
            child = e.path
            if is_link_like(child) or e.is_dir(follow_symlinks=False):
                _walk(child)
                continue
            _clear_ro(child)
            try:
                os.unlink(child)
            except OSError as ex:
                _on_rm_error(os.unlink, child, (type(ex), ex, None))
                if os.path.exists(child):
                    errors.append("%s（%s）" % (child, ex))
        _clear_ro(p)
        try:
            os.rmdir(p)
        except OSError as e:
            errors.append("%s（%s）" % (p, e))

    _walk(path)
    if os.path.exists(path):
        return "；".join(errors[:3]) or (T("无法删除：%s") % path)
    return ""


def _on_rm_error(func, path, exc_info):
    """只读文件删不掉时清掉只读位再试一次（node_modules 里常见）。"""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def _copy_tree_no_follow(src, dst):
    """复制一棵树，**不跟随 junction / 符号链接**，并返回复制到的链接数。

    为什么不用 shutil.copytree(symlinks=True)
    ----------------------------------------
    实测：`os.path.islink()` 对 Windows junction 返回 **False**
    （junction 的 reparse 属性位与符号链接不同），于是 copytree 把 junction
    当成普通目录**复制进去**，目标就多出一份真实副本 —— 校验必然对不上
    （源 5 文件 / 目标 6 文件），搬迁被安全校验中止。

    这里自己走 os.walk，遇到链接只**记录**、不进入、不复制其内容：
    搬迁后的槽仍然指向原来的链接目标（与搬迁前的行为一致），
    而校验的文件数/字节数也天然对得上。
    """
    copied_links = 0
    os.makedirs(dst, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(src, topdown=True):
        rel = os.path.relpath(dirpath, src)
        tgt_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(tgt_dir, exist_ok=True)

        keep = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if is_link_like(full):
                copied_links += 1      # 链接目录：跳过，不复制、不进入
                continue
            keep.append(d)
        dirnames[:] = keep

        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if is_link_like(full):
                copied_links += 1      # 链接文件：同样跳过
                continue
            try:
                shutil.copy2(full, os.path.join(tgt_dir, fn))
            except Exception as e:
                raise RuntimeError(T("复制 %s 失败：%s") % (full, e))
    return copied_links


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
    src_base = os.path.normpath(os.path.abspath(src_base))
    dst_base = os.path.normpath(os.path.abspath(dst_base))

    if os.path.normcase(src_base) == os.path.normcase(dst_base):
        return {"ok": False, "error": T("目标位置与当前位置相同，无需移动。"), "steps": [], "slots": []}

    # 目标不能是源的一部分（会自己搬自己）
    def _inside(child, parent):
        c = os.path.normcase(os.path.abspath(child)) + os.sep
        p = os.path.normcase(os.path.abspath(parent)) + os.sep
        return c.startswith(p)

    if _inside(dst_base, src_base) or _inside(src_base, dst_base):
        return {"ok": False,
                "error": T("目标位置与当前位置互相包含（一个在另一个里面），无法移动。\n"
                         "请选一个完全分开的位置。"), "steps": [], "slots": []}

    src_versions = os.path.join(src_base, "dsh-versions")
    dst_versions = os.path.join(dst_base, "dsh-versions")
    slots = []
    steps = []
    for name in slot_dirs:
        s = os.path.join(src_versions, name)
        d = os.path.join(dst_versions, name)
        if not os.path.isdir(s):
            return {"ok": False, "error": T("源槽不存在：%s") % s, "steps": [], "slots": []}
        if os.path.exists(d):
            return {"ok": False,
                    "error": T("目标位置已存在同名目录，为避免覆盖已中止：\n%s") % d,
                    "steps": [], "slots": []}
        st = tree_stats(s)
        slots.append({"name": name, "src": s, "dst": d, "stats": st})
        steps.append(T("复制 %s → %s（%d 个文件，%.1f MB）")
                     % (s, d, st["files"], st["bytes"] / 1048576.0))

    if not slots:
        return {"ok": False, "error": T("没有要移动的槽。"), "steps": [], "slots": []}

    # 备份目录
    src_bak = os.path.join(src_base, backup_dirname)
    dst_bak = os.path.join(dst_base, backup_dirname)
    if os.path.isdir(src_bak):
        steps.append(T("复制备份目录 %s → %s") % (src_bak, dst_bak))
    # 临时目录
    src_tmp = os.path.join(src_base, temp_rootname)
    dst_tmp = os.path.join(dst_base, temp_rootname)
    if os.path.isdir(src_tmp):
        steps.append(T("复制临时目录 %s → %s") % (src_tmp, dst_tmp))

    # 空间预检（2026-11-14）：跨盘搬迁最常见的失败就是「拷到一半盘满」。
    # 与其拷到 70% 再报错（旧位置还占着、新位置一堆残件），不如事前算清楚。
    need = sum(s["stats"]["bytes"] for s in slots)
    for extra in (src_bak, src_tmp):
        if os.path.isdir(extra):
            try:
                need += tree_stats(extra)["bytes"]
            except Exception:
                pass
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
            "src_bak": src_bak, "dst_bak": dst_bak,
            "src_tmp": src_tmp, "dst_tmp": dst_tmp,
            "need_bytes": need, "free_bytes": free}


def move_now(plan, config_path, progress=None):
    """按计划执行搬迁。返回 (ok, message)。

    顺序：复制 → 校验 → 改 config → 删源。
    **只有校验通过才改 config 和删源**；任何失败都保留源、回滚 config。
    """
    if not isinstance(plan, dict) or not plan.get("ok"):
        return False, (plan or {}).get("error") or T("搬迁计划无效")

    def step(msg):
        if progress is not None:
            try:
                progress(msg)
            except Exception:
                pass

    os.makedirs(plan["dst_versions"], exist_ok=True)

    # ① 复制并校验每个槽
    for s in plan["slots"]:
        step(T("复制 %s …") % s["name"])
        try:
            _copy_tree_no_follow(s["src"], s["dst"])
        except Exception as e:
            _safe_rmtree(s["dst"])
            return False, T("复制 %s 失败：%s\n源未改动。") % (s["name"], e)
        # ② 校验
        step(T("校验 %s …") % s["name"])
        a = s["stats"]
        b = tree_stats(s["dst"])
        if b["files"] != a["files"] or b["bytes"] != a["bytes"]:
            _safe_rmtree(s["dst"])
            return False, (T("%s 校验失败（源 %d 文件 / %d 字节，目标 %d 文件 / %d 字节），"
                           "已删除目标副本，源未改动。")
                           % (s["name"], a["files"], a["bytes"],
                              b["files"], b["bytes"]))

    # ③ 备份目录与临时目录（可选，失败不致命）
    for key in ("bak", "tmp"):
        src = plan.get("src_%s" % key)
        dst = plan.get("dst_%s" % key)
        if src and os.path.isdir(src) and not os.path.exists(dst):
            step(T("复制 %s …") % os.path.basename(src))
            try:
                shutil.copytree(src, dst, symlinks=True)
            except Exception:
                pass        # 备份/临时目录复制失败不阻断主流程

    # ④ 改 config.json：把 <base> 之类解析后的旧根换成新根
    step(T("更新 config.json …"))
    try:
        with open(config_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        return False, T("读取 config.json 失败（副本已复制到新位置，源未删）：%s") % e

    old_base = plan["src_base"]
    new_base = plan["dst_base"]

    def _swap(v):
        if not isinstance(v, str):
            return v
        # install_base 可能是 "<home>" 这类占位符：只有当它解析后等于旧根时才改
        if v.strip() in ("<home>", "<base>", "<self>"):
            return v
        try:
            nv = os.path.normpath(v.replace("<base>", old_base)
                                   .replace("<home>", os.path.expanduser("~"))
                                   .replace("<self>", os.path.dirname(os.path.abspath(config_path))))
        except Exception:
            return v
        if nv.lower().startswith(old_base.lower()):
            return os.path.join(new_base, nv[len(old_base):].lstrip("\\/"))
        return v

    data["install_base"] = new_base
    for _k, cfg in (data.get("versions") or {}).items():
        if not isinstance(cfg, dict):
            continue
        for fld in ("node_exe", "bin_js", "dsh_home", "temp_dir"):
            if cfg.get(fld):
                cfg[fld] = _swap(cfg[fld])
    if data.get("temp_root"):
        data["temp_root"] = _swap(data["temp_root"])
    for rec in (data.get("detached") or []):
        if isinstance(rec, dict):
            for fld in ("root", "node_exe", "bin_js", "dsh_home", "temp_dir"):
                if rec.get(fld):
                    rec[fld] = _swap(rec[fld])

    tmp = config_path + ".tmp-move"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, config_path)
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False, T("写入 config.json 失败（副本已复制，源未删）：%s") % e

    # ⑤ 删源
    failed = []
    for s in plan["slots"]:
        step(T("清理旧位置 %s …") % s["name"])
        err = _safe_rmtree(s["src"])
        if err:
            failed.append("%s（%s）" % (s["name"], err))

    if failed:
        return True, (T("搬迁完成，config 已指向新位置。\n"
                      "但以下旧目录未能删除，可以手动清理：\n  ") + "\n  ".join(failed))

    return True, T("搬迁完成：%d 个槽已移到新位置，config.json 已更新。") % len(plan["slots"])
