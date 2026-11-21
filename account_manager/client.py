"""Small JSON-only HTTP client. It never follows credential-bearing redirects."""

import json
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import ApiError, UserError

MAX_RESPONSE = 2 * 1024 * 1024


def business_error(data, *, uncertain=False, status=None):
    error = data.get("error", "") if isinstance(data, dict) else ""
    if isinstance(error, str) and ("登录流程已过期或已取消" in error or "二维码已过期" in error):
        return ApiError("二维码已过期或被取消，请重新生成。", category="qr_expired", status=status)
    return ApiError("原系统未接受本次操作，请核对账号状态、目录和业务限制。",
                    uncertain=uncertain, status=status)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, base_url, token, *, timeout=12, opener=None):
        base_url = base_url.rstrip("/")
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query
                or parsed.fragment or parsed.path not in {"", "/"}):
            raise UserError("服务地址必须是完整的 HTTP(S) 根地址，不能包含凭据、路径或查询参数。")
        try:
            parsed.port
        except ValueError:
            raise UserError("服务地址端口无效。") from None
        if not isinstance(token, str) or not token.strip():
            raise UserError("未配置运行时 OpenAPI Token，请填写插件 Token 字段，或按 README 设置环境变量。")
        if "\r" in token or "\n" in token:
            raise UserError("OpenAPI Token 格式无效。")
        self.base_url = base_url
        self._token = token.strip()
        self.timeout = timeout
        self.opener = opener or build_opener(NoRedirect())

    def json(self, method, path, *, query=None, body=None):
        if not path.startswith("/api/emby_proxy/") or "?" in path or "#" in path:
            raise UserError("拒绝调用账号管理范围以外的接口。")
        url = self.base_url + path
        if query:
            url += "?" + urlencode(query)
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        attempts = 2 if method == "GET" else 1
        for attempt in range(attempts):
            req = Request(url, data=payload, method=method, headers={
                "Authorization": "Bearer " + self._token,
                "Accept": "application/json", "Content-Type": "application/json",
                "User-Agent": "tgto-115-accounts/0.2.3",
            })
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    if response.status != 200:
                        raise ApiError("接口返回了未确认的状态码。", uncertain=method != "GET")
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                    if content_type != "application/json":
                        raise ApiError("接口未返回 JSON，请检查权限及版本。", uncertain=method != "GET")
                    raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise ApiError("接口结果超出大小限制。", uncertain=method != "GET")
                try:
                    result = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise ApiError("接口 JSON 格式无效。", uncertain=method != "GET") from None
                if not isinstance(result, dict) or type(result.get("success")) is not bool:
                    raise ApiError("接口响应契约发生变化，操作结果需要核实。", uncertain=method != "GET")
                if result["success"] is False:
                    # Do not echo upstream messages: they can contain credentials or URLs.
                    raise business_error(result)
                return result
            except HTTPError as exc:
                status = exc.code
                try:
                    failure = json.loads(exc.read(65536))
                except (ValueError, UnicodeError, OSError):
                    failure = {}
                exc.close()
                if status == 401:
                    raise ApiError("运行时鉴权失败，请检查 OpenAPI Token。", status=status) from None
                if status == 403:
                    raise ApiError("当前 Token 无权限，或操作不满足原系统的业务条件。", status=status) from None
                if 300 <= status < 400:
                    raise ApiError("接口发生重定向，已停止发送凭据。", status=status,
                                   uncertain=method != "GET") from None
                if method == "GET" and status >= 500 and attempt + 1 < attempts:
                    time.sleep(0.2)
                    continue
                if status in {400, 409, 422}:
                    raise business_error(failure, status=status) from None
                raise ApiError("原系统接口不可用，请稍后核实。", status=status,
                               uncertain=method != "GET" and status >= 500) from None
            except (URLError, TimeoutError, socket.timeout, OSError):
                if attempt + 1 < attempts:
                    time.sleep(0.2)
                    continue
                raise ApiError("请求超时或网络不可用；写入结果请先核实。",
                               uncertain=method != "GET") from None
