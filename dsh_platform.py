"""Paths and launch helpers shared by dsh-tm's entry points."""
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"
FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NODE_NAME = "node.exe" if IS_WINDOWS else "node"


def app_support(home=None):
    home = os.path.expanduser("~") if home is None else home
    return os.path.join(home, "Library", "Application Support")


def default_config_path():
    root = Path(app_support()) / "DSH-TM" if IS_MAC else RESOURCE_DIR
    return str(root / "config.json")


def log_path(configuration=None):
    return os.path.join(os.path.dirname(configuration or default_config_path()),
                        "dsh-manager.log")


def config_base(cfg, config_path=None):
    preferred = "<appsupport>/DSH-TM" if IS_MAC else "<home>"
    base = resolve_path(cfg.get("install_base", preferred))
    if not IS_MAC or not isinstance(base, str) or not os.path.isabs(base):
        return base
    standard = os.path.join(app_support(), "DSH-TM")
    if cfg.get("install_base") != "<appsupport>/DSH-TM" and base != standard:
        return base

    # The old frozen app implicitly appended dsh-versions to <base>. Prefer
    # that layout only when it explains the managed entries unambiguously.
    versions = cfg.get("versions")
    entries = [slot.get("bin_js") for slot in versions.values()
               if isinstance(slot, dict)] if isinstance(versions, dict) else []
    relative = [entry for entry in entries
                if isinstance(entry, str) and entry.startswith("<base>/")]
    legacy = os.path.join(base, "dsh-versions")
    if (relative and all(os.path.isfile(resolve_path(entry, legacy)) for entry in relative)
            and not any(os.path.isfile(resolve_path(entry, base)) for entry in relative)):
        return legacy
    return base


def resolve_path(value, base="", home=None, self_dir=None, npm_global=""):
    if not isinstance(value, str):
        return value
    home = os.path.expanduser("~") if home is None else home
    return os.path.expanduser(value.replace("<self>", str(self_dir or RESOURCE_DIR))
                              .replace("<home>", str(home))
                              .replace("<appsupport>", app_support(home))
                              .replace("<base>", str(base))
                              .replace("<npm_global>", str(npm_global or "")))


def slot_node_path(root):
    path = Path(root) / "node"
    if not IS_WINDOWS:
        path /= "bin"
    return str(path / NODE_NAME)


def bundled_node():
    parent = RESOURCE_DIR / "runtime" / "node"
    candidate = parent / NODE_NAME if IS_WINDOWS else parent / "bin" / NODE_NAME
    return str(candidate) if os.access(candidate, os.X_OK) else ""


def resolve_node(node=None):
    node = node or NODE_NAME
    if os.path.dirname(node):
        path = os.path.expanduser(node)
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else ""
    if IS_MAC and node == "node":
        return bundled_node() or shutil.which(node) or next(
            (p for p in ("/opt/homebrew/bin/node", "/usr/local/bin/node")
             if os.access(p, os.X_OK)), "")
    return shutil.which(node) or ""


def npm_cli_path(node):
    if not node:
        return ""
    parent = Path(node).resolve().parent
    prefixes = [parent] if IS_WINDOWS else [parent.parent / "lib", parent]
    candidates = [root / "node_modules" / "npm" / "bin" / "npm-cli.js"
                  for root in prefixes]
    if IS_MAC:
        # Homebrew installs npm separately from the node Cellar prefix.
        candidates.extend(Path(prefix) / "lib/node_modules/npm/bin/npm-cli.js"
                          for prefix in ("/opt/homebrew", "/usr/local"))
        npm = shutil.which("npm")
        if npm:
            candidates.append(Path(npm).resolve())
    return next((str(p) for p in candidates if p.name == "npm-cli.js" and p.is_file()), "")


def probe_npm_global():
    if not IS_MAC:
        appdata = os.environ.get("APPDATA")
        return os.path.join(appdata, "npm", "node_modules") if appdata else ""
    candidates = []
    node = resolve_node()
    cli = npm_cli_path(node)
    if node and cli:
        try:
            run = subprocess.run([node, cli, "root", "-g"], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=4)
            if run.returncode == 0 and run.stdout.strip():
                candidates.append(run.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            pass
    candidates.extend(("/opt/homebrew/lib/node_modules", "/usr/local/lib/node_modules"))
    return next((p for p in candidates if os.path.isdir(
        os.path.join(p, "@deepseek-ai", "dsh"))), candidates[0] if candidates else "")


def slot_env(cfg, env=None):
    env = dict(os.environ if env is None else env)
    if cfg.get("dsh_home"):
        env["DSH_HOME"] = cfg["dsh_home"]
    if cfg.get("temp_dir"):
        env["TEMP"] = env["TMP"] = cfg["temp_dir"]
        if not IS_WINDOWS:
            env["TMPDIR"] = cfg["temp_dir"]
    if IS_MAC:
        node = cfg.get("node_exe")
        if node and os.path.dirname(node):
            env["PATH"] = os.path.dirname(node) + os.pathsep + env.get("PATH", os.defpath)
    return env


def slot_launcher_paths(root, port):
    extension = "command" if IS_MAC else "bat"
    filenames = {"start": "dsh", "cli": "dsh-cli"}
    if port:
        filenames["stop"] = "kill-%s" % port
    return {kind: os.path.join(root, "%s.%s" % (name, extension))
            for kind, name in filenames.items()}


def read_slot_port(root):
    try:
        with open(slot_launcher_paths(root, None)["start"], encoding="utf-8") as script:
            next(script)
            match = re.fullmatch(r"# dsh-tm port: (\d{1,5})\n", next(script))
    except (OSError, StopIteration, UnicodeError):
        return None
    value = int(match[1]) if match else 0
    return value if 1 <= value <= 65535 else None


def _shell_value(root, filename):
    filename = os.fspath(filename)
    if os.path.isabs(filename):
        relative = os.path.relpath(filename, root)
        if relative != ".." and not relative.startswith("../"):
            return '"$slot_dir"/' + shlex.quote(relative)
    return shlex.quote(filename)


def _write_launcher(path, script, only_missing):
    if only_missing and os.path.lexists(path):
        return False
    if os.path.islink(path):
        raise OSError("Refusing to overwrite a linked launcher: " + path)
    if os.path.exists(path):
        with tempfile.NamedTemporaryFile(prefix=os.path.basename(path) + ".bak-",
                                         dir=os.path.dirname(path), delete=False) as backup:
            with open(path, "rb") as original:
                shutil.copyfileobj(original, backup)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                     dir=os.path.dirname(path), delete=False) as target:
        name = target.name
        try:
            target.write(script)
            target.flush()
            os.chmod(name, 0o755)
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    return True


def slot_launcher_texts(root, cfg, stop_command=None):
    """Render slot-relative macOS web, CLI, and guarded stop launchers."""
    port = int(cfg["port"])
    launchers = slot_launcher_paths(root, port)
    header = ("#!/bin/sh\n# dsh-tm port: %d\n"
              'set -eu\nslot_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n') % port
    node = cfg.get("node_exe") or slot_node_path(root)
    node = _shell_value(root, node)
    entry = _shell_value(root, cfg["bin_js"])
    env = ('node=' + node + '\n'
           'export PATH="$(dirname -- "$node"):$PATH"\n')
    if cfg.get("dsh_home"):
        env += ("export DSH_HOME=" + _shell_value(root, cfg["dsh_home"])
                + '\nmkdir -p "$DSH_HOME"\n')
    if cfg.get("temp_dir"):
        env += ('export TMPDIR=' + _shell_value(root, cfg["temp_dir"]) + '\n'
                'export TEMP="$TMPDIR" TMP="$TMPDIR"\nmkdir -p "$TMPDIR"\n')
    env += 'cd "${DSH_HOME:-$slot_dir}"\n'
    extra = cfg.get("extra_args") or ["web", "--host", "127.0.0.1", "--port", str(port), "--no-open"]
    if stop_command is None:
        stop_command = ([sys.executable, "--kill-slot"] if FROZEN else
                        [sys.executable, str(RESOURCE_DIR / "dsh_kill.py")])
    cmds = {
        "start": env + 'exec "$node" ' + entry + " " + " ".join(map(shlex.quote, extra)) + "\n",
        "cli": env + 'exec "$node" ' + entry + ' "$@"\n',
        "stop": "exec " + " ".join(map(shlex.quote, stop_command))
                + " --port %d --entrypoint " % port + entry + "\n",
    }
    return {path: header + cmds[kind] for kind, path in launchers.items()}


def write_slot_launchers(root, cfg, only_missing=False, stop_command=None):
    return [path for path, text in slot_launcher_texts(root, cfg, stop_command).items()
            if _write_launcher(path, text, only_missing)]


def bootstrap_config(path, template):
    """Create the initial config atomically without overwriting another instance."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".config-")
    try:
        with os.fdopen(fd, "wb") as file, open(template, "rb") as source:
            if IS_MAC:
                import json
                data = json.load(source)
                data["install_base"] = "<appsupport>/DSH-TM"
                file.write((json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            else:
                shutil.copyfileobj(source, file)
            file.flush()
            os.fsync(file.fileno())
        try:
            if IS_WINDOWS:
                # Windows rename is exclusive and works on FAT/exFAT.
                os.rename(temporary, path)
            else:
                os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def copy_mac_runtime(source, slot):
    if not source:
        raise OSError("No Node.js runtime was selected.")
    source = os.fspath(source)
    if os.path.isdir(source):
        source = os.path.join(source, "bin", "node")
    executable = resolve_node(source)
    npm = npm_cli_path(executable)
    if not executable or not npm:
        raise OSError("Install a native Node.js distribution with npm.")
    dest = Path(slot_node_path(slot))
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(executable, dest)
    dest.chmod(0o755)
    lib = dest.parent.parent / "lib" / "node_modules"
    npm_root = Path(npm).parent.parent
    shutil.copytree(npm_root, lib / "npm", symlinks=True, dirs_exist_ok=True)
    pnpm = Path(executable).parent.parent / "lib" / "node_modules" / "pnpm"
    if pnpm.is_dir():
        shutil.copytree(pnpm, lib / "pnpm", symlinks=True, dirs_exist_ok=True)
    for name, cli in (("npm", "npm/bin/npm-cli.js"), ("pnpm", "pnpm/bin/pnpm.cjs")):
        wrapper = dest.parent / name
        if not (lib / cli).exists() or wrapper.exists():
            continue
        wrapper.write_text('#!/bin/sh\nset -eu\nbin=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
                           'exec "$bin/node" "$bin/../lib/node_modules/' + cli + '" "$@"\n',
                           encoding="utf-8")
        wrapper.chmod(0o755)
    return str(dest)
