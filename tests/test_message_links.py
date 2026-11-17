"""买家在 eBay 消息里按模板发来“订单号 + WeTransfer 视频链接”：自动下载进售后附件、对上订单；
其他网盘链接登记提醒人工下载；同一个链接只处理一次。"""
from app.config import get_settings


def test_links_in_template_message():
    from app.aftersales_mail.message_links import links_in

    wt, other = links_in("我的订单号是：04-15163-40358，我的视频链接为：https://we.tl/t-nzWOAZzOUqq7pLpx。谢谢")
    assert wt == ["https://we.tl/t-nzWOAZzOUqq7pLpx"] and other == []
    wt, other = links_in("Meine Bestellnummer ist: 04-15163-40358, mein Videolink ist: we.tl/t-AbC123! "
                         "Fotos: https://drive.google.com/file/d/xyz/view")
    assert wt == ["https://we.tl/t-AbC123"] and other == ["https://drive.google.com/file/d/xyz/view"]
    assert links_in("Tracking: https://www.dhl.de/x?piececode=1") == ([], [])  # 物流链接不管


def test_fetch_video_link_from_buyer_message(app_env, monkeypatch, tmp_path):
    from sqlalchemy import select

    from app.aftersales_mail import message_links as ml
    from app.db import session_scope
    from app.ingestion.service import _append_message, get_or_create_conversation
    from app.models import AftersalesFile

    s = get_settings()
    monkeypatch.setattr(s, "aftersales_files_dir", str(tmp_path))
    monkeypatch.setattr(s, "aftersales_virus_scan", False)
    calls = []

    class Resp:
        def close(self):
            pass

    def fake_open(link, client):
        calls.append(link)
        return "problem.mp4", "video/mp4", iter([b"\x00\x00\x00\x18ftypmp42", b"x" * 100]), Resp()

    monkeypatch.setattr(ml.wetransfer, "open_download", fake_open)
    with session_scope() as db:
        conv = get_or_create_conversation(db, "STORE_A", "C-LINK", "buyer_link", None, "EBAY_DE", None)
        _append_message(db, conv, "M-L1", "BUYER", "IN",
                        "Meine Bestellnummer ist: 04-15163-40358, mein Videolink ist: https://we.tl/t-nzWOAZzOUqq7pLpx", None)
        _append_message(db, conv, "M-L2", "BUYER", "IN", "Hier noch Fotos: https://www.dropbox.com/s/abc/fotos.zip", None)
        conv_id = conv.id
        r = ml.run(db, http=object())
        assert (r["stored"], r["failed"]) == (1, 1)
        files = db.scalars(select(AftersalesFile).where(AftersalesFile.conversation_id == conv_id)
                           .order_by(AftersalesFile.id)).all()
        v, d = files
        assert (v.source, v.status, v.order_id, v.original_name, v.link_url) == (
            "WETRANSFER", "STORED", "04-15163-40358", "problem.mp4", "https://we.tl/t-nzWOAZzOUqq7pLpx")
        assert (tmp_path / v.rel_path).read_bytes().startswith(b"\x00\x00\x00\x18ftyp")
        assert (d.source, d.status) == ("LINK", "DOWNLOAD_FAILED") and "人工下载" in d.status_reason
        r = ml.run(db, http=object())
        assert (r["stored"], r["failed"]) == (0, 0) and len(calls) == 1  # 同一链接不重复处理
        # 买家先只发了链接（没写订单号），后来才补订单号：已下载的文件补上订单号
        _append_message(db, conv, "M-L3", "BUYER", "IN", "Video: https://we.tl/t-NoOrder1", None)
        r = ml.run(db, http=object())
        nf = db.scalar(select(AftersalesFile).where(AftersalesFile.link_url == "https://we.tl/t-NoOrder1"))
        assert r["stored"] == 1 and nf.order_id is None
        _append_message(db, conv, "M-L4", "BUYER", "IN", "Sorry, order 11-22222-33333", None)
        assert ml.run(db, http=object())["orders_filled"] == 1 and nf.order_id == "11-22222-33333"


def test_wetransfer_notification_mail_links_and_name_match(app_env, monkeypatch):
    """WeTransfer 通知邮件：只取真正的下载链接、同一传输只留一个（首页、两种编码的同一链接不重复）；
    主题 / 留言里写了 eBay 用户名的对到这个买家的会话；对买家说的售后邮箱可单独配置。"""
    from app.aftersales_mail.parse import wetransfer_downloads
    from app.aftersales_mail.service import locate_buyer_name
    from app.db import session_scope
    from app.ingestion.service import get_or_create_conversation

    links = ["https://wetransfer.com/?utm_source=wt_sendgrid&trk=TRN_DL_WTP_01",
             "https://wetransfer.com/downloads/393e0fe1677798c7/f18ee361b42a7556/20fd9a?t_rid=ZW1h%3D%3D&t_s=download_link",
             "https://wetransfer.com/downloads/393e0fe1677798c7/f18ee361b42a7556/20fd9a?t_rid=ZW1h==&t_s=download_link",
             "https://we.tl/t-AbC123", "https://wetransfer.com/explore"]
    assert wetransfer_downloads(links) == [links[1], links[3]]
    # 纯文本正文里折行截断的半截链接（传输号只剩前几位）：不当成另一个文件
    assert wetransfer_downloads(["https://wetransfer.com/downloads/393e0f", links[1]]) == [links[1]]

    with session_scope() as db:
        conv = get_or_create_conversation(db, "STORE_A", "C-NAME", "Ewok_3810", None, "EBAY_GB", None)
        loc = locate_buyer_name(db, "1483952649@qq.com sent you problem.mp4 via WeTransfer\nMessage: ewok_3810 turbo noise")
        assert (loc.conversation_id, loc.store_id, loc.match) == (conv.id, "STORE_A", "ebay_name")
        assert locate_buyer_name(db, "someone sent you video.mp4 via WeTransfer") is None
        get_or_create_conversation(db, "STORE_B", "C-NAME2", "ewok_3810", None, "EBAY_GB", None)
        assert locate_buyer_name(db, "ewok_3810") is None  # 两个店铺都有这个买家：拿不准，交人工认领

    s = get_settings()
    monkeypatch.setattr(s, "aftersales_mail_user", "inbox@shop.example")
    monkeypatch.setattr(s, "aftersales_mail_share_with_buyers", True)
    assert s.aftersales_public_email == "inbox@shop.example"
    monkeypatch.setattr(s, "aftersales_public_address", "service@shop.example")
    assert s.aftersales_public_email == "service@shop.example"
    monkeypatch.setattr(s, "aftersales_mail_share_with_buyers", False)
    assert s.aftersales_public_email is None



def test_reply_context_asks_for_order_when_link_has_none():
    """买家直接发了视频链接：对得上订单的告诉模型已收到、已转技术；对不上的请买家补订单号（多笔订单列出来选）。"""
    from types import SimpleNamespace

    from app.agent import nodes

    def state(text, orders=()):
        obs = ([{"tool_name": "get_authorized_order_context", "status": "FOUND", "data": {"order_id": orders[0]}}]
               if len(orders) == 1 else
               [{"tool_name": "get_authorized_order_context", "status": "AMBIGUOUS", "data": [{"order_id": o} for o in orders]}]
               if orders else [])
        return {"snapshot": {"buyer_texts": [text]}, "observations": obs}

    deps = SimpleNamespace()
    assert nodes._video_link_context(state("Hello, where is my parcel?"), deps) == []
    ok = nodes._video_link_context(state("My order number is: 04-15163-40358, my video link is: https://we.tl/t-abc"), deps)
    assert "对应订单 04-15163-40358" in ok[0] and "不用再要订单号" in ok[0]
    one = nodes._video_link_context(state("here is the video https://we.tl/t-abc", ["11-11111-11111"]), deps)
    assert "对应订单 11-11111-11111" in one[0]
    ask = nodes._video_link_context(state("here is the video https://we.tl/t-abc", ["1-1", "2-2"]), deps)
    assert "请买家回复这个视频对应的 eBay 订单号" in ask[0] and "1-1、2-2" in ask[0]
    none = nodes._video_link_context(state("video: https://drive.google.com/file/d/x"), deps)
    assert "网盘链接" in none[0] and "请买家回复" in none[0]
