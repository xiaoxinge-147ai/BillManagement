"""数据模型。第 1 步只需要「站点」。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import BigInteger, Date, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.crypto import decrypt, encrypt
from app.db import Base


class EncryptedText(TypeDecorator):
    """透明加密文本列：写入加密、读取解密（兼容历史明文）。"""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        return encrypt(value)

    def process_result_value(self, value, dialect):  # noqa: ANN001
        return decrypt(value)


class AppSetting(Base):
    """运行期可配置项（键值）。如定时任务开关/时间。"""

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Site(Base):
    """总站 / 客户站点的只读数据库连接。"""

    __tablename__ = "site"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # master=总站, customer=客户站
    site_type: Mapped[str] = mapped_column(String(16), default="master")
    # mysql / postgresql / sqlite
    dialect: Mapped[str] = mapped_column(String(16), default="mysql")
    # 只读连接串（加密存储），例：mysql+pymysql://user:pass@host:3306/dbname
    dsn: Mapped[Optional[str]] = mapped_column(EncryptedText)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChannelUsageDaily(Base):
    """每天保存的渠道×模型消耗快照。存原始 quota（精确），展示时换算成平台单位。"""

    __tablename__ = "channel_usage_daily"
    __table_args__ = (
        UniqueConstraint("stat_date", "site_id", "channel_id", "model_name", name="uq_usage_daily"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stat_date: Mapped[date] = mapped_column(Date, index=True)
    site_id: Mapped[int] = mapped_column(Integer, index=True)
    channel_id: Mapped[int] = mapped_column(Integer, index=True)
    channel_name: Mapped[Optional[str]] = mapped_column(String(256))
    model_name: Mapped[str] = mapped_column(String(128))
    quota: Mapped[int] = mapped_column(BigInteger, default=0)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChannelRatio(Base):
    """渠道消耗倍率（按量模型用）。成本 = 平台单位 × 倍率。一个渠道一个值。"""

    __tablename__ = "channel_ratio"
    __table_args__ = (UniqueConstraint("site_id", "channel_id", name="uq_channel_ratio"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(Integer, index=True)
    channel_id: Mapped[int] = mapped_column(Integer, index=True)
    ratio: Mapped[float] = mapped_column(default=1.0)


class PerCallPrice(Base):
    """按次单价（按次模型用）。成本 = 单价 × 调用次数。按 渠道+模型 设置。"""

    __tablename__ = "per_call_price"
    __table_args__ = (
        UniqueConstraint("site_id", "channel_id", "model_name", name="uq_per_call_price"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(Integer, index=True)
    channel_id: Mapped[int] = mapped_column(Integer, index=True)
    model_name: Mapped[str] = mapped_column(String(128))
    unit_price: Mapped[float] = mapped_column(default=0.0)


class TokenChannelUsage(Base):
    """总站 分组×令牌×渠道 日消耗快照（把渠道成本摊到令牌用）。"""

    __tablename__ = "token_channel_usage"
    __table_args__ = (
        UniqueConstraint("stat_date", "site_id", "token_id", "channel_id", "model_name",
                         name="uq_token_channel"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stat_date: Mapped[date] = mapped_column(Date, index=True)
    site_id: Mapped[int] = mapped_column(Integer, index=True)
    group_name: Mapped[Optional[str]] = mapped_column(String(128))
    token_id: Mapped[int] = mapped_column(Integer, index=True)
    token_name: Mapped[Optional[str]] = mapped_column(String(256))
    channel_id: Mapped[int] = mapped_column(Integer, index=True)
    model_name: Mapped[str] = mapped_column(String(128), default="")
    quota: Mapped[int] = mapped_column(BigInteger, default=0)
    calls: Mapped[int] = mapped_column(Integer, default=0)


class CustomerUsageDaily(Base):
    """客户站 客户×分组×渠道 日消耗快照（落库，供客户利润读取，不再实时查源库）。"""

    __tablename__ = "customer_usage_daily"
    __table_args__ = (
        UniqueConstraint("stat_date", "site_id", "customer_id", "group_name", "channel_id",
                         "model_name", name="uq_customer_usage"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stat_date: Mapped[date] = mapped_column(Date, index=True)
    site_id: Mapped[int] = mapped_column(Integer, index=True)
    customer_id: Mapped[int] = mapped_column(Integer, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(256))
    group_name: Mapped[Optional[str]] = mapped_column(String(128))
    channel_id: Mapped[int] = mapped_column(Integer, index=True)
    model_name: Mapped[str] = mapped_column(String(128), default="")
    quota: Mapped[int] = mapped_column(BigInteger, default=0)
    tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    group_ratio: Mapped[float] = mapped_column(default=0.0)  # 消耗时实际应用的分组倍率(含专属)，0=未取到
    calls: Mapped[int] = mapped_column(Integer, default=0)


class CustomerGroupRatio(Base):
    """客户站分组倍率（用于把 quota 换算成 1倍率消耗）。默认 1。"""

    __tablename__ = "customer_group_ratio"
    __table_args__ = (UniqueConstraint("site_id", "group_name", name="uq_cust_group_ratio"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(Integer, index=True)
    group_name: Mapped[str] = mapped_column(String(128))
    ratio: Mapped[float] = mapped_column(default=1.0)


class CustomerSpecialRatio(Base):
    """客户专属倍率（覆盖分组倍率）。某客户特殊倍率时用它换算 1倍率。"""

    __tablename__ = "customer_special_ratio"
    __table_args__ = (UniqueConstraint("site_id", "customer_id", name="uq_cust_special_ratio"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(Integer, index=True)
    customer_id: Mapped[int] = mapped_column(Integer, index=True)
    ratio: Mapped[float] = mapped_column(default=1.0)


GLOBAL_BILLMODE_SITE = 0  # 按次模型为全局配置，所有客户站公用，存到 site_id=0


class CustomerBillMode(Base):
    """按次模型标记（全局、人工、持久）。site_id=0 表示所有客户站公用；group_name 留空=所有分组。"""

    __tablename__ = "customer_bill_mode"
    __table_args__ = (UniqueConstraint("site_id", "group_name", "model_name", name="uq_cust_bill_mode"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(Integer, index=True)
    group_name: Mapped[str] = mapped_column(String(128), default="")
    model_name: Mapped[str] = mapped_column(String(128))
    mode: Mapped[str] = mapped_column(String(16), default="per_call")  # per_call


class TokenMeta(Base):
    """总站令牌元信息（token_id, name, key）。落库供客户站 sk→令牌 映射。"""

    __tablename__ = "token_meta"
    __table_args__ = (UniqueConstraint("site_id", "token_id", name="uq_token_meta"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(Integer, index=True)
    token_id: Mapped[int] = mapped_column(Integer, index=True)
    token_name: Mapped[Optional[str]] = mapped_column(String(256))
    sk: Mapped[Optional[str]] = mapped_column(Text)


class ChannelMeta(Base):
    """渠道元信息（从源库 channels 读取）：sk、上游地址。用于按 sk 合并对账、从上游读用量。"""

    __tablename__ = "channel_meta"
    __table_args__ = (UniqueConstraint("site_id", "channel_id", name="uq_channel_meta"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(Integer, index=True)
    channel_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(256))
    sk: Mapped[Optional[str]] = mapped_column(Text)          # 上游 key（明文）
    base_url: Mapped[Optional[str]] = mapped_column(String(512))


class SkActual(Base):
    """按 sk 的供应商实际用量：平台用量 + 充值倍率。实际成本 = 平台用量 × 充值倍率。"""

    __tablename__ = "sk_actual"
    __table_args__ = (UniqueConstraint("site_id", "stat_date", "sk", name="uq_sk_actual"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(Integer, index=True)
    stat_date: Mapped[date] = mapped_column(Date, index=True)
    sk: Mapped[str] = mapped_column(Text)
    platform_usage: Mapped[float] = mapped_column(default=0.0)   # 上游读到/手填的平台用量
    recharge_ratio: Mapped[float] = mapped_column(default=1.0)   # 充值倍率
    has_usage: Mapped[bool] = mapped_column(default=False)       # 是否已有用量（区分未录入）
    source: Mapped[Optional[str]] = mapped_column(String(16))    # upstream / manual


class UpstreamCred(Base):
    """上游登录账号。scope='__default__' 为默认账密；否则为某上游 base_url 的覆盖。"""

    __tablename__ = "upstream_cred"
    __table_args__ = (UniqueConstraint("scope", name="uq_upstream_cred"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(512), index=True)
    username: Mapped[Optional[str]] = mapped_column(String(256))   # 备注名
    # 复用原密码列存「用户ID」：旧版 new-api 需要 New-Api-User 头，
    # 填了就直连、免去 1~N 遍历探测（详见 app/upstream.py）。留空则自动探测。
    password: Mapped[Optional[str]] = mapped_column(Text)
    login_url: Mapped[Optional[str]] = mapped_column(String(512))   # 接口地址（默认取 base_url 的根）
    status_ok: Mapped[Optional[bool]] = mapped_column()             # 上次测试结果
    status_msg: Mapped[Optional[str]] = mapped_column(String(256))
    # 访问令牌(PAT)：new-api 里由「个人设置 → 生成系统访问令牌」得到。
    # 取代账密登录 —— 新版 new-api 的令牌列表把 key 脱敏了，靠登录+后缀猜令牌名已不可靠。
    access_token: Mapped[Optional[str]] = mapped_column(EncryptedText)


class ExportScript(Base):
    """账单导出脚本（呈现层）。内容是一段 Python，必须导出 write_workbook(...)。

    ⚠️ 上传的脚本会在本服务进程内执行 —— 等同于服务器代码权限，只对可信人员开放。
    数据层（读源库、拆 token、按 new-api 公式重算金额）不可替换，保证各客户金额口径一致。
    """

    __tablename__ = "export_script"
    __table_args__ = (UniqueConstraint("name", name="uq_export_script_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    note: Mapped[Optional[str]] = mapped_column(String(256))
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class ExportScriptBinding(Base):
    """脚本绑定。customer_id=0 表示「该站点默认」。

    解析优先级：客户绑定 > 站点默认 > 系统内置默认脚本。
    """

    __tablename__ = "export_script_binding"
    __table_args__ = (
        UniqueConstraint("site_id", "customer_id", name="uq_export_binding"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(Integer, index=True)
    customer_id: Mapped[int] = mapped_column(Integer, index=True, default=0)
    script_id: Mapped[int] = mapped_column(Integer, index=True)


class ExportScriptAudit(Base):
    """导出脚本的变更审计。脚本能在服务器上执行代码，改动必须留痕。"""

    __tablename__ = "export_script_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    action: Mapped[str] = mapped_column(String(16))            # save / delete
    script_name: Mapped[str] = mapped_column(String(128))
    content_sha: Mapped[Optional[str]] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer, default=0)
    ip: Mapped[Optional[str]] = mapped_column(String(64))


class AppUser(Base):
    """后台账号。role='admin' 为超级管理员（拥有全部模块，且只有它能管账号）。

    密码存 PBKDF2 派生值，不存明文；modules / site_ids 是逗号分隔的授权清单，
    admin 忽略这两列。
    """

    __tablename__ = "app_user"
    __table_args__ = (UniqueConstraint("username", name="uq_app_user_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(256), default="")
    role: Mapped[str] = mapped_column(String(16), default="operator")   # admin / operator
    modules: Mapped[str] = mapped_column(Text, default="")             # 逗号分隔的模块 key
    site_ids: Mapped[str] = mapped_column(Text, default="")            # 逗号分隔的客户站 id，空=不限
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class AuthAudit(Base):
    """账号与权限的变更留痕（谁在什么时候改了谁）。"""

    __tablename__ = "auth_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[Optional[str]] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(32))          # login / login_failed / create / update / delete / reset_pw
    target: Mapped[Optional[str]] = mapped_column(String(64))
    detail: Mapped[Optional[str]] = mapped_column(Text)
    ip: Mapped[Optional[str]] = mapped_column(String(64))
