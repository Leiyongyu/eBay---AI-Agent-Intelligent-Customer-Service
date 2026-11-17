"""审核员改中文对照：中文译成买家语言填回正式回复（语言按 AI 草稿，再按站点；用量按会话记录）。"""
from sqlalchemy import select


class FakeLLM:
    def __init__(self):
        self.calls = []

    def translate_with_usage(self, text, target_language="中文"):
        self.calls.append((text, target_language))
        return f"[{target_language}] {text}", {"total_tokens": 7}


def test_translate_from_zh_uses_draft_language_then_site(client, monkeypatch):
    from app.api import workbench
    from app.db import session_scope
    from app.ingestion.service import _append_message, get_or_create_conversation
    from app.models import Draft, LlmUsage, ReplyJob

    fake = FakeLLM()
    monkeypatch.setattr(workbench, "get_chat_provider", lambda: fake)
    with session_scope() as db:
        de = get_or_create_conversation(db, "STORE_A", "C-ZH1", "buyer_zh1", None, "EBAY_DE", None)
        gb = get_or_create_conversation(db, "STORE_A", "C-ZH2", "buyer_zh2", None, "EBAY_GB", None)
        msg = _append_message(db, gb, "M-ZH2", "BUYER", "IN", "Bonjour, où est mon colis ?", None)
        db.flush()
        job = ReplyJob(conversation_id=gb.id, store_id="STORE_A", state="REVIEW_REQUIRED",
                       anchor_source_message_id=msg.id)
        db.add(job)
        db.flush()
        db.add(Draft(job_id=job.id, run_generation=1, version=1, author="agent", text="Bonjour", language="fr",
                     intent="other", intents=[], action="reply", claims=[], conversation_row_version=1))
        de_id, gb_id = de.id, gb.id

    r = client.post(f"/internal/conversations/{de_id}/translate-from-zh", json={"text_zh": "包裹已到取件点，请尽快去取"}).json()
    assert (r["language"], r["language_name"]) == ("de", "德语") and r["text"].startswith("[德语")
    r = client.post(f"/internal/conversations/{gb_id}/translate-from-zh", json={"text_zh": "您好"}).json()
    assert r["language"] == "fr"  # 有 AI 草稿时按草稿语言（买家来信语言），不按站点
    r = client.post(f"/internal/conversations/{gb_id}/translate-from-zh", json={"text_zh": "您好", "language": "en"}).json()
    assert r["language_name"] == "英语"
    r = client.post(f"/internal/conversations/{de_id}/translate-from-zh", json={"text_zh": "您好", "language": "zh"}).json()
    assert r["text"] == "您好" and len(fake.calls) == 3  # 买家用中文：不用翻译
    with session_scope() as db:
        rows = db.scalars(select(LlmUsage).where(LlmUsage.purpose == "translate")).all()
        assert {u.conversation_id for u in rows} == {de_id, gb_id}
