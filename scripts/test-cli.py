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
chk("B1 读同一份 config.json", 'CONFIG_PATH = PLATFORM.default_config_path()' in src)
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
chk("B5 每槽注入 DSH_HOME（绝不能读串数据）", "PLATFORM.slot_env(" in src)
chk("B6 每槽注入 TEMP/TMP", "PLATFORM.slot_env(" in src)
chk("B7 启动参数取自 config 的 extra_args",
    "extra_args" in src and "_build_start_cmd" in src)

print()
print("== C. 安全边界（与 GUI 同一条门控） ==")
chk("B8 缺 config 时 CLI 从模板自动生成（与 GUI 行为一致）",
    "EXAMPLE_PATH" in src and "PLATFORM.bootstrap_config(path, EXAMPLE_PATH)" in src)


chk("C1 停止走共享进程门控（签名 + 端口门控，不误杀别的 node）",
    "SLOT_PROCESS.stop_slot(" in src)
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

    # 坏配置要给出明确错误而不是崩栈。
    # 2026-11-15 地基修复：下面两条断言查的是中文报错文案，以前依赖**本机**
    # config.json 的 ui.lang —— 用户切到英文界面后这里会假红，而 CI 用零槽模板
    # （无 ui.lang）永远是 zh，于是「本地红、云端绿」。回归必须钉住语言。
    import dsh_i18n as _I18N
    _saved_lang = _I18N.lang()
    _I18N.set_lang("zh")

    bad = os.path.join(d, "bad.json")
    with io.open(bad, "w", encoding="utf-8") as f:
        f.write("{not json")
    c2, e2 = mod.load_config(bad)
    chk("D10 坏 JSON 返回错误字符串而不是抛异常", c2 is None and "解析失败" in e2, e2)

    missing = os.path.join(d, "nope.json")
    c3, e3 = mod.load_config(missing)
    chk("D11 缺文件返回明确错误", c3 is None and "找不到配置文件" in e3, e3)
    _I18N.set_lang(_saved_lang)

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

# ── E. --json（机器可读输出，2026-11-14）──────────────────────────────
# 目的：让另一个 agent 能驱动 dsh-tm。给人看的对齐文本 agent 解析起来很痛苦。
print("== E. --json 机器可读输出 ==")
_src_cli = io.open(CLI, encoding="utf-8").read()
chk("E1 CLI 提供 --json 参数", 'p.add_argument("--json"' in _src_cli)
chk("E2 顶层带 schema_version", "SCHEMA_VERSION = 1" in _src_cli)
# 八个命令都要有 JSON 分支（2026-11-15 补 start / stop / kill-stray，
# 2026-11-15 再加 doctor）
_n_json = _src_cli.count('getattr(args, "json", False)')
chk("E3 八个命令都有 JSON 分支（含写操作与 doctor）",
    _n_json >= 7 and "_json = getattr(args, \"json\", False)" in _src_cli
    and "def cmd_doctor(" in _src_cli,
    "%d 处" % _n_json)
# 字段名与枚举值必须是英文，不跟 --lang 漂移
chk("E4 level 归一成英文枚举",
    mod._lv_en("错误") == "error" and mod._lv_en("警告") == "warning"
    and mod._lv_en("提示") == "info",
    "%s / %s / %s" % (mod._lv_en("错误"), mod._lv_en("警告"), mod._lv_en("提示")))
chk("E5 英文 level 也认（界面语言切换后不炸）",
    mod._lv_en("error") == "error" and mod._lv_en("warning") == "warning")
# 选槽不能往 stderr 写东西；url / health 的两个分支必须共用同一个选槽函数
# （2026-11-15 修：以前非 JSON 分支用了只在 JSON 分支赋值的 keys → 必崩）
chk("E6 选槽统一走 _selected_keys / _json_target_keys",
    "def _selected_keys(" in _src_cli
    and _src_cli.count("_selected_keys(ctx, args)") >= 2
    and "def _json_target_keys(" in _src_cli)
# `list --json` 比 `--json list` 自然，argparse 只认后者，所以有 argv 重排
chk("E7 --json / --lang 可写在子命令之后（argv 重排）",
    "_pre + _argv" in _src_cli and 'if _a == "--json"' in _src_cli)
# 槽快照字段固定
chk("E8 槽快照字段固定（key/label/port/state/version）",
    all(('"%s"' % f) in _src_cli for f in ("key", "label", "port", "state", "version")))
chk("E9 _emit 补齐公共顶层字段（所有命令同形）",
    'setdefault("count"' in _src_cli and 'setdefault("errors"' in _src_cli)
chk("E10 JSON 模式等端口不往 stdout 打 WARN",
    "quiet=True" in _src_cli and "def _wait_until_listening" in _src_cli)
chk("E11 写操作不默认全开，缺目标 / 未知槽给固定英文枚举",
    '"no_target"' in _src_cli and '"unknown_slot"' in _src_cli)

# ── F. 实跑：契约与两个分支 bug（2026-11-15）─────────────────────────
# 假配置刻意选 59999 端口 + 不存在的 bin.js：start 会失败但不会启动任何
# 进程，stop 也不会杀任何进程；kill-stray 不带 --yes 只出计划。零副作用。
print()
print("== F. 实跑：--json 契约与非 JSON 分支 ==")
import subprocess as _sp

_fdir = tempfile.mkdtemp(prefix="dsh-cli-api-")
_fcfg = os.path.join(_fdir, "config.json")
_fake = {"install_base": "<self>", "versions": {
    "v1": {"label": "fake", "port": 59999,
           "node_exe": "<self>/__no_such__/node.exe",
           "bin_js": "<self>/__no_such__/node_modules/@deepseek-ai/dsh/lib/bin.js",
           "dsh_home": "<self>/__no_such__/home",
           "temp_dir": "<self>/__no_such__/tmp"}}}
with io.open(_fcfg, "w", encoding="utf-8") as _f:
    json.dump(_fake, _f)
_fenv = dict(os.environ)
_fenv["PYTHONPATH"] = REPO + os.pathsep + _fenv.get("PYTHONPATH", "")


def _run(*argv):
    # 用 `-c` 显式注入 REPO 到 sys.path：Python embed 便携版（python311._pth）
    # 会忽略 PYTHONPATH 和脚本所在目录，直接 `python dsh-tm.py` 会找不到
    # dsh_i18n。CI 用官方发行版没这个问题，但回归脚本不该挑解释器。
    # sys.argv / __file__ 都按真实调用模拟，行为与直接跑脚本一致。
    _full = ["--config", _fcfg] + list(argv)
    _code = ("import sys, runpy; "
             "sys.path.insert(0, %r); "
             "sys.argv = [%r] + %r; "
             "runpy.run_path(%r, run_name='__main__')"
             % (REPO, CLI, _full, CLI))
    _p = _sp.run([sys.executable, "-c", _code],
                 cwd=REPO, env=_fenv, capture_output=True, text=True,
                 encoding="utf-8", errors="replace", timeout=60)
    return _p.returncode, _p.stdout, _p.stderr


# url / health 的非 JSON 分支以前必崩（UnboundLocalError: keys）；doctor 一并守着
for _c in ("url", "health", "doctor"):
    _rc, _out, _err = _run(_c)
    chk("F1 %s 不带 --json 不再崩" % _c,
        "Traceback" not in _err and "UnboundLocalError" not in _err,
        _err[-140:])

_cases = [("list", "--json"), ("status", "--json"), ("url", "--json"),
          ("health", "--json"), ("doctor", "--json"),
          ("start", "v1", "--json", "--wait", "0"),
          ("stop", "v1", "--json"), ("kill-stray", "--json")]
_need = ("schema_version", "command", "ok", "count", "slots", "errors")
_bad = []
for _a in _cases:
    try:
        _rc, _out, _err = _run(*_a)
        _o = json.loads(_out)       # stdout 有多个 JSON 对象时这里会失败
        if not (all(k in _o for k in _need)
                and _o["schema_version"] == mod.SCHEMA_VERSION
                and isinstance(_o["slots"], list)
                and _o["command"] == _a[0]):
            _bad.append(" ".join(_a) + ":字段")
    except Exception as _e:
        _bad.append(" ".join(_a) + ":" + str(_e)[:60])
chk("F2 八个命令 --json 都是单个 JSON、顶层字段齐", not _bad, "; ".join(_bad[:3]))

_rc, _out, _ = _run("start", "--json")
_o = json.loads(_out)
chk("F3 写操作缺目标 → no_target + 退出码 2",
    _rc == mod.EXIT_USAGE and _o["ok"] is False and _o.get("error") == "no_target")
_rc, _out, _ = _run("stop", "v9", "--json")
_o = json.loads(_out)
chk("F4 未知槽 → unknown_slot", _o.get("error") == "unknown_slot")
_rc, _out, _ = _run("kill-stray", "--json")
_o = json.loads(_out)
chk("F5 kill-stray 不带 --yes 只出计划（executed=false）",
    _o.get("executed") is False and _o.get("requires_confirm") is True)
_rc, _out, _ = _run("start", "v1", "--json", "--wait", "0")
_o = json.loads(_out)
_r = (_o.get("slots") or [{}])[0]
chk("F6 写操作失败项带固定英文 error_code",
    _o["ok"] is False and _r.get("error_code") in ("spawn_failed", "port_not_listening")
    and (_o.get("errors") or [{}])[0].get("code") == _r.get("error_code"))
_rc, _out, _ = _run("--lang", "en", "list", "--json")
_o = json.loads(_out)
chk("F7 --lang en 不改字段名", all(k in _o for k in _need))
_rc, _out, _ = _run("doctor", "--json")
_o = json.loads(_out)
chk("F8 doctor --json 带 level / config_issues / actions（不只是自由文本）",
    "level" in _o and "config_issues" in _o
    and all("actions" in s for s in _o["slots"]))
_rc, _out, _ = _run("list", "--json")
chk("F9 所有 --json 输出都带 config_problems（配置可信度不再靠猜）",
    "config_problems" in json.loads(_out))

import shutil as _sh
_sh.rmtree(_fdir, ignore_errors=True)

print()
# 与其它回归脚本同一套收尾格式：check-all.ps1 只从以 RESULT 开头的行里取摘要，
# 少了这一行，总检会打出一个空的「+ 」（退出码仍是 0，但看着像没跑）。
print("RESULT: %s（%d 项，FAIL=%d）" % ("ALL PASS" if not FAIL else "FAILED", N, FAIL))
sys.exit(1 if FAIL else 0)
