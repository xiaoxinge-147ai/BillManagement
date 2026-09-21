"""保存/读取每日渠道消耗快照。"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    ChannelMeta, ChannelUsageDaily, CustomerGroupRatio, CustomerUsageDaily, Site,
    TokenChannelUsage, TokenMeta,
)
from app.source import (
    build_engine, channel_info, channel_usage, customer_group_ratio_used,
    customer_usage, day_range, group_ratios, list_tokens, token_channel_usage,
)

logger = logging.getLogger(__name__)


def _sync_channel_meta(db: Session, site: Site, infos: list[dict]) -> dict[int, str]:
    """更新渠道元信息(sk/base_url/name)，返回 渠道id->名称。"""
    names: dict[int, str] = {}
    for c in infos:
        cid = int(c["channel_id"])
        names[cid] = c.get("name") or ""
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
        obj.base_url = (c.get("base_url") or "")
    return names


def _save_group_ratios(db: Session, site: Site, eng) -> None:
    """从客户站读取分组倍率，覆盖式存到本地（用于按量 1倍率换算）。"""
    ratios = group_ratios(eng, site.dialect)
    if not ratios:
        return
    db.execute(delete(CustomerGroupRatio).where(CustomerGroupRatio.site_id == site.id))
    for g, r in ratios.items():
        db.add(CustomerGroupRatio(site_id=site.id, group_name=g, ratio=r))


def _save_customer(db: Session, site: Site, eng, start_ts: int, end_ts: int, d: date) -> int:
    """客户站：保存 客户×分组×渠道 消耗到本地，并同步分组倍率。"""
    _save_group_ratios(db, site, eng)
    db.execute(
        delete(CustomerUsageDaily).where(
            CustomerUsageDaily.stat_date == d, CustomerUsageDaily.site_id == site.id
        )
    )
    rows = customer_usage(eng, site.dialect, start_ts, end_ts)
    # 消耗时实际应用的分组倍率(含专属)，按 (分组, user_id)
    used_ratio = customer_group_ratio_used(eng, site.dialect, start_ts, end_ts)
    # 按唯一键(客户,分组,渠道,模型)内存聚合：兼容 model_name 等为 NULL/空 塌缩成同键
    agg: dict[tuple, dict] = {}
    for r in rows:
        gname = r.get("group_name") or ""
        uid = int(r["user_id"] or 0)
        cid = int(r["channel_id"] or 0)
        mn = r.get("model_name") or ""
        e = agg.setdefault((uid, gname, cid, mn), {
            "username": r.get("username"), "quota": 0, "tokens": 0, "calls": 0,
            "group_ratio": float(used_ratio.get((gname, uid), 0.0) or 0.0),
        })
        e["quota"] += int(r["quota"] or 0)
        e["tokens"] += int(r.get("tokens") or 0)
        e["calls"] += int(r["calls"] or 0)
    for (uid, gname, cid, mn), e in agg.items():
        db.add(
            CustomerUsageDaily(
                stat_date=d, site_id=site.id,
                customer_id=uid, username=e["username"],
                group_name=gname, channel_id=cid, model_name=mn,
                quota=e["quota"], tokens=e["tokens"],
                group_ratio=e["group_ratio"], calls=e["calls"],
            )
        )
    db.commit()
    return len(agg)


def _save_token_meta(db: Session, site: Site, eng) -> None:
    """总站：保存令牌 key→id 映射到本地。"""
    db.execute(delete(TokenMeta).where(TokenMeta.site_id == site.id))
    for t in list_tokens(eng, site.dialect):
        db.add(TokenMeta(
            site_id=site.id, token_id=int(t["token_id"]),
            token_name=t.get("token_name"), sk=str(t.get("sk") or ""),
        ))
    db.commit()


def save_snapshot(db: Session, site: Site, d: date) -> int:
    """从源库读取并保存某站某天的消耗快照。返回保存行数。

    总站(master)：渠道×模型、令牌×渠道、令牌元信息、渠道元信息。
    客户站(customer)：客户×分组×渠道、渠道元信息。
    """
    if not site.dsn:
        raise ValueError("该站点未配置 DSN")
    eng = build_engine(site.dialect, site.dsn)
    start_ts, end_ts = day_range(d)
    names = _sync_channel_meta(db, site, channel_info(eng, site.dialect))

    if site.site_type == "customer":
        n = _save_customer(db, site, eng, start_ts, end_ts, d)
        db.commit()
        return n

    # 总站
    raw = channel_usage(eng, start_ts, end_ts)

    # 覆盖式：先删当天该站旧快照
    db.execute(
        delete(ChannelUsageDaily).where(
            ChannelUsageDaily.stat_date == d, ChannelUsageDaily.site_id == site.id
        )
    )
    # 按唯一键(渠道,模型)在内存聚合：兼容源库里 model_name 为 NULL/空 而塌缩成同键的情况
    chan_agg: dict[tuple, dict] = {}
    for r in raw:
        cid = int(r["channel_id"] or 0)
        mn = (r["model_name"] or "")
        e = chan_agg.setdefault((cid, mn), {"quota": 0, "calls": 0})
        e["quota"] += int(r["quota"] or 0)
        e["calls"] += int(r["calls"] or 0)
    for (cid, mn), e in chan_agg.items():
        db.add(
            ChannelUsageDaily(
                stat_date=d, site_id=site.id, channel_id=cid,
                channel_name=names.get(cid, ""), model_name=mn,
                quota=e["quota"], calls=e["calls"],
            )
        )

    # 令牌×渠道 消耗（用于令牌成本摊分）
    db.execute(
        delete(TokenChannelUsage).where(
            TokenChannelUsage.stat_date == d, TokenChannelUsage.site_id == site.id
        )
    )
    tok_agg: dict[tuple, dict] = {}
    for r in token_channel_usage(eng, site.dialect, start_ts, end_ts):
        tid = int(r["token_id"] or 0)
        cid = int(r["channel_id"] or 0)
        mn = (r.get("model_name") or "")
        e = tok_agg.setdefault((tid, cid, mn), {
            "group_name": r.get("group_name") or "", "token_name": r.get("token_name"),
            "quota": 0, "calls": 0,
        })
        e["quota"] += int(r["quota"] or 0)
        e["calls"] += int(r["calls"] or 0)
    for (tid, cid, mn), e in tok_agg.items():
        db.add(
            TokenChannelUsage(
                stat_date=d, site_id=site.id, group_name=e["group_name"],
                token_id=tid, token_name=e["token_name"], channel_id=cid,
                model_name=mn, quota=e["quota"], calls=e["calls"],
            )
        )

    _save_token_meta(db, site, eng)
    db.commit()
    return len(chan_agg)


def save_all_sites(db: Session, d: date) -> dict:
    """保存所有已配置 DSN 的站点某天快照。"""
    result = {}
    sites = db.scalars(select(Site).where(Site.dsn.isnot(None))).all()
    for s in sites:
        if not s.dsn:
            continue
        try:
            result[s.id] = save_snapshot(db, s, d)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.exception("保存快照失败 site=%s", s.id)
            result[s.id] = f"失败: {exc}"
    return result


def snapshot_count(db: Session, site_id: int, d: date) -> int:
    return db.scalar(
        select(func.count()).select_from(ChannelUsageDaily).where(
            ChannelUsageDaily.stat_date == d, ChannelUsageDaily.site_id == site_id
        )
    ) or 0
