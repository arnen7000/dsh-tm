# -*- coding: utf-8 -*-
"""离线回归：守护「生成的 bat 能被 cmd.exe 正常双击运行」这条底线。

背景（2026-10-15 故障反馈：dsh-cli.bat 双击没反应）
----------------------------------------------------
实测根因不是 node、不是 dsh，而是**编码**：bat 生成器原先把文件写成 UTF-8 无 BOM，
而 cmd.exe 读 .bat 用 ANSI 代码页（中文 Windows = 936/GBK）。后果：
  · 中文注释按 GBK 解析成乱码（表面问题）；
  · 相邻两个中文 REM 行时，按 GBK 的字节边界会把上一行行尾的 CRLF 吞掉/撕裂 ——
    实测 `REM 用法：... dsh-cli.bat <子命令> <参数>` 被从「双击进入」处撕成两半，
    后半截 `<子命令> <参数>` 被当成命令执行，`<` 触发输入重定向
    → cmd 报「命令语法不正确」rc=255 → 双击只看到窗口一闪就没。
  · UTF-8 带 BOM 更糟（实测同样 rc=255，cmd 把 BOM 当前提内容）。

所以约定必须是：**.bat 一律写 GBK + CRLF**（仓库 check-all.ps1 的编码检查一直这么要求，
只是它只扫仓库内的 bat，漏了生成到槽目录里的这三份）。本脚本把这条底线变成断言。

跑法：python scripts/test-bat-text.py    （退出码 0 = 全过，1 = 有失败）
"""
import ast
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, os.pardir))
SRC = os.path.join(REPO, "dsh_manager.pyw")
OPS = os.path.join(REPO, "dsh_install_ops.py")

if not os.path.exists(SRC):
    print("找不到 %s —— 请在管理器仓库内运行本脚本" % SRC)
    sys.exit(1)

FAILS = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ((" | " + extra) if extra else ""))
    if not cond:
        FAILS.append(name)


src = open(SRC, encoding="utf-8").read()
tree = ast.parse(src)

# ── 取出三个 bat 文本生成函数（AST 抽取，避开 GUI 副作用）─────────────────
keep = [n for n in tree.body
        if isinstance(n, ast.FunctionDef)
        and n.name in ("_bat_text_start", "_bat_text_cli", "_bat_text_kill")]
names = [n.name for n in keep]
chk("A1 三个 bat 生成函数都存在", len(keep) == 3, ",".join(names))

ns = {"os": os, "re": __import__("re"),
      "SLOT_KEYS": ["v1"], "VC": {}, "BASE_DIR": REPO,
      "STANDALONE_PYW": ""}
exec(compile(ast.Module(body=keep, type_ignores=[]), SRC, "exec"), ns)

# 一个「像真配置」的槽：路径形态与 config.json 一致（含正斜杠混写）
home = os.path.expanduser("~")
slot = {
    "label": "dshV1",
    "node_exe": home + r"/dsh-versions/dshV1/node/node.exe",
    "bin_js": home + r"/dsh-versions/dshV1/node_modules/@deepseek-ai/dsh/lib/bin.js",
    "dsh_home": home + r"/dsh-versions/dshV1/.dsh-home",
    "temp_dir": home + r"/dsh-versions/dsh-temp/dshV1",
    "port": 4080,
}

texts = {
    "dsh.bat": ns["_bat_text_start"]("v1", slot, home + r"\dsh-versions\dshV1"),
    "kill-4080.bat": ns["_bat_text_kill"]("v1", slot, home + r"\dsh-versions\dshV1"),
    "dsh-cli.bat": ns["_bat_text_cli"]("v1", slot, home + r"\dsh-versions\dshV1"),
}

# ── A 组：写盘编码与行尾约定 ────────────────────────────────────────────
chk("A2 _write_bat 用 gbk 编码（不是 utf-8）",
    'encoding="gbk"' in src and 'encoding="utf-8", newline=""' not in src)
chk("A3 dsh_install_ops 的 bat 写入也用 gbk",
    os.path.exists(OPS)
    and 'encoding="gbk", newline=""' in open(OPS, encoding="utf-8").read())

for name, text in texts.items():
    ok_gbk = True
    try:
        text.encode("gbk")
    except UnicodeEncodeError:
        ok_gbk = False
    chk("A4 %-13s 可无损 GBK 编码" % name, ok_gbk)
    chk("A5 %-13s 行尾是 CRLF" % name, "\r\n" in text and "\n" not in text.replace("\r\n", ""))

# ── B 组：注释行不得含 cmd 元字符 ───────────────────────────────────────
META = "<>|&^"
bad = []
for name, text in texts.items():
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith("REM") or s.startswith("echo"):
            hit = [c for c in META if c in s]
            if hit:
                bad.append("%s:L%d %r" % (name, i, s[:60]))
chk("B1 三个 bat 的 REM/echo 行不含 < > | & ^", not bad, "; ".join(bad) or "clean")

# ── C 组：真正跑一遍（cmd.exe 只在这台机器上有；没有就跳过，不算失败）──
# 安全约束（重要，别改坏）：
#   本组**绝不**让 bat 真的启动 dsh / 真的杀进程。做法：
#     1) 端口换成本机空闲端口（真端口可能正被用户的 dsh 占用，跑预检/kill 会动它）；
#     2) 所有会启动 node / dsh_kill 的行统一替换成 echo 标记。
#   本组要验的只有一件事：**cmd.exe 能不能无损解析并跑完这份 GBK 编码的 bat**。
CAN_RUN = os.name == "nt" and subprocess.run(
    ["cmd", "/c", "exit 0"], capture_output=True).returncode == 0
if not CAN_RUN:
    chk("C1 非 Windows 环境，跳过实跑（GBK/元字符断言仍有效）", True)
else:
    import socket

    def free_port():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    tmp = tempfile.mkdtemp(prefix="dsh-bat-text-")
    PORT = free_port()
    # C 组要**真的把 bat 交给 cmd 跑**，所以里面每条路径都会真的被创建。
    # 上面那份 fixture 用的是真实形态的路径（`~/.dsh-versions/dshV1`、`<临时目录根>\dshV1`），
    # 直接跑就等于拿用户的真实槽当沙盒 —— 实测会 mkdir 出 `<临时目录根>\dshV1`
    # （用户 2026-10-31 问「我删了怎么又出现了」，这一处也是元凶之一）。
    # 所以 C 组另做一份全部落在 tmp 里的槽，A/B 组仍用真实形态的文本做断言。
    cslot = dict(slot)
    cslot["node_exe"] = os.path.join(tmp, "slot", "node", "node.exe")
    cslot["bin_js"] = os.path.join(tmp, "slot", "node_modules", "@deepseek-ai",
                                   "dsh", "lib", "bin.js")
    cslot["dsh_home"] = os.path.join(tmp, "slot", ".dsh-home")
    cslot["temp_dir"] = os.path.join(tmp, "temp")
    ctexts = {
        "dsh.bat": ns["_bat_text_start"]("v1", cslot, os.path.join(tmp, "slot")),
        "kill-4080.bat": ns["_bat_text_kill"]("v1", cslot, os.path.join(tmp, "slot")),
        "dsh-cli.bat": ns["_bat_text_cli"]("v1", cslot, os.path.join(tmp, "slot")),
    }
    ran_all = True
    for name, text in ctexts.items():
        safe = []
        for line in text.splitlines():
            s = line.strip()
            # 换成空闲端口：避免预检/kill 碰到用户正在跑的 dsh
            line = line.replace(str(slot["port"]), str(PORT))
            # 真正启动 node / dsh_kill 的行 → echo 标记（不起进程、不杀进程）
            if (s.startswith('"') and ("node.exe" in s or s.endswith("%*"))) \
                    or "--port" in s or "%KILL%" in s or "%PRECHK%" in s:
                safe.append("echo C_RAN_OK_%s" % name.replace(".", "_"))
            else:
                safe.append(line)
        body = "\r\n".join(safe) + "\r\n"
        p = os.path.join(tmp, name)
        with open(p, "w", encoding="gbk", newline="") as f:
            f.write(body)
        try:
            r = subprocess.run(["cmd", "/c", p], cwd=tmp,
                               capture_output=True, timeout=60,
                               stdin=subprocess.DEVNULL)
            out = (r.stdout or b"").decode("gbk", "replace")
            err = (r.stderr or b"").decode("gbk", "replace")
            ok = (r.returncode == 0
                  and "C_RAN_OK_" + name.replace(".", "_") in out
                  and not err.strip())
            ran_all = ran_all and ok
            chk("C2 %-13s GBK 写入后 cmd 实跑通过 (rc=%s)" % (name, r.returncode),
                ok, (out + err).strip()[:80].replace("\n", " "))
        except subprocess.TimeoutExpired:
            ran_all = False
            chk("C2 %-13s cmd 实跑" % name, False, "超时")
    chk("C3 三个 bat 全部实跑通过", ran_all)

    # 反向参照（仅信息，不作为硬断言）：UTF-8+带BOM 是「旧 bug 的近亲形态」。
    # 注意：cmd.exe 对 BOM 的处理本身不稳定（同一份内容有时 rc=0、有时 rc=255），
    # 所以这里只报告、不判失败 —— 真正的硬断言是上面的 A/B/C 三组。
    if ran_all:
        p = os.path.join(tmp, "neg-utf8bom.bat")
        safe = []
        for line in ctexts["dsh-cli.bat"].splitlines():
            s = line.strip()
            line = line.replace(str(slot["port"]), str(PORT))
            if s.startswith('"') and ("node.exe" in s or s.endswith("%*")):
                safe.append("echo C_RAN_OK_dsh-cli_bat")
            else:
                safe.append(line)
        body = "\r\n".join(safe) + "\r\n"
        with open(p, "wb") as f:
            f.write(b"\xef\xbb\xbf" + body.encode("utf-8"))
        try:
            r = subprocess.run(["cmd", "/c", p], cwd=tmp, capture_output=True,
                               timeout=60, stdin=subprocess.DEVNULL)
            print("INFO  C4 反向参照：UTF-8+BOM 写入 rc=%s（旧 bug 的近亲形态，"
                  "已知 cmd 对此处理不稳定，仅记录不判定）" % r.returncode)
        except subprocess.TimeoutExpired:
            print("INFO  C4 反向参照：UTF-8+BOM 写入超时（阻塞，同样说明不可用）")

print()
if FAILS:
    print("RESULT: FAIL（%d 项）: %s" % (len(FAILS), "; ".join(FAILS)))
    sys.exit(1)
print("RESULT: ALL PASS")
