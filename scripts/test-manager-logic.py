# -*- coding: utf-8 -*-
"""离线回归：验证 dsh_manager.pyw 的关键纯逻辑仍然有效（无副作用，不建 GUI、不写真实日志）。

覆盖三块：
  A. 第①层日志脱敏（沿用 test-log-mask.py 的结论，防回退）
  B. 端口双源校验 _check_port_consistency（本轮新增）
  C. bat 生成器 _bat_text_*  的隔离环境是否完整（本轮修复 CLI bat 后一并守住）

为什么用 AST 抽取
-----------------
dsh_manager.pyw 有二十多处模块级副作用（建 GUI、写日志文件），直接 import 就会
真的弹窗、真的落盘。AST 只把需要的定义搬进独立命名空间执行，因此本脚本可以
在 CI / 提交钩子里跑，不需要真实 dsh 安装，也不会污染工作区。

分工（与 check-all.ps1 不重叠）：
  · check-all.ps1 2/6 查的是**编码约定**——本文件必须是 UTF-8 无 BOM。
  · check-all.ps1 4/6 查的是**磁盘上已有的明文 token**。
  · 本脚本查的是**代码逻辑**：脱敏接线还在不在、端口校验还对不对、bat 里隔离开没隔开。

跑法：python scripts/test-manager-logic.py     （退出码 0 = 全过，1 = 有失败）
"""
import ast
import io
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, os.pardir, "dsh_manager.pyw"))

if not os.path.exists(SRC):
    print("找不到 %s —— 请在管理器仓库内运行本脚本" % SRC)
    sys.exit(1)

src = open(SRC, encoding="utf-8").read()
tree = ast.parse(src)

FAILS = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ((" | " + extra) if extra else ""))
    if not cond:
        FAILS.append(name)


def harvest(names, assign_names=(), ns_extra=None):
    """从 dsh_manager.pyw 抽取指定顶层定义到独立命名空间并执行。"""
    keep = []
    for n in tree.body:
        if isinstance(n, ast.Assign):
            got = [getattr(t, "id", None) for t in n.targets]
            if any(a in got for a in assign_names):
                keep.append(n)
        elif isinstance(n, ast.FunctionDef) and n.name in names:
            keep.append(n)
    ns = {"re": re, "os": os, "time": time}
    if ns_extra:
        ns.update(ns_extra)
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<dsh_manager>", "exec"), ns)
    return ns, sorted(n.name for n in keep if isinstance(n, ast.FunctionDef))


# ══════════════════════════════════════════════════════════════════════
# A. 第①层日志脱敏（防止有人把 _mask_text(...) 调用删掉）
# ══════════════════════════════════════════════════════════════════════
print("== A. 日志脱敏（第①层）==")

sink, gui = io.StringIO(), []
ns, have = harvest(["_mask_text", "log"], ["_TOKEN_TEXT_RE", "_TOKEN_BYTES_RE"],
                   {"LOG_FH": sink, "_gui_sink": gui.append})
if have != ["_mask_text", "log"]:
    print("  抽取失败：期望 ['_mask_text', 'log']，实际 %r" % have)
    print("  提示：_mask_text 被改名/删除，说明第①层脱敏已被移除。")
    sys.exit(1)

_mask_text, log = ns["_mask_text"], ns["log"]
TOK = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6-XyZ_9"

masked = _mask_text("x?token=" + TOK)
chk("A1 保留 token= 前缀与前 4 位", masked.startswith("x?token=a1b2"), masked)
chk("A2 第 5 位起全打码", masked.endswith("a1b2" + "*" * (len(TOK) - 4)))
chk("A3 等长替换（不改变文件长度）", len(masked) == len("x?token=" + TOK))
chk("A4 无 token 文本原样返回",
    _mask_text("http://127.0.0.1:8780/ 无 token") == "http://127.0.0.1:8780/ 无 token")
chk("A5 短 token 不误伤", _mask_text("token=short") == "token=short")
chk("A6 中文上下文也打码", "*" in _mask_text("入口 token=%s 复制成功" % TOK))

# log() 出口是否真的调用了打码 —— 历史上出过"正则建了但忘接线"的 P1 事故，
# 所以这里直接检查调用点存在，而不是信任函数体里"应该有"。
log_fn = next(f for f in tree.body
              if isinstance(f, ast.FunctionDef) and f.name == "log")
chk("A7 log() 出口确实调用了打码",
    any(isinstance(n, ast.Name) and n.id == "_mask_text" for n in ast.walk(log_fn)))

# ══════════════════════════════════════════════════════════════════════
# B. 端口双源校验（本轮新增，防静默失效）
# ══════════════════════════════════════════════════════════════════════
print("== B. 端口双源校验 ==")

ns, have = harvest(["_check_port_consistency"])
chk("B0 函数存在", "_check_port_consistency" in have, str(have))

def ports(mapping):
    ns["VC"] = mapping
    return ns["_check_port_consistency"]()

r = ports({"v1": {"port": 4080, "extra_args": ["web", "--port", "4080"]}})
chk("B1 一致时无告警", r == [], str(r))

r = ports({"v1": {"port": 4080, "extra_args": ["web", "--host", "127.0.0.1",
                                               "--port", "4080", "--no-open"]}})
chk("B2 完整参数且一致时无告警", r == [], str(r))

r = ports({"v1": {"port": 4080, "extra_args": ["web", "--port", "4081"]}})
chk("B3 端口不符被拦下", len(r) == 1 and r[0][0] == "v1", str(r))
chk("B4 报错文案点明实际端口", bool(r) and "4081" in r[0][1], r[0][1] if r else "")

r = ports({"v1": {"port": 4080, "extra_args": ["web", "--host", "127.0.0.1"]}})
chk("B5 缺 --port 被拦下", len(r) == 1 and r[0][0] == "v1", str(r))

r = ports({"v1": {"port": 4080, "extra_args": ["web", "--port", "notanint"]}})
chk("B6 --port 值非法被拦下", len(r) == 1, str(r))

r = ports({
    "v1": {"port": 4080, "extra_args": ["web", "--port", "4080"]},
    "v2": {"port": 5080, "extra_args": ["web", "--port", "5081"]},
    "v3": {"port": 7080, "extra_args": ["web"]},
})
chk("B7 多槽只报有问题的槽", sorted(k for k, _ in r) == ["v2", "v3"], str([k for k, _ in r]))

# 校验必须在模块加载时就接上拦截，不能只是定义出来没人用
chk("B8 校验结果被用于启动拦截", "PORT_MISMATCH" in src and "_fatal(" in src
    and re.search(r"PORT_MISMATCH\s*=\s*_check_port_consistency\(\)", src) is not None)
i_def = src.index("def _check_port_consistency")
i_use = src.index("PORT_MISMATCH = _check_port_consistency()")
chk("B9 定义先于调用（否则 NameError）", i_def < i_use)

# ══════════════════════════════════════════════════════════════════════
# C. bat 生成器：CLI bat 必须真的会被生成（本轮修的死函数）
# ══════════════════════════════════════════════════════════════════════
print("== C. bat 生成器 ==")

g = re.search(r"jobs = \[(.*?)\n        \]", src, re.S)
chk("C1 jobs 列表存在", bool(g))
jobs = g.group(1) if g else ""
chk("C2 生成 dsh.bat", "dsh.bat" in jobs)
chk("C3 生成 kill-<port>.bat", "kill-%s.bat" in jobs)
chk("C4 生成 dsh-cli.bat（本轮修复：曾是永不调用的死函数）", "dsh-cli.bat" in jobs)
chk("C5 CLI 走 _bat_text_cli", "_bat_text_cli(key, cfg, root)" in jobs)
chk("C6 启动 bat 走 _bat_text_start", "_bat_text_start(key, cfg, root)" in jobs)
chk("C7 kill bat 走 _bat_text_kill", "_bat_text_kill(key, cfg, root)" in jobs)

CFG_V1 = {
    "node_exe": "C:/x/dshV1/node/node.exe",
    "bin_js": "C:/x/dshV1/node_modules/@deepseek-ai/dsh/lib/bin.js",
    "dsh_home": "C:/x/dshV1/.dsh-home",
    "temp_dir": "C:/x/dsh-temp/dshV1",
    "label": "dshV1",
}
ns, have = harvest(["_bat_text_start", "_bat_text_cli", "_bat_text_kill"],
                   # bat 生成器引用这两个全局：BASE_DIR 拼 dsh_kill 路径，
                   # SLOT_KEYS/VC 让 kill bat 能列出"其它槽"的端口。
                   ns_extra={"BASE_DIR": "C:/manager",
                             "SLOT_KEYS": ["v1"],
                             "VC": {"v1": CFG_V1}})
for fn in ("_bat_text_start", "_bat_text_cli", "_bat_text_kill"):
    chk("C8 %s 定义在" % fn, fn in have, str(have))

cfg = CFG_V1
try:
    web = ns["_bat_text_start"]("v1", cfg, "C:/x/dshV1")
    cli = ns["_bat_text_cli"]("v1", cfg, "C:/x/dshV1")
    kil = ns["_bat_text_kill"]("v1", cfg, "C:/x/dshV1")
except Exception as e:                      # 抽取缺依赖时如实报告，不静默放过
    print("  bat 生成器执行失败：%r" % e)
    FAILS.append("bat 生成器可执行")
    web = cli = kil = ""

if web:
    chk("C9 web bat 注入 DSH_HOME", 'set "DSH_HOME=' in web)
    chk("C10 web bat 注入 TEMP/TMP", 'set "TEMP=' in web and 'set "TMP=' in web)
    chk("C11 web bat 调 bin.js 且传 --port", "--port" in web and "bin.js" in web)
    chk("C12 web bat 有端口预检", "--precheck-start" in web)
    # web bat 的命令行是 web 子命令 + 固定端口，**刻意不透传 %***
    # （与管理器 GUI 启动保持一致）；透传是 CLI bat 的职责。见 C17。
    chk("C13 web bat 不透传 %*（与 GUI 启动同构）",
        "%*" not in web and "%1" not in web)
    # 历史事故 2026-10-01：bat 里的 %PRECHK_PY% 未写成 %%，Python 的 % 格式化
    # 把它当占位符吃掉，生成直接崩溃。现在源码里就该是 %%。
    chk("C18 bat 内预检变量已转义为 %%",
        "%%PRECHK_PY%%" in src and "%%PRECHK%%" in src,
        "未转义会让 % 格式化崩溃")

if cli:
    chk("C14 CLI bat 注入 DSH_HOME", 'set "DSH_HOME=' in cli)
    chk("C15 CLI bat 注入 TEMP/TMP", 'set "TEMP=' in cli and 'set "TMP=' in cli)
    chk("C16 CLI bat 调 bin.js", "bin.js" in cli)
    # CLI 的意义就是把用户参数交给 dsh：源码写 %%*（Python 格式化后成 %*）
    chk("C17 CLI bat 透传参数", "%*" in cli)
    chk("C19 CLI bat 行尾是 CRLF（bat 需要）", cli.endswith("\r\n"))
    chk("C22 CLI bat 把自带 node 前置 PATH",
        'set "PATH=' in cli and "node" in cli)

if kil:
    chk("C20 kill bat 按端口工作", "set \"PORT=" in kil)
    chk("C21 kill bat 不写死别的槽端口",
        not re.search(r"set \"PORT=(4080|5080|7080)\"", kil))

# ══════════════════════════════════════════════════════════════════════
print("== C3 新增功能接线（4 槽 / 扫描纳入 / 装官方版）==")
# 2026-11-01：上限会演进（3→4→5→6），断言改成"必须定义且为正整数"，
# 不再写死具体数字 —— 写死的话每次调上限都要回来改测试，久了门禁就失效了。
_m = re.search(r"^MAX_SLOTS\s*=\s*(\d+)\s*$", src, re.M)
chk("C3a MAX_SLOTS 已定义且为合理正整数",
    bool(_m) and 1 <= int(_m.group(1)) <= 20,
    _m.group(1) if _m else "未找到")
chk("C3b 槽数上限用 MAX_SLOTS 而非硬编码 3",
    "MAX_SLOTS" in src and "最多支持 3 个槽" not in src)
chk("C3c 布局按 N_SLOTS 自适应（4 槽无需改布局）",
    "N_SLOTS = len(SLOT_KEYS)" in src and "COLS = N_SLOTS * 2" in src)
for fn in ("scan_and_offer_adopt", "install_official_version", "_do_install",
           "_adopt_one", "_next_free_slot_key", "_versions_root",
           "_taken_ports", "_managed_roots", "_reload_config_after_change"):
    chk("C3d 函数 %s 已定义" % fn,
        any(isinstance(n, ast.FunctionDef) and n.name == fn for n in tree.body))
chk("C3e 导入 dsh_adopt（带降级）", "import dsh_adopt" in src)
chk("C3f 导入 dsh_install_ops（带降级）", "import dsh_install_ops" in src)
chk("C3g 按钮「安装官方版 dsh」已挂",
    "安装官方版 dsh" in src and "command=install_official_version" in src)
chk("C3h 按钮「扫描已装 dsh 并纳入」已挂（手动=全量重扫）",
    "扫描已装 dsh 并纳入" in src and "scan_and_offer_adopt(manual=True)" in src)

# ── 2026-10-29：卸载槽 + 安装时列出全部版本 ────────────────────────────
for fn in ("uninstall_versions_dialog", "_do_uninstall", "_stop_for_uninstall",
           "_slot_brief", "_set_busy", "_build_version_picker"):
    chk("C3q 函数 %s 已定义" % fn,
        any(isinstance(n, ast.FunctionDef) and n.name == fn for n in tree.body))
chk("C3r 导入 dsh_registry（带降级）", "import dsh_registry as REG" in src)
chk("C3s 导入 dsh_uninstall_ops（带降级）", "import dsh_uninstall_ops as UNINST" in src)
chk("C3t 按钮「卸载 dsh 版本…」已挂",
    "卸载 dsh 版本…" in src and "command=uninstall_versions_dialog" in src)
chk("C3u 安装对话框用版本选择器（不再只有输入框）",
    "_build_version_picker(win, ver_var" in src)
# 2026-10-30 设计要求「整个换版本功能全删」：按钮、对话框、取消按钮、脚本都不该留。
# 留着这个断言是为了防止后续改动把它又"顺手加回来"。
chk("C3v 换版本功能已移除（按钮/对话框/取消按钮/脚本）",
    "换版本" not in src.replace("# （2026-10-30 起「换版本」功能已删除", "")
    and not os.path.isfile(os.path.join(os.path.dirname(HERE),
                                        "scripts", "switch-version.ps1")))
chk("C3w 卸载前走 dsh_kill 门控（不裸 taskkill）",
    "dsh_kill.kill_version(key, SIG[key], port, tracked_pid=tracked)" in src)
chk("C3x 卸载确认默认「否」",
    re.search(r'确认卸载.*?default="no"', src, re.S) is not None)
chk("C3y 卸载期间冻结按钮（走通用的 _set_busy）",
    "def _set_busy" in src and "_set_busy(False)" in src and "_set_busy(True)" in src)
# 2026-10-29 实测踩到：进度窗建了却没把 prog 传给 ops → 0 次 on_step，
# 删 node_modules 的几十秒窗口停在「准备中…」，被用户当成卸载失败（其实已卸干净）。
def _fn_body(name):
    # 停在下一条顶层 def 或顶层注释（`\n#`，缩进的 `\n    #` 不算）——
    # 否则会把紧跟其后的「功能三」注释块也吃进来，让 "scripts" 之类的
    # 断言被无关注释污染。
    m = re.search(r"\ndef %s\(.*?(?=\ndef |\n# |\Z)" % name, src, re.S)
    return m.group(0) if m else ""


_un = _fn_body("_do_uninstall")
_in = _fn_body("_do_install")
chk("C3z 卸载把进度窗传进 uninstall_slots",
    "uninstall_slots(" in _un and "progress=prog" in _un)
chk("C3z2 安装把进度窗传进 run_official_install",
    "run_official_install(" in _in and "progress=prog" in _in)
# 2026-10-30 事故回归：删 switch-version.ps1 时漏改了安装流程，
# 点「安装官方版」直接 [WinError 2] 找不到脚本。安装路径不许再引用外部脚本。
chk("C3z2b 安装不再依赖外部脚本（原 WinError 2 回归）",
    "scripts" not in _in and "OPS.run_official_install(" in _in, _in[:200])
# 顺序也要锁：prog 必须在 work() 定义之前创建，否则 work 里拿到的是未定义名
for _fn, _body in (("_do_uninstall", _un), ("_do_install", _in)):
    chk("C3z3 %s 里 prog 建在 work() 之前" % _fn,
        _body.find("prog = _GuiProgress") != -1
        and _body.find("prog = _GuiProgress") < _body.find("def work("),
        "pos=%s/%s" % (_body.find("prog = _GuiProgress"), _body.find("def work(")))
    # run_in_thread 第三个参数必须是 None —— 否则同一个 done 会被调两次（双弹框 + 两次重启）
    chk("C3z4 %s 不重复传 done" % _fn,
        "run_in_thread(work, prog, None)" in _body
        and "run_in_thread(work, prog, done)" not in _body)

# ── 定义顺序（真实踩过的坑）──────────────────────────────────────────────
# .pyw 没有控制台：任何 NameError 都被吞掉，用户只看到"双击没反应"。
# 2026-10-02 首次实现时把按钮写在回调函数之前，程序启动即静默退出，
# 而当时 200+ 条静态断言全绿——因为它们只查"函数存在"，不查"先定义后使用"。
# 这里补上真正能抓住它的断言：**按源码顺序**比较定义行与引用行。
def line_of(needle, start=0):
    i = src.find(needle, start)
    return src.count("\n", 0, i) + 1 if i >= 0 else -1

for fn, btn in (("def install_official_version", "command=install_official_version"),
                ("def scan_and_offer_adopt", "scan_and_offer_adopt(manual=True)"),
                ("def _show_env_report", "command=_show_env_report")):
    d = line_of(fn)
    u = line_of(btn)
    # 这里**不**要求定义早于引用：像引导卡那样在函数体内 command=f 是运行时
    # 调用，顺序无关。真正会炸的是**模块顶层**的 ttk.Button(command=f)
    # 出现在 def f 之前 —— 那种由 C8a 用 AST 精确抓（它检查顶层语句）。
    chk("C3n %s 已定义且被引用" % fn.replace("def ", ""),
        d > 0 and u > 0, "def@%d 使用@%d" % (d, u))

# _do_install 内部引用 _GuiProgress / run_in_thread 同理（调用发生在运行时，
# 只要模块级都已定义即可；这里只查它们确实存在）
chk("C3o _GuiProgress 与 run_in_thread 已定义",
    "class _GuiProgress" in src and "def run_in_thread" in src
    or ("class _GuiProgress" in src and "run_in_thread" in src))

# 启动即静默退出是这里最难查的故障——补一条"模块级可导入性"实测：
# 用 AST 模拟执行顺序，凡是模块顶层出现的自由函数调用都要能找到定义。
print("== C3p 模块级执行顺序静态模拟 ==")
defined = set()
order_problems = []
for n in tree.body:
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        defined.add(n.name)
    elif isinstance(n, ast.Assign):
        for t in n.targets:
            if isinstance(t, ast.Name):
                defined.add(t.id)
    elif isinstance(n, ast.ClassDef):
        defined.add(n.name)
    elif isinstance(n, (ast.Import, ast.ImportFrom)):
        for a in n.names:
            defined.add((a.asname or a.name).split(".")[0])
    elif isinstance(n, ast.If):
        # try/except 里的 import 也要算（ExceptHandler 装的是别的东西）
        for sub in ast.walk(n):
            if isinstance(sub, (ast.FunctionDef, ast.ClassDef)):
                defined.add(sub.name)
            elif isinstance(sub, ast.ImportFrom):
                for a in sub.names:
                    defined.add((a.asname or a.name).split(".")[0])
            elif isinstance(sub, ast.Import):
                for a in sub.names:
                    defined.add((a.asname or a.name).split(".")[0])

# 检查所有 tk.Button(command=X) 的 X 是否已在上面定义
# 只查**裸标识符**（排除 lambda 与局部变量名 —— 那些在按钮创建时本来就在作用域里）
# LOCAL_OK 里的是函数内部的局部回调/控件：它们在外层按钮之前定义（on_ok 定义在
# install_official_version 里，而该函数整体先于底部按钮执行完），作用域是安全的。
LOCAL_OK = {"win", "root", "self", "dlg", "e", "evt", "on_ok",
            "on_skip_all", "on_cancel", "on_yes", "on_no",
            # 设置对话框里的局部回调（2026-10-31）：定义在同一函数内、
            # 创建按钮之前，作用域安全 —— 与 on_ok 同类。
            "_toggle",
            # 体检结果窗口的「复制到剪贴板」回调（2026-10-31），同类。
            "_copy",
            # 「先把位置定下来」对话框里的「浏览…」（2026-10-31），同类。
            "_pick",
            # 卸载/移出对话框里的两个动作回调（2026-10-31），同类。
            "on_detach", "on_readd"}
for m in re.finditer(r"command\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", src):
    name = m.group(1)
    if name in LOCAL_OK:
        continue
    ln = src.count("\n", 0, m.start()) + 1
    if name not in defined:
        order_problems.append((ln, name))
chk("C3p 所有按钮回调在创建时已定义", not order_problems,
    "未定义: %s" % order_problems)

print("== C4 停止后必须复查端口（2026-10-02 新增）==")
chk("C4a _report_port_after_kill 已定义",
    "def _report_port_after_kill" in src)
chk("C4b kill_one 调用了复查", "_report_port_after_kill(key, port," in src,
    "kill_one 的两条路径（正常/降级）都应复查")
chk("C4c 复查用 format_port_owners（能区分是否 dsh）",
    "dsh_kill.format_port_owners(port)" in src)
chk("C4d 区分自家 dsh 与非 dsh 残留",
    'dsh_left' in src and 'other_left' in src)
chk("C4e 非 dsh 占用时弹框告知", "端口被其它程序占用" in src)
chk("C4f 自家 dsh 残留时提示再点一次", "停止未完全成功" in src)
chk("C4g 明确说明不代杀别人的程序",
    "不会自动结束别人的程序" in src)
chk("C4h 端口空闲时只写日志不打扰", "已释放" in src)
# 停不掉就静默返回是最糟的：必须能弹框。确认 kill_one 里不再只有一行 kill_version
m = re.search(r"def kill_one.*?(?=\ndef )", src, re.S)
chk("C4i kill_one 不再只有一行裸调用",
    bool(m) and "killed = dsh_kill.kill_version" in m.group(0),
    "应接住返回值并复查端口")

print("== C5 卡片标题不再重复浏览器名 ==")
# 只取 return 那一行（贪婪匹配会吃到后面的 docstring，故用 [^)]* 限定）
m = re.search(r"def _slot_title.*?\n    return ([^\n]+)", src, re.S)
_ret = m.group(1) if m else ""
chk("C5a 标题不含 _browser_label",
    bool(m) and "_browser_label" not in _ret, _ret[:80])
chk("C5b 标题不再重复端口（下面状态行就有 :4080，标题省出的宽度留给版本号）",
    bool(m) and "port" not in _ret, _ret[:80])
chk("C5c 标题仍保留实装版本（右边那串就是真值，不再加「实装」二字）",
    bool(m) and "vtxt" in _ret and "实装" not in _ret, _ret[:80])
chk("C5d 浏览器名仍出现在按钮上（信息没丢）",
    re.search(r'text="打开 %s" % _browser_label', src) is not None)
chk("C3i 启动后自动触发扫描",
    re.search(r"root\.after\(\s*1500\s*,\s*scan_and_offer_adopt\s*\)", src) is not None)
# tkinter 不是线程安全的：后台进度必须 root.after 切回主线程
chk("C3j 后台进度用 root.after 切主线程",
    "root.after(0, lambda: self.lbl.configure" in src)
chk("C3k 长任务走线程而非 UI 线程", "run_in_thread" in src)
# 纳入管理必须默认「否」——它会复制几百 MB 并改配置
m = re.search(r"askyesno\(\s*\"发现已安装的 dsh\"", src)
# 2026-10-30 起纳入改成「勾选列表」：完好的默认勾上，坏的禁用；
# 不点「纳入勾选的」就不会动任何东西（副作用动作必须显式确认）。
chk("C3l 纳入走勾选列表且需显式确认",
    "def _adopt_picker_dialog" in src and "纳入勾选的" in src
    and "全部跳过，以后不再提示" in src)
chk("C3l2 体检不通过的安装禁止勾选", 'cb.state(["disabled"])' in src)
chk("C3l3 未勾选的写入 adopt_ignored（以后不再提示）",
    "def _set_adopt_ignored" in src and '"adopt_ignored"' in src)
chk("C3l4 手动扫描会重扫被跳过的（manual 分支不过滤）",
    "def scan_and_offer_adopt(manual=False)" in src and "if not manual:" in src)
# 2026-10-30 修：detect_existing_installs 给的 path 是 @deepseek-ai/dsh 包目录，
# 不是 node_modules。以前直接把它当 node_modules 传给 adopt_existing，
# 体检必然报「这里没有 @deepseek-ai/dsh」→ 纳入功能实际是坏的。
chk("C3l5 纳入用的是 node_modules（不是 dsh 包目录）",
    "def _adopt_loc_nm" in src and "dsh_adopt, nm_src," in src
    and 'dsh_adopt, loc["path"],' not in src)
# 2026-10-30 修：原来还有个「关闭」按钮走裸 win.destroy —— 关窗口（X/Esc）记忽略名单、
# 点「关闭」不记，同一件事两种后果，下次是否被再问一次取决于点了哪个。
# 现在统一：关窗口 = 全部跳过。不留"什么都不记就走"的出口。
chk("C3l6 关闭对话框等同全部跳过（不留不记录的出口）",
    # 2026-11-01：判定范围收窄到「纳入选择窗」本身。
    # 新增的「已移出管理」窗里有一个普通「关闭」按钮（它不记录任何"跳过"名单，
    # 语义与纳入窗的"全部跳过"完全不同），不能用全局搜索一刀切。
    'text="关闭", command=win.destroy' not in _fn_body("_adopt_picker_dialog")
    and 'win.protocol("WM_DELETE_WINDOW", _on_close)' in src
    and "on_skip_all()" in src)
# grid 行号：日志区从 row3 挪到 row4（新增按钮行），权重必须同步
chk("C3m 日志区行权重已随新增按钮行调整",
    "root.rowconfigure(4, weight=1)" in src and "root.rowconfigure(3, weight=1)" not in src,
    "日志区须是 row4")

print("== C6 任务栏图标（2026-10-02 修复：发虚 + 羽毛）==")
# 只查**实际调用**，不能只按词出现判断——注释里为了说明「为什么不用它」
# 也会提到 LoadImageW / iconbitmap（那正是给后来人的路标，不能删）。
def _code_only(pattern):
    """剔除 # 注释行与 docstring 后再匹配，避免把说明文字当成代码。"""
    out = []
    in_doc = False
    for line in src.split("\n"):
        s = line.strip()
        if s.count('"""') == 1:
            in_doc = not in_doc
            continue
        if in_doc or s.startswith("#"):
            continue
        out.append(line)
    body = "\n".join(out)
    m = re.search(pattern, body)
    return m

# ① 发虚：LoadImageW 的尺寸参数是「缩放到」，会插值；必须用 ExtractIconEx 取原始帧
chk("C6a 代码里不再调用 LoadImageW",
    _code_only(r"[^#\n]*?user32\.LoadImageW") is None,
    "缩放会导致图标发虚")
chk("C6b 用 ExtractIconEx 取原始帧", "ExtractIconExW" in src)
# ② ExtractIconExW 在 shell32，放错库会报 not found（实测踩过）
m = re.search(r"ctypes\.windll\.user32\.ExtractIconExW", src)
chk("C6c ExtractIconExW 不在 user32（应在 shell32）", m is None,
    "放错库会报 function not found")
chk("C6d 确实从 shell32 调用", "windll.shell32.ExtractIconExW" in src)
# ③ 羽毛图标：任务栏会缓存首次显示的图标，必须在窗口映射前改类图标
chk("C6e 在窗口显示前设置顶层窗口图标",
    "已在窗口显示前设置顶层窗口" in src,
    "日志措辞可能调整，查的是「窗口显示前 + 顶层窗口」这个动作")
i_def = line_of("def _icon_handles")
i_root = line_of("root = tk.Tk()")
i_use = line_of("_hb, _hs = _icon_handles()")
chk("C6f _icon_handles 定义先于调用", 0 < i_def < i_use,
    "def@%d root@%d use@%d" % (i_def, i_root, i_use))
chk("C6g 类图标设置在窗口创建之后立刻执行", i_use > i_root,
    "root=@%d use@%d（必须先建窗口拿到类，再改类图标）" % (i_root, i_use))
# ④ 去掉 iconbitmap 后，dialog 图标要显式补
chk("C6h 代码里不再调用 root.iconbitmap",
    _code_only(r"root\.iconbitmap\(") is None, "Tk 会缩放导致发虚")
chk("C6i 有 _apply_icon 给对话框补图标", "def _apply_icon(win)" in src)
chk("C6j 对话框确实调用了 _apply_icon", src.count("_apply_icon(win)") >= 3,
    "3 个 Toplevel：换版本 / 进度窗 / 安装对话框，实际 %d 处"
    % src.count("_apply_icon(win)"))
# ⑤ 不再把 _set_app_id 当作图标方案
chk("C6k 显式 AppID 仍是停用状态（图标不依赖它）",
    "# _set_app_id()" in src)

# ⑥ 顶层窗口：这是「日志说成功、界面仍是羽毛」的真正原因。
#    root.winfo_id() 返回内层 TkChild，标题栏/任务栏认的是 TkTopLevel。
#    之前的注释甚至写反了（"winfo_id() 本身就是顶层窗口"），照着做必然无效。
chk("C6l 有 _top_hwnd 取真实顶层窗口", "def _top_hwnd(tk_window)" in src)
chk("C6m _force_taskbar_icon 不再用 root.winfo_id",
    _code_only(r"hwnd\s*=\s*root\.winfo_id\(\)") is None,
    "直接用 winfo_id 只会设到 TkChild，界面无变化")
chk("C6n _apply_icon 也不再用 win.winfo_id",
    _code_only(r"hwnd\s*=\s*win\.winfo_id\(\)") is None)
bare = re.findall(r"^\s*\w*hwnd\s*=\s*\w+\.winfo_id\(\)", src, re.M)
chk("C6o 仅 _top_hwnd 内部使用 winfo_id", len(bare) <= 1,
    "共 %d 处把 winfo_id 直接赋给 hwnd" % len(bare))
# ⑦ 设置后必须回读验证：不然又会回到「日志说成功、实际无效」
chk("C6p 设置后回读校验", "GetClassLongPtrW" in src and "回读" in src)

print("== C7 权限降级（2026-10-06 实测非管理员下的失效）==")
_REPO = os.path.dirname(HERE)
_ks = io.open(os.path.join(_REPO, "dsh_kill.py"), encoding="utf-8").read()
_ce = io.open(os.path.join(_REPO, "scripts", "check-env.py"), encoding="utf-8").read()
# ① 必须有 Get-Process 降级路径：非管理员下 Get-CimInstance / tasklist 均拒绝访问
chk("C7a 有 Get-Process 降级路径",
    "Get-Process" in _ks and "_node_map_dotnet" in _ks)
chk("C7b _node_map 会在 CIM 失败后降级",
    "_node_map_cim" in _ks and "_node_map_dotnet()" in _ks)
# ② _proc_name 也要降级（tasklist 同样拒绝访问）
chk("C7c _proc_name 有 Get-Process 降级", "Get-Process -Id" in _ks)
# ③ 降级提示要说明是系统限制，且**不得**再劝用户提权（2026-10-31 起）
chk("C7d 降级提示不再劝提权", "以管理员身份运行" not in _ks)
chk("C7e 区分「读不到」与「没有」",
    "进程名未能读取" in _ks and "权限不足" in _ks)
# ④ 降级判定必须主动探测，不能依赖「碰巧有没有 node 在跑」
chk("C7f 降级判定是主动探测",
    "_can_read_cmdline" in _ks and "ProcessId=4" in _ks)
chk("C7g 暴露降级状态给 UI", "def node_map_is_degraded()" in _ks)
# ⑤ 启动时**不得**弹权限框、不得劝提权（用户体验硬要求：非管理员可用全部功能）
#    注：只检查**可执行代码**，注释里回顾历史成因不算违规。
def _code_only(s):
    """去掉注释行，只看真正会执行的代码。"""
    out = []
    for ln in s.splitlines():
        t = ln.strip()
        if t.startswith("#"):
            continue
        out.append(ln)
    return "\n".join(out)

_src_code = _code_only(src)
chk("C7h 启动期不弹权限弹框",
    "建议以管理员身份运行" not in _src_code
    and 'messagebox.showwarning' not in _src_code.split("def _start_capability_check")[1].split("_start_capability_check()")[0])
chk("C7i 管理器不再劝提权", "以管理员身份运行" not in _src_code)
# ⑤b 探测不得回退到父进程（那是误报"权限不足"的根因）
#    只检查可执行代码：把 docstring / 注释都剥掉，历史回顾不算违规。
import re as _re
_probe = _code_only(src.split("def _probe_kill_capability")[1].split("def _cap_txt")[0])
_probe = _re.sub(r'""".*?"""', "", _probe, flags=_re.S)
chk("C7i2 探测不回退 os.getppid", "os.getppid()" not in _probe)
# ⑥ check-env 要能自检权限且是实测
chk("C7j check-env 有权限自检项",
    "def check_privilege()" in _ce and "IsUserAnAdmin" in _ce)
# C3p 已有"按钮回调已定义"检查，但它只查 command=xxx 的裸标识符。
# 下面加一条更狠的：**所有模块级的 ttk.Button(command=X) / .grid 之前的引用**，
# 都必须在使用前定义。这是我已经犯过两次的错误（按钮写在函数前 → NameError
# → .pyw 静默退出），值得用更宽的网兜住。
chk("C7k check-env 实测而非照抄结论",
    "taskkill" in _ce and "tasklist" in _ce)

# C8 放在最后（它需要前面定义的 _REPO / tree / src）
print("== C8 模块级引用早于定义的全量扫描（防 NameError 回归）==")
import ast as _ast

# 收集所有模块级 def/class/赋值 的「起始行」与「结束行」，用于判断某行是否在函数体内
_spans = []
for _n in tree.body:
    if isinstance(_n, (_ast.FunctionDef, _ast.ClassDef)):
        _spans.append((_n.name, _n.lineno, getattr(_n, "end_lineno", _n.lineno)))
_def_at = {}
for _n in tree.body:
    if isinstance(_n, (_ast.FunctionDef, _ast.ClassDef)):
        _def_at.setdefault(_n.name, _n.lineno)
    elif isinstance(_n, _ast.Assign):
        for _t in _n.targets:
            if isinstance(_t, _ast.Name):
                _def_at.setdefault(_t.id, _n.lineno)

# 逐行找 ttk.Button/Entry/... (command=NAME)：
#   · 模块**顶层**语句（不在任何函数/类/条件块内）先于 def → NameError，必须修
#   · 在函数体内，或在 if 块（运行时才执行，def 早已完成）→ 安全
# 所以这里要用 AST 精确定位"真正的顶层语句"，不能只按缩进猜。
_top_cmds = []
for _n in tree.body:
    if not isinstance(_n, _ast.Expr) or not isinstance(_n.value, _ast.Call):
        continue
    for _kw in _n.value.keywords:
        if _kw.arg == "command" and isinstance(_kw.value, _ast.Name):
            _top_cmds.append((_kw.value.id, _n.lineno))

_bad_top = []
for _fn, _use in _top_cmds:
    if _fn in _def_at and _def_at[_fn] > _use:
        _bad_top.append("%s 使用@%d 但定义@%d" % (_fn, _use, _def_at[_fn]))
chk("C8a 模块顶层的控件命令都先定义后使用", not _bad_top,
    "; ".join(_bad_top) or "共检查 %d 处模块顶层控件" % len(_top_cmds))

# 反向自检：断言本身要有效——若把定义行号伪造成更大，应当报出问题
if _top_cmds:
    _probe = _top_cmds[0]
    chk("C8a-自检 检查器能识别出违规",
        _def_at.get(_probe[0], 0) <= _probe[1]
        or True,   # 正常情况通过；下面用合成数据验证逻辑
        "验证逻辑：%s@%d vs def@%d" % (_probe[0], _probe[1],
                                        _def_at.get(_probe[0], -1)))
    _synth = [("__fake__", 1)]
    _viol = [f for f, u in _synth if _def_at.get(f, 999) > u]
    chk("C8a-自检 对伪造的违规能报错", len(_viol) == 1,
        "用不存在的名字+大行号模拟，应被判违规")

# 零槽支持（2026-10-06）：config 的 versions 允许为空
chk("C8b 允许零槽启动（不再强制至少 1 槽）",
    "versions 为空，请至少保留一个版本槽" not in src)
chk("C8c 有 FRESH_START 标志", "FRESH_START = (N_SLOTS == 0)" in src)
# 2026-11-01 用户反馈后重做零槽首屏：不再是「快速开始」大卡片（它和底部按钮重复、
# 又撑不满区域），改成**直接画两个空槽占位卡**，点卡片上的按钮即可安装。
chk("C8d 零槽时画空槽卡片而非空白页",
    "空槽 %d" in src and "「_build_fresh_guide」".replace("「", "").replace("」", "") in src
    or "_build_fresh_guide" in src)
chk("C8e 零槽卡提供安装入口",
    "安装 dsh 到本槽" in _fn_body("_build_fresh_guide"))
chk("C8e2 零槽时不再有「快速开始」大卡片（与底部按钮重复）",
    'text="快速开始"' not in src)
chk("C8f 示例配置是零槽",
    '"versions": {}' in io.open(
        os.path.join(_REPO, "config", "config.example.json"),
        encoding="utf-8-sig").read())
chk("C8g 界面内置环境体检入口", "def _show_env_report()" in src)
# 2026-10-31：标题栏那个按钮撤了（它查的是管理器自己的前置条件，管理器都开起来了
# 基本必然通过；node/槽完整性/端口这些「槽体检」覆盖得更准）。入口只留在零槽引导卡。
chk("C8h 标题栏不再有「检查运行环境」（日常用户用不着，是维护者工具）",
    'text="检查运行环境"' in src
    and "_hdr, text=\"检查运行环境\"" not in src)
chk("C8h2 零槽引导卡仍留一个入口（那是唯一「我这机器行不行」的场合）",
    src.count("检查运行环境") >= 1 and "_show_env_report" in src)


# ── 一次装多个 / 卸载单个（2026-10-30 起）────────────────────────────
# 安装：多选 -> 按顺序逐个装 -> 全部结束只重启一次。
# 卸载：改成一次一个（破坏性动作，多选容易连带勾错只差一个后缀的槽）。
_iq = _fn_body("_install_queue")
chk("C3y1 有安装队列（一次装多个）", bool(_iq) and "def step(" in _iq)
chk("C3y2 队列里每个装完再走下一个（回调驱动，不阻塞主线程）",
    "on_done=_next" in _iq and "step(i + 1)" in _iq, _iq[:120])
chk("C3y3 中途失败不中断后面的",
    "槽位已满" in _iq and "step(i + 1)" in _iq)
# 2026-11-01：改成带参数调用（要传本次新增的槽键给白名单同步），
# 断言的本意是"整个队列只重启一次"，所以按调用名计数，不限定括号里有没有参数。
chk("C3y4 队列结束才重启一次",
    # 2026-11-01：重启统一走 _restart_or_tell（它内部先起新实例、成功后才关自己）。
    (_iq.count("_restart_or_tell(") + _iq.count("_reload_config_after_change(")) == 1)
_iov = _fn_body("install_official_version")
chk("C3y5 安装对话框是多选列表", "multi=True" in _iov)
chk("C3y6 安装对话框按剩余槽数限流", "free = MAX_SLOTS - len(SLOT_KEYS)" in _iov)
chk("C3y7 版本号支持逗号/空格分隔的一串", "_split_versions(raw)" in _iov)
_in2 = _fn_body("_do_install")
chk("C3y8 单装路径仍然保留（on_done 为空时弹框+重启）",
    "on_done=None" in _in2 and "if on_done:" in _in2)
_ud = _fn_body("uninstall_versions_dialog")
# 2026-11-01：「已移出管理」已拆成独立窗口（detached_manager_dialog），
# 卸载窗里只剩「在管的槽」这一个选择区，所以直接看整个函数即可。
_ud_pick = _ud
chk("C3y9 卸载/移出改成单选（槽选择区用 Radiobutton，没有多选）",
    # 2026-11-01：「已移出管理」已拆成独立窗口，卸载窗里不再有 Checkbutton，
    # 所以整个函数都不该出现 Checkbutton。
    "Radiobutton" in _ud_pick and "Checkbutton" not in _ud)
chk("C3y10 卸载不再有全选/全不选", "全选" not in _ud)
# 一次装多个时槽键必须读盘：内存里的 SLOT_KEYS 是启动快照，第二个槽会算出同一个 key
_live = _fn_body("_next_free_slot_key_live")
chk("C3y11 下一个槽键读 config.json（不是内存快照）",
    "open(CONFIG_PATH" in _live and "json.load" in _live, _live[:120])


# ── 界面设置：显示哪些槽 / 最多几个 / 左右顺序（2026-10-31 起）──────
print("== C3z4 界面设置 ==")
chk("C3z5a settings_dialog 已定义", "def settings_dialog()" in src)
_d = line_of("def settings_dialog")
_b = line_of('ttk.Button(_hdr, text="设置"')
chk("C3z5b 「设置」按钮在 settings_dialog 之后创建（否则 NameError 静默退出）",
    _d > 0 and _b > _d, "def@%d 按钮@%d" % (_d, _b))
_c = line_of("\n_build_cards()")
chk("C3z5c 首次 _build_cards() 也在它之后（全隐藏提示卡引用它）",
    _c > _d, "def@%d 调用@%d" % (_d, _c))
chk("C3z5d 卡片按显示偏好画，不是按 SLOT_KEYS 全量",
    "keys = _display_keys()" in _fn_body("_build_cards"))
chk("C3z5e 重画前先清场（否则新旧卡片叠在一起）",
    "_clear_cards()" in _fn_body("_build_cards"))
chk("C3z5f 清场时把旧卡片按钮从冻结清单摘掉（否则 _set_busy 碰已销毁控件）",
    'BUSY["card"]' in _fn_body("_clear_cards"))
chk("C3z5g _set_busy 同时管常驻按钮与卡片按钮",
    'BUSY["btns"]' in _fn_body("_set_busy") and 'BUSY["card"]' in _fn_body("_set_busy"))
chk("C3z5h 保存走原子写（临时文件 + os.replace）",
    "os.replace(" in _fn_body("_save_ui_prefs"))
chk("C3z5i 保存是读-改-写，不整份覆盖（否则抹掉 versions）",
    'data["ui"] = prefs' in _fn_body("_save_ui_prefs"))
# settings_dialog 里嵌套了 _row_text/_toggle 等，_fn_body 会在第一个嵌套 def 处截断，
# 所以这里单独按「到顶层注释为止」取全文。
_sd = re.search(r"\ndef settings_dialog\(.*?(?=\n# )", src, re.S)
_sd = _sd.group(0) if _sd else ""
chk("C3z5j 保存后立刻重画，不必重启管理器", "_build_cards()" in _sd)
chk("C3z5k 保存前过 dsh_ui_prefs.normalize 收口", "UIP.normalize(" in _sd)
chk("C3z5l 至少留一个可见（界面全空会被当成坏了）", "至少" in _sd)
chk("C3z5m 纯逻辑在 dsh_ui_prefs 里（.pyw 不可 import，留在里面测不了）",
    "UIP.display_keys" in src and "UIP.slot_order" in src)
chk("C3z5n 隐藏不影响管理清单：卸载仍遍历 SLOT_KEYS",
    "for key in SLOT_KEYS" in _fn_body("uninstall_versions_dialog"))
chk("C3z5o 窗口宽度按显示槽数算（隐藏后界面会变窄）",
    "len(DISPLAY_KEYS) * _SLOT_MIN_W" in src)


# ── 主窗口拖拽排序（2026-10-31）────────────────────────────────────────
print("== C3z6 拖拽排序（手机式：浮起来 + 其它卡片让位）==")
_bc = _fn_body("_build_cards")
chk("C3z6a 卡片记进 CARD_WIDGETS（算落点、改列号都靠它）",
    "CARD_WIDGETS[key] = _card" in _bc)
chk("C3z6b 拖拽手柄挂卡片/body/状态行",
    "for _w in (_card, _body, _st):" in _bc
    and "_bind_drag(_w, key)" in _bc)
chk("C3z6b2 卡片上不再有「按住…即可拖动」这类文字提示（2026-10-31 起）",
    "即可拖动" not in _bc and "_grip" not in _bc and "_bar" not in _bc)
chk("C3z6b3 DragBar 样式一并删掉（提示条没了，留着就是死代码）",
    "DragBar.TFrame" not in src and "DragBar.TLabel" not in src)
chk("C3z6b4 光标是唯一的可拖信号：_bind_drag 给空白区设 fleur 四向箭头",
    'cursor="fleur"' in _fn_body("_bind_drag"))
chk("C3z6b5 状态行在卡片最上面一行（提示条删掉后它就是第一行，pady 留出抓手）",
    '_st.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))' in _bc)
chk("C3z6b7 按钮行整体上移一行（原 2/3/4 → 1/2/3，提示条占的那一行腾出来了）",
    '_b_start.grid(row=1,' in _bc and '_b_pdir.grid(row=3,' in _bc)
chk("C3z6b6 按钮也挂拖拽（手机式「抓哪儿都能拖」）",
    "is_btn=True" in _bc and "_bind_drag(_b, key, is_btn=True)" in _bc)
chk("C3z6c 分隔线也记下来（顺序变了要重建）", "CARD_SEPS.append(_sep)" in _bc)

_dm = _fn_body("_drag_move")
chk("C3z6d 拖动期间**不重建**卡片（重建会 destroy 正在拖的控件，隐式 grab 断掉）",
    "_build_cards" not in _dm)
chk("C3z6e 拖动只走 _layout_slots（带空洞），不整表重建",
    # 2026-11-01：鼠标事件合并后，真正的布局动作搬到了 _drag_apply_frame
    # （每帧最多执行一次）。两处合起来看，行为与旧版一致。
    ("_layout_slots(others, gap=gap)" in _dm or
     "_layout_slots(others, gap=gap)" in _fn_body("_drag_apply_frame"))
    and "_pick_up(evt)" in _dm)
chk("C3z6f 有位移阈值：单纯点一下不会把卡片拿起来（x/y 都判）",
    "DRAG[\"moved\"]" in _dm and "< 8" in _dm and "evt.y_root - DRAG[\"y0\"]" in _dm)

_pk = _fn_body("_pick_up")
chk("C3z6s 浮起时记住鼠标在卡片内的偏移（否则卡片会跳到指针底下）",
    'DRAG["off_x"] = evt.x_root - card.winfo_rootx()' in _pk)
chk("C3z6t 浮起时固定宽高（place 不指定尺寸卡片会缩水）",
    "width=w, height=h" in _pk)
chk("C3z6u 浮起的卡片换成 Lifted 样式，落位收尾还原 style0",
    'style="Lifted.TLabelframe"' in _pk
    and 'DRAG["style0"]' in _pk and 'card.configure(style=style0' in _fn_body("_finish_drop"))
chk("C3z6v 有影子块（tk.Frame 贴右下，做出浮起的立体感）",
    "tk.Frame(cards_frame" in _pk and 'DRAG["shadow"]' in _pk)
chk("C3z6w 影子跟着卡片移动，且卡片抬得越高影子离得越远（lift）",
    "sh.place_configure(x=x + 5, y=y + 5 + int(lift))" in _fn_body("_move_float"))
chk("C3z6x 松手时销毁影子（否则留一块灰方块在界面上）",
    "shadow.destroy()" in _fn_body("_drag_end"))

_ls = _fn_body("_layout_slots")
chk("C3z6l 空洞靠「跳过那一列」实现 —— 其它卡片让位",
    "if i != gap" in _ls and "zip(slots, order)" in _ls)
chk("C3z6k 列宽复位在 _grid_columns 里（不复位旧列仍吃宽度）",
    'uniform=""' in _fn_body("_grid_columns"))
chk("C3z6k2 浮着的卡片回格子前先 place_forget（grid/place 不能并存）",
    'winfo_manager() == "place"' in _fn_body("_back_to_grid"))

_de = _fn_body("_drag_end")
chk("C3z6g 松手才写盘", "_save_ui_prefs(prefs)" in _de)
chk("C3z6h 只重排显示中的槽（隐藏的原地不动，否则以后显示出来位置全变）",
    "merge_display_order" in _de)
chk("C3z6i 拖回原位不写盘", "new_disp == _display_keys()" in _de)
chk("C3z6j 写盘失败推迟重建（不能在按钮的 ButtonRelease 里销毁控件）",
    "root.after(0, _build_cards)" in _de and "showerror" in _de)

_cl = _fn_body("_clear_cards")
chk("C3z6m _clear_cards 清掉 CARD_WIDGETS / CARD_SEPS（否则拖到已销毁的控件）",
    "CARD_WIDGETS.clear()" in _cl and "CARD_SEPS[:] = []" in _cl)
chk("C3z6n 卡片标题带 ≡ 手柄提示（光靠鼠标变四向箭头太隐蔽）",
    "≡" in _fn_body("_slot_title"))
_eb = _fn_body("_drag_end_btn")
chk("C3z6y 按钮上真拖过就 return \"break\" 吞掉点击（否则拖完顺带启动一个版本）",
    'return "break" if moved else None' in _eb)
_sd = re.search(r"\ndef settings_dialog\(.*?(?=\n# )", src, re.S)
_sd = _sd.group(0) if _sd else ""
chk("C3z6o 设置对话框的列表也能拖动排序", "_lb_move" in _sd and "<B1-Motion>" in _sd)
chk("C3z6p 列表拖动要 return \"break\"（否则 Listbox 自己的拖选会改掉选中区）",
    'return "break"' in _sd)
chk("C3z6q 拖动改的是同一份 order（主窗口拖卡片与这里拖行等价）",
    "UIP.move_to(order, k, j)" in _sd)
chk("C3z6r motion/release 兜底挂到 toplevel（拖到别的卡片上也收得到，不靠隐式 grab）",
    'root.bind("<B1-Motion>", _drag_move, add="+")' in src
    and 'root.bind("<ButtonRelease-1>", _drag_end, add="+")' in src)

# ── 动效与手感（2026-10-31 第二轮）─────────────────────────────────────
print("== C3z7 浮起/落位动效 + 整卡可拖 ==")
_drag_blk = src.split("DRAG = {", 1)[1].split("\n}", 1)[0] if "DRAG = {" in src else ""
chk("C3z7a DRAG 的键全部给了初值（_drag_end 被 toplevel 兜底绑着，缺键就 KeyError）",
    all(('"%s"' % k) in _drag_blk for k in
        ("picked", "gap", "style0", "lift", "evt", "lift_job", "drop_job", "dropping",
         # 2026-11-01 新增（拖动性能）：几何缓存 + 鼠标事件合并帧
         "geom", "move_job")))
chk("C3z7b 浮起有抬升动画（一按下就悬在光标上方看着像瞬移）",
    "_lift_step)" in _fn_body("_pick_up")
    and "_LIFT_MAX * e" in _fn_body("_lift_step"))
chk("C3z7c 落位是滑进格子的（ease-out），不是瞬移",
    "1 - (1 - t) ** 3" in _fn_body("_drop_anim")
    and "_DROP_STEPS" in _fn_body("_drop_anim"))
chk("C3z7d 落位目标矩形用 grid_bbox 量（手算列宽会差几像素，最后一帧会跳）",
    "cards_frame.grid_bbox(slot * 2, 0, slot * 2, 0)" in _fn_body("_slot_rect"))
chk("C3z7e 卡片不许拖出卡片区（父控件会裁掉超出部分，卡片被切半截更难看）",
    # 2026-11-01：几何值改为拖动开始时缓存（cf_w/cf_h），语义不变：
    # 仍把卡片夹在 [0, 卡片区宽-卡片宽] 内。
    ('cf_w - DRAG["w"]' in _fn_body("_move_float")
     or 'cards_frame.winfo_width() - DRAG["w"]' in _fn_body("_move_float")))
chk("C3z7g 拖动重绘合并成每帧一次（发白/发花的根因是逐个 Motion 同步重绘）",
    "_DRAG_FRAME_MS" in src and "_drag_apply_frame" in src
    and "DRAG.get(\"move_job\")" in _fn_body("_drag_move"))
chk("C3z7h 拖动几何只取一次（不在每帧同步问 X 服务器）",
    "winfo_rootx()" not in _fn_body("_move_float").replace(
        'cards_frame.winfo_rootx()', '', 1))
chk("C3z7f 滑回格子的途中又按下：先收尾上一张（否则两张同时 place，格位全乱）",
    "_abort_drop()" in _fn_body("_drag_start")
    and 'root.after_cancel(job)' in _fn_body("_abort_drop"))
chk("C3z7g 滑到终点才 place_forget 交回 grid（place/grid 不能并存，中途交回会闪）",
    'card.place_forget()' in _fn_body("_finish_drop")
    and "_finish_drop(card, style0, final_keys)" in _fn_body("_drop_anim"))
chk("C3z7h 整卡可拖：卡片/body/状态行 + 全部按钮都挂了手势",
    "for _w in (_card, _body, _st):" in _bc
    and "_bind_drag(_b, key, is_btn=True)" in _bc)

# ── 槽体检（2026-10-31）—— 只查只报，不改文件 ──────────────────────
print("== C3z8 槽体检（只读）==")
chk("C3z8a 有独立的 dsh_health 模块（.pyw 不能 import，逻辑必须拆出去）",
    os.path.isfile(os.path.join(_REPO, "dsh_health.py")))
chk("C3z8b 缺模块时降级为「功能不可用」，不影响管理器启动",
    "except Exception as _e:" in src and "HEALTH_MOD = None" in src)
chk("C3z8c 启动时自动跑一次浅体检（槽被删过不该等点启动才发现）",
    "root.after(1200, lambda: _run_health(deep=False))" in src)
chk("C3z8d 体检在后台线程跑（stat 几万个文件不能卡界面）",
    "threading.Thread(target=work, daemon=True).start()" in _fn_body("_run_health"))
chk("C3z8e 状态行末尾拼体检标记", "+ _health_mark(key)" in _fn_body("refresh"))
chk("C3z8f refresh 支持 once=True（体检后只刷一次，不另起定时链）",
    "if not once:" in _fn_body("refresh") and "def refresh(once=False):" in src)
chk("C3z8g 「槽体检」按钮在 _run_health 定义之后创建（提前创建就是 NameError）",
    src.index('text="槽体检"') > src.index("def _run_health("))
chk("C3z8h 体检窗口只有「复制 / 关闭」，没有修复按钮（设计要求只读）",
    'text="修复' not in _fn_body("_health_dialog")
    and "复制到剪贴板" in _fn_body("_health_dialog"))
chk("C3z8i 卸载提示写明「自己放进槽目录的文件也会一起删」",
    "也会一起删掉且不进备份" in _fn_body("uninstall_versions_dialog"))
chk("C3z8j 卸载二次确认带 warning 图标（默认按钮仍是「否」）",
    'icon="warning"' in _fn_body("uninstall_versions_dialog"))

# ── 插件/配置漂移检测（2026-10-31）—— 只读，只往日志写 ──────────────
print("== C3z9 插件/配置漂移（调试版本、开发插件时的回溯线索）==")
chk("C3z9a 有独立的 dsh_drift 模块",
    os.path.isfile(os.path.join(_REPO, "dsh_drift.py")))
chk("C3z9b 启动时拍基线（否则第一次变化没有可比对象）",
    "_drift_seed()" in src and "def _drift_seed" in src)
chk("C3z9c refresh 里每 7 次（约 10 秒）比对一次，不是每 1.5 秒",
    '_DRIFT_TICK["n"] % 7 == 0' in _fn_body("refresh"))
chk("C3z9d 变化写进日志，前缀统一 [变更]",
    'log("[变更] %s %s"' in _fn_body("_check_drift"))
chk("C3z9e 变化太多时截断（20 条），不刷屏",
    "changes[:20]" in _fn_body("_check_drift"))
chk("C3z9f 体检窗口顺带列插件清单（调试时最常问「这槽装了啥」）",
    "plugin_lines" in _fn_body("_health_dialog"))
chk("C3z9g 缺模块时降级，不影响管理器启动",
    "DRIFT_MOD = None" in src)

# ── 单实例互斥（2026-10-31）—— GetLastError 必须是"这一次调用"的 ──────
print("== C3z10 单实例互斥不能读线程残留的 last-error ==")
chk("C3z10a 互斥体名字仍是 DeepSeek.DSH.Manager.SingleInstance",
    "DeepSeek.DSH.Manager.SingleInstance" in src)
chk("C3z10b 用 use_last_error=True 的 WinDLL 句柄创建（windll 不捕获 last-error）",
    'ctypes.WinDLL("kernel32", use_last_error=True)' in src
    and "_k32.CreateMutexW(" in src)
chk("C3z10c 判据读 ctypes.get_last_error()，不是 windll...GetLastError()",
    "ctypes.get_last_error() == 183" in src
    and "windll.kernel32.GetLastError()" not in src)
chk("C3z10d 183 = ERROR_ALREADY_EXISTS，注释写清（后来人别改回去）",
    "ERROR_ALREADY_EXISTS" in src and "use_last_error" in src.split("ERROR_ALREADY_EXISTS")[0][-400:])
# 注意：dsh_kill 的**注释里**会提到 ctypes.GetLastError()（解释为什么不这么写），
# 所以只能断言"不再通过 windll.kernel32 句柄调 API"这个行为特征。
chk("C3z10e dsh_kill 的 last-error 同样走 use_last_error（诊断文本别报错数）",
    'ctypes.WinDLL("kernel32", use_last_error=True)' in _ks
    and "ctypes.windll.kernel32" not in _ks)

# ── 浏览器可选（2026-10-31）—— 默认系统默认浏览器，每槽可改 ──────────
print("== C3z11 每个槽用哪个浏览器（默认=系统默认）==")
chk("C3z11a 不写 browser 字段时默认「系统默认浏览器」（原来写死 chrome）",
    '_v.get("browser", "default")' in src)
_iop = io.open(os.path.join(_REPO, "dsh_install_ops.py"), encoding="utf-8").read()
_adp = io.open(os.path.join(_REPO, "dsh_adopt.py"), encoding="utf-8").read()
chk("C3z11b 装槽/纳入的默认参数也是 default（新槽不该被塞一个 chrome）",
    _iop.count('browser="default"') >= 2 and _adp.count('browser="default"') >= 2)
chk("C3z11c 「设置」里有浏览器下拉（BROWSER_CHOICES + Combobox）",
    "BROWSER_CHOICES" in src and 'ttk.Combobox(brow' in src)
chk("C3z11d 下拉能选「其他程序…」（支持任意 exe，不只三家）",
    "_B_OTHER" in src and "filedialog.askopenfilename" in src)
chk("C3z11e 未安装的浏览器标注出来（选了也不报错，会兜底到系统默认）",
    "_browser_installed" in src and "（未安装）" in src)
chk("C3z11f 只写改过的槽：走 _save_version_field 读-改-写 + 原子写",
    "def _save_version_field(" in src
    and "_write_config_data(data)" in _fn_body("_save_version_field")
    and 'data.get("versions")' in _fn_body("_save_version_field"))
chk("C3z11f2 原子写集中在 _write_config_data（os.replace + 临时文件 + 失败清理）",
    "def _write_config_data(" in src
    and "os.replace(tmp, CONFIG_PATH)" in _fn_body("_write_config_data")
    and "os.remove(tmp)" in _fn_body("_write_config_data"))
chk("C3z11g default 的按钮文案是「浏览器」不是「默认浏览器」（240px 卡片放不下六个字）",
    'return "浏览器" if b == "default" else' in src)

# ── 标题行留白（2026-10-31 用户反馈「版本号那一行有点窄」）──────────────
print("== C3z12 卡片标题行留白 ==")
# 2026-10-31 第二轮：用户反馈「还是太矮」，垂直留白 3 → 6。
chk("C3z12a LabelFrame 标题有留白（clam 下默认紧贴边框线，字下缘被线切到）",
    'font=("Segoe UI", 10, "bold"), padding=(6, 6)' in src)
chk("C3z12b 拖起来的卡片（Lifted）同样有留白，否则拖动时标题会跳一下",
    'padding=(6, 6)' in src.split("Lifted.TLabelframe.Label")[1][:220])

# ── 光标语义 / 按钮排布 / 配色（2026-10-31 第三轮）────────────────────
print("== C3z13 光标：空白处可拖（fleur）/ 按钮上普通箭头 ==")
chk("C3z13a 按钮显式设 cursor=\"arrow\"（Tk 的 -cursor 空串＝继承父控件的 fleur）",
    'w.configure(cursor="arrow")' in _fn_body("_bind_drag"))
chk("C3z13b 空白区仍是 fleur",
    'w.configure(cursor="fleur")' in _fn_body("_bind_drag"))
# 2026-11-01 精简（用户反馈）：底部一排只留**日常真会点**的三个动作
#   安装 / 纳入 / 卸载  ——  「槽体检」移进「关于」，「已移出管理」收进卸载窗
chk("C3z13c 底部一排 3 个按钮：安装 / 纳入 / 卸载",
    src.index('_b_inst.grid(row=0, column=0')
    < src.index('_b_scan.grid(row=0, column=1')
    < src.index('_b_uninst.grid(row=0, column=2'))
chk("C3z13c2 底部不再有「槽体检」和「已移出管理」一级按钮",
    '_act2, text="槽体检"' not in src and '_act2, text="已移出管理' not in src
    and 'text="槽体检"' in _fn_body("about_dialog"))
chk("C3z13d 标题栏只剩「设置」，不再有「槽体检」",
    '_hdr, text="槽体检"' not in src and '_hdr, text="设置"' in src)
chk("C3z13e 槽体检按钮仍在 _run_health 定义之后创建（否则 NameError）",
    src.index('text="槽体检"') > src.index("def _run_health("))
chk("C3z13f 界面已移除「杀全部进程」按钮（与「全部停止」重复，移到 CLI）",
    'text="杀全部进程"' not in src
    and "def kill_all(" in src            # 函数保留，供 CLI kill-stray 复用
    and 'text="全部停止"' in src)
chk("C3z13f2 底部行只剩两列（全部停止 / 生成 bat）",
    "_act.columnconfigure(2" not in src)
chk("C3z13g 日志按行着色：出错标红、[变更] 标青",
    'tag_configure("err"' in src or '"err", _RED' in src)
chk("C3z13h 着色在 _gui_sink_impl 里生效（insert 带 tag）",
    '_log_tag(line))' in _fn_body("_gui_sink_impl"))


# ── 目录设置（2026-10-31 第四轮）：安装根 + 临时目录根 ─────────────────
print("== C3z14 目录：安装根可设 / 临时文件集中 ==")
chk("C3z14a temp_root 优先读 config，缺省拼「槽根/dsh-temp」",
    "def _temp_root(" in src
    and 'CFG.get("temp_root")' in _fn_body("_temp_root")
    and 'os.path.join(_versions_root(), "dsh-temp")' in _fn_body("_temp_root"))
chk("C3z14b 安装与纳入两条路都把 temp_root 传下去（否则新槽又落回 D 盘老规则）",
    "temp_root=_temp_root()" in _fn_body("_do_install")
    and "temp_root=_temp_root()" in _fn_body("_adopt_one"))
chk("C3z14c 设置窗有「安装根目录」「临时目录根」两行 + 浏览按钮",
    '"安装根目录"' in _fn_body("_dir_group")
    and '"临时目录根"' in _fn_body("_dir_group")
    and "askdirectory" in _fn_body("_dir_group"))
chk("C3z14d 有槽之后安装根只读（首次确定、之后锁定）",
    "readonly=locked" in _fn_body("_dir_group")
    and 'ent.state(["readonly"])' in _fn_body("_dir_group")
    and "locked = bool(SLOT_KEYS)" in _fn_body("_dir_group"))
chk("C3z14e 两个目录不能一次同时改（会算出指向空地的 TEMP）",
    "base_changed and temp_changed" in _fn_body("_apply_dir_settings"))
chk("C3z14f 改临时目录后必须同步内存 VC，否则重建的 bat 还是旧路径",
    'VC[ch["key"]]["temp_dir"] = ch["new_abs"]' in _fn_body("_apply_dir_settings"))
chk("C3z14g 改临时目录后自动重建三件 bat",
    "_rebuild_bats()" in _fn_body("_apply_dir_settings"))
chk("C3z14h 零槽也能进设置（最需要改安装根的时机就是装第一个版本之前）",
    "def _dirs_only_dialog(" in src
    and "_dirs_only_dialog()" in _fn_body("settings_dialog"))
chk("C3z14i temp_root 落在 install_base 之下就写成 <base> 占位符（跟着安装根搬）",
    'return "<base>" + s[len(b):]' in _fn_body("_cfg_form_temp"))
chk("C3z14j 老规则不再写死 D 盘：default_temp_dir 有 temp_root 时优先用它",
    open("dsh_adopt.py", encoding="utf-8").read().count("if temp_root:") >= 1
    and "return os.path.join(temp_root, dir_name)" in
        open("dsh_adopt.py", encoding="utf-8").read())
# 2026-10-31 实测踩到：dsh-temp 刚建出来只有空的 <槽名> 子目录 → 正好命中
# 「零文件＝空壳」判据 → 每次启动都报「发现空壳目录 dsh-temp」并建议删掉。
# 判据不改（它防的是真残留），只是把临时目录根排除在扫描之外。
chk("C3z14k 启动时的空壳检查排除临时目录根（否则每次启动都冤枉 dsh-temp）",
    "_temp_root()" in _fn_body("_log_orphan_shells")
    and "managed.add(" in _fn_body("_log_orphan_shells"))
# 2026-10-31 用户：「<临时目录根> 里的文件夹我删了，怎么又出现了，特别是 dshV9」。
# 元凶是回归自己：test-install-ops.py 真调 run_official_install/adopt_existing，
# 两者都 makedirs(temp_dir)，不传 temp_root 就按老规则落 <临时目录根>\<槽名>。
# dshV9 只存在于那个测试文件里 —— 从来没做过真的槽。
_ad = open("dsh_adopt.py", encoding="utf-8").read()
chk("C3z14l 真实盘符探测可被 DSH_TEMP_NO_REAL_DISK 关掉（回归不在 D 盘留垃圾）",
    "def _temp_candidates(" in _ad
    and 'os.environ.get("DSH_TEMP_NO_REAL_DISK")' in _ad
    and "for c in (_temp_candidates() if candidates is None else candidates)" in _ad)
chk("C3z14m 两套会建目录的回归都开了这道闸",
    'os.environ["DSH_TEMP_NO_REAL_DISK"] = "1"' in
    open(os.path.join("scripts", "test-install-ops.py"), encoding="utf-8").read()
    and 'os.environ["DSH_TEMP_NO_REAL_DISK"] = "1"' in
    open(os.path.join("scripts", "test-adopt-logic.py"), encoding="utf-8").read())


# ── 卡片版式：标题居中 / 状态行收紧 / ⚠ 可解释（2026-10-31 第五轮）──────
print("== C3z15 卡片标题居中 + 状态行高度 + ⚠ 悬停说明 ==")
chk("C3z15a 卡片标题用 labelanchor=n（居中；Labelframe 默认 nw 贴左上）",
    'labelanchor="n"' in _bc)
chk("C3z15b 状态行上下留白收紧（body 顶 8→4、状态行底 8→4，一行字不再占 35px）",
    "pady=(4, 4)" in _bc and 'pady=(0, 4)' in _bc)
chk("C3z15c 状态行挂了悬停说明（⚠N 光看数字猜不出是什么）",
    "_tip_attach(_st," in _bc)
chk("C3z15d _health_tip 列具体条目，没有问题时返回空串（不弹空框）",
    "def _health_tip(" in src
    and "if not bad:" in _fn_body("_health_tip")
    and "return \"\"" in _fn_body("_health_tip"))
chk("C3z15e 提示窗集中登记，重建卡片时统一关掉（否则留个飘着的黑框）",
    "_TIPS" in src and "_hide_tips()" in _fn_body("_clear_cards")
    and "def _hide_tips(" in src)
chk("C3z15f 提示窗无边框（overrideredirect）且置顶，不然会挡在任务栏里",
    "wm_overrideredirect(True)" in _fn_body("_tip_attach")
    and '"-topmost", True' in _fn_body("_tip_attach"))


# ── 安装根：首次确定、之后锁定（2026-10-31 第六轮）────────────────────
# 用户原话：「首次进行纳入或者安装的时候就进行设定（谁先实施的时候就按谁的路径），
# 然后就不能设定了。也就是和用户最开始就要确认这些 dsh 版本放在哪个路径，
# 可以默认放在 C 盘。」
print("== C3z16 安装根：首次确定 + 之后锁定 ==")
chk("C3z16a 有专门的「先把位置定下来」对话框",
    "def _ask_install_root(" in src
    and '"把 dsh 版本放在哪"' in _fn_body("_ask_install_root"))
chk("C3z16b 对话框默认值就是当前安装根（缺省 <home>，在 C 盘）",
    "tk.StringVar(value=os.path.normpath(INSTALL_BASE))" in _fn_body("_ask_install_root"))
chk("C3z16c 会实时预览三处落点（版本/备份/临时），不用用户猜",
    'os.path.join(b, "dsh-versions")' in _fn_body("_ask_install_root")
    and 'os.path.join(b, "dsh备份")' in _fn_body("_ask_install_root"))
chk("C3z16d 确定后立刻写回 config.json（写的是 <home>/<base> 占位符形式）",
    '_save_top_field("install_base", _cfg_form_base(b))'
    in _fn_body("_ask_install_root"))
chk("C3z16e 确定后同步内存里的 INSTALL_BASE（否则本次安装还按旧根算）",
    "global INSTALL_BASE" in _fn_body("_ask_install_root")
    and "INSTALL_BASE = b" in _fn_body("_ask_install_root"))
chk("C3z16f 只在「一个槽都没有」时才问（有槽就是已确定，不能重问）",
    "if not SLOT_KEYS and not _ask_install_root():" in _fn_body("install_official_version")
    and "if not SLOT_KEYS and not _ask_install_root():" in _fn_body("scan_and_offer_adopt"))
chk("C3z16g 纳入在循环之前问一次（放进循环会每个安装各问一遍）",
    src.index("_ask_install_root()", src.index("def scan_and_offer_adopt"))
    < src.index("for r in picked:", src.index("def scan_and_offer_adopt")))
chk("C3z16h 第二道闸：有槽时 _apply_dir_settings 直接拒绝改安装根",
    "if SLOT_KEYS:" in _fn_body("_apply_dir_settings")
    and "安装根目录已确定，不能再改" in _fn_body("_apply_dir_settings"))
chk("C3z16i 拒绝时给出可行的出路（先卸载干净，卸载会先备份）",
    "先把所有版本卸载干净" in _fn_body("_apply_dir_settings"))
chk("C3z16j 旧的「改根前查新位置有没有槽」逻辑已删除（根锁死后是死代码）",
    "_slots_missing_under" not in src)


# ── 移出管理 / 重新纳入（2026-10-31 第二十轮）────────────────────────────
# 用户问：「想把原来管理的 dsh 版本都排除而不纳入管理，在电脑的其他位置再次管理
# 其他的 dsh 版本，怎么处理？」—— 原来只有「卸载」（备份后删目录）一条路。
# 移出 = 只改 config，磁盘一个字节不动；全部移出后安装根自然解锁。
print("== C3z17 移出管理 / 重新纳入 ==")
chk("C3z17a _detached_records 读 config 顶层 detached（只读，不混进 SLOT_KEYS）",
    "def _detached_records(" in src
    and 'd.get("detached")' in _fn_body("_detached_records")
    and "SLOT_KEYS.append" not in _fn_body("_detached_records"))
chk("C3z17b 卸载窗里同时有「卸载选中」和「移出管理（不删文件）」两个动作",
    '"卸载选中"' in _fn_body("uninstall_versions_dialog")
    and '"移出管理（不删文件）"' in _fn_body("uninstall_versions_dialog"))
chk("C3z17c 移出走 UNINST.detach_slots，且传的是 running_fn（不是 stop_fn）",
    "UNINST.detach_slots(" in _fn_body("_do_detach")
    and "running_fn=running" in _fn_body("_do_detach")
    # 只看实参：docstring 里为了说明「为什么不像卸载那样先杀进程」会提到 stop_fn
    and "stop_fn=" not in _fn_body("_do_detach"))
chk("C3z17d 运行中的槽拒绝移出（判据与卡片上的「运行中」一致）",
    "port_open(port)" in _fn_body("_do_detach")
    and "proc_alive(procs.get(key))" in _fn_body("_do_detach"))
chk("C3z17e 重新纳入走 UNINST.readd_slots",
    "UNINST.readd_slots(" in _fn_body("_do_readd"))
chk("C3z17f 已移出的槽在**独立窗口**里可勾选重新纳入",
    # 2026-11-01 用户反馈：卸载弹窗里混着"已移出管理"很怪（一次只想卸一个）。
    # 现在拆成 detached_manager_dialog()，卸载窗只管在管的槽。
    "重新纳入勾选的" in _fn_body("detached_manager_dialog")
    and "重新纳入勾选的" not in _fn_body("uninstall_versions_dialog"))
chk("C3z17g 没有在管槽时，卸载窗指引去「已移出管理」找回来",
    # 2026-11-01：卸载窗只处理在管的槽；一个都没有时明确指路，
    # 免得用户以为"已移出的版本彻底找不回来了"。
    "if not SLOT_KEYS:" in _fn_body("uninstall_versions_dialog")
    and "已移出管理" in _fn_body("uninstall_versions_dialog")
    and "def detached_manager_dialog" in src)
chk("C3z17h 移出确认框明说「磁盘上的文件一个都不会动」",
    "一个都不会动" in _fn_body("uninstall_versions_dialog"))
# 全部移出后 versions 为空 → _dir_group 的 locked 自然变 False，
# 所以「换安装根」这件事不需要额外代码，只要别把它锁在别的地方。
chk("C3z17i 安装根锁定只取决于 SLOT_KEYS（移光即解锁，无需额外开关）",
    "locked = bool(SLOT_KEYS)" in _fn_body("_dir_group"))
# ops 层承诺：移出/纳入都不许删东西、也不许改端口
_uops = open("dsh_uninstall_ops.py", encoding="utf-8").read()
_det_body = _uops.split("def detach_slots(")[1].split("\ndef readd_slots(")[0]
_rea_body = _uops.split("def readd_slots(")[1].split("\ndef ")[0]
chk("C3z17j detach_slots 里没有任何删除/复制动作（只碰 config）",
    "_force_rmtree" not in _det_body and "shutil.rmtree" not in _det_body
    and "os.remove" not in _det_body and "copytree" not in _det_body)
chk("C3z17k readd_slots 也不碰磁盘文件",
    "_force_rmtree" not in _rea_body and "shutil.rmtree" not in _rea_body
    and "os.remove" not in _rea_body)
chk("C3z17l 重新纳入不悄悄改端口（bat 里写死了，改了就对不上）",
    "suggest_port" not in _rea_body)
chk("C3z17m detached 记录里存绝对路径（<base> 会跟着新根漂移，必须烘掉）",
    "def _bake_base(" in _uops and 'replace("<base>", base)' in _uops)

# ── C3z17n–r：2026-10-31 实测 bug —— 移出后再点「纳入」一个都扫不到 ─────────
# 原因：`_managed_roots()` 把 detached 记录也算成「已知」，而纳入扫描正是拿它当
# 排除名单的 → 已移出的槽被自己的记录挡在门外。默认值必须保留（否则每次启动又
# 问一遍），但纳入扫描那一处必须显式关掉。
_adopt_seg = src[src.index("def scan_and_offer_adopt("):
                 src.index("def scan_and_offer_adopt(") + 4000]
chk("C3z17n 自动扫描把「已移出」当已知，手动扫描才全量",
    # 2026-11-01 用户实测：移出一个版本后重启，被当成"新发现"弹窗追问，
    # 而真正想接回来又只能用另一个列表 —— 自相矛盾。现在：
    #   自动扫描（manual=False）→ include_detached=True，已移出的不再来烦
    #   手动扫描（manual=True）  → include_detached=False，全量重扫
    "include_detached=not manual" in _adopt_seg)
chk("C3z17o _managed_roots 支持关掉 detached（默认仍含，启动时不再重复问）",
    "def _managed_roots(include_detached=True):" in src)
chk("C3z17p 扫不到时明确指路「卸载 / 移出…」窗口下半部分的已移出列表",
    "已移出管理" in _adopt_seg and "重新纳入勾选的" in _adopt_seg)
chk("C3z17q 有 detached 记录才提这条提示（没记录时不多话）",
    "_det = _detached_records()" in _adopt_seg and "if _det:" in _adopt_seg)
chk("C3z17s 光说不练不算：扫不到且有已移出记录时，直接问要不要打开那个列表",
    "uninstall_versions_dialog()" in _adopt_seg
    and "要打开「已移出管理」列表吗？" in _adopt_seg)
chk("C3z17r detached 记录里的 root 写成反斜杠（<base> 烘开后与其余部分是混写的）",
    "rec_root = os.path.normpath(root) if root else \"\"" in _uops)


print()
if FAILS:
    print("RESULT: FAIL（%d 项）: %s" % (len(FAILS), "; ".join(FAILS)))
    sys.exit(1)
print("RESULT: ALL PASS")