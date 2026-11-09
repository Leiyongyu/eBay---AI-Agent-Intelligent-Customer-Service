"""消息模板：占位符、系统预定义模板（首次启动写入）、可见 / 可改权限、图片附件存取。

- 系统预定义：所有人可见，只有管理员能改；企业模板：所有人可见，管理员或创建人能改；个人模板：只有创建人可见可改。
- 占位符写成 {{buyer}} 这种形式，插入回复时再按会话和订单替换（替换在使用模板的地方做）。
- 图片附件存在 media 目录下的 templates 子目录，文件名随机，只认常见图片格式。
"""
from __future__ import annotations

import base64
import binascii
import re
import secrets
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import MessageTemplate

SCOPES = ("SYSTEM", "COMPANY", "PERSONAL")
USAGES = ("MESSAGE", "FEEDBACK")
MAX_CONTENT = 1800
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_ATTACHMENTS = 6

# 占位符：中文说明 → 键（和参考 ERP 一致）
PLACEHOLDERS = [
    ("买家ID", "buyer"), ("买家姓名", "buyerName"), ("平台账号", "sellerName"), ("收件人姓名", "receiverName"),
    ("付款时间", "paidTime"), ("发货时间", "deliverTime"), ("跟踪单号", "trackNumber"), ("物品编号", "itemNumber"),
    ("买家地址", "buyerAddress"), ("Listing标题", "itemTitle"), ("Listing价格", "itemPrice"), ("Listing运费", "shippingPrice"),
    ("Listing链接", "itemLink"), ("Listing属性", "itemAttribute"), ("处理时间", "handlingTime"), ("平台SKU", "sku"),
    ("订单编号", "orderNumber"), ("平台物流", "shippingCompany"), ("订单金额(含税)", "orderAmount"),
    ("联系电话", "receiverPhoneNumber"),
]

# 系统预定义模板（首次启动写入；之后管理员可改，不会被覆盖）
SYSTEM_TEMPLATES = [
    ("对节假日等可预测的邮递延误进行解释", "MESSAGE", ["物流"],
     "Dear {{buyer}},\nThank you for your purchase and prompt payment. China will be observing a public holiday from ____ to ____, "
     "and the postal service may be slower than usual during this period. Your item {{itemTitle}} has been shipped and is on its way. "
     "We appreciate your patience and understanding.\n\nBest regards,\n{{sellerName}}"),
    ("可以选择加入自动邮件系统中的详细的物流信息", "MESSAGE", ["物流"],
     "Dear {{buyer}},\nThank you for purchasing (item ID: {{itemNumber}}). Please note that this item is coming from our overseas "
     "warehouse. Your order {{orderNumber}} has been shipped with {{shippingCompany}}, tracking number: {{trackNumber}}.\n"
     "You can track your parcel on the carrier's website. If you have any questions, please feel free to contact us.\n\n"
     "Best regards,\n{{sellerName}}"),
    ("如何撰写Notice Left - 提醒买家及时领取包裹模板", "MESSAGE", ["物流"],
     "Dear {{buyer}},\nThanks for buying our item, and I'm the seller {{sellerName}}. We send this e-mail to you to confirm whether "
     "you have received the item. The tracking information ({{trackNumber}}) shows that the carrier left a notice because nobody was "
     "available to receive the parcel. Please collect it from your local post office as soon as possible, otherwise it may be "
     "returned to the sender.\n\nBest regards,\n{{sellerName}}"),
    ("特殊情况致物流延误沟通模板 - 自然灾害导致物流延误", "MESSAGE", ["物流"],
     "Dear {{buyer}},\nWe're sorry to inform you that your item delivery may be delayed due to ____ (severe weather / natural "
     "disaster) in the transit area. Your parcel {{trackNumber}} is safe and will continue its journey as soon as conditions allow. "
     "Thank you very much for your patience.\n\nBest regards,\n{{sellerName}}"),
    ("物品未收到沟通模板：提供物流最新状态", "MESSAGE", ["物流", "未收到货"],
     "Hello, dear {{buyer}},\nI've just tracked your parcel status and got the following latest information:\n"
     "Tracking No.: {{trackNumber}} ({{shippingCompany}})\nLatest status: ____\n\n"
     "The parcel is still on the way. Please kindly wait a few more days. If it still doesn't arrive, please contact us and we "
     "will solve it for you.\n\nBest regards,\n{{sellerName}}"),
    ("物品收到邮件模板 - 关于退货（买家同意退货）", "MESSAGE", ["退货"],
     "Dear {{buyer}},\nYes, we accept return or exchange. Please open a return request on eBay for order {{orderNumber}} and send the "
     "item back with the return label. Once we receive the item, we will issue the refund / send the replacement promptly.\n\n"
     "Best regards,\n{{sellerName}}"),
    ("请买家提供VIN确认适配", "MESSAGE", ["适配"],
     "Dear {{buyer}},\nThank you for your message. To make sure the part fits your car, could you please provide us with the VIN "
     "(Vehicle Identification Number) and a photo of your original part?\nYou can find the VIN at the bottom left corner of the "
     "windscreen or on your vehicle registration document.\n\nBest regards,\n{{sellerName}}"),
    ("感谢买家好评", "FEEDBACK", ["评价"],
     "Thank you for your positive feedback, {{buyer}}! We are glad you are happy with {{itemTitle}}. Welcome back anytime."),
    ("中差评沟通 - 主动提供解决方案", "FEEDBACK", ["评价"],
     "Dear {{buyer}}, we are sorry for your experience with order {{orderNumber}}. Please contact us through eBay messages and we "
     "will offer a replacement or refund to make it right."),
]


def seed_system(db: Session) -> int:
    """没有任何系统预定义模板时写入默认的几条（幂等）。"""
    if db.scalar(select(func.count(MessageTemplate.id)).where(MessageTemplate.scope == "SYSTEM")):
        return 0
    for title, usage, tags, content in SYSTEM_TEMPLATES:
        db.add(MessageTemplate(scope="SYSTEM", usage=usage, title=title, content=content, tags=tags,
                               created_by="系统", updated_by="系统"))
    db.flush()
    return len(SYSTEM_TEMPLATES)


# ------------------------------------------------------------------ 权限
def can_see(t: MessageTemplate, user) -> bool:
    return t.scope != "PERSONAL" or t.owner_id == user.id


def can_edit(t: MessageTemplate, user) -> bool:
    if user.role == "ADMIN":
        return t.scope != "PERSONAL" or t.owner_id == user.id
    if t.scope == "SYSTEM":
        return False
    return t.owner_id == user.id


# ------------------------------------------------------------------ 图片附件
_MAGIC = {b"\x89PNG": "png", b"\xff\xd8\xff": "jpg", b"GIF8": "gif", b"RIFF": "webp"}


class TemplateFileError(ValueError):
    pass


def files_root() -> Path:
    root = get_settings().media_root / "templates"
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_image(data_base64: str) -> str:
    """保存一张图片（base64 或 data URL），返回文件名。只收 PNG / JPG / GIF / WEBP，最大 5 MB。"""
    raw = data_base64.split(",", 1)[1] if data_base64.startswith("data:") else data_base64
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise TemplateFileError("图片数据不对")
    if len(data) > MAX_IMAGE_BYTES:
        raise TemplateFileError("图片不能超过 5 MB")
    ext = next((e for magic, e in _MAGIC.items() if data.startswith(magic)), None)
    if ext is None or (ext == "webp" and data[8:12] != b"WEBP"):
        raise TemplateFileError("只支持 PNG、JPG、GIF、WEBP 图片")
    name = f"{secrets.token_hex(12)}.{ext}"
    (files_root() / name).write_bytes(data)
    return name


def file_path(name: str) -> Path | None:
    """按文件名取图片路径；名字不合规或文件不存在返回 None（防路径穿越）。"""
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    root = files_root().resolve()
    p = (root / name).resolve()
    return p if p.parent == root and p.is_file() else None


# ------------------------------------------------------------------ 用到会话里：把占位符换成实际内容
_TOKEN = re.compile(r"\{\{\s*([A-Za-z]+)\s*\}\}")
_ORDER_KEYS = {"buyerName", "receiverName", "paidTime", "buyerAddress", "orderNumber", "orderAmount",
               "receiverPhoneNumber", "sku", "trackNumber", "shippingCompany", "deliverTime"}
_PACKAGE_KEYS = {"trackNumber", "shippingCompany", "deliverTime"}
_LISTING_KEYS = {"itemTitle", "itemPrice", "sku"}


def _date(iso_text: str | None) -> str | None:
    return (iso_text or "")[:10] or None


def render(db: Session, content: str, conv) -> tuple[str, list[str]]:
    """把 {{buyer}} 等换成这个会话的实际内容。只在模板用到时才查订单、包裹、刊登（都有缓存）。
    返回（替换后的正文, 没取到值、原样留着的占位符）。"""
    from .models import Store
    from .sources.ebay import item_url

    keys = set(_TOKEN.findall(content))
    if not keys:
        return content, []
    store = db.get(Store, conv.store_id)
    values: dict[str, str | None] = {
        "buyer": conv.buyer_key, "itemNumber": conv.item_id,
        "sellerName": (store.ebay_user_id or store.name) if store else None,
        "itemLink": item_url(conv.item_id, conv.marketplace_id) if conv.item_id else None,
        "itemTitle": conv.conversation_title,
    }
    if keys & _LISTING_KEYS and conv.item_id:
        try:
            from .sources import get_source_for_store

            listing = get_source_for_store(conv.store_id).get_listing(conv.store_id, conv.item_id) or {}
            price = listing.get("price") or {}
            values["itemTitle"] = listing.get("title") or values["itemTitle"]
            values["itemPrice"] = f"{price.get('value')} {price.get('currency') or ''}".strip() if price.get("value") else None
            values["sku"] = listing.get("sku")
        except Exception:  # noqa: BLE001 — 查不到就留着占位符让客服自己填
            pass
    if keys & _ORDER_KEYS:
        try:
            from . import customer_panel

            orders = customer_panel.store_orders(conv.store_id, conv.buyer_key)
            o = next((o for o in orders if conv.item_id and any(i.get("item_id") == conv.item_id for i in o["items"])), None) \
                or (orders[0] if orders else None)
            if o:
                ship = o.get("ship_to") or {}
                line = next((i for i in o["items"] if i.get("item_id") == conv.item_id), o["items"][0] if o["items"] else {})
                total = o.get("total") or {}
                values.update({
                    "orderNumber": o.get("order_id"), "paidTime": _date(o.get("paid_at")),
                    "buyerName": ship.get("name"), "receiverName": ship.get("name"),
                    "buyerAddress": ", ".join(x for x in (ship.get("address"), ship.get("country")) if x) or None,
                    "receiverPhoneNumber": ship.get("phone"),
                    "orderAmount": f"{total.get('value')} {total.get('currency') or ''}".strip() if total.get("value") else None,
                    "sku": line.get("sku") or values.get("sku"),
                })
                if keys & _PACKAGE_KEYS and o.get("order_id"):
                    pkgs = customer_panel.packages(conv.store_id, o["order_id"])
                    p = next((p for p in pkgs if p.get("tracking_number")), None)
                    if p:
                        values.update({"trackNumber": p.get("tracking_number"), "shippingCompany": p.get("carrier"),
                                       "deliverTime": _date(p.get("event_time"))})
        except Exception:  # noqa: BLE001
            pass
    missing: list[str] = []

    def sub(m: re.Match) -> str:
        v = values.get(m.group(1))
        if v:
            return str(v)
        if m.group(1) not in missing:
            missing.append(m.group(1))
        return m.group(0)

    return _TOKEN.sub(sub, content), missing
