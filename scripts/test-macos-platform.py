"""Exercise the shared Darwin paths and standalone launchers without an npm download."""
import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dsh_platform as platform
import dsh_adopt
import dsh_install_ops


@unittest.skipUnless(sys.platform == "darwin", "macOS")
class MacPlatformTests(unittest.TestCase):
    def test_source_version_check_distinguishes_annotations_from_regex_strings(self):
        script = Path(__file__).with_name("check-env.py").read_text(encoding="utf-8")
        definitions = [node for node in ast.parse(script).body
                       if isinstance(node, ast.FunctionDef) and node.name == "check_min_version_claim"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "dsh_install_ops.py").write_text(
                'pattern = r"(?:ETARGET|E404)"\n', encoding="utf-8")
            (root / "dsh_adopt.py").write_text(
                "def choose(value: str | None) -> str:\n    return value or ''\n",
                encoding="utf-8")
            problems = []
            namespace = {"ROOT": directory, "os": os, "MIN_PY": (3, 8), "T": lambda text: text,
                         "bad": problems.append, "ok": lambda text: None}
            exec(compile(ast.Module(body=definitions, type_ignores=[]), "check-env.py", "exec"), namespace)
            namespace["check_min_version_claim"]()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("dsh_adopt.py", problems[0])
        self.assertIn("3.10", problems[0])

    def test_zero_slot_check_does_not_warn_about_missing_slot_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / "config.json"
            config.write_text('{"versions":{}}', encoding="utf-8")
            report = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("check-env.py")),
                 "--config", str(config), "--lang", "zh"],
                env=dict(os.environ, HOME=directory), capture_output=True,
                encoding="utf-8", timeout=30,
            )
            self.assertIn("[OK]   当前没有配置 dsh 版本槽", report.stdout)
            self.assertNotIn("dsh-versions", report.stdout)

    def test_npm_timeout_also_stops_lifecycle_children(self):
        child = (
            "from pathlib import Path; import time; "
            "Path('started').touch(); time.sleep(2); Path('survived').touch()"
        )
        parent = (
            "import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(10)"
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(dsh_install_ops, "NPM_TIMEOUT_SECONDS", 1):
                rc, _ = dsh_install_ops._run_npm(sys.executable, "-c", [parent], cwd=directory)
            self.assertEqual(rc, dsh_install_ops.NPM_TIMEOUT_EXIT)
            self.assertTrue((Path(directory) / "started").is_file())
            time.sleep(1.5)
            self.assertFalse((Path(directory) / "survived").exists())

    def test_mac_view_timeout_uses_mirror_and_reports_network_failure(self):
        with patch.object(dsh_install_ops, "_run_npm", return_value=(124, "")) as run:
            ok, registry, message = dsh_install_ops._pick_registry(
                "node", "npm", "@deepseek-ai/dsh@0.2.2-rc.5")
        self.assertFalse(ok)
        self.assertEqual(registry, "")
        self.assertIn("均不可达", message)
        self.assertEqual(run.call_count, 2)
        first, second = (call.args[2] for call in run.call_args_list)
        self.assertIn("--fetch-timeout=45000", first)
        self.assertEqual(second[-2:], ["--registry", dsh_install_ops.FALLBACK_REGISTRY])

    def test_mac_preflight_rechecks_cached_version_error_on_same_registry(self):
        with patch.object(dsh_install_ops, "_run_npm",
                          side_effect=[(1, "npm ERR! code E404"), (0, "0.2.2-rc.5")]) as run:
            ok, registry, message = dsh_install_ops._pick_registry(
                "node", "npm", "@deepseek-ai/dsh@0.2.2-rc.5")
        self.assertEqual((ok, registry, message), (True, "", ""))
        first, retry = (call.args[2] for call in run.call_args_list)
        self.assertIn("--prefer-offline", first)
        self.assertNotIn("--registry", retry)
        self.assertNotIn("--prefer-offline", retry)
        self.assertIn("--prefer-online", retry)

    def test_mac_install_rechecks_transitive_version_error_once(self):
        for retry_code in (0, 1):
            with self.subTest(retry_code=retry_code), tempfile.TemporaryDirectory() as directory:
                (Path(directory) / "package.json").write_text("{}", encoding="utf-8")
                error = "npm error code ETARGET\nnpm error No matching version found for dependency@1.0.0."
                mirror = dsh_install_ops.FALLBACK_REGISTRY
                with patch.object(dsh_install_ops, "_npm_cli", return_value=("node", "npm")), \
                        patch.object(dsh_install_ops, "_pick_registry", return_value=(True, mirror, "")), \
                        patch.object(dsh_install_ops, "_run_npm",
                                     side_effect=[(1, error), (retry_code, "online result")]) as run:
                    result = dsh_install_ops._npm_install_slot("node", directory, "0.2.2-rc.5")
                self.assertEqual(result, (retry_code, "online result"))
                first, retry = (call.args[2] for call in run.call_args_list)
                self.assertIn("--prefer-offline", first)
                self.assertIn("--prefer-online", retry)
                self.assertNotIn("--prefer-offline", retry)
                self.assertEqual(retry[retry.index("--registry") + 1], mirror)
                self.assertEqual([call.kwargs["cwd"] for call in run.call_args_list],
                                 [directory, directory])

    def test_failed_staged_install_reports_cause_and_discards_partial_files(self):
        cases = (
            (124, "npm warn deprecated fixture", "超时（10 分钟）"),
            (1, "npm error fixture EIO\nnpm warn deprecated fixture", "npm error fixture EIO"),
            (0, "npm warn deprecated fixture", "没有找到 dsh 程序入口"),
        )
        for code, output, cause in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                config = base / "config.json"
                config.write_text('{"versions":{},"unknown_field":true}', encoding="utf-8-sig")
                before = config.read_bytes()
                versions = base / "dsh-versions"

                def install_partial(node, payload, version, progress):
                    (Path(payload) / "partial-download").write_bytes(b"fixture data")
                    return code, output

                with patch.object(dsh_install_ops, "_pick_node_exe", return_value="/fixture/node"), \
                        patch.object(dsh_adopt, "copy_node_exe", return_value=("/fixture/node", "")), \
                        patch.object(dsh_install_ops, "_npm_install_slot", side_effect=install_partial):
                    ok, key, message = dsh_install_ops.run_official_install(
                        dsh_adopt, "0.2.2-rc.5", str(config), directory, "v2", [])
                self.assertFalse(ok)
                self.assertEqual(key, "")
                self.assertIn(cause, message)
                self.assertNotIn("deprecated", message)
                self.assertEqual(config.read_bytes(), before)
                self.assertEqual(list(versions.iterdir()), [])

    def test_mac_npm_timeout_retries_with_mirror_once(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "package.json").write_text("{}", encoding="utf-8")
            with patch.object(dsh_install_ops, "_npm_cli", return_value=("/fixture/node", "/fixture/npm")), \
                    patch.object(dsh_install_ops, "_pick_registry", return_value=(True, "", "")), \
                    patch.object(dsh_install_ops, "_run_npm",
                                 side_effect=[(124, "only a warning"), (0, "installed")]) as run:
                rc, _ = dsh_install_ops._npm_install_slot("/fixture/node", directory, "0.2.2-rc.5")
            self.assertEqual(rc, 0)
            self.assertEqual(run.call_count, 2)
            first, retry = (call.args[2] for call in run.call_args_list)
            self.assertIn("--fetch-timeout=45000", first)
            self.assertIn("--prefer-offline", first)
            self.assertNotIn("--registry", first)
            self.assertEqual(retry[-2:], ["--registry", dsh_install_ops.FALLBACK_REGISTRY])

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
