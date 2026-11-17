"""仅内部条款的“适用场景”与会话页“内部参考”；新增的两条合规检查。全程不调用模型。"""
import pytest

from test_knowledge_upload import HEADER, body, kb, upload, xlsx  # noqa: F401  kb 是夹具
from test_pipeline import send_buyer

HEADER_S = HEADER + ["适用场景"]


def set_intent(cid, intent):
    """给会话造一条 AI 草稿（只为设置识别出的意图）。"""
    from app.db import session_scope
    from app.models import Conversation, Draft, ReplyJob

    with session_scope() as db:
        conv = db.get(Conversation, cid)
        n = db.query(ReplyJob).filter(ReplyJob.conversation_id == cid).count()
        job = ReplyJob(store_id=conv.store_id, conversation_id=cid, anchor_source_message_id=f"anchor-{n}")
        db.add(job)
        db.flush()
        db.add(Draft(job_id=job.id, run_generation=1, conversation_row_version=conv.row_version, intent=intent, intents=[]))


def r(no, title, text, audience="仅内部", scenes="", source="售后主管", site="全部", on="是"):
    return [no, title, text, site, audience, "售后", 20, "zh", "2026-01-01", None, "", on, source, scenes]


ROWS = [
    r("SOP-Q", "质量问题先问清", "问安装时间、故障码。", scenes="质量问题"),
    r("SOP-R", "部分退款协商", "比例由主管定。", scenes="退货退款,质量问题"),
    r("SOP-L", "物流丢件判断", "核对签收地址。", scenes="物流异常"),
    r("PRF", "店铺概况", "德国站为主。", scenes="常驻"),
    r("SOP-N", "没挂场景的说明", "只在知识库页看。"),
    r("SOP-OFF", "未启用", "草稿。", scenes="质量问题", on="否"),
    r("PUB", "退货流程", "请在 eBay 订单页申请退货。", audience="可以"),
]


def test_scene_and_source_validation(kb):
    bad = [r("A", "未知场景", "x", scenes="质量问题,催单"), r("B", "来源太长", "x", source="来" * 65),
           r("C", "对外条款挂场景", "x", audience="可以", scenes="发票")]
    p = kb.post("/internal/knowledge/upload/preview", json=body(xlsx(bad, header=HEADER_S))).json()
    errs = {(e["row"], e["column"]) for e in p["errors"]}
    assert errs == {(2, "适用场景"), (3, "负责人/来源")}
    assert any(w["row"] == 4 and w["column"] == "适用场景" for w in p["warnings"])


def test_template_has_scene_column(kb):
    import io

    from openpyxl import load_workbook

    data = kb.get("/internal/knowledge/template", params={"store_id": "STORE_A"}).content
    assert "适用场景" in [c.value for c in load_workbook(io.BytesIO(data))["知识条款"][1]]


def test_internal_refs_follow_conversation_scenes(kb):
    from app.db import session_scope
    from app.models import Conversation

    upload(kb, xlsx(ROWS, header=HEADER_S))
    cid = send_buyer(kb, "The pump stopped working after two weeks.")["conversation_id"]
    with session_scope() as db:
        conv = db.get(Conversation, cid)
        conv.aftersales_type, conv.situation_stage = None, None
    url = f"/internal/conversations/{cid}/internal-refs"

    # 还没有场景：只有常驻条款
    set_intent(cid, "other")
    res = kb.get(url).json()
    assert res["items"] == [] and [x["clause_id"] for x in res["pinned"]] == ["PRF"]

    # 客服标了“质量问题”：挂了质量问题的条款出来，未启用、没挂场景、对外条款都不出来
    with session_scope() as db:
        db.get(Conversation, cid).aftersales_type = "质量问题"
    res = kb.get(url).json()
    assert [s["scene"] for s in res["scenes"]] == ["质量问题"]
    assert [x["clause_id"] for x in res["items"]] == ["SOP-Q", "SOP-R"]
    assert res["items"][1]["scenes"] == ["质量问题"] and res["items"][0]["text"] == "问安装时间、故障码。"

    # AI 识别的意图也算：退款进度 → 退货退款（取最新一条 AI 草稿）
    with session_scope() as db:
        db.get(Conversation, cid).aftersales_type = None
    set_intent(cid, "refund_status")
    assert [x["clause_id"] for x in kb.get(url).json()["items"]] == ["SOP-R"]

    # 会话阶段只在没有其他线索时兜底
    set_intent(cid, "other")
    with session_scope() as db:
        db.get(Conversation, cid).situation_stage = "shipping"
    assert [x["clause_id"] for x in kb.get(url).json()["items"]] == ["SOP-L"]

    listed = kb.get("/internal/knowledge", params={"store_id": "STORE_A", "marketplace_id": "EBAY_DE"}).json()["items"]
    assert {i["clause_id"]: i["scenes"] for i in listed}["SOP-R"] == ["退货退款", "质量问题"]


def test_internal_refs_respect_store_scope(kb):
    from conftest import login, make_user
    from fastapi.testclient import TestClient

    from app.server import create_app

    upload(kb, xlsx(ROWS, header=HEADER_S))
    cid = send_buyer(kb, "Hello")["conversation_id"]
    make_user("cs_other", "CS", stores=["STORE_B"])
    other = TestClient(create_app())
    login(other, "cs_other")
    assert other.get(f"/internal/conversations/{cid}/internal-refs").status_code == 404


@pytest.fixture()
def checker(app_env):
    from app.guards.compliance import ComplianceChecker

    return ComplianceChecker()


@pytest.mark.parametrize("text", [
    "Please cancel your return request, as the reason you selected would cause significant harm to our shop.",
    "Please close the return request first, then we will provide you with a return label.",
    "Please choose the reason for returning as: Changed mind.",
    "Würden Sie bitte die Rücksendeanfrage stornieren? Wir können Ihnen dann ein Rücksendeetikett zusenden.",
    "Bitte ändern Sie den Rückgabegrund auf „Gefällt nicht mehr“.",
])
def test_return_reason_steering_blocked(checker, text):
    res = checker.check(text)
    assert res.result == "BLOCK" and any(h.rule == "return_reason_steering" for h in res.hits)


@pytest.mark.parametrize("text", [
    "Could you keep the item? We can refund you 15% as compensation.",
    "Wir erstatten Ihnen 20 % als Entschädigung, wenn Sie den Artikel behalten.",
    "We can offer you a partial refund.",
])
def test_refund_offer_needs_review(checker, text):
    res = checker.check(text)
    assert res.result == "REVIEW" and {h.rule for h in res.hits} == {"refund_offer"}


@pytest.mark.parametrize("text", [
    "Please open a return request in your eBay order; the return label will be provided there.",
    "You can cancel the return request in My eBay if you would like to keep the item.",
    "Your refund has been issued to the original payment method.",
])
def test_normal_return_wording_passes(checker, text):
    assert checker.check(text).result == "PASS"


def test_scene_maps_stay_in_sync():
    """售后类型、工单类别、意图清单改了，场景映射要跟着改。"""
    from app.feishu.cases import CATEGORIES
    from app.intents import INTENTS
    from app.knowledge import scenes as sc
    from app.models import Aftersales

    assert set(sc.AFTERSALES_SCENES) <= set(Aftersales.TYPES)
    assert set(Aftersales.TYPES) - set(sc.AFTERSALES_SCENES) == {"其他"}
    assert set(sc.CASE_SCENES) >= set(CATEGORIES.values())
    assert set(sc.INTENT_SCENES) <= set(INTENTS)
    for m in (sc.AFTERSALES_SCENES, sc.CASE_SCENES, sc.INTENT_SCENES, sc.STAGE_SCENES):
        assert all(s in sc.SCENES for v in m.values() for s in v)


def test_export_keeps_scenes(kb):
    """导出带「适用场景」列，原样再上传内容不变。"""
    import io

    from openpyxl import load_workbook

    upload(kb, xlsx(ROWS, header=HEADER_S))
    res = kb.get("/internal/knowledge/export", params={"store_id": "STORE_A"})
    ws = load_workbook(io.BytesIO(res.content))["知识条款"]
    head = [c.value for c in ws[1]]
    got = {r[0]: dict(zip(head, r)) for r in ws.iter_rows(min_row=2, values_only=True)}
    assert got["SOP-R"]["适用场景"] == "退货退款,质量问题" and got["PRF"]["适用场景"] == "常驻"
    p = kb.post("/internal/knowledge/upload/preview", json=body(res.content)).json()
    assert p["ok"] and not p["diff"]["changed"] and p["diff"]["unchanged"] == len(ROWS)
