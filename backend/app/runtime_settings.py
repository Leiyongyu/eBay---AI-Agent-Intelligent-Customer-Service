"""系统配置页：管理员在页面上改的配置项，存数据库（表 app_settings），优先于 .env。

- 只开放下面 FIELDS 列出的项；数据库地址、eBay/模型密钥等不在这里改；
- 密码类只能写不能读：接口只返回“已设置/未设置”，审计里不记值；
- 改完立即生效（重新生成 Settings）；标了“需重启”的是启动时才读的，改完要重启 main.py；
- 删掉页面配置就回到 .env 里的值。
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .db import session_scope, utcnow
from .models import AppSetting, AuditEvent

log = logging.getLogger(__name__)

SECRETS = {"invoice_smtp_password", "aftersales_mail_password"}

# (分组, 键, 名称, 说明, 需重启, 改动提醒)
FIELDS: list[tuple[str, str, str, str, bool, str | None]] = [
    ("发票", "invoice_auto_send", "普通发票自动发送", "买家在 eBay 消息里给了邮箱、订单能确定时，自动生成普通发票并直接发到邮箱，不用审核；增值税发票一律转人工", False, None),
    ("发票", "invoice_test_recipient", "测试收件邮箱", "填了以后所有发票邮件都改发到这个邮箱（不发给买家），测试完清空", False, None),
    ("发票", "invoice_mail_enabled", "真正发送邮件", "关掉时只记为模拟发送，不发出邮件", False, None),
    ("发票", "invoice_smtp_host", "SMTP 服务器", "腾讯企业邮为 smtp.exmail.qq.com", False, None),
    ("发票", "invoice_smtp_port", "SMTP 端口", "SSL 一般为 465", False, None),
    ("发票", "invoice_smtp_user", "发件邮箱", "也会写进给买家的提醒消息里", False, None),
    ("发票", "invoice_smtp_password", "发件邮箱密码", "企业邮箱的客户端专用密码", False, None),
    ("发票", "invoice_mail_from_name", "发件人显示名", "留空用店铺名", False, None),
    ("发票", "invoice_mail_bcc", "密送邮箱", "每封发票邮件密送一份留档（可空）", False, None),
    ("售后邮箱", "aftersales_mail_enabled", "收售后邮件", "定时收买家发来的照片、视频", True, None),
    ("售后邮箱", "aftersales_imap_host", "IMAP 服务器", "腾讯企业邮为 imap.exmail.qq.com", False, None),
    ("售后邮箱", "aftersales_imap_port", "IMAP 端口", "SSL 一般为 993", False, None),
    ("售后邮箱", "aftersales_mail_user", "售后邮箱地址", "", False, None),
    ("售后邮箱", "aftersales_mail_password", "售后邮箱密码", "客户端专用密码", False, None),
    ("售后邮箱", "aftersales_public_address", "对买家说的售后邮箱", "买家发视频、在 WeTransfer 里填的收件邮箱；空 = 用售后邮箱地址。"
     "填别的地址时，这个地址的邮件要能进系统读的售后邮箱（同一个或设了自动转发）", False, None),
    ("售后邮箱", "aftersales_mail_share_with_buyers", "AI 可告诉买家售后邮箱", "买家要发视频、大文件时 AI 可以提供这个邮箱", False, None),
    ("售后邮箱", "aftersales_retention_days", "附件保留天数", "", False, None),
    ("AI 自动回复", "llm_translate_model", "翻译模型", "买家来信译中文、草稿回译用的模型（同一个秘钥）；留空 = 用回复模型", False, None),
    ("AI 自动回复", "llm_translate_thinking", "翻译开思考模式", "一般不用；开了每句都多出思考 token", False, None),
    ("AI 自动回复", "auto_send_enabled", "自动回复总开关", "关掉后所有店铺都只生成草稿、不自动发", False, None),
    ("AI 自动回复", "auto_send_max_risk", "自动发送最高风险", "L0 或 L1；L2、L3 永远转人工", False, None),
    ("AI 自动回复", "auto_send_exclude_intents", "仍要人工的意图", "英文逗号分隔，如 fitment,price_offer", False, None),
    ("AI 自动回复", "auto_reply_max_streak", "连续自动回复上限", "同一会话 AI 连续自动回复几次后，买家再来信交人工", False, None),
    ("AI 自动回复", "auto_tag_enabled", "自动打标记", "按问题类型自动给会话打标记", False, None),
    ("物流查件", "logistics_ticket_auto_submit", "查件单自动提交谷仓", "起草后直接提交到谷仓（真实写操作）", False, None),
    ("物流查件", "logistics_ticket_auto_send", "查件结果自动回复买家", "", False, None),
    ("物流查件", "logistics_ticket_send_needs_human", "需人工的结果也回复", "丢失、退回等结果也直接回复买家，同时标待售后", False, None),
    ("物流查件", "logistics_ticket_notify_emails", "查件通知邮箱", "谷仓必填，英文逗号分隔，最多 10 个", False, None),
    ("eBay 消息", "ebay_sync_enabled", "定时拉取 eBay 消息", "", False,
     "打开后真实买家来信会被自动处理、自动回复，物流问题还会自动向谷仓建查件单"),
    ("eBay 消息", "ebay_push_enabled", "实时推送", "", False,
     "打开后真实买家来信会被自动处理、自动回复，物流问题还会自动向谷仓建查件单"),
    ("eBay 消息", "ebay_live_stores", "对接 eBay 的店铺", "店铺名或 ID，英文逗号分隔：只拉取这些店铺、只有这些店铺真实发到 eBay；留空 = 拉取全部、都不真实发送", False,
     "列进来的店铺，回复会真实发给 eBay 买家"),
    ("议价", "offer_live_stores", "可发报价的店铺", "店铺名或 ID，英文逗号分隔：这些店铺可以在「议价」页真实给感兴趣的买家发折扣报价；"
     "“对接 eBay 的店铺”本来就能发。只开议价，不会因此真实收发消息", False,
     "列进来的店铺，报价会真实发给 eBay 买家，发出后不能撤回"),
    ("数据同步", "ebay_sync_stores", "消息拉取范围", "* = 全部店铺；留空 = 只拉“对接 eBay 的店铺”；也可写店铺名逗号分隔。只影响拉取，真实发送仍只限对接店铺", False, None),
    ("数据同步", "ebay_sync_interval_seconds", "消息定时拉取间隔（秒）", "最少 60；有实时推送时这是兜底", False, None),
    ("数据同步", "ebay_sync_lookback_days", "消息首次拉取天数", "新对接或清空游标的店铺，第一次往回拉多少天", False, None),
    ("数据同步", "ebay_cases_sync_minutes", "售后与订单拉取间隔（分钟）", "咨询、纠纷、退货、付款纠纷、评价、取消、退款单和客户订单；0 = 暂停", False, None),
    ("数据同步", "best_offer_sync_minutes", "Best offer 拉取间隔（分钟）", "买家报价 48 小时过期；0 = 暂停", False, None),
    ("数据同步", "orders_sync_days", "订单首次拉取天数", "客户列表用的订单，第一次往回拉多少天", False, None),
    ("数据同步", "ship_notice_enabled", "发货通知汇总", "汇总已发货订单，生成待审核的发货通知", False, None),
    ("数据同步", "ship_notice_interval_minutes", "发货通知汇总间隔（分钟）", "", False, None),
    ("数据同步", "ship_notice_live_only", "发货通知只给对接店铺", "只给“对接 eBay 的店铺”生成；其余店铺发不出去", False, None),
    ("eBay 消息", "send_delay_seconds", "发送延迟（秒）", "发给买家的消息等这么久才发出，期间可以撤回；默认 300（5 分钟）", False, None),
]
KEYS = {f[1] for f in FIELDS}


def _type(key: str) -> str:
    ann = config.Settings.model_fields[key].annotation
    if key in SECRETS:
        return "secret"
    if ann is bool:
        return "bool"
    if ann is int:
        return "int"
    return "str"


def load(db: Session | None = None) -> dict:
    """把页面配置装进 get_settings()。启动时（bootstrap）和每次保存后调用。"""
    if db is None:
        with session_scope() as s:
            return load(s)
    values = {r.key: r.value for r in db.scalars(select(AppSetting)) if r.key in KEYS}
    try:
        config.Settings(**values)
    except ValidationError:  # 数据库里有坏值：不让它拖垮启动，整体回到 .env
        log.exception("页面配置有无效值，本次忽略页面配置")
        values = {}
    config.set_overrides(values)
    return values


def listing(db: Session) -> list[dict]:
    s = config.get_settings()
    rows = {r.key: r for r in db.scalars(select(AppSetting))}
    out = []
    for group, key, label, help_, restart, warn in FIELDS:
        row = rows.get(key)
        t = _type(key)
        value = getattr(s, key)
        item = {"group": group, "key": key, "env": key.upper(), "label": label, "help": help_, "type": t,
                "restart": restart, "warn": warn, "source": "page" if row else "env",
                "updated_by": row.updated_by if row else None,
                "updated_at": row.updated_at.isoformat(timespec="seconds") + "Z" if row and row.updated_at else None}
        if t == "secret":
            item["value"], item["is_set"] = None, bool(value)
        else:
            item["value"] = value
        if key == "auto_send_max_risk":
            item["choices"] = ["L0", "L1"]
        out.append(item)
    return out


class SettingsError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _clean(key: str, value: Any) -> Any:
    t = _type(key)
    if t == "bool":
        if not isinstance(value, bool):
            raise SettingsError(f"{key} 要填开或关")
        return value
    if t == "int":
        try:
            return int(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{key} 要填整数") from None
    v = "" if value is None else str(value).strip()
    return v or None


def save(db: Session, changes: dict[str, Any], actor: str) -> list[str]:
    """保存一组配置（键 → 新值；值为 None 的字符串项表示清空）。密码项传空表示不改。返回改动的键。"""
    unknown = set(changes) - KEYS
    if unknown:
        raise SettingsError("这些配置不能在页面上改：" + "、".join(sorted(unknown)))
    cleaned = {}
    for key, value in changes.items():
        if key in SECRETS and (value is None or str(value).strip() == ""):
            continue  # 密码留空 = 不改
        cleaned[key] = _clean(key, value)
    if key_risk := cleaned.get("auto_send_max_risk"):
        if key_risk not in ("L0", "L1"):
            raise SettingsError("自动发送最高风险只能是 L0 或 L1")
    current = {r.key: r.value for r in db.scalars(select(AppSetting)) if r.key in KEYS}
    try:
        config.Settings(**{**current, **cleaned})
    except ValidationError as e:
        raise SettingsError("配置值不合法：" + "；".join(err["msg"] for err in e.errors())) from None
    changed = []
    for key, value in cleaned.items():
        row = db.get(AppSetting, key)
        if row is not None and row.value == value:
            continue
        if row is None:
            row = AppSetting(key=key)
            db.add(row)
        row.value, row.updated_by, row.updated_at = value, actor[:64], utcnow()
        changed.append(key)
    if changed:
        db.add(AuditEvent(actor=actor[:64], action="APP_SETTINGS_CHANGED",
                          detail={k: ("***" if k in SECRETS else cleaned[k]) for k in changed}))
        db.flush()
        load(db)
    return changed


def reset(db: Session, key: str, actor: str) -> bool:
    """删掉页面配置，回到 .env 的值。"""
    row = db.get(AppSetting, key)
    if row is None:
        return False
    db.delete(row)
    db.add(AuditEvent(actor=actor[:64], action="APP_SETTING_RESET", detail={"key": key}))
    db.flush()
    load(db)
    return True
