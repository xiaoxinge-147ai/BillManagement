"""上游访问凭据 + 地址：默认令牌 + 按上游(base_url)覆盖。

- 访问令牌(PAT)：scope='__default__' 为默认；scope=base_url 为该上游覆盖。
  取代原先的账号密码登录 —— 新版 new-api 令牌列表 key 已脱敏，
  登录后靠后缀猜令牌名不再可靠（详见 app/upstream.py 顶部说明）。
- 地址：默认取 base_url 的根(协议://主机:端口)，可对该上游覆盖。
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

from sqlalchemy import distinct, select
from sqlalchemy.orm import Session

from app.models import ChannelMeta, UpstreamCred

DEFAULT = "__default__"


def _get(db: Session, scope: str) -> Optional[UpstreamCred]:
    return db.scalars(select(UpstreamCred).where(UpstreamCred.scope == scope)).first()


def _get_or_create(db: Session, scope: str) -> UpstreamCred:
    o = _get(db, scope)
    if o is None:
        o = UpstreamCred(scope=scope)
        db.add(o)
    return o


def auto_login_url(base_url: str) -> str:
    """默认登录地址 = base_url 的根（去掉中转子路径）。"""
    if not base_url:
        return ""
    parts = urlsplit(base_url)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return base_url.rstrip("/")


def set_cred(db: Session, scope: str, access_token: Optional[str],
             note: Optional[str] = None, user_id: Optional[str] = None) -> None:
    """写入访问令牌。令牌空串不覆盖（留空=不改）；备注与用户ID传了就写（含清空）。"""
    o = _get_or_create(db, scope)
    if access_token is not None and access_token.strip() != "":
        o.access_token = access_token.strip()
    if note is not None:
        o.username = note.strip() or None
    if user_id is not None:
        uid = user_id.strip()
        o.password = uid if uid.isdigit() else None    # 只存纯数字，非法直接清空
    db.commit()


def clear_cred(db: Session, scope: str) -> None:
    """清空某个上游的令牌覆盖，回落到默认令牌。"""
    o = _get(db, scope)
    if o:
        o.access_token = None
        o.password = None       # 用户ID 也一并清，否则会残留一个对不上默认令牌的 id
        db.commit()


def set_login_url(db: Session, base_url: str, login_url: Optional[str]) -> None:
    o = _get_or_create(db, base_url)
    o.login_url = (login_url or "").strip() or None
    db.commit()


def set_status(db: Session, base_url: str, ok: bool, msg: str) -> None:
    o = _get_or_create(db, base_url)
    o.status_ok = ok
    o.status_msg = msg[:250]
    db.commit()


def resolve(db: Session, base_url: str) -> dict:
    """某上游有效配置：地址 + 访问令牌（本站覆盖优先于默认）。"""
    o = _get(db, base_url) if base_url else None
    default = _get(db, DEFAULT)
    token = None
    if o and (o.access_token or "").strip():
        token = o.access_token.strip()
    elif default and (default.access_token or "").strip():
        token = default.access_token.strip()
    # 用户ID跟着令牌来源走：用了本站令牌就用本站的 id，用默认令牌就用默认的 id
    src = o if (o and (o.access_token or "").strip()) else default
    uid = (src.password or "").strip() if src else ""
    login_url = (o.login_url if (o and o.login_url) else auto_login_url(base_url))
    return {"access_token": token, "user_id": (uid if uid.isdigit() else None),
            "login_url": login_url,
            "note": (o.username if o and o.username else
                     (default.username if default else None))}


def list_upstreams(db: Session) -> list[dict]:
    urls = [
        r[0]
        for r in db.execute(
            select(distinct(ChannelMeta.base_url)).where(ChannelMeta.base_url.isnot(None))
        ).all()
        if r[0]
    ]
    default = _get(db, DEFAULT)
    has_default = bool(default and (default.access_token or "").strip())
    out = []
    for u in sorted(urls):
        o = _get(db, u)
        if o and (o.access_token or "").strip():
            acct_state, who = "override", (o.username or "本站令牌")
        elif has_default:
            acct_state, who = "default", (default.username or "默认令牌")
        else:
            acct_state, who = "none", ""
        out.append({
            "base_url": u,
            "acct_state": acct_state,
            "username": who,
            "user_id": (o.password if (o and (o.password or "").strip().isdigit()) else ""),
            "login_url": (o.login_url if (o and o.login_url) else ""),
            "auto_login_url": auto_login_url(u),
            "status_ok": (o.status_ok if o else None),
            "status_msg": (o.status_msg if o else None),
        })
    return out


def get_default(db: Session) -> dict:
    o = _get(db, DEFAULT)
    tok = (o.access_token or "") if o else ""
    return {"note": (o.username if o else "") or "",
            "has_token": bool(tok.strip()),
            "token_tail": ("…" + tok.strip()[-6:]) if tok.strip() else "",
            "user_id": (o.password if (o and (o.password or "").strip().isdigit()) else "")}
