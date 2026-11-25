"""Run a clean installation and authenticated lifecycle with a packaged macOS runtime."""
import argparse
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, build_opener, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dsh_adopt
import dsh_install_ops
import dsh_platform
import dsh_slot_process
import dsh_uninstall_ops

DSH_VERSION = "0.2.2-rc.1"


def packaged_cli(base_args, *command):
    result = subprocess.run([*base_args, *command], capture_output=True, timeout=95)
    if result.returncode:
        raise RuntimeError("Packaged CLI %s failed (exit %d): %s" % (
            command[0], result.returncode, result.stdout.decode("utf-8", "replace")))
    return json.loads(result.stdout)


def verify_gui_start(exe, config):
    output_path = config.parent / "gui.log"
    manager_log = Path(dsh_platform.log_path(str(config)))
    with output_path.open("wb") as output:
        proc = subprocess.Popen([str(exe), "--config", str(config)],
                                stdout=output, stderr=output)
        try:
            # The startup snapshot is written by a callback in the Tk event loop.
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and proc.poll() is None:
                if manager_log.exists():
                    log_text = manager_log.read_text(encoding="utf-8", errors="replace")
                    if "=== 启动快照结束 ===" in log_text:
                        print("Packaged GUI startup completed", flush=True)
                        return
                time.sleep(0.25)
            raise RuntimeError("GUI startup did not complete (exit %s): %s" %
                               (proc.poll(), output_path.read_text(errors="replace")))
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    code = proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
                    raise
                assert code == 0, ("Packaged manager did not quit normally", code)


def verify_running_uninstall(app, args, config, home, base, key, slot):
    slot_root = Path(slot["bin_js"]).parents[4]
    marker = Path(slot["dsh_home"]) / "coverage-session.txt"
    marker_content = "fixture session to preserve\n"
    marker.write_text(marker_content, encoding="utf-8")
    port = slot["port"]

    def stop(key_to_stop, port_to_stop):
        assert (key_to_stop, port_to_stop) == (key, port)
        assert dsh_slot_process.port_reachable(port)
        stopped = packaged_cli(args, "stop", key)
        return stopped["changed"] == [key] and not dsh_slot_process.port_reachable(port)

    try:
        started = packaged_cli(args, "start", key, "--wait", "60")
        assert started["changed"] == [key]
        ok, payload, detail = dsh_uninstall_ops.uninstall_slots(
            [key], str(config), str(home), str(app), str(app),
            versions_root=str(slot_root.parent), backup_root=str(base / "test backups"),
            stop_fn=stop, base_override=str(base))
        assert ok, detail
        assert payload["removed"] == [key] and not payload["kept_in_config"]
        assert payload["details"][0]["stopped"]
        backup = Path(payload["details"][0]["backup"])
        assert (backup / marker.name).read_text(encoding="utf-8") == marker_content
        assert not slot_root.exists()
        assert not dsh_slot_process.port_reachable(port)
        saved = json.loads(config.read_text(encoding="utf-8-sig"))
        assert saved["legacy_extension"] == {"keep": True}
        assert packaged_cli(args, "list")["count"] == 0
    finally:
        if dsh_slot_process.port_reachable(port):
            dsh_slot_process.stop_slot(slot["bin_js"], port)
    print("Running dsh was stopped, backed up and uninstalled", flush=True)


def run(app, gui=True, dsh_version=DSH_VERSION):
    app = app.resolve()
    with (app / "Contents/Info.plist").open("rb") as source:
        info = plistlib.load(source)
    assert app.name == "DSH-TM.app", app.name
    for key in ("CFBundleName", "CFBundleDisplayName", "CFBundleExecutable"):
        assert info.get(key) == "DSH-TM", (key, info.get(key))
    exe = app / "Contents/MacOS" / info["CFBundleExecutable"]
    runtime = app / "Contents/Resources/runtime/node"
    if not runtime.is_dir():
        runtime = app / "Contents/Frameworks/runtime/node"
    dsh_platform.RESOURCE_DIR = runtime.parent.parent
    with tempfile.TemporaryDirectory(prefix="dsh-mac-") as work:
        home = Path(work)
        base = home / "dsh 中文 slots"
        base.mkdir()
        config = home / "config.json"
        config.write_text(json.dumps({"install_base": str(base), "versions": {},
                                      "legacy_extension": {"keep": True}}), encoding="utf-8-sig")
        os.environ.update(HOME=work, PATH="/usr/bin:/bin:/usr/sbin:/sbin")
        report = subprocess.run([str(exe), "--check-env", "--lang", "en", "--config", str(config)],
                                capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        assert report.returncode == 0, report.stdout + report.stderr
        assert "[OK]   config.json" in report.stdout, report.stdout
        if gui:
            verify_gui_start(exe, config)

        ok, key, detail = dsh_install_ops.run_official_install(
            dsh_adopt, dsh_version, str(config), str(base), "v1", [])
        if not ok:
            raise RuntimeError(detail)
        saved = json.loads(config.read_text(encoding="utf-8-sig"))
        assert saved["legacy_extension"] == {"keep": True}
        slot = saved["versions"][key]
        for field in ("node_exe", "bin_js", "dsh_home", "temp_dir"):
            slot[field] = dsh_platform.resolve_path(slot[field], str(base))

        args = [str(exe), "--cli", "--config", str(config), "--json"]
        listed = packaged_cli(args, "list")
        assert listed["schema_version"] == 1 and listed["count"] == 1
        logfile = home / "service.log"
        port = slot["port"]
        tracked = dsh_slot_process.spawn_slot(
            slot["node_exe"], slot["bin_js"], port, env=dsh_platform.slot_env(slot),
            cwd=slot["dsh_home"], output_path=str(logfile),
            launcher=[str(exe), "--run-slot-supervisor"])
        try:
            deadline = time.monotonic() + 300
            while tracked.poll() is None and not tracked.url and time.monotonic() < deadline:
                time.sleep(0.25)
            if not tracked.url:
                tail = logfile.read_text(errors="replace").splitlines()[-25:] if logfile.exists() else []
                raise RuntimeError("No authenticated entry from dsh\n" + "\n".join(tail))

            bare = "http://127.0.0.1:%d/" % port
            try:
                with urlopen(bare, timeout=10) as response:
                    raise AssertionError("Unexpected unauthenticated status %s" % response.status)
            except HTTPError as exc:
                assert exc.code == 401
            jar = CookieJar()
            opener = build_opener(HTTPCookieProcessor(jar))
            # Do not include the URL in a diagnostic or traceback.
            try:
                with opener.open(tracked.url, timeout=15) as response:
                    assert response.status == 200 and list(jar)
            except Exception:
                raise AssertionError("Failed authenticated HTTP navigation") from None
            assert tracked.url not in logfile.read_text(errors="replace")
            packaged_cli(args, "stop", key)
            tracked.wait(timeout=20)
            assert not dsh_slot_process.port_reachable(port)
        finally:
            if tracked.poll() is None:
                dsh_slot_process.stop_slot(slot["bin_js"], port, tracked)
                tracked.wait(timeout=20)

        try:
            started = packaged_cli(args, "start", "--all", "--wait", "60")
            assert started["changed"] == [key]
            status = packaged_cli(args, "status", key)
            assert status["slots"][0]["state"] == "running"
            stopped = packaged_cli(args, "stop", "--all")
            assert stopped["changed"] == [key]
            assert not dsh_slot_process.port_reachable(port)
        finally:
            if dsh_slot_process.port_reachable(port):
                dsh_slot_process.stop_slot(slot["bin_js"], port)
        verify_running_uninstall(app, args, config, home, base, key, slot)
        print("Packaged dsh lifecycle passed for " + dsh_version)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--skip-gui", action="store_true")
    parser.add_argument("--dsh-version", default=DSH_VERSION)
    options = parser.parse_args()
    run(options.app, gui=not options.skip_gui, dsh_version=options.dsh_version)
