"""导出脚本与数据层共用的稳定符号。

脚本契约只有 write_workbook(...) 的 8 个入参，原则上不应 import 数据层的内部函数。
但历史脚本（以及内置默认脚本）曾经 `from app.bill_export import ...` 拿过一些常量，
一旦数据层改名/删除，脚本就会崩。把这类**契约内会用到的稳定符号**集中到这里，
脚本与数据层都从这里取 —— 数据层重构时这里保持不变，脚本就不会碎。

⚠️ 不要在这里放数据层的函数（除了 BILLING_KIND_LABEL 这类纯常量映射），
   这些是给脚本方看的稳定接口，不是数据层内部实现。
"""
from __future__ import annotations

# 币种：只决定金额符号/单位标注（站点额度按哪个币种设定就是哪个币种），
# 不做汇率换算。数据层把它放进 stats["currency"] 传给脚本。
CURRENCY = {
    "usd": {"label": "美元", "symbol": "$"},
    "cny": {"label": "人民币", "symbol": "￥"},
}
CURRENCY_DEFAULT = "usd"

# 计费类型 → 中文名。账单行 billing_kind 字段用的是英文键，脚本展示时转中文。
BILLING_KIND_LABEL = {
    "usage": "按量", "per_call": "按次", "tiered": "分层表达式",
    "task": "异步任务", "violation": "违规扣费", "refund": "退款", "test": "渠道测试",
    "nodata": "无倍率信息", "audio": "音频/实时",
}


def currency_symbol(currency: str) -> str:
    """某币种的货币符号。传入的值只在 usd/cny 之间，默认 $。"""
    c = CURRENCY.get(currency, CURRENCY[CURRENCY_DEFAULT])
    return c["symbol"]


def currency_label(currency: str) -> str:
    c = CURRENCY.get(currency, CURRENCY[CURRENCY_DEFAULT])
    return c["label"]


# 列按 scope 分组：summary 只出 all 列，daily 出 all+daily，detail 出 all+daily+detail。
_SCOPE_KIND = {"summary": {"all"}, "daily": {"all", "daily"},
               "detail": {"all", "daily", "detail"}}


def sheet_columns(cols: list, scope: str) -> list:
    """按 sheet 取可见且属于该 scope 的列。脚本写法：sheet_columns(cols, "summary")。"""
    allow = _SCOPE_KIND.get(scope, _SCOPE_KIND["summary"])
    return [c for c in (cols or []) if c.get("visible", True) and c.get("scope", "all") in allow]
