"""主动发信（第二批 C9，“智能客服 → 自动发信”）：规则触发的主动通知——只生成草稿进“未处理”，仍然人工审核发送，不自动发。

规则（在“自动发信”页开关、改阈值；默认全部关）：
  tracking_stalled    物流断更：轨迹 ≥ N 天（默认 5）没有新动态（已签收 / 在取件点 / 丢失损坏 / 异常的不算，那些另有流程）
  pickup_overdue      取件点超时：包裹在取件点放了 ≥ N 天（默认 LOGISTICS_PICKUP_DAYS）还没取，提醒买家去取
  return_not_shipped  退货标签已提供 N 天（默认 7）买家还没寄出
  case_due_soon       咨询（未收到货）/ 纠纷 N 小时内（默认 24）到期、还没处理（处理状态是“待处理”）：先给买家一个进度说明
只读本地数据（17TRACK 缓存的轨迹、售后单、订单），不连外部系统。每次扫描每 PROACTIVE_SCAN_MINUTES 分钟一次（0 = 不自动扫）。
触发后：建一个回复任务（状态“待审核”，原因 PROACTIVE_NOTICE）+ 一份模板草稿（按买家语言：英 / 德 / 法 / 意 / 西，附中文对照），
会话出现在工作台“未处理”，客服在会话里改、发（合规检查、5 分钟可撤回都照旧）；也可以点“AI 起草”。
限流：同一会话同一规则 7 天最多触发一次；会话已经有待处理的回复、黑名单买家、eBay 系统会话不触发。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import session_scope, utcnow
from .models import AppSetting, AuditEvent, Conversation, Draft, EbayCase, EbayOrder, JobState, ReplyJob

log = logging.getLogger(__name__)
SETTING_KEY = "proactive_rules"
COOLDOWN_DAYS = 7
SOURCE = "proactive"  # 草稿作者、回复任务锚点前缀
RULES = {
    "tracking_stalled": {"label": "物流断更", "unit": "天", "default": 5, "intent": "delivery_delay"},
    "pickup_overdue": {"label": "取件点超时", "unit": "天", "default": None, "intent": "shipping_status"},
    "return_not_shipped": {"label": "退货标签已提供未寄出", "unit": "天", "default": 7, "intent": "return_refund"},
    "case_due_soon": {"label": "咨询 / 纠纷快到期未处理", "unit": "小时", "default": 24, "intent": "delivery_delay"},
}
MK_LANG = {"EBAY_DE": "de", "EBAY_AT": "de", "EBAY_CH": "de", "EBAY_FR": "fr", "EBAY_IT": "it", "EBAY_ES": "es"}
# 退货里“等买家寄回”的状态（eBay 原样状态，大小写不敏感，包含即可）
RETURN_WAITING = ("WAITING_FOR_RETURN_SHIPMENT", "READY_FOR_SHIPPING", "LABEL_PROVIDED", "LABEL_SENT", "RETURN_LABEL",
                  "WAITING_FOR_RETURN_LABEL_SHIPMENT")
RETURN_DONE = ("ITEM_SHIPPED", "ITEM_DELIVERED", "CLOSED", "REFUND")

TEMPLATES = {
    "tracking_stalled": {
        "en": "Hello, we noticed that the tracking for your order {order} ({carrier} {tracking}) has not updated for {days} days. "
              "We have contacted the carrier to check on your parcel and will update you as soon as we hear back. Thank you for your patience.",
        "de": "Hallo, uns ist aufgefallen, dass die Sendungsverfolgung Ihrer Bestellung {order} ({carrier} {tracking}) seit {days} Tagen "
              "nicht aktualisiert wurde. Wir haben beim Paketdienst nachgefragt und melden uns, sobald wir eine Antwort haben. Vielen Dank für Ihre Geduld.",
        "fr": "Bonjour, nous avons remarqué que le suivi de votre commande {order} ({carrier} {tracking}) n'a pas été mis à jour depuis {days} jours. "
              "Nous avons contacté le transporteur et reviendrons vers vous dès que possible. Merci de votre patience.",
        "it": "Buongiorno, abbiamo notato che il tracciamento del suo ordine {order} ({carrier} {tracking}) non si aggiorna da {days} giorni. "
              "Abbiamo contattato il corriere e la aggiorneremo appena possibile. Grazie per la pazienza.",
        "es": "Hola, hemos visto que el seguimiento de su pedido {order} ({carrier} {tracking}) no se actualiza desde hace {days} días. "
              "Hemos contactado con la empresa de transporte y le informaremos en cuanto tengamos noticias. Gracias por su paciencia.",
        "zh": "您好，我们注意到您的订单 {order}（{carrier} {tracking}）的物流已经 {days} 天没有更新。我们已联系承运商核实，有结果会第一时间通知您，感谢耐心等待。",
    },
    "pickup_overdue": {
        "en": "Hello, your parcel for order {order} ({carrier} {tracking}) has been waiting at the pickup point for {days} days. "
              "Please collect it soon with your tracking number — pickup points only keep parcels for a limited time before returning them.",
        "de": "Hallo, Ihr Paket zur Bestellung {order} ({carrier} {tracking}) liegt seit {days} Tagen in der Abholstation bzw. Filiale. "
              "Bitte holen Sie es bald mit der Sendungsnummer ab – Pakete werden nur begrenzt aufbewahrt und danach zurückgeschickt.",
        "fr": "Bonjour, votre colis de la commande {order} ({carrier} {tracking}) attend au point de retrait depuis {days} jours. "
              "Merci de le récupérer rapidement avec votre numéro de suivi : il ne sera conservé que pour une durée limitée.",
        "it": "Buongiorno, il pacco dell'ordine {order} ({carrier} {tracking}) è in giacenza al punto di ritiro da {days} giorni. "
              "La preghiamo di ritirarlo presto con il numero di tracciamento: viene conservato solo per un periodo limitato.",
        "es": "Hola, el paquete de su pedido {order} ({carrier} {tracking}) lleva {days} días esperando en el punto de recogida. "
              "Por favor, recójalo pronto con su número de seguimiento: solo se guarda durante un tiempo limitado.",
        "zh": "您好，您订单 {order}（{carrier} {tracking}）的包裹已在取件点放了 {days} 天，请尽快凭运单号取件，取件点只保留有限时间，逾期会退回。",
    },
    "return_not_shipped": {
        "en": "Hello, a return label for your return {ref} was provided {days} days ago, but the item has not been sent yet. "
              "Please ship it back with the label at your convenience so we can process your return. If you have any problem with the label, just let us know.",
        "de": "Hallo, das Rücksendeetikett für Ihre Rücksendung {ref} wurde vor {days} Tagen bereitgestellt, der Artikel wurde aber noch nicht verschickt. "
              "Bitte senden Sie ihn mit dem Etikett zurück, damit wir die Rücksendung bearbeiten können. Bei Problemen mit dem Etikett melden Sie sich gerne.",
        "fr": "Bonjour, l'étiquette de retour pour votre retour {ref} a été fournie il y a {days} jours, mais l'article n'a pas encore été expédié. "
              "Merci de le renvoyer avec l'étiquette afin que nous puissions traiter votre retour. En cas de souci avec l'étiquette, contactez-nous.",
        "it": "Buongiorno, l'etichetta per il reso {ref} è stata fornita {days} giorni fa, ma l'articolo non è ancora stato spedito. "
              "La preghiamo di rispedirlo con l'etichetta così possiamo gestire il reso. In caso di problemi con l'etichetta ci scriva pure.",
        "es": "Hola, la etiqueta de devolución {ref} se facilitó hace {days} días, pero el artículo aún no se ha enviado. "
              "Por favor, envíelo con la etiqueta para que podamos tramitar la devolución. Si tiene algún problema con la etiqueta, díganoslo.",
        "zh": "您好，您的退货 {ref} 的退货标签在 {days} 天前已经提供，但商品还没有寄出。请方便时用标签寄回，我们收到后会处理退货；标签有问题请告诉我们。",
    },
    "case_due_soon": {
        "en": "Hello, regarding your request {ref}: we are checking it with the carrier and our warehouse and will get back to you shortly. Thank you for your patience.",
        "de": "Hallo, zu Ihrer Anfrage {ref}: Wir prüfen den Vorgang gerade mit dem Paketdienst und unserem Lager und melden uns in Kürze. Vielen Dank für Ihre Geduld.",
        "fr": "Bonjour, concernant votre demande {ref} : nous vérifions actuellement auprès du transporteur et de notre entrepôt et revenons vers vous très vite. Merci de votre patience.",
        "it": "Buongiorno, riguardo alla sua richiesta {ref}: stiamo verificando con il corriere e il nostro magazzino e la ricontatteremo a breve. Grazie per la pazienza.",
        "es": "Hola, sobre su solicitud {ref}: lo estamos comprobando con la empresa de transporte y nuestro almacén y le responderemos en breve. Gracias por su paciencia.",
        "zh": "您好，关于您的申请 {ref}：我们正在和承运商、仓库核实，很快给您答复，感谢耐心等待。",
    },
}


# ------------------------------------------------------------------ 规则设置
def defaults() -> dict:
    s = get_settings()
    return {k: {"enabled": False, "threshold": v["default"] if v["default"] is not None else s.logistics_pickup_days}
            for k, v in RULES.items()}


def get_rules(db: Session) -> dict:
    row = db.get(AppSetting, SETTING_KEY)
    out = defaults()
    for k, v in ((row.value if row else None) or {}).items():
        if k in out and isinstance(v, dict):
            out[k] = {"enabled": bool(v.get("enabled", out[k]["enabled"])),
                      "threshold": float(v.get("threshold", out[k]["threshold"]))}
    return out


def set_rules(db: Session, values: dict, actor: str) -> dict:
    cur = get_rules(db)
    for k, v in (values or {}).items():
        if k not in RULES or not isinstance(v, dict):
            raise ValueError(f"没有这条规则：{k}")
        th = float(v.get("threshold", cur[k]["threshold"]))
        if th <= 0 or th > (720 if RULES[k]["unit"] == "小时" else 60):
            raise ValueError(f"{RULES[k]['label']}的阈值不对：{th}")
        cur[k] = {"enabled": bool(v.get("enabled", cur[k]["enabled"])), "threshold": th}
    row = db.get(AppSetting, SETTING_KEY)
    if row is None:
        row = AppSetting(key=SETTING_KEY, value=cur)
        db.add(row)
    row.value, row.updated_by, row.updated_at = cur, actor[:64], utcnow()
    db.add(AuditEvent(actor=actor[:64], action="PROACTIVE_RULES_SAVED", detail=cur))
    return cur


# ------------------------------------------------------------------ 候选
def _conv_for(db: Session, store_id: str, buyer: str | None) -> Conversation | None:
    if not buyer:
        return None
    return db.scalar(select(Conversation).where(Conversation.store_id == store_id, func.lower(Conversation.buyer_key) == buyer.lower(),
                                                Conversation.conversation_type != "FROM_EBAY")
                     .order_by(Conversation.last_message_at.desc(), Conversation.id.desc()).limit(1))


def _tracking_candidates(db: Session, rule: str, threshold: float, now: datetime) -> list[dict]:
    from .logistics.assess import assess
    from .models import TrackingNumber

    out = []
    for tn in db.scalars(select(TrackingNumber).where(TrackingNumber.order_id.is_not(None), TrackingNumber.track.is_not(None))):
        a = assess({"tracking_number": tn.number, "tracking": tn.track or {}}, tracking_enabled=True, now=now)
        days = a.get("days_without_update")
        if days is None or days < threshold:
            continue
        if rule == "tracking_stalled" and a["stage"] not in ("STALLED", "RESHIP_REVIEW", "OK", "NO_INFO"):
            continue  # 已签收、在取件点、丢失损坏、异常：另有流程
        if rule == "pickup_overdue" and a["stage"] not in ("PICKUP", "PICKUP_OVERDUE"):
            continue
        o = db.scalar(select(EbayOrder).where(EbayOrder.order_id == tn.order_id).limit(1))
        if o is None or o.cancelled or o.refunded:
            continue
        out.append({"store_id": o.store_id, "buyer": o.buyer, "ref": tn.number, "order": o.order_id,
                    "vars": {"order": o.order_id, "carrier": tn.carrier_name or "", "tracking": tn.number, "days": int(days)}})
    return out


def _return_candidates(db: Session, threshold: float, now: datetime) -> list[dict]:
    from .models import ConversationEvent

    out = []
    for c in db.scalars(select(EbayCase).where(EbayCase.kind == "RETURN", EbayCase.is_open.is_(True), EbayCase.hidden_at.is_(None))):
        st = (c.status or "").upper()
        if any(x in st for x in RETURN_DONE):
            continue
        since = None
        if any(x in st for x in RETURN_WAITING):
            since = c.status_changed_at or c.opened_at
        ev = db.scalar(select(ConversationEvent.occurred_at).where(  # 本系统里提供了标签（接受退货）
            ConversationEvent.case_id == c.external_id, ConversationEvent.event_type == "SELLER_RETURN_ACCEPT")
            .order_by(ConversationEvent.occurred_at.desc()).limit(1))
        since = ev or since
        if since is None or (now.replace(tzinfo=None) - since).days < threshold:
            continue
        out.append({"store_id": c.store_id, "buyer": c.buyer, "ref": c.external_id, "order": c.order_id,
                    "vars": {"ref": c.external_id, "days": (now.replace(tzinfo=None) - since).days}})
    return out


def _case_candidates(db: Session, threshold_hours: float, now: datetime) -> list[dict]:
    n = now.replace(tzinfo=None)
    out = []
    for c in db.scalars(select(EbayCase).where(EbayCase.kind.in_(("INQUIRY", "CASE")), EbayCase.is_open.is_(True),
                                              EbayCase.hidden_at.is_(None), EbayCase.work_state.is_(None),
                                              EbayCase.due_at.is_not(None), EbayCase.due_at > n,
                                              EbayCase.due_at <= n + timedelta(hours=threshold_hours))):
        out.append({"store_id": c.store_id, "buyer": c.buyer, "ref": c.external_id, "order": c.order_id,
                    "vars": {"ref": c.external_id, "hours": round((c.due_at - n).total_seconds() / 3600, 1)}})
    return out


def candidates(db: Session, rule: str, threshold: float, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    if rule in ("tracking_stalled", "pickup_overdue"):
        return _tracking_candidates(db, rule, threshold, now)
    if rule == "return_not_shipped":
        return _return_candidates(db, threshold, now)
    if rule == "case_due_soon":
        return _case_candidates(db, threshold, now)
    return []


# ------------------------------------------------------------------ 生成草稿
def language_of(db: Session, conv: Conversation) -> str:
    from .evaluation.autolabel import language
    from .models import Message

    last = db.scalar(select(Message.body).where(Message.conversation_id == conv.id, Message.sender_role == "BUYER")
                     .order_by(Message.conversation_seq.desc()).limit(1))
    lang = language(last or "")
    if lang in TEMPLATES["tracking_stalled"] and lang != "zh":
        return lang
    return MK_LANG.get(conv.marketplace_id or "", "en")


def skip_reason(db: Session, conv: Conversation | None, rule: str, now: datetime) -> str | None:
    """这个会话不触发的原因；None = 可以触发。"""
    from .customers import blacklist_entry

    if conv is None:
        return "对不上买家会话"
    if blacklist_entry(db, conv.store_id, conv.buyer_key):
        return "黑名单买家"
    job = db.scalar(select(ReplyJob).where(ReplyJob.conversation_id == conv.id, ReplyJob.state != JobState.CANCELLED_STALE)
                    .order_by(ReplyJob.id.desc()).limit(1))
    if job is not None and job.state in (*JobState.PENDING_REVIEW, JobState.RECEIVED, JobState.RUNNING, JobState.SENDING):
        return "会话里已经有待处理的回复"
    recent = db.scalar(select(ReplyJob.id).where(ReplyJob.conversation_id == conv.id,
                                                 ReplyJob.anchor_source_message_id.like(f"{SOURCE}:{rule}:%"),
                                                 ReplyJob.created_at >= now.replace(tzinfo=None) - timedelta(days=COOLDOWN_DAYS)).limit(1))
    if recent is not None:
        return f"{COOLDOWN_DAYS} 天内已经触发过"
    return None


def create_notice(db: Session, conv: Conversation, rule: str, cand: dict, now: datetime) -> ReplyJob:
    """建“待审核”的回复任务 + 模板草稿（作者 proactive），会话进工作台“未处理”。"""
    lang = language_of(db, conv)
    text = TEMPLATES[rule][lang].format(**{"order": "", "carrier": "", "tracking": "", "days": "", "ref": "", **cand["vars"]})
    zh = TEMPLATES[rule]["zh"].format(**{"order": "", "carrier": "", "tracking": "", "days": "", "ref": "", **cand["vars"]})
    text, zh = " ".join(text.replace("( ", "(").replace(" )", ")").split()), " ".join(zh.split())
    job = ReplyJob(store_id=conv.store_id, conversation_id=conv.id,
                   anchor_source_message_id=f"{SOURCE}:{rule}:{cand['ref']}:{now:%Y%m%d%H%M}"[:128],
                   state=JobState.REVIEW_REQUIRED, ready_at=now.replace(tzinfo=None), risk_level="L1",
                   decision_reasons=["PROACTIVE_NOTICE"])
    db.add(job)
    db.flush()
    db.add(Draft(job_id=job.id, run_generation=job.run_generation, version=1, author=SOURCE, status="ACTIVE",
                 conversation_row_version=conv.row_version, text=text, text_zh=zh, language=lang, intent=RULES[rule]["intent"],
                 intents=[RULES[rule]["intent"]], action="answer", risk_level="L1", decision="REVIEW_REQUIRED",
                 review_reasons=["PROACTIVE_NOTICE"],
                 reasoning_zh=f"主动发信规则“{RULES[rule]['label']}”触发（{cand['ref']}），模板草稿，请核对后再发"))
    db.add(AuditEvent(actor="proactive", action="PROACTIVE_NOTICE_DRAFTED", store_id=conv.store_id, conversation_id=conv.id,
                      detail={"rule": rule, "ref": cand["ref"], "job_id": job.id, "language": lang}))
    return job


def scan(db: Session, *, dry_run: bool = False, now: datetime | None = None, rules: list[str] | None = None) -> dict:
    """按开着的规则扫一遍；dry_run 只列候选不建草稿。返回每条规则的候选 / 生成 / 跳过（及原因）。"""
    now = now or datetime.now(timezone.utc)
    cfg = get_rules(db)
    out: dict[str, dict] = {}
    seen: set[tuple[int, str]] = set()
    for rule, rc in cfg.items():
        if rules is not None and rule not in rules:
            continue
        if not rc["enabled"] and rules is None:
            continue
        res = {"candidates": 0, "drafted": 0, "skipped": {}, "items": []}
        for cand in candidates(db, rule, rc["threshold"], now):
            res["candidates"] += 1
            conv = _conv_for(db, cand["store_id"], cand["buyer"])
            why = skip_reason(db, conv, rule, now)
            if why is None and (conv.id, rule) in seen:
                why = "这次扫描已触发"
            item = {"ref": cand["ref"], "order": cand.get("order"), "store_id": cand["store_id"],
                    "conversation_id": conv.id if conv else None, "skip": why}
            if why:
                res["skipped"][why] = res["skipped"].get(why, 0) + 1
            elif not dry_run:
                job = create_notice(db, conv, rule, cand, now)
                item["job_id"] = job.id
                res["drafted"] += 1
                seen.add((conv.id, rule))
            else:
                seen.add((conv.id, rule))
            res["items"].append(item)
        res["items"] = res["items"][:100]
        out[rule] = res
    db.flush()
    return out


def notices(db: Session, limit: int = 200) -> list[dict]:
    """最近触发的主动通知：规则、会话、状态（按回复任务状态：待审核 / 已发出 / 已作废）。"""
    rows = db.scalars(select(ReplyJob).where(ReplyJob.anchor_source_message_id.like(f"{SOURCE}:%"))
                      .order_by(ReplyJob.id.desc()).limit(limit)).all()
    out = []
    for j in rows:
        _, rule, ref = (j.anchor_source_message_id.split(":", 3) + ["", ""])[:3]
        out.append({"job_id": j.id, "rule": rule, "rule_label": RULES.get(rule, {}).get("label", rule), "ref": ref,
                    "conversation_id": j.conversation_id, "store_id": j.store_id, "state": j.state,
                    "created_at": j.created_at.isoformat(timespec="seconds") + "Z" if j.created_at else None})
    return out


def dismiss(db: Session, job_id: int, actor: str) -> ReplyJob:
    """不发了：任务作废（草稿不再显示），记审计。"""
    job = db.get(ReplyJob, job_id)
    if job is None or not (job.anchor_source_message_id or "").startswith(f"{SOURCE}:"):
        raise ValueError("不是主动发信的任务")
    if job.state not in JobState.PENDING_REVIEW:
        raise ValueError(f"任务状态是 {job.state}，不能作废")
    job.state = JobState.CANCELLED_STALE
    job.lock_version += 1
    db.add(AuditEvent(actor=actor[:64], action="PROACTIVE_NOTICE_DISMISSED", store_id=job.store_id,
                      conversation_id=job.conversation_id, detail={"job_id": job.id}))
    return job


class ProactiveWorker:
    """每 PROACTIVE_SCAN_MINUTES 分钟按开着的规则扫一次（0 = 不自动扫）。"""

    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if get_settings().proactive_scan_minutes <= 0 or (self._thread and self._thread.is_alive()):
            return False
        self._thread = threading.Thread(target=self._loop, name="proactive", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> dict:
        with session_scope() as db:
            return scan(db)

    def _loop(self) -> None:
        while not self._stop.wait(max(1, get_settings().proactive_scan_minutes) * 60):
            try:
                self.run_once()
            except Exception:  # noqa: BLE001
                log.exception("主动发信扫描失败")


worker = ProactiveWorker()
