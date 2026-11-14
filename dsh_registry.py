# -*- coding: utf-8 -*-
"""从 npm registry 取 @deepseek-ai/dsh 的**全部已发布版本**，供安装/换版本对话框选择。

为什么单独成文件
----------------
`dsh_manager.pyw` 有二十多处模块级副作用（建 GUI、写日志），不可 import、不可单测。
这里只放「取数据 + 解析 + 排序」，与界面无关：
  · `parse_versions()` 是纯函数 —— 离线回归直接喂固定 JSON 断言，不联网；
  · `fetch_versions()` 才碰网络，且由调用方决定在哪个线程跑。

为什么不用 `npm view @deepseek-ai/dsh versions`
----------------------------------------------
1. `npm.cmd` 在 pythonw（无控制台）下会派生 node 孙进程继承管道，即使超时杀掉
   子进程、读管道仍可能永久阻塞 —— 主文件的 `_probe_npm_global` 与
   `_swap_worker` 都为此踩过坑。直接 HTTP 拿 JSON 没有这个问题。
2. 目标机可能没装 npm（管理器自带 node，但不一定有 npm CLI 在 PATH 上）。
3. registry 的 JSON 里还带 dist-tags 与 deprecated 标记，能直接标出「最新」与
   「已弃用」，比 `versions` 纯数组更有用。
"""
import json
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from dsh_i18n import T

# 官方源优先；国内网络访问 npmjs 常被墙/超时，镜像兜底。
# 镜像与官方同步有延迟（通常几分钟），所以只在官方源失败时才用。
DEFAULT_REGISTRIES = (
    "https://registry.npmjs.org/@deepseek-ai/dsh",
    "https://registry.npmmirror.com/@deepseek-ai/dsh",
)

# 只要「版本表 + dist-tags + deprecated」，不要 readme/所有 tarball 元数据。
# npm 的 abbreviated 文档能把 1MB+ 压到 ~100KB，界面里等它的时间差别明显。
ACCEPT_ABBREVIATED = "application/vnd.npm.install-v1+json"

_UA = "dsh-manager-version-picker/1.0 (+https://github.com/)"

_NUM_RE = re.compile(r"^\d+$")


def version_key(v):
    """把版本串转成可比较的元组（近似 semver 排序）。

    为什么不能直接字符串排序：`0.1.10` 会排在 `0.1.9` 前面（字典序），
    `0.2.0-rc.10` 会排在 `0.2.0-rc.2` 前面 —— 界面里「最新版本」就选错了。

    规则（与 semver 一致的部分）：
      · 主版本三段按**数字**比；
      · 同主版本时，正式版 > 预发布版（`0.2.0` > `0.2.0-rc.2`）；
      · 预发布段逐段比：数字段按数字比，字母段按字母比（alpha < beta < rc）。
    解析不了的串排在最后（返回 (2, ...)），不至于让整张表炸掉。
    """
    s = (v or "").strip()
    if not s:
        return (3, (), 0, ())
    main, sep, pre = s.partition("-")
    parts = main.split(".")
    if len(parts) < 3 or not all(_NUM_RE.match(p or "") for p in parts[:3]):
        return (3, (), 0, ())          # 非标准形态：排最后
    nums = tuple(int(p) for p in parts[:3])
    if not sep or not pre:
        return (0, nums, 1, ())        # 正式版
    ids = []
    for seg in pre.split("."):
        if _NUM_RE.match(seg):
            ids.append((0, int(seg), ""))
        else:
            ids.append((1, 0, seg.lower()))
    return (0, nums, 0, tuple(ids))


def parse_versions(data):
    """registry JSON -> [{version, tags, deprecated}]，按版本从新到旧。

    纯函数：不联网、不写文件。`data` 就是 registry 那个 JSON 对象。
    只保留 `versions` 里真实存在的键（dist-tags 可能指向已被 unpublish 的版本，
    那种不能给用户选 —— 选中会得到一个装不上的版本号）。
    """
    if not isinstance(data, dict):
        return []
    versions = data.get("versions")
    if not isinstance(versions, dict):
        return []

    tags_of = {}
    dist_tags = data.get("dist-tags")
    if isinstance(dist_tags, dict):
        for tag, ver in dist_tags.items():
            if isinstance(ver, str) and ver:
                tags_of.setdefault(ver, []).append(str(tag))

    out = []
    for ver, meta in versions.items():
        if not isinstance(ver, str) or not ver.strip():
            continue
        dep = ""
        if isinstance(meta, dict):
            dep = str(meta.get("deprecated") or "").strip()
        out.append({
            "version": ver,
            "tags": sorted(tags_of.get(ver, [])),
            "deprecated": dep,
        })
    out.sort(key=lambda it: version_key(it["version"]), reverse=True)
    return out


def format_line(item):
    """列表里一行怎么显示：版本号 + 标记。给界面用，也便于测试断言。"""
    marks = []
    for t in item.get("tags") or []:
        marks.append({"latest": T("最新"), "next": T("预览")}.get(t, t))
    if item.get("deprecated"):
        marks.append(T("已弃用"))
    return item["version"] + (("　（%s）" % "，".join(marks)) if marks else "")


def default_pick(items):
    """默认选中哪个版本：dist-tag latest > next > 表里第一个（最新）。
    返回版本号字符串，没有可用版本时返回 ''。"""
    for want in ("latest", "next"):
        for it in items or []:
            if want in (it.get("tags") or []) and not it.get("deprecated"):
                return it["version"]
    for it in items or []:
        if not it.get("deprecated"):
            return it["version"]
    return (items or [{}])[0].get("version", "") if items else ""


def fetch_versions(urls=None, timeout=8.0, opener=None):
    """从 registry 取版本表。返回 (ok, items, 来源或错误说明)。

    依次尝试 urls（默认官方源 -> 镜像），第一个成功的就用。全部失败时返回
    ok=False 与**人话**原因（超时/网络不通/HTTP 状态/JSON 坏了要分开说，
    否则用户只会看到「获取失败」，不知道该查网络还是查代理）。
    """
    urls = urls or DEFAULT_REGISTRIES
    errors = []
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={
                "Accept": ACCEPT_ABBREVIATED,
                "User-Agent": _UA,
            })
            op = opener or urllib.request.urlopen
            with op(req, timeout=timeout) as resp:
                raw = resp.read()
            data = json.loads(raw.decode("utf-8", "replace"))
            items = parse_versions(data)
            if not items:
                errors.append("%s：返回的 JSON 里没有版本列表" % url)
                continue
            return (True, items, urlsplit(url).netloc or url)
        except urllib.error.HTTPError as e:
            errors.append("%s：HTTP %s" % (url, e.code))
        except urllib.error.URLError as e:
            errors.append("%s：网络不通（%s）" % (url, getattr(e, "reason", e)))
        except Exception as e:
            errors.append("%s：%s" % (url, e))
    return (False, [], "；".join(errors) or "没有可用的 registry 地址")


# ── 进程内缓存：同一分钟内反复开对话框不重复联网 ────────────────────────────
# 只缓存成功结果；失败不缓存（用户可能刚修好代理，重开就该重试）。
_CACHE = {"ts": 0.0, "items": [], "source": "", "ttl": 600.0}


def cached_versions(max_age=None):
    """返回 (items, source)；缓存过期或为空则返回 ([], '')，由调用方决定是否刷新。"""
    ttl = _CACHE["ttl"] if max_age is None else float(max_age)
    if _CACHE["items"] and (time.time() - _CACHE["ts"]) <= ttl:
        return (list(_CACHE["items"]), _CACHE["source"])
    return ([], "")


def remember(items, source):
    _CACHE["ts"] = time.time()
    _CACHE["items"] = list(items or [])
    _CACHE["source"] = source or ""


def clear_cache():
    _CACHE["ts"] = 0.0
    _CACHE["items"] = []
    _CACHE["source"] = ""
