"""验收（测试工程师，2026-11-10）：工作台会话列表批量化——和逐个会话查询的结果逐字段一致（边界数据）、
黑名单优先级、变更标记在各种变化下都会变、店铺范围、索引幂等。"""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import login, make_user


def _seed_edge():
    """一批边界数据，返回 {名字: 会话 ID}。"""
    from app.db import session_scope, utcnow
    from app.models import (BuyerBlacklist, Conversation, ConversationMark, Draft, InvoiceRequest, Message, MessageTag,
                            OutboundMessage, ReplyJob, SendIntent, WorkClaim)

    now = utcnow()
    ids = {}
    with session_scope() as db:
        tags = db.query(MessageTag).order_by(MessageTag.sort, MessageTag.id).limit(3).all()

        def conv(name, store="STORE_A", site="EBAY_US", buyer=None, item="110000000001", minutes=0):
            c = Conversation(store_id=store, ebay_conversation_id=f"E-{name}", buyer_key=buyer or f"eb_{name}",
                             conversation_type="FROM_MEMBERS", item_id=item, marketplace_id=site,
                             last_message_at=now - timedelta(minutes=minutes))
            db.add(c)
            db.flush()
            ids[name] = c.id
            return c

        def msg(c, seq, role, mid=None, body="b", minutes=60, auto_kind=None):
            m = Message(store_id=c.store_id, ebay_message_id=mid or f"M-{c.id}-{seq}", conversation_id=c.id, conversation_seq=seq,
                        sender_role=role, direction="IN" if role == "BUYER" else "OUT", sent_at=now - timedelta(minutes=minutes),
                        body=body, auto_kind=auto_kind)
            db.add(m)
            db.flush()
            return m

        def job(c, state, anchor="x"):
            j = ReplyJob(store_id=c.store_id, conversation_id=c.id, anchor_source_message_id=anchor, state=state)
            db.add(j)
            db.flush()
            return j

        # 1 发票申请（每个会话唯一）、两份进行中草稿（取最新那份的意图）、最新任务作废（当前任务是前一个）
        c = conv("invoice2", minutes=1)
        msg(c, 1, "BUYER", body="invoice please " * 10, minutes=90)
        j = job(c, "REVIEW_REQUIRED")
        db.add(Draft(job_id=j.id, run_generation=1, version=1, author="agent", conversation_row_version=1, text="a", intent="invoice_vat"))
        db.add(Draft(job_id=j.id, run_generation=2, version=2, author="agent", conversation_row_version=1, text="b", intent="shipping_status"))
        job(c, "CANCELLED_STALE", anchor="y")
        db.add(InvoiceRequest(conversation_id=c.id, store_id="STORE_A", status="OPEN"))
        # 2 认领过期 / 认领记在别的店铺 / 有效认领
        c = conv("claim_expired", minutes=2)
        msg(c, 1, "BUYER")
        job(c, "RECEIVED")
        db.add(WorkClaim(store_id="STORE_A", conversation_id=c.id, claimed_by="alice", expires_at=now - timedelta(minutes=1)))
        c = conv("claim_other_store", minutes=3)
        msg(c, 1, "BUYER")
        db.add(WorkClaim(store_id="STORE_B", conversation_id=c.id, claimed_by="bob", expires_at=now + timedelta(minutes=5)))
        c = conv("claim_ok", minutes=4)
        msg(c, 1, "BUYER")
        job(c, "REVIEW_REQUIRED")
        db.add(WorkClaim(store_id="STORE_A", conversation_id=c.id, claimed_by="alice", expires_at=now + timedelta(minutes=5)))
        # 3 最后回复人：回执不算 / 发件箱提交人 / 发送意图提交人 / 模拟 / eBay 后台 / auto_kind=ACK
        c = conv("replier_ack", minutes=5)
        msg(c, 1, "BUYER", minutes=100)
        msg(c, 2, "SELLER", mid="EBAY-REAL-1", minutes=90)
        msg(c, 3, "SELLER", mid="SIM-OUT-ACK-1", minutes=80)
        c = conv("replier_outbox", minutes=6)
        msg(c, 1, "BUYER", minutes=100)
        m = msg(c, 2, "SELLER", mid="EBAY-OB-1", minutes=90)
        db.add(OutboundMessage(store_id="STORE_A", conversation_id=c.id, body="b", source="manual", actor="carol", state="SENT",
                               send_after=now, message_id=m.id, platform_message_id="EBAY-OB-1"))
        db.add(OutboundMessage(store_id="STORE_A", conversation_id=c.id, body="b2", source="manual", actor="carol", state="PENDING",
                               send_after=now))
        db.add(OutboundMessage(store_id="STORE_A", conversation_id=c.id, body="b3", source="manual", actor="carol", state="SENDING",
                               send_after=now))
        c = conv("replier_intent", minutes=7)
        msg(c, 1, "BUYER", minutes=100)
        msg(c, 2, "SELLER", mid="EBAY-SI-1", minutes=90)
        j = job(c, "SENT")
        d = Draft(job_id=j.id, run_generation=1, version=1, author="agent", conversation_row_version=1, text="x", status="SENT")
        db.add(d)
        db.flush()
        db.add(SendIntent(job_id=j.id, draft_id=d.id, idempotency_key="acc-si-1", actor="AI 自动回复", text="x", content_hash="h",
                          state="SENT", platform_message_id="EBAY-SI-1"))
        c = conv("replier_sim", minutes=8)
        msg(c, 1, "BUYER", minutes=100)
        msg(c, 2, "SELLER", mid="SIM-OUT-xyz", minutes=90)
        msg(c, 3, "SELLER", mid="SIM-OUT-auto", minutes=80, auto_kind="ACK")
        c = conv("replier_ebay", minutes=9)
        msg(c, 1, "BUYER", minutes=100)
        msg(c, 2, "SELLER", mid="EBAY-BACK-1", minutes=90)
        db.add(OutboundMessage(store_id="STORE_A", conversation_id=c.id, body="f", source="manual", actor="x", state="UNCERTAIN",
                               send_after=now))
        # 4 黑名单：同一买家既有全部店铺记录又有店铺专属记录（全部店铺优先）；只有店铺专属；只有别的店铺的
        db.add(BuyerBlacklist(buyer="eb_bl_both", store_id="STORE_A", reason="店铺专属", created_by="t"))
        db.add(BuyerBlacklist(buyer="eb_bl_both", store_id=None, reason="全部店铺", created_by="t"))
        db.add(BuyerBlacklist(buyer="eb_bl_store", store_id="STORE_A", reason="店铺专属", created_by="t"))
        db.add(BuyerBlacklist(buyer="eb_bl_other", store_id="STORE_B", reason="别的店铺", created_by="t"))
        for n, b in (("bl_both", "EB_BL_Both "), ("bl_store", "eb_bl_store"), ("bl_other", "eb_bl_other")):
            c = conv(n, buyer=b, minutes=10)
            msg(c, 1, "BUYER")
        # 5 多个标记（按标签排序）、站点 UNKNOWN 用店铺默认站点、没有刊登号、没有任何消息
        c = conv("marks", site="UNKNOWN", minutes=11)
        msg(c, 1, "BUYER")
        for t in reversed(tags):
            db.add(ConversationMark(conversation_id=c.id, store_id="STORE_A", tag_id=t.id, marked_by="t", note=f"n{t.id}"))
        c = conv("no_item", item=None, site=None, minutes=12)
        msg(c, 1, "SELLER", mid="EBAY-ONLY-SELLER")
        conv("empty", minutes=13)
        # 6 店铺 B 的会话（客服只看店铺 A 时不出现）
        c = conv("store_b", store="STORE_B", minutes=14)
        msg(c, 1, "BUYER")
    return ids


def _reference(db, conv):
    """逐个会话查（旧实现用的就是这些函数）。"""
    from app.api import workbench as wb
    from app.models import SendIntent

    job = wb._current_job(db, conv.id)
    from app.models import Draft, InvoiceRequest, Message, OutboundMessage

    draft = db.scalar(select(Draft).where(Draft.job_id == job.id, Draft.status == "ACTIVE").order_by(Draft.id.desc()).limit(1)) \
        if job else None
    last = db.scalar(select(Message).where(Message.conversation_id == conv.id).order_by(Message.conversation_seq.desc()).limit(1))
    return {
        "job": wb._job_dict(job), "intent": draft.intent if draft else None,
        "auto_replied": bool(job and job.state == "SENT" and db.scalar(
            select(SendIntent.id).where(SendIntent.job_id == job.id, SendIntent.actor == wb.AUTO_ACTOR))),
        "invoice_status": db.scalar(select(InvoiceRequest.status).where(InvoiceRequest.conversation_id == conv.id)),
        "claim": wb._claim_dict(wb._active_claim(db, conv)), "last_replier": wb._last_replier(db, conv.id),
        "preview": (last.body[:80] if last else ""), "preview_role": last.sender_role if last else None,
        "mark": wb._conversation_mark(db, conv.id), "marks": wb._conversation_marks(db, conv.id),
        "blacklist": wb._blacklisted(db, conv),
        "outbox_pending": db.scalar(select(__import__("sqlalchemy").func.count(OutboundMessage.id)).where(
            OutboundMessage.conversation_id == conv.id, OutboundMessage.state.in_(("PENDING", "SENDING")))) or 0,
        "outbox_problem": bool(db.scalar(select(OutboundMessage.id).where(
            OutboundMessage.conversation_id == conv.id, OutboundMessage.state.in_(("FAILED", "UNCERTAIN"))).limit(1))),
        "item_url": wb._conv_item_url(db, conv),
    }


def test_list_matches_per_conversation_reference(client):
    from app.db import session_scope
    from app.models import Conversation

    ids = _seed_edge()
    items = {x["id"]: x for x in client.get("/internal/conversations", params={"group": "all"}).json()}
    assert set(ids.values()) <= set(items)
    with session_scope() as db:
        for name, cid in ids.items():
            ref = _reference(db, db.get(Conversation, cid))
            got = items[cid]
            bad = {k: (got[k], v) for k, v in ref.items() if got[k] != v}
            assert not bad, (name, bad)
    # 抽几个关键值，确认边界数据确实踩到了
    assert items[ids["invoice2"]]["intent"] == "shipping_status" and items[ids["invoice2"]]["invoice_status"] == "OPEN"
    assert items[ids["claim_expired"]]["claim"] is None and items[ids["claim_other_store"]]["claim"] is None
    assert items[ids["claim_ok"]]["claim"]["claimed_by"] == "alice"
    assert items[ids["replier_ack"]]["last_replier"] is None  # 最后一条是回执：不算
    assert items[ids["replier_outbox"]]["last_replier"] == "carol" and items[ids["replier_outbox"]]["outbox_pending"] == 2
    assert items[ids["replier_intent"]]["last_replier"] == "AI 自动回复"
    assert items[ids["replier_sim"]]["last_replier"] == "本系统" and items[ids["replier_ebay"]]["last_replier"] == "eBay 后台"
    assert items[ids["replier_ebay"]]["outbox_problem"] is True
    assert items[ids["bl_both"]]["blacklist"]["reason"] == "全部店铺"  # 全部店铺的记录优先（和详情页一致）
    assert items[ids["bl_store"]]["blacklist"]["reason"] == "店铺专属" and items[ids["bl_other"]]["blacklist"] is None
    assert [m["tag_id"] for m in items[ids["marks"]]["marks"]] == sorted(m["tag_id"] for m in items[ids["marks"]]["marks"]) \
        or len(items[ids["marks"]]["marks"]) == 3
    assert items[ids["marks"]]["item_url"] and items[ids["no_item"]]["item_url"] is None
    assert items[ids["empty"]]["preview"] == "" and items[ids["empty"]]["job"] is None


def test_blacklist_same_as_detail_page(client):
    ids = _seed_edge()
    for name in ("bl_both", "bl_store", "bl_other"):
        row = next(x for x in client.get("/internal/conversations", params={"group": "all"}).json() if x["id"] == ids[name])
        det = client.get(f"/internal/conversations/{ids[name]}").json()
        want = det.get("blacklist") if "blacklist" in det else (det.get("conversation") or {}).get("blacklist")
        assert row["blacklist"] == want, name



def test_cs_scope_list_and_version(client):
    ids = _seed_edge()
    make_user("cs_list_a", "CS", stores=["STORE_A"])
    cs = TestClient(client.app)
    login(cs, "cs_list_a")
    rows = cs.get("/internal/conversations", params={"group": "all"}).json()
    assert {x["store_id"] for x in rows} == {"STORE_A"} and ids["store_b"] not in {x["id"] for x in rows}
    assert cs.get("/internal/conversations/version", params={"store_id": "STORE_B"}).status_code == 404
    v = cs.get("/internal/conversations/version").json()["version"]
    from app.db import session_scope
    from app.ingestion.service import _append_message
    from app.models import Conversation

    with session_scope() as db:  # 店铺 B 来新消息：店铺 A 客服的标记不变
        _append_message(db, db.get(Conversation, ids["store_b"]), "B-NEW", "BUYER", "IN", "hi", None)
    assert cs.get("/internal/conversations/version", params={"since": v}).json()["changed"] is False
    with session_scope() as db:
        _append_message(db, db.get(Conversation, ids["claim_ok"]), "A-NEW", "BUYER", "IN", "hi", None)
    assert cs.get("/internal/conversations/version", params={"since": v}).json()["changed"] is True


# ---------------------------------------------------------------- 变更标记：各种变化
def _changes():
    from app.db import utcnow
    from app.models import (BuyerBlacklist, Conversation, ConversationMark, InvoiceRequest, MessageTag, OutboundMessage,
                            ReplyJob, WorkClaim)

    def conv(db, ids, name="claim_ok"):
        return db.get(Conversation, ids[name])

    def job(db, ids, name="claim_ok"):
        return db.scalar(select(ReplyJob).where(ReplyJob.conversation_id == ids[name]))

    return {
        "店铺回复": lambda db, ids: __import__("app.ingestion.service", fromlist=["x"])._append_message(
            db, conv(db, ids), "SELLER-NEW", "SELLER", "OUT", "reply", None),
        "任务状态": lambda db, ids: setattr(job(db, ids), "state", "SENT"),
        "发件箱新增": lambda db, ids: db.add(OutboundMessage(store_id="STORE_A", conversation_id=ids["claim_ok"], body="x",
                                                        source="manual", actor="t", state="PENDING", send_after=utcnow())),
        "认领到期": lambda db, ids: setattr(db.scalar(select(WorkClaim).where(WorkClaim.conversation_id == ids["claim_ok"])),
                                         "expires_at", utcnow() - timedelta(seconds=1)),
        "标记删除": lambda db, ids: db.delete(db.scalar(select(ConversationMark).where(ConversationMark.conversation_id == ids["marks"]))),
        "星标取消": lambda db, ids: (setattr(conv(db, ids), "starred", True), setattr(conv(db, ids), "starred_at", utcnow())),
        "人工接管": lambda db, ids: setattr(conv(db, ids), "handoff_state", "HUMAN"),
        "发票申请新增": lambda db, ids: db.add(InvoiceRequest(conversation_id=ids["claim_expired"], store_id="STORE_A", status="OPEN")),
        "发票申请状态": lambda db, ids: setattr(db.scalar(select(InvoiceRequest).where(InvoiceRequest.status == "OPEN")),
                                           "status", "DONE"),
        "新标签": lambda db, ids: db.add(MessageTag(name="验收新标签", color="none", sort=999, created_by="t")),
        "黑名单新增": lambda db, ids: db.add(BuyerBlacklist(buyer="eb_claim_ok", store_id=None, reason="r", created_by="t")),
        "黑名单删除": lambda db, ids: db.delete(db.scalar(select(BuyerBlacklist).where(BuyerBlacklist.buyer == "eb_bl_store"))),
        "已读": lambda db, ids: (setattr(conv(db, ids), "read_seq", 5), setattr(conv(db, ids), "read_at", utcnow())),
        "转售后": lambda db, ids: (setattr(conv(db, ids), "aftersales_status", "PENDING"),
                                setattr(conv(db, ids), "aftersales_updated_at", utcnow())),
    }


@pytest.mark.parametrize("what", list(_changes()))
def test_version_changes_on(client, what):
    from app.db import session_scope

    ids = _seed_edge()
    before = client.get("/internal/conversations/version").json()["version"]
    with session_scope() as db:
        _changes()[what](db, ids)
    assert client.get("/internal/conversations/version", params={"since": before}).json()["changed"] is True, what


def _misses():
    from app.db import utcnow
    from app.models import BuyerBlacklist, Conversation, ConversationMark, MessageTag, WorkClaim

    return {
        # 列表上显示的东西变了，但变更标记没变（前端最多 60 秒后整体刷新才看到）
        "标签改名或改颜色": lambda db, ids: setattr(db.scalar(select(MessageTag).join(
            ConversationMark, ConversationMark.tag_id == MessageTag.id).where(ConversationMark.conversation_id == ids["marks"])),
            "name", "改过的名字"),
        "黑名单改原因": lambda db, ids: setattr(db.scalar(select(BuyerBlacklist).where(BuyerBlacklist.buyer == "eb_bl_store")),
                                           "reason", "新原因"),
        "黑名单改范围（店铺专属改全部店铺）": lambda db, ids: setattr(
            db.scalar(select(BuyerBlacklist).where(BuyerBlacklist.buyer == "eb_bl_other")), "store_id", None),
        "标记改备注": lambda db, ids: setattr(db.scalar(select(ConversationMark).where(ConversationMark.conversation_id == ids["marks"])),
                                         "note", "新备注"),
        "认领续期": lambda db, ids: setattr(db.scalar(select(WorkClaim).where(WorkClaim.conversation_id == ids["claim_ok"])),
                                         "expires_at", utcnow() + timedelta(minutes=30)),
        "eBay 未读数": lambda db, ids: setattr(db.get(Conversation, ids["claim_ok"]), "platform_unread_count", 3),
    }


@pytest.mark.parametrize("what", list(_misses()))
def test_version_misses(client, what):
    """记录现状：这些变化会改变列表显示，但变更标记不变（低：60 秒整体刷新兜底）。用例断言“标记变了”，不满足时标 xfail。"""
    from app.db import session_scope

    ids = _seed_edge()
    before = client.get("/internal/conversations/version").json()["version"]
    list_before = client.get("/internal/conversations", params={"group": "all"}).json()
    with session_scope() as db:
        _misses()[what](db, ids)
    list_after = client.get("/internal/conversations", params={"group": "all"}).json()
    changed = client.get("/internal/conversations/version", params={"since": before}).json()["changed"]
    if list_after == list_before:
        pytest.skip(f"{what}：列表显示没变，不算问题")
    if not changed:
        pytest.xfail(f"问题 L1（低）：{what} 后列表显示变了，变更标记没变")


def test_add_missing_indexes_idempotent_sqlite(client):
    from sqlalchemy import inspect

    from app.db import ADD_INDEXES, add_missing_indexes, get_engine

    assert add_missing_indexes() == []
    assert add_missing_indexes() == []
    eng = get_engine()
    names = {ix["name"] for t in {x[0] for x in ADD_INDEXES} for ix in inspect(eng).get_indexes(t)}
    assert {x[1] for x in ADD_INDEXES} <= names
