"""Synchronous TgtoDrive plugin entry point. No import-time I/O."""

import os

from account_manager.api import Api
from account_manager.client import Client
from account_manager.contract import Contract
from account_manager.native import NativeContract
from account_manager.errors import UserError
from account_manager.service import Service
from account_manager.state import State
from account_manager.ui import button, render, text


def run(params, context):
    state = None
    try:
        env_name = params.get("token_env", "TGTO_OPENAPI_TOKEN")
        if not isinstance(env_name, str) or not env_name.isidentifier():
            raise UserError("Token 环境变量名称无效。")
        token = os.environ.get(env_name) or params.get("api_token", "")
        client = Client(params.get("base_url") or os.environ.get("TGTO_BASE_URL", "http://127.0.0.1:12366"), token)
        state = State(context["data_dir"])
        state.cleanup_qr()
        contract_path = os.environ.get("TGTO_ACCOUNT_CONTRACT")
        contract = Contract(contract_path) if contract_path else NativeContract()
        service = Service(Api(client), contract, state)
        rule_id = params.get("rule_id", "")
        snapshot = service.snapshot(rule_id)
        action = params.get("action", "refresh")
        extras = {"contract_available": contract.available}
        if snapshot.get("scope_changed"):
            extras["notice"] = "已切换规则，临时用户选择、目标和旧预览已清空。"
            # Never process an old result button while switching scopes.
            action = "refresh"
        if rule_id:
            target_id = state.target(service.scope)
            if action in {"add_user", "remove_user"}:
                service.select(snapshot, params.get("user_id", ""), action == "add_user")
            elif action == "choose_account":
                service.account(snapshot, params.get("cloud_account_id", ""))
                state.target(service.scope, params["cloud_account_id"])
            elif action == "clear_selection":
                state.clear(service.scope)
            elif action in {"preview_assign", "preview_unbind"}:
                extras["preview"] = service.preview_binding(snapshot, target_id, "assign" if action == "preview_assign" else "unbind")
            elif action == "confirm_binding":
                extras["results"] = service.execute_binding(snapshot, params.get("preview_id", ""))
                try:
                    snapshot = service.snapshot(rule_id)
                except UserError:
                    extras["notice"] = "逐项结果已保留，但列表刷新失败；当前列表可能已过期，请稍后刷新。"
            elif action == "reconcile":
                extras["results"] = service.reconcile()
            elif action.startswith("preview_"):
                extras["maintenance"] = service.preview_maintenance(snapshot, action.removeprefix("preview_"), target_id,
                        params.get("account_name", ""), params.get("folder_id", ""),
                        params.get("folder_name", ""), params.get("parent_id", "0"), params.get("login_app", "web"))
            elif action == "confirm_maintenance":
                outcome = service.execute_maintenance(snapshot, params.get("preview_id", ""), context["data_dir"])
                extras["notice"] = outcome["detail"]
                extras["qr_image"] = outcome.get("qr_image")
                try:
                    snapshot = service.snapshot(rule_id)
                except UserError:
                    extras["notice"] += " 列表刷新失败，当前列表可能已过期，请稍后刷新。"
            elif action == "qr_status":
                extras["qr_status"] = service.qr_status(snapshot, target_id)
            elif action == "check_login":
                if params.get("cloud_account_id") != target_id:
                    raise UserError("目标小号已变化，请刷新工作台后重新点击检查。")
                outcome = service.check_login(snapshot, target_id, context["data_dir"])
                extras["qr_status"] = outcome.get("qr_status")
                if outcome.get("completion_submitted"):
                    extras["notice"] = outcome["detail"]
                    try:
                        snapshot = service.snapshot(rule_id)
                    except UserError:
                        extras["notice"] += " 列表刷新失败，请稍后刷新核实账号状态。"
            elif action == "folders":
                extras["folders"] = service.folders(snapshot, target_id, params.get("folder_id", "0"))
            elif action not in {"refresh", "history"}:
                raise UserError("不支持的操作。")
            target_id = state.target(service.scope)
            extras["qr_image"] = state.qr_image(service.scope, target_id) if target_id else None
        safe_params = {k: v for k, v in params.items() if k != "api_token"}
        safe_params["base_url"] = client.base_url
        return render(snapshot, state, service.scope, safe_params, **extras)
    except UserError as exc:
        return {"ui": [text(str(exc), "warning", "alert"),
                       text("操作结果以原系统为准；可返回工作台查看待核实记录。", "muted"),
                       button("返回工作台", "refresh")]}
    finally:
        if state is not None:
            state.close()
