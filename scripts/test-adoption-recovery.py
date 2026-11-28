"""Regression coverage for the portable-install adoption flow."""
import ast
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dsh_adopt as adopt
import dsh_install_ops as ops
import dsh_platform as platform


class AdoptionRecovery(unittest.TestCase):
    def setUp(self):
        # The portable fixtures use a Windows node.exe and DLLs.
        layout = patch.multiple(platform, IS_MAC=False, IS_WINDOWS=True, NODE_NAME="node.exe")
        layout.start()
        self.addCleanup(layout.stop)
        scratch = tempfile.TemporaryDirectory(prefix="dsh-adoption-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)

    def make_install(self, directory, version="0.2.0-rc.2"):
        nm = directory / "node_modules"
        package = nm / "@deepseek-ai/dsh"
        (package / "lib").mkdir(parents=True)
        (package / "lib/bin.js").touch()
        (package / "package.json").write_text(
            json.dumps({"name": "@deepseek-ai/dsh", "version": version}), encoding="utf-8")
        return nm

    def test_search_parent_and_precise_paths(self):
        users = self.root / "Users/_example"
        first = self.make_install(users / "dsh-01")
        second = self.make_install(users / "dsh-02", "0.1.5-rc.2")
        same_version = self.make_install(users / "项目 with spaces/dsh-copy")
        for selection in (self.root, users, first.parent, first, first / "@deepseek-ai/dsh",
                          first / "@deepseek-ai"):
            with self.subTest(selection=selection):
                found, inaccessible, cancelled = adopt.search_installations(selection)
                paths = {Path(hit["node_modules"]) for hit in found}
                self.assertEqual(paths, {first, second, same_version} if selection in
                                 (self.root, users) else {first})
                self.assertEqual(inaccessible, 0)
                self.assertFalse(cancelled)
        found, _, cancelled = adopt.search_installations(users, cancelled=lambda: True)
        self.assertEqual(found, [])
        self.assertTrue(cancelled)

    def test_search_excludes_managed_installations_and_ignores_link_targets(self):
        outside = self.make_install(self.root / "outside/dsh-02")
        scope = self.root / "scope"
        scope.mkdir()
        inside = self.make_install(scope / "nested/dsh")
        try:
            (scope / "link").symlink_to(outside.parent.parent, target_is_directory=True)
        except OSError:
            pass  # Some Windows hosts restrict creation of directory links.
        found, _, _ = adopt.search_installations(scope, managed_roots=[inside.parent])
        self.assertEqual(found, [])

    def make_node(self, directory, body=b"node"):
        directory.mkdir(parents=True, exist_ok=True)
        exe = directory / "node.exe"
        exe.write_bytes(body)
        return exe

    def test_adoption_prefers_source_node_and_preserves_sibling_data(self):
        directory = self.root / "Users/_example/dsh-01"
        nm = self.make_install(directory)
        self.make_node(directory / "node", b"portable")
        home = directory.with_name("dsh-01-home")
        home.mkdir()
        (home / "session.json").write_text("first slot", encoding="utf-8")
        config = self.root / "config.json"
        config.write_text('{"versions": {}}', encoding="utf-8")
        fallback = self.make_node(self.root / "fallback", b"unrelated runtime")
        with patch.object(adopt, "suggest_port", return_value=4080):
            ok, key, detail = ops.adopt_existing(
                adopt, str(nm), str(self.root / "slots"), str(config), "v1", [],
                node_exe_source=str(fallback), install_base=str(self.root))
        self.assertTrue(ok, detail)
        self.assertEqual(key, "v1")
        target = self.root / "slots/dshV1"
        self.assertEqual((target / "node/node.exe").read_bytes(), b"portable")
        self.assertEqual((target / ".dsh-home/session.json").read_text(), "first slot")
        self.assertEqual((home / "session.json").read_text(), "first slot")

    def test_backups_created_in_same_second_remain_separate(self):
        backup = self.root / "backup"
        sources = [self.root / name for name in ("dsh-01-home", "dsh-02-home")]
        for source in sources:
            source.mkdir()
            (source / "session.json").write_text(source.name, encoding="utf-8")
        backups = [adopt.backup_dsh_home(str(source), str(backup), stamp="same")[0]
                   for source in sources]
        self.assertNotEqual(*backups)
        self.assertEqual([Path(path, "session.json").read_text() for path in backups],
                         [source.name for source in sources])

    def test_fresh_install_uses_a_private_copy_of_path_node(self):
        path_node = self.make_node(self.root / "system", b"system")
        config = self.root / "config.json"
        config.write_text('{"versions": {}}', encoding="utf-8")

        def install(node, root, version, progress=None):
            self.assertEqual(Path(node), Path(root) / "node/node.exe")
            self.assertEqual(Path(node).read_bytes(), b"system")
            self.make_install(Path(root), version)
            return 0, ""

        with patch.object(ops, "bundled_node_exe", return_value=""), \
                patch.object(ops.shutil, "which", return_value=str(path_node)), \
                patch.object(adopt, "suggest_port", return_value=4080), \
                patch.object(ops, "_npm_install_slot", side_effect=install):
            ok, key, detail = ops.run_official_install(
                adopt, "0.2.2-rc.3", str(config), str(self.root), "v1", [])
        self.assertTrue(ok, detail)
        self.assertEqual(ops.read_config(str(config))["versions"][key]["node_exe"],
                         "<base>/dsh-versions/dshV1/node/node.exe")

    def test_legacy_path_slot_pins_current_binary_and_keeps_data_and_launchers(self):
        slot = self.root / "dsh-versions/dshV1"
        package = self.make_install(slot) / "@deepseek-ai/dsh"
        home = slot / ".dsh-home"
        home.mkdir()
        data = home / "session.json"
        data.write_bytes(b"my session")
        script = slot / "dsh.bat"
        script.write_bytes(b"custom script")
        config = self.root / "config.json"
        ops.write_config_atomic(str(config), {"versions": {"v1": {
            "node_exe": "node", "bin_js": str(package / "lib/bin.js"), "unknown": True}}})
        system = self.make_node(self.root / "system", b"same binary used before")
        with patch.object(ops.shutil, "which", return_value=str(system)), \
                patch.object(ops.os, "link", side_effect=OSError("No hardlinks supported")):
            self.assertTrue(ops.pin_path_node(str(config), "v1", str(slot), str(self.root)))
            self.assertFalse(ops.pin_path_node(str(config), "v1", str(slot), str(self.root)))
        saved = ops.read_config(str(config))["versions"]["v1"]
        self.assertTrue(saved["unknown"])
        self.assertEqual(saved["node_exe"], "<base>/dsh-versions/dshV1/node/node.exe")
        self.assertEqual((slot / "node/node.exe").read_bytes(), system.read_bytes())
        self.assertEqual(data.read_bytes(), b"my session")
        self.assertEqual(script.read_bytes(), b"custom script")

    def test_copy_keeps_node_dlls_and_excludes_unrelated_programs(self):
        runtime = self.root / "runtime"
        exe = self.make_node(runtime)
        (runtime / "node.dll").write_bytes(b"needed by node")
        (runtime / "unrelated.exe").write_bytes(b"other application")
        target = self.root / "newslot"
        copied, error = adopt.copy_node_exe(str(exe), str(target))
        self.assertTrue(copied, error)
        self.assertEqual((target / "node/node.dll").read_bytes(), b"needed by node")
        self.assertFalse((target / "node/unrelated.exe").exists())

    def test_fix_rejects_a_different_existing_node_and_keeps_configuration(self):
        slot = self.root / "dshV1"
        nm = self.make_install(slot)
        existing = self.make_node(slot / "node", b"another Node")
        system = self.make_node(self.root / "system", b"current PATH Node")
        config = self.root / "config.json"
        ops.write_config_atomic(str(config), {"versions": {"v1": {
            "node_exe": "node", "bin_js": str(nm / "@deepseek-ai/dsh/lib/bin.js")}}})
        before = config.read_bytes()
        with patch.object(ops.shutil, "which", return_value=str(system)):
            with self.assertRaises(OSError):
                ops.pin_path_node(str(config), "v1", str(slot), str(self.root))
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(existing.read_bytes(), b"another Node")

    def test_gui_adoption_sends_one_completion_and_refreshes_before_the_next_item(self):
        source = (Path(__file__).resolve().parents[1] / "dsh_manager.pyw").read_text(encoding="utf-8")
        nodes = [node for node in ast.parse(source).body
                 if isinstance(node, ast.FunctionDef) and node.name == "_adopt_one"]
        calls = []
        callbacks = []
        refreshed = []
        progress = []

        class Progress:
            def on_step(self, text):
                pass
            def on_done(self, *args):
                pass

        class Recorder(Progress):
            def __init__(self, title, done):
                self.done = done
                progress.append(self)
            def on_done(self, ok, payload, error=""):
                calls.append((ok, payload))
                self.done(ok, payload, error)

        class Operations:
            @staticmethod
            def adopt_existing(*args, progress=None, **kwargs):
                progress.on_done(True, {"slot_key": "v1"})
                return True, "v1", "adopted"
            @staticmethod
            def run_in_thread(work, gui, done):
                ok, payload, message = work()
                gui.on_done(ok, payload, message)

        from types import SimpleNamespace
        Operations.Progress = Progress
        env = dict(T=lambda text: text, OPS=Operations, dsh_adopt=adopt,
                   _next_free_slot_key=lambda: "v1", _pick_node_source=lambda tag: "",
                   _adopt_loc_nm=lambda loc: "node_modules", _actions_enabled=lambda *_: None,
                   _versions_root=lambda: "", CONFIG_PATH="", _taken_ports=lambda: [],
                   MAX_SLOTS=6, INSTALL_BASE="", _temp_root=lambda: "",
                   log=lambda *_: None, _GuiProgress=Recorder, VC={"v1": {"port": 4080}},
                   _restart_or_tell=lambda *args: refreshed.append(args),
                   messagebox=SimpleNamespace(showinfo=lambda *args: None,
                                              showerror=lambda *args: None),
                   root=SimpleNamespace(after_idle=lambda callback: callbacks.append(callback)))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<adopt>", "exec"), env)
        finished = []
        env["_adopt_one"]("0.2.0", {}, on_done=lambda: finished.append(True))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(refreshed), 1)
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(finished, [True])


if __name__ == "__main__":
    unittest.main()
