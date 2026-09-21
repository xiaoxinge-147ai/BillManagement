"""对账（按 sk）：

- 理论成本：把渠道成本按 sk 合并（同一 sk 多渠道合计）。
- 实际成本：平台用量 × 充值倍率。平台用量可「从上游用 sk 读取」或「手动录入」；充值倍率可手动调整。
- 差异额 = 理论 − 实际；差异% = 差异额 ÷ 实际；按阈值上色。
"""
from __future__ import annotations

import threading
from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.cost import compute_costs
from app.creds import resolve as resolve_cred
from app.models import ChannelMeta, Site, SkActual
from app.source import build_engine, channel_info
from app.upstream import UpstreamSession, fetch_usage


def _color(has_actual: bool, actual: float, theoretical: float, pct):
    if not has_actual:
        return "none"
    if actual == 0:
        return "red" if theoretical > 0 else "green"
    a = abs(pct)
    if a < settings.recon_green:
        return "green"
    if a < settings.recon_amber:
        return "amber"
    return "red"


def _mask(sk: str) -> str:
    if not sk:
        return "（无 sk）"
    return f"***{sk[-6:]}" if len(sk) > 6 else "***"


def refresh_channel_meta(db: Session, site: Site) -> int:
    """从源库重新读取渠道 sk/base_url 更新到 channel_meta（无需重存快照）。"""
    if not site.dsn:
        raise ValueError("该站点未配置 DSN")
    eng = build_engine(site.dialect, site.dsn)
    infos = channel_info(eng, site.dialect)
    for c in infos:
        cid = int(c["channel_id"])
        obj = db.scalars(
            select(ChannelMeta).where(
                ChannelMeta.site_id == site.id, ChannelMeta.channel_id == cid
            )
        ).first()
        if obj is None:
            obj = ChannelMeta(site_id=site.id, channel_id=cid)
            db.add(obj)
        obj.name = c.get("name")
        obj.sk = c.get("sk")
        obj.base_url = c.get("base_url") or ""
    db.commit()
    return len(infos)


def _channel_meta(db: Session, site_id: int) -> dict[int, dict]:
    return {
        m.channel_id: {"sk": m.sk or "", "base_url": m.base_url or "", "name": m.name or ""}
        for m in db.scalars(select(ChannelMeta).where(ChannelMeta.site_id == site_id)).all()
    }


def compute_recon(db: Session, site_id: int, d: date) -> dict:
    cost = compute_costs(db, site_id, d)
    meta = _channel_meta(db, site_id)

    # 按 sk 合并理论成本；无 sk 的渠道各自成组
    groups: dict[str, dict] = {}
    for r in cost["rows"]:
        cid = r["channel_id"]
        m = meta.get(cid, {})
        sk = m.get("sk") or ""
        gkey = sk if sk else f"__ch_{cid}"
        g = groups.setdefault(gkey, {
            "sk": sk, "base_url": m.get("base_url", ""),
            "channels": {}, "theoretical": 0.0,
        })
        g["theoretical"] += r["cost"]
        g["channels"][cid] = r["channel_name"] or m.get("name", "")
        if not g["base_url"]:
            g["base_url"] = m.get("base_url", "")

    actuals = {
        a.sk: a
        for a in db.scalars(
            select(SkActual).where(SkActual.site_id == site_id, SkActual.stat_date == d)
        ).all()
    }
    # 历史充值倍率（该日之前最近一次），用作未录入时的默认显示
    hist_ratio: dict[str, float] = {}
    for a in db.scalars(
        select(SkActual).where(SkActual.site_id == site_id, SkActual.stat_date < d)
        .order_by(SkActual.stat_date)
    ).all():
        hist_ratio[a.sk] = a.recharge_ratio

    rows = []
    tot_theo = tot_act = 0.0
    for gkey in sorted(groups):
        g = groups[gkey]
        sk = g["sk"]
        a = actuals.get(sk)
        has_actual = bool(a and a.has_usage)
        usage = a.platform_usage if a else 0.0
        ratio = a.recharge_ratio if a else hist_ratio.get(sk, 1.0)
        actual = usage * ratio
        theo = g["theoretical"]
        diff = theo - (actual if has_actual else 0.0)
        pct = (diff / actual * 100) if (has_actual and actual != 0) else None
        login_url = resolve_cred(db, g["base_url"]).get("login_url") if g["base_url"] else ""
        rows.append({
            "sk": sk,
            "sk_mask": _mask(sk),
            "base_url": g["base_url"],
            "login_url": login_url,
            "channels": ", ".join(f"{cid}:{name}" for cid, name in sorted(g["channels"].items())),
            "theoretical": theo,
            "usage": usage,
            "ratio": ratio,
            "actual": actual,
            "has_actual": has_actual,
            "source": a.source if a else None,
            "diff": diff,
            "pct": pct,
            "color": _color(has_actual, actual, theo, pct if pct is not None else 0),
            "can_fetch": bool(sk and g["base_url"]),
        })
        tot_theo += theo
        if has_actual:
            tot_act += actual

    tot_diff = tot_theo - tot_act
    tot_pct = (tot_diff / tot_act * 100) if tot_act != 0 else None
    return {
        "rows": rows, "has_snapshot": cost["has_snapshot"],
        "tot_theo": tot_theo, "tot_act": tot_act, "tot_diff": tot_diff, "tot_pct": tot_pct,
    }


def _get_or_create(db: Session, site_id: int, d: date, sk: str) -> SkActual:
    obj = db.scalars(
        select(SkActual).where(
            SkActual.site_id == site_id, SkActual.stat_date == d, SkActual.sk == sk
        )
    ).first()
    if obj is None:
        # 充值倍率同步历史：沿用该 sk 最近一次设置过的充值倍率
        prev = db.scalars(
            select(SkActual).where(
                SkActual.site_id == site_id, SkActual.sk == sk, SkActual.stat_date < d
            ).order_by(SkActual.stat_date.desc())
        ).first()
        obj = SkActual(site_id=site_id, stat_date=d, sk=sk,
                       recharge_ratio=(prev.recharge_ratio if prev else 1.0))
        db.add(obj)
    return obj


def set_usage(db: Session, site_id: int, d: date, sk: str, usage: float) -> None:
    obj = _get_or_create(db, site_id, d, sk)
    obj.platform_usage = usage
    obj.has_usage = True
    obj.source = "manual"
    db.commit()


def set_recharge(db: Session, site_id: int, d: date, sk: str, ratio: float) -> None:
    obj = _get_or_create(db, site_id, d, sk)
    obj.recharge_ratio = ratio
    db.commit()


def fetch_from_upstream(db: Session, site_id: int, d: date, sk: str, base_url: str) -> tuple[bool, str]:
    cred = resolve_cred(db, base_url)  # 默认/单独账密 + 登录地址
    login_url = cred.get("login_url") or base_url
    ok, usage, msg = fetch_usage(login_url, cred.get("access_token"), sk, d, cred.get("user_id") or "")
    if not ok:
        return False, msg
    obj = _get_or_create(db, site_id, d, sk)
    obj.platform_usage = usage
    obj.has_usage = True
    obj.source = "upstream"
    db.commit()
    return True, msg


# ---------------- 后台批量读取（站点多时避免请求超时/崩溃）----------------
_FETCH: dict[str, dict] = {}
_FLOCK = threading.Lock()


def _fkey(site_id: int, d_iso: str) -> str:
    return f"{site_id}:{d_iso}"


def fetch_progress(site_id: int, d_iso: str):
    return _FETCH.get(_fkey(site_id, d_iso))


def fetch_all_background(site_id: int, d_iso: str) -> bool:
    """启动后台线程读取全部上游用量。返回是否新启动。"""
    k = _fkey(site_id, d_iso)
    with _FLOCK:
        cur = _FETCH.get(k)
        if cur and cur.get("status") == "running":
            return False
        _FETCH[k] = {"status": "running", "total": 0, "done": 0, "ok": 0, "fail": 0, "errors": []}
    threading.Thread(target=_fetch_worker, args=(site_id, d_iso, k), daemon=True).start()
    return True


def _fetch_worker(site_id: int, d_iso: str, k: str) -> None:
    from app.db import SessionLocal
    db = SessionLocal()
    prog = _FETCH[k]
    try:
        d = date.fromisoformat(d_iso)
        rec = compute_recon(db, site_id, d)
        by_base: dict[str, list[str]] = defaultdict(list)
        for r in rec["rows"]:
            if r["can_fetch"]:
                by_base[r["base_url"]].append(r["sk"])
        prog["total"] = sum(len(v) for v in by_base.values())
        for base_url, sks in by_base.items():
            cred = resolve_cred(db, base_url)
            login_url = cred.get("login_url") or base_url
            session = UpstreamSession(login_url, cred.get("access_token"), cred.get("user_id") or "")
            ok, msg = session.open()
            if not ok:
                prog["fail"] += len(sks)
                prog["done"] += len(sks)
                if len(prog["errors"]) < 30:
                    prog["errors"].append(f"{base_url}: {msg}")
                session.close()
                continue
            try:
                for sk in sks:
                    u_ok, usage, u_msg = session.usage_for_sk(sk, d)
                    if u_ok:
                        obj = _get_or_create(db, site_id, d, sk)
                        obj.platform_usage = usage
                        obj.has_usage = True
                        obj.source = "upstream"
                        prog["ok"] += 1
                    else:
                        prog["fail"] += 1
                        if len(prog["errors"]) < 30:
                            prog["errors"].append(f"{_mask(sk)}: {u_msg}")
                    prog["done"] += 1
                db.commit()
            finally:
                session.close()
        prog["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        prog["status"] = "error"
        prog["errors"].append(str(exc))
    finally:
        db.close()


def fetch_all(db: Session, site_id: int, d: date) -> dict:
    """对当天所有可读取(sk+base_url)的分组从上游读取用量。

    按 base_url 分组：同一上游只登录一次、复用会话查其下所有 sk。
    """
    result = {"ok": 0, "fail": 0, "errors": []}
    rec = compute_recon(db, site_id, d)

    # 按 base_url 聚合需要读取的 sk
    by_base: dict[str, list[str]] = {}
    for r in rec["rows"]:
        if r["can_fetch"]:
            by_base.setdefault(r["base_url"], []).append(r["sk"])

    for base_url, sks in by_base.items():
        cred = resolve_cred(db, base_url)
        login_url = cred.get("login_url") or base_url
        session = UpstreamSession(login_url, cred.get("access_token"), cred.get("user_id") or "")
        ok, msg = session.open()
        if not ok:
            result["fail"] += len(sks)
            result["errors"].append(f"{base_url}: {msg}")
            session.close()
            continue
        try:
            for sk in sks:
                u_ok, usage, u_msg = session.usage_for_sk(sk, d)
                if u_ok:
                    obj = _get_or_create(db, site_id, d, sk)
                    obj.platform_usage = usage
                    obj.has_usage = True
                    obj.source = "upstream"
                    result["ok"] += 1
                else:
                    result["fail"] += 1
                    result["errors"].append(f"{_mask(sk)}: {u_msg}")
            db.commit()
        finally:
            session.close()
    return result
