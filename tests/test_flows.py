import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import plugin
from account_manager.api import Api, identifier
from account_manager.client import Client
from account_manager.contract import Contract
from account_manager.errors import ApiError, ContractError, UserError
from account_manager.service import Service
from account_manager.state import State
from account_manager.ui import render
from tests.support import Fixture, MAPPING, mock_server


class Flows(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = Fixture()
        self.server = mock_server(self.fixture)
        self.base_url = self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.client = Client(self.base_url, "synthetic-test-token", timeout=1)
        self.api = Api(self.client)
        self.state = State(self.temp.name)
        self.addCleanup(self.state.close)
        self.contract = Contract(mapping=copy.deepcopy(MAPPING))
        self.service = Service(self.api, self.contract, self.state)
        self.snapshot = self.service.snapshot("rule-A")

    def choose(self, users=("emby-0",), target="pool-A", kind="assign"):
        for user_id in users:
            self.service.select(self.snapshot, user_id, True)
        return self.service.preview_binding(self.snapshot, target, kind)

    def test_distinct_ids_and_real_native_assignment(self):
        preview_id, _ = self.choose()
        rows = self.service.execute_binding(self.snapshot, preview_id)
        self.assertEqual(rows[0]["status"], "success")
        writes = [c for c in self.fixture.calls if c[0] == "POST"]
        self.assertEqual(writes[0][3], {"rule_id": "rule-A", "emby_user_id": "emby-0"})
        self.assertIn("/native-record-0/cloud/", writes[1][1])
        self.assertEqual(writes[1][3], {"cloud_account_id": "pool-A"})
        self.assertEqual(self.fixture.users[0]["bindings"][0]["cloud_account_id"], "pool-A")

    def test_preview_is_one_use_even_after_process_restart(self):
        preview_id, _ = self.choose()
        self.service.execute_binding(self.snapshot, preview_id)
        second_state = State(self.temp.name)
        self.addCleanup(second_state.close)
        with self.assertRaises(UserError):
            second_state.claim(self.service.scope, preview_id)
        self.assertEqual(len([c for c in self.fixture.calls if c[1].endswith("/assign")]), 1)

    def test_partial_success_then_individual_retry(self):
        self.fixture.fail_user = "emby-1"
        preview_id, _ = self.choose(("emby-0", "emby-1", "emby-2"))
        results = self.service.execute_binding(self.snapshot, preview_id)
        self.assertEqual([r["status"] for r in results], ["success", "failed", "success"])
        self.assertNotIn("SYNTHETIC-SECRET", json.dumps(results))
        self.fixture.fail_user = None
        self.snapshot = self.service.snapshot("rule-A")
        preview_id, _ = self.choose(("emby-1",))
        results = self.service.execute_binding(self.snapshot, preview_id)
        self.assertEqual(results[0]["status"], "success")
        self.assertEqual(len([c for c in self.fixture.calls if c[1].endswith("/assign")]), 4)

    def test_uncertain_write_reconciles_without_replay(self):
        self.fixture.uncertain_user = "emby-0"
        preview_id, _ = self.choose()
        results = self.service.execute_binding(self.snapshot, preview_id)
        self.assertEqual(results[0]["status"], "success")
        self.assertEqual(len([c for c in self.fixture.calls if c[1].endswith("/assign")]), 1)

    def test_uncertain_unconfirmed_write_blocks_new_preview(self):
        preview_id, _ = self.choose()
        with patch.object(self.api, "assign", side_effect=ApiError("网络错误", uncertain=True)):
            results = self.service.execute_binding(self.snapshot, preview_id)
        self.assertEqual(results[0]["status"], "unknown")
        self.snapshot = self.service.snapshot("rule-A")
        self.service.select(self.snapshot, "emby-0", True)
        with self.assertRaises(UserError):
            self.service.preview_binding(self.snapshot, "pool-A")
        self.assertEqual(self.service.reconcile()[0]["status"], "unknown")

    def test_unbind_keeps_other_user_and_shared_account(self):
        preview_id, _ = self.choose(("emby-0", "emby-1"))
        self.service.execute_binding(self.snapshot, preview_id)
        self.snapshot = self.service.snapshot("rule-A")
        preview_id, _ = self.choose(("emby-0",), kind="unbind")
        self.service.execute_binding(self.snapshot, preview_id)
        self.assertEqual(self.fixture.users[0]["bindings"], [])
        self.assertEqual(self.fixture.users[1]["bindings"][0]["cloud_account_id"], "pool-A")
        self.assertEqual(len(self.fixture.accounts), 3)
        self.assertFalse(any(c[1].endswith("/delete") for c in self.fixture.calls))

    def test_switching_account_updates_only_selected_user(self):
        preview_id, _ = self.choose(("emby-0", "emby-1"))
        self.service.execute_binding(self.snapshot, preview_id)
        self.snapshot = self.service.snapshot("rule-A")
        preview_id, _ = self.choose(("emby-1",), "pool-B")
        self.service.execute_binding(self.snapshot, preview_id)
        self.assertEqual([u["bindings"][0]["cloud_account_id"] for u in self.fixture.users[:2]], ["pool-A", "pool-B"])

    def test_changed_binding_invalidates_preview(self):
        preview_id, _ = self.choose()
        self.fixture.users[0]["bindings"] = [{"provider": "115", "cloud_account_id": "pool-B", "display_name": "B"}]
        self.snapshot = self.service.snapshot("rule-A")
        with self.assertRaises(UserError):
            self.service.execute_binding(self.snapshot, preview_id)
        self.assertFalse(any(c[0] == "POST" for c in self.fixture.calls))

    def test_rule_change_clears_selection_target_and_previews(self):
        preview_id, _ = self.choose()
        scope = self.service.scope
        self.state.target(scope, "pool-A")
        self.service.snapshot("rule-B")
        self.assertEqual(self.state.selected(scope), [])
        self.assertEqual(self.state.target(scope), "")
        with self.assertRaises(UserError):
            self.state.load_preview(scope, preview_id)

    def test_cross_rule_target_rejected(self):
        self.service.select(self.snapshot, "emby-0", True)
        with self.assertRaises(UserError):
            self.service.preview_binding(self.snapshot, "pool-C")

    def test_expired_and_unconfigured_account_rejected(self):
        for change in ({"expired": True}, {"logged_in": False}, {"folder_id": ""}, {"folder_ready": False}):
            raw = self.fixture.accounts[0]
            old = copy.deepcopy(raw)
            raw.update(change)
            snapshot = self.service.snapshot("rule-A")
            self.service.select(snapshot, "emby-0", True)
            with self.assertRaises(UserError):
                self.service.preview_binding(snapshot, "pool-A")
            raw.clear()
            raw.update(old)

    def test_qr_lifecycle_and_account_scope(self):
        for raw, expected in [(0, "waiting"), (1, "scanned"), (2, "authorized"), (-1, "expired"), (-2, "failed")]:
            self.fixture.qr_state = raw
            self.assertEqual(self.service.qr_status(self.snapshot, "pool-A"), expected)
        self.fixture.qr_state = 2
        self.api.qr_complete("pool-A", self.contract.qr_request("complete"))
        self.assertTrue(all("/cloud-accounts/pool-A/api/115/qr/" in c[1] for c in self.fixture.calls if "/qr/" in c[1]))
        self.assertFalse(any(c[1].startswith("/api/115") for c in self.fixture.calls))

    def test_directory_id_never_becomes_float(self):
        huge = "999999999999999999999"
        rows = self.service.folders(self.snapshot, "pool-A", huge)
        self.assertEqual(rows[0]["id"], huge)
        preview_id, _ = self.service.preview_maintenance(self.snapshot, "folder", "pool-A", folder_id=huge)
        self.service.execute_maintenance(self.snapshot, preview_id, self.temp.name)
        self.assertEqual(self.fixture.folder, huge)
        self.assertIsInstance(self.fixture.folder, str)
        bad = copy.deepcopy(self.fixture.accounts[0])
        bad["folder_id"] = 999999999999999999999
        with self.assertRaises(ContractError):
            self.contract.account(bad, "rule-A")

    def test_delete_used_account_rejected_before_request(self):
        self.snapshot["accounts"][0]["users"] = ["Someone"]
        self.snapshot["accounts"][0]["usage_count"] = 1
        with self.assertRaises(UserError):
            self.service.preview_maintenance(self.snapshot, "delete", "pool-A")
        self.assertFalse(any(c[1].endswith("/delete") for c in self.fixture.calls))

    def test_native_guard_also_rejects_late_account_usage(self):
        preview_id, _ = self.service.preview_maintenance(self.snapshot, "delete", "pool-A")
        self.fixture.accounts[0]["users"] = ["Late user"]
        with self.assertRaises(ApiError):
            self.service.execute_maintenance(self.snapshot, preview_id, self.temp.name)
        self.assertEqual(len(self.fixture.accounts), 3)

    def test_no_guessing_when_contract_missing(self):
        missing = Service(self.api, Contract(), self.state)
        with self.assertRaises(ContractError):
            missing.snapshot("rule-A")
        with self.assertRaises(ContractError):
            Contract().qr_request("start")
        self.fixture.accounts = []
        self.assertEqual(missing.snapshot("rule-A")["accounts"], [])
        with self.assertRaises(ContractError):
            missing.preview_maintenance(self.snapshot, "create", "", "Test account")

    def test_redirect_does_not_send_token_to_destination(self):
        self.fixture.redirect = True
        with self.assertRaises(ApiError) as caught:
            self.api.rules()
        self.assertEqual(caught.exception.status, 302)
        self.assertNotIn("synthetic-test-token", str(caught.exception))

    def test_read_retries_but_write_does_not(self):
        self.fixture.server_error = True
        before = len(self.fixture.calls)
        with self.assertRaises(ApiError):
            self.api.rules()
        self.assertEqual(len(self.fixture.calls) - before, 2)
        before = len(self.fixture.calls)
        with self.assertRaises(ApiError) as caught:
            self.api.create("rule-A", "test")
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(len(self.fixture.calls) - before, 1)

    def test_no_secrets_in_results_or_persisted_state(self):
        preview_id, _ = self.choose()
        results = self.service.execute_binding(self.snapshot, preview_id)
        ui = render(self.snapshot, self.state, self.service.scope, {"base_url": self.base_url}, results=results, contract_available=True)
        serialized = json.dumps(ui, ensure_ascii=False)
        for file in Path(self.temp.name).glob("*"):
            serialized += file.read_bytes().decode("latin-1")
        for secret in ("synthetic-test-token", "SYNTHETIC-COOKIE", "SYNTHETIC-API-KEY"):
            self.assertNotIn(secret, serialized)

    def test_missing_token_is_safe_entry_result(self):
        with patch.dict("os.environ", {}, clear=True):
            result = plugin.run({"base_url": self.base_url}, {"data_dir": self.temp.name})
        self.assertIn("未配置", json.dumps(result, ensure_ascii=False))
        self.assertFalse(any(c[0] == "POST" for c in self.fixture.calls))

    def test_bad_identifiers_cannot_escape_scope(self):
        for value in ("..", "%2f", "../x", "a/b", "x?y", 123):
            with self.assertRaises(ContractError):
                identifier(value)

    def test_create_rename_and_delete_use_native_pool(self):
        preview_id, _ = self.service.preview_maintenance(self.snapshot, "create", "", "New test")
        self.service.execute_maintenance(self.snapshot, preview_id, self.temp.name)
        self.snapshot = self.service.snapshot("rule-A")
        created = next(a for a in self.snapshot["accounts"] if a["name"] == "New test")
        self.assertFalse(created["ready"])
        preview_id, _ = self.service.preview_maintenance(self.snapshot, "rename", created["id"], "Renamed test")
        self.service.execute_maintenance(self.snapshot, preview_id, self.temp.name)
        self.snapshot = self.service.snapshot("rule-A")
        self.assertEqual(next(a for a in self.snapshot["accounts"] if a["id"] == created["id"])["name"], "Renamed test")
        preview_id, _ = self.service.preview_maintenance(self.snapshot, "delete", created["id"])
        self.service.execute_maintenance(self.snapshot, preview_id, self.temp.name)
        self.assertEqual(len(self.fixture.accounts), 3)

    def test_qr_image_generation_keeps_accounts_separate(self):
        from PIL import Image
        preview_id, _ = self.service.preview_maintenance(self.snapshot, "qr_start", "pool-A")
        first = self.service.execute_maintenance(self.snapshot, preview_id, self.temp.name)
        first_path = Path(self.temp.name) / first["qr_image"].removeprefix("data://")
        with Image.open(first_path) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.width, image.height)
            self.assertGreater(image.width, 100)
        preview_id, _ = self.service.preview_maintenance(self.snapshot, "qr_start", "pool-B")
        second = self.service.execute_maintenance(self.snapshot, preview_id, self.temp.name)
        self.assertTrue(first_path.is_file())
        self.assertNotEqual(first["qr_image"], second["qr_image"])
        self.assertNotIn("synthetic-qr", json.dumps(first))

    def test_unbind_preserves_other_provider_binding(self):
        self.fixture.users[0]["bindings"] = [
            {"provider": "115", "cloud_account_id": "pool-A", "display_name": "A"},
            {"provider": "123", "cloud_account_id": "other-provider", "display_name": "Other"},
        ]
        self.snapshot = self.service.snapshot("rule-A")
        preview_id, _ = self.choose(kind="unbind")
        self.service.execute_binding(self.snapshot, preview_id)
        self.assertEqual(self.fixture.users[0]["bindings"], [
            {"provider": "123", "cloud_account_id": "other-provider", "display_name": "Other"}])

    def test_expired_preview_has_no_native_writes(self):
        preview_id, _ = self.choose()
        with self.state.db:
            self.state.db.execute("UPDATE previews SET created=0 WHERE id=?", (preview_id,))
        with self.assertRaises(UserError):
            self.service.execute_binding(self.snapshot, preview_id)
        self.assertFalse(any(c[0] == "POST" for c in self.fixture.calls))

    def test_pending_guard_checks_beyond_first_thirty_records(self):
        preview_id, _ = self.choose()
        for number in range(31):
            self.state.pending(preview_id, "emby-0" if number == 30 else "unknown-" + str(number))
        self.assertEqual(len(self.state.unresolved(self.service.scope)), 31)
        with self.assertRaises(UserError):
            self.service.preview_binding(self.snapshot, "pool-B")

    def test_unknown_create_is_reconciled_without_duplicate(self):
        preview_id, _ = self.service.preview_maintenance(self.snapshot, "create", "", "Recover test")
        original = self.api.create
        def late_response(*args):
            original(*args)
            raise ApiError("结果未知", uncertain=True)
        with patch.object(self.api, "create", side_effect=late_response):
            with self.assertRaises(ApiError):
                self.service.execute_maintenance(self.snapshot, preview_id, self.temp.name)
        rows = self.service.reconcile()
        self.assertEqual(rows[0]["status"], "success")
        self.assertEqual(len([c for c in self.fixture.calls if c[1].endswith("/create")]), 1)

    def test_entry_buttons_persist_target_and_do_not_repeat_confirm(self):
        contract_path = Path(self.temp.name) / "synthetic-contract.json"
        contract_path.write_text(json.dumps(MAPPING), encoding="utf-8")
        env = {"TGTO_OPENAPI_TOKEN": "synthetic-test-token", "TGTO_ACCOUNT_CONTRACT": str(contract_path)}
        base = {"base_url": self.base_url, "rule_id": "rule-A"}
        context = {"data_dir": self.temp.name}
        with patch.dict("os.environ", env):
            plugin.run({**base, "action": "choose_account", "cloud_account_id": "pool-B"}, context)
            plugin.run({**base, "action": "add_user", "user_id": "emby-0"}, context)
            result = plugin.run({**base, "action": "preview_assign"}, context)
            def find(nodes):
                for node in nodes:
                    if node.get("params", {}).get("action") == "confirm_binding":
                        return node["params"]["preview_id"]
                    nested = find(node.get("children", []))
                    if nested:
                        return nested
            preview_id = find(result["ui"]["components"])
            self.assertIsNotNone(preview_id)
            first = plugin.run({**base, "action": "confirm_binding", "preview_id": preview_id}, context)
            second = plugin.run({**base, "action": "confirm_binding", "preview_id": preview_id}, context)
        self.assertIn("成功", json.dumps(first, ensure_ascii=False))
        self.assertIn("预览已过期", json.dumps(second, ensure_ascii=False))
        self.assertEqual(self.fixture.users[0]["bindings"][0]["cloud_account_id"], "pool-B")
        self.assertEqual(len([c for c in self.fixture.calls if c[1].endswith("/assign")]), 1)


if __name__ == "__main__":
    unittest.main()
