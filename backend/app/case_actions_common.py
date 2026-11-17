"""售后单（咨询 / 退货 / 纠纷，以及 2 号的取消交易、退款单）在本系统里做 eBay 写操作的共用部分。

- 能不能做：只对接 eBay 真实收发的店铺（EBAY_LIVE_STORES）、发送渠道是 eBay（EBAY_CHANNEL=ebay）时；
- 角色：senior_actions 里的处理（退款、接受退货、升级、申诉等）只有管理员和售后能做；
- 调 eBay：Post-Order API 用店铺自己的令牌（IAF）；超时 / 连接中断 / 5xx 不重试（不确定 eBay 有没有收到），
  4xx 带回 eBay 的说明；
- 做成以后（record_action）：审计 CASE_ACTION、处理备注（ebay_case_notes，kind=ACTION）、售后单处理状态，
  往这个买家的会话写系统事件（source=case_action，下一次 AI 起草从会话状态卡里看到“已在 eBay 上做的处理”），
  同步会话的售后状态 / 备注；提交后由接口层调 refresh_reasons 重算这张订单的售后原因；
- 给买家发信不在这里直接发：走发件箱（queue_message → sending.service.simulate_send），合规检查、审核记录、
  5 分钟可撤回都和工作台一样，到期后发件箱改调 Post-Order 的 send_message（post_order_send）。
"""
from __future__ import annotations

import base64
import binascii
import logging
from datetime import datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import utcnow
from .models import AuditEvent, Conversation, ConversationEvent, EbayCase, EbayCaseNote, Role, Store

log = logging.getLogger(__name__)

SENIOR_ROLES = (Role.ADMIN, Role.AFTERSALES)
MAX_TEXT = 1000
MAX_FILE_BYTES = 8 * 1024 * 1024  # 上传给 eBay 的单个文件（图片 / PDF）
EVENT_SOURCE = "case_action"  # 写进会话的系统事件来源：会话状态卡按它取“已在 eBay 上做的处理”
KIND_LABEL = {"INQUIRY": "未收到货咨询", "RETURN": "退货", "CASE": "纠纷", "CANCEL": "取消交易", "REFUND": "退款单"}


class ActionError(Exception):
    """处理失败：status_code 给接口层用（403 不能做 / 409 状态不对 / 422 参数或 eBay 拒绝 / 502 不确定）。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status_code, self.message = status, message
        self.signature_code: str | None = None  # eBay 拒绝数字签名时的错误码（215xxx），接口层记审计和用量
        self.endpoint: str | None = None
        self.called_ebay = False  # 请求已经发到 eBay（eBay 拒绝 / 超时 / 5xx）；False = 发出之前就挡下了


# ------------------------------------------------------------------ 能不能做
def cannot_act(store: Store | None) -> str | None:
    """不能在本系统处理的原因；None = 可以。"""
    from .sending.channel import is_live_store

    if get_settings().ebay_channel != "ebay":
        return "发送渠道不是 eBay（EBAY_CHANNEL=mock），不能在系统里处理，请到 eBay 后台"
    if not is_live_store(store):
        return "这个店铺没有对接 eBay 真实收发（不在“对接 eBay 的店铺”里），请到 eBay 后台处理"
    return None


def allowed(user, action: str, senior_actions) -> bool:
    return action not in senior_actions or (user is not None and user.role in SENIOR_ROLES)


def check(db: Session, case: EbayCase, kind: str, action: str, actions: dict, senior_actions, user,
          *, open_required: bool = True) -> None:
    """通用前置检查：类型、处理名、角色、店铺、是否还开着。"""
    if case.kind != kind:
        raise ActionError(422, f"只有{KIND_LABEL.get(kind, kind)}可以在这里处理")
    if action not in actions:
        raise ActionError(422, "没有这种处理")
    if not allowed(user, action, senior_actions):
        raise ActionError(403, f"{actions[action]}只有管理员和售后可以做")
    reason = cannot_act(db.get(Store, case.store_id))
    if reason:
        raise ActionError(403, reason)
    if open_required and not case.is_open:
        raise ActionError(409, f"这条{KIND_LABEL.get(kind, '售后单')}在 eBay 上已经关闭")


def who(user) -> str:
    return ((user.display_name if user else None) or "system")[:64]


# ------------------------------------------------------------------ 字段
def dt(x) -> str | None:
    v = (x or {}).get("value") if isinstance(x, dict) else x
    return str(v) if v else None


def amount(x) -> dict | None:
    if not isinstance(x, dict) or x.get("value") is None:
        return None
    try:
        return {"value": round(float(x["value"]), 2), "currency": x.get("currency")}
    except (TypeError, ValueError):
        return None


def text(v: str | None, field: str, required: bool = False, max_len: int = MAX_TEXT) -> str | None:
    t = (v or "").strip()
    if required and not t:
        raise ActionError(422, f"请填写{field}")
    if len(t) > max_len:
        raise ActionError(422, f"{field}最多 {max_len} 个字")
    return t or None


def iso_date(v, field: str = "时间") -> dict | None:
    """前端给的时间 → eBay 的 {value: 2026-11-05T17:51:22.000Z}；没给返回 None。"""
    if not v:
        return None
    try:
        at = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        raise ActionError(422, f"{field}格式不对") from None
    return {"value": at.strftime("%Y-%m-%dT%H:%M:%S.000Z")}


def file_data(data: str | None, name: str | None) -> tuple[str, str]:
    """前端上传的文件（base64，可带 data URL 前缀）→ (纯 base64, 文件名)；校验能解码、不超过大小。"""
    raw = (data or "").split(",", 1)[-1].strip() if data and data.startswith("data:") else (data or "").strip()
    if not raw:
        raise ActionError(422, "请选择文件")
    try:
        size = len(base64.b64decode(raw, validate=True))
    except (binascii.Error, ValueError):
        raise ActionError(422, "文件内容读不出来，请重新选择") from None
    if size > MAX_FILE_BYTES:
        raise ActionError(422, f"文件超过 {MAX_FILE_BYTES // 1024 // 1024} MB")
    fname = text(name, "文件名", True, 200)
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    if ext not in ("jpg", "jpeg", "png", "gif", "bmp", "pdf"):
        raise ActionError(422, "eBay 只收图片（JPG、PNG、GIF、BMP）或 PDF")
    return raw, fname


SITE_DOMAIN = {"EBAY_GB": "ebay.co.uk", "EBAY_DE": "ebay.de", "EBAY_FR": "ebay.fr", "EBAY_IT": "ebay.it", "EBAY_ES": "ebay.es",
               "EBAY_AT": "ebay.at", "EBAY_CH": "ebay.ch", "EBAY_IE": "ebay.ie", "EBAY_NL": "ebay.nl", "EBAY_BE": "befr.ebay.be",
               "EBAY_PL": "ebay.pl", "EBAY_AU": "ebay.com.au", "EBAY_CA": "ebay.ca", "EBAY_US": "ebay.com"}
NO_API = "eBay 没有开放接口，请到 eBay 后台处理"


def ebay_url(store: Store | None, *candidates: str | None) -> str:
    """做不了的处理跳到 eBay 后台：优先用 eBay 自己给的链接（退货的 actionURL、纠纷的处理中心链接），
    没有就用店铺站点的 eBay 纠纷处理中心。"""
    for c in candidates:
        if c and str(c).startswith("https://"):
            return str(c)
    return f"https://resolutioncenter.{SITE_DOMAIN.get((store.default_marketplace_id if store else '') or '', 'ebay.com')}/"


def marketplace(store_id: str) -> str | None:
    from .db import session_scope

    with session_scope() as db:
        s = db.get(Store, store_id)
        return s.default_marketplace_id if s else None


# ------------------------------------------------------------------ 调 eBay
def get_detail(store_id: str, path: str, api=None, params: dict | None = None) -> dict:
    """实时读一条 Post-Order 记录（只读）。"""
    from .sources.ebay import EbayReadOnlyApi

    api = api or EbayReadOnlyApi(store_id)
    d = api.get(path, params, auth="IAF", marketplace=marketplace(store_id))
    if not d:
        raise ActionError(404, "eBay 上找不到这条记录")
    return d


def post(store_id: str, path: str, body: dict, http: httpx.Client | None = None) -> dict:
    """POST 到 eBay（真实写操作）。成功返回 eBay 的响应体（没有则 {}）；不重试。
    配置了 EBAY_SIGNING_* 时带数字签名（EU/UK 注册卖家的退款、处理退货等要求）；签名算的是实际发出的字节。"""
    import json

    from .ebay.auth import READ_SCOPE, EbayAuthError, for_store
    from .ebay.signing import SigningError, sign_headers, signature_error, signature_error_code

    url = get_settings().ebay_api_base + path
    mk = marketplace(store_id)
    try:
        token = for_store(store_id, READ_SCOPE).access_token()
    except EbayAuthError as e:
        raise ActionError(502, f"拿不到店铺令牌：{e}") from None
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        sig = sign_headers("POST", url, raw)  # 没配置签名密钥时为 {}
    except SigningError as e:
        raise ActionError(500, f"数字签名配置有误：{e}") from None
    headers = {"Authorization": f"IAF {token}", "Content-Type": "application/json", "Accept": "application/json",
               **({"X-EBAY-C-MARKETPLACE-ID": mk} if mk else {}), **sig}
    def sent(err: ActionError) -> ActionError:
        err.called_ebay, err.endpoint = True, err.endpoint or path
        return err

    try:
        resp = (http or httpx.Client(timeout=60)).post(url, content=raw, headers=headers)
    except httpx.HTTPError as e:  # 不确定 eBay 有没有收到：不重试
        raise sent(ActionError(502, f"请求 eBay 出错（{type(e).__name__}），不确定是否已处理，请到 eBay 后台核对")) from None
    if resp.status_code in (200, 201, 202, 204):
        try:
            return resp.json() if resp.content else {}
        except ValueError:
            return {}
    if resp.status_code >= 500:
        raise sent(ActionError(502, f"eBay 返回 HTTP {resp.status_code}，不确定是否已处理，请到 eBay 后台核对"))
    hint = signature_error(resp.text)
    if hint:
        err = ActionError(422, hint)
        err.signature_code, err.endpoint = signature_error_code(resp.text) or "215xxx", path
        raise sent(err)
    try:
        j = resp.json()
        errs = j.get("error") or j.get("errors") or []
        msg = "；".join(str(e.get("message") or e.get("longMessage") or e) for e in errs)[:300]
    except ValueError:
        msg = resp.text[:300]
    raise sent(ActionError(422, f"eBay 没有接受（HTTP {resp.status_code}）：{msg or '无说明'}"))


# ------------------------------------------------------------------ 订单退款情况（退款前核对）
PAYMENT_STATUS = {"PAID": "已付款", "PARTIALLY_REFUNDED": "已部分退款", "FULLY_REFUNDED": "已全额退款",
                  "PENDING": "付款处理中", "FAILED": "付款失败"}


def order_refunds(o: dict) -> dict:
    """eBay 订单（getOrder）上的付款状态和已退款记录：订单级 paymentSummary.refunds + 每个商品的 lineItems[].refunds。"""
    items = []
    for r in (o.get("paymentSummary") or {}).get("refunds") or []:
        items.append({"amount": amount(r.get("amount")), "at": r.get("refundDate"), "status": r.get("refundStatus"),
                      "line": None})
    for ln in o.get("lineItems") or []:
        for r in ln.get("refunds") or []:
            items.append({"amount": amount(r.get("amount")), "at": r.get("refundDate"), "status": r.get("refundStatus"),
                          "line": ln.get("sku") or ln.get("legacyItemId")})
    st = o.get("orderPaymentStatus")
    return {"payment_status": st, "payment_status_label": PAYMENT_STATUS.get(st or "", st), "refunds": items}


def order_refund_state(store_id: str, order_id: str | None) -> dict | None:
    """退款前实时查订单的付款状态（只读）；没有订单号返回 None。"""
    if not order_id:
        return None
    from .sources import get_ebay_source

    o = get_ebay_source().invoice_order(store_id, order_id)
    return order_refunds(o) if o else None


def ensure_not_refunded(store_id: str, order_id: str | None, fetch=None) -> dict | None:
    """退款前实时查订单：已全额退款 → 409；查不到（不确定退没退过）→ 502；返回订单退款情况（没有订单号为 None）。"""
    try:
        st = (fetch or order_refund_state)(store_id, order_id)
    except Exception as e:  # noqa: BLE001
        raise ActionError(502, f"查不到订单的退款情况（{str(e)[:120]}），请稍后再试或到 eBay 后台处理") from None
    if st and st["payment_status"] == "FULLY_REFUNDED":
        raise ActionError(409, "eBay 显示这笔订单已经全额退款，不能再退")
    return st


# ------------------------------------------------------------------ 做成以后
def conversation_for(db: Session, case: EbayCase) -> Conversation | None:
    """这张售后单的买家会话（同店铺、同买家；多条取最近有消息的）。"""
    if not case.buyer:
        return None
    return db.scalar(select(Conversation).where(Conversation.store_id == case.store_id,
                                                func.lower(Conversation.buyer_key) == case.buyer.lower(),
                                                Conversation.conversation_type != "FROM_EBAY")
                     .order_by(Conversation.last_message_at.desc(), Conversation.id.desc()).limit(1))


def record_action(db: Session, case: EbayCase, *, action: str, label: str, summary: str, user,
                  work_state: str | None = "PROCESSING", aftersales_done: bool = False) -> Conversation | None:
    """写操作成功后：审计、处理备注、售后单处理状态、会话系统事件、会话售后状态 / 备注。返回关联的会话。"""
    actor, now = who(user), utcnow()
    db.add(AuditEvent(actor=actor, action=f"{case.kind}_{action.upper()}", store_id=case.store_id,
                      detail={"case_id": case.id, "kind": case.kind, "external_id": case.external_id, "summary": summary}))
    db.add(EbayCaseNote(case_id=case.id, store_id=case.store_id, text=f"[{label}] {summary}"[:4000], kind="ACTION",
                        created_by=actor, created_at=now))
    if work_state:
        case.work_state = work_state
    case.worked_by, case.worked_at = actor, now
    conv = conversation_for(db, case)
    if conv is not None:
        kl = KIND_LABEL.get(case.kind, case.kind)
        db.add(ConversationEvent(store_id=conv.store_id, conversation_id=conv.id, event_type=f"SELLER_{case.kind}_{action.upper()}",
                                 source=EVENT_SOURCE, case_id=case.external_id,
                                 detail=f"{kl} {case.external_id}：{summary}"[:1000], occurred_at=now))
        conv.row_version += 1  # 系统事件：之前生成的草稿过期，下一次起草能看到这一步
        conv.aftersales_status = "DONE" if aftersales_done else "IN_PROGRESS"
        conv.aftersales_type = conv.aftersales_type or kl[:32]
        line = f"[{now.strftime('%m-%d %H:%M')} {actor}] {kl} {case.external_id}：{summary}"
        conv.aftersales_note = (f"{conv.aftersales_note}\n{line}" if conv.aftersales_note else line)[-4000:]
        conv.aftersales_handled_by = conv.aftersales_handled_by or actor
        conv.aftersales_updated_at = now
    return conv


def record_failure(case: EbayCase, action: str, e: ActionError, who_: str) -> None:
    """请求已经发到 eBay 但没成功（e.called_ebay：eBay 拒绝 / 超时 / 5xx）：独立事务记审计 {类型}_{处理}_FAILED
    和一条处理备注，接口层之后回滚也删不掉。超时 / 5xx（502）是“已尝试、结果不确定”：不重试，备注提示到 eBay 后台核对。
    detail：case_id、external_id、endpoint、status、error、uncertain、signature_error_code（有的话）。失败只记日志。"""
    from .db import session_scope

    uncertain = e.status_code == 502
    try:
        with session_scope() as s:
            s.add(AuditEvent(actor=who_[:64], action=f"{case.kind}_{action.upper()}_FAILED", store_id=case.store_id,
                             detail={"case_id": case.id, "external_id": case.external_id, "endpoint": e.endpoint,
                                     "status": e.status_code, "error": e.message[:500], "uncertain": uncertain,
                                     **({"signature_error_code": e.signature_code} if e.signature_code else {})}))
            s.add(EbayCaseNote(case_id=case.id, store_id=case.store_id, kind="ACTION", created_by=who_[:64], created_at=utcnow(),
                               text=f"[{'结果不确定' if uncertain else '没有成功'}] {action}：{e.message}"[:4000]))
    except Exception:  # noqa: BLE001
        log.exception("记处理失败审计出错：%s %s", case.id, action)


def mark_order_refunded(db: Session, case: EbayCase) -> None:
    """在 eBay 上退了款（咨询全额退款等不经过退款申请单的）：把本地订单标成有退款，提交后重算售后原因时马上能看到，
    不用等财务流水同步。订单号可以是新格式，或咨询 / 纠纷上的 商品号-交易号（按订单行对上）。"""
    from .models import EbayOrder, EbayOrderLine

    if not case.order_id:
        return
    oid = db.scalar(select(EbayOrderLine.order_id).where(
        EbayOrderLine.store_id == case.store_id,
        (EbayOrderLine.line_key == case.order_id) | (EbayOrderLine.legacy_order_id == case.order_id)
        | (EbayOrderLine.order_id == case.order_id)).limit(1)) or case.order_id
    o = db.scalar(select(EbayOrder).where(EbayOrder.store_id == case.store_id, EbayOrder.order_id == oid))
    if o is not None:
        o.refunded = True


def refresh_reasons(store_id: str) -> None:
    """提交后重算这个店铺订单的售后原因（eBay 状态变了、会话售后状态变了）；失败只记日志。"""
    try:
        from .aftersales_reasons import refresh_store

        refresh_store(store_id)
    except Exception as e:  # noqa: BLE001
        log.warning("重算售后原因失败（%s）：%s", store_id, e)


def recent_actions(db: Session, conv: Conversation, days: int = 60, limit: int = 6) -> list[str]:
    """会话状态卡用：最近在 eBay 上做过的处理（系统事件），从早到晚。"""
    from datetime import timedelta

    rows = db.scalars(select(ConversationEvent).where(ConversationEvent.conversation_id == conv.id,
                                                      ConversationEvent.source == EVENT_SOURCE,
                                                      ConversationEvent.occurred_at >= utcnow() - timedelta(days=days))
                      .order_by(ConversationEvent.occurred_at.desc(), ConversationEvent.id.desc()).limit(limit)).all()
    return [f"{e.occurred_at.strftime('%Y-%m-%d')} {(e.detail or '')[:120]}" for e in reversed(rows)]


# ------------------------------------------------------------------ 给买家发信（走发件箱）
def queue_message(db: Session, case: EbayCase, path: str, message: str, user, *, text_zh: str | None = None,
                  handling_ms: int | None = None) -> dict:
    """放进发件箱：合规检查（BLOCK 不能发）、审核记录、延迟发送可撤回；到期后改调 Post-Order send_message（path）。"""
    from .sending.service import SendError, simulate_send

    body = text(message, "发信内容", True)
    conv = conversation_for(db, case)
    if conv is None:
        raise ActionError(409, "这张售后单对不上买家会话，不能走发件箱发信，请到 eBay 后台给买家留言")
    meta = {"channel": "post_order", "post_order_path": path, "case_id": case.id, "case_kind": case.kind,
            "case_external_id": case.external_id}
    try:
        ob, _ = simulate_send(db, conv=conv, text=body, actor=who(user), handling_ms=handling_ms, text_zh=text_zh,
                              outbox_meta=meta)
    except SendError as e:
        raise ActionError(e.status_code, e.message) from None
    db.add(EbayCaseNote(case_id=case.id, store_id=case.store_id, kind="ACTION", created_by=who(user), created_at=utcnow(),
                        text=f"[给买家发信] 已放进发件箱（{ob.send_after.strftime('%H:%M')} 后发出，之前可在会话里撤回）：{body[:60]}"))
    return {"outbound_id": ob.id, "state": ob.state, "send_after": ob.send_after.isoformat() + "Z",
            "conversation_id": conv.id}


def post_order_send(db: Session, conv: Conversation, text_: str, meta: dict, http: httpx.Client | None = None) -> str:
    """发件箱到期后调用：把消息发到 Post-Order 的 send_message。返回本地消息 ID（eBay 不返回消息 ID）。
    不能发 → ChannelNotConfigured（明确没发出）；超时 / 5xx → ChannelUncertain（不重发，要核对）。"""
    import uuid

    from .sending.channel import ChannelNotConfigured, ChannelUncertain

    reason = cannot_act(db.get(Store, conv.store_id))
    if reason:
        raise ChannelNotConfigured(reason)
    try:
        post(conv.store_id, meta["post_order_path"], {"message": {"content": text_}}, http)
    except ActionError as e:
        if e.status_code == 502 and "不确定" in e.message:
            raise ChannelUncertain(e.message) from None
        raise ChannelNotConfigured(e.message) from None
    case = db.get(EbayCase, meta.get("case_id")) if meta.get("case_id") else None
    if case is not None:  # 消息本身已写进会话：这里只记处理备注和审计，不再写会话事件
        db.add(EbayCaseNote(case_id=case.id, store_id=case.store_id, kind="ACTION", created_by="send_service",
                            created_at=utcnow(), text=f"[给买家发信] 已发到 eBay：{text_[:60]}"))
        db.add(AuditEvent(actor="send_service", action=f"{case.kind}_MESSAGE", store_id=case.store_id,
                          conversation_id=conv.id, detail={"case_id": case.id, "external_id": case.external_id}))
        if case.work_state != "DONE":
            case.work_state = "PROCESSING"
    return f"PO-{(meta.get('case_kind') or 'CASE')[:3]}-{uuid.uuid4().hex[:12]}"
