"""账单导出脚本：存取、校验、执行、绑定解析。

脚本只接管**呈现层**（把算好的数据写成 Excel），数据层不可替换 ——
读源库、拆 token、按 new-api 计费公式重算金额这些必须对所有客户一致，否则就是算错账。

脚本契约（与内置默认脚本 app/export_xlsx.py 的签名完全一致）：

    def write_workbook(path, cols, summary_rows, daily_rows, detail, pricing, stats, qpu) -> dict

    path         要写入的 xlsx 路径
    cols         列定义列表，每项 {key,label,scope,kind,w,visible}
    summary_rows 账单汇总行（dict 列表）
    daily_rows   按天明细行；未勾选时为 None
    detail       None 或 (逐条明细的临时CSV路径, CSV列序, 输出列序)
    pricing      {(站点,模型,分组,倍率指纹): {ratios,kind,qpu,site}}
    stats        账期/站点/条数/币种等元信息，用于「说明」sheet
    qpu          QuotaPerUnit 兜底值
    返回          {sheet名: 被截断的行数}，没有就返回 {}

⚠️ 安全：脚本内容会在本服务进程内 exec 执行，等同于服务器代码权限。
   只对可信人员开放，务必设置 APP_PASSWORD。
"""
from __future__ import annotations

import hashlib
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ExportScript, ExportScriptAudit, ExportScriptBinding

logger = logging.getLogger(__name__)

ENTRY = "write_workbook"
BUILTIN_NAME = "系统默认"
_BUILTIN_PATH = Path(__file__).parent / "export_xlsx.py"

# 编译缓存：内容哈希 → 模块命名空间。同一脚本反复导出不必重复 exec。
_CACHE: dict = {}
_LOCK = threading.Lock()


def guard_enabled() -> Optional[str]:
    """加固①：没设访问密码就不开放脚本功能 —— 否则等于把服务器代码权限对外裸奔。

    返回 None 表示可用，否则返回拒绝原因。
    """
    if not (settings.app_password or "").strip():
        return ("未设置 APP_PASSWORD。导出脚本会在服务器上执行代码，"
                "必须先在 .env 里设置访问密码并重启，才能使用本功能。")
    return None


def check_confirm(pw: str, user=None) -> None:
    """加固②：改脚本时再确认一次密码，避免误点或会话被借用。

    校验的是**当前登录账号自己的密码** —— 运营人员用自己的账号密码即可，
    不必知道 .env 里的 APP_PASSWORD（那个只是启用开关，不是谁的登录凭据）。
    拿不到登录用户时（理论上不会，路由都在鉴权之后）才回退比 APP_PASSWORD。
    """
    pw = pw or ""
    if not pw:
        raise ValueError("请填写你的登录密码以确认此操作")
    if user is not None and getattr(user, "password_hash", ""):
        from app import auth as auth_mod
        if not auth_mod.verify_password(pw, user.password_hash):
            raise ValueError("密码不正确（填你自己的登录密码）")
        return
    if pw != (settings.app_password or ""):
        raise ValueError("密码不正确")


def audit(db: Session, action: str, name: str, content: str = "", ip: str = "") -> None:
    """加固③：脚本变更留痕（谁改的这套系统里没有用户体系，至少记时间/来源/内容哈希）。"""
    try:
        db.add(ExportScriptAudit(
            action=action, script_name=name[:128],
            content_sha=hashlib.sha256((content or "").encode()).hexdigest()[:64] if content else None,
            size=len(content or ""), ip=(ip or "")[:64],
        ))
        db.commit()
        logger.warning("导出脚本变更 action=%s name=%s size=%d ip=%s",
                       action, name, len(content or ""), ip)
    except Exception:  # noqa: BLE001
        db.rollback()


def recent_audit(db: Session, limit: int = 15) -> list:
    return db.scalars(
        select(ExportScriptAudit).order_by(ExportScriptAudit.id.desc()).limit(limit)
    ).all()


BUILTIN_OVERRIDE_KEY = "export_builtin_override"


def builtin_factory_source() -> str:
    """出厂内置脚本的源码（app/export_xlsx.py 本身），用于「恢复出厂」与下载改写。"""
    try:
        return _BUILTIN_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def builtin_override(db: Session) -> str:
    """管理员替换过的内置脚本源码；没替换过返回空串。"""
    from app.appconfig import get_value
    return get_value(db, BUILTIN_OVERRIDE_KEY, "") or ""


def builtin_source(db: Session = None) -> str:
    """当前生效的内置脚本源码：替换过就用替换的，否则用出厂的。"""
    if db is not None:
        ov = builtin_override(db)
        if ov.strip():
            return ov
    return builtin_factory_source()


def set_builtin_override(db: Session, content: str) -> None:
    """替换内置脚本。传空串 = 恢复出厂。替换前先编译校验，坏脚本不许落库。"""
    from app.appconfig import set_value
    content = content or ""
    if content.strip():
        compile_script(content, BUILTIN_NAME)      # 编译不过直接抛，不写库
    set_value(db, BUILTIN_OVERRIDE_KEY, content)


def compile_script(content: str, label: str = "script"):
    """把脚本源码编译成可调用的 write_workbook。校验失败抛 ValueError。"""
    if not (content or "").strip():
        raise ValueError("脚本内容为空")
    key = hashlib.sha256(content.encode("utf-8")).hexdigest()
    with _LOCK:
        hit = _CACHE.get(key)
    if hit:
        return hit

    try:
        code = compile(content, f"<导出脚本:{label}>", "exec")
    except SyntaxError as exc:
        raise ValueError(f"语法错误：第 {exc.lineno} 行 {exc.msg}") from exc

    ns: dict = {"__name__": f"export_script_{key[:8]}", "__file__": f"<{label}>"}
    try:
        exec(code, ns)  # noqa: S102 — 这是本功能的核心：用户自定义导出脚本
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"脚本加载失败：{type(exc).__name__}: {exc}") from exc

    fn = ns.get(ENTRY)
    if not callable(fn):
        raise ValueError(f"脚本里没有找到 {ENTRY}() 函数")
    with _LOCK:
        if len(_CACHE) > 32:
            _CACHE.clear()
        _CACHE[key] = fn
    return fn


def validate(content: str) -> str:
    """上传前校验，返回给用户看的提示。不通过则抛 ValueError。"""
    fn = compile_script(content, "校验")
    import inspect

    try:
        params = list(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return "已通过（无法读取参数签名，运行时再校验）"
    need = ["path", "cols", "summary_rows", "daily_rows", "detail", "pricing", "stats", "qpu"]
    if len(params) < len(need) and not any(
        p for p in params if p.startswith("*")
    ):
        raise ValueError(
            f"{ENTRY}() 需要 {len(need)} 个参数 {need}，当前只有 {len(params)} 个：{params}"
        )
    return "已通过校验"


# ---------------------------------------------------------------- 增删改查

def list_scripts(db: Session) -> list:
    return db.scalars(select(ExportScript).order_by(ExportScript.id)).all()


def get_script(db: Session, sid: int) -> Optional[ExportScript]:
    return db.get(ExportScript, sid) if sid else None


def save_script(db: Session, sid: Optional[int], name: str, note: str, content: str) -> ExportScript:
    """新增或更新脚本。会先校验，校验不过不落库。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("脚本名不能为空")
    if name == BUILTIN_NAME:
        raise ValueError(f"「{BUILTIN_NAME}」是内置脚本的保留名，请换一个")
    validate(content)

    obj = db.get(ExportScript, sid) if sid else None
    dup = db.scalars(select(ExportScript).where(ExportScript.name == name)).first()
    if dup and (obj is None or dup.id != obj.id):
        raise ValueError(f"已有同名脚本「{name}」")
    if obj is None:
        obj = ExportScript(name=name)
        db.add(obj)
    obj.name = name
    obj.note = (note or "")[:250]
    obj.content = content
    obj.updated_at = datetime.now()
    db.commit()
    return obj


def delete_script(db: Session, sid: int) -> None:
    """删除脚本，连同它的绑定 —— 那些客户回落到默认脚本。"""
    obj = db.get(ExportScript, sid)
    if not obj:
        return
    for b in db.scalars(
        select(ExportScriptBinding).where(ExportScriptBinding.script_id == sid)
    ).all():
        db.delete(b)
    db.delete(obj)
    db.commit()


# ---------------------------------------------------------------- 绑定

def set_binding(db: Session, site_id: int, customer_id: int, script_id: int) -> None:
    """script_id<=0 表示解绑（回落到上一级）。"""
    obj = db.scalars(
        select(ExportScriptBinding).where(
            ExportScriptBinding.site_id == site_id,
            ExportScriptBinding.customer_id == customer_id,
        )
    ).first()
    if script_id and script_id > 0:
        if obj is None:
            obj = ExportScriptBinding(site_id=site_id, customer_id=customer_id)
            db.add(obj)
        obj.script_id = script_id
    elif obj is not None:
        db.delete(obj)
    db.commit()


def bindings_for_site(db: Session, site_id: int) -> dict:
    """返回 {customer_id: script_id}，其中 0 = 该站点默认。"""
    return {
        b.customer_id: b.script_id
        for b in db.scalars(
            select(ExportScriptBinding).where(ExportScriptBinding.site_id == site_id)
        ).all()
    }


def resolve(db: Session, site_id: int, customer_id: int) -> tuple:
    """解析某客户实际用哪个脚本。返回 (script_id, 显示名, 来源)。

    优先级：客户绑定 > 站点默认 > 系统内置默认。
    """
    bmap = bindings_for_site(db, site_id)
    for cid, src in ((customer_id, "客户绑定"), (0, "站点默认")):
        sid = bmap.get(cid)
        if sid:
            obj = db.get(ExportScript, sid)
            if obj:
                return obj.id, obj.name, src
    return 0, BUILTIN_NAME, "系统默认"


def writer_for(db: Session, site_id: int, customer_id: int):
    """拿到该客户实际要用的 write_workbook 函数 + 脚本名。

    自定义脚本编译/执行失败时**不静默回退**到默认脚本 —— 那会让客户拿到一份
    格式不对却毫无提示的账单。直接抛错，让导出任务显示失败原因。
    """
    sid, name, _src = resolve(db, site_id, customer_id)
    if not sid:
        return _builtin_writer(db), name
    obj = db.get(ExportScript, sid)
    if not obj:
        return _builtin_writer(db), BUILTIN_NAME
    return compile_script(obj.content, obj.name), obj.name


def _builtin_writer(db: Session):
    """内置脚本的 write_workbook。被替换过就编译替换版，否则用打包进来的那份。"""
    ov = builtin_override(db)
    if ov.strip():
        return compile_script(ov, BUILTIN_NAME)
    from app.export_xlsx import write_workbook
    return write_workbook
