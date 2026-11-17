"""发件箱：所有发给买家的消息延迟发出、发出前可撤回；对接 eBay 的店铺真实调用 sendMessage；人工接管后 AI 不再生成回复。"""
import pytest

from app import config
from test_generate_draft import make_conversation
from test_pipeline import detail, draft_json, send_buyer, use_fake_llm


@pytest.fixture()
def delayed(monkeypatch):
    monkeypatch.setenv("SEND_DELAY_SECONDS", "300")
    config.get_settings.cache_clear()


def _due_now(ob_id):
    """把待发消息的发送时间提前到现在（等同过了 5 分钟）。"""
    from app.db import session_scope, utcnow
    from app.models import OutboundMessage

    with session_scope() as db:
        db.get(OutboundMessage, ob_id).send_after = utcnow()


def _ebay_conv(store="STORE_A"):
    from app.db import session_scope
    from app.ingestion.service import _append_message, get_or_create_conversation

    with session_scope() as db:
        c = get_or_create_conversation(db, store, f"EB-CONV-{store}", "buyer_live", "110000000001", "EBAY_US", "listing")
        c.channel_source, c.ebay_conversation_id = "ebay", f"EBC-{store}"
        _append_message(db, c, f"EB-IN-{store}", "BUYER", "IN", "Is it in stock?", None)
        db.flush()
        return c.id


def test_delay_recall_then_send(client, delayed):
    from app.worker import worker

    cid = make_conversation(["BUYER"])
    use_fake_llm(draft_json())
    client.post(f"/internal/conversations/{cid}/generate-draft")
    worker.run_once()
    draft = detail(client, cid)["agent_draft"]

    r = client.post(f"/internal/conversations/{cid}/send", json={"text": draft["text"]}).json()
    assert r["state"] == "PENDING" and r["message_id"] is None
    d = detail(client, cid)
    assert d["job"]["state"] == "SENDING" and [m["role"] for m in d["messages"]] == ["BUYER"]  # 还没进聊天记录
    assert [(o["state"], o["source"]) for o in d["outbox"]] == [("PENDING", "manual")]
    card = next(i for i in client.get("/internal/conversations", params={"group": "all"}).json() if i["id"] == cid)
    assert card["outbox_pending"] == 1
    worker.run_once()  # 没到时间：不发
    assert detail(client, cid)["outbox"][0]["state"] == "PENDING"

    # 撤回：不会发出，任务回到待审核、草稿可再改
    ob = client.post(f"/internal/outbox/{r['outbound_id']}/recall").json()
    assert ob["state"] == "RECALLED" and ob["recalled_by"] == "tester"
    d = detail(client, cid)
    assert d["job"]["state"] == "REVIEW_REQUIRED" and d["agent_draft"]["status"] == "ACTIVE"
    _due_now(r["outbound_id"])
    worker.run_once()
    assert [m["role"] for m in detail(client, cid)["messages"]] == ["BUYER"]  # 撤回的到点也不发
    assert client.post(f"/internal/outbox/{r['outbound_id']}/recall").status_code == 409

    # 再发一次，过了 5 分钟才发出；发出后不能撤回
    r2 = client.post(f"/internal/conversations/{cid}/send", json={"text": "Edited reply."}).json()
    _due_now(r2["outbound_id"])
    worker.run_once()
    d = detail(client, cid)
    assert d["job"]["state"] == "SENT" and d["messages"][-1]["body"] == "Edited reply."
    assert all(o["state"] != "PENDING" for o in d["outbox"])
    assert client.post(f"/internal/outbox/{r2['outbound_id']}/recall").status_code == 409


def test_live_store_sends_to_ebay_others_stay_local(client, monkeypatch):
    from app.ebay import auth as ebay_auth
    from app.ebay import message_api

    calls = []

    def fake_send(cli, **kw):
        calls.append(kw)
        return {"messageId": f"EBAY-MSG-{len(calls)}", "createdDate": "2026-11-05T10:00:00.000Z"}

    monkeypatch.setattr(message_api, "send_message", fake_send)
    monkeypatch.setattr(ebay_auth, "for_store", lambda store_id, *a, **k: None)
    monkeypatch.setenv("EBAY_CHANNEL", "ebay")
    monkeypatch.setenv("EBAY_LIVE_STORES", "store_a")
    config.get_settings.cache_clear()

    live, other = _ebay_conv("STORE_A"), _ebay_conv("STORE_B")
    client.post(f"/internal/conversations/{live}/send", json={"text": "Yes, in stock."})
    client.post(f"/internal/conversations/{other}/send", json={"text": "Yes, in stock."})
    assert calls == [{"text": "Yes, in stock.", "conversation_id": "EBC-STORE_A", "other_party": None,
                      "item_id": "110000000001", "media": None}]
    assert detail(client, live)["messages"][-1]["ebay_message_id"] == "EBAY-MSG-1"  # 存 eBay 的消息 ID，拉回来不会重复
    assert detail(client, other)["messages"][-1]["simulated"]  # 没对接的店铺只记在本地

    # 超时等结果不确定：标“结果不确定”，不重发
    def boom(cli, **kw):
        raise message_api.EbaySendUncertain("发送请求中断（ReadTimeout），不确定是否已发出")

    monkeypatch.setattr(message_api, "send_message", boom)
    client.post(f"/internal/conversations/{live}/send", json={"text": "Second."})
    ob = detail(client, live)["outbox"]
    assert [o["state"] for o in ob] == ["UNCERTAIN"] and "不确定" in ob[0]["error"]


def test_human_handoff_stops_ai_drafts(client):
    from app.db import session_scope
    from app.models import Conversation
    from app.worker import worker

    use_fake_llm(draft_json(), draft_json())
    first = send_buyer(client, "Where is my parcel?")
    cid = first["conversation_id"]
    with session_scope() as db:
        db.get(Conversation, cid).handoff_state = "HUMAN"
    worker.run_once()
    d = detail(client, cid)
    assert d["job"]["state"] == "REVIEW_REQUIRED" and "HUMAN_HANDOFF" in d["job"]["decision_reasons"]
    assert d["agent_draft"] is None  # 人工接管：AI 不生成回复，直接给客服


def test_pod_image_public_link_and_send_with_media(client, monkeypatch, tmp_path):
    """谷仓妥投照片：签名公网链接能取到文件（改签名取不到）；对接 eBay 的店铺发消息带上附件地址；地址打不开就不发。"""
    from app.db import session_scope
    from app.ebay import auth as ebay_auth
    from app.ebay import message_api
    from app.models import AftersalesFile
    from app.sending import channel as ch
    from app.sending import media_links
    from app.sending.outbox import enqueue

    monkeypatch.setenv("AFTERSALES_FILES_DIR", tmp_path.as_posix())
    monkeypatch.setenv("CREDENTIALS_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("EBAY_NOTIFY_ENDPOINT", "https://hook.example.com/webhooks/ebay/notification")
    monkeypatch.setenv("EBAY_CHANNEL", "ebay")
    monkeypatch.setenv("EBAY_LIVE_STORES", "STORE_A")
    config.get_settings.cache_clear()
    (tmp_path / "pod.jpg").write_bytes(b"\xff\xd8\xff fake jpeg")
    with session_scope() as db:
        f = AftersalesFile(store_id="STORE_A", source="GOODCANG_TICKET", original_name="POD 1.jpg", content_type="image/jpeg",
                           rel_path="pod.jpg", status="STORED")
        db.add(f)
        db.flush()
        fid = f.id
    url = media_links.url_for(fid, "POD 1.jpg")
    assert url.startswith("https://hook.example.com/webhooks/media/") and url.endswith("/POD_1.jpg")
    path = url.removeprefix("https://hook.example.com")
    assert client.get(path).content == b"\xff\xd8\xff fake jpeg"  # 不需要登录
    token = path.split("/")[3]
    assert client.get(path.replace(token, token[:-2] + "xx")).status_code == 404

    sent = []
    monkeypatch.setattr(message_api, "send_message", lambda cli, **kw: sent.append(kw) or {"messageId": "EB-POD-1"})
    monkeypatch.setattr(ebay_auth, "for_store", lambda *a, **k: None)
    monkeypatch.setattr(ch, "check_public_url", lambda u: None)
    media = [{"mediaName": "POD 1.jpg", "mediaType": "IMAGE", "mediaUrl": url}]
    cid = _ebay_conv("STORE_A")
    with session_scope() as db:
        from app.models import Conversation

        ob = enqueue(db, db.get(Conversation, cid), "Here is the proof of delivery.", actor="AI", source="ticket",
                     meta={"ebay_media": media, "media": [{"mediaName": "POD 1.jpg", "mediaType": "IMAGE", "fileId": fid}]})
        assert ob.state == "SENT"
    assert sent[0]["media"] == media
    last = detail(client, cid)["messages"][-1]
    assert last["ebay_message_id"] == "EB-POD-1" and last["media"][0]["fileId"] == fid

    monkeypatch.setattr(ch, "check_public_url", lambda u: "HTTP 530")  # 隧道没开：不发，交人工
    with session_scope() as db:
        ob = enqueue(db, db.get(Conversation, cid), "Proof again.", actor="AI", source="ticket", meta={"ebay_media": media})
        assert ob.state == "FAILED" and "公网地址打不开" in ob.error
    assert len(sent) == 1


def test_human_following_still_drafts(client):
    """同事在 eBay 后台人工回复后（如在谈部分退款），买家再来信、AI 判断要转人工：规则是最后一句永远是我们的，
    草稿照样起草给跟进的同事审核，只标出“同事在跟进”；能直接回答的照常起草。"""
    from app.db import session_scope
    from app.ingestion.service import _append_message
    from app.models import Conversation
    from app.worker import worker

    use_fake_llm(draft_json(intent="return_refund", intents=["return_refund"], action="handoff", needs_human=True, claims=[],
                            reply_text="I will forward your request to my colleague.", reply_zh="我会转给同事。"))
    first = send_buyer(client, "I want to return it.", buyer="buyer_neg")
    worker.run_once()
    cid = first["conversation_id"]
    with session_scope() as db:  # 同事直接在 eBay 后台回复（不经本系统）：提议退一半、商品留着
        _append_message(db, db.get(Conversation, cid), "EBAY-SELLER-1", "SELLER", "OUT",
                        "Could we refund half and you keep the item?", None)
    use_fake_llm(draft_json(intent="return_refund", intents=["return_refund"], action="handoff", needs_human=True, claims=[],
                            reply_text="I understand. I will forward this to my colleague.", reply_zh="我会转给同事。"))
    send_buyer(client, "Unfortunately no, I don't need the item at all.", buyer="buyer_neg")
    worker.run_once()
    d = detail(client, cid)
    assert d["job"]["state"] == "REVIEW_REQUIRED" and "HUMAN_FOLLOWING" in d["job"]["decision_reasons"]
    assert d["agent_draft"] is not None  # 规则：最后一句永远是我们的——草稿照样起草，标出“同事在跟进”请同事核对

    # 能直接回答的（不需要转人工）照常起草
    use_fake_llm(draft_json())
    send_buyer(client, "Where is my parcel?", buyer="buyer_neg")
    worker.run_once()
    assert detail(client, cid)["agent_draft"] is not None



def test_resolve_sends_short_closing_instead_of_no_reply(client):
    """原“无需回复”：规则是最后一句永远是我们的——改为按买家语言发一句简短致谢，任务记为已发送。"""
    from app.review_ack import CLOSING_TEXT
    from app.worker import worker

    use_fake_llm(draft_json(intent="thanks_closing", intents=["thanks_closing"], reply_text="You're welcome!",
                            reply_zh="不客气！", language="en"))
    r = send_buyer(client, "Thanks a lot, all good!", buyer="buyer_close")
    worker.run_once()
    cid = r["conversation_id"]
    job_id = detail(client, cid)["job"]["id"]
    assert client.post(f"/internal/conversations/{cid}/claim", json={"action": "claim"}).status_code == 200
    out = client.post(f"/internal/jobs/{job_id}/resolve", json={}).json()
    assert out["ok"] and out["language"] == "en"
    d = detail(client, cid)
    assert d["job"]["state"] == "SENT"
    assert (d["messages"][-1]["role"], d["messages"][-1]["body"]) == ("SELLER", CLOSING_TEXT["en"])  # 最后一句是我们的
