"""dsh-tm.py（CLI）静态回归。

为什么单独写：CLI 是**新增入口**，GUI 那套 test-manager-logic 只检查 dsh_manager.pyw。
CLI 一旦和 config 语义漂移（占位符、隔离环境、签名门控），后果是"脚本把版本启动到
错误的 DSH_HOME"，比界面坏了更隐蔽。

全部只读：解析源码 + 用假 config 跑纯函数，不启动任何进程、不写盘。
"""
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, os.pardir))
CLI = os.path.join(REPO, "dsh-tm.py")

FAIL = 0
N = 0


def chk(name, cond, detail=""):
    global FAIL, N
    N += 1
    if cond:
        print("PASS  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s%s" % (name, (" | " + detail) if detail else ""))


if not os.path.exists(CLI):
    chk("A1 存在 dsh-tm.py", False, CLI)
    print("\n== test-cli: %d 项，FAIL=%d ==" % (N, FAIL))
    sys.exit(1)

src = io.open(CLI, encoding="utf-8").read()

print("== A. 结构与独立性 ==")
chk("A1 存在 dsh-tm.py", True)
chk("A2 不导入 dsh_manager.pyw（GUI 模块级会建窗口，CLI 必须无图形依赖）",
    "import dsh_manager" not in src and "from dsh_manager" not in src)
chk("A3 不导入 tkinter", "tkinter" not in src)
chk("A4 只复用纯逻辑模块",
    "dsh_kill" in src and "dsh_health" in src)
chk("A5 有 __main__ 入口且返回退出码",
    '__name__ == "__main__"' in src and "sys.exit(main())" in src)

print()
print("== B. 与 GUI 同一套配置语义 ==")
chk("B1 读同一份 config.json", 'CONFIG_PATH = os.path.join(BASE_DIR, "config.json")' in src)
chk("B2 支持全部四个占位符",
    all(t in src for t in ("<base>", "<home>", "<self>", "<npm_global>")))
chk("B3 读 config 用 utf-8-sig（容忍 BOM）", "utf-8-sig" in src)
# 2026-11-01：上限会演进（3→4→5→6），断言改成"两个文件的数值一致"，
# 而不是写死某个数字 —— 写死的话每次调上限都要改测试，改着改着就懒得改、门禁失效。
_gui_src = io.open(os.path.join(REPO, "dsh_manager.pyw"), encoding="utf-8").read()
import re as _re
_cli_max = _re.search(r"^MAX_SLOTS = (\d+)", src, _re.M)
_gui_max = _re.search(r"^MAX_SLOTS = (\d+)", _gui_src, _re.M)
chk("B4 槽上限 CLI 与 GUI 一致（MAX_SLOTS）",
    bool(_cli_max and _gui_max and _cli_max.group(1) == _gui_max.group(1)),
    "cli=%s gui=%s" % (_cli_max.group(1) if _cli_max else "?",
                       _gui_max.group(1) if _gui_max else "?"))
chk("B5 每槽注入 DSH_HOME（绝不能读串数据）", 'env["DSH_HOME"]' in src)
chk("B6 每槽注入 TEMP/TMP", 'env["TEMP"]' in src and 'env["TMP"]' in src)
chk("B7 启动参数取自 config 的 extra_args",
    "extra_args" in src and "_build_start_cmd" in src)

print()
print("== C. 安全边界（与 GUI 同一条门控） ==")
chk("C1 停止走 dsh_kill.kill_version（签名 + 端口门控，不误杀别的 node）",
    "kill_version" in src)
chk("C2 签名取 bin_js 里 /node_modules/ 之前那段（与 GUI 的 _make_sig 同义）",
    "/node_modules/" in src and "_make_sig" in src)
chk("C3 不自己 taskkill / tasklist（必须复用门控模块）",
    "taskkill" not in src and "tasklist" not in src)
chk("C4 边界说明写进帮助（--all 只作用于本管理器管的槽）",
    "本管理器" in src or "--all" in src)
chk("C5 kill-stray 默认只打印计划，必须 --yes 才执行",
    '"kill-stray"' in src and '"--yes"' in src and 'getattr(args, "yes", False)' in src)
chk("C6 kill-stray 用 SIG_ANY 只认 @deepseek-ai/dsh（不误杀别的 node）",
    '"@deepseek-ai/dsh"' in src and "kill_all" in src)

print()
print("== D. 实跑：纯函数（假 config，不启动进程） ==")
sys.path.insert(0, REPO)
import importlib.util

spec = importlib.util.spec_from_file_location("dsh_tm_cli", CLI)
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
    loaded = True
except Exception as e:
    loaded = False
    chk("D0 模块可加载", False, str(e))

if loaded:
    chk("D0 模块可加载", True)
    fake = {
        "install_base": "<home>",
        "versions": {
            "v1": {"label": "dshV1", "port": 4080,
                   "node_exe": "<base>/dsh-versions/dshV1/node/node.exe",
                   "bin_js": "<base>/dsh-versions/dshV1/node_modules/@deepseek-ai/dsh/lib/bin.js",
                   "dsh_home": "<base>/dsh-versions/dshV1/.dsh-home",
                   "temp_dir": "<base>/dsh-versions/dsh-temp/dshV1"},
        },
    }
    d = tempfile.mkdtemp(prefix="dsh-cli-test-")
    cfg_path = os.path.join(d, "config.json")
    with io.open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(fake, f)

    cfg, err = mod.load_config(cfg_path)
    chk("D1 load_config 能读临时 config", cfg is not None and not err, err)

    ctx = mod.Ctx(cfg)
    chk("D2 install_base 展开成主目录",
        ctx.install_base == os.path.normpath(os.path.expanduser("~")),
        ctx.install_base)
    slot = ctx.slot("v1")
    chk("D3 <base> 占位符展开到 node_exe",
        slot["node_exe"].startswith(os.path.normpath(os.path.expanduser("~"))),
        slot["node_exe"])
    chk("D4 展开后不含残留占位符",
        not any(t in v for v in slot.values() if isinstance(v, str)
                for t in ("<base>", "<home>", "<self>")))
    chk("D5 slot 还原的是副本（改它不影响 ctx.versions）",
        (slot.__setitem__("port", 1) or True) and ctx.versions["v1"]["port"] == 4080)

    cmd = mod._build_start_cmd(ctx, "v1")
    chk("D6 缺省启动参数是 web --host --port --no-open",
        cmd[2] == "web" and "--port" in cmd and "--no-open" in cmd, str(cmd))
    chk("D7 命令第一个是 node_exe", cmd[0].endswith("node.exe"), cmd[0])

    env = mod._slot_env(ctx, "v1")
    chk("D8 环境里 DSH_HOME 指向该槽", env.get("DSH_HOME") == slot["dsh_home"])
    chk("D9 环境里 TEMP 指向该槽", env.get("TEMP") == slot["temp_dir"])

    # 坏配置要给出明确错误而不是崩栈
    bad = os.path.join(d, "bad.json")
    with io.open(bad, "w", encoding="utf-8") as f:
        f.write("{not json")
    c2, e2 = mod.load_config(bad)
    chk("D10 坏 JSON 返回错误字符串而不是抛异常", c2 is None and "解析失败" in e2, e2)

    missing = os.path.join(d, "nope.json")
    c3, e3 = mod.load_config(missing)
    chk("D11 缺文件返回明确错误", c3 is None and "找不到配置文件" in e3, e3)

    # 未知槽要返回 None，而不是 KeyError
    chk("D12 未知槽返回 None", ctx.slot("v9") is None)

    # --all 展开
    class _A(object):
        all = True
        slot = []
    keys = mod._target_keys(ctx, _A())
    chk("D13 --all 展开成全部槽键", keys == ["v1"], str(keys))

    class _B(object):
        all = False
        slot = ["v1"]
    chk("D14 指定槽名有效", mod._target_keys(ctx, _B()) == ["v1"])

    import shutil
    shutil.rmtree(d, ignore_errors=True)

print()
# 与其它回归脚本同一套收尾格式：check-all.ps1 只从以 RESULT 开头的行里取摘要，
# 少了这一行，总检会打出一个空的「+ 」（退出码仍是 0，但看着像没跑）。
print("RESULT: %s（%d 项，FAIL=%d）" % ("ALL PASS" if not FAIL else "FAILED", N, FAIL))
sys.exit(1 if FAIL else 0)
