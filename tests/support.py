import copy
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

MAPPING = {
    "schema_version": 1,
    "account": {key: [key] for key in (
        "id", "rule_id", "provider", "name", "masked_login", "logged_in",
        "expired", "folder_id", "folder_ready", "folder_path", "users", "usage_count")},
    "binding": {"provider": ["provider"], "id": ["cloud_account_id"], "name": ["display_name"]},
    "qr": {"flow_storage": "native", "start_request": {"device": "synthetic"},
           "status_request": {}, "complete_request": {}, "payload": ["payload"],
           "status": ["status"], "states": {"0": "waiting", "1": "scanned", "2": "authorized",
                                          "-1": "expired", "-2": "failed"}},
    "folders": {"parent_parameter": "synthetic_parent", "selection_parameter": "synthetic_folder",
                "items": ["folders"], "id": ["id"], "name": ["name"]},
}


def account(account_id, rule="rule-A"):
    return {"id": account_id, "rule_id": rule, "provider": "115", "name": account_id,
            "masked_login": "115***42", "logged_in": True, "expired": False,
            "folder_id": "123456789012345678901", "folder_ready": True, "folder_path": "/media",
            "users": [], "usage_count": 0, "credentials": "SYNTHETIC-COOKIE-MUST-NOT-LEAK"}


class Fixture:
    def __init__(self):
        self.calls = []
        self.accounts = [account("pool-A"), account("pool-B"), account("pool-C", "rule-B")]
        self.users = [{"emby_user_id": "emby-" + str(i), "emby_username": "User " + str(i),
                       "bindings": [], "is_administrator": False, "is_disabled": False}
                      for i in range(3)]
        self.fail_user = None
        self.uncertain_user = None
        self.qr_state = 0
        self.qr_states = {}
        self.folder = None
        self.redirect = False
        self.server_error = False

    def dispatch(self, method, path, query, body):
        self.calls.append((method, path, copy.deepcopy(query), copy.deepcopy(body)))
        if self.redirect:
            return 302, {"success": False}
        if self.server_error:
            return 500, {"success": False}
        if path == "/api/emby_proxy/config":
            return 200, {"success": True, "rules": [
                {"id": "rule-A", "name": "Rule A", "enabled": True,
                 "api_key": "SYNTHETIC-API-KEY-MUST-NOT-LEAK"},
                {"id": "rule-B", "name": "Rule B", "enabled": True}]}
        if path == "/api/emby_proxy/cloud-accounts":
            return 200, {"success": True, "cloud_accounts": [a for a in self.accounts if a["rule_id"] == query["id"][0]]}
        if path == "/api/emby_proxy/existing-users":
            return 200, {"success": True, "users": self.users}
        if path.endswith("/cloud-accounts/create"):
            new = account("created-" + str(len(self.accounts)), body["rule_id"])
            new["name"] = body["display_name"]
            new.update(logged_in=False, folder_id="", folder_ready=False)
            self.accounts.append(new)
            return 200, {"success": True, "cloud_account": new}
        if path.endswith("/cloud-accounts/rename"):
            next(a for a in self.accounts if a["id"] == body["cloud_account_id"])["name"] = body["display_name"]
            return 200, {"success": True}
        if path.endswith("/cloud-accounts/delete"):
            selected = next(a for a in self.accounts if a["id"] == body["cloud_account_id"])
            if selected["users"]:
                return 403, {"success": False}
            self.accounts.remove(selected)
            return 200, {"success": True}
        if path.endswith("/existing-users/prepare"):
            number = body["emby_user_id"].removeprefix("emby-")
            record = "native-record-" + number
            return 200, {"success": True, "account": {"id": record},
                         "binding_url": "/api/emby_proxy/existing-users/" + record + "/cloud"}
        if "/existing-users/native-record-" in path:
            number = path.split("native-record-", 1)[1].split("/", 1)[0]
            user = next(u for u in self.users if u["emby_user_id"] == "emby-" + number)
            if path.endswith("/assign") or path.endswith("/unbind"):
                if self.fail_user == user["emby_user_id"]:
                    return 403, {"success": False, "message": "SYNTHETIC-SECRET-IN-ERROR"}
                if path.endswith("/assign"):
                    a = next(a for a in self.accounts if a["id"] == body["cloud_account_id"])
                    user["bindings"] = [b for b in user["bindings"] if b["provider"] != "115"]
                    user["bindings"].append({"provider": "115", "cloud_account_id": a["id"], "display_name": a["name"]})
                else:
                    user["bindings"] = [b for b in user["bindings"] if b["provider"] != "115"]
                for pool_account in self.accounts:
                    pool_account["users"] = [u["emby_username"] for u in self.users
                                             if any(b.get("cloud_account_id") == pool_account["id"] for b in u["bindings"])]
                    pool_account["usage_count"] = len(pool_account["users"])
                if self.uncertain_user == user["emby_user_id"]:
                    return 500, {"success": False}
                return 200, {"success": True}
        if "/cloud-accounts/" in path:
            account_id = path.split("/cloud-accounts/", 1)[1].split("/", 1)[0]
            a = next(a for a in self.accounts if a["id"] == account_id)
            if path.endswith("/qr/start"):
                self.qr_states[account_id] = 0
                return 200, {"success": True, "payload": "https://example.invalid/synthetic-qr"}
            if path.endswith("/qr/status"):
                return 200, {"success": True, "status": self.qr_states.get(account_id, self.qr_state)}
            if path.endswith("/qr/complete"):
                if self.qr_states.get(account_id, self.qr_state) != 2:
                    return 403, {"success": False}
                a["logged_in"] = True
                a["expired"] = False
                return 200, {"success": True}
            if path.endswith("/folders"):
                return 200, {"success": True, "folders": [{"id": "999999999999999999999", "name": "Media"}]}
            if path.endswith("/folder"):
                self.folder = body["synthetic_folder"]
                a["folder_id"] = self.folder
                return 200, {"success": True}
            if path.endswith("/session"):
                return 200, {"success": True, "cloud_account": a}
        return 404, {"success": False}


@contextmanager
def mock_server(fixture):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_json("GET")

        def do_POST(self):
            self.handle_json("POST")

        def handle_json(self, method):
            if self.headers.get("Authorization") != "Bearer synthetic-test-token":
                status, result = 401, {"success": False}
            else:
                parsed = urlsplit(self.path)
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                status, result = fixture.dispatch(method, parsed.path, parse_qs(parsed.query), body)
            raw = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if status == 302:
                self.send_header("Location", "http://127.0.0.1:1/credential-trap")
            self.send_header("Content-Length", str(len(raw)))
            try:
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # Expected only when the recovery test deliberately kills its own child.
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
