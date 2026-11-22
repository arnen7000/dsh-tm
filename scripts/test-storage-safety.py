"""Regression tests for slot filesystem operations."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dsh_move as move
import dsh_platform as platform
import dsh_uninstall_ops as uninstall


class SlotFilesystemTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name).resolve()
        self.base = self.work / "original"
        self.dst = self.work / "new"
        self.slots = self.base / "dsh-versions"
        self.slot = self.slots / "dshV1"
        self.home = self.slot / ".dsh-home"
        self.home.mkdir(parents=True)
        self.data = self.home / "session"
        self.data.write_text("user data", encoding="utf-8")
        self.config_path = self.work / "config.json"
        self.config = {
            "install_base": str(self.base),
            "detached": [{"root": str(self.slots / "dshV2")}],
            "versions": {"v1": {
                "bin_js": "<base>/dsh-versions/dshV1/node_modules/@deepseek-ai/dsh/lib/bin.js",
                "dsh_home": "<base>/dsh-versions/dshV1/.dsh-home",
                "node_exe": str(self.base) + "-neighbor/node",
                "temp_dir": "<base>/local-temp",
                "port": 4080,
            }},
        }
        self.write_config()

    def write_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def symlink(self, path, target, directory=False):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            self.skipTest(f"Creating a symbolic link requires permission: {exc}")

    def uninstall_slot(self, stop_fn=None):
        return uninstall.uninstall_slots(
            ["v1"], str(self.config_path), str(self.work), str(self.work),
            str(self.work), versions_root=str(self.slots), stop_fn=stop_fn,
        )

    def move_plan(self, slots=("dshV1",)):
        return move.plan_move(str(self.base), str(self.dst), slots)

    def test_unlink_top_level_directory_symlink_without_touching_target(self):
        target = self.work / "elsewhere"
        target.mkdir()
        child = target / "keep"
        child.write_text("outside", encoding="utf-8")
        link = self.slot / "external"
        self.symlink(link, target, directory=True)

        uninstall._force_rmtree(str(link))

        self.assertFalse(link.is_symlink())
        self.assertEqual(child.read_text(encoding="utf-8"), "outside")

    def test_deleting_nested_symlink_preserves_target_mode(self):
        target = self.work / "elsewhere"
        target.write_text("outside", encoding="utf-8")
        original_mode = target.stat().st_mode
        self.symlink(self.home / "external", target)

        uninstall._force_rmtree(str(self.home))

        self.assertEqual(target.stat().st_mode, original_mode)
        self.assertEqual(target.read_text(encoding="utf-8"), "outside")

    def test_containment_respects_case_insensitive_macos_volumes(self):
        if sys.platform != "darwin":
            self.skipTest("macOS filesystem semantics")
        folder = self.slot / "cache files"
        folder.mkdir()
        child = folder / "state.txt"
        child.touch()
        alias = self.slot / "CACHE FILES"
        if not alias.is_dir():
            self.skipTest("Case-sensitive volume")
        self.assertTrue(uninstall._path_within(child, alias, strict=True))
        self.assertFalse(uninstall._path_within(folder, alias, strict=True))
        self.assertFalse(uninstall.is_safe_to_delete(
            str(child), str(self.slot), [str(alias)], str(self.work), str(self.work))[0])
        self.assertEqual(
            move._moved_path(str(child), [(str(alias), str(self.dst))]),
            str(self.dst / child.name),
        )

    def test_uninstall_rejects_escape_and_symlink_ancestor(self):
        target = self.work / "elsewhere"
        target.mkdir()
        (target / "keep").touch()
        self.symlink(self.slot / "external", target, directory=True)
        sibling = self.slots / "dshV2"
        sibling.mkdir()
        for child in (str(self.slot) + "/../dshV2",
                      str(self.slot / "external" / "keep")):
            with self.subTest(child=child):
                allowed, _ = uninstall.is_safe_to_delete(
                    child, str(self.slot), [], str(self.work), str(self.work))
                self.assertFalse(allowed)

    def test_uninstall_protects_a_detached_slot_nested_under_the_selected_slot(self):
        nested = self.slot / "nested"
        nested.mkdir()
        sentinel = nested / "keep"
        sentinel.touch()
        self.config["detached"] = [{"root": str(nested), "at": "earlier"}]
        self.write_config()

        ok, payload, _ = self.uninstall_slot()

        self.assertFalse(ok)
        self.assertEqual(payload["kept_in_config"], ["v1"])
        self.assertTrue(sentinel.exists())
        self.assertEqual(json.loads(self.config_path.read_text()), self.config)

    def test_uninstall_never_deletes_unbacked_legacy_default_data(self):
        self.config["versions"]["v1"].pop("dsh_home")
        self.write_config()

        ok, payload, message = self.uninstall_slot()

        self.assertTrue(ok, message)
        backup = Path(payload["details"][0]["backup"])
        self.assertEqual((backup / "session").read_text(), "user data")

    def test_stop_failure_leaves_files_and_config(self):
        def cannot_stop(key, port):
            raise OSError("process is still listening")

        ok, payload, _ = self.uninstall_slot(stop_fn=cannot_stop)
        self.assertFalse(ok)
        self.assertEqual(payload["kept_in_config"], ["v1"])
        self.assertEqual(self.data.read_text(encoding="utf-8"), "user data")
        self.assertEqual(json.loads(self.config_path.read_text()), self.config)

    def test_corrupted_same_size_backup_leaves_files_and_config(self):
        copy_file = shutil.copy2

        def corrupt_copy(source, destination, *args, **kwargs):
            result = copy_file(source, destination, *args, **kwargs)
            if Path(source) == self.data:
                Path(result).write_text("bad bytes", encoding="utf-8")
            return result

        with patch.object(uninstall, "_robocopy", return_value=(False, "")):
            with patch.object(uninstall.shutil, "copy2", side_effect=corrupt_copy):
                ok, payload, _ = self.uninstall_slot()
        self.assertFalse(ok)
        self.assertEqual(payload["kept_in_config"], ["v1"])
        self.assertEqual(self.data.read_text(encoding="utf-8"), "user data")
        self.assertEqual(json.loads(self.config_path.read_text()), self.config)

    def test_move_preserves_external_internal_and_dangling_symlinks(self):
        target = self.work / "external"
        target.mkdir()
        child = target / "keep"
        child.write_text("outside", encoding="utf-8")
        self.symlink(self.home / "external", target, directory=True)
        self.symlink(self.home / "internal", "session")
        self.symlink(self.home / "dangling", "unavailable")

        ok, message = move.move_now(self.move_plan(), str(self.config_path))

        self.assertTrue(ok, message)
        self.assertFalse(self.slot.exists())
        new_home = self.dst / "dsh-versions" / "dshV1" / ".dsh-home"
        self.assertEqual((new_home / "external" / "keep").read_text(), "outside")
        self.assertEqual(os.readlink(new_home / "internal"), "session")
        self.assertEqual(os.readlink(new_home / "dangling"), "unavailable")
        self.assertEqual(child.read_text(), "outside")

    def test_move_rewrites_only_paths_that_move(self):
        ok, message = move.move_now(self.move_plan(), str(self.config_path))

        self.assertTrue(ok, message)
        after = json.loads(self.config_path.read_text())
        self.assertEqual(after["detached"], self.config["detached"])
        before_slot = self.config["versions"]["v1"]
        after_slot = after["versions"]["v1"]
        self.assertEqual(after_slot["node_exe"], before_slot["node_exe"])
        self.assertEqual(Path(after_slot["temp_dir"]), self.base / "local-temp")
        data_home = after_slot["dsh_home"].replace("<base>", after["install_base"])
        self.assertEqual(Path(data_home), self.dst / "dsh-versions" / "dshV1" / ".dsh-home")

    @unittest.skipUnless(sys.platform == "darwin", "macOS command launchers")
    def test_move_preserves_custom_launcher_content_and_updates_moved_temp(self):
        (self.base / "dsh-temp").mkdir()
        self.config["versions"]["v1"]["temp_dir"] = "<base>/dsh-temp/中文 path"
        node = self.work / "fake node"
        node.write_text('#!/bin/sh\nprintf "%s" "$TMPDIR"\n', encoding="utf-8")
        node.chmod(0o755)
        self.config["versions"]["v1"]["node_exe"] = str(node)
        self.write_config()
        cfg = {k: v.replace("<base>", str(self.base)) if isinstance(v, str) else v
               for k, v in self.config["versions"]["v1"].items()}
        platform.write_slot_launchers(str(self.slot), cfg)
        start = self.slot / "dsh.command"
        with start.open("a", encoding="utf-8") as script:
            script.write("# Keep my comment\n")
        original_cli = self.slot / "dsh-cli.command"
        original_cli.write_text('#!/bin/sh\nprintf "custom"\n', encoding="utf-8")

        ok, message = move.move_now(self.move_plan(), str(self.config_path))

        self.assertTrue(ok, message)
        relocated = self.dst / "dsh-versions" / "dshV1"
        self.assertEqual(subprocess.check_output([relocated / "dsh.command"], text=True, encoding="utf-8"),
                         str(self.dst / "dsh-temp" / "中文 path"))
        self.assertIn("# Keep my comment", (relocated / start.name).read_text())
        self.assertEqual(subprocess.check_output([relocated / original_cli.name], text=True, encoding="utf-8"),
                         "custom")

    def test_move_rewrites_existing_windows_launcher_path_lines_only(self):
        self.config["versions"]["v1"]["temp_dir"] = "<base>/dsh-temp/中文 path"
        self.write_config()
        (self.base / "dsh-temp").mkdir()
        start = self.slot / "dsh.bat"
        entry = self.slot / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
        custom = f'REM My custom comment refers to {entry}'
        original = (f'@echo off\r\n{custom}\r\n'
                    f'set "TEMP={self.base / "dsh-temp" / "中文 path"}"\r\n'
                    f'"node" "{entry}" web --no-open --port 4080\r\n')
        start.write_bytes(original.encode("gbk"))

        with patch.object(platform, "IS_MAC", False), patch.object(platform, "IS_WINDOWS", True):
            ok, message = move.move_now(self.move_plan(), str(self.config_path))

        self.assertTrue(ok, message)
        relocated = self.dst / "dsh-versions" / "dshV1"
        updated = (relocated / start.name).read_bytes().decode("gbk")
        self.assertIn(f'{custom}\r\n', updated)
        self.assertIn(f'set "TEMP={self.dst / "dsh-temp" / "中文 path"}"\r\n', updated)
        self.assertIn(f'"node" "{relocated / entry.relative_to(self.slot)}" web', updated)

    def test_move_keeps_source_when_updating_a_copied_launcher_fails(self):
        start = self.slot / "dsh.bat"
        source = f'@echo off\r\nset "DSH_HOME={self.home}"\r\n'.encode("gbk")
        start.write_bytes(source)
        original_config = self.config_path.read_bytes()
        plan = self.move_plan()
        with patch.object(platform, "IS_MAC", False), patch.object(platform, "IS_WINDOWS", True):
            with patch.object(Path, "write_bytes", side_effect=PermissionError("launcher is read-only")):
                ok, message = move.move_now(plan, str(self.config_path))

        self.assertFalse(ok)
        self.assertIn("launcher is read-only", message)
        self.assertEqual(start.read_bytes(), source)
        self.assertEqual(self.config_path.read_bytes(), original_config)
        self.assertEqual(list(self.dst.iterdir()), [])

    def test_move_rebases_absolute_links_into_the_moved_slot(self):
        self.symlink(self.home / "absolute", self.data)

        ok, message = move.move_now(self.move_plan(), str(self.config_path))

        self.assertTrue(ok, message)
        link = self.dst / "dsh-versions" / "dshV1" / ".dsh-home" / "absolute"
        self.assertEqual(link.read_text(), "user data")

    def test_move_rolls_back_published_destinations_if_config_write_fails(self):
        prior = self.config_path.read_bytes()
        real_replace = os.replace

        def reject_config(source, destination, *args, **kwargs):
            if os.fspath(destination) == str(self.config_path):
                raise PermissionError("configuration replacement denied")
            return real_replace(source, destination, *args, **kwargs)

        plan = self.move_plan()
        with patch.object(os, "replace", side_effect=reject_config):
            ok, _ = move.move_now(plan, str(self.config_path))

        self.assertFalse(ok)
        self.assertFalse((self.dst / "dsh-versions" / "dshV1").exists())
        self.assertEqual(self.data.read_text(), "user data")
        self.assertEqual(self.config_path.read_bytes(), prior)

    def install_package_fixture(self):
        package = self.slot / "node_modules" / "@deepseek-ai" / "dsh"
        (package / "lib").mkdir(parents=True)
        (package / "lib" / "bin.js").touch()
        (package / "package.json").write_text(
            '{"name": "@deepseek-ai/dsh", "version": "0.1.0"}', encoding="utf-8"
        )

    @unittest.skipUnless(sys.platform == "darwin", "macOS runtime layout")
    def test_repair_uses_the_native_node_runtime_path(self):
        self.install_package_fixture()
        source = self.work / "node"
        source.write_text("#!/bin/sh\n", encoding="utf-8")
        source.chmod(0o755)

        ok, _, message = uninstall.repair_slot(str(self.slot), str(source))

        self.assertTrue(ok, message)
        installed = self.slot / "node" / "bin" / "node"
        self.assertEqual(installed.read_bytes(), source.read_bytes())
        self.assertTrue(os.access(installed, os.X_OK))

    @unittest.skipUnless(sys.platform == "darwin", "macOS launchers")
    def test_reattach_reads_the_command_port_and_records_the_native_runtime(self):
        self.install_package_fixture()
        node = self.slot / "node" / "bin" / "node"
        node.parent.mkdir(parents=True)
        node.touch(mode=0o755)
        launcher = self.slot / "dsh.command"
        launcher.write_text("#!/bin/sh\n# dsh-tm port: 43785\n", encoding="utf-8")
        self.config["versions"] = {}
        self.config["detached"] = [{"root": str(self.slot)}]
        self.write_config()

        ok, details, message = uninstall.readd_slots(
            [str(self.slot)], str(self.config_path), str(self.work), str(self.work),
            str(self.work), versions_root=str(self.slots), probe=lambda port: False,
        )

        self.assertTrue(ok, message)
        self.assertEqual(details["port_changed"], {})
        stored = json.loads(self.config_path.read_text())["versions"]["v1"]
        self.assertEqual(stored["port"], 43785)
        self.assertEqual(Path(stored["node_exe"].replace("<base>", str(self.base))), node)

    @unittest.skipUnless(sys.platform == "win32", "Windows PowerShell entry point")
    def test_powershell_dry_run_forwards_target_and_config_path(self):
        wrapper = Path(uninstall.__file__).parent / "scripts" / "uninstall.ps1"
        config = self.work / "alternate config.json"
        self.config_path.rename(config)
        prior = config.read_bytes()
        result = subprocess.run([
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(wrapper), "-ConfigPath", str(config), "-Target", "all", "-DryRun",
            "-SkipBackup", "-SkipGlobal",
        ], capture_output=True, timeout=30)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"[DryRun]", result.stdout)
        self.assertEqual(config.read_bytes(), prior)
        self.assertTrue(self.data.exists())

    def test_command_dry_run_performs_no_writes(self):
        prior = self.config_path.read_bytes()
        result = subprocess.run([
            sys.executable, str(Path(uninstall.__file__)),
            "--config-path", str(self.config_path), "--target", "all", "--dry-run",
        ], capture_output=True, text=True, encoding="utf-8", errors="replace",
           env=dict(os.environ, PYTHONIOENCODING="utf-8"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DryRun", result.stdout)
        self.assertEqual(self.config_path.read_bytes(), prior)
        self.assertEqual(set(self.work.iterdir()), {self.base, self.config_path})
        self.assertFalse((self.base / "dsh备份").exists())
        self.assertTrue(self.data.exists())

    def test_full_uninstall_keeps_failed_config_entries_and_successful_slot_data(self):
        second = self.slots / "dshV2"
        second_home = second / ".dsh-home"
        second_home.mkdir(parents=True)
        (second_home / "unmodified").touch()
        (self.slot / "node").mkdir()
        (self.slot / ".tmp").mkdir()
        (self.slot / "obsolete-program").touch()
        failed_config = dict(self.config["versions"]["v1"], port=5080)
        failed_config["bin_js"] = failed_config["bin_js"].replace("dshV1", "dshV2")
        failed_config["dsh_home"] = failed_config["dsh_home"].replace("dshV1", "dshV2")
        self.config["versions"]["v2"] = failed_config
        self.config.pop("detached")
        self.write_config()
        log = self.work / "dsh-manager.log"
        log.write_text("prior log", encoding="utf-8")

        with patch.object(uninstall, "_stop_cli_slot",
                          side_effect=lambda entry, port: port == 4080):
            with patch("builtins.print"):
                exit_code = uninstall._command_main([
                    "--config-path", str(self.config_path), "--target", "all",
                    "--yes", "--skip-backup",
                ])

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(self.config_path.read_text())["versions"],
                         {"v2": failed_config})
        self.assertTrue((second_home / "unmodified").exists())
        self.assertFalse((self.slot / "obsolete-program").exists())
        self.assertEqual(self.data.read_text(), "user data")
        self.assertTrue((self.slot / "node").is_dir())
        self.assertTrue((self.slot / ".tmp").is_dir())
        self.assertEqual(log.read_text(), "prior log")

    def test_invalid_config_leaves_no_destination_slot(self):
        plan = self.move_plan()
        self.config_path.write_text("non-JSON", encoding="utf-8")

        ok, _ = move.move_now(plan, str(self.config_path))

        self.assertFalse(ok)
        self.assertFalse((self.dst / "dsh-versions" / "dshV1").exists())
        self.assertTrue(self.data.exists())


if __name__ == "__main__":
    unittest.main()
