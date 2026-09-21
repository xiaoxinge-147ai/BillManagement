"""周/月报表：按时间区间聚合客户利润（逐日读本地已保存数据再汇总）。"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.customer_cost import compute_customer_profit
from app.models import Site


def customer_report(db: Session, start: date, end: date, site_ids=None) -> dict:
    sites = db.scalars(select(Site).where(Site.site_type == "customer")).all()
    if site_ids:
        wanted = set(site_ids)
        sites = [s for s in sites if s.id in wanted]

    agg = {}  # (site_id, customer_id) -> dict
    site_name = {s.id: s.name for s in sites}
    days = 0
    d = start
    while d <= end and days < 400:
        days += 1
        for s in sites:
            res = compute_customer_profit(db, s, d)
            if not res.get("has_data"):
                continue
            for c in res["customers"]:
                k = (s.id, c["customer_id"])
                e = agg.setdefault(k, {
                    "site_id": s.id, "site_name": site_name.get(s.id),
                    "customer_id": c["customer_id"], "username": c["username"],
                    "cost": 0.0, "revenue": 0.0,
                })
                e["cost"] += c["cost"]
                e["revenue"] += c["revenue"]
                if c.get("username"):
                    e["username"] = c["username"]
        d += timedelta(days=1)

    rows = []
    tot_cost = tot_rev = 0.0
    for e in agg.values():
        e["profit"] = e["revenue"] - e["cost"]
        e["margin"] = (e["profit"] / e["revenue"] * 100) if e["revenue"] else None
        rows.append(e)
        tot_cost += e["cost"]
        tot_rev += e["revenue"]
    rows.sort(key=lambda x: -x["revenue"])
    tot_profit = tot_rev - tot_cost
    return {
        "rows": rows, "tot_cost": tot_cost, "tot_rev": tot_rev, "tot_profit": tot_profit,
        "tot_margin": (tot_profit / tot_rev * 100) if tot_rev else None,
    }
