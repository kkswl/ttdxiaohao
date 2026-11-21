import copy
import json
import tempfile
import unittest
from pathlib import Path

from account_manager.contract import Contract
from account_manager.state import State
from account_manager.ui import render
from tests.support import MAPPING, account
from tools.validate import validate_manifest, validate_ui


class NativeUI(unittest.TestCase):
    def test_manifest_and_worst_case_result_limits(self):
        _, inputs, _ = validate_manifest(Path(__file__).resolve().parents[1])
        with tempfile.TemporaryDirectory() as directory:
            state = State(directory)
            try:
                scope = "synthetic-scope"
                state.enter_scope(scope)
                accounts = [Contract(mapping=MAPPING).account(account("pool-" + str(i)), "rule-A") for i in range(50)]
                users = [{"id": "emby-" + str(i), "name": "User " + str(i), "binding": {"id": "", "name": "未关联"},
                          "admin": False, "disabled": False} for i in range(1000)]
                snapshot = {"rules": [{"id": "rule-A", "name": "Rule", "enabled": True}],
                            "accounts": accounts, "users": users, "rule_id": "rule-A"}
                state.target(scope, "pool-0")
                for user in users[:30]:
                    state.select(scope, user["id"], True)
                payload = {"kind": "assign", "target": accounts[0],
                           "items": [{"id": u["id"], "name": u["name"], "old": u["binding"]} for u in users[:30]]}
                cases = [
                    {},
                    {"preview": ("a" * 32, payload)},
                    {"results": [{"id": u["id"], "name": u["name"], "status": "failed", "detail": "合成失败"} for u in users[:30]]},
                    {"maintenance": ("b" * 32, {"kind": "folder", "account": accounts[0], "folder_id": "999999999999999999999", "name": ""}),
                     "folders": [{"id": str(i), "name": "目录"} for i in range(500)],
                     "qr_image": "data://login-qr-synthetic.png", "qr_status": "expired"},
                ]
                for extras in cases:
                    result = render(snapshot, state, scope, {"base_url": "http://127.0.0.1:12366", "action": "history"},
                                    contract_available=True, **extras)
                    check = validate_ui(result["ui"], inputs, result=True)
                    self.assertLessEqual(check["components"], 200)
                    self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 1024 * 1024)
                    self.assertNotIn('"type": "field"', json.dumps(result))
                    self.assertNotIn('"theme"', json.dumps(result))
            finally:
                state.close()

    def test_initial_rule_selection_and_empty_state(self):
        _, inputs, _ = validate_manifest(Path(__file__).resolve().parents[1])
        snapshot = {"rules": [{"id": "rule-A", "name": "Rule"}], "rule_id": "", "accounts": [], "users": []}
        with tempfile.TemporaryDirectory() as directory:
            state = State(directory)
            try:
                result = render(snapshot, state, "", {}, contract_available=False)
                validate_ui(result["ui"], inputs, result=True)
                self.assertIn("尚未核实", json.dumps(result, ensure_ascii=False))
                self.assertNotIn("confirm_binding", json.dumps(result))
            finally:
                state.close()

    def test_unknown_contract_does_not_show_account_create_action(self):
        _, inputs, _ = validate_manifest(Path(__file__).resolve().parents[1])
        snapshot = {"rules": [{"id": "rule-A", "name": "Rule"}], "rule_id": "rule-A", "accounts": [], "users": []}
        with tempfile.TemporaryDirectory() as directory:
            state = State(directory)
            try:
                result = render(snapshot, state, "scope", {"base_url": "http://127.0.0.1:12366"}, contract_available=False)
                validate_ui(result["ui"], inputs, result=True)
                self.assertNotIn("preview_create", json.dumps(result))
            finally:
                state.close()

    def test_rule_selection_stays_within_component_limit_for_large_lists(self):
        _, inputs, _ = validate_manifest(Path(__file__).resolve().parents[1])
        snapshot = {"rules": [{"id": "rule-" + str(i), "name": "Rule " + str(i)} for i in range(1000)],
                    "rule_id": "", "accounts": [], "users": []}
        with tempfile.TemporaryDirectory() as directory:
            state = State(directory)
            try:
                for page in (0, 50, 99):
                    result = render(snapshot, state, "", {"rule_page": page})
                    check = validate_ui(result["ui"], inputs, result=True)
                    self.assertLess(check["components"], 25)
                    self.assertIn('"rule_id": "rule-' + str(page * 10) + '"', json.dumps(result))
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
