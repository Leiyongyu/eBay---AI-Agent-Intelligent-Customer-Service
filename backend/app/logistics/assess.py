"""物流状况判断（确定性规则，不交给模型）：只看“轨迹多少天没有更新”，再加查不到、异常、已签收几种情况。

- 没有跟踪号 / 17TRACK 查不到 → 查不到物流信息，转人工核实；
- 轨迹显示损坏、丢失、退件/已退回、销毁，或运输过久 → 需判断补发（转人工、查补发库存）；
- 其他异常（投递失败、拒收、找不到收件人、清关问题等）→ 转人工；
- 已签收 → 正常（买家说没收到由风险闸门另判）；
- 已到取件点（取件柜、ParcelShop 等，17TRACK AvailableForPickup）→ 等买家去取，不算断更，不转人工；
- 其余按最近一条动态距今的天数：≥ LOGISTICS_STALL_DAYS 算断更、≥ LOGISTICS_RESHIP_DAYS 算长时间未更新，都要人工判断补发；
  有跟踪号但一直查不到轨迹的，发货后满 LOGISTICS_STALL_DAYS 天同样按断更处理（刚发货的只转人工核实）。
谷仓的时间是东八区、没有时区标记，这里按 +08:00 解析。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..config import Settings, get_settings
from .track17 import RISKY_STATUS

CN = timezone(timedelta(hours=8))
STAGE_LABELS = {
    "NO_TRACKING": "没有跟踪号", "NO_INFO": "查不到物流轨迹", "EXCEPTION": "物流异常", "DELIVERED": "已签收", "PICKUP": "已到取件点，等买家取件",
    "RESHIP_REVIEW": "轨迹长时间未更新，需判断补发", "STALLED": "轨迹多天未更新，需判断补发", "OK": "正常运输中",
    "UNKNOWN": "未开启轨迹查询", "LOST_DAMAGED": "轨迹显示损坏/丢失/退回，需判断补发",
}
# 17TRACK 这些状态说明包裹到不了买家手里：按需补发处理（运输过久也算长时间没更新）
RESHIP_SUB_STATUS = {"Exception_Damage": "损坏", "Exception_Lost": "丢失", "Exception_Returning": "退件中",
                     "Exception_Returned": "已退回", "Exception_Destroyed": "已销毁", "Expired_Other": "运输过久"}
# 需要人工的阶段 → 风险原因码
HUMAN_REASONS = {"NO_TRACKING": "TRACKING_NOT_FOUND", "NO_INFO": "TRACKING_NOT_FOUND", "EXCEPTION": "TRACKING_EXCEPTION",
                 "STALLED": "TRACKING_STALLED", "RESHIP_REVIEW": "TRACKING_RESHIP_REVIEW",
                 "LOST_DAMAGED": "TRACKING_LOST_DAMAGED"}


def parse_time(value: str | None, naive_tz=timezone.utc) -> datetime | None:
    """ISO 时间（带或不带时区）或谷仓的 'YYYY-MM-DD HH:MM:SS'（东八区）→ UTC aware。"""
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v.replace(" ", "T", 1) if "T" not in v else v)
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=naive_tz)).astimezone(timezone.utc)


def assess(pkg: dict, *, tracking_enabled: bool, now: datetime | None = None,
           settings: Settings | None = None) -> dict:
    s = settings or get_settings()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    t = pkg.get("tracking") or {}
    wh = pkg.get("warehouse") or {}
    last = None
    events = t.get("events") or []
    if events:
        last = parse_time(events[0].get("time_utc")) or parse_time(events[0].get("time"))
    if last is None:  # 没有轨迹节点时退回谷仓揽收 / 出库时间、eBay 发货时间
        last = (parse_time(wh.get("ascan_time"), CN) or parse_time(wh.get("date_shipping"), CN)
                or parse_time(pkg.get("event_time")))
    days = round((now - last).total_seconds() / 86400, 1) if last else None

    if not pkg.get("tracking_number"):
        stage = "NO_TRACKING"
    elif not tracking_enabled:
        stage = "UNKNOWN"
    elif not t or t.get("status") in (None, "NotFound"):
        # 有跟踪号却一直没有轨迹：发货多天后仍查不到按断更处理（需判断补发），刚发货的只转人工核实
        stage = ("RESHIP_REVIEW" if days is not None and days >= s.logistics_reship_days else
                 "STALLED" if days is not None and days >= s.logistics_stall_days else "NO_INFO")
    elif t.get("status") == "Delivered":
        stage = "DELIVERED"
    elif t.get("status") == "AvailableForPickup":  # 包裹在取件点等买家，轨迹不会再更新：不是断更
        stage = "PICKUP"
    elif t.get("sub_status") in RESHIP_SUB_STATUS or t.get("status") == "Expired":
        stage = "LOST_DAMAGED"
    elif t.get("status") in RISKY_STATUS or (t.get("sub_status") or "").startswith("Exception"):
        stage = "EXCEPTION"
    elif days is not None and days >= s.logistics_reship_days:
        stage = "RESHIP_REVIEW"
    elif days is not None and days >= s.logistics_stall_days:
        stage = "STALLED"
    else:
        stage = "OK"
    label = STAGE_LABELS[stage]
    if stage == "LOST_DAMAGED":
        label = f"轨迹显示{RESHIP_SUB_STATUS.get(t.get('sub_status'), '运输过久')}，需判断补发"
    elif stage in ("STALLED", "RESHIP_REVIEW") and not events:
        label = "发货后多天查不到轨迹，需判断补发"
    return {"stage": stage, "stage_label": label,
            "last_update": last.isoformat(timespec="minutes") if last else None,
            "days_without_update": days, "needs_human": stage in HUMAN_REASONS,
            "reason_code": HUMAN_REASONS.get(stage)}
