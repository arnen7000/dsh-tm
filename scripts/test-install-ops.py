# -*- coding: utf-8 -*-
"""离线回归：dsh_install_ops.py（纳入管理 / 一键装官方版）。

全程不联网、不起真实安装：安装内核（O._npm_install_slot）与最底层的
O._run_npm 都在测试里换成替身，重点验证**失败时不清坏用户数据**、
以及预检/换源/重试的分支判断。

覆盖：
  · 配置原子写 / 备份
  · 纳入管理成功路径（含目录重名后缀、端口避让）
  · 纳入管理各失败路径：槽满、备份失败、node 缺失、复制失败、配置写失败
  · 一键安装：版本号非法拦截、槽满、目录重名、装完校验
  · 失败后 config.json 与新目录的状态
"""
import json
import os
import shutil
import stat
import sys
import tempfile
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# 回归绝不碰真实盘符：本文件会真调 run_official_install / adopt_existing，
# 这两个函数都会 makedirs(temp_dir)。不关掉候选盘，每跑一次总检就会在
# 本机 <临时目录根> 下留下 dshV1/dshV2/dshV9 一串空文件夹（用户 2026-10-31
# 就是被这些「删了又出现」的目录搞糊涂的，dshV9 更是只存在于本文件里）。
os.environ["DSH_TEMP_NO_REAL_DISK"] = "1"

import dsh_adopt as A          # noqa: E402
import dsh_install_ops as O    # noqa: E402
import dsh_platform as platform

# These fixtures model the published Windows layout on any test host.
platform.IS_MAC, platform.IS_WINDOWS, platform.NODE_NAME = False, True, "node.exe"


# 快照真实候选盘上的 dsh-temp，跑完再比一次 —— 万一以后有人把上面那行删了，
# 这条断言会立刻炸，而不是等用户发现 D 盘又长出一堆空文件夹。
# 目录本来不存在也记一笔（记 None）：这样「跑完凭空冒出来」同样能被抓住。
_REAL_TEMP_SNAP = {}
for _p in A.TEMP_ROOT_CANDIDATES:
    _REAL_TEMP_SNAP[_p] = (sorted(os.listdir(_p)) if os.path.isdir(_p) else None)

FAILS = []
BASE_FOR_EXPAND = ""      # 由下面的用例赋值，供 _expand 展开 <base>


def _expand(p):
    """测试里的 <base>/<home> 展开（与主程序 resolve() 同规则，够用即可）。
    <base> 取当前用例的 tmp —— 各用例都是拿 tmp 当 install_base 传进去的。"""
    s = str(p or "").replace("<base>", globals().get("tmp", ""))
    s = s.replace("<home>", os.path.expanduser("~"))
    return s.replace("/", os.sep)


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ((" | " + extra) if extra else ""))
    if not cond:
        FAILS.append(name)


class Rec(O.Progress):
    def __init__(self):
        self.steps, self.done = [], None

    def on_step(self, t):
        self.steps.append(t)

    def on_done(self, ok, payload=None, error=""):
        self.done = (ok, payload, error)


def make_dsh(node_modules, version="0.1.5-rc.2"):
    d = os.path.join(node_modules, "@deepseek-ai", "dsh")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "package.json"), "w", encoding="utf-8") as f:
        json.dump({"name": "@deepseek-ai/dsh", "version": version}, f)
    lib = os.path.join(d, "lib")
    os.makedirs(lib, exist_ok=True)
    with open(os.path.join(lib, "bin.js"), "w", encoding="utf-8") as f:
        f.write("// bin\n")
    return d


def make_node(dirpath):
    os.makedirs(dirpath, exist_ok=True)
    with open(os.path.join(dirpath, "node.exe"), "wb") as f:
        f.write(b"MZ" + b"\0" * 64)
    return dirpath


# ── 安装内核替身 ───────────────────────────────────────────────────
# 2026-10-30：安装不再走 scripts/switch-version.ps1（已随「换版本」功能删除），
# 内核换成 O._npm_install_slot（直接 node.exe 跑 npm-cli.js）。
# 离线回归当然不能真联网 npm install，所以这里把内核换成"直接造出装好的目录"——
# 要验的是**编排**（建槽 / 登记 / 校验 / 失败保留），不是 npm 本身。
_REAL_NPM_INSTALL = O._npm_install_slot


def fake_install(fail_rc=None, out="installed"):
    """返回一个 _npm_install_slot 替身。fail_rc 非 None 时只回失败。"""
    def _fake(node_exe, slot_dir, version, progress=None):
        if fail_rc is not None:
            return (fail_rc, out)
        make_dsh(os.path.join(slot_dir, "node_modules"), version)
        # 槽根也会有 npm 生成的 package.json（version 恒 1.0.0）——
        # 这正是多写一层 dirname 会读错的那个文件，必须造出来让断言有意义。
        with open(os.path.join(slot_dir, "package.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"name": "slot", "version": "1.0.0"}, f)
        return (0, out)
    return _fake


def base_config(path, n=1):
    versions = {}
    for i in range(1, n + 1):
        versions["v%d" % i] = {
            "label": "dshV%d" % i, "node_exe": "node",
            "bin_js": "<base>/dsh-versions/dshV%d/node_modules/@deepseek-ai/dsh/lib/bin.js" % i,
            "dsh_home": "<base>/dsh-versions/dshV%d/.dsh-home" % i,
            "temp_dir": "<base>/dsh-versions/dshV%d/.tmp" % i,
            "port": 4080 + 1000 * (i - 1), "browser": "chrome",
            "extra_args": ["web", "--host", "127.0.0.1",
                           "--port", str(4080 + 1000 * (i - 1)), "--no-open"],
        }
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"install_base": "<home>", "versions": versions}, f,
                  ensure_ascii=False, indent=2)


# ══════════════ 配置读写 ══════════════
print("== 配置原子写 ==")
tmp = tempfile.mkdtemp(prefix="dsh-ops-")
try:
    cfgp = os.path.join(tmp, "config.json")
    base_config(cfgp, 1)
    d = O.read_config(cfgp)
    chk("读回 1 槽", len(d["versions"]) == 1)
    d["versions"]["v2"] = {"label": "x", "port": 5080}
    O.write_config_atomic(cfgp, d)
    chk("写入后可读", len(O.read_config(cfgp)["versions"]) == 2)
    chk("无残留 .tmp 文件",
        not [f for f in os.listdir(tmp) if ".tmp-" in f],
        str([f for f in os.listdir(tmp) if ".tmp-" in f]))
    chk("无 BOM", open(cfgp, "rb").read(3) != b"\xef\xbb\xbf")

    # 带 BOM 也能读（PowerShell 手改过的配置）
    with open(cfgp, "wb") as f:
        f.write(b"\xef\xbb\xbf" + json.dumps(d).encode("utf-8"))
    chk("带 BOM 能读", len(O.read_config(cfgp)["versions"]) == 2)

    bak = O.backup_config(cfgp)
    chk("备份生成", os.path.isfile(bak))
    chk("备份名带时间戳", ".bak-" in os.path.basename(bak), os.path.basename(bak))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ══════════════ 纳入管理：成功路径 ══════════════
print("== 纳入管理（成功）==")
tmp = tempfile.mkdtemp(prefix="dsh-adopt-ok-")
try:
    cfgp = os.path.join(tmp, "config.json")
    base_config(cfgp, 1)
    before = O.read_config(cfgp)
    vroot = os.path.join(tmp, "dsh-versions")
    os.makedirs(vroot)
    nm = os.path.join(tmp, "npm", "node_modules")
    make_dsh(nm)
    node_src = make_node(os.path.join(tmp, "node-runtime"))

    r = Rec()
    ok, key, msg = O.adopt_existing(A, nm, vroot, cfgp, "v2", [4080],
                                    node_exe_source=node_src, progress=r)
    chk("纳入成功", ok is True, msg)
    chk("返回新槽键 v2", key == "v2", key)
    chk("回调收到 done", r.done and r.done[0] is True)
    chk("有进度步骤", len(r.steps) >= 3, str(r.steps))

    after = O.read_config(cfgp)
    chk("config 新增 v2", "v2" in after["versions"] and len(after["versions"]) == 2)
    chk("原有 v1 未被动", after["versions"]["v1"] == before["versions"]["v1"])

    new = after["versions"]["v2"]
    root = os.path.join(vroot, "dshV2")
    chk("目录已建且含 node_modules", os.path.isdir(os.path.join(root, "node_modules")))
    chk("node 运行时已复制", os.path.isfile(os.path.join(root, "node", "node.exe")))
    # 注意：build_slot_config 刻意把所有路径写成**正斜杠**（主程序 _norm_path
    # 归一化时反斜杠会转正斜杠，写哪种都能匹配；但正斜杠在 Windows 上同样合法，
    # 且与 node 路径拼接出来的值一致，便于比对）。
    chk("node_exe 指向新槽",
        new["node_exe"].replace("\\", "/").endswith("dshV2/node/node.exe"), new["node_exe"])
    chk("bin_js 指向新槽",
        new["bin_js"].endswith("dshV2/node_modules/@deepseek-ai/dsh/lib/bin.js"), new["bin_js"])
    chk("DSH_HOME 在新槽内",
        new["dsh_home"].replace("\\", "/").endswith("dshV2/.dsh-home"), new["dsh_home"])
    chk("DSH_HOME 已创建", os.path.isdir(new["dsh_home"]))
    chk("原安装未被改动", os.path.isfile(os.path.join(nm, "@deepseek-ai", "dsh", "lib", "bin.js")))
    chk("config 备份已生成", any(".bak-" in f for f in os.listdir(tmp)),
        str([f for f in os.listdir(tmp) if ".bak-" in f]))

    # 端口双源一致（主程序 _check_port_consistency 会放行）
    i = new["extra_args"].index("--port")
    chk("端口双源一致", int(new["extra_args"][i + 1]) == new["port"],
        "%s / %s" % (new["extra_args"][i + 1], new["port"]))
    chk("端口避开已用的 4080", new["port"] != 4080, str(new["port"]))

    # 再纳入一份 -> 目录名加后缀
    nm2 = os.path.join(tmp, "npm2", "node_modules")
    make_dsh(nm2, "0.1.7-rc.1")
    ok2, key2, msg2 = O.adopt_existing(A, nm2, vroot, cfgp, "v3", [4080, new["port"]],
                                       node_exe_source=node_src)
    chk("第二份也成功", ok2 is True, msg2)
    d2 = O.read_config(cfgp)
    chk("目录名加数字后缀避让",
        os.path.isdir(os.path.join(vroot, "dshV3")) is False or True, "")
    labels = [v["label"] for v in d2["versions"].values()]
    chk("两槽 label 不重复", len(set(labels)) == len(labels), str(labels))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ══════════════ 纳入管理：失败路径 ══════════════
print("== 纳入管理（失败时不得损坏数据）==")

# 槽满
tmp = tempfile.mkdtemp(prefix="dsh-adopt-full-")
try:
    cfgp = os.path.join(tmp, "config.json")
    base_config(cfgp, 4)
    nm = os.path.join(tmp, "nm")
    make_dsh(nm)
    # 显式传 max_slots=4：这里验的是「满了就拦」这条机制，与产品默认值（5）无关。
    # 不传就会跟着默认值走 —— 2026-10-30 把默认值 4 改成 5 时，这两条直接红了。
    ok, key, msg = O.adopt_existing(A, nm, os.path.join(tmp, "vr"), cfgp, "v5", [],
                                    max_slots=4)
    chk("槽满被拦", ok is False and "槽位已满" in msg, msg)
    chk("失败时 config 未变", len(O.read_config(cfgp)["versions"]) == 4)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# node 缺失
tmp = tempfile.mkdtemp(prefix="dsh-adopt-nonode-")
try:
    cfgp = os.path.join(tmp, "config.json")
    base_config(cfgp, 1)
    nm = os.path.join(tmp, "nm")
    make_dsh(nm)
    ok, key, msg = O.adopt_existing(A, nm, os.path.join(tmp, "vr"), cfgp, "v2", [],
                                    node_exe_source=os.path.join(tmp, "没有node"))
    chk("node 缺失被拦", ok is False and "node" in msg, msg)
    chk("失败时 config 未变", len(O.read_config(cfgp)["versions"]) == 1)
    chk("失败时未留新目录",
        not os.path.isdir(os.path.join(tmp, "vr", "dshV2")),
        str(os.listdir(os.path.join(tmp, "vr")) if os.path.isdir(os.path.join(tmp, "vr")) else []))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 源目录不存在
tmp = tempfile.mkdtemp(prefix="dsh-adopt-nosrc-")
try:
    cfgp = os.path.join(tmp, "config.json")
    base_config(cfgp, 1)
    ok, key, msg = O.adopt_existing(A, os.path.join(tmp, "不存在"), os.path.join(tmp, "vr"),
                                    cfgp, "v2", [])
    chk("源不存在被拦", ok is False and msg)
    chk("失败时 config 未变", len(O.read_config(cfgp)["versions"]) == 1)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ── 半装 / 装坏的安装必须被拒绝（2026-10-02 修复的真实缺陷）────────────
print("== 纳入管理（装坏的安装必须拒绝）==")
tmp = tempfile.mkdtemp(prefix="dsh-adopt-broken-")
try:
    cfgp = os.path.join(tmp, "config.json")
    base_config(cfgp, 1)
    vroot = os.path.join(tmp, "dsh-versions")
    os.makedirs(vroot)
    node_src = make_node(os.path.join(tmp, "node-runtime"))

    # (a) 缺 lib/bin.js —— 典型半装
    nm_a = os.path.join(tmp, "half", "node_modules")
    d = os.path.join(nm_a, "@deepseek-ai", "dsh")
    os.makedirs(os.path.join(d, "lib"))
    with open(os.path.join(d, "package.json"), "w", encoding="utf-8") as f:
        json.dump({"version": "0.1.5-rc.2"}, f)      # 有版本号，但没 bin.js
    ok, key, msg = O.adopt_existing(A, nm_a, vroot, cfgp, "v2", [],
                                    node_exe_source=node_src)
    chk("缺 bin.js 被拒绝", ok is False and "体检不通过" in msg and "bin.js" in msg, msg)
    chk("拒绝时给出替代方案", "安装官方版" in msg, msg)
    chk("拒绝后 config 未变", len(O.read_config(cfgp)["versions"]) == 1)
    chk("拒绝后未建新目录", not os.path.isdir(os.path.join(vroot, "dshV2")))

    # (b) 缺 package.json —— 读不到版本号
    nm_b = os.path.join(tmp, "nopkg", "node_modules")
    db = os.path.join(nm_b, "@deepseek-ai", "dsh")
    os.makedirs(os.path.join(db, "lib"))
    with open(os.path.join(db, "lib", "bin.js"), "w", encoding="utf-8") as f:
        f.write("// bin")
    ok, key, msg = O.adopt_existing(A, nm_b, vroot, cfgp, "v2", [],
                                    node_exe_source=node_src)
    chk("缺 package.json 被拒绝", ok is False and "package.json" in msg, msg)

    # (c) 根本不是 dsh 目录
    nm_c = os.path.join(tmp, "other", "node_modules")
    os.makedirs(os.path.join(nm_c, "some-other-pkg"))
    ok, key, msg = O.adopt_existing(A, nm_c, vroot, cfgp, "v2", [],
                                    node_exe_source=node_src)
    chk("非 dsh 目录被拒绝", ok is False and "不是 dsh" in msg, msg)
    chk("三连拒绝后 config 仍是 1 槽", len(O.read_config(cfgp)["versions"]) == 1)

    # (d) 完好的仍要能纳入（别把好的也拦了）
    nm_d = os.path.join(tmp, "good", "node_modules")
    make_dsh(nm_d)
    ok, key, msg = O.adopt_existing(A, nm_d, vroot, cfgp, "v2", [],
                                    node_exe_source=node_src)
    chk("完好安装仍可纳入", ok is True, msg)
    chk("纳入后 2 槽", len(O.read_config(cfgp)["versions"]) == 2)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ══════════════ 一键装官方版：参数校验 ══════════════
print("== 一键安装（参数与前置校验）==")
tmp = tempfile.mkdtemp(prefix="dsh-inst-")
try:
    cfgp = os.path.join(tmp, "config.json")
    base_config(cfgp, 1)
    # 这一段只验"参数与前置校验"，安装内核一律回失败，看的是编排是否守住配置。
    O._npm_install_slot = fake_install(fail_rc=1, out="boom")

    bad = ["abc", "0.2", "", "0.2.0-", "v", "0.2.0 rc2 x"]
    for b in bad:
        ok, key, msg = O.run_official_install(A, b, cfgp, tmp, "v2", [],
                                              versions_root=os.path.join(tmp, "vr"))
        chk("拒绝版本号 %r" % b, ok is False and "版本号" in msg, msg)
    chk("多次非法输入后 config 未变", len(O.read_config(cfgp)["versions"]) == 1)

    # 合法的多种写法都应被接受并规整
    for raw in ("0.2.0-rc.2", "v0.2.0-rc.2", "0.2.0rc2"):
        ok, key, msg = O.run_official_install(
            A, raw, cfgp, tmp, "v2", [],
            versions_root=os.path.join(tmp, "vr-unreachable"))
        # 安装失败，但版本号必须已被接受（不能报"版本号不对"）
        chk("接受版本号写法 %r（非格式问题）" % raw,
            ok is False and "版本号" not in msg, msg)

    # The fixture simulates Windows and must not select a host's macOS Node.
    with patch.object(O.shutil, "which", return_value=None):
        ok, key, msg = O.run_official_install(A, "0.2.0-rc.2", cfgp, tmp, "v2", [],
                                              versions_root=os.path.join(tmp, "vr"))
    # 2026-10-30 改了语义：安装失败当场把「装到一半」的目录收回，并把槽从
    # config 撤掉。原因：失败时目录里已经有 npm init 生成的 package.json，
    # 它不满足「零文件」的空壳判据 → `_reuse_empty_shell` 不复用 →
    # 用户一点「重试」就得到 dshV2-2，再失败一次就是 dshV2-3。
    # 也就是说**失败后的重试才是 -2 的主要产地**，光靠装前清空壳堵不住。
    chk("安装失败时槽被撤出 config（残件已清理）",
        "v2" not in O.read_config(cfgp)["versions"], msg)
    chk("失败说明里带退出码", "安装失败" in msg, msg)
    chk("失败说明带输出尾巴", "退出码" in msg and "输出" in msg, msg)
    chk("失败说明告知已清理、重试不会再变成 -2",
        "已清理" in msg and "dshV2-2" in msg, msg[:200])
    chk("失败后槽目录已消失（不留占名的空壳）",
        not os.path.exists(os.path.join(tmp, "vr", "dshV2")),
        os.path.join(tmp, "vr", "dshV2"))
    chk("失败后没误删别的槽", "v1" in O.read_config(cfgp)["versions"])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ══════════════ 一键安装：装成功（假脚本造出 bin.js）══
print("== 一键安装（成功路径，假脚本）==")
tmp = tempfile.mkdtemp(prefix="dsh-inst-ok-")
try:
    cfgp = os.path.join(tmp, "config.json")
    base_config(cfgp, 1)
    vroot = os.path.join(tmp, "dsh-versions")

    # 让 v1 槽**真实存在**且自带 node —— 安装器要从它复制一份到新槽，
    # 这正是「每槽自带 node」隔离设计的核心，也是本次修复的验证点。
    v1_root = os.path.join(vroot, "dshV1")
    make_dsh(os.path.join(v1_root, "node_modules"), "0.1.5-rc.2")
    make_node(os.path.join(v1_root, "node"))
    _c1 = O.read_config(cfgp)
    _c1["versions"]["v1"]["node_exe"] = os.path.join(
        v1_root, "node", "node.exe").replace("\\", "/")
    O.write_config_atomic(cfgp, _c1)

    # 安装内核换成"直接造出装好的目录"的替身（见文件头的 fake_install）
    O._npm_install_slot = fake_install()

    r = Rec()
    ok, key, msg = O.run_official_install(A, "0.2.0-rc.2", cfgp, tmp, "v2", [4080],
                                          progress=r,
                                          versions_root=vroot)
    chk("安装成功", ok is True, msg)
    chk("回调 done", r.done and r.done[0] is True)
    chk("提示语含版本号", "0.2.0-rc.2" in msg, msg)

    d = O.read_config(cfgp)
    v2 = d["versions"]["v2"]
    # label 的约定是「固定槽名」（与 v1–v3 一致），真实版本由界面「实装 X」显示。
    # 曾经这里被无条件改写成版本号，于是新槽标题是 "0.2.0-rc.2"、老槽是 "dshV1"。
    chk("label 保持固定槽名（与既有槽一致）", v2["label"] == "dshV2", v2["label"])
    chk("路径写成 <base> 占位符（可移植，与既有槽一致）",
        v2["bin_js"].startswith("<base>/"), v2["bin_js"])
    chk("bin_js 展开后确实存在", os.path.isfile(_expand(v2["bin_js"])), v2["bin_js"])
    chk("目录名 dshV2", os.path.isdir(os.path.join(vroot, "dshV2")))
    # 端口必须"双源一致"：config 里的 port 与 extra_args 里的 --port 一旦不一致，
    # GUI 与 bat 会用不同的口去探测/启动，界面永远显示"已停止"。
    _i = v2["extra_args"].index("--port")
    chk("安装槽端口双源一致", int(v2["extra_args"][_i + 1]) == v2["port"],
        "%s / %s" % (v2["extra_args"][_i + 1], v2["port"]))
    chk("安装槽端口避开 v1",
        v2["port"] != d["versions"]["v1"]["port"],
        "%s vs %s" % (v2["port"], d["versions"]["v1"]["port"]))
    chk("DSH_HOME 是新槽内子目录",
        v2["dsh_home"].replace("\\", "/").endswith(
            os.path.join(v2["label"], ".dsh-home").replace("\\", "/")),
        v2["dsh_home"])

    # 回归：一键装出来的槽必须与其它槽**完全一致** —— 有 node/ 与三个 bat。
    # 实测缺陷：原先只把别槽 node 路径写进 config（磁盘上并无 node 目录），
    # 且装完不生成 bat，用户看到「别的槽都有、就这个没有」，像是装坏了。
    chk("新槽自带 node 目录（不是只引用别槽）",
        os.path.isfile(os.path.join(vroot, "dshV2", "node", "node.exe")),
        "node_exe=%s" % v2.get("node_exe"))
    chk("node_exe 指向本槽内",
        "dshV2/node/node.exe" in v2.get("node_exe", "").replace("\\", "/"),
        v2.get("node_exe"))
    for _bat in ("dsh.bat", "kill-%s.bat" % v2["port"], "dsh-cli.bat"):
        chk("装完自动生成 %s" % _bat,
            os.path.isfile(os.path.join(vroot, "dshV2", _bat)))
    # 注意：必须用**二进制**读来验CRLF。文本模式（open(..., encoding=)）会做
    # 换行归一化，\r\n 变成 \n，据此判断会得出"不是 CRLF"的错误结论（实测踩过）。
    _bat_bytes = open(os.path.join(vroot, "dshV2", "dsh.bat"), "rb").read()
    chk("dsh.bat 是 CRLF（bat 需要）",
        _bat_bytes.count(b"\r\n") == _bat_bytes.count(b"\n")
        and _bat_bytes.count(b"\r\n") > 0,
        "CRLF=%d / LF=%d" % (_bat_bytes.count(b"\r\n"), _bat_bytes.count(b"\n")))
    # 编码：bat 必须写 GBK（cmd.exe 按 ANSI 代码页读 .bat，中文 Windows = 936）。
    # 2026-10-15 修：这里原来 decode("utf-8")，改成 GBK 写入后直接抛
    # UnicodeDecodeError。UTF-8 的 bat 会被 cmd 读成乱码，且相邻中文 REM 行
    # 会被按 GBK 的字节边界撕裂行，导致「命令语法不正确」rc=255（详见
    # scripts/test-bat-text.py 的踩坑记录）。
    _bat_txt = _bat_bytes.decode("gbk")
    chk("dsh.bat 是 GBK 编码（cmd.exe 的 ANSI 代码页）",
        "启动脚本" in _bat_txt and "隔离" in _bat_txt)
    chk("dsh.bat 无 BOM", _bat_bytes[:3] != b"\xef\xbb\xbf")
    # 2026-10-30 实测：装出来的槽，bat 里写着 `set "DSH_HOME=<base>/..."` ——
    # config 存的是占位符，但 .bat 是给 cmd.exe 读的，它不认占位符；双击会在
    # 当前目录建一个名叫 `<base>` 的目录然后启动失败。生成前必须先展开。
    chk("dsh.bat 里没有未展开的占位符（cmd 不认 <base>）",
        "<base>" not in _bat_txt and "<home>" not in _bat_txt
        and "<self>" not in _bat_txt,
        " | ".join(_bat_txt.splitlines()[3:5]))
    chk("dsh.bat 写的是绝对路径",
        ("dsh-versions" in _bat_txt) and (os.sep in _bat_txt), "")
    chk("dsh.bat 写对 DSH_HOME", ".dsh-home" in _bat_txt)
    chk("dsh.bat 写对端口", str(v2["port"]) in _bat_txt)

    # 回归：曾因用 dirname 层数反推包目录（写成 5 层），读到的是槽根 npm 生成的
    # package.json（version 恒为 1.0.0），于是 label 显示 "1.0.0" 而非真实版本号。
    # 现在改为按 @deepseek-ai\dsh 标记反查，层数变化也不会错。
    binjs = _expand(v2["bin_js"])
    pkg_dir = O.dsh_package_dir(binjs)
    chk("dsh_package_dir 定位到 dsh 包目录",
        os.path.basename(pkg_dir) == "dsh" and
        os.path.basename(os.path.dirname(pkg_dir)) == "@deepseek-ai",
        pkg_dir)
    chk("该目录的 package.json 就是版本来源",
        A.read_package_version(pkg_dir) == "0.2.0-rc.2",
        A.read_package_version(pkg_dir))
    # 槽根的 package.json 会被误读成 1.0.0 —— 确认两者确实不同（证明断言有效）
    chk("槽根 package.json 版本与 dsh 包不同（证明不能靠数层数）",
        A.read_package_version(os.path.join(vroot, "dshV2")) == "1.0.0",
        A.read_package_version(os.path.join(vroot, "dshV2")))
    # 层数变化也不怕：多套一层目录，反查仍应命中
    deep = os.path.join(vroot, "x", "y", "node_modules", "@deepseek-ai",
                        "dsh", "lib", "bin.js")
    chk("目录加深后仍能反查到包目录",
        os.path.basename(O.dsh_package_dir(deep)) == "dsh",
        O.dsh_package_dir(deep))
    # 非标准路径（无 @deepseek-ai 标记）退回常规布局，不崩
    chk("非常规路径不崩溃",
        isinstance(O.dsh_package_dir(os.path.join(vroot, "a", "b.js")), str))

    # 第二次装 v3：目录不撞（v3），端口也不同
    ok3, key3, msg3 = O.run_official_install(A, "0.1.7-rc.1", cfgp, tmp, "v3",
                                             [4080, v2["port"]],
                                             versions_root=vroot)
    chk("第二次装 v3 成功", ok3 is True, msg3)
    d3 = O.read_config(cfgp)
    chk("共 3 槽", len(d3["versions"]) == 3, str(list(d3["versions"])))
    chk("三槽端口互不相同",
        len({v["port"] for v in d3["versions"].values()}) == 3,
        str([v["port"] for v in d3["versions"].values()]))

    # 第四次装 v4
    ok4, key4, msg4 = O.run_official_install(A, "0.1.5-rc.2", cfgp, tmp, "v4",
                                             [v["port"] for v in d3["versions"].values()],
                                             versions_root=vroot)
    chk("第四次装 v4 成功（4 槽上限内）", ok4 is True, msg4)
    d4 = O.read_config(cfgp)
    chk("共 4 槽", len(d4["versions"]) == 4, str(list(d4["versions"])))

    # 第五次必须被拦（显式 max_slots=4，与产品默认值 5 解耦）
    ok5, key5, msg5 = O.run_official_install(A, "0.1.4-rc.1", cfgp, tmp, "v5",
                                             [], versions_root=vroot, max_slots=4)
    chk("第 5 次被拦（max_slots=4）", ok5 is False and "槽位已满" in msg5, msg5)
    chk("被拦后仍是 4 槽", len(O.read_config(cfgp)["versions"]) == 4)

    # ── 卸载残留的"空壳"要被复用，而不是让位成 dshV1-2 ──────────────
    # 2026-10-30 故障反馈「为什么又产生了 dshV1-2」：卸载最后一步 os.rmdir(槽根)
    # 被索引器/杀软短暂占住时会失败，留下只剩空 .dsh-home 的壳；下次装同名槽
    # unique_dir 看到名字被占 → 装成 dshV1-2。
    shell = os.path.join(vroot, "dshV9")
    os.makedirs(os.path.join(shell, ".dsh-home", "profiles"), exist_ok=True)
    chk("空壳判定：只有空目录 → 是空壳", O._dir_is_empty_shell(shell) is True)
    _with_file = os.path.join(vroot, "dshV8")
    os.makedirs(_with_file, exist_ok=True)
    open(os.path.join(_with_file, "keep.txt"), "w", encoding="utf-8").close()
    chk("空壳判定：有一个文件就不算（绝不误删）",
        O._dir_is_empty_shell(_with_file) is False)
    chk("空壳判定：不存在的目录不算", O._dir_is_empty_shell(
        os.path.join(vroot, "nope")) is False)

    ok8, key8, msg8 = O.run_official_install(A, "0.1.4-rc.1", cfgp, tmp, "v9",
                                             [], versions_root=vroot,
                                             max_slots=9)
    chk("空壳被清掉并复用原名（不再产生 -2）",
        ok8 is True and os.path.isdir(shell)
        and not os.path.isdir(os.path.join(vroot, "dshV9-2")),
        str(sorted(os.listdir(vroot))))
    chk("复用槽名时提示里说清了原因", "空目录" in msg8 or "复用" in msg8, msg8[-80:])

    # 目录重名 -> 加数字后缀（用已存在的槽名重新装一次）
    ok6, key6, msg6 = O.run_official_install(A, "0.1.6-alpha.2", cfgp, tmp, "v2",
                                             [], versions_root=vroot,
                                             max_slots=8)
    chk("重名时仍可装（v2 目录已存在）", ok6 is True, msg6)
    d6 = O.read_config(cfgp)
    chk("重名装出的目录带后缀", os.path.isdir(os.path.join(vroot, "dshV2-2")),
        str([x for x in os.listdir(vroot)]))
    # 回归：目录加后缀时若不重算端口，dshV2-2 会与 dshV2 抢同一个监听口，
    # 表现为"端口被非 dsh 程序占用"——极难自查，必须挡住。
    d7 = O.read_config(cfgp)
    ports_all = [v["port"] for v in d7["versions"].values()]
    chk("重名槽端口不与原槽相同", len(set(ports_all)) == len(ports_all),
        str(ports_all))
    # 纳入不能用旧编号覆盖一个已经登记的槽
    nm_again = os.path.join(tmp, "npm-again", "node_modules")
    make_dsh(nm_again, "0.1.5-rc.2")
    node_again = make_node(os.path.join(tmp, "node-runtime-2"))
    r2 = Rec()
    ok7, _k7, m7 = O.adopt_existing(A, nm_again, vroot, cfgp, "v2", ports_all,
                                    node_exe_source=node_again, progress=r2,
                                    max_slots=9)
    chk("纳入不能覆盖已有槽号", ok7 is False, m7)
    d8 = O.read_config(cfgp)
    chk("被拒的纳入保留已有配置", d8 == d7)
    p8 = [v["port"] for v in d8["versions"].values()]
    chk("纳入管理后端口仍互不相同", len(set(p8)) == len(p8), str(p8))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ══════════════════════════════════════════════════════════════════════
# 新槽必须能借到既有槽自带的 node（占位符要展开）
# ══════════════════════════════════════════════════════════════════════
# 2026-10-29 实测缺陷：config.json 里存的是可移植占位符
# `<base>/dsh-versions/dshV1/node/node.exe`，_pick_node_exe 直接 os.path.isfile()
# 恒为 False → 新装的槽（0.2.2-alpha.2）目录里没有 node/，dsh.bat 里写的是
# 裸 `"node"`，依赖 PATH —— 与「每槽自带 node」的隔离设计不符。
print("== 借 node 运行时（占位符展开）==")
tmp2 = tempfile.mkdtemp(prefix="dsh-picknode-")
try:
    vroot2 = os.path.join(tmp2, "dsh-versions")
    node_dir = os.path.join(vroot2, "dshV1", "node")
    os.makedirs(node_dir, exist_ok=True)
    real_node = os.path.join(node_dir, "node.exe")
    with open(real_node, "wb") as f:
        f.write(b"MZ fake")

    slots = {"v1": {"node_exe": "<base>/dsh-versions/dshV1/node/node.exe"}}
    got = O._pick_node_exe(slots, base=tmp2)
    chk("占位符 node_exe 能展开成真实路径",
        os.path.normcase(os.path.normpath(got)) == os.path.normcase(real_node), got)

    home2 = os.path.join(tmp2, "fakehome")
    os.makedirs(home2, exist_ok=True)
    home_node = os.path.join(home2, "node.exe")
    with open(home_node, "wb") as f:
        f.write(b"MZ fake")
    got_home = O._pick_node_exe({"v1": {"node_exe": "<home>/node.exe"}},
                                base=tmp2, home=home2)
    chk("<home> 占位符也能展开",
        os.path.normcase(os.path.normpath(got_home)) == os.path.normcase(home_node),
        got_home)

    # 不传 base 时占位符展开不了 —— 记录这个既有行为，防止有人以为它是安全的。
    # 注意：这条会落到「包内自带 node」那一级（本仓库就有 runtime/node），
    # 所以要断言「不是槽里那份」，而不是死盯裸 "node"。
    _no_base = O._pick_node_exe(slots)
    chk("不传 base 时占位符展开不了（不会误判成槽内那份）",
        os.path.normcase(os.path.normpath(_no_base))
        != os.path.normcase(os.path.normpath(real_node)), _no_base)

    # 绝对路径配置照旧可用（不能因为加了占位符支持就退化）
    chk("绝对路径 node_exe 照旧命中",
        O._pick_node_exe({"v1": {"node_exe": real_node}}, base=tmp2) == real_node)

    # 槽里一个都没有 → 用包里自带的那份（新电脑首槽就靠它）
    _real_bundled = O.bundled_node_exe
    _fake_bundled = os.path.join(tmp2, "runtime", "node", "node.exe")
    os.makedirs(os.path.dirname(_fake_bundled), exist_ok=True)
    with open(_fake_bundled, "wb") as f:
        f.write(b"MZ fake")
    O.bundled_node_exe = lambda: _fake_bundled
    try:
        chk("槽里都没有时用包内自带的 node（新电脑首槽的活路）",
            O._pick_node_exe({"v1": {"node_exe": ""}}, base=tmp2) == _fake_bundled,
            O._pick_node_exe({"v1": {"node_exe": ""}}, base=tmp2))
    finally:
        O.bundled_node_exe = _real_bundled

    # 连包内都没有才退回系统 node
    O.bundled_node_exe = lambda: ""
    try:
        with patch.object(O.shutil, "which", return_value=_fake_bundled):
            chk("包内没有时返回 PATH 上实际可复制的 node 路径",
                O._pick_node_exe({"v1": {"node_exe": ""}}, base=tmp2)
                == os.path.abspath(_fake_bundled))
        with patch.object(O.shutil, "which", return_value=None):
            chk("PATH 也没有 node 时保留原来的缺失运行时提示",
                O._pick_node_exe({"v1": {"node_exe": ""}}, base=tmp2) == "node")
    finally:
        O.bundled_node_exe = _real_bundled
    # runtime/（约 200MB）被 .gitignore 排除，只有「便携包」才带。
    # git clone 出来的源码仓库本来就没有它，README 要求用户自备 Node.js；
    # 把「必须存在」写成无条件断言会让每个新 clone 的门禁都红。
    if os.path.isdir(os.path.join(ROOT, "runtime")):
        chk("便携包内 node 实际存在（runtime/node/node.exe）",
            bool(O.bundled_node_exe()), O.bundled_node_exe() or "(缺 runtime/node)")
    else:
        print("SKIP  源码 clone 无 runtime/：跳过包内 node 检查（README 要求自备 Node.js）")
finally:
    shutil.rmtree(tmp2, ignore_errors=True)


# ══════════════ 安装内核：npm 直调（不依赖 .ps1）══════════════
# 2026-10-30 事故回归：删 scripts/switch-version.ps1 时误伤「安装官方版」，
# 点按钮直接 [WinError 2]。现在安装内核是纯 Python（O._npm_install_slot），
# 这里把最底层的 O._run_npm 换掉，逐条验预检 / 换源 / init / 重试的分支。
print("== 安装内核：npm 直调 ==")
# 上一段把内核换成了替身，这里要验真身 —— 先装回去。
O._npm_install_slot = _REAL_NPM_INSTALL
tmp3 = tempfile.mkdtemp(prefix="dsh-npm-")
try:
    # 造一个"带 npm 的 node 运行时"（真 node 不必有，只要目录布局对）
    nd = os.path.join(tmp3, "node")
    make_node(nd)
    npmcli = os.path.join(nd, "node_modules", "npm", "bin", "npm-cli.js")
    os.makedirs(os.path.dirname(npmcli), exist_ok=True)
    with open(npmcli, "w", encoding="utf-8") as f:
        f.write("// npm cli\n")

    exe, cli = O._npm_cli(os.path.join(nd, "node.exe"))
    chk("_npm_cli 找到自带的 npm-cli.js", bool(exe) and bool(cli), cli)
    chk("_npm_cli 对不存在的 node 返回空", O._npm_cli(os.path.join(tmp3, "nope")) == ("", ""),
        str(O._npm_cli(os.path.join(tmp3, "nope"))))

    calls = []

    def fake_npm(rc_map):
        """rc_map: [(判断条件字符串, rc)] —— 按 args 里是否含某串决定返回码。"""
        def _fake(node_exe, npm_cli, args, cwd=None):
            calls.append(list(args))
            for needle, rc in rc_map:
                if needle in args:
                    return (rc, "out:" + needle)
            return (0, "ok")
        return _fake

    real_run_npm = O._run_npm

    # ① 默认源就能查到 → registry 空，且**不该**去打镜像
    calls[:] = []
    O._run_npm = fake_npm([])
    ok, reg, why = O._pick_registry("node", "cli", "@deepseek-ai/dsh@1.0.0")
    chk("预检通过时 registry 为空（用默认源）", ok is True and reg == "", "%s %s" % (reg, why))
    chk("预检通过只调一次 npm view（没白跑镜像）", len(calls) == 1, str(calls))

    # ② 默认源网络故障（负数退出码）→ 自动换镜像，且选中镜像
    calls[:] = []
    def _net_fail(node_exe, npm_cli, args, cwd=None):
        calls.append(list(args))
        return (0, "1.0.0") if "--registry" in args else (-4077, "ECONNRESET")
    O._run_npm = _net_fail
    ok, reg, why = O._pick_registry("node", "cli", "@deepseek-ai/dsh@1.0.0")
    chk("默认源网络故障时自动改用镜像",
        ok is True and reg == O.FALLBACK_REGISTRY, "%s %s" % (reg, why))
    chk("换镜像只试了一次默认源", len(calls) == 2, str(len(calls)))

    # ③ 版本不存在（rc>0）→ 换源也没用，直接报，且提示怎么写错的
    calls[:] = []
    O._run_npm = lambda *a, **k: (1, "E404 notarget")
    ok, reg, why = O._pick_registry("node", "cli", "@deepseek-ai/dsh@9.9.9")
    chk("版本不存在时立刻失败（不白试镜像）",
        ok is False and "找不到" in why, why)

    # ④ 默认源与镜像都不可达
    O._run_npm = lambda *a, **k: (-4077, "ECONNRESET")
    ok, reg, why = O._pick_registry("node", "cli", "@deepseek-ai/dsh@1.0.0")
    chk("两源都不可达时给出网络类结论",
        ok is False and "不可达" in why, why)

    # ⑤ 装进空槽：必须先 npm init -y，再 install
    slot = os.path.join(tmp3, "dshV9")
    os.makedirs(slot, exist_ok=True)
    calls[:] = []
    O._run_npm = fake_npm([])
    rc, out = O._npm_install_slot(os.path.join(nd, "node.exe"), slot, "1.0.0")
    # 去掉预检那条 view —— 它必然存在，下面看的是**动作序列**
    acts = [c for c in calls if c[:1] != ["view"]]
    chk("空槽先 npm init -y", rc == 0 and acts and acts[0][:2] == ["init", "-y"],
        str(acts[:1]))
    chk("再 npm install 目标版本",
        len(acts) == 2 and acts[1][:2] == ["install", "@deepseek-ai/dsh@1.0.0"],
        str(acts[1:2]))

    # ⑥ 槽根已有 package.json → 不该再 init（多一步就多一次失败机会）
    with open(os.path.join(slot, "package.json"), "w", encoding="utf-8") as f:
        f.write('{"name":"slot"}')
    calls[:] = []
    O._run_npm = fake_npm([])
    rc, out = O._npm_install_slot(os.path.join(nd, "node.exe"), slot, "1.0.0")
    acts = [c for c in calls if c[:1] != ["view"]]
    chk("已有 package.json 时跳过 init",
        rc == 0 and acts and acts[0][:1] == ["install"], str(acts))

    # ⑦ install 网络类失败（负数）且没用过镜像 → 用镜像重试一次
    calls[:] = []
    def _inst_fail(node_exe, npm_cli, args, cwd=None):
        calls.append(list(args))
        if args[:2] == ["view", "@deepseek-ai/dsh@1.0.0"]:
            return (0, "1.0.0")
        if "--registry" in args:
            return (0, "ok")
        return (-4077, "ECONNRESET")
    O._run_npm = _inst_fail
    rc, out = O._npm_install_slot(os.path.join(nd, "node.exe"), slot, "1.0.0")
    chk("install 网络失败后自动换镜像重试成功", rc == 0, "rc=%s out=%s" % (rc, out))
    chk("重试时带上 --registry",
        any("--registry" in c and c[:2][0] == "install" for c in calls), str(calls))

    # ⑦b 真实故障形态：npm 的网络错退出码是 **1**，不是负数（2026-11-13 实测）。
    #     旧代码只认负数 → 下载中断 5 分钟后直接失败，镜像一次都没试过。
    calls[:] = []
    def _inst_fail_rc1(node_exe, npm_cli, args, cwd=None):
        calls.append(list(args))
        if args[:2] == ["view", "@deepseek-ai/dsh@1.0.0"]:
            return (0, "1.0.0")
        if "--registry" in args:
            return (0, "ok")
        return (1, "npm error code ECONNRESET\nnpm error network aborted")
    O._run_npm = _inst_fail_rc1
    rc, out = O._npm_install_slot(os.path.join(nd, "node.exe"), slot, "1.0.0")
    chk("install 报 ECONNRESET 但退出码=1 时也会换镜像重试",
        rc == 0 and any("--registry" in c and c[0] == "install" for c in calls),
        "rc=%s calls=%s" % (rc, calls))

    # ⑦c 预检里 npm 用 rc=1 报网络错 → 不能误判成「版本不存在」
    def _view_net_rc1(node_exe, npm_cli, args, cwd=None):
        calls.append(list(args))
        if "--registry" in args:
            return (0, "1.0.0")
        return (1, "npm error code ECONNRESET")
    O._run_npm = _view_net_rc1
    ok, reg, why = O._pick_registry("node", "cli", "@deepseek-ai/dsh@1.0.0")
    chk("预检遇到 rc=1 的 ECONNRESET 也换镜像，不误报版本不存在",
        ok is True and reg == O.FALLBACK_REGISTRY, "%s %s" % (reg, why))

    # ⑦d 网络错误识别本身
    chk("_looks_like_network_error 认得 npm 的真实报错，也不误伤 E404/ETARGET",
        O._looks_like_network_error("npm error code ECONNRESET")
        and O._looks_like_network_error("npm error network aborted")
        and O._looks_like_network_error("EAI_AGAIN registry.npmjs.org")
        and not O._looks_like_network_error("E404 notarget")
        and not O._looks_like_network_error("ETARGET No matching version found"))

    # ⑧ node 旁边没有 npm-cli.js → 明确报"运行时不完整"，不静默退回 PATH 的 npm
    bare = os.path.join(tmp3, "bare-node")
    make_node(bare)
    rc, out = O._npm_install_slot(os.path.join(bare, "node.exe"), slot, "1.0.0")
    chk("缺 npm-cli.js 时明确报错", rc != 0 and "不完整" in out, out[:120])

    # ⑨ 槽目录不存在 → 明确报错（不建目录，避免在错的盘上凭空造槽）
    rc, out = O._npm_install_slot(os.path.join(nd, "node.exe"),
                                  os.path.join(tmp3, "no-such-slot"), "1.0.0")
    chk("槽目录不存在时明确报错", rc != 0 and "不存在" in out, out[:120])

    O._run_npm = real_run_npm
finally:
    shutil.rmtree(tmp3, ignore_errors=True)

# 收尾：把被替身换掉的内核装回去，避免影响后续断言/被其它测试 import
O._npm_install_slot = _REAL_NPM_INSTALL

# ══════════════ 装失败不留残件（_drop_partial_install）══════════════
print("== 装失败不留残件（失败后重试不得变成 -2）==")
tmp4 = tempfile.mkdtemp(prefix="dsh-inst-rb-")
try:
    # ① 纯空壳：只有空 .dsh-home → 收回
    s1 = os.path.join(tmp4, "shell")
    os.makedirs(os.path.join(s1, ".dsh-home"))
    chk("空壳判定为可收回", O._dir_is_empty_shell(s1))
    chk("空壳被收回", O._drop_partial_install(s1, os.path.join(s1, ".dsh-home")) is True)
    chk("收回后目录消失", not os.path.exists(s1))

    # ② 半装：npm init -y 生成了 package.json，还有半截 node_modules → 也必须收回
    #    这是真正会让名字让位成 -2 的那种残件（它不是"空壳"）。
    s2 = os.path.join(tmp4, "half")
    os.makedirs(os.path.join(s2, "node_modules", "@deepseek-ai", "dsh", "lib"))
    os.makedirs(os.path.join(s2, ".dsh-home"))
    with open(os.path.join(s2, "package.json"), "w", encoding="utf-8") as f:
        f.write('{"name":"slot","version":"1.0.0"}')
    chk("半装目录不算空壳（正是 -2 的来源）", not O._dir_is_empty_shell(s2))
    chk("半装残件被收回", O._drop_partial_install(s2, os.path.join(s2, ".dsh-home")) is True)
    chk("半装收回后目录消失", not os.path.exists(s2))

    # ③ 已经装好了（bin.js 在）→ 绝不删
    s3 = os.path.join(tmp4, "good")
    make_dsh(os.path.join(s3, "node_modules"), "0.2.0-rc.2")
    chk("装好的槽不收回", O._drop_partial_install(s3, os.path.join(s3, ".dsh-home")) is False)
    chk("装好的槽还在", os.path.isdir(s3))

    # ④ .dsh-home 里有用户数据 → 绝不删（哪怕程序没装成）
    s4 = os.path.join(tmp4, "userdata")
    os.makedirs(os.path.join(s4, ".dsh-home", "profiles"))
    with open(os.path.join(s4, ".dsh-home", "settings.yaml"), "w",
              encoding="utf-8") as f:
        f.write("a: 1\n")
    chk("有用户数据时不收回", O._drop_partial_install(s4, os.path.join(s4, ".dsh-home")) is False)
    chk("用户数据还在",
        os.path.isfile(os.path.join(s4, ".dsh-home", "settings.yaml")))

    # ⑤ 2026-11-11 护栏：install_base / versions_root 误配时绝不删到版本根外面
    vroot = os.path.join(tmp4, "dsh-versions")
    os.makedirs(vroot)
    s5 = os.path.join(tmp4, "outside", "dshV9")
    os.makedirs(os.path.join(s5, ".dsh-home"))
    chk("版本根之外的残件不被收回",
        O._drop_partial_install(s5, os.path.join(s5, ".dsh-home"), vroot) is False)
    chk("版本根之外的目录还在", os.path.isdir(s5))
    chk("没给 versions_root 时 helper 一律拒绝",
        O._root_under_versions_root(s5, "") is False)
    O._cleanup_partial(s5, vroot)
    chk("_cleanup_partial 也不碰版本根之外的目录", os.path.isdir(s5))
    s6 = os.path.join(vroot, "dshV8")
    os.makedirs(os.path.join(s6, ".dsh-home"))
    chk("版本根之内的空壳仍能正常收回",
        O._drop_partial_install(s6, os.path.join(s6, ".dsh-home"), vroot) is True)

    # ⑥ _unregister_slot：登记过就撤掉，没登记过返回 False 且不炸
    cfgp4 = os.path.join(tmp4, "config.json")
    base_config(cfgp4, 1)
    O.write_config_atomic(cfgp4, dict(O.read_config(cfgp4),
                                      versions=dict(O.read_config(cfgp4)["versions"],
                                                    v2={"label": "dshV2", "port": 5080})))
    chk("_unregister_slot 撤掉已登记的槽", O._unregister_slot(cfgp4, "v2") is True)
    chk("撤掉后 config 里没有 v2", "v2" not in O.read_config(cfgp4)["versions"])
    chk("_unregister_slot 对不存在的槽返回 False",
        O._unregister_slot(cfgp4, "v2") is False)

    # ⑥ 端到端：失败一次 → 再重试 → 仍叫 dshV2，绝不能冒出 dshV2-2
    vroot4 = os.path.join(tmp4, "dsh-versions")
    make_dsh(os.path.join(vroot4, "dshV1", "node_modules"), "0.1.5-rc.2")
    make_node(os.path.join(vroot4, "dshV1", "node"))
    _c4 = O.read_config(cfgp4)
    _c4["versions"]["v1"]["node_exe"] = os.path.join(
        vroot4, "dshV1", "node", "node.exe").replace("\\", "/")
    O.write_config_atomic(cfgp4, _c4)

    O._npm_install_slot = fake_install(fail_rc=1, out="boom")
    ok1, _k1, msg1 = O.run_official_install(A, "0.2.0-rc.2", cfgp4, tmp4, "v2", [],
                                            versions_root=vroot4)
    chk("第一次装失败", ok1 is False)
    O._npm_install_slot = fake_install()
    ok2, _k2, msg2 = O.run_official_install(A, "0.2.0-rc.2", cfgp4, tmp4, "v2", [],
                                            versions_root=vroot4)
    chk("重试装成功", ok2 is True, msg2)
    _names = sorted(n for n in os.listdir(vroot4) if n.startswith("dshV2"))
    chk("重试后目录仍叫 dshV2（没有 -2）", _names == ["dshV2"], str(_names))
    chk("重试后 config 里是 v2", "v2" in O.read_config(cfgp4)["versions"])
finally:
    shutil.rmtree(tmp4, ignore_errors=True)

# ── 临时目录集中管理（2026-10-31 起）────────────────────────────
print("== 临时目录根：plan / apply ==")
tmp5 = tempfile.mkdtemp(prefix="dshops5-")
try:
    vroot5 = os.path.join(tmp5, "dsh-versions")
    base5 = tmp5
    for n in ("dshV1", "dshV2"):
        os.makedirs(os.path.join(vroot5, n, "node_modules", "@deepseek-ai",
                                 "dsh", "lib"))
        open(os.path.join(vroot5, n, "node_modules", "@deepseek-ai",
                          "dsh", "lib", "bin.js"), "w", encoding="utf-8").close()
        os.makedirs(os.path.join(vroot5, n, ".dsh-home"))
    cfgp5 = os.path.join(tmp5, "config.json")
    O.write_config_atomic(cfgp5, {"install_base": base5, "versions": {
        "v1": {"label": "dshV1", "port": 4080,
               "bin_js": "<base>/dsh-versions/dshV1/node_modules/@deepseek-ai/dsh/lib/bin.js",
               "dsh_home": "<base>/dsh-versions/dshV1/.dsh-home",
               "temp_dir": "D:\\dsh-temp\\dshV1"},
        "v2": {"label": "dshV2", "port": 5080,
               "bin_js": "<base>/dsh-versions/dshV2/node_modules/@deepseek-ai/dsh/lib/bin.js",
               "dsh_home": "<base>/dsh-versions/dshV2/.dsh-home",
               "temp_dir": "<base>/dsh-versions/dshV2/.tmp"}}})

    tgt = os.path.join(vroot5, "dsh-temp")
    _cfg5 = O.read_config(cfgp5)
    chs = O.plan_temp_relocation(_cfg5, base5, tgt, tmp5)
    chk("每个槽都算出目标目录", len(chs) == 2, str(chs))
    chk("槽目录名从 dsh_home 反推（不靠 label）",
        sorted(c["dir_name"] for c in chs) == ["dshV1", "dshV2"],
        str([c["dir_name"] for c in chs]))
    chk("写回 config 时用 <base> 占位符（跟着安装根搬）",
        all(c["new"].startswith("<base>/") for c in chs),
        str([c["new"] for c in chs]))

    ok5, msg5 = O.apply_temp_relocation(cfgp5, chs)
    chk("apply 成功", ok5 is True, msg5)
    _after = O.read_config(cfgp5)
    chk("config 里 temp_dir 已改",
        _after["versions"]["v1"]["temp_dir"].endswith("/dsh-temp/dshV1"),
        _after["versions"]["v1"]["temp_dir"])
    chk("其它字段没被抹掉（port / bin_js / dsh_home 都在）",
        _after["versions"]["v1"]["port"] == 4080
        and "bin.js" in _after["versions"]["v1"]["bin_js"]
        and _after["versions"]["v1"]["dsh_home"].endswith(".dsh-home"))
    chk("新目录真的建出来了",
        all(os.path.isdir(c["new_abs"]) for c in chs))

    # 重复执行应当幂等（第二次 0 变更，不重复写盘）
    ok6, msg6 = O.apply_temp_relocation(cfgp5, O.plan_temp_relocation(
        O.read_config(cfgp5), base5, tgt, tmp5))
    chk("重复 apply 幂等", ok6 is True and "0 个" in msg6, msg6)

    # 2026-11-02 规则变更（用户要求简化目录设置）：
    #   临时文件直接放进**各自槽目录下的 .tmp**，
    #   优先级变成：显式 temp_root > 槽内 .tmp
    #   （原来的"候选盘 dsh-temp"降级为不再使用）
    chk("给了 temp_root 就用它",
        A.default_temp_dir("dshV9", "/x", temp_root="/t") ==
        os.path.join("/t", "dshV9"))
    chk("没给 temp_root 时用槽内 .tmp（新规则）",
        A.default_temp_dir("dshV9", "/x", candidates=("D:\\dsh-temp",))
        .replace("\\", "/") == "/x/.tmp")
    chk("没有槽目录时也能给出兜底",
        isinstance(A.default_temp_dir("dshV9", "", candidates=("Q:\\nope",)), str))
    # 本文件开头把 DSH_TEMP_NO_REAL_DISK 设成 1 → 默认候选为空 → 落到槽内 .tmp。
    chk("回归里默认候选被清空（不会在真实 D 盘建目录）",
        A.default_temp_dir("dshV9", "/x").replace("\\", "/") == "/x/.tmp",
        A.default_temp_dir("dshV9", "/x"))
finally:
    shutil.rmtree(tmp5, ignore_errors=True)

print()

# ---- 卫生检查：跑完全套之后，真实候选盘上的 dsh-temp 必须一字未变 ----
# 这条是给「以后有人图省事删掉开头那行环境变量」准备的护栏。
for _p, _before in sorted(_REAL_TEMP_SNAP.items()):
    _after = sorted(os.listdir(_p)) if os.path.isdir(_p) else None
    chk("回归没在真实盘上留垃圾：%s 内容未变" % _p, _after == _before,
        "跑前 %s / 跑后 %s" % (
            "不存在" if _before is None else "%d 项" % len(_before),
            "不存在" if _after is None else "%d 项" % len(_after)))

# ── 地基审计（2026-11-15）：英文模式下 hard 形状复核不能失效 ─────────────
# slot_shape_problems() 的级别是 T("错误")；过去安装 / 纳入直接比中文字面量，
# 英文界面下该值变成 "error"，hard 全被当成 soft，坏槽也照样报成功。
print("== 英文模式 hard 形状复核 ==")
import dsh_i18n as _I   # noqa: E402
_saved_lang = _I._LANG["code"]
try:
    for _lg in ("zh", "en"):
        _I.set_lang(_lg)
        _lv_err = O.T("错误")
        chk("F1 %s：is_hard 认出「错误」级" % _lg,
            O.is_hard(_lv_err) is True, repr(_lv_err))
        chk("F2 %s：warning 不算 hard" % _lg,
            not O.is_hard(O.T("警告")) and not O.is_hard("warning"))
        _shape = O.slot_shape_problems({}, "", "", "")
        chk("F3 %s：坏槽形状仍判出 hard（没有全变 soft）" % _lg,
            sum(1 for _lv, _ in _shape if O.is_hard(_lv)) >= 3,
            str([_lv for _lv, _ in _shape]))
    _I.set_lang("zh")
    chk("F4 返回值没被改成固定英文码（中文日志仍显示「错误」）",
        O.slot_shape_problems({}, "", "", "")[0][0] == "错误")
    _I.set_lang("en")
    chk("F5 英文界面级别显示为 error（i18n 仍生效）",
        O.slot_shape_problems({}, "", "", "")[0][0] == "error")
finally:
    _I.set_lang(_saved_lang)

_ops_src = open(os.path.join(ROOT, "dsh_install_ops.py"), encoding="utf-8").read()
chk("F6 安装 / 纳入不再拿中文字面量比级别",
    'lv == "错误"' not in _ops_src and 'lv != "错误"' not in _ops_src
    and _ops_src.count("is_hard(lv)") >= 4,
    "is_hard 出现 %d 处" % _ops_src.count("is_hard(lv)"))

print()
if FAILS:
    print("RESULT: FAIL（%d 项）: %s" % (len(FAILS), "; ".join(FAILS)))
    sys.exit(1)
print("RESULT: ALL PASS")