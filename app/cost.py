"""基于已保存快照计算每天各渠道成本。

规则：
  某渠道某模型若设了「按次单价」(>0) → 按次：成本 = 单价 × 调用次数
  否则 → 按量：成本 = 平台单位(quota/quota_per_unit) × 渠道倍率(默认1)
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ChannelRatio, ChannelUsageDaily, PerCallPrice


def compute_costs(db: Session, site_id: int, d: date) -> dict:
    qpu = settings.quota_per_unit or 1

    snap = db.scalars(
        select(ChannelUsageDaily).where(
            ChannelUsageDaily.stat_date == d, ChannelUsageDaily.site_id == site_id
        ).order_by(ChannelUsageDaily.channel_id, ChannelUsageDaily.model_name)
    ).all()

    ratios = {
        r.channel_id: r.ratio
        for r in db.scalars(select(ChannelRatio).where(ChannelRatio.site_id == site_id)).all()
    }
    prices = {
        (p.channel_id, p.model_name): p.unit_price
        for p in db.scalars(select(PerCallPrice).where(PerCallPrice.site_id == site_id)).all()
    }

    rows = []
    total = 0.0
    for s in snap:
        units = (s.quota or 0) / qpu
        ratio = ratios.get(s.channel_id, 1.0)
        price = prices.get((s.channel_id, s.model_name))
        ratio_set = s.channel_id in ratios  # 是否显式设置过渠道倍率
        price_set = price is not None and price > 0  # 是否设置过按次单价
        if price_set:
            mode = "per_call"
            cost = price * (s.calls or 0)
        else:
            mode = "usage"
            cost = units * ratio
        total += cost
        rows.append({
            "channel_id": s.channel_id,
            "channel_name": s.channel_name,
            "model_name": s.model_name,
            "units": units,
            "calls": s.calls or 0,
            "mode": mode,
            "ratio": ratio,
            "price": price or 0.0,
            "ratio_set": ratio_set,
            "price_set": price_set,
            "cost": cost,
        })
    # 未确定成本（既没设倍率也没设按次价）的排在最前，方便每天确认新渠道
    rows.sort(key=lambda r: (1 if (r["ratio_set"] or r["price_set"]) else 0, r["channel_id"], r["model_name"]))
    return {"rows": rows, "total": total, "has_snapshot": len(snap) > 0}


def set_ratio(db: Session, site_id: int, channel_id: int, ratio: float) -> None:
    obj = db.scalars(
        select(ChannelRatio).where(
            ChannelRatio.site_id == site_id, ChannelRatio.channel_id == channel_id
        )
    ).first()
    if obj is None:
        obj = ChannelRatio(site_id=site_id, channel_id=channel_id)
        db.add(obj)
    obj.ratio = ratio
    db.commit()


def set_price(db: Session, site_id: int, channel_id: int, model_name: str, unit_price: float) -> None:
    obj = db.scalars(
        select(PerCallPrice).where(
            PerCallPrice.site_id == site_id,
            PerCallPrice.channel_id == channel_id,
            PerCallPrice.model_name == model_name,
        )
    ).first()
    if obj is None:
        obj = PerCallPrice(site_id=site_id, channel_id=channel_id, model_name=model_name)
        db.add(obj)
    obj.unit_price = unit_price
    db.commit()
