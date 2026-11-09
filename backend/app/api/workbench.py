"""人工工作台接口（第 5 章、第 13 章“内部 API”）。"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session, defer

from ..config import get_settings
from ..db import get_db, utcnow
from ..guards.compliance import ComplianceChecker
from ..knowledge.retriever import estimate_tokens, exclusion_reason
from ..media.service import attachments_for_messages, ensure_stored
from ..media.service import file_path as media_file_path
from ..models import (
    Aftersales,
    Role,
    AgentRun,
    AgentStep,
    AttachmentStatus,
    AuditEvent,
    Conversation,
    ConversationEvent,
    Draft,
    EbayListing,
    ImageObservation,
    JobState,
    KnowledgeChunk,
    Message,
    MessageAttachment,
    ConversationMark,
    Invoice,
    InvoiceRequest,
    OutboundMessage,
    InvoiceStatus,
    MessageTag,
    ReplyJob,
    ReplyJobSource,
    ReviewFeedback,
    SendIntent,
    Store,
    ToolCall,
    WorkClaim,
    LogisticsTicket,
)
from ..logistics.followup import CLOSED as CLOSED_TICKET
from ..logistics.followup import (DRAFT_PREFIX, TicketError, discard_draft, edit_draft, is_draft, is_local_draft,
                                  reship_stock, send_submit_notice, submit_draft)
from ..logistics.goodcang import GoodcangClient, GoodcangError
from ..providers.chat import get_chat_provider
from ..sending.service import AUTO_ACTOR, SIMULATED_PREFIX, SendError, is_ack, request_send, simulate_send
from .. import usage as usage_log
from ..worker import worker
from ..sources import get_source_for_store
from ..sources.ebay import item_url
from ..guards.language import mostly_chinese
from ..read_state import mark_read, mark_unread, unread_count, unread_count_expr
from ..auth import allowed_store_ids, check_store, current_user, require_admin
from ..auth import current as current_account
from .deps import iso, optional_reviewer, reviewer_name


log = logging.getLogger(__name__)
router = APIRouter(prefix="/internal", tags=["workbench"])

GROUPS = {
    "pending": JobState.PENDING_REVIEW,
    "processing": (JobState.RECEIVED, JobState.RUNNING, JobState.RETRYABLE_ERROR),
    "sending": (JobState.SENDING, JobState.SEND_UNCERTAIN),
    "resolved": (JobState.SENT, JobState.RESOLVED_NO_REPLY),
}


# ------------------------------------------------------------------ 序列化
def _current_job(db: Session, conv_id: int) -> ReplyJob | None:
    return db.scalar(select(ReplyJob).where(ReplyJob.conversation_id == conv_id,
                                            ReplyJob.state != JobState.CANCELLED_STALE)
                     .order_by(ReplyJob.id.desc()).limit(1))


def _active_claim(db: Session, conv: Conversation) -> WorkClaim | None:
    claim = db.scalar(select(WorkClaim).where(WorkClaim.store_id == conv.store_id,
                                              WorkClaim.conversation_id == conv.id))
    return claim if claim and claim.expires_at > utcnow() else None


STAGES = ("presale", "shipping", "aftersales", "invoice", "other")


def _situation_dict(conv: Conversation) -> dict | None:
    if not conv.situation_stage and not conv.situation_label:
        return None
    return {"stage": conv.situation_stage or "other", "label": conv.situation_label, "at": iso(conv.situation_at)}


def _claim_dict(claim: WorkClaim | None) -> dict | None:
    return {"claimed_by": claim.claimed_by, "expires_at": iso(claim.expires_at)} if claim else None


def _job_dict(job: ReplyJob | None) -> dict | None:
    if job is None:
        return None
    return {
        "id": job.id, "state": job.state, "risk_level": job.risk_level, "run_generation": job.run_generation,
        "decision_reasons": job.decision_reasons or [], "ready_at": iso(job.ready_at), "attempt": job.attempt,
        "last_error": job.last_error, "created_at": iso(job.created_at), "updated_at": iso(job.updated_at),
        "rule_version": job.rule_version, "model_version": job.model_version,
    }


def _draft_dict(d: Draft) -> dict:
    return {
        "id": d.id, "job_id": d.job_id, "run_generation": d.run_generation, "version": d.version,
        "author": d.author, "status": d.status, "conversation_row_version": d.conversation_row_version,
        "text": d.text, "text_zh": d.text_zh, "reasoning_zh": d.reasoning_zh, "language": d.language, "intent": d.intent, "intents": d.intents or [],
        "action": d.action, "risk_level": d.risk_level, "decision": d.decision, "claims": d.claims or [],
        "evidence_ids": d.evidence_ids or [], "missing_facts": d.missing_facts or [], "conflicts": d.conflicts or [],
        "review_reasons": d.review_reasons or [], "compliance": d.compliance_result,
        "prompt_version": d.prompt_version, "model_config_version": d.model_config_version,
        "knowledge_versions": d.knowledge_versions or [], "token_usage": d.token_usage, "created_at": iso(d.created_at),
    }


def _load(db: Session, model, obj_id):
    obj = db.get(model, obj_id)
    if obj is None:
        raise HTTPException(404, f"{model.__tablename__} {obj_id} 不存在")
    # 店铺范围：客服、售后只能碰分配给自己的店铺（不在范围内一律当作不存在）
    if model is Store:
        check_store(obj.id)
    elif hasattr(obj, "store_id"):
        check_store(obj.store_id)
    return obj


def _scope(query, column):
    """列表查询按当前账号负责的店铺过滤（管理员、全部店铺账号不过滤）。"""
    allowed = allowed_store_ids()
    return query if allowed is None else query.where(column.in_(allowed))


def _ensure_claim(db: Session, conv: Conversation, reviewer: str) -> None:
    claim = _active_claim(db, conv)
    if claim and claim.claimed_by != reviewer:
        raise HTTPException(409, f"会话已被 {claim.claimed_by} 认领（{iso(claim.expires_at)} 到期），当前只读")


def _send_error(e: SendError):
    raise HTTPException(e.status_code, {"message": e.message, **e.detail})


# ------------------------------------------------------------------ 列表与详情
@router.get("/stores")
def list_stores(db: Session = Depends(get_db)):
    # 真实 eBay 店铺（名称取自 eBay 原始店铺名，见 store_credentials）按名称排在前，演示店铺排在后
    stores = sorted(db.scalars(_scope(select(Store), Store.id)), key=lambda s: (s.ebay_user_id is None, s.name.lower()))
    unread = unread_count_expr()
    counts: dict[tuple[str, bool], int] = {}  # (店铺, 是否 eBay 系统会话) → 有未读的会话数
    for store_id, ctype, n in db.execute(select(Conversation.store_id, Conversation.conversation_type,
                                                func.count(Conversation.id))
                                         .where(unread > 0).group_by(Conversation.store_id, Conversation.conversation_type)):
        key = (store_id, ctype == SYSTEM_TYPE)
        counts[key] = counts.get(key, 0) + n
    return [{"id": s.id, "name": s.name, "default_marketplace_id": s.default_marketplace_id,
             "timezone": s.timezone, "auto_send_enabled": s.auto_send_enabled, "ebay_user_id": s.ebay_user_id,
             "unread_conversations": counts.get((s.id, False), 0), "unread_system": counts.get((s.id, True), 0)}
            for s in stores]


@router.get("/auto-reply/settings")
def auto_reply_settings():
    """AI 自动回复的全局设置（只读，改 .env）：总开关、可自动发的意图、只索要资料时可自动发的意图。"""
    s = get_settings()
    return {"auto_send_enabled": s.auto_send_enabled, "intents": sorted(s.auto_send_intent_set),
            "clarify_intents": sorted(s.auto_send_clarify_intent_set),
            "listing_intents": sorted(s.auto_send_listing_intent_set),
            "exclude_intents": sorted(s.auto_send_exclude_intent_set), "max_risk": s.auto_send_max_risk,
            "evidence": s.auto_send_evidence, "max_streak": s.auto_reply_max_streak}


class AutoReplyBody(BaseModel):
    enabled: bool


@router.post("/stores/{store_id}/auto-reply", dependencies=[Depends(require_admin)])
def set_store_auto_reply(store_id: str, body: AutoReplyBody, reviewer: str = Depends(reviewer_name),
                         db: Session = Depends(get_db)):
    """店铺“AI 自动回复”开关：新买家来信自动交给 AI；有知识库依据、低风险的自动（模拟）发出，其余进待审核。"""
    store = _load(db, Store, store_id)
    if store.auto_send_enabled != body.enabled:
        store.auto_send_enabled = body.enabled
        db.add(AuditEvent(actor=reviewer, action="STORE_AUTO_REPLY_ON" if body.enabled else "STORE_AUTO_REPLY_OFF",
                          store_id=store.id, detail={}))
    return {"store_id": store.id, "auto_send_enabled": store.auto_send_enabled}


SYSTEM_TYPE = "FROM_EBAY"  # eBay 系统消息会话（退货、退款、取消等通知）


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@router.get("/conversations")
def list_conversations(store_id: str | None = None, group: str = "pending", q: str | None = None,
                       unread_only: bool = False, kind: str | None = None, tag_id: int | None = None,
                       view: str = "all", stage: str | None = None, read_only: bool = False,
                       buyers: str | None = None, buyer_match: str = "contains", store_ids: str | None = None,
                       site: str | None = None, starred: bool = False, order_no: str | None = None,
                       tracking_no: str | None = None, sku: str | None = None, tag_ids: str | None = None,
                       content: str | None = None, assignee: str | None = None, last_replier: str | None = None,
                       draft: str | None = None, first_from: str | None = None, first_to: str | None = None,
                       last_from: str | None = None, last_to: str | None = None,
                       reviewer: str = Depends(reviewer_name),
                       db: Session = Depends(get_db)):
    """kind：buyer 买家消息（含测试会话）/ system eBay 系统消息 / 空为全部。
    group 另有 failed 发送失败 / unassigned 暂未分配（没人认领）/ mine 我认领的（左侧菜单的消息箱）。
    view：买家消息列表顶部的页签——pending 待审核（当前任务需要人工审核，客服平时只看这个）/
    auto 自动回复（AI 已自动发出，供抽查）/ all 全部；在此基础上再按 group、tag_id、stage（会话阶段）细分。

    筛选（都可组合）：buyers 客户ID（多个用逗号或换行分隔）+ buyer_match prefix 开头 / contains 包含 / exact 完全一致；
    store_ids eBay 账号（店铺 ID，逗号分隔）；site 站点（EBAY_DE 等）；starred 只看星标；order_no 订单编号；tracking_no 货运单号；
    sku（eBay 上的 SKU）；tag_ids 标签（逗号分隔，任一）；content 消息内容（原文或中文译文）；assignee 负责人（认领人，unassigned = 没人认领）；
    last_replier 最近回复人（发出最后一条店铺消息的人；“eBay 后台”= 直接在 eBay 回复的）；draft has / none 有没有 AI 草稿；
    first_from / first_to 最初消息时间、last_from / last_to 最新消息时间（ISO 日期或时间，UTC）。
    订单编号、货运单号、SKU 按本地订单汇总（ebay_orders）、查件单和消息正文对到会话。"""
    unread = unread_count_expr()
    query = select(Conversation, unread).order_by(Conversation.last_message_at.desc().nulls_last(),
                                                  Conversation.id.desc()).limit(300)
    query = _scope(query, Conversation.store_id)
    if store_id:
        query = query.where(Conversation.store_id == store_id)
    if kind == "system":
        query = query.where(Conversation.conversation_type == SYSTEM_TYPE)
    elif kind == "buyer":
        query = query.where(Conversation.conversation_type != SYSTEM_TYPE)
    if q and q.strip():  # 买家消息按买家 eBay 用户名、系统消息按标题模糊搜索，不区分大小写
        field = Conversation.conversation_title if kind == "system" else Conversation.buyer_key
        query = query.where(field.ilike(_like(q.strip()), escape="\\"))
    if unread_only:
        query = query.where(unread > 0)
    if read_only:  # 都读过了（平台消息的“已处理”）
        query = query.where(unread == 0)
    if tag_id:  # 有消息打了这个标记的会话
        query = query.where(Conversation.id.in_(select(ConversationMark.conversation_id)
                                                .where(ConversationMark.tag_id == tag_id)))
    if stage in STAGES:
        query = query.where(Conversation.situation_stage == stage)
    query = _apply_filters(db, query, buyers=buyers, buyer_match=buyer_match, store_ids=store_ids, site=site,
                           starred=starred, order_no=order_no, tracking_no=tracking_no, sku=sku, tag_ids=tag_ids,
                           content=content, first_from=first_from, first_to=first_to, last_from=last_from, last_to=last_to)
    items = []
    now = utcnow()
    store_names = dict(db.execute(select(Store.id, Store.name)).all())  # 卡片上显示店铺名
    for conv, unread_n in db.execute(query):
        job = _current_job(db, conv.id)
        auto_replied = bool(job and job.state == JobState.SENT and db.scalar(
            select(SendIntent.id).where(SendIntent.job_id == job.id, SendIntent.actor == AUTO_ACTOR)))
        inv_status = db.scalar(select(InvoiceRequest.status).where(InvoiceRequest.conversation_id == conv.id))
        if view == "pending" and (job is None or job.state not in JobState.PENDING_REVIEW):
            continue
        if view == "auto" and not auto_replied:
            continue
        if group == "handoff":
            if conv.handoff_state != "HUMAN":
                continue
        elif group == "aftersales":  # 售后待处理与处理中（客服标记转售后的）
            if conv.aftersales_status not in (Aftersales.PENDING, Aftersales.IN_PROGRESS):
                continue
        elif group == "aftersales_done":
            if conv.aftersales_status != Aftersales.DONE:
                continue
        elif group == "invoice":  # 买家要发票、已给邮箱，等开票发送
            if inv_status != "OPEN":
                continue
        elif group == "auto":  # AI 已自动回复（当前任务由 AI 自动发出），供抽查
            if not auto_replied:
                continue
        elif group in ("failed", "unassigned", "mine"):
            if group == "failed" and not db.scalar(select(OutboundMessage.id).where(
                    OutboundMessage.conversation_id == conv.id,
                    OutboundMessage.state.in_(("FAILED", "UNCERTAIN"))).limit(1)):
                continue
            claim_now = _active_claim(db, conv)
            if group == "unassigned" and claim_now is not None:
                continue
            if group == "mine" and (claim_now is None or claim_now.claimed_by != reviewer):
                continue
        elif group != "all":
            if job is None or job.state not in GROUPS.get(group, ()):
                continue
        claim_row = _active_claim(db, conv)
        if assignee:
            if assignee == "unassigned" and claim_row is not None:
                continue
            if assignee != "unassigned" and (claim_row is None or claim_row.claimed_by != assignee):
                continue
        draft_row = db.scalar(select(Draft.id).where(Draft.job_id == job.id, Draft.status == "ACTIVE").limit(1)) if job else None
        if draft == "has" and draft_row is None or draft == "none" and draft_row is not None:
            continue
        replier = _last_replier(db, conv.id)
        if last_replier and (replier or "") != last_replier:
            continue
        last = db.scalar(select(Message).where(Message.conversation_id == conv.id)
                         .order_by(Message.conversation_seq.desc()).limit(1))
        last_buyer = db.scalar(select(Message).where(Message.conversation_id == conv.id, Message.sender_role == "BUYER")
                               .order_by(Message.conversation_seq.desc()).limit(1))
        draft = db.scalar(select(Draft).where(Draft.job_id == job.id, Draft.status == "ACTIVE")
                          .order_by(Draft.id.desc()).limit(1)) if job else None
        waiting = int((now - last_buyer.sent_at).total_seconds()) if last_buyer and job and job.state not in GROUPS["resolved"] else None
        items.append({
            "id": conv.id, "store_id": conv.store_id, "store_name": store_names.get(conv.store_id) or conv.store_id,
            "marketplace_id": conv.marketplace_id,
            "buyer_key": conv.buyer_key, "item_id": conv.item_id, "handoff_state": conv.handoff_state,
            "channel_source": conv.channel_source, "conversation_type": conv.conversation_type,
            "conversation_title": conv.conversation_title,
            "last_message_at": iso(conv.last_message_at),
            "preview": (last.body[:80] if last else ""), "preview_role": last.sender_role if last else None,
            "waiting_seconds": waiting,
            "job": _job_dict(job), "intent": draft.intent if draft else None,
            "claim": _claim_dict(claim_row),
            "auto_replied": auto_replied,
            "starred": bool(conv.starred), "last_replier": replier,
            "aftersales": _aftersales_dict(conv), "situation": _situation_dict(conv),
            "mark": _conversation_mark(db, conv.id), "marks": _conversation_marks(db, conv.id),
            "invoice_status": inv_status,  # 发票申请：OPEN 待开票发送 / DONE 已发 / CANCELLED
            "blacklist": _blacklisted(db, conv),
            "outbox_pending": db.scalar(select(func.count(OutboundMessage.id)).where(
                OutboundMessage.conversation_id == conv.id, OutboundMessage.state.in_(("PENDING", "SENDING")))) or 0,
            "outbox_problem": bool(db.scalar(select(OutboundMessage.id).where(
                OutboundMessage.conversation_id == conv.id, OutboundMessage.state.in_(("FAILED", "UNCERTAIN"))).limit(1))),
            "item_url": _conv_item_url(db, conv),
            "unread_count": unread_n or 0, "platform_unread_count": conv.platform_unread_count,
            "read_by": conv.read_by, "read_at": iso(conv.read_at),
        })
    # 查询已按最后一条消息时间从新到旧；只有“待审核”按等待时长（越久越前）、其次风险等级排，先处理等得久的
    if group == "pending" or view == "pending":
        risk_rank = {"L3": 0, "L2": 1, "L1": 2, "L0": 3, None: 4}
        items.sort(key=lambda i: (-(i["waiting_seconds"] or -1),
                                  risk_rank.get((i["job"] or {}).get("risk_level"), 4)))
    return items


def _split(v: str | None) -> list[str]:
    import re

    return [x.strip() for x in re.split(r"[\n,，;；]+", v or "") if x.strip()]


def _pairs_cond(pairs: set[tuple[str, str]]):
    """（店铺, 买家小写）→ 会话条件。"""
    from sqlalchemy import tuple_

    pairs = set(list(pairs)[:2000])
    if not pairs:
        return None
    return tuple_(Conversation.store_id, func.lower(Conversation.buyer_key)).in_(pairs)


def _apply_filters(db: Session, query, *, buyers, buyer_match, store_ids, site, starred, order_no, tracking_no, sku,
                   tag_ids, content, first_from, first_to, last_from, last_to):
    from datetime import datetime

    from ..models import EbayOrder

    def ts(v):
        if not v:
            return None
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(422, f"时间格式不对：{v}") from None
        return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d

    names = [b.lower() for b in _split(buyers)]
    if names:
        col = func.lower(Conversation.buyer_key)
        if buyer_match == "exact":
            query = query.where(col.in_(names))
        else:
            pre = "" if buyer_match == "prefix" else "%"
            query = query.where(or_(*(col.like(pre + _like(n)[1:], escape="\\") for n in names)))
    if _split(store_ids):
        query = query.where(Conversation.store_id.in_(_split(store_ids)))
    if site:
        query = query.where(Conversation.marketplace_id == site)
    if starred:
        query = query.where(Conversation.starred.is_(True))
    if _split(tag_ids):
        ids = [int(x) for x in _split(tag_ids) if x.isdigit()]
        query = query.where(Conversation.id.in_(select(ConversationMark.conversation_id).where(ConversationMark.tag_id.in_(ids))))

    def body_has(v):
        return Conversation.id.in_(select(Message.conversation_id).where(Message.body.ilike(_like(v), escape="\\")))

    if order_no and order_no.strip():
        v = order_no.strip()
        pairs = {(s, (b or "").lower()) for s, b in db.execute(select(EbayOrder.store_id, EbayOrder.buyer).where(
            EbayOrder.order_id.ilike(_like(v), escape="\\")).limit(2000))}
        conds = [body_has(v), Conversation.id.in_(select(LogisticsTicket.conversation_id).where(
            LogisticsTicket.reference_no.ilike(_like(v), escape="\\")))]
        if _pairs_cond(pairs) is not None:
            conds.append(_pairs_cond(pairs))
        query = query.where(or_(*conds))
    if tracking_no and tracking_no.strip():
        v = tracking_no.strip()
        tickets = db.execute(select(LogisticsTicket.conversation_id, LogisticsTicket.reference_no).where(
            LogisticsTicket.tracking_no.ilike(_like(v), escape="\\"))).all()
        refs = [r for _, r in tickets if r]
        pairs = {(s, (b or "").lower()) for s, b in db.execute(select(EbayOrder.store_id, EbayOrder.buyer).where(
            EbayOrder.order_id.in_(refs)))} if refs else set()
        conds = [body_has(v)]
        if [c for c, _ in tickets if c]:
            conds.append(Conversation.id.in_([c for c, _ in tickets if c]))
        if _pairs_cond(pairs) is not None:
            conds.append(_pairs_cond(pairs))
        query = query.where(or_(*conds))
    if sku and sku.strip():
        v = sku.strip()
        pairs = {(s, (b or "").lower()) for s, b in db.execute(select(EbayOrder.store_id, EbayOrder.buyer).where(
            EbayOrder.skus.ilike(_like(v), escape="\\")).limit(2000))}
        cond = _pairs_cond(pairs)
        query = query.where(cond if cond is not None else Conversation.id.is_(None))
    if content and content.strip():
        like = _like(content.strip())
        query = query.where(Conversation.id.in_(select(Message.conversation_id).where(
            or_(Message.body.ilike(like, escape="\\"), Message.zh_translation.ilike(like, escape="\\")))))
    if first_from or first_to:
        firsts = select(Message.conversation_id).group_by(Message.conversation_id)
        if first_from:
            firsts = firsts.having(func.min(Message.sent_at) >= ts(first_from))
        if first_to:
            firsts = firsts.having(func.min(Message.sent_at) <= ts(first_to))
        query = query.where(Conversation.id.in_(firsts))
    if last_from:
        query = query.where(Conversation.last_message_at >= ts(last_from))
    if last_to:
        query = query.where(Conversation.last_message_at <= ts(last_to))
    return query


def _last_replier(db: Session, conv_id: int) -> str | None:
    """最后一条店铺消息是谁发的：本系统发出的记提交人（AI 自动回复为 AI），不是本系统发的记“eBay 后台”。“已收到”回执不算。"""
    from ..sending.service import is_ack

    m = db.scalar(select(Message).where(Message.conversation_id == conv_id, Message.sender_role == "SELLER",
                                        Message.auto_kind.is_(None)).order_by(Message.conversation_seq.desc()).limit(1))
    if m is None or is_ack(m.ebay_message_id):
        return None
    ob = db.scalar(select(OutboundMessage.actor).where(OutboundMessage.message_id == m.id).limit(1))
    if ob:
        return ob
    actor = db.scalar(select(SendIntent.actor).where(SendIntent.platform_message_id == m.ebay_message_id).limit(1))
    if actor:
        return actor
    return "本系统" if m.ebay_message_id.startswith(SIMULATED_PREFIX) else "eBay 后台"


class StarBody(BaseModel):
    starred: bool


@router.put("/conversations/{conv_id}/star")
def star_conversation(conv_id: int, body: StarBody, reviewer: str = Depends(reviewer_name), db: Session = Depends(get_db)):
    """星标（同事共用）。"""
    conv = _load(db, Conversation, conv_id)
    conv.starred = body.starred
    conv.starred_by, conv.starred_at = (reviewer[:64], utcnow()) if body.starred else (None, None)
    db.commit()
    return {"id": conv.id, "starred": bool(conv.starred), "starred_by": conv.starred_by}


@router.get("/conversations/filter-options")
def filter_options(db: Session = Depends(get_db)):
    """筛选下拉的候选：eBay 账号（店铺）、站点、负责人、最近回复人、标签。"""
    from ..models import User

    stores = db.execute(_scope(select(Store.id, Store.name, Store.default_marketplace_id), Store.id).order_by(Store.name)).all()
    sites = sorted({m for (m,) in db.execute(_scope(select(Conversation.marketplace_id).distinct(), Conversation.store_id))
                    if m and m != "UNKNOWN"} | {s[2] for s in stores if s[2]})
    people = sorted({u.display_name for u in db.scalars(select(User).where(User.is_active.is_(True)))})
    tags = db.execute(select(MessageTag.id, MessageTag.name, MessageTag.color).where(MessageTag.is_active.is_(True))
                      .order_by(MessageTag.sort, MessageTag.id)).all()
    return {"stores": [{"id": i, "name": n, "marketplace_id": m} for i, n, m in stores], "sites": sites,
            "assignees": people, "repliers": [*people, AUTO_ACTOR, "eBay 后台"],
            "tags": [{"id": i, "name": n, "color": c} for i, n, c in tags],
            "buyer_match": [{"value": "contains", "label": "包含"}, {"value": "prefix", "label": "开头"},
                            {"value": "exact", "label": "完全一致"}]}


@router.get("/conversations/filter-counts")
def conversation_filter_counts(store_id: str | None = None, q: str | None = None, unread_only: bool = False,
                               view: str = "all", reviewer: str = Depends(reviewer_name),
                               db: Session = Depends(get_db)):
    """买家消息列表的会话数（按当前店铺、搜索词、只看未读统计；不受列表最多 300 条的限制）：
    views 是顶部三个页签各自的数量；其余是下拉筛选每一项在当前页签（view）里的数量。"""
    base = _scope(select(Conversation.id).where(Conversation.conversation_type != SYSTEM_TYPE), Conversation.store_id)
    if store_id:
        base = base.where(Conversation.store_id == store_id)
    if q and q.strip():
        base = base.where(Conversation.buyer_key.ilike(_like(q.strip()), escape="\\"))
    if unread_only:
        base = base.where(unread_count_expr() > 0)
    # 每个会话的当前任务（最新一个没作废的）
    latest = (select(ReplyJob.conversation_id.label("cid"), func.max(ReplyJob.id).label("jid"))
              .where(ReplyJob.state != JobState.CANCELLED_STALE).group_by(ReplyJob.conversation_id).subquery())
    current = select(latest.c.cid).join(ReplyJob, ReplyJob.id == latest.c.jid)
    view_conds = {
        "pending": Conversation.id.in_(current.where(ReplyJob.state.in_(JobState.PENDING_REVIEW))),
        "auto": Conversation.id.in_(current.where(ReplyJob.state == JobState.SENT, ReplyJob.id.in_(
            select(SendIntent.job_id).where(SendIntent.actor == AUTO_ACTOR)))),
    }
    views = {k: db.scalar(select(func.count()).select_from(base.where(c).subquery())) or 0 for k, c in view_conds.items()}
    views["all"] = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    # 左侧菜单消息箱的数量（不受页签影响）
    active_claims = select(WorkClaim.conversation_id).where(WorkClaim.expires_at > utcnow())
    box_conds = {
        "resolved": Conversation.id.in_(current.where(ReplyJob.state.in_(GROUPS["resolved"]))),
        "handoff": Conversation.handoff_state == "HUMAN",
        "failed": Conversation.id.in_(select(OutboundMessage.conversation_id)
                                      .where(OutboundMessage.state.in_(("FAILED", "UNCERTAIN")))),
        "unassigned": Conversation.id.not_in(active_claims),
        "mine": Conversation.id.in_(active_claims.where(WorkClaim.claimed_by == reviewer)),
    }
    boxes = {k: db.scalar(select(func.count()).select_from(base.where(c).subquery())) or 0 for k, c in box_conds.items()}
    if view in view_conds:
        base = base.where(view_conds[view])

    def count(*conds) -> int:
        return db.scalar(select(func.count()).select_from(base.where(*conds).subquery())) or 0

    open_invoice = select(InvoiceRequest.conversation_id).where(InvoiceRequest.status == "OPEN")
    tags = dict(db.execute(select(ConversationMark.tag_id, func.count(ConversationMark.conversation_id))
                           .where(ConversationMark.conversation_id.in_(base)).group_by(ConversationMark.tag_id)).all())
    stages = dict(db.execute(select(Conversation.situation_stage, func.count()).where(Conversation.id.in_(base))
                             .group_by(Conversation.situation_stage)).all())
    return {"all": count(), "invoice": count(Conversation.id.in_(open_invoice)),
            "stages": {k: stages.get(k, 0) for k in STAGES},
            "aftersales": count(Conversation.aftersales_status.in_((Aftersales.PENDING, Aftersales.IN_PROGRESS))),
            "aftersales_done": count(Conversation.aftersales_status == Aftersales.DONE),
            "tags": {str(k): v for k, v in tags.items()}, "views": views, "boxes": boxes}


def _observation_dict(o: ImageObservation | None) -> dict | None:
    if o is None:
        return None
    return {"id": o.id, "status": o.status, "observation": o.observation, "error": o.error,
            "cached": o.cached_from_id is not None, "review_status": o.review_status, "review_note": o.review_note,
            "reviewed_by": o.reviewed_by, "reviewed_at": iso(o.reviewed_at), "created_at": iso(o.created_at)}


def _attachments_by_message(db: Session, message_ids: list[int]) -> dict[int, list[dict]]:
    atts = attachments_for_messages(db, message_ids)
    latest: dict[int, ImageObservation] = {}
    if atts:
        for o in db.scalars(select(ImageObservation).where(ImageObservation.attachment_id.in_([a.id for a in atts]))
                            .order_by(ImageObservation.id)):
            latest[o.attachment_id] = o  # 按 id 升序覆盖，留下最新一条
    out: dict[int, list[dict]] = {}
    for a in atts:
        out.setdefault(a.message_id, []).append({
            "id": a.id, "seq": a.seq, "media_name": a.media_name, "media_type": a.media_type, "source": a.source,
            "status": a.status, "status_reason": a.status_reason, "content_type": a.content_type,
            "size_bytes": a.size_bytes, "width": a.width, "height": a.height,
            "file_url": f"/internal/attachments/{a.id}/file" if a.status == AttachmentStatus.STORED else None,
            "expires_at": iso(a.expires_at), "observation": _observation_dict(latest.get(a.id)),
        })
    return out


@router.get("/attachments/{att_id}/file")
def attachment_file(att_id: int, db: Session = Depends(get_db)):
    """返回已校验、重新编码的图片副本（不返回原始文件，也不代理任意外部地址）。"""
    att = _load(db, MessageAttachment, att_id)
    path = media_file_path(att)
    if path is None:
        raise HTTPException(404, att.status_reason or "附件文件不存在或已过保留期")
    return FileResponse(path, media_type=att.content_type or "application/octet-stream",
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"})


class ObservationReviewBody(BaseModel):
    review_status: Literal["CONFIRMED", "CORRECTED"]
    note: str = Field(default="", max_length=2000)


@router.post("/image-observations/{obs_id}/review")
def review_observation(obs_id: int, body: ObservationReviewBody, db: Session = Depends(get_db),
                       reviewer: str = Depends(reviewer_name)):
    """审核员确认或纠正图片观察；纠正记录用于评估和改进识图（第 5.2 节）。"""
    obs = _load(db, ImageObservation, obs_id)
    if body.review_status == "CORRECTED" and not body.note.strip():
        raise HTTPException(422, "纠正时请写明正确的内容")
    att = _load(db, MessageAttachment, obs.attachment_id)  # 附件带店铺，借它做店铺范围校验
    obs.review_status, obs.review_note = body.review_status, body.note.strip() or None
    obs.reviewed_by, obs.reviewed_at = reviewer, utcnow()
    db.add(AuditEvent(actor=reviewer, action="IMAGE_OBSERVATION_REVIEWED", store_id=att.store_id,
                      conversation_id=att.conversation_id,
                      detail={"observation_id": obs.id, "attachment_id": att.id, "review_status": body.review_status}))
    db.commit()
    return _observation_dict(obs)


def _conv_item_url(db: Session, conv: Conversation) -> str | None:
    """运营查看商品用的链接：按会话站点（来自刊登）选 eBay 域名，站点未知时用店铺默认站点。"""
    if not conv.item_id:
        return None
    site = conv.marketplace_id
    if not site or site == "UNKNOWN":
        store = db.get(Store, conv.store_id)
        site = store.default_marketplace_id if store else None
    return item_url(conv.item_id, site)


@router.get("/conversations/{conv_id}/customer")
def conversation_customer(conv_id: int, refresh: bool = False, db: Session = Depends(get_db)):
    from ..cache_age import max_age

    with max_age(0 if refresh else get_settings().panel_cache_seconds):
        return _conversation_customer(conv_id, db)


def _conversation_customer(conv_id: int, db: Session):
    """会话页右侧：相关信息（与这条会话相关的订单、包裹）、客户资料（累计订单与退款）、
    历史记录（同一买家在我负责的店铺里的其他会话）。订单只读、实时查（10 分钟缓存）。
    收件人姓名、地址、电话只显示给客服，不写库、不发给模型。"""
    from .. import customer_panel

    conv = _load(db, Conversation, conv_id)
    others = list(db.scalars(_scope(
        select(Conversation).where(func.lower(Conversation.buyer_key) == conv.buyer_key.lower(),
                                   Conversation.id != conv.id, Conversation.conversation_type != SYSTEM_TYPE)
        .order_by(Conversation.last_message_at.desc().nulls_last()).limit(50), Conversation.store_id)))
    previews = {}
    for c in others:
        last = db.scalar(select(Message.body).where(Message.conversation_id == c.id)
                         .order_by(Message.conversation_seq.desc()).limit(1))
        previews[c.id] = (last or "")[:120]
    store_names = dict(db.execute(select(Store.id, Store.name)).all())
    data = customer_panel.build(conv, others, store_names, previews)
    from ..customers import buyer_summary

    # 客户资料、历史订单：这个买家在我能看的全部店铺（本地汇总，随售后管理定时拉取，全部 eBay 店铺）
    data["all_stores"] = buyer_summary(db, conv.buyer_key, allowed_store_ids())
    by_id = {c.id: c for c in others}
    for h in data["history"]:
        h["last_message_at"] = iso(h["last_message_at"])
        h["item_url"] = _conv_item_url(db, by_id[h["id"]])
    return data


@router.get("/conversations/{conv_id}/listing")
def conversation_listing(conv_id: int, refresh: bool = False, db: Session = Depends(get_db)):
    from ..cache_age import max_age

    with max_age(0 if refresh else get_settings().panel_cache_seconds):
        return _conversation_listing(conv_id, db)


def _conversation_listing(conv_id: int, db: Session):
    """会话商品卡片：主图、标题、SKU、价格、库存、状态与链接。实时查本店刊登（10 分钟缓存），只读。

    查不到时退回同步时缓存的刊登信息（ebay_listings）；给运营看的，可以显示具体库存数量。
    """
    conv = _load(db, Conversation, conv_id)
    if not conv.item_id:
        return {"item_id": None}
    card = {"item_id": conv.item_id, "url": _conv_item_url(db, conv), "error": None}
    try:
        listing = get_source_for_store(conv.store_id).get_listing(conv.store_id, conv.item_id)
    except Exception as e:  # 数据源出错只影响卡片，不影响会话
        listing, card["error"] = None, f"查询商品失败：{type(e).__name__}"
    if listing:
        variations = listing.get("variations") or []
        qty = [v.get("quantity_available") for v in variations if isinstance(v.get("quantity_available"), int)]
        card.update({
            "title": listing.get("title"), "sku": listing.get("sku") or (variations[0].get("sku") if variations else None),
            "price": listing.get("price"), "condition": listing.get("condition"), "picture_url": listing.get("picture_url"),
            "listing_status": listing.get("listing_status"), "marketplace_id": listing.get("marketplace_id"),
            "quantity_available": sum(qty) if qty else None, "variations": len(variations),
            "compatibility_total": listing.get("compatibility_total") or len(listing.get("compatibility") or []),
            "url": listing.get("url") or card["url"],
        })
        return card
    cached = db.get(EbayListing, conv.item_id)
    if cached and cached.lookup_status == "FOUND":
        card.update({"title": cached.title, "marketplace_id": cached.marketplace_id,
                     "price": {"value": cached.price_value, "currency": cached.price_currency}
                     if cached.price_value else None})
    elif not card["error"]:
        card["error"] = "商品不属于当前店铺或已下架"
    return card


PENDING_FETCH_LIMIT = 8


@router.get("/conversations/{conv_id}")
def conversation_detail(conv_id: int, db: Session = Depends(get_db)):
    conv = _load(db, Conversation, conv_id)
    messages = db.scalars(select(Message).where(Message.conversation_id == conv.id)
                          .options(defer(Message.body_html)).order_by(Message.conversation_seq)).all()
    # 买家发的图片拉取时只登记：打开会话时把还没下载的下载下来（每次最多 PENDING_FETCH_LIMIT 张，下次刷新接着下）
    pending = db.scalars(select(MessageAttachment).where(MessageAttachment.conversation_id == conv.id,
                                                         MessageAttachment.status == AttachmentStatus.PENDING)
                         .order_by(MessageAttachment.id.desc()).limit(PENDING_FETCH_LIMIT)).all()
    if pending:
        ensure_stored(db, list(pending))
    attachments = _attachments_by_message(db, [m.id for m in messages])
    from ..translation import translator, untranslated_ids

    translator.enqueue(untranslated_ids(db, conv.id))  # 还没翻译的买家来信：后台译成中文
    translating = translator.pending([m.id for m in messages])
    with_html = set(db.scalars(select(Message.id).where(Message.conversation_id == conv.id,
                                                        Message.body_html.is_not(None))))
    events = db.scalars(select(ConversationEvent).where(ConversationEvent.conversation_id == conv.id)
                        .order_by(ConversationEvent.id))
    jobs = db.scalars(select(ReplyJob).where(ReplyJob.conversation_id == conv.id).order_by(ReplyJob.id.desc())).all()
    job = next((j for j in jobs if j.state != JobState.CANCELLED_STALE), None)
    sent_from = {}  # 平台消息 ID → 发出它的任务与代次（AI 草稿发出的才有生成依据）
    if jobs:
        for intent, gen, author in db.execute(
                select(SendIntent, Draft.run_generation, Draft.author).join(Draft, Draft.id == SendIntent.draft_id)
                .where(SendIntent.job_id.in_([j.id for j in jobs]), SendIntent.platform_message_id.is_not(None))):
            sent_from[intent.platform_message_id] = {"job_id": intent.job_id, "generation": gen,
                                                     "from_ai": author == "agent", "auto": intent.actor == AUTO_ACTOR}

    detail = {
        "conversation": {
            "id": conv.id, "store_id": conv.store_id, "ebay_conversation_id": conv.ebay_conversation_id,
            "store_name": (db.get(Store, conv.store_id).name if db.get(Store, conv.store_id) else None) or conv.store_id,
            "conversation_type": conv.conversation_type, "marketplace_id": conv.marketplace_id,
            "marketplace_source": conv.marketplace_source, "buyer_key": conv.buyer_key, "item_id": conv.item_id,
            "handoff_state": conv.handoff_state, "row_version": conv.row_version,
            "channel_source": conv.channel_source, "conversation_title": conv.conversation_title,
            "platform_status": conv.platform_status,
            "read_seq": conv.read_seq, "read_by": conv.read_by, "read_at": iso(conv.read_at),
            "aftersales": _aftersales_dict(conv), "situation": _situation_dict(conv),
            "mark": _conversation_mark(db, conv.id), "marks": _conversation_marks(db, conv.id),
            "item_url": _conv_item_url(db, conv),
            "unread_count": unread_count(db, conv), "platform_unread_count": conv.platform_unread_count,
        },
        "messages": [{"id": m.id, "ebay_message_id": m.ebay_message_id, "seq": m.conversation_seq,
                      "role": m.sender_role, "direction": m.direction, "body": m.body,
                      "zh_translation": m.zh_translation, "translating": m.id in translating, "sent_at": iso(m.sent_at),
                      "subject": m.subject, "sender_key": m.sender_key, "media": m.media or [],
                      "simulated": m.ebay_message_id.startswith(SIMULATED_PREFIX),
                      "ack": is_ack(m.ebay_message_id) or m.auto_kind == "ACK",  # 自动“已收到”回执：不算正式回复
                      "has_html": m.id in with_html,
                      "trace": sent_from.get(m.ebay_message_id),
                      "attachments": attachments.get(m.id, [])} for m in messages],
        "ticket_timeline": _ticket_timeline(db, conv.id),
        "invoice_timeline": _invoice_timeline(db, conv.id),
        "outbox": _outbox(db, conv.id),
        "blacklist": _blacklisted(db, conv),
        "starred": bool(conv.starred), "starred_by": conv.starred_by,
        "events": [{"id": e.id, "event_type": e.event_type, "source": e.source, "case_id": e.case_id,
                    "detail": e.detail, "occurred_at": iso(e.occurred_at)} for e in events],
        "jobs": [{"id": j.id, "state": j.state, "run_generation": j.run_generation, "created_at": iso(j.created_at)}
                 for j in jobs],
        "job": _job_dict(job),
        "claim": _claim_dict(_active_claim(db, conv)),
        "drafts": [], "tool_calls": [], "steps": [], "sources": [], "send_intent": None, "cited_knowledge": [],
        "agent_draft": None, "run": None,
    }
    if job:
        detail["drafts"] = [_draft_dict(d) for d in db.scalars(
            select(Draft).where(Draft.job_id == job.id).order_by(Draft.id.desc()))]
        detail.update(_trace(db, job, job.run_generation))
        intent = db.scalar(select(SendIntent).where(SendIntent.job_id == job.id))
        if intent:
            detail["send_intent"] = {"id": intent.id, "state": intent.state, "actor": intent.actor,
                                     "platform_message_id": intent.platform_message_id,
                                     "last_error": intent.last_error, "sent_at": iso(intent.sent_at)}
    return detail


def _trace(db: Session, job: ReplyJob, generation: int) -> dict:
    """某任务某一代次的完整生成过程：覆盖的买家消息、各步骤、业务工具结果、AI 草稿与它引用的知识条款。"""
    out = {
        "sources": list(db.scalars(select(ReplyJobSource.source_message_id).where(ReplyJobSource.job_id == job.id))),
        "tool_calls": [{
            "id": t.id, "request_id": t.request_id, "tool_name": t.tool_name, "tool_version": t.tool_version,
            "arguments": t.arguments_redacted, "status": t.result_status, "data": (t.result_data or {}).get("data"),
            "message": (t.result_data or {}).get("message"), "source": t.source, "evidence_ref": t.evidence_ref,
            "duration_ms": t.duration_ms, "queried_at": iso(t.queried_at),
        } for t in db.scalars(select(ToolCall).where(ToolCall.job_id == job.id, ToolCall.run_generation == generation)
                              .order_by(ToolCall.id))],
        "run": None, "steps": [], "agent_draft": None, "cited_knowledge": [],
    }
    run = db.scalar(select(AgentRun).where(AgentRun.job_id == job.id, AgentRun.run_generation == generation))
    if run:
        out["run"] = {"id": run.id, "state": run.state, "thread_id": run.thread_id, "mode": run.mode,
                      "prompt_version": run.prompt_version, "model_config_version": run.model_config_version,
                      "error": run.error, "started_at": iso(run.started_at), "finished_at": iso(run.finished_at)}
        out["steps"] = [{"node": s.node, "status": s.result_status, "detail": s.detail, "usage": s.usage,
                         "duration_ms": s.duration_ms}
                        for s in db.scalars(select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.id))]
    draft = db.scalar(select(Draft).where(Draft.job_id == job.id, Draft.run_generation == generation,
                                          Draft.author == "agent").order_by(Draft.id.desc()).limit(1))
    if draft:
        out["agent_draft"] = _draft_dict(draft)
        # 草稿引用的知识条款原文，供核对依据
        # 证据 ID 是 kb:<编号>@<版本>：37 个店铺共用同样的编号，必须按本会话店铺和版本取，否则会带出别的店铺、历史版本
        cited = {tuple(e[3:].split("@", 1)) for e in draft.evidence_ids or [] if e.startswith("kb:") and "@" in e}
        rows = db.scalars(select(KnowledgeChunk).where(
            KnowledgeChunk.clause_id.in_({cid for cid, _ in cited}), KnowledgeChunk.version.in_({v for _, v in cited}),
            KnowledgeChunk.store_id.in_((job.store_id, "ALL")))).all() if cited else []
        out["cited_knowledge"] = [
            {"evidence_id": f"kb:{c.clause_id}@{c.version}", "title": c.title, "text": c.text, "store_id": c.store_id,
             "marketplace_id": c.marketplace_id, "priority": c.priority, "version": c.version}
            for c in rows if (c.clause_id, c.version) in cited
        ]
    return out


@router.get("/jobs/{job_id}/trace")
def job_trace(job_id: int, generation: int | None = None, db: Session = Depends(get_db)):
    """生成依据（思考过程）：默认当前代次；已发出的消息按它对应的代次查看。"""
    job = _load(db, ReplyJob, job_id)
    gen = generation or job.run_generation
    return {"job": _job_dict(job), "generation": gen, **_trace(db, job, gen)}


# ------------------------------------------------------------------ 认领与接管
# ------------------------------------------------------------------ 测试期：删除测试会话（正式上线前移除）
BUSY_STATES = (JobState.RECEIVED, JobState.RUNNING, JobState.SENDING, JobState.SEND_UNCERTAIN)


# ------------------------------------------------------------------ 发件箱（延迟发送、撤回）
@router.post("/outbox/{ob_id}/recall")
def recall_outbound(ob_id: int, reviewer: str = Depends(reviewer_name), db: Session = Depends(get_db)):
    """撤回还没发出的消息（提交后 SEND_DELAY_SECONDS 内）。回复任务回到待审核，返回原文方便改了再发。"""
    from ..sending.outbox import OutboxError, outbox_dict, recall

    ob = db.get(OutboundMessage, ob_id)
    if ob is None:
        raise HTTPException(404, "这条待发消息不存在")
    _load(db, Conversation, ob.conversation_id)  # 店铺范围
    try:
        ob = recall(db, ob_id, reviewer)
    except OutboxError as e:
        raise HTTPException(e.status_code, e.message) from None
    db.commit()
    return outbox_dict(ob)


def _blacklisted(db: Session, conv: Conversation) -> dict | None:
    from ..customers import blacklist_dict, blacklist_entry

    return blacklist_dict(blacklist_entry(db, conv.store_id, conv.buyer_key))


def _outbox(db: Session, conv_id: int) -> list[dict]:
    """会话里还没发出、发不出或结果不确定的消息，以及最近撤回的（发出的已经在聊天记录里）。"""
    from ..sending.outbox import outbox_dict

    since = utcnow() - timedelta(days=1)
    rows = db.scalars(select(OutboundMessage).where(
        OutboundMessage.conversation_id == conv_id,
        or_(OutboundMessage.state.in_(("PENDING", "SENDING", "FAILED", "UNCERTAIN")),
            and_(OutboundMessage.state == "RECALLED", OutboundMessage.recalled_at >= since)))
        .order_by(OutboundMessage.id))
    return [outbox_dict(o) for o in rows]


# ------------------------------------------------------------------ 会话标记（整段会话的当前状态 + 备注）
def _tag_dict(t: MessageTag) -> dict:
    return {"id": t.id, "name": t.name, "color": t.color, "sort": t.sort, "is_active": t.is_active,
            "created_by": t.created_by}


def _conversation_marks(db: Session, conv_id: int) -> list[dict]:
    rows = db.execute(select(ConversationMark, MessageTag).join(MessageTag, MessageTag.id == ConversationMark.tag_id)
                      .where(ConversationMark.conversation_id == conv_id)
                      .order_by(MessageTag.sort, MessageTag.id)).all()
    return [{"tag_id": t.id, "tag": t.name, "color": t.color, "note": m.note, "marked_by": m.marked_by,
             "marked_at": iso(m.marked_at)} for m, t in rows]


def _conversation_mark(db: Session, conv_id: int) -> dict | None:
    """第一个标记（旧字段 mark，兼容）。"""
    marks = _conversation_marks(db, conv_id)
    return marks[0] if marks else None


@router.get("/message-tags")
def list_message_tags(include_inactive: bool = False, db: Session = Depends(get_db)):
    q = select(MessageTag).order_by(MessageTag.sort, MessageTag.id)
    if not include_inactive:
        q = q.where(MessageTag.is_active.is_(True))
    return [_tag_dict(t) for t in db.scalars(q)]


class TagBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=32)
    color: str | None = Field(default=None, pattern="^(primary|success|warning|danger|info|none|#[0-9a-fA-F]{6})$")  # 任意颜色或无色
    sort: int | None = None
    is_active: bool | None = None


@router.post("/message-tags")
def create_message_tag(body: TagBody, reviewer: str = Depends(reviewer_name), db: Session = Depends(get_db)):
    """新建标记项：所有账号都能建（在会话的标记弹窗里直接加）。"""
    if not body.name or not body.name.strip():
        raise HTTPException(422, "请填写标记名称")
    if db.scalar(select(MessageTag.id).where(MessageTag.name == body.name.strip())):
        raise HTTPException(409, "已有同名标记")
    top = db.scalar(select(func.max(MessageTag.sort))) or 0
    tag = MessageTag(name=body.name.strip(), color=body.color or "primary", sort=body.sort or top + 10,
                     created_by=reviewer)
    db.add(tag)
    db.flush()
    db.add(AuditEvent(actor=reviewer, action="MESSAGE_TAG_CREATED", detail={"tag": tag.name}))
    return _tag_dict(tag)


@router.delete("/message-tags/{tag_id}")
def delete_message_tag(tag_id: int, reviewer: str = Depends(reviewer_name), db: Session = Depends(get_db)):
    """删除标记项：所有账号都能删；所有会话上的这个标记一起去掉。"""
    tag = _load(db, MessageTag, tag_id)
    n = db.execute(delete(ConversationMark).where(ConversationMark.tag_id == tag.id)).rowcount or 0
    db.add(AuditEvent(actor=reviewer, action="MESSAGE_TAG_DELETED", detail={"tag": tag.name, "removed_marks": n}))
    db.delete(tag)
    db.flush()
    return {"removed_marks": n}


@router.patch("/message-tags/{tag_id}", dependencies=[Depends(require_admin)])
def update_message_tag(tag_id: int, body: TagBody, db: Session = Depends(get_db)):
    tag = _load(db, MessageTag, tag_id)
    if body.name and body.name.strip() != tag.name:
        if db.scalar(select(MessageTag.id).where(MessageTag.name == body.name.strip())):
            raise HTTPException(409, "已有同名标记")
        tag.name = body.name.strip()
    for field in ("color", "sort", "is_active"):
        if getattr(body, field) is not None:
            setattr(tag, field, getattr(body, field))
    db.flush()
    return _tag_dict(tag)


class MarkBody(BaseModel):
    tag_id: int
    note: str | None = Field(default=None, max_length=1000)


@router.put("/conversations/{conv_id}/mark")
def mark_conversation(conv_id: int, body: MarkBody, reviewer: str = Depends(reviewer_name),
                      db: Session = Depends(get_db)):
    """给会话加一个标记（如 查适配中）并写备注；一个会话可以有多个标记，同一个标记再打一次就是改备注。返回会话的全部标记。"""
    conv = _load(db, Conversation, conv_id)
    tag = _load(db, MessageTag, body.tag_id)
    mark = db.scalar(select(ConversationMark).where(ConversationMark.conversation_id == conv.id,
                                                    ConversationMark.tag_id == tag.id))
    if not tag.is_active and mark is None:
        raise HTTPException(422, "这个标记已停用")
    if mark is None:
        mark = ConversationMark(conversation_id=conv.id, store_id=conv.store_id, tag_id=tag.id)
        db.add(mark)
    mark.note = (body.note or "").strip() or None
    mark.marked_by, mark.marked_at = reviewer, utcnow()
    db.add(AuditEvent(actor=reviewer, action="CONVERSATION_MARKED", store_id=conv.store_id, conversation_id=conv.id,
                      detail={"tag": tag.name}))
    db.flush()
    return _conversation_marks(db, conv.id)


@router.delete("/conversations/{conv_id}/mark")
def unmark_conversation(conv_id: int, tag_id: int | None = None, reviewer: str = Depends(reviewer_name),
                        db: Session = Depends(get_db)):
    """去掉会话上的一个标记（tag_id），不传 tag_id 就全部去掉。"""
    conv = _load(db, Conversation, conv_id)
    cond = [ConversationMark.conversation_id == conv.id]
    if tag_id is not None:
        cond.append(ConversationMark.tag_id == tag_id)
    n = db.execute(delete(ConversationMark).where(*cond)).rowcount or 0
    if n:
        db.add(AuditEvent(actor=reviewer, action="CONVERSATION_UNMARKED", store_id=conv.store_id,
                          conversation_id=conv.id, detail={"tag_id": tag_id}))
    db.flush()
    return {"removed": n, "marks": _conversation_marks(db, conv.id)}


# ------------------------------------------------------------------ 客服转售后 / 售后处理
class AftersalesBody(BaseModel):
    action: str = Field(pattern="^(mark|unmark|start|done)$")
    type: str | None = Field(default=None, max_length=32)
    note: str | None = Field(default=None, max_length=2000)


@router.post("/conversations/{conv_id}/aftersales")
def aftersales(conv_id: int, body: AftersalesBody, request: Request, db: Session = Depends(get_db)):
    """mark：客服（或管理员）判断回复不了，标记问题类型转售后；unmark：撤回（售后还没接手时）；
    start：售后接手；done：售后处理完成。状态变化记审计。"""
    user = current_user(request)
    conv = _load(db, Conversation, conv_id)
    now, st = utcnow(), conv.aftersales_status
    if body.action in ("mark", "unmark") and user.role == Role.AFTERSALES:
        raise HTTPException(403, "转售后由客服标记")
    if body.action in ("start", "done") and user.role == Role.CS:
        raise HTTPException(403, "只有售后能接手和完成售后处理")
    if body.action == "mark":
        if body.type not in Aftersales.TYPES:
            raise HTTPException(422, "请选择问题类型")
        if st == Aftersales.DONE or st is None:
            conv.aftersales_handled_by = None
        conv.aftersales_status = st if st == Aftersales.IN_PROGRESS else Aftersales.PENDING
        conv.aftersales_type, conv.aftersales_note = body.type, (body.note or "").strip() or None
        conv.aftersales_marked_by, conv.aftersales_marked_at = user.display_name, now
    elif body.action == "unmark":
        if st != Aftersales.PENDING:
            raise HTTPException(409, "售后已接手或已完成，不能撤回")
        conv.aftersales_status = conv.aftersales_type = conv.aftersales_marked_by = conv.aftersales_marked_at = None
    elif body.action == "start":
        if st != Aftersales.PENDING:
            raise HTTPException(409, "这个会话不在待售后处理中")
        conv.aftersales_status, conv.aftersales_handled_by = Aftersales.IN_PROGRESS, user.display_name
    else:
        if st not in (Aftersales.PENDING, Aftersales.IN_PROGRESS):
            raise HTTPException(409, "这个会话没有在售后处理")
        conv.aftersales_status = Aftersales.DONE
        conv.aftersales_handled_by = conv.aftersales_handled_by or user.display_name
        if body.note and body.note.strip():
            conv.aftersales_note = ((conv.aftersales_note or "") + f"\n【售后处理】{body.note.strip()}").strip()
    conv.aftersales_updated_at = now
    db.add(AuditEvent(actor=user.display_name, action=f"AFTERSALES_{body.action.upper()}", store_id=conv.store_id,
                      conversation_id=conv.id, detail={"type": conv.aftersales_type, "status": conv.aftersales_status}))
    db.flush()
    return _aftersales_dict(conv)


def _aftersales_dict(conv: Conversation) -> dict | None:
    if not conv.aftersales_status:
        return None
    return {"status": conv.aftersales_status, "status_label": Aftersales.STATUS_LABELS.get(conv.aftersales_status),
            "type": conv.aftersales_type, "note": conv.aftersales_note, "marked_by": conv.aftersales_marked_by,
            "marked_at": iso(conv.aftersales_marked_at), "handled_by": conv.aftersales_handled_by,
            "updated_at": iso(conv.aftersales_updated_at)}


class ReadBody(BaseModel):
    read: bool = True


@router.post("/conversations/{conv_id}/read")
def set_read(conv_id: int, body: ReadBody, reader: str = Depends(optional_reviewer), db: Session = Depends(get_db)):
    """标为已读 / 未读（团队共享，只改本系统，不写回 eBay）。工作台打开会话时自动标已读。"""
    conv = _load(db, Conversation, conv_id)
    changed = mark_read(db, conv, reader) if body.read else mark_unread(db, conv, reader)
    db.flush()
    return {"changed": changed, "unread_count": unread_count(db, conv), "read_by": conv.read_by,
            "read_at": iso(conv.read_at)}


class ReadBatchBody(BaseModel):
    conversation_ids: list[int] = Field(max_length=500)


@router.post("/conversations/read-batch")
def set_read_batch(body: ReadBatchBody, reader: str = Depends(optional_reviewer), db: Session = Depends(get_db)):
    """把当前列表里的会话全部标为已读。"""
    changed = sum(mark_read(db, conv, reader)
                  for conv in db.scalars(_scope(select(Conversation).where(Conversation.id.in_(body.conversation_ids)),
                                                Conversation.store_id)))
    return {"changed": changed}


class ClaimBody(BaseModel):
    action: str = Field(pattern="^(claim|renew|release)$")


@router.post("/conversations/{conv_id}/claim")
def claim_conversation(conv_id: int, body: ClaimBody, reviewer: str = Depends(reviewer_name),
                       db: Session = Depends(get_db)):
    conv = _load(db, Conversation, conv_id)
    claim = db.scalar(select(WorkClaim).where(WorkClaim.store_id == conv.store_id, WorkClaim.conversation_id == conv.id))
    now = utcnow()
    active = claim if claim and claim.expires_at > now else None
    if body.action == "release":
        if claim and (active is None or claim.claimed_by == reviewer):
            db.delete(claim)
        return {"claim": None}
    if active and active.claimed_by != reviewer:
        raise HTTPException(409, f"已被 {active.claimed_by} 认领，{iso(active.expires_at)} 到期")
    ttl = timedelta(seconds=get_settings().claim_ttl_seconds)
    if claim is None:
        claim = WorkClaim(store_id=conv.store_id, conversation_id=conv.id, claimed_by=reviewer,
                          claimed_at=now, expires_at=now + ttl)
        db.add(claim)
    else:
        claim.claimed_by, claim.claimed_at, claim.expires_at = reviewer, now, now + ttl
    db.flush()
    return {"claim": _claim_dict(claim)}


class HandoffBody(BaseModel):
    state: str = Field(pattern="^(HUMAN|NONE)$")


@router.post("/conversations/{conv_id}/handoff")
def set_handoff(conv_id: int, body: HandoffBody, reviewer: str = Depends(reviewer_name), db: Session = Depends(get_db)):
    conv = _load(db, Conversation, conv_id)
    _ensure_claim(db, conv, reviewer)
    before = conv.handoff_state
    conv.handoff_state = body.state
    db.add(AuditEvent(actor=reviewer, action="HANDOFF_CHANGED", store_id=conv.store_id, conversation_id=conv.id,
                      detail={"before": before, "after": body.state}))
    return {"handoff_state": conv.handoff_state}


# ------------------------------------------------------------------ 审核操作
class ApproveBody(BaseModel):
    text: str = Field(max_length=5000)
    expected_conversation_version: int
    idempotency_key: str = Field(min_length=8, max_length=128)
    reason_codes: list[str] = Field(default_factory=list)
    handling_ms: int | None = None


@router.post("/drafts/{draft_id}/approve", status_code=202)
def approve_draft(draft_id: int, body: ApproveBody, reviewer: str = Depends(reviewer_name),
                  db: Session = Depends(get_db)):
    draft = _load(db, Draft, draft_id)
    job = _load(db, ReplyJob, draft.job_id)
    _ensure_claim(db, _load(db, Conversation, job.conversation_id), reviewer)
    if draft.status != "ACTIVE":
        raise HTTPException(409, f"草稿状态为 {draft.status}，不是当前有效草稿")
    if draft.text.strip() != body.text.strip() and not body.reason_codes:
        raise HTTPException(422, "修改了草稿时必须选择修改原因")
    try:
        intent = request_send(db, job=job, draft=draft, final_text=body.text, actor=reviewer,
                              idempotency_key=body.idempotency_key,
                              expected_conversation_version=body.expected_conversation_version,
                              reason_codes=body.reason_codes, handling_ms=body.handling_ms)
    except SendError as e:
        _send_error(e)
    db.commit()
    worker.wake()
    return {"intent_id": intent.id, "state": intent.state}


class ManualReplyBody(BaseModel):
    text: str = Field(max_length=5000)
    expected_conversation_version: int
    idempotency_key: str = Field(min_length=8, max_length=128)
    handling_ms: int | None = None


@router.post("/jobs/{job_id}/manual-reply", status_code=202)
def manual_reply(job_id: int, body: ManualReplyBody, reviewer: str = Depends(reviewer_name),
                 db: Session = Depends(get_db)):
    """没有 AI 草稿（如 AGENT_NOT_READY）或审核员选择自己撰写时使用；同样走统一发送入口和合规检查。"""
    job = _load(db, ReplyJob, job_id)
    conv = _load(db, Conversation, job.conversation_id)
    _ensure_claim(db, conv, reviewer)
    if db.scalar(select(SendIntent).where(SendIntent.idempotency_key == body.idempotency_key)) is None:
        for d in db.scalars(select(Draft).where(Draft.job_id == job.id, Draft.status == "ACTIVE")):
            d.status = "SUPERSEDED"
        version = (db.scalar(select(func.max(Draft.version)).where(Draft.job_id == job.id)) or 0) + 1
        draft = Draft(job_id=job.id, run_generation=job.run_generation, version=version, author=reviewer,
                      conversation_row_version=conv.row_version, text=body.text.strip(), risk_level=job.risk_level,
                      decision="REVIEW_REQUIRED", review_reasons=["MANUAL_REPLY"])
        db.add(draft)
        db.flush()
    else:
        draft = db.get(Draft, db.scalar(select(SendIntent.draft_id).where(SendIntent.idempotency_key == body.idempotency_key)))
    try:
        intent = request_send(db, job=job, draft=draft, final_text=body.text, actor=reviewer,
                              idempotency_key=body.idempotency_key,
                              expected_conversation_version=body.expected_conversation_version,
                              handling_ms=body.handling_ms)
    except SendError as e:
        _send_error(e)
    db.commit()
    worker.wake()
    return {"intent_id": intent.id, "state": intent.state}


class RejectBody(BaseModel):
    reason_codes: list[str] = Field(min_length=1)
    note: str | None = Field(default=None, max_length=1000)


@router.post("/drafts/{draft_id}/reject")
def reject_draft(draft_id: int, body: RejectBody, reviewer: str = Depends(reviewer_name), db: Session = Depends(get_db)):
    draft = _load(db, Draft, draft_id)
    job = _load(db, ReplyJob, draft.job_id)
    _ensure_claim(db, _load(db, Conversation, job.conversation_id), reviewer)
    if draft.status != "ACTIVE":
        raise HTTPException(409, f"草稿状态为 {draft.status}")
    draft.status = "REJECTED"
    db.add(ReviewFeedback(draft_id=draft.id, job_id=job.id, reviewer=reviewer, action="REJECT",
                          reason_codes=body.reason_codes, note=body.note))
    db.add(AuditEvent(actor=reviewer, action="DRAFT_REJECTED", store_id=job.store_id,
                      conversation_id=job.conversation_id, detail={"draft_id": draft.id, "reasons": body.reason_codes}))
    return {"ok": True}


@router.post("/conversations/{conv_id}/generate-draft")
def generate_draft(conv_id: int, reviewer: str = Depends(reviewer_name), db: Session = Depends(get_db)):
    """手动为会话生成 AI 草稿（主要用于从 eBay 同步来、默认不建回复任务的真实会话）。

    覆盖“店铺最后一次回复之后”的全部买家消息；只生成草稿填进工作台输入框，不发送。待审时再点一次即重新生成。
    """
    conv = _load(db, Conversation, conv_id)
    _ensure_claim(db, conv, reviewer)
    if conv.conversation_type == "FROM_EBAY":
        raise HTTPException(409, "eBay 系统消息会话不生成回复")
    current = _current_job(db, conv.id)
    if current and current.state in (JobState.RECEIVED, JobState.RUNNING, JobState.SENDING, JobState.SEND_UNCERTAIN):
        raise HTTPException(409, "AI 正在生成草稿，请稍候")
    msgs = db.scalars(select(Message).where(Message.conversation_id == conv.id).order_by(Message.conversation_seq)).all()
    last_seller = max((m.conversation_seq for m in msgs if m.sender_role == "SELLER"
                       and not (is_ack(m.ebay_message_id) or m.auto_kind == "ACK")),
                      default=0)  # “已收到”回执不算店铺回复
    pending = [m for m in msgs if m.sender_role == "BUYER" and m.conversation_seq > last_seller]
    if not pending:
        raise HTTPException(409, "没有待回复的买家消息（最后一条是店铺发出的，或发送方角色未知）")
    anchor = pending[-1].ebay_message_id
    job = db.scalar(select(ReplyJob).where(ReplyJob.store_id == conv.store_id, ReplyJob.conversation_id == conv.id,
                                           ReplyJob.anchor_source_message_id == anchor))
    if current and current.state in (*JobState.PENDING_REVIEW, JobState.RETRYABLE_ERROR) and current is not job:
        current.state = JobState.CANCELLED_STALE  # 之后又来了买家消息：旧任务的草稿不再适用
        current.lock_version += 1
    if job is None:
        job = ReplyJob(store_id=conv.store_id, conversation_id=conv.id, anchor_source_message_id=anchor,
                       state=JobState.RECEIVED, ready_at=utcnow())
        db.add(job)
        db.flush()
    else:  # 同一锚点生成过（待审中再点一次、已发出、已作废）：沿用任务、代次 +1，旧草稿作废
        job.run_generation += 1
        job.state, job.ready_at, job.attempt, job.last_error = JobState.RECEIVED, utcnow(), 0, None
        job.lock_version += 1
    stale = [job.id, current.id] if current else [job.id]
    for d in db.scalars(select(Draft).where(Draft.job_id.in_(stale), Draft.status == "ACTIVE")):
        d.status = "SUPERSEDED"
    for m in pending:  # 消息归属转到本任务
        db.execute(delete(ReplyJobSource).where(ReplyJobSource.store_id == conv.store_id,
                                                ReplyJobSource.source_message_id == m.ebay_message_id))
        db.add(ReplyJobSource(store_id=conv.store_id, source_message_id=m.ebay_message_id, job_id=job.id))
    db.add(AuditEvent(actor=reviewer, action="GENERATE_DRAFT", store_id=conv.store_id, conversation_id=conv.id,
                      detail={"job_id": job.id, "messages": len(pending), "generation": job.run_generation}))
    db.commit()
    worker.wake()
    return {"job_id": job.id, "messages": len(pending), "run_generation": job.run_generation}


class SimulatedSendBody(BaseModel):
    text: str = Field(max_length=5000)
    file_ids: list[int] = Field(default_factory=list, max_length=5)  # 回复里带的图片（先用 reply-images 上传）
    handling_ms: int | None = None
    text_zh: str | None = Field(default=None, max_length=10000)  # 工作台已有的当前文本中文译文（只作对照显示）


class ReplyImageBody(BaseModel):
    name: str | None = Field(default=None, max_length=300)
    data: str = Field(min_length=10, max_length=8 * 1024 * 1024)  # base64（含 data URL 前缀也行）


@router.post("/conversations/{conv_id}/reply-images")
def upload_reply_image(conv_id: int, body: ReplyImageBody, reviewer: str = Depends(reviewer_name),
                       db: Session = Depends(get_db)):
    """回复里要带的图片：先上传存好（挂在这个会话上），发送时把返回的 file_id 放进 file_ids。"""
    from ..sending.reply_media import save_image

    conv = _load(db, Conversation, conv_id)
    try:
        f = save_image(db, conv, body.name, body.data, reviewer)
    except SendError as e:
        _send_error(e)
    db.commit()
    return {"file_id": f.id, "name": f.original_name, "url": f"/internal/conversations/{conv.id}/reply-images/{f.id}"}


@router.get("/conversations/{conv_id}/reply-images/{file_id}")
def reply_image(conv_id: int, file_id: int, db: Session = Depends(get_db)):
    from ..models import AftersalesFile

    conv = _load(db, Conversation, conv_id)
    f = db.get(AftersalesFile, file_id)
    if f is None or f.conversation_id != conv.id or f.status != "STORED" or not f.rel_path:
        raise HTTPException(404, "图片不存在")
    root = get_settings().aftersales_root.resolve()
    path = (root / f.rel_path).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(404, "图片不存在")
    return FileResponse(path, media_type=f.content_type or None)


@router.post("/conversations/{conv_id}/send")
def send_simulated(conv_id: int, body: SimulatedSendBody, reviewer: str = Depends(reviewer_name),
                   db: Session = Depends(get_db)):
    """工作台的“发送”：进发件箱，延迟 SEND_DELAY_SECONDS（默认 5 分钟）后发出，期间可撤回；对接了 eBay 的店铺真实发到 eBay。"""
    conv = _load(db, Conversation, conv_id)
    _ensure_claim(db, conv, reviewer)
    try:
        msg, job = simulate_send(db, conv=conv, text=body.text, actor=reviewer, handling_ms=body.handling_ms,
                                 text_zh=body.text_zh, file_ids=body.file_ids)
    except SendError as e:
        _send_error(e)
    db.commit()
    try:  # 飞书售后群已回复的工单：标“已回复买家”并在话题里回帖
        from ..feishu.cases import after_seller_reply

        if after_seller_reply(db, conv, msg.body, body.text_zh, reviewer):
            db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
    # 改过的草稿或人工撰写的回复：发出后补一份中文对照（尽力而为，模型不可用时不影响发送，可在消息上点“译”）
    if not msg.zh_translation and not _mostly_chinese(msg.body):
        try:
            msg.zh_translation = _translate(db, msg.body, "中文", store_id=msg.store_id, conversation_id=conv.id)
            if msg.message_id:  # 已经发出（延迟为 0）：消息上也补上
                sent = db.get(Message, msg.message_id)
                if sent is not None:
                    sent.zh_translation = msg.zh_translation
            db.commit()
        except Exception:
            db.rollback()
    return {"outbound_id": msg.id, "state": msg.state, "send_after": iso(msg.send_after), "message_id": msg.message_id,
            "ebay_message_id": msg.platform_message_id, "job_id": job.id if job else None,
            "zh_translation": msg.zh_translation}


def _mostly_chinese(text: str) -> bool:
    return mostly_chinese(text)


@router.post("/jobs/{job_id}/regenerate")
def regenerate(job_id: int, reviewer: str = Depends(reviewer_name), db: Session = Depends(get_db)):
    job = _load(db, ReplyJob, job_id)
    _ensure_claim(db, _load(db, Conversation, job.conversation_id), reviewer)
    if job.state not in (*JobState.PENDING_REVIEW, JobState.RETRYABLE_ERROR):
        raise HTTPException(409, f"任务状态为 {job.state}，不能重新生成")
    for d in db.scalars(select(Draft).where(Draft.job_id == job.id, Draft.status == "ACTIVE")):
        d.status = "SUPERSEDED"
    job.run_generation += 1
    job.state = JobState.RECEIVED
    job.ready_at = utcnow()
    job.attempt = 0
    job.lock_version += 1
    db.add(AuditEvent(actor=reviewer, action="REGENERATE", store_id=job.store_id, conversation_id=job.conversation_id,
                      detail={"job_id": job.id, "generation": job.run_generation}))
    db.commit()
    worker.wake()
    return {"job_id": job.id, "run_generation": job.run_generation}


class ResolveBody(BaseModel):
    note: str | None = Field(default=None, max_length=500)




@router.post("/jobs/{job_id}/resolve")
def resolve_no_reply(job_id: int, body: ResolveBody, reviewer: str = Depends(reviewer_name),
                     db: Session = Depends(get_db)):
    """原“无需回复”：按规则最后一句必须是我们发的，改为按买家语言发一句简短致谢，任务记为已发送（不再有不回复就结束）。"""
    from ..logistics.followup import _notice_language
    from ..review_ack import CLOSING_TEXT, CLOSING_ZH

    job = _load(db, ReplyJob, job_id)
    conv = _load(db, Conversation, job.conversation_id)
    _ensure_claim(db, conv, reviewer)
    if job.state not in (*JobState.PENDING_REVIEW, JobState.RETRYABLE_ERROR):
        raise HTTPException(409, f"任务状态为 {job.state}")
    draft = db.scalar(select(Draft).where(Draft.job_id == job.id, Draft.status == "ACTIVE").order_by(Draft.id.desc()).limit(1))
    lang = _notice_language(db, conv, draft.language if draft else None)
    try:
        msg, _ = simulate_send(db, conv=conv, text=CLOSING_TEXT.get(lang, CLOSING_TEXT["en"]), actor=reviewer,
                               text_zh=CLOSING_ZH)
    except SendError as e:
        _send_error(e)
    db.add(ReviewFeedback(job_id=job.id, reviewer=reviewer, action="CLOSING_REPLY", note=body.note))
    return {"ok": True, "message_id": msg.id, "language": lang}


# ------------------------------------------------------------------ 预览、翻译
class TextBody(BaseModel):
    text: str = Field(max_length=5000)
    target_language: str = "中文"


@router.post("/compliance/check")
def compliance_check(body: TextBody):
    return ComplianceChecker().check(body.text).to_dict()


def _llm_or_503():
    llm = get_chat_provider()
    if llm is None:
        raise HTTPException(503, {"message": "AGENT_NOT_READY：模型未配置，无法翻译",
                                  "reasons": get_settings().agent_not_ready_reasons()})
    return llm


def _translate(db: Session, text: str, target_language: str, **where) -> str:
    llm = _llm_or_503()
    provider, model = usage_log.translate_model(get_settings())
    started = time.perf_counter()
    out, usage = llm.translate_with_usage(text, target_language)
    usage_log.record(db, "translate", provider=provider, model=model, usage=usage,
                     duration_ms=int((time.perf_counter() - started) * 1000), **where)
    return out


@router.post("/translate")
def translate(body: TextBody, db: Session = Depends(get_db)):
    """中文改写转目标语言、草稿回译等（第 5.2 节）。译文只用于理解，不作为证据。"""
    return {"translation": _translate(db, body.text, body.target_language)}


LANG_NAMES = {"de": "德语", "en": "英语", "fr": "法语", "it": "意大利语", "es": "西班牙语", "nl": "荷兰语", "pl": "波兰语",
              "zh": "中文"}
_SITE_LANG = {"EBAY_DE": "de", "EBAY_AT": "de", "EBAY_FR": "fr", "EBAY_IT": "it", "EBAY_ES": "es", "EBAY_NL": "nl",
              "EBAY_PL": "pl"}


class ZhComposeBody(BaseModel):
    text_zh: str = Field(min_length=1, max_length=3000)
    language: str | None = Field(default=None, max_length=16)  # 不填：按本会话 AI 草稿的语言，再按站点


@router.post("/conversations/{conv_id}/translate-from-zh")
def translate_from_zh(conv_id: int, body: ZhComposeBody, db: Session = Depends(get_db)):
    """审核员在“中文对照”里改稿：把中文译成买家语言，填回上方正式回复（发送前仍要审核）。"""
    conv = _load(db, Conversation, conv_id)
    lang = (body.language or "").lower().split("-")[0] or None
    if not lang:
        lang = db.scalar(select(Draft.language).join(ReplyJob, Draft.job_id == ReplyJob.id)
                         .where(ReplyJob.conversation_id == conv.id, Draft.author == "agent", Draft.language.is_not(None))
                         .order_by(Draft.id.desc()).limit(1))
        lang = (lang or "").lower().split("-")[0] or _SITE_LANG.get(conv.marketplace_id or "", "en")
    name = LANG_NAMES.get(lang, lang)
    text = body.text_zh.strip() if lang == "zh" else _translate(
        db, body.text_zh, f"{name}（发给 eBay 买家的客服回复，语气礼貌自然）", store_id=conv.store_id, conversation_id=conv.id)
    return {"text": text, "language": lang, "language_name": name}


@router.get("/messages/{message_id}/html")
def message_html(message_id: int, db: Session = Depends(get_db)):
    """eBay 原始 HTML 邮件（系统通知）。工作台放进禁用脚本的沙箱 iframe 里显示。"""
    msg = _load(db, Message, message_id)
    if not msg.body_html:
        raise HTTPException(404, "这条消息没有原始 HTML")
    return {"html": msg.body_html, "subject": msg.subject}


@router.post("/messages/{message_id}/translate")
def translate_message(message_id: int, db: Session = Depends(get_db)):
    msg = _load(db, Message, message_id)
    if not msg.zh_translation:
        msg.zh_translation = _translate(db, msg.body, "中文", store_id=msg.store_id, conversation_id=msg.conversation_id)
    return {"zh_translation": msg.zh_translation}


# ------------------------------------------------------------------ 知识与指标
@router.get("/knowledge")
def list_knowledge(store_id: str | None = None, marketplace_id: str | None = None, on: date | None = None,
                   include_retired: bool = False, db: Session = Depends(get_db)):
    s = get_settings()
    store_id = store_id or s.ebay_store_id
    check_store(store_id)
    marketplace_id = marketplace_id or s.ebay_store_marketplace
    on = on or s.knowledge_date()
    rows = []
    applicable_tokens = 0
    q = select(KnowledgeChunk).where(KnowledgeChunk.store_id.in_((store_id, "ALL")))
    if not include_retired:  # 被新版本替换下线的条款默认不显示，在“历史版本”里看
        q = q.where(KnowledgeChunk.review_state.notin_(("RETIRED", "WITHDRAWN")))
    from ..auth import current
    from ..knowledge.kb_docs import docs_brief

    user = current()
    for c in db.scalars(q.order_by(KnowledgeChunk.priority, KnowledgeChunk.clause_id)):
        reason = exclusion_reason(c, store_id, marketplace_id, on, "PUBLIC_REPLY")
        tokens = estimate_tokens(c.title + c.text)
        if reason is None:
            applicable_tokens += tokens + 12
        rows.append({
            "chunk_id": c.chunk_id, "clause_id": c.clause_id, "import_id": c.import_id,
            "document_id": c.document_id, "version": c.version, "title": c.title,
            "text": c.text, "store_id": c.store_id, "marketplace_id": c.marketplace_id, "audience": c.audience,
            "language": c.language, "topic": c.topic, "priority": c.priority, "effective_at": c.effective_at,
            "expires_at": c.expires_at, "review_state": c.review_state, "sku": c.sku or [], "source": c.source,
            "scenes": c.scenes or [], "applicable": reason is None, "excluded_reason": reason, "tokens": tokens,
            "doc_refs": c.doc_refs or [], "docs": docs_brief(db, c.doc_refs or [], user),
        })
    return {"date": on.isoformat(), "store_id": store_id, "marketplace_id": marketplace_id, "items": rows,
            "applicable_count": sum(r["applicable"] for r in rows), "applicable_tokens": applicable_tokens,
            "budget_tokens": s.knowledge_full_context_max_tokens}


@router.get("/metrics/workbench")
def workbench_metrics(store_id: str | None = None, db: Session = Depends(get_db)):
    jobs_q = _scope(select(ReplyJob.state, func.count()).group_by(ReplyJob.state), ReplyJob.store_id)
    fb_q = _scope(select(ReviewFeedback).join(ReplyJob, ReplyJob.id == ReviewFeedback.job_id), ReplyJob.store_id)
    if store_id:
        jobs_q = jobs_q.where(ReplyJob.store_id == store_id)
        fb_q = fb_q.where(ReplyJob.store_id == store_id)
    feedback = db.scalars(fb_q).all()
    reviewed = [f for f in feedback if f.action in ("APPROVE", "EDIT_APPROVE", "REJECT")]
    adopted = [f for f in feedback if f.action == "APPROVE" or (f.action == "EDIT_APPROVE" and (f.edit_ratio or 0) < 0.1)]
    ratios = [f.edit_ratio for f in feedback if f.edit_ratio is not None]
    durations = sorted(f.handling_ms for f in feedback if f.handling_ms)
    reasons: dict[str, int] = {}
    for f in feedback:
        for r in f.reason_codes or []:
            reasons[r] = reasons.get(r, 0) + 1
    return {
        "jobs_by_state": dict(db.execute(jobs_q).all()),
        "reviewed_drafts": len(reviewed),
        # 草稿采纳率：无修改或修改幅度 < 10% 即批准 ÷ 已审核草稿（第 2.2 节口径）
        "adoption_rate": round(len(adopted) / len(reviewed), 4) if reviewed else None,
        "avg_edit_ratio": round(sum(ratios) / len(ratios), 4) if ratios else None,
        "median_handling_ms": durations[len(durations) // 2] if durations else None,
        "manual_replies": sum(f.action == "MANUAL" for f in feedback),
        "reason_distribution": reasons,
    }


# ------------------------------------------------------------------ 谷仓查件单（同步结果与回复建议）
def _cn_iso(value: str | None) -> str | None:
    """谷仓时间（北京时间、无时区）→ ISO UTC。"""
    from ..logistics.assess import CN, parse_time

    dt = parse_time(value, CN)
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z" if dt else None


def _ticket_timeline(db: Session, conv_id: int) -> list[dict]:
    """对话里的查件记录：发起查件（提交到谷仓 / 本系统起草 / 谷仓后台建的）和收到反馈，按时间插进聊天记录。"""
    out = []
    for t in db.scalars(select(LogisticsTicket).where(LogisticsTicket.conversation_id == conv_id)):
        opened = iso(t.submitted_at) if t.submitted_at else (iso(t.created_at) if t.created_at else _cn_iso(t.added_at))
        base = {"ticket_id": t.id, "io_code": t.io_code, "type": t.type_name, "tracking_no": t.tracking_no}
        if opened:
            out.append({**base, "kind": "opened", "key": f"o{t.id}", "at": opened, "desc": t.io_desc,
                        "by": t.submitted_by or t.created_by or "谷仓后台", "status": t.io_status_text,
                        "draft": is_local_draft(t)})
        if t.reply or t.attachments:
            out.append({**base, "kind": "replied", "key": f"r{t.id}", "at": _cn_iso(t.replied_at) or iso(t.synced_at),
                        "reply": t.reply, "attachments": [{"name": a.get("name"), "file_id": a.get("file_id")}
                                                          for a in t.attachments or []],
                        "send_status": t.send_status, "send_error": t.send_error, "sent_at": iso(t.sent_at),
                        "needs_human": t.needs_human, **_outcome(t)})
    return sorted(out, key=lambda x: x["at"] or "")


def _invoice_timeline(db: Session, conv_id: int) -> list[dict]:
    """对话里的发票：生成、发到邮箱的时间和结果，按时间插进聊天记录；管理员可点开看 PDF。"""
    from ..invoicing.service import KIND_LABELS, mask_email

    user = current_account()
    admin = user is not None and user.role == Role.ADMIN
    req = db.scalar(select(InvoiceRequest).where(InvoiceRequest.conversation_id == conv_id))
    out = []
    for inv in db.scalars(select(Invoice).where(Invoice.conversation_id == conv_id).order_by(Invoice.id)):
        reviewable = inv.status in (InvoiceStatus.ISSUED, InvoiceStatus.SEND_FAILED)
        out.append({
            "kind": "invoice", "key": f"i{inv.id}", "at": iso(inv.sent_at or inv.created_at), "invoice_id": inv.id,
            "number": inv.number, "invoice_kind": inv.kind, "kind_label": KIND_LABELS.get(inv.kind, inv.kind),
            "order_id": inv.order_id, "total": inv.total, "currency": inv.currency, "status": inv.status,
            "status_label": InvoiceStatus.LABELS.get(inv.status, inv.status), "send_error": inv.send_error,
            "sent_to": inv.sent_to if admin else mask_email(inv.sent_to),
            "buyer_email": (req.email if admin else mask_email(req.email)) if req and req.email else None,
            "sent_by": inv.sent_by, "created_by": inv.created_by, "created_at": iso(inv.created_at),
            "sent_at": iso(inv.sent_at),
            "pdf_url": f"/internal/invoices/{inv.id}/pdf" if admin or reviewable else None,
        })
    return out


def _ticket_dict(t: LogisticsTicket) -> dict:
    return {"id": t.id, "io_code": t.io_code, "order_id": t.reference_no, "tracking_no": t.tracking_no,
            "type": t.type_name, "status": t.io_status_text, "trail_status": t.trail_status_text, "sm_name": t.sm_name,
            "warehouse_code": t.warehouse_code, "reply": t.reply, "reject_reason": t.reject_reason,
            "attachments": [{"name": a.get("name"), "file_id": a.get("file_id")} for a in t.attachments or []],
            "added_at": t.added_at, "replied_at": t.replied_at, "suggestion": t.suggestion,
            "suggestion_zh": t.suggestion_zh, "needs_human": t.needs_human, "suggested_at": iso(t.suggested_at),
            "handled_at": iso(t.handled_at), "handled_by": t.handled_by, "is_draft": is_draft(t), "origin": t.origin,
            "io_desc": t.io_desc, "created_by": t.created_by, "created_at": iso(t.created_at),
            "is_local_draft": is_local_draft(t), "it_type_id": t.it_type_id, "sm_code": t.sm_code,
            "submitted_by": t.submitted_by, "submitted_at": iso(t.submitted_at),
            "send_status": t.send_status, "sent_at": iso(t.sent_at), "sent_message_id": t.sent_message_id,
            "send_error": t.send_error, "notice_status": t.notice_status, "notice_sent_at": iso(t.notice_sent_at),
            **_outcome(t)}


def _outcome(t: LogisticsTicket) -> dict:
    """谷仓回复的结果类别和处理方式（自动回复 / 需人工审核 / 需判断补发）。"""
    from ..logistics.tickets import OUTCOMES

    label, how = OUTCOMES.get(t.outcome, (None, None)) if t.outcome else (None, None)
    return {"outcome": t.outcome, "outcome_label": label, "handling": how}


def _ticket_view_cond(view: str):
    T = LogisticsTicket
    not_closed = or_(T.io_status.is_(None), T.io_status.not_in(CLOSED_TICKET))
    draft = and_(not_closed, or_(T.io_code.like(DRAFT_PREFIX + "%"), T.io_status_text.like("%草稿%"),
                                 T.io_status_text.like("%待提交%")))
    return {
        "draft": draft,  # 本系统草稿（或谷仓后台保存的草稿），等人工提交
        "replied": or_(T.reply.is_not(None), T.replied_at.is_not(None)),  # 谷仓已回复（含附件），附发给买家的结果
        "reply": and_(T.suggestion.is_not(None), T.handled_at.is_(None)),  # 谷仓已回复、AI 起草了给买家的回复
        "human": and_(T.needs_human.is_(True), T.handled_at.is_(None)),
        "open": and_(T.handled_at.is_(None), not_closed, ~draft),  # 已提交，谷仓处理中
        "done": or_(T.handled_at.is_not(None), T.io_status.in_(CLOSED_TICKET)),
    }.get(view)


@router.get("/logistics-tickets")
def list_tickets(view: str = "draft", store_id: str | None = None, q: str | None = None, limit: int = 200,
                 db: Session = Depends(get_db)):
    """查件单总表：draft 草稿待提交 / replied 谷仓已回复（含是否已发给买家）/ reply 有新回复待回买家 / human 需人工 /
    open 谷仓处理中 / done 已处理或已完结 / all。
    按负责店铺；没匹配到店铺的（非 eBay 订单）只有管理员和全部店铺账号看得到。附各分类数量。"""
    base = _scope(select(LogisticsTicket), LogisticsTicket.store_id)
    if store_id:
        base = base.where(LogisticsTicket.store_id == store_id)
    if q and q.strip():
        like = _like(q.strip())
        base = base.where(or_(*(c.ilike(like, escape="\\") for c in (
            LogisticsTicket.io_code, LogisticsTicket.reference_no, LogisticsTicket.tracking_no, LogisticsTicket.buyer_key))))
    counts = {v: db.scalar(select(func.count()).select_from(base.where(_ticket_view_cond(v)).subquery())) or 0
              for v in ("draft", "replied", "reply", "human", "open", "done")}
    counts["all"] = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    cond = _ticket_view_cond(view)
    rows = db.scalars((base.where(cond) if cond is not None else base)
                      .order_by(func.coalesce(LogisticsTicket.replied_at, LogisticsTicket.added_at).desc().nulls_first(),
                                LogisticsTicket.id.desc()).limit(min(max(limit, 1), 500))).all()
    names = dict(db.execute(select(Store.id, Store.name)).all())
    conv_ids = {t.conversation_id for t in rows if t.conversation_id}
    buyers = dict(db.execute(select(Conversation.id, Conversation.buyer_key).where(Conversation.id.in_(conv_ids))).all())         if conv_ids else {}
    refs = {t.reference_no for t in rows if t.reference_no}
    open_by_ref = dict(db.execute(select(LogisticsTicket.reference_no, func.count()).where(
        LogisticsTicket.reference_no.in_(refs), LogisticsTicket.handled_at.is_(None),
        or_(LogisticsTicket.io_status.is_(None), LogisticsTicket.io_status.not_in(CLOSED_TICKET)))
        .group_by(LogisticsTicket.reference_no)).all()) if refs else {}
    return {"counts": counts, "items": [{**_ticket_dict(t), "store_id": t.store_id, "store_name": names.get(t.store_id),
                                         "buyer_key": buyers.get(t.conversation_id) or t.buyer_key,
                                         "conversation_id": t.conversation_id,
                                         # 同一订单未完结的查件单数（大于 1 = 有重复，建单前先看一眼）
                                         "open_same_order": open_by_ref.get(t.reference_no, 0)} for t in rows]}


@router.get("/conversations/{conv_id}/logistics-tickets")
def conversation_tickets(conv_id: int, db: Session = Depends(get_db)):
    """这个会话关联的谷仓查件单（每天同步，含客服在谷仓后台手动建的），新回复在前。"""
    conv = _load(db, Conversation, conv_id)
    rows = db.scalars(select(LogisticsTicket).where(LogisticsTicket.conversation_id == conv.id)
                      .order_by(LogisticsTicket.handled_at.is_(None).desc(), LogisticsTicket.id.desc()).limit(20))
    return [_ticket_dict(t) for t in rows]


@router.post("/logistics-tickets/{ticket_id}/handled")
def ticket_handled(ticket_id: int, request: Request, db: Session = Depends(get_db)):
    """客服已就查件结果回复买家（或判断不用回复）：标记已处理，不再提醒。"""
    t = _load(db, LogisticsTicket, ticket_id)
    user = current_user(request)
    t.handled_at, t.handled_by = utcnow(), user.display_name
    db.add(AuditEvent(actor=user.display_name, action="LOGISTICS_TICKET_HANDLED", store_id=t.store_id,
                      conversation_id=t.conversation_id, detail={"io_code": t.io_code}))
    return _ticket_dict(t)


class TicketDraftEdit(BaseModel):
    it_type_id: int | None = None
    type_name: str | None = Field(default=None, max_length=40)
    io_desc: str | None = Field(default=None, max_length=300)
    notify_buyer: bool = True  # 提交时同时给买家发查件安抚消息（客服点提交即审核通过）


@router.get("/logistics-tickets/{ticket_id}/types")
def ticket_types(ticket_id: int, db: Session = Depends(get_db)):
    """这张查件单的物流方式支持的查件类型（谷仓实时查询，只读）。"""
    t = _load(db, LogisticsTicket, ticket_id)
    try:
        return [{"value": x.get("value"), "name": x.get("name")} for x in GoodcangClient().ticket_types(t.sm_code)]
    except GoodcangError as e:
        raise HTTPException(502, str(e)) from None


@router.patch("/logistics-tickets/{ticket_id}")
def ticket_edit(ticket_id: int, body: TicketDraftEdit, request: Request, db: Session = Depends(get_db)):
    """修改本系统草稿的查件类型、查件需求（提交前）。"""
    t = _load(db, LogisticsTicket, ticket_id)
    try:
        edit_draft(t, it_type_id=body.it_type_id, type_name=body.type_name, io_desc=body.io_desc)
    except TicketError as e:
        raise HTTPException(409, str(e)) from None
    db.add(AuditEvent(actor=current_user(request).display_name, action="LOGISTICS_TICKET_EDITED", store_id=t.store_id,
                      conversation_id=t.conversation_id, detail={"io_code": t.io_code, "type": t.type_name}))
    return _ticket_dict(t)


@router.post("/logistics-tickets/{ticket_id}/submit")
def ticket_submit(ticket_id: int, body: TicketDraftEdit, request: Request, db: Session = Depends(get_db)):
    """（可先带上修改）把本系统草稿提交到谷仓：真正在谷仓建查件单，之后买家回复里才能说已联系承运商。"""
    t = _load(db, LogisticsTicket, ticket_id)
    user = current_user(request).display_name
    try:
        edit_draft(t, it_type_id=body.it_type_id, type_name=body.type_name, io_desc=body.io_desc)
        submit_draft(db, t, GoodcangClient(), user)
    except TicketError as e:
        raise HTTPException(409, str(e)) from None
    except GoodcangError as e:
        raise HTTPException(502, str(e)) from None
    try:  # 异常包裹跟进表：登记查件单号，下一步“等查件结果”
        from ..parcel_auto import on_ticket_submitted

        with db.begin_nested():
            on_ticket_submitted(db, t)
    except Exception:  # noqa: BLE001
        log.exception("异常包裹跟进登记失败 ticket=%s", t.id)
    if body.notify_buyer:  # 客服勾了“通知买家”：提交即审核通过，马上发安抚消息；没发的原因随结果返回
        notice = send_submit_notice(db, [t], approved_by=user)
    else:
        notice = t.notice_status = "提交时选择不通知买家"
    return {**_ticket_dict(t), "notice": notice}


@router.post("/logistics-tickets/{ticket_id}/discard")
def ticket_discard(ticket_id: int, request: Request, db: Session = Depends(get_db)):
    """废弃本系统草稿（建错了、不需要查件）：不会发到谷仓。"""
    t = _load(db, LogisticsTicket, ticket_id)
    try:
        discard_draft(db, t, current_user(request).display_name)
    except TicketError as e:
        raise HTTPException(409, str(e)) from None
    return _ticket_dict(t)


@router.get("/logistics-tickets/{ticket_id}/stock")
def ticket_stock(ticket_id: int, db: Session = Depends(get_db)):
    """补发前查库存：订单商品在原发货区域仓的可售数量（谷仓实时查询，只读）。没货只能协商退款。"""
    t = _load(db, LogisticsTicket, ticket_id)
    if not t.reference_no:
        raise HTTPException(409, "查件单没有订单号")
    try:
        return reship_stock(GoodcangClient(), t.reference_no)
    except GoodcangError as e:
        raise HTTPException(502, str(e)) from None
