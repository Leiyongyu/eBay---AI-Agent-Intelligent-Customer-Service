"""人工工作台接口（第 5 章、第 13 章“内部 API”）。"""
from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, defer

from ..config import get_settings
from ..db import get_db, utcnow
from ..guards.compliance import ComplianceChecker
from ..knowledge.retriever import estimate_tokens, exclusion_reason
from ..media.service import attachments_for_messages
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
    InvoiceRequest,
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
from ..logistics.followup import is_draft, reship_stock
from ..logistics.goodcang import GoodcangClient, GoodcangError
from ..providers.chat import get_chat_provider
from ..sending.service import AUTO_ACTOR, SIMULATED_PREFIX, SendError, request_send, simulate_send
from .. import usage as usage_log
from ..worker import worker
from ..sources import get_source_for_store
from ..sources.ebay import item_url
from ..guards.language import mostly_chinese
from ..read_state import mark_read, mark_unread, unread_count, unread_count_expr
from ..auth import allowed_store_ids, check_store, current_user, require_admin
from .deps import iso, optional_reviewer, reviewer_name


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
                       db: Session = Depends(get_db)):
    """kind：buyer 买家消息（含测试会话）/ system eBay 系统消息 / 空为全部。"""
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
    if tag_id:  # 有消息打了这个标记的会话
        query = query.where(Conversation.id.in_(select(ConversationMark.conversation_id)
                                                .where(ConversationMark.tag_id == tag_id)))
    items = []
    now = utcnow()
    for conv, unread_n in db.execute(query):
        job = _current_job(db, conv.id)
        auto_replied = bool(job and job.state == JobState.SENT and db.scalar(
            select(SendIntent.id).where(SendIntent.job_id == job.id, SendIntent.actor == AUTO_ACTOR)))
        inv_status = db.scalar(select(InvoiceRequest.status).where(InvoiceRequest.conversation_id == conv.id))
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
        elif group != "all":
            if job is None or job.state not in GROUPS.get(group, ()):
                continue
        last = db.scalar(select(Message).where(Message.conversation_id == conv.id)
                         .order_by(Message.conversation_seq.desc()).limit(1))
        last_buyer = db.scalar(select(Message).where(Message.conversation_id == conv.id, Message.sender_role == "BUYER")
                               .order_by(Message.conversation_seq.desc()).limit(1))
        draft = db.scalar(select(Draft).where(Draft.job_id == job.id, Draft.status == "ACTIVE")
                          .order_by(Draft.id.desc()).limit(1)) if job else None
        waiting = int((now - last_buyer.sent_at).total_seconds()) if last_buyer and job and job.state not in GROUPS["resolved"] else None
        items.append({
            "id": conv.id, "store_id": conv.store_id, "marketplace_id": conv.marketplace_id,
            "buyer_key": conv.buyer_key, "item_id": conv.item_id, "handoff_state": conv.handoff_state,
            "channel_source": conv.channel_source, "conversation_type": conv.conversation_type,
            "conversation_title": conv.conversation_title,
            "last_message_at": iso(conv.last_message_at),
            "preview": (last.body[:80] if last else ""), "preview_role": last.sender_role if last else None,
            "waiting_seconds": waiting,
            "job": _job_dict(job), "intent": draft.intent if draft else None,
            "claim": _claim_dict(_active_claim(db, conv)),
            "auto_replied": auto_replied,
            "aftersales": _aftersales_dict(conv),
            "mark": _conversation_mark(db, conv.id),
            "invoice_status": inv_status,  # 发票申请：OPEN 待开票发送 / DONE 已发 / CANCELLED
            "item_url": _conv_item_url(db, conv),
            "unread_count": unread_n or 0, "platform_unread_count": conv.platform_unread_count,
            "read_by": conv.read_by, "read_at": iso(conv.read_at),
        })
    # 查询已按最后一条消息时间从新到旧；只有“待审核”按等待时长（越久越前）、其次风险等级排，先处理等得久的
    if group == "pending":
        risk_rank = {"L3": 0, "L2": 1, "L1": 2, "L0": 3, None: 4}
        items.sort(key=lambda i: (-(i["waiting_seconds"] or -1),
                                  risk_rank.get((i["job"] or {}).get("risk_level"), 4)))
    return items


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


@router.get("/conversations/{conv_id}/listing")
def conversation_listing(conv_id: int, db: Session = Depends(get_db)):
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


@router.get("/conversations/{conv_id}")
def conversation_detail(conv_id: int, db: Session = Depends(get_db)):
    conv = _load(db, Conversation, conv_id)
    messages = db.scalars(select(Message).where(Message.conversation_id == conv.id)
                          .options(defer(Message.body_html)).order_by(Message.conversation_seq)).all()
    attachments = _attachments_by_message(db, [m.id for m in messages])
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
            "conversation_type": conv.conversation_type, "marketplace_id": conv.marketplace_id,
            "marketplace_source": conv.marketplace_source, "buyer_key": conv.buyer_key, "item_id": conv.item_id,
            "handoff_state": conv.handoff_state, "row_version": conv.row_version,
            "channel_source": conv.channel_source, "conversation_title": conv.conversation_title,
            "platform_status": conv.platform_status,
            "read_seq": conv.read_seq, "read_by": conv.read_by, "read_at": iso(conv.read_at),
            "aftersales": _aftersales_dict(conv),
            "mark": _conversation_mark(db, conv.id),
            "item_url": _conv_item_url(db, conv),
            "unread_count": unread_count(db, conv), "platform_unread_count": conv.platform_unread_count,
        },
        "messages": [{"id": m.id, "ebay_message_id": m.ebay_message_id, "seq": m.conversation_seq,
                      "role": m.sender_role, "direction": m.direction, "body": m.body,
                      "zh_translation": m.zh_translation, "sent_at": iso(m.sent_at),
                      "subject": m.subject, "sender_key": m.sender_key, "media": m.media or [],
                      "simulated": m.ebay_message_id.startswith(SIMULATED_PREFIX),
                      "has_html": m.id in with_html,
                      "trace": sent_from.get(m.ebay_message_id),
                      "attachments": attachments.get(m.id, [])} for m in messages],
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
        clause_ids = {e[3:].split("@")[0] for e in draft.evidence_ids or [] if e.startswith("kb:")}
        out["cited_knowledge"] = [
            {"evidence_id": f"kb:{c.clause_id}@{c.version}", "title": c.title, "text": c.text, "store_id": c.store_id,
             "marketplace_id": c.marketplace_id, "priority": c.priority, "version": c.version}
            for c in db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.clause_id.in_(clause_ids)))
        ] if clause_ids else []
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


def _require_test_delete() -> None:
    if not get_settings().test_delete_enabled:
        raise HTTPException(403, "删除会话只在测试期开放（.env TEST_DELETE_ENABLED=true）")


def _delete_test(db: Session, ids: list[int], reviewer: str, action: str) -> dict:
    from ..retention import _delete_files, delete_conversations

    counts, files = delete_conversations(db, ids)
    db.add(AuditEvent(actor=reviewer, action=action, detail={"conversation_ids": ids[:50], **counts}))
    db.commit()
    counts["files"] = _delete_files(files)
    return counts


@router.delete("/conversations/{conv_id}")
def delete_test_conversation(conv_id: int, reviewer: str = Depends(reviewer_name), db: Session = Depends(get_db)):
    """测试期：删除一个测试会话（连同消息、图片、回复任务与草稿）。从 eBay 拉取的真实会话不能删。"""
    _require_test_delete()
    conv = _load(db, Conversation, conv_id)
    if conv.channel_source == "ebay":
        raise HTTPException(409, "从 eBay 拉取的真实会话不能在这里删除")
    if db.scalar(select(ReplyJob.id).where(ReplyJob.conversation_id == conv.id, ReplyJob.state.in_(BUSY_STATES))):
        raise HTTPException(409, "AI 正在处理这个会话，稍后再删")
    return _delete_test(db, [conv.id], reviewer, "TEST_CONVERSATION_DELETED")


class PurgeTestBody(BaseModel):
    store_id: str | None = None


@router.post("/conversations/purge-test")
def purge_test_conversations(body: PurgeTestBody, reviewer: str = Depends(reviewer_name),
                             db: Session = Depends(get_db)):
    """测试期：删除全部测试会话（可只删某个店铺的）；AI 正在处理的跳过。eBay 真实会话不动。"""
    _require_test_delete()
    q = _scope(select(Conversation.id).where(Conversation.channel_source.is_(None)), Conversation.store_id)
    if body.store_id:
        q = q.where(Conversation.store_id == body.store_id)
    ids = list(db.scalars(q))
    busy = set(db.scalars(select(ReplyJob.conversation_id).where(ReplyJob.conversation_id.in_(ids),
                                                                 ReplyJob.state.in_(BUSY_STATES)))) if ids else set()
    counts = _delete_test(db, [i for i in ids if i not in busy], reviewer, "TEST_CONVERSATIONS_PURGED")
    return {**counts, "skipped_busy": len(busy)}


# ------------------------------------------------------------------ 会话标记（整段会话的当前状态 + 备注）
def _tag_dict(t: MessageTag) -> dict:
    return {"id": t.id, "name": t.name, "color": t.color, "sort": t.sort, "is_active": t.is_active}


def _conversation_mark(db: Session, conv_id: int) -> dict | None:
    row = db.execute(select(ConversationMark, MessageTag).join(MessageTag, MessageTag.id == ConversationMark.tag_id)
                     .where(ConversationMark.conversation_id == conv_id)).first()
    if row is None:
        return None
    m, t = row
    return {"tag_id": t.id, "tag": t.name, "color": t.color, "note": m.note, "marked_by": m.marked_by,
            "marked_at": iso(m.marked_at)}


@router.get("/message-tags")
def list_message_tags(include_inactive: bool = False, db: Session = Depends(get_db)):
    q = select(MessageTag).order_by(MessageTag.sort, MessageTag.id)
    if not include_inactive:
        q = q.where(MessageTag.is_active.is_(True))
    return [_tag_dict(t) for t in db.scalars(q)]


class TagBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=32)
    color: str | None = Field(default=None, pattern="^(primary|success|warning|danger|info)$")
    sort: int | None = None
    is_active: bool | None = None


@router.post("/message-tags", dependencies=[Depends(require_admin)])
def create_message_tag(body: TagBody, reviewer: str = Depends(reviewer_name), db: Session = Depends(get_db)):
    if not body.name or not body.name.strip():
        raise HTTPException(422, "请填写标记名称")
    if db.scalar(select(MessageTag.id).where(MessageTag.name == body.name.strip())):
        raise HTTPException(409, "已有同名标记")
    top = db.scalar(select(func.max(MessageTag.sort))) or 0
    tag = MessageTag(name=body.name.strip(), color=body.color or "primary", sort=body.sort or top + 10,
                     created_by=reviewer)
    db.add(tag)
    db.flush()
    return _tag_dict(tag)


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
    """给会话打标记（如 查适配中）并写备注；已有标记就改成新的。"""
    conv = _load(db, Conversation, conv_id)
    tag = _load(db, MessageTag, body.tag_id)
    mark = db.scalar(select(ConversationMark).where(ConversationMark.conversation_id == conv.id))
    if not tag.is_active and (mark is None or mark.tag_id != tag.id):
        raise HTTPException(422, "这个标记已停用")
    if mark is None:
        mark = ConversationMark(conversation_id=conv.id, store_id=conv.store_id)
        db.add(mark)
    mark.tag_id, mark.note = tag.id, (body.note or "").strip() or None
    mark.marked_by, mark.marked_at = reviewer, utcnow()
    db.add(AuditEvent(actor=reviewer, action="CONVERSATION_MARKED", store_id=conv.store_id, conversation_id=conv.id,
                      detail={"tag": tag.name}))
    db.flush()
    return _conversation_mark(db, conv.id)


@router.delete("/conversations/{conv_id}/mark")
def unmark_conversation(conv_id: int, reviewer: str = Depends(reviewer_name), db: Session = Depends(get_db)):
    conv = _load(db, Conversation, conv_id)
    n = db.execute(delete(ConversationMark).where(ConversationMark.conversation_id == conv.id)).rowcount or 0
    if n:
        db.add(AuditEvent(actor=reviewer, action="CONVERSATION_UNMARKED", store_id=conv.store_id,
                          conversation_id=conv.id, detail={}))
    return {"removed": n}


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
    last_seller = max((m.conversation_seq for m in msgs if m.sender_role == "SELLER"), default=0)
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
    handling_ms: int | None = None
    text_zh: str | None = Field(default=None, max_length=10000)  # 工作台已有的当前文本中文译文（只作对照显示）


@router.post("/conversations/{conv_id}/send")
def send_simulated(conv_id: int, body: SimulatedSendBody, reviewer: str = Depends(reviewer_name),
                   db: Session = Depends(get_db)):
    """工作台的“发送”：模拟发送，只追加到本地会话（标记为模拟），不会发到 eBay。"""
    conv = _load(db, Conversation, conv_id)
    _ensure_claim(db, conv, reviewer)
    try:
        msg, job = simulate_send(db, conv=conv, text=body.text, actor=reviewer, handling_ms=body.handling_ms,
                                 text_zh=body.text_zh)
    except SendError as e:
        _send_error(e)
    db.commit()
    # 改过的草稿或人工撰写的回复：发出后补一份中文对照（尽力而为，模型不可用时不影响发送，可在消息上点“译”）
    if not msg.zh_translation and not _mostly_chinese(msg.body):
        try:
            msg.zh_translation = _translate(db, msg.body, "中文", store_id=msg.store_id, conversation_id=conv.id)
            db.commit()
        except Exception:
            db.rollback()
    return {"message_id": msg.id, "ebay_message_id": msg.ebay_message_id, "job_id": job.id if job else None,
            "zh_translation": msg.zh_translation}


def _mostly_chinese(text: str) -> bool:
    return mostly_chinese(text)


@router.delete("/conversations/{conv_id}/simulated-messages")
def clear_simulated(conv_id: int, reviewer: str = Depends(reviewer_name), db: Session = Depends(get_db)):
    """删除本会话里模拟发送的店铺消息（只删 SIM-OUT- 开头的），方便对同一条买家消息反复测试。"""
    conv = _load(db, Conversation, conv_id)
    _ensure_claim(db, conv, reviewer)
    ids = list(db.scalars(select(Message.id).where(Message.conversation_id == conv.id,
                                                   Message.ebay_message_id.startswith(SIMULATED_PREFIX))))
    if ids:
        db.execute(delete(Message).where(Message.id.in_(ids)))
        last = db.scalar(select(func.max(Message.sent_at)).where(Message.conversation_id == conv.id))
        conv.last_message_at = last or conv.last_message_at
        conv.row_version += 1
        db.add(AuditEvent(actor=reviewer, action="SIMULATED_MESSAGES_CLEARED", store_id=conv.store_id,
                          conversation_id=conv.id, detail={"deleted": len(ids)}))
    db.commit()
    return {"deleted": len(ids)}


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
    job = _load(db, ReplyJob, job_id)
    _ensure_claim(db, _load(db, Conversation, job.conversation_id), reviewer)
    if job.state not in (*JobState.PENDING_REVIEW, JobState.RETRYABLE_ERROR):
        raise HTTPException(409, f"任务状态为 {job.state}")
    job.state = JobState.RESOLVED_NO_REPLY
    job.lock_version += 1
    for d in db.scalars(select(Draft).where(Draft.job_id == job.id, Draft.status == "ACTIVE")):
        d.status = "SUPERSEDED"
    db.add(ReviewFeedback(job_id=job.id, reviewer=reviewer, action="NO_REPLY", note=body.note))
    return {"ok": True}


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
    provider, model = usage_log.chat_model(get_settings())
    started = time.perf_counter()
    out, usage = llm.translate_with_usage(text, target_language)
    usage_log.record(db, "translate", provider=provider, model=model, usage=usage,
                     duration_ms=int((time.perf_counter() - started) * 1000), **where)
    return out


@router.post("/translate")
def translate(body: TextBody, db: Session = Depends(get_db)):
    """中文改写转目标语言、草稿回译等（第 5.2 节）。译文只用于理解，不作为证据。"""
    return {"translation": _translate(db, body.text, body.target_language)}


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
            "applicable": reason is None, "excluded_reason": reason, "tokens": tokens,
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
def _ticket_dict(t: LogisticsTicket) -> dict:
    return {"id": t.id, "io_code": t.io_code, "order_id": t.reference_no, "tracking_no": t.tracking_no,
            "type": t.type_name, "status": t.io_status_text, "trail_status": t.trail_status_text, "sm_name": t.sm_name,
            "warehouse_code": t.warehouse_code, "reply": t.reply, "reject_reason": t.reject_reason,
            "attachments": [{"name": a.get("name"), "file_id": a.get("file_id")} for a in t.attachments or []],
            "added_at": t.added_at, "replied_at": t.replied_at, "suggestion": t.suggestion,
            "suggestion_zh": t.suggestion_zh, "needs_human": t.needs_human, "suggested_at": iso(t.suggested_at),
            "handled_at": iso(t.handled_at), "handled_by": t.handled_by, "is_draft": is_draft(t), "origin": t.origin,
            "io_desc": t.io_desc, "created_by": t.created_by, "created_at": iso(t.created_at)}


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
