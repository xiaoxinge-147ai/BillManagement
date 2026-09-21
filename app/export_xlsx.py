"""账单 xlsx 生成。用 XlsxWriter 的 constant_memory 模式流式写，逐条明细几十万行也不吃内存。

sheet 顺序（= tab 顺序）：账单汇总 → 按天明细 → 模型单价 → 说明 → 详细明细。
逐条明细放最后，是因为 constant_memory 模式下每个 worksheet 必须一次性按行序写完，
而汇总要等全部读完才算得出来 —— 所以明细先落到临时 CSV，最后再流式搬进来。
"""
from __future__ import annotations

import csv
import os

import xlsxwriter

DETAIL_CHUNK = 1000000  # 每个明细 sheet 最多这么多数据行，超了自动开下一个

# tab 排序权重（写入顺序 ≠ 展示顺序：单价表必须先写，但要显示在账单后面）
_TAB_ORDER = {"账单汇总": 0, "按天明细": 10, "详细明细": 20, "模型单价": 30, "说明": 40}

_NUMFMT = {
    "int": "#,##0",
    "money": "#,##0.0000",
    "ratio": "0.####",
    "price": "#,##0.000000",
    "pct": '0.00"%"',
    "text": None,
    "datetime": None,
}


def _formats(wb):
    return {
        "head": wb.add_format({
            "bold": True, "bg_color": "#F1F5F9", "font_color": "#334155",
            "border": 1, "border_color": "#CBD5E1", "align": "center", "valign": "vcenter",
            "text_wrap": True,
        }),
        "title": wb.add_format({"bold": True, "font_size": 13, "font_color": "#1F2937"}),
        "kv_k": wb.add_format({"bold": True, "font_color": "#475569"}),
        "bad": wb.add_format({"bg_color": "#FEE2E2", "font_color": "#991B1B"}),
        # 公式格用蓝字标识：这一格是算出来的，点开能看见引用了哪些单元格
        "money_fx": wb.add_format({"num_format": "#,##0.0000", "font_color": "#1D4ED8"}),
        "cell": {k: (wb.add_format({"num_format": v}) if v else None) for k, v in _NUMFMT.items()},
    }


def _write_header(ws, cols, fmt):
    for i, c in enumerate(cols):
        ws.write_string(0, i, c["label"], fmt["head"])
        ws.set_column(i, i, c.get("w", 12))
    ws.freeze_panes(1, 0)
    ws.set_row(0, 30)


def _colletter(n: int) -> str:
    """0 → A, 25 → Z, 26 → AA…"""
    s = ""
    while n >= 0:
        s = chr(65 + n % 26) + s
        n = n // 26 - 1
    return s


def build_amount_formula(cols, price_index):
    """把「消费金额」做成引用「模型单价」sheet 折后价的 Excel 公式。

    金额 = Σ(本行各类 token × 单价表里该模型的折后价) ÷ 1e6，按次 = 单次折后价 × 调用次数。
    这样客户点开金额格就能顺着引用跳到单价表，看清用的是哪一档折后价。
    price_index: {查找键: 单价表行号}；账单行按隐藏的 price_key 列匹配。
    """
    pos = {c["key"]: i for i, c in enumerate(cols)}
    if "amount_recalc" not in pos or "price_key" not in pos:
        return None

    def cell(key, row):
        return f"{_colletter(pos[key])}{row}" if key in pos else None

    def pcell(which, prow):
        return f"'{PRICE_SHEET}'!${_colletter(PRICE_COL[which])}${prow}"

    rows_idx = (price_index or {}).get("row") or {}
    have_idx = (price_index or {}).get("have") or {}

    def make(row, rowdata):
        lookup = rowdata.get("price_key")
        prow = rows_idx.get(lookup)
        if not prow:
            return None                      # 单价表里没有对应行，退回写数值
        avail = have_idx.get(lookup) or set()
        kind = rowdata.get("billing_kind")
        if kind == "按次":
            if "calls" not in pos or "per_call" not in avail:
                return None
            return f"={pcell('per_call', prow)}*{cell('calls', row)}"
        if kind not in ("按量", "音频/实时", "分层表达式"):
            return None                      # 任务/违规/退款：无单价可乘，直接采信数值
        terms = []
        for tkey, which in (("input_tokens", "input"), ("cache_tokens", "cache_read"),
                            ("cache_create_plain", "cache_5m"), ("cache_create_5m", "cache_5m"),
                            ("cache_create_1h", "cache_1h"), ("completion_tokens", "output"),
                            ("image_tokens", "image")):
            if tkey in pos and (rowdata.get(tkey) or 0):
                if which not in avail:
                    return None              # 有用量但单价表里那格是空的 → 公式会算出 0，退回数值
                terms.append(f"{cell(tkey, row)}*{pcell(which, prow)}")
        if not terms:
            return None
        return "=(" + "+".join(terms) + ")/1000000"

    return pos["amount_recalc"], make


def _write_cell(ws, r, i, col, val, fmt):
    kind = col["kind"]
    cf = fmt["cell"].get(kind)
    if val is None or val == "":
        ws.write_blank(r, i, None, cf)
    elif kind in ("int", "money", "ratio", "pct"):
        try:
            ws.write_number(r, i, float(val), cf)
        except (TypeError, ValueError):
            ws.write_string(r, i, str(val), cf)
    else:
        ws.write_string(r, i, str(val), cf)


def _diff_highlight(ws, cols, nrows, fmt):
    """差异% 绝对值超过 1% 的自动标红，把不可重算/公式盲区的行暴露出来。"""
    if nrows <= 0:
        return
    for i, c in enumerate(cols):
        if c["key"] != "amount_diff_pct":
            continue
        ws.conditional_format(1, i, nrows, i, {
            "type": "cell", "criteria": "not between",
            "minimum": -1, "maximum": 1, "format": fmt["bad"],
        })


def _finish_sheet(ws, cols, r, fmt):
    ws.autofilter(0, 0, max(r, 1), len(cols) - 1)
    _diff_highlight(ws, cols, r, fmt)


def _visible(cols):
    return [c for c in cols if not c.get("internal")]


def _write_rows(wb, name, cols, rows, fmt, amount_fx=None):
    """写一个聚合 sheet。超过 Excel 行数上限自动开「名2」「名3」…

    XlsxWriter 在 row >= 1048576 时是**静默丢弃**（返回 -1，不抛异常），
    而 autofilter 又不做边界校验，会写出越界 ref 让 Excel 报「文件已损坏」。
    amount_fx = (列下标, make(row, 计费类型)) 时，该列写 Excel 公式而非数值。
    """
    out_cols = _visible(cols)
    ws = wb.add_worksheet(name)
    _write_header(ws, out_cols, fmt)
    if not out_cols:
        return 0
    fx_col, fx_make = amount_fx if amount_fx else (None, None)
    idx, r, written = 1, 0, 0
    for row in rows:
        if r >= DETAIL_CHUNK:
            _finish_sheet(ws, out_cols, r, fmt)
            idx += 1
            ws = wb.add_worksheet(f"{name}{idx}")
            _write_header(ws, out_cols, fmt)
            r = 0
        r += 1
        written += 1
        for i, c in enumerate(out_cols):
            val = row.get(c["key"])
            f = fx_make(r + 1, row) if i == fx_col else None
            if f:
                ws.write_formula(r, i, f, fmt["money_fx"], float(val or 0))
            else:
                _write_cell(ws, r, i, c, val, fmt)
    _finish_sheet(ws, out_cols, r, fmt)
    return written


def _cast(kind, v):
    if v is None or v == "":
        return None
    if kind == "int":
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0
    if kind in ("money", "ratio", "pct"):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return v


def _write_detail(wb, spool_path, spool_cols, cols, fmt, amount_fx=None):
    """把临时 CSV 里的逐条明细搬进来，超过 Excel 行数上限就自动分 sheet。

    spool_cols 是写 CSV 时的完整列序，cols 是最终要输出的列（可能被裁掉了全 0 列），
    按 key 取下标挑选，避免错位。
    """
    out_cols = _visible(cols)
    if not out_cols or not spool_path or not os.path.exists(spool_path):
        ws = wb.add_worksheet("详细明细")
        _write_header(ws, out_cols, fmt)
        return
    pos = {c["key"]: i for i, c in enumerate(spool_cols)}
    take = [pos.get(c["key"], -1) for c in out_cols]
    kinds = [c["kind"] for c in out_cols]
    fx_col, fx_make = amount_fx if amount_fx else (None, None)
    idx, r = 1, 0
    ws = wb.add_worksheet("详细明细")      # 无条件先建，0 行时也要有这个 sheet
    _write_header(ws, out_cols, fmt)
    with open(spool_path, "r", newline="", encoding="utf-8") as fh:
        for rec in csv.reader(fh):
            if r >= DETAIL_CHUNK:
                _finish_sheet(ws, out_cols, r, fmt)
                idx += 1
                ws = wb.add_worksheet(f"详细明细{idx}")
                _write_header(ws, out_cols, fmt)
                r = 0
            r += 1
            rowdata = {c["key"]: _cast(c["kind"], rec[pos[c["key"]]])
                       for c in spool_cols if pos[c["key"]] < len(rec)}
            for i, c in enumerate(out_cols):
                j = take[i]
                raw = rec[j] if 0 <= j < len(rec) else None
                f = fx_make(r + 1, rowdata) if i == fx_col else None
                if f:
                    ws.write_formula(r, i, f, fmt["money_fx"], float(_cast("money", raw) or 0))
                else:
                    _write_cell(ws, r, i, c, _cast(kinds[i], raw), fmt)
    _finish_sheet(ws, out_cols, r, fmt)


# 单价表：一行一个 (站点, 模型, 分组, 倍率指纹)。账单里的公式靠 A 列的「查找键」引用这里。
PRICE_HEAD = [
    ("查找键", 30), ("模型名", 26), ("分组", 12), ("计费方式", 12), ("命中阶梯", 14), ("分组倍率", 10),
    ("官方原价·输入", 14), ("官方原价·输出", 14), ("官方原价·缓存读取", 16),
    ("官方原价·缓存创建5m", 18), ("官方原价·缓存创建1h", 18), ("官方原价·图片", 14),
    ("折后价·输入", 13), ("折后价·输出", 13), ("折后价·缓存读取", 15),
    ("折后价·缓存创建5m", 17), ("折后价·缓存创建1h", 17), ("折后价·图片", 13),
    ("单次原价", 13), ("单次折后价", 13),
]
PRICE_SHEET = "模型单价"
# 折后价各列在 PRICE_HEAD 里的下标（0-based）
PRICE_COL = {
    "input": 12, "output": 13, "cache_read": 14,
    "cache_5m": 15, "cache_1h": 16, "image": 17, "per_call": 19,
}
PRICE_FIRST_ROW = 3          # 数据从第 3 行开始（1=表头，2=注释）


def price_key(site, model, group, rkey):
    """账单行与单价表行的对应关系。倍率变过 → rkey 不同 → 单价表里是两行。"""
    return f"{model}|{group}|{rkey[:6]}"


def _write_pricing(wb, pricing, fmt, qpu):
    """模型单价：官方原价（1 倍率即上游官方价）与折后价（× 分组倍率）。

    账单 sheet 里的「消费金额」公式用 VLOOKUP 引用本表的折后价列，
    所以每个出现在账单里的 (模型 × 分组 × 倍率指纹) 都必须在这里占一行 ——
    包括分层表达式这种无法用倍率反推单价的，留空单价、单独标注。
    返回 {查找键: 行号}，供公式生成使用。
    """
    from app.export_spec import BILLING_KIND_LABEL

    # 单位跟随站点自身的额度口径（QuotaPerUnit 换算结果），不做币种换算
    unit = "金额"
    money = fmt["cell"]["money"]
    ratio = fmt["cell"]["ratio"]
    per_call_fmt = wb.add_format({"num_format": "#,##0.000000"})

    ws = wb.add_worksheet(PRICE_SHEET)
    for i, (label, w) in enumerate(PRICE_HEAD):
        ws.write_string(0, i, label, fmt["head"])
        ws.set_column(i, i, w)
    ws.freeze_panes(2, 0)
    ws.set_row(0, 30)
    ws.write_string(1, 0, f"单位：{unit} / 1M tokens（按次为 {unit}/次）。"
                          f"按量：官方原价 = 倍率 × 1e6/QuotaPerUnit（1 倍率即上游官方价）。"
                          f"分层表达式：官方原价取「命中阶梯」在计费表达式里的该档系数（本身即 /1M 单价）。"
                          f"折后价 = 官方原价 × 分组倍率，账单里的金额公式引用的就是折后价列。")
    ws.set_row(1, 18)

    index = {}
    r = 1
    have: dict = {}
    for key, v in sorted(pricing.items(), key=lambda kv: (kv[1]["kind"] != "usage", kv[0])):
        site, model, group, rkey = key[0], key[1], key[2], key[3]
        rt, kind = v["ratios"], v["kind"]
        k = 1000000.0 / (v.get("qpu") or qpu or 1)
        gr = rt.get("group_ratio") or 0.0
        mr = rt.get("model_ratio") or 0.0
        ccr = rt.get("cache_creation_ratio") or 0.0
        r += 1
        lookup = price_key(site, model, group, rkey)
        index[lookup] = r + 1                    # Excel 行号（1-based）
        avail = set()
        ws.write_string(r, 0, lookup)
        ws.write_string(r, 1, model or "")
        ws.write_string(r, 2, group or "")
        ws.write_string(r, 3, BILLING_KIND_LABEL.get(kind, kind))
        ws.write_string(r, 4, str(rt.get("_tier_name") or ""))
        ws.write_number(r, 5, float(gr), ratio)

        # 六项官方原价：按量由倍率推；分层直接用命中档位在表达式里的系数（本身就是 $/1M）
        if kind == "tiered":
            tp = rt.get("_tier_prices") or {}
            officials = [tp.get("input"), tp.get("output"), tp.get("cache_read"),
                         tp.get("cache_5m"), tp.get("cache_1h"), tp.get("image")]
        elif kind in ("usage", "audio"):
            base = mr * k
            officials = [base * float(m) if m else None for m in
                         (1.0, rt.get("completion_ratio"), rt.get("cache_ratio"), ccr,
                          (ccr * 1.6 if ccr else 0), rt.get("image_ratio"))]
        else:
            officials = [None] * 6
        for n, off_val in enumerate(officials):
            off, dis = 6 + n, PRICE_COL["input"] + n
            if off_val:
                ws.write_number(r, off, float(off_val), money)
                ws.write_number(r, dis, float(off_val) * gr, money)
                avail.add(("input", "output", "cache_read", "cache_5m", "cache_1h", "image")[n])
            else:
                # 该类 token 没出现过就留空（写 0 会被读成「免费」）
                ws.write_blank(r, off, None, money)
                ws.write_blank(r, dis, None, money)
        # 按次两列
        mp = float(rt.get("model_price") or 0)
        if kind == "per_call" and mp > 0:
            ws.write_number(r, 18, mp, per_call_fmt)
            ws.write_number(r, PRICE_COL["per_call"], mp * gr, per_call_fmt)
            avail.add("per_call")
        else:
            ws.write_blank(r, 18, None, per_call_fmt)
            ws.write_blank(r, PRICE_COL["per_call"], None, per_call_fmt)
        have[lookup] = avail
    ws.autofilter(1, 0, max(r, 2), len(PRICE_HEAD) - 1)
    return {"row": index, "have": have}


NOTES = [
    "「消费金额」是表格内的 Excel 公式（蓝色字），点开单元格即可看到它引用了本行的 token 列与"
    "「模型单价」sheet 里对应模型的折后价，可自行改单价验算。",
    "  公式（按量）= Σ(各类 token × 模型单价表里的折后价) ÷ 1,000,000；（按次）= 单次折后价 × 调用次数。",
    "  分层表达式计费的模型没有可反推的单价（金额由 new-api 的表达式算出），该行金额直接取系统扣费额，"
    "在「模型单价」表里也会列出但单价留空。",
    "「系统扣费额」= SUM(logs.quota) 换算，是 new-api 当时真实扣掉的额度，用于交叉核对。",
    "两者在下列情况会不一致，属预期：分层表达式计费(quota 由表达式算出)、多张图生成(张数乘数不落库)、"
    "异步任务、违规扣费、退款 —— 这些行的消费金额直接采信系统扣费额；另有最小计费 1 quota 地板带来的零头差。",
    "「输入token(不含缓存)」按计费语义分流：Anthropic 口径的 prompt_tokens 本就不含缓存；"
    "OpenAI/Gemini 口径含缓存，已减去缓存读取与缓存创建。图片/音频 token 两种口径都已从中扣除。",
    "「缓存创建token」的 5m / 1h 拆分只有 Anthropic 语义的日志才有；其余归入「普通」。1h 倍率恒为 5m 的 1.6 倍。",
    "分组倍率取自每条日志冻结的 other.group_ratio，已包含用户组专属倍率的覆盖，不会重复计算。",
    "new-api 的倍率配置没有历史版本，因此本表一律使用日志中冻结的倍率；周期内倍率变更会自动拆成多行。",
    "渠道测试日志（令牌名「模型测试」）已排除；退款日志(type=6)按负数冲销。",
    "音频 / Realtime(ws) 日志用的是另一套公式（text/audio 四分量 × audio_ratio），已单独识别并按该公式重算。",
    "订阅计费日志(billing_source=subscription)的 quota 照记但未从钱包扣款，条数见上，如需按现金口径请自行剔除。",
    "金额换算比例 QuotaPerUnit 逐站从各客户站 options 表读取，未读到才回退本系统配置。",
    "「模型单价」：官方原价 = 倍率 × 1e6/QuotaPerUnit（1 倍率即上游官方价）；折后价 = 官方原价 × 分组倍率，"
    "即该客户实际单价。按次计费只有单次原价/折后价两列。各类倍率不再出现在账单表里，统一在此查。",
    "币种选项只决定表格里的单位标注，不做汇率换算 —— 站点额度按哪个币种设定，算出来就是那个币种。",
    "某类 token 整份账单都为 0 时（例如该客户从未用过图片/音频/缓存），对应的列不会输出。",
]


def _write_notes(wb, stats, fmt, qpu):
    ws = wb.add_worksheet("说明")
    ws.set_column(0, 0, 22)
    ws.set_column(1, 1, 110)
    r = 0
    ws.write_string(r, 0, "客户账单导出说明", fmt["title"])
    r += 2
    kv = [
        ("账期", stats.get("period", "")),
        ("客户站", stats.get("sites", "")),
        ("QuotaPerUnit", "；".join(f"{k}={v:,.0f}" for k, v in (stats.get("site_qpu") or {}).items())
                          or f"{qpu:,.0f}"),
        ("读取日志行数", f'{stats.get("rows_read", 0):,}'),
        ("逐条明细行数", f'{stats.get("detail_rows", 0):,}'),
        ("排除的渠道测试日志", f'{stats.get("skipped_test", 0):,}'),
        ("other 解析失败行数", f'{stats.get("parse_failed", 0):,}'),
        ("订阅计费日志条数", f'{stats.get("subscription_rows", 0):,}'),
        ("客户筛选", ("仅 " + "、".join(str(c) for c in stats["customer_filter"]))
                     if stats.get("customer_filter") else "全部客户"),
        ("未输出的空列", "、".join(stats.get("dropped_columns") or []) or "无"),
        ("耗时(秒)", str(stats.get("elapsed", ""))),
    ]
    for key, val in kv:
        ws.write_string(r, 0, key, fmt["kv_k"])
        ws.write_string(r, 1, str(val))
        r += 1

    r += 1
    ws.write_string(r, 0, "各计费类型", fmt["kv_k"])
    r += 1
    ws.write_string(r, 0, "类型", fmt["head"])
    ws.write_string(r, 1, "条数 / 金额", fmt["head"])
    r += 1
    for name, v in (stats.get("kinds") or {}).items():
        ws.write_string(r, 0, name)
        ws.write_string(r, 1, f'{v["calls"]:,} 条 / {v["amount"]:,.4f}')
        r += 1

    r += 1
    ws.write_string(r, 0, "口径说明", fmt["kv_k"])
    r += 1
    for line in NOTES:
        ws.write_string(r, 1, "· " + line)
        r += 1


def write_workbook(path, cols, summary_rows, daily_rows, detail, pricing, stats, qpu):
    """detail: None 或 (spool_csv_path, spool_cols, detail_cols)。

    spool_cols = 写 CSV 时的列序；detail_cols = 最终输出列（可能少几列）。
    qpu 只作为「模型单价」sheet 的兜底换算基准，逐行优先用各站自己的值。
    """
    from app.export_spec import sheet_columns

    wb = xlsxwriter.Workbook(path, {"constant_memory": True, "default_date_format": "yyyy-mm-dd"})
    truncated = {}
    try:
        fmt = _formats(wb)
        # 单价表先写，账单里的金额公式要引用它的行号
        price_index = _write_pricing(wb, pricing, fmt, qpu)
        sum_cols = sheet_columns(cols, "summary")
        n = _write_rows(wb, "账单汇总", sum_cols, summary_rows, fmt,
                        build_amount_formula(sum_cols, price_index))
        if n < len(summary_rows):
            truncated["账单汇总"] = len(summary_rows) - n
        if daily_rows is not None:
            day_cols = sheet_columns(cols, "daily")
            n = _write_rows(wb, "按天明细", day_cols, daily_rows, fmt,
                            build_amount_formula(day_cols, price_index))
            if n < len(daily_rows):
                truncated["按天明细"] = len(daily_rows) - n
        _write_notes(wb, stats, fmt, qpu)
        if detail is not None:
            spool_path, spool_cols, detail_cols = detail
            _write_detail(wb, spool_path, spool_cols, detail_cols, fmt,
                          build_amount_formula(detail_cols, price_index))
        # tab 顺序：账单在前，单价/说明在后（写入顺序被单价表的行号依赖绑住了）
        wb.worksheets_objs.sort(key=lambda w: _TAB_ORDER.get(w.name, 50))
    finally:
        wb.close()
    return truncated
