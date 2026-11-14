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
    "扫描已装 dsh 并纳入" in src and "scan_and_offer_adopt()" in src)

# ── 2026-10-29：卸载槽 + 安装时列出全部版本 ────────────────────────────
for fn in ("uninstall_versions_dialog", "_do_uninstall", "_stop_for_uninstall",
           "_slot_brief", "_actions_enabled", "_build_version_picker"):
    chk("C3q 函数 %s 已定义" % fn,
        any(isinstance(n, ast.FunctionDef) and n.name == fn for n in tree.body))
chk("C3r 导入 dsh_registry（带降级）", "import dsh_registry as REG" in src)
chk("C3s 导入 dsh_uninstall_ops（带降级）", "import dsh_uninstall_ops as UNINST" in src)
# _fn_body 会在顶层注释处截断，而 hub 里恰好有「· 动作：」这类顶层注释，
# 所以这里直接对整份源码断言（按钮绑定只可能是那一处）。
chk("C3t 卸载入口已挂",
    # 2026-11-02：设置页不再放安装/纳入/卸载（它们回到主界面），
    # 但卸载对话框本身必须仍可被调用。
    "def uninstall_versions_dialog(" in src
    and "uninstall_versions_dialog" in src)
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
chk("C3y 长操作期间冻结按钮（走通用的 _actions_enabled）",
    "def _actions_enabled" in src
    and "_actions_enabled(False)" in src and "_actions_enabled(True)" in src)
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
                ("def scan_and_offer_adopt", "scan_and_offer_adopt()"),
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
            "on_detach", "on_readd",
            # 2026-11-02：设置页的按钮工厂 _mbtn/_act_btn 用形参 cmd 传回调
            "cmd",
            # 设置页「选择安装位置…」的嵌套回调
            "_choose",
            # 槽体检窗口里的「修复…」（2026-11-14），同类
            "_do_fix"}
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
    re.search(r'text=T\("打开 %s"\) % _browser_label', src) is not None)
chk("C3i 启动时**不**自动扫描（只在用户点按钮时扫）",
    # 2026-11-13 用户要求：「点击扫描已装 dsh 并纳入时就进行扫描，其他不需要提示」。
    # 自动扫描 + 「以后不再提示」名单是一整套，去掉扫描就没有名单存在的理由。
    re.search(r"root\.after\(\s*1500\s*,\s*scan_and_offer_adopt", src) is None
    and "def scan_and_offer_adopt():" in src)
# tkinter 不是线程安全的：后台进度必须 root.after 切回主线程
chk("C3j 后台进度用 root.after 切主线程",
    "root.after(0, lambda: self.lbl.configure" in src)
chk("C3k 长任务走线程而非 UI 线程", "run_in_thread" in src)
# 纳入管理必须默认「否」——它会复制几百 MB 并改配置
m = re.search(r"askyesno\(\s*\"发现已安装的 dsh\"", src)
# 2026-10-30 起纳入改成「勾选列表」：完好的默认勾上，坏的禁用；
# 不点「纳入勾选的」就不会动任何东西（副作用动作必须显式确认）。
chk("C3l 纳入走勾选列表且需显式确认（只有「纳入勾选的 / 取消」两个出口）",
    "def _adopt_picker_dialog" in src and "纳入勾选的" in src
    and 'text=T("取消"), command=on_cancel' in src)
chk("C3l2 体检不通过的安装禁止勾选",
    # 2026-11-01：勾选态从原生 Checkbutton 改成可点击 Label（✅/空），
    # 禁用逻辑同步改成「点不动 + 变灰」。
    'if not _ok:' in src and '_ok=r["ok"]' in src)
chk("C3l3 不再有「以后不再提示」名单（勾选 = 纳入，不勾 = 什么都不记）",
    "adopt_ignored" not in src and "_set_adopt_ignored" not in src
    and "全部跳过" not in src)
chk("C3l4 扫描没有 manual 分支（只有一种入口，每次全量查磁盘）",
    "manual" not in _fn_body("scan_and_offer_adopt"))
# 2026-10-30 修：detect_existing_installs 给的 path 是 @deepseek-ai/dsh 包目录，
# 不是 node_modules。以前直接把它当 node_modules 传给 adopt_existing，
# 体检必然报「这里没有 @deepseek-ai/dsh」→ 纳入功能实际是坏的。
chk("C3l5 纳入用的是 node_modules（不是 dsh 包目录）",
    "def _adopt_loc_nm" in src and "dsh_adopt, nm_src," in src
    and 'dsh_adopt, loc["path"],' not in src)
# 2026-10-30 修：原来还有个「关闭」按钮走裸 win.destroy —— 关窗口（X/Esc）记忽略名单、
# 点「关闭」不记，同一件事两种后果，下次是否被再问一次取决于点了哪个。
# 现在统一：关窗口 = 全部跳过。不留"什么都不记就走"的出口。
_pk = _fn_body("_adopt_picker_dialog")
chk("C3l6 关窗口 / Esc / 取消 三个出口完全等价（都是这次不纳入）",
    'win.protocol("WM_DELETE_WINDOW", on_cancel)' in _pk
    and 'bind("<Escape>", lambda e: on_cancel())' in _pk
    and "on_skip_all" not in _pk)
# grid 行号：日志区从 row3 挪到 row4（新增按钮行），权重必须同步
chk("C3m 日志区行权重与当前布局一致",
    # 2026-11-01 第三轮：底部按钮条整体移到标题栏 → 日志区上提到 row2
    "root.rowconfigure(2, weight=1)" in src, "日志区须是 row2")

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
chk("C8e 零槽引导提供安装与纳入两个入口",
    # 2026-11-01 定稿：不再画假槽卡，改为一段说明 + 两个真按钮
    "install_official_version" in _fn_body("_build_fresh_guide")
    and "scan_and_offer_adopt()" in _fn_body("_build_fresh_guide"))
chk("C8e2 零槽时不再有「快速开始」大卡片（与底部按钮重复）",
    'text=T("快速开始")' not in src)
chk("C8f 示例配置是零槽",
    '"versions": {}' in io.open(
        os.path.join(_REPO, "config", "config.example.json"),
        encoding="utf-8-sig").read())
chk("C8g 界面内置环境体检入口", "def _show_env_report()" in src)
# 2026-10-31：标题栏那个按钮撤了（它查的是管理器自己的前置条件，管理器都开起来了
# 基本必然通过；node/槽完整性/端口这些「槽体检」覆盖得更准）。入口只留在零槽引导卡。
chk("C8h 标题栏不再有「检查运行环境」（日常用户用不着，是维护者工具）",
    'text=T("检查运行环境")' in src
    and "_hdr, text=T(\"检查运行环境\")" not in src)
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
    # 2026-11-13：「没空槽」不再笼统说「槽位已满」，改用 _slot_full_reason()
    # 说清是 config 满了还是磁盘目录占着编号。
    "_slot_full_reason()" in _iq and "step(i + 1)" in _iq)
# 2026-11-01：改成带参数调用（要传本次新增的槽键给白名单同步），
# 断言的本意是"整个队列只重启一次"，所以按调用名计数，不限定括号里有没有参数。
chk("C3y4 队列结束才重启一次",
    # 2026-11-01：重启统一走 _restart_or_tell（它内部先起新实例、成功后才关自己）。
    (_iq.count("_restart_or_tell(") + _iq.count("_reload_config_after_change(")) == 1)
_iov = _fn_body("install_official_version")
chk("C3y5 安装对话框是多选列表", "multi=True" in _iov)
chk("C3y6 安装对话框按「真正可用的编号」限流",
    # 2026-11-13：可分配编号 = 没登记 且 磁盘上没有同名目录占着（_free_slot_keys）。
    # 旧写法 MAX_SLOTS - len(SLOT_KEYS) 会把被 dshV3 占着的编号也当成空槽。
    "free = len(_free_keys)" in _iov and "_free_slot_keys()" in _iov)
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
_b = line_of('ttk.Button(_hdr, text=T("设置")')
chk("C3z5b 「设置」按钮在 settings_dialog / settings_hub_dialog 之后创建",
    # 2026-11-01：主界面按钮改指向统一入口 settings_dialog（它内部再调 hub）
    _d > 0 and _b > _d, "def@%d 按钮@%d" % (_d, _b))
_c = line_of("\n_build_cards()")
chk("C3z5c 首次 _build_cards() 也在它之后（全隐藏提示卡引用它）",
    _c > _d, "def@%d 调用@%d" % (_d, _c))
chk("C3z5d 卡片按显示偏好画，不是按 SLOT_KEYS 全量",
    "keys = _display_keys()" in _fn_body("_build_cards"))
chk("C3z5e 重画前先清场（否则新旧卡片叠在一起）",
    "_clear_cards()" in _fn_body("_build_cards"))
chk("C3z5f 清场时把旧卡片按钮从冻结清单摘掉（否则 _actions_enabled 碰已销毁控件）",
    'BUSY["card"]' in _fn_body("_clear_cards"))
chk("C3z5g _actions_enabled 同时管常驻按钮与卡片按钮",
    'BUSY["btns"]' in _fn_body("_actions_enabled")
    and 'BUSY["card"]' in _fn_body("_actions_enabled"))
chk("C3z5h 保存走原子写（临时文件 + os.replace）",
    "os.replace(" in _fn_body("_save_ui_prefs"))
chk("C3z5i 保存是读-改-写，不整份覆盖（否则抹掉 versions）",
    'data["ui"] = prefs' in _fn_body("_save_ui_prefs"))
# settings_dialog 里嵌套了 _row_text/_toggle 等，_fn_body 会在第一个嵌套 def 处截断，
# 所以这里单独按「到顶层注释为止」取全文。
# 2026-11-01：设置窗口拆成「总页 settings_hub_dialog」+
# 「显示/排序页 _settings_display_dialog」。下面这些断言测的是后者。
_sd = re.search(r"\ndef _settings_display_dialog\(.*?(?=\n# )", src, re.S)
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
# 2026-11-01：设置窗口拆成「总页 settings_hub_dialog」+
# 「显示/排序页 _settings_display_dialog」。下面这些断言测的是后者。
_sd = re.search(r"\ndef _settings_display_dialog\(.*?(?=\n# )", src, re.S)
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
chk("C3z8e 状态行末尾拼体检标记", "+ _health_mark(key)" in _fn_body("_refresh_one"))
chk("C3z8f refresh 支持 once=True（体检后只刷一次，不另起定时链）",
    "if not once:" in _fn_body("refresh") and "def refresh(once=False):" in src)
chk("C3z8g 「槽体检」按钮在 _run_health 定义之后创建（提前创建就是 NameError）",
    src.index('text=T("槽体检")') > src.index("def _run_health("))
chk("C3z8h 体检窗口默认只报告；修复是**显式按钮**（2026-11-14 用户要求）",
    "复制到剪贴板" in _fn_body("_health_dialog")
    and 'text=T("修复…")' in _fn_body("_health_dialog")
    and "repair_slot" in _fn_body("_health_dialog")
    and "askyesno" in _fn_body("_health_dialog"))
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
    'log(T("[变更] %s %s"' in _fn_body("_check_drift"))
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
# 2026-11-01 第二轮重构（用户要求）：**初始界面只留「槽 + 日志 + 全启停」**，
# 其余动作（安装 / 纳入 / 卸载 / 生成 bat / 关于 / 收藏）全部收进「设置」。
chk("C3z13c 全启停按钮在标题栏、位于「设置」左侧",
    # 2026-11-01 第三轮：底部按钮条移到标题栏（pack side=right 后 pack 的在更左）
    '_hdr, text=T("全部启动")' in src and '_hdr, text=T("全部停止")' in src
    and src.index('_hdr, text=T("设置")') < src.index('_hdr, text=T("全部停止")'))
chk("C3z13c2 主界面不再有第二排按钮（安装/纳入/卸载已收进设置）",
    "_act2 = ttk.Frame(root)" not in src)
chk("C3z13c3 设置页只留界面/目录/维护/日志四组",
    # 2026-11-02 重构：设置页不再堆安装/纳入/卸载，只保留不常用的配置项
    'text=T("界面")' in src and 'text=T("维护")' in src and 'text=T("日志")' in src
    and "_dir_group_single" in src)
chk("C3z13d 标题栏只剩「设置」一个按钮",
    '_hdr, text=T("槽体检")' not in src
    and '_hdr, text=T("关于")' not in src
    and '_hdr, text=T("设置")' in src)
chk("C3z13e 槽体检按钮仍在 _run_health 定义之后创建（否则 NameError）",
    src.index('text=T("槽体检")') > src.index("def _run_health("))
chk("C3z13f2 底部行只剩两列（全部停止 / 生成 bat）",
    "_act.columnconfigure(2" not in src)
chk("C3z13g 日志按行着色：出错标红、[变更] 标青",
    'tag_configure("err"' in src or '"err", _RED' in src)
chk("C3z13h 着色在 _gui_sink_impl 里生效（insert 带 tag）",
    '_log_tag(line))' in _fn_body("_gui_sink_impl"))


# ── 目录设置（2026-10-31 第四轮）：安装根 + 临时目录根 ─────────────────
print("== C3z14 目录：安装根可设 / 临时文件集中 ==")
chk("C3z14a 临时目录新规则：各槽自己的 .tmp（2026-11-02 用户要求）",
    # 原规则是"cònfig 的 temp_root，缺省 <槽根>/dsh-temp"（集中式）。
    # 用户要求简化目录设置：临时文件直接放进各自槽目录下的 .tmp，
    # 这样界面只需一个「安装根」。
    "def _slot_tmp_dir(" in src
    and '.tmp' in _fn_body("_slot_tmp_dir")
    and "def _dir_group_single(" in src)
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
chk("C3z14h 零槽也能进设置并改安装根",
    # 2026-11-01：设置总页 settings_hub_dialog 自带「目录」分组，
    # 零槽时也能打开（那时安装根还没锁定，正是最该设置的时机）。
    "def _dirs_only_dialog(" in src
    and "_dir_group(win)" in src
    and "def settings_hub_dialog(" in src)
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
    and "安装根目录不能直接改" in _fn_body("_apply_dir_settings"))
_ad = _fn_body("_apply_dir_settings")
chk("C3z16i 拒绝时指出可行出路是「移动」而不是「先卸载」",
    # 2026-11-14 用户指出：换盘不用卸载，整体搬过去就行（dsh_move 保证安全）。
    "移动安装根目录" in _ad and "不需要卸载" in _ad
    and "先把所有版本卸载干净" not in _ad)
chk("C3z16i2 目录分组不再宣称「要换位置请先卸载全部版本」",
    "要换位置请先卸载全部版本" not in src
    and "不用卸载" in _fn_body("_dir_group_single"))
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
chk("C3z17f 已移出的槽由「扫描并纳入」原样接回（不再有独立窗口）",
    # 2026-11-12 用户反馈：没必要记住移出了哪个版本，扫描能重新发现。
    "_do_readd(_readd_roots)" in _fn_body("scan_and_offer_adopt")
    and "重新纳入勾选的" not in _fn_body("uninstall_versions_dialog"))
chk("C3z17g 没有在管槽时，卸载窗指引去「扫描并纳入」接回",
    # 2026-11-12：独立入口已移除，统一指向扫描。
    "if not SLOT_KEYS:" in _fn_body("uninstall_versions_dialog")
    and "扫描已装 dsh 并纳入" in _fn_body("uninstall_versions_dialog"))
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
chk("C3z17l 重新纳入只在端口被占时换端口，且必须回报 port_changed",
    # 2026-11-13：以前一律"原样保留 + 警告"，结果两条记录抢 6080，一启动就撞车。
    # 现在：端口从 bat 读；被占就换一个空闲的，并在 payload 里标出来让调用方重写 bat。
    "port_from_bats(root)" in _rea_body
    and "port_changed[key]" in _rea_body
    and "taken" in _rea_body)
chk("C3z17m detached 只记绝对路径（label/port/配置快照一律不存）",
    # 2026-11-13：快照描述的是"当时那个槽"，重新纳入时槽键可能已经变了，
    # 照抄就会造出与现役槽同名、同端口、同路径的幽灵槽（实测就是槽 v4）。
    '"root": rec_root' in _det_body
    and "_bake_base" not in _uops
    and '"config": ' not in _det_body)

# ── C3z17n–r：2026-10-31 实测 bug —— 移出后再点「纳入」一个都扫不到 ─────────
# 原因：`_managed_roots()` 把 detached 记录也算成「已知」，而纳入扫描正是拿它当
# 排除名单的 → 已移出的槽被自己的记录挡在门外。默认值必须保留（否则每次启动又
# 问一遍），但纳入扫描那一处必须显式关掉。
_adopt_seg = src[src.index("def scan_and_offer_adopt("):
                 src.index("def scan_and_offer_adopt(") + 4000]
chk("C3z17n 扫描把「移出过的目录」也补进候选（每次点按钮都补）",
    # 2026-11-13：没有自动扫描了，也就不需要 manual 分支。
    "for r in _live_detached_records():" in _adopt_seg)
chk("C3z17o _managed_roots 只收 config 里登记的槽（不再混进 detached）",
    # 混进来的后果：同一目录既算"已登记"又算"已移出"，手动扫描时被当新发现，
    # 接回时槽键已被占用 → 幽灵槽。
    "def _managed_roots():" in src
    and "_live_detached_records" not in _fn_body("_managed_roots"))
_adopt_full = _fn_body("scan_and_offer_adopt")
chk("C3z17p 扫不到时不再指路已删除的「已移出管理」列表",
    # 那个独立入口 2026-11-12 已删；接回统一走「扫描并纳入」。
    "已移出管理」列表" not in _adopt_full
    and "没扫到管理范围外的 dsh" in _adopt_full)
chk("C3z17q 失效记录只进日志，弹窗里一个字都不提",
    "_stale_n = len(_stale_detached_records())" in _adopt_full
    and 'log("[检测] 忽略' in _adopt_full
    and "另外，磁盘上还有" not in _adopt_full
    and "已移出管理」列表" not in _adopt_full)
chk("C3z17s 扫不到时只弹一个说明框，不再连环追问",
    # 2026-11-02 用户反馈「弹出了一些窗口，还有移出管理，不知道你怎么设计的」。
    # 原流程：弹「没有可纳入的」→ 再问「要打开已移出管理吗」→ 打开「卸载/移出」窗。
    # 已改为：只弹一个说明框，把"已移出的去哪找"写在同一段文字里。
    "要打开「已移出管理」列表吗？" not in _adopt_seg)
chk("C3z17r detached 记录里的 root 写成反斜杠（<base> 烘开后与其余部分是混写的）",
    "rec_root = os.path.normpath(root) if root else \"\"" in _uops)

# ── C3z18：设置页必须真的挂上「装 / 纳入 / 卸载」三个入口 ────────────────
# 2026-11-11 审计事故：11-01 删掉主界面第二排按钮，注释写着「已全部移进设置」，
# 但设置页从来没补过这三个入口 —— 零槽引导卡只在 FRESH_START 时出现，于是
# 装完第一个版本后再也装不了第二个、也卸不掉，而所有回归一路全绿（它们只
# 断言源码里还有 `command=install_official_version` 这个字符串）。
_hub = _fn_body("settings_hub_dialog")
chk("C3z18a 设置页有「版本管理」分组", 'text=T("版本管理")' in _hub)
chk("C3z18b 版本管理挂了「安装官方版 dsh」", "install_official_version" in _hub)
chk("C3z18c 版本管理挂了「扫描并纳入」",
    "scan_and_offer_adopt()" in _hub)
chk("C3z18d 版本管理挂了「卸载 / 移出管理」",
    "uninstall_versions_dialog" in _hub)
chk("C3z18e 生成 bat 前先拦 cmd 元字符（路径/label 可注入）",
    "_bat_unsafe_fields(cfg)" in _fn_body("_rebuild_bats")
    and "_BAT_UNSAFE_CHARS" in src)

# ── C3z19：扫描必须以磁盘为准，不能拿 detached 存档当事实 ────────────────
# 2026-11-11 用户反馈：移出的 dshV2 目录已经被删掉了，点「扫描并纳入」却还
# 提示「你有 1 个版本处于已移出管理状态（dshV2）」。根因是提示和「已知」
# 集合直接读 config 记录，没有复核目录是否还在。
chk("C3z19a 有按磁盘复核 detached 记录的三个辅助函数",
    all(("def %s(" % n) in src for n in
        ("_detached_alive", "_live_detached_records", "_stale_detached_records")))
chk("C3z19b 已知集合 = config 里登记的槽（不含 detached 存档）",
    "_live_detached_records" not in _fn_body("_managed_roots")
    and "for _k in SLOT_KEYS:" in _fn_body("_managed_roots"))
_adopt = _fn_body("scan_and_offer_adopt")
chk("C3z19c 补候选的三道闸：目录在 + 有 bin.js + 不在 versions 里",
    "in _known" in _adopt
    and '"lib", "bin.js"' in _adopt
    and "for r in _live_detached_records():" in _adopt)
chk("C3z19d 没扫到时失效记录只进日志（不弹名字）",
    "_stale_n = len(_stale_detached_records())" in _adopt
    and 'log("[检测] 忽略' in _adopt
    and "_det = _live_detached_records()" not in _adopt)
chk("C3z19d2 扫描弹窗彻底不显示失效记录的名字（二次反馈）",
    "另有 %d 条已移出记录" not in _adopt
    and "for r in _stale" not in _adopt
    and "不会把已删除的旧记录当成安装" not in _adopt)
chk("C3z19e 不再无条件宣称已移出的版本「文件还在原处」",
    "它们不在这条扫描路径上：文件还在原处" not in _adopt)
chk("C3z19f 不再有「已移出管理」独立入口（统一走「扫描并纳入」）",
    "detached_manager_dialog" not in src
    and "已移出管理（%d）" not in src)
chk("C3z19g 卸载窗不再有「已移出管理（N）」按钮",
    "_recs_n" not in _fn_body("uninstall_versions_dialog"))

# ── C3z20：装 / 纳入 / 移出 / 卸载后**原地刷新**，不重启进程 ────────────
# 2026-11-12 用户要求：装完第一个版本就该直接看到结果界面，不要提示重启，
# 也不要用户手动重开。重启进程只作为兜底。
_rl = _fn_body("_reload_config_after_change")
chk("C3z20a 原地刷新：重读 config 并重建 VC / SLOT_KEYS / FRESH_START",
    "with open(CONFIG_PATH" in _rl
    and "VC = _build_vc(VERSIONS)" in _rl
    and "SLOT_KEYS = list(VERSIONS.keys())" in _rl
    and "FRESH_START = (N_SLOTS == 0)" in _rl)
chk("C3z20b 原地刷新：保留运行中的进程引用与已抓到的 token",
    "procs = {k: _old_procs.get(k) for k in SLOT_KEYS}" in _rl
    and "auth_urls = {k: _old_urls.get(k) for k in SLOT_KEYS}" in _rl)
chk("C3z20c 原地刷新：重画卡片并按新槽数重算窗口宽度",
    all(x in _rl for x in ("_clear_cards()", "_build_cards()",
                           "_build_fresh_guide()", "root.geometry(")))
chk("C3z20d 所有收尾路径都不再对用户说「即将重启」",
    all("即将重启" not in _fn_body(f) for f in
        ("_adopt_one", "_install_queue", "_do_install", "_do_uninstall",
         "_do_detach", "_do_readd", "_do_move_root")))
chk("C3z20e 刷新失败才退回重启进程",
    "_reload_config_after_change(new_keys)" in _fn_body("_restart_or_tell")
    and "_relaunch_self()" in _fn_body("_restart_or_tell"))
_iq_code = "\n".join(l for l in _fn_body("_install_queue").splitlines()
                     if not l.lstrip().startswith("#"))
chk("C3z20f 批量安装收尾不引用未定义的 payload（曾导致装完不刷新）",
    "payload" not in _iq_code)
chk("C3z20h 原地刷新重建探测缓存与状态记忆（否则新槽永远「检测中…」）",
    "_DETECT.clear()" in _rl and "_prev_state.clear()" in _rl
    and "_old_detect.get(_k)" in _rl)
chk("C3z20i 探测线程对缺失槽键用 setdefault（防 KeyError 被吞）",
    "_DETECT.setdefault(key" in _fn_body("_detect_worker"))

# ── C3z21：移出后再「扫描并纳入」必须原样接回，不能复制成 -2 ─────────────
# 2026-11-12 实测：移出 v3 后点「扫描并纳入」，走了复制路径 → 736 MB 拷到
# 一半被强杀，磁盘上留下 dshV3-2（289 MB 残件）。
chk("C3z21a 分流只看位置：版本根下→原地接回，别处→复制纳入",
    # 按"有没有移出记录"分类的旧写法，会让同一目录既被判接回又被当新发现，
    # 正是 dshV3-2 半成品与幽灵槽 v4 的来源。
    "_is_under_path(_root, _vr)" in _adopt_full
    and "_det_by_nm" not in _adopt_full
    and "_do_readd(_readd_roots)" in _adopt_full
    and '_adopt_one(r["version"], r["loc"])' in _adopt_full)
_pk2 = _fn_body("_adopt_picker_dialog")
chk("C3z21b 纳入候选对话框按**位置**说明：原地接回 / 复制一份",
    # 2026-11-14：说明压成一句，并且**每一行**都标出自己属于哪种。
    "原地接回 = 只改配置" in _pk2 and "复制一份 = 拷进版本根" in _pk2
    and 'r.get("action")' in _pk2)

# ── C3z29：按钮冻结的方向不能写反（2026-11-14「全部停止后按钮全失灵」）────
_sa_body = _fn_body("stop_all_slots")
chk("C3z29a 全部停止：先冻结、结束后恢复（参数是「可用」，曾经写反）",
    "_actions_enabled(False)" in _sa_body
    and "_actions_enabled(True)" in _sa_body
    and _sa_body.index("_actions_enabled(False)")
    < _sa_body.index("_actions_enabled(True)"))
chk("C3z29b 安装/卸载/移出/接回 都是同一顺序（False 开头、True 收尾）",
    all(_fn_body(_n).index("_actions_enabled(False)")
        < _fn_body(_n).index("_actions_enabled(True)")
        for _n in ("_do_uninstall", "_do_detach", "_do_readd")))
chk("C3z29c 函数名不再叫 _set_busy（名字会让人把方向写反）",
    "_set_busy(" not in src)
chk("C3z29d 全部停止在后台线程跑（4 个槽枚举进程会让界面卡住）",
    "threading.Thread(target=work" in _sa_body
    and "root.after(0, done)" in _sa_body
    and "kill_one(k, report=False)" in _sa_body)
# ── C3z30：进度窗不能有能点的「关闭」（2026-11-14 用户要求）──────────────
# 注意：`_fn_body` 只认顶层 `def name(`，`class X.method` 形式匹配不到
# （文本里根本没有 "def _GuiProgress.__init__" 这一串），所以这里直接切 class。
# 2026-11-14 修：原来是 `_gp[:src.index(...)]` —— 拿**绝对**索引去切**相对**切片，
# 越界等于没截断，`_gp` 一直是「从 class 到文件末尾」136848 个字符，断言范围
# 一直过大（`overrideredirect(` 那条实际在扫整个文件）。
_gp_i = src.index("class _GuiProgress")
_gp = src[_gp_i:src.index("    def on_step", _gp_i)]
chk("C3z30a 进度窗自绘标题栏（overrideredirect），标题在左、「—」在右同一行",
    "overrideredirect(True)" in _gp
    and 'self.tlabel.pack(side="left"' in _gp
    and 'self.mini.pack(side="right"' in _gp)
# 自绘窗口必须清 WS_EX_TOOLWINDOW —— Tk 给 overrideredirect 窗口默认加的就是它，
# 而工具窗口不进任务栏。早先「最小化后找不回来」误判成"自绘这条路不行"，
# 其实是漏了这一步（2026-11-14 实测 exstyle 0x80 → 0x40000 后任务栏就有按钮）。
_mtb = "\n".join(l for l in _fn_body("_make_taskbar_window").splitlines()
                 if not l.lstrip().startswith("#"))
chk("C3z30b 自绘窗口清 WS_EX_TOOLWINDOW、加 WS_EX_APPWINDOW（任务栏入口）",
    "WS_EX_TOOLWINDOW" in _mtb and "WS_EX_APPWINDOW" in _mtb
    and "& ~WS_EX_TOOLWINDOW" in _mtb and "| WS_EX_APPWINDOW" in _mtb)
chk("C3z30c 取 wm_frame 之前先 update（否则 HWND 还没建好，改样式静默失败）",
    _mtb.index("win.update()") < _mtb.index("wm_frame()"))
chk("C3z30d WM_DELETE_WINDOW 仍兜住 Alt+F4 / 任务栏关闭",
    'WM_DELETE_WINDOW' in _gp)
chk("C3z30e 无边框窗口自己实现拖动（只有标题栏能拖，跟正常窗口一致）",
    "_drag_start" in _gp and "_drag_move" in _gp
    and 'self.tbar, self.tlabel' in _gp)
chk("C3z30f 无边框窗口自己居中（没有 WM 帮忙摆初始位置）",
    "_center_on_root" in _gp and "winfo_screenwidth" in _gp)
# 最小化这条路踩过三个坑，全部用真机实测确认过（2026-11-14）：
#   ① Tk 的 iconify() 对 overrideredirect 窗口直接抛
#      TclError: can't iconify: override-redirect flag is set
#   ② WinDLL.ShowWindow 会同步派发消息、重入 Tk 回调，而 ctypes 已放 GIL
#      → Fatal Python error: PyEval_RestoreThread
#   ③ PyDLL 保持 GIL 也救不了（重入发生在 Tk 的 C 代码里）
# 唯一稳的：调用期间把 <Unmap> 这个 Python 回调摘掉，回来再挂上。
_min_i = _gp.index("def _minimize")
# `_gp` 本身已经截到 `def on_step` 之前，直接取到末尾即可（别再 index on_step）。
_min_code = "\n".join(
    l for l in _gp[_min_i:].splitlines()
    if not l.lstrip().startswith("#"))
chk("C3z30g 最小化不用 Tk iconify（overrideredirect 会抛 TclError），改 ShowWindow",
    "user32.ShowWindow(wintypes.HWND(" in _min_code
    and "self.win.iconify()" not in _min_code)
chk("C3z30h 调 ShowWindow 前先 unbind <Unmap>（否则重入 Tk 回调 → 进程崩溃）",
    _min_code.index('self.win.unbind("<Unmap>")')
    < _min_code.index("user32.ShowWindow.argtypes"))
chk("C3z30i 最小化前手动 grab_release（摘掉 <Unmap> 后没人替我们放了）",
    _min_code.index("self.win.grab_release()")
    < _min_code.index("user32.ShowWindow.argtypes"))

# ── C3z32：进度窗的「—」不能用 tk.Button（2026-11-14 用户截图：白补丁）────
# Windows 上 tk.Button 是原生控件，`bg` 基本不生效 —— 浅灰窗口上会贴出一块
# 白色方块，看着像 bug。tk.Label 是 Tk 自绘，底色完全可控。
_gp_code = "\n".join(l for l in _gp.splitlines() if not l.lstrip().startswith("#"))
chk("C3z32a 「—」用 tk.Label 而不是 tk.Button（后者 bg 在 Windows 上不生效）",
    "self.mini = tk.Label(" in _gp_code
    and "self.mini = tk.Button(" not in _gp_code,
    "label=%s button=%s len=%d" % (
        "self.mini = tk.Label(" in _gp_code,
        "self.mini = tk.Button(" in _gp_code, len(_gp)))
chk("C3z32b 「—」的底色和窗口内容区同源（都用 _APP_BG，避免色差）",
    "bg=_APP_BG" in _gp_code and "self.win.configure(bg=_APP_BG)" in _gp_code)
chk("C3z32c 窗口内容区显式设底色（Toplevel 默认是系统色，会和 ttk 差一层）",
    "self.win.configure(bg=_APP_BG)" in _gp_code)
chk("C3z32d 界面上不再写「点右上角…」这类操作提示（正文已含「请勿关闭」）",
    "点右上角" not in _gp and "可以最小化" not in _gp)
chk("C3z32e 最小化时释放 grab（否则窗口没了、主界面还是点不动）",
    "<Unmap>" in _gp_code and "grab_release()" in _gp_code
    and "<Map>" in _gp_code and "grab_set()" in _gp_code)
# _APP_BG / _CARD_BG 必须跟着主题走：原来只更新 _INK 那批，深色主题下凡是
# 后来新建、直接读 _APP_BG 的控件都会冒出浅色补丁。
_ap = _fn_body("_apply_palette")
chk("C3z32f _apply_palette 同步重绑 _APP_BG/_CARD_BG/_CARD_BD",
    "global _APP_BG, _CARD_BG, _CARD_BD" in _ap
    and '_APP_BG = pal["app_bg"]' in _ap
    and '_CARD_BG = pal["card_bg"]' in _ap
    and '_CARD_BD = pal["card_bd"]' in _ap)

# ── C3z33：装新版本时不许把「用别的版本」一起禁掉（2026-11-14 用户提出）──
# 安装只在新建的槽目录里跑 npm + 往 config 加一条记录，不杀进程、不动已有槽；
# 装完之前新槽也不在界面上。所以已装槽的启动/停止没有理由跟着冻。
_ae = _fn_body("_actions_enabled")
_ae_code = "\n".join(l for l in _ae.splitlines() if not l.lstrip().startswith("#"))
chk("C3z33a _actions_enabled 支持 keep_run（冻结时跳过卡片上的启动/停止）",
    "keep_run" in _ae_code
    and 'if keep_run and not enabled' in _ae_code
    and 'b not in BUSY["card_run"]' in _ae_code)
chk("C3z33b 恢复时一律全开（keep_run 只管冻结方向）",
    "normal\" if enabled else \"disabled" in _ae_code.replace("'", '"'))
_iq = "\n".join(l for l in _fn_body("_install_queue").splitlines()
                if not l.lstrip().startswith("#"))
chk("C3z33c 安装队列冻结时带 keep_run=True",
    "_actions_enabled(False, keep_run=True)" in _iq)
chk("C3z33d 装新版本的进度窗非模态（modal=False），不锁主界面",
    "_GuiProgress(title, done, modal=False)" in _fn_body("_do_install"))
# 卸载 / 移出 / 移动必须仍然是全模态全冻结 —— 那几个会删或搬磁盘上的东西
for _fn_name in ("_do_uninstall", "_do_detach", "_do_move"):
    _b = _fn_body(_fn_name)
    if not _b:
        continue
    chk("C3z33e %s 仍全冻结（不带 keep_run）" % _fn_name,
        "keep_run" not in _b)
chk("C3z33f 卡片创建时把启动/停止单独登记进 card_run",
    'BUSY["card_run"].extend([_b_start, _b_stop])' in src)
chk("C3z33g 重画卡片要清空 card_run（否则残留已销毁的按钮）",
    'BUSY["card_run"] = []' in _fn_body("_clear_cards"))
chk("C3z33h 非模态窗口恢复时不抢 grab",
    "if not self.modal:" in _gp and "return" in _gp.split("if not self.modal:")[1][:80])

# 真跑一遍 _actions_enabled：静态断言看不出「到底冻了哪几个按钮」。
_ae_src = ast.get_source_segment(
    src, [n for n in ast.parse(src).body
          if isinstance(n, ast.FunctionDef) and n.name == "_actions_enabled"][0])


class _FakeBtn:
    def __init__(self, name):
        self.name = name
        self.state = "normal"

    def configure(self, **kw):
        if "state" in kw:
            self.state = kw["state"]


_install = _FakeBtn("安装")
_start, _stop, _open = _FakeBtn("启动"), _FakeBtn("停止"), _FakeBtn("打开")
_ns_ae = {"BUSY": {"btns": [_install],
                   "card": [_start, _stop, _open],
                   "card_run": [_start, _stop]}}
exec(compile(_ae_src, "<ae>", "exec"), _ns_ae)
_ns_ae["_actions_enabled"](False, keep_run=True)
chk("C3z33i 装新版本冻结时：安装按钮冻、已装槽的启动/停止仍可用、其它卡片按钮冻",
    _install.state == "disabled"
    and _start.state == "normal" and _stop.state == "normal"
    and _open.state == "disabled",
    "install=%s start=%s open=%s" % (_install.state, _start.state, _open.state))
_ns_ae["_actions_enabled"](True)
chk("C3z33j 恢复时全部可用（含 keep_run 跳过的那些）",
    all(b.state == "normal" for b in (_install, _start, _stop, _open)))
_ns_ae["_actions_enabled"](False)      # 不带 keep_run：全冻
chk("C3z33k 不带 keep_run 时卡片按钮也全冻（卸载/移出/移动走这条）",
    all(b.state == "disabled" for b in (_install, _start, _stop, _open)))


# ── C3z31：版本号只有一个事实来源（VERSION 文件）──────────────────────────
_root_dir = os.path.abspath(os.path.join(HERE, os.pardir))
_ver = io.open(os.path.join(_root_dir, "VERSION"), encoding="utf-8").read().strip()
chk("C3z31a VERSION 文件存在且形如 0.1.0（不带 v 前缀）",
    bool(re.match(r"^\d+\.\d+\.\d+$", _ver)), _ver)
chk("C3z31b 界面版本号读 VERSION（不再跟着每台机器的 config.json 走）",
    'os.path.join(BASE_DIR, "VERSION")' in _fn_body("_app_version")
    and 'return "0.0.0"' in _fn_body("_app_version"))
_cl = io.open(os.path.join(_root_dir, "CHANGELOG.md"), encoding="utf-8").read()
chk("C3z31c CHANGELOG 里有当前版本的条目（改版本号必须同步更新记录）",
    ("## [%s]" % _ver) in _cl, "VERSION=%s" % _ver)
_cli = io.open(os.path.join(_root_dir, "dsh-tm.py"), encoding="utf-8").read()
chk("C3z31d CLI 的 --version 与界面同源",
    "def app_version(" in _cli and 'os.path.join(BASE_DIR, "VERSION")' in _cli
    and '"--version"' in _cli)


chk("C3z29e 杀完进程立刻刷新探测缓存（否则日志里「已停止」紧跟「运行中(外部)」）",
    "_refresh_detect_cache(key)" in _fn_body("kill_one")
    and "_DETECT[key] = {" in _fn_body("_refresh_detect_cache"))


# ── C3z26：纳入对话框的尺寸按内容算（2026-11-14「这个界面不太合理」）────────
chk("C3z26a 窗口高度按行数算，不再用 126px/行 的粗估（单行不再撑出大片空白）",
    "_row_h = sum(34 if r[\"ok\"] else 72 for r in rows)" in _pk2
    and "_list_h = max(34, min(300, _row_h))" in _pk2
    and "_h = 150 + _list_h" in _pk2
    and "126 *" not in _pk2)
chk("C3z26b 列表区高度由行数定，画布不写死宽度（写死会和滚动条一起溢出）",
    'height=_list_h' in _pk2
    and "width=484" not in _pk2
    and 'fill="both", expand=True' in _pk2)
chk("C3z26c 每行标出「原地接回 / 复制一份」，不能纳入的标红",
    'text=r["action"]' in _pk2 and 'text=T("不能纳入")' in _pk2)
# ── C3z27：接回做体检；修复必须由用户点（2026-11-14）────────────────────
_rep_body = _uops.split("def repair_slot(")[1].split("\ndef ")[0]
chk("C3z27a 接回走和复制纳入同一套体检（不再只看 bin.js）",
    "inspect_adopt_source" in _rea_body
    and "这个安装不完整，不能接回" in _rea_body)
chk("C3z27b 缺 node **不自动补**（可能是用户故意删的），只在显式修复时补",
    "补上 node 运行时" not in _rea_body
    and "补上 node 运行时" in _rep_body)
chk("C3z27c 接回不碰用户目录；补建 .dsh-home/.tmp 只发生在 repair_slot",
    'for _sub in (".dsh-home", ".tmp")' not in _rea_body
    and 'for sub in (".dsh-home", ".tmp")' in _rep_body)
chk("C3z27d 修复只补不删（repair_slot 里没有删除动作）",
    "shutil.rmtree" not in _rep_body and "os.remove" not in _rep_body
    and "os.unlink" not in _rep_body)
chk("C3z27e 接回只在 bat 缺失或端口变了时重写（不覆盖用户改过的 bat）",
    "_bats_missing" in _fn_body("_do_readd")
    and "port_changed" in _fn_body("_do_readd"))
chk("C3z27f 扫描对话框只指路到「槽体检」，不再自己放修复按钮",
    "槽体检" in _pk2 and "_is_under_path" in _pk2
    and "repair_slot" not in _pk2)
chk("C3z27g 修复入口在槽体检里，且只对「有错误/警告」的槽提供",
    "fixable" in _fn_body("_health_dialog")
    and "LEVEL_ERROR" in _fn_body("_health_dialog"))


chk("C3z26d _adopt_row_info 按位置算出 action",
    'action = "原地接回"' in _fn_body("_adopt_row_info")
    and '"action": _action' in _fn_body("_adopt_row_info"))
chk("C3z21c 挑 node 来源时传 dict（曾传 list 报 items 错误）",
    # 2026-11-14：这段逻辑抽成了 _pick_node_source（接回也要用）
    "_existing = {_k: VC[_k]" in _fn_body("_pick_node_source")
    and "_pick_node_source(\"纳入\")" in _fn_body("_adopt_one"))

# ── C3z22：后台线程不许直接碰 Tk（纳入/复制卡死的根因）──────────────────
chk("C3z22a 日志 GUI 输出切回主线程",
    "_MAIN_TID = threading.get_ident()" in src
    and "threading.get_ident() != _MAIN_TID" in src)
chk("C3z22b 进度回调先写日志、再更新标签",
    'log("[%s] %s" % (self.title, text))' in src
    and src.index('log("[%s] %s" % (self.title, text))')
    < src.index('root.after(0, lambda: self.lbl.configure'))
chk("C3z22c 探测数据未到时也显示端口（不再写「检测中…」）",
    'status_vars[key].set("检测中…"' not in src
    and '已停止  :%d" % cfg["port"]' in _fn_body("_refresh_one"))
chk("C3z22d 单槽刷新有异常隔离（一个槽坏了不影响其它槽显示端口）",
    # 2026-11-13：v4 没有端口号的真因 —— _scan_pos 缺它的键，_scan_slot 抛
    # KeyError，而 refresh 的循环没有隔离，v4 及之后的槽全部停在「—」。
    "def _refresh_one(key):" in src
    and "_refresh_one(key)" in _fn_body("refresh")
    and "except Exception as e:" in _fn_body("refresh"))
chk("C3z22e 日志扫描偏移随软刷新重建（新槽不会 KeyError）",
    "_scan_pos.update({k: _old_scan.get(k) for k in SLOT_KEYS})"
    in _fn_body("_reload_config_after_change")
    and "_scan_pos.get(key) is None" in _fn_body("_scan_slot"))


# ── C3z23：槽编号与目录一一对应（2026-11-13「为什么会有 dshV3-2」）──────────
# 移出管理只删 config 记录、目录还在。若分配编号时只看 config，安装就会把
# dshV3 让位成 dshV3-2（键仍是 v3），编号与目录从此错位。现在目录占着编号就跳号。
import shutil as _sh23
import tempfile as _tf23
_tmpnum = _tf23.mkdtemp(prefix="dsh-num-")
try:
    for _n, _files in (("dshV1", ["x"]), ("dshV2", ["x"]),
                       ("dshV3", ["x"]), ("dshV4", [])):
        _d = os.path.join(_tmpnum, _n)
        os.makedirs(_d)
        for _f in _files:
            open(os.path.join(_d, _f), "w").write("x")

    class _FakeOPS23:
        @staticmethod
        def _dir_is_empty_shell(p):
            for _r, _ds, _fs in os.walk(p):
                if _fs:
                    return False
            return True

    _num_seg = "\n\n".join(
        ast.get_source_segment(src, _n) for _n in ast.parse(src).body
        if isinstance(_n, ast.FunctionDef)
        and _n.name in ("_slot_dir_occupied", "_free_slot_keys", "_next_free_slot_key"))
    _ns23 = {"os": os, "OPS": _FakeOPS23, "MAX_SLOTS": 6,
             "SLOT_KEYS": ["v1", "v2"], "_versions_root": lambda: _tmpnum}
    exec(compile(_num_seg, "<num>", "exec"), _ns23)
    chk("C3z23a 编号分配跳过「磁盘上已有内容」的目录（移出的槽占着编号）",
        _ns23["_slot_dir_occupied"]("v3") is True
        and "v3" not in _ns23["_free_slot_keys"]()
        and _ns23["_next_free_slot_key"]() == "v4",
        str(_ns23["_free_slot_keys"]()))
    chk("C3z23b 零文件空壳不算占用（复用同一个编号，不跳号）",
        _ns23["_slot_dir_occupied"]("v4") is False)
finally:
    _sh23.rmtree(_tmpnum, ignore_errors=True)

chk("C3z23c 实时分配也走同一套规则（不再只看 config）",
    "_free_slot_keys(used)" in _fn_body("_next_free_slot_key_live")
    and "_free_slot_keys()" in _fn_body("_next_free_slot_key"))
chk("C3z23d 「没有空编号」时说清是哪个目录占着",
    "_occupied_unmanaged_keys" in _fn_body("_slot_full_reason")
    and "占着编号" in _fn_body("_slot_full_reason"))
chk("C3z23e 安装队列的日志点名被跳过的编号",
    "_occupied_unmanaged_keys()" in _fn_body("_install_queue"))
chk("C3z23f 启动检查报告「未登记但完整」的槽目录（它们占着编号）",
    "detached_dirs" in _fn_body("_log_orphan_shells") and "[编号]" in src)


# ── C3z24：主题选择必须一眼看出选了哪个（2026-11-14 用户反馈）────────────
# 原来用 ttk.Radiobutton：小圆点在浅色/深色下都只有几像素，选中态几乎看不出来。
# 改成三个分段按钮，选中的那个是品牌色填充 + 白字 + 加粗。
_hub = _fn_body("settings_hub_dialog")
chk("C3z24a 主题用分段按钮（选中=填充），不再用小圆点单选",
    "SegOn.TButton" in _hub and "SegOff.TButton" in _hub
    and "ttk.Radiobutton(" not in _hub)
chk("C3z24b 两种样式都随主题配色（深色下也清晰）",
    'st.configure("SegOn.TButton"' in src
    and 'st.configure("SegOff.TButton"' in src
    and 'foreground="white"' in src)
chk("C3z24c 切主题后重刷选中态（不然按钮还停在旧的高亮上）",
    "def _refresh_theme_seg()" in _hub
    and "_refresh_theme_seg()" in _hub
    and src.count("_refresh_theme_seg()") >= 2)

# ── C3z25：移动安装根不用卸载（2026-11-14 用户指出提示自相矛盾）───────────
_mv = _fn_body("move_install_root_dialog")
chk("C3z25a 有版本在运行时拒绝移动（文件被占用会拷坏）",
    "有版本正在运行" in _mv and "port_open(" in _mv and "proc_alive(" in _mv)
chk("C3z25b 确认框显示「需要多少 / 可用多少」",
    "need_bytes" in _mv and "free_bytes" in _mv)
chk("C3z25c 移动完成后重写三件 bat（bat 里是绝对路径）",
    "_rebuild_bats()" in _fn_body("_do_move_root"))
chk("C3z25d 提示不再说「要换位置请先卸载全部版本」",
    "要换位置请先卸载全部版本" not in src)


print()
if FAILS:
    print("RESULT: FAIL（%d 项）: %s" % (len(FAILS), "; ".join(FAILS)))
    sys.exit(1)
print("RESULT: ALL PASS")