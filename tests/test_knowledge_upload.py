"""正式知识库 Excel 上传：模板、校验、预览、整体替换、回滚。全程不调用模型。"""
import base64
import io
from datetime import date

import pytest
from openpyxl import Workbook, load_workbook

HEADER = ["编号", "标题", "正文", "适用站点", "能否对买家说", "主题", "优先级", "语言", "生效日期", "失效日期",
          "相关SKU", "启用", "负责人/来源"]


def xlsx(rows, sheet="知识条款", header=HEADER) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def row(no, title, text, site="EBAY_DE", audience="可以", topic="退货", prio=20, eff="2026-01-01", exp=None, on="是"):
    return [no, title, text, site, audience, topic, prio, "de", eff, exp, "", on, "客服主管"]


GOOD = [
    row("RET-1", "退货期限", "Rücksendung innerhalb von 30 Tagen über eBay."),
    row("SHP-1", "发货时效", "Versand am nächsten Werktag mit DHL.", site="EBAY_DE,EBAY_GB", topic="发货"),
    row("SOP-1", "部分退款上限", "最多退 30%，需主管批准。", site="全部", audience="仅内部", topic="退款"),
    row("", "暂不启用的条款", "还在讨论。", on="否"),
]


@pytest.fixture()
def kb(client, monkeypatch, tmp_path):
    from app.knowledge import excel_import

    monkeypatch.setattr(excel_import, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setenv("EBAY_STORE_ID", "STORE_A")  # 演示店铺当作真实店铺，开通美德英三站
    from app import config

    config.get_settings.cache_clear()
    return client


def body(data: bytes, sha=None, name="kb.xlsx"):
    b = {"store_id": "STORE_A", "file_name": name, "data_base64": base64.b64encode(data).decode()}
    if sha:
        b["expected_sha256"] = sha
    return b


def upload(client, data: bytes):
    p = client.post("/internal/knowledge/upload/preview", json=body(data)).json()
    r = client.post("/internal/knowledge/upload/commit", json=body(data, p["file_sha256"]))
    assert r.status_code == 200, r.text
    return p, r.json()


def applicable(store, site):
    from app.db import session_scope
    from app.knowledge.retriever import load_full_context

    with session_scope() as db:
        return sorted(c.clause_id for c in load_full_context(db, store, site, date(2026, 10, 26), 10**9).clauses)


def test_template_downloads_and_round_trips(kb):
    r = kb.get("/internal/knowledge/template", params={"store_id": "STORE_A"})
    assert r.status_code == 200
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["知识条款", "填写说明"]
    assert [c.value for c in wb["知识条款"][1]] == HEADER
    # 模板原样上传：只有示例行，被跳过后报“没有可导入的条款”
    p = kb.post("/internal/knowledge/upload/preview", json=body(r.content)).json()
    assert not p["ok"] and "没有可导入" in p["errors"][0]["message"]


def test_preview_reports_errors_without_writing(kb):
    bad = xlsx([
        row("A", "", "正文"),  # 标题为空
        row("A", "重复编号", "正文"),  # 编号重复
        row("B", "站点不对", "正文", site="EBAY_FR"),
        row("C", "可见级别不对", "正文", audience="对外"),
        row("D", "日期反了", "正文", eff="2026-05-01", exp="2026-04-01"),
        row("E", "联系方式", "请发邮件到 help@example.com"),
    ])
    p = kb.post("/internal/knowledge/upload/preview", json=body(bad)).json()
    msgs = " | ".join(f"{e['row']}:{e['column']}:{e['message']}" for e in p["errors"])
    for expect in ("2:标题", "3:编号", "4:适用站点", "5:能否对买家说", "6:失效日期"):
        assert expect in msgs, msgs
    assert any("help@example.com" in w["message"] for w in p["warnings"])
    assert kb.post("/internal/knowledge/upload/commit", json=body(bad, p["file_sha256"])).status_code == 422
    assert applicable("STORE_A", "EBAY_DE") == []


def test_missing_sheet_or_column(kb):
    assert kb.post("/internal/knowledge/upload/preview", json=body(xlsx([], sheet="Sheet1"))).status_code == 422
    r = kb.post("/internal/knowledge/upload/preview", json=body(xlsx([], header=["标题", "正文"])))
    assert r.status_code == 422 and "适用站点" in r.json()["detail"]["message"]
    assert kb.post("/internal/knowledge/upload/preview", json=body(b"not excel")).status_code == 422


def test_commit_requires_matching_preview(kb):
    data = xlsx(GOOD)
    assert kb.post("/internal/knowledge/upload/commit", json=body(data, "0" * 64)).status_code == 409


def test_upload_replace_and_rollback(kb):
    p, r1 = upload(kb, xlsx(GOOD))
    assert p["counts"] == {"rows": 4, "public": 3, "internal": 1, "disabled": 1}
    assert p["diff"]["added"] == ["KB-001", "RET-1", "SHP-1", "SOP-1"]
    assert applicable("STORE_A", "EBAY_DE") == ["RET-1", "SHP-1"]  # 内部 SOP、未启用不注入
    assert applicable("STORE_A", "EBAY_GB") == ["SHP-1"]  # 多站点条款按站点展开
    assert applicable("STORE_A", "EBAY_US") == []

    # 第二版：删掉发货、改退货期限 → 整体替换
    v2 = [row("RET-1", "退货期限", "Rücksendung innerhalb von 60 Tagen."), GOOD[2]]
    p2, r2 = upload(kb, xlsx(v2))
    assert p2["diff"]["changed"] == ["RET-1"] and "SHP-1" in p2["diff"]["removed"]
    assert applicable("STORE_A", "EBAY_DE") == ["RET-1"]
    assert applicable("STORE_A", "EBAY_GB") == []

    imports = kb.get("/internal/knowledge/imports", params={"store_id": "STORE_A"}).json()
    assert [(i["id"], i["status"]) for i in imports] == [(r2["import_id"], "ACTIVE"), (r1["import_id"], "SUPERSEDED")]

    # 回滚到第一版：发货条款回来，未启用的仍未启用
    assert kb.post(f"/internal/knowledge/imports/{r1['import_id']}/rollback", json={}).status_code == 200
    assert applicable("STORE_A", "EBAY_DE") == ["RET-1", "SHP-1"]
    statuses = {i["id"]: i["status"] for i in kb.get("/internal/knowledge/imports", params={"store_id": "STORE_A"}).json()}
    assert statuses == {r1["import_id"]: "ACTIVE", r2["import_id"]: "ROLLED_BACK"}
    listed = kb.get("/internal/knowledge", params={"store_id": "STORE_A", "marketplace_id": "EBAY_DE"}).json()["items"]
    assert {i["clause_id"] for i in listed} == {"RET-1", "SHP-1", "SOP-1", "KB-001"}  # 下线的旧版本默认不列出


def test_upload_does_not_touch_other_stores(kb):
    upload(kb, xlsx(GOOD))
    assert applicable("STORE_B", "EBAY_GB") == []
