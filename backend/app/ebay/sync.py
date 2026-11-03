"""eBay 消息同步（只读）：把店铺的买家会话和 eBay 系统消息拉到本系统（第 10 章“接入”的轮询方式）。

- 两类会话分开拉：FROM_MEMBERS（买家）按上次成功时间往前重叠 1 小时增量拉取；
  FROM_EBAY（系统消息）接口不支持按时间过滤，只看前几页并按最新消息时间过滤。
- 会话列表里最新消息已入库的会话直接跳过，只对有新消息的会话调 getConversation。
- 按 (店铺, 平台消息 ID) 去重，重复拉取不会产生重复消息。
- 保留期：早于该店铺保留截止时间（DATA_RETENTION_DAYS，见 retention.py）的消息不入库，否则清理后
  会话一来新消息，旧消息又被整段拉回来；首次同步回溯天数也不超过保留天数。
- 默认只入库展示，不建回复任务（EBAY_SYNC_CREATE_REPLY_JOBS=false），真实买家消息不会被发给模型。
- 会话标记 channel_source=ebay，只读阶段发送服务会拒绝对它发送。
- 会话站点：Message API 不返回站点，同步后按会话关联的刊登用 Browse API 查出站点并回填（resolve_marketplaces）。
- 多店铺：逐个店铺同步，每个店铺用自己的秘钥（store_credentials 解密）换访问令牌，会话、消息、同步进度都按店铺记录；
  一个店铺失败（如秘钥失效）不影响其他店铺。店铺清单见 ebay/stores.py。
"""
from __future__ import annotations

import logging
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import session_scope, utcnow
from ..ingestion.service import _append_message, get_or_create_conversation, ingest_buyer_message
from ..media.service import register_ebay_media
from ..models import AuditEvent, Conversation, EbayListing, EbaySyncState, Message, Store
from ..read_state import INITIAL_READER, apply_platform_unread, mark_read
from ..retention import cutoff_for, store_cutoff
from .auth import EbayAuthError, for_store
from .browse_api import BrowseClient, listing_marketplace_of, normalize_marketplace
from .html_text import split_body
from .message_api import EbayApiError, MessageApiClient
from .stores import EbayStoreTarget, ebay_not_ready_reasons, ebay_store_targets

log = logging.getLogger(__name__)

CONVERSATION_TYPES = ("FROM_MEMBERS", "FROM_EBAY")
OVERLAP = timedelta(hours=1)
FROM_EBAY_MAX_PAGES = 4
MAX_LISTING_LOOKUPS = 100  # 每次同步最多查多少个刊登的站点
LISTING_RETRY = timedelta(days=7)  # 查不到/出错的刊登隔多久再查
AUTO_REPLY_MAX_AGE = timedelta(hours=24)  # 店铺开了 AI 自动回复时，只把这么新的买家来信交给 AI

_run_lock = threading.Lock()
# 每个店铺一把锁：定时全量拉取与推送触发的单店铺拉取不会同时写同一个店铺
_store_locks: dict[str, threading.Lock] = {}
_store_locks_guard = threading.Lock()


def store_lock(store_id: str) -> threading.Lock:
    with _store_locks_guard:
        return _store_locks.setdefault(store_id, threading.Lock())


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def format_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def ensure_ebay_store(db: Session) -> Store:
    s = get_settings()
    store = db.get(Store, s.ebay_store_id)
    if store is None:
        store = Store(id=s.ebay_store_id, name=s.ebay_store_name or s.ebay_store_id,
                      ebay_user_id=s.ebay_seller_username, timezone=s.ebay_store_timezone,
                      default_marketplace_id=s.ebay_store_marketplace)
        db.add(store)
        db.flush()
    elif s.ebay_seller_username and store.ebay_user_id != s.ebay_seller_username:
        store.ebay_user_id = s.ebay_seller_username
    return store


def infer_seller(conversations: list[dict]) -> str | None:
    """店铺自己的用户名：出现在所有买家会话参与方里的那个人。只有一个会话时无法判断。"""
    common: set[str] | None = None
    for c in conversations:
        m = c.get("latestMessage") or {}
        people = {u for u in (m.get("senderUsername"), m.get("recipientUsername")) if u}
        if not people:
            continue
        common = people if common is None else common & people
    if common and len(common) == 1 and len(conversations) > 1:
        return next(iter(common))
    return None


def resolve_marketplaces(browse: BrowseClient | None = None, limit: int = MAX_LISTING_LOOKUPS) -> dict:
    """给站点未知、但关联了刊登的真实会话（买家会话和 eBay 系统通知）补上站点（Message API 的会话本身不带站点）。

    站点只取自刊登（Browse API 的 listingMarketplaceId），不按语言、币种或买家自述推断（第 11.3 节）。
    刊登站点不在 EBAY_STORE_MARKETPLACES 内时，会话站点保持 UNKNOWN 并记下原因。
    """
    s = get_settings()
    allowed = set(s.ebay_marketplace_set)
    browse = browse or BrowseClient()
    stats: Counter = Counter({k: 0 for k in ("conversations_updated", "listings_looked_up", "listings_not_found")})
    with session_scope() as db:
        pending = db.scalars(select(Conversation).where(
            Conversation.channel_source == "ebay",
            Conversation.marketplace_id == "UNKNOWN", Conversation.item_id.isnot(None))).all()
        item_ids = sorted({c.item_id for c in pending})
        item_store = {c.item_id: c.store_id for c in pending}  # 刊登缓存记在会话所属店铺名下
    now = utcnow()
    for item_id in item_ids:
        with session_scope() as db:
            cached = db.get(EbayListing, item_id)
            if cached and (cached.lookup_status == "FOUND" or now - cached.fetched_at < LISTING_RETRY):
                continue
        if stats["listings_looked_up"] >= limit:
            stats["deferred"] += 1
            continue
        stats["listings_looked_up"] += 1
        try:
            item = browse.get_item_by_legacy_id(item_id)
            status, error = ("FOUND", None) if item else ("NOT_FOUND", None)
        except (EbayAuthError, EbayApiError) as e:
            item, status, error = None, "ERROR", str(e)[:1000]
            if isinstance(e, EbayAuthError):
                stats["error"] = str(e)
                break
        if status == "NOT_FOUND":
            stats["listings_not_found"] += 1
        raw = listing_marketplace_of(item) if item else None
        price = (item or {}).get("price") or {}
        with session_scope() as db:
            row = db.get(EbayListing, item_id) or EbayListing(item_id=item_id, store_id=item_store[item_id])
            row.lookup_status, row.last_error, row.fetched_at = status, error, now
            if item:
                row.listing_marketplace_id = raw
                row.marketplace_id = normalize_marketplace(raw)
                row.title = (item.get("title") or "")[:300] or None
                row.item_web_url = (item.get("itemWebUrl") or "")[:500] or None
                row.price_value, row.price_currency = price.get("value"), price.get("currency")
            db.merge(row)

    with session_scope() as db:
        for conv in db.scalars(select(Conversation).where(
                Conversation.channel_source == "ebay", Conversation.marketplace_id == "UNKNOWN",
                Conversation.item_id.isnot(None))).all():
            listing = db.get(EbayListing, conv.item_id)
            if not listing or not listing.marketplace_id:
                continue
            if listing.marketplace_id in allowed:
                conv.marketplace_id, conv.marketplace_source = listing.marketplace_id, "listing"
                stats["conversations_updated"] += 1
                stats[f"site_{listing.marketplace_id}"] += 1
            else:
                conv.marketplace_source = f"listing_not_enabled:{listing.listing_marketplace_id}"
                stats["not_enabled"] += 1
    stats["browse_calls"] = browse.calls
    return dict(stats)


class EbayMessageSync:
    def __init__(self, client: MessageApiClient | None = None, browse: BrowseClient | None = None,
                 client_factory=None):
        # client 只在测试里注入（所有店铺共用这一个假客户端）；正常运行按店铺各建一个，用各自的秘钥
        self._fixed_client = client
        self.client_factory = client_factory or (lambda t: MessageApiClient(for_store(t.store_id)))
        self.browse = browse

    def run(self) -> dict:
        reasons = ebay_not_ready_reasons()
        if reasons:
            return {"ok": False, "error": "；".join(reasons)}
        if not _run_lock.acquire(blocking=False):
            return {"ok": False, "error": "上一次同步还在进行中"}
        try:
            result: dict = {"ok": True, "stores": {}}
            calls = 0
            targets = ebay_store_targets()
            for target in targets:
                store_result, n = self.sync_target(target)
                calls += n
                if not store_result["ok"]:
                    result["ok"] = False
                result["stores"][target.store_id] = store_result
            if len(targets) == 1:  # 单店铺时保留原来的返回结构
                result["store_id"] = targets[0].store_id
                result["types"] = result["stores"][targets[0].store_id]["types"]
            try:
                result["marketplaces"] = resolve_marketplaces(self.browse)
            except Exception as e:  # 站点回填失败不影响消息入库
                log.warning("会话站点回填失败：%s", e)
                result["marketplaces"] = {"error": str(e)[:500]}
            result["api_calls"] = calls + result["marketplaces"].get("browse_calls", 0)
            return result
        finally:
            _run_lock.release()

    def sync_target(self, target: EbayStoreTarget) -> tuple[dict, int]:
        """拉取一个店铺的两类会话（持有该店铺的锁）；返回 (结果, 接口调用数)。定时拉取与推送触发共用。"""
        client = self._fixed_client or self.client_factory(target)
        before = client.calls
        store_result = {"name": target.name, "ok": True, "types": {}}
        with store_lock(target.store_id):
            for ctype in CONVERSATION_TYPES:
                r = self._sync_type(target, ctype, client)
                store_result["types"][ctype] = r
                if not r.get("ok"):
                    store_result["ok"] = False
                    if r.get("auth_error"):  # 秘钥失效等：这个店铺的另一类会话也不用再试
                        break
        return store_result, client.calls - before

    def sync_stores(self, store_ids: set[str] | None = None) -> dict:
        """只拉指定店铺（推送触发）；None 为全部。不占用全量拉取的总锁，按店铺加锁。"""
        reasons = ebay_not_ready_reasons()
        if reasons:
            return {"ok": False, "error": "；".join(reasons)}
        result: dict = {"ok": True, "stores": {}}
        for target in ebay_store_targets():
            if store_ids is not None and target.store_id not in store_ids:
                continue
            store_result, _ = self.sync_target(target)
            result["ok"] = result["ok"] and store_result["ok"]
            result["stores"][target.store_id] = store_result
        try:  # 新会话立刻补上站点，AI 处理时不会因站点未知而转人工
            result["marketplaces"] = resolve_marketplaces(self.browse)
        except Exception as e:
            log.warning("会话站点回填失败：%s", e)
        return result

    # ------------------------------------------------------------------
    def _sync_type(self, target: EbayStoreTarget, ctype: str, client: MessageApiClient) -> dict:
        s = get_settings()
        started = utcnow()
        with session_scope() as db:
            store = db.get(Store, target.store_id) or ensure_ebay_store(db)
            state = db.scalar(select(EbaySyncState).where(EbaySyncState.store_id == store.id,
                                                          EbaySyncState.conversation_type == ctype))
            if state is None:
                state = EbaySyncState(store_id=store.id, conversation_type=ctype)
                db.add(state)
            state.last_started_at = started
            since = (state.last_success_at - OVERLAP) if state.last_success_at \
                else started - timedelta(days=s.ebay_sync_lookback_days)
            keep_from = cutoff_for(store, started)
            # 已读功能上线后的第一次同步：把保留期内的会话都列一遍，按 eBay 的未读数回填已读状态
            backfill_read = state.read_backfilled_at is None and state.last_success_at is not None
            if backfill_read:
                since = min(since, keep_from or started - timedelta(days=s.ebay_sync_lookback_days))
            if keep_from and since < keep_from:  # 不拉保留期之外的会话
                since = keep_from
            seller = target.seller_username or store.ebay_user_id
            store_id = store.id

        stats: Counter = Counter({k: 0 for k in ("conversations_listed", "conversations_synced",
                                                 "conversations_unchanged", "messages_new")})
        try:
            listed = []
            for conv in client.iter_conversations(
                    ctype, start_time=format_time(since),
                    max_pages=FROM_EBAY_MAX_PAGES if ctype == "FROM_EBAY" and not backfill_read else 20):
                listed.append(conv)
                # FROM_EBAY 不能按时间过滤，但列表按最新消息时间倒序（2026-10-24 实测），
                # 遇到早于本次起点的会话即可停止翻页
                latest = conv.get("latestMessage") or {}
                at = parse_time(latest.get("createdDate") or conv.get("createdDate"))
                if ctype == "FROM_EBAY" and at and at < since:
                    break
            stats["conversations_listed"] = len(listed)
            if ctype == "FROM_MEMBERS" and not seller:
                seller = infer_seller(listed)
                if seller:
                    with session_scope() as db:
                        db.get(Store, store_id).ebay_user_id = seller
                    log.info("已推断店铺 eBay 用户名（可在 .env 用 EBAY_SELLER_USERNAME 固定）")
            for conv in listed:
                latest = conv.get("latestMessage") or {}
                latest_at = parse_time(latest.get("createdDate") or conv.get("createdDate"))
                if ctype == "FROM_EBAY" and latest_at and latest_at < since:
                    continue
                self._sync_conversation(client, store_id, ctype, conv, seller, stats)
        except (EbayAuthError, EbayApiError) as e:
            with session_scope() as db:
                st = db.scalar(select(EbaySyncState).where(EbaySyncState.store_id == store_id,
                                                           EbaySyncState.conversation_type == ctype))
                st.last_error = str(e)[:2000]
                st.last_stats = dict(stats)
            log.warning("eBay 店铺 %s %s 同步失败：%s", store_id, ctype, e)
            return {"ok": False, "error": str(e), "auth_error": isinstance(e, EbayAuthError), **stats}

        with session_scope() as db:
            st = db.scalar(select(EbaySyncState).where(EbaySyncState.store_id == store_id,
                                                       EbaySyncState.conversation_type == ctype))
            if backfill_read:  # 保留期内没列到的旧会话（已归档等）视为已读
                for c in db.scalars(select(Conversation).where(
                        Conversation.store_id == store_id, Conversation.conversation_type == ctype,
                        Conversation.read_seq.is_(None), Conversation.created_at < started)):
                    mark_read(db, c, INITIAL_READER)
                    stats["read_initialized"] += 1
            if st.read_backfilled_at is None:
                st.read_backfilled_at = utcnow()
            st.last_success_at = started
            st.last_error = None
            st.last_stats = dict(stats)
            db.add(AuditEvent(actor="ebay_sync", action="EBAY_SYNC", store_id=store_id,
                              detail={"conversation_type": ctype, **stats}))
        if not seller and ctype == "FROM_MEMBERS" and stats["conversations_listed"]:
            stats["warning"] = "无法推断店铺自己的 eBay 用户名，发送方角色暂记为 UNKNOWN；请在 .env 填 EBAY_SELLER_USERNAME"
        return {"ok": True, "since": format_time(since), **stats}

    def _sync_conversation(self, client: MessageApiClient, store_id: str, ctype: str, conv: dict,
                           seller: str | None, stats: Counter) -> None:
        conv_id = conv["conversationId"]
        latest_id = (conv.get("latestMessage") or {}).get("messageId")
        with session_scope() as db:
            existing = db.scalar(select(Conversation).where(Conversation.store_id == store_id,
                                                            Conversation.ebay_conversation_id == conv_id))
            if existing and latest_id and db.scalar(select(Message.id).where(
                    Message.store_id == store_id, Message.ebay_message_id == latest_id)):
                if apply_platform_unread(db, existing, conv.get("unreadCount")):
                    stats["read_from_ebay"] += 1
                stats["conversations_unchanged"] += 1
                return

        meta, messages = client.get_conversation_messages(conv_id, ctype)
        messages.sort(key=lambda m: (parse_time(m.get("createdDate")) or datetime.min, m.get("messageId") or ""))
        settings = get_settings()

        with session_scope() as db:
            cutoff = store_cutoff(db, store_id)
            if cutoff:
                kept = [m for m in messages if (parse_time(m.get("createdDate")) or cutoff) >= cutoff]
                if len(kept) < len(messages):
                    stats["messages_skipped_old"] += len(messages) - len(kept)
                    messages = kept
                if not messages and not db.scalar(select(Conversation.id).where(
                        Conversation.store_id == store_id, Conversation.ebay_conversation_id == conv_id)):
                    stats["conversations_skipped_old"] += 1
                    return
            item_id = conv.get("referenceId") if conv.get("referenceType") == "LISTING" else None
            buyer_key = "ebay" if ctype == "FROM_EBAY" else _other_party(messages, seller) or "UNKNOWN"
            c = get_or_create_conversation(db, store_id, conv_id, buyer_key, item_id, "UNKNOWN", None)
            c.channel_source = "ebay"
            c.conversation_type = ctype
            c.conversation_title = (meta.get("conversationTitle") or conv.get("conversationTitle") or "")[:500] or None
            c.platform_status = meta.get("conversationStatus") or conv.get("conversationStatus")
            if c.buyer_key == "UNKNOWN" and buyer_key != "UNKNOWN":
                c.buyer_key = buyer_key
            if item_id and not c.item_id:
                c.item_id = item_id
            stats["conversations_synced"] += 1
            # 开了“AI 自动回复”的店铺：新的买家消息自动交给 AI（全局开关 EBAY_SYNC_CREATE_REPLY_JOBS 对所有店铺生效）。
            # 店铺开关只处理 24 小时内的来信：补拉到的旧消息（如已读回填、首次同步）不自动回复
            store_ai = bool(db.get(Store, store_id).auto_send_enabled)
            recent_from = utcnow() - AUTO_REPLY_MAX_AGE

            for m in messages:
                mid = m.get("messageId")
                if not mid or db.scalar(select(Message.id).where(Message.store_id == store_id,
                                                                 Message.ebay_message_id == mid)):
                    continue
                role = _role(ctype, m.get("senderUsername"), seller)
                sent_at = parse_time(m.get("createdDate"))
                body, body_html = split_body(m.get("messageBody"))  # eBay 系统通知是 HTML 邮件：正文存纯文本
                if role == "BUYER" and (settings.ebay_sync_create_reply_jobs
                                        or (store_ai and sent_at and sent_at >= recent_from)):
                    result = ingest_buyer_message(db, conv=c, ebay_message_id=mid, body=body, sent_at=sent_at,
                                                  actor="ebay_sync")
                    msg = result.message
                else:
                    msg = _append_message(db, c, mid, role, "OUT" if role == "SELLER" else "IN", body, sent_at)
                    c.row_version += 1
                msg.body_html = body_html
                msg.subject = (m.get("subject") or "")[:500] or None
                msg.sender_key = m.get("senderUsername")
                msg.media = m.get("messageMedia") or None
                msg.platform_read = m.get("readStatus")
                if msg.media and role in ("BUYER", "UNKNOWN"):
                    register_ebay_media(db, msg, msg.media)  # 只登记，下载在 Agent 运行时按需进行
                    stats["attachments_registered"] += len(msg.media)
                stats["messages_new"] += 1
                stats[f"messages_{role.lower()}"] += 1
            db.flush()
            if apply_platform_unread(db, c, conv.get("unreadCount")):
                stats["read_from_ebay"] += 1


def _role(ctype: str, sender: str | None, seller: str | None) -> str:
    if ctype == "FROM_EBAY":
        return "SYSTEM"
    if not seller:
        return "UNKNOWN"
    return "SELLER" if sender == seller else "BUYER"


def _other_party(messages: list[dict], seller: str | None) -> str | None:
    if not seller:
        return None
    for m in messages:
        for u in (m.get("senderUsername"), m.get("recipientUsername")):
            if u and u != seller:
                return u
    return None
