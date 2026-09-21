"""第5步：令牌成本（含按模型拆分，支持按次按调用次数分摊）。

流程：
  1) sk 实际成本(对账) → 按「渠道×模型」的理论成本占比拆到每个 (渠道,模型) → AC[c,m]；
  2) AC[c,m] 在令牌间按「该模型计费方式」的度量分摊：按量用 quota，按次用调用次数；
  3) 得到每个令牌「按模型」的成本与总成本。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.cost import compute_costs
from app.models import ChannelMeta, SkActual, TokenChannelUsage


def compute_token_cost(db: Session, site_id: int, d: date) -> dict:
    qpu = settings.quota_per_unit or 1

    # 渠道×模型 的理论成本与计费方式（复用渠道成本计算）
    theo: dict[tuple, float] = {}
    billing: dict[tuple, str] = {}
    sk_theo_quota: dict[tuple, float] = {}
    for r in compute_costs(db, site_id, d)["rows"]:
        cm = (r["channel_id"], r["model_name"])
        theo[cm] = float(r["cost"] or 0)
        billing[cm] = r["mode"]  # 'usage' | 'per_call'
        sk_theo_quota[cm] = float(r["units"] or 0)

    has_data = db.scalar(
        select(TokenChannelUsage.id).where(
            TokenChannelUsage.stat_date == d, TokenChannelUsage.site_id == site_id
        ).limit(1)
    ) is not None

    # sk 实际成本
    sk_cost = {
        a.sk: a.platform_usage * a.recharge_ratio
        for a in db.scalars(
            select(SkActual).where(
                SkActual.site_id == site_id, SkActual.stat_date == d, SkActual.has_usage == True  # noqa: E712
            )
        ).all()
    }
    sk_by_channel = {
        m.channel_id: (m.sk or "")
        for m in db.scalars(select(ChannelMeta).where(ChannelMeta.site_id == site_id)).all()
    }

    # 每个 sk 的理论成本合计（用于把 sk 实际成本拆到 渠道×模型）
    sk_theo_total: dict[str, float] = defaultdict(float)
    sk_quota_total: dict[str, float] = defaultdict(float)
    for (c, m), t in theo.items():
        sk = sk_by_channel.get(c, "")
        if sk:
            sk_theo_total[sk] += t
            sk_quota_total[sk] += sk_theo_quota[(c, m)]

    # AC[c,m] = sk实际成本 × 该(c,m)理论成本 / 该sk理论成本合计（理论为0时退回按quota）
    AC: dict[tuple, float] = {}
    for (c, m), t in theo.items():
        sk = sk_by_channel.get(c, "")
        cost = sk_cost.get(sk)
        if cost is None:
            AC[(c, m)] = 0.0
            continue
        denom = sk_theo_total.get(sk, 0.0)
        if denom > 0:
            AC[(c, m)] = cost * t / denom
        else:
            qd = sk_quota_total.get(sk, 0.0)
            AC[(c, m)] = cost * (sk_theo_quota[(c, m)] / qd) if qd > 0 else 0.0

    # 令牌×渠道×模型 用量
    tcu = db.scalars(
        select(TokenChannelUsage).where(
            TokenChannelUsage.stat_date == d, TokenChannelUsage.site_id == site_id
        )
    ).all()
    # 每个 (c,m) 的度量合计（按量=quota，按次=calls）
    metric_total: dict[tuple, float] = defaultdict(float)
    for r in tcu:
        cm = (r.channel_id, r.model_name)
        mode = billing.get(cm, "usage")
        metric_total[cm] += (r.calls or 0) if mode == "per_call" else (r.quota or 0)

    # 令牌成本（按模型）
    model_costs: dict[int, dict] = defaultdict(lambda: defaultdict(lambda: {"cost": 0.0, "mode": "usage"}))
    token_total: dict[int, float] = defaultdict(float)
    token_quota: dict[int, float] = defaultdict(float)
    token_name: dict[int, str] = {}
    token_group: dict[int, str] = {}
    token_known: dict[int, bool] = defaultdict(lambda: True)
    for r in tcu:
        cm = (r.channel_id, r.model_name)
        mode = billing.get(cm, "usage")
        metric = (r.calls or 0) if mode == "per_call" else (r.quota or 0)
        tot = metric_total.get(cm, 0)
        cost = AC.get(cm, 0.0) * (metric / tot) if tot > 0 else 0.0
        e = model_costs[r.token_id][r.model_name]
        e["cost"] += cost
        e["mode"] = mode
        token_total[r.token_id] += cost
        token_quota[r.token_id] += (r.quota or 0)
        token_name[r.token_id] = r.token_name
        token_group[r.token_id] = r.group_name or ""
        sk = sk_by_channel.get(r.channel_id, "")
        if (r.quota or r.calls) and (not sk or sk not in sk_cost):
            token_known[r.token_id] = False

    tokens = []
    group_cost: dict[str, float] = defaultdict(float)
    total = 0.0
    for tid, tcost in token_total.items():
        tokens.append({
            "token_id": tid, "token_name": token_name.get(tid, ""),
            "group_name": token_group.get(tid, ""), "quota": token_quota[tid],
            "units": token_quota[tid] / qpu, "cost": tcost, "known": token_known[tid],
        })
        group_cost[token_group.get(tid, "")] += tcost
        total += tcost
    tokens.sort(key=lambda x: (x["group_name"], -x["cost"]))
    groups = [{"group_name": g, "cost": c} for g, c in sorted(group_cost.items())]

    return {
        "has_data": has_data, "tokens": tokens, "groups": groups, "total": total,
        "model_costs": {tid: {m: dict(v) for m, v in mc.items()} for tid, mc in model_costs.items()},
    }
