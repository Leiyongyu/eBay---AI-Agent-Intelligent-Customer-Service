"""上线前业务基线补拉（baseline.py）：用 httpx.MockTransport 模拟 eBay，不联网。
覆盖：试运行不写库、角色判断（不区分大小写）、时间段过滤、去重幂等、断点续拉、接口调用上限、不碰工作台表、保留期清理。"""
from datetime import datetime, timedelta

import httpx
import pytest

from app import config

SELLER = "stardrive_real"
START, END = datetime(2026, 9, 10), datetime(2026, 11, 5)


def _m(mid, sender, recipient, at, body="hi"):
    return {"messageId": mid, "senderUsername": sender, "recipientUsername": recipient, "messageBody": body,
            "createdDate": at, "subject": "Re", "readStatus": True}


# 列表按最新消息时间倒序（与 eBay 一致）
CONVS = [
    {"conversationId": "A", "createdDate": "2026-09-15T10:00:00.000Z", "referenceType": "LISTING", "referenceId": "3001",
     "latestMessage": _m("A3", "Buyer_A", SELLER, "2026-11-06T08:00:00.000Z")},
    {"conversationId": "B", "createdDate": "2026-11-06T09:00:00.000Z",
     "latestMessage": _m("B1", "buyer_b", SELLER, "2026-11-06T09:00:00.000Z")},
    {"conversationId": "E", "createdDate": "2026-09-25T07:00:00.000Z",
     "latestMessage": _m("E1", "buyer_e", SELLER, "2026-09-25T07:00:00.000Z")},
    {"conversationId": "C", "createdDate": "2026-08-01T10:00:00.000Z",
     "latestMessage": _m("C3", "StarDrive_Real", "buyer_c", "2026-09-21T10:00:00.000Z")},
    {"conversationId": "D", "createdDate": "2026-09-01T00:00:00.000Z",
     "latestMessage": _m("D1", "buyer_d", SELLER, "2026-09-05T00:00:00.000Z")},
]
MESSAGES = {
    "A": [_m("A1", "Buyer_A", SELLER, "2026-09-15T10:00:00.000Z", "Does it fit?"),
          _m("A2", "StarDrive_Real", "Buyer_A", "2026-09-15T12:00:00.000Z", "Yes, it fits."),
          _m("A3", "Buyer_A", SELLER, "2026-11-06T08:00:00.000Z", "Thanks")],  # 上线后：不算基线
    "B": [_m("B1", "buyer_b", SELLER, "2026-11-06T09:00:00.000Z")],
    "C": [_m("C1", "buyer_c", SELLER, "2026-08-01T10:00:00.000Z", "old"),  # 时间段前：不存
          _m("C2", "buyer_c", SELLER, "2026-09-20T10:00:00.000Z", "Where is it?"),
          _m("C3", "StarDrive_Real", "buyer_c", "2026-09-21T10:00:00.000Z", "Shipped.")],
    "D": [_m("D1", "buyer_d", SELLER, "2026-09-05T00:00:00.000Z")],
    "E": [_m("E1", "buyer_e", SELLER, "2026-09-25T07:00:00.000Z", "Hallo")],
}


class FakeEbay:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.crash_on: str | None = None  # 取这个会话时模拟进程中断
        self.fail_on: str | None = None  # 取这个会话时 eBay 一直报 500

    def details(self, cid=None):
        return [r for r in self.requests if r.url.path.startswith("/commerce/message/v1/conversation/")
                and (cid is None or r.url.path.endswith("/" + cid))]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/identity/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 7200})
        assert request.method == "GET", "基线补拉只能发 GET"
        q = dict(request.url.params)
        if path == "/commerce/message/v1/conversation":
            assert q["conversation_type"] == "FROM_MEMBERS"
            off, lim = int(q["offset"]), int(q["limit"])
            page = CONVS[off: off + min(lim, 2)]  # 每页 2 条，测分页
            more = off + len(page) < len(CONVS)
            return httpx.Response(200, json={"conversations": page, "total": len(CONVS), **({"next": "n"} if more else {})})
        cid = path.rsplit("/", 1)[1]
        if cid == self.crash_on:
            raise RuntimeError("模拟中断")
        if cid == self.fail_on:
            return httpx.Response(500, json={"errors": [{"errorId": 1, "message": "boom"}]})
        return httpx.Response(200, json={"conversationStatus": "ACTIVE", "messages": MESSAGES[cid],
                                         "total": len(MESSAGES[cid])})


@pytest.fixture()
def bl(app_env, monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "app-id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "cert-id")
    monkeypatch.setenv("EBAY_REFRESH_TOKEN", "v^1.1#fake")
    monkeypatch.setenv("EBAY_STORE_ID", "REAL_US")
    monkeypatch.setenv("EBAY_SELLER_USERNAME", SELLER)
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def backfill(fake: FakeEbay):
    from app.baseline import BaselineBackfill
    from app.ebay.auth import EbayTokenManager
    from app.ebay.message_api import MessageApiClient

    http = httpx.Client(transport=httpx.MockTransport(fake))
    return BaselineBackfill(client_factory=lambda t: MessageApiClient(EbayTokenManager(http), http, sleep=lambda s: None),
                            sleep=lambda s: None, pause=0.5)


def run(fake, **kw):
    kw.setdefault("store_keys", {"real_us"})
    return backfill(fake).run(start=START, end=END, **kw)


def counts():
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import (BaselineBatch, BaselineConversation, BaselineMessage, Conversation, Message, ReplyJob)

    with session_scope() as db:
        return {m.__name__: db.scalar(select(func.count()).select_from(m))
                for m in (BaselineBatch, BaselineConversation, BaselineMessage, Conversation, Message, ReplyJob)}


def test_dry_run_counts_without_writing(bl):
    fake = FakeEbay()
    r = run(fake)
    assert r["ok"] and not r["apply"]
    st = r["stores"]["REAL_US"]
    assert st["probe"] == {"total_conversations": 5, "earliest_available": "2026-09-01T00:00:00Z"}
    assert (st["listed"], st["candidates"], st["skipped_after_period"], st["skipped_before_period"]) == (5, 3, 1, 1)
    assert st["sample"] == {"conversations": 3, "messages": 5, "buyer": 3, "seller": 2, "empty": 0}
    assert r["estimated_calls"] == r["list_calls"] + 3
    assert not fake.details("B") and not fake.details("D")  # 上线后才开始的、更早的会话不取消息
    assert counts() == {"BaselineBatch": 0, "BaselineConversation": 0, "BaselineMessage": 0,
                        "Conversation": 0, "Message": 0, "ReplyJob": 0}


def test_apply_roles_period_and_no_workbench_tables(bl):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import BaselineBatch, BaselineConversation, BaselineMessage

    fake = FakeEbay()
    r = run(fake, apply=True)
    assert r["ok"] and r["status"] == "DONE", r
    assert (r["conversations"], r["messages"], r["messages_buyer"], r["messages_seller"]) == (3, 5, 3, 2)
    assert r["api_calls_used"] == len([x for x in fake.requests if x.url.path.startswith("/commerce/")])
    with session_scope() as db:
        msgs = {m.ebay_message_id: m for m in db.scalars(select(BaselineMessage))}
        assert set(msgs) == {"A1", "A2", "C2", "C3", "E1"}  # A3（上线后）、C1（时间段前）不存
        assert {k: m.sender_role for k, m in msgs.items()} == {"A1": "BUYER", "A2": "SELLER", "C2": "BUYER",
                                                              "C3": "SELLER", "E1": "BUYER"}
        assert msgs["A2"].body == "Yes, it fits." and msgs["A2"].sent_at == datetime(2026, 9, 15, 12)
        a = db.scalar(select(BaselineConversation).where(BaselineConversation.ebay_conversation_id == "A"))
        assert (a.buyer_key, a.item_id, a.conversation_type) == ("buyer_a", "3001", "FROM_MEMBERS")
        assert (a.first_message_at, a.last_message_at) == (datetime(2026, 9, 15, 10), datetime(2026, 9, 15, 12))
        b = db.get(BaselineBatch, r["batch_id"])
        assert b.status == "DONE" and b.finished_at and b.stores == ["REAL_US"]
        assert (b.period_start, b.period_end) == (START, END)
        assert b.stats["messages"] == 5 and b.stats["api_calls"] == r["api_calls_used"]
        assert b.stats["earliest_available"] == {"REAL_US": "2026-09-01T00:00:00Z"}
    c = counts()
    assert (c["Conversation"], c["Message"], c["ReplyJob"]) == (0, 0, 0)  # 不写工作台的会话、消息、回复任务


def test_rerun_is_idempotent(bl):
    run(FakeEbay(), apply=True)
    r = run(FakeEbay(), apply=True)
    assert r["ok"] and r["messages"] == 0 and r["batch_messages_total"] == 0
    c = counts()
    assert (c["BaselineBatch"], c["BaselineConversation"], c["BaselineMessage"]) == (2, 3, 5)


def test_resume_after_interrupt_skips_written(bl):
    from app.db import session_scope
    from app.models import BaselineBatch

    fake = FakeEbay()
    fake.crash_on = "C"  # 顺序 A、E、C：A、E 写好后中断
    with pytest.raises(RuntimeError):
        run(fake, apply=True)
    with session_scope() as db:
        b = db.query(BaselineBatch).one()
        assert b.status == "RUNNING" and b.finished_at is None
        batch_id = b.id
    assert counts()["BaselineMessage"] == 3

    fake2 = FakeEbay()
    r = backfill(fake2).run(resume_batch=batch_id, apply=True)
    assert r["ok"] and r["status"] == "DONE" and r["batch_id"] == batch_id
    assert [x.url.path.rsplit("/", 1)[1] for x in fake2.details()] == ["C"]  # 只取没写完的
    assert r["batch_messages_total"] == 5 and counts()["BaselineBatch"] == 1
    assert backfill(FakeEbay()).run(resume_batch=batch_id, apply=True)["ok"] is False  # 已完成不再续拉


def test_api_error_marks_partial_and_resume_retries(bl):
    fake = FakeEbay()
    fake.fail_on = "E"
    r = run(fake, apply=True)
    assert not r["ok"] and r["status"] == "PARTIAL" and any("会话 E" in e for e in r["errors"])
    assert r["conversations"] == 2
    r2 = backfill(FakeEbay()).run(resume_batch=r["batch_id"], apply=True)
    assert r2["ok"] and r2["batch_messages_total"] == 5


def test_over_limit_stops_before_writing(bl):
    fake = FakeEbay()
    r = run(fake, apply=True, max_calls=3)
    assert r["stopped"] == "OVER_LIMIT" and not r["ok"]
    assert not fake.details()
    assert counts()["BaselineBatch"] == 0


def test_unknown_seller_is_an_error(bl, monkeypatch):
    """不知道店铺自己的 eBay 账号就分不清买家 / 店铺发言：报错，不写库（样例里店铺账号大小写不一，推断不出来）。"""
    monkeypatch.delenv("EBAY_SELLER_USERNAME")
    config.get_settings.cache_clear()
    fake = FakeEbay()
    r = run(fake, apply=True)
    assert not r["ok"] and r["status"] == "PARTIAL"
    assert "eBay 账号" in r["stores"]["REAL_US"]["error"]
    assert not fake.details() and counts()["BaselineMessage"] == 0


def test_retention_cleanup(bl, monkeypatch):
    from sqlalchemy import select

    from app import retention
    from app.baseline import write_conversation
    from app.db import session_scope
    from app.models import BaselineConversation, BaselineMessage

    monkeypatch.setenv("BASELINE_RETENTION_DAYS", "90")
    config.get_settings.cache_clear()
    now = datetime(2026, 12, 15)  # 保留 90 天：截止 09-16
    with session_scope() as db:
        write_conversation(db, "REAL_US", {"conversationId": "OLD"}, [
            {"ebay_message_id": "o1", "sender_role": "BUYER", "sent_at": datetime(2026, 9, 11), "body": "x"}], "b", None)
        write_conversation(db, "REAL_US", {"conversationId": "MIX"}, [
            {"ebay_message_id": "m1", "sender_role": "BUYER", "sent_at": datetime(2026, 9, 12), "body": "x"},
            {"ebay_message_id": "m2", "sender_role": "SELLER", "sent_at": datetime(2026, 10, 1), "body": "y"}], "c", None)
    dry = retention.run(dry_run=True, now=now)["baseline"]
    assert (dry["messages"], dry["conversations"]) == (2, 1)
    assert counts()["BaselineMessage"] == 3
    done = retention.run(dry_run=False, now=now)["baseline"]
    assert (done["messages"], done["conversations"]) == (2, 1)
    with session_scope() as db:
        assert [c.ebay_conversation_id for c in db.scalars(select(BaselineConversation))] == ["MIX"]
        assert [m.ebay_message_id for m in db.scalars(select(BaselineMessage))] == ["m2"]
    monkeypatch.setenv("BASELINE_RETENTION_DAYS", "0")
    config.get_settings.cache_clear()
    assert "skipped" in retention.run(dry_run=False, now=now)["baseline"]
