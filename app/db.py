"""自有数据库：引擎、会话、建表。"""
from __future__ import annotations

import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _make_engine():
    dsn = settings.resolved_dsn()
    connect_args = {}
    is_sqlite = isinstance(dsn, str) and dsn.startswith("sqlite")
    if is_sqlite:
        # 确保 sqlite 目录存在
        path = dsn.replace("sqlite:///", "")
        if path and os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        connect_args = {"check_same_thread": False}
    return create_engine(dsn, pool_pre_ping=True, future=True, connect_args=connect_args)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _ensure_columns() -> None:
    """模型新增列而表已存在时补列（create_all 不会 ALTER）。"""
    from sqlalchemy import inspect
    additions = {
        "upstream_cred": [
            ("login_url", "VARCHAR(512)"),
            ("status_ok", "BOOLEAN"),
            ("status_msg", "VARCHAR(256)"),
            ("access_token", "TEXT"),
        ],
    }
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for table, cols in additions.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        for name, ddl in cols:
            if name not in existing:
                try:
                    with engine.begin() as conn:
                        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                except Exception:  # noqa: BLE001
                    pass


def _encrypt_legacy_dsn() -> None:
    """把历史明文 DSN 加密入库（一次性，仅处理未加密的）。"""
    from sqlalchemy import text
    from app.crypto import encrypt
    try:
        with engine.begin() as conn:
            rows = conn.execute(text("SELECT id, dsn FROM site")).fetchall()
            for rid, raw in rows:
                if raw and not str(raw).startswith("enc::"):
                    conn.execute(text("UPDATE site SET dsn=:d WHERE id=:i"),
                                 {"d": encrypt(raw), "i": rid})
    except Exception:  # noqa: BLE001
        pass


def _drop_outdated_snapshots() -> None:
    """快照表(可再生)若缺少新列(model_name)，直接删表由 create_all 重建。"""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    required = {"token_channel_usage": "model_name", "customer_usage_daily": "group_ratio"}
    for tbl, col in required.items():
        if tbl in tables:
            cols = {c["name"] for c in insp.get_columns(tbl)}
            if col not in cols:
                try:
                    with engine.begin() as conn:
                        conn.execute(text(f"DROP TABLE {tbl}"))
                except Exception:  # noqa: BLE001
                    pass


def init_db() -> None:
    import app.models  # noqa: F401  注册模型
    _drop_outdated_snapshots()
    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    _encrypt_legacy_dsn()
