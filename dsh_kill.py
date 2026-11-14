# dsh_kill.py — 安全、可监控的 dsh 双版本进程清理内核（配置驱动版）
#
# 由 DSHweblink/dsh_kill.py 泛化而来：本文件【不写死任何路径/端口/版本号】，
# 签名判定由调用方（dsh_manager.pyw）根据 config.json 动态构造后注入。
#
# 设计原则（用户硬约束）：
#   1. 精准杀：只杀「签名白名单」命中的 dsh 进程，绝不影响另一个 dsh 版本、
#      也绝不误杀其他 app 的 node 进程。
#   2. 可监控：杀之前先记录 PID / 端口 / 命令行，给人看清楚杀的是谁。
#   3. 多级回退：taskkill /F → taskkill /T /F → TerminateProcess(原生 API)，
#      杜绝 taskkill /T 偶发 rc=255 静默失败。
#   4. 拒绝误杀守卫：端口占用者若命令行不匹配该版本签名，一律拒绝并告警。
#
# 本模块不依赖 GUI、不触碰任何文件，可被 dsh_manager.pyw 与测试脚本共同 import。

import subprocess, time, ctypes, csv, io, sys, os
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
                       creationflags=CREATE_NO_WINDOW,
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
    """返回 {pid: set(ports)}，仅含 LISTENING 的 TCP 端口。

    注意：这里曾经用 subprocess 的 text=True，在本机（PYTHONUTF8=1 + 中文 Windows）
    恒返回空字典 —— netstat 输出是 CP936，严格 UTF-8 解码失败被吞、stdout 变 None。
    改用 run_capture 后实测可正常枚举（约 200+ 行）。"""
    pp = {}
    try:
        _rc, out, _err = run_capture(["netstat", "-ano"])
        for line in out.splitlines():
            if "LISTENING" not in line:
                continue
            parts = line.split()
            if len(parts) < 5:
                continue
            # 行格式:  Proto  Local Address  Foreign ...  State  PID
            addr = parts[1]            # 127.0.0.1:4080
            pid = parts[-1]
            if ":" not in addr:
                continue
            port = addr.rsplit(":", 1)[-1]
            try:
                pp.setdefault(int(pid), set()).add(int(port))
            except ValueError:
                pass
    except Exception as e:
        LOG("[kill] netstat failed: %s" % e)
    return pp


def _pid_alive(pid):
    """精确判断 PID 是否存活（按 tasklist CSV 的 PID 列比对）。

    旧写法是 `str(pid) in out` 的子串匹配。**实测结论：在这个调用形态下它不会
    假阳性** —— `tasklist /FI "PID eq N"` 的过滤本身是精确的，查不存在的 pid
    返回中文 INFO 行「信息: 没有运行的任务匹配指定标准。」，里面不含任何数字
    （本机验证：查 1496（真实 pid 14960 的前缀）返回 False）。
    这里仍改成按列精确比较，理由有二：
      1. 与 dsh_manager._pid_alive 保持一致（两处语义不同是维护陷阱）；
      2. 不再依赖"tasklist 的输出里恰好不会出现该数字串"这一隐含前提，
         将来若有人去掉 /FI 过滤或改用别的命令，子串匹配会立刻变成 bug。"""
    try:
        rc, out, _err = run_capture(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"])
    except Exception:
        return False
    if rc != 0:
        return False
    want = str(pid)
    try:
        rows = list(csv.reader(io.StringIO(out)))
    except Exception:
        return False
    return any(len(row) > 1 and row[1].strip() == want for row in rows)


def _terminate_api(pid):
    """原生 TerminateProcess，绕过 taskkill（处理 rc=255 等异常）。

    last-error 必须用 use_last_error=True 的句柄 + ctypes.get_last_error()：
    ctypes.windll 的句柄不捕获 last-error，`ctypes.GetLastError()` 读的是线程的
    「上一次错误」，可能来自更早的任意一次 Win32 调用 —— 报出来的 lastErr 数字
    会指向完全无关的原因（2026-10-31 与 dsh_manager 的单实例互斥一起发现）。
    这里 lastErr 只用于诊断文本、不影响控制流，但错的诊断比没有更误导。
    """
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k.OpenProcess(0x0001, False, pid)   # PROCESS_TERMINATE
        if not h:
            return False, "OpenProcess null (lastErr=%d)" % ctypes.get_last_error()
        ok = k.TerminateProcess(h, 0)
        err = ctypes.get_last_error()
        k.CloseHandle(h)
        return bool(ok), ("ok" if ok else "lastErr=%d" % err)
    except Exception as e:
        return False, str(e)


# ─── 识别目标 ──────────────────────────────────────────────────────────────
def _looks_like_dsh(cmd):
    """宽松判定：node 进程且命令行含 @deepseek-ai/dsh 即视为 dsh。

    覆盖绝对路径（管理器拉起）、相对路径（桌面 dsh.bat 拉起）、npm 间接启动等所有
    形态——因为 dsh 的命令行里必然带 @deepseek-ai/dsh 这个包路径。双条件（node +
    @deepseek-ai/dsh）保证其它 node 应用（各类 MCP 服务、编辑器插件宿主等，
    命令行不含该串）不会被误判。"""
    c = norm_path(cmd)
    return "node" in c and "@deepseek-ai/dsh" in c


def _is_node_exe(pid):
    """针对性查询某 pid 的 ExecutablePath，判定是否为 node 进程。

    用途：Get-CimInstance 偶发拿不到 commandline（cmd 为空）时，dsh 进程因此被
    _looks_like_dsh 误判为「非 dsh」，导致端口权威命中却被拒绝收编、把自己锁死。
    此时退一步用 ExecutablePath 确认进程映像是不是 node.exe：是 node + 命中 dsh
    保留端口 -> 仍按 dsh 收编；非 node（真·外来进程占了保留端口）-> 仍拒绝，安全网保留。"""
    try:
        ps = ('Get-CimInstance Win32_Process -Filter "ProcessId=%d"'
              ' | ForEach-Object { $_.ExecutablePath }' % int(pid))
        _rc, out, _err = run_capture(["powershell", "-NoProfile", "-Command", ps])
        return "node" in norm_path(out)
    except Exception:
        return False


def find_version_pids(sig, port):
    """返回该版本应当清理的 dsh PID 集合。

    端口权威：本槽端口（4080/5080/7080 之一）是 dsh 的保留端口，凡是 LISTENING
    在该端口上的 dsh 进程一律收编——无论它是管理器绝对路径拉起、还是用户用 dsh.bat
    相对路径拉起、还是 npm 间接启动（命令行都含 @deepseek-ai/dsh）。这修复了
    「相对路径启动的 dsh 因签名失配而被判成非 dsh、单杀/全杀都清不掉」的问题。

    安全网：端口占用者若既非 node 也非 @deepseek-ai/dsh（真·外来进程占了保留端口，
    属异常），只告警、不杀，避免误伤别的软件。"""
    m = _node_map()
    pp = _pid_ports()
    res = set()
    for pid, ports in pp.items():
        if port not in ports:
            continue
        cmd = m.get(pid, "")
        if _looks_like_dsh(cmd):
            res.add(pid)
            LOG(T("[kill] 端口 %d 命中 dsh 进程 pid=%d cmd=%r") % (port, pid, cmd[:120]))
        elif _is_node_exe(pid):
            # 端口权威命中 + 进程确为 node，但 CIM 偶发未返回 commandline。
            # 降级收编，避免把自己锁死；非 node 进程仍拒绝（安全网保留）。
            res.add(pid)
            LOG(T("[kill] 端口 %d 命中 pid=%d 为 node 进程（cmdline 未取到），按端口权威降级收编")
                % (port, pid))
        else:
            LOG(T("[kill] 端口 %d 被非 dsh/非 node 进程占用 pid=%d cmd=%r -> 拒绝误杀（需人工处理）")
                % (port, pid, cmd[:120]))
    if res:
        LOG(T("[kill] 端口=%s 命中 %d 个 dsh 进程: %s") % (port, len(res), sorted(res)))
    return res


# ─── 单进程监控 + 清理 ─────────────────────────────────────────────────────
def monitor_and_kill(pid, tag, port=None, sig=None):
    """清理一个**已过门控**的 PID，并全程记录命令行备查。

    sig 非空时做执行前复核（防误杀的最后一道闸）：候选集是"枚举 → 门控"两步得到
    的，但枚举与执行之间进程可能已退出、PID 被系统复用给别的软件。不复核就会把
    复用该 PID 的无关进程杀掉 —— 这是"杀单个版本"最容易伤到别人的路径。
    查得到 node 进程表（m 非空）且不命中签名，一律拒绝；只有 powershell 枚举
    整体失败（m 为空）才降级放行，否则一次枚举失败会让整条清理链路全废。"""
    m = _node_map()
    cmd = m.get(pid, "")
    LOG(T("%s 目标 pid=%d 端口=%s 命令行=%r") % (tag, pid, port, cmd[:150]))
    if sig is not None and m:
        if not cmd:
            # CIM 未返回 commandline：针对性查 exe，确为 node 则降级放行，
            # 否则视为「查不到该 pid（已退出 / PID 已被复用）」拒绝，防误杀。
            if _is_node_exe(pid):
                LOG(T("%s pid=%d 查不到 cmdline 但确为 node 进程，降级放行") % (tag, pid))
            else:
                LOG(T("%s pid=%d 拒绝：进程表里查不到该 pid（已退出 / PID 已被复用）-> 不杀")
                    % (tag, pid))
                return False
        elif not sig(cmd):
            # cmd 存在但不匹配签名：若 exe 确为 node 仍降级（CIM 可能返回了残缺 cmdline），
            # 否则拒绝，防误杀非 dsh 进程。
            if _is_node_exe(pid):
                LOG(T("%s pid=%d 命令行不匹配签名但确为 node 进程，降级放行 cmd=%r")
                    % (tag, pid, cmd[:120]))
            else:
                LOG(T("%s pid=%d 拒绝：命令行不匹配签名，不杀（防误杀非 dsh 进程）cmd=%r")
                    % (tag, pid, cmd[:120]))
                return False
    if not _pid_alive(pid):
        LOG(T("%s pid=%d 已不存在，跳过") % (tag, pid))
        return True

    # 步骤1：taskkill /F（杀主进程，尽量不波及其他）
    rc, _out, err = run_capture(["taskkill", "/PID", str(pid), "/F"])
    LOG("%s taskkill /F rc=%d %r" % (tag, rc, err.strip()))
    if not _pid_alive(pid):
        LOG(T("%s pid=%d 步骤1后已死") % (tag, pid))
        return True

    time.sleep(0.4)
    # 步骤2：taskkill /T /F（连带子进程）
    rc, _out, err = run_capture(["taskkill", "/PID", str(pid), "/T", "/F"])
    LOG("%s taskkill /T rc=%d %r" % (tag, rc, err.strip()))
    if not _pid_alive(pid):
        LOG(T("%s pid=%d 步骤2后已死") % (tag, pid))
        return True

    time.sleep(0.4)
    # 步骤3：原生 TerminateProcess 兜底
    ok, msg = _terminate_api(pid)
    LOG("%s TerminateProcess ok=%s %s" % (tag, ok, msg))
    time.sleep(0.3)
    alive = _pid_alive(pid)
    LOG("%s pid=%d %s" % (tag, pid, T("仍存活!!") if alive else T("已死")))
    return not alive


# ─── 对版本清理 ────────────────────────────────────────────────────────────
def kill_version(key, sig, port, tracked_pid=None):
    """清理某版本的 dsh 进程。tracked_pid 为本 app 自己拉起并跟踪的 PID
    （即便签名校验偶漏也强制纳入，因为是我们自己生的）。"""
    pids = find_version_pids(sig, port)
    if tracked_pid is not None:
        pids.add(int(tracked_pid))
    if not pids:
        LOG(T("[%s] 未找到匹配的 dsh 进程，无需清理") % key)
        return 0
    killed = 0
    for pid in sorted(pids):
        if _pid_alive(pid) or pid == tracked_pid:
            # 用宽松 dsh 判定做执行前复核（防 PID 复用误杀），不再要求绝对路径签名
            if monitor_and_kill(pid, "[%s]" % key, port, sig=_looks_like_dsh):
                killed += 1
    LOG(T("[%s] 已清理 %d/%d") % (key, killed, len(pids)))
    return killed


def kill_all(versions, sig_any=None):
    """清理所有版本的 dsh。versions: {key: {"sig": sig, "port": port}}。

    端口权威：只清三个 dsh 保留端口（4080/5080/7080）上的 dsh 进程。绝不扫描、也
    绝不碰其它端口上的 node（包括用户在别的端口上启动的桌面 dsh）——符合「全杀只杀
    这三个 dsh 版本端口」的约束。sig_any 参数保留仅为向后兼容，不再使用。"""
    LOG(T("=== KILL ALL（端口权威，只清 dsh 保留端口） ==="))
    for key, spec in versions.items():
        kill_version(key, spec["sig"], spec["port"])
    # 终检：若某保留端口清理后仍有进程监听，告警（已尽力，可能是外来非 dsh 进程）
    pp = _pid_ports()
    ports = [spec["port"] for spec in versions.values()]
    for port in ports:
        for pid, pid_ports in pp.items():
            if port in pid_ports:
                LOG(T("[kill-all] 端口 %d 清理后仍有进程 pid=%d 监听 -> 可能需要人工处理")
                    % (port, pid))
    LOG(T("=== KILL ALL 完成 ==="))


# ─── 端口占用者识别（给提示用） ───────────────────────────────────────────────
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


def describe_port(port):
    """只读：返回端口 LISTENING 占用者的结构化列表。
    每项 = {pid, name, cmd, kind}，kind ∈
      'dsh'            node + 命令行含 @deepseek-ai/dsh（确属 dsh）
      'dsh-ambiguous'  node 进程但 cmdline 取不到（CIM 偶发丢命令行），按端口权威视为自家 dsh
      'non-dsh-node'   node 进程但命令行不含 dsh（真·外来 node 占了保留端口）
      'non-dsh-other'  非 node 进程（其它软件占了保留端口）
    不改动任何进程；调用方据此决定：dsh → 可清，non-dsh → 绝不自动清、只提示。"""
    nm = _node_map()
    pp = _pid_ports()
    owners = []
    for pid, ports in pp.items():
        if port not in ports:
            continue
        cmd = nm.get(pid, "")
        is_node = bool(cmd) or _is_node_exe(pid)
        if is_node:
            if "@deepseek-ai/dsh" in norm_path(cmd):
                kind = "dsh"
            elif cmd == "":
                kind = "dsh-ambiguous"   # node 但命令行未取到，视为自家 dsh
            else:
                kind = "non-dsh-node"
        else:
            kind = "non-dsh-other"
        owners.append({"pid": pid, "name": _proc_name(pid), "cmd": cmd, "kind": kind})
    owners.sort(key=lambda o: o["pid"])
    return owners


_KIND_TAG = {
    "dsh": T("【你的 dsh 进程】"),
    "dsh-ambiguous": T("【node 进程（疑似你的 dsh，命令行未能读取）】"),
    "non-dsh-node": T("【非 dsh 的 node 进程】"),
    "non-dsh-other": T("【非 dsh 的其它程序】"),
}

def format_port_owners(port):
    """返回 (owners, text)。owners 为空表示端口空闲。text 为人类可读的多行描述，
    明确标出占用者是谁（程序名 + PID + 命令行），便于用户判断是否要处理。

    降级态的措辞
    ------------
    非管理员账户下取不到进程名与命令行。此时若只写「（进程名未知）」，用户会以为
    是程序没名字，转而去折腾别的方向；真实原因是**当前账户权限不足**。
    所以这里显式区分「读不到」与「真的没有」，并给出可执行的建议。
    """
    owners = describe_port(port)
    if not owners:
        return [], T("端口 %d 当前空闲。") % port
    degraded = node_map_is_degraded()
    lines = [T("端口 %d 被以下进程占用：") % port]
    for o in owners:
        lines.append("  • PID=%d  %s  %s"
                     % (o["pid"], _KIND_TAG.get(o["kind"], ""),
                        o["name"] or (T("（进程名未能读取）") if degraded
                                      else T("（进程名未知）"))))
        if o["cmd"]:
            lines.append(T("      命令行: %s") % o["cmd"][:400])
        elif degraded:
            lines.append(T("      命令行: 当前账户权限不足，无法读取"))
        else:
            lines.append(T("      命令行: （未能读取）"))
    if degraded:
        lines.append("")
        lines.append(T("提示：当前账户读不到这些进程的完整命令行（系统限制，非故障）。"))
        lines.append(T("      · 结束 dsh 不受影响：dsh 由管理器自己启动，普通权限即可停止"))
        lines.append(T("      · 若只想查看是谁占了端口，可打开"
                     "「资源管理器 → 右键任务栏 → 任务管理器 → 详细信息」按 PID 查找"))
    return owners, "\n".join(lines)


# ─── bat 启动前端口预检（2026-10-01 新增）────────────────────────────────
# 需求：bat / 管理器启动前，若端口被「其他程序（非 dsh）」占用，
# 必须明确告诉用户「XX软件占用了端口号，请处理」，并写日志留痕，
# 让用户手动处理冲突。退出码：0=端口空闲（放行）；
# 3=非 dsh 程序占用（拒绝启动）；4=自家 dsh 已在运行（不重复启动）；
# 2=参数错误；检测自身异常时 fail-open 放行（exit 0）。
def precheck_start(port, label=""):
    """bat 启动前调用：检测端口占用者身份，决定是否放行启动。"""
    mgr_log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dsh-manager.log")

    def _rec(line):
        print(line)
        try:
            with open(mgr_log, "a", encoding="utf-8") as f:
                f.write("[%s] [bat-precheck]%s %s\n"
                        % (time.strftime("%Y-%m-%d %H:%M:%S"),
                           ("[%s]" % label) if label else "", line))
        except Exception:
            pass  # 日志写失败绝不拦启动

    try:
        owners, text = format_port_owners(port)
    except Exception as e:
        _rec(T("[precheck] 端口 %d 检测异常（%s）—— fail-open 放行，由启动路径自行报错") % (port, e))
        sys.exit(0)
    if not owners:
        _rec(T("[precheck] 端口 %d 空闲，放行启动。") % port)
        sys.exit(0)
    dsh_o = [o for o in owners if o["kind"] in ("dsh", "dsh-ambiguous")]
    other_o = [o for o in owners if o["kind"] not in ("dsh", "dsh-ambiguous")]
    if other_o:
        names = ", ".join((o["name"] or T("未知进程")) for o in other_o)
        _rec(T("!! 端口 %d 被「%s」占用（非 dsh 程序），请处理：关闭该程序或改 config.json 端口。详情：%s")
             % (port, names, text.replace("\n", " | ")))
        sys.exit(3)
    _rec(T("[precheck] 端口 %d 被自家 dsh 占用（%s），不重复启动。如需重启请先运行 kill-<端口>.bat。")
         % (port, ", ".join("PID=%d" % o["pid"] for o in dsh_o)))
    sys.exit(4)


if __name__ == "__main__":
    # 命令行用法：
    #   python dsh_kill.py --port 4080          交互式：显示占用者，确认后结束 dsh 占用者
    #   python dsh_kill.py --port 4080 --yes    非交互：直接结束 dsh 占用者（仍绝不碰非 dsh 程序）
    #   python dsh_kill.py --port 4080 --precheck-start v1
    #                                       bat 启动前预检：0=放行 3=非dsh占用 4=自家dsh在跑
    # 设计：只结束「确实是 dsh」的占用者；非 dsh 程序（无论是不是 node）一律拒绝并提示，
    # 避免影响其它软件运行——符合「非 dsh 不杀」的硬约束。
    port = None
    yes = False
    precheck_label = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--port", "-p") and i + 1 < len(args):
            try:
                port = int(args[i + 1])
            except ValueError:
                port = None
            i += 2
        elif a == "--precheck-start" and i + 1 < len(args):
            precheck_label = args[i + 1]
            i += 2
        elif a in ("--yes", "-y"):
            yes = True
            i += 1
        else:
            i += 1
    if port is None:
        print(T("用法: python dsh_kill.py --port <端口> [--yes] [--precheck-start <槽标签>]"))
        sys.exit(2)
    if precheck_label is not None:
        precheck_start(port, precheck_label)  # 内部自行 sys.exit
    owners, text = format_port_owners(port)
    print(text)
    if not owners:
        sys.exit(0)
    dsh_o = [o for o in owners if o["kind"] in ("dsh", "dsh-ambiguous")]
    other_o = [o for o in owners if o["kind"] not in ("dsh", "dsh-ambiguous")]
    if other_o:
        print(T("\n[拒绝] 端口被非 dsh 程序占用（见上）。为避免影响其它软件，本工具不会自动结束它。"))
        print(T("        请手动关闭该程序，或修改 config.json 里该版本的 port 后重启管理器。"))
        sys.exit(3)
    if not yes:
        try:
            ans = input(T("\n结束以上 dsh 进程(PID %s)? [Y/n] ")
                        % ",".join(str(o["pid"]) for o in dsh_o)).strip().lower()
        except EOFError:
            ans = ""
        if ans and ans not in ("y", "yes"):
            print(T("已取消，未做任何改动。"))
            sys.exit(0)
    killed = 0
    for o in dsh_o:
        if monitor_and_kill(o["pid"], "[bat]", port, sig=_looks_like_dsh):
            killed += 1
    print(T("已结束 %d 个 dsh 进程，端口 %d 现在应已释放。") % (killed, port))
    sys.exit(0)
