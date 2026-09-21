"""只读读取源库（new-api）的消耗数据。

new-api `logs` 表关键字段（已核对）：
  created_at(unix秒), type(2=消费), channel_id, model_name, quota, token_id, group, user_id
第 1 步：按 渠道×模型 聚合某天的消耗量(quota) 与调用次数。
"""
from __future__ import annotations

import time as _time
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

from app.config import settings

LOG_TYPE_CONSUME = 2


def build_engine(dialect: str, dsn: str):
    connect_args = {}
    if dsn.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    elif dialect.startswith("mysql"):
        connect_args = {"connect_timeout": 8}
    return create_engine(dsn, pool_pre_ping=True, pool_size=2, max_overflow=2,
                         pool_recycle=1800, future=True, connect_args=connect_args)


def day_range(d: date) -> tuple[int, int]:
    tz = ZoneInfo(settings.timezone)
    start = datetime.combine(d, time.min, tzinfo=tz)
    end = datetime.combine(d + timedelta(days=1), time.min, tzinfo=tz)
    return int(start.timestamp()), int(end.timestamp())


def channel_usage(engine, start_ts: int, end_ts: int) -> list[dict]:
    sql = text(
        """
        SELECT channel_id AS channel_id,
               model_name AS model_name,
               COALESCE(SUM(quota), 0) AS quota,
               COUNT(*) AS calls
        FROM logs
        WHERE type = :t AND created_at >= :s AND created_at < :e
        GROUP BY channel_id, model_name
        ORDER BY channel_id, model_name
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"t": LOG_TYPE_CONSUME, "s": start_ts, "e": end_ts}).mappings().all()
    return [dict(r) for r in rows]


def channel_names(engine) -> dict[int, str]:
    """渠道id -> 渠道名（失败则返回空，不影响展示）。"""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, name FROM channels")).mappings().all()
        return {int(r["id"]): r["name"] for r in rows}
    except Exception:  # noqa: BLE001
        return {}


def token_channel_usage(engine, dialect: str, start_ts: int, end_ts: int) -> list[dict]:
    """总站：按 分组×令牌×渠道 聚合消耗（用于把渠道成本摊到令牌）。"""
    grp = "`group`" if dialect.startswith("mysql") else '"group"'
    sql = text(
        f"""
        SELECT {grp} AS group_name, token_id AS token_id, token_name AS token_name,
               channel_id AS channel_id, model_name AS model_name,
               COALESCE(SUM(quota),0) AS quota, COUNT(*) AS calls
        FROM logs
        WHERE type = :t AND created_at >= :s AND created_at < :e
        GROUP BY {grp}, token_id, token_name, channel_id, model_name
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"t": LOG_TYPE_CONSUME, "s": start_ts, "e": end_ts}).mappings().all()
    return [dict(r) for r in rows]


def group_ratios(engine, dialect: str) -> dict:
    """读取客户站已配置的分组倍率（new-api options 表 key='GroupRatio' 的 JSON）。"""
    import json
    key_col = "`key`" if dialect.startswith("mysql") else "key"
    for k in ("GroupRatio", "GroupRatioMapping"):
        try:
            with engine.connect() as conn:
                row = conn.execute(text(f"SELECT value FROM options WHERE {key_col} = :k"), {"k": k}).first()
            if row and row[0]:
                data = json.loads(row[0])
                out = {}
                for g, v in data.items():
                    try:
                        out[g] = float(v)
                    except (TypeError, ValueError):
                        pass
                if out:
                    return out
        except Exception:  # noqa: BLE001
            continue
    return {}


def list_tokens(engine, dialect: str) -> list[dict]:
    """总站令牌表：id, name, key(sk)。用于把客户站渠道 sk 映射到总站令牌。"""
    key_col = "`key`" if dialect.startswith("mysql") else "key"
    sql = text(f"SELECT id AS token_id, name AS token_name, {key_col} AS sk FROM tokens")
    try:
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(sql).mappings().all()]
    except Exception:  # noqa: BLE001
        return []


def channel_group_keys(engine, dialect: str) -> list[dict]:
    """客户站渠道：分组 → sk(key)。每分组通常一个渠道。"""
    grp = "`group`" if dialect.startswith("mysql") else '"group"'
    key_col = "`key`" if dialect.startswith("mysql") else "key"
    sql = text(f"SELECT {grp} AS group_name, {key_col} AS sk FROM channels")
    try:
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(sql).mappings().all()]
    except Exception:  # noqa: BLE001
        return []


def customer_usage(engine, dialect: str, start_ts: int, end_ts: int) -> list[dict]:
    """客户站：按 分组×客户(user)×渠道 聚合消耗与扣费（带 channel_id 以精确映射总站令牌）。"""
    grp = "`group`" if dialect.startswith("mysql") else '"group"'
    sql = text(
        f"""
        SELECT {grp} AS group_name, user_id AS user_id, username AS username,
               channel_id AS channel_id, model_name AS model_name,
               COALESCE(SUM(quota),0) AS quota,
               COALESCE(SUM(prompt_tokens + completion_tokens),0) AS tokens,
               COUNT(*) AS calls
        FROM logs
        WHERE type = :t AND created_at >= :s AND created_at < :e
        GROUP BY {grp}, user_id, username, channel_id, model_name
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"t": LOG_TYPE_CONSUME, "s": start_ts, "e": end_ts}).mappings().all()
    return [dict(r) for r in rows]


def customer_group_ratio_used(engine, dialect: str, start_ts: int, end_ts: int) -> dict:
    """从日志 other(JSON) 读取消耗时实际应用的分组倍率(含用户专属倍率)，按 (分组, user_id) 聚合。

    new-api 每条消费日志的 other 字段记录了当时实际生效的 group_ratio。
    取不到（字段不存在/非JSON/键名不同）时返回 {}，调用方回退到 options 分组倍率。
    """
    grp = "`group`" if dialect.startswith("mysql") else '"group"'
    if dialect.startswith("mysql"):
        expr = "CAST(JSON_EXTRACT(other, '$.group_ratio') AS DECIMAL(20,8))"
    elif dialect.startswith("postgres"):
        expr = "(other::json ->> 'group_ratio')::float8"
    else:
        expr = "json_extract(other, '$.group_ratio')"
    sql = text(
        f"""
        SELECT {grp} AS group_name, user_id AS user_id, MAX({expr}) AS ratio
        FROM logs
        WHERE type = :t AND created_at >= :s AND created_at < :e AND other IS NOT NULL
        GROUP BY {grp}, user_id
        """
    )
    out: dict = {}
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, {"t": LOG_TYPE_CONSUME, "s": start_ts, "e": end_ts}).mappings().all()
        for r in rows:
            try:
                v = float(r["ratio"])
            except (TypeError, ValueError):
                continue
            if v > 0:
                out[(r["group_name"] or "", int(r["user_id"]))] = v
    except Exception:  # noqa: BLE001
        return {}
    return out


# ---------------- 账单导出：逐条读取消费日志 ----------------

LOG_TYPE_REFUND = 6
BILL_LOG_TYPES = (LOG_TYPE_CONSUME, LOG_TYPE_REFUND)
# 渠道测试也写 type=2 日志（管理员触发，token_id=0），账单与客户清单都要排除
CHANNEL_TEST_TOKEN_NAME = "模型测试"

# 账单需要的列。older new-api 没有 request_id，按实际存在的列拼 SELECT。
_BILL_COLS = [
    "id", "created_at", "type", "user_id", "username", "token_id", "token_name",
    "model_name", "channel_id", "quota", "prompt_tokens", "completion_tokens",
    "request_id", "other",
]


def logs_columns(engine) -> set:
    """探测源库 logs 表实际有哪些列（兼容不同 new-api 版本）。

    探测失败时回退到「各版本都存在」的保守列集，避免 SELECT 到不存在的列（如老版本没有
    request_id）而整个导出失败。
    """
    try:
        with engine.connect() as conn:
            rs = conn.execute(text("SELECT * FROM logs WHERE 1=0"))
            found = {str(k) for k in rs.keys()}
        if found:
            return found
    except Exception:  # noqa: BLE001
        pass
    return set(_BILL_COLS) - {"request_id"} | {"group"}


def apply_read_guards(conn, dialect: str, timeout_ms: int = 60000) -> None:
    """给只读连接加语句超时，避免拖垮线上库。失败静默（权限不足等）。"""
    try:
        if dialect.startswith("mysql"):
            conn.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME={int(timeout_ms)}")
        elif dialect.startswith("postgres"):
            conn.exec_driver_sql(f"SET statement_timeout = {int(timeout_ms)}")
    except Exception:  # noqa: BLE001
        pass


def bill_logs_sql(dialect: str, cols: set, user_ids: Optional[list] = None):
    """构造 keyset 分页 SQL。返回 (sql, 额外参数, 是否按 id 排序)。

    new-api 的 logs 表有三个相关索引：
      idx_created_at_id(created_at, id)   —— 按时间范围扫
      idx_user_id_id(user_id, id)         —— 按用户扫
      idx_created_at_type(created_at, type)

    所以分两种走法，否则 MySQL 两个索引都用不上、直接超时：
      · 没筛客户  → ORDER BY created_at, id，吃 idx_created_at_id
      · 筛了客户  → ORDER BY id，吃 idx_user_id_id（(user_id, id) 前缀完全命中，无需排序）
    keyset 条件用行值比较而不是 `a>x OR (a=x AND b>y)` —— 后者 MySQL 优化器
    识别不成索引范围扫描。
    """
    grp = "`group`" if dialect.startswith("mysql") else '"group"'
    sel = [c for c in _BILL_COLS if (not cols or c in cols)]
    if not cols or "group" in cols:
        sel.append(f"{grp} AS group_name")

    extra: dict = {}
    if user_ids:
        keys = []
        for i, uid in enumerate(user_ids):
            extra[f"u{i}"] = int(uid)
            keys.append(f":u{i}")
        sql = text(
            f"""
            SELECT {', '.join(sel)}
            FROM logs
            WHERE user_id IN ({', '.join(keys)})
              AND id > :lid
              AND type IN (:t1, :t2)
              AND created_at >= :s AND created_at < :e
            ORDER BY id
            LIMIT :lim
            """
        )
        return sql, extra, True

    sql = text(
        f"""
        SELECT {', '.join(sel)}
        FROM logs
        WHERE type IN (:t1, :t2)
          AND created_at >= :s AND created_at < :e
          AND (created_at, id) > (:lct, :lid)
        ORDER BY created_at, id
        LIMIT :lim
        """
    )
    return sql, extra, False


def min_id_since(engine, dialect: str, start_ts: int, timeout_ms: int = 60000) -> int:
    """账期起点对应的最小 id。走 idx_created_at_id，很快。

    按 id 分页时用它做下界，否则会从 id=0 开始扫该客户的全部历史日志
    （账期只要 31 天，却扫了几年的量）。取不到就返回 0（退化成全扫，但不会出错）。
    """
    try:
        with engine.connect() as conn:
            apply_read_guards(conn, dialect, timeout_ms)
            row = conn.execute(
                text("SELECT MIN(id) FROM logs WHERE created_at >= :s"), {"s": start_ts}
            ).first()
        return int(row[0]) - 1 if row and row[0] else 0
    except Exception:  # noqa: BLE001
        return 0


def _run_page(conn, sql, extra, start_ts, end_ts, last_ct, last_id, limit):
    params = {"t1": LOG_TYPE_CONSUME, "t2": LOG_TYPE_REFUND,
              "s": start_ts, "e": end_ts, "lct": last_ct, "lid": last_id,
              "lim": int(limit), **extra}
    return [dict(r) for r in conn.execute(sql, params).mappings().all()]


def bill_logs_page(engine, dialect: str, cols: set, start_ts: int, end_ts: int,
                   last_ct: int, last_id: int, limit: int, timeout_ms: int = 60000,
                   user_ids: Optional[list] = None) -> list[dict]:
    """按 keyset 分页读一批日志（单次调用版，自带连接）。"""
    sql, extra, _by_id = bill_logs_sql(dialect, cols, user_ids)
    with engine.connect() as conn:
        apply_read_guards(conn, dialect, timeout_ms)
        return _run_page(conn, sql, extra, start_ts, end_ts, last_ct, last_id, limit)


def iter_bill_logs(engine, dialect: str, cols: set, start_ts: int, end_ts: int,
                   batch: int = 20000, sleep_ms: int = 0, timeout_ms: int = 180000,
                   user_ids: Optional[list] = None, on_retry=None):
    """流式迭代整个账期的消费/退款日志，一批一批 yield。

    整段账期一次连续扫描 + 全程复用一条连接；批次大小遇超时会自动减半重试，
    最小到 500 —— 宁可多跑几批，也不要整个导出因为一次超时全废。
    """
    sql, extra, by_id = bill_logs_sql(dialect, cols, user_ids)
    if by_id:
        # 按 id 分页：先把下界推到账期起点，别从 0 开始扫历史
        last_ct, last_id = start_ts - 1, min_id_since(engine, dialect, start_ts, timeout_ms)
    else:
        last_ct, last_id = start_ts - 1, 0

    conn = engine.connect()
    cur = max(int(batch), 1)
    try:
        apply_read_guards(conn, dialect, timeout_ms)
        while True:
            try:
                rows = _run_page(conn, sql, extra, start_ts, end_ts, last_ct, last_id, cur)
            except Exception as exc:  # noqa: BLE001
                if cur <= 500:
                    raise
                cur = max(cur // 4, 500)          # 超时/中断 → 批次缩小重试
                if on_retry:
                    on_retry(cur, exc)
                try:                              # 连接可能已被服务端中断，换一条
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                conn = engine.connect()
                apply_read_guards(conn, dialect, timeout_ms)
                continue
            if not rows:
                return
            yield rows
            last = rows[-1]
            last_ct = int(last.get("created_at") or 0)
            last_id = int(last.get("id") or 0)
            if len(rows) < cur:
                return
            if sleep_ms:
                _time.sleep(sleep_ms / 1000.0)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _table_columns(engine, table: str) -> set:
    """探测某表实际有哪些列。失败返回空集。"""
    try:
        with engine.connect() as conn:
            rs = conn.execute(text(f"SELECT * FROM {table} WHERE 1=0"))
            return {str(k) for k in rs.keys()}
    except Exception:  # noqa: BLE001
        return set()


def list_customers(engine, dialect: str, timeout_ms: int = 20000) -> list[dict]:
    """列出该客户站的全部客户（user_id, username, 备注名）。

    直接读 `users` 表 —— 只有几百行，秒回。
    早期实现是扫一周 logs 做 DISTINCT，在真实站点上动辄百万行、会撞语句超时，
    而超时被当成「没有客户」，看起来就像搜索功能坏了。
    id / username 两列各版本都有；display_name 有就带上，方便按中文名搜。
    **不吞异常**：连不上/没权限要让调用方看见真实原因，不能伪装成空列表。
    """
    cols = _table_columns(engine, "users")
    sel = ["id", "username"]
    if "display_name" in cols:
        sel.append("display_name")
    where = ""
    if "deleted_at" in cols:            # gorm 软删除
        where = " WHERE deleted_at IS NULL"
    sql = text(f"SELECT {', '.join(sel)} FROM users{where}")
    with engine.connect() as conn:
        apply_read_guards(conn, dialect, timeout_ms)
        rows = conn.execute(sql).mappings().all()
    out = []
    for r in rows:
        out.append({
            "user_id": int(r["id"] or 0),
            "username": r["username"] or "",
            "display_name": (r.get("display_name") or "") if "display_name" in sel else "",
        })
    return sorted(out, key=lambda x: (x["username"].lower(), x["user_id"]))


def read_options(engine, dialect: str, keys: list) -> dict:
    """批量读源库 options 表的若干 key（MySQL 用 `key`，PG/其它用 "key"）。"""
    key_col = "`key`" if dialect.startswith("mysql") else '"key"'
    marks = ", ".join(f":k{i}" for i in range(len(keys)))
    params = {f"k{i}": k for i, k in enumerate(keys)}
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(f"SELECT {key_col} AS k, value AS v FROM options WHERE {key_col} IN ({marks})"),
                params,
            ).mappings().all()
        return {str(r["k"]): r["v"] for r in rows}
    except Exception:  # noqa: BLE001
        return {}


def read_quota_per_unit(engine, dialect: str, default: float) -> float:
    """读客户站自己的 QuotaPerUnit（options 表 key='QuotaPerUnit'）。

    额度→金额的换算比例是**每个站点各自可改**的运行时配置，不能用本系统的全局值一刀切，
    否则多站同时导出时金额会整体按比例偏掉。读不到就回退传入的默认值。
    """
    raw = read_options(engine, dialect, ["QuotaPerUnit"]).get("QuotaPerUnit")
    if raw:
        try:
            v = float(str(raw).strip())
            if v > 0:
                return v
        except ValueError:
            pass
    return default


def read_usd_rate(engine, dialect: str, default: float = 7.3) -> float:
    """读客户站的美元→人民币汇率（options 表 key='USDExchangeRate'，new-api 默认 7.3）。"""
    raw = read_options(engine, dialect, ["USDExchangeRate"]).get("USDExchangeRate")
    if raw:
        try:
            v = float(str(raw).strip())
            if v > 0:
                return v
        except ValueError:
            pass
    return default


def channel_info(engine, dialect: str) -> list[dict]:
    """渠道完整元信息：id, name, sk(key), base_url。用于按 sk 合并对账与上游读取。"""
    key_col = "`key`" if dialect.startswith("mysql") else "key"
    sql = text(f"SELECT id AS channel_id, name AS name, {key_col} AS sk, base_url AS base_url FROM channels")
    try:
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(sql).mappings().all()]
    except Exception:  # noqa: BLE001
        return []
