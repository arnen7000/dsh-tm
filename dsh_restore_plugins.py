# -*- coding: utf-8 -*-
"""从卸载备份恢复插件到指定槽（纯逻辑，可离线测试）。

为什么单独一个模块
------------------
`dsh_uninstall_ops` 负责「卸载时留什么」，这里负责「装回来」。两者共用
备份目录的布局，但方向相反，放一起会让两边都变难读。

恢复什么 / 不恢复什么
---------------------
  · 恢复：`profiles/<name>/package.json` 里的**用户插件**（dependencies +
    dsh.profile.bundles 中非 @deepseek-ai/ 的项）、`cordis.patch.yml`、
    `pnpm-workspace.yaml`
  · **不恢复**：`node_modules`（重新装，顺便验证与目标槽 dsh 版本的兼容性）、
    `pnpm-lock.yaml`（旧 lock 与新版 dsh 冲突，让 pnpm 重新解析）、
    会话 / 凭据（那是 `.dsh-home` 其他部分，用户可手工拷回）

合并规则（2026-11-28 定）
------------------------
  · dependencies：只追加目标槽**没有**的；目标已有的保留，不降级
  · bundles：只追加**非 @deepseek-ai/** 的项（内置 bundle 由目标槽的 dsh 决定）
  · 目标槽已有 package.json → 先备份成 `.bak-<stamp>` 再写
"""
import io
import json
import os
import shutil
import subprocess
import time

# 恢复时从备份拷过去的配置文件（不含 node_modules / lock）
_COPY_FILES = ("cordis.patch.yml", "pnpm-workspace.yaml")


def _read_json(path):
    with io.open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def _profile_manifests(backup_path):
    """读备份里每个 profile 的 package.json。返回 {profile_name: dict}。"""
    root = os.path.join(backup_path, "profiles")
    out = {}
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name, "package.json")
        if not os.path.isfile(p):
            continue
        try:
            out[name] = _read_json(p)
        except Exception:
            continue
    return out


def list_backups(backup_root):
    """扫描备份目录，返回可恢复的备份列表（最新的在前）。

    每条：path / name / usable / reason / slot_key / label /
          installed_version / backed_up_at / profiles / plugin_count
    """
    out = []
    if not os.path.isdir(backup_root):
        return out
    for name in sorted(os.listdir(backup_root), reverse=True):
        p = os.path.join(backup_root, name)
        if not os.path.isdir(p) or "-userdata-" not in name:
            continue
        meta_p = os.path.join(p, "_meta.json")
        if not os.path.isfile(meta_p):
            out.append({"path": p, "name": name, "usable": False,
                        "reason": "missing_meta"})
            continue
        try:
            meta = _read_json(meta_p)
        except Exception:
            out.append({"path": p, "name": name, "usable": False,
                        "reason": "unreadable_meta"})
            continue
        profiles = _profile_manifests(p)
        count = sum(len((m.get("dependencies") or {})) for m in profiles.values())
        out.append({
            "path": p, "name": name, "usable": bool(profiles),
            "reason": "" if profiles else "no_profile_manifest",
            "slot_key": meta.get("slot_key"),
            "label": meta.get("label"),
            "installed_version": meta.get("installed_version"),
            "backed_up_at": meta.get("backed_up_at"),
            "profiles": profiles,
            "plugin_count": count,
        })
    return out


def plan_restore(backup_path, slot_root, profile="web"):
    """纯计算：算出要把哪些依赖 / bundle 加进目标槽。**不写盘**。

    返回 dict：
      ok / reason / profile
      add_deps          目标槽没有的依赖
      version_conflicts 两边都有但版本不同（保留目标版本，只报告）
      add_bundles       要追加的用户 bundle（非 @deepseek-ai/）
      src_dir / dst_dir / dst_manifest / dst_manifest_path
    """
    src_man = _profile_manifests(backup_path).get(profile)
    if not src_man:
        return {"ok": False, "reason": "no_profile_manifest", "profile": profile}

    dst_dir = os.path.join(slot_root, ".dsh-home", "profiles", profile)
    dst_p = os.path.join(dst_dir, "package.json")
    dst_man = {}
    if os.path.isfile(dst_p):
        try:
            dst_man = _read_json(dst_p)
        except Exception:
            dst_man = {}

    src_deps = src_man.get("dependencies") or {}
    dst_deps = dst_man.get("dependencies") or {}
    add_deps = {k: v for k, v in src_deps.items() if k not in dst_deps}
    conflicts = {k: {"backup": v, "current": dst_deps[k]}
                 for k, v in src_deps.items()
                 if k in dst_deps and dst_deps[k] != v}

    def _bundles(m):
        return ((m.get("dsh") or {}).get("profile") or {}).get("bundles") or []

    dst_bundles = _bundles(dst_man)
    add_bundles = [b for b in _bundles(src_man)
                   if not str(b).startswith("@deepseek-ai/") and b not in dst_bundles]

    return {
        "ok": True, "reason": "", "profile": profile,
        "add_deps": add_deps, "version_conflicts": conflicts,
        "add_bundles": add_bundles,
        "src_dir": os.path.join(backup_path, "profiles", profile),
        "dst_dir": dst_dir, "dst_manifest": dst_man,
        "dst_manifest_path": dst_p,
    }


# ── 兼容性预检（2026-11-28）──────────────────────────────────────────────
# 备份记的是「卸载时那套插件清单」，恢复目标却是「用户随手选的槽」，两边的
# dsh 版本本来就可能不同（用户原话：卸载时的版本不一定等于新装的版本，槽也
# 可能随意选）。所以判定必须**现场**做，而且必须和 dsh 自己一致：
#
#   dsh-app-boot/lib/index.js 的 unmetRequirements()：
#     · 有 engines.dsh → 它**单独**决定，peerDependencies 一律忽略
#     · 否则 → 每个 @deepseek-ai/dsh 或 @deepseek-ai/dsh-* 的 peer 都必须满足
#     · 一律 semver.satisfies(v, r, { includePrerelease: true })
#
# 最后一条很关键：0.2.3-alpha.2 这种预发布版本，严格 semver 判
# `>=0.2.0-rc.1 <0.3.0-0` 是 false（预发布默认不参与区间），加了
# includePrerelease 才是 true。自己实现 semver 很容易和 npm 判得不一样，
# 所以直接调槽内自带的 semver（dsh 依赖它，必然在树里）；判不了就如实说
# 「判不了」，绝不猜。


def slot_dsh_version(slot_root):
    """读目标槽的 dsh 版本；读不到返回 None。"""
    p = os.path.join(slot_root, "node_modules", "@deepseek-ai", "dsh",
                     "package.json")
    try:
        return str(_read_json(p).get("version") or "") or None
    except Exception:
        return None


def _semver_dir(slot_root):
    """找槽内可用的 semver 包目录（dsh-app-boot 依赖它，所以必然在树里）。

    必须确认 `index.js` 真的在：目录存在但残缺（比如被清理了一半）时，宁可
    报「判不了」，也不要交给 node 去撞 require 失败。
    """
    for c in (os.path.join(slot_root, "node_modules", "semver"),
              os.path.join(slot_root, "node_modules", "@deepseek-ai",
                           "dsh-app-boot", "node_modules", "semver")):
        if os.path.isfile(os.path.join(c, "index.js")):
            return c
    return None


def _plugin_requirement(profile_dir, name):
    """从**备份**的 node_modules 读插件声明，返回 (kind, ranges, version)。

    kind: "engines"（engines.dsh 单独决定）/ "peers" / ""（没声明）
    """
    pj = os.path.join(profile_dir, "node_modules", name, "package.json")
    try:
        man = _read_json(pj)
    except Exception:
        return "", [], ""
    ver = str(man.get("version") or "")
    eng = (man.get("engines") or {}).get("dsh")
    if isinstance(eng, str) and eng.strip():
        return "engines", [["engines.dsh", eng]], ver
    peers = [[k, v] for k, v in (man.get("peerDependencies") or {}).items()
             if (k == "@deepseek-ai/dsh" or k.startswith("@deepseek-ai/dsh-"))
             and isinstance(v, str)]
    if peers:
        return "peers", peers, ver
    return "", [], ver


# node 侧只做一件事：用槽内的 semver 按 dsh 的规则逐条判。payload 走临时文件，
# 避免把 JSON 拼进命令行（范围里有 | > < 之类字符）。
_NODE_CHECK_JS = """
const fs = require("fs");
const semver = require(process.argv[1]);
const payload = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const out = payload.plugins.map(function (p) {
  const pairs = p.kind === "engines" ? [["engines.dsh", p.ranges[0][1]]] : p.ranges;
  const bad = pairs.filter(function (pair) {
    const r = String(pair[1]);
    return !(r.trim() !== "" &&
             semver.satisfies(payload.version, r, { includePrerelease: true }));
  });
  return { name: p.name, ok: bad.length === 0, bad: bad };
});
process.stdout.write(JSON.stringify(out));
"""


def check_compat(backup_path, slot_root, node_exe, plugins,
                 profile="web", timeout=30):
    """判断这些插件在目标槽的 dsh 版本下是否相容（只读，不写盘）。

    返回 {"dsh_version", "judged", "results": [{name, version, kind, ranges,
    ok, bad}], "reason"}。`judged=False` 时 `ok` 一律为 None —— 只报告插件
    自己声明的范围，不做判定（缺 dsh 版本 / 缺 semver / node 跑不起来）。
    """
    dsh_ver = slot_dsh_version(slot_root)
    prof_dir = os.path.join(backup_path, "profiles", profile)
    results = []
    for name in plugins:
        kind, ranges, ver = _plugin_requirement(prof_dir, name)
        results.append({"name": name, "version": ver, "kind": kind,
                        "ranges": ranges, "ok": None, "bad": []})
    out = {"dsh_version": dsh_ver, "judged": False, "results": results,
           "reason": ""}
    if not dsh_ver:
        out["reason"] = "no_dsh_version"
        return out
    declared = [r for r in results if r["kind"]]
    if not declared:
        out["reason"] = "no_declaration"
        return out
    sem = _semver_dir(slot_root)
    if not sem or not node_exe:
        out["reason"] = "no_semver"
        return out

    payload = {"version": dsh_ver,
               "plugins": [{"name": r["name"], "kind": r["kind"],
                            "ranges": r["ranges"]} for r in declared]}
    tmp = None
    try:
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix="dsh-compat-", suffix=".json")
        with io.open(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        p = subprocess.run(
            [node_exe, "-e", _NODE_CHECK_JS, sem.replace("\\", "/"), tmp],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout)
        if p.returncode != 0:
            out["reason"] = "node_exit_%d" % p.returncode
            return out
        judged = {d["name"]: d for d in json.loads(p.stdout or "[]")}
    except Exception as e:
        out["reason"] = "check_failed: %s" % e
        return out
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass

    for r in results:
        d = judged.get(r["name"])
        if d is not None:
            r["ok"] = bool(d.get("ok"))
            r["bad"] = [[k, v] for k, v in (d.get("bad") or [])]
    out["judged"] = True
    return out


def compat_problems(compat):
    """从 check_compat 的结果里挑出「声明不支持」的插件（judged=False 时为空）。"""
    if not compat or not compat.get("judged"):
        return []
    return [r for r in (compat.get("results") or []) if r.get("ok") is False]


def _merged_manifest(plan):
    """把 plan 应用到目标清单上，返回新 dict（不改原对象）。"""
    man = json.loads(json.dumps(plan.get("dst_manifest") or {}))
    deps = dict(man.get("dependencies") or {})
    deps.update(plan.get("add_deps") or {})
    man["dependencies"] = deps
    man.setdefault("name", "dsh-profile-%s" % plan.get("profile", "web"))
    man.setdefault("private", True)
    dsh = man.setdefault("dsh", {})
    prof = dsh.setdefault("profile", {})
    bundles = list(prof.get("bundles") or [])
    for b in plan.get("add_bundles") or []:
        if b not in bundles:
            bundles.append(b)
    prof["bundles"] = bundles
    return man


def _pnpm_install(node_exe, cwd, timeout, log):
    """用槽内 corepack 跑 pnpm install。返回 (ok, 说明)。"""
    node_dir = os.path.dirname(os.path.abspath(node_exe))
    corepack = os.path.join(node_dir, "corepack.cmd")
    if not os.path.isfile(corepack):
        return False, "corepack_missing"
    env = dict(os.environ)
    env["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"
    try:
        p = subprocess.run(["cmd", "/c", corepack, "pnpm", "install"],
                           cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, "spawn_failed: %s" % e
    tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
    log.extend(tail[-30:])
    if p.returncode != 0:
        return False, "pnpm_exit_%d" % p.returncode
    return True, ""


def apply_restore(plan, node_exe, progress=None, timeout=600):
    """执行恢复：备份目标清单 → 写合并结果 → 拷配置 → pnpm install。

    返回 (ok, report)；report 含 installed / missing / log / reason。
    """
    def step(msg):
        if progress:
            progress(msg)

    if not plan.get("ok"):
        return False, {"reason": plan.get("reason") or "bad_plan",
                       "installed": [], "missing": [], "log": []}

    dst_dir = plan["dst_dir"]
    os.makedirs(dst_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst_p = plan["dst_manifest_path"]

    # 1) 目标已有清单 → 先备份
    if os.path.isfile(dst_p):
        bak = dst_p + ".bak-" + stamp
        try:
            shutil.copy2(dst_p, bak)
        except OSError as e:
            return False, {"reason": "backup_failed: %s" % e,
                           "installed": [], "missing": [], "log": []}

    # 2) 写合并后的清单
    merged = _merged_manifest(plan)
    step("write_manifest")
    try:
        tmp = dst_p + ".tmp-%d" % os.getpid()
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        os.replace(tmp, dst_p)
    except OSError as e:
        return False, {"reason": "write_failed: %s" % e,
                       "installed": [], "missing": [], "log": []}

    # 3) 拷配置文件（目标没有才拷，避免覆盖用户已改的）
    for name in _COPY_FILES:
        src = os.path.join(plan["src_dir"], name)
        dst = os.path.join(dst_dir, name)
        if os.path.isfile(src) and not os.path.exists(dst):
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass

    # 4) 安装
    step("pnpm_install")
    log = []
    ok, reason = _pnpm_install(node_exe, dst_dir, timeout, log)

    # 5) 校验：每个新增依赖在 node_modules 里都有目录
    installed, missing = [], []
    nm = os.path.join(dst_dir, "node_modules")
    # 校验「备份里的所有插件是否都可用」，不只是本次新增的 ——
    # 目标槽原本就有的（版本冲突那一批）同样要确认目录存在。
    wanted = (list(plan.get("add_deps") or {})
              + list(plan.get("version_conflicts") or {}))
    for dep in wanted:
        if os.path.isdir(os.path.join(nm, dep)):
            installed.append(dep)
        else:
            missing.append(dep)
    if ok and missing:
        ok, reason = False, "missing_after_install"

    return ok, {"reason": reason, "installed": installed,
                "missing": missing, "log": log,
                "conflicts": plan.get("version_conflicts") or {}}
