"""Recovery checks use a real local HTTP server and a disposable child process."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import plugin
from account_manager.api import Api
from account_manager.client import Client
from account_manager.contract import Contract
from account_manager.errors import ApiError, UserError
from account_manager.service import Service
from account_manager.state import State
from tests.support import Fixture, MAPPING, mock_server


class Recovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = Fixture()
        server = mock_server(self.fixture)
        self.base_url = server.__enter__()
        self.addCleanup(server.__exit__, None, None, None)
        self.api = Api(Client(self.base_url, "synthetic-test-token", timeout=1))
        self.state = State(self.temp.name)
        self.addCleanup(self.state.close)
        self.contract = Contract(mapping=copy.deepcopy(MAPPING))
        self.service = Service(self.api, self.contract, self.state)
        self.snapshot = self.service.snapshot("rule-A")

    def preview(self):
        self.service.select(self.snapshot, "emby-0", True)
        return self.service.preview_binding(self.snapshot, "pool-A")[0]

    def qr(self, account_id="pool-A"):
        preview_id, _ = self.service.preview_maintenance(self.snapshot, "qr_start", account_id)
        result = self.service.execute_maintenance(self.snapshot, preview_id, self.temp.name)
        return result["qr_image"]

    def entry_env(self):
        contract_path = Path(self.temp.name) / "synthetic-contract.json"
        contract_path.write_text(json.dumps(MAPPING), encoding="utf-8")
        return {"TGTO_OPENAPI_TOKEN": "synthetic-test-token", "TGTO_ACCOUNT_CONTRACT": str(contract_path)}

    def test_postwrite_read_failure_stays_unknown_until_reconciled(self):
        preview_id = self.preview()
        original = self.api.users
        def fail_after_write(*args):
            if any(c[1].endswith("/assign") for c in self.fixture.calls):
                raise ApiError("读取暂不可用", uncertain=False)
            return original(*args)
        with patch.object(self.api, "users", side_effect=fail_after_write):
            result = self.service.execute_binding(self.snapshot, preview_id)
        self.assertEqual(result[0]["status"], "unknown")
        self.service.select(self.snapshot, "emby-0", True)
        with self.assertRaises(UserError):
            self.service.preview_binding(self.snapshot, "pool-A")
        self.assertEqual(self.service.reconcile()[0]["status"], "success")
        self.assertEqual(len([c for c in self.fixture.calls if c[1].endswith("/assign")]), 1)

    def test_noop_assignment_reads_again_and_logs_the_result(self):
        self.fixture.users[0]["bindings"] = [{"provider": "115", "cloud_account_id": "pool-A", "display_name": "pool-A"}]
        self.snapshot = self.service.snapshot("rule-A")
        preview_id = self.preview()
        with patch.object(self.service, "_current_binding", return_value="pool-B"):
            result = self.service.execute_binding(self.snapshot, preview_id)
        self.assertEqual(result[0]["status"], "failed")
        self.assertFalse(any(c[0] == "POST" for c in self.fixture.calls))
        self.assertEqual(self.state.history(self.service.scope)[0]["result"], "failed")

    def test_entry_keeps_confirmed_rows_when_final_refresh_fails(self):
        preview_id = self.preview()
        original = Service.snapshot
        snapshots = 0
        def fail_second_snapshot(service, rule_id):
            nonlocal snapshots
            snapshots += 1
            if snapshots == 2:
                raise ApiError("刷新暂不可用")
            return original(service, rule_id)
        with patch.dict("os.environ", self.entry_env()), patch.object(Service, "snapshot", new=fail_second_snapshot):
            result = plugin.run({"base_url": self.base_url, "rule_id": "rule-A", "action": "confirm_binding",
                                 "preview_id": preview_id}, {"data_dir": self.temp.name})
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertIn("列表刷新失败", serialized)
        self.assertIn("逐项结果", serialized)
        self.assertIn("成功", serialized)

    def test_qr_persists_across_process_state_and_status_checks(self):
        image = self.qr()
        self.state.target(self.service.scope, "pool-A")
        second = State(self.temp.name)
        try:
            self.assertEqual(second.qr_image(self.service.scope, "pool-A"), image)
        finally:
            second.close()
        self.fixture.qr_states["pool-A"] = 1
        with patch.dict("os.environ", self.entry_env()):
            result = plugin.run({"base_url": self.base_url, "rule_id": "rule-A", "action": "qr_status"},
                                {"data_dir": self.temp.name})
        self.assertIn(image, json.dumps(result))
        self.assertIn("已扫码", json.dumps(result, ensure_ascii=False))

    def test_expired_qr_clears_only_the_selected_account_image(self):
        first = self.qr("pool-A")
        second = self.qr("pool-B")
        self.fixture.qr_states["pool-A"] = -1
        self.assertEqual(self.service.qr_status(self.snapshot, "pool-A"), "expired")
        self.assertIsNone(self.state.qr_image(self.service.scope, "pool-A"))
        self.assertFalse((Path(self.temp.name) / first.removeprefix("data://")).exists())
        self.assertEqual(self.state.qr_image(self.service.scope, "pool-B"), second)

    def test_qr_retention_cleans_stale_images_and_preserves_other_files(self):
        image = self.qr()
        keep = Path(self.temp.name) / "other-plugin-owned-image.png"
        keep.write_bytes(b"do not remove")
        with self.state.db:
            self.state.db.execute("UPDATE qr_images SET created=0")
        self.state.cleanup_qr()
        self.assertIsNone(self.state.qr_image(self.service.scope, "pool-A"))
        self.assertFalse((Path(self.temp.name) / image.removeprefix("data://")).exists())
        self.assertEqual(keep.read_bytes(), b"do not remove")

    def test_qr_replacement_is_scoped_by_server_and_rule(self):
        first = self.qr()
        other_scope = "another-server-or-rule"
        path = Path(self.temp.name) / ("login-qr-" + "a" * 32 + ".png")
        path.write_bytes(b"synthetic second image")
        self.state.remember_qr(other_scope, "pool-A", path.name)
        replacement = self.qr()
        self.assertNotEqual(first, replacement)
        self.assertTrue(path.exists())
        self.assertEqual(self.state.qr_image(other_scope, "pool-A"), "data://" + path.name)

    def test_corrupt_qr_reference_cannot_escape_plugin_data(self):
        with self.state.db:
            self.state.db.execute("INSERT INTO qr_images VALUES(?,?,?,?,?)",
                                  (self.service.scope, "pool-A", "../keep.png", 0, 0))
        with self.assertRaises(UserError):
            self.state.cleanup_qr()

    def test_stale_preview_cannot_be_claimed_after_validation(self):
        preview_id = self.preview()
        original = self.state.load_preview
        def expires_during_claim(*args):
            result = original(*args)
            with self.state.db:
                self.state.db.execute("UPDATE previews SET created=0 WHERE id=?", (preview_id,))
            return result
        with patch.object(self.state, "load_preview", side_effect=expires_during_claim):
            with self.assertRaises(UserError):
                self.state.claim(self.service.scope, preview_id)

    def test_each_account_keeps_its_own_qr_authorization_state(self):
        self.qr("pool-A")
        self.qr("pool-B")
        self.fixture.qr_states["pool-A"] = 2
        self.assertEqual(self.service.qr_status(self.snapshot, "pool-A"), "authorized")
        self.assertEqual(self.service.qr_status(self.snapshot, "pool-B"), "waiting")
        with self.assertRaises(UserError):
            self.service.preview_maintenance(self.snapshot, "qr_complete", "pool-B")
        preview_id, _ = self.service.preview_maintenance(self.snapshot, "qr_complete", "pool-A")
        self.service.execute_maintenance(self.snapshot, preview_id, self.temp.name)
        self.assertIsNone(self.state.qr_image(self.service.scope, "pool-A"))
        self.assertIsNotNone(self.state.qr_image(self.service.scope, "pool-B"))

    def test_qr_authorization_is_rechecked_before_completing(self):
        self.qr()
        self.fixture.qr_states["pool-A"] = 2
        preview_id, _ = self.service.preview_maintenance(self.snapshot, "qr_complete", "pool-A")
        self.fixture.qr_states["pool-A"] = -1
        with self.assertRaises(UserError):
            self.service.execute_maintenance(self.snapshot, preview_id, self.temp.name)
        self.assertFalse(any(c[1].endswith("/qr/complete") for c in self.fixture.calls))
        self.assertIsNone(self.state.qr_image(self.service.scope, "pool-A"))

    def test_deleted_target_does_not_keep_showing_old_qr(self):
        image = self.qr()
        self.state.target(self.service.scope, "pool-A")
        self.fixture.accounts = [a for a in self.fixture.accounts if a["id"] != "pool-A"]
        self.service.snapshot("rule-A")
        self.assertEqual(self.state.target(self.service.scope), "")
        self.assertFalse((Path(self.temp.name) / image.removeprefix("data://")).exists())

    def test_killed_child_after_native_write_reconciles_without_replay(self):
        preview_id = self.preview()
        assigned = threading.Event()
        reading_confirmation = threading.Event()
        release = threading.Event()
        original = self.fixture.dispatch
        def gated_dispatch(method, path, query, body):
            result = original(method, path, query, body)
            if method == "POST" and path.endswith("/assign"):
                assigned.set()
            if method == "GET" and path.endswith("/existing-users") and assigned.is_set():
                reading_confirmation.set()
                release.wait(timeout=10)
            return result
        self.fixture.dispatch = gated_dispatch
        child_env = os.environ.copy()
        child_env.update(TEST_MOCK_URL=self.base_url, TEST_STATE_DIR=self.temp.name, TEST_PREVIEW_ID=preview_id)
        code = """
import os
from account_manager.api import Api
from account_manager.client import Client
from account_manager.contract import Contract
from account_manager.service import Service
from account_manager.state import State
from tests.support import MAPPING
state = State(os.environ["TEST_STATE_DIR"])
service = Service(Api(Client(os.environ["TEST_MOCK_URL"], "synthetic-test-token")),
                  Contract(mapping=MAPPING), state)
snapshot = service.snapshot("rule-A")
service.execute_binding(snapshot, os.environ["TEST_PREVIEW_ID"])
"""
        child = subprocess.Popen([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                                 env=child_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertTrue(reading_confirmation.wait(timeout=5), "child never reached post-write confirmation")
            child.kill()
            child.communicate(timeout=5)
            self.assertIsNotNone(child.poll())
        finally:
            release.set()
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
        second = State(self.temp.name)
        try:
            recovered = Service(self.api, self.contract, second)
            recovered.snapshot("rule-A")
            self.assertEqual(len(second.unresolved(recovered.scope)), 1)
            self.assertEqual(recovered.reconcile()[0]["status"], "success")
            self.assertEqual(second.unresolved(recovered.scope), [])
            self.assertEqual(len([c for c in self.fixture.calls if c[1].endswith("/assign")]), 1)
            with self.assertRaises(UserError):
                recovered.execute_binding(recovered.snapshot("rule-A"), preview_id)
        finally:
            second.close()


if __name__ == "__main__":
    unittest.main()
