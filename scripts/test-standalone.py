# -*- coding: utf-8 -*-
"""离线回归：**管理器没了，各 dsh 版本仍能自己跑**。

设计要求（2026-10-31）：**卸掉管理器不能影响任何 dsh 版本** —— 任何时刻，
每个 dsh 版本都能自己单独运行；管理器只负责把这些版本管起来。

这条承诺不能只写在文档里。本脚本用**真的 cmd.exe + 真的 node.exe** 跑一遍：
把 bat 生成时的 `BASE_DIR` 指向一个**不存在的目录**（等价于「管理器已被删掉」），
然后执行生成出来的三件 bat，断言：

  · `dsh.bat`    —— 照样把 node 拉起来跑 bin.js（不依赖管理器的任何文件）
  · `dsh-cli.bat` —— 同样能跑，且参数原样透传
  · `kill-<port>.bat` —— 找不到 dsh_kill.py 时**优雅退出**（rc=0）并给出可操作的提示，
                        而不是报错、更不是去猜着杀进程

安全约束：本组**绝不**真的启动 dsh、也绝不杀任何进程。
  · `bin.js` 是假文件，只打印标记就退出；
  · 端口用本机空闲端口，且 kill bat 走的是「找不到 dsh_kill.py」那条分支（不执行任何 kill）。

跑法：python scripts/test-standalone.py    （退出码 0 = 全过，1 = 有失败）
没有 node 的机器上，实跑那组会 SKIP（不算失败）。
"""
import ast
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, os.pardir))
SRC = os.path.join(REPO, "dsh_manager.pyw")

if not os.path.exists(SRC):
    print("找不到 %s —— 请在管理器仓库内运行本脚本" % SRC)
    sys.exit(1)

FAILS = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ((" | " + extra) if extra else ""))
    if not cond:
        FAILS.append(name)


def skip(name, why=""):
    print("SKIP  " + name + ((" | " + why) if why else ""))


src = open(SRC, encoding="utf-8").read()
tree = ast.parse(src)
keep = [n for n in tree.body
        if isinstance(n, ast.FunctionDef)
        and n.name in ("_bat_text_start", "_bat_text_cli", "_bat_text_kill")]
chk("A1 三个 bat 生成函数都在", len(keep) == 3,
    ",".join(n.name for n in keep))

TMP = tempfile.mkdtemp(prefix="dsh-standalone-")
try:
    # ── 关键一步：BASE_DIR 指向一个**不存在**的目录 ────────────────────
    # 这就是「dsh-tm 已被卸载」的等价物：bat 里凡是引用管理器的地方全部指空。
    GHOST = os.path.join(TMP, "manager-gone")
    chk("A2 前置：模拟的管理器目录确实不存在", not os.path.exists(GHOST), GHOST)

    ns = {"os": os, "re": re, "SLOT_KEYS": ["v1"], "VC": {},
          "BASE_DIR": GHOST, "STANDALONE_PYW": ""}
    exec(compile(ast.Module(body=keep, type_ignores=[]), SRC, "exec"), ns)

    # 找一个能真跑的 node.exe（槽自带的优先 —— 这才是「槽自给自足」的样子）
    node = ""
    vroot = os.path.join(os.path.expanduser("~"), "dsh-versions")
    if os.path.isdir(vroot):
        for name in sorted(os.listdir(vroot)):
            cand = os.path.join(vroot, name, "node", "node.exe")
            if os.path.isfile(cand):
                node = cand
                break
    if not node:
        node = shutil.which("node") or ""
    print("用的 node：%s" % (node or "(没有)"))

    slot_root = os.path.join(TMP, "dshV1")
    home = os.path.join(slot_root, ".dsh-home")
    tempd = os.path.join(TMP, "dsh-temp", "dshV1")
    libdir = os.path.join(slot_root, "node_modules", "@deepseek-ai", "dsh", "lib")
    for d in (home, tempd, libdir):
        os.makedirs(d, exist_ok=True)
    # 假的 dsh 入口：打印标记 + 参数，立刻退出（绝不真的启动 dsh）
    with open(os.path.join(libdir, "bin.js"), "w", encoding="utf-8") as f:
        f.write('console.log("PROBE-NODE-OK " + process.argv.slice(2).join(" "));\n')

    def _free_port():
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
        finally:
            s.close()

    port = _free_port()
    cfg = {
        "label": "dshV1", "port": port,
        "node_exe": node or "node",
        "bin_js": os.path.join(libdir, "bin.js"),
        "dsh_home": home, "temp_dir": tempd,
    }
    bats = {
        "dsh.bat": ns["_bat_text_start"]("v1", cfg, slot_root),
        "kill-%d.bat" % port: ns["_bat_text_kill"]("v1", cfg, slot_root),
        "dsh-cli.bat": ns["_bat_text_cli"]("v1", cfg, slot_root),
    }
    for name, text in bats.items():
        with open(os.path.join(slot_root, name), "w",
                  encoding="gbk", newline="") as f:
            f.write(text)

    # ── B 组：静态检查 —— 不许有「无条件依赖管理器」的写法 ─────────────
    print("\n== B. 静态：bat 不硬依赖管理器 ==")
    start = bats["dsh.bat"]
    kill = bats["kill-%d.bat" % port]
    cli = bats["dsh-cli.bat"]

    chk("B1 dsh.bat 里没有出现 dsh_manager（管理器删了也不影响）",
        "dsh_manager" not in start)
    chk("B2 dsh.bat 的预检是 fail-open（if exist 守卫包住，不是硬调用）",
        'if exist "%PRECHK_PY%" if exist "%PRECHK%" (' in start
        and start.index('if exist "%PRECHK_PY%"')
        < start.index('"%PRECHK_PY%" "%PRECHK%"'))
    chk("B3 预检缺失时**静默**跳过（不弹警告、不留悬空 else）",
        "[警告]" not in start and ") else (" not in start
        and "静默跳过" in start,
        " / ".join(l for l in start.splitlines() if "静默跳过" in l)[:120])
    chk("B4 启动行直接 node + bin.js，不经管理器的任何中间层",
        'web --no-open --port %d' % port in start)
    # 判据用**当前**仓库目录名，而不是某个写死的旧名字 —— 改名之后这条约束
    # 不该跟着失效（否则就成了"只在旧名字下有效"的假绿）。
    _repo_name = os.path.basename(REPO)
    # Some callers put TMPDIR below REPO. The known fixture paths are legal
    # even there. Keep checking any other reference to the repository.
    kill_refs = kill
    for path in (GHOST, cfg["bin_js"], node):
        if os.path.isabs(path):
            kill_refs = kill_refs.replace(path, "<fixture>")
    chk("B5 kill bat 不写死管理器所在的仓库路径（仓库改名也照样拦得住）",
        _repo_name not in kill_refs and REPO not in kill_refs,
        " / ".join(l for l in kill_refs.splitlines() if _repo_name in l))
    chk("B6 kill bat 缺 dsh_kill.py 时给的是可操作提示（netstat + 按 PID 结束）",
        "netstat -ano" in kill and "PID" in kill)
    chk("B7 kill bat 缺脚本时 goto :eof（优雅退出，不是报错退出）",
        "  goto :eof" in kill)
    chk("B8 dsh-cli.bat 也只用槽内路径（无管理器依赖）",
        "dsh_manager" not in cli and "bin.js" in cli)

    # ── C 组：真跑（cmd.exe + 真 node）──────────────────────────────────
    print("\n== C. 实跑：BASE_DIR 指向不存在的目录 ==")
    can_run = os.name == "nt" and bool(node) and os.path.isfile(node)
    if not can_run:
        skip("C1..C4（本机没有可用的 node.exe，或不是 Windows）")
    else:
        env = dict(os.environ)
        env.pop("DSH_HOME", None)
        env.pop("DSH_PORT", None)

        def run_bat(name, *args):
            return subprocess.run(
                ["cmd", "/c", name] + list(args), cwd=slot_root, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=120,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        r1 = run_bat("dsh.bat")
        out1 = r1.stdout.decode("gbk", "replace")
        chk("C1 管理器不在，dsh.bat 仍能拉起 node 跑 bin.js",
            r1.returncode == 0 and "PROBE-NODE-OK" in out1,
            "rc=%s out=%r" % (r1.returncode, out1.strip()[-160:]))
        chk("C2 启动参数原样传给了 dsh（web --no-open --port N）",
            "web --no-open --port %d" % port in out1, out1.strip()[-160:])
        chk("C3 预检缺失时静默跳过，不挡启动也不念经",
            "跳过检测" not in out1 and "[警告]" not in out1, out1.strip()[-200:])

        r2 = run_bat("dsh-cli.bat", "--version")
        out2 = r2.stdout.decode("gbk", "replace")
        chk("C4 dsh-cli.bat 同样能跑，且参数透传",
            r2.returncode == 0 and "PROBE-NODE-OK" in out2 and "--version" in out2,
            "rc=%s out=%r" % (r2.returncode, out2.strip()[-160:]))

        r3 = run_bat("kill-%d.bat" % port)
        out3 = r3.stdout.decode("gbk", "replace")
        chk("C5 kill bat 找不到 dsh_kill.py 时优雅退出（rc=0）",
            r3.returncode == 0, "rc=%s out=%r" % (r3.returncode, out3.strip()[-200:]))
        chk("C6 kill bat 明确告诉用户「dsh 本身不受影响」",
            "dsh 本身不受影响" in out3, out3.strip()[-200:])

    # ── D 组：DSH_HOME 隔离不依赖管理器 ─────────────────────────────────
    print("\n== D. 数据目录隔离与管理器无关 ==")
    chk("D1 dsh.bat 里 DSH_HOME 是槽内绝对路径（不读注册表/不读 config.json）",
        'set "DSH_HOME=%s"' % home in start, home)
    chk("D2 数据目录不存在时 bat 自己会 mkdir（不靠管理器先建）",
        'if not exist "%DSH_HOME%" mkdir "%DSH_HOME%"' in start)
    chk("D3 TEMP 也是槽内绝对路径（不是 %TEMP% 继承）",
        'set "TEMP=%s"' % tempd in start, tempd)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
if FAILS:
    print("RESULT: FAIL（%d 项）: %s" % (len(FAILS), "; ".join(FAILS)))
    sys.exit(1)
print("RESULT: ALL PASS")
