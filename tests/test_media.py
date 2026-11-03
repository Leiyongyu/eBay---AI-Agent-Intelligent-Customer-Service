"""买家附件（第 20 章）：安全下载、去元数据重新编码、带图强制人工审核、视觉观察与缓存、审核员纠正。

视觉模型用 GenericFakeChatModel 注入，只验证编排、闸门和落库，不代表真实识图效果。
"""
import io
import json

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from PIL import Image

from app.config import get_settings
from app.guards import risk
from app.media import service as media
from app.models import MessageAttachment
from app.providers.vision import VisionModelProvider, scrub, set_vision_provider_override
from app.worker import worker
from test_pipeline import detail, draft_json, send_buyer, use_fake_llm


def jpeg_bytes(size=(3000, 2000), orientation: int | None = 6, color=(200, 30, 30)) -> bytes:
    img = Image.new("RGB", size, color)
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"  # Make
    if orientation:
        exif[0x0112] = orientation
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif.tobytes())
    return out.getvalue()


def send_with_images(client, text, images, buyer="buyer_us_001", item="110000000001"):
    return send_buyer(client, text, buyer=buyer, item=item, images=[(f"p{i}.jpg", d) for i, d in enumerate(images, 1)])


def use_fake_vision(*observations: dict):
    model = GenericFakeChatModel(messages=iter([AIMessage(content=json.dumps(o, ensure_ascii=False))
                                                for o in observations]))
    set_vision_provider_override(VisionModelProvider(get_settings(), model=model))


def observation(**overrides) -> dict:
    base = {"image_kind": "故障码或仪表屏幕", "quality": "清晰", "description": "诊断仪屏幕显示故障码",
            "visible_text": ["C1234 Wheel speed sensor"], "part_numbers": [], "error_codes": ["C1234"],
            "vin_masked": "", "damage_observed": False, "damage_description": "", "uncertain": [],
            "contains_personal_info": False}
    base.update(overrides)
    return base


# ---------------------------------------------------------------- 安全处理
def test_sanitize_strips_metadata_rotates_and_downscales(app_env):
    clean, mime, w, h = media.sanitize_image(jpeg_bytes())
    assert mime == "image/jpeg"
    assert max(w, h) <= get_settings().media_max_dimension
    assert h > w  # EXIF 方向 6 已摆正（横图转竖图）
    with Image.open(io.BytesIO(clean)) as img:
        assert not img.getexif()  # 原图 EXIF（相机型号、方向等）全部丢弃


def test_sanitize_rejects_non_image_and_oversize(app_env, monkeypatch):
    with pytest.raises(media.MediaError):
        media.sanitize_image(b"%PDF-1.7 not an image")
    monkeypatch.setattr(get_settings(), "media_max_bytes", 1000)
    with pytest.raises(media.MediaError, match="上限"):
        media.sanitize_image(jpeg_bytes((200, 200)))


def test_download_only_allowed_https_hosts(app_env):
    s = get_settings()
    assert media.host_allowed("https://i.ebayimg.com/images/g/abc/s-l1600.jpg", s)
    assert not media.host_allowed("http://i.ebayimg.com/x.jpg", s)  # 必须 HTTPS
    assert not media.host_allowed("https://ebayimg.com.evil.example/x.jpg", s)
    assert not media.host_allowed("https://evil.example/ebayimg.com.jpg", s)

    def handler(request: httpx.Request):
        if request.url.host == "i.ebayimg.com":
            return httpx.Response(302, headers={"location": "https://evil.example/steal.jpg"})
        return httpx.Response(200, content=b"x")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    with pytest.raises(media.MediaError, match="白名单"):  # 重定向到白名单外也拒绝
        media.download("https://i.ebayimg.com/a.jpg", client, s)

    big = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"0" * (s.media_max_bytes + 1))))
    with pytest.raises(media.MediaError, match="上限"):
        media.download("https://i.ebayimg.com/a.jpg", big, s)


def test_scrub_masks_vin_email_phone():
    out = scrub({"visible_text": ["VIN WBA8A12010A676631", "mail a@b.com", "+44 7700 900123"]})
    assert out["visible_text"] == ["VIN WBA8A12010A******", "mail [邮箱]", "[电话]"]


def test_risk_gate_attachments():
    base = dict(intents=["shipping_status"], buyer_text="see photo", marketplace_id="EBAY_US")
    g = risk.evaluate(risk.GateInput(**base, attachments=2, attachments_not_read=1))
    assert g.risk_level == "L1" and {"HAS_ATTACHMENTS", "ATTACHMENT_NOT_READ"} <= set(g.reasons)
    g = risk.evaluate(risk.GateInput(**base, attachments=1, image_damage=True))
    assert g.risk_level == "L2" and "IMAGE_DAMAGE_OBSERVED" in g.reasons and "ATTACHMENT_NOT_READ" not in g.reasons


# ---------------------------------------------------------------- 端到端
def test_image_without_vision_forces_review(client):
    use_fake_llm(draft_json(claims=[], reply_text="Thanks for the photo, a colleague will review it."))
    sent = send_with_images(client, "See the photo, it does not work", [jpeg_bytes()])
    assert sent["attachments"][0]["status"] == "STORED"
    worker.run_once()
    d = detail(client, sent["conversation_id"])
    reasons = d["job"]["decision_reasons"]
    assert "HAS_ATTACHMENTS" in reasons and "ATTACHMENT_NOT_READ" in reasons
    assert d["drafts"][0]["decision"] == "REVIEW_REQUIRED"
    step = next(s for s in d["steps"] if s["node"] == "observe_attachments")
    assert step["detail"]["observed"] == 0 and "VISION_ENABLED" in step["detail"]["not_read"][0]

    att = d["messages"][-1]["attachments"][0]
    assert att["status"] == "STORED" and att["observation"] is None
    r = client.get(att["file_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_image_only_message_and_bad_upload(client):
    sent = send_with_images(client, "", [jpeg_bytes((300, 300))])
    assert sent["job_id"]
    bad = send_with_images(client, "pdf disguised", [b"%PDF-1.7 fake"])
    assert bad["attachments"][0]["status"] == "FAILED"


def test_vision_observation_evidence_damage_and_cache(client):
    use_fake_vision(observation(), observation(damage_observed=True, damage_description="外壳开裂"))
    use_fake_llm(
        draft_json(claims=[{"text": "The photo shows fault code C1234.", "evidence_ids": ["img:1"]}],
                   reply_text="Thanks, we can see fault code C1234 in your photo; a colleague will check it."),
        draft_json(claims=[], reply_text="Thanks, a colleague will review the photos."),
    )
    img = jpeg_bytes()
    first = send_with_images(client, "Error after installation", [img])
    worker.run_once()
    d = detail(client, first["conversation_id"])
    obs = d["messages"][-1]["attachments"][0]["observation"]
    assert obs["status"] == "OK" and obs["observation"]["error_codes"] == ["C1234"] and not obs["cached"]
    draft = d["drafts"][0]
    assert "EVIDENCE_INVALID" not in draft["review_reasons"]  # img:1 是本轮合法证据
    assert "HAS_ATTACHMENTS" in draft["review_reasons"] and "ATTACHMENT_NOT_READ" not in draft["review_reasons"]
    assert draft["risk_level"] == "L1"

    # 同一张图再次出现：复用识别结果，不再调用视觉模型；另一张新图被识别出破损 → L2
    second = send_with_images(client, "Also damaged", [img, jpeg_bytes(color=(10, 200, 10))], buyer="buyer_us_003",
                              item="110000000003")
    worker.run_once()
    d2 = detail(client, second["conversation_id"])
    atts = d2["messages"][-1]["attachments"]
    assert atts[0]["observation"]["cached"] is True
    assert atts[1]["observation"]["observation"]["damage_observed"] is True
    assert d2["drafts"][0]["risk_level"] == "L2"
    assert "IMAGE_DAMAGE_OBSERVED" in d2["drafts"][0]["review_reasons"]

    # 审核员纠正观察结果
    r = client.post(f"/internal/image-observations/{atts[1]['observation']['id']}/review",
                    json={"review_status": "CORRECTED", "note": ""})
    assert r.status_code == 422
    r = client.post(f"/internal/image-observations/{atts[1]['observation']['id']}/review",
                    json={"review_status": "CORRECTED", "note": "只是包装压痕，产品本身没有开裂"})
    assert r.status_code == 200 and r.json()["review_status"] == "CORRECTED" and r.json()["reviewed_by"] == "tester"


def test_ebay_media_registration_and_cleanup(app_env):
    from datetime import timedelta

    from app.db import session_scope, utcnow
    from app.ingestion.service import _append_message, get_or_create_conversation

    with session_scope() as db:
        conv = get_or_create_conversation(db, "STORE_A", "EBAY-CONV-1", "buyer_x", None, "EBAY_US", None)
        msg = _append_message(db, conv, "EBAY-MSG-1", "BUYER", "IN", "see attached", None)
        atts = media.register_ebay_media(db, msg, [
            {"mediaName": "a.jpg", "mediaType": "IMAGE", "mediaUrl": "https://i.ebayimg.com/a.jpg"},
            {"mediaName": "invoice.pdf", "mediaType": "PDF", "mediaUrl": "https://i.ebayimg.com/b.pdf"},
        ])
        assert [a.status for a in atts] == ["PENDING", "SKIPPED"]
        media.store_image(atts[0], jpeg_bytes((100, 100)))
        atts[0].expires_at = utcnow() - timedelta(days=1)
        path = media.file_path(atts[0])
        assert path and path.exists()
        assert media.cleanup_expired(db) == 1
        assert not path.exists() and db.get(MessageAttachment, atts[0].id).status == "EXPIRED"
