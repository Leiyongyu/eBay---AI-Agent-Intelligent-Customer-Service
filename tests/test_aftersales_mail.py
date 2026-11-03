"""售后邮箱：解析邮件、按订单号定店铺和会话、按店铺文件夹与命名规则保存、类型与大小限制、WeTransfer 下载、
待认领与认领、权限（权限点 + 店铺范围）、每日清理、收信流程、合规允许本店售后邮箱。"""
from datetime import datetime, timedelta
from email.message import EmailMessage

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config
from conftest import login, make_user

ORDER = "12-34567-89012"


def mail(subject=f"Defekt Bestellung {ORDER}", body="Hallo, anbei das Video.", files=(), html=None,
         sender="buyer.one@example.de", msg_id="<m1@example.de>") -> bytes:
    m = EmailMessage()
    m["Subject"], m["From"], m["To"], m["Message-ID"] = subject, sender, "service@shop.example", msg_id
    m["Date"] = "Fri, 30 Oct 2026 10:00:00 +0100"
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    for name, ctype, data in files:
        maintype, subtype = ctype.split("/")
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return m.as_bytes()


@pytest.fixture()
def as_env(client, monkeypatch, tmp_path):
    from app.aftersales_mail import service
    from app.db import session_scope
    from app.ingestion.service import _append_message, get_or_create_conversation

    monkeypatch.setenv("AFTERSALES_FILES_DIR", (tmp_path / "files").as_posix())
    monkeypatch.setenv("AFTERSALES_VIRUS_SCAN", "false")
    monkeypatch.setenv("AFTERSALES_MAIL_USER", "service@shop.example")
    config.get_settings.cache_clear()
    orders = {ORDER: ("STORE_A", "buyer_as"), "98-76543-21098": ("STORE_B", "api_buyer")}
    monkeypatch.setattr(service, "lookup_order_api", lambda order_id: orders.get(order_id))
    with session_scope() as db:
        c = get_or_create_conversation(db, "STORE_A", "SIM-AS-1", "buyer_as", None, "EBAY_DE", "simulator")
        _append_message(db, c, "SIM-IN-AS-1", "BUYER", "IN", "Das Teil ist defekt", None)
        db.flush()
        client.conv_id = c.id
    return client


def process(raw, http=None):
    from app.aftersales_mail.service import process as _p
    from app.db import session_scope

    with session_scope() as db:
        m = _p(db, "1", raw, http)
        return m.id


def test_parse_mail():
    from app.aftersales_mail.mailbox import imap_utf7
    from app.aftersales_mail.parse import parse

    raw = mail(html=f'<p>Video: <a href="https://we.tl/t-AbC123">hier</a> Order {ORDER}</p>'
                    '<a href="https://drive.google.com/file/d/x">drive</a>',
               files=[("defekt.jpg", "image/jpeg", b"\xff\xd8jpeg")])
    pm = parse(raw)
    assert pm.order_ids == [ORDER] and pm.sender == "buyer.one@example.de"
    assert pm.wetransfer_links == ["https://we.tl/t-AbC123"] and pm.other_links == ["https://drive.google.com/file/d/x"]
    assert [(a.filename, a.content_type) for a in pm.attachments] == [("defekt.jpg", "image/jpeg")]
    assert imap_utf7("已处理") == "&XfJZBHQG-" and imap_utf7("A&B") == "A&-B"


def test_process_saves_by_store_and_links_conversation(as_env, tmp_path):
    from app.db import session_scope
    from app.models import AftersalesFile, AftersalesMail, ConversationEvent
    from sqlalchemy import select

    raw = mail(files=[("Video 1.mp4", "video/mp4", b"\x00\x00\x00\x18ftypmp42"), ("tool.exe", "application/octet-stream", b"MZ")])
    mid = process(raw)
    assert process(raw) == mid  # 同一 Message-ID 只处理一次
    with session_scope() as db:
        m = db.get(AftersalesMail, mid)
        assert (m.store_id, m.order_id, m.conversation_id, m.match, m.status, m.files) == (
            "STORE_A", ORDER, as_env.conv_id, "order_api", "PROCESSED", 1)
        files = db.scalars(select(AftersalesFile).where(AftersalesFile.mail_id == mid).order_by(AftersalesFile.id)).all()
        assert [f.status for f in files] == ["STORED", "BLOCKED_TYPE"]
        assert files[0].rel_path.startswith("STORE_A（stardrive_us_demo）/") and files[0].rel_path.split("/")[-1].startswith(f"{ORDER}_")
        assert files[0].rel_path.endswith("_01.mp4") and files[0].original_name == "Video 1.mp4"
        assert db.scalar(select(ConversationEvent.event_type).where(
            ConversationEvent.conversation_id == as_env.conv_id)) == "AFTERSALES_FILES_RECEIVED"
    assert (tmp_path / "files" / files[0].rel_path).read_bytes().endswith(b"mp42")

    # 本地没有的订单：逐店铺查到 STORE_B；没有订单号也对不上发票邮箱的：待认领
    process(mail(subject="Order 98-76543-21098", msg_id="<m2@x>", files=[("a.png", "image/png", b"png")]))
    un = process(mail(subject="Hilfe", msg_id="<m3@x>", sender="nobody@x.de", files=[("b.png", "image/png", b"png")]))
    with session_scope() as db:
        assert db.scalar(select(AftersalesMail.store_id).where(AftersalesMail.message_id == "<m2@x>")) == "STORE_B"
        f = db.scalar(select(AftersalesFile).where(AftersalesFile.mail_id == un))
        assert f.store_id is None and f.rel_path.startswith("_待认领/未匹配_")


def test_size_limit_and_wetransfer(as_env, monkeypatch):
    from app.db import session_scope
    from app.models import AftersalesFile
    from sqlalchemy import select

    monkeypatch.setenv("AFTERSALES_FILE_MAX_MB", "1")
    config.get_settings.cache_clear()

    def handler(req: httpx.Request):
        url = str(req.url)
        if url.startswith("https://we.tl/t-ok"):
            return httpx.Response(302, headers={"location": "https://wetransfer.com/downloads/aa11/bb22"})
        if url.startswith("https://wetransfer.com/downloads/"):
            return httpx.Response(200, text="page")
        if url.endswith("/api/v4/transfers/aa11/download"):
            assert req.method == "POST" and b'"security_hash":"bb22"' in req.content.replace(b" ", b"")
            return httpx.Response(200, json={"direct_link": "https://download.wetransfer.com/x/clip.mp4?token=1"})
        if url.startswith("https://download.wetransfer.com/"):
            return httpx.Response(200, content=b"v" * 1000, headers={"content-type": "video/mp4"})
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    mid = process(mail(html='<a href="https://we.tl/t-ok">a</a> <a href="https://we.tl/t-gone">b</a>',
                       files=[("big.jpg", "image/jpeg", b"x" * (1024 * 1024 + 1))], msg_id="<w1@x>"), http)
    with session_scope() as db:
        rows = {(f.source, f.status) for f in db.scalars(select(AftersalesFile).where(AftersalesFile.mail_id == mid))}
    assert rows == {("ATTACHMENT", "TOO_LARGE"), ("WETRANSFER", "STORED"), ("WETRANSFER", "DOWNLOAD_FAILED")}


def test_permissions_claim_and_content(as_env, tmp_path):

    client = as_env
    process(mail(files=[("p.jpg", "image/jpeg", b"\xff\xd8jpeg")]))
    un = process(mail(subject="Hilfe", msg_id="<u@x>", sender="nobody@x.de", files=[("q.jpg", "image/jpeg", b"jpg")]))
    make_user("cs_as", "CS", stores=["STORE_A"])
    cs = TestClient(client.app)
    login(cs, "cs_as")
    assert cs.get("/internal/aftersales-files").status_code == 403  # 没有“售后附件”权限
    assert cs.get("/api/auth/me").json()["features"] == {"aftersales_files": False}
    uid = next(u["id"] for u in client.get("/api/admin/users").json() if u["username"] == "cs_as")
    r = client.patch(f"/api/admin/users/{uid}", json={"can_aftersales_files": True})  # 管理员在账号管理里打开
    assert r.json()["can_aftersales_files"] is True
    assert cs.get("/api/auth/me").json()["features"] == {"aftersales_files": True}
    listed = cs.get("/internal/aftersales-files").json()
    assert [m["order_id"] for m in listed["items"]] == [ORDER] and listed["unclaimed"] is None
    fid = listed["items"][0]["files"][0]["id"]
    assert listed["items"][0]["files"][0]["days_left"] == 30 and listed["items"][0]["files"][0]["kind"] == "image"
    assert cs.get(f"/internal/aftersales-files/{fid}/content").content == b"\xff\xd8jpeg"
    assert cs.get("/internal/aftersales-files", params={"unclaimed": True}).status_code == 403
    conv_files = cs.get(f"/internal/conversations/{client.conv_id}/aftersales-files").json()
    assert conv_files["allowed"] and conv_files["items"][0]["files"][0]["id"] == fid

    # 待认领：管理员认领 → 挪到店铺文件夹并改名；客服不能认领别人店铺的/待认领的
    uf = client.get("/internal/aftersales-files", params={"unclaimed": True}).json()["items"][0]["files"][0]["id"]
    assert cs.get(f"/internal/aftersales-files/{uf}/content").status_code == 404
    assert cs.post(f"/internal/aftersales-mails/{un}/claim", json={"order_id": ORDER}).status_code == 404
    assert client.post(f"/internal/aftersales-mails/{un}/claim", json={"order_id": "00-00000-00000"}).status_code == 404
    r = client.post(f"/internal/aftersales-mails/{un}/claim", json={"order_id": ORDER}).json()
    assert (r["store_id"], r["match"], r["conversation_id"]) == ("STORE_A", "manual", client.conv_id)
    assert r["files"][0]["file_name"].startswith(f"{ORDER}_") and r["files"][0]["claimed_by"] == "tester"
    assert not (tmp_path / "files" / "_待认领").exists()  # 空文件夹已清掉
    assert cs.get(f"/internal/aftersales-files/{uf}/content").status_code == 200


def test_cleanup_after_retention(as_env, tmp_path):
    from app.aftersales_mail.service import cleanup
    from app.db import session_scope, utcnow
    from app.models import AftersalesFile, AftersalesMail
    from sqlalchemy import select

    mid = process(mail(files=[("p.jpg", "image/jpeg", b"jpg")]))
    purged = []

    class Box:
        def purge_done(self, before):
            purged.append(before)
            return 3

    with session_scope() as db:
        f = db.scalar(select(AftersalesFile).where(AftersalesFile.mail_id == mid))
        path = tmp_path / "files" / f.rel_path
        assert cleanup(db, mailbox=Box())["files"] == 0  # 还没到期
        f.received_at = utcnow() - timedelta(days=31)
        db.get(AftersalesMail, mid).processed_at = utcnow() - timedelta(days=31)
        r = cleanup(db, mailbox=Box())
        assert (r["files"], r["mail_records"], r["mailbox_deleted"]) == (1, 1, 3)
        assert f.status == "PURGED" and not path.exists() and not path.parent.exists()
        assert db.get(AftersalesMail, mid).sender is None and purged[-1] == (utcnow() - timedelta(days=30)).date()
    assert as_env.get(f"/internal/aftersales-files/{f.id}/content").status_code == 410


def test_run_once_moves_processed_mail(as_env, monkeypatch):
    from app.aftersales_mail import mailbox, service

    monkeypatch.setenv("AFTERSALES_MAIL_ENABLED", "true")
    monkeypatch.setenv("AFTERSALES_MAIL_PASSWORD", "x")
    config.get_settings.cache_clear()
    moved = []

    class Box:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def fetch_all(self, limit=50):
            return [("7", mail(files=[("p.jpg", "image/jpeg", b"jpg")])), ("8", b"not a mail but parses")]

        def move_done(self, uid):
            moved.append(uid)

    monkeypatch.setattr(mailbox, "ImapMailbox", Box)
    assert service.run_once() == {"processed": 2, "failed": 0}
    assert moved == ["7", "8"]


def test_compliance_allows_own_aftersales_mailbox(as_env):
    from app.guards.compliance import ComplianceChecker

    ok = ComplianceChecker().check("Bitte senden Sie das Video an service@shop.example mit der Bestellnummer im Betreff.")
    assert ok.result == "PASS"
    bad = ComplianceChecker().check("Bitte senden Sie das Video an other@gmail.com")
    assert bad.result == "BLOCK"


def test_virus_scan_result_parsing(as_env, monkeypatch, tmp_path):
    """只有扫描输出明确报告威胁才删文件；Defender 服务没运行、扫描失败都保留文件。"""
    import subprocess
    from types import SimpleNamespace

    from app.aftersales_mail import storage

    monkeypatch.setenv("AFTERSALES_VIRUS_SCAN", "true")
    config.get_settings.cache_clear()
    monkeypatch.setattr(storage, "_defender", lambda: "MpCmdRun.exe")
    root = tmp_path / "files"
    root.mkdir(exist_ok=True)

    def run_with(rc, out):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=rc, stdout=out.encode(), stderr=b""))
        (root / "f.bin").write_bytes(b"x")
        return storage.scan("f.bin"), (root / "f.bin").exists()

    assert run_with(0, "Scanning f.bin found no threats.") == (("CLEAN", None), True)
    assert run_with(2, "CmdTool: Failed with hr = 0x800106ba.")[0][0] == "SKIPPED"
    assert run_with(2, "CmdTool: Failed with hr = 0x800106ba.")[1] is True
    assert run_with(2, "Scan finished. found 1 threats.") == (("THREAT", "病毒扫描发现威胁，文件已删除"), False)
    assert run_with(2, "CmdTool: Failed with hr = 0x80508023.")[0] == ("ERROR", "病毒扫描失败（0x80508023），打开前请注意")
