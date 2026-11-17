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
    assert [c.value for c in wb["知识条款"][1]] == HEADER + ["适用场景", "适用店铺", "参考文档"]
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


def test_export_round_trips_and_cleanup(kb, tmp_path):
    """导出 = 当前知识按模板格式（可直接再上传，内容不变）；清理历史只删旧版本；清空要输入“清空”。"""
    from app.db import session_scope
    from app.models import AuditEvent, KnowledgeChunk
    from sqlalchemy import func, select

    _, r1 = upload(kb, xlsx(GOOD))
    _, r2 = upload(kb, xlsx(GOOD[:3]))  # 第二版：去掉未启用的那条
    _, r3 = upload(kb, xlsx(GOOD))  # 第三版：又加回来

    res = kb.get("/internal/knowledge/export", params={"store_id": "STORE_A"})
    assert res.status_code == 200 and "attachment" in res.headers["content-disposition"]
    ws = load_workbook(io.BytesIO(res.content))["知识条款"]
    got = {r[0]: r for r in ws.iter_rows(min_row=2, values_only=True)}
    assert set(got) == {"RET-1", "SHP-1", "SOP-1", "KB-001"}
    assert got["SHP-1"][2] == "Versand am nächsten Werktag mit DHL." and got["SHP-1"][3] == "EBAY_DE,EBAY_GB"
    assert got["SOP-1"][3:6] == ("全部", "仅内部", "退款") and got["KB-001"][11] == "否"
    p = kb.post("/internal/knowledge/upload/preview", json=body(res.content)).json()
    assert p["ok"] and p["diff"]["unchanged"] == 4 and not p["diff"]["changed"]  # 原样再上传：内容不变

    z = kb.get("/internal/knowledge/export", params={"store_id": "*"})  # 全部店铺：一个 Excel
    assert "spreadsheetml" in z.headers["content-type"]
    head = [c.value for c in load_workbook(io.BytesIO(z.content))["知识条款"][1]]
    col = head.index("适用店铺")
    rows = list(load_workbook(io.BytesIO(z.content))["知识条款"].iter_rows(min_row=2, values_only=True))
    assert len(rows) == 4 and {r[col] for r in rows} == {"全部"}  # 只有 STORE_A 有知识：有知识的店铺都有 = 全部

    pre = kb.get("/internal/knowledge/cleanup/preview", params={"store_id": "STORE_A"}).json()
    assert pre["current_clauses"] == 4 and pre["active_imports"] == 1 and pre["history_imports"] == 2 and pre["history_rows"] > 0
    uploads = list((tmp_path / "uploads").iterdir())
    assert len(uploads) == 3

    out = kb.post("/internal/knowledge/cleanup", json={"store_id": "STORE_A", "mode": "history"}).json()
    assert out["deleted_imports"] == 2 and out["deleted_files"] == 2 and out["deleted_rows"] == pre["history_rows"]
    assert applicable("STORE_A", "EBAY_DE") == ["RET-1", "SHP-1"]  # 当前知识不受影响
    imports = kb.get("/internal/knowledge/imports", params={"store_id": "STORE_A"}).json()
    assert [(i["id"], i["status"]) for i in imports] == [(r3["import_id"], "ACTIVE")]

    assert kb.post("/internal/knowledge/cleanup", json={"store_id": "STORE_A", "mode": "all"}).status_code == 422
    out = kb.post("/internal/knowledge/cleanup", json={"store_id": "STORE_A", "mode": "all", "confirm": "清空"}).json()
    assert out["deleted_imports"] == 1 and out["deleted_files"] == 1
    assert applicable("STORE_A", "EBAY_DE") == []
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(KnowledgeChunk).where(KnowledgeChunk.store_id == "STORE_A")) == 0
        acts = db.scalars(select(AuditEvent.action).where(AuditEvent.action.in_(("KNOWLEDGE_CLEANED", "KNOWLEDGE_EXPORTED")))).all()
        assert acts.count("KNOWLEDGE_CLEANED") == 2 and acts.count("KNOWLEDGE_EXPORTED") == 2


def test_export_and_cleanup_admin_only(kb):
    from fastapi.testclient import TestClient

    from conftest import login, make_user

    make_user("cs_kb", "CS", stores=["STORE_A"])
    cs = TestClient(kb.app)
    login(cs, "cs_kb")
    assert cs.get("/internal/knowledge/export", params={"store_id": "STORE_A"}).status_code == 403
    assert cs.post("/internal/knowledge/cleanup", json={"store_id": "STORE_A", "mode": "history"}).status_code == 403


def test_all_stores_export_merges_and_upload_routes_by_store(kb):
    """全部店铺：导出合并相同条款（适用店铺 = 全部 / 店铺名）；上传按“适用店铺”分给各店铺，各自一个版本；
    同一编号可多行但店铺不能重叠；不认识的店铺报错；上传到单个店铺时跳过不适用的行。"""
    from app.db import session_scope
    from app.models import Store

    with session_scope() as db:
        name_b = db.get(Store, "STORE_B").name
    head = HEADER + ["适用店铺"]
    common = row("RET-1", "退货期限", "30 天内可退。", site="全部") + ["全部"]
    only_a = row("PRF-A", "店铺概况 A", "A 店。", site="全部") + ["STORE_A"]
    b_ver = row("RET-1", "退货期限（B 店）", "60 天内可退。", site="全部") + [name_b]
    # RET-1 通用版和 B 店版重叠 → 报错
    bad = kb.post("/internal/knowledge/upload/preview", json={**body(xlsx([common, b_ver], header=head)), "store_id": "*"}).json()
    assert not bad["ok"] and any("适用店铺不能重叠" in e["message"] for e in bad["errors"])
    unknown = kb.post("/internal/knowledge/upload/preview",
                      json={**body(xlsx([row("X-1", "t", "x", site="全部") + ["没有这个店"]], header=head)), "store_id": "*"}).json()
    assert any("找不到店铺" in e["message"] for e in unknown["errors"])

    a_ver = row("RET-1", "退货期限", "30 天内可退。", site="全部") + ["STORE_A"]
    data = xlsx([a_ver, b_ver, only_a], header=head)
    p = kb.post("/internal/knowledge/upload/preview", json={**body(data), "store_id": "*"}).json()
    assert p["ok"], p["errors"]
    assert {x["store_id"]: x["rows"] for x in p["stores"]} == {"STORE_A": 2, "STORE_B": 1}
    r = kb.post("/internal/knowledge/upload/commit", json={**body(data, p["file_sha256"]), "store_id": "*"}).json()
    assert r["stores"] == 2 and len(r["import_ids"]) == 2
    assert applicable("STORE_A", "EBAY_US") == ["PRF-A", "RET-1"]
    assert applicable("STORE_B", "EBAY_GB") == ["RET-1"]

    # 导出全部店铺：B 店的 RET-1 和 A 店的不同，各一行，写店铺名
    res = kb.get("/internal/knowledge/export", params={"store_id": "*"})
    ws = load_workbook(io.BytesIO(res.content))["知识条款"]
    col = [c.value for c in ws[1]].index("适用店铺")
    got = sorted((r[0], r[col]) for r in ws.iter_rows(min_row=2, values_only=True))
    a_name = next(x["name"] for x in p["stores"] if x["store_id"] == "STORE_A")
    assert got == [("PRF-A", a_name), ("RET-1", a_name), ("RET-1", name_b)]
    # 原样再上传到全部店铺：每个店铺都不变
    p2 = kb.post("/internal/knowledge/upload/preview", json={**body(res.content), "store_id": "*"}).json()
    assert p2["ok"] and all(not (x["added"] or x["changed"] or x["removed"]) for x in p2["stores"])
    # 上传到单个店铺：不适用的行跳过并提醒
    p3 = kb.post("/internal/knowledge/upload/preview", json=body(res.content)).json()
    assert p3["counts"]["rows"] == 2 and any("不适用于这个店铺" in w["message"] for w in p3["warnings"])
