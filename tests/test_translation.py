"""买家来信自动翻译：只译非中文的买家消息，结果存 zh_translation，计入用量；打开会话时把没译的入队。"""
from sqlalchemy import select


class FakeLLM:
    def __init__(self):
        self.calls = []

    def translate_with_usage(self, text, target_language="中文"):
        self.calls.append((text, target_language))
        return "你好，我的包裹在哪里？", {"total_tokens": 12}


def test_translate_buyer_messages_only(app_env):
    from app.db import session_scope
    from app.ingestion.service import _append_message, get_or_create_conversation
    from app.models import LlmUsage, Message
    from app.translation import BuyerTranslator, untranslated_ids

    with session_scope() as db:
        conv = get_or_create_conversation(db, "STORE_A", "C-TR1", "buyer_tr", None, "EBAY_DE", None)
        de = _append_message(db, conv, "M-TR1", "BUYER", "IN", "Hallo, wo ist mein Paket?", None)
        zh = _append_message(db, conv, "M-TR2", "BUYER", "IN", "你好，请问什么时候发货？", None)
        out = _append_message(db, conv, "M-TR3", "SELLER", "OUT", "Hello, it ships today.", None)
        ids = (de.id, zh.id, out.id)
        conv_id = conv.id
    with session_scope() as db:
        assert untranslated_ids(db, conv_id) == [ids[0]]  # 中文来信、店铺消息不译
    fake = FakeLLM()
    assert [BuyerTranslator.translate_one(i, provider=fake) for i in ids] == [True, False, False]
    assert BuyerTranslator.translate_one(ids[0], provider=fake) is False  # 译过的不再译
    with session_scope() as db:
        assert db.get(Message, ids[0]).zh_translation == "你好，我的包裹在哪里？"
        u = db.scalar(select(LlmUsage).where(LlmUsage.purpose == "translate"))
        assert (u.conversation_id, u.store_id) == (conv_id, "STORE_A")
        assert untranslated_ids(db, conv_id) == []
    assert len(fake.calls) == 1 and fake.calls[0][1] == "中文"


def test_detail_reports_translating_and_original(client, monkeypatch):
    from app.db import session_scope
    from app.ingestion.service import _append_message, get_or_create_conversation
    from app.translation import translator

    with session_scope() as db:
        conv = get_or_create_conversation(db, "STORE_A", "C-TR2", "buyer_tr2", None, "EBAY_GB", None)
        msg = _append_message(db, conv, "M-TR9", "BUYER", "IN", "Where is my parcel?", None)
        conv_id, mid = conv.id, msg.id
    queued = []
    monkeypatch.setattr(translator, "enqueue", lambda ids: queued.extend(ids))
    monkeypatch.setattr(translator, "pending", lambda ids: set(queued) & set(ids))
    m = client.get(f"/internal/conversations/{conv_id}").json()["messages"][0]
    assert queued == [mid] and m["translating"] is True and m["body"] == "Where is my parcel?" and m["zh_translation"] is None
