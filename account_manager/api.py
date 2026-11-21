"""Known endpoint paths from the v8.9.2 instance. No global 115 endpoint exists here."""

import re
from urllib.parse import quote, unquote, urlsplit

from .errors import ContractError, UserError


def identifier(value):
    if (not isinstance(value, str) or not value or value in {".", ".."} or len(value) > 200
            or any(c in value for c in "/\\%?#\r\n")):
        raise ContractError("接口标识符格式不符合已核实契约。")
    return value


class Api:
    def __init__(self, client):
        self.client = client

    def rules(self):
        result = self.client.json("GET", "/api/emby_proxy/config")
        rules = result.get("rules")
        if not isinstance(rules, list):
            raise ContractError("反代规则响应缺少 rules 数组。")
        # Never return host/api_key or other native configuration to the UI or state.
        return [{"id": identifier(r["id"]), "name": str(r.get("name", ""))[:120],
                 "enabled": r.get("enabled") is True} for r in rules]

    def accounts(self, rule_id):
        result = self.client.json("GET", "/api/emby_proxy/cloud-accounts", query={"id": rule_id})
        if not isinstance(result.get("cloud_accounts"), list):
            raise ContractError("账号池响应缺少 cloud_accounts 数组。")
        return result["cloud_accounts"]

    def users(self, rule_id):
        result = self.client.json("GET", "/api/emby_proxy/existing-users", query={"id": rule_id})
        users = result.get("users")
        if not isinstance(users, list):
            raise ContractError("已有用户响应缺少 users 数组。")
        clean = []
        for u in users:
            if (not isinstance(u, dict) or not isinstance(u.get("bindings"), list)
                    or not isinstance(u.get("emby_username"), str)
                    or type(u.get("is_administrator")) is not bool
                    or type(u.get("is_disabled")) is not bool):
                raise ContractError("已有用户字段与当前已核实的结构不一致。")
            clean.append({"id": identifier(u.get("emby_user_id")), "name": u["emby_username"][:120],
                          "admin": u["is_administrator"], "disabled": u["is_disabled"],
                          "bindings": u["bindings"]})
        return clean

    def create(self, rule_id, display_name):
        return self.client.json("POST", "/api/emby_proxy/cloud-accounts/create",
                                body={"rule_id": rule_id, "provider": "115", "display_name": display_name})

    def rename(self, cloud_account_id, display_name):
        return self.client.json("POST", "/api/emby_proxy/cloud-accounts/rename",
                                body={"cloud_account_id": cloud_account_id, "display_name": display_name})

    def delete(self, cloud_account_id):
        return self.client.json("POST", "/api/emby_proxy/cloud-accounts/delete",
                                body={"cloud_account_id": cloud_account_id})

    @staticmethod
    def account_path(cloud_account_id):
        return "/api/emby_proxy/cloud-accounts/" + quote(identifier(cloud_account_id), safe="")

    def account_session(self, cloud_account_id):
        return self.client.json("GET", self.account_path(cloud_account_id) + "/api/session")

    def prepare(self, rule_id, emby_user_id):
        result = self.client.json("POST", "/api/emby_proxy/existing-users/prepare",
                                  body={"rule_id": rule_id, "emby_user_id": emby_user_id})
        account = result.get("account")
        if not isinstance(account, dict):
            raise ContractError("prepare 没有返回用户管理记录。")
        url = result.get("binding_url")
        if not isinstance(url, str):
            raise ContractError("prepare 没有返回 binding_url。")
        parsed = urlsplit(url)
        base = urlsplit(self.client.base_url)
        if ((parsed.scheme or parsed.netloc) and (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc)):
            raise UserError("原系统返回了其他服务地址的绑定链接，已停止发送凭据。")
        match = re.fullmatch(r"/api/emby_proxy/existing-users/([^/]+)/cloud", parsed.path)
        if not match or parsed.query or parsed.fragment:
            raise ContractError("绑定链接不属于已有用户绑定接口。")
        identifier(unquote(match.group(1)))
        return parsed.path

    def binding_session(self, binding_path):
        return self.client.json("GET", binding_path + "/api/session")

    def assign(self, binding_path, cloud_account_id):
        return self.client.json("POST", binding_path + "/api/cloud/115/assign",
                                body={"cloud_account_id": cloud_account_id})

    def unbind(self, binding_path):
        return self.client.json("POST", binding_path + "/api/cloud/115/unbind", body={})

    def qr_start(self, cloud_account_id, body):
        return self.client.json("POST", self.account_path(cloud_account_id) + "/api/115/qr/start", body=body)

    def qr_status(self, cloud_account_id, query):
        return self.client.json("GET", self.account_path(cloud_account_id) + "/api/115/qr/status", query=query)

    def qr_complete(self, cloud_account_id, body):
        return self.client.json("POST", self.account_path(cloud_account_id) + "/api/115/qr/complete", body=body)

    def folders(self, cloud_account_id, query):
        return self.client.json("GET", self.account_path(cloud_account_id) + "/api/cloud/115/folders", query=query)

    def folder(self, cloud_account_id, body):
        return self.client.json("POST", self.account_path(cloud_account_id) + "/api/cloud/115/folder", body=body)
