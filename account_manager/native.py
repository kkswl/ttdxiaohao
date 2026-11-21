"""TgtoDrive v8.9.2 wire contract verified against an authorized live account panel.

No credentials or real account identifiers belong in this module.
"""

from .api import identifier
from .contract import string
from .errors import ContractError

P115_APPS = [
    ("android", "115生活 Android"), ("115android", "115 Android"), ("web", "网页版"),
    ("ios", "115生活 iOS"), ("115ios", "115 iOS"), ("ipad", "115生活 iPad"),
    ("115ipad", "115 iPad"), ("linux", "Linux"), ("mac", "macOS"),
    ("tv", "Android 电视"), ("apple_tv", "Apple TV"), ("qandroid", "115管理 Android"),
    ("qios", "115管理 iOS"), ("qipad", "115管理 iPad"),
    ("alipaymini", "支付宝小程序"), ("wechatmini", "微信小程序"), ("harmony", "鸿蒙"),
]


class NativeContract:
    available = True
    flow_required = True

    def section(self, name):
        if name not in {"account", "binding", "qr", "folders"}:
            raise ContractError("不支持的原生接口类型。")
        return {}

    def account(self, raw, rule_id):
        if not isinstance(raw, dict):
            raise ContractError("原生账号摘要格式无效。")
        account_id = identifier(raw.get("id"))
        if raw.get("cloud_account_id") != account_id or raw.get("rule_id") != rule_id:
            raise ContractError("原生账号标识或规则不一致。")
        if raw.get("provider") != "115":
            return None
        authorized, ready = raw.get("authorized"), raw.get("ready")
        if type(authorized) is not bool or type(ready) is not bool:
            raise ContractError("原生授权状态类型发生变化。")
        users = raw.get("users")
        count = raw.get("user_count")
        if not isinstance(users, list) or type(count) is not int or count < len(users):
            raise ContractError("原生账号关联人数格式发生变化。")
        names = []
        for user in users:
            if isinstance(user, str):
                names.append(user[:120])
            elif isinstance(user, dict) and isinstance(user.get("emby_username"), str):
                names.append(user["emby_username"][:120])
            else:
                raise ContractError("原生关联用户摘要字段尚未确认。")
        folder_id = string(raw.get("folder_id"), "接收目录 ID")
        status = string(raw.get("status"), "原生状态")
        return {
            "id": account_id, "rule_id": rule_id, "provider": "115",
            "name": string(raw.get("display_name"), "显示名称")[:120],
            "masked_login": string(raw.get("account_label"), "脱敏标识")[:120],
            "logged_in": authorized, "expired": False,
            "folder_id": folder_id, "folder_path": string(raw.get("folder_name"), "目录名称")[:300],
            "folder_ready": ready, "ready": authorized and ready and bool(folder_id),
            "users": names, "usage_count": count, "native_status": status,
            "revision": str(raw.get("updated_at", "")),
        }

    def binding(self, bindings):
        matches = []
        for raw in bindings:
            if not isinstance(raw, dict):
                raise ContractError("原生绑定摘要无效。")
            if raw.get("provider") != "115":
                continue
            account_id = identifier(raw.get("cloud_account_id"))
            label = raw.get("display_name") or raw.get("account_label")
            matches.append({"id": account_id, "name": string(label, "关联账号名称")[:120]})
        if len(matches) > 1:
            raise ContractError("用户存在多个 115 绑定，需核实原生业务状态。")
        return matches[0] if matches else {"id": "", "name": "未关联"}

    def qr_request(self, action, *, flow_id=None, app="web"):
        if action == "start":
            if app not in {value for value, _ in P115_APPS}:
                raise ContractError("登录设备不在原系统支持列表中。")
            return {"app": app}
        if action in {"status", "complete"}:
            if not flow_id:
                raise ContractError("没有有效的扫码流程，请重新生成二维码。")
            return {"flow_id": identifier(flow_id)}
        raise ContractError("未知扫码操作。")

    def qr_flow(self, raw):
        return identifier(raw.get("flow_id"))

    def qr_ttl(self, raw):
        ttl = raw.get("expires_in")
        if type(ttl) is not int or not 1 <= ttl <= 3600:
            raise ContractError("二维码有效期格式发生变化。")
        return min(ttl, 300)

    def qr_payload(self, raw):
        qr = raw.get("qr")
        payload = qr.get("qrcode") if isinstance(qr, dict) else None
        if not isinstance(payload, str) or not payload or len(payload) > 4096:
            raise ContractError("原系统没有返回可生成二维码的内容。")
        return payload

    def qr_status(self, raw):
        status = raw.get("status")
        value = status.get("status") if isinstance(status, dict) else None
        if isinstance(value, str) and value.lstrip("-").isdigit():
            value = int(value)
        if type(value) is not int:
            raise ContractError("扫码状态与原生格式不一致。")
        if value < 0:
            return "expired"
        if value not in {0, 1, 2}:
            raise ContractError("原系统返回未识别的扫码状态。")
        return {0: "waiting", 1: "scanned", 2: "authorized"}[value]

    def folder_query(self, folder_id):
        return {"parent_id": string(folder_id, "父目录 ID")}

    def folder_body(self, folder_id, folder_name="", parent_id="0"):
        return {"folder_id": string(folder_id, "目录 ID"),
                "folder_name": string(folder_name, "目录名称"),
                "parent_id": string(parent_id, "父目录 ID")}

    def folders(self, raw):
        folders = raw.get("folders")
        if not isinstance(folders, list):
            raise ContractError("原生目录列表字段发生变化。")
        result = []
        for row in folders:
            if not isinstance(row, dict):
                raise ContractError("原生目录条目无效。")
            result.append({"id": string(row.get("id"), "目录 ID"),
                           "name": string(row.get("name"), "目录名称")[:200]})
        return result
