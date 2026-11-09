"""飞书售后群工单：划分规则、起草（标题/资料缺什么）、发群（卡片 + 话题图片）、读回复（话题 + @机器人带订单号）、
起草买家回复（不自动发）、客服发出后回帖、买家追问追加到话题。全部用模拟的飞书客户端。"""
import json

from app.config import get_settings


class FakeFeishu:
    def __init__(self):
        self.cards, self.replies, self.thread_msgs, self.chat_msgs = [], [], {}, []

    def send_card(self, chat_id, card):
        self.cards.append((chat_id, card))
        return f"om_card{len(self.cards)}"

    def upload_image(self, name, content, content_type="image/jpeg"):
        return "img_key"

    def upload_file(self, name, content, file_type="stream"):
        if getattr(self, "fail_upload", False):
            from app.feishu.client import FeishuError
            raise FeishuError("上传失败")
        self.files = getattr(self, "files", []) + [(name, file_type, len(content))]
        return f"file_key_{file_type}_{name}"

    def reply(self, message_id, msg_type, content, in_thread=True):
        if msg_type == "file" and content.get("file_key", "").startswith("file_key_mp4_"):  # 真实飞书：230055 类型不匹配
            from app.feishu.client import FeishuError
            raise FeishuError("飞书接口返回错误 230055")
        self.replies.append((message_id, msg_type, content))
        return {"message_id": f"om_r{len(self.replies)}", "thread_id": f"omt_{message_id}"}

    def reply_text(self, message_id, text):
        return self.reply(message_id, "text", {"text": text})

    def messages(self, container_type, container_id, start_time=None, page_size=50):
        return self.thread_msgs.get(container_id, []) if container_type == "thread" else self.chat_msgs

    def members(self, chat_id):
        return {"ou_lei": "雷永玉"}

    def bot_open_id(self):
        return "ou_bot"

    def download_image(self, message_id, key):
        return b"\xff\xd8\xff fake"


class FakeLLM:
    def __init__(self):
        self.calls = []

    def translate_with_usage(self, text, target_language="中文"):
        return "安装时发现垫片太小，还缺一个卡箍。", {"total_tokens": 5}

    def structured(self, messages, schema):
        from app.providers.chat import StructuredResult

        self.calls.append(messages[-1].content)
        parsed = schema(language="en", reply_text="Hello, you can reuse your original gasket. We are checking the clamp.",
                        reply_zh="可以用原来的垫片，卡箍正在核实。", unanswered=["卡箍"])
        return StructuredResult(parsed, "", None, {"total_tokens": 9})


def _on(monkeypatch):
    s = get_settings()
    for k, v in {"feishu_case_enabled": True, "feishu_app_id": "cli_x", "feishu_app_secret": "sec",
                 "feishu_case_chat_id": "oc_test"}.items():
        monkeypatch.setattr(s, k, v)


def _conv(db, key="C-FS1", buyer="ewok_3810", site="EBAY_GB", text="The crush gasket is too small and the clamp is missing"):
    from app.ingestion.service import _append_message, get_or_create_conversation

    conv = get_or_create_conversation(db, "STORE_A", key, buyer, "406692564129", site, None)
    _append_message(db, conv, f"M-{key}", "BUYER", "IN", text, None)
    db.flush()
    return conv


def test_routing_rule_and_sku():
    from app.feishu.cases import category_for, clean_sku

    assert category_for(["quality_issue"], "") == "质量问题"
    assert category_for(["damaged_wrong", "return_refund"], "") == "破损错发缺件"
    assert category_for(["install_help"], "") == "安装技术问题"
    assert category_for(["fitment"], "Does it fit? VIN VR3EFYHWCMN537855") == "适配核对"
    assert category_for(["fitment"], "Does it fit my Golf?") is None  # 没给 VIN：AI 先要 VIN
    for intents in (["shipping_status"], ["return_refund"], ["item_spec"], ["cancel_request"]):
        assert category_for(intents, "") is None  # 物流、退货退款、商品参数、取消：不发群
    assert clean_sku("ZMY-DE-MCD-20351-0557") == "MCD-20351-0557"
    assert clean_sku("MCD-20351-0557-YXR") == "MCD-20351-0557-YXR"  # 二手件后缀保留


def test_suggest_send_poll_answer_followup(app_env, monkeypatch):
    from app.db import session_scope
    from app.feishu import cases as svc
    from app.ingestion.service import _append_message
    from app.models import AftersalesCase, Conversation

    _on(monkeypatch)
    llm = FakeLLM()
    monkeypatch.setattr("app.providers.chat.get_chat_provider", lambda: llm)
    fs = FakeFeishu()
    with session_scope() as db:
        conv = _conv(db)
        state = {"snapshot": {"buyer_texts": ["The crush gasket is too small and the clamp is missing"]},
                 "observations": [{"tool_name": "get_authorized_order_context", "status": "FOUND",
                                   "data": {"order_id": "11-15359-38989", "sku": "MCD-20351-0557-YXR"}}]}
        c = svc.suggest(db, conv, state, {"intents": ["damaged_wrong"]})
        assert (c.status, c.case_type, c.category) == ("DRAFT", "售后", "破损错发缺件")
        assert svc.title(c) == "eBay 英国售后 · MCD-20351-0557-YXR · 11-15359-38989"
        assert c.problem_zh.startswith("安装时") and c.missing  # 没有照片：提示先要照片
        assert svc.suggest(db, conv, state, {"intents": ["shipping_status"]}) is None
        svc.send(db, c, "tester", client=fs)
        assert (c.status, c.message_id, c.thread_id) == ("SENT", "om_card1", "omt_om_card1")
        chat, card = fs.cards[0]
        body = json.dumps(card, ensure_ascii=False)
        assert chat == "oc_test" and "【测试】eBay 英国售后" in body and "STORE_A" not in body  # 不显示店铺
        assert [e["text"]["content"].split("\n")[0] for e in card["elements"] if e.get("tag") == "div"] == [
            "**问题（中文）**", "**故障码 / VIN**", "**买家原文**"]
        case_id, conv_id = c.id, conv.id

    # 群里回复：话题里一条 + 主聊天 @ 机器人一条（不带订单号 → 最近一张还没回复的工单）
    fs.thread_msgs["omt_om_card1"] = [
        {"message_id": "om_card1", "msg_type": "interactive", "sender": {"sender_type": "app"}, "create_time": "1"},
        {"message_id": "om_t1", "msg_type": "text", "sender": {"id": "ou_lei", "sender_type": "user"},
         "create_time": "1793781775743", "body": {"content": json.dumps({"text": "垫片是压过的，用原来的也可以"})}}]
    fs.chat_msgs = [{"message_id": "om_c1", "msg_type": "text", "sender": {"id": "ou_lei", "sender_type": "user"},
                     "create_time": "1793781775743", "mentions": [{"key": "@_user_1", "id": "ou_bot"}],
                     "body": {"content": json.dumps({"text": "@_user_1 卡箍下午补寄"})}}]
    with session_scope() as db:
        stats = svc.poll(db, client=fs, provider=llm)
        c = db.get(AftersalesCase, case_id)
        assert stats["replies"] == 2 and c.status == "REPLIED" and c.draft_reply.startswith("Hello")
        assert [(r["sender"], r["via"]) for r in c.replies] == [("雷永玉", "thread"), ("雷永玉", "mention")]
        assert "垫片是压过的" in llm.calls[0] and "卡箍下午补寄" in llm.calls[0]
        assert svc.poll(db, client=fs, provider=llm)["replies"] == 0  # 不重复收
        # 客服审核后发出：工单标“已回复买家”，话题里回帖
        monkeypatch.setattr(svc, "FeishuClient", lambda *a, **k: fs)
        svc.after_seller_reply(db, db.get(Conversation, conv_id), "Hello ...", "您好……", "tester")
        assert c.status == "ANSWERED" and "已回复买家（tester）" in fs.replies[-1][2]["text"]
        # 买家追问：追加到话题，工单回到“已发待回复”
        _append_message(db, db.get(Conversation, conv_id), "M-FOLLOW", "BUYER", "IN", "Any news on the clamp?", None)
        db.flush()
        db.get(Conversation, conv_id)
        from app.models import Message
        m = db.query(Message).filter(Message.ebay_message_id == "M-FOLLOW").one()
        m.zh_translation = "卡箍有消息了吗？"
        assert svc.forward_followups(db, c, fs) == 1
        assert "买家追问" in fs.replies[-1][2]["text"] and c.status == "SENT"


def test_mention_with_order_goes_to_that_case(app_env, monkeypatch):
    from app.db import session_scope, utcnow
    from app.feishu import cases as svc
    from app.models import AftersalesCase

    _on(monkeypatch)
    fs = FakeFeishu()
    with session_scope() as db:
        ids = []
        for i, order in enumerate(("01-11111-11111", "02-22222-22222")):
            conv = _conv(db, key=f"C-MO{i}", buyer=f"b{i}")
            c = AftersalesCase(conversation_id=conv.id, store_id="STORE_A", order_id=order, case_type="售后", category="质量问题",
                               problem_zh="x", original_text="x", status="SENT", chat_id="oc_test",
                               message_id=f"om_case{i}", thread_id=f"omt_{i}", sent_at=utcnow())
            db.add(c)
            db.flush()
            ids.append(c.id)
        fs.chat_msgs = [{"message_id": "om_m1", "msg_type": "text", "sender": {"id": "ou_lei", "sender_type": "user"},
                         "create_time": "1793781775743", "mentions": [{"key": "@_user_1", "id": "ou_bot"}],
                         "body": {"content": json.dumps({"text": "@_user_1 01-11111-11111 同意退货"})}}]
        svc.poll(db, client=fs, provider=FakeLLM())
        assert db.get(AftersalesCase, ids[0]).replies and not db.get(AftersalesCase, ids[1]).replies


def test_case_endpoints(client, monkeypatch):
    from app.db import session_scope
    from app.feishu import cases as svc

    _on(monkeypatch)
    fs = FakeFeishu()
    monkeypatch.setattr(svc, "FeishuClient", lambda *a, **k: fs)
    monkeypatch.setattr("app.providers.chat.get_chat_provider", lambda: FakeLLM())
    with session_scope() as db:
        conv_id = _conv(db, key="C-EP1").id
    assert client.get(f"/internal/conversations/{conv_id}/aftersales-case").json()["case"] is None
    c = client.post(f"/internal/conversations/{conv_id}/aftersales-case", json={"category": "安装技术问题"}).json()
    assert (c["status"], c["case_type"], c["category"]) == ("DRAFT", "售前", "安装技术问题")  # 测试会话查不到订单：售前
    c = client.patch(f"/internal/aftersales-cases/{c['id']}", json={"problem_zh": "螺栓规格是多少？"}).json()
    c = client.post(f"/internal/aftersales-cases/{c['id']}/send").json()
    assert c["status"] == "SENT" and fs.cards
    assert client.patch(f"/internal/aftersales-cases/{c['id']}", json={"problem_zh": "改"}).status_code == 409
    lst = client.get("/internal/aftersales-cases").json()
    assert lst["counts"]["SENT"] == 1 and lst["items"][0]["title"].startswith("eBay 英国售前")
    assert client.post(f"/internal/aftersales-cases/{c['id']}/close").json()["status"] == "CLOSED"


def test_auto_send_complete_case_and_wait_for_missing(app_env, monkeypatch):
    """FEISHU_CASE_AUTO_SEND：资料齐全的工单直接发群，不用人工点；缺资料的留草稿等买家补齐；开关关着照旧要人工点。"""
    from app.db import session_scope
    from app.feishu import cases as svc

    _on(monkeypatch)
    monkeypatch.setattr("app.providers.chat.get_chat_provider", lambda: FakeLLM())
    fs = FakeFeishu()
    monkeypatch.setattr(svc, "FeishuClient", lambda *a, **k: fs)
    state = {"snapshot": {"buyer_texts": ["How do I install the turbo oil line?"]},
             "observations": [{"tool_name": "get_authorized_order_context", "status": "FOUND",
                               "data": {"order_id": "11-15359-38990", "sku": "TB-100"}}]}
    with session_scope() as db:
        off = svc.suggest(db, _conv(db, "C-AS0", text="How do I install it?"), state, {"intents": ["install_help"]})
        assert off.status == "DRAFT" and not fs.cards  # 开关默认关：照旧人工点
        monkeypatch.setattr(get_settings(), "feishu_case_auto_send", True)
        c = svc.suggest(db, _conv(db, "C-AS1", text="How do I install the turbo oil line?"), state,
                        {"intents": ["install_help"]})
        assert (c.status, c.sent_by) == ("SENT", "AI（自动发群）") and len(fs.cards) == 1
        assert fs.cards[0][0] == "oc_test" and "11-15359-38990" in json.dumps(fs.cards[0][1], ensure_ascii=False)
        broken = svc.suggest(db, _conv(db, "C-AS2", text="The part arrived broken"), state, {"intents": ["damaged_wrong"]})
        assert broken.status == "DRAFT" and broken.missing and len(fs.cards) == 1  # 缺照片：等买家补


def test_unknown_site_falls_back_to_order_then_store(app_env, monkeypatch):
    """eBay 拉来的会话站点是 UNKNOWN：标题用订单站点，没有订单用店铺默认站点（不出现“eBay UNKNOWN售后”）。"""
    from app.db import session_scope
    from app.feishu import cases as svc

    _on(monkeypatch)
    monkeypatch.setattr("app.providers.chat.get_chat_provider", lambda: FakeLLM())
    with session_scope() as db:
        st = {"snapshot": {"buyer_texts": ["VIN WBA8F72030K721065 fit?"]}, "observations": [
            {"tool_name": "get_authorized_order_context", "status": "FOUND",
             "data": {"order_id": "11-15359-38991", "sku": "X-1", "marketplace_id": "EBAY_DE"}}]}
        c = svc.suggest(db, _conv(db, "C-UK1", site="UNKNOWN", text="VIN WBA8F72030K721065 fit?"), st, {"intents": ["fitment"]})
        assert svc.title(c).startswith("eBay 德国售后")
        st["observations"] = []
        c2 = svc.suggest(db, _conv(db, "C-UK2", site="UNKNOWN", text="VIN WBA8F72030K721065 fit?"), st, {"intents": ["fitment"]})
        assert "UNKNOWN" not in svc.title(c2) and "售前" in svc.title(c2)


def test_optional_missing_does_not_block_auto_send(app_env, monkeypatch):
    """质量问题只缺“故障码截图（有的话）”这类可选项：照样自动发群；缺必需的（照片、VIN）不发。"""
    from app.db import session_scope
    from app.feishu import cases as svc
    from app.models import AftersalesCase

    _on(monkeypatch)
    monkeypatch.setattr(get_settings(), "feishu_case_auto_send", True)
    fs = FakeFeishu()
    with session_scope() as db:
        conv = _conv(db, "C-OPT")
        c = AftersalesCase(conversation_id=conv.id, store_id="STORE_A", origin="AI", status="DRAFT", category="质量问题",
                           case_type="售后", original_text="x", problem_zh="有缺陷",
                           missing=["诊断仪读出的故障码截图（有的话）"])
        db.add(c)
        db.flush()
        assert svc.auto_send(db, c, fs) and c.status == "SENT"
        c2 = AftersalesCase(conversation_id=_conv(db, "C-OPT2").id, store_id="STORE_A", origin="AI", status="DRAFT",
                            category="质量问题", case_type="售后", original_text="x", problem_zh="有缺陷",
                            missing=["问题照片或视频", "诊断仪读出的故障码截图（有的话）"])
        db.add(c2)
        db.flush()
        assert not svc.auto_send(db, c2, fs) and c2.status == "DRAFT"


def test_case_carries_videos_and_photos_from_mail_and_upload(app_env, monkeypatch, tmp_path):
    """售后邮箱 / 上传页来的视频和图片：发群时一起发到话题（视频发文件，图片发图，超 30MB 的写明去工作台看）；
    发群后买家再补的自动追加；缺照片 / 视频没发的工单，资料到了就补齐并自动发。"""
    from app.db import session_scope
    from app.feishu import cases as svc
    from app.models import AftersalesFile

    _on(monkeypatch)
    s = get_settings()
    monkeypatch.setattr(s, "aftersales_files_dir", str(tmp_path))
    monkeypatch.setattr(s, "feishu_case_auto_send", True)
    monkeypatch.setattr(svc, "FEISHU_FILE_MAX", 1000)
    monkeypatch.setattr("app.providers.chat.get_chat_provider", lambda: FakeLLM())
    fs = FakeFeishu()
    monkeypatch.setattr(svc, "FeishuClient", lambda *a, **k: fs)

    def add(db, conv, name, ctype, size, source="ATTACHMENT", order=None):
        (tmp_path / name).write_bytes(b"x" * size)
        f = AftersalesFile(store_id="STORE_A", order_id=order, conversation_id=conv.id if conv else None, source=source,
                           original_name=name, content_type=ctype, size_bytes=size, rel_path=name, status="STORED")
        db.add(f)
        db.flush()
        return f

    state = {"snapshot": {"buyer_texts": ["The part arrived broken"]},
             "observations": [{"tool_name": "get_authorized_order_context", "status": "FOUND",
                               "data": {"order_id": "11-15359-38999", "sku": "X-9"}}]}
    with session_scope() as db:
        conv = _conv(db, "C-VID", text="The part arrived broken")
        c = svc.suggest(db, conv, state, {"intents": ["damaged_wrong"]})
        assert c.status == "DRAFT" and c.missing  # 还没有照片 / 视频：先不发
        add(db, None, "broken.mp4", "video/mp4", 500, source="UPLOAD", order="11-15359-38999")  # 按订单号对上
        add(db, conv, "box.jpg", "image/jpeg", 200)
        big = add(db, conv, "long.mov", "video/quicktime", 5000)
        assert svc.refresh_waiting(db, fs) == 1 and c.status == "SENT"  # 资料到了：补齐并自动发群
        assert fs.files == [("broken.mp4", "mp4", 500)]
        kinds = [r[1] for r in fs.replies]
        assert "media" in kinds and "image" in kinds  # mp4 发“视频”消息，群里可直接播放
        assert any("long.mov" in json.dumps(r[2], ensure_ascii=False) and "30MB" in json.dumps(r[2], ensure_ascii=False)
                   for r in fs.replies)
        assert big.id in c.file_ids and len(c.file_ids) == 3
        # 发群后买家又通过售后邮箱补了一段视频：追加到话题，前面发过的不重复发
        add(db, conv, "more.mp4", "video/mp4", 300)
        before = len(fs.replies)
        assert svc._send_files(db, c, fs, header="买家补充了视频 / 图片：") == 1
        assert fs.replies[before][2]["text"].startswith("买家补充了") and fs.files[-1][0] == "more.mp4"
        assert svc._send_files(db, c, fs) == 0
        # 上传失败：不发“买家补充了…”说明，下一轮重试也不会刷屏
        add(db, conv, "again.mp4", "video/mp4", 300)
        fs.fail_upload, before = True, len(fs.replies)
        assert svc._send_files(db, c, fs, header="买家补充了视频 / 图片：") == 0
        assert svc._send_files(db, c, fs, header="买家补充了视频 / 图片：") == 0
        assert len(fs.replies) == before



def test_same_video_from_mail_and_link_sent_once(app_env, monkeypatch, tmp_path):
    """同一个视频邮箱收了一份、eBay 消息里的链接又下了一份（内容相同）：话题里只发一次。"""
    from app.db import session_scope
    from app.feishu import cases as svc
    from app.models import AftersalesCase, AftersalesFile

    _on(monkeypatch)
    monkeypatch.setattr(get_settings(), "aftersales_files_dir", str(tmp_path))
    fs = FakeFeishu()
    with session_scope() as db:
        conv = _conv(db, "C-DUP")
        for i, src in enumerate(("WETRANSFER", "WETRANSFER")):
            (tmp_path / f"v{i}.mp4").write_bytes(b"same-video")
            db.add(AftersalesFile(store_id="STORE_A", conversation_id=conv.id, source=src, original_name=f"v{i}.mp4",
                                  content_type="video/mp4", size_bytes=10, rel_path=f"v{i}.mp4", sha256="abc", status="STORED"))
        c = AftersalesCase(conversation_id=conv.id, store_id="STORE_A", origin="AI", status="SENT", category="质量问题",
                           case_type="售后", original_text="x", problem_zh="y", message_id="om_1")
        db.add(c)
        db.flush()
        assert svc._send_files(db, c, fs) == 1 and len(fs.files) == 1 and len(c.file_ids) == 2
        assert svc._send_files(db, c, fs) == 0
