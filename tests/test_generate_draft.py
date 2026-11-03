"""手动生成草稿、模拟发送与生成依据、发送消息的中文对照（假模型，不真实调用）。"""

from test_pipeline import detail, draft_json, use_fake_llm


def make_conversation(roles):
    from app.db import session_scope
    from app.ingestion.service import _append_message, get_or_create_conversation

    with session_scope() as db:
        conv = get_or_create_conversation(db, "STORE_A", f"REAL-{len(roles)}-{'-'.join(roles)}", "buyer_us_001",
                                          "110000000001", "EBAY_US", "listing")
        conv.channel_source = "ebay"
        for i, role in enumerate(roles, 1):
            _append_message(db, conv, f"M{conv.id}-{i}", role, "OUT" if role == "SELLER" else "IN",
                            f"{role} message {i}", None)
        return conv.id


def test_generate_draft_for_synced_conversation(client):
    from app.worker import worker

    conv_id = make_conversation(["BUYER", "SELLER", "BUYER", "BUYER"])
    use_fake_llm(draft_json())
    r = client.post(f"/internal/conversations/{conv_id}/generate-draft")
    assert r.status_code == 200 and r.json()["messages"] == 2  # 只覆盖店铺最后回复之后的两条
    assert client.post(f"/internal/conversations/{conv_id}/generate-draft").status_code == 409  # 已有进行中的任务
    worker.run_once()
    d = detail(client, conv_id)
    assert d["job"]["state"] == "REVIEW_REQUIRED" and len(d["drafts"]) == 1
    assert sorted(d["sources"]) == [f"M{conv_id}-3", f"M{conv_id}-4"]

    answered = make_conversation(["BUYER", "SELLER"])
    r = client.post(f"/internal/conversations/{answered}/generate-draft")
    assert r.status_code == 409 and "没有待回复" in r.json()["detail"]


def test_simulated_send_regenerate_and_trace(client):
    from app.worker import worker

    conv_id = make_conversation(["BUYER", "BUYER"])
    use_fake_llm(draft_json(), draft_json(reply_text="Hi, second try.", reasoning_zh="买家问发货时间；物流已有在途记录，直接回答。"))
    assert client.post(f"/internal/conversations/{conv_id}/generate-draft").status_code == 200
    worker.run_once()
    # 待审时再点一次：同一任务代次 +1，旧草稿作废
    r = client.post(f"/internal/conversations/{conv_id}/generate-draft")
    assert r.status_code == 200 and r.json()["run_generation"] == 2
    worker.run_once()
    d = detail(client, conv_id)
    assert d["agent_draft"]["text"] == "Hi, second try." and [x["status"] for x in d["drafts"]] == ["ACTIVE", "SUPERSEDED"]
    assert d["agent_draft"]["reasoning_zh"] == "买家问发货时间；物流已有在途记录，直接回答。"  # 回复思路随草稿保存
    assert d["steps"] and d["steps"][-1]["node"] == "persist_candidate"

    # 合规 BLOCK 不能发
    r = client.post(f"/internal/conversations/{conv_id}/send", json={"text": "Mail me: a@b.com"})
    assert r.status_code == 422 and r.json()["detail"]["result"] == "BLOCK"

    # 改过的 AI 草稿模拟发送：本地追加一条店铺消息，任务结案，能查到当时的生成依据
    r = client.post(f"/internal/conversations/{conv_id}/send", json={"text": "Hi, second try (edited)."})
    assert r.status_code == 200 and r.json()["job_id"] == d["job"]["id"]
    d = detail(client, conv_id)
    last = d["messages"][-1]
    assert last["role"] == "SELLER" and last["simulated"] and last["ebay_message_id"].startswith("SIM-OUT-")
    assert last["trace"] == {"job_id": d["job"]["id"], "generation": 2, "from_ai": True, "auto": False}
    assert d["job"]["state"] == "SENT" and d["drafts"][0]["status"] == "APPROVED"
    trace = client.get(f"/internal/jobs/{d['job']['id']}/trace", params={"generation": 1}).json()
    assert trace["generation"] == 1 and trace["agent_draft"]["text"] == "Hi, your parcel is on its way with USPS " \
        "and recently departed the Phoenix facility."
    assert [s["node"] for s in trace["steps"]][0] == "load_snapshot"
    compose = next(s for s in trace["steps"] if s["node"] == "compose_reply")
    assert compose["detail"]["model"]

    # 没有待回复的买家消息：人工消息照样能模拟发送，但不能再生成草稿
    r = client.post(f"/internal/conversations/{conv_id}/send", json={"text": "Anything else?"})
    assert r.status_code == 200 and r.json()["job_id"] is None
    assert client.post(f"/internal/conversations/{conv_id}/generate-draft").status_code == 409

    # 清除模拟消息后可以对同一买家消息重新测试；真实消息不受影响
    assert client.delete(f"/internal/conversations/{conv_id}/simulated-messages").json() == {"deleted": 2}
    d = detail(client, conv_id)
    assert [m["role"] for m in d["messages"]] == ["BUYER", "BUYER"]
    r = client.post(f"/internal/conversations/{conv_id}/generate-draft")
    assert r.status_code == 200 and r.json()["run_generation"] == 3


def test_sent_message_keeps_chinese_for_reviewers(client):
    from app.worker import worker

    conv_id = make_conversation(["BUYER"])
    use_fake_llm(draft_json())
    client.post(f"/internal/conversations/{conv_id}/generate-draft")
    worker.run_once()
    draft = detail(client, conv_id)["agent_draft"]

    # 原样发出 AI 草稿：用草稿自带的中文，买家收到的仍是英文原文
    r = client.post(f"/internal/conversations/{conv_id}/send", json={"text": draft["text"]})
    assert r.json()["zh_translation"] == draft["text_zh"]
    last = detail(client, conv_id)["messages"][-1]
    assert (last["body"], last["zh_translation"]) == (draft["text"], draft["text_zh"])

    # 工作台带上现成译文就直接用；没有就发出后机翻补上
    r = client.post(f"/internal/conversations/{conv_id}/send", json={"text": "Anything else?", "text_zh": "还有别的吗？"})
    assert r.json()["zh_translation"] == "还有别的吗？"
    use_fake_llm("祝您愉快。")
    r = client.post(f"/internal/conversations/{conv_id}/send", json={"text": "Have a nice day."})
    assert r.json()["zh_translation"] == "祝您愉快。"
    # 模型不可用时照样发出，只是没有中文；中文消息不翻译
    use_fake_llm()
    r = client.post(f"/internal/conversations/{conv_id}/send", json={"text": "Bye."})
    assert r.status_code == 200 and r.json()["zh_translation"] is None
    r = client.post(f"/internal/conversations/{conv_id}/send", json={"text": "好的，谢谢"})
    assert r.status_code == 200 and r.json()["zh_translation"] is None
