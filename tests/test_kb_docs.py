"""参考文档库：上传、编号、类型与大小、安全文件名、权限（负责店铺、敏感）、替换版本、删除拦截、
知识库 Excel「参考文档」列、内部参考返回 docs、AI 不读文档、清空知识库不删文档。"""
import base64
import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from conftest import login, make_user
from test_knowledge_upload import HEADER, body, row, upload, xlsx

HEAD = HEADER + ["适用场景", "适用店铺", "参考文档"]


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def add_doc(client, title="物流手册", name="manual.pdf", data=b"%PDF-1.4 manual", **meta):
    r = client.post("/internal/kb-docs", json={"title": title, "file_name": name, "data_base64": b64(data), **meta})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture()
def kb(client, monkeypatch, tmp_path):
    from app import config
    from app.knowledge import excel_import

    monkeypatch.setattr(excel_import, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setenv("EBAY_STORE_ID", "STORE_A")
    config.get_settings.cache_clear()
    return client


def cs_client(kb, name="cs_docs", stores=("STORE_A",)):
    make_user(name, "CS", stores=list(stores))
    c = TestClient(kb.app)
    login(c, name)
    return c


def test_upload_numbering_type_size_and_safe_name(kb, monkeypatch):
    from app import config

    d1 = add_doc(kb)
    d2 = add_doc(kb, title="供应商资料", name="../../evil/路径\\供应商.xlsx", data=b"PK\x03\x04 xlsx", category="供应商资料")
    assert (d1["doc_no"], d2["doc_no"]) == ("DOC-0001", "DOC-0002")
    assert d2["file_name"] == "供应商.xlsx" and ".." not in d2["file_name"]
    assert config.get_settings().kb_docs_root in [p.parent.parent for p in config.get_settings().kb_docs_root.rglob("*.xlsx")]
    bad = kb.post("/internal/kb-docs", json={"title": "x", "file_name": "a.zip", "data_base64": b64(b"PK")})
    assert bad.status_code == 422 and "不支持的文件类型" in bad.text
    assert kb.post("/internal/kb-docs", json={"title": "x", "file_name": "a.exe", "data_base64": b64(b"MZ")}).status_code == 422
    monkeypatch.setenv("KB_DOCS_MAX_MB", "1")
    config.get_settings.cache_clear()
    big = kb.post("/internal/kb-docs", json={"title": "x", "file_name": "a.txt", "data_base64": b64(b"x" * (1024 * 1024 + 1))})
    assert big.status_code == 422 and "超过 1 MB" in big.text
    # 删掉的编号不复用
    assert kb.delete(f"/internal/kb-docs/{d2['id']}").status_code == 200
    assert add_doc(kb, title="新文档")["doc_no"] == "DOC-0003"
    items = kb.get("/internal/kb-docs").json()["items"]
    assert [x["doc_no"] for x in items] == ["DOC-0001", "DOC-0003"]
    res = kb.get(f"/internal/kb-docs/{d1['id']}/file")
    assert res.status_code == 200 and res.content == b"%PDF-1.4 manual" and res.headers["content-disposition"].startswith("inline")


def test_permissions_store_scope_and_sensitive(kb):
    a = add_doc(kb, title="A 店资料", store_ids=["STORE_A"])
    b = add_doc(kb, title="B 店资料", store_ids=["STORE_B"])
    s = add_doc(kb, title="含买家信息的报表", name="report.xlsx", data=b"PK xlsx", sensitive=True)
    cs = cs_client(kb)
    # 非管理员不能上传、改、删
    assert cs.post("/internal/kb-docs", json={"title": "x", "file_name": "a.pdf", "data_base64": b64(b"x")}).status_code == 403
    assert cs.put(f"/internal/kb-docs/{a['id']}", json={"title": "改"}).status_code == 403
    assert cs.delete(f"/internal/kb-docs/{a['id']}").status_code == 403
    items = {x["doc_no"]: x for x in cs.get("/internal/kb-docs").json()["items"]}
    assert set(items) == {a["doc_no"], s["doc_no"]}  # 看不到 B 店的
    assert "file_url" not in items[s["doc_no"]] and items[s["doc_no"]]["title"] == "含买家信息的报表"  # 敏感：只有标题
    assert cs.get(f"/internal/kb-docs/{s['id']}/file").status_code == 403
    assert cs.get(f"/internal/kb-docs/{b['id']}/file").status_code == 404  # 越权一律 404
    assert cs.get(f"/internal/kb-docs/{b['id']}/refs").status_code == 404
    assert cs.get(f"/internal/kb-docs/{a['id']}/file").status_code == 200
    assert kb.get(f"/internal/kb-docs/{s['id']}/file").status_code == 200  # 管理员可以


def test_replace_keeps_number_and_history(kb):
    d = add_doc(kb, name="v1.txt", data=b"version one")
    r = kb.post(f"/internal/kb-docs/{d['id']}/replace", json={"file_name": "v2.txt", "data_base64": b64(b"version two")})
    assert r.status_code == 200 and r.json()["doc_no"] == d["doc_no"] and r.json()["version"] == 2
    assert kb.get(f"/internal/kb-docs/{d['id']}/file").content == b"version two"
    assert kb.get(f"/internal/kb-docs/{d['id']}/file", params={"version": 1}).content == b"version one"
    vs = kb.get(f"/internal/kb-docs/{d['id']}/versions").json()
    assert [v["version"] for v in vs] == [1]
    # 归档后列表默认不显示，已有链接照样能打开
    assert kb.post(f"/internal/kb-docs/{d['id']}/archive").status_code == 200
    assert kb.get("/internal/kb-docs").json()["items"] == []
    assert len(kb.get("/internal/kb-docs", params={"status": "ARCHIVED"}).json()["items"]) == 1
    assert kb.get(f"/internal/kb-docs/{d['id']}/file").status_code == 200


def test_download_audit_once_per_10_minutes(kb):
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import AuditEvent

    d = add_doc(kb)
    for _ in range(3):
        kb.get(f"/internal/kb-docs/{d['id']}/file")
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "KB_DOC_DOWNLOADED")) == 1


def test_excel_column_refs_delete_block_internal_refs_and_cleanup(kb):
    from app.config import get_settings

    doc = add_doc(kb, title="意图路由统计", name="route.xlsx", data=b"PK route stats SECRET-STAT-123")
    arch = add_doc(kb, title="旧手册")
    kb.post(f"/internal/kb-docs/{arch['id']}/archive")
    sens = add_doc(kb, title="A 店敏感报表", sensitive=True, store_ids=["STORE_A"])

    def r(no, title, text, refs="", audience="仅内部", scenes="常驻", stores="全部"):
        return row(no, title, text, site="全部", audience=audience) + [scenes, stores, refs]

    # 校验：不存在 → 错误；已归档、对外条款、敏感文档店铺超范围、正文提到但没填 → 提醒
    bad = xlsx([r("X-1", "t", "x", refs="DOC-0999")], header=HEAD)
    p = kb.post("/internal/knowledge/upload/preview", json=body(bad)).json()
    assert not p["ok"] and any("DOC-0999 不存在" in e["message"] for e in p["errors"])
    rows = [r("ESC-1", "分流说明", "统计详表见 DOC-0001。", refs=doc["doc_no"]),
            r("ESC-2", "旧做法", "见旧手册", refs=arch["doc_no"]),
            r("PUB-1", "退货期限", "30 天内可退。", refs=doc["doc_no"], audience="可以", scenes=""),
            r("SEN-1", "敏感", "见报表", refs=sens["doc_no"]),
            r("MEN-1", "只在正文提到", "详见 DOC-0001")]
    data = xlsx(rows, header=HEAD)
    p = kb.post("/internal/knowledge/upload/preview", json=body(data)).json()
    assert p["ok"], p["errors"]
    msgs = " | ".join(w["message"] for w in p["warnings"])
    assert "已归档" in msgs and "AI 不会读文档" in msgs and "超出了它的范围" in msgs and "请在「参考文档」列填写" in msgs
    upload(kb, data)

    # 知识库列表带 docs；导出后原样再上传全部“不变”
    items = {i["clause_id"]: i for i in kb.get("/internal/knowledge", params={"store_id": "STORE_A", "marketplace_id": "EBAY_US"}).json()["items"]}
    assert items["ESC-1"]["docs"][0]["doc_no"] == doc["doc_no"] and items["ESC-1"]["docs"][0]["file_url"]
    res = kb.get("/internal/knowledge/export", params={"store_id": "STORE_A"})
    ws = load_workbook(io.BytesIO(res.content))["知识条款"]
    head = [c.value for c in ws[1]]
    assert head[-1] == "参考文档"
    got = {x[0]: x[head.index("参考文档")] for x in ws.iter_rows(min_row=2, values_only=True)}
    assert got["ESC-1"] == doc["doc_no"] and not got["MEN-1"]
    p2 = kb.post("/internal/knowledge/upload/preview", json=body(res.content)).json()
    assert p2["ok"] and not p2["diff"]["changed"] and p2["diff"]["unchanged"] == len(rows)

    # 被当前条款引用的文档不能删，并列出引用
    blocked = kb.delete(f"/internal/kb-docs/{doc['id']}")
    assert blocked.status_code == 409 and "ESC-1" in blocked.text and "PUB-1" in blocked.text
    refs = kb.get(f"/internal/kb-docs/{doc['id']}/refs").json()
    assert {x["clause_id"] for x in refs} == {"ESC-1", "PUB-1"}

    # 会话页内部参考：每条带 docs；敏感文档对客服没有链接
    from test_pipeline import send_buyer

    cid = send_buyer(kb, "Where is my parcel?")["conversation_id"]
    cs = cs_client(kb)
    pinned = {i["clause_id"]: i for i in cs.get(f"/internal/conversations/{cid}/internal-refs").json()["pinned"]}
    assert pinned["ESC-1"]["docs"][0]["file_url"]
    sen = pinned["SEN-1"]["docs"][0]
    assert sen["title"] == "A 店敏感报表" and sen["file_url"] is None

    # 清空全部知识：文档还在（文件也在）
    out = kb.post("/internal/knowledge/cleanup", json={"store_id": "STORE_A", "mode": "all", "confirm": "清空"})
    assert out.status_code == 200
    assert len(kb.get("/internal/kb-docs", params={"status": "ALL"}).json()["items"]) == 3
    assert any(get_settings().kb_docs_root.rglob("*route.xlsx"))
    # 没有引用了就可以删，文件一起删
    assert kb.delete(f"/internal/kb-docs/{doc['id']}").json()["deleted_files"] == 1
    assert not any(get_settings().kb_docs_root.rglob("*route.xlsx"))


def test_ai_never_reads_document_content(kb):
    """生成草稿时，提示词里没有文档内容（文档只给人看）。"""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from app.config import get_settings
    from app.providers.chat import ChatModelProvider, set_chat_provider_override
    from app.worker import worker
    from test_pipeline import draft_json, send_buyer

    doc = add_doc(kb, title="供应商退货手册", name="supplier.txt", data=b"SUPPLIER-SECRET-CONTENT refund 80%")
    upload(kb, xlsx([row("RET-1", "退货期限", "Returns within 30 days. 详见 DOC-0001。", site="全部")
                     + ["", "全部", doc["doc_no"]]], header=HEAD))
    seen: list[str] = []

    class Capture(GenericFakeChatModel):
        def _generate(self, messages, *args, **kwargs):
            seen.append("\n".join(str(m.content) for m in messages))
            return super()._generate(messages, *args, **kwargs)

    model = Capture(messages=iter([AIMessage(content=draft_json())] * 5))
    set_chat_provider_override(ChatModelProvider(get_settings(), model=model))
    send_buyer(kb, "What is your return policy?")
    worker.run_once()
    assert seen, "模型没有被调用"
    assert all("SUPPLIER-SECRET-CONTENT" not in s and "供应商退货手册" not in s for s in seen)
