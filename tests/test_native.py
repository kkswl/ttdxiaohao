"""Synthetic values using field names verified from the v8.9.2 native panel."""

import copy
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import plugin
from account_manager.api import Api
from account_manager.client import Client
from account_manager.errors import ApiError, UserError
from account_manager.flow_crypto import FlowCipher
from account_manager.native import NativeContract
from account_manager.service import Service
from account_manager.state import State
from tests.support import Fixture, mock_server


def wire_account(raw):
    return {"id": raw["id"], "cloud_account_id": raw["id"], "rule_id": raw["rule_id"],
            "provider": raw["provider"], "display_name": raw["name"], "account_label": raw["masked_login"],
            "authorized": raw["logged_in"], "ready": raw["logged_in"] and raw["folder_ready"] and bool(raw["folder_id"]),
            "folder_id": raw["folder_id"], "folder_name": raw["folder_path"],
            "users": raw["users"], "user_count": raw["usage_count"], "created_at": 1, "updated_at": 1,
            "last_validated_at": 0, "status": "active" if raw["logged_in"] else "draft", "shared": True}


class NativeFixture(Fixture):
    def __init__(self):
        super().__init__()
        self.flows = {}
        self.native_folder_body = None

    def dispatch(self, method, path, query, body):
        if "/cloud-accounts/" in path:
            account_id = path.split("/cloud-accounts/", 1)[1].split("/", 1)[0]
            if "/qr/" in path:
                self.calls.append((method, path, copy.deepcopy(query), copy.deepcopy(body)))
                if path.endswith("/start"):
                    if body.get("app") not in {"web", "android", "linux"}:
                        return 400, {"success": False, "error": "登录设备无效"}
                    flow_id = "synthetic-sensitive-flow-" + account_id
                    self.flows[account_id] = flow_id
                    self.qr_states[account_id] = 0
                    return 200, {"success": True, "app": body["app"], "expires_in": 120, "flow_id": flow_id,
                                 "qr": {"qrcode": "http://example.invalid/synthetic-qr", "uid": "SENSITIVE-UID",
                                        "time": 123, "sign": "SENSITIVE-SIGN"}}
                supplied = query.get("flow_id", [""])[0] if method == "GET" else body.get("flow_id")
                if not self.flows.get(account_id) or supplied != self.flows[account_id]:
                    return 400, {"success": False, "error": "登录流程已过期或已取消，请重新发起"}
                if path.endswith("/status"):
                    return 200, {"success": True, "status": {"status": self.qr_states[account_id], "retry_after": 5}}
                if self.qr_states[account_id] != 2:
                    return 400, {"success": False, "error": "尚未确认"}
                raw = next(a for a in self.accounts if a["id"] == account_id)
                raw["logged_in"] = True
                return 200, {"success": True, "account": {"cloud_account": wire_account(raw)}}
            if path.endswith("/folders"):
                self.calls.append((method, path, query, body))
                if "parent_id" not in query:
                    return 400, {"success": False}
                return 200, {"success": True, "folders": [{"id": "999999999999999999999", "name": "Media"}]}
            if path.endswith("/folder"):
                self.calls.append((method, path, query, body))
                self.native_folder_body = body
                if set(body) != {"folder_id", "folder_name", "parent_id"}:
                    return 400, {"success": False}
                raw = next(a for a in self.accounts if a["id"] == account_id)
                raw.update(folder_id=body["folder_id"], folder_path=body["folder_name"], folder_ready=True)
                return 200, {"success": True}
        status, result = super().dispatch(method, path, query, body)
        if path == "/api/emby_proxy/cloud-accounts" and status == 200:
            result["cloud_accounts"] = [wire_account(a) for a in result["cloud_accounts"]]
        return status, result


class NativeWire(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = NativeFixture()
        server = mock_server(self.fixture)
        self.base = server.__enter__()
        self.addCleanup(server.__exit__, None, None, None)
        self.api = Api(Client(self.base, "synthetic-test-token", timeout=1))
        self.state = State(self.temp.name)
        self.addCleanup(self.state.close)
        self.contract = NativeContract()
        self.service = Service(self.api, self.contract, self.state)
        self.snapshot = self.service.snapshot("rule-A")

    def start(self, account_id="pool-A"):
        preview_id, _ = self.service.preview_maintenance(self.snapshot, "qr_start", account_id)
        return self.service.execute_maintenance(self.snapshot, preview_id, self.temp.name)

    def test_native_account_and_binding_fields(self):
        self.assertEqual(self.snapshot["accounts"][0]["name"], "pool-A")
        self.assertTrue(self.snapshot["accounts"][0]["ready"])
        self.service.select(self.snapshot, "emby-0", True)
        preview_id, _ = self.service.preview_binding(self.snapshot, "pool-A")
        self.assertEqual(self.service.execute_binding(self.snapshot, preview_id)[0]["status"], "success")
        refreshed = self.service.snapshot("rule-A")
        self.assertEqual(refreshed["users"][0]["binding"]["id"], "pool-A")

    def test_flow_handle_is_encrypted_and_reused_after_reopen(self):
        self.start()
        flow_id = self.fixture.flows["pool-A"]
        second = State(self.temp.name)
        try:
            restored = Service(self.api, self.contract, second)
            snapshot = restored.snapshot("rule-A")
            self.assertEqual(restored.qr_status(snapshot, "pool-A"), "waiting")
        finally:
            second.close()
        self.fixture.qr_states["pool-A"] = 2
        preview_id, _ = self.service.preview_maintenance(self.snapshot, "qr_complete", "pool-A")
        self.service.execute_maintenance(self.snapshot, preview_id, self.temp.name)
        self.assertIsNone(self.state.qr_flow(self.service.scope, "pool-A"))
        complete = next(c for c in self.fixture.calls if c[1].endswith("/qr/complete"))
        self.assertEqual(complete[3], {"flow_id": flow_id})
        for file in Path(self.temp.name).glob("*.sqlite3*"):
            data = file.read_bytes()
            for secret in (flow_id, "synthetic-test-token", "SENSITIVE-UID", "SENSITIVE-SIGN"):
                self.assertNotIn(secret.encode(), data)

    def test_server_expiry_overrides_local_five_minute_cap(self):
        self.start()
        row = self.state.db.execute("SELECT created,expires FROM qr_flows").fetchone()
        self.assertGreater(row["expires"] - row["created"], 118)
        self.assertLessEqual(row["expires"] - row["created"], 120)
        with self.state.db:
            self.state.db.execute("UPDATE qr_flows SET expires=0")
        self.state.cleanup_qr()
        self.assertIsNone(self.state.qr_flow(self.service.scope, "pool-A"))
        self.assertIsNone(self.state.qr_image(self.service.scope, "pool-A"))

    def test_http400_expired_flow_is_a_real_expired_state(self):
        self.start()
        self.fixture.flows.clear()
        self.assertEqual(self.service.qr_status(self.snapshot, "pool-A"), "expired")
        self.assertIsNone(self.state.qr_image(self.service.scope, "pool-A"))

    def test_directory_selection_uses_real_three_field_body(self):
        rows = self.service.folders(self.snapshot, "pool-A", "0")
        preview_id, _ = self.service.preview_maintenance(self.snapshot, "folder", "pool-A",
                            folder_id=rows[0]["id"], folder_name=rows[0]["name"], parent_id="0")
        self.service.execute_maintenance(self.snapshot, preview_id, self.temp.name)
        self.assertEqual(self.fixture.native_folder_body,
                         {"folder_id": "999999999999999999999", "folder_name": "Media", "parent_id": "0"})

    def test_api_token_form_fallback_is_not_returned(self):
        with patch.dict("os.environ", {}, clear=True):
            result = plugin.run({"base_url": self.base, "api_token": "synthetic-test-token",
                                 "rule_id": "rule-A"}, {"data_dir": self.temp.name})
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertIn("用户分配工作台", serialized)
        self.assertNotIn("synthetic-test-token", serialized)

    def test_crypto_rejects_wrong_account_scope_and_token(self):
        cipher = FlowCipher(self.base, "synthetic-test-token")
        encrypted = cipher.encrypt("sensitive-flow", "scope-A", "pool-A")
        for instance, scope, account_id in [
            (cipher, "scope-B", "pool-A"), (cipher, "scope-A", "pool-B"),
            (FlowCipher(self.base, "other-token"), "scope-A", "pool-A"),
        ]:
            with self.assertRaises(UserError):
                instance.decrypt(encrypted, scope, account_id)
        self.assertNotIn(b"sensitive-flow", encrypted)

    def test_previous_qr_table_is_migrated_without_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "account-manager.sqlite3"
            db = sqlite3.connect(database)
            db.execute("CREATE TABLE qr_images(scope TEXT,account_id TEXT,filename TEXT,created REAL,PRIMARY KEY(scope,account_id))")
            db.commit()
            db.close()
            migrated = State(directory)
            try:
                self.assertIn("expires", [row[1] for row in migrated.db.execute("PRAGMA table_info(qr_images)")])
            finally:
                migrated.close()

    def test_folder_buttons_carry_name_and_parent_across_tasks(self):
        base = {"base_url": self.base, "api_token": "synthetic-test-token", "rule_id": "rule-A"}
        context = {"data_dir": self.temp.name}
        def nodes(result):
            def walk(items):
                for item in items:
                    yield item
                    yield from walk(item.get("children", []))
            return list(walk(result["ui"].get("components", []) if isinstance(result["ui"], dict) else result["ui"]))
        with patch.dict("os.environ", {}, clear=True):
            plugin.run({**base, "action": "choose_account", "cloud_account_id": "pool-A"}, context)
            root = plugin.run({**base, "action": "folders", "folder_id": "0"}, context)
            enter = next(n for n in nodes(root) if n.get("label") == "进入：Media")
            child = plugin.run({**base, **enter["params"]}, context)
            choose = next(n for n in nodes(child) if n.get("label") == "预览选用当前目录：Media")
            preview = plugin.run({**base, **choose["params"]}, context)
            confirm = next(n for n in nodes(preview) if n.get("params", {}).get("action") == "confirm_maintenance")
            plugin.run({**base, **confirm["params"]}, context)
        self.assertEqual(self.fixture.native_folder_body,
                         {"folder_id": "999999999999999999999", "folder_name": "Media", "parent_id": "0"})

    def interrupted_start(self):
        # Stop after the real mock HTTP response and durable encrypted flow, before image creation.
        with patch.object(self.service, "_qr_image", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                self.start()
        self.assertEqual(len(self.state.unresolved(self.service.scope)), 1)
        second = State(self.temp.name)
        self.addCleanup(second.close)
        restored = Service(self.api, self.contract, second)
        restored.snapshot("rule-A")
        return restored

    def test_image_failure_does_not_turn_an_accepted_qr_start_into_an_unknown_write(self):
        with patch("qrcode.make", side_effect=OSError("SYNTHETIC-PRIVATE-PATH")):
            with self.assertRaises(UserError) as error:
                self.start()
        self.assertNotIn("SYNTHETIC-PRIVATE-PATH", str(error.exception))
        self.assertEqual(self.state.unresolved(self.service.scope), [])
        self.assertEqual(self.state.history(self.service.scope)[0]["result"], "accepted")
        self.assertEqual(self.service.qr_status(self.snapshot, "pool-A"), "waiting")
        self.service.preview_maintenance(self.snapshot, "qr_start", "pool-A")
        self.assertEqual(len([c for c in self.fixture.calls if c[1].endswith("/qr/start")]), 1)

    def test_missing_qrcode_dependency_is_a_local_failure_without_a_write(self):
        import builtins
        original_import = builtins.__import__
        def without_qrcode(name, *args, **kwargs):
            if name == "qrcode":
                raise ImportError("synthetic missing dependency")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=without_qrcode):
            with self.assertRaises(UserError):
                self.start()
        self.assertEqual(self.state.unresolved(self.service.scope), [])
        self.assertEqual(self.state.history(self.service.scope)[0]["result"], "failed")
        self.assertFalse(any(c[1].endswith("/qr/start") for c in self.fixture.calls))

    def test_interrupted_start_reconciles_a_known_flow_without_replaying(self):
        restored = self.interrupted_start()
        self.assertEqual(restored.reconcile()[0]["status"], "success")
        self.assertEqual(restored.state.unresolved(restored.scope), [])
        self.assertEqual(len([c for c in self.fixture.calls if c[1].endswith("/qr/start")]), 1)

    def test_expired_interrupted_flow_can_be_closed_and_previewed_again(self):
        restored = self.interrupted_start()
        self.fixture.flows.clear()
        self.assertEqual(restored.reconcile()[0]["status"], "expired")
        self.assertEqual(restored.state.unresolved(restored.scope), [])
        self.assertIsNone(restored.state.qr_flow(restored.scope, "pool-A"))
        restored.preview_maintenance(restored.snapshot("rule-A"), "qr_start", "pool-A")
        self.assertEqual(len([c for c in self.fixture.calls if c[1].endswith("/qr/start")]), 1)

    def test_failed_flow_observation_keeps_the_same_request_pending(self):
        restored = self.interrupted_start()
        with patch.object(self.api, "qr_status", side_effect=ApiError("synthetic network error")):
            self.assertEqual(restored.reconcile()[0]["status"], "unknown")
        self.assertEqual(len(restored.state.unresolved(restored.scope)), 1)
        self.assertEqual(restored.reconcile()[0]["status"], "success")
        self.assertEqual(len([c for c in self.fixture.calls if c[1].endswith("/qr/start")]), 1)

    def test_lost_start_response_without_a_flow_handle_stays_unknown(self):
        with patch.object(self.api, "qr_start", side_effect=ApiError("synthetic timeout", uncertain=True)):
            with self.assertRaises(ApiError):
                self.start()
        self.assertEqual(self.service.reconcile()[0]["status"], "unknown")
        self.assertEqual(len(self.state.unresolved(self.service.scope)), 1)
        self.assertFalse(any(c[1].endswith("/qr/status") for c in self.fixture.calls))
        with self.assertRaises(UserError):
            self.service.preview_maintenance(self.snapshot, "qr_start", "pool-A")

    def test_old_login_is_not_proof_of_an_uncertain_relogin(self):
        self.assertTrue(self.snapshot["accounts"][0]["logged_in"])
        self.start()
        self.fixture.qr_states["pool-A"] = 2
        preview_id, _ = self.service.preview_maintenance(self.snapshot, "qr_complete", "pool-A")
        with patch.object(self.api, "qr_complete", side_effect=ApiError("synthetic timeout", uncertain=True)):
            with self.assertRaises(ApiError):
                self.service.execute_maintenance(self.snapshot, preview_id, self.temp.name)
        self.assertEqual(self.service.reconcile()[0]["status"], "unknown")
        self.assertEqual(len(self.state.unresolved(self.service.scope)), 1)

    def test_first_login_with_a_lost_reply_reconciles_the_authoritative_transition(self):
        self.fixture.accounts[0]["logged_in"] = False
        self.snapshot = self.service.snapshot("rule-A")
        self.start()
        self.fixture.qr_states["pool-A"] = 2
        preview_id, _ = self.service.preview_maintenance(self.snapshot, "qr_complete", "pool-A")
        original_complete = self.api.qr_complete
        def lost_reply(*args):
            original_complete(*args)
            raise ApiError("synthetic lost reply", uncertain=True)
        with patch.object(self.api, "qr_complete", side_effect=lost_reply):
            with self.assertRaises(ApiError):
                self.service.execute_maintenance(self.snapshot, preview_id, self.temp.name)
        self.assertEqual(self.service.reconcile()[0]["status"], "success")
        self.assertEqual(self.state.unresolved(self.service.scope), [])
        self.assertEqual(len([c for c in self.fixture.calls if c[1].endswith("/qr/complete")]), 1)

    def test_combined_login_checks_once_and_returns_waiting_or_scanned(self):
        self.start()
        self.assertEqual(self.service.check_login(self.snapshot, "pool-A", self.temp.name), {"qr_status": "waiting"})
        self.fixture.qr_states["pool-A"] = 1
        self.assertEqual(self.service.check_login(self.snapshot, "pool-A", self.temp.name), {"qr_status": "scanned"})
        self.assertEqual(len([c for c in self.fixture.calls if c[1].endswith("/qr/status")]), 2)
        self.assertFalse(any(c[1].endswith("/qr/complete") for c in self.fixture.calls))

    def test_combined_login_uses_the_authorized_flow_and_completes_once(self):
        self.start()
        flow = self.fixture.flows["pool-A"]
        self.fixture.qr_states["pool-A"] = 2
        outcome = self.service.check_login(self.snapshot, "pool-A", self.temp.name)
        self.assertTrue(outcome["completion_submitted"])
        complete = [c for c in self.fixture.calls if c[1].endswith("/qr/complete")]
        self.assertEqual(len(complete), 1)
        self.assertEqual(complete[0][3], {"flow_id": flow})
        self.assertEqual(len([c for c in self.fixture.calls if c[1].endswith("/qr/status")]), 1)
        self.assertIsNone(self.state.qr_flow(self.service.scope, "pool-A"))
        self.assertEqual(self.service.check_login(self.snapshot, "pool-A", self.temp.name), {"qr_status": "expired"})
        self.assertEqual(len([c for c in self.fixture.calls if c[1].endswith("/qr/complete")]), 1)

    def test_combined_login_keeps_two_accounts_separate(self):
        self.start("pool-A")
        self.start("pool-B")
        self.fixture.qr_states["pool-A"] = 2
        self.assertEqual(self.service.check_login(self.snapshot, "pool-B", self.temp.name), {"qr_status": "waiting"})
        self.service.check_login(self.snapshot, "pool-A", self.temp.name)
        complete = [c for c in self.fixture.calls if c[1].endswith("/qr/complete")]
        self.assertEqual(len(complete), 1)
        self.assertIn("/pool-A/", complete[0][1])
        self.assertIsNotNone(self.state.qr_flow(self.service.scope, "pool-B"))

    def test_combined_login_rechecks_affected_users_after_the_status_request(self):
        self.start()
        self.fixture.qr_states["pool-A"] = 2
        original_status = self.api.qr_status
        def another_user_was_assigned(*args):
            response = original_status(*args)
            self.fixture.accounts[0]["usage_count"] = 1
            self.fixture.accounts[0]["users"] = ["new-test-user"]
            return response
        with patch.object(self.api, "qr_status", side_effect=another_user_was_assigned):
            with self.assertRaises(UserError):
                self.service.check_login(self.snapshot, "pool-A", self.temp.name)
        self.assertFalse(any(c[1].endswith("/qr/complete") for c in self.fixture.calls))

    def test_combined_login_rejects_a_replaced_flow(self):
        self.start()
        self.fixture.qr_states["pool-A"] = 2
        original_status = self.api.qr_status
        def replaced(*args):
            response = original_status(*args)
            encrypted = FlowCipher(self.base, "synthetic-test-token").encrypt("new-synthetic-flow", self.service.scope, "pool-A")
            self.state.remember_flow(self.service.scope, "pool-A", encrypted, time.time() + 120)
            return response
        with patch.object(self.api, "qr_status", side_effect=replaced):
            with self.assertRaises(UserError):
                self.service.check_login(self.snapshot, "pool-A", self.temp.name)
        self.assertFalse(any(c[1].endswith("/qr/complete") for c in self.fixture.calls))

    def test_legacy_flow_requires_explicit_completion_preview(self):
        self.start()
        self.fixture.qr_states["pool-A"] = 2
        with self.state.db:
            self.state.db.execute("UPDATE qr_flows SET preview_id=''")
        with self.assertRaises(UserError):
            self.service.check_login(self.snapshot, "pool-A", self.temp.name)
        preview_id, _ = self.service.preview_maintenance(self.snapshot, "qr_complete", "pool-A")
        self.service.execute_maintenance(self.snapshot, preview_id, self.temp.name)
        self.assertEqual(len([c for c in self.fixture.calls if c[1].endswith("/qr/complete")]), 1)

    def test_combined_login_does_not_replay_an_uncertain_completion(self):
        self.start()
        self.fixture.qr_states["pool-A"] = 2
        with patch.object(self.api, "qr_complete", side_effect=ApiError("synthetic timeout", uncertain=True)) as complete:
            with self.assertRaises(ApiError):
                self.service.check_login(self.snapshot, "pool-A", self.temp.name)
            with self.assertRaises(UserError):
                self.service.check_login(self.snapshot, "pool-A", self.temp.name)
            self.assertEqual(complete.call_count, 1)
        self.assertEqual(len(self.state.unresolved(self.service.scope)), 1)

    def test_combined_login_handles_expiry_without_completing(self):
        self.start()
        self.fixture.flows.clear()
        self.assertEqual(self.service.check_login(self.snapshot, "pool-A", self.temp.name), {"qr_status": "expired"})
        self.assertIsNone(self.state.qr_approval(self.service.scope, "pool-A"))
        self.assertFalse(any(c[1].endswith("/qr/complete") for c in self.fixture.calls))

    def test_combined_login_button_survives_a_new_entry_process(self):
        self.start()
        self.state.target(self.service.scope, "pool-A")
        self.fixture.qr_states["pool-A"] = 2
        with patch.dict("os.environ", {}, clear=True):
            result = plugin.run({"base_url": self.base, "api_token": "synthetic-test-token", "rule_id": "rule-A",
                                 "action": "check_login", "cloud_account_id": "pool-A"}, {"data_dir": self.temp.name})
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertIn("原接口已接受", serialized)
        self.assertNotIn("synthetic-test-token", serialized)
        self.assertEqual(len([c for c in self.fixture.calls if c[1].endswith("/qr/status")]), 1)
        self.assertEqual(len([c for c in self.fixture.calls if c[1].endswith("/qr/complete")]), 1)

    def test_stale_combined_login_button_cannot_complete_another_target(self):
        self.start("pool-A")
        self.start("pool-B")
        self.fixture.qr_states["pool-B"] = 2
        self.state.target(self.service.scope, "pool-B")
        with patch.dict("os.environ", {}, clear=True):
            result = plugin.run({"base_url": self.base, "api_token": "synthetic-test-token", "rule_id": "rule-A",
                                 "action": "check_login", "cloud_account_id": "pool-A"}, {"data_dir": self.temp.name})
        self.assertIn("目标小号已变化", json.dumps(result, ensure_ascii=False))
        self.assertFalse(any(c[1].endswith("/qr/complete") for c in self.fixture.calls))

    def test_pending_assignment_blocks_combined_login_and_account_maintenance(self):
        self.start()
        self.fixture.qr_states["pool-A"] = 2
        pid = self.state.preview(self.service.scope, {"kind": "assign", "target": self.snapshot["accounts"][0], "items": []})
        self.state.claim(self.service.scope, pid)
        self.state.pending(pid, "emby-0")
        with self.assertRaises(UserError):
            self.service.check_login(self.snapshot, "pool-A", self.temp.name)
        with self.assertRaises(UserError):
            self.service.preview_maintenance(self.snapshot, "qr_start", "pool-A")
        self.assertFalse(any(c[1].endswith("/qr/complete") for c in self.fixture.calls))

    def test_previous_flow_table_keeps_legacy_context_without_implicit_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "account-manager.sqlite3"
            encrypted = FlowCipher(self.base, "synthetic-test-token").encrypt("legacy-flow", self.service.scope, "pool-A")
            db = sqlite3.connect(database)
            db.execute("CREATE TABLE qr_flows(scope TEXT,account_id TEXT,encrypted BLOB,created REAL,expires REAL,PRIMARY KEY(scope,account_id))")
            db.execute("INSERT INTO qr_flows VALUES(?,?,?,?,?)", (self.service.scope, "pool-A", encrypted, time.time(), time.time() + 120))
            db.commit()
            db.close()
            migrated = State(directory)
            try:
                self.assertEqual(migrated.qr_flow(self.service.scope, "pool-A"), encrypted)
                self.assertIsNone(migrated.qr_approval(self.service.scope, "pool-A"))
            finally:
                migrated.close()


if __name__ == "__main__":
    unittest.main()
