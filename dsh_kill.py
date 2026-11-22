"""Guarded slot stop command, port diagnostics, and subprocess output decoding."""

import argparse
import json
import subprocess, time, csv, io, sys, os
import dsh_slot_process as SLOT
from dsh_i18n import T

CREATE_NO_WINDOW = 0x08000000

# 日志回调：默认 print，管理器接入时替换为 GUI 日志
LOG = print


# ─── 子进程调用（编码安全） ────────────────────────────────────────────────
# 为什么必须自己解码，**不能**用 subprocess 的 text=True：
#   本机设了 PYTHONUTF8=1，Python 的 locale 首选编码被固定成 UTF-8；
#   而中文 Windows 的 netstat / taskkill 输出是 **CP936(GBK)** 字节。
#   text=True 按 UTF-8 **严格**解码，读线程抛 UnicodeDecodeError 后被 subprocess
#   内部吞掉 —— 不往上抛异常，只把 r.stdout 置成 None，于是对外报出的是
#   "'NoneType' object has no attribute 'splitlines'" 这种与真因无关的错（本机日志实证）。
#   实测：netstat 中招（约 17 KB，含中文表头与 GBK 字节），tasklist / powershell
#   均为 ASCII 或 UTF-8 → 不中招。这正是日志里只有 netstat 报错的原因。
def decode_out(b):
    """把子进程输出字节解成文本：先严格 UTF-8，失败退 CP936，最后兜底 replace。"""
    if not b:
        return ""
    for enc in ("utf-8", "cp936"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def run_capture(cmd, timeout=None, stdin_devnull=False, shell=False):
    """统一的外部命令调用：返回 (rc, stdout, stderr)，且**永不**因解码失败丢输出。
    注意：这里故意不传 text=True —— 拿字节回来自己解，才能既拿到中文、又不会
    在解码失败时被静默置成 None。"""
    r = subprocess.run(cmd, capture_output=True,
                       stdin=subprocess.DEVNULL if stdin_devnull else None,
                       creationflags=CREATE_NO_WINDOW if SLOT.WINDOWS else 0,
                       timeout=timeout, shell=shell)
    return r.returncode, decode_out(r.stdout), decode_out(r.stderr)


def norm_path(s):
    """把路径 / 命令行归一化后再比较：反斜杠统一成正斜杠、转小写、合并重复分隔符。

    为什么必须做：Windows 上 `C:\\a/b\\c` 与 `C:/a/b/c` 是**同一路径但字符串不等**。
    实测本机同一份 0.1.6-alpha.2 的 web 进程，管理器用 config.json 的路径拉起时
    命令行是 `C:\\Users\\.../dsh-versions/...`（混合斜杠），而用 bash 拉起时是
    `C:/Users/...`（全正斜杠）。不做归一化，签名就会随"谁拉起来的"而失配 ——
    表现为【杀不掉自己的进程】，这正是最该被避免的失效方向。"""
    s = (s or "").lower().replace("\\", "/")
    while "//" in s:
        s = s.replace("//", "/")
    return s


# ─── 进程 / 端口枚举 ──────────────────────────────────────────────────────

# ── 只读探测缓存（2026-09-27）─────────────────────────────────────────────
# 背景：netstat（~1s）与 Get-CimInstance（~1-2s）每次调用都要 1-2 秒。
# 管理器的状态轮询/启动快照每个槽各调一次 → 窗口出现前卡 6 秒+，运行中每轮
# 刷状态还会周期性冻住界面。这里给【只读探测】加 TTL 缓存；**杀进程路径
# （find_version_pids / kill_version）仍然直调无缓存版本，保持实时**。
_PROBE_CACHE = {}

def _cached_probe(name, fn, ttl):
    ts, val = _PROBE_CACHE.get(name, (0.0, None))
    now = time.time()
    if val is None or (now - ts) > ttl:
        val = fn()
        _PROBE_CACHE[name] = (now, val)
    return val

def _node_map_cached(ttl=8.0):
    """_node_map 的缓存版（管理器只读探测用；返回值可能与实时差一个 TTL 窗口）。
    TTL 必须 > CIM 查询本身的耗时（实测 ~4s），否则缓存永远过期、形同虚设。"""
    return _cached_probe("node_map", _node_map, ttl)

def _pid_ports_cached(ttl=2.0):
    """_pid_ports 的缓存版（管理器只读探测用；杀进程请勿使用）。"""
    return _cached_probe("pid_ports", _pid_ports, ttl)

def _node_map():
    """返回 {pid: commandline}，仅含 node.exe 进程。

    两条路径，按可用性自动降级
    --------------------------
    1) Get-CimInstance Win32_Process —— 能拿到**完整命令行**（含 @deepseek-ai/dsh
       路径），是签名判定的理想数据源。
    2) Get-Process（.NET，不走 WMI）—— 只拿得到**可执行文件路径**。

    为什么必须有第2 条（实测，非推测）
    --------------------------------
    非管理员账户下 Get-CimInstance 与 tasklist **全部报「拒绝访问」**，
    而 Get-Process 可用。此时若没有降级：
      · 一个 dsh 进程都识别不出来 → 「停止」杀不掉
      · 端口占用者只能显示「未知进程」→ 用户无从判断该关哪个程序
      · 更糟：自家 dsh 会被误判成「非 dsh 程序占用」

    降级后能保住什么：_looks_like_dsh 判定命令行含 @deepseek-ai/dsh；
    拿不到命令行时**不能**靠路径猜（node.exe 路径里没有 dsh 信息），
    所以降级态下只能配合端口来判定（find_version_pids 本来就以端口为准）。
    这一点由 _node_map_degraded() 显式告知调用方，不让它误以为拿到了全量数据。
    """
    if not SLOT.WINDOWS:
        processes = (SLOT.process_info(pid) for pid in _pid_ports())
        return {p.pid: " ".join(p.argv) for p in processes if p and
                os.path.basename(p.executable).lower() in ("node", "node.exe")}
    m = _node_map_cim()
    if m:
        _NODE_MAP_DEGRADED["v"] = False
        return m
    m2 = _node_map_dotnet()
    # 两种情况要分开说：
    #   · m2 非空 → 降级但可用（拿得到"是不是 node"，拿不到命令行）
    #   · m2 为空 → 连 node 都列不出来（多半当前没有 node 在跑，也可能是权限）
    if m2:
        # 标记为降级，但保留 node_map_is_degraded() 的主动探测结果：
        # 那个探测问的是「能不能读命令行」，比「有没有列到 node」更准确。
        _NODE_MAP_DEGRADED["v"] = True
        LOG(T("[kill] Get-CimInstance 不可用（当前账户权限不足），"
            "已降级为 Get-Process：可识别 node 进程，但读不到命令行"))
    else:
        LOG(T("[kill] 未枚举到 node 进程（此刻可能本来就没有 node 在跑）"))
    return m2


# 记录最近一次 _node_map 是否处于降级态，供 UI 提示与排障使用
_NODE_MAP_DEGRADED = {"v": None}      # None = 尚未探测过


def node_map_is_degraded():
    """True = 当前**读不到进程命令行**（多半是非管理员权限）。

    为什么要主动探测而不是「看 _node_map 有没有降级」
    --------------------------------------------
    降级标志只在 _node_map() 成功枚举到 node 进程时才会被置位。如果此刻恰好
    没有 node 在跑（很常见，比如 dsh 还没启动），标志仍是 None/False，
    而权限问题其实存在 —— 于是「端口被占用、却说不出是谁」的提示不会出现，
    用户被误导去查错方向（实测踩到）。

    这里改成**直接问系统**：能不能读到本机任意进程的完整命令行。
    读不到就说明权限不足，与有没有 node 在跑无关。
    """
    if _NODE_MAP_DEGRADED["v"] is None:
        _NODE_MAP_DEGRADED["v"] = not _can_read_cmdline()
        if _NODE_MAP_DEGRADED["v"]:
            LOG(T("[kill] 探测：当前账户读不到进程命令行（权限不足），"
                "端口占用者将无法显示程序名/命令行"))
    return bool(_NODE_MAP_DEGRADED["v"])


def _can_read_cmdline():
    """能否读到进程命令行。用 CIM 试一个必定存在的进程（PID 4 = System）。

    拿不到就返回 False。非管理员下 Get-CimInstance 会报「拒绝访问」，
    此时返回 False 是正确的判定。
    """
    if not SLOT.WINDOWS:
        return SLOT.process_info(os.getpid()) is not None
    try:
        ps = ('Get-CimInstance Win32_Process -Filter "ProcessId=4"'
              ' -ErrorAction Stop | ForEach-Object { $_.Name }')
        _rc, out, _err = run_capture(["powershell", "-NoProfile", "-Command", ps],
                                     timeout=25)
        return bool((out or "").strip())
    except Exception:
        return False


def _node_map_cim():
    """用 Get-CimInstance 取 {pid: 完整命令行}。权限不足时返回空 dict。"""
    m = {}
    try:
        # commandline 里的 '|' 替换为空格，避免破坏分隔
        ps = ('Get-CimInstance Win32_Process -Filter "Name=\'node.exe\'"'
              ' | ForEach-Object { ($_.ProcessId.ToString()) + \'|\''
              ' + ($_.CommandLine -replace \'\\|\',\' \') }')
        _rc, out, _err = run_capture(["powershell", "-NoProfile", "-Command", ps])
        for line in out.splitlines():
            line = line.strip()
            if "|" in line:
                pid_s, cmd = line.split("|", 1)
                try:
                    m[int(pid_s)] = cmd
                except ValueError:
                    pass
    except Exception as e:
        LOG("[kill] enum node (CIM) failed: %s" % e)
    return m


def _node_map_dotnet():
    """降级路径：Get-Process 取 {pid: node.exe 路径}。

    注意拿到的**不是命令行**（那是识别 dsh 的关键），只有 exe 路径。
    仅用于「这个 PID 是不是 node」这一层判断，dsh 身份仍靠端口 + 签名。
    """
    m = {}
    try:
        ps = ('Get-Process -Name node -ErrorAction SilentlyContinue'
              ' | ForEach-Object { ($_.Id.ToString()) + \'|\' + $_.Path }')
        _rc, out, _err = run_capture(["powershell", "-NoProfile", "-Command", ps])
        for line in out.splitlines():
            line = line.strip()
            if "|" in line:
                pid_s, path = line.split("|", 1)
                try:
                    m[int(pid_s)] = path or ""
                except ValueError:
                    pass
    except Exception as e:
        LOG("[kill] enum node (Get-Process) failed: %s" % e)
    return m



def _pid_ports():
    return SLOT.listener_ports()


def _looks_like_dsh(cmd):
    """宽松判定：node 进程且命令行含 @deepseek-ai/dsh 即视为 dsh。

    覆盖绝对路径（管理器拉起）、相对路径（桌面 dsh.bat 拉起）、npm 间接启动等所有
    形态——因为 dsh 的命令行里必然带 @deepseek-ai/dsh 这个包路径。双条件（node +
    @deepseek-ai/dsh）保证其它 node 应用（各类 MCP 服务、编辑器插件宿主等，
    命令行不含该串）不会被误判。"""
    c = norm_path(cmd)
    return "node" in c and "@deepseek-ai/dsh" in c


def _is_node_exe(pid):
    process = SLOT.process_info(pid)
    return bool(process and os.path.basename(process.executable).lower() in ("node", "node.exe"))


def find_version_pids(entrypoint, port):
    """Return exact entrypoint matches at the supplied TCP port."""
    return SLOT.slot_listeners(entrypoint, port)[0] if isinstance(entrypoint, (str, os.PathLike)) else set()


def kill_version(key, entrypoint, port, tracked_pid=None):
    """Stop an exact slot. A verified supervisor handle can include a starting child."""
    if not isinstance(entrypoint, (str, os.PathLike)):
        return 0
    killed = SLOT.stop_slot(entrypoint, port, tracked=tracked_pid)
    LOG(T("[%s] 已结束 %d 个槽进程") % (key, killed))
    return killed


def kill_all(versions, sig_any=None):
    return sum(kill_version(key, spec.get("entrypoint"), spec["port"])
               for key, spec in versions.items())


def _proc_name(pid):
    """取进程名。返回 '' 表示取不到。

    两条路径（实测：非管理员下 tasklist 报「拒绝访问」）：
      1) tasklist /FI "PID eq N" /FO CSV /NH —— 管理员与普通账户都可用时走它
      2) Get-Process -Id N（.NET，不走 WMI）—— tasklist 失败时降级到它
    两条都拿不到才返回 ''，由调用方显示「权限不足」而不是「未知」。
    """
    try:
        rc, out, _ = run_capture(["tasklist", "/FI", "PID eq %d" % pid,
                                  "/FO", "CSV", "/NH"])
        if rc == 0:
            rows = list(csv.reader(io.StringIO(out)))
            for row in rows:
                if len(row) > 1 and row[1].strip() == str(pid):
                    return row[0].strip()
    except Exception:
        pass
    # 降级：tasklist 不可用时用 .NET 的 Get-Process
    try:
        ps = ('Get-Process -Id %d -ErrorAction SilentlyContinue'
              ' | ForEach-Object { $_.ProcessName }' % int(pid))
        rc, out, _ = run_capture(["powershell", "-NoProfile", "-Command", ps])
        if rc == 0:
            nm = (out or "").strip().splitlines()
            if nm and nm[0].strip():
                return nm[0].strip()
    except Exception:
        pass
    return ""


def describe_port(port, entrypoint=None):
    """Return a redacted, read-only description of listeners."""
    owned, listeners = SLOT.slot_listeners(entrypoint, port)
    owners = []
    for pid in sorted(listeners):
        process = SLOT.process_info(pid)
        name = os.path.basename(process.executable) if process else ""
        command = " ".join(process.argv) if process else ""
        command = SLOT.TOKEN.sub(rb"\1[redacted]", command.encode("utf-8", "replace")).decode("utf-8")
        if pid in owned:
            kind = "dsh"
        elif not process:
            kind = "unknown"
        elif name.lower() in ("node", "node.exe"):
            kind = "non-dsh-node"
        else:
            kind = "non-dsh-other"
        owners.append({"pid": pid, "name": name, "cmd": command, "kind": kind})
    return owners


_KIND_TAG = {
    "dsh": "【本槽 dsh 进程】",
    "unknown": "【进程身份未能核实】",
    "non-dsh-node": "【其他 Node.js 进程】",
    "non-dsh-other": "【其他进程】",
}


def format_port_owners(port, entrypoint=None):
    owners = describe_port(port, entrypoint)
    if not owners:
        if SLOT.port_reachable(port):
            return [], T("端口 %d 被占用，未能核实进程身份。") % port
        return [], T("端口 %d 当前空闲。") % port
    lines = [T("端口 %d 被以下进程占用：") % port]
    for owner in owners:
        lines.append("  • PID=%d  %s  %s" % (owner["pid"], T(_KIND_TAG[owner["kind"]]),
                                             owner["name"] or T("（进程名未能读取）")))
        if owner["cmd"]:
            lines.append(T("      命令行: %s") % owner["cmd"][:400])
        else:
            lines.append(T("      命令行: 当前账户权限不足，无法读取"))
    return owners, "\n".join(lines)


def _configured_entrypoint(port):
    # Existing launchers supply only a port. Resolve it through the common
    # configuration when exactly one slot declares the port.
    import dsh_platform as platform
    path = platform.default_config_path()
    try:
        with open(path, encoding="utf-8-sig") as config_file:
            cfg = json.load(config_file)
        matches = [c["bin_js"] for c in cfg["versions"].values() if SLOT.normalize_port(c.get("port")) == port]
        if len(matches) == 1:
            return platform.resolve_path(matches[0], base=platform.config_base(cfg, path),
                                         npm_global=platform.probe_npm_global())
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        pass
    return None


def precheck_start(port, label="", entrypoint=None):
    entrypoint = entrypoint or _configured_entrypoint(port)
    state = SLOT.slot_state(entrypoint, port)
    _, detail = format_port_owners(port, entrypoint)
    print(detail)
    try:
        from dsh_platform import log_path
        with open(log_path(), "a", encoding="utf-8") as file:
            file.write("[%s] [bat-precheck][%s] %s\n" %
                       (time.strftime("%Y-%m-%d %H:%M:%S"), label, detail.replace("\n", " | ")))
    except OSError:
        pass
    return {"idle": 0, "conflict": 3, "owned": 4}[state]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", "-p", type=int, required=True)
    parser.add_argument("--entrypoint")
    parser.add_argument("--yes", "-y", action="store_true")
    parser.add_argument("--precheck-start", metavar="SLOT")
    args = parser.parse_args(argv)
    if not 0 < args.port <= 65535:
        parser.error(T("无效的 TCP 端口"))
    entrypoint = args.entrypoint or _configured_entrypoint(args.port)
    if args.precheck_start is not None:
        return precheck_start(args.port, args.precheck_start, entrypoint)
    owners, detail = format_port_owners(args.port, entrypoint)
    print(detail)
    if not owners and not SLOT.port_reachable(args.port):
        return 0
    if not owners or any(owner["kind"] != "dsh" for owner in owners):
        print(T("端口占用者未核实为本槽进程，已保留该进程。"))
        return 3
    if not args.yes:
        try:
            if input(T("结束本槽 dsh 进程？ [y/N] ")).strip().lower() not in ("y", "yes"):
                return 0
        except EOFError:
            return 0
    SLOT.stop_slot(entrypoint, args.port)
    if SLOT.slot_state(entrypoint, args.port) != "idle":
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
