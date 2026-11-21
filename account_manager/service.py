import hashlib
import time

from .api import identifier
from .errors import ApiError, ContractError, UserError


class Service:
    def __init__(self, api, contract, state):
        self.api, self.contract, self.state = api, contract, state
        self.scope = self.rule_id = ""

    def _flow_cipher(self):
        from .flow_crypto import FlowCipher
        return FlowCipher(self.api.client.base_url, self.api.client._token)

    def _qr_request(self, action, account_id, app="web"):
        if not self.contract.flow_required:
            return self.contract.qr_request(action)
        flow_id = None
        if action != "start":
            encrypted = self.state.qr_flow(self.scope, account_id)
            if not encrypted:
                raise UserError("扫码流程已过期或不存在，请重新生成二维码。")
            try:
                flow_id = self._flow_cipher().decrypt(encrypted, self.scope, account_id)
            except UserError:
                self.state.clear_qr(self.scope, account_id)
                raise
        return self.contract.qr_request(action, flow_id=flow_id, app=app)

    def _qr_revision(self, account_id):
        encrypted = self.state.qr_flow(self.scope, account_id)
        return hashlib.sha256(encrypted).hexdigest() if encrypted else ""

    def snapshot(self, rule_id):
        rules = self.api.rules()
        if not rule_id:
            return {"rules": rules, "accounts": [], "users": [], "selected": [], "rule_id": ""}
        if rule_id not in {r["id"] for r in rules}:
            raise UserError("所选反代规则已不存在，请重新选择。")
        self.rule_id = identifier(rule_id)
        self.scope = hashlib.sha256((self.api.client.base_url + "\0" + rule_id).encode()).hexdigest()
        changed = self.state.enter_scope(self.scope)
        raw_accounts = self.api.accounts(rule_id)
        accounts = []
        if raw_accounts:
            for raw in raw_accounts:
                account = self.contract.account(raw, rule_id)
                if account:
                    accounts.append(account)
        old_target = self.state.target(self.scope)
        if old_target and old_target not in {a["id"] for a in accounts}:
            self.state.target(self.scope, "")
            self.state.clear_qr(self.scope, old_target)
        users = self.api.users(rule_id)
        for user in users:
            user["binding"] = self.contract.binding(user.pop("bindings"))
        valid_ids = {u["id"] for u in users}
        for user_id in self.state.selected(self.scope):
            if user_id not in valid_ids:
                self.state.select(self.scope, user_id, False)
        return {"rules": rules, "accounts": accounts, "users": users,
                "selected": self.state.selected(self.scope), "rule_id": rule_id,
                "scope_changed": changed, "pending": self.state.unresolved(self.scope),
                "observed_at": time.time()}

    @staticmethod
    def account(snapshot, account_id, *, ready=False):
        for account in snapshot["accounts"]:
            if account["id"] == account_id:
                if ready and not account["ready"]:
                    raise UserError("目标小号未登录、登录已失效或接收目录尚未就绪。")
                return account
        raise UserError("当前规则中找不到所选 115 小号。")

    def select(self, snapshot, user_id, add):
        if user_id not in {u["id"] for u in snapshot["users"]}:
            raise UserError("当前规则中找不到该用户。")
        self.state.select(self.scope, identifier(user_id), add)

    def preview_binding(self, snapshot, target_id, kind="assign"):
        if kind not in {"assign", "unbind"}:
            raise UserError("不支持的分配操作。")
        selected = self.state.selected(self.scope)
        if not selected:
            raise UserError("请先选择至少一位用户。")
        pending_users = {r["user_id"] for r in self.state.unresolved(self.scope)}
        if pending_users.intersection(selected):
            raise UserError("所选用户有结果待核实的操作，请先核实，避免重复写入。")
        target = self.account(snapshot, target_id, ready=True) if kind == "assign" else None
        if target:
            import json
            for row in self.state.unresolved(self.scope):
                pending_account = json.loads(row["payload"]).get("account")
                if pending_account and pending_account["id"] == target["id"]:
                    raise UserError("目标小号有结果待核实的维护操作，请先核实账号状态。")
        items = [{"id": u["id"], "name": u["name"], "old": u["binding"],
                  "disabled": u["disabled"], "admin": u["admin"]}
                 for u in snapshot["users"] if u["id"] in selected]
        payload = {"kind": kind, "target": target, "items": items}
        return self.state.preview(self.scope, payload), payload

    def execute_binding(self, snapshot, preview_id):
        payload = self.state.load_preview(self.scope, preview_id)
        if payload.get("kind") not in {"assign", "unbind"}:
            raise UserError("该预览不是用户分配操作。")
        current = {u["id"]: u for u in snapshot["users"]}
        target = payload["target"]
        if target:
            live_target = self.account(snapshot, target["id"], ready=True)
            if live_target != target:
                raise UserError("目标账号状态、目录或关联用户已变化，请重新预览。")
        for item in payload["items"]:
            if item["id"] not in current or current[item["id"]]["binding"] != item["old"]:
                raise UserError("用户或原关联已变化，请重新预览。")
        self.state.claim(self.scope, preview_id)
        results = []
        deadline = time.monotonic() + 420
        for item in payload["items"]:
            if time.monotonic() > deadline:
                results.append({"id": item["id"], "name": item["name"], "status": "not_started",
                                "detail": "本批执行时间已到，请重新预览后处理该用户。"})
                continue
            expected_id = target["id"] if target else ""
            operation = self.state.pending(preview_id, item["id"])
            binding_write_returned = False
            try:
                # Re-read immediately before every write. Other admins may have changed a user mid-batch.
                fresh = self._current_binding(item["id"])
                if fresh != item["old"]["id"]:
                    raise UserError("用户关联已变化，未执行本项写入。")
                if fresh == expected_id:
                    status, detail = "success", "已重新读取，关联符合目标，无需写入。"
                else:
                    if target:
                        live_accounts = [self.contract.account(raw, self.rule_id) for raw in self.api.accounts(self.rule_id)]
                        fresh_target = next((a for a in live_accounts if a and a["id"] == target["id"]), None)
                        stable = lambda a: {k: v for k, v in a.items() if k not in {"users", "usage_count"}}
                        if not fresh_target or not fresh_target["ready"] or stable(fresh_target) != stable(target):
                            raise UserError("目标小号登录或目录状态已变化，未执行本项写入。")
                    binding_path = self.api.prepare(self.rule_id, item["id"])
                    if target:
                        self.api.assign(binding_path, target["id"])
                    else:
                        self.api.unbind(binding_path)
                    binding_write_returned = True
                    if self._current_binding(item["id"]) != expected_id:
                        raise ApiError("写入已返回，但关联结果尚未确认。", uncertain=True)
                    status, detail = "success", "已从原系统重新读取并确认关联。"
            except ApiError as exc:
                if exc.uncertain or binding_write_returned:
                    try:
                        confirmed = self._current_binding(item["id"]) == expected_id
                    except UserError:
                        confirmed = False
                    status = "success" if confirmed else "unknown"
                    detail = "已查询确认目标关联。" if confirmed else "结果待核实；不会自动重复写入。"
                else:
                    status, detail = "failed", str(exc)
            except ContractError:
                # A contract error can occur AFTER prepare has written. Do not blindly replay it.
                status, detail = "unknown", "接口契约不完整，结果待核实。"
            except UserError as exc:
                status = "unknown" if binding_write_returned else "failed"
                detail = "写入后未确认状态；不会自动重复写入。" if binding_write_returned else str(exc)
            self.state.result(operation, status, detail)
            self.state.log(self.scope, payload["kind"], expected_id, item["id"], status)
            results.append({"id": item["id"], "name": item["name"], "status": status, "detail": detail})
        self.state.finish(preview_id)
        self.state.clear(self.scope)
        return results

    def _current_binding(self, user_id):
        users = self.api.users(self.rule_id)
        for user in users:
            if user["id"] == user_id:
                return self.contract.binding(user["bindings"])["id"]
        raise UserError("原系统中已找不到该用户。")

    def reconcile(self):
        results = []
        for row in self.state.unresolved(self.scope)[:30]:
            import json
            payload = json.loads(row["payload"])
            if payload["kind"] not in {"assign", "unbind"}:
                results.append(self._reconcile_maintenance(row, payload))
                continue
            target = payload.get("target")
            expected = target["id"] if target else ""
            try:
                matched = self._current_binding(row["user_id"]) == expected
            except UserError:
                matched = False
            status = "success" if matched else "unknown"
            detail = "已核实目标关联。" if matched else "当前未确认目标关联；保留待核实状态。"
            self.state.result(row["id"], status, detail)
            results.append({"id": row["user_id"], "name": row["user_id"], "status": status, "detail": detail})
        return results

    def _reconcile_maintenance(self, row, payload):
        kind = payload["kind"]
        accounts = [self.contract.account(raw, self.rule_id) for raw in self.api.accounts(self.rule_id)]
        accounts = [a for a in accounts if a]
        old = payload["account"]
        live = next((a for a in accounts if old and a["id"] == old["id"]), None)
        if kind == "qr_start":
            return self._reconcile_qr_start(row, old, live)
        confirmed = False
        if kind == "create":
            candidates = [a for a in accounts if a["id"] not in payload.get("before_ids", []) and a["name"] == payload["name"]]
            confirmed = len(candidates) == 1
        elif kind == "delete":
            confirmed = live is None
        elif kind == "rename":
            confirmed = bool(live and live["name"] == payload["name"])
        elif kind == "folder":
            confirmed = bool(live and live["folder_id"] == payload["folder_id"])
        elif kind == "qr_complete":
            # An unchanged old login does not prove that an uncertain re-login completed.
            confirmed = bool(live and (not old["logged_in"] or old["expired"])
                             and live["logged_in"] and not live["expired"])
        status = "success" if confirmed else "unknown"
        detail = "已核实目标账号状态。" if confirmed else "账号操作结果仍待核实；未重放写入。"
        self.state.result(row["id"], status, detail)
        return {"id": "", "name": old["name"] if old else payload["name"], "status": status, "detail": detail}

    def _reconcile_qr_start(self, row, old, live):
        status, detail = "unknown", "没有可核实的扫码流程；请检查原系统账号面板，未重新发起请求。"
        if live and self.contract.flow_required and self.state.qr_flow(self.scope, old["id"]):
            try:
                raw = self.api.qr_status(old["id"], self._qr_request("status", old["id"]))
                phase = self.contract.qr_status(raw)
            except ApiError as exc:
                phase = "expired" if exc.category == "qr_expired" else None
            except UserError:
                phase = None
            if phase in {"waiting", "scanned", "authorized"}:
                status, detail = "success", "已从原系统确认扫码流程存在；尚未确认登录完成。"
            elif phase in {"expired", "failed"}:
                self.state.clear_qr(self.scope, old["id"])
                status, detail = "expired", "已核实扫码流程已过期或结束，可重新预览发起登录。"
            else:
                detail = "扫码流程暂时无法核实；保留待核实状态，未重新发起请求。"
        self.state.result(row["id"], status, detail)
        if status != "unknown":
            self.state.log(self.scope, "reconcile_qr_start", old["id"], "", status)
        return {"id": "", "name": old["name"], "status": status, "detail": detail}

    def preview_maintenance(self, snapshot, kind, account_id, name="", folder_id="",
                            folder_name="", parent_id="0", app="web", *, checked_flow=None):
        if kind not in {"create", "rename", "delete", "qr_start", "qr_complete", "folder"}:
            raise UserError("不支持的账号操作。")
        self.contract.section("account")
        if kind in {"create", "rename"} and (not isinstance(name, str) or not 1 <= len(name.strip()) <= 120):
            raise UserError("显示名称须为 1 至 120 个字符。")
        account = None if kind == "create" else self.account(snapshot, account_id)
        import json
        for row in self.state.unresolved(self.scope):
            prior = json.loads(row["payload"])
            prior_account = prior.get("account") or prior.get("target")
            if ((kind == "create" and prior["kind"] == "create")
                    or (account and prior_account and account["id"] == prior_account["id"])):
                raise UserError("该账号有结果待核实的维护操作，请先核实，避免重复写入。")
        if kind == "delete" and account["usage_count"]:
            raise UserError("该账号仍有关联用户，请先更换或解除关联。")
        if kind.startswith("qr_"):
            if self.contract.flow_required:
                self._flow_cipher()
            self._qr_request(kind.removeprefix("qr_"), account_id, app)
            if kind == "qr_complete" and checked_flow is not None and (
                    not self.contract.flow_required or not checked_flow or checked_flow != self._qr_revision(account_id)):
                raise UserError("扫码流程已变化，请重新检查。")
            if kind == "qr_complete" and checked_flow is None and self.qr_status(snapshot, account_id) != "authorized":
                raise UserError("二维码尚未授权或已失效，请先检查扫码状态。")
        if kind == "folder":
            self.contract.folder_body(folder_id, folder_name, parent_id)
            if not folder_id:
                raise UserError("请填写接收目录 ID。")
        payload = {"kind": kind, "account": account, "name": name.strip(), "folder_id": folder_id,
                   "folder_name": folder_name, "parent_id": parent_id, "app": app,
                   "before_ids": [a["id"] for a in snapshot["accounts"]],
                   "qr_revision": self._qr_revision(account_id) if kind == "qr_complete" else ""}
        return self.state.preview(self.scope, payload), payload

    def execute_maintenance(self, snapshot, preview_id, data_dir, *, checked_flow=None):
        payload = self.state.load_preview(self.scope, preview_id)
        kind = payload.get("kind")
        if kind not in {"create", "rename", "delete", "qr_start", "qr_complete", "folder"}:
            raise UserError("该预览不是账号维护操作。")
        account = payload["account"]
        if account and self.account(snapshot, account["id"]) != account:
            raise UserError("账号状态或关联用户发生变化，请重新预览。")
        if kind == "qr_complete" and checked_flow is not None and checked_flow != payload.get("qr_revision"):
            raise UserError("扫码流程已变化，请重新检查。")
        if kind == "qr_complete" and checked_flow is None and self.qr_status(snapshot, account["id"]) != "authorized":
            raise UserError("二维码授权状态已变化，请重新检查后生成预览。")
        if (kind == "qr_complete" and self.contract.flow_required
                and payload.get("qr_revision") != self._qr_revision(account["id"])):
            raise UserError("扫码流程已更换，请重新生成确认预览。")
        self.state.claim(self.scope, preview_id)
        account_id = account["id"] if account else ""
        operation = self.state.pending(preview_id, "")
        write_attempted = write_returned = False

        def write(method, *args):
            nonlocal write_attempted, write_returned
            write_attempted = True
            result = method(*args)
            write_returned = True
            return result

        try:
            extra = {}
            if kind == "create":
                write(self.api.create, self.rule_id, payload["name"])
            elif kind == "rename":
                write(self.api.rename, account_id, payload["name"])
            elif kind == "delete":
                write(self.api.delete, account_id)
                self.state.clear_qr(self.scope, account_id)
            elif kind == "qr_start":
                # No QR auth payload is logged or put in the plugin's JSON state.
                request = self._qr_request("start", account_id, payload.get("app", "web"))
                cipher = self._flow_cipher() if self.contract.flow_required else None
                try:
                    import qrcode
                except ImportError:
                    raise UserError("运行环境缺少二维码依赖 qrcode。") from None
                self.state.clear_qr(self.scope, account_id)
                started = time.time()
                result = write(self.api.qr_start, account_id, request)
                ttl = self.contract.qr_ttl(result) if self.contract.flow_required else 300
                expires = started + ttl
                if cipher:
                    encrypted = cipher.encrypt(self.contract.qr_flow(result), self.scope, account_id)
                    self.state.remember_flow(self.scope, account_id, encrypted, expires, preview_id)
                extra["qr_image"] = self._qr_image(self.contract.qr_payload(result), data_dir)
                self.state.remember_qr(self.scope, account_id, extra["qr_image"].removeprefix("data://"), expires)
            elif kind == "qr_complete":
                write(self.api.qr_complete, account_id, self._qr_request("complete", account_id))
                self.state.clear_qr(self.scope, account_id)
            elif kind == "folder":
                write(self.api.folder, account_id, self.contract.folder_body(payload["folder_id"],
                                payload.get("folder_name", ""), payload.get("parent_id", "0")))
            detail = "原接口已接受操作；账号就绪状态请以刷新结果为准。"
            self.state.result(operation, "accepted", detail)
            self.state.log(self.scope, kind, account_id, "", "accepted")
            self.state.finish(preview_id)
            return {"detail": detail, **extra}
        except ApiError as exc:
            self.state.result(operation, "unknown" if exc.uncertain else "failed", str(exc))
            self.state.log(self.scope, kind, account_id, "", "unknown" if exc.uncertain else "failed")
            self.state.finish(preview_id)
            raise
        except UserError:
            if write_returned:
                status, detail = "accepted", "原接口已接受操作，本地后续处理未完成，请检查原系统账号面板。"
            elif write_attempted:
                status, detail = "unknown", "响应未能确认，请核实账号状态。"
            else:
                status, detail = "failed", "操作尚未发送，请修正本地配置后重新预览。"
            self.state.result(operation, status, detail)
            self.state.log(self.scope, kind, account_id, "", status)
            self.state.finish(preview_id)
            raise

    @staticmethod
    def _qr_image(payload, data_dir):
        import secrets
        from pathlib import Path
        try:
            import qrcode
            from qrcode.exceptions import DataOverflowError
        except ImportError:
            raise UserError("运行环境缺少已核实的二维码依赖 qrcode。") from None
        directory = Path(data_dir)
        name = "login-qr-" + secrets.token_hex(16) + ".png"
        try:
            qrcode.make(payload).save(directory / name)
        except (OSError, ValueError, DataOverflowError):
            try:
                (directory / name).unlink(missing_ok=True)
            except OSError:
                pass
            raise UserError("二维码图片生成失败，请检查插件数据目录权限及接口版本。") from None
        return "data://" + name

    def check_login(self, snapshot, account_id, data_dir):
        account = self.account(snapshot, account_id)
        if not self.contract.flow_required:
            raise UserError("当前自定义适配不支持合并完成登录，请使用预览完成登录。")
        if not self.state.qr_flow(self.scope, account_id):
            return {"qr_status": "expired"}
        approval = self.state.qr_approval(self.scope, account_id)
        if not approval:
            raise UserError("此扫码流程没有发起时的影响确认，请使用预览完成登录，或重新发起扫码。")
        if account != approval:
            raise UserError("小号状态、目录或关联人数已变化，请重新预览完成登录。")
        for row in self.state.unresolved(self.scope):
            import json
            prior = json.loads(row["payload"])
            target = prior.get("account") or prior.get("target")
            if target and target.get("id") == account_id:
                raise UserError("该小号仍有结果待核实的操作，请先核实，避免重复完成登录。")
        revision = self._qr_revision(account_id)
        phase = self.qr_status(snapshot, account_id)
        if phase != "authorized":
            return {"qr_status": phase}
        fresh = self.snapshot(self.rule_id)
        if fresh.get("scope_changed") or self.state.target(self.scope) not in {"", account_id}:
            raise UserError("当前规则或目标小号已变化，请刷新工作台。")
        if self.account(fresh, account_id) != approval:
            raise UserError("检查期间小号状态或关联人数已变化，请重新预览完成登录。")
        if revision != self._qr_revision(account_id):
            raise UserError("扫码流程已变化，请重新检查。")
        preview_id, _ = self.preview_maintenance(fresh, "qr_complete", account_id, checked_flow=revision)
        result = self.execute_maintenance(fresh, preview_id, data_dir, checked_flow=revision)
        return {**result, "completion_submitted": True}

    def qr_status(self, snapshot, account_id):
        self.account(snapshot, account_id)
        if self.contract.flow_required and not self.state.qr_flow(self.scope, account_id):
            return "expired"
        try:
            result = self.api.qr_status(account_id, self._qr_request("status", account_id))
        except ApiError as exc:
            if exc.category != "qr_expired":
                raise
            self.state.clear_qr(self.scope, account_id)
            return "expired"
        status = self.contract.qr_status(result)
        if status in {"expired", "failed"}:
            self.state.clear_qr(self.scope, account_id)
        return status

    def folders(self, snapshot, account_id, folder_id):
        self.account(snapshot, account_id)
        return self.contract.folders(self.api.folders(account_id, self.contract.folder_query(folder_id)))
