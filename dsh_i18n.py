# -*- coding: utf-8 -*-
"""界面文案的中英切换。

**设计：中文原文就是 key。**

源码里所有面向用户的字符串都写成中文，外面包一层 `T()`。英文模式下 `T()` 去
`lang/en.json` 查表；查不到就**原样返回中文** —— 所以翻译可以分批补，任何时候
界面都不会出现空白、`None` 或者 key 名。

为什么不起英文 key（`ui.install.title` 那种）：
  · 一千多条文案，起名 + 维护映射表的工作量远超收益，而且很容易起错、两处不同步
  · 用原文当 key 时，源码本身就是可读的（改文案即改 key，不会出现「表里有、代码里没有」）
  · 代价是同一句话在多处出现时只能一起翻 —— 对本项目够用

**切换语言需要重建界面**：控件文案在创建时求值（`text=T("启动")`），改完语言后
由调用方重建卡片 / 重开窗口。见 `_apply_ui_lang()`。
"""
import json
import os

# 当前语言：zh / en。默认中文 —— 老配置里没有 ui.lang 时就是这个。
_LANG = {"code": "zh"}
# 中文原文 -> 英文。懒加载，只有切到 en 才读文件。
_EN = {}
_LOADED = {"en": False}

_HERE = os.path.dirname(os.path.abspath(__file__))
EN_PATH = os.path.join(_HERE, "lang", "en.json")

VALID = ("zh", "en")


def normalize(code):
    """把配置里的语言值收成 zh / en。认不出来的当 zh（老配置、手改错都兜住）。"""
    code = (code or "").strip().lower()
    return code if code in VALID else "zh"


def set_lang(code):
    """设置当前语言。不落盘 —— 落盘由调用方写 config.json 的 ui.lang。"""
    _LANG["code"] = normalize(code)
    return _LANG["code"]


def lang():
    return _LANG["code"]


def load_en(path=None):
    """读英文表。返回加载到的条数；失败返回 0（此时 T() 全部回退中文）。"""
    global _EN
    path = path or EN_PATH
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        _EN = {}
        _LOADED["en"] = True
        return 0
    if not isinstance(data, dict):
        _EN = {}
        _LOADED["en"] = True
        return 0
    _EN = {k: v for k, v in data.items()
           if isinstance(k, str) and isinstance(v, str) and v.strip()}
    _LOADED["en"] = True
    return len(_EN)


def T(s):
    """翻译一个字符串。非字符串、中文模式、没翻到 —— 一律原样返回。

    注意 `%` 格式化的顺序：`T("槽位已满（上限 %d）") % n` —— 先翻译模板再填参数，
    所以表里存的应该是带 `%d` / `%s` 的模板，而不是填好数字的句子。
    """
    if not isinstance(s, str):
        return s
    if _LANG["code"] != "en":
        return s
    if not _LOADED["en"]:
        load_en()
    return _EN.get(s, s)


def coverage():
    """返回 (已翻译条数, 是否已加载)，给「关于」/ 设置页显示进度用。"""
    return (len(_EN), _LOADED["en"])
