"""eBay HTML 邮件通知转纯文本：转换规则、升级前已入库消息的补转换、原邮件接口。"""
from app.ebay.html_text import html_to_text, looks_like_html, split_body

MAIL = """<!DOCTYPE html><!--e50ee5d9--><html><head><title>t</title>
<style type="text/css">.hide{display:none!important}</style></head>
<body><div class="hdr">謝謝你發出退款給&nbsp;buyer_x</div>
<p>你為這件物品發出了 GBP 22.17 的退款。<br>查看退貨詳情</p>
<table><tr><td>訂單編號：</td><td>11-15322-53941</td></tr></table>
<script>document.write('x')</script><noscript>no</noscript></body></html>"""


def test_html_to_text_rules():
    assert looks_like_html(MAIL)
    assert html_to_text(MAIL) == ("謝謝你發出退款給 buyer_x\n你為這件物品發出了 GBP 22.17 的退款。\n查看退貨詳情\n"
                                  "訂單編號： 11-15322-53941")
    # 买家纯文本里偶尔出现尖括号、行内标签，不当 HTML
    for text in ("Price < 5 EUR > 3 EUR", "I need <b>2</b> pieces", "Hallo,\nwo ist mein Paket?"):
        assert not looks_like_html(text) and split_body(text) == (text, None)
    assert split_body(None) == ("", None)


def test_convert_stored_and_raw_html_endpoint(client):
    from sqlalchemy import select

    from app.db import session_scope
    from app.ebay.html_text import convert_stored
    from app.ingestion.service import _append_message, get_or_create_conversation
    from app.models import Message

    with session_scope() as db:
        conv = get_or_create_conversation(db, "STORE_A", "E-HTML", "ebay", None, "EBAY_GB", None)
        conv.channel_source, conv.conversation_type = "ebay", "FROM_EBAY"
        html_msg = _append_message(db, conv, "S-1", "SYSTEM", "IN", MAIL, None)
        html_msg.zh_translation = "（对 HTML 的旧译文）"
        plain = _append_message(db, conv, "S-2", "SYSTEM", "IN", "Price < 5 EUR, <br> not a mail", None)
        conv_id, html_id, plain_id = conv.id, html_msg.id, plain.id
    with session_scope() as db:
        assert convert_stored(db) == 1
    with session_scope() as db:
        m = db.get(Message, html_id)
        assert m.body.startswith("謝謝你發出退款給 buyer_x") and m.body_html == MAIL and m.zh_translation is None
        assert db.get(Message, plain_id).body_html is None
        assert convert_stored(db) == 0  # 幂等

    d = client.get(f"/internal/conversations/{conv_id}").json()
    assert [(x["id"], x["has_html"]) for x in d["messages"]] == [(html_id, True), (plain_id, False)]
    assert "<" not in d["messages"][0]["body"]
    assert client.get(f"/internal/messages/{html_id}/html").json()["html"] == MAIL
    assert client.get(f"/internal/messages/{plain_id}/html").status_code == 404
    with session_scope() as db:
        assert db.scalar(select(Message.body).where(Message.id == plain_id)) == "Price < 5 EUR, <br> not a mail"
