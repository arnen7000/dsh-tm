"""Exercise the shared Darwin paths and standalone launchers without an npm download."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dsh_platform as platform
import dsh_adopt
import dsh_install_ops


@unittest.skipUnless(sys.platform == "darwin", "macOS")
class MacPlatformTests(unittest.TestCase):
    def test_legacy_base_resolves_only_when_old_slot_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "Library/Application Support/DSH-TM"
            relative = "dsh-versions/dshV1/node_modules/@deepseek-ai/dsh/lib/bin.js"
            cfg = {"install_base": str(base),
                   "versions": {"v1": {"bin_js": "<base>/" + relative}}}
            old = base / "dsh-versions" / relative
            old.parent.mkdir(parents=True)
            old.touch()
            with patch.dict(os.environ, {"HOME": directory}):
                self.assertEqual(platform.config_base(cfg), str(base / "dsh-versions"))
            new = base / relative
            new.parent.mkdir(parents=True)
            new.touch()
            self.assertEqual(platform.config_base(cfg), str(base))

    def test_standalone_launch_is_relative_and_preserves_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "带 空格"
            root.mkdir()
            node = Path(platform.slot_node_path(root))
            node.parent.mkdir(parents=True)
            node.write_text('#!/bin/sh\nprintf "%s\\n" "$TMPDIR" "$@"\n')
            node.chmod(0o755)
            cfg = {"bin_js": str(root / "lib/bin.js"), "node_exe": str(node),
                   "dsh_home": str(root / ".dsh-home"), "temp_dir": str(root / ".tmp"),
                   "port": 49080}
            platform.write_slot_launchers(root, cfg, stop_command=["/usr/bin/false"])
            paths = platform.slot_launcher_paths(root, 49080)
            self.assertEqual(platform.read_slot_port(root), 49080)
            out = subprocess.check_output([paths["cli"], "a b", "$c"], cwd=directory,
                                          text=True, encoding="utf-8")
            self.assertEqual(out.splitlines(), [cfg["temp_dir"], cfg["bin_js"], "a b", "$c"])
            # Repair only fills missing files.
            Path(paths["start"]).write_text("custom")
            platform.write_slot_launchers(root, cfg, only_missing=True)
            self.assertEqual(Path(paths["start"]).read_text(), "custom")

    def test_adoption_copies_the_original_node_and_npm(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "已有 版本"
            modules = source / "node_modules"
            package = modules / "@deepseek-ai/dsh"
            (package / "lib").mkdir(parents=True)
            (package / "lib/bin.js").touch()
            (package / "package.json").write_text('{"version":"0.2.2-rc.1"}')
            node = Path(platform.slot_node_path(source))
            node.parent.mkdir(parents=True)
            node.write_text("#!/bin/sh\nexit 0\n")
            node.chmod(0o755)
            npm = source / "node/lib/node_modules/npm/bin/npm-cli.js"
            npm.parent.mkdir(parents=True)
            npm.write_text("// original npm")
            config = base / "config.json"
            config.write_text('{"versions":{}}')
            self.assertEqual(dsh_adopt.source_node_exe(modules), str(node))
            with patch.object(dsh_adopt, "suggest_port", return_value=49080):
                ok, key, message = dsh_install_ops.adopt_existing(
                    dsh_adopt, str(modules), str(base / "slots"), str(config),
                    "v1", [], node_exe_source="/bad/fallback", install_base=directory)
            self.assertTrue(ok, message)
            slot = dsh_install_ops.read_config(config)["versions"][key]
            copied = Path(platform.resolve_path(slot["node_exe"], directory))
            self.assertEqual(copied.read_bytes(), node.read_bytes())
            self.assertEqual(Path(platform.npm_cli_path(copied)).read_bytes(), npm.read_bytes())
            self.assertTrue((copied.parent.parent.parent / "dsh.command").is_file())

    def test_slot_temp_overrides_inherited_tmpdir(self):
        env = platform.slot_env({"temp_dir": "/slot tmp", "dsh_home": "/slot home"},
                                {"TMPDIR": "/wrong", "PATH": "/usr/bin"})
        self.assertEqual(env["TMPDIR"], "/slot tmp")
        self.assertEqual(env["TMP"], "/slot tmp")
        self.assertEqual(env["DSH_HOME"], "/slot home")


if __name__ == "__main__":
    unittest.main()
