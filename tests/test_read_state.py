"""会话已读/未读：团队共享一份已读位置；eBay 侧未读数只让已读位置前进；工作台按买家名模糊搜索。"""
import copy
from datetime import datetime, timedelta

import pytest

from test_ebay_sync import CONVS, FakeEbay, ebay_env, make_sync  # noqa: F401  ebay_env 是夹具

AT = datetime(2026, 10, 26, 12, 0, 0)


def conv(db, key, buyer, store="RD"):
    from app.models import Conversation

    c = Conversation(store_id=store, ebay_conversation_id=key, buyer_key=buyer, channel_source="ebay",
                     last_message_at=AT)
    db.add(c)
    db.flush()
    return c


def msgs(db, c, roles):
    """roles 形如 "BSB"：B 买家来信、S 店铺发出、Y eBay 系统消息。"""
    from app.models import Message

    for i, r in enumerate(roles, 1):
        role, direction = {"B": ("BUYER", "IN"), "S": ("SELLER", "OUT"), "Y": ("SYSTEM", "IN")}[r]
        db.add(Message(store_id=c.store_id, ebay_message_id=f"{c.ebay_conversation_id}-{i}", conversation_id=c.id,
                       conversation_seq=i, sender_role=role, direction=direction, body=r,
                       sent_at=AT + timedelta(minutes=i)))
    db.flush()


@pytest.fixture()
def rd(app_env):
    from app.db import session_scope
    from app.models import Store

    with session_scope() as db:
        db.add(Store(id="RD", name="已读测试店", default_marketplace_id="EBAY_DE"))


def test_mark_read_unread_counts_only_incoming(rd):
    from app.db import session_scope
    from app.read_state import mark_read, mark_unread, unread_count

    with session_scope() as db:
        c = conv(db, "A", "anna_buyer")
        msgs(db, c, "BSBB")
        assert unread_count(db, c) == 3  # 店铺发出的不算未读
        assert mark_read(db, c, "小王") and (c.read_seq, c.read_by) == (4, "小王")
        assert unread_count(db, c) == 0 and not mark_read(db, c, "小王")
        assert mark_unread(db, c, "小王") and unread_count(db, c) == 1  # 退到最后一条收到的消息之前
        assert not mark_unread(db, c, "小王")


def test_platform_unread_only_moves_forward(rd):
    from app.db import session_scope
    from app.read_state import EBAY_READER, apply_platform_unread, mark_read, unread_count

    with session_scope() as db:
        c = conv(db, "B", "bert")
        msgs(db, c, "BBSBB")
        assert apply_platform_unread(db, c, 2)  # eBay：最后 2 条未读 → 第 3 条（店铺回复）及之前都已读
        assert (c.read_seq, c.read_by, c.platform_unread_count) == (3, EBAY_READER, 2)
        assert unread_count(db, c) == 2
        assert not apply_platform_unread(db, c, None)  # 列表没给未读数时不动
        mark_read(db, c, "小王")
        assert not apply_platform_unread(db, c, 2)  # 这边已读，eBay 仍显示未读：不改回未读
        assert unread_count(db, c) == 0 and c.platform_unread_count == 2

        d = conv(db, "C", "carl")
        msgs(db, d, "BSB")
        assert apply_platform_unread(db, d, 5)  # 未读数比收到的消息还多：全部未读，店铺回复之前算已读
        assert d.read_seq == 2 and unread_count(db, d) == 1


def test_list_search_unread_filter_and_read_api(rd, client):
    from app.db import session_scope

    with session_scope() as db:
        a = conv(db, "D", "lyy33495")
        msgs(db, a, "B")
        b = conv(db, "E", "psychologist_99")
        msgs(db, b, "BS")
        c = conv(db, "F", "lyy_100%")
        msgs(db, c, "B")
        ids = {"a": a.id, "b": b.id, "c": c.id}

    def names(**params):
        r = client.get("/internal/conversations", params={"group": "all", "store_id": "RD", **params})
        assert r.status_code == 200
        return sorted(i["buyer_key"] for i in r.json())

    assert names(q="LYY") == ["lyy33495", "lyy_100%"]  # 模糊、不区分大小写
    assert names(q="y_1") == ["lyy_100%"]  # 下划线按字面匹配，不当通配符
    assert names(q="100%") == ["lyy_100%"]
    assert names(q="no-such") == []

    item = next(i for i in client.get("/internal/conversations", params={"group": "all"}).json() if i["id"] == ids["b"])
    assert item["unread_count"] == 1 and item["read_by"] is None
    store = next(s for s in client.get("/internal/stores").json() if s["id"] == "RD")
    assert store["unread_conversations"] == 3

    r = client.post(f"/internal/conversations/{ids['a']}/read", json={"read": True})
    # 标记人是当前登录账号的显示名
    assert r.json() == {"changed": True, "unread_count": 0, "read_by": "tester", "read_at": r.json()["read_at"]}
    assert names(unread_only="true") == ["lyy_100%", "psychologist_99"]
    client.post(f"/internal/conversations/{ids['a']}/read", json={"read": False})
    assert names(unread_only="true") == ["lyy33495", "lyy_100%", "psychologist_99"]

    r = client.post("/internal/conversations/read-batch", json={"conversation_ids": list(ids.values())})
    assert r.json() == {"changed": 3}
    assert names(unread_only="true") == []
    detail = client.get(f"/internal/conversations/{ids['a']}").json()["conversation"]
    assert (detail["unread_count"], detail["read_by"]) == (0, "tester")


def test_sync_applies_ebay_unread_and_backfills_once(ebay_env, monkeypatch):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Conversation, EbaySyncState
    from app.read_state import EBAY_READER, INITIAL_READER, unread_count

    convs = copy.deepcopy(CONVS)
    unread = {"C1": 0, "C2": 1, "C3": 0, "C4": 1, "E1": 1}
    for items in convs.values():
        for c in items:
            c["unreadCount"] = unread[c["conversationId"]]
    monkeypatch.setattr("test_ebay_sync.CONVS", convs)

    assert make_sync(FakeEbay()).run()["ok"]
    with session_scope() as db:
        got = {c.ebay_conversation_id: (unread_count(db, c), c.read_by) for c in db.scalars(select(Conversation))}
        assert got == {"C1": (0, EBAY_READER), "C2": (1, EBAY_READER), "C3": (0, EBAY_READER),
                       "C4": (1, EBAY_READER), "E1": (1, EBAY_READER)}
        # 新店铺第一次同步时会话都按 eBay 未读数建好，不需要再回填
        assert all(s.read_backfilled_at for s in db.scalars(select(EbaySyncState)))

        # 模拟功能上线前就有的会话：已读位置为空、游标未回填
        legacy = Conversation(store_id="REAL_US", ebay_conversation_id="OLD", buyer_key="old_buyer",
                              conversation_type="FROM_MEMBERS", channel_source="ebay")
        db.add(legacy)
        for s in db.scalars(select(EbaySyncState)):
            s.read_backfilled_at = None
        db.get(Conversation, db.scalar(select(Conversation.id).where(
            Conversation.ebay_conversation_id == "C2"))).read_seq = None

    convs["FROM_MEMBERS"][1]["unreadCount"] = 0  # 期间在 eBay 后台看过 C2
    r = make_sync(FakeEbay()).run()
    assert r["ok"] and r["types"]["FROM_MEMBERS"]["read_initialized"] == 1
    with session_scope() as db:
        by_key = {c.ebay_conversation_id: c for c in db.scalars(select(Conversation))}
        assert (by_key["OLD"].read_by, unread_count(db, by_key["OLD"])) == (INITIAL_READER, 0)
        assert unread_count(db, by_key["C2"]) == 0 and by_key["C2"].platform_unread_count == 0
        assert all(s.read_backfilled_at for s in db.scalars(select(EbaySyncState)))


def test_buyer_and_system_conversations_listed_separately(rd, client):
    from app.db import session_scope

    with session_scope() as db:
        buyer = conv(db, "G", "anna_buyer")
        msgs(db, buyer, "B")
        system = conv(db, "H", "ebay")
        system.conversation_type, system.conversation_title = "FROM_EBAY", "Return 123: refund sent"
        msgs(db, system, "Y")

    def keys(**params):
        r = client.get("/internal/conversations", params={"group": "all", "store_id": "RD", **params})
        return [i["buyer_key"] for i in r.json()]

    assert keys(kind="buyer") == ["anna_buyer"]
    assert keys(kind="system") == ["ebay"]
    assert sorted(keys()) == ["anna_buyer", "ebay"]  # 不带 kind 仍是全部
    assert keys(kind="system", q="REFUND") == ["ebay"]  # 系统消息按标题搜
    assert keys(kind="buyer", q="refund") == []
    store = next(s for s in client.get("/internal/stores").json() if s["id"] == "RD")
    assert (store["unread_conversations"], store["unread_system"]) == (1, 1)


def test_list_order_newest_first_except_pending(rd, client):
    from datetime import timedelta

    from app.db import session_scope

    with session_scope() as db:
        old = conv(db, "OLD", "old_buyer")
        new = conv(db, "NEW", "new_buyer")
        old.last_message_at, new.last_message_at = AT - timedelta(hours=5), AT
        nul = conv(db, "NUL", "no_message_yet")
        nul.last_message_at = None

    names = [i["buyer_key"] for i in client.get("/internal/conversations",
                                                 params={"group": "all", "store_id": "RD"}).json()]
    assert names == ["new_buyer", "old_buyer", "no_message_yet"]  # 新的在前，没有消息的排最后
