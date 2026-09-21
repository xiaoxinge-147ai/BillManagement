"""敏感字段对称加密（Fernet）。用于 DSN 等加密存储。"""
from __future__ import annotations

import base64
import hashlib
import re

from cryptography.fernet import Fernet

from app.config import settings

_PREFIX = "enc::"


def _fernet() -> Fernet:
    secret = settings.crypto_secret or "bill-default-key-please-set-CRYPTO_SECRET"
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt(value):
    if value is None:
        return None
    return _PREFIX + _fernet().encrypt(str(value).encode()).decode()


def decrypt(value):
    if value is None:
        return None
    if not str(value).startswith(_PREFIX):
        return value  # 兼容历史明文
    try:
        return _fernet().decrypt(str(value)[len(_PREFIX):].encode()).decode()
    except Exception:  # noqa: BLE001
        return None


def is_encrypted(value) -> bool:
    return bool(value) and str(value).startswith(_PREFIX)


def mask_dsn(dsn: str) -> str:
    """脱敏展示：隐藏密码段 ://user:****@host。"""
    if not dsn:
        return ""
    return re.sub(r"(://[^:/@]+:)[^@]*(@)", r"\1****\2", dsn)
