"""按需识图（C10）：VISION_ENABLED 关、VISION_ON_DEMAND 开时，只在来信说破损 / 发错 / 缺件时识别，最多 1 张，先缩小。假视觉模型。"""
import io

from PIL import Image

from test_media import jpeg_bytes, observation, send_with_images, use_fake_vision
from test_pipeline import detail, draft_json, use_fake_llm


def test_on_demand_only_damage_one_image_downscaled(client, monkeypatch):
    from app import config
    from app.providers.vision import VisionModelProvider
    from app.worker import worker

    monkeypatch.setenv("VISION_ENABLED", "false")
    monkeypatch.setenv("VISION_ON_DEMAND", "true")
    monkeypatch.setenv("VISION_ON_DEMAND_MAX_PX", "800")
    config.get_settings.cache_clear()
    seen = []
    orig = VisionModelProvider.observe

    def spy(self, image, mime, context=""):
        seen.append(Image.open(io.BytesIO(image)).size)
        return orig(self, image, mime, context)
    monkeypatch.setattr(VisionModelProvider, "observe", spy)
    use_fake_vision(observation(damage_observed=True, damage_description="外壳开裂"))
    use_fake_llm(draft_json(claims=[], reply_text="Thanks, a colleague will check."),
                 draft_json(claims=[], reply_text="Sorry about that, a colleague will check the photos."))

    a = send_with_images(client, "Where is my parcel? Photo of the label attached.", [jpeg_bytes()])
    worker.run_once()
    att = detail(client, a["conversation_id"])["messages"][-1]["attachments"][0]
    assert not seen and att["observation"] is None  # 不是破损 / 发错 / 缺件：不识别

    b = send_with_images(client, "The part arrived broken, see photos", [jpeg_bytes(), jpeg_bytes(color=(10, 200, 10))],
                         buyer="buyer_us_003", item="110000000003")
    worker.run_once()
    d = detail(client, b["conversation_id"])
    atts = d["messages"][-1]["attachments"]
    assert len(seen) == 1 and max(seen[0]) <= 800  # 只识别 1 张，缩到 800 像素以内
    assert atts[0]["observation"]["observation"]["damage_observed"] is True and atts[1]["observation"] is None
    step = next(s for s in d["steps"] if s["node"] == "observe_attachments")
    assert step["detail"]["mode"] == "on_demand" and step["detail"]["vision_calls"] == 1
    assert any("只识别 1 张" in r for r in step["detail"]["not_read"])
    assert d["drafts"][0]["risk_level"] == "L2" and "IMAGE_DAMAGE_OBSERVED" in d["drafts"][0]["review_reasons"]
    config.get_settings.cache_clear()


def test_on_demand_off_by_default(app_env):
    from app.config import get_settings

    s = get_settings()
    assert s.vision_on_demand is False and "VISION_ENABLED 未开启" in "；".join(s.vision_not_ready_reasons())


def test_downscale_helper():
    from app.agent.nodes import downscale

    big = jpeg_bytes(size=(3000, 2000), orientation=None)
    out, ctype = downscale(big, "image/jpeg", 1024)
    assert ctype == "image/jpeg" and max(Image.open(io.BytesIO(out)).size) == 1024
    small = jpeg_bytes(size=(600, 400), orientation=None)
    assert downscale(small, "image/jpeg", 1024) == (small, "image/jpeg")
    assert downscale(b"not an image", "image/png", 1024) == (b"not an image", "image/png")
