"""第6步：客户成本与利润（按模型分摊，支持按次按调用次数）。

- 客户站 channel_id → 渠道 key → 总站令牌（含 master_site_id）。
- 令牌成本「按模型」来自 token_cost.model_costs；每个 (令牌,模型) 的成本，
  在客户间按该模型计费方式的度量分摊：按量=quota 占比，按次=调用次数占比。
- 营收 = 客户扣除额度/quota_per_unit；利润 = 营收 − 成本。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    GLOBAL_BILLMODE_SITE, ChannelMeta, CustomerBillMode, CustomerGroupRatio,
    CustomerUsageDaily, Site, TokenMeta,
)
from app.token_cost import compute_token_cost


def _norm(k: str) -> str:
    k = (k or "").strip()
    return k[3:] if k.startswith("sk-") else k


def _build_index(db: Session, d: date):
    """返回 (key_index, model_costs)。
    key_index: norm(key) -> {token_id, token_name, master_site_id}
    model_costs: {(master_site_id, token_id): {model: {cost, mode}}}
    """
    key_index = {}
    model_costs = {}
    for m in db.scalars(select(Site).where(Site.site_type == "master")).all():
        tc = compute_token_cost(db, m.id, d)
        for tid, mc in tc["model_costs"].items():
            model_costs[(m.id, tid)] = mc
        for tm in db.scalars(select(TokenMeta).where(TokenMeta.site_id == m.id)).all():
            key_index[_norm(tm.sk or "")] = {
                "token_id": tm.token_id, "token_name": tm.token_name, "master_site_id": m.id,
            }
    return key_index, model_costs


def compute_customer_profit(db: Session, customer_site: Site, d: date) -> dict:
    qpu = settings.quota_per_unit or 1
    empty = {"has_data": False, "customers": [], "groups": [], "tot_cost": 0, "tot_rev": 0, "tot_profit": 0, "tot_margin": None}

    usage = db.scalars(
        select(CustomerUsageDaily).where(
            CustomerUsageDaily.stat_date == d, CustomerUsageDaily.site_id == customer_site.id
        )
    ).all()
    if not usage:
        return empty

    key_index, model_costs = _build_index(db, d)
    chan_token = {
        c.channel_id: key_index.get(_norm(c.sk or ""))
        for c in db.scalars(select(ChannelMeta).where(ChannelMeta.site_id == customer_site.id)).all()
    }

    # 分组倍率自动读取自客户站(new-api options)，作为日志取不到倍率时的回退
    group_ratio = {
        r.group_name: r.ratio
        for r in db.scalars(select(CustomerGroupRatio).where(CustomerGroupRatio.site_id == customer_site.id)).all()
    }
    # 按次模型为全局配置（所有客户站公用，site_id=0）
    bill_marks = {}
    for r in db.scalars(select(CustomerBillMode).where(CustomerBillMode.site_id == GLOBAL_BILLMODE_SITE)).all():
        bill_marks[(r.group_name or "", r.model_name)] = r.mode

    def _row_ratio(u):
        """消耗时实际应用的倍率：优先用日志记录的实际值(含专属)，否则回退分组倍率。"""
        if u.group_ratio and u.group_ratio > 0:
            return float(u.group_ratio)
        return group_ratio.get(u.group_name or "", 1.0) or 1.0

    def _mode(group, model):
        if (group, model) in bill_marks:
            return bill_marks[(group, model)]
        if ("", model) in bill_marks:
            return bill_marks[("", model)]
        return "usage"

    def _info(u):
        return chan_token.get(u.channel_id)

    def _modecost(info, model):
        if not info:
            return None
        mc = model_costs.get((info["master_site_id"], info["token_id"]), {})
        return mc.get(model)

    # 按量：1倍率消耗 = quota / 消耗时实际倍率；按次：调用次数
    def _metric(u, mode):
        if mode == "per_call":
            return float(u.calls or 0)
        return float(u.quota or 0) / _row_ratio(u)

    # Pass1: 每个 (master,token,model) 的度量合计
    tm_total: dict[tuple, float] = defaultdict(float)
    for u in usage:
        info = _info(u)
        if not info:
            continue
        mode = _mode(u.group_name or "", u.model_name)
        tm_total[(info["master_site_id"], info["token_id"], u.model_name)] += _metric(u, mode)

    # Pass2: 聚合到 (客户,分组)，并保留每模型明细
    cg = defaultdict(lambda: {"cost": 0.0, "rev": 0.0, "calls": 0, "tokens": set(), "mapped": False, "username": None, "models": {}})
    for u in usage:
        q = float(u.quota or 0)
        rev = q / qpu
        info = _info(u)
        cost = 0.0
        mode = "usage"
        mapped = False
        if info:
            mc = _modecost(info, u.model_name)
            if mc:
                mode = _mode(u.group_name or "", u.model_name)
                metric = _metric(u, mode)
                tot = tm_total.get((info["master_site_id"], info["token_id"], u.model_name), 0)
                if tot > 0:
                    cost = mc["cost"] * metric / tot
                mapped = True
        g = u.group_name or ""
        dmode = _mode(g, u.model_name)
        model_cost = mc["cost"] if (info and mc) else 0.0
        total_metric = tm_total.get((info["master_site_id"], info["token_id"], u.model_name), 0) if info else 0
        e = cg[(u.customer_id, g)]
        e["username"] = u.username
        e["cost"] += cost
        e["rev"] += rev
        e["calls"] += int(u.calls or 0)
        md = e["models"].setdefault(u.model_name or "", {"mode": dmode, "units1x": 0.0, "calls": 0, "cost": 0.0, "model_cost": 0.0, "total_metric": 0.0})
        md["mode"] = dmode
        md["units1x"] += float(u.quota or 0) / _row_ratio(u)
        md["calls"] += int(u.calls or 0)
        md["cost"] += cost
        md["model_cost"] = model_cost
        md["total_metric"] = total_metric
        if info:
            e["tokens"].add(info["token_name"])
            e["mapped"] = True

    customers_map = {}
    group_agg = defaultdict(lambda: {"cost": 0.0, "rev": 0.0, "tokens": set()})
    tot_cost = tot_rev = 0.0
    for (uid, g), e in cg.items():
        prof = e["rev"] - e["cost"]
        models = []
        for mn, v in e["models"].items():
            per_call = v["mode"] == "per_call"
            if per_call:
                cust_metric = v["calls"]
                total_metric = v["total_metric"]
            else:
                # 按量度量以「消耗金额(平台单位)」展示：1倍率消耗(quota/倍率) ÷ quota_per_unit
                cust_metric = v["units1x"] / qpu
                total_metric = v["total_metric"] / qpu
            share = (v["cost"] / v["model_cost"]) if v["model_cost"] else None
            models.append({
                "model": mn, "mode": v["mode"],
                "metric_name": "调用次数" if per_call else "消耗金额",
                "cust_metric": cust_metric, "total_metric": total_metric,
                "share": share, "model_cost": v["model_cost"], "cost": v["cost"],
            })
        models.sort(key=lambda x: -x["cost"])
        grow = {
            "group_name": g, "revenue": e["rev"], "cost": e["cost"], "profit": prof,
            "margin": (prof / e["rev"] * 100) if e["rev"] else None,
            "token_name": ", ".join(sorted(t for t in e["tokens"] if t)) or None,
            "mapped": e["mapped"], "calls": e["calls"], "models": models,
        }
        c = customers_map.setdefault(uid, {
            "customer_id": uid, "username": e["username"], "cost": 0.0, "revenue": 0.0, "groups": [],
        })
        c["cost"] += e["cost"]
        c["revenue"] += e["rev"]
        c["groups"].append(grow)
        tot_cost += e["cost"]
        tot_rev += e["rev"]
        group_agg[g]["cost"] += e["cost"]
        group_agg[g]["rev"] += e["rev"]
        group_agg[g]["tokens"].update(e["tokens"])

    customers = []
    for c in customers_map.values():
        c["profit"] = c["revenue"] - c["cost"]
        c["margin"] = (c["profit"] / c["revenue"] * 100) if c["revenue"] else None
        c["groups"].sort(key=lambda x: -x["revenue"])
        customers.append(c)
    customers.sort(key=lambda x: -x["revenue"])

    groups = []
    for g in sorted(group_agg):
        gc = group_agg[g]
        prof = gc["rev"] - gc["cost"]
        groups.append({
            "group_name": g, "cost": gc["cost"], "rev": gc["rev"], "profit": prof,
            "margin": (prof / gc["rev"] * 100) if gc["rev"] else None,
            "token": ", ".join(sorted(t for t in gc["tokens"] if t)) or None,
        })

    tot_profit = tot_rev - tot_cost
    return {
        "has_data": True, "customers": customers, "groups": groups,
        "tot_cost": tot_cost, "tot_rev": tot_rev, "tot_profit": tot_profit,
        "tot_margin": (tot_profit / tot_rev * 100) if tot_rev else None,
    }


def debug_mapping(db: Session, customer_site: Site, d: date) -> dict:
    key_index, _ = _build_index(db, d)
    matched, unmatched = [], []
    for c in db.scalars(select(ChannelMeta).where(ChannelMeta.site_id == customer_site.id)).all():
        info = key_index.get(_norm(c.sk or ""))
        item = {"channel_id": c.channel_id, "name": c.name, "token": (info["token_name"] if info else None)}
        (matched if info else unmatched).append(item)
    return {"master_token_count": len(key_index), "matched_channels": matched, "unmatched_channels": unmatched}
