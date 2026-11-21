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

DSH_VERSION = "0.2.2-rc.1"


def packaged_cli(base_args, *command):
    result = subprocess.run([*base_args, *command], capture_output=True, timeout=95)
    if result.returncode:
        raise RuntimeError("Packaged CLI %s failed (exit %d): %s" % (
            command[0], result.returncode, result.stdout.decode("utf-8", "replace")))
    return json.loads(result.stdout)


def run(app, gui=True):
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
            with (home / "gui.log").open("wb") as output:
                proc = subprocess.Popen([str(exe), "--config", str(config)],
                                        stdout=output, stderr=output)
            try:
                try:
                    code = proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                else:
                    raise RuntimeError("GUI exited %s: %s" %
                                       (code, (home / "gui.log").read_text(errors="replace")))
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=10)

        ok, key, detail = dsh_install_ops.run_official_install(
            dsh_adopt, DSH_VERSION, str(config), str(base), "v1", [])
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
        print("Packaged dsh lifecycle passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--skip-gui", action="store_true")
    options = parser.parse_args()
    run(options.app, gui=not options.skip_gui)
