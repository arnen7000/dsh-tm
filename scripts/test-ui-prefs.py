# -*- coding: utf-8 -*-
"""离线回归：dsh_ui_prefs.py（界面显示偏好：显示哪些槽 / 最多几个 / 左右顺序）。

为什么单独一套：`dsh_manager.pyw` 不可 import（模块级建 GUI），
留在里面的逻辑只能靠正则抽函数体做静态断言。这里测的是**真行为**：
给一个 ui 配置 dict，看它算出哪几张卡片、按什么顺序。

不联网、不起进程、不碰用户目录，可以进提交钩子。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import dsh_ui_prefs as U   # noqa: E402

FAILS = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ((" | " + extra) if extra else ""))
    if not cond:
        FAILS.append(name)


# 四槽的真实形状（与用户本机一致：v1 是最后装进去的，所以排在 config 末尾）
VERSIONS = {"v2": {}, "v3": {}, "v4": {}, "v1": {}}
SLOTS = ["v2", "v3", "v4", "v1"]
# 上限只影响 max_visible 的钳制范围，取一个"比当前槽数大"的值即可。
# 2026-11-01 起跟随 GUI 的 MAX_SLOTS（6）。
MAXS = 6


# ══════════════ 没有 ui 段（老配置）══════════════
print("== 老配置：没有 ui 段时行为完全不变 ==")
chk("order 就是 config 原顺序", U.slot_order({}, VERSIONS, SLOTS) == SLOTS)
chk("visible = None（表示全显示）", U.visible_keys({}, VERSIONS, SLOTS) is None)
chk("display 就是全量", U.display_keys({}, VERSIONS, SLOTS, MAXS) == SLOTS)
chk("max_visible 缺省 = 槽上限", U.max_visible({}, MAXS) == MAXS)
chk("ui 传 None 也不炸", U.display_keys(None, VERSIONS, SLOTS, MAXS) == SLOTS)
chk("ui 传字符串（配置写坏了）也不炸",
    U.display_keys("oops", VERSIONS, SLOTS, MAXS) == SLOTS)


# ══════════════ 顺序 ══════════════
print("== 顺序：用户排的优先，新槽自动接到末尾 ==")
ui = {"order": ["v1", "v2", "v3", "v4"]}
chk("全排一遍就按排的来",
    U.slot_order(ui, VERSIONS, SLOTS) == ["v1", "v2", "v3", "v4"])

ui = {"order": ["v1", "v3"]}
chk("只记了两个：剩下按 config 顺序补在后面",
    U.slot_order(ui, VERSIONS, SLOTS) == ["v1", "v3", "v2", "v4"],
    str(U.slot_order(ui, VERSIONS, SLOTS)))

ui = {"order": ["v9", "v1", "v1", "v3"]}
got = U.slot_order(ui, VERSIONS, SLOTS)
chk("未知槽被丢掉、重复的只留一次",
    got == ["v1", "v3", "v2", "v4"], str(got))

# 这条是「装了新版本，界面上看不见它」的回归：order 是旧快照，v5 是后装的
V5 = dict(VERSIONS)
V5["v5"] = {}
ui = {"order": ["v1", "v2", "v3", "v4"]}
chk("order 是旧快照时，新装的 v5 自动接在末尾",
    U.slot_order(ui, V5, SLOTS + ["v5"]) == ["v1", "v2", "v3", "v4", "v5"],
    str(U.slot_order(ui, V5, SLOTS + ["v5"])))


# ══════════════ 显示哪些 ══════════════
print("== 显示哪些 ==")
ui = {"visible": ["v2", "v4"]}
chk("visible 挑出指定槽", U.display_keys(ui, VERSIONS, SLOTS, MAXS) == ["v2", "v4"])
chk("visible 的顺序服从 order 而不是 visible 自己的书写顺序",
    U.display_keys({"order": ["v4", "v2"], "visible": ["v2", "v4"]},
                   VERSIONS, SLOTS, MAXS) == ["v4", "v2"])
ui = {"visible": ["v1", "已卸载的v9"]}
chk("visible 里已卸载的槽键被忽略",
    U.display_keys(ui, VERSIONS, SLOTS, MAXS) == ["v1"])
ui = {"visible": []}
chk("visible 空列表 = 什么都不显示（由调用方给提示卡）",
    U.display_keys(ui, VERSIONS, SLOTS, MAXS) == [])
ui = {"visible": "v2"}          # 写错类型（字符串不是列表）
chk("visible 类型写错时退回「全显示」，不当成空",
    U.display_keys(ui, VERSIONS, SLOTS, MAXS) == SLOTS)


# ══════════════ 最多显示几个 ══════════════
print("== 最多显示 N 个 ==")
chk("max_visible=2 只留前两个",
    U.display_keys({"max_visible": 2}, VERSIONS, SLOTS, MAXS) == ["v2", "v3"])
chk("max_visible 与 order 一起用",
    U.display_keys({"max_visible": 2, "order": ["v1", "v4", "v2", "v3"]},
                   VERSIONS, SLOTS, MAXS) == ["v1", "v4"])
chk("max_visible 大于槽数时按实际槽数",
    U.display_keys({"max_visible": 99}, VERSIONS, SLOTS, MAXS) == SLOTS)
chk("max_visible=0 被钳到 1（界面不能空）",
    U.max_visible({"max_visible": 0}, MAXS) == 1)
chk("max_visible=-3 被钳到 1", U.max_visible({"max_visible": -3}, MAXS) == 1)
chk("max_visible=999 被钳到槽上限", U.max_visible({"max_visible": 999}, MAXS) == MAXS)
chk("max_visible 写的是字符串 '3' 也能认", U.max_visible({"max_visible": "3"}, MAXS) == 3)
chk("max_visible 写的是垃圾就用默认", U.max_visible({"max_visible": "abc"}, MAXS) == MAXS)
chk("槽上限传 2 时 max_visible 跟着钳到 2",
    U.max_visible({"max_visible": 4}, 2) == 2)


# ══════════════ 保存前的收口 ══════════════
print("== normalize：写盘前的收口 ==")
got = U.normalize({"max_visible": 3, "order": ["v1", "v2", "v3", "v4"],
                   "visible": ["v1", "v2", "v3"]}, VERSIONS, SLOTS, MAXS)
chk("正常输入原样保留",
    got == {"max_visible": 3, "order": ["v1", "v2", "v3", "v4"],
            "visible": ["v1", "v2", "v3"]}, str(got))

got = U.normalize({"visible": ["v1", "不存在"]}, VERSIONS, SLOTS, MAXS)
chk("未知槽键被清掉", got["visible"] == ["v1"], str(got))
chk("order 补齐到全量", got["order"] == SLOTS, str(got))

got = U.normalize({"visible": []}, VERSIONS, SLOTS, MAXS)
chk("全不选时兜底显示第一个（界面不能全空）",
    got["visible"] == ["v2"], str(got))

got = U.normalize({"visible": ["v4"]}, VERSIONS, SLOTS, 1)
chk("visible 有 1 个但 max_visible=1，display 就是那 1 个",
    U.display_keys(got, VERSIONS, SLOTS, 1) == ["v4"])

got = U.normalize(None, VERSIONS, SLOTS, MAXS)
chk("normalize(None) 得到全显示的合法配置",
    got["visible"] == SLOTS and got["max_visible"] == MAXS, str(got))

got = U.normalize({"max_visible": 2, "visible": ["v1", "v2", "v3", "v4"]},
                  VERSIONS, SLOTS, MAXS)
chk("normalize 保留全部勾选（截断只发生在渲染时）",
    len(got["visible"]) == 4, str(got))
chk("但渲染时只出 2 张",
    U.display_keys(got, VERSIONS, SLOTS, MAXS) == ["v2", "v3"])


# ══════════════ 幂等 / 稳定性 ══════════════
print("== 幂等：把 normalize 的结果再喂回去，结果不变 ==")
for prefs in ({"max_visible": 2, "order": ["v1", "v4"], "visible": ["v1", "v4"]},
              {"visible": ["v3"]},
              {"max_visible": 5, "order": ["v4", "v3", "v2", "v1"]},
              {"visible": []}):
    a = U.normalize(prefs, VERSIONS, SLOTS, MAXS)
    b = U.normalize(a, VERSIONS, SLOTS, MAXS)
    chk("幂等 %s" % (prefs.get("visible"),), a == b, "%s / %s" % (a, b))


# ══════════════ 隐藏不影响「管理」清单 ══════════════
print("== 隐藏 != 卸载 ==")
ui = {"visible": ["v2"]}
chk("显示只有 1 个，但 order 仍是全部槽（卸载/杀全部的名单不受影响）",
    len(U.slot_order(ui, VERSIONS, SLOTS)) == len(SLOTS),
    str(U.slot_order(ui, VERSIONS, SLOTS)))
chk("display 只出 1 个", len(U.display_keys(ui, VERSIONS, SLOTS, MAXS)) == 1)


# ══════════════ 拖拽排序的两个纯函数 ══════════════
print("== 拖动排序：move_to / merge_display_order ==")
chk("move_to 往后移", U.move_to(["a", "b", "c", "d"], "a", 2) == ["b", "c", "a", "d"],
    str(U.move_to(["a", "b", "c", "d"], "a", 2)))
chk("move_to 往前移", U.move_to(["a", "b", "c", "d"], "d", 0) == ["d", "a", "b", "c"],
    str(U.move_to(["a", "b", "c", "d"], "d", 0)))
chk("move_to 原位不动", U.move_to(["a", "b", "c"], "b", 1) == ["a", "b", "c"])
chk("move_to 落到末尾", U.move_to(["a", "b", "c"], "a", 2) == ["b", "c", "a"])
chk("move_to 越界钳到末尾（鼠标拖出窗口是常态）",
    U.move_to(["a", "b", "c"], "a", 99) == ["b", "c", "a"])
chk("move_to 负数钳到开头", U.move_to(["a", "b", "c"], "c", -5) == ["c", "a", "b"])
chk("move_to 未知键原样返回（不抛异常）",
    U.move_to(["a", "b"], "zz", 0) == ["a", "b"])
chk("move_to 不改原列表", (lambda s: (U.move_to(s, "a", 2), s)[1])(["a", "b", "c"]) == ["a", "b", "c"])

# 主窗口拖的是"显示中的那几个"，隐藏的槽不在里面 —— 合并时不能把它们挤到末尾
full = ["v2", "v3", "v4", "v1"]
chk("只重排显示中的槽，隐藏的原地不动",
    U.merge_display_order(full, ["v4", "v2"]) == ["v4", "v3", "v2", "v1"],
    str(U.merge_display_order(full, ["v4", "v2"])))
chk("全部显示时就是普通重排",
    U.merge_display_order(full, ["v1", "v4", "v3", "v2"]) == ["v1", "v4", "v3", "v2"])
chk("一个槽时不重排（返回原顺序）",
    U.merge_display_order(full, ["v3"]) == full)
chk("空输入不炸", U.merge_display_order([], []) == [] and U.merge_display_order(None, None) == [])
chk("合并结果一定是原集合的排列（不增不减）",
    sorted(U.merge_display_order(full, ["v4", "v2"])) == sorted(full))
# 合并后过一遍 normalize，界面上看到的顺序应当就是拖动后的顺序
ui = U.normalize({"max_visible": 4, "order": U.merge_display_order(full, ["v4", "v2"]),
                  "visible": full}, VERSIONS, SLOTS, MAXS)
chk("拖动结果经 normalize 后，显示顺序仍是拖动后的",
    U.display_keys(ui, VERSIONS, SLOTS, MAXS) == ["v4", "v3", "v2", "v1"],
    str(U.display_keys(ui, VERSIONS, SLOTS, MAXS)))

# 拖动 + 上限截断：只画前 N 个时，拖的是前 N 个，被截掉的槽位置也不能乱
ui2 = {"max_visible": 2, "order": full, "visible": full}
disp2 = U.display_keys(ui2, VERSIONS, SLOTS, MAXS)
chk("上限 2 时只画 2 个（拖动的范围就是这 2 个）", disp2 == ["v2", "v3"], str(disp2))
merged = U.merge_display_order(full, ["v3", "v2"])
chk("拖动被截断场景：合并后前两个就是拖动结果",
    U.display_keys({"max_visible": 2, "order": merged, "visible": full},
                   VERSIONS, SLOTS, MAXS) == ["v3", "v2"])


print()
if FAILS:
    print("RESULT: FAIL（%d 项）: %s" % (len(FAILS), "; ".join(FAILS)))
    sys.exit(1)
print("RESULT: ALL PASS")
