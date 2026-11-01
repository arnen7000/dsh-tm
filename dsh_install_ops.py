# -*- coding: utf-8 -*-
"""管理器侧的两项新功能：纳入已有安装 / 一键装官方版。

与 dsh_adopt.py 的分工
-----------------------
  · dsh_adopt.py   —— 纯算法（版本号纠错、扫描、重名后缀、端口、备份），可直接单测。
  · 本文件        —— 与主程序状态打交道的部分（写 config.json、直调 npm 装版本、
                     线程与进度回调）。同样不碰 tkinter 控件，只接收回调，
                     这样既不把主文件撑大，也能在测试里用假回调驱动。

设计原则
--------
1. **先备份，后动结构**：纳入管理要复制目录、换 DSH_HOME。任何一步失败都不
   应该让用户丢数据，所以先备份 DSH_HOME 再复制，且复制失败即中止（不半途留残件）。
2. **不删原安装**：纳入只是"复制一份进管理槽 + 在 config.json 登记"，原来的
   npm 全局安装保持可用。用户想撤销，删掉新槽即可，原装毫发无损。
3. **失败要说人话**：每一步的失败都带原因，不留"未知错误"。
"""
import json
import os
import shutil
import subprocess
import threading
import time


class Progress(object):
    """进度回调。on_step(文字) / on_done(ok, 结果或错误) / on_cancel()。

    做成接口而不是直接调 tkinter，是为了让本文件可测、也让调用方决定
    在哪个线程刷新界面。"""

    def on_step(self, text):
        pass

    def on_done(self, ok, payload=None, error=""):
        pass

    def on_cancel(self):
        pass


# ══════════════════════════════════════════════════════════════════
# 配置读写（原子写，防半截文件）
# ══════════════════════════════════════════════════════════════════

def read_config(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def write_config_atomic(path, data):
    """原子写：先写临时文件再替换。
    直接 open(path,"w") 崩在中途会留下半截 JSON，下次启动直接读不出来——
    配置是这台机器上所有槽的唯一真相，不能冒这个险。"""
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def backup_config(path, stamp=None):
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    bak = "%s.bak-%s" % (path, stamp)
    try:
        shutil.copyfile(path, bak)
        return bak
    except Exception as e:
        raise IOError("备份 config.json 失败（已中止，避免写坏配置）：%s" % e)


# ══════════════════════════════════════════════════════════════════
# 功能一：把已装好的 dsh 纳入管理槽
# ══════════════════════════════════════════════════════════════════

def _dsh_dir_under(node_modules):
    """<某处>/node_modules 里是否装着 @deepseek-ai/dsh；返回包目录，没有返回 ''。

    注意：这里**只判断目录存在**，不判断内容完整——完整性由调用方逐项校验
    （package.json / lib/bin.js），这样错误信息能说清到底缺什么。"""
    if not node_modules:
        return ""
    cand = os.path.join(node_modules, "@deepseek-ai", "dsh")
    return cand if os.path.isdir(cand) else ""


def adopt_existing(adopt_mod, node_modules, versions_root, config_path,
                   slot_key, taken_ports, node_exe_source=None, progress=None,
                   max_slots=5, browser="default", backup_root=None,
                   install_base="", temp_root=""):
    """把一份已装好的 dsh 整理成一个管理槽，并写进 config.json。

    流程（任一步失败即中止，且不动原安装）：
      1. 体检来源（缺依赖/缺入口/空壳目录 → 直接拒绝，别复制一个坏槽）
      2. 规划槽位（目录名重名加数字后缀、端口避让、DSH_HOME 落在新槽内）
      3. 备份现有 config.json
      4. 备份原来的用户数据目录（.dsh-home / ~/.dsh）—— 备份失败就中止
      5. 复制 node_modules 到新槽（这一步最慢，几百 MB）+ 复制 node 运行时
      6. 把第 4 步的备份复制进新槽的 .dsh-home（原目录一个字节都不动）
      7. 写 config.json、生成三件 bat、最后对一遍「槽的形状」
    返回 (ok, 新槽 key, 说明)。"""
    p = progress or Progress()
    if adopt_mod is None:
        return (False, "", "缺少 dsh_adopt.py 模块，无法规划槽位")

    # ── 1) 体检来源 ────────────────────────────────────────────────────
    # 为什么必须体检：纳入的动作是「复制一份并登记」。来源若是半装/装坏的
    # （目录在、package.json 或 bin.js 缺失、依赖没装全），复制过去只会得到一个
    # **看起来正常但一启动就炸**的槽 —— 比直接报错更坏，因为用户会以为是管理器
    # 的启动逻辑有问题。所以这里宁可拒绝：坏的就该重新装（「安装官方版 dsh」）。
    info = adopt_mod.inspect_adopt_source(node_modules)
    if not info.get("ok"):
        return (False, "", "这个目录不能纳入（体检不通过）：\n  "
                + "\n  ".join(info.get("problems") or ["原因未知"])
                + "\n\n建议：改用「安装官方版 dsh」从官方源装一份完整的。")
    dsh_dir = info["dsh_dir"]
    ver = info["version"]

    existing = (read_config(config_path).get("versions") or {})
    if len(existing) >= max_slots:
        return (False, "", "槽位已满（%d/%d），请先删除不再使用的槽"
                % (len(existing), max_slots))

    # 注意：不要把 backup_root 当 temp_root 传下去 —— 那是「用户数据备份目录」，
    # 临时目录有自己的规范位置（temp_root，缺省 <槽根>/dsh-temp）。
    plan = adopt_mod.adopt_plan(node_modules, versions_root, slot_key,
                                taken_ports, node_exe_source=node_exe_source,
                                browser=browser, temp_root=temp_root)
    if not plan.get("ok"):
        return (False, "", plan.get("error") or "规划槽位失败")

    root = plan["root"]
    if not backup_root:
        base_for_backup = install_base or os.path.dirname(os.path.abspath(versions_root))
        backup_root = os.path.join(base_for_backup, "dsh备份")

    p.on_step("备份配置…")
    try:
        backup_config(config_path)
    except IOError as e:
        return (False, "", str(e))

    # ── 2) 备份原来的用户数据（在动任何东西之前）──────────────────────
    # 会话、设置、凭据都在 DSH_HOME 里，这是唯一的后悔药。备份失败就中止：
    # 宁可这次不纳入，也不能让用户的数据处于「只剩一份、还被换过位置」的状态。
    src_home = info.get("source_home") or ""
    src_backup = ""
    src_links = []
    if src_home:
        # 先记下原数据目录里的 junction（插件就靠它挂到 node_modules 上）。
        # 备份与复制都会**跳过**这些链接（跟进去会 942 MB / 14 分钟），
        # 所以必须单独收集，等新槽建好后再按新路径重建。
        try:
            src_links = adopt_mod.collect_links(src_home)
        except Exception as e:
            p.on_step("提示：读取原数据目录的链接失败（插件可能需要在管理器里重装）：%s" % e)
            src_links = []
        p.on_step("备份原来的用户数据（%s，%s）…" % (src_home, info.get("source_home_why") or ""))
        try:
            src_backup, berr = adopt_mod.backup_dsh_home(src_home, backup_root)
        except Exception as e:
            src_backup, berr = None, str(e)
        if not src_backup:
            return (False, "", "备份原来的用户数据失败，已中止（没有做任何改动）：\n%s"
                    % (berr or "原因未知"))
        p.on_step("已备份到 %s" % src_backup)

    # node 运行时：源可能是文件或目录；找不到就别硬来——没有独立 node 的槽
    # 与既有槽不同构，换版本时会有 ABI 风险，所以这里当硬性要求。
    if node_exe_source:
        p.on_step("复制 node 运行时…")
        node_exe, err = adopt_mod.copy_node_exe(node_exe_source, root)
        if not node_exe:
            return (False, "", err)
    else:
        node_exe = ""
        p.on_step("未找到可复用的 node 运行时，本槽将使用系统 node（换版本时留意 ABI）…")

    # ── 3) 先建数据目录、把备份的用户数据复制进来，再复制几百 MB 程序 ──────
    # 顺序是刻意的（2026-10-31 调整）：用户数据这一步最容易失败 —— 旧 dsh 没退
    # 干净时 `.dsh-home` 里的文件被锁住；而 `_cleanup_partial` 只在 node_modules
    # 还不存在时才肯删目录。放在程序复制之前，失败时才能真正做到"什么都不留下"。
    dsh_home = plan["dsh_home"]
    try:
        os.makedirs(root, exist_ok=True)
        os.makedirs(dsh_home, exist_ok=True)
        os.makedirs(plan["temp_dir"], exist_ok=True)
    except Exception as e:
        _cleanup_partial(root)
        return (False, "", "创建数据/临时目录失败（已清理未完成目录）：%s" % e)

    # 只复制（源目录与备份都不动）：新槽仍是独立的一份，用户打开还是原来的会话。
    # 复制完当场核对 —— 不完整就中止，不能"新槽看着正常、只少了几个会话"。
    data_note = ""
    if src_backup:
        p.on_step("把原来的会话/设置/凭据复制进新槽…")
        copy_fails = []
        try:
            adopt_mod.copy_tree_nolinks(src_backup, dsh_home, copy_fails)
            missing, _changed, total = adopt_mod.verify_copy(src_backup, dsh_home)
        except Exception as e:
            _cleanup_partial(root)
            return (False, "",
                    "把原用户数据复制进新槽失败，已中止（原数据与备份都没动）：%s\n"
                    "备份仍在：%s" % (e, src_backup))
        if missing or copy_fails:
            _cleanup_partial(root)
            return (False, "",
                    "原用户数据复制不完整，已中止（原数据与备份都没动）：\n"
                    "  · 应复制 %d 个文件，缺 %d 个%s\n"
                    "  · 拷不动 %d 个%s\n"
                    "常见原因：那个 dsh 还在运行，锁住了 .dsh-home 里的文件。"
                    "请先退出它再重试。\n备份仍在：%s"
                    % (total, len(missing),
                       ("（例：%s）" % missing[0]) if missing else "",
                       len(copy_fails),
                       ("（例：%s —— %s）" % copy_fails[0]) if copy_fails else "",
                       src_backup))
        data_note = "，原用户数据已带过来（备份在 %s）" % src_backup

    p.on_step("复制 dsh 程序与依赖（几百 MB，请稍候）…")
    try:
        shutil.copytree(node_modules, os.path.join(root, "node_modules"),
                        dirs_exist_ok=True)
    except Exception as e:
        _cleanup_partial(root)
        return (False, "", "复制程序目录失败（已清理未完成目录）：%s" % e)

    # 复制完再验一次：版本号必须与体检时读到的一致，入口文件必须在。
    # 复制几百 MB 时偶发"复制到一半中断/杀软锁文件"，不验就会登记一个坏槽。
    p.on_step("校验复制结果…")
    new_dsh_dir = os.path.join(root, "node_modules", "@deepseek-ai", "dsh")
    new_ver = adopt_mod.read_package_version(new_dsh_dir)
    if not new_ver:
        _cleanup_partial(root)
        return (False, "", "复制后读不到版本号（复制可能被中断，已清理未完成目录）。"
                           "请重试，或改用「安装官方版 dsh」。")
    if ver and new_ver != ver:
        _cleanup_partial(root)
        return (False, "", "复制后版本号不一致（体检时 %s，复制后 %s），已清理未完成目录。"
                % (ver, new_ver))
    if not os.path.isfile(os.path.join(new_dsh_dir, "lib", "bin.js")):
        _cleanup_partial(root)
        return (False, "", "复制后缺 lib/bin.js（复制不完整，已清理未完成目录）。")

    # 把被跳过的 junction 重建回来（插件就挂在这些链接上）。
    # 指向旧安装目录的改指新槽，指向槽外仓库的原样指回去。
    # 必须等 node_modules 复制完再建 —— 链接的目标就在里面。
    if src_backup and src_links:
        p.on_step("重建插件链接（%d 个）…" % len(src_links))
        old_root = os.path.dirname(os.path.abspath(node_modules))
        try:
            made, lost = adopt_mod.relink_home(dsh_home, src_links,
                                               old_root=old_root, new_root=root)
        except Exception as e:
            made, lost = [], [(r, t) for r, t in src_links]
            p.on_step("提示：重建插件链接失败：%s" % e)
        if made:
            data_note += "，%d 个插件链接已重建" % len(made)
        if lost:
            data_note += ("，%d 个链接的目标已不存在（插件可能要在管理器里重装）"
                          % len(lost))

    p.on_step("写入配置…")
    slot_cfg = adopt_mod.build_slot_config(
        slot_key=plan["slot_key"], label=plan["label"], root=root,
        dsh_home=dsh_home, temp_dir=plan["temp_dir"], port=plan["port"],
        node_exe=node_exe or "node", browser=browser, install_base=install_base)
    try:
        cfg = read_config(config_path)
        cfg.setdefault("versions", {})[plan["slot_key"]] = slot_cfg
        write_config_atomic(config_path, cfg)
    except Exception as e:
        _cleanup_partial(root)
        return (False, "", "写 config.json 失败（已清理未完成目录，原文件保持不变）：%s" % e)

    # ── 4) 与既有槽对齐：三件 bat + 形状校验 ──────────────────────────
    # 纳入出来的槽若缺 dsh.bat / dsh-cli.bat / kill-<port>.bat，用户双击目录
    # 会发现「别的槽都有，就这个没有」—— 那不是"规范纳入"。
    bat_note = ""
    try:
        bat_note = _gen_slot_bats(adopt_mod, slot_cfg, root, plan["slot_key"],
                               install_base)
    except Exception as e:
        bat_note = "（生成 bat 失败：%s；可在管理器里点「生成启动/kill/CLI bat」补）" % e

    shape = slot_shape_problems(slot_cfg, install_base or "",
                                os.path.expanduser("~"),
                                os.path.dirname(os.path.abspath(__file__)))
    soft = [t for lv, t in shape if lv != "错误"]
    hard = [t for lv, t in shape if lv == "错误"]
    for t in soft:
        p.on_step("提示：%s" % t)

    p.on_done(True, {
        "slot_key": plan["slot_key"],
        "label": plan["label"],
        "root": root,
        "port": plan["port"],
        "dsh_home": dsh_home,
        "backup": src_backup,
        "warnings": (info.get("warnings") or []) + soft,
    })
    tail = data_note + bat_note
    if info.get("warnings"):
        tail += "\n\n来源体检的提醒：\n  " + "\n  ".join(info["warnings"])
    if hard:
        tail += "\n\n注意：纳入后仍有问题：\n  " + "\n  ".join(hard)
    return (True, plan["slot_key"], "已纳入为槽 %s（%s，端口 %d）%s"
            % (plan["slot_key"], plan["label"], plan["port"], tail))


def _cleanup_partial(root):
    """删掉建到一半的槽目录。
    只删**本次新建**的目录名（带 -N 后缀的那些尤其要小心），所以额外确认
    它不在任何已登记的安装根里 —— 宁可删不掉留个残件，也不能误删用户数据。"""
    try:
        if not os.path.isdir(root):
            return
        marker = os.path.join(root, "node_modules")
        if not os.path.isdir(marker):
            shutil.rmtree(root, ignore_errors=True)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
# 功能二：一键装官方版（走官方 npm registry）
# ══════════════════════════════════════════════════════════════════

def _dir_is_empty_shell(path):
    """这个目录是不是「卸载残留的空壳」——一个文件都没有，只有空目录。

    为什么需要它（2026-10-30 故障反馈「为什么又产生了 dshV1-2」）：
    卸载最后一步是 `os.rmdir(槽根)`，但 Windows 上刚删完几万文件时目录常被
    索引器/杀软/资源管理器短暂占住，rmdir 会失败 → 留下一个只剩空 `.dsh-home`
    的壳。下一次装同名槽时 `unique_dir()` 看到名字被占就让位成 `dshV1-2`，
    用户看到的是"怎么又装了一个"。

    判定刻意保守：**只有零文件**才算空壳（空目录 + 不含任何链接）。
    只要里面有一个文件或一个 junction，就返回 False —— 宁可让位成 -2，
    也不能把可能有数据的目录删掉。
    """
    if not os.path.isdir(path):
        return False
    try:
        for cur, dirs, files in os.walk(path):
            if files:
                return False
            for d in dirs:
                p = os.path.join(cur, d)
                try:
                    st = os.stat(p, follow_symlinks=False)
                except OSError:
                    return False
                # FILE_ATTRIBUTE_REPARSE_POINT：junction / 符号链接也算"内容"
                if getattr(st, "st_file_attributes", 0) & 0x400:
                    return False
    except OSError:
        return False
    return True


def _reuse_empty_shell(versions_root, dir_name):
    """槽名被"空壳"占着时清掉它，把名字还回来。返回说明文字（没动就返回空串）。

    只删**确认零文件**的目录；删不掉就算了（照旧让位成 -2），绝不因为清不掉
    而让整个安装失败。"""
    p = os.path.join(versions_root, dir_name)
    if not os.path.exists(p) or not _dir_is_empty_shell(p):
        return ""
    try:
        shutil.rmtree(p)
    except Exception:
        return ""
    if os.path.exists(p):
        return ""
    return "%s 是上次卸载残留的空目录（没有任何文件），已清掉并复用这个槽名" % dir_name


def _drop_partial_install(root, dsh_home):
    """装失败后把「本次新建的槽目录」整个收回，别留下占名字的残件。

    为什么需要（2026-10-30 再次反馈「为什么又产生了 dshV1-2」）：
    `npm install` 失败时目录里**已经有文件了** —— `npm init -y` 生成的
    package.json（失败时可能还有半截 node_modules）。它因此不满足
    `_dir_is_empty_shell` 的「零文件」，`_reuse_empty_shell` 不会复用，
    用户一点「重试」就让位成 dshV1-2；再失败一次就是 dshV1-3。
    也就是说**失败后的重试才是 -2 的主要产地**，光靠安装前清理空壳堵不住。

    两条安全前提，缺一条就不动手：
      1. 确实没装成（`node_modules/@deepseek-ai/dsh/lib/bin.js` 不存在）；
      2. `.dsh-home` 里一个文件都没有（`_dir_is_empty_shell`）。
    有用户数据、或程序已经装好，一律不删。返回 True 表示确实删干净了。
    """
    if not root or not os.path.isdir(root):
        return False
    bin_js = os.path.join(root, "node_modules", "@deepseek-ai", "dsh", "lib", "bin.js")
    if os.path.isfile(bin_js):
        return False
    if dsh_home and os.path.isdir(dsh_home) and not _dir_is_empty_shell(dsh_home):
        return False
    try:
        shutil.rmtree(root)
    except Exception:
        # 刚跑完 npm 的目录常被杀软/索引器短暂占住：再试一次忽略错误
        shutil.rmtree(root, ignore_errors=True)
    return not os.path.exists(root)


def _unregister_slot(config_path, slot_key):
    """把刚登记、但最终没装成的槽从 config.json 里撤掉（失败不抛）。

    不撤的话界面会留一张指向已删目录的卡片，用户点「启动」必然报
    `[WinError 2] 系统找不到指定的文件` —— 正是 2026-10-30 那次
    「改名后连点三次启动都报错」的同款现象。
    """
    try:
        cfg = read_config(config_path)
        if slot_key in (cfg.get("versions") or {}):
            del cfg["versions"][slot_key]
            write_config_atomic(config_path, cfg)
            return True
    except Exception:
        pass
    return False


def run_official_install(adopt_mod, version_text, config_path, install_base,
                         slot_key, taken_ports, progress=None,
                         max_slots=5, browser="default", versions_root=None,
                         versions_names=None, temp_root=""):
    """装一个官方 dsh 版本到新槽（v2 / v3 / v4……），返回 (ok, slot_key, 说明)。

    顺序：建槽（写好路径与端口）→ 用 npm 装该版本 → 登记进 config.json。
    只有最后一步失败时才会留下一个装了程序的目录 —— 那是有意保留的：
    删掉等于白装一次，而它并不影响任何东西。

    `script_path` 参数已删除（2026-10-30）：安装不再调用任何 .ps1，
    内核改为 `_npm_install_slot`（直接 node.exe 跑 npm-cli.js）。
    """
    p = progress or Progress()

    if adopt_mod is None:
        return (False, "", "缺少 dsh_adopt.py 模块")
    # dist-tag（latest / next / beta / alpha）是 npm 的合法入参，安装内核
    # 也直接支持（`@deepseek-ai/dsh@latest`）。用户从版本列表里可能直接手填标签，
    # 这里不能按"版本号格式不对"打回去 —— 装完会用 package.json 的真实版本回填 label。
    _tag = (version_text or "").strip().lower()
    if _tag in ("latest", "next", "beta", "alpha"):
        ver = _tag
    else:
        ver = adopt_mod.normalize_version(version_text)
        if not ver:
            return (False, "", "版本号「%s」不符合官方格式。%s"
                    % (version_text, adopt_mod.version_hint()))

    existing = (read_config(config_path).get("versions") or {})
    if len(existing) >= max_slots:
        return (False, "", "槽位已满（%d/%d）。管理器最多管理 %d 个版本，"
                          "请先在 config.json 里删掉不再使用的槽。"
                % (len(existing), max_slots, max_slots))

    versions_root = versions_root or os.path.join(install_base, "dsh-versions")
    versions_names = versions_names or set()
    dir_name = "dsh%s" % slot_key.upper()
    # 先看看这个名字是不是被"卸载残留的空壳"占着 —— 是就清掉复用，
    # 而不是让位成 dshV1-2（用户会以为又凭空多装了一个）。
    reuse_note = _reuse_empty_shell(versions_root, dir_name)
    root, dir_name = adopt_mod.unique_dir(versions_root, dir_name)
    if versions_names and dir_name in versions_names:
        return (False, "", "槽名 %s 已在使用" % dir_name)
    if not root:
        return (False, "", "无法在 %s 下分配目录名" % versions_root)

    port = adopt_mod.suggest_port(int(slot_key[1:]), taken_ports)
    if not port:
        return (False, "", "4080 起找不到空闲端口，请手工在 config.json 里指定")

    # 目录重名而加后缀时，同一个 slot_key 会算出同一个端口 —— 新槽就与旧槽
    # 撞端口了（两个 dsh 抢同一个监听口，后启动的那个直接失败，而且界面只显示
    # "端口被非 dsh 程序占用"，极难自查）。所以把已分配的端口一并算进去。
    port = adopt_mod.suggest_port(int(slot_key[1:]),
                                 list(taken_ports) + [p for _k, p in
                                                     ((k, v.get("port"))
                                                      for k, v in (existing or {}).items())
                                                     if p])

    dsh_home = os.path.join(root, ".dsh-home")
    # 临时目录（2026-10-31 改）：优先 `<temp_root>\<槽目录名>`，temp_root 缺省是
    # `<槽根>/dsh-temp` —— 即「临时文件跟着安装根走」，换盘/换 install_base 时
    # 一起搬。没给 temp_root 时才退回老规则（<临时目录根>\<槽>），再不行槽内 .tmp。
    temp_dir = adopt_mod.default_temp_dir(dir_name, root, temp_root=temp_root)
    bin_js = os.path.join(root, "node_modules", "@deepseek-ai", "dsh", "lib", "bin.js")

    p.on_step("准备槽 %s（%s，端口 %d）…" % (slot_key, dir_name, port))
    if reuse_note:
        p.on_step("提示：%s" % reuse_note)
    try:
        os.makedirs(dsh_home, exist_ok=True)
        os.makedirs(temp_dir, exist_ok=True)
    except Exception as e:
        return (False, "", "创建目录失败：%s" % e)

    # ── node 必须**复制**一份进新槽，不能只引用别槽的 ──────────────────
    # 隔离的设计前提是「每槽自带 node」：换版本时用本槽的 node 跑 npm，
    # node-pty 这类按 ABI 编译的原生模块才不会崩；别槽被删/被换时本槽也不受影响。
    # 实测缺陷：原先只把别槽路径填进 config，磁盘上并没有 node 目录 ——
    # 新槽于是缺 node/（与其它槽不一致），也拿不到隔离收益。
    node_exe = ""
    node_src = _pick_node_exe(existing, install_base, os.path.expanduser("~"))
    if node_src and os.path.isfile(node_src):
        p.on_step("复制 node 运行时到本槽（每槽独立，ABI 稳定）…")
        node_exe, nerr = adopt_mod.copy_node_exe(node_src, root)
        if not node_exe:
            return (False, "", "复制 node 运行时失败：%s" % nerr)
    else:
        # 首槽 / 既有槽都没自带 node 时才退回系统 node，并说清代价，
        # 别让用户以为拿到了完整隔离。
        node_exe = "node"
        p.on_step("未找到可复用的 node 运行时，本槽将使用系统 node"
                  "（换版本若报原生模块错误，请为本槽补一份 node）")

    slot_cfg = adopt_mod.build_slot_config(
        slot_key=slot_key, label=dir_name, root=root, dsh_home=dsh_home,
        temp_dir=temp_dir, port=port, node_exe=node_exe, browser=browser,
        install_base=install_base)

    p.on_step("备份配置…")
    try:
        backup_config(config_path)
    except IOError as e:
        _drop_partial_install(root, dsh_home)
        return (False, "", str(e))

    # 先登记：安装内核要按槽目录落 node_modules，而槽的端口/路径/
    # 数据目录都在 config 里 —— 先登记，装完就只差「校验」一步。
    p.on_step("登记槽位…")
    try:
        cfg = read_config(config_path)
        cfg.setdefault("versions", {})[slot_key] = slot_cfg
        write_config_atomic(config_path, cfg)
    except Exception as e:
        _drop_partial_install(root, dsh_home)
        return (False, "", "写 config.json 失败：%s" % e)

    p.on_step("从官方仓库安装 %s（走 npm registry，可能要几分钟）…" % ver)
    rc, out = _npm_install_slot(node_exe, root, ver, progress=p)

    if rc != 0:
        hint = ""
        if rc < 0:
            hint = ("\n\n负数退出码多为网络类错误（-4077=ECONNRESET 连接被重置、"
                    "-4078=ECONNREFUSED 等），稍后重试通常可恢复。")
        # 失败当场收回残件：否则 package.json（npm init -y 的产物）会让这个目录
        # 不再算"空壳"，下次重试就变成 dshV1-2。详见 _drop_partial_install。
        cleaned = _drop_partial_install(root, dsh_home)
        if cleaned:
            _unregister_slot(config_path, slot_key)
            hint += ("\n\n本次未完成的目录已清理（槽名 %s 已还回来），"
                     "直接重试即可，不会再多出一个 %s-2。" % (dir_name, dir_name))
        return (False, "", "安装失败（退出码 %d）。最后几行输出：\n%s%s"
                % (rc, _tail(out), hint))

    p.on_step("校验安装结果…")
    if not os.path.isfile(bin_js):
        cleaned = _drop_partial_install(root, dsh_home)
        if cleaned:
            _unregister_slot(config_path, slot_key)
        return (False, slot_key, "安装结束但没找到 %s。请看日志里的 npm 输出，"
                                 "多半是网络或镜像问题。%s"
                % (bin_js,
                   "未完成的目录已清理，可直接重试。" if cleaned
                   else "配置已保留，可重试安装。"))

    # ── 自带 node：必须和既有槽一样有一份 ──────────────────────────────
    # 2026-10-29 实测：_pick_node_exe 拿占位符去 isfile() 恒为 False，于是每个
    # 新槽都退回系统 node —— 目录里没有 node\、dsh.bat 写裸 "node"，违反
    # 「每槽自带 node」的隔离设计。根因已修，这里再兜一道：真缺就补一份。
    node_note = ""
    if not (node_exe and node_exe.strip().lower() != "node"
            and os.path.isfile(node_exe)):
        src = _pick_node_exe(read_config(config_path).get("versions") or {},
                             install_base, os.path.expanduser("~"))
        if src and src.strip().lower() != "node" and os.path.isfile(src):
            p.on_step("补一份自带 node 运行时到本槽（每槽独立，ABI 稳定）…")
            fixed, nerr = adopt_mod.copy_node_exe(src, root)
            if fixed:
                node_exe = fixed
                slot_cfg["node_exe"] = adopt_mod.portable_path(fixed, install_base)
                try:
                    _c = read_config(config_path)
                    _c["versions"][slot_key]["node_exe"] = slot_cfg["node_exe"]
                    write_config_atomic(config_path, _c)
                except Exception as e:
                    node_note = "（node 已复制，但写回 config 失败：%s）" % e
            else:
                node_note = "（补 node 失败：%s）" % nerr
        else:
            node_note = "（找不到可复用的 node 运行时，本槽将用系统 node，无 ABI 隔离）"

    # 装完把真实版本号报出来，但**不改 label**。
    #
    # label 的约定是「固定槽名」（dshV4），真实版本由界面单独显示成「实装 X」——
    # 主文件的 _label_sync 也是这个约定（看到 dshV\d+ 就跳过同步）。以前这里
    # 无条件把 label 写成版本号，于是新装的槽标题是 "0.2.2-alpha.2"、老槽是 "dshV1"，
    # 一眼就看得出「这个槽是另装的」。
    #
    # 取版本号**不能靠数 dirname 层数** —— 这里踩过坑：
    # bin_js = <root>/node_modules/@deepseek-ai/dsh/lib/bin.js
    # 上溯 2 层才是 dsh 包目录；曾写成 5 层，正好落到槽根，读到 npm install
    # 生成的 package.json（version 恒为 1.0.0），于是界面显示 "1.0.0"。
    # 现在改成按 @deepseek-ai\dsh 标记反查，层数变化也不会错。
    ver_note = ""
    try:
        real_ver = adopt_mod.read_package_version(dsh_package_dir(bin_js)) or ver
        if real_ver != ver:
            p.on_step("注意：实际装到 %s（请求的是 %s）" % (real_ver, ver))
            ver_note = "（实际装到 %s）" % real_ver
    except Exception:
        pass        # 只是显示名，失败不影响使用

    # 装完顺手生成本槽的 bat，让新槽与既有槽**完全一致**。
    # 为什么必须做：一键装出来的槽若缺 dsh.bat / kill-*.bat / dsh-cli.bat，
    # 用户双击目录里的 bat 会发现「别的槽都有，就这个没有」，
    # 像是装坏了；而且 bat 里的 DSH_HOME/TEMP/端口是与 GUI 启动同源的，
    # 缺了就等于少一条启动路径。
    # 失败只提示不阻断 —— bat 可以事后在管理器里点「生成启动/kill/CLI bat」补。
    bat_note = ""
    try:
        bat_note = _gen_slot_bats(adopt_mod, slot_cfg, root, slot_key, install_base)
    except Exception as e:
        bat_note = "（生成 bat 失败：%s；可在管理器里点「生成启动/kill/CLI bat」补）" % e

    # ── 最后再对一遍「形状」────────────────────────────────────────────
    # 判据不是「npm 退出码 0」，而是「和既有槽长得一样」：自带 node、依赖齐全、
    # 数据/临时目录就位、三件 bat 都在。少一项就明确说出来 —— 否则用户只会
    # 某天发现「别的槽都有 node\，就这个没有」。
    shape = slot_shape_problems(slot_cfg, install_base,
                                os.path.expanduser("~"),
                                os.path.dirname(os.path.abspath(__file__)))
    hard = [t for lv, t in shape if lv == "错误"]
    soft = [t for lv, t in shape if lv != "错误"]
    if hard:
        return (False, slot_key,
                "安装结束，但槽结构和其它槽不一致（配置已保留，可重试）：\n  "
                + "\n  ".join(hard))
    for t in soft:
        p.on_step("提示：%s" % t)

    p.on_done(True, {"slot_key": slot_key, "label": dir_name, "root": root,
                     "port": port, "version": ver})
    tail = "%s%s%s%s" % (reuse_note and "（%s）" % reuse_note,
                         ver_note, node_note, bat_note)
    if soft:
        tail += "\n\n以下与其它槽不同，需要时可手工补齐：\n  " + "\n  ".join(soft)
    return (True, slot_key, "已安装官方版 %s 到槽 %s（%s，端口 %d）%s"
            % (ver, slot_key, dir_name, port, tail))


def _gen_slot_bats(adopt_mod, slot_cfg, root, slot_key, install_base=""):
    """为单个槽生成 dsh.bat / kill-<port>.bat / dsh-cli.bat。

    复用管理器主程序里的 _bat_text_* 三件套，保证与 GUI 生成的**逐字一致**
    （隔离环境、端口预检、CRLF、% 转义都相同）。所以这里从主程序 import，
    而不是复制一份实现 —— 复制就会出现两套 bat 生成逻辑慢慢分叉。

    **`install_base` 不能省**：config 里存的是 `<base>/<home>` 占位符，而 .bat
    是给 cmd.exe 读的，它不认占位符。2026-10-30 实测：装出来的槽，bat 里写着
    `set "DSH_HOME=<base>/dsh-versions/dshV1/.dsh-home"` —— 双击会在当前目录下
    建一个名叫 `<base>` 的目录，然后启动失败。这里必须先展开成绝对路径。

    拿不到主程序模块时（单独跑本脚本）返回提示而不是抛错。
    """
    # 本文件就在仓库根（与 dsh_manager.pyw 同级），不要再上退一层。
    mgr = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "dsh_manager.pyw")
    if not os.path.isfile(mgr):
        return ""
    try:
        # 只抽取三个纯函数，不执行主程序（它有 GUI 副作用）
        src = open(mgr, encoding="utf-8").read()
        tree = __import__("ast").parse(src)
        keep = [n for n in tree.body
                if isinstance(n, __import__("ast").FunctionDef)
                and n.name in ("_bat_text_start", "_bat_text_cli", "_bat_text_kill")]
        if len(keep) != 3:
            return ""
        # 展开 <base>/<home>/<self>/<npm_global> 后再交给 bat 文本函数。
        cfg = dict(slot_cfg)
        for k in ("node_exe", "bin_js", "dsh_home", "temp_dir"):
            if isinstance(cfg.get(k), str):
                cfg[k] = _expand_slot_path(cfg[k], install_base)
        ns = {"os": os, "re": __import__("re"),
              "SLOT_KEYS": [slot_key],
              "VC": {slot_key: cfg},
              "BASE_DIR": os.path.dirname(mgr)}
        exec(compile(__import__("ast").Module(body=keep, type_ignores=[]),
                     "<bats>", "exec"), ns)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        made = []
        jobs = [
            ("dsh.bat", ns["_bat_text_start"](slot_key, cfg, root)),
            ("kill-%s.bat" % cfg["port"],
             ns["_bat_text_kill"](slot_key, cfg, root)),
            ("dsh-cli.bat", ns["_bat_text_cli"](slot_key, cfg, root)),
        ]
        for name, text in jobs:
            path = os.path.join(root, name)
            if os.path.exists(path):
                try:
                    shutil.copyfile(path, "%s.bak-%s" % (path, stamp))
                except Exception:
                    pass
            # GBK：cmd.exe 读 .bat 用 ANSI 代码页（中文 Windows=936），不认 UTF-8。
            # 写 UTF-8 会让中文注释变乱码，且相邻中文 REM 行会被按 GBK 的字节边界
            # 撕裂行 —— 实测导致 dsh-cli.bat 双击「命令语法不正确」rc=255、窗口一闪就没。
            # 与 dsh_manager.pyw 的 _write_bat 保持一致（那边有完整踩坑记录）。
            with open(path, "w", encoding="gbk", newline="") as f:
                f.write(text)
            made.append(name)
        return "，并生成了 %s" % "、".join(made)
    except Exception as e:
        # 静默失败过一次（路径多退一层找不到主程序），排查时毫无线索。
        # 这里把原因带回给调用方，由它写进用户可见的提示/日志。
        return "（未能生成 bat：%s；可在管理器里点「生成启动/kill/CLI bat」补）" % e


def dsh_package_dir(bin_js):
    """从 dsh 入口路径反推 **@deepseek-ai/dsh 包目录**。

    为什么不数 dirname 层数：入口形如
        <root>/node_modules/@deepseek-ai/dsh/lib/bin.js
    到包目录是 2 层；但dsh 改目录结构时层数就会变。本项目曾把它写成 5 层，
    结果正好落到槽根，读到 npm install 生成的 package.json（version 恒 1.0.0），
    界面显示成"1.0.0"。这里改为按 @deepseek-ai\\dsh 标记**反查**：
    向上找到含 @deepseek-ai 的那一段，其下的 dsh/ 即包目录。
    找不到就退回"2 层"的常规布局。
    """
    p = os.path.abspath(bin_js)
    # 逐级上溯，找 "…\\@deepseek-ai\\dsh\\" 这一段
    cur = p
    for _ in range(10):
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        if os.path.basename(parent).lower() == "@deepseek-ai":
            cand = os.path.join(parent, "dsh")
            if os.path.isdir(cand):
                return cand
        cur = parent
    return os.path.dirname(os.path.dirname(p))       # 常规布局兜底


def _expand_slot_path(p, base="", home=""):
    """展开 config.json 里的 <base> / <home> / <self> / <npm_global> 占位符。

    2026-10-29 实测踩到：config.json 存的是可移植占位符
    （`<base>/dsh-versions/dshV1/node/node.exe`），而 `_pick_node_exe` 直接拿它
    去 `os.path.isfile()` —— 恒为 False，于是每个新槽都退回系统 `node`，
    装出来的目录里没有 `node/`，与既有槽不一致、还依赖 PATH 上有 node。
    """
    s = str(p or "").strip()
    if not s:
        return ""
    s = s.replace("<base>", base or "")
    s = s.replace("<home>", home or os.path.expanduser("~"))
    s = s.replace("<self>", os.path.dirname(os.path.abspath(__file__)))
    s = s.replace("<npm_global>", os.path.join(
        os.environ.get("APPDATA", ""), "npm"))
    return os.path.expandvars(s)


def slot_dir_name(cfg, base="", home=""):
    """从槽配置反推**槽目录名**（`dshV1` / `dshV1-2` 这种）。

    为什么从 dsh_home 反推而不是用 label：label 是给人看的（可以被改成
    「正式版」），而目录名是磁盘事实。dsh_home 恒为 `<槽根>/.dsh-home`，
    所以 basename(dirname(dsh_home)) 就是槽目录名。
    """
    for key in ("dsh_home", "bin_js"):
        p = _expand_slot_path(cfg.get(key), base, home)
        if not p:
            continue
        d = os.path.dirname(os.path.normpath(p))
        if key == "bin_js":
            # …/dshV1/node_modules/@deepseek-ai/dsh/lib/bin.js → 上溯到 node_modules 的父目录
            for _ in range(8):
                if os.path.basename(d).lower() == "node_modules":
                    d = os.path.dirname(d)
                    break
                nd = os.path.dirname(d)
                if nd == d:
                    break
                d = nd
        if os.path.basename(d):
            return os.path.basename(d)
    return str(cfg.get("label") or "")


def plan_temp_relocation(config, install_base, temp_root, home=""):
    """算出「把每个槽的临时目录搬到 `<temp_root>/<槽目录名>`」的变更清单。

    纯计算，不写盘 —— 便于离线回归断言规则，也便于界面先预览再执行。

    返回 `[{key, dir_name, old, new, old_abs, new_abs}, ...]`（new 是写进
    config 的形式：落在 install_base 之下就写成 `<base>/…` 占位符）。
    """
    out = []
    versions = (config or {}).get("versions") or {}
    for key, cfg in versions.items():
        if not isinstance(cfg, dict):
            continue
        dir_name = slot_dir_name(cfg, install_base, home) or key
        new_abs = os.path.join(temp_root, dir_name)
        # 写回形式：install_base 之下 → <base>/…（跟着安装根一起搬）
        b = str(install_base or "").replace("\\", "/").rstrip("/")
        n = new_abs.replace("\\", "/")
        if b and (n.lower() == b.lower() or n.lower().startswith(b.lower() + "/")):
            new = "<base>" + n[len(b):]
        else:
            new = n
        out.append({
            "key": key,
            "dir_name": dir_name,
            "old": str(cfg.get("temp_dir") or ""),
            "new": new,
            "old_abs": _expand_slot_path(cfg.get("temp_dir"), install_base, home),
            "new_abs": new_abs,
        })
    return out


def apply_temp_relocation(config_path, changes):
    """把 `plan_temp_relocation` 的结果写进 config.json，并建好新目录。

    **只改 temp_dir 一个字段**（读-改-写 + 原子写），其它字段一字节不动 ——
    整份覆盖会抹掉 versions 里的 node_exe / browser / extra_args。
    旧临时目录里的文件**不搬**：那是每次启动重建的垃圾，复制既慢又没意义；
    旧目录原样留着，删不删由用户决定（体检会报它占多少）。
    返回 (ok, 说明)。
    """
    try:
        cfg = read_config(config_path)
    except Exception as e:
        return (False, "读 config.json 失败：%s" % e)
    versions = cfg.get("versions") or {}
    changed = 0
    for ch in changes:
        slot = versions.get(ch["key"])
        if not isinstance(slot, dict):
            continue
        if slot.get("temp_dir") == ch["new"]:
            continue
        slot["temp_dir"] = ch["new"]
        changed += 1
    try:
        for ch in changes:
            os.makedirs(ch["new_abs"], exist_ok=True)
    except Exception as e:
        return (False, "创建临时目录失败：%s" % e)
    if changed:
        try:
            write_config_atomic(config_path, cfg)
        except Exception as e:
            return (False, "写 config.json 失败：%s" % e)
    return (True, "已更新 %d 个槽的临时目录" % changed)


def bundled_node_exe():
    """管理器包内自带的 node：`<管理器目录>/runtime/node/node.exe`，没有就返回空串。

    为什么包里要带一份（2026-10-31 用户拍板）：新电脑第一次装 dsh 时，
    既没有既有槽可借（`_pick_node_exe` 只从已有槽找），系统也未必装过 node ——
    那时只能退回裸 `"node"` 交给 PATH，PATH 上也没有就当场失败，报的还是
    「找不到可用的 node.exe」，用户完全不知道要先装 Node.js。
    带一份等于「解压即用」，用户全程不用管 node。

    版本随管理器发版更新：dsh 对 node 的要求变化不频繁（实测 0.1.5-rc.2 到
    0.2.2-alpha.2 都能跑同一个 v24.21.0），等它真要求更高版本时再随新版管理器换。
    目录来自官方 win-x64 发行包，**必须连 `node_modules/npm` 一起带**，否则装不了东西。
    """
    exe = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "runtime", "node", "node.exe")
    return exe if os.path.isfile(exe) else ""


def _pick_node_exe(existing_slots, base="", home=""):
    """挑一个 node.exe 给新槽：优先用既有槽里已验证能跑的，其次用包里自带的。

    为什么「已有槽」优先于「包内」：新槽与老槽同构（同一份 node、同一 ABI），
    换版本时 node-pty 这类原生模块才不会因为 ABI 不匹配崩掉。包内那份是
    **首槽 / 所有槽的 node 都坏了**时的兜底 —— 它保证新电脑上至少有一条活路。
    两者都没有才退回裸 `"node"` 交给 PATH（最后兜底，调用方会提示代价）。

    `base`/`home` 用来展开占位符；不传也能工作（只认绝对路径的老配置），
    但那样占位符配置会被漏掉 —— 调用方应当传。
    """
    for _k, v in (existing_slots or {}).items():
        p = _expand_slot_path((v or {}).get("node_exe"), base, home)
        if p and os.path.isfile(p):
            return p
    bundled = bundled_node_exe()
    if bundled:
        return bundled
    return "node"       # 兜底：交给脚本/系统去解析


def slot_root_from_bin_js(bin_js):
    """从 bin_js 反推槽根目录：<root>/node_modules/@deepseek-ai/dsh/lib/bin.js → <root>。
    不是本地布局（如 npm 全局安装）时返回空串。"""
    p = str(bin_js or "").replace("\\", "/")
    i = p.lower().find("/node_modules/")
    if i <= 0:
        return ""
    return p[:i].replace("/", os.sep)


def slot_shape_problems(cfg, base="", home="", self_dir=""):
    """把一个槽「该有的样子」逐项对一遍，返回 [(级别, 说明), ...]。

    判据就是「一个槽该有的样子」：自带 node、node_modules 里有
    @deepseek-ai/dsh/lib/bin.js、.dsh-home、专属临时目录、三件 bat。
    级别约定：**错误** = 起不来；**警告** = 能跑但与其它槽不同构。

    为什么要有它：以前只校验 bin.js 一个文件，于是「缺 node 目录的槽」也能装成功，
    用户要到某天系统 node 被卸掉、或者换版本报原生模块错时才发现。
    """
    cfg = cfg or {}
    out = []
    bin_js = _expand_slot_path(cfg.get("bin_js"), base, home)
    node = _expand_slot_path(cfg.get("node_exe"), base, home)
    dsh_home = _expand_slot_path(cfg.get("dsh_home"), base, home)
    temp = _expand_slot_path(cfg.get("temp_dir"), base, home)
    root = slot_root_from_bin_js(bin_js)

    if not bin_js:
        out.append(("错误", "bin_js 没配"))
    elif not os.path.isfile(bin_js):
        out.append(("错误", "bin_js 不存在：%s" % bin_js))

    if not node:
        out.append(("错误", "node_exe 没配"))
    elif node.strip().lower() == "node":
        out.append(("警告", "用的是系统 node（没有自带 node\\，与其它槽不同构）"))
    elif not os.path.isfile(node):
        out.append(("错误", "node_exe 不存在：%s" % node))

    if not dsh_home:
        out.append(("错误", "dsh_home 没配"))
    elif not os.path.isdir(dsh_home):
        out.append(("警告", "数据目录还不存在（首次启动会创建）：%s" % dsh_home))

    if not temp:
        out.append(("警告", "temp_dir 没配（会继承系统 TEMP，隔离性变差）"))
    elif not os.path.isdir(temp):
        out.append(("警告", "临时目录还不存在（启动前会创建）：%s" % temp))

    if root:
        for n in ("dsh.bat", "dsh-cli.bat"):
            if not os.path.isfile(os.path.join(root, n)):
                out.append(("警告", "缺 %s（可在管理器里点「生成启动/kill/CLI bat」补）" % n))
        port = cfg.get("port")
        if port and not os.path.isfile(os.path.join(root, "kill-%s.bat" % port)):
            out.append(("警告", "缺 kill-%s.bat（同上，可在管理器里补）" % port))
    else:
        out.append(("警告", "bin_js 不在本地 node_modules 布局里，无法检查槽目录内容"))
    return out


# ══════════════════════════════════════════════════════════════════
# npm 直调（装官方版的内核，**不依赖任何 .ps1**）
# ══════════════════════════════════════════════════════════════════
# 2026-10-30 事故：移除「换版本」时把 scripts/switch-version.ps1 整个删了，
# 但安装流程也在调它 → 点「安装官方版」直接 [WinError 2] 找不到文件。
# 现在这段逻辑搬到 Python 里，安装不再有任何脚本依赖（也少一层 powershell
# 启动开销与执行策略风险）。等价能力逐条保留：
#   ① 用**本槽的 node** 直跑 npm-cli.js（不是 PATH 里的 npm.cmd）
#      —— npm.cmd 会派生 node 孙进程继承管道，pythonw 无控制台时会永久阻塞；
#      且本槽 node 能保证 node-pty 按同 ABI 编译。
#   ② 装前 npm view 预检版本存在性，默认源网络故障自动回退镜像（仅本次生效）。
#   ③ npm init -y（槽根没有 package.json 时）+ npm install <spec>。

FALLBACK_REGISTRY = "https://registry.npmmirror.com"


def _npm_cli(node_exe):
    """定位 node 发行包自带的 npm-cli.js。

    为什么不调 npm / npm.cmd：PATH 上的 npm 可能属于别的发行版（甚至被
    执行策略拦下的 npm.ps1），而 npm.cmd 的孙进程管道在 pythonw 下会卡死。
    返回 (node_exe, npm_cli)；找不到 npm_cli 时第二项为空串。"""
    exe = (node_exe or "").strip()
    if exe and not os.path.dirname(exe):
        # 裸名字（"node"）：交给 PATH 解析 —— 这是 config 里 node_exe 的合法写法。
        exe = shutil.which(exe) or ""
    if not exe or not os.path.isfile(exe):
        # 给了路径但文件不在：**不**静默换成 PATH 上别的 node。
        # 换成不同版本的 node，node-pty 这类按 ABI 编译的原生模块会直接崩。
        return ("", "")
    cli = os.path.join(os.path.dirname(exe), "node_modules", "npm",
                       "bin", "npm-cli.js")
    return (exe, cli if os.path.isfile(cli) else "")


def _npm_env(node_exe):
    """把本槽 node 目录前置进 PATH（只影响本次子进程，不改系统环境）。"""
    env = dict(os.environ)
    if node_exe and os.path.isfile(node_exe):
        env["PATH"] = os.path.dirname(node_exe) + os.pathsep + env.get("PATH", "")
    return env


def _run_npm(node_exe, npm_cli, args, cwd=None):
    """跑一次 npm，返回 (rc, 输出文本)。

    输出重定向到临时文件再读，而不是用管道：pythonw 没有控制台时，
    npm → node 孙进程会持有管道不放，直接读会永久阻塞（主文件里
    _probe_npm_global 为此踩过坑）。
    退出码沿用 npm 语义：0 成功 / 正数失败 / **负数 = 网络类故障**
    （libuv errno，如 -4077=ECONNRESET），据此决定要不要换镜像重试。"""
    tmp = os.path.join(os.environ.get("TEMP", "."),
                       "dsh-install-%d-%d.log" % (os.getpid(), int(time.time())))
    cmd = [node_exe, npm_cli] + list(args)
    try:
        with open(tmp, "wb") as out:
            proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT,
                                    cwd=cwd or None, env=_npm_env(node_exe),
                                    creationflags=getattr(subprocess,
                                                          "CREATE_NO_WINDOW", 0))
            rc = proc.wait()
    except Exception as e:
        return (-1, "无法启动 npm（%s）：%s" % (node_exe, e))
    try:
        with open(tmp, "rb") as f:
            raw = f.read()
    except Exception:
        raw = b""
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
    for enc in ("utf-8", "gbk"):
        try:
            return (rc, raw.decode(enc))
        except UnicodeDecodeError:
            continue
    return (rc, raw.decode("utf-8", "replace"))


def _pick_registry(node_exe, npm_cli, spec):
    """npm view 预检：确认 <spec> 存在，并挑出可用的 registry。

    返回 (ok, registry, 说明)。registry 为空串表示用 npm 默认源。
    为什么先预检而不是直接 install：默认源 ECONNRESET 时 npm 会自己重试很久
    （实测 3.5 小时），预检几秒就能发现并改走镜像；版本号打错也能当场报出来，
    而不是等下载完才失败。"""
    tried = []
    for reg in ("", FALLBACK_REGISTRY):
        rc, out = _run_npm(node_exe, npm_cli,
                           ["view", spec, "version"] +
                           (["--registry", reg] if reg else []))
        name = reg or "npm 默认源"
        tried.append(name)
        if rc == 0:
            return (True, reg, "")
        if rc < 0:
            continue        # 网络类故障 → 换下一个源
        # rc > 0：版本不存在（E404/ETARGET）。换源也没用，直接报。
        return (False, "", "npm 上找不到 %s（%s 返回 %d）。\n%s\n"
                           "请确认版本号是否写对（可用 npm view @deepseek-ai/dsh "
                           "versions 查全部版本）。"
                % (spec, name, rc, _tail(out, 5)))
    return (False, "", "npm 预检失败：%s 均不可达（断网 / 代理 / npm 源故障）。\n"
                       "网络类报错多为临时性，稍后重试即可。" % " 与 ".join(tried))


def _npm_install_slot(node_exe, slot_dir, version, progress=None):
    """把 @deepseek-ai/dsh@<version> 装进 slot_dir（本地 npm install，非 -g）。

    返回 (rc, 输出文本)。测试用替身换掉本函数即可离线驱动各分支。"""
    p = progress or Progress()
    exe, cli = _npm_cli(node_exe)
    if not exe:
        return (-1, "找不到可用的 node.exe（既没有本槽自带的，PATH 上也没有）")
    if not cli:
        return (-1, "在 %s 旁边找不到 node_modules/npm/bin/npm-cli.js —— "
                    "这份 node 运行时不完整，请换一份自带 npm 的 node。"
                % os.path.dirname(exe))
    if not os.path.isdir(slot_dir):
        return (-1, "槽目录不存在：%s" % slot_dir)

    spec = "@deepseek-ai/dsh@%s" % version
    p.on_step("预检 %s 是否存在（顺带探测可用 npm 源）…" % spec)
    ok, reg, why = _pick_registry(exe, cli, spec)
    if not ok:
        return (1, why)
    if reg:
        p.on_step("默认源不可达，本次安装改用镜像 %s（仅本次生效，不改全局配置）" % reg)

    # 槽根没有 package.json 时先 npm init -y：否则 npm 会往上找父目录的
    # package.json，把依赖装到槽外去（多槽共用同一父目录时必然踩）。
    if not os.path.isfile(os.path.join(slot_dir, "package.json")):
        p.on_step("初始化槽目录（npm init -y）…")
        rc, out = _run_npm(exe, cli, ["init", "-y"], cwd=slot_dir)
        if rc != 0:
            return (rc, "npm init -y 失败（退出码 %d）：\n%s" % (rc, _tail(out)))

    p.on_step("下载并安装 %s（要几分钟，请勿关闭管理器）…" % spec)
    args = ["install", spec] + (["--registry", reg] if reg else [])
    rc, out = _run_npm(exe, cli, args, cwd=slot_dir)
    if rc < 0 and not reg:
        # 网络类失败（负数退出码）→ 换镜像重试一次，仅本次命令生效。
        p.on_step("默认源网络故障（退出码 %d），改用镜像 %s 重试一次…"
                  % (rc, FALLBACK_REGISTRY))
        rc, out = _run_npm(exe, cli, ["install", spec,
                                      "--registry", FALLBACK_REGISTRY],
                           cwd=slot_dir)
    return (rc, out)


def _tail(text, n=8):
    lines = [l for l in (text or "").splitlines() if l.strip()]
    return "\n".join(lines[-n:]) or "(无输出)"


def run_in_thread(fn, progress, done_callback):
    """把长任务丢后台线程。
    注意：回调里刷新界面必须由调用方切回主线程（tkinter 不线程安全），
    这里只负责不让界面卡住。"""
    def wrapper():
        try:
            result = fn()
        except Exception as e:
            if progress:
                progress.on_done(False, None, "意外错误：%s" % e)
            if done_callback:
                done_callback(False, None, str(e))
            return
        ok = bool(result and result[0])
        msg = result[2] if result else ""
        payload = result[1] if ok and result else None
        if progress:
            progress.on_done(ok, payload, msg if not ok else "")
        if done_callback:
            done_callback(ok, payload, msg)

    t = threading.Thread(target=wrapper, daemon=True)
    t.start()
    return t