"""界面显示偏好：显示哪几个槽、最多显示几个、从左到右的顺序。

为什么单独一个模块：`dsh_manager.pyw` 有二十多处模块级副作用（建 GUI、写日志），
**不可 import** —— 纯逻辑留在里面就只能靠正则抽函数体做静态断言，那种测试
证明不了行为。这里全是纯函数（dict 进、list 出），可以真跑，
`scripts/test-ui-prefs.py` 就是它的回归。

关键约定：**隐藏 != 卸载**。本模块只决定「画哪几张卡片、按什么顺序画」，
槽的进程、端口、数据目录一概不碰，「卸载」「杀全部」的名单也不受影响。
所以用户调布局不可能丢东西 —— 这也是它敢做成即时生效、不必重启的原因。

config.json 里的样子（顶层可选段，缺省时一切照旧）：

    "ui": {
      "max_visible": 4,                     # 界面最多同时显示几个（1..槽上限）
      "order":   ["v2", "v1", "v3", "v4"],  # 从左到右的顺序
      "visible": ["v2", "v1", "v3"]         # 显示哪些；缺这个键 = 全显示
    }
"""


def _ui(ui):
    return ui if isinstance(ui, dict) else {}


def slot_order(ui, versions, slot_keys):
    """界面上槽的左右顺序：ui.order 里记过的优先，没记过的按 config 原顺序接在后面。

    为什么要兜底：新装的槽是 append 进 config.json 的（装 v1 时它排在 v4 后面），
    而 ui.order 是旧快照。不兜底就会出现「装了新版本，界面上看不见它」。
    """
    ui = _ui(ui)
    versions = versions or {}
    seen, out = set(), []
    for k in (ui.get("order") or []):
        if k in versions and k not in seen:
            seen.add(k)
            out.append(k)
    for k in (slot_keys or []):
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def max_visible(ui, max_slots):
    """界面最多同时显示几个槽，钳到 1..max_slots。"""
    ui = _ui(ui)
    try:
        n = int(ui.get("max_visible", max_slots))
    except (TypeError, ValueError):
        n = int(max_slots)
    return max(1, min(int(max_slots), n))


def visible_keys(ui, versions, slot_keys):
    """要显示的槽键；返回 None 表示「全显示」（老配置没有 visible 这个键）。

    返回列表而不是 set：顺序以 slot_order 为准，直接能拿去渲染，
    也顺手丢掉已卸载槽残留的键（config 里删了槽，ui.visible 里还留着）。
    """
    ui = _ui(ui)
    v = ui.get("visible")
    if not isinstance(v, list):
        return None
    want = set(v)
    return [k for k in slot_order(ui, versions, slot_keys) if k in want]


def display_keys(ui, versions, slot_keys, max_slots):
    """界面真正渲染的槽：按用户排的顺序，再按「最多显示 N 个」截断。"""
    keys = visible_keys(ui, versions, slot_keys)
    if keys is None:
        keys = slot_order(ui, versions, slot_keys)
    return keys[:max_visible(ui, max_slots)]


def move_to(seq, item, index):
    """把 item 移到 index 位置，其余元素保持相对顺序。

    拖拽排序的唯一基本动作。index 会被钳进 [0, len(其余)]：
    界面上的落点来自鼠标位置，越界是常态，不能因此抛异常。
    item 不在 seq 里时原样返回副本（调用方可能拿到过期的键）。
    """
    seq = list(seq or [])
    if item not in seq:
        return seq
    out = [x for x in seq if x != item]
    i = max(0, min(len(out), int(index)))
    out.insert(i, item)
    return out


def merge_display_order(base, displayed_new):
    """把界面上拖动得到的相对顺序合并回完整顺序 base。

    为什么不能直接把新顺序当 order 存：界面上只画得出「显示中」的那几个槽，
    隐藏的槽不在里面。直接存会把隐藏槽挤到末尾（以后重新显示时位置全变）。
    这里的做法是 —— 只重排 displayed_new 里那些槽，占住它们原来占的那几个
    位置，其它槽原地不动。

    base 与 displayed_new 都是键的列表；displayed_new 里的键必须在 base 里，
    多出来的会被忽略（normalize 也会再收一遍）。
    """
    base = list(base or [])
    disp = [k for k in (displayed_new or []) if k in base]
    if len(disp) < 2:
        return base
    want = set(disp)
    it = iter(disp)
    return [next(it) if k in want else k for k in base]


def normalize(prefs, versions, slot_keys, max_slots):
    """把对话框交上来的偏好收成能安全写进 config.json 的形状。

    收口三件事：丢掉未知槽键（卸载后残留的）、钳住 max_visible、
    保证 visible 至少有一个 —— 界面全空会被当成程序坏了，宁可兜底。
    """
    prefs = _ui(prefs)
    versions = versions or {}
    slot_keys = slot_keys or []
    order = slot_order(prefs, versions, slot_keys)
    vis = visible_keys(prefs, versions, slot_keys)
    if vis is None:
        vis = list(order)
    want = set(vis)
    vis = [k for k in order if k in want]
    if not vis and order:
        vis = order[:1]              # 兜底：绝不留一个全空的界面
    return {
        "max_visible": max_visible(prefs, max_slots),
        "order": order,
        "visible": vis,
    }
