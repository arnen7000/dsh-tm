# -*- coding: utf-8 -*-
"""离线回归：守护中英双语的完整性。

三条底线，每条都对应一次真实踩坑：

  1. **源码里每个 `T("中文")` 都要在 `lang/en.json` 里有译文。**
     漏一条不会报错，只会在英文界面里冒出一句中文 —— 靠人肉发现，2026-11-14
     就因此被用户连着抓到三轮（按钮、状态行、日志、环境检查）。

  2. **译文里不能出现字面 `/n`。**
     `\n` 写坏成 `/n` 时，界面会原样显示 `version:/n/n  · v2 ...`。
     这类错误肉眼在 JSON 里极难看出，必须让机器查。

  3. **译文不能是空串。** 空串会让那一处直接显示空白，比回退中文更糟。

跑法：python scripts/test-i18n.py    （退出码 0 = 全过，1 = 有失败）
"""
import ast
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, os.pardir))
EN_PATH = os.path.join(REPO, "lang", "en.json")

CJK = re.compile(r"[\u4e00-\u9fa5]")

# 这些**故意不翻**，出现在源码里但没有英文译文是正常的：
#   · dsh备份 —— 磁盘上真实的目录名，翻了路径就错
ALLOW_MISSING = {"dsh备份"}

FAILS = []


def chk(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + ((" | " + str(detail)) if detail else ""))
    if not ok:
        FAILS.append(name)


def collect_keys():
    """源码里所有 `T("...")` 的中文 key（含 scripts/ 下的独立脚本）。"""
    keys = set()
    files = []
    for name in os.listdir(REPO):
        if name.endswith((".py", ".pyw")) and name.startswith("dsh"):
            files.append(os.path.join(REPO, name))
    scripts = os.path.join(REPO, "scripts")
    for name in os.listdir(scripts):
        if name.endswith(".py"):
            files.append(os.path.join(scripts, name))
    for path in files:
        try:
            src = io.open(path, encoding="utf-8-sig").read()
            tree = ast.parse(src)
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
               and node.func.id == "T" and node.args:
                a = node.args[0]
                if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                   and CJK.search(a.value):
                    keys.add(a.value)
    return keys


def main():
    try:
        en = json.load(io.open(EN_PATH, encoding="utf-8-sig"))
    except Exception as e:
        chk("A1 en.json 可解析", False, e)
        print("\nRESULT: FAIL（1 项）")
        return 1
    chk("A1 en.json 可解析", isinstance(en, dict), "%d 条" % len(en))

    keys = collect_keys()
    chk("A2 源码里有可检查的文案", len(keys) > 300, "%d 条" % len(keys))

    # ① 覆盖率
    missing = sorted(k for k in keys if k not in en and k not in ALLOW_MISSING)
    chk("A3 每个 T() 中文 key 都有英文译文", not missing,
        "缺 %d 条：%s" % (len(missing), "；".join(m[:28] for m in missing[:5])))

    # ② 字面 /n（换行写坏）
    #    只查「该有换行却写成 /n」的：原文有 n 个换行、译文换行数对不上
    bad_nl = []
    for k, v in en.items():
        if "/n" not in v or not isinstance(k, str):
            continue
        # [Y/n]、node_modules/npm/... 这类正常斜杠：译文换行数已够，不算坏
        if k.count("\n") <= v.count("\n"):
            continue
        bad_nl.append(k[:40])
    chk("A4 没有把换行写成字面 /n", not bad_nl,
        "%d 条：%s" % (len(bad_nl), "；".join(bad_nl[:5])))

    # ③ 空译文
    empty = [k for k, v in en.items() if not (v or "").strip()]
    chk("A5 没有空译文", not empty, "%d 条" % len(empty))

    # ④ en.json 里不许有孤立的中文（说明漏翻了）
    still_cn = [k for k, v in en.items()
                if isinstance(v, str) and CJK.search(v) and v != k]
    chk("A6 译文里没有残留中文（除同名条目）", not still_cn,
        "%d 条：%s" % (len(still_cn), "；".join(still_cn[:5])))

    # ⑤ 关键 key 抽查：这几条漏了会很明显
    for k in ("启动", "停止", "设置", "界面语言", "卸载 / 移出管理"):
        chk("A7 关键文案有译文：%s" % k, k in en, en.get(k, "缺"))

    if FAILS:
        print("\nRESULT: FAIL（%d 项）：%s" % (len(FAILS), "; ".join(FAILS)))
        return 1
    print("\nRESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
