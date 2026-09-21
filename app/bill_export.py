"""客户账单导出：从客户站源库实时读取消费日志，拆分 token、重算金额、聚合成账单。

设计要点（依据 new-api 源码，详见 docs/账单导出模块需求规格.md）：
  1. 逐条读取 logs（keyset 分页 + 限速），**在 Python 里解析 other 并聚合**。
     不在 SQL 里做 JSON 提取/GROUP BY —— 跨 MySQL/PG 的 JSON 函数兼容性差，
     且 tool_surcharges 是数组、缓存创建要按语义分 5m/1h，可移植 SQL 表达不了。
  2. prompt_tokens 有两套语义：Anthropic 口径**不含**缓存，OpenAI/Gemini 口径**含**。
     判据是 other.usage_semantic == 'anthropic'（该键只在 Anthropic 语义时写入），
     并用 cache_creation_tokens_5m/_1h 是否存在兜住 legacy Claude 盲区。
  3. 倍率无历史版本（new-api 的 options 表就地覆盖），只能用每条日志里冻结的倍率。
     周期内倍率变过 → 「倍率指纹」不同 → 自动裂成多行。
  4. 金额双列：实际扣费(logs.quota，权威) + 按公式重算，并给出差异。
"""
from __future__ import annotations

import base64
import csv
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from datetime import time as dtime
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.appconfig import get_value, set_value
from app.config import settings
from app.models import Site
from app.source import (CHANNEL_TEST_TOKEN_NAME, LOG_TYPE_REFUND, build_engine,
                        day_range, iter_bill_logs, list_customers, logs_columns,
                        read_quota_per_unit)

logger = logging.getLogger(__name__)

# 导出任务的临时文件都放这里，启动时清残留
EXPORT_TMP_DIR = os.path.join(tempfile.gettempdir(), "billexp")
MAX_RUNNING_JOBS = 2          # 同时最多几个导出在跑（读的是线上库，别放开）
JOB_TTL_SECONDS = 6 * 3600    # 完成的任务保留多久


# ---------------------------------------------------------------- 列定义

# scope: all=三个 sheet 都有 / daily=按天与逐条明细才有 / detail=只有逐条明细
# kind:  text|int|ratio|money|pct|datetime
COLUMNS = [
    {"key": "stat_date",       "label": "日期",              "scope": "daily",  "kind": "text",     "w": 11},
    {"key": "ts",              "label": "时间",              "scope": "detail", "kind": "datetime", "w": 19},
    {"key": "site_name",       "label": "站点",              "scope": "all",    "kind": "text",     "w": 14},
    {"key": "customer_id",     "label": "用户ID",            "scope": "all",    "kind": "int",      "w": 9},
    {"key": "username",        "label": "用户名",            "scope": "all",    "kind": "text",     "w": 16},
    {"key": "token_id",        "label": "令牌ID",            "scope": "all",    "kind": "int",      "w": 9},
    {"key": "token_name",      "label": "令牌",              "scope": "all",    "kind": "text",     "w": 18},
    {"key": "group_name",      "label": "分组",              "scope": "all",    "kind": "text",     "w": 12},
    {"key": "model_name",      "label": "模型名",            "scope": "all",    "kind": "text",     "w": 26},
    {"key": "billing_kind",    "label": "计费类型",          "scope": "all",    "kind": "text",     "w": 10},
    {"key": "group_ratio",     "label": "分组倍率",          "scope": "all",    "kind": "ratio",    "w": 10},
    {"key": "calls",           "label": "调用次数",          "scope": "all",    "kind": "int",      "w": 10},
    {"key": "input_tokens",    "label": "输入token(不含缓存)", "scope": "all",  "kind": "int",      "w": 17},
    {"key": "cache_tokens",    "label": "缓存读取token",     "scope": "all",    "kind": "int",      "w": 14},
    {"key": "cache_create_plain", "label": "缓存创建token-普通", "scope": "all", "kind": "int",     "w": 18},
    {"key": "cache_create_5m", "label": "缓存创建token-5m",  "scope": "all",    "kind": "int",      "w": 17},
    {"key": "cache_create_1h", "label": "缓存创建token-1h",  "scope": "all",    "kind": "int",      "w": 17},
    {"key": "completion_tokens", "label": "输出token",       "scope": "all",    "kind": "int",      "w": 12},
    {"key": "image_tokens",    "label": "图片token",         "scope": "all",    "kind": "int",      "w": 11},
    {"key": "audio_tokens",    "label": "音频token",         "scope": "all",    "kind": "int",      "w": 11},
    {"key": "total_tokens",    "label": "token合计",         "scope": "all",    "kind": "int",      "w": 13},
    {"key": "amount_recalc",   "label": "消费金额",          "scope": "all",    "kind": "money",    "w": 14},
    {"key": "amount",          "label": "系统扣费额",        "scope": "all",    "kind": "money",    "w": 14},
    {"key": "amount_diff",     "label": "差异",              "scope": "all",    "kind": "money",    "w": 12},
    {"key": "amount_diff_pct", "label": "差异%",             "scope": "all",    "kind": "pct",      "w": 10},
    {"key": "request_id",      "label": "请求ID",            "scope": "detail", "kind": "text",     "w": 34},
    # 内部列：账单行 → 单价表行的对应键。不出现在导出结果里，只供公式定位单价。
    {"key": "price_key",       "label": "_单价键",           "scope": "all",    "kind": "text",
     "w": 1, "internal": True},
]

COLUMN_BY_KEY = {c["key"]: c for c in COLUMNS}

# 这些列若整份账单都是 0（例如这个客户从没用过图片/音频/缓存），就不输出该列 ——
# 勾选了也不显示，免得一堆恒为 0 的列干扰阅读。
OMIT_IF_ALL_ZERO = [
    "cache_tokens", "cache_create_plain", "cache_create_5m", "cache_create_1h",
    "image_tokens", "audio_tokens",
]
# 账单表里已不含各类倍率列（改为在「模型单价」sheet 里查），所以这里为空
RATIO_DEPENDS_ON: dict = {}
CFG_KEY = "export_columns"

# 稳定符号（BILLING_KIND_LABEL / CURRENCY / CURRENCY_DEFAULT / currency_symbol）
# 从 app.export_spec 取 —— 脚本也 import 它，别让脚本依赖本模块的内部实现。
from app.export_spec import BILLING_KIND_LABEL, CURRENCY, CURRENCY_DEFAULT  # noqa: E402
# 这些计费类型无法用倍率公式重算（详见规格 §1.6），重算金额直接采信实际扣费
NON_RECALCULABLE = {"task", "violation", "refund", "nodata"}
# 进「模型单价」sheet 的计费类型。分层表达式虽然无法用倍率反推单价，
# 但必须占一行 —— 否则账单里引用不到它，金额会算成 0。
PRICEABLE = {"usage", "per_call", "tiered", "audio"}


def load_columns(db: Session) -> list[dict]:
    """读取列配置。代码里维护列全集，DB 只存用户的覆盖项。

    新增列时老配置不会失效：配置里没有的 key 按默认顺序追加到末尾、默认显示。
    """
    raw = get_value(db, CFG_KEY, "")
    saved = []
    if raw:
        try:
            saved = json.loads(raw)
        except Exception:  # noqa: BLE001
            saved = []
    out, seen = [], set()
    for item in saved if isinstance(saved, list) else []:
        key = (item or {}).get("key")
        base = COLUMN_BY_KEY.get(key)
        if base is None or key in seen or base.get("internal"):
            continue  # 已废弃的列静默丢弃
        seen.add(key)
        out.append({**base,
                    "label": (item.get("label") or base["label"]).strip() or base["label"],
                    "visible": bool(item.get("visible", True))})
    for base in COLUMNS:  # 新增列补到末尾
        if base["key"] not in seen:
            out.append({**base, "visible": True})
    return out


def visible_columns(cols: list[dict]) -> list[dict]:
    """页面上给用户看的列（隐去内部列）。"""
    return [c for c in cols if not c.get("internal")]


def save_columns(db: Session, order: list[str], labels: dict, visible: set) -> None:
    data = [{"key": k, "label": labels.get(k, COLUMN_BY_KEY[k]["label"]), "visible": k in visible}
            for k in order if k in COLUMN_BY_KEY]
    set_value(db, CFG_KEY, json.dumps(data, ensure_ascii=False))


def reset_columns(db: Session) -> None:
    set_value(db, CFG_KEY, "")


def money_factor(qpu: float) -> float:
    """quota → 金额 的系数。

    币种只决定表格里的单位标注，**不做汇率换算** —— 站点额度本身按哪个币种设定，
    换算出来就是那个币种的金额。
    """
    return 1.0 / (qpu or 1)


def drop_zero_columns(cols: list[dict], rows: list) -> list[dict]:
    """把整列全 0 的 token 列、以及对应 token 全 0 的倍率列去掉。"""
    if not rows:
        return cols
    nonzero = {key for key in OMIT_IF_ALL_ZERO if any((r.get(key) or 0) for r in rows)}
    drop = {key for key in OMIT_IF_ALL_ZERO if key not in nonzero}
    for rk, deps in RATIO_DEPENDS_ON.items():
        if not any(d in nonzero for d in deps):
            drop.add(rk)
    return [c for c in cols if c["key"] not in drop]


from app.export_spec import sheet_columns  # noqa: E402  与脚本共用同一定义



_CUST_CACHE: dict = {}
_CUST_CACHE_TTL = 300      # 客户清单缓存 5 分钟，避免每次搜索都打源库


def customers_in_period(db: Session, site_id: int, sd: date, ed: date,
                        keyword: str = "", refresh: bool = False) -> tuple[list, bool]:
    """列出某个客户站的客户，供导出页搜索勾选。返回 (清单, 是否命中缓存)。

    单站点：账单本来就是一个平台一份，且各站 user_id 各自编号，混在一起选会撞车。
    keyword 在内存里过滤（缓存住全量清单，换关键词不必重复查库）。
    sd/ed 目前不参与查询（客户清单取自站点 users 表，与账期无关），保留参数是为了
    将来若要按账期过滤时不改调用方。

    读取失败会**抛出异常**，由调用方展示真实原因 —— 不能吞成空列表，
    否则「连不上/超时」会伪装成「该站没有客户」。
    """
    site = db.get(Site, site_id) if site_id else None
    if not site or site.site_type != "customer" or not site.dsn:
        return [], False

    ck = (site_id,)
    hit = False
    now = time.time()
    cached = _CUST_CACHE.get(ck)
    if cached and not refresh and now - cached[0] < _CUST_CACHE_TTL:
        rows, hit = cached[1], True
    else:
        engine = build_engine(site.dialect, site.dsn)
        try:
            rows = list_customers(engine, site.dialect)
        finally:
            engine.dispose()
        rows = [{**c, "site_id": site.id, "site_name": site.name} for c in rows]
        if rows:                       # 只缓存有内容的结果，失败/空不缓存
            if len(_CUST_CACHE) > 40:
                _CUST_CACHE.clear()
            _CUST_CACHE[ck] = (now, rows)

    kw = (keyword or "").strip().lower()
    if kw:
        rows = [c for c in rows
                if kw in (c["username"] or "").lower()
                or kw in (c.get("display_name") or "").lower()
                or kw == str(c["user_id"])]
    return rows, hit


# ---------------------------------------------------------------- 周期

def resolve_period(preset: str, start: str, end: str) -> tuple[date, date, str]:
    """返回 (起, 止, 标题)。止为闭区间（含当天）。"""
    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).date()
    if preset == "this_month":
        s = today.replace(day=1)
        return s, today, f"{s:%Y-%m} 本月"
    if preset == "last_month":
        first = today.replace(day=1)
        e = first - timedelta(days=1)
        s = e.replace(day=1)
        return s, e, f"{s:%Y-%m} 月账单"
    if preset == "this_week":
        s = today - timedelta(days=today.weekday())
        return s, today, f"{s:%Y-%m-%d} 本周"
    if preset == "last_week":
        s = today - timedelta(days=today.weekday() + 7)
        e = s + timedelta(days=6)
        return s, e, f"{s:%Y-%m-%d} 周账单"
    def _parse(v, fallback):
        try:
            return date.fromisoformat((v or "").strip())
        except (ValueError, AttributeError):
            return fallback   # 空串/脏 cookie/手改 URL 都不该 500

    ed = _parse(end, today)
    sd = _parse(start, ed - timedelta(days=6))
    if ed < sd:
        sd, ed = ed, sd
    return sd, ed, f"{sd:%Y-%m-%d} ~ {ed:%Y-%m-%d}"


# ---------------------------------------------------------------- other 解析

def parse_other(raw) -> tuple[dict, bool]:
    """把 logs.other 解析成 (dict, 是否正常)。

    ''/'null' 是 nil map 的正常序列化结果，'{}' 也是合法空对象 —— 都不算解析失败；
    只有真的解析不出来（非法 JSON、或解出来不是对象）才记失败。
    """
    if raw is None:
        return {}, True
    if isinstance(raw, dict):
        return raw, True
    s = str(raw).strip()
    if not s or s == "null":
        return {}, True
    try:
        v = json.loads(s)
    except Exception:  # noqa: BLE001
        return {}, False
    return (v, True) if isinstance(v, dict) else ({}, False)


def _f(o: dict, key: str, default: float = 0.0) -> float:
    v = o.get(key)
    if v is None or isinstance(v, bool):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(o: dict, key: str) -> int:
    v = o.get(key)
    if v is None or isinstance(v, bool):
        return 0
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _tier_prices(expr: str, tier: str) -> dict:
    """从分层表达式里抽出指定档位的各类 token 单价（单位：金额 / 1M tokens）。

    表达式形如 `v1:len <= 200000 ? tier("<=200k", p*3 + c*15) : tier(">200k", p*6 + c*22.5)`
    —— `tier(档位名, 该档成本)` 是 new-api 的标记函数（pkg/billingexpr），运行时它把
    档位名记进 other.matched_tier。这里在对应 tier(...) 的第二个实参里找 `p*系数`、
    `c*系数` 等，得到该档单价。解析不出来返回 {}，调用方退回按 quota 反推。
    """
    if not expr or not tier:
        return {}
    for quote in ('"', "'"):
        marker = f"tier({quote}{tier}{quote}"
        i = expr.find(marker)
        if i >= 0:
            break
    else:
        return {}
    j = expr.find(",", i + len(marker) - 1)
    if j < 0:
        return {}
    depth, k = 1, j + 1              # tier( 的左括号已算一层
    while k < len(expr) and depth:
        if expr[k] == "(":
            depth += 1
        elif expr[k] == ")":
            depth -= 1
        k += 1
    body = expr[j + 1:k - 1]
    var_map = {"p": "input", "c": "output", "cr": "cache_read",
               "cc1h": "cache_1h", "cc": "cache_5m", "img": "image"}
    out = {}
    for var, key in var_map.items():
        m = (re.search(r"\b" + var + r"\s*\*\s*([0-9]*\.?[0-9]+)", body)
             or re.search(r"([0-9]*\.?[0-9]+)\s*\*\s*\b" + var + r"\b", body))
        if m:
            try:
                out[key] = float(m.group(1))
            except ValueError:
                pass
    return out


def tier_info(o: dict) -> dict:
    """分层计费的档位信息：档位名 + 该档各类 token 单价。"""
    tier = str(o.get("matched_tier") or "")
    expr = ""
    raw = o.get("expr_b64")
    if raw:
        try:
            expr = base64.b64decode(str(raw)).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            expr = ""
    return {"tier": tier, "expr": expr, "prices": _tier_prices(expr, tier)}


def is_anthropic_semantic(o: dict) -> bool:
    """Anthropic 口径判定。

    usage_semantic 只在 Anthropic 语义时写入 'anthropic'，其他语义该键不存在。
    另有一类 OpenAI 形状但按 Anthropic 口径计费的日志（isLegacyClaudeDerivedOpenAIUsage）
    没有任何标记，用 5m/1h 键是否存在兜住 —— 那两个键只有 Claude 缓存才会写。
    """
    if o.get("usage_semantic") == "anthropic":
        return True
    return "cache_creation_tokens_5m" in o or "cache_creation_tokens_1h" in o


def classify(o: dict, token_name: str, token_id: int, log_type: int) -> str:
    if log_type == LOG_TYPE_REFUND:
        return "refund"
    if token_name == CHANNEL_TEST_TOKEN_NAME and not token_id:
        return "test"
    if o.get("violation_fee"):
        return "violation"
    # 任务链路有三种写日志姿势：LogTaskConsumption 写 is_task；RefundTaskQuota 与
    # RecalculateTaskQuota（差额结算，也是 type=2）只写 task_id 不写 is_task。
    if o.get("is_task") or "task_id" in o:
        return "task"
    if o.get("billing_mode") == "tiered_expr":
        return "tiered"
    # 音频 / Realtime(ws) 走的是 calculateAudioQuota，公式与文本链路完全不同
    if o.get("audio") or o.get("ws"):
        return "audio"
    mp = o.get("model_price")
    if mp is not None and _f(o, "model_price", -1.0) != -1.0:
        return "per_call"
    if "model_ratio" not in o:
        # 正常的按量日志一定有 model_ratio（GenerateTextOtherInfo 无条件写入），
        # 没有就说明 other 为空/损坏/是别的写日志路径 —— 标出来，不要假装能重算。
        return "nodata"
    return "usage"


def split_tokens(o: dict, prompt_tokens: int, completion_tokens: int, kind: str = "usage") -> dict:
    """把一条日志的 token 拆成账单需要的几类。"""
    anthropic = is_anthropic_semantic(o)

    if kind == "audio":
        # 音频/Realtime：other 里直接给了四分量，落库的 prompt/completion 本身含音频，
        # 所以这里改用 text_input/text_output，音频量单独进「音频token」列。
        return {
            "anthropic": anthropic, "audio_mode": True,
            "input_tokens": _i(o, "text_input"),
            "completion_tokens": _i(o, "text_output"),
            "audio_in": _i(o, "audio_input"), "audio_out": _i(o, "audio_output"),
            "audio_tokens": _i(o, "audio_input") + _i(o, "audio_output"),
            "cache_tokens": 0, "cache_create_plain": 0, "cache_create_5m": 0,
            "cache_create_1h": 0, "cache_create_total": 0, "image_tokens": 0,
            "prompt_raw": prompt_tokens, "completion_raw": completion_tokens,
        }

    cache_read = _i(o, "cache_tokens")
    cc_total = _i(o, "cache_creation_tokens")
    c5m = _i(o, "cache_creation_tokens_5m")
    c1h = _i(o, "cache_creation_tokens_1h")
    image = _i(o, "image_output")          # 键名叫 output，实际是「输入」图像 token
    audio = _i(o, "audio_input_token_count")

    base = prompt_tokens
    if not anthropic:
        # OpenAI/Gemini 口径的 prompt_tokens 含缓存，要减掉
        base -= cache_read + cc_total
    base -= image + audio                   # 图像 token 两种语义下都含在 prompt_tokens 里
    if base < 0:
        base = 0                            # new-api 自己也有这个钳位

    return {
        "anthropic": anthropic, "audio_mode": False,
        "input_tokens": base,
        "cache_tokens": cache_read,
        "cache_create_plain": max(cc_total - c5m - c1h, 0),
        "cache_create_5m": c5m,
        "cache_create_1h": c1h,
        "cache_create_total": cc_total,
        "completion_tokens": completion_tokens,
        "image_tokens": image,
        "audio_tokens": audio,
        "audio_in": 0, "audio_out": 0,
        # new-api 的 hasBillableUsage 只看原始 prompt+completion，单独留着对齐用
        "prompt_raw": prompt_tokens, "completion_raw": completion_tokens,
    }


def _d(x) -> Decimal:
    """float → Decimal，走 str 以取得「人看到的」十进制值（对齐 Go shopspring decimal）。"""
    return Decimal(str(x))


_INT32_MAX, _INT32_MIN = 2 ** 31 - 1, -(2 ** 31)


def _round_away(x: Decimal) -> int:
    """四舍五入 half-away-from-zero 并钳到 int32，对齐 QuotaFromDecimalChecked。

    全程在 Decimal 上做 —— 先转 float 会把精确十进制截断到 double，小数部分落在 .5
    的半个 ulp 内时会比 Go 多进 1。
    """
    v = int(x.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return max(_INT32_MIN, min(_INT32_MAX, v))


def recalc_quota(o: dict, tk: dict, kind: str, qpu: float) -> Optional[int]:
    """按 new-api 的计费公式重算 quota。无法重算的返回 None。"""
    if kind in NON_RECALCULABLE or kind == "test":
        return None

    gr = _d(_f(o, "group_ratio", 1.0))
    mr = _d(_f(o, "model_ratio"))
    ratio = mr * gr
    dq = _d(qpu)

    surcharge = Decimal(0)
    for item in (o.get("tool_surcharges") or []):
        if not isinstance(item, dict):
            continue
        # price 单位是 $/1K 次，不乘 model_ratio
        surcharge += (_d(_f(item, "price")) * _d(_i(item, "count")) / _d(1000)) * gr * dq

    if kind == "tiered":
        prices = (o.get("_tier") or {}).get("prices") or {}
        if not prices:
            return None                      # 解析不出档位单价，退回采信系统扣费额
        cost = (_d(tk["input_tokens"]) * _d(prices.get("input", 0))
                + _d(tk["completion_tokens"]) * _d(prices.get("output", 0))
                + _d(tk["cache_tokens"]) * _d(prices.get("cache_read", 0))
                + _d(tk["cache_create_plain"] + tk["cache_create_5m"]) * _d(prices.get("cache_5m", 0))
                + _d(tk["cache_create_1h"]) * _d(prices.get("cache_1h", 0))
                + _d(tk["image_tokens"]) * _d(prices.get("image", 0)))
        q = cost / _d(1000000) * dq * gr
        out = _round_away(q)
        if (tk["prompt_raw"] + tk["completion_raw"]) <= 0:
            return 0
        if q > 0 and out == 0:
            out = 1
        return out

    if kind == "audio":
        # service/quota.go: calculateAudioQuota
        ar = _d(_f(o, "audio_ratio"))
        acr = _d(_f(o, "audio_completion_ratio"))
        mp = _f(o, "model_price", -1.0)
        if mp != -1.0:
            q = _d(mp) * dq * gr
        else:
            q = (_d(tk["input_tokens"])
                 + _d(tk["completion_tokens"]) * _d(_f(o, "completion_ratio"))
                 + _d(tk["audio_in"]) * ar
                 + _d(tk["audio_out"]) * ar * acr) * ratio
            if ratio != 0 and q <= 0:
                q = Decimal(1)
    elif kind == "per_call":
        q = _d(_f(o, "model_price")) * dq * gr + surcharge
    else:
        ccr = _d(_f(o, "cache_creation_ratio"))
        if tk["anthropic"]:
            ccr5 = _d(_f(o, "cache_creation_ratio_5m", _f(o, "cache_creation_ratio")))
            # 1h 倍率恒为 5m 的 1.6 倍（new-api 硬编码 6/3.75，不可单独配置）
            ccr1 = _d(_f(o, "cache_creation_ratio_1h", _f(o, "cache_creation_ratio") * 1.6))
            cc_weighted = (_d(tk["cache_create_plain"]) * ccr
                           + _d(tk["cache_create_5m"]) * ccr5
                           + _d(tk["cache_create_1h"]) * ccr1)
        else:
            cc_weighted = _d(tk["cache_create_total"]) * ccr

        prompt_q = (_d(tk["input_tokens"])
                    + _d(tk["cache_tokens"]) * _d(_f(o, "cache_ratio"))
                    + _d(tk["image_tokens"]) * _d(_f(o, "image_ratio"))
                    + cc_weighted)
        completion_q = _d(tk["completion_tokens"]) * _d(_f(o, "completion_ratio"))
        q = (prompt_q + completion_q) * ratio

        audio_price = _f(o, "audio_input_price")
        if audio_price and tk["audio_tokens"]:
            # Gemini 音频输入独立计价，不乘 model_ratio
            q += _d(audio_price) / _d(1000000) * _d(tk["audio_tokens"]) * gr * dq

        # 注意：new-api 还会乘一个 Π(otherRatios)（图像张数 n 等），但它不落库，
        # 所以多张图生成的请求重算会偏低 —— 靠差异列暴露。
        q += surcharge
        if ratio != 0 and q <= 0:
            q = Decimal(1)                   # 最小计费 1 quota 地板

    out = _round_away(q)
    # text_quota.go:378 的归零判定在按量/按次两个分支之外，对两者都生效；
    # 且 TotalTokens 只等于「原始 prompt + completion」，不含 cache/image/audio。
    if (tk["prompt_raw"] + tk["completion_raw"]) <= 0 and surcharge == 0:
        return 0
    if ratio != 0 and out == 0:
        out = 1
    return out


# 指纹只用 GenerateTextOtherInfo **无条件写入** 的倍率键。
# cache_creation_ratio / _5m / _1h / image_ratio 是「按需写入」的（对应 token 为 0 时整个键不存在），
# 把它们放进指纹会让「这次没写缓存」和「这次写了缓存」拿到不同指纹，同一价格平白裂成多行。
FP_FIELDS = ["model_ratio", "group_ratio", "completion_ratio", "cache_ratio", "model_price"]
FP_FIELDS_AUDIO = FP_FIELDS + ["audio_ratio", "audio_completion_ratio"]
# 展示用（可能缺键），随桶取第一个非零值
DISPLAY_RATIOS = ["model_ratio", "group_ratio", "completion_ratio", "cache_ratio",
                  "cache_creation_ratio", "image_ratio", "model_price"]
# 只给「模型单价」sheet 用，不进账单列
_PRICE_EXTRA = ["audio_ratio", "audio_completion_ratio"]


def ratio_fingerprint(o: dict, kind: str) -> tuple[str, dict]:
    """倍率指纹：周期内任一计费倍率变过就裂成不同的行。

    只盯分组倍率是不够的 —— 模型倍率/输出倍率/缓存倍率变了，展示出来的倍率就会和
    桶里的实际用量对不上。反过来也要小心：按需写入的倍率键不能进指纹，否则会误拆。
    """
    fields = FP_FIELDS_AUDIO if kind == "audio" else FP_FIELDS
    parts = [f"{k}={round(_f(o, k), 10)}" for k in fields]
    parts.append("sem=anthropic" if is_anthropic_semantic(o) else "sem=")
    parts.append("kind=" + kind)
    if kind == "tiered":
        # 命中的阶梯不同 → 单价不同 → 必须分行统计
        ti = o.get("_tier") or {}
        parts.append("tier=" + str(ti.get("tier") or ""))
    key = hashlib.md5("|".join(parts).encode()).hexdigest()[:12]
    display = {k: round(_f(o, k), 10) for k in DISPLAY_RATIOS + _PRICE_EXTRA}
    if display.get("model_price", 0) < 0:
        display["model_price"] = 0.0   # 按量计费时 new-api 写的是 -1，别让它出现在表里
    if kind == "tiered":
        ti = o.get("_tier") or {}
        display["_tier_name"] = ti.get("tier") or ""
        display["_tier_prices"] = ti.get("prices") or {}
    return key, display


# ---------------------------------------------------------------- 聚合

def _safe_name(v) -> str:
    """文件名去掉非法字符。"""
    return re.sub(r'[\\/:*?"<>|]', "_", str(v or "")).strip() or "未命名"


def _pk_str(pkey: tuple) -> str:
    """单价表的元组 key → 账单行里的 price_key 字符串。"""
    from app.export_xlsx import price_key
    return price_key(*pkey)


def writer_for_customer(site_id: int, customer_id: int):
    """取该客户绑定的导出脚本（后台线程里跑，用独立 Session）。"""
    from app.db import SessionLocal
    from app.export_scripts import writer_for
    db = SessionLocal()
    try:
        return writer_for(db, site_id, customer_id)
    finally:
        db.close()


def merge_ratios(cur: dict, new: dict) -> None:
    """按需写入的倍率键缺失时值为 0，用第一个非零值填空，供展示。"""
    for k, v in new.items():
        if v and not cur.get(k):
            cur[k] = v


_COUNTER_FIELDS = ("calls", "input_tokens", "cache_tokens", "cache_create_plain",
                   "cache_create_5m", "cache_create_1h", "completion_tokens",
                   "image_tokens", "audio_tokens", "quota", "quota_recalc")


class _Bucket:
    __slots__ = ("calls", "input_tokens", "cache_tokens", "cache_create_plain",
                 "cache_create_5m", "cache_create_1h", "completion_tokens",
                 "image_tokens", "audio_tokens", "quota", "quota_recalc", "meta")

    def __init__(self, meta: dict):
        self.calls = 0
        self.input_tokens = self.cache_tokens = 0
        self.cache_create_plain = self.cache_create_5m = self.cache_create_1h = 0
        self.completion_tokens = self.image_tokens = self.audio_tokens = 0
        self.quota = 0
        self.quota_recalc = 0
        self.meta = meta

    def add(self, tk: dict, quota: int, recalc: Optional[int], counts_as_call: bool) -> None:
        if counts_as_call:
            self.calls += 1
        self.input_tokens += tk["input_tokens"]
        self.cache_tokens += tk["cache_tokens"]
        self.cache_create_plain += tk["cache_create_plain"]
        self.cache_create_5m += tk["cache_create_5m"]
        self.cache_create_1h += tk["cache_create_1h"]
        self.completion_tokens += tk["completion_tokens"]
        self.image_tokens += tk["image_tokens"]
        self.audio_tokens += tk["audio_tokens"]
        self.quota += quota
        # 不可重算的采信实际扣费，保证「重算金额」列总额有意义
        self.quota_recalc += quota if recalc is None else recalc

    def note_ratios(self, ratios: dict) -> None:
        """按需写入的倍率（如 cache_creation_ratio）可能整桶只有部分日志带，取第一个非零值展示。"""
        merge_ratios(self.meta["ratios"], ratios)

    def merge(self, other: "_Bucket") -> None:
        """把另一个桶并进来（按天 → 汇总）。新增计数字段时不必再改这里。"""
        for f in _COUNTER_FIELDS:
            setattr(self, f, getattr(self, f) + getattr(other, f))
        self.note_ratios(other.meta["ratios"])


def _row_from_bucket(b: _Bucket, stat_date: Optional[str]) -> dict:
    m = b.meta
    mf = m["mf"]
    amount = b.quota * mf
    amount_recalc = b.quota_recalc * mf
    diff = amount_recalc - amount     # 以「消费金额(重算)」为主口径，差异 = 重算 − 系统扣费
    total_tokens = (b.input_tokens + b.cache_tokens + b.cache_create_plain
                    + b.cache_create_5m + b.cache_create_1h + b.completion_tokens
                    + b.image_tokens + b.audio_tokens)
    row = {
        "stat_date": stat_date or "",
        "site_name": m["site_name"],
        "customer_id": m["customer_id"], "username": m["username"],
        "token_id": m["token_id"], "token_name": m["token_name"],
        "group_name": m["group_name"], "model_name": m["model_name"],
        "billing_kind": BILLING_KIND_LABEL.get(m["billing_kind"], m["billing_kind"]),
        "price_key": m.get("price_key", ""),
        "calls": b.calls,
        "input_tokens": b.input_tokens, "cache_tokens": b.cache_tokens,
        "cache_create_plain": b.cache_create_plain,
        "cache_create_5m": b.cache_create_5m, "cache_create_1h": b.cache_create_1h,
        "completion_tokens": b.completion_tokens, "image_tokens": b.image_tokens,
        "audio_tokens": b.audio_tokens, "total_tokens": total_tokens,
        "amount": amount, "amount_recalc": amount_recalc, "amount_diff": diff,
        "amount_diff_pct": (diff / amount_recalc * 100) if amount_recalc else None,
        "ts": "", "request_id": "",
    }
    for k in DISPLAY_RATIOS:
        row[k] = m["ratios"].get(k)
    return row


def _sort_key(r: dict):
    return (r.get("stat_date") or "", str(r["site_name"]), str(r["username"] or ""),
            str(r["token_name"] or ""), str(r["group_name"] or ""), -r["amount"])


# ---------------------------------------------------------------- 后台任务

_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()
MAX_JOBS_KEPT = 20


class _Cancelled(Exception):
    """用户主动取消，不是故障。"""


def get_job(job_id: str) -> Optional[dict]:
    return _JOBS.get(job_id)


def cleanup_stale_temp() -> None:
    """启动时清掉上次进程留下的临时文件（容器重启/崩溃后不会一直堆着）。"""
    try:
        os.makedirs(EXPORT_TMP_DIR, exist_ok=True)
        for name in os.listdir(EXPORT_TMP_DIR):
            try:
                os.unlink(os.path.join(EXPORT_TMP_DIR, name))
            except OSError:
                pass
    except OSError:
        pass


def running_count() -> int:
    return sum(1 for j in _JOBS.values() if j["status"] == "running")


def _new_job(title: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    with _JOBS_LOCK:
        # 过期与超量的已完成任务一并清掉，连同它们的 xlsx（跑着的绝不动）
        stale = [k for k, j in _JOBS.items()
                 if j["status"] != "running" and now - (j.get("finished") or j["created"]) > JOB_TTL_SECONDS]
        done = [k for k in _JOBS if _JOBS[k]["status"] != "running" and k not in stale]
        over = sorted(done, key=lambda k: _JOBS[k]["created"])[:max(0, len(_JOBS) - MAX_JOBS_KEPT + 1)]
        for old in stale + over:
            _cleanup(_JOBS.pop(old, None))
        _JOBS[job_id] = {
            "id": job_id, "title": title, "status": "running", "phase": "准备中",
            "days_done": 0, "days_total": 0, "rows_read": 0, "cur_date": "",
            "file": None, "filename": None, "size": 0, "error": None,
            "created": time.time(), "finished": None, "stats": {},
        }
    return job_id


def cancel_job(job_id: str) -> bool:
    """请求取消一个正在跑的导出任务（worker 每批检查一次）。"""
    j = _JOBS.get(job_id)
    if j and j.get("status") == "running":
        j["cancel"] = True
        return True
    return False


def _mask_secrets(msg: str, plan: list) -> str:
    """异常文本里可能整串回显 DSN，把出现过的连接串换成脱敏形式。"""
    from app.crypto import mask_dsn
    for site in plan or []:
        dsn = site.get("dsn")
        if dsn:
            msg = msg.replace(dsn, mask_dsn(dsn))
    return msg[:800]


def _cleanup(job: Optional[dict]) -> None:
    if job and job.get("file"):
        try:
            os.unlink(job["file"])
        except OSError:
            pass


def start_export(db: Session, site_id: int, preset: str, start: str, end: str,
                 with_daily: bool, with_detail: bool,
                 batch: int = 20000, sleep_ms: int = 0,
                 customer_ids: Optional[list] = None,
                 allowed_sites: Optional[set] = None,
                 currency: str = "") -> str:
    """启动后台导出任务，返回 job_id。一次只导一个客户站（账单本来就是一站一份）。"""
    sd, ed, title = resolve_period(preset, start, end)
    cols = load_columns(db)
    site = db.get(Site, site_id) if site_id else None
    if site and allowed_sites is not None and site.id not in allowed_sites:
        site = None                       # 越权：前端可能被绕过，后端也要挡
    plan = ([{"id": site.id, "name": site.name, "dialect": site.dialect, "dsn": site.dsn}]
            if site and site.site_type == "customer" and site.dsn else [])

    if running_count() >= MAX_RUNNING_JOBS:
        job_id = _new_job(title)
        _JOBS[job_id].update(
            status="error",
            error=f"已有 {MAX_RUNNING_JOBS} 个导出任务在跑（读的是线上库，不宜并发太多），请等它们完成后再试。",
        )
        return job_id

    job_id = _new_job(title)
    job = _JOBS[job_id]
    job["days_total"] = ((ed - sd).days + 1) * max(len(plan), 1)
    job["period"] = (sd.isoformat(), ed.isoformat())
    job["site_names"] = [p["name"] for p in plan]
    job["customer_ids"] = list(customer_ids or [])
    job["currency"] = str(currency) if str(currency) in CURRENCY else CURRENCY_DEFAULT
    if not plan:
        job["status"] = "error"
        job["error"] = "请先选择一个客户站（且该站点要配置好只读 DSN）"
        return job_id

    threading.Thread(
        target=_worker,
        args=(job_id, plan, sd, ed, title, cols, with_daily, with_detail, batch, sleep_ms,
              list(customer_ids or []), job["currency"]),
        daemon=True,
    ).start()
    return job_id


def _detail_row(site_name, o, tk, ratios, kind, log, quota, recalc, mf, tz, pkey_str=""):
    ts = ""
    try:
        ts = datetime.fromtimestamp(int(log.get("created_at") or 0), tz).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        pass
    amount = quota * mf
    amount_recalc = (quota if recalc is None else recalc) * mf
    diff = amount_recalc - amount
    total_tokens = (tk["input_tokens"] + tk["cache_tokens"] + tk["cache_create_plain"]
                    + tk["cache_create_5m"] + tk["cache_create_1h"] + tk["completion_tokens"]
                    + tk["image_tokens"] + tk["audio_tokens"])
    row = {
        "stat_date": ts[:10], "ts": ts, "site_name": site_name,
        "customer_id": log.get("user_id") or 0, "username": log.get("username") or "",
        "token_id": log.get("token_id") or 0, "token_name": log.get("token_name") or "",
        "group_name": log.get("group_name") or "", "model_name": log.get("model_name") or "",
        "billing_kind": BILLING_KIND_LABEL.get(kind, kind),
        "price_key": pkey_str,
        "calls": 0 if kind == "refund" else 1,
        "input_tokens": tk["input_tokens"], "cache_tokens": tk["cache_tokens"],
        "cache_create_plain": tk["cache_create_plain"],
        "cache_create_5m": tk["cache_create_5m"], "cache_create_1h": tk["cache_create_1h"],
        "completion_tokens": tk["completion_tokens"], "image_tokens": tk["image_tokens"],
        "audio_tokens": tk["audio_tokens"], "total_tokens": total_tokens,
        "amount": amount, "amount_recalc": amount_recalc, "amount_diff": diff,
        "amount_diff_pct": (diff / amount_recalc * 100) if amount_recalc else None,
        "request_id": log.get("request_id") or "",
    }
    for k in DISPLAY_RATIOS:
        row[k] = ratios.get(k)
    return row


def _worker(job_id, plan, sd, ed, title, cols, with_daily, with_detail, batch, sleep_ms,
            customer_ids=None, currency=""):
    job = _JOBS[job_id]
    default_qpu = settings.quota_per_unit or 1
    tz = ZoneInfo(settings.timezone)
    t0 = time.time()

    daily: dict = {}
    pricing: dict = {}
    kind_stats: dict = defaultdict(lambda: {"calls": 0, "amount": 0.0})
    skipped_test = 0
    parse_failed = 0
    subscription_rows = 0
    site_qpu: dict = {}
    _day_cache: dict = {}          # 本地日序号 → 'YYYY-MM-DD'，避免每行都做时区转换
    # 本地时区相对 UTC 的偏移秒数，用于把时间戳对齐到「本地日」而不是 UTC 日。
    # 中国无夏令时，固定 +8；其它时区按账期起始日取偏移（跨夏令时切换时最多影响边界一天）。
    _tz_off = int(datetime.combine(sd, dtime.min, tzinfo=tz).utcoffset().total_seconds())

    detail_cols = sheet_columns(cols, "detail") if with_detail else []
    cust_spool: dict = {}          # customer_id -> 临时 CSV 路径
    _spool_buf: dict = {}          # customer_id -> 待落盘的行
    detail_rows = 0

    def _spool(cust_id, row_vals, force=False):
        """按客户缓冲明细行，攒够再落盘 —— 客户多时不会同时占一堆文件句柄。"""
        buf = _spool_buf.setdefault(cust_id, [])
        if row_vals is not None:
            buf.append(row_vals)
        if buf and (force or len(buf) >= 5000):
            path = cust_spool.get(cust_id)
            if path is None:
                fd, path = tempfile.mkstemp(prefix=f"spool_{cust_id}_", suffix=".csv",
                                            dir=EXPORT_TMP_DIR)
                os.close(fd)
                cust_spool[cust_id] = path
            with open(path, "a", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerows(buf)
            buf.clear()
    try:
        if with_detail:
            os.makedirs(EXPORT_TMP_DIR, exist_ok=True)

        for site in plan:
            engine = build_engine(site["dialect"], site["dsn"])
            try:
                cols_present = logs_columns(engine)
                # 额度→金额的换算比例每个站点各自可配，必须回源库读，不能用本系统的全局值
                qpu = read_quota_per_unit(engine, site["dialect"], default_qpu)
                site_qpu[site["name"]] = qpu
                mf = money_factor(qpu)
                job["phase"] = "读取源库"
                period_start, _ = day_range(sd)
                _, period_end = day_range(ed)
                def _on_retry(new_batch, exc):
                    """源库查询超时 → 缩小批次重试，把过程写进进度里让人看得见。"""
                    job["retries"] = job.get("retries", 0) + 1
                    job["phase"] = f"读取源库（查询超时，批次降到 {new_batch} 重试）"
                    logger.warning("导出读取超时，批次降到 %d：%s", new_batch, exc)

                for rows in iter_bill_logs(
                        engine, site["dialect"], cols_present, period_start, period_end,
                        batch=batch, sleep_ms=sleep_ms, user_ids=customer_ids,
                        on_retry=_on_retry):
                    if True:
                        # 取消必须抛异常终止：静默 break 会让 worker 以为读完了，
                        # 用户点了取消却拿到一份缺数据的「完整」账单。
                        if job.get("cancel"):
                            raise _Cancelled()
                        for log in rows:
                            o, ok = parse_other(log.get("other"))
                            if not ok:
                                parse_failed += 1
                            log_type = int(log.get("type") or 2)
                            token_name = log.get("token_name") or ""
                            token_id = int(log.get("token_id") or 0)
                            kind = classify(o, token_name, token_id, log_type)
                            if kind == "tiered":
                                o["_tier"] = tier_info(o)
                            if kind == "test":
                                skipped_test += 1
                                continue

                            prompt_t = int(log.get("prompt_tokens") or 0)
                            comp_t = int(log.get("completion_tokens") or 0)
                            quota = int(log.get("quota") or 0)
                            if kind == "refund":
                                quota = -quota           # 退款：quota 是正数但含义是退回
                                prompt_t = comp_t = 0
                                o = {}                   # 只冲销金额，不参与任何 token 统计
                            tk = split_tokens(o, prompt_t, comp_t, kind)
                            recalc = recalc_quota(o, tk, kind, qpu)
                            rkey, ratios = ratio_fingerprint(o, kind)
                            if o.get("billing_source") == "subscription":
                                subscription_rows += 1

                            st = kind_stats[kind]
                            st["calls"] += 1
                            st["amount"] += quota * mf   # 用本站的换算系数

                            pkey_str = ""
                            if kind in PRICEABLE:
                                from app.export_xlsx import price_key as _pk
                                # key 带上站点：各站 QuotaPerUnit 不同，单价基准也不同
                                pkey = (site["name"], log.get("model_name") or "",
                                        log.get("group_name") or "", rkey)
                                pkey_str = _pk(*pkey)
                                pr = pricing.get(pkey)
                                if pr is None:
                                    pricing[pkey] = {"ratios": dict(ratios), "kind": kind,
                                                     "qpu": qpu, "site": site["name"]}
                                else:
                                    # 按需写入的倍率（缓存创建/图片）只有部分日志带，逐步补齐
                                    merge_ratios(pr["ratios"], ratios)

                            ts_i = int(log.get("created_at") or 0)
                            # 按「本地日」分桶：ts//86400 是 UTC 整天，与 Asia/Shanghai
                            # 的日界差 8 小时，会把 00:00-07:59 的日志算到前一天。
                            day_s = _day_cache.get((ts_i + _tz_off) // 86400)
                            if day_s is None:
                                day_s = datetime.fromtimestamp(ts_i, tz).date().isoformat()
                                _day_cache[(ts_i + _tz_off) // 86400] = day_s
                            dkey = (day_s, site["id"], int(log.get("user_id") or 0),
                                    token_id, log.get("group_name") or "",
                                    log.get("model_name") or "", rkey)
                            b = daily.get(dkey)
                            if b is None:
                                b = daily[dkey] = _Bucket({
                                    "site_name": site["name"],
                                    "customer_id": int(log.get("user_id") or 0),
                                    "username": log.get("username") or "",
                                    "token_id": token_id, "token_name": token_name,
                                    "group_name": log.get("group_name") or "",
                                    "model_name": log.get("model_name") or "",
                                    "billing_kind": kind, "ratios": dict(ratios),
                                    "qpu": qpu, "mf": mf, "price_key": pkey_str,
                                })
                            b.note_ratios(ratios)
                            b.add(tk, quota, recalc, counts_as_call=(kind != "refund"))

                            if with_detail:
                                dr = _detail_row(site["name"], o, tk, ratios, kind, log,
                                                 quota, recalc, mf, tz, pkey_str)
                                _spool(int(log.get("user_id") or 0),
                                       [dr.get(c["key"]) for c in detail_cols])
                                detail_rows += 1
                            job["rows_read"] += 1
                        job["cur_date"] = f'{site["name"]} {day_s}'
                        job["days_done"] = min((date.fromisoformat(day_s) - sd).days + 1,
                                               job["days_total"] or 1)
            finally:
                engine.dispose()   # 异常路径也要归还连接

        for _cid in list(_spool_buf):        # 收尾：把没攒满的也落盘
            _spool(_cid, None, force=True)

        job["phase"] = "生成表格"
        job["cur_date"] = ""

        # ---- 按客户切分：每个客户一份账单（各自可能用不同的导出脚本）----
        by_cust: dict = defaultdict(dict)        # customer_id -> {daily_key: bucket}
        while daily:
            k, b = daily.popitem()               # k = (日期, 站点, 客户, 令牌, 分组, 模型, 指纹)
            by_cust[k[2]][k] = b                 # 边搬边清，不让两份索引同时占内存

        base_stats = {
            "period": f"{sd:%Y-%m-%d} ~ {ed:%Y-%m-%d}",
            "sites": "、".join(p["name"] for p in plan),
            "rows_read": job["rows_read"],
            "skipped_test": skipped_test,
            "parse_failed": parse_failed,
            "subscription_rows": subscription_rows,
            "site_qpu": site_qpu,
            "customer_filter": list(customer_ids or []),
            "detail_rows": detail_rows,
            "kinds": {BILLING_KIND_LABEL.get(k, k): dict(v)
                      for k, v in sorted(kind_stats.items())},
            "elapsed": round(time.time() - t0, 1),
            "currency": job.get("currency") or CURRENCY_DEFAULT,
        }
        job["stats"] = dict(base_stats)

        site_id = plan[0]["id"] if plan else 0
        os.makedirs(EXPORT_TMP_DIR, exist_ok=True)
        parts, scripts_used, truncated_all = [], {}, {}

        for cust_id in sorted(by_cust):
            if job.get("cancel"):
                raise _Cancelled()
            cust_daily = by_cust.pop(cust_id)      # 取出即摘除，处理完就能回收
            d_rows = [_row_from_bucket(b, k[0]) for k, b in cust_daily.items()]
            summary: dict = {}
            for k, b in cust_daily.items():
                skey = k[1:]
                s_b = summary.get(skey)
                if s_b is None:
                    s_b = summary[skey] = _Bucket(dict(b.meta, ratios=dict(b.meta["ratios"])))
                s_b.merge(b)
            s_rows = [_row_from_bucket(b, None) for b in summary.values()]
            s_rows.sort(key=_sort_key)
            d_rows.sort(key=_sort_key)

            uname = next((r.get("username") for r in s_rows if r.get("username")), "") or str(cust_id)
            # 只带该客户用到的单价行，别把别人的价目表也塞进他的账单
            used = {r.get("price_key") for r in s_rows}
            cust_pricing = {k: v for k, v in pricing.items()
                            if _pk_str(k) in used}

            st = dict(base_stats)
            st["customer"] = uname
            st["customer_filter"] = [cust_id]
            eff_cols = drop_zero_columns(cols, d_rows)
            eff_detail = drop_zero_columns(detail_cols, d_rows) if with_detail else []
            st["dropped_columns"] = [c["label"] for c in cols
                                     if c["key"] not in {x["key"] for x in eff_cols}
                                     and not c.get("internal")]

            writer, script_name = writer_for_customer(site_id, cust_id)
            scripts_used[uname] = script_name
            job["phase"] = f"生成表格（{uname} · {script_name}）"

            fd, out = tempfile.mkstemp(prefix="bill_", suffix=".xlsx", dir=EXPORT_TMP_DIR)
            os.close(fd)
            try:
                tr = writer(out, eff_cols, s_rows,
                            d_rows if with_daily else None,
                            (cust_spool.get(cust_id), detail_cols, eff_detail) if with_detail else None,
                            cust_pricing, st, default_qpu)
            except Exception as exc:  # noqa: BLE001
                try:
                    os.unlink(out)
                except OSError:
                    pass
                raise RuntimeError(f"客户「{uname}」用脚本「{script_name}」生成失败：{exc}") from exc
            for k2, v2 in (tr or {}).items():
                truncated_all[f"{uname}·{k2}"] = v2
            parts.append((uname, out))
            cust_daily.clear(); d_rows.clear(); s_rows.clear(); summary.clear()

        if not parts:
            raise RuntimeError("该账期内没有任何可导出的消费记录")

        title_safe = title.replace(' ', '_').replace('~', '-')
        if len(parts) == 1:
            uname, out_path = parts[0]
            job["file"] = out_path
            job["filename"] = f"{_safe_name(uname)}_{title_safe}.xlsx"
        else:
            # 多个客户：每人一份，打包 zip
            job["phase"] = "打包"
            fd, zpath = tempfile.mkstemp(prefix="bill_", suffix=".zip", dir=EXPORT_TMP_DIR)
            os.close(fd)
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
                for uname, fp in parts:
                    zf.write(fp, f"{_safe_name(uname)}_{title_safe}.xlsx")
                    try:
                        os.unlink(fp)
                    except OSError:
                        pass
            job["file"] = zpath
            job["filename"] = f"{_safe_name(plan[0]['name'] if plan else '账单')}_{title_safe}_共{len(parts)}个客户.zip"

        base_stats["truncated"] = truncated_all
        base_stats["scripts"] = scripts_used
        base_stats["files"] = len(parts)
        job["stats"] = base_stats
        job["size"] = os.path.getsize(job["file"])
        job["status"] = "done"
        job["phase"] = "完成"
        job["finished"] = time.time()
    except _Cancelled:
        job["status"] = "cancelled"
        job["phase"] = "已取消"
        job["finished"] = time.time()
    except Exception as exc:  # noqa: BLE001
        logger.exception("账单导出失败 job=%s", job_id)
        job["status"] = "error"
        # 异常文本里可能带着源库 DSN（含明文密码），脱敏后再落到页面/日志
        job["error"] = _mask_secrets(f"{type(exc).__name__}: {exc}", plan)
        job["finished"] = time.time()
    finally:
        for _p in cust_spool.values():       # 清掉每个客户的明细临时文件
            try:
                os.unlink(_p)
            except OSError:
                pass
