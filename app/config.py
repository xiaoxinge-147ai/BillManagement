"""配置：从环境变量 / .env 读取。

自有数据库连接优先级：
  1) 显式 OWN_DB_DSN（完整连接串）
  2) DB_HOST 提供时，用 DB_USER/DB_PASSWORD/... 安全拼成 PostgreSQL 连接串
     （密码含特殊字符也没问题，由 URL.create 转义）
  3) 都没有时，回退本地 SQLite 文件（零配置）
"""
from __future__ import annotations

from functools import lru_cache
from typing import Union

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 方式1：显式完整连接串（可选）
    own_db_dsn: str = ""

    # 方式2：分项（部署用 PostgreSQL）
    db_host: str = ""
    db_port: int = 5432
    db_user: str = "bill"
    db_password: str = "bill"
    db_name: str = "bill"

    # 统计时区
    timezone: str = "Asia/Shanghai"

    # 额度换算：平台单位 = quota / quota_per_unit（new-api 默认 1 美元 = 500000 quota）
    quota_per_unit: float = 500000

    # 每天自动保存快照的时间。支持 5 段(分 时 日 月 周) 或 6 段(秒 分 时 日 月 周,精确到秒)
    save_cron: str = "0 2 * * *"
    # 是否启用自动保存（本地调试可关）
    auto_save: bool = True

    # 对账差异颜色阈值（差异百分比绝对值）：<green 绿，<amber 黄，否则红
    recon_green: float = 1.0
    recon_amber: float = 5.0

    # 站点访问密码（旧版单一密码，仅作超管初始化的兜底；账号体系启用后不再直接用于登录）
    app_password: str = ""

    # 超级管理员账号（首次启动据此创建；改了 ADMIN_PASSWORD 会同步更新超管密码）
    admin_user: str = "admin"
    admin_password: str = ""

    # 敏感字段（DSN 等）加密密钥；留空则用默认派生（建议在 .env 设置）
    crypto_secret: str = ""

    def resolved_dsn(self) -> Union[str, URL]:
        if self.own_db_dsn.strip():
            return self.own_db_dsn.strip()
        if self.db_host.strip():
            return URL.create(
                "postgresql+psycopg",
                username=self.db_user,
                password=self.db_password,
                host=self.db_host,
                port=self.db_port,
                database=self.db_name,
            )
        return "sqlite:///./data/bill.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
