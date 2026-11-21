"""Version adapter. No default guesses for the instance's undocumented response fields.

Only an administrator-supplied, reviewed JSON mapping can enable undocumented flows.
The synthetic mapping used in tests is deliberately excluded from the import ZIP.
"""

import json
from pathlib import Path

from .api import identifier
from .errors import ContractError


def lookup(value, path):
    if not isinstance(path, list) or not path or not all(isinstance(k, str) for k in path):
        raise ContractError("版本契约中的字段路径无效。")
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise ContractError("原系统返回字段与已配置的版本契约不一致。")
        value = value[key]
    return value


def string(value, label):
    if not isinstance(value, str):
        raise ContractError(label + "必须是字符串，不能通过浮点数转换。")
    return value


class Contract:
    flow_required = False
    def __init__(self, path=None, *, mapping=None):
        if mapping is None and path:
            try:
                file = Path(path)
                if file.stat().st_size > 65536:
                    raise ValueError()
                mapping = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                raise ContractError("版本契约文件无法读取或格式无效。") from None
        self.mapping = mapping
        if mapping is not None and (not isinstance(mapping, dict) or mapping.get("schema_version") != 1):
            raise ContractError("版本契约版本不支持。")

    @property
    def available(self):
        return self.mapping is not None

    def section(self, name):
        section = self.mapping.get(name) if self.mapping else None
        if not isinstance(section, dict):
            raise ContractError("当前版本的" + name + "字段尚未核实；请先补齐版本契约。")
        return section

    def field(self, name, key, raw):
        return lookup(raw, self.section(name).get(key))

    def account(self, raw, rule_id):
        get = lambda key: self.field("account", key, raw)
        account_id = identifier(get("id"))
        actual_rule = identifier(get("rule_id"))
        provider = string(get("provider"), "云盘类型")
        if actual_rule != rule_id:
            raise ContractError("账号池中出现其他规则的记录，已停止操作。")
        if provider != "115":
            return None
        logged_in, expired = get("logged_in"), get("expired")
        if type(logged_in) is not bool or type(expired) is not bool:
            raise ContractError("登录状态不是已核实的布尔值。")
        folder_id = string(get("folder_id"), "目录 ID")
        folder_ready = get("folder_ready")
        if type(folder_ready) is not bool:
            raise ContractError("接收目录可用状态尚未核实。")
        users = get("users")
        if not isinstance(users, list) or not all(isinstance(u, str) for u in users):
            raise ContractError("账号关联用户摘要结构发生变化。")
        usage_count = get("usage_count")
        if type(usage_count) is not int or usage_count < len(users):
            raise ContractError("账号关联人数与摘要不一致。")
        return {"id": account_id, "rule_id": actual_rule, "provider": provider,
                "name": string(get("name"), "账号名称")[:120],
                "masked_login": string(get("masked_login"), "脱敏标识")[:120],
                "logged_in": logged_in, "expired": expired,
                "folder_id": folder_id, "folder_ready": folder_ready,
                "folder_path": string(get("folder_path"), "目录路径")[:300],
                "users": [u[:120] for u in users], "usage_count": usage_count,
                "ready": logged_in and not expired and bool(folder_id) and folder_ready}

    def binding(self, bindings):
        matches = []
        for raw in bindings:
            if self.field("binding", "provider", raw) == "115":
                matches.append({"id": identifier(self.field("binding", "id", raw)),
                                "name": string(self.field("binding", "name", raw), "账号名称")[:120]})
        if len(matches) > 1:
            raise ContractError("用户同时存在多个 115 绑定，需先核实业务契约。")
        return matches[0] if matches else {"id": "", "name": "未关联"}

    def qr_request(self, action, *, flow_id=None, app="web"):
        section = self.section("qr")
        if section.get("flow_storage") != "native":
            raise ContractError("扫码流程需要授权上下文，当前尚未核实由原系统保存的方式。")
        body = section.get(action + "_request")
        if not isinstance(body, dict) or any(not isinstance(v, (str, int, bool)) for v in body.values()):
            raise ContractError("扫码请求契约不完整。")
        return dict(body)

    def qr_payload(self, raw):
        value = self.field("qr", "payload", raw)
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise ContractError("二维码内容与已核实的格式不一致。")
        return value

    def qr_status(self, raw):
        value = self.field("qr", "status", raw)
        if type(value) not in {int, str}:
            raise ContractError("扫码状态类型未确认。")
        result = self.section("qr").get("states", {}).get(str(value))
        if result not in {"waiting", "scanned", "authorized", "expired", "failed"}:
            raise ContractError("原系统返回未确认的扫码状态。")
        return result

    def folder_query(self, folder_id):
        key = self.section("folders").get("parent_parameter")
        if not isinstance(key, str) or not key:
            raise ContractError("目录浏览参数尚未核实。")
        return {key: string(folder_id, "目录 ID")}

    def folder_body(self, folder_id, folder_name="", parent_id="0"):
        key = self.section("folders").get("selection_parameter")
        if not isinstance(key, str) or not key:
            raise ContractError("接收目录设置参数尚未核实。")
        return {key: string(folder_id, "目录 ID")}

    def folders(self, raw):
        rows = self.field("folders", "items", raw)
        if not isinstance(rows, list):
            raise ContractError("目录列表格式发生变化。")
        return [{"id": string(lookup(r, self.section("folders").get("id")), "目录 ID"),
                 "name": string(lookup(r, self.section("folders").get("name")), "目录名称")[:200]}
                for r in rows]
