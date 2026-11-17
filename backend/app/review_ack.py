"""转人工的买家来信先回应（“事事有回应”）：买家来信进了人工审核队列，REVIEW_ACK_DELAY_MINUTES 分钟内还没人回复的，
自动给买家发一条“已收到，同事正在处理，会尽快回复”（买家语言、固定话术，不承诺任何结果；目前为模拟发送）。

- 每个待审任务最多发一次；买家接着追问时，上一条回执之后还没有正式回复、且在 REVIEW_ACK_COOLDOWN_HOURS 小时内的不再重复发；
- 规则：会话最后一句永远是我们发的。买家只是致谢：不发“已收到”，改发一句简短致谢（CLOSING_TEXT）；已人工接管的会话超时没人回也照发；
- 不发：eBay 系统消息会话、黑名单买家、来信已超过 REVIEW_ACK_MAX_AGE_HOURS 小时（刚开启时不给积压的旧消息补发）；
- 有同事认领着这个会话时先不发，等认领过期后再看；
- 回执不改会话版本号：待审的 AI 草稿照样可以直接批准发送，正式回复以人工审核为准。
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import session_scope, utcnow
from .models import AuditEvent, Conversation, Draft, JobState, Message, ReplyJob, WorkClaim
from .sending.service import ACK_PREFIX, is_ack

log = logging.getLogger(__name__)
ACTOR = "AI（已收到回执）"
SKIP_INTENTS = {"thanks_closing"}  # 只是致谢、结束语：不回“已收到”，改回简短致谢（CLOSING_TEXT）
ACK_TEXT = {
    "en": "Thank you for your message. We have received it and our team is already looking into it. "
          "We will get back to you as soon as possible.",
    "de": "Vielen Dank für Ihre Nachricht. Wir haben sie erhalten und unser Team kümmert sich bereits darum. "
          "Wir melden uns so schnell wie möglich bei Ihnen.",
    "fr": "Merci pour votre message. Nous l'avons bien reçu et notre équipe s'en occupe déjà. "
          "Nous reviendrons vers vous dans les meilleurs délais.",
    "it": "Grazie per il messaggio. L'abbiamo ricevuto e il nostro team se ne sta già occupando. "
          "Le risponderemo il prima possibile.",
    "es": "Gracias por su mensaje. Lo hemos recibido y nuestro equipo ya se está ocupando de ello. "
          "Le responderemos lo antes posible.",
    "zh": "感谢您的来信。我们已经收到，同事正在处理，会尽快回复您。",
}
ACK_ZH = ACK_TEXT["zh"]
CLOSING_TEXT = {  # 买家只是致谢 / 结束语时的回复（工作台“结束会话”也用这段）
    "en": "Thank you for your message! If you have any further questions, please feel free to contact us at any time.",
    "de": "Vielen Dank für Ihre Nachricht! Wenn Sie weitere Fragen haben, können Sie uns jederzeit gerne kontaktieren.",
    "fr": "Merci beaucoup pour votre message ! Si vous avez d'autres questions, n'hésitez pas à nous contacter à tout moment.",
    "it": "Grazie mille per il suo messaggio! Se ha altre domande, non esiti a contattarci in qualsiasi momento.",
    "es": "¡Muchas gracias por su mensaje! Si tiene alguna otra pregunta, no dude en ponerse en contacto con nosotros.",
    "zh": "感谢您的来信！如果还有其他问题，欢迎随时联系我们。",
}
CLOSING_ZH = CLOSING_TEXT["zh"]
_SITE_LANG = {"EBAY_DE": "de", "EBAY_AT": "de", "EBAY_CH": "de", "EBAY_FR": "fr", "EBAY_BE": "fr", "EBAY_IT": "it",
              "EBAY_ES": "es"}


def _language(conv: Conversation, draft: Draft | None) -> str:
    lang = ((draft.language if draft else None) or "").lower()[:2]
    return lang if lang in ACK_TEXT else _SITE_LANG.get(conv.marketplace_id or "", "en")


def _skip(job: ReplyJob, reason: str) -> str:
    job.ack_status = reason[:64]
    return reason


def _ack_one(db: Session, job: ReplyJob, now: datetime) -> str:
    from .guards.compliance import ComplianceChecker
    from .ingestion.service import _append_message

    s = get_settings()
    conv = db.get(Conversation, job.conversation_id)
    if conv is None or conv.conversation_type == "FROM_EBAY":
        return _skip(job, "不需要：eBay 系统消息会话")
    from .customers import blacklist_entry

    if blacklist_entry(db, conv.store_id, conv.buyer_key):
        return _skip(job, "不需要：黑名单买家，由客服处理")
    anchor = db.scalar(select(Message).where(Message.conversation_id == conv.id,
                                             Message.ebay_message_id == job.anchor_source_message_id))
    asked_at = anchor.sent_at if anchor else job.created_at
    if asked_at < now - timedelta(hours=s.review_ack_max_age_hours):
        return _skip(job, "不需要：来信太久了")
    if asked_at > now - timedelta(minutes=s.review_ack_delay_minutes):
        return "WAIT"
    sellers = db.scalars(select(Message).where(Message.conversation_id == conv.id, Message.sender_role == "SELLER")
                         .order_by(Message.sent_at.desc()).limit(20)).all()
    if any(m.sent_at >= asked_at for m in sellers):
        return _skip(job, "不需要：来信后已有回复")
    last_ack = next((m for m in sellers if is_ack(m.ebay_message_id) or m.auto_kind == "ACK"), None)
    last_real = next((m for m in sellers if not (is_ack(m.ebay_message_id) or m.auto_kind == "ACK")), None)
    if last_ack and (last_real is None or last_real.sent_at < last_ack.sent_at) and \
            last_ack.sent_at > now - timedelta(hours=s.review_ack_cooldown_hours):
        return _skip(job, "不需要：刚发过回执，还没正式回复")
    draft = db.scalar(select(Draft).where(Draft.job_id == job.id, Draft.status == "ACTIVE").order_by(Draft.id.desc()).limit(1))
    closing = draft is not None and bool(draft.intents) and set(draft.intents) <= SKIP_INTENTS  # 只是致谢：回简短致谢
    claim = db.scalar(select(WorkClaim).where(WorkClaim.store_id == conv.store_id, WorkClaim.conversation_id == conv.id))
    if claim and claim.expires_at > now:
        return "WAIT"  # 同事正在处理：等认领过期再看
    lang = _language(conv, draft)
    text, text_zh = (CLOSING_TEXT[lang], CLOSING_ZH) if closing else (ACK_TEXT[lang], ACK_ZH)
    if ComplianceChecker().check(text).result == "BLOCK":
        return _skip(job, "没发：合规检查不通过")
    from .sending.outbox import enqueue

    job.ack_status, job.ack_sent_at = "QUEUED", now  # 发出后由发件箱回写 SENT；撤回记 RECALLED，不再重发
    ob = enqueue(db, conv, text, actor=ACTOR, source="ack", zh=text_zh if lang != "zh" else None,
                 meta={"ack_job_id": job.id}, prefix=ACK_PREFIX)
    db.add(AuditEvent(actor=ACTOR, action="REVIEW_ACK_QUEUED", store_id=conv.store_id, conversation_id=conv.id,
                      detail={"job_id": job.id, "language": lang, "outbound_id": ob.id,
                              "kind": "closing" if closing else "ack"}))
    return "SENT"


def run(db: Session, now: datetime | None = None) -> dict:
    """扫一遍待审任务，到时间、还没人回复的发回执。返回各结果的数量。"""
    s = get_settings()
    now = now or utcnow()
    stats: dict[str, int] = {}
    if not s.review_ack_enabled:
        return stats
    jobs = db.scalars(select(ReplyJob).where(ReplyJob.state.in_(JobState.PENDING_REVIEW), ReplyJob.ack_status.is_(None),
                                             ReplyJob.created_at >= now - timedelta(hours=s.review_ack_max_age_hours + 1))
                      .order_by(ReplyJob.id)).all()
    for job in jobs:
        try:
            with db.begin_nested():
                r = _ack_one(db, job, now)
        except Exception:  # noqa: BLE001  单条失败不影响其他
            log.exception("回执 job=%s 失败", job.id)
            r = "ERROR"
        key = r if r in ("SENT", "WAIT", "ERROR") else "SKIPPED"
        stats[key] = stats.get(key, 0) + 1
    return stats


class ReviewAckWorker:
    """每分钟看一次待审队列。"""

    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if not get_settings().review_ack_enabled:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="review-ack", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        self._stop.wait(60)
        while not self._stop.is_set():
            try:
                with session_scope() as db:
                    stats = run(db)
                if stats.get("SENT"):
                    log.info("已给 %s 个转人工的买家发“已收到”回执", stats["SENT"])
            except Exception:  # noqa: BLE001
                log.exception("回执任务出错")
            self._stop.wait(60)


worker = ReviewAckWorker()
