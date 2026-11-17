"""上线前业务基线补拉（开发方案第 2.1 节）：从 eBay 只读补拉对接店铺上线前“纯人工时期”的买家会话，单独存表，只用于统计。

- 只读：只调 Message API 的 getConversations / getConversation（ebay/message_api.py），不发消息、不改已读；
- 不走同步入库流程：不写 conversations / messages、不建回复任务、不翻译、不下载图片、不推送；
  写 baseline_conversations / baseline_messages / baseline_batches 三张表（models.py），工作台和客户资料都不读它们；
- 角色：发件人是店铺自己的 eBay 账号 → SELLER，其余 → BUYER（与 ebay/sync.py 一致，不区分大小写）；
- 时间段：period_start（含）到 period_end（不含），按消息在 eBay 的发送时间过滤；只存时间段内有消息的会话；
- 分页、限速（每次调用前停顿 pause 秒）；按批次记录进度，续拉跳过本批次已写好的会话；按 (店铺, eBay 消息 ID) 去重，重跑幂等；
- 默认试运行（apply=False）：只列会话、抽样取几个会话的消息估算消息数和接口调用数，不写库；
- 保留期：基线消息正文也是买家个人数据，按 BASELINE_RETENTION_DAYS（默认 90 天，按消息时间）由每日清理删除（retention.py）。
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import delete, exists, func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import session_scope, utcnow
from .ebay.auth import EbayAuthError, for_store
from .ebay.html_text import split_body
from .ebay.message_api import EbayApiError, MessageApiClient
from .ebay.stores import EbayStoreTarget, ebay_store_targets
from .ebay.sync import format_time, infer_seller, parse_time
from .models import BaselineBatch, BaselineConversation, BaselineMessage, Store

log = logging.getLogger(__name__)

CTYPE = "FROM_MEMBERS"
# 默认基线：上线前 8 周。11-05 起对接店铺开始用本系统真实收发，不算基线
DEFAULT_START = datetime(2026, 9, 10)
DEFAULT_END = datetime(2026, 11, 5)  # 不含：到 11-04 当天结束
PROBE_SINCE = datetime(2000, 1, 1)  # 探测 eBay 能取到多早：不按时间过滤
LIST_MAX_PAGES = 400  # 会话列表最多翻多少页（每页 50）
SAVE_EVERY = 20  # 每写多少个会话把进度存进批次
MAX_ERRORS_KEPT = 30


def role_of(sender: str | None, seller: str) -> str:
    return "SELLER" if (sender or "").lower() == seller.lower() else "BUYER"


def other_party(messages: list[dict], seller: str) -> str | None:
    for m in messages:
        for u in (m.get("senderUsername"), m.get("recipientUsername")):
            if u and u.lower() != seller.lower():
                return u.lower()
    return None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


def _conv_times(conv: dict) -> tuple[datetime | None, datetime | None]:
    latest = conv.get("latestMessage") or {}
    return parse_time(conv.get("createdDate")), parse_time(latest.get("createdDate"))


class BaselineBackfill:
    def __init__(self, client_factory=None, sleep=time.sleep, pause: float = 0.2, sample: int = 10,
                 list_max_pages: int = LIST_MAX_PAGES, echo=None):
        self.client_factory = client_factory or (lambda t: MessageApiClient(for_store(t.store_id)))
        self.sleep = sleep
        self.pause = pause
        self.sample = sample
        self.list_max_pages = list_max_pages
        self.echo = echo or (lambda msg: log.info(msg))

    # ------------------------------------------------------------ 准备：探测 + 列会话
    def _client(self, target: EbayStoreTarget) -> MessageApiClient:
        client = self.client_factory(target)
        orig = client._get

        def throttled(path, params):  # 限速：每次请求前停一下（重试的等待在 _get 里面）
            if self.pause > 0:
                self.sleep(self.pause)
            return orig(path, params)

        client._get = throttled
        return client

    def probe(self, client: MessageApiClient) -> dict:
        """eBay 能取到多早的买家会话：不按时间过滤取总数，再取最后一条（列表按最新消息时间倒序）。2 次调用。"""
        first = client._get("/conversation", {"conversation_type": CTYPE, "start_time": format_time(PROBE_SINCE),
                                              "limit": 1, "offset": 0})
        total = int(first.get("total") or 0)
        out: dict = {"total_conversations": total, "earliest_available": None}
        if total <= 0:
            return out
        last = client._get("/conversation", {"conversation_type": CTYPE, "start_time": format_time(PROBE_SINCE),
                                             "limit": 1, "offset": total - 1})
        items = last.get("conversations") or first.get("conversations") or []
        times = [t for c in items for t in _conv_times(c) if t]
        out["earliest_available"] = _iso(min(times)) if times else None
        return out

    def plan(self, target: EbayStoreTarget, start: datetime, end: datetime) -> dict:
        """列出这个店铺时间段内可能有消息的买家会话（不取消息）。"""
        client = self._client(target)
        p: dict = {"target": target, "client": client, "candidates": [], "stats": Counter(), "error": None}
        try:  # 探测失败（如 eBay 不让翻到最后）不影响补拉
            p["probe"] = self.probe(client)
        except (EbayAuthError, EbayApiError) as e:
            if isinstance(e, EbayAuthError):
                p["error"], p["list_calls"] = str(e)[:500], client.calls
                return p
            p["probe"] = {"error": str(e)[:300]}
        try:
            listed = list(client.iter_conversations(CTYPE, start_time=format_time(start), max_pages=self.list_max_pages))
        except (EbayAuthError, EbayApiError) as e:
            p["error"] = str(e)[:500]
            p["list_calls"] = client.calls
            return p
        p["stats"]["listed"] = len(listed)
        p["list_truncated"] = len(listed) >= self.list_max_pages * 50
        with session_scope() as db:
            store = db.get(Store, target.store_id)
            seller = target.seller_username or (store.ebay_user_id if store else None) or infer_seller(listed)
        p["seller"] = seller
        if not seller:
            p["error"] = "不知道店铺自己的 eBay 账号，分不清买家 / 店铺发言：请先导入店铺凭证或在 .env 填 EBAY_SELLER_USERNAME"
        for c in listed:
            created, latest = _conv_times(c)
            first_at = created or latest
            if first_at and first_at >= end:  # 上线后才开始的会话：没有基线消息，不取
                p["stats"]["skipped_after_period"] += 1
                continue
            if latest and latest < start:
                p["stats"]["skipped_before_period"] += 1
                continue
            p["candidates"].append(c)
        p["list_calls"] = client.calls
        return p

    # ------------------------------------------------------------ 取消息
    def _fetch(self, client: MessageApiClient, conv: dict, seller: str, start: datetime, end: datetime):
        meta, messages = client.get_conversation_messages(conv["conversationId"], CTYPE)
        messages.sort(key=lambda m: (parse_time(m.get("createdDate")) or datetime.min, m.get("messageId") or ""))
        rows = []
        for m in messages:
            at = parse_time(m.get("createdDate"))
            if not m.get("messageId") or at is None or not (start <= at < end):
                continue
            rows.append({"ebay_message_id": m["messageId"], "sender_role": role_of(m.get("senderUsername"), seller),
                         "sent_at": at, "body": split_body(m.get("messageBody"))[0]})
        return rows, other_party(messages, seller)

    # ------------------------------------------------------------ 入口
    def run(self, store_keys: set[str] | None = None, start: datetime = DEFAULT_START, end: datetime = DEFAULT_END,
            apply: bool = False, resume_batch: int | None = None, max_calls: int = 2000) -> dict:
        batch_stats: dict = {}
        if resume_batch is not None:
            with session_scope() as db:
                b = db.get(BaselineBatch, resume_batch)
                if b is None:
                    return {"ok": False, "error": f"没有批次 #{resume_batch}"}
                if b.status == "DONE":
                    return {"ok": False, "error": f"批次 #{resume_batch} 已完成，不用续拉"}
                store_keys, start, end, batch_stats = {x.lower() for x in b.stores}, b.period_start, b.period_end, dict(b.stats or {})
        keys = store_keys if store_keys else get_settings().ebay_live_store_keys
        if not keys:
            return {"ok": False, "error": "没有指定店铺，EBAY_LIVE_STORES 也是空的"}
        targets = ebay_store_targets(keys)
        if not targets:
            return {"ok": False, "error": f"找不到可对接的店铺：{'、'.join(sorted(keys))}（要有导入的店铺凭证）"}
        if not (start < end):
            return {"ok": False, "error": "时间段不对：起点要早于终点"}

        self.echo(f"列会话：{'、'.join(t.store_id for t in targets)}，{start:%Y-%m-%d} 到 {end - timedelta(seconds=1):%Y-%m-%d}")
        plans = [self.plan(t, start, end) for t in targets]
        done_ids: dict[str, set[str]] = {t.store_id: set() for t in targets}
        if resume_batch is not None:
            with session_scope() as db:
                for t in targets:
                    done_ids[t.store_id] = set(db.scalars(select(BaselineConversation.ebay_conversation_id).where(
                        BaselineConversation.store_id == t.store_id, BaselineConversation.batch_id == resume_batch)))
                    done_ids[t.store_id] |= set((batch_stats.get("empty_ids") or {}).get(t.store_id) or [])
        for p in plans:
            done = done_ids[p["target"].store_id]
            p["todo"] = [c for c in p["candidates"] if c["conversationId"] not in done]
        list_calls = sum(p["list_calls"] for p in plans)
        estimate = list_calls + sum(len(p["todo"]) for p in plans if not p["error"])
        report: dict = {"ok": True, "apply": apply, "period_start": _iso(start), "period_end": _iso(end),
                        "stores": {}, "list_calls": list_calls, "estimated_calls": estimate}

        if not apply:
            return self._dry_run(plans, start, end, report)
        if estimate > max_calls:
            report.update(ok=False, stopped="OVER_LIMIT",
                          error=f"预计接口调用 {estimate} 次，超过上限 {max_calls}，没有写库；确认后用 --max-calls 调高再跑")
            for p in plans:
                report["stores"][p["target"].store_id] = self._store_summary(p)
            return report
        return self._apply(plans, start, end, report, resume_batch, batch_stats)

    def _store_summary(self, p: dict) -> dict:
        return {"error": p["error"], "probe": p.get("probe"), "listed": p["stats"].get("listed", 0),
                "candidates": len(p["candidates"]), "todo": len(p.get("todo") or []),
                "skipped_after_period": p["stats"].get("skipped_after_period", 0),
                "skipped_before_period": p["stats"].get("skipped_before_period", 0),
                "list_truncated": p.get("list_truncated", False), "list_calls": p["list_calls"]}

    def _dry_run(self, plans: list[dict], start: datetime, end: datetime, report: dict) -> dict:
        """不写库：抽样取几个会话的消息，估算消息数。"""
        sample_calls = 0
        for p in plans:
            st = self._store_summary(p)
            todo = p["todo"]
            if not p["error"] and todo and self.sample > 0:
                step = max(1, len(todo) // self.sample)
                picked = todo[::step][: self.sample]
                before = p["client"].calls
                n = Counter()
                for c in picked:
                    try:
                        rows, _ = self._fetch(p["client"], c, p["seller"], start, end)
                    except (EbayAuthError, EbayApiError) as e:
                        st["sample_error"] = str(e)[:300]
                        break
                    n["conversations"] += 1
                    n["messages"] += len(rows)
                    n["buyer"] += sum(r["sender_role"] == "BUYER" for r in rows)
                    n["seller"] += sum(r["sender_role"] == "SELLER" for r in rows)
                    n["empty"] += not rows
                sample_calls += p["client"].calls - before
                st["sample"] = dict(n)
                if n["conversations"]:
                    st["estimated_messages"] = round(n["messages"] / n["conversations"] * len(todo))
            report["stores"][p["target"].store_id] = st
        report["sample_calls"] = sample_calls
        report["api_calls_used"] = report["list_calls"] + sample_calls
        report["estimated_messages"] = sum(v.get("estimated_messages") or 0 for v in report["stores"].values())
        return report

    def _apply(self, plans: list[dict], start: datetime, end: datetime, report: dict, resume_batch: int | None,
               batch_stats: dict) -> dict:
        prev_calls = int(batch_stats.get("api_calls") or 0)
        self._plans = plans
        with session_scope() as db:
            if resume_batch is None:
                b = BaselineBatch(stores=[p["target"].store_id for p in plans], period_start=start, period_end=end,
                                  status="RUNNING", stats={})
                db.add(b)
                db.flush()
            else:
                b = db.get(BaselineBatch, resume_batch)
                b.status, b.started_at, b.finished_at = "RUNNING", utcnow(), None
            batch_id = b.id
        report["batch_id"] = batch_id
        stats = {"conversations": 0, "messages": 0, "messages_buyer": 0, "messages_seller": 0, "api_calls": prev_calls,
                 "errors": list(batch_stats.get("errors") or [])[-MAX_ERRORS_KEPT:],
                 "earliest_available": dict(batch_stats.get("earliest_available") or {}),
                 "empty_ids": {k: list(v) for k, v in (batch_stats.get("empty_ids") or {}).items()},
                 "stores": dict(batch_stats.get("stores") or {})}
        failed = False
        for p in plans:
            sid = p["target"].store_id
            st = self._store_summary(p)
            if p.get("probe"):
                stats["earliest_available"][sid] = p["probe"].get("earliest_available")
            if p["error"]:
                failed = True
                stats["errors"].append(f"{sid}：{p['error']}")
                stats["stores"][sid] = st
                continue
            empty = stats["empty_ids"].setdefault(sid, [])
            written = Counter()
            for i, c in enumerate(p["todo"], 1):
                try:
                    rows, buyer = self._fetch(p["client"], c, p["seller"], start, end)
                except EbayAuthError as e:
                    failed = True
                    stats["errors"].append(f"{sid}：{str(e)[:300]}")
                    break
                except EbayApiError as e:  # 一个会话出错不影响其他会话；续拉时会再试
                    written["errors"] += 1
                    stats["errors"].append(f"{sid} 会话 {c['conversationId']}：{str(e)[:200]}")
                    continue
                if not rows:
                    empty.append(c["conversationId"])
                    written["empty"] += 1
                else:
                    with session_scope() as db:
                        new = write_conversation(db, sid, c, rows, buyer, batch_id)
                    written["conversations"] += 1
                    written["messages"] += new["messages"]
                    written["buyer"] += new["buyer"]
                    written["seller"] += new["seller"]
                if i % SAVE_EVERY == 0:
                    self._save(batch_id, stats, p, written, st, sid, "RUNNING")
                    self.echo(f"  {sid}：{i}/{len(p['todo'])}")
            if written["errors"]:
                failed = True
            stats["conversations"] += written["conversations"]
            stats["messages"] += written["messages"]
            stats["messages_buyer"] += written["buyer"]
            stats["messages_seller"] += written["seller"]
            self._save(batch_id, stats, p, written, st, sid, None)
        stats["api_calls"] = prev_calls + sum(p["client"].calls for p in plans)
        status = "PARTIAL" if failed else "DONE"
        with session_scope() as db:
            b = db.get(BaselineBatch, batch_id)
            totals = db.execute(select(func.count(), func.min(BaselineMessage.sent_at), func.max(BaselineMessage.sent_at))
                                .where(BaselineMessage.batch_id == batch_id)).one()
            stats["batch_messages_total"], stats["first_message_at"], stats["last_message_at"] = \
                totals[0], _iso(totals[1]), _iso(totals[2])
            stats["batch_conversations_total"] = db.scalar(select(func.count()).select_from(BaselineConversation)
                                                           .where(BaselineConversation.batch_id == batch_id))
            b.stats, b.status, b.finished_at = stats, status, utcnow()
        report.update(ok=not failed, status=status, api_calls_used=stats["api_calls"] - prev_calls, **{
            k: stats[k] for k in ("conversations", "messages", "messages_buyer", "messages_seller", "errors",
                                  "earliest_available", "batch_messages_total", "batch_conversations_total",
                                  "first_message_at", "last_message_at")})
        report["stores"] = stats["stores"]
        return report

    def _save(self, batch_id: int, stats: dict, p: dict, written: Counter, st: dict, sid: str, status: str | None):
        st = {**st, "written_conversations": written["conversations"], "written_messages": written["messages"],
              "empty": written["empty"], "errors": written["errors"], "api_calls": p["client"].calls}
        stats["stores"][sid] = st
        with session_scope() as db:
            b = db.get(BaselineBatch, batch_id)
            b.stats = {**stats, "api_calls": int(stats["api_calls"]) + sum(x["client"].calls for x in self._plans),
                       "empty_ids": {k: list(v) for k, v in stats["empty_ids"].items()}}
            if status:
                b.status = status


def write_conversation(db: Session, store_id: str, conv: dict, rows: list[dict], buyer: str | None,
                       batch_id: int | None) -> dict:
    """写一个基线会话和它时间段内的消息；按 (店铺, eBay 消息 ID) 去重。返回新写入的数量。"""
    cid = conv["conversationId"]
    row = db.scalar(select(BaselineConversation).where(BaselineConversation.store_id == store_id,
                                                       BaselineConversation.ebay_conversation_id == cid))
    if row is None:
        row = BaselineConversation(store_id=store_id, ebay_conversation_id=cid, conversation_type=CTYPE)
        db.add(row)
    row.buyer_key = buyer or row.buyer_key
    if conv.get("referenceType") == "LISTING" and conv.get("referenceId"):
        row.item_id = str(conv["referenceId"])[:64]
    row.fetched_at, row.batch_id = utcnow(), batch_id
    db.flush()
    ids = [r["ebay_message_id"] for r in rows]
    have = set(db.scalars(select(BaselineMessage.ebay_message_id).where(BaselineMessage.store_id == store_id,
                                                                       BaselineMessage.ebay_message_id.in_(ids))))
    out = Counter()
    for r in rows:
        if r["ebay_message_id"] in have:
            continue
        have.add(r["ebay_message_id"])
        db.add(BaselineMessage(store_id=store_id, conversation_id=row.id, batch_id=batch_id, **r))
        out["messages"] += 1
        out["buyer" if r["sender_role"] == "BUYER" else "seller"] += 1
    db.flush()
    first, last = db.execute(select(func.min(BaselineMessage.sent_at), func.max(BaselineMessage.sent_at))
                             .where(BaselineMessage.conversation_id == row.id)).one()
    row.first_message_at, row.last_message_at = first, last
    return {"messages": out["messages"], "buyer": out["buyer"], "seller": out["seller"]}


# ---------------------------------------------------------------- 保留期（每日清理调用，见 retention.run）
def cleanup(now: datetime, dry_run: bool) -> dict:
    """删除早于 BASELINE_RETENTION_DAYS 的基线消息，以及消息删光的基线会话；批次记录（只有数量）保留。"""
    days = get_settings().baseline_retention_days
    if days <= 0:
        return {"days": days, "skipped": "不清理（保留天数为 0）"}
    cutoff = now - timedelta(days=days)
    old = (BaselineMessage.sent_at < cutoff,)
    with session_scope() as db:
        if dry_run:
            recent = exists().where(BaselineMessage.conversation_id == BaselineConversation.id,
                                    BaselineMessage.sent_at >= cutoff)
            return {"days": days, "cutoff": _iso(cutoff),
                    "messages": db.scalar(select(func.count()).select_from(BaselineMessage).where(*old)) or 0,
                    "conversations": db.scalar(select(func.count()).select_from(BaselineConversation).where(~recent)) or 0}
        n_msgs = db.execute(delete(BaselineMessage).where(*old)).rowcount or 0
        has_msg = exists().where(BaselineMessage.conversation_id == BaselineConversation.id)
        n_convs = db.execute(delete(BaselineConversation).where(~has_msg)).rowcount or 0
    return {"days": days, "cutoff": _iso(cutoff), "messages": n_msgs, "conversations": n_convs}
