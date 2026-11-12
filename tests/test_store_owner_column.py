"""工作台“负责人”列显示店铺负责客服（账号管理里分配的店铺），不是正在看会话的管理员（2026-11-10 用户要求）。"""
from datetime import timedelta

from conftest import make_user


def _conv(store, name, claimed_by=None):
    from app.db import session_scope, utcnow
    from app.models import Conversation, Message, ReplyJob, WorkClaim

    now = utcnow()
    with session_scope() as db:
        c = Conversation(store_id=store, ebay_conversation_id=f"E-own-{name}", buyer_key=f"eb_own_{name}",
                         conversation_type="FROM_MEMBERS", marketplace_id="EBAY_US", last_message_at=now)
        db.add(c)
        db.flush()
        db.add(Message(store_id=store, ebay_message_id=f"M-own-{name}", conversation_id=c.id, conversation_seq=1,
                       sender_role="BUYER", direction="IN", sent_at=now, body="hi"))
        db.add(ReplyJob(store_id=store, conversation_id=c.id, anchor_source_message_id="x", state="REVIEW_REQUIRED"))
        if claimed_by:
            db.add(WorkClaim(store_id=store, conversation_id=c.id, claimed_by=claimed_by, expires_at=now + timedelta(minutes=5)))
        return c.id


def _rows(client, **params):
    return {x["id"]: x for x in client.get("/internal/conversations", params={"group": "all", **params}).json()}


def test_owner_is_store_cs_not_admin_claim(client):
    make_user("cs_owner_a", "CS", stores=["STORE_A"], display_name="景丽娟")
    a = _conv("STORE_A", "a", claimed_by="管理员")
    b = _conv("STORE_B", "b")
    rows = _rows(client)
    assert rows[a]["store_owner"] == "景丽娟"
    assert rows[a]["claim"]["claimed_by"] == "管理员"  # 认领仍返回，页面显示为“处理中：管理员”
    assert rows[b]["store_owner"] is None  # 没分配客服的店铺：页面显示“未分配”


def test_admin_is_never_owner_and_all_stores_cs_fills_gaps(client):
    make_user("admin_owner_x", "ADMIN", stores=["STORE_A"], display_name="管理员X")
    make_user("cs_owner_b", "CS", stores=["STORE_A"], display_name="曾婷婷")
    make_user("cs_owner_all", "CS", all_stores=True, display_name="全店客服")
    a, b = _conv("STORE_A", "a2"), _conv("STORE_B", "b2")
    rows = _rows(client)
    assert rows[a]["store_owner"] == "曾婷婷"  # 专门分配的优先，管理员不算
    assert rows[b]["store_owner"] == "全店客服"


def test_assignee_filter_uses_store_owner(client):
    make_user("cs_owner_c", "CS", stores=["STORE_A"], display_name="景丽娟")
    a, b = _conv("STORE_A", "a3"), _conv("STORE_B", "b3")
    mine = _rows(client, assignee="景丽娟")
    assert a in mine and b not in mine
    unassigned = _rows(client, assignee="unassigned")
    assert b in unassigned and a not in unassigned
