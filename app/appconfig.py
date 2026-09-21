"""运行期可配置项（持久到自有数据库 app_setting 表），带 .env 默认回退。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import settings
from app.models import AppSetting

KEY_ENABLED = "schedule_enabled"
KEY_CRON = "schedule_cron"


def get_value(db: Session, key: str, default: str | None = None) -> str | None:
    o = db.get(AppSetting, key)
    return o.value if o else default


def set_value(db: Session, key: str, value: str) -> None:
    o = db.get(AppSetting, key)
    if o is None:
        db.add(AppSetting(key=key, value=str(value)))
    else:
        o.value = str(value)
    db.commit()


def get_schedule(db: Session) -> tuple[bool, str]:
    """返回 (是否启用, cron)。未配置时回退 .env 默认。"""
    enabled = get_value(db, KEY_ENABLED)
    cron = get_value(db, KEY_CRON)
    if enabled is None:
        enabled = "1" if settings.auto_save else "0"
    if not cron:
        cron = settings.save_cron
    return enabled == "1", cron


def set_schedule(db: Session, enabled: bool, cron: str) -> None:
    set_value(db, KEY_ENABLED, "1" if enabled else "0")
    set_value(db, KEY_CRON, cron.strip() or settings.save_cron)
