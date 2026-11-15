# -*- coding: utf-8 -*-
"""配置层校验：CLI / GUI / doctor 共用的**唯一判据**（纯逻辑、只读、无副作用）。

为什么单独拆一个模块（2026-11-15 地基审计）
--------------------------------------------
同一份 config.json 以前被三个入口用三种信任程度读：

  * GUI 在模块加载时跑 `_check_port_consistency()`，port 与 extra_args --port
    不一致直接 fatal；
  * CLI 完全没有这道校验，而 0.1.5 的 `start --json` / `list --json` 正是拿
    `port` 字段判断 `state` / `listening` —— 配置一错就会稳定误报；
  * CLI 的 `_cfg_or_die()` 还把**类型写坏的** `versions` 静默改成 `{}`，
    于是「配置损坏」被伪装成「这台机器没装任何版本」，agent 会据此下错判断。

现在三处都调本模块。判据只有一份，GUI 与 CLI 不可能再各说各话。

分两级
------
  * **blocking**（无法安全枚举槽）：顶层不是对象、versions 缺失/类型错、
    单个槽不是对象、槽数超上限。CLI 直接结构化报错退出 3；
  * **非 blocking**（槽能列出来，但端口不可信）：port 缺失/非法、
    extra_args 类型错、缺 --port、--port 值非法、port 与 --port 不一致、
    两槽抢同一端口。只读命令照常出报告（并带 `config_problems`），
    **写操作拒绝执行这些槽**，doctor 负责把问题和建议动作讲清楚。

`detail` 是给人看的自由文本（随 --lang 变），`code` / `key` / `level` /
`blocking` 是固定英文结构化字段，agent 只认后者。
"""
from dsh_i18n import T

LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"

# 只有这五种会导致「连槽表都没法安全读出来」，必须阻断。
BLOCKING_CODES = frozenset((
    "config_not_object", "versions_missing", "versions_not_object",
    "slot_not_object", "too_many_slots",
))

# GUI 的 `_check_port_consistency()` 历史上只报这四类；抽到共用模块后
# 仍保持同样口径，避免界面突然多出一堆和「端口双源」无关的报错。
PORT_CODES = frozenset((
    "port_arg_missing", "port_arg_invalid", "port_mismatch",
    "extra_args_not_list",
))

_MISSING = object()


def _p(code, key, level, detail, **extra):
    d = {"code": code, "key": key, "level": level,
         "blocking": code in BLOCKING_CODES, "detail": detail}
    d.update(extra)
    return d


def _coerce_port(value):
    """把 config 里的 port 归一化成 int。返回 (int|None, 问题码|None)。"""
    if value is None or value == "":
        return None, "port_missing"
    if isinstance(value, bool):          # bool 是 int 的子类，但 True 不是端口
        return None, "port_invalid"
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and value.strip().isdigit():
        n = int(value.strip())
    else:
        return None, "port_invalid"
    if not (1 <= n <= 65535):
        return None, "port_invalid"
    return n, None


def effective_port(extra_args):
    """从 extra_args 里读**真正会生效**的 --port。返回 (int|None, 问题码|None)。

    `_MISSING` / None 表示配置没写 extra_args：CLI 的 `_build_start_cmd()` 会
    按 `port` 字段合成一条带 --port 的默认参数，属于「由构造保证一致」，不报错。
    空列表则不同 —— 它是显式写了、却没带 --port，必须报缺项。

    同时认 `--port 4080` 与 `--port=4080` 两种写法（GUI 老实现只认前者）。
    """
    if extra_args is _MISSING or extra_args is None:
        return None, "default"
    if not isinstance(extra_args, (list, tuple)):
        return None, "not_a_list"
    for i, a in enumerate(extra_args):
        s = str(a)
        raw = None
        if s == "--port":
            if i + 1 >= len(extra_args):
                return None, "invalid"
            raw = extra_args[i + 1]
        elif s.startswith("--port="):
            raw = s.split("=", 1)[1]
        if raw is None:
            continue
        try:
            n = int(str(raw).strip())
        except (TypeError, ValueError):
            return None, "invalid"
        if not (1 <= n <= 65535):
            return None, "invalid"
        return n, None
    return None, "missing"


def validate_config(cfg, max_slots=6):
    """校验整份 config，返回结构化问题列表（空 = 干净）。**绝不修改 cfg**。"""
    out = []
    if not isinstance(cfg, dict):
        return [_p("config_not_object", None, LEVEL_ERROR,
                   T("config.json 顶层必须是对象"))]

    versions = cfg.get("versions", _MISSING)
    if versions is _MISSING or versions is None:
        return [_p("versions_missing", None, LEVEL_ERROR,
                   T("config.json 里没有 versions 段（无法区分零槽和配置被写坏）"))]
    if not isinstance(versions, dict):
        return [_p("versions_not_object", None, LEVEL_ERROR,
                   T("versions 必须是对象，实际是 %s（拒绝静默当成零槽）")
                   % type(versions).__name__)]

    if len(versions) > max_slots:
        out.append(_p("too_many_slots", None, LEVEL_ERROR,
                      T("config.json 里有 %d 个槽，超过上限 %d")
                      % (len(versions), max_slots)))

    seen_ports = {}
    for key, raw in versions.items():
        if not isinstance(raw, dict):
            out.append(_p("slot_not_object", key, LEVEL_ERROR,
                          T("槽配置必须是对象，实际是 %s") % type(raw).__name__))
            continue

        want, pcode = _coerce_port(raw.get("port"))
        if pcode == "port_missing":
            out.append(_p("port_missing", key, LEVEL_ERROR,
                          T("槽没配 port（状态探测无法确定端口）")))
        elif pcode == "port_invalid":
            out.append(_p("port_invalid", key, LEVEL_ERROR,
                          T("port 非法：%s（应为 1–65535 的整数）")
                          % repr(raw.get("port"))))
        elif isinstance(raw.get("port"), str):
            out.append(_p("port_is_string", key, LEVEL_WARNING,
                          T("port 写成了字符串 %s（能跑但易出错，建议改成整数）")
                          % repr(raw.get("port"))))

        if want is not None:
            if want in seen_ports:
                out.append(_p("duplicate_port", key, LEVEL_ERROR,
                              T("和 %s 抢同一个端口 %s（两个槽不能共用）")
                              % (seen_ports[want], want),
                              conflicts_with=seen_ports[want], port=want))
            else:
                seen_ports[want] = key

        got, acode = effective_port(raw.get("extra_args", _MISSING))
        if acode == "not_a_list":
            out.append(_p("extra_args_not_list", key, LEVEL_ERROR,
                          T("extra_args 必须是数组，实际是 %s")
                          % type(raw.get("extra_args")).__name__))
        elif acode == "missing":
            out.append(_p("port_arg_missing", key, LEVEL_ERROR,
                          T("extra_args 里没有 --port（实际监听端口无法确定）")))
        elif acode == "invalid":
            out.append(_p("port_arg_invalid", key, LEVEL_ERROR,
                          T("extra_args 里 --port 的值非法或超出 1–65535")))
        elif acode is None and want is not None and got != want:
            out.append(_p("port_mismatch", key, LEVEL_ERROR,
                          T("port=%d 但 extra_args --port=%d（实际会起在 %d）")
                          % (want, got, got), expected=want, actual=got))
    return out


def fatal(problems):
    """阻断级问题：拿不到可信的槽表，命令不能继续。"""
    return [p for p in (problems or []) if p.get("blocking")]


def port_problems(versions):
    """GUI 老接口形状：`[(槽键, 说明), ...]`，只报端口双源相关的四类。

    保留这个形状是为了让 `dsh_manager.pyw` 的 `_fatal()` 文案与
    `scripts/test-manager-logic.py` B1–B9 不用改；判据本身已搬到这里。
    """
    return [(p["key"], p["detail"])
            for p in validate_config({"versions": versions or {}})
            if p["code"] in PORT_CODES]
