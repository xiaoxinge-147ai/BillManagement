"""账单系统主程序（第 1 步）。

页面：
  /                 站点列表 + 新增/删除总站连接
  /usage            选站点+日期，查看各渠道消耗（消耗量/调用次数）
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import hashlib

from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.cost import compute_costs, set_price, set_ratio
from app.recon import (
    compute_recon, fetch_all_background, fetch_from_upstream, fetch_progress,
    refresh_channel_meta, set_recharge, set_usage,
)
from app.token_cost import compute_token_cost
from app.customer_cost import compute_customer_profit, debug_mapping
from app import creds as creds_mod
from app.db import SessionLocal, get_db, init_db
from app.models import ChannelUsageDaily, CustomerUsageDaily, Site
from app.scheduler import next_run_time, reschedule, start_scheduler, stop_scheduler
from app.snapshot import save_snapshot
from app.source import build_engine

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="账单系统")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _ctx(request: Request) -> dict:
    """当前登录用户 + 可见模块。模板里用 ctx(request) 取，不必每个路由都传参。"""
    from app import auth as _a
    user = getattr(request.state, "user", None)
    return {"user": user,
            "modules": _a.user_modules(user),
            "is_admin": bool(user and user.role == "admin")}


templates.env.globals["ctx"] = _ctx
templates.env.globals["module_name"] = lambda k: __import__(
    "app.auth", fromlist=["MODULE_NAME"]).MODULE_NAME.get(k, k)


@app.on_event("startup")
def _startup() -> None:
    from app.auth import ensure_admin
    from app.bill_export import cleanup_stale_temp
    init_db()
    db = SessionLocal()
    try:
        ensure_admin(db)          # 首次启动创建超管；已存在则按环境变量校准
    finally:
        db.close()
    cleanup_stale_temp()
    start_scheduler()


@app.on_event("shutdown")
def _shutdown() -> None:
    stop_scheduler()


def _yesterday() -> str:
    tz = ZoneInfo(settings.timezone)
    return (datetime.now(tz) - timedelta(days=1)).date().isoformat()


def _resolve_date(request: Request, d: Optional[str]) -> str:
    """日期优先级：URL 参数 > cookie(全局选择) > 昨天。"""
    if d:
        return d
    ck = request.cookies.get("bill_date")
    return ck if ck else _yesterday()


# ---------------- 登录与鉴权 ----------------
from app import auth as auth_mod                                       # noqa: E402
from app.models import AppUser, AuthAudit                              # noqa: E402


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for") or ""
    return (fwd.split(",")[0].strip() or (request.client.host if request.client else "")) or ""


@app.middleware("http")
async def _auth_mw(request: Request, call_next):
    path = request.url.path
    if path in auth_mod.PUBLIC_PATHS:
        return await call_next(request)

    db = SessionLocal()
    try:
        user = auth_mod.read_session(db, request.cookies.get(auth_mod.SESSION_COOKIE))
        if not user:
            resp = RedirectResponse("/login?next=" + quote(path), status_code=303)
            resp.delete_cookie(auth_mod.SESSION_COOKIE)
            return resp
        request.state.user = user     # 先挂上：403 页面也要能渲染出侧边栏
        if not auth_mod.can_access(user, path):
            mod = auth_mod.module_for_path(path)
            if mod:
                what = auth_mod.MODULE_NAME.get(mod, mod)
            elif path.startswith("/users"):
                what = "账号管理"
            elif path.startswith("/scripts"):
                what = "导出脚本"
            else:
                what = path
            # 越权：不跳登录页（那会让人以为掉线），给明确提示 + 保留侧边栏让他能自己去有权限的页面
            return templates.TemplateResponse(
                "message.html",
                {"request": request, "ok": False, "back": auth_mod.landing_path(user),
                 "msg": f"没有访问「{what}」的权限，请联系管理员开通。"},
                status_code=403)
    finally:
        db.close()
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, err: Optional[int] = None, next: str = "/"):
    return templates.TemplateResponse(
        "login.html", {"request": request, "err": err, "next": next})


@app.post("/login")
def login_submit(request: Request, username: str = Form(""), password: str = Form(""),
                 next: str = Form("/"), db: Session = Depends(get_db)):
    name = (username or "").strip()
    user = db.scalars(select(AppUser).where(AppUser.username == name)).first()
    ip = _client_ip(request)
    if not user or not user.active or not auth_mod.verify_password(password, user.password_hash):
        auth_mod.audit(db, name or "?", "login_failed", name, "", ip)
        return RedirectResponse("/login?err=1&next=" + quote(next or "/"), status_code=303)

    user.last_login_at = datetime.now(ZoneInfo(settings.timezone))
    db.commit()
    auth_mod.audit(db, user.username, "login", user.username, "", ip)
    home = auth_mod.landing_path(user)
    dest = next if (next or "").startswith("/") and not (next or "").startswith("//") else home
    if not auth_mod.can_access(user, dest):
        dest = home                       # next 指向无权限的页面时，落到它自己的首页
    resp = RedirectResponse(dest, status_code=303)
    resp.set_cookie(auth_mod.SESSION_COOKIE, auth_mod.make_session(user),
                    httponly=True, samesite="lax", max_age=auth_mod.SESSION_MAX_AGE)
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth_mod.SESSION_COOKIE)
    return resp


# ---------------- 账号管理（仅超管）----------------

@app.get("/users", response_class=HTMLResponse)
def users_view(request: Request, msg: Optional[str] = None,
               err: Optional[str] = None, db: Session = Depends(get_db)):
    users = db.scalars(select(AppUser).order_by(AppUser.role.desc(), AppUser.id)).all()
    cust_sites = db.scalars(
        select(Site).where(Site.site_type == "customer").order_by(Site.id)).all()
    audits = db.scalars(select(AuthAudit).order_by(AuthAudit.id.desc()).limit(30)).all()
    return templates.TemplateResponse("users.html", {
        "request": request, "users": users, "sites": cust_sites,
        "groups": auth_mod.grouped_modules(), "audits": audits,
        "danger_keys": set(auth_mod.DANGEROUS_MODULES),
        "msg": msg, "err": err,
    })


def _read_perm(form) -> tuple:
    mods = [k for k in auth_mod.MODULE_KEYS if form.get("m_" + k)]
    sids = sorted({v for v in form.getlist("site_id") if str(v).strip().isdigit()},
                  key=lambda x: int(x))
    return ",".join(mods), ",".join(sids)


@app.post("/users/create")
async def users_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    name = (form.get("username") or "").strip()
    pw = (form.get("password") or "").strip()
    actor = getattr(request.state, "user", None)
    if not name or not pw:
        return RedirectResponse("/users?err=" + quote("用户名和密码都不能为空"), status_code=303)
    if len(pw) < 6:
        return RedirectResponse("/users?err=" + quote("密码至少 6 位"), status_code=303)
    if db.scalars(select(AppUser).where(AppUser.username == name)).first():
        return RedirectResponse("/users?err=" + quote(f"用户名「{name}」已存在"), status_code=303)

    mods, sids = _read_perm(form)
    db.add(AppUser(username=name, display_name=(form.get("display_name") or "").strip(),
                   role="operator", password_hash=auth_mod.hash_password(pw),
                   modules=mods, site_ids=sids, active=True))
    db.commit()
    danger = auth_mod.dangerous_granted(mods)
    auth_mod.audit(db, actor.username if actor else "?", "create", name,
                   f"模块={mods or '无'} 站点={sids or '不限'}"
                   + (f" ⚠高危={'、'.join(danger)}" if danger else ""), _client_ip(request))
    return RedirectResponse("/users?msg=" + quote(f"已创建运营账号「{name}」"), status_code=303)


@app.post("/users/{uid}/perm")
async def users_perm(uid: int, request: Request, db: Session = Depends(get_db)):
    user = db.get(AppUser, uid)
    actor = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/users?err=" + quote("账号不存在"), status_code=303)
    if user.role == "admin":
        return RedirectResponse("/users?err=" + quote("超级管理员拥有全部权限，无需配置"), status_code=303)
    form = await request.form()
    user.modules, user.site_ids = _read_perm(form)
    user.display_name = (form.get("display_name") or "").strip()
    db.commit()
    danger = auth_mod.dangerous_granted(user.modules)
    auth_mod.audit(db, actor.username if actor else "?", "update", user.username,
                   f"模块={user.modules or '无'} 站点={user.site_ids or '不限'}"
                   + (f" ⚠高危={'、'.join(danger)}" if danger else ""), _client_ip(request))
    return RedirectResponse("/users?msg=" + quote(f"已更新「{user.username}」的权限"), status_code=303)


@app.post("/users/{uid}/password")
def users_password(uid: int, request: Request, password: str = Form(""),
                   db: Session = Depends(get_db)):
    user = db.get(AppUser, uid)
    actor = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/users?err=" + quote("账号不存在"), status_code=303)
    if len((password or "").strip()) < 6:
        return RedirectResponse("/users?err=" + quote("密码至少 6 位"), status_code=303)
    user.password_hash = auth_mod.hash_password(password.strip())
    db.commit()          # 改密码会让该账号的旧会话立即失效（会话里带密码指纹）
    auth_mod.audit(db, actor.username if actor else "?", "reset_pw", user.username,
                   "", _client_ip(request))
    return RedirectResponse("/users?msg=" + quote(f"已重置「{user.username}」的密码"), status_code=303)


@app.post("/users/{uid}/toggle")
def users_toggle(uid: int, request: Request, db: Session = Depends(get_db)):
    user = db.get(AppUser, uid)
    actor = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/users?err=" + quote("账号不存在"), status_code=303)
    if user.role == "admin":
        return RedirectResponse("/users?err=" + quote("不能停用超级管理员"), status_code=303)
    user.active = not user.active
    db.commit()
    auth_mod.audit(db, actor.username if actor else "?",
                   "enable" if user.active else "disable", user.username, "", _client_ip(request))
    return RedirectResponse(
        "/users?msg=" + quote(f"已{'启用' if user.active else '停用'}「{user.username}」"),
        status_code=303)


@app.post("/users/{uid}/delete")
def users_delete(uid: int, request: Request, db: Session = Depends(get_db)):
    user = db.get(AppUser, uid)
    actor = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/users?err=" + quote("账号不存在"), status_code=303)
    if user.role == "admin":
        return RedirectResponse("/users?err=" + quote("不能删除超级管理员"), status_code=303)
    name = user.username
    db.delete(user)
    db.commit()
    auth_mod.audit(db, actor.username if actor else "?", "delete", name, "", _client_ip(request))
    return RedirectResponse("/users?msg=" + quote(f"已删除账号「{name}」"), status_code=303)


@app.get("/me", response_class=HTMLResponse)
def me_view(request: Request, msg: Optional[str] = None, err: Optional[str] = None):
    return templates.TemplateResponse(
        "me.html", {"request": request, "msg": msg, "err": err})


@app.post("/me/password")
def me_password(request: Request, old: str = Form(""), new: str = Form(""),
                db: Session = Depends(get_db)):
    cur = getattr(request.state, "user", None)
    user = db.get(AppUser, cur.id) if cur else None
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not auth_mod.verify_password(old, user.password_hash):
        return RedirectResponse("/me?err=" + quote("原密码不正确"), status_code=303)
    if len((new or "").strip()) < 6:
        return RedirectResponse("/me?err=" + quote("新密码至少 6 位"), status_code=303)
    user.password_hash = auth_mod.hash_password(new.strip())
    db.commit()
    auth_mod.audit(db, user.username, "change_pw", user.username, "", _client_ip(request))
    # 会话里带着密码指纹，改完必须重新登录
    resp = RedirectResponse("/login?msg=" + quote("密码已修改，请重新登录"), status_code=303)
    resp.delete_cookie(auth_mod.SESSION_COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    from app.crypto import mask_dsn
    sites = db.scalars(select(Site).order_by(Site.id)).all()
    masked = {s.id: mask_dsn(s.dsn) for s in sites}
    return templates.TemplateResponse("index.html", {"request": request, "sites": sites, "masked": masked})


@app.post("/sites")
def add_site(
    name: str = Form(...),
    site_type: str = Form("master"),
    dialect: str = Form("mysql"),
    dsn: str = Form(""),
    db: Session = Depends(get_db),
):
    db.add(Site(name=name.strip(), site_type=site_type, dialect=dialect, dsn=dsn.strip()))
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/sites/{site_id}/delete")
def delete_site(site_id: int, db: Session = Depends(get_db)):
    obj = db.get(Site, site_id)
    if obj:
        db.delete(obj)
        db.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/sites/{site_id}/test", response_class=HTMLResponse)
def test_site(site_id: int, request: Request, db: Session = Depends(get_db)):
    site = db.get(Site, site_id)
    ok, msg = False, "站点不存在"
    if site and site.dsn:
        try:
            eng = build_engine(site.dialect, site.dsn)
            with eng.connect() as conn:
                conn.execute(text("SELECT 1"))
            ok, msg = True, "连接成功"
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"连接失败：{exc}"
    return templates.TemplateResponse(
        "message.html", {"request": request, "ok": ok, "msg": msg, "back": "/"}
    )


@app.get("/usage", response_class=HTMLResponse)
def usage(
    request: Request,
    site_id: Optional[int] = None,
    d: Optional[str] = None,
    refresh: Optional[int] = None,
    db: Session = Depends(get_db),
):
    sites = db.scalars(select(Site).where(Site.site_type == "master").order_by(Site.id)).all()
    if site_id is None and sites:
        site_id = sites[0].id
    d = _resolve_date(request, d)

    rows, total_units, total_calls, error, saved = [], 0.0, 0, None, None
    qpu = settings.quota_per_unit or 1
    site = db.get(Site, site_id) if site_id else None
    dd = date.fromisoformat(d)
    if site:
        # 仅在「查询并保存」(refresh=1) 时才回源库拉取落库；平时切 tab 只读本地快照（快）
        if refresh and site.dsn:
            try:
                saved = save_snapshot(db, site, dd)
            except Exception as exc:  # noqa: BLE001
                db.rollback()  # 回滚以免破坏会话导致后续读取也 500
                logging.exception("渠道消耗保存失败")
                error = f"{type(exc).__name__}: {exc}"
        for r in db.scalars(
            select(ChannelUsageDaily).where(
                ChannelUsageDaily.stat_date == dd, ChannelUsageDaily.site_id == site_id
            ).order_by(ChannelUsageDaily.channel_id, ChannelUsageDaily.model_name)
        ).all():
            units = (r.quota or 0) / qpu
            rows.append({
                "channel_id": r.channel_id, "channel_name": r.channel_name,
                "model_name": r.model_name, "units": units, "calls": r.calls or 0,
            })
            total_units += units
            total_calls += r.calls or 0

    saved_count = len(rows)
    return templates.TemplateResponse(
        "usage.html",
        {
            "request": request, "sites": sites, "site": site, "site_id": site_id,
            "d": d, "rows": rows, "total_units": total_units,
            "total_calls": total_calls, "error": error, "saved": saved,
            "saved_count": saved_count,
        },
    )


@app.get("/cost", response_class=HTMLResponse)
def cost_view(
    request: Request,
    site_id: Optional[int] = None,
    d: Optional[str] = None,
    db: Session = Depends(get_db),
):
    sites = db.scalars(select(Site).where(Site.site_type == "master").order_by(Site.id)).all()
    if site_id is None and sites:
        site_id = sites[0].id
    d = _resolve_date(request, d)
    result = {"rows": [], "total": 0.0, "has_snapshot": False}
    if site_id:
        result = compute_costs(db, site_id, date.fromisoformat(d))
    site = db.get(Site, site_id) if site_id else None
    return templates.TemplateResponse(
        "cost.html",
        {
            "request": request, "sites": sites, "site": site, "site_id": site_id,
            "d": d, "rows": result["rows"], "total": result["total"],
            "has_snapshot": result["has_snapshot"],
        },
    )


@app.post("/cost/ratio")
def cost_set_ratio(
    site_id: int = Form(...), channel_id: int = Form(...),
    ratio: float = Form(...), d: str = Form(...), db: Session = Depends(get_db),
):
    set_ratio(db, site_id, channel_id, ratio)
    return RedirectResponse(f"/cost?site_id={site_id}&d={d}", status_code=303)


@app.post("/cost/price")
def cost_set_price(
    site_id: int = Form(...), channel_id: int = Form(...), model_name: str = Form(...),
    unit_price: float = Form(...), d: str = Form(...), db: Session = Depends(get_db),
):
    set_price(db, site_id, channel_id, model_name, unit_price)
    return RedirectResponse(f"/cost?site_id={site_id}&d={d}", status_code=303)


@app.get("/recon", response_class=HTMLResponse)
def recon_view(
    request: Request,
    site_id: Optional[int] = None,
    d: Optional[str] = None,
    msg: Optional[str] = None,
    db: Session = Depends(get_db),
):
    sites = db.scalars(select(Site).where(Site.site_type == "master").order_by(Site.id)).all()
    if site_id is None and sites:
        site_id = sites[0].id
    d = _resolve_date(request, d)
    result = {"rows": [], "has_snapshot": False, "tot_theo": 0.0, "tot_act": 0.0, "tot_diff": 0.0, "tot_pct": None}
    if site_id:
        result = compute_recon(db, site_id, date.fromisoformat(d))
    site = db.get(Site, site_id) if site_id else None
    prog = fetch_progress(site_id, d) if site_id else None
    return templates.TemplateResponse(
        "recon.html",
        {
            "request": request, "sites": sites, "site": site, "site_id": site_id, "d": d,
            "g": settings.recon_green, "a": settings.recon_amber, "msg": msg,
            "fetch_prog": prog, **result,
        },
    )


@app.post("/recon/usage")
def recon_set_usage(
    site_id: int = Form(...), sk: str = Form(...),
    platform_usage: float = Form(...), d: str = Form(...), db: Session = Depends(get_db),
):
    set_usage(db, site_id, date.fromisoformat(d), sk, platform_usage)
    return RedirectResponse(f"/recon?site_id={site_id}&d={d}", status_code=303)


@app.post("/recon/recharge")
def recon_set_recharge(
    site_id: int = Form(...), sk: str = Form(...),
    recharge_ratio: float = Form(...), d: str = Form(...), db: Session = Depends(get_db),
):
    set_recharge(db, site_id, date.fromisoformat(d), sk, recharge_ratio)
    return RedirectResponse(f"/recon?site_id={site_id}&d={d}", status_code=303)


@app.post("/recon/fetch")
def recon_fetch_one(
    site_id: int = Form(...), sk: str = Form(...), base_url: str = Form(...),
    d: str = Form(...), db: Session = Depends(get_db),
):
    ok, msg = fetch_from_upstream(db, site_id, date.fromisoformat(d), sk, base_url)
    flash = ("读取成功：" + msg) if ok else ("读取失败：" + msg)
    return RedirectResponse(f"/recon?site_id={site_id}&d={d}&msg={quote(flash)}", status_code=303)


@app.post("/recon/fetch-all")
def recon_fetch_all(site_id: int = Form(...), d: str = Form(...)):
    started = fetch_all_background(site_id, d)
    flash = "已在后台开始读取，页面会自动刷新进度" if started else "已有读取任务进行中，请稍候"
    return RedirectResponse(f"/recon?site_id={site_id}&d={d}&msg={quote(flash)}", status_code=303)


@app.get("/recon/debug")
def recon_debug(site_id: int, sk: str, d: str, db: Session = Depends(get_db)):
    """诊断某 sk 的上游读取：匹配到的令牌名、token 列表样本、stat 返回。"""
    from app.creds import resolve as resolve_cred
    from app.models import ChannelMeta
    from app.upstream import UpstreamSession

    # 找该 sk 的 base_url
    meta = db.scalars(
        select(ChannelMeta).where(ChannelMeta.site_id == site_id, ChannelMeta.sk == sk)
    ).first()
    base_url = meta.base_url if meta else ""
    cred = resolve_cred(db, base_url)
    login_url = cred.get("login_url") or base_url
    s = UpstreamSession(login_url, cred.get("access_token"), cred.get("user_id") or "")
    ok, msg = s.open()
    if not ok:
        s.close()
        return JSONResponse({"base_url": base_url, "auth": msg})
    try:
        info = s.debug_sk(sk, date.fromisoformat(d))
        info["base_url"] = base_url
        info["login"] = "ok"
        return JSONResponse(info)
    finally:
        s.close()


@app.post("/recon/refresh-meta")
def recon_refresh_meta(site_id: int = Form(...), d: str = Form(...), db: Session = Depends(get_db)):
    site = db.get(Site, site_id)
    if site:
        try:
            refresh_channel_meta(db, site)
        except Exception:  # noqa: BLE001
            pass
    return RedirectResponse(f"/recon?site_id={site_id}&d={d}", status_code=303)


@app.get("/creds", response_class=HTMLResponse)
def creds_view(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "creds.html",
        {
            "request": request,
            "default": creds_mod.get_default(db),
            "upstreams": creds_mod.list_upstreams(db),
        },
    )


@app.post("/creds/default")
def creds_set_default(
    access_token: str = Form(""), note: str = Form(""), user_id: str = Form(""),
    db: Session = Depends(get_db),
):
    creds_mod.set_cred(db, creds_mod.DEFAULT, access_token, note, user_id)
    return RedirectResponse("/creds", status_code=303)


@app.post("/creds/upstream")
def creds_set_upstream(
    base_url: str = Form(...), access_token: str = Form(""), note: str = Form(""),
    user_id: str = Form(""), login_url: str = Form(""), clear: str = Form(""),
    db: Session = Depends(get_db),
):
    if clear:
        creds_mod.clear_cred(db, base_url)     # 清掉本站覆盖，回落默认令牌
    else:
        creds_mod.set_cred(db, base_url, access_token, note, user_id)
    creds_mod.set_login_url(db, base_url, login_url)
    return RedirectResponse("/creds", status_code=303)


def _test_login(db: Session, base_url: str) -> tuple[bool, str]:
    from app.upstream import UpstreamSession
    cred = creds_mod.resolve(db, base_url)
    login_url = cred.get("login_url") or base_url
    if not cred.get("access_token"):
        creds_mod.set_status(db, base_url, False, "未配置访问令牌")
        return False, "未配置访问令牌"
    s = UpstreamSession(login_url, cred.get("access_token"), cred.get("user_id") or "")
    ok, msg = s.open()
    s.close()
    creds_mod.set_status(db, base_url, ok, msg)
    return ok, msg


@app.post("/creds/test")
def creds_test(base_url: str = Form(...), db: Session = Depends(get_db)):
    _test_login(db, base_url)
    return RedirectResponse("/creds", status_code=303)


@app.post("/creds/test-all")
def creds_test_all(db: Session = Depends(get_db)):
    for u in creds_mod.list_upstreams(db):
        _test_login(db, u["base_url"])
    return RedirectResponse("/creds", status_code=303)


@app.get("/tokens", response_class=HTMLResponse)
def tokens_view(
    request: Request,
    site_id: Optional[int] = None,
    d: Optional[str] = None,
    db: Session = Depends(get_db),
):
    sites = db.scalars(select(Site).where(Site.site_type == "master").order_by(Site.id)).all()
    if site_id is None and sites:
        site_id = sites[0].id
    d = _resolve_date(request, d)
    result = {"has_data": False, "tokens": [], "groups": [], "total": 0.0}
    if site_id:
        result = compute_token_cost(db, site_id, date.fromisoformat(d))
    site = db.get(Site, site_id) if site_id else None
    return templates.TemplateResponse(
        "tokens.html",
        {"request": request, "sites": sites, "site": site, "site_id": site_id, "d": d, **result},
    )


@app.get("/customers", response_class=HTMLResponse)
def customers_view(
    request: Request,
    site_id: Optional[int] = None,
    d: Optional[str] = None,
    refresh: Optional[int] = None,
    db: Session = Depends(get_db),
):
    from app.models import CustomerBillMode, GLOBAL_BILLMODE_SITE
    sites = auth_mod.filter_sites(getattr(request.state, "user", None),
                                  db.scalars(select(Site).order_by(Site.id)).all())
    cust_sites = [s for s in sites if s.site_type == "customer"]
    if site_id is None and cust_sites:
        site_id = cust_sites[0].id
    d = _resolve_date(request, d)
    result = {"has_data": False, "rows": [], "groups": [], "tot_cost": 0, "tot_rev": 0, "tot_profit": 0}
    site = db.get(Site, site_id) if site_id else None
    if site and site.site_type == "customer":
        save_err = None
        # 仅在「查询并保存」(refresh=1) 时回源库拉取；平时只读本地快照计算（快）
        if refresh and site.dsn:
            try:
                save_snapshot(db, site, date.fromisoformat(d))
            except Exception as exc:  # noqa: BLE001
                db.rollback()  # 回滚以免破坏会话导致后续计算也 500
                logging.exception("客户站保存失败")
                save_err = f"{type(exc).__name__}: {exc}"
        try:
            result = compute_customer_profit(db, site, date.fromisoformat(d))
        except Exception as exc:  # noqa: BLE001
            logging.exception("客户利润计算失败")
            result = {
                "has_data": False, "rows": [], "groups": [],
                "tot_cost": 0, "tot_rev": 0, "tot_profit": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        if save_err and not result.get("error"):
            result["error"] = "保存当天数据失败：" + save_err
    billmodes = db.scalars(
        select(CustomerBillMode).where(CustomerBillMode.site_id == GLOBAL_BILLMODE_SITE)
        .order_by(CustomerBillMode.model_name)
    ).all()
    return templates.TemplateResponse(
        "customers.html",
        {"request": request, "sites": cust_sites, "site": site, "site_id": site_id, "d": d,
         "billmodes": billmodes, **result},
    )


@app.post("/customers/billmode")
def customers_add_billmode(
    model_name: str = Form(...), site_id: str = Form(""), d: str = Form(""),
    db: Session = Depends(get_db),
):
    """全局标记某模型为「按次」（所有客户站公用，site_id=0）。"""
    from app.models import CustomerBillMode, GLOBAL_BILLMODE_SITE
    m = (model_name or "").strip()
    if m:
        exists = db.scalars(
            select(CustomerBillMode).where(
                CustomerBillMode.site_id == GLOBAL_BILLMODE_SITE,
                CustomerBillMode.group_name == "",
                CustomerBillMode.model_name == m,
            )
        ).first()
        if exists is None:
            db.add(CustomerBillMode(site_id=GLOBAL_BILLMODE_SITE, group_name="", model_name=m, mode="per_call"))
            db.commit()
    suffix = f"?site_id={site_id}&d={d}" if site_id else ""
    return RedirectResponse(f"/customers{suffix}", status_code=303)


@app.post("/customers/billmode/delete")
def customers_del_billmode(
    rid: int = Form(...), site_id: str = Form(""), d: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.models import CustomerBillMode
    o = db.get(CustomerBillMode, rid)
    if o:
        db.delete(o)
        db.commit()
    suffix = f"?site_id={site_id}&d={d}" if site_id else ""
    return RedirectResponse(f"/customers{suffix}", status_code=303)


@app.get("/customers/debug")
def customers_debug(site_id: int, d: str, db: Session = Depends(get_db)):
    site = db.get(Site, site_id)
    if not site:
        return JSONResponse({"error": "站点不存在"})
    return JSONResponse(debug_mapping(db, site, date.fromisoformat(d)))


@app.post("/run-prepare")
def run_prepare(d: str = Form(...), db: Session = Depends(get_db)):
    """一键准备当天：保存所有站点快照 + 后台读取所有总站上游用量。"""
    dd = date.fromisoformat(d)
    sites = db.scalars(select(Site).where(Site.dsn.isnot(None))).all()
    saved = 0
    for s in sites:
        try:
            save_snapshot(db, s, dd)
            saved += 1
        except Exception:  # noqa: BLE001
            logging.exception("一键准备保存失败 site=%s", s.id)
    for s in sites:
        if s.site_type == "master":
            fetch_all_background(s.id, d)
    return RedirectResponse(
        f"/recon?d={d}&msg={quote(f'已保存 {saved} 个站点快照，并在后台读取上游用量')}", status_code=303
    )


@app.get("/report", response_class=HTMLResponse)
def report_view(
    request: Request,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    site_id: Optional[list[str]] = Query(None),
    db: Session = Depends(get_db),
):
    from app.report import customer_report
    cust_sites = auth_mod.filter_sites(getattr(request.state, "user", None), db.scalars(
        select(Site).where(Site.site_type == "customer").order_by(Site.id)
    ).all())
    # 站点多选：不选(或空)=全部；可单选/多选
    sids = set()
    for v in (site_id or []):
        v = (v or "").strip()
        if v.isdigit():
            sids.add(int(v))
    if not end_date:
        end_date = _yesterday()
    if not start_date:
        start_date = (date.fromisoformat(end_date) - timedelta(days=6)).isoformat()
    result = customer_report(
        db, date.fromisoformat(start_date), date.fromisoformat(end_date), sids or None
    )
    return templates.TemplateResponse(
        "report.html",
        {
            "request": request, "sites": cust_sites, "site_ids": sids,
            "start_date": start_date, "end_date": end_date, **result,
        },
    )


def _int_set(values) -> set:
    """表单里的站点 id 容错转 int：空串/非 ASCII 数字/垃圾值一律忽略，不要 500。"""
    out = set()
    for v in (values or []):
        try:
            out.add(int(str(v).strip()))
        except (TypeError, ValueError):
            continue
    return out


@app.get("/export", response_class=HTMLResponse)
def export_view(
    request: Request,
    job: Optional[str] = None,
    msg: Optional[str] = None,
    preset: str = "last_month",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    site_id: Optional[int] = None,
    customer_id: Optional[list[str]] = Query(None),
    q: Optional[str] = None,
    currency: str = "usd",
    daily: Optional[int] = None,
    detail: Optional[int] = None,
    search: Optional[int] = None,
    refresh: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """三步流程：① 选站点 → ② 搜客户 → ③ 生成账单。"""
    from app import bill_export as bx

    cust_sites = auth_mod.filter_sites(getattr(request.state, "user", None), db.scalars(
        select(Site).where(Site.site_type == "customer").order_by(Site.id)
    ).all())
    cids = _int_set(customer_id)
    sd, ed, title = bx.resolve_period(preset, start_date or "", end_date or "")

    # 客户清单要回源库查（结果缓存 5 分钟），只在点「搜索客户」后才做
    customers, cust_err, cached = [], None, False
    if search and site_id:
        try:
            customers, cached = bx.customers_in_period(
                db, site_id, sd, ed, q or "", refresh=bool(refresh))
        except Exception as exc:  # noqa: BLE001
            logging.exception("搜索客户失败")
            cust_err = f"{type(exc).__name__}: {exc}"

    # 已勾选但不在当前搜索结果里的客户，单独回显，免得换个关键词就「丢了」
    picked = [c for c in customers if c["user_id"] in cids]
    extra = sorted(cids - {c["user_id"] for c in customers})

    # 每个客户实际会用哪个导出脚本（客户绑定 > 站点默认 > 系统内置）
    from app import export_scripts as es
    all_scripts = es.list_scripts(db) if not es.guard_enabled() else []
    site_default = None
    if site_id:
        bmap = es.bindings_for_site(db, site_id)
        site_default = next((s for s in all_scripts if s.id == bmap.get(0)), None)
        for c in customers:
            c["script_id"], c["script_name"], c["script_src"] = \
                es.resolve(db, site_id, c["user_id"])

    return templates.TemplateResponse(
        "export.html",
        {
            "request": request, "sites": cust_sites, "site_id": site_id,
            "preset": preset, "title": title,
            "start_date": start_date or sd.isoformat(),
            "end_date": end_date or ed.isoformat(),
            "with_daily": bool(daily), "with_detail": bool(detail),
            "currency": currency if currency in ("usd", "cny") else "usd",
            "columns": bx.visible_columns(bx.load_columns(db)),
            "customers": customers, "customer_ids": cids, "q": q or "",
            "searched": bool(search), "cust_err": cust_err, "cached": cached,
            "picked_count": len(picked) + len(extra), "extra_ids": extra,
            "all_scripts": all_scripts, "site_default": site_default,
            "builtin_name": es.BUILTIN_NAME, "scripts_on": not es.guard_enabled(),
            "job": bx.get_job(job) if job else None, "msg": msg,
        },
    )


@app.post("/export/run")
def export_run(
    request: Request,
    preset: str = Form("last_month"),
    start_date: str = Form(""),
    end_date: str = Form(""),
    site_id: str = Form(""),
    customer_id: Optional[list[str]] = Form(None),
    q: str = Form(""),
    currency: str = Form(""),
    with_daily: str = Form(""),
    with_detail: str = Form(""),
    db: Session = Depends(get_db),
):
    from app import bill_export as bx

    sid = next(iter(_int_set([site_id])), 0)
    cids = sorted(_int_set(customer_id))
    job_id = bx.start_export(
        db, sid, preset, start_date, end_date,
        with_daily in ("on", "1", "true"), with_detail in ("on", "1", "true"),
        customer_ids=cids,
        allowed_sites=auth_mod.allowed_site_ids(getattr(request.state, "user", None)),
        currency=currency,
    )
    params = [("job", job_id), ("preset", preset),
              ("start_date", start_date), ("end_date", end_date)]
    if sid:
        params.append(("site_id", sid))
    params += [("customer_id", c) for c in cids]
    if currency in ("usd", "cny"):
        params.append(("currency", currency))
    if q:
        params += [("q", q), ("search", "1")]
    if with_daily in ("on", "1", "true"):
        params.append(("daily", "1"))
    if with_detail in ("on", "1", "true"):
        params.append(("detail", "1"))
    return RedirectResponse("/export?" + urlencode(params), status_code=303)


# ---------------- 导出脚本 ----------------
def _scripts_page(request, db, *, msg=None, err=None, edit=None, keep=None):
    """渲染脚本页。keep 是「刚提交但没保存成功」的表单内容 ——
    密码填错时原样回填，别让人辛苦粘贴的脚本白丢。"""
    from app import export_scripts as es

    blocked = es.guard_enabled()
    if blocked:
        return templates.TemplateResponse(
            "scripts.html",
            {"request": request, "scripts": [], "used": {}, "edit": None,
             "builtin": es.BUILTIN_NAME, "msg": None, "err": None,
             "entry": es.ENTRY, "blocked": blocked, "audits": [],
             "builtin_replaced": False, "edit_builtin": False,
             "builtin_src": "", "keep": None},
        )
    from app.models import ExportScriptBinding
    binds = db.scalars(select(ExportScriptBinding)).all()
    site_names = {s.id: s.name for s in db.scalars(select(Site)).all()}
    used = {}
    for b in binds:
        used.setdefault(b.script_id, []).append(
            f"{site_names.get(b.site_id, b.site_id)}·" +
            ("站点默认" if not b.customer_id else f"客户{b.customer_id}")
        )
    return templates.TemplateResponse(
        "scripts.html",
        {"request": request, "scripts": es.list_scripts(db), "used": used,
         "edit": es.get_script(db, edit) if edit else None,
         "builtin": es.BUILTIN_NAME, "msg": msg, "err": err,
         "entry": es.ENTRY, "blocked": None, "audits": es.recent_audit(db),
         "builtin_replaced": bool(es.builtin_override(db).strip()),
         "edit_builtin": bool(keep and keep.get("scope") == "builtin"),
         "builtin_src": (keep.get("content") if keep and keep.get("scope") == "builtin"
                         else es.builtin_source(db)),
         "keep": keep},
    )


@app.get("/scripts", response_class=HTMLResponse)
def scripts_view(request: Request, msg: Optional[str] = None, err: Optional[str] = None,
                 edit: Optional[int] = None, db: Session = Depends(get_db)):
    return _scripts_page(request, db, msg=msg, err=err, edit=edit)


@app.get("/scripts/download")
def scripts_download(sid: int = 0, db: Session = Depends(get_db)):
    """下载脚本源码。sid=0 下载系统内置默认脚本（就是线上正在跑的那份）。"""
    from app import export_scripts as es

    if es.guard_enabled():
        return RedirectResponse("/scripts", status_code=303)
    if sid:
        obj = es.get_script(db, sid)
        if not obj:
            return RedirectResponse("/scripts", status_code=303)
        content, name = obj.content, obj.name
    else:
        content, name = es.builtin_source(db), "默认导出脚本"
    return Response(
        content=content.encode("utf-8"),
        media_type="text/x-python; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{quote(name + '.py')}"},
    )


@app.post("/scripts/builtin")
async def scripts_builtin(request: Request, db: Session = Depends(get_db)):
    """替换或恢复「系统默认」脚本本身。"""
    from app import export_scripts as es

    blocked = es.guard_enabled()
    if blocked:
        return RedirectResponse("/scripts?err=" + quote(blocked), status_code=303)
    form = await request.form()
    ip = request.client.host if request.client else ""
    restore = bool(form.get("restore"))
    content = "" if restore else str(form.get("content") or "")
    up = form.get("file")
    if not restore and up is not None and getattr(up, "filename", ""):
        content = (await up.read()).decode("utf-8", "replace")
    actor = getattr(request.state, "user", None)
    try:
        es.check_confirm(str(form.get("confirm") or ""), actor)
        if not restore and not content.strip():
            raise ValueError("脚本内容为空")
        es.set_builtin_override(db, content)
        es.audit(db, "restore_builtin" if restore else "replace_builtin",
                 es.BUILTIN_NAME, content, ip)
    except ValueError as exc:
        # 不重定向：原页返回并回填内容，密码填错不该让人重写一遍脚本
        return _scripts_page(request, db, err=str(exc),
                             keep={"scope": "builtin", "content": content})
    return RedirectResponse(
        "/scripts?msg=" + quote("已恢复出厂默认脚本" if restore else "已替换系统默认脚本"),
        status_code=303)


@app.post("/scripts/save")
async def scripts_save(request: Request, db: Session = Depends(get_db)):
    from app import export_scripts as es

    blocked = es.guard_enabled()
    if blocked:
        return RedirectResponse("/scripts?err=" + quote(blocked), status_code=303)
    form = await request.form()
    sid = next(iter(_int_set([form.get("sid")])), 0)
    name = str(form.get("name") or "")
    note = str(form.get("note") or "")
    content = str(form.get("content") or "")
    up = form.get("file")
    if up is not None and getattr(up, "filename", ""):
        content = (await up.read()).decode("utf-8", "replace")
    ip = request.client.host if request.client else ""
    actor = getattr(request.state, "user", None)
    try:
        es.check_confirm(str(form.get("confirm") or ""), actor)
        obj = es.save_script(db, sid, name, note, content)
        es.audit(db, "save", obj.name, content, ip)
        return RedirectResponse(
            "/scripts?msg=" + quote(f"已保存脚本「{obj.name}」（已记入审计）"), status_code=303)
    except ValueError as exc:
        # 密码填错 / 校验不过 → 原页返回，脚本内容与名称原样保留
        return _scripts_page(request, db, err=str(exc), edit=sid or None,
                             keep={"scope": "script", "sid": sid, "name": name,
                                   "note": note, "content": content})


@app.post("/scripts/delete")
def scripts_delete(request: Request, sid: int = Form(...), confirm: str = Form(""),
                   db: Session = Depends(get_db)):
    from app import export_scripts as es

    blocked = es.guard_enabled()
    if blocked:
        return RedirectResponse("/scripts?err=" + quote(blocked), status_code=303)
    obj = es.get_script(db, sid)
    try:
        es.check_confirm(confirm, getattr(request.state, "user", None))
    except ValueError as exc:
        return _scripts_page(request, db, err=str(exc))
    name = obj.name if obj else str(sid)
    es.delete_script(db, sid)
    es.audit(db, "delete", name, "", request.client.host if request.client else "")
    return RedirectResponse("/scripts?msg=" + quote(f"已删除「{name}」，相关客户回落到默认脚本"),
                            status_code=303)


@app.post("/export/bind")
def export_bind(
    site_id: int = Form(...), customer_id: int = Form(...), script_id: int = Form(0),
    back: str = Form("/export"), db: Session = Depends(get_db),
):
    """给客户（customer_id=0 表示整个站点）绑定导出脚本。"""
    from app import export_scripts as es

    es.set_binding(db, site_id, customer_id, script_id)
    return RedirectResponse(back or "/export", status_code=303)


@app.post("/export/cancel")
def export_cancel(job_id: str = Form(...)):
    from app import bill_export as bx

    bx.cancel_job(job_id)
    return RedirectResponse(f"/export?job={job_id}", status_code=303)


@app.get("/export/download/{job_id}")
def export_download(job_id: str):
    from app import bill_export as bx

    import os

    j = bx.get_job(job_id)
    # 任务可能已被 TTL 淘汰、文件已删 —— 直接回列表页，别让 FileResponse 抛出临时路径
    if not j or j.get("status") != "done" or not j.get("file") or not os.path.exists(j["file"]):
        return RedirectResponse(
            "/export?msg=" + quote("该导出结果已过期或被清理，请重新生成"), status_code=303)
    return FileResponse(
        j["file"],
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=j.get("filename") or "bill.xlsx",
    )


@app.post("/export/columns")
async def export_save_columns(request: Request, db: Session = Depends(get_db)):
    from app import bill_export as bx

    form = await request.form()
    order = [k for k in (form.get("col_order") or "").split(",") if k]
    labels = {k[6:]: str(v) for k, v in form.items() if k.startswith("label_")}
    visible = {k[4:] for k in form if k.startswith("vis_")}
    if order:
        bx.save_columns(db, order, labels, visible)
    # 带着原来的站点/账期/搜索/勾选回去 —— 否则保存个列设置就把上面选好的全清了
    return RedirectResponse(_export_back(form), status_code=303)


def _export_back(form) -> str:
    """从表单里取回导出页的当前条件，拼成返回地址。"""
    keep = [(k, form.get(k)) for k in
            ("site_id", "preset", "start_date", "end_date", "q")
            if (form.get(k) or "").strip()]
    for cid in form.getlist("customer_id"):
        if str(cid).strip():
            keep.append(("customer_id", cid))
    for k in ("daily", "detail", "search"):
        if form.get(k):
            keep.append((k, "1"))
    return "/export?" + urlencode(keep) if keep else "/export"


@app.post("/export/columns/reset")
async def export_reset_columns(request: Request, db: Session = Depends(get_db)):
    from app import bill_export as bx

    form = await request.form()
    bx.reset_columns(db)
    return RedirectResponse(_export_back(form), status_code=303)


def _cron_to_time(cron: str) -> str:
    """把简单的「每天 m h * * *」(或 6 段) cron 还原成 HH:MM，复杂表达式返回空。"""
    parts = (cron or "").split()
    try:
        if len(parts) == 5 and parts[2:] == ["*", "*", "*"]:
            return f"{int(parts[1]):02d}:{int(parts[0]):02d}"
        if len(parts) == 6 and parts[3:] == ["*", "*", "*"]:
            return f"{int(parts[2]):02d}:{int(parts[1]):02d}"
    except (ValueError, IndexError):
        pass
    return ""


@app.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request, msg: Optional[str] = None, db: Session = Depends(get_db)):
    from app.appconfig import get_schedule
    enabled, cron = get_schedule(db)
    nrt = next_run_time()
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request, "enabled": enabled, "cron": cron,
            "run_time": _cron_to_time(cron), "tz": settings.timezone,
            "next_run": nrt.strftime("%Y-%m-%d %H:%M:%S") if nrt else None,
            "msg": msg,
        },
    )


@app.post("/settings")
def settings_save(
    enabled: str = Form(""), run_time: str = Form(""), cron_advanced: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.appconfig import set_schedule
    en = enabled in ("on", "1", "true")
    adv = (cron_advanced or "").strip()
    if adv:
        cron = adv
    elif run_time and ":" in run_time:
        hh, mm = run_time.split(":")[:2]
        try:
            cron = f"{int(mm)} {int(hh)} * * *"
        except ValueError:
            cron = settings.save_cron
    else:
        cron = settings.save_cron
    set_schedule(db, en, cron)
    reschedule(en, cron)
    flash = "已保存：" + ("已启用，cron=" + cron if en else "已停用")
    return RedirectResponse(f"/settings?msg={quote(flash)}", status_code=303)


@app.get("/saved", response_class=HTMLResponse)
def saved_view(request: Request, db: Session = Depends(get_db)):
    qpu = settings.quota_per_unit or 1
    site_names = {s.id: s.name for s in db.scalars(select(Site)).all()}
    data = []
    # 总站渠道快照
    for r in db.execute(
        select(ChannelUsageDaily.stat_date, ChannelUsageDaily.site_id,
               func.sum(ChannelUsageDaily.quota), func.sum(ChannelUsageDaily.calls), func.count())
        .group_by(ChannelUsageDaily.stat_date, ChannelUsageDaily.site_id)
    ).all():
        data.append({"stat_date": str(r[0]), "site_name": str(site_names.get(r[1], r[1])),
                     "kind": "总站渠道", "units": float(r[2] or 0) / qpu, "calls": int(r[3] or 0), "n": int(r[4] or 0)})
    # 客户站消耗快照
    for r in db.execute(
        select(CustomerUsageDaily.stat_date, CustomerUsageDaily.site_id,
               func.sum(CustomerUsageDaily.quota), func.sum(CustomerUsageDaily.calls), func.count())
        .group_by(CustomerUsageDaily.stat_date, CustomerUsageDaily.site_id)
    ).all():
        data.append({"stat_date": str(r[0]), "site_name": str(site_names.get(r[1], r[1])),
                     "kind": "客户站消耗", "units": float(r[2] or 0) / qpu, "calls": int(r[3] or 0), "n": int(r[4] or 0)})
    data.sort(key=lambda x: (x["stat_date"], x["site_name"]), reverse=True)
    return templates.TemplateResponse("saved.html", {"request": request, "data": data})
