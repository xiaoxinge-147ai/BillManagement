"""账号与权限。

- 超管（role='admin'）拥有全部模块，且只有它能管账号。
- 运营（role='operator'）按「侧边栏模块」授权，还能限定可访问的客户站。
- 会话用签名 cookie（HMAC），不落库；改密码/停用会让旧会话立即失效。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AppUser, AuthAudit

logger = logging.getLogger(__name__)

SESSION_COOKIE = "bill_session"
SESSION_MAX_AGE = 7 * 86400

# ---------------------------------------------------------------- 模块定义

# key, 名称, 分组, 该模块涵盖的路由前缀
MODULES = [
    ("sites",     "站点",        "基础",      ["/", "/sites"]),
    ("creds",     "供应商令牌",  "基础",      ["/creds"]),
    ("settings",  "设置",        "基础",      ["/settings"]),
    ("usage",     "渠道消耗",    "成本核算",  ["/usage"]),
    ("cost",      "渠道成本",    "成本核算",  ["/cost"]),
    ("recon",     "对账",        "成本核算",  ["/recon", "/run-prepare"]),
    ("tokens",    "令牌成本",    "成本核算",  ["/tokens"]),
    ("customers", "客户利润",    "客户与账单", ["/customers"]),
    ("report",    "周/月报表",   "客户与账单", ["/report"]),
    ("export",    "账单导出",    "客户与账单", ["/export"]),
    ("saved",     "保存记录",    "客户与账单", ["/saved"]),
    ("scripts",   "导出脚本",    "高危权限",  ["/scripts"]),
]
MODULE_KEYS = [m[0] for m in MODULES]
MODULE_NAME = {m[0]: m[1] for m in MODULES}

# 高危模块：授予后等同交出服务器。配置页会单独分组 + 红字警示。
# scripts 会 exec() 用户提交的 Python（app/export_scripts.py），
# 拿到它就能读到所有客户站的 DSN 明文与整个账单库。
DANGEROUS_MODULES = {
    "scripts": "可在服务器上执行任意代码，能读取所有客户站的数据库密码，等同服务器权限",
}

ADMIN_ONLY_PREFIXES = ["/users"]          # 账号管理，仅超管
PUBLIC_PATHS = {"/login", "/logout", "/favicon.ico", "/health"}
# 登录即可访问，不受模块授权限制（个人设置）
SELF_PREFIXES = ["/me"]


def module_for_path(path: str) -> Optional[str]:
    """把请求路径映射到模块 key。返回 None 表示不属于任何受控模块。

    按前缀长度从长到短匹配，避免 "/" 抢走 "/cost" 这类。
    """
    best, best_len = None, -1
    for key, _name, _grp, prefixes in MODULES:
        for p in prefixes:
            if p == "/":
                if path == "/" and best_len < 1:
                    best, best_len = key, 1
            elif path == p or path.startswith(p + "/") or path.startswith(p + "?"):
                if len(p) > best_len:
                    best, best_len = key, len(p)
    return best


def grouped_modules():
    """按分组返回 [(分组名, 是否高危, [(key, 名称, 警示语), ...])]，供权限配置页渲染。

    高危分组排到最后，页面上单独用红框标出来。
    """
    out, seen = [], {}
    for key, name, grp, _ in MODULES:
        if grp not in seen:
            seen[grp] = []
            out.append([grp, False, seen[grp]])
        seen[grp].append((key, name, DANGEROUS_MODULES.get(key, "")))
    for row in out:
        row[1] = any(k in DANGEROUS_MODULES for k, _n, _w in row[2])
    out.sort(key=lambda r: 1 if r[1] else 0)      # 高危分组沉底
    return [(g, d, items) for g, d, items in out]


def dangerous_granted(modules_csv: str) -> list:
    """从逗号分隔的模块串里挑出高危项，用于审计与二次确认。"""
    got = [m for m in (modules_csv or "").split(",") if m in DANGEROUS_MODULES]
    return [MODULE_NAME.get(m, m) for m in got]


# ---------------------------------------------------------------- 密码

def hash_password(pw: str, *, salt: Optional[bytes] = None, rounds: int = 120_000) -> str:
    """PBKDF2-HMAC-SHA256。格式：pbkdf2$轮数$salt_b64$hash_b64。"""
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, rounds)
    return "pbkdf2${}${}${}".format(
        rounds, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(pw: str, stored: str) -> bool:
    if not stored or not stored.startswith("pbkdf2$"):
        return False
    try:
        _, rounds, salt_b64, hash_b64 = stored.split("$", 3)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(),
                                 base64.b64decode(salt_b64), int(rounds))
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------- 会话

def _secret() -> bytes:
    """签名密钥。优先用 CRYPTO_SECRET，其次 APP_PASSWORD，都没有则用机器相关的兜底。"""
    raw = (settings.crypto_secret or settings.app_password
           or "bill-session-fallback-please-set-CRYPTO_SECRET")
    return hashlib.sha256(("session::" + raw).encode()).digest()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_session(user: AppUser) -> str:
    """会话串：uid.过期时间.密码指纹.签名

    带密码指纹是为了「改密码 / 停用后旧会话立即失效」—— 不用在服务端存会话表。
    """
    exp = int(time.time()) + SESSION_MAX_AGE
    fp = hashlib.sha256((user.password_hash or "").encode()).hexdigest()[:8]
    payload = f"{user.id}.{exp}.{fp}"
    return f"{payload}.{_sign(payload)}"


def read_session(db: Session, raw: Optional[str]) -> Optional[AppUser]:
    """校验会话串并取回用户。任何一步不对都返回 None。"""
    if not raw or raw.count(".") != 3:
        return None
    uid_s, exp_s, fp, sig = raw.split(".")
    payload = f"{uid_s}.{exp_s}.{fp}"
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    try:
        if int(exp_s) < time.time():
            return None
        user = db.get(AppUser, int(uid_s))
    except (ValueError, TypeError):
        return None
    if not user or not user.active:
        return None
    if hashlib.sha256((user.password_hash or "").encode()).hexdigest()[:8] != fp:
        return None                       # 密码已改，旧会话作废
    return user


# ---------------------------------------------------------------- 权限判定

def user_modules(user: Optional[AppUser]) -> list:
    if not user:
        return []
    if user.role == "admin":
        return list(MODULE_KEYS)
    return [m for m in (user.modules or "").split(",") if m in MODULE_KEYS]


def can_access(user: Optional[AppUser], path: str) -> bool:
    if not user:
        return False
    if any(path == p or path.startswith(p + "/") for p in SELF_PREFIXES):
        return True                       # 任何登录用户都能改自己的密码
    if any(path == p or path.startswith(p + "/") for p in ADMIN_ONLY_PREFIXES):
        return user.role == "admin"
    if user.role == "admin":
        return True
    mod = module_for_path(path)
    if mod is None:
        return True                       # 不属于受控模块（如静态/健康检查）
    return mod in user_modules(user)


MODULE_HOME = {m[0]: (m[3][0] if m[3][0] != "/" else "/") for m in MODULES}


def landing_path(user) -> str:
    """该账号登录后该落在哪个页面 —— 取它有权限的第一个模块。

    不能写死 "/"：运营账号多半没有「站点」权限，一登录就会撞 403。
    """
    if not user:
        return "/login"
    if user.role == "admin":
        return "/"
    mods = user_modules(user)
    for key in MODULE_KEYS:                   # 按侧边栏顺序取第一个有权限的
        if key in mods and key not in DANGEROUS_MODULES:
            return MODULE_HOME.get(key, "/")
    for key in MODULE_KEYS:                   # 只剩高危模块时也得有个落脚点
        if key in mods:
            return MODULE_HOME.get(key, "/")
    return "/me"                              # 一个模块都没授权，至少能改密码


def allowed_site_ids(user: Optional[AppUser]) -> Optional[set]:
    """该账号可访问的客户站 id 集合。None = 不限。"""
    if not user or user.role == "admin":
        return None
    raw = (user.site_ids or "").strip()
    if not raw:
        return None
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out or None


def filter_sites(user: Optional[AppUser], sites: list) -> list:
    """按账号的站点授权过滤站点列表（只限制客户站，总站属于成本侧）。"""
    allow = allowed_site_ids(user)
    if allow is None:
        return sites
    return [s for s in sites if s.site_type != "customer" or s.id in allow]


# ---------------------------------------------------------------- 初始化与审计

def audit(db: Session, actor: Optional[str], action: str,
          target: str = "", detail: str = "", ip: str = "") -> None:
    try:
        db.add(AuthAudit(actor=actor, action=action, target=target,
                         detail=detail[:2000], ip=ip[:64]))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def ensure_admin(db: Session) -> None:
    """确保存在超管账号。用户名/密码取自环境变量 ADMIN_USER / ADMIN_PASSWORD。

    已存在同名账号时只在 ADMIN_PASSWORD 有值且与当前不符时更新密码，
    这样重启不会覆盖管理员在页面上改过的密码（除非显式改了环境变量）。
    """
    name = (settings.admin_user or "admin").strip() or "admin"
    pw = (settings.admin_password or "").strip()

    user = db.scalars(select(AppUser).where(AppUser.username == name)).first()
    if user is None:
        if not pw:
            # 没配密码就退回旧的 APP_PASSWORD，再没有就给个一次性随机密码并打日志
            pw = (settings.app_password or "").strip()
            if pw:
                logger.warning("未设置 ADMIN_PASSWORD，超管 %s 沿用 APP_PASSWORD 登录", name)
            else:
                pw = secrets.token_urlsafe(12)
                logger.warning("=" * 60)
                logger.warning("首次启动：已创建超级管理员 %s，密码 %s", name, pw)
                logger.warning("请登录后立即在「账号管理」里修改密码。")
                logger.warning("=" * 60)
        db.add(AppUser(username=name, display_name="超级管理员", role="admin",
                       password_hash=hash_password(pw), active=True))
        db.commit()
        return

    user.role = "admin"                   # 环境变量指定的账号始终是超管
    user.active = True
    if pw and not verify_password(pw, user.password_hash):
        user.password_hash = hash_password(pw)
        logger.info("超管 %s 的密码已按 ADMIN_PASSWORD 更新", name)
    db.commit()
