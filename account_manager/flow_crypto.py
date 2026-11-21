"""Encrypt the short-lived native flow handle; never persist the API token."""

import hashlib
import os

from .errors import UserError


class FlowCipher:
    def __init__(self, base_url, token):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            raise UserError("运行环境缺少扫码上下文保护依赖 cryptography。") from None
        key = hashlib.sha256(b"tgto-115-qr-v1" + bytes([0]) + base_url.encode() +
                             bytes([0]) + token.encode()).digest()
        self.aes = AESGCM(key)

    @staticmethod
    def aad(scope, account_id):
        return (scope + "\0" + account_id).encode()

    def encrypt(self, flow_id, scope, account_id):
        nonce = os.urandom(12)
        return bytes([1]) + nonce + self.aes.encrypt(nonce, flow_id.encode(), self.aad(scope, account_id))

    def decrypt(self, encrypted, scope, account_id):
        try:
            if not isinstance(encrypted, bytes) or encrypted[:1] != bytes([1]) or len(encrypted) < 30:
                raise ValueError()
            return self.aes.decrypt(encrypted[1:13], encrypted[13:], self.aad(scope, account_id)).decode()
        except Exception:
            raise UserError("扫码上下文已失效或鉴权配置已变化，请重新生成二维码。") from None
