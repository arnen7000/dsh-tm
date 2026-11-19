#!/usr/bin/env python3
"""Exercise slot ownership and batch operations with real Node listeners."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
if ROOT.name == "scripts":
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))
NODE = os.environ.get("DSH_TEST_NODE") or shutil.which("node")
TOKEN = "fixture-token-testing-12345"
SERVER = """
const server = require('net').createServer();
const port = Number(process.argv[process.argv.indexOf('--port') + 1]);
server.listen(port, '127.0.0.1', () => {
    if (process.env.DSH_TEST_LOG_TOKEN === '1') {
        process.stdout.write('dsh web: http://127.0.0.1:' + port + '/?to');
        setTimeout(() => process.stdout.write('ken=fixture-token-testing-12345\\n'), 15);
    }
});
"""


@unittest.skipUnless(NODE, "Node.js is needed for the process integration tests")
class SlotProcessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="dsh tm test ")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.entry = self.root / "slot A" / "bin.js"
        self.decoy = self.root / "slot B" / "bin.js"
        for path in self.entry, self.decoy:
            path.parent.mkdir()
            path.write_text(SERVER, encoding="utf-8")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "versions": {
                "v1": {"node_exe": str(NODE), "bin_js": str(self.entry), "port": self.port},
            }
        }), encoding="utf-8")

    def wait_until(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("condition did not become true within five seconds")
            time.sleep(0.05)

    def reachable(self):
        return self.port_reachable(self.port)

    @staticmethod
    def port_reachable(port):
        with socket.socket() as connection:
            return connection.connect_ex(("127.0.0.1", port)) == 0

    def start_external(self, entrypoint, *args, cwd=None):
        proc = subprocess.Popen(
            [str(NODE), str(entrypoint), "web", "--port", str(self.port), *args],
            cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)

        self.addCleanup(cleanup)
        self.wait_until(self.reachable)
        return proc

    def cli(self, *args):
        result = subprocess.run(
            [sys.executable, str(ROOT / "dsh-tm.py"), "--config", str(self.config), *args, "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        return result, json.loads(result.stdout)

    def test_cli_does_not_report_other_slot_as_running(self):
        with_decoy_path = self.start_external(self.decoy, "--logfile", str(self.entry))
        result, payload = self.cli("status", "v1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["slots"][0]["state"], "stopped")
        self.assertTrue(payload["slots"][0]["port_conflict"])
        self.assertIsNone(with_decoy_path.poll())

    def test_cli_start_and_stop_refuse_other_slot(self):
        decoy = self.start_external(self.decoy, "--logfile", str(self.entry))
        for command in "start", "stop":
            with self.subTest(command=command):
                result, payload = self.cli(command, "v1")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(payload["slots"][0]["error_code"], "port_conflict")
                self.assertIsNone(decoy.poll())

    def test_batch_launches_six_slots_and_reports_a_foreign_port(self):
        import dsh_slot_process as slot
        config = json.loads(self.config.read_text(encoding="utf-8"))
        versions = config["versions"]
        decoy = self.start_external(self.decoy)
        with ExitStack() as reserved:
            for number in range(2, 7):
                listener = reserved.enter_context(socket.socket())
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
                entry = self.root / ("槽 %d" % number) / "bin.js"
                entry.parent.mkdir()
                entry.write_text(SERVER, encoding="utf-8")
                versions["v%d" % number] = {"node_exe": str(NODE), "bin_js": str(entry), "port": port}
        self.config.write_text(json.dumps(config), encoding="utf-8")
        for cfg in versions.values():
            self.addCleanup(slot.stop_slot, cfg["bin_js"], cfg["port"])

        result, payload = self.cli("start", "--all", "--wait", "30")
        self.assertEqual(result.returncode, 1, (result.stderr, payload))
        rows = {row["key"]: row for row in payload["slots"]}
        self.assertEqual(rows["v1"]["error_code"], "port_conflict")
        self.assertCountEqual(payload["changed"], list(versions)[1:])
        self.assertIsNone(decoy.poll())

        decoy.terminate()
        decoy.wait(timeout=5)
        result, payload = self.cli("start", "v1", "--wait", "30")
        self.assertEqual(result.returncode, 0, (result.stderr, payload))
        result, status = self.cli("status", "--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(status["count"], 6)
        self.assertTrue(all(row["state"] == "running" for row in status["slots"]), status)
        result, stopped = self.cli("stop", "--all")
        self.assertEqual(result.returncode, 0, (result.stderr, stopped))
        self.assertCountEqual(stopped["changed"], versions)
        self.assertFalse(any(self.port_reachable(cfg["port"]) for cfg in versions.values()))

    @unittest.skipIf(sys.platform == "win32", "POSIX exposes the running working directory")
    def test_relative_entrypoint_cannot_be_inferred_after_process_changes_directory(self):
        import dsh_slot_process as slot
        self.decoy.write_text("process.chdir(" + json.dumps(str(self.entry.parent)) + ");\n" + SERVER,
                              encoding="utf-8")
        decoy = self.start_external(self.decoy.name, cwd=self.decoy.parent)
        self.assertEqual(slot.slot_state(self.entry, self.port), "conflict")
        self.assertEqual(slot.stop_slot(self.entry, self.port), 0)
        self.assertIsNone(decoy.poll())

    def test_stop_matches_entrypoint_and_port(self):
        proc = self.start_external(self.entry)
        result, payload = self.cli("stop", "v1")
        self.assertEqual(result.returncode, 0, (result.stderr, payload))
        self.wait_until(lambda: proc.poll() is not None)

    def test_legacy_string_port_still_matches_listener(self):
        import dsh_slot_process as slot
        proc = self.start_external(self.entry)
        self.assertEqual(slot.slot_state(self.entry, str(self.port)), "owned")
        self.assertEqual(slot.stop_slot(self.entry, str(self.port)), 1)
        self.wait_until(lambda: proc.poll() is not None)

    def test_stop_rechecks_creation_identity_before_signaling(self):
        import dsh_slot_process as slot
        proc = self.start_external(self.entry)
        current = slot.process_info(proc.pid)
        self.assertIsNotNone(current)
        stale = slot.Process(proc.pid, "previous-creation", current.executable, current.argv)
        with patch.object(slot, "_matching_listeners",
                          return_value=({proc.pid: stale}, {proc.pid})):
            self.assertEqual(slot.stop_slot(self.entry, self.port), 0)
        self.assertIsNone(proc.poll())

    def test_unreadable_node_identity_is_a_conflict(self):
        import dsh_slot_process as slot
        proc = self.start_external(self.entry)
        with patch.object(slot, "process_info", return_value=None):
            self.assertEqual(slot.slot_state(self.entry, self.port), "conflict")
            self.assertEqual(slot.stop_slot(self.entry, self.port, tracked=proc.pid), 0)
        self.assertIsNone(proc.poll())

    def test_supervisor_redacts_split_token_and_captures_url_in_memory(self):
        import dsh_slot_process as slot
        log_path = self.root / "manager.log"
        proc = slot.spawn_slot(
            NODE, self.entry, self.port, env={**os.environ, "DSH_TEST_LOG_TOKEN": "1"},
            cwd=self.root, output_path=log_path,
        )
        self.addCleanup(lambda: slot.stop_slot(self.entry, self.port, tracked=proc))
        self.wait_until(lambda: proc.url is not None)
        self.assertIn(TOKEN, proc.url)
        self.wait_until(lambda: log_path.exists() and "token=" in log_path.read_text())
        self.assertNotIn(TOKEN, log_path.read_text())
        self.assertEqual(slot.slot_state(self.entry, self.port), "owned")
        self.assertEqual(slot.stop_slot(self.entry, self.port, tracked=proc), 1)
        proc.wait(timeout=5)
        self.assertFalse(self.reachable())


if __name__ == "__main__":
    unittest.main()
