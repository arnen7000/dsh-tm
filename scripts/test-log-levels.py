"""日志分级规则测试：验证 _log_tag 的判定符合"错误>操作>警告>细节>普通"。"""
import io
import os
import re
import sys

# 不能写死本机路径：clone 到别的盘符/目录后，总检会直接 FileNotFoundError。
# 一律从脚本自身位置反推仓库根。
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = io.open(os.path.join(REPO, "dsh_manager.pyw"), encoding="utf-8").read()

# 抽出需要的常量与函数
seg_start = src.find("_ERR_WORDS = (")
seg_end = src.find("def _gui_sink_impl(")
assert seg_start > 0 and seg_end > seg_start
seg = src[seg_start:seg_end]

# 抽出来的代码段里现在有 `_MAIN_TID = threading.get_ident()`（GUI sink 的
# 主线程判定），exec 时需要把 threading 也注进去。
ns = {"threading": __import__("threading")}
exec(compile(seg, "<logseg>", "exec"), ns)
tag = ns["_log_tag"]

CASES = [
    # (行内容, 期望 tag, 说明)
    ("[2026-11-02 07:00:00] [v1] 状态变化: 运行中 pid=1 端口=4080", "plain", "槽状态=普通"),
    ("[2026-11-02 07:00:00] [环境] [OK] Python 3.10.21", "plain", "环境自检=普通"),
    ("[2026-11-02 07:00:00] [环境] [注意] 未装 pywin32", "warn", "注意=警告"),
    ("[2026-11-02 07:00:00] [变更] 已记录 3 个槽的插件与配置基线", "plain", "变更=普通(不再彩色)"),
    ("[2026-11-02 07:00:00] [设置] 已保存界面偏好", "user", "设置=用户操作(蓝)"),
    ("[2026-11-02 07:00:00] [安装] 开始：官方版 0.2.1", "user", "安装=用户操作(蓝)"),
    ("[2026-11-02 07:00:00] [纳入] 成功：v1", "user", "纳入=用户操作(蓝)"),
    ("[2026-11-02 07:00:00] [卸载] 完成", "user", "卸载=用户操作(蓝)"),
    ("[2026-11-02 07:00:00] [主题] 已切换为：深色", "user", "主题=用户操作(蓝)"),
    ("[2026-11-02 07:00:00] [全部启动] 已发出 3 个启动指令", "user", "批量=用户操作(蓝)"),
    ("[2026-11-02 07:00:00] [kill] 端口=4080 命中 1 个 dsh 进程", "detail", "kill=细节(紫)"),
    ("[2026-11-02 07:00:00] [权限] 能力实测：读命令行=可用", "detail", "权限=细节(紫)"),
    ("[2026-11-02 07:00:00] [检测] 本机没有发现新的 dsh", "detail", "检测=细节(紫)"),
    ("[2026-11-02 07:00:00] [v1] 启动失败：端口被占用", "err", "含失败=错误(红)，优先于一切"),
    ("[2026-11-02 07:00:00] [安装] 失败：npm 退出码 -4077", "err", "操作失败仍是错误"),
    ("[2026-11-02 07:00:00] [卸载] 未完成：备份校验失败", "err", "未完成+失败=错误"),
]

fail = 0
print("=== 日志分级判定 ===")
for line, want, why in CASES:
    got = tag(line)
    ok = got == want
    if not ok:
        fail += 1
    print(("PASS  " if ok else "FAIL  ") + "%-52s %-7s %s"
          % (line.split("] ", 1)[1][:52], got, why))

print()
print("=== 深色配色是否都有定义 ===")
pal = ns["_LOG_COLORS"]["dark"]
for k in ("err", "warn", "user", "detail", "plain"):
    v = pal.get(k, "")
    print("  %-7s %s %s" % (k, v, "OK" if re.match(r"^#[0-9A-Fa-f]{6}$", v) else "BAD"))
    if not re.match(r"^#[0-9A-Fa-f]{6}$", v):
        fail += 1

print()
print("FAIL = %d / %d" % (fail, len(CASES) + 5))
sys.exit(1 if fail else 0)
