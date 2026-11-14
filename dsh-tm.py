#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""dsh-tm 命令行接口（CLI）。

设计原则（2026-11-01）
----------------------
1. **不导入 dsh_manager.pyw**。那个文件是 GUI 入口，模块级就会建窗口、
   起线程、弹对话框；CLI 必须能在无图形环境（含 CI / 计划任务）里跑。
   所以这里只复用**纯逻辑**模块：dsh_kill / dsh_health / dsh_registry。
2. **与 GUI 用同一份 config.json、同一套隔离语义**（DSH_HOME / TEMP / 端口）。
   启动参数直接从 config 的 extra_args 取，不另造一套。
3. **只做 GUI 做不到或做起来笨的事**：批量启动、脚本化查询状态。
   单槽启停在界面上点一下更直观，CLI 也提供但主要用于自动化。
4. **CLI 拿不到入口 token**：完整 token 只存在 GUI 进程内存的 auth_urls 里，
   写进日志的是等长掩码后的 `token=abcd****`。所以 `url` 只打印裸地址，
   带 token 的完整 URL 请在管理器卡片上点「复制入口 URL」。

用法
----
    python dsh-tm.py list                  # 列出所有槽与运行状态
    python dsh-tm.py status [槽名]         # 同 list，可指定单个
    python dsh-tm.py start <槽名>          # 启动一个
    python dsh-tm.py start --all           # 启动全部
    python dsh-tm.py stop <槽名>           # 停止一个
    python dsh-tm.py stop --all            # 停止全部
    python dsh-tm.py url <槽名>            # 打印入口地址（不含 token）
    python dsh-tm.py health [槽名]         # 槽体检（只读）

退出码：0 成功 / 1 有失败 / 2 用法错误 / 3 配置错误
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dsh_i18n import T

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
HOME = os.path.expanduser("~")
LOG_FILE = os.path.join(BASE_DIR, "dsh-manager.log")

# 槽位数量上限。**必须与 dsh_manager.pyw 的 MAX_SLOTS 保持一致**，
# 否则 CLI 会放行界面上装不下的 config（或反过来误报超限）。
# scripts/test-cli.py 的 B4 断言锁着这条一致性。
MAX_SLOTS = 6

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3

# --verbose 打开后，底层模块（dsh_kill / dsh_health）的详细过程才打到 stderr。
_VERBOSE = {"on": False}


# ─────────────────────────── 配置 ───────────────────────────
def _probe_npm_global():
    """npm 全局 node_modules（供 <npm_global> 占位符用）。失败返回空串。"""
    try:
        if os.name != "nt":
            return ""
        appdata = os.environ.get("APPDATA") or ""
        p = os.path.join(appdata, "npm", "node_modules")
        return p if os.path.isdir(p) else ""
    except Exception:
        return ""


def load_config(path=CONFIG_PATH):
    """读 config.json。返回 (cfg, err)；失败时 cfg 为 None。"""
    if not os.path.exists(path):
        return None, T("找不到配置文件：%s") % path
    try:
        # 必须无 BOM：有 BOM 时 json.load 直接抛异常（项目文档里的硬规矩）
        with open(path, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except Exception as e:
        return None, T("config.json 解析失败：%s") % e
    if not isinstance(cfg, dict):
        return None, T("config.json 顶层必须是对象")
    return cfg, ""


class Ctx(object):
    """解析后的运行上下文：占位符已展开、版本表已就绪。"""

    def __init__(self, cfg):
        self.cfg = cfg
        # 先展开 install_base 自身：它可能出现 <home> / <self>，但不会出现 <base>
        # （旧实现直接调 _norm，而 _norm 里要读 self.install_base → AttributeError）
        _raw_base = cfg.get("install_base") or HOME
        if isinstance(_raw_base, str):
            _raw_base = _raw_base.replace("<home>", HOME).replace("<self>", BASE_DIR)
        self.install_base = os.path.normpath(_raw_base) if _raw_base else HOME
        self.npm_global = _probe_npm_global()
        self.versions = cfg.get("versions") or {}
        self.keys = list(self.versions.keys())

    def _norm(self, p):
        if not isinstance(p, str):
            return p
        p = p.replace("<home>", HOME).replace("<self>", BASE_DIR)
        p = p.replace("<base>", self.install_base)
        if "<npm_global>" in p:
            p = p.replace("<npm_global>", self.npm_global or "")
        return os.path.normpath(p) if p else p

    def slot(self, key):
        """取一个槽的展开后配置（node_exe / bin_js / dsh_home / temp_dir / port ...）。"""
        raw = self.versions.get(key)
        if raw is None:
            return None
        c = dict(raw)
        for f in ("node_exe", "bin_js", "dsh_home", "temp_dir"):
            if c.get(f):
                c[f] = self._norm(c[f])
        return c


EXAMPLE_PATH = os.path.join(BASE_DIR, "config", "config.example.json")


def _cfg_or_die(path=CONFIG_PATH):
    """读配置；缺失时**和 GUI 一样**从模板自动生成。

    2026-11-01 实测发现：全新 clone 后直接跑 `dsh-tm.py list` 会报
    「找不到配置文件」而退出，但 GUI 双击却能自动生成 —— 同一个产品，
    两个入口对"首次使用"的态度不一致。这里补齐：缺 config 且有模板时，
    先按模板生成一份零槽配置，再照常往下走（零槽会打印「还没有任何槽」）。
    """
    if not os.path.exists(path) and os.path.exists(EXAMPLE_PATH):
        try:
            import shutil
            shutil.copyfile(EXAMPLE_PATH, path)
            sys.stderr.write(T("[提示] 未找到 config.json，已从 config/config.example.json "
                             "生成默认配置。\n"))
        except Exception as e:
            sys.stderr.write(T("[警告] 自动生成 config.json 失败：%s\n") % e)
    cfg, err = load_config(path)
    if cfg is None:
        sys.stderr.write(T("[错误] %s\n") % err)
        sys.exit(EXIT_CONFIG)
    if not isinstance(cfg.get("versions"), dict):
        cfg["versions"] = {}
    if len(cfg["versions"]) > MAX_SLOTS:
        sys.stderr.write(T("[错误] versions 超过 %d 个槽上限\n") % MAX_SLOTS)
        sys.exit(EXIT_CONFIG)
    return cfg


# ─────────────────────────── 运行态探测 ───────────────────────────
def _port_open(port, timeout=0.25):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    finally:
        try:
            s.close()
        except Exception:
            pass


def _is_running(ctx, key):
    """该槽是否在运行：端口在听即视为运行（与 GUI 判定一致）。"""
    c = ctx.slot(key)
    if not c:
        return False
    try:
        return _port_open(c["port"])
    except Exception:
        return False


def _build_start_cmd(ctx, key):
    """拼出启动命令：本槽 node + bin.js + extra_args（缺省 web --host/--port --no-open）。"""
    c = ctx.slot(key)
    node = c.get("node_exe") or "node"
    bin_js = c.get("bin_js")
    args = c.get("extra_args") or ["web", "--host", "127.0.0.1",
                                   "--port", str(c.get("port")), "--no-open"]
    return [node, bin_js] + [str(a) for a in args]


def _slot_env(ctx, key):
    """每槽的隔离环境：DSH_HOME + TEMP/TMP（与 GUI、与槽内 bat 完全一致）。"""
    c = ctx.slot(key)
    env = os.environ.copy()
    if c.get("dsh_home"):
        env["DSH_HOME"] = c["dsh_home"]
    if c.get("temp_dir"):
        # 纯函数：这里**不建目录**。以前在这里 makedirs，任何调用方
        # （包括 test-cli.py 这种只读回归）都会在真实盘上留下
        # `<home>/dsh-versions/dsh-temp/<槽>`，总检的卫生闸因此报红
        # （2026-11-12 定位到 test-cli）。建目录挪到 _spawn。
        env["TEMP"] = c["temp_dir"]
        env["TMP"] = c["temp_dir"]
    try:
        env["NODE_OPTIONS"] = env.get("NODE_OPTIONS", "")
    except Exception:
        pass
    return env


def _spawn(ctx, key):
    """后台启动一个槽。返回 (ok, 说明)。"""
    c = ctx.slot(key)
    if c is None:
        return False, T("没有这个槽：%s") % key
    if _is_running(ctx, key):
        return True, T("已在运行（端口 %s）") % c.get("port")
    bin_js = c.get("bin_js")
    if not bin_js or not os.path.exists(bin_js):
        return False, T("bin.js 不存在：%s") % bin_js
    node = c.get("node_exe")
    if node and not os.path.exists(node):
        return False, T("node.exe 不存在：%s") % node
    cmd = _build_start_cmd(ctx, key)
    # 真正启动前才建 TEMP（见 _slot_env 的说明：算环境变量不该有副作用）
    _td = c.get("temp_dir")
    if _td:
        try:
            os.makedirs(_td, exist_ok=True)
        except Exception:
            pass
    env = _slot_env(ctx, key)
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | \
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(cmd, cwd=c.get("dsh_home") or BASE_DIR, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, creationflags=flags)
    except Exception as e:
        return False, T("启动失败：%s") % e
    return True, T("已发出启动指令（端口 %s）") % c.get("port")


def _stop(ctx, key):
    """走 dsh_kill 的签名门控停止（与 GUI「停止」同一条路，不误杀别的 node）。"""
    c = ctx.slot(key)
    if c is None:
        return False, T("没有这个槽：%s") % key
    if not _is_running(ctx, key):
        return True, T("本来就没在运行")
    try:
        sys.path.insert(0, BASE_DIR)
        import dsh_kill
    except Exception as e:
        return False, T("无法加载 dsh_kill：%s") % e
    # dsh_kill 的 LOG 默认是 print：GUI 里那叫"日志区"，CLI 里就只是噪音。
    # 静音后由本模块自己输出一行结论；--verbose 时才恢复详细过程。
    if not _VERBOSE.get("on"):
        dsh_kill.LOG = lambda *_a, **_k: None
    try:
        res = dsh_kill.kill_version(key, _make_sig(c), c.get("port"))
    except TypeError:
        # 老签名兼容
        try:
            res = dsh_kill.kill_version(_make_sig(c), c.get("port"))
        except Exception as e:
            return False, T("停止失败：%s") % e
    except Exception as e:
        return False, T("停止失败：%s") % e
    time.sleep(0.6)
    if _is_running(ctx, key):
        return False, T("已发出停止指令，但端口 %s 仍被占用") % c.get("port")
    return True, T("已停止")


def _make_sig(cfg):
    """签名 = 安装根（bin_js 里 /node_modules/ 之前那段），与 GUI 的 _make_sig 同义。"""
    bin_js = (cfg.get("bin_js") or "").replace("\\", "/").lower()
    i = bin_js.find("/node_modules/")
    return bin_js[:i] if i > 0 else bin_js


def _entry_url(ctx, key):
    """从日志里捞该槽最近一条入口 URL（拿不到完整 token 时返回空串）。"""
    c = ctx.slot(key)
    if not c or not os.path.exists(LOG_FILE):
        return ""
    marker = ("dsh web: http://127.0.0.1:%d" % c["port"]).encode()
    try:
        with open(LOG_FILE, "rb") as f:
            blob = f.read()
    except Exception:
        return ""
    pos = 0
    last = ""
    while True:
        i = blob.find(marker, pos)
        if i < 0:
            break
        j = blob.find(b"\n", i)
        line = blob[i: j if j >= 0 else len(blob)]
        m = re.search(rb"https?://\S+", line)
        if m:
            last = m.group(0).decode("utf-8", "replace")
        pos = (j + 1) if j >= 0 else len(blob)
    # 管理器把日志里的 token 等长掩码成 `abcd********`。这种 URL 复制到
    # 浏览器也登不进去，绝不能当成「带 token 的入口」返回。
    if "*" in last:
        return ""
    return last


# ─────────────────────────── 子命令 ───────────────────────────
def cmd_list(ctx, args):
    if not ctx.keys:
        print(T("当前没有管理任何版本槽（config.json 的 versions 为空）。"))
        return EXIT_OK
    width = max([len(k) for k in ctx.keys] + [4])
    print("%-*s  %-10s  %-6s  %-10s  %s" % (width, T("槽"), T("标签"), T("端口"), T("状态"), T("版本")))
    print("-" * 78)
    for k in ctx.keys:
        c = ctx.slot(k)
        running = _is_running(ctx, k)
        ver = _installed_version(c) or T("未知")
        print("%-*s  %-10s  %-6s  %-10s  %s" % (
            width, k, c.get("label") or k, c.get("port"),
            T("运行中") if running else T("已停止"), ver))
    return EXIT_OK


def _installed_version(cfg):
    """从 node_modules/@deepseek-ai/dsh/package.json 读实装版本。"""
    try:
        bin_js = cfg.get("bin_js") or ""
        root = bin_js
        i = root.replace("\\", "/").find("/lib/bin.js")
        if i > 0:
            root = root[:i]
        pkg = os.path.join(root, "package.json")
        if os.path.exists(pkg):
            with open(pkg, "r", encoding="utf-8-sig") as f:
                return json.load(f).get("version") or ""
    except Exception:
        pass
    return ""


def cmd_start(ctx, args):
    keys = _target_keys(ctx, args)
    if not keys:
        return EXIT_USAGE
    rc = EXIT_OK
    for k in keys:
        ok, msg = _spawn(ctx, k)
        print("%-6s %s  %s" % (k, "OK  " if ok else "FAIL", msg))
        if not ok:
            rc = EXIT_FAIL
    if keys:
        # 等端口真正开始监听再回报。dsh 从 spawn 到 listen 实测要 5~8 秒
        # （0.2.x 要加载插件树），固定 sleep 1.5s 会把"已启动"误报成"已停止"。
        _wait_until_listening(ctx, keys, timeout=float(getattr(args, "wait", 20)))
        print(T("\n实际状态："))
        rc |= cmd_list(ctx, args)
    return rc


def _wait_until_listening(ctx, keys, timeout=20.0, step=0.4):
    """轮询到所有目标槽都开始监听，或超时。返回仍未起来的槽名列表。"""
    deadline = time.time() + max(0.0, timeout)
    pending = list(keys)
    while pending and time.time() < deadline:
        pending = [k for k in pending if not _is_running(ctx, k)]
        if not pending:
            break
        time.sleep(step)
    for k in pending:
        print(T("%-6s WARN  等了 %.0f 秒仍未监听（dsh 可能仍在加载插件，稍后自行确认）")
              % (k, timeout))
    return pending


def cmd_stop(ctx, args):
    keys = _target_keys(ctx, args)
    if not keys:
        return EXIT_USAGE
    rc = EXIT_OK
    for k in keys:
        ok, msg = _stop(ctx, k)
        print("%-6s %s  %s" % (k, "OK  " if ok else "FAIL", msg))
        if not ok:
            rc = EXIT_FAIL
    return rc


def cmd_status(ctx, args):
    if args.slot:
        for k in _target_keys(ctx, args):
            print("%s: %s" % (k, T("运行中") if _is_running(ctx, k) else T("已停止")))
        return EXIT_OK
    return cmd_list(ctx, args)


def cmd_url(ctx, args):
    keys = _target_keys(ctx, args)
    if not keys:
        return EXIT_USAGE
    found = 0
    for k in keys:
        u = _entry_url(ctx, k)
        c = ctx.slot(k) or {}
        bare = ("http://127.0.0.1:%s/" % c["port"]) if c.get("port") else ""
        if u or bare:
            found += 1
            print(u or bare)
            if not u:
                print(T("  （仅入口地址；带 token 的完整 URL 请在管理器点「复制入口 URL」）"),
                      file=sys.stderr)
        else:
            print(T("%s  找不到入口地址（槽配置里没有 port）") % k)
    return EXIT_OK if found == len(keys) else EXIT_FAIL


def cmd_health(ctx, args):
    try:
        sys.path.insert(0, BASE_DIR)
        import dsh_health
    except Exception as e:
        sys.stderr.write(T("[错误] 无法加载 dsh_health：%s\n") % e)
        return EXIT_FAIL
    keys = _target_keys(ctx, args)
    bad = 0
    for k in keys:
        c = ctx.slot(k)
        print("=== %s (%s) ===" % (k, c.get("label") or k))
        try:
            items = dsh_health.check_slot(
                k, c, base=ctx.install_base, home=HOME, self_dir=BASE_DIR,
                deep=bool(getattr(args, "deep", False)))
        except Exception as e:
            print(T("  体检失败：%s") % e)
            bad += 1
            continue
        if not items:
            # 浅查干净时 items 是空的。只打标题、下面一片空白会让人以为命令没跑，
            # 所以明确说一句「没发现问题」，并提示深查开关。
            print(T("  未发现问题%s") % ("" if getattr(args, "deep", False)
                                      else T("（浅查；加 --deep 做依赖深查）")))
        for lv, title, detail in items:
            print("  [%s] %s：%s" % (lv, title, detail))
            if lv == "错误":
                bad += 1
        print()
    return EXIT_FAIL if bad else EXIT_OK


def cmd_kill_stray(ctx, args):
    """清理**槽外**的 dsh 残留（界面上取消了这个按钮，改由 CLI 承担）。

    与 stop --all 的区别：
      · stop --all   —— 只停 config 里登记的这几个槽；
      · kill-stray   —— 连"没登记进 config 的 dsh 副本 / 子进程"一起清，
                        用于"端口被一个我不认识的 dsh 占着"的排障场景。

    安全边界与管理器的停止逻辑一致：只认命令行里出现
    @deepseek-ai/dsh 的 node 进程（SIG_ANY），绝不碰其它应用的 node。
    """
    try:
        sys.path.insert(0, BASE_DIR)
        import dsh_kill
    except Exception as e:
        sys.stderr.write(T("[错误] 无法加载 dsh_kill：%s\n") % e)
        return EXIT_FAIL
    if not _VERBOSE.get("on"):
        dsh_kill.LOG = lambda *_a, **_k: None
    versions = {}
    for k in ctx.keys:
        c = ctx.slot(k)
        versions[k] = {"sig": _make_sig(c), "port": c.get("port")}
    sig_any = "@deepseek-ai/dsh"
    if not getattr(args, "yes", False):
        print(T("将清理：本管理器管的 %d 个槽 + 所有命令行含 %s 的 node 进程。")
              % (len(versions), sig_any))
        print(T("（只杀 dsh，不碰其它软件的 node 进程）"))
        print(T("确认执行请加 --yes"))
        return EXIT_OK
    try:
        dsh_kill.kill_all(versions, sig_any=sig_any)
    except Exception as e:
        sys.stderr.write(T("[错误] 清理失败：%s\n") % e)
        return EXIT_FAIL
    time.sleep(0.8)
    print(T("已清理。当前状态："))
    return cmd_list(ctx, args)


def _target_keys(ctx, args):
    """决定要操作哪些槽：--all / 指定槽名 / 缺省报错。"""
    if getattr(args, "all", False):
        if not ctx.keys:
            sys.stderr.write(T("[错误] 当前没有任何槽\n"))
            return []
        return list(ctx.keys)
    if not args.slot:
        sys.stderr.write(T("[错误] 请指定槽名（或用 --all）\n"))
        sys.stderr.write(T("可用槽：%s\n") % ("、".join(ctx.keys) or T("（无）")))
        return []
    keys = [s for s in args.slot if s in ctx.versions]
    for s in args.slot:
        if s not in ctx.versions:
            sys.stderr.write(T("[错误] 没有这个槽：%s\n") % s)
    return keys


def app_version():
    """程序版本：与 GUI 同源 —— 读仓库根的 `VERSION` 文件（2026-11-14）。

    以前版本号只存在于 config.json（每台机器一份），换台机器就显示兜底值；
    现在版本跟代码一起提交，`--version` 和界面标题栏读到的是同一个数。
    """
    try:
        with open(os.path.join(BASE_DIR, "VERSION"), "r", encoding="utf-8-sig") as f:
            v = (f.read() or "").strip()
        if v:
            return v
    except Exception:
        pass
    return "0.0.0"


def build_parser():
    p = argparse.ArgumentParser(
        prog="dsh-tm",
        description=T("dsh-tm 命令行接口（与图形界面共用同一份 config.json）"),
        epilog=T("示例：python dsh-tm.py start --all / python dsh-tm.py list"))
    p.add_argument("--config", default=CONFIG_PATH, help=T("配置文件路径（缺省同目录 config.json）"))
    p.add_argument("--verbose", action="store_true", help=T("打印底层详细过程（排障用）"))
    p.add_argument("--version", action="version", version="dsh-tm " + app_version(),
                   help=T("打印版本号后退出"))
    sub = p.add_subparsers(dest="cmd")

    for name, help_text in (("list", T("列出所有槽与运行状态")),
                            ("status", T("同 list，可指定槽")),
                            ("url", T("打印入口地址（不含 token）")),
                            ("health", T("槽体检（只读）"))):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("slot", nargs="*", help=T("槽名（如 v1）；可给多个"))
        sp.add_argument("--all", action="store_true", help=T("对所有槽执行"))
        if name == "health":
            sp.add_argument("--deep", action="store_true", help=T("深度体检"))

    for name, help_text in (("start", T("启动槽（支持 --all）")),
                            ("stop", T("停止槽（支持 --all）"))):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("slot", nargs="*", help=T("槽名（如 v1）；可给多个"))
        sp.add_argument("--all", action="store_true", help=T("对全部槽执行"))
        if name == "start":
            sp.add_argument("--wait", type=float, default=20.0, metavar=T("秒"),
                            help=T("等待端口监听的超时秒数（缺省 20）"))

    sp = sub.add_parser(
        "kill-stray",
        help=T("清理槽外 dsh 残留（排障用；旧版界面按钮已移除）"))
    sp.add_argument("--yes", action="store_true", help=T("确认执行（不加只打印计划）"))

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return EXIT_USAGE
    _VERBOSE["on"] = bool(getattr(args, "verbose", False))
    ctx = Ctx(_cfg_or_die(args.config))
    handler = {
        "list": cmd_list, "status": cmd_status, "start": cmd_start,
        "stop": cmd_stop, "url": cmd_url, "health": cmd_health,
        "kill-stray": cmd_kill_stray,
    }[args.cmd]
    return handler(ctx, args)


if __name__ == "__main__":
    sys.exit(main())
